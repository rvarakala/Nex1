"""NAV-012 · Phase 2A — Idempotency-Key regression suite.

Covers Bundle A across three financial endpoints:

  * POST /api/billing/invoices/{invoice_id}/payments
  * POST /api/billing/invoices/{invoice_id}/refund
  * POST /api/referral-partners/{partner_id}/payouts

Includes crash-recovery verification: an `in_flight` idempotency
record whose business row DID land must NOT re-execute the financial
op; one whose business row did NOT land must safely CAS-takeover.
Uses direct DB mutation via Motor to simulate the crash-recovery
state (never a real crash / re-exec, so the test suite never depends
on subprocess kills).
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import os
import random
import string
import sys
import pathlib
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest
import requests
from motor.motor_asyncio import AsyncIOMotorClient

sys.path.append(str(pathlib.Path(__file__).resolve().parents[1]))
from _helpers import (  # noqa: E402
    API, ADMIN_EMAIL, ADMIN_PASSWORD, H, login,
)


_BRANCH_ID = "BR-PYTEST-001"
_CLINIC_ID = os.environ.get("TEST_CLINIC_ID", "clinic-pytest-suite")


def _uniq() -> str:
    return f"{int(time.time()*1000)%1_000_000_000:x}{''.join(random.choices(string.ascii_lowercase, k=4))}"


def _key(prefix: str = "idem") -> str:
    return f"{prefix}-{_uniq()}-{uuid.uuid4().hex[:12]}"


def _unique_phone() -> str:
    return f"+91{random.randint(7, 9)}{random.randint(10**8, 10**9 - 1)}"


async def _mongo_db():
    from dotenv import load_dotenv
    load_dotenv("/app/backend/.env")
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    return client, client[os.environ["DB_NAME"]]


# ─────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def token() -> str:
    return login(ADMIN_EMAIL, ADMIN_PASSWORD)


def _mk_service(token: str) -> str:
    r = requests.post(f"{API}/billing/services", headers=H(token), json={
        "code": f"NAV012-{_uniq()[:6].upper()}",
        "name": "NAV-012 test service",
        "price": 5000,
        "gst_rate": 0,
        "category": "Consultation",
        "active": True,
    }, timeout=15)
    assert r.status_code in (200, 201), r.text
    return r.json()["service_id"]


def _mk_patient(token: str) -> str:
    r = requests.post(f"{API}/patients", headers=H(token), json={
        "name": f"NAV012 Patient {_uniq()}",
        "mobile": _unique_phone(),
        "age": 40, "sex": "M", "branch_id": _BRANCH_ID,
    }, timeout=15)
    assert r.status_code in (200, 201), r.text
    return r.json()["patient_id"]


def _mk_open_invoice(token: str, svc: str, pat: str, amount: float = 5000) -> dict:
    r = requests.post(f"{API}/billing/invoices", headers=H(token), json={
        "patient_id": pat,
        "lines": [{
            "service_id": svc,
            "description": "NAV-012 line",
            "quantity": 1, "unit_price": amount,
            "discount_type": "flat", "discount_value": 0,
        }],
    }, timeout=20)
    assert r.status_code == 200, r.text
    return r.json()


def _mk_paid_invoice(token: str, svc: str, pat: str, amount: float = 5000) -> dict:
    r = requests.post(f"{API}/billing/invoices", headers=H(token), json={
        "patient_id": pat,
        "lines": [{
            "service_id": svc,
            "description": "NAV-012 line",
            "quantity": 1, "unit_price": amount,
            "discount_type": "flat", "discount_value": 0,
        }],
        "initial_payment": {"method": "cash", "amount": amount},
    }, timeout=20)
    assert r.status_code == 200, r.text
    return r.json()


# ─────────────────────────────────────────────────────────────────────
# BUNDLE A · Payment idempotency
# ─────────────────────────────────────────────────────────────────────

def test_payment_first_request_with_key_succeeds(token):
    svc = _mk_service(token); pat = _mk_patient(token)
    inv = _mk_open_invoice(token, svc, pat, 4000)
    key = _key("pay-first")
    r = requests.post(
        f"{API}/billing/invoices/{inv['invoice_id']}/payments",
        headers={**H(token), "Idempotency-Key": key},
        json={"method": "cash", "amount": 1000},
        timeout=15,
    )
    assert r.status_code == 200, r.text
    assert r.json()["paid_total"] == 1000
    assert r.headers.get("Idempotency-Replay") is None


def test_payment_replay_returns_identical_body(token):
    svc = _mk_service(token); pat = _mk_patient(token)
    inv = _mk_open_invoice(token, svc, pat, 4000)
    key = _key("pay-replay")
    url = f"{API}/billing/invoices/{inv['invoice_id']}/payments"
    body = {"method": "cash", "amount": 800}
    r1 = requests.post(url, headers={**H(token), "Idempotency-Key": key}, json=body, timeout=15)
    assert r1.status_code == 200
    r2 = requests.post(url, headers={**H(token), "Idempotency-Key": key}, json=body, timeout=15)
    assert r2.status_code == 200
    assert r2.headers.get("Idempotency-Replay") == "true"
    assert r2.json()["paid_total"] == r1.json()["paid_total"] == 800
    # Only ONE payment row landed.
    inv_now = requests.get(f"{API}/billing/invoices/{inv['invoice_id']}", headers=H(token), timeout=10).json()
    assert len([p for p in (inv_now.get("payments") or []) if (p.get("kind") or "payment") == "payment"]) == 1


def test_payment_same_key_different_payload_rejects_422(token):
    svc = _mk_service(token); pat = _mk_patient(token)
    inv = _mk_open_invoice(token, svc, pat, 5000)
    key = _key("pay-mismatch")
    url = f"{API}/billing/invoices/{inv['invoice_id']}/payments"
    r1 = requests.post(url, headers={**H(token), "Idempotency-Key": key},
                       json={"method": "cash", "amount": 500}, timeout=15)
    assert r1.status_code == 200
    r2 = requests.post(url, headers={**H(token), "Idempotency-Key": key},
                       json={"method": "cash", "amount": 700}, timeout=15)
    assert r2.status_code == 422, r2.text
    assert "different payload" in (r2.json().get("detail") or "").lower()


def test_payment_missing_key_preserves_existing_behaviour(token):
    svc = _mk_service(token); pat = _mk_patient(token)
    inv = _mk_open_invoice(token, svc, pat, 3000)
    r = requests.post(
        f"{API}/billing/invoices/{inv['invoice_id']}/payments",
        headers=H(token),   # no Idempotency-Key header
        json={"method": "cash", "amount": 500}, timeout=15,
    )
    assert r.status_code == 200
    assert r.json()["paid_total"] == 500
    assert r.headers.get("Idempotency-Replay") is None


def test_payment_malformed_key_rejects_400(token):
    svc = _mk_service(token); pat = _mk_patient(token)
    inv = _mk_open_invoice(token, svc, pat, 2000)
    # Only server-side rejections; whitespace / illegal chars are
    # scrubbed by the `requests` client-side header validator so we do
    # not exercise those here.
    for bad in ("short", "x" * 129, "abc"):
        r = requests.post(
            f"{API}/billing/invoices/{inv['invoice_id']}/payments",
            headers={**H(token), "Idempotency-Key": bad},
            json={"method": "cash", "amount": 100}, timeout=10,
        )
        assert r.status_code == 400, r.text


def test_payment_concurrent_same_key_serialises(token):
    svc = _mk_service(token); pat = _mk_patient(token)
    inv = _mk_open_invoice(token, svc, pat, 5000)
    key = _key("pay-conc")
    url = f"{API}/billing/invoices/{inv['invoice_id']}/payments"

    def go(_i: int):
        return requests.post(url, headers={**H(token), "Idempotency-Key": key},
                             json={"method": "cash", "amount": 900}, timeout=20)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        results = list(ex.map(go, range(5)))
    codes = [r.status_code for r in results]
    assert all(c < 500 for c in codes)
    # Exactly one payment landed.
    inv_now = requests.get(f"{API}/billing/invoices/{inv['invoice_id']}", headers=H(token), timeout=10).json()
    landed = [p for p in (inv_now.get("payments") or []) if (p.get("kind") or "payment") == "payment"]
    assert len(landed) == 1, f"expected exactly one payment, got {len(landed)}"


def test_payment_concurrent_different_keys_all_succeed(token):
    svc = _mk_service(token); pat = _mk_patient(token)
    inv = _mk_open_invoice(token, svc, pat, 5000)
    url = f"{API}/billing/invoices/{inv['invoice_id']}/payments"

    def go(_i: int):
        return requests.post(url, headers={**H(token), "Idempotency-Key": _key(f"pay-diff{_i}")},
                             json={"method": "cash", "amount": 500}, timeout=20)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(go, range(4)))
    ok = [r for r in results if r.status_code == 200]
    assert len(ok) == 4, [r.text for r in results if r.status_code != 200]
    inv_now = requests.get(f"{API}/billing/invoices/{inv['invoice_id']}", headers=H(token), timeout=10).json()
    landed = [p for p in (inv_now.get("payments") or []) if (p.get("kind") or "payment") == "payment"]
    assert len(landed) == 4


def test_payment_failed_operation_replays_failure(token):
    """Overpay-rejected payments cache their 400 for replay."""
    svc = _mk_service(token); pat = _mk_patient(token)
    inv = _mk_open_invoice(token, svc, pat, 2000)
    key = _key("pay-fail")
    url = f"{API}/billing/invoices/{inv['invoice_id']}/payments"
    r1 = requests.post(url, headers={**H(token), "Idempotency-Key": key},
                       json={"method": "cash", "amount": 9999}, timeout=15)
    assert r1.status_code == 400
    r2 = requests.post(url, headers={**H(token), "Idempotency-Key": key},
                       json={"method": "cash", "amount": 9999}, timeout=15)
    assert r2.status_code == 400
    assert r2.headers.get("Idempotency-Replay") == "true"


# ─────────────────────────────────────────────────────────────────────
# BUNDLE A · Refund idempotency
# ─────────────────────────────────────────────────────────────────────

def test_refund_first_and_replay(token):
    svc = _mk_service(token); pat = _mk_patient(token)
    inv = _mk_paid_invoice(token, svc, pat, 4000)
    key = _key("ref-first")
    url = f"{API}/billing/invoices/{inv['invoice_id']}/refund"
    payload = {"method": "cash", "amount": 1000, "reason": "test"}
    r1 = requests.post(url, headers={**H(token), "Idempotency-Key": key}, json=payload, timeout=15)
    assert r1.status_code == 200, r1.text
    assert r1.json()["refunded_total"] == 1000
    r2 = requests.post(url, headers={**H(token), "Idempotency-Key": key}, json=payload, timeout=15)
    assert r2.status_code == 200
    assert r2.headers.get("Idempotency-Replay") == "true"
    assert r2.json()["refunded_total"] == 1000
    # Only ONE refund row landed.
    inv_now = requests.get(f"{API}/billing/invoices/{inv['invoice_id']}", headers=H(token), timeout=10).json()
    assert len([p for p in (inv_now.get("payments") or []) if p.get("kind") == "refund"]) == 1


def test_refund_missing_key_preserves_behaviour(token):
    svc = _mk_service(token); pat = _mk_patient(token)
    inv = _mk_paid_invoice(token, svc, pat, 3000)
    r = requests.post(
        f"{API}/billing/invoices/{inv['invoice_id']}/refund",
        headers=H(token),
        json={"method": "cash", "amount": 500, "reason": "test"}, timeout=15,
    )
    assert r.status_code == 200
    assert r.json()["refunded_total"] == 500


def test_refund_scope_isolation_from_payment(token):
    """Same key value on payment + refund scopes never collide."""
    svc = _mk_service(token); pat = _mk_patient(token)
    inv = _mk_paid_invoice(token, svc, pat, 4000)
    key = _key("scope-x")
    # First: refund with the key.
    r1 = requests.post(f"{API}/billing/invoices/{inv['invoice_id']}/refund",
                       headers={**H(token), "Idempotency-Key": key},
                       json={"method": "cash", "amount": 800, "reason": "scope-iso-test"}, timeout=15)
    assert r1.status_code == 200, r1.text
    # Now: payment on a DIFFERENT invoice reuses the SAME key — must be independent.
    inv2 = _mk_open_invoice(token, svc, pat, 2000)
    r2 = requests.post(f"{API}/billing/invoices/{inv2['invoice_id']}/payments",
                       headers={**H(token), "Idempotency-Key": key},
                       json={"method": "cash", "amount": 500}, timeout=15)
    assert r2.status_code == 200, r2.text
    assert r2.json()["paid_total"] == 500


# ─────────────────────────────────────────────────────────────────────
# BUNDLE A · Payout idempotency (referral partner)
# ─────────────────────────────────────────────────────────────────────

def _mk_partner(token: str) -> str:
    r = requests.post(f"{API}/referral-partners", headers=H(token), json={
        "name": f"NAV012 Partner {_uniq()}",
        "email": f"nav012-partner-{_uniq()}@nav012.example.com",
        "commission_kind": "percent",
        "commission_value": 10,
        "phone": _unique_phone(),
    }, timeout=15)
    assert r.status_code in (200, 201), r.text
    pid = r.json()["partner_id"]
    r2 = requests.patch(f"{API}/referral-partners/{pid}",
                        headers=H(token), json={"status": "active"}, timeout=15)
    assert r2.status_code == 200, r2.text
    return pid


def test_payout_first_and_replay(token):
    pid = _mk_partner(token)
    key = _key("po-first")
    url = f"{API}/referral-partners/{pid}/payouts"
    payload = {"period_start": "2025-01-01", "period_end": "2025-01-31", "notes": "nav012-test"}
    r1 = requests.post(url, headers={**H(token), "Idempotency-Key": key}, json=payload, timeout=20)
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    r2 = requests.post(url, headers={**H(token), "Idempotency-Key": key}, json=payload, timeout=20)
    assert r2.status_code == 200
    assert r2.headers.get("Idempotency-Replay") == "true"
    assert r2.json()["payout_id"] == body1["payout_id"]
    # Only ONE payout row landed for this window.
    listing = requests.get(url, headers=H(token), timeout=15).json()
    matching = [p for p in listing
                if p["period_start"] == payload["period_start"]
                and p["period_end"] == payload["period_end"]]
    assert len(matching) == 1


def test_payout_missing_key_preserves_behaviour(token):
    pid = _mk_partner(token)
    r = requests.post(f"{API}/referral-partners/{pid}/payouts", headers=H(token),
                      json={"period_start": "2025-02-01", "period_end": "2025-02-28"},
                      timeout=20)
    assert r.status_code == 200, r.text


def test_payout_concurrent_same_key_only_one_row(token):
    pid = _mk_partner(token)
    key = _key("po-conc")
    url = f"{API}/referral-partners/{pid}/payouts"
    payload = {"period_start": "2025-03-01", "period_end": "2025-03-31"}

    def go(_i: int):
        return requests.post(url, headers={**H(token), "Idempotency-Key": key},
                             json=payload, timeout=20)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(go, range(4)))
    assert all(r.status_code < 500 for r in results)
    listing = requests.get(url, headers=H(token), timeout=15).json()
    matching = [p for p in listing
                if p["period_start"] == payload["period_start"]
                and p["period_end"] == payload["period_end"]]
    assert len(matching) == 1


# ─────────────────────────────────────────────────────────────────────
# BUNDLE A · TTL / expiry / tenant isolation / crash-recovery
# ─────────────────────────────────────────────────────────────────────

def test_idempotency_ttl_index_present():
    """Sanity-check the required TTL index exists on the collection."""
    async def _go():
        client, db = await _mongo_db()
        try:
            info = await db.idempotency_keys.index_information()
            assert "uniq_clinic_scope_key" in info
            assert info["uniq_clinic_scope_key"].get("unique") is True
            assert "idem_expires_ttl" in info
            assert info["idem_expires_ttl"].get("expireAfterSeconds") == 0
        finally:
            client.close()
    asyncio.get_event_loop().run_until_complete(_go())


def test_idempotency_tenant_scoping(token):
    """Same key value in two different tenants must not collide.

    We use direct DB inspection here (rather than logging in a second
    tenant) to confirm the compound UNIQUE index scopes by clinic_id.
    """
    async def _go():
        client, db = await _mongo_db()
        try:
            key = _key("tenant-iso")
            # Manually seed a completed record on a different tenant.
            other = {
                "clinic_id": "tenant-other-clinic-nav012",
                "idempotency_key": key, "scope": "payment", "route": "test",
                "request_hash": "beef", "status": "completed",
                "http_status": 200, "response_body": {"ok": True},
                "operation_ref": {"collection": "payments",
                                   "field": "idempotency_correlation_id",
                                   "value": "other-tenant"},
                "created_at": datetime.now(timezone.utc).isoformat(),
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "expires_at": datetime.now(timezone.utc) + timedelta(hours=24),
                "actor": {}, "failure": None,
            }
            await db.idempotency_keys.insert_one(other)
            # Our tenant now uses the same key — must succeed cleanly.
            svc = _mk_service(token); pat = _mk_patient(token)
            inv = _mk_open_invoice(token, svc, pat, 3000)
            r = requests.post(
                f"{API}/billing/invoices/{inv['invoice_id']}/payments",
                headers={**H(token), "Idempotency-Key": key},
                json={"method": "cash", "amount": 500}, timeout=15,
            )
            assert r.status_code == 200, r.text
            assert r.headers.get("Idempotency-Replay") is None
            # Confirm both tenant records exist independently.
            n = await db.idempotency_keys.count_documents(
                {"idempotency_key": key, "scope": "payment"}
            )
            assert n == 2, f"expected 2 tenant-scoped records; got {n}"
            await db.idempotency_keys.delete_many({"idempotency_key": key})
        finally:
            client.close()
    asyncio.get_event_loop().run_until_complete(_go())


def _server_hash(payload: dict) -> str:
    """Reproduce the server's `_canonical_hash` exactly by calling it."""
    from utils.idempotency import _canonical_hash
    return _canonical_hash(payload)


def _payment_canonical_payload(invoice_id: str, method: str, amount: float,
                                reference=None, notes=None) -> dict:
    """Mimic `PaymentCreate.model_dump()` order + defaults exactly so
    the resulting hash matches the server's.  `amount` is coerced to
    float because Pydantic's `float` field always produces a float in
    `model_dump()`, and `json.dumps(1234) != json.dumps(1234.0)`.
    """
    return {
        "invoice_id": invoice_id,
        "method": method,
        "amount": float(amount),
        "reference": reference,
        "notes": notes,
    }


def _stale_ts(seconds_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def test_crash_recovery_when_business_op_LANDED_replays_no_duplicate(token):
    """Simulate: server wrote the payment row + set correlation_id, then
    crashed BEFORE the idempotency record flipped to `completed`.
    Retry MUST NOT execute a second financial write.
    """
    async def _go():
        client, db = await _mongo_db()
        try:
            svc = _mk_service(token); pat = _mk_patient(token)
            inv = _mk_open_invoice(token, svc, pat, 5000)
            key = _key("crash-landed")
            corr = uuid.uuid4().hex
            body = {"method": "cash", "amount": 1234}
            request_hash = _server_hash(_payment_canonical_payload(
                inv["invoice_id"], "cash", 1234,
            ))

            # 1. Fake: prior request landed the payment with `corr`.
            payment_doc = {
                "payment_id": f"PAY-CRASH-{uuid.uuid4().hex[:8]}",
                "clinic_id": _CLINIC_ID, "invoice_id": inv["invoice_id"],
                "kind": "payment", "method": "cash", "amount": 1234,
                "paid_at": datetime.now(timezone.utc).isoformat(),
                "idempotency_correlation_id": corr,
            }
            await db.payments.insert_one(payment_doc)
            await db.invoices.update_one(
                {"invoice_id": inv["invoice_id"]},
                {"$push": {"payments": payment_doc},
                 "$inc": {"paid_total": 1234}},
            )

            # 2. Fake: idempotency record stayed `in_flight` and is now stale.
            await db.idempotency_keys.insert_one({
                "clinic_id": _CLINIC_ID, "idempotency_key": key,
                "scope": "payment",
                "route": "/api/billing/invoices/{invoice_id}/payments",
                "request_hash": request_hash,
                "status": "in_flight",
                "http_status": None, "response_body": None,
                "operation_ref": {"collection": "payments",
                                   "field": "idempotency_correlation_id",
                                   "value": corr},
                "created_at": _stale_ts(200),
                "completed_at": None,
                "expires_at": datetime.now(timezone.utc) + timedelta(hours=23),
                "actor": {}, "failure": None,
            })

            # 3. Retry.
            r = requests.post(
                f"{API}/billing/invoices/{inv['invoice_id']}/payments",
                headers={**H(token), "Idempotency-Key": key},
                json=body, timeout=15,
            )
            assert r.status_code == 200, r.text
            assert r.headers.get("Idempotency-Replay") == "true"

            # 4. Only the SEED payment row exists — no second row.
            pay_rows = await db.payments.count_documents({
                "invoice_id": inv["invoice_id"], "kind": "payment",
            })
            assert pay_rows == 1, f"crash-recovery double-wrote payment: {pay_rows} rows"

            # Idempotency record should now be `completed`.
            rec = await db.idempotency_keys.find_one(
                {"clinic_id": _CLINIC_ID, "idempotency_key": key},
                {"_id": 0},
            )
            assert rec["status"] == "completed"
            await db.idempotency_keys.delete_many({"idempotency_key": key})
        finally:
            client.close()
    asyncio.get_event_loop().run_until_complete(_go())


def test_crash_recovery_when_business_op_MISSING_takes_over(token):
    """Simulate: server inserted the idempotency record and crashed
    BEFORE calling the business op. Retry MUST safely take over and
    execute a fresh business op.
    """
    async def _go():
        client, db = await _mongo_db()
        try:
            svc = _mk_service(token); pat = _mk_patient(token)
            inv = _mk_open_invoice(token, svc, pat, 3000)
            key = _key("crash-missing")
            corr = uuid.uuid4().hex
            body = {"method": "cash", "amount": 700}
            request_hash = _server_hash(_payment_canonical_payload(
                inv["invoice_id"], "cash", 700,
            ))

            # Seed stale in_flight — no matching payment row exists.
            await db.idempotency_keys.insert_one({
                "clinic_id": _CLINIC_ID, "idempotency_key": key,
                "scope": "payment",
                "route": "/api/billing/invoices/{invoice_id}/payments",
                "request_hash": request_hash,
                "status": "in_flight",
                "http_status": None, "response_body": None,
                "operation_ref": {"collection": "payments",
                                   "field": "idempotency_correlation_id",
                                   "value": corr},
                "created_at": _stale_ts(300),
                "completed_at": None,
                "expires_at": datetime.now(timezone.utc) + timedelta(hours=23),
                "actor": {}, "failure": None,
            })

            r = requests.post(
                f"{API}/billing/invoices/{inv['invoice_id']}/payments",
                headers={**H(token), "Idempotency-Key": key},
                json=body, timeout=15,
            )
            assert r.status_code == 200, r.text
            # A takeover is a FRESH request — no replay header.
            assert r.headers.get("Idempotency-Replay") is None

            pay_rows = await db.payments.count_documents({
                "invoice_id": inv["invoice_id"], "kind": "payment",
            })
            assert pay_rows == 1
            await db.idempotency_keys.delete_many({"idempotency_key": key})
        finally:
            client.close()
    asyncio.get_event_loop().run_until_complete(_go())


def test_in_flight_within_stale_window_returns_409(token):
    """A `in_flight` record that is FRESH must produce 409, not
    take over."""
    async def _go():
        client, db = await _mongo_db()
        try:
            svc = _mk_service(token); pat = _mk_patient(token)
            inv = _mk_open_invoice(token, svc, pat, 2000)
            key = _key("in-flight-fresh")
            corr = uuid.uuid4().hex
            body = {"method": "cash", "amount": 300}
            request_hash = _server_hash(_payment_canonical_payload(
                inv["invoice_id"], "cash", 300,
            ))
            await db.idempotency_keys.insert_one({
                "clinic_id": _CLINIC_ID, "idempotency_key": key,
                "scope": "payment",
                "route": "/api/billing/invoices/{invoice_id}/payments",
                "request_hash": request_hash,
                "status": "in_flight",
                "http_status": None, "response_body": None,
                "operation_ref": {"collection": "payments",
                                   "field": "idempotency_correlation_id",
                                   "value": corr},
                "created_at": datetime.now(timezone.utc).isoformat(),
                "completed_at": None,
                "expires_at": datetime.now(timezone.utc) + timedelta(hours=24),
                "actor": {}, "failure": None,
            })
            r = requests.post(
                f"{API}/billing/invoices/{inv['invoice_id']}/payments",
                headers={**H(token), "Idempotency-Key": key},
                json=body, timeout=10,
            )
            assert r.status_code == 409, r.text
            await db.idempotency_keys.delete_many({"idempotency_key": key})
        finally:
            client.close()
    asyncio.get_event_loop().run_until_complete(_go())


def test_expired_record_permits_new_operation(token):
    """A record that has TTL-expired (or was TTL-swept) MUST permit a
    fresh operation without any hint of the prior key.  The Mongo TTL
    sweeper runs on its own cadence, so we simulate by deleting the
    record ourselves after a `completed` first request."""
    async def _go():
        client, db = await _mongo_db()
        try:
            svc = _mk_service(token); pat = _mk_patient(token)
            inv = _mk_open_invoice(token, svc, pat, 2000)
            key = _key("ttl-expire")
            body = {"method": "cash", "amount": 200}
            # 1. First request lands.
            r1 = requests.post(
                f"{API}/billing/invoices/{inv['invoice_id']}/payments",
                headers={**H(token), "Idempotency-Key": key},
                json=body, timeout=15,
            )
            assert r1.status_code == 200
            # 2. TTL-simulate: delete the idempotency record.
            n = await db.idempotency_keys.delete_many(
                {"clinic_id": _CLINIC_ID, "idempotency_key": key},
            )
            assert n.deleted_count == 1
            # 3. Retry — must be treated as brand new (no replay).
            r2 = requests.post(
                f"{API}/billing/invoices/{inv['invoice_id']}/payments",
                headers={**H(token), "Idempotency-Key": key},
                json=body, timeout=15,
            )
            assert r2.status_code == 200
            assert r2.headers.get("Idempotency-Replay") is None
        finally:
            client.close()
    asyncio.get_event_loop().run_until_complete(_go())
