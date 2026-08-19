"""Regression: Accessory catalogue CRUD + tracking-type conversion.

Feb 2026 — the user asked "If I created an accessory with serial number,
how do I switch it to batch stock?". These tests lock in the safety
rails for the new Edit / Delete / Convert endpoints so a future refactor
can't silently orphan inventory rows.
"""
import os
import uuid
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://referral-sprint.preview.emergentagent.com").rstrip("/")
EMAIL = "owner@thesoundclinic.in"
PASSWORD = "demo123"


def _sess():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": EMAIL, "password": PASSWORD}, timeout=30)
    assert r.status_code == 200, r.text
    tok = r.json().get("access_token")
    s.headers.update({"Authorization": f"Bearer {tok}"})
    return s


def _first_branch(s):
    r = s.get(f"{BASE_URL}/api/branches", timeout=15)
    r.raise_for_status()
    return r.json()[0]["branch_id"]


def _create_accessory(s, is_serialised, brand=None):
    payload = {
        "brand": brand or f"AutoTest-{uuid.uuid4().hex[:6]}",
        "model": "PyTest Model",
        "form_factor": "accessory",
        "is_serialised": is_serialised,
        "mrp": 100, "gst_rate": 18,
        "accessory_kind": "charger" if is_serialised else "battery",
        "accessory_category": "addon" if is_serialised else "consumable",
    }
    r = s.post(f"{BASE_URL}/api/ha/products", json=payload, timeout=15)
    assert r.status_code == 200, r.text
    return r.json()["product_id"]


def test_convert_serialised_to_batch_then_back():
    """Happy path: serialised → batch (creates stock rows) → back to serialised (drops them)."""
    s = _sess()
    branch = _first_branch(s)
    pid = _create_accessory(s, is_serialised=True)

    # Convert to batch with 3 variants
    r = s.patch(
        f"{BASE_URL}/api/ha/products/{pid}/convert-tracking",
        json={"to": "batch", "branch_ids": [branch],
              "variants": ["S", "M", "L"], "reorder_level": 5},
        timeout=15,
    )
    assert r.status_code == 200, r.text
    assert r.json()["stock_rows_created"] == 3

    # Verify the flip on the product
    r2 = s.get(f"{BASE_URL}/api/ha/products/{pid}", timeout=15)
    assert r2.json()["is_serialised"] is False
    assert set(r2.json()["variant_labels"]) == {"S", "M", "L"}

    # Convert back to serialised (all qty=0 so it must succeed)
    r3 = s.patch(f"{BASE_URL}/api/ha/products/{pid}/convert-tracking",
                 json={"to": "serialised"}, timeout=15)
    assert r3.status_code == 200, r3.text
    assert r3.json()["stock_rows_removed"] == 3

    # Cleanup
    s.delete(f"{BASE_URL}/api/ha/products/{pid}", timeout=15)


def test_convert_batch_to_serialised_blocked_when_qty_present():
    """Safety rail: batch → serialised must be blocked while any variant has qty > 0."""
    s = _sess()
    branch = _first_branch(s)
    pid = _create_accessory(s, is_serialised=False)

    # Init a single-variant stock row and bump qty via /adjust
    r = s.post(
        f"{BASE_URL}/api/ha/products/{pid}/init-accessory-stock",
        json={"branch_ids": [branch], "variants": ["default"], "reorder_level": 5},
        timeout=15,
    )
    assert r.status_code == 200

    # Locate the sku_id via the hydrated grid
    grid = s.get(f"{BASE_URL}/api/ha/accessory-stock-hydrated",
                 params={"branch_id": branch}, timeout=15).json()
    sku_row = next(r for r in grid["items"] if r["product_id"] == pid)
    sku_id = sku_row["sku_id"]

    # Bump qty to 5
    s.post(f"{BASE_URL}/api/ha/accessory-stock/{sku_id}/adjust",
           json={"delta": 5, "reason": "pytest"}, timeout=15)

    # Convert attempt should 409
    r2 = s.patch(f"{BASE_URL}/api/ha/products/{pid}/convert-tracking",
                 json={"to": "serialised"}, timeout=15)
    assert r2.status_code == 409, r2.text
    assert "on hand" in r2.json()["detail"].lower()

    # Delete attempt should also 409 while qty>0
    r3 = s.delete(f"{BASE_URL}/api/ha/products/{pid}", timeout=15)
    assert r3.status_code == 409, r3.text

    # Zero it out, then delete works
    s.post(f"{BASE_URL}/api/ha/accessory-stock/{sku_id}/adjust",
           json={"delta": -5, "reason": "pytest cleanup"}, timeout=15)
    r4 = s.delete(f"{BASE_URL}/api/ha/products/{pid}", timeout=15)
    assert r4.status_code == 200


def test_edit_accessory_preserves_tracking_type():
    """PUT /products/{id} must not silently flip is_serialised — that path
    is reserved for the dedicated convert-tracking endpoint. Guards against
    a future refactor where the Edit modal's payload accidentally alters
    the flag."""
    s = _sess()
    pid = _create_accessory(s, is_serialised=True)

    payload = {
        "brand": "Edited-Brand",
        "model": "Edited-Model",
        "form_factor": "accessory",
        "is_serialised": True,  # kept true
        "mrp": 999, "gst_rate": 12,
        "accessory_kind": "charger",
        "accessory_category": "addon",
        "variant_labels": [],
    }
    r = s.put(f"{BASE_URL}/api/ha/products/{pid}", json=payload, timeout=15)
    assert r.status_code == 200
    body = r.json()
    assert body["brand"] == "Edited-Brand"
    assert body["mrp"] == 999.0
    assert body["gst_rate"] == 12.0
    assert body["is_serialised"] is True

    # Cleanup
    s.delete(f"{BASE_URL}/api/ha/products/{pid}", timeout=15)
