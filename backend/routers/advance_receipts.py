"""Advance Receipt (Phase 2A · Receipt-only) — FastAPI router.

Urgent client requirement. See models/_advance.py for the strict scope
boundary. This router NEVER touches invoices / payments / serial_items /
accessory_stock. All writes are scoped to two isolated collections:

  * `db.advance_receipts`
  * `db.advance_audit_events`

Idempotency
-----------
The `POST /api/advance-receipts` endpoint requires a valid
`Idempotency-Key` header per the client's mandate (unlike NAV-012's
optional-header design on invoice payments). A missing key produces
a controlled 400; a replayed key returns the cached first response
byte-for-byte. Scope = "advance_receipt".

RBAC
----
  * CREATE  → front_desk, accounts, clinic_owner (super_admin/founder bypass)
  * LIST    → front_desk, accounts, clinic_owner, audiologist (read-only)
  * READ    → front_desk, accounts, clinic_owner, audiologist (read-only)
  * VOID    → accounts, clinic_owner (super_admin/founder bypass)
  * RECEIPT → same as READ

Multi-tenant
------------
Every DB query includes `clinic_id: user["clinic_id"]`. Cross-tenant
reads are impossible.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from auth import get_current_user, require_roles
from database import get_db
from models._advance import (
    AdvanceAllocation,
    AdvanceAllocationCreate,
    AdvanceAuditEvent,
    AdvanceReceipt,
    AdvanceReceiptCreate,
    AdvanceVoidIn,
)
from utils.idempotency import IdempotencyContext, extract_idempotency_key

_log = logging.getLogger(__name__)

# NAV-011 · Phase 2B.2 · imports for the allocation writer.
# `MONEY_TOL` = 0.01 (1-paisa) — same tolerance used by
# `record_payment_atomic` on the invoice CAS guard.
from billing import MONEY_TOL, _deserialize, record_payment_atomic  # noqa: E402

from pymongo import ReturnDocument  # noqa: E402

router = APIRouter(prefix="/api/advance-receipts", tags=["advance-receipts"])

CREATE_ROLES = ("front_desk", "accounts", "clinic_owner")
READ_ROLES = ("front_desk", "accounts", "clinic_owner", "audiologist")
VOID_ROLES = ("accounts", "clinic_owner")
# NAV-011 · Phase 2B.2 · Allocation-writer roles — mirror the invoice
# `add_payment` RBAC (front_desk / accounts / clinic_owner) because an
# allocation IS a payment from a financial-integrity standpoint. Super
# admin / founder bypass via `require_roles`.
ALLOCATE_ROLES = ("front_desk", "accounts", "clinic_owner")


# ─────────────────────────────────────────────────────────────────────
# Numbering: AR/YYYY/NNNNNN — clinic-scoped, year-reset
# ─────────────────────────────────────────────────────────────────────

async def _next_advance_receipt_no(db, clinic_id: str) -> str:
    """Generate `AR/YYYY/NNNNNN` — clinic-scoped, year-reset counter.

    Same shape as invoice numbering (`INV/YYYY/NNNNNN`) but keyed on a
    dedicated counter so advance and invoice sequences NEVER collide.
    """
    year = datetime.utcnow().year
    res = await db.counters.find_one_and_update(
        {"_id": f"advance_receipt:{clinic_id}:{year}"},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=True,
    )
    seq = (res or {}).get("seq", 1)
    return f"AR/{year}/{str(seq).zfill(6)}"


async def _next_advance_allocation_no(db, clinic_id: str) -> str:
    """NAV-011 · Phase 2B.2 · Generate `AA/YYYY/NNNNNN` — clinic-scoped,
    year-reset counter for the Advance Allocation ledger. Keyed on a
    dedicated `advance_allocation:*` counter so AA / AR / INV counters
    cannot collide.
    """
    year = datetime.utcnow().year
    res = await db.counters.find_one_and_update(
        {"_id": f"advance_allocation:{clinic_id}:{year}"},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=True,
    )
    seq = (res or {}).get("seq", 1)
    return f"AA/{year}/{str(seq).zfill(6)}"


# ─────────────────────────────────────────────────────────────────────
# Audit helper (append-only, never blocks the primary write on failure)
# ─────────────────────────────────────────────────────────────────────

async def _emit_audit(
    db,
    *,
    clinic_id: str,
    receipt_id: str,
    receipt_no: Optional[str],
    kind: str,
    actor: dict,
    payload: Optional[dict] = None,
) -> None:
    try:
        evt = AdvanceAuditEvent(
            clinic_id=clinic_id,
            receipt_id=receipt_id,
            receipt_no=receipt_no,
            kind=kind,  # type: ignore[arg-type]
            actor_user_id=actor.get("user_id"),
            actor_name=actor.get("name"),
            actor_role=actor.get("role"),
            payload=payload,
        )
        await db.advance_audit_events.insert_one(evt.model_dump())
    except Exception:
        _log.exception(
            "advance-receipt audit event skipped (non-fatal): "
            "receipt_id=%s kind=%s", receipt_id, kind,
        )


# ─────────────────────────────────────────────────────────────────────
# CREATE — POST /api/advance-receipts
# ─────────────────────────────────────────────────────────────────────

@router.post("")
async def create_advance_receipt(
    payload: AdvanceReceiptCreate,
    request: Request,
    user=Depends(require_roles(*CREATE_ROLES)),
    db=Depends(get_db),
):
    """Create an Advance Receipt.

    Requirements:
      * `Idempotency-Key` header MANDATORY (400 if missing/invalid).
      * Patient must belong to caller's clinic (404 otherwise).
      * received_amount > 0 (Pydantic-enforced).
      * NO invoice / payment / inventory side-effects.
    """
    # ── Mandatory Idempotency-Key gate ─────────────────────────────
    raw_key = request.headers.get("Idempotency-Key") or request.headers.get("idempotency-key")
    if raw_key is None or raw_key.strip() == "":
        raise HTTPException(
            status_code=400,
            detail="Idempotency-Key header is required for advance-receipt creation.",
        )
    # Validate shape via the shared extractor (raises 400 on malformed).
    extract_idempotency_key(request)

    # ── Idempotency context (mandatory scope) ──────────────────────
    idem = await IdempotencyContext.enter(
        request, db,
        scope="advance_receipt", clinic_id=user["clinic_id"],
        actor=user,
        payload=payload.model_dump(),
        route="/api/advance-receipts",
        operation_collection="advance_receipts",
    )
    if idem.replayed:
        body, status, headers = idem.replay_response()
        return JSONResponse(content=body, status_code=status, headers=headers)

    try:
        # ── Patient must be tenant-owned ───────────────────────────
        pat = await db.patients.find_one(
            {"patient_id": payload.patient_id, "clinic_id": user["clinic_id"]},
            {"_id": 0, "patient_id": 1, "name": 1, "mobile": 1, "mrd": 1, "branch_id": 1},
        )
        if not pat:
            raise HTTPException(
                status_code=404,
                detail="Patient not found in this clinic",
            )

        receipt_no = await _next_advance_receipt_no(db, user["clinic_id"])
        # NAV-011 · Phase 2B.2 · New receipts are born with a live
        # balance ledger (available_balance = received_amount,
        # allocated_total = 0). Legacy Phase 2A receipts (127 rows at
        # cut-over) intentionally remain with both fields = None until
        # a separately-authorized backfill runs. The allocation endpoint
        # rejects allocation attempts against uninitialised (None)
        # receipts with a clear 409 so the invariant is preserved.
        _received = float(payload.received_amount)
        receipt = AdvanceReceipt(
            receipt_no=receipt_no,
            clinic_id=user["clinic_id"],
            branch_id=pat.get("branch_id") or user.get("branch_id"),
            patient_id=pat["patient_id"],
            patient_name=pat.get("name"),
            patient_mobile=pat.get("mobile"),
            patient_mrd=pat.get("mrd"),
            received_amount=_received,
            available_balance=_received,
            allocated_total=0.0,
            method=payload.method,
            reference=payload.reference,
            purpose_note=payload.purpose_note,
            received_at=payload.received_at or datetime.now(timezone.utc).isoformat(),
            created_by_user_id=user.get("user_id"),
            created_by_name=user.get("name"),
        )
        doc = receipt.model_dump()
        # Bind the pre-generated idempotency correlation id so a crash-
        # recovery retry can detect whether this row landed.
        if idem.enabled:
            doc["idempotency_correlation_id"] = idem.correlation_id
        await db.advance_receipts.insert_one(dict(doc))

        await _emit_audit(
            db, clinic_id=user["clinic_id"],
            receipt_id=receipt.receipt_id, receipt_no=receipt.receipt_no,
            kind="created", actor=user,
            payload={
                "received_amount": receipt.received_amount,
                "method": receipt.method,
                "patient_id": receipt.patient_id,
            },
        )

        response_body = receipt.model_dump()
        if idem.enabled:
            await idem.complete(
                http_status=200, response_body=response_body,
                operation_id=receipt.receipt_id,
            )
        return response_body
    except HTTPException as exc:
        if idem.enabled:
            await idem.fail(
                http_status=exc.status_code,
                response_body={"detail": exc.detail},
                detail=str(exc.detail),
            )
        raise


# ─────────────────────────────────────────────────────────────────────
# LIST — GET /api/advance-receipts
# ─────────────────────────────────────────────────────────────────────

@router.get("")
async def list_advance_receipts(
    patient_id: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None, regex=r"^(active|voided)$"),
    date_from: Optional[str] = Query(default=None, description="ISO date"),
    date_to: Optional[str] = Query(default=None, description="ISO date"),
    limit: int = Query(default=100, ge=1, le=500),
    user=Depends(require_roles(*READ_ROLES)),
    db=Depends(get_db),
):
    """List advance receipts scoped to the caller's clinic. Optional
    filters: patient_id, status, received_at date range.
    """
    query: dict = {"clinic_id": user["clinic_id"]}
    if patient_id:
        query["patient_id"] = patient_id
    if status:
        query["status"] = status
    if date_from or date_to:
        query["received_at"] = {}
        if date_from:
            query["received_at"]["$gte"] = date_from
        if date_to:
            # Inclusive upper bound: match all timestamps within the day.
            query["received_at"]["$lte"] = date_to + "T23:59:59.999999+00:00"

    rows = await db.advance_receipts.find(query, {"_id": 0}).sort(
        "created_at", -1
    ).to_list(limit)

    # Aggregate totals for the caller's current filter (active only).
    active_total = 0.0
    for r in rows:
        if r.get("status") == "active":
            active_total += float(r.get("received_amount") or 0)

    return {
        "items": rows,
        "count": len(rows),
        "active_total": round(active_total, 2),
    }


# ─────────────────────────────────────────────────────────────────────
# READ — GET /api/advance-receipts/{receipt_id}
# ─────────────────────────────────────────────────────────────────────

@router.get("/{receipt_id}")
async def get_advance_receipt(
    receipt_id: str,
    user=Depends(require_roles(*READ_ROLES)),
    db=Depends(get_db),
):
    r = await db.advance_receipts.find_one(
        {"receipt_id": receipt_id, "clinic_id": user["clinic_id"]},
        {"_id": 0},
    )
    if not r:
        raise HTTPException(status_code=404, detail="Advance receipt not found")
    return r


# ─────────────────────────────────────────────────────────────────────
# LIST ALLOCATIONS — GET /api/advance-receipts/{receipt_id}/allocations
# ─────────────────────────────────────────────────────────────────────
# Phase 2B.3 (UX) · read-only ledger view over the Phase 2B.2
# collection. Tenant-scoped. Used by the Apply-Advance UI ("View
# Allocations" action) and by patient-profile audit trails.

@router.get("/{receipt_id}/allocations")
async def list_advance_allocations(
    receipt_id: str,
    status: Optional[str] = Query(default=None, pattern=r"^(active|voided)$"),
    limit: int = Query(default=100, ge=1, le=500),
    user=Depends(require_roles(*READ_ROLES)),
    db=Depends(get_db),
):
    """Return the allocation history for a specific Advance Receipt.

    Response includes both `active` and `voided` allocations by default
    so the caller can render the full audit trail. Amount aggregates
    are computed from the ledger, not from the (denormalised) receipt
    document — the ledger is the source of truth.
    """
    # 1. Tenant-scoped receipt lookup (fail-fast 404 if receipt does
    #    not belong to this clinic).
    receipt = await db.advance_receipts.find_one(
        {"receipt_id": receipt_id, "clinic_id": user["clinic_id"]},
        {"_id": 0, "receipt_id": 1, "receipt_no": 1, "patient_id": 1,
         "patient_name": 1, "received_amount": 1, "available_balance": 1,
         "allocated_total": 1, "status": 1},
    )
    if not receipt:
        raise HTTPException(status_code=404, detail="Advance receipt not found")

    # 2. Ledger fetch — status filter is optional.
    query: dict = {
        "clinic_id": user["clinic_id"],
        "advance_receipt_id": receipt_id,
    }
    if status:
        query["status"] = status

    rows = await db.advance_allocations.find(
        query, {"_id": 0},
    ).sort("created_at", -1).to_list(limit)

    # 3. Aggregate from the ledger (source of truth).
    total_active = round(sum(
        float(r.get("amount") or 0) for r in rows if r.get("status") == "active"
    ), 2)
    total_voided = round(sum(
        float(r.get("amount") or 0) for r in rows if r.get("status") == "voided"
    ), 2)

    return {
        "receipt": receipt,
        "items": rows,
        "count": len(rows),
        "total_active_amount": total_active,
        "total_voided_amount": total_voided,
    }


# ─────────────────────────────────────────────────────────────────────
# VOID — POST /api/advance-receipts/{receipt_id}/void
# ─────────────────────────────────────────────────────────────────────

@router.post("/{receipt_id}/void")
async def void_advance_receipt(
    receipt_id: str,
    payload: AdvanceVoidIn,
    user=Depends(require_roles(*VOID_ROLES)),
    db=Depends(get_db),
):
    """Void an active advance receipt. CAS on `status=active` guarantees
    exactly-once transition even under concurrent void attempts. Reason
    is mandatory (Pydantic-enforced).

    Phase 2A does NOT emit a refund — the money remains "held" in the
    clinic's books; the void just marks the acknowledgement itself as
    voided. Phase 2C will introduce controlled advance-refund flows.

    NAV-011 · Phase 2B.2 · Safety-guard tightening — voiding a receipt
    that has ANY active allocations is REJECTED with 409. The CAS `$expr`
    treats missing / null `allocated_total` (legacy Phase 2A rows and
    the one Phase 2B.1-transition row) as 0, so those receipts remain
    voidable as before. Only receipts with a positive live
    `allocated_total` are blocked. Money invariant preserved — an
    allocated receipt cannot silently vanish.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    res = await db.advance_receipts.find_one_and_update(
        {
            "receipt_id": receipt_id,
            "clinic_id": user["clinic_id"],
            "status": "active",
            # `$ifNull` maps missing/null allocated_total → 0. MONEY_TOL
            # gives 1-paisa float tolerance in the (theoretical) case
            # where allocated_total drifts to a sub-paisa residue.
            "$expr": {
                "$lte": [{"$ifNull": ["$allocated_total", 0]}, MONEY_TOL],
            },
        },
        {"$set": {
            "status": "voided",
            "voided_at": now_iso,
            "void_reason": payload.reason,
            "voided_by_user_id": user.get("user_id"),
            "voided_by_name": user.get("name"),
        }},
        projection={"_id": 0},
        return_document=True,
    )
    if not res:
        existing = await db.advance_receipts.find_one(
            {"receipt_id": receipt_id, "clinic_id": user["clinic_id"]},
            {"_id": 0, "status": 1, "allocated_total": 1},
        )
        if not existing:
            raise HTTPException(status_code=404, detail="Advance receipt not found")
        # Distinguish "wrong state" vs "has live allocations" for a
        # helpful, deterministic 409 message.
        alloc_total = round(float(existing.get("allocated_total") or 0), 2)
        if existing.get("status") != "active":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Cannot void — receipt is in state {existing['status']!r} "
                    f"(only 'active' can be voided)"
                ),
            )
        # status is active but allocated_total > MONEY_TOL → blocked.
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot void — advance has ₹{alloc_total:.2f} in active "
                "allocations. Void the allocation(s) first, then retry."
            ),
        )
    await _emit_audit(
        db, clinic_id=user["clinic_id"],
        receipt_id=receipt_id, receipt_no=res.get("receipt_no"),
        kind="voided", actor=user,
        payload={"reason": payload.reason},
    )
    return res


# ─────────────────────────────────────────────────────────────────────
# PRINTABLE — GET /api/advance-receipts/{receipt_id}/receipt.pdf
# ─────────────────────────────────────────────────────────────────────

def _render_receipt_html(receipt: dict, clinic: dict) -> str:
    """Render a print-ready HTML acknowledgement. Deliberately no GST /
    HSN / SAC blocks — this is NOT a Tax Invoice.
    """
    amount = float(receipt.get("received_amount") or 0)
    amount_str = f"₹{amount:,.2f}"
    method_lbl = str(receipt.get("method", "")).replace("_", " ").title()
    disclaimer = (
        "This is an Advance Receipt / Payment Acknowledgement only. "
        "It is NOT a Tax Invoice, does NOT constitute a supply of goods or "
        "services, and does NOT include GST. A Tax Invoice will be issued "
        "separately once the product or service is finalised and delivered."
    )
    clinic_name = (clinic or {}).get("name", "AUDINEXA Clinic")
    clinic_addr = ", ".join(filter(None, [
        (clinic or {}).get("address_line1"),
        (clinic or {}).get("city"),
        (clinic or {}).get("state"),
    ]))
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>Advance Receipt {receipt.get('receipt_no', '')}</title>
<style>
  @page {{ size: A5 portrait; margin: 14mm; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; color: #0f172a; margin: 0; padding: 24px; background: #fff; }}
  .wrap {{ max-width: 640px; margin: 0 auto; border: 2px dashed #0ea5e9; padding: 24px 28px; border-radius: 12px; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; text-transform: uppercase; letter-spacing: 1.5px; color: #0369a1; }}
  h2 {{ font-size: 14px; margin: 0 0 16px; color: #64748b; font-weight: 500; letter-spacing: 0.5px; }}
  .clinic {{ text-align: right; font-size: 12px; color: #334155; }}
  .clinic strong {{ display: block; font-size: 14px; color: #0f172a; }}
  .row {{ display: flex; justify-content: space-between; gap: 16px; margin: 14px 0; }}
  .grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px 24px; padding: 12px 0; border-top: 1px solid #e2e8f0; border-bottom: 1px solid #e2e8f0; margin: 14px 0; }}
  .grid dt {{ font-size: 10px; text-transform: uppercase; color: #94a3b8; letter-spacing: 0.6px; }}
  .grid dd {{ margin: 0; font-size: 13px; color: #0f172a; font-weight: 600; }}
  .amount {{ text-align: center; padding: 18px 0; background: linear-gradient(135deg, #eff6ff, #f0fdfa); border-radius: 8px; margin: 18px 0; }}
  .amount .label {{ font-size: 11px; text-transform: uppercase; color: #0369a1; letter-spacing: 0.6px; }}
  .amount .value {{ font-size: 30px; font-weight: 700; color: #0f172a; margin-top: 4px; }}
  .disclaimer {{ margin-top: 22px; padding: 12px 14px; background: #fef3c7; border-left: 4px solid #f59e0b; font-size: 11px; color: #78350f; line-height: 1.55; }}
  .sig {{ display: flex; justify-content: space-between; margin-top: 40px; font-size: 11px; color: #475569; }}
  .sig div {{ text-align: center; }}
  .sig .line {{ border-top: 1px solid #94a3b8; padding-top: 4px; min-width: 160px; }}
  .voided {{ position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; pointer-events: none; }}
  .voided span {{ font-size: 90px; color: rgba(220, 38, 38, 0.18); font-weight: 900; letter-spacing: 12px; transform: rotate(-18deg); }}
  .footer {{ margin-top: 22px; text-align: center; font-size: 10px; color: #94a3b8; }}
</style>
</head>
<body>
  <div class="wrap" style="position: relative;">
    {'<div class="voided"><span>VOIDED</span></div>' if receipt.get('status') == 'voided' else ''}
    <div class="row">
      <div>
        <h1>Advance Receipt</h1>
        <h2>Payment Acknowledgement</h2>
      </div>
      <div class="clinic">
        <strong>{clinic_name}</strong>
        <div>{clinic_addr}</div>
      </div>
    </div>

    <dl class="grid">
      <div><dt>Receipt No.</dt><dd>{receipt.get('receipt_no', '')}</dd></div>
      <div><dt>Received On</dt><dd>{(receipt.get('received_at') or '')[:19].replace('T', ' ')}</dd></div>
      <div><dt>Patient</dt><dd>{receipt.get('patient_name') or receipt.get('patient_id', '')}</dd></div>
      <div><dt>MRD / Mobile</dt><dd>{receipt.get('patient_mrd') or receipt.get('patient_mobile') or '—'}</dd></div>
      <div><dt>Payment Method</dt><dd>{method_lbl}</dd></div>
      <div><dt>Reference</dt><dd>{receipt.get('reference') or '—'}</dd></div>
    </dl>

    <div class="amount">
      <div class="label">Amount Received (Advance)</div>
      <div class="value">{amount_str}</div>
    </div>

    {f'<div style="font-size: 12px; color: #334155;"><strong>Purpose:</strong> {receipt.get("purpose_note")}</div>' if receipt.get('purpose_note') else ''}

    <div class="disclaimer">
      <strong>Important:</strong> {disclaimer}
    </div>

    <div class="sig">
      <div><div class="line">Received By ({receipt.get('created_by_name') or '—'})</div></div>
      <div><div class="line">Payer's Signature</div></div>
    </div>

    <div class="footer">
      Receipt ID: {receipt.get('receipt_id', '')} · Generated by AUDINEXA
    </div>
  </div>
</body>
</html>"""


@router.get("/{receipt_id}/receipt.pdf", response_class=HTMLResponse)
async def render_receipt_document(
    receipt_id: str,
    user=Depends(require_roles(*READ_ROLES)),
    db=Depends(get_db),
):
    """Return a print-ready HTML page (browser-print → PDF). Kept as HTML
    so no server-side PDF engine is required — the browser's built-in
    print dialog converts it to PDF on demand. The URL path retains the
    `.pdf` suffix so downstream integrations can rely on a stable
    endpoint contract when we add server-side PDF rendering in a
    future phase.
    """
    r = await db.advance_receipts.find_one(
        {"receipt_id": receipt_id, "clinic_id": user["clinic_id"]},
        {"_id": 0},
    )
    if not r:
        raise HTTPException(status_code=404, detail="Advance receipt not found")
    clinic = await db.clinics.find_one(
        {"clinic_id": user["clinic_id"]},
        {"_id": 0, "name": 1, "city": 1, "state": 1, "address_line1": 1},
    ) or {}
    return HTMLResponse(content=_render_receipt_html(r, clinic))


# ═══════════════════════════════════════════════════════════════════════
# NAV-011 · Phase 2B.2 · ADVANCE ALLOCATION WRITER
# ═══════════════════════════════════════════════════════════════════════
# Approved financial architecture:
#     Advance Receipt
#           ↓  (CAS decrement, single-doc atomic)
#     Advance Allocation Ledger  (db.advance_allocations)
#           ↓  (record_payment_atomic — proven NAV-009 pipeline)
#     db.payments (method="advance", advance_receipt_id, allocation_id)
#           ↓
#     Existing invoice / revenue / GST / referral attribution stack
#
# Guarantees (see /app/memory/ADVANCE_ALLOCATION_PHASE1_AUDIT.md §7):
#   I1  available_balance = received_amount − allocated_total
#   I3  Every active allocation ⇔ exactly one payments row with
#       method="advance", matching allocation_id.
#   I5  SUM(advance-sourced payments on invoice) ≤ paid_total.
#   I6  available_balance ≥ 0 (enforced by CAS $gte guard).
#   I10 Idempotent replay = byte-identical response + zero extra money.
#
# NOT implemented in this phase (Phase 2B.3+):
#   * allocation-void endpoint
#   * refund of an unallocated advance
#   * historical backfill of legacy receipts
#   * UI

def _invoice_snapshot(inv: dict) -> dict:
    """Minimal invoice snapshot returned in the allocation response.
    Deliberately projects only the fields a client needs to render the
    "after allocation" state — full invoice reads go through the
    existing billing endpoint.
    """
    if not inv:
        return {}
    return {
        "invoice_id": inv.get("invoice_id"),
        "invoice_no": inv.get("invoice_no"),
        "status": inv.get("status"),
        "paid_total": round(float(inv.get("paid_total") or 0), 2),
        "due_total": round(float(inv.get("due_total") or 0), 2),
        "refunded_total": round(float(inv.get("refunded_total") or 0), 2),
        "grand_total": round(float(inv.get("grand_total") or 0), 2),
        "rounded_total": round(float(inv.get("rounded_total") or 0), 2)
                         if inv.get("rounded_total") is not None else None,
    }


def _advance_snapshot(receipt: dict) -> dict:
    """Minimal advance-receipt snapshot returned in the allocation
    response — carries the freshly-updated balance ledger."""
    if not receipt:
        return {}
    return {
        "receipt_id": receipt.get("receipt_id"),
        "receipt_no": receipt.get("receipt_no"),
        "status": receipt.get("status"),
        "received_amount": round(float(receipt.get("received_amount") or 0), 2),
        "available_balance": round(float(receipt.get("available_balance") or 0), 2),
        "allocated_total": round(float(receipt.get("allocated_total") or 0), 2),
    }


@router.post("/{receipt_id}/allocations")
async def allocate_advance(
    receipt_id: str,
    payload: AdvanceAllocationCreate,
    request: Request,
    user=Depends(require_roles(*ALLOCATE_ROLES)),
    db=Depends(get_db),
):
    """Allocate an Advance Receipt against an existing invoice.

    Sequence (see file-level comment for the full state diagram):
      1. Mandatory Idempotency-Key gate.
      2. Enter idempotency context (scope=`advance_allocation`).
      3. Pre-flight tenant + ownership + state checks (fail fast).
      4. Advance CAS: `$gte:amt` guard, `$inc: -amt / +amt` on the
         receipt's balance ledger (single-doc atomic; loser sees a
         409 with the current balance for a helpful message).
      5. Insert `advance_allocations` ledger row (status=active,
         payment_id=None; stamped with `idempotency_correlation_id`
         so crash-recovery can rebuild the response).
      6. `record_payment_atomic(method="advance", extra_fields=…)` —
         same proven pipeline as every other invoice payment; carries
         `advance_receipt_id` + `allocation_id` back-links on both the
         top-level and embedded payment rows.
      7. Update allocation with `payment_id` back-link (single-doc
         atomic).
      8. Append `allocated` audit event.
      9. `idem.complete(operation_id=allocation_id)`.

    Rollbacks (compensating):
      * If step 6 fails → mark allocation `status='voided'` with
        `void_reason='Payment write failed: <detail>'`, restore
        advance balance via `$inc +amt / -amt`. NO physical delete —
        the failed row remains in the ledger for audit.
      * If step 7 fails → log critically but return success (the
        payment is committed, the allocation is retrievable via
        `idempotency_correlation_id`; a follow-up patch reconciles).

    Body: `AdvanceAllocationCreate` (invoice_id + amount + optional note).
    """
    # 1) Mandatory Idempotency-Key gate.
    raw_key = request.headers.get("Idempotency-Key") or request.headers.get("idempotency-key")
    if raw_key is None or raw_key.strip() == "":
        raise HTTPException(
            status_code=400,
            detail="Idempotency-Key header is required for advance-allocation writes.",
        )
    extract_idempotency_key(request)  # raises 400 on malformed

    # 2) Idempotency context.
    idem = await IdempotencyContext.enter(
        request, db,
        scope="advance_allocation", clinic_id=user["clinic_id"],
        actor=user,
        payload={"receipt_id": receipt_id, **payload.model_dump()},
        route="/api/advance-receipts/{receipt_id}/allocations",
        operation_collection="advance_allocations",
    )
    if idem.replayed:
        body, status, headers = idem.replay_response()
        return JSONResponse(content=body, status_code=status, headers=headers)

    amount = float(payload.amount)

    try:
        # 3a) Advance receipt tenant-scoped fetch.
        receipt = await db.advance_receipts.find_one(
            {"receipt_id": receipt_id, "clinic_id": user["clinic_id"]},
            {"_id": 0},
        )
        if not receipt:
            raise HTTPException(status_code=404, detail="Advance receipt not found")
        if receipt.get("status") != "active":
            raise HTTPException(
                status_code=409,
                detail=f"Cannot allocate — advance receipt is {receipt.get('status')!r}",
            )
        if receipt.get("available_balance") is None:
            # Legacy Phase 2A row — awaiting the separately-authorized
            # backfill. Refuse to allocate rather than blindly assume
            # `available_balance = received_amount`.
            raise HTTPException(
                status_code=409,
                detail=(
                    "This advance receipt was created before Phase 2B.2 "
                    "and its balance ledger has not been initialised yet. "
                    "A controlled backfill is required before it can be "
                    "allocated."
                ),
            )

        # 3b) Invoice tenant-scoped fetch.
        invoice = await db.invoices.find_one(
            {"invoice_id": payload.invoice_id, "clinic_id": user["clinic_id"]},
            {"_id": 0},
        )
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")

        # 3c) Same-patient invariant.
        if invoice.get("patient_id") != receipt.get("patient_id"):
            raise HTTPException(
                status_code=400,
                detail="Advance receipt and invoice belong to different patients",
            )

        # 3d) Invoice status guard (NAV-012 F-15).
        inv_status = (invoice.get("status") or "").lower()
        if inv_status in {"cancelled", "refunded", "partially_refunded"}:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Cannot allocate to a {inv_status!r} invoice. "
                    "Create a fresh invoice instead."
                ),
            )

        # 3e) Fast pre-check on invoice due_total. The final overpayment
        # guard is enforced atomically inside `record_payment_atomic`;
        # this pre-check just gives a clearer error before the CAS
        # advance decrement.
        current_due = round(
            float(invoice.get("due_total") or 0),
            2,
        )
        if current_due <= MONEY_TOL:
            raise HTTPException(
                status_code=400,
                detail=f"Invoice has no outstanding balance (due=₹{current_due:.2f})",
            )
        if amount > current_due + MONEY_TOL:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Allocation amount ₹{amount:.2f} exceeds invoice "
                    f"outstanding ₹{current_due:.2f}"
                ),
            )

        # 3f) Fast pre-check on advance available_balance. The final
        # CAS $gte guard in step 4 is authoritative — this is just for
        # a clearer error on the common single-request path.
        avail = round(float(receipt.get("available_balance") or 0), 2)
        if amount > avail + MONEY_TOL:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Allocation amount ₹{amount:.2f} exceeds advance "
                    f"available balance ₹{avail:.2f}"
                ),
            )

        # 4) Advance CAS — the exclusive winner.
        after_receipt = await db.advance_receipts.find_one_and_update(
            {
                "receipt_id": receipt_id,
                "clinic_id": user["clinic_id"],
                "status": "active",
                "available_balance": {"$gte": amount - MONEY_TOL},
            },
            {"$inc": {
                "available_balance": -amount,
                "allocated_total": amount,
            }},
            projection={"_id": 0},
            return_document=ReturnDocument.AFTER,
        )
        if not after_receipt:
            # Diagnose why: fetch fresh state.
            latest = await db.advance_receipts.find_one(
                {"receipt_id": receipt_id, "clinic_id": user["clinic_id"]},
                {"_id": 0, "status": 1, "available_balance": 1},
            )
            if not latest:
                raise HTTPException(status_code=404, detail="Advance receipt not found")
            if latest.get("status") != "active":
                raise HTTPException(
                    status_code=409,
                    detail=f"Advance receipt is {latest.get('status')!r}",
                )
            fresh_avail = round(float(latest.get("available_balance") or 0), 2)
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Insufficient advance balance — requested ₹{amount:.2f} "
                    f"but only ₹{fresh_avail:.2f} available"
                ),
            )

        # 5) Insert allocation ledger row. `payment_id` is filled in
        # after step 6 succeeds. `idempotency_correlation_id` allows
        # crash-recovery to rebuild the response.
        allocation_no = await _next_advance_allocation_no(db, user["clinic_id"])
        allocation = AdvanceAllocation(
            allocation_no=allocation_no,
            clinic_id=user["clinic_id"],
            branch_id=user.get("branch_id") or receipt.get("branch_id"),
            advance_receipt_id=receipt_id,
            advance_receipt_no=receipt.get("receipt_no"),
            invoice_id=payload.invoice_id,
            invoice_no=invoice.get("invoice_no"),
            patient_id=receipt.get("patient_id"),
            amount=amount,
            correlation_id=idem.correlation_id if idem.enabled else None,
            idempotency_correlation_id=idem.correlation_id if idem.enabled else None,
            note=payload.note,
            created_by_user_id=user.get("user_id"),
            created_by_name=user.get("name"),
        )
        allocation_id = allocation.allocation_id
        try:
            await db.advance_allocations.insert_one(allocation.model_dump())
        except Exception:
            # 5a) Rollback: restore advance balance.
            await db.advance_receipts.find_one_and_update(
                {"receipt_id": receipt_id, "clinic_id": user["clinic_id"]},
                {"$inc": {
                    "available_balance": amount,
                    "allocated_total": -amount,
                }},
            )
            _log.exception(
                "NAV-011 · allocation ledger insert failed (rolled back advance): "
                "receipt_id=%s amount=%s", receipt_id, amount,
            )
            raise HTTPException(
                status_code=500,
                detail="Failed to persist allocation ledger row — advance restored.",
            )

        # 6) Payment via the proven NAV-009 atomic pipeline.
        try:
            updated_invoice, payment_doc = await record_payment_atomic(
                db,
                clinic_id=user["clinic_id"],
                invoice_id=payload.invoice_id,
                amount=amount,
                method="advance",
                received_by_user_id=user.get("user_id"),
                reference=f"Allocation from {receipt.get('receipt_no', receipt_id)}",
                idempotency_correlation_id=idem.correlation_id if idem.enabled else None,
                extra_fields={
                    "advance_receipt_id": receipt_id,
                    "allocation_id": allocation_id,
                },
            )
        except HTTPException as pay_exc:
            # 6a) Payment write failed. Mark allocation as system-voided
            # (NO physical delete, per approved architecture), and
            # restore advance balance.
            now_iso = datetime.now(timezone.utc).isoformat()
            void_reason = (
                f"Payment write failed during allocation: "
                f"{pay_exc.status_code} {pay_exc.detail}"
            )
            await db.advance_allocations.update_one(
                {
                    "allocation_id": allocation_id,
                    "clinic_id": user["clinic_id"],
                    "status": "active",
                },
                {"$set": {
                    "status": "voided",
                    "voided_at": now_iso,
                    "void_reason": void_reason,
                    "voided_by_user_id": user.get("user_id"),
                    "voided_by_name": user.get("name"),
                }},
            )
            await db.advance_receipts.find_one_and_update(
                {"receipt_id": receipt_id, "clinic_id": user["clinic_id"]},
                {"$inc": {
                    "available_balance": amount,
                    "allocated_total": -amount,
                }},
            )
            _log.warning(
                "NAV-011 · allocation payment failed; system-voided "
                "allocation_id=%s reason=%s",
                allocation_id, void_reason,
            )
            raise

        # 7) Backfill payment_id on the allocation row.
        payment_id = payment_doc.get("payment_id")
        try:
            await db.advance_allocations.update_one(
                {"allocation_id": allocation_id, "clinic_id": user["clinic_id"]},
                {"$set": {"payment_id": payment_id}},
            )
        except Exception:
            # Non-fatal — the payment IS the source of truth for money;
            # the allocation row is still retrievable via
            # idempotency_correlation_id + advance_receipt_id. Log and
            # continue (rare Mongo failure between two ops).
            _log.exception(
                "NAV-011 · post-payment allocation update failed "
                "(payment already committed): allocation_id=%s payment_id=%s",
                allocation_id, payment_id,
            )

        # 8) Audit event.
        await _emit_audit(
            db, clinic_id=user["clinic_id"],
            receipt_id=receipt_id, receipt_no=receipt.get("receipt_no"),
            kind="allocated", actor=user,
            payload={
                "allocation_id": allocation_id,
                "allocation_no": allocation_no,
                "invoice_id": payload.invoice_id,
                "invoice_no": invoice.get("invoice_no"),
                "amount": amount,
                "remaining_balance": round(
                    float(after_receipt.get("available_balance") or 0), 2,
                ),
            },
        )

        # 9) Build the response body + close idempotency.
        response_body = {
            "allocation_id": allocation_id,
            "allocation_no": allocation_no,
            "advance_receipt_id": receipt_id,
            "advance_receipt_no": receipt.get("receipt_no"),
            "invoice_id": payload.invoice_id,
            "invoice_no": invoice.get("invoice_no"),
            "patient_id": receipt.get("patient_id"),
            "amount": amount,
            "status": "active",
            "payment_id": payment_id,
            "correlation_id": idem.correlation_id if idem.enabled else None,
            "created_at": allocation.created_at,
            "created_by_user_id": user.get("user_id"),
            "created_by_name": user.get("name"),
            "advance_receipt": _advance_snapshot(after_receipt),
            "invoice": _invoice_snapshot(_deserialize(updated_invoice)),
        }
        if idem.enabled:
            await idem.complete(
                http_status=200,
                response_body=response_body,
                operation_id=allocation_id,
            )
        return response_body

    except HTTPException as exc:
        if idem.enabled:
            await idem.fail(
                http_status=exc.status_code,
                response_body={"detail": exc.detail},
                detail=str(exc.detail),
            )
        raise

