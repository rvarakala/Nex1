"""Ear Mould Orders — lightweight "book-and-forget" workflow.

Feb 2026 (quick-book variant) — the audiologist just needs to capture:
  patient · L / R / Both · material · vent · colour · expected delivery
  · advance amount (may be 0) · total · notes
… and get a proper invoice (PARTIAL / PAID / UNPAID) attached to the
patient's ledger. Balance-due follow-ups reuse the existing
`POST /api/billing/invoices/{id}/payment` endpoint — no new payment
plumbing needed. Order state transitions along a simple ribbon:

    pending_impression → sent_to_lab → arrived → delivered → cancelled

Status is a soft workflow marker; money is authoritative on the invoice.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import get_current_user, require_roles, user_can_see_branch
from database import get_db
from utils.serde import deserialize_datetime

router = APIRouter(prefix="/api/ha", tags=["ha-ear-moulds"])


# ── Models ────────────────────────────────────────────────────────────
Side = Literal["left", "right", "both"]
EMStatus = Literal[
    "pending_impression", "sent_to_lab", "arrived", "delivered", "cancelled",
]


class EarMouldOrderCreate(BaseModel):
    patient_id: str
    side: Side
    material: str = "silicone"
    # When side == "both", `vent_size_left` and `vent_size_right` are the
    # authoritative fields; audiologists often prescribe different vents
    # per ear (e.g. 1.5mm on the left, IROS on the right).
    # When side == "left" or "right", the single `vent_size` field is
    # used (legacy + simpler UX). All three are optional so the caller
    # can send whichever subset is relevant.
    vent_size: Optional[str] = None
    vent_size_left: Optional[str] = None
    vent_size_right: Optional[str] = None
    colour: Optional[str] = None
    lab_vendor: Optional[str] = None
    expected_delivery_date: Optional[str] = None    # YYYY-MM-DD
    total_amount: float = Field(ge=0)
    advance_amount: float = Field(0, ge=0)          # 0 = book without deposit
    payment_mode: str = "cash"                      # cash|upi|card|bank|other
    gst_rate: float = 18                            # matches HA convention
    notes: Optional[str] = None
    branch_id: Optional[str] = None                 # source branch (for multi-branch clinics)


class EarMouldStatusIn(BaseModel):
    status: EMStatus
    note: Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────
def _new_order_no(clinic_id: str) -> str:
    """`EM/2026/000123` style running number. Short-enough for a printed
    tag, unique-enough for a small clinic without a sequence server."""
    year = datetime.now(timezone.utc).year
    return f"EM/{year}/{uuid.uuid4().hex[:6].upper()}"


def _new_invoice_no(clinic_id: str) -> str:
    """DEPRECATED · NAV-008 · Kept as an inert shim for any legacy caller.
    All new callers must import `billing._next_invoice_no` and pass the
    live DB. This local generator was the root of NAV008-INV-001 —
    format-namespace collision with the atomic counter.
    """
    raise RuntimeError(
        "ha_ear_moulds._new_invoice_no is retired (NAV-008). "
        "Use billing._next_invoice_no(db, clinic_id) instead."
    )


# NAV-008 · Ear-mould-order invoice numbering must go through the
# canonical atomic counter. Import + retry helper.
from billing import _next_invoice_no, _insert_invoice_with_retry  # noqa: E402


# ── Endpoints ─────────────────────────────────────────────────────────
@router.post("/ear-moulds")
async def create_ear_mould_order(
    payload: EarMouldOrderCreate,
    user=Depends(require_roles(
        "front_desk", "audiologist", "clinic_owner", "accounts", "super_admin",
    )),
    db=Depends(get_db),
):
    """Create an ear mould order + a linked partial-payable invoice in
    ONE call. Advance amount (may be 0) lands as the first payment on
    the invoice; balance shows up in the patient's Payments tab and can
    be topped up via the standard billing payment endpoint."""
    patient = await db.patients.find_one(
        {"patient_id": payload.patient_id, "clinic_id": user["clinic_id"]},
        {"_id": 0, "patient_id": 1, "name": 1, "mobile": 1, "branch_id": 1},
    )
    if not patient:
        raise HTTPException(404, "Patient not found in this clinic")

    branch_id = payload.branch_id or patient.get("branch_id") or user.get("branch_ids", [None])[0]
    if branch_id and not user_can_see_branch(user, branch_id):
        raise HTTPException(403, "Branch access denied")

    if payload.advance_amount > payload.total_amount + 0.5:
        raise HTTPException(400, "Advance cannot exceed total")

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    order_id = f"EMO-{uuid.uuid4().hex[:10].upper()}"
    order_no = _new_order_no(user["clinic_id"])

    # ── Invoice (reuses the shared invoices collection) ──
    # Money math mirrors HA Quick Sale: total is GST-inclusive, so we
    # extract the taxable value and split GST 50/50 CGST + SGST.
    total = round(float(payload.total_amount), 2)
    gst_rate = float(payload.gst_rate or 0)
    taxable = round(total / (1 + gst_rate / 100.0), 2) if gst_rate else total
    tax_total = round(total - taxable, 2)
    cgst = round(tax_total / 2, 2)
    sgst = round(tax_total - cgst, 2)
    invoice_id = f"INV-{uuid.uuid4().hex[:10].upper()}"
    invoice_no = await _next_invoice_no(db, user["clinic_id"])

    paid = round(float(payload.advance_amount), 2)
    balance = round(total - paid, 2)
    # Invoice model's `status` Literal only accepts draft/paid/partial/…
    # No advance → "draft"; some advance → "partial"; full → "paid".
    inv_status = ("paid" if balance <= 0
                  else ("partial" if paid > 0 else "draft"))

    # Build a vent descriptor that renders correctly for one-ear (single
    # value) or both-ears (per-ear values) orders.
    vent_desc = None
    if payload.side == "both" and (payload.vent_size_left or payload.vent_size_right):
        parts = []
        if payload.vent_size_left:  parts.append(f"L {payload.vent_size_left}")
        if payload.vent_size_right: parts.append(f"R {payload.vent_size_right}")
        vent_desc = " · ".join(parts)
    elif payload.vent_size:
        vent_desc = payload.vent_size

    line_desc = (
        f"Custom Ear Mould — {payload.side.title()} · "
        f"{payload.material.title()}"
        + (f" · Vent {vent_desc}" if vent_desc else "")
        + (f" · Colour {payload.colour}" if payload.colour else "")
        + (f" · Lab: {payload.lab_vendor}" if payload.lab_vendor else "")
        + (f". Expected delivery {payload.expected_delivery_date}"
           if payload.expected_delivery_date else "")
    )

    invoice_doc = {
        "invoice_id": invoice_id,
        "invoice_no": invoice_no,
        "clinic_id": user["clinic_id"],
        "branch_id": branch_id,
        "patient_id": patient["patient_id"],
        "patient_name": patient.get("name"),
        "patient_mobile": patient.get("mobile"),
        "invoice_date": now,
        "due_date": None,
        "status": inv_status,
        "lines": [{
            "line_id": f"LN-{uuid.uuid4().hex[:8].upper()}",
            "description": line_desc,
            "qty": 1,
            "unit_price": total,
            "discount_amount": 0.0,
            "taxable_value": taxable,
            "gst_rate": gst_rate,
            "cgst_rate": gst_rate / 2 if gst_rate else 0,
            "sgst_rate": gst_rate / 2 if gst_rate else 0,
            "igst_rate": 0,
            "cgst_amount": cgst,
            "sgst_amount": sgst,
            "igst_amount": 0,
            "line_total": total,
        }],
        "subtotal": total,
        "discount_total": 0.0,
        "tax_total": tax_total,
        "grand_total": total,
        "rounded_total": total,
        "paid_total": paid,
        "due_total": balance,
        "payments": [] if paid == 0 else [{
            "payment_id": f"PMT-{uuid.uuid4().hex[:8].upper()}",
            "amount": paid,
            "method": payload.payment_mode,
            "paid_at": now,
            "reference": None,
            "kind": "payment",
            "received_by_user_id": user["user_id"],
            "notes": "Advance on ear mould booking",
        }],
        "notes": (
            f"Ear Mould Order {order_no}. "
            + (payload.notes or "")
            + " · Sent for fabrication."
        ).strip(),
        "created_at": now,
        "created_by_user_id": user["user_id"],
    }
    await _insert_invoice_with_retry(db, invoice_doc, user["clinic_id"])
    invoice_no = invoice_doc["invoice_no"]

    # NAV-009 · PAY-001 — mirror the initial-advance embedded payment
    # (if any) into `db.payments` so revenue KPIs no longer miss it.
    from billing import mirror_embedded_payments_to_top_level
    await mirror_embedded_payments_to_top_level(
        db, invoice_doc, actor_context=f"ha_ear_moulds.create/{order_id}",
    )

    # ── Order doc (soft workflow tracker) ──
    order_doc = {
        "order_id": order_id,
        "order_no": order_no,
        "clinic_id": user["clinic_id"],
        "branch_id": branch_id,
        "patient_id": patient["patient_id"],
        "patient_name": patient.get("name"),
        "patient_mobile": patient.get("mobile"),
        "side": payload.side,
        "material": payload.material,
        "vent_size": payload.vent_size,
        "vent_size_left": payload.vent_size_left,
        "vent_size_right": payload.vent_size_right,
        "colour": payload.colour,
        "lab_vendor": payload.lab_vendor,
        "expected_delivery_date": payload.expected_delivery_date,
        # Freshly booked orders default to "sent_to_lab" if a lab was named,
        # otherwise the impression is still pending on the audiologist.
        "status": "sent_to_lab" if payload.lab_vendor else "pending_impression",
        "history": [{
            "at": now_iso,
            "status": "booked",
            "actor_user_id": user["user_id"],
            "note": "Order booked via quick-book flow",
        }],
        "invoice_id": invoice_id,
        "invoice_no": invoice_no,
        "total_amount": total,
        "advance_amount": paid,
        "balance_due": balance,
        "notes": payload.notes,
        "created_at": now,
        "created_by_user_id": user["user_id"],
        "updated_at": now_iso,
    }
    await db.ear_mould_orders.insert_one(order_doc)

    return deserialize_datetime({k: v for k, v in order_doc.items() if k != "_id"})


@router.get("/ear-moulds")
async def list_ear_mould_orders(
    status: Optional[EMStatus] = None,
    patient_id: Optional[str] = None,
    limit: int = 100,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Newest-first list. Front-desk filters by status ('sent_to_lab',
    'arrived') for chase-and-collect workflows. Patient profile page
    passes `patient_id` to show a patient's ear-mould history."""
    q: dict = {"clinic_id": user["clinic_id"]}
    if status:
        q["status"] = status
    if patient_id:
        q["patient_id"] = patient_id
    branch_ids = user.get("branch_ids") or []
    if branch_ids and user.get("role") != "super_admin":
        q["$or"] = [
            {"branch_id": {"$in": branch_ids}},
            {"branch_id": {"$in": [None]}},  # legacy rows without a branch
        ]
    rows = await db.ear_mould_orders.find(q, {"_id": 0}).sort("created_at", -1).to_list(limit)
    return [deserialize_datetime(r) for r in rows]


@router.patch("/ear-moulds/{order_id}/status")
async def update_ear_mould_status(
    order_id: str,
    payload: EarMouldStatusIn,
    user=Depends(require_roles(
        "front_desk", "audiologist", "clinic_owner", "accounts", "super_admin",
    )),
    db=Depends(get_db),
):
    """Move an order along the ribbon. History log is append-only so
    later disputes can be reconstructed. Doesn't touch money — payments
    are captured via the invoice's `POST /invoices/{id}/payment` endpoint."""
    order = await db.ear_mould_orders.find_one(
        {"order_id": order_id, "clinic_id": user["clinic_id"]}, {"_id": 0},
    )
    if not order:
        raise HTTPException(404, "Ear mould order not found")

    now_iso = datetime.now(timezone.utc).isoformat()
    history_entry = {
        "at": now_iso,
        "status": payload.status,
        "actor_user_id": user["user_id"],
        "note": payload.note or None,
    }
    await db.ear_mould_orders.update_one(
        {"order_id": order_id, "clinic_id": user["clinic_id"]},
        {"$set": {"status": payload.status, "updated_at": now_iso},
         "$push": {"history": history_entry}},
    )
    updated = await db.ear_mould_orders.find_one(
        {"order_id": order_id}, {"_id": 0},
    )
    return deserialize_datetime(updated)
