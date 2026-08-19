"""Phase 12.1 — public clinic self-signup flow."""
import os
import uuid

import pytest
import requests


BASE_URL = os.environ.get("REACT_APP_BACKEND_URL",
                         "https://referral-sprint.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"


def h(tok):
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


@pytest.fixture
def fresh_signup_body():
    uid = uuid.uuid4().hex[:8]
    return {
        "clinic_name": f"Test Clinic {uid}",
        "city": "Pune",
        "state": "Maharashtra",
        "phone": "+91-20-00000000",
        "owner_name": f"Dr. Test {uid[:4]}",
        "owner_email": f"owner_{uid}@test.in",
        "owner_password": "password1234",
    }


class TestClinicSignup:
    def test_happy_path_auto_login_with_trial(self, fresh_signup_body):
        r = requests.post(f"{API}/public/clinic-signup",
                          json=fresh_signup_body, timeout=15)
        assert r.status_code == 201, r.text
        d = r.json()
        # Core identifiers returned
        assert d["clinic_id"].startswith("clinic-")
        assert d["user_id"].startswith("USR-")
        assert d["branch_id"].startswith("BR-")
        assert d["access_token"]
        assert d["stored_tier"] == "BASIC"
        assert d["effective_tier"] == "PREMIUM"   # during trial window
        assert d["trial_days"] == 30

        # Token is a real JWT that works against /auth/me
        me = requests.get(f"{API}/auth/me", headers=h(d["access_token"]), timeout=10)
        assert me.status_code == 200, me.text
        me_body = me.json()
        assert me_body["user"]["email"] == fresh_signup_body["owner_email"]
        assert me_body["user"]["role"] == "clinic_owner"
        assert me_body["user"]["clinic_id"] == d["clinic_id"]

        # Subscription endpoint reflects trial-PREMIUM
        sub = requests.get(f"{API}/subscription/my", headers=h(d["access_token"]), timeout=10)
        assert sub.status_code == 200
        sub_body = sub.json()
        assert sub_body["effective_tier"] == "PREMIUM"
        assert sub_body["trial_active"] is True
        assert sub_body["trial_days_left"] in (29, 30)  # depending on UTC boundary

        # Primary branch was auto-created
        br = requests.get(f"{API}/branches", headers=h(d["access_token"]), timeout=10)
        assert br.status_code == 200
        branches = br.json()
        assert len(branches) == 1
        assert branches[0]["branch_id"] == d["branch_id"]
        assert branches[0]["is_primary"] is True

    def test_duplicate_email_409(self, fresh_signup_body):
        r1 = requests.post(f"{API}/public/clinic-signup",
                           json=fresh_signup_body, timeout=15)
        assert r1.status_code == 201
        # Same email, different clinic
        body2 = {**fresh_signup_body, "clinic_name": "Different Clinic Name"}
        r2 = requests.post(f"{API}/public/clinic-signup", json=body2, timeout=15)
        assert r2.status_code == 409
        assert "already exists" in r2.text.lower()

    def test_weak_password_422(self, fresh_signup_body):
        body = {**fresh_signup_body, "owner_password": "short"}
        r = requests.post(f"{API}/public/clinic-signup", json=body, timeout=10)
        assert r.status_code == 422

    def test_bad_email_422(self, fresh_signup_body):
        body = {**fresh_signup_body, "owner_email": "not-an-email"}
        r = requests.post(f"{API}/public/clinic-signup", json=body, timeout=10)
        assert r.status_code == 422

    def test_short_clinic_name_422(self, fresh_signup_body):
        body = {**fresh_signup_body, "clinic_name": "X"}
        r = requests.post(f"{API}/public/clinic-signup", json=body, timeout=10)
        assert r.status_code == 422

    def test_honeypot_blocked(self, fresh_signup_body):
        body = {**fresh_signup_body, "company_url": "http://spam.ru"}
        r = requests.post(f"{API}/public/clinic-signup", json=body, timeout=10)
        assert r.status_code == 400

    def test_new_clinic_sees_all_modules_during_trial(self, fresh_signup_body):
        r = requests.post(f"{API}/public/clinic-signup",
                          json=fresh_signup_body, timeout=15)
        tok = r.json()["access_token"]
        a = requests.get(f"{API}/subscription/access",
                         headers=h(tok), timeout=10).json()
        # During trial, effective tier is PREMIUM → every module accessible
        for m in ("frontdesk", "diagnostics", "hearing-aids", "repair", "analytics"):
            assert a["access"][m] is True, f"module {m} blocked during trial"
        # And owner is NOT super_admin (bypass=False) — tier check is what granted access
        assert a["super_admin_bypass"] is False

    def test_endpoint_is_public(self, fresh_signup_body):
        """No auth header → 201 (not 401)."""
        r = requests.post(f"{API}/public/clinic-signup",
                          json=fresh_signup_body, timeout=10)
        # Should be 201 or 409 (if email collision) — never 401
        assert r.status_code in (201, 409), r.text
