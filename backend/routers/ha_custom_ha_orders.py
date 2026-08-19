"""Custom Hearing Aid Orders — bespoke IIC / CIC / ITC / ITE workflow (Feb 2026).

Custom hearing aids are patient-specific: the audiologist takes an
impression, fills a Custom Order Form (per-ear vent, colour, receiver
power, shell type, brand/model, features), and ships that spec to
either:

  · a vendor / manufacturer (Phonak, Signia, Starkey, GN, …), OR
  · another branch (head office / main branch that owns the vendor
    relationship — Phase 2 of Multi-Clinic).

Money math mirrors Ear Moulds:
  · one linked invoice is generated on booking
  · advance may be 0 (booking on credit), partial, or full
  · balance chases the patient via the shared payment endpoint

Status ribbon:
    impression_pending → sent_to_vendor → dispatched
                                        → arrived
                                        → delivered
                                        → cancelled

Fields captured are a **leaner Indian-market subset** of the classic
Starkey/Audibel PDF form — full audiogram + faceplate/receiver detail
are captured as free-form notes / attachments to keep the modal quick.
"""
from __future__ import annotations

import io
import uuid
from datetime import datetime, timezone
from typing import List, Literal, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from motor.motor_asyncio import AsyncIOMotorGridFSBucket
from pydantic import BaseModel, Field

from auth import get_current_user, require_roles, user_can_see_branch
from database import get_db
from utils.serde import deserialize_datetime

router = APIRouter(prefix="/api/ha", tags=["ha-custom-ha-orders"])

# 15 MB is comfortably above a full colour audiogram print (4-6 pages),
# matches the report_handover cap so operators see consistent limits.
_AUDIOGRAM_MAX_BYTES = 15 * 1024 * 1024
_AUDIOGRAM_BUCKET = "custom_ha_audiograms"
# Magic-byte detection: PDF `%PDF`, PNG `\x89PNG`, JPG `\xff\xd8\xff`.
# We keep JPG/PNG allowed because many diagnostic audiometers export a
# printable image rather than a PDF.
_ACCEPTED_MIME = {
    "application/pdf": (b"%PDF", ".pdf"),
    "image/png":       (b"\x89PNG\r\n\x1a\n", ".png"),
    "image/jpeg":      (b"\xff\xd8\xff", ".jpg"),
}


# ── Types ─────────────────────────────────────────────────────────────
Side = Literal["left", "right", "both"]
ShellType = Literal["IIC", "CIC", "ITC", "ITE"]
DeliveryTarget = Literal["vendor", "branch"]
CustomHAStatus = Literal[
    "impression_pending", "awaiting_approval", "sent_to_vendor", "dispatched",
    "arrived", "delivered", "cancelled",
]


class CustomHAOrderCreate(BaseModel):
    patient_id: str
    side: Side
    shell_type: ShellType

    # Per-ear specs — only the ear(s) matching `side` need to be filled.
    vent_size_left: Optional[str] = None
    vent_size_right: Optional[str] = None
    shell_colour_left: Optional[str] = None
    shell_colour_right: Optional[str] = None
    faceplate_colour_left: Optional[str] = None
    faceplate_colour_right: Optional[str] = None
    receiver_power_left: Optional[str] = None       # e.g. 'M', 'P', 'HP', 'SP'
    receiver_power_right: Optional[str] = None

    # Free-text brand + model (per user preference — no dropdown lock-in).
    brand: Optional[str] = None
    model: Optional[str] = None
    warranty_months: int = 24
    features: List[str] = Field(default_factory=list)   # e.g. ["telecoil","push_button","directional"]

    # Delivery target — vendor (from Vendors master) OR another branch.
    delivery_target: DeliveryTarget = "vendor"
    vendor_id: Optional[str] = None
    target_branch_id: Optional[str] = None
    expected_delivery_date: Optional[str] = None   # YYYY-MM-DD

    # Financials.
    total_amount: float = Field(ge=0)
    advance_amount: float = Field(0, ge=0)
    payment_mode: str = "cash"
    gst_rate: float = 18
    notes: Optional[str] = None
    branch_id: Optional[str] = None                # source branch
    # When the audiologist is booking off the back of a completed hearing
    # test, `from_session_id` copies THAT session's audiogram PDF into
    # the Custom HA order at creation time — no separate upload needed.
    # `from_trial_no` (optional) closes the source trial as CONVERTED
    # once the order is booked, so nothing dangles in Trials.
    from_session_id: Optional[str] = None
    from_trial_no: Optional[str] = None


class CustomHAStatusIn(BaseModel):
    status: CustomHAStatus
    note: Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────
def _new_order_no() -> str:
    year = datetime.now(timezone.utc).year
    return f"CHA/{year}/{uuid.uuid4().hex[:6].upper()}"


# NAV-008 · Custom-HA-order invoice numbering must go through the
# canonical atomic counter. The previous local `_new_invoice_no()`
# generator (INV/YYYY/{6-char-hex}) shared its regex namespace with
# the counter's INV/YYYY/{6-digit-decimal} format and was the latent
# collision risk documented in NAV008-INV-001. Import + retry-safe
# insert helper live in `billing.py`.
from billing import _next_invoice_no, _insert_invoice_with_retry  # noqa: E402


def _build_line_desc(payload: CustomHAOrderCreate) -> str:
    """Renders a printable description that survives PDF invoice
    generation. Only includes the ear(s) selected in `side`."""
    bits = [f"Custom {payload.shell_type} — {payload.side.title()}"]
    if payload.brand or payload.model:
        bits.append(f"{payload.brand or ''} {payload.model or ''}".strip())

    def _ear(label: str, vent, shell, faceplate, receiver):
        parts = []
        if vent:      parts.append(f"vent {vent}")
        if shell:     parts.append(f"shell {shell}")
        if faceplate: parts.append(f"faceplate {faceplate}")
        if receiver:  parts.append(f"receiver {receiver}")
        return f"{label}: " + ", ".join(parts) if parts else None

    if payload.side in ("left", "both"):
        line = _ear("L", payload.vent_size_left, payload.shell_colour_left,
                    payload.faceplate_colour_left, payload.receiver_power_left)
        if line: bits.append(line)
    if payload.side in ("right", "both"):
        line = _ear("R", payload.vent_size_right, payload.shell_colour_right,
                    payload.faceplate_colour_right, payload.receiver_power_right)
        if line: bits.append(line)
    if payload.features:
        bits.append("features: " + ", ".join(payload.features))
    if payload.expected_delivery_date:
        bits.append(f"expected {payload.expected_delivery_date}")
    return " · ".join(bits)


# ── Endpoints ─────────────────────────────────────────────────────────
@router.post("/custom-ha-orders")
async def create_custom_ha_order(
    payload: CustomHAOrderCreate,
    user=Depends(require_roles(
        "front_desk", "audiologist", "clinic_owner", "accounts", "super_admin",
    )),
    db=Depends(get_db),
):
    """Book a bespoke IIC/CIC/ITC/ITE order + linked invoice in ONE call.

    - `delivery_target='vendor'` → `vendor_id` must resolve to an active
      vendor in this clinic's Vendors master.
    - `delivery_target='branch'` → `target_branch_id` is the receiving
      branch (typically the head office that owns the vendor
      relationship for the group).
    """
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

    # Resolve delivery target so we can denormalise a display name.
    vendor_name: Optional[str] = None
    target_branch_name: Optional[str] = None
    # If the branch target is routed through a clinic group's approval
    # flow, we keep track of the linked stock_request + target head.
    linked_stock_request_id: Optional[str] = None
    target_clinic_id: Optional[str] = None
    target_clinic_name: Optional[str] = None
    # Order starts sent_to_vendor by default; recomputed below if the
    # request goes through group-head approval.
    initial_status: str = "sent_to_vendor"
    if payload.delivery_target == "vendor":
        if not payload.vendor_id:
            raise HTTPException(400, "vendor_id is required when delivery_target='vendor'")
        vendor = await db.vendors.find_one(
            {"vendor_id": payload.vendor_id, "clinic_id": user["clinic_id"]},
            {"_id": 0, "name": 1},
        )
        if not vendor:
            raise HTTPException(404, "Vendor not found in this clinic")
        vendor_name = vendor.get("name")
    else:  # branch
        # If the clinic is in a multi-clinic group AND is NOT the head,
        # the branch delivery target means "ask the head clinic to
        # fulfil this bespoke order" — auto-spawn a stock_request in the
        # head owner's inbox and hold the order at `awaiting_approval`
        # until head fulfils / declines. If the clinic is standalone or
        # IS the head, fall back to the existing intra-clinic branch
        # dropdown (no approval needed).
        group = await db.clinic_groups.find_one(
            {"$or": [
                {"head_clinic_id": user["clinic_id"]},
                {"member_clinic_ids": user["clinic_id"]},
            ]},
            {"_id": 0},
        )
        viewer_is_head = bool(group and group.get("head_clinic_id") == user["clinic_id"])

        if group and not viewer_is_head:
            # Group-head approval path.
            target_clinic_id = group["head_clinic_id"]
            head = await db.clinics.find_one(
                {"clinic_id": target_clinic_id}, {"_id": 0, "name": 1},
            ) or {}
            target_clinic_name = head.get("name") or target_clinic_id
            initial_status = "awaiting_approval"
        else:
            if not payload.target_branch_id:
                raise HTTPException(400, "target_branch_id is required when delivery_target='branch'")
            if payload.target_branch_id == branch_id:
                raise HTTPException(400, "Requesting branch cannot equal the target branch")
            tb = await db.branches.find_one(
                {"branch_id": payload.target_branch_id, "clinic_id": user["clinic_id"]},
                {"_id": 0, "name": 1},
            )
            if not tb:
                raise HTTPException(404, "Target branch not found in this clinic")
            target_branch_name = tb.get("name")

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    order_id = f"CHA-{uuid.uuid4().hex[:10].upper()}"
    order_no = _new_order_no()

    # ── Invoice (reuses shared collection, math matches HA quick sale) ──
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

    line_desc = _build_line_desc(payload)

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
            "notes": "Advance on custom HA booking",
        }],
        "notes": (
            f"Custom HA Order {order_no}. "
            + (payload.notes or "")
            + f" · Delivery target: {payload.delivery_target}"
            + (f" ({vendor_name})" if vendor_name else "")
            + (f" ({target_branch_name})" if target_branch_name else "")
        ).strip(),
        "created_at": now,
        "created_by_user_id": user["user_id"],
    }
    await _insert_invoice_with_retry(db, invoice_doc, user["clinic_id"])
    # If the retry loop renewed invoice_no, ensure downstream code
    # (order back-link + response) sees the final canonical number.
    invoice_no = invoice_doc["invoice_no"]

    # NAV-009 · PAY-001 — mirror the initial-advance embedded payment
    # (if any) into `db.payments` so revenue KPIs no longer miss it.
    from billing import mirror_embedded_payments_to_top_level
    await mirror_embedded_payments_to_top_level(
        db, invoice_doc, actor_context=f"ha_custom_ha_orders.create/{order_id}",
    )

    # ── Order doc ──
    order_doc = {
        "order_id": order_id,
        "order_no": order_no,
        "clinic_id": user["clinic_id"],
        "branch_id": branch_id,
        "patient_id": patient["patient_id"],
        "patient_name": patient.get("name"),
        "patient_mobile": patient.get("mobile"),
        "side": payload.side,
        "shell_type": payload.shell_type,
        "vent_size_left": payload.vent_size_left,
        "vent_size_right": payload.vent_size_right,
        "shell_colour_left": payload.shell_colour_left,
        "shell_colour_right": payload.shell_colour_right,
        "faceplate_colour_left": payload.faceplate_colour_left,
        "faceplate_colour_right": payload.faceplate_colour_right,
        "receiver_power_left": payload.receiver_power_left,
        "receiver_power_right": payload.receiver_power_right,
        "brand": payload.brand,
        "model": payload.model,
        "warranty_months": payload.warranty_months,
        "features": payload.features,
        "delivery_target": payload.delivery_target,
        "vendor_id": payload.vendor_id,
        "vendor_name": vendor_name,
        "target_branch_id": payload.target_branch_id,
        "target_branch_name": target_branch_name,
        "target_clinic_id": target_clinic_id,
        "target_clinic_name": target_clinic_name,
        "linked_stock_request_id": None,   # filled in below if we create one
        "expected_delivery_date": payload.expected_delivery_date,
        # Freshly booked orders default to "sent_to_vendor" (or "awaiting_approval"
        # if the branch target routes through the head clinic's inbox).
        "status": initial_status,
        "history": [{
            "at": now_iso,
            "status": "booked",
            "actor_user_id": user["user_id"],
            "note": "Order booked via custom HA quick-book flow",
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
    await db.custom_ha_orders.insert_one(order_doc)

    # ── Head-clinic approval path ──
    # When this order needs group-head approval (branch target + non-head
    # requester in a clinic group), spawn a stock_request doc that shows
    # up in the head owner's Stock Requests inbox. Fulfil/decline on that
    # request will transition this order via `_apply_stock_request_decision()`
    # in the stock_requests router.
    if initial_status == "awaiting_approval":
        stock_req_id = f"REQ-{uuid.uuid4().hex[:10].upper()}"
        requesting_clinic = await db.clinics.find_one(
            {"clinic_id": user["clinic_id"]}, {"_id": 0, "name": 1},
        ) or {}
        # Compact one-line label so the inbox column stays readable —
        # full spec sheet is in the linked custom HA order.
        line_bits = [f"Custom {payload.shell_type}", payload.side.title()]
        if payload.brand or payload.model:
            line_bits.append(f"{payload.brand or ''} {payload.model or ''}".strip())
        request_note_bits = [
            f"L vent {payload.vent_size_left}" if payload.vent_size_left else None,
            f"R vent {payload.vent_size_right}" if payload.vent_size_right else None,
            f"L colour {payload.shell_colour_left}" if payload.shell_colour_left else None,
            f"R colour {payload.shell_colour_right}" if payload.shell_colour_right else None,
            f"L receiver {payload.receiver_power_left}" if payload.receiver_power_left else None,
            f"R receiver {payload.receiver_power_right}" if payload.receiver_power_right else None,
            ("features: " + ", ".join(payload.features)) if payload.features else None,
            payload.notes,
        ]
        request_note = " · ".join([b for b in request_note_bits if b])

        stock_request_doc = {
            "request_id": stock_req_id,
            "clinic_id": user["clinic_id"],
            "clinic_name": requesting_clinic.get("name"),
            "group_id": group["group_id"],
            "head_clinic_id": group["head_clinic_id"],
            "requested_by_user_id": user["user_id"],
            "requested_by_role": user.get("role"),
            "lines": [{
                "product_label": " · ".join([b for b in line_bits if b]),
                "kind": "ha",
                "product_id": None,
                "variant": None,
                "qty": 1,
                "notes": request_note or None,
            }],
            "urgency": "normal",
            "reason": f"Custom HA order {order_no} needs head approval",
            "needed_by": payload.expected_delivery_date,
            "status": "pending",
            "fulfilled_by_user_id": None,
            "fulfilled_at": None,
            "fulfilled_from_clinic_id": None,
            "linked_transfer_id": None,
            "decline_reason": None,
            "po_details": None,
            # Back-refs so both directions of the pair can be resolved.
            "linked_custom_ha_order_id": order_id,
            "linked_custom_ha_order_no": order_no,
            # Full spec snapshot — the head owner needs every field the
            # branch filled to actually place the order with the vendor
            # (Phonak, Signia, Starkey etc.). Snapshotting here means
            # the inbox stays self-contained and we don't need any
            # cross-clinic Custom HA fetch endpoint.
            "custom_ha_details": {
                "patient_name": patient.get("name"),
                "patient_mobile": patient.get("mobile"),
                "shell_type": payload.shell_type,
                "side": payload.side,
                "vent_size_left": payload.vent_size_left,
                "vent_size_right": payload.vent_size_right,
                "shell_colour_left": payload.shell_colour_left,
                "shell_colour_right": payload.shell_colour_right,
                "faceplate_colour_left": payload.faceplate_colour_left,
                "faceplate_colour_right": payload.faceplate_colour_right,
                "receiver_power_left": payload.receiver_power_left,
                "receiver_power_right": payload.receiver_power_right,
                "brand": payload.brand,
                "model": payload.model,
                "warranty_months": payload.warranty_months,
                "features": payload.features,
                "expected_delivery_date": payload.expected_delivery_date,
                "total_amount": total,
                "advance_amount": paid,
                "balance_due": balance,
                "gst_rate": gst_rate,
                "payment_mode": payload.payment_mode,
                "invoice_no": invoice_no,
                "notes": payload.notes,
            },
            "created_at": now,
            "updated_at": now,
        }
        await db.stock_requests.insert_one(stock_request_doc)

        # Backfill the order with the linked request id so the Custom HA
        # list can deep-link to the inbox row.
        await db.custom_ha_orders.update_one(
            {"order_id": order_id},
            {"$set": {"linked_stock_request_id": stock_req_id}},
        )
        order_doc["linked_stock_request_id"] = stock_req_id

    # ── Trial-to-Order audiogram pipe ──
    # If the audiologist is booking off the back of a completed hearing
    # test session, copy that session's PDF straight into the Custom HA
    # audiogram bucket — no re-upload needed. If a trial was the trigger,
    # we also close it as CONVERTED so nothing dangles in Trials.
    if payload.from_session_id:
        upd = await _clone_session_audiogram_to_order(
            db, order_doc, payload.from_session_id, user,
        )
        if upd:
            order_doc.update(upd)
            # Mirror onto the linked stock_request so head owner sees the
            # audiogram from the first sight of the inbox row.
            if order_doc.get("linked_stock_request_id"):
                await db.stock_requests.update_one(
                    {"request_id": order_doc["linked_stock_request_id"]},
                    {"$set": {
                        "custom_ha_details.audiogram_fs_id": upd["audiogram_fs_id"],
                        "custom_ha_details.audiogram_content_type": upd["audiogram_content_type"],
                        "custom_ha_details.audiogram_filename": upd["audiogram_filename"],
                    }},
                )
    if payload.from_trial_no:
        # Mirror `mark_trial_converted` without importing the endpoint —
        # we just flip status + hand the demo unit back to Demo Stock.
        trial = await db.ha_trials.find_one(
            {"clinic_id": user["clinic_id"], "trial_no": payload.from_trial_no},
            {"_id": 0},
        )
        if trial and trial.get("status") in {"active", "extended"}:
            trial_upd = {
                "status": "converted",
                "converted_custom_ha_order_id": order_id,
                "converted_custom_ha_order_no": order_no,
                "closed_at": now_iso,
                "updated_at": now_iso,
            }
            await db.ha_trials.update_one(
                {"clinic_id": user["clinic_id"], "trial_no": payload.from_trial_no},
                {"$set": trial_upd},
            )
            # Return demo units to Demo Stock so the next patient can trial.
            try:
                from ha_state_machine import transition_serial   # local import — avoid cycles at boot
                for s in trial.get("serials", []):
                    sid = s.get("serial_id")
                    if not sid:
                        continue
                    cur = await db.serial_items.find_one({"serial_id": sid}, {"_id": 0, "state": 1})
                    if cur and cur["state"] == "TRIAL_OUT":
                        await transition_serial(
                            db, sid, "IN_STOCK",
                            actor_user_id=user["user_id"],
                            ref_doc={"kind": "trial-to-custom-ha", "id": payload.from_trial_no,
                                     "order_no": order_no},
                            note=f"Trial {payload.from_trial_no} converted to Custom HA order {order_no}",
                        )
                        await db.serial_items.update_one(
                            {"serial_id": sid}, {"$set": {"current_patient_id": None}},
                        )
            except Exception:  # noqa: BLE001
                # Best-effort — a serial-transition failure must NOT
                # roll back a successful order booking.
                pass

    return deserialize_datetime({k: v for k, v in order_doc.items() if k != "_id"})


async def _clone_session_audiogram_to_order(
    db, order: dict, session_id: str, user: dict,
) -> Optional[dict]:
    """Copy a patient's captured hearing-test PDF (bucket `session_reports`)
    into the Custom HA audiogram bucket, then stamp the same audiogram
    fields we set on manual upload. Returns the update dict that was
    written to the order (or None if nothing was attached).

    Silent no-op when the session has no PDF yet — audiologist can still
    upload one later from the list row.
    """
    session = await db.test_sessions.find_one(
        {"session_id": session_id,
         "clinic_id": user["clinic_id"],
         "patient_id": order["patient_id"]},
        {"_id": 0, "session_id": 1, "report_pdf_fs_id": 1, "test_date": 1},
    )
    if not session or not session.get("report_pdf_fs_id"):
        return None

    src_bucket = AsyncIOMotorGridFSBucket(db, bucket_name="session_reports")
    try:
        stream = await src_bucket.open_download_stream(
            ObjectId(session["report_pdf_fs_id"])
        )
    except Exception:  # noqa: BLE001
        return None
    raw = await stream.read()
    if not raw or not raw.startswith(b"%PDF"):
        return None

    dst_bucket = AsyncIOMotorGridFSBucket(db, bucket_name=_AUDIOGRAM_BUCKET)
    filename = f"{order['order_id']}.pdf"
    fs_id = await dst_bucket.upload_from_stream(
        filename=filename,
        source=raw,
        metadata={
            "clinic_id": user["clinic_id"],
            "order_id": order["order_id"],
            "patient_id": order["patient_id"],
            "source_session_id": session_id,
            "cloned_at": datetime.now(timezone.utc).isoformat(),
            "size_bytes": len(raw),
            "content_type": "application/pdf",
        },
    )
    now_iso = datetime.now(timezone.utc).isoformat()
    upd = {
        "audiogram_fs_id": str(fs_id),
        "audiogram_content_type": "application/pdf",
        "audiogram_size_bytes": len(raw),
        "audiogram_uploaded_at": now_iso,
        "audiogram_filename": filename,
        "audiogram_source_session_id": session_id,
        "updated_at": now_iso,
    }
    await db.custom_ha_orders.update_one(
        {"order_id": order["order_id"], "clinic_id": user["clinic_id"]},
        {"$set": upd},
    )
    return upd


@router.get("/custom-ha-orders")
async def list_custom_ha_orders(
    status: Optional[CustomHAStatus] = None,
    patient_id: Optional[str] = None,
    limit: int = 100,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    q: dict = {"clinic_id": user["clinic_id"]}
    if status:
        q["status"] = status
    if patient_id:
        q["patient_id"] = patient_id
    branch_ids = user.get("branch_ids") or []
    if branch_ids and user.get("role") != "super_admin":
        q["$or"] = [
            {"branch_id": {"$in": branch_ids}},
            {"branch_id": {"$in": [None]}},
        ]
    rows = await db.custom_ha_orders.find(q, {"_id": 0}).sort("created_at", -1).to_list(limit)
    return [deserialize_datetime(r) for r in rows]


@router.patch("/custom-ha-orders/{order_id}/status")
async def update_custom_ha_status(
    order_id: str,
    payload: CustomHAStatusIn,
    user=Depends(require_roles(
        "front_desk", "audiologist", "clinic_owner", "accounts", "super_admin",
    )),
    db=Depends(get_db),
):
    order = await db.custom_ha_orders.find_one(
        {"order_id": order_id, "clinic_id": user["clinic_id"]}, {"_id": 0},
    )
    if not order:
        raise HTTPException(404, "Custom HA order not found")

    now_iso = datetime.now(timezone.utc).isoformat()
    history_entry = {
        "at": now_iso,
        "status": payload.status,
        "actor_user_id": user["user_id"],
        "note": payload.note or None,
    }
    await db.custom_ha_orders.update_one(
        {"order_id": order_id, "clinic_id": user["clinic_id"]},
        {"$set": {"status": payload.status, "updated_at": now_iso},
         "$push": {"history": history_entry}},
    )
    updated = await db.custom_ha_orders.find_one(
        {"order_id": order_id}, {"_id": 0},
    )
    return deserialize_datetime(updated)



# ── Audiogram attachment ──────────────────────────────────────────────
# Audiologists routinely need to send the patient's audiogram PDF to
# the vendor (Phonak, Signia, Starkey, …) so the manufacturer can pick
# a receiver/gain matrix that fits the loss. Since the head clinic and
# the branch clinic are separate tenants, we store the file once in a
# dedicated GridFS bucket and expose two auth-scoped read endpoints:
#   · order owner (branch) → `GET /custom-ha-orders/{id}/audiogram`
#   · head owner (approver) → `GET /stock-requests/{id}/audiogram`
# The stock_request mirror is populated the moment the audiogram is
# attached, so the head sees a "View Audiogram" button in the inbox.

async def _sniff_content_type(raw: bytes, declared: Optional[str]) -> Optional[str]:
    """Pick a content-type from magic bytes; fall back to the declared
    mime only if it's in the whitelist. Prevents someone renaming a
    .exe to .pdf and slipping past the upload validator."""
    for mime, (magic, _ext) in _ACCEPTED_MIME.items():
        if raw.startswith(magic):
            return mime
    if declared in _ACCEPTED_MIME:
        return declared
    return None


@router.post("/custom-ha-orders/{order_id}/audiogram")
async def attach_audiogram(
    order_id: str,
    file: UploadFile = File(...),
    user=Depends(require_roles(
        "front_desk", "audiologist", "clinic_owner", "accounts", "super_admin",
    )),
    db=Depends(get_db),
):
    order = await db.custom_ha_orders.find_one(
        {"order_id": order_id, "clinic_id": user["clinic_id"]}, {"_id": 0},
    )
    if not order:
        raise HTTPException(404, "Custom HA order not found")

    raw = await file.read()
    if not raw:
        raise HTTPException(400, "Empty file")
    if len(raw) > _AUDIOGRAM_MAX_BYTES:
        raise HTTPException(413, "Audiogram file too large (max 15 MB)")
    content_type = await _sniff_content_type(raw, (file.content_type or "").lower())
    if not content_type:
        raise HTTPException(415, "Only PDF, PNG or JPG audiograms are accepted")

    bucket = AsyncIOMotorGridFSBucket(db, bucket_name=_AUDIOGRAM_BUCKET)
    # Idempotent: overwrite previous audiogram if the audiologist
    # re-uploads a fresh test result. Old GridFS blob is deleted so we
    # don't accumulate orphans over time.
    old_id = order.get("audiogram_fs_id")
    if old_id:
        try:
            await bucket.delete(ObjectId(old_id))
        except Exception:  # noqa: BLE001
            pass

    fs_id = await bucket.upload_from_stream(
        filename=f"{order_id}{_ACCEPTED_MIME[content_type][1]}",
        source=raw,
        metadata={
            "clinic_id": user["clinic_id"],
            "order_id": order_id,
            "patient_id": order.get("patient_id"),
            "uploaded_by_user_id": user["user_id"],
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "content_type": content_type,
            "size_bytes": len(raw),
        },
    )
    fs_id_str = str(fs_id)
    now_iso = datetime.now(timezone.utc).isoformat()
    await db.custom_ha_orders.update_one(
        {"order_id": order_id, "clinic_id": user["clinic_id"]},
        {"$set": {
            "audiogram_fs_id": fs_id_str,
            "audiogram_content_type": content_type,
            "audiogram_size_bytes": len(raw),
            "audiogram_uploaded_at": now_iso,
            "audiogram_filename": file.filename,
            "updated_at": now_iso,
        }},
    )

    # Mirror onto the linked stock_request so the head owner sees the
    # View Audiogram button in their inbox without a cross-clinic fetch.
    linked_req = order.get("linked_stock_request_id")
    if linked_req:
        await db.stock_requests.update_one(
            {"request_id": linked_req},
            {"$set": {
                "custom_ha_details.audiogram_fs_id": fs_id_str,
                "custom_ha_details.audiogram_content_type": content_type,
                "custom_ha_details.audiogram_filename": file.filename,
            }},
        )

    return {
        "ok": True,
        "order_id": order_id,
        "audiogram_fs_id": fs_id_str,
        "content_type": content_type,
        "size_bytes": len(raw),
    }


@router.delete("/custom-ha-orders/{order_id}/audiogram")
async def remove_audiogram(
    order_id: str,
    user=Depends(require_roles(
        "front_desk", "audiologist", "clinic_owner", "accounts", "super_admin",
    )),
    db=Depends(get_db),
):
    order = await db.custom_ha_orders.find_one(
        {"order_id": order_id, "clinic_id": user["clinic_id"]}, {"_id": 0},
    )
    if not order:
        raise HTTPException(404, "Custom HA order not found")
    fs_id = order.get("audiogram_fs_id")
    if fs_id:
        try:
            bucket = AsyncIOMotorGridFSBucket(db, bucket_name=_AUDIOGRAM_BUCKET)
            await bucket.delete(ObjectId(fs_id))
        except Exception:  # noqa: BLE001
            pass
    await db.custom_ha_orders.update_one(
        {"order_id": order_id, "clinic_id": user["clinic_id"]},
        {"$unset": {
            "audiogram_fs_id": "",
            "audiogram_content_type": "",
            "audiogram_size_bytes": "",
            "audiogram_uploaded_at": "",
            "audiogram_filename": "",
        }, "$set": {"updated_at": datetime.now(timezone.utc).isoformat()}},
    )
    if order.get("linked_stock_request_id"):
        await db.stock_requests.update_one(
            {"request_id": order["linked_stock_request_id"]},
            {"$unset": {
                "custom_ha_details.audiogram_fs_id": "",
                "custom_ha_details.audiogram_content_type": "",
                "custom_ha_details.audiogram_filename": "",
            }},
        )
    return {"ok": True}


async def _stream_audiogram(db, fs_id: str, content_type: str, filename: Optional[str]):
    bucket = AsyncIOMotorGridFSBucket(db, bucket_name=_AUDIOGRAM_BUCKET)
    try:
        stream = await bucket.open_download_stream(ObjectId(fs_id))
    except Exception:  # noqa: BLE001
        raise HTTPException(404, "Audiogram file missing")
    data = await stream.read()
    headers = {
        "Content-Disposition": f'inline; filename="{filename or "audiogram"}"',
        "Cache-Control": "private, max-age=300",
    }
    return StreamingResponse(io.BytesIO(data), media_type=content_type, headers=headers)


@router.get("/custom-ha-orders/{order_id}/audiogram")
async def download_audiogram(
    order_id: str,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    order = await db.custom_ha_orders.find_one(
        {"order_id": order_id, "clinic_id": user["clinic_id"]},
        {"_id": 0, "audiogram_fs_id": 1, "audiogram_content_type": 1, "audiogram_filename": 1},
    )
    if not order or not order.get("audiogram_fs_id"):
        raise HTTPException(404, "Audiogram not attached to this order")
    return await _stream_audiogram(
        db,
        order["audiogram_fs_id"],
        order.get("audiogram_content_type") or "application/pdf",
        order.get("audiogram_filename"),
    )


@router.get("/custom-ha-orders/available-audiograms")
async def list_available_audiograms(
    patient_id: str,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """List the patient's completed hearing-test sessions that have an
    uploaded report PDF — used by the Custom HA booking modal to offer
    one-click "attach latest audiogram" without a fresh upload.

    Only sessions with `report_pdf_fs_id` are returned; audiogram-less
    draft sessions are filtered out at the DB query level.
    """
    p = await db.patients.find_one(
        {"patient_id": patient_id, "clinic_id": user["clinic_id"]},
        {"_id": 0, "patient_id": 1},
    )
    if not p:
        raise HTTPException(404, "Patient not found")
    rows = await db.test_sessions.find(
        {"clinic_id": user["clinic_id"], "patient_id": patient_id,
         "report_pdf_fs_id": {"$exists": True, "$ne": None}},
        {"_id": 0, "session_id": 1, "test_date": 1, "audiologist_name": 1,
         "right_ear_degree": 1, "left_ear_degree": 1,
         "clinical_impression": 1, "report_pdf_size_bytes": 1,
         "created_at": 1},
    ).sort("created_at", -1).to_list(10)
    return [deserialize_datetime(r) for r in rows]
