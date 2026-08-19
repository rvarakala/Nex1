"""E2E device limit tests against preview URL."""
import os
import requests
import pytest

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', 'https://referral-sprint.preview.emergentagent.com').rstrip('/')

DL_EMAIL = "dltest@example.com"
DL_PASS = "TestPass@123"
FOUNDER_EMAIL = "founder@audinexa.com"
FOUNDER_PASS = "founder123"


def _login(email, pw, ua="pytest-e2e/1.0", replace_session_id=None):
    body = {"email": email, "password": pw}
    if replace_session_id:
        body["replace_session_id"] = replace_session_id
    r = requests.post(f"{BASE_URL}/api/auth/login", json=body, headers={"User-Agent": ua})
    return r


def test_founder_unlimited_and_device_limit_endpoint():
    r = _login(FOUNDER_EMAIL, FOUNDER_PASS, ua="pytest-founder/1.0")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "device_limit" in body, "login response must include device_limit"
    dl = body["device_limit"]
    assert dl["action"] == "allow"
    token = body.get("access_token") or body.get("token")
    assert token
    r2 = requests.get(f"{BASE_URL}/api/auth/sessions/device-limit",
                      headers={"Authorization": f"Bearer {token}"})
    assert r2.status_code == 200, r2.text
    j = r2.json()
    assert j["unlimited"] is True
    assert j["at_limit"] is False
    assert j["cap"] >= 9999


def test_dltest_basic_tier_and_third_login_warn_mode():
    # Login 1
    r1 = _login(DL_EMAIL, DL_PASS, ua="ua-alpha/1.0")
    assert r1.status_code == 200, r1.text
    b1 = r1.json()
    assert "device_limit" in b1
    dl1 = b1["device_limit"]
    assert dl1["cap"] == 2
    tok1 = b1.get("access_token") or b1.get("token")

    # Check device-limit endpoint
    r_dl = requests.get(f"{BASE_URL}/api/auth/sessions/device-limit",
                        headers={"Authorization": f"Bearer {tok1}"})
    assert r_dl.status_code == 200
    j_dl = r_dl.json()
    assert j_dl["cap"] == 2
    assert j_dl["tier"] == "BASIC"
    assert j_dl["unlimited"] is False

    # Login 2 & 3 different UA
    r2 = _login(DL_EMAIL, DL_PASS, ua="ua-beta/1.0")
    assert r2.status_code == 200, r2.text
    r3 = _login(DL_EMAIL, DL_PASS, ua="ua-gamma/1.0")
    # Warn mode: 3rd should succeed
    assert r3.status_code == 200, f"warn mode should not block: {r3.status_code} {r3.text}"
    b3 = r3.json()
    dl3 = b3["device_limit"]
    assert dl3["action"] == "warn", f"expected warn, got {dl3}"
    assert dl3["count"] >= 3
    assert dl3["cap"] == 2


def test_replace_session_id_flow():
    # Login to get token & list sessions
    r = _login(DL_EMAIL, DL_PASS, ua="ua-replace-driver/1.0")
    assert r.status_code == 200
    tok = r.json().get("access_token") or r.json().get("token")
    # Reset state: revoke all other sessions so we start clean
    requests.post(f"{BASE_URL}/api/auth/sessions/revoke-others",
                  headers={"Authorization": f"Bearer {tok}"})
    # Create one more session
    r_extra = _login(DL_EMAIL, DL_PASS, ua="ua-replace-victim/1.0")
    assert r_extra.status_code == 200
    # get sessions
    r_s = requests.get(f"{BASE_URL}/api/auth/sessions",
                       headers={"Authorization": f"Bearer {tok}"})
    assert r_s.status_code == 200, r_s.text
    sessions = r_s.json()
    if isinstance(sessions, dict):
        sessions = sessions.get("sessions", sessions.get("items", []))
    assert len(sessions) >= 1
    # pick a session that's not the current one
    target = None
    for s in sessions:
        if not s.get("current"):
            target = s.get("session_id") or s.get("sid") or s.get("id")
            if target:
                break
    if not target:
        pytest.skip("no non-current session to replace")

    # Login again with replace_session_id
    r_repl = _login(DL_EMAIL, DL_PASS, ua="ua-replace-new/1.0", replace_session_id=target)
    assert r_repl.status_code == 200, r_repl.text
    body = r_repl.json()
    assert body["device_limit"].get("replaced") == target
    tok2 = body.get("access_token") or body.get("token")
    # Verify old session gone
    r_s2 = requests.get(f"{BASE_URL}/api/auth/sessions",
                        headers={"Authorization": f"Bearer {tok2}"})
    assert r_s2.status_code == 200
    sessions2 = r_s2.json()
    if isinstance(sessions2, dict):
        sessions2 = sessions2.get("sessions", sessions2.get("items", []))
    ids = [s.get("session_id") or s.get("sid") or s.get("id") for s in sessions2]
    assert target not in ids, f"revoked sid still in list: {ids}"
