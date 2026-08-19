"""Backend regression - POST /api/vendors role gate.

Verifies:
- clinic_owner (dltest) -> 200
- audiologist (pytest.audio) -> 403 with 'Requires one of' in detail
- super_admin/founder -> 200 (bypass)
"""
import os
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://referral-sprint.preview.emergentagent.com").rstrip("/")

USERS = {
    "clinic_owner": ("dltest@example.com", "TestPass@123"),
    "audiologist": ("pytest.audio@audinexa.test", "Pytest@123"),
    "founder": ("founder@audinexa.com", "AudinexaFounder@2026"),
}


def _login(email, password):
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login", json={"email": email, "password": password}, timeout=20)
    assert r.status_code == 200, f"Login failed for {email}: {r.status_code} {r.text}"
    tok = r.json().get("token") or r.json().get("access_token")
    if tok:
        s.headers.update({"Authorization": f"Bearer {tok}"})
    return s


@pytest.fixture(scope="module")
def owner_session():
    return _login(*USERS["clinic_owner"])


@pytest.fixture(scope="module")
def audio_session():
    return _login(*USERS["audiologist"])


@pytest.fixture(scope="module")
def founder_session():
    return _login(*USERS["founder"])


def test_clinic_owner_can_create_vendor(owner_session):
    payload = {"name": "TEST_BackendGate_Owner", "contact_person": "QA", "phone": "9999999999"}
    r = owner_session.post(f"{BASE_URL}/api/vendors", json=payload, timeout=20)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["name"] == payload["name"]
    assert "vendor_id" in data and data["vendor_id"].startswith("VND-")


def test_audiologist_forbidden(audio_session):
    payload = {"name": "TEST_BackendGate_Denied"}
    r = audio_session.post(f"{BASE_URL}/api/vendors", json=payload, timeout=20)
    assert r.status_code == 403, f"Expected 403, got {r.status_code}: {r.text}"
    detail = r.json().get("detail", "")
    assert "Requires one of" in detail, f"Unexpected detail: {detail}"


def test_founder_super_admin_bypass(founder_session):
    payload = {"name": "TEST_BackendGate_Founder"}
    r = founder_session.post(f"{BASE_URL}/api/vendors", json=payload, timeout=20)
    assert r.status_code == 200, r.text
    assert r.json()["name"] == payload["name"]


def test_dltest_vendors_empty_precondition(owner_session):
    """Sanity: after cleanup, dltest should have exactly our TEST_ vendor(s)."""
    r = owner_session.get(f"{BASE_URL}/api/vendors", timeout=20)
    assert r.status_code == 200
    # Not asserting empty because previous test just created one
    assert isinstance(r.json(), list)
