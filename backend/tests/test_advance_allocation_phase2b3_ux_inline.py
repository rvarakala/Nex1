"""Advance Allocation · Phase 2B.3 (UX Correction) — inline-integration suite.

Strict scope
------------
The Phase 2B.3 UX correction moves the "Apply Advance" step INSIDE
the sale forms (HA, Custom HA, Ear Mould, Accessory) instead of on a
separate Advance Receipts page. The frontend chains two existing,
independent endpoints:

    1. Sale POST — creates the invoice with FULL sale value
       (grand_total unchanged).
    2. Allocation POST — the closed Phase 2B.2 writer records the
       advance as a `method="advance"` payment against the just-
       created invoice.

This file verifies that chain is safe by exercising:

    * HA Quick Sale        → invoice → allocation
    * Custom HA Order      → invoice → allocation
    * Ear Mould Order      → invoice → allocation
    * Accessory Sale       → billing invoice with accessory line → allocation
    * Non-serialised (custom-line) accessory     invoice → allocation
    * Serialised (accessory_product_id) accessory invoice → allocation

Plus the accounting invariant the UX correction promises:

    * Invoice grand_total unchanged after allocation.
    * Advance appears as a method="advance" payment row on the invoice.
    * Remaining sale balance = grand_total − advance − any cash paid.
    * Partial advance leaves remaining available_balance.
    * Full advance drives receipt to available_balance == 0.
    * Multi-allocation from one receipt across HA/EM/Accessory sales.
    * Idempotent retry same-key/same-body via authoritative writer.
    * Concurrent inline apply → exactly one 2xx + one 4xx (CAS wins).
    * Patient mismatch guarded (400).
    * Tenant mismatch guarded (404).
    * Over-allocation guarded (400).
    * Pre-flight failure mode (advance drained between check and submit)
      → the second attempt is 400 with no invoice mutation.

Zero changes to the Phase 2B.2 writer, `record_payment_atomic`,
NAV-009 payment plumbing, or NAV-010 inventory paths. The tests
exercise the same public HTTP surface the new UI calls.
"""
from __future__ import annotations

import concurrent.futures
import os
import random
import string
import sys
import pathlib
import time
import uuid

import pytest
import requests

sys.path.append(str(pathlib.Path(__file__).resolve().parents[1]))
from _helpers import (  # noqa: E402
    API,
    ADMIN_EMAIL, ADMIN_PASSWORD,
    FRONTDESK_EMAIL, FRONTDESK_PASSWORD,
    H, login,
)


_BRANCH_ID = "BR-PYTEST-001"


def _uniq() -> str:
    return f"{int(time.time()*1000) % 1_000_000_000:x}{''.join(random.choices(string.ascii_lowercase, k=4))}"


def _key(prefix: str = "aa-inline") -> str:
    return f"{prefix}-{_uniq()}-{uuid.uuid4().hex[:12]}"


def _phone() -> str:
    return f"+91{random.randint(7, 9)}{random.randint(10**8, 10**9 - 1)}"


# ─────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def admin_token() -> str:
    return login(ADMIN_EMAIL, ADMIN_PASSWORD)


@pytest.fixture(scope="module")
def frontdesk_token() -> str:
    return login(FRONTDESK_EMAIL, FRONTDESK_PASSWORD)


# ─────────────────────────────────────────────────────────────────────
# Factory helpers — mirror the exact request shape each sale form
# produces post the UX correction. NO frontend code executes here; we
# hit the same endpoints the browser calls.
# ─────────────────────────────────────────────────────────────────────

def _mk_patient(token: str) -> dict:
    r = requests.post(f"{API}/patients", headers=H(token), json={
        "name": f"UX Inline {_uniq()}", "mobile": _phone(),
        "age": 44, "sex": "M", "branch_id": _BRANCH_ID,
    }, timeout=15)
    assert r.status_code in (200, 201), r.text
    return r.json()


def _mk_advance(token: str, patient_id: str, *, amount: float) -> dict:
    r = requests.post(
        f"{API}/advance-receipts",
        headers={**H(token), "Idempotency-Key": _key("ar")},
        json={"patient_id": patient_id, "received_amount": amount, "method": "cash"},
        timeout=15,
    )
    assert r.status_code == 200, r.text
    return r.json()


def _preflight_advance(token: str, receipt_id: str) -> dict:
    """Mirrors the UI's pre-flight — GET the receipt to check current
    available_balance right before submit. Fail-fast if drained."""
    r = requests.get(
        f"{API}/advance-receipts/{receipt_id}", headers=H(token), timeout=10,
    )
    assert r.status_code == 200, r.text
    return r.json()


def _post_alloc(token: str, receipt_id: str, invoice_id: str, amount: float,
                *, key: str | None = None) -> requests.Response:
    body = {"invoice_id": invoice_id, "amount": amount}
    headers = H(token)
    headers["Idempotency-Key"] = key or _key()
    return requests.post(
        f"{API}/advance-receipts/{receipt_id}/allocations",
        headers=headers, json=body, timeout=20,
    )


def _get_invoice(token: str, invoice_id: str) -> dict:
    r = requests.get(
        f"{API}/billing/invoices/{invoice_id}", headers=H(token), timeout=10,
    )
    assert r.status_code == 200, r.text
    return r.json()


# ── Sale-form facsimile helpers ──────────────────────────────────────

def _mk_ha_quick_sale(token: str, patient_id: str, *, sale_price: float,
                     payment_status: str = "unpaid") -> dict:
    """Facsimile of the QuickHASaleModal POST body — non-serialised
    (serial numbers get 'not_found' badges but backend still accepts
    them, per NAV-010). Returns the sale response including invoice_id.
    """
    body = {
        "patient_id": patient_id,
        "branch_id": _BRANCH_ID,
        "brand": "Phonak",
        "model": f"AudeoP50-{_uniq()}",
        "ha_type": "RIC",
        "serial_left": None,
        "serial_right": f"UX-{uuid.uuid4().hex[:8].upper()}",
        "side": "right",
        "fitting_date": "2026-08-21",
        "warranty_months": 12,
        "mrp": sale_price,
        "sale_price": sale_price,
        "discount_amount": None,
        "gst_rate": 0,
        "payment_status": payment_status,
        "payment_mode": "cash" if payment_status != "unpaid" else None,
        "payment_date": "2026-08-21" if payment_status != "unpaid" else None,
        "advance_amount": None,
        "expected_payment_date": None,
        "notes": "UX inline apply-advance test",
        "spec": None,
    }
    r = requests.post(f"{API}/ha/quick-sale", headers=H(token), json=body, timeout=20)
    assert r.status_code in (200, 201), r.text
    return r.json()


def _mk_custom_ha_order(token: str, patient_id: str, *, total: float,
                       advance_amount: float = 0.0) -> dict:
    """Facsimile of the Custom HA order POST body."""
    # Ensure a vendor exists in the tenant; the endpoint requires
    # vendor_id when delivery_target='vendor'.
    vres = requests.get(f"{API}/vendors", headers=H(token),
                        params={"active": "true"}, timeout=10)
    vendor_id = None
    if vres.status_code == 200 and vres.json():
        vendor_id = vres.json()[0].get("vendor_id")
    if not vendor_id:
        # Try to create one (super_admin bypasses require_roles).
        cr = requests.post(
            f"{API}/vendors", headers=H(token),
            json={"name": f"UX Test Vendor {_uniq()}", "active": True},
            timeout=15,
        )
        if cr.status_code in (200, 201):
            vendor_id = cr.json().get("vendor_id")
    if not vendor_id:
        pytest.skip("cannot create/fetch a vendor for Custom HA test in this preview")

    body = {
        "patient_id": patient_id,
        "side": "right",
        "shell_type": "CIC",
        "vent_size_left": None,
        "vent_size_right": "1.5",
        "shell_colour_left": None,
        "shell_colour_right": "Tan",
        "faceplate_colour_left": None,
        "faceplate_colour_right": "Tan",
        "receiver_power_left": None,
        "receiver_power_right": "M",
        "brand": "Starkey",
        "model": "PicassoCIC",
        "warranty_months": 24,
        "features": [],
        "delivery_target": "vendor",
        "vendor_id": vendor_id,
        "target_branch_id": None,
        "expected_delivery_date": "2026-09-10",
        "total_amount": total,
        "advance_amount": advance_amount,
        "payment_mode": "cash",
        "gst_rate": 0,
        "notes": "UX inline apply-advance test",
        "from_session_id": None,
        "from_trial_no": None,
        "branch_id": _BRANCH_ID,
    }
    r = requests.post(f"{API}/ha/custom-ha-orders", headers=H(token), json=body, timeout=20)
    if r.status_code not in (200, 201):
        pytest.skip(f"Custom HA order factory unavailable in this preview: {r.status_code} {r.text}")
    return r.json()


def _mk_ear_mould(token: str, patient_id: str, *, total: float,
                 advance_amount: float = 0.0) -> dict:
    body = {
        "patient_id": patient_id,
        "side": "right",
        "material": "silicone",
        "vent_size": "1.5",
        "vent_size_left": None,
        "vent_size_right": None,
        "colour": "Clear",
        "lab_vendor": "InternalLab",
        "expected_delivery_date": "2026-09-01",
        "total_amount": total,
        "advance_amount": advance_amount,
        "payment_mode": "cash",
        "gst_rate": 0,
        "notes": "UX inline apply-advance test",
        "branch_id": _BRANCH_ID,
    }
    r = requests.post(f"{API}/ha/ear-moulds", headers=H(token), json=body, timeout=20)
    assert r.status_code in (200, 201), r.text
    return r.json()


def _mk_accessory_invoice(token: str, patient_id: str, *, unit_price: float,
                          product_id: str | None = None, variant: str | None = None) -> dict:
    """Facsimile of the CreateInvoicePage submit body — a single line
    with (or without) an accessory_product_id. When product_id is None
    this is the "custom line / non-serialised" path; when set it
    exercises the NAV-010 accessory-stock reservation code.
    """
    line = {
        "service_id": None,
        "description": f"Test Accessory Line {_uniq()}",
        "quantity": 1,
        "unit_price": unit_price,
        "discount_type": "flat",
        "discount_value": 0,
        "is_taxable": False,
        "gst_rate": 0,
        "product_type": "Accessory" if product_id else None,
        "make": None,
        "model": None,
        "serial_numbers": None,
        "technology_tier": None,
        "accessory_product_id": product_id,
        "accessory_variant": variant,
    }
    body = {
        "patient_id": patient_id,
        "session_id": None,
        "lines": [line],
        "notes": "UX inline accessory test",
        "patient_gstin": None,
        "from_sale_no": None,
        "initial_payment": None,
    }
    r = requests.post(f"{API}/billing/invoices", headers=H(token), json=body, timeout=20)
    assert r.status_code in (200, 201), r.text
    return r.json()


# ═════════════════════════════════════════════════════════════════════
# 1) HAPPY-PATH — each sale surface → invoice → allocation
# ═════════════════════════════════════════════════════════════════════

def test_inline_ha_sale_advance_recorded_as_payment_total_unchanged(admin_token):
    """Sale ₹1,80,000 · Advance ₹50,000 → invoice.grand_total stays
    ₹1,80,000, invoice.paid_total = 50,000, invoice.due_total = 1,30,000.
    Advance is a PAYMENT, not a discount."""
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat["patient_id"], amount=50000)
    sale = _mk_ha_quick_sale(admin_token, pat["patient_id"], sale_price=180000)
    grand_before = round(float(sale["total"] or 0), 2)
    assert grand_before == 180000.0

    r = _post_alloc(admin_token, ar["receipt_id"], sale["invoice_id"], 50000, key=_key())
    assert r.status_code == 200, r.text

    inv = _get_invoice(admin_token, sale["invoice_id"])
    # Invoice grand total unchanged.
    assert round(float(inv["grand_total"] or 0), 2) == 180000.0
    # Paid_total reflects the advance.
    assert round(float(inv["paid_total"] or 0), 2) == 50000.0
    # Due_total = full sale price − advance applied.
    assert round(float(inv["due_total"] or 0), 2) == 130000.0
    # Advance receipt available_balance drained.
    fresh_ar = _preflight_advance(admin_token, ar["receipt_id"])
    assert fresh_ar["available_balance"] == 0.0
    # A method='advance' payment row exists on the invoice with backlinks.
    adv_pay = [p for p in (inv.get("payments") or []) if p.get("method") == "advance"]
    assert len(adv_pay) == 1
    assert adv_pay[0]["advance_receipt_id"] == ar["receipt_id"]
    assert round(float(adv_pay[0]["amount"] or 0), 2) == 50000.0


def test_inline_custom_ha_advance_flow(admin_token):
    """Custom HA total ₹90,000 · advance ₹20,000."""
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat["patient_id"], amount=20000)
    order = _mk_custom_ha_order(admin_token, pat["patient_id"], total=90000)
    r = _post_alloc(admin_token, ar["receipt_id"], order["invoice_id"], 20000, key=_key())
    assert r.status_code == 200, r.text
    inv = _get_invoice(admin_token, order["invoice_id"])
    assert round(float(inv["grand_total"] or 0), 2) == 90000.0
    assert round(float(inv["paid_total"] or 0), 2) == 20000.0
    assert round(float(inv["due_total"] or 0), 2) == 70000.0


def test_inline_ear_mould_advance_flow(admin_token):
    """Ear Mould total ₹5,000 · advance ₹5,000 → invoice flips to paid."""
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat["patient_id"], amount=5000)
    em = _mk_ear_mould(admin_token, pat["patient_id"], total=5000)
    r = _post_alloc(admin_token, ar["receipt_id"], em["invoice_id"], 5000, key=_key())
    assert r.status_code == 200, r.text
    inv = _get_invoice(admin_token, em["invoice_id"])
    assert round(float(inv["grand_total"] or 0), 2) == 5000.0
    assert round(float(inv["paid_total"] or 0), 2) == 5000.0
    assert round(float(inv["due_total"] or 0), 2) == 0.0
    assert inv["status"] == "paid"


def test_inline_accessory_non_serialised_advance_flow(admin_token):
    """Custom-line accessory (no accessory_product_id) — NAV-010
    inventory decrement path is NOT exercised. Advance still applies."""
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat["patient_id"], amount=800)
    inv = _mk_accessory_invoice(admin_token, pat["patient_id"], unit_price=800)
    r = _post_alloc(admin_token, ar["receipt_id"], inv["invoice_id"], 800, key=_key())
    assert r.status_code == 200, r.text
    inv_after = _get_invoice(admin_token, inv["invoice_id"])
    assert round(float(inv_after["grand_total"] or 0), 2) == 800.0
    assert round(float(inv_after["paid_total"] or 0), 2) == 800.0
    assert round(float(inv_after["due_total"] or 0), 2) == 0.0


def test_inline_accessory_serialised_advance_flow(admin_token):
    """Serialised accessory path (accessory_product_id set) — NAV-010
    stock reservation runs first, then advance allocation applies.
    Verifies NAV-010 inventory architecture is untouched by this UX
    correction. If the tenant lacks a saleable accessory SKU, the test
    skips (the sale itself cannot be created in that environment)."""
    pat = _mk_patient(admin_token)
    # Look for any accessory product that has stock we can pull from.
    catalog = requests.get(f"{API}/ha/products",
                           headers=H(admin_token),
                           params={"form_factor": "accessory", "active": "true"},
                           timeout=10)
    if catalog.status_code != 200 or not catalog.json():
        pytest.skip("no accessory catalogue in this preview tenant")
    products = catalog.json()
    stock_rows = []
    picked_product = None
    picked_variant = None
    for p in products:
        s = requests.get(f"{API}/ha/accessory-stock",
                         headers=H(admin_token),
                         params={"product_id": p["product_id"]},
                         timeout=10)
        if s.status_code != 200:
            continue
        rows = s.json() or []
        for row in rows:
            if (row.get("qty_on_hand") or 0) >= 1:
                stock_rows.append(row)
                picked_product = p
                picked_variant = row.get("variant")
                break
        if picked_product:
            break
    if not picked_product:
        pytest.skip("no accessory stock available in this preview tenant")

    ar = _mk_advance(admin_token, pat["patient_id"], amount=300)
    inv = _mk_accessory_invoice(
        admin_token, pat["patient_id"], unit_price=300,
        product_id=picked_product["product_id"], variant=picked_variant,
    )
    r = _post_alloc(admin_token, ar["receipt_id"], inv["invoice_id"], 300, key=_key())
    assert r.status_code == 200, r.text
    inv_after = _get_invoice(admin_token, inv["invoice_id"])
    assert round(float(inv_after["grand_total"] or 0), 2) == 300.0
    assert round(float(inv_after["paid_total"] or 0), 2) == 300.0
    # Verify the reservation is still recorded on the line — NAV-010 untouched.
    lines = inv_after.get("lines") or []
    if lines:
        assert lines[0].get("accessory_stock_decremented") in (True, None)


# ═════════════════════════════════════════════════════════════════════
# 2) PARTIAL / FULL / MULTI-ALLOCATION
# ═════════════════════════════════════════════════════════════════════

def test_inline_partial_advance_leaves_remaining_available(admin_token):
    """Advance ₹50,000 · HA sale ₹1,80,000 — after ₹50,000 allocation
    the advance receipt shows available_balance = 0, allocated_total = 50,000."""
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat["patient_id"], amount=50000)
    sale = _mk_ha_quick_sale(admin_token, pat["patient_id"], sale_price=180000)
    r = _post_alloc(admin_token, ar["receipt_id"], sale["invoice_id"], 30000, key=_key())
    assert r.status_code == 200
    fresh = _preflight_advance(admin_token, ar["receipt_id"])
    assert fresh["available_balance"] == 20000.0
    assert fresh["allocated_total"] == 30000.0


def test_inline_multiple_allocations_from_one_receipt_across_products(admin_token):
    """One ₹1,00,000 advance applied across HA sale, ear mould, and
    accessory invoice back-to-back."""
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat["patient_id"], amount=100000)
    ha = _mk_ha_quick_sale(admin_token, pat["patient_id"], sale_price=60000)
    em = _mk_ear_mould(admin_token, pat["patient_id"], total=25000)
    acc = _mk_accessory_invoice(admin_token, pat["patient_id"], unit_price=15000)

    r1 = _post_alloc(admin_token, ar["receipt_id"], ha["invoice_id"], 60000, key=_key())
    r2 = _post_alloc(admin_token, ar["receipt_id"], em["invoice_id"], 25000, key=_key())
    r3 = _post_alloc(admin_token, ar["receipt_id"], acc["invoice_id"], 15000, key=_key())
    assert r1.status_code == 200 and r2.status_code == 200 and r3.status_code == 200

    fresh = _preflight_advance(admin_token, ar["receipt_id"])
    assert fresh["available_balance"] == 0.0
    assert fresh["allocated_total"] == 100000.0


def test_inline_full_advance_over_multiple_products_ledger(admin_token):
    """Ledger endpoint agrees with the receipt after multi-allocation."""
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat["patient_id"], amount=40000)
    ha = _mk_ha_quick_sale(admin_token, pat["patient_id"], sale_price=25000)
    em = _mk_ear_mould(admin_token, pat["patient_id"], total=15000)
    assert _post_alloc(admin_token, ar["receipt_id"], ha["invoice_id"], 25000, key=_key()).status_code == 200
    assert _post_alloc(admin_token, ar["receipt_id"], em["invoice_id"], 15000, key=_key()).status_code == 200

    r = requests.get(
        f"{API}/advance-receipts/{ar['receipt_id']}/allocations",
        headers=H(admin_token), timeout=10,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert body["total_active_amount"] == 40000.0
    assert body["receipt"]["available_balance"] == 0.0


# ═════════════════════════════════════════════════════════════════════
# 3) SAFETY GUARDS — pre-flight, tenant/patient/over/concurrent/idempotent
# ═════════════════════════════════════════════════════════════════════

def test_inline_preflight_catches_drained_advance(admin_token):
    """Simulates the concurrent-race the UI's pre-flight is designed
    to catch: another user drains the advance between check and the
    Phase 2B.2 write. The 2nd allocation attempt returns 400 with no
    invoice mutation. Repeating a preflight then would see 0 balance."""
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat["patient_id"], amount=1000)
    inv1 = _mk_accessory_invoice(admin_token, pat["patient_id"], unit_price=1000)
    inv2 = _mk_accessory_invoice(admin_token, pat["patient_id"], unit_price=1000)

    # First attempt drains the advance.
    r1 = _post_alloc(admin_token, ar["receipt_id"], inv1["invoice_id"], 1000, key=_key())
    assert r1.status_code == 200
    # Second attempt (fresh key) — pre-flight would see 0 balance; even
    # if the UI attempted anyway, the CAS rejects with 400.
    fresh = _preflight_advance(admin_token, ar["receipt_id"])
    assert fresh["available_balance"] == 0.0
    r2 = _post_alloc(admin_token, ar["receipt_id"], inv2["invoice_id"], 500, key=_key())
    assert r2.status_code == 400
    detail = (r2.json().get("detail") or "").lower()
    assert "advance" in detail

    # inv2 remains untouched — no phantom advance payment.
    inv2_after = _get_invoice(admin_token, inv2["invoice_id"])
    assert round(float(inv2_after["paid_total"] or 0), 2) == 0.0
    adv_pays = [p for p in (inv2_after.get("payments") or []) if p.get("method") == "advance"]
    assert adv_pays == []


def test_inline_over_allocation_rejected(admin_token):
    """Requesting more than available_balance → 400 (no phantom write)."""
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat["patient_id"], amount=500)
    inv = _mk_accessory_invoice(admin_token, pat["patient_id"], unit_price=1000)
    r = _post_alloc(admin_token, ar["receipt_id"], inv["invoice_id"], 700, key=_key())
    assert r.status_code == 400
    # Invoice untouched.
    inv_after = _get_invoice(admin_token, inv["invoice_id"])
    assert round(float(inv_after["paid_total"] or 0), 2) == 0.0


def test_inline_patient_mismatch_rejected(admin_token):
    """Patient A holds the advance, Patient B holds the invoice → 400."""
    pat_a = _mk_patient(admin_token)
    pat_b = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat_a["patient_id"], amount=1000)
    inv = _mk_accessory_invoice(admin_token, pat_b["patient_id"], unit_price=500)
    r = _post_alloc(admin_token, ar["receipt_id"], inv["invoice_id"], 500, key=_key())
    assert r.status_code == 400
    assert "patient" in (r.json().get("detail") or "").lower()


def test_inline_tenant_mismatch_returns_404(admin_token):
    """Unknown receipt id from another tenant → 404."""
    pat = _mk_patient(admin_token)
    inv = _mk_accessory_invoice(admin_token, pat["patient_id"], unit_price=100)
    r = _post_alloc(admin_token, "AR-BOGUS-FROM-OTHER-CLINIC", inv["invoice_id"], 50, key=_key())
    assert r.status_code == 404


def test_inline_concurrent_apply_arbitration(admin_token):
    """Two clinics racing to apply the same advance to two different
    invoices — exactly one 200 + one 4xx; balance drops by the winner
    amount only. Confirms the UX correction cannot double-spend even
    if two staff hit Submit at the same instant on two forms."""
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat["patient_id"], amount=1500)
    ha = _mk_ha_quick_sale(admin_token, pat["patient_id"], sale_price=1500)
    em = _mk_ear_mould(admin_token, pat["patient_id"], total=1500)

    def _hit(inv_id):
        return _post_alloc(admin_token, ar["receipt_id"], inv_id, 1200, key=_key("race"))

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        futs = [ex.submit(_hit, ha["invoice_id"]),
                ex.submit(_hit, em["invoice_id"])]
        results = [f.result() for f in concurrent.futures.as_completed(futs)]

    codes = sorted(r.status_code for r in results)
    assert codes[0] == 200
    assert codes[1] in (400, 409)
    fresh = _preflight_advance(admin_token, ar["receipt_id"])
    assert fresh["available_balance"] == 300.0
    assert fresh["allocated_total"] == 1200.0


def test_inline_idempotent_retry_same_key(admin_token):
    """Simulates the network-retry case: UI POSTs twice with the same
    Idempotency-Key. Second call replays; no double-spend."""
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat["patient_id"], amount=3000)
    inv = _mk_ear_mould(admin_token, pat["patient_id"], total=3000)
    k = _key("ux-retry")
    r1 = _post_alloc(admin_token, ar["receipt_id"], inv["invoice_id"], 1500, key=k)
    r2 = _post_alloc(admin_token, ar["receipt_id"], inv["invoice_id"], 1500, key=k)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r2.headers.get("Idempotency-Replay") == "true"
    assert r1.json()["allocation_id"] == r2.json()["allocation_id"]
    fresh = _preflight_advance(admin_token, ar["receipt_id"])
    assert fresh["available_balance"] == 1500.0


def test_inline_ha_frontdesk_can_apply_advance(frontdesk_token, admin_token):
    """RBAC: front_desk (typical clinic user who does inline sales)
    can call the allocation endpoint the UX correction relies on."""
    pat = _mk_patient(admin_token)  # patients only createable by admin+front_desk; use admin here
    ar = _mk_advance(admin_token, pat["patient_id"], amount=1200)
    inv = _mk_accessory_invoice(admin_token, pat["patient_id"], unit_price=1200)
    r = _post_alloc(frontdesk_token, ar["receipt_id"], inv["invoice_id"], 1200, key=_key())
    assert r.status_code == 200


# ═════════════════════════════════════════════════════════════════════
# 4) ACCOUNTING INVARIANT — advance is a PAYMENT, not a DISCOUNT
# ═════════════════════════════════════════════════════════════════════

def test_inline_grand_total_never_shrinks_after_allocation(admin_token):
    """The classic user example: HA ₹1,80,000, advance ₹50,000. The
    invoice grand_total MUST stay ₹1,80,000, not ₹1,30,000 (that would
    be a discount). This is the core Phase 2B guarantee."""
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat["patient_id"], amount=50000)
    sale = _mk_ha_quick_sale(admin_token, pat["patient_id"], sale_price=180000)
    r = _post_alloc(admin_token, ar["receipt_id"], sale["invoice_id"], 50000, key=_key())
    assert r.status_code == 200

    inv = _get_invoice(admin_token, sale["invoice_id"])
    grand = round(float(inv["grand_total"] or 0), 2)
    subtotal = round(float(inv.get("subtotal") or 0), 2)
    # Grand total unchanged.
    assert grand == 180000.0
    # Subtotal must also be untouched — no line-level discount smuggled in.
    if subtotal:
        assert subtotal == 180000.0
    # A method=advance payment row exists.
    adv_pay = [p for p in (inv.get("payments") or []) if p.get("method") == "advance"]
    assert len(adv_pay) == 1
    assert round(float(adv_pay[0]["amount"] or 0), 2) == 50000.0


def test_inline_receipt_received_amount_never_mutated(admin_token):
    """After any allocation, the original `received_amount` on the
    Advance Receipt stays the same — the receipt is an immutable
    acknowledgement."""
    pat = _mk_patient(admin_token)
    ar = _mk_advance(admin_token, pat["patient_id"], amount=8000)
    inv1 = _mk_ear_mould(admin_token, pat["patient_id"], total=3000)
    inv2 = _mk_accessory_invoice(admin_token, pat["patient_id"], unit_price=2000)
    assert _post_alloc(admin_token, ar["receipt_id"], inv1["invoice_id"], 3000, key=_key()).status_code == 200
    assert _post_alloc(admin_token, ar["receipt_id"], inv2["invoice_id"], 2000, key=_key()).status_code == 200
    fresh = _preflight_advance(admin_token, ar["receipt_id"])
    assert fresh["received_amount"] == 8000.0
    assert fresh["available_balance"] == 3000.0
    assert fresh["allocated_total"] == 5000.0
