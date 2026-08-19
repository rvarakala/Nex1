"""
Authentication + RBAC + multi-tenant scoping for ACS.
- JWT (HS256) with Bearer token in Authorization header (frontend stores in localStorage).
- bcrypt for password hashing.
- Roles: super_admin, front_desk, audiologist, accounts.
- Tenant scoping: every authenticated request carries `clinic_id` in JWT claims.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import bcrypt
import jwt
from fastapi import HTTPException, Request, status

JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_TTL = timedelta(hours=12)  # front-desk runs all day; 12h is pragmatic for this sprint

# bcrypt silently truncates input longer than 72 bytes — a 100-char password
# would auth-equivalent to its first 72 bytes, which is a real auth-bypass
# vector. We enforce the cap at the hashing boundary so EVERY caller (login,
# reset-password, admin reset, seeds) is protected, regardless of whether
# their Pydantic model added max_length.
MAX_PASSWORD_BYTES = 72

VALID_ROLES = {
    "super_admin", "clinic_owner", "front_desk", "audiologist",
    "accounts", "inventory_manager", "technician", "referral_partner",
    "founder",
    # Phase 14C granular internal-team roles
    "sales_manager", "support_agent", "finance_manager", "product_ops", "read_only",
}
# Roles that see every branch of a clinic; everyone else is branch-scoped.
CLINIC_WIDE_ROLES = {"super_admin", "clinic_owner", "accounts", "founder"}


def _jwt_secret() -> str:
    s = os.environ.get("JWT_SECRET")
    if not s:
        raise RuntimeError("JWT_SECRET not configured")
    return s


def hash_password(pw: str) -> str:
    """Return a bcrypt hash for `pw`.

    Cost is 10 by default (industry standard for 2026 — Django's default too).
    Each hash takes ~55ms — the sweet spot between brute-force resistance
    (2^10 = 1024 rounds) and login speed under load. Override via env
    `BCRYPT_ROUNDS` if a specific compliance framework demands 12+.
    Old cost-12 hashes remain valid — bcrypt stores the cost inside the
    hash string and `checkpw` reads it from there.
    """
    if len(pw.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"Password is too long. Please use {MAX_PASSWORD_BYTES} characters or fewer.",
        )
    rounds = int(os.environ.get("BCRYPT_ROUNDS", "10"))
    return bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt(rounds=rounds)).decode("utf-8")


def verify_password(pw: str, hashed: str) -> bool:
    try:
        # Reject inputs longer than bcrypt's 72-byte limit so an attacker can't
        # bypass auth with a 100-char password whose first 72 bytes match.
        # Existing accounts unaffected — their stored hashes were created from
        # passwords already <= 72 bytes (older code never enforced this, but
        # passwords longer than 72 are extremely rare in practice).
        if len(pw.encode("utf-8")) > MAX_PASSWORD_BYTES:
            return False
        return bcrypt.checkpw(pw.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def create_access_token(
    user_id: str,
    email: str,
    role: str,
    clinic_id: str,
    token_version: int = 0,
    session_id: Optional[str] = None,
) -> str:
    """Mint a signed JWT.

    `session_id` is embedded as the `sid` claim. When present, every
    authenticated request looks up the session row and refuses tokens whose
    session has been revoked. Pass `None` for legacy callers (the resulting
    token works but cannot be individually revoked).
    """
    payload = {
        "sub": user_id,
        "email": email,
        "role": role,
        "clinic_id": clinic_id,
        "tv": int(token_version or 0),  # token version — incremented to force-logout all sessions
        "exp": datetime.now(timezone.utc) + ACCESS_TOKEN_TTL,
        "type": "access",
    }
    if session_id:
        payload["sid"] = session_id
    return jwt.encode(payload, _jwt_secret(), algorithm=JWT_ALGORITHM)


# ──────────────────────────────────────────────────────────────────────
# MFA enforcement for platform admins (super_admin + founder)
# ──────────────────────────────────────────────────────────────────────
#
# Platform admins can read/write every clinic on the platform. We give them
# a 7-day grace from "first sighting" to enable 2FA, then refuse every
# non-MFA endpoint until they do. A stolen super_admin password
# therefore compromises *nothing* once the grace has elapsed.

MFA_ENFORCED_ROLES = {"super_admin", "founder"}
MFA_GRACE_DAYS = 7

# ──────────────────────────────────────────────────────────────────────
# NAV-007 · Multi-Branch deactivation gate
# ──────────────────────────────────────────────────────────────────────
# When a branch clinic is deactivated (POST /api/clinic-groups/mine/
# branches/{id}/deactivate) or a whole tenant is suspended by the
# founder (POST /api/admin-panel/tenants/{id}/suspend), the clinic doc
# receives `status="inactive"` or `status="suspended"`. Every
# authenticated request that resolves to such a clinic must be rejected
# 401, regardless of how the JWT was minted or how recent the session
# is. Enforced centrally so every FastAPI dependency transitively
# depending on get_current_user() inherits the check.
#
# Legacy tolerance is CRITICAL: the audit found 14/23 preview clinics
# with no `status` field at all (pre-2026 rows). Missing/None must PASS
# — do NOT switch this to an active-only whitelist.
_INACTIVE_CLINIC_STATUSES = {"inactive", "suspended"}

# Break-glass kill switch. NOT user-configurable, NOT exposed in the
# frontend, NOT documented as a normal config option. Set only via a
# manual pod-env edit in a security incident. When enabled the central
# gate below becomes a no-op AND an explicit ERROR log fires on every
# authenticated request so ops can spot accidental enablement fast.
# Env var name deliberately verbose so it never appears in a normal
# .env file by accident.
def _multi_branch_inactive_enforcement_disabled() -> bool:
    import os
    return os.environ.get("MULTI_BRANCH_INACTIVE_ENFORCEMENT_DISABLED") == "1"


async def _reject_if_clinic_inactive(db, clinic_id: str) -> None:
    """Raise 401 when the caller's active clinic is inactive/suspended.

    Status handling (per NAV-007 approved plan):
      - "active"           → PASS
      - missing / None     → PASS  (legacy pre-status rows)
      - "inactive"         → 401
      - "suspended"        → 401
      - any other value    → PASS  (conservative — never lock users out
                                     on typos or future statuses)

    Break-glass: if MULTI_BRANCH_INACTIVE_ENFORCEMENT_DISABLED=1 is set
    in the environment, the check is skipped AND a loud error is logged
    so accidental enablement is visible in ops dashboards.
    """
    if _multi_branch_inactive_enforcement_disabled():
        import logging
        logging.getLogger(__name__).error(
            "SECURITY: NAV-007 inactive-clinic enforcement is DISABLED via "
            "MULTI_BRANCH_INACTIVE_ENFORCEMENT_DISABLED env var. "
            "Deactivated branches are currently accessible. "
            "Re-enable IMMEDIATELY unless actively debugging a lockout."
        )
        return
    doc = await db.clinics.find_one(
        {"clinic_id": clinic_id},
        {"_id": 0, "status": 1},
    )
    if doc and doc.get("status") in _INACTIVE_CLINIC_STATUSES:
        raise HTTPException(
            status_code=401,
            detail="This clinic is no longer active. Please contact your head clinic.",
        )

# 2026-06-03 — operator-controlled kill switch for the 2FA grace-period
# enforcement. The founder was repeatedly running into the post-grace 403
# while we iterated on production, and the dict-shaped error payload was
# triggering a React render crash (error #31). Setting
# `MFA_ENFORCEMENT_DISABLED=1` in `backend/.env` short-circuits the
# check below — all platform admins can hit every endpoint without 2FA.
# Re-enable in production once a permanent 2FA UX is shipped and the
# founder has set up TOTP. Default remains enforce-on.
#
# IMPORTANT: read the env var inside the function (NOT at module-load),
# because auth.py loads before server.py calls `load_dotenv`. A
# module-load read here would miss the .env file every time.
def _mfa_enforcement_disabled() -> bool:
    import os
    return os.environ.get("MFA_ENFORCEMENT_DISABLED") == "1"

# Paths a blocked platform-admin can still hit, so they can enable 2FA + see
# who they are + log out. Everything else returns 403.
_MFA_ENFORCEMENT_ALLOWLIST_PREFIXES = (
    "/api/mfa/",                  # /setup/init, /setup/verify, /disable, /status
    "/api/auth/mfa/verify-login",
    "/api/auth/me",
    "/api/auth/logout",
    "/api/auth/switch-clinic",    # harmless and read-only-ish
    "/api/health",
    "/api/_telemetry/",           # let the UI keep reporting JS crashes
)


def _is_path_mfa_setup_only(path: str) -> bool:
    return any(path == p or path.startswith(p) for p in _MFA_ENFORCEMENT_ALLOWLIST_PREFIXES)


async def _mfa_enforcement_check(db, user: dict, request: Request) -> dict:
    """Returns `{required, enabled, grace_days_left, blocked, must_enable_by}`.

    Side effects: lazily stamps `mfa_grace_started_at` on the user doc the
    first time we see a platform admin without 2FA.

    Raises 403 when the user is past grace and the path isn't on the
    allowlist of "setup-only" endpoints.
    """
    # Operator-controlled kill switch — see _mfa_enforcement_disabled() docstring.
    if _mfa_enforcement_disabled():
        return {
            "required": False,
            "enabled": bool(user.get("mfa_enabled")),
            "blocked": False,
            "grace_days_left": None,
            "enforcement_disabled": True,
        }

    role = user.get("role")
    if role not in MFA_ENFORCED_ROLES:
        return {"required": False, "enabled": bool(user.get("mfa_enabled")), "blocked": False}

    if user.get("mfa_enabled"):
        return {"required": True, "enabled": True, "blocked": False, "grace_days_left": None}

    now = datetime.now(timezone.utc)
    started_iso = user.get("mfa_grace_started_at")
    if not started_iso:
        # First sighting — stamp the start of the grace window.
        await db.users.update_one(
            {"user_id": user["user_id"]},
            {"$set": {"mfa_grace_started_at": now.isoformat()}},
        )
        started = now
    else:
        try:
            started = datetime.fromisoformat(str(started_iso).replace("Z", "+00:00"))
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
        except (ValueError, AttributeError):
            started = now

    elapsed = now - started
    grace_left = max(0, MFA_GRACE_DAYS - elapsed.days)
    must_enable_by = (started + timedelta(days=MFA_GRACE_DAYS)).isoformat()
    blocked = elapsed >= timedelta(days=MFA_GRACE_DAYS)

    if blocked and not _is_path_mfa_setup_only(request.url.path):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "MFA_ENFORCEMENT_REQUIRED",
                "message": (
                    "Two-factor authentication is mandatory for platform admin "
                    "accounts. The 7-day grace period has elapsed — enable 2FA "
                    "in Settings → Security & Privacy to continue."
                ),
                "must_enable_by": must_enable_by,
            },
        )

    return {
        "required": True,
        "enabled": False,
        "blocked": blocked,
        "grace_days_left": grace_left,
        "must_enable_by": must_enable_by,
    }


def _extract_token(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[7:]
    # Cookie fallback (optional)
    cookie = request.cookies.get("access_token")
    if cookie:
        return cookie
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")


def decode_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    if payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="Invalid token type")
    return payload


async def get_current_user(request: Request):
    """FastAPI dependency — returns dict: {user_id, email, role, clinic_id}.

    The DB existence check is done once per request so revoked users are rejected.
    Also updates user's last-seen heartbeat (throttled to 1 write/min per user).

    Multi-clinic: the JWT's `clinic_id` claim is the *active* clinic. We check
    that the user has access to it either as their primary clinic or via
    `additional_clinic_ids`. The returned dict's `clinic_id` is the JWT's
    active one — so every downstream tenant-scoped query just works.
    """
    token = _extract_token(request)
    payload = decode_token(token)
    db = request.app.state.db
    user = await db.users.find_one({"user_id": payload["sub"]}, {"_id": 0, "password_hash": 0})
    if not user or not user.get("active", True):
        raise HTTPException(status_code=401, detail="User not found or inactive")

    # Multi-clinic: accept the token's clinic_id if it's the primary OR one of
    # the user's granted additional clinics (set by super_admin via Settings).
    allowed: set = {user.get("clinic_id")}
    for cid in user.get("additional_clinic_ids", []) or []:
        allowed.add(cid)
    if payload.get("clinic_id") not in allowed:
        raise HTTPException(status_code=401, detail="Tenant mismatch")

    # NAV-007 · Reject if the caller's active clinic has been deactivated
    # or suspended. Runs BEFORE token_version/session checks so that a
    # revoked-plus-reactivated user cannot exploit a race; runs AFTER the
    # allowlist check so we never leak the existence of a clinic the user
    # never had access to.
    await _reject_if_clinic_inactive(db, payload["clinic_id"])

    # Force-logout check: if user's token_version was bumped after this token
    # was issued, reject (user must re-login)
    current_tv = int(user.get("token_version", 0) or 0)
    token_tv = int(payload.get("tv", 0) or 0)
    if token_tv < current_tv:
        raise HTTPException(status_code=401, detail="Session revoked, please sign in again")

    # ── Per-session revocation ──
    # Tokens minted before per-session tracking shipped have no `sid` claim
    # and stay valid until they expire (legacy compatibility). Newer tokens
    # carry `sid` → we look up the session row and refuse if it was revoked.
    sid = payload.get("sid")
    if sid:
        sess = await db.user_sessions.find_one(
            {"session_id": sid, "user_id": user["user_id"]},
            {"_id": 0, "revoked_at": 1},
        )
        if sess and sess.get("revoked_at"):
            raise HTTPException(status_code=401, detail="This sign-in was revoked")

    # ── MFA enforcement for platform admins (super_admin + founder) ──
    # We give them a 7-day grace window from first authenticated request,
    # then block all non-MFA traffic until they enable 2FA.
    enforcement = await _mfa_enforcement_check(db, user, request)

    # Heartbeat — fire-and-forget, never blocks the request
    try:
        from utils.activity import record_heartbeat
        await record_heartbeat(db, user["user_id"], request, session_id=sid)
    except Exception:
        pass
    user_ctx = {
        "user_id": user["user_id"],
        "email": user["email"],
        "name": user.get("name", ""),
        "role": user["role"],
        "clinic_id": payload["clinic_id"],  # ← active clinic from JWT, not user.clinic_id
        "primary_clinic_id": user.get("clinic_id"),
        "additional_clinic_ids": list(user.get("additional_clinic_ids", []) or []),
        "branch_ids": user.get("branch_ids", []) or [],
        "active": user.get("active", True),
        "signature_image_fs_id": user.get("signature_image_fs_id"),
        "seal_image_fs_id": user.get("seal_image_fs_id"),
        "seal_include_on": list(user.get("seal_include_on") or []),
        "license_no": user.get("license_no"),
        "can_access_referrals": bool(user.get("can_access_referrals")),
        "appointment_color": user.get("appointment_color"),
        "mfa_enforcement": enforcement,
        "session_id": sid,
    }
    # Stash on `request.state` so the global error-logger middleware can
    # correlate any crashes raised AFTER auth succeeded (e.g. response
    # validation errors, NPEs in the route body) to the actual user.
    try:
        request.state.user = user_ctx
    except Exception:
        pass
    return user_ctx


def require_roles(*roles: str):
    """Returns a FastAPI dependency that enforces one of the given roles.
    `super_admin` and `founder` always bypass every role gate in the codebase.
    """
    async def checker(request: Request):
        user = await get_current_user(request)
        if user["role"] not in set(roles) | {"super_admin", "founder"}:
            raise HTTPException(status_code=403, detail=f"Requires one of: {roles}")
        return user
    return checker


def user_can_see_branch(user: dict, branch_id: str) -> bool:
    """Clinic-wide roles see every branch of their clinic; everyone else must
    have the branch explicitly in their `branch_ids` list."""
    if user.get("role") in CLINIC_WIDE_ROLES:
        return True
    return branch_id in (user.get("branch_ids") or [])


def assert_branch_access(user: dict, branch_id: str) -> None:
    """Raises 403 if the user cannot act on `branch_id`."""
    if not user_can_see_branch(user, branch_id):
        raise HTTPException(status_code=403, detail="Branch access denied")
