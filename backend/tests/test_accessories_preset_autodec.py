"""Regression + feature tests for the 3 next-action items:

1. Legacy 500 fix on GET /ha/serial-items, /ha/amc/contracts, /ha/fittings
2. New silicone_dome accessory preset (POST /ha/products/preset-seed)
3. Auto-decrement accessory_stock on invoice paid transition

Runs against the seeded `tenant-sound-clinic-blr` clinic (owner login).
"""
from __future__ import annotations

import os
import time
import uuid

import pytest
import requests

from _helpers import H  # shared header helper

# ----- resolve API URL exactly like _helpers does, without touching pytest tenant -----
def _api() -> str:
    raw = os.environ.get("REACT_APP_BACKEND_URL")
    if not raw:
        with open("/app/frontend/.env", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("REACT_APP_BACKEND_URL="):
                    raw = line.split("=", 1)[1].strip()
                    break
    raw = raw.rstrip("/")
    return raw if raw.endswith("/api") else f"{raw}/api"


API = _api()

OWNER_EMAIL = "owner@thesoundclinic.in"
OWNER_PASSWORD = "demo123"
AUDIO_EMAIL = "aditi@thesoundclinic.in"
FRONTDESK_EMAIL = "meera@thesoundclinic.in"
CLINIC_ID = "tenant-sound-clinic-blr"
BRANCH_ID = "BR-SOUNDCLINIC-HQ"


def _login(email: str, password: str = "demo123") -> str:
    r = requests.post(f"{API}/auth/login",
                      json={"email": email, "password": password}, timeout=20)
    assert r.status_code == 200, f"login {email} failed: {r.status_code} {r.text[:200]}"
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def owner_token():
    return _login(OWNER_EMAIL, OWNER_PASSWORD)


@pytest.fixture(scope="module")
def audio_token():
    return _login(AUDIO_EMAIL)


@pytest.fixture(scope="module")
def frontdesk_token():
    return _login(FRONTDESK_EMAIL)


@pytest.fixture(scope="module")
def patient_id(owner_token) -> str:
    r = requests.get(f"{API}/patients?limit=1", headers=H(owner_token), timeout=20)
    assert r.status_code == 200
    d = r.json()
    items = d.get("items", d) if isinstance(d, dict) else d
    assert items, "no patients on tenant-sound-clinic-blr"
    return items[0]["patient_id"]


# ============================================================
# Section 1 — 500 regression on legacy list endpoints
# ============================================================
class TestLegacy500Regression:
    def test_serial_items_unfiltered_200(self, owner_token):
        r = requests.get(f"{API}/ha/serial-items", headers=H(owner_token), timeout=30)
        assert r.status_code == 200, r.text[:300]
        assert isinstance(r.json(), list)

    def test_serial_items_sold_200(self, owner_token):
        r = requests.get(f"{API}/ha/serial-items?state=SOLD",
                         headers=H(owner_token), timeout=30)
        assert r.status_code == 200, r.text[:300]
        assert isinstance(r.json(), list)

    def test_amc_contracts_200(self, owner_token):
        r = requests.get(f"{API}/ha/amc/contracts", headers=H(owner_token), timeout=30)
        assert r.status_code == 200, r.text[:300]
        assert isinstance(r.json(), list)

    def test_fittings_200(self, owner_token):
        r = requests.get(f"{API}/ha/fittings", headers=H(owner_token), timeout=30)
        assert r.status_code == 200, r.text[:300]
        assert isinstance(r.json(), list)


# ============================================================
# Section 2 — Accessory presets endpoint
# ============================================================
class TestAccessoryPresets:
    def test_presets_shape(self, owner_token):
        r = requests.get(f"{API}/ha/accessory-presets",
                         headers=H(owner_token), timeout=20)
        assert r.status_code == 200
        data = r.json()
        keys = {p["key"]: p for p in data["presets"]}
        assert "ric_receiver" in keys and "silicone_dome" in keys
        assert keys["silicone_dome"]["variants"] == ["S", "M", "L", "Power"]
        assert keys["ric_receiver"]["variants"] == \
            ["1M", "2M", "3M", "10P", "2P", "3P", "1S", "2S", "3S"]

    def test_presets_requires_auth(self):
        r = requests.get(f"{API}/ha/accessory-presets", timeout=10)
        assert r.status_code in (401, 403)


# ============================================================
# Section 3 — preset-seed (silicone_dome) + idempotency + role gate
# ============================================================
@pytest.fixture(scope="module")
def unique_brand() -> str:
    return f"TEST_BRAND_{uuid.uuid4().hex[:6].upper()}"


class TestPresetSeed:
    def test_seed_silicone_dome_creates_4_rows(self, owner_token, unique_brand):
        r = requests.post(
            f"{API}/ha/products/preset-seed",
            headers=H(owner_token),
            json={"preset_key": "silicone_dome", "brand": unique_brand,
                  "branch_ids": [BRANCH_ID]},
            timeout=30,
        )
        assert r.status_code == 200, r.text[:400]
        data = r.json()
        assert data["reused_existing_product"] is False
        assert data["stock_rows_created"] == 4
        assert data["stock_rows_skipped_existing"] == 0
        p = data["product"]
        assert p["form_factor"] == "accessory"
        assert p["is_serialised"] is False
        assert p["accessory_kind"] == "tip"
        assert p["accessory_category"] == "consumable"
        assert p["variant_labels"] == ["S", "M", "L", "Power"]

    def test_seed_silicone_dome_idempotent(self, owner_token, unique_brand):
        # count products before
        r_list1 = requests.get(
            f"{API}/ha/products?form_factor=accessory",
            headers=H(owner_token), timeout=20,
        )
        assert r_list1.status_code == 200
        matches_before = [p for p in r_list1.json()
                          if p.get("brand") == unique_brand and p.get("model") == "Silicone Dome"]
        assert len(matches_before) == 1, "precondition: first seed should exist"

        r = requests.post(
            f"{API}/ha/products/preset-seed",
            headers=H(owner_token),
            json={"preset_key": "silicone_dome", "brand": unique_brand,
                  "branch_ids": [BRANCH_ID]},
            timeout=30,
        )
        assert r.status_code == 200, r.text[:300]
        data = r.json()
        assert data["reused_existing_product"] is True
        assert data["stock_rows_created"] == 0
        assert data["stock_rows_skipped_existing"] == 4

        # No duplicate product row appeared
        r_list2 = requests.get(
            f"{API}/ha/products?form_factor=accessory",
            headers=H(owner_token), timeout=20,
        )
        matches_after = [p for p in r_list2.json()
                         if p.get("brand") == unique_brand and p.get("model") == "Silicone Dome"]
        assert len(matches_after) == 1

    def test_preset_ric_receiver_backcompat(self, owner_token):
        brand = f"TEST_RIC_{uuid.uuid4().hex[:6].upper()}"
        r = requests.post(
            f"{API}/ha/products/preset-ric-receiver",
            headers=H(owner_token),
            json={"brand": brand, "branch_ids": [BRANCH_ID]},
            timeout=30,
        )
        assert r.status_code == 200, r.text[:400]
        data = r.json()
        assert data["reused_existing_product"] is False
        assert data["stock_rows_created"] == 9  # 9 RIC variants
        # 2nd call idempotent
        r2 = requests.post(
            f"{API}/ha/products/preset-ric-receiver",
            headers=H(owner_token),
            json={"brand": brand, "branch_ids": [BRANCH_ID]},
            timeout=30,
        )
        assert r2.status_code == 200
        assert r2.json()["reused_existing_product"] is True
        assert r2.json()["stock_rows_created"] == 0

    def test_preset_seed_role_gate_audiologist(self, audio_token):
        r = requests.post(
            f"{API}/ha/products/preset-seed",
            headers=H(audio_token),
            json={"preset_key": "silicone_dome", "brand": "ShouldFail",
                  "branch_ids": [BRANCH_ID]},
            timeout=15,
        )
        assert r.status_code == 403, f"expected 403 got {r.status_code}: {r.text[:200]}"

    def test_preset_seed_role_gate_frontdesk(self, frontdesk_token):
        r = requests.post(
            f"{API}/ha/products/preset-seed",
            headers=H(frontdesk_token),
            json={"preset_key": "silicone_dome", "brand": "ShouldFail",
                  "branch_ids": [BRANCH_ID]},
            timeout=15,
        )
        assert r.status_code == 403

    def test_preset_seed_unknown_preset(self, owner_token):
        r = requests.post(
            f"{API}/ha/products/preset-seed",
            headers=H(owner_token),
            json={"preset_key": "bogus_xyz", "brand": "X",
                  "branch_ids": [BRANCH_ID]},
            timeout=15,
        )
        assert r.status_code == 400


# ============================================================
# Section 4 — Auto-decrement flow
# ============================================================
def _get_stock_row(token: str, product_id: str, variant: str) -> dict | None:
    r = requests.get(f"{API}/ha/accessory-stock", headers=H(token), timeout=20)
    assert r.status_code == 200, r.text[:200]
    for row in r.json():
        if row["product_id"] == product_id and row.get("variant") == variant:
            return row
    return None


def _adjust_stock(token: str, sku_id: str, delta: int):
    r = requests.post(
        f"{API}/ha/accessory-stock/{sku_id}/adjust",
        headers=H(token),
        json={"delta": delta, "reason": "stock_in"},
        timeout=15,
    )
    assert r.status_code == 200, r.text[:200]


@pytest.fixture(scope="module")
def dome_product(owner_token) -> dict:
    """Create a fresh silicone_dome product for auto-decrement tests."""
    brand = f"TEST_AUTODEC_{uuid.uuid4().hex[:6].upper()}"
    r = requests.post(
        f"{API}/ha/products/preset-seed",
        headers=H(owner_token),
        json={"preset_key": "silicone_dome", "brand": brand,
              "branch_ids": [BRANCH_ID]},
        timeout=20,
    )
    assert r.status_code == 200, r.text[:200]
    return {"brand": brand, "product": r.json()["product"]}


class TestAutoDecrement:
    def test_happy_path_paid_invoice_decrements(self, owner_token, patient_id, dome_product):
        product = dome_product["product"]
        # Set M variant qty to 30
        row = _get_stock_row(owner_token, product["product_id"], "M")
        assert row is not None
        # Delta = 30 - current
        _adjust_stock(owner_token, row["sku_id"], 30 - int(row["qty_on_hand"]))
        # Create paid invoice with 10 units on M
        payload = {
            "patient_id": patient_id,
            "lines": [{
                "description": f"{dome_product['brand']} Silicone Dome (M)",
                "quantity": 10, "unit_price": 100.0,
                "is_taxable": False, "gst_rate": 0.0,
                "product_type": "Accessory",
                "make": dome_product["brand"], "model": "Silicone Dome",
                "accessory_product_id": product["product_id"],
                "accessory_variant": "M",
            }],
            "initial_payment": {"method": "cash", "amount": 1000.0},
        }
        r = requests.post(f"{API}/billing/invoices", headers=H(owner_token),
                          json=payload, timeout=30)
        assert r.status_code in (200, 201), r.text[:400]
        inv = r.json()
        assert inv["status"] == "paid"
        invoice_id = inv["invoice_id"]

        # Verify stock decremented 30 -> 20
        time.sleep(0.5)
        row_after = _get_stock_row(owner_token, product["product_id"], "M")
        assert row_after["qty_on_hand"] == 20, \
            f"Expected 20, got {row_after['qty_on_hand']}"

        # Verify invoice line flag flipped
        r_inv = requests.get(f"{API}/billing/invoices/{invoice_id}",
                             headers=H(owner_token), timeout=15)
        assert r_inv.status_code == 200
        acc_line = next(l for l in r_inv.json()["lines"]
                        if l.get("product_type") == "Accessory")
        assert acc_line.get("accessory_stock_decremented") is True

        # store for idempotency test
        self.__class__._paid_invoice_id = invoice_id
        self.__class__._product_id = product["product_id"]

    def test_idempotency_no_double_decrement(self, owner_token):
        """Adding a zero-amount payment should not re-decrement."""
        invoice_id = getattr(self.__class__, "_paid_invoice_id", None)
        product_id = getattr(self.__class__, "_product_id", None)
        if not invoice_id:
            pytest.skip("happy path did not run")
        r = requests.post(
            f"{API}/billing/invoices/{invoice_id}/payments",
            headers=H(owner_token),
            json={"method": "cash", "amount": 0.0},
            timeout=15,
        )
        # Backend may accept (no-op) or reject 400 — either way stock unchanged
        row = _get_stock_row(owner_token, product_id, "M")
        assert row["qty_on_hand"] == 20, \
            f"stock re-decremented! now {row['qty_on_hand']}, expected 20 (r={r.status_code})"

    def test_partial_to_paid_transition(self, owner_token, patient_id, dome_product):
        # NAV-010 · INV-003 · accessory stock is now reserved (decremented)
        # atomically at invoice-creation time rather than on the paid
        # transition. This test now verifies the immediate-decrement
        # semantics and the idempotency guard against re-decrement.
        product = dome_product["product"]
        # set L variant to 15
        row = _get_stock_row(owner_token, product["product_id"], "L")
        _adjust_stock(owner_token, row["sku_id"], 15 - int(row["qty_on_hand"]))

        # Draft invoice (no initial_payment)
        payload = {
            "patient_id": patient_id,
            "lines": [{
                "description": "Dome L", "quantity": 5, "unit_price": 100.0,
                "is_taxable": False, "gst_rate": 0.0,
                "product_type": "Accessory",
                "make": dome_product["brand"], "model": "Silicone Dome",
                "accessory_product_id": product["product_id"],
                "accessory_variant": "L",
            }],
        }
        r = requests.post(f"{API}/billing/invoices", headers=H(owner_token),
                          json=payload, timeout=30)
        assert r.status_code in (200, 201), r.text[:400]
        inv = r.json()
        invoice_id = inv["invoice_id"]
        assert inv["status"] in ("draft", "unpaid", "partial"), inv["status"]

        # NAV-010 · INV-003: stock has already been reserved on create.
        row2 = _get_stock_row(owner_token, product["product_id"], "L")
        assert row2["qty_on_hand"] == 10, \
            f"expected 10 (immediate reservation on create), got {row2['qty_on_hand']}"

        # Partial payment — no additional decrement (INV-003 idempotency).
        r_p1 = requests.post(f"{API}/billing/invoices/{invoice_id}/payments",
                             headers=H(owner_token),
                             json={"method": "cash", "amount": 100.0},
                             timeout=15)
        assert r_p1.status_code in (200, 201), r_p1.text[:200]
        assert r_p1.json()["status"] == "partial"
        row3 = _get_stock_row(owner_token, product["product_id"], "L")
        assert row3["qty_on_hand"] == 10, "stock re-decremented on partial!"

        # Full settlement — still no additional decrement.
        r_p2 = requests.post(f"{API}/billing/invoices/{invoice_id}/payments",
                             headers=H(owner_token),
                             json={"method": "cash", "amount": 400.0},
                             timeout=15)
        assert r_p2.status_code in (200, 201), r_p2.text[:200]
        assert r_p2.json()["status"] == "paid"
        time.sleep(0.5)
        row4 = _get_stock_row(owner_token, product["product_id"], "L")
        assert row4["qty_on_hand"] == 10, \
            f"expected 10 (no re-decrement on paid), got {row4['qty_on_hand']}"

    def test_non_accessory_invoice_no_side_effects(self, owner_token, patient_id, dome_product):
        product = dome_product["product"]
        row_before = _get_stock_row(owner_token, product["product_id"], "S")
        qty_before = int(row_before["qty_on_hand"])
        payload = {
            "patient_id": patient_id,
            "lines": [{
                "description": "Hearing Aid Sale", "quantity": 1,
                "unit_price": 50000.0, "is_taxable": False, "gst_rate": 0.0,
                "product_type": "Hearing Aid",
                "make": "Phonak", "model": "Audeo",
            }],
            "initial_payment": {"method": "cash", "amount": 50000.0},
        }
        r = requests.post(f"{API}/billing/invoices", headers=H(owner_token),
                          json=payload, timeout=30)
        assert r.status_code in (200, 201), r.text[:400]
        assert r.json()["status"] == "paid"
        row_after = _get_stock_row(owner_token, product["product_id"], "S")
        assert int(row_after["qty_on_hand"]) == qty_before

    def test_shortfall_returns_409_no_side_effects(self, owner_token, patient_id, dome_product):
        # NAV-010 · INV-007 · shortage now returns HTTP 409 at invoice
        # creation time with zero side-effects (no invoice, no payment,
        # no stock mutation). Previously the code silently floored to
        # zero — that behaviour has been retired.
        product = dome_product["product"]
        row = _get_stock_row(owner_token, product["product_id"], "Power")
        # Set Power to 2
        _adjust_stock(owner_token, row["sku_id"], 2 - int(row["qty_on_hand"]))
        qty_before = _get_stock_row(owner_token, product["product_id"], "Power")["qty_on_hand"]
        assert qty_before == 2
        payload = {
            "patient_id": patient_id,
            "lines": [{
                "description": "Dome Power", "quantity": 10, "unit_price": 50.0,
                "is_taxable": False, "gst_rate": 0.0,
                "product_type": "Accessory",
                "make": dome_product["brand"], "model": "Silicone Dome",
                "accessory_product_id": product["product_id"],
                "accessory_variant": "Power",
            }],
            "initial_payment": {"method": "cash", "amount": 500.0},
        }
        r = requests.post(f"{API}/billing/invoices", headers=H(owner_token),
                          json=payload, timeout=30)
        assert r.status_code == 409, r.text[:400]
        assert "insufficient" in r.text.lower() or "stock" in r.text.lower()
        # No mutation.
        row_after = _get_stock_row(owner_token, product["product_id"], "Power")
        assert row_after["qty_on_hand"] == 2, \
            f"expected qty unchanged at 2, got {row_after['qty_on_hand']}"

    def test_ambiguous_brand_model_skips_gracefully(self, owner_token, patient_id):
        """Two accessory products with same brand+model → auto-decrement skips."""
        brand = f"TEST_AMB_{uuid.uuid4().hex[:6].upper()}"
        # Create two silicone_dome products with same brand
        for _ in range(2):
            # different preset attempt would collide idempotently, so directly
            # POST two products via ha_inventory create-product endpoint.
            pass
        # Since preset seeder is idempotent by (brand, model, kind), we need
        # two different presets with same brand/model. Instead, use the direct
        # product-create endpoint if available.
        # Fallback: create via preset seed with model= override so we get two
        # rows with (brand, "Silicone Dome"), and one with (brand, "SilDome2").
        # The auto-decrement matches on brand + model, so we need identical
        # (brand,model). Try creating via /ha/products POST.
        p1 = requests.post(
            f"{API}/ha/products",
            headers=H(owner_token),
            json={
                "brand": brand, "model": "Silicone Dome",
                "form_factor": "accessory", "is_serialised": False,
                "accessory_kind": "tip", "accessory_category": "consumable",
                "variant_labels": ["S", "M", "L", "Power"],
                "hsn": "9021", "gst_rate": 18.0, "mrp": 100.0,
            },
            timeout=15,
        )
        p2 = requests.post(
            f"{API}/ha/products",
            headers=H(owner_token),
            json={
                "brand": brand, "model": "Silicone Dome",
                "form_factor": "accessory", "is_serialised": False,
                "accessory_kind": "tip", "accessory_category": "consumable",
                "variant_labels": ["S", "M", "L", "Power"],
                "hsn": "9021", "gst_rate": 18.0, "mrp": 100.0,
            },
            timeout=15,
        )
        if p1.status_code not in (200, 201) or p2.status_code not in (200, 201):
            pytest.skip(f"could not create two ambiguous products (p1={p1.status_code}, p2={p2.status_code})")

        # Now create a paid invoice with brand+model, no accessory_product_id
        payload = {
            "patient_id": patient_id,
            "lines": [{
                "description": "Ambiguous dome", "quantity": 3,
                "unit_price": 100.0, "is_taxable": False, "gst_rate": 0.0,
                "product_type": "Accessory",
                "make": brand, "model": "Silicone Dome",
                # NO accessory_product_id + NO variant
            }],
            "initial_payment": {"method": "cash", "amount": 300.0},
        }
        r = requests.post(f"{API}/billing/invoices", headers=H(owner_token),
                          json=payload, timeout=30)
        # Must NOT block — invoice still succeeds
        assert r.status_code in (200, 201), r.text[:300]
        assert r.json()["status"] == "paid"
        # Nothing to assert on stock (no unique match) — the important thing
        # is that the invoice was accepted despite ambiguity.
