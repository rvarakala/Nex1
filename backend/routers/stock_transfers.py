"""Inter-clinic stock-transfer router.

Endpoints
---------
POST   /api/stock-transfers                   create draft (source clinic)
POST   /api/stock-transfers/{id}/dispatch     mark dispatched, lock serials → RESERVED
POST   /api/stock-transfers/{id}/receive      destination signs + receives
POST   /api/stock-transfers/{id}/cancel       roll back (admin / owner only)
GET    /api/stock-transfers                   list with direction + status filters
GET    /api/stock-transfers/{id}              detail
POST   /api/stock-transfers/{id}/signature    upload a drawn-signature PNG (GridFS)
GET    /api/stock-transfers/{id}/signature    fetch the captured signature

Auth model
----------
The signed-in user must have the `from_clinic_id` (for create/dispatch) and
`to_clinic_id` (for receive) within their accessible clinics
(`primary_clinic_id` ∪ `additional_clinic_ids`). Listing is automatically
filtered to clinics the user has access to.
"""
from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import List, Optional
from uuid import uuid4

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query, Response
from motor.motor_asyncio import AsyncIOMotorGridFSBucket

from auth import get_current_user, require_roles
from database import get_db
from models_transfers import (
    StockTransfer, StockTransferCreate, StockTransferDispatch,
    StockTransferReceive, StockTransferCancel,
)
from utils.serde import serialize_datetime, deserialize_datetime
from utils.ha_states import transition_serial


router = APIRouter(prefix="/api/stock-transfers")
GRIDFS_BUCKET = "transfer_signatures"


# ============================================================================
# Helpers
# ============================================================================
async def _accessible_clinic_ids(user, db) -> set[str]:
    """Returns every clinic_id the signed-in user can act on.
    Falls back to a fresh user-doc lookup so additional_clinic_ids granted
    after token issuance still work without forcing a re-login.
    """
    udoc = await db.users.find_one(
        {"user_id": user["user_id"]},
        {"_id": 0, "primary_clinic_id": 1, "additional_clinic_ids": 1, "clinic_id": 1, "role": 1},
    ) or {}
    primary = udoc.get("primary_clinic_id") or udoc.get("clinic_id") or user["clinic_id"]
    extras = udoc.get("additional_clinic_ids") or []
    out = {primary, *extras, user["clinic_id"]}
    # Internal/super-admin roles can act on any tenant.
    if user.get("role") in {"super_admin", "founder"}:
        rows = await db.clinics.find({}, {"_id": 0, "clinic_id": 1}).to_list(2000)
        out.update(c["clinic_id"] for c in rows)
    return {c for c in out if c}


async def _next_challan_no(db, clinic_id: str) -> str:
    """Atomic counter — `challan:<clinic>:<year>` increments on dispatch."""
    year = datetime.now(timezone.utc).year
    key = f"challan:{clinic_id}:{year}"
    res = await db.counters.find_one_and_update(
        {"_id": key},
        {"$inc": {"seq": 1}},
        upsert=True, return_document=True,
    )
    seq = (res or {}).get("seq", 1)
    return f"DC/{year}/{str(seq).zfill(4)}"


async def _hydrate_clinic_block(db, clinic_id: str) -> dict:
    """Picks just the fields the challan template needs."""
    c = await db.clinics.find_one(
        {"clinic_id": clinic_id},
        {"_id": 0, "name": 1, "address": 1, "city": 1, "state": 1, "pincode": 1,
         "gstin": 1, "phone": 1},
    ) or {}
    addr_parts = [c.get("address"), c.get("city"), c.get("state"), c.get("pincode")]
    return {
        "name": c.get("name", ""),
        "address": ", ".join([p for p in addr_parts if p]),
        "gstin": c.get("gstin"),
    }


def _strip(t: dict) -> dict:
    """Drop Mongo `_id` from response payload."""
    if t and "_id" in t:
        t.pop("_id", None)
    return t


# ============================================================================
# CREATE — draft transfer
# ============================================================================
@router.post("", response_model=StockTransfer)
async def create_transfer(payload: StockTransferCreate,
                          user=Depends(require_roles("inventory_manager", "clinic_owner")),
                          db=Depends(get_db)):
    if not payload.serial_ids and not payload.accessory_lines:
        raise HTTPException(status_code=400, detail="Add at least one serial or accessory line")

    src_clinic_id = user["clinic_id"]
    dst_clinic_id = payload.to_clinic_id
    if src_clinic_id == dst_clinic_id:
        raise HTTPException(status_code=400, detail="Source and destination clinics must differ")

    accessible = await _accessible_clinic_ids(user, db)
    if dst_clinic_id not in accessible:
        raise HTTPException(status_code=403, detail="You don't have access to the destination clinic")

    # Resolve every serial — must belong to the source clinic and be IN_STOCK.
    serials = []
    for sid in payload.serial_ids:
        si = await db.serial_items.find_one({"serial_id": sid, "clinic_id": src_clinic_id}, {"_id": 0})
        if not si:
            raise HTTPException(status_code=404, detail=f"Serial {sid} not found in source clinic")
        if si["state"] != "IN_STOCK":
            raise HTTPException(
                status_code=409,
                detail=f"Serial {si.get('serial_no')} is not IN_STOCK (current: {si['state']})",
            )
        prod = await db.ha_products.find_one(
            {"product_id": si["product_id"]}, {"_id": 0, "brand": 1, "model": 1},
        ) or {}
        serials.append({
            "serial_id": sid,
            "serial_no": si.get("serial_no", ""),
            "product_id": si["product_id"],
            "product_label": f"{prod.get('brand', '')} {prod.get('model', '')}".strip(),
            "qty": 1,
        })

    src_block = await _hydrate_clinic_block(db, src_clinic_id)
    dst_block = await _hydrate_clinic_block(db, dst_clinic_id)

    obj = StockTransfer(
        challan_no="",  # assigned on dispatch
        from_clinic_id=src_clinic_id,
        from_clinic_name=src_block["name"],
        from_clinic_address=src_block["address"],
        from_clinic_gstin=src_block["gstin"],
        from_branch_id=None,
        to_clinic_id=dst_clinic_id,
        to_clinic_name=dst_block["name"],
        to_clinic_address=dst_block["address"],
        to_clinic_gstin=dst_block["gstin"],
        to_branch_id=payload.to_branch_id,
        purpose=payload.purpose,
        lines=serials,
        accessory_lines=[a.model_dump() for a in payload.accessory_lines],
        courier_name=payload.courier_name,
        tracking_no=payload.tracking_no,
        notes=payload.notes,
        created_by_user_id=user["user_id"],
    )
    await db.stock_transfers.insert_one(serialize_datetime(obj.model_dump()))
    return obj


# ============================================================================
# DISPATCH — lock serials, assign challan number
# ============================================================================
@router.post("/{transfer_id}/dispatch", response_model=StockTransfer)
async def dispatch_transfer(transfer_id: str, payload: StockTransferDispatch,
                            user=Depends(require_roles("inventory_manager", "clinic_owner")),
                            db=Depends(get_db)):
    t = await db.stock_transfers.find_one({"transfer_id": transfer_id}, {"_id": 0})
    if not t:
        raise HTTPException(status_code=404, detail="Transfer not found")
    if t["status"] != "draft":
        raise HTTPException(status_code=409, detail=f"Cannot dispatch a transfer in status '{t['status']}'")

    accessible = await _accessible_clinic_ids(user, db)
    if t["from_clinic_id"] not in accessible:
        raise HTTPException(status_code=403, detail="You can't dispatch from this clinic")

    # Lock every serial: IN_STOCK → RESERVED (kept on source clinic).
    for ln in t.get("lines", []):
        await transition_serial(
            db, ln["serial_id"], "RESERVED", actor_user_id=user["user_id"],
            ref_doc={"kind": "stock_transfer", "id": transfer_id, "challan_no": "(pending)"},
            note=f"Locked for inter-clinic transfer to {t['to_clinic_name']}",
        )

    # Deduct batch/accessory qty from source now (dispatch = physical
    # departure). If any line lacks sufficient stock we 409 BEFORE touching
    # anything else so the draft can be adjusted without half-effects.
    accessory_lines = t.get("accessory_lines") or []
    for al in accessory_lines:
        match = {
            "clinic_id": t["from_clinic_id"],
            "product_id": al["product_id"],
        }
        if t.get("from_branch_id"):
            match["branch_id"] = t["from_branch_id"]
        if al.get("variant") is not None:
            match["variant"] = al["variant"]
        row = await db.accessory_stock.find_one(match, {"_id": 0})
        if not row:
            raise HTTPException(
                status_code=404,
                detail=f"No source stock row for {al.get('product_label') or al['product_id']}"
                       + (f" · {al['variant']}" if al.get('variant') else ""),
            )
        on_hand = int(row.get("qty_on_hand") or 0)
        if on_hand < int(al["qty"]):
            raise HTTPException(
                status_code=409,
                detail=f"Insufficient stock of {al.get('product_label') or al['product_id']}"
                       + (f" ({al['variant']})" if al.get('variant') else "")
                       + f" — need {al['qty']}, have {on_hand}",
            )
    for al in accessory_lines:
        match = {"clinic_id": t["from_clinic_id"], "product_id": al["product_id"]}
        if t.get("from_branch_id"):
            match["branch_id"] = t["from_branch_id"]
        if al.get("variant") is not None:
            match["variant"] = al["variant"]
        await db.accessory_stock.update_one(
            match,
            {"$inc": {"qty_on_hand": -int(al["qty"])},
             "$set": {"updated_at": datetime.now(timezone.utc).isoformat()}},
        )

    challan_no = await _next_challan_no(db, t["from_clinic_id"])
    now = datetime.now(timezone.utc)
    update = {
        "status": "dispatched",
        "challan_no": challan_no,
        "dispatched_at": now,
        "dispatched_by_user_id": user["user_id"],
        "dispatched_by_name": (await db.users.find_one({"user_id": user["user_id"]}, {"_id": 0, "name": 1}) or {}).get("name", ""),
        "courier_name": payload.courier_name or t.get("courier_name"),
        "tracking_no": payload.tracking_no or t.get("tracking_no"),
        "notes": payload.notes or t.get("notes"),
        "updated_at": now,
    }
    await db.stock_transfers.update_one(
        {"transfer_id": transfer_id}, {"$set": serialize_datetime(update)},
    )
    return deserialize_datetime(_strip(await db.stock_transfers.find_one({"transfer_id": transfer_id})))


# ============================================================================
# RECEIVE — destination signs and accepts; flips inventory
# ============================================================================
@router.post("/{transfer_id}/receive", response_model=StockTransfer)
async def receive_transfer(transfer_id: str, payload: StockTransferReceive,
                           user=Depends(require_roles("inventory_manager", "clinic_owner", "front_desk")),
                           db=Depends(get_db)):
    t = await db.stock_transfers.find_one({"transfer_id": transfer_id}, {"_id": 0})
    if not t:
        raise HTTPException(status_code=404, detail="Transfer not found")
    if t["status"] != "dispatched":
        raise HTTPException(status_code=409, detail=f"Cannot receive a transfer in status '{t['status']}'")

    # The receiver must be acting from the destination clinic context.
    if user["clinic_id"] != t["to_clinic_id"]:
        accessible = await _accessible_clinic_ids(user, db)
        if t["to_clinic_id"] not in accessible:
            raise HTTPException(status_code=403, detail="You can't receive at this clinic")

    # Build a `serial_id → condition` map from the payload. Missing entries
    # default to "ok" for backwards compat with old receive callers.
    disp = {r.serial_id: r for r in (payload.line_receipts or [])}
    any_damaged = False
    any_missing = False

    # Atomically flip every serial per its condition. Damaged units land
    # in DAMAGED state (still at the destination — they're physically here
    # but unsellable). Missing units stay on the source clinic — the head
    # investigates before the branch signs. OK is the historical path:
    # RESERVED → IN_STOCK + rewrite clinic/branch.
    damaged_serial_ids: list[str] = []
    missing_serial_ids: list[str] = []
    for ln in t.get("lines", []):
        sid = ln["serial_id"]
        cond = disp.get(sid)
        condition = cond.condition if cond else "ok"

        if condition == "missing":
            any_missing = True
            missing_serial_ids.append(sid)
            # Don't transition — the serial stays RESERVED to the source so
            # the head sees the loss and can investigate. It'll be moved
            # back manually or written off after the incident review.
            continue
        target_state = "DAMAGED" if condition == "damaged" else "IN_STOCK"
        note_bits = [f"Received from {t['from_clinic_name']}"]
        if condition == "damaged":
            any_damaged = True
            damaged_serial_ids.append(sid)
            note_bits.append("Marked DAMAGED at receipt")
            if cond and cond.damage_notes:
                note_bits.append(cond.damage_notes)
        await transition_serial(
            db, sid, target_state, actor_user_id=user["user_id"],
            ref_doc={"kind": "stock_transfer_receive", "id": transfer_id, "challan_no": t["challan_no"]},
            note=" · ".join(note_bits),
        )
        await db.serial_items.update_one(
            {"serial_id": sid},
            {"$set": {
                "clinic_id": t["to_clinic_id"],
                "branch_id": t.get("to_branch_id"),
            }},
        )

    # Credit destination accessory_stock for each batch line. When the
    # destination clinic doesn't already carry a stock row for the SKU
    # (fresh SKU at this branch), fabricate one so the receipt lands
    # cleanly. This mirrors how serial lines rewrite `clinic_id`.
    for al in (t.get("accessory_lines") or []):
        match = {"clinic_id": t["to_clinic_id"], "product_id": al["product_id"]}
        if t.get("to_branch_id"):
            match["branch_id"] = t["to_branch_id"]
        if al.get("variant") is not None:
            match["variant"] = al["variant"]
        existing = await db.accessory_stock.find_one(match, {"_id": 0, "sku_id": 1})
        if existing:
            await db.accessory_stock.update_one(
                match,
                {"$inc": {"qty_on_hand": int(al["qty"])},
                 "$set": {"updated_at": datetime.now(timezone.utc).isoformat()}},
            )
        else:
            # Import here (not at top) to avoid touching startup imports.
            from models_ha import AccessoryStock
            row = AccessoryStock(
                clinic_id=t["to_clinic_id"],
                branch_id=t.get("to_branch_id"),
                product_id=al["product_id"],
                variant=al.get("variant"),
                qty_on_hand=int(al["qty"]),
                reorder_level=0,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            await db.accessory_stock.insert_one(serialize_datetime(row.model_dump()))

    now = datetime.now(timezone.utc)
    # Terminal status reflects what actually happened.
    status = "received"
    if any_missing:
        status = "received_partial"
    elif any_damaged:
        status = "received_with_damage"
    update = {
        "status": status,
        "received_at": now,
        "received_by_user_id": user["user_id"],
        "received_by_name": payload.received_by_name,
        "received_by_role": payload.received_by_role,
        "signature_image_fs_id": payload.signature_image_fs_id,
        "short_shipment_notes": payload.short_shipment_notes,
        "damaged_serial_ids": damaged_serial_ids,
        "missing_serial_ids": missing_serial_ids,
        "updated_at": now,
    }
    await db.stock_transfers.update_one(
        {"transfer_id": transfer_id}, {"$set": serialize_datetime(update)},
    )
    return deserialize_datetime(_strip(await db.stock_transfers.find_one({"transfer_id": transfer_id})))


# ============================================================================
# CANCEL — admin/owner override; only valid before receive
# ============================================================================
@router.post("/{transfer_id}/cancel", response_model=StockTransfer)
async def cancel_transfer(transfer_id: str, payload: StockTransferCancel,
                          user=Depends(get_current_user), db=Depends(get_db)):
    t = await db.stock_transfers.find_one({"transfer_id": transfer_id}, {"_id": 0})
    if not t:
        raise HTTPException(status_code=404, detail="Transfer not found")
    if t["status"] not in ("draft", "dispatched"):
        raise HTTPException(status_code=409, detail=f"Cannot cancel a transfer in status '{t['status']}'")

    accessible = await _accessible_clinic_ids(user, db)
    if t["from_clinic_id"] not in accessible:
        raise HTTPException(status_code=403, detail="Only the source clinic owner/admin can cancel")
    if user.get("role") not in {"clinic_owner", "super_admin", "founder"}:
        raise HTTPException(status_code=403, detail="Cancellation requires owner / admin role")

    # If already dispatched, free the serials back into stock at source.
    if t["status"] == "dispatched":
        for ln in t.get("lines", []):
            try:
                await transition_serial(
                    db, ln["serial_id"], "IN_STOCK", actor_user_id=user["user_id"],
                    ref_doc={"kind": "stock_transfer_cancel", "id": transfer_id},
                    note=f"Transfer cancelled: {payload.reason}",
                )
            except HTTPException:
                # Already returned by another flow — ignore.
                pass
        # Same for batch/accessory lines — credit the qty back to source
        # so the cancellation is fully reversible.
        for al in (t.get("accessory_lines") or []):
            match = {"clinic_id": t["from_clinic_id"], "product_id": al["product_id"]}
            if t.get("from_branch_id"):
                match["branch_id"] = t["from_branch_id"]
            if al.get("variant") is not None:
                match["variant"] = al["variant"]
            await db.accessory_stock.update_one(
                match,
                {"$inc": {"qty_on_hand": int(al["qty"])},
                 "$set": {"updated_at": datetime.now(timezone.utc).isoformat()}},
            )

    now = datetime.now(timezone.utc)
    await db.stock_transfers.update_one(
        {"transfer_id": transfer_id},
        {"$set": serialize_datetime({
            "status": "cancelled",
            "cancelled_at": now,
            "cancelled_by_user_id": user["user_id"],
            "cancelled_reason": payload.reason,
            "updated_at": now,
        })},
    )
    return deserialize_datetime(_strip(await db.stock_transfers.find_one({"transfer_id": transfer_id})))


# ============================================================================
# LIST + DETAIL
# ============================================================================
@router.get("", response_model=List[StockTransfer])
async def list_transfers(
    direction: Optional[str] = Query(None, description="incoming | outgoing | all"),
    status: Optional[str] = None,
    limit: int = 200,
    user=Depends(get_current_user), db=Depends(get_db),
):
    accessible = await _accessible_clinic_ids(user, db)
    if not accessible:
        return []

    cur_clinic = user["clinic_id"]
    if direction == "incoming":
        q: dict = {"to_clinic_id": cur_clinic}
    elif direction == "outgoing":
        q = {"from_clinic_id": cur_clinic}
    else:
        q = {"$or": [
            {"from_clinic_id": {"$in": list(accessible)}},
            {"to_clinic_id":   {"$in": list(accessible)}},
        ]}
    if status:
        q["status"] = status

    rows = await db.stock_transfers.find(q, {"_id": 0}).sort("created_at", -1).to_list(limit)
    return [deserialize_datetime(r) for r in rows]


@router.get("/{transfer_id}", response_model=StockTransfer)
async def get_transfer(transfer_id: str,
                       user=Depends(get_current_user), db=Depends(get_db)):
    t = await db.stock_transfers.find_one({"transfer_id": transfer_id}, {"_id": 0})
    if not t:
        raise HTTPException(status_code=404, detail="Transfer not found")
    accessible = await _accessible_clinic_ids(user, db)
    if t["from_clinic_id"] not in accessible and t["to_clinic_id"] not in accessible:
        raise HTTPException(status_code=403, detail="Not allowed to view this transfer")
    # Flag whether the receiving user has opted-in to stamping their seal on
    # challans. The frontend uses this to decide whether to fetch the seal
    # image — a cheap projection on `users.seal_include_on` keeps the doc
    # render path purely declarative.
    if t.get("received_by_user_id"):
        rdoc = await db.users.find_one(
            {"user_id": t["received_by_user_id"]},
            {"_id": 0, "seal_image_fs_id": 1, "seal_include_on": 1},
        ) or {}
        prefs = list(rdoc.get("seal_include_on") or [])
        t["received_by_seal_eligible"] = (
            "challan" in prefs and bool(rdoc.get("seal_image_fs_id"))
        )
    return deserialize_datetime(t)


# ============================================================================
# SIGNATURE — drawn PNG upload + fetch (GridFS)
# ============================================================================
@router.post("/{transfer_id}/signature")
async def upload_signature(transfer_id: str,
                           file: UploadFile = File(...),
                           user=Depends(get_current_user), db=Depends(get_db)):
    t = await db.stock_transfers.find_one({"transfer_id": transfer_id}, {"_id": 0})
    if not t:
        raise HTTPException(status_code=404, detail="Transfer not found")
    accessible = await _accessible_clinic_ids(user, db)
    if t["to_clinic_id"] not in accessible:
        raise HTTPException(status_code=403, detail="Only destination staff can upload signature")

    blob = await file.read()
    if not blob:
        raise HTTPException(status_code=400, detail="Empty signature payload")
    if len(blob) > 1_500_000:  # 1.5 MB ceiling — drawn PNGs are tiny
        raise HTTPException(status_code=413, detail="Signature image too large (max 1.5 MB)")

    bucket = AsyncIOMotorGridFSBucket(db, bucket_name=GRIDFS_BUCKET)
    # Replace any prior signature for this transfer.
    if t.get("signature_image_fs_id"):
        try:
            await bucket.delete(ObjectId(t["signature_image_fs_id"]))
        except Exception:
            pass
    fs_id = await bucket.upload_from_stream(
        f"sig-{transfer_id}-{uuid4().hex[:8]}.png",
        io.BytesIO(blob),
        metadata={"transfer_id": transfer_id, "uploaded_by": user["user_id"]},
    )
    await db.stock_transfers.update_one(
        {"transfer_id": transfer_id},
        {"$set": {"signature_image_fs_id": str(fs_id), "updated_at": datetime.now(timezone.utc).isoformat()}},
    )
    return {"signature_image_fs_id": str(fs_id)}


@router.get("/{transfer_id}/signature")
async def fetch_signature(transfer_id: str,
                          user=Depends(get_current_user), db=Depends(get_db)):
    t = await db.stock_transfers.find_one({"transfer_id": transfer_id}, {"_id": 0})
    if not t or not t.get("signature_image_fs_id"):
        raise HTTPException(status_code=404, detail="No signature on this transfer")
    accessible = await _accessible_clinic_ids(user, db)
    if t["from_clinic_id"] not in accessible and t["to_clinic_id"] not in accessible:
        raise HTTPException(status_code=403, detail="Not allowed")
    bucket = AsyncIOMotorGridFSBucket(db, bucket_name=GRIDFS_BUCKET)
    try:
        stream = await bucket.open_download_stream(ObjectId(t["signature_image_fs_id"]))
        data = await stream.read()
    except Exception:
        raise HTTPException(status_code=404, detail="Signature blob missing")
    return Response(content=data, media_type="image/png")
