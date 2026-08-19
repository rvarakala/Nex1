"""Backend tests for Phase 1 — Patient Records, Referring Doctors, Patient Notes, Sessions."""
import os
import time
import pytest
import requests

from _helpers import ADMIN_EMAIL, ADMIN_PASSWORD  # legacy creds (env-overridable)
BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://referral-sprint.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"

session = requests.Session()
session.headers.update({"Content-Type": "application/json"})


@pytest.fixture(scope="module", autouse=True)
def _authenticate():
    """Log the module-level session in as admin before any test runs.

    Without this, every request returns 401 because `session` has no Authorization
    header. Uses super-admin so patient-records POST/DELETE are permitted.
    """
    r = session.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert r.status_code == 200, f"Login failed: {r.status_code} {r.text}"
    token = r.json()["access_token"]
    session.headers.update({"Authorization": f"Bearer {token}"})
    yield
    session.headers.pop("Authorization", None)


# Cleanup tracking
_created_patients = []
_created_doctors = []
_created_notes = []
_created_sessions = []


@pytest.fixture(scope="module", autouse=True)
def cleanup():
    yield
    for nid in _created_notes:
        try: session.delete(f"{API}/patient-notes/{nid}")
        except: pass
    for sid in _created_sessions:
        try: session.delete(f"{API}/sessions/{sid}")
        except: pass
    for pid in _created_patients:
        try: session.delete(f"{API}/patients/{pid}")
        except: pass
    for did in _created_doctors:
        try: session.delete(f"{API}/referring-doctors/{did}")
        except: pass


# ==================== HEALTH ====================
def test_health():
    r = session.get(f"{API}/health")
    assert r.status_code == 200
    assert r.json().get("status") == "healthy"


# ==================== REFERRING DOCTORS ====================
def test_create_referring_doctor():
    r = session.post(f"{API}/referring-doctors", json={
        "name": "TEST_Dr Sharma", "specialty": "ENT", "clinic": "TEST_Clinic", "phone": "9999000011"
    })
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["name"] == "TEST_Dr Sharma"
    assert d["specialty"] == "ENT"
    assert d["doctor_id"].startswith("DR-")
    _created_doctors.append(d["doctor_id"])


def test_list_referring_doctors_search():
    r = session.get(f"{API}/referring-doctors", params={"search": "TEST_Dr Sh"})
    assert r.status_code == 200
    docs = r.json()
    assert any(d["name"] == "TEST_Dr Sharma" for d in docs)


def test_update_referring_doctor():
    if not _created_doctors:
        pytest.skip("no doctor created")
    did = _created_doctors[0]
    r = session.put(f"{API}/referring-doctors/{did}", json={
        "name": "TEST_Dr Sharma", "specialty": "ENT, Otology", "clinic": "TEST_Clinic", "phone": "9999000011"
    })
    assert r.status_code == 200
    assert r.json()["specialty"] == "ENT, Otology"


def test_delete_referring_doctor_404():
    r = session.delete(f"{API}/referring-doctors/DR-NOTEXIST")
    assert r.status_code == 404


# ==================== PATIENTS ====================
def test_create_patient_india_fields():
    did = _created_doctors[0] if _created_doctors else None
    r = session.post(f"{API}/patients", json={
        "name": "TEST_Rahul Verma",
        "age": 35,
        "gender": "Male",
        "mobile": "9876543210",
        "aadhaar_last4": "1234",
        "address": "Mumbai, MH",
        "referring_doctor_id": did,
    })
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["name"] == "TEST_Rahul Verma"
    assert p["mobile"] == "9876543210"
    assert p["aadhaar_last4"] == "1234"
    assert p["referring_doctor_id"] == did
    assert p["patient_id"].startswith("ACS-")
    _created_patients.append(p["patient_id"])


def test_search_patient_case_insensitive():
    # by name
    r = session.get(f"{API}/patients", params={"search": "rahul ver"})
    assert r.status_code == 200
    pats = r.json()
    assert any(p["name"] == "TEST_Rahul Verma" for p in pats), pats
    # by mobile
    r = session.get(f"{API}/patients", params={"search": "9876543210"})
    assert r.status_code == 200
    assert any(p["mobile"] == "9876543210" for p in r.json())
    # by patient_id
    pid = _created_patients[0]
    r = session.get(f"{API}/patients", params={"search": pid})
    assert r.status_code == 200
    assert any(p["patient_id"] == pid for p in r.json())


def test_update_patient_persistence():
    pid = _created_patients[0]
    r = session.put(f"{API}/patients/{pid}", json={
        "name": "TEST_Rahul Verma",
        "age": 36,
        "gender": "Male",
        "mobile": "9876543210",
        "aadhaar_last4": "5678",
        "address": "Pune, MH",
    })
    assert r.status_code == 200
    assert r.json()["age"] == 36
    assert r.json()["aadhaar_last4"] == "5678"
    # GET to verify persistence
    g = session.get(f"{API}/patients/{pid}")
    assert g.status_code == 200
    assert g.json()["age"] == 36
    assert g.json()["address"] == "Pune, MH"


# ==================== PATIENT NOTES ====================
def test_create_patient_note():
    pid = _created_patients[0]
    r = session.post(f"{API}/patient-notes", json={
        "patient_id": pid, "text": "TEST_Note 1", "auto": False
    })
    assert r.status_code == 200, r.text
    n = r.json()
    assert n["text"] == "TEST_Note 1"
    assert n["note_id"].startswith("NOTE-")
    _created_notes.append(n["note_id"])
    # Second note for sort verification
    time.sleep(0.05)
    r2 = session.post(f"{API}/patient-notes", json={
        "patient_id": pid, "text": "TEST_Note 2 (auto)", "auto": True
    })
    assert r2.status_code == 200
    _created_notes.append(r2.json()["note_id"])


def test_create_patient_note_unknown_patient_404():
    r = session.post(f"{API}/patient-notes", json={
        "patient_id": "ACS-XXXX-NOTREAL", "text": "should fail"
    })
    assert r.status_code == 404


def test_list_notes_sorted_desc():
    pid = _created_patients[0]
    r = session.get(f"{API}/patient-notes", params={"patient_id": pid})
    assert r.status_code == 200
    notes = r.json()
    assert len(notes) >= 2
    # DESC by created_at
    for i in range(len(notes) - 1):
        assert notes[i]["created_at"] >= notes[i + 1]["created_at"]


def test_delete_patient_note():
    if not _created_notes:
        pytest.skip("no notes")
    nid = _created_notes.pop()
    r = session.delete(f"{API}/patient-notes/{nid}")
    assert r.status_code == 200


# ==================== SESSIONS ====================
def test_create_two_sessions_and_sort():
    pid = _created_patients[0]
    r1 = session.post(f"{API}/sessions", json={"patient_id": pid})
    assert r1.status_code == 200, r1.text
    sid1 = r1.json()["session_id"]
    _created_sessions.append(sid1)
    time.sleep(0.5)
    r2 = session.post(f"{API}/sessions", json={"patient_id": pid})
    assert r2.status_code == 200
    sid2 = r2.json()["session_id"]
    _created_sessions.append(sid2)
    # GET sorted DESC
    g = session.get(f"{API}/sessions", params={"patient_id": pid})
    assert g.status_code == 200
    sessions = g.json()
    assert len(sessions) >= 2
    for i in range(len(sessions) - 1):
        assert sessions[i]["test_date"] >= sessions[i + 1]["test_date"]


# ==================== CASCADE DELETE ====================
def test_delete_patient_cascades_notes():
    # create a separate patient + note for clean cascade test
    p = session.post(f"{API}/patients", json={
        "name": "TEST_Cascade", "age": 20, "gender": "Female", "mobile": "9000000001"
    }).json()
    pid = p["patient_id"]
    n = session.post(f"{API}/patient-notes", json={"patient_id": pid, "text": "TEST_x"}).json()
    assert "note_id" in n
    # Delete patient
    d = session.delete(f"{API}/patients/{pid}")
    assert d.status_code == 200
    # Notes for this patient must be gone — cascade delete removes patient,
    # so GET with that patient_id returns 404 (Patient not found) OR [] depending on impl.
    resp = session.get(f"{API}/patient-notes", params={"patient_id": pid})
    assert resp.status_code in (200, 404)
    if resp.status_code == 200:
        assert resp.json() == []
    # patient gone
    assert session.get(f"{API}/patients/{pid}").status_code == 404
