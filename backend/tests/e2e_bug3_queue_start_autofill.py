"""E2E test for Bug 3 backend: diagnostics/queue/start includes referring doctor info."""
import uuid
import requests

BASE = "https://referral-sprint.preview.emergentagent.com"


def login(email, password):
    s = requests.Session()
    r = s.post(f"{BASE}/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    tok = r.json().get("access_token") or r.json().get("token")
    if tok:
        s.headers.update({"Authorization": f"Bearer {tok}"})
    return s


def test_queue_start_returns_referring_doctor():
    s = login("dltest@example.com", "TestPass@123")
    suffix = uuid.uuid4().hex[:6]

    r = s.post(f"{BASE}/api/referring-doctors", json={
        "name": f"TEST_DrQS_{suffix}", "phone": f"7000{suffix[:6]}", "specialty": "ENT",
    })
    assert r.status_code in (200, 201), r.text
    did = r.json()["doctor_id"]

    r = s.post(f"{BASE}/api/patients", json={
        "name": f"TEST_PatQS_{suffix}", "phone": f"6000{suffix[:6]}",
        "age": 40, "gender": "female", "referring_doctor_id": did,
        "referring_physician": "Free Text Fallback",
    })
    assert r.status_code in (200, 201), r.text
    pid = r.json()["patient_id"]

    r = s.post(f"{BASE}/api/diagnostics/queue/start", json={"patient_id": pid})
    print("queue/start:", r.status_code, r.text[:500])
    assert r.status_code in (200, 201), r.text
    body = r.json()
    p = body.get("patient") or {}
    assert p.get("referring_doctor_id") == did
    assert p.get("referring_doctor_name") == f"TEST_DrQS_{suffix}"
    assert "referring_physician" in p
    print("PASS - referring doctor auto-fill data present in queue/start")


def test_queue_start_no_doctor_link():
    s = login("dltest@example.com", "TestPass@123")
    suffix = uuid.uuid4().hex[:6]
    r = s.post(f"{BASE}/api/patients", json={
        "name": f"TEST_PatNoDoc_{suffix}", "phone": f"5000{suffix[:6]}",
        "age": 40, "gender": "male",
        "referring_physician": "Freehand Dr",
    })
    assert r.status_code in (200, 201), r.text
    pid = r.json()["patient_id"]

    r = s.post(f"{BASE}/api/diagnostics/queue/start", json={"patient_id": pid})
    assert r.status_code == 200, r.text
    p = r.json().get("patient") or {}
    assert p.get("referring_doctor_id") is None
    assert p.get("referring_doctor_name") is None
    assert p.get("referring_physician") == "Freehand Dr"
    print("PASS - free-text physician still returned")


if __name__ == "__main__":
    test_queue_start_returns_referring_doctor()
    test_queue_start_no_doctor_link()
    print("\nALL BUG 3 BACKEND E2E TESTS PASSED")
