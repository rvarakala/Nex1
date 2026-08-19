"""Phase 16.1 smoke: confirm dashboard endpoints still return 200 for owner."""
import os
import requests
import pytest

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://referral-sprint.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"

OWNER_EMAIL = "owner@thesoundclinic.in"
OWNER_PASSWORD = "demo123"


@pytest.fixture(scope="module")
def owner_token():
    r = requests.post(f"{API}/auth/login", json={"email": OWNER_EMAIL, "password": OWNER_PASSWORD}, timeout=15)
    if r.status_code != 200:
        pytest.skip(f"Owner login failed: {r.status_code} {r.text[:200]}")
    data = r.json()
    tok = data.get("access_token") or data.get("token")
    assert tok, f"no token in login response: {data}"
    return tok


@pytest.fixture(scope="module")
def headers(owner_token):
    return {"Authorization": f"Bearer {owner_token}"}


DASHBOARD_ENDPOINTS = [
    "/appointments",
    "/patients",
    "/sessions",
    "/billing/invoices",
    "/users",
    "/ha/service-tickets?status=ready_for_pickup",
    "/ha/accessory-stock?low_stock_only=true",
]


@pytest.mark.parametrize("path", DASHBOARD_ENDPOINTS)
def test_dashboard_endpoint_200(headers, path):
    r = requests.get(f"{API}{path}", headers=headers, timeout=20)
    assert r.status_code == 200, f"{path} -> {r.status_code}: {r.text[:200]}"
    # Sanity: response is JSON (list or dict), not HTML
    try:
        body = r.json()
    except Exception:
        pytest.fail(f"{path} -> non-JSON response: {r.text[:200]}")
    assert body is not None


def test_me_endpoint(headers):
    r = requests.get(f"{API}/auth/me", headers=headers, timeout=15)
    assert r.status_code == 200
    me = r.json()
    assert me.get("email") == OWNER_EMAIL
