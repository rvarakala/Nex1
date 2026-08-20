"""Referral Corner — owner-grade revenue + payout dashboard.

A focused, owner-and-delegate UI that tells the clinic exactly which
referring doctors are sending business and how much the practice owes
them in commission for the chosen window.

Endpoints:
  GET    /api/referrals/access                — caller's effective access
  GET    /api/referrals/dashboard             — per-doctor revenue + payout
  PATCH  /api/referrals/doctors/{id}/cut-config  — set % / ₹ cut config
  GET    /api/referrals/payout-report.csv     — owner-side CSV export

Revenue rules (locked by product call, 2026-06-30):
  • DIAGNOSTICS revenue per doctor = sum of invoice-line totals for
    paid invoices where the patient was referred by that doctor AND the
    line's `product_type` is NOT "Hearing Aid".
  • HA SALES revenue per doctor = sum of invoice-line totals for paid
    invoices where the patient was referred by that doctor AND the line's
    `product_type == "Hearing Aid"`. We additionally require the
    underlying HA sale (when linked) to be in a `delivered`/`paid` state
    — trials, returned, and cancelled deals are excluded.
  • Payout per category = either `value%` of category revenue
    (mode='percent') or `value × patient_count` (mode='flat'). Defaults
    to 0 when no mode is configured.

Access control:
  • Always allowed: super_admin, clinic_owner.
  • Optional grant: any user with `can_access_referrals = True`. Owners
    flip this via the staff settings page.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from auth import get_current_user
from database import get_db


router = APIRouter(prefix="/api/referrals")


# ───────────────────────────────────────────────────────────────────────
# Access dependency
# ───────────────────────────────────────────────────────────────────────
async def _require_referral_access(user=Depends(get_current_user)):
    role = user.get("role")
    if role in ("super_admin", "clinic_owner"):
        return user
    if user.get("can_access_referrals"):
        return user
    raise HTTPException(
        status_code=403,
        detail="Referral Corner access is owner-only by default. "
               "Ask the clinic owner to enable it for your account in Settings → Staff.",
    )


def _is_owner(user) -> bool:
    return user.get("role") in ("super_admin", "clinic_owner")


# ───────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────
def _parse_window(start: Optional[str], end: Optional[str]) -> tuple[datetime, datetime]:
    """Default = month-to-date in IST. Clamps end to "now" to prevent
    accidental future-dated queries from returning zero rows silently.

    When the caller passes a date-only string (`2026-07-31`), we pad the
    end to 23:59:59.999999 so invoices created later that same day are
    still included. Without this pad, an invoice raised at 10:04 on
    31-Jul would be excluded from the "This month" query because the
    end anchors at 00:00 of 31-Jul — a real bug hit during the Vishnu /
    Dr Prasad walkthrough (2026-07-31).
    """
    now = datetime.now(timezone.utc)
    if not end:
        end_dt = now
    else:
        end_dt = datetime.fromisoformat(end).replace(tzinfo=timezone.utc)
        # Date-only input (no explicit time) → pad to end-of-day
        if len(end) <= 10:
            end_dt = end_dt.replace(hour=23, minute=59, second=59, microsecond=999999)
        if end_dt > now:
            end_dt = now
    if not start:
        # Default: first day of the current calendar month, UTC
        start_dt = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    else:
        start_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    if start_dt > end_dt:
        raise HTTPException(status_code=400, detail="start must be <= end")
    return start_dt, end_dt


def _compute_payout(revenue: float, flat_patient_count: int,
                     mode: Optional[str], value: float) -> float:
    """Translate a configured cut into rupees owed.

    Two modes:
      • percent → `value%` of `revenue` (e.g. 10% of ₹50 000 = ₹5 000)
      • flat    → `value × flat_patient_count`. `flat_patient_count` MUST
        be the count of patients who ACTUALLY contributed revenue to
        this bucket (diag or HA), NOT the aggregate referred-patient
        count. Otherwise a "flat per patient HA cut" would pay the
        doctor even for patients who never bought a hearing aid.
        (Regression fix — see test_referral_flat_payout_scoping.py.)
    """
    if not mode or not value:
        return 0.0
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    if mode == "percent":
        return round(revenue * v / 100.0, 2)
    if mode == "flat":
        return round(v * max(0, int(flat_patient_count)), 2)
    return 0.0


async def _dashboard_rows(db, clinic_id: str, start_dt: datetime, end_dt: datetime):
    """Build the per-doctor revenue rollup. Pure function over Mongo —
    no auth concerns here; the caller has already gated access."""
    start_iso = start_dt.isoformat()
    end_iso = end_dt.isoformat()

    # 1. Pull all referring doctors for this clinic. We seed the rollup
    #    with a row for every doctor (even those with zero referrals in
    #    the window) so the owner sees the complete pad while configuring
    #    cuts. The empty-revenue rows are sorted to the bottom in the API.
    doctors: dict[str, dict] = {}
    async for d in db.referring_doctors.find(
        {"clinic_id": clinic_id},
        {"_id": 0},
    ):
        doctors[d["doctor_id"]] = {
            "doctor_id": d["doctor_id"],
            "name": d.get("name") or d["doctor_id"],
            "specialty": d.get("specialty"),
            "clinic": d.get("clinic"),
            "phone": d.get("phone"),
            "diag_cut_mode": d.get("diag_cut_mode"),
            "diag_cut_value": float(d.get("diag_cut_value") or 0.0),
            "ha_cut_mode": d.get("ha_cut_mode"),
            "ha_cut_value": float(d.get("ha_cut_value") or 0.0),
            "patient_count": 0,
            "patient_ids": set(),
            "diagnostics_revenue": 0.0,
            "ha_sales_revenue": 0.0,
            # Per-patient contribution tracking so we can count only the
            # patients who ACTUALLY contributed to each bucket for the
            # flat-per-patient payout mode. Also lets the drill-down show
            # each patient's billing without a second Mongo aggregation.
            "per_patient_diag": {},   # {patient_id: cumulative diag ₹}
            "per_patient_ha": {},     # {patient_id: cumulative HA ₹}
        }

    # 2. Find every patient in the clinic that points to one of these
    #    doctors. We use referring_doctor_id (the canonical FK) — the
    #    free-text `referring_physician` field is ignored on purpose
    #    because it can't be reliably matched to a doctor record.
    #
    # NAV-011 · Bundle 2 · Partner > Doctor precedence
    # ------------------------------------------------
    # If a patient ALSO has `referral_partner_id` set, the external
    # partner earns the commission (Decision #5). We exclude such
    # patients from the doctor rollup so no invoice can pay commission
    # twice on the same underlying revenue.
    patient_to_doctor: dict[str, str] = {}
    async for p in db.patients.find(
        {"clinic_id": clinic_id, "referring_doctor_id": {"$in": list(doctors.keys()) or [None]}},
        {"_id": 0, "patient_id": 1, "referring_doctor_id": 1, "referral_partner_id": 1},
    ):
        # Partner-exclusivity gate — patient owed to partner, not doctor.
        if p.get("referral_partner_id"):
            continue
        did = p.get("referring_doctor_id")
        pid = p.get("patient_id")
        if did and pid and did in doctors:
            patient_to_doctor[pid] = did

    # Note: if `patient_to_doctor` is empty, the queries below produce
    # no matches (they filter on `patient_id: {"$in": [...]}`) and we
    # fall straight through to the finalize loop with all-zero rows.
    # Regression fix (2026-07-31): the previous early-return here
    # crashed /referrals/dashboard with `KeyError: 'diagnostics_payout'`
    # for clinics whose referring doctors had no linked patients yet.

    # 3. Pull invoices in window for those patients, applying the
    #    NAV-011 canonical revenue formula.
    #
    # NAV-011 · Bundle 1 · canonical commissionable revenue
    # -----------------------------------------------------
    # Commissionable per invoice = max(0, paid_total − refunded_total).
    # We include statuses {paid, partial, partially_refunded} — anything
    # where money has been collected — and gate on net > 0. `cancelled`,
    # `refunded`, `draft`, and `unpaid` invoices contribute 0.
    #
    # The per-line diag-vs-HA split remains driven by `product_type` /
    # appointment wing. Each line's contribution is then SCALED by
    # `net / grand_total` so the total commissionable for the invoice
    # matches the canonical formula while preserving the bucket ratio.
    invoices_to_process: list[dict] = []
    async for inv in db.invoices.find(
        {
            "clinic_id": clinic_id,
            "patient_id": {"$in": list(patient_to_doctor.keys())},
            "status": {"$in": ["paid", "partial", "partially_refunded"]},
            "invoice_date": {"$gte": start_iso, "$lte": end_iso},
        },
        {"_id": 0, "patient_id": 1, "lines": 1, "ticket_no": 1, "session_id": 1,
         "grand_total": 1, "rounded_total": 1, "appointment_id": 1,
         "paid_total": 1, "refunded_total": 1, "status": 1},
    ):
        invoices_to_process.append(inv)

    # Batch-fetch the linked appointments so we know each invoice's wing.
    appt_ids = list({inv.get("appointment_id") for inv in invoices_to_process if inv.get("appointment_id")})
    wing_by_appt: dict[str, str] = {}
    if appt_ids:
        async for a in db.appointments.find(
            {"clinic_id": clinic_id, "appointment_id": {"$in": appt_ids}},
            {"_id": 0, "appointment_id": 1, "wing": 1},
        ):
            wing_by_appt[a["appointment_id"]] = a.get("wing") or "diagnostic"

    for inv in invoices_to_process:
        did = patient_to_doctor.get(inv.get("patient_id"))
        if not did or did not in doctors:
            continue

        # NAV-011 · canonical net-collected for this invoice.
        # NAV-009 stores refunds as NEGATIVE payment amounts, so
        # `paid_total` is ALREADY net-of-refunds. `refunded_total` is
        # retained as a separate audit field but NOT subtracted here
        # (doing so would double-count).
        #
        # Legacy fallback: pre-NAV-009 fully-paid invoices may not carry
        # `paid_total` — treat as `grand_total` in that case, else 0.
        pt = inv.get("paid_total")
        if pt is None:
            if (inv.get("status") or "").lower() == "paid":
                net_collected = float(inv.get("grand_total") or inv.get("rounded_total") or 0.0)
            else:
                net_collected = 0.0
        else:
            net_collected = max(0.0, float(pt))
        if net_collected <= 0.005:
            continue  # nothing commissionable on this invoice

        # If the linked appointment is on the HA wing, count the whole
        # invoice as HA revenue (heals legacy `product_type=None` rows).
        is_ha_wing = wing_by_appt.get(inv.get("appointment_id") or "") == "hearing_aid"

        diag_rev = 0.0
        ha_rev = 0.0
        for ln in (inv.get("lines") or []):
            if not isinstance(ln, dict):
                continue
            amt = float(ln.get("line_total") or 0.0)
            if is_ha_wing or ln.get("product_type") == "Hearing Aid":
                ha_rev += amt
            else:
                diag_rev += amt

        # Edge case: invoice has no line breakdown (very old or imported
        # data) — fall back to grand_total and bucket by parent linkage.
        if not (diag_rev or ha_rev):
            gt = float(inv.get("grand_total") or 0.0)
            if is_ha_wing or inv.get("ticket_no"):       # HA service ticket → HA
                ha_rev += gt
            else:
                diag_rev += gt

        # NAV-011 · scale each bucket by (net_collected / gross) so the
        # invoice's total contribution matches canonical net-collected.
        gross = float(inv.get("grand_total") or inv.get("rounded_total") or 0.0)
        if gross > 0.005:
            scale = min(1.0, net_collected / gross)
            diag_rev *= scale
            ha_rev *= scale

        doctors[did]["diagnostics_revenue"] += diag_rev
        doctors[did]["ha_sales_revenue"] += ha_rev
        doctors[did]["patient_ids"].add(inv.get("patient_id"))
        # Attribute each invoice's contribution to the patient — the
        # per-patient dicts feed the drill-down UI and the flat-per-patient
        # payout counts.
        pid = inv.get("patient_id")
        if pid:
            if diag_rev:
                doctors[did]["per_patient_diag"][pid] = (
                    doctors[did]["per_patient_diag"].get(pid, 0.0) + diag_rev
                )
            if ha_rev:
                doctors[did]["per_patient_ha"][pid] = (
                    doctors[did]["per_patient_ha"].get(pid, 0.0) + ha_rev
                )

    # 4. Tighten HA revenue against the linked HA-sale lifecycle. The user's
    #    rule: only "delivered AND paid" sales count. Trials/returns/
    #    cancelled deals are excluded. We look up linked sales via the
    #    patient_id (best-available join), exclude any sale not in the
    #    allowed set, and *subtract* that sale's invoice contribution.
    #    NOTE: this is a tightening, not a re-source — the invoice's `paid`
    #    status remains the primary truth.
    ha_sale_blacklist_patients: set[str] = set()
    async for sale in db.ha_sales.find(
        {
            "clinic_id": clinic_id,
            "patient_id": {"$in": list(patient_to_doctor.keys())},
            "status": {"$in": ["trial", "cancelled", "returned"]},
        },
        {"_id": 0, "patient_id": 1, "status": 1},
    ):
        # Conservative: if ANY linked sale is not closed, we treat this
        # patient's HA revenue as "not yet earned" for the doctor's payout.
        # The owner can override by re-mapping the invoice if needed.
        ha_sale_blacklist_patients.add(sale["patient_id"])

    # Re-walk the rollup: if a doctor has HA revenue contributed by a
    # blacklisted patient, drop that contribution back out. This is rare
    # in practice (most paid invoices are for closed deals) but matters
    # for clinics that bill at trial start.
    if ha_sale_blacklist_patients:
        async for inv in db.invoices.find(
            {
                "clinic_id": clinic_id,
                "patient_id": {"$in": list(ha_sale_blacklist_patients)},
                "status": "paid",
                "invoice_date": {"$gte": start_iso, "$lte": end_iso},
            },
            {"_id": 0, "patient_id": 1, "lines": 1, "appointment_id": 1, "grand_total": 1},
        ):
            did = patient_to_doctor.get(inv.get("patient_id"))
            if not did or did not in doctors:
                continue
            is_ha_wing = wing_by_appt.get(inv.get("appointment_id") or "") == "hearing_aid"
            for ln in (inv.get("lines") or []):
                if not isinstance(ln, dict):
                    continue
                if is_ha_wing or ln.get("product_type") == "Hearing Aid":
                    amt = float(ln.get("line_total") or 0.0)
                    doctors[did]["ha_sales_revenue"] = max(
                        0.0, doctors[did]["ha_sales_revenue"] - amt,
                    )
                    # Mirror the trim on the per-patient dict so the
                    # flat-count / drill-down see the corrected value.
                    pid = inv.get("patient_id")
                    if pid and pid in doctors[did]["per_patient_ha"]:
                        doctors[did]["per_patient_ha"][pid] = max(
                            0.0, doctors[did]["per_patient_ha"][pid] - amt,
                        )
                        if doctors[did]["per_patient_ha"][pid] == 0.0:
                            doctors[did]["per_patient_ha"].pop(pid, None)

    # 5. Finalise: patient counts + payout computation.
    rows = []
    for d in doctors.values():
        d["patient_count"] = len(d.pop("patient_ids", set()))
        # Flat-per-patient counts MUST use the buckets that actually saw
        # revenue — see _compute_payout for the reasoning. A patient
        # whose HA line was fully blacklisted has been popped from
        # per_patient_ha, so they no longer count for the HA flat cut.
        diag_flat_count = len([pid for pid, amt in d["per_patient_diag"].items() if amt > 0])
        ha_flat_count = len([pid for pid, amt in d["per_patient_ha"].items() if amt > 0])
        d["diag_patient_count"] = diag_flat_count
        d["ha_patient_count"] = ha_flat_count
        d["diagnostics_payout"] = _compute_payout(
            d["diagnostics_revenue"], diag_flat_count,
            d["diag_cut_mode"], d["diag_cut_value"],
        )
        d["ha_payout"] = _compute_payout(
            d["ha_sales_revenue"], ha_flat_count,
            d["ha_cut_mode"], d["ha_cut_value"],
        )
        d["total_payout"] = round(d["diagnostics_payout"] + d["ha_payout"], 2)
        d["diagnostics_revenue"] = round(d["diagnostics_revenue"], 2)
        d["ha_sales_revenue"] = round(d["ha_sales_revenue"], 2)
        d["total_revenue"] = round(d["diagnostics_revenue"] + d["ha_sales_revenue"], 2)
        rows.append(d)

    # Sort: doctors with revenue first (desc), then alphabetical for the rest.
    rows.sort(key=lambda r: (-(r["total_revenue"]), r["name"].lower()))
    return rows


# ───────────────────────────────────────────────────────────────────────
# Endpoints
# ───────────────────────────────────────────────────────────────────────
@router.get("/access")
async def my_access(user=Depends(get_current_user)):
    """The frontend nav uses this to show / hide the menu item."""
    return {
        "has_access": _is_owner(user) or bool(user.get("can_access_referrals")),
        "role": user.get("role"),
        "is_owner": _is_owner(user),
    }


@router.get("/dashboard")
async def referral_dashboard(
    start: Optional[str] = Query(None, description="ISO date YYYY-MM-DD inclusive"),
    end: Optional[str] = Query(None, description="ISO date YYYY-MM-DD inclusive"),
    user=Depends(_require_referral_access),
    db=Depends(get_db),
):
    start_dt, end_dt = _parse_window(start, end)
    rows = await _dashboard_rows(db, user["clinic_id"], start_dt, end_dt)
    totals = {
        "patient_count": sum(r["patient_count"] for r in rows),
        "diagnostics_revenue": round(sum(r["diagnostics_revenue"] for r in rows), 2),
        "ha_sales_revenue": round(sum(r["ha_sales_revenue"] for r in rows), 2),
        "diagnostics_payout": round(sum(r["diagnostics_payout"] for r in rows), 2),
        "ha_payout": round(sum(r["ha_payout"] for r in rows), 2),
        "total_payout": round(sum(r["total_payout"] for r in rows), 2),
    }
    return {
        "window": {
            "start": start_dt.date().isoformat(),
            "end": end_dt.date().isoformat(),
        },
        "totals": totals,
        "rows": rows,
        "configured_by_owner_only": True,
    }


class CutConfigPayload(BaseModel):
    """Set BOTH diagnostics and HA payouts for a single doctor in one call.
    Setting `mode=None` for either category effectively disables payouts
    for that revenue stream."""
    diag_cut_mode: Optional[str] = Field(None, pattern="^(percent|flat)$")
    diag_cut_value: float = 0.0
    ha_cut_mode: Optional[str] = Field(None, pattern="^(percent|flat)$")
    ha_cut_value: float = 0.0


@router.patch("/doctors/{doctor_id}/cut-config")
async def set_cut_config(
    doctor_id: str,
    payload: CutConfigPayload,
    user=Depends(_require_referral_access),
    db=Depends(get_db),
):
    """Owner-only edit. Delegated staff get a 403 here so they can VIEW
    the dashboard but not change payout terms (those affect cheque-sized
    money and stay with the owner)."""
    if not _is_owner(user):
        raise HTTPException(
            status_code=403,
            detail="Only the clinic owner can change referral payouts.",
        )

    # Negative payouts make no sense and would create awkward "you owe
    # the clinic" rows. Clamp at 0.
    diag_v = max(0.0, float(payload.diag_cut_value or 0.0))
    ha_v = max(0.0, float(payload.ha_cut_value or 0.0))

    # When mode='percent' we additionally cap value at 100. Past 100% the
    # clinic is literally paying the doctor more than it earned, which is
    # almost always a typo.
    if payload.diag_cut_mode == "percent" and diag_v > 100:
        raise HTTPException(status_code=400, detail="Diagnostics percentage cannot exceed 100%")
    if payload.ha_cut_mode == "percent" and ha_v > 100:
        raise HTTPException(status_code=400, detail="HA-sales percentage cannot exceed 100%")

    res = await db.referring_doctors.update_one(
        {"doctor_id": doctor_id, "clinic_id": user["clinic_id"]},
        {"$set": {
            "diag_cut_mode": payload.diag_cut_mode,
            "diag_cut_value": diag_v,
            "ha_cut_mode": payload.ha_cut_mode,
            "ha_cut_value": ha_v,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }},
    )
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Referring doctor not found")
    return {"ok": True, "doctor_id": doctor_id}


@router.get("/payout-report.csv")
async def payout_report_csv(
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    report_type: str = Query("both", pattern="^(diagnostics|ha|both)$",
                              description="Which stream to include in the report"),
    user=Depends(_require_referral_access),
    db=Depends(get_db),
):
    """End-of-month-style payout CSV. The 3 report types map to three
    accounting workflows:
      • `diagnostics` — one cheque per doctor for diagnostic referrals
      • `ha`          — one cheque per doctor for HA-sale referrals
      • `both`        — consolidated single-row-per-doctor summary
    """
    start_dt, end_dt = _parse_window(start, end)
    rows = await _dashboard_rows(db, user["clinic_id"], start_dt, end_dt)
    # Drop zero-payout rows from the CSV — they're noise on the print-out.
    if report_type == "diagnostics":
        rows = [r for r in rows if r["diagnostics_payout"] > 0]
    elif report_type == "ha":
        rows = [r for r in rows if r["ha_payout"] > 0]
    else:
        rows = [r for r in rows if r["total_payout"] > 0]

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        f"AUDINEXA — Referral Payout Report ({report_type.upper()})",
        f"Window: {start_dt.date().isoformat()} → {end_dt.date().isoformat()}",
    ])
    w.writerow([])

    if report_type == "diagnostics":
        w.writerow(["Doctor", "Specialty", "Referred Patients",
                    "Diagnostics Revenue (₹)", "Cut Mode", "Cut Value",
                    "Diagnostics Payout (₹)"])
        for r in rows:
            w.writerow([r["name"], r.get("specialty") or "",
                        r["patient_count"], r["diagnostics_revenue"],
                        r.get("diag_cut_mode") or "—", r.get("diag_cut_value") or 0,
                        r["diagnostics_payout"]])
    elif report_type == "ha":
        w.writerow(["Doctor", "Specialty", "Referred Patients",
                    "HA Sales Revenue (₹)", "Cut Mode", "Cut Value",
                    "HA Payout (₹)"])
        for r in rows:
            w.writerow([r["name"], r.get("specialty") or "",
                        r["patient_count"], r["ha_sales_revenue"],
                        r.get("ha_cut_mode") or "—", r.get("ha_cut_value") or 0,
                        r["ha_payout"]])
    else:
        w.writerow(["Doctor", "Specialty", "Referred Patients",
                    "Diagnostics Revenue (₹)", "Diagnostics Payout (₹)",
                    "HA Sales Revenue (₹)", "HA Payout (₹)", "Total Payout (₹)"])
        for r in rows:
            w.writerow([r["name"], r.get("specialty") or "",
                        r["patient_count"], r["diagnostics_revenue"],
                        r["diagnostics_payout"], r["ha_sales_revenue"],
                        r["ha_payout"], r["total_payout"]])

    buf.seek(0)
    filename = f"audinexa_referral_payout_{report_type}_{start_dt.date()}_{end_dt.date()}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ═══════════════════════════════════════════════════════════════════════
# Pathway breakdown + per-doctor drill-down (Phase 2)
# ═══════════════════════════════════════════════════════════════════════

# Canonical set of referral pathways. Any patient.referral_source outside
# this set is bucketed into "Other" so the UI never gets a long tail.
KNOWN_PATHWAYS = ["Doctor", "Walk-in", "Self", "Camp", "Online", "Family", "Partner", "Other"]


def _normalize_pathway(raw: Optional[str], has_doctor: bool) -> str:
    """Fold the free-form `referral_source` column into a canonical bucket.

    A patient with a `referring_doctor_id` set always counts as `Doctor`
    even if `referral_source` is empty (very common — front desk fills the
    dropdown but skips the "source" pill)."""
    if has_doctor:
        return "Doctor"
    s = (raw or "").strip().lower()
    mapping = {
        "walk-in": "Walk-in", "walkin": "Walk-in", "walk_in": "Walk-in",
        "self": "Self", "self-referred": "Self", "self_referred": "Self",
        "camp": "Camp", "screening": "Camp",
        "online": "Online", "internet": "Online", "web": "Online",
        "family": "Family", "friend": "Family", "family/friend": "Family",
        "partner": "Partner", "corporate": "Partner",
        "doctor": "Doctor", "physician": "Doctor", "ent": "Doctor",
    }
    return mapping.get(s, "Other" if s else "Walk-in")


@router.get("/pathways")
async def pathway_breakdown(
    start: Optional[str] = Query(None, description="ISO date YYYY-MM-DD inclusive"),
    end: Optional[str] = Query(None, description="ISO date YYYY-MM-DD inclusive"),
    user=Depends(_require_referral_access),
    db=Depends(get_db),
):
    """Return counts + revenue per referral pathway for the given window.

    Pathways: Doctor · Walk-in · Self · Camp · Online · Family · Partner · Other.
    Revenue is invoice-based (paid invoices in window), grouped by the
    patient's pathway. Also emits per-pathway `patient_count` (unique
    patients seen from that source) so the UI can show "Doctors sent 24 patients"
    style copy.
    """
    start_dt, end_dt = _parse_window(start, end)
    start_iso, end_iso = start_dt.isoformat(), end_dt.isoformat()
    clinic_id = user["clinic_id"]

    # 1. Bucket every patient in the clinic by pathway.
    pathway_of: dict[str, str] = {}
    async for p in db.patients.find(
        {"clinic_id": clinic_id},
        {"_id": 0, "patient_id": 1, "referral_source": 1, "referring_doctor_id": 1, "created_at": 1},
    ):
        pid = p.get("patient_id")
        if not pid:
            continue
        pathway_of[pid] = _normalize_pathway(
            p.get("referral_source"),
            bool(p.get("referring_doctor_id")),
        )

    # 2. Init buckets.
    buckets: dict[str, dict] = {k: {"pathway": k, "patient_count": 0,
                                    "diagnostics_revenue": 0.0, "ha_sales_revenue": 0.0}
                                for k in KNOWN_PATHWAYS}
    seen_by_pathway: dict[str, set] = {k: set() for k in KNOWN_PATHWAYS}

    # 3. Walk paid invoices in window, attribute revenue.
    async for inv in db.invoices.find(
        {"clinic_id": clinic_id, "status": "paid",
         "invoice_date": {"$gte": start_iso, "$lte": end_iso}},
        {"_id": 0, "patient_id": 1, "lines": 1, "grand_total": 1, "ticket_no": 1},
    ):
        pid = inv.get("patient_id")
        pw = pathway_of.get(pid, "Walk-in")
        diag_rev = 0.0
        ha_rev = 0.0
        for ln in (inv.get("lines") or []):
            if not isinstance(ln, dict):
                continue
            amt = float(ln.get("line_total") or 0.0)
            if ln.get("product_type") == "Hearing Aid":
                ha_rev += amt
            else:
                diag_rev += amt
        if not (diag_rev or ha_rev):
            gt = float(inv.get("grand_total") or 0.0)
            if inv.get("ticket_no"):
                ha_rev += gt
            else:
                diag_rev += gt
        buckets[pw]["diagnostics_revenue"] += diag_rev
        buckets[pw]["ha_sales_revenue"] += ha_rev
        seen_by_pathway[pw].add(pid)

    # 4. Finalise counts + rounding.
    rows = []
    for pw, b in buckets.items():
        b["patient_count"] = len(seen_by_pathway[pw])
        b["diagnostics_revenue"] = round(b["diagnostics_revenue"], 2)
        b["ha_sales_revenue"] = round(b["ha_sales_revenue"], 2)
        b["total_revenue"] = round(b["diagnostics_revenue"] + b["ha_sales_revenue"], 2)
        rows.append(b)

    # Sort by total revenue desc; keep the canonical order among ties by
    # relying on Python's stable sort.
    rows.sort(key=lambda r: -r["total_revenue"])
    return {
        "window": {"start": start_dt.date().isoformat(), "end": end_dt.date().isoformat()},
        "pathways": rows,
    }


@router.get("/doctors/{doctor_id}/detail")
async def doctor_drill_down(
    doctor_id: str,
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    user=Depends(_require_referral_access),
    db=Depends(get_db),
):
    """Per-doctor drill-down for the "click a doctor" flow:

    Returns:
      • doctor:           name/specialty/contact + configured cut
      • patients:         list of referred patients with first-visit date
      • test_breakdown:   {PTA: n, Tympanometry: n, …} across all visits
      • revenue:          {diagnostics, ha_sales, total}
      • ha_fittings:      list of closed HA sales linked to referred patients
      • payout:           {diagnostics, ha, total} for the window
    """
    start_dt, end_dt = _parse_window(start, end)
    start_iso, end_iso = start_dt.isoformat(), end_dt.isoformat()
    clinic_id = user["clinic_id"]

    doctor = await db.referring_doctors.find_one(
        {"doctor_id": doctor_id, "clinic_id": clinic_id}, {"_id": 0},
    )
    if not doctor:
        raise HTTPException(status_code=404, detail="Referring doctor not found")

    # 1. Patients referred by this doctor (all-time — the doctor's book
    #    is cumulative, but revenue is windowed below).
    patients = []
    patient_ids: list[str] = []
    async for p in db.patients.find(
        {"clinic_id": clinic_id, "referring_doctor_id": doctor_id},
        {"_id": 0, "patient_id": 1, "name": 1, "mrd": 1, "created_at": 1,
         "age": 1, "gender": 1, "mobile": 1},
    ):
        patient_ids.append(p["patient_id"])
        patients.append({
            "patient_id": p["patient_id"],
            "name": p.get("name") or "",
            "mrd": p.get("mrd") or "",
            "age": p.get("age"),
            "gender": p.get("gender"),
            "mobile": p.get("mobile") or "",
            "first_visit": str(p.get("created_at") or "")[:10],
        })

    # 2. Test breakdown — count sessions per test type across all their
    #    visits in the window. Sessions can have multiple `recommended_tests`
    #    or `tests_performed`; we credit ALL performed tests.
    test_counts: dict[str, int] = {}
    session_ids: list[str] = []
    if patient_ids:
        async for s in db.test_sessions.find(
            {"clinic_id": clinic_id, "patient_id": {"$in": patient_ids},
             "test_date": {"$gte": start_dt.date().isoformat(), "$lte": end_dt.date().isoformat()}},
            {"_id": 0, "session_id": 1, "recommended_tests": 1, "tests_performed": 1},
        ):
            session_ids.append(s.get("session_id"))
            for t in (s.get("tests_performed") or s.get("recommended_tests") or []):
                if not t:
                    continue
                key = str(t).strip()
                test_counts[key] = test_counts.get(key, 0) + 1

    # 3. Revenue + payout (reuse the existing rollup logic for consistency).
    dashboard_rows = await _dashboard_rows(db, clinic_id, start_dt, end_dt)
    row = next((r for r in dashboard_rows if r["doctor_id"] == doctor_id), None)
    if row is None:
        row = {
            "patient_count": 0, "diagnostics_revenue": 0.0, "ha_sales_revenue": 0.0,
            "diagnostics_payout": 0.0, "ha_payout": 0.0, "total_payout": 0.0,
            "per_patient_diag": {}, "per_patient_ha": {},
        }

    # Enrich the patients list with per-patient billing (the user's ask:
    # "when I click on a doctor's name I should see each referred
    # patient and their billing towards Diagnostics & Hearing aids").
    # Only counts PAID invoices in the window and honours the HA-sale
    # blacklist — same logic as the aggregate payout.
    per_p_diag = row.get("per_patient_diag") or {}
    per_p_ha = row.get("per_patient_ha") or {}
    for p in patients:
        pid = p["patient_id"]
        p["diag_revenue"] = round(float(per_p_diag.get(pid, 0.0)), 2)
        p["ha_revenue"] = round(float(per_p_ha.get(pid, 0.0)), 2)
        p["total_revenue"] = round(p["diag_revenue"] + p["ha_revenue"], 2)
    # Sort patients: contributors first (higher revenue first), then
    # zero-revenue "referred but hasn't bought yet" rows alphabetically.
    patients.sort(key=lambda pp: (-(pp.get("total_revenue") or 0), (pp.get("name") or "").lower()))

    # 4. HA fittings — closed sales in window from this doctor's referrals.
    ha_fittings = []
    if patient_ids:
        async for sale in db.ha_sales.find(
            {"clinic_id": clinic_id, "patient_id": {"$in": patient_ids},
             "status": {"$nin": ["trial", "cancelled", "returned"]}},
            {"_id": 0, "sale_id": 1, "fitting_id": 1, "patient_id": 1,
             "product_name": 1, "brand": 1, "model": 1, "amount": 1,
             "total_amount": 1, "grand_total": 1, "status": 1, "delivered_at": 1,
             "created_at": 1},
        ):
            amt = sale.get("grand_total") or sale.get("total_amount") or sale.get("amount") or 0
            ha_fittings.append({
                "sale_id":    sale.get("sale_id") or sale.get("fitting_id"),
                "patient_id": sale.get("patient_id"),
                "product":    (sale.get("product_name")
                               or f"{sale.get('brand', '') or ''} {sale.get('model', '') or ''}".strip()
                               or "—"),
                "amount":     float(amt or 0),
                "status":     sale.get("status") or "—",
                "date":       str(sale.get("delivered_at") or sale.get("created_at") or "")[:10],
            })

    return {
        "window": {"start": start_dt.date().isoformat(), "end": end_dt.date().isoformat()},
        "doctor": {
            "doctor_id":     doctor["doctor_id"],
            "name":          doctor.get("name"),
            "specialty":     doctor.get("specialty"),
            "clinic":        doctor.get("clinic"),
            "phone":         doctor.get("phone"),
            "email":         doctor.get("email"),
            "diag_cut_mode": doctor.get("diag_cut_mode"),
            "diag_cut_value": float(doctor.get("diag_cut_value") or 0),
            "ha_cut_mode":   doctor.get("ha_cut_mode"),
            "ha_cut_value":  float(doctor.get("ha_cut_value") or 0),
        },
        "patients":       patients,
        "patient_total":  len(patients),
        "test_breakdown": [{"test": k, "count": v} for k, v
                           in sorted(test_counts.items(), key=lambda kv: -kv[1])],
        "revenue": {
            "diagnostics": row["diagnostics_revenue"],
            "ha_sales":    row["ha_sales_revenue"],
            "total":       round(row["diagnostics_revenue"] + row["ha_sales_revenue"], 2),
        },
        "ha_fittings":    ha_fittings,
        "payout": {
            "diagnostics": row["diagnostics_payout"],
            "ha":          row["ha_payout"],
            "total":       row["total_payout"],
        },
    }
