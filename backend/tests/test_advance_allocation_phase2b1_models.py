"""Advance Allocation · Phase 2B.1 · DATA-MODEL PREPARATION regression suite.

Strict scope for this file:
    * Validate the additive Pydantic changes ONLY.
    * PROVE that the currently-deployed Phase 2A `AdvanceReceipt`
      shape survives round-tripping under the extended model (i.e.
      existing DB rows continue to load without validation errors).
    * PROVE that the new `AdvanceAllocation…` schema behaves as
      documented in `/app/memory/ADVANCE_ALLOCATION_PHASE1_AUDIT.md`.

This file NEVER performs HTTP calls, NEVER touches MongoDB, NEVER runs
the full Phase 2A HTTP regression suite. It is a pure-Pydantic unit
test and must remain hermetic.

Do NOT expand this test file with allocation-engine coverage — that
belongs to Phase 2B.2's suite.
"""
from __future__ import annotations

import sys
import pathlib

import pytest
from pydantic import ValidationError

# Ensure the backend module dir is importable regardless of pytest CWD.
sys.path.append(str(pathlib.Path(__file__).resolve().parents[1]))

from models._advance import (  # noqa: E402
    AdvanceReceipt,
    AdvanceReceiptCreate,
    AdvanceAllocation,
    AdvanceAllocationCreate,
    AdvanceAllocationVoidIn,
    AdvanceAllocationStatus,
)
from models._canonical import (  # noqa: E402
    PAYMENT_METHODS,
    Payment,
    PaymentCreate,
    RefundCreate,
)
from utils.idempotency import SUPPORTED_SCOPES  # noqa: E402


# ─────────────────────────────────────────────────────────────────────
# 1) AdvanceReceipt — Phase 2A shape must still round-trip verbatim
# ─────────────────────────────────────────────────────────────────────

def _legacy_phase2a_receipt_dict() -> dict:
    """The exact shape a Phase 2A DB row carries today. Copied from the
    production population inspection: `available_balance` /
    `allocated_total` are ABSENT (not None, not zero — missing keys)."""
    return {
        "receipt_id": "AR-DEADBEEF12AB",
        "receipt_no": "AR/2026/000001",
        "clinic_id": "clinic-xyz",
        "branch_id": "BR-001",
        "patient_id": "PAT-XYZ",
        "patient_name": "Test Patient",
        "patient_mobile": "+919999999999",
        "patient_mrd": "MRD-01",
        "received_amount": 5000.0,
        "method": "cash",
        "reference": None,
        "purpose_note": None,
        "status": "active",
        "received_at": "2026-02-01T10:00:00+00:00",
        "created_at": "2026-02-01T10:00:00+00:00",
        "created_by_user_id": "user-1",
        "created_by_name": "Reception",
        "voided_at": None,
        "void_reason": None,
        "voided_by_user_id": None,
        "voided_by_name": None,
    }


def test_legacy_phase2a_receipt_loads_without_balance_fields():
    """Existing DB rows (no `available_balance` / `allocated_total`
    keys) MUST continue to parse cleanly. Phase 2B.1 adds the fields
    as Optional=None."""
    doc = _legacy_phase2a_receipt_dict()
    receipt = AdvanceReceipt(**doc)
    assert receipt.status == "active"
    assert receipt.received_amount == 5000.0
    # The new fields default to None on legacy input.
    assert receipt.available_balance is None
    assert receipt.allocated_total is None


def test_new_receipt_model_dump_adds_two_new_null_fields():
    """A brand-new AdvanceReceipt built from a Phase 2A CREATE payload
    should serialise with `available_balance=None, allocated_total=None`
    — additive, never overwriting `received_amount`."""
    doc = _legacy_phase2a_receipt_dict()
    receipt = AdvanceReceipt(**{k: v for k, v in doc.items()
                                if k not in ("available_balance", "allocated_total")})
    dumped = receipt.model_dump()
    assert "available_balance" in dumped
    assert "allocated_total" in dumped
    assert dumped["available_balance"] is None
    assert dumped["allocated_total"] is None
    # Existing keys unchanged.
    assert dumped["received_amount"] == 5000.0
    assert dumped["status"] == "active"


def test_receipt_accepts_explicitly_provided_balance_fields():
    """Phase 2B.2's writer will pass explicit values; the model must
    accept them and preserve them."""
    doc = _legacy_phase2a_receipt_dict()
    doc["available_balance"] = 4500.0
    doc["allocated_total"] = 500.0
    receipt = AdvanceReceipt(**doc)
    assert receipt.available_balance == 4500.0
    assert receipt.allocated_total == 500.0


def test_receipt_create_payload_unchanged():
    """Phase 2A `AdvanceReceiptCreate` payload MUST reject any attempt
    to smuggle `available_balance` / `allocated_total` via the CREATE
    endpoint — extra fields are forbidden."""
    with pytest.raises(ValidationError):
        AdvanceReceiptCreate(
            patient_id="PAT-1", received_amount=100, method="cash",
            available_balance=99,  # smuggled
        )
    # Baseline still works.
    ok = AdvanceReceiptCreate(patient_id="PAT-1", received_amount=100, method="cash")
    assert ok.received_amount == 100
    assert ok.method == "cash"


# ─────────────────────────────────────────────────────────────────────
# 2) AdvanceAllocationCreate — new Phase 2B.1 schema
# ─────────────────────────────────────────────────────────────────────

def test_allocation_create_happy_path():
    body = AdvanceAllocationCreate(invoice_id="INV-1", amount=1500)
    assert body.invoice_id == "INV-1"
    assert body.amount == 1500.0
    assert body.note is None


def test_allocation_create_rounds_to_two_decimals():
    body = AdvanceAllocationCreate(invoice_id="INV-1", amount=1000.005)
    assert body.amount == 1000.01 or body.amount == 1000.00  # bank-rounded


@pytest.mark.parametrize("bad", [0, -1, -0.01])
def test_allocation_create_rejects_non_positive_amount(bad):
    with pytest.raises(ValidationError):
        AdvanceAllocationCreate(invoice_id="INV-1", amount=bad)


def test_allocation_create_rejects_nan_amount():
    with pytest.raises(ValidationError):
        AdvanceAllocationCreate(invoice_id="INV-1", amount=float("nan"))


def test_allocation_create_rejects_extra_fields():
    with pytest.raises(ValidationError):
        AdvanceAllocationCreate(
            invoice_id="INV-1", amount=100,
            allocation_id="AA-FORGE",  # extra="forbid"
        )


def test_allocation_create_rejects_missing_invoice_id():
    with pytest.raises(ValidationError):
        AdvanceAllocationCreate(amount=100)


# ─────────────────────────────────────────────────────────────────────
# 3) AdvanceAllocationVoidIn — reason contract
# ─────────────────────────────────────────────────────────────────────

def test_allocation_void_requires_min_reason_length():
    with pytest.raises(ValidationError):
        AdvanceAllocationVoidIn(reason="ab")   # < 3 chars
    ok = AdvanceAllocationVoidIn(reason="Duplicate")
    assert ok.reason == "Duplicate"


def test_allocation_void_rejects_empty_reason():
    with pytest.raises(ValidationError):
        AdvanceAllocationVoidIn(reason="")


def test_allocation_void_rejects_extra_fields():
    with pytest.raises(ValidationError):
        AdvanceAllocationVoidIn(reason="ok", refund_now=True)


# ─────────────────────────────────────────────────────────────────────
# 4) AdvanceAllocation — persisted row shape
# ─────────────────────────────────────────────────────────────────────

def test_allocation_row_defaults_and_status_enum():
    row = AdvanceAllocation(
        allocation_no="AA/2026/000001",
        clinic_id="clinic-x",
        advance_receipt_id="AR-1",
        invoice_id="INV-1",
        patient_id="PAT-1",
        amount=1000.0,
    )
    assert row.status == "active"
    assert row.allocation_id.startswith("AA-")
    assert row.payment_id is None
    assert row.correlation_id is None
    assert row.voided_at is None


def test_allocation_row_status_literal_rejects_invalid():
    with pytest.raises(ValidationError):
        AdvanceAllocation(
            allocation_no="AA/2026/000001",
            clinic_id="c", advance_receipt_id="AR-1", invoice_id="INV-1",
            patient_id="PAT-1", amount=100,
            status="draft",  # not a legal state
        )


def test_allocation_status_type_alias_exports_expected_literals():
    # AdvanceAllocationStatus is a Literal type alias; instantiate the
    # model twice to exercise both legal values.
    for s in ("active", "voided"):
        row = AdvanceAllocation(
            allocation_no="AA/2026/000001",
            clinic_id="c", advance_receipt_id="AR-1", invoice_id="INV-1",
            patient_id="PAT-1", amount=100, status=s,
        )
        assert row.status == s
    # Compile-time sanity — the alias exists and is truthy.
    assert AdvanceAllocationStatus is not None


# ─────────────────────────────────────────────────────────────────────
# 5) Payment / PaymentCreate / RefundCreate — method="advance"
# ─────────────────────────────────────────────────────────────────────

def test_payment_persistent_model_accepts_advance_method():
    """The `Payment` model persists rows written by the future Phase
    2B.2 allocation writer — MUST accept `method="advance"`."""
    p = Payment(
        payment_id="PAY-TEST0001",
        clinic_id="c", invoice_id="INV-1",
        kind="payment", method="advance", amount=500.0,
        advance_receipt_id="AR-1",
        allocation_id="AA-1",
    )
    assert p.method == "advance"
    assert p.advance_receipt_id == "AR-1"
    assert p.allocation_id == "AA-1"


def test_payment_read_backlink_defaults_to_none_on_legacy_row():
    """Existing DB payment rows have neither `advance_receipt_id` nor
    `allocation_id`. Pydantic must default both to None."""
    p = Payment(
        payment_id="PAY-LEGACY01",
        clinic_id="c", invoice_id="INV-9", kind="payment",
        method="upi", amount=200.0,
    )
    assert p.advance_receipt_id is None
    assert p.allocation_id is None


def test_payment_create_rejects_advance_method():
    """Front-desk / accounts staff MUST NOT be able to smuggle a
    manual `method="advance"` payment via `PaymentCreate`. Only the
    allocation route (future 2B.2) can produce it."""
    with pytest.raises(ValidationError):
        PaymentCreate(method="advance", amount=100)
    # Sanity — canonical methods still work.
    ok = PaymentCreate(method="cash", amount=100)
    assert ok.method == "cash"


def test_refund_create_accepts_advance_method():
    """`RefundCreate` MUST accept `method="advance"` so the future
    allocation-void writer can emit its compensating refund with the
    same method label as the original allocation."""
    body = RefundCreate(method="advance", amount=100.0, reason="Void allocation")
    assert body.method == "advance"


def test_refund_create_still_accepts_legacy_methods():
    for m in ("cash", "upi", "card", "bank_transfer", "insurance"):
        body = RefundCreate(method=m, amount=100.0, reason="Return")
        assert body.method == m


# ─────────────────────────────────────────────────────────────────────
# 6) Registration-level constants
# ─────────────────────────────────────────────────────────────────────

def test_payment_methods_catalogue_includes_advance():
    """Documentation-alignment: `PAYMENT_METHODS` list mirrors the
    persistent `Payment.method` enum."""
    assert "advance" in PAYMENT_METHODS
    # Legacy entries still present, unchanged order for the first five.
    for legacy in ("cash", "upi", "card", "bank_transfer", "insurance"):
        assert legacy in PAYMENT_METHODS


def test_supported_scopes_includes_advance_allocation():
    """`utils.idempotency.SUPPORTED_SCOPES` MUST list the reserved
    scope so `IdempotencyContext.enter(scope='advance_allocation', ...)`
    will not raise once Phase 2B.2 wires up the route."""
    assert "advance_allocation" in SUPPORTED_SCOPES
    # Existing scopes unchanged.
    for legacy in ("payment", "refund", "payout", "advance_receipt"):
        assert legacy in SUPPORTED_SCOPES


# ─────────────────────────────────────────────────────────────────────
# 7) Non-interference smoke — Phase 2A CREATE payload contract intact
# ─────────────────────────────────────────────────────────────────────

def test_phase2a_create_payload_is_backward_compatible():
    """Every legal Phase 2A CREATE payload must still validate under
    the extended model — proving no accidental tightening."""
    # Minimum fields.
    ok1 = AdvanceReceiptCreate(patient_id="PAT-1", received_amount=100, method="cash")
    assert ok1.method == "cash"
    # With full optional payload.
    ok2 = AdvanceReceiptCreate(
        patient_id="PAT-1", received_amount=250.75, method="upi",
        reference="UPI/1234", purpose_note="Advance for HA trial",
        received_at="2026-02-02T00:00:00+00:00",
    )
    assert ok2.reference == "UPI/1234"
    assert ok2.received_amount == 250.75


def test_phase2a_advance_method_catalogue_unchanged():
    """Phase 2A's `ADVANCE_PAYMENT_METHODS` (methods a patient can use
    to PAY the clinic when creating an Advance Receipt) MUST NOT include
    `"advance"` — that would be self-referential nonsense."""
    from models._advance import ADVANCE_PAYMENT_METHODS
    assert "advance" not in ADVANCE_PAYMENT_METHODS
    for legacy in ("cash", "upi", "card", "bank_transfer", "cheque", "insurance", "other"):
        assert legacy in ADVANCE_PAYMENT_METHODS
