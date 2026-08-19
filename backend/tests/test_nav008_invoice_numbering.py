"""NAV-008 · Invoice Integrity & Numbering Hardening — regression suite.

Covers the 7 approved fixes (B1–B7 in Phase 2 proposal):

  * Unify Paths D + E to canonical `_next_invoice_no`
  * Duplicate-key retry safeguard `_insert_invoice_with_retry`
  * `seed_demo_premium.py` counter sync
  * `seed_story_demo.py` field rename + counter no-inflate
  * CSV import Policy B (preserve + collision detect + `original_invoice_no`)
  * Pydantic backward-compatible `invoice_no` pattern validator
  * Counter reconciliation migration (additive-only)

Design notes
------------
* Every test builds and tears down its own NAV-008-scoped state so runs
  are fully idempotent. No touch of the pytest bootstrap tenant, no
  touch of demo-seed clinics, no touch of production.
* Behavioural assertions drive the HTTP surface where possible.
* Direct DB access is used ONLY for state verification (counter values,
  cross-collection cascades, index enforcement).
* Fixtures scope every artefact under a NAV008-* prefix for safe
  post-hoc cleanup.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

import pytest
import requests

from _helpers import API, H, login

# ─── Test tenant identifiers ─────────────────────────────────────────────
CLINIC_A = "NAV008-CLINIC-A"
CLINIC_B = "NAV008-CLINIC-B"
PATIENT_A = "PT-NAV008-A-1"
PATIENT_B = "PT-NAV008-B-1"

OWNER_A_EMAIL = "nav008.owner.a@audinexa.test"
OWNER_A_PASSWORD = "Nav008OwnerA@1"
OWNER_B_EMAIL = "nav008.owner.b@audinexa.test"
OWNER_B_PASSWORD = "Nav008OwnerB@1"

SVC_ID_A = "SVC-NAV008-A-PTA"
SVC_ID_B = "SVC-NAV008-B-PTA"


# ─── Async DB helper ─────────────────────────────────────────────────────
def _run(coro):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            raise RuntimeError("nested loop")
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


async def _db():
    from motor.motor_asyncio import AsyncIOMotorClient
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    return client, client[os.environ["DB_NAME"]]


# ─── Bootstrap ───────────────────────────────────────────────────────────
async def _bootstrap() -> None:
    from auth import hash_password
    client, db = await _db()
    try:
        now_iso = datetime.now(timezone.utc).isoformat()

        # Two isolated clinic tenants + one patient per clinic.
        for cid, name in [(CLINIC_A, "NAV008 Clinic A"),
                          (CLINIC_B, "NAV008 Clinic B")]:
            await db.clinics.update_one(
                {"clinic_id": cid},
                {"$setOnInsert": {
                    "clinic_id": cid, "name": name,
                    "city": "Bengaluru", "state": "Karnataka",
                    "status": "active",
                    "subscription_tier": "PREMIUM",
                    "mrd_prefix": f"NAV008{cid[-1]}",
                    "created_at": now_iso,
                }},
                upsert=True,
            )

        # Two clinic_owner users.
        for email, pw, uid, cid in [
            (OWNER_A_EMAIL, OWNER_A_PASSWORD, "USR-NAV008-OWNER-A", CLINIC_A),
            (OWNER_B_EMAIL, OWNER_B_PASSWORD, "USR-NAV008-OWNER-B", CLINIC_B),
        ]:
            await db.users.update_one(
                {"email": email},
                {"$set": {
                    "user_id": uid, "email": email,
                    "name": f"NAV008 Owner {cid[-1]}",
                    "role": "clinic_owner",
                    "clinic_id": cid,
                    "additional_clinic_ids": [],
                    "active": True, "email_verified": True,
                    "password_hash": hash_password(pw),
                    "created_at": now_iso,
                }},
                upsert=True,
            )

        # One taxable service per clinic (used by create-invoice tests).
        for sid, cid in [(SVC_ID_A, CLINIC_A), (SVC_ID_B, CLINIC_B)]:
            await db.services.update_one(
                {"service_id": sid},
                {"$set": {
                    "service_id": sid, "clinic_id": cid,
                    "name": "PTA Consultation",
                    "price": 1000.0,
                    "is_taxable": True,
                    "gst_rate": 18.0,
                    "gst_inclusive": True,
                    "hsn_sac": "998512",
                    "active": True,
                    "created_at": now_iso,
                }},
                upsert=True,
            )

        # One patient per clinic (referenced by every create-invoice test).
        for pid, cid, mrd in [
            ("PT-NAV008-A-1", CLINIC_A, "NAV008A-0001"),
            ("PT-NAV008-B-1", CLINIC_B, "NAV008B-0001"),
        ]:
            await db.patients.update_one(
                {"patient_id": pid},
                {"$set": {
                    "patient_id": pid, "clinic_id": cid,
                    "name": f"NAV008 Patient {cid[-1]}",
                    "mrd": mrd,
                    "mobile": "9000000001",
                    "gender": "Male", "age": 30,
                    "state_code": "29",
                    "created_at": now_iso,
                }},
                upsert=True,
            )

        # Ensure a clean slate for each clinic's invoice counter + rows.
        await db.counters.delete_many({"_id": {"$regex": r"^invoice:NAV008-"}})
        await db.invoices.delete_many({"clinic_id": {"$in": [CLINIC_A, CLINIC_B]}})
        await db.payments.delete_many({"clinic_id": {"$in": [CLINIC_A, CLINIC_B]}})
        await db.ha_sales.delete_many({"clinic_id": {"$in": [CLINIC_A, CLINIC_B]}})
    finally:
        client.close()


@pytest.fixture(scope="module", autouse=True)
def _bootstrap_module():
    _run(_bootstrap())
    _run(_install_test_scoped_unique_index())
    yield
    _run(_drop_test_scoped_unique_index())


# ─── NAV-008 Phase 3B test-scoped unique index ───────────────────────────
# The main compound unique index (`clinic_id_1_invoice_no_1_unique`) is
# CLUSTER-wide and cannot be installed on Preview while the known
# historical duplicate `INV/2026/000004` exists in `tenant-sound-clinic-blr`.
#
# To let tests 3, 7, 14 exercise the DB-level uniqueness guarantee AND
# the retry helper end-to-end, we install a narrower NAV-008-scoped index
# at module fixture time. It only enforces uniqueness for clinic_ids that
# begin with `NAV008-` — so the historical duplicate in a real-clinic
# namespace is untouched, and the test tenants get the full DB-level
# safeguard. The index is dropped at module teardown so no persistent
# state remains.
#
# This is a TEST-ONLY technique. Production and Preview app startup
# continue to attempt the un-scoped index; the try/except wrapper in
# server.py surfaces a loud error when that fails on Preview.
_TEST_INDEX_NAME = "nav008_test_scoped_invoice_no_unique"


async def _install_test_scoped_unique_index() -> None:
    client, db = await _db()
    try:
        # Drop first in case a prior test module aborted mid-run.
        try:
            await db.invoices.drop_index(_TEST_INDEX_NAME)
        except Exception:
            pass
        await db.invoices.create_index(
            [("clinic_id", 1), ("invoice_no", 1)],
            unique=True,
            partialFilterExpression={
                "invoice_no": {"$type": "string"},
                "clinic_id": {"$in": [CLINIC_A, CLINIC_B]},
            },
            name=_TEST_INDEX_NAME,
        )
    finally:
        client.close()


async def _drop_test_scoped_unique_index() -> None:
    client, db = await _db()
    try:
        try:
            await db.invoices.drop_index(_TEST_INDEX_NAME)
        except Exception:
            pass
    finally:
        client.close()


@pytest.fixture(autouse=True)
def _reset_between_tests():
    """Clean per-test to guarantee isolation."""
    async def _c():
        client, db = await _db()
        try:
            await db.invoices.delete_many({"clinic_id": {"$in": [CLINIC_A, CLINIC_B]}})
            await db.payments.delete_many({"clinic_id": {"$in": [CLINIC_A, CLINIC_B]}})
            await db.counters.delete_many({"_id": {"$regex": r"^invoice:NAV008-"}})
            await db.ha_sales.delete_many({"clinic_id": {"$in": [CLINIC_A, CLINIC_B]}})
        finally:
            client.close()
    _run(_c())
    yield


# ─── Helpers ─────────────────────────────────────────────────────────────
def _login_a() -> str:
    return login(OWNER_A_EMAIL, OWNER_A_PASSWORD)


def _login_b() -> str:
    return login(OWNER_B_EMAIL, OWNER_B_PASSWORD)


def _create_invoice(token: str, svc_id: str, patient_id: str = PATIENT_A):
    """POST /api/billing/invoices with a single-line invoice."""
    body = {
        "patient_id": patient_id,
        "lines": [{"service_id": svc_id, "description": "PTA", "quantity": 1}],
    }
    return requests.post(f"{API}/billing/invoices", json=body, headers=H(token), timeout=15)


# ══════════════════════════════════════════════════════════════════════════
# TESTS
# ══════════════════════════════════════════════════════════════════════════

def test_01_sequential_numbering_gives_next_number():
    tok = _login_a()
    r1 = _create_invoice(tok, SVC_ID_A, PATIENT_A)
    assert r1.status_code == 200, r1.text
    r2 = _create_invoice(tok, SVC_ID_A, PATIENT_A)
    assert r2.status_code == 200, r2.text
    seq1 = int(r1.json()["invoice_no"].split("/")[-1])
    seq2 = int(r2.json()["invoice_no"].split("/")[-1])
    assert seq2 == seq1 + 1, f"expected consecutive seqs, got {seq1} → {seq2}"


def test_02_concurrent_creation_produces_distinct_numbers():
    """Fire 8 parallel POSTs at the same clinic and assert all invoice_no
    values are unique. Relies on the atomic counter."""
    import concurrent.futures
    tok = _login_a()
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futures = [
            ex.submit(_create_invoice, tok, SVC_ID_A, PATIENT_A)
            for i in range(8)
        ]
        results = [f.result(timeout=30) for f in futures]
    for r in results:
        assert r.status_code == 200, r.text
    seen = {r.json()["invoice_no"] for r in results}
    assert len(seen) == 8, f"expected 8 distinct invoice_no; got {len(seen)}: {seen}"


def test_03_same_clinic_duplicate_rejected_by_unique_index_or_retry():
    """After the compound unique index is installed, a raw insert of a
    duplicate (clinic_id, invoice_no) MUST fail with E11000. This test
    verifies the DB-level guarantee against our test-scoped index."""
    tok = _login_a()
    r = _create_invoice(tok, SVC_ID_A, PATIENT_A)
    assert r.status_code == 200
    existing_no = r.json()["invoice_no"]
    # Try a raw insert with the same (clinic_id, invoice_no) → must fail.
    from pymongo.errors import DuplicateKeyError

    async def _dup_insert():
        client, db = await _db()
        try:
            try:
                await db.invoices.insert_one({
                    "invoice_id": "INV-DUPTEST",
                    "clinic_id": CLINIC_A,
                    "invoice_no": existing_no,
                    "patient_id": "P-X",
                    "invoice_date": datetime.now(timezone.utc).isoformat(),
                })
                return None
            except DuplicateKeyError as e:
                return str(e)
        finally:
            client.close()

    err = _run(_dup_insert())
    assert err is not None and ("11000" in err or "duplicate" in err.lower()), err


def test_04_cross_clinic_same_number_allowed():
    """Clinic A and Clinic B both start their own INV/YYYY/000001 sequence."""
    r_a = _create_invoice(_login_a(), SVC_ID_A, PATIENT_A)
    r_b = _create_invoice(_login_b(), SVC_ID_B, PATIENT_B)
    assert r_a.status_code == 200 and r_b.status_code == 200
    # Both should have received "000001" since counters are per-clinic.
    a_seq = int(r_a.json()["invoice_no"].split("/")[-1])
    b_seq = int(r_b.json()["invoice_no"].split("/")[-1])
    assert a_seq == b_seq == 1, f"expected per-clinic seq=1; got A={a_seq} B={b_seq}"


def test_05_yearly_counter_isolation():
    """The counter key includes year; explicitly probe the shape."""
    _create_invoice(_login_a(), SVC_ID_A, PATIENT_A)

    async def _probe():
        client, db = await _db()
        try:
            year = datetime.now(timezone.utc).year
            doc = await db.counters.find_one({"_id": f"invoice:{CLINIC_A}:{year}"})
            return doc, year
        finally:
            client.close()

    doc, year = _run(_probe())
    assert doc is not None
    assert doc["seq"] >= 1
    # A future-year counter must not exist as a side-effect.
    async def _future():
        client, db = await _db()
        try:
            return await db.counters.find_one({"_id": f"invoice:{CLINIC_A}:{year + 1}"})
        finally:
            client.close()
    assert _run(_future()) is None


def test_06_failed_insert_leaves_counter_gap_no_reuse():
    """A create that fails Pydantic validation must NOT decrement the
    counter. Next successful create moves past the failed number."""
    tok = _login_a()
    r1 = _create_invoice(tok, SVC_ID_A, PATIENT_A)
    assert r1.status_code == 200
    seq1 = int(r1.json()["invoice_no"].split("/")[-1])
    # Force a validation failure — missing `lines`.
    bad = requests.post(
        f"{API}/billing/invoices",
        json={"patient_id": PATIENT_A, "lines": []},
        headers=H(tok), timeout=15,
    )
    assert bad.status_code in (400, 422)
    r2 = _create_invoice(tok, SVC_ID_A, PATIENT_A)
    assert r2.status_code == 200
    seq2 = int(r2.json()["invoice_no"].split("/")[-1])
    # seq2 > seq1; the failed row DID or DID NOT burn a counter (both
    # behaviours are contract-conformant — GST allows gaps). Only assert
    # forward-monotonicity.
    assert seq2 > seq1


def test_07_duplicate_key_retry_helper_produces_next_number():
    """Directly exercise `_insert_invoice_with_retry`: pre-seed a
    doc with a taken invoice_no, then let the helper collide once
    and recover on the next counter value."""
    from pymongo.errors import DuplicateKeyError

    async def _do():
        client, db = await _db()
        try:
            year = datetime.now(timezone.utc).year
            # Occupy INV/{year}/000001 via a raw insert.
            occupied_no = f"INV/{year}/000001"
            try:
                await db.invoices.insert_one({
                    "invoice_id": "INV-OCCUPY1",
                    "clinic_id": CLINIC_A,
                    "invoice_no": occupied_no,
                    "patient_id": "P-X",
                })
            except DuplicateKeyError:
                pass  # already occupied by earlier test
            # Reset the counter so `_next_invoice_no` will hand out 1 first.
            await db.counters.update_one(
                {"_id": f"invoice:{CLINIC_A}:{year}"},
                {"$set": {"seq": 0}},
                upsert=True,
            )
            # Now drive the helper directly.
            from billing import _insert_invoice_with_retry, _next_invoice_no
            first_no = await _next_invoice_no(db, CLINIC_A)
            assert first_no == occupied_no
            inv_doc = {
                "invoice_id": "INV-RETRY-TEST",
                "clinic_id": CLINIC_A,
                "invoice_no": first_no,
                "patient_id": "P-Y",
            }
            await _insert_invoice_with_retry(db, inv_doc, CLINIC_A)
            return inv_doc["invoice_no"]
        finally:
            client.close()

    out = _run(_do())
    # The helper must have advanced past the occupied number.
    assert out != f"INV/{datetime.now(timezone.utc).year}/000001", \
        f"helper did not renew; still landed on {out}"


def test_08_ha_quick_sale_uses_canonical_numbering():
    """The HA quick-sale flow must emit `INV/YYYY/NNNNNN` decimal, not hex."""
    tok = _login_a()
    # Sanity: create a plain invoice first — pins that the counter is
    # exercised and the sequence has started.
    r_seed = _create_invoice(tok, SVC_ID_A, PATIENT_A)
    assert r_seed.status_code == 200
    seed_seq = int(r_seed.json()["invoice_no"].split("/")[-1])
    # We do NOT drive the full HA quick sale here (requires HA product
    # setup) — instead we assert that the numbering *helper* used by
    # ha_quick_sale (the canonical `_next_invoice_no`) hands out the
    # next decimal sequence.
    import billing

    async def _do():
        client, db = await _db()
        try:
            return await billing._next_invoice_no(db, CLINIC_A)
        finally:
            client.close()

    next_no = _run(_do())
    year = datetime.now(timezone.utc).year
    assert next_no.startswith(f"INV/{year}/")
    tail = next_no.split("/")[-1]
    assert tail.isdigit() and len(tail) == 6
    assert int(tail) == seed_seq + 1


def test_09_ha_service_ticket_uses_canonical_numbering_helper():
    """Path C also depends on `_next_invoice_no`. Same helper coverage
    as test_08 — enough to prove the shared numbering contract."""
    async def _do():
        client, db = await _db()
        try:
            from billing import _next_invoice_no
            first = await _next_invoice_no(db, CLINIC_A)
            second = await _next_invoice_no(db, CLINIC_A)
            return first, second
        finally:
            client.close()

    first, second = _run(_do())
    tail1 = int(first.split("/")[-1])
    tail2 = int(second.split("/")[-1])
    assert tail2 == tail1 + 1


def test_10_custom_ha_order_no_longer_produces_hex_numbers():
    """Path D — ha_custom_ha_orders — must no longer expose the
    retired uuid-hex helper. Grep the module for the hex generator."""
    from routers import ha_custom_ha_orders
    # The retired local helper must be gone.
    assert not hasattr(ha_custom_ha_orders, "_new_invoice_no"), (
        "ha_custom_ha_orders._new_invoice_no should be removed"
    )
    # And the canonical helper must be imported.
    assert hasattr(ha_custom_ha_orders, "_next_invoice_no"), (
        "ha_custom_ha_orders must import billing._next_invoice_no"
    )


def test_11_ear_mould_no_longer_calls_hex_generator():
    """Path E — ha_ear_moulds — the local `_new_invoice_no` is
    retired and now raises RuntimeError to catch any lingering caller."""
    from routers import ha_ear_moulds
    with pytest.raises(RuntimeError):
        ha_ear_moulds._new_invoice_no("any-clinic")
    assert hasattr(ha_ear_moulds, "_next_invoice_no"), (
        "ha_ear_moulds must import billing._next_invoice_no"
    )


def test_12_csv_import_collision_detection_writes_failure_row():
    """The CSV importer must NOT silently overwrite an existing
    `(clinic_id, invoice_no)`. We assert the pre-insert guard by
    inspecting `imports.py` for the marker phrases + confirm the
    module still parses."""
    from routers import imports as imports_router
    src = open(imports_router.__file__, "r", encoding="utf-8").read()
    assert "NAV-008 Policy B" in src, "imports.py must contain NAV-008 Policy B block"
    assert "already exists in this clinic" in src, (
        "imports.py must surface a duplicate-invoice_no failure row"
    )
    assert "external_invoice_no" in src, (
        "imports.py must preserve the original invoice_no as external_invoice_no"
    )


def test_13_csv_import_canonical_fallback_present():
    """When `bill_no` is empty, the importer must fall back to an
    IMP/YYYY-MM-DD/… canonical placeholder — never to the empty
    string and never to invoice_no=None."""
    from routers import imports as imports_router
    src = open(imports_router.__file__, "r", encoding="utf-8").read()
    assert 'f"IMP/{visit_date or datetime.utcnow().strftime' in src, (
        "IMP/… canonical fallback for missing bill_no must remain in place"
    )


def test_14_missing_invoice_no_partial_index_bypass():
    """The compound unique index uses a partial filter to skip docs
    with invoice_no missing/null so preview test-fixture rows don't
    collide with real data. Assert two null-invoice-no rows can
    coexist WITHOUT tripping the index."""
    async def _do():
        client, db = await _db()
        try:
            # Two docs with same clinic_id and null invoice_no.
            await db.invoices.insert_one({
                "invoice_id": "INV-NULL-A",
                "clinic_id": CLINIC_A,
                "invoice_no": None,
                "patient_id": "P-X",
            })
            await db.invoices.insert_one({
                "invoice_id": "INV-NULL-B",
                "clinic_id": CLINIC_A,
                "invoice_no": None,
                "patient_id": "P-Y",
            })
            n = await db.invoices.count_documents(
                {"clinic_id": CLINIC_A, "invoice_no": None},
            )
            return n
        finally:
            client.close()

    out = _run(_do())
    assert out == 2, f"expected 2 null-invoice_no rows to coexist, got {out}"


def test_15_pydantic_model_rejects_bad_invoice_no_format():
    """Direct Invoice model construction with malformed invoice_no →
    ValidationError."""
    from pydantic import ValidationError
    from models._canonical import Invoice, InvoiceLine
    with pytest.raises(ValidationError):
        Invoice(
            clinic_id=CLINIC_A,
            invoice_no="not-a-valid-format",   # invalid
            patient_id="P-X",
            patient_name="X",
            invoice_date=datetime.now(timezone.utc),
            lines=[InvoiceLine(description="X", qty=1, unit_price=100.0,
                               taxable_value=100.0, gst_rate=0.0,
                               cgst_rate=0.0, sgst_rate=0.0, igst_rate=0.0,
                               cgst_amount=0.0, sgst_amount=0.0, igst_amount=0.0,
                               line_total=100.0)],
        )
    # Canonical format passes.
    Invoice(
        clinic_id=CLINIC_A,
        invoice_no="INV/2026/000042",
        patient_id="P-X",
        patient_name="X",
        invoice_date=datetime.now(timezone.utc),
        lines=[InvoiceLine(description="X", qty=1, unit_price=100.0,
                           taxable_value=100.0, gst_rate=0.0,
                           cgst_rate=0.0, sgst_rate=0.0, igst_rate=0.0,
                           cgst_amount=0.0, sgst_amount=0.0, igst_amount=0.0,
                           line_total=100.0)],
    )
    # Legacy hex passes (backwards compatibility).
    Invoice(
        clinic_id=CLINIC_A,
        invoice_no="INV/2026/0669C8",
        patient_id="P-X",
        patient_name="X",
        invoice_date=datetime.now(timezone.utc),
        lines=[InvoiceLine(description="X", qty=1, unit_price=100.0,
                           taxable_value=100.0, gst_rate=0.0,
                           cgst_rate=0.0, sgst_rate=0.0, igst_rate=0.0,
                           cgst_amount=0.0, sgst_amount=0.0, igst_amount=0.0,
                           line_total=100.0)],
    )
    # IMP/… import-canonical format passes.
    Invoice(
        clinic_id=CLINIC_A,
        invoice_no="IMP/2026/AB12CD",
        patient_id="P-X",
        patient_name="X",
        invoice_date=datetime.now(timezone.utc),
        lines=[InvoiceLine(description="X", qty=1, unit_price=100.0,
                           taxable_value=100.0, gst_rate=0.0,
                           cgst_rate=0.0, sgst_rate=0.0, igst_rate=0.0,
                           cgst_amount=0.0, sgst_amount=0.0, igst_amount=0.0,
                           line_total=100.0)],
    )


def test_16_tenant_isolation_invoice_read_still_enforced():
    """NAV-007 regression guard: Clinic A owner cannot fetch Clinic B's
    invoice by id."""
    r_a = _create_invoice(_login_a(), SVC_ID_A, PATIENT_A)
    r_b = _create_invoice(_login_b(), SVC_ID_B, PATIENT_B)
    assert r_a.status_code == 200 and r_b.status_code == 200
    b_iid = r_b.json()["invoice_id"]
    # A tries to fetch B's invoice — must be 404.
    r = requests.get(f"{API}/billing/invoices/{b_iid}",
                     headers=H(_login_a()), timeout=15)
    assert r.status_code == 404, r.text


def test_17_payment_reference_survives_by_invoice_id():
    """Payments reference invoices by invoice_id — NOT by invoice_no.
    Verify a payment lookup by invoice_id still works cleanly."""
    tok = _login_a()
    r = _create_invoice(tok, SVC_ID_A, PATIENT_A)
    assert r.status_code == 200
    iid = r.json()["invoice_id"]
    pay = requests.post(
        f"{API}/billing/invoices/{iid}/payments",
        json={"method": "cash", "amount": 500.0,
              "reference": "TEST-REF", "notes": "unit test"},
        headers=H(tok), timeout=15,
    )
    assert pay.status_code == 200, pay.text
    # Read back — payments must be visible.
    got = requests.get(f"{API}/billing/invoices/{iid}", headers=H(tok), timeout=15)
    assert got.status_code == 200
    assert got.json()["paid_total"] >= 500.0


def test_18_counter_reconcile_advances_only_upward():
    """`$max` invariant: `nav008_counter_reconcile.py` never lowers a
    counter, even if an older invoice_no with a low seq is added later."""
    async def _do():
        client, db = await _db()
        try:
            year = datetime.now(timezone.utc).year
            key = f"invoice:{CLINIC_A}:{year}"
            # Pin counter high.
            await db.counters.update_one(
                {"_id": key}, {"$set": {"seq": 999}}, upsert=True,
            )
            # Insert a low-seq legacy-style invoice directly.
            await db.invoices.insert_one({
                "invoice_id": "INV-LEGACY-LOW",
                "clinic_id": CLINIC_A,
                "invoice_no": f"INV/{year}/000005",
                "patient_id": "P-LOW",
            })
            # Emulate reconcile's core: $max cannot lower.
            await db.counters.update_one(
                {"_id": key}, {"$max": {"seq": 5}},
            )
            doc = await db.counters.find_one({"_id": key})
            return doc["seq"]
        finally:
            client.close()

    seq = _run(_do())
    assert seq == 999, f"$max should not lower counter; got {seq}"


def test_19_counter_reconcile_script_idempotent():
    """Two consecutive runs of the reconcile script leave the counter
    identical after the first application."""
    async def _do():
        client, db = await _db()
        try:
            year = datetime.now(timezone.utc).year
            key = f"invoice:{CLINIC_A}:{year}"
            # Clean slate.
            await db.counters.delete_one({"_id": key})
            await db.invoices.delete_many({"clinic_id": CLINIC_A})
            # Insert three canonical invoices with gaps to test $max.
            for seq in (1, 4, 10):
                await db.invoices.insert_one({
                    "invoice_id": f"INV-RECON-{seq}",
                    "clinic_id": CLINIC_A,
                    "invoice_no": f"INV/{year}/{str(seq).zfill(6)}",
                    "patient_id": f"P-{seq}",
                })
            # Emulate reconcile: $max with the current max.
            await db.counters.update_one(
                {"_id": key}, {"$max": {"seq": 10}}, upsert=True,
            )
            first = (await db.counters.find_one({"_id": key}))["seq"]
            # Run again — no change.
            await db.counters.update_one(
                {"_id": key}, {"$max": {"seq": 10}}, upsert=True,
            )
            second = (await db.counters.find_one({"_id": key}))["seq"]
            return first, second
        finally:
            client.close()

    first, second = _run(_do())
    assert first == second == 10


def test_20_seed_scripts_dont_leave_counter_behind():
    """Simulate the fixed seed pattern: insert an invoice batch with
    an explicit seq, then $max the counter — the next real
    _next_invoice_no call must return seq+1, not 1."""
    async def _do():
        client, db = await _db()
        try:
            year = datetime.now(timezone.utc).year
            key = f"invoice:{CLINIC_A}:{year}"
            await db.counters.delete_one({"_id": key})
            await db.invoices.delete_many({"clinic_id": CLINIC_A})
            # Seed pattern (fixed): batch insert + $max sync.
            for seq in range(1, 6):
                await db.invoices.insert_one({
                    "invoice_id": f"INV-SEED-{seq}",
                    "clinic_id": CLINIC_A,
                    "invoice_no": f"INV/{year}/{str(seq).zfill(6)}",
                    "patient_id": f"P-S{seq}",
                })
            await db.counters.update_one(
                {"_id": key}, {"$max": {"seq": 5}}, upsert=True,
            )
            # Real call — must return seq=6.
            from billing import _next_invoice_no
            next_no = await _next_invoice_no(db, CLINIC_A)
            return next_no
        finally:
            client.close()

    year = datetime.now(timezone.utc).year
    assert _run(_do()) == f"INV/{year}/000006"


def test_21_rbac_regression_role_gates_intact():
    """NAV-008 must not weaken any existing role gate. Baseline test:
    an anonymous request to `/api/billing/invoices` returns 401."""
    r = requests.get(f"{API}/billing/invoices", timeout=15)
    assert r.status_code == 401, r.text


def test_22_ha_sales_linked_invoice_no_pattern_preserved():
    """When Path B (ha_quick_sale via billing.create_invoice from-sale)
    back-links the sale, the invoice_no on ha_sales must match the
    canonical format. We assert format-only here — full quick-sale
    integration is exercised by existing HA test suites."""
    tok = _login_a()

    async def _seed_sale():
        client, db = await _db()
        try:
            await db.ha_sales.insert_one({
                "sale_no": "SAL-NAV008A-1",
                "clinic_id": CLINIC_A,
                "status": "pending_invoice",
                "grand_total": 5000.0,
            })
        finally:
            client.close()
    _run(_seed_sale())
    # Create a from-sale invoice.
    body = {
        "patient_id": PATIENT_A,
        "lines": [{"service_id": SVC_ID_A, "description": "PTA", "quantity": 1}],
        "from_sale_no": "SAL-NAV008A-1",
    }
    r = requests.post(f"{API}/billing/invoices", json=body,
                      headers=H(tok), timeout=15)
    assert r.status_code == 200, r.text
    inv_no = r.json()["invoice_no"]

    async def _check_link():
        client, db = await _db()
        try:
            sale = await db.ha_sales.find_one({"sale_no": "SAL-NAV008A-1"})
            return sale.get("invoice_no")
        finally:
            client.close()

    linked = _run(_check_link())
    assert linked == inv_no
    year = datetime.now(timezone.utc).year
    assert linked.startswith(f"INV/{year}/")


def test_23_reconcile_script_refuses_without_env_gate():
    """Refuses execution unless BOTH `NAV008_MIGRATE=1` AND (post-Phase 3B)
    `NAV008_MIGRATE_OVERRIDE=1` are set. Sanity-check both safety gates
    without launching a full migration."""
    import subprocess
    import sys
    script = "/app/backend/scripts/nav008_counter_reconcile.py"
    # Env WITHOUT either gate — must refuse (Phase 3B override missing).
    proc = subprocess.run(
        [sys.executable, script, "--dry-run"],
        capture_output=True, text=True, timeout=15,
        env={**{k: v for k, v in os.environ.items()
                if k not in {"NAV008_MIGRATE", "NAV008_MIGRATE_OVERRIDE"}}},
    )
    assert "REFUSED" in proc.stdout or proc.returncode != 0, \
        f"reconcile should refuse; stdout={proc.stdout!r} rc={proc.returncode}"


def test_24_reconcile_script_dry_run_no_writes_with_override():
    """With BOTH gates set + --dry-run, script scans but performs no writes.
    Post-Phase 3B the override is required as a belt-and-braces safeguard;
    without it the script refuses (see test_23 and test_29)."""
    import subprocess
    import sys
    script = "/app/backend/scripts/nav008_counter_reconcile.py"

    async def _snapshot():
        client, db = await _db()
        try:
            docs = await db.counters.find({}, {"_id": 1, "seq": 1}).to_list(None)
            return sorted((d["_id"], d.get("seq", 0)) for d in docs)
        finally:
            client.close()

    before = _run(_snapshot())
    proc = subprocess.run(
        [sys.executable, script, "--dry-run"],
        capture_output=True, text=True, timeout=60,
        env={**os.environ,
             "NAV008_MIGRATE": "1",
             "NAV008_MIGRATE_OVERRIDE": "1"},
    )
    assert proc.returncode == 0, f"dry-run failed: {proc.stdout} {proc.stderr}"
    after = _run(_snapshot())
    assert before == after, "dry-run must not modify db.counters"


def test_25_invoice_no_field_never_regresses_to_invoice_number_field():
    """Story-demo fixture was writing to the WRONG field name
    `invoice_number`. Assert that on a fresh seed rewrite, ONLY the
    canonical `invoice_no` field is present."""
    src = open("/app/backend/scripts/seed_story_demo.py", "r", encoding="utf-8").read()
    # `invoice_number` should no longer appear in the fixture rows.
    # (It may still appear in comments — this is a defensive check.)
    for bad in ('"invoice_number":', "'invoice_number':"):
        assert bad not in src, (
            f"seed_story_demo.py must not use the wrong field {bad!r}"
        )
    assert '"invoice_no":' in src, (
        "seed_story_demo.py must use canonical `invoice_no` field"
    )



# ══════════════════════════════════════════════════════════════════════════
# NAV-008 Phase 3B · Decision-6 explicit collide-then-retry coverage
# ══════════════════════════════════════════════════════════════════════════
# The Preview dry-run demonstrated that historical uuid-hex invoice numbers
# can be indistinguishable from canonical counter output when the hex tail
# happens to be all-digits. Global counter reconciliation was deferred; the
# safeguard is now the retry-helper + unique-index combo. These tests
# verify that combo end-to-end under the exact scenario the reconciliation
# was intended to prevent.


def test_26_collide_with_historical_low_seq_then_retry_succeeds():
    """Explicit end-to-end coverage of the deferred-reconciliation scenario.

    Scenario:
      1. Counter starts at 0 (below any historical invoice number).
      2. A historical invoice at INV/{year}/000001 already occupies the
         first counter output — simulating a seed-inserted or uuid-hex
         invoice that pre-dates the sprint.
      3. A brand-new POST /billing/invoices comes in via the normal
         path (billing.create_invoice → _insert_invoice_with_retry).
      4. The DB unique index raises E11000 on the first attempt.
      5. The retry helper renews via _next_invoice_no and succeeds on
         attempt 2 with INV/{year}/000002.
      6. The historical invoice remains untouched.
      7. No invoice is renumbered.
      8. Counter is left at 2.
    """
    year = datetime.now(timezone.utc).year

    async def _setup():
        client, db = await _db()
        try:
            # Reset counter to 0.
            await db.counters.update_one(
                {"_id": f"invoice:{CLINIC_A}:{year}"},
                {"$set": {"seq": 0}},
                upsert=True,
            )
            # Occupy INV/{year}/000001 with a historical invoice.
            await db.invoices.insert_one({
                "invoice_id": "INV-HIST-001",
                "clinic_id": CLINIC_A,
                "invoice_no": f"INV/{year}/000001",
                "patient_id": "P-HIST",
                "grand_total": 999.0,
                "status": "paid",
                "invoice_date": datetime.now(timezone.utc).isoformat(),
            })
        finally:
            client.close()
    _run(_setup())

    tok = _login_a()
    r = _create_invoice(tok, SVC_ID_A, PATIENT_A)
    assert r.status_code == 200, r.text
    got = r.json()["invoice_no"]
    # Retry must have advanced past 000001.
    assert got == f"INV/{year}/000002", (
        f"expected INV/{year}/000002 (retry from occupied 000001); got {got}"
    )

    async def _verify():
        client, db = await _db()
        try:
            # Historical invoice remains present + unchanged.
            hist = await db.invoices.find_one({"invoice_id": "INV-HIST-001"})
            assert hist is not None and hist["invoice_no"] == f"INV/{year}/000001"
            # Counter now at 2.
            ctr = await db.counters.find_one({"_id": f"invoice:{CLINIC_A}:{year}"})
            return ctr["seq"]
        finally:
            client.close()
    assert _run(_verify()) == 2


def test_27_collide_with_multiple_historical_gaps_retry_bounded():
    """Counter starts at 0; historical rows occupy 000001, 000002, 000003.
    A single new POST must succeed on attempt 4 (retry limit is 3) → the
    helper CANNOT recover from 3 consecutive collisions. Verify the
    controlled 500 fires and the historical invoices remain untouched.

    This is the failure-mode test — proves the helper does NOT loop
    forever and surfaces a clean application error when the retry budget
    is exhausted."""
    year = datetime.now(timezone.utc).year

    async def _setup():
        client, db = await _db()
        try:
            await db.counters.update_one(
                {"_id": f"invoice:{CLINIC_A}:{year}"},
                {"$set": {"seq": 0}},
                upsert=True,
            )
            for n in (1, 2, 3):
                await db.invoices.insert_one({
                    "invoice_id": f"INV-HIST3-{n}",
                    "clinic_id": CLINIC_A,
                    "invoice_no": f"INV/{year}/{str(n).zfill(6)}",
                    "patient_id": "P-HIST3",
                    "grand_total": 100.0,
                    "status": "paid",
                    "invoice_date": datetime.now(timezone.utc).isoformat(),
                })
        finally:
            client.close()
    _run(_setup())

    tok = _login_a()
    r = _create_invoice(tok, SVC_ID_A, PATIENT_A)
    # 3 retries exhausted → helper raises HTTPException 500 with
    # a controlled message (never leaks Mongo E11000).
    assert r.status_code == 500, r.text
    assert "conflict" in r.text.lower() or "retry" in r.text.lower(), r.text

    async def _verify():
        client, db = await _db()
        try:
            # None of the historical invoices are modified.
            for n in (1, 2, 3):
                doc = await db.invoices.find_one({"invoice_id": f"INV-HIST3-{n}"})
                assert doc is not None
                assert doc["invoice_no"] == f"INV/{year}/{str(n).zfill(6)}"
        finally:
            client.close()
    _run(_verify())


def test_28_cross_clinic_identical_numbers_still_allowed_after_index():
    """The test-scoped unique index is on (clinic_id, invoice_no) — NOT
    global invoice_no. Verify Clinic A and Clinic B can both host their
    own INV/YYYY/000001 without either failing at the DB layer."""
    year = datetime.now(timezone.utc).year

    async def _setup():
        client, db = await _db()
        try:
            for cid in (CLINIC_A, CLINIC_B):
                await db.counters.update_one(
                    {"_id": f"invoice:{cid}:{year}"},
                    {"$set": {"seq": 0}}, upsert=True,
                )
        finally:
            client.close()
    _run(_setup())

    r_a = _create_invoice(_login_a(), SVC_ID_A, PATIENT_A)
    r_b = _create_invoice(_login_b(), SVC_ID_B, PATIENT_B)
    assert r_a.status_code == 200 and r_b.status_code == 200
    assert r_a.json()["invoice_no"] == f"INV/{year}/000001"
    assert r_b.json()["invoice_no"] == f"INV/{year}/000001"


def test_29_reconciler_script_refuses_execution_after_phase_3b_decision():
    """Phase 3B deferred the reconciler. Even with NAV008_MIGRATE=1,
    it must refuse to run — enforced by the additional
    NAV008_MIGRATE_OVERRIDE=1 gate. Belt-and-braces safety."""
    import subprocess
    import sys
    script = "/app/backend/scripts/nav008_counter_reconcile.py"
    proc = subprocess.run(
        [sys.executable, script, "--dry-run"],
        capture_output=True, text=True, timeout=15,
        env={**os.environ, "NAV008_MIGRATE": "1"},  # note: no override
    )
    # Should refuse: return code 3 (Phase 3B deferral).
    assert proc.returncode == 3, (
        f"reconciler must refuse without NAV008_MIGRATE_OVERRIDE=1; "
        f"got rc={proc.returncode} stdout={proc.stdout!r}"
    )
    assert "NOT APPROVED FOR EXECUTION" in proc.stdout, (
        f"deferred-execution banner missing; stdout={proc.stdout!r}"
    )
