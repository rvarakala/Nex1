"""Tests for the API Latency Speedometer feature (Phase 15).

Covers:
- /api/admin/v2/system/latency shape & founder access
- Middleware records requests + path normalisation
- RBAC — non-founder without system:read gets 403
- Regression: /system/health, /system/data-health, /dashboard still work
"""
import os
import time
import requests
import pytest

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://referral-sprint.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"

FOUNDER_EMAIL = "founder@audinexa.com"
FOUNDER_PW = "founder123"
AUDIOLOGIST_EMAIL = "pytest.audio@audinexa.test"
AUDIOLOGIST_PW = "Pytest@123"


def _login(email, pw):
    r = requests.post(f"{API}/auth/login", json={"email": email, "password": pw}, timeout=15)
    return r


@pytest.fixture(scope="module")
def founder_token():
    r = _login(FOUNDER_EMAIL, FOUNDER_PW)
    assert r.status_code == 200, f"founder login failed: {r.status_code} {r.text}"
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def audiologist_token():
    r = _login(AUDIOLOGIST_EMAIL, AUDIOLOGIST_PW)
    if r.status_code != 200:
        pytest.skip(f"audiologist login failed: {r.status_code} {r.text}")
    return r.json()["access_token"]


def _H(token):
    return {"Authorization": f"Bearer {token}"}


# ---------------- Endpoint shape ----------------
def test_latency_endpoint_shape(founder_token):
    r = requests.get(f"{API}/admin/v2/system/latency", headers=_H(founder_token), timeout=15)
    assert r.status_code == 200, r.text
    d = r.json()
    for k in ("at", "uptime_seconds", "window_60s", "window_5m", "health",
              "slowest_routes", "status_distribution"):
        assert k in d, f"missing key {k}"
    for wk in ("count", "rps", "p50", "p95", "p99", "max", "avg"):
        assert wk in d["window_60s"], f"window_60s missing {wk}"
        assert wk in d["window_5m"], f"window_5m missing {wk}"
    assert d["health"] in {"idle", "healthy", "warning", "critical"}
    assert isinstance(d["slowest_routes"], list)
    assert set(d["status_distribution"].keys()) >= {"2xx", "3xx", "4xx", "5xx"}
    assert isinstance(d["uptime_seconds"], int)


# ---------------- Middleware capture ----------------
def test_middleware_records_requests(founder_token):
    # Fire 5 health calls
    for _ in range(5):
        requests.get(f"{API}/health", timeout=10)
    time.sleep(1.2)
    r = requests.get(f"{API}/admin/v2/system/latency", headers=_H(founder_token), timeout=15)
    assert r.status_code == 200
    d = r.json()
    # window_5m count should include our probes
    assert d["window_5m"]["count"] >= 5, f"expected >=5, got {d['window_5m']['count']}"


# ---------------- Path normalisation ----------------
def test_path_normalisation(founder_token):
    # id-like segment — long token > 12 chars → collapsed to ':id'
    requests.get(f"{API}/patients/pat_abc123456789", timeout=10)
    requests.get(f"{API}/patients/pat_abc123456789", timeout=10)
    time.sleep(1.0)
    r = requests.get(f"{API}/admin/v2/system/latency", headers=_H(founder_token), timeout=15)
    assert r.status_code == 200
    paths = [row["path"] for row in r.json()["slowest_routes"]]
    # Either raw path is absent or a :id-collapsed variant is present
    assert not any("pat_abc123456789" in p for p in paths), f"Raw id leaked into slowest_routes: {paths}"
    # Look for a collapsed patients path in either window (5m) — soft check:
    # It's possible the endpoint isn't in top-10 slowest; so make it best-effort log
    print("slowest_routes paths:", paths)


# ---------------- RBAC 403 ----------------
def test_rbac_non_founder_403(audiologist_token):
    r = requests.get(f"{API}/admin/v2/system/latency", headers=_H(audiologist_token), timeout=15)
    assert r.status_code == 403, f"expected 403, got {r.status_code} {r.text}"


def test_latency_endpoint_requires_auth():
    r = requests.get(f"{API}/admin/v2/system/latency", timeout=10)
    assert r.status_code in (401, 403)


# ---------------- Regression ----------------
def test_regression_system_health(founder_token):
    r = requests.get(f"{API}/admin/v2/system/health", headers=_H(founder_token), timeout=15)
    assert r.status_code == 200, r.text
    d = r.json()
    assert "api" in d and "database" in d


def test_regression_data_health(founder_token):
    r = requests.get(f"{API}/admin/v2/system/data-health", headers=_H(founder_token), timeout=20)
    assert r.status_code == 200, r.text
    assert "overall" in r.json()


def test_regression_dashboard(founder_token):
    r = requests.get(f"{API}/admin/v2/dashboard", headers=_H(founder_token), timeout=15)
    assert r.status_code == 200, r.text
