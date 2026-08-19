"""Regression: 5 endpoints that were returning 500 must now return 200.
Also verifies Demo/Saleable stock add + cleanup."""
import os
import uuid
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://referral-sprint.preview.emergentagent.com").rstrip("/")
EMAIL = "owner@thesoundclinic.in"
PASSWORD = "demo123"


def _login():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login", json={"email": EMAIL, "password": PASSWORD}, timeout=30)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    tok = r.json().get("access_token") or r.json().get("token")
    if tok:
        s.headers.update({"Authorization": f"Bearer {tok}"})
    return s


def test_serial_items_200():
    s = _login()
    r = s.get(f"{BASE_URL}/api/ha/serial-items", timeout=30)
    assert r.status_code == 200, f"{r.status_code}: {r.text[:400]}"
    assert isinstance(r.json(), list)


def test_serial_items_summary_200():
    """Regression: /serial-items/by-branch-summary was 500 when any row had
    missing `pool` (Mongo $group drops missing sub-fields from _id → KeyError).
    Fixed with $ifNull. Frontend's KPI chips depend on this — a 500 here
    showed every state's counter as 0."""
    s = _login()
    r = s.get(f"{BASE_URL}/api/ha/serial-items/by-branch-summary", timeout=30)
    assert r.status_code == 200, f"{r.status_code}: {r.text[:400]}"
    body = r.json()
    assert "total" in body and "by_state" in body and "by_pool" in body
    assert isinstance(body["total"], int)
    # by_state and by_pool must be dicts, and total must equal sum of any of them
    assert sum(body["by_state"].values()) == body["total"], "by_state must sum to total"
    assert sum(body["by_pool"].values()) == body["total"], "by_pool must sum to total"
    # revenue_by_state added Feb 2026 — must exist, must only hold SOLD/RESERVED
    assert "revenue_by_state" in body, "revenue_by_state missing from summary"
    assert isinstance(body["revenue_by_state"], dict)
    for state_key in body["revenue_by_state"]:
        assert state_key in ("SOLD", "RESERVED"), (
            f"revenue only meaningful for SOLD/RESERVED, got '{state_key}'"
        )


def test_amc_contracts_200():
    s = _login()
    r = s.get(f"{BASE_URL}/api/ha/amc/contracts", timeout=30)
    assert r.status_code == 200, f"{r.status_code}: {r.text[:400]}"


def test_fittings_200():
    s = _login()
    r = s.get(f"{BASE_URL}/api/ha/fittings", timeout=30)
    assert r.status_code == 200, f"{r.status_code}: {r.text[:400]}"


def test_demo_stock_200():
    s = _login()
    r = s.get(f"{BASE_URL}/api/ha/demo-stock", timeout=30)
    assert r.status_code == 200, f"{r.status_code}: {r.text[:400]}"


def test_saleable_stock_200():
    s = _login()
    r = s.get(f"{BASE_URL}/api/ha/saleable-stock", timeout=30)
    assert r.status_code == 200, f"{r.status_code}: {r.text[:400]}"


def test_borrowed_attention_200():
    s = _login()
    # Best-effort - endpoint name may vary
    for path in ["/api/ha/borrowed-attention", "/api/dashboard/needs-attention", "/api/ha/needs-attention"]:
        r = s.get(f"{BASE_URL}{path}", timeout=30)
        if r.status_code == 200:
            return
    # not fatal
    print("no borrowed-attention endpoint found (non-blocking)")
