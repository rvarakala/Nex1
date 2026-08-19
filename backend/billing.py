"""UC-04 Billing & Report Handover
================================
GST invoice engine with:
- Healthcare-exempt + taxable line mix (HSN/SAC aware)
- CGST/SGST split for intra-state, IGST for inter-state (based on clinic.state vs patient.state)
- Split payments (cash/upi/card/bank_transfer/insurance)
- Running counter per clinic-year (INV/2026/000001 style)
- Report delivery logging (print / whatsapp / email / in_person)
"""
from fastapi import APIRouter, Depends, HTTPException
from typing import List, Literal, Optional
from datetime import datetime, timezone
import logging
import math
import re
import uuid

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from utils.ist import IST  # noqa: F401

from database import get_db

from models import (
    Service, ServiceCreate,
    Invoice, InvoiceCreate, InvoiceLine, InvoiceLineCreate,
    Payment, PaymentCreate, RefundCreate,
    ReportDelivery,
    INVOICE_STATUSES,
)
from auth import get_current_user, require_roles

billing_router = APIRouter(prefix="/api")

# NAV-009 · Standardised monetary tolerance for float rounding & comparisons.
# Used by every payment/refund guard so the same 1-paisa window applies at
# the API layer and the atomic MongoDB conditional-update layer.
MONEY_TOL = 0.01

# NAV-009 · Roles allowed to CAPTURE PATIENT PAYMENTS on the canonical
# billing endpoint. Mirrors the refund gate exactly so that the two flows
# can never diverge silently. `super_admin` / `founder` bypass via the
# `require_roles` helper (see auth.require_roles).
_PAYMENT_ROLES = ("front_desk", "accounts", "clinic_owner")


# --------------- helpers ---------------

def _serialize(obj):
    """Recursively convert datetime → ISO string for Mongo storage."""
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialize(x) for x in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


def _deserialize(obj):
    """Recursively convert ISO strings back to datetime where possible.

    **UTC-awareness (2026-08-13 bug fix):** naive datetimes coming from
    BSON storage were passing through unchanged and being serialised back
    to the client without a `+00:00` / `Z` suffix. JS's `new Date(...)`
    parsed those naive strings as browser-local time, so IST users saw
    invoice timestamps 5:30 hrs behind reality. Fix: stamp naive datetime
    objects AND naive ISO strings with `tzinfo=timezone.utc` so FastAPI's
    encoder emits a `Z` suffix on the response.
    """
    if isinstance(obj, dict):
        return {k: _deserialize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_deserialize(x) for x in obj]
    if isinstance(obj, datetime):
        # BSON stores datetimes as naive `datetime` objects (this branch
        # is the whole reason IST users saw 06:36 instead of 12:06).
        if obj.tzinfo is None:
            return obj.replace(tzinfo=timezone.utc)
        return obj
    if isinstance(obj, str) and len(obj) >= 19 and obj[4] == '-' and obj[10] in ('T', ' '):
        try:
            parsed = datetime.fromisoformat(obj.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except Exception:
            return obj
    return obj


async def _next_invoice_no(db, clinic_id: str) -> str:
    """Generates clinic-scoped annual invoice number like 'INV/2026/000123'."""
    year = datetime.utcnow().year
    key = f"invoice:{clinic_id}:{year}"
    res = await db.counters.find_one_and_update(
        {"_id": key},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=True,
    )
    seq = res["seq"] if res else 1
    return f"INV/{year}/{str(seq).zfill(6)}"


# ──────────────────────────────────────────────────────────────────────
# NAV-008 · Duplicate-key retry safeguard
# ──────────────────────────────────────────────────────────────────────
# The compound unique index `clinic_id_1_invoice_no_1_unique` (installed
# by server.py startup, gated by the absence of existing duplicates)
# enforces that no two invoices in the same clinic can share the same
# invoice_no. Under the atomic counter this SHOULD be impossible — but
# a defence-in-depth wrapper handles the pathological edge case where a
# raw insert (from another process, a manual DB write, or a bug in a
# future writer) has occupied the number the counter is about to hand
# out. In that case we transparently pull the NEXT counter value and
# retry, up to 3 attempts total.
#
# The wrapper ONLY retries when the DuplicateKeyError originates from
# the invoice-uniqueness index; unrelated duplicate errors (e.g. a
# concurrent identical invoice_id) are re-raised unchanged.
_INVOICE_UNIQUE_INDEX_NAME = "clinic_id_1_invoice_no_1_unique"


async def _insert_invoice_with_retry(db, inv_doc: dict, clinic_id: str, max_attempts: int = 3) -> dict:
    """Insert an invoice document with automatic retry on (clinic_id,
    invoice_no) uniqueness violation.

    Args:
        db          : Motor DB handle.
        inv_doc     : the fully-serialised invoice document ready for
                      insert. MUST already contain `invoice_no` (set by
                      the caller via `_next_invoice_no`).
        clinic_id   : caller's clinic_id for counter renewal on retry.
        max_attempts: total insert attempts (default 3).

    Returns the (possibly updated) inv_doc on success. Raises
    HTTPException 500 on repeated conflict, letting the caller surface
    a controlled application-level error instead of an unhandled 500.

    Behaviour by MongoDB error:
    - E11000 on `clinic_id_1_invoice_no_1_unique` → renew invoice_no
      from counter, retry.
    - E11000 on any other index (e.g. invoice_id) → NOT retried;
      re-raised so the caller can decide what to do.
    - Non-duplicate errors → re-raised unchanged.
    """
    from pymongo.errors import DuplicateKeyError
    for attempt in range(1, max_attempts + 1):
        try:
            await db.invoices.insert_one(inv_doc)
            return inv_doc
        except DuplicateKeyError as e:
            # Only retry when the conflict is on the invoice-uniqueness
            # index we installed for NAV-008. Any other collision (e.g.
            # invoice_id which is separately unique) is a genuinely
            # different bug — surface it as-is.
            err_text = str(e)
            if _INVOICE_UNIQUE_INDEX_NAME not in err_text and "invoice_no" not in err_text:
                raise
            if attempt == max_attempts:
                logging.getLogger(__name__).error(
                    f"NAV-008 · Invoice-number conflict persisted after {max_attempts} attempts "
                    f"for clinic {clinic_id!r}; giving up. Last attempted invoice_no="
                    f"{inv_doc.get('invoice_no')!r}. Raw error: {err_text}"
                )
                raise HTTPException(
                    status_code=500,
                    detail="Invoice-number conflict; please retry the request.",
                )
            # Renew the number and try again.
            inv_doc["invoice_no"] = await _next_invoice_no(db, clinic_id)
            # Also renew embedded MongoDB _id if the driver stamped one
            # onto the failed insert (Motor mutates the doc on insert).
            inv_doc.pop("_id", None)
            logging.getLogger(__name__).info(
                f"NAV-008 · Invoice-no conflict on attempt {attempt} for clinic {clinic_id!r}; "
                f"retrying with fresh number {inv_doc['invoice_no']!r}."
            )
    # Unreachable — either return or raise inside the loop.
    raise HTTPException(status_code=500, detail="Invoice insert failed unexpectedly.")


# ──────────────────────────────────────────────────────────────────────
# NAV-009 · Atomic payment / refund writers (dual-write consistent + safe
# under concurrency).
# ──────────────────────────────────────────────────────────────────────
# Design summary
# --------------
# Prior to NAV-009 every payment/refund writer used a read-modify-write
# pattern on the invoice document — read invoice, build a new payments
# array in Python, `$set` it back. Two problems:
#   1. Full-array `$set` overwrites concurrent writes → lost-update race.
#   2. HA workflows (ha_quick_sale, ha_custom_ha_orders, ha_ear_moulds)
#      only pushed to `invoices.payments` and skipped `db.payments`,
#      producing top-level revenue drift (~20 orphan rows on Preview).
#
# The two helpers below unify all payment/refund capture flows onto:
#   * A single insert into the top-level `db.payments` collection.
#   * A single **atomic** aggregation-pipeline update on the invoice
#     with an `$expr` guard that enforces the overpayment / refundable
#     ceiling AT THE DATABASE LAYER, not in Python. This closes the
#     PAY-003 (overpayment), PAY-004 (payment race) and REF-001
#     (refund race) findings in one shot.
#
# On MongoDB standalone (Preview), multi-document transactions are not
# available, so we use an insert-first + compensating-delete pattern:
#   1. Insert `db.payments` row (unique payment_id).
#   2. Attempt atomic conditional `find_one_and_update` on the invoice.
#   3. If matched → return; the two stores are consistent.
#   4. If not matched → `db.payments.delete_one(payment_id)`, then
#      diagnose (cancelled / missing / overpayment / no-refundable) and
#      raise the correct 4xx.
#
# The <1 ms window between (1) and (2) is acceptable — a listing that
# reads `db.payments` in that window sees a real payment that will be
# committed to the invoice within the next atomic op. The reverse order
# would produce the PAY-001 drift we are fixing.

def _new_payment_id() -> str:
    return f"PAY-{uuid.uuid4().hex[:8].upper()}"


def _due_expr_field():
    """Aggregation-pipeline expression: prefer `rounded_total`, fall
    back to `grand_total`, then 0. Used both for overpayment guards
    and for computing due_total after the update."""
    return {"$ifNull": ["$rounded_total", {"$ifNull": ["$grand_total", 0]}]}


def _status_expr():
    """Aggregation-pipeline expression that derives the refund-aware
    invoice status from the post-update `paid_total`, `refunded_total`,
    `due_total`. Semantically identical to `_sum_invoice`'s Python
    ladder (kept in sync — do not diverge)."""
    return {"$switch": {
        "branches": [
            {"case": {"$eq": ["$status", "cancelled"]}, "then": "cancelled"},
            # Full refund — every rupee originally collected has been
            # refunded, so `paid_total` has decayed to ~0 while
            # `refunded_total` carries the historical positive display.
            {"case": {"$and": [
                {"$gt": [{"$ifNull": ["$refunded_total", 0]}, MONEY_TOL]},
                {"$lte": [{"$ifNull": ["$paid_total", 0]}, MONEY_TOL]},
            ]}, "then": "refunded"},
            # Partial refund — some paid, some refunded, both non-zero.
            {"case": {"$gt": [{"$ifNull": ["$refunded_total", 0]}, MONEY_TOL]},
             "then": "partially_refunded"},
            # Classic ladder — fully paid.
            {"case": {"$lte": ["$due_total", MONEY_TOL]}, "then": "paid"},
            {"case": {"$gt": [{"$ifNull": ["$paid_total", 0]}, 0]}, "then": "partial"},
        ],
        "default": "draft"
    }}


async def record_payment_atomic(
    db,
    *,
    clinic_id: str,
    invoice_id: str,
    amount: float,
    method: str,
    received_by_user_id: Optional[str] = None,
    paid_at: Optional[datetime] = None,
    reference: Optional[str] = None,
    notes: Optional[str] = None,
    enforce_overpay: bool = True,
) -> tuple[dict, dict]:
    """Atomic payment writer used by canonical billing + all HA payment
    capture flows. Returns (updated_invoice_dict, payment_dict).

    Raises HTTPException on: non-finite / non-positive amount, missing
    invoice, cancelled invoice, or overpayment (payload > current
    due_total + MONEY_TOL).

    Preserves the invariant `db.payments.payment_id == embedded
    invoice.payments[].payment_id` on success.
    """
    amt = float(amount)
    if not math.isfinite(amt):
        raise HTTPException(status_code=400, detail="Payment amount must be a finite number")
    amt = round(amt, 2)
    if amt <= 0:
        raise HTTPException(status_code=400, detail="Payment amount must be > 0")

    now = paid_at if isinstance(paid_at, datetime) else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    payment_id = _new_payment_id()
    pay_doc: dict = {
        "payment_id": payment_id,
        "clinic_id": clinic_id,
        "invoice_id": invoice_id,
        "kind": "payment",
        "method": method,
        "amount": amt,
        "reference": reference,
        "paid_at": now.isoformat(),
        "received_by_user_id": received_by_user_id,
        "notes": notes,
    }

    # 1) Insert top-level row first. UUID collision is astronomically
    # unlikely; on the off-chance retry once with a fresh id.
    try:
        await db.payments.insert_one(dict(pay_doc))
    except DuplicateKeyError:
        pay_doc["payment_id"] = _new_payment_id()
        payment_id = pay_doc["payment_id"]
        await db.payments.insert_one(dict(pay_doc))

    # 2) Atomic conditional invoice update.
    match: dict = {
        "invoice_id": invoice_id,
        "clinic_id": clinic_id,
        # Payments are rejected only on cancelled invoices — matches the
        # pre-NAV-009 behaviour. `refunded` / `partially_refunded`
        # semantics are the P2 finding NAV009-PAY-006 and were NOT
        # approved for Phase 2A; keeping them accepted here so this
        # sprint does not silently change user-facing behaviour.
        "status": {"$ne": "cancelled"},
    }
    if enforce_overpay:
        match["$expr"] = {"$gte": [
            {"$subtract": [_due_expr_field(), {"$ifNull": ["$paid_total", 0]}]},
            amt - MONEY_TOL,
        ]}

    pipeline = [
        {"$set": {
            "payments": {"$concatArrays": [
                {"$ifNull": ["$payments", []]},
                [pay_doc],
            ]},
            "paid_total": {"$round": [
                {"$add": [{"$ifNull": ["$paid_total", 0]}, amt]},
                2,
            ]},
        }},
        {"$set": {
            "due_total": {"$round": [
                {"$subtract": [_due_expr_field(), "$paid_total"]},
                2,
            ]},
        }},
        {"$set": {"status": _status_expr()}},
    ]

    updated = await db.invoices.find_one_and_update(
        match, pipeline,
        projection={"_id": 0},
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        # Compensating rollback so the top-level row does not orphan.
        await db.payments.delete_one({"payment_id": payment_id})
        # Diagnose the specific failure reason for a helpful 4xx.
        inv = await db.invoices.find_one(
            {"invoice_id": invoice_id, "clinic_id": clinic_id},
            {"_id": 0, "status": 1, "paid_total": 1,
             "rounded_total": 1, "grand_total": 1},
        )
        if not inv:
            raise HTTPException(status_code=404, detail="Invoice not found")
        st = (inv.get("status") or "").lower()
        if st == "cancelled":
            raise HTTPException(status_code=400, detail="Cannot add payment to a cancelled invoice")
        # Overpayment path.
        due = round(
            float(inv.get("rounded_total") or inv.get("grand_total") or 0)
            - float(inv.get("paid_total") or 0),
            2,
        )
        raise HTTPException(
            status_code=400,
            detail=(
                f"Payment amount ₹{amt:.2f} exceeds due balance ₹{max(0.0, due):.2f}"
            ),
        )

    return updated, pay_doc


async def record_refund_atomic(
    db,
    *,
    clinic_id: str,
    invoice_id: str,
    amount: float,
    method: str,
    reason: str,
    received_by_user_id: Optional[str] = None,
    reference: Optional[str] = None,
    notes: Optional[str] = None,
) -> tuple[dict, dict]:
    """Atomic refund writer. `amount` is the POSITIVE refund value;
    stored on the row as a NEGATIVE amount so `_sum_invoice`'s
    `sum(payments.amount)` naturally reduces `paid_total`.

    Raises HTTPException on: non-finite / non-positive amount, missing
    invoice, cancelled / draft invoice, no positive balance, or refund
    exceeding the refundable ceiling.

    Uses an aggregation-pipeline `find_one_and_update` guarded by
    `$expr: paid_total >= amount - MONEY_TOL` so two concurrent refund
    attempts on the same ceiling cannot both succeed — closes REF-001.
    """
    amt_pos = float(amount)
    if not math.isfinite(amt_pos):
        raise HTTPException(status_code=400, detail="Refund amount must be a finite number")
    amt_pos = round(amt_pos, 2)
    if amt_pos <= 0:
        raise HTTPException(status_code=400, detail="Refund amount must be > 0")

    now = datetime.now(timezone.utc)
    payment_id = _new_payment_id()
    refund_doc: dict = {
        "payment_id": payment_id,
        "clinic_id": clinic_id,
        "invoice_id": invoice_id,
        "kind": "refund",
        "method": method,
        # Stored NEGATIVE by convention (see Payment.kind docstring).
        "amount": -amt_pos,
        "reference": reference,
        "reason": reason,
        "paid_at": now.isoformat(),
        "received_by_user_id": received_by_user_id,
        "notes": notes,
    }
    try:
        await db.payments.insert_one(dict(refund_doc))
    except DuplicateKeyError:
        refund_doc["payment_id"] = _new_payment_id()
        payment_id = refund_doc["payment_id"]
        await db.payments.insert_one(dict(refund_doc))

    match: dict = {
        "invoice_id": invoice_id,
        "clinic_id": clinic_id,
        "status": {"$nin": ["cancelled", "draft"]},
        # Atomic refundable-ceiling guard — see REF-001 in the audit.
        "$expr": {"$gte": [{"$ifNull": ["$paid_total", 0]}, amt_pos - MONEY_TOL]},
    }
    pipeline = [
        {"$set": {
            "payments": {"$concatArrays": [
                {"$ifNull": ["$payments", []]},
                [refund_doc],
            ]},
            "paid_total": {"$round": [
                {"$subtract": [{"$ifNull": ["$paid_total", 0]}, amt_pos]},
                2,
            ]},
            "refunded_total": {"$round": [
                {"$add": [{"$ifNull": ["$refunded_total", 0]}, amt_pos]},
                2,
            ]},
        }},
        {"$set": {
            "due_total": {"$round": [
                {"$subtract": [_due_expr_field(), "$paid_total"]},
                2,
            ]},
        }},
        {"$set": {"status": _status_expr()}},
    ]
    updated = await db.invoices.find_one_and_update(
        match, pipeline,
        projection={"_id": 0},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        await db.payments.delete_one({"payment_id": payment_id})
        inv = await db.invoices.find_one(
            {"invoice_id": invoice_id, "clinic_id": clinic_id},
            {"_id": 0, "status": 1, "paid_total": 1},
        )
        if not inv:
            raise HTTPException(status_code=404, detail="Invoice not found")
        st = (inv.get("status") or "").lower()
        if st == "cancelled":
            raise HTTPException(status_code=400, detail="Cannot refund a cancelled invoice")
        if st == "draft":
            raise HTTPException(
                status_code=400,
                detail="Nothing to refund — this invoice has no payments yet",
            )
        paid_now = round(float(inv.get("paid_total") or 0), 2)
        if paid_now <= MONEY_TOL:
            raise HTTPException(status_code=400, detail="This invoice has no positive balance to refund")
        raise HTTPException(
            status_code=400,
            detail=(
                f"Refund amount ₹{amt_pos:.2f} exceeds refundable balance ₹{paid_now:.2f}"
            ),
        )
    return updated, refund_doc


async def mirror_embedded_payments_to_top_level(
    db, invoice_doc: dict, *, actor_context: str = "",
) -> int:
    """NAV-009 · Best-effort mirror of an invoice's *just-inserted*
    embedded `payments[]` array into the top-level `db.payments`
    collection. Used by HA workflows that build the invoice + its
    initial payment(s) in a single doc (Quick Sale create, Custom HA
    Order create, Ear-Mould Order create).

    Idempotent: skips any embedded row whose `payment_id` already
    exists top-level. Never raises — a mirror failure is logged but
    does not undo the invoice.
    """
    rows = invoice_doc.get("payments") or []
    if not rows:
        return 0
    mirrored = 0
    for row in rows:
        pid = row.get("payment_id")
        if not pid:
            continue
        # Idempotency guard — if this payment_id is already at top
        # level (e.g. a caller already inserted via record_payment_
        # atomic), skip silently.
        existing = await db.payments.find_one({"payment_id": pid}, {"_id": 0, "payment_id": 1})
        if existing:
            continue
        mirror_row = dict(row)
        # Enforce top-level shape parity — the embedded row is
        # allowed to omit clinic_id / invoice_id (legacy tolerance),
        # but the top-level row MUST carry both for tenant queries.
        mirror_row.setdefault("clinic_id", invoice_doc.get("clinic_id"))
        mirror_row.setdefault("invoice_id", invoice_doc.get("invoice_id"))
        mirror_row.setdefault("kind", "payment")
        # Timezone hygiene — top-level should always be ISO string.
        pa = mirror_row.get("paid_at")
        if isinstance(pa, datetime):
            mirror_row["paid_at"] = pa.isoformat() if pa.tzinfo else pa.replace(tzinfo=timezone.utc).isoformat()
        try:
            await db.payments.insert_one(mirror_row)
            mirrored += 1
        except DuplicateKeyError:
            # Rare — another writer got there first. Not fatal.
            continue
        except Exception as exc:  # noqa: BLE001 — defensive
            logging.getLogger(__name__).warning(
                f"NAV-009 · mirror_embedded_payments failed for "
                f"payment_id={pid!r} invoice_id={invoice_doc.get('invoice_id')!r} "
                f"actor_context={actor_context!r}: {exc}",
            )
    return mirrored




def _compute_line(line_in: InvoiceLineCreate, service: Optional[dict]) -> InvoiceLine:
    """Resolve a line-create request against optional service and compute taxes."""
    name = line_in.description or (service.get("name") if service else None)
    if not name:
        raise HTTPException(status_code=400, detail="Line must have description or service_id")
    unit_price = line_in.unit_price if line_in.unit_price is not None else float(service.get("price", 0.0) if service else 0.0)
    is_taxable = line_in.is_taxable if line_in.is_taxable is not None else bool(service.get("is_taxable", False) if service else False)
    gst_rate = line_in.gst_rate if line_in.gst_rate is not None else float(service.get("gst_rate", 0.0) if service else 0.0)
    hsn = line_in.hsn_sac or (service.get("hsn_sac") if service else None)
    # gst_inclusive: explicit wire value wins. Falls back to service-level
    # default. Falls back to True (legacy) only if neither is set — but
    # callers entering "flat-fee + GST" workflows should pass
    # `gst_inclusive=false`. Fixes Bug #2 from iter33 QA: flat-fee invoices
    # were silently treating unit_price as inclusive, eating into revenue.
    if line_in.gst_inclusive is not None:
        gst_inclusive = bool(line_in.gst_inclusive)
    elif service is not None and "gst_inclusive" in service:
        gst_inclusive = bool(service["gst_inclusive"])
    else:
        gst_inclusive = True

    qty = float(line_in.quantity or 1.0)

    gross = qty * unit_price

    # Resolve discount amount. Support two entry modes:
    #   - discount_type="flat"    → discount_value is ₹ (fallback: legacy discount_amount)
    #   - discount_type="percent" → discount_value is % of gross; we compute the ₹ equivalent.
    discount_type = getattr(line_in, "discount_type", "flat") or "flat"
    discount_value = float(getattr(line_in, "discount_value", 0.0) or 0.0)
    if discount_type == "percent":
        pct = max(0.0, min(100.0, discount_value))
        disc = round(gross * pct / 100.0, 2)
    else:
        # Flat mode — prefer discount_value when provided, else fall back to legacy discount_amount.
        disc = float(discount_value if discount_value else (line_in.discount_amount or 0.0))
    disc = max(0.0, min(gross, round(disc, 2)))
    # If price is GST inclusive, back-calculate taxable value: tx = gross / (1 + rate/100)
    if is_taxable and gst_rate > 0 and gst_inclusive:
        # Apply discount to gross first, then strip GST
        net_gross = max(0.0, gross - disc)
        taxable = round(net_gross / (1 + gst_rate / 100.0), 2)
        tax_amount = round(net_gross - taxable, 2)
    elif is_taxable and gst_rate > 0:
        taxable = max(0.0, gross - disc)
        tax_amount = round(taxable * (gst_rate / 100.0), 2)
    else:
        taxable = max(0.0, gross - disc)
        tax_amount = 0.0

    # The CGST/SGST vs IGST split is decided at invoice level (intra vs inter-state).
    # Validate serial numbers length against quantity for hearing aids — each
    # unit ought to ship with one serial. We only WARN by trimming/padding to
    # avoid breaking legacy callers; the UI enforces it strictly.
    serial_numbers = [s.strip() for s in (line_in.serial_numbers or []) if s and s.strip()]

    return InvoiceLine(
        service_id=line_in.service_id,
        description=name,
        hsn_sac=hsn,
        quantity=qty,
        unit_price=unit_price,
        discount_amount=disc,
        discount_type=discount_type,
        discount_value=round(discount_value, 2),
        is_taxable=is_taxable,
        gst_rate=gst_rate,
        taxable_value=taxable,
        cgst_amount=0.0, sgst_amount=0.0, igst_amount=0.0,
        line_total=round(taxable + tax_amount, 2),
        # Pass-through product detail fields.
        product_type=line_in.product_type,
        make=line_in.make,
        model=line_in.model,
        serial_numbers=serial_numbers,
        technology_tier=line_in.technology_tier,
        # Accessory stock plumbing — passed through so the paid-invoice
        # hook can find the matching `accessory_stock` row deterministically.
        accessory_product_id=getattr(line_in, "accessory_product_id", None),
        accessory_variant=getattr(line_in, "accessory_variant", None),
    )


def _apply_tax_split(lines: List[InvoiceLine], inter_state: bool):
    """Populate CGST/SGST or IGST per line based on intra vs inter-state."""
    for ln in lines:
        tax_total = round(ln.line_total - ln.taxable_value, 2)
        if not ln.is_taxable or tax_total <= 0:
            ln.cgst_amount = ln.sgst_amount = ln.igst_amount = 0.0
            continue
        if inter_state:
            ln.igst_amount = tax_total
            ln.cgst_amount = ln.sgst_amount = 0.0
        else:
            half = round(tax_total / 2.0, 2)
            ln.cgst_amount = half
            ln.sgst_amount = round(tax_total - half, 2)
            ln.igst_amount = 0.0


def _sum_invoice(inv: Invoice):
    """Compute invoice totals from lines + payments.

    Refunds are recorded as Payment rows with `kind="refund"` and a
    NEGATIVE amount, so `paid_total = sum(all amounts)` correctly reflects
    net money-in-hand. `refunded_total` is the positive display value
    (|sum of refund amounts|) used for the UI and the "refunded" pill.
    """
    inv.subtotal = round(sum(ln.taxable_value for ln in inv.lines), 2)
    inv.discount_total = round(sum(ln.discount_amount for ln in inv.lines), 2)
    inv.cgst_total = round(sum(ln.cgst_amount for ln in inv.lines), 2)
    inv.sgst_total = round(sum(ln.sgst_amount for ln in inv.lines), 2)
    inv.igst_total = round(sum(ln.igst_amount for ln in inv.lines), 2)
    inv.tax_total = round(inv.cgst_total + inv.sgst_total + inv.igst_total, 2)
    inv.grand_total = round(inv.subtotal + inv.tax_total, 2)
    inv.rounded_total = round(inv.grand_total)
    inv.round_off = round(inv.rounded_total - inv.grand_total, 2)
    inv.paid_total = round(sum(p.amount for p in inv.payments), 2)
    inv.refunded_total = round(
        sum(-p.amount for p in inv.payments if (p.kind or "payment") == "refund"),
        2,
    )
    inv.due_total = round(inv.rounded_total - inv.paid_total, 2)
    if inv.status != "cancelled":
        # Refund-aware status. A refund exists when refunded_total > 0.
        # * Full refund (refunded ≈ original paid) → "refunded"
        # * Partial refund (some money still in hand) → "partially_refunded"
        # * No refund → classic draft / partial / paid ladder.
        has_refund = inv.refunded_total > 0.01
        original_paid = round(inv.paid_total + inv.refunded_total, 2)
        if has_refund and original_paid > 0 and (inv.refunded_total >= original_paid - 0.01):
            inv.status = "refunded"
        elif has_refund:
            inv.status = "partially_refunded"
        elif inv.paid_total <= 0:
            inv.status = "draft"
        elif inv.due_total <= 0.01:
            inv.status = "paid"
        else:
            inv.status = "partial"


# --------------- SERVICE CATALOGUE ---------------

@billing_router.get("/billing/services", response_model=List[Service])
async def list_services(active_only: bool = True, search: Optional[str] = None,
                        user=Depends(get_current_user), db=Depends(get_db)):
    q = {"clinic_id": user["clinic_id"]}
    if active_only:
        q["active"] = True
    if search:
        rx = {"$regex": re.escape(search.strip()), "$options": "i"}
        q["$or"] = [{"name": rx}, {"code": rx}, {"category": rx}]
    rows = await db.services.find(q, {"_id": 0}).sort("name", 1).to_list(500)
    return [_deserialize(r) for r in rows]


@billing_router.post("/billing/services", response_model=Service)
async def create_service(payload: ServiceCreate,
                         user=Depends(get_current_user), db=Depends(get_db)):
    if user["role"] not in {"super_admin", "founder", "clinic_owner", "accounts"}:
        raise HTTPException(status_code=403, detail="Only owner / accounts / admin can manage services")
    obj = Service(clinic_id=user["clinic_id"], **payload.model_dump())
    await db.services.insert_one(_serialize(obj.model_dump()))
    return obj


@billing_router.put("/billing/services/{service_id}", response_model=Service)
async def update_service(service_id: str, payload: dict,
                         user=Depends(get_current_user), db=Depends(get_db)):
    if user["role"] not in {"super_admin", "founder", "clinic_owner", "accounts"}:
        raise HTTPException(status_code=403, detail="Only owner / accounts / admin can manage services")
    existing = await db.services.find_one({"service_id": service_id, "clinic_id": user["clinic_id"]})
    if not existing:
        raise HTTPException(status_code=404, detail="Service not found")
    allowed = {"name", "code", "category", "hsn_sac", "price", "gst_rate", "gst_inclusive", "is_taxable", "active"}
    patch = {k: v for k, v in payload.items() if k in allowed}
    await db.services.update_one({"service_id": service_id}, {"$set": patch})
    updated = await db.services.find_one({"service_id": service_id}, {"_id": 0})
    return _deserialize(updated)


@billing_router.delete("/billing/services/{service_id}")
async def deactivate_service(service_id: str,
                             user=Depends(get_current_user), db=Depends(get_db)):
    if user["role"] not in {"super_admin", "founder", "clinic_owner", "accounts"}:
        raise HTTPException(status_code=403, detail="Only owner / accounts / admin can manage services")
    res = await db.services.update_one(
        {"service_id": service_id, "clinic_id": user["clinic_id"]},
        {"$set": {"active": False}},
    )
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Service not found")
    return {"message": "Deactivated", "service_id": service_id}


# --------------- INVOICES ---------------

@billing_router.post("/billing/invoices", response_model=Invoice)
async def create_invoice(payload: InvoiceCreate,
                         user=Depends(get_current_user), db=Depends(get_db)):
    clinic_id = user["clinic_id"]

    # Validate + hydrate patient
    patient = await db.patients.find_one({"patient_id": payload.patient_id, "clinic_id": clinic_id}, {"_id": 0})
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    clinic = await db.clinics.find_one({"clinic_id": clinic_id}, {"_id": 0}) or {}

    # Resolve each line against service catalogue
    if not payload.lines:
        raise HTTPException(status_code=400, detail="Invoice must have at least one line")

    resolved_lines: List[InvoiceLine] = []
    for ln in payload.lines:
        svc = None
        if ln.service_id:
            svc = await db.services.find_one(
                {"service_id": ln.service_id, "clinic_id": clinic_id}, {"_id": 0}
            )
            if not svc:
                raise HTTPException(status_code=400, detail=f"Service {ln.service_id} not found")
        resolved_lines.append(_compute_line(ln, svc))

    # Determine intra vs inter-state (CGST+SGST vs IGST)
    clinic_state = (clinic.get("state") or "").strip().lower()
    pat_state = (patient.get("state") or "").strip().lower()
    inter_state = bool(clinic_state and pat_state and clinic_state != pat_state)
    _apply_tax_split(resolved_lines, inter_state)

    # Auto-link to the patient's most recent not-yet-handed-over session if the
    # caller didn't explicitly provide a session_id. This makes handover gating
    # work even when reception creates invoices via "+ New Invoice" (a flow
    # that doesn't carry session context).
    resolved_session_id = payload.session_id
    if not resolved_session_id:
        sess = await db.test_sessions.find_one(
            {"clinic_id": clinic_id, "patient_id": patient["patient_id"],
             "report_status": {"$in": ["draft", "test_completed", "printed"]}},
            {"_id": 0, "session_id": 1},
            sort=[("created_at", -1)],
        )
        if sess:
            resolved_session_id = sess["session_id"]

    invoice_no = await _next_invoice_no(db, clinic_id)
    inv = Invoice(
        clinic_id=clinic_id,
        invoice_no=invoice_no,
        patient_id=patient["patient_id"],
        patient_name=patient.get("name", ""),
        patient_mobile=patient.get("mobile") or patient.get("phone"),
        mrd=patient.get("mrd"),
        patient_address=_format_patient_address(patient),
        patient_gstin=payload.patient_gstin,
        appointment_id=payload.appointment_id,
        session_id=resolved_session_id,
        lines=resolved_lines,
        notes=payload.notes,
        linked_sale_no=payload.from_sale_no,
        created_by_user_id=user["user_id"],
    )

    # Optional initial payment
    if payload.initial_payment and payload.initial_payment.amount > 0:
        pay = Payment(
            clinic_id=clinic_id,
            invoice_id=inv.invoice_id,
            method=payload.initial_payment.method,
            amount=float(payload.initial_payment.amount),
            reference=payload.initial_payment.reference,
            notes=payload.initial_payment.notes,
            received_by_user_id=user["user_id"],
        )
        inv.payments.append(pay)
        await db.payments.insert_one(_serialize(pay.model_dump()))

    _sum_invoice(inv)

    inv_serialized = _serialize(inv.model_dump())
    inv_serialized = await _insert_invoice_with_retry(db, inv_serialized, clinic_id)
    # If the retry loop renewed invoice_no, keep the response in sync.
    if inv_serialized.get("invoice_no") != inv.invoice_no:
        inv.invoice_no = inv_serialized["invoice_no"]

    # If created from an HA sale, write the back-link into ha_sales so the
    # auto-flip on payment can find the sale by invoice_no.
    if payload.from_sale_no:
        await db.ha_sales.update_one(
            {"sale_no": payload.from_sale_no, "clinic_id": clinic_id},
            {"$set": {"invoice_no": inv.invoice_no, "status": "invoiced"}},
        )

    # If this brand-new invoice is already fully paid (e.g. cash-in-hand at
    # checkout via initial_payment), also auto-flip the linked HA sale.
    if inv.status == "paid" and payload.from_sale_no:
        try:
            from routers.ha_sales import mark_sale_paid_internal
            await mark_sale_paid_internal(
                db, clinic_id, payload.from_sale_no,
                actor_user_id=user["user_id"], invoice_no=inv.invoice_no,
            )
        except HTTPException as exc:
            # Don't fail the invoice create — just log; admin can manually
            # mark-paid later.
            logging.getLogger(__name__).warning(
                f"Auto-flip on invoice create skipped for sale "
                f"{payload.from_sale_no}: {exc.detail}",
            )

    # Auto-decrement accessory stock when the invoice is created already
    # fully paid (cash-in-hand at counter). Same guarantees as the
    # mid-invoice add_payment path — never raises.
    if inv.status == "paid":
        try:
            from utils.accessory_stock import auto_decrement_accessory_stock
            fresh_inv_doc = await db.invoices.find_one({"invoice_id": inv.invoice_id}, {"_id": 0})
            if fresh_inv_doc:
                await auto_decrement_accessory_stock(
                    db, fresh_inv_doc,
                    actor_user_id=user["user_id"],
                    branch_id=user.get("branch_id"),
                )
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                f"Accessory auto-decrement on invoice create failed for {inv.invoice_no}: {exc}",
            )

    return inv


def _format_patient_address(p: dict) -> Optional[str]:
    parts = [p.get("address"), p.get("city"), p.get("state"), p.get("pincode")]
    parts = [x for x in parts if x]
    return ", ".join(parts) if parts else None


@billing_router.get("/billing/invoices", response_model=None)
async def list_invoices(
    status: Optional[str] = None,
    patient_id: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 200,
    cursor: Optional[str] = None,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """List invoices. See get_patients() in routers/patients.py for the
    legacy-array vs paginated-envelope contract."""
    from utils.pagination import cursor_clause, next_cursor_for

    q: dict = {"clinic_id": user["clinic_id"]}
    if status:
        q["status"] = status
    if patient_id:
        q["patient_id"] = patient_id
    if from_date or to_date:
        rng: dict = {}
        if from_date:
            rng["$gte"] = f"{from_date}T00:00:00"
        if to_date:
            rng["$lte"] = f"{to_date}T23:59:59"
        q["invoice_date"] = rng
    if search:
        rx = {"$regex": re.escape(search.strip()), "$options": "i"}
        q["$or"] = [{"invoice_no": rx}, {"patient_name": rx}, {"mrd": rx}, {"patient_mobile": rx}]

    paginated = cursor is not None
    if paginated and cursor:
        clause = cursor_clause("invoice_date", "invoice_id", cursor)
        if clause:
            if "$or" in q:
                q = {"$and": [{"$or": q.pop("$or")}, clause, q]}
            else:
                q.update(clause)

    cap = max(1, min(int(limit or 50), 500))
    rows = await (
        db.invoices.find(q, {"_id": 0})
        .sort([("invoice_date", -1), ("invoice_id", -1)])
        .to_list(cap)
    )
    items = [_deserialize(r) for r in rows]
    if paginated:
        nxt = next_cursor_for(rows, "invoice_date", "invoice_id", cap)
        return {"items": items, "next_cursor": nxt, "has_more": nxt is not None}
    return items


@billing_router.get("/billing/invoices/export.csv")
async def export_invoices_csv(
    status: Optional[str] = None,
    patient_id: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    search: Optional[str] = None,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Stream the current Invoices view as CSV. Accepts the same filter
    params as `/billing/invoices`. Useful for GST filings, AR aging, and
    insurance reimbursement reports."""
    from utils.csv_export import stream_csv

    q: dict = {"clinic_id": user["clinic_id"]}
    if status:
        q["status"] = status
    if patient_id:
        q["patient_id"] = patient_id
    if from_date or to_date:
        rng: dict = {}
        if from_date:
            rng["$gte"] = f"{from_date}T00:00:00"
        if to_date:
            rng["$lte"] = f"{to_date}T23:59:59"
        q["invoice_date"] = rng
    if search:
        rx = {"$regex": re.escape(search.strip()), "$options": "i"}
        q["$or"] = [{"invoice_no": rx}, {"patient_name": rx}, {"mrd": rx}, {"patient_mobile": rx}]

    headers = [
        "Invoice No", "Invoice Date", "Status",
        "Patient Name", "MRD", "Patient Mobile", "Patient GSTIN",
        "Subtotal", "Discount", "CGST", "SGST", "IGST",
        "Tax Total", "Round Off", "Grand Total", "Rounded Total",
        "Paid", "Due",
        "Linked Sale", "Linked Service Ticket",
    ]

    async def rows_iter():
        cursor = db.invoices.find(
            q,
            {"_id": 0, "invoice_no": 1, "invoice_date": 1, "status": 1,
             "patient_name": 1, "mrd": 1, "patient_mobile": 1, "patient_gstin": 1,
             "subtotal": 1, "discount_total": 1, "cgst_total": 1, "sgst_total": 1,
             "igst_total": 1, "tax_total": 1, "round_off": 1, "grand_total": 1,
             "rounded_total": 1, "paid_total": 1, "due_total": 1,
             "linked_sale_no": 1, "ticket_no": 1},
        ).sort([("invoice_date", -1), ("invoice_id", -1)])
        async for inv in cursor:
            yield [
                inv.get("invoice_no") or "",
                str(inv.get("invoice_date") or "")[:19],
                inv.get("status") or "",
                inv.get("patient_name") or "",
                inv.get("mrd") or "",
                inv.get("patient_mobile") or "",
                inv.get("patient_gstin") or "",
                inv.get("subtotal") or 0,
                inv.get("discount_total") or 0,
                inv.get("cgst_total") or 0,
                inv.get("sgst_total") or 0,
                inv.get("igst_total") or 0,
                inv.get("tax_total") or 0,
                inv.get("round_off") or 0,
                inv.get("grand_total") or 0,
                inv.get("rounded_total") or 0,
                inv.get("paid_total") or 0,
                inv.get("due_total") or 0,
                inv.get("linked_sale_no") or "",
                inv.get("ticket_no") or "",
            ]

    return await stream_csv(
        filename_prefix=f"audinexa-invoices-{user['clinic_id']}",
        headers=headers,
        rows_iter=rows_iter(),
    )


@billing_router.get("/billing/invoices/{invoice_id}", response_model=Invoice)
async def get_invoice(invoice_id: str,
                      user=Depends(get_current_user), db=Depends(get_db)):
    inv = await db.invoices.find_one({"invoice_id": invoice_id, "clinic_id": user["clinic_id"]}, {"_id": 0})
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return _deserialize(inv)


@billing_router.post("/billing/invoices/{invoice_id}/payments", response_model=Invoice)
async def add_payment(invoice_id: str, payload: PaymentCreate,
                      user=Depends(require_roles(*_PAYMENT_ROLES)),
                      db=Depends(get_db)):
    """NAV-009 · Capture a patient payment against an invoice.

    Concurrency & correctness: delegates to `record_payment_atomic`
    which enforces overpayment guard, tenant match and cancelled /
    refunded status blocks AT THE DATABASE LAYER via an aggregation-
    pipeline `find_one_and_update`. See helper docstring for the full
    design. Every payment written on this route lands in BOTH
    `db.payments` (for `/billing/payments` + `/billing/collections`
    KPIs) and `invoices.payments[]` (for InvoiceDetail rendering).
    """
    # Snapshot pre-write status for the auto-flip side-effect gate
    # below (mark_sale_paid_internal + accessory stock decrement).
    prev = await db.invoices.find_one(
        {"invoice_id": invoice_id, "clinic_id": user["clinic_id"]},
        {"_id": 0, "status": 1, "linked_sale_no": 1, "invoice_no": 1},
    )
    was_paid_before = bool(prev and prev.get("status") == "paid")
    linked_sale_no = prev.get("linked_sale_no") if prev else None

    updated_doc, _pay = await record_payment_atomic(
        db,
        clinic_id=user["clinic_id"],
        invoice_id=invoice_id,
        amount=float(payload.amount),
        method=payload.method,
        received_by_user_id=user["user_id"],
        reference=payload.reference,
        notes=payload.notes,
        enforce_overpay=True,
    )
    inv = Invoice(**_deserialize(updated_doc))

    # Auto-flip linked HA sale → paid (P2 Quote→Sale→Invoice→Paid one-click)
    # Trigger only on the transition to fully-paid; idempotent if already paid.
    if inv.status == "paid" and not was_paid_before and linked_sale_no:
        try:
            from routers.ha_sales import mark_sale_paid_internal
            await mark_sale_paid_internal(
                db, user["clinic_id"], linked_sale_no,
                actor_user_id=user["user_id"], invoice_no=inv.invoice_no,
            )
        except HTTPException as exc:
            logging.getLogger(__name__).warning(
                f"Auto-flip on payment skipped for sale {linked_sale_no}: {exc.detail}",
            )

    # ---- Auto-decrement accessory stock on paid transition -----------
    # Fires exactly once per line, guarded by
    # `InvoiceLine.accessory_stock_decremented`. Never raises — a stock
    # mismatch must not block the clinic from taking money.
    if inv.status == "paid" and not was_paid_before:
        try:
            from utils.accessory_stock import auto_decrement_accessory_stock
            fresh_inv_doc = await db.invoices.find_one({"invoice_id": invoice_id}, {"_id": 0})
            if fresh_inv_doc:
                await auto_decrement_accessory_stock(
                    db, fresh_inv_doc,
                    actor_user_id=user["user_id"],
                    branch_id=user.get("branch_id"),
                )
        except Exception as exc:  # noqa: BLE001 — defensive
            logging.getLogger(__name__).warning(
                f"Accessory auto-decrement failed for invoice {inv.invoice_no}: {exc}",
            )
    return inv


@billing_router.post("/billing/invoices/{invoice_id}/refund", response_model=Invoice)
async def refund_invoice(invoice_id: str, payload: RefundCreate,
                         user=Depends(get_current_user), db=Depends(get_db)):
    """Record a refund against a paid/partial invoice — record-only, no
    payment-gateway integration. Persists a Payment row with
    `kind="refund"` and a NEGATIVE amount so `_sum_invoice()` naturally
    subtracts it from `paid_total` and re-derives the status.

    * Full refund   → status becomes ``refunded``
    * Partial refund → status becomes ``partially_refunded``
    * Repeated partials are additive (multiple refund rows accumulate).

    Roles allowed: clinic_owner, accounts, front_desk, super_admin, founder.
    """
    if user.get("role") not in {"clinic_owner", "accounts", "front_desk", "super_admin", "founder"}:
        raise HTTPException(status_code=403, detail="You don't have permission to issue refunds")

    # NAV-009 · REF-001 — delegate to atomic writer. Pre-flight
    # existence check kept to preserve historical 404 payload shape
    # (record_refund_atomic returns 404 too, but this way any future
    # divergence stays localised).
    exists = await db.invoices.find_one(
        {"invoice_id": invoice_id, "clinic_id": user["clinic_id"]},
        {"_id": 0, "invoice_id": 1},
    )
    if not exists:
        raise HTTPException(status_code=404, detail="Invoice not found")

    updated_doc, _refund = await record_refund_atomic(
        db,
        clinic_id=user["clinic_id"],
        invoice_id=invoice_id,
        amount=float(payload.amount),
        method=payload.method,
        reason=payload.reason,
        reference=payload.reference,
        notes=payload.notes,
        received_by_user_id=user["user_id"],
    )
    return Invoice(**_deserialize(updated_doc))


# --------------- CONSOLIDATED PAYMENTS + REFUNDS LISTING ---------------


@billing_router.get("/billing/payments")
async def list_payments_and_refunds(
    kind: Optional[Literal["payment", "refund"]] = None,
    since: Optional[str] = None,             # ISO date, e.g. 2026-07-01
    until: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    user=Depends(get_current_user), db=Depends(get_db),
):
    """Consolidated view for the Billing → Payments & Refunds tab.

    Joins each payment row with its parent invoice so the UI can render
    invoice_no, patient_name, current invoice status alongside the
    payment amount, method, and (for refunds) the reason.
    """
    q: dict = {"clinic_id": user["clinic_id"]}
    if kind:
        q["kind"] = kind
    elif kind is None:
        # `kind` was added 2026-07-30; old rows don't carry it. Treat missing
        # as "payment" so the default view (no filter) still surfaces legacy.
        pass
    if since or until:
        rng: dict = {}
        if since:
            rng["$gte"] = since
        if until:
            rng["$lte"] = until
        q["paid_at"] = rng

    limit = max(1, min(int(limit or 100), 500))
    offset = max(0, int(offset or 0))
    cursor = db.payments.find(q, {"_id": 0}).sort("paid_at", -1).skip(offset).limit(limit)
    rows = await cursor.to_list(length=limit)

    # Enrich with invoice_no + patient_name for the UI.
    inv_ids = list({r.get("invoice_id") for r in rows if r.get("invoice_id")})
    inv_map: dict[str, dict] = {}
    if inv_ids:
        inv_cursor = db.invoices.find(
            {"invoice_id": {"$in": inv_ids}, "clinic_id": user["clinic_id"]},
            {"_id": 0, "invoice_id": 1, "invoice_no": 1, "patient_name": 1,
             "patient_id": 1, "status": 1, "grand_total": 1, "rounded_total": 1},
        )
        async for inv in inv_cursor:
            inv_map[inv["invoice_id"]] = inv

    enriched = []
    for r in rows:
        inv = inv_map.get(r.get("invoice_id"), {})
        # Backfill kind for legacy rows — anything with a positive amount
        # is a payment; anything negative is a refund. Cheap heuristic.
        row_kind = r.get("kind") or ("refund" if float(r.get("amount") or 0) < 0 else "payment")
        enriched.append({
            "payment_id":     r.get("payment_id"),
            "invoice_id":     r.get("invoice_id"),
            "invoice_no":     inv.get("invoice_no"),
            "patient_id":     inv.get("patient_id"),
            "patient_name":   inv.get("patient_name"),
            "invoice_status": inv.get("status"),
            "kind":           row_kind,
            "amount":         float(r.get("amount") or 0),
            "method":         r.get("method"),
            "reference":      r.get("reference"),
            "reason":         r.get("reason"),
            "notes":          r.get("notes"),
            "paid_at":        r.get("paid_at"),
            "received_by_user_id": r.get("received_by_user_id"),
        })

    # Rollup — powers the top-of-page KPIs.
    total_payments = round(sum(x["amount"] for x in enriched if x["kind"] == "payment"), 2)
    total_refunds = round(-sum(x["amount"] for x in enriched if x["kind"] == "refund"), 2)
    return {
        "items": enriched,
        "count": len(enriched),
        "offset": offset,
        "limit": limit,
        "totals": {
            "payments": total_payments,
            "refunds": total_refunds,
            "net": round(total_payments - total_refunds, 2),
        },
    }


@billing_router.post("/billing/invoices/{invoice_id}/cancel", response_model=Invoice)
async def cancel_invoice(invoice_id: str, payload: dict,
                         user=Depends(get_current_user), db=Depends(get_db)):
    if user["role"] not in {"super_admin", "accounts"}:
        raise HTTPException(status_code=403, detail="Only accounts/admin can cancel invoices")
    inv = await db.invoices.find_one({"invoice_id": invoice_id, "clinic_id": user["clinic_id"]}, {"_id": 0})
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if inv.get("status") == "cancelled":
        raise HTTPException(status_code=400, detail="Already cancelled")
    reason = (payload or {}).get("reason") or "Cancelled"
    await db.invoices.update_one(
        {"invoice_id": invoice_id},
        {"$set": _serialize({
            "status": "cancelled",
            "cancelled_at": datetime.utcnow(),
            "cancelled_reason": reason,
        })},
    )
    updated = await db.invoices.find_one({"invoice_id": invoice_id}, {"_id": 0})
    return _deserialize(updated)


# --------------- DAILY COLLECTIONS SUMMARY ---------------

@billing_router.get("/billing/collections")
async def collections_summary(date: Optional[str] = None,
                              user=Depends(get_current_user), db=Depends(get_db)):
    """Daily collections broken down by payment method for the given date (YYYY-MM-DD) or today."""
    day = date or datetime.now(IST).strftime("%Y-%m-%d")
    q = {
        "clinic_id": user["clinic_id"],
        "paid_at": {"$gte": f"{day}T00:00:00", "$lte": f"{day}T23:59:59"},
    }
    rows = await db.payments.find(q, {"_id": 0}).to_list(1000)
    by_method: dict = {}
    total = 0.0
    for r in rows:
        m = r.get("method", "other")
        amt = float(r.get("amount", 0.0))
        by_method[m] = round(by_method.get(m, 0.0) + amt, 2)
        total += amt
    return {
        "date": day,
        "total": round(total, 2),
        "by_method": by_method,
        "payment_count": len(rows),
    }


# --------------- REPORT HANDOVER ---------------

@billing_router.get("/billing/pending-reports")
async def pending_reports(user=Depends(get_current_user), db=Depends(get_db)):  # noqa: ARG001
    """DEPRECATED (Feb 2026 v2) — the handover feature has been scrapped.

    Kept as an empty stub so any in-flight UI / mobile client that still polls
    this endpoint degrades gracefully to an empty list instead of 404-ing.
    Audiology reports now flip directly `draft` → `completed` when the
    audiologist clicks "Save & Print Report", and the Reports module shows
    one list of completed sessions.
    """
    return []


@billing_router.post("/billing/report-deliveries", response_model=ReportDelivery)
async def record_delivery(payload: dict,
                          user=Depends(get_current_user), db=Depends(get_db)):
    """Body: {session_id, channel, invoice_id?, recipient?, notes?}"""
    session_id = payload.get("session_id")
    channel = payload.get("channel")
    if not session_id or channel not in {"print", "whatsapp", "email", "in_person"}:
        raise HTTPException(status_code=400, detail="session_id and valid channel required")

    s = await db.test_sessions.find_one({"session_id": session_id}, {"_id": 0})
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")
    # Tenant check via patient
    pat = await db.patients.find_one({"patient_id": s.get("patient_id"), "clinic_id": user["clinic_id"]}, {"_id": 0})
    if not pat:
        raise HTTPException(status_code=403, detail="Not authorised")

    delivery = ReportDelivery(
        clinic_id=user["clinic_id"],
        session_id=session_id,
        patient_id=s.get("patient_id"),
        invoice_id=payload.get("invoice_id"),
        channel=channel,
        recipient=payload.get("recipient"),
        notes=payload.get("notes"),
        delivered_by_user_id=user["user_id"],
    )
    await db.report_deliveries.insert_one(_serialize(delivery.model_dump()))
    return delivery


@billing_router.get("/billing/report-deliveries", response_model=List[ReportDelivery])
async def list_deliveries(
    session_id: Optional[str] = None,
    patient_id: Optional[str] = None,
    limit: int = 200,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    q: dict = {"clinic_id": user["clinic_id"]}
    if session_id:
        q["session_id"] = session_id
    if patient_id:
        q["patient_id"] = patient_id
    rows = await db.report_deliveries.find(q, {"_id": 0}).sort("delivered_at", -1).to_list(limit)
    return [_deserialize(r) for r in rows]


# --------------- Default service catalogue seeding ---------------

DEFAULT_SERVICES = [
    # Healthcare: GST-exempt (is_taxable=False, gst_rate=0)
    {"code": "CONSULT", "name": "Audiology Consultation", "category": "Consultation", "hsn_sac": "999312", "price": 500.0, "gst_rate": 0.0, "is_taxable": False},
    {"code": "PTA", "name": "Pure Tone Audiometry", "category": "Audiology", "hsn_sac": "999312", "price": 800.0, "gst_rate": 0.0, "is_taxable": False},
    {"code": "IMM", "name": "Immittance (Tymp + Reflex)", "category": "Audiology", "hsn_sac": "999312", "price": 600.0, "gst_rate": 0.0, "is_taxable": False},
    {"code": "OAE", "name": "Otoacoustic Emissions (OAE)", "category": "Audiology", "hsn_sac": "999312", "price": 1000.0, "gst_rate": 0.0, "is_taxable": False},
    {"code": "ABR", "name": "ABR/BERA", "category": "Audiology", "hsn_sac": "999312", "price": 2500.0, "gst_rate": 0.0, "is_taxable": False},
    {"code": "ASSR", "name": "ASSR", "category": "Audiology", "hsn_sac": "999312", "price": 3000.0, "gst_rate": 0.0, "is_taxable": False},
    {"code": "SPEECH", "name": "Speech Audiometry", "category": "Audiology", "hsn_sac": "999312", "price": 800.0, "gst_rate": 0.0, "is_taxable": False},
    {"code": "HAF", "name": "Hearing Aid Fitting", "category": "Audiology", "hsn_sac": "999312", "price": 1500.0, "gst_rate": 0.0, "is_taxable": False},
    # Hearing aids + accessories: GST-applicable (HSN 9021 = 12% typical for hearing aids; accessories 18%)
    {"code": "HA-BTE", "name": "Hearing Aid – BTE (per unit)", "category": "Hearing Aid", "hsn_sac": "9021", "price": 35000.0, "gst_rate": 12.0, "is_taxable": True, "gst_inclusive": True},
    {"code": "HA-RIC", "name": "Hearing Aid – RIC (per unit)", "category": "Hearing Aid", "hsn_sac": "9021", "price": 55000.0, "gst_rate": 12.0, "is_taxable": True, "gst_inclusive": True},
    {"code": "BATTERY", "name": "Hearing Aid Battery (pack of 6)", "category": "Accessory", "hsn_sac": "8506", "price": 300.0, "gst_rate": 18.0, "is_taxable": True, "gst_inclusive": True},
    {"code": "EARMOULD", "name": "Custom Ear Mould", "category": "Accessory", "hsn_sac": "9021", "price": 1200.0, "gst_rate": 12.0, "is_taxable": True, "gst_inclusive": True},
]


async def seed_default_services(db, clinic_id: str):
    """Idempotent seed of default service catalogue for a clinic."""
    existing_count = await db.services.count_documents({"clinic_id": clinic_id})
    if existing_count > 0:
        return 0
    inserted = 0
    for s in DEFAULT_SERVICES:
        obj = Service(clinic_id=clinic_id, **s)
        await db.services.insert_one(_serialize(obj.model_dump()))
        inserted += 1
    return inserted
