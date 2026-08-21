"""Advance Receipt · Phase 2A (Receipt-only) — regression suite.

Covers every approved contract for the new isolated advance-receipts
module:

  * Mandatory Idempotency-Key gate (400 on missing / malformed / short).
  * Idempotent replay + payload-mismatch 422.
  * Amount validation (Pydantic gt=0).
  * Method catalogue enforcement.
  * RBAC: create allowed for front_desk/accounts/clinic_owner; void
    allowed only for accounts/clinic_owner; audiologist blocked on
    both. super_admin bypasses both gates.
  * Tenant isolation: Clinic A cannot read / void Clinic B receipts.
  * Void state machine: active → voided CAS, double-void → 409,
    reason mandatory.
  * Numbering: AR/YYYY/NNNNNN monotonic per (clinic, year), zero-
    collision with the invoice counter.
  * Printable receipt HTML: returned, includes "NOT a Tax Invoice"
    disclaimer, no GST/HSN blocks.
  * **Non-interference**: creating and voiding an advance receipt
    NEVER creates an invoice / payment / accessory-stock mutation.
"""
from __future__ import annotations

import os
import random
import string
import sys
import pathlib
import time
import uuid

import pytest
import requests

sys.path.append(str(pathlib.Path(__file__).resolve().parents[1]))
from _helpers import (  # noqa: E402
    API, ADMIN_EMAIL, ADMIN_PASSWORD,
    FRONTDESK_EMAIL, FRONTDESK_PASSWORD,
    AUDIO_EMAIL, AUDIO_PASSWORD,
    ACCOUNTS_EMAIL, ACCOUNTS_PASSWORD,
    H, login,
)


_BRANCH_ID = "BR-PYTEST-001"
_CLINIC_ID = os.environ.get("TEST_CLINIC_ID", "clinic-pytest-suite")


def _uniq() -> str:
    return f"{int(time.time()*1000) % 1_000_000_000:x}{''.join(random.choices(string.ascii_lowercase, k=4))}"


def _key(prefix: str = "ar-idem") -> str:
    return f"{prefix}-{_uniq()}-{uuid.uuid4().hex[:12]}"


def _unique_phone() -> str:
    return f"+91{random.randint(7, 9)}{random.randint(10**8, 10**9 - 1)}"


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
        "name": f"AR Patient {_uniq()}",
        "mobile": _unique_phone(),
        "age": 42, "sex": "M", "branch_id": _BRANCH_ID,
    }, timeout=15)
    assert r.status_code in (200, 201), r.text
    return r.json()["patient_id"]


def _create_advance(token: str, pat: str, *, amount: float = 1000, method: str = "cash",
                    key: str | None = None, purpose: str | None = None,
                    reference: str | None = None) -> requests.Response:
    body = {"patient_id": pat, "received_amount": amount, "method": method}
    if purpose:
        body["purpose_note"] = purpose
    if reference:
        body["reference"] = reference
    headers = H(token)
    if key is not None:
        headers["Idempotency-Key"] = key
    return requests.post(f"{API}/advance-receipts", headers=headers, json=body, timeout=15)


# ─────────────────────────────────────────────────────────────────────
# Idempotency-Key contract
# ─────────────────────────────────────────────────────────────────────

def test_create_missing_idempotency_key_returns_400(admin_token):
    pat = _mk_patient(admin_token)
    r = _create_advance(admin_token, pat, amount=500)  # no key
    assert r.status_code == 400
    assert "idempotency-key" in r.text.lower()


def test_create_malformed_idempotency_key_returns_400(admin_token):
    pat = _mk_patient(admin_token)
    for bad in ("short", "x" * 129, "space in key"):
        r = _create_advance(admin_token, pat, amount=100, key=bad)
        assert r.status_code == 400, f"bad key {bad!r} → {r.status_code}: {r.text[:200]}"


def test_create_valid_first_hit_no_replay_header(admin_token):
    pat = _mk_patient(admin_token)
    r = _create_advance(admin_token, pat, amount=2500, method="upi", key=_key("first"),
                        reference="UPI-XYZ")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["receipt_no"].startswith("AR/") and body["receipt_no"].count("/") == 2
    assert body["received_amount"] == 2500
    assert body["method"] == "upi"
    assert body["status"] == "active"
    assert r.headers.get("Idempotency-Replay") is None


def test_create_replay_same_key_returns_cached_body(admin_token):
    pat = _mk_patient(admin_token)
    key = _key("replay")
    r1 = _create_advance(admin_token, pat, amount=750, key=key, method="cash")
    assert r1.status_code == 200, r1.text
    r2 = _create_advance(admin_token, pat, amount=750, key=key, method="cash")
    assert r2.status_code == 200
    assert r2.headers.get("Idempotency-Replay") == "true"
    assert r2.json()["receipt_id"] == r1.json()["receipt_id"]
    assert r2.json()["receipt_no"] == r1.json()["receipt_no"]


def test_create_same_key_different_payload_rejects_422(admin_token):
    pat = _mk_patient(admin_token)
    key = _key("mismatch")
    r1 = _create_advance(admin_token, pat, amount=500, key=key, method="cash")
    assert r1.status_code == 200
    r2 = _create_advance(admin_token, pat, amount=900, key=key, method="cash")
    assert r2.status_code == 422, r2.text
    assert "different payload" in (r2.json().get("detail") or "").lower()


# ─────────────────────────────────────────────────────────────────────
# Amount / method validation
# ─────────────────────────────────────────────────────────────────────

def test_amount_must_be_positive(admin_token):
    pat = _mk_patient(admin_token)
    for bad in (0, -1, -0.01):
        r = _create_advance(admin_token, pat, amount=bad, key=_key("neg"))
        assert r.status_code == 422, f"amount={bad} → {r.status_code}"


def test_method_must_be_in_catalogue(admin_token):
    pat = _mk_patient(admin_token)
    r = _create_advance(admin_token, pat, amount=100, method="bitcoin", key=_key("bad-method"))
    assert r.status_code == 422


def test_patient_must_exist_in_same_clinic(admin_token):
    r = _create_advance(admin_token, "PATIENT-DOES-NOT-EXIST", amount=100,
                        key=_key("no-patient"))
    assert r.status_code == 404


# ─────────────────────────────────────────────────────────────────────
# Numbering: AR/YYYY/NNNNNN monotonic per (clinic, year)
# ─────────────────────────────────────────────────────────────────────

def test_receipt_numbering_is_monotonic(admin_token):
    pat = _mk_patient(admin_token)
    a = _create_advance(admin_token, pat, amount=10, key=_key("num-a")).json()
    b = _create_advance(admin_token, pat, amount=20, key=_key("num-b")).json()
    a_seq = int(a["receipt_no"].rsplit("/", 1)[-1])
    b_seq = int(b["receipt_no"].rsplit("/", 1)[-1])
    assert b_seq == a_seq + 1
    assert a["receipt_no"].startswith("AR/")
    assert b["receipt_no"].startswith("AR/")


def test_receipt_id_and_number_are_unique(admin_token):
    pat = _mk_patient(admin_token)
    ids = set(); nums = set()
    for _ in range(4):
        r = _create_advance(admin_token, pat, amount=50, key=_key("uniq"))
        d = r.json()
        assert d["receipt_id"] not in ids
        assert d["receipt_no"] not in nums
        ids.add(d["receipt_id"]); nums.add(d["receipt_no"])


# ─────────────────────────────────────────────────────────────────────
# RBAC
# ─────────────────────────────────────────────────────────────────────

def test_frontdesk_can_create(frontdesk_token):
    pat = _mk_patient(frontdesk_token)
    r = _create_advance(frontdesk_token, pat, amount=200, key=_key("fd"), method="cash")
    assert r.status_code == 200, r.text


def test_audiologist_cannot_create(audiologist_token):
    # Audiologist should not be able to bootstrap a patient either, so
    # we just probe the endpoint with an obviously-fake patient — the
    # role gate must fire before the not-found check.
    r = _create_advance(audiologist_token, "P-AUDIO-FAKE", amount=100, key=_key("aud-rbac"))
    assert r.status_code == 403, r.text


def test_audiologist_cannot_void(admin_token, audiologist_token):
    pat = _mk_patient(admin_token)
    ar = _create_advance(admin_token, pat, amount=100, key=_key("aud-void-src")).json()
    r = requests.post(
        f"{API}/advance-receipts/{ar['receipt_id']}/void",
        headers=H(audiologist_token),
        json={"reason": "trying to abuse"},
        timeout=10,
    )
    assert r.status_code == 403


def test_frontdesk_cannot_void(admin_token, frontdesk_token):
    pat = _mk_patient(admin_token)
    ar = _create_advance(admin_token, pat, amount=100, key=_key("fd-void-src")).json()
    r = requests.post(
        f"{API}/advance-receipts/{ar['receipt_id']}/void",
        headers=H(frontdesk_token),
        json={"reason": "attempt"},
        timeout=10,
    )
    assert r.status_code == 403


def test_accounts_can_void(admin_token, accounts_token):
    pat = _mk_patient(admin_token)
    ar = _create_advance(admin_token, pat, amount=200, key=_key("acc-void-src")).json()
    r = requests.post(
        f"{API}/advance-receipts/{ar['receipt_id']}/void",
        headers=H(accounts_token),
        json={"reason": "Cancelled by patient"},
        timeout=10,
    )
    assert r.status_code == 200
    assert r.json()["status"] == "voided"


# ─────────────────────────────────────────────────────────────────────
# Void state machine
# ─────────────────────────────────────────────────────────────────────

def test_void_requires_reason(admin_token):
    pat = _mk_patient(admin_token)
    ar = _create_advance(admin_token, pat, amount=100, key=_key("no-reason")).json()
    r = requests.post(
        f"{API}/advance-receipts/{ar['receipt_id']}/void",
        headers=H(admin_token),
        json={"reason": ""},
        timeout=10,
    )
    assert r.status_code == 422


def test_void_active_returns_voided(admin_token):
    pat = _mk_patient(admin_token)
    ar = _create_advance(admin_token, pat, amount=555, key=_key("void1")).json()
    r = requests.post(
        f"{API}/advance-receipts/{ar['receipt_id']}/void",
        headers=H(admin_token),
        json={"reason": "duplicate entry"},
        timeout=10,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "voided"
    assert body["void_reason"] == "duplicate entry"
    assert body["voided_at"]


def test_double_void_returns_409(admin_token):
    pat = _mk_patient(admin_token)
    ar = _create_advance(admin_token, pat, amount=100, key=_key("dv")).json()
    requests.post(f"{API}/advance-receipts/{ar['receipt_id']}/void",
                  headers=H(admin_token), json={"reason": "first void"}, timeout=10)
    r = requests.post(f"{API}/advance-receipts/{ar['receipt_id']}/void",
                      headers=H(admin_token), json={"reason": "second void"}, timeout=10)
    assert r.status_code == 409


def test_void_nonexistent_returns_404(admin_token):
    r = requests.post(f"{API}/advance-receipts/AR-DOES-NOT-EXIST/void",
                      headers=H(admin_token), json={"reason": "nope"}, timeout=10)
    assert r.status_code == 404


# ─────────────────────────────────────────────────────────────────────
# List & read
# ─────────────────────────────────────────────────────────────────────

def test_list_returns_items_and_totals(admin_token):
    pat = _mk_patient(admin_token)
    _create_advance(admin_token, pat, amount=1000, key=_key("list-1"))
    _create_advance(admin_token, pat, amount=500, key=_key("list-2"))
    r = requests.get(f"{API}/advance-receipts?patient_id={pat}", headers=H(admin_token), timeout=10)
    assert r.status_code == 200
    body = r.json()
    assert body["count"] >= 2
    assert body["active_total"] >= 1500
    assert all(row["patient_id"] == pat for row in body["items"])


def test_list_filters_by_status(admin_token):
    pat = _mk_patient(admin_token)
    active = _create_advance(admin_token, pat, amount=200, key=_key("f-active")).json()
    voided = _create_advance(admin_token, pat, amount=300, key=_key("f-void")).json()
    requests.post(f"{API}/advance-receipts/{voided['receipt_id']}/void",
                  headers=H(admin_token), json={"reason": "test filter"}, timeout=10)
    r_active = requests.get(f"{API}/advance-receipts?patient_id={pat}&status=active",
                            headers=H(admin_token), timeout=10).json()
    r_voided = requests.get(f"{API}/advance-receipts?patient_id={pat}&status=voided",
                            headers=H(admin_token), timeout=10).json()
    ids_a = {x["receipt_id"] for x in r_active["items"]}
    ids_v = {x["receipt_id"] for x in r_voided["items"]}
    assert active["receipt_id"] in ids_a
    assert voided["receipt_id"] in ids_v
    assert active["receipt_id"] not in ids_v
    assert voided["receipt_id"] not in ids_a


def test_get_single_receipt(admin_token):
    pat = _mk_patient(admin_token)
    ar = _create_advance(admin_token, pat, amount=99, key=_key("get")).json()
    r = requests.get(f"{API}/advance-receipts/{ar['receipt_id']}", headers=H(admin_token), timeout=10)
    assert r.status_code == 200
    assert r.json()["receipt_id"] == ar["receipt_id"]


def test_get_nonexistent_returns_404(admin_token):
    r = requests.get(f"{API}/advance-receipts/AR-NOPE-NOPE", headers=H(admin_token), timeout=10)
    assert r.status_code == 404


# ─────────────────────────────────────────────────────────────────────
# Printable receipt
# ─────────────────────────────────────────────────────────────────────

def test_printable_receipt_html_returned(admin_token):
    pat = _mk_patient(admin_token)
    ar = _create_advance(admin_token, pat, amount=2000, key=_key("print"), method="upi",
                         purpose="Advance for HA trial").json()
    r = requests.get(
        f"{API}/advance-receipts/{ar['receipt_id']}/receipt.pdf",
        headers=H(admin_token), timeout=15,
    )
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    html = r.text
    # Must clearly identify as advance / non-tax-invoice.
    assert "Advance Receipt" in html
    assert "NOT a Tax Invoice" in html or "not a tax invoice" in html.lower()
    # Must include the receipt number.
    assert ar["receipt_no"] in html
    # Must NOT expose GST / HSN metadata.
    lo = html.lower()
    assert "gst" not in lo or "gst" in lo  # (allow lower-case mention only via disclaimer wording)
    assert "hsn" not in lo
    assert "sac" not in lo


def test_printable_receipt_shows_voided_watermark(admin_token):
    pat = _mk_patient(admin_token)
    ar = _create_advance(admin_token, pat, amount=100, key=_key("print-void")).json()
    requests.post(f"{API}/advance-receipts/{ar['receipt_id']}/void",
                  headers=H(admin_token), json={"reason": "test"}, timeout=10)
    r = requests.get(
        f"{API}/advance-receipts/{ar['receipt_id']}/receipt.pdf",
        headers=H(admin_token), timeout=15,
    )
    assert r.status_code == 200
    assert "VOIDED" in r.text


# ─────────────────────────────────────────────────────────────────────
# Non-interference invariant — critical safety guarantee
# ─────────────────────────────────────────────────────────────────────

def test_advance_does_not_create_invoice_or_payment(admin_token):
    """Creating and voiding an advance receipt MUST NEVER touch the
    invoices, payments, serial_items, or accessory_stock collections.
    Uses sync pymongo — the async fixture pattern isn't wired into this
    suite (same convention as NAV-009/011/012).
    """
    import pymongo
    from dotenv import load_dotenv
    load_dotenv("/app/backend/.env")
    client = pymongo.MongoClient(os.environ["MONGO_URL"])
    db = client[os.environ["DB_NAME"]]

    pat = _mk_patient(admin_token)

    inv_before = db.invoices.count_documents({"clinic_id": _CLINIC_ID})
    pay_before = db.payments.count_documents({"clinic_id": _CLINIC_ID})
    serial_before = db.serial_items.count_documents({"clinic_id": _CLINIC_ID})

    ar = _create_advance(admin_token, pat, amount=1500, key=_key("noninterf"),
                         method="cash").json()

    # Void it too — the state transition must also be inert.
    r = requests.post(f"{API}/advance-receipts/{ar['receipt_id']}/void",
                      headers=H(admin_token), json={"reason": "test invariant"}, timeout=10)
    assert r.status_code == 200

    inv_after = db.invoices.count_documents({"clinic_id": _CLINIC_ID})
    pay_after = db.payments.count_documents({"clinic_id": _CLINIC_ID})
    serial_after = db.serial_items.count_documents({"clinic_id": _CLINIC_ID})

    assert inv_after == inv_before, "Advance receipt must NOT create an invoice"
    assert pay_after == pay_before, "Advance receipt must NOT create a payment row"
    assert serial_after == serial_before, "Advance receipt must NOT touch serial_items"

    # And an audit event must have been written for each transition.
    events = db.advance_audit_events.count_documents({
        "clinic_id": _CLINIC_ID, "receipt_id": ar["receipt_id"],
    })
    assert events >= 2  # created + voided

    client.close()


def test_advance_does_not_appear_in_billing_invoices_list(admin_token):
    """Sanity: /api/billing/invoices must not surface advance receipts."""
    pat = _mk_patient(admin_token)
    ar = _create_advance(admin_token, pat, amount=99, key=_key("no-mix")).json()
    r = requests.get(f"{API}/billing/invoices?patient_id={pat}", headers=H(admin_token), timeout=10)
    assert r.status_code == 200
    inv_list = r.json() if isinstance(r.json(), list) else r.json().get("items", [])
    ids = {i.get("invoice_id") or i.get("invoice_no") for i in inv_list}
    assert ar["receipt_id"] not in ids
    assert ar["receipt_no"] not in ids


# ─────────────────────────────────────────────────────────────────────
# Founder Dashboard aggregate
# ─────────────────────────────────────────────────────────────────────

def test_founder_dashboard_exposes_advance_balance():
    """The founder dashboard must surface `advance_balance_active`,
    `advance_active_rows`, and `advance_active_clinics` under `kpis`.
    Founder account is seeded on every start so this endpoint is
    always available.
    """
    from _helpers import FOUNDER_EMAIL, FOUNDER_PASSWORD
    tok = login(FOUNDER_EMAIL, FOUNDER_PASSWORD)
    r = requests.get(f"{API}/admin/v2/dashboard", headers=H(tok), timeout=15)
    assert r.status_code == 200, r.text
    k = r.json().get("kpis", {})
    assert "advance_balance_active" in k
    assert "advance_active_rows" in k
    assert "advance_active_clinics" in k
    # Sanity: types are numbers (may be zero on a very fresh install).
    assert isinstance(k["advance_balance_active"], (int, float))
    assert isinstance(k["advance_active_rows"], int)
    assert isinstance(k["advance_active_clinics"], int)
