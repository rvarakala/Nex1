"""Phase 12.A + 12.B + 12.C — AUDINEXA Service Jobs, Couriers, Estimates,
Approvals, Repair Analytics, Job Card PDF, WhatsApp templates, trial expiry.
"""
import os
import uuid
from datetime import datetime, timezone, timedelta

import pytest
import requests



from _helpers import (  # legacy creds (env-overridable)
    ADMIN_EMAIL, ADMIN_PASSWORD,
    FRONTDESK_EMAIL, FRONTDESK_PASSWORD,
    AUDIO_EMAIL, AUDIO_PASSWORD,
    ACCOUNTS_EMAIL, ACCOUNTS_PASSWORD,
)
BASE_URL = os.environ.get("REACT_APP_BACKEND_URL",
                         "https://referral-sprint.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"


def _login(email, password):
    r = requests.post(f"{API}/auth/login",
                      json={"email": email, "password": password}, timeout=10)
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def h(tok):
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


@pytest.fixture(scope="module")
def admin(): return _login(ADMIN_EMAIL, ADMIN_PASSWORD)
@pytest.fixture(scope="module")
def fd(): return _login(FRONTDESK_EMAIL, FRONTDESK_PASSWORD)


@pytest.fixture(scope="module")
def branch_id(admin):
    bs = requests.get(f"{API}/branches", headers=h(admin), timeout=10).json()
    return [b for b in bs if b.get("is_primary")][0]["branch_id"]


@pytest.fixture
def fresh_ticket(admin, fd, branch_id):
    """Create a plain service ticket via the existing endpoint; returns ticket_no."""
    uid = uuid.uuid4().hex[:6].upper()
    p = requests.post(f"{API}/patients", headers=h(admin),
                      json={"name": f"SvcJob_{uid}", "age": 60, "gender": "Male",
                            "mobile": f"91{uuid.uuid4().int % 100000000:08d}"},
                      timeout=10).json()
    r = requests.post(f"{API}/ha/service-tickets", headers=h(fd),
                      json={"branch_id": branch_id, "patient_id": p["patient_id"],
                            "kind": "repair",
                            "complaint": f"Test complaint {uid}"}, timeout=10)
    assert r.status_code == 201, r.text
    return r.json()["ticket_no"]


# ==================== 12.A — STATE MACHINE ====================

class TestStateMachine:
    def test_legacy_ticket_maps_to_received(self, admin, fresh_ticket):
        """New tickets go in at status=open; our normaliser treats that as RECEIVED
        for transition purposes."""
        r = requests.get(f"{API}/ha/service-tickets/{fresh_ticket}", headers=h(admin),
                         timeout=10)
        assert r.status_code == 200
        # May be 'open' (legacy default) or 'RECEIVED' — either is OK
        assert r.json()["status"] in ("open", "RECEIVED")

    def test_happy_path_through_pipeline(self, admin, fresh_ticket, fd):
        seq = ["INSPECTED", "AWAITING_DISPATCH", "DISPATCHED", "IN_TRANSIT",
               "DELIVERED_TO_COMPANY"]
        for st in seq:
            r = requests.post(
                f"{API}/ha/service-tickets/{fresh_ticket}/transition",
                headers=h(fd), json={"to_status": st, "note": f"→ {st}"}, timeout=10,
            )
            assert r.status_code == 200, r.text
            assert r.json()["to"] == st

    def test_illegal_transition_409(self, admin, fresh_ticket, fd):
        # Try to jump from RECEIVED → CLOSED (skipping the whole pipeline)
        r = requests.post(
            f"{API}/ha/service-tickets/{fresh_ticket}/transition",
            headers=h(fd), json={"to_status": "CLOSED"}, timeout=10,
        )
        assert r.status_code == 409
        assert "illegal" in r.text.lower()

    def test_cancel_from_any_state(self, admin, fresh_ticket, fd):
        r = requests.post(
            f"{API}/ha/service-tickets/{fresh_ticket}/transition",
            headers=h(fd), json={"to_status": "CANCELLED", "note": "Test cancel"},
            timeout=10,
        )
        assert r.status_code == 200

    def test_role_gate_accounts_blocked(self, admin, fresh_ticket):
        acc = _login(ACCOUNTS_EMAIL, ACCOUNTS_PASSWORD)
        r = requests.post(
            f"{API}/ha/service-tickets/{fresh_ticket}/transition",
            headers=h(acc), json={"to_status": "INSPECTED"}, timeout=10,
        )
        assert r.status_code == 403


# ==================== 12.B — COURIER SHIPMENTS ====================

class TestCouriers:
    def test_book_outbound(self, fd, fresh_ticket, admin):
        # Walk ticket → AWAITING_DISPATCH so we can book a courier
        for st in ("INSPECTED", "AWAITING_DISPATCH"):
            requests.post(f"{API}/ha/service-tickets/{fresh_ticket}/transition",
                          headers=h(fd), json={"to_status": st}, timeout=10)
        awb = f"BD{uuid.uuid4().hex[:10].upper()}"
        r = requests.post(f"{API}/ha/couriers", headers=h(fd), json={
            "ticket_no": fresh_ticket, "direction": "OUTBOUND",
            "courier_partner": "Bluedart", "awb_number": awb,
            "dispatch_date": "2026-04-22", "eta_date": "2026-04-25",
            "to_address": "Phonak India Service Centre, Mumbai",
        }, timeout=10)
        assert r.status_code == 201, r.text
        shipment = r.json()
        assert shipment["status"] == "BOOKED"
        assert shipment["shipment_id"].startswith("CSH-")

        # Duplicate AWB + direction → 409
        r2 = requests.post(f"{API}/ha/couriers", headers=h(fd), json={
            "ticket_no": fresh_ticket, "direction": "OUTBOUND",
            "courier_partner": "Bluedart", "awb_number": awb,
        }, timeout=10)
        assert r2.status_code == 409

    def test_shipment_status_transitions(self, fd, fresh_ticket, admin):
        for st in ("INSPECTED", "AWAITING_DISPATCH"):
            requests.post(f"{API}/ha/service-tickets/{fresh_ticket}/transition",
                          headers=h(fd), json={"to_status": st}, timeout=10)
        awb = f"DT{uuid.uuid4().hex[:10].upper()}"
        shp = requests.post(f"{API}/ha/couriers", headers=h(fd), json={
            "ticket_no": fresh_ticket, "direction": "OUTBOUND",
            "courier_partner": "DTDC", "awb_number": awb,
        }, timeout=10).json()
        sid = shp["shipment_id"]
        # Walk statuses
        for st in ("PICKED_UP", "IN_TRANSIT", "DELIVERED"):
            r = requests.post(f"{API}/ha/couriers/{sid}/status",
                              headers=h(fd), json={"to_status": st}, timeout=10)
            assert r.status_code == 200, (st, r.text)
        # Illegal: DELIVERED → IN_TRANSIT
        r = requests.post(f"{API}/ha/couriers/{sid}/status",
                          headers=h(fd), json={"to_status": "IN_TRANSIT"}, timeout=10)
        assert r.status_code == 409

    def test_outbound_delivered_auto_advances_job(self, admin, fd, branch_id):
        """When outbound courier flips to DELIVERED, linked ticket auto-moves
        to DELIVERED_TO_COMPANY."""
        # New fresh ticket for isolation
        uid = uuid.uuid4().hex[:6].upper()
        p = requests.post(f"{API}/patients", headers=h(admin),
                          json={"name": f"AutoAdv_{uid}", "age": 55, "gender": "Male",
                                "mobile": f"92{uuid.uuid4().int % 100000000:08d}"},
                          timeout=10).json()
        ticket = requests.post(f"{API}/ha/service-tickets", headers=h(fd),
                               json={"branch_id": branch_id,
                                     "patient_id": p["patient_id"],
                                     "complaint": "auto-advance test"},
                               timeout=10).json()["ticket_no"]
        for st in ("INSPECTED", "AWAITING_DISPATCH", "DISPATCHED"):
            requests.post(f"{API}/ha/service-tickets/{ticket}/transition",
                          headers=h(fd), json={"to_status": st}, timeout=10)
        awb = f"DL{uuid.uuid4().hex[:10].upper()}"
        shp = requests.post(f"{API}/ha/couriers", headers=h(fd), json={
            "ticket_no": ticket, "direction": "OUTBOUND",
            "courier_partner": "Delhivery", "awb_number": awb,
        }, timeout=10).json()
        for st in ("PICKED_UP", "DELIVERED"):
            requests.post(f"{API}/ha/couriers/{shp['shipment_id']}/status",
                          headers=h(fd), json={"to_status": st}, timeout=10)
        # Ticket should now be DELIVERED_TO_COMPANY
        t = requests.get(f"{API}/ha/service-tickets/{ticket}",
                         headers=h(admin), timeout=10).json()
        assert t["status"] == "DELIVERED_TO_COMPANY"


# ==================== 12.B — ESTIMATES + APPROVALS ====================

class TestEstimatesApprovals:
    def test_full_estimate_to_approval_flow(self, admin, fd, branch_id):
        uid = uuid.uuid4().hex[:6].upper()
        p = requests.post(f"{API}/patients", headers=h(admin),
                          json={"name": f"EstPat_{uid}", "age": 55, "gender": "Female",
                                "mobile": f"93{uuid.uuid4().int % 100000000:08d}"},
                          timeout=10).json()
        t_no = requests.post(f"{API}/ha/service-tickets", headers=h(fd),
                             json={"branch_id": branch_id,
                                   "patient_id": p["patient_id"],
                                   "complaint": "Estimate test"},
                             timeout=10).json()["ticket_no"]
        # Walk to DELIVERED_TO_COMPANY
        for st in ("INSPECTED", "AWAITING_DISPATCH", "DISPATCHED",
                   "IN_TRANSIT", "DELIVERED_TO_COMPANY"):
            r = requests.post(f"{API}/ha/service-tickets/{t_no}/transition",
                              headers=h(fd), json={"to_status": st}, timeout=10)
            assert r.status_code == 200, (st, r.text)

        # Record estimate
        est = requests.post(f"{API}/ha/service-estimates", headers=h(fd), json={
            "ticket_no": t_no, "vendor_name": "Phonak India",
            "amount": 4500, "warranty_covered": False,
            "eta_days": 4, "repair_notes": "Replace receiver",
        }, timeout=10)
        assert est.status_code == 201, est.text
        est_id = est.json()["estimate_id"]
        assert est_id.startswith("EST-")

        # Ticket should now be ESTIMATE_PENDING + have estimate_id + approval_id
        t = requests.get(f"{API}/ha/service-tickets/{t_no}",
                         headers=h(admin), timeout=10).json()
        assert t["status"] == "ESTIMATE_PENDING"

        # Fetch pipeline — should show 1 shipment link maybe, 1 estimate, 1 pending approval
        pipe = requests.get(f"{API}/ha/service-jobs/{t_no}/pipeline",
                            headers=h(admin), timeout=10).json()
        assert len(pipe["estimates"]) == 1
        assert len(pipe["approvals"]) == 1
        assert pipe["approvals"][0]["decision"] == "PENDING"

        # Customer approves
        aid = pipe["approvals"][0]["approval_id"]
        ap = requests.post(f"{API}/ha/customer-approvals/{aid}/decide",
                           headers=h(fd), json={"decision": "APPROVED"}, timeout=10)
        assert ap.status_code == 200
        assert ap.json()["decision"] == "APPROVED"

        # Ticket now CLIENT_APPROVED
        t = requests.get(f"{API}/ha/service-tickets/{t_no}",
                         headers=h(admin), timeout=10).json()
        assert t["status"] == "CLIENT_APPROVED"

        # Double-decide same approval → 409
        ap2 = requests.post(f"{API}/ha/customer-approvals/{aid}/decide",
                            headers=h(fd), json={"decision": "REJECTED"}, timeout=10)
        assert ap2.status_code == 409

    def test_estimate_blocked_before_delivery_to_company(self, admin, fd, fresh_ticket):
        r = requests.post(f"{API}/ha/service-estimates", headers=h(fd), json={
            "ticket_no": fresh_ticket, "amount": 1000,
        }, timeout=10)
        assert r.status_code == 409

    def test_rejected_approval_advances_to_client_rejected(self, admin, fd, branch_id):
        uid = uuid.uuid4().hex[:6].upper()
        p = requests.post(f"{API}/patients", headers=h(admin),
                          json={"name": f"Rej_{uid}", "age": 50, "gender": "Male",
                                "mobile": f"94{uuid.uuid4().int % 100000000:08d}"},
                          timeout=10).json()
        t_no = requests.post(f"{API}/ha/service-tickets", headers=h(fd),
                             json={"branch_id": branch_id,
                                   "patient_id": p["patient_id"],
                                   "complaint": "Reject test"},
                             timeout=10).json()["ticket_no"]
        for st in ("INSPECTED", "AWAITING_DISPATCH", "DISPATCHED",
                   "IN_TRANSIT", "DELIVERED_TO_COMPANY"):
            requests.post(f"{API}/ha/service-tickets/{t_no}/transition",
                          headers=h(fd), json={"to_status": st}, timeout=10)
        requests.post(f"{API}/ha/service-estimates", headers=h(fd), json={
            "ticket_no": t_no, "amount": 8000, "warranty_covered": False,
        }, timeout=10).json()
        pipe = requests.get(f"{API}/ha/service-jobs/{t_no}/pipeline",
                            headers=h(admin), timeout=10).json()
        aid = pipe["approvals"][0]["approval_id"]
        requests.post(f"{API}/ha/customer-approvals/{aid}/decide",
                      headers=h(fd),
                      json={"decision": "REJECTED",
                            "notes": "Too expensive"}, timeout=10)
        t = requests.get(f"{API}/ha/service-tickets/{t_no}",
                         headers=h(admin), timeout=10).json()
        assert t["status"] == "CLIENT_REJECTED"


# ==================== 12.C — REPAIR ANALYTICS + PDF + WHATSAPP ====================

class TestRepairAnalytics:
    def test_analytics_shape(self, admin):
        r = requests.get(f"{API}/ha/repair/analytics?days=90",
                         headers=h(admin), timeout=10)
        assert r.status_code == 200
        d = r.json()
        for key in ("live", "closed", "repeat_failures", "by_brand", "window_days"):
            assert key in d
        for k in ("in_repair", "couriers_in_transit", "awaiting_approval"):
            assert k in d["live"]

    def test_analytics_role_gate_accounts_allowed(self):
        # Accounts role has access to analytics
        tok = _login(ACCOUNTS_EMAIL, ACCOUNTS_PASSWORD)
        r = requests.get(f"{API}/ha/repair/analytics", headers=h(tok), timeout=10)
        assert r.status_code == 200


class TestJobCardPDF:
    def test_pdf_renders(self, admin, fresh_ticket):
        r = requests.get(f"{API}/ha/service-tickets/{fresh_ticket}/job-card.pdf",
                         headers=h(admin), timeout=20)
        assert r.status_code == 200
        assert "pdf" in r.headers.get("content-type", "").lower()
        assert r.content[:4] == b"%PDF"

    def test_pdf_unknown_ticket_404(self, admin):
        r = requests.get(f"{API}/ha/service-tickets/JOB-NOPE-0000/job-card.pdf",
                         headers=h(admin), timeout=10)
        assert r.status_code == 404


class TestWhatsAppTemplates:
    def test_default_uses_current_status(self, admin, fresh_ticket):
        r = requests.get(f"{API}/ha/service-tickets/{fresh_ticket}/whatsapp",
                         headers=h(admin), timeout=10)
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True
        # New tickets may be 'open' (legacy) which normalises to RECEIVED
        assert d["status"] in ("RECEIVED", "open")
        if d["message"]:
            assert "JOB-" in d["message"] or fresh_ticket in d["message"]

    def test_forced_status_ready_for_pickup(self, admin, fresh_ticket):
        r = requests.get(
            f"{API}/ha/service-tickets/{fresh_ticket}/whatsapp?status=READY_FOR_PICKUP",
            headers=h(admin), timeout=10,
        )
        assert r.status_code == 200
        d = r.json()
        assert "ready for pickup" in (d.get("message") or "").lower()
        # URL should be wa.me + percent-encoded message (if mobile is set)
        if d.get("url"):
            assert d["url"].startswith("https://wa.me/91")

    def test_noisy_statuses_return_null(self, admin, fresh_ticket):
        r = requests.get(
            f"{API}/ha/service-tickets/{fresh_ticket}/whatsapp?status=IN_TRANSIT",
            headers=h(admin), timeout=10,
        )
        assert r.status_code == 200
        assert r.json()["message"] is None


# ==================== TRIAL EXPIRY ====================

class TestTrialExpiry:
    def test_expiry_scan_flips_expired_clinic(self):
        """Direct-motor test — insert a clinic with trial_ends_at in the past,
        run the scanner, assert tier is now BASIC."""
        import asyncio
        from motor.motor_asyncio import AsyncIOMotorClient
        from trial_expiry import run_trial_expiry_scan

        async def _run():
            client = AsyncIOMotorClient(os.environ["MONGO_URL"])
            db = client[os.environ["DB_NAME"]]
            cid = f"trial-test-{uuid.uuid4().hex[:6]}"
            try:
                await db.clinics.insert_one({
                    "clinic_id": cid,
                    "name": "Trial Expiry Test Clinic",
                    "subscription_tier": "PREMIUM",
                    "trial_ends_at": datetime.now(timezone.utc) - timedelta(days=1),
                })
                n = await run_trial_expiry_scan(db)
                assert n >= 1
                after = await db.clinics.find_one({"clinic_id": cid})
                assert after["subscription_tier"] == "BASIC"
                assert "trial_ends_at" not in after
                assert after["tier_auto_downgraded_from_trial"] is True
            finally:
                await db.clinics.delete_one({"clinic_id": cid})
                client.close()

        asyncio.run(_run())

    def test_expiry_leaves_active_trials_alone(self):
        import asyncio
        from motor.motor_asyncio import AsyncIOMotorClient
        from trial_expiry import run_trial_expiry_scan

        async def _run():
            client = AsyncIOMotorClient(os.environ["MONGO_URL"])
            db = client[os.environ["DB_NAME"]]
            cid = f"trial-active-{uuid.uuid4().hex[:6]}"
            try:
                await db.clinics.insert_one({
                    "clinic_id": cid, "name": "Active Trial",
                    "subscription_tier": "PREMIUM",
                    "trial_ends_at": datetime.now(timezone.utc) + timedelta(days=7),
                })
                await run_trial_expiry_scan(db)
                after = await db.clinics.find_one({"clinic_id": cid})
                assert after["subscription_tier"] == "PREMIUM"
                assert "trial_ends_at" in after
            finally:
                await db.clinics.delete_one({"clinic_id": cid})
                client.close()

        asyncio.run(_run())
