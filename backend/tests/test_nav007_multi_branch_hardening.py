"""NAV-007 · Multi-Branch / Login-as-Branch hardening regression suite.

Covers all 7 approved fixes (B1, B2, B3, B4, B5, G1, B6) + the multi-clinic
isolation invariant explicitly required in the Phase 2 approval note.

Design notes
------------
* Every test builds and tears down its own NAV-007-scoped state so runs are
  fully idempotent. No touch of the pytest bootstrap tenant, no touch of
  demo-seed clinics, no touch of production.
* We drive the HTTP surface for behavioural assertions; direct DB inspection
  is used ONLY for state verification (session revocation counts,
  token_version comparisons, presence checks).
* Fixtures create a distinct clinic-group of shape:

      NAV007-HEAD (head)
      ├── NAV007-BRANCH-A
      └── NAV007-BRANCH-B

  plus three users:

      * `nav007.owner@audinexa.test`      — clinic_owner at HEAD,
        additional_clinic_ids = [A, B]  (mimics the state that the real
        POST /branches endpoint would leave after creating both branches).
      * `nav007.branch_a_user@audinexa.test` — audiologist whose PRIMARY
        clinic_id == BRANCH-A.
      * `nav007.grantee@audinexa.test`    — audiologist at HEAD with
        BRANCH-A in `additional_clinic_ids` (models a cross-clinic
        accountant/grantee linked via /auth/link-clinic).

  The BOOTSTRAP is idempotent; a second run reuses whatever exists.
* Cleanup is best-effort at module teardown — we don't fail tests when
  cleanup misses a row (concurrent runs on the same DB are safe).

NAV-007 test-matrix line-up (22 tests total):

    1  test_active_branch_baseline_access
    2  test_deactivate_branch_returns_ok_with_counts
    3  test_existing_jwt_for_branch_rejected_after_deactivation
    4  test_branch_primary_user_authed_calls_rejected
    5  test_additional_clinic_user_loses_branch_from_extras
    6  test_cross_clinic_grantee_pruned_platform_wide
    7  test_inactive_branch_absent_from_my_clinics
    8  test_switch_clinic_to_inactive_rejects_403
    9  test_fresh_login_cannot_use_deactivated_branch
    10 test_head_clinic_unaffected_by_branch_deactivation
    11 test_sibling_active_branch_unaffected
    12 test_user_sessions_revoked_correctly
    13 test_token_version_not_bumped_preserves_other_clinic_access
    14 test_reactivate_branch_restores_group_and_status
    15 test_reactivated_branch_reappears_in_my_clinics
    16 test_reactivation_idempotent_on_already_active
    17 test_reactivate_foreign_branch_rejected
    18 test_reactivation_does_not_resurrect_manually_deactivated_user
    19 test_cross_tenant_isolation_still_intact
    20 test_legacy_clinic_without_status_field_still_works
    21 test_my_clinics_no_longer_returns_active_field
    22 test_multi_clinic_isolation_head_branch_a_branch_b
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

import pytest
import requests

from _helpers import API, H, login

# ─── Test tenant identifiers (all prefixed so cleanup can sweep by prefix) ─
HEAD_ID = "NAV007-HEAD"
BRANCH_A_ID = "NAV007-BRANCH-A"
BRANCH_B_ID = "NAV007-BRANCH-B"
GROUP_ID = "NAV007-GROUP"
LEGACY_ID = "NAV007-LEGACY"      # clinic with no `status` field
FOREIGN_HEAD_ID = "NAV007-FOREIGN-HEAD"
FOREIGN_BRANCH_ID = "NAV007-FOREIGN-BRANCH"
FOREIGN_GROUP_ID = "NAV007-FOREIGN-GROUP"

OWNER_EMAIL = "nav007.owner@audinexa.test"
OWNER_PASSWORD = "Nav007Owner@1"
BRANCH_A_USER_EMAIL = "nav007.branch_a_user@audinexa.test"
BRANCH_A_USER_PASSWORD = "Nav007BranchA@1"
GRANTEE_EMAIL = "nav007.grantee@audinexa.test"
GRANTEE_PASSWORD = "Nav007Grantee@1"
DEACTIVATED_USER_EMAIL = "nav007.deactivated@audinexa.test"
DEACTIVATED_USER_PASSWORD = "Nav007Deact@1"
LEGACY_USER_EMAIL = "nav007.legacy@audinexa.test"
LEGACY_USER_PASSWORD = "Nav007Legacy@1"
FOREIGN_OWNER_EMAIL = "nav007.foreign_owner@audinexa.test"
FOREIGN_OWNER_PASSWORD = "Nav007Foreign@1"


# ─── Async DB helper ──────────────────────────────────────────────────────
def _run(coro):
    """Run an async DB helper from a sync test body."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # In case tests are nested under an async runner; make a fresh loop.
            raise RuntimeError("nested loop")
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


async def _db():
    """Fresh Motor client bound to the same MONGO_URL / DB_NAME the server uses."""
    from motor.motor_asyncio import AsyncIOMotorClient
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    return client, client[os.environ["DB_NAME"]]


# ─── One-time bootstrap (idempotent) ──────────────────────────────────────
async def _bootstrap() -> None:
    from auth import hash_password
    client, db = await _db()
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        # ── 1. Head + two branches + one legacy no-status clinic ──
        clinic_docs = [
            {
                "clinic_id": HEAD_ID, "name": "NAV007 Head Clinic",
                "city": "Bengaluru", "state": "Karnataka",
                "status": "active", "is_head_of_group": True,
                "clinic_group_id": GROUP_ID,
                "subscription_tier": "PREMIUM",
                "mrd_prefix": "NAV7", "created_at": now_iso,
            },
            {
                "clinic_id": BRANCH_A_ID, "name": "NAV007 Branch A",
                "city": "Mysore", "state": "Karnataka",
                "status": "active", "is_head_of_group": False,
                "clinic_group_id": GROUP_ID,
                "parent_clinic_id": HEAD_ID,
                "subscription_tier": "PREMIUM",
                "mrd_prefix": "NAV7A", "created_at": now_iso,
            },
            {
                "clinic_id": BRANCH_B_ID, "name": "NAV007 Branch B",
                "city": "Hubli", "state": "Karnataka",
                "status": "active", "is_head_of_group": False,
                "clinic_group_id": GROUP_ID,
                "parent_clinic_id": HEAD_ID,
                "subscription_tier": "PREMIUM",
                "mrd_prefix": "NAV7B", "created_at": now_iso,
            },
            # Legacy clinic — deliberately no `status` field to prove
            # missing-status still authenticates (B1 legacy tolerance).
            {
                "clinic_id": LEGACY_ID, "name": "NAV007 Legacy Clinic",
                "city": "Delhi", "state": "Delhi",
                "subscription_tier": "STANDARD",
                "mrd_prefix": "NAV7L", "created_at": now_iso,
            },
            # Foreign-group head + one branch to prove cross-group
            # reactivation is rejected.
            {
                "clinic_id": FOREIGN_HEAD_ID, "name": "NAV007 Foreign Head",
                "city": "Chennai", "state": "Tamil Nadu",
                "status": "active", "is_head_of_group": True,
                "clinic_group_id": FOREIGN_GROUP_ID,
                "subscription_tier": "PREMIUM",
                "mrd_prefix": "NAV7F", "created_at": now_iso,
            },
            {
                "clinic_id": FOREIGN_BRANCH_ID, "name": "NAV007 Foreign Branch",
                "city": "Madurai", "state": "Tamil Nadu",
                "status": "active", "is_head_of_group": False,
                "clinic_group_id": FOREIGN_GROUP_ID,
                "parent_clinic_id": FOREIGN_HEAD_ID,
                "subscription_tier": "PREMIUM",
                "mrd_prefix": "NAV7FB", "created_at": now_iso,
            },
        ]
        for c in clinic_docs:
            await db.clinics.update_one(
                {"clinic_id": c["clinic_id"]},
                {"$setOnInsert": c},
                upsert=True,
            )
        # Reset status on branches every bootstrap — a previous suite run
        # may have left BRANCH-A inactive. Legacy clinic explicitly kept
        # with no `status` field via $unset for regression #20.
        await db.clinics.update_one(
            {"clinic_id": BRANCH_A_ID},
            {"$set": {"status": "active"}, "$unset": {"deactivated_at": ""}},
        )
        await db.clinics.update_one(
            {"clinic_id": BRANCH_B_ID},
            {"$set": {"status": "active"}, "$unset": {"deactivated_at": ""}},
        )
        await db.clinics.update_one(
            {"clinic_id": FOREIGN_BRANCH_ID},
            {"$set": {"status": "active"}, "$unset": {"deactivated_at": ""}},
        )
        await db.clinics.update_one(
            {"clinic_id": LEGACY_ID},
            {"$unset": {"status": "", "active": ""}},
        )

        # ── 2. clinic_groups doc for HEAD (idempotent) ──
        await db.clinic_groups.update_one(
            {"group_id": GROUP_ID},
            {"$setOnInsert": {
                "group_id": GROUP_ID,
                "name": "NAV007 Test Group",
                "head_clinic_id": HEAD_ID,
                "member_clinic_ids": [],
                "created_at": now_iso, "updated_at": now_iso,
            }},
            upsert=True,
        )
        # Ensure BOTH branches are members every run (a previous run may
        # have deactivated one and $pull'd it).
        await db.clinic_groups.update_one(
            {"group_id": GROUP_ID},
            {"$addToSet": {"member_clinic_ids": {"$each": [BRANCH_A_ID, BRANCH_B_ID]}}},
        )
        # Foreign group with its branch as member.
        await db.clinic_groups.update_one(
            {"group_id": FOREIGN_GROUP_ID},
            {"$setOnInsert": {
                "group_id": FOREIGN_GROUP_ID,
                "name": "NAV007 Foreign Group",
                "head_clinic_id": FOREIGN_HEAD_ID,
                "member_clinic_ids": [],
                "created_at": now_iso, "updated_at": now_iso,
            }},
            upsert=True,
        )
        await db.clinic_groups.update_one(
            {"group_id": FOREIGN_GROUP_ID},
            {"$addToSet": {"member_clinic_ids": FOREIGN_BRANCH_ID}},
        )

        # ── 3. Users ──
        users = [
            # Head owner — clinic_owner at HEAD, extras = [A, B]
            {
                "user_id": "USR-NAV007-OWNER", "email": OWNER_EMAIL,
                "name": "NAV007 Owner", "role": "clinic_owner",
                "clinic_id": HEAD_ID,
                "additional_clinic_ids": [BRANCH_A_ID, BRANCH_B_ID],
                "active": True, "email_verified": True,
                "created_at": now_iso,
            },
            # Branch A primary user
            {
                "user_id": "USR-NAV007-BR-A-USER", "email": BRANCH_A_USER_EMAIL,
                "name": "NAV007 Branch A User", "role": "audiologist",
                "clinic_id": BRANCH_A_ID,
                "additional_clinic_ids": [],
                "active": True, "email_verified": True,
                "created_at": now_iso,
            },
            # Cross-clinic grantee — primary at HEAD, extras = [A]
            # (models a non-head-admin role having discretionary access
            # to Branch A via /auth/link-clinic).
            {
                "user_id": "USR-NAV007-GRANTEE", "email": GRANTEE_EMAIL,
                "name": "NAV007 Grantee", "role": "audiologist",
                "clinic_id": HEAD_ID,
                "additional_clinic_ids": [BRANCH_A_ID],
                "active": True, "email_verified": True,
                "created_at": now_iso,
            },
            # Manually-deactivated Branch A user (used by test #18)
            {
                "user_id": "USR-NAV007-DEACT", "email": DEACTIVATED_USER_EMAIL,
                "name": "NAV007 Deactivated", "role": "audiologist",
                "clinic_id": BRANCH_A_ID,
                "additional_clinic_ids": [],
                "active": False, "email_verified": True,
                "created_at": now_iso,
            },
            # Legacy-clinic user (test #20 — clinic has no status field)
            {
                "user_id": "USR-NAV007-LEGACY", "email": LEGACY_USER_EMAIL,
                "name": "NAV007 Legacy User", "role": "clinic_owner",
                "clinic_id": LEGACY_ID,
                "additional_clinic_ids": [],
                "active": True, "email_verified": True,
                "created_at": now_iso,
            },
            # Foreign-group owner (test #17)
            {
                "user_id": "USR-NAV007-FOREIGN", "email": FOREIGN_OWNER_EMAIL,
                "name": "NAV007 Foreign Owner", "role": "clinic_owner",
                "clinic_id": FOREIGN_HEAD_ID,
                "additional_clinic_ids": [FOREIGN_BRANCH_ID],
                "active": True, "email_verified": True,
                "created_at": now_iso,
            },
        ]
        # Persist password + reset transient state (extras, active flags).
        for u in users:
            existing = await db.users.find_one({"email": u["email"]}, {"_id": 0, "user_id": 1})
            u["password_hash"] = hash_password(
                {
                    OWNER_EMAIL: OWNER_PASSWORD,
                    BRANCH_A_USER_EMAIL: BRANCH_A_USER_PASSWORD,
                    GRANTEE_EMAIL: GRANTEE_PASSWORD,
                    DEACTIVATED_USER_EMAIL: DEACTIVATED_USER_PASSWORD,
                    LEGACY_USER_EMAIL: LEGACY_USER_PASSWORD,
                    FOREIGN_OWNER_EMAIL: FOREIGN_OWNER_PASSWORD,
                }[u["email"]]
            )
            if existing:
                # Overwrite mutable fields; keep user_id stable.
                await db.users.update_one(
                    {"email": u["email"]},
                    {"$set": {
                        "clinic_id": u["clinic_id"],
                        "additional_clinic_ids": u["additional_clinic_ids"],
                        "active": u["active"],
                        "password_hash": u["password_hash"],
                        "role": u["role"],
                    }},
                )
            else:
                await db.users.insert_one(u)
    finally:
        client.close()


async def _cleanup_transient() -> None:
    """Best-effort inter-test reset: re-open both branches + revive any
    sessions that a previous test revoked, so the next test starts clean.
    Cheap enough to run before every test.
    """
    client, db = await _db()
    try:
        for cid in (BRANCH_A_ID, BRANCH_B_ID, FOREIGN_BRANCH_ID):
            await db.clinics.update_one(
                {"clinic_id": cid},
                {"$set": {"status": "active"}, "$unset": {"deactivated_at": ""}},
            )
        # Ensure group membership + additional_clinic_ids are restored.
        await db.clinic_groups.update_one(
            {"group_id": GROUP_ID},
            {"$addToSet": {"member_clinic_ids": {"$each": [BRANCH_A_ID, BRANCH_B_ID]}}},
        )
        await db.clinic_groups.update_one(
            {"group_id": FOREIGN_GROUP_ID},
            {"$addToSet": {"member_clinic_ids": FOREIGN_BRANCH_ID}},
        )
        # Restore owner extras — a deactivate test will have $pull'd them.
        await db.users.update_one(
            {"email": OWNER_EMAIL},
            {"$addToSet": {"additional_clinic_ids": {"$each": [BRANCH_A_ID, BRANCH_B_ID]}}},
        )
        await db.users.update_one(
            {"email": GRANTEE_EMAIL},
            {"$addToSet": {"additional_clinic_ids": BRANCH_A_ID}},
        )
        await db.users.update_one(
            {"email": FOREIGN_OWNER_EMAIL},
            {"$addToSet": {"additional_clinic_ids": FOREIGN_BRANCH_ID}},
        )
    finally:
        client.close()


@pytest.fixture(scope="module", autouse=True)
def _bootstrap_module():
    _run(_bootstrap())
    yield
    # Teardown intentionally light — leave rows for post-hoc debugging.


@pytest.fixture(autouse=True)
def _reset_between_tests():
    _run(_cleanup_transient())
    yield


# ─── Endpoint helpers ─────────────────────────────────────────────────────
def _login_owner() -> str:
    return login(OWNER_EMAIL, OWNER_PASSWORD)


def _login_branch_a_user() -> str:
    return login(BRANCH_A_USER_EMAIL, BRANCH_A_USER_PASSWORD)


def _login_grantee() -> str:
    return login(GRANTEE_EMAIL, GRANTEE_PASSWORD)


def _login_legacy() -> str:
    return login(LEGACY_USER_EMAIL, LEGACY_USER_PASSWORD)


def _login_foreign_owner() -> str:
    return login(FOREIGN_OWNER_EMAIL, FOREIGN_OWNER_PASSWORD)


def _switch(token: str, target_clinic_id: str, timeout: int = 15):
    return requests.post(
        f"{API}/auth/switch-clinic",
        json={"clinic_id": target_clinic_id},
        headers=H(token),
        timeout=timeout,
    )


def _deactivate(token: str, branch_id: str, timeout: int = 15):
    return requests.post(
        f"{API}/clinic-groups/mine/branches/{branch_id}/deactivate",
        headers=H(token),
        timeout=timeout,
    )


def _reactivate(token: str, branch_id: str, timeout: int = 15):
    return requests.post(
        f"{API}/clinic-groups/mine/branches/{branch_id}/reactivate",
        headers=H(token),
        timeout=timeout,
    )


# ══════════════════════════════════════════════════════════════════════════
# TESTS
# ══════════════════════════════════════════════════════════════════════════

def test_active_branch_baseline_access():
    """1 · Active branch — user can authenticate against the branch."""
    tok = _login_owner()
    r = _switch(tok, BRANCH_A_ID)
    assert r.status_code == 200, r.text
    branch_tok = r.json()["access_token"]
    me = requests.get(f"{API}/auth/me", headers=H(branch_tok), timeout=15)
    assert me.status_code == 200, me.text
    assert me.json()["user"]["clinic_id"] == BRANCH_A_ID


def test_deactivate_branch_returns_ok_with_counts():
    """2 · Deactivation endpoint returns 200 + new NAV-007 counts."""
    tok = _login_owner()
    r = _deactivate(tok, BRANCH_A_ID)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert "sessions_revoked" in body
    assert "revoked_from_users" in body


def test_existing_jwt_for_branch_rejected_after_deactivation():
    """3 · Pre-deactivation JWT scoped to the branch → 401 after deactivation (B1)."""
    tok = _login_owner()
    switch = _switch(tok, BRANCH_A_ID)
    branch_tok = switch.json()["access_token"]
    # Baseline works.
    me1 = requests.get(f"{API}/auth/me", headers=H(branch_tok), timeout=15)
    assert me1.status_code == 200
    # Deactivate.
    d = _deactivate(tok, BRANCH_A_ID)
    assert d.status_code == 200
    # Same JWT — now 401.
    me2 = requests.get(f"{API}/auth/me", headers=H(branch_tok), timeout=15)
    assert me2.status_code == 401, me2.text


def test_branch_primary_user_authed_calls_rejected():
    """4 · Branch-primary user's fresh login still gets a JWT (login unchanged)
    but every authenticated call after it → 401 (B1)."""
    owner_tok = _login_owner()
    _deactivate(owner_tok, BRANCH_A_ID)
    # Fresh login for a branch-primary user — succeeds (login endpoint
    # untouched per approved plan §6).
    branch_tok = _login_branch_a_user()
    r = requests.get(f"{API}/auth/me", headers=H(branch_tok), timeout=15)
    assert r.status_code == 401, r.text


def test_additional_clinic_user_loses_branch_from_extras():
    """5 · Non-head-admin user with the branch in additional_clinic_ids
    has it pulled at deactivation (B2)."""
    owner_tok = _login_owner()
    # Baseline — grantee has BRANCH_A in extras.
    me_before = requests.get(f"{API}/auth/me", headers=H(_login_grantee()), timeout=15)
    assert BRANCH_A_ID in me_before.json()["user"]["additional_clinic_ids"]

    _deactivate(owner_tok, BRANCH_A_ID)

    # Grantee re-authenticates — extras no longer contain BRANCH_A.
    # (Existing JWT would show cached extras; a fresh login reads the
    #  updated user doc.)
    me_after = requests.get(f"{API}/auth/me", headers=H(_login_grantee()), timeout=15)
    assert me_after.status_code == 200
    assert BRANCH_A_ID not in me_after.json()["user"]["additional_clinic_ids"], me_after.text


def test_cross_clinic_grantee_pruned_platform_wide():
    """6 · The widened _revoke_head_admins_access must pull from EVERY user
    with the branch in extras, regardless of role or primary. Same evidence
    as #5 — asserted via direct DB probe for defence in depth."""
    owner_tok = _login_owner()
    _deactivate(owner_tok, BRANCH_A_ID)

    async def _check():
        client, db = await _db()
        try:
            leftovers = await db.users.count_documents({"additional_clinic_ids": BRANCH_A_ID})
            return leftovers
        finally:
            client.close()

    n = _run(_check())
    assert n == 0, f"still {n} user(s) reference BRANCH-A in additional_clinic_ids"


def test_inactive_branch_absent_from_my_clinics():
    """7 · GET /auth/my-clinics excludes inactive branches (B4)."""
    tok = _login_owner()
    _deactivate(tok, BRANCH_A_ID)
    # Fresh login to pick up refreshed extras.
    fresh = _login_owner()
    r = requests.get(f"{API}/auth/my-clinics", headers=H(fresh), timeout=15)
    assert r.status_code == 200
    ids = [c["clinic_id"] for c in r.json()["clinics"]]
    assert BRANCH_A_ID not in ids, ids
    # Branch B (still active) SHOULD be present.
    assert BRANCH_B_ID in ids, ids


def test_switch_clinic_to_inactive_rejects_403():
    """8 · Direct POST /auth/switch-clinic to inactive target → 403 (B5).

    We force the entitlement allowlist to still contain BRANCH-A by
    re-adding it to the owner's extras BETWEEN deactivation and the
    switch attempt — simulating a request that raced the pull.
    """
    tok = _login_owner()
    _deactivate(tok, BRANCH_A_ID)

    async def _readd():
        client, db = await _db()
        try:
            await db.users.update_one(
                {"email": OWNER_EMAIL},
                {"$addToSet": {"additional_clinic_ids": BRANCH_A_ID}},
            )
        finally:
            client.close()
    _run(_readd())

    fresh = _login_owner()
    r = _switch(fresh, BRANCH_A_ID)
    assert r.status_code == 403, r.text
    assert "no longer active" in r.text.lower() or "deactivated" in r.text.lower()


def test_fresh_login_cannot_use_deactivated_branch():
    """9 · Even a brand-new login for the branch-primary user cannot
    query clinic-scoped endpoints against the deactivated branch (B1)."""
    owner_tok = _login_owner()
    _deactivate(owner_tok, BRANCH_A_ID)
    # Fresh login (login itself unchanged and returns 200 + JWT).
    fresh = _login_branch_a_user()
    r = requests.get(f"{API}/patients", headers=H(fresh), timeout=15)
    assert r.status_code == 401, r.text


def test_head_clinic_unaffected_by_branch_deactivation():
    """10 · Head owner keeps full access to the head clinic after a branch dies."""
    tok = _login_owner()
    me_before = requests.get(f"{API}/auth/me", headers=H(tok), timeout=15)
    assert me_before.status_code == 200
    _deactivate(tok, BRANCH_A_ID)
    # Same head-scoped token still works — the head clinic is active.
    me_after = requests.get(f"{API}/auth/me", headers=H(tok), timeout=15)
    assert me_after.status_code == 200
    assert me_after.json()["user"]["clinic_id"] == HEAD_ID


def test_sibling_active_branch_unaffected():
    """11 · Deactivating BRANCH-A must not affect BRANCH-B."""
    tok = _login_owner()
    _deactivate(tok, BRANCH_A_ID)
    fresh = _login_owner()
    r = _switch(fresh, BRANCH_B_ID)
    assert r.status_code == 200, r.text
    branch_b_tok = r.json()["access_token"]
    me = requests.get(f"{API}/auth/me", headers=H(branch_b_tok), timeout=15)
    assert me.status_code == 200
    assert me.json()["user"]["clinic_id"] == BRANCH_B_ID


def test_user_sessions_revoked_correctly():
    """12 · user_sessions with clinic_id == branch have revoked_at set (B3).
    Sessions in OTHER clinics stay untouched."""
    owner_tok = _login_owner()
    # Create a session in BRANCH-A (via switch).
    switch_a = _switch(owner_tok, BRANCH_A_ID)
    assert switch_a.status_code == 200
    branch_a_tok = switch_a.json()["access_token"]
    # And another one in BRANCH-B for the sibling-preservation check.
    switch_b = _switch(_login_owner(), BRANCH_B_ID)
    assert switch_b.status_code == 200

    _deactivate(owner_tok, BRANCH_A_ID)

    async def _check():
        client, db = await _db()
        try:
            live_a = await db.user_sessions.count_documents(
                {"clinic_id": BRANCH_A_ID, "revoked_at": None},
            )
            live_b = await db.user_sessions.count_documents(
                {"clinic_id": BRANCH_B_ID, "revoked_at": None},
            )
            revoked_a = await db.user_sessions.count_documents(
                {"clinic_id": BRANCH_A_ID, "revoke_reason": "branch_deactivated"},
            )
            return live_a, live_b, revoked_a
        finally:
            client.close()

    live_a, live_b, revoked_a = _run(_check())
    assert live_a == 0, f"expected 0 live sessions in BRANCH-A, got {live_a}"
    assert live_b >= 1, f"expected sibling BRANCH-B sessions preserved, got {live_b}"
    assert revoked_a >= 1, f"expected revoke_reason='branch_deactivated' rows, got {revoked_a}"

    # And the previously-issued BRANCH-A token itself → 401 now.
    r = requests.get(f"{API}/auth/me", headers=H(branch_a_tok), timeout=15)
    assert r.status_code == 401


def test_token_version_not_bumped_preserves_other_clinic_access():
    """13 · CRITICAL · token_version MUST NOT be bumped for anyone
    during branch deactivation. Multi-clinic users' tokens against OTHER
    active clinics must keep working."""

    async def _snapshot(email: str):
        client, db = await _db()
        try:
            u = await db.users.find_one({"email": email}, {"_id": 0, "token_version": 1})
            return int((u or {}).get("token_version") or 0)
        finally:
            client.close()

    tv_owner_before = _run(_snapshot(OWNER_EMAIL))
    tv_branch_a_before = _run(_snapshot(BRANCH_A_USER_EMAIL))
    tv_grantee_before = _run(_snapshot(GRANTEE_EMAIL))

    _deactivate(_login_owner(), BRANCH_A_ID)

    tv_owner_after = _run(_snapshot(OWNER_EMAIL))
    tv_branch_a_after = _run(_snapshot(BRANCH_A_USER_EMAIL))
    tv_grantee_after = _run(_snapshot(GRANTEE_EMAIL))

    assert tv_owner_after == tv_owner_before, "owner tv bumped — head access would be forcibly lost"
    assert tv_branch_a_after == tv_branch_a_before, "branch-primary tv bumped — plan says surgical, not blanket"
    assert tv_grantee_after == tv_grantee_before, "grantee tv bumped — their other-clinic access would be broken"


def test_reactivate_branch_restores_group_and_status():
    """14 · Reactivate flips status + re-adds to group (G1)."""
    tok = _login_owner()
    _deactivate(tok, BRANCH_A_ID)
    r = _reactivate(_login_owner(), BRANCH_A_ID)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body.get("granted_switcher_to_users", 0) >= 0

    # Verify group + status.
    async def _probe():
        client, db = await _db()
        try:
            c = await db.clinics.find_one({"clinic_id": BRANCH_A_ID}, {"_id": 0, "status": 1, "deactivated_at": 1})
            g = await db.clinic_groups.find_one({"group_id": GROUP_ID}, {"_id": 0, "member_clinic_ids": 1})
            return c, g
        finally:
            client.close()

    c, g = _run(_probe())
    assert c["status"] == "active"
    assert "deactivated_at" not in c
    assert BRANCH_A_ID in g["member_clinic_ids"]


def test_reactivated_branch_reappears_in_my_clinics():
    """15 · After reactivation, /auth/my-clinics lists the branch again."""
    tok = _login_owner()
    _deactivate(tok, BRANCH_A_ID)
    _reactivate(_login_owner(), BRANCH_A_ID)
    fresh = _login_owner()
    r = requests.get(f"{API}/auth/my-clinics", headers=H(fresh), timeout=15)
    assert r.status_code == 200
    ids = [c["clinic_id"] for c in r.json()["clinics"]]
    assert BRANCH_A_ID in ids, ids


def test_reactivation_idempotent_on_already_active():
    """16 · Second call on an already-active branch → {ok:true, already_active:true}."""
    tok = _login_owner()
    r = _reactivate(tok, BRANCH_A_ID)  # BRANCH-A is already active (cleanup runs before every test)
    assert r.status_code == 200, r.text
    assert r.json().get("already_active") is True


def test_reactivate_foreign_branch_rejected():
    """17 · Head of Group-A cannot reactivate a branch of Group-B → 404."""
    foreign_tok = _login_foreign_owner()
    r = _reactivate(_login_owner(), FOREIGN_BRANCH_ID)
    assert r.status_code == 404, r.text
    # Foreign owner (correct group) can reactivate it — sanity.
    r2 = _reactivate(foreign_tok, FOREIGN_BRANCH_ID)
    assert r2.status_code == 200


def test_reactivation_does_not_resurrect_manually_deactivated_user():
    """18 · A user manually deactivated before or during branch deactivation
    MUST stay inactive after reactivation."""

    async def _get_active(email: str) -> bool:
        client, db = await _db()
        try:
            u = await db.users.find_one({"email": email}, {"_id": 0, "active": 1})
            return bool((u or {}).get("active"))
        finally:
            client.close()

    # Baseline: fixture already set this user active=False.
    assert _run(_get_active(DEACTIVATED_USER_EMAIL)) is False
    tok = _login_owner()
    _deactivate(tok, BRANCH_A_ID)
    _reactivate(_login_owner(), BRANCH_A_ID)
    assert _run(_get_active(DEACTIVATED_USER_EMAIL)) is False, \
        "reactivation resurrected a manually-deactivated user — anti-pattern breach"


def test_cross_tenant_isolation_still_intact():
    """19 · Deactivated-branch data is preserved server-side (audit) but
    unreachable via any auth path."""
    async def _count_patients(cid: str) -> int:
        client, db = await _db()
        try:
            return await db.patients.count_documents({"clinic_id": cid})
        finally:
            client.close()

    # Seed a marker patient in BRANCH-A.
    async def _seed_patient():
        client, db = await _db()
        try:
            await db.patients.update_one(
                {"patient_id": "PT-NAV007-A-1"},
                {"$setOnInsert": {
                    "patient_id": "PT-NAV007-A-1",
                    "clinic_id": BRANCH_A_ID,
                    "name": "NAV007 Test Patient",
                    "mrd": "NAV7A-0001",
                }},
                upsert=True,
            )
        finally:
            client.close()
    _run(_seed_patient())

    before = _run(_count_patients(BRANCH_A_ID))
    assert before >= 1

    tok = _login_owner()
    _deactivate(tok, BRANCH_A_ID)

    after = _run(_count_patients(BRANCH_A_ID))
    assert after == before, "deactivation must not delete patient data"

    # Access blocked via any auth path.
    fresh = _login_branch_a_user()
    r = requests.get(f"{API}/patients", headers=H(fresh), timeout=15)
    assert r.status_code == 401


def test_legacy_clinic_without_status_field_still_works():
    """20 · CRITICAL · Central auth gate must PASS legacy clinics that
    have no `status` field. Regression guard for 14/23 preview rows."""
    tok = _login_legacy()
    r = requests.get(f"{API}/auth/me", headers=H(tok), timeout=15)
    assert r.status_code == 200, r.text
    assert r.json()["user"]["clinic_id"] == LEGACY_ID


def test_my_clinics_no_longer_returns_active_field():
    """21 · Phantom `clinics.active` retired from the projection (B6).
    Response items should carry `status` instead."""
    tok = _login_owner()
    r = requests.get(f"{API}/auth/my-clinics", headers=H(tok), timeout=15)
    assert r.status_code == 200
    clinics = r.json()["clinics"]
    assert len(clinics) >= 1
    for c in clinics:
        assert "active" not in c, f"phantom `active` still present: {c}"
        # `status` is projected but may be None/absent for legacy rows —
        # only assert it isn't the retired name.


def test_multi_clinic_isolation_head_branch_a_branch_b():
    """22 · CRITICAL · Multi-clinic user with active sessions in Head +
    Branch A + Branch B. Deactivate Branch A. Branch A session → 401.
    Head session → still 200. Branch B session → still 200. Switching
    into Branch A → 403. Switching into Branch B → 200.

    This is the explicit invariant added in the Phase 2 approval note:
    deactivation must not collateral-damage a multi-clinic user's other
    active-clinic sessions.
    """
    # Baseline three sessions minted from three distinct logins.
    head_tok = _login_owner()  # scoped to HEAD by default
    switch_a = _switch(_login_owner(), BRANCH_A_ID)
    branch_a_tok = switch_a.json()["access_token"]
    switch_b = _switch(_login_owner(), BRANCH_B_ID)
    branch_b_tok = switch_b.json()["access_token"]

    # All three work.
    assert requests.get(f"{API}/auth/me", headers=H(head_tok), timeout=15).status_code == 200
    assert requests.get(f"{API}/auth/me", headers=H(branch_a_tok), timeout=15).status_code == 200
    assert requests.get(f"{API}/auth/me", headers=H(branch_b_tok), timeout=15).status_code == 200

    # Deactivate Branch A.
    _deactivate(_login_owner(), BRANCH_A_ID)

    # Post-deactivation:
    #  • HEAD session → still 200
    #  • BRANCH-A session → 401 (via B1)
    #  • BRANCH-B session → still 200
    assert requests.get(f"{API}/auth/me", headers=H(head_tok), timeout=15).status_code == 200
    assert requests.get(f"{API}/auth/me", headers=H(branch_a_tok), timeout=15).status_code == 401
    assert requests.get(f"{API}/auth/me", headers=H(branch_b_tok), timeout=15).status_code == 200

    # Switch attempts:
    #  • Switching into BRANCH-A (after re-adding to extras to reach the
    #    allowlist gate) → 403.
    #  • Switching into BRANCH-B → 200.
    async def _readd_a():
        client, db = await _db()
        try:
            await db.users.update_one(
                {"email": OWNER_EMAIL},
                {"$addToSet": {"additional_clinic_ids": BRANCH_A_ID}},
            )
        finally:
            client.close()
    _run(_readd_a())
    fresh = _login_owner()
    r_a = _switch(fresh, BRANCH_A_ID)
    assert r_a.status_code == 403, r_a.text
    r_b = _switch(fresh, BRANCH_B_ID)
    assert r_b.status_code == 200, r_b.text
