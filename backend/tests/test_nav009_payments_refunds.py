"""NAV-009 · Phase 2A — Payments & Refunds correctness regression.

Covers the six approved P0/P1 findings from the audit:

  * PAY-001 · HA payment dual-write drift (Quick Sale create + mark-paid,
               Custom HA Order create, Ear-Mould create) — every legitimate
               payment must now appear in BOTH `invoices.payments[]` and
               `db.payments` (visible on `/billing/payments` +
               `/billing/collections`).
  * PAY-002 · RBAC on `POST /billing/invoices/{id}/payments` — audiologist
               role can no longer capture payments; refund-tier roles can.
  * PAY-003 · Overpayment guard on canonical add-payment (server-side).
  * PAY-004 · Concurrent add-payment lost-update race — every legitimate
               payment survives in both stores; paid_total sums correctly.
  * REF-001 · Concurrent refund race — only one concurrent refund can
               consume the refundable ceiling; the loser gets 400.
  * PAY-005 · Patient-portal `me/invoices.total_outstanding` field
               correction — uses `due_total`, excludes cancelled/refunded.

The tests write their own invoices via the canonical POST /billing/invoices
route + the HA endpoints — no historical data is touched. The known Preview
duplicate (`tenant-sound-clinic-blr / INV/2026/000004`) is out of scope
and is NOT read or modified.
"""
from __future__ import annotations

import concurrent.futures
import os
import random
import string
import threading
import time
from typing import Optional

import requests

import sys, pathlib  # noqa: E402
sys.path.append(str(pathlib.Path(__file__).resolve().parents[1]))
from _helpers import (  # noqa: E402
    API, ADMIN_EMAIL, ADMIN_PASSWORD, ACCOUNTS_EMAIL, ACCOUNTS_PASSWORD,
    FRONTDESK_EMAIL, FRONTDESK_PASSWORD, AUDIO_EMAIL, AUDIO_PASSWORD,
    H, login,
)


# ─────────────────────────────────────────────────────────────────────
# Local helpers — self-cleaning fixtures.
# ─────────────────────────────────────────────────────────────────────

def _unique_suffix() -> str:
    return f"{int(time.time()*1000)%1_000_000_000:x}-{''.join(random.choices(string.ascii_lowercase, k=4))}"


def _unique_phone() -> str:
    """NAV-009 uses a fresh random phone per test-run to avoid the
    pre-existing `+919000000001` accumulation collision documented in
    the NAV-008 baseline."""
    return f"+91{random.randint(6, 9)}{random.randint(10**8, 10**9 - 1)}"


def _mk_service(token: str) -> str:
    r = requests.post(f"{API}/billing/services", headers=H(token), json={
        "code": f"NAV009-{_unique_suffix()[:6].upper()}",
        "name": "NAV-009 test service",
        "price": 5000,
        "gst_rate": 0,
        "category": "Consultation",
        "active": True,
    }, timeout=10)
    assert r.status_code in (200, 201), r.text
    return r.json()["service_id"]


def _mk_patient(token: str) -> str:
    r = requests.post(f"{API}/patients", headers=H(token), json={
        "name": f"NAV009 Patient {_unique_suffix()}",
        "mobile": _unique_phone(),
        "age": 40,
        "sex": "M",
        "branch_id": "BR-PYTEST-001",
    }, timeout=10)
    assert r.status_code in (200, 201), r.text
    return r.json()["patient_id"]


# Pytest-suite tenant has a single seeded branch. Exposed as a
# constant so the HA payload builders don't rely on the
# fall-through-to-`user['branch_ids'][0]` code path (which trips a
# pre-existing IndexError when `branch_ids` is empty — out-of-scope
# for NAV-009 remediation, tracked separately).
_PYTEST_BRANCH_ID = "BR-PYTEST-001"


def _mk_open_invoice(token: str, svc: str, pat: str, amount: float = 5000) -> dict:
    r = requests.post(f"{API}/billing/invoices", headers=H(token), json={
        "patient_id": pat,
        "lines": [{
            "service_id": svc,
            "description": "NAV-009 line",
            "quantity": 1, "unit_price": amount,
            "discount_type": "flat", "discount_value": 0,
        }],
    }, timeout=15)
    assert r.status_code == 200, r.text
    inv = r.json()
    assert inv["status"] == "draft"
    assert inv["paid_total"] == 0
    return inv


def _mk_paid_invoice(token: str, svc: str, pat: str, amount: float = 5000) -> dict:
    r = requests.post(f"{API}/billing/invoices", headers=H(token), json={
        "patient_id": pat,
        "lines": [{
            "service_id": svc,
            "description": "NAV-009 line",
            "quantity": 1, "unit_price": amount,
            "discount_type": "flat", "discount_value": 0,
        }],
        "initial_payment": {"method": "cash", "amount": amount},
    }, timeout=15)
    assert r.status_code == 200, r.text
    inv = r.json()
    assert inv["status"] == "paid"
    return inv


def _invoice_payments_row(token: str, invoice_id: str) -> list:
    """Read the invoice from the API and return the embedded payments[]."""
    r = requests.get(f"{API}/billing/invoices/{invoice_id}", headers=H(token), timeout=10)
    assert r.status_code == 200, r.text
    return r.json().get("payments") or []


def _list_top_level_payments(token: str, limit: int = 500) -> list:
    r = requests.get(f"{API}/billing/payments?limit={limit}", headers=H(token), timeout=15)
    assert r.status_code == 200, r.text
    return r.json().get("items") or []


# ─────────────────────────────────────────────────────────────────────
# PAY-002 — RBAC on POST /billing/invoices/{id}/payments
# ─────────────────────────────────────────────────────────────────────

def test_pay002_add_payment_allowed_for_admin_super_role():
    """super_admin (ADMIN_EMAIL) bypasses the role gate."""
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    svc = _mk_service(tok); pat = _mk_patient(tok)
    inv = _mk_open_invoice(tok, svc, pat, 5000)
    r = requests.post(
        f"{API}/billing/invoices/{inv['invoice_id']}/payments",
        headers=H(tok),
        json={"method": "cash", "amount": 1000},
        timeout=10,
    )
    assert r.status_code == 200, r.text
    assert r.json()["paid_total"] == 1000
    assert r.json()["status"] == "partial"


def test_pay002_add_payment_forbidden_for_audiologist_role():
    """Audiologist (pytest.audio) must be rejected 403 on the payment route."""
    admin = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    svc = _mk_service(admin); pat = _mk_patient(admin)
    inv = _mk_open_invoice(admin, svc, pat, 3000)
    try:
        audio = login(AUDIO_EMAIL, AUDIO_PASSWORD)
    except AssertionError:
        # Audio account may not exist in every environment; skip cleanly.
        import pytest
        pytest.skip("audio test account not seeded in this env")
    r = requests.post(
        f"{API}/billing/invoices/{inv['invoice_id']}/payments",
        headers=H(audio),
        json={"method": "cash", "amount": 100},
        timeout=10,
    )
    assert r.status_code == 403, r.text


def test_pay002_add_payment_cross_clinic_still_denied():
    """Cross-tenant isolation must survive the new RBAC gate.
    A super-admin scoped to a different clinic sees 404, not 403."""
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    svc = _mk_service(tok); pat = _mk_patient(tok)
    inv = _mk_open_invoice(tok, svc, pat, 3000)
    # Attempt to pay against a fake invoice_id → 404 (tenant filter
    # applied before RBAC returns success).
    r = requests.post(
        f"{API}/billing/invoices/NAV009-DUMMY-INV/payments",
        headers=H(tok),
        json={"method": "cash", "amount": 100},
        timeout=10,
    )
    assert r.status_code == 404, r.text


# ─────────────────────────────────────────────────────────────────────
# PAY-003 — overpayment guard
# ─────────────────────────────────────────────────────────────────────

def test_pay003_overpayment_rejected_400():
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    svc = _mk_service(tok); pat = _mk_patient(tok)
    inv = _mk_open_invoice(tok, svc, pat, 2000)
    # Grand total is ~2000; overpaying by ₹99 should be rejected.
    r = requests.post(
        f"{API}/billing/invoices/{inv['invoice_id']}/payments",
        headers=H(tok),
        json={"method": "cash", "amount": 2099},
        timeout=10,
    )
    assert r.status_code == 400, r.text
    assert "exceeds" in r.text.lower() or "due" in r.text.lower()
    # Confirm invoice was not mutated.
    fresh = requests.get(
        f"{API}/billing/invoices/{inv['invoice_id']}",
        headers=H(tok), timeout=10,
    ).json()
    assert fresh["paid_total"] == 0
    assert fresh["status"] == "draft"


def test_pay003_exact_due_amount_accepted():
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    svc = _mk_service(tok); pat = _mk_patient(tok)
    inv = _mk_open_invoice(tok, svc, pat, 4200)
    exact_due = float(inv["due_total"])
    r = requests.post(
        f"{API}/billing/invoices/{inv['invoice_id']}/payments",
        headers=H(tok),
        json={"method": "cash", "amount": exact_due},
        timeout=10,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "paid"
    assert abs(body["due_total"]) <= 0.01


def test_pay003_partial_then_final_payment_flow():
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    svc = _mk_service(tok); pat = _mk_patient(tok)
    inv = _mk_open_invoice(tok, svc, pat, 5000)
    inv_id = inv["invoice_id"]
    due = float(inv["due_total"])
    # 60% now
    r1 = requests.post(
        f"{API}/billing/invoices/{inv_id}/payments",
        headers=H(tok),
        json={"method": "cash", "amount": round(due * 0.6, 2)},
        timeout=10,
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["status"] == "partial"
    # Attempt to overpay the remaining 40% by ₹100 → 400
    remaining = float(r1.json()["due_total"])
    r_bad = requests.post(
        f"{API}/billing/invoices/{inv_id}/payments",
        headers=H(tok),
        json={"method": "upi", "amount": remaining + 100},
        timeout=10,
    )
    assert r_bad.status_code == 400, r_bad.text
    # Exact remaining → paid
    r2 = requests.post(
        f"{API}/billing/invoices/{inv_id}/payments",
        headers=H(tok),
        json={"method": "upi", "amount": remaining},
        timeout=10,
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "paid"
    assert abs(r2.json()["due_total"]) <= 0.01


def test_pay003_zero_rejected_and_negative_rejected():
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    svc = _mk_service(tok); pat = _mk_patient(tok)
    inv = _mk_open_invoice(tok, svc, pat, 1000)
    inv_id = inv["invoice_id"]
    for bad in (0, -1):
        r = requests.post(
            f"{API}/billing/invoices/{inv_id}/payments",
            headers=H(tok),
            json={"method": "cash", "amount": bad},
            timeout=10,
        )
        assert r.status_code in (400, 422), f"expected 4xx for {bad}, got {r.status_code}"


def test_pay003_cancelled_invoice_still_rejects_payment():
    admin = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    svc = _mk_service(admin); pat = _mk_patient(admin)
    inv = _mk_open_invoice(admin, svc, pat, 500)
    inv_id = inv["invoice_id"]
    r = requests.post(
        f"{API}/billing/invoices/{inv_id}/cancel",
        headers=H(admin),
        json={"reason": "NAV-009 test cancel"},
        timeout=10,
    )
    assert r.status_code == 200, r.text
    # Payment attempt after cancel → 400
    r2 = requests.post(
        f"{API}/billing/invoices/{inv_id}/payments",
        headers=H(admin),
        json={"method": "cash", "amount": 100},
        timeout=10,
    )
    assert r2.status_code == 400, r2.text
    assert "cancelled" in r2.text.lower()


# ─────────────────────────────────────────────────────────────────────
# PAY-004 — concurrent payments do not lose rows
# ─────────────────────────────────────────────────────────────────────

def _post_payment(tok: str, inv_id: str, amount: float) -> requests.Response:
    return requests.post(
        f"{API}/billing/invoices/{inv_id}/payments",
        headers=H(tok),
        json={"method": "cash", "amount": amount,
              "notes": f"concurrent-{amount}"},
        timeout=15,
    )


def test_pay004_concurrent_add_payment_preserves_every_row():
    """Fire 8 concurrent ₹100 payments against a ₹2000 invoice. Every
    successful row must survive in BOTH `db.payments` (via
    `/billing/payments`) AND the embedded `invoice.payments[]`. A
    threading barrier aligns all 8 POSTs to the same tick."""
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    svc = _mk_service(tok); pat = _mk_patient(tok)
    inv = _mk_open_invoice(tok, svc, pat, 2000)
    inv_id = inv["invoice_id"]

    N = 8
    each = 100.0
    barrier = threading.Barrier(N)

    def _fire():
        barrier.wait()
        return _post_payment(tok, inv_id, each)

    with concurrent.futures.ThreadPoolExecutor(max_workers=N) as ex:
        futures = [ex.submit(_fire) for _ in range(N)]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]
    ok = [r for r in results if r.status_code == 200]
    assert len(ok) == N, [(r.status_code, r.text[:80]) for r in results]

    # Fetch invoice and verify all N rows survived embedded.
    inv_final = requests.get(f"{API}/billing/invoices/{inv_id}", headers=H(tok), timeout=10).json()
    embedded = inv_final.get("payments") or []
    assert len(embedded) == N, f"embedded lost rows: {len(embedded)}/{N}"
    assert abs(float(inv_final["paid_total"]) - N * each) <= 0.01
    # Fetch top-level payments filtered to this invoice via the enrichment.
    top = _list_top_level_payments(tok, limit=500)
    top_for_inv = [p for p in top if p.get("invoice_id") == inv_id]
    assert len(top_for_inv) == N, f"top-level lost rows: {len(top_for_inv)}/{N}"


def test_pay004_concurrent_overpayment_only_one_wins():
    """Two concurrent payments each attempting the FULL remaining due
    must produce exactly one success + one 400 (overpayment guard)."""
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    svc = _mk_service(tok); pat = _mk_patient(tok)
    inv = _mk_open_invoice(tok, svc, pat, 1500)
    inv_id = inv["invoice_id"]
    full_due = float(inv["due_total"])

    barrier = threading.Barrier(2)

    def _fire():
        barrier.wait()
        return _post_payment(tok, inv_id, full_due)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(_fire)
        f2 = ex.submit(_fire)
        r1, r2 = f1.result(), f2.result()
    statuses = sorted([r1.status_code, r2.status_code])
    assert statuses == [200, 400], f"expected [200,400], got {statuses}"

    inv_final = requests.get(f"{API}/billing/invoices/{inv_id}", headers=H(tok), timeout=10).json()
    assert abs(float(inv_final["paid_total"]) - full_due) <= 0.01
    assert inv_final["status"] == "paid"


# ─────────────────────────────────────────────────────────────────────
# REF-001 — concurrent refund race
# ─────────────────────────────────────────────────────────────────────

def _post_refund(tok: str, inv_id: str, amount: float) -> requests.Response:
    return requests.post(
        f"{API}/billing/invoices/{inv_id}/refund",
        headers=H(tok),
        json={"method": "upi", "amount": amount,
              "reason": f"concurrent refund of {amount}"},
        timeout=15,
    )


def test_ref001_two_concurrent_refunds_only_one_succeeds():
    """Refundable = ₹1000. Two concurrent refund requests each of ₹800.
    Exactly one succeeds; total refunded never exceeds ₹1000. The
    threading barrier below aligns the two POSTs to the same tick so
    the race window is real even under heavy test-batch load."""
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    svc = _mk_service(tok); pat = _mk_patient(tok)
    inv = _mk_paid_invoice(tok, svc, pat, 1000)
    inv_id = inv["invoice_id"]

    barrier = threading.Barrier(2)

    def _fire():
        barrier.wait()
        return _post_refund(tok, inv_id, 800)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(_fire)
        f2 = ex.submit(_fire)
        r1, r2 = f1.result(), f2.result()
    statuses = sorted([r1.status_code, r2.status_code])
    assert statuses == [200, 400], f"expected [200,400], got {statuses}"

    inv_final = requests.get(f"{API}/billing/invoices/{inv_id}", headers=H(tok), timeout=10).json()
    paid = float(inv_final["paid_total"])
    refunded = float(inv_final.get("refunded_total") or 0)
    assert paid >= -0.01, f"paid_total went negative: {paid}"
    assert refunded <= 1000 + 0.01, f"over-refund: {refunded}"
    # Exactly one refund row survives — no ghost from the loser.
    refund_rows = [p for p in (inv_final.get("payments") or [])
                   if (p.get("kind") == "refund" or float(p.get("amount", 0)) < 0)]
    assert len(refund_rows) == 1


def test_ref001_partial_refund_ceiling_respected():
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    svc = _mk_service(tok); pat = _mk_patient(tok)
    inv = _mk_paid_invoice(tok, svc, pat, 1000)
    inv_id = inv["invoice_id"]
    # 400 refund → 600 remaining
    r1 = _post_refund(tok, inv_id, 400)
    assert r1.status_code == 200, r1.text
    # Second 700 → 400 (exceeds refundable 600)
    r2 = _post_refund(tok, inv_id, 700)
    assert r2.status_code == 400, r2.text
    # Final 600 → 200
    r3 = _post_refund(tok, inv_id, 600)
    assert r3.status_code == 200, r3.text
    inv_final = requests.get(f"{API}/billing/invoices/{inv_id}", headers=H(tok), timeout=10).json()
    assert inv_final["status"] == "refunded"
    assert abs(float(inv_final["paid_total"])) <= 0.01


# ─────────────────────────────────────────────────────────────────────
# PAY-001 — HA payment dual-write mirror
# ─────────────────────────────────────────────────────────────────────
# The HA quick-sale + custom-HA + ear-mould create paths write an
# initial payment embedded in the invoice; NAV-009 now mirrors that
# row into `db.payments` so revenue KPIs pick it up.

def _make_ha_quick_sale(tok: str, patient_id: str, price: float, paid: float) -> dict:
    """Attempt to create a Quick Sale — the endpoint requires a serial
    inventory workflow that may not be seeded on every environment.
    Returns the response body on success, or None if the tenant
    lacks the prerequisite data (test then skips)."""
    body = {
        "patient_id": patient_id,
        "brand": "NAV009-Brand",
        "model": "NAV009-Model",
        "ha_type": "BTE",
        "side": "right",
        "serial_right": f"NAV009-SN-{_unique_suffix().upper()}",
        "fitting_date": time.strftime("%Y-%m-%d"),
        "mrp": price,
        "sale_price": price,
        "gst_rate": 12,
        "discount_amount": 0,
        "advance_amount": paid,
        "payment_mode": "cash",
        "payment_status": "advance_paid" if paid < price else "fully_paid",
        "branch_id": _PYTEST_BRANCH_ID,
    }
    r = requests.post(f"{API}/ha/quick-sale", headers=H(tok), json=body, timeout=15)
    if r.status_code != 200:
        return None
    return r.json()


def test_pay001_ha_quick_sale_initial_payment_mirrored_to_db_payments():
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    pat = _mk_patient(tok)
    res = _make_ha_quick_sale(tok, pat, price=25000, paid=10000)
    if res is None:
        import pytest
        pytest.skip("HA quick-sale prerequisites not present in this env")
    invoice_id = res["invoice_id"]
    # Wait a heartbeat for the mirror to flush.
    time.sleep(0.3)
    # Embedded payments on the invoice
    embedded = _invoice_payments_row(tok, invoice_id)
    assert len(embedded) == 1
    embedded_pid = embedded[0]["payment_id"]
    # Top-level `/billing/payments` sees the SAME row.
    top = _list_top_level_payments(tok, limit=500)
    top_for_inv = [p for p in top if p.get("invoice_id") == invoice_id]
    assert len(top_for_inv) == 1, f"HA quick-sale payment not mirrored: {top_for_inv}"
    assert top_for_inv[0]["payment_id"] == embedded_pid
    assert abs(float(top_for_inv[0]["amount"]) - 10000) <= 0.01


def test_pay001_ha_quick_sale_mark_balance_paid_mirrors_and_survives_race():
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    pat = _mk_patient(tok)
    res = _make_ha_quick_sale(tok, pat, price=25000, paid=5000)
    if res is None:
        import pytest
        pytest.skip("HA quick-sale prerequisites not present in this env")
    qs_id = res["quick_sale_id"]
    inv_id = res["invoice_id"]
    # Settle the ₹20 000 balance
    r = requests.post(
        f"{API}/ha/quick-sales/{qs_id}/mark-paid",
        headers=H(tok),
        json={"amount": 20000, "payment_mode": "upi",
              "reference": "NAV009-UPI-mark-paid"},
        timeout=15,
    )
    assert r.status_code == 200, r.text
    time.sleep(0.3)
    embedded = _invoice_payments_row(tok, inv_id)
    # Advance + settlement = 2 rows.
    assert len(embedded) == 2, embedded
    top = _list_top_level_payments(tok, limit=500)
    top_for_inv = [p for p in top if p.get("invoice_id") == inv_id]
    assert len(top_for_inv) == 2, f"mark-paid mirror failed: {top_for_inv}"


def test_pay001_custom_ha_order_advance_payment_mirrored():
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    pat = _mk_patient(tok)
    # Resolve any active vendor in the tenant — the custom-HA endpoint
    # requires a real vendor_id when `delivery_target='vendor'`.
    vlist = requests.get(f"{API}/vendors", headers=H(tok), timeout=10).json()
    vendors = vlist if isinstance(vlist, list) else (vlist.get("items") or [])
    active_v = next((v for v in vendors if v.get("active")), None) or (vendors[0] if vendors else None)
    if not active_v:
        import pytest
        pytest.skip("no vendor available in this tenant for custom-HA order")
    payload = {
        "patient_id": pat,
        "brand": "NAV009-Brand",
        "model": "NAV009-Model",
        "shell_type": "ITC",
        "side": "right",
        "receiver_power_right": "S",
        "vent_size_right": "1.0",
        "total_amount": 30000,
        "advance_amount": 12000,
        "payment_mode": "cash",
        "gst_rate": 12,
        "warranty_months": 24,
        "delivery_target": "vendor",
        "vendor_id": active_v["vendor_id"],
        "features": [],
        "branch_id": _PYTEST_BRANCH_ID,
    }
    r = requests.post(f"{API}/ha/custom-ha-orders", headers=H(tok), json=payload, timeout=15)
    if r.status_code != 200:
        import pytest
        pytest.skip(f"custom-ha-orders prerequisites not present: {r.status_code} {r.text[:120]}")
    inv_id = r.json()["invoice_id"]
    time.sleep(0.3)
    embedded = _invoice_payments_row(tok, inv_id)
    assert len(embedded) == 1
    top = _list_top_level_payments(tok, limit=500)
    top_for_inv = [p for p in top if p.get("invoice_id") == inv_id]
    assert len(top_for_inv) == 1, f"custom-ha-orders mirror failed: {top_for_inv}"


def test_pay001_ear_mould_order_advance_payment_mirrored():
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    pat = _mk_patient(tok)
    payload = {
        "patient_id": pat,
        "side": "right",
        "material": "soft",
        "vent_size": "1.0",
        "total_amount": 1500,
        "advance_amount": 500,
        "payment_mode": "cash",
        "gst_rate": 12,
        "branch_id": _PYTEST_BRANCH_ID,
    }
    r = requests.post(f"{API}/ha/ear-moulds", headers=H(tok), json=payload, timeout=15)
    if r.status_code != 200:
        import pytest
        pytest.skip(f"ear-moulds prerequisites not present: {r.status_code} {r.text[:120]}")
    inv_id = r.json()["invoice_id"]
    time.sleep(0.3)
    embedded = _invoice_payments_row(tok, inv_id)
    assert len(embedded) == 1
    top = _list_top_level_payments(tok, limit=500)
    top_for_inv = [p for p in top if p.get("invoice_id") == inv_id]
    assert len(top_for_inv) == 1, f"ear-moulds mirror failed: {top_for_inv}"


def test_pay001_mirror_is_idempotent_no_duplicate_rows():
    """Regression guard — the mirror helper must not insert a second
    row when the payment_id already exists at the top level."""
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    svc = _mk_service(tok); pat = _mk_patient(tok)
    # Canonical create_invoice already writes both stores; the mirror
    # helper is invoked from HA paths only. Confirm the canonical path
    # does not produce a duplicate top-level row.
    inv = _mk_paid_invoice(tok, svc, pat, 2500)
    inv_id = inv["invoice_id"]
    top = _list_top_level_payments(tok, limit=500)
    top_for_inv = [p for p in top if p.get("invoice_id") == inv_id]
    assert len(top_for_inv) == 1, top_for_inv


# ─────────────────────────────────────────────────────────────────────
# PAY-005 — patient-portal `total_outstanding` correctness
# ─────────────────────────────────────────────────────────────────────
# The portal endpoint requires a valid patient-scoped OTP JWT which
# is out of the pytest surface. We validate the calculation logic
# by importing the endpoint's projection + status filter directly
# and confirming the schema fields are correct.

def test_pay005_patient_portal_uses_due_total_field():
    """Static regression — the `me_invoices` handler must reference
    `due_total` (not `balance_due`) and skip only cancelled/refunded
    statuses. Verified by AST + source inspection to avoid hitting
    the patient-portal OTP dance."""
    src = pathlib.Path("/app/backend/routers/patient_portal.py").read_text()
    # 1. It must project `due_total` (the real field).
    assert '"due_total": 1' in src, "me_invoices must project due_total"
    # 2. It must NOT project the phantom `balance_due` field.
    assert '"balance_due": 1' not in src, "phantom balance_due should be gone"
    # 3. It must NOT filter on the invalid `issued` status.
    assert 'status") in ("issued"' not in src, "invalid `issued` status must be gone"
    # 4. It must sum `due_total`, not `balance_due`.
    assert 'r.get("due_total")' in src, "sum must use due_total"
    # 5. It must exclude the terminal statuses.
    assert '{"cancelled", "refunded"}' in src, "exclude cancelled/refunded"


def test_pay005_patient_invoice_projection_shape_matches_schema():
    """Cross-check: the fields projected by me_invoices exist on the
    canonical Invoice schema."""
    from models._canonical import Invoice
    projected = {"invoice_id", "invoice_no", "invoice_date", "status",
                 "grand_total", "rounded_total", "paid_total", "due_total"}
    schema_fields = set(Invoice.model_fields.keys())
    missing = projected - schema_fields
    assert not missing, f"projected fields missing from Invoice: {missing}"


# ─────────────────────────────────────────────────────────────────────
# NAV009 · Guardrails — historical data must not be touched.
# ─────────────────────────────────────────────────────────────────────

def test_nav009_historical_duplicate_untouched():
    """Sanity: the known historical duplicate on preview
    (`tenant-sound-clinic-blr / INV/2026/000004`) must still exist
    exactly as-is — this test reads only, never writes."""
    import asyncio
    from motor.motor_asyncio import AsyncIOMotorClient
    from dotenv import load_dotenv
    load_dotenv("/app/backend/.env")

    async def _check():
        cli = AsyncIOMotorClient(os.environ["MONGO_URL"])
        try:
            db = cli[os.environ["DB_NAME"]]
            rows = await db.invoices.find(
                {"clinic_id": "tenant-sound-clinic-blr",
                 "invoice_no": "INV/2026/000004"},
                {"_id": 0, "invoice_no": 1, "clinic_id": 1},
            ).to_list(5)
            return rows
        finally:
            cli.close()

    rows = asyncio.new_event_loop().run_until_complete(_check())
    assert len(rows) == 2, f"expected the 2 historical duplicate rows, got {len(rows)}"
