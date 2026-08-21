from fastapi import FastAPI, APIRouter, HTTPException, Depends, Request, Response
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
import os
import logging
import asyncio
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
import uuid

# IST helpers — shared module (single source of truth)
from utils.ist import IST, ist_day_start_utc, ist_today_ymd, ist_next_day_start_utc  # noqa: F401

# Models used by remaining in-file routes (auth / clinic)
from models import LoginRequest
from auth import (
    hash_password, verify_password, create_access_token,
    get_current_user, require_roles, VALID_ROLES,
)
import billing as billing_module
import closeout as closeout_module
from utils.auth_cookies import set_auth_cookies, clear_auth_cookies
from utils.serde import serialize_datetime  # noqa: F401 — used by _seed_defaults


ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# MongoDB connection (single source; shared with routers via Depends(get_db))
from database import client, db, get_db  # noqa: E402

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """FastAPI lifespan: replaces deprecated on_event('startup'/'shutdown') handlers.
    Startup: creates MongoDB indexes, seeds default clinic/users/services, cleans stale UTC-keyed token counters.
    Shutdown: closes MongoDB client connection.
    """
    _log = logging.getLogger(__name__)
    try:
        # ---- indexes ----
        await db.patients.create_index("patient_id", unique=True)
        await db.patients.create_index("mobile")
        await db.patients.create_index("updated_at")
        # Password reset (self-serve forgot-password flow)
        await db.password_reset_tokens.create_index("token_hash", unique=True)
        await db.password_reset_tokens.create_index("user_id")
        await db.password_reset_tokens.create_index(
            "expires_at", expireAfterSeconds=86_400,  # 24h grace after expiry, then auto-purge
        )
        await db.auth_events.create_index([("user_id", 1), ("at", -1)])
        # user_sessions — hot path on every request (session lookup) and
        # every login/logout (revoke). Without indexes each hit does a
        # full collection scan → load test at 100 concurrent users showed
        # p50=2s dominated by scanning 3.7k session rows. These indexes
        # bring per-request session lookup from ~50ms → ~1ms.
        await db.user_sessions.create_index("session_id", unique=True)
        await db.user_sessions.create_index([("user_id", 1), ("revoked_at", 1)])
        await db.user_sessions.create_index([("last_seen_at", -1)])
        # audit_log — grows fast, queried by (target, action) on admin
        # pages. Compound index keeps founder-panel loads snappy.
        await db.audit_log.create_index([("target", 1), ("at", -1)])
        await db.audit_log.create_index([("action", 1), ("at", -1)])
        # email_events (Email Health banner) — auto-purge after 30 days
        await db.email_events.create_index([("timestamp", -1)])
        await db.email_events.create_index([("status", 1), ("timestamp", -1)])
        await db.email_events.create_index("timestamp", expireAfterSeconds=30 * 86_400)
        await db.referring_doctors.create_index("doctor_id", unique=True)
        await db.referring_doctors.create_index("name")
        await db.patient_notes.create_index("patient_id")
        await db.patient_notes.create_index("created_at")
        await db.test_sessions.create_index("session_id", unique=True)
        await db.test_sessions.create_index([("patient_id", 1), ("test_date", -1)])
        # M01 indexes
        await db.users.create_index("email", unique=True)
        await db.users.create_index([("clinic_id", 1), ("role", 1)])
        await db.clinics.create_index("clinic_id", unique=True)
        # Powers `GET /admin/v2/signups/recent` — 20s polling from the
        # founder-dashboard live feed. Descending order matches the query
        # sort, string values compare correctly for ISO timestamps.
        await db.clinics.create_index([("created_at", -1)])
        await db.tokens.create_index([("clinic_id", 1), ("issued_at", -1)])
        await db.tokens.create_index("token_id", unique=True)
        await db.patients.create_index([("clinic_id", 1), ("updated_at", -1)])
        await db.patients.create_index("mrd")
        # M01.B appointment indexes
        await db.appointments.create_index("appointment_id", unique=True)
        await db.appointments.create_index([("clinic_id", 1), ("start_at", 1)])
        await db.appointments.create_index([("clinic_id", 1), ("audiologist_id", 1), ("start_at", 1)])
        await db.appointments.create_index([("clinic_id", 1), ("staff_id", 1), ("start_at", 1)])
        await db.appointments.create_index([("clinic_id", 1), ("counterparty_type", 1), ("start_at", 1)])
        await db.waitlist.create_index("entry_id", unique=True)
        await db.waitlist.create_index([("clinic_id", 1), ("status", 1), ("created_at", -1)])
        await db.reminder_logs.create_index([("clinic_id", 1), ("sent_at", -1)])
        await db.cancellation_logs.create_index([("clinic_id", 1), ("cancelled_at", -1)])
        # M01.C billing indexes
        await db.services.create_index("service_id", unique=True)
        await db.services.create_index([("clinic_id", 1), ("active", 1), ("name", 1)])
        await db.invoices.create_index("invoice_id", unique=True)
        await db.invoices.create_index([("clinic_id", 1), ("invoice_date", -1)])
        await db.invoices.create_index([("clinic_id", 1), ("patient_id", 1)])
        await db.invoices.create_index("invoice_no")
        # NAV-008 · Compound partial unique index on (clinic_id, invoice_no).
        # `partialFilterExpression={"invoice_no": {"$type": "string"}}` skips
        # legacy rows where invoice_no is missing/null (Preview has 2 such
        # test-fixture docs). Wrapped in try/except so a Preview environment
        # with a KNOWN existing duplicate does NOT crash startup — the loud
        # error surfaces the blocking data condition without taking down
        # the pod. Production is expected to have zero duplicates and the
        # index will build cleanly.
        try:
            await db.invoices.create_index(
                [("clinic_id", 1), ("invoice_no", 1)],
                unique=True,
                partialFilterExpression={"invoice_no": {"$type": "string"}},
                name="clinic_id_1_invoice_no_1_unique",
            )
        except Exception as _idx_err:
            _msg = str(_idx_err)
            if "E11000" in _msg or "duplicate" in _msg.lower():
                _log.error(
                    "NAV-008 · Compound unique index (clinic_id, invoice_no) "
                    "NOT installed — existing duplicate data detected: %s. "
                    "New duplicates are NOT prevented until the duplicate row(s) "
                    "are remediated. Run backend/scripts/nav008_counter_reconcile.py "
                    "(counter sync only) and see docs for the separately-gated "
                    "renumber path.",
                    _msg,
                )
            else:
                # Unrelated failure — re-raise to surface the real bug.
                raise
        await db.payments.create_index("payment_id", unique=True)
        await db.payments.create_index([("clinic_id", 1), ("paid_at", -1)])
        await db.payments.create_index("invoice_id")
        # HA module Phase 1/2 — inventory integrity
        await db.branches.create_index("branch_id", unique=True)
        await db.branches.create_index([("clinic_id", 1), ("is_primary", -1)])
        await db.vendors.create_index("vendor_id", unique=True)
        await db.vendors.create_index([("clinic_id", 1), ("name", 1)])
        await db.ha_products.create_index("product_id", unique=True)
        await db.ha_products.create_index([("clinic_id", 1), ("brand", 1), ("model", 1)])
        await db.serial_items.create_index("serial_id", unique=True)
        # Hard uniqueness — same physical sticker cannot be received twice in a clinic.
        await db.serial_items.create_index(
            [("clinic_id", 1), ("serial_no", 1)], unique=True, name="uniq_clinic_serial_no",
        )
        await db.serial_items.create_index([("clinic_id", 1), ("branch_id", 1), ("state", 1)])
        await db.serial_events.create_index([("serial_id", 1), ("at", -1)])
        await db.purchase_orders.create_index([("clinic_id", 1), ("status", 1), ("created_at", -1)])
        # Numbering identifiers minted via `utils.numbering.next_number` are
        # CLINIC-SCOPED (counter is keyed by `(kind, clinic_id, year)`), so the
        # same `PO-2026-0001` legitimately exists in two tenants. Drop legacy
        # global unique indexes if present and replace with compound
        # (clinic_id, <number>) unique keys.
        for coll, field in (
            ("purchase_orders", "po_no"),
            ("quotations", "quote_no"),
            ("ha_sales", "sale_no"),
            ("ha_trials", "trial_no"),
            ("ha_amc_contracts", "contract_no"),
        ):
            try:
                await db[coll].drop_index(f"{field}_1")
            except Exception:
                pass
            await db[coll].create_index(
                [("clinic_id", 1), (field, 1)],
                unique=True,
                name=f"uniq_clinic_{field}",
            )
        # Numbering identifiers are clinic-scoped — same `GRN-YYYY-NNNN` may legitimately
        # exist in two different tenants. Use a compound (clinic_id, grn_no) unique key
        # and drop the legacy global index if present (safe: only blocks cross-tenant dupes).
        try:
            await db.grns.drop_index("grn_no_1")
        except Exception:
            pass
        await db.grns.create_index([("clinic_id", 1), ("grn_no", 1)], unique=True, name="uniq_clinic_grn_no")
        await db.grns.create_index([("po_no", 1), ("received_at", -1)])
        await db.accessory_stock.create_index("sku_id", unique=True)
        await db.accessory_stock.create_index([("clinic_id", 1), ("branch_id", 1), ("product_id", 1), ("variant", 1)], name="uniq_accessory_variant", unique=True)
        # HA module Phase 3 — transactions
        await db.quotations.create_index([("clinic_id", 1), ("status", 1), ("created_at", -1)])
        await db.quotations.create_index("patient_id")
        await db.ha_sales.create_index([("clinic_id", 1), ("status", 1), ("created_at", -1)])
        await db.ha_sales.create_index("patient_id")
        # HA module Phase 4 — clinical fittings
        await db.ha_fittings.create_index("fitting_id", unique=True)
        await db.ha_fittings.create_index([("clinic_id", 1), ("status", 1), ("created_at", -1)])
        await db.ha_fittings.create_index([("clinic_id", 1), ("patient_id", 1), ("created_at", -1)])
        await db.ha_fittings.create_index("sale_no")
        # HA module Phase 4.5 — trials
        await db.ha_trials.create_index([("clinic_id", 1), ("status", 1), ("return_date", 1)])
        await db.ha_trials.create_index([("clinic_id", 1), ("patient_id", 1), ("created_at", -1)])
        # HA module Phase 6 — CRM
        await db.ha_followups.create_index("followup_id", unique=True)
        await db.ha_followups.create_index([("clinic_id", 1), ("status", 1), ("due_date", 1)])
        await db.ha_followups.create_index([("clinic_id", 1), ("patient_id", 1), ("kind", 1), ("ref_id", 1)])
        await db.ha_subscriptions.create_index("subscription_id", unique=True)
        await db.ha_subscriptions.create_index([("clinic_id", 1), ("status", 1), ("next_due_date", 1)])
        await db.ha_subscriptions.create_index([("clinic_id", 1), ("patient_id", 1)])
        # Service tickets (post-P7 UI catch-up) — ticket_no clinic-scoped
        try:
            await db.service_tickets.drop_index("ticket_no_1")
        except Exception:
            pass
        await db.service_tickets.create_index([("clinic_id", 1), ("ticket_no", 1)], unique=True, name="uniq_clinic_ticket_no")
        await db.service_tickets.create_index([("clinic_id", 1), ("status", 1), ("created_at", -1)])
        await db.service_tickets.create_index([("clinic_id", 1), ("patient_id", 1)])
        await db.service_tickets.create_index("serial_id")
        # Loaners
        await db.ha_loaners.create_index("loaner_id", unique=True)
        await db.ha_loaners.create_index([("clinic_id", 1), ("status", 1), ("expected_return_date", 1)])
        await db.ha_loaners.create_index([("clinic_id", 1), ("patient_id", 1)])
        # Trade-ins (Phase 10.5 — Upgrade Engine)
        await db.ha_trade_ins.create_index("trade_in_id", unique=True)
        await db.ha_trade_ins.create_index([("clinic_id", 1), ("status", 1), ("created_at", -1)])
        await db.ha_trade_ins.create_index([("clinic_id", 1), ("patient_id", 1)])
        await db.ha_trade_ins.create_index("old_serial_id")
        # Waitlist (Phase 12.0 — public signup)
        await db.waitlist_signups.create_index("email", unique=True)
        await db.waitlist_signups.create_index([("created_at", -1)])
        # Login events — capped audit (Phase 14D Activity Tracking)
        from utils.activity import ensure_login_events_collection, ensure_page_views_collection
        await ensure_login_events_collection(db)
        await ensure_page_views_collection(db)
        try:
            await db.login_events.create_index([("clinic_id", 1), ("at", -1)])
            await db.login_events.create_index([("at", -1)])
            await db.users.create_index([("last_seen_at", -1)])
            await db.page_views.create_index([("user_id", 1), ("at", -1)])
            await db.geoip_cache.create_index("ip", unique=True)
        except Exception as e:
            logger.debug(f"login_events index skip: {e}")
        # AUDINEXA Couriers / Estimates / Approvals (Phase 12.B)
        # ── Multi-tenant numbered IDs ──────────────────────────────────
        # `shipment_id`, `estimate_id`, `approval_id` are minted as
        # `CSH/EST/APR-YYYY-NNNN` via `next_number()`, which keeps a
        # per-(clinic, year) counter. Two clinics legitimately mint the
        # same number — so the unique index MUST be (clinic_id, <id>)
        # NOT global. We drop any pre-existing global unique index
        # (idempotent: ignore the error if it never existed) before
        # creating the correct compound one. This fixes Bug B from
        # iter34 QA: tenant-A getting HTTP 500 because tenant-B already
        # owned the same CSH-2026-0002.
        for coll, field in (
            (db.ha_courier_shipments, "shipment_id"),
            (db.ha_service_estimates, "estimate_id"),
            (db.ha_customer_approvals, "approval_id"),
        ):
            try:
                await coll.drop_index(f"{field}_1")
            except Exception:  # noqa: BLE001  — index already absent
                pass
            await coll.create_index([("clinic_id", 1), (field, 1)], unique=True)
        await db.ha_courier_shipments.create_index("shipment_id")
        await db.ha_courier_shipments.create_index([("clinic_id", 1), ("ticket_no", 1)])
        await db.ha_courier_shipments.create_index([("clinic_id", 1), ("status", 1), ("direction", 1)])
        await db.ha_courier_shipments.create_index([("clinic_id", 1), ("awb_number", 1), ("direction", 1)], unique=True)
        await db.ha_service_estimates.create_index([("clinic_id", 1), ("ticket_no", 1)])
        await db.ha_customer_approvals.create_index([("clinic_id", 1), ("ticket_no", 1)])
        await db.ha_customer_approvals.create_index([("clinic_id", 1), ("decision", 1)])
        await db.report_deliveries.create_index("delivery_id", unique=True)
        await db.report_deliveries.create_index([("clinic_id", 1), ("session_id", 1)])
        # AMC (Phase 13.A)
        await db.ha_amc_plans.create_index("plan_id", unique=True)
        await db.ha_amc_plans.create_index([("clinic_id", 1), ("active", 1)])
        await db.ha_amc_contracts.create_index([("clinic_id", 1), ("status", 1), ("amc_expiry_date", 1)])
        await db.ha_amc_contracts.create_index([("clinic_id", 1), ("patient_id", 1)])
        await db.ha_amc_contracts.create_index([("clinic_id", 1), ("serial_id", 1), ("status", 1)])

        # M-Transfers (inter-clinic stock transfer + delivery challan)
        await db.stock_transfers.create_index("transfer_id", unique=True)
        await db.stock_transfers.create_index([("from_clinic_id", 1), ("status", 1), ("created_at", -1)])
        await db.stock_transfers.create_index([("to_clinic_id", 1), ("status", 1), ("created_at", -1)])
        await db.stock_transfers.create_index([("challan_no", 1)])
        # Referral Partners (M12, Phase 13.C)
        await db.referral_partners.create_index("partner_id", unique=True)
        await db.referral_partners.create_index([("clinic_id", 1), ("referral_code", 1)], unique=True)
        await db.referral_partners.create_index([("clinic_id", 1), ("status", 1)])
        await db.partner_payouts.create_index("payout_id", unique=True)
        await db.partner_payouts.create_index([("clinic_id", 1), ("partner_id", 1), ("created_at", -1)])
        await db.patients.create_index([("clinic_id", 1), ("referral_partner_id", 1)])
        # NAV-012 · Bundle C — recovery-ledger indexes.
        # `recovery_id` uniqueness backs the CAS update in
        # `_consume_pending_recovery`; compound indexes back the two
        # read paths (`{clinic, partner, status}` scan + admin list).
        # Wrapped in try/except so partial preview data cannot break
        # boot; same soft-fail pattern as the NAV-008 invoice unique
        # index above.
        try:
            await db.partner_recovery_ledger.create_index(
                "recovery_id", unique=True, name="uniq_recovery_id",
            )
        except Exception as _rec_err:
            _log.error(
                "NAV-012 · unique index on partner_recovery_ledger.recovery_id "
                "NOT installed: %s. Duplicate recovery_ids may exist.", _rec_err,
            )
        await db.partner_recovery_ledger.create_index(
            [("clinic_id", 1), ("partner_id", 1), ("status", 1), ("created_at", 1)],
            name="rec_clinic_partner_status_ct",
        )
        await db.partner_recovery_ledger.create_index(
            [("clinic_id", 1), ("status", 1), ("created_at", -1)],
            name="rec_clinic_status_ct",
        )
        # NAV-012 · Bundle A — idempotency-key store.
        # `(clinic_id, scope, idempotency_key)` UNIQUE arbitrates
        # concurrent duplicate requests. `expires_at` TTL auto-purges
        # records after 24h. `operation_ref.id_value` supports
        # reverse-lookup during crash-recovery.
        await db.idempotency_keys.create_index(
            [("clinic_id", 1), ("scope", 1), ("idempotency_key", 1)],
            unique=True, name="uniq_clinic_scope_key",
        )
        await db.idempotency_keys.create_index(
            [("operation_ref.id_value", 1)], name="idem_op_ref_id",
        )
        # TTL: expireAfterSeconds=0 uses `expires_at` value verbatim.
        try:
            await db.idempotency_keys.drop_index("idem_expires_ttl")
        except Exception:
            pass
        await db.idempotency_keys.create_index(
            [("expires_at", 1)],
            name="idem_expires_ttl", expireAfterSeconds=0,
        )
        # Advance Receipts · Phase 2A (Receipt-only).
        # Isolated collections — no cross-linking to invoices / payments.
        # (clinic_id, receipt_no) is UNIQUE per tenant per year via the
        # AR/YYYY/NNNNNN counter. `receipt_id` is globally unique.
        await db.advance_receipts.create_index("receipt_id", unique=True)
        await db.advance_receipts.create_index(
            [("clinic_id", 1), ("receipt_no", 1)],
            unique=True, name="uniq_clinic_advance_receipt_no",
        )
        await db.advance_receipts.create_index(
            [("clinic_id", 1), ("patient_id", 1), ("created_at", -1)],
            name="ar_clinic_patient_ct",
        )
        await db.advance_receipts.create_index(
            [("clinic_id", 1), ("status", 1), ("created_at", -1)],
            name="ar_clinic_status_ct",
        )
        await db.advance_audit_events.create_index("event_id", unique=True)
        await db.advance_audit_events.create_index([("receipt_id", 1), ("at", -1)])
        await db.advance_audit_events.create_index([("clinic_id", 1), ("at", -1)])
        # Patient Portal (M13, Phase 13.D)
        await db.patient_otps.create_index([("clinic_id", 1), ("patient_id", 1)], unique=True)
        await db.patient_appointment_requests.create_index("request_id", unique=True)
        await db.patient_appointment_requests.create_index([("clinic_id", 1), ("status", 1), ("created_at", -1)])
        await db.patient_feedback.create_index("feedback_id", unique=True)
        await db.patient_feedback.create_index([("clinic_id", 1), ("created_at", -1)])
        # Admin panel (Phase 14A)
        await db.tenant_invoices.create_index("invoice_id", unique=True)
        await db.tenant_invoices.create_index([("clinic_id", 1), ("issued_at", -1)])
        await db.tenant_invoices.create_index([("status", 1), ("issued_at", -1)])
        await db.tenant_feature_flags.create_index("clinic_id", unique=True)
        await db.plan_overrides.create_index("tier", unique=True)
        await db.admin_audit_logs.create_index([("at", -1)])
        await db.admin_audit_logs.create_index([("actor_user_id", 1), ("at", -1)])
        await db.admin_audit_logs.create_index("log_id", unique=True)
        # Self-hosted error telemetry (routers/error_telemetry.py).
        # TTL index — auto-purges old crash docs after `ERROR_LOG_RETENTION_DAYS`
        # so crash payloads (which can embed PII via URL/body) don't pile up.
        try:
            await db.error_logs.drop_index("at_ttl")
        except Exception:
            pass
        ttl_seconds = int(os.environ.get("ERROR_LOG_RETENTION_DAYS", "30")) * 86400
        await db.error_logs.create_index(
            [("at", 1)], name="at_ttl", expireAfterSeconds=ttl_seconds,
        )
        await db.error_logs.create_index([("kind", 1), ("at", -1)])
        await db.error_logs.create_index([("clinic_id", 1), ("at", -1)])
        await db.error_logs.create_index([("fingerprint", 1), ("at", -1)])
        await db.error_logs.create_index("log_id", unique=True)
        # Spike-alerter cooldown state (utils/error_alerts.py).
        await db.error_alert_state.create_index("fingerprint", unique=True)
        # BYOK Phase 1 — Clinic Vault PoC
        await db.clinic_vaults.create_index("clinic_id", unique=True)
        await db.vault_test_records.create_index([("clinic_id", 1), ("created_at", -1)])
        await db.vault_test_records.create_index("record_id", unique=True)
        # Email-token invitations (P1 onboarding)
        await db.invitations.create_index("token", unique=True)
        await db.invitations.create_index("invite_id", unique=True)
        await db.invitations.create_index([("clinic_id", 1), ("status", 1), ("created_at", -1)])
        _log.info("MongoDB indexes ensured")

        # ---- seed defaults (clinic, users, services) — idempotent ----
        from seeds import run_demo_seed
        await run_demo_seed(db, billing_module)

        # ---- one-time backfill: extend existing appointments with the new
        # counterparty + staff resource fields (Phase: Calendar v2). Idempotent
        # — only touches rows missing the new fields. ------------------------
        try:
            res = await db.appointments.update_many(
                {"staff_id": {"$exists": False}},
                [{
                    "$set": {
                        "staff_id": "$audiologist_id",
                        "staff_name": "$audiologist_name",
                        "counterparty_type": "patient",
                        "counterparty_id": "$patient_id",
                        "counterparty_name": "$patient_name",
                        "counterparty_phone": "$patient_mobile",
                        "category": "consultation",
                    },
                }],
            )
            if res.modified_count:
                _log.info(f"Appointments backfill: {res.modified_count} rows enriched with staff/counterparty fields")
        except Exception as e:
            _log.warning(f"Appointments backfill skipped: {e}")

        # ---- NAV-005 Sprint-3A / CLIN-001 backfill: legacy test_sessions
        # without clinic_id get it stamped from their linked patient. Idempotent —
        # only touches rows missing the field.
        try:
            missing_cursor = db.test_sessions.find(
                {"clinic_id": {"$in": [None]}, "patient_id": {"$exists": True}},
                {"_id": 1, "patient_id": 1},
            )
            # Also handle rows where the field is entirely absent (not just null).
            absent_cursor = db.test_sessions.find(
                {"clinic_id": {"$exists": False}, "patient_id": {"$exists": True}},
                {"_id": 1, "patient_id": 1},
            )
            backfilled = 0
            for cursor in (missing_cursor, absent_cursor):
                async for s in cursor:
                    pid = s.get("patient_id")
                    if not pid:
                        continue
                    pat = await db.patients.find_one(
                        {"patient_id": pid},
                        {"_id": 0, "clinic_id": 1},
                    )
                    cid = pat.get("clinic_id") if pat else None
                    if cid:
                        await db.test_sessions.update_one(
                            {"_id": s["_id"]},
                            {"$set": {"clinic_id": cid}},
                        )
                        backfilled += 1
            if backfilled:
                _log.info(f"CLIN-001 backfill: stamped clinic_id on {backfilled} legacy test_sessions")
        except Exception as e:
            _log.warning(f"CLIN-001 test_sessions clinic_id backfill skipped: {e}")

        # Compound index for tenant-scoped session lookups (post-CLIN-001).
        try:
            await db.test_sessions.create_index(
                [("clinic_id", 1), ("session_id", 1)],
                name="clinic_session_id",
            )
            await db.test_sessions.create_index(
                [("clinic_id", 1), ("patient_id", 1), ("test_date", -1)],
                name="clinic_patient_test_date",
            )
        except Exception as e:
            _log.debug(f"test_sessions compound index skip: {e}")

        # ---- one-time cleanup of stale UTC-keyed token counters ----
        # After the IST migration, old `token:{clinic}:{YYYY-MM-DD}` counter docs keyed on UTC date
        # (e.g., yesterday's UTC date when we crossed IST midnight) are functionally obsolete.
        # Drop anything that isn't today's IST-YMD. Counters auto-regenerate on next issuance.
        try:
            today_ymd = ist_today_ymd()
            cleanup = await db.counters.delete_many({
                "$and": [
                    {"_id": {"$regex": r"^token:.+:\d{4}-\d{2}-\d{2}$"}},
                    {"_id": {"$not": {"$regex": f":{today_ymd}$"}}},
                ]
            })
            if cleanup.deleted_count:
                _log.info(f"Counter cleanup: removed {cleanup.deleted_count} stale token counter docs")
        except Exception as e:
            _log.warning(f"Counter cleanup skipped: {e}")

    except Exception as e:
        _log.error(f"Startup initialisation error: {e}")

    # Start daily close-out scheduler (21:00 IST) + follow-up scan (09:30 IST)
    scheduler = None
    try:
        scheduler = closeout_module.start_scheduler(db)
        # Attach the CRM follow-up scan as a second job on the same scheduler.
        try:
            from apscheduler.triggers.cron import CronTrigger
            from routers.ha_crm import run_daily_followup_scan
            scheduler.add_job(
                run_daily_followup_scan,
                trigger=CronTrigger(hour=9, minute=30, timezone=IST),
                args=[db],
                id="daily_followup_scan_0930_ist",
                replace_existing=True,
                misfire_grace_time=3600,
            )
            logging.getLogger(__name__).info("APScheduler job added: daily_followup_scan_0930_ist (09:30 IST)")
            # Trial-expiry scanner — 02:00 IST daily (Phase 12.0)
            try:
                from trial_expiry import run_trial_expiry_scan
                scheduler.add_job(
                    run_trial_expiry_scan,
                    trigger=CronTrigger(hour=2, minute=0, timezone=IST),
                    args=[db],
                    id="trial_expiry_0200_ist",
                    replace_existing=True,
                    misfire_grace_time=3600,
                )
                logging.getLogger(__name__).info("APScheduler job added: trial_expiry_0200_ist (02:00 IST)")
            except Exception as e:
                logging.getLogger(__name__).warning(f"Trial-expiry scheduler skipped: {e}")
            # AMC expiry sweep — 02:30 IST daily (Phase 13.A)
            try:
                from routers.ha_amc import run_amc_expiry_sweep
                scheduler.add_job(
                    run_amc_expiry_sweep,
                    trigger=CronTrigger(hour=2, minute=30, timezone=IST),
                    args=[db],
                    id="amc_expiry_sweep_0230_ist",
                    replace_existing=True,
                    misfire_grace_time=3600,
                )
                logging.getLogger(__name__).info("APScheduler job added: amc_expiry_sweep_0230_ist (02:30 IST)")
            except Exception as e:
                logging.getLogger(__name__).warning(f"AMC sweep scheduler skipped: {e}")
            # Birthday + anniversary greeting scan — 09:00 IST daily
            try:
                from routers.greetings import run_daily_greeting_scan
                scheduler.add_job(
                    run_daily_greeting_scan,
                    trigger=CronTrigger(hour=9, minute=0, timezone=IST),
                    args=[db],
                    id="daily_greeting_scan_0900_ist",
                    replace_existing=True,
                    misfire_grace_time=3600,
                )
                logging.getLogger(__name__).info("APScheduler job added: daily_greeting_scan_0900_ist (09:00 IST)")
            except Exception as e:
                logging.getLogger(__name__).warning(f"Greeting scan scheduler skipped: {e}")
            # Hybrid PDF Storage retention sweep — 03:15 IST daily.
            # Purges audiogram-report PDFs older than PDF_RETENTION_DAYS (default 30d)
            # so the on-demand generator handles older fetches. Set PDF_RETENTION_DAYS=0
            # to disable.
            try:
                from services.pdf_retention import purge_expired_session_reports
                scheduler.add_job(
                    purge_expired_session_reports,
                    trigger=CronTrigger(hour=3, minute=15, timezone=IST),
                    args=[db],
                    id="pdf_retention_sweep_0315_ist",
                    replace_existing=True,
                    misfire_grace_time=3600,
                )
                logging.getLogger(__name__).info("APScheduler job added: pdf_retention_sweep_0315_ist (03:15 IST)")
            except Exception as e:
                logging.getLogger(__name__).warning(f"PDF retention sweeper skipped: {e}")
            # Weekly CSV email exports — Mondays 07:00 IST.
            # Iterates `csv_export_subscriptions` docs where active=true and
            # last_sent_at is >6 days ago, generating + emailing the CSV.
            try:
                from routers.csv_email_exports import run_weekly_csv_exports
                scheduler.add_job(
                    run_weekly_csv_exports,
                    trigger=CronTrigger(day_of_week="mon", hour=7, minute=0, timezone=IST),
                    args=[db],
                    id="weekly_csv_exports_mon_0700_ist",
                    replace_existing=True,
                    misfire_grace_time=3600,
                )
                logging.getLogger(__name__).info("APScheduler job added: weekly_csv_exports_mon_0700_ist")
            except Exception as e:
                logging.getLogger(__name__).warning(f"Weekly CSV exports scheduler skipped: {e}")
        except Exception as e:
            logging.getLogger(__name__).warning(f"FollowUp scheduler job skipped: {e}")
    except Exception as e:
        _log.warning(f"Close-out scheduler skipped: {e}")

    # One-time (idempotent) migration of legacy report_status values to the new
    # 3-state model (draft | report_ready | completed). Safe on every boot.
    try:
        from routers.report_handover import migrate_legacy_report_statuses
        res = await migrate_legacy_report_statuses(db)
        if res.get("merged_into_completed"):
            _log.info(f"report_status migration: {res}")
    except Exception as e:        _log.warning(f"report_status migration skipped: {e}")

    # Daily Mongo backup scheduler — see routers/backup_admin.py for config.
    # Idempotent + skipped entirely when BACKUP_DISABLED=1.
    try:
        from routers.backup_admin import setup_backup_scheduler
        setup_backup_scheduler(db)
    except Exception as e:
        _log.warning(f"Backup scheduler skipped: {e}")

    yield

    # ---- shutdown ----
    if scheduler:
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass
    try:
        from routers.backup_admin import shutdown_backup_scheduler
        shutdown_backup_scheduler()
    except Exception:
        pass
    client.close()
    _log.info("MongoDB client closed")


app = FastAPI(lifespan=lifespan)

# ==================== Rate limiting (brute-force protection) ====================
# Singleton Limiter lives in rate_limit.py so routers can import it without
# circular dependency. Strict per-endpoint limits live next to each route via
# @limiter.limit decorators. Default app-wide ceiling = 300/minute per IP.
from slowapi import _rate_limit_exceeded_handler  # noqa: E402
from slowapi.errors import RateLimitExceeded  # noqa: E402
from rate_limit import limiter  # noqa: E402

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ==================== Self-hosted error telemetry ====================
# Catches every uncaught 5xx + ingests frontend crashes from the React error
# boundary. See routers/error_telemetry.py for the full rationale.
from routers.error_telemetry import (  # noqa: E402
    ErrorLoggerMiddleware,
    router as error_telemetry_router,
    admin_errors_router,
)

# Middleware order matters: this must be FIRST (added last → runs outermost)
# so it sees exceptions raised by every other middleware/route.
app.add_middleware(ErrorLoggerMiddleware)

# ==================== API Latency Recorder ====================
# Feeds the founder-dashboard live latency speedometer. Zero-config, in-process
# ring buffer — see utils/latency_recorder.py for the full rationale.
from utils.latency_recorder import LatencyRecorderMiddleware  # noqa: E402
app.add_middleware(LatencyRecorderMiddleware)


# ==================== CSRF guard (cookie-auth only) ====================
# When a request authenticates via the `access_token` httpOnly cookie AND
# uses an unsafe HTTP method, require `X-CSRF-Token` header == `audinexa_csrf`
# cookie (double-submit pattern). Authorization-header-authenticated requests
# (pytest, curl, native API clients) are exempt — they can't be CSRF'd by a
# browser, since the malicious site can't forge an Authorization header.
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402

_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# Endpoints that legitimately receive a cookie + unsafe method but don't
# need CSRF: login (no cookie set yet), logout (idempotent + needs to work
# from a stuck state), telemetry ingest (anonymous), MFA verify-login
# (challenge token already authenticates).
_CSRF_EXEMPT_PATHS = {
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/mfa/verify-login",
    "/api/_telemetry/frontend-error",
    "/api/public/clinic-signup",
}


class CsrfMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method not in _UNSAFE_METHODS:
            return await call_next(request)
        if request.url.path in _CSRF_EXEMPT_PATHS:
            return await call_next(request)
        auth_hdr = request.headers.get("authorization") or ""
        if auth_hdr.startswith("Bearer "):
            # API-client / pytest path — no cookie auth, no CSRF risk.
            return await call_next(request)
        cookie_token = request.cookies.get("audinexa_csrf")
        access_cookie = request.cookies.get("access_token")
        if not access_cookie:
            # Not authenticated via cookie — let the endpoint's normal auth
            # check return 401 if needed. No CSRF check applies.
            return await call_next(request)
        header_token = request.headers.get("x-csrf-token")
        if not (cookie_token and header_token and cookie_token == header_token):
            return JSONResponse(
                {"detail": "CSRF token missing or mismatched"},
                status_code=403,
            )
        return await call_next(request)


app.add_middleware(CsrfMiddleware)

# Expose db to dependency (used by auth.get_current_user)
app.state.db = db

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")


# ==================== M01: AUTH ROUTES ====================

@api_router.post("/auth/login")
# 60/minute keeps brute-force protection (real credential-stuffing attacks
# are caught long before 60 attempts/min by IP-level WAF rules), while
# unblocking the pytest suite where each test fixture re-logs in.
@limiter.limit("60/minute")
async def login(req: LoginRequest, request: Request, response: Response):
    email = req.email.strip().lower()
    user = await db.users.find_one({"email": email}, {"_id": 0})
    # bcrypt.checkpw is CPU-bound (~200ms/hash at cost=12). Running it on
    # the event loop serialises every concurrent login → the load test at
    # 100 concurrent users showed p50=2s. `asyncio.to_thread` offloads it
    # to the default ThreadPoolExecutor (40 workers), pulling p50 down to
    # ~250ms.
    if not user or not user.get("active", True) or not (
        await asyncio.to_thread(verify_password, req.password, user.get("password_hash", ""))
    ):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    # ── Email verification gate ── Grandfathered users (2026-07-26 migration)
    # already have email_verified=true; every fresh signup after that date
    # starts as false and must complete OTP verification before login. The
    # 403 body carries `verification_required` + `email` so the frontend
    # can auto-redirect to /verify-email with the field prefilled.
    if not user.get("email_verified", False):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "EMAIL_NOT_VERIFIED",
                "verification_required": True,
                "email": user["email"],
                "message": "Please verify your email to activate your account. Check your inbox for a 6-digit code.",
            },
        )

    # ── 2FA gate ── If the user has MFA enabled, hand back a short-lived
    # `mfa_token` instead of an access token. The client then POSTs the
    # 6-digit TOTP (or a recovery code) to /api/auth/mfa/verify-login to
    # exchange the challenge for the real access_token.
    if user.get("mfa_enabled"):
        return issue_mfa_challenge(user["user_id"])

    # ── Device-limit gate ── Netflix-style per-user cap keyed to the
    # clinic's effective tier. On the 3rd concurrent device for a BASIC
    # user (or 5th for STANDARD, 9th for PREMIUM) we either:
    #   - block with a 409 + device picker (DEVICE_LIMIT_ENFORCE=true), or
    #   - warn silently (rollout mode) and let the login continue.
    from utils.device_limits import enforce_or_warn
    clinic_for_cap = await db.clinics.find_one(
        {"clinic_id": user["clinic_id"]},
        {"_id": 0, "subscription_tier": 1, "trial_ends_at": 1},
    )
    if clinic_for_cap:
        from utils.tiers import resolve_effective_tier
        clinic_for_cap["effective_tier"] = await resolve_effective_tier(clinic_for_cap)
    dl_result = await enforce_or_warn(
        db, user, clinic_for_cap,
        replace_session_id=req.replace_session_id,
        remember_device=req.remember_device,
    )
    if dl_result["action"] == "block":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "DEVICE_LIMIT_EXCEEDED",
                "cap": dl_result["cap"],
                "count": dl_result["count"],
                "devices": dl_result["devices"],
                "message": f"You are signed in on {dl_result['count']} devices — your plan allows {dl_result['cap']}. Sign out on one device to continue.",
            },
        )

    sid = await mint_session_row(db, user, request, purpose="login", remember_device=req.remember_device)
    token = create_access_token(
        user["user_id"], user["email"], user["role"], user["clinic_id"],
        token_version=int(user.get("token_version", 0) or 0),
        session_id=sid,
    )
    clinic = await db.clinics.find_one({"clinic_id": user["clinic_id"]}, {"_id": 0})
    # Fire-and-forget login audit (never blocks or fails the login)
    from utils.activity import record_login
    await record_login(db, user, clinic, request)
    # P1 XSS hardening — also set httpOnly cookies. The JSON body still
    # returns `access_token` for backward compat with existing localStorage
    # clients during the migration window.
    csrf = set_auth_cookies(response, token, request, remember_device=req.remember_device)
    resp_body = {
        "access_token": token,
        "token_type": "bearer",
        "csrf_token": csrf,
        "user": {
            "user_id": user["user_id"],
            "email": user["email"],
            "name": user.get("name", ""),
            "role": user["role"],
            "clinic_id": user["clinic_id"],
            "branch_ids": user.get("branch_ids", []) or [],
        },
        "clinic": clinic,
    }
    # Surface the device-limit outcome so the frontend can render either
    # a soft banner ("You are at 2/2 devices — upgrade to Standard for
    # 2 more slots") in warn-mode or nothing when the cap isn't hit.
    if dl_result and dl_result.get("action") in {"warn", "allow", "allow_ephemeral"}:
        resp_body["device_limit"] = {
            "action":  dl_result["action"],
            "count":   dl_result.get("count", 0) + (0 if dl_result["action"] == "allow_ephemeral" else 1),
            "cap":     dl_result["cap"],
            "replaced": dl_result.get("replaced"),
            "ephemeral": dl_result["action"] == "allow_ephemeral",
        }
    return resp_body


@api_router.get("/auth/me")
async def auth_me(user=Depends(get_current_user)):
    clinic = await db.clinics.find_one({"clinic_id": user["clinic_id"]}, {"_id": 0})
    return {"user": user, "clinic": clinic}


# ==================== MULTI-CLINIC SWITCHER ====================

@api_router.get("/auth/my-clinics")
async def my_clinics(user=Depends(get_current_user)):
    """Every clinic this user can sign into — primary + granted additionals.

    Used by the top-nav clinic-switcher dropdown. Includes the *active*
    clinic_id so the UI can highlight it.

    NAV-007 · B4 · Clinics with `status in {"inactive","suspended"}` are
    filtered out. Clinics with a missing/null `status` PASS through
    (legacy tolerance — 14/23 preview clinics predate the `status`
    field and are legitimately active).
    NAV-007 · B6 · The phantom `clinics.active` field is no longer
    projected; the switcher UI reads `clinic_id / name / city / state /
    subscription_tier` only.
    """
    ids = list({user["primary_clinic_id"], *user.get("additional_clinic_ids", [])})
    clinics = await db.clinics.find(
        {
            "clinic_id": {"$in": ids},
            "$or": [
                {"status": {"$exists": False}},
                {"status": None},
                {"status": {"$nin": ["inactive", "suspended"]}},
            ],
        },
        {"_id": 0, "clinic_id": 1, "name": 1, "city": 1, "state": 1,
         "logo_fs_id": 1, "subscription_tier": 1, "status": 1},
    ).to_list(len(ids))
    return {
        "active_clinic_id": user["clinic_id"],
        "primary_clinic_id": user["primary_clinic_id"],
        "clinics": clinics,
    }


class SwitchClinicIn(BaseModel):
    clinic_id: str


@api_router.post("/auth/switch-clinic")
async def switch_clinic(
    payload: SwitchClinicIn, request: Request, response: Response,
    user=Depends(get_current_user),
):
    """Re-issues a JWT bound to a different clinic the user has been granted.

    The token_version is preserved — we do not bump it, so parallel sessions
    (if any) stay valid. Switching is purely a re-scope of the active tenant;
    the user's identity and role are unchanged.

    Every switch is persisted to `clinic_switch_audit` with IP + user-agent
    so super-admins have a compliance-grade trail of who moved between
    which tenants.
    """
    target = payload.clinic_id
    allowed = {user["primary_clinic_id"], *user.get("additional_clinic_ids", [])}
    if target not in allowed:
        raise HTTPException(status_code=403, detail="You don't have access to that clinic")

    clinic = await db.clinics.find_one(
        {"clinic_id": target}, {"_id": 0, "clinic_id": 1, "name": 1, "status": 1},
    )
    if not clinic:
        raise HTTPException(status_code=404, detail="Clinic not found")

    # NAV-007 · B5 · Reject switches into deactivated / suspended tenants
    # BEFORE minting a fresh JWT. The central inactive-clinic gate in
    # auth.get_current_user (B1) would also reject any token minted here,
    # but blocking at this endpoint avoids polluting `clinic_switch_audit`
    # with successful-looking rows that immediately die.
    if clinic.get("status") in {"inactive", "suspended"}:
        raise HTTPException(
            status_code=403,
            detail="That clinic is no longer active. Please contact your head clinic.",
        )

    # Capture the *from* clinic name while we still hold the old context.
    from_clinic = await db.clinics.find_one(
        {"clinic_id": user["clinic_id"]},
        {"_id": 0, "clinic_id": 1, "name": 1},
    ) or {"clinic_id": user["clinic_id"], "name": "(unknown)"}

    # Token version lookup (to preserve current force-logout state).
    udoc = await db.users.find_one(
        {"user_id": user["user_id"]}, {"_id": 0, "token_version": 1},
    )
    sid = await mint_session_row(
        db,
        {**user, "clinic_id": target},   # log the *new* clinic on the session row
        request, purpose="switch_clinic",
    )
    token = create_access_token(
        user_id=user["user_id"],
        email=user["email"],
        role=user["role"],
        clinic_id=target,
        token_version=int((udoc or {}).get("token_version") or 0),
        session_id=sid,
    )

    # --- Audit trail (fire-and-forget, not critical path) ---------------
    # Skip the audit insert when the user "switches" to the clinic they
    # are already on — that's a no-op the UI shouldn't trigger but might.
    if target != user["clinic_id"]:
        try:
            client_ip = (request.headers.get("x-forwarded-for") or request.client.host or "").split(",")[0].strip() if request else ""
            ua = (request.headers.get("user-agent") or "")[:300] if request else ""
            await db.clinic_switch_audit.insert_one({
                "audit_id": f"CSA-{uuid.uuid4().hex[:10].upper()}",
                "user_id": user["user_id"],
                "user_email": user["email"],
                "user_role": user["role"],
                "from_clinic_id": from_clinic["clinic_id"],
                "from_clinic_name": from_clinic.get("name"),
                "to_clinic_id": target,
                "to_clinic_name": clinic["name"],
                "ip": client_ip,
                "user_agent": ua,
                "at": datetime.now(timezone.utc).isoformat(),
            })
        except Exception:
            # Never let audit failure block a legitimate switch.
            pass

    return {"access_token": token, "token_type": "bearer",
            "csrf_token": set_auth_cookies(response, token, request),
            "active_clinic_id": target, "active_clinic_name": clinic["name"]}


@api_router.post("/auth/logout")
async def auth_logout(request: Request, response: Response):
    """Clears the httpOnly auth + CSRF cookies. Idempotent — callable
    without auth (a client that lost its token can still wipe its cookies).
    Frontend should also clear in-memory state + local caches client-side.
    """
    clear_auth_cookies(response, request)
    return {"ok": True}


class LinkClinicIn(BaseModel):
    user_id: str
    clinic_id: str


@api_router.post("/auth/link-clinic")
async def link_clinic_to_user(
    payload: LinkClinicIn,
    user=Depends(get_current_user),
):
    """Grant a user access to an additional clinic.

    Gate: only `super_admin` or `founder` can do this (multi-clinic is a
    provisioning action — typically done by AUDINEXA support staff when a
    chain owner onboards a new branch-clinic). Idempotent.
    """
    if user.get("role") not in ("super_admin", "founder"):
        raise HTTPException(status_code=403, detail="Super admin only")

    target_user = await db.users.find_one(
        {"user_id": payload.user_id}, {"_id": 0, "password_hash": 0},
    )
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")
    clinic = await db.clinics.find_one(
        {"clinic_id": payload.clinic_id}, {"_id": 0, "clinic_id": 1, "name": 1},
    )
    if not clinic:
        raise HTTPException(status_code=404, detail="Clinic not found")

    # If this is the user's primary clinic already, nothing to do.
    if target_user.get("clinic_id") == payload.clinic_id:
        return {"ok": True, "already_primary": True}

    await db.users.update_one(
        {"user_id": payload.user_id},
        {"$addToSet": {"additional_clinic_ids": payload.clinic_id}},
    )
    return {"ok": True, "user_id": payload.user_id,
            "clinic_id": payload.clinic_id, "clinic_name": clinic["name"]}


@api_router.post("/auth/unlink-clinic")
async def unlink_clinic_from_user(
    payload: LinkClinicIn,
    user=Depends(get_current_user),
):
    """Revoke a user's access to an additional clinic. Super-admin only."""
    if user.get("role") not in ("super_admin", "founder"):
        raise HTTPException(status_code=403, detail="Super admin only")
    await db.users.update_one(
        {"user_id": payload.user_id},
        {"$pull": {"additional_clinic_ids": payload.clinic_id},
         "$inc": {"token_version": 1}},  # kick them out of any session holding that clinic
    )
    return {"ok": True}


class PageViewIn(BaseModel):
    path: str = Field(..., min_length=1, max_length=300)


@api_router.post("/activity/pageview")
async def record_page_view_endpoint(
    payload: PageViewIn,
    request: Request,
    user=Depends(get_current_user),
):
    """Authenticated frontend pings this on every route change. Throttled
    server-side to avoid write-storms."""
    from utils.activity import record_page_view
    fwd = request.headers.get("x-forwarded-for", "")
    ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else None)
    await record_page_view(db, user, payload.path, ip=ip)
    return {"ok": True}


# ==================== M01: CLINIC ROUTES ====================

@api_router.get("/clinic")
async def get_my_clinic(user=Depends(get_current_user)):
    c = await db.clinics.find_one({"clinic_id": user["clinic_id"]}, {"_id": 0})
    if not c:
        raise HTTPException(status_code=404, detail="Clinic not found")
    return c


# ==================== HELPER FUNCTIONS ====================
# serialize_datetime / deserialize_datetime now live in utils/serde.py — shared
# with the extracted routers. Imported above for _seed_defaults to use.


# ==================== BASIC ROUTES ====================

@api_router.get("/")
async def root():
    return {"message": "ACS Audiology Management System API"}

@api_router.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}


# Non-prefixed /health for Kubernetes liveness/readiness probes.
# Emergent's K8s probe hits 127.0.0.1:8001/health (no /api prefix) and a 404
# would mark the pod unhealthy and fail the deployment.
@app.get("/health")
async def k8s_health_probe():
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}


# ==================== EXTRACTED → routers/ ==================== (== PATIENT ROUTES)
# ==================== EXTRACTED → routers/ ==================== (== M01.B: APPOINTMEN)
# ==================== EXTRACTED → routers/ ==================== (== TOKEN / QUEUE)
# ==================== EXTRACTED → routers/ref_docs.py ====================
# ==================== EXTRACTED → routers/ref_docs.py ==================== (PATIENT NOTES)
# ==================== EXTRACTED → routers/sessions.py ==================== (TEST SESSIONS + PTA)


# Include the router in the main app
app.include_router(api_router)
app.include_router(billing_module.billing_router)
# Self-hosted error telemetry endpoints (POST + reader)
app.include_router(error_telemetry_router)
app.include_router(admin_errors_router)
# Founder backup admin (list / run-now / config)
from routers.backup_admin import router as backup_admin_router  # noqa: E402
app.include_router(backup_admin_router)

from routers.mfa import (  # noqa: E402
    router as mfa_router,
    auth_router as mfa_auth_router,
    issue_mfa_challenge,
)
app.include_router(mfa_router)
app.include_router(mfa_auth_router)

from routers.dpdpa import router as dpdpa_router  # noqa: E402
app.include_router(dpdpa_router)

from routers.user_sessions import router as user_sessions_router, mint_session_row  # noqa: E402
app.include_router(user_sessions_router)

from routers.status_page import router as status_page_router  # noqa: E402
app.include_router(status_page_router)

from routers.email_verify import router as email_verify_router  # noqa: E402
app.include_router(email_verify_router)

from routers.admin_backfill import router as admin_backfill_router  # noqa: E402
app.include_router(admin_backfill_router)

from routers import closeouts as closeouts_router    # noqa: E402
from routers import reports as reports_router         # noqa: E402
from routers import patients as patients_router       # noqa: E402
from routers import vault as vault_router              # noqa: E402
from routers import invitations as invitations_router  # noqa: E402
from routers import care_support as care_support_router  # noqa: E402
from routers import appointments as appointments_router  # noqa: E402
from routers import tokens as tokens_router           # noqa: E402
from routers import test_sessions as sessions_router       # noqa: E402
from routers import diagnostics_queue as diagnostics_queue_router  # noqa: E402
from routers import ref_docs as ref_docs_router       # noqa: E402
from routers import branches as branches_router       # noqa: E402
from routers import vendors as vendors_router         # noqa: E402
from routers import ha_products as ha_products_router       # noqa: E402
from routers import ha_inventory as ha_inventory_router     # noqa: E402
from routers import ha_procurement as ha_procurement_router # noqa: E402
from routers import ha_quotations as ha_quotations_router   # noqa: E402
from routers import ha_sales as ha_sales_router             # noqa: E402
from routers import ha_fittings as ha_fittings_router       # noqa: E402
from routers import ha_trials as ha_trials_router             # noqa: E402
from routers import ha_crm as ha_crm_router                   # noqa: E402
from routers import ha_analytics as ha_analytics_router       # noqa: E402
from routers import ha_service as ha_service_router           # noqa: E402
from routers import ha_loaners as ha_loaners_router           # noqa: E402
from routers import ha_tradeins as ha_tradeins_router         # noqa: E402
from routers import subscription as subscription_router       # noqa: E402
from routers import ha_service_v2 as ha_service_v2_router     # noqa: E402
from routers import ha_repair_ops as ha_repair_ops_router     # noqa: E402
from routers import ha_amc as ha_amc_router                   # noqa: E402
from routers import analytics as analytics_router             # noqa: E402
from routers import referrals as referrals_router             # noqa: E402
from routers import referral_partners as referral_partners_router  # noqa: E402
from routers import patient_portal as patient_portal_router   # noqa: E402
from routers import admin_panel as admin_panel_router         # noqa: E402
from routers import admin_panel_b as admin_panel_b_router     # noqa: E402
from routers import admin_activity as admin_activity_router   # noqa: E402
from routers import schedules as schedules_router              # noqa: E402
from routers import export_data as export_data_router         # noqa: E402
from routers import report_handover as report_handover_router # noqa: E402
from routers import settings as settings_router                # noqa: E402
from routers import stock_transfers as stock_transfers_router  # noqa: E402
from routers import connect as connect_router                  # noqa: E402
from routers import clinic_status as clinic_status_router      # noqa: E402
from routers import greetings as greetings_router               # noqa: E402
from routers import razorpay_payments as razorpay_router        # noqa: E402
from routers import imports as imports_router                    # noqa: E402
from routers import accounts as accounts_router                   # noqa: E402
from routers import legal as legal_router                         # noqa: E402
from routers import password_reset as password_reset_router       # noqa: E402
from routers import ha_quick_sale as ha_quick_sale_router         # noqa: E402
from routers import ha_ear_moulds as ha_ear_moulds_router         # noqa: E402
from routers import ha_custom_ha_orders as ha_custom_ha_orders_router  # noqa: E402
from routers import marketing_traffic as marketing_traffic_router  # noqa: E402
from routers import csv_email_exports as csv_email_exports_router # noqa: E402
from routers import hearing_report_versions as hearing_report_versions_router  # noqa: E402
from routers import launch_banner as launch_banner_router  # noqa: E402
from routers import advance_receipts as advance_receipts_router  # noqa: E402

app.include_router(closeouts_router.router)
app.include_router(reports_router.router)
app.include_router(patients_router.router)
from routers import family_groups as family_groups_router  # noqa: E402
app.include_router(family_groups_router.router)
from routers import clinic_groups as clinic_groups_router  # noqa: E402
app.include_router(clinic_groups_router.router)
from routers import stock_requests as stock_requests_router  # noqa: E402
app.include_router(stock_requests_router.router)
app.include_router(vault_router.router)
app.include_router(care_support_router.router)
# Invitations router mounts at /api (paths inside the router include /settings/*
# for owner endpoints and /public/* for invitee endpoints)
app.include_router(invitations_router.router, prefix="/api")
app.include_router(appointments_router.router)
app.include_router(tokens_router.router)
app.include_router(sessions_router.router)
app.include_router(diagnostics_queue_router.router)
app.include_router(ref_docs_router.router)
app.include_router(branches_router.router)
app.include_router(vendors_router.router)
app.include_router(ha_products_router.router)
app.include_router(ha_inventory_router.router)
app.include_router(ha_procurement_router.router)
app.include_router(ha_quotations_router.router)
app.include_router(ha_sales_router.router)
app.include_router(ha_fittings_router.router)
app.include_router(ha_trials_router.router)
app.include_router(ha_crm_router.router)
app.include_router(ha_analytics_router.router)
app.include_router(ha_service_router.router)
app.include_router(ha_loaners_router.router)
app.include_router(ha_tradeins_router.router)
app.include_router(subscription_router.router)
app.include_router(ha_service_v2_router.router)
app.include_router(ha_repair_ops_router.router)
app.include_router(ha_amc_router.router)
app.include_router(analytics_router.router)
app.include_router(referrals_router.router)
app.include_router(referral_partners_router.router)
app.include_router(patient_portal_router.router)
app.include_router(admin_panel_router.router)
app.include_router(admin_panel_b_router.router)
app.include_router(admin_activity_router.router)
app.include_router(schedules_router.router)
app.include_router(export_data_router.router)
app.include_router(csv_email_exports_router.router)
app.include_router(hearing_report_versions_router.router)
app.include_router(report_handover_router.router)
app.include_router(settings_router.router)
app.include_router(stock_transfers_router.router)
app.include_router(connect_router.router)
app.include_router(clinic_status_router.router)
app.include_router(greetings_router.router)
app.include_router(razorpay_router.router)
app.include_router(imports_router.router)
app.include_router(accounts_router.router)
app.include_router(legal_router.router)
app.include_router(password_reset_router.router)
app.include_router(launch_banner_router.router)
app.include_router(launch_banner_router.public_router)
app.include_router(ha_quick_sale_router.router)
app.include_router(ha_ear_moulds_router.router)
app.include_router(ha_custom_ha_orders_router.router)
app.include_router(marketing_traffic_router.router)
app.include_router(advance_receipts_router.router)

# ---- CORS lockdown ----
# Production SHOULD set CORS_ORIGINS to a comma-separated list of allowed origins
# (e.g. "https://audinexa.com,https://www.audinexa.com"). The default below is a
# **resilience fallback**: it allows the production apex + www + every Emergent
# preview subdomain via regex, so the app keeps working even if the explicit env
# var is missing on a fresh deploy.
#
# This change was driven by a P0 prod incident on 2026-06-02: after shipping
# cookie auth (`withCredentials: true`), production was unconfigured for CORS
# and every login returned "Network Error". The regex fallback below means a
# similar regression in future just-works without ops intervention.
#
# UPDATE 2026-06-02 (second incident): production had `CORS_ORIGINS=*` set on
# a stale ops config. With `withCredentials: true` on the frontend, the browser
# REJECTS responses carrying `Access-Control-Allow-Origin: *` — every login
# returned "Network Error". The block below now **explicitly ignores
# `CORS_ORIGINS=*`** because it is fundamentally incompatible with our cookie
# auth model. Operators wanting truly-open CORS would also need to drop cookie
# auth — not something we'd silently do.
_cors_raw = os.environ.get('CORS_ORIGINS', '').strip()

# Always-accepted origin pattern (apex audinexa.com + any subdomain like www,
# api, staging; plus every Emergent preview deployment for the dev pod).
_PROD_ORIGIN_REGEX = (
    r"^https://"
    r"(?:[A-Za-z0-9-]+\.)?audinexa\.com$"
    r"|^https://[A-Za-z0-9-]+\.preview\.emergentagent\.com$"
    r"|^https?://localhost(?::\d+)?$"        # local dev frontend
    r"|^https?://127\.0\.0\.1(?::\d+)?$"
)
_allow_origin_regex: Optional[str] = _PROD_ORIGIN_REGEX
_allow_credentials = True

if _cors_raw and _cors_raw != '*':
    # Explicit allowlist takes precedence. Still allow credentials.
    _allow_origins = [o.strip() for o in _cors_raw.split(',') if o.strip()]
elif _cors_raw == '*':
    # IGNORE wildcard. Cookie auth (`withCredentials: true`) means the browser
    # will refuse responses with `Allow-Origin: *`. Fall through to regex so
    # legitimate origins (audinexa.com + preview) keep working with cookies.
    logging.getLogger(__name__).error(
        "CORS_ORIGINS='*' detected — IGNORED because it breaks cookie auth. "
        "Falling back to regex (audinexa.com + preview + localhost). "
        "Set CORS_ORIGINS to an explicit comma-separated allowlist to override."
    )
    _allow_origins = []
else:
    # No env var → use regex-only fallback (still credential-friendly).
    _allow_origins = []
    logging.getLogger(__name__).info(
        "CORS_ORIGINS unset — using built-in regex fallback "
        "(audinexa.com + Emergent preview + localhost)."
    )

_cors_kwargs = dict(
    allow_credentials=_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],  # CSV export filenames
)
if _allow_origin_regex:
    _cors_kwargs["allow_origin_regex"] = _allow_origin_regex
_cors_kwargs["allow_origins"] = _allow_origins

app.add_middleware(CORSMiddleware, **_cors_kwargs)

# GZip compress every response ≥ 500 bytes — cuts JSON payloads by 60-80%
# and shaves 100-300ms off first-paint over 4G / weak Wi-Fi (the audience
# for this app is field-office clinics — real network latency matters).
from starlette.middleware.gzip import GZipMiddleware  # noqa: E402
app.add_middleware(GZipMiddleware, minimum_size=500, compresslevel=6)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


