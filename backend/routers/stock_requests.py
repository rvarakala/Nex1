"""Stock Requests — Branch → Head fulfilment workflow.

Complements `stock_transfers.py`:
    - `stock_transfers` = the physical dispatch/receive of goods.
    - `stock_requests`  = "please send us X", pending head's decision.

Workflow the user described:
    1. Branch raises a stock request (accessory / HA product + qty).
    2. Head sees a "Pending Requests" queue.
    3. Head clicks Fulfil → picks a source (head or any other branch
       that has stock).  A `stock_transfers` doc is auto-created and the
       request status transitions to `fulfilled`.
    4. If nothing in the group has stock, Head marks the request as
       `awaiting_po` — reminder to raise a PO with the vendor.  Once
       the vendor delivery arrives, Head fulfils normally.
    5. Head can also `decline` with a reason.

Only clinics inside the same `clinic_groups` doc can request/see each
other's requests. Data isolation stays intact.
"""
from __future__ import annotations

import io
import uuid
from datetime import datetime, timezone
from typing import List, Literal, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from motor.motor_asyncio import AsyncIOMotorGridFSBucket
from pydantic import BaseModel, Field

from auth import get_current_user, require_roles
from database import get_db
from utils.serde import serialize_datetime

router = APIRouter(prefix="/api/stock-requests")

RequestStatus = Literal["pending", "fulfilled", "declined", "awaiting_po", "cancelled"]
Urgency = Literal["normal", "urgent"]


class RequestLine(BaseModel):
    """One line in a stock request. Free-form product label so branches
    can request items they don't have in their local catalogue yet
    (e.g. a new Phonak model the head just started stocking).
    """
    product_label: str = Field(..., min_length=1, max_length=200)
    kind: Literal["ha", "accessory", "tool", "other"] = "accessory"
    product_id: Optional[str] = None       # if branch picked from their local catalog
    variant: Optional[str] = None
    qty: int = Field(1, ge=1)
    notes: Optional[str] = None
    # Structured device spec — colour, receiver/tube power, wire length.
    # Populated when kind='ha' so the head clinic knows the exact SKU
    # variant the branch needs from the vendor.
    spec: Optional[dict] = None


class CreateRequestPayload(BaseModel):
    lines: List[RequestLine] = Field(..., min_length=1)
    urgency: Urgency = "normal"
    reason: Optional[str] = None
    needed_by: Optional[str] = None        # ISO date string


class FulfilPayload(BaseModel):
    source_clinic_id: str                  # head or another branch
    # Optional pass-through so head can wire this straight into a real
    # `stock_transfers` doc from the same modal. If omitted we just
    # mark fulfilled and let head raise the transfer separately.
    create_transfer: bool = True
    courier_name: Optional[str] = None
    tracking_no: Optional[str] = None
    notes: Optional[str] = None


class DeclinePayload(BaseModel):
    reason: str = Field(..., min_length=2, max_length=500)


class MarkPoPayload(BaseModel):
    vendor_name: Optional[str] = None
    po_no: Optional[str] = None
    expected_at: Optional[str] = None      # ISO date
    notes: Optional[str] = None


# ─── helpers ──────────────────────────────────────────────────────────────
async def _find_group_containing(db, clinic_id: str) -> Optional[dict]:
    return await db.clinic_groups.find_one(
        {"$or": [{"head_clinic_id": clinic_id}, {"member_clinic_ids": clinic_id}]},
        {"_id": 0},
    )


async def _accessible_clinic_ids_for_requests(db, user) -> set[str]:
    """The set of clinics whose requests the caller may see. Includes
    the active clinic + every other clinic in the same group.
    """
    ids: set[str] = {user["clinic_id"]}
    group = await _find_group_containing(db, user["clinic_id"])
    if group:
        ids.add(group["head_clinic_id"])
        for c in (group.get("member_clinic_ids") or []):
            ids.add(c)
    # Also include user-level additional clinic grants (super_admins etc.)
    for cid in (user.get("additional_clinic_ids") or []):
        ids.add(cid)
    return ids


async def _clinic_name(db, clinic_id: str) -> str:
    doc = await db.clinics.find_one({"clinic_id": clinic_id}, {"_id": 0, "name": 1})
    return (doc or {}).get("name") or clinic_id


async def _apply_stock_request_decision(
    db, req: dict, decision: str, actor_user_id: str, note: Optional[str] = None,
) -> None:
    """Mirror a fulfil/decline on a stock_request into any linked
    Custom HA order so the audiologist's view stays consistent.

    - fulfil  → order → `sent_to_vendor` (head owner has approved and
      will place/route the actual manufacturing order)
    - decline → order → `cancelled` (head owner rejected; branch must
      inform patient / refund advance separately)
    """
    order_id = req.get("linked_custom_ha_order_id")
    if not order_id:
        return
    if decision == "fulfil":
        next_status = "sent_to_vendor"
        history_note = "Head clinic approved via Stock Request"
    elif decision == "decline":
        next_status = "cancelled"
        history_note = f"Head clinic declined via Stock Request: {note}" if note else "Head clinic declined via Stock Request"
    else:
        return
    now_iso = datetime.now(timezone.utc).isoformat()
    await db.custom_ha_orders.update_one(
        {"order_id": order_id},
        {"$set": {"status": next_status, "updated_at": now_iso},
         "$push": {"history": {
             "at": now_iso,
             "status": next_status,
             "actor_user_id": actor_user_id,
             "note": history_note,
         }}},
    )


def _strip(row: dict) -> dict:
    row.pop("_id", None)
    return row


# ─── endpoints ────────────────────────────────────────────────────────────
@router.post("")
async def create_request(
    payload: CreateRequestPayload,
    user=Depends(require_roles("front_desk", "accounts", "clinic_owner", "inventory_manager")),
    db=Depends(get_db),
):
    """Branch (or head) raises a request. Head clinic requests are
    allowed too — occasionally head realises another branch has
    surplus stock and wants a reverse transfer.
    """
    group = await _find_group_containing(db, user["clinic_id"])
    if not group:
        raise HTTPException(
            status_code=409,
            detail={"code": "no_group", "message": "Your clinic isn't part of a group yet."},
        )

    now = datetime.now(timezone.utc)
    req_id = f"REQ-{uuid.uuid4().hex[:10].upper()}"
    doc = {
        "request_id": req_id,
        "clinic_id": user["clinic_id"],
        "clinic_name": await _clinic_name(db, user["clinic_id"]),
        "group_id": group["group_id"],
        "head_clinic_id": group["head_clinic_id"],
        "requested_by_user_id": user["user_id"],
        "requested_by_role": user.get("role"),
        "lines": [ln.model_dump() for ln in payload.lines],
        "urgency": payload.urgency,
        "reason": payload.reason,
        "needed_by": payload.needed_by,
        "status": "pending",
        "fulfilled_by_user_id": None,
        "fulfilled_at": None,
        "fulfilled_from_clinic_id": None,
        "linked_transfer_id": None,
        "decline_reason": None,
        "po_details": None,
        "created_at": now,
        "updated_at": now,
    }
    await db.stock_requests.insert_one(serialize_datetime(doc))
    await db.activity_logs.insert_one(serialize_datetime({
        "clinic_id": user["clinic_id"], "user_id": user["user_id"],
        "action": "stock_request.create", "request_id": req_id,
        "urgency": payload.urgency, "line_count": len(payload.lines), "at": now,
    }))
    return _strip(doc)


@router.get("")
async def list_requests(
    status: Optional[str] = None,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Return every request within the caller's group. Head sees all
    branches' requests + their own. Branches see only their own.
    """
    ids = await _accessible_clinic_ids_for_requests(db, user)
    group = await _find_group_containing(db, user["clinic_id"])
    q: dict = {}
    if group and user["clinic_id"] == group["head_clinic_id"]:
        # Head sees every clinic in the group.
        q["clinic_id"] = {"$in": list(ids)}
    else:
        # Branch users see only their own requests.
        q["clinic_id"] = user["clinic_id"]
    if status:
        q["status"] = status
    rows = await db.stock_requests.find(q, {"_id": 0}).sort("created_at", -1).to_list(500)
    return rows


@router.get("/{request_id}")
async def get_request(request_id: str, user=Depends(get_current_user), db=Depends(get_db)):
    ids = await _accessible_clinic_ids_for_requests(db, user)
    row = await db.stock_requests.find_one({"request_id": request_id, "clinic_id": {"$in": list(ids)}}, {"_id": 0})
    if not row:
        raise HTTPException(status_code=404, detail="Request not found")
    return row


@router.post("/{request_id}/fulfill")
async def fulfill_request(
    request_id: str,
    payload: FulfilPayload,
    user=Depends(require_roles("clinic_owner")),
    db=Depends(get_db),
):
    """Head marks a request fulfilled from a specific source clinic.
    Optionally wires a real `stock_transfers` doc so the physical
    dispatch is tracked in the same click.
    """
    group = await _find_group_containing(db, user["clinic_id"])
    if not group or user["clinic_id"] != group["head_clinic_id"]:
        raise HTTPException(status_code=403, detail="Only the head clinic owner can fulfil requests")

    req = await db.stock_requests.find_one(
        {"request_id": request_id, "group_id": group["group_id"]}, {"_id": 0}
    )
    if not req:
        raise HTTPException(status_code=404, detail="Request not found in your group")
    if req["status"] not in ("pending", "awaiting_po"):
        raise HTTPException(status_code=409, detail=f"Request is already {req['status']}")

    # Source must be inside the group.
    all_ids = {group["head_clinic_id"], *(group.get("member_clinic_ids") or [])}
    if payload.source_clinic_id not in all_ids:
        raise HTTPException(status_code=400, detail="source_clinic_id must belong to your group")
    if payload.source_clinic_id == req["clinic_id"]:
        raise HTTPException(status_code=400, detail="A branch cannot fulfil its own request")

    # Optionally kick off a stock transfer doc — head uses the existing
    # transfer machinery from there (pick serials, courier, dispatch).
    linked_transfer_id: Optional[str] = None
    if payload.create_transfer:
        transfer_id = f"TRF-{uuid.uuid4().hex[:10].upper()}"
        src = await db.clinics.find_one({"clinic_id": payload.source_clinic_id}) or {}
        dst = await db.clinics.find_one({"clinic_id": req["clinic_id"]}) or {}
        now = datetime.now(timezone.utc)
        await db.stock_transfers.insert_one(serialize_datetime({
            "transfer_id": transfer_id,
            "challan_no": "",  # assigned at dispatch
            "from_clinic_id": payload.source_clinic_id,
            "from_clinic_name": src.get("name") or payload.source_clinic_id,
            "from_clinic_address": src.get("address"),
            "from_clinic_gstin": src.get("gstin"),
            "to_clinic_id": req["clinic_id"],
            "to_clinic_name": dst.get("name") or req["clinic_id"],
            "to_clinic_address": dst.get("address"),
            "to_clinic_gstin": dst.get("gstin"),
            "status": "draft",
            "purpose": "replenishment",
            "lines": [],
            # Seed accessory lines from the request so head just needs
            # to confirm quantities before dispatch — no re-typing.
            "accessory_lines": [
                {
                    "product_id": ln.get("product_id") or "",
                    "product_label": ln["product_label"],
                    "variant": ln.get("variant"),
                    "qty": ln["qty"],
                }
                for ln in req["lines"] if ln.get("kind") in (None, "accessory", "other")
            ],
            "courier_name": payload.courier_name,
            "tracking_no": payload.tracking_no,
            "notes": payload.notes or f"Fulfils request {request_id}",
            "created_at": now,
            "created_by_user_id": user["user_id"],
            "updated_at": now,
            "linked_request_id": request_id,
        }))
        linked_transfer_id = transfer_id

    now = datetime.now(timezone.utc)
    await db.stock_requests.update_one(
        {"request_id": request_id},
        {"$set": {
            "status": "fulfilled",
            "fulfilled_by_user_id": user["user_id"],
            "fulfilled_at": now.isoformat(),
            "fulfilled_from_clinic_id": payload.source_clinic_id,
            "linked_transfer_id": linked_transfer_id,
            "updated_at": now.isoformat(),
        }},
    )
    await db.activity_logs.insert_one(serialize_datetime({
        "clinic_id": user["clinic_id"], "user_id": user["user_id"],
        "action": "stock_request.fulfill", "request_id": request_id,
        "source_clinic_id": payload.source_clinic_id,
        "linked_transfer_id": linked_transfer_id, "at": now,
    }))
    # Approval mirrored on the linked Custom HA order (no-op for regular
    # requests that don't carry a `linked_custom_ha_order_id`).
    await _apply_stock_request_decision(
        db, req, "fulfil", actor_user_id=user["user_id"],
    )
    return await get_request(request_id, user=user, db=db)


@router.post("/{request_id}/decline")
async def decline_request(
    request_id: str,
    payload: DeclinePayload,
    user=Depends(require_roles("clinic_owner")),
    db=Depends(get_db),
):
    group = await _find_group_containing(db, user["clinic_id"])
    if not group or user["clinic_id"] != group["head_clinic_id"]:
        raise HTTPException(status_code=403, detail="Only the head clinic owner can decline")
    req = await db.stock_requests.find_one({"request_id": request_id, "group_id": group["group_id"]})
    if not req:
        raise HTTPException(status_code=404, detail="Request not found in your group")
    if req["status"] not in ("pending", "awaiting_po"):
        raise HTTPException(status_code=409, detail=f"Request is already {req['status']}")
    now = datetime.now(timezone.utc)
    await db.stock_requests.update_one(
        {"request_id": request_id},
        {"$set": {"status": "declined", "decline_reason": payload.reason, "updated_at": now.isoformat()}},
    )
    await db.activity_logs.insert_one(serialize_datetime({
        "clinic_id": user["clinic_id"], "user_id": user["user_id"],
        "action": "stock_request.decline", "request_id": request_id,
        "reason": payload.reason, "at": now,
    }))
    # Rejection mirrored on the linked Custom HA order (no-op otherwise).
    await _apply_stock_request_decision(
        db, req, "decline", actor_user_id=user["user_id"], note=payload.reason,
    )
    return await get_request(request_id, user=user, db=db)


@router.post("/{request_id}/mark-po")
async def mark_awaiting_po(
    request_id: str,
    payload: MarkPoPayload,
    user=Depends(require_roles("clinic_owner")),
    db=Depends(get_db),
):
    """Head marks that no clinic in the group has stock, so a PO
    will be raised with the vendor. Keeps the request open; head can
    fulfil it later once the vendor delivery arrives.
    """
    group = await _find_group_containing(db, user["clinic_id"])
    if not group or user["clinic_id"] != group["head_clinic_id"]:
        raise HTTPException(status_code=403, detail="Only the head clinic owner")
    req = await db.stock_requests.find_one({"request_id": request_id, "group_id": group["group_id"]})
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    if req["status"] not in ("pending", "awaiting_po"):
        raise HTTPException(status_code=409, detail=f"Cannot mark {req['status']} request as PO")
    now = datetime.now(timezone.utc)
    await db.stock_requests.update_one(
        {"request_id": request_id},
        {"$set": {
            "status": "awaiting_po",
            "po_details": payload.model_dump(),
            "updated_at": now.isoformat(),
        }},
    )
    await db.activity_logs.insert_one(serialize_datetime({
        "clinic_id": user["clinic_id"], "user_id": user["user_id"],
        "action": "stock_request.mark_po", "request_id": request_id,
        "vendor_name": payload.vendor_name, "at": now,
    }))
    return await get_request(request_id, user=user, db=db)


@router.post("/{request_id}/cancel")
async def cancel_request(
    request_id: str,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """A branch can cancel its own pending request. Head can cancel
    anything within the group.
    """
    ids = await _accessible_clinic_ids_for_requests(db, user)
    req = await db.stock_requests.find_one({"request_id": request_id, "clinic_id": {"$in": list(ids)}})
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    if req["status"] not in ("pending", "awaiting_po"):
        raise HTTPException(status_code=409, detail=f"Request already {req['status']}")

    group = await _find_group_containing(db, user["clinic_id"])
    is_head = bool(group and user["clinic_id"] == group["head_clinic_id"])
    is_owner = req["clinic_id"] == user["clinic_id"]
    if not (is_head or is_owner):
        raise HTTPException(status_code=403, detail="You can only cancel your own request")

    now = datetime.now(timezone.utc)
    await db.stock_requests.update_one(
        {"request_id": request_id},
        {"$set": {"status": "cancelled", "updated_at": now.isoformat()}},
    )
    return await get_request(request_id, user=user, db=db)



# ── Audiogram passthrough for linked Custom HA orders ─────────────────
# The head owner needs to preview the branch's audiogram to sanity-check
# the fit brief before approving. The file itself sits in the branch's
# tenant, so we serve it via a stock_request-scoped endpoint that only
# allows callers who can see the request (owner clinic or head clinic).
@router.get("/{request_id}/audiogram")
async def stock_request_audiogram(
    request_id: str,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    ids = await _accessible_clinic_ids_for_requests(db, user)
    req = await db.stock_requests.find_one(
        {"request_id": request_id, "clinic_id": {"$in": list(ids)}},
        {"_id": 0, "custom_ha_details": 1},
    )
    if not req:
        raise HTTPException(404, "Request not found")
    details = (req or {}).get("custom_ha_details") or {}
    fs_id = details.get("audiogram_fs_id")
    if not fs_id:
        raise HTTPException(404, "No audiogram attached to this request")

    bucket = AsyncIOMotorGridFSBucket(db, bucket_name="custom_ha_audiograms")
    try:
        stream = await bucket.open_download_stream(ObjectId(fs_id))
    except Exception:  # noqa: BLE001
        raise HTTPException(404, "Audiogram file missing")
    data = await stream.read()
    headers = {
        "Content-Disposition": f'inline; filename="{details.get("audiogram_filename") or "audiogram"}"',
        "Cache-Control": "private, max-age=300",
    }
    return StreamingResponse(
        io.BytesIO(data),
        media_type=details.get("audiogram_content_type") or "application/pdf",
        headers=headers,
    )
