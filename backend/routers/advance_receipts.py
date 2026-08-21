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
    AdvanceAuditEvent,
    AdvanceReceipt,
    AdvanceReceiptCreate,
    AdvanceVoidIn,
)
from utils.idempotency import IdempotencyContext, extract_idempotency_key

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/advance-receipts", tags=["advance-receipts"])

CREATE_ROLES = ("front_desk", "accounts", "clinic_owner")
READ_ROLES = ("front_desk", "accounts", "clinic_owner", "audiologist")
VOID_ROLES = ("accounts", "clinic_owner")


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
        receipt = AdvanceReceipt(
            receipt_no=receipt_no,
            clinic_id=user["clinic_id"],
            branch_id=pat.get("branch_id") or user.get("branch_id"),
            patient_id=pat["patient_id"],
            patient_name=pat.get("name"),
            patient_mobile=pat.get("mobile"),
            patient_mrd=pat.get("mrd"),
            received_amount=payload.received_amount,
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
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    res = await db.advance_receipts.find_one_and_update(
        {"receipt_id": receipt_id, "clinic_id": user["clinic_id"],
         "status": "active"},
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
            {"_id": 0, "status": 1},
        )
        if not existing:
            raise HTTPException(status_code=404, detail="Advance receipt not found")
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot void — receipt is in state {existing['status']!r} "
                f"(only 'active' can be voided)"
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
