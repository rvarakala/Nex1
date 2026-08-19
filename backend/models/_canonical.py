from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator
from typing import Optional, List, Literal, Dict, Union, Any
from datetime import datetime, date, timedelta, timezone
from uuid import uuid4
import re


# NAV-005 Sprint-3C · REG-002/003/004 shared helpers
# Kept at module scope so `PatientCreate` validators can reference them
# without duplication and so tests can import them for parity checks.
def _ist_today() -> date:
    """Return today's date in IST. Matches `greetings._today_ist()` so
    the entire codebase agrees on when "tomorrow" begins."""
    return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date()


# REG-003 email regex. Deliberately the HTML5 living-standard shape:
# ^[^\s@]+@[^\s@]+\.[^\s@]+$
# Practical (accepts + addressing, subdomains, single-letter TLDs) and
# in sync with the frontend `EMAIL_RE`. Compile once.
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _digits_only(v) -> str:
    return re.sub(r"\D", "", str(v or ""))


def _last10(v) -> str:
    """Last 10 digits — the canonical Indian-phone equality shape used
    by the duplicate-detection guard in `POST /patients`."""
    d = _digits_only(v)
    return d[-10:] if len(d) >= 10 else d


def _normalize_date_str(v):
    """Tolerate legacy rows where dob/anniversary_date were written as
    `datetime` instead of an ISO `"YYYY-MM-DD"` string. Returns None for
    falsy input, ISO date string otherwise."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, str):
        return v
    # Fallback: stringify whatever it is (defensive — should not hit in practice).
    return str(v)


# ==================== MULTI-TENANT + AUTH MODELS ====================

class Clinic(BaseModel):
    model_config = ConfigDict(extra="ignore")
    clinic_id: str
    name: str
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    pincode: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    gstin: Optional[str] = None
    mrd_prefix: str = "ACS"
    # When true, audiologists can read-only view peers' appointments (avoids equipment double-booking).
    appointment_peer_visibility: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)


class User(BaseModel):
    model_config = ConfigDict(extra="ignore")
    user_id: str = Field(default_factory=lambda: f"USR-{str(uuid4())[:10].upper()}")
    clinic_id: str
    email: str
    name: str
    role: Literal[
        "super_admin", "clinic_owner", "front_desk", "audiologist",
        "accounts", "inventory_manager", "technician",
    ]
    # Branch scope: empty list = clinic-wide (super_admin / clinic_owner / accounts).
    # Non-empty = user can only see/modify data scoped to these branches.
    branch_ids: List[str] = Field(default_factory=list)
    # Optional override of the auto-assigned calendar colour for this user's appointments.
    appointment_color: Optional[str] = None
    # Drawn-signature PNG stored in GridFS bucket `user_signatures`. Auto-applied
    # to audiogram reports + delivery-challan receipts when present.
    signature_image_fs_id: Optional[str] = None
    # Optional license / registration number printed under the signature.
    license_no: Optional[str] = None
    # Referral-corner delegation: when True (or the user is clinic_owner /
    # super_admin), this user can view + edit referral payouts. Default off
    # so Marketing Manager / Admin staff must be explicitly granted access
    # by the owner. Branch scoping is enforced via the existing
    # `branch_ids` filter — a delegated user only sees referrals from
    # patients registered at their branches.
    can_access_referrals: bool = False
    active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)


class LoginRequest(BaseModel):
    email: str = Field(..., max_length=320)        # RFC 5321 max email length
    password: str = Field(..., min_length=1, max_length=72)  # bcrypt's hard limit
    # Optional device-limit escape hatch — the login-again call from the
    # DEVICE_LIMIT_EXCEEDED picker includes the session_id the user chose
    # to kick. Backend revokes it and mints the new session in one hop.
    replace_session_id: str | None = Field(default=None, max_length=64)
    # "Remember this device for 30 days" checkbox on the login form.
    # * True  → long-lived (30-day cookie) session that counts against
    #           the tier's concurrent-device cap.
    # * False → ephemeral (~8-hour cookie) session that does NOT count
    #           against the cap — designed for incognito test-drives on
    #           a coworker's laptop, patient-facing kiosk previews, etc.
    # Default TRUE so existing clients + tests behave unchanged.
    remember_device: bool = True


# ==================== TOKEN + QUEUE (UC-01 front-desk) ====================

class OPDToken(BaseModel):
    model_config = ConfigDict(extra="ignore")
    token_id: str = Field(default_factory=lambda: str(uuid4()))
    clinic_id: str
    token_no: int
    patient_id: str
    patient_name: str
    patient_mobile: Optional[str] = None
    mrd: Optional[str] = None
    issued_at: datetime = Field(default_factory=datetime.utcnow)
    issued_by_user_id: Optional[str] = None
    status: Literal["waiting", "in_consultation", "in_testing", "billing", "completed", "cancelled"] = "waiting"
    called_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    service: Optional[str] = None
    priority: Literal["normal", "urgent", "vip"] = "normal"
    notes: Optional[str] = None


# ==================== UC-03 APPOINTMENTS ====================

APPOINTMENT_SERVICES = [
    "Consultation", "PTA", "Immittance", "OAE", "ABR/BERA", "ASSR",
    "Vestibular Tests", "Follow-up", "Speech Audiometry", "Hearing Aid Fitting",
]
APPOINTMENT_PRIORITIES = ["normal", "urgent", "vip"]
APPOINTMENT_STATUSES = ["scheduled", "confirmed", "checked_in", "in_progress", "completed", "no_show", "cancelled"]

# Counterparty = the entity the appointment is *with* (extends beyond patients
# so clinic owners / staff can also book vendor demos, sales-rep calls,
# technician slots, internal meetings, etc.).
COUNTERPARTY_TYPES = ["patient", "vendor", "sales_rep", "tech_staff", "internal", "other"]

# High-level grouping used for filters & colour fall-backs in the calendar UI.
APPOINTMENT_CATEGORIES = ["consultation", "diagnostic", "fitting", "meeting", "demo", "other"]

# Deterministic palette used when a staff member has no explicit colour override.
# Picked for AA contrast against white text and to read well as solid event blocks.
STAFF_COLOR_PALETTE = [
    "#F59E0B",  # amber
    "#3B82F6",  # blue
    "#8B5CF6",  # violet
    "#EC4899",  # pink
    "#10B981",  # emerald
    "#EF4444",  # red
    "#0EA5E9",  # sky
    "#F97316",  # orange
    "#14B8A6",  # teal
    "#6366F1",  # indigo
]


def color_for_staff(staff_id: Optional[str]) -> str:
    """Stable colour for a staff resource. Same id always maps to the same swatch."""
    if not staff_id:
        return "#6B7280"  # neutral grey for unassigned
    h = 0
    for ch in staff_id:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return STAFF_COLOR_PALETTE[h % len(STAFF_COLOR_PALETTE)]


class Appointment(BaseModel):
    model_config = ConfigDict(extra="ignore")
    appointment_id: str = Field(default_factory=lambda: f"APT-{str(uuid4())[:10].upper()}")
    clinic_id: str

    # ---- Counterparty (the "who is this appointment with") --------------
    # For backward-compat, patient_id/patient_name remain present and act as
    # the counterparty when counterparty_type == "patient".
    patient_id: Optional[str] = None
    patient_name: Optional[str] = None                # Denormalised for fast list render
    patient_mobile: Optional[str] = None
    mrd: Optional[str] = None

    counterparty_type: Literal["patient", "vendor", "sales_rep", "tech_staff", "internal", "other"] = "patient"
    counterparty_id: Optional[str] = None             # vendor_id / user_id / null for free-text
    counterparty_name: Optional[str] = None           # always set; falls back to patient_name
    counterparty_phone: Optional[str] = None
    counterparty_company: Optional[str] = None        # e.g. brand a sales rep represents

    # ---- Resource (the staff owner of this slot) ------------------------
    # Legacy `audiologist_*` fields stay in sync with `staff_*` so existing
    # consumers keep working. Both are written on create/update.
    audiologist_id: Optional[str] = None
    audiologist_name: Optional[str] = None            # Denormalised
    staff_id: Optional[str] = None                    # Resource owner of the slot
    staff_name: Optional[str] = None
    staff_role: Optional[str] = None                  # cached at create-time
    staff_color: Optional[str] = None                 # explicit override; else derived

    room: Optional[str] = None

    service: Optional[str] = None
    category: Literal["consultation", "diagnostic", "fitting", "meeting", "demo", "other"] = "consultation"
    priority: Literal["normal", "urgent", "vip"] = "normal"

    # Front-desk intake triage (front-desk marks what to perform)
    visit_type: Literal["referral", "walkin", "consultation"] = "walkin"
    recommended_tests: List[str] = Field(default_factory=list)   # e.g. ["pta", "impedance"]
    referred_by: Optional[str] = None                            # ENT / GP name if visit_type=referral
    # Hearing Aid wing chips picked by front-desk. Distinct from
    # `recommended_tests` so downstream diagnostic modules ignore them.
    # e.g. ["ha_fitting", "ha_trial", "ha_earmould"]
    hearing_aid_services: List[str] = Field(default_factory=list)
    # Which "wing" this appointment is routed to. Drives category + module
    # navigation. `diagnostic` = audiology tests, `hearing_aid` = HA fitting/
    # sales/service. Optional — defaults to diagnostic for legacy rows.
    wing: Literal["diagnostic", "hearing_aid"] = "diagnostic"

    start_at: datetime                                # Full timestamp (UTC)
    end_at: datetime
    duration_minutes: int = 30

    status: Literal["scheduled", "confirmed", "checked_in", "in_progress", "completed", "no_show", "cancelled"] = "scheduled"

    notes: Optional[str] = None
    reminder_sent: bool = False

    created_at: datetime = Field(default_factory=datetime.utcnow)
    created_by_user_id: Optional[str] = None
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    # Imported-via tag — populated when row created by /api/imports/patients/commit.
    # Surfaces in the patient profile timeline as the "Imported" badge.
    imported_via: Optional[str] = None


class AppointmentCreate(BaseModel):
    # Either `patient_id` (legacy patient booking) or counterparty_* fields are required.
    patient_id: Optional[str] = None

    # Resource: prefer `staff_id`; `audiologist_id` accepted for backward compatibility.
    staff_id: Optional[str] = None
    audiologist_id: Optional[str] = None

    # Counterparty (non-patient bookings):
    counterparty_type: Literal["patient", "vendor", "sales_rep", "tech_staff", "internal", "other"] = "patient"
    counterparty_id: Optional[str] = None
    counterparty_name: Optional[str] = None
    counterparty_phone: Optional[str] = None
    counterparty_company: Optional[str] = None

    service: Optional[str] = None
    category: Literal["consultation", "diagnostic", "fitting", "meeting", "demo", "other"] = "consultation"
    start_at: datetime
    duration_minutes: int = 30
    priority: Literal["normal", "urgent", "vip"] = "normal"
    room: Optional[str] = None
    notes: Optional[str] = None
    visit_type: Literal["referral", "walkin", "consultation"] = "walkin"
    recommended_tests: List[str] = Field(default_factory=list)
    referred_by: Optional[str] = None
    # HA-wing chips + wing routing (mirror AppointmentBase). Optional so
    # existing callers don't break.
    hearing_aid_services: List[str] = Field(default_factory=list)
    wing: Literal["diagnostic", "hearing_aid"] = "diagnostic"
    # When a referral appointment is booked via ReferringDoctorPicker,
    # the picker also emits the doctor_id — the appointments router uses
    # it to auto-link the patient to that referring doctor so the
    # Referral Corner + payout rollup pick the visit up automatically.
    referring_doctor_id: Optional[str] = None


class WaitlistEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")
    entry_id: str = Field(default_factory=lambda: f"WL-{str(uuid4())[:10].upper()}")
    clinic_id: str
    patient_id: str
    patient_name: str
    patient_mobile: Optional[str] = None
    mrd: Optional[str] = None
    preferred_audiologist_id: Optional[str] = None
    preferred_service: Optional[str] = None
    preferred_date: Optional[str] = None              # 'YYYY-MM-DD'
    notes: Optional[str] = None
    status: Literal["active", "scheduled", "cancelled"] = "active"
    created_at: datetime = Field(default_factory=datetime.utcnow)


class WaitlistCreate(BaseModel):
    patient_id: str
    preferred_audiologist_id: Optional[str] = None
    preferred_service: Optional[str] = None
    preferred_date: Optional[str] = None
    notes: Optional[str] = None


class CancellationLog(BaseModel):
    model_config = ConfigDict(extra="ignore")
    log_id: str = Field(default_factory=lambda: f"CAN-{str(uuid4())[:10].upper()}")
    clinic_id: str
    appointment_id: str
    patient_id: str
    patient_name: str
    cancelled_at: datetime = Field(default_factory=datetime.utcnow)
    cancelled_by_user_id: str
    reason: Optional[str] = None
    was_same_day: bool = False


class ReminderLog(BaseModel):
    model_config = ConfigDict(extra="ignore")
    reminder_id: str = Field(default_factory=lambda: f"REM-{str(uuid4())[:10].upper()}")
    clinic_id: str
    appointment_id: Optional[str] = None
    patient_id: str
    channel: Literal["whatsapp", "sms", "email"]
    recipient: str                                    # Phone or email used
    subject: Optional[str] = None
    body: str
    status: Literal["pending", "sent", "failed", "stubbed_no_provider_key"] = "pending"
    provider_response: Optional[str] = None
    sent_at: datetime = Field(default_factory=datetime.utcnow)
    sent_by_user_id: Optional[str] = None


# ==================== UC-04 BILLING + REPORT HANDOVER ====================

PAYMENT_METHODS = ["cash", "upi", "card", "bank_transfer", "insurance"]
INVOICE_STATUSES = ["draft", "paid", "partial", "refunded", "cancelled"]


class Service(BaseModel):
    """Clinic service catalogue item (audiology procedure / hearing aid / accessory).
    Prices are inclusive of GST unless gst_inclusive=False."""
    model_config = ConfigDict(extra="ignore")
    service_id: str = Field(default_factory=lambda: f"SVC-{str(uuid4())[:8].upper()}")
    clinic_id: str
    code: Optional[str] = None                                   # Short alias (e.g., "PTA")
    name: str                                                    # e.g., "Pure Tone Audiometry"
    category: Optional[str] = None                               # Audiology / Hearing Aid / Consultation / Accessory
    hsn_sac: Optional[str] = None                                # HSN/SAC for GST compliance (e.g., "999312")
    price: float                                                 # Base price (in INR)
    gst_rate: float = 0.0                                        # GST % (0/5/12/18). 0 for exempt healthcare.
    gst_inclusive: bool = True                                   # Whether `price` already includes GST
    is_taxable: bool = False                                     # Healthcare services are usually exempt
    active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ServiceCreate(BaseModel):
    code: Optional[str] = None
    name: str
    category: Optional[str] = None
    hsn_sac: Optional[str] = None
    price: float
    gst_rate: float = 0.0
    gst_inclusive: bool = True
    is_taxable: bool = False


class InvoiceLine(BaseModel):
    line_id: str = Field(default_factory=lambda: str(uuid4())[:8])
    service_id: Optional[str] = None
    description: str
    hsn_sac: Optional[str] = None
    quantity: float = 1.0
    unit_price: float                                            # Pre-discount, pre-tax unit amount
    discount_amount: float = 0.0                                 # Flat amount off this line (computed)
    discount_type: Literal["flat", "percent"] = "flat"           # How user entered discount
    discount_value: float = 0.0                                  # Raw user-entered value (₹ for flat, % for percent)
    is_taxable: bool = False
    gst_rate: float = 0.0                                        # % e.g. 18
    taxable_value: float = 0.0                                   # = qty*unit_price - discount (stored)
    cgst_amount: float = 0.0
    sgst_amount: float = 0.0
    igst_amount: float = 0.0
    line_total: float = 0.0                                      # taxable_value + cgst + sgst (or + igst)
    # Optional product detail fields — populated when the line is a hearing
    # aid, accessory, or any tracked physical product. All optional so generic
    # service lines (consultation, audiogram, etc.) stay untouched.
    product_type: Optional[Literal["Hearing Aid", "Accessory", "Other"]] = None
    make: Optional[str] = None                                   # Brand: Phonak, Signia, etc.
    model: Optional[str] = None                                  # Model name / SKU
    serial_numbers: List[str] = []                               # One entry per unit; len should equal quantity for HAs
    technology_tier: Optional[Literal["Basic", "Essential", "Standard", "Advanced", "Premium"]] = None
    # Accessory stock plumbing — populated when the line is an Accessory.
    # `accessory_product_id` points at `ha_products.product_id`;
    # `accessory_variant` is the size/power label (e.g. "2M" for RIC
    # receivers, "M" for silicone domes). Both optional so legacy service
    # lines stay untouched. When set, they let the paid-invoice hook
    # decrement `accessory_stock` deterministically instead of guessing
    # from brand+model.
    accessory_product_id: Optional[str] = None
    accessory_variant: Optional[str] = None
    # Idempotency: set to True once the paid-invoice hook has decremented
    # stock for this line. Guards against re-decrement on subsequent
    # payments (partial → paid) or on background reconciliation loops.
    accessory_stock_decremented: bool = False


class Payment(BaseModel):
    model_config = ConfigDict(extra="ignore")
    payment_id: str = Field(default_factory=lambda: f"PAY-{str(uuid4())[:8].upper()}")
    # `clinic_id` and `invoice_id` are redundant when the Payment is
    # embedded inside an Invoice (which it always is in practice — the
    # parent Invoice carries both). Legacy embedded payments do not have
    # these fields, which caused the `DATA_HEALTH: invoices schema drift`
    # incident on 2026-06-02 (10/66 invoices failed Pydantic validation).
    # Keeping them Optional preserves the field for any future
    # de-normalisation needs without breaking historical data.
    clinic_id: Optional[str] = None
    invoice_id: Optional[str] = None
    # `kind` distinguishes patient-in-payments from clinic-out-refunds.
    # Refunds are stored as Payment rows with `kind="refund"` and a
    # NEGATIVE `amount` — `_sum_invoice()` then subtracts them
    # naturally from `paid_total`. Rows minted before the refund
    # feature (2026-07-30) don't have this field → treated as "payment"
    # by the default. Do NOT change the default: it preserves the
    # historic invariant that `sum(payments.amount) == paid_total`.
    kind: Literal["payment", "refund"] = "payment"
    method: Optional[Literal["cash", "upi", "card", "bank_transfer", "insurance"]] = None
    amount: float
    reference: Optional[str] = None                              # Txn ref / UPI UTR / card last-4
    # Free-text refund reason — required when kind=="refund", ignored otherwise.
    reason: Optional[str] = None
    paid_at: Optional[Union[str, datetime]] = Field(default_factory=datetime.utcnow)
    received_by_user_id: Optional[str] = None
    notes: Optional[str] = None


class PaymentCreate(BaseModel):
    method: Literal["cash", "upi", "card", "bank_transfer", "insurance"]
    amount: float
    reference: Optional[str] = None
    notes: Optional[str] = None


class RefundCreate(BaseModel):
    """Body for POST /api/billing/invoices/{id}/refund. All refunds are
    RECORD-ONLY — no gateway integration. Actual money movement is
    handled by the clinic offline (cash back, manual UPI reversal, bank
    transfer). The row lands in the same `payments` collection with
    `kind="refund"` for audit-trail purposes.
    """
    amount: float = Field(gt=0, description="Positive ₹ amount to refund")
    method: Literal["cash", "upi", "card", "bank_transfer", "insurance"]
    reason: str = Field(min_length=3, max_length=500)
    reference: Optional[str] = Field(default=None, max_length=120)
    notes: Optional[str] = Field(default=None, max_length=500)


class Invoice(BaseModel):
    model_config = ConfigDict(extra="ignore")
    invoice_id: str = Field(default_factory=lambda: f"INV-{str(uuid4())[:10].upper()}")
    clinic_id: str
    # NAV-008 · Human-facing invoice number. Canonical newly-generated
    # format is `INV/{YYYY}/{6-digit decimal}` produced by
    # `billing._next_invoice_no`. The regex accepts:
    #   · canonical decimal — `INV/2026/000042`
    #   · legacy 6-char hex — `INV/2026/0669C8`  (historical writers)
    #   · CSV-imported `IMP/…` prefix — see `imports.py`
    # Empty string / None is explicitly rejected — a defensive layer
    # that catches raw-insert bugs BEFORE they reach the DB uniqueness
    # index. Test fixtures / historical rows that lack `invoice_no`
    # continue to load via `find_one` because Pydantic runs on the
    # WRITE path only, not the read-back path (billing._deserialize
    # tolerates the absence).
    invoice_no: str = Field(pattern=r"^(INV|IMP)/\d{4}/[0-9A-Za-z\-]{4,32}$")   # Human-facing, e.g. "INV/2026/000123"

    patient_id: str
    patient_name: str
    patient_mobile: Optional[str] = None
    mrd: Optional[str] = None
    patient_address: Optional[str] = None
    patient_gstin: Optional[str] = None                          # B2B invoice

    appointment_id: Optional[str] = None
    session_id: Optional[str] = None                             # Linked M02 test session (for handover)
    ticket_no: Optional[str] = None                              # Linked HA service-ticket (auto-billed at handover)

    invoice_date: datetime = Field(default_factory=datetime.utcnow)

    lines: List[InvoiceLine] = []

    subtotal: float = 0.0                                        # Sum of line taxable_value
    discount_total: float = 0.0
    cgst_total: float = 0.0
    sgst_total: float = 0.0
    igst_total: float = 0.0
    tax_total: float = 0.0
    grand_total: float = 0.0
    rounded_total: float = 0.0                                   # After nearest-rupee round
    round_off: float = 0.0

    paid_total: float = 0.0
    # Positive display value: sum of |amount| across refund payments.
    # Added 2026-07-30 with the clinic refund flow.
    refunded_total: float = 0.0
    due_total: float = 0.0

    status: Literal["draft", "paid", "partial", "refunded", "partially_refunded", "cancelled"] = "draft"

    payments: List[Payment] = []

    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    created_by_user_id: Optional[str] = None
    cancelled_at: Optional[datetime] = None
    cancelled_reason: Optional[str] = None

    # Imported-via tag — populated when row created by /api/imports/patients/commit.
    imported_via: Optional[str] = None
    external_invoice_no: Optional[str] = None  # Original clinic bill # from CSV import
    linked_sale_no: Optional[str] = None       # If invoice was generated from an HA sale.


class InvoiceLineCreate(BaseModel):
    service_id: Optional[str] = None
    description: Optional[str] = None                            # Override service name if needed
    quantity: float = 1.0
    unit_price: Optional[float] = None                           # Override service price
    discount_amount: float = 0.0                                 # Legacy/computed flat amount (may be ignored if discount_type=percent)
    discount_type: Literal["flat", "percent"] = "flat"
    discount_value: float = 0.0                                  # Raw user-entered value (₹ for flat, % for percent)
    is_taxable: Optional[bool] = None                            # Override
    gst_rate: Optional[float] = None
    # When `True`, `unit_price` already includes GST and we back-calculate
    # the taxable value (legacy behaviour — default for product sales).
    # When `False`, `unit_price` is the pre-tax amount and we add GST on
    # top (the intuitive behaviour for flat-fee service charges like
    # "Consultation = ₹500 + 18% GST"). Explicit override beats the
    # service-level default.
    gst_inclusive: Optional[bool] = None
    hsn_sac: Optional[str] = None
    # Optional product detail fields (mirrored from InvoiceLine).
    product_type: Optional[Literal["Hearing Aid", "Accessory", "Other"]] = None
    make: Optional[str] = None
    model: Optional[str] = None
    serial_numbers: Optional[List[str]] = None
    technology_tier: Optional[Literal["Basic", "Essential", "Standard", "Advanced", "Premium"]] = None
    # Accessory stock plumbing — see `InvoiceLine` for semantics.
    accessory_product_id: Optional[str] = None
    accessory_variant: Optional[str] = None


class InvoiceCreate(BaseModel):
    patient_id: str
    appointment_id: Optional[str] = None
    session_id: Optional[str] = None
    lines: List[InvoiceLineCreate]
    patient_gstin: Optional[str] = None
    notes: Optional[str] = None
    initial_payment: Optional[PaymentCreate] = None              # Optional single-shot payment on create
    from_sale_no: Optional[str] = None                           # If invoice was generated from an HA sale, link them.


class ReportDelivery(BaseModel):
    """Log of each time a report was handed over (printed / emailed / WhatsApp'd)."""
    model_config = ConfigDict(extra="ignore")
    delivery_id: str = Field(default_factory=lambda: f"DEL-{str(uuid4())[:8].upper()}")
    clinic_id: str
    session_id: str
    patient_id: str
    invoice_id: Optional[str] = None
    channel: Literal["print", "whatsapp", "email", "in_person"]
    delivered_at: datetime = Field(default_factory=datetime.utcnow)
    delivered_by_user_id: str
    recipient: Optional[str] = None                              # Phone or email
    notes: Optional[str] = None



# ==================== PATIENT MODELS ====================

class Patient(BaseModel):
    model_config = ConfigDict(extra="ignore")

    patient_id: str = Field(default_factory=lambda: f"ACS-{datetime.now().year}-{str(uuid4())[:8].upper()}")
    clinic_id: str                                      # Tenant scope
    mrd: Optional[str] = None                           # Human-facing Medical Record Document number

    # Demographics
    name: str
    # ── Tolerance: legacy/seed rows occasionally have age=None or a
    # non-canonical gender string ("M", "F", "T"). Strict types here
    # caused production ResponseValidationErrors on any list that
    # included one such row. Keep them optional + permissive — the
    # registration form still enforces canonical values at write time.
    age: Optional[int] = None
    gender: Optional[str] = None
    # dob/anniversary_date may exist as legacy `datetime` rows — see
    # _normalize_date_str validators below.
    dob: Optional[Union[str, datetime, date]] = None
    occupation: Optional[str] = None

    # Contact
    mobile: Optional[str] = None
    alternate_mobile: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    pincode: Optional[str] = None

    # Identity
    aadhaar_last4: Optional[str] = None

    # Special occasions (used by birthday / anniversary auto-greetings)
    anniversary_date: Optional[Union[str, datetime, date]] = None      # ISO YYYY-MM-DD

    # Clinical triage at registration (one-liner — full case history lives in M02 Pre-Test)
    chief_complaint: Optional[str] = None
    complaint_duration: Optional[str] = None
    ear_side: Optional[Literal["Left", "Right", "Bilateral"]] = None

    # Referral + insurance
    referring_physician: Optional[str] = None           # Free-text fallback
    referring_doctor_id: Optional[str] = None           # FK into referring_doctors
    referral_source: Optional[str] = None               # Walk-in / Doctor / Online / Camp / Family / Partner / Other
    referral_partner_id: Optional[str] = None           # FK into referral_partners (M12)

    insurance_scheme: Optional[str] = None              # Cash / CGHS / ECHS / ESIC / Ayushman / Private / Other
    insurance_card_number: Optional[str] = None
    insurance_validity: Optional[str] = None
    insurance_beneficiary: Optional[str] = None         # e.g. "Self", "Spouse", "Dependant"

    notes: Optional[str] = None
    phone: Optional[str] = None  # Legacy compatibility

    # AUDINEXA Connect — DPDP-compliant explicit opt-in for WhatsApp messaging.
    whatsapp_consent: bool = False
    whatsapp_consent_at: Optional[str] = None           # ISO timestamp when granted
    whatsapp_consent_withdrawn_at: Optional[str] = None # ISO timestamp when revoked

    # ── Merge bookkeeping (set only when this row was folded into another) ──
    # When two accidentally-created rows are collapsed via POST /patients/merge,
    # the secondary row is soft-marked with these fields so the audit chain
    # stays intact. `merged_into` points to the surviving canonical patient_id.
    merged_into: Optional[str] = None
    merged_at: Optional[str] = None
    merged_by: Optional[str] = None                     # user_id of the owner who ran the merge

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    # ── Validators to coerce legacy data ─────────────────────────────
    # Some pre-2026 rows have `dob` / `anniversary_date` written as a
    # raw `datetime` object instead of an ISO `"YYYY-MM-DD"` string,
    # which made the strict Patient response model 500 on list/detail
    # endpoints. `mode='before'` runs before type coercion so we can
    # safely intercept and normalise.
    @field_validator("dob", "anniversary_date", mode="before")
    @classmethod
    def _coerce_legacy_dates(cls, v):
        return _normalize_date_str(v)


class PatientCreate(BaseModel):
    name: str
    # Walk-in / phone-in registrations don't always have age + gender at
    # first capture. Front-desk can still register the patient and
    # backfill demographics on first follow-up (a UI nudge fires on
    # next opening of the profile). Strict enums removed — the
    # registration form still defaults to canonical values.
    age: Optional[int] = None
    gender: Optional[str] = None
    dob: Optional[str] = None
    occupation: Optional[str] = None

    mobile: Optional[str] = None
    alternate_mobile: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    pincode: Optional[str] = None

    aadhaar_last4: Optional[str] = None

    # Special occasions
    anniversary_date: Optional[str] = None

    chief_complaint: Optional[str] = None
    complaint_duration: Optional[str] = None
    ear_side: Optional[Literal["Left", "Right", "Bilateral"]] = None

    referring_physician: Optional[str] = None
    referring_doctor_id: Optional[str] = None
    referral_source: Optional[str] = None
    referral_partner_id: Optional[str] = None

    insurance_scheme: Optional[str] = None
    insurance_card_number: Optional[str] = None
    insurance_validity: Optional[str] = None
    insurance_beneficiary: Optional[str] = None

    notes: Optional[str] = None
    phone: Optional[str] = None  # Legacy

    # AUDINEXA Connect opt-in (DPDP Act 2023). False until patient ticks the
    # consent box at registration.
    whatsapp_consent: bool = False

    # ── NAV-005 Sprint-3C · REG-002/003/004 hard validators ─────────
    # These fire on both POST /patients and PUT /patients/{id} because
    # both endpoints use `PatientCreate` as their body model. Silent
    # coercion is deliberately avoided — we raise `ValueError` so the
    # user sees the field-anchored FastAPI 422 with our message.
    @field_validator("dob", mode="after")
    @classmethod
    def _dob_not_in_future(cls, v):
        if not v:
            return v
        # Accept ISO YYYY-MM-DD strings; leave anything odd for the
        # existing legacy tolerance path to handle at Patient level.
        try:
            parsed = date.fromisoformat(v[:10]) if isinstance(v, str) else None
        except ValueError:
            return v  # let the existing normaliser cope
        if parsed and parsed > _ist_today():
            raise ValueError("DOB cannot be in the future.")
        return v

    @field_validator("anniversary_date", mode="after")
    @classmethod
    def _anniv_not_in_future(cls, v):
        if not v:
            return v
        try:
            parsed = date.fromisoformat(v[:10]) if isinstance(v, str) else None
        except ValueError:
            return v
        if parsed and parsed > _ist_today():
            raise ValueError("Anniversary date cannot be in the future.")
        return v

    @field_validator("email", mode="before")
    @classmethod
    def _email_format(cls, v):
        if v is None:
            return None
        if not isinstance(v, str):
            return v
        s = v.strip().lower()
        if not s:
            return None
        if not _EMAIL_RE.match(s):
            raise ValueError("Enter a valid email address (e.g. name@example.com).")
        return s

    @model_validator(mode="after")
    def _mobile_ne_alt_mobile(self):
        # REG-004: fire only when BOTH fields are non-empty and reduce
        # to the same digits. The cross-patient duplicate guard in
        # `POST /patients` remains authoritative for family-shared
        # phones — this validator is exclusively about the *same row*
        # having identical mobile and alternate_mobile (a data-entry
        # slip, never a legitimate business case).
        m = _last10(self.mobile)
        a = _last10(self.alternate_mobile)
        if m and a and m == a:
            raise ValueError("Mobile and Alternate Mobile cannot be the same.")
        return self


# ==================== REFERRING DOCTOR MODELS ====================

class ReferringDoctor(BaseModel):
    model_config = ConfigDict(extra="ignore")

    doctor_id: str = Field(default_factory=lambda: f"DR-{str(uuid4())[:8].upper()}")
    clinic_id: Optional[str] = None                # Populated by server from auth
    name: str
    specialty: Optional[str] = None        # e.g., ENT, GP, Paediatrics, Neurology
    clinic: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    notes: Optional[str] = None

    # ── Referral payout configuration ─────────────────────────────────
    # The clinic owner can set a cut for this doctor on TWO categories
    # independently: diagnostics revenue and HA-sales revenue. Within each
    # category the cut is EITHER a percentage of the booked revenue OR a
    # fixed flat amount per referred patient — never both at once (per
    # product call — "one mode active at a time" is simpler to reconcile
    # with manual cheque-cutting).
    #
    # Default: no payout (`mode=None`, `value=0`). Setting a category to
    # `mode=None` effectively disables payouts for that revenue stream
    # for this doctor, even if revenue accrues.
    diag_cut_mode: Optional[Literal["percent", "flat"]] = None
    diag_cut_value: float = 0.0
    ha_cut_mode: Optional[Literal["percent", "flat"]] = None
    ha_cut_value: float = 0.0

    # ── WhatsApp thank-you notifications ──────────────────────────────
    # Optional, opt-in per stream. When enabled and the doctor has a
    # `phone` set, an auto-thank-you WhatsApp is fired to the doctor:
    #   • notify_on_diag=True → sent when a referred patient's hearing
    #     test session flips to `completed` / report is printed.
    #   • notify_on_ha=True   → sent when a HA sale for a referred patient
    #     is marked delivered / closed.
    # Both default to False so nothing is sent unless the owner
    # explicitly enables it on the Settings → Referral Doctors form.
    notify_on_diag: bool = False
    notify_on_ha: bool = False

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ReferringDoctorCreate(BaseModel):
    name: str
    specialty: Optional[str] = None
    clinic: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    notes: Optional[str] = None
    # ── Optional payout config at create/update time ───────────────
    # If omitted, defaults to no-payout (mode=None, value=0). Owners
    # typically set these on the Settings → Referral Doctors form.
    diag_cut_mode: Optional[Literal["percent", "flat"]] = None
    diag_cut_value: Optional[float] = 0.0
    ha_cut_mode: Optional[Literal["percent", "flat"]] = None
    ha_cut_value: Optional[float] = 0.0
    # Opt-in per-stream WhatsApp thank-you (see model doc above).
    notify_on_diag: Optional[bool] = False
    notify_on_ha: Optional[bool] = False


# ==================== PATIENT JOURNAL / CHART NOTES ====================

class PatientNote(BaseModel):
    model_config = ConfigDict(extra="ignore")

    note_id: str = Field(default_factory=lambda: f"NOTE-{str(uuid4())[:10].upper()}")
    patient_id: str
    audiologist: Optional[str] = None
    text: str
    auto: bool = False                     # True for system-generated entries (session created, report printed, etc.)
    created_at: datetime = Field(default_factory=datetime.utcnow)

    # Imported-via tag — populated when row created by /api/imports/patients/commit.
    imported_via: Optional[str] = None
    visit_date: Optional[str] = None       # YYYY-MM-DD if from CSV/Excel import


class PatientNoteCreate(BaseModel):
    patient_id: str
    text: str
    audiologist: Optional[str] = None
    auto: bool = False


# ==================== AUDIOGRAM MODELS ====================

class AudiogramMeasurement(BaseModel):
    """Single threshold measurement"""
    frequency: int  # Hz (250, 500, 1000, etc.)
    threshold_db: Optional[int] = None  # dB HL
    masked: bool = False
    no_response: bool = False

class AudiogramData(BaseModel):
    """Complete audiogram for one ear"""
    ear: Literal["right", "left"]
    ac_measurements: List[AudiogramMeasurement] = []  # Air Conduction
    bc_measurements: List[AudiogramMeasurement] = []  # Bone Conduction
    pta_3freq: Optional[float] = None  # Average of 500, 1K, 2K
    pta_4freq: Optional[float] = None  # Average of 500, 1K, 2K, 4K


# ==================== SPEECH AUDIOMETRY MODELS ====================

class SpeechTest(BaseModel):
    """Speech audiometry results for one ear"""
    ear: Literal["right", "left"]
    srt: Optional[int] = None  # Speech Reception Threshold (dB)
    srt_masked: bool = False
    wds_percent: Optional[int] = None  # Word Discrimination Score (%)
    wds_presentation_level: Optional[int] = None  # dB
    wds_masked: bool = False
    sat: Optional[int] = None  # Speech Awareness Threshold
    mcl: Optional[int] = None  # Most Comfortable Level
    ucl: Optional[int] = None  # Uncomfortable Loudness Level


class SpeechRow(BaseModel):
    """Single row of the Speech Audiometry grid (Right / Left / Soundfield / Soundfield Aided).
    All values are free-form strings so clinicians can enter numbers ("55") or
    markers ("NR", "CNT") interchangeably.
    """
    sat: Optional[str] = None
    srt: Optional[str] = None
    masking: Optional[str] = None
    mcl: Optional[str] = None
    ucl: Optional[str] = None


class SpeechWRSPoint(BaseModel):
    """Single plotted point on the Speech Audiogram (WRS curve)."""
    db_hl: float
    percent: float
    masked: bool = False


class WordRecognitionRow(BaseModel):
    """Row of the Word Recognition table — unaided (Word List) + aided (Presentation) pair."""
    db_hl_unaided: Optional[str] = None
    percent_unaided: Optional[str] = None
    masking_unaided: Optional[str] = None
    db_hl_aided: Optional[str] = None
    percent_aided: Optional[str] = None
    masking_aided: Optional[str] = None


class WordRecognitionInNoiseRow(BaseModel):
    """Row of the Word Recognition in Noise table."""
    db_hl: Optional[str] = None
    percent: Optional[str] = None
    noise_level: Optional[str] = None


class SpeechAudiometryData(BaseModel):
    """Speech Audiometry dataset.
    - WRS curves per channel (audiogram plotting points)
    - `fields` is a flat key→string map for every SRT/SAT/WR/WRN/MCL/UCL/QSIN entry,
      keeping the model schema-free so we can iterate on the form layout without migrations.
    """
    wrs_right: List[SpeechWRSPoint] = []
    wrs_left: List[SpeechWRSPoint] = []
    wrs_soundfield: List[SpeechWRSPoint] = []
    wrs_soundfield_aided: List[SpeechWRSPoint] = []
    fields: Dict[str, str] = Field(default_factory=dict)


# ==================== PRE-TEST MODELS (Case History / Tuning Fork / Otoscopy) ====================

class HearingSpecifics(BaseModel):
    suspect_hearing_loss: Optional[Literal["yes", "no", "not_sure"]] = None
    better_ear: Optional[Literal["right", "left", "same"]] = None
    progression: Optional[Literal["fluctuating", "gradual", "rapid", "sudden"]] = None
    prior_test: bool = False
    prior_test_details: Optional[str] = None
    seen_physician: bool = False
    physician_details: Optional[str] = None
    earache_drainage_3mo: bool = False
    aural_fullness: bool = False
    aural_fullness_ear: Optional[Literal["right", "left", "both"]] = None
    aural_fullness_frequency: Optional[str] = None


class TinnitusDetail(BaseModel):
    ear: Optional[Literal["right", "left", "both"]] = None
    frequency: Optional[Literal["constant", "intermittent", "occasional"]] = None
    bothersome: Optional[Literal["yes", "no", "sometimes"]] = None
    sound_description: Optional[str] = None


class DizzinessDetail(BaseModel):
    dizzy_today: bool = False
    associated_symptoms: List[str] = []  # nausea/tinnitus/hearing_loss/vision/other
    frequency: Optional[str] = None
    falls_12mo: bool = False
    falls_count: Optional[int] = None
    falls_injured: bool = False
    falls_injury_details: Optional[str] = None


class NoiseExposure(BaseModel):
    exposed: bool = False
    description: Optional[str] = None


class FamilyHistory(BaseModel):
    hearing_loss_in_family: Optional[Literal["yes", "no", "not_sure"]] = None
    description: Optional[str] = None


class MedicalHistoryDetail(BaseModel):
    prior_head_neck_surgery: bool = False
    prior_head_neck_surgery_details: Optional[str] = None
    head_trauma: bool = False
    head_trauma_details: Optional[str] = None
    medications: Optional[str] = None
    conditions: List[str] = []  # diabetes, hypertension, stroke_tia, etc.


class HearingAidHistory(BaseModel):
    ever_used: bool = False
    currently_using: bool = False
    ear: Optional[Literal["right", "left", "both"]] = None
    years_of_use: Optional[str] = None
    regular_wear: Optional[bool] = None
    benefit: Optional[bool] = None
    problems: Optional[str] = None


class CommunicationNeeds(BaseModel):
    difficult_situations: List[str] = []  # tv/phone/restaurant/meeting/theatre/worship
    top_problem_areas: List[str] = []  # up to 3 free-text items
    phone_ear: Optional[Literal["right", "left", "switch"]] = None


# ==================== IMPEDANCE / TYMPANOMETRY MODELS ====================

class TympanogramEar(BaseModel):
    jerger_type: Optional[Literal["A", "As", "Ad", "B", "C"]] = None
    me_pressure: Optional[float] = None   # daPa
    compliance: Optional[float] = None    # mL
    volume: Optional[float] = None        # cc (ECV — reported value only, not used in curve plotting)
    probe_hz: Literal[226, 678, 800, 1000] = 226
    notes: Optional[str] = None


class Tympanometry(BaseModel):
    right: TympanogramEar = Field(default_factory=TympanogramEar)
    left: TympanogramEar = Field(default_factory=TympanogramEar)


class ReflexCell(BaseModel):
    level: Optional[str] = None     # free-form: numbers ("85"), alphabetic markers ("NR", "CNT"), or any combination
    volume: Optional[float] = None  # legacy — kept for backward compatibility, no longer displayed
    pressure: Optional[float] = None  # legacy — kept for backward compatibility, no longer displayed


class ReflexSide(BaseModel):
    freqs: Dict[str, ReflexCell] = Field(default_factory=dict)


class ReflexEar(BaseModel):
    ipsi: ReflexSide = Field(default_factory=ReflexSide)
    contra: ReflexSide = Field(default_factory=ReflexSide)


class AcousticReflex(BaseModel):
    enabled: bool = False
    right: ReflexEar = Field(default_factory=ReflexEar)
    left: ReflexEar = Field(default_factory=ReflexEar)


class ReflexDecay(BaseModel):
    enabled: bool = False
    right: ReflexEar = Field(default_factory=ReflexEar)
    left: ReflexEar = Field(default_factory=ReflexEar)


class ETManeuver(BaseModel):
    pressure_before: Optional[float] = None
    pressure_after: Optional[float] = None
    interpretation: Optional[Literal["positive", "negative", "equivocal"]] = None
    notes: Optional[str] = None


class ETEar(BaseModel):
    toynbee: ETManeuver = Field(default_factory=ETManeuver)
    valsalva: ETManeuver = Field(default_factory=ETManeuver)
    pressure_app: ETManeuver = Field(default_factory=ETManeuver)


class ETDysfunction(BaseModel):
    enabled: bool = False
    right: ETEar = Field(default_factory=ETEar)
    left: ETEar = Field(default_factory=ETEar)


class ETFIntactEar(BaseModel):
    """Williams ETF-Intact TM test — 3 sequential tympanograms produce 3 peak pressures.
    P1 = baseline · P2 = after Valsalva (positive swing) · P3 = after Toynbee (negative swing).
    ETF is considered intact if consecutive peaks shift by ≥15-30 daPa.
    """
    volume: Optional[float] = None         # mL (ECV — single value for the ear)
    pressure_1: Optional[float] = None     # daPa — baseline peak
    pressure_2: Optional[float] = None     # daPa — post-Valsalva peak
    pressure_3: Optional[float] = None     # daPa — post-Toynbee peak
    notes: Optional[str] = None


class ETFIntact(BaseModel):
    enabled: bool = False
    right: ETFIntactEar = Field(default_factory=ETFIntactEar)
    left: ETFIntactEar = Field(default_factory=ETFIntactEar)


class ImpedanceData(BaseModel):
    tympanometry: Tympanometry = Field(default_factory=Tympanometry)
    acoustic_reflex: AcousticReflex = Field(default_factory=AcousticReflex)
    reflex_decay: ReflexDecay = Field(default_factory=ReflexDecay)
    et_dysfunction: ETDysfunction = Field(default_factory=ETDysfunction)
    etf_intact: ETFIntact = Field(default_factory=ETFIntact)



class CaseHistory(BaseModel):
    """Expanded adult audiology case history"""
    # Core (minimal — always visible)
    chief_complaint: Optional[str] = None
    duration: Optional[str] = None  # e.g., "3 months"
    onset: Optional[Literal["sudden", "gradual", "unknown"]] = None
    affected_ear: Optional[Literal["right", "left", "both", "unknown"]] = None
    # Associated symptom flags (quick checkboxes)
    tinnitus: bool = False
    vertigo: bool = False
    otalgia: bool = False
    otorrhea: bool = False
    notes: Optional[str] = None

    # Extended sections (accordion)
    hearing_specifics: HearingSpecifics = Field(default_factory=HearingSpecifics)
    tinnitus_detail: TinnitusDetail = Field(default_factory=TinnitusDetail)
    dizziness_detail: DizzinessDetail = Field(default_factory=DizzinessDetail)
    noise_exposure: NoiseExposure = Field(default_factory=NoiseExposure)
    family_history: FamilyHistory = Field(default_factory=FamilyHistory)
    medical_history: MedicalHistoryDetail = Field(default_factory=MedicalHistoryDetail)
    hearing_aid_history: HearingAidHistory = Field(default_factory=HearingAidHistory)
    communication_needs: CommunicationNeeds = Field(default_factory=CommunicationNeeds)


class TuningForkTest(BaseModel):
    """Standard tuning-fork battery"""
    frequency_hz: Literal[256, 512, 1024, 2048] = 512
    # Rinne (AC vs BC) per ear
    rinne_right: Optional[Literal["positive", "negative", "equal"]] = None
    rinne_left: Optional[Literal["positive", "negative", "equal"]] = None
    rinne_notes: Optional[str] = None
    # Weber — where sound lateralizes
    weber: Optional[Literal["right", "left", "midline", "not_lateralized"]] = None
    weber_notes: Optional[str] = None
    # Absolute Bone Conduction per ear
    abc_right: Optional[Literal["normal", "reduced"]] = None
    abc_left: Optional[Literal["normal", "reduced"]] = None
    abc_notes: Optional[str] = None
    # Bing (occlusion effect) per ear
    bing_right: Optional[Literal["positive", "negative"]] = None
    bing_left: Optional[Literal["positive", "negative"]] = None
    bing_notes: Optional[str] = None


class EarOtoscopy(BaseModel):
    """Otoscopic findings for a single ear"""
    pinna: Optional[Literal["normal", "abnormal"]] = None
    eac: Optional[Literal["clear", "wax", "debris", "inflamed", "foreign_body", "other"]] = None
    tm: Optional[Literal[
        "intact_normal", "retracted", "bulging", "perforated",
        "dull", "erythematous", "effusion", "scarred", "other"
    ]] = None
    notes: Optional[str] = None
    image_base64: Optional[str] = None  # client-side resized (<= 800px), data-URI


class OtoscopyFinding(BaseModel):
    right: EarOtoscopy = Field(default_factory=EarOtoscopy)
    left: EarOtoscopy = Field(default_factory=EarOtoscopy)


class PreTestData(BaseModel):
    """Combined pre-test intake (case history + tuning fork + otoscopy)"""
    case_history: CaseHistory = Field(default_factory=CaseHistory)
    tuning_fork: TuningForkTest = Field(default_factory=TuningForkTest)
    otoscopy: OtoscopyFinding = Field(default_factory=OtoscopyFinding)


# ==================== TEST SESSION MODELS ====================

class TestSession(BaseModel):
    model_config = ConfigDict(extra="ignore")
    
    session_id: str = Field(default_factory=lambda: f"SES-{str(uuid4())[:12].upper()}")
    # NAV-005 Sprint-3A / CLIN-001: `clinic_id` is now first-class on the
    # canonical schema. Router stamps it from JWT on insert; legacy rows
    # without it are backfilled at startup via `patients.clinic_id`.
    # Kept Optional at the model layer for backwards-compat with any
    # in-flight docs (pre-backfill); every read/write path filters
    # by `clinic_id` explicitly regardless.
    clinic_id: Optional[str] = None
    patient_id: str
    test_date: datetime = Field(default_factory=datetime.utcnow)
    audiologist_name: Optional[str] = None
    audiologist_license: Optional[str] = None
    
    # Test Context
    test_reliability: Literal["good", "fair", "poor"] = "good"
    test_methods: List[str] = ["headphones"]  # headphones, inserts, sound_field, bone_vibrator
    
    # History/Symptoms
    symptoms: List[str] = []
    chief_complaint: Optional[str] = None
    history_notes: Optional[str] = None
    
    # Pre-Test (Case History + Tuning Fork + Otoscopy)
    pre_test_data: Optional[PreTestData] = None
    
    # Impedance / Tympanometry
    impedance_data: Optional[ImpedanceData] = None
    
    # Pure Tone Audiometry
    right_ear_audiogram: Optional[AudiogramData] = None
    left_ear_audiogram: Optional[AudiogramData] = None
    
    # Speech Audiometry
    right_ear_speech: Optional[SpeechTest] = None
    left_ear_speech: Optional[SpeechTest] = None
    speech_data: Optional[SpeechAudiometryData] = None

    # P2 clinical tabs — schema-free (evolves without migrations)
    special_tests_data: Optional[Dict[str, Dict[str, str]]] = None
    oae_data: Optional[Dict[str, Dict[str, str]]] = None
    soundfield_data: Optional[Dict[str, Dict[str, str]]] = None
    abr_data: Optional[Dict[str, Dict[str, str]]] = None
    pediatric_data: Optional[Dict[str, Dict[str, str]]] = None
    tinnitus_data: Optional[Dict[str, Dict[str, str]]] = None

    # Results Interpretation
    right_ear_degree: Optional[Literal["normal", "slight", "mild", "moderate", "moderately_severe", "severe", "profound"]] = None
    right_ear_type: Optional[Literal["normal", "conductive", "sensorineural", "mixed"]] = None
    right_ear_config: Optional[Literal["flat", "sloping", "rising", "notch", "u_shape", "high_freq", "low_freq"]] = None
    
    left_ear_degree: Optional[Literal["normal", "slight", "mild", "moderate", "moderately_severe", "severe", "profound"]] = None
    left_ear_type: Optional[Literal["normal", "conductive", "sensorineural", "mixed"]] = None
    left_ear_config: Optional[Literal["flat", "sloping", "rising", "notch", "u_shape", "high_freq", "low_freq"]] = None
    
    # Clinical Notes
    clinical_impression: Optional[str] = None
    recommendations: List[str] = []
    # Report Builder state — the audiologist's narrative + section
    # toggles. Populated by the ReportsPanel auto-save.
    puretone_findings: Optional[str] = None
    immitence_findings: Optional[str] = None
    speech_findings: Optional[str] = None
    findings_by_section: Optional[Dict[str, str]] = None
    provisional_diagnosis: Optional[str] = None
    further_advice: Optional[str] = None
    license: Optional[str] = None
    # Toggleable section checkboxes — authoritative for both live
    # preview + saved snapshots. Absent → frontend defaults.
    sections: Optional[List[Dict[str, Any]]] = None
    
    # Front-desk intake triage (copied from Appointment at session start)
    visit_type: Optional[Literal["referral", "walkin", "consultation"]] = "walkin"
    recommended_tests: List[str] = Field(default_factory=list)   # tests front-desk marked
    referred_by: Optional[str] = None                            # ENT / GP name when visit_type=referral
    appointment_id: Optional[str] = None                         # Link back for handover tracking
    # NAV-006 F-004-A (2026-08-18) — walk-in visit identity. When a session
    # is started via the Diagnostics Queue with a `token_id` (and no
    # scheduled appointment), we persist the token_id so that a SECOND
    # walk-in the same day for the same patient (a new token) can never
    # reuse the earlier session's row. Optional so pre-existing sessions
    # remain valid.
    token_id: Optional[str] = None

    # Report-handover lifecycle — simplified per ops manager review (Feb 2026).
    # draft → report_ready (audiologist "Generate & Print Report") → completed (FD "Consultation Finished")
    # Legacy states (test_completed, printed, handed_over) are migrated on boot — see server.py lifespan.
    report_status: Literal["draft", "report_ready", "completed"] = "draft"
    # Timestamps kept with same field names for backwards-compat. Semantics:
    #   test_completed_at / test_completed_by_user_id  → stamped when report generated
    #   printed_at                                     → also stamped at generate time (PDF IS the print)
    #   handed_over_at / handed_over_by_user_id        → stamped on Consultation Finished
    test_completed_at: Optional[datetime] = None
    test_completed_by_user_id: Optional[str] = None
    printed_at: Optional[datetime] = None
    handed_over_at: Optional[datetime] = None
    handed_over_by_user_id: Optional[str] = None

    # Metadata
    status: Literal["draft", "completed", "finalized"] = "draft"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class TestSessionCreate(BaseModel):
    patient_id: str
    audiologist_name: Optional[str] = None
    audiologist_license: Optional[str] = None
    test_reliability: Literal["good", "fair", "poor"] = "good"
    test_methods: List[str] = ["headphones"]
    symptoms: List[str] = []
    chief_complaint: Optional[str] = None
    # Optional — if provided, server pulls visit_type / recommended_tests / referred_by from the appointment
    appointment_id: Optional[str] = None


class TestSessionUpdate(BaseModel):
    test_reliability: Optional[Literal["good", "fair", "poor"]] = None
    test_methods: Optional[List[str]] = None
    symptoms: Optional[List[str]] = None
    chief_complaint: Optional[str] = None
    history_notes: Optional[str] = None
    pre_test_data: Optional[PreTestData] = None
    impedance_data: Optional[ImpedanceData] = None
    right_ear_audiogram: Optional[AudiogramData] = None
    left_ear_audiogram: Optional[AudiogramData] = None
    right_ear_speech: Optional[SpeechTest] = None
    left_ear_speech: Optional[SpeechTest] = None
    speech_data: Optional[SpeechAudiometryData] = None
    special_tests_data: Optional[Dict[str, Dict[str, str]]] = None
    oae_data: Optional[Dict[str, Dict[str, str]]] = None
    soundfield_data: Optional[Dict[str, Dict[str, str]]] = None
    abr_data: Optional[Dict[str, Dict[str, str]]] = None
    pediatric_data: Optional[Dict[str, Dict[str, str]]] = None
    tinnitus_data: Optional[Dict[str, Dict[str, str]]] = None
    right_ear_degree: Optional[str] = None
    right_ear_type: Optional[str] = None
    right_ear_config: Optional[str] = None
    left_ear_degree: Optional[str] = None
    left_ear_type: Optional[str] = None
    left_ear_config: Optional[str] = None
    clinical_impression: Optional[str] = None
    puretone_findings: Optional[str] = None
    immitence_findings: Optional[str] = None
    speech_findings: Optional[str] = None
    provisional_diagnosis: Optional[str] = None
    further_advice: Optional[str] = None
    referred_by: Optional[str] = None
    recommendations: Optional[List[str]] = None
    status: Optional[Literal["draft", "completed", "finalized"]] = None
    # Per-section findings narrative — dict keyed by section id (e.g.
    # "pure_tone", "tympanometry"). Populated by the ReportsPanel builder.
    findings_by_section: Optional[Dict[str, str]] = None
    # Toggleable Report Builder sections — audiologist's explicit
    # checkbox state per section. Each entry: {id: str, enabled: bool}.
    # Snapshot + live preview honour this list; missing ids fall back
    # to TOGGLEABLE_SECTIONS.defaultEnabled on the frontend.
    sections: Optional[List[Dict[str, Any]]] = None
    # Optional persisted license string shown in the signature block.
    license: Optional[str] = None
