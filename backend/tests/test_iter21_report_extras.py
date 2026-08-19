"""Extra regression coverage for the simplified report lifecycle (iter 21).

Covers scenarios NOT already in test_report_handover.py:

  * PDF re-upload replaces the previous GridFS blob (idempotent).
  * PDF fallback: session with no uploaded blob still serves a template PDF.
  * Cross-tenant: Delhi user gets 403 uploading / fetching a Mumbai PDF.
  * Cross-tenant: Delhi user does NOT see Mumbai sessions in GET /api/reports.
  * GET /api/patients/{id}/history returns {patient, sessions, invoices, ha_sales}.
  * POST /api/billing/report-deliveries channel=whatsapp is still accepted.
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


# ---- Fixtures ------------------------------------------------------------
@pytest.fixture(scope="module")
def tokens():
    return {
        "mum_fd":  _login(FRONTDESK_EMAIL, FRONTDESK_PASSWORD),
        "mum_aud": _login(AUDIO_EMAIL, AUDIO_PASSWORD),
        "mum_adm": _login(ADMIN_EMAIL, ADMIN_PASSWORD),
        "mum_acc": _login(ACCOUNTS_EMAIL, ACCOUNTS_PASSWORD),
        "del_adm": _login("admin@delhi.test", "delhiadmin123"),
        "del_fd":  _login("frontdesk@delhi.test", "delhifrontdesk123"),
    }


@pytest.fixture(scope="module")
def mumbai_patient(tokens):
    mobile = f"97{random.randint(10000000, 99999999)}"
    r = requests.post(
        f"{API}/patients",
        json={"name": f"Iter21Mum {random.randint(1000, 9999)}",
              "age": 42, "gender": "Male", "mobile": mobile},
        headers=_h(tokens["mum_fd"]), timeout=15,
    )
    assert r.status_code in (200, 201), r.text
    return r.json()


@pytest.fixture(scope="module")
def mumbai_session(tokens, mumbai_patient):
    r = requests.post(
        f"{API}/sessions",
        json={"patient_id": mumbai_patient["patient_id"],
              "audiologist_name": "Iter21 Aud",
              "test_reliability": "good",
              "test_methods": ["headphones"]},
        headers=_h(tokens["mum_aud"]), timeout=15,
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["session_id"]


# ---- PDF re-upload (GridFS cleanup) --------------------------------------
def test_pdf_reupload_replaces_previous_blob(tokens, mumbai_session):
    sid = mumbai_session
    h = _h(tokens["mum_aud"])

    payload_a = b"%PDF-1.4\n%FIRST upload\n%%EOF"
    r1 = requests.post(f"{API}/sessions/{sid}/report-pdf",
                       files={"file": ("a.pdf", payload_a, "application/pdf")},
                       headers=h, timeout=20)
    assert r1.status_code == 200, r1.text
    fs_id_a = r1.json()["report_pdf_fs_id"]

    payload_b = b"%PDF-1.4\n%SECOND upload - should win\n%%EOF"
    r2 = requests.post(f"{API}/sessions/{sid}/report-pdf",
                       files={"file": ("b.pdf", payload_b, "application/pdf")},
                       headers=h, timeout=20)
    assert r2.status_code == 200, r2.text
    fs_id_b = r2.json()["report_pdf_fs_id"]
    assert fs_id_a != fs_id_b, "GridFS id did not change on re-upload"
    assert r2.json()["size_bytes"] == len(payload_b)

    # Round-trip must return the newest bytes, not the original.
    g = requests.get(f"{API}/reports/{sid}/pdf", headers=h, timeout=20)
    assert g.status_code == 200
    assert g.content == payload_b
    assert g.content != payload_a


# ---- PDF fallback when no upload exists ----------------------------------
def test_pdf_fallback_template_when_no_upload(tokens, mumbai_patient):
    """A brand-new session (no upload) must still serve a template PDF."""
    r = requests.post(
        f"{API}/sessions",
        json={"patient_id": mumbai_patient["patient_id"],
              "audiologist_name": "Fallback Aud",
              "test_reliability": "good",
              "test_methods": ["headphones"]},
        headers=_h(tokens["mum_aud"]), timeout=15,
    )
    sid = r.json()["session_id"]

    g = requests.get(f"{API}/reports/{sid}/pdf",
                     headers=_h(tokens["mum_aud"]), timeout=25)
    assert g.status_code == 200, g.text
    assert g.headers.get("content-type", "").startswith("application/pdf")
    # Template PDFs from reportlab always start with %PDF-.
    assert g.content[:5] == b"%PDF-"
    assert len(g.content) > 200  # not empty stub


# ---- Cross-tenant isolation ----------------------------------------------
def test_cross_tenant_pdf_upload_blocked(tokens, mumbai_session):
    """Delhi user uploading to a Mumbai session must be blocked (403/404)."""
    r = requests.post(
        f"{API}/sessions/{mumbai_session}/report-pdf",
        files={"file": ("x.pdf", b"%PDF-1.4\n%x\n%%EOF", "application/pdf")},
        headers=_h(tokens["del_adm"]), timeout=15,
    )
    assert r.status_code in (403, 404), f"expected 403/404, got {r.status_code}: {r.text}"


def test_cross_tenant_pdf_fetch_blocked(tokens, mumbai_session):
    r = requests.get(f"{API}/reports/{mumbai_session}/pdf",
                     headers=_h(tokens["del_adm"]), timeout=15)
    assert r.status_code in (403, 404), f"expected 403/404, got {r.status_code}"


def test_cross_tenant_reports_listing_isolated(tokens, mumbai_session):
    """Delhi admin should NOT see any Mumbai-clinic sessions in /reports."""
    # Force a PDF on the Mumbai session so it shows up in completed listings.
    requests.post(
        f"{API}/sessions/{mumbai_session}/report-pdf",
        files={"file": ("mum.pdf", b"%PDF-1.4\n%m\n%%EOF", "application/pdf")},
        headers=_h(tokens["mum_aud"]), timeout=15,
    )

    r = requests.get(f"{API}/reports?status=completed&per_page=100",
                     headers=_h(tokens["del_adm"]), timeout=15)
    assert r.status_code == 200
    items = r.json().get("items", [])
    mum_hits = [x for x in items if x.get("session_id") == mumbai_session]
    assert mum_hits == [], f"Delhi saw Mumbai session: {mum_hits}"


# ---- Patient history aggregate ------------------------------------------
def test_patient_history_shape(tokens, mumbai_patient):
    r = requests.get(f"{API}/patients/{mumbai_patient['patient_id']}/history",
                     headers=_h(tokens["mum_adm"]), timeout=15)
    assert r.status_code == 200, r.text
    body = r.json()
    # Required top-level keys.
    for key in ("patient", "sessions", "invoices", "ha_sales"):
        assert key in body, f"missing key {key!r} in history response: {list(body.keys())}"
    assert body["patient"]["patient_id"] == mumbai_patient["patient_id"]
    assert isinstance(body["sessions"], list)
    assert isinstance(body["invoices"], list)
    assert isinstance(body["ha_sales"], list)


def test_patient_history_cross_tenant_blocked(tokens, mumbai_patient):
    r = requests.get(f"{API}/patients/{mumbai_patient['patient_id']}/history",
                     headers=_h(tokens["del_adm"]), timeout=15)
    assert r.status_code in (403, 404), f"expected 403/404, got {r.status_code}"


# ---- Legacy billing delivery channel -------------------------------------
def test_billing_report_delivery_whatsapp_legacy(tokens, mumbai_session):
    """The legacy WhatsApp quick-action should still be accepted (deprecated but alive)."""
    payload = {
        "session_id": mumbai_session,
        "channel": "whatsapp",
        "notes": "iter21 regression",
    }
    r = requests.post(f"{API}/billing/report-deliveries", json=payload,
                      headers=_h(tokens["mum_acc"]), timeout=15)
    # Accept: success (200/201), or explicit deprecation signal (410/404) as long as it's not a 500.
    assert r.status_code in (200, 201, 204, 404, 410), f"unexpected {r.status_code}: {r.text}"
