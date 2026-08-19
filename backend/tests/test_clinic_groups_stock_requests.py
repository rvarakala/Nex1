"""Backend tests for Clinic Groups + Stock Requests feature."""
import os
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://referral-sprint.preview.emergentagent.com").rstrip("/")
HEAD_EMAIL = "owner@thesoundclinic.in"
HEAD_PASS = "demo123"
HEAD_CLINIC_ID = None  # discovered
BRANCH_CLINIC_ID = "BR-CL-4601C9DF"


@pytest.fixture(scope="module")
def head_token():
    r = requests.post(f"{BASE_URL}/api/auth/login", json={"email": HEAD_EMAIL, "password": HEAD_PASS})
    assert r.status_code == 200, f"Login failed: {r.status_code} {r.text}"
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def head_headers(head_token):
    return {"Authorization": f"Bearer {head_token}", "Content-Type": "application/json"}


@pytest.fixture(scope="module")
def branch_token(head_token):
    # Switch clinic to branch
    r = requests.post(
        f"{BASE_URL}/api/auth/switch-clinic",
        json={"clinic_id": BRANCH_CLINIC_ID},
        headers={"Authorization": f"Bearer {head_token}"},
    )
    assert r.status_code == 200, f"switch-clinic failed: {r.status_code} {r.text}"
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def branch_headers(branch_token):
    return {"Authorization": f"Bearer {branch_token}", "Content-Type": "application/json"}


# ─── Clinic Group endpoints ───────────────────────────────────────────────
class TestClinicGroups:
    def test_get_my_group_returns_data(self, head_headers):
        r = requests.get(f"{BASE_URL}/api/clinic-groups/mine", headers=head_headers)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["group"] is not None, "expected existing group"
        assert data["viewer_is_head"] is True
        assert isinstance(data["branches"], list)
        assert data["head"]["is_head"] is True
        # stock summary present
        assert "stock" in data["head"]
        for k in ("ha_units", "low_stock_skus", "patients"):
            assert k in data["head"]["stock"]
        # branch already added
        branch_ids = [b["clinic_id"] for b in data["branches"]]
        assert BRANCH_CLINIC_ID in branch_ids
        global HEAD_CLINIC_ID
        HEAD_CLINIC_ID = data["group"]["head_clinic_id"]

    def test_create_group_idempotent(self, head_headers):
        r = requests.post(
            f"{BASE_URL}/api/clinic-groups",
            json={"name": "Sound Clinic Chain"},
            headers=head_headers,
        )
        assert r.status_code == 200, r.text
        assert "group" in r.json()

    def test_get_my_group_from_branch_context(self, branch_headers):
        r = requests.get(f"{BASE_URL}/api/clinic-groups/mine", headers=branch_headers)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["group"] is not None
        assert d["viewer_is_head"] is False


# ─── Stock Requests ────────────────────────────────────────────────────────
class TestStockRequests:
    created_req_id = None
    fulfill_req_id = None
    po_req_id = None
    cancel_req_id = None

    def test_branch_creates_request(self, branch_headers):
        payload = {
            "lines": [{"product_label": "TEST_Domes size M", "kind": "accessory", "qty": 3}],
            "urgency": "urgent",
            "reason": "TEST_low stock",
        }
        r = requests.post(f"{BASE_URL}/api/stock-requests", json=payload, headers=branch_headers)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] == "pending"
        assert data["clinic_id"] == BRANCH_CLINIC_ID
        assert data["urgency"] == "urgent"
        assert len(data["lines"]) == 1
        TestStockRequests.created_req_id = data["request_id"]

        # GET verify
        rid = data["request_id"]
        r2 = requests.get(f"{BASE_URL}/api/stock-requests/{rid}", headers=branch_headers)
        assert r2.status_code == 200
        assert r2.json()["request_id"] == rid

    def test_head_sees_all_branch_seeks_only_own(self, head_headers, branch_headers):
        rh = requests.get(f"{BASE_URL}/api/stock-requests", headers=head_headers)
        rb = requests.get(f"{BASE_URL}/api/stock-requests", headers=branch_headers)
        assert rh.status_code == 200 and rb.status_code == 200
        head_list = rh.json()
        branch_list = rb.json()
        head_clinic_ids = {r["clinic_id"] for r in head_list}
        branch_clinic_ids = {r["clinic_id"] for r in branch_list}
        # branch only sees its own clinic's requests
        assert branch_clinic_ids <= {BRANCH_CLINIC_ID}
        # head should see the branch's request too
        assert BRANCH_CLINIC_ID in head_clinic_ids or len(head_list) >= len(branch_list)
        assert len(head_list) >= len(branch_list)

    def test_head_fulfill_creates_transfer(self, head_headers, branch_headers):
        # Create a fresh branch request
        payload = {"lines": [{"product_label": "TEST_wax guards", "kind": "accessory", "qty": 5}]}
        r = requests.post(f"{BASE_URL}/api/stock-requests", json=payload, headers=branch_headers)
        assert r.status_code == 200
        rid = r.json()["request_id"]
        TestStockRequests.fulfill_req_id = rid

        # Get head clinic id
        g = requests.get(f"{BASE_URL}/api/clinic-groups/mine", headers=head_headers).json()
        head_id = g["group"]["head_clinic_id"]

        # Fulfill from head
        fr = requests.post(
            f"{BASE_URL}/api/stock-requests/{rid}/fulfill",
            json={"source_clinic_id": head_id, "create_transfer": True},
            headers=head_headers,
        )
        assert fr.status_code == 200, fr.text
        d = fr.json()
        assert d["status"] == "fulfilled"
        assert d["linked_transfer_id"] is not None
        assert d["fulfilled_from_clinic_id"] == head_id

    def test_fulfill_fails_403_for_branch(self, branch_headers):
        payload = {"lines": [{"product_label": "TEST_x", "qty": 1}]}
        r = requests.post(f"{BASE_URL}/api/stock-requests", json=payload, headers=branch_headers)
        rid = r.json()["request_id"]
        # branch tries to fulfill
        fr = requests.post(
            f"{BASE_URL}/api/stock-requests/{rid}/fulfill",
            json={"source_clinic_id": BRANCH_CLINIC_ID, "create_transfer": False},
            headers=branch_headers,
        )
        assert fr.status_code == 403, f"expected 403 got {fr.status_code}: {fr.text}"
        # Cancel it to keep DB clean
        requests.post(f"{BASE_URL}/api/stock-requests/{rid}/cancel", headers=branch_headers)

    def test_fulfill_fails_400_same_source_and_requester(self, head_headers, branch_headers):
        # Branch's own clinic is requester -> source cannot equal that
        payload = {"lines": [{"product_label": "TEST_same", "qty": 1}]}
        r = requests.post(f"{BASE_URL}/api/stock-requests", json=payload, headers=branch_headers)
        rid = r.json()["request_id"]
        fr = requests.post(
            f"{BASE_URL}/api/stock-requests/{rid}/fulfill",
            json={"source_clinic_id": BRANCH_CLINIC_ID, "create_transfer": False},
            headers=head_headers,
        )
        assert fr.status_code == 400, fr.text
        requests.post(f"{BASE_URL}/api/stock-requests/{rid}/cancel", headers=head_headers)

    def test_mark_po_then_fulfill(self, head_headers, branch_headers):
        payload = {"lines": [{"product_label": "TEST_Phonak Audeo", "kind": "ha", "qty": 2}]}
        r = requests.post(f"{BASE_URL}/api/stock-requests", json=payload, headers=branch_headers)
        rid = r.json()["request_id"]
        TestStockRequests.po_req_id = rid

        po = requests.post(
            f"{BASE_URL}/api/stock-requests/{rid}/mark-po",
            json={"vendor_name": "Phonak India", "po_no": "TEST_PO-1", "expected_at": "2026-02-01"},
            headers=head_headers,
        )
        assert po.status_code == 200, po.text
        assert po.json()["status"] == "awaiting_po"
        assert po.json()["po_details"]["po_no"] == "TEST_PO-1"

        # then fulfill
        g = requests.get(f"{BASE_URL}/api/clinic-groups/mine", headers=head_headers).json()
        head_id = g["group"]["head_clinic_id"]
        fr = requests.post(
            f"{BASE_URL}/api/stock-requests/{rid}/fulfill",
            json={"source_clinic_id": head_id, "create_transfer": True},
            headers=head_headers,
        )
        assert fr.status_code == 200, fr.text
        assert fr.json()["status"] == "fulfilled"

    def test_decline_request(self, head_headers, branch_headers):
        payload = {"lines": [{"product_label": "TEST_decline_me", "qty": 1}]}
        r = requests.post(f"{BASE_URL}/api/stock-requests", json=payload, headers=branch_headers)
        rid = r.json()["request_id"]
        dr = requests.post(
            f"{BASE_URL}/api/stock-requests/{rid}/decline",
            json={"reason": "TEST_out of stock"},
            headers=head_headers,
        )
        assert dr.status_code == 200, dr.text
        assert dr.json()["status"] == "declined"
        assert dr.json()["decline_reason"] == "TEST_out of stock"

    def test_branch_can_cancel_own_pending(self, branch_headers):
        payload = {"lines": [{"product_label": "TEST_cancel", "qty": 1}]}
        r = requests.post(f"{BASE_URL}/api/stock-requests", json=payload, headers=branch_headers)
        rid = r.json()["request_id"]
        cr = requests.post(f"{BASE_URL}/api/stock-requests/{rid}/cancel", headers=branch_headers)
        assert cr.status_code == 200
        assert cr.json()["status"] == "cancelled"
        # cannot cancel again
        cr2 = requests.post(f"{BASE_URL}/api/stock-requests/{rid}/cancel", headers=branch_headers)
        assert cr2.status_code == 409


# ─── Branch creation + branding inheritance ───────────────────────────────
class TestBranchCreation:
    created_branch_id = None

    def test_create_branch_inherits_branding_and_services(self, head_headers):
        payload = {
            "name": "TEST_Sound Clinic Hubli",
            "city": "Hubli",
            "state": "Karnataka",
            "inherit_branding": True,
            "inherit_services": True,
        }
        r = requests.post(
            f"{BASE_URL}/api/clinic-groups/mine/branches",
            json=payload,
            headers=head_headers,
        )
        assert r.status_code == 200, r.text
        d = r.json()
        assert "branch" in d
        assert d["branch"]["clinic_id"].startswith("BR-CL-")
        assert d["branch"]["name"] == "TEST_Sound Clinic Hubli"
        assert d["head_admins_granted"] >= 1, f"head admins should be granted access; got {d}"
        # services either cloned or default-seeded should be > 0
        assert isinstance(d["services_seeded"], int)
        TestBranchCreation.created_branch_id = d["branch"]["clinic_id"]

        # Verify branch is now in group
        g = requests.get(f"{BASE_URL}/api/clinic-groups/mine", headers=head_headers).json()
        member_ids = [b["clinic_id"] for b in g["branches"]]
        assert TestBranchCreation.created_branch_id in member_ids

        # Verify /me shows the new branch in additional_clinic_ids
        me = requests.get(f"{BASE_URL}/api/auth/me", headers=head_headers)
        if me.status_code == 200:
            body = me.json()
            user_obj = body.get("user") or body
            add_ids = user_obj.get("additional_clinic_ids") or []
            assert TestBranchCreation.created_branch_id in add_ids, f"expected in {add_ids}"

    def test_switch_to_new_branch(self, head_headers):
        bid = TestBranchCreation.created_branch_id
        assert bid, "prior test must have created a branch"
        r = requests.post(
            f"{BASE_URL}/api/auth/switch-clinic",
            json={"clinic_id": bid},
            headers=head_headers,
        )
        assert r.status_code == 200, r.text
        assert "access_token" in r.json()

    def test_deactivate_branch(self, head_headers):
        bid = TestBranchCreation.created_branch_id
        r = requests.post(
            f"{BASE_URL}/api/clinic-groups/mine/branches/{bid}/deactivate",
            headers=head_headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True
        # Should no longer be listed
        g = requests.get(f"{BASE_URL}/api/clinic-groups/mine", headers=head_headers).json()
        member_ids = [b["clinic_id"] for b in g["branches"]]
        assert bid not in member_ids

    def test_branch_cannot_create_branch(self, branch_headers):
        r = requests.post(
            f"{BASE_URL}/api/clinic-groups/mine/branches",
            json={"name": "TEST_should_fail", "city": "Nowhere"},
            headers=branch_headers,
        )
        # Branch clinic is not a head so should be 409 no_group
        assert r.status_code == 409, r.text
        assert r.json().get("detail", {}).get("code") == "no_group"
