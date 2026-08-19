"""Regression: Bulk Duplicate Sweep.

The clinic owner opens `/patients/duplicates` to see every phone/name
collision in one screen and merge them inline. This locks the API
contract used by that screen — group shape, activity counts, key
matrix (phone_and_name / phone_only / name_only), and merged-out rows
being excluded so a cleaned-up collision doesn't come back to haunt.
"""
import os
import uuid
import requests

BASE_URL = os.environ.get(
    "REACT_APP_BACKEND_URL",
    "https://referral-sprint.preview.emergentagent.com",
).rstrip("/")
EMAIL = "owner@thesoundclinic.in"
PASSWORD = "demo123"


def _sess():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": EMAIL, "password": PASSWORD}, timeout=30)
    assert r.status_code == 200, r.text
    s.headers.update({"Authorization": f"Bearer {r.json()['access_token']}"})
    return s


def _seed_dup_pair(s, phone: str, name: str):
    """Two patients that will collide on both phone AND name. The second
    create is intentional so pass `?allow_duplicate_phone=true` — that's
    the real path an audiologist takes when the collision is a data-
    entry mistake they'll clean up via the sweep."""
    r1 = s.post(f"{BASE_URL}/api/patients", json={
        "name": name, "mobile": phone, "age": 42, "gender": "male",
    }, timeout=15)
    assert r1.status_code == 200, r1.text
    r2 = s.post(f"{BASE_URL}/api/patients?allow_duplicate_phone=true", json={
        "name": name, "mobile": phone, "age": 42, "gender": "male",
    }, timeout=15)
    assert r2.status_code == 200, r2.text
    return r1.json()["patient_id"], r2.json()["patient_id"]


def test_duplicates_phone_and_name_groups_are_returned():
    """Two rows with identical phone + name → one collision group of
    size 2 with the expected shape."""
    s = _sess()
    phone = f"9{uuid.uuid4().int % 10**9:09d}"
    name = f"PyTestDup {uuid.uuid4().hex[:6]}"
    p1, p2 = _seed_dup_pair(s, phone, name)
    try:
        r = s.get(f"{BASE_URL}/api/patients/duplicates",
                  params={"key": "phone_and_name", "min_group": 2}, timeout=20)
        assert r.status_code == 200, r.text
        data = r.json()
        group = next((g for g in data["groups"] if g["key"]["phone"] == phone), None)
        assert group is not None, "duplicate group must include our seeded pair"
        assert group["count"] == 2
        ids = {p["patient_id"] for p in group["patients"]}
        assert {p1, p2}.issubset(ids)
        # Activity counts must be present per patient (owner UI relies
        # on them to pick which record to keep).
        for p in group["patients"]:
            assert "counts" in p
            for k in ("sessions", "invoices", "appointments"):
                assert k in p["counts"]
    finally:
        # Clean up so re-runs stay predictable.
        for pid in (p1, p2):
            try:
                s.delete(f"{BASE_URL}/api/patients/{pid}", timeout=10)
            except Exception:
                pass


def test_duplicates_ignores_already_merged_rows():
    """Once we merge secondary → primary, the group must disappear from
    the sweep on the next scan. The screen mustn't nag the owner to
    re-merge a row they already cleaned up."""
    s = _sess()
    phone = f"9{uuid.uuid4().int % 10**9:09d}"
    name = f"PyTestMerged {uuid.uuid4().hex[:6]}"
    p1, p2 = _seed_dup_pair(s, phone, name)
    try:
        r = s.post(f"{BASE_URL}/api/patients/merge", json={
            "primary_patient_id": p1,
            "secondary_patient_id": p2,
            "dry_run": False,
        }, timeout=15)
        assert r.status_code == 200, r.text

        r = s.get(f"{BASE_URL}/api/patients/duplicates",
                  params={"key": "phone_and_name", "min_group": 2}, timeout=20)
        data = r.json()
        # The merged pair must NOT surface any more.
        assert not any(
            g["key"]["phone"] == phone
            for g in data["groups"]
        ), "merged rows must be excluded from the sweep"
    finally:
        try:
            s.delete(f"{BASE_URL}/api/patients/{p1}", timeout=10)
        except Exception:
            pass


def test_duplicates_key_matrix_supports_three_modes():
    """phone_only + name_only must each return at least the phone-only
    or name-only surface. The screen relies on all three keys as
    tabbed filters — an owner who wants to review families sharing
    one landline picks `phone_only`."""
    s = _sess()
    for key in ("phone_and_name", "phone_only", "name_only"):
        r = s.get(f"{BASE_URL}/api/patients/duplicates",
                  params={"key": key, "min_group": 2}, timeout=20)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["key"] == key
        assert "groups" in data
        assert "group_count" in data
        assert "affected_patients" in data
        # Malformed key must be a 400.
    r = s.get(f"{BASE_URL}/api/patients/duplicates",
              params={"key": "bogus", "min_group": 2}, timeout=10)
    assert r.status_code == 400
