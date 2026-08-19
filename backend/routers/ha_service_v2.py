"""AUDINEXA Service Operations — Phase 12.A + 12.B.

Adds to the existing `ha_service.py` (which keeps the legacy create/resolve/
close endpoints for backward compat). This router provides the new AUDINEXA
13-state transition endpoint plus Couriers, Estimates, Customer Approvals.

Endpoints:
    POST   /api/ha/service-tickets/{ticket_no}/transition
                — generic state-machine transition
                  {to_status, note, vendor_id, shipment_id, ...}

    POST   /api/ha/couriers                     — book a shipment
    GET    /api/ha/couriers                     — list (filterable)
    GET    /api/ha/couriers/{shipment_id}       — detail
    POST   /api/ha/couriers/{shipment_id}/status — transition status
                — also auto-advances the linked service-job on DELIVERED

    POST   /api/ha/service-estimates            — record vendor estimate
                — creates pending CustomerApproval + advances job → ESTIMATE_PENDING
    GET    /api/ha/service-estimates?ticket_no= — list

    POST   /api/ha/customer-approvals/{approval_id}/decide
                — front-desk records APPROVED / REJECTED
                — auto-advances job → CLIENT_APPROVED or CLIENT_REJECTED

    GET    /api/ha/service-jobs/{ticket_no}/pipeline
                — full stitched view: job + shipments + estimates + approvals
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from auth import get_current_user, require_roles, user_can_see_branch, CLINIC_WIDE_ROLES
from database import get_db
from models_ha import (
    CourierShipment, CourierShipmentCreate, CourierStatusPayload,
    ServiceEstimate, ServiceEstimateCreate,
    CustomerApproval, CustomerApprovalPayload,
)
from utils.concurrency import (
    assert_version, get_expected_version, version_update,
)
from utils.numbering import next_number
from utils.serde import serialize_datetime, deserialize_datetime
from utils.service_job_states import (
    assert_job_transition, normalise_status, TERMINAL_STATES,
)


router = APIRouter(prefix="/api/ha")

WRITE_ROLES = ("front_desk", "audiologist", "technician", "clinic_owner", "super_admin")


def _scope(user: dict) -> dict:
    if user["role"] in CLINIC_WIDE_ROLES:
        return {"clinic_id": user["clinic_id"]}
    return {"clinic_id": user["clinic_id"], "branch_id": {"$in": user.get("branch_ids") or []}}


async def _ticket(db, clinic_id: str, ticket_no: str) -> dict:
    t = await db.service_tickets.find_one(
        {"clinic_id": clinic_id, "ticket_no": ticket_no}, {"_id": 0},
    )
    if not t:
        raise HTTPException(status_code=404, detail="Service ticket not found")
    return t


# ==================== STATE TRANSITIONS ====================

class TransitionPayload(BaseModel):
    to_status: str
    note: Optional[str] = None
    vendor_id: Optional[str] = None           # for AWAITING_DISPATCH
    shipment_id: Optional[str] = None         # for DISPATCHED/IN_TRANSIT/RETURN_SHIPPED
    expected_version: Optional[int] = None    # opt-in optimistic-lock for offline replay


@router.post("/service-tickets/{ticket_no}/transition")
async def transition_service_job(
    ticket_no: str, payload: TransitionPayload, request: Request,
    user=Depends(require_roles(*WRITE_ROLES)),
    db=Depends(get_db),
):
    t = await _ticket(db, user["clinic_id"], ticket_no)
    if not user_can_see_branch(user, t["branch_id"]):
        raise HTTPException(status_code=403, detail="Ticket not in your branch")

    # Optimistic concurrency: client may pin the version it loaded.
    expected = get_expected_version(request, payload.model_dump())
    assert_version(t, expected)

    cur = normalise_status(t["status"])
    assert_job_transition(cur, payload.to_status)

    # ── Guard: must have an Outbound shipment with AWB before moving to
    # DISPATCHED. Front desk often hits the "→ Dispatched" next-step button
    # without first booking a courier — block that, otherwise the customer
    # has no tracking record. Skip when the transition already carries a
    # fresh shipment_id (i.e. the courier booking flow auto-advances).
    if cur == "AWAITING_DISPATCH" and payload.to_status == "DISPATCHED" and not payload.shipment_id:
        outbound = await db.ha_courier_shipments.find_one(
            {
                "clinic_id": user["clinic_id"],
                "ticket_no": ticket_no,
                "direction": "OUTBOUND",
                "awb_number": {"$nin": [None, ""]},
            },
            {"_id": 0, "shipment_id": 1},
        )
        if not outbound:
            raise HTTPException(
                status_code=422,
                detail="Book an outbound courier (with AWB / tracking number) before marking this job Dispatched.",
            )

    now_iso = datetime.now(timezone.utc).isoformat()
    upd: dict = {"status": payload.to_status, "updated_at": now_iso}

    # Fine-grained timestamp stamps for TAT analytics
    stamp_key = {
        "DISPATCHED":           "dispatched_at",
        "DELIVERED_TO_COMPANY": "delivered_to_company_at",
        "ESTIMATE_PENDING":     "estimate_received_at",
        "CLIENT_APPROVED":      "client_decided_at",
        "CLIENT_REJECTED":      "client_decided_at",
        "RETURN_SHIPPED":       "return_shipped_at",
        "READY_FOR_PICKUP":     "ready_at",
        "DELIVERED_TO_CLIENT":  "delivered_to_client_at",
        "CLOSED":               "closed_at",
    }.get(payload.to_status)
    if stamp_key:
        upd[stamp_key] = now_iso

    if payload.vendor_id:
        upd["vendor_id"] = payload.vendor_id
    if payload.shipment_id:
        # DISPATCHED/IN_TRANSIT = outbound; RETURN_SHIPPED = inbound
        if payload.to_status in ("DISPATCHED", "IN_TRANSIT", "DELIVERED_TO_COMPANY"):
            upd["outbound_shipment_id"] = payload.shipment_id
        elif payload.to_status in ("RETURN_SHIPPED", "READY_FOR_PICKUP"):
            upd["inbound_shipment_id"] = payload.shipment_id

    # Persist inspection / resolution notes alongside the audit trail so the
    # Service Report PDF can render them as first-class fields.
    if payload.note:
        if payload.to_status == "INSPECTED":
            upd["inspection_notes"] = payload.note
        elif payload.to_status == "DELIVERED_TO_CLIENT":
            upd["handover_notes"] = payload.note
        elif payload.to_status in ("READY_FOR_PICKUP", "CLOSED"):
            # Append-or-set for resolution summary
            upd.setdefault("resolution_notes", payload.note)

    await db.service_tickets.update_one(
        {"clinic_id": user["clinic_id"], "ticket_no": ticket_no},
        {"$set": {**upd, "version_updated_at": now_iso},
         "$inc": {"version": 1},
         "$push": {"audit_trail": {
             "from": cur, "to": payload.to_status,
             "at": now_iso, "by_user_id": user["user_id"],
             "note": payload.note,
         }}},
    )
    # Read back the new version so the client can pin its next call.
    fresh = await db.service_tickets.find_one(
        {"clinic_id": user["clinic_id"], "ticket_no": ticket_no},
        {"_id": 0, "version": 1},
    )
    return {"ok": True, "ticket_no": ticket_no,
            "from": cur, "to": payload.to_status, "at": now_iso,
            "version": (fresh or {}).get("version", 1)}


# ==================== COURIER SHIPMENTS ====================

@router.post("/couriers", response_model=CourierShipment, status_code=201)
async def create_shipment(
    payload: CourierShipmentCreate,
    user=Depends(require_roles(*WRITE_ROLES)),
    db=Depends(get_db),
):
    t = await _ticket(db, user["clinic_id"], payload.ticket_no)
    if not user_can_see_branch(user, t["branch_id"]):
        raise HTTPException(status_code=403, detail="Ticket not in your branch")

    # Uniqueness: same AWB cannot be booked twice on the same direction.
    # Skip uniqueness check when AWB is missing (PENDING_AWB case — the
    # courier guy promised the number "tomorrow"); we'll re-validate
    # when the AWB arrives via PATCH /couriers/{id}/awb.
    if payload.awb_number:
        dup = await db.ha_courier_shipments.find_one({
            "clinic_id": user["clinic_id"], "awb_number": payload.awb_number,
            "direction": payload.direction,
        }, {"_id": 0, "shipment_id": 1})
        if dup:
            raise HTTPException(
                status_code=409,
                detail=f"AWB {payload.awb_number} already booked ({dup['shipment_id']})",
            )

    shid = await next_number(db, "courier", user["clinic_id"])
    doc = CourierShipment(
        shipment_id=shid,
        clinic_id=user["clinic_id"],
        branch_id=t["branch_id"],
        ticket_no=payload.ticket_no,
        direction=payload.direction,
        courier_partner=payload.courier_partner,
        awb_number=payload.awb_number,
        dispatch_date=payload.dispatch_date,
        eta_date=payload.eta_date,
        from_address=payload.from_address,
        to_address=payload.to_address,
        recipient_name=payload.recipient_name,
        notes=payload.notes,
        # PENDING_AWB while the number is missing; reception will PATCH
        # it in when the courier guy comes back with the slip.
        status="BOOKED" if payload.awb_number else "PENDING_AWB",
        created_by_user_id=user["user_id"],
    )
    await db.ha_courier_shipments.insert_one(serialize_datetime(doc.model_dump()))
    # Attach onto ticket right away for quick drill-down
    link_key = "outbound_shipment_id" if payload.direction == "OUTBOUND" else "inbound_shipment_id"
    upd: dict = {link_key: shid}

    # ---- Auto-advance the linked job's pipeline state ----
    # Booking a shipment is the natural trigger for the next state.
    cur_job = normalise_status(t["status"])
    auto_to: Optional[str] = None
    if payload.direction == "OUTBOUND" and cur_job == "AWAITING_DISPATCH":
        auto_to = "DISPATCHED"
    elif payload.direction == "INBOUND" and cur_job in (
        "REPAIR_IN_PROGRESS", "CLIENT_REJECTED",
    ):
        auto_to = "RETURN_SHIPPED"

    if auto_to:
        now_iso = datetime.now(timezone.utc).isoformat()
        upd["status"] = auto_to
        upd["updated_at"] = now_iso
        if auto_to == "DISPATCHED":
            upd["dispatched_at"] = now_iso
        elif auto_to == "RETURN_SHIPPED":
            upd["return_shipped_at"] = now_iso
        await db.service_tickets.update_one(
            {"clinic_id": user["clinic_id"], "ticket_no": payload.ticket_no},
            {"$set": upd,
             "$push": {"audit_trail": {
                 "from": cur_job, "to": auto_to,
                 "at": now_iso, "by_user_id": user["user_id"],
                 "note": f"Auto-advanced on shipment {shid} booking",
             }}},
        )
    else:
        await db.service_tickets.update_one(
            {"clinic_id": user["clinic_id"], "ticket_no": payload.ticket_no},
            {"$set": upd},
        )
    return deserialize_datetime(doc.model_dump())


@router.get("/couriers", response_model=List[CourierShipment])
async def list_shipments(
    ticket_no: Optional[str] = None,
    direction: Optional[str] = Query(None, description="OUTBOUND|INBOUND"),
    status: Optional[str] = None,
    limit: int = 200,
    user=Depends(get_current_user), db=Depends(get_db),
):
    q = _scope(user)
    if ticket_no:
        q["ticket_no"] = ticket_no
    if direction:
        q["direction"] = direction
    if status:
        q["status"] = status
    rows = await db.ha_courier_shipments.find(q, {"_id": 0}).sort("created_at", -1).to_list(limit)
    return [deserialize_datetime(r) for r in rows]


@router.get("/couriers/{shipment_id}", response_model=CourierShipment)
async def get_shipment(shipment_id: str, user=Depends(get_current_user), db=Depends(get_db)):
    row = await db.ha_courier_shipments.find_one(
        {"clinic_id": user["clinic_id"], "shipment_id": shipment_id}, {"_id": 0},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Shipment not found")
    if not user_can_see_branch(user, row["branch_id"]):
        raise HTTPException(status_code=403, detail="Shipment not in your branch")
    return deserialize_datetime(row)


# Valid shipment-status transitions
_SHIP_TRANSITIONS = {
    "BOOKED":     {"PICKED_UP", "CANCELLED", "EXCEPTION"},
    "PICKED_UP":  {"IN_TRANSIT", "DELIVERED", "EXCEPTION", "CANCELLED"},
    "IN_TRANSIT": {"DELIVERED", "EXCEPTION", "CANCELLED"},
    "EXCEPTION":  {"IN_TRANSIT", "DELIVERED", "CANCELLED"},
    "DELIVERED":  set(),
    "CANCELLED":  set(),
}


@router.post("/couriers/{shipment_id}/status", response_model=CourierShipment)
async def update_shipment_status(
    shipment_id: str, payload: CourierStatusPayload,
    user=Depends(require_roles(*WRITE_ROLES)),
    db=Depends(get_db),
):
    row = await db.ha_courier_shipments.find_one(
        {"clinic_id": user["clinic_id"], "shipment_id": shipment_id}, {"_id": 0},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Shipment not found")
    if not user_can_see_branch(user, row["branch_id"]):
        raise HTTPException(status_code=403, detail="Shipment not in your branch")
    cur = row["status"]
    if payload.to_status not in _SHIP_TRANSITIONS.get(cur, set()):
        raise HTTPException(
            status_code=409,
            detail=f"Illegal shipment transition {cur} → {payload.to_status}. "
                   f"Legal: {sorted(_SHIP_TRANSITIONS.get(cur, []))}",
        )
    now_iso = datetime.now(timezone.utc).isoformat()
    upd = {"status": payload.to_status, "updated_at": now_iso}
    if payload.exception_note:
        upd["exception_note"] = payload.exception_note
    if payload.to_status == "DELIVERED":
        upd["delivered_at"] = now_iso

    await db.ha_courier_shipments.update_one(
        {"clinic_id": user["clinic_id"], "shipment_id": shipment_id},
        {"$set": upd},
    )

    # Auto-advance the linked service-job when outbound DELIVERED
    if payload.to_status == "DELIVERED" and row["direction"] == "OUTBOUND":
        try:
            t = await _ticket(db, user["clinic_id"], row["ticket_no"])
            cur_job = normalise_status(t["status"])
            # Only auto-advance if currently DISPATCHED / IN_TRANSIT
            if cur_job in ("DISPATCHED", "IN_TRANSIT"):
                await db.service_tickets.update_one(
                    {"clinic_id": user["clinic_id"], "ticket_no": row["ticket_no"]},
                    {"$set": {"status": "DELIVERED_TO_COMPANY",
                              "delivered_to_company_at": now_iso,
                              "updated_at": now_iso}},
                )
        except HTTPException:
            pass
    row["status"] = payload.to_status
    row["updated_at"] = now_iso
    if payload.to_status == "DELIVERED":
        row["delivered_at"] = now_iso
    if payload.exception_note:
        row["exception_note"] = payload.exception_note
    return deserialize_datetime(row)


# ==================== ESTIMATES + CUSTOMER APPROVAL ====================

@router.post("/service-estimates", response_model=ServiceEstimate, status_code=201)
async def record_estimate(
    payload: ServiceEstimateCreate,
    user=Depends(require_roles(*WRITE_ROLES)),
    db=Depends(get_db),
):
    t = await _ticket(db, user["clinic_id"], payload.ticket_no)
    if not user_can_see_branch(user, t["branch_id"]):
        raise HTTPException(status_code=403, detail="Ticket not in your branch")

    # Only valid when ticket is at the company
    cur = normalise_status(t["status"])
    if cur != "DELIVERED_TO_COMPANY" and cur != "ESTIMATE_PENDING":
        raise HTTPException(
            status_code=409,
            detail=f"Estimates can only be recorded after device reaches the company "
                   f"(current status: {cur})",
        )

    eid = await next_number(db, "estimate", user["clinic_id"])
    received_on = payload.received_on or datetime.now(timezone.utc).date().isoformat()
    now_iso = datetime.now(timezone.utc).isoformat()
    has_conveyed = (
        payload.conveyed_amount is not None
        or payload.discount is not None
    )
    est = ServiceEstimate(
        estimate_id=eid,
        clinic_id=user["clinic_id"],
        ticket_no=payload.ticket_no,
        vendor_id=payload.vendor_id or t.get("vendor_id"),
        vendor_name=payload.vendor_name,
        received_on=received_on,
        warranty_covered=payload.warranty_covered,
        amount=float(payload.amount or 0),
        conveyed_amount=(float(payload.conveyed_amount)
                         if payload.conveyed_amount is not None else None),
        discount=(float(payload.discount)
                  if payload.discount is not None else None),
        # Stamp who conveyed the price the moment the estimate is created
        conveyed_by_user_id=user["user_id"] if has_conveyed else None,
        conveyed_by_name=user.get("name") if has_conveyed else None,
        conveyed_at=now_iso if has_conveyed else None,
        repair_notes=payload.repair_notes,
        eta_days=payload.eta_days,
        created_by_user_id=user["user_id"],
    )
    await db.ha_service_estimates.insert_one(serialize_datetime(est.model_dump()))

    # Auto-create a PENDING CustomerApproval
    aid = await next_number(db, "approval", user["clinic_id"])
    approval = CustomerApproval(
        approval_id=aid,
        clinic_id=user["clinic_id"],
        ticket_no=payload.ticket_no,
        estimate_id=eid,
        decision="PENDING",
    )
    await db.ha_customer_approvals.insert_one(serialize_datetime(approval.model_dump()))

    # Advance job → ESTIMATE_PENDING (legal from DELIVERED_TO_COMPANY;
    # from ESTIMATE_PENDING itself it's a no-op).
    if cur == "DELIVERED_TO_COMPANY":
        await db.service_tickets.update_one(
            {"clinic_id": user["clinic_id"], "ticket_no": payload.ticket_no},
            {"$set": {"status": "ESTIMATE_PENDING",
                      "estimate_received_at": now_iso,
                      "estimate_id": eid, "approval_id": aid,
                      "updated_at": now_iso}},
        )
    else:
        # still link latest estimate/approval for easy drill-down
        await db.service_tickets.update_one(
            {"clinic_id": user["clinic_id"], "ticket_no": payload.ticket_no},
            {"$set": {"estimate_id": eid, "approval_id": aid,
                      "updated_at": now_iso}},
        )
    return deserialize_datetime(est.model_dump())


@router.get("/service-estimates", response_model=List[ServiceEstimate])
async def list_estimates(
    ticket_no: Optional[str] = None, limit: int = 200,
    user=Depends(get_current_user), db=Depends(get_db),
):
    q = {"clinic_id": user["clinic_id"]}
    if ticket_no:
        q["ticket_no"] = ticket_no
    rows = await db.ha_service_estimates.find(q, {"_id": 0}).sort("created_at", -1).to_list(limit)
    return [deserialize_datetime(r) for r in rows]


@router.post("/customer-approvals/{approval_id}/decide", response_model=CustomerApproval)
async def decide_approval(
    approval_id: str, payload: CustomerApprovalPayload,
    user=Depends(require_roles(*WRITE_ROLES)),
    db=Depends(get_db),
):
    if payload.decision not in ("APPROVED", "REJECTED"):
        raise HTTPException(status_code=400, detail="decision must be APPROVED or REJECTED")
    row = await db.ha_customer_approvals.find_one(
        {"clinic_id": user["clinic_id"], "approval_id": approval_id}, {"_id": 0},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Approval not found")
    if row["decision"] != "PENDING":
        raise HTTPException(status_code=409,
                            detail=f"Approval already {row['decision']}")
    now_iso = datetime.now(timezone.utc).isoformat()
    decision_set = {
        "decision": payload.decision,
        "notes": payload.notes,
        "contact_number": payload.contact_number,
        "decided_by_user_id": user["user_id"],
        "decided_by_name": user.get("name"),
        "decided_at": now_iso,
    }
    await db.ha_customer_approvals.update_one(
        {"clinic_id": user["clinic_id"], "approval_id": approval_id},
        {"$set": decision_set},
    )
    # Advance linked service-job
    t = await _ticket(db, user["clinic_id"], row["ticket_no"])
    cur = normalise_status(t["status"])
    new_job_status = "CLIENT_APPROVED" if payload.decision == "APPROVED" else "CLIENT_REJECTED"
    if cur == "ESTIMATE_PENDING":
        await db.service_tickets.update_one(
            {"clinic_id": user["clinic_id"], "ticket_no": row["ticket_no"]},
            {"$set": {"status": new_job_status,
                      "client_decided_at": now_iso,
                      "updated_at": now_iso}},
        )
    row.update(decision_set)
    return deserialize_datetime(row)


# ==================== STITCHED PIPELINE VIEW ====================

@router.get("/service-jobs/{ticket_no}/pipeline")
async def pipeline_view(
    ticket_no: str, user=Depends(get_current_user), db=Depends(get_db),
):
    t = await _ticket(db, user["clinic_id"], ticket_no)
    if not user_can_see_branch(user, t["branch_id"]):
        raise HTTPException(status_code=403, detail="Ticket not in your branch")
    shipments = await db.ha_courier_shipments.find(
        {"clinic_id": user["clinic_id"], "ticket_no": ticket_no}, {"_id": 0},
    ).sort("created_at", 1).to_list(50)
    estimates = await db.ha_service_estimates.find(
        {"clinic_id": user["clinic_id"], "ticket_no": ticket_no}, {"_id": 0},
    ).sort("created_at", 1).to_list(50)
    approvals = await db.ha_customer_approvals.find(
        {"clinic_id": user["clinic_id"], "ticket_no": ticket_no}, {"_id": 0},
    ).sort("created_at", 1).to_list(50)
    return {
        "ticket": deserialize_datetime(t),
        "normalised_status": normalise_status(t["status"]),
        "is_terminal": normalise_status(t["status"]) in TERMINAL_STATES,
        "shipments": [deserialize_datetime(r) for r in shipments],
        "estimates": [deserialize_datetime(r) for r in estimates],
        "approvals": [deserialize_datetime(r) for r in approvals],
    }


# ============================================================================
# AUTO-INVOICE — generate a GST invoice for the service job at handover.
# ============================================================================
SERVICE_GST_RATE = 18.0     # India: hearing-aid service & repair classified
                            # under HSN/SAC 9985 → 18% IGST
SERVICE_HSN_SAC = "9985"    # Standard SAC for "Other support services"


@router.post("/service-tickets/{ticket_no}/invoice")
async def generate_service_invoice(
    ticket_no: str,
    user=Depends(require_roles(*WRITE_ROLES)),
    db=Depends(get_db),
):
    """Auto-generate a GST invoice for the completed service job.

    Behaviour:
      • Idempotent: if the ticket already has `invoice_id`, returns the
        existing invoice (callers don't need to know about state).
      • Allowed only at terminal-customer states: READY_FOR_PICKUP /
        DELIVERED_TO_CLIENT / CLOSED.
      • Creates ONE invoice line:
          description = "Hearing-aid Service & Repair · {ticket_no}"
          unit_price  = approved estimate's (conveyed_amount − discount), or
                        fallback to ticket.cost_to_patient
          gst_rate    = 18% (SAC 9985)
      • Warranty-covered jobs → unit_price=0 → tax-exempt invoice (₹0 grand
        total) which still serves as a paper trail for the patient.
      • Stamps the new invoice_id + invoice_no on the ticket so the drawer
        can render "View Invoice" instead of "Generate Invoice" on reopen.
    """
    from billing import _next_invoice_no, _compute_line, _apply_tax_split
    from models import (
        Invoice, InvoiceLineCreate, InvoiceLine,
    )

    t = await _ticket(db, user["clinic_id"], ticket_no)
    if not user_can_see_branch(user, t["branch_id"]):
        raise HTTPException(status_code=403, detail="Ticket not in your branch")

    cur = normalise_status(t["status"])
    if cur not in {"READY_FOR_PICKUP", "DELIVERED_TO_CLIENT", "CLOSED"}:
        raise HTTPException(
            status_code=409,
            detail=(
                "Invoice can be generated only after the job reaches "
                "Ready-for-pickup / Delivered / Closed (current: "
                f"{cur}). Approve the estimate and complete the repair first."
            ),
        )

    # Idempotent: return existing invoice
    if t.get("invoice_id"):
        existing = await db.invoices.find_one(
            {"invoice_id": t["invoice_id"], "clinic_id": user["clinic_id"]},
            {"_id": 0},
        )
        if existing:
            return deserialize_datetime(existing)
        # Stale linkage — fall through and regenerate

    # Resolve final amount: approved estimate first, then ticket cost
    estimates = await db.ha_service_estimates.find(
        {"clinic_id": user["clinic_id"], "ticket_no": ticket_no},
        {"_id": 0},
    ).sort("created_at", -1).to_list(10)
    approvals = {a["estimate_id"]: a for a in await db.ha_customer_approvals.find(
        {"clinic_id": user["clinic_id"], "ticket_no": ticket_no, "decision": "APPROVED"},
        {"_id": 0},
    ).to_list(10)}

    final_amount = 0.0
    warranty_covered = bool(t.get("warranty_covered"))
    chosen_est = None
    for e in estimates:
        if e.get("estimate_id") in approvals:
            chosen_est = e
            break
    if chosen_est:
        warranty_covered = bool(chosen_est.get("warranty_covered"))
        if warranty_covered:
            final_amount = 0.0
        else:
            conveyed = chosen_est.get("conveyed_amount")
            base = float(conveyed) if conveyed is not None else float(chosen_est.get("amount") or 0)
            final_amount = max(0.0, base - float(chosen_est.get("discount") or 0))
    else:
        final_amount = float(t.get("cost_to_patient") or 0)

    # Patient + clinic for header/state-split
    patient = await db.patients.find_one(
        {"patient_id": t["patient_id"], "clinic_id": user["clinic_id"]}, {"_id": 0},
    ) or {}
    clinic = await db.clinics.find_one(
        {"clinic_id": user["clinic_id"]}, {"_id": 0},
    ) or {}

    # Build the single line
    line_in = InvoiceLineCreate(
        description=f"Hearing-aid Service & Repair · {ticket_no}",
        quantity=1.0,
        unit_price=final_amount,
        is_taxable=(not warranty_covered),
        gst_rate=(SERVICE_GST_RATE if not warranty_covered else 0.0),
        hsn_sac=SERVICE_HSN_SAC,
    )
    # Service-aware shape — _compute_line reads `gst_inclusive` from this dict.
    # The conveyed_amount the customer approved IS the final amount they pay
    # (3000 conveyed = 3000 invoice grand-total, NOT 3000 + 18% = 3540).
    # Treating it as inclusive back-calculates the taxable base + tax split,
    # which is what GST law requires on a tax-invoice for a quoted service.
    pseudo_service = {
        "name": line_in.description,
        "price": final_amount,
        "is_taxable": line_in.is_taxable,
        "gst_rate": line_in.gst_rate,
        "hsn_sac": line_in.hsn_sac,
        "gst_inclusive": True,
    }
    resolved_line: InvoiceLine = _compute_line(line_in, pseudo_service)

    # Intra vs inter-state split
    clinic_state = (clinic.get("state") or "").strip().lower()
    pat_state = (patient.get("state") or "").strip().lower()
    inter_state = bool(clinic_state and pat_state and clinic_state != pat_state)
    _apply_tax_split([resolved_line], inter_state)

    invoice_no = await _next_invoice_no(db, user["clinic_id"])
    inv = Invoice(
        clinic_id=user["clinic_id"],
        invoice_no=invoice_no,
        patient_id=patient.get("patient_id", t["patient_id"]),
        patient_name=patient.get("name", t.get("patient_name", "")),
        patient_mobile=patient.get("mobile") or patient.get("phone") or t.get("patient_mobile"),
        mrd=patient.get("mrd"),
        ticket_no=ticket_no,
        lines=[resolved_line],
        notes=(
            f"Auto-generated from Service Job {ticket_no}."
            + (" Warranty-covered." if warranty_covered else "")
        ),
        created_by_user_id=user["user_id"],
    )
    # Roll-up totals (mirror billing.create_invoice)
    inv.subtotal = round(sum(ln.taxable_value for ln in inv.lines), 2)
    inv.discount_total = round(sum(ln.discount_amount for ln in inv.lines), 2)
    inv.cgst_total = round(sum(ln.cgst_amount for ln in inv.lines), 2)
    inv.sgst_total = round(sum(ln.sgst_amount for ln in inv.lines), 2)
    inv.igst_total = round(sum(ln.igst_amount for ln in inv.lines), 2)
    inv.tax_total = round(inv.cgst_total + inv.sgst_total + inv.igst_total, 2)
    inv.grand_total = round(inv.subtotal + inv.tax_total, 2)
    inv.rounded_total = round(inv.grand_total)
    inv.round_off = round(inv.rounded_total - inv.grand_total, 2)
    inv.due_total = inv.rounded_total
    inv.paid_total = 0.0
    if inv.rounded_total <= 0:
        inv.status = "paid"
        inv.due_total = 0.0

    from billing import _serialize, _insert_invoice_with_retry
    inv_doc = _serialize(inv.model_dump())
    await _insert_invoice_with_retry(db, inv_doc, user["clinic_id"])
    if inv_doc.get("invoice_no") != inv.invoice_no:
        inv.invoice_no = inv_doc["invoice_no"]
    # Stamp on ticket so future calls are idempotent
    await db.service_tickets.update_one(
        {"clinic_id": user["clinic_id"], "ticket_no": ticket_no},
        {"$set": {
            "invoice_id": inv.invoice_id,
            "invoice_no": inv.invoice_no,
            "cost_to_patient": final_amount,
            "version_updated_at": datetime.now(timezone.utc).isoformat(),
        }, "$inc": {"version": 1}},
    )
    return deserialize_datetime(inv.model_dump())


# ==================== Phase 14 — Clinical workflow extensions ====================

class AwbStampIn(BaseModel):
    awb_number: str
    courier_partner: Optional[str] = None
    dispatch_date: Optional[str] = None
    eta_date: Optional[str] = None


@router.patch("/couriers/{shipment_id}/awb")
async def patch_courier_awb(
    shipment_id: str,
    payload: AwbStampIn,
    user=Depends(require_roles(*WRITE_ROLES)),
    db=Depends(get_db),
):
    """Stamp the AWB on a shipment that was booked in PENDING_AWB mode
    (courier guy promised the number 'tomorrow'). Auto-flips status to
    BOOKED and re-checks AWB uniqueness within (clinic, direction)."""
    s = await db.ha_courier_shipments.find_one(
        {"clinic_id": user["clinic_id"], "shipment_id": shipment_id},
        {"_id": 0},
    )
    if not s:
        raise HTTPException(404, "Shipment not found")
    if not user_can_see_branch(user, s["branch_id"]):
        raise HTTPException(403, "Shipment not in your branch")

    awb = (payload.awb_number or "").strip()
    if not awb:
        raise HTTPException(422, "awb_number is required")

    dup = await db.ha_courier_shipments.find_one(
        {"clinic_id": user["clinic_id"], "awb_number": awb,
         "direction": s["direction"], "shipment_id": {"$ne": shipment_id}},
        {"_id": 0, "shipment_id": 1},
    )
    if dup:
        raise HTTPException(
            status_code=409,
            detail=f"AWB {awb} already booked ({dup['shipment_id']})",
        )

    now = datetime.now(timezone.utc).isoformat()
    upd: dict = {"awb_number": awb, "updated_at": now}
    if s.get("status") == "PENDING_AWB":
        upd["status"] = "BOOKED"
    if payload.courier_partner:
        upd["courier_partner"] = payload.courier_partner
    if payload.dispatch_date:
        upd["dispatch_date"] = payload.dispatch_date
    if payload.eta_date:
        upd["eta_date"] = payload.eta_date
    await db.ha_courier_shipments.update_one(
        {"clinic_id": user["clinic_id"], "shipment_id": shipment_id},
        {"$set": upd},
    )
    return {**s, **upd}


class LoanerIssueIn(BaseModel):
    loaner_serial_id: str
    deposit_amount: Optional[float] = None  # blank by default — clinic types it case-by-case


@router.post("/service-tickets/{ticket_no}/loaner/issue", status_code=200)
async def issue_loaner(
    ticket_no: str,
    payload: LoanerIssueIn,
    user=Depends(require_roles(*WRITE_ROLES)),
    db=Depends(get_db),
):
    """Hand a loaner HA to the patient while their unit is at the
    manufacturer. Moves the loaner serial IN_STOCK → ON_LOAN and stamps
    the ticket with the loaner_serial_id + optional refundable deposit."""
    from routers.ha_inventory import transition_serial
    t = await _ticket(db, user["clinic_id"], ticket_no)
    if not user_can_see_branch(user, t["branch_id"]):
        raise HTTPException(403, "Ticket not in your branch")
    if t.get("loaner_serial_id"):
        raise HTTPException(409, "Loaner already issued for this ticket")
    sid = payload.loaner_serial_id
    serial = await db.serial_items.find_one(
        {"serial_id": sid, "clinic_id": user["clinic_id"]},
        {"_id": 0, "state": 1, "serial_no": 1, "pool": 1},
    )
    if not serial:
        raise HTTPException(404, "Loaner serial not found")
    if serial["state"] != "IN_STOCK":
        raise HTTPException(409, f"Loaner serial is {serial['state']}, expected IN_STOCK")

    await transition_serial(
        db, sid, "ON_LOAN",
        actor_user_id=user["user_id"],
        ref_doc={"kind": "loaner", "id": ticket_no},
        note=f"Issued as loaner on ticket {ticket_no}",
    )
    now = datetime.now(timezone.utc).isoformat()
    patch: dict = {
        "loaner_serial_id": sid,
        "loaner_issued_at": now,
        "version_updated_at": now,
    }
    if payload.deposit_amount is not None and payload.deposit_amount > 0:
        patch["loaner_deposit_amount"] = float(payload.deposit_amount)
        patch["loaner_deposit_collected_at"] = now
    await db.service_tickets.update_one(
        {"clinic_id": user["clinic_id"], "ticket_no": ticket_no},
        {"$set": patch, "$inc": {"version": 1}},
    )
    return {"ok": True, "loaner_serial_id": sid,
            "loaner_serial_no": serial.get("serial_no"),
            "deposit_amount": patch.get("loaner_deposit_amount"),
            "loaner_issued_at": now}


class LoanerReturnIn(BaseModel):
    forfeit_deposit: bool = False  # patient walked off / never returned
    notes: Optional[str] = None


@router.post("/service-tickets/{ticket_no}/loaner/return", status_code=200)
async def return_loaner(
    ticket_no: str,
    payload: LoanerReturnIn,
    user=Depends(require_roles(*WRITE_ROLES)),
    db=Depends(get_db),
):
    """Patient returns the loaner. Moves serial ON_LOAN → IN_STOCK and
    refunds the deposit (unless `forfeit_deposit=true` — used when the
    7-day program expiry hit and patient never showed)."""
    from routers.ha_inventory import transition_serial
    t = await _ticket(db, user["clinic_id"], ticket_no)
    sid = t.get("loaner_serial_id")
    if not sid:
        raise HTTPException(409, "No loaner issued on this ticket")
    if t.get("loaner_returned_at"):
        raise HTTPException(409, "Loaner already returned")

    await transition_serial(
        db, sid, "IN_STOCK",
        actor_user_id=user["user_id"],
        ref_doc={"kind": "loaner_return", "id": ticket_no},
        note=f"Loaner returned on ticket {ticket_no}",
    )
    now = datetime.now(timezone.utc).isoformat()
    patch: dict = {"loaner_returned_at": now, "version_updated_at": now}
    if t.get("loaner_deposit_amount") and not t.get("loaner_deposit_refunded_at"):
        if payload.forfeit_deposit:
            patch["loaner_deposit_forfeited_at"] = now
        else:
            patch["loaner_deposit_refunded_at"] = now
    await db.service_tickets.update_one(
        {"clinic_id": user["clinic_id"], "ticket_no": ticket_no},
        {"$set": patch, "$inc": {"version": 1}},
    )
    return {"ok": True, **patch}


@router.post("/service-tickets/{ticket_no}/mark-return-unrepaired", status_code=200)
async def mark_return_unrepaired(
    ticket_no: str,
    user=Depends(require_roles(*WRITE_ROLES)),
    db=Depends(get_db),
):
    """Patient declined the vendor estimate. Vendor books the return
    courier (reception fills the AWB later via the courier endpoint).
    This action flags the ticket as no-charge and creates a placeholder
    INBOUND courier shell so reception can fill in the AWB when the
    vendor's courier reaches them."""
    t = await _ticket(db, user["clinic_id"], ticket_no)
    if not user_can_see_branch(user, t["branch_id"]):
        raise HTTPException(403, "Ticket not in your branch")
    if t.get("return_unrepaired"):
        raise HTTPException(409, "Already flagged as return-unrepaired")

    # Pre-create inbound courier shell (AWB blank — vendor will book,
    # reception fills the AWB later via PATCH /couriers/{id}/awb).
    if not t.get("inbound_shipment_id"):
        shid = await next_number(db, "courier", user["clinic_id"])
        doc = CourierShipment(
            shipment_id=shid,
            clinic_id=user["clinic_id"],
            branch_id=t["branch_id"],
            ticket_no=ticket_no,
            direction="INBOUND",
            courier_partner="(pending)",
            awb_number=None,
            status="PENDING_AWB",
            notes="Auto-created on return-unrepaired flag. Awaiting AWB from vendor.",
            created_by_user_id=user["user_id"],
        )
        await db.ha_courier_shipments.insert_one(serialize_datetime(doc.model_dump()))
    else:
        shid = t["inbound_shipment_id"]

    now = datetime.now(timezone.utc).isoformat()
    await db.service_tickets.update_one(
        {"clinic_id": user["clinic_id"], "ticket_no": ticket_no},
        {"$set": {
            "return_unrepaired": True,
            "return_unrepaired_at": now,
            "warranty_covered": False,  # no charges
            "cost_to_patient": 0.0,
            "inbound_shipment_id": shid,
            "version_updated_at": now,
        }, "$inc": {"version": 1}},
    )
    return {"ok": True, "ticket_no": ticket_no,
            "inbound_shipment_id": shid, "return_unrepaired_at": now}



# ==================== SERVICE NOTE PDF ====================

@router.get("/service-tickets/{ticket_no}/service-note.pdf")
async def service_note_pdf(
    ticket_no: str,
    user=Depends(require_roles(*WRITE_ROLES)),
    db=Depends(get_db),
):
    """A4 acknowledgement printed at the moment a HA leaves the clinic
    for the manufacturer (or is held at the clinic for in-house repair).
    Doubles as the patient's claim slip + loaner deposit receipt.
    """
    from fastapi.responses import Response
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    )
    from reportlab.lib import colors
    import io

    t = await _ticket(db, user["clinic_id"], ticket_no)
    if not user_can_see_branch(user, t["branch_id"]):
        raise HTTPException(403, "Ticket not in your branch")

    clinic = await db.clinics.find_one(
        {"clinic_id": user["clinic_id"]},
        {"_id": 0, "name": 1, "address": 1, "phone": 1, "gstin": 1},
    ) or {}
    patient = await db.patients.find_one(
        {"patient_id": t["patient_id"]},
        {"_id": 0, "name": 1, "mobile": 1, "mrd": 1},
    ) or {}
    serial = None
    if t.get("serial_id"):
        serial = await db.serial_items.find_one(
            {"serial_id": t["serial_id"]},
            {"_id": 0, "serial_no": 1, "product_id": 1, "warranty_end_date": 1},
        )
    product = None
    if serial and serial.get("product_id"):
        product = await db.ha_products.find_one(
            {"product_id": serial["product_id"]},
            {"_id": 0, "brand": 1, "model": 1},
        )
    loaner_serial = None
    if t.get("loaner_serial_id"):
        loaner_serial = await db.serial_items.find_one(
            {"serial_id": t["loaner_serial_id"]},
            {"_id": 0, "serial_no": 1},
        )

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm,
    )
    styles = getSampleStyleSheet()
    body = styles["BodyText"]
    body.fontSize = 10
    flow = []

    flow.append(Paragraph(
        f"<b>{clinic.get('name', 'Audiology Clinic')}</b>", styles["Title"]
    ))
    if clinic.get("address"):
        flow.append(Paragraph(clinic["address"], body))
    meta = []
    if clinic.get("phone"):
        meta.append(f"Phone: {clinic['phone']}")
    if clinic.get("gstin"):
        meta.append(f"GSTIN: {clinic['gstin']}")
    if meta:
        flow.append(Paragraph(" &nbsp;·&nbsp; ".join(meta), body))
    flow.append(Spacer(1, 6 * mm))

    flow.append(Paragraph("<b>SERVICE ACKNOWLEDGEMENT</b>", styles["Heading2"]))
    flow.append(Spacer(1, 2 * mm))

    header_rows = [
        ["Ticket No.", t["ticket_no"], "Date", (str(t.get("created_at") or ""))[:10]],
        ["Patient", patient.get("name", "—"), "Mobile", patient.get("mobile", "—")],
        ["MRD", patient.get("mrd", "—"), "Repair Location", t.get("repair_location", "—")],
    ]
    tbl = Table(header_rows, colWidths=[35 * mm, 55 * mm, 30 * mm, 50 * mm])
    tbl.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, -1), "Helvetica", 9),
        ("FONT", (0, 0), (0, -1), "Helvetica-Bold", 9),
        ("FONT", (2, 0), (2, -1), "Helvetica-Bold", 9),
        ("BOX", (0, 0), (-1, -1), 0.4, colors.grey),
        ("INNERGRID", (0, 0), (-1, -1), 0.2, colors.lightgrey),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    flow.append(tbl)
    flow.append(Spacer(1, 4 * mm))

    flow.append(Paragraph("<b>Hearing Aid Details</b>", body))
    unit_lines = []
    if product:
        unit_lines.append(f"Make / Model: {product.get('brand', '')} {product.get('model', '')}")
    if serial:
        unit_lines.append(f"Serial No.: {serial.get('serial_no', '—')}")
        if serial.get("warranty_end_date"):
            unit_lines.append(f"Warranty till: {serial['warranty_end_date']}")
    if not unit_lines:
        unit_lines.append("(No HA unit linked)")
    flow.append(Paragraph("<br/>".join(unit_lines), body))
    flow.append(Spacer(1, 3 * mm))

    flow.append(Paragraph("<b>Complaint as recorded</b>", body))
    flow.append(Paragraph(t.get("complaint") or "—", body))
    flow.append(Spacer(1, 3 * mm))

    if t.get("repair_location") == "VENDOR":
        msg = ("This unit will be sent to the manufacturer for service. "
               "We will contact you with the repair estimate once received.")
    else:
        msg = ("This unit is held at the clinic for inspection / repair. "
               "We will contact you with the outcome.")
    flow.append(Paragraph(f"<i>{msg}</i>", body))
    flow.append(Spacer(1, 4 * mm))

    if loaner_serial:
        flow.append(Paragraph("<b>Loaner Hearing Aid Issued</b>", body))
        ldata = [["Loaner Serial No.", loaner_serial.get("serial_no", "—")]]
        if t.get("loaner_deposit_amount"):
            ldata.append([
                "Refundable Deposit",
                f"INR {t['loaner_deposit_amount']:.2f}",
            ])
        ldata.append([
            "Notice",
            "Loaner programmed for ~7 days. Please return on collection of repaired unit.",
        ])
        ltbl = Table(ldata, colWidths=[45 * mm, 125 * mm])
        ltbl.setStyle(TableStyle([
            ("FONT", (0, 0), (-1, -1), "Helvetica", 9),
            ("FONT", (0, 0), (0, -1), "Helvetica-Bold", 9),
            ("BOX", (0, 0), (-1, -1), 0.4, colors.grey),
            ("INNERGRID", (0, 0), (-1, -1), 0.2, colors.lightgrey),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        flow.append(ltbl)
        flow.append(Spacer(1, 4 * mm))

    flow.append(Paragraph(
        "<i>Estimated turnaround 10-14 working days, subject to "
        "manufacturer's availability of spares.</i>", body,
    ))
    flow.append(Spacer(1, 10 * mm))

    sig = Table(
        [["Received by (Clinic)", "Patient / Authorised Signatory"],
         ["", ""], ["", ""]],
        colWidths=[85 * mm, 85 * mm], rowHeights=[6 * mm, 14 * mm, 4 * mm],
    )
    sig.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, -1), "Helvetica", 9),
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9),
        ("LINEBELOW", (0, 1), (-1, 1), 0.4, colors.grey),
        ("ALIGN", (0, 0), (-1, -1), "LEFT"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    flow.append(sig)

    doc.build(flow)
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="service-note-{ticket_no}.pdf"',
            "Cache-Control": "no-store",
        },
    )



# ==================== Loaner Fleet Health (Phase 14 KPI) ====================

@router.get("/service/loaner-fleet-health")
async def loaner_fleet_health(
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """KPI tile for clinic owners — single glance at every loaner unit
    currently out in the field.

    Returns:
      • total ON_LOAN serials in the clinic (across all tickets)
      • bucketed days-out histogram (0-3 / 4-7 / 8-14 / 15+)
      • overdue list (issued > 7 days ago, not yet returned) with patient
        + serial + days-out for each ticket
      • total deposits collected (rupees out the door)
      • deposits still held (collected − refunded − forfeited)
    """
    clinic_id = user["clinic_id"]
    now = datetime.now(timezone.utc)

    # Count serials currently flagged ON_LOAN. Branch-scope-safe — clinic-wide
    # for owners/super_admin, branch-scoped via patch on ticket lookups below.
    on_loan_serials = await db.serial_items.count_documents(
        {"clinic_id": clinic_id, "state": "ON_LOAN"}
    )

    # Pull every open loaner-bearing ticket (loaner issued but not returned).
    cursor = db.service_tickets.find(
        {
            "clinic_id": clinic_id,
            "loaner_serial_id": {"$ne": None, "$exists": True},
            "loaner_returned_at": None,
        },
        {
            "_id": 0,
            "ticket_no": 1,
            "branch_id": 1,
            "patient_id": 1,
            "patient_name": 1,
            "patient_mobile": 1,
            "loaner_serial_id": 1,
            "loaner_issued_at": 1,
            "loaner_deposit_amount": 1,
        },
    )
    open_loans = await cursor.to_list(length=500)

    # Branch-filter for non-clinic-wide roles
    if user["role"] not in CLINIC_WIDE_ROLES:
        allowed = set(user.get("branch_ids") or [])
        open_loans = [t for t in open_loans if t.get("branch_id") in allowed]

    buckets = {"0-3d": 0, "4-7d": 0, "8-14d": 0, "15d+": 0}
    overdue: list[dict] = []
    for t in open_loans:
        iso = t.get("loaner_issued_at")
        if not iso:
            continue
        try:
            issued_at = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        days_out = (now - issued_at).days
        if days_out <= 3:
            buckets["0-3d"] += 1
        elif days_out <= 7:
            buckets["4-7d"] += 1
        elif days_out <= 14:
            buckets["8-14d"] += 1
        else:
            buckets["15d+"] += 1
        if days_out > 7:
            overdue.append({
                "ticket_no": t["ticket_no"],
                "patient_name": t.get("patient_name"),
                "patient_mobile": t.get("patient_mobile"),
                "loaner_serial_id": t["loaner_serial_id"],
                "days_out": days_out,
                "deposit_amount": t.get("loaner_deposit_amount"),
            })
    overdue.sort(key=lambda r: r["days_out"], reverse=True)

    # Deposits — sum across every ticket (open or closed) for this clinic.
    pipeline_collected: list = [
        {"$match": {"clinic_id": clinic_id,
                    "loaner_deposit_amount": {"$gt": 0}}},
        {"$group": {"_id": None,
                     "collected": {"$sum": "$loaner_deposit_amount"},
                     "refunded":  {"$sum": {"$cond": [{"$ne": ["$loaner_deposit_refunded_at", None]},
                                                       "$loaner_deposit_amount", 0]}},
                     "forfeited": {"$sum": {"$cond": [{"$ne": ["$loaner_deposit_forfeited_at", None]},
                                                       "$loaner_deposit_amount", 0]}}}},
    ]
    if user["role"] not in CLINIC_WIDE_ROLES:
        pipeline_collected[0]["$match"]["branch_id"] = {
            "$in": user.get("branch_ids") or []
        }
    agg = await db.service_tickets.aggregate(pipeline_collected).to_list(length=1)
    if agg:
        collected = float(agg[0].get("collected") or 0)
        refunded = float(agg[0].get("refunded") or 0)
        forfeited = float(agg[0].get("forfeited") or 0)
    else:
        collected = refunded = forfeited = 0.0
    deposits_held = max(0.0, collected - refunded - forfeited)

    return {
        "on_loan_count": on_loan_serials,
        "open_tickets": len(open_loans),
        "days_out_buckets": buckets,
        "overdue": overdue[:20],   # cap at 20 worst offenders
        "overdue_count": len(overdue),
        "deposits": {
            "collected": round(collected, 2),
            "refunded": round(refunded, 2),
            "forfeited": round(forfeited, 2),
            "held": round(deposits_held, 2),
        },
        "as_of": now.isoformat(),
    }
