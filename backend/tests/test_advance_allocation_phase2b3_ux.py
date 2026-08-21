"""Advance Allocation · Phase 2B.3 (UX) — targeted regression suite.

Strict scope:
    * Verify the NEW `GET /api/advance-receipts/{receipt_id}/allocations`
      endpoint returns the correct ledger view, respects tenant isolation,
      and mirrors the aggregate math from the underlying documents.
    * Prove the "Apply Advance to Existing Invoice" UX flow works
      end-to-end through the Phase 2B.2 endpoint that the new UI calls
      (invoice total preserved, advance recorded as a payment, patient
      matching enforced, RBAC enforced, idempotent retry, over-alloc
      guard, insufficient-advance guard, multiple allocations, partial
      allocation, tenant guard).

The Phase 2B.3 change is UI-first plus one read-only backend endpoint.
This file exercises the read-only endpoint AND re-verifies the
Phase 2B.2 write endpoint under the exact conditions the new UI
produces, to catch any accidental regression to the closed writer
architecture.
"""
from __future__ import annotations

import os
import random
import string
import sys
import pathlib
import time
import uuid
import concurrent.futures

import pytest
import requests

sys.path.append(str(pathlib.Path(__file__).resolve().parents[1]))
from _helpers import (  # noqa: E402
    API,
    ADMIN_EMAIL, ADMIN_PASSWORD,
    FRONTDESK_EMAIL, FRONTDESK_PASSWORD,
    AUDIO_EMAIL, AUDIO_PASSWORD,
    ACCOUNTS_EMAIL, ACCOUNTS_PASSWORD,
    H, login,
)


_BRANCH_ID = "BR-PYTEST-001"


def _uniq() -> str:
    return f"{int(time.time()*1000) % 1_000_000_000:x}{''.join(random.choices(string.ascii_lowercase, k=4))}"


def _key(prefix: str = "aa-ux") -> str:
    return f"{prefix}-{_uniq()}-{uuid.uuid4().hex[:12]}"


def _phone() -> str:
    return f"+91{random.randint(7, 9)}{random.randint(10**8, 10**9 - 1)}"


# ─────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def admin_token() -> str:
    return login(ADMIN_EMAIL, ADMIN_PASSWORD)


@pytest.fixture(scope="module")
def frontdesk_token() -> str:
    return login(FRONTDESK_EMAIL, FRONTDESK_PASSWORD)


@pytest.fixture(scope="module")
def audiologist_token() -> str:
    return login(AUDIO_EMAIL, AUDIO_PASSWORD)


@pytest.fixture(scope="module")
def accounts_token() -> str:
    return login(ACCOUNTS_EMAIL, ACCOUNTS_PASSWORD)


def _mk_patient(token: str) -> str:
    r = requests.post(f"{API}/patients", headers=H(token), json={
        "name": f"UX Patient {_uniq()}", "mobile": _phone(),
        "age": 42, "sex": "F", "branch_id": _BRANCH_ID,
    }, timeout=15)
    assert r.status_code in (200, 201), r.text
    return r.json()["patient_id"]


def _mk_invoice(token: str, patient_id: str, *, unit_price: float) -> dict:
    r = requests.post(f"{API}/billing/invoices", headers=H(token), json={
        "patient_id": patient_id,
        "lines": [{
            "description": f"UX line {_uniq()}",
            "quantity": 1, "unit_price": unit_price, "is_taxable": False,
        }],
    }, timeout=15)
    assert r.status_code in (200, 201), r.text
    return r.json()


def _mk_advance(token: str, patient_id: str, *, amount: float) -> dict:
    r = requests.post(
        f"{API}/advance-receipts",
        headers={**H(token), "Idempotency-Key": _key("ar")},
        json={"patient_id": patient_id, "received_amount": amount, "method": "cash"},
        timeout=15,
    )
    assert r.status_code == 200, r.text
    return r.json()


def _post_alloc(token: str, receipt_id: str, invoice_id: str, amount: float,
                *, key: str | None = None) -> requests.Response:
    body = {"invoice_id": invoice_id, "amount": amount}
    headers = H(token)
    if key is not None:
        headers["Idempotency-Key"] = key
    return requests.post(
        f"{API}/advance-receipts/{receipt_id}/allocations",
        headers=headers, json=body, timeout=20,
    )


# ═════════════════════════════════════════════════════════════════════
# NEW ENDPOINT — GET /api/advance-receipts/{id}/allocations
# ═════════════════════════════════════════════════════════════════════

def test_list_allocations_empty_receipt_returns_empty(admin_token):
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=1000)
    r = requests.get(
        f"{API}/advance-receipts/{ar['receipt_id']}/allocations",
        headers=H(admin_token), timeout=10,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["items"] == []
    assert body["count"] == 0
    assert body["total_active_amount"] == 0
    assert body["total_voided_amount"] == 0
    assert body["receipt"]["receipt_id"] == ar["receipt_id"]
    assert body["receipt"]["available_balance"] == 1000.0


def test_list_allocations_after_partial_and_full(admin_token):
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=5000)
    inv1 = _mk_invoice(admin_token, pat, unit_price=3000)
    inv2 = _mk_invoice(admin_token, pat, unit_price=2000)
    a1 = _post_alloc(admin_token, ar["receipt_id"], inv1["invoice_id"], 3000, key=_key())
    a2 = _post_alloc(admin_token, ar["receipt_id"], inv2["invoice_id"], 2000, key=_key())
    assert a1.status_code == 200 and a2.status_code == 200

    r = requests.get(
        f"{API}/advance-receipts/{ar['receipt_id']}/allocations",
        headers=H(admin_token), timeout=10,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert body["total_active_amount"] == 5000.0
    assert body["total_voided_amount"] == 0.0
    assert body["receipt"]["available_balance"] == 0.0
    assert body["receipt"]["allocated_total"] == 5000.0
    seen_invoices = {row["invoice_id"] for row in body["items"]}
    assert inv1["invoice_id"] in seen_invoices
    assert inv2["invoice_id"] in seen_invoices


def test_list_allocations_bogus_receipt_returns_404(admin_token):
    r = requests.get(
        f"{API}/advance-receipts/AR-BOGUS-XYZ123/allocations",
        headers=H(admin_token), timeout=10,
    )
    assert r.status_code == 404, r.text


def test_list_allocations_rbac_readable_by_audiologist(admin_token, audiologist_token):
    """Read-only listing must be accessible to READ_ROLES (audiologist
    is included so it can view the patient audit trail)."""
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=100)
    r = requests.get(
        f"{API}/advance-receipts/{ar['receipt_id']}/allocations",
        headers=H(audiologist_token), timeout=10,
    )
    assert r.status_code == 200, r.text


def test_list_allocations_status_filter(admin_token):
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=1000)
    inv = _mk_invoice(admin_token, pat, unit_price=1000)
    a = _post_alloc(admin_token, ar["receipt_id"], inv["invoice_id"], 500, key=_key())
    assert a.status_code == 200
    r_active = requests.get(
        f"{API}/advance-receipts/{ar['receipt_id']}/allocations?status=active",
        headers=H(admin_token), timeout=10,
    )
    r_voided = requests.get(
        f"{API}/advance-receipts/{ar['receipt_id']}/allocations?status=voided",
        headers=H(admin_token), timeout=10,
    )
    assert r_active.status_code == 200
    assert r_voided.status_code == 200
    assert r_active.json()["count"] == 1
    assert r_voided.json()["count"] == 0


# ═════════════════════════════════════════════════════════════════════
# UI-DRIVEN Phase 2B.2 WRITER RE-VERIFICATION
# (exercises the exact request shape the ApplyAdvanceModal produces)
# ═════════════════════════════════════════════════════════════════════

def test_ui_flow_invoice_total_preserved_after_partial_allocation(admin_token):
    """Modal sends `{invoice_id, amount}` with Idempotency-Key. Invoice
    total MUST stay at the sale value; the allocation appears as a
    payment (not a discount)."""
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=50000)
    inv = _mk_invoice(admin_token, pat, unit_price=180000)
    inv_before = requests.get(
        f"{API}/billing/invoices/{inv['invoice_id']}", headers=H(admin_token), timeout=10,
    ).json()
    grand_before = round(float(inv_before.get("grand_total") or inv_before.get("rounded_total") or 0), 2)

    r = _post_alloc(admin_token, ar["receipt_id"], inv["invoice_id"], 50000, key=_key())
    assert r.status_code == 200, r.text
    body = r.json()

    inv_after = requests.get(
        f"{API}/billing/invoices/{inv['invoice_id']}", headers=H(admin_token), timeout=10,
    ).json()
    grand_after = round(float(inv_after.get("grand_total") or inv_after.get("rounded_total") or 0), 2)

    # Invoice total unchanged.
    assert grand_after == grand_before
    # Paid_total bumped by exactly the allocation amount.
    assert round(float(inv_after.get("paid_total") or 0), 2) == 50000.0
    # Balance due reflects the remaining amount.
    assert round(float(inv_after.get("due_total") or 0), 2) == round(grand_before - 50000, 2)
    # Advance receipt available balance dropped by the allocation.
    assert body["advance_receipt"]["available_balance"] == 0.0
    assert body["advance_receipt"]["received_amount"] == 50000.0  # original amount preserved


def test_ui_flow_full_allocation_flips_invoice_to_paid(admin_token):
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=6000)
    inv = _mk_invoice(admin_token, pat, unit_price=6000)
    r = _post_alloc(admin_token, ar["receipt_id"], inv["invoice_id"], 6000, key=_key())
    assert r.status_code == 200
    assert r.json()["invoice"]["status"] == "paid"
    assert r.json()["invoice"]["due_total"] == 0.0
    # Invoice total is the same as the sale value.
    assert round(float(r.json()["invoice"]["grand_total"] or 0), 2) == 6000.0


def test_ui_flow_over_allocation_of_advance_400(admin_token):
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=1000)
    inv = _mk_invoice(admin_token, pat, unit_price=5000)
    r = _post_alloc(admin_token, ar["receipt_id"], inv["invoice_id"], 2000, key=_key())
    assert r.status_code == 400
    detail = (r.json().get("detail") or "").lower()
    assert "advance" in detail and "available" in detail


def test_ui_flow_over_invoice_outstanding_400(admin_token):
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=10000)
    inv = _mk_invoice(admin_token, pat, unit_price=1000)
    r = _post_alloc(admin_token, ar["receipt_id"], inv["invoice_id"], 3000, key=_key())
    assert r.status_code == 400
    detail = (r.json().get("detail") or "").lower()
    assert "outstanding" in detail or "exceeds" in detail


def test_ui_flow_patient_mismatch_400(admin_token):
    p1 = _mk_patient(admin_token)
    p2 = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, p1, amount=1000)
    inv = _mk_invoice(admin_token, p2, unit_price=1000)  # different patient
    r = _post_alloc(admin_token, ar["receipt_id"], inv["invoice_id"], 100, key=_key())
    assert r.status_code == 400
    assert "patient" in (r.json().get("detail") or "").lower()


def test_ui_flow_tenant_mismatch_404(admin_token):
    """Modal's caller passes a receipt_id — the tenant guard on the
    endpoint returns 404 for cross-tenant probes (never 200)."""
    pat = _mk_patient(admin_token)
    inv = _mk_invoice(admin_token, pat, unit_price=500)
    r = _post_alloc(admin_token, "AR-FROM-DIFFERENT-CLINIC", inv["invoice_id"], 100, key=_key())
    assert r.status_code == 404


def test_ui_flow_multiple_partial_allocations_from_one_advance(admin_token):
    """Advance ₹50,000 split across three invoices."""
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=50000)
    inv_ha = _mk_invoice(admin_token, pat, unit_price=20000)
    inv_em = _mk_invoice(admin_token, pat, unit_price=15000)
    inv_acc = _mk_invoice(admin_token, pat, unit_price=15000)
    r1 = _post_alloc(admin_token, ar["receipt_id"], inv_ha["invoice_id"], 20000, key=_key())
    r2 = _post_alloc(admin_token, ar["receipt_id"], inv_em["invoice_id"], 15000, key=_key())
    r3 = _post_alloc(admin_token, ar["receipt_id"], inv_acc["invoice_id"], 15000, key=_key())
    assert r1.status_code == 200 and r2.status_code == 200 and r3.status_code == 200
    # Ledger endpoint agrees.
    r = requests.get(
        f"{API}/advance-receipts/{ar['receipt_id']}/allocations",
        headers=H(admin_token), timeout=10,
    )
    body = r.json()
    assert body["count"] == 3
    assert body["total_active_amount"] == 50000.0
    assert body["receipt"]["available_balance"] == 0.0


def test_ui_flow_partial_allocation_leaves_remaining_available(admin_token):
    """Advance ₹50,000, invoice ₹30,000 → apply ₹30,000 → ₹20,000
    must remain available for a future sale."""
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=50000)
    inv = _mk_invoice(admin_token, pat, unit_price=30000)
    r = _post_alloc(admin_token, ar["receipt_id"], inv["invoice_id"], 30000, key=_key())
    assert r.status_code == 200
    assert r.json()["advance_receipt"]["available_balance"] == 20000.0
    assert r.json()["advance_receipt"]["allocated_total"] == 30000.0


def test_ui_flow_insufficient_advance_after_prior_use(admin_token):
    """A prior allocation reduces the balance; a follow-up over-use
    is rejected at the CAS boundary."""
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=5000)
    inv_1 = _mk_invoice(admin_token, pat, unit_price=4000)
    inv_2 = _mk_invoice(admin_token, pat, unit_price=5000)
    r1 = _post_alloc(admin_token, ar["receipt_id"], inv_1["invoice_id"], 4000, key=_key())
    assert r1.status_code == 200
    r2 = _post_alloc(admin_token, ar["receipt_id"], inv_2["invoice_id"], 2000, key=_key())
    assert r2.status_code == 400   # only ₹1000 remaining
    assert "advance" in (r2.json().get("detail") or "").lower()


def test_ui_flow_idempotent_retry_via_modal(admin_token):
    """The modal generates a fresh Idempotency-Key per attempt. Same
    key + same body must replay the same response, never double-write."""
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=3000)
    inv = _mk_invoice(admin_token, pat, unit_price=3000)
    k = _key("ui-replay")
    r1 = _post_alloc(admin_token, ar["receipt_id"], inv["invoice_id"], 1500, key=k)
    r2 = _post_alloc(admin_token, ar["receipt_id"], inv["invoice_id"], 1500, key=k)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r2.headers.get("Idempotency-Replay") == "true"
    assert r1.json()["allocation_id"] == r2.json()["allocation_id"]
    fresh = requests.get(
        f"{API}/advance-receipts/{ar['receipt_id']}", headers=H(admin_token), timeout=10,
    ).json()
    assert fresh["available_balance"] == 1500.0
    assert fresh["allocated_total"] == 1500.0


def test_ui_flow_concurrent_allocations_across_two_invoices(admin_token):
    """Two staff members race to apply the same advance to different
    invoices; the total consumed MUST never exceed the balance."""
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=1500)
    inv_a = _mk_invoice(admin_token, pat, unit_price=1500)
    inv_b = _mk_invoice(admin_token, pat, unit_price=1500)

    def _hit(inv_id):
        return _post_alloc(admin_token, ar["receipt_id"], inv_id, 1200, key=_key("cc-ux"))

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        futs = [ex.submit(_hit, inv_a["invoice_id"]),
                ex.submit(_hit, inv_b["invoice_id"])]
        results = [f.result() for f in concurrent.futures.as_completed(futs)]

    codes = sorted(r.status_code for r in results)
    assert codes[0] == 200, f"expected one success; got {codes}"
    assert codes[1] in (400, 409), f"expected one 4xx; got {codes}"

    fresh = requests.get(
        f"{API}/advance-receipts/{ar['receipt_id']}", headers=H(admin_token), timeout=10,
    ).json()
    assert fresh["available_balance"] == 300.0
    assert fresh["allocated_total"] == 1200.0


def test_ui_flow_voided_receipt_cannot_be_applied(admin_token, accounts_token):
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=1000)
    inv = _mk_invoice(admin_token, pat, unit_price=1000)
    v = requests.post(
        f"{API}/advance-receipts/{ar['receipt_id']}/void",
        headers=H(accounts_token), json={"reason": "not needed"}, timeout=10,
    )
    assert v.status_code == 200
    r = _post_alloc(admin_token, ar["receipt_id"], inv["invoice_id"], 100, key=_key())
    assert r.status_code == 409
    assert "voided" in (r.json().get("detail") or "").lower()


def test_ui_flow_fully_applied_receipt_cannot_be_reused(admin_token):
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=1000)
    inv_1 = _mk_invoice(admin_token, pat, unit_price=1000)
    inv_2 = _mk_invoice(admin_token, pat, unit_price=500)
    r1 = _post_alloc(admin_token, ar["receipt_id"], inv_1["invoice_id"], 1000, key=_key())
    assert r1.status_code == 200
    r2 = _post_alloc(admin_token, ar["receipt_id"], inv_2["invoice_id"], 100, key=_key())
    assert r2.status_code == 400   # 0 available
    assert "advance" in (r2.json().get("detail") or "").lower()


def test_ui_flow_rbac_frontdesk_can_apply(admin_token, frontdesk_token):
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=200)
    inv = _mk_invoice(admin_token, pat, unit_price=200)
    r = _post_alloc(frontdesk_token, ar["receipt_id"], inv["invoice_id"], 200, key=_key())
    assert r.status_code == 200


def test_ui_flow_rbac_audiologist_cannot_apply(admin_token, audiologist_token):
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=200)
    inv = _mk_invoice(admin_token, pat, unit_price=200)
    r = _post_alloc(audiologist_token, ar["receipt_id"], inv["invoice_id"], 200, key=_key())
    assert r.status_code == 403


def test_ui_flow_receipt_amount_never_mutated(admin_token):
    """After many allocations, the original `received_amount` MUST
    remain untouched — the advance receipt is an immutable
    acknowledgement, never a discount tally."""
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=10000)
    original_received = ar["received_amount"]
    for amt in (1000, 2000, 3000):
        inv = _mk_invoice(admin_token, pat, unit_price=amt)
        r = _post_alloc(admin_token, ar["receipt_id"], inv["invoice_id"], amt, key=_key())
        assert r.status_code == 200
    fresh = requests.get(
        f"{API}/advance-receipts/{ar['receipt_id']}", headers=H(admin_token), timeout=10,
    ).json()
    assert fresh["received_amount"] == original_received
    assert fresh["received_amount"] == 10000.0
    assert fresh["available_balance"] == 4000.0
    assert fresh["allocated_total"] == 6000.0


def test_ui_flow_payment_row_carries_advance_backlinks(admin_token):
    """The db.payments row generated by the allocation MUST carry
    method='advance', allocation_id, and advance_receipt_id, so the
    audit trail: Advance → Allocation → Invoice → Payment can be
    reconstructed."""
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=2000)
    inv = _mk_invoice(admin_token, pat, unit_price=2000)
    r = _post_alloc(admin_token, ar["receipt_id"], inv["invoice_id"], 1200, key=_key())
    assert r.status_code == 200
    allocation_id = r.json()["allocation_id"]
    payment_id = r.json()["payment_id"]

    inv_fresh = requests.get(
        f"{API}/billing/invoices/{inv['invoice_id']}", headers=H(admin_token), timeout=10,
    ).json()
    rows = [p for p in (inv_fresh.get("payments") or [])
            if p.get("payment_id") == payment_id]
    assert len(rows) == 1
    assert rows[0]["method"] == "advance"
    assert rows[0]["allocation_id"] == allocation_id
    assert rows[0]["advance_receipt_id"] == ar["receipt_id"]
