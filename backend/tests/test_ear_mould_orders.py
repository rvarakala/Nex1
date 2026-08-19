"""Regression: Ear Mould Orders — quick-book flow.

Feb 2026 — a beta clinic asked whether the app supports advance payments
for hearing aids AND ear moulds. Hearing aids: yes (HA Quick Sale
already handles advance/partial/fully-paid). Ear moulds: no dedicated
flow existed. Shipped a "book-and-forget" ear-mould module that
generates a proper PARTIAL/PAID/UNPAID invoice + a soft workflow order
for chase-and-collect.

These tests lock in the invoice math + status-ribbon behaviour so a
future refactor can't silently break the balance-due tracking.
"""
import os
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
    tok = r.json()["access_token"]
    s.headers.update({"Authorization": f"Bearer {tok}"})
    return s


def _first_patient(s):
    r = s.get(f"{BASE_URL}/api/patients?limit=1", timeout=15)
    d = r.json()
    d = d.get("items", d) if isinstance(d, dict) else d
    return d[0]["patient_id"]


def test_book_ear_mould_with_advance_generates_partial_invoice():
    """Booking with advance < total must produce a PARTIAL invoice with
    correct paid_total and due_total, and the order must land in
    `sent_to_lab` when a lab is named."""
    s = _sess()
    pid = _first_patient(s)
    r = s.post(f"{BASE_URL}/api/ha/ear-moulds", json={
        "patient_id": pid, "side": "both", "material": "silicone",
        "vent_size": "1.5mm", "lab_vendor": "PyTest Lab",
        "expected_delivery_date": "2026-08-25",
        "total_amount": 8000, "advance_amount": 3000,
        "payment_mode": "upi", "gst_rate": 18,
    }, timeout=15)
    assert r.status_code == 200, r.text
    order = r.json()
    assert order["status"] == "sent_to_lab"          # lab named → auto-forward
    assert order["total_amount"] == 8000
    assert order["advance_amount"] == 3000
    assert order["balance_due"] == 5000
    # Verify the linked invoice is PARTIAL with the right numbers
    inv = s.get(f"{BASE_URL}/api/billing/invoices/{order['invoice_id']}", timeout=15).json()
    assert inv["status"] == "partial"
    assert inv["grand_total"] == 8000
    assert inv["paid_total"] == 3000
    assert inv["due_total"] == 5000
    assert len(inv["payments"]) == 1
    assert inv["payments"][0]["amount"] == 3000
    assert inv["payments"][0]["method"] == "upi"


def test_book_ear_mould_no_advance_produces_unpaid_invoice():
    """Booking WITHOUT an advance must produce an UNPAID invoice and land
    in `pending_impression` when no lab was named."""
    s = _sess()
    pid = _first_patient(s)
    r = s.post(f"{BASE_URL}/api/ha/ear-moulds", json={
        "patient_id": pid, "side": "left", "material": "acrylic",
        "total_amount": 4500, "advance_amount": 0, "payment_mode": "cash",
    }, timeout=15)
    assert r.status_code == 200, r.text
    order = r.json()
    assert order["status"] == "pending_impression"
    assert order["balance_due"] == 4500
    inv = s.get(f"{BASE_URL}/api/billing/invoices/{order['invoice_id']}", timeout=15).json()
    assert inv["status"] == "draft"
    assert len(inv["payments"]) == 0
    assert inv["due_total"] == 4500


def test_book_ear_mould_full_upfront_produces_paid_invoice():
    """Full upfront payment must produce a PAID invoice — some patients
    prefer to clear the amount at booking."""
    s = _sess()
    pid = _first_patient(s)
    r = s.post(f"{BASE_URL}/api/ha/ear-moulds", json={
        "patient_id": pid, "side": "right", "material": "soft_acrylic",
        "total_amount": 3200, "advance_amount": 3200, "payment_mode": "card",
    }, timeout=15)
    assert r.status_code == 200, r.text
    order = r.json()
    assert order["balance_due"] == 0
    inv = s.get(f"{BASE_URL}/api/billing/invoices/{order['invoice_id']}", timeout=15).json()
    assert inv["status"] == "paid"
    assert inv["due_total"] == 0


def test_status_transition_appends_to_history():
    """PATCH /status must move the status AND append to `history` so
    later disputes can be reconstructed."""
    s = _sess()
    pid = _first_patient(s)
    r = s.post(f"{BASE_URL}/api/ha/ear-moulds", json={
        "patient_id": pid, "side": "both", "material": "silicone",
        "lab_vendor": "PyTest Lab",
        "total_amount": 5000, "advance_amount": 1000,
    }, timeout=15)
    order_id = r.json()["order_id"]

    # Move sent_to_lab → arrived
    r = s.patch(f"{BASE_URL}/api/ha/ear-moulds/{order_id}/status",
                json={"status": "arrived", "note": "Received from lab"}, timeout=15)
    assert r.status_code == 200
    updated = r.json()
    assert updated["status"] == "arrived"
    assert len(updated["history"]) >= 2
    assert updated["history"][-1]["status"] == "arrived"
    assert updated["history"][-1]["note"] == "Received from lab"


def test_advance_greater_than_total_is_rejected():
    """Guardrail: over-payment at booking is a data-entry mistake — reject
    with 400 so the receptionist can correct the numbers."""
    s = _sess()
    pid = _first_patient(s)
    r = s.post(f"{BASE_URL}/api/ha/ear-moulds", json={
        "patient_id": pid, "side": "both", "material": "silicone",
        "total_amount": 3000, "advance_amount": 5000,
    }, timeout=15)
    assert r.status_code == 400
