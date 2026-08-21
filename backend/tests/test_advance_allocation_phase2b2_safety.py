"""Advance Allocation · Phase 2B.2 · PRE-DEPLOYMENT SAFETY corrections.

Two focused corrections proven here:

  1. **Void guard tightening** — the advance-void endpoint now rejects
     voiding a receipt with any active allocations (`$ifNull` +
     `MONEY_TOL` guard). Legacy Phase 2A rows (no `allocated_total`
     field) and unused new rows (`allocated_total == 0`) remain
     voidable. Only receipts with a live `allocated_total > 0` are
     blocked.

  2. **Deterministic payment-failure rollback** — using an in-process
     monkeypatch, we force `record_payment_atomic()` to fail AFTER the
     advance CAS has succeeded, and prove that the compensating
     rollback restores the advance ledger and system-voids the
     allocation stub (no orphan active allocation, no payment row).

Do NOT expand this file beyond those two topics. Phase 2B.3+ builds
allocation-void; Phase 2B.4+ handles UI. This file is the pre-deploy
safety net.
"""
from __future__ import annotations

import os
import random
import string
import sys
import pathlib
import time
import uuid
import asyncio

import pytest
import requests

sys.path.append(str(pathlib.Path(__file__).resolve().parents[1]))
from _helpers import (  # noqa: E402
    API,
    ADMIN_EMAIL, ADMIN_PASSWORD,
    ACCOUNTS_EMAIL, ACCOUNTS_PASSWORD,
    H, login,
)


_BRANCH_ID = "BR-PYTEST-001"


def _uniq() -> str:
    return f"{int(time.time()*1000) % 1_000_000_000:x}{''.join(random.choices(string.ascii_lowercase, k=4))}"


def _key(prefix: str = "aa-safety") -> str:
    return f"{prefix}-{_uniq()}-{uuid.uuid4().hex[:12]}"


def _phone() -> str:
    return f"+91{random.randint(7, 9)}{random.randint(10**8, 10**9 - 1)}"


@pytest.fixture(scope="module")
def admin_token() -> str:
    return login(ADMIN_EMAIL, ADMIN_PASSWORD)


@pytest.fixture(scope="module")
def accounts_token() -> str:
    return login(ACCOUNTS_EMAIL, ACCOUNTS_PASSWORD)


def _mk_patient(token: str) -> str:
    r = requests.post(f"{API}/patients", headers=H(token), json={
        "name": f"AA Safety Patient {_uniq()}",
        "mobile": _phone(),
        "age": 40, "sex": "F", "branch_id": _BRANCH_ID,
    }, timeout=15)
    assert r.status_code in (200, 201), r.text
    return r.json()["patient_id"]


def _mk_invoice(token: str, patient_id: str, *, unit_price: float = 5000.0) -> dict:
    r = requests.post(f"{API}/billing/invoices", headers=H(token), json={
        "patient_id": patient_id,
        "lines": [{
            "description": f"AA safety line {_uniq()}",
            "quantity": 1,
            "unit_price": unit_price,
            "is_taxable": False,
        }],
    }, timeout=15)
    assert r.status_code in (200, 201), r.text
    return r.json()


def _mk_advance(token: str, patient_id: str, *, amount: float = 5000.0) -> dict:
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
# CORRECTION #1 — VOID GUARD
# ═════════════════════════════════════════════════════════════════════

def test_void_unused_active_advance_still_succeeds(admin_token, accounts_token):
    """Unused active advances (no allocations) MUST remain voidable —
    the tightening must not regress the Phase 2A happy path."""
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=750)
    v = requests.post(
        f"{API}/advance-receipts/{ar['receipt_id']}/void",
        headers=H(accounts_token),
        json={"reason": "unused advance void"},
        timeout=10,
    )
    assert v.status_code == 200, v.text
    body = v.json()
    assert body["status"] == "voided"
    assert body["void_reason"] == "unused advance void"


def test_void_partially_allocated_advance_returns_409(admin_token, accounts_token):
    """₹500 advance, ₹200 allocated → void MUST return 409 and mutate
    nothing (advance stays active with the same balance ledger)."""
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
    detail = (v.json().get("detail") or "").lower()
    assert "allocation" in detail
    assert "200" in v.text or "₹200" in v.text  # helpful diagnostic

    fresh = requests.get(
        f"{API}/advance-receipts/{ar['receipt_id']}", headers=H(admin_token), timeout=10,
    ).json()
    assert fresh["status"] == "active"
    assert fresh["available_balance"] == 300.0
    assert fresh["allocated_total"] == 200.0
    assert fresh.get("voided_at") is None
    assert fresh.get("void_reason") is None


def test_void_fully_allocated_advance_returns_409(admin_token, accounts_token):
    """₹1000 advance, ₹1000 fully allocated → void MUST return 409."""
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=1000)
    inv = _mk_invoice(admin_token, pat, unit_price=1000)
    r = _post_alloc(admin_token, ar["receipt_id"], inv["invoice_id"], 1000, key=_key())
    assert r.status_code == 200

    v = requests.post(
        f"{API}/advance-receipts/{ar['receipt_id']}/void",
        headers=H(accounts_token),
        json={"reason": "fully consumed void attempt"},
        timeout=10,
    )
    assert v.status_code == 409, v.text
    fresh = requests.get(
        f"{API}/advance-receipts/{ar['receipt_id']}", headers=H(admin_token), timeout=10,
    ).json()
    assert fresh["status"] == "active"
    assert fresh["available_balance"] == 0.0
    assert fresh["allocated_total"] == 1000.0


def test_failed_void_produces_zero_mutation(admin_token, accounts_token):
    """The 409 rejection MUST be atomic — no partial writes on the
    advance receipt (no voided_at, no void_reason, no status change)."""
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat, amount=800)
    inv = _mk_invoice(admin_token, pat, unit_price=800)
    r = _post_alloc(admin_token, ar["receipt_id"], inv["invoice_id"], 500, key=_key())
    assert r.status_code == 200

    # Fire the (rejected) void.
    v = requests.post(
        f"{API}/advance-receipts/{ar['receipt_id']}/void",
        headers=H(accounts_token),
        json={"reason": "partial reject probe"},
        timeout=10,
    )
    assert v.status_code == 409

    # Verify EVERY field-level invariant.
    fresh = requests.get(
        f"{API}/advance-receipts/{ar['receipt_id']}", headers=H(admin_token), timeout=10,
    ).json()
    assert fresh["status"] == "active"
    assert fresh["available_balance"] == 300.0
    assert fresh["allocated_total"] == 500.0
    assert fresh.get("voided_at") is None
    assert fresh.get("void_reason") is None
    assert fresh.get("voided_by_user_id") is None
    assert fresh.get("voided_by_name") is None


# ═════════════════════════════════════════════════════════════════════
# CORRECTION #2 — DETERMINISTIC PAYMENT-FAILURE ROLLBACK
# ═════════════════════════════════════════════════════════════════════
#
# Strategy: skip the external HTTP round-trip and invoke the async
# router function directly with a real motor db + a fake `Request`
# carrying the Idempotency-Key header. Monkeypatch
# `routers.advance_receipts.record_payment_atomic` to raise a
# deterministic HTTPException AFTER the advance CAS has succeeded.
#
# This uses REAL DB code paths for:
#   * advance-balance CAS decrement,
#   * allocation ledger insert,
#   * compensating rollback (system-void allocation + $inc restore).
# The only stubbed dependency is the payment writer — precisely the
# component whose failure we are simulating.

class _FakeRequest:
    """Minimal duck-typed Request used only by `extract_idempotency_key`
    and `IdempotencyContext.enter` (both read only `.headers.get()`)."""
    def __init__(self, headers: dict):
        self.headers = headers


async def _rollback_body(monkeypatch, admin_token_val):
    """Deterministic rollback assertion body — invoked via
    `asyncio.run(...)` inside a sync test wrapper. Not marked as a
    pytest test itself (leading underscore + not collected)."""
    from fastapi import HTTPException
    from motor.motor_asyncio import AsyncIOMotorClient
    from dotenv import load_dotenv

    load_dotenv("/app/backend/.env")

    from routers import advance_receipts as ar_router
    from models._advance import AdvanceAllocationCreate

    # ── Real DB handle ──
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ["DB_NAME"]]

    pat_id = _mk_patient(admin_token_val)
    ar = _mk_advance(admin_token_val, pat_id, amount=20000)
    inv = _mk_invoice(admin_token_val, pat_id, unit_price=20000)

    pre_ar = await db.advance_receipts.find_one(
        {"receipt_id": ar["receipt_id"]},
        {"_id": 0, "available_balance": 1, "allocated_total": 1},
    )
    assert pre_ar["available_balance"] == 20000.0
    assert pre_ar["allocated_total"] == 0.0

    user = {
        "user_id": "USR-ROLLBACK-TEST",
        "clinic_id": "clinic-pytest-suite",
        "role": "clinic_owner",
        "name": "Rollback Tester",
        "branch_id": _BRANCH_ID,
    }
    fake_key = _key("rollback-inject")
    request = _FakeRequest(headers={"Idempotency-Key": fake_key})
    payload = AdvanceAllocationCreate(invoice_id=inv["invoice_id"], amount=10000.0)

    async def _fail(*args, **kwargs):
        raise HTTPException(
            status_code=502,
            detail="Simulated payment writer failure (test-only)",
        )
    monkeypatch.setattr(ar_router, "record_payment_atomic", _fail)

    with pytest.raises(HTTPException) as exc:
        await ar_router.allocate_advance(
            receipt_id=ar["receipt_id"],
            payload=payload,
            request=request,
            user=user,
            db=db,
        )
    assert exc.value.status_code == 502
    assert "simulated" in str(exc.value.detail).lower()

    post_ar = await db.advance_receipts.find_one(
        {"receipt_id": ar["receipt_id"]},
        {"_id": 0, "status": 1, "available_balance": 1, "allocated_total": 1},
    )
    assert post_ar["status"] == "active"
    assert post_ar["available_balance"] == 20000.0, f"advance not restored: {post_ar}"
    assert post_ar["allocated_total"] == 0.0, f"allocated_total not restored: {post_ar}"

    alloc_rows = await db.advance_allocations.find(
        {"advance_receipt_id": ar["receipt_id"], "clinic_id": user["clinic_id"]},
        {"_id": 0},
    ).to_list(10)
    assert len(alloc_rows) == 1, f"expected exactly one alloc row, got {len(alloc_rows)}"
    row = alloc_rows[0]
    assert row["status"] == "voided"
    assert row["amount"] == 10000.0
    assert row["voided_at"] is not None
    assert "payment" in (row.get("void_reason") or "").lower()
    assert "simulated" in (row.get("void_reason") or "").lower()
    assert row.get("voided_by_user_id") == user["user_id"]
    assert row.get("payment_id") is None

    n_active = await db.advance_allocations.count_documents(
        {"advance_receipt_id": ar["receipt_id"], "status": "active"},
    )
    assert n_active == 0

    n_payments = await db.payments.count_documents(
        {"allocation_id": row["allocation_id"]},
    )
    assert n_payments == 0, f"unexpected payment rows: {n_payments}"
    n_pmts_for_ar = await db.payments.count_documents(
        {"advance_receipt_id": ar["receipt_id"]},
    )
    assert n_pmts_for_ar == 0

    inv_post = await db.invoices.find_one(
        {"invoice_id": inv["invoice_id"]},
        {"_id": 0, "paid_total": 1, "due_total": 1, "status": 1},
    )
    assert round(float(inv_post.get("paid_total") or 0), 2) == 0.0
    assert inv_post["status"] in {"draft", "partial"}
    inv_full = await db.invoices.find_one(
        {"invoice_id": inv["invoice_id"]}, {"_id": 0, "payments": 1},
    )
    embedded = [p for p in (inv_full.get("payments") or [])
                if p.get("allocation_id") == row["allocation_id"]]
    assert embedded == [], f"unexpected embedded payment: {embedded}"

    idem_row = await db.idempotency_keys.find_one(
        {"clinic_id": user["clinic_id"], "scope": "advance_allocation",
         "idempotency_key": fake_key},
        {"_id": 0, "status": 1, "http_status": 1},
    )
    assert idem_row is not None
    assert idem_row["status"] == "failed"
    assert idem_row["http_status"] == 502


def test_deterministic_rollback_on_payment_failure(monkeypatch, admin_token):
    """Sync wrapper — pytest-asyncio is not installed on this project,
    so we run the async assertion body via `asyncio.run`. `monkeypatch`
    is a sync fixture; its setattr survives across the awaited body."""
    asyncio.run(_rollback_body(monkeypatch, admin_token))
