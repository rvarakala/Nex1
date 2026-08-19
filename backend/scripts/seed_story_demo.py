"""Seed the two use-case stories for the /demo route.

Populates `tenant-sound-clinic-blr` with EXACTLY the demo shape the
clinician asked for:

    Dr. Anand Kumar (ENT, MBBS DLO) as a referring doctor (opted-in
    for both diagnostic + HA-sale WhatsApp thank-yous, 500 rupee flat
    per diagnostic + 5% per HA sale).

    6 patients — one per story branch — each referred by Dr. Anand
    Kumar and each with the exact clinical picture the demo copy
    describes.

Idempotent: uses stable `patient_id`s and `patient_id`-scoped
appointment/invoice/quote inserts, deleting any prior rows before
re-inserting.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Any

import motor.motor_asyncio
from dotenv import load_dotenv

load_dotenv("/app/backend/.env")
sys.path.insert(0, "/app/backend")

CLINIC_ID = "tenant-sound-clinic-blr"
BRANCH_ID = "BR-SC-BLR-001"

DR_AK_ID = "REF-DOC-DEMO-AK-001"
NOW = datetime.now(timezone.utc)
NOW_ISO = NOW.isoformat()


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


PATIENTS: list[dict[str, Any]] = [
    # Story 1 — Diagnostic-only, Mild Conductive HL
    {
        "patient_id": "PT-STORY-01",
        "mrd": "TSC-2026-STORY01",
        "name": "Rohan Menon",
        "age": 42, "gender": "Male",
        "mobile": "+919845090001", "email": "rohan.menon.demo@audinexa.test",
        "city": "Bengaluru", "state": "Karnataka", "pincode": "560068",
        "chief_complaint": "Right ear feels blocked after cold, mild hearing reduction 3 wks",
        "complaint_duration": "3 weeks", "ear_side": "Bilateral",
        "referral_source": "Doctor",
        # Story 1 diagnosis tag (used by report generator)
        "_story": "01_diagnostic_conductive",
    },
    # Story 2 — Diagnostic + HA recommendation, Moderate Sloping SNHL
    {
        "patient_id": "PT-STORY-02",
        "mrd": "TSC-2026-STORY02",
        "name": "Priya Nair",
        "age": 34, "gender": "Female",
        "mobile": "+919845090002", "email": "priya.nair.demo@audinexa.test",
        "city": "Bengaluru", "state": "Karnataka", "pincode": "560034",
        "chief_complaint": "People sound like they are mumbling — worse in noisy rooms",
        "complaint_duration": "6 months", "ear_side": "Bilateral",
        "referral_source": "Doctor",
        "_story": "02_diagnostic_snhl",
    },
    # Story 2.a — In-clinic HA trial (fresh case)
    {
        "patient_id": "PT-STORY-02A",
        "mrd": "TSC-2026-STORY02A",
        "name": "Sneha Bhat",
        "age": 55, "gender": "Female",
        "mobile": "+919845090003",
        "city": "Bengaluru", "state": "Karnataka", "pincode": "560076",
        "chief_complaint": "Missing TV dialogues — spouse complains about volume",
        "complaint_duration": "1 year", "ear_side": "Bilateral",
        "referral_source": "Doctor",
        "_story": "02a_trial_inclinic",
    },
    # Story 2.b — Home trial (fresh case)
    {
        "patient_id": "PT-STORY-02B",
        "mrd": "TSC-2026-STORY02B",
        "name": "Karthik Iyer",
        "age": 62, "gender": "Male",
        "mobile": "+919845090004",
        "city": "Bengaluru", "state": "Karnataka", "pincode": "560095",
        "chief_complaint": "Hearing loss after retirement — wants to try HA at home first",
        "complaint_duration": "2 years", "ear_side": "Bilateral",
        "referral_source": "Doctor",
        "_story": "02b_trial_home",
    },
    # Story 2.c — Buys from stock (fresh case)
    {
        "patient_id": "PT-STORY-02C",
        "mrd": "TSC-2026-STORY02C",
        "name": "Meera Rao",
        "age": 48, "gender": "Female",
        "mobile": "+919845090005",
        "city": "Bengaluru", "state": "Karnataka", "pincode": "560003",
        "chief_complaint": "Trialed Phonak Audeo Lumity 30 RIC for a week — wants to buy",
        "complaint_duration": "N/A — post-trial purchase", "ear_side": "Bilateral",
        "referral_source": "Doctor",
        "_story": "02c_buy_from_stock",
    },
    # Story 2.c.1 — Buys but out of stock, advance + PO
    {
        "patient_id": "PT-STORY-02C1",
        "mrd": "TSC-2026-STORY02C1",
        "name": "Ravi Kumar",
        "age": 58, "gender": "Male",
        "mobile": "+919845090006",
        "city": "Bengaluru", "state": "Karnataka", "pincode": "560102",
        "chief_complaint": "Trialed and confirmed — wants Phonak Audeo Lumity 30, currently OOS",
        "complaint_duration": "N/A — post-trial purchase, awaiting stock",
        "ear_side": "Bilateral", "referral_source": "Doctor",
        "_story": "02c1_buy_out_of_stock",
    },
]


async def wipe(db):
    """Idempotent — delete anything from previous runs."""
    ids = [p["patient_id"] for p in PATIENTS]
    await db.referring_doctors.delete_many({"clinic_id": CLINIC_ID, "doctor_id": DR_AK_ID})
    await db.patients.delete_many({"clinic_id": CLINIC_ID, "patient_id": {"$in": ids}})
    await db.appointments.delete_many({"clinic_id": CLINIC_ID, "patient_id": {"$in": ids}})
    await db.hearing_report_versions.delete_many({"patient_id": {"$in": ids}})
    await db.invoices.delete_many({"clinic_id": CLINIC_ID, "patient_id": {"$in": ids}})
    await db.ha_sales.delete_many({"clinic_id": CLINIC_ID, "patient_id": {"$in": ids}})
    await db.ha_quotes.delete_many({"clinic_id": CLINIC_ID, "patient_id": {"$in": ids}})
    await db.ha_trials.delete_many({"clinic_id": CLINIC_ID, "patient_id": {"$in": ids}})
    await db.purchase_orders.delete_many({"clinic_id": CLINIC_ID, "patient_id": {"$in": ids}})
    await db.referral_notifications.delete_many({"clinic_id": CLINIC_ID, "referring_doctor_id": DR_AK_ID})
    print("  ✓ wiped previous story rows")


async def seed_referring_doctor(db):
    doc = {
        "doctor_id": DR_AK_ID,
        "clinic_id": CLINIC_ID,
        "name": "Dr. Anand Kumar",
        "qualifications": "MBBS, DLO",
        "specialty": "ENT",
        "clinic_name": "AK ENT & Voice Clinic",
        "mobile": "+919845000123",
        "email": "dr.anandkumar.demo@audinexa.test",
        "city": "Bengaluru",
        # Payout config — flat ₹500 per diagnostic, 5% per HA sale
        "diag_cut_mode": "flat", "diag_cut_amount": 500,
        "ha_cut_mode": "percent", "ha_cut_amount": 5,
        # WhatsApp thank-you opt-ins
        "notify_on_diagnostics": True,
        "notify_on_ha_sale": True,
        "active": True,
        "created_at": NOW_ISO, "updated_at": NOW_ISO,
    }
    await db.referring_doctors.insert_one(doc)
    print(f"  ✓ Dr. Anand Kumar (ENT) referring doctor seeded")


async def seed_patients(db):
    docs = []
    for p in PATIENTS:
        row = {**p}
        story_tag = row.pop("_story", None)
        docs.append({
            "clinic_id": CLINIC_ID,
            "branch_id": BRANCH_ID,
            "referring_doctor_id": DR_AK_ID,
            "referring_physician": "Dr. Anand Kumar (ENT)",
            "whatsapp_consent": True,
            "whatsapp_consent_at": NOW_ISO,
            "created_at": NOW_ISO, "updated_at": NOW_ISO,
            "_story_tag": story_tag,
            **row,
        })
    await db.patients.insert_many(docs)
    print(f"  ✓ 6 story patients seeded (all referred by Dr. AK)")


async def seed_appointments(db):
    # One past-dated appt per patient — represents the visit for the tests
    docs = []
    base = NOW - timedelta(days=3, hours=6)
    for i, p in enumerate(PATIENTS):
        appt_time = base + timedelta(days=i, hours=1)
        docs.append({
            "appointment_id": f"APPT-STORY-{i+1:02d}",
            "clinic_id": CLINIC_ID, "branch_id": BRANCH_ID,
            "patient_id": p["patient_id"],
            "patient_name": p["name"],
            "start_time": iso(appt_time),
            "end_time": iso(appt_time + timedelta(minutes=45)),
            "status": "completed",
            "type": "Diagnostic" if "diagnostic" in (p.get("_story") or "")
                    else "HA Trial" if "trial" in (p.get("_story") or "")
                    else "HA Sale",
            "notes": p["chief_complaint"],
            "created_at": NOW_ISO,
        })
    await db.appointments.insert_many(docs)
    print(f"  ✓ 6 appointments (past-dated for storyboard)")


# ── Audiogram data ────────────────────────────────────────────────
# 4-frequency PTA + tympano interpretations for the two clinical
# stories. Kept SEPARATE from any UI model — the demo reads these
# as `hearing_report_versions.builder_state` blobs.

MILD_CONDUCTIVE = {
    "right": {"250": 25, "500": 35, "1000": 40, "2000": 35, "4000": 30, "8000": 25},
    "left":  {"250": 25, "500": 30, "1000": 35, "2000": 30, "4000": 25, "8000": 25},
    "right_bc": {"500": 10, "1000": 10, "2000": 15, "4000": 15},
    "left_bc":  {"500": 10, "1000": 10, "2000": 10, "4000": 15},
    "tymp_right": "Type As", "tymp_left": "Type As",
    "srt_right": 30, "srt_left": 30,
    "wrs_right": 96, "wrs_left": 96,
    "diagnosis": "Bilateral Mild Conductive Hearing Loss",
    "recommendation": "ENT consultation & follow-up. Rule out otitis media / eustachian tube dysfunction.",
    "ent_notes": "AB Gap increased (~25 dB across 500-2000 Hz). Type As tympanogram bilaterally. WRS excellent — consistent with conductive pathology.",
    "avg_pta_right": 37, "avg_pta_left": 32,
}

MODERATE_SLOPING_SNHL = {
    "right": {"250": 25, "500": 30, "1000": 45, "2000": 55, "4000": 65, "8000": 70},
    "left":  {"250": 25, "500": 30, "1000": 40, "2000": 55, "4000": 65, "8000": 70},
    "right_bc": {"500": 30, "1000": 45, "2000": 55, "4000": 65},
    "left_bc":  {"500": 30, "1000": 40, "2000": 55, "4000": 65},
    "tymp_right": "Type A", "tymp_left": "Type A",
    "srt_right": 45, "srt_left": 45,
    "wrs_right": 78, "wrs_left": 82,
    "diagnosis": "Bilateral Moderate Sloping Sensorineural Hearing Loss",
    "recommendation": "Bilateral hearing aid trial. ENT consultation & follow-up. Counsel for regular audiological reviews.",
    "ent_notes": "No AB Gap — SNHL confirmed. High-frequency sloping consistent with age-related hearing loss. HA candidacy strong.",
    "avg_pta_right": 49, "avg_pta_left": 46,
}


async def seed_reports(db):
    """Only Rohan (Story 1) + Priya (Story 2) get a signed report — the
    HA-branch patients (2.a…2.c1) start from an existing SNHL diagnosis
    and are already trial/sale flow, not diagnostic flow."""
    docs = []
    for p in PATIENTS:
        story = p.get("_story", "")
        picture = None
        if story == "01_diagnostic_conductive":
            picture = MILD_CONDUCTIVE
        elif story == "02_diagnostic_snhl":
            picture = MODERATE_SLOPING_SNHL
        elif story.startswith("02"):
            # HA-branch patients share the SNHL picture as their pre-existing dx
            picture = MODERATE_SLOPING_SNHL

        if not picture:
            continue

        docs.append({
            "session_id": f"HRV-STORY-{p['patient_id']}",
            "patient_id": p["patient_id"],
            "clinic_id": CLINIC_ID,
            "signed_by": "Dr. Aditi Krishnan",
            "signed_at": NOW_ISO,
            "saved_at": NOW_ISO,
            "created_at": NOW_ISO,
            "builder_state": picture,
            "status": "signed",
        })
    if docs:
        await db.hearing_report_versions.insert_many(docs)
    print(f"  ✓ {len(docs)} signed hearing reports")


async def seed_invoices(db):
    """Diagnostic invoices for Stories 1 + 2 (+ HA branches to give
    the sales screens something to display).

    NAV-008 · Field renamed from the wrong `invoice_number` to the
    canonical `invoice_no`, and each story-fixture number is now
    prefixed with `INV/2026/S-…` (accepted by the Pydantic invoice_no
    pattern). Counter is synced after insert so a subsequent real user
    invoice cannot re-collide with a story-fixture number.
    """
    docs = []
    # Diagnostic invoices — flat ₹1500 for PTA+Impedance combo
    for p in PATIENTS[:2]:
        docs.append({
            "invoice_id": f"INV-STORY-DIAG-{p['patient_id']}",
            "invoice_no": f"INV/2026/S{p['patient_id'][-2:]}",
            "clinic_id": CLINIC_ID, "branch_id": BRANCH_ID,
            "patient_id": p["patient_id"], "patient_name": p["name"],
            "items": [
                {"description": "Pure Tone Audiometry (PTA)", "hsn": "998512", "qty": 1, "rate": 750, "gst_pct": 0, "amount": 750},
                {"description": "Impedance Audiometry (Tympanometry)", "hsn": "998512", "qty": 1, "rate": 750, "gst_pct": 0, "amount": 750},
            ],
            "subtotal": 1500, "tax_amount": 0, "total": 1500, "paid": 1500, "due": 0,
            "status": "paid", "payment_mode": "UPI",
            "referring_doctor_id": DR_AK_ID,
            "referral_payout_amount": 500,  # Dr. AK's flat cut
            "date": iso(NOW - timedelta(days=3)),
            "created_at": NOW_ISO,
        })
    # HA sale invoice — Story 2.c (Meera, from stock, full ₹1,30,000)
    docs.append({
        "invoice_id": "INV-STORY-HA-02C",
        "invoice_no": "INV/2026/S02C0",
        "clinic_id": CLINIC_ID, "branch_id": BRANCH_ID,
        "patient_id": "PT-STORY-02C", "patient_name": "Meera Rao",
        "items": [
            {"description": "Phonak Audeo Lumity 30 RIC — Pair (Serial: PHO-L30-2026001 · PHO-L30-2026002)",
             "hsn": "902140", "qty": 1, "rate": 110169, "gst_pct": 18, "amount": 130000},
        ],
        "subtotal": 110169, "tax_amount": 19831, "total": 130000, "paid": 130000, "due": 0,
        "status": "paid", "payment_mode": "Card",
        "referring_doctor_id": DR_AK_ID,
        "referral_payout_amount": 6500,  # 5% of 1,30,000
        "date": iso(NOW - timedelta(days=1)),
        "created_at": NOW_ISO,
    })
    # HA advance invoice — Story 2.c.1 (Ravi, out of stock, ₹10,000 advance)
    docs.append({
        "invoice_id": "INV-STORY-HA-02C1",
        "invoice_no": "INV/2026/S02C1",
        "clinic_id": CLINIC_ID, "branch_id": BRANCH_ID,
        "patient_id": "PT-STORY-02C1", "patient_name": "Ravi Kumar",
        "items": [
            {"description": "Advance — Phonak Audeo Lumity 30 RIC (Pair) — Awaiting stock",
             "hsn": "902140", "qty": 1, "rate": 10000, "gst_pct": 0, "amount": 10000},
        ],
        "subtotal": 10000, "tax_amount": 0, "total": 130000, "paid": 10000, "due": 120000,
        "status": "partial", "payment_mode": "UPI",
        "referring_doctor_id": DR_AK_ID,
        "referral_payout_amount": 0,  # Paid on final settlement
        "notes": "Advance ₹10,000 · Balance ₹1,20,000 due on device receipt · PO placed with Phonak",
        "date": iso(NOW),
        "created_at": NOW_ISO,
    })
    await db.invoices.insert_many(docs)
    # NAV-008 · Story-demo uses the `S<...>` invoice_no namespace which
    # never overlaps with the atomic counter's zero-padded decimals, so
    # the counter does NOT need to be advanced — leaving it untouched
    # avoids inflating the seq for a real user's next canonical number.
    print(f"  ✓ {len(docs)} invoices (2 diagnostic + 1 HA sale + 1 HA advance)")


async def seed_ha_journey(db):
    """Trials, quotes, sale record, purchase order — one per branch."""
    # 2.a — In-clinic trial (Sneha)
    await db.ha_trials.insert_one({
        "trial_id": "TR-STORY-02A",
        "trial_no": "TR-2026-S02A",
        "clinic_id": CLINIC_ID, "patient_id": "PT-STORY-02A",
        "patient_name": "Sneha Bhat",
        "model": "Signia Pure Charge&Go 7AX (Demo)",
        "type": "RIC", "kind": "in_clinic",
        "start_date": iso(NOW - timedelta(hours=2)),
        "outcome": "positive_feedback",
        "notes": "Verbal SRT improved from 45→30 dB in aided condition. Very clear preference for RIC. Patient wants price quote.",
        "created_at": NOW_ISO,
    })
    await db.ha_quotes.insert_one({
        "quote_id": "QT-STORY-02A",
        "clinic_id": CLINIC_ID, "patient_id": "PT-STORY-02A",
        "patient_name": "Sneha Bhat",
        "items": [
            {"description": "Signia Pure Charge&Go 7AX — Pair", "qty": 1, "rate": 145000, "gst_pct": 18, "amount": 171100},
        ],
        "total": 171100, "status": "sent",
        "lead_flagged_at": NOW_ISO,
        "notes": "Potential HA lead — patient responded well to trial. Follow up in 3 days.",
        "date": iso(NOW),
        "created_at": NOW_ISO,
    })

    # 2.b — Home trial (Karthik) with caution deposit
    await db.ha_trials.insert_one({
        "trial_id": "TR-STORY-02B",
        "trial_no": "TR-2026-S02B",
        "clinic_id": CLINIC_ID, "patient_id": "PT-STORY-02B",
        "patient_name": "Karthik Iyer",
        "model": "Phonak Audeo Lumity 30 (Demo)",
        "type": "RIC", "kind": "home",
        "start_date": iso(NOW - timedelta(days=1)),
        "planned_return_date": iso(NOW + timedelta(days=6)),
        "caution_deposit": 15000,
        "outcome": "pending",
        "notes": "Home trial for 7 days. Caution deposit ₹15,000 collected (Card). Patient contacted via WhatsApp on days 2/4/6.",
        "created_at": NOW_ISO,
    })

    # 2.c — Sale (Meera) — HA sale record with serial numbers
    await db.ha_sales.insert_one({
        "sale_id": "HAS-STORY-02C",
        "sale_no": "SALE-2026-S02C",
        "clinic_id": CLINIC_ID, "branch_id": BRANCH_ID,
        "patient_id": "PT-STORY-02C", "patient_name": "Meera Rao",
        "model": "Phonak Audeo Lumity 30", "type": "RIC",
        "manufacturer": "Phonak",
        "serial_numbers": ["PHO-L30-2026001", "PHO-L30-2026002"],
        "amount": 130000, "gst_amount": 19831,
        "invoice_id": "INV-STORY-HA-02C",
        "status": "delivered",
        "fitting_date": iso(NOW),
        "warranty_end": iso(NOW + timedelta(days=730)),
        "created_at": NOW_ISO,
    })

    # 2.c.1 — Sale (Ravi) — out-of-stock → purchase order
    await db.purchase_orders.insert_one({
        "po_id": "PO-STORY-02C1",
        "po_no": "PO-2026-S02C1",
        "po_number": "PO/2026/S-02C1",
        "clinic_id": CLINIC_ID, "branch_id": BRANCH_ID,
        "vendor": "Phonak India Pvt Ltd",
        "vendor_email": "orders.in@phonak.com",
        "for_patient_id": "PT-STORY-02C1",
        "for_patient_name": "Ravi Kumar",
        "items": [
            {"description": "Phonak Audeo Lumity 30 RIC — Pair", "qty": 1, "rate": 110169, "amount": 110169},
        ],
        "total": 110169,
        "status": "placed",
        "expected_by": iso(NOW + timedelta(days=5)),
        "linked_invoice_id": "INV-STORY-HA-02C1",
        "notes": "Advance ₹10,000 collected from patient. Balance ₹1,20,000 due on device receipt.",
        "placed_at": iso(NOW),
        "created_at": NOW_ISO,
    })
    await db.ha_sales.insert_one({
        "sale_id": "HAS-STORY-02C1",
        "sale_no": "SALE-2026-S02C1",
        "clinic_id": CLINIC_ID, "branch_id": BRANCH_ID,
        "patient_id": "PT-STORY-02C1", "patient_name": "Ravi Kumar",
        "model": "Phonak Audeo Lumity 30", "type": "RIC",
        "manufacturer": "Phonak",
        "serial_numbers": [],
        "amount": 130000, "gst_amount": 19831,
        "advance_paid": 10000, "balance_due": 120000,
        "invoice_id": "INV-STORY-HA-02C1",
        "po_id": "PO-STORY-02C1",
        "status": "awaiting_stock",
        "created_at": NOW_ISO,
    })

    print("  ✓ HA journey seeded: 2 trials, 1 quote (lead), 2 sales (1 delivered + 1 awaiting stock), 1 PO to Phonak")


async def seed_referral_notifications(db):
    """Log the thank-you WhatsApp messages that (would have) fired for
    each patient at registration + at invoice close."""
    docs = []
    for p in PATIENTS:
        docs.append({
            "notification_id": f"NOTIF-STORY-REG-{p['patient_id']}",
            "clinic_id": CLINIC_ID, "referring_doctor_id": DR_AK_ID,
            "patient_id": p["patient_id"], "patient_name": p["name"],
            "channel": "whatsapp", "stream": "referral_received",
            "status": "sent",
            "sent_at": NOW_ISO,
            "message_snippet": f"Namaste Dr. Anand, thank you for referring {p['name']} to The Sound Clinic. We\u2019ll keep you posted on the outcome.",
            "provider_message_id": f"msg-{p['patient_id'][-4:].lower()}",
        })
    # Diagnostic-outcome messages for the 2 finished diagnostic stories
    for p in PATIENTS[:2]:
        docs.append({
            "notification_id": f"NOTIF-STORY-DIAG-{p['patient_id']}",
            "clinic_id": CLINIC_ID, "referring_doctor_id": DR_AK_ID,
            "patient_id": p["patient_id"], "patient_name": p["name"],
            "channel": "whatsapp", "stream": "diagnostic_completed",
            "status": "sent",
            "sent_at": NOW_ISO,
            "message_snippet": (
                "Diagnostic report ready. "
                + ("Mild Conductive HL — recommending ENT follow-up." if p["patient_id"] == "PT-STORY-01"
                   else "Moderate Sloping SNHL — HA trial planned.")
            ),
            "provider_message_id": f"msg-diag-{p['patient_id'][-4:].lower()}",
        })
    # HA-sale outcome message for Meera (2.c)
    docs.append({
        "notification_id": "NOTIF-STORY-HASALE-02C",
        "clinic_id": CLINIC_ID, "referring_doctor_id": DR_AK_ID,
        "patient_id": "PT-STORY-02C", "patient_name": "Meera Rao",
        "channel": "whatsapp", "stream": "ha_sale_completed",
        "status": "sent",
        "sent_at": NOW_ISO,
        "message_snippet": "HA fitted — Phonak Lumity 30 RIC pair. Your commission this month: ₹6,500.",
        "provider_message_id": "msg-hasale-02c",
    })
    await db.referral_notifications.insert_many(docs)
    print(f"  ✓ {len(docs)} referral-thank-you WhatsApp events logged")


async def main():
    if not os.environ.get("MONGO_URL"):
        print("ERROR: MONGO_URL not set — did dotenv load?")
        sys.exit(1)
    client = motor.motor_asyncio.AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ["DB_NAME"]]
    print("═══════════════════════════════════════════════════════════")
    print("  Seeding /demo user-case story data")
    print(f"  Clinic: {CLINIC_ID}  ·  Referring doctor: Dr. Anand Kumar")
    print("═══════════════════════════════════════════════════════════")
    await wipe(db)
    await seed_referring_doctor(db)
    await seed_patients(db)
    await seed_appointments(db)
    await seed_reports(db)
    await seed_invoices(db)
    await seed_ha_journey(db)
    await seed_referral_notifications(db)
    print("═══════════════════════════════════════════════════════════")
    print("  ✅ STORY SEED COMPLETE")
    print("═══════════════════════════════════════════════════════════")
    client.close()


if __name__ == "__main__":
    asyncio.run(main())
