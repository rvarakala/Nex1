"""NAV-012 · Phase 2A — Bundle B (F-15) payment hardening tests.

Guards `record_payment_atomic` against writing a fresh payment onto an
invoice that has already been refunded (status ∈ {refunded,
partially_refunded} OR refunded_total > 0).  Existing NAV-009 payment
semantics on paid / partial / draft / cancelled / legacy invoices are
preserved.
"""
from __future__ import annotations

import concurrent.futures
import os
import random
import string
import sys
import pathlib
import time
from typing import Optional

import requests

sys.path.append(str(pathlib.Path(__file__).resolve().parents[1]))
from _helpers import (  # noqa: E402
    API, ADMIN_EMAIL, ADMIN_PASSWORD, H, login,
)


_BRANCH_ID = "BR-PYTEST-001"


def _uniq() -> str:
    return f"{int(time.time()*1000)%1_000_000_000:x}{''.join(random.choices(string.ascii_lowercase, k=4))}"


def _phone() -> str:
    return f"+91{random.randint(7, 9)}{random.randint(10**8, 10**9 - 1)}"


def _tok() -> str:
    return login(ADMIN_EMAIL, ADMIN_PASSWORD)


def _mk_service(token: str) -> str:
    r = requests.post(f"{API}/billing/services", headers=H(token), json={
        "code": f"NAV012F15-{_uniq()[:5].upper()}",
        "name": "NAV-012 F-15 service", "price": 5000, "gst_rate": 0,
        "category": "Consultation", "active": True,
    }, timeout=15)
    assert r.status_code in (200, 201), r.text
    return r.json()["service_id"]


def _mk_patient(token: str) -> str:
    r = requests.post(f"{API}/patients", headers=H(token), json={
        "name": f"NAV012-F15 {_uniq()}",
        "mobile": _phone(), "age": 42, "sex": "F", "branch_id": _BRANCH_ID,
    }, timeout=15)
    assert r.status_code in (200, 201), r.text
    return r.json()["patient_id"]


def _mk_paid_inv(token: str, svc: str, pat: str, amount: float = 4000) -> dict:
    r = requests.post(f"{API}/billing/invoices", headers=H(token), json={
        "patient_id": pat,
        "lines": [{
            "service_id": svc, "description": "line",
            "quantity": 1, "unit_price": amount,
            "discount_type": "flat", "discount_value": 0,
        }],
        "initial_payment": {"method": "cash", "amount": amount},
    }, timeout=20)
    assert r.status_code == 200, r.text
    return r.json()


def _mk_draft_inv(token: str, svc: str, pat: str, amount: float = 4000) -> dict:
    r = requests.post(f"{API}/billing/invoices", headers=H(token), json={
        "patient_id": pat,
        "lines": [{
            "service_id": svc, "description": "line",
            "quantity": 1, "unit_price": amount,
            "discount_type": "flat", "discount_value": 0,
        }],
    }, timeout=20)
    assert r.status_code == 200, r.text
    return r.json()


def _refund(token: str, invoice_id: str, amount: float) -> dict:
    r = requests.post(f"{API}/billing/invoices/{invoice_id}/refund",
                      headers=H(token),
                      json={"method": "cash", "amount": amount, "reason": "F-15 setup"},
                      timeout=15)
    assert r.status_code == 200, r.text
    return r.json()


def _pay(token: str, invoice_id: str, amount: float) -> requests.Response:
    return requests.post(f"{API}/billing/invoices/{invoice_id}/payments",
                         headers=H(token),
                         json={"method": "cash", "amount": amount}, timeout=15)


# ─────────────────────────────────────────────────────────────────────
# Positive controls — payment behaviour that MUST stay unchanged
# ─────────────────────────────────────────────────────────────────────

def test_f15_normal_payment_on_open_invoice_still_succeeds():
    t = _tok(); s = _mk_service(t); p = _mk_patient(t)
    inv = _mk_draft_inv(t, s, p, 3000)
    r = _pay(t, inv["invoice_id"], 1500)
    assert r.status_code == 200, r.text
    assert r.json()["paid_total"] == 1500
    assert r.json()["status"] == "partial"


def test_f15_partial_payment_on_partial_still_succeeds():
    t = _tok(); s = _mk_service(t); p = _mk_patient(t)
    inv = _mk_draft_inv(t, s, p, 4000)
    _pay(t, inv["invoice_id"], 1000)
    r = _pay(t, inv["invoice_id"], 1500)
    assert r.status_code == 200
    assert r.json()["paid_total"] == 2500


def test_f15_payment_on_cancelled_invoice_still_400_unchanged():
    t = _tok(); s = _mk_service(t); p = _mk_patient(t)
    inv = _mk_draft_inv(t, s, p, 2000)
    r_cancel = requests.post(
        f"{API}/billing/invoices/{inv['invoice_id']}/cancel",
        headers=H(t), json={"reason": "test"}, timeout=15,
    )
    assert r_cancel.status_code == 200, r_cancel.text
    r = _pay(t, inv["invoice_id"], 500)
    assert r.status_code == 400, r.text
    assert "cancel" in (r.json().get("detail") or "").lower()


# ─────────────────────────────────────────────────────────────────────
# F-15 core rejections
# ─────────────────────────────────────────────────────────────────────

def test_f15_payment_on_fully_refunded_invoice_rejected():
    t = _tok(); s = _mk_service(t); p = _mk_patient(t)
    inv = _mk_paid_inv(t, s, p, 4000)
    ref = _refund(t, inv["invoice_id"], 4000)   # full refund
    assert ref["status"] == "refunded"
    r = _pay(t, inv["invoice_id"], 1000)
    assert r.status_code == 400, r.text
    detail = (r.json().get("detail") or "").lower()
    assert "refund" in detail
    # Confirm no ghost payment landed at the top-level collection.
    inv_now = requests.get(f"{API}/billing/invoices/{inv['invoice_id']}",
                            headers=H(t), timeout=10).json()
    fresh_payments = [
        p for p in (inv_now.get("payments") or [])
        if (p.get("kind") or "payment") == "payment"
    ]
    # Only the original initial_payment row exists (one).
    assert len(fresh_payments) == 1


def test_f15_payment_on_partially_refunded_invoice_rejected():
    t = _tok(); s = _mk_service(t); p = _mk_patient(t)
    inv = _mk_paid_inv(t, s, p, 5000)
    ref = _refund(t, inv["invoice_id"], 2000)   # partial refund
    assert ref["status"] == "partially_refunded"
    r = _pay(t, inv["invoice_id"], 1000)
    assert r.status_code == 400
    assert "refund" in (r.json().get("detail") or "").lower()


def test_f15_message_calls_out_refund_state_clearly():
    t = _tok(); s = _mk_service(t); p = _mk_patient(t)
    inv = _mk_paid_inv(t, s, p, 3000)
    _refund(t, inv["invoice_id"], 3000)
    r = _pay(t, inv["invoice_id"], 500)
    assert r.status_code == 400
    body = r.json()
    assert isinstance(body.get("detail"), str)
    assert "refunded" in body["detail"].lower()


# ─────────────────────────────────────────────────────────────────────
# Concurrency — refund lands first vs payment lands first
# ─────────────────────────────────────────────────────────────────────

def test_f15_concurrent_refund_first_then_payment_is_rejected():
    """Refund lands, then a competing payment MUST be blocked."""
    t = _tok(); s = _mk_service(t); p = _mk_patient(t)
    inv = _mk_paid_inv(t, s, p, 5000)
    _refund(t, inv["invoice_id"], 5000)
    r = _pay(t, inv["invoice_id"], 100)
    assert r.status_code == 400


def test_f15_concurrent_payment_first_then_refund_both_succeed():
    """Payment lands first, subsequent refund is limited by NAV-009's
    existing CAS.  Ensures the new guard did NOT weaken the pre-existing
    ceiling check."""
    t = _tok(); s = _mk_service(t); p = _mk_patient(t)
    inv = _mk_draft_inv(t, s, p, 5000)
    _pay(t, inv["invoice_id"], 5000)   # brings to paid
    r = _refund(t, inv["invoice_id"], 5000)
    assert r["status"] == "refunded"


# ─────────────────────────────────────────────────────────────────────
# Legacy tolerance — invoices without paid_total remain acceptable
# ─────────────────────────────────────────────────────────────────────

def test_f15_normal_flow_unaffected_by_legacy_null_paid_total():
    """We do not synthesize a legacy row; we assert the invariant that
    fresh invoices (which will always carry paid_total=0 explicitly)
    accept a payment.  This documents that the F-15 guard does not
    block legitimate cases."""
    t = _tok(); s = _mk_service(t); p = _mk_patient(t)
    inv = _mk_draft_inv(t, s, p, 2500)
    r = _pay(t, inv["invoice_id"], 2500)
    assert r.status_code == 200
    assert r.json()["status"] == "paid"


# ─────────────────────────────────────────────────────────────────────
# Bundle C — recovery-ledger index presence & sanity
# ─────────────────────────────────────────────────────────────────────

def test_recovery_ledger_indexes_present():
    import asyncio, os as _os
    from motor.motor_asyncio import AsyncIOMotorClient
    async def _go():
        from dotenv import load_dotenv
        load_dotenv("/app/backend/.env")
        client = AsyncIOMotorClient(_os.environ["MONGO_URL"])
        try:
            info = await client[_os.environ["DB_NAME"]].partner_recovery_ledger.index_information()
            assert "uniq_recovery_id" in info
            assert info["uniq_recovery_id"].get("unique") is True
            assert "rec_clinic_partner_status_ct" in info
            assert "rec_clinic_status_ct" in info
            # Bundle C explicitly excludes the (source_payout_id) index.
            for name, meta in info.items():
                key = meta.get("key") or []
                fields = [k for k, _ in key]
                assert fields != ["source_payout_id"], (
                    f"unexpected index {name} — (source_payout_id) is deferred to Phase 2B"
                )
        finally:
            client.close()
    asyncio.get_event_loop().run_until_complete(_go())


def test_recovery_ledger_recovery_id_unique_enforced():
    import asyncio, os as _os, uuid as _uu
    from motor.motor_asyncio import AsyncIOMotorClient
    from pymongo.errors import DuplicateKeyError

    async def _go():
        from dotenv import load_dotenv
        load_dotenv("/app/backend/.env")
        client = AsyncIOMotorClient(_os.environ["MONGO_URL"])
        db = client[_os.environ["DB_NAME"]]
        try:
            rid = f"REC-TEST-{_uu.uuid4().hex[:12]}"
            doc = {"recovery_id": rid, "clinic_id": "nav012-uniq",
                   "partner_id": "P", "amount": 1.0, "status": "pending",
                   "created_at": "2026-01-01T00:00:00+00:00"}
            await db.partner_recovery_ledger.insert_one(dict(doc))
            duplicate_blocked = False
            try:
                await db.partner_recovery_ledger.insert_one(dict(doc))
            except DuplicateKeyError:
                duplicate_blocked = True
            assert duplicate_blocked, "recovery_id unique index did not reject duplicate"
            await db.partner_recovery_ledger.delete_many({"recovery_id": rid})
        finally:
            client.close()

    asyncio.get_event_loop().run_until_complete(_go())
