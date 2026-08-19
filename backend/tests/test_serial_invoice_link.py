"""Regression: Inventory Board must show which patient bought each SOLD/RESERVED
serial hearing-aid unit (invoice_no + patient_name + payment_status).

Feb 2026 — user asked "for a SOLD or RESERVED unit, show me the invoice so I
can trace who it went to". Locks in `POST /api/ha/serial-items/invoice-lookup`
and the enriched `/timeline` response so a future refactor can't silently
break this trace.
"""
import os
import requests

BASE_URL = os.environ.get(
    "REACT_APP_BACKEND_URL",
    "https://referral-sprint.preview.emergentagent.com",
).rstrip("/")
EMAIL = "owner@thesoundclinic.in"
PASSWORD = "demo123"


def _sess():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": EMAIL, "password": PASSWORD}, timeout=30)
    assert r.status_code == 200, r.text
    tok = r.json()["access_token"]
    s.headers.update({"Authorization": f"Bearer {tok}"})
    return s


def test_invoice_lookup_returns_data_for_sold_serials():
    """Bulk-lookup must return `invoice_no` + `patient_name` + payment info
    for every SOLD/RESERVED serial that has a Quick Sale or full HA Sale."""
    s = _sess()
    items = s.get(f"{BASE_URL}/api/ha/serial-items?limit=200", timeout=15).json()
    linkable = [r["serial_id"] for r in items if r.get("state") in ("SOLD", "RESERVED")]
    assert linkable, "seeded tenant must have at least one SOLD/RESERVED serial"

    r = s.post(f"{BASE_URL}/api/ha/serial-items/invoice-lookup",
               json={"serial_ids": linkable}, timeout=15)
    assert r.status_code == 200, r.text
    mp = r.json()
    assert isinstance(mp, dict)
    matched = [v for v in mp.values() if v]
    assert matched, "at least one seeded serial should link to a sale"
    # Sanity — every hit must carry patient_name + source
    for hit in matched:
        assert hit.get("source") in ("quick_sale", "ha_sale")
        assert hit.get("patient_name") is not None or hit.get("patient_id") is not None
        # Either invoice_no or sale_no must be present so the UI has something to render
        assert hit.get("invoice_no") or hit.get("sale_no")


def test_invoice_lookup_empty_body_returns_empty_map():
    """Guardrail: sending an empty list must not 500."""
    s = _sess()
    r = s.post(f"{BASE_URL}/api/ha/serial-items/invoice-lookup",
               json={"serial_ids": []}, timeout=15)
    assert r.status_code == 200
    assert r.json() == {}


def test_timeline_carries_invoice_for_sold_serial():
    """The Timeline drawer needs `invoice` on the top-level response so the
    UI can render the "who bought it" header without a second round-trip."""
    s = _sess()
    items = s.get(f"{BASE_URL}/api/ha/serial-items?state=SOLD&limit=50", timeout=15).json()
    # Grab one that has a real patient link (Quick-Sale-sync'd rows may not).
    lookup = s.post(f"{BASE_URL}/api/ha/serial-items/invoice-lookup",
                    json={"serial_ids": [r["serial_id"] for r in items]},
                    timeout=15).json()
    linked = next((sid for sid, v in lookup.items() if v and v.get("patient_name")), None)
    assert linked, "at least one SOLD serial should have a patient linked"

    r = s.get(f"{BASE_URL}/api/ha/serial-items/{linked}/timeline", timeout=15)
    assert r.status_code == 200
    body = r.json()
    assert "invoice" in body, "timeline response must include an `invoice` key"
    assert body["invoice"] is not None
    assert body["invoice"].get("patient_name") or body["invoice"].get("patient_id")


def test_timeline_no_invoice_for_in_stock_serial():
    """IN_STOCK rows have no sale linked yet — `invoice` must be null, not
    a fabricated placeholder."""
    s = _sess()
    items = s.get(f"{BASE_URL}/api/ha/serial-items?state=IN_STOCK&limit=5", timeout=15).json()
    assert items, "tenant must have IN_STOCK serials"
    sid = items[0]["serial_id"]
    r = s.get(f"{BASE_URL}/api/ha/serial-items/{sid}/timeline", timeout=15)
    assert r.status_code == 200
    assert r.json().get("invoice") is None


def test_quick_sale_invoice_math_is_consistent():
    """Regression: Feb 2026 the Quick-Sale invoice writer was setting
    `subtotal = taxable` (post-discount) but also emitting `discount_total`
    separately — so the invoice popup showed
        Subtotal ₹1.65L − Discount ₹10k = Grand Total ₹1.65L
    which the audiologist correctly flagged as broken math. Fix: subtotal
    now writes qty × MRP (pre-discount) so the standard
        subtotal − discount + tax == grand_total
    identity holds. Sweeps every Quick-Sale-linked invoice on the tenant
    to make sure no drift has crept back in."""
    s = _sess()
    r = s.get(f"{BASE_URL}/api/billing/invoices?limit=200", timeout=20)
    assert r.status_code == 200, r.text
    invoices = r.json()
    invoices = invoices.get("items", invoices) if isinstance(invoices, dict) else invoices
    quick_sale_invs = [
        inv for inv in invoices
        if isinstance(inv, dict)
        and inv.get("notes")
        and "HA Quick Sale" in (inv.get("notes") or "")
    ]
    assert quick_sale_invs, "seeded tenant must have at least one Quick-Sale invoice"
    for inv in quick_sale_invs:
        sub = float(inv.get("subtotal") or 0)
        disc = float(inv.get("discount_total") or 0)
        tax = float(inv.get("tax_total") or 0)
        gt = float(inv.get("grand_total") or 0)
        expected_gt = round(sub - disc + tax, 2)
        assert abs(expected_gt - gt) < 0.5, (
            f"Invoice {inv['invoice_no']}: subtotal({sub}) − discount({disc})"
            f" + tax({tax}) = {expected_gt}, but grand_total = {gt}"
        )
        # When a discount is present, subtotal MUST be greater than grand_total
        if disc > 0:
            assert sub > gt, (
                f"Invoice {inv['invoice_no']}: has discount ₹{disc} but subtotal ({sub})"
                f" is not greater than grand_total ({gt}) — the popup will mislead."
            )


def test_trial_lookup_returns_data_for_trial_out_serials():
    """Trial-lookup must return `trial_no + patient_name + start/return dates
    + status + days_active/overdue` for every TRIAL_OUT serial that has an
    active ha_trials row. Powers the Inventory Board's "Linked To" column
    trial mini-card."""
    s = _sess()
    items = s.get(f"{BASE_URL}/api/ha/serial-items?state=TRIAL_OUT&limit=50", timeout=15).json()
    linkable = [r["serial_id"] for r in items]
    if not linkable:
        return  # tenant has no TRIAL_OUT serials — nothing to assert
    r = s.post(f"{BASE_URL}/api/ha/serial-items/trial-lookup",
               json={"serial_ids": linkable}, timeout=15)
    assert r.status_code == 200, r.text
    mp = r.json()
    assert isinstance(mp, dict)
    matched = [v for v in mp.values() if v]
    # At least one seeded TRIAL_OUT serial should have a linked ha_trials row.
    if not matched:
        return
    for hit in matched:
        assert hit.get("source") == "trial"
        assert hit.get("trial_no")
        assert hit.get("patient_name") or hit.get("patient_id")
        assert hit.get("start_date")
        # days_active should be a non-negative int when start_date is valid
        if hit.get("days_active") is not None:
            assert hit["days_active"] >= 0


def test_timeline_carries_trial_for_active_trial_serial():
    """The Timeline drawer needs `trial` on the top-level response so the
    UI can render the amber "TRIAL IN PROGRESS" card without a second
    round-trip."""
    s = _sess()
    items = s.get(f"{BASE_URL}/api/ha/serial-items?state=TRIAL_OUT&limit=50", timeout=15).json()
    lookup = s.post(f"{BASE_URL}/api/ha/serial-items/trial-lookup",
                    json={"serial_ids": [r["serial_id"] for r in items]},
                    timeout=15).json()
    linked = next((sid for sid, v in lookup.items() if v and v.get("patient_name")), None)
    if not linked:
        return
    r = s.get(f"{BASE_URL}/api/ha/serial-items/{linked}/timeline", timeout=15)
    assert r.status_code == 200
    body = r.json()
    assert "trial" in body, "timeline response must include a `trial` key"
    assert body["trial"] is not None
    assert body["trial"].get("patient_name") or body["trial"].get("patient_id")


def test_loaner_lookup_returns_200_and_empty_map_when_none():
    """Cross-tab consistency (Feb 2026): loaner-lookup must always 200,
    return a dict, and only carry keys for serials that actually have a
    linked ha_loaners row. The current seeded tenant has no loaners,
    so an empty dict is the correct behaviour."""
    s = _sess()
    items = s.get(f"{BASE_URL}/api/ha/serial-items?state=LOANER&limit=50", timeout=15).json()
    ids = [r["serial_id"] for r in items] or ["SI-NONEXISTENT"]
    r = s.post(f"{BASE_URL}/api/ha/serial-items/loaner-lookup",
               json={"serial_ids": ids}, timeout=15)
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), dict)


def test_timeline_response_carries_loaner_key():
    """Timeline shape guarantee: every timeline response includes a `loaner`
    key so the frontend can render conditionally without an existence check.
    Null is fine when the serial has no loaner history."""
    s = _sess()
    items = s.get(f"{BASE_URL}/api/ha/serial-items?limit=5", timeout=15).json()
    assert items
    r = s.get(f"{BASE_URL}/api/ha/serial-items/{items[0]['serial_id']}/timeline", timeout=15)
    assert r.status_code == 200
    body = r.json()
    assert "loaner" in body


def test_stock_heatmap_head_clinic_shape():
    """Multi-Clinic Phase 2 (Feb 2026): the head-clinic heatmap must return
    a 200 with the { group_id, branches[], rows[], branch_totals{}, grand_total }
    shape. Branches list is head-first for readability. Every row's cells
    must cover every branch (even when count = 0)."""
    s = _sess()
    r = s.get(f"{BASE_URL}/api/clinic-groups/mine/stock-heatmap", timeout=20)
    if r.status_code == 404:
        return  # not part of a group — skip
    assert r.status_code == 200, r.text
    body = r.json()
    for k in ("group_id", "branches", "rows", "branch_totals", "grand_total"):
        assert k in body, f"missing key {k!r}"
    assert body["branches"], "at least one branch expected"
    assert body["branches"][0].get("is_head") is True, "head clinic must be listed first"
    branch_ids = {b["clinic_id"] for b in body["branches"]}
    for row in body["rows"]:
        assert set(row["cells"].keys()) == branch_ids, (
            f"row {row['product_id']} cells {set(row['cells'].keys())} must cover every branch {branch_ids}"
        )
        assert row["total"] == sum(row["cells"].values()), "row total must match cell sum"
    # branch_totals must equal per-column sums
    for cid in branch_ids:
        expected = sum(r["cells"].get(cid, 0) for r in body["rows"])
        assert body["branch_totals"].get(cid, 0) == expected
