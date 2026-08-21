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
  * allocation to a future invoice
  * refund of an advance back to the customer
  * merge/interaction with existing invoices/payments
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


__all__ = [
    "ADVANCE_PAYMENT_METHODS",
    "AdvanceReceiptStatus",
    "AdvanceReceiptCreate",
    "AdvanceVoidIn",
    "AdvanceReceipt",
    "AdvanceAuditEvent",
]
