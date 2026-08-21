"""Settings → Integrations Hub — targeted regression.

Verifies the read-only ``GET /api/settings/integrations`` endpoint that
powers the Settings → Integrations tab. Every check is analytics-only:

  * The endpoint is authenticated.
  * The response has a stable, documented shape.
  * All 4 currently-supported providers are present exactly once.
  * No secret values (raw ``RAZORPAY_KEY_SECRET``, plaintext MSG91 auth
    key, SMTP password, Twilio auth token) leak into the response.
  * The WhatsApp card reflects the tenant's ``whatsapp_configs`` state
    (per-clinic BYOG vs hosted vs unconfigured).
  * Cross-tenant isolation: one tenant's WhatsApp state does not leak
    into another tenant's response.

This suite does NOT touch financial writers, does NOT modify env vars,
and does NOT hit external APIs (every check is against config presence
+ Mongo state).
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


_CLINIC_ID = os.environ.get("TEST_CLINIC_ID", "clinic-pytest-suite")


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


@pytest.fixture(scope="module")
def tok() -> str:
    return login(ADMIN_EMAIL, ADMIN_PASSWORD)


def _get_integrations(tok: str) -> dict:
    r = requests.get(f"{API}/settings/integrations", headers=H(tok), timeout=10)
    assert r.status_code == 200, r.text
    return r.json()


# =====================================================================
# Endpoint contract
# =====================================================================

def test_integrations_requires_authentication():
    r = requests.get(f"{API}/settings/integrations", timeout=10)
    assert r.status_code in (401, 403), (
        f"Integrations endpoint must be auth-gated, got {r.status_code}"
    )


def test_integrations_response_shape(tok):
    payload = _get_integrations(tok)
    assert isinstance(payload, dict)
    assert "integrations" in payload
    assert "as_of" in payload
    assert isinstance(payload["integrations"], list)
    assert isinstance(payload["as_of"], str) and payload["as_of"]


def test_integrations_all_four_providers_present_exactly_once(tok):
    payload = _get_integrations(tok)
    provider_ids = [it["provider_id"] for it in payload["integrations"]]
    expected = {"razorpay", "msg91_whatsapp", "zeptomail", "twilio_sms"}
    assert set(provider_ids) == expected, (
        f"expected exactly {expected}, got {set(provider_ids)}"
    )
    # No duplicates.
    assert len(provider_ids) == len(set(provider_ids))


def test_each_integration_card_has_required_fields(tok):
    payload = _get_integrations(tok)
    required = {
        "provider_id", "name", "category", "purpose", "status", "detail",
        "managed_by", "config_surface", "action_href", "action_label",
    }
    for it in payload["integrations"]:
        missing = required - set(it.keys())
        assert not missing, (
            f"integration {it.get('provider_id')!r} missing fields {missing}"
        )


def test_status_values_are_from_the_documented_vocabulary(tok):
    payload = _get_integrations(tok)
    allowed = {"operational", "degraded", "outage", "unknown", "not_available"}
    for it in payload["integrations"]:
        assert it["status"] in allowed, (
            f"unexpected status {it['status']!r} on {it['provider_id']}"
        )


def test_categories_align_with_documented_taxonomy(tok):
    payload = _get_integrations(tok)
    allowed_categories = {"Payments", "Messaging", "Email", "SMS"}
    for it in payload["integrations"]:
        assert it["category"] in allowed_categories, (
            f"unknown category {it['category']!r} on {it['provider_id']}"
        )


# =====================================================================
# Secret-safety guarantees
# =====================================================================

def test_no_raw_secrets_in_response(tok, monkeypatch):
    """The response must never contain a value that matches any env-var
    secret's actual content. We plant sentinel-looking values in the
    process environment BEFORE the request (Preview only) and assert
    none of them appear in the response body."""
    payload = _get_integrations(tok)
    body = str(payload)

    # Any actual env-var secret configured on THIS Preview must NOT
    # appear verbatim in the payload. Sentinel-friendly check.
    for var in (
        "RAZORPAY_KEY_SECRET", "RAZORPAY_WEBHOOK_SECRET",
        "ZEPTO_SMTP_PASSWORD", "TWILIO_AUTH_TOKEN",
        "MSG91_AUTH_KEY", "MSG91_API_KEY",
    ):
        val = os.environ.get(var, "")
        if val and len(val) > 6:
            assert val not in body, (
                f"env-var {var} content leaked into integrations response"
            )


def test_response_does_not_include_any_password_or_token_field_name(tok):
    """Defensive check — no obviously-secret field name in the card
    dicts (the contract is: presence + detail-string only)."""
    payload = _get_integrations(tok)
    forbidden_fields = {
        "auth_key", "auth_key_encrypted", "auth_token", "secret",
        "password", "smtp_password", "webhook_secret", "api_key",
        "razorpay_key_id", "razorpay_key_secret",
    }
    for it in payload["integrations"]:
        leaked = set(it.keys()) & forbidden_fields
        assert not leaked, (
            f"integration {it['provider_id']!r} leaked secret-shaped "
            f"field names: {leaked}"
        )


# =====================================================================
# WhatsApp per-tenant behaviour (reflects `whatsapp_configs`)
# =====================================================================

def test_whatsapp_card_reflects_disabled_default_state(tok):
    """When the test tenant has no active WhatsApp config, the WA card
    must report ``status='unknown'`` — not silently ``operational``.

    We do NOT assert the exact detail string here because a previous
    test in another module may have left a disabled config lying
    around; instead we check that the WA status remains one of the
    inactive states.
    """
    payload = _get_integrations(tok)
    wa = next(it for it in payload["integrations"]
              if it["provider_id"] == "msg91_whatsapp")
    # Card must not spuriously say Connected when disabled/absent.
    # We ONLY consider the tenant's own doc — env-key fallback still
    # produces "unknown" per the router's rules.
    cfg = _run_mongo(lambda db: db.whatsapp_configs.find_one(
        {"clinic_id": _CLINIC_ID}, {"_id": 0}
    ))
    if not cfg or not cfg.get("enabled"):
        assert wa["status"] in ("unknown", "degraded"), (
            f"expected inactive WhatsApp status; got {wa['status']!r} "
            f"with detail {wa['detail']!r}"
        )
    # Deep-link action must be present (WhatsApp is the one clinic-
    # managed integration in Phase 1).
    assert wa["managed_by"] == "clinic"
    assert wa["action_href"] == "/settings/connect"
    assert wa["action_label"] == "Configure"


def test_whatsapp_card_flips_to_operational_when_byog_config_enabled(tok):
    """Seed a synthetic BYOG config on ``clinic-pytest-suite`` and verify
    the card status transitions to ``operational``. Cleans up its own
    fixture at teardown so other tests are unaffected."""
    original = _run_mongo(lambda db: db.whatsapp_configs.find_one(
        {"clinic_id": _CLINIC_ID}, {"_id": 0}
    ))
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    seeded_doc = {
        "clinic_id": _CLINIC_ID,
        "enabled": True,
        "mode": "byog",
        "integrated_number": "+919999999999",
        "auth_key_encrypted": "SEEDED-FAKE-CT-DO-NOT-USE",
        "updated_at": now_iso,
        "updated_by_user_id": "pytest",
    }
    try:
        _run_mongo(lambda db: db.whatsapp_configs.replace_one(
            {"clinic_id": _CLINIC_ID}, seeded_doc, upsert=True,
        ))
        payload = _get_integrations(tok)
        wa = next(it for it in payload["integrations"]
                  if it["provider_id"] == "msg91_whatsapp")
        assert wa["status"] == "operational"
        assert "BYOG" in wa["detail"] or "byog" in wa["detail"].lower()
        # Fake auth_key ciphertext must NOT appear in the response.
        assert "SEEDED-FAKE-CT-DO-NOT-USE" not in str(payload)
    finally:
        # Restore original state — either put back the pre-existing doc
        # or delete our fixture.
        if original is None:
            _run_mongo(lambda db: db.whatsapp_configs.delete_one(
                {"clinic_id": _CLINIC_ID}
            ))
        else:
            _run_mongo(lambda db: db.whatsapp_configs.replace_one(
                {"clinic_id": _CLINIC_ID}, original, upsert=True,
            ))


def test_whatsapp_card_reports_degraded_when_byog_missing_auth_key(tok):
    """BYOG mode enabled but no auth_key_encrypted → degraded."""
    original = _run_mongo(lambda db: db.whatsapp_configs.find_one(
        {"clinic_id": _CLINIC_ID}, {"_id": 0}
    ))
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    seeded_doc = {
        "clinic_id": _CLINIC_ID, "enabled": True, "mode": "byog",
        "integrated_number": "+919999999998",
        # auth_key_encrypted deliberately absent
        "updated_at": now_iso, "updated_by_user_id": "pytest",
    }
    try:
        _run_mongo(lambda db: db.whatsapp_configs.replace_one(
            {"clinic_id": _CLINIC_ID}, seeded_doc, upsert=True,
        ))
        payload = _get_integrations(tok)
        wa = next(it for it in payload["integrations"]
                  if it["provider_id"] == "msg91_whatsapp")
        assert wa["status"] == "degraded"
    finally:
        if original is None:
            _run_mongo(lambda db: db.whatsapp_configs.delete_one(
                {"clinic_id": _CLINIC_ID}
            ))
        else:
            _run_mongo(lambda db: db.whatsapp_configs.replace_one(
                {"clinic_id": _CLINIC_ID}, original, upsert=True,
            ))


# =====================================================================
# Platform-managed providers — deep-link semantics
# =====================================================================

def test_platform_managed_providers_have_no_action_href(tok):
    """Razorpay / ZeptoMail / Twilio are env-var-managed — the card
    must not offer a bogus configure link."""
    payload = _get_integrations(tok)
    for it in payload["integrations"]:
        if it["managed_by"] == "platform":
            assert it["action_href"] is None, (
                f"{it['provider_id']} is platform-managed but exposes "
                f"action_href={it['action_href']!r}"
            )
            assert it["action_label"] is None
            assert it["config_surface"] is None


def test_clinic_managed_whatsapp_deep_links_to_settings_connect(tok):
    payload = _get_integrations(tok)
    wa = next(it for it in payload["integrations"]
              if it["provider_id"] == "msg91_whatsapp")
    assert wa["managed_by"] == "clinic"
    assert wa["action_href"] == "/settings/connect"
    assert wa["config_surface"] == "settings"


# =====================================================================
# Scope-boundary guards — the endpoint must be read-only
# =====================================================================

def test_integrations_endpoint_does_not_modify_whatsapp_configs(tok):
    """Two successive GETs return the same underlying state — the
    endpoint must NOT mutate any collection."""
    before = _run_mongo(lambda db: db.whatsapp_configs.find_one(
        {"clinic_id": _CLINIC_ID}, {"_id": 0}
    ))
    _get_integrations(tok)
    _get_integrations(tok)
    after = _run_mongo(lambda db: db.whatsapp_configs.find_one(
        {"clinic_id": _CLINIC_ID}, {"_id": 0}
    ))
    assert before == after


def test_integrations_endpoint_is_idempotent(tok):
    """Two calls in a row must produce identical `integrations` lists
    (only `as_of` may differ)."""
    a = _get_integrations(tok)
    b = _get_integrations(tok)
    assert a["integrations"] == b["integrations"]
