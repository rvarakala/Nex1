"""Seed a fresh PREMIUM demo tenant — "The Sound Clinic — Bangaluru".

Idempotent: re-running purges only docs tagged with `signup_source=demo-sound-clinic`
(i.e. the demo tenant's data) and recreates them. Everything else in the DB is left alone.

Usage:
    cd /app/backend && python3 scripts/seed_demo_premium.py
"""
from __future__ import annotations

import asyncio
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from uuid import uuid4

# Make the backend root importable when running as a script.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402
from auth import hash_password  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CLINIC_ID = "tenant-sound-clinic-blr"
SIGNUP_SOURCE = "demo-sound-clinic"
TIER = "PREMIUM"
NOW = datetime.now(timezone.utc).replace(microsecond=0)
TODAY = NOW.date()
random.seed(42)  # reproducible data across re-runs


# ---------------------------------------------------------------------------
# Realistic seed data (Bangalore-centric)
# ---------------------------------------------------------------------------
BRANCH_ID = "BR-SOUNDCLINIC-HQ"

USERS = [
    # email, role, name, password
    ("owner@thesoundclinic.in",  "clinic_owner", "Dr. Rajesh Iyer",       "demo123"),
    ("aditi@thesoundclinic.in",  "audiologist",  "Dr. Aditi Krishnan",    "demo123"),
    ("vikram@thesoundclinic.in", "audiologist",  "Dr. Vikram Reddy",      "demo123"),
    ("meera@thesoundclinic.in",  "front_desk",   "Meera Bhat",            "demo123"),
    ("suresh@thesoundclinic.in", "technician",   "Suresh Kumar",          "demo123"),
    ("priya@thesoundclinic.in",  "accounts",     "Priya Nair",            "demo123"),
]

# 25 patients with realistic Indian / Karnataka demographics.
PATIENT_SEED = [
    ("Anil Kumar Bhat",        62, "Male",   "+919845001001", "Retired Engineer",      "Bilateral hearing loss for 2 years",         "Bilateral", "Doctor"),
    ("Lakshmi Devi Reddy",     58, "Female", "+919845001002", "Homemaker",             "Difficulty in conversations",                "Bilateral", "Doctor"),
    ("Arjun Krishnamurthy",    35, "Male",   "+919845001003", "Software Engineer",     "Tinnitus in right ear",                      "Right",     "Online"),
    ("Sushma Iyengar",         44, "Female", "+919845001004", "Teacher",               "Sudden hearing loss left ear",               "Left",      "Walk-in"),
    ("Ramachandra Hegde",      71, "Male",   "+919845001005", "Retired Govt Officer",  "Severe hearing loss, needs aids",            "Bilateral", "Family"),
    ("Pooja Shenoy",            8, "Female", "+919845001006", "Student",               "Speech delay, hearing screen requested",     "Bilateral", "Doctor"),
    ("Karthik Raj",            29, "Male",   "+919845001007", "IT Consultant",         "Pressure feeling after concert",             "Bilateral", "Online"),
    ("Geetha Murthy",          54, "Female", "+919845001008", "Bank Manager",          "Hearing aid follow-up",                      "Bilateral", "Walk-in"),
    ("Venkatesh Prasad",       66, "Male",   "+919845001009", "Retired Banker",        "AMC renewal & hearing check",                "Bilateral", "Family"),
    ("Anita Bhandari",         48, "Female", "+919845001010", "Architect",             "Mild hearing difficulty in meetings",        "Right",     "Online"),
    ("Suresh Babu",            72, "Male",   "+919845001011", "Retired",               "Both ears, progressive loss",                "Bilateral", "Camp"),
    ("Divya Pai",              33, "Female", "+919845001012", "Doctor",                "Tinnitus screening — self-referred",         "Bilateral", "Walk-in"),
    ("Manjunath Gowda",        59, "Male",   "+919845001013", "Farmer",                "Right ear blocked, wax suspected",           "Right",     "Doctor"),
    ("Shilpa Joshi",           40, "Female", "+919845001014", "Marketing Manager",     "Hearing loss after pregnancy",               "Left",      "Doctor"),
    ("Mohan Kamath",           67, "Male",   "+919845001015", "Retired Professor",     "Hearing aid not working",                    "Bilateral", "Walk-in"),
    ("Reshma Khan",            27, "Female", "+919845001016", "Journalist",            "Annual hearing check",                       "Bilateral", "Online"),
    ("Prakash Rao",            55, "Male",   "+919845001017", "Sales Executive",       "Right ear loss after viral fever",           "Right",     "Doctor"),
    ("Bharati Acharya",        69, "Female", "+919845001018", "Retired Teacher",       "Both aids need re-fitting",                  "Bilateral", "Family"),
    ("Naveen Suresh",          31, "Male",   "+919845001019", "Pilot",                 "DGCA medical hearing test",                  "Bilateral", "Walk-in"),
    ("Kavitha Subramanian",    51, "Female", "+919845001020", "Lawyer",                "Hearing loss & vertigo",                     "Bilateral", "Doctor"),
    ("Rohan Desai",            12, "Male",   "+919845001021", "Student",               "Pediatric hearing screening",                "Bilateral", "Doctor"),
    ("Asha Pillai",            38, "Female", "+919845001022", "Designer",              "OAE for occupational health",                "Bilateral", "Walk-in"),
    ("Girish Naik",            61, "Male",   "+919845001023", "Police Officer",        "Service-related noise exposure",             "Bilateral", "Camp"),
    ("Nandini Rao",            46, "Female", "+919845001024", "HR Manager",            "Hearing aid trial enquiry",                  "Right",     "Partner"),
    ("Vinod Shetty",           58, "Male",   "+919845001025", "Hotelier",              "Premium hearing aid sale follow-up",         "Bilateral", "Partner"),
]

VENDORS = [
    ("Phonak India",       "Anand Mehta",      "+912226572000", "phonak.in@phonak.com",    "27AAAAP1234A1Z5", "Maharashtra", "Mumbai"),
    ("Signia (WS Audio)",  "Rakesh Verma",     "+911244678900", "info@signia.com",         "06AAACS5678B1Z2", "Haryana",     "Gurugram"),
    ("ReSound India",      "Jyoti Saxena",     "+911140876500", "support@resound.in",      "07AAACR9101C1Z9", "Delhi",       "New Delhi"),
    ("Widex India",        "Mahesh Pandey",    "+912266781234", "india@widex.com",         "27AABCW2345D1Z6", "Maharashtra", "Mumbai"),
    ("Oticon India",       "Smita Bose",       "+913340567800", "info.in@oticon.com",      "19AAACO5678E1Z3", "West Bengal", "Kolkata"),
]

# (brand, model, form_factor, tech_tier, mrp, cost)
PRODUCTS = [
    ("Phonak",       "Audeo Lumity L90 RIC",     "RIC", "premium",  185000, 95000),
    ("Phonak",       "Audeo Paradise P50 RIC",   "RIC", "standard",  85000, 42000),
    ("Phonak",       "Bolero V70 BTE",           "BTE", "advanced", 125000, 62000),
    ("Signia",       "Pure Charge&Go AX 7",      "RIC", "premium",  175000, 88000),
    ("Signia",       "Motion P 5X",              "BTE", "standard",  95000, 47000),
    ("ReSound",      "OMNIA 9 RIE",              "RIC", "premium",  195000, 98000),
    ("ReSound",      "Key 4 BTE",                "BTE", "essential", 38000, 19000),
    ("Widex",        "Moment Sheer 440",         "RIC", "premium",  178000, 89000),
    ("Oticon",       "Real 1 miniRITE",          "RIC", "premium",  185000, 92000),
    ("Oticon",       "Zircon 1 BTE",             "BTE", "advanced", 115000, 57500),
]

REFERRAL_PARTNERS = [
    ("Dr. Sanjay ENT Hospital",     "Dr. Sanjay Murthy",    "+919845555101", "dr.sanjay@entcare.in",     "doctor",  "REF-SANJAY"),
    ("Manipal Hospital — Whitefield","Dr. Lakshmi Iyer",     "+919845555102", "lakshmi.i@manipal.in",     "hospital","REF-MANIPAL"),
    ("Apollo Clinic — Indiranagar", "Dr. Praveen Rao",      "+919845555103", "praveen@apollo.in",        "clinic",  "REF-APOLLO"),
    ("Mythri Speech & Hearing",     "Sushma Hebbar",        "+919845555104", "sushma@mythri.in",         "clinic",  "REF-MYTHRI"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _id(prefix: str) -> str:
    return f"{prefix}-{str(uuid4())[:8].upper()}"


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _color_for(uid: str) -> str:
    palette = ["#F59E0B", "#3B82F6", "#8B5CF6", "#EC4899", "#10B981", "#EF4444",
               "#0EA5E9", "#F97316", "#14B8A6", "#6366F1"]
    h = 0
    for ch in uid:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return palette[h % len(palette)]


# ---------------------------------------------------------------------------
# Main seeder
# ---------------------------------------------------------------------------
async def seed():
    mongo_url = os.environ["MONGO_URL"]
    db_name = os.environ["DB_NAME"]
    client = AsyncIOMotorClient(mongo_url)
    db = client[db_name]

    print(f"\nConnecting to mongo db='{db_name}'...")
    print(f"Seeding tenant '{CLINIC_ID}' (PREMIUM)...\n")

    # ---- 1. Wipe previous demo data (idempotent re-seed) -----------------
    print("• Purging any prior demo data for this tenant ...")
    purge_collections = [
        "clinics", "users", "branches", "patients", "appointments",
        "test_sessions", "vendors", "ha_products", "serial_items", "grns",
        "purchase_orders", "quotations", "ha_sales", "ha_fittings", "ha_trials",
        "ha_amc_contracts", "ha_amc_plans", "service_tickets",
        "invoices", "payments", "services", "tokens", "queue_tokens",
        "waitlist", "referral_partners", "partner_payouts",
        "patient_feedback", "patient_otps", "tenant_feature_flags",
        "report_deliveries", "ha_followups",
    ]
    for c in purge_collections:
        try:
            await db[c].delete_many({"clinic_id": CLINIC_ID})
        except Exception:  # noqa: BLE001 — collection may not exist
            pass
    # Special: delete clinic doc + users by clinic_id
    await db.clinics.delete_many({"clinic_id": CLINIC_ID})
    await db.users.delete_many({"clinic_id": CLINIC_ID})
    # Reset clinic-scoped numbering counters so reseeded GRN/JOB/PO/SALE etc.
    # numbers stay aligned with the seeded sample data.
    try:
        await db.counters.delete_many({"_id": {"$regex": f":{CLINIC_ID}:"}})
    except Exception:  # noqa: BLE001
        pass

    # ---- 2. Clinic ----------------------------------------------------------
    await db.clinics.insert_one({
        "clinic_id": CLINIC_ID,
        "name": "The Sound Clinic — Bangaluru",
        "city": "Bengaluru",
        "state": "Karnataka",
        "country": "India",
        "address": "#123, 100 Feet Road, Indiranagar, Bengaluru 560038",
        "pincode": "560038",
        "phone": "+918045671234",
        "email": "hello@thesoundclinic.in",
        "gstin": "29AAACT1234S1Z5",
        "mrd_prefix": "TSC",
        "subscription_tier": TIER,
        "signup_source": SIGNUP_SOURCE,
        "status": "active",
        "appointment_peer_visibility": True,
        "tagline": "Listen Better. Live Brighter.",
        "created_at": _iso(NOW - timedelta(days=180)),
    })

    # ---- 3. Branch + tenant flags ------------------------------------------
    await db.branches.insert_one({
        "branch_id": BRANCH_ID,
        "clinic_id": CLINIC_ID,
        "name": "Indiranagar HQ",
        "city": "Bengaluru",
        "state": "Karnataka",
        "pincode": "560038",
        "phone": "+918045671234",
        "gstin": "29AAACT1234S1Z5",
        "is_primary": True,
        "active": True,
        "created_at": _iso(NOW - timedelta(days=180)),
    })
    await db.tenant_feature_flags.insert_one({
        "clinic_id": CLINIC_ID,
        "frontdesk": True, "diagnostics": True, "hearing-aids": True,
        "repair": True, "analytics": True, "referral-partners": True,
        "patient-portal": True, "audinexa-connect": True, "reports": True,
    })

    # ---- 4. Users -----------------------------------------------------------
    user_docs: list[dict] = []
    user_id_by_email: dict[str, str] = {}
    for email, role, name, pw in USERS:
        uid = f"USR-{str(uuid4())[:8].upper()}"
        user_id_by_email[email] = uid
        user_docs.append({
            "user_id": uid,
            "clinic_id": CLINIC_ID,
            "email": email,
            "name": name,
            "role": role,
            "active": True,
            "branch_ids": [BRANCH_ID],
            "primary_clinic_id": CLINIC_ID,
            "additional_clinic_ids": [],
            "appointment_color": _color_for(uid) if role in ("audiologist", "clinic_owner", "front_desk", "technician") else None,
            "password_hash": hash_password(pw),
            "created_at": _iso(NOW - timedelta(days=180)),
        })
    if user_docs:
        await db.users.insert_many(user_docs)
    owner_id = user_id_by_email["owner@thesoundclinic.in"]

    # Grant the clinic owner read access to two more existing demo tenants so
    # the multi-clinic switcher in the toolbar has something to show.
    extra_clinics = ["tenant-kims-hearing", "tenant-apollo-audiology"]
    existing_extra = await db.clinics.distinct("clinic_id", {"clinic_id": {"$in": extra_clinics}})
    if existing_extra:
        await db.users.update_one(
            {"user_id": owner_id},
            {"$set": {"additional_clinic_ids": existing_extra}},
        )
    aud1_id = user_id_by_email["aditi@thesoundclinic.in"]
    aud2_id = user_id_by_email["vikram@thesoundclinic.in"]
    front_id = user_id_by_email["meera@thesoundclinic.in"]
    tech_id = user_id_by_email["suresh@thesoundclinic.in"]
    audiologist_ids = [aud1_id, aud2_id]

    # ---- 5. Service catalogue ---------------------------------------------
    service_catalog = [
        ("CONS",  "Consultation",            "Audiology",   1500, False),
        ("PTA",   "Pure Tone Audiometry",    "Audiology",   2000, False),
        ("IMP",   "Immittance / Tympanometry","Audiology",  1500, False),
        ("OAE",   "OAE Screening",           "Audiology",   1200, False),
        ("ABR",   "ABR / BERA",              "Audiology",   4500, False),
        ("ASSR",  "ASSR",                    "Audiology",   3500, False),
        ("SPEECH","Speech Audiometry",       "Audiology",   1800, False),
        ("VEST",  "Vestibular Test (VNG)",   "Audiology",   3500, False),
        ("HAT",   "Hearing Aid Trial",       "Hearing Aid",  500, False),
        ("HAF",   "Hearing Aid Fitting",     "Hearing Aid", 2500, False),
        ("EARMOLD","Custom Earmould (pair)", "Accessory",   3500, True),
        ("REPAIR","Service / Repair Charge", "Service",     1500, True),
    ]
    services_docs = []
    service_by_code: dict[str, dict] = {}
    for code, name, cat, price, taxable in service_catalog:
        sid = _id("SVC")
        doc = {
            "service_id": sid, "clinic_id": CLINIC_ID,
            "code": code, "name": name, "category": cat,
            "price": float(price), "gst_rate": 18.0 if taxable else 0.0,
            "gst_inclusive": True, "is_taxable": bool(taxable), "active": True,
            "hsn_sac": "9021" if cat == "Hearing Aid" else "999312",
            "created_at": _iso(NOW - timedelta(days=180)),
        }
        services_docs.append(doc)
        service_by_code[code] = doc
    # NOTE: We deliberately DO NOT insert these into `db.services` — clinics
    # should curate their own Service Catalogue from Settings → Service Catalogue.
    # The local `service_by_code` dict is still used to snapshot description /
    # price / hsn on seeded invoice lines below (those lines carry no `service_id`
    # so they remain valid even with an empty catalogue).

    # ---- 6. Patients --------------------------------------------------------
    patient_docs = []
    bangalore_areas = [
        ("Indiranagar",     "560038"), ("Koramangala", "560034"), ("Whitefield",  "560066"),
        ("HSR Layout",      "560102"), ("Jayanagar",   "560011"), ("Malleshwaram","560003"),
        ("Banashankari",    "560070"), ("Bellandur",   "560103"), ("Marathahalli","560037"),
        ("JP Nagar",        "560078"), ("BTM Layout",  "560076"), ("Yelahanka",   "560064"),
    ]
    for i, (name, age, gender, mobile, occ, complaint, ear, source) in enumerate(PATIENT_SEED):
        area, pincode = random.choice(bangalore_areas)
        pid = f"TSC-{NOW.year}-{str(uuid4())[:8].upper()}"
        mrd = f"TSC-{NOW.year}-{str(i + 1).zfill(6)}"
        patient_docs.append({
            "patient_id": pid, "clinic_id": CLINIC_ID, "mrd": mrd,
            "name": name, "age": age, "gender": gender,
            "mobile": mobile, "phone": mobile,
            "email": f"{name.split()[0].lower()}.{name.split()[-1].lower()}@gmail.com",
            "address": f"#{random.randint(1, 200)}, {area}",
            "city": "Bengaluru", "state": "Karnataka", "pincode": pincode,
            "occupation": occ,
            "chief_complaint": complaint, "ear_side": ear,
            "complaint_duration": random.choice(["1 week", "1 month", "3 months", "6 months", "1 year", "2 years"]),
            "referral_source": source,
            "insurance_scheme": random.choice(["Cash", "Cash", "Cash", "Private", "CGHS", "Ayushman"]),
            "created_at": _iso(NOW - timedelta(days=random.randint(15, 170))),
            "updated_at": _iso(NOW - timedelta(days=random.randint(0, 14))),
        })
    await db.patients.insert_many(patient_docs)
    print(f"  ✓ Patients: {len(patient_docs)}")

    # ---- 7. Vendors ---------------------------------------------------------
    vendor_docs = []
    vendor_by_brand: dict[str, dict] = {}
    for vname, contact, phone, email, gstin, state, city in VENDORS:
        vid = _id("VND")
        doc = {
            "vendor_id": vid, "clinic_id": CLINIC_ID,
            "name": vname, "contact_person": contact, "phone": phone, "email": email,
            "gstin": gstin, "state": state, "address": city,
            "payment_terms_days": 45, "active": True,
            "created_at": _iso(NOW - timedelta(days=170)),
        }
        vendor_docs.append(doc)
        # Store first word of vendor name as brand key (Phonak / Signia / ReSound / Widex / Oticon)
        vendor_by_brand[vname.split()[0]] = doc
    await db.vendors.insert_many(vendor_docs)
    print(f"  ✓ Vendors: {len(vendor_docs)}")

    # ---- 8. HA Products -----------------------------------------------------
    product_docs = []
    for brand, model, ff, tier, mrp, cost in PRODUCTS:
        pid = _id("PRD")
        product_docs.append({
            "product_id": pid, "clinic_id": CLINIC_ID,
            "brand": brand, "model": model,
            "form_factor": ff, "tech_tier": tier,
            "connectivity": ["bluetooth", "rechargeable"] if tier in ("premium", "advanced") else [],
            "warranty_months": 36 if tier == "premium" else 24,
            "mrp": float(mrp), "cost": float(cost),
            "min_sell_price": float(int(mrp * 0.85)),
            "hsn": "9021", "gst_rate": 18.0,
            "is_serialised": True, "active": True,
            "created_at": _iso(NOW - timedelta(days=170)),
        })
    await db.ha_products.insert_many(product_docs)
    print(f"  ✓ Products: {len(product_docs)}")

    # ---- 9. Serial inventory + GRNs ----------------------------------------
    serial_docs = []
    grn_docs = []
    grn_seq = 1
    # 4 serials per product → 40 serial items
    for prod in product_docs:
        brand = prod["brand"]
        vendor = vendor_by_brand.get(brand) or vendor_docs[0]
        # 4 GRN line per product → distribute states
        for n in range(4):
            sid = _id("SI")
            state = random.choices(
                ["IN_STOCK", "SOLD", "TRIAL_OUT", "RESERVED"],
                weights=[5, 3, 1, 1],
            )[0]
            serial_no = f"{brand[:3].upper()}-{prod['model'].split()[-1]}-{2026000 + grn_seq * 10 + n}"
            wend = (NOW.date() + timedelta(days=365 * (3 if prod["warranty_months"] >= 36 else 2))).isoformat()
            serial_docs.append({
                "serial_id": sid, "clinic_id": CLINIC_ID, "branch_id": BRANCH_ID,
                "product_id": prod["product_id"], "serial_no": serial_no,
                "state": state, "pool": "saleable",
                "warranty_end_date": wend,
                "grn_no": f"GRN-2026-{str(grn_seq).zfill(4)}",
                "created_at": _iso(NOW - timedelta(days=random.randint(20, 150))),
            })
        grn_docs.append({
            "grn_no": f"GRN-2026-{str(grn_seq).zfill(4)}",
            "clinic_id": CLINIC_ID, "branch_id": BRANCH_ID,
            "vendor_id": vendor["vendor_id"], "vendor_name": vendor["name"],
            "po_no": None,
            "received_at": _iso(NOW - timedelta(days=random.randint(20, 150))),
            "received_by_user_id": owner_id,
            "lines": [{"product_id": prod["product_id"], "qty": 4, "unit_cost": prod["cost"]}],
            "subtotal": prod["cost"] * 4, "gst_amount": prod["cost"] * 4 * 0.18,
            "total": prod["cost"] * 4 * 1.18, "notes": f"Stock receipt — {prod['brand']} {prod['model']}",
        })
        grn_seq += 1
    await db.serial_items.insert_many(serial_docs)
    await db.grns.insert_many(grn_docs)
    # Advance the GRN counter so live POSTs don't collide with seeded numbers
    await db.counters.update_one(
        {"_id": f"grn:{CLINIC_ID}:2026"},
        {"$set": {"seq": len(grn_docs)}},
        upsert=True,
    )
    print(f"  ✓ Serial items: {len(serial_docs)} | GRNs: {len(grn_docs)}")

    # ---- 10. Appointments (~60: patient + vendor + sales rep + internal + tech_staff) -
    appt_docs = []
    used_slots: set[tuple] = set()  # (staff_id, start_iso) — dedupe overlaps
    statuses = ["scheduled", "confirmed", "completed", "completed", "completed",
                "checked_in", "in_progress", "cancelled", "no_show"]

    def _add_appt(staff_id: str, start_dt: datetime, dur: int, *,
                  cp_type: str, cp_name: str, patient: dict | None = None,
                  service: str = "Consultation", category: str = "consultation",
                  status: str | None = None, notes: str | None = None,
                  cp_phone: str | None = None, cp_company: str | None = None,
                  cp_id: str | None = None):
        key = (staff_id, start_dt.replace(microsecond=0).isoformat())
        if key in used_slots:
            return None
        used_slots.add(key)
        end_dt = start_dt + timedelta(minutes=dur)
        # Auto-status: future → scheduled/confirmed; past → completed/cancelled/no_show
        if status is None:
            status = random.choice(["scheduled", "confirmed"]) if start_dt > NOW else random.choice(statuses)
        staff_color = next((u["appointment_color"] for u in user_docs if u["user_id"] == staff_id), None)
        staff_name = next((u["name"] for u in user_docs if u["user_id"] == staff_id), "")
        staff_role = next((u["role"] for u in user_docs if u["user_id"] == staff_id), "")
        doc = {
            "appointment_id": _id("APT"), "clinic_id": CLINIC_ID,
            "patient_id": patient["patient_id"] if patient else None,
            "patient_name": patient["name"] if patient else None,
            "patient_mobile": patient["mobile"] if patient else None,
            "mrd": patient["mrd"] if patient else None,
            "counterparty_type": cp_type,
            "counterparty_id": cp_id or (patient["patient_id"] if patient else None),
            "counterparty_name": cp_name,
            "counterparty_phone": cp_phone or (patient["mobile"] if patient else None),
            "counterparty_company": cp_company,
            "staff_id": staff_id, "staff_name": staff_name, "staff_role": staff_role,
            "staff_color": staff_color,
            "audiologist_id": staff_id, "audiologist_name": staff_name,
            "service": service, "category": category,
            "priority": random.choice(["normal", "normal", "normal", "urgent"]),
            "visit_type": "walkin",
            "recommended_tests": random.sample(["pta", "impedance", "oae", "speech"], k=random.randint(0, 2)),
            "start_at": _iso(start_dt), "end_at": _iso(end_dt),
            "duration_minutes": dur, "status": status,
            "notes": notes, "reminder_sent": status in ("confirmed", "completed"),
            "created_at": _iso(start_dt - timedelta(days=random.randint(1, 7))),
            "updated_at": _iso(start_dt - timedelta(days=random.randint(0, 1))),
            "created_by_user_id": front_id,
        }
        appt_docs.append(doc)
        return doc

    # Patient appointments — spread across past 14 days + next 14 days
    for i in range(45):
        offset_days = random.randint(-12, 12)
        hour = random.choice([9, 10, 10, 11, 11, 14, 15, 15, 16, 17])
        minute = random.choice([0, 15, 30, 45])
        start = (NOW + timedelta(days=offset_days)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if start.weekday() == 6:  # Sunday → bump to Monday
            start += timedelta(days=1)
        patient = random.choice(patient_docs)
        staff = random.choice(audiologist_ids)
        service = random.choice(["Consultation", "PTA", "Immittance", "OAE", "Speech Audiometry",
                                  "Hearing Aid Fitting", "Hearing Aid Trial", "Follow-up"])
        category = "fitting" if "Aid" in service else "diagnostic" if service != "Consultation" else "consultation"
        _add_appt(staff, start, random.choice([30, 30, 45, 60]),
                  cp_type="patient", cp_name=patient["name"], patient=patient,
                  service=service, category=category)

    # Vendor visits (clinic owner meets reps)
    for vendor in vendor_docs[:4]:
        offset = random.choice([-3, 1, 5, 8])
        start = (NOW + timedelta(days=offset)).replace(hour=11, minute=0, second=0, microsecond=0)
        _add_appt(owner_id, start, 60, cp_type="vendor",
                  cp_name=vendor["name"], cp_phone=vendor["phone"],
                  cp_company=vendor["name"], cp_id=vendor["vendor_id"],
                  service="Vendor Meeting", category="meeting",
                  notes=f"Quarterly business review with {vendor['contact_person']}")

    # Sales rep visits (free-text)
    sales_reps = [
        ("Anand Mehta (Phonak)",     "+919845011001", "Phonak India"),
        ("Rakesh Verma (Signia)",    "+919845011002", "Signia"),
        ("Jyoti Saxena (ReSound)",   "+919845011003", "ReSound India"),
    ]
    for rep_name, rep_phone, company in sales_reps:
        offset = random.randint(-5, 10)
        start = (NOW + timedelta(days=offset)).replace(hour=12, minute=0, second=0, microsecond=0)
        _add_appt(owner_id, start, 45, cp_type="sales_rep",
                  cp_name=rep_name, cp_phone=rep_phone, cp_company=company,
                  service="Product Demo", category="demo",
                  notes=f"New product line demo from {company}")

    # Tech staff (Suresh) — repair appointments
    for i in range(4):
        offset = random.randint(-6, 8)
        start = (NOW + timedelta(days=offset)).replace(hour=14, minute=random.choice([0, 30]), second=0, microsecond=0)
        patient = random.choice(patient_docs)
        _add_appt(tech_id, start, 30, cp_type="patient", cp_name=patient["name"],
                  patient=patient, service="Service / Repair", category="other",
                  notes="In-house service appointment")

    # Internal team meetings
    for label, day_off, hour in [
        ("Weekly clinical huddle", -7, 8), ("Inventory close-out", -3, 18),
        ("Audiology team standup", 1, 8), ("Vendor pricing review", 5, 17),
        ("Quarterly review", 10, 9),
    ]:
        start = (NOW + timedelta(days=day_off)).replace(hour=hour, minute=0, second=0, microsecond=0)
        _add_appt(owner_id, start, 60, cp_type="internal", cp_name=label,
                  service="Team Meeting", category="meeting",
                  notes="Internal — all staff")

    # Tech staff "other" — courier dispatch
    for i in range(2):
        start = (NOW + timedelta(days=random.randint(-4, 3))).replace(hour=16, minute=30, second=0, microsecond=0)
        _add_appt(tech_id, start, 30, cp_type="other",
                  cp_name=random.choice(["Bluedart courier pickup", "Phonak Mumbai dispatch"]),
                  service="Logistics", category="other")

    await db.appointments.insert_many(appt_docs)
    print(f"  ✓ Appointments: {len(appt_docs)}")

    # ---- 11. Test sessions (audiograms — completed reports for past appts) -
    completed_patient_appts = [a for a in appt_docs
                                if a["status"] == "completed" and a["counterparty_type"] == "patient"
                                and a["service"] in ("PTA", "Hearing Aid Fitting", "Consultation")][:18]
    session_docs = []
    for a in completed_patient_appts:
        sid = _id("SES")
        # PTA-style measurement set
        air_right = [{"freq_hz": f, "level_db": random.choice([20, 25, 30, 35, 40, 50, 55, 60])}
                     for f in [250, 500, 1000, 2000, 4000, 8000]]
        air_left  = [{"freq_hz": f, "level_db": random.choice([15, 20, 30, 40, 45, 55])}
                     for f in [250, 500, 1000, 2000, 4000, 8000]]
        bone_right = [{"freq_hz": f, "level_db": random.choice([15, 20, 25, 30])} for f in [500, 1000, 2000, 4000]]
        session_docs.append({
            "session_id": sid, "clinic_id": CLINIC_ID,
            "patient_id": a["patient_id"], "patient_name": a["patient_name"],
            "mrd": a["mrd"], "appointment_id": a["appointment_id"],
            "audiologist_id": a["staff_id"], "audiologist_name": a["staff_name"],
            "test_date": a["start_at"][:10],
            "report_status": "completed",
            "audiogram": {
                "air_conduction_right": air_right,
                "air_conduction_left": air_left,
                "bone_conduction_right": bone_right,
                "bone_conduction_left": [],
            },
            "speech_audiometry": {
                "right": {"srt": random.choice([25, 30, 40]),  "wrs": random.randint(72, 96)},
                "left":  {"srt": random.choice([25, 35, 40]),  "wrs": random.randint(70, 95)},
            },
            "diagnosis": random.choice([
                "Bilateral mild-to-moderate sensorineural hearing loss",
                "Right ear moderate sensorineural hearing loss",
                "Bilateral mixed hearing loss",
                "Bilateral high-frequency hearing loss",
            ]),
            "recommendation": "Hearing aid trial recommended. Counselling provided.",
            "created_at": a["start_at"],
            "completed_at": a["end_at"],
        })
    if session_docs:
        await db.test_sessions.insert_many(session_docs)
    print(f"  ✓ Test sessions: {len(session_docs)}")

    # ---- 12. Quotations -----------------------------------------------------
    quote_docs = []
    seq = 1
    for patient in random.sample(patient_docs, 6):
        prod = random.choice(product_docs)
        unit_price = float(int(prod["mrp"] * random.uniform(0.88, 0.97)))
        quote_docs.append({
            "quote_no": f"QT/2026/{str(seq).zfill(4)}",
            "clinic_id": CLINIC_ID, "branch_id": BRANCH_ID,
            "patient_id": patient["patient_id"], "patient_name": patient["name"],
            "patient_mobile": patient["mobile"],
            "lines": [{
                "product_id": prod["product_id"],
                "description": f"{prod['brand']} {prod['model']}",
                "qty": 2, "unit_price": unit_price, "gst_rate": 18.0,
            }],
            "subtotal": unit_price * 2,
            "gst_amount": unit_price * 2 * 0.18,
            "total": round(unit_price * 2 * 1.18, 2),
            "status": random.choice(["sent", "sent", "accepted", "expired"]),
            "valid_until": (TODAY + timedelta(days=random.randint(7, 30))).isoformat(),
            "created_by_user_id": random.choice(audiologist_ids),
            "created_at": _iso(NOW - timedelta(days=random.randint(2, 30))),
        })
        seq += 1
    await db.quotations.insert_many(quote_docs)
    print(f"  ✓ Quotations: {len(quote_docs)}")

    # ---- 13. HA Sales (12) + linked invoices -------------------------------
    sale_docs = []
    invoice_docs = []
    fitting_docs = []
    inv_seq = 1
    sale_seq = 1
    sold_serials = [s for s in serial_docs if s["state"] == "SOLD"][:12]
    for s in sold_serials:
        prod = next(p for p in product_docs if p["product_id"] == s["product_id"])
        patient = random.choice(patient_docs)
        unit_price = float(int(prod["mrp"] * random.uniform(0.85, 0.95)))
        sale_no = f"SL/2026/{str(sale_seq).zfill(4)}"

        sale_docs.append({
            "sale_no": sale_no, "clinic_id": CLINIC_ID, "branch_id": BRANCH_ID,
            "patient_id": patient["patient_id"], "patient_name": patient["name"],
            "patient_mobile": patient["mobile"],
            "lines": [{
                "product_id": prod["product_id"], "serial_id": s["serial_id"],
                "serial_no": s["serial_no"],
                "description": f"{prod['brand']} {prod['model']}",
                "qty": 1, "unit_price": unit_price, "gst_rate": 18.0,
            }],
            "subtotal": unit_price, "gst_amount": unit_price * 0.18,
            "total": round(unit_price * 1.18, 2),
            "status": "completed",
            "audiologist_id": random.choice(audiologist_ids),
            "sold_at": _iso(NOW - timedelta(days=random.randint(3, 90))),
            "created_at": _iso(NOW - timedelta(days=random.randint(3, 90))),
        })

        # Update the serial item with patient binding
        await db.serial_items.update_one(
            {"serial_id": s["serial_id"]},
            {"$set": {"current_patient_id": patient["patient_id"], "state": "SOLD"}},
        )

        # Linked invoice (paid)
        gross = round(unit_price * 1.18, 2)
        inv_no = f"INV/2026/{str(inv_seq).zfill(6)}"
        invoice_docs.append({
            "invoice_id": _id("INV"), "clinic_id": CLINIC_ID, "invoice_no": inv_no,
            "patient_id": patient["patient_id"], "patient_name": patient["name"],
            "patient_mobile": patient["mobile"], "mrd": patient["mrd"],
            "patient_address": patient.get("address"),
            "invoice_date": _iso(NOW - timedelta(days=random.randint(3, 90))),
            "lines": [{
                "service_id": None,
                "description": f"{prod['brand']} {prod['model']} (S/N {s['serial_no']})",
                "quantity": 1, "unit_price": unit_price,
                "discount_amount": 0, "discount_type": "flat", "discount_value": 0,
                "gst_rate": 18.0, "is_taxable": True, "hsn_sac": "9021",
                "taxable_value": unit_price, "tax_amount": round(unit_price * 0.18, 2),
                "line_total": gross,
            }],
            "subtotal": unit_price, "discount_total": 0,
            "cgst_total": round(unit_price * 0.09, 2), "sgst_total": round(unit_price * 0.09, 2),
            "igst_total": 0, "tax_total": round(unit_price * 0.18, 2),
            "grand_total": gross, "rounded_total": round(gross), "round_off": round(round(gross) - gross, 2),
            "paid_total": round(gross), "due_total": 0,
            "status": "paid",
            "payments": [{
                "payment_id": _id("PAY"),
                "amount": round(gross),
                "method": random.choice(["upi", "card", "cash"]),
                "ref_no": f"REF{random.randint(100000, 999999)}",
                "paid_at": _iso(NOW - timedelta(days=random.randint(3, 90))),
            }],
            "created_at": _iso(NOW - timedelta(days=random.randint(3, 90))),
            "created_by_user_id": front_id,
        })

        # Fitting record
        fitting_docs.append({
            "fitting_id": _id("FIT"),
            "clinic_id": CLINIC_ID, "branch_id": BRANCH_ID,
            "patient_id": patient["patient_id"], "patient_name": patient["name"],
            "sale_no": sale_no,
            "serial_id": s["serial_id"], "serial_no": s["serial_no"],
            "product_id": prod["product_id"],
            "audiologist_id": random.choice(audiologist_ids),
            "ear_side": random.choice(["Bilateral", "Right", "Left"]),
            "fitting_date": _iso(NOW - timedelta(days=random.randint(1, 60))),
            "status": "completed",
            "settings_snapshot": {"gain_low": random.randint(20, 35), "gain_mid": random.randint(25, 40),
                                  "noise_reduction": "moderate"},
            "patient_feedback": random.choice([
                "Clear sound, comfortable fit.",
                "Will return after 1 week for fine-tuning.",
                "Excellent — patient happy.",
                "Mild occlusion reported, vent enlarged.",
            ]),
            "created_at": _iso(NOW - timedelta(days=random.randint(1, 60))),
        })
        inv_seq += 1
        sale_seq += 1
    await db.ha_sales.insert_many(sale_docs)
    if invoice_docs:
        await db.invoices.insert_many(invoice_docs)
        # NAV-008 · Sync the atomic invoice counter to the max seq we
        # just assigned. This closes the class of duplicates caused by
        # the seed bypassing `_next_invoice_no` (root cause of the
        # observed Preview `INV/2026/000004` collision). Uses `$max` so
        # a later real-user invoice cannot collide with a lower value.
        await db.counters.update_one(
            {"_id": f"invoice:{CLINIC_ID}:2026"},
            {"$max": {"seq": inv_seq - 1}},
            upsert=True,
        )
    if fitting_docs:
        await db.ha_fittings.insert_many(fitting_docs)
    print(f"  ✓ HA sales: {len(sale_docs)} | Invoices: {len(invoice_docs)} | Fittings: {len(fitting_docs)}")

    # ---- 14. Active trials --------------------------------------------------
    trial_docs = []
    for s in [si for si in serial_docs if si["state"] == "TRIAL_OUT"][:3]:
        prod = next(p for p in product_docs if p["product_id"] == s["product_id"])
        patient = random.choice(patient_docs)
        start_d = NOW.date() - timedelta(days=random.randint(2, 8))
        trial_docs.append({
            "trial_id": _id("TRL"),
            "trial_no": f"TR-2026-{str(len(trial_docs)+1).zfill(4)}",
            "clinic_id": CLINIC_ID, "branch_id": BRANCH_ID,
            "patient_id": patient["patient_id"], "patient_name": patient["name"],
            "patient_mobile": patient["mobile"],
            "serial_id": s["serial_id"], "serial_no": s["serial_no"],
            "product_id": prod["product_id"], "product_label": f"{prod['brand']} {prod['model']}",
            "start_date": start_d.isoformat(),
            "return_date": (start_d + timedelta(days=14)).isoformat(),
            "status": "active",
            "audiologist_id": random.choice(audiologist_ids),
            "trial_fee": 500,
            "notes": "14-day take-home trial",
            "created_at": _iso(NOW - timedelta(days=random.randint(2, 8))),
        })
    if trial_docs:
        await db.ha_trials.insert_many(trial_docs)
    print(f"  ✓ Trials: {len(trial_docs)}")

    # ---- 15. AMC contracts --------------------------------------------------
    amc_docs = []
    for i in range(5):
        sale = sale_docs[i]
        patient_id = sale["patient_id"]
        patient = next(p for p in patient_docs if p["patient_id"] == patient_id)
        sn = sale["lines"][0]["serial_no"]
        sid = sale["lines"][0]["serial_id"]
        start_d = NOW.date() - timedelta(days=random.randint(10, 200))
        amc_docs.append({
            "contract_no": f"AMC/2026/{str(i+1).zfill(4)}",
            "clinic_id": CLINIC_ID, "branch_id": BRANCH_ID,
            "patient_id": patient_id, "patient_name": patient["name"],
            "patient_mobile": patient["mobile"],
            "serial_id": sid, "serial_no": sn,
            "plan_name": "Premium Care", "duration_months": 12,
            "fee": 8500, "fee_paid": 8500,
            "amc_start_date": start_d.isoformat(),
            "amc_expiry_date": (start_d + timedelta(days=365)).isoformat(),
            "status": "active",
            "covered_services": ["Cleaning", "Hearing test", "Free repairs (parts excluded)", "Tubing/dome change"],
            "created_at": _iso(NOW - timedelta(days=random.randint(10, 200))),
        })
    if amc_docs:
        await db.ha_amc_contracts.insert_many(amc_docs)
    print(f"  ✓ AMC contracts: {len(amc_docs)}")

    # ---- 16. Service / Repair tickets --------------------------------------
    ticket_docs = []
    for i in range(8):
        patient = random.choice(patient_docs)
        sale = random.choice(sale_docs)
        status = random.choice(["open", "in_progress", "in_progress", "in_progress", "resolved", "resolved"])
        ticket_docs.append({
            "ticket_no": f"JOB-2026-{str(i+1).zfill(4)}",
            "clinic_id": CLINIC_ID, "branch_id": BRANCH_ID,
            "patient_id": patient["patient_id"], "patient_name": patient["name"],
            "patient_mobile": patient["mobile"],
            "serial_id": sale["lines"][0]["serial_id"],
            "serial_no": sale["lines"][0]["serial_no"],
            "kind": "repair",
            "complaint": random.choice([
                "Right device intermittent — no sound",
                "Battery drain reported by patient",
                "Receiver replacement needed",
                "Microphone clarity reduced",
                "Bluetooth pairing failure",
                "Tube discoloured — replacement",
            ]),
            "diagnosis": "Pending technician inspection" if status == "open"
                          else "Device serviced and verified",
            "status": status,
            "cost_to_patient": float(random.choice([0, 0, 850, 1200, 2500, 3500])),
            "warranty_covered": False,
            "technician_user_id": tech_id,
            "technician_name": "Suresh Kumar",
            "created_by_user_id": owner_id,
            "received_at": _iso(NOW - timedelta(days=random.randint(0, 25))),
            "created_at": _iso(NOW - timedelta(days=random.randint(0, 25))),
            "resolved_at": _iso(NOW - timedelta(days=random.randint(0, 5))) if status == "resolved" else None,
        })
    await db.service_tickets.insert_many(ticket_docs)
    # Advance the JOB counter so future tickets don't collide with seeded numbers
    await db.counters.update_one(
        {"_id": f"job:{CLINIC_ID}:2026"},
        {"$set": {"seq": len(ticket_docs)}},
        upsert=True,
    )
    print(f"  ✓ Service tickets: {len(ticket_docs)}")

    # ---- 17. Standalone diagnostic invoices (mixed status) -----------------
    extra_invoices = []
    for i in range(8):
        patient = random.choice(patient_docs)
        svc = random.choice([service_by_code["PTA"], service_by_code["IMP"], service_by_code["OAE"], service_by_code["CONS"]])
        gross = svc["price"]
        status = random.choice(["paid", "paid", "partial", "pending"])
        paid = gross if status == "paid" else (gross * 0.5 if status == "partial" else 0)
        inv_no = f"INV/2026/{str(inv_seq).zfill(6)}"
        extra_invoices.append({
            "invoice_id": _id("INV"), "clinic_id": CLINIC_ID, "invoice_no": inv_no,
            "patient_id": patient["patient_id"], "patient_name": patient["name"],
            "patient_mobile": patient["mobile"], "mrd": patient["mrd"],
            "invoice_date": _iso(NOW - timedelta(days=random.randint(0, 30))),
            "lines": [{
                "service_id": None, "description": svc["name"],
                "quantity": 1, "unit_price": gross,
                "discount_amount": 0, "discount_type": "flat", "discount_value": 0,
                "gst_rate": 0, "is_taxable": False, "hsn_sac": svc.get("hsn_sac"),
                "taxable_value": gross, "tax_amount": 0, "line_total": gross,
            }],
            "subtotal": gross, "discount_total": 0,
            "cgst_total": 0, "sgst_total": 0, "igst_total": 0, "tax_total": 0,
            "grand_total": gross, "rounded_total": gross, "round_off": 0,
            "paid_total": paid, "due_total": gross - paid,
            "status": "paid" if status == "paid" else ("partial" if status == "partial" else "draft"),
            "payments": [{
                "payment_id": _id("PAY"),
                "amount": paid, "method": random.choice(["upi", "cash", "card"]),
                "ref_no": f"REF{random.randint(100000, 999999)}",
                "paid_at": _iso(NOW - timedelta(days=random.randint(0, 30))),
            }] if paid > 0 else [],
            "created_at": _iso(NOW - timedelta(days=random.randint(0, 30))),
            "created_by_user_id": front_id,
        })
        inv_seq += 1
    if extra_invoices:
        await db.invoices.insert_many(extra_invoices)
        # NAV-008 · Second seed batch — sync counter again to whichever
        # is higher of (existing counter, inv_seq we just consumed).
        await db.counters.update_one(
            {"_id": f"invoice:{CLINIC_ID}:2026"},
            {"$max": {"seq": inv_seq - 1}},
            upsert=True,
        )
    print(f"  ✓ Diagnostic invoices: {len(extra_invoices)}")

    # ---- 18. Referral partners + payouts -----------------------------------
    partner_docs = []
    payout_docs = []
    for name, contact, phone, email, kind, code in REFERRAL_PARTNERS:
        pid = _id("RPT")
        partner_docs.append({
            "partner_id": pid, "clinic_id": CLINIC_ID,
            "name": name, "contact_person": contact, "phone": phone, "email": email,
            "kind": kind, "referral_code": code,
            "commission_rate_pct": random.choice([5, 7.5, 10]),
            "status": "active",
            "address": "Bengaluru, Karnataka",
            "total_referrals": random.randint(8, 35),
            "lifetime_revenue": random.randint(45000, 280000),
            "created_at": _iso(NOW - timedelta(days=random.randint(40, 170))),
        })
        for _ in range(random.randint(1, 3)):
            payout_docs.append({
                "payout_id": _id("PYO"), "clinic_id": CLINIC_ID,
                "partner_id": pid, "partner_name": name,
                "amount": random.choice([3500, 5500, 7800, 12000, 15500]),
                "status": random.choice(["paid", "paid", "pending"]),
                "paid_at": _iso(NOW - timedelta(days=random.randint(2, 60))),
                "method": "bank_transfer",
                "ref_no": f"NEFT{random.randint(10000000, 99999999)}",
                "created_at": _iso(NOW - timedelta(days=random.randint(2, 60))),
            })
    await db.referral_partners.insert_many(partner_docs)
    if payout_docs:
        await db.partner_payouts.insert_many(payout_docs)
    print(f"  ✓ Referral partners: {len(partner_docs)} | Payouts: {len(payout_docs)}")

    # ---- 19. Patient feedback ---------------------------------------------
    feedback_quotes = [
        ("Excellent service. Dr. Aditi was very patient with my elderly mother.",  5),
        ("Clean clinic, modern equipment. Highly recommend.",                       5),
        ("Quick appointment, no waiting. Thank you.",                                5),
        ("Detailed explanation of audiogram results.",                               5),
        ("A bit pricey but worth it. Hearing aid working great.",                    4),
        ("Reception staff was helpful and warm.",                                    5),
    ]
    feedback_docs = []
    for quote, rating in feedback_quotes:
        patient = random.choice(patient_docs)
        feedback_docs.append({
            "feedback_id": _id("FBK"), "clinic_id": CLINIC_ID,
            "patient_id": patient["patient_id"], "patient_name": patient["name"],
            "rating": rating, "comment": quote,
            "channel": "patient_portal",
            "created_at": _iso(NOW - timedelta(days=random.randint(1, 30))),
        })
    await db.patient_feedback.insert_many(feedback_docs)
    print(f"  ✓ Patient feedback: {len(feedback_docs)}")

    # ---- 20. Today's tokens (front-desk live queue) -----------------------
    today_tokens = []
    for i in range(8):
        patient = random.choice(patient_docs)
        state = "waiting" if i < 3 else "in_testing" if i < 5 else "completed"
        today_tokens.append({
            "token_id": str(uuid4()), "clinic_id": CLINIC_ID,
            "token_no": f"T-{i+1:03d}",
            "patient_id": patient["patient_id"], "patient_name": patient["name"],
            "patient_mobile": patient["mobile"],
            "service": random.choice(["PTA", "Consultation", "OAE"]),
            "priority": random.choice(["normal", "normal", "urgent"]),
            "state": state,
            "issued_at": _iso(NOW - timedelta(hours=random.randint(0, 5))),
            "called_at": _iso(NOW - timedelta(hours=random.randint(0, 4))) if state != "waiting" else None,
            "completed_at": _iso(NOW - timedelta(hours=random.randint(0, 2))) if state == "completed" else None,
        })
    await db.tokens.insert_many(today_tokens)
    print(f"  ✓ Today's tokens: {len(today_tokens)}")

    # ---- 21. Waitlist -----------------------------------------------------
    waitlist_docs = []
    for patient in random.sample(patient_docs, 3):
        waitlist_docs.append({
            "entry_id": _id("WL"), "clinic_id": CLINIC_ID,
            "patient_id": patient["patient_id"], "patient_name": patient["name"],
            "patient_mobile": patient["mobile"], "mrd": patient["mrd"],
            "preferred_audiologist_id": random.choice(audiologist_ids),
            "preferred_service": random.choice(["PTA", "Hearing Aid Fitting"]),
            "preferred_date": (TODAY + timedelta(days=random.randint(1, 7))).isoformat(),
            "status": "active",
            "notes": "Patient called — needs earliest slot",
            "created_at": _iso(NOW - timedelta(days=random.randint(0, 3))),
        })
    await db.waitlist.insert_many(waitlist_docs)
    print(f"  ✓ Waitlist: {len(waitlist_docs)}")

    # ----------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("✅ DEMO TENANT SEED COMPLETE")
    print("=" * 60)
    print(f"  Clinic     : The Sound Clinic — Bangaluru")
    print(f"  Tier       : {TIER}")
    print(f"  Login URL  : (use your preview URL) /login")
    print()
    print("  CREDENTIALS")
    print("  -----------")
    for email, role, name, pw in USERS:
        print(f"  {role:<14} {email:<30} {pw:<10} ({name})")
    print()
    client.close()


if __name__ == "__main__":
    asyncio.run(seed())
