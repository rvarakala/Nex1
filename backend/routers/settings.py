"""Clinic Settings router — Feb 2026 (Phase 1).

Consolidated endpoints used by the new `/settings` UI for clinic owners:

    GET  /api/settings/clinic            — full clinic record
    PUT  /api/settings/clinic            — update name / address / GSTIN / etc.
    POST /api/settings/clinic/logo       — upload logo (PNG/JPG/SVG, ≤2 MB)
    GET  /api/settings/clinic/logo       — stream stored logo
    POST /api/settings/staff             — create a new staff account
                                           (auto-generates password, returns it so
                                            the UI can show/email it to the user)
    PUT  /api/settings/staff/{user_id}   — update name / role / branch access / active
    POST /api/settings/staff/{user_id}/reset-password — generate + return a new temp password
    POST /api/settings/staff/{user_id}/force-logout   — bump token_version → kick them out

Branch CRUD already exists in `routers/branches.py` — we re-expose the same
endpoints under /api/settings/* is not needed; the Settings UI calls the
existing /api/branches endpoints directly.
"""
from __future__ import annotations

import io
import asyncio
import secrets
import string
from datetime import datetime, timezone
from typing import List, Literal, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse
from motor.motor_asyncio import AsyncIOMotorGridFSBucket
from pydantic import BaseModel, Field as pyField

from auth import get_current_user, hash_password, require_roles
from database import get_db
from models import User
from utils.serde import serialize_datetime, deserialize_datetime


router = APIRouter(prefix="/api/settings", tags=["settings"])

_ALLOWED_MIMES = {"image/png", "image/jpeg", "image/jpg", "image/svg+xml"}
_MAX_LOGO_BYTES = 2 * 1024 * 1024  # 2 MB


# ---------- Clinic details ----------
class ClinicUpdate(BaseModel):
    name: Optional[str] = None
    tagline: Optional[str] = None                                    # 1-line motto rendered on reports / print
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    pincode: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    website: Optional[str] = None
    gstin: Optional[str] = None
    pan: Optional[str] = None
    # Typography overrides. Applied via inline style on the report /
    # template root — takes effect for both on-screen preview AND the
    # PDF capture (html2canvas). Falls back to Arial when unset.
    report_font: Optional[str] = None
    template_font: Optional[str] = None


@router.get("/clinic")
async def get_clinic(user=Depends(get_current_user), db=Depends(get_db)):
    c = await db.clinics.find_one({"clinic_id": user["clinic_id"]}, {"_id": 0})
    if not c:
        raise HTTPException(status_code=404, detail="Clinic not found")
    return deserialize_datetime(c)


@router.put("/clinic")
async def update_clinic(
    payload: ClinicUpdate,
    user=Depends(require_roles("clinic_owner", "super_admin")),
    db=Depends(get_db),
):
    patch = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not patch:
        raise HTTPException(status_code=400, detail="No fields to update")
    patch["updated_at"] = datetime.now(timezone.utc).isoformat()
    res = await db.clinics.update_one(
        {"clinic_id": user["clinic_id"]}, {"$set": patch}
    )
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Clinic not found")
    c = await db.clinics.find_one({"clinic_id": user["clinic_id"]}, {"_id": 0})
    return deserialize_datetime(c)


# ---------- Clinic logo (GridFS) ----------
@router.post("/clinic/logo")
async def upload_clinic_logo(
    file: UploadFile = File(...),
    user=Depends(require_roles("clinic_owner", "super_admin")),
    db=Depends(get_db),
):
    if file.content_type not in _ALLOWED_MIMES:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported type {file.content_type}. Use PNG, JPG, or SVG.",
        )
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(raw) > _MAX_LOGO_BYTES:
        raise HTTPException(status_code=413, detail="Logo too large (max 2 MB)")

    bucket = AsyncIOMotorGridFSBucket(db, bucket_name="clinic_logos")

    # Remove previous logo for this clinic (idempotent).
    c = await db.clinics.find_one({"clinic_id": user["clinic_id"]}, {"_id": 0})
    old = (c or {}).get("logo_fs_id")
    if old:
        try:
            await bucket.delete(ObjectId(old))
        except Exception:
            pass  # missing/orphan is fine

    fs_id = await bucket.upload_from_stream(
        filename=file.filename or "logo",
        source=raw,
        metadata={
            "clinic_id": user["clinic_id"],
            "content_type": file.content_type,
            "size_bytes": len(raw),
            "uploaded_by_user_id": user["user_id"],
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    await db.clinics.update_one(
        {"clinic_id": user["clinic_id"]},
        {"$set": {
            "logo_fs_id": str(fs_id),
            "logo_mime": file.content_type,
            "logo_updated_at": datetime.now(timezone.utc).isoformat(),
        }},
    )
    return {"ok": True, "logo_fs_id": str(fs_id), "size_bytes": len(raw)}


@router.get("/clinic/logo")
async def get_clinic_logo(user=Depends(get_current_user), db=Depends(get_db)):
    c = await db.clinics.find_one({"clinic_id": user["clinic_id"]}, {"_id": 0})
    if not c or not c.get("logo_fs_id"):
        raise HTTPException(status_code=404, detail="No logo set")
    bucket = AsyncIOMotorGridFSBucket(db, bucket_name="clinic_logos")
    try:
        stream = await bucket.open_download_stream(ObjectId(c["logo_fs_id"]))
        raw = await stream.read()
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Logo not found: {e}")
    return StreamingResponse(
        io.BytesIO(raw),
        media_type=c.get("logo_mime") or "image/png",
        headers={"Cache-Control": "private, max-age=300"},
    )


# ---------- Staff ----------
_STAFF_ROLES = ("clinic_owner", "front_desk", "audiologist", "accounts",
                "inventory_manager", "technician")

_EMAIL_ALPHABET = string.ascii_letters + string.digits
def _gen_password(n: int = 12) -> str:
    """Generate a URL-safe temp password: 12 mixed case + digits."""
    return "".join(secrets.choice(_EMAIL_ALPHABET) for _ in range(n))


class StaffCreate(BaseModel):
    name: str
    email: str
    role: Literal["clinic_owner", "front_desk", "audiologist", "accounts",
                  "inventory_manager", "technician"]
    branch_ids: List[str] = []
    phone: Optional[str] = None
    can_access_referrals: bool = False


class StaffUpdate(BaseModel):
    name: Optional[str] = None
    role: Optional[Literal["clinic_owner", "front_desk", "audiologist", "accounts",
                           "inventory_manager", "technician"]] = None
    branch_ids: Optional[List[str]] = None
    phone: Optional[str] = None
    active: Optional[bool] = None
    # When True, this user can view the Referral Corner dashboard even
    # without being a clinic_owner / super_admin. Editing payout terms
    # still requires owner role. Default OFF — explicit grants only.
    can_access_referrals: Optional[bool] = None


async def _log_mock_email(db, clinic_id: str, to: str, subject: str, body: str, kind: str = "staff_welcome"):
    """Store the email payload in mongo + server log so a real SMTP provider
    can retro-fire them once integrated."""
    await db.email_outbox.insert_one({
        "clinic_id": clinic_id,
        "to": to,
        "subject": subject,
        "body": body,
        "kind": kind,
        "status": "mocked",  # flip to `sent` once real delivery lands
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    # Also print to server log for debugging / demo.
    import logging
    logging.getLogger("settings.mock_email").info(
        f"[MOCK-EMAIL clinic={clinic_id}] TO={to} SUBJECT={subject}"
    )


@router.post("/staff")
async def create_staff(
    payload: StaffCreate,
    user=Depends(require_roles("clinic_owner", "super_admin")),
    db=Depends(get_db),
):
    # Email uniqueness check (per-clinic — same email across different tenants is fine).
    email = payload.email.strip().lower()
    existing = await db.users.find_one(
        {"clinic_id": user["clinic_id"], "email": email}, {"_id": 0, "user_id": 1},
    )
    if existing:
        raise HTTPException(status_code=409, detail="Email already exists in this clinic")

    temp_password = _gen_password()
    now = datetime.now(timezone.utc).isoformat()
    new_user = User(
        clinic_id=user["clinic_id"],
        email=email,
        name=payload.name.strip(),
        role=payload.role,
        branch_ids=payload.branch_ids or [],
    )
    doc = serialize_datetime(new_user.model_dump())
    doc["password_hash"] = hash_password(temp_password)
    doc["must_change_password"] = True
    if payload.phone:
        doc["phone"] = payload.phone
    doc["created_at"] = now
    await db.users.insert_one(doc)

    # Mock-email the welcome / credentials (MOCKED until real SMTP).
    subject = "Welcome to AUDINEXA — your staff account is ready"
    body = (
        f"Hi {payload.name},\n\n"
        f"Your AUDINEXA account was created by your clinic admin.\n\n"
        f"  Login:    {email}\n"
        f"  Password: {temp_password}\n\n"
        f"Please sign in at the AUDINEXA portal and change your password on first login.\n"
    )
    await _log_mock_email(db, user["clinic_id"], email, subject, body)

    return {
        "user": deserialize_datetime({**new_user.model_dump(),
                                      "must_change_password": True,
                                      "phone": payload.phone}),
        "temp_password": temp_password,
        "email_status": "mocked",  # UI should show "password emailed (MOCKED)"
    }


@router.put("/staff/{user_id}")
async def update_staff(
    user_id: str, payload: StaffUpdate,
    user=Depends(require_roles("clinic_owner", "super_admin")),
    db=Depends(get_db),
):
    target = await db.users.find_one(
        {"user_id": user_id, "clinic_id": user["clinic_id"]}, {"_id": 0},
    )
    if not target:
        raise HTTPException(status_code=404, detail="Staff member not found")

    patch = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not patch:
        raise HTTPException(status_code=400, detail="No fields to update")
    patch["updated_at"] = datetime.now(timezone.utc).isoformat()
    await db.users.update_one({"user_id": user_id}, {"$set": patch})
    updated = await db.users.find_one(
        {"user_id": user_id}, {"_id": 0, "password_hash": 0},
    )
    return deserialize_datetime(updated)


@router.post("/staff/{user_id}/reset-password")
async def reset_password(
    user_id: str,
    user=Depends(require_roles("clinic_owner", "super_admin")),
    db=Depends(get_db),
):
    target = await db.users.find_one(
        {"user_id": user_id, "clinic_id": user["clinic_id"]}, {"_id": 0},
    )
    if not target:
        raise HTTPException(status_code=404, detail="Staff member not found")

    temp_password = _gen_password()
    await db.users.update_one(
        {"user_id": user_id},
        {"$set": {
            "password_hash": hash_password(temp_password),
            "must_change_password": True,
            "token_version": int(target.get("token_version") or 0) + 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }},
    )

    subject = "Your AUDINEXA password was reset"
    body = (
        f"Hi {target.get('name')},\n\n"
        f"Your clinic admin has issued a new temporary password.\n\n"
        f"  Login:    {target.get('email')}\n"
        f"  Password: {temp_password}\n\n"
        f"Please sign in and change it immediately.\n"
    )
    await _log_mock_email(db, user["clinic_id"], target.get("email"), subject, body,
                          kind="staff_reset_password")

    return {"ok": True, "temp_password": temp_password, "email_status": "mocked"}


@router.post("/staff/{user_id}/force-logout")
async def force_logout_staff(
    user_id: str,
    user=Depends(require_roles("clinic_owner", "super_admin")),
    db=Depends(get_db),
):
    target = await db.users.find_one(
        {"user_id": user_id, "clinic_id": user["clinic_id"]}, {"_id": 0},
    )
    if not target:
        raise HTTPException(status_code=404, detail="Staff member not found")
    await db.users.update_one(
        {"user_id": user_id},
        {"$inc": {"token_version": 1},
         "$set": {"updated_at": datetime.now(timezone.utc).isoformat()}},
    )
    return {"ok": True, "message": "User's active sessions invalidated"}



# ============================================================================
# Personal signature — every authenticated user can upload one.
# Used by:
#   • Audiogram report footer (auto-applied for the signing audiologist)
#   • Delivery-challan receipt (legacy alternative to drawing on the receive modal)
#
# Bucket: `user_signatures`. We strip the `data:image/png;base64,` prefix when
# the client sends a data-URL; raw uploads also work.
# ============================================================================
_SIG_BUCKET = "user_signatures"
_MAX_SIG_BYTES = 1_500_000  # 1.5 MB — drawn PNGs are small; reject pasted photos


class SignaturePayload(BaseModel):
    """JSON body for canvas-drawn signatures. The client sends the base64 PNG
    inline (data-URL or raw base64). We avoid multipart for this path because
    the canvas-pad already produces a base64 string."""
    image_base64: str
    license_no: Optional[str] = None


@router.post("/me/signature")
async def upload_my_signature(payload: SignaturePayload,
                              user=Depends(get_current_user), db=Depends(get_db)):
    import base64
    raw = payload.image_base64 or ""
    # Strip data-URL prefix if present.
    if "," in raw and raw.lower().startswith("data:"):
        raw = raw.split(",", 1)[1]
    try:
        blob = base64.b64decode(raw, validate=True)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid base64 PNG: {e}")
    if not blob:
        raise HTTPException(status_code=400, detail="Empty signature")
    if len(blob) > _MAX_SIG_BYTES:
        raise HTTPException(status_code=413, detail="Signature too large (max 1.5 MB)")

    bucket = AsyncIOMotorGridFSBucket(db, bucket_name=_SIG_BUCKET)
    udoc = await db.users.find_one({"user_id": user["user_id"]}, {"_id": 0, "signature_image_fs_id": 1}) or {}
    if udoc.get("signature_image_fs_id"):
        try:
            await bucket.delete(ObjectId(udoc["signature_image_fs_id"]))
        except Exception:
            pass
    fs_id = await bucket.upload_from_stream(
        f"sig-{user['user_id']}.png",
        io.BytesIO(blob),
        metadata={"user_id": user["user_id"], "kind": "user-signature"},
    )
    update = {
        "signature_image_fs_id": str(fs_id),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if payload.license_no is not None:
        update["license_no"] = payload.license_no.strip() or None
    await db.users.update_one({"user_id": user["user_id"]}, {"$set": update})
    return {
        "signature_image_fs_id": str(fs_id),
        "license_no": update.get("license_no"),
    }


@router.delete("/me/signature")
async def clear_my_signature(user=Depends(get_current_user), db=Depends(get_db)):
    udoc = await db.users.find_one({"user_id": user["user_id"]}, {"_id": 0, "signature_image_fs_id": 1}) or {}
    if udoc.get("signature_image_fs_id"):
        bucket = AsyncIOMotorGridFSBucket(db, bucket_name=_SIG_BUCKET)
        try:
            await bucket.delete(ObjectId(udoc["signature_image_fs_id"]))
        except Exception:
            pass
    await db.users.update_one(
        {"user_id": user["user_id"]},
        {"$set": {"signature_image_fs_id": None, "updated_at": datetime.now(timezone.utc).isoformat()}},
    )
    return {"ok": True}


@router.get("/users/{user_id}/signature")
async def fetch_user_signature(user_id: str,
                               user=Depends(get_current_user), db=Depends(get_db)):
    """Same-tenant fetch — used by the audiogram footer to embed the signing
    audiologist's signature. 404 cleanly when no signature is set so the UI
    can fall back to the typed name."""
    udoc = await db.users.find_one(
        {"user_id": user_id, "clinic_id": user["clinic_id"]},
        {"_id": 0, "signature_image_fs_id": 1, "license_no": 1, "name": 1},
    )
    if not udoc or not udoc.get("signature_image_fs_id"):
        raise HTTPException(status_code=404, detail="No signature on file")
    bucket = AsyncIOMotorGridFSBucket(db, bucket_name=_SIG_BUCKET)
    try:
        stream = await bucket.open_download_stream(ObjectId(udoc["signature_image_fs_id"]))
        data = await stream.read()
    except Exception:
        raise HTTPException(status_code=404, detail="Signature blob missing")
    from fastapi.responses import Response
    return Response(content=data, media_type="image/png", headers={
        "X-License-No": udoc.get("license_no") or "",
        "X-Signed-By": udoc.get("name") or "",
        "Cache-Control": "private, max-age=300",
    })



# ============================================================================
# Personal seal / stamp — every authenticated user can upload one.
#
# Mirrors the signature pattern (same shape: POST/DELETE on /me/seal, GET on
# /users/{user_id}/seal) but accepts a pre-made image (uploaded PNG/JPEG/WEBP)
# instead of a canvas-drawn data-URL — because users typically already have a
# designed or scanned seal artwork rather than drawing one freehand.
#
# Bucket: `user_seals`. Max 3 MB (slightly larger than signatures because
# detailed seal artwork is heavier than a quick signature trace).
# ============================================================================
_SEAL_BUCKET = "user_seals"
_MAX_SEAL_BYTES = 3_000_000  # 3 MB
_ALLOWED_SEAL_MIME = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
}


class SealPayload(BaseModel):
    """JSON body for seal uploads. The client sends the base64 image (data-URL
    or raw base64). Multipart is also supported via a separate endpoint below
    in case the UI wants a true file picker without doing the base64 dance."""
    image_base64: str


@router.post("/me/seal")
async def upload_my_seal(payload: SealPayload,
                         user=Depends(get_current_user), db=Depends(get_db)):
    """Upload (or replace) the current user's official seal / stamp.

    Accepts a base64-encoded PNG / JPEG / WEBP. The previous seal blob (if
    any) is deleted from GridFS before the new one is stored — keeping the
    bucket tight. Returns the new GridFS id.
    """
    import base64
    raw = payload.image_base64 or ""

    # Sniff mime from the data-URL prefix when available; this lets us reject
    # garbage uploads (e.g. PDFs renamed to .png) before they hit GridFS.
    mime = "image/png"
    if "," in raw and raw.lower().startswith("data:"):
        header, raw = raw.split(",", 1)
        # header looks like 'data:image/png;base64'
        try:
            mime = header.split(":", 1)[1].split(";", 1)[0].strip().lower()
        except Exception:
            mime = "image/png"
    if mime not in _ALLOWED_SEAL_MIME:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported image type. Allowed: PNG, JPEG, WEBP. Got: {mime}",
        )
    try:
        blob = base64.b64decode(raw, validate=True)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid base64 image: {e}")
    if not blob:
        raise HTTPException(status_code=400, detail="Empty seal upload")
    if len(blob) > _MAX_SEAL_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Seal image too large (max {_MAX_SEAL_BYTES // 1_000_000} MB)",
        )

    bucket = AsyncIOMotorGridFSBucket(db, bucket_name=_SEAL_BUCKET)
    udoc = await db.users.find_one(
        {"user_id": user["user_id"]},
        {"_id": 0, "seal_image_fs_id": 1},
    ) or {}
    if udoc.get("seal_image_fs_id"):
        try:
            await bucket.delete(ObjectId(udoc["seal_image_fs_id"]))
        except Exception:
            pass  # orphan blob is harmless; we move on.

    ext = _ALLOWED_SEAL_MIME[mime]
    fs_id = await bucket.upload_from_stream(
        f"seal-{user['user_id']}.{ext}",
        io.BytesIO(blob),
        metadata={
            "user_id": user["user_id"],
            "clinic_id": user.get("clinic_id"),
            "kind": "user-seal",
            "mime": mime,
        },
    )
    await db.users.update_one(
        {"user_id": user["user_id"]},
        {"$set": {
            "seal_image_fs_id": str(fs_id),
            "seal_image_mime": mime,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }},
    )
    return {
        "seal_image_fs_id": str(fs_id),
        "mime": mime,
        "size_bytes": len(blob),
    }


@router.delete("/me/seal")
async def clear_my_seal(user=Depends(get_current_user), db=Depends(get_db)):
    """Remove the current user's seal. Returns 200 even if there was nothing
    to remove — the goal-state is 'no seal', and that's now true regardless."""
    udoc = await db.users.find_one(
        {"user_id": user["user_id"]},
        {"_id": 0, "seal_image_fs_id": 1},
    ) or {}
    if udoc.get("seal_image_fs_id"):
        bucket = AsyncIOMotorGridFSBucket(db, bucket_name=_SEAL_BUCKET)
        try:
            await bucket.delete(ObjectId(udoc["seal_image_fs_id"]))
        except Exception:
            pass
    await db.users.update_one(
        {"user_id": user["user_id"]},
        {"$set": {
            "seal_image_fs_id": None,
            "seal_image_mime": None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }},
    )
    return {"ok": True}


@router.get("/users/{user_id}/seal")
async def fetch_user_seal(user_id: str,
                          user=Depends(get_current_user), db=Depends(get_db)):
    """Same-tenant fetch of a user's seal. 404s cleanly when no seal is on
    file so the consumer can fall back gracefully (e.g. omit the seal from
    a printed report)."""
    udoc = await db.users.find_one(
        {"user_id": user_id, "clinic_id": user["clinic_id"]},
        {"_id": 0, "seal_image_fs_id": 1, "seal_image_mime": 1, "name": 1},
    )
    if not udoc or not udoc.get("seal_image_fs_id"):
        raise HTTPException(status_code=404, detail="No seal on file")
    bucket = AsyncIOMotorGridFSBucket(db, bucket_name=_SEAL_BUCKET)
    try:
        stream = await bucket.open_download_stream(ObjectId(udoc["seal_image_fs_id"]))
        data = await stream.read()
    except Exception:
        raise HTTPException(status_code=404, detail="Seal blob missing")
    from fastapi.responses import Response
    return Response(
        content=data,
        media_type=udoc.get("seal_image_mime") or "image/png",
        headers={
            "X-Seal-Owner": udoc.get("name") or "",
            "Cache-Control": "private, max-age=300",
        },
    )


# ---------- Seal placement preferences ---------------------------------------
# Where should the user's seal appear by default? Stored as a small string list
# on the user doc (`seal_include_on`). The PDF renderers + printable doc
# components read this to decide whether to draw the seal on a given document
# type. Valid doc types are exactly the three places we currently render
# clinic stationery — kept tight on the server side to prevent UI typos from
# silently disabling the feature.
_SEAL_DOC_TYPES = {"audiogram", "invoice", "challan"}


class SealPrefsPayload(BaseModel):
    """Plain list of doc-type codes the user wants the seal printed on.
    Unknown codes are rejected (415-ish via a 400) so a typo doesn't silently
    leave the seal off every doc."""
    include_on: List[str]


@router.get("/me/seal-prefs")
async def get_my_seal_prefs(user=Depends(get_current_user), db=Depends(get_db)):
    """Return the user's preferences + whether they actually have a seal on
    file. The UI uses both to decide which checkboxes to show as "available"."""
    udoc = await db.users.find_one(
        {"user_id": user["user_id"]},
        {"_id": 0, "seal_include_on": 1, "seal_image_fs_id": 1},
    ) or {}
    return {
        "include_on": list(udoc.get("seal_include_on") or []),
        "has_seal": bool(udoc.get("seal_image_fs_id")),
        "valid_doc_types": sorted(_SEAL_DOC_TYPES),
    }


@router.put("/me/seal-prefs")
async def set_my_seal_prefs(payload: SealPrefsPayload,
                            user=Depends(get_current_user), db=Depends(get_db)):
    cleaned: list[str] = []
    seen: set[str] = set()
    for code in payload.include_on or []:
        c = (code or "").strip().lower()
        if not c:
            continue
        if c not in _SEAL_DOC_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown doc type '{c}'. Allowed: {sorted(_SEAL_DOC_TYPES)}",
            )
        if c not in seen:
            seen.add(c)
            cleaned.append(c)

    await db.users.update_one(
        {"user_id": user["user_id"]},
        {"$set": {
            "seal_include_on": cleaned,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }},
    )
    return {"ok": True, "include_on": cleaned}



# ============================================================================
# SELF-SERVICE PROFILE & PASSWORD ENDPOINTS
# ----------------------------------------------------------------------------
# The `change-password` flow is critical because admins now provision users
# with a temporary password (via /api/admin/v2/tenants ?initial_password or
# /api/admin/v2/tenant-users). Without this endpoint, those users would be
# stuck on a password they didn't choose. Profile editing + avatar upload
# follow the same pattern as the existing signature endpoints (GridFS-backed,
# data-URL friendly) so we don't need a new infrastructure layer.
# ============================================================================

from auth import verify_password as _verify_password  # noqa: E402

_AVATAR_BUCKET = "user_avatars"
_MAX_AVATAR_BYTES = 2 * 1024 * 1024  # 2 MB — head-shot photos compress small

# Industry-standard professional fields for an audiology clinic. None are
# required by the schema, but the UI guides the audiologist to fill them.
class MyProfileUpdate(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    designation: Optional[str] = None        # e.g., "Senior Audiologist"
    qualifications: Optional[str] = None     # e.g., "MASLP, M.Sc. Audiology"
    license_no: Optional[str] = None         # RCI / state council registration
    rci_registration_no: Optional[str] = None  # RCI is the national body in India
    specialization: Optional[str] = None     # "Pediatric audiology", "Hearing aid fitting"
    years_of_experience: Optional[int] = None
    languages: Optional[List[str]] = None    # spoken languages — used in patient matching
    bio: Optional[str] = None                # short bio shown on patient portal


class ChangePasswordPayload(BaseModel):
    current_password: str
    new_password: str = pyField(min_length=8, max_length=128)


# pyField alias so pydantic doesn't clash with the form-data Field already imported.
@router.get("/me/profile")
async def get_my_profile(user=Depends(get_current_user), db=Depends(get_db)):
    """Return the rich profile + clinic context for the current user.

    UI uses this on the My Profile tab; intentionally returns more fields than
    /auth/me which is meant to be lightweight for nav rendering."""
    fields = [
        "user_id", "email", "name", "role", "phone", "designation",
        "qualifications", "license_no", "rci_registration_no",
        "specialization", "years_of_experience", "languages", "bio",
        "avatar_fs_id", "signature_image_fs_id", "seal_image_fs_id",
        "seal_include_on",
        "appointment_color", "must_change_password",
        "created_at", "updated_at",
    ]
    proj = {"_id": 0, **{f: 1 for f in fields}}
    udoc = await db.users.find_one({"user_id": user["user_id"]}, proj) or {}
    clinic = await db.clinics.find_one(
        {"clinic_id": user["clinic_id"]},
        {"_id": 0, "clinic_id": 1, "name": 1, "city": 1, "state": 1,
         "phone": 1, "email": 1, "subscription_tier": 1, "logo_fs_id": 1,
         "mrd_prefix": 1, "gst_no": 1, "registration_no": 1,
         "address": 1, "pincode": 1},
    ) or {}
    return {"user": udoc, "clinic": clinic}


@router.patch("/me/profile")
async def update_my_profile(payload: MyProfileUpdate,
                            user=Depends(get_current_user), db=Depends(get_db)):
    """User self-edits a curated subset of their profile.

    Email + role are NOT editable here — those changes require admin action
    to keep audit trails clean and prevent privilege escalation."""
    update = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None or v == 0}
    # Years of experience can legitimately be 0; treat empty string as null.
    if payload.years_of_experience is None:
        update.pop("years_of_experience", None)
    if not update:
        raise HTTPException(400, "No fields to update")
    update["updated_at"] = datetime.now(timezone.utc).isoformat()
    await db.users.update_one({"user_id": user["user_id"]}, {"$set": update})
    return {"ok": True, "updated_fields": list(update.keys())}


@router.post("/me/change-password")
async def change_my_password(payload: ChangePasswordPayload,
                             user=Depends(get_current_user), db=Depends(get_db)):
    """Self-service password change. Requires the *current* password so a
    stolen JWT can't pivot into a permanent account takeover."""
    udoc = await db.users.find_one({"user_id": user["user_id"]},
                                   {"_id": 0, "password_hash": 1}) or {}
    if not udoc.get("password_hash"):
        raise HTTPException(400, "Password authentication isn't enabled on this account")
    if not await asyncio.to_thread(_verify_password, payload.current_password, udoc["password_hash"]):
        raise HTTPException(401, "Current password is incorrect")
    if payload.current_password == payload.new_password:
        raise HTTPException(400, "New password must be different from current password")

    new_hash = await asyncio.to_thread(hash_password, payload.new_password)
    await db.users.update_one(
        {"user_id": user["user_id"]},
        {"$set": {
            "password_hash": new_hash,
            "must_change_password": False,    # admin-set temp password is now consumed
            "password_changed_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, "$inc": {"token_version": 1}},     # invalidate other sessions
    )
    return {"ok": True}


# ---------- Avatar upload (multipart) -----------------------------------------
# Two upload paths supported:
#   • multipart file POST  → /me/avatar (file=...)
#   • base64 JSON          → /me/avatar/base64 (mirrors the signature pad)
# A single GET returns the bytes; the GridFS id is stored on the user.

@router.post("/me/avatar")
async def upload_my_avatar(file: UploadFile = File(...),
                           user=Depends(get_current_user), db=Depends(get_db)):
    if file.content_type not in {"image/png", "image/jpeg", "image/jpg", "image/webp"}:
        raise HTTPException(400, "Only PNG, JPEG, or WebP images are accepted")
    blob = await file.read()
    if not blob:
        raise HTTPException(400, "Empty file")
    if len(blob) > _MAX_AVATAR_BYTES:
        raise HTTPException(413, "Avatar image too large (max 2 MB)")

    bucket = AsyncIOMotorGridFSBucket(db, bucket_name=_AVATAR_BUCKET)
    udoc = await db.users.find_one({"user_id": user["user_id"]}, {"_id": 0, "avatar_fs_id": 1}) or {}
    if udoc.get("avatar_fs_id"):
        try:
            await bucket.delete(ObjectId(udoc["avatar_fs_id"]))
        except Exception:
            pass
    fs_id = await bucket.upload_from_stream(
        f"avatar-{user['user_id']}.{(file.filename or 'png').rsplit('.', 1)[-1].lower()}",
        io.BytesIO(blob),
        metadata={"user_id": user["user_id"], "kind": "user-avatar",
                  "content_type": file.content_type},
    )
    await db.users.update_one(
        {"user_id": user["user_id"]},
        {"$set": {"avatar_fs_id": str(fs_id),
                  "updated_at": datetime.now(timezone.utc).isoformat()}},
    )
    return {"avatar_fs_id": str(fs_id)}


@router.delete("/me/avatar")
async def delete_my_avatar(user=Depends(get_current_user), db=Depends(get_db)):
    udoc = await db.users.find_one({"user_id": user["user_id"]}, {"_id": 0, "avatar_fs_id": 1}) or {}
    if udoc.get("avatar_fs_id"):
        bucket = AsyncIOMotorGridFSBucket(db, bucket_name=_AVATAR_BUCKET)
        try:
            await bucket.delete(ObjectId(udoc["avatar_fs_id"]))
        except Exception:
            pass
    await db.users.update_one(
        {"user_id": user["user_id"]},
        {"$set": {"avatar_fs_id": None,
                  "updated_at": datetime.now(timezone.utc).isoformat()}},
    )
    return {"ok": True}


@router.get("/users/{user_id}/avatar")
async def fetch_user_avatar(user_id: str,
                            user=Depends(get_current_user), db=Depends(get_db)):
    """Same-tenant lookup. The avatar is small + non-sensitive so any logged-in
    teammate can render it (e.g. on the appointment grid)."""
    udoc = await db.users.find_one(
        {"user_id": user_id, "clinic_id": user["clinic_id"]},
        {"_id": 0, "avatar_fs_id": 1},
    )
    if not udoc or not udoc.get("avatar_fs_id"):
        raise HTTPException(404, "No avatar on file")
    bucket = AsyncIOMotorGridFSBucket(db, bucket_name=_AVATAR_BUCKET)
    try:
        stream = await bucket.open_download_stream(ObjectId(udoc["avatar_fs_id"]))
        data = await stream.read()
        meta = stream.metadata or {}
    except Exception:
        raise HTTPException(404, "Avatar blob missing")
    from fastapi.responses import Response
    return Response(
        content=data,
        media_type=meta.get("content_type", "image/png"),
        headers={"Cache-Control": "private, max-age=300"},
    )


# ────────────────────────── Integrations Hub ─────────────────────────
#
# GET /api/settings/integrations
#
# Consolidated read-only view of every third-party integration AUDINEXA
# actually supports today. Reuses the existing env-var + collection
# configuration mechanisms already documented in `routers/status_page.py`
# and `routers/connect.py`. Never returns secret values — only presence.
#
# Categories:
#   • Payments   — Razorpay
#   • Messaging  — WhatsApp (MSG91)
#   • Email      — ZeptoMail (SMTP)
#   • SMS        — Twilio
#   • System     — Database (MongoDB) · API
#
# Status values (mirrors status_page.py):
#   • operational       — credentials present, reachable
#   • degraded          — credentials present but downstream is slow / partial
#   • outage            — configured but unreachable
#   • unknown           — no credentials configured (integration not wired up
#                         on this deployment)
#   • not_available     — integration not offered on the current tier
#
# Cards do NOT expose:
#   • raw secret values
#   • plaintext auth keys
#   • webhook secrets
#
# Owner / staff read only. Zero write endpoints — configuration continues
# to happen via the existing per-integration surfaces (Razorpay via env,
# ZeptoMail via env, Twilio via env, WhatsApp via /settings/connect).

@router.get("/integrations")
async def list_integrations(user=Depends(get_current_user), db=Depends(get_db)):
    """Return a provider-card list for the Settings → Integrations hub.

    Reuses the presence-check pattern established in
    ``routers/status_page.py`` (per-provider ``_probe_*`` functions).
    All checks are cheap: env-var presence + one Mongo find_one for the
    tenant's WhatsApp config. No outbound HTTP calls. No secrets in the
    payload. Response shape is intentionally flat and stable so the
    frontend can render generically.
    """
    import os

    clinic_id = user["clinic_id"]

    # ── Razorpay ──
    rzp_kid = (os.environ.get("RAZORPAY_KEY_ID") or "").strip()
    rzp_sec = (os.environ.get("RAZORPAY_KEY_SECRET") or "").strip()
    rzp_hook = (os.environ.get("RAZORPAY_WEBHOOK_SECRET") or "").strip()
    if rzp_kid and rzp_sec:
        rzp_status = "operational"
        rzp_detail = "Credentials present"
        if not rzp_hook:
            rzp_detail = "Credentials present · webhook secret not set"
    else:
        rzp_status = "unknown"
        rzp_detail = "No credentials configured on this deployment"

    # ── WhatsApp (MSG91) ──
    # Two paths: (a) BYOG per-clinic doc, (b) hosted / env-key fallback.
    wa_doc = await db.whatsapp_configs.find_one(
        {"clinic_id": clinic_id}, {"_id": 0}
    ) or {}
    wa_env_key = bool(
        os.environ.get("MSG91_AUTH_KEY") or os.environ.get("MSG91_API_KEY")
    )
    if wa_doc.get("enabled"):
        mode = wa_doc.get("mode") or "byog"
        if mode == "byog":
            has_key = bool(wa_doc.get("auth_key_encrypted"))
            has_number = bool(wa_doc.get("integrated_number"))
            if has_key and has_number:
                wa_status = "operational"
                wa_detail = f"BYOG mode · sender {wa_doc.get('integrated_number')}"
            else:
                wa_status = "degraded"
                wa_detail = "BYOG mode enabled but auth key / sender not fully configured"
        else:  # hosted
            wa_status = "operational" if wa_env_key else "degraded"
            wa_detail = "Hosted mode · shared AUDINEXA sender"
    elif wa_env_key or wa_doc.get("mode"):
        wa_status = "unknown"
        wa_detail = "Configured but disabled"
    else:
        wa_status = "unknown"
        wa_detail = "Awaiting configuration in Settings → Connect (WhatsApp)"

    # ── ZeptoMail (SMTP) ──
    zm_host = (os.environ.get("ZEPTO_SMTP_HOST") or "").strip()
    zm_pw = (os.environ.get("ZEPTO_SMTP_PASSWORD") or "").strip()
    zm_from = (os.environ.get("ZEPTO_FROM_ADDRESS") or "").strip()
    if zm_host and zm_pw:
        zm_status = "operational"
        zm_detail = f"Credentials present · from {zm_from}" if zm_from else "Credentials present"
    else:
        zm_status = "unknown"
        zm_detail = "No credentials configured on this deployment"

    # ── Twilio (SMS) ──
    tw_sid = (os.environ.get("TWILIO_ACCOUNT_SID") or "").strip()
    tw_tok = (os.environ.get("TWILIO_AUTH_TOKEN") or "").strip()
    tw_from = (os.environ.get("TWILIO_FROM_NUMBER") or "").strip()
    if tw_sid and tw_tok:
        tw_status = "operational"
        tw_detail = f"Credentials present · sender {tw_from}" if tw_from else "Credentials present"
    else:
        tw_status = "unknown"
        tw_detail = "No credentials configured on this deployment"

    integrations = [
        {
            "provider_id": "razorpay",
            "name": "Razorpay",
            "category": "Payments",
            "purpose": "Online invoice payment collection (UPI, cards, netbanking).",
            "status": rzp_status,
            "detail": rzp_detail,
            "managed_by": "platform",
            "config_surface": None,
            "action_href": None,
            "action_label": None,
        },
        {
            "provider_id": "msg91_whatsapp",
            "name": "WhatsApp (MSG91)",
            "category": "Messaging",
            "purpose": "Appointment / invoice / report-ready notifications to patients on WhatsApp.",
            "status": wa_status,
            "detail": wa_detail,
            "managed_by": "clinic",
            "config_surface": "settings",
            "action_href": "/settings/connect",
            "action_label": "Configure",
        },
        {
            "provider_id": "zeptomail",
            "name": "ZeptoMail",
            "category": "Email",
            "purpose": "Transactional email — verification, password reset, invoice PDFs.",
            "status": zm_status,
            "detail": zm_detail,
            "managed_by": "platform",
            "config_surface": None,
            "action_href": None,
            "action_label": None,
        },
        {
            "provider_id": "twilio_sms",
            "name": "Twilio",
            "category": "SMS",
            "purpose": "SMS delivery for OTPs, appointment reminders, follow-ups.",
            "status": tw_status,
            "detail": tw_detail,
            "managed_by": "platform",
            "config_surface": None,
            "action_href": None,
            "action_label": None,
        },
    ]

    return {
        "integrations": integrations,
        "as_of": datetime.now(timezone.utc).isoformat(),
    }
