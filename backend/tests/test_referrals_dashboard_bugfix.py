"""Regression test for Sound Clinic referral dashboard 500 (KeyError diagnostics_payout)."""
import os
import requests
import pytest

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://referral-sprint.preview.emergentagent.com").rstrip("/")

REQUIRED_ROW_KEYS = {
    "doctor_id", "name", "diagnostics_revenue", "ha_sales_revenue",
    "diagnostics_payout", "ha_payout", "total_payout", "total_revenue",
    "patient_count", "diag_patient_count", "ha_patient_count",
}


def _login(email: str, password: str) -> str:
    r = requests.post(f"{BASE_URL}/api/auth/login", json={"email": email, "password": password}, timeout=20)
    assert r.status_code == 200, f"login failed for {email}: {r.status_code} {r.text}"
    tok = r.json().get("access_token") or r.json().get("token")
    assert tok, f"no token in login response: {r.json()}"
    return tok


def _auth_headers(token: str):
    return {"Authorization": f"Bearer {token}"}


def test_sound_clinic_dashboard_returns_200_with_full_rows():
    token = _login("owner@thesoundclinic.in", "demo123")
    r = requests.get(f"{BASE_URL}/api/referrals/dashboard", headers=_auth_headers(token), timeout=30)
    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text[:500]}"
    data = r.json()
    assert "totals" in data
    assert "rows" in data
    assert isinstance(data["rows"], list)
    # sound clinic seeded 4 referring doctors
    assert len(data["rows"]) >= 1, "expected at least one referring doctor row"
    for row in data["rows"]:
        missing = REQUIRED_ROW_KEYS - set(row.keys())
        assert not missing, f"row missing keys {missing}: {row}"
        # All should be zero since no patients linked
        assert row["total_payout"] == 0
        assert row["total_revenue"] == 0
        assert row["patient_count"] == 0


def test_sound_clinic_pathways_still_200():
    token = _login("owner@thesoundclinic.in", "demo123")
    r = requests.get(f"{BASE_URL}/api/referrals/pathways", headers=_auth_headers(token), timeout=30)
    assert r.status_code == 200, f"pathways expected 200, got {r.status_code}: {r.text[:500]}"


def test_dltest_regression_dashboard_still_works():
    token = _login("dltest@example.com", "TestPass@123")
    r = requests.get(f"{BASE_URL}/api/referrals/dashboard", headers=_auth_headers(token), timeout=30)
    assert r.status_code == 200, f"dltest dashboard: {r.status_code} {r.text[:500]}"
    data = r.json()
    assert "rows" in data
    for row in data["rows"]:
        missing = REQUIRED_ROW_KEYS - set(row.keys())
        assert not missing, f"row missing keys {missing}: {row}"
    # Ensure some non-zero payout exists (regression tenant has linked patients + paid invoices)
    total_payout = sum(row["total_payout"] for row in data["rows"])
    assert total_payout > 0, f"expected some payout for dltest tenant, rows={data['rows']}"
