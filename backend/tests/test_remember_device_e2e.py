"""E2E tests for remember_device feature via public URL."""
import os
import requests
import pytest

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://referral-sprint.preview.emergentagent.com").rstrip("/")
EMAIL = "dltest@example.com"
PWD = "TestPass@123"


def _login(remember=None, ua="pytest-ua-default"):
    payload = {"email": EMAIL, "password": PWD}
    if remember is not None:
        payload["remember_device"] = remember
    r = requests.post(f"{BASE_URL}/api/auth/login", json=payload,
                      headers={"User-Agent": ua})
    return r


def test_login_default_remember_true_counts():
    # baseline
    r0 = _login(remember=None, ua="baseline-ua")
    assert r0.status_code == 200, r0.text
    tok = r0.json().get("access_token")
    dl0 = requests.get(f"{BASE_URL}/api/auth/sessions/device-limit",
                       headers={"Authorization": f"Bearer {tok}"}).json()
    baseline = dl0.get("count", 0)

    # default login (no remember_device field)
    r = _login(remember=None, ua="default-remember-ua")
    assert r.status_code == 200
    body = r.json()
    dl = body.get("device_limit", {})
    # allow or warn are both "counted" outcomes (not ephemeral)
    assert dl.get("action") in ("allow", "warn"), f"expected allow/warn got {dl}"
    assert dl.get("ephemeral") in (False, None), f"ephemeral should be false, got {dl}"

    tok2 = body["access_token"]
    dl_after = requests.get(f"{BASE_URL}/api/auth/sessions/device-limit",
                            headers={"Authorization": f"Bearer {tok2}"}).json()
    assert dl_after["count"] >= baseline, f"count should not decrease: {baseline} -> {dl_after['count']}"

    # sessions list contains remember_device=true entry
    ss = requests.get(f"{BASE_URL}/api/auth/sessions",
                      headers={"Authorization": f"Bearer {tok2}"}).json()
    rows = ss if isinstance(ss, list) else ss.get("sessions", [])
    assert any(row.get("remember_device") is True for row in rows), rows


def test_login_ephemeral_does_not_count():
    # baseline via remembered login
    r0 = _login(remember=True, ua="ephem-baseline")
    tok = r0.json()["access_token"]
    dl0 = requests.get(f"{BASE_URL}/api/auth/sessions/device-limit",
                       headers={"Authorization": f"Bearer {tok}"}).json()
    baseline = dl0["count"]

    # ephemeral login
    r = _login(remember=False, ua="ephemeral-ua-1")
    assert r.status_code == 200
    body = r.json()
    dl = body.get("device_limit", {})
    assert dl.get("action") == "allow_ephemeral", dl
    assert dl.get("ephemeral") is True, dl

    tok2 = body["access_token"]
    dl_after = requests.get(f"{BASE_URL}/api/auth/sessions/device-limit",
                            headers={"Authorization": f"Bearer {tok2}"}).json()
    assert dl_after["count"] == baseline, f"ephemeral changed count {baseline}->{dl_after['count']}"

    # sessions list has ephemeral row
    ss = requests.get(f"{BASE_URL}/api/auth/sessions",
                      headers={"Authorization": f"Bearer {tok2}"}).json()
    rows = ss if isinstance(ss, list) else ss.get("sessions", [])
    assert any(row.get("remember_device") is False for row in rows), rows


def test_ephemeral_multiple_logins_do_not_increase_count():
    r0 = _login(remember=True, ua="many-ephem-baseline")
    tok = r0.json()["access_token"]
    baseline = requests.get(f"{BASE_URL}/api/auth/sessions/device-limit",
                            headers={"Authorization": f"Bearer {tok}"}).json()["count"]
    for i in range(5):
        rr = _login(remember=False, ua=f"ephemeral-multi-{i}")
        assert rr.status_code == 200
    after = requests.get(f"{BASE_URL}/api/auth/sessions/device-limit",
                         headers={"Authorization": f"Bearer {tok}"}).json()["count"]
    assert after == baseline, f"ephemerals should not increment count {baseline}->{after}"


def test_cookie_max_age_differs_by_remember():
    r_rem = requests.post(f"{BASE_URL}/api/auth/login",
                          json={"email": EMAIL, "password": PWD, "remember_device": True},
                          headers={"User-Agent": "cookie-rem"})
    assert r_rem.status_code == 200
    set_cookie_rem = r_rem.headers.get("set-cookie", "") or ""
    assert "access_token=" in set_cookie_rem.lower()
    assert "max-age=2592000" in set_cookie_rem.lower(), set_cookie_rem

    r_eph = requests.post(f"{BASE_URL}/api/auth/login",
                          json={"email": EMAIL, "password": PWD, "remember_device": False},
                          headers={"User-Agent": "cookie-eph"})
    assert r_eph.status_code == 200
    set_cookie_eph = r_eph.headers.get("set-cookie", "") or ""
    assert "max-age=28800" in set_cookie_eph.lower(), set_cookie_eph
