"""Phase 12.0 — Subscription, waitlist, module-gate coverage."""
import os
import uuid

import pytest
import requests



from _helpers import (  # legacy creds (env-overridable)
    ADMIN_EMAIL, ADMIN_PASSWORD,
    FRONTDESK_EMAIL, FRONTDESK_PASSWORD,
    AUDIO_EMAIL, AUDIO_PASSWORD,
    ACCOUNTS_EMAIL, ACCOUNTS_PASSWORD,
)
BASE_URL = os.environ.get("REACT_APP_BACKEND_URL",
                         "https://referral-sprint.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"


def _login(email, password):
    r = requests.post(f"{API}/auth/login",
                      json={"email": email, "password": password}, timeout=10)
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def h(tok):
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


@pytest.fixture(scope="module")
def admin_token():
    return _login(ADMIN_EMAIL, ADMIN_PASSWORD)


@pytest.fixture(scope="module")
def fd_token():
    return _login(FRONTDESK_EMAIL, FRONTDESK_PASSWORD)


class TestPublicTiers:
    def test_tiers_exposed(self):
        r = requests.get(f"{API}/subscription/tiers", timeout=10)
        assert r.status_code == 200, r.text
        d = r.json()
        codes = [t["code"] for t in d["tiers"]]
        assert codes == ["BASIC", "STANDARD", "PREMIUM"]
        # Pricing matrix present
        for t in d["tiers"]:
            assert {"annual", "half_yearly", "quarterly"} <= set(t["prices"].keys())
            assert t["prices"]["annual"] > 0
        assert d["trial_days"] == 30

    def test_waitlist_signup_idempotent(self):
        email = f"waitlist_{uuid.uuid4().hex[:8]}@test.in"
        r1 = requests.post(f"{API}/public/waitlist-signup",
                           json={"email": email, "clinic_name": "Test Clinic",
                                 "city": "Pune", "tier_interest": "PREMIUM"}, timeout=10)
        assert r1.status_code == 201, r1.text
        # Duplicate email → still 201 (upsert is idempotent)
        r2 = requests.post(f"{API}/public/waitlist-signup",
                           json={"email": email, "city": "Mumbai"}, timeout=10)
        assert r2.status_code == 201

    def test_waitlist_rejects_bad_email(self):
        r = requests.post(f"{API}/public/waitlist-signup",
                          json={"email": "not-an-email"}, timeout=10)
        assert r.status_code == 422

    def test_tiers_is_public_no_auth(self):
        r = requests.get(f"{API}/subscription/tiers")
        assert r.status_code == 200


class TestAuthenticatedSubscription:
    def test_my_subscription(self, admin_token):
        r = requests.get(f"{API}/subscription/my", headers=h(admin_token), timeout=10)
        assert r.status_code == 200
        d = r.json()
        assert d["effective_tier"] in ("BASIC", "STANDARD", "PREMIUM")
        assert "modules" in d

    def test_access_map_superadmin_bypass(self, admin_token):
        r = requests.get(f"{API}/subscription/access", headers=h(admin_token), timeout=10)
        assert r.status_code == 200
        d = r.json()
        assert d["super_admin_bypass"] is True
        # Super admin sees all modules as accessible regardless of clinic tier
        for m in ("frontdesk", "diagnostics", "hearing-aids", "repair", "analytics"):
            assert d["access"][m] is True

    def test_access_map_regular_user_follows_tier(self, fd_token):
        r = requests.get(f"{API}/subscription/access", headers=h(fd_token), timeout=10)
        assert r.status_code == 200
        d = r.json()
        assert d["super_admin_bypass"] is False
        # Demo clinic is PREMIUM → all modules accessible
        for m in ("frontdesk", "diagnostics", "hearing-aids", "repair", "analytics"):
            assert d["access"][m] is True


class TestAdminClinics:
    def test_list_requires_super_admin(self, fd_token):
        r = requests.get(f"{API}/admin/clinics", headers=h(fd_token), timeout=10)
        assert r.status_code == 403

    def test_list_as_super_admin(self, admin_token):
        r = requests.get(f"{API}/admin/clinics", headers=h(admin_token), timeout=10)
        assert r.status_code == 200
        rows = r.json()
        assert isinstance(rows, list) and len(rows) >= 1
        # Demo clinic was seeded PREMIUM
        demo = [c for c in rows if c["clinic_id"] == "clinic-pytest-suite"]
        assert len(demo) == 1
        assert demo[0]["subscription_tier"] == "PREMIUM"

    def test_flip_tier_and_revert(self, admin_token):
        # Flip demo clinic to BASIC then back to PREMIUM
        r = requests.patch(f"{API}/admin/clinics/clinic-pytest-suite/tier",
                           headers=h(admin_token),
                           json={"subscription_tier": "STANDARD"}, timeout=10)
        assert r.status_code == 200
        r = requests.patch(f"{API}/admin/clinics/clinic-pytest-suite/tier",
                           headers=h(admin_token),
                           json={"subscription_tier": "PREMIUM"}, timeout=10)
        assert r.status_code == 200
        # Invalid tier rejected
        r = requests.patch(f"{API}/admin/clinics/clinic-pytest-suite/tier",
                           headers=h(admin_token),
                           json={"subscription_tier": "DIAMOND"}, timeout=10)
        assert r.status_code == 400

    def test_flip_unknown_clinic_404(self, admin_token):
        r = requests.patch(f"{API}/admin/clinics/clinic-nope/tier",
                           headers=h(admin_token),
                           json={"subscription_tier": "BASIC"}, timeout=10)
        assert r.status_code == 404

    def test_extend_trial(self, admin_token):
        r = requests.post(f"{API}/admin/clinics/clinic-pytest-suite/extend-trial?days=30",
                          headers=h(admin_token), timeout=10)
        assert r.status_code == 200
        assert r.json()["days"] == 30

    def test_waitlist_list(self, admin_token):
        r = requests.get(f"{API}/admin/waitlist", headers=h(admin_token), timeout=10)
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_waitlist_csv_export(self, admin_token):
        r = requests.get(f"{API}/admin/waitlist/export.csv",
                         headers=h(admin_token), timeout=10)
        assert r.status_code == 200
        assert "text/csv" in r.headers.get("content-type", "")
        body = r.text
        assert "email" in body.split("\n")[0]  # header row present
