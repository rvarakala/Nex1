"""M12 — Referral Partner Portal (Phase 13.C).

A Referral Partner is an external entity (ENT doctor, senior-care home, GP
chain, audiology college) that refers patients to an ACS clinic in exchange
for a commission on the resulting revenue.

7 UCs covered:
  1. Partner registration (admin invite or self-signup → pending approval)
  2. Partner approval / activation (super-admin / clinic owner)
  3. Unique referral code generation
  4. Patient tagging on registration (via referral_code)
  5. Partner dashboard — referred patients, revenue earned
  6. Commission calculation (flat % or fixed ₹ per referral)
  7. Payout ledger — clinic marks payouts as "paid"

A Partner has its own login (separate role: `referral_partner`). JWT is
issued by standard /auth/login but the resulting scope is limited to their
own partner_id.

Tier gate: PREMIUM.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta, date
from typing import List, Optional, Literal
from uuid import uuid4
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from auth import (
    get_current_user, require_roles, hash_password,
    create_access_token, verify_password,
)
from database import get_db
from utils.numbering import next_number
from utils.serde import serialize_datetime, deserialize_datetime
from utils.tiers import require_tier
from utils.idempotency import IdempotencyContext
from fastapi.responses import JSONResponse


router = APIRouter(prefix="/api/referral-partners")


PartnerStatus = Literal["pending", "active", "suspended"]
CommissionKind = Literal["percent", "fixed"]


# ==================== MODELS ====================

class ReferralPartner(BaseModel):
    model_config = ConfigDict(extra="ignore")
    partner_id: str = Field(default_factory=lambda: f"RP-{str(uuid4())[:8].upper()}")
    clinic_id: str
    name: str
    email: EmailStr
    phone: Optional[str] = None
    organization: Optional[str] = None
    specialization: Optional[str] = None
    city: Optional[str] = None
    referral_code: str                   # unique per-clinic (human-readable)
    commission_kind: CommissionKind = "percent"
    commission_value: float = 5.0        # 5% of revenue OR ₹500 per referral
    bank_details: Optional[dict] = None  # account_no, ifsc, account_name
    status: PartnerStatus = "pending"
    notes: Optional[str] = None
    partner_since: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class PartnerCreate(BaseModel):
    name: str
    email: EmailStr
    phone: Optional[str] = None
    organization: Optional[str] = None
    specialization: Optional[str] = None
    city: Optional[str] = None
    referral_code: Optional[str] = None    # auto-generated if not provided
    commission_kind: CommissionKind = "percent"
    commission_value: float = 5.0
    password: Optional[str] = None         # if set → provisions login
    notes: Optional[str] = None


class PartnerUpdate(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    organization: Optional[str] = None
    specialization: Optional[str] = None
    city: Optional[str] = None
    commission_kind: Optional[CommissionKind] = None
    commission_value: Optional[float] = None
    bank_details: Optional[dict] = None
    status: Optional[PartnerStatus] = None
    notes: Optional[str] = None


class PartnerSelfSignup(BaseModel):
    clinic_id: str                       # they need to know which clinic
    name: str
    email: EmailStr
    phone: Optional[str] = None
    organization: Optional[str] = None
    specialization: Optional[str] = None
    city: Optional[str] = None
    password: str = Field(min_length=8)


class PartnerLogin(BaseModel):
    email: EmailStr
    password: str


class PartnerPayout(BaseModel):
    model_config = ConfigDict(extra="ignore")
    payout_id: str                                     # PAY-YYYY-NNNN
    clinic_id: str
    partner_id: str
    period_start: str                                  # YYYY-MM-DD
    period_end: str                                    # YYYY-MM-DD
    referral_count: int
    attributed_revenue: float
    commission_amount: float
    # NAV-011 · Bundle 5 · Recovery-ledger applied against this payout.
    gross_commission_amount: float = 0.0
    recovery_applied_amount: float = 0.0
    recovery_applied_ids: List[str] = Field(default_factory=list)
    status: Literal["pending", "paid", "void", "reversed"] = "pending"
    paid_at: Optional[str] = None
    payment_ref: Optional[str] = None
    notes: Optional[str] = None
    # NAV-011 · Bundle 4 · Actor tracking on every lifecycle transition.
    created_by_user_id: Optional[str] = None
    created_by_name: Optional[str] = None
    paid_by_user_id: Optional[str] = None
    paid_by_name: Optional[str] = None
    voided_at: Optional[str] = None
    voided_by_user_id: Optional[str] = None
    voided_by_name: Optional[str] = None
    void_reason: Optional[str] = None
    reversed_at: Optional[str] = None
    reversed_by_user_id: Optional[str] = None
    reversed_by_name: Optional[str] = None
    reverse_reason: Optional[str] = None
    recovery_ledger_id_on_reverse: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class PayoutCreate(BaseModel):
    # NAV-011 · Bundle 3 · period bounds are now mandatory; null windows rejected.
    period_start: str = Field(..., min_length=10, max_length=10)
    period_end: str = Field(..., min_length=10, max_length=10)
    notes: Optional[str] = None


class PayoutMarkPaid(BaseModel):
    payment_ref: Optional[str] = None


class PayoutVoidIn(BaseModel):
    """NAV-011 · Bundle 4 · Void a pending payout with a mandatory reason."""
    reason: str = Field(..., min_length=1, max_length=500)


class PayoutReverseIn(BaseModel):
    """NAV-011 · Bundle 4 · Reverse an already-paid payout. Reason mandatory.
    Automatically creates a `partner_recovery_ledger` entry for the full
    commission amount so the clinic can claw it back on the next payout."""
    reason: str = Field(..., min_length=1, max_length=500)


# ==================== HELPERS ====================

def _gen_code(name: str) -> str:
    base = "".join(c for c in (name or "").upper() if c.isalpha())[:4] or "PTR"
    return f"{base}-{secrets.token_hex(2).upper()}"


async def _resolve_partner_from_user(user: dict, db) -> dict:
    """For users with role=referral_partner, look up their partner row."""
    row = await db.referral_partners.find_one(
        {"clinic_id": user["clinic_id"], "linked_user_id": user["user_id"]},
        {"_id": 0},
    )
    if not row:
        raise HTTPException(status_code=404, detail="No partner profile linked to your login")
    return row


def _compute_commission(partner: dict, revenue: float, referrals: int) -> float:
    if (partner.get("commission_kind") or "percent") == "percent":
        pct = float(partner.get("commission_value") or 0)
        return round(revenue * pct / 100.0, 2)
    return round(float(partner.get("commission_value") or 0) * referrals, 2)


async def _attribute_revenue(db, clinic_id: str, partner_id: str,
                             start: Optional[str] = None, end: Optional[str] = None) -> dict:
    """NAV-011 · canonical commissionable-revenue formula.

    Commissionable revenue per invoice = max(0, paid_total - refunded_total).
    Only invoices with status ∉ {cancelled} count. HA-sale contributes iff its
    status ∈ {delivered, paid} AND the linked invoice's net-collected > 0.

    Historical fields:
      * `paid_total` was introduced by NAV-009 · most invoices carry it;
        legacy rows lacking it are treated as 0 (nothing collected).
      * `refunded_total` was introduced by NAV-009 · legacy rows default to 0.

    NAV-011 · Phase 2C — Category-aware attribution (2026-08-21)
    ------------------------------------------------------------
    In addition to the pre-existing `invoice_revenue`, `ha_sale_revenue`,
    and `total_revenue` fields (all preserved for backward compatibility),
    the response now includes three READ-SIDE analytics-only fields:

      * ``diagnostics_revenue`` — the Diagnostics-Income slice of the
        invoice net-collected total.
      * ``ha_sales_revenue`` — the Hearing-Aid / Core-Business slice.
      * ``total_attributed_revenue`` — the sum of the two mutually-
        exclusive category buckets.

    Classification rules (identical to the internal-doctor path in
    ``routers/referrals.py`` — replicated here LOCALLY to satisfy the
    Phase 2C scope which forbids modifying the internal-doctor code):

      1. If the invoice's linked appointment has ``wing='hearing_aid'``,
         the ENTIRE invoice contribution is HA. This heals legacy
         rows whose lines lack ``product_type``.
      2. Otherwise, per line: HA if ``product_type == 'Hearing Aid'``,
         Diagnostics otherwise.
      3. Legacy fallback (invoice has no line breakdown at all): HA
         if ``is_ha_wing`` OR ``ticket_no`` is set, else Diagnostics.
      4. Each bucket is scaled by ``net_collected / grand_total`` so
         the sum of the two buckets equals canonical net-collected.

    Phase 2C is READ-ONLY: no writes, no migration, no schema change,
    no payout-writer change. Existing fields are NOT modified.
    """
    pat_q = {"clinic_id": clinic_id, "referral_partner_id": partner_id}
    if start or end:
        created = {}
        if start: created["$gte"] = start + "T00:00:00"
        if end:   created["$lt"] = end + "T00:00:00"
        pat_q["created_at"] = created

    patients = await db.patients.find(pat_q, {"_id": 0, "patient_id": 1}).to_list(20000)
    pids = [p["patient_id"] for p in patients]

    invoice_rev = 0.0
    net_by_invoice: dict[str, float] = {}
    # NAV-011 · Phase 2C — collect the raw invoice docs we need for the
    # category-aware pass. We keep the legacy aggregation for
    # ``invoice_rev`` untouched and do the classification in a small
    # in-Python second pass so the existing test surface is preserved.
    invoices_for_classification: list[dict] = []
    if pids:
        async for row in db.invoices.aggregate([
            {"$match": {
                "clinic_id": clinic_id,
                "patient_id": {"$in": pids},
                "status": {"$nin": ["cancelled", "draft"]},
            }},
            {"$project": {
                "_id": 0,
                "invoice_id": 1,
                # Fields required for category-aware classification.
                "appointment_id": 1,
                "grand_total": 1,
                "rounded_total": 1,
                "lines": 1,
                "ticket_no": 1,
                # NAV-011 canonical formula.
                #
                # NAV-009 stores refunds as NEGATIVE entries in the
                # embedded payments[] array and derives paid_total via
                # `sum(payments.amount)` — so `paid_total` is ALREADY
                # net-of-refunds. Subtracting `refunded_total` a second
                # time would double-count.
                #
                # Legacy fallback: pre-NAV-009 invoices that are fully
                # `paid` may not carry a `paid_total` field at all. For
                # those, we treat `paid_total` as `grand_total` (the
                # accepted pre-NAV-009 semantics) so historical revenue
                # is not silently zeroed out.
                "net_collected": {
                    "$max": [0, {"$cond": [
                        {"$eq": [{"$ifNull": ["$paid_total", None]}, None]},
                        {"$cond": [
                            {"$eq": ["$status", "paid"]},
                            {"$ifNull": ["$grand_total", 0]},
                            0,
                        ]},
                        {"$ifNull": ["$paid_total", 0]},
                    ]}]
                },
            }},
        ]):
            net = float(row.get("net_collected") or 0)
            if net > 0:
                net_by_invoice[row["invoice_id"]] = net
                invoice_rev += net
                invoices_for_classification.append(row)

    # HA-sale contribution: only if the linked invoice actually collected money.
    ha_rev = 0.0
    if pids:
        async for row in db.ha_sales.aggregate([
            {"$match": {
                "clinic_id": clinic_id,
                "patient_id": {"$in": pids},
                "status": {"$in": ["delivered", "paid"]},
            }},
            {"$project": {"_id": 0, "invoice_id": 1, "invoice_no": 1, "total": 1}},
        ]):
            # gate on the linked invoice's net-collected > 0
            inv_id = row.get("invoice_id")
            gate = net_by_invoice.get(inv_id) if inv_id else None
            if gate is None and row.get("invoice_no"):
                # Older ha_sales rows link by invoice_no rather than invoice_id.
                inv = await db.invoices.find_one(
                    {"invoice_no": row["invoice_no"], "clinic_id": clinic_id},
                    {"_id": 0, "paid_total": 1, "status": 1},
                )
                if inv and (inv.get("status") not in ("cancelled", "draft")):
                    gate = max(0.0, float(inv.get("paid_total") or 0))
            if gate and gate > 0:
                ha_rev += float(row.get("total") or 0)

    # ── NAV-011 · Phase 2C · Category-aware classification pass ──
    # We split ``invoice_rev`` (already the canonical net-collected sum)
    # into ``diagnostics_revenue`` and ``ha_sales_revenue`` buckets
    # using the same rules the internal-doctor dashboard applies. This
    # is a pure in-memory pass on the invoice docs we already fetched
    # above — NO additional DB round-trips beyond the appointment
    # batch lookup.
    diagnostics_rev = 0.0
    ha_sales_rev = 0.0
    if invoices_for_classification:
        appt_ids = list({
            inv.get("appointment_id")
            for inv in invoices_for_classification
            if inv.get("appointment_id")
        })
        wing_by_appt: dict[str, str] = {}
        if appt_ids:
            async for a in db.appointments.find(
                {"clinic_id": clinic_id, "appointment_id": {"$in": appt_ids}},
                {"_id": 0, "appointment_id": 1, "wing": 1},
            ):
                wing_by_appt[a["appointment_id"]] = a.get("wing") or "diagnostic"

        for inv in invoices_for_classification:
            net_collected = float(inv.get("net_collected") or 0.0)
            if net_collected <= 0.005:
                continue
            is_ha_wing = (
                wing_by_appt.get(inv.get("appointment_id") or "") == "hearing_aid"
            )
            diag_amt = 0.0
            ha_amt = 0.0
            for ln in (inv.get("lines") or []):
                if not isinstance(ln, dict):
                    continue
                amt = float(ln.get("line_total") or 0.0)
                if is_ha_wing or ln.get("product_type") == "Hearing Aid":
                    ha_amt += amt
                else:
                    diag_amt += amt
            # Legacy fallback: invoice has no line breakdown (very old
            # or imported data). Bucket the whole invoice by parent
            # linkage (matches referrals.py:274-281).
            if not (diag_amt or ha_amt):
                gt = float(
                    inv.get("grand_total") or inv.get("rounded_total") or 0.0
                )
                if is_ha_wing or inv.get("ticket_no"):
                    ha_amt += gt
                else:
                    diag_amt += gt
            # Scale each bucket so the invoice's total contribution
            # equals canonical net_collected (matches referrals.py:283-289).
            gross = float(
                inv.get("grand_total") or inv.get("rounded_total") or 0.0
            )
            if gross > 0.005:
                scale = min(1.0, net_collected / gross)
                diag_amt *= scale
                ha_amt *= scale
            diagnostics_rev += diag_amt
            ha_sales_rev += ha_amt

    return {
        # ── Pre-existing fields — preserved for backward compat ──
        "patients": len(pids),
        "invoice_revenue": round(invoice_rev, 2),
        "ha_sale_revenue": round(ha_rev, 2),
        "total_revenue": round(invoice_rev + ha_rev, 2),
        # ── NAV-011 · Phase 2C · Category-aware attribution ──
        # These three fields split the canonical `invoice_revenue`
        # (net-collected) into two mutually-exclusive buckets.
        # ``total_attributed_revenue`` therefore equals ``invoice_revenue``
        # to within rounding — it is NOT the same as ``total_revenue``,
        # which continues to include the separate ha_sales-collection
        # add-on that pre-dates Phase 2C.
        "diagnostics_revenue": round(diagnostics_rev, 2),
        "ha_sales_revenue": round(ha_sales_rev, 2),
        "total_attributed_revenue": round(diagnostics_rev + ha_sales_rev, 2),
    }


# ─── NAV-011 · Bundle 5 · Recovery Ledger ─────────────────────────────
#
# A `partner_recovery_ledger` row represents money that the clinic must
# claw back from a partner because a refund / cancellation reduced the
# net-collected revenue AFTER a commission cheque was already cut.
# The row starts `pending`; the next payout automatically deducts it
# and flips it to `applied`. Manual entries are created via the endpoint
# below by accounts / clinic_owner. Phase 2A does NOT auto-emit these
# from NAV-009 or NAV-010 atomic paths (deferred to Phase 2B).

class RecoveryCreate(BaseModel):
    partner_id: str
    amount: float = Field(..., gt=0)
    reason: str = Field(..., min_length=1, max_length=500)
    source_kind: Literal["refund", "cancellation", "manual", "duplicate_reconciliation"] = "manual"
    source_invoice_id: Optional[str] = None
    source_payout_id: Optional[str] = None


async def _emit_referral_event(
    db, *, clinic_id: str, kind: str, actor: dict,
    subject_id: str, old_value: Optional[dict] = None,
    new_value: Optional[dict] = None, reason: Optional[str] = None,
    ref_doc: Optional[dict] = None,
) -> None:
    """Best-effort audit row insertion. Never raises — audit failure must
    not block the primary financial action."""
    try:
        event_id = await next_number(db, "referral_event", clinic_id)
        await db.referral_audit_events.insert_one({
            "event_id": event_id,
            "clinic_id": clinic_id,
            "kind": kind,
            "actor_user_id": actor.get("user_id"),
            "actor_name": actor.get("name", ""),
            "actor_role": actor.get("role"),
            "subject_id": subject_id,
            "old_value": old_value,
            "new_value": new_value,
            "reason": reason,
            "ref_doc": ref_doc,
            "at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception:  # noqa: BLE001 — audit must never block financial action
        import logging
        logging.getLogger(__name__).warning(
            "NAV-011 referral audit event insertion failed clinic=%s kind=%s subject=%s",
            clinic_id, kind, subject_id,
        )


async def _consume_pending_recovery(
    db, *, clinic_id: str, partner_id: str, gross_commission: float, actor: dict,
) -> tuple[float, list[str], float]:
    """Consume pending recovery entries against a fresh gross commission.

    Returns:
        net_commission: max(0, gross - deducted); recovery never drives negative.
        applied_ids:    list of recovery_ids that were consumed (partial or full).
        deducted:       actual rupees applied.

    Any residual (recovery > gross) stays in the ledger as pending for the
    next payout window.
    """
    if gross_commission <= 0:
        return (max(0.0, gross_commission), [], 0.0)
    remaining_budget = gross_commission
    applied_ids: list[str] = []
    deducted_total = 0.0
    async for rec in db.partner_recovery_ledger.find(
        {"clinic_id": clinic_id, "partner_id": partner_id, "status": "pending"},
        {"_id": 0},
    ).sort("created_at", 1):
        if remaining_budget <= 0.005:
            break
        rec_amount = float(rec.get("amount") or 0)
        apply_now = min(rec_amount, remaining_budget)
        now_iso = datetime.now(timezone.utc).isoformat()
        if apply_now >= rec_amount - 0.005:
            # Fully applied — flip status.
            await db.partner_recovery_ledger.update_one(
                {"recovery_id": rec["recovery_id"], "status": "pending"},
                {"$set": {
                    "status": "applied",
                    "applied_at": now_iso,
                    "applied_amount": apply_now,
                }},
            )
        else:
            # Partial — reduce amount, keep pending.
            await db.partner_recovery_ledger.update_one(
                {"recovery_id": rec["recovery_id"], "status": "pending"},
                {"$set": {"amount": round(rec_amount - apply_now, 2)}},
            )
        applied_ids.append(rec["recovery_id"])
        deducted_total += apply_now
        remaining_budget -= apply_now
        await _emit_referral_event(
            db, clinic_id=clinic_id, kind="recovery_deducted",
            actor=actor, subject_id=rec["recovery_id"],
            old_value={"amount": rec_amount, "status": "pending"},
            new_value={"amount_deducted": round(apply_now, 2)},
            ref_doc={"partner_id": partner_id},
        )
    return (round(max(0.0, gross_commission - deducted_total), 2),
            applied_ids, round(deducted_total, 2))


# ==================== PUBLIC: SELF-SIGNUP ====================

@router.post("/public/signup")
async def partner_self_signup(payload: PartnerSelfSignup, db=Depends(get_db)):
    """Open endpoint — partners signup for a clinic and land in 'pending' status.
    Clinic owner must approve before they become active.
    """
    clinic = await db.clinics.find_one({"clinic_id": payload.clinic_id}, {"_id": 0, "clinic_id": 1})
    if not clinic:
        raise HTTPException(status_code=404, detail="Unknown clinic")

    # Email unique across users (partners share the users collection so /auth/login works)
    existing = await db.users.find_one({"email": payload.email.lower()})
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered")

    code = _gen_code(payload.name)
    while await db.referral_partners.find_one({"clinic_id": payload.clinic_id, "referral_code": code}):
        code = _gen_code(payload.name)

    partner = ReferralPartner(
        clinic_id=payload.clinic_id,
        name=payload.name,
        email=payload.email,
        phone=payload.phone,
        organization=payload.organization,
        specialization=payload.specialization,
        city=payload.city,
        referral_code=code,
        status="pending",
    )

    user_id = f"USR-{str(os.urandom(4).hex()).upper()}"
    user_doc = {
        "user_id": user_id,
        "clinic_id": payload.clinic_id,
        "email": payload.email.lower(),
        "name": payload.name,
        "role": "referral_partner",
        "active": True,                # allow login; tenant gate handles access
        "password_hash": hash_password(payload.password),
        "created_at": datetime.utcnow(),
        "branch_ids": [],
    }
    await db.users.insert_one(serialize_datetime(user_doc))

    p_doc = serialize_datetime(partner.model_dump())
    p_doc["linked_user_id"] = user_id
    await db.referral_partners.insert_one(p_doc)

    return {
        "partner_id": partner.partner_id,
        "referral_code": partner.referral_code,
        "status": partner.status,
        "message": "Thank you. Your application is pending approval.",
    }


# ==================== PARTNER SELF API (role=referral_partner) ====================

@router.get("/me")
async def partner_me(user=Depends(get_current_user), db=Depends(get_db)):
    if user["role"] != "referral_partner":
        raise HTTPException(status_code=403, detail="Not a partner account")
    p = await _resolve_partner_from_user(user, db)
    return deserialize_datetime(p)


@router.get("/me/dashboard")
async def partner_dashboard(
    days: int = 90,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    if user["role"] != "referral_partner":
        raise HTTPException(status_code=403, detail="Not a partner account")
    p = await _resolve_partner_from_user(user, db)
    if p["status"] != "active":
        return {
            "partner": deserialize_datetime(p),
            "status_message": "Your account is pending approval by the clinic.",
            "stats": {
                # Legacy fields (backward compat).
                "patients": 0, "invoice_revenue": 0,
                "ha_sale_revenue": 0, "total_revenue": 0,
                # NAV-011 · Phase 2C · category-aware attribution fields
                # exposed with zero values so the response shape is
                # uniform between pending and active partners.
                "diagnostics_revenue": 0, "ha_sales_revenue": 0,
                "total_attributed_revenue": 0,
            },
            "recent_patients": [],
            "payouts": [],
        }
    start_iso = (date.today() - timedelta(days=days)).isoformat()
    end_iso = (date.today() + timedelta(days=1)).isoformat()
    stats = await _attribute_revenue(db, user["clinic_id"], p["partner_id"], start_iso, end_iso)

    # Recent 25 patients (privacy-redacted name — first + initial)
    recent = await db.patients.find(
        {"clinic_id": user["clinic_id"], "referral_partner_id": p["partner_id"]},
        {"_id": 0, "patient_id": 1, "name": 1, "created_at": 1, "city": 1},
    ).sort("created_at", -1).to_list(25)
    redacted = []
    for r in recent:
        nm = (r.get("name") or "").strip().split(" ", 1)
        first = nm[0] if nm else "Patient"
        last_initial = nm[1][0] + "." if len(nm) > 1 and nm[1] else ""
        redacted.append({
            "patient_id": r["patient_id"],
            "display_name": f"{first} {last_initial}".strip(),
            "created_at": r.get("created_at"),
            "city": r.get("city"),
        })

    payouts = await db.partner_payouts.find(
        {"clinic_id": user["clinic_id"], "partner_id": p["partner_id"]},
        {"_id": 0},
    ).sort("created_at", -1).to_list(20)

    stats["commission_estimate"] = _compute_commission(p, stats["total_revenue"], stats["patients"])

    return {
        "partner": deserialize_datetime(p),
        "window_days": days,
        "stats": stats,
        "recent_patients": redacted,
        "payouts": [deserialize_datetime(pp) for pp in payouts],
    }


# ==================== CLINIC-SIDE: PARTNER MANAGEMENT (PREMIUM-gated) ====================

ADMIN_ROLES = ("clinic_owner", "super_admin", "accounts")


@router.get(
    "",
    dependencies=[Depends(require_tier("referral-partners"))],
)
async def list_partners(
    status: Optional[str] = None,
    user=Depends(require_roles(*ADMIN_ROLES)),
    db=Depends(get_db),
):
    q = {"clinic_id": user["clinic_id"]}
    if status:
        q["status"] = status
    rows = await db.referral_partners.find(q, {"_id": 0}).sort("created_at", -1).to_list(500)
    return [deserialize_datetime(r) for r in rows]


@router.post(
    "",
    dependencies=[Depends(require_tier("referral-partners"))],
)
async def create_partner(
    payload: PartnerCreate,
    user=Depends(require_roles(*ADMIN_ROLES)),
    db=Depends(get_db),
):
    existing_user = await db.users.find_one({"email": payload.email.lower()})
    if existing_user:
        raise HTTPException(status_code=409, detail="Email already registered")

    code = payload.referral_code or _gen_code(payload.name)
    while await db.referral_partners.find_one({"clinic_id": user["clinic_id"], "referral_code": code}):
        code = _gen_code(payload.name)

    partner = ReferralPartner(
        clinic_id=user["clinic_id"],
        name=payload.name,
        email=payload.email,
        phone=payload.phone,
        organization=payload.organization,
        specialization=payload.specialization,
        city=payload.city,
        referral_code=code,
        commission_kind=payload.commission_kind,
        commission_value=payload.commission_value,
        status="active",                        # clinic-created partners auto-active
        partner_since=date.today().isoformat(),
        notes=payload.notes,
    )
    p_doc = serialize_datetime(partner.model_dump())

    # Optional: provision login immediately
    if payload.password:
        user_id = f"USR-{str(os.urandom(4).hex()).upper()}"
        await db.users.insert_one(serialize_datetime({
            "user_id": user_id,
            "clinic_id": user["clinic_id"],
            "email": payload.email.lower(),
            "name": payload.name,
            "role": "referral_partner",
            "active": True,
            "password_hash": hash_password(payload.password),
            "created_at": datetime.utcnow(),
            "branch_ids": [],
        }))
        p_doc["linked_user_id"] = user_id

    await db.referral_partners.insert_one(p_doc)
    p_doc.pop("_id", None)
    return deserialize_datetime(p_doc)


@router.patch(
    "/{partner_id}",
    dependencies=[Depends(require_tier("referral-partners"))],
)
async def update_partner(
    partner_id: str,
    payload: PartnerUpdate,
    user=Depends(require_roles(*ADMIN_ROLES)),
    db=Depends(get_db),
):
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    # if activating for the first time, stamp partner_since
    if updates.get("status") == "active":
        existing = await db.referral_partners.find_one(
            {"partner_id": partner_id, "clinic_id": user["clinic_id"]},
            {"_id": 0, "partner_since": 1},
        )
        if existing and not existing.get("partner_since"):
            updates["partner_since"] = date.today().isoformat()
    res = await db.referral_partners.find_one_and_update(
        {"partner_id": partner_id, "clinic_id": user["clinic_id"]},
        {"$set": updates},
        projection={"_id": 0},
        return_document=True,
    )
    if not res:
        raise HTTPException(status_code=404, detail="Partner not found")
    return deserialize_datetime(res)


@router.get(
    "/{partner_id}/stats",
    dependencies=[Depends(require_tier("referral-partners"))],
)
async def partner_stats(
    partner_id: str,
    days: int = 90,
    user=Depends(require_roles(*ADMIN_ROLES)),
    db=Depends(get_db),
):
    p = await db.referral_partners.find_one(
        {"partner_id": partner_id, "clinic_id": user["clinic_id"]},
        {"_id": 0},
    )
    if not p:
        raise HTTPException(status_code=404, detail="Partner not found")
    start_iso = (date.today() - timedelta(days=days)).isoformat()
    end_iso = (date.today() + timedelta(days=1)).isoformat()
    stats = await _attribute_revenue(db, user["clinic_id"], partner_id, start_iso, end_iso)
    stats["commission_estimate"] = _compute_commission(p, stats["total_revenue"], stats["patients"])
    return {"partner": deserialize_datetime(p), "window_days": days, "stats": stats}


# ==================== PATIENT TAGGING ====================

class AttachCodePayload(BaseModel):
    referral_code: str


@router.post(
    "/patients/{patient_id}/attach-code",
    dependencies=[Depends(require_tier("referral-partners"))],
)
async def attach_referral_code(
    patient_id: str,
    payload: AttachCodePayload,
    user=Depends(require_roles("front_desk", "clinic_owner", "super_admin", "accounts", "audiologist")),
    db=Depends(get_db),
):
    code = payload.referral_code.strip().upper()
    partner = await db.referral_partners.find_one({
        "clinic_id": user["clinic_id"],
        "referral_code": code,
        "status": "active",
    }, {"_id": 0, "partner_id": 1, "name": 1, "referral_code": 1})
    if not partner:
        raise HTTPException(status_code=404, detail=f"No active partner for code {code}")
    res = await db.patients.update_one(
        {"patient_id": patient_id, "clinic_id": user["clinic_id"]},
        {"$set": {
            "referral_partner_id": partner["partner_id"],
            "referral_source": "Partner",
        }},
    )
    if not res.matched_count:
        raise HTTPException(status_code=404, detail="Patient not found")
    return {"patient_id": patient_id, "partner_id": partner["partner_id"], "partner_name": partner["name"]}


# ==================== PAYOUTS ====================

@router.post(
    "/{partner_id}/payouts",
    dependencies=[Depends(require_tier("referral-partners"))],
)
async def create_payout(
    partner_id: str,
    payload: PayoutCreate,
    request: Request,
    user=Depends(require_roles("clinic_owner", "super_admin", "accounts")),
    db=Depends(get_db),
):
    """NAV-011 · Bundle 3 (overlap guard) + Bundle 4 (actor tracking) +
    Bundle 5 (recovery deduction).

    * Rejects overlapping / duplicate active-payout windows for the same
      (clinic, partner).
    * Deducts pending `partner_recovery_ledger` entries from the gross
      commission before storing.
    * Records `created_by_user_id`.

    NAV-012 · Optional `Idempotency-Key` header dedups the create per
    `(clinic_id, "payout", key)` for 24h.  The correlation id is
    embedded on the created `partner_payouts` row so a crash-recovery
    retry can detect whether this write landed.
    """
    idem = await IdempotencyContext.enter(
        request, db,
        scope="payout", clinic_id=user["clinic_id"],
        actor=user,
        payload={"partner_id": partner_id, **payload.model_dump()},
        route="/api/referral-partners/{partner_id}/payouts",
        operation_collection="partner_payouts",
    )
    if idem.replayed:
        body, status, headers = idem.replay_response()
        return JSONResponse(content=body, status_code=status, headers=headers)

    try:
        p = await db.referral_partners.find_one(
            {"partner_id": partner_id, "clinic_id": user["clinic_id"]},
            {"_id": 0},
        )
        if not p:
            raise HTTPException(status_code=404, detail="Partner not found")

        # ── Validate period bounds — reject inverted / null windows ────
        if not payload.period_start or not payload.period_end:
            raise HTTPException(
                status_code=422,
                detail="period_start and period_end are required (YYYY-MM-DD).",
            )
        if payload.period_start > payload.period_end:
            raise HTTPException(
                status_code=422,
                detail="period_start must be ≤ period_end",
            )

        # ── Bundle 3 · Overlap guard ───────────────────────────────────
        # Two ACTIVE (status ≠ void) payouts for the same (clinic, partner)
        # cannot share overlapping [start, end] windows. Inclusive on both
        # ends because YYYY-MM-DD strings compare correctly lexicographically.
        conflict = await db.partner_payouts.find_one(
            {
                "clinic_id": user["clinic_id"],
                "partner_id": partner_id,
                "status": {"$nin": ["void"]},
                "period_start": {"$lte": payload.period_end},
                "period_end":   {"$gte": payload.period_start},
            },
            {"_id": 0, "payout_id": 1, "period_start": 1, "period_end": 1, "status": 1},
        )
        if conflict:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Overlapping active payout {conflict['payout_id']} exists for this "
                    f"partner covering {conflict['period_start']} → {conflict['period_end']} "
                    f"(status={conflict['status']}). Void it first or choose a distinct window."
                ),
            )

        stats = await _attribute_revenue(
            db, user["clinic_id"], partner_id,
            payload.period_start, payload.period_end,
        )
        gross_commission = _compute_commission(p, stats["total_revenue"], stats["patients"])

        # ── Bundle 5 · Recovery-ledger deduction ────────────────────────
        net_commission, applied_ids, deducted = await _consume_pending_recovery(
            db, clinic_id=user["clinic_id"], partner_id=partner_id,
            gross_commission=gross_commission, actor=user,
        )

        payout_id = await next_number(db, "payout", user["clinic_id"])
        payout = PartnerPayout(
            payout_id=payout_id,
            clinic_id=user["clinic_id"],
            partner_id=partner_id,
            period_start=payload.period_start,
            period_end=payload.period_end,
            referral_count=stats["patients"],
            attributed_revenue=stats["total_revenue"],
            commission_amount=net_commission,
            gross_commission_amount=gross_commission,
            recovery_applied_amount=deducted,
            recovery_applied_ids=applied_ids,
            notes=payload.notes,
            created_by_user_id=user.get("user_id"),
            created_by_name=user.get("name", ""),
        )
        payout_doc = serialize_datetime(payout.model_dump())
        if idem.enabled:
            payout_doc["idempotency_correlation_id"] = idem.correlation_id
        await db.partner_payouts.insert_one(payout_doc)
        await _emit_referral_event(
            db, clinic_id=user["clinic_id"], kind="payout_created",
            actor=user, subject_id=payout_id,
            new_value={
                "period_start": payload.period_start, "period_end": payload.period_end,
                "gross_commission": gross_commission,
                "recovery_deducted": deducted, "net_commission": net_commission,
            },
            ref_doc={"partner_id": partner_id},
        )
        response_body = serialize_datetime(payout.model_dump())
        if idem.enabled:
            await idem.complete(
                http_status=200, response_body=response_body,
                operation_id=payout_id,
            )
        return payout
    except HTTPException as exc:
        if idem.enabled:
            await idem.fail(
                http_status=exc.status_code,
                response_body={"detail": exc.detail},
                detail=str(exc.detail),
            )
        raise


@router.get(
    "/{partner_id}/payouts",
    dependencies=[Depends(require_tier("referral-partners"))],
)
async def list_payouts(
    partner_id: str,
    user=Depends(require_roles(*ADMIN_ROLES)),
    db=Depends(get_db),
):
    rows = await db.partner_payouts.find(
        {"partner_id": partner_id, "clinic_id": user["clinic_id"]},
        {"_id": 0},
    ).sort("created_at", -1).to_list(100)
    return [deserialize_datetime(r) for r in rows]


@router.post(
    "/{partner_id}/payouts/{payout_id}/mark-paid",
    dependencies=[Depends(require_tier("referral-partners"))],
)
async def mark_paid(
    partner_id: str,
    payout_id: str,
    payload: PayoutMarkPaid,
    user=Depends(require_roles("clinic_owner", "super_admin", "accounts")),
    db=Depends(get_db),
):
    # NAV-011 · Bundle 4 · records paid_by_user_id via CAS on status=pending.
    now_iso = datetime.now(timezone.utc).isoformat()
    res = await db.partner_payouts.find_one_and_update(
        {"payout_id": payout_id, "partner_id": partner_id,
         "clinic_id": user["clinic_id"], "status": "pending"},
        {"$set": {
            "status": "paid",
            "paid_at": now_iso,
            "payment_ref": payload.payment_ref,
            "paid_by_user_id": user.get("user_id"),
            "paid_by_name": user.get("name", ""),
        }},
        projection={"_id": 0},
        return_document=True,
    )
    if not res:
        raise HTTPException(status_code=404, detail="Pending payout not found")
    await _emit_referral_event(
        db, clinic_id=user["clinic_id"], kind="payout_marked_paid",
        actor=user, subject_id=payout_id,
        old_value={"status": "pending"},
        new_value={"status": "paid", "payment_ref": payload.payment_ref, "paid_at": now_iso},
        ref_doc={"partner_id": partner_id},
    )
    return deserialize_datetime(res)


# ─── NAV-011 · Bundle 4 · Void a pending payout ───────────────────────

@router.post(
    "/{partner_id}/payouts/{payout_id}/void",
    dependencies=[Depends(require_tier("referral-partners"))],
)
async def void_payout(
    partner_id: str,
    payout_id: str,
    payload: PayoutVoidIn,
    user=Depends(require_roles("clinic_owner", "super_admin", "accounts")),
    db=Depends(get_db),
):
    """Void a pending payout. CAS on status=pending guarantees exactly one
    voider even under contention with mark-paid. Reason mandatory. The
    original row is NEVER deleted — status flips to `void` and the
    ledger keeps history."""
    now_iso = datetime.now(timezone.utc).isoformat()
    res = await db.partner_payouts.find_one_and_update(
        {"payout_id": payout_id, "partner_id": partner_id,
         "clinic_id": user["clinic_id"], "status": "pending"},
        {"$set": {
            "status": "void",
            "voided_at": now_iso,
            "voided_by_user_id": user.get("user_id"),
            "voided_by_name": user.get("name", ""),
            "void_reason": payload.reason,
        }},
        projection={"_id": 0},
        return_document=True,
    )
    if not res:
        # Diagnose: does it exist but not pending?
        existing = await db.partner_payouts.find_one(
            {"payout_id": payout_id, "partner_id": partner_id, "clinic_id": user["clinic_id"]},
            {"_id": 0, "status": 1},
        )
        if not existing:
            raise HTTPException(status_code=404, detail="Payout not found")
        raise HTTPException(
            status_code=409,
            detail=f"Cannot void — payout is in state {existing['status']!r} (only 'pending' can be voided)",
        )
    await _emit_referral_event(
        db, clinic_id=user["clinic_id"], kind="payout_voided",
        actor=user, subject_id=payout_id,
        old_value={"status": "pending"},
        new_value={"status": "void", "void_reason": payload.reason},
        reason=payload.reason,
        ref_doc={"partner_id": partner_id},
    )
    return deserialize_datetime(res)


# ─── NAV-011 · Bundle 4 · Reverse a paid payout ───────────────────────

@router.post(
    "/{partner_id}/payouts/{payout_id}/reverse",
    dependencies=[Depends(require_tier("referral-partners"))],
)
async def reverse_payout(
    partner_id: str,
    payout_id: str,
    payload: PayoutReverseIn,
    user=Depends(require_roles("clinic_owner")),   # owner-only + super_admin/founder bypass
    db=Depends(get_db),
):
    """Reverse an already-paid payout. Creates a `partner_recovery_ledger`
    entry so the clinic can deduct the full commission amount from the
    partner's next payout. The original PAID row stays intact — status
    flips to `reversed` for historical clarity.

    Owner-only per approved Decision #14 (Phase 2A).
    """
    now_iso = datetime.now(timezone.utc).isoformat()

    # ── Pre-flight ──────────────────────────────────────────────
    existing = await db.partner_payouts.find_one(
        {"payout_id": payout_id, "partner_id": partner_id, "clinic_id": user["clinic_id"]},
        {"_id": 0},
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Payout not found")
    if existing.get("status") != "paid":
        raise HTTPException(
            status_code=409,
            detail=f"Cannot reverse — payout is in state {existing.get('status')!r} (only 'paid' can be reversed).",
        )

    # ── Recovery entry FIRST, so if the CAS on the payout loses we
    # do not leave the ledger inconsistent. We keep the recovery
    # even if the CAS lost — it becomes the audit of the intent.
    recovery_id = await next_number(db, "recovery", user["clinic_id"])
    await db.partner_recovery_ledger.insert_one({
        "recovery_id": recovery_id,
        "clinic_id": user["clinic_id"],
        "partner_id": partner_id,
        "amount": float(existing.get("commission_amount") or 0),
        "status": "pending",
        "applied_to_payout": None,
        "applied_at": None,
        "source_kind": "manual",
        "source_invoice_id": None,
        "source_payout_id": payout_id,
        "reason": f"Reversal of paid payout {payout_id}: {payload.reason}",
        "created_by": user.get("user_id"),
        "created_at": now_iso,
    })

    # ── CAS: only flip if still paid ────────────────────────────
    res = await db.partner_payouts.find_one_and_update(
        {"payout_id": payout_id, "partner_id": partner_id,
         "clinic_id": user["clinic_id"], "status": "paid"},
        {"$set": {
            "status": "reversed",
            "reversed_at": now_iso,
            "reversed_by_user_id": user.get("user_id"),
            "reversed_by_name": user.get("name", ""),
            "reverse_reason": payload.reason,
            "recovery_ledger_id_on_reverse": recovery_id,
        }},
        projection={"_id": 0},
        return_document=True,
    )
    if not res:
        # Race: someone else changed the status. Best-effort void the
        # recovery we just created so we do not leave an orphan.
        await db.partner_recovery_ledger.update_one(
            {"recovery_id": recovery_id, "status": "pending"},
            {"$set": {"status": "void",
                       "void_reason": "reversal CAS lost — no state change",
                       "applied_at": now_iso}},
        )
        raise HTTPException(
            status_code=409,
            detail="Payout state changed during reversal — retry.",
        )
    await _emit_referral_event(
        db, clinic_id=user["clinic_id"], kind="payout_reversed",
        actor=user, subject_id=payout_id,
        old_value={"status": "paid", "commission_amount": existing.get("commission_amount")},
        new_value={"status": "reversed", "recovery_ledger_id": recovery_id,
                   "reverse_reason": payload.reason},
        reason=payload.reason,
        ref_doc={"partner_id": partner_id, "recovery_id": recovery_id},
    )
    return deserialize_datetime(res)


# ─── NAV-011 · Bundle 5 · Recovery-ledger endpoints ───────────────────

@router.post(
    "/recovery-ledger",
    dependencies=[Depends(require_tier("referral-partners"))],
)
async def create_recovery(
    payload: RecoveryCreate,
    user=Depends(require_roles("clinic_owner", "super_admin", "accounts")),
    db=Depends(get_db),
):
    """Endpoint-driven creation of a recovery obligation. Phase 2A does NOT
    auto-emit from NAV-009/NAV-010 atomic paths — that is deferred to 2B.

    The recovery amount is deducted from the partner's NEXT payout
    (Bundle 5 · `_consume_pending_recovery` runs inside `create_payout`).
    """
    partner = await db.referral_partners.find_one(
        {"partner_id": payload.partner_id, "clinic_id": user["clinic_id"]},
        {"_id": 0, "partner_id": 1},
    )
    if not partner:
        raise HTTPException(status_code=404, detail="Partner not found")

    recovery_id = await next_number(db, "recovery", user["clinic_id"])
    now_iso = datetime.now(timezone.utc).isoformat()
    doc = {
        "recovery_id": recovery_id,
        "clinic_id": user["clinic_id"],
        "partner_id": payload.partner_id,
        "amount": round(float(payload.amount), 2),
        "status": "pending",
        "applied_to_payout": None,
        "applied_at": None,
        "source_kind": payload.source_kind,
        "source_invoice_id": payload.source_invoice_id,
        "source_payout_id": payload.source_payout_id,
        "reason": payload.reason,
        "created_by": user.get("user_id"),
        "created_at": now_iso,
    }
    await db.partner_recovery_ledger.insert_one(dict(doc))
    await _emit_referral_event(
        db, clinic_id=user["clinic_id"], kind="recovery_created",
        actor=user, subject_id=recovery_id,
        new_value={"amount": doc["amount"], "source_kind": payload.source_kind},
        reason=payload.reason,
        ref_doc={"partner_id": payload.partner_id,
                  "source_invoice_id": payload.source_invoice_id,
                  "source_payout_id": payload.source_payout_id},
    )
    return doc


@router.get(
    "/recovery-ledger",
    dependencies=[Depends(require_tier("referral-partners"))],
)
async def list_recovery(
    partner_id: Optional[str] = None,
    status: Optional[str] = None,
    user=Depends(require_roles(*ADMIN_ROLES)),
    db=Depends(get_db),
):
    q: dict = {"clinic_id": user["clinic_id"]}
    if partner_id:
        q["partner_id"] = partner_id
    if status:
        q["status"] = status
    rows = await db.partner_recovery_ledger.find(q, {"_id": 0}).sort("created_at", -1).to_list(500)
    return rows
