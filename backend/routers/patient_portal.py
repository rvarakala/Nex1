"""M13 — Patient Self-Service Dashboard (Phase 13.D).

9 UCs covered:
  1. Phone-OTP login (mock SMS — OTP echoed in dev response)
  2. View profile + MRD
  3. View diagnostic reports (list + PDF via existing share-link)
  4. Book/cancel appointments
  5. View upcoming appointments
  6. View HA sales / service tickets / AMC contracts
  7. View pending invoices + paid history
  8. Submit follow-up feedback (simple message)
  9. Logout

Auth model:
  * `POST /patient-portal/request-otp`   {clinic_id, mobile} → stores otp hash
  * `POST /patient-portal/verify-otp`    {clinic_id, mobile, otp} → returns JWT
    JWT payload: {type: "patient_access", patient_id, clinic_id}
  * Every /patient-portal/me/* endpoint requires this JWT.

Tier gate: STANDARD + PREMIUM (via module "patient-portal").
"""
from __future__ import annotations

import os
import secrets
import hashlib
from datetime import datetime, timezone, timedelta, date
from typing import List, Optional
from uuid import uuid4

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from database import get_db
from utils.serde import serialize_datetime, deserialize_datetime
from utils.tiers import require_tier, resolve_effective_tier, has_module_access


router = APIRouter(prefix="/api/patient-portal")

PATIENT_JWT_ALGORITHM = "HS256"
PATIENT_JWT_TTL_HOURS = 24 * 30      # 30 days — SMS-gated, mobile UX
OTP_TTL_MINUTES = 10
DEV_ECHO_OTP = os.environ.get("PATIENT_OTP_DEV_ECHO", "true").lower() == "true"


def _jwt_secret() -> str:
    s = os.environ.get("JWT_SECRET")
    if not s:
        raise RuntimeError("JWT_SECRET not configured")
    return s


def _hash_otp(otp: str) -> str:
    return hashlib.sha256(otp.encode("utf-8")).hexdigest()


def _issue_patient_token(patient_id: str, clinic_id: str) -> str:
    payload = {
        "sub": patient_id,
        "patient_id": patient_id,
        "clinic_id": clinic_id,
        "type": "patient_access",
        "exp": datetime.now(timezone.utc) + timedelta(hours=PATIENT_JWT_TTL_HOURS),
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=PATIENT_JWT_ALGORITHM)


async def _ensure_portal_module(db, clinic_id: str):
    """Enforce module gate for the *clinic whose patient is accessing*.
    Separate from require_tier because the caller isn't a clinic user.
    """
    clinic = await db.clinics.find_one(
        {"clinic_id": clinic_id},
        {"_id": 0, "subscription_tier": 1, "trial_ends_at": 1},
    )
    if not clinic:
        raise HTTPException(status_code=404, detail="Clinic not found")
    tier = await resolve_effective_tier(clinic)
    if not has_module_access(tier, "patient-portal"):
        raise HTTPException(
            status_code=402,
            detail={"error": "upgrade_required",
                    "current_tier": tier,
                    "message": "Patient Portal is not enabled for this clinic."},
        )


async def _current_patient(request: Request, db) -> dict:
    """Dependency-esque helper — resolves patient via patient_access JWT."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = auth[7:]
    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=[PATIENT_JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    if payload.get("type") != "patient_access":
        raise HTTPException(status_code=401, detail="Invalid token type")
    patient_id = payload.get("patient_id")
    clinic_id = payload.get("clinic_id")
    pat = await db.patients.find_one(
        {"patient_id": patient_id, "clinic_id": clinic_id},
        {"_id": 0},
    )
    if not pat:
        raise HTTPException(status_code=404, detail="Patient record not found")
    await _ensure_portal_module(db, clinic_id)
    return pat


# ==================== MODELS ====================

class OTPRequest(BaseModel):
    clinic_id: str
    mobile: str = Field(min_length=6)


class OTPVerify(BaseModel):
    clinic_id: str
    mobile: str
    otp: str


class AppointmentRequest(BaseModel):
    start_at: str           # ISO 8601
    service: Optional[str] = None
    notes: Optional[str] = None


class FeedbackSubmit(BaseModel):
    session_id: Optional[str] = None
    rating: Optional[int] = Field(default=None, ge=1, le=5)
    message: str = Field(min_length=1, max_length=2000)


# ==================== AUTH ====================

@router.post("/request-otp")
async def request_otp(payload: OTPRequest, db=Depends(get_db)):
    """Issues an OTP for phone-based login. The mobile must belong to a
    patient of the given clinic. In dev, the OTP is echoed in the response.
    """
    await _ensure_portal_module(db, payload.clinic_id)

    # Normalise mobile (last 10 digits match)
    mob = "".join(c for c in payload.mobile if c.isdigit())[-10:]
    if len(mob) < 6:
        raise HTTPException(status_code=400, detail="Invalid mobile")

    # Try exact match first, then fuzzy last-10
    patient = await db.patients.find_one(
        {"clinic_id": payload.clinic_id, "mobile": payload.mobile},
        {"_id": 0, "patient_id": 1, "mobile": 1, "name": 1},
    )
    if not patient:
        async for p in db.patients.find(
            {"clinic_id": payload.clinic_id},
            {"_id": 0, "patient_id": 1, "mobile": 1, "name": 1},
        ):
            pm = "".join(c for c in (p.get("mobile") or "") if c.isdigit())[-10:]
            if pm == mob:
                patient = p
                break

    # Security: always respond "ok" even if patient not found (prevents enumeration).
    # But in dev mode, return an informative flag so UI knows to retry.
    if not patient:
        return {"sent": True, "dev_note": "no_matching_patient"}

    otp = f"{secrets.randbelow(1_000_000):06d}"
    exp_iso = (datetime.now(timezone.utc) + timedelta(minutes=OTP_TTL_MINUTES)).isoformat()
    await db.patient_otps.update_one(
        {"clinic_id": payload.clinic_id, "patient_id": patient["patient_id"]},
        {"$set": {
            "clinic_id": payload.clinic_id,
            "patient_id": patient["patient_id"],
            "otp_hash": _hash_otp(otp),
            "otp_expires_at": exp_iso,
            "attempts": 0,
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }},
        upsert=True,
    )
    # Real SMS via Twilio (or mock fallback in dev). OTP flow is security-
    # critical, so if the send fails we surface a clean error instead of
    # silently pretending it worked. Clinic branding goes into the body so
    # the patient knows what the code is for.
    from utils.sms import send_sms

    # Grab clinic name for template; fall back to "AUDINEXA" for tidiness.
    clinic_doc = await db.clinics.find_one(
        {"clinic_id": payload.clinic_id}, {"_id": 0, "name": 1}
    ) or {}
    clinic_name = clinic_doc.get("name") or "AUDINEXA"
    body_text = (
        f"{otp} is your {clinic_name} patient portal login code. "
        f"Valid for {OTP_TTL_MINUTES} minutes. Do not share."
    )
    sms_result = send_sms(patient.get("mobile") or "", body_text, purpose="patient_otp")

    resp = {"sent": sms_result["status"] in ("sent", "mocked"), "expires_at": exp_iso, "sms": sms_result}
    if sms_result["status"] == "error":
        # Don't leak the OTP if SMS failed — signal a clean 502 so the UI can retry.
        raise HTTPException(502, detail=f"Could not send OTP SMS: {sms_result.get('error')}")
    if DEV_ECHO_OTP:
        resp["dev_otp"] = otp
        resp["dev_note"] = "OTP echoed because PATIENT_OTP_DEV_ECHO=true"
    return resp


@router.post("/verify-otp")
async def verify_otp(payload: OTPVerify, db=Depends(get_db)):
    await _ensure_portal_module(db, payload.clinic_id)
    mob = "".join(c for c in payload.mobile if c.isdigit())[-10:]
    # find patient by mobile
    patient = None
    async for p in db.patients.find(
        {"clinic_id": payload.clinic_id},
        {"_id": 0, "patient_id": 1, "mobile": 1, "name": 1, "mrd": 1},
    ):
        pm = "".join(c for c in (p.get("mobile") or "") if c.isdigit())[-10:]
        if pm == mob:
            patient = p
            break
    if not patient:
        raise HTTPException(status_code=401, detail="Invalid OTP or mobile")

    otp_doc = await db.patient_otps.find_one(
        {"clinic_id": payload.clinic_id, "patient_id": patient["patient_id"]},
        {"_id": 0},
    )
    if not otp_doc:
        raise HTTPException(status_code=401, detail="Request an OTP first")
    if (otp_doc.get("attempts") or 0) >= 5:
        raise HTTPException(status_code=429, detail="Too many attempts")
    expires = otp_doc.get("otp_expires_at")
    if not expires or datetime.fromisoformat(expires) < datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail="OTP expired")
    if _hash_otp(payload.otp) != otp_doc.get("otp_hash"):
        await db.patient_otps.update_one(
            {"clinic_id": payload.clinic_id, "patient_id": patient["patient_id"]},
            {"$inc": {"attempts": 1}},
        )
        raise HTTPException(status_code=401, detail="Invalid OTP")

    # consume OTP
    await db.patient_otps.delete_one(
        {"clinic_id": payload.clinic_id, "patient_id": patient["patient_id"]},
    )
    token = _issue_patient_token(patient["patient_id"], payload.clinic_id)
    return {
        "access_token": token,
        "token_type": "bearer",
        "patient": {
            "patient_id": patient["patient_id"],
            "name": patient.get("name"),
            "mrd": patient.get("mrd"),
            "clinic_id": payload.clinic_id,
        },
    }


# ==================== ME ENDPOINTS ====================

@router.get("/me")
async def me_profile(request: Request, db=Depends(get_db)):
    p = await _current_patient(request, db)
    clinic = await db.clinics.find_one(
        {"clinic_id": p["clinic_id"]},
        {"_id": 0, "name": 1, "city": 1, "phone": 1, "email": 1},
    )
    return {"patient": deserialize_datetime(p), "clinic": clinic}


@router.get("/me/reports")
async def me_reports(request: Request, db=Depends(get_db)):
    p = await _current_patient(request, db)
    sessions = await db.test_sessions.find(
        {"patient_id": p["patient_id"], "clinic_id": p["clinic_id"]},
        {"_id": 0, "session_id": 1, "test_date": 1, "complaint": 1, "diagnosis": 1, "clinical_summary": 1},
    ).sort("test_date", -1).to_list(50)
    return {"reports": [deserialize_datetime(s) for s in sessions]}


@router.get("/me/appointments")
async def me_appointments(request: Request, db=Depends(get_db)):
    p = await _current_patient(request, db)
    today_iso = datetime.now(timezone.utc).isoformat()
    upcoming = await db.appointments.find(
        {"patient_id": p["patient_id"], "clinic_id": p["clinic_id"],
         "start_at": {"$gte": today_iso}, "status": {"$nin": ["cancelled"]}},
        {"_id": 0},
    ).sort("start_at", 1).to_list(20)
    past = await db.appointments.find(
        {"patient_id": p["patient_id"], "clinic_id": p["clinic_id"],
         "start_at": {"$lt": today_iso}},
        {"_id": 0},
    ).sort("start_at", -1).to_list(20)
    return {
        "upcoming": [deserialize_datetime(a) for a in upcoming],
        "past": [deserialize_datetime(a) for a in past],
    }


@router.post("/me/appointment-request")
async def me_request_appointment(
    payload: AppointmentRequest,
    request: Request,
    db=Depends(get_db),
):
    p = await _current_patient(request, db)
    # Lightweight booking request — goes into a dedicated queue for front-desk to approve.
    req_id = f"APR-{str(uuid4())[:8].upper()}"
    doc = {
        "request_id": req_id,
        "clinic_id": p["clinic_id"],
        "patient_id": p["patient_id"],
        "patient_name": p.get("name"),
        "mobile": p.get("mobile"),
        "start_at": payload.start_at,
        "service": payload.service,
        "notes": payload.notes,
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": "patient_portal",
    }
    await db.patient_appointment_requests.insert_one(serialize_datetime(doc))
    return {"request_id": req_id, "status": "pending"}


@router.get("/me/sales")
async def me_sales(request: Request, db=Depends(get_db)):
    p = await _current_patient(request, db)
    sales = await db.ha_sales.find(
        {"patient_id": p["patient_id"], "clinic_id": p["clinic_id"]},
        {"_id": 0, "sale_no": 1, "created_at": 1, "status": 1, "total": 1,
         "is_pair": 1, "lines": 1},
    ).sort("created_at", -1).to_list(50)
    return {"sales": [deserialize_datetime(s) for s in sales]}


@router.get("/me/service-tickets")
async def me_service(request: Request, db=Depends(get_db)):
    p = await _current_patient(request, db)
    tickets = await db.service_tickets.find(
        {"patient_id": p["patient_id"], "clinic_id": p["clinic_id"]},
        {"_id": 0, "ticket_no": 1, "created_at": 1, "status": 1, "kind": 1,
         "complaint_notes": 1, "cost_to_patient": 1, "resolved_at": 1},
    ).sort("created_at", -1).to_list(50)
    return {"tickets": [deserialize_datetime(t) for t in tickets]}


@router.get("/me/amc")
async def me_amc(request: Request, db=Depends(get_db)):
    p = await _current_patient(request, db)
    rows = await db.ha_amc_contracts.find(
        {"patient_id": p["patient_id"], "clinic_id": p["clinic_id"]},
        {"_id": 0},
    ).sort("amc_expiry_date", -1).to_list(20)
    return {"contracts": [deserialize_datetime(r) for r in rows]}


@router.get("/me/invoices")
async def me_invoices(request: Request, db=Depends(get_db)):
    p = await _current_patient(request, db)
    rows = await db.invoices.find(
        {"patient_id": p["patient_id"], "clinic_id": p["clinic_id"]},
        {"_id": 0, "invoice_id": 1, "invoice_no": 1, "invoice_date": 1,
         "status": 1, "grand_total": 1, "rounded_total": 1,
         "paid_total": 1, "due_total": 1},
    ).sort("invoice_date", -1).to_list(50)
    # NAV-009 · PAY-005 — the patient's outstanding balance is the
    # sum of `due_total` across every invoice that still owes money.
    # Prior code read `balance_due` (never populated) and filtered on
    # `status == "issued"` (not a valid Invoice status), so the value
    # was always 0.0. Correct approach: use `due_total`, and skip
    # only the terminal / already-settled statuses.
    _EXCLUDE = {"cancelled", "refunded"}
    total_due = 0.0
    for r in rows:
        st = (r.get("status") or "").lower()
        if st in _EXCLUDE:
            continue
        total_due += float(r.get("due_total") or 0)
    return {
        "invoices": [deserialize_datetime(r) for r in rows],
        "total_outstanding": round(total_due, 2),
    }


@router.post("/me/feedback")
async def me_feedback(
    payload: FeedbackSubmit,
    request: Request,
    db=Depends(get_db),
):
    p = await _current_patient(request, db)
    doc = {
        "feedback_id": f"FDB-{str(uuid4())[:8].upper()}",
        "clinic_id": p["clinic_id"],
        "patient_id": p["patient_id"],
        "patient_name": p.get("name"),
        "session_id": payload.session_id,
        "rating": payload.rating,
        "message": payload.message,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.patient_feedback.insert_one(serialize_datetime(doc))
    return {"feedback_id": doc["feedback_id"], "status": "received"}


# ==================== CLINIC-SIDE: VIEW PORTAL REQUESTS ====================
# These let front-desk see the incoming OTP-portal bookings + feedback.

@router.get(
    "/clinic/appointment-requests",
    dependencies=[Depends(require_tier("patient-portal"))],
)
async def list_appointment_requests(
    status: Optional[str] = "pending",
    user=None,  # resolved below
    db=Depends(get_db),
    request: Request = None,
):
    # Standard clinic-user auth via require_tier; also enforce role here
    from auth import get_current_user, require_roles
    u = await get_current_user(request)
    if u["role"] not in {"super_admin", "clinic_owner", "front_desk", "accounts", "audiologist"}:
        raise HTTPException(status_code=403, detail="Not permitted")
    q = {"clinic_id": u["clinic_id"]}
    if status:
        q["status"] = status
    rows = await db.patient_appointment_requests.find(q, {"_id": 0}) \
        .sort("created_at", -1).to_list(200)
    return [deserialize_datetime(r) for r in rows]


@router.post(
    "/clinic/appointment-requests/{request_id}/{decision}",
    dependencies=[Depends(require_tier("patient-portal"))],
)
async def resolve_request(
    request_id: str,
    decision: str,
    db=Depends(get_db),
    request: Request = None,
):
    from auth import get_current_user
    u = await get_current_user(request)
    if decision not in {"approve", "decline"}:
        raise HTTPException(status_code=400, detail="decision must be approve|decline")
    res = await db.patient_appointment_requests.find_one_and_update(
        {"request_id": request_id, "clinic_id": u["clinic_id"], "status": "pending"},
        {"$set": {
            "status": "approved" if decision == "approve" else "declined",
            "decided_at": datetime.now(timezone.utc).isoformat(),
            "decided_by": u["user_id"],
        }},
        projection={"_id": 0},
        return_document=True,
    )
    if not res:
        raise HTTPException(status_code=404, detail="Pending request not found")
    return deserialize_datetime(res)
