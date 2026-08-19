"""M01.C Billing & Report Handover backend tests."""
import os
import pytest
import requests
from datetime import datetime


from _helpers import (  # legacy creds (env-overridable)
    ADMIN_EMAIL, ADMIN_PASSWORD,
    FRONTDESK_EMAIL, FRONTDESK_PASSWORD,
    AUDIO_EMAIL, AUDIO_PASSWORD,
    ACCOUNTS_EMAIL, ACCOUNTS_PASSWORD,
)
BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', 'https://referral-sprint.preview.emergentagent.com').rstrip('/')
API = f"{BASE_URL}/api"


def _login(email, pwd):
    r = requests.post(f"{API}/auth/login", json={"email": email, "password": pwd}, timeout=15)
    assert r.status_code == 200, f"login failed for {email}: {r.status_code} {r.text}"
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def front_tok():
    return _login(FRONTDESK_EMAIL, FRONTDESK_PASSWORD)


@pytest.fixture(scope="module")
def acct_tok():
    return _login(ACCOUNTS_EMAIL, ACCOUNTS_PASSWORD)


@pytest.fixture(scope="module")
def admin_tok():
    return _login(ADMIN_EMAIL, ADMIN_PASSWORD)


def H(t): return {"Authorization": f"Bearer {t}"}


# Patient fixtures (intra-state Maharashtra and inter-state Telangana)
@pytest.fixture(scope="module")
def patient_intra(front_tok):
    p = requests.post(f"{API}/patients", headers=H(front_tok), json={
        "name": "TEST_BILL Intra MH", "age": 40, "gender": "Male",
        "mobile": "9000000301", "state": "Maharashtra", "city": "Mumbai",
        "address": "Test 1", "pincode": "400001",
    }, timeout=15)
    assert p.status_code == 200, p.text
    return p.json()


@pytest.fixture(scope="module")
def patient_inter(front_tok):
    p = requests.post(f"{API}/patients", headers=H(front_tok), json={
        "name": "TEST_BILL Inter TG", "age": 45, "gender": "Female",
        "mobile": "9000000302", "state": "Telangana", "city": "Hyderabad",
        "pincode": "500001",
    }, timeout=15)
    assert p.status_code == 200
    return p.json()


# ---------- AUTH GATE ----------
class TestAuthGate:
    def test_no_token_401(self):
        for path in ["/billing/services", "/billing/invoices", "/billing/collections", "/billing/pending-reports"]:
            r = requests.get(f"{API}{path}", timeout=10)
            assert r.status_code in (401, 403), f"{path} expected 401/403, got {r.status_code}"


# ---------- SERVICE CATALOGUE ----------
class TestServices:
    def test_seeded_catalogue(self, front_tok):
        r = requests.get(f"{API}/billing/services", headers=H(front_tok), timeout=15)
        assert r.status_code == 200
        items = r.json()
        assert isinstance(items, list)
        codes = {it.get("code") for it in items}
        for need in ["CONSULT", "PTA", "HA-BTE", "HA-RIC", "BATTERY", "EARMOULD"]:
            assert need in codes, f"missing seeded service {need}"
        for it in items:
            assert "_id" not in it
        assert len(items) >= 12

    def test_create_update_delete_service(self, acct_tok):
        # CREATE
        payload = {"code": "TEST_ACC1", "name": "TEST_BILL Accessory", "category": "Accessory",
                   "hsn_sac": "8506", "price": 500.0, "gst_rate": 18.0, "gst_inclusive": True, "is_taxable": True}
        c = requests.post(f"{API}/billing/services", headers=H(acct_tok), json=payload, timeout=15)
        assert c.status_code == 200, c.text
        svc = c.json()
        sid = svc["service_id"]
        assert svc["gst_rate"] == 18.0 and svc["is_taxable"] is True

        # UPDATE patch-allowed
        u = requests.put(f"{API}/billing/services/{sid}", headers=H(acct_tok),
                         json={"price": 550.0, "gst_rate": 18.0, "name": "TEST_BILL Accessory v2",
                               "service_id": "should-be-ignored"}, timeout=15)
        assert u.status_code == 200, u.text
        assert u.json()["price"] == 550.0
        assert u.json()["service_id"] == sid  # unchanged

        # DEACTIVATE
        d = requests.delete(f"{API}/billing/services/{sid}", headers=H(acct_tok), timeout=15)
        assert d.status_code == 200
        # listed only when active_only=False
        all_list = requests.get(f"{API}/billing/services?active_only=false", headers=H(acct_tok), timeout=15).json()
        match = [s for s in all_list if s["service_id"] == sid]
        assert match and match[0]["active"] is False


# ---------- INVOICES ----------
class TestInvoices:
    def _ha_bte_id(self, tok):
        items = requests.get(f"{API}/billing/services", headers=H(tok), timeout=15).json()
        return next(it["service_id"] for it in items if it["code"] == "HA-BTE")

    def _consult_id(self, tok):
        items = requests.get(f"{API}/billing/services", headers=H(tok), timeout=15).json()
        return next(it["service_id"] for it in items if it["code"] == "CONSULT")

    def test_mixed_invoice_inter_state_igst(self, front_tok, patient_inter):
        ha = self._ha_bte_id(front_tok)
        consult = self._consult_id(front_tok)
        body = {
            "patient_id": patient_inter["patient_id"],
            "lines": [
                {"service_id": consult, "quantity": 1},
                {"service_id": ha, "quantity": 1, "discount_amount": 2000},
            ],
        }
        r = requests.post(f"{API}/billing/invoices", headers=H(front_tok), json=body, timeout=15)
        assert r.status_code == 200, r.text
        inv = r.json()
        assert inv["status"] == "draft"
        # taxable_value = 500 (consult exempt) + 29464.29 (HA-BTE post-disc)
        assert abs(inv["subtotal"] - (500 + 29464.29)) < 0.5
        assert abs(inv["tax_total"] - 3535.71) < 0.5
        assert abs(inv["grand_total"] - 33500.0) < 0.5
        # Inter-state -> IGST only
        assert inv["igst_total"] > 0 and inv["cgst_total"] == 0 and inv["sgst_total"] == 0
        assert inv["invoice_no"].startswith("INV/")
        # numbering parts
        parts = inv["invoice_no"].split("/")
        assert len(parts) == 3 and len(parts[2]) == 6

    def test_intra_state_cgst_sgst_split(self, front_tok, patient_intra):
        ha = self._ha_bte_id(front_tok)
        body = {"patient_id": patient_intra["patient_id"],
                "lines": [{"service_id": ha, "quantity": 1, "discount_amount": 2000}]}
        r = requests.post(f"{API}/billing/invoices", headers=H(front_tok), json=body, timeout=15)
        assert r.status_code == 200
        inv = r.json()
        assert inv["igst_total"] == 0
        assert abs(inv["cgst_total"] + inv["sgst_total"] - 3535.71) < 0.5
        # Halves should be roughly equal
        assert abs(inv["cgst_total"] - inv["sgst_total"]) < 0.1

    def test_percent_discount_computes_and_persists(self, front_tok, patient_intra):
        """Percent discount: discount_value=10% on HA-BTE (₹35,000) → flat ₹3,500 discount."""
        ha = self._ha_bte_id(front_tok)
        body = {"patient_id": patient_intra["patient_id"],
                "lines": [{"service_id": ha, "quantity": 1,
                           "discount_type": "percent", "discount_value": 10}]}
        r = requests.post(f"{API}/billing/invoices", headers=H(front_tok), json=body, timeout=15)
        assert r.status_code == 200, r.text
        inv = r.json()
        line = inv["lines"][0]
        # 10% of ₹35,000 gross = ₹3,500 flat
        assert abs(line["discount_amount"] - 3500.0) < 0.5
        assert line["discount_type"] == "percent"
        assert abs(line["discount_value"] - 10.0) < 0.01
        # Grand total: 35000 - 3500 = 31500 (GST inclusive so no extra)
        assert abs(inv["rounded_total"] - 31500.0) < 1.0

    def test_flat_discount_via_discount_value(self, front_tok, patient_intra):
        """discount_type=flat with discount_value populated (new flow) still works."""
        ha = self._ha_bte_id(front_tok)
        body = {"patient_id": patient_intra["patient_id"],
                "lines": [{"service_id": ha, "quantity": 1,
                           "discount_type": "flat", "discount_value": 1500}]}
        r = requests.post(f"{API}/billing/invoices", headers=H(front_tok), json=body, timeout=15)
        assert r.status_code == 200, r.text
        inv = r.json()
        line = inv["lines"][0]
        assert abs(line["discount_amount"] - 1500.0) < 0.5
        assert line["discount_type"] == "flat"

    def test_invoice_with_initial_payment_status_partial(self, front_tok, patient_intra):
        ha = self._ha_bte_id(front_tok)
        consult = self._consult_id(front_tok)
        body = {"patient_id": patient_intra["patient_id"],
                "lines": [
                    {"service_id": consult, "quantity": 1},
                    {"service_id": ha, "quantity": 1, "discount_amount": 2000},
                ],
                "initial_payment": {"method": "upi", "amount": 10000, "reference": "UPI-TEST"}}
        r = requests.post(f"{API}/billing/invoices", headers=H(front_tok), json=body, timeout=15)
        assert r.status_code == 200, r.text
        inv = r.json()
        assert inv["paid_total"] == 10000
        assert abs(inv["due_total"] - 23500) < 0.5
        assert inv["status"] == "partial"
        assert len(inv["payments"]) == 1 and inv["payments"][0]["method"] == "upi"

    def test_invalid_service_id_400(self, front_tok, patient_intra):
        r = requests.post(f"{API}/billing/invoices", headers=H(front_tok), json={
            "patient_id": patient_intra["patient_id"],
            "lines": [{"service_id": "SVC-DOESNOTEXIST", "quantity": 1}]
        }, timeout=15)
        assert r.status_code == 400

    def test_empty_lines_400(self, front_tok, patient_intra):
        r = requests.post(f"{API}/billing/invoices", headers=H(front_tok), json={
            "patient_id": patient_intra["patient_id"], "lines": []
        }, timeout=15)
        assert r.status_code == 400

    def test_list_filter_and_get_by_id(self, front_tok, patient_intra):
        # list by patient_id
        r = requests.get(f"{API}/billing/invoices?patient_id={patient_intra['patient_id']}",
                         headers=H(front_tok), timeout=15)
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) >= 1
        for row in rows:
            assert "_id" not in row
        inv_id = rows[0]["invoice_id"]
        g = requests.get(f"{API}/billing/invoices/{inv_id}", headers=H(front_tok), timeout=15)
        assert g.status_code == 200
        full = g.json()
        assert full["lines"] and "payments" in full

    def test_payment_progresses_to_paid(self, front_tok, patient_intra):
        # Create fresh invoice for this test
        ha = self._ha_bte_id(front_tok)
        r = requests.post(f"{API}/billing/invoices", headers=H(front_tok), json={
            "patient_id": patient_intra["patient_id"],
            "lines": [{"service_id": ha, "quantity": 1}]
        }, timeout=15)
        inv = r.json()
        inv_id = inv["invoice_id"]
        rounded = inv["rounded_total"]

        # negative payment -> 400
        bad = requests.post(f"{API}/billing/invoices/{inv_id}/payments", headers=H(front_tok),
                            json={"method": "cash", "amount": -10}, timeout=15)
        assert bad.status_code == 400
        zero = requests.post(f"{API}/billing/invoices/{inv_id}/payments", headers=H(front_tok),
                             json={"method": "cash", "amount": 0}, timeout=15)
        assert zero.status_code == 400

        # partial
        p1 = requests.post(f"{API}/billing/invoices/{inv_id}/payments", headers=H(front_tok),
                           json={"method": "cash", "amount": 1000}, timeout=15)
        assert p1.status_code == 200
        assert p1.json()["status"] == "partial"

        # pay rest
        p2 = requests.post(f"{API}/billing/invoices/{inv_id}/payments", headers=H(front_tok),
                           json={"method": "card", "amount": rounded - 1000}, timeout=15)
        assert p2.status_code == 200
        assert p2.json()["status"] == "paid"
        assert abs(p2.json()["due_total"]) < 0.5

    def test_cancel_role_gate(self, front_tok, acct_tok, patient_intra):
        consult = self._consult_id(front_tok)
        r = requests.post(f"{API}/billing/invoices", headers=H(front_tok), json={
            "patient_id": patient_intra["patient_id"],
            "lines": [{"service_id": consult, "quantity": 1}]
        }, timeout=15)
        inv_id = r.json()["invoice_id"]

        # front_desk -> 403
        f = requests.post(f"{API}/billing/invoices/{inv_id}/cancel", headers=H(front_tok),
                          json={"reason": "test"}, timeout=15)
        assert f.status_code == 403

        # accounts -> 200
        a = requests.post(f"{API}/billing/invoices/{inv_id}/cancel", headers=H(acct_tok),
                          json={"reason": "TEST_BILL cancel"}, timeout=15)
        assert a.status_code == 200
        assert a.json()["status"] == "cancelled"

        # payment on cancelled -> 400
        p = requests.post(f"{API}/billing/invoices/{inv_id}/payments", headers=H(front_tok),
                          json={"method": "cash", "amount": 100}, timeout=15)
        assert p.status_code == 400


# ---------- COLLECTIONS + DASHBOARD ----------
class TestCollections:
    def test_collections_today(self, front_tok):
        r = requests.get(f"{API}/billing/collections", headers=H(front_tok), timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert "total" in data and "by_method" in data and "payment_count" in data
        assert data["total"] >= 0

    def test_dashboard_collections_today(self, front_tok):
        r = requests.get(f"{API}/dashboard/frontdesk", headers=H(front_tok), timeout=15)
        assert r.status_code == 200
        assert "collections_today" in r.json()["kpis"]


# ---------- REPORT HANDOVER ----------
class TestHandover:
    def test_pending_reports_listing(self, front_tok):
        r = requests.get(f"{API}/billing/pending-reports", headers=H(front_tok), timeout=15)
        assert r.status_code == 200
        rows = r.json()
        assert isinstance(rows, list)
        for row in rows:
            assert "session_id" in row
            assert "_id" not in row

    def test_log_delivery_and_disappear_from_pending(self, front_tok, patient_intra):
        """Post-Feb-2026-v2 the handover flow was scrapped. `pending-reports`
        is a deprecated stub that always returns []. Logging a ReportDelivery
        remains supported (used by WhatsApp/email quick actions elsewhere)."""
        # Create a test session for the test patient
        s = requests.post(f"{API}/sessions", headers=H(front_tok), json={
            "patient_id": patient_intra["patient_id"],
            "audiologist_name": "Dr Test"
        }, timeout=15)
        assert s.status_code == 200, s.text
        sid = s.json()["session_id"]
        # mark completed
        u = requests.put(f"{API}/sessions/{sid}", headers=H(front_tok), json={"status": "completed"}, timeout=15)
        assert u.status_code == 200

        # Deprecated endpoint — always returns empty list now.
        pend1 = requests.get(f"{API}/billing/pending-reports", headers=H(front_tok), timeout=15).json()
        assert pend1 == []

        # Delivery logging still works (WhatsApp/email quick actions in the app).
        d = requests.post(f"{API}/billing/report-deliveries", headers=H(front_tok), json={
            "session_id": sid, "channel": "whatsapp", "recipient": "9000000301"
        }, timeout=15)
        assert d.status_code == 200, d.text
        assert d.json()["channel"] == "whatsapp"

        # Filter list — the delivery we just created should be there.
        lst = requests.get(f"{API}/billing/report-deliveries?session_id={sid}",
                           headers=H(front_tok), timeout=15)
        assert lst.status_code == 200 and len(lst.json()) >= 1

    def test_invalid_channel_400(self, front_tok, patient_intra):
        r = requests.post(f"{API}/billing/report-deliveries", headers=H(front_tok),
                          json={"session_id": "x", "channel": "xyz"}, timeout=15)
        assert r.status_code == 400
