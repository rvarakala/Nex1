"""Regression tests for perf sweep: bcrypt threadpool, gzip, indexes, backward-compat cost-12 hashes."""
import os
import time
import gzip as gzip_lib
import requests
import pytest
from pymongo import MongoClient

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://referral-sprint.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"

FOUNDER_EMAIL = "founder@audinexa.com"
FOUNDER_PASSWORD = "founder123"

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "test_database")


@pytest.fixture(scope="module")
def founder_token():
    r = requests.post(f"{API}/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD}, timeout=15)
    assert r.status_code == 200, f"Founder login failed: {r.status_code} {r.text}"
    data = r.json()
    assert "access_token" in data
    assert "user" in data
    assert "clinic" in data
    return data["access_token"]


# ---------- 1. Founder legacy cost-12 hash still validates ----------
def test_founder_login_legacy_cost12_hash():
    r = requests.post(f"{API}/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD}, timeout=15)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"]["email"] == FOUNDER_EMAIL
    assert body["access_token"]
    assert body.get("clinic") is not None


def test_founder_login_wrong_password_401():
    r = requests.post(f"{API}/auth/login", json={"email": FOUNDER_EMAIL, "password": "wrong-password-xyz"}, timeout=15)
    assert r.status_code in (400, 401), r.text


# ---------- 2. Signup uses asyncio.to_thread(hash_password) ----------
def test_public_clinic_signup_new_email():
    ts = int(time.time())
    email = f"perf-test-{ts}@example.com"
    payload = {
        "clinic_name": f"Perf Test Clinic {ts}",
        "owner_name": "Perf Tester",
        "owner_email": email,
        "owner_password": "PerfTest@123",
        "phone": "+919900112233",
        "city": "Bengaluru",
        "state": "Karnataka",
    }
    r = requests.post(f"{API}/public/clinic-signup", json=payload, timeout=30)
    # Accept 200 or 201; the important thing is not 500 (bcrypt threadpool wrap didn't break signup)
    assert r.status_code in (200, 201), f"Signup failed: {r.status_code} {r.text}"
    body = r.json()
    # Should not raise and should not be a 500 from asyncio.to_thread wiring
    assert "detail" not in body or r.status_code < 400


# ---------- 3. Change-password wrong current returns 401 ----------
def test_change_password_wrong_current(founder_token):
    r = requests.post(
        f"{API}/settings/me/change-password",
        headers={"Authorization": f"Bearer {founder_token}"},
        json={"current_password": "definitely-wrong-xyz", "new_password": "NewPass@12345"},
        timeout=15,
    )
    assert r.status_code == 401, f"Expected 401 got {r.status_code}: {r.text}"
    body = r.json()
    detail = str(body.get("detail", "")).lower()
    assert "current password" in detail or "incorrect" in detail


# ---------- 4. GZip compression active ----------
def test_gzip_compression_active():
    r = requests.get(
        f"{API}/subscription/tiers",
        headers={"Accept-Encoding": "gzip"},
        timeout=15,
    )
    assert r.status_code == 200, r.text
    # requests auto-decodes, but original encoding header is exposed
    ce = r.headers.get("Content-Encoding", "").lower()
    assert "gzip" in ce, f"Expected gzip Content-Encoding, got headers: {dict(r.headers)}"
    # Decoded body should still parse as JSON
    assert isinstance(r.json(), (list, dict))


def test_gzip_body_matches_plain():
    # Fetch with and without gzip and confirm decoded JSON matches
    r_gz = requests.get(f"{API}/subscription/tiers", headers={"Accept-Encoding": "gzip"}, timeout=15)
    r_pl = requests.get(f"{API}/subscription/tiers", headers={"Accept-Encoding": "identity"}, timeout=15)
    assert r_gz.status_code == 200 and r_pl.status_code == 200
    assert r_gz.json() == r_pl.json()


# ---------- 5. Indexes actually built ----------
def test_user_sessions_indexes_exist():
    client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)
    db = client[DB_NAME]
    idx = list(db.user_sessions.list_indexes())
    names = {i["name"] for i in idx}
    # required per problem statement
    assert "_id_" in names
    assert "session_id_1" in names, f"missing session_id_1; got {names}"
    # compound (user_id, revoked_at) — pymongo names them user_id_1_revoked_at_1
    assert any(n.startswith("user_id_1_revoked_at") for n in names), f"missing (user_id, revoked_at) compound; got {names}"
    # last_seen_at index (ascending or descending)
    assert any("last_seen_at" in n for n in names), f"missing last_seen_at index; got {names}"
    assert len(names) >= 4, f"expected >=4 indexes on user_sessions, got {names}"


def test_audit_log_indexes_exist():
    client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)
    db = client[DB_NAME]
    idx = list(db.audit_log.list_indexes())
    names = {i["name"] for i in idx}
    assert "_id_" in names
    assert any(n.startswith("target_1_at") for n in names), f"missing (target, at) compound; got {names}"
    assert any(n.startswith("action_1_at") for n in names), f"missing (action, at) compound; got {names}"
    assert len(names) >= 3, f"expected >=3 indexes on audit_log, got {names}"


def test_email_events_ttl_index_still_exists():
    client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)
    db = client[DB_NAME]
    idx = list(db.email_events.list_indexes())
    has_ttl = any("expireAfterSeconds" in i for i in idx)
    assert has_ttl, f"email_events TTL index missing; indexes={idx}"


# ---------- 6. Auth /me endpoint smoke ----------
def test_auth_me_smoke(founder_token):
    r = requests.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {founder_token}"}, timeout=15)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"]["email"] == FOUNDER_EMAIL
