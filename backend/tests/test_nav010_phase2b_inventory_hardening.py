"""NAV-010 · Phase 2B · Sprint 1 · Inventory Hardening regression.

Covers the six approved findings in Sprint 1:

  * INV-009 · Forward-only ``clinic_id`` stamping on every NEW
              ``serial_events`` writer (transition_serial, mark-demo,
              unmark-demo, return-borrow, GRN receipt).
  * INV-010 · Compound clinic-scoped index on ``serial_events``.
  * INV-011 · Compound clinic-scoped index on ``accessory_events``.
  * INV-012 · Unique + compound indexes on ``stock_requests``.
  * INV-013 · Unique + compound indexes on ``payment_reversals``.
  * INV-014 · ``_branch_scope`` helper de-duplication with
              zero-diff behavioural preservation.
  * INV-018 · Performance index on ``serial_items``.

Explicitly HELD in this sprint (no coverage here): INV-015, INV-016, INV-017.

The suite is intentionally focused — it does NOT re-run the 1790-file
full regression per the sprint-1 mandate. INV-Phase-2A smoke coverage
lives in ``test_nav010_inventory_hardening.py`` and is unaffected.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import time
import uuid
from typing import Optional

import pytest
import requests
from motor.motor_asyncio import AsyncIOMotorClient

sys.path.append(str(pathlib.Path(__file__).resolve().parents[1]))
from _helpers import API, ADMIN_EMAIL, ADMIN_PASSWORD, H, login  # noqa: E402


_BRANCH_ID = "BR-PYTEST-001"
_CLINIC_ID = os.environ.get("TEST_CLINIC_ID", "clinic-pytest-suite")


# ─── DB fixture helpers ───────────────────────────────────────────────

def _run_mongo(fn):
    loop = asyncio.new_event_loop()
    try:
        async def _wrapped():
            cli = AsyncIOMotorClient(os.environ["MONGO_URL"])
            try:
                return await fn(cli[os.environ["DB_NAME"]])
            finally:
                cli.close()
        return loop.run_until_complete(_wrapped())
    finally:
        loop.close()


def _list_indexes(coll: str) -> list[dict]:
    return _run_mongo(lambda db: db[coll].list_indexes().to_list(100))


def _index_by_name(coll: str, name: str) -> Optional[dict]:
    for d in _list_indexes(coll):
        if d.get("name") == name:
            return d
    return None


# =====================================================================
# INV-010 / 011 / 012 / 013 / 018 · Index existence + key patterns
# =====================================================================

def test_inv010_serial_events_clinic_scoped_index_installed():
    idx = _index_by_name("serial_events", "serial_events_clinic_serial_at")
    assert idx is not None, "INV-010 index missing"
    assert list(idx["key"].items()) == [
        ("clinic_id", 1), ("serial_id", 1), ("at", -1),
    ]
    assert not idx.get("unique", False), "INV-010 must NOT be unique"


def test_inv010_preserves_legacy_serial_events_serial_id_index():
    """Additive-only guarantee: the legacy (serial_id, at) index that
    powers /serial-items/{id}/timeline MUST still exist."""
    idxs = _list_indexes("serial_events")
    keys = [list(d["key"].items()) for d in idxs]
    assert [("serial_id", 1), ("at", -1)] in keys, \
        "INV-010 must not remove the legacy serial_id index"


def test_inv011_accessory_events_clinic_sku_at_index_installed():
    idx = _index_by_name("accessory_events", "accessory_events_clinic_sku_at")
    assert idx is not None, "INV-011 index missing"
    assert list(idx["key"].items()) == [
        ("clinic_id", 1), ("sku_id", 1), ("at", -1),
    ]
    assert not idx.get("unique", False)


def test_inv012_stock_requests_request_id_unique_index_installed():
    idx = _index_by_name("stock_requests", "uniq_stock_request_id")
    assert idx is not None, "INV-012 unique index missing"
    assert list(idx["key"].items()) == [("request_id", 1)]
    assert idx.get("unique", False) is True


def test_inv012_stock_requests_clinic_status_ct_index_installed():
    idx = _index_by_name("stock_requests", "stock_requests_clinic_status_ct")
    assert idx is not None, "INV-012 clinic index missing"
    assert list(idx["key"].items()) == [
        ("clinic_id", 1), ("status", 1), ("created_at", -1),
    ]
    assert not idx.get("unique", False)


def test_inv012_stock_requests_group_status_ct_index_installed():
    idx = _index_by_name("stock_requests", "stock_requests_group_status_ct")
    assert idx is not None, "INV-012 group index missing"
    assert list(idx["key"].items()) == [
        ("group_id", 1), ("status", 1), ("created_at", -1),
    ]


def test_inv013_payment_reversals_unique_index_installed():
    idx = _index_by_name("payment_reversals", "uniq_payment_reversal_id")
    assert idx is not None, "INV-013 unique index missing"
    assert list(idx["key"].items()) == [("reversal_id", 1)]
    assert idx.get("unique", False) is True


def test_inv013_payment_reversals_clinic_invoice_index_installed():
    idx = _index_by_name("payment_reversals", "payment_reversals_clinic_invoice")
    assert idx is not None
    assert list(idx["key"].items()) == [("clinic_id", 1), ("invoice_id", 1)]
    assert not idx.get("unique", False)


def test_inv013_payment_reversals_clinic_quick_sale_index_installed():
    idx = _index_by_name("payment_reversals", "payment_reversals_clinic_quick_sale")
    assert idx is not None
    assert list(idx["key"].items()) == [("clinic_id", 1), ("quick_sale_id", 1)]


def test_inv018_serial_items_clinic_branch_product_state_index_installed():
    idx = _index_by_name("serial_items", "serial_clinic_branch_product_state")
    assert idx is not None, "INV-018 index missing"
    assert list(idx["key"].items()) == [
        ("clinic_id", 1), ("branch_id", 1), ("product_id", 1), ("state", 1),
    ]
    assert not idx.get("unique", False), "INV-018 must NOT be unique"


def test_inv018_preserves_legacy_serial_items_indexes():
    """Legacy `(clinic_id, serial_no, unique)` + `(clinic_id, branch_id, state)`
    indexes MUST still exist — INV-018 is purely additive."""
    idxs = _list_indexes("serial_items")
    names = {d["name"] for d in idxs}
    assert "uniq_clinic_serial_no" in names
    keys = [list(d["key"].items()) for d in idxs]
    assert [("clinic_id", 1), ("branch_id", 1), ("state", 1)] in keys


def test_startup_index_creation_is_idempotent():
    """Re-running the index creation block on already-existing indexes
    must NOT crash the server. We simulate this by re-invoking each
    ``create_index`` call — Mongo treats a same-spec re-creation as a
    no-op. If any of the Sprint-1 indexes were declared with a
    conflicting spec they would raise; a clean pass here is the
    idempotency proof."""
    async def _replay(db):
        # Only the six Sprint-1 index calls — literally the same specs.
        await db.serial_events.create_index(
            [("clinic_id", 1), ("serial_id", 1), ("at", -1)],
            name="serial_events_clinic_serial_at",
        )
        await db.accessory_events.create_index(
            [("clinic_id", 1), ("sku_id", 1), ("at", -1)],
            name="accessory_events_clinic_sku_at",
        )
        await db.stock_requests.create_index(
            "request_id", unique=True, name="uniq_stock_request_id",
        )
        await db.stock_requests.create_index(
            [("clinic_id", 1), ("status", 1), ("created_at", -1)],
            name="stock_requests_clinic_status_ct",
        )
        await db.stock_requests.create_index(
            [("group_id", 1), ("status", 1), ("created_at", -1)],
            name="stock_requests_group_status_ct",
        )
        await db.payment_reversals.create_index(
            "reversal_id", unique=True, name="uniq_payment_reversal_id",
        )
        await db.payment_reversals.create_index(
            [("clinic_id", 1), ("invoice_id", 1)],
            name="payment_reversals_clinic_invoice",
        )
        await db.payment_reversals.create_index(
            [("clinic_id", 1), ("quick_sale_id", 1)],
            name="payment_reversals_clinic_quick_sale",
        )
        await db.serial_items.create_index(
            [("clinic_id", 1), ("branch_id", 1), ("product_id", 1), ("state", 1)],
            name="serial_clinic_branch_product_state",
        )
    _run_mongo(_replay)  # no exception ⇒ idempotent


# =====================================================================
# INV-009 · Forward-only clinic_id stamping on serial_events writers
# =====================================================================

def _seed_test_serial(state: str = "IN_STOCK") -> dict:
    """Insert a serial_items row for the pytest tenant. Returns full doc."""
    serial_id = f"SI-P2B-{uuid.uuid4().hex[:10].upper()}"
    serial_no = f"P2B-SN-{uuid.uuid4().hex[:8].upper()}"
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    doc = {
        "serial_id": serial_id,
        "clinic_id": _CLINIC_ID,
        "branch_id": _BRANCH_ID,
        "product_id": "PRD-P2B-SEED",
        "serial_no": serial_no,
        "state": state,
        "pool": "saleable",
        "received_at": time.strftime("%Y-%m-%d"),
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    _run_mongo(lambda db: db.serial_items.insert_one(dict(doc)))
    return doc


def _find_latest_event(serial_id: str) -> Optional[dict]:
    return _run_mongo(lambda db: db.serial_events.find_one(
        {"serial_id": serial_id}, {"_id": 0}, sort=[("at", -1)],
    ))


def test_inv009_transition_serial_stamps_clinic_id():
    """POST /api/ha/serial-items/{id}/transition → the emitted
    serial_events row carries clinic_id + preserves every legacy field."""
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    seed = _seed_test_serial(state="IN_STOCK")
    sid = seed["serial_id"]
    r = requests.post(
        f"{API}/ha/serial-items/{sid}/transition",
        headers=H(tok), json={"to_state": "RESERVED", "note": "INV-009 test"},
        timeout=10,
    )
    assert r.status_code == 200, r.text
    evt = _find_latest_event(sid)
    assert evt is not None, "transition_serial did not emit an event"
    # INV-009 — clinic_id stamped forward.
    assert evt.get("clinic_id") == _CLINIC_ID
    # Legacy fields intact.
    assert evt.get("from") == "IN_STOCK"
    assert evt.get("to") == "RESERVED"
    assert evt.get("at")
    assert evt.get("actor_user_id")
    assert evt.get("ref_doc", {}).get("kind") == "manual"
    assert evt.get("note") == "INV-009 test"


def test_inv009_mark_demo_stamps_clinic_id():
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    seed = _seed_test_serial(state="IN_STOCK")
    sid = seed["serial_id"]
    r = requests.post(
        f"{API}/ha/serial-items/{sid}/mark-demo",
        headers=H(tok), json={"note": "INV-009 demo"}, timeout=10,
    )
    assert r.status_code == 200, r.text
    evt = _find_latest_event(sid)
    assert evt is not None
    assert evt.get("clinic_id") == _CLINIC_ID
    assert evt.get("ref_doc", {}).get("to_pool") == "demo"


def test_inv009_unmark_demo_stamps_clinic_id():
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    seed = _seed_test_serial(state="IN_STOCK")
    sid = seed["serial_id"]
    # First flip to demo pool.
    r_mark = requests.post(
        f"{API}/ha/serial-items/{sid}/mark-demo",
        headers=H(tok), json={"note": "seed demo"}, timeout=10,
    )
    assert r_mark.status_code == 200, r_mark.text
    # Then unmark.
    r_unmark = requests.post(
        f"{API}/ha/serial-items/{sid}/unmark-demo",
        headers=H(tok), json={"note": "INV-009 unmark"}, timeout=10,
    )
    assert r_unmark.status_code == 200, r_unmark.text
    evt = _find_latest_event(sid)
    assert evt is not None
    assert evt.get("clinic_id") == _CLINIC_ID
    assert evt.get("ref_doc", {}).get("to_pool") == "saleable"


def test_inv009_return_borrow_stamps_clinic_id():
    """Borrowed unit returned to source clinic → serial_events row
    carries clinic_id + `return-to-source` kind."""
    serial_id = f"SI-P2B-B-{uuid.uuid4().hex[:10].upper()}"
    serial_no = f"P2B-BSN-{uuid.uuid4().hex[:8].upper()}"
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    doc = {
        "serial_id": serial_id,
        "clinic_id": _CLINIC_ID,
        "branch_id": _BRANCH_ID,
        "product_id": "PRD-P2B-SEED",
        "serial_no": serial_no,
        "state": "IN_STOCK",
        "pool": "saleable",
        "source_kind": "borrowed",
        "borrowed_from": "CL-OTHER-CLINIC",
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    _run_mongo(lambda db: db.serial_items.insert_one(dict(doc)))
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    r = requests.post(
        f"{API}/ha/serial-items/{serial_id}/return-borrow",
        headers=H(tok), json={"note": "INV-009 return"}, timeout=10,
    )
    assert r.status_code == 200, r.text
    evt = _find_latest_event(serial_id)
    assert evt is not None
    assert evt.get("clinic_id") == _CLINIC_ID
    assert evt.get("ref_doc", {}).get("kind") == "return-to-source"
    assert evt.get("to") == "RETURNED"


def test_inv009_add_serials_via_catalogue_stamps_clinic_id():
    """Sprint-1 amendment · Catalogue Quick-Add serials writer.

    POST /api/ha/products/{product_id}/serials emits one
    ``catalogue-quick-add`` event per unit inserted. The Sprint-1
    amendment stamps ``clinic_id`` on those NEW events. Proves:

    1. The endpoint call creates a serial_events row.
    2. The new event carries ``clinic_id`` = the authenticated user's clinic.
    3. All legacy event fields remain unchanged.
    4. Historical serial_events rows are NOT modified.
    """
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)

    # Snapshot of pre-existing rows for the test tenant. Historical
    # events must NOT be touched (forward-only guarantee).
    pre_snapshot = _run_mongo(lambda db: db.serial_events.find(
        {"clinic_id": _CLINIC_ID},
        {"_id": 0, "serial_id": 1, "at": 1, "to": 1, "clinic_id": 1},
    ).to_list(5000))
    pre_pairs = sorted((r["serial_id"], r["at"]) for r in pre_snapshot)

    # Seed a serialised catalogue product for the test clinic. Direct
    # insert bypasses the create endpoint (out-of-scope for this test);
    # the fixture is fully self-contained and torn down implicitly by
    # the tenant scope.
    product_id = f"PRD-P2B-CAT-{uuid.uuid4().hex[:8].upper()}"
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    _run_mongo(lambda db: db.ha_products.insert_one({
        "product_id": product_id,
        "clinic_id": _CLINIC_ID,
        "brand": "P2B-CAT",
        "model": f"Amendment-{uuid.uuid4().hex[:6]}",
        "category": "hearing_aid",
        "is_serialised": True,
        "active": True,
        "created_at": now_iso,
        "updated_at": now_iso,
    }))

    serial_no = f"P2B-CAT-SN-{uuid.uuid4().hex[:8].upper()}"
    r = requests.post(
        f"{API}/ha/products/{product_id}/serials",
        headers=H(tok),
        json=[{
            "serial_no": serial_no,
            "branch_id": _BRANCH_ID,
            "pool": "saleable",
            "source_kind": "vendor",
        }],
        timeout=15,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["inserted"] == 1
    new_serial_id = body["serials"][0]["serial_id"]

    # New event must exist and be stamped.
    evt = _run_mongo(lambda db: db.serial_events.find_one(
        {"serial_id": new_serial_id}, {"_id": 0},
    ))
    assert evt is not None, "Catalogue Quick-Add did not emit a serial_events row"

    # (2) clinic_id stamped forward.
    assert evt.get("clinic_id") == _CLINIC_ID, (
        f"INV-009 amendment failed: clinic_id={evt.get('clinic_id')!r}"
    )
    # (3) Legacy fields unchanged.
    assert evt.get("from") is None
    assert evt.get("to") == "IN_STOCK"
    assert evt.get("at")
    assert evt.get("actor_user_id")
    ref = evt.get("ref_doc") or {}
    assert ref.get("kind") == "catalogue-quick-add"
    assert ref.get("id") == product_id
    assert "Added via Catalogue form" in (evt.get("note") or "")

    # (4) No historical rows were modified. Compare the invariant
    # (serial_id, at) tuple set for the test tenant BEFORE the call
    # against the same set AFTER, excluding rows produced by this
    # call. Every historical pair must still be present unchanged.
    post_snapshot = _run_mongo(lambda db: db.serial_events.find(
        {"clinic_id": _CLINIC_ID},
        {"_id": 0, "serial_id": 1, "at": 1},
    ).to_list(5000))
    post_pairs_excluding_new = sorted(
        (r["serial_id"], r["at"]) for r in post_snapshot
        if r["serial_id"] != new_serial_id
    )
    assert post_pairs_excluding_new == pre_pairs, (
        "Historical serial_events were modified — INV-009 forward-only "
        "contract violated"
    )



def test_inv009_event_shape_preserves_all_legacy_fields():
    """Concretely enforce: emitting NEW events must not drop or rename
    any legacy field. INV-009 is additive only."""
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    seed = _seed_test_serial(state="IN_STOCK")
    sid = seed["serial_id"]
    r = requests.post(
        f"{API}/ha/serial-items/{sid}/transition",
        headers=H(tok), json={"to_state": "RESERVED"}, timeout=10,
    )
    assert r.status_code == 200
    evt = _find_latest_event(sid)
    assert evt is not None
    legacy_fields = {"serial_id", "from", "to", "at", "actor_user_id", "ref_doc", "note"}
    for f in legacy_fields:
        assert f in evt, f"legacy field {f!r} dropped from serial_events shape"
    # Additive tenant field.
    assert "clinic_id" in evt
    # No unrelated fields sneaked in.
    unexpected = set(evt.keys()) - (legacy_fields | {"clinic_id"})
    assert not unexpected, f"unexpected new fields on serial_events: {unexpected}"


# =====================================================================
# INV-014 · _branch_scope dedupe — behavioural equivalence tests
# =====================================================================
#
# The pre-refactor helper was defined identically in both routers. The
# post-refactor helper is imported from utils.branch_scope. The tests
# call the shared helper with the same user dicts that both routers
# would have received and assert the output filter dict is IDENTICAL
# to the historical inline implementation. This is a pure equivalence
# check — no HTTP calls, no DB writes.

from utils.branch_scope import branch_scope  # noqa: E402


def _historical_ha_inventory_branch_scope(user: dict) -> dict:
    """Byte-for-byte copy of the pre-refactor ``_branch_scope`` from
    routers/ha_inventory.py (INV-014 reference implementation)."""
    from auth import CLINIC_WIDE_ROLES
    if user["role"] in CLINIC_WIDE_ROLES:
        return {"clinic_id": user["clinic_id"]}
    return {
        "clinic_id": user["clinic_id"],
        "branch_id": {"$in": user.get("branch_ids") or []},
    }


def _historical_ha_procurement_branch_scope(user: dict) -> dict:
    """Byte-for-byte copy of the pre-refactor ``_branch_scope`` from
    routers/ha_procurement.py (INV-014 reference implementation)."""
    from auth import CLINIC_WIDE_ROLES
    if user["role"] in CLINIC_WIDE_ROLES:
        return {"clinic_id": user["clinic_id"]}
    return {"clinic_id": user["clinic_id"], "branch_id": {"$in": user.get("branch_ids") or []}}


def test_inv014_shared_helper_matches_historical_ha_inventory_for_clinic_wide_role():
    from auth import CLINIC_WIDE_ROLES
    role = next(iter(CLINIC_WIDE_ROLES))
    user = {"role": role, "clinic_id": "clinic-x", "branch_ids": ["BR1", "BR2"]}
    assert branch_scope(user) == _historical_ha_inventory_branch_scope(user)


def test_inv014_shared_helper_matches_historical_ha_procurement_for_clinic_wide_role():
    from auth import CLINIC_WIDE_ROLES
    role = next(iter(CLINIC_WIDE_ROLES))
    user = {"role": role, "clinic_id": "clinic-x", "branch_ids": ["BR1"]}
    assert branch_scope(user) == _historical_ha_procurement_branch_scope(user)


def test_inv014_shared_helper_matches_historical_for_branch_scoped_role():
    """For a role NOT in CLINIC_WIDE_ROLES, the shared helper must
    include the branch_id restrictor identically."""
    user = {
        "role": "audiologist",  # any branch-scoped role
        "clinic_id": "clinic-x",
        "branch_ids": ["BR1", "BR2"],
    }
    expected = {
        "clinic_id": "clinic-x",
        "branch_id": {"$in": ["BR1", "BR2"]},
    }
    assert branch_scope(user) == expected
    assert branch_scope(user) == _historical_ha_inventory_branch_scope(user)
    assert branch_scope(user) == _historical_ha_procurement_branch_scope(user)


def test_inv014_shared_helper_matches_historical_for_empty_branch_list():
    user = {"role": "front_desk", "clinic_id": "clinic-x", "branch_ids": []}
    expected = {"clinic_id": "clinic-x", "branch_id": {"$in": []}}
    assert branch_scope(user) == expected
    assert branch_scope(user) == _historical_ha_inventory_branch_scope(user)


def test_inv014_shared_helper_matches_historical_for_missing_branch_key():
    """A user dict without a ``branch_ids`` key at all — historical
    behaviour was ``{"$in": []}``."""
    user = {"role": "front_desk", "clinic_id": "clinic-x"}
    expected = {"clinic_id": "clinic-x", "branch_id": {"$in": []}}
    assert branch_scope(user) == expected
    assert branch_scope(user) == _historical_ha_inventory_branch_scope(user)
    assert branch_scope(user) == _historical_ha_procurement_branch_scope(user)


def test_inv014_ha_inventory_module_uses_shared_helper():
    """The router's ``_branch_scope`` binding must resolve to the
    exact same callable as ``utils.branch_scope.branch_scope`` — no
    accidental shadowing."""
    from routers import ha_inventory
    assert ha_inventory._branch_scope is branch_scope


def test_inv014_ha_procurement_module_uses_shared_helper():
    from routers import ha_procurement
    assert ha_procurement._branch_scope is branch_scope


# =====================================================================
# NAV-010 Phase 2A regression sanity — approved scope only
# =====================================================================
#
# Per the sprint mandate these are lightweight verifications that
# Phase 2A public surfaces still respond as expected — they are NOT a
# re-run of the 32/32 P2A suite (which lives in
# test_nav010_inventory_hardening.py).

def test_phase2a_regression_serial_transition_still_produces_event():
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    seed = _seed_test_serial(state="IN_STOCK")
    r = requests.post(
        f"{API}/ha/serial-items/{seed['serial_id']}/transition",
        headers=H(tok), json={"to_state": "RESERVED"}, timeout=10,
    )
    assert r.status_code == 200
    # Exactly one new event per successful transition.
    count = _run_mongo(lambda db: db.serial_events.count_documents(
        {"serial_id": seed["serial_id"]},
    ))
    assert count == 1


def test_phase2a_regression_illegal_transition_returns_409():
    tok = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    seed = _seed_test_serial(state="IN_STOCK")
    r = requests.post(
        f"{API}/ha/serial-items/{seed['serial_id']}/transition",
        headers=H(tok), json={"to_state": "RETIRED"}, timeout=10,
    )
    assert r.status_code == 409, r.text


def test_phase2a_regression_quick_sale_route_still_gated():
    """POST /api/ha/quick-sales/{id}/cancel (INV-005) must still exist
    and be auth-gated. Unauthenticated → 401 or 403."""
    r = requests.post(
        f"{API}/ha/quick-sales/NAV010-P2B-DUMMY-DO-NOT-USE/cancel",
        json={"reason": "regression probe"},
        timeout=10,
    )
    assert r.status_code in (401, 403), f"unexpected: {r.status_code} {r.text}"
