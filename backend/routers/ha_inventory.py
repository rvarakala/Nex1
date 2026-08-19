"""HA Inventory — SerialItem list / lifecycle timeline + AccessoryStock — Phase 2.

Serialised inventory (HA units): browse by branch / state / pool / brand / search by serial_no.
Lifecycle timeline: append-only `serial_events` rows from `transition_serial()`.
Accessory stock: qty-tracked, consume / replenish via +/- delta.
"""
import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from pymongo import ReturnDocument

from auth import (
    get_current_user, require_roles, user_can_see_branch,
    CLINIC_WIDE_ROLES,
)
from database import get_db
from models_ha import (
    Product, SerialItem, SerialItemUpdate,
    AccessoryStock, AccessoryAdjust,
)

log = logging.getLogger(__name__)
from utils.ha_states import transition_serial
from utils.serde import serialize_datetime, deserialize_datetime, safe_deserialize_rows

router = APIRouter(prefix="/api/ha")


def _branch_scope(user: dict) -> dict:
    """Return a Mongo filter fragment that restricts to branches this user can see."""
    if user["role"] in CLINIC_WIDE_ROLES:
        return {"clinic_id": user["clinic_id"]}
    return {
        "clinic_id": user["clinic_id"],
        "branch_id": {"$in": user.get("branch_ids") or []},
    }


# ==================== SERIAL ITEMS ====================

@router.get("/serial-items", response_model=List[SerialItem])
async def list_serial_items(
    branch_id: Optional[str] = None,
    state: Optional[str] = None,
    pool: Optional[str] = None,
    product_id: Optional[str] = None,
    current_patient_id: Optional[str] = None,
    source_kind: Optional[str] = None,          # "vendor" | "borrowed"
    only_active: bool = False,                  # drop returned/retired rows
    search: Optional[str] = None,
    limit: int = 200,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    q = _branch_scope(user)
    if branch_id:
        if not user_can_see_branch(user, branch_id):
            raise HTTPException(status_code=403, detail="Branch access denied")
        q["branch_id"] = branch_id
    if state:
        q["state"] = state
    if pool:
        q["pool"] = pool
    if product_id:
        q["product_id"] = product_id
    if current_patient_id:
        q["current_patient_id"] = current_patient_id
    if source_kind in ("vendor", "borrowed"):
        # Legacy rows (created before source_kind existed) don't have the
        # field at all — they should be treated as vendor by default.
        if source_kind == "vendor":
            q["$or"] = [{"source_kind": "vendor"}, {"source_kind": {"$exists": False}}]
        else:
            q["source_kind"] = "borrowed"
    if only_active:
        q["state"] = {"$nin": ["RETIRED", "RETURNED", "SOLD", "DAMAGED"]}
    if search:
        safe = re.escape(search.strip())
        if safe:
            q["serial_no"] = {"$regex": safe, "$options": "i"}
    rows = await db.serial_items.find(q, {"_id": 0}).sort("created_at", -1).to_list(limit)
    # Per-row deserialise so legacy pre-schema-tighten rows (e.g. missing
    # `product_id` from ~mid-2025 imports) don't 500 the whole endpoint.
    return safe_deserialize_rows(
        rows, SerialItem, collection="serial_items", clinic_id=user.get("clinic_id", ""),
    )


@router.get("/serial-items/by-branch-summary")
async def serial_items_summary(
    branch_id: Optional[str] = None,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Count of serial items by (state, pool) for the Inventory Board KPI strip."""
    match = _branch_scope(user)
    if branch_id:
        if not user_can_see_branch(user, branch_id):
            raise HTTPException(status_code=403, detail="Branch access denied")
        match["branch_id"] = branch_id
    # Coerce missing/null state|pool to a sentinel via $ifNull. Legacy rows
    # from mid-2025 imports sometimes miss `pool`; Mongo's $group drops
    # missing sub-fields entirely from `_id`, which used to raise KeyError
    # here and 500'd the whole endpoint → frontend KPI chips fell back to
    # zeros. Bucketing them under "unknown" keeps the total honest.
    pipeline = [
        {"$match": match},
        {"$group": {
            "_id": {
                "state": {"$ifNull": ["$state", "unknown"]},
                "pool":  {"$ifNull": ["$pool",  "unknown"]},
            },
            "n": {"$sum": 1},
        }},
    ]
    by_state: dict[str, int] = {}
    by_pool: dict[str, int] = {}
    total = 0
    async for row in db.serial_items.aggregate(pipeline):
        key = row.get("_id") or {}
        state = key.get("state") or "unknown"
        pool = key.get("pool") or "unknown"
        n = row.get("n", 0)
        by_state[state] = by_state.get(state, 0) + n
        by_pool[pool] = by_pool.get(pool, 0) + n
        total += n

    # Revenue attached to SOLD & RESERVED serials — one Quick-Sale (or
    # HA-Sale) can span multiple serials (a pair of hearing aids), so
    # we split total across `consumed_serial_ids` / `lines.serial_id`
    # so we don't double-count when the audiologist filters by SOLD.
    revenue_by_state: dict[str, float] = {}
    # Fetch the id → state map first (bounded to same filter, incl branch)
    sid_state: dict[str, str] = {}
    async for r in db.serial_items.find(match, {"_id": 0, "serial_id": 1, "state": 1}):
        if r.get("state") in ("SOLD", "RESERVED"):
            sid_state[r["serial_id"]] = r["state"]

    if sid_state:
        sids = list(sid_state.keys())
        async for qs in db.ha_quick_sales.find(
            {"clinic_id": user["clinic_id"], "consumed_serial_ids": {"$in": sids}},
            {"_id": 0, "consumed_serial_ids": 1, "total": 1},
        ):
            linked = [s for s in (qs.get("consumed_serial_ids") or []) if s in sid_state]
            if not linked:
                continue
            per = float(qs.get("total") or 0) / max(len(linked), 1)
            for sid in linked:
                st = sid_state[sid]
                revenue_by_state[st] = revenue_by_state.get(st, 0.0) + per
        async for sl in db.ha_sales.find(
            {"clinic_id": user["clinic_id"], "lines.serial_id": {"$in": sids}},
            {"_id": 0, "lines": 1, "total": 1},
        ):
            linked = [ln.get("serial_id") for ln in (sl.get("lines") or []) if ln.get("serial_id") in sid_state]
            if not linked:
                continue
            per = float(sl.get("total") or 0) / max(len(linked), 1)
            for sid in linked:
                st = sid_state[sid]
                revenue_by_state[st] = revenue_by_state.get(st, 0.0) + per

    return {
        "total": total,
        "by_state": by_state,
        "by_pool": by_pool,
        "revenue_by_state": {k: round(v, 2) for k, v in revenue_by_state.items()},
    }


@router.get("/serial-items/{serial_id}", response_model=SerialItem)
async def get_serial_item(serial_id: str, user=Depends(get_current_user), db=Depends(get_db)):
    row = await db.serial_items.find_one(
        {"serial_id": serial_id, "clinic_id": user["clinic_id"]}, {"_id": 0},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Serial item not found")
    if not user_can_see_branch(user, row["branch_id"]):
        raise HTTPException(status_code=403, detail="Branch access denied")
    return deserialize_datetime(row)


@router.get("/serial-items/{serial_id}/timeline")
async def serial_timeline(serial_id: str, user=Depends(get_current_user), db=Depends(get_db)):
    """Append-only lifecycle ledger for a single serial item (UC-HA03)."""
    si = await db.serial_items.find_one(
        {"serial_id": serial_id, "clinic_id": user["clinic_id"]}, {"_id": 0},
    )
    if not si:
        raise HTTPException(status_code=404, detail="Serial item not found")
    if not user_can_see_branch(user, si["branch_id"]):
        raise HTTPException(status_code=403, detail="Branch access denied")
    events = await db.serial_events.find(
        {"serial_id": serial_id}, {"_id": 0},
    ).sort("at", -1).to_list(500)
    # Attach the linked invoice(s) or active trial, if any — so the drawer
    # can render "Sold to Kavitha · INV/2026/000004" or "On trial with
    # Ramesh · Ends 15 Aug" without a second round-trip. Both lookups
    # run in parallel and only the ones with data are returned.
    inv_map, trial_map, loaner_map = await asyncio.gather(
        _resolve_serial_invoices(db, user["clinic_id"], [serial_id]),
        _resolve_serial_trials(db, user["clinic_id"], [serial_id]),
        _resolve_serial_loaners(db, user["clinic_id"], [serial_id]),
    )
    return {
        "serial": deserialize_datetime(si),
        "events": events,
        "invoice": inv_map.get(serial_id),
        "trial": trial_map.get(serial_id),
        "loaner": loaner_map.get(serial_id),
    }


class SerialInvoiceLookupIn(BaseModel):
    """Bulk-resolve serial → invoice map. POST so the id list can be long
    without hitting URL length limits. Used by the Inventory Board table
    to hydrate the SOLD / RESERVED rows with `INV/…` numbers in one call."""
    serial_ids: List[str]


@router.post("/serial-items/invoice-lookup")
async def serial_invoice_lookup(
    payload: SerialInvoiceLookupIn,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Returns `{serial_id: {invoice_no, invoice_id, total, paid, due,
    payment_status, sale_no, patient_id, patient_name, source}}` for
    every serial that's linked to a Quick Sale or full HA Sale."""
    if not payload.serial_ids:
        return {}
    # De-dupe + cap for safety (Inventory Board pages at 200 rows).
    ids = list({s for s in payload.serial_ids if s})[:500]
    return await _resolve_serial_invoices(db, user["clinic_id"], ids)


@router.post("/serial-items/trial-lookup")
async def serial_trial_lookup(
    payload: SerialInvoiceLookupIn,   # same {serial_ids} shape
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Returns `{serial_id: {trial_no, patient_*, start_date, return_date,
    status, days_active, days_overdue}}` for every serial currently on
    trial (or with a completed/converted trial history). Used by the
    Inventory Board to render a "On trial with X · Ends Y" mini-card
    against TRIAL_OUT rows without another list-scoped join.
    """
    if not payload.serial_ids:
        return {}
    ids = list({s for s in payload.serial_ids if s})[:500]
    return await _resolve_serial_trials(db, user["clinic_id"], ids)


@router.post("/serial-items/loaner-lookup")
async def serial_loaner_lookup(
    payload: SerialInvoiceLookupIn,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Returns `{serial_id: {loaner_no, patient_*, issued_on,
    expected_return_date, status, days_active, days_overdue, deposit,
    service_ticket_no}}` for every serial currently loaned out (or with
    a returned/damaged loaner history). Cross-tab consistency ask: the
    Inventory Board's "Linked To" column now hydrates LOANER serials too.
    """
    if not payload.serial_ids:
        return {}
    ids = list({s for s in payload.serial_ids if s})[:500]
    return await _resolve_serial_loaners(db, user["clinic_id"], ids)


async def _resolve_serial_loaners(db, clinic_id: str, serial_ids: List[str]) -> dict:
    """One trip to `ha_loaners`, prioritising the ACTIVE loan over any
    historical one (a serial can loop through multiple loans over time —
    the current active one wins so the audiologist sees who has the unit
    RIGHT NOW, not who had it last month).
    """
    if not serial_ids:
        return {}
    out: dict[str, dict] = {}
    async for ln in db.ha_loaners.find(
        {"clinic_id": clinic_id, "serial_id": {"$in": serial_ids}},
        {
            "_id": 0, "loaner_id": 1, "serial_id": 1,
            "patient_id": 1, "patient_name": 1, "patient_mobile": 1,
            "issued_on": 1, "expected_return_date": 1, "actual_return_date": 1,
            "status": 1, "deposit_amount": 1, "service_ticket_no": 1,
            "notes": 1, "created_at": 1,
        },
    ).sort("created_at", -1):
        sid = ln.get("serial_id")
        if not sid or sid not in serial_ids:
            continue
        existing = out.get(sid)
        st = (ln.get("status") or "").lower()
        # Prefer active over returned/damaged when we already have a hit.
        if existing and (existing.get("status") or "").lower() == "active" and st != "active":
            continue

        days_active = None
        days_overdue = None
        try:
            issued = ln.get("issued_on")
            expected = ln.get("expected_return_date")
            if issued:
                d0 = datetime.fromisoformat(str(issued))
                if d0.tzinfo is None:
                    d0 = d0.replace(tzinfo=timezone.utc)
                days_active = (datetime.now(timezone.utc) - d0).days
            if expected and st == "active":
                d1 = datetime.fromisoformat(str(expected))
                if d1.tzinfo is None:
                    d1 = d1.replace(tzinfo=timezone.utc)
                delta = (datetime.now(timezone.utc) - d1).days
                if delta > 0:
                    days_overdue = delta
        except Exception:
            pass

        out[sid] = {
            "source": "loaner",
            "loaner_id": ln.get("loaner_id"),
            # Loaners don't have a customer-facing "no" — use the id as the display ref.
            "loaner_no": ln.get("loaner_id"),
            "patient_id": ln.get("patient_id"),
            "patient_name": ln.get("patient_name"),
            "patient_mobile": ln.get("patient_mobile"),
            "issued_on": ln.get("issued_on"),
            "expected_return_date": ln.get("expected_return_date"),
            "actual_return_date": ln.get("actual_return_date"),
            "status": ln.get("status"),
            "deposit_amount": ln.get("deposit_amount"),
            "service_ticket_no": ln.get("service_ticket_no"),
            "days_active": days_active,
            "days_overdue": days_overdue,
        }
    return out


async def _resolve_serial_trials(db, clinic_id: str, serial_ids: List[str]) -> dict:
    """One trip to `ha_trials`, prioritising active trials over historical
    ones (a serial can have multiple trial rows across time — the "active"
    one wins so the audiologist sees the CURRENT trial, not last year's).
    """
    if not serial_ids:
        return {}
    out: dict[str, dict] = {}
    # Sort by start_date desc so the most-recent trial per serial wins.
    # Then within that, still let "active" trump "converted"/"returned".
    async for tr in db.ha_trials.find(
        {"clinic_id": clinic_id, "serial_id": {"$in": serial_ids}},
        {
            "_id": 0, "trial_id": 1, "trial_no": 1, "serial_id": 1,
            "patient_id": 1, "patient_name": 1, "patient_mobile": 1,
            "start_date": 1, "return_date": 1, "status": 1,
            "product_label": 1, "trial_fee": 1, "audiologist_id": 1,
            "notes": 1, "created_at": 1,
        },
    ).sort("start_date", -1):
        sid = tr.get("serial_id")
        if not sid or sid not in serial_ids:
            continue
        # Prefer an active trial if we already saved a non-active one.
        existing = out.get(sid)
        st = (tr.get("status") or "").lower()
        if existing and (existing.get("status") or "").lower() == "active" and st != "active":
            continue

        # Days math — client renders "Started X days ago · Ends in Y days"
        days_active = None
        days_overdue = None
        try:
            start = tr.get("start_date")
            end = tr.get("return_date")
            if start:
                start_dt = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=timezone.utc)
                days_active = (datetime.now(timezone.utc) - start_dt).days
            if end and st == "active":
                end_dt = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
                delta = (datetime.now(timezone.utc) - end_dt).days
                if delta > 0:
                    days_overdue = delta
        except Exception:
            # Malformed date? Silently skip the delta rather than 500 the
            # Inventory Board — the UI still renders the raw dates.
            pass

        out[sid] = {
            "source": "trial",
            "trial_id": tr.get("trial_id"),
            "trial_no": tr.get("trial_no"),
            "patient_id": tr.get("patient_id"),
            "patient_name": tr.get("patient_name"),
            "patient_mobile": tr.get("patient_mobile"),
            "start_date": tr.get("start_date"),
            "return_date": tr.get("return_date"),
            "status": tr.get("status"),
            "product_label": tr.get("product_label"),
            "trial_fee": tr.get("trial_fee"),
            "days_active": days_active,
            "days_overdue": days_overdue,
        }
    return out


async def _resolve_serial_invoices(db, clinic_id: str, serial_ids: List[str]) -> dict:
    """One trip each to `ha_quick_sales` and `ha_sales`, then merged into
    a flat `{serial_id: {…invoice fields…}}` dict.

    Priority: if a serial is referenced by BOTH a Quick Sale and a full
    HA Sale (rare — usually a data migration artefact), the Quick Sale
    wins because it always has an `invoice_no`. Reserved full HA Sales
    without an invoice yet still surface (`invoice_no=None`, status
    `reserved`) so the audiologist can trace the reservation trail.
    """
    if not serial_ids:
        return {}
    out: dict[str, dict] = {}

    # 1) Quick Sales — serials live in `consumed_serial_ids` array
    async for qs in db.ha_quick_sales.find(
        {"clinic_id": clinic_id, "consumed_serial_ids": {"$in": serial_ids}},
        {
            "_id": 0, "consumed_serial_ids": 1, "sale_no": 1, "invoice_id": 1,
            "invoice_no": 1, "patient_id": 1, "patient_name": 1,
            "total": 1, "amount_paid": 1, "balance_due": 1,
            "payment_status": 1, "created_at": 1, "quick_sale_id": 1,
        },
    ):
        for sid in (qs.get("consumed_serial_ids") or []):
            if sid in serial_ids and sid not in out:
                out[sid] = {
                    "source": "quick_sale",
                    "sale_no": qs.get("sale_no"),
                    "invoice_id": qs.get("invoice_id"),
                    "invoice_no": qs.get("invoice_no"),
                    "quick_sale_id": qs.get("quick_sale_id"),
                    "patient_id": qs.get("patient_id"),
                    "patient_name": qs.get("patient_name"),
                    "total": qs.get("total"),
                    "amount_paid": qs.get("amount_paid"),
                    "balance_due": qs.get("balance_due"),
                    "payment_status": qs.get("payment_status"),
                    "created_at": qs.get("created_at"),
                }

    # 2) Full HA Sales — serials live inside `lines[].serial_id`.
    async for sl in db.ha_sales.find(
        {"clinic_id": clinic_id, "lines.serial_id": {"$in": serial_ids}},
        {
            "_id": 0, "sale_no": 1, "invoice_no": 1, "patient_id": 1,
            "patient_name": 1, "total": 1, "status": 1, "created_at": 1,
            "lines": 1,
        },
    ):
        line_serials = [ln.get("serial_id") for ln in (sl.get("lines") or []) if ln.get("serial_id")]
        for sid in line_serials:
            if sid in serial_ids and sid not in out:
                out[sid] = {
                    "source": "ha_sale",
                    "sale_no": sl.get("sale_no"),
                    "invoice_no": sl.get("invoice_no"),
                    "patient_id": sl.get("patient_id"),
                    "patient_name": sl.get("patient_name"),
                    "total": sl.get("total"),
                    "status": sl.get("status"),  # "reserved" | "completed"
                    "created_at": sl.get("created_at"),
                }
    return out


@router.put("/serial-items/{serial_id}", response_model=SerialItem)
async def update_serial_item(
    serial_id: str, payload: SerialItemUpdate,
    user=Depends(require_roles("inventory_manager", "clinic_owner")),
    db=Depends(get_db),
):
    """Edit non-state fields (pool, notes). State changes must go through the
    dedicated transition endpoint below."""
    existing = await db.serial_items.find_one(
        {"serial_id": serial_id, "clinic_id": user["clinic_id"]}, {"_id": 0},
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Serial item not found")
    if not user_can_see_branch(user, existing["branch_id"]):
        raise HTTPException(status_code=403, detail="Branch access denied")
    update = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not update:
        return deserialize_datetime(existing)
    update["updated_at"] = datetime.now(timezone.utc).isoformat()
    await db.serial_items.update_one({"serial_id": serial_id}, {"$set": update})
    row = await db.serial_items.find_one({"serial_id": serial_id}, {"_id": 0})
    return deserialize_datetime(row)


@router.post("/serial-items/{serial_id}/transition")
async def transition_serial_state(
    serial_id: str, payload: dict,
    user=Depends(require_roles("inventory_manager", "clinic_owner", "front_desk", "audiologist")),
    db=Depends(get_db),
):
    """Explicit state transition. Body: {to_state, note?}. The state-machine
    helper validates legality and writes the audit row.

    Destructive / terminal transitions (DAMAGED, RETIRED, RETURNED) require
    inventory_manager or above — front-desk/audiologist can do clinical flow
    (RESERVED/TRIAL_OUT/SOLD/SERVICE_IN) but cannot scrap a unit."""
    to_state = payload.get("to_state")
    note = payload.get("note")
    if not to_state:
        raise HTTPException(status_code=400, detail="to_state is required")

    # Stricter role gate: destructive terminals need inventory/owner rights.
    DESTRUCTIVE = {"DAMAGED", "RETIRED", "RETURNED"}
    if to_state in DESTRUCTIVE and user["role"] not in {
        "super_admin", "clinic_owner", "inventory_manager", "technician",
    }:
        raise HTTPException(
            status_code=403,
            detail=f"Role {user['role']} cannot move a unit to {to_state}",
        )

    existing = await db.serial_items.find_one(
        {"serial_id": serial_id, "clinic_id": user["clinic_id"]}, {"_id": 0},
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Serial item not found")
    if not user_can_see_branch(user, existing["branch_id"]):
        raise HTTPException(status_code=403, detail="Branch access denied")
    updated = await transition_serial(
        db, serial_id, to_state,
        actor_user_id=user["user_id"],
        ref_doc={"kind": "manual", "note": note} if note else {"kind": "manual"},
        note=note,
    )
    return updated


@router.post("/serial-items/{serial_id}/mark-demo")
async def mark_serial_demo(
    serial_id: str, payload: dict | None = None,
    user=Depends(require_roles("clinic_owner", "inventory_manager")),
    db=Depends(get_db),
):
    """Move a saleable unit into the DEMO pool (state stays IN_STOCK).

    Demo units are intended for take-home trials and clinic demos — they
    should never be sold. Only owners/inventory managers can flip the pool
    to prevent accidental saleable → demo cross-contamination.
    """
    row = await db.serial_items.find_one(
        {"serial_id": serial_id, "clinic_id": user["clinic_id"]}, {"_id": 0},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Serial item not found")
    if not user_can_see_branch(user, row["branch_id"]):
        raise HTTPException(status_code=403, detail="Branch access denied")
    if row.get("pool") == "demo":
        return deserialize_datetime(row)
    if row["state"] not in ("IN_STOCK", "RESERVED"):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot mark demo — unit is currently {row['state']}",
        )
    note = (payload or {}).get("note") if payload else None
    now = datetime.now(timezone.utc).isoformat()
    await db.serial_items.update_one(
        {"serial_id": serial_id},
        {"$set": {"pool": "demo", "updated_at": now}},
    )
    await db.serial_events.insert_one({
        "serial_id": serial_id,
        "from": row["state"], "to": row["state"],  # pool-only change
        "at": now, "actor_user_id": user["user_id"],
        "ref_doc": {"kind": "pool-change", "to_pool": "demo"},
        "note": note or "Moved to demo pool",
    })
    updated = await db.serial_items.find_one({"serial_id": serial_id}, {"_id": 0})
    return deserialize_datetime(updated)


@router.post("/serial-items/{serial_id}/unmark-demo")
async def unmark_serial_demo(
    serial_id: str, payload: dict | None = None,
    user=Depends(require_roles("clinic_owner", "inventory_manager")),
    db=Depends(get_db),
):
    """Return a demo unit to the saleable pool (e.g. to sell a demo at a
    discount, or retire from the demo pool)."""
    row = await db.serial_items.find_one(
        {"serial_id": serial_id, "clinic_id": user["clinic_id"]}, {"_id": 0},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Serial item not found")
    if not user_can_see_branch(user, row["branch_id"]):
        raise HTTPException(status_code=403, detail="Branch access denied")
    if row.get("pool") != "demo":
        raise HTTPException(status_code=409, detail="Unit is not in demo pool")
    if row["state"] != "IN_STOCK":
        raise HTTPException(
            status_code=409,
            detail=f"Return the unit to stock first (currently {row['state']})",
        )
    now = datetime.now(timezone.utc).isoformat()
    await db.serial_items.update_one(
        {"serial_id": serial_id},
        {"$set": {"pool": "saleable", "updated_at": now}},
    )
    note = (payload or {}).get("note") if payload else None
    await db.serial_events.insert_one({
        "serial_id": serial_id,
        "from": row["state"], "to": row["state"],
        "at": now, "actor_user_id": user["user_id"],
        "ref_doc": {"kind": "pool-change", "to_pool": "saleable"},
        "note": note or "Removed from demo pool",
    })
    updated = await db.serial_items.find_one({"serial_id": serial_id}, {"_id": 0})
    return deserialize_datetime(updated)


@router.get("/demo-stock")
async def list_demo_stock(
    branch_id: Optional[str] = None,
    user=Depends(get_current_user), db=Depends(get_db),
):
    """Demo units only — both IN_STOCK (available for trial) and TRIAL_OUT
    (currently with a patient). Used by the Demo Stock tab."""
    q = _branch_scope(user)
    q["pool"] = "demo"
    if branch_id:
        if not user_can_see_branch(user, branch_id):
            raise HTTPException(status_code=403, detail="Branch access denied")
        q["branch_id"] = branch_id
    rows = await db.serial_items.find(q, {"_id": 0}).sort("created_at", -1).to_list(500)

    # Hydrate product + patient names for the UI.
    product_ids = list({r["product_id"] for r in rows if r.get("product_id")})
    pmap: dict = {}
    if product_ids:
        async for p in db.ha_products.find(
            {"clinic_id": user["clinic_id"], "product_id": {"$in": product_ids}},
            {"_id": 0, "product_id": 1, "brand": 1, "model": 1, "form_factor": 1},
        ):
            pmap[p["product_id"]] = p
    patient_ids = list({r["current_patient_id"] for r in rows if r.get("current_patient_id")})
    patmap: dict = {}
    if patient_ids:
        async for pt in db.patients.find(
            {"clinic_id": user["clinic_id"], "patient_id": {"$in": patient_ids}},
            {"_id": 0, "patient_id": 1, "name": 1, "mrd": 1, "mobile": 1},
        ):
            patmap[pt["patient_id"]] = pt

    out = []
    for r in rows:
        r = deserialize_datetime(r)
        r["product"] = pmap.get(r.get("product_id"))
        r["current_patient"] = patmap.get(r.get("current_patient_id"))
        out.append(r)
    return out


# ==================== SALEABLE STOCK (Phase B) ====================

@router.get("/saleable-stock")
async def list_saleable_stock(
    branch_id: Optional[str] = None,
    source_kind: Optional[str] = None,   # "vendor" | "borrowed" | None (all)
    user=Depends(get_current_user), db=Depends(get_db),
):
    """Saleable pool — every unit that could be sold to a patient.
    Excludes demo pool and lifecycle-terminated states (SOLD / RETIRED /
    RETURNED). Rows come hydrated with product + optional borrow-source
    context so the UI can render source badges without a second call.
    """
    q = _branch_scope(user)
    q["pool"] = "saleable"
    q["state"] = {"$nin": ["SOLD", "RETIRED", "RETURNED", "DAMAGED"]}
    if branch_id:
        if not user_can_see_branch(user, branch_id):
            raise HTTPException(status_code=403, detail="Branch access denied")
        q["branch_id"] = branch_id
    if source_kind in ("vendor", "borrowed"):
        if source_kind == "vendor":
            q["$or"] = [{"source_kind": "vendor"}, {"source_kind": {"$exists": False}}]
        else:
            q["source_kind"] = "borrowed"
    rows = await db.serial_items.find(q, {"_id": 0}).sort("created_at", -1).to_list(500)

    product_ids = list({r["product_id"] for r in rows if r.get("product_id")})
    pmap: dict = {}
    if product_ids:
        async for p in db.ha_products.find(
            {"clinic_id": user["clinic_id"], "product_id": {"$in": product_ids}},
            {"_id": 0, "product_id": 1, "brand": 1, "model": 1,
             "form_factor": 1, "sale_unit": 1, "mrp": 1, "min_sell_price": 1},
        ):
            pmap[p["product_id"]] = p

    # KPI strip totals
    total = len(rows)
    available = sum(1 for r in rows if r.get("state") == "IN_STOCK")
    on_trial = sum(1 for r in rows if r.get("state") == "TRIAL_OUT")
    reserved = sum(1 for r in rows if r.get("state") == "RESERVED")
    borrowed = sum(1 for r in rows if r.get("source_kind") == "borrowed")

    out = []
    for r in rows:
        r = deserialize_datetime(r)
        r["product"] = pmap.get(r.get("product_id"))
        out.append(r)
    return {
        "totals": {
            "total": total, "available": available, "on_trial": on_trial,
            "reserved": reserved, "borrowed_still_here": borrowed,
        },
        "items": out,
    }


@router.post("/serial-items/{serial_id}/return-borrow")
async def return_borrowed_unit(
    serial_id: str,
    payload: Optional[dict] = None,
    user=Depends(require_roles("clinic_owner", "inventory_manager")),
    db=Depends(get_db),
):
    """Hand a borrowed unit back to the source clinic. The row stays in
    the DB (so history + audit remain) but its state flips to RETURNED
    and it drops off active stock lists.
    """
    row = await db.serial_items.find_one(
        {"serial_id": serial_id, "clinic_id": user["clinic_id"]}, {"_id": 0},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Unit not found")
    if not user_can_see_branch(user, row.get("branch_id")):
        raise HTTPException(status_code=403, detail="Branch access denied")
    if row.get("source_kind") != "borrowed":
        raise HTTPException(status_code=409, detail="Only borrowed units can be returned to source")
    if row.get("state") in ("SOLD", "RETIRED", "RETURNED"):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot return — unit is {row['state']}",
        )
    now = datetime.now(timezone.utc).isoformat()
    note = ((payload or {}).get("note") or "").strip() or "Returned to source clinic"
    await db.serial_items.update_one(
        {"serial_id": serial_id},
        {"$set": {
            "state": "RETURNED",
            "returned_at": now,
            "return_note": note,
            "updated_at": now,
        }},
    )
    await db.serial_events.insert_one({
        "serial_id": serial_id,
        "from": row["state"], "to": "RETURNED",
        "at": now, "actor_user_id": user["user_id"],
        "ref_doc": {"kind": "return-to-source",
                    "borrowed_from": row.get("borrowed_from")},
        "note": note,
    })
    updated = await db.serial_items.find_one({"serial_id": serial_id}, {"_id": 0})
    return deserialize_datetime(updated)


@router.get("/borrowed-attention")
async def borrowed_needs_attention(
    user=Depends(get_current_user), db=Depends(get_db),
):
    """Fuel for the Main Dashboard "Needs Attention" widget — count and
    top-5 preview of borrowed units still sitting in this clinic (i.e.
    not yet returned to source).
    """
    q = _branch_scope(user)
    q["source_kind"] = "borrowed"
    q["state"] = {"$nin": ["RETURNED", "RETIRED"]}
    rows = await db.serial_items.find(
        q, {"_id": 0, "serial_id": 1, "serial_no": 1, "product_id": 1,
            "borrowed_from": 1, "borrow_reason": 1, "borrowed_at": 1, "state": 1},
    ).sort("borrowed_at", 1).to_list(200)

    # Hydrate the top 5 with brand/model for the widget preview.
    top = rows[:5]
    product_ids = list({r.get("product_id") for r in top if r.get("product_id")})
    pmap: dict = {}
    if product_ids:
        async for p in db.ha_products.find(
            {"clinic_id": user["clinic_id"], "product_id": {"$in": product_ids}},
            {"_id": 0, "product_id": 1, "brand": 1, "model": 1},
        ):
            pmap[p["product_id"]] = p
    for r in top:
        r["product"] = pmap.get(r.get("product_id"))

    return {"count": len(rows), "top": top}


# ==================== ACCESSORY STOCK ====================

@router.get("/accessory-stock", response_model=List[AccessoryStock])
async def list_accessory_stock(
    branch_id: Optional[str] = None,
    product_id: Optional[str] = None,
    low_stock_only: bool = False,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    q = _branch_scope(user)
    if branch_id:
        if not user_can_see_branch(user, branch_id):
            raise HTTPException(status_code=403, detail="Branch access denied")
        q["branch_id"] = branch_id
    if product_id:
        q["product_id"] = product_id
    if low_stock_only:
        q["$expr"] = {"$lte": ["$qty_on_hand", "$reorder_level"]}
    rows = await db.accessory_stock.find(q, {"_id": 0}).sort("updated_at", -1).to_list(500)
    return [deserialize_datetime(r) for r in rows]


@router.get("/accessory-stock-hydrated")
async def list_accessory_stock_hydrated(
    branch_id: Optional[str] = None,
    product_id: Optional[str] = None,
    low_stock_only: bool = False,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Same rows as `/accessory-stock` but with the product SKU + branch
    name joined in — saves the frontend a batch fetch. Also returns a
    KPI strip (total_skus, zero_stock, low_stock, ok_stock) so the tab
    header lights up in one round-trip.
    """
    q = _branch_scope(user)
    if branch_id:
        if not user_can_see_branch(user, branch_id):
            raise HTTPException(status_code=403, detail="Branch access denied")
        q["branch_id"] = branch_id
    if product_id:
        q["product_id"] = product_id
    if low_stock_only:
        q["$expr"] = {"$lte": ["$qty_on_hand", "$reorder_level"]}
    rows = await db.accessory_stock.find(q, {"_id": 0}).sort("updated_at", -1).to_list(500)

    # Hydrate product + branch names
    product_ids = list({r["product_id"] for r in rows if r.get("product_id")})
    branch_ids = list({r["branch_id"] for r in rows if r.get("branch_id")})
    pmap: dict = {}
    if product_ids:
        async for p in db.ha_products.find(
            {"clinic_id": user["clinic_id"], "product_id": {"$in": product_ids}},
            {"_id": 0, "product_id": 1, "brand": 1, "model": 1, "form_factor": 1,
             "accessory_kind": 1, "accessory_category": 1, "mrp": 1, "gst_rate": 1},
        ):
            pmap[p["product_id"]] = p
    bmap: dict = {}
    if branch_ids:
        async for b in db.branches.find(
            {"clinic_id": user["clinic_id"], "branch_id": {"$in": branch_ids}},
            {"_id": 0, "branch_id": 1, "name": 1, "city": 1},
        ):
            bmap[b["branch_id"]] = b

    # Full unfiltered KPI totals (independent of low_stock_only)
    kpi_q = _branch_scope(user)
    total_skus = await db.accessory_stock.count_documents(kpi_q)
    zero_stock = await db.accessory_stock.count_documents({**kpi_q, "qty_on_hand": 0})
    low_stock = await db.accessory_stock.count_documents({
        **kpi_q,
        "$expr": {"$and": [
            {"$gt": ["$qty_on_hand", 0]},
            {"$lte": ["$qty_on_hand", "$reorder_level"]},
        ]},
    })
    ok_stock = max(0, total_skus - zero_stock - low_stock)

    out = []
    for r in rows:
        r = deserialize_datetime(r)
        r["product"] = pmap.get(r.get("product_id"))
        r["branch"] = bmap.get(r.get("branch_id"))
        out.append(r)
    return {
        "kpis": {
            "total_skus": total_skus, "zero_stock": zero_stock,
            "low_stock": low_stock, "ok_stock": ok_stock,
        },
        "items": out,
    }


class InitAccessoryStockPayload(BaseModel):
    """Bulk-create `accessory_stock` rows for a product across variants × branches.

    Idempotent: if a row already exists for (product, branch, variant) the
    existing row is left untouched. Newly created rows start at qty=0.
    Pass an empty `variants` list to init a single row per branch (for
    accessories that have no size/power variants — e.g. wax guards).
    """
    branch_ids: List[str]
    variants: List[str] = []                # empty means "no variant" (single row)
    reorder_level: int = 0


@router.post("/products/{product_id}/init-accessory-stock")
async def init_accessory_stock(
    product_id: str,
    payload: InitAccessoryStockPayload,
    user=Depends(require_roles("clinic_owner", "inventory_manager")),
    db=Depends(get_db),
):
    """Create the zero-qty `accessory_stock` rows so the Batch Stock tab
    lights up immediately after the SKU is added to the catalogue."""
    product = await db.ha_products.find_one(
        {"product_id": product_id, "clinic_id": user["clinic_id"]}, {"_id": 0},
    )
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    if product.get("is_serialised", True):
        raise HTTPException(
            status_code=400,
            detail="Init-stock only applies to non-serialised (batch) accessories",
        )
    for bid in payload.branch_ids:
        if not user_can_see_branch(user, bid):
            raise HTTPException(status_code=403, detail=f"Branch {bid} not in your access")

    variants = payload.variants or [None]  # None = a single unified row
    now_iso = datetime.now(timezone.utc).isoformat()
    created = 0
    skipped = 0
    for bid in payload.branch_ids:
        for variant in variants:
            match = {"clinic_id": user["clinic_id"], "product_id": product_id,
                     "branch_id": bid, "variant": variant}
            exists = await db.accessory_stock.find_one(match, {"_id": 0, "sku_id": 1})
            if exists:
                skipped += 1
                continue
            row = AccessoryStock(
                clinic_id=user["clinic_id"],
                branch_id=bid,
                product_id=product_id,
                variant=variant,
                qty_on_hand=0,
                reorder_level=int(payload.reorder_level or 0),
                updated_at=now_iso,
            )
            await db.accessory_stock.insert_one(serialize_datetime(row.model_dump()))
            created += 1
    return {"created": created, "skipped_existing": skipped}


class _RicPresetPayload(BaseModel):
    """One-tap create-and-seed for RIC Receivers.

    Creates a catalogue Product with `form_factor="accessory"`,
    `is_serialised=false`, and the 9-variant preset labels populated:

        1M · 2M · 3M · 10P · 2P · 3P · 1S · 2S · 3S

        (M = Medium/Moderate; P = Power; S = Standard)

    Also seeds the 9 `accessory_stock` rows per branch at qty=0. Owner
    or inventory manager only.
    """
    brand: str
    model: str = "RIC Receiver"
    branch_ids: List[str]
    mrp: float = 0.0
    gst_rate: float = 18.0
    hsn: str = "9021"
    reorder_level: int = 0


RIC_RECEIVER_VARIANTS = ["1M", "2M", "3M", "10P", "2P", "3P", "1S", "2S", "3S"]

# ---- Preset catalogue -------------------------------------------------
# Each preset is a shortcut for the "+ New Accessory" modal — one tap
# creates the SKU + seeds one zero-qty stock row per (branch × variant).
# Adding a new preset is a 5-line dict below.
_ACCESSORY_PRESETS = {
    "ric_receiver": {
        "default_model": "RIC Receiver",
        "accessory_kind": "ric_receiver",
        "accessory_category": "replaceable",
        "variants": RIC_RECEIVER_VARIANTS,
        "hsn": "9021", "gst_rate": 18.0,
    },
    "silicone_dome": {
        "default_model": "Silicone Dome",
        "accessory_kind": "tip",
        "accessory_category": "consumable",
        # Standard 4-size dome family sold by every major HA manufacturer:
        # Small · Medium · Large · Power (closed, higher-vent-loss).
        "variants": ["S", "M", "L", "Power"],
        "hsn": "9021", "gst_rate": 18.0,
    },
}


@router.get("/accessory-presets")
async def list_accessory_presets(user=Depends(get_current_user)):  # noqa: ARG001
    """Return every preset the UI can offer as a one-tap button.
    Keeps the frontend in sync when new presets get added server-side."""
    return {
        "presets": [
            {"key": k, "label": v["default_model"], **v}
            for k, v in _ACCESSORY_PRESETS.items()
        ]
    }


@router.post("/products/preset-ric-receiver")
async def preset_ric_receiver(
    payload: _RicPresetPayload,
    user=Depends(require_roles("clinic_owner", "inventory_manager")),
    db=Depends(get_db),
):
    """Back-compat shim — kept so the earlier frontend build keeps working.
    Internally delegates to the generic preset seeder."""
    return await _seed_accessory_preset(
        db, user, preset_key="ric_receiver",
        brand=payload.brand, model=payload.model,
        branch_ids=payload.branch_ids,
        mrp=payload.mrp, gst_rate=payload.gst_rate,
        hsn=payload.hsn, reorder_level=payload.reorder_level,
    )


class _PresetSeedPayload(BaseModel):
    preset_key: str                    # "ric_receiver" | "silicone_dome" | …
    brand: str
    model: Optional[str] = None        # defaults to preset's default_model
    branch_ids: List[str]
    mrp: float = 0.0
    gst_rate: Optional[float] = None
    hsn: Optional[str] = None
    reorder_level: int = 0


@router.post("/products/preset-seed")
async def preset_seed(
    payload: _PresetSeedPayload,
    user=Depends(require_roles("clinic_owner", "inventory_manager")),
    db=Depends(get_db),
):
    """Generic one-tap accessory preset seeder. Supports RIC receivers,
    silicone domes (S/M/L/Power) — and any future preset registered in
    `_ACCESSORY_PRESETS`. Idempotent: if the exact (brand, model,
    accessory_kind) already exists for this clinic, we re-use that
    product row and only seed missing stock rows (no duplicates)."""
    return await _seed_accessory_preset(
        db, user,
        preset_key=payload.preset_key,
        brand=payload.brand, model=payload.model,
        branch_ids=payload.branch_ids,
        mrp=payload.mrp, gst_rate=payload.gst_rate,
        hsn=payload.hsn, reorder_level=payload.reorder_level,
    )


async def _seed_accessory_preset(
    db, user, *,
    preset_key: str,
    brand: str,
    model: Optional[str],
    branch_ids: List[str],
    mrp: float = 0.0,
    gst_rate: Optional[float] = None,
    hsn: Optional[str] = None,
    reorder_level: int = 0,
):
    preset = _ACCESSORY_PRESETS.get(preset_key)
    if not preset:
        raise HTTPException(status_code=400, detail=f"Unknown preset '{preset_key}'")
    for bid in branch_ids:
        if not user_can_see_branch(user, bid):
            raise HTTPException(status_code=403, detail=f"Branch {bid} not in your access")

    resolved_model = (model or "").strip() or preset["default_model"]
    resolved_brand = brand.strip()
    if not resolved_brand:
        raise HTTPException(status_code=400, detail="Brand is required")

    # ---- Idempotency: re-use an existing SKU that matches shape ----
    # Prevents "Quick-add" from spawning duplicate catalogue rows when
    # tapped twice.
    existing = await db.ha_products.find_one({
        "clinic_id": user["clinic_id"],
        "brand": resolved_brand,
        "model": resolved_model,
        "accessory_kind": preset["accessory_kind"],
        "active": True,
    }, {"_id": 0})
    reused = existing is not None
    if reused:
        product_dict = existing
    else:
        product = Product(
            clinic_id=user["clinic_id"],
            brand=resolved_brand,
            model=resolved_model,
            form_factor="accessory",
            is_serialised=False,
            mrp=float(mrp or 0),
            gst_rate=float(gst_rate if gst_rate is not None else preset["gst_rate"]),
            hsn=(hsn or preset["hsn"]),
            accessory_kind=preset["accessory_kind"],
            accessory_category=preset["accessory_category"],
            variant_labels=list(preset["variants"]),
        )
        product_dict = product.model_dump()
        await db.ha_products.insert_one(serialize_datetime(product_dict))

    # ---- Seed / top-up stock rows (idempotent per row) ----
    now_iso = datetime.now(timezone.utc).isoformat()
    created = 0
    skipped = 0
    for bid in branch_ids:
        for variant in preset["variants"]:
            match = {
                "clinic_id": user["clinic_id"],
                "product_id": product_dict["product_id"],
                "branch_id": bid, "variant": variant,
            }
            already = await db.accessory_stock.find_one(match, {"_id": 0, "sku_id": 1})
            if already:
                skipped += 1
                continue
            row = AccessoryStock(
                clinic_id=user["clinic_id"],
                branch_id=bid,
                product_id=product_dict["product_id"],
                variant=variant,
                qty_on_hand=0,
                reorder_level=int(reorder_level or 0),
                updated_at=now_iso,
            )
            await db.accessory_stock.insert_one(serialize_datetime(row.model_dump()))
            created += 1
    return {
        "product": deserialize_datetime(product_dict),
        "reused_existing_product": reused,
        "stock_rows_created": created,
        "stock_rows_skipped_existing": skipped,
    }


@router.post("/accessory-stock/{sku_id}/adjust")
async def adjust_accessory_stock(
    sku_id: str, payload: AccessoryAdjust,
    user=Depends(require_roles("inventory_manager", "clinic_owner")),
    db=Depends(get_db),
):
    """Manual qty adjust. Writes to `accessory_events` for audit.

    NAV-010 · INV-004 · Uses atomic ``$inc`` with a stock-sufficiency
    guard on negative deltas so two concurrent adjustments cannot
    lost-update each other and no adjustment can drive qty below zero.
    """
    sku = await db.accessory_stock.find_one(
        {"sku_id": sku_id, "clinic_id": user["clinic_id"]},
        {"_id": 0, "branch_id": 1, "qty_on_hand": 1},
    )
    if not sku:
        raise HTTPException(status_code=404, detail="Accessory SKU not found")
    if not user_can_see_branch(user, sku["branch_id"]):
        raise HTTPException(status_code=403, detail="Branch access denied")

    delta = int(payload.delta)
    now = datetime.now(timezone.utc).isoformat()
    match: dict = {"sku_id": sku_id, "clinic_id": user["clinic_id"]}
    if delta < 0:
        # Only apply the decrement if stock is at least |delta|. The
        # ``$expr`` shape keeps this a single atomic operation.
        match["qty_on_hand"] = {"$gte": -delta}

    result = await db.accessory_stock.find_one_and_update(
        match,
        {"$inc": {"qty_on_hand": delta},
         "$set": {"updated_at": now}},
        projection={"_id": 0, "qty_on_hand": 1},
        return_document=ReturnDocument.AFTER,
    )
    if result is None:
        # Only reachable for negative deltas where the guard failed.
        fresh = await db.accessory_stock.find_one(
            {"sku_id": sku_id, "clinic_id": user["clinic_id"]},
            {"_id": 0, "qty_on_hand": 1},
        )
        current = int((fresh or {}).get("qty_on_hand", 0))
        raise HTTPException(
            status_code=409,
            detail=(
                f"Adjustment would drive qty below zero "
                f"(current: {current}, delta: {delta})"
            ),
        )

    new_qty = int(result["qty_on_hand"])
    before = new_qty - delta
    await db.accessory_events.insert_one({
        "sku_id": sku_id,
        "clinic_id": user["clinic_id"],
        "branch_id": sku["branch_id"],
        "delta": delta,
        "reason": payload.reason,
        "at": now,
        "actor_user_id": user["user_id"],
        "before": before,
        "after": new_qty,
    })
    return {"sku_id": sku_id, "qty_on_hand": new_qty}
