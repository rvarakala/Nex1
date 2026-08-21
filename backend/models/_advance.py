"""Advance Receipt (Phase 2A · Receipt-only) — Pydantic models.

Urgent client requirement: accept money from a patient BEFORE a final
product (Hearing Aid / Accessory) is decided or invoiced. Produces a
formal *Advance Receipt / Payment Acknowledgement*.

Strict boundary: an Advance Receipt is **isolated** from every other
financial artefact. It does NOT create an invoice, does NOT touch
`invoices` / `payments` / `serial_items` / `accessory_stock`, does NOT
recognise revenue, and does NOT compute or emit GST. It carries a plain
`received_amount` (money in hand) and no tax breakup.

Phase 2A endpoints (this file):
  * `POST /api/advance-receipts`          — create (mandatory Idempotency-Key)
  * `GET  /api/advance-receipts`          — list (clinic-scoped)
  * `GET  /api/advance-receipts/{id}`     — read single
  * `POST /api/advance-receipts/{id}/void`— void (clinic_owner / accounts)
  * `GET  /api/advance-receipts/{id}/receipt.pdf` — printable acknowledgement

Phase 2B/2C/2D (NOT in this file — DO NOT implement here):
  * allocation to a future invoice          ← engine deferred to Phase 2B.2+
  * refund of an advance back to the customer
  * merge/interaction with existing invoices/payments

Phase 2B.1 (this commit — DATA MODEL PREPARATION ONLY):
  * Nullable `available_balance` / `allocated_total` fields on
    `AdvanceReceipt` — populated by Phase 2B.2's allocation writer
    and by Phase 2B's controlled backfill. Left `None` for now on
    every historical row; the Phase 2A router MUST continue to ignore
    them (no runtime behaviour change).
  * `AdvanceAllocation` / `AdvanceAllocationCreate` /
    `AdvanceAllocationVoidIn` schema classes for the future
    `db.advance_allocations` ledger collection. NO router uses them
    yet.

Proposed indexes (Phase 2B.2+ — NOT created here):
  * `advance_allocations`:
      - unique  (clinic_id, allocation_id)
      - unique  (clinic_id, allocation_no)
      -         (clinic_id, advance_receipt_id, status)
      -         (clinic_id, invoice_id)
  * `payments` (additive, partial):
      -         (clinic_id, advance_receipt_id) — partial where field exists
      -         (clinic_id, allocation_id)      — partial where field exists
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


AdvanceReceiptStatus = Literal["active", "voided"]

# Same catalogue as billing so the front desk uses one consistent picker.
ADVANCE_PAYMENT_METHODS = (
    "cash", "upi", "card", "bank_transfer", "cheque", "insurance", "other",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AdvanceReceiptCreate(BaseModel):
    """POST /api/advance-receipts body.

    `received_amount` MUST be > 0 (rupees, 2-decimal precision). No GST
    fields are accepted or computed on Phase 2A — an Advance Receipt is
    an acknowledgement of money in hand, not a taxable supply.
    """
    model_config = ConfigDict(extra="forbid")

    patient_id: str = Field(..., min_length=1, max_length=64)
    received_amount: float = Field(..., gt=0)
    method: str = Field(..., min_length=1, max_length=32)
    reference: Optional[str] = Field(default=None, max_length=128)
    purpose_note: Optional[str] = Field(
        default=None, max_length=500,
        description=(
            "Free-text purpose (e.g. 'Advance for hearing-aid trial')."
            " Purely informational — does NOT bind the money to any product."
        ),
    )
    received_at: Optional[str] = Field(
        default=None,
        description=(
            "Optional ISO-8601 datetime when the money was actually received."
            " Defaults to now(UTC). Backdate is allowed for cash walk-ins."
        ),
    )

    @field_validator("method")
    @classmethod
    def _method_in_catalogue(cls, v: str) -> str:
        v_norm = (v or "").strip().lower()
        if v_norm not in ADVANCE_PAYMENT_METHODS:
            raise ValueError(
                f"method must be one of {ADVANCE_PAYMENT_METHODS}"
            )
        return v_norm

    @field_validator("received_amount")
    @classmethod
    def _amount_positive(cls, v: float) -> float:
        # Pydantic's gt=0 already rejects 0 / negatives, but we
        # explicitly reject NaN + round to 2 decimals so the ledger
        # never stores 1234.567890123.
        if v != v:  # NaN check
            raise ValueError("received_amount must be a real number > 0")
        return round(float(v), 2)


class AdvanceVoidIn(BaseModel):
    """POST /api/advance-receipts/{id}/void body."""
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(..., min_length=3, max_length=500)


class AdvanceReceipt(BaseModel):
    """Persisted Advance Receipt row (`db.advance_receipts`)."""
    model_config = ConfigDict(extra="ignore")

    receipt_id: str = Field(default_factory=lambda: f"AR-{uuid4().hex[:12].upper()}")
    receipt_no: str  # AR/YYYY/NNNNNN — assigned server-side via next_number()
    clinic_id: str
    branch_id: Optional[str] = None

    patient_id: str
    patient_name: Optional[str] = None
    patient_mobile: Optional[str] = None
    patient_mrd: Optional[str] = None

    received_amount: float
    method: str
    reference: Optional[str] = None
    purpose_note: Optional[str] = None

    status: AdvanceReceiptStatus = "active"

    # ── Phase 2B.1 · Balance ledger fields (Optional, default None) ──
    # `available_balance` and `allocated_total` are added as the schema
    # anchor for Phase 2B.2's allocation writer. They are DELIBERATELY
    # nullable with default `None` so that:
    #   1. No backfill runs in Phase 2B.1 — every existing DB row keeps
    #      its exact current shape until Phase 2B.2 initialises them.
    #   2. The Phase 2A router does not read or emit them, preserving
    #      100 % of the currently-deployed API contract.
    #   3. Phase 2B.2 can choose eager (init on create + backfill) OR
    #      lazy (init on first allocation) semantics without breaking
    #      this model.
    # Invariant target (enforced only in Phase 2B.2+):
    #     available_balance = received_amount − allocated_total
    #     available_balance ≥ 0
    available_balance: Optional[float] = None
    allocated_total: Optional[float] = None

    received_at: str = Field(default_factory=_now_iso)
    created_at: str = Field(default_factory=_now_iso)
    created_by_user_id: Optional[str] = None
    created_by_name: Optional[str] = None

    voided_at: Optional[str] = None
    void_reason: Optional[str] = None
    voided_by_user_id: Optional[str] = None
    voided_by_name: Optional[str] = None


class AdvanceAuditEvent(BaseModel):
    """Append-only audit trail (`db.advance_audit_events`)."""
    model_config = ConfigDict(extra="ignore")

    event_id: str = Field(default_factory=lambda: f"AAE-{uuid4().hex[:12].upper()}")
    clinic_id: str
    receipt_id: str
    receipt_no: Optional[str] = None
    kind: Literal["created", "voided"]
    at: str = Field(default_factory=_now_iso)
    actor_user_id: Optional[str] = None
    actor_name: Optional[str] = None
    actor_role: Optional[str] = None
    payload: Optional[dict] = None


# ═══════════════════════════════════════════════════════════════════════
# PHASE 2B.1 · Advance Allocation Ledger — SCHEMA ONLY
# ═══════════════════════════════════════════════════════════════════════
# These classes describe the future `db.advance_allocations` collection
# that will land in Phase 2B.2. **No router uses them yet.** They are
# defined here so:
#   * downstream code can `from models._advance import AdvanceAllocation`
#     without pulling schema changes across another commit,
#   * pytest can validate the shape ahead of the allocation writer's
#     first real usage,
#   * anybody reading the codebase understands the target data model
#     ahead of the implementation sprint.
#
# See `/app/memory/ADVANCE_ALLOCATION_PHASE1_AUDIT.md` §4 & §5 for the
# state-machine + concurrency rationale.

AdvanceAllocationStatus = Literal["active", "voided"]


class AdvanceAllocationCreate(BaseModel):
    """POST body for the (future) Phase 2B.2 allocation endpoint.

    * `amount` MUST be > 0 (rupees, 2-decimal precision).
    * `invoice_id` MUST belong to the same clinic AND the same patient
      as the source Advance Receipt (enforced by the router, not here).
    """
    model_config = ConfigDict(extra="forbid")

    invoice_id: str = Field(..., min_length=1, max_length=64)
    amount: float = Field(..., gt=0)
    note: Optional[str] = Field(default=None, max_length=500)

    @field_validator("amount")
    @classmethod
    def _amount_positive(cls, v: float) -> float:
        if v != v:  # NaN check
            raise ValueError("amount must be a real number > 0")
        return round(float(v), 2)


class AdvanceAllocationVoidIn(BaseModel):
    """POST body for the (future) Phase 2B.2 void-allocation endpoint."""
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(..., min_length=3, max_length=500)


class AdvanceAllocation(BaseModel):
    """Persisted allocation row (`db.advance_allocations`) — the source
    of truth for how an Advance Receipt has been consumed against real
    invoices.

    Money-story invariants (enforced only by the Phase 2B.2 writer):
      * `SUM(amount WHERE status='active')` per (clinic_id, advance_receipt_id)
        equals the parent receipt's `allocated_total`.
      * Every `active` row corresponds to exactly one `payments` row
        with `method='advance'`, `allocation_id=<this.allocation_id>`,
        `advance_receipt_id=<this.advance_receipt_id>`.
      * Every `voided` row corresponds to exactly one offsetting refund
        row on the same invoice (`kind='refund'`, negative `amount`,
        matching `allocation_id`).
    """
    model_config = ConfigDict(extra="ignore")

    allocation_id: str = Field(
        default_factory=lambda: f"AA-{uuid4().hex[:12].upper()}"
    )
    # AA/YYYY/NNNNNN — clinic-scoped, year-reset counter. Assigned by the
    # Phase 2B.2 writer via `db.counters` (mirrors AR/YYYY/NNNNNN).
    allocation_no: str

    clinic_id: str
    branch_id: Optional[str] = None

    advance_receipt_id: str
    advance_receipt_no: Optional[str] = None

    invoice_id: str
    invoice_no: Optional[str] = None

    patient_id: str
    amount: float

    status: AdvanceAllocationStatus = "active"

    # Idempotency + FK to the payment row emitted by the allocation
    # writer. Both are Optional here because they are stamped by the
    # writer after CAS wins; the model itself must survive being read
    # back before those fields have been persisted (defensive).
    correlation_id: Optional[str] = None
    idempotency_correlation_id: Optional[str] = None
    payment_id: Optional[str] = None
    note: Optional[str] = None

    created_at: str = Field(default_factory=_now_iso)
    created_by_user_id: Optional[str] = None
    created_by_name: Optional[str] = None

    voided_at: Optional[str] = None
    void_reason: Optional[str] = None
    voided_by_user_id: Optional[str] = None
    voided_by_name: Optional[str] = None
    # FK to the compensating refund row emitted on void.
    void_refund_payment_id: Optional[str] = None


__all__ = [
    "ADVANCE_PAYMENT_METHODS",
    "AdvanceReceiptStatus",
    "AdvanceReceiptCreate",
    "AdvanceVoidIn",
    "AdvanceReceipt",
    "AdvanceAuditEvent",
    # Phase 2B.1 · additive schema (no router usage yet)
    "AdvanceAllocationStatus",
    "AdvanceAllocationCreate",
    "AdvanceAllocationVoidIn",
    "AdvanceAllocation",
]
