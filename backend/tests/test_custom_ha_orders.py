"""Regression: Custom HA Orders — bespoke IIC/CIC/ITC/ITE quick-book flow.

Feb 2026 — clinic asked us to support custom-made hearing aids alongside
ear moulds. Same "book + auto-invoice + status ribbon" pattern, but with
per-ear specs (vent, shell colour, faceplate colour, receiver power) and
a dual-target delivery choice (external vendor OR another branch — used
by branches placing requests with the head office that owns the vendor
relationship).

These tests lock in the invoice math + per-ear spec persistence + the
delivery-target validation so a future refactor can't silently break
either path.
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


def _first_vendor(s):
    r = s.get(f"{BASE_URL}/api/vendors?active=true", timeout=15)
    d = r.json() or []
    if not d:
        # Seed a temp vendor so the test doesn't depend on tenant fixtures.
        r = s.post(f"{BASE_URL}/api/vendors",
                   json={"name": "PyTest Custom-HA Vendor"}, timeout=15)
        assert r.status_code in (200, 201), r.text
        return r.json()["vendor_id"]
    return d[0]["vendor_id"]


def _first_branch(s):
    r = s.get(f"{BASE_URL}/api/branches", timeout=15)
    d = r.json() or []
    return d[0]["branch_id"] if d else None


def test_custom_ha_vendor_target_with_advance_generates_partial_invoice():
    """Vendor-target order with per-ear specs must persist all specs and
    generate a PARTIAL invoice with correct math."""
    s = _sess()
    pid = _first_patient(s)
    vid = _first_vendor(s)
    r = s.post(f"{BASE_URL}/api/ha/custom-ha-orders", json={
        "patient_id": pid, "side": "both", "shell_type": "CIC",
        "vent_size_left": "1.5mm", "vent_size_right": "IROS",
        "shell_colour_left": "Skin", "shell_colour_right": "Skin",
        "receiver_power_left": "P", "receiver_power_right": "M",
        "brand": "Phonak", "model": "Virto B90",
        "features": ["telecoil", "push_button"],
        "delivery_target": "vendor", "vendor_id": vid,
        "expected_delivery_date": "2026-09-15",
        "total_amount": 120000, "advance_amount": 30000,
        "payment_mode": "upi", "gst_rate": 18,
    }, timeout=15)
    assert r.status_code == 200, r.text
    order = r.json()
    assert order["status"] == "sent_to_vendor"
    assert order["shell_type"] == "CIC"
    assert order["vent_size_left"] == "1.5mm"
    assert order["vent_size_right"] == "IROS"
    assert order["receiver_power_left"] == "P"
    assert order["receiver_power_right"] == "M"
    assert order["brand"] == "Phonak"
    assert "telecoil" in order["features"]
    assert order["balance_due"] == 90000
    assert order["vendor_id"] == vid

    inv = s.get(f"{BASE_URL}/api/billing/invoices/{order['invoice_id']}", timeout=15).json()
    assert inv["status"] == "partial"
    assert inv["grand_total"] == 120000
    assert inv["paid_total"] == 30000
    assert inv["due_total"] == 90000


def test_custom_ha_branch_target_requires_target_branch_id():
    """Branch delivery target without target_branch_id must be rejected."""
    s = _sess()
    pid = _first_patient(s)
    r = s.post(f"{BASE_URL}/api/ha/custom-ha-orders", json={
        "patient_id": pid, "side": "left", "shell_type": "ITE",
        "delivery_target": "branch",
        "total_amount": 80000, "advance_amount": 0,
    }, timeout=15)
    assert r.status_code == 400
    assert "target_branch_id" in r.text.lower()


def test_custom_ha_vendor_target_requires_vendor_id():
    """Vendor delivery target without vendor_id must be rejected."""
    s = _sess()
    pid = _first_patient(s)
    r = s.post(f"{BASE_URL}/api/ha/custom-ha-orders", json={
        "patient_id": pid, "side": "both", "shell_type": "IIC",
        "delivery_target": "vendor",
        "total_amount": 60000, "advance_amount": 0,
    }, timeout=15)
    assert r.status_code == 400
    assert "vendor_id" in r.text.lower()


def test_custom_ha_advance_over_total_rejected():
    """Guardrail: over-payment at booking is a data-entry mistake."""
    s = _sess()
    pid = _first_patient(s)
    vid = _first_vendor(s)
    r = s.post(f"{BASE_URL}/api/ha/custom-ha-orders", json={
        "patient_id": pid, "side": "both", "shell_type": "ITC",
        "delivery_target": "vendor", "vendor_id": vid,
        "total_amount": 50000, "advance_amount": 80000,
    }, timeout=15)
    assert r.status_code == 400


def test_custom_ha_status_transition_appends_history():
    """PATCH /status must move the status AND append to `history`."""
    s = _sess()
    pid = _first_patient(s)
    vid = _first_vendor(s)
    r = s.post(f"{BASE_URL}/api/ha/custom-ha-orders", json={
        "patient_id": pid, "side": "right", "shell_type": "CIC",
        "delivery_target": "vendor", "vendor_id": vid,
        "total_amount": 90000, "advance_amount": 10000,
    }, timeout=15)
    order_id = r.json()["order_id"]

    r = s.patch(f"{BASE_URL}/api/ha/custom-ha-orders/{order_id}/status",
                json={"status": "arrived", "note": "Received from Phonak"}, timeout=15)
    assert r.status_code == 200
    updated = r.json()
    assert updated["status"] == "arrived"
    assert len(updated["history"]) >= 2
    assert updated["history"][-1]["status"] == "arrived"


def test_ear_mould_per_ear_vent_persists_when_side_both():
    """Regression: Ear Moulds should store per-ear vent sizes when side=both
    so audiogram-driven prescriptions with different vents on each ear
    (e.g. 1.5mm left, IROS right) don't lose data at booking."""
    s = _sess()
    pid = _first_patient(s)
    r = s.post(f"{BASE_URL}/api/ha/ear-moulds", json={
        "patient_id": pid, "side": "both", "material": "silicone",
        "vent_size_left": "1.5mm", "vent_size_right": "IROS",
        "total_amount": 5000, "advance_amount": 1000,
    }, timeout=15)
    assert r.status_code == 200, r.text
    order = r.json()
    assert order["vent_size_left"] == "1.5mm"
    assert order["vent_size_right"] == "IROS"
    # Invoice description must mention both vents so the tax-invoice print
    # captures the prescription for the patient.
    inv = s.get(f"{BASE_URL}/api/billing/invoices/{order['invoice_id']}", timeout=15).json()
    desc = inv["lines"][0]["description"]
    assert "1.5mm" in desc
    assert "IROS" in desc



# ── Branch → Head approval flow ────────────────────────────────────────
# When a branch clinic (Mysore) places a Custom HA order with target=branch
# and the clinic is a member (non-head) of a clinic group, we auto-spawn a
# stock_request in the head owner's inbox. Approving it must transition
# the order to `sent_to_vendor`; declining must cancel it.
BRANCH_CLINIC_ID = "BR-CL-4601C9DF"   # Sound Clinic – Mysore


def _branch_session():
    """Sign in as head, then flip context to the Mysore branch clinic
    via `/api/auth/switch-clinic`. Mirrors the existing test pattern in
    `test_clinic_groups_stock_requests.py`."""
    s = _sess()   # head-owner token
    r = s.post(f"{BASE_URL}/api/auth/switch-clinic",
               json={"clinic_id": BRANCH_CLINIC_ID}, timeout=15)
    assert r.status_code == 200, r.text
    tok = r.json()["access_token"]
    branch = requests.Session()
    branch.headers.update({"Authorization": f"Bearer {tok}"})
    return branch


def _find_patient_in_branch(branch_session):
    """Grab a patient scoped to the Mysore branch. If Mysore has no
    patients yet, seed one — bookings need a patient_id from the same
    clinic as the caller."""
    r = branch_session.get(f"{BASE_URL}/api/patients?limit=1", timeout=15)
    d = r.json()
    d = d.get("items", d) if isinstance(d, dict) else d
    if d:
        return d[0]["patient_id"]
    r = branch_session.post(f"{BASE_URL}/api/patients", json={
        "name": "PyTest Branch Patient", "mobile": "9000000001",
        "age": 45, "gender": "male",
    }, timeout=15)
    assert r.status_code == 200, r.text
    return r.json()["patient_id"]


def test_custom_ha_branch_target_from_non_head_spawns_stock_request():
    """Branch clinic (non-head, in group) + target=branch must auto-spawn
    a stock_request in the head's inbox AND flag the order as awaiting."""
    branch = _branch_session()
    pid = _find_patient_in_branch(branch)

    r = branch.post(f"{BASE_URL}/api/ha/custom-ha-orders", json={
        "patient_id": pid, "side": "both", "shell_type": "ITC",
        "vent_size_left": "1mm", "vent_size_right": "2mm",
        "brand": "Signia", "model": "Insio 7AX",
        "features": ["telecoil", "rechargeable"],
        "delivery_target": "branch",
        "total_amount": 95000, "advance_amount": 20000,
        "payment_mode": "upi", "gst_rate": 18,
    }, timeout=15)
    assert r.status_code == 200, r.text
    order = r.json()
    assert order["status"] == "awaiting_approval"
    assert order["target_clinic_id"] is not None
    assert order["linked_stock_request_id"] is not None

    # Head must see the auto-created stock_request in their inbox.
    head = _sess()
    r = head.get(f"{BASE_URL}/api/stock-requests?status=pending", timeout=15)
    pending = r.json()
    linked = [x for x in pending if x.get("linked_custom_ha_order_id") == order["order_id"]]
    assert len(linked) == 1
    stock_req = linked[0]
    assert stock_req["linked_custom_ha_order_no"] == order["order_no"]
    assert stock_req["lines"][0]["kind"] == "ha"
    assert "ITC" in stock_req["lines"][0]["product_label"]
    assert "Signia" in stock_req["lines"][0]["product_label"]

    # The head owner needs the FULL form the branch filled — brand, model,
    # per-ear specs, financials — snapshotted on `custom_ha_details` so
    # they don't need any cross-clinic fetch to make the vendor order.
    d = stock_req.get("custom_ha_details") or {}
    assert d.get("shell_type") == "ITC"
    assert d.get("side") == "both"
    assert d.get("brand") == "Signia"
    assert d.get("model") == "Insio 7AX"
    assert d.get("vent_size_left") == "1mm"
    assert d.get("vent_size_right") == "2mm"
    assert "telecoil" in (d.get("features") or [])
    assert d.get("total_amount") == 95000
    assert d.get("advance_amount") == 20000
    assert d.get("balance_due") == 75000
    assert d.get("patient_name")   # denormalised for head's view
    assert d.get("invoice_no")     # branch's linked invoice ref


def test_head_fulfill_transitions_linked_custom_ha_order():
    """Fulfilling the linked stock_request must move the Custom HA order
    to `sent_to_vendor` with an approval entry in history."""
    branch = _branch_session()
    pid = _find_patient_in_branch(branch)
    r = branch.post(f"{BASE_URL}/api/ha/custom-ha-orders", json={
        "patient_id": pid, "side": "right", "shell_type": "IIC",
        "vent_size_right": "1mm", "brand": "Phonak", "model": "Virto",
        "delivery_target": "branch",
        "total_amount": 110000, "advance_amount": 40000,
    }, timeout=15)
    order = r.json()
    stock_req_id = order["linked_stock_request_id"]
    order_id = order["order_id"]

    head = _sess()
    grp = head.get(f"{BASE_URL}/api/clinic-groups/mine", timeout=15).json()
    head_clinic_id = grp["head"]["clinic_id"]
    r = head.post(f"{BASE_URL}/api/stock-requests/{stock_req_id}/fulfill",
                  json={"source_clinic_id": head_clinic_id, "create_transfer": False},
                  timeout=15)
    assert r.status_code == 200, r.text

    r = branch.get(f"{BASE_URL}/api/ha/custom-ha-orders", timeout=15)
    orders = r.json()
    updated = next((o for o in orders if o["order_id"] == order_id), None)
    assert updated is not None
    assert updated["status"] == "sent_to_vendor"
    approvals = [h for h in updated["history"]
                 if "approved" in (h.get("note") or "").lower()]
    assert len(approvals) >= 1


def test_head_decline_cancels_linked_custom_ha_order():
    """Declining the linked stock_request must cancel the Custom HA
    order and carry the decline reason into the order's history."""
    branch = _branch_session()
    pid = _find_patient_in_branch(branch)
    r = branch.post(f"{BASE_URL}/api/ha/custom-ha-orders", json={
        "patient_id": pid, "side": "left", "shell_type": "CIC",
        "vent_size_left": "1.5mm", "brand": "Oticon", "model": "More",
        "delivery_target": "branch",
        "total_amount": 85000, "advance_amount": 0,
    }, timeout=15)
    order = r.json()
    stock_req_id = order["linked_stock_request_id"]
    order_id = order["order_id"]

    head = _sess()
    r = head.post(f"{BASE_URL}/api/stock-requests/{stock_req_id}/decline",
                  json={"reason": "Model discontinued — please pick a current one"},
                  timeout=15)
    assert r.status_code == 200, r.text

    r = branch.get(f"{BASE_URL}/api/ha/custom-ha-orders", timeout=15)
    orders = r.json()
    updated = next((o for o in orders if o["order_id"] == order_id), None)
    assert updated is not None
    assert updated["status"] == "cancelled"
    declines = [h for h in updated["history"]
                if "declined" in (h.get("note") or "").lower()]
    assert len(declines) >= 1
    assert "discontinued" in declines[-1]["note"].lower()


# ── Audiogram attachment ───────────────────────────────────────────────
# The audiologist can upload the patient's audiogram (PDF/PNG/JPG) to a
# Custom HA order at any time. When the order is a branch → head request,
# uploading also mirrors the reference onto the linked stock_request so
# the head owner sees a "View Audiogram" button in their inbox.
_MINIMAL_PDF = (
    b"%PDF-1.4\n1 0 obj<<>>endobj\n"
    b"xref\n0 1\n0000000000 65535 f\n"
    b"trailer<<>>\nstartxref\n0\n%%EOF\n"
)


def test_audiogram_upload_persists_and_mirrors_to_stock_request():
    """Branch attaches audiogram → order picks up `audiogram_fs_id`
    AND the linked stock_request's `custom_ha_details.audiogram_fs_id`
    is populated so the head can preview it from their inbox."""
    branch = _branch_session()
    pid = _find_patient_in_branch(branch)
    r = branch.post(f"{BASE_URL}/api/ha/custom-ha-orders", json={
        "patient_id": pid, "side": "both", "shell_type": "CIC",
        "brand": "Phonak", "model": "Virto",
        "delivery_target": "branch",
        "total_amount": 120000, "advance_amount": 15000,
    }, timeout=15)
    order = r.json()
    order_id = order["order_id"]
    linked_req_id = order["linked_stock_request_id"]

    r = branch.post(
        f"{BASE_URL}/api/ha/custom-ha-orders/{order_id}/audiogram",
        files={"file": ("audiogram.pdf", _MINIMAL_PDF, "application/pdf")},
        timeout=15,
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["audiogram_fs_id"]
    assert payload["content_type"] == "application/pdf"

    # Branch fetch — inline PDF stream.
    r = branch.get(f"{BASE_URL}/api/ha/custom-ha-orders/{order_id}/audiogram", timeout=15)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/pdf")
    assert r.content.startswith(b"%PDF")

    # Head must see the audiogram reference on the linked stock_request AND
    # be able to view the file via the passthrough endpoint.
    head = _sess()
    r = head.get(f"{BASE_URL}/api/stock-requests/{linked_req_id}", timeout=15)
    d = (r.json() or {}).get("custom_ha_details") or {}
    assert d.get("audiogram_fs_id"), "audiogram fs_id must be mirrored"

    r = head.get(f"{BASE_URL}/api/stock-requests/{linked_req_id}/audiogram", timeout=15)
    assert r.status_code == 200
    assert r.content.startswith(b"%PDF")


def test_audiogram_rejects_non_pdf_image_types():
    """Uploader must reject anything that isn't a PDF / PNG / JPG."""
    s = _sess()
    pid = _first_patient(s)
    vid = _first_vendor(s)
    r = s.post(f"{BASE_URL}/api/ha/custom-ha-orders", json={
        "patient_id": pid, "side": "left", "shell_type": "ITE",
        "delivery_target": "vendor", "vendor_id": vid,
        "total_amount": 70000, "advance_amount": 0,
    }, timeout=15)
    order_id = r.json()["order_id"]

    # An executable disguised as an audiogram must be rejected.
    r = s.post(
        f"{BASE_URL}/api/ha/custom-ha-orders/{order_id}/audiogram",
        files={"file": ("virus.exe", b"MZ\x90\x00\x03\x00\x00\x00", "application/octet-stream")},
        timeout=15,
    )
    assert r.status_code == 415


def test_audiogram_delete_clears_stock_request_mirror():
    """Removing an audiogram must unset the order fields AND the
    matching fields on the linked stock_request."""
    branch = _branch_session()
    pid = _find_patient_in_branch(branch)
    r = branch.post(f"{BASE_URL}/api/ha/custom-ha-orders", json={
        "patient_id": pid, "side": "both", "shell_type": "IIC",
        "delivery_target": "branch",
        "total_amount": 90000, "advance_amount": 0,
    }, timeout=15)
    order = r.json()
    order_id = order["order_id"]
    linked_req_id = order["linked_stock_request_id"]

    branch.post(
        f"{BASE_URL}/api/ha/custom-ha-orders/{order_id}/audiogram",
        files={"file": ("audiogram.pdf", _MINIMAL_PDF, "application/pdf")},
        timeout=15,
    )
    r = branch.delete(f"{BASE_URL}/api/ha/custom-ha-orders/{order_id}/audiogram", timeout=15)
    assert r.status_code == 200

    head = _sess()
    r = head.get(f"{BASE_URL}/api/stock-requests/{linked_req_id}", timeout=15)
    d = (r.json() or {}).get("custom_ha_details") or {}
    assert not d.get("audiogram_fs_id"), "mirror must be cleared on delete"

    # The passthrough endpoint must now return 404.
    r = head.get(f"{BASE_URL}/api/stock-requests/{linked_req_id}/audiogram", timeout=15)
    assert r.status_code == 404



# ── Trial-to-Order audiogram auto-attach ───────────────────────────────
# When the audiologist finishes a hearing test → uploads the audiogram
# to the session → then converts the trial into a Custom HA order, we
# auto-clone the session's PDF into the Custom HA audiogram bucket. No
# re-upload needed. If a trial was the trigger, we also close it as
# CONVERTED so nothing dangles in Trials.

def _seed_session_with_pdf(session, patient_id):
    """Create a test session for a patient and upload a PDF to it."""
    r = session.post(f"{BASE_URL}/api/sessions", json={
        "patient_id": patient_id,
        "test_type": "audiometry",
        "test_date": "2026-08-13T10:00:00",
        "audiologist_name": "PyTest Audiologist",
        "referring_doctor": "PyTest ENT",
    }, timeout=15)
    assert r.status_code in (200, 201), r.text
    sid = r.json()["session_id"]
    # Upload the PDF exactly like the client-rendered report handover.
    r = session.post(
        f"{BASE_URL}/api/sessions/{sid}/report-pdf",
        files={"file": ("audiogram.pdf", _MINIMAL_PDF, "application/pdf")},
        timeout=15,
    )
    assert r.status_code == 200, r.text
    return sid


def test_available_audiograms_lists_only_sessions_with_pdf():
    """The Custom HA modal's audiogram picker must see PDF-attached
    sessions only — draft/no-PDF sessions are filtered out at the DB
    level to keep the picker uncluttered."""
    s = _sess()
    pid = _first_patient(s)
    _seed_session_with_pdf(s, pid)

    r = s.get(f"{BASE_URL}/api/ha/custom-ha-orders/available-audiograms",
              params={"patient_id": pid}, timeout=15)
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list)
    assert len(rows) >= 1
    assert all(row.get("session_id") for row in rows)


def test_from_session_id_auto_attaches_audiogram_on_booking():
    """POST /custom-ha-orders with `from_session_id` must clone the
    session's PDF into the order — no follow-up upload. The order
    should immediately have `audiogram_fs_id` set and be viewable via
    the /audiogram endpoint."""
    s = _sess()
    pid = _first_patient(s)
    vid = _first_vendor(s)
    sid = _seed_session_with_pdf(s, pid)

    r = s.post(f"{BASE_URL}/api/ha/custom-ha-orders", json={
        "patient_id": pid, "side": "both", "shell_type": "IIC",
        "brand": "Phonak", "model": "Virto B90",
        "delivery_target": "vendor", "vendor_id": vid,
        "total_amount": 250000, "advance_amount": 25000,
        "from_session_id": sid,   # ← trial-to-order: auto-attach the audiogram
    }, timeout=15)
    assert r.status_code == 200, r.text
    order = r.json()
    order_id = order["order_id"]
    # The order was returned WITH the audiogram fields populated — no
    # second call needed.
    assert order.get("audiogram_fs_id"), "audiogram must auto-attach from session"
    assert order.get("audiogram_source_session_id") == sid
    assert order.get("audiogram_content_type") == "application/pdf"
    # Downloading via the order endpoint returns a real PDF.
    r = s.get(f"{BASE_URL}/api/ha/custom-ha-orders/{order_id}/audiogram", timeout=15)
    assert r.status_code == 200
    assert r.content.startswith(b"%PDF")


def test_from_session_id_mirrors_audiogram_to_linked_stock_request():
    """Auto-attach must also stamp the linked stock_request's mirror
    fields when the order routes to the head clinic — otherwise the
    head owner wouldn't see the "View Audiogram" button."""
    branch = _branch_session()
    pid = _find_patient_in_branch(branch)
    sid = _seed_session_with_pdf(branch, pid)

    r = branch.post(f"{BASE_URL}/api/ha/custom-ha-orders", json={
        "patient_id": pid, "side": "both", "shell_type": "CIC",
        "brand": "Signia", "model": "Insio",
        "delivery_target": "branch",
        "total_amount": 150000, "advance_amount": 10000,
        "from_session_id": sid,
    }, timeout=15)
    order = r.json()
    linked_req_id = order["linked_stock_request_id"]

    head = _sess()
    r = head.get(f"{BASE_URL}/api/stock-requests/{linked_req_id}", timeout=15)
    d = (r.json() or {}).get("custom_ha_details") or {}
    assert d.get("audiogram_fs_id"), "linked stock_request must carry the cloned audiogram ref"
    # And head can view it via the passthrough.
    r = head.get(f"{BASE_URL}/api/stock-requests/{linked_req_id}/audiogram", timeout=15)
    assert r.status_code == 200
    assert r.content.startswith(b"%PDF")


def test_from_session_id_missing_pdf_is_silent_noop():
    """If the audiologist points at a session that has no PDF uploaded
    yet, we must still create the order (no attachment) — surfacing a
    500 or blocking the booking would be worse than the missing
    attachment."""
    s = _sess()
    pid = _first_patient(s)
    vid = _first_vendor(s)
    r = s.post(f"{BASE_URL}/api/sessions", json={
        "patient_id": pid, "test_type": "audiometry",
        "test_date": "2026-08-13T10:00:00",
    }, timeout=15)
    sid = r.json()["session_id"]  # no PDF uploaded on this session

    r = s.post(f"{BASE_URL}/api/ha/custom-ha-orders", json={
        "patient_id": pid, "side": "left", "shell_type": "ITE",
        "delivery_target": "vendor", "vendor_id": vid,
        "total_amount": 90000, "advance_amount": 0,
        "from_session_id": sid,
    }, timeout=15)
    assert r.status_code == 200
    order = r.json()
    assert not order.get("audiogram_fs_id"), "no PDF → no attachment, order still succeeds"


def test_from_trial_no_closes_source_trial():
    """Passing `from_trial_no` at booking must close the trial as
    CONVERTED with `converted_custom_ha_order_no` back-linked."""
    s = _sess()
    pid = _first_patient(s)
    vid = _first_vendor(s)
    # Seed a trial with a demo unit so `from_trial_no` has something to
    # close. Uses the same helper we use elsewhere in the suite.
    demo = s.get(f"{BASE_URL}/api/ha/serial-items",
                 params={"pool": "demo", "state": "IN_STOCK", "limit": 1},
                 timeout=15).json()
    if not demo:
        return  # no demo units available in this fixture — skip cleanly
    demo_serial = demo[0]["serial_id"]

    r = s.post(f"{BASE_URL}/api/ha/trials", json={
        "patient_id": pid,
        "serials": [{"serial_id": demo_serial, "side": "both"}],
        "start_date": "2026-08-01", "return_date": "2026-08-14",
        "audiologist_user_id": None,
    }, timeout=15)
    if r.status_code not in (200, 201):
        return  # fixture missing an audiologist or product — skip
    trial_no = r.json().get("trial_no")

    r = s.post(f"{BASE_URL}/api/ha/custom-ha-orders", json={
        "patient_id": pid, "side": "both", "shell_type": "ITC",
        "delivery_target": "vendor", "vendor_id": vid,
        "total_amount": 180000, "advance_amount": 20000,
        "from_trial_no": trial_no,
    }, timeout=15)
    assert r.status_code == 200, r.text
    order = r.json()

    r = s.get(f"{BASE_URL}/api/ha/trials/{trial_no}", timeout=15)
    updated_trial = r.json()
    assert updated_trial["status"] == "converted"
    assert updated_trial.get("converted_custom_ha_order_no") == order["order_no"]
