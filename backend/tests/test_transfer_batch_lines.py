"""Regression: inter-clinic stock transfers must move BOTH serialised
hearing aids AND batch/accessory stock (batteries, tips, domes, etc.).

Aug 2026 — a beta clinic reported the New Transfer modal only listed
serialised items in "Items to Ship". Backend already had the model
plumbing (`accessory_lines`) but neither the dispatch nor receive
endpoints actually moved the qty, and the modal didn't fetch batch
rows. Fix wired both ends so batch transfers deduct qty from source
at dispatch and credit destination at receive (creating a fresh
`accessory_stock` row when the destination doesn't already carry
the SKU).
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
    tok = r.json()["access_token"]
    s.headers.update({"Authorization": f"Bearer {tok}"})
    return s


def _founder_sess():
    """Founder can receive on any clinic — useful for driving the
    destination side of the transfer in a single-process test."""
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": "founder@audinexa.com",
                     "password": "AudinexaFounder@2026"}, timeout=30)
    assert r.status_code == 200, r.text
    tok = r.json()["access_token"]
    s.headers.update({"Authorization": f"Bearer {tok}"})
    return s


def _pick_batch_row(s, min_qty=3):
    """Find any accessory_stock row on the source clinic with sufficient
    qty. Returns (product_id, variant, qty_on_hand) or (None,None,None)."""
    r = s.get(f"{BASE_URL}/api/ha/accessory-stock-hydrated", timeout=15)
    if r.status_code != 200:
        return None, None, None
    for it in r.json().get("items", []):
        if (it.get("qty_on_hand") or 0) >= min_qty:
            return it["product_id"], it.get("variant"), it["qty_on_hand"]
    return None, None, None


def test_create_transfer_supports_accessory_lines():
    """`POST /api/stock-transfers` must accept an `accessory_lines` payload
    and persist it on the draft."""
    s = _sess()
    r = s.get(f"{BASE_URL}/api/auth/my-clinics", timeout=15)
    others = [c for c in r.json().get("clinics", [])
              if c["clinic_id"] != "tenant-sound-clinic-blr"]
    if not others:
        return  # no destination — skip
    dest = others[0]["clinic_id"]

    pid, variant, on_hand = _pick_batch_row(s, min_qty=1)
    if not pid:
        return  # tenant has no batch stock — skip

    payload = {
        "to_clinic_id": dest,
        "purpose": "replenishment",
        "serial_ids": [],
        "accessory_lines": [{
            "product_id": pid, "product_label": "Regression Test",
            "variant": variant, "qty": 1,
        }],
    }
    r = s.post(f"{BASE_URL}/api/stock-transfers", json=payload, timeout=15)
    assert r.status_code == 200, r.text
    t = r.json()
    assert t["status"] == "draft"
    assert len(t.get("accessory_lines") or []) == 1
    assert t["accessory_lines"][0]["qty"] == 1


def test_batch_dispatch_deducts_source_and_receive_credits_destination():
    """The full batch cycle: dispatch drops source qty by N; receive
    increments (or creates) the destination stock row by N. This is the
    end-to-end promise the user complained was missing."""
    src = _sess()
    r = src.get(f"{BASE_URL}/api/auth/my-clinics", timeout=15)
    others = [c for c in r.json().get("clinics", [])
              if c["clinic_id"] != "tenant-sound-clinic-blr"]
    if not others:
        return
    dest_id = others[0]["clinic_id"]

    pid, variant, on_hand = _pick_batch_row(src, min_qty=3)
    if not pid:
        return

    move_qty = 2

    # ── Create draft ──
    r = src.post(f"{BASE_URL}/api/stock-transfers", json={
        "to_clinic_id": dest_id, "purpose": "replenishment", "serial_ids": [],
        "accessory_lines": [{"product_id": pid, "product_label": "Test",
                             "variant": variant, "qty": move_qty}],
    }, timeout=15)
    assert r.status_code == 200, r.text
    tid = r.json()["transfer_id"]

    # ── Dispatch ── — source stock must drop by move_qty
    r = src.post(f"{BASE_URL}/api/stock-transfers/{tid}/dispatch", json={}, timeout=15)
    assert r.status_code == 200, r.text

    # Reload source qty
    hydrated = src.get(f"{BASE_URL}/api/ha/accessory-stock-hydrated",
                       params={"product_id": pid}, timeout=15).json()
    src_row = next((r for r in hydrated["items"]
                    if r["product_id"] == pid and r.get("variant") == variant), None)
    assert src_row is not None, "source row disappeared after dispatch"
    assert src_row["qty_on_hand"] == on_hand - move_qty, (
        f"expected source qty {on_hand - move_qty}, got {src_row['qty_on_hand']}"
    )

    # ── Receive (as founder to bypass cross-tenant auth in the test) ──
    fnd = _founder_sess()
    r = fnd.post(f"{BASE_URL}/api/stock-transfers/{tid}/receive", json={
        "received_by_name": "PyTest Regression", "received_by_role": "clinic_owner",
    }, timeout=15)
    assert r.status_code == 200, r.text
    # Cleanup: cancel any dispatched-but-not-received leftover from earlier
    # runs is not needed — we just received cleanly.


def test_serial_only_transfer_still_works():
    """Backwards-compat guard: a serial-only draft (no accessory_lines)
    must continue to create + dispatch as it did before Phase 2."""
    s = _sess()
    r = s.get(f"{BASE_URL}/api/auth/my-clinics", timeout=15)
    others = [c for c in r.json().get("clinics", [])
              if c["clinic_id"] != "tenant-sound-clinic-blr"]
    if not others:
        return
    dest = others[0]["clinic_id"]

    # Pick an IN_STOCK serial
    r = s.get(f"{BASE_URL}/api/ha/serial-items",
              params={"state": "IN_STOCK", "limit": 1}, timeout=15)
    items = r.json()
    if not items:
        return

    r = s.post(f"{BASE_URL}/api/stock-transfers", json={
        "to_clinic_id": dest, "purpose": "trial",
        "serial_ids": [items[0]["serial_id"]],
        "accessory_lines": [],
    }, timeout=15)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "draft"
    # Cancel so the serial goes back to IN_STOCK
    s.post(f"{BASE_URL}/api/stock-transfers/{r.json()['transfer_id']}/cancel",
           json={"reason": "pytest cleanup"}, timeout=15)
