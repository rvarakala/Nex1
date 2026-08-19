"""AUDINEXA Super-Admin Panel — Phase 14B + 14C.

Extends /api/admin/v2 prefix with 8 more modules:
  Phase 14B:
    7. Support Desk
    8. Usage Analytics (per-tenant + churn-risk scoring)
    9. System Health (live + incident log)
   10. Marketing CRM (campaigns + attribution)
  Phase 14C:
   11. Notifications Center (global broadcast)
   12. Full Audit Log viewer (filtered query)
   13. Settings (platform config)
   14. Granular RBAC (7 role permission matrix)

All endpoints require founder or super_admin by default. Sub-roles
(sales_manager, support_agent, finance_manager, product_ops, read_only)
are enforced inside each endpoint via the ROLE_PERMISSIONS matrix.
"""
from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from auth import get_current_user
from database import get_db
from utils.serde import serialize_datetime, deserialize_datetime
from utils.rbac import ROLE_PERMISSIONS, has_permission, require_permission

from routers.admin_panel import _log_audit  # reuse audit helper


router = APIRouter(prefix="/api/admin/v2")


# ==================== RBAC (Phase 14C) ====================
# Permission matrix lives in utils/rbac.py (shared with admin_panel.py).


@router.get("/rbac/matrix")
async def get_rbac_matrix(user=Depends(get_current_user)):
    if user["role"] not in {"founder", "super_admin", "product_ops"}:
        raise HTTPException(403, detail="Not permitted")
    return {
        "roles": list(ROLE_PERMISSIONS.keys()),
        "matrix": ROLE_PERMISSIONS,
        "documented_actions": sorted({a for perms in ROLE_PERMISSIONS.values() for a in perms}),
    }


# ==================== EMAIL VERIFICATION RECOVERY (2026-07-26) ====================
# Any founder can unblock users stuck at the verification gate — either by
# force-verifying them (skip OTP entirely) or by resending a fresh OTP through
# the current email provider. Introduced after a Zepto → Resend migration left
# ~production users trapped at "Check your email".

@router.get("/users/stuck-verification")
async def list_unverified_users(user=Depends(get_current_user), db=Depends(get_db)):
    """List every user whose signup never completed the email-OTP step."""
    if user["role"] not in {"founder", "super_admin"}:
        raise HTTPException(403, detail="Founder / super_admin only")
    cursor = db.users.find(
        {"$or": [{"email_verified": False}, {"email_verified": {"$exists": False}}]},
        {"_id": 0, "user_id": 1, "email": 1, "name": 1, "clinic_id": 1,
         "role": 1, "created_at": 1, "email_verified_via": 1},
    ).sort("created_at", -1).limit(500)
    rows = [serialize_datetime(u) async for u in cursor]
    return {"count": len(rows), "users": rows}


class VerifyOverrideRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    email: EmailStr


@router.post("/users/force-verify")
async def force_verify_user(
    body: VerifyOverrideRequest,
    request: Request,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Founder override — mark a user as email-verified without an OTP.

    Use when the user is stuck (email provider down, mail lost, etc.).
    Audit-logged.
    """
    if user["role"] not in {"founder", "super_admin"}:
        raise HTTPException(403, detail="Founder / super_admin only")
    target = await db.users.find_one({"email": body.email.lower()}, {"_id": 0})
    if not target:
        raise HTTPException(404, detail=f"No user with email {body.email}")
    if target.get("email_verified"):
        return {"ok": True, "already_verified": True, "email": target["email"]}
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    await db.users.update_one(
        {"email": target["email"]},
        {"$set": {"email_verified": True,
                  "email_verified_at": now_iso,
                  "email_verified_via": f"founder_override:{user['email']}"},
         "$unset": {"email_verification_code": "",
                    "email_verification_expires": "",
                    "email_verification_attempts": ""}},
    )
    await _log_audit(db, user, "user.email.force_verify", target["email"],
                     after={"verified_at": now_iso}, request=request)
    return {"ok": True, "email": target["email"], "verified_at": now_iso}


@router.post("/users/resend-verification")
async def resend_verification_email(
    body: VerifyOverrideRequest,
    request: Request,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Regenerate the 6-digit OTP and re-send via the current email provider."""
    if user["role"] not in {"founder", "super_admin"}:
        raise HTTPException(403, detail="Founder / super_admin only")
    target = await db.users.find_one({"email": body.email.lower()}, {"_id": 0})
    if not target:
        raise HTTPException(404, detail=f"No user with email {body.email}")
    if target.get("email_verified"):
        return {"ok": True, "already_verified": True, "email": target["email"]}
    # Reuse the same signup path — persists a fresh code + sends the mail.
    from routers.email_verify import issue_verification_code
    await issue_verification_code(db, target, purpose="admin_resend")
    await _log_audit(db, user, "user.email.resend_otp", target["email"], request=request)
    return {"ok": True, "email": target["email"],
            "message": "A fresh 6-digit code was sent via the current email provider."}


@router.get("/email-health")
async def email_health(user=Depends(get_current_user), db=Depends(get_db)):
    """Powers the Founder Dashboard email-health banner + detail page.

    Rolls up the `email_events` collection (populated by `utils/email.py`
    on every send) over 1h + 24h windows and returns a compact health
    envelope: current provider, deliverability rate, recent errors,
    and a traffic-light status.
    """
    if user["role"] not in {"founder", "super_admin"}:
        raise HTTPException(403, detail="Founder / super_admin only")
    import os
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    since_1h  = now - timedelta(hours=1)
    since_24h = now - timedelta(hours=24)

    async def _bucket(since):
        total = await db.email_events.count_documents({"timestamp": {"$gte": since}})
        sent  = await db.email_events.count_documents({"timestamp": {"$gte": since},
                                                       "status": {"$in": ["sent", "mocked"]}})
        errors = await db.email_events.count_documents({"timestamp": {"$gte": since},
                                                         "status": "error"})
        fallback_used = await db.email_events.count_documents({"timestamp": {"$gte": since},
                                                                "used_fallback": True})
        rate = round(100.0 * errors / total, 1) if total else 0.0
        return {"total": total, "sent": sent, "errors": errors,
                "used_fallback": fallback_used, "error_rate_pct": rate}

    h1  = await _bucket(since_1h)
    h24 = await _bucket(since_24h)

    # Recent errors — last 5
    cursor = db.email_events.find(
        {"status": "error"},
        {"_id": 0, "timestamp": 1, "provider": 1, "to": 1, "purpose": 1,
         "error": 1, "fallback_provider": 1, "fallback_error": 1},
    ).sort("timestamp", -1).limit(5)
    recent_errors = [serialize_datetime(d) async for d in cursor]

    # Traffic-light status
    # - critical: any error in the last 5 min OR >25% error rate in 24h with any activity
    # - degraded: any error in last hour OR 5-25% error rate in 24h
    # - healthy:  otherwise
    since_5m = now - timedelta(minutes=5)
    err_5m = await db.email_events.count_documents({"timestamp": {"$gte": since_5m},
                                                     "status": "error"})
    if err_5m > 0 or (h24["total"] > 0 and h24["error_rate_pct"] > 25):
        status_light = "critical"
    elif h1["errors"] > 0 or (h24["total"] > 0 and h24["error_rate_pct"] > 5):
        status_light = "degraded"
    else:
        status_light = "healthy"

    return {
        "status":               status_light,
        "provider":             os.environ.get("EMAIL_PROVIDER", "mock"),
        "fallback_provider":    os.environ.get("EMAIL_FALLBACK_PROVIDER") or None,
        "last_1h":              h1,
        "last_24h":             h24,
        "errors_last_5m":       err_5m,
        "recent_errors":        recent_errors,
        "checked_at":           now.isoformat(),
    }


# ==================== 7. SUPPORT DESK (Phase 14B) ====================

TICKET_CATEGORIES = ["Billing", "Bug", "Feature Request", "Training", "Data Import", "Urgent Outage"]
TICKET_STATUSES = ["Open", "Pending", "Resolved", "Escalated", "Closed"]
TICKET_PRIORITIES = ["low", "medium", "high", "urgent"]
SLA_HOURS = {"low": 72, "medium": 24, "high": 8, "urgent": 2}


class TicketCreate(BaseModel):
    clinic_id: Optional[str] = None       # None for internal/ops-only tickets
    category: str
    priority: str = "medium"
    subject: str = Field(min_length=2)
    body: str = Field(min_length=1)
    contact_email: Optional[EmailStr] = None


class TicketUpdate(BaseModel):
    status: Optional[str] = None
    priority: Optional[str] = None
    owner_user_id: Optional[str] = None
    reply: Optional[str] = None           # appended to thread


@router.get("/tickets")
async def list_tickets(
    status: Optional[str] = None,
    priority: Optional[str] = None,
    clinic_id: Optional[str] = None,
    limit: int = 500,
    user=Depends(require_permission("tickets:read")),
    db=Depends(get_db),
):
    q: dict = {}
    if status:
        q["status"] = status
    if priority:
        q["priority"] = priority
    if clinic_id:
        q["clinic_id"] = clinic_id
    rows = await db.support_tickets.find(q, {"_id": 0}).sort("created_at", -1).to_list(limit)

    now = datetime.now(timezone.utc)
    open_statuses = {"Open", "Pending", "Escalated"}
    resolution_times: list[float] = []
    response_times: list[float] = []
    priority_counts: dict[str, int] = {p: 0 for p in TICKET_PRIORITIES}
    sla_breaches = 0

    for t in rows:
        if t.get("status") in open_statuses:
            try:
                sla_due = t.get("sla_due_at")
                if sla_due and datetime.fromisoformat(sla_due.replace("Z", "+00:00")) < now:
                    sla_breaches += 1
            except Exception:
                pass
        if t.get("first_response_at") and t.get("created_at"):
            try:
                a = datetime.fromisoformat(t["created_at"].replace("Z", "+00:00"))
                b = datetime.fromisoformat(t["first_response_at"].replace("Z", "+00:00"))
                response_times.append((b - a).total_seconds() / 3600.0)
            except Exception:
                pass
        if t.get("resolved_at") and t.get("created_at"):
            try:
                a = datetime.fromisoformat(t["created_at"].replace("Z", "+00:00"))
                b = datetime.fromisoformat(t["resolved_at"].replace("Z", "+00:00"))
                resolution_times.append((b - a).total_seconds() / 3600.0)
            except Exception:
                pass
        priority_counts[t.get("priority", "medium")] = priority_counts.get(t.get("priority", "medium"), 0) + 1

    def avg(xs):
        return round(sum(xs) / len(xs), 1) if xs else None

    return {
        "count": len(rows),
        "rows": [deserialize_datetime(r) for r in rows],
        "stats": {
            "avg_response_hrs": avg(response_times),
            "avg_resolution_hrs": avg(resolution_times),
            "sla_breaches": sla_breaches,
            "open_by_priority": priority_counts,
            "categories": TICKET_CATEGORIES,
            "statuses": TICKET_STATUSES,
            "priorities": TICKET_PRIORITIES,
        },
    }


@router.post("/tickets")
async def create_ticket(
    payload: TicketCreate, request: Request,
    user=Depends(require_permission("tickets:write")),
    db=Depends(get_db),
):
    if payload.category not in TICKET_CATEGORIES:
        raise HTTPException(400, detail=f"category must be one of {TICKET_CATEGORIES}")
    if payload.priority not in TICKET_PRIORITIES:
        raise HTTPException(400, detail=f"priority must be one of {TICKET_PRIORITIES}")
    now = datetime.now(timezone.utc)
    sla = now + timedelta(hours=SLA_HOURS[payload.priority])
    ticket = {
        "ticket_id": f"TKT-{uuid.uuid4().hex[:8].upper()}",
        "clinic_id": payload.clinic_id,
        "category": payload.category,
        "priority": payload.priority,
        "status": "Open",
        "subject": payload.subject,
        "body": payload.body,
        "contact_email": payload.contact_email,
        "owner_user_id": None,
        "thread": [{"at": now.isoformat(), "author": user.get("email"), "text": payload.body, "kind": "open"}],
        "first_response_at": None,
        "resolved_at": None,
        "created_by": user["user_id"],
        "created_at": now.isoformat(),
        "sla_due_at": sla.isoformat(),
    }
    await db.support_tickets.insert_one(ticket.copy())
    await _log_audit(db, user, "ticket.create", ticket["ticket_id"], after={"category": payload.category, "priority": payload.priority}, request=request)
    ticket.pop("_id", None)
    return ticket


@router.patch("/tickets/{ticket_id}")
async def update_ticket(
    ticket_id: str, payload: TicketUpdate, request: Request,
    user=Depends(require_permission("tickets:write")),
    db=Depends(get_db),
):
    existing = await db.support_tickets.find_one({"ticket_id": ticket_id}, {"_id": 0})
    if not existing:
        raise HTTPException(404, detail="Ticket not found")
    now_iso = datetime.now(timezone.utc).isoformat()
    updates: dict = {}
    push_items: list[dict] = []
    if payload.status:
        if payload.status not in TICKET_STATUSES:
            raise HTTPException(400, detail=f"status must be one of {TICKET_STATUSES}")
        updates["status"] = payload.status
        if payload.status == "Resolved" and not existing.get("resolved_at"):
            updates["resolved_at"] = now_iso
    if payload.priority:
        if payload.priority not in TICKET_PRIORITIES:
            raise HTTPException(400, detail=f"priority must be one of {TICKET_PRIORITIES}")
        updates["priority"] = payload.priority
    if payload.owner_user_id is not None:
        updates["owner_user_id"] = payload.owner_user_id
    if payload.reply:
        push_items.append({"at": now_iso, "author": user.get("email"), "text": payload.reply, "kind": "reply"})
        if not existing.get("first_response_at"):
            updates["first_response_at"] = now_iso
    if not updates and not push_items:
        raise HTTPException(400, detail="Nothing to update")
    mongo_update: dict = {}
    if updates:
        mongo_update["$set"] = updates
    if push_items:
        mongo_update["$push"] = {"thread": {"$each": push_items}}
    r = await db.support_tickets.find_one_and_update(
        {"ticket_id": ticket_id},
        mongo_update,
        projection={"_id": 0},
        return_document=True,
    )
    await _log_audit(db, user, "ticket.update", ticket_id, after=updates, request=request)
    return deserialize_datetime(r)


# ==================== 8. USAGE ANALYTICS (Phase 14B) ====================

@router.get("/usage-analytics")
async def usage_analytics(
    days: int = 30,
    user=Depends(require_permission("usage:read")),
    db=Depends(get_db),
):
    """Per-tenant DAU/MAU/retention proxy + churn-risk score."""
    now = datetime.now(timezone.utc)
    window_start = (now - timedelta(days=max(days, 7))).isoformat()
    month_start = (now - timedelta(days=30)).isoformat()
    day_start = (now - timedelta(days=1)).isoformat()

    clinics = await db.clinics.find({}, {"_id": 0, "clinic_id": 1, "name": 1, "subscription_tier": 1, "created_at": 1}).to_list(1000)

    rows = []
    for c in clinics:
        cid = c["clinic_id"]
        # DAU/MAU proxy via tokens.issued_at timestamps (tokens are issued on every clinic workflow)
        dau = await db.tokens.count_documents({"clinic_id": cid, "issued_at": {"$gte": day_start}})
        mau = await db.tokens.count_documents({"clinic_id": cid, "issued_at": {"$gte": month_start}})
        active_users_month = len(await db.tokens.distinct("issued_by_user_id", {"clinic_id": cid, "issued_at": {"$gte": month_start}}))
        patients_added = await db.patients.count_documents({"clinic_id": cid, "created_at": {"$gte": window_start}})
        reports_generated = await db.test_sessions.count_documents({"clinic_id": cid, "test_date": {"$gte": window_start}})
        invoices_created = await db.invoices.count_documents({"clinic_id": cid, "created_at": {"$gte": window_start}})
        # Days since last activity
        last_tok = await db.tokens.find_one({"clinic_id": cid}, {"_id": 0, "issued_at": 1}, sort=[("issued_at", -1)])
        inactive_days = None
        if last_tok and last_tok.get("issued_at"):
            try:
                inactive_days = (now - datetime.fromisoformat(last_tok["issued_at"].replace("Z", "+00:00"))).days
            except Exception:
                inactive_days = None

        # Churn-risk heuristic:
        #   low:   mau ≥ 20 AND inactive_days ≤ 3
        #   high:  inactive_days ≥ 14 OR mau == 0
        #   medium: otherwise
        risk = "medium"
        if mau == 0 or (inactive_days is not None and inactive_days >= 14):
            risk = "high"
        elif mau >= 20 and (inactive_days is None or inactive_days <= 3):
            risk = "low"

        # Feature adoption: how many distinct modules the tenant actually *touched*
        modules_touched = 0
        for coll, field in [("patients", "patient_id"), ("test_sessions", "session_id"),
                            ("ha_sales", "sale_no"), ("service_tickets", "ticket_no"),
                            ("ha_amc_contracts", "contract_no"), ("referral_partners", "partner_id")]:
            if await db[coll].count_documents({"clinic_id": cid}, limit=1):
                modules_touched += 1

        rows.append({
            "clinic_id": cid,
            "name": c.get("name"),
            "tier": c.get("subscription_tier", "BASIC"),
            "dau": dau,
            "mau": mau,
            "active_users_month": active_users_month,
            "patients_added": patients_added,
            "reports_generated": reports_generated,
            "invoices_created": invoices_created,
            "inactive_days": inactive_days,
            "feature_adoption": modules_touched,
            "churn_risk": risk,
        })

    # Aggregate totals
    totals = {
        "total_tenants": len(rows),
        "high_risk": sum(1 for r in rows if r["churn_risk"] == "high"),
        "medium_risk": sum(1 for r in rows if r["churn_risk"] == "medium"),
        "low_risk": sum(1 for r in rows if r["churn_risk"] == "low"),
        "platform_dau": sum(r["dau"] for r in rows),
        "platform_mau": sum(r["mau"] for r in rows),
    }
    rows.sort(key=lambda r: (-{"high": 2, "medium": 1, "low": 0}[r["churn_risk"]], -(r["inactive_days"] or 0)))
    return {"window_days": days, "totals": totals, "rows": rows}


# ==================== 9. SYSTEM HEALTH (Phase 14B) ====================

_APP_START_TS = time.time()


@router.get("/system/health")
async def system_health(
    user=Depends(require_permission("system:read")),
    db=Depends(get_db),
):
    uptime_s = int(time.time() - _APP_START_TS)
    uptime_h = round(uptime_s / 3600, 1)

    # DB ping
    db_ok = False
    db_latency_ms = None
    try:
        t0 = time.time()
        await db.command("ping")
        db_latency_ms = int((time.time() - t0) * 1000)
        db_ok = True
    except Exception:
        db_ok = False

    # Last completed backup (mock: use latest closeout doc as a proxy)
    last_backup = await db.closeouts.find_one({}, {"_id": 0, "closed_at": 1, "clinic_id": 1}, sort=[("closed_at", -1)])

    # Queue backlog (proxy: count of service_tickets in non-terminal states)
    queue_backlog = await db.service_tickets.count_documents({"status": {"$in": ["awaiting_triage", "in_service", "awaiting_parts"]}})

    # ----- Gateway statuses derived from env + creds -----
    # Each gateway can be in one of: healthy | degraded | mocked | down.
    # We derive from the provider env flag + whether the required creds are
    # present, so the System Health page reflects reality without a separate
    # background probe writing to `platform_gateway_health`.
    def _email_status() -> dict:
        provider = os.environ.get("EMAIL_PROVIDER", "mock").strip().lower()
        if provider == "zepto":
            host = os.environ.get("ZEPTO_SMTP_HOST", "").strip()
            pw   = os.environ.get("ZEPTO_SMTP_PASSWORD", "").strip()
            frm  = os.environ.get("ZEPTO_FROM_ADDRESS", "").strip()
            ok   = bool(host and pw and frm)
            return {"status": "healthy" if ok else "degraded", "provider": "zepto",
                    "from_addr": frm or None, "host": host or None}
        return {"status": "mocked", "provider": "mock"}

    def _sms_status() -> dict:
        provider = os.environ.get("SMS_PROVIDER", "mock").strip().lower()
        if provider == "twilio":
            sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
            tok = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
            frm = os.environ.get("TWILIO_FROM_NUMBER", "").strip()
            ok  = bool(sid and tok and frm)
            return {"status": "healthy" if ok else "degraded", "provider": "twilio",
                    "from_number": frm or None, "account_sid": (sid[:8] + "…") if sid else None}
        return {"status": "mocked", "provider": "mock"}

    def _whatsapp_status() -> dict:
        auth    = os.environ.get("MSG91_HOSTED_AUTH_KEY", "").strip()
        number  = os.environ.get("MSG91_HOSTED_NUMBER", "").strip()
        enc_key = os.environ.get("MSG91_ENCRYPTION_KEY", "").strip()
        if not auth:
            return {"status": "mocked", "provider": "mock"}
        # Auth key is there but integrated number / templates aren't — we can
        # authenticate but can't actually send, so this is "degraded".
        if not number:
            return {"status": "degraded", "provider": "msg91", "note": "Auth key present — waiting on integrated number & approved templates"}
        return {"status": "healthy" if enc_key else "degraded", "provider": "msg91",
                "from_number": number}

    # Async history counts — last 7 days delivery stats (best-effort).
    seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)
    try:
        wa_total = await db.whatsapp_message_logs.count_documents({"created_at": {"$gte": seven_days_ago.isoformat()}})
        wa_failed = await db.whatsapp_message_logs.count_documents({
            "created_at": {"$gte": seven_days_ago.isoformat()},
            "status": {"$in": ["failed", "error"]},
        })
    except Exception:
        wa_total, wa_failed = 0, 0

    email_block = _email_status()
    email_block["success_rate_7d"] = 100  # TODO: compute from audit logs once email-tracking is wired

    sms_block = _sms_status()
    sms_block["success_rate_7d"] = 100

    whatsapp_block = _whatsapp_status()
    whatsapp_block["success_rate_7d"] = 100 if wa_total == 0 else round((wa_total - wa_failed) / wa_total * 100)

    # Recent incidents
    incidents = await db.platform_incidents.find({}, {"_id": 0}).sort("started_at", -1).limit(20).to_list(20)

    return {
        "api": {
            "status": "healthy",
            "uptime_seconds": uptime_s,
            "uptime_hours": uptime_h,
            "started_at": datetime.fromtimestamp(_APP_START_TS, tz=timezone.utc).isoformat(),
        },
        "database": {
            "status": "healthy" if db_ok else "down",
            "latency_ms": db_latency_ms,
        },
        "email_gateway":    email_block,
        "sms_gateway":      sms_block,
        "whatsapp_gateway": whatsapp_block,
        "queue_backlog":    queue_backlog,
        "last_backup":      last_backup,
        "incidents":        [deserialize_datetime(i) for i in incidents],
    }


# ---------- Ping-now: live round-trip test per gateway -------------------------

@router.post("/system/ping-gateway")
async def ping_gateway(
    payload: dict, request: Request,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Live round-trip probe for a single gateway. Body: {"gateway": "email"|"sms"|"whatsapp"}.

    The handler fires a real API call to the configured provider and returns
    latency + the structured provider response. For SMS/email it sends a
    deliberate "to-an-invalid-address" request so no real message is
    consumed — we only need to confirm auth works.
    """
    if user["role"] not in {"founder", "super_admin"}:
        raise HTTPException(403, detail="Not permitted")
    kind = (payload or {}).get("gateway", "").strip().lower()
    if kind not in {"email", "sms", "whatsapp"}:
        raise HTTPException(400, detail="gateway must be 'email', 'sms', or 'whatsapp'")

    t0 = time.time()
    try:
        if kind == "email":
            from utils.email import send_email
            # Ping: send a tiny email TO the configured From address (bounces back
            # to us, proves auth works without hitting real inboxes).
            probe_to = os.environ.get("ZEPTO_FROM_ADDRESS") or "noreply@example.invalid"
            res = send_email(probe_to, "[AUDINEXA ping]",
                             html_body="<p>ping</p>", purpose="system_health_ping")
        elif kind == "sms":
            from utils.sms import send_sms
            # Use an invalid-E.164 number on purpose — Twilio will return 21211
            # (bad 'To') which still proves auth works without consuming SMS quota.
            res = send_sms("+10000000000", "AUDINEXA ping", purpose="system_health_ping")
        else:  # whatsapp
            # MSG91 auth-only probe — checks we can reach the API and token is valid.
            res = await _probe_msg91(db)
    except Exception as exc:
        res = {"status": "error", "error": str(exc)}
    latency_ms = int((time.time() - t0) * 1000)

    await _log_audit(db, user, f"system.ping_gateway.{kind}", kind,
                     after={"status": res.get("status"), "latency_ms": latency_ms},
                     request=request)
    return {"gateway": kind, "latency_ms": latency_ms, "result": res}


async def _probe_msg91(db) -> dict:
    """Small MSG91 auth-only probe. Reuses the utils.msg91 helpers if present."""
    auth = os.environ.get("MSG91_HOSTED_AUTH_KEY", "").strip()
    if not auth:
        return {"status": "mocked", "provider": "mock", "error": "MSG91_HOSTED_AUTH_KEY not set"}
    try:
        import httpx
        # Templates list is a harmless auth-check endpoint. If key is bad, MSG91
        # returns 401; if template list fetches, key is valid.
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get("https://api.msg91.com/api/v5/whatsapp/templates",
                                 headers={"authkey": auth})
        if r.status_code == 200:
            return {"status": "healthy", "provider": "msg91", "note": f"auth OK ({r.status_code})"}
        if r.status_code in (401, 403):
            return {"status": "error", "provider": "msg91",
                    "error": f"MSG91 auth rejected ({r.status_code})"}
        return {"status": "degraded", "provider": "msg91",
                "note": f"unexpected HTTP {r.status_code}"}
    except Exception as exc:
        return {"status": "error", "provider": "msg91", "error": str(exc)}


# ==================== DATA-HEALTH PROBE (c) ===================================
# Validates a sample of docs in schema-critical collections against their
# current Pydantic models. Flags rows that would cause a ResponseValidationError
# like the ha_sales 500 we fixed earlier. Cheap, read-only, safe to run live.

@router.get("/system/data-health")
async def data_health(
    user=Depends(require_permission("system:read")),
    db=Depends(get_db),
):
    from pydantic import ValidationError
    PROBES = []
    try:
        from models import Patient
        PROBES.append(("patients", "patients", Patient))
    except ImportError:
        pass
    try:
        from models import Invoice
        PROBES.append(("invoices", "invoices", Invoice))
    except ImportError:
        pass
    try:
        from models_ha import Sale
        PROBES.append(("ha_sales", "ha_sales", Sale))
    except ImportError:
        pass

    results = []
    for label, coll, Model in PROBES:
        total   = await db[coll].count_documents({})
        sampled = 0
        failed_count = 0                # true count for scoring
        failed_samples: list[dict] = [] # capped at 10 for drill-down display
        # Sample up to 500 docs per collection — fast (<50ms) and statistically
        # sufficient for a regression signal. Sorted newest-first because drift
        # usually starts from a model change that hits fresh writes first.
        cursor = db[coll].find({}, {"_id": 0}).sort("created_at", -1).limit(500)
        async for doc in cursor:
            sampled += 1
            try:
                Model(**doc)
            except ValidationError as exc:
                failed_count += 1
                # Keep first 10 failing doc-ids per collection for drill-down.
                if len(failed_samples) < 10:
                    pk = doc.get("patient_id") or doc.get("invoice_id") or doc.get("sale_no") or doc.get("id") or "?"
                    failed_samples.append({
                        "id": pk,
                        "errors": [{"loc": ".".join(str(x) for x in e["loc"]), "msg": e["msg"]}
                                   for e in exc.errors()[:3]],
                    })
            except Exception as exc:  # noqa: BLE001 — catch deserialization glitches
                failed_count += 1
                if len(failed_samples) < 10:
                    failed_samples.append({"id": "?", "errors": [{"loc": "", "msg": str(exc)}]})

        results.append({
            "collection": label,
            "total_docs": total,
            "sampled":    sampled,
            "failed":     failed_count,
            "health_pct": 100 if sampled == 0 else round((sampled - failed_count) / sampled * 100, 1),
            "failures":   failed_samples,
        })

    overall = "healthy"
    if any(r["failed"] > 0 for r in results):
        overall = "degraded"

    # ---- Auto-incident on schema drift -------------------------------------
    # For every probed collection with failures, ensure exactly ONE open
    # incident exists titled `DATA_HEALTH: <coll> schema drift`. We never
    # close it from here — operator must investigate + manually resolve.
    auto_incidents: list[str] = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for r in results:
        if r["failed"] <= 0:
            continue
        title = f"DATA_HEALTH: {r['collection']} schema drift"
        existing = await db.platform_incidents.find_one(
            {"title": title, "resolved_at": None}, projection={"_id": 0}
        )
        if existing:
            continue
        sev = "critical" if r["health_pct"] < 90 else "major"
        sample_ids = ", ".join(str(f.get("id", "?")) for f in r["failures"][:3])
        doc = {
            "incident_id": f"INC-{uuid.uuid4().hex[:8].upper()}",
            "title": title,
            "severity": sev,
            "summary": (
                f"Auto-detected by data-health probe: {r['failed']}/{r['sampled']} "
                f"sampled docs failed Pydantic validation "
                f"({r['health_pct']}% healthy). Sample doc ids: {sample_ids or '—'}."
            ),
            "started_at": now_iso,
            "resolved_at": None,
            "logged_by": "system:data-health",
            "source": "auto",
        }
        await db.platform_incidents.insert_one(doc.copy())
        auto_incidents.append(doc["incident_id"])

    return {"overall": overall, "probes": results,
            "auto_incidents_opened": auto_incidents,
            "at": now_iso}


# ==================== API LATENCY SPEEDOMETER (Phase 15) ====================
# Live in-process latency stats fed by utils/latency_recorder.py middleware.
# Founder dashboard polls this every 5s to render the speedometer + p50/p95/p99
# tiles. Zero external deps, per-worker sampling, bounded memory.

@router.get("/system/latency")
async def api_latency(
    user=Depends(require_permission("system:read")),
):
    from utils.latency_recorder import (
        stats_for_window,
        slowest_routes,
        status_distribution,
        health_level,
        _APP_START_TS,
    )
    s60 = stats_for_window(60)
    s300 = stats_for_window(300)
    slowest = slowest_routes(300, limit=10)
    statuses = status_distribution(300)
    uptime_s = int(time.time() - _APP_START_TS)
    return {
        "at": datetime.now(timezone.utc).isoformat(),
        "uptime_seconds": uptime_s,
        "window_60s": s60,
        "window_5m": s300,
        "health": health_level(s60["p95"]),
        "slowest_routes": slowest,
        "status_distribution": statuses,
    }


# ---------- Bulk-resolve synthetic / named-prefix incidents -------------------

@router.post("/system/incidents/bulk-resolve")
async def bulk_resolve_incidents(
    payload: dict, request: Request,
    user=Depends(require_permission("system:read")),
    db=Depends(get_db),
):
    """Resolve every open incident whose title starts with `title_prefix`.
    Used to clear `TEST_*` noise accumulated during QA phases."""
    prefix = (payload or {}).get("title_prefix", "").strip()
    if not prefix or len(prefix) < 3:
        raise HTTPException(400, detail="title_prefix (>=3 chars) required")
    # Escape regex metacharacters so user input can't over-match (e.g. `.*`
    # in a prefix would resolve every open incident). Only the literal
    # `^prefix` prefix-match semantic is preserved.
    import re as _re
    safe_prefix = _re.escape(prefix)
    now = datetime.now(timezone.utc).isoformat()
    r = await db.platform_incidents.update_many(
        {"title": {"$regex": f"^{safe_prefix}"}, "resolved_at": None},
        {"$set": {"resolved_at": now, "resolved_by": user["user_id"], "resolution_note": "bulk-resolved via admin panel"}},
    )
    await _log_audit(db, user, "incident.bulk_resolve", prefix,
                     after={"matched": r.matched_count, "modified": r.modified_count},
                     request=request)
    return {"matched": r.matched_count, "modified": r.modified_count}


class IncidentCreate(BaseModel):
    title: str
    severity: Literal["info", "minor", "major", "critical"] = "minor"
    summary: str = ""


@router.post("/system/incidents")
async def log_incident(
    payload: IncidentCreate, request: Request,
    user=Depends(require_permission("system:read")),
    db=Depends(get_db),
):
    doc = {
        "incident_id": f"INC-{uuid.uuid4().hex[:8].upper()}",
        "title": payload.title,
        "severity": payload.severity,
        "summary": payload.summary,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "resolved_at": None,
        "logged_by": user["user_id"],
    }
    await db.platform_incidents.insert_one(doc.copy())
    await _log_audit(db, user, "incident.log", doc["incident_id"], after=doc, request=request)
    doc.pop("_id", None)
    return doc


@router.post("/system/incidents/{incident_id}/resolve")
async def resolve_incident(
    incident_id: str, request: Request,
    user=Depends(require_permission("system:read")),
    db=Depends(get_db),
):
    r = await db.platform_incidents.find_one_and_update(
        {"incident_id": incident_id, "resolved_at": None},
        {"$set": {"resolved_at": datetime.now(timezone.utc).isoformat(), "resolved_by": user["user_id"]}},
        projection={"_id": 0},
        return_document=True,
    )
    if not r:
        raise HTTPException(404, detail="Open incident not found")
    await _log_audit(db, user, "incident.resolve", incident_id, request=request)
    return deserialize_datetime(r)


# ==================== HYBRID PDF STORAGE (P2) =========================
# Stats + manual sweep trigger for the audiogram-report blob retention model.
# Daily APScheduler job runs at 03:15 IST; admins can also force a sweep.

@router.get("/system/storage")
async def system_storage(
    user=Depends(require_permission("system:read")),
    db=Depends(get_db),
):
    from services.pdf_retention import gridfs_storage_stats
    return await gridfs_storage_stats(db)


@router.post("/system/storage/purge-pdfs")
async def system_storage_purge(
    request: Request,
    payload: dict | None = None,
    user=Depends(require_permission("system:read")),
    db=Depends(get_db),
):
    """Force-run the audiogram-report retention sweep. Founders/super_admin
    can override the configured retention with `?days=N` in the body.
    """
    from services.pdf_retention import purge_expired_session_reports
    days = None
    if isinstance(payload, dict) and "days" in payload:
        try:
            days = int(payload["days"])
        except (TypeError, ValueError):
            raise HTTPException(400, detail="days must be an integer")
    if days is not None and user["role"] not in {"founder", "super_admin"}:
        raise HTTPException(403, detail="Only founder/super_admin may override retention window")
    res = await purge_expired_session_reports(db, retention_days=days)
    await _log_audit(db, user, "system.pdf_retention.purge", "session_reports",
                     after=res, request=request)
    return res


# ==================== 10. MARKETING CRM (Phase 14B) ====================

class CampaignCreate(BaseModel):
    name: str = Field(min_length=2)
    source: str                       # "google-ads", "instagram", "partner", "linkedin", etc.
    channel: Optional[str] = None     # "paid", "organic", "referral"
    budget: float = 0.0
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    notes: Optional[str] = None


@router.get("/marketing/campaigns")
async def list_campaigns(
    user=Depends(require_permission("marketing:read")),
    db=Depends(get_db),
):
    rows = await db.marketing_campaigns.find({}, {"_id": 0}).sort("started_at", -1).to_list(200)
    # Enrich each campaign with leads generated (match waitlist_signups.source = campaign.source)
    enriched = []
    for c in rows:
        leads_n = await db.waitlist_signups.count_documents({"source": {"$regex": f"^{c['source']}$", "$options": "i"}})
        converted_n = await db.waitlist_signups.count_documents({"source": {"$regex": f"^{c['source']}$", "$options": "i"}, "stage": "Converted"})
        cac = round(c["budget"] / converted_n, 2) if converted_n else None
        enriched.append({
            **deserialize_datetime(c),
            "leads_generated": leads_n,
            "converted": converted_n,
            "conversion_pct": round(100 * converted_n / max(leads_n, 1), 1),
            "cac": cac,
        })
    # Totals
    total_budget = sum(r["budget"] for r in rows)
    total_leads = sum(r["leads_generated"] for r in enriched)
    total_converted = sum(r["converted"] for r in enriched)

    # Partner referrals roll-up
    partner_converted = await db.waitlist_signups.count_documents({"source": {"$regex": "partner", "$options": "i"}, "stage": "Converted"})
    webinar_registrations = await db.waitlist_signups.count_documents({"source": {"$regex": "webinar", "$options": "i"}})

    return {
        "campaigns": enriched,
        "totals": {
            "total_budget": round(total_budget, 2),
            "total_leads": total_leads,
            "total_converted": total_converted,
            "overall_conversion_pct": round(100 * total_converted / max(total_leads, 1), 1),
            "blended_cac": round(total_budget / total_converted, 2) if total_converted else None,
            "partner_referrals_converted": partner_converted,
            "webinar_registrations": webinar_registrations,
        },
    }


@router.post("/marketing/campaigns")
async def create_campaign(
    payload: CampaignCreate, request: Request,
    user=Depends(require_permission("marketing:write")),
    db=Depends(get_db),
):
    doc = {
        "campaign_id": f"CAM-{uuid.uuid4().hex[:8].upper()}",
        **payload.model_dump(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_by": user["user_id"],
    }
    await db.marketing_campaigns.insert_one(doc.copy())
    await _log_audit(db, user, "campaign.create", doc["campaign_id"], after=doc, request=request)
    doc.pop("_id", None)
    return doc


# ==================== 11. NOTIFICATIONS CENTER (Phase 14C) ====================

class NotificationSend(BaseModel):
    title: str = Field(min_length=2)
    body: str = Field(min_length=1)
    audience: Literal["all", "tier", "tenant"] = "all"
    audience_filter: Optional[str] = None   # tier name or clinic_id
    channels: List[Literal["in-app", "email", "sms", "whatsapp"]] = ["in-app"]
    priority: Literal["info", "important", "critical"] = "info"


@router.post("/notifications/send")
async def send_notification(
    payload: NotificationSend, request: Request,
    user=Depends(require_permission("notifications:write")),
    db=Depends(get_db),
):
    """Writes a broadcast doc. The `in-app` channel is the only one actually
    delivered today (poll via GET /notifications/feed). email/sms/whatsapp
    flags are recorded for downstream worker (MOCKED).
    """
    # Resolve target clinics
    q: dict = {}
    if payload.audience == "tier":
        q["subscription_tier"] = (payload.audience_filter or "").upper()
    elif payload.audience == "tenant":
        q["clinic_id"] = payload.audience_filter
    clinic_ids = [c["clinic_id"] async for c in db.clinics.find(q, {"_id": 0, "clinic_id": 1})]
    now_iso = datetime.now(timezone.utc).isoformat()
    doc = {
        "notification_id": f"NOT-{uuid.uuid4().hex[:8].upper()}",
        "title": payload.title,
        "body": payload.body,
        "audience": payload.audience,
        "audience_filter": payload.audience_filter,
        "channels": payload.channels,
        "priority": payload.priority,
        "target_clinic_ids": clinic_ids,
        "target_count": len(clinic_ids),
        "delivered_in_app": "in-app" in payload.channels,
        "sent_by": user["user_id"],
        "sent_at": now_iso,
    }
    await db.platform_notifications.insert_one(doc.copy())
    await _log_audit(db, user, "notification.send", doc["notification_id"], after={"audience": payload.audience, "targets": len(clinic_ids)}, request=request)
    doc.pop("_id", None)
    return doc


@router.get("/notifications")
async def list_notifications(
    user=Depends(require_permission("notifications:read")),
    db=Depends(get_db),
):
    rows = await db.platform_notifications.find({}, {"_id": 0}).sort("sent_at", -1).limit(100).to_list(100)
    return [deserialize_datetime(r) for r in rows]


@router.get("/notifications/feed")
async def feed_for_current_clinic(user=Depends(get_current_user), db=Depends(get_db)):
    """In-app feed endpoint for any authenticated user."""
    rows = await db.platform_notifications.find({
        "$or": [
            {"target_clinic_ids": user["clinic_id"]},
            {"audience": "all"},
        ],
    }, {"_id": 0}).sort("sent_at", -1).limit(20).to_list(20)
    return [deserialize_datetime(r) for r in rows]


# ==================== 12. AUDIT LOG VIEWER (Phase 14C) ====================

@router.get("/audit")
async def audit_logs_filtered(
    actor: Optional[str] = None,
    action: Optional[str] = None,
    target: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 500,
    user=Depends(require_permission("audit:read")),
    db=Depends(get_db),
):
    q: dict = {}
    if actor:
        q["actor_email"] = {"$regex": actor, "$options": "i"}
    if action:
        q["action"] = {"$regex": action, "$options": "i"}
    if target:
        q["target"] = {"$regex": target, "$options": "i"}
    if since:
        q["at"] = {"$gte": since}
    rows = await db.admin_audit_logs.find(q, {"_id": 0}).sort("at", -1).to_list(limit)
    # Stats
    action_counts: dict[str, int] = {}
    actor_counts: dict[str, int] = {}
    for r in rows:
        action_counts[r["action"]] = action_counts.get(r["action"], 0) + 1
        actor_counts[r.get("actor_email", "?")] = actor_counts.get(r.get("actor_email", "?"), 0) + 1
    return {
        "count": len(rows),
        "rows": [deserialize_datetime(r) for r in rows],
        "by_action": [{"action": k, "count": v} for k, v in sorted(action_counts.items(), key=lambda kv: -kv[1])[:20]],
        "by_actor": [{"actor": k, "count": v} for k, v in sorted(actor_counts.items(), key=lambda kv: -kv[1])[:20]],
    }


# ==================== 13. SETTINGS (Phase 14C) ====================

PLATFORM_SETTINGS_ID = "platform-settings-v1"

_DEFAULT_SETTINGS = {
    "brand_logo_url": None,
    "brand_name": "AUDINEXA",
    "support_email": "support@audinexa.com",
    "currency": "INR",
    "timezone": "Asia/Kolkata",
    "trial_duration_days": 30,
    "tax_rate_pct": 18.0,
    "tax_label": "GST",
    "email_templates": {
        "welcome": "Welcome to AUDINEXA!",
        "trial_ending": "Your trial ends in {days} days.",
        "payment_failed": "Your recent payment failed. Please update your payment method.",
    },
    "default_onboarding_checklist": [
        "Add first branch",
        "Invite 1 audiologist",
        "Configure service catalogue",
        "Register 5 patients",
        "Generate first diagnostic report",
    ],
}


class SettingsUpdate(BaseModel):
    brand_logo_url: Optional[str] = None
    brand_name: Optional[str] = None
    support_email: Optional[EmailStr] = None
    currency: Optional[str] = None
    timezone: Optional[str] = None
    trial_duration_days: Optional[int] = Field(default=None, ge=0, le=365)
    tax_rate_pct: Optional[float] = None
    tax_label: Optional[str] = None
    email_templates: Optional[dict] = None
    default_onboarding_checklist: Optional[List[str]] = None


@router.get("/settings")
async def get_settings(
    user=Depends(require_permission("dashboard:read")),
    db=Depends(get_db),
):
    doc = await db.platform_settings.find_one({"_id": PLATFORM_SETTINGS_ID})
    if not doc:
        return _DEFAULT_SETTINGS
    doc.pop("_id", None)
    # Merge with defaults so newly added keys always present
    merged = {**_DEFAULT_SETTINGS, **doc}
    return merged


@router.put("/settings")
async def update_settings(
    payload: SettingsUpdate, request: Request,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    # founder + super_admin only (settings writes are sensitive)
    if user["role"] not in {"founder", "super_admin"}:
        raise HTTPException(403, detail="Only founder/super_admin can update settings")
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(400, detail="Nothing to update")
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    updates["updated_by"] = user["user_id"]
    await db.platform_settings.update_one(
        {"_id": PLATFORM_SETTINGS_ID},
        {"$set": updates},
        upsert=True,
    )
    await _log_audit(db, user, "settings.update", PLATFORM_SETTINGS_ID, after=updates, request=request)
    doc = await db.platform_settings.find_one({"_id": PLATFORM_SETTINGS_ID})
    doc.pop("_id", None)
    return {**_DEFAULT_SETTINGS, **doc}


# ==================== INTERNAL USERS (Phase 14C) ====================

class InternalUserCreate(BaseModel):
    email: EmailStr
    name: str
    password: str = Field(min_length=8)
    role: str
    two_fa_enabled: bool = False


@router.get("/internal-users")
async def list_internal_users(
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    if user["role"] not in {"founder", "super_admin"}:
        raise HTTPException(403, detail="Not permitted")
    platform_id = "audinexa-platform"
    rows = await db.users.find(
        {"clinic_id": platform_id},
        {"_id": 0, "password_hash": 0},
    ).to_list(200)
    return [deserialize_datetime(r) for r in rows]


@router.post("/internal-users")
async def invite_internal_user(
    payload: InternalUserCreate, request: Request,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    if user["role"] not in {"founder", "super_admin"}:
        raise HTTPException(403, detail="Not permitted")
    if payload.role not in ROLE_PERMISSIONS:
        raise HTTPException(400, detail=f"Unknown role. Valid: {list(ROLE_PERMISSIONS.keys())}")
    existing = await db.users.find_one({"email": payload.email.lower()})
    if existing:
        raise HTTPException(409, detail="Email already registered")
    from auth import hash_password as _hp  # avoid circular
    doc = {
        "user_id": f"USR-{uuid.uuid4().hex[:8].upper()}",
        "clinic_id": "audinexa-platform",
        "email": payload.email.lower(),
        "name": payload.name,
        "role": payload.role,
        "active": True,
        "two_fa_enabled": payload.two_fa_enabled,
        "password_hash": _hp(payload.password),
        "branch_ids": [],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_by": user["user_id"],
    }
    await db.users.insert_one(doc.copy())
    await _log_audit(db, user, "internal_user.invite", payload.email.lower(), after={"role": payload.role}, request=request)
    doc.pop("_id", None)
    doc.pop("password_hash", None)
    return doc


@router.patch("/internal-users/{user_id}")
async def update_internal_user(
    user_id: str,
    active: Optional[bool] = None,
    role: Optional[str] = None,
    request: Request = None,
    caller=Depends(get_current_user),
    db=Depends(get_db),
):
    if caller["role"] not in {"founder", "super_admin"}:
        raise HTTPException(403, detail="Not permitted")
    updates: dict = {}
    if active is not None:
        updates["active"] = active
    if role is not None:
        if role not in ROLE_PERMISSIONS:
            raise HTTPException(400, detail=f"Unknown role: {role}")
        updates["role"] = role
    if not updates:
        raise HTTPException(400, detail="Nothing to update")
    r = await db.users.find_one_and_update(
        {"user_id": user_id, "clinic_id": "audinexa-platform"},
        {"$set": updates},
        projection={"_id": 0, "password_hash": 0},
        return_document=True,
    )
    if not r:
        raise HTTPException(404, detail="Internal user not found")
    await _log_audit(db, caller, "internal_user.update", user_id, after=updates, request=request)
    return deserialize_datetime(r)


# ==================== TENANT USERS (admin-created clinic staff) ====================
# Founder / Super Admin can manually create a user inside a specific clinic by
# providing email + password directly — bypasses the invite-accept flow when
# support is onboarding a clinic over a phone call or setting up a demo.

CLINIC_ROLES = {
    "clinic_owner", "front_desk", "audiologist", "accounts",
    "inventory_manager", "technician", "referral_partner",
}


class TenantUserCreate(BaseModel):
    clinic_id: str
    email: EmailStr
    name: str
    password: str = Field(min_length=8)
    role: str
    branch_ids: Optional[list[str]] = None


@router.post("/tenant-users")
async def create_tenant_user(
    payload: TenantUserCreate, request: Request,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Directly create a clinic user. Founder + Super Admin only."""
    if user["role"] not in {"founder", "super_admin"}:
        raise HTTPException(403, detail="Not permitted")
    if payload.role not in CLINIC_ROLES:
        raise HTTPException(
            400,
            detail=f"Invalid clinic role. Valid: {sorted(CLINIC_ROLES)}",
        )
    # Confirm target clinic exists (prevents orphaned users from typos).
    clinic = await db.clinics.find_one({"clinic_id": payload.clinic_id}, {"_id": 0, "clinic_id": 1, "name": 1})
    if not clinic:
        raise HTTPException(404, detail=f"Clinic '{payload.clinic_id}' not found")
    # Global email uniqueness (the DB has a unique index on email).
    existing = await db.users.find_one({"email": payload.email.lower()})
    if existing:
        raise HTTPException(409, detail="Email already registered")

    from auth import hash_password as _hp  # avoid circular

    doc = {
        "user_id": f"USR-{uuid.uuid4().hex[:8].upper()}",
        "clinic_id": payload.clinic_id,
        "email": payload.email.lower(),
        "name": payload.name,
        "role": payload.role,
        "active": True,
        "two_fa_enabled": False,
        "password_hash": _hp(payload.password),
        "branch_ids": payload.branch_ids or [],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_by": user["user_id"],
        "created_via": "admin_manual",  # audit hint — not from invite flow
    }
    await db.users.insert_one(doc.copy())
    await _log_audit(
        db, user, "tenant_user.create", payload.email.lower(),
        after={"clinic_id": payload.clinic_id, "role": payload.role},
        request=request,
    )
    doc.pop("_id", None)
    doc.pop("password_hash", None)
    return {**doc, "clinic_name": clinic.get("name")}


# ==================== USER LIFECYCLE (deactivate / reactivate / hard-delete) ====
# Unified endpoints that work on ANY user (internal team or tenant clinic staff).
# Founder + Super Admin can deactivate/reactivate; hard-delete is FOUNDER-ONLY.

async def _revoke_all_sessions(db, user_id: str) -> int:
    """Mark every open session for the user as revoked. Returns count revoked."""
    now = datetime.now(timezone.utc).isoformat()
    r = await db.user_sessions.update_many(
        {"user_id": user_id, "revoked_at": None},
        {"$set": {"revoked_at": now, "revoke_reason": "admin_deactivate"}},
    )
    return r.modified_count or 0


async def _bump_token_version(db, user_id: str) -> None:
    """Invalidate every outstanding JWT for this user without touching sessions."""
    await db.users.update_one({"user_id": user_id}, {"$inc": {"token_version": 1}})


@router.patch("/users/{user_id}/deactivate")
async def deactivate_user(
    user_id: str, request: Request,
    caller=Depends(get_current_user),
    db=Depends(get_db),
):
    """Soft-delete: sets active=false, revokes every session, bumps token_version
    so any cached JWT stops working. Reversible via /reactivate."""
    if caller["role"] not in {"founder", "super_admin"}:
        raise HTTPException(403, detail="Not permitted")
    if user_id == caller["user_id"]:
        raise HTTPException(400, detail="Cannot deactivate your own account")
    target = await db.users.find_one({"user_id": user_id}, {"_id": 0, "password_hash": 0})
    if not target:
        raise HTTPException(404, detail="User not found")
    if target.get("role") == "founder":
        raise HTTPException(403, detail="Cannot deactivate a founder account")
    await db.users.update_one({"user_id": user_id}, {"$set": {"active": False}})
    sessions_revoked = await _revoke_all_sessions(db, user_id)
    await _bump_token_version(db, user_id)
    await _log_audit(
        db, caller, "user.deactivate", user_id,
        after={"email": target.get("email"), "sessions_revoked": sessions_revoked},
        request=request,
    )
    return {"ok": True, "user_id": user_id, "sessions_revoked": sessions_revoked}


@router.patch("/users/{user_id}/reactivate")
async def reactivate_user(
    user_id: str, request: Request,
    caller=Depends(get_current_user),
    db=Depends(get_db),
):
    """Undoes deactivate. User must log in again — sessions stay revoked."""
    if caller["role"] not in {"founder", "super_admin"}:
        raise HTTPException(403, detail="Not permitted")
    target = await db.users.find_one({"user_id": user_id}, {"_id": 0, "password_hash": 0})
    if not target:
        raise HTTPException(404, detail="User not found")
    await db.users.update_one({"user_id": user_id}, {"$set": {"active": True}})
    await _log_audit(
        db, caller, "user.reactivate", user_id,
        after={"email": target.get("email")},
        request=request,
    )
    return {"ok": True, "user_id": user_id}


@router.delete("/users/{user_id}")
async def hard_delete_user(
    user_id: str, request: Request,
    caller=Depends(get_current_user),
    db=Depends(get_db),
):
    """FOUNDER-ONLY. Hard-deletes the user row + sessions. Audit rows referencing
    this user_id are preserved (compliance trail). Blocks deletion when:
      - target is the caller themselves
      - target is a founder
      - target is the SOLE clinic_owner of an active clinic (would orphan it —
        deactivate first, transfer ownership, then delete).
    """
    if caller["role"] != "founder":
        raise HTTPException(403, detail="Only the founder can hard-delete a user")
    if user_id == caller["user_id"]:
        raise HTTPException(400, detail="Cannot delete your own account")
    target = await db.users.find_one({"user_id": user_id}, {"_id": 0, "password_hash": 0})
    if not target:
        raise HTTPException(404, detail="User not found")
    if target.get("role") == "founder":
        raise HTTPException(403, detail="Cannot delete a founder account")
    # Sole clinic_owner guard — only for non-platform clinics.
    if target.get("role") == "clinic_owner" and target.get("clinic_id") != "audinexa-platform":
        other_owners = await db.users.count_documents({
            "clinic_id": target["clinic_id"],
            "role": "clinic_owner",
            "user_id": {"$ne": user_id},
            "active": True,
        })
        if other_owners == 0:
            raise HTTPException(
                409,
                detail=(
                    "This user is the only active owner of the clinic. "
                    "Add another clinic_owner (or delete the whole tenant) before removing them."
                ),
            )
    # Best-effort cascade — sessions + pending invitations. Historical rows
    # (invoices, appointments, audit_log) preserve the user_id as data-only,
    # not FK, so no cascade is needed for the compliance trail.
    sessions_revoked = await _revoke_all_sessions(db, user_id)
    try:
        await db.invitations.delete_many({"invited_by": user_id, "status": "pending"})
    except Exception:
        pass
    del_result = await db.users.delete_one({"user_id": user_id})
    if del_result.deleted_count == 0:
        raise HTTPException(404, detail="User already removed")
    await _log_audit(
        db, caller, "user.hard_delete", user_id,
        before={"email": target.get("email"), "role": target.get("role"), "clinic_id": target.get("clinic_id")},
        after={"sessions_revoked": sessions_revoked},
        request=request,
    )
    return {"ok": True, "user_id": user_id, "sessions_revoked": sessions_revoked}


# ---- Bulk operations --------------------------------------------------------
# Same guards as the single-user endpoints, applied per-row. Skips (rather than
# aborts) rows that fail a guard so a mixed batch still processes the safe rows.

class BulkUserIds(BaseModel):
    user_ids: list[str] = Field(min_length=1, max_length=200)


async def _process_bulk(
    db, caller, request, user_ids: list[str], action: str,
) -> dict:
    """action ∈ {'deactivate', 'reactivate', 'delete'}"""
    deactivated: list[str] = []
    skipped: list[dict] = []
    for uid in user_ids:
        try:
            if uid == caller["user_id"]:
                skipped.append({"user_id": uid, "reason": "self"})
                continue
            target = await db.users.find_one({"user_id": uid}, {"_id": 0, "password_hash": 0})
            if not target:
                skipped.append({"user_id": uid, "reason": "not_found"})
                continue
            if target.get("role") == "founder":
                skipped.append({"user_id": uid, "reason": "founder_protected"})
                continue
            if action == "deactivate":
                await db.users.update_one({"user_id": uid}, {"$set": {"active": False}})
                await _revoke_all_sessions(db, uid)
                await _bump_token_version(db, uid)
            elif action == "reactivate":
                await db.users.update_one({"user_id": uid}, {"$set": {"active": True}})
            elif action == "delete":
                if caller["role"] != "founder":
                    skipped.append({"user_id": uid, "reason": "delete_founder_only"})
                    continue
                # Sole-owner guard
                if target.get("role") == "clinic_owner" and target.get("clinic_id") != "audinexa-platform":
                    others = await db.users.count_documents({
                        "clinic_id": target["clinic_id"],
                        "role": "clinic_owner",
                        "user_id": {"$ne": uid},
                        "active": True,
                    })
                    if others == 0:
                        skipped.append({"user_id": uid, "reason": "sole_clinic_owner"})
                        continue
                await _revoke_all_sessions(db, uid)
                try:
                    await db.invitations.delete_many({"invited_by": uid, "status": "pending"})
                except Exception:
                    pass
                await db.users.delete_one({"user_id": uid})
            deactivated.append(uid)
        except Exception as e:  # noqa: BLE001 — keep the batch going on unexpected failure
            skipped.append({"user_id": uid, "reason": f"error: {str(e)[:80]}"})
    await _log_audit(
        db, caller, f"user.bulk_{action}", ",".join(user_ids[:5]),
        after={"processed": len(deactivated), "skipped": len(skipped)},
        request=request,
    )
    return {"ok": True, "processed": deactivated, "skipped": skipped, "counts": {
        "processed": len(deactivated), "skipped": len(skipped),
    }}


@router.post("/users/bulk-deactivate")
async def bulk_deactivate_users(
    payload: BulkUserIds, request: Request,
    caller=Depends(get_current_user),
    db=Depends(get_db),
):
    if caller["role"] not in {"founder", "super_admin"}:
        raise HTTPException(403, detail="Not permitted")
    return await _process_bulk(db, caller, request, payload.user_ids, "deactivate")


@router.post("/users/bulk-reactivate")
async def bulk_reactivate_users(
    payload: BulkUserIds, request: Request,
    caller=Depends(get_current_user),
    db=Depends(get_db),
):
    if caller["role"] not in {"founder", "super_admin"}:
        raise HTTPException(403, detail="Not permitted")
    return await _process_bulk(db, caller, request, payload.user_ids, "reactivate")


@router.post("/users/bulk-delete")
async def bulk_delete_users(
    payload: BulkUserIds, request: Request,
    caller=Depends(get_current_user),
    db=Depends(get_db),
):
    if caller["role"] != "founder":
        raise HTTPException(403, detail="Only the founder can bulk-delete users")
    return await _process_bulk(db, caller, request, payload.user_ids, "delete")


# ==================== TEST SMS (Twilio smoke test) ==========================
# One-shot endpoint for the founder to fire a real SMS and confirm end-to-end
# delivery. Mirrors the `send_sms()` helper contract so the UI just needs to
# show the returned status + error string. Audit-logged so the channel-bill
# can be reconciled later.

class TestSmsRequest(BaseModel):
    to: str = Field(min_length=6, max_length=20)
    body: str = Field(default="AUDINEXA test SMS — if you see this, Twilio works.",
                      min_length=1, max_length=480)


@router.post("/test-sms")
async def test_sms(
    payload: TestSmsRequest, request: Request,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Founder / super-admin fires a real SMS via the configured provider.
    Returns the raw `send_sms()` result so any Twilio error code surfaces
    directly in the UI."""
    if user["role"] not in {"founder", "super_admin"}:
        raise HTTPException(403, detail="Not permitted")
    from utils.sms import send_sms
    result = send_sms(payload.to, payload.body, purpose="admin_smoke_test")
    await _log_audit(
        db, user, "sms.test", payload.to,
        after={"status": result.get("status"), "provider": result.get("provider"), "sid": result.get("sid")},
        request=request,
    )
    return result


# ==================== TEST EMAIL (ZeptoMail smoke test) =====================
# Mirror of /test-sms for the email channel.

class TestEmailRequest(BaseModel):
    to: EmailStr
    subject: str = Field(default="AUDINEXA test email — if you see this, ZeptoMail works.",
                         min_length=1, max_length=200)
    body: str = Field(default="<p>Hello from <b>AUDINEXA</b>.</p><p>If you see this, ZeptoMail SMTP is wired correctly.</p>",
                      min_length=1, max_length=10_000)


@router.post("/test-email")
async def test_email(
    payload: TestEmailRequest, request: Request,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Founder / super-admin fires a real email via the configured provider.
    Returns the structured `send_email()` result so any SMTP error surfaces
    directly in the UI."""
    if user["role"] not in {"founder", "super_admin"}:
        raise HTTPException(403, detail="Not permitted")
    from utils.email import send_email
    result = send_email(
        payload.to, payload.subject,
        html_body=payload.body,
        purpose="admin_smoke_test",
    )
    await _log_audit(
        db, user, "email.test", payload.to,
        after={"status": result.get("status"), "provider": result.get("provider"),
               "message_id": result.get("message_id")},
        request=request,
    )
    return result


# ==================== 15. CLINIC ASSIGNMENTS (Multi-Clinic admin) ====================
#
# Lets founders / super_admins manage which clinics a user can sign into.
# Used by the /admin/clinic-assignments page + switch-audit viewer.
# Mutations re-use the already-existing POST /api/auth/link-clinic and
# /api/auth/unlink-clinic endpoints — this module only adds listing views.

@router.get("/clinic-assignments")
async def list_clinic_assignments(
    q: Optional[str] = None,
    limit: int = 200,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """All non-platform users with their primary + additional clinic grants.

    Filters out the internal audinexa-platform users (they never belong to
    a clinic tenant). Optional `q` matches name/email substring.
    """
    if user["role"] not in {"founder", "super_admin"}:
        raise HTTPException(403, detail="Not permitted")

    query: dict = {"clinic_id": {"$ne": "audinexa-platform"}}
    if q:
        query["$or"] = [
            {"name": {"$regex": q, "$options": "i"}},
            {"email": {"$regex": q, "$options": "i"}},
        ]
    rows = await db.users.find(
        query,
        {"_id": 0, "password_hash": 0, "token_version": 0},
    ).sort("created_at", -1).to_list(limit)

    # Hydrate clinic names for primary + additional in one round-trip.
    all_ids: set[str] = set()
    for r in rows:
        if r.get("clinic_id"):
            all_ids.add(r["clinic_id"])
        for cid in r.get("additional_clinic_ids", []) or []:
            all_ids.add(cid)
    clinics = await db.clinics.find(
        {"clinic_id": {"$in": list(all_ids)}},
        {"_id": 0, "clinic_id": 1, "name": 1, "city": 1, "subscription_tier": 1},
    ).to_list(len(all_ids) or 1)
    cmap = {c["clinic_id"]: c for c in clinics}

    out = []
    for r in rows:
        extras = r.get("additional_clinic_ids", []) or []
        out.append({
            "user_id": r["user_id"],
            "email": r["email"],
            "name": r.get("name"),
            "role": r.get("role"),
            "active": r.get("active", True),
            "primary_clinic_id": r.get("clinic_id"),
            "primary_clinic": cmap.get(r.get("clinic_id")),
            "additional_clinic_ids": extras,
            "additional_clinics": [cmap[c] for c in extras if c in cmap],
            "total_clinics": 1 + len(extras),
            "created_at": r.get("created_at"),
        })
    # Multi-clinic owners first, then single-clinic users — easiest to spot.
    out.sort(key=lambda u: (-u["total_clinics"], u.get("email") or ""))
    return {"count": len(out), "rows": out}


@router.get("/clinic-assignments/export.csv")
async def export_clinic_assignments_csv(
    q: Optional[str] = None,
    limit: int = 5000,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """CSV snapshot of who-can-access-which-clinic for compliance audits.

    Flattens the JSON shape into ONE ROW PER ASSIGNMENT. A user with
    1 primary + 2 additional clinics produces 3 rows, each tagged
    `assignment_type = primary | additional`. This makes the file trivial
    to sort/filter in Excel for an internal audit cycle.
    """
    if user["role"] not in {"founder", "super_admin"}:
        raise HTTPException(403, detail="Not permitted")

    import csv
    import io
    from fastapi.responses import StreamingResponse

    query: dict = {"clinic_id": {"$ne": "audinexa-platform"}}
    if q:
        query["$or"] = [
            {"name": {"$regex": q, "$options": "i"}},
            {"email": {"$regex": q, "$options": "i"}},
        ]
    users = await db.users.find(
        query, {"_id": 0, "password_hash": 0, "token_version": 0},
    ).sort("email", 1).to_list(min(max(limit, 1), 50000))

    # Hydrate clinic metadata in one round-trip.
    all_ids: set[str] = set()
    for u in users:
        if u.get("clinic_id"):
            all_ids.add(u["clinic_id"])
        for cid in u.get("additional_clinic_ids", []) or []:
            all_ids.add(cid)
    clinics = await db.clinics.find(
        {"clinic_id": {"$in": list(all_ids)}},
        {"_id": 0, "clinic_id": 1, "name": 1, "city": 1, "state": 1, "subscription_tier": 1, "status": 1},
    ).to_list(len(all_ids) or 1)
    cmap = {c["clinic_id"]: c for c in clinics}

    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
    writer.writerow([
        "user_id", "user_email", "user_name", "user_role", "user_active",
        "assignment_type", "clinic_id", "clinic_name", "clinic_city", "clinic_state",
        "clinic_tier", "clinic_active", "user_created_at",
    ])

    def _write(u, cid, kind):
        c = cmap.get(cid) or {}
        # NAV-007 · B6 · Derive clinic_active from `status` instead of
        # the phantom `active` field. Legacy rows with missing status
        # continue to render as `yes` (correct — they've been operational
        # for months).
        _status = c.get("status")
        _active = "no" if _status in {"inactive", "suspended"} else "yes"
        writer.writerow([
            u.get("user_id", ""), u.get("email", ""), u.get("name", ""),
            u.get("role", ""), "yes" if u.get("active", True) else "no",
            kind, cid or "", c.get("name", ""),
            c.get("city", ""), c.get("state", ""),
            c.get("subscription_tier", ""),
            _active,
            u.get("created_at", ""),
        ])

    for u in users:
        primary = u.get("clinic_id")
        if primary:
            _write(u, primary, "primary")
        for cid in u.get("additional_clinic_ids", []) or []:
            _write(u, cid, "additional")

    filename = f"clinic-assignments-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.csv"
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/clinics-directory")
async def clinics_directory(
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Flat clinic list for the 'Link clinic' autocomplete in Assignments UI."""
    if user["role"] not in {"founder", "super_admin"}:
        raise HTTPException(403, detail="Not permitted")
    rows = await db.clinics.find(
        {},
        {"_id": 0, "clinic_id": 1, "name": 1, "city": 1, "state": 1, "subscription_tier": 1, "active": 1},
    ).sort("name", 1).to_list(500)
    return rows


# ==================== 16. CLINIC SWITCH AUDIT (compliance trail) ====================

@router.get("/clinic-switch-audit")
async def clinic_switch_audit(
    user_id: Optional[str] = None,
    clinic_id: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 300,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Chronological view of every `/auth/switch-clinic` call.

    Filters: `user_id` (exact), `clinic_id` (either from- or to-clinic),
    `since` (ISO-8601 lower bound on `at`). Sorted newest-first.
    """
    if user["role"] not in {"founder", "super_admin"}:
        raise HTTPException(403, detail="Not permitted")

    q: dict = {}
    if user_id:
        q["user_id"] = user_id
    if clinic_id:
        q["$or"] = [
            {"from_clinic_id": clinic_id},
            {"to_clinic_id": clinic_id},
        ]
    if since:
        q["at"] = {"$gte": since}

    rows = await db.clinic_switch_audit.find(
        q, {"_id": 0},
    ).sort("at", -1).to_list(limit)

    # Aggregate: how many distinct users + top movers
    by_user: dict[str, int] = {}
    for r in rows:
        by_user[r.get("user_email") or r.get("user_id")] = by_user.get(r.get("user_email") or r.get("user_id"), 0) + 1
    top_movers = sorted(by_user.items(), key=lambda kv: -kv[1])[:10]

    return {
        "count": len(rows),
        "rows": rows,
        "distinct_users": len(by_user),
        "top_movers": [{"user": k, "switch_count": v} for k, v in top_movers],
    }



@router.get("/clinic-switch-audit/export.csv")
async def export_clinic_switch_audit_csv(
    user_id: Optional[str] = None,
    clinic_id: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 5000,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """CSV dump of the switch-audit trail for compliance hand-off.

    Accepts the same filters as the JSON endpoint, but caps the default
    at 5000 rows (Mongo cursor hard-cap 50k) so a single export is
    downloadable without streaming chunked responses.
    """
    if user["role"] not in {"founder", "super_admin"}:
        raise HTTPException(403, detail="Not permitted")

    import csv
    import io
    from fastapi.responses import StreamingResponse

    q: dict = {}
    if user_id:
        q["user_id"] = user_id
    if clinic_id:
        q["$or"] = [
            {"from_clinic_id": clinic_id},
            {"to_clinic_id": clinic_id},
        ]
    if since:
        q["at"] = {"$gte": since}

    rows = await db.clinic_switch_audit.find(
        q, {"_id": 0},
    ).sort("at", -1).to_list(min(max(limit, 1), 50000))

    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
    writer.writerow([
        "at", "audit_id", "user_id", "user_email", "user_role",
        "from_clinic_id", "from_clinic_name",
        "to_clinic_id", "to_clinic_name",
        "ip", "user_agent",
    ])
    for r in rows:
        writer.writerow([
            r.get("at", ""), r.get("audit_id", ""),
            r.get("user_id", ""), r.get("user_email", ""), r.get("user_role", ""),
            r.get("from_clinic_id", ""), r.get("from_clinic_name", ""),
            r.get("to_clinic_id", ""), r.get("to_clinic_name", ""),
            r.get("ip", ""), r.get("user_agent", ""),
        ])

    filename = f"clinic-switch-audit-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.csv"
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
