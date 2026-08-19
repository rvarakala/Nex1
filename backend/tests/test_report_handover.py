"""Tests for the simplified report lifecycle (Feb 2026 v2 — handover scrapped).

Covers:
  * Queue token dedupe: two POST /tokens for same patient same day → ONE token (service updated).
  * PDF upload: POST /sessions/{id}/report-pdf stores blob in GridFS, GET /reports/{id}/pdf serves it.
  * Lifecycle: session flips draft → completed on first print (no intermediate report_ready).
  * Legacy aliases: /complete-test, /mark-printed, /generate-report all land on completed.
  * /api/reports/pending-count stays at 0 (deprecated).
  * /api/billing/pending-reports returns [] (deprecated).
  * /api/reports?status=completed returns rows with has_uploaded_pdf + bill_paid flags.
"""
import os
import random
from datetime import datetime, timezone

import pytest
import requests


from _helpers import (  # legacy creds (env-overridable)
    ADMIN_EMAIL, ADMIN_PASSWORD,
    FRONTDESK_EMAIL, FRONTDESK_PASSWORD,
    AUDIO_EMAIL, AUDIO_PASSWORD,
    ACCOUNTS_EMAIL, ACCOUNTS_PASSWORD,
)
BASE = os.environ.get("REACT_APP_BACKEND_URL", "https://referral-sprint.preview.emergentagent.com").rstrip("/")
API = f"{BASE}/api"


def _login(email, pwd):
    r = requests.post(f"{API}/auth/login", json={"email": email, "password": pwd}, timeout=15)
    assert r.status_code == 200, f"login {email}: {r.status_code} {r.text}"
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


@pytest.fixture(scope="module")
def tokens():
    return {
        "fd": _login(FRONTDESK_EMAIL, FRONTDESK_PASSWORD),
        "aud": _login(AUDIO_EMAIL, AUDIO_PASSWORD),
        "accounts": _login(ACCOUNTS_EMAIL, ACCOUNTS_PASSWORD),
        "admin": _login(ADMIN_EMAIL, ADMIN_PASSWORD),
    }


@pytest.fixture(scope="module")
def patient(tokens):
    mobile = f"97{random.randint(10000000, 99999999)}"
    r = requests.post(
        f"{API}/patients",
        json={"name": f"HandoverV2 {random.randint(1000, 9999)}",
              "age": 30, "gender": "Female", "mobile": mobile},
        headers=_h(tokens["fd"]), timeout=15,
    )
    assert r.status_code in (200, 201), r.text
    return r.json()


# ------------------------------------------------------------------
# Token dedupe — the "Jasmita appeared twice" bug fix.
# ------------------------------------------------------------------

def test_token_dedupe_same_patient_same_day(tokens, patient):
    """Two POST /tokens for the same patient same day should return ONE token."""
    pid = patient["patient_id"]
    h = _h(tokens["fd"])

    r1 = requests.post(f"{API}/tokens", json={"patient_id": pid, "service": "Registration"},
                       headers=h, timeout=15)
    assert r1.status_code == 200, r1.text
    t1 = r1.json()
    assert t1["service"] == "Registration"

    r2 = requests.post(f"{API}/tokens", json={"patient_id": pid, "service": "PTA"},
                       headers=h, timeout=15)
    assert r2.status_code == 200, r2.text
    t2 = r2.json()

    # Same token_id — service got upgraded in place.
    assert t1["token_id"] == t2["token_id"], "token was duplicated instead of updated"
    assert t2["service"] == "PTA"
    assert t1["token_no"] == t2["token_no"]

    # Exactly one row in the queue for this patient today.
    qr = requests.get(f"{API}/tokens", headers=h, timeout=15)
    assert qr.status_code == 200
    rows = [t for t in qr.json() if t.get("patient_id") == pid]
    assert len(rows) == 1, f"expected exactly 1 token, got {len(rows)}: {rows}"


def test_token_dedupe_skipped_once_completed(tokens, patient):
    """Completing the first token should allow a genuine follow-up token later."""
    pid = patient["patient_id"]
    h = _h(tokens["fd"])

    # Complete the existing active token.
    qr = requests.get(f"{API}/tokens", headers=h, timeout=15)
    mine = [t for t in qr.json() if t.get("patient_id") == pid]
    if mine:
        tid = mine[0]["token_id"]
        requests.put(f"{API}/tokens/{tid}/status", json={"status": "completed"},
                     headers=h, timeout=15)

    # Now a new token should actually be created (not dedupe-merged).
    r3 = requests.post(f"{API}/tokens", json={"patient_id": pid, "service": "Follow-up"},
                       headers=h, timeout=15)
    assert r3.status_code == 200
    t3 = r3.json()
    assert t3["service"] == "Follow-up"


# ------------------------------------------------------------------
# Simplified lifecycle (draft → completed).
# ------------------------------------------------------------------

@pytest.fixture
def session_for_patient(tokens, patient):
    r = requests.post(
        f"{API}/sessions",
        json={"patient_id": patient["patient_id"],
              "audiologist_name": "Test Audiologist",
              "test_reliability": "good",
              "test_methods": ["headphones"]},
        headers=_h(tokens["aud"]), timeout=15,
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["session_id"]


def test_generate_report_flips_to_completed(tokens, session_for_patient):
    """POST /sessions/{id}/generate-report → report_status becomes 'completed'."""
    r = requests.post(f"{API}/sessions/{session_for_patient}/generate-report",
                      headers=_h(tokens["aud"]), timeout=15)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["report_status"] == "completed"


def test_legacy_aliases_all_land_on_completed(tokens, patient):
    """mark-printed and complete-test should both flip to 'completed'."""
    # New session for each alias.
    for alias in ("mark-printed", "complete-test"):
        r = requests.post(
            f"{API}/sessions",
            json={"patient_id": patient["patient_id"],
                  "audiologist_name": "Test", "test_reliability": "good",
                  "test_methods": ["headphones"]},
            headers=_h(tokens["aud"]), timeout=15,
        )
        sid = r.json()["session_id"]
        r2 = requests.post(f"{API}/sessions/{sid}/{alias}",
                           headers=_h(tokens["aud"]), timeout=15)
        assert r2.status_code == 200, f"{alias} failed: {r2.text}"
        assert r2.json()["report_status"] == "completed", alias


def test_handover_endpoint_is_gone(tokens, session_for_patient):
    """POST /sessions/{id}/handover should 404 — feature was scrapped."""
    r = requests.post(f"{API}/sessions/{session_for_patient}/handover",
                      json={"channel": "in_person", "bypass_bill_check": True},
                      headers=_h(tokens["accounts"]), timeout=15)
    assert r.status_code == 404, f"handover should be 404 now, got {r.status_code}"


# ------------------------------------------------------------------
# PDF upload / GridFS retrieval.
# ------------------------------------------------------------------

def test_upload_pdf_and_retrieve(tokens, session_for_patient):
    """POST /sessions/{id}/report-pdf stores the blob; GET /reports/{id}/pdf returns it."""
    # Minimal valid PDF (the %PDF- magic byte check is all we care about).
    payload = b"%PDF-1.4\n%test payload\n%%EOF"
    files = {"file": ("report.pdf", payload, "application/pdf")}
    r = requests.post(
        f"{API}/sessions/{session_for_patient}/report-pdf",
        files=files, headers=_h(tokens["aud"]), timeout=20,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["size_bytes"] == len(payload)
    assert body["report_status"] == "completed"

    # Round-trip: the streamed PDF should be byte-identical to what we uploaded.
    g = requests.get(f"{API}/reports/{session_for_patient}/pdf",
                     headers=_h(tokens["aud"]), timeout=20)
    assert g.status_code == 200
    assert g.headers.get("content-type", "").startswith("application/pdf")
    assert g.content == payload, "stored PDF differs from uploaded PDF"


def test_upload_rejects_non_pdf(tokens, session_for_patient):
    files = {"file": ("not.pdf", b"<html>nope</html>", "application/pdf")}
    r = requests.post(
        f"{API}/sessions/{session_for_patient}/report-pdf",
        files=files, headers=_h(tokens["aud"]), timeout=15,
    )
    assert r.status_code == 415, f"expected 415, got {r.status_code}: {r.text}"


def test_upload_rejects_empty(tokens, session_for_patient):
    files = {"file": ("empty.pdf", b"", "application/pdf")}
    r = requests.post(
        f"{API}/sessions/{session_for_patient}/report-pdf",
        files=files, headers=_h(tokens["aud"]), timeout=15,
    )
    assert r.status_code == 400


# ------------------------------------------------------------------
# Listings and badges (back-compat stubs).
# ------------------------------------------------------------------

def test_reports_listing_only_completed(tokens):
    r = requests.get(f"{API}/reports?status=completed&per_page=5",
                     headers=_h(tokens["admin"]), timeout=15)
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    for row in body["items"]:
        assert row["report_status"] == "completed"
        assert "has_uploaded_pdf" in row
        assert "bill_paid" in row


def test_pending_count_always_zero(tokens):
    r = requests.get(f"{API}/reports/pending-count",
                     headers=_h(tokens["admin"]), timeout=15)
    assert r.status_code == 200
    assert r.json() == {"pending": 0}


def test_billing_pending_reports_empty(tokens):
    r = requests.get(f"{API}/billing/pending-reports",
                     headers=_h(tokens["admin"]), timeout=15)
    assert r.status_code == 200
    assert r.json() == []


# ------------------------------------------------------------------
# Appointment + draft invoice atomic endpoint still works.
# ------------------------------------------------------------------

def test_appointment_with_invoice_still_works(tokens, patient):
    """POST /appointments/with-invoice creates both in one call."""
    users = requests.get(f"{API}/users", headers=_h(tokens["admin"]), timeout=15).json()
    audiologist = next((u for u in users if u.get("role") == "audiologist"), None)
    assert audiologist, "no audiologist user seeded for this test clinic"

    # Random far-future slot to avoid conflicts with other running tests.
    from datetime import timedelta
    slot = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(
        days=random.randint(30, 60), hours=random.randint(0, 23),
        minutes=random.choice([0, 15, 30, 45]))
    start = slot.isoformat().replace("+00:00", "")
    payload = {
        "patient_id": patient["patient_id"],
        "audiologist_id": audiologist["user_id"],
        "service": "Pure Tone Audiometry",
        "start_at": start,
        "duration_minutes": 30,
        "visit_type": "walkin",
        "recommended_tests": ["pta"],
        "raise_invoice": True,
        "invoice_lines": [
            {"description": "PTA", "quantity": 1.0, "unit_price": 800.0,
             "discount_type": "flat", "discount_value": 0.0},
        ],
    }
    r = requests.post(f"{API}/appointments/with-invoice", json=payload,
                      headers=_h(tokens["fd"]), timeout=20)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["appointment"].get("appointment_id")
    inv = body["invoice"]
    assert inv and not inv.get("error"), f"invoice not created: {inv}"
    assert inv.get("invoice_id")
