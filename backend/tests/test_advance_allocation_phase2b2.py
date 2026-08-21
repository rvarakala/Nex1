"""Advance Allocation · Phase 2B.2 · CORE ALLOCATION WRITER regression suite.

Strict scope for this file:
    * Prove the `POST /api/advance-receipts/{receipt_id}/allocations`
      endpoint satisfies every P0 invariant from the approved
      architecture (see `/app/memory/ADVANCE_ALLOCATION_PHASE1_AUDIT.md`
      §7 and the user's Phase 2B.2 authorization).
    * Cover every explicit test topic the user listed
      (successful / partial / full / over-allocation / rejected states
      / concurrency / idempotency / RBAC / consistency / tenant
      isolation / multiple allocations / rollback).

Do NOT expand this file with allocation-void, refund, or UI coverage —
those belong to Phase 2B.3+.

The suite is HERMETIC: every test creates its own patient / advance /
invoice via the public HTTP API, using the seeded pytest tenant. No
manual DB seeding.
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
_CLINIC_ID = os.environ.get("TEST_CLINIC_ID", "clinic-pytest-suite")


def _uniq() -> str:
    return f"{int(time.time()*1000) % 1_000_000_000:x}{''.join(random.choices(string.ascii_lowercase, k=4))}"


def _key(prefix: str = "aa-idem") -> str:
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


# ─────────────────────────────────────────────────────────────────────
# HTTP setup helpers — create patient / invoice / advance / allocation
# ─────────────────────────────────────────────────────────────────────

def _mk_patient(token: str) -> str:
    r = requests.post(f"{API}/patients", headers=H(token), json={
        "name": f"AA Patient {_uniq()}",
        "mobile": _phone(),
        "age": 40, "sex": "F", "branch_id": _BRANCH_ID,
    }, timeout=15)
    assert r.status_code in (200, 201), r.text
    return r.json()["patient_id"]


def _mk_invoice(token: str, patient_id: str, *, unit_price: float = 5000.0) -> dict:
    """Create a bare-minimum single-line invoice. Returns full invoice
    dict (invoice_id, invoice_no, grand_total, due_total, status...).
    """
    r = requests.post(f"{API}/billing/invoices", headers=H(token), json={
        "patient_id": patient_id,
        "lines": [{
            "description": f"AA test line {_uniq()}",
            "quantity": 1,
            "unit_price": unit_price,
            "is_taxable": False,
        }],
    }, timeout=15)
    assert r.status_code in (200, 201), r.text
    return r.json()


def _mk_advance(token: str, patient_id: str, *, amount: float = 5000.0,
                method: str = "cash") -> dict:
    r = requests.post(
        f"{API}/advance-receipts",
        headers={**H(token), "Idempotency-Key": _key("ar")},
        json={"patient_id": patient_id, "received_amount": amount, "method": method},
        timeout=15,
    )
    assert r.status_code == 200, r.text
    return r.json()


def _post_alloc(token: str, receipt_id: str, invoice_id: str, amount: float,
                *, key: str | None = None, note: str | None = None) -> requests.Response:
    body: dict = {"invoice_id": invoice_id, "amount": amount}
    if note:
        body["note"] = note
    headers = H(token)
    if key is not None:
        headers["Idempotency-Key"] = key
    return requests.post(
        f"{API}/advance-receipts/{receipt_id}/allocations",
        headers=headers, json=body, timeout=20,
    )


# ─────────────────────────────────────────────────────────────────────
# 1) NEW-RECEIPT BALANCE INITIALIZATION (Phase 2B.2 side-effect)
# ─────────────────────────────────────────────────────────────────────

def test_new_advance_receipt_initializes_balance_ledger(admin_token):
    """CREATE endpoint MUST now stamp `available_balance = received_amount`
    and `allocated_total = 0.0`. Legacy Phase 2A rows retain None."""
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=1234.0)
    assert ar.get("available_balance") == 1234.0
    assert ar.get("allocated_total") == 0.0


# ─────────────────────────────────────────────────────────────────────
# 2) IDEMPOTENCY-KEY GATE
# ─────────────────────────────────────────────────────────────────────

def test_allocate_missing_idempotency_key_400(admin_token):
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=1000)
    inv = _mk_invoice(admin_token, pat, unit_price=500)
    r = _post_alloc(admin_token, ar["receipt_id"], inv["invoice_id"], 200)  # no key
    assert r.status_code == 400, r.text
    assert "idempotency-key" in r.text.lower()


def test_allocate_malformed_idempotency_key_400(admin_token):
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=1000)
    inv = _mk_invoice(admin_token, pat, unit_price=500)
    for bad in ("short", "x" * 129, "space in key"):
        r = _post_alloc(admin_token, ar["receipt_id"], inv["invoice_id"], 200, key=bad)
        assert r.status_code == 400, f"bad key {bad!r} → {r.status_code}: {r.text[:200]}"


# ─────────────────────────────────────────────────────────────────────
# 3) SUCCESSFUL ALLOCATION (partial + full + multiple)
# ─────────────────────────────────────────────────────────────────────

def test_allocation_full_happy_path(admin_token):
    """Full-value allocation clears both the advance and the invoice due."""
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=3000)
    inv = _mk_invoice(admin_token, pat, unit_price=3000)
    r = _post_alloc(admin_token, ar["receipt_id"], inv["invoice_id"], 3000, key=_key())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["allocation_id"].startswith("AA-")
    assert body["allocation_no"].startswith("AA/")
    assert body["amount"] == 3000.0
    assert body["status"] == "active"
    assert body["payment_id"].startswith("PAY-")
    assert body["advance_receipt"]["available_balance"] == 0.0
    assert body["advance_receipt"]["allocated_total"] == 3000.0
    assert body["invoice"]["status"] == "paid"
    assert body["invoice"]["due_total"] == 0.0
    assert body["invoice"]["paid_total"] == 3000.0


def test_allocation_partial_amount(admin_token):
    """Partial allocation leaves both advance and invoice with balances."""
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=5000)
    inv = _mk_invoice(admin_token, pat, unit_price=5000)
    r = _post_alloc(admin_token, ar["receipt_id"], inv["invoice_id"], 2000, key=_key())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["advance_receipt"]["available_balance"] == 3000.0
    assert body["advance_receipt"]["allocated_total"] == 2000.0
    assert body["invoice"]["status"] == "partial"
    assert body["invoice"]["due_total"] == 3000.0
    assert body["invoice"]["paid_total"] == 2000.0


def test_multiple_allocations_from_one_advance(admin_token):
    """One advance can be split across multiple invoices; balances add up."""
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=10000)
    inv1 = _mk_invoice(admin_token, pat, unit_price=4000)
    inv2 = _mk_invoice(admin_token, pat, unit_price=6000)
    r1 = _post_alloc(admin_token, ar["receipt_id"], inv1["invoice_id"], 4000, key=_key())
    r2 = _post_alloc(admin_token, ar["receipt_id"], inv2["invoice_id"], 6000, key=_key())
    assert r1.status_code == 200 and r2.status_code == 200
    # Final balance zeroed.
    final = requests.get(
        f"{API}/advance-receipts/{ar['receipt_id']}", headers=H(admin_token), timeout=10,
    ).json()
    assert final["available_balance"] == 0.0
    assert final["allocated_total"] == 10000.0


def test_multiple_advances_against_one_invoice(admin_token):
    """One invoice can be settled by allocations from multiple advances."""
    pat = _mk_patient(admin_token)
    ar1 = _mk_advance(admin_token, pat, amount=3000)
    ar2 = _mk_advance(admin_token, pat, amount=2000)
    inv = _mk_invoice(admin_token, pat, unit_price=5000)
    r1 = _post_alloc(admin_token, ar1["receipt_id"], inv["invoice_id"], 3000, key=_key())
    r2 = _post_alloc(admin_token, ar2["receipt_id"], inv["invoice_id"], 2000, key=_key())
    assert r1.status_code == 200 and r2.status_code == 200
    assert r2.json()["invoice"]["status"] == "paid"
    assert r2.json()["invoice"]["due_total"] == 0.0


# ─────────────────────────────────────────────────────────────────────
# 4) REJECTION MATRIX
# ─────────────────────────────────────────────────────────────────────

def test_over_allocation_of_advance_rejected(admin_token):
    """Amount > advance available_balance → 400 fast pre-check."""
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=1000)
    inv = _mk_invoice(admin_token, pat, unit_price=5000)  # invoice has room
    r = _post_alloc(admin_token, ar["receipt_id"], inv["invoice_id"], 2000, key=_key())
    assert r.status_code == 400, r.text
    detail = (r.json().get("detail") or "").lower()
    assert "advance" in detail and "available" in detail
    # Advance balance untouched.
    fresh = requests.get(
        f"{API}/advance-receipts/{ar['receipt_id']}", headers=H(admin_token), timeout=10,
    ).json()
    assert fresh["available_balance"] == 1000.0
    assert fresh["allocated_total"] == 0.0


def test_over_invoice_outstanding_rejected(admin_token):
    """Amount > invoice outstanding → 400 fast pre-check; no side-effects."""
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=5000)
    inv = _mk_invoice(admin_token, pat, unit_price=1000)  # invoice only has ₹1000 due
    r = _post_alloc(admin_token, ar["receipt_id"], inv["invoice_id"], 3000, key=_key())
    assert r.status_code == 400, r.text
    detail = (r.json().get("detail") or "").lower()
    assert "outstanding" in detail or "exceeds" in detail
    # Advance untouched.
    fresh = requests.get(
        f"{API}/advance-receipts/{ar['receipt_id']}", headers=H(admin_token), timeout=10,
    ).json()
    assert fresh["available_balance"] == 5000.0


def test_zero_or_negative_amount_rejected(admin_token):
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=1000)
    inv = _mk_invoice(admin_token, pat, unit_price=1000)
    for bad in (0, -1, -0.01):
        r = _post_alloc(admin_token, ar["receipt_id"], inv["invoice_id"], bad, key=_key())
        assert r.status_code == 422, f"amount={bad} → {r.status_code}"


def test_voided_advance_rejected(admin_token, accounts_token):
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=1000)
    inv = _mk_invoice(admin_token, pat, unit_price=1000)
    # Void the advance.
    v = requests.post(
        f"{API}/advance-receipts/{ar['receipt_id']}/void",
        headers=H(accounts_token),
        json={"reason": "duplicate entry"},
        timeout=10,
    )
    assert v.status_code == 200
    # Attempt allocation.
    r = _post_alloc(admin_token, ar["receipt_id"], inv["invoice_id"], 100, key=_key())
    assert r.status_code == 409, r.text
    assert "voided" in (r.json().get("detail") or "").lower()


def test_cancelled_invoice_rejected(admin_token, accounts_token):
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=1000)
    inv = _mk_invoice(admin_token, pat, unit_price=1000)
    # Cancel the invoice (requires accounts/super_admin role and a
    # JSON body — even an empty dict).
    c = requests.post(
        f"{API}/billing/invoices/{inv['invoice_id']}/cancel",
        headers=H(accounts_token), json={"reason": "test cancel"}, timeout=10,
    )
    assert c.status_code == 200, c.text
    r = _post_alloc(admin_token, ar["receipt_id"], inv["invoice_id"], 100, key=_key())
    assert r.status_code == 400, r.text
    assert "cancelled" in (r.json().get("detail") or "").lower()


def test_refunded_invoice_rejected(admin_token):
    """Fully-refunded invoice → 400; the pre-existing NAV-012 F-15
    guard is honoured through the allocation pipeline too."""
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=1000)
    inv = _mk_invoice(admin_token, pat, unit_price=1000)
    # Pay via a NORMAL payment (not allocation) so the invoice becomes
    # paid, then refund it.
    p = requests.post(
        f"{API}/billing/invoices/{inv['invoice_id']}/payments",
        headers=H(admin_token),
        json={"method": "cash", "amount": 1000},
        timeout=15,
    )
    assert p.status_code == 200, p.text
    ref = requests.post(
        f"{API}/billing/invoices/{inv['invoice_id']}/refund",
        headers=H(admin_token),
        json={"method": "cash", "amount": 1000, "reason": "test refund"},
        timeout=15,
    )
    assert ref.status_code == 200, ref.text
    # Now try to allocate.
    r = _post_alloc(admin_token, ar["receipt_id"], inv["invoice_id"], 100, key=_key())
    assert r.status_code == 400, r.text
    detail = (r.json().get("detail") or "").lower()
    assert "refunded" in detail or "cancelled" in detail


def test_cross_patient_rejected(admin_token):
    pat_a = _mk_patient(admin_token)
    pat_b = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat_a, amount=1000)
    inv_b = _mk_invoice(admin_token, pat_b, unit_price=1000)  # different patient
    r = _post_alloc(admin_token, ar["receipt_id"], inv_b["invoice_id"], 100, key=_key())
    assert r.status_code == 400, r.text
    assert "patient" in (r.json().get("detail") or "").lower()


def test_missing_advance_receipt_404(admin_token):
    pat = _mk_patient(admin_token)
    inv = _mk_invoice(admin_token, pat, unit_price=1000)
    r = _post_alloc(admin_token, "AR-DOESNOTEXIST", inv["invoice_id"], 100, key=_key())
    assert r.status_code == 404, r.text


def test_missing_invoice_404(admin_token):
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=1000)
    r = _post_alloc(admin_token, ar["receipt_id"], "INV-DOESNOTEXIST", 100, key=_key())
    assert r.status_code == 404, r.text


# ─────────────────────────────────────────────────────────────────────
# 5) IDEMPOTENT REPLAY + PAYLOAD-MISMATCH
# ─────────────────────────────────────────────────────────────────────

def test_idempotent_replay_returns_cached_body(admin_token):
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=1000)
    inv = _mk_invoice(admin_token, pat, unit_price=1000)
    k = _key("replay")
    r1 = _post_alloc(admin_token, ar["receipt_id"], inv["invoice_id"], 500, key=k)
    r2 = _post_alloc(admin_token, ar["receipt_id"], inv["invoice_id"], 500, key=k)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r2.headers.get("Idempotency-Replay") == "true"
    assert r1.json()["allocation_id"] == r2.json()["allocation_id"]
    assert r1.json()["payment_id"] == r2.json()["payment_id"]
    # No double-decrement — advance shows a single ₹500 consumption.
    fresh = requests.get(
        f"{API}/advance-receipts/{ar['receipt_id']}", headers=H(admin_token), timeout=10,
    ).json()
    assert fresh["available_balance"] == 500.0
    assert fresh["allocated_total"] == 500.0


def test_same_key_different_payload_rejected_422(admin_token):
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=1000)
    inv = _mk_invoice(admin_token, pat, unit_price=1000)
    k = _key("mismatch")
    r1 = _post_alloc(admin_token, ar["receipt_id"], inv["invoice_id"], 500, key=k)
    assert r1.status_code == 200, r1.text
    r2 = _post_alloc(admin_token, ar["receipt_id"], inv["invoice_id"], 700, key=k)
    assert r2.status_code == 422, r2.text
    assert "different payload" in (r2.json().get("detail") or "").lower()


# ─────────────────────────────────────────────────────────────────────
# 6) ATOMICITY — CONCURRENT ALLOCATIONS
# ─────────────────────────────────────────────────────────────────────

def test_concurrent_allocations_never_over_consume(admin_token):
    """Two concurrent allocations against the SAME advance for MORE
    than half the balance each: only one can win, ledger stays sane.
    """
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=2000)
    inv1 = _mk_invoice(admin_token, pat, unit_price=2000)
    inv2 = _mk_invoice(admin_token, pat, unit_price=2000)

    def _hit(inv_id: str):
        return _post_alloc(admin_token, ar["receipt_id"], inv_id, 1500, key=_key("cc"))

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        futs = [ex.submit(_hit, inv1["invoice_id"]),
                ex.submit(_hit, inv2["invoice_id"])]
        results = [f.result() for f in concurrent.futures.as_completed(futs)]

    codes = sorted(r.status_code for r in results)
    # Exactly one 200 and one 4xx (either 400 pre-check or 409 CAS-loser).
    assert codes[0] == 200, f"expected one success; got {codes}"
    assert codes[1] in (400, 409), f"expected one 4xx; got {codes}"

    fresh = requests.get(
        f"{API}/advance-receipts/{ar['receipt_id']}", headers=H(admin_token), timeout=10,
    ).json()
    # Winner consumed 1500; loser did not touch the ledger.
    assert fresh["available_balance"] == 500.0
    assert fresh["allocated_total"] == 1500.0


# ─────────────────────────────────────────────────────────────────────
# 7) PAYMENT / ALLOCATION CONSISTENCY (invariants I3 + I5 + I6)
# ─────────────────────────────────────────────────────────────────────

def test_payment_allocation_ledger_consistency(admin_token):
    """After a successful allocation:
      * exactly one embedded invoice.payments[] row with method='advance'
        + matching allocation_id + advance_receipt_id back-links
      * advance available_balance + allocated_total == received_amount
      * `allocation_id` is recoverable from the embedded row (the
        `/billing/payments` LIST endpoint projects a slimmer view, so
        we assert against the invoice document — the source of truth
        for the embedded payment array).
    """
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=2000)
    inv = _mk_invoice(admin_token, pat, unit_price=2000)
    r = _post_alloc(admin_token, ar["receipt_id"], inv["invoice_id"], 1200, key=_key())
    assert r.status_code == 200, r.text
    payment_id = r.json()["payment_id"]
    allocation_id = r.json()["allocation_id"]

    # I1 + I6: advance balance ledger sums to received_amount, no negatives.
    fresh_ar = requests.get(
        f"{API}/advance-receipts/{ar['receipt_id']}", headers=H(admin_token), timeout=10,
    ).json()
    assert round(fresh_ar["available_balance"] + fresh_ar["allocated_total"], 2) == 2000.0
    assert fresh_ar["available_balance"] >= 0.0

    # I3: embedded invoice.payments[] carries exactly one row for this
    # allocation, with method='advance' and matching back-links.
    inv_fresh = requests.get(
        f"{API}/billing/invoices/{inv['invoice_id']}", headers=H(admin_token), timeout=10,
    ).json()
    rows = [p for p in (inv_fresh.get("payments") or [])
            if p.get("payment_id") == payment_id]
    assert len(rows) == 1, f"expected exactly one embedded payment row, got {len(rows)}"
    row = rows[0]
    assert row.get("method") == "advance"
    assert row.get("allocation_id") == allocation_id
    assert row.get("advance_receipt_id") == ar["receipt_id"]
    assert round(float(row.get("amount") or 0), 2) == 1200.0
    # I5: invoice paid_total increased by exactly the allocation amount.
    assert round(float(inv_fresh.get("paid_total") or 0), 2) == 1200.0


# ─────────────────────────────────────────────────────────────────────
# 8) TENANT / OWNERSHIP ISOLATION
# ─────────────────────────────────────────────────────────────────────

def test_cross_tenant_receipt_returns_404(admin_token):
    """Founder is the only role able to hop tenants; a regular admin
    from clinic A must not be able to allocate against clinic B's
    receipt. Simulate via a bogus/foreign receipt_id → 404 (the query
    is clinic-scoped)."""
    pat = _mk_patient(admin_token)
    inv = _mk_invoice(admin_token, pat, unit_price=500)
    r = _post_alloc(admin_token, "AR-FROM-OTHER-CLINIC-XYZ", inv["invoice_id"],
                    100, key=_key())
    assert r.status_code == 404, r.text


# ─────────────────────────────────────────────────────────────────────
# 9) RBAC DENIAL
# ─────────────────────────────────────────────────────────────────────

def test_audiologist_denied_403(admin_token, audiologist_token):
    """Audiologist has READ-only access to advances — they must NOT
    be able to allocate."""
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=1000)
    inv = _mk_invoice(admin_token, pat, unit_price=1000)
    r = _post_alloc(audiologist_token, ar["receipt_id"], inv["invoice_id"],
                    100, key=_key())
    assert r.status_code == 403, r.text


def test_frontdesk_allowed(admin_token, frontdesk_token):
    """Front-desk staff CAN allocate (matches the payment RBAC)."""
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=500)
    inv = _mk_invoice(admin_token, pat, unit_price=500)
    r = _post_alloc(frontdesk_token, ar["receipt_id"], inv["invoice_id"],
                    500, key=_key())
    assert r.status_code == 200, r.text


def test_accounts_allowed(admin_token, accounts_token):
    """Accounts role can allocate."""
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=500)
    inv = _mk_invoice(admin_token, pat, unit_price=500)
    r = _post_alloc(accounts_token, ar["receipt_id"], inv["invoice_id"],
                    500, key=_key())
    assert r.status_code == 200, r.text


# ─────────────────────────────────────────────────────────────────────
# 10) ROLLBACK SAFETY
# ─────────────────────────────────────────────────────────────────────

def test_rollback_on_missing_invoice_leaves_advance_intact(admin_token):
    """A 404 on the invoice pre-check MUST NOT decrement the advance —
    the CAS on the advance only runs AFTER the invoice checks pass."""
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=1234)
    r = _post_alloc(admin_token, ar["receipt_id"], "INV-NOPE", 100, key=_key())
    assert r.status_code == 404
    fresh = requests.get(
        f"{API}/advance-receipts/{ar['receipt_id']}", headers=H(admin_token), timeout=10,
    ).json()
    assert fresh["available_balance"] == 1234.0
    assert fresh["allocated_total"] == 0.0


def test_rollback_on_cancelled_invoice_leaves_advance_intact(admin_token, accounts_token):
    """A 400 on the invoice-status guard MUST NOT decrement the advance."""
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=999)
    inv = _mk_invoice(admin_token, pat, unit_price=999)
    c = requests.post(
        f"{API}/billing/invoices/{inv['invoice_id']}/cancel",
        headers=H(accounts_token), json={"reason": "rollback probe"}, timeout=10,
    )
    assert c.status_code == 200
    r = _post_alloc(admin_token, ar["receipt_id"], inv["invoice_id"], 100, key=_key())
    assert r.status_code == 400
    fresh = requests.get(
        f"{API}/advance-receipts/{ar['receipt_id']}", headers=H(admin_token), timeout=10,
    ).json()
    assert fresh["available_balance"] == 999.0
    assert fresh["allocated_total"] == 0.0


# ─────────────────────────────────────────────────────────────────────
# 11) Phase 2B.2 · Safety-guard tightening — the void endpoint now
#     REJECTS voiding an advance that has active allocations. Regression
#     locks in the corrected behaviour.
# ─────────────────────────────────────────────────────────────────────

def test_void_after_partial_allocation_now_blocked_409(admin_token, accounts_token):
    """After the Phase 2B.2 safety correction: voiding an advance with
    a live allocation MUST fail with 409 and mutate nothing."""
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=500)
    inv = _mk_invoice(admin_token, pat, unit_price=500)
    r = _post_alloc(admin_token, ar["receipt_id"], inv["invoice_id"], 200, key=_key())
    assert r.status_code == 200
    v = requests.post(
        f"{API}/advance-receipts/{ar['receipt_id']}/void",
        headers=H(accounts_token),
        json={"reason": "should be blocked"},
        timeout=10,
    )
    assert v.status_code == 409, v.text
    assert "allocation" in (v.json().get("detail") or "").lower()
    # Advance stayed active with the correct partially-allocated ledger.
    fresh = requests.get(
        f"{API}/advance-receipts/{ar['receipt_id']}", headers=H(admin_token), timeout=10,
    ).json()
    assert fresh["status"] == "active"
    assert fresh["available_balance"] == 300.0
    assert fresh["allocated_total"] == 200.0
