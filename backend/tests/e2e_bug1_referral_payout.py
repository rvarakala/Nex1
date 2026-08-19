"""E2E test for Bug 1: HA referral payout via API (DL Test clinic)."""
import os
import uuid
import requests

BASE = "https://referral-sprint.preview.emergentagent.com"
EMAIL = "dltest@example.com"
PASSWORD = "TestPass@123"


def login():
    s = requests.Session()
    r = s.post(f"{BASE}/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    print("login status:", r.status_code)
    assert r.status_code == 200, r.text
    data = r.json()
    token = data.get("access_token") or data.get("token")
    if token:
        s.headers.update({"Authorization": f"Bearer {token}"})
    return s


def run(with_product_type: bool):
    s = login()
    suffix = uuid.uuid4().hex[:6]
    # Create referring doctor
    r = s.post(f"{BASE}/api/referring-doctors", json={
        "name": f"TEST_DrE2E_{suffix}",
        "phone": f"9000{suffix[:6]}",
        "specialty": "ENT",
    })
    print("create doctor:", r.status_code, r.text[:200])
    assert r.status_code in (200, 201), r.text
    doctor = r.json()
    did = doctor.get("id") or doctor.get("_id") or doctor.get("doctor_id")
    print("doctor id:", did)

    # Configure cut
    r = s.patch(f"{BASE}/api/referrals/doctors/{did}/cut-config", json={
        "ha_cut_mode": "flat",
        "ha_cut_value": 5000,
        "diag_cut_mode": "flat",
        "diag_cut_value": 0,
    })
    print("cut config:", r.status_code, r.text[:200])
    assert r.status_code == 200, r.text

    # Create patient linked to doctor
    r = s.post(f"{BASE}/api/patients", json={
        "name": f"TEST_PatE2E_{suffix}",
        "phone": f"8000{suffix[:6]}",
        "age": 55,
        "gender": "male",
        "referring_doctor_id": did,
    })
    print("create patient:", r.status_code, r.text[:300])
    assert r.status_code in (200, 201), r.text
    patient = r.json()
    pid = patient.get("id") or patient.get("_id") or patient.get("patient_id")
    print("patient id:", pid)

    # Create appt+invoice
    line = {
        "service_id": None,
        "description": "HA Fitting",
        "unit_price": 30000,
        "quantity": 1,
    }
    if with_product_type:
        line["product_type"] = "Hearing Aid"

    # Get audiologist / staff id — use own user_id as fallback
    aid = None
    ru = s.get(f"{BASE}/api/users")
    if ru.status_code == 200:
        users = ru.json()
        if isinstance(users, dict):
            users = users.get("users") or users.get("data") or []
        # Prefer audiologist role
        for u in users:
            if u.get("role") == "audiologist":
                aid = u.get("user_id") or u.get("id")
                break
        if not aid:
            for u in users:
                aid = u.get("user_id") or u.get("id")
                if aid:
                    break
    print("staff/audiologist id:", aid)

    from datetime import datetime, timedelta
    slot = (datetime.utcnow() + timedelta(days=int(uuid.uuid4().int % 30) + 1, hours=int(uuid.uuid4().int % 8) + 9)).replace(minute=0, second=0, microsecond=0)
    payload = {
        "patient_id": pid,
        "audiologist_id": aid,
        "service": "HA Fitting",
        "start_at": slot.isoformat() + "Z",
        "wing": "hearing_aid",
        "hearing_aid_services": ["ha_fitting"],
        "duration_minutes": 30,
        "invoice_lines": [line],
    }
    r = s.post(f"{BASE}/api/appointments/with-invoice", json=payload)
    print("appt+invoice:", r.status_code, r.text[:400])
    if r.status_code not in (200, 201):
        # retry with a different slot
        from datetime import timedelta
        slot = slot + timedelta(hours=2)
        payload["start_at"] = slot.isoformat() + "Z"
        r = s.post(f"{BASE}/api/appointments/with-invoice", json=payload)
        print("retry appt+invoice:", r.status_code, r.text[:400])
    assert r.status_code in (200, 201), r.text
    result = r.json()
    invoice = result.get("invoice") or result.get("invoice_data") or {}
    iid = invoice.get("id") or invoice.get("invoice_id") or result.get("invoice_id")
    print("invoice id:", iid)
    assert iid, f"no invoice id in {result}"

    total = invoice.get("total") or invoice.get("total_amount") or 30000

    # Pay it
    r = s.post(f"{BASE}/api/billing/invoices/{iid}/payments", json={
        "amount": total,
        "method": "cash",
    })
    print("pay:", r.status_code, r.text[:200])
    assert r.status_code in (200, 201), r.text

    # Fetch dashboard
    r = s.get(f"{BASE}/api/referrals/dashboard")
    print("dashboard status:", r.status_code)
    assert r.status_code == 200, r.text
    dash = r.json()
    rows = dash.get("doctors") or dash.get("rows") or dash.get("data") or dash
    if isinstance(rows, dict):
        rows = rows.get("doctors", [])
    found = None
    for row in rows:
        rid = row.get("doctor_id") or row.get("id") or row.get("_id")
        if rid == did:
            found = row
            break
    print("found row:", found)
    assert found, f"doctor {did} not in dashboard"
    ha_rev = found.get("ha_sales_revenue") or found.get("ha_revenue") or 0
    ha_payout = found.get("ha_payout") or 0
    total_payout = found.get("total_payout") or 0
    print(f"[with_product_type={with_product_type}] ha_rev={ha_rev} ha_payout={ha_payout} total_payout={total_payout}")
    assert ha_rev == 30000, f"expected ha_rev=30000, got {ha_rev}"
    assert ha_payout == 5000, f"expected ha_payout=5000, got {ha_payout}"
    assert total_payout >= 5000
    print(f"PASS with_product_type={with_product_type}")
    return did, pid


if __name__ == "__main__":
    print("=== Run 1: with product_type='Hearing Aid' ===")
    run(with_product_type=True)
    print("\n=== Run 2: legacy shape (product_type omitted) ===")
    run(with_product_type=False)
    print("\nALL E2E CHECKS PASSED")
