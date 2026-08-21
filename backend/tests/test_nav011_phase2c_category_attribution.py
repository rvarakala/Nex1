"""NAV-011 · Phase 2C — Category-Aware External-Partner Revenue Attribution.

Verifies the READ-SIDE analytics enhancement to
``routers/referral_partners.py::_attribute_revenue``:

  * ``diagnostics_revenue`` — Diagnostics-Income slice of net-collected.
  * ``ha_sales_revenue``    — Hearing-Aid / Core-Business slice.
  * ``total_attributed_revenue`` — sum of the two mutually-exclusive
    buckets (equals canonical ``invoice_revenue`` within rounding).

Business rules under test:
  * Attribution is analytical; payout writers are UNAFFECTED.
  * Referral commission remains fully discretionary — fixed, percent,
    zero — and Phase 2C never re-derives it from the new category
    fields.
  * NAV-011 Phase 2A behaviour (``total_revenue``, ``commission_estimate``,
    ``ha_sale_revenue``) is preserved for backward compatibility.

Scope discipline: this file does NOT touch NAV-009 / NAV-010 / NAV-012 /
the internal referring-doctor path / payout writers / recovery-ledger /
any financial atomic path. Every assertion is read-side only.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import random
import string
import sys
import time
import uuid
from typing import Optional

import pytest
import requests
from motor.motor_asyncio import AsyncIOMotorClient

sys.path.append(str(pathlib.Path(__file__).resolve().parents[1]))
from _helpers import API, ADMIN_EMAIL, ADMIN_PASSWORD, H, login  # noqa: E402


_BRANCH_ID = "BR-PYTEST-001"
_CLINIC_ID = os.environ.get("TEST_CLINIC_ID", "clinic-pytest-suite")


# ─── Mongo helpers (read-only for assertions, direct writes are limited to
#     synthetic test-fixture rows on the ``clinic-pytest-suite`` tenant only) ──

def _run_mongo(fn):
    loop = asyncio.new_event_loop()
    try:
        async def _wrapped():
            cli = AsyncIOMotorClient(os.environ["MONGO_URL"])
            try:
                return await fn(cli[os.environ["DB_NAME"]])
            finally:
                cli.close()
        return loop.run_until_complete(_wrapped())
    finally:
        loop.close()


def _uniq() -> str:
    return f"{int(time.time()*1000)%1_000_000_000:x}{''.join(random.choices(string.ascii_lowercase, k=4))}"


def _phone() -> str:
    return f"+91{random.randint(7, 9)}{random.randint(10**8, 10**9 - 1)}"


# ─── API sugar (unchanged from Phase 2A test helpers, duplicated here to
#     keep this suite independent of Phase 2A file layout) ──

def _mk_patient(tok: str) -> str:
    r = requests.post(f"{API}/patients", headers=H(tok), json={
        "name": f"NAV011-P2C Patient {_uniq()}",
        "mobile": _phone(), "age": 40, "sex": "M",
        "branch_id": _BRANCH_ID,
    }, timeout=10)
    assert r.status_code in (200, 201), r.text
    return r.json()["patient_id"]


def _mk_service(tok: str, price: float = 3000.0,
                category: str = "Consultation") -> str:
    r = requests.post(f"{API}/billing/services", headers=H(tok), json={
        "code": f"P2C-{_uniq()[:6].upper()}",
        "name": f"P2C {category} service",
        "price": price, "gst_rate": 0,
        "category": category, "active": True,
    }, timeout=10)
    assert r.status_code in (200, 201), r.text
    return r.json()["service_id"]


def _mk_partner(tok: str, kind: str = "percent", value: float = 10.0) -> dict:
    r = requests.post(f"{API}/referral-partners", headers=H(tok), json={
        "name": f"NAV011-P2C Partner {_uniq()}",
        "email": f"p2c-{_uniq()}@partner.example.com",
        "phone": _phone(),
        "commission_kind": kind, "commission_value": value,
    }, timeout=10)
    if r.status_code not in (200, 201):
        pytest.skip(f"partner create not available ({r.status_code}): {r.text[:200]}")
    return r.json()


def _attach_partner(tok: str, patient_id: str, code: str) -> None:
    r = requests.post(
        f"{API}/referral-partners/patients/{patient_id}/attach-code",
        headers=H(tok), json={"referral_code": code}, timeout=10,
    )
    assert r.status_code == 200, r.text


def _mk_invoice(tok: str, patient_id: str, service_id: str, *,
                unit_price: float = 5000.0, quantity: int = 1,
                initial_payment: Optional[float] = None) -> dict:
    body = {
        "patient_id": patient_id,
        "lines": [{
            "service_id": service_id, "description": "svc",
            "quantity": quantity, "unit_price": unit_price,
            "discount_type": "flat", "discount_value": 0,
        }],
    }
    if initial_payment is not None:
        body["initial_payment"] = {"method": "cash", "amount": initial_payment}
    r = requests.post(f"{API}/billing/invoices", headers=H(tok),
                      json=body, timeout=15)
    assert r.status_code in (200, 201), r.text
    return r.json()


def _refund(tok: str, invoice_id: str, amount: float) -> dict:
    r = requests.post(
        f"{API}/billing/invoices/{invoice_id}/refund",
        headers=H(tok),
        json={"method": "cash", "amount": amount, "reason": "P2C test"},
        timeout=15,
    )
    assert r.status_code in (200, 201), r.text
    return r.json()


def _get_stats(tok: str, partner_id: str,
               start: str = "2025-01-01", end: str = "2027-12-31") -> dict:
    r = requests.get(
        f"{API}/referral-partners/{partner_id}/stats",
        headers=H(tok), params={"start": start, "end": end}, timeout=10,
    )
    assert r.status_code == 200, r.text
    return r.json()


# ─── Fixture helpers to steer the classifier ─────────────────────────
#
# Phase 2C's classifier prefers ``appointment.wing == 'hearing_aid'`` over
# the per-line ``product_type`` field. Both signals are legal — we test
# both.

def _link_invoice_to_ha_wing_appointment(invoice_id: str, patient_id: str) -> str:
    """Create a synthetic appointment with ``wing='hearing_aid'`` and
    attach it to the invoice via ``invoices.appointment_id``. Direct
    DB write on ``clinic-pytest-suite`` synthetic data — no historical
    or Production record is touched."""
    appt_id = f"APT-P2C-{uuid.uuid4().hex[:10].upper()}"
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    _run_mongo(lambda db: db.appointments.insert_one({
        "appointment_id": appt_id,
        "clinic_id": _CLINIC_ID,
        "branch_id": _BRANCH_ID,
        "patient_id": patient_id,
        "wing": "hearing_aid",
        "status": "completed",
        "created_at": now_iso,
        "updated_at": now_iso,
    }))
    _run_mongo(lambda db: db.invoices.update_one(
        {"invoice_id": invoice_id, "clinic_id": _CLINIC_ID},
        {"$set": {"appointment_id": appt_id}},
    ))
    return appt_id


def _stamp_line_product_type(invoice_id: str, product_type: str) -> None:
    """Set ``product_type`` on ALL invoice lines. Synthetic Preview
    write only."""
    _run_mongo(lambda db: db.invoices.update_one(
        {"invoice_id": invoice_id, "clinic_id": _CLINIC_ID},
        {"$set": {"lines.$[].product_type": product_type}},
    ))


# ─── Fixture ──────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def tok() -> str:
    return login(ADMIN_EMAIL, ADMIN_PASSWORD)


# =====================================================================
# 1. Diagnostics-only referral revenue
# =====================================================================

def test_p2c_diagnostics_only_populates_only_diagnostics_bucket(tok):
    """A patient's paid diagnostic invoice → all revenue lands in
    ``diagnostics_revenue``; ``ha_sales_revenue`` remains 0."""
    p = _mk_partner(tok)
    pat = _mk_patient(tok)
    _attach_partner(tok, pat, p["referral_code"])
    svc = _mk_service(tok, price=4000.0, category="Consultation")
    _mk_invoice(tok, pat, svc, unit_price=4000, initial_payment=4000)

    s = _get_stats(tok, p["partner_id"])["stats"]

    assert s["diagnostics_revenue"] == 4000.0
    assert s["ha_sales_revenue"] == 0.0
    assert s["total_attributed_revenue"] == 4000.0
    # Backward-compat: legacy fields still populated.
    assert s["invoice_revenue"] == 4000.0
    assert s["total_revenue"] == 4000.0


# =====================================================================
# 2. Hearing-Aid-only referral revenue (via appointment.wing)
# =====================================================================

def test_p2c_ha_wing_appointment_routes_revenue_to_ha_bucket(tok):
    """An invoice linked to an appointment on the ``hearing_aid`` wing
    is classified as HA regardless of the line's product_type."""
    p = _mk_partner(tok)
    pat = _mk_patient(tok)
    _attach_partner(tok, pat, p["referral_code"])
    svc = _mk_service(tok, price=25000.0, category="Consultation")
    inv = _mk_invoice(tok, pat, svc, unit_price=25000, initial_payment=25000)
    _link_invoice_to_ha_wing_appointment(inv["invoice_id"], pat)

    s = _get_stats(tok, p["partner_id"])["stats"]

    assert s["ha_sales_revenue"] == 25000.0
    assert s["diagnostics_revenue"] == 0.0
    assert s["total_attributed_revenue"] == 25000.0


# =====================================================================
# 3. Hearing-Aid revenue via line.product_type ('Hearing Aid')
# =====================================================================

def test_p2c_line_product_type_hearing_aid_routes_to_ha_bucket(tok):
    """A regular invoice with lines stamped ``product_type='Hearing Aid'``
    (no HA-wing appointment) is classified as HA."""
    p = _mk_partner(tok)
    pat = _mk_patient(tok)
    _attach_partner(tok, pat, p["referral_code"])
    svc = _mk_service(tok, price=18000.0, category="Consultation")
    inv = _mk_invoice(tok, pat, svc, unit_price=18000, initial_payment=18000)
    _stamp_line_product_type(inv["invoice_id"], "Hearing Aid")

    s = _get_stats(tok, p["partner_id"])["stats"]

    assert s["ha_sales_revenue"] == 18000.0
    assert s["diagnostics_revenue"] == 0.0
    assert s["total_attributed_revenue"] == 18000.0


# =====================================================================
# 4. Mixed portfolio — one HA invoice + one Diagnostics invoice
# =====================================================================

def test_p2c_mixed_invoices_split_by_category(tok):
    """A single patient with two invoices — one HA-wing (₹30k) and one
    Diagnostics (₹4k) — the classifier splits them correctly and the
    total matches the sum."""
    p = _mk_partner(tok)
    pat = _mk_patient(tok)
    _attach_partner(tok, pat, p["referral_code"])
    diag_svc = _mk_service(tok, price=4000.0, category="Consultation")
    ha_svc = _mk_service(tok, price=30000.0, category="Consultation")
    _mk_invoice(tok, pat, diag_svc, unit_price=4000, initial_payment=4000)
    ha_inv = _mk_invoice(tok, pat, ha_svc, unit_price=30000, initial_payment=30000)
    _link_invoice_to_ha_wing_appointment(ha_inv["invoice_id"], pat)

    s = _get_stats(tok, p["partner_id"])["stats"]

    assert s["diagnostics_revenue"] == 4000.0
    assert s["ha_sales_revenue"] == 30000.0
    assert s["total_attributed_revenue"] == 34000.0


# =====================================================================
# 5. Total = Diagnostics + HA (rounding-safe invariant)
# =====================================================================

def test_p2c_total_attributed_equals_sum_of_categories(tok):
    """Invariant: total_attributed_revenue == diagnostics + ha_sales,
    within ₹0.01 of rounding. Verified across a mix of full and
    partial payments to exercise the ``net/gross`` scale factor."""
    p = _mk_partner(tok)
    pat = _mk_patient(tok)
    _attach_partner(tok, pat, p["referral_code"])
    diag_svc = _mk_service(tok, price=3333.33, category="Consultation")
    ha_svc = _mk_service(tok, price=22222.22, category="Consultation")
    _mk_invoice(tok, pat, diag_svc,
                unit_price=3333.33, initial_payment=1666.67)  # partial
    ha_inv = _mk_invoice(tok, pat, ha_svc,
                         unit_price=22222.22, initial_payment=22222.22)
    _link_invoice_to_ha_wing_appointment(ha_inv["invoice_id"], pat)

    s = _get_stats(tok, p["partner_id"])["stats"]

    assert abs(
        s["total_attributed_revenue"]
        - (s["diagnostics_revenue"] + s["ha_sales_revenue"])
    ) < 0.02


# =====================================================================
# 6. Multiple patients — attribution aggregates across the partner
# =====================================================================

def test_p2c_multiple_patients_aggregate_across_partner(tok):
    """Two patients, two invoices each. All Diagnostics.
    ``diagnostics_revenue`` should aggregate over both patients."""
    p = _mk_partner(tok)
    svc = _mk_service(tok, price=2500.0, category="Consultation")
    for _ in range(2):
        pat = _mk_patient(tok)
        _attach_partner(tok, pat, p["referral_code"])
        _mk_invoice(tok, pat, svc, unit_price=2500, initial_payment=2500)
        _mk_invoice(tok, pat, svc, unit_price=2500, initial_payment=2500)

    s = _get_stats(tok, p["partner_id"])["stats"]

    assert s["patients"] == 2
    assert s["diagnostics_revenue"] == 10000.0  # 2 patients × 2 invoices × ₹2500
    assert s["ha_sales_revenue"] == 0.0
    assert s["total_attributed_revenue"] == 10000.0


# =====================================================================
# 7. Refund reduces the correct category
# =====================================================================

def test_p2c_refund_on_ha_wing_reduces_ha_bucket(tok):
    """A refund on an HA-wing invoice reduces ``ha_sales_revenue``,
    NOT ``diagnostics_revenue``."""
    p = _mk_partner(tok)
    pat = _mk_patient(tok)
    _attach_partner(tok, pat, p["referral_code"])
    ha_svc = _mk_service(tok, price=20000.0, category="Consultation")
    ha_inv = _mk_invoice(tok, pat, ha_svc,
                         unit_price=20000, initial_payment=20000)
    _link_invoice_to_ha_wing_appointment(ha_inv["invoice_id"], pat)
    _refund(tok, ha_inv["invoice_id"], amount=5000)

    s = _get_stats(tok, p["partner_id"])["stats"]

    # NAV-009 paid_total is net-of-refunds → 20000 − 5000 = 15000.
    assert s["ha_sales_revenue"] == 15000.0
    assert s["diagnostics_revenue"] == 0.0


def test_p2c_refund_on_diagnostics_reduces_diagnostics_bucket(tok):
    p = _mk_partner(tok)
    pat = _mk_patient(tok)
    _attach_partner(tok, pat, p["referral_code"])
    svc = _mk_service(tok, price=8000.0, category="Consultation")
    inv = _mk_invoice(tok, pat, svc, unit_price=8000, initial_payment=8000)
    _refund(tok, inv["invoice_id"], amount=2000)

    s = _get_stats(tok, p["partner_id"])["stats"]

    assert s["diagnostics_revenue"] == 6000.0
    assert s["ha_sales_revenue"] == 0.0


# =====================================================================
# 8. Legacy paid_total / refunded_total preserved
# =====================================================================

def test_p2c_missing_paid_total_falls_back_to_grand_total_when_paid(tok):
    """Legacy fully-paid invoices without ``paid_total`` must still
    contribute to the diagnostics bucket via the grand_total
    fallback (matches Phase 2A Bundle-1 behaviour)."""
    p = _mk_partner(tok)
    pat = _mk_patient(tok)
    _attach_partner(tok, pat, p["referral_code"])
    svc = _mk_service(tok, price=6000.0, category="Consultation")
    inv = _mk_invoice(tok, pat, svc, unit_price=6000, initial_payment=6000)
    # Simulate pre-NAV-009 legacy invoice: strip paid_total but keep
    # status='paid' and grand_total=6000.
    _run_mongo(lambda db: db.invoices.update_one(
        {"invoice_id": inv["invoice_id"], "clinic_id": _CLINIC_ID},
        {"$unset": {"paid_total": ""}},
    ))

    s = _get_stats(tok, p["partner_id"])["stats"]

    assert s["diagnostics_revenue"] == 6000.0
    assert s["total_attributed_revenue"] == 6000.0
    # Backward compat: existing `invoice_revenue` field also honoured
    # the legacy fallback.
    assert s["invoice_revenue"] == 6000.0


# =====================================================================
# 9. Diagnostic service linked via appointment.wing='diagnostic'
# =====================================================================

def test_p2c_diagnostic_wing_appointment_stays_in_diagnostics(tok):
    p = _mk_partner(tok)
    pat = _mk_patient(tok)
    _attach_partner(tok, pat, p["referral_code"])
    svc = _mk_service(tok, price=5000.0, category="Consultation")
    inv = _mk_invoice(tok, pat, svc, unit_price=5000, initial_payment=5000)
    # Explicit diagnostic-wing appointment — must NOT tip into HA.
    appt_id = f"APT-P2C-DIAG-{uuid.uuid4().hex[:10].upper()}"
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    _run_mongo(lambda db: db.appointments.insert_one({
        "appointment_id": appt_id, "clinic_id": _CLINIC_ID,
        "branch_id": _BRANCH_ID, "patient_id": pat,
        "wing": "diagnostic", "status": "completed",
        "created_at": now_iso, "updated_at": now_iso,
    }))
    _run_mongo(lambda db: db.invoices.update_one(
        {"invoice_id": inv["invoice_id"], "clinic_id": _CLINIC_ID},
        {"$set": {"appointment_id": appt_id}},
    ))

    s = _get_stats(tok, p["partner_id"])["stats"]

    assert s["diagnostics_revenue"] == 5000.0
    assert s["ha_sales_revenue"] == 0.0


# =====================================================================
# 10. No referral payout — attribution independent of commission_kind
# =====================================================================

def test_p2c_zero_commission_partner_still_shows_attribution(tok):
    """A partner with a 0% commission — attribution fields must
    still be computed. Commission_estimate stays 0."""
    p = _mk_partner(tok, kind="percent", value=0.0)
    pat = _mk_patient(tok)
    _attach_partner(tok, pat, p["referral_code"])
    svc = _mk_service(tok, price=3000.0, category="Consultation")
    _mk_invoice(tok, pat, svc, unit_price=3000, initial_payment=3000)

    s = _get_stats(tok, p["partner_id"])["stats"]

    assert s["diagnostics_revenue"] == 3000.0
    assert s["total_attributed_revenue"] == 3000.0
    # Commission remains discretionary and Phase 2C did not recompute it.
    assert s["commission_estimate"] == 0.0


# =====================================================================
# 11. Fixed referral payout — Phase 2C does NOT recalculate it
# =====================================================================

def test_p2c_fixed_commission_kind_untouched_by_phase2c(tok):
    """Partner with ``commission_kind='fixed', value=₹500`` — the
    commission_estimate must remain 500 × patient_count regardless
    of the new category split."""
    p = _mk_partner(tok, kind="fixed", value=500.0)
    for _ in range(3):
        pat = _mk_patient(tok)
        _attach_partner(tok, pat, p["referral_code"])
        svc = _mk_service(tok, price=2500.0, category="Consultation")
        _mk_invoice(tok, pat, svc, unit_price=2500, initial_payment=2500)

    s = _get_stats(tok, p["partner_id"])["stats"]

    assert s["patients"] == 3
    assert s["commission_estimate"] == 1500.0  # 3 × ₹500
    # Category-aware fields still populated correctly.
    assert s["diagnostics_revenue"] == 7500.0
    assert s["ha_sales_revenue"] == 0.0


# =====================================================================
# 12. Percentage referral payout — Phase 2C does NOT recalculate it
# =====================================================================

def test_p2c_percent_commission_kind_untouched_by_phase2c(tok):
    """Partner with ``commission_kind='percent', value=10`` — the
    commission_estimate must remain 10% of ``total_revenue`` and
    is NOT re-derived from category-split fields."""
    p = _mk_partner(tok, kind="percent", value=10.0)
    pat = _mk_patient(tok)
    _attach_partner(tok, pat, p["referral_code"])
    svc = _mk_service(tok, price=12000.0, category="Consultation")
    _mk_invoice(tok, pat, svc, unit_price=12000, initial_payment=12000)

    s = _get_stats(tok, p["partner_id"])["stats"]

    assert s["diagnostics_revenue"] == 12000.0
    assert s["total_revenue"] == 12000.0
    assert s["commission_estimate"] == 1200.0  # 10% × 12000


# =====================================================================
# 13. Discretionary / zero referral amount — attribution still works
# =====================================================================

def test_p2c_discretionary_partner_zero_fixed_still_gets_attribution(tok):
    """A partner set up as ``fixed, value=0`` (discretionary — clinic
    will decide manually) — the analytics fields still work
    independently of the payout configuration."""
    p = _mk_partner(tok, kind="fixed", value=0.0)
    pat = _mk_patient(tok)
    _attach_partner(tok, pat, p["referral_code"])
    ha_svc = _mk_service(tok, price=15000.0, category="Consultation")
    ha_inv = _mk_invoice(tok, pat, ha_svc,
                         unit_price=15000, initial_payment=15000)
    _link_invoice_to_ha_wing_appointment(ha_inv["invoice_id"], pat)
    diag_svc = _mk_service(tok, price=2000.0, category="Consultation")
    _mk_invoice(tok, pat, diag_svc, unit_price=2000, initial_payment=2000)

    s = _get_stats(tok, p["partner_id"])["stats"]

    assert s["diagnostics_revenue"] == 2000.0
    assert s["ha_sales_revenue"] == 15000.0
    assert s["total_attributed_revenue"] == 17000.0
    # Discretionary partner → zero commission_estimate.
    assert s["commission_estimate"] == 0.0


# =====================================================================
# Backward compatibility guard — Phase 2A response shape preserved
# =====================================================================

def test_p2c_canonical_business_example_10k_diag_100k_ha_discretionary_payout(tok):
    """User's canonical business example, verbatim:

        Dr X refers 10 patients.
        Diagnostics generated: ₹10,000.
        Two patients subsequently purchase Hearing Aids: ₹1,00,000.
        Revenue attribution must show:
          Diagnostics Revenue = ₹10,000
          Hearing Aid / Core Business Revenue = ₹1,00,000
          Total Revenue Generated = ₹1,10,000
        Separately the user may set Referral Amount = ₹12,000 OR ₹0
        OR any other user-designated amount. The ₹12,000 is NOT
        automatically derived from the ₹1,10,000.

    Enforces the semantic distinction end-to-end:
      * Revenue attribution reflects what patients GENERATED.
      * The payout writer's ``commission_estimate`` is a SEPARATE
        quantity governed by ``commission_kind`` / ``commission_value``.
      * The two must remain independent — Phase 2C must NEVER label
        or derive the ₹1,10,000 as the referral amount, and must
        NEVER add a payout-shaped field to the response.
    """
    p = _mk_partner(tok, kind="percent", value=10.0)
    diag_svc = _mk_service(tok, price=1000.0, category="Consultation")
    ha_svc = _mk_service(tok, price=50000.0, category="Consultation")

    ha_patients = []
    for i in range(10):
        pat = _mk_patient(tok)
        _attach_partner(tok, pat, p["referral_code"])
        _mk_invoice(tok, pat, diag_svc, unit_price=1000.0,
                    initial_payment=1000.0)
        if i < 2:
            ha_patients.append(pat)
    for pat in ha_patients:
        ha_inv = _mk_invoice(tok, pat, ha_svc,
                             unit_price=50000.0, initial_payment=50000.0)
        _link_invoice_to_ha_wing_appointment(ha_inv["invoice_id"], pat)

    s = _get_stats(tok, p["partner_id"])["stats"]

    # ── REVENUE ATTRIBUTION (what patients generated) ──
    assert s["diagnostics_revenue"] == 10000.0
    assert s["ha_sales_revenue"] == 100000.0
    assert s["total_attributed_revenue"] == 110000.0
    # ── Payout writer's commission is a SEPARATE quantity ──
    # Phase 2A payout uses `total_revenue` (which for these purely
    # invoice-only referrals equals ₹1,10,000), so 10% = ₹11,000.
    # This value is intentionally different from every attribution
    # figure above.
    assert s["commission_estimate"] == 11000.0
    # ── Semantic distinction guards ──
    assert s["diagnostics_revenue"] != s["commission_estimate"]
    assert s["ha_sales_revenue"] != s["commission_estimate"]
    assert s["total_attributed_revenue"] != s["commission_estimate"]
    # No payout-shaped field must have been introduced by Phase 2C.
    forbidden_field_names = {
        "referral_amount", "amount_payable", "commission_due",
        "doctor_payment", "amount_due_to_partner",
    }
    assert set(s.keys()).isdisjoint(forbidden_field_names), (
        f"Phase 2C must not introduce a payout-shaped field. "
        f"Found: {set(s.keys()) & forbidden_field_names}"
    )



def test_p2c_response_shape_preserves_all_legacy_fields(tok):
    """A single sanity assertion enumerating every field the Phase 2A
    contract advertised. Frontend clients that never learned the new
    fields must continue to see identical keys."""
    p = _mk_partner(tok)
    pat = _mk_patient(tok)
    _attach_partner(tok, pat, p["referral_code"])
    svc = _mk_service(tok, price=1000.0)
    _mk_invoice(tok, pat, svc, unit_price=1000, initial_payment=1000)

    s = _get_stats(tok, p["partner_id"])["stats"]

    legacy_fields = {
        "patients", "invoice_revenue", "ha_sale_revenue",
        "total_revenue", "commission_estimate",
    }
    for f in legacy_fields:
        assert f in s, f"legacy field {f!r} dropped from Phase 2C response"
    # Additive Phase 2C fields.
    p2c_fields = {"diagnostics_revenue", "ha_sales_revenue",
                  "total_attributed_revenue"}
    for f in p2c_fields:
        assert f in s, f"Phase 2C field {f!r} missing"


# =====================================================================
# Scope-boundary guard — Phase 2C did not touch payout writer
# =====================================================================

def test_p2c_payout_creation_still_uses_legacy_total_revenue(tok):
    """Create a payout and confirm ``attributed_revenue`` = the legacy
    ``total_revenue`` value (which includes the pre-existing
    ha_sales-collection add-on). Phase 2C introduces new READ fields
    only — the payout writer's revenue basis is untouched."""
    p = _mk_partner(tok, kind="percent", value=10.0)
    pat = _mk_patient(tok)
    _attach_partner(tok, pat, p["referral_code"])
    svc = _mk_service(tok, price=5000.0)
    _mk_invoice(tok, pat, svc, unit_price=5000, initial_payment=5000)

    r = requests.post(
        f"{API}/referral-partners/{p['partner_id']}/payouts",
        headers=H(tok),
        json={
            "period_start": "2025-01-01", "period_end": "2027-12-31",
            "notes": "P2C scope-boundary check",
        },
        timeout=10,
    )
    assert r.status_code == 200, r.text
    payout = r.json()
    # Attributed revenue on the payout row still corresponds to the
    # legacy `total_revenue` figure (5000.0 here). Phase 2C did not
    # redefine this quantity.
    assert payout["attributed_revenue"] == 5000.0
    assert payout["commission_amount"] == 500.0
    # Sanity: partner_id echoed.
    assert payout["partner_id"] == p["partner_id"]
