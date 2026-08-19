"""Backend tests for the new Accounts revenue endpoint:
GET /api/accounts/accessory-sales

Covers:
- Happy path (monthly range) — response shape + arithmetic sanity
- Every range key (daily / weekly / monthly / quarterly / half_yearly / yearly)
- Custom range with from+to
- Custom range without from/to → 400
- Auth required (no token → 401/403)
- Tenant scoping (founder@audinexa.com in a different tenant does NOT see
  the sound-clinic accessory revenue rollup)
- Empty window returns the safe empty response (no 500)
- InvoiceLineCreate accepts accessory_product_id + accessory_variant and the
  fields are persisted (draft path — before payment, accessory_stock_decremented=False)
- Regression: non-accessory invoice line (Hearing Aid) still saves without picker fields
"""
from __future__ import annotations

import os
import uuid
from datetime import date, timedelta

import pytest
import requests

from _helpers import H


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
FOUNDER_EMAIL = "founder@audinexa.com"
FOUNDER_PASSWORD = "AudinexaFounder@2026"
CLINIC_ID = "tenant-sound-clinic-blr"
BRANCH_ID = "BR-SOUNDCLINIC-HQ"


def _login(email: str, password: str) -> str:
    r = requests.post(f"{API}/auth/login",
                      json={"email": email, "password": password}, timeout=20)
    assert r.status_code == 200, f"login {email} failed: {r.status_code} {r.text[:200]}"
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def owner_token() -> str:
    return _login(OWNER_EMAIL, OWNER_PASSWORD)


@pytest.fixture(scope="module")
def founder_token() -> str:
    return _login(FOUNDER_EMAIL, FOUNDER_PASSWORD)


@pytest.fixture(scope="module")
def patient_id(owner_token) -> str:
    r = requests.get(f"{API}/patients?limit=1", headers=H(owner_token), timeout=20)
    assert r.status_code == 200
    d = r.json()
    items = d.get("items", d) if isinstance(d, dict) else d
    assert items, "no patients on tenant-sound-clinic-blr"
    return items[0]["patient_id"]


# ================= Section A — endpoint shape + arithmetic =================
class TestAccessorySalesShape:
    def test_monthly_shape(self, owner_token):
        r = requests.get(f"{API}/accounts/accessory-sales?range=monthly",
                         headers=H(owner_token), timeout=30)
        assert r.status_code == 200, r.text[:400]
        j = r.json()
        # Required keys
        for k in ("range", "from", "to", "unit_count", "revenue",
                  "invoice_count", "top_skus"):
            assert k in j, f"missing key {k}"
        assert j["range"] == "monthly"
        assert isinstance(j["unit_count"], int)
        assert isinstance(j["revenue"], (int, float))
        assert isinstance(j["invoice_count"], int)
        assert isinstance(j["top_skus"], list)
        # Non-negative
        assert j["unit_count"] >= 0
        assert j["revenue"] >= 0
        assert j["invoice_count"] >= 0
        assert len(j["top_skus"]) <= 5
        # top_skus shape
        for row in j["top_skus"]:
            for f in ("brand", "model", "kind", "variant", "unit_count", "revenue"):
                assert f in row, f"top_sku missing {f}: {row}"
            assert isinstance(row["unit_count"], int)
            assert isinstance(row["revenue"], (int, float))

    def test_previous_smoke_numbers_present(self, owner_token):
        """From main agent notes: ~9 paid invoices with ~59 units / ~4580 revenue.
        Just sanity — at least SOME data should be non-zero for monthly."""
        r = requests.get(f"{API}/accounts/accessory-sales?range=monthly",
                         headers=H(owner_token), timeout=30)
        j = r.json()
        # We don't assert exact numbers (test data may have grown), just non-zero
        assert j["unit_count"] > 0, f"expected non-zero unit_count, got {j}"
        assert j["revenue"] > 0
        assert j["invoice_count"] > 0
        assert len(j["top_skus"]) > 0

    def test_top_skus_sorted_by_revenue_desc(self, owner_token):
        r = requests.get(f"{API}/accounts/accessory-sales?range=yearly",
                         headers=H(owner_token), timeout=30)
        j = r.json()
        revs = [row["revenue"] for row in j["top_skus"]]
        assert revs == sorted(revs, reverse=True), \
            f"top_skus not sorted desc: {revs}"


# ================= Section B — range keys =================
class TestRangeKeys:
    @pytest.mark.parametrize("rk,days_span", [
        ("daily", 0),
        ("weekly", 6),
        ("monthly", 29),
        ("quarterly", 89),
        ("half_yearly", 179),
        ("yearly", 364),
    ])
    def test_range_key_ok(self, owner_token, rk, days_span):
        r = requests.get(f"{API}/accounts/accessory-sales?range={rk}",
                         headers=H(owner_token), timeout=30)
        assert r.status_code == 200, f"{rk}: {r.text[:200]}"
        j = r.json()
        assert j["range"] == rk
        # verify window width roughly matches
        d_from = date.fromisoformat(j["from"])
        d_to = date.fromisoformat(j["to"])
        assert (d_to - d_from).days == days_span, \
            f"{rk} span expected {days_span}, got {(d_to - d_from).days}"

    def test_custom_ok(self, owner_token):
        today = date.today()
        frm = (today - timedelta(days=45)).isoformat()
        to = today.isoformat()
        r = requests.get(
            f"{API}/accounts/accessory-sales?range=custom&from={frm}&to={to}",
            headers=H(owner_token), timeout=30)
        assert r.status_code == 200, r.text[:200]
        j = r.json()
        assert j["from"] == frm
        assert j["to"] == to

    def test_custom_missing_from_to_400(self, owner_token):
        r = requests.get(f"{API}/accounts/accessory-sales?range=custom",
                         headers=H(owner_token), timeout=15)
        assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text[:200]}"

    def test_custom_missing_only_to_400(self, owner_token):
        r = requests.get(f"{API}/accounts/accessory-sales?range=custom&from=2026-01-01",
                         headers=H(owner_token), timeout=15)
        assert r.status_code == 400

    def test_empty_window_no_500(self, owner_token):
        """Ancient window: no paid invoices should exist there.
        Endpoint must return empty rollup, not 500."""
        r = requests.get(
            f"{API}/accounts/accessory-sales?range=custom&from=2015-01-01&to=2015-01-31",
            headers=H(owner_token), timeout=20)
        assert r.status_code == 200, r.text[:200]
        j = r.json()
        assert j["unit_count"] == 0
        assert j["revenue"] == 0.0
        assert j["invoice_count"] == 0
        assert j["top_skus"] == []


# ================= Section C — auth + tenant scoping =================
class TestAuthAndScope:
    def test_no_auth_401(self):
        r = requests.get(f"{API}/accounts/accessory-sales?range=monthly", timeout=15)
        assert r.status_code in (401, 403), \
            f"expected 401/403 for unauth call, got {r.status_code}"

    def test_founder_isolated_from_sound_clinic(self, founder_token, owner_token):
        """Founder lives on the Audinexa platform tenant, not sound-clinic.
        Founder's accessory-sales rollup must NOT include sound-clinic's revenue.
        """
        r_owner = requests.get(f"{API}/accounts/accessory-sales?range=yearly",
                               headers=H(owner_token), timeout=30)
        r_founder = requests.get(f"{API}/accounts/accessory-sales?range=yearly",
                                 headers=H(founder_token), timeout=30)
        assert r_owner.status_code == 200
        # Founder may 200 (own tenant with zero) or 403 (platform tenant blocked).
        # If 200, revenue must be tenant-scoped — not sound-clinic numbers.
        assert r_founder.status_code in (200, 403), r_founder.text[:200]
        if r_founder.status_code == 200:
            j_o = r_owner.json()
            j_f = r_founder.json()
            # Tenant scoping proof: numbers must differ (owner has revenue,
            # founder platform tenant has 0 accessory revenue).
            assert not (j_o["revenue"] == j_f["revenue"]
                        and j_o["unit_count"] == j_f["unit_count"]
                        and j_o["invoice_count"] == j_f["invoice_count"]
                        and j_o["revenue"] > 0), (
                "Founder saw same numbers as sound-clinic owner — tenant scope broken!"
                f" owner={j_o} founder={j_f}"
            )


# ================= Section D — InvoiceLineCreate accepts new fields =================
class TestInvoiceLineAccessoryFields:
    def _first_accessory_product(self, owner_token) -> dict | None:
        r = requests.get(f"{API}/ha/products?form_factor=accessory&active=true",
                         headers=H(owner_token), timeout=20)
        assert r.status_code == 200, r.text[:200]
        prods = r.json()
        # need one with variant_labels
        for p in prods:
            if p.get("variant_labels"):
                return p
        return prods[0] if prods else None

    def test_draft_invoice_with_accessory_fields_persists(self, owner_token, patient_id):
        prod = self._first_accessory_product(owner_token)
        assert prod, "no accessory products in tenant"
        variant = (prod.get("variant_labels") or [None])[0]

        # NAV-010 · INV-003 · accessory stock is now reserved at invoice
        # creation. Ensure the picked (product, variant) row has qty ≥ 1
        # before submitting so this test — which validates that the new
        # `accessory_product_id` / `accessory_variant` picker fields
        # persist on the invoice line — is not blocked by a stray
        # zero-stock demo SKU.
        try:
            hydrated = requests.get(
                f"{API}/ha/accessory-stock-hydrated",
                headers=H(owner_token),
                params={"branch_id": BRANCH_ID},
                timeout=15,
            ).json()
            items = hydrated.get("items") or []
            match = next(
                (r for r in items
                 if r.get("product_id") == prod["product_id"]
                 and (r.get("variant") == variant or (r.get("variant") is None and variant is None))),
                None,
            )
            if match and int(match.get("qty_on_hand") or 0) < 1:
                requests.post(
                    f"{API}/ha/accessory-stock/{match['sku_id']}/adjust",
                    headers=H(owner_token),
                    json={"delta": 5, "reason": "NAV-010 test seed"},
                    timeout=10,
                )
        except Exception:  # noqa: BLE001 — best-effort seed
            pass

        payload = {
            "patient_id": patient_id,
            "lines": [{
                "description": f"TEST_SALES_{uuid.uuid4().hex[:6]} accessory draft",
                "quantity": 1, "unit_price": 50.0,
                "is_taxable": False, "gst_rate": 0.0,
                "product_type": "Accessory",
                "make": prod.get("brand") or "TestBrand",
                "model": prod.get("model") or "TestModel",
                "accessory_product_id": prod["product_id"],
                "accessory_variant": variant,
            }],
            # NO initial_payment → invoice stays draft/unpaid
        }
        r = requests.post(f"{API}/billing/invoices", headers=H(owner_token),
                          json=payload, timeout=30)
        assert r.status_code in (200, 201), r.text[:400]
        inv = r.json()
        assert inv["status"] in ("draft", "unpaid"), inv["status"]
        # Verify persistence via GET
        r_g = requests.get(f"{API}/billing/invoices/{inv['invoice_id']}",
                           headers=H(owner_token), timeout=15)
        assert r_g.status_code == 200
        line = r_g.json()["lines"][0]
        assert line["accessory_product_id"] == prod["product_id"]
        assert line.get("accessory_variant") == variant
        # NAV-010 · INV-003: stock is reserved on create → decremented
        # flag is now True on the persisted line.
        assert line.get("accessory_stock_decremented") is True

    def test_regression_hearing_aid_line_no_picker_fields(self, owner_token, patient_id):
        """Backwards-compat: non-Accessory line (HA / service) still creates fine
        without the new picker fields — no 400/500 crash."""
        payload = {
            "patient_id": patient_id,
            "lines": [{
                "description": "TEST_REGRESSION regular service line",
                "quantity": 1, "unit_price": 500.0,
                "is_taxable": False, "gst_rate": 0.0,
                # No product_type at all → default 'Service' path
            }],
        }
        r = requests.post(f"{API}/billing/invoices", headers=H(owner_token),
                          json=payload, timeout=30)
        assert r.status_code in (200, 201), r.text[:400]
        inv = r.json()
        line = inv["lines"][0]
        # Accessory fields should be None / missing, not crash
        assert not line.get("accessory_product_id")
        assert not line.get("accessory_variant")
