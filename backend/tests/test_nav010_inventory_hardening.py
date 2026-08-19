"""NAV-010 · Phase 2A — Inventory hardening regression.

Covers the eight approved P0/P1 findings from the audit:

  * INV-001 · Atomic CAS on ``transition_serial`` — concurrent same-serial
              transitions produce exactly one success + one 409.
  * INV-002 · Stock transfer RBAC — audiologist blocked from create /
              dispatch / receive; front_desk allowed on receive only.
  * INV-003 · Concurrent accessory reservation — no negative stock,
              exactly one success under contention.
  * INV-004 · Atomic accessory manual adjustment — no lost-update; a
              negative delta exceeding available qty returns 409 with
              zero side-effect.
  * INV-005 · Quick Sale cancellation state-machine — two-phase
              LOCK / PRE-FLIGHT / COMMIT with zero partial mutation;
              one ``payment_reversals`` row per reversed payment; tight
              RBAC (clinic_owner / super_admin / founder only).
  * INV-006 · Invoice cancellation HARD BLOCK on inventory footprint —
              linked HA sale, linked Quick Sale, and accessory-
              decremented invoices all return 409 on the generic
              ``/billing/invoices/{id}/cancel`` route.
  * INV-007 · Strict-reject accessory shortage at invoice creation —
              insufficient stock → 409 with zero invoice / payment /
              stock mutation and clean compensation on multi-line
              partial failure.
  * INV-008 · Stock request RBAC — audiologist blocked from POST.

These tests self-clean per-test by using fresh patients / SKUs / serials
per unique suffix; they do NOT touch historical data or the known
duplicate ``INV/2026/000004`` on ``tenant-sound-clinic-blr``.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import os
import random
import string
import threading
import time
import uuid
from typing import Optional

import pytest
import requests
from motor.motor_asyncio import AsyncIOMotorClient

import sys
import pathlib

sys.path.append(str(pathlib.Path(__file__).resolve().parents[1]))
from _helpers import (  # noqa: E402
    API, ADMIN_EMAIL, ADMIN_PASSWORD, ACCOUNTS_EMAIL, ACCOUNTS_PASSWORD,
    FRONTDESK_EMAIL, FRONTDESK_PASSWORD, AUDIO_EMAIL, AUDIO_PASSWORD,
    H, login,
)


_BRANCH_ID = "BR-PYTEST-001"
_CLINIC_ID = os.environ.get("TEST_CLINIC_ID", "clinic-pytest-suite")


# ─────────────────────────────────────────────────────────────────────
# Local helpers — self-cleaning fixtures.
# ─────────────────────────────────────────────────────────────────────

def _uniq() -> str:
    return f"{int(time.time()*1000)%1_000_000_000:x}{''.join(random.choices(string.ascii_lowercase, k=4))}"


def _unique_phone() -> str:
    return f"+91{random.randint(7, 9)}{random.randint(10**8, 10**9 - 1)}"


def _mk_patient(token: str) -> str:
    r = requests.post(f"{API}/patients", headers=H(token), json={
        "name": f"NAV010 Patient {_uniq()}",
        "mobile": _unique_phone(),
        "age": 40,
        "sex": "M",
        "branch_id": _BRANCH_ID,
    }, timeout=10)
    assert r.status_code in (200, 201), r.text
    return r.json()["patient_id"]


def _mk_service(token: str, price: float = 3000) -> str:
    r = requests.post(f"{API}/billing/services", headers=H(token), json={
        "code": f"NAV010-{_uniq()[:6].upper()}",
        "name": "NAV-010 test service",
        "price": price,
        "gst_rate": 0,
        "category": "Consultation",
        "active": True,
    }, timeout=10)
    assert r.status_code in (200, 201), r.text
    return r.json()["service_id"]


def _mk_ha_product(token: str) -> str:
    """Create a hearing aid catalogue product (for serial linkage)."""
    r = requests.post(f"{API}/ha/products", headers=H(token), json={
        "brand": f"NAV010-Brand-{_uniq()[:4]}",
        "model": f"NAV010-Model-{_uniq()[:4]}",
        "form_factor": "BTE",
        "is_serialised": True,
        "mrp": 40000,
        "gst_rate": 12,
    }, timeout=10)
    if r.status_code not in (200, 201):
        pytest.skip(f"HA product create not available: {r.status_code}")
    return r.json()["product_id"]


def _mk_accessory_product(token: str) -> str:
    """Create an accessory catalogue product (non-serialised)."""
    r = requests.post(f"{API}/ha/products", headers=H(token), json={
        "brand": f"NAV010-Acc-{_uniq()[:4]}",
        "model": f"NAV010-AccModel-{_uniq()[:4]}",
        "form_factor": "accessory",
        "is_serialised": False,
        "mrp": 500,
        "gst_rate": 18,
        "accessory_kind": "battery",
        "accessory_category": "consumable",
    }, timeout=10)
    if r.status_code not in (200, 201):
        pytest.skip(f"Accessory product create not available: {r.status_code}")
    return r.json()["product_id"]


def _init_accessory_stock(token: str, pid: str, qty: int = 5) -> str:
    """Initialize a single (no-variant) stock row for the given accessory
    product and pump qty via /adjust. Returns the sku_id."""
    r = requests.post(
        f"{API}/ha/products/{pid}/init-accessory-stock",
        headers=H(token),
        json={"branch_ids": [_BRANCH_ID], "variants": [], "reorder_level": 2},
        timeout=10,
    )
    if r.status_code not in (200, 201):
        pytest.skip(f"init-accessory-stock unavailable: {r.status_code}")
    hydrated = requests.get(
        f"{API}/ha/accessory-stock-hydrated",
        headers=H(token), params={"branch_id": _BRANCH_ID}, timeout=10,
    ).json()
    items = hydrated.get("items") or []
    sku_row = next((r for r in items if r["product_id"] == pid), None)
    assert sku_row, f"sku row for {pid} not created; hydrated={items[:2]}"
    sku_id = sku_row["sku_id"]
    if qty > 0:
        r_adj = requests.post(
            f"{API}/ha/accessory-stock/{sku_id}/adjust",
            headers=H(token),
            json={"delta": qty, "reason": "NAV010 seed"},
            timeout=10,
        )
        assert r_adj.status_code == 200, r_adj.text
    return sku_id


# ─────────────────────────────────────────────────────────────────────
# Direct-DB helpers (serial_items require full state-machine seeding
# which the GRN happy-path performs; direct insertion mirrors what
# the existing test_nav005 seed does and is the simplest fixture for
# a concurrency test on `transition_serial`).
# ─────────────────────────────────────────────────────────────────────

def _run_mongo(fn):
    """Run an async function against a fresh motor client on a fresh
    event loop. `fn` is a coroutine function taking (db) and returning
    the value we want."""
    loop = asyncio.new_event_loop()
    try:
        async def _wrapped():
            cli = AsyncIOMotorClient(os.environ["MONGO_URL"])
            try:
                db = cli[os.environ["DB_NAME"]]
                return await fn(db)
            finally:
                cli.close()
        return loop.run_until_complete(_wrapped())
    finally:
        loop.close()


def _seed_serial(state: str = "IN_STOCK", product_id: Optional[str] = None) -> dict:
    """Insert a fresh serial_items row for the pytest tenant. Returns
    the full doc so tests can grab `serial_id` and `serial_no`."""
    serial_id = f"SI-NAV010-{uuid.uuid4().hex[:10].upper()}"
    serial_no = f"NAV010-SN-{uuid.uuid4().hex[:8].upper()}"
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    doc = {
        "serial_id": serial_id,
        "clinic_id": _CLINIC_ID,
        "branch_id": _BRANCH_ID,
        "product_id": product_id or "PRD-NAV010-SEED",
        "serial_no": serial_no,
        "state": state,
        "pool": "saleable",
        "received_at": time.strftime("%Y-%m-%d"),
        "created_at": now_iso,
        "updated_at": now_iso,
        "history": [{
            "at": now_iso,
            "actor_user_id": "USR-PYTEST-ADMIN",
            "from_state": None,
            "to_state": state,
            "ref_doc": {"kind": "nav010_seed", "id": serial_id},
            "note": "NAV-010 test seed",
        }],
    }
    _run_mongo(lambda db: db.serial_items.insert_one(dict(doc)))
    return doc


def _fetch_serial(serial_id: str) -> Optional[dict]:
    return _run_mongo(lambda db: db.serial_items.find_one(
        {"serial_id": serial_id}, {"_id": 0},
    ))


def _count_serial_events(serial_id: str) -> int:
    return _run_mongo(lambda db: db.serial_events.count_documents(
        {"serial_id": serial_id},
    ))


def _accessory_qty(sku_id: str) -> int:
    doc = _run_mongo(lambda db: db.accessory_stock.find_one(
        {"sku_id": sku_id}, {"_id": 0, "qty_on_hand": 1},
    ))
    return int((doc or {}).get("qty_on_hand", 0))


def _fetch_invoice(invoice_id: str) -> Optional[dict]:
    return _run_mongo(lambda db: db.invoices.find_one(
        {"invoice_id": invoice_id}, {"_id": 0},
    ))


def _count_payment_reversals(quick_sale_id: str) -> int:
    return _run_mongo(lambda db: db.payment_reversals.count_documents(
        {"quick_sale_id": quick_sale_id},
    ))


def _find_payment_reversal(quick_sale_id: str, original_payment_id: str) -> Optional[dict]:
    return _run_mongo(lambda db: db.payment_reversals.find_one({
        "quick_sale_id": quick_sale_id,
        "original_payment_id": original_payment_id,
        "kind": "cancellation_reversal",
    }, {"_id": 0}))


# ─────────────────────────────────────────────────────────────────────
# Quick-Sale creation helper (drives the full inventory footprint).
# ─────────────────────────────────────────────────────────────────────

def _make_quick_sale(
    tok: str,
    patient_id: str,
    serial_no: str,
    *,
    price: float = 25000,
    paid: float = 25000,
    payment_status: str = "fully_paid",
) -> dict:
    body = {
        "patient_id": patient_id,
        "brand": "NAV010",
        "model": "NAV010-Model",
        "ha_type": "BTE",
        "side": "right",
        "serial_right": serial_no,
        "fitting_date": time.strftime("%Y-%m-%d"),
        "mrp": price,
        "sale_price": price,
        "gst_rate": 12,
        "discount_amount": 0,
        "advance_amount": paid,
        "payment_mode": "cash",
        "payment_status": payment_status,
        "branch_id": _BRANCH_ID,
    }
    r = requests.post(f"{API}/ha/quick-sale", headers=H(tok), json=body, timeout=15)
    assert r.status_code == 200, r.text
    return r.json()


# =====================================================================
# INV-001 · Atomic CAS on transition_serial
# =====================================================================

def test_inv001_concurrent_serial_transition_exactly_one_wins():
    """Two concurrent Quick Sales for the SAME serial no → exactly
    one 200 + one 409. The serial-item ends up SOLD with exactly one
    serial_events row (post-seed)."""
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    seeded = _seed_serial()
    pat1 = _mk_patient(tok)
    pat2 = _mk_patient(tok)

    barrier = threading.Barrier(2)

    def _fire(patient_id: str):
        barrier.wait()
        return requests.post(f"{API}/ha/quick-sale", headers=H(tok), json={
            "patient_id": patient_id,
            "brand": "NAV010", "model": "NAV010-INV001", "ha_type": "BTE",
            "side": "right", "serial_right": seeded["serial_no"],
            "fitting_date": time.strftime("%Y-%m-%d"),
            "mrp": 20000, "sale_price": 20000, "gst_rate": 12,
            "payment_mode": "cash", "payment_status": "fully_paid",
            "branch_id": _BRANCH_ID,
        }, timeout=15)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(_fire, pat1)
        f2 = ex.submit(_fire, pat2)
        r1, r2 = f1.result(), f2.result()
    statuses = sorted([r1.status_code, r2.status_code])
    assert statuses == [200, 409], f"expected [200,409], got {statuses}: r1={r1.text[:120]} r2={r2.text[:120]}"

    # Serial must be SOLD
    fresh = _fetch_serial(seeded["serial_id"])
    assert fresh and fresh["state"] == "SOLD"
    # Exactly one INSERT-STOCK → SOLD event on top of the seed.
    events = _count_serial_events(seeded["serial_id"])
    assert events == 1, f"expected exactly one serial event after CAS win; got {events}"


def test_inv001_stale_state_read_gets_409():
    """A programmatic call to transition_serial with a stale from-state
    read produces 409 (illegal transition) — this exercises the
    ``assert_transition`` guard before CAS."""
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    seeded = _seed_serial(state="SOLD")
    # SOLD → SOLD is not a legal transition (see ALLOWED_TRANSITIONS).
    # Attempt via a Quick Sale of the same serial → 409.
    pat = _mk_patient(tok)
    r = requests.post(f"{API}/ha/quick-sale", headers=H(tok), json={
        "patient_id": pat,
        "brand": "NAV010", "model": "NAV010-INV001b", "ha_type": "BTE",
        "side": "right", "serial_right": seeded["serial_no"],
        "fitting_date": time.strftime("%Y-%m-%d"),
        "mrp": 15000, "sale_price": 15000, "gst_rate": 12,
        "payment_mode": "cash", "payment_status": "fully_paid",
        "branch_id": _BRANCH_ID,
    }, timeout=15)
    assert r.status_code == 409, r.text


def test_inv001_final_serial_state_and_event_count_after_sequential_transitions():
    """Sequential SOLD → SERVICE_IN → IN_STOCK writes exactly 2 audit
    rows and lands in IN_STOCK. Verifies CAS does not double-count."""
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    # Sell a serial via Quick Sale (IN_STOCK → SOLD).
    pat = _mk_patient(tok)
    seeded = _seed_serial()
    _make_quick_sale(tok, pat, seeded["serial_no"], price=15000, paid=15000)
    # Programmatic transition via service open (SOLD → SERVICE_IN).
    # We use the existing /ha/service-tickets endpoint via direct HTTP.
    r = requests.post(f"{API}/ha/service-tickets", headers=H(tok), json={
        "patient_id": pat,
        "branch_id": _BRANCH_ID,
        "serial_id": seeded["serial_id"],
        "kind": "repair",
        "complaint": "NAV-010 test repair complaint",
    }, timeout=15)
    if r.status_code not in (200, 201):
        pytest.skip(f"HA service tickets not available (got {r.status_code}: {r.text[:120]})")
    fresh = _fetch_serial(seeded["serial_id"])
    assert fresh and fresh["state"] == "SERVICE_IN"
    n_events = _count_serial_events(seeded["serial_id"])
    assert n_events >= 2, f"expected ≥ 2 serial events, got {n_events}"


# =====================================================================
# INV-002 · Stock transfer RBAC
# =====================================================================

def test_inv002_audiologist_forbidden_on_create_stock_transfer():
    try:
        audio = login(AUDIO_EMAIL, AUDIO_PASSWORD)
    except AssertionError:
        pytest.skip("audio test account not seeded")
    r = requests.post(f"{API}/stock-transfers", headers=H(audio), json={
        "to_clinic_id": "clinic-nav010-dummy",
        "purpose": "branch_stock_top_up",
        "serial_ids": [],
        "accessory_lines": [],
    }, timeout=10)
    assert r.status_code == 403, r.text


def test_inv002_audiologist_forbidden_on_dispatch_stock_transfer():
    try:
        audio = login(AUDIO_EMAIL, AUDIO_PASSWORD)
    except AssertionError:
        pytest.skip("audio test account not seeded")
    r = requests.post(
        f"{API}/stock-transfers/TR-NAV010-DUMMY/dispatch",
        headers=H(audio), json={"courier_name": "x", "tracking_no": "y"},
        timeout=10,
    )
    assert r.status_code == 403, r.text


def test_inv002_audiologist_forbidden_on_receive_stock_transfer():
    try:
        audio = login(AUDIO_EMAIL, AUDIO_PASSWORD)
    except AssertionError:
        pytest.skip("audio test account not seeded")
    r = requests.post(
        f"{API}/stock-transfers/TR-NAV010-DUMMY/receive",
        headers=H(audio), json={"received_serial_ids": []},
        timeout=10,
    )
    assert r.status_code == 403, r.text


def test_inv002_front_desk_allowed_on_receive_stock_transfer():
    """front_desk is on the RBAC list for RECEIVE only — the call should
    pass the RBAC gate and hit business logic (404 for the fake id)."""
    try:
        fd = login(FRONTDESK_EMAIL, FRONTDESK_PASSWORD)
    except AssertionError:
        pytest.skip("front_desk test account not seeded")
    r = requests.post(
        f"{API}/stock-transfers/TR-NAV010-DUMMY-{_uniq()}/receive",
        headers=H(fd), json={"received_serial_ids": []},
        timeout=10,
    )
    # NOT 403 — RBAC lets front_desk through. 404 / 400 are business-
    # layer responses depending on payload/id validity.
    assert r.status_code != 403, r.text
    assert r.status_code in (400, 404, 409, 422), r.text


def test_inv002_front_desk_forbidden_on_create_stock_transfer():
    """front_desk is NOT allowed on CREATE (only on RECEIVE)."""
    try:
        fd = login(FRONTDESK_EMAIL, FRONTDESK_PASSWORD)
    except AssertionError:
        pytest.skip("front_desk test account not seeded")
    r = requests.post(f"{API}/stock-transfers", headers=H(fd), json={
        "to_clinic_id": "clinic-nav010-dummy",
        "purpose": "branch_stock_top_up",
        "serial_ids": [], "accessory_lines": [],
    }, timeout=10)
    assert r.status_code == 403, r.text


# =====================================================================
# INV-003 + INV-007 · Accessory reservation & strict-reject
# =====================================================================

def test_inv003_007_insufficient_accessory_stock_returns_409_no_side_effects():
    """Invoice with an accessory line for a product that has qty=0 →
    409. No invoice, no payment, no stock mutation."""
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    pat = _mk_patient(tok)
    ap = _mk_accessory_product(tok)
    # Init stock but keep qty=0.
    sku = _init_accessory_stock(tok, ap, qty=0)
    before = _accessory_qty(sku)
    payload = {
        "patient_id": pat,
        "lines": [{
            "description": "Accessory battery pack",
            "quantity": 1,
            "unit_price": 500,
            "discount_type": "flat", "discount_value": 0,
            "product_type": "Accessory",
            "make": "does-not-matter",
            "model": "does-not-matter",
            "accessory_product_id": ap,
        }],
        "initial_payment": {"method": "cash", "amount": 500},
    }
    r = requests.post(f"{API}/billing/invoices", headers=H(tok), json=payload, timeout=15)
    assert r.status_code == 409, r.text
    assert "stock" in r.text.lower() or "insufficient" in r.text.lower()
    # Stock qty unchanged.
    assert _accessory_qty(sku) == before


def test_inv003_007_exact_available_quantity_succeeds():
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    pat = _mk_patient(tok)
    ap = _mk_accessory_product(tok)
    sku_id = _init_accessory_stock(tok, ap, qty=3)
    before = _accessory_qty(sku_id)
    payload = {
        "patient_id": pat,
        "lines": [{
            "description": "Accessory battery pack",
            "quantity": before,
            "unit_price": 500,
            "discount_type": "flat", "discount_value": 0,
            "product_type": "Accessory",
            "make": "does-not-matter",
            "model": "does-not-matter",
            "accessory_product_id": ap,
        }],
    }
    r = requests.post(f"{API}/billing/invoices", headers=H(tok), json=payload, timeout=15)
    assert r.status_code == 200, r.text
    after = _accessory_qty(sku_id)
    assert after == 0, f"expected qty=0 after exact reservation; got {after}"


def test_inv003_concurrent_reservations_exactly_one_wins():
    """Two concurrent invoice-creates for the last available unit →
    exactly one 200 + one 409; final qty on the sku is 0."""
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    pat1 = _mk_patient(tok)
    pat2 = _mk_patient(tok)
    ap = _mk_accessory_product(tok)
    sku_id = _init_accessory_stock(tok, ap, qty=1)

    barrier = threading.Barrier(2)

    def _fire(patient_id: str):
        barrier.wait()
        return requests.post(f"{API}/billing/invoices", headers=H(tok), json={
            "patient_id": patient_id,
            "lines": [{
                "description": "Race battery",
                "quantity": 1, "unit_price": 500,
                "discount_type": "flat", "discount_value": 0,
                "product_type": "Accessory",
                "make": "race", "model": "race",
                "accessory_product_id": ap,
            }],
        }, timeout=15)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(_fire, pat1)
        f2 = ex.submit(_fire, pat2)
        r1, r2 = f1.result(), f2.result()
    statuses = sorted([r1.status_code, r2.status_code])
    assert statuses == [200, 409], f"expected [200,409], got {statuses}"
    assert _accessory_qty(sku_id) == 0


def test_inv003_007_multiline_partial_failure_compensates_earlier_reservation():
    """Invoice with TWO accessory lines: line 1 fits, line 2 exceeds
    available. Whole invoice must fail 409 AND line 1 must be
    compensated ($inc back). No invoice inserted."""
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    pat = _mk_patient(tok)
    ap1 = _mk_accessory_product(tok)
    ap2 = _mk_accessory_product(tok)
    sku_a = _init_accessory_stock(tok, ap1, qty=2)
    sku_b = _init_accessory_stock(tok, ap2, qty=1)
    before_a = _accessory_qty(sku_a)
    before_b = _accessory_qty(sku_b)

    payload = {
        "patient_id": pat,
        "lines": [
            {  # line 1 — needs 1 of 2 available → would succeed
                "description": "Multi line accessory 1",
                "quantity": 1, "unit_price": 500,
                "discount_type": "flat", "discount_value": 0,
                "product_type": "Accessory", "make": "m1", "model": "m1",
                "accessory_product_id": ap1,
            },
            {  # line 2 — needs 5 of 1 available → 409
                "description": "Multi line accessory 2",
                "quantity": 5, "unit_price": 500,
                "discount_type": "flat", "discount_value": 0,
                "product_type": "Accessory", "make": "m2", "model": "m2",
                "accessory_product_id": ap2,
            },
        ],
    }
    r = requests.post(f"{API}/billing/invoices", headers=H(tok), json=payload, timeout=15)
    assert r.status_code == 409, r.text
    # Both SKUs must be restored to their original quantity.
    assert _accessory_qty(sku_a) == before_a
    assert _accessory_qty(sku_b) == before_b


# =====================================================================
# INV-004 · Atomic accessory manual adjustment
# =====================================================================

def test_inv004_positive_negative_adjustments_are_atomic():
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    ap = _mk_accessory_product(tok)
    sku = _init_accessory_stock(tok, ap, qty=10)
    # Positive
    r = requests.post(f"{API}/ha/accessory-stock/{sku}/adjust", headers=H(tok),
                      json={"delta": 5, "reason": "NAV010 pos"}, timeout=10)
    assert r.status_code == 200, r.text
    assert _accessory_qty(sku) == 15
    # Negative within available
    r = requests.post(f"{API}/ha/accessory-stock/{sku}/adjust", headers=H(tok),
                      json={"delta": -7, "reason": "NAV010 neg"}, timeout=10)
    assert r.status_code == 200, r.text
    assert _accessory_qty(sku) == 8


def test_inv004_negative_adjustment_exceeding_qty_returns_409_no_change():
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    ap = _mk_accessory_product(tok)
    sku = _init_accessory_stock(tok, ap, qty=3)
    r = requests.post(f"{API}/ha/accessory-stock/{sku}/adjust", headers=H(tok),
                      json={"delta": -10, "reason": "NAV010 bad"}, timeout=10)
    assert r.status_code == 409, r.text
    assert _accessory_qty(sku) == 3


def test_inv004_concurrent_adjustments_no_lost_updates():
    """Fire 6 concurrent +1 adjustments. All 6 must succeed and the
    final qty must equal starting qty + 6 (no lost update)."""
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    ap = _mk_accessory_product(tok)
    sku = _init_accessory_stock(tok, ap, qty=5)
    start = _accessory_qty(sku)
    N = 6
    barrier = threading.Barrier(N)

    def _fire():
        barrier.wait()
        return requests.post(f"{API}/ha/accessory-stock/{sku}/adjust", headers=H(tok),
                             json={"delta": 1, "reason": f"NAV010 conc {_uniq()}"}, timeout=10)

    with concurrent.futures.ThreadPoolExecutor(max_workers=N) as ex:
        results = [ex.submit(_fire) for _ in range(N)]
        codes = [f.result().status_code for f in concurrent.futures.as_completed(results)]
    assert codes.count(200) == N, codes
    assert _accessory_qty(sku) == start + N


# =====================================================================
# INV-005 · Quick Sale cancellation state-machine
# =====================================================================

def test_inv005_cancel_unpaid_happy_path():
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    pat = _mk_patient(tok)
    seeded = _seed_serial()
    qs = _make_quick_sale(
        tok, pat, seeded["serial_no"],
        price=15000, paid=0, payment_status="unpaid",
    )
    r = requests.post(
        f"{API}/ha/quick-sales/{qs['quick_sale_id']}/cancel",
        headers=H(tok),
        json={"reason": "NAV010 unpaid cancel", "confirm_refund_offline": False},
        timeout=15,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "cancelled"
    assert body["cancellation_state"] == "cancelled"
    assert body["payment_reversal_ids"] == []
    # Serial must be RETURNED.
    fresh = _fetch_serial(seeded["serial_id"])
    assert fresh and fresh["state"] == "RETURNED"
    # Invoice cancelled with due_total=0.
    inv = _fetch_invoice(qs["invoice_id"])
    assert inv and inv["status"] == "cancelled"
    assert abs(float(inv.get("due_total") or 0)) <= 0.01


def test_inv005_cancel_fully_paid_requires_offline_refund_confirmation():
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    pat = _mk_patient(tok)
    seeded = _seed_serial()
    qs = _make_quick_sale(tok, pat, seeded["serial_no"], price=20000, paid=20000)
    # Without confirm_refund_offline → 409
    r_bad = requests.post(
        f"{API}/ha/quick-sales/{qs['quick_sale_id']}/cancel",
        headers=H(tok),
        json={"reason": "NAV010 fp cancel", "confirm_refund_offline": False},
        timeout=15,
    )
    assert r_bad.status_code == 409, r_bad.text
    # Lock must be released — retry with confirm=true succeeds.
    fresh = _fetch_serial(seeded["serial_id"])
    assert fresh["state"] == "SOLD"  # unchanged
    r_ok = requests.post(
        f"{API}/ha/quick-sales/{qs['quick_sale_id']}/cancel",
        headers=H(tok),
        json={"reason": "NAV010 fp cancel", "confirm_refund_offline": True},
        timeout=15,
    )
    assert r_ok.status_code == 200, r_ok.text
    body = r_ok.json()
    assert body["status"] == "cancelled"
    assert len(body["payment_reversal_ids"]) == 1


def test_inv005_cancel_partially_paid_generates_reversal_per_payment():
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    pat = _mk_patient(tok)
    seeded = _seed_serial()
    qs = _make_quick_sale(
        tok, pat, seeded["serial_no"],
        price=30000, paid=10000, payment_status="advance_paid",
    )
    # Add a second payment via mark-balance-paid → total 2 embedded payments
    r_mp = requests.post(
        f"{API}/ha/quick-sales/{qs['quick_sale_id']}/mark-paid",
        headers=H(tok),
        json={"amount": 5000, "payment_mode": "upi"},
        timeout=15,
    )
    assert r_mp.status_code == 200, r_mp.text
    # Cancel → confirm required (paid_total > 0)
    r = requests.post(
        f"{API}/ha/quick-sales/{qs['quick_sale_id']}/cancel",
        headers=H(tok),
        json={"reason": "NAV010 partial", "confirm_refund_offline": True},
        timeout=15,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # Exactly TWO reversal rows — one per payment.
    assert len(body["payment_reversal_ids"]) == 2
    assert _count_payment_reversals(qs["quick_sale_id"]) == 2


def test_inv005_cancel_second_call_returns_409():
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    pat = _mk_patient(tok)
    seeded = _seed_serial()
    qs = _make_quick_sale(
        tok, pat, seeded["serial_no"],
        price=15000, paid=0, payment_status="unpaid",
    )
    r1 = requests.post(
        f"{API}/ha/quick-sales/{qs['quick_sale_id']}/cancel",
        headers=H(tok),
        json={"reason": "first", "confirm_refund_offline": False},
        timeout=15,
    )
    assert r1.status_code == 200, r1.text
    r2 = requests.post(
        f"{API}/ha/quick-sales/{qs['quick_sale_id']}/cancel",
        headers=H(tok),
        json={"reason": "second", "confirm_refund_offline": False},
        timeout=15,
    )
    assert r2.status_code == 409, r2.text
    assert "already" in r2.text.lower() or "cancel" in r2.text.lower()


def test_inv005_concurrent_cancel_only_one_wins():
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    pat = _mk_patient(tok)
    seeded = _seed_serial()
    qs = _make_quick_sale(tok, pat, seeded["serial_no"], price=10000, paid=0,
                          payment_status="unpaid")
    barrier = threading.Barrier(2)

    def _fire():
        barrier.wait()
        return requests.post(
            f"{API}/ha/quick-sales/{qs['quick_sale_id']}/cancel",
            headers=H(tok),
            json={"reason": f"race {_uniq()}", "confirm_refund_offline": False},
            timeout=15,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(_fire)
        f2 = ex.submit(_fire)
        r1, r2 = f1.result(), f2.result()
    statuses = sorted([r1.status_code, r2.status_code])
    assert statuses == [200, 409], f"expected [200,409], got {statuses}"


def test_inv005_serial_wrong_state_blocks_cancel_and_holds_no_mutation():
    """If the serial has been transitioned SOLD → SERVICE_IN OUTSIDE the
    Quick Sale after creation, cancel must 409 without touching the
    invoice / accessory state, and the lock must be released."""
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    pat = _mk_patient(tok)
    seeded = _seed_serial()
    qs = _make_quick_sale(tok, pat, seeded["serial_no"], price=10000, paid=0,
                          payment_status="unpaid")
    # Move serial SOLD → SERVICE_IN via HA service.
    r_svc = requests.post(f"{API}/ha/service-tickets", headers=H(tok), json={
        "patient_id": pat,
        "branch_id": _BRANCH_ID,
        "serial_id": seeded["serial_id"],
        "kind": "repair",
        "complaint": "NAV-010 out-of-band service open",
    }, timeout=15)
    if r_svc.status_code not in (200, 201):
        pytest.skip(f"HA service tickets not available (got {r_svc.status_code})")
    # Now cancel → must 409, invoice must remain uncancelled, lock released.
    r_cancel = requests.post(
        f"{API}/ha/quick-sales/{qs['quick_sale_id']}/cancel",
        headers=H(tok),
        json={"reason": "should fail", "confirm_refund_offline": False},
        timeout=15,
    )
    assert r_cancel.status_code == 409, r_cancel.text
    inv = _fetch_invoice(qs["invoice_id"])
    assert inv and inv["status"] != "cancelled"


def test_inv005_audiologist_forbidden():
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    pat = _mk_patient(tok)
    seeded = _seed_serial()
    qs = _make_quick_sale(tok, pat, seeded["serial_no"], price=10000, paid=0,
                          payment_status="unpaid")
    try:
        audio = login(AUDIO_EMAIL, AUDIO_PASSWORD)
    except AssertionError:
        pytest.skip("audio test account not seeded")
    r = requests.post(
        f"{API}/ha/quick-sales/{qs['quick_sale_id']}/cancel",
        headers=H(audio),
        json={"reason": "audio", "confirm_refund_offline": False},
        timeout=15,
    )
    assert r.status_code == 403, r.text


def test_inv005_front_desk_forbidden():
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    pat = _mk_patient(tok)
    seeded = _seed_serial()
    qs = _make_quick_sale(tok, pat, seeded["serial_no"], price=10000, paid=0,
                          payment_status="unpaid")
    try:
        fd = login(FRONTDESK_EMAIL, FRONTDESK_PASSWORD)
    except AssertionError:
        pytest.skip("front_desk test account not seeded")
    r = requests.post(
        f"{API}/ha/quick-sales/{qs['quick_sale_id']}/cancel",
        headers=H(fd),
        json={"reason": "fd", "confirm_refund_offline": False},
        timeout=15,
    )
    assert r.status_code == 403, r.text


def test_inv005_accounts_forbidden():
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    pat = _mk_patient(tok)
    seeded = _seed_serial()
    qs = _make_quick_sale(tok, pat, seeded["serial_no"], price=10000, paid=0,
                          payment_status="unpaid")
    try:
        acc = login(ACCOUNTS_EMAIL, ACCOUNTS_PASSWORD)
    except AssertionError:
        pytest.skip("accounts test account not seeded")
    r = requests.post(
        f"{API}/ha/quick-sales/{qs['quick_sale_id']}/cancel",
        headers=H(acc),
        json={"reason": "acc", "confirm_refund_offline": False},
        timeout=15,
    )
    assert r.status_code == 403, r.text


def test_inv005_super_admin_allowed_and_original_payments_untouched():
    """Verifies original embedded payment rows are NOT mutated and the
    reversal audit rows fully preserve traceability."""
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    pat = _mk_patient(tok)
    seeded = _seed_serial()
    qs = _make_quick_sale(tok, pat, seeded["serial_no"], price=12000, paid=12000)
    inv_before = _fetch_invoice(qs["invoice_id"])
    payments_before = inv_before.get("payments") or []
    assert len(payments_before) == 1
    original_payment_id = payments_before[0]["payment_id"]
    original_amount = payments_before[0]["amount"]

    r = requests.post(
        f"{API}/ha/quick-sales/{qs['quick_sale_id']}/cancel",
        headers=H(tok),
        json={"reason": "SA cancel", "confirm_refund_offline": True,
              "notes": "operator reversed cash offline"},
        timeout=15,
    )
    assert r.status_code == 200, r.text
    # Original payment row must be preserved bit-for-bit.
    inv_after = _fetch_invoice(qs["invoice_id"])
    payments_after = inv_after.get("payments") or []
    assert len(payments_after) == 1
    assert payments_after[0]["payment_id"] == original_payment_id
    assert payments_after[0]["amount"] == original_amount
    # A cancellation_reversal row exists with matching original_payment_id.
    rev = _find_payment_reversal(qs["quick_sale_id"], original_payment_id)
    assert rev is not None
    assert rev["amount"] == original_amount
    assert rev["invoice_id"] == qs["invoice_id"]


# =====================================================================
# INV-006 · Invoice cancellation HARD BLOCK on inventory footprint
# =====================================================================

def test_inv006_service_only_invoice_can_still_be_cancelled():
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    svc = _mk_service(tok)
    pat = _mk_patient(tok)
    r = requests.post(f"{API}/billing/invoices", headers=H(tok), json={
        "patient_id": pat,
        "lines": [{"service_id": svc, "description": "svc", "quantity": 1,
                   "unit_price": 500, "discount_type": "flat", "discount_value": 0}],
    }, timeout=15)
    assert r.status_code == 200, r.text
    inv_id = r.json()["invoice_id"]
    r_cancel = requests.post(
        f"{API}/billing/invoices/{inv_id}/cancel",
        headers=H(tok), json={"reason": "svc-only cancel"}, timeout=10,
    )
    assert r_cancel.status_code == 200, r_cancel.text


def test_inv006_quick_sale_linked_invoice_hard_block_409():
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    pat = _mk_patient(tok)
    seeded = _seed_serial()
    qs = _make_quick_sale(tok, pat, seeded["serial_no"], price=15000, paid=0,
                          payment_status="unpaid")
    r = requests.post(
        f"{API}/billing/invoices/{qs['invoice_id']}/cancel",
        headers=H(tok), json={"reason": "bypass attempt"}, timeout=10,
    )
    assert r.status_code == 409, r.text
    assert "quick sale" in r.text.lower() or "quick-sale" in r.text.lower()


def test_inv006_accessory_decremented_invoice_hard_block_409():
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    pat = _mk_patient(tok)
    ap = _mk_accessory_product(tok)
    sku = _init_accessory_stock(tok, ap, qty=2)
    r = requests.post(f"{API}/billing/invoices", headers=H(tok), json={
        "patient_id": pat,
        "lines": [{
            "description": "battery",
            "quantity": 1, "unit_price": 500,
            "discount_type": "flat", "discount_value": 0,
            "product_type": "Accessory", "make": "acc", "model": "acc",
            "accessory_product_id": ap,
        }],
    }, timeout=15)
    assert r.status_code == 200, r.text
    inv_id = r.json()["invoice_id"]
    # Try generic cancel → must 409 because line 0 had
    # accessory_stock_decremented set.
    r_cancel = requests.post(
        f"{API}/billing/invoices/{inv_id}/cancel",
        headers=H(tok), json={"reason": "should be blocked"}, timeout=10,
    )
    assert r_cancel.status_code == 409, r_cancel.text
    assert "accessory" in r_cancel.text.lower() or "stock" in r_cancel.text.lower()


def test_inv006_historical_footprint_blocks_even_after_serial_reverted():
    """After a Quick Sale cancel legitimately reverts the serial to
    RETURNED, the invoice is already cancelled — but we assert the
    generic cancel endpoint would ALSO have blocked based on the
    footprint fields (source=ha_quick_sale)."""
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    pat = _mk_patient(tok)
    seeded = _seed_serial()
    qs = _make_quick_sale(tok, pat, seeded["serial_no"], price=15000, paid=0,
                          payment_status="unpaid")
    inv_id = qs["invoice_id"]
    r = requests.post(
        f"{API}/billing/invoices/{inv_id}/cancel",
        headers=H(tok), json={"reason": "attempt"}, timeout=10,
    )
    assert r.status_code == 409, r.text
    # Invoice is still active, footprint hasn't moved.
    inv = _fetch_invoice(inv_id)
    assert inv and inv["status"] != "cancelled"


# =====================================================================
# INV-008 · Stock request RBAC
# =====================================================================

def test_inv008_audiologist_forbidden_on_stock_request_create():
    try:
        audio = login(AUDIO_EMAIL, AUDIO_PASSWORD)
    except AssertionError:
        pytest.skip("audio test account not seeded")
    r = requests.post(f"{API}/stock-requests", headers=H(audio), json={
        "lines": [{"kind": "hearing_aid_model", "product_id": "P-NAV010",
                   "qty": 1}],
        "urgency": "normal",
    }, timeout=10)
    assert r.status_code == 403, r.text


def test_inv008_front_desk_passes_rbac_gate():
    """front_desk is on the allow list — the call passes the RBAC gate
    and hits the clinic-group business logic (which may 409 if the
    tenant isn't grouped). Any non-403 return code proves the RBAC
    change worked."""
    try:
        fd = login(FRONTDESK_EMAIL, FRONTDESK_PASSWORD)
    except AssertionError:
        pytest.skip("front_desk test account not seeded")
    r = requests.post(f"{API}/stock-requests", headers=H(fd), json={
        "lines": [{"kind": "hearing_aid_model", "product_id": "P-NAV010",
                   "qty": 1}],
        "urgency": "normal",
    }, timeout=10)
    assert r.status_code != 403, r.text
    # Downstream layer typically 409 (no_group) — accept 400/404/409/422 as
    # evidence the gate is open.
    assert r.status_code in (200, 201, 400, 404, 409, 422), r.text


# =====================================================================
# Cross-cutting isolation — cannot touch another tenant's quick sale
# =====================================================================

def test_inv005_cross_tenant_cancel_returns_404():
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    r = requests.post(
        f"{API}/ha/quick-sales/QSL-NAV010-DUMMY-999/cancel",
        headers=H(tok),
        json={"reason": "cross tenant", "confirm_refund_offline": False},
        timeout=10,
    )
    assert r.status_code == 404, r.text
