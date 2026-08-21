"""AUDINEXA Super-Admin Panel — Phase 14A.

Internal founder/super-admin command centre. Aggregates across every tenant.

Modules covered in this phase:
  1. Dashboard     — cross-tenant KPIs + charts
  2. Tenants       — enriched tenant table + detail + actions (suspend/impersonate/delete)
  3. Subscriptions — plan catalogue CRUD + upgrade/downgrade + manual invoices
  4. Revenue       — platform-wide revenue roll-up + invoice ledger
  5. Leads/Trials  — pipeline built on waitlist_signups + trial clinics
  6. FeatureFlags  — per-tenant additive module toggles on top of tier

Role gating:
  * `founder`       — full access including delete-tenant
  * `super_admin`   — everything except delete-tenant
  * All endpoints return 403 for anyone else.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import uuid
from datetime import datetime, timezone, timedelta, date
from typing import List, Optional, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from auth import (
    get_current_user, hash_password, create_access_token,
    VALID_ROLES,
)
from utils.hot_cache import cached, invalidate as _cache_invalidate


def _invalidate_dashboard_cache():
    """Drop dashboard + tenants + leads cache entries — called by any
    mutation that changes platform-level KPIs (tenant create/suspend/
    activate/delete, lead convert, plan change, payment status flip).

    Cheap: scans the in-process cache map (≤1024 entries) for matching
    prefixes; typical eviction count is 1-5 entries.
    """
    _cache_invalidate("dashboard:")
    _cache_invalidate("tenants:")
    _cache_invalidate("leads:")
from database import get_db
from utils.serde import serialize_datetime, deserialize_datetime

log = logging.getLogger("audinexa.admin_panel")
from utils.tiers import (
    TIER_ORDER, TIER_MODULES, get_tier_prices,
    resolve_effective_tier, has_module_access,
)
from utils.rbac import require_permission


router = APIRouter(prefix="/api/admin/v2")


# ==================== HELPERS ====================

def _is_founder(user: dict) -> bool:
    return user.get("role") == "founder"


# Role gating for all routes is done via utils.rbac.require_permission(...)
# using the shared ROLE_PERMISSIONS matrix (Phase 14C).


async def _log_audit(db, user: dict, action: str, target: str, before: dict | None = None, after: dict | None = None, request: Request | None = None):
    """Append-only audit log for admin actions."""
    await db.admin_audit_logs.insert_one(serialize_datetime({
        "log_id": f"AUD-{uuid.uuid4().hex[:10].upper()}",
        "actor_user_id": user["user_id"],
        "actor_email": user.get("email"),
        "actor_role": user.get("role"),
        "action": action,
        "target": target,
        "before": before or {},
        "after": after or {},
        "ip": (request.client.host if request and request.client else None),
        "at": datetime.now(timezone.utc),
    }))


# ==================== 1. DASHBOARD ====================

@router.get("/dashboard")
async def dashboard(
    user=Depends(require_permission("dashboard:read")),
    db=Depends(get_db),
):
    """Founder dashboard KPI tile + charts.

    **Cached** for 30s — these are platform-wide aggregations that are
    expensive to recompute on every poll (founder dashboard polls every
    15s). Cache key is shared across all founder/super_admin viewers
    because the response is platform-wide, not user-scoped. Cache is
    automatically invalidated when a tenant is created/updated/deleted
    (see `_invalidate_dashboard_cache()` in the mutation handlers).
    """
    return await cached(
        key="dashboard:v1",
        factory=lambda: _compute_dashboard(db),
        ttl_seconds=30,
    )


async def _compute_dashboard(db) -> dict:
    """Pure compute fn for the dashboard payload. Pulled out of the route
    handler so it can be invoked behind the `cached()` wrapper without
    threading FastAPI internals through the cache layer.
    """
    now = datetime.now(timezone.utc)
    month_ago = (now - timedelta(days=30)).isoformat()
    months_ago_12 = (now - timedelta(days=365)).isoformat()

    # ---- KPI counts ----
    total_clinics = await db.clinics.count_documents({})
    # Trial vs paid via resolve_effective_tier (consult trial_ends_at)
    trials = 0
    active = 0
    suspended = 0
    plan_dist: dict[str, int] = {"BASIC": 0, "STANDARD": 0, "PREMIUM": 0}
    tier_revenue: dict[str, float] = {"BASIC": 0, "STANDARD": 0, "PREMIUM": 0}
    prices = get_tier_prices()
    async for c in db.clinics.find({}, {"_id": 0, "subscription_tier": 1, "trial_ends_at": 1, "status": 1}):
        if c.get("status") == "suspended":
            suspended += 1
            continue
        t = await resolve_effective_tier(c)
        plan_dist[t] = plan_dist.get(t, 0) + 1
        if c.get("trial_ends_at") and c.get("subscription_tier", "BASIC") == "BASIC":
            # Still on trial
            trials += 1
        else:
            active += 1
        # Estimated annual MRR-equivalent per tier
        tier_revenue[t] = tier_revenue.get(t, 0) + prices[t]["annual"] / 12.0

    mrr = round(sum(tier_revenue.values()), 2)
    arr = round(mrr * 12, 2)
    avg_per_tenant = round(mrr / max(total_clinics, 1), 2)

    # ---- New signups this month ----
    new_signups_30d = await db.clinics.count_documents({"created_at": {"$gte": month_ago}})

    # ---- Churn proxy (clinics auto-downgraded from trial in last 30d) ----
    # NB: the trial-expiry cron stamps `trial_expired_at` (ISO string) — an
    # earlier version of this code looked for a `tier_updated_at` field that
    # nothing writes, so churn was permanently 0. Migrated to the real stamp
    # during the 2026-07-25 launch-readiness audit.
    churned = await db.clinics.count_documents({
        "tier_auto_downgraded_from_trial": True,
        "trial_expired_at": {"$gte": month_ago},
    })
    churn_rate = round(100 * churned / max(active + trials, 1), 1)

    # ---- Payment failures (placeholder — reads tenant_invoices collection) ----
    payment_failures = await db.tenant_invoices.count_documents({"status": "failed"})

    # ---- 12-month MRR growth chart ----
    mrr_series = []
    async for row in db.clinics.aggregate([
        {"$match": {"created_at": {"$gte": months_ago_12}}},
        {"$project": {
            "ts": {"$dateFromString": {"dateString": "$created_at", "onError": None}},
            "tier": {"$ifNull": ["$subscription_tier", "BASIC"]},
        }},
        {"$match": {"ts": {"$ne": None}}},
        {"$project": {
            "tier": 1,
            "bucket": {"$dateToString": {"date": "$ts", "format": "%Y-%m", "timezone": "Asia/Kolkata"}},
        }},
        {"$group": {"_id": {"month": "$bucket", "tier": "$tier"}, "n": {"$sum": 1}}},
        {"$sort": {"_id.month": 1}},
    ]):
        mrr_series.append(row)

    # Roll up into month->cumulative MRR
    monthly: dict[str, dict] = {}
    running = {"BASIC": 0, "STANDARD": 0, "PREMIUM": 0}
    for row in mrr_series:
        m = row["_id"]["month"]
        t = row["_id"]["tier"]
        if t not in running:
            running[t] = 0
        running[t] += row["n"]
        monthly.setdefault(m, {})
        monthly[m] = {
            "month": m,
            "basic": running.get("BASIC", 0),
            "standard": running.get("STANDARD", 0),
            "premium": running.get("PREMIUM", 0),
            "mrr": round(sum(running.get(k, 0) * prices[k]["annual"] / 12.0 for k in ("BASIC", "STANDARD", "PREMIUM")), 2),
        }
    mrr_chart = sorted(monthly.values(), key=lambda x: x["month"])

    # ---- New signups trend (last 30d, daily) ----
    signups_trend = []
    async for row in db.clinics.aggregate([
        {"$match": {"created_at": {"$gte": month_ago}}},
        {"$project": {
            "ts": {"$dateFromString": {"dateString": "$created_at", "onError": None}},
        }},
        {"$match": {"ts": {"$ne": None}}},
        {"$project": {
            "day": {"$dateToString": {"date": "$ts", "format": "%Y-%m-%d", "timezone": "Asia/Kolkata"}},
        }},
        {"$group": {"_id": "$day", "n": {"$sum": 1}}},
        {"$sort": {"_id": 1}},
    ]):
        signups_trend.append({"day": row["_id"], "count": row["n"]})

    # ---- Recent signups (last 10) ----
    recent = await db.clinics.find({}, {"_id": 0, "clinic_id": 1, "name": 1, "city": 1, "subscription_tier": 1, "trial_ends_at": 1, "created_at": 1})\
        .sort("created_at", -1).limit(10).to_list(10)
    recent_signups = [deserialize_datetime(r) for r in recent]

    # ---- Renewals due (trial ends in next 14 days) ----
    cutoff = (now + timedelta(days=14)).isoformat()
    now_iso = now.isoformat()
    renewals = await db.clinics.find(
        {"trial_ends_at": {"$gte": now_iso, "$lte": cutoff}},
        {"_id": 0, "clinic_id": 1, "name": 1, "city": 1, "trial_ends_at": 1, "email": 1, "phone": 1},
    ).sort("trial_ends_at", 1).limit(25).to_list(25)

    # ---- Conversion funnel (leads → trial → paid) ----
    # `trials` = clinics with a currently ACTIVE trial (trial_ends_at still set
    # after the nightly expiry cron `$unset`s expired ones). `paid` = anyone
    # on STANDARD or PREMIUM. `all_ever_trialed` = paid + churned + still-trialing
    # (best proxy we have for "trials that ever started" without a separate
    # audit collection). This makes `trial_to_paid_pct` a meaningful ratio
    # instead of divide-by-a-post-migration-zero.
    waitlist_count = await db.waitlist_signups.count_documents({})
    trial_count = await db.clinics.count_documents({"trial_ends_at": {"$exists": True}})
    paid_count = await db.clinics.count_documents({"subscription_tier": {"$in": ["STANDARD", "PREMIUM"]}})
    ever_trialed = paid_count + churned + trial_count

    # ---- 30-day signup funnel (signups → verified → activated) ----
    # Different from `funnel` above: that's a lifetime lead-to-paid conversion.
    # This one is the last-30-days ONBOARDING funnel — the fastest way to
    # spot silent-drop incidents (email verify blocked) or activation drops
    # (users signed up + verified but never actually used the product).
    #
    # Definitions:
    #   signups   = clinic docs created in the last 30 days
    #   verified  = of those, the ones whose owner user has email_verified=True
    #   activated = of those, the ones with ≥1 patient in the DB
    signup_ids = [
        c["clinic_id"]
        async for c in db.clinics.find(
            {"created_at": {"$gte": month_ago}},
            {"_id": 0, "clinic_id": 1},
        )
    ]
    signups_30 = len(signup_ids)
    if signup_ids:
        verified_30 = await db.users.count_documents({
            "clinic_id":       {"$in": signup_ids},
            "role":            {"$in": ["clinic_owner", "founder"]},
            "email_verified":  True,
        })
        # `distinct` returns the unique clinic_ids that have at least one
        # patient — cheap and correct at any scale.
        activated_ids = await db.patients.distinct(
            "clinic_id", {"clinic_id": {"$in": signup_ids}}
        )
        activated_30 = len(activated_ids)
    else:
        verified_30 = 0
        activated_30 = 0

    verify_rate = round(100 * verified_30 / max(signups_30, 1), 1)
    activation_rate = round(100 * activated_30 / max(signups_30, 1), 1)
    verified_to_activated = round(100 * activated_30 / max(verified_30, 1), 1)

    return {
        "kpis": {
            "active_clinics": active,
            "trial_accounts": trials,
            "suspended": suspended,
            "total_tenants": total_clinics,
            "mrr": mrr,
            "arr": arr,
            "new_signups_30d": new_signups_30d,
            "churn_rate_pct": churn_rate,
            "payment_failures": payment_failures,
            "avg_revenue_per_tenant": avg_per_tenant,
        },
        "plan_distribution": [{"tier": t, "count": plan_dist.get(t, 0)} for t in TIER_ORDER],
        "revenue_by_tier": [{"tier": t, "revenue": round(tier_revenue.get(t, 0), 2)} for t in TIER_ORDER],
        "mrr_chart": mrr_chart,
        "signups_trend": signups_trend,
        "funnel": {
            "leads": waitlist_count,
            "trials": trial_count,
            "paid": paid_count,
            "churned_30d": churned,
            "trial_to_paid_pct": round(100 * paid_count / max(ever_trialed, 1), 1),
        },
        "signup_funnel_30d": {
            "signups":               signups_30,
            "verified":              verified_30,
            "activated":             activated_30,
            "verify_rate_pct":       verify_rate,
            "activation_rate_pct":   activation_rate,
            "verified_to_activated_pct": verified_to_activated,
            "signup_to_verify_drop": max(signups_30 - verified_30, 0),
            "verify_to_activate_drop": max(verified_30 - activated_30, 0),
        },
        "recent_signups": recent_signups,
        "renewals_due": [deserialize_datetime(r) for r in renewals],
    }


# ==================== 2. TENANTS ====================

class TenantUpdate(BaseModel):
    name: Optional[str] = None
    city: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    status: Optional[Literal["active", "suspended"]] = None
    subscription_tier: Optional[str] = None


# ==================== 1b. LIVE SIGNUP FEED ====================
#
# Founder dashboard polls this every ~20s for a real-time "launch pulse"
# toast whenever a fresh clinic signs up via /api/public/clinic-signup.
# Uncached (would defeat the purpose) but ultra-cheap: single indexed
# query on `clinics.created_at`, projected to 5 fields, capped at 20
# rows. `since` is an ISO string (matches how we store `created_at`)
# to avoid the exact string-vs-date BSON bug we just fixed elsewhere.

@router.get("/signups/recent")
async def recent_signups(
    since: Optional[str] = Query(None, description="ISO timestamp — only return signups after this"),
    limit: int = Query(20, ge=1, le=50),
    user=Depends(require_permission("dashboard:read")),
    db=Depends(get_db),
):
    """Return clinics created after `since` (ISO string). If `since` is
    omitted, returns the most recent `limit` signups. Powers the founder
    dashboard live-feed toast.
    """
    query: dict = {}
    if since:
        query["created_at"] = {"$gt": since}
    cursor = (
        db.clinics.find(
            query,
            {
                "_id": 0,
                "clinic_id": 1,
                "name": 1,
                "city": 1,
                "country": 1,
                "subscription_tier": 1,
                "created_at": 1,
                "trial_ends_at": 1,
            },
        )
        .sort("created_at", -1)
        .limit(limit)
    )
    rows = [c async for c in cursor]
    now_iso = datetime.now(timezone.utc).isoformat()
    return {
        "count": len(rows),
        "server_now": now_iso,
        "rows": rows,
    }




@router.get("/tenants")
async def list_tenants(
    status: Optional[str] = None,
    tier: Optional[str] = None,
    country: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 500,
    user=Depends(require_permission("tenants:read")),
    db=Depends(get_db),
):
    """Enriched tenants list. **Cached 30s** per unique filter combo.
    Founder console polls this every few seconds when filtering; the
    cache makes repeat filter clicks instant. Invalidated on tenant
    create/update/delete."""
    # Cache key includes every filter dimension. Note: filter values are
    # already typed (Optional[str]), no injection risk via the key.
    key = f"tenants:v1:{status}:{tier}:{country}:{q}:{limit}"
    return await cached(
        key=key,
        factory=lambda: _compute_list_tenants(status, tier, country, q, limit, db),
        ttl_seconds=30,
    )


async def _compute_list_tenants(status, tier, country, q, limit, db):
    """Pure-compute backing function for the cached `/tenants` endpoint.
    Identical shape to the previous handler body."""
    query: dict = {}
    if status:
        query["status"] = status
    if tier:
        query["subscription_tier"] = tier
    if country:
        query["country"] = country
    if q:
        query["$or"] = [
            {"name": {"$regex": q, "$options": "i"}},
            {"clinic_id": {"$regex": q, "$options": "i"}},
            {"email": {"$regex": q, "$options": "i"}},
            {"city": {"$regex": q, "$options": "i"}},
        ]
    rows = await db.clinics.find(query, {"_id": 0}).sort("created_at", -1).to_list(limit)

    # Enrich each with user counts, branches, last login, health score
    out = []
    for c in rows:
        cid = c["clinic_id"]
        eff = await resolve_effective_tier(c)
        users_n = await db.users.count_documents({"clinic_id": cid, "active": True})
        branches_n = await db.branches.count_documents({"clinic_id": cid, "active": True})
        patients_n = await db.patients.count_documents({"clinic_id": cid})
        # Last login = most recent tokens.issued_at OR user-level field (we don't track yet; approximate via token activity)
        last_tok = await db.tokens.find_one({"clinic_id": cid}, {"_id": 0, "issued_at": 1}, sort=[("issued_at", -1)])
        last_activity = last_tok.get("issued_at") if last_tok else None
        # Health score 0-100
        tier_cap = {"BASIC": 50, "STANDARD": 150, "PREMIUM": 1000}.get(eff, 50)
        util = min(100, int(100 * patients_n / max(tier_cap, 1)))
        owner = await db.users.find_one({"clinic_id": cid, "role": {"$in": ["clinic_owner", "super_admin"]}}, {"_id": 0, "name": 1, "email": 1})
        out.append({
            **deserialize_datetime(c),
            "effective_tier": eff,
            "users_count": users_n,
            "branches_count": branches_n,
            "patients_count": patients_n,
            "last_activity_at": last_activity,
            "owner_name": (owner or {}).get("name"),
            "owner_email": (owner or {}).get("email"),
            "health_score": util,
        })
    return {"count": len(out), "rows": out}


@router.get("/tenants/{clinic_id}")
async def tenant_detail(
    clinic_id: str,
    user=Depends(require_permission("tenants:read")),
    db=Depends(get_db),
):
    c = await db.clinics.find_one({"clinic_id": clinic_id}, {"_id": 0})
    if not c:
        raise HTTPException(404, detail="Tenant not found")
    eff = await resolve_effective_tier(c)
    users = await db.users.find({"clinic_id": clinic_id}, {"_id": 0, "password_hash": 0}).to_list(200)
    branches = await db.branches.find({"clinic_id": clinic_id}, {"_id": 0}).to_list(50)

    # Usage metrics
    patients_n = await db.patients.count_documents({"clinic_id": clinic_id})
    sessions_n = await db.test_sessions.count_documents({"clinic_id": clinic_id})
    invoices_n = await db.invoices.count_documents({"clinic_id": clinic_id})
    ha_sales_n = await db.ha_sales.count_documents({"clinic_id": clinic_id})
    tickets_n = await db.service_tickets.count_documents({"clinic_id": clinic_id})

    # Billing (admin-panel invoices — mock/manual)
    tenant_invoices = await db.tenant_invoices.find({"clinic_id": clinic_id}, {"_id": 0}).sort("issued_at", -1).to_list(50)

    # Feature flags — enriched payload so the embedded `<FeatureFlagsEditor>`
    # in the Founder tenant-detail page renders without a second round-trip.
    # Must match the shape returned by GET /admin/v2/feature-flags/{clinic_id}
    # (the editor reads base_modules + available_modules and crashes if either
    # is undefined).
    flags_doc_raw = await db.tenant_feature_flags.find_one(
        {"clinic_id": clinic_id}, {"_id": 0},
    ) or {"clinic_id": clinic_id, "extra_modules": [], "disabled_modules": []}
    base_mods = set(TIER_MODULES.get(eff, []))
    extra_set = set(flags_doc_raw.get("extra_modules", []))
    disabled_set = set(flags_doc_raw.get("disabled_modules", []))
    effective_modules = sorted((base_mods | extra_set) - disabled_set)
    flags_doc = {
        **flags_doc_raw,
        "tier": eff,
        "base_modules": sorted(base_mods),
        "available_modules": AVAILABLE_MODULES,
        "effective_modules": effective_modules,
    }

    # Audit trail
    audit = await db.admin_audit_logs.find(
        {"$or": [{"target": clinic_id}, {"target": {"$regex": f"^{clinic_id}:"}}]},
        {"_id": 0},
    ).sort("at", -1).limit(50).to_list(50)

    return {
        "tenant": {**deserialize_datetime(c), "effective_tier": eff},
        "users": [deserialize_datetime(u) for u in users],
        "branches": [deserialize_datetime(b) for b in branches],
        "usage": {
            "patients": patients_n, "test_sessions": sessions_n,
            "invoices": invoices_n, "ha_sales": ha_sales_n, "service_tickets": tickets_n,
        },
        "invoices": [deserialize_datetime(i) for i in tenant_invoices],
        "feature_flags": deserialize_datetime(flags_doc),
        "audit_trail": [deserialize_datetime(a) for a in audit],
    }


@router.patch("/tenants/{clinic_id}")
async def update_tenant(
    clinic_id: str,
    payload: TenantUpdate,
    request: Request,
    user=Depends(require_permission("tenants:write")),
    db=Depends(get_db),
):
    existing = await db.clinics.find_one({"clinic_id": clinic_id}, {"_id": 0})
    if not existing:
        raise HTTPException(404, detail="Tenant not found")
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(400, detail="No fields to update")
    if "subscription_tier" in updates and updates["subscription_tier"] not in TIER_ORDER:
        raise HTTPException(400, detail=f"tier must be one of {TIER_ORDER}")
    updates["updated_at"] = datetime.now(timezone.utc)
    await db.clinics.update_one({"clinic_id": clinic_id}, {"$set": serialize_datetime(updates)})
    await _log_audit(db, user, "tenant.update", clinic_id, before={k: existing.get(k) for k in updates}, after=updates, request=request)
    _invalidate_dashboard_cache()
    return {"ok": True}


@router.post("/tenants/{clinic_id}/suspend")
async def suspend_tenant(
    clinic_id: str, request: Request,
    user=Depends(require_permission("tenants:write")),
    db=Depends(get_db),
):
    await db.clinics.update_one({"clinic_id": clinic_id}, {"$set": {"status": "suspended", "suspended_at": datetime.now(timezone.utc).isoformat()}})
    await db.users.update_many({"clinic_id": clinic_id}, {"$set": {"active": False}})
    await _log_audit(db, user, "tenant.suspend", clinic_id, request=request)
    _invalidate_dashboard_cache()
    return {"ok": True, "clinic_id": clinic_id, "status": "suspended"}


@router.post("/tenants/{clinic_id}/activate")
async def activate_tenant(
    clinic_id: str, request: Request,
    user=Depends(require_permission("tenants:write")),
    db=Depends(get_db),
):
    await db.clinics.update_one({"clinic_id": clinic_id}, {"$set": {"status": "active"}, "$unset": {"suspended_at": ""}})
    await db.users.update_many({"clinic_id": clinic_id}, {"$set": {"active": True}})
    await _log_audit(db, user, "tenant.activate", clinic_id, request=request)
    _invalidate_dashboard_cache()
    return {"ok": True, "clinic_id": clinic_id, "status": "active"}


@router.post("/tenants/{clinic_id}/impersonate")
async def impersonate_tenant(
    clinic_id: str, request: Request,
    user=Depends(require_permission("tenants:impersonate")),
    db=Depends(get_db),
):
    """Mint a short-lived JWT as the tenant's clinic_owner for support / debugging.
    Impersonator identity recorded in audit trail.
    """
    owner = await db.users.find_one(
        {"clinic_id": clinic_id, "role": {"$in": ["clinic_owner", "super_admin"]}, "active": True},
        {"_id": 0},
    )
    if not owner:
        # fallback: any admin-ish active user in the clinic
        owner = await db.users.find_one({"clinic_id": clinic_id, "active": True}, {"_id": 0})
    if not owner:
        raise HTTPException(404, detail="No owner found in tenant")
    token = create_access_token(owner["user_id"], owner["email"], owner["role"], clinic_id)
    await _log_audit(db, user, "tenant.impersonate", clinic_id, after={"impersonated_user": owner["email"]}, request=request)
    return {"access_token": token, "token_type": "bearer", "as_user": owner["email"], "role": owner["role"]}


@router.delete("/tenants/{clinic_id}")
async def delete_tenant(
    clinic_id: str, request: Request,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """FOUNDER-ONLY. Hard-deletes a tenant + all its data. Not reversible."""
    if not _is_founder(user):
        raise HTTPException(status_code=403, detail="Only the founder can delete a tenant")
    if clinic_id in {"clinic-acs-demo"}:
        raise HTTPException(status_code=400, detail="Cannot delete the primary demo clinic")
    deleted = await _purge_tenant(db, clinic_id)
    await _log_audit(db, user, "tenant.delete", clinic_id, before={"deleted_counts": deleted}, request=request)
    _invalidate_dashboard_cache()
    return {"ok": True, "clinic_id": clinic_id, "deleted": deleted}


_TENANT_PURGE_COLLECTIONS = [
    "clinics", "users", "branches", "patients", "test_sessions",
    "invoices", "payments", "services", "tokens", "appointments",
    "ha_products", "ha_sales", "ha_fittings", "ha_trials", "quotations",
    "service_tickets", "ha_loaners", "ha_trade_ins", "ha_followups",
    "ha_subscriptions", "ha_amc_plans", "ha_amc_contracts",
    "referral_partners", "partner_payouts", "tenant_feature_flags",
    "tenant_invoices", "closeouts", "waitlist_signups",
    "patient_otps", "patient_appointment_requests", "patient_feedback",
    "service_estimates", "service_couriers", "service_approvals",
    "report_deliveries",
]


async def _purge_tenant(db, clinic_id: str) -> dict:
    """Hard-delete every document scoped to `clinic_id` across all tenant
    collections. Returns per-collection deleted counts."""
    deleted: dict = {}
    for coll in _TENANT_PURGE_COLLECTIONS:
        r = await db[coll].delete_many({"clinic_id": clinic_id})
        deleted[coll] = r.deleted_count
    return deleted


class BulkTenantIds(BaseModel):
    clinic_ids: list[str] = Field(min_length=1, max_length=50)


@router.post("/tenants/bulk-delete")
async def bulk_delete_tenants(
    payload: BulkTenantIds, request: Request,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """FOUNDER-ONLY. Hard-deletes multiple tenants in one call. Skips (rather
    than aborts) protected/missing clinics so a mixed batch still processes
    the safe ones. Returns per-clinic status."""
    if not _is_founder(user):
        raise HTTPException(status_code=403, detail="Only the founder can delete tenants")
    processed: list[dict] = []
    skipped: list[dict] = []
    for cid in payload.clinic_ids:
        if cid in {"clinic-acs-demo", "audinexa-platform"}:
            skipped.append({"clinic_id": cid, "reason": "protected"})
            continue
        exists = await db.clinics.find_one({"clinic_id": cid}, {"_id": 0, "clinic_id": 1})
        if not exists:
            skipped.append({"clinic_id": cid, "reason": "not_found"})
            continue
        try:
            deleted = await _purge_tenant(db, cid)
            processed.append({"clinic_id": cid, "deleted_counts": deleted})
        except Exception as e:  # noqa: BLE001 — keep the batch going
            skipped.append({"clinic_id": cid, "reason": f"error: {str(e)[:80]}"})
    await _log_audit(
        db, user, "tenant.bulk_delete",
        ",".join(payload.clinic_ids[:5]),
        after={"processed": len(processed), "skipped": len(skipped)},
        request=request,
    )
    _invalidate_dashboard_cache()
    return {
        "ok": True,
        "processed": processed,
        "skipped": skipped,
        "counts": {"processed": len(processed), "skipped": len(skipped)},
    }


# ==================== FOUNDER RESET — wipe non-paying data ==================
# One-shot endpoint used to fresh-start the platform: purges leads + all
# non-paying tenants + tenant invoices. NEVER touches:
#   • The `audinexa-platform` clinic (your platform tenant, holds founder+staff)
#   • The `clinic-acs-demo` primary demo clinic
#   • Any clinic where subscription_status == "active" (real paying customers)
#   • The audit_log itself (the wipe operation is itself logged)
# Requires the caller to type the exact confirmation phrase in the body so
# nobody triggers it by mistake.

CONFIRM_PHRASE_FOUNDER_RESET = "WIPE-EVERYTHING-EXCEPT-PLATFORM"


class FounderResetPayload(BaseModel):
    confirm: str = Field(description=f"Must equal: {CONFIRM_PHRASE_FOUNDER_RESET}")
    dry_run: bool = Field(default=False, description="If true, count what WOULD be deleted without deleting.")


@router.post("/founder/reset")
async def founder_reset(
    payload: FounderResetPayload, request: Request,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    if not _is_founder(user):
        raise HTTPException(status_code=403, detail="Only the founder can trigger a platform reset")
    if payload.confirm != CONFIRM_PHRASE_FOUNDER_RESET:
        raise HTTPException(
            status_code=400,
            detail=f"Confirmation phrase mismatch. Send confirm='{CONFIRM_PHRASE_FOUNDER_RESET}'.",
        )

    # ---- Build the "to delete" tenant list -----------------------------------
    preserve_ids = {"audinexa-platform", "clinic-acs-demo"}
    paying_cursor = db.clinics.find({"subscription_status": "active"}, {"_id": 0, "clinic_id": 1})
    async for c in paying_cursor:
        preserve_ids.add(c["clinic_id"])

    all_ids = [c["clinic_id"] async for c in db.clinics.find({}, {"_id": 0, "clinic_id": 1})]
    to_delete_ids = [cid for cid in all_ids if cid not in preserve_ids]

    # ---- Count what WILL disappear ------------------------------------------
    leads_count = await db.waitlist_signups.count_documents({})
    invoices_count = await db.tenant_invoices.count_documents({})

    summary_before = {
        "leads": leads_count,
        "tenant_invoices": invoices_count,
        "clinics_total": len(all_ids),
        "clinics_preserved": len(preserve_ids),
        "clinics_to_delete": len(to_delete_ids),
        "preserved_clinic_ids": sorted(preserve_ids),
    }

    if payload.dry_run:
        # Show the full preserved list with human-readable details, plus a
        # sample of the delete list so the founder can eyeball for real
        # customers that haven't been tagged with subscription_status yet.
        preserved_detail = []
        cur = db.clinics.find(
            {"clinic_id": {"$in": list(preserve_ids)}},
            {"_id": 0, "clinic_id": 1, "name": 1, "owner_email": 1,
             "subscription_status": 1, "subscription_tier": 1, "created_at": 1},
        )
        async for c in cur:
            preserved_detail.append({
                "clinic_id": c.get("clinic_id"),
                "name": c.get("name"),
                "owner_email": c.get("owner_email"),
                "subscription_status": c.get("subscription_status", "—"),
                "subscription_tier": c.get("subscription_tier", "—"),
                "reason": (
                    "platform"       if c.get("clinic_id") == "audinexa-platform"
                    else "demo"      if c.get("clinic_id") == "clinic-acs-demo"
                    else "paying"    if c.get("subscription_status") == "active"
                    else "unknown"
                ),
            })

        sample_to_delete = []
        cur = db.clinics.find(
            {"clinic_id": {"$in": to_delete_ids[:30]}},
            {"_id": 0, "clinic_id": 1, "name": 1, "owner_email": 1,
             "subscription_status": 1, "subscription_tier": 1, "created_at": 1},
        )
        async for c in cur:
            sample_to_delete.append({
                "clinic_id": c.get("clinic_id"),
                "name": c.get("name"),
                "owner_email": c.get("owner_email"),
                "subscription_status": c.get("subscription_status", "—"),
                "subscription_tier": c.get("subscription_tier", "—"),
            })

        # Health check on the paying-customer tag — if this comes back 0 on
        # a production DB with real customers, the founder must tag them
        # BEFORE running the wipe.
        subscription_status_stats = {
            "active":   await db.clinics.count_documents({"subscription_status": "active"}),
            "trial":    await db.clinics.count_documents({"subscription_status": "trial"}),
            "cancelled":await db.clinics.count_documents({"subscription_status": "cancelled"}),
            "missing":  await db.clinics.count_documents({"subscription_status": {"$exists": False}}),
        }

        return {
            "ok": True,
            "dry_run": True,
            "would_delete": summary_before,
            "preserved_clinics": preserved_detail,
            "sample_clinics_to_delete": sample_to_delete,
            "subscription_status_distribution": subscription_status_stats,
            "hint": (
                "Scan `sample_clinics_to_delete` for any real customer. "
                "If you spot one, cancel the wipe and set that clinic's "
                "`subscription_status` to 'active' first — then re-run dry_run."
            ),
        }

    # ---- Execute the wipe ---------------------------------------------------
    # 1. Leads (waitlist_signups) — full wipe
    leads_r = await db.waitlist_signups.delete_many({})
    # 2. Tenant invoices — full wipe (revenue chart resets to zero)
    invoices_r = await db.tenant_invoices.delete_many({})
    # 3. Non-preserved clinics — purge each with the full 33-collection sweep
    per_clinic = []
    for cid in to_delete_ids:
        try:
            deleted = await _purge_tenant(db, cid)
            per_clinic.append({"clinic_id": cid, "docs": sum(deleted.values())})
        except Exception as e:  # noqa: BLE001 — keep going
            per_clinic.append({"clinic_id": cid, "error": str(e)[:120]})
    # 4. Orphan users — belong to a clinic that no longer exists (either
    #    already purged or was never created). Safe to reap.
    remaining_clinic_ids = [c["clinic_id"] async for c in db.clinics.find({}, {"_id": 0, "clinic_id": 1})]
    orphans_r = await db.users.delete_many({
        "clinic_id": {"$nin": remaining_clinic_ids},
    })

    result = {
        "ok": True,
        "wiped": {
            "leads": leads_r.deleted_count,
            "tenant_invoices": invoices_r.deleted_count,
            "clinics_deleted": sum(1 for row in per_clinic if "error" not in row),
            "clinics_failed": sum(1 for row in per_clinic if "error" in row),
            "orphan_users_reaped": orphans_r.deleted_count,
        },
        "preserved_clinic_ids": sorted(preserve_ids),
        "per_clinic": per_clinic,
    }

    await _log_audit(
        db, user, "founder.reset", "*",
        before=summary_before,
        after=result["wiped"],
        request=request,
    )
    _invalidate_dashboard_cache()
    return result



# ==================== 3. SUBSCRIPTIONS — PLAN CRUD ====================
# We keep the original tier registry static (BASIC/STANDARD/PREMIUM); this section
# exposes the plan catalogue + lets admins issue manual invoices for a tenant.

@router.get("/subscriptions/plans")
async def get_plan_catalogue(user=Depends(require_permission("subscriptions:read")), db=Depends(get_db)):
    """Returns the currently-active 3-tier plan matrix + any plan overrides stored in DB."""
    prices = get_tier_prices()
    overrides = await db.plan_overrides.find({}, {"_id": 0}).to_list(20)
    ov_map = {o["tier"]: o for o in overrides}
    plans = []
    for t in TIER_ORDER:
        base = {
            "tier": t,
            "name": t.title(),
            "annual_price": prices[t]["annual"],
            "half_yearly_price": prices[t]["half_yearly"],
            "quarterly_price": prices[t]["quarterly"],
            "modules_included": TIER_MODULES[t],
        }
        if t in ov_map:
            o = ov_map[t]
            base.update({k: v for k, v in o.items() if k in {"user_limit", "branch_limit", "storage_limit_mb", "sms_credits", "whatsapp_credits", "support_level", "custom_note"}})
        plans.append(base)
    return {"plans": plans, "tier_order": TIER_ORDER}


class PlanOverride(BaseModel):
    user_limit: Optional[int] = None
    branch_limit: Optional[int] = None
    storage_limit_mb: Optional[int] = None
    sms_credits: Optional[int] = None
    whatsapp_credits: Optional[int] = None
    support_level: Optional[str] = None
    custom_note: Optional[str] = None


@router.put("/subscriptions/plans/{tier}")
async def update_plan_override(
    tier: str, payload: PlanOverride, request: Request,
    user=Depends(require_permission("subscriptions:write")),
    db=Depends(get_db),
):
    if tier not in TIER_ORDER:
        raise HTTPException(400, detail=f"tier must be one of {TIER_ORDER}")
    upd = {k: v for k, v in payload.model_dump().items() if v is not None}
    upd["tier"] = tier
    upd["updated_at"] = datetime.now(timezone.utc).isoformat()
    await db.plan_overrides.update_one({"tier": tier}, {"$set": upd}, upsert=True)
    await _log_audit(db, user, "plan.override", tier, after=upd, request=request)
    return {"ok": True, "tier": tier, "overrides": {k: v for k, v in upd.items() if k not in {"tier", "updated_at"}}}


class TenantInvoiceCreate(BaseModel):
    clinic_id: str
    tier: str                                # BASIC|STANDARD|PREMIUM
    duration: Literal["annual", "half_yearly", "quarterly"] = "annual"
    amount_override: Optional[float] = None  # allows discounts / coupons
    coupon_code: Optional[str] = None
    notes: Optional[str] = None


@router.post("/subscriptions/invoices")
async def issue_tenant_invoice(
    payload: TenantInvoiceCreate, request: Request,
    user=Depends(require_permission("invoices:write")),
    db=Depends(get_db),
):
    if payload.tier not in TIER_ORDER:
        raise HTTPException(400, detail=f"tier must be one of {TIER_ORDER}")
    clinic = await db.clinics.find_one({"clinic_id": payload.clinic_id}, {"_id": 0, "name": 1, "email": 1})
    if not clinic:
        raise HTTPException(404, detail="Tenant not found")
    prices = get_tier_prices()[payload.tier]
    price_key = {"annual": "annual", "half_yearly": "half_yearly", "quarterly": "quarterly"}[payload.duration]
    base_amount = float(prices[price_key])
    amount = float(payload.amount_override) if payload.amount_override is not None else base_amount
    gst = round(amount * 0.18, 2)
    grand = round(amount + gst, 2)
    doc = {
        "invoice_id": f"TIN-{uuid.uuid4().hex[:8].upper()}",
        "clinic_id": payload.clinic_id,
        "clinic_name": clinic.get("name"),
        "tier": payload.tier,
        "duration": payload.duration,
        "base_amount": base_amount,
        "amount": amount,
        "gst_amount": gst,
        "grand_total": grand,
        "coupon_code": payload.coupon_code,
        "notes": payload.notes,
        "status": "pending",
        "payment_method": "manual",
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "issued_by": user["user_id"],
    }
    await db.tenant_invoices.insert_one(doc.copy())
    await _log_audit(db, user, "tenant_invoice.issue", payload.clinic_id, after=doc, request=request)
    doc.pop("_id", None)
    return doc


class InvoicePaidPayload(BaseModel):
    payment_ref: Optional[str] = None


@router.post("/subscriptions/invoices/{invoice_id}/mark-paid")
async def mark_tenant_invoice_paid(
    invoice_id: str, request: Request,
    payload: Optional[InvoicePaidPayload] = None,
    payment_ref: Optional[str] = None,
    user=Depends(require_permission("invoices:write")),
    db=Depends(get_db),
):
    ref = (payload.payment_ref if payload else None) or payment_ref
    r = await db.tenant_invoices.find_one_and_update(
        {"invoice_id": invoice_id, "status": "pending"},
        {"$set": {
            "status": "paid",
            "paid_at": datetime.now(timezone.utc).isoformat(),
            "payment_ref": ref,
            "paid_by": user["user_id"],
        }},
        projection={"_id": 0},
        return_document=True,
    )
    if not r:
        raise HTTPException(404, detail="Pending invoice not found")
    await _log_audit(db, user, "tenant_invoice.paid", r.get("clinic_id", invoice_id), after={"invoice_id": invoice_id, "ref": ref}, request=request)
    return r


# ==================== 4. REVENUE ====================

@router.get("/revenue")
async def platform_revenue(
    user=Depends(require_permission("revenue:read")),
    db=Depends(get_db),
):
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()

    # Tenant invoices (SaaS revenue)
    pipeline_this_month = [
        {"$match": {"issued_at": {"$gte": month_start}}},
        {"$group": {
            "_id": "$status",
            "count": {"$sum": 1},
            "sum": {"$sum": "$grand_total"},
        }},
    ]
    month_stats = {"paid": {"count": 0, "sum": 0}, "pending": {"count": 0, "sum": 0}, "failed": {"count": 0, "sum": 0}}
    async for row in db.tenant_invoices.aggregate(pipeline_this_month):
        month_stats[row["_id"]] = {"count": row["count"], "sum": round(float(row.get("sum") or 0), 2)}

    # Annual contracts still open
    annual_open = await db.tenant_invoices.count_documents({"duration": "annual", "status": "paid"})

    # Pending / overdue
    overdue = await db.tenant_invoices.find(
        {"status": "pending"},
        {"_id": 0},
    ).sort("issued_at", 1).limit(50).to_list(50)

    # Recent invoices
    recent = await db.tenant_invoices.find({}, {"_id": 0}).sort("issued_at", -1).limit(50).to_list(50)

    # Refunds (status=refunded)
    refunds_count = await db.tenant_invoices.count_documents({"status": "refunded"})

    return {
        "this_month": month_stats,
        "total_this_month_collected": month_stats["paid"]["sum"],
        "annual_contracts_open": annual_open,
        "refunds_count": refunds_count,
        "overdue": [deserialize_datetime(r) for r in overdue],
        "recent_invoices": [deserialize_datetime(r) for r in recent],
    }


# ==================== 5. LEADS / TRIALS PIPELINE ====================

LEAD_STAGES = ["Lead", "Demo Scheduled", "Trial Started", "Active Trial", "Converted", "Lost"]


class LeadUpdate(BaseModel):
    stage: Optional[str] = None
    assigned_sales_rep: Optional[str] = None
    notes: Optional[str] = None
    contact_name: Optional[str] = None
    mobile: Optional[str] = None
    source: Optional[str] = None


@router.get("/leads")
async def list_leads(
    stage: Optional[str] = None,
    user=Depends(require_permission("leads:read")),
    db=Depends(get_db),
):
    """Lead pipeline. **Cached 30s** per stage filter."""
    return await cached(
        key=f"leads:v1:{stage}",
        factory=lambda: _compute_list_leads(stage, db),
        ttl_seconds=30,
    )


async def _compute_list_leads(stage, db):
    q: dict = {}
    if stage:
        q["stage"] = stage
    # Pull waitlist + enriched with any lead-pipeline fields
    rows = await db.waitlist_signups.find(q, {"_id": 0}).sort("created_at", -1).to_list(1000)
    # Bucket counts for kanban header
    counts = {s: 0 for s in LEAD_STAGES}
    for r in rows:
        counts[r.get("stage") or "Lead"] = counts.get(r.get("stage") or "Lead", 0) + 1

    # "N in queue this week" KPI — counts real (non-test) signups created in
    # the last 7 days, regardless of stage. Sales uses this to gauge inbound
    # velocity at a glance.
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    in_queue_this_week = await db.waitlist_signups.count_documents({
        "created_at": {"$gte": week_ago},
        "email": {"$not": {"$regex": r"(?i)^(test|qa|sample|demo|smoke|pytest|fake)@"}},
    })

    return {
        "stages": LEAD_STAGES,
        "counts": counts,
        "in_queue_this_week": in_queue_this_week,
        "rows": [deserialize_datetime(r) for r in rows],
    }


@router.patch("/leads/{email}")
async def update_lead(
    email: str, payload: LeadUpdate, request: Request,
    user=Depends(require_permission("leads:write")),
    db=Depends(get_db),
):
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(400, detail="Nothing to update")
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    if updates.get("stage") and updates["stage"] not in LEAD_STAGES:
        raise HTTPException(400, detail=f"stage must be one of {LEAD_STAGES}")
    r = await db.waitlist_signups.find_one_and_update(
        {"email": email.lower()},
        {"$set": updates},
        projection={"_id": 0},
        return_document=True,
    )
    if not r:
        raise HTTPException(404, detail="Lead not found")
    await _log_audit(db, user, "lead.update", email, after=updates, request=request)
    return deserialize_datetime(r)


# ---------- Convert Lead → Clinic + Invitation -----------------------------

class ConvertLeadRequest(BaseModel):
    """Founder confirms / overrides the lead's submitted details before
    creating the clinic. All fields are optional; missing fields fall back
    to the lead's original values."""
    clinic_name: Optional[str] = Field(default=None, min_length=2, max_length=120)
    city: Optional[str] = None
    state: Optional[str] = None
    phone: Optional[str] = None
    owner_name: Optional[str] = Field(default=None, min_length=2, max_length=80)
    owner_email: Optional[EmailStr] = None
    tier: Optional[Literal["BASIC", "STANDARD", "PREMIUM"]] = None
    trial_days: int = Field(default=30, ge=0, le=180)


class CreateTenantRequest(BaseModel):
    """For the manual 'Add Tenant' flow — founder onboards a clinic that
    didn't come through the website."""
    clinic_name: str = Field(min_length=2, max_length=120)
    owner_name: str = Field(min_length=2, max_length=80)
    owner_email: EmailStr
    city: Optional[str] = None
    state: Optional[str] = None
    phone: Optional[str] = None
    tier: Literal["BASIC", "STANDARD", "PREMIUM"] = "STANDARD"
    trial_days: int = Field(default=30, ge=0, le=180)
    # Direct-password mode — if provided, a fully-formed clinic_owner user
    # is created with this password and NO invitation link is issued. Useful
    # when the founder is on a phone call with the owner and wants them to
    # log in immediately. Length mirrors the tenant-user endpoint (>=8).
    initial_password: Optional[str] = Field(default=None, min_length=8, max_length=128)


class TenantCreatedResponse(BaseModel):
    """Returned to the founder after a successful conversion / creation.
    Either `accept_url` (invite flow) OR `direct_login_password` (direct-
    password flow) will be populated — never both."""
    clinic_id: str
    clinic_name: str
    owner_email: str
    accept_url: Optional[str] = None
    invite_token: Optional[str] = None
    invite_expires_at: Optional[datetime] = None
    converted_from_lead: bool = False
    # Direct-password mode fields (only present when initial_password was set).
    direct_login_password: Optional[str] = None
    direct_login_name: Optional[str] = None
    tier: Optional[str] = None


async def _create_clinic_with_invite(
    *, db, request: Request, actor: dict,
    clinic_name: str, owner_name: str, owner_email: str,
    city: str, state: str, phone: str,
    tier: str, trial_days: int,
    converted_from_lead: bool = False, lead_email: Optional[str] = None,
    initial_password: Optional[str] = None,
) -> TenantCreatedResponse:
    """Shared helper used by both 'Convert Lead' and 'Add Tenant'.
    Creates clinic + primary branch. Then either (a) issues an invitation
    link so the new owner sets their own password, or (b) when
    `initial_password` is supplied, creates the clinic_owner user directly
    with that password so they can sign in immediately — no invite dance."""
    import re
    import secrets
    from datetime import timedelta as _td
    from routers.invitations import _build_accept_url, INVITE_TTL_DAYS

    email = owner_email.lower().strip()

    # Conflict guard — someone might already own a clinic on this email
    if await db.users.find_one({"email": email}, {"_id": 0, "user_id": 1}):
        raise HTTPException(409, detail="A user with this email already exists. Use the existing tenant or invite to a different email.")

    # Unique slug
    slug = re.sub(r"[^a-z0-9]+", "-", clinic_name.lower()).strip("-")[:40] or "clinic"
    clinic_id = f"clinic-{slug}-{uuid.uuid4().hex[:6]}"
    branch_id = f"BR-{uuid.uuid4().hex[:8].upper()}"

    now = datetime.now(timezone.utc)
    trial_end = now + _td(days=trial_days) if trial_days > 0 else None

    clinic_doc = {
        "clinic_id": clinic_id,
        "name": clinic_name.strip(),
        "city": city or "",
        "state": state or "",
        "phone": phone or "",
        "email": email,
        "mrd_prefix": slug.upper()[:3] or "CLN",
        "subscription_tier": tier,
        "signup_source": "founder-converted" if converted_from_lead else "founder-direct",
        "created_at": now,
    }
    if trial_end:
        clinic_doc["trial_ends_at"] = trial_end
    await db.clinics.insert_one(serialize_datetime(clinic_doc))

    await db.branches.insert_one(serialize_datetime({
        "branch_id": branch_id,
        "clinic_id": clinic_id,
        "name": clinic_name.strip(),
        "city": city or "",
        "is_primary": True,
        "active": True,
        "created_at": now,
    }))

    # ----- Branch: direct-password OR invite flow --------------------------
    if initial_password:
        # Create the clinic_owner user right now with the supplied password.
        # No invitation row is minted — the owner can log in immediately.
        from auth import hash_password as _hp
        user_doc = {
            "user_id": f"USR-{uuid.uuid4().hex[:8].upper()}",
            "clinic_id": clinic_id,
            "email": email,
            "name": owner_name.strip(),
            "role": "clinic_owner",
            "active": True,
            "two_fa_enabled": False,
            "password_hash": _hp(initial_password),
            "branch_ids": [branch_id],
            "created_at": now.isoformat(),
            "created_by": actor["user_id"],
            "created_via": "admin_direct_tenant_create",
        }
        await db.users.insert_one(user_doc.copy())

        # Update lead → converted (same audit trail as invite flow).
        if converted_from_lead and lead_email:
            await db.waitlist_signups.update_one(
                {"email": lead_email.lower()},
                {"$set": {
                    "stage": "Converted",
                    "converted_clinic_id": clinic_id,
                    "converted_at": now,
                    "converted_by": actor["user_id"],
                    "updated_at": now.isoformat(),
                }},
            )
        await _log_audit(
            db, actor,
            "tenant.create_direct_password" if not converted_from_lead else "lead.convert_direct_password",
            clinic_id,
            after={"email": email, "tier": tier, "lead_email": lead_email},
            request=request,
        )

        # Fire a welcome email with the credentials as a backup for the owner —
        # the UI still shows them once for the admin. Best-effort: if email
        # fails, we log but do not break the creation flow.
        try:
            from utils.email import send_email
            login_url = os.environ.get("PUBLIC_APP_URL", "").rstrip("/") or ""
            html = f"""
            <div style="font-family:-apple-system,Segoe UI,sans-serif;max-width:540px;margin:0 auto">
              <div style="background:#0B5FFF;color:#fff;padding:20px 24px;border-radius:12px 12px 0 0">
                <h2 style="margin:0">Welcome to AUDINEXA</h2>
                <p style="margin:4px 0 0;opacity:0.9;font-size:13px">{clinic_name.strip()} · {tier} plan</p>
              </div>
              <div style="background:#fff;border:1px solid #e5e7eb;border-top:0;padding:24px;border-radius:0 0 12px 12px">
                <p>Hi {owner_name.strip()},</p>
                <p>Your AUDINEXA clinic account has been created. You can sign in with the credentials below:</p>
                <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:14px;margin:16px 0;font-family:monospace;font-size:13px">
                  <div><b style="color:#64748b;font-family:sans-serif;font-size:11px;text-transform:uppercase;letter-spacing:0.05em">Email</b><br>{email}</div>
                  <div style="margin-top:10px"><b style="color:#64748b;font-family:sans-serif;font-size:11px;text-transform:uppercase;letter-spacing:0.05em">Password</b><br>{initial_password}</div>
                </div>
                <p><b>Next step:</b> please change this password after your first login. Open Settings → My Profile to update it.</p>
                { f'<p style="margin-top:20px"><a href="{login_url}/login" style="background:#0B5FFF;color:#fff;text-decoration:none;padding:10px 20px;border-radius:8px;font-weight:600;display:inline-block">Sign in now</a></p>' if login_url else '' }
                <p style="color:#94a3b8;font-size:12px;margin-top:24px">If you didn't expect this email, please ignore it — your account remains secure.</p>
              </div>
            </div>
            """
            send_email(
                to=email,
                subject=f"Your AUDINEXA account is ready — {clinic_name.strip()}",
                html_body=html,
                purpose="tenant_welcome_direct_password",
            )
        except Exception as _exc:  # noqa: BLE001 — email is non-critical here
            log.warning("welcome_email_failed clinic=%s err=%s", clinic_id, _exc)

        return TenantCreatedResponse(
            clinic_id=clinic_id,
            clinic_name=clinic_name.strip(),
            owner_email=email,
            converted_from_lead=converted_from_lead,
            direct_login_password=initial_password,
            direct_login_name=owner_name.strip(),
            tier=tier,
        )

    # ----- Invitation flow (default) ---------------------------------------
    token = secrets.token_urlsafe(32)
    expires_at = now + _td(days=INVITE_TTL_DAYS)
    invite_doc = {
        "invite_id": f"INV-{uuid.uuid4().hex[:10].upper()}",
        "token": token,
        "clinic_id": clinic_id,
        "email": email,
        "name": owner_name.strip(),
        "role": "clinic_owner",
        "branch_ids": [branch_id],
        "phone": phone,
        "status": "pending",
        "created_at": now,
        "created_by": actor["user_id"],
        "expires_at": expires_at,
    }
    await db.invitations.insert_one(invite_doc)

    accept_url = _build_accept_url(request, token)

    # ----- Update lead → converted -----
    if converted_from_lead and lead_email:
        await db.waitlist_signups.update_one(
            {"email": lead_email.lower()},
            {"$set": {
                "stage": "Converted",
                "converted_clinic_id": clinic_id,
                "converted_at": now,
                "converted_by": actor["user_id"],
                "updated_at": now.isoformat(),
            }},
        )

    await _log_audit(db, actor,
                     "tenant.create_via_invite" if not converted_from_lead else "lead.convert",
                     clinic_id,
                     after={"email": email, "tier": tier, "lead_email": lead_email},
                     request=request)

    return TenantCreatedResponse(
        clinic_id=clinic_id,
        clinic_name=clinic_name.strip(),
        owner_email=email,
        accept_url=accept_url,
        invite_token=token,
        invite_expires_at=expires_at,
        converted_from_lead=converted_from_lead,
        tier=tier,
    )


@router.post("/leads/{email}/convert", response_model=TenantCreatedResponse)
async def convert_lead_to_tenant(
    email: str, payload: ConvertLeadRequest, request: Request,
    user=Depends(require_permission("leads:write")),
    db=Depends(get_db),
):
    """One-click convert: lead → clinic + primary branch + owner invitation.
    Founder shares the returned `accept_url` with the prospect (WhatsApp
    today; auto-emailed once SendGrid lands)."""
    lead = await db.waitlist_signups.find_one({"email": email.lower()}, {"_id": 0})
    if not lead:
        raise HTTPException(404, detail="Lead not found")
    if lead.get("stage") == "Converted" and lead.get("converted_clinic_id"):
        # Idempotent guard — return the existing clinic (no duplicate creation)
        raise HTTPException(409, detail=f"Lead already converted to clinic {lead['converted_clinic_id']}")

    return await _create_clinic_with_invite(
        db=db, request=request, actor=user,
        clinic_name=payload.clinic_name or lead.get("clinic_name") or f"{lead.get('name', 'New')} Clinic",
        owner_name=payload.owner_name or lead.get("name") or "Clinic Owner",
        owner_email=str(payload.owner_email or email).lower(),
        city=payload.city or lead.get("city", ""),
        state=payload.state or lead.get("state", ""),
        phone=payload.phone or lead.get("phone", ""),
        tier=payload.tier or lead.get("tier") or "STANDARD",
        trial_days=payload.trial_days,
        converted_from_lead=True,
        lead_email=email.lower(),
    )


@router.post("/tenants", response_model=TenantCreatedResponse)
async def create_tenant_with_invite(
    payload: CreateTenantRequest, request: Request,
    user=Depends(require_permission("tenants:write")),
    db=Depends(get_db),
):
    """Manual 'Add Tenant' — founder onboards a clinic that didn't come
    through the website. Same end-state as convert_lead, just no lead
    record to update."""
    return await _create_clinic_with_invite(
        db=db, request=request, actor=user,
        clinic_name=payload.clinic_name,
        owner_name=payload.owner_name,
        owner_email=payload.owner_email,
        city=payload.city or "",
        state=payload.state or "",
        phone=payload.phone or "",
        tier=payload.tier,
        trial_days=payload.trial_days,
        converted_from_lead=False,
        initial_password=payload.initial_password,
    )


# ==================== 6. FEATURE FLAGS (per-tenant additive) ====================
# A tenant's effective modules = TIER_MODULES[tier] ∪ extra_modules − disabled_modules

AVAILABLE_MODULES = [
    # (code, label, description)
    {"code": "frontdesk", "label": "Clinical Front Desk", "category": "core"},
    {"code": "diagnostics", "label": "Clinical Diagnostics", "category": "core"},
    {"code": "hearing-aids", "label": "HA Commerce Engine", "category": "commerce"},
    {"code": "repair", "label": "Service & Repair (AUDINEXA)", "category": "commerce"},
    {"code": "amc", "label": "AMC Management", "category": "commerce"},
    {"code": "analytics", "label": "Analytics Pro", "category": "insights"},
    {"code": "patient-portal", "label": "Patient Portal", "category": "engagement"},
    {"code": "referral-partners", "label": "Referral Partners", "category": "engagement"},
    {"code": "loaners", "label": "Loaner Program", "category": "commerce"},
    {"code": "ci-module", "label": "Cochlear Implants (roadmap)", "category": "clinical"},
    {"code": "rehab-module", "label": "Rehabilitation (roadmap)", "category": "clinical"},
    {"code": "white-label", "label": "White Label", "category": "enterprise"},
    {"code": "api-access", "label": "API Access", "category": "enterprise"},
    {"code": "multi-branch", "label": "Multi Branch", "category": "enterprise"},
]


class FlagsUpdate(BaseModel):
    extra_modules: Optional[List[str]] = None
    disabled_modules: Optional[List[str]] = None


@router.get("/feature-flags/{clinic_id}")
async def get_feature_flags(
    clinic_id: str,
    user=Depends(require_permission("features:read")),
    db=Depends(get_db),
):
    c = await db.clinics.find_one({"clinic_id": clinic_id}, {"_id": 0, "subscription_tier": 1, "trial_ends_at": 1})
    if not c:
        raise HTTPException(404, detail="Tenant not found")
    tier = await resolve_effective_tier(c)
    flags = await db.tenant_feature_flags.find_one({"clinic_id": clinic_id}, {"_id": 0}) or {
        "clinic_id": clinic_id, "extra_modules": [], "disabled_modules": [],
    }
    base_mods = set(TIER_MODULES[tier])
    effective = (base_mods | set(flags.get("extra_modules", []))) - set(flags.get("disabled_modules", []))
    return {
        "clinic_id": clinic_id,
        "tier": tier,
        "base_modules": sorted(base_mods),
        "extra_modules": flags.get("extra_modules", []),
        "disabled_modules": flags.get("disabled_modules", []),
        "effective_modules": sorted(effective),
        "available_modules": AVAILABLE_MODULES,
    }


@router.put("/feature-flags/{clinic_id}")
async def update_feature_flags(
    clinic_id: str, payload: FlagsUpdate, request: Request,
    user=Depends(require_permission("features:write")),
    db=Depends(get_db),
):
    c = await db.clinics.find_one({"clinic_id": clinic_id}, {"_id": 0})
    if not c:
        raise HTTPException(404, detail="Tenant not found")
    updates: dict = {}
    if payload.extra_modules is not None:
        updates["extra_modules"] = list({m for m in payload.extra_modules})
    if payload.disabled_modules is not None:
        updates["disabled_modules"] = list({m for m in payload.disabled_modules})
    if not updates:
        raise HTTPException(400, detail="Nothing to update")
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    updates["clinic_id"] = clinic_id
    await db.tenant_feature_flags.update_one({"clinic_id": clinic_id}, {"$set": updates}, upsert=True)
    await _log_audit(db, user, "feature_flags.update", clinic_id, after=updates, request=request)
    return {"ok": True, **updates}


# ==================== AUDIT EXPORT ====================

@router.get("/audit-logs")
async def list_audit_logs(
    limit: int = 200,
    user=Depends(require_permission("audit:read")),
    db=Depends(get_db),
):
    rows = await db.admin_audit_logs.find({}, {"_id": 0}).sort("at", -1).to_list(limit)
    return [deserialize_datetime(r) for r in rows]


# ==================== ACTIVITY TRACKING — extracted to routers/admin_activity.py ====================




# ==================== BETA TESTER SEEDER (founder-only, one-time) ====================

class BetaSeedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reset: bool = False  # if True, wipe all beta-* tenants/users before re-seeding (dangerous)


@router.post("/seed/beta-testers")
async def seed_beta_testers_endpoint(
    payload: BetaSeedRequest,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """One-shot founder-only endpoint to seed 10 beta tester workspaces.

    Idempotent — skips clinics that already exist unless `reset=true`.
    Returns the generated credentials ONLY on first creation; for skipped
    entries, password is masked (cannot recover — re-run with reset=true to
    rotate).
    """
    if user.get("role") != "founder":
        raise HTTPException(status_code=403, detail="Only founder can run the beta seeder")

    # Delegate to the standalone module so logic stays in one place
    from beta_seed import BETA_TESTERS, TRIAL_DAYS, _gen_password, _mrd_prefix
    from datetime import datetime, timezone, timedelta
    from uuid import uuid4
    from utils.serde import serialize_datetime

    now = datetime.now(timezone.utc)
    credentials: list[dict] = []

    if payload.reset:
        ids = [t["clinic_id"] for t in BETA_TESTERS]
        emails = [t["email"] for t in BETA_TESTERS]
        await db.clinics.delete_many({"clinic_id": {"$in": ids}})
        await db.users.delete_many({"email": {"$in": emails}})
        await db.branches.delete_many({"clinic_id": {"$in": ids}})

    for t in BETA_TESTERS:
        cid = t["clinic_id"]
        existing_clinic = await db.clinics.find_one({"clinic_id": cid})
        existing_user = await db.users.find_one({"email": t["email"]})

        if existing_clinic and existing_user:
            credentials.append({
                "clinic": t["name"], "city": t["city"], "contact": t["contact_name"],
                "email": t["email"], "password": "<already-seeded>", "status": "skipped",
            })
            continue

        if not existing_clinic:
            await db.clinics.insert_one(serialize_datetime({
                "clinic_id": cid,
                "name": t["name"],
                "city": t["city"],
                "state": t["state"],
                "country": "India",
                "phone": t["phone"],
                "email": t["email"],
                "mrd_prefix": _mrd_prefix(cid),
                "subscription_tier": "STANDARD",
                "trial_ends_at": now + timedelta(days=TRIAL_DAYS),
                "signup_source": "beta-program",
                "status": "active",
                "created_at": now,
            }))

        branch = await db.branches.find_one({"clinic_id": cid, "is_primary": True})
        if branch:
            branch_id = branch["branch_id"]
        else:
            branch_id = f"BR-{str(uuid4())[:8].upper()}"
            await db.branches.insert_one(serialize_datetime({
                "branch_id": branch_id, "clinic_id": cid,
                "name": f"{t['city']} HQ",
                "city": t["city"], "state": t["state"],
                "is_primary": True, "active": True, "created_at": now,
            }))

        password = _gen_password()
        await db.users.insert_one(serialize_datetime({
            "user_id": f"USR-{str(uuid4())[:8].upper()}",
            "clinic_id": cid,
            "email": t["email"],
            "name": t["contact_name"],
            "role": "clinic_owner",
            "active": True,
            "password_hash": hash_password(password),
            "branch_ids": [branch_id],
            "created_at": now,
        }))

        # Service catalogue is now curated per-tenant in Settings → Service Catalogue.
        # We intentionally DO NOT auto-seed services so each clinic starts clean and
        # only sees what their owner explicitly adds. Owners can add their first
        # service in seconds via the inline "+ New service" button in Billing.

        credentials.append({
            "clinic": t["name"], "city": t["city"], "contact": t["contact_name"],
            "email": t["email"], "password": password, "status": "created",
        })

    # Audit trail
    await db.admin_audit_logs.insert_one(serialize_datetime({
        "log_id": f"LOG-{str(uuid4())[:8].upper()}",
        "actor_email": user["email"], "actor_role": user.get("role"),
        "action": "beta_testers_seeded",
        "details": {"reset": payload.reset, "created": sum(1 for c in credentials if c["status"] == "created")},
        "at": now,
    }))

    return {
        "success": True,
        "trial_days": TRIAL_DAYS,
        "tier": "STANDARD",
        "credentials": credentials,
        "instruction": "Distribute these credentials to your beta testers. Passwords cannot be recovered — copy them now.",
    }
