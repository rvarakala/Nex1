"""HA Procurement — PurchaseOrder + GRN — Phase 2.

Lifecycle:
  PO draft → approved → ordered → (partial_received | received) → closed
  GRN posts against an approved/ordered PO, spawns SerialItem rows for
  serialised products (IN_STOCK), or upserts AccessoryStock qty for SKUs.
  Each SerialItem creation writes a `serial_events` row (from='(new)', to='IN_STOCK').

Roles:
  - read POs / GRNs: any authenticated user (needed for dashboards)
  - create/approve/close PO: inventory_manager + clinic_owner
  - create GRN: inventory_manager + clinic_owner
"""
from datetime import datetime, timezone
from typing import List, Optional

from dateutil.relativedelta import relativedelta
from fastapi import APIRouter, Depends, HTTPException
from pymongo.errors import BulkWriteError, DuplicateKeyError

from auth import (
    get_current_user, require_roles, user_can_see_branch,
)
from database import get_db
from models_ha import (
    PurchaseOrder, PurchaseOrderCreate, POLine,
    GRN, GRNCreate, GRNLine,
)
from utils.branch_scope import branch_scope as _branch_scope  # noqa: F401
from utils.numbering import next_number
from utils.po_states import assert_po_transition, PO_RECEIVABLE, auto_advance_on_grn
from utils.serde import serialize_datetime, deserialize_datetime

router = APIRouter(prefix="/api/ha")


def _compute_po_totals(lines: List[POLine]) -> tuple[float, float, float]:
    subtotal = 0.0
    gst_amount = 0.0
    for ln in lines:
        line_subtotal = round(ln.qty * ln.unit_cost, 2)
        subtotal += line_subtotal
        gst_amount += round(line_subtotal * (ln.gst_rate or 0) / 100.0, 2)
    return round(subtotal, 2), round(gst_amount, 2), round(subtotal + gst_amount, 2)


# ==================== PURCHASE ORDERS ====================

@router.get("/purchase-orders", response_model=List[PurchaseOrder])
async def list_purchase_orders(
    status: Optional[str] = None,
    branch_id: Optional[str] = None,
    vendor_id: Optional[str] = None,
    limit: int = 100,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    q = _branch_scope(user)
    if status:
        q["status"] = status
    if branch_id:
        if not user_can_see_branch(user, branch_id):
            raise HTTPException(status_code=403, detail="Branch access denied")
        q["branch_id"] = branch_id
    if vendor_id:
        q["vendor_id"] = vendor_id
    rows = await db.purchase_orders.find(q, {"_id": 0}).sort("created_at", -1).to_list(limit)
    return [deserialize_datetime(r) for r in rows]


@router.post("/purchase-orders", response_model=PurchaseOrder)
async def create_purchase_order(
    payload: PurchaseOrderCreate,
    user=Depends(require_roles("inventory_manager", "clinic_owner")),
    db=Depends(get_db),
):
    if not user_can_see_branch(user, payload.branch_id):
        raise HTTPException(status_code=403, detail="Branch access denied")
    if not payload.lines:
        raise HTTPException(status_code=400, detail="PO must have at least one line")

    vendor = await db.vendors.find_one(
        {"vendor_id": payload.vendor_id, "clinic_id": user["clinic_id"]},
        {"_id": 0, "name": 1},
    )
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    # Validate every product exists in this clinic
    product_ids = list({ln.product_id for ln in payload.lines})
    found = await db.ha_products.find(
        {"product_id": {"$in": product_ids}, "clinic_id": user["clinic_id"]},
        {"_id": 0, "product_id": 1},
    ).to_list(len(product_ids))
    missing = set(product_ids) - {p["product_id"] for p in found}
    if missing:
        raise HTTPException(status_code=400, detail=f"Unknown products: {sorted(missing)}")

    subtotal, gst, total = _compute_po_totals(payload.lines)
    po_no = await next_number(db, "po", user["clinic_id"])

    po = PurchaseOrder(
        po_no=po_no,
        clinic_id=user["clinic_id"],
        branch_id=payload.branch_id,
        vendor_id=payload.vendor_id,
        vendor_name=vendor["name"],
        lines=payload.lines,
        subtotal=subtotal,
        gst_amount=gst,
        total=total,
        status="draft",
        expected_date=payload.expected_date,
        notes=payload.notes,
        created_by_user_id=user["user_id"],
    )
    await db.purchase_orders.insert_one(serialize_datetime(po.model_dump()))
    return po


@router.get("/purchase-orders/{po_no}", response_model=PurchaseOrder)
async def get_purchase_order(po_no: str, user=Depends(get_current_user), db=Depends(get_db)):
    row = await db.purchase_orders.find_one(
        {"po_no": po_no, "clinic_id": user["clinic_id"]}, {"_id": 0},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Purchase order not found")
    if not user_can_see_branch(user, row["branch_id"]):
        raise HTTPException(status_code=403, detail="Branch access denied")
    return deserialize_datetime(row)


@router.post("/purchase-orders/{po_no}/status")
async def transition_po_status(
    po_no: str, payload: dict,
    user=Depends(require_roles("inventory_manager", "clinic_owner")),
    db=Depends(get_db),
):
    """Legal transitions driven by `utils/po_states.PO_ALLOWED`."""
    to_status = payload.get("to_status")
    row = await db.purchase_orders.find_one(
        {"po_no": po_no, "clinic_id": user["clinic_id"]}, {"_id": 0},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Purchase order not found")
    if not user_can_see_branch(user, row["branch_id"]):
        raise HTTPException(status_code=403, detail="Branch access denied")
    assert_po_transition(row["status"], to_status)
    upd = {"status": to_status}
    now = datetime.now(timezone.utc).isoformat()
    if to_status == "approved":
        upd["approved_at"] = now
    if to_status == "closed":
        upd["closed_at"] = now
    # CRITICAL — filter by BOTH po_no AND clinic_id on the update.
    # Without the clinic_id scope, this endpoint updates the FIRST
    # matching doc in the collection — which could be another tenant's
    # PO that happens to share the po_no counter (they're not globally
    # unique). Symptom: Sound Clinic clicks Approve, POST returns 200,
    # but the local drawer still shows DRAFT because the wrong tenant's
    # PO got the state change. Confirmed via a duplicate-po_no scenario
    # between `tenant-sound-clinic-blr` and `clinic-pytest-suite`.
    await db.purchase_orders.update_one(
        {"po_no": po_no, "clinic_id": user["clinic_id"]},
        {"$set": upd},
    )
    return {"po_no": po_no, "status": to_status}


# ==================== GRN ====================

@router.get("/grns", response_model=List[GRN])
async def list_grns(
    po_no: Optional[str] = None,
    branch_id: Optional[str] = None,
    limit: int = 100,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    q = _branch_scope(user)
    if po_no:
        q["po_no"] = po_no
    if branch_id:
        if not user_can_see_branch(user, branch_id):
            raise HTTPException(status_code=403, detail="Branch access denied")
        q["branch_id"] = branch_id
    rows = await db.grns.find(q, {"_id": 0}).sort("created_at", -1).to_list(limit)
    return [deserialize_datetime(r) for r in rows]


@router.get("/grns/{grn_no}", response_model=GRN)
async def get_grn(grn_no: str, user=Depends(get_current_user), db=Depends(get_db)):
    row = await db.grns.find_one(
        {"grn_no": grn_no, "clinic_id": user["clinic_id"]}, {"_id": 0},
    )
    if not row:
        raise HTTPException(status_code=404, detail="GRN not found")
    if not user_can_see_branch(user, row["branch_id"]):
        raise HTTPException(status_code=403, detail="Branch access denied")
    return deserialize_datetime(row)


@router.post("/grns", response_model=GRN)
async def create_grn(
    payload: GRNCreate,
    user=Depends(require_roles("inventory_manager", "clinic_owner")),
    db=Depends(get_db),
):
    """Receive goods against a PO.
    For serialised products: spawns SerialItems (state=IN_STOCK, pool=saleable)
    + writes a serial_events row for each. For non-serialised: upserts
    AccessoryStock qty. Updates PO status to partial/received/closed based on
    cumulative received-vs-ordered totals."""
    po = await db.purchase_orders.find_one(
        {"po_no": payload.po_no, "clinic_id": user["clinic_id"]}, {"_id": 0},
    )
    if not po:
        raise HTTPException(status_code=404, detail="Purchase order not found")
    if not user_can_see_branch(user, po["branch_id"]):
        raise HTTPException(status_code=403, detail="Branch access denied")
    if po["status"] not in PO_RECEIVABLE:
        raise HTTPException(status_code=409, detail=f"Cannot receive against a {po['status']} PO")
    if not payload.lines:
        raise HTTPException(status_code=400, detail="GRN must have at least one line")

    # Load all referenced products in one shot
    product_ids = list({ln.product_id for ln in payload.lines})
    products = {
        p["product_id"]: p async for p in db.ha_products.find(
            {"product_id": {"$in": product_ids}, "clinic_id": user["clinic_id"]},
            {"_id": 0},
        )
    }
    missing = set(product_ids) - set(products)
    if missing:
        raise HTTPException(status_code=400, detail=f"Unknown products on GRN: {sorted(missing)}")

    # Validate serial-no coverage for serialised lines (uniqueness enforced by
    # DB index — we catch DuplicateKeyError on insert for the error message).
    for ln in payload.lines:
        p = products[ln.product_id]
        if p["is_serialised"]:
            if len(ln.serial_nos) != ln.qty_received:
                raise HTTPException(
                    status_code=400,
                    detail=f"Line for {p['brand']} {p['model']}: qty_received={ln.qty_received} but {len(ln.serial_nos)} serial(s) supplied",
                )
            if len(set(ln.serial_nos)) != len(ln.serial_nos):
                raise HTTPException(status_code=400, detail="Duplicate serial numbers on GRN line")

    grn_no = await next_number(db, "grn", user["clinic_id"])
    received_at = payload.received_at or datetime.now(timezone.utc).isoformat()

    # ----- Pre-compute aggregate receipt + over-receipt check -----
    # Must happen BEFORE we insert any serial_items / update accessory stock,
    # otherwise a 409 over-receipt leaves orphan inventory in the DB.
    received_by_key: dict[tuple, int] = {}
    async for g in db.grns.find({"po_no": payload.po_no, "clinic_id": user["clinic_id"]}, {"_id": 0, "lines": 1}):
        for ln in g.get("lines", []):
            k = (ln["product_id"], ln.get("variant"))
            received_by_key[k] = received_by_key.get(k, 0) + int(ln["qty_received"])
    # Add this GRN's pending lines
    for ln in payload.lines:
        k = (ln.product_id, ln.variant)
        received_by_key[k] = received_by_key.get(k, 0) + int(ln.qty_received)
    # Compare vs PO ordered
    for ln in po["lines"]:
        k = (ln["product_id"], ln.get("variant"))
        if received_by_key.get(k, 0) > int(ln["qty"]):
            raise HTTPException(
                status_code=409,
                detail=f"Over-receipt: {received_by_key[k]} received vs {ln['qty']} ordered for product {ln['product_id']}",
            )

    grn = GRN(
        grn_no=grn_no,
        po_no=payload.po_no,
        clinic_id=user["clinic_id"],
        branch_id=po["branch_id"],
        received_at=received_at,
        lines=payload.lines,
        vendor_invoice_ref=payload.vendor_invoice_ref,
        notes=payload.notes,
        created_by_user_id=user["user_id"],
    )
    # NOTE: GRN doc is inserted AFTER duplicate-serial check succeeds (see below).
    # Inserting it upfront would leave orphan GRN rows on 409 Duplicate-Serial,
    # inflating the `received_by_key` totals on the NEXT GRN's over-receipt check.

    # ----- Spawn inventory -----
    now_iso = datetime.now(timezone.utc).isoformat()
    from uuid import uuid4

    serial_docs: list[dict] = []
    serial_event_docs: list[dict] = []

    for ln in payload.lines:
        p = products[ln.product_id]
        if p["is_serialised"]:
            # Exact calendar-month warranty (e.g. 24 months → same date 2 years later)
            wmonths = int(p.get("warranty_months") or 0)
            try:
                base = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
            except Exception:
                base = datetime.now(timezone.utc)
            warranty_end = (base + relativedelta(months=wmonths)).date().isoformat()
            for sn in ln.serial_nos:
                serial_id = f"SI-{str(uuid4())[:10].upper()}"
                serial_docs.append({
                    "serial_id": serial_id,
                    "clinic_id": user["clinic_id"],
                    "branch_id": po["branch_id"],
                    "product_id": ln.product_id,
                    "serial_no": sn,
                    "state": "IN_STOCK",
                    "pool": "saleable",
                    "warranty_end_date": warranty_end,
                    "grn_no": grn_no,
                    "current_patient_id": None,
                    "created_at": now_iso,
                    "updated_at": now_iso,
                })
                serial_event_docs.append({
                    "serial_id": serial_id,
                    # NAV-010 · INV-009 · Forward-only tenant stamping.
                    "clinic_id": user["clinic_id"],
                    "from": "(new)",
                    "to": "IN_STOCK",
                    "at": now_iso,
                    "actor_user_id": user["user_id"],
                    "ref_doc": {"kind": "grn", "id": grn_no},
                    "note": f"Received via {grn_no}",
                })
        else:
            # Upsert accessory stock row (per product + variant + branch)
            await db.accessory_stock.update_one(
                {
                    "clinic_id": user["clinic_id"],
                    "branch_id": po["branch_id"],
                    "product_id": ln.product_id,
                    "variant": ln.variant,
                },
                {
                    "$inc": {"qty_on_hand": int(ln.qty_received)},
                    "$setOnInsert": {
                        "sku_id": f"SKU-{str(uuid4())[:8].upper()}",
                        "clinic_id": user["clinic_id"],
                        "branch_id": po["branch_id"],
                        "product_id": ln.product_id,
                        "variant": ln.variant,
                        "reorder_level": 0,
                        "created_at": now_iso,
                    },
                    "$set": {"updated_at": now_iso},
                },
                upsert=True,
            )

    if serial_docs:
        try:
            await db.serial_items.insert_many(serial_docs, ordered=True)
        except (DuplicateKeyError, BulkWriteError) as e:
            # Unique index (clinic_id, serial_no) rejected at least one serial.
            # Motor's insert_many surfaces write errors via BulkWriteError; a
            # single-doc path could raise DuplicateKeyError. Handle both.
            bad_serial = "(unknown)"
            details = getattr(e, "details", {}) or {}
            # BulkWriteError nests the offending key under writeErrors[0]
            write_errors = details.get("writeErrors") or []
            if write_errors:
                bad_serial = (
                    write_errors[0].get("keyValue", {}).get("serial_no")
                    or bad_serial
                )
            else:
                bad_serial = details.get("keyValue", {}).get("serial_no") or bad_serial
            # Roll back any accessory stock we already incremented for this GRN
            # so the 409 response leaves the database unchanged.
            for ln in payload.lines:
                p = products[ln.product_id]
                if not p["is_serialised"]:
                    await db.accessory_stock.update_one(
                        {
                            "clinic_id": user["clinic_id"],
                            "branch_id": po["branch_id"],
                            "product_id": ln.product_id,
                            "variant": ln.variant,
                        },
                        {"$inc": {"qty_on_hand": -int(ln.qty_received)}},
                    )
            raise HTTPException(
                status_code=409,
                detail=f"Serial already on record in this clinic: {bad_serial}",
            )
    if serial_event_docs:
        await db.serial_events.insert_many(serial_event_docs)

    # ----- Persist the GRN document now that inventory writes succeeded -----
    # Defensive: if a duplicate `grn_no` slips through (e.g. legacy data inserted
    # directly bypassing the counter), keep minting fresh numbers until the
    # insert succeeds rather than 500-ing the request.
    for _ in range(5):
        try:
            await db.grns.insert_one(serialize_datetime(grn.model_dump()))
            break
        except DuplicateKeyError:
            grn.grn_no = await next_number(db, "grn", user["clinic_id"])
    else:
        raise HTTPException(status_code=500, detail="Could not allocate a unique GRN number")

    # ----- Update PO status -----
    # `received_by_key` already includes this GRN's lines (computed pre-insert).
    fully_received = all(
        received_by_key.get((ln["product_id"], ln.get("variant")), 0) >= int(ln["qty"])
        for ln in po["lines"]
    )
    # Walk the PO status forward through the allowed table — never skip states.
    for step in auto_advance_on_grn(po["status"], fully_received):
        await db.purchase_orders.update_one(
            {"po_no": payload.po_no, "clinic_id": user["clinic_id"]},
            {"$set": {"status": step}},
        )

    return grn
