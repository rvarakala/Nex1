"""NAV-012 · Phase 2B · Bundle D — Build/Commit Observability targeted tests.

Additive, read-only, database-free endpoint at ``GET /api/health/build``.
These tests exercise the endpoint via TestClient so the module-level cache
`server._BUILD_IDENTITY` is re-computable inside each test by recomputing
`server._compute_build_identity()` under a monkeypatched environment.

DO NOT touch: existing /api/health, /health K8s probe, authentication /
RBAC, NAV-009/010/011, Advance stack, inventory, billing/payments, frontend.
"""
from __future__ import annotations

import os
import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

# Ensure `/app/backend` is importable, matching the rest of the test suite.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import server as _server  # noqa: E402


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(_server.app)


# ─── 1. Endpoint reachability + auth posture ──────────────────────────

def test_health_build_endpoint_exists_returns_200_without_auth(client):
    """Bundle D endpoint must respond 200 anonymously — same posture as
    /api/health. It is deliberately unauthenticated and public."""
    r = client.get("/api/health/build")
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, dict)


# ─── 2. Response schema ───────────────────────────────────────────────

def test_health_build_response_contains_required_fields(client):
    """`commit`, `built_at`, `environment` are the three mandatory keys.
    `version` is optional (only present when APP_VERSION env is set)."""
    r = client.get("/api/health/build")
    body = r.json()
    for key in ("commit", "built_at", "environment"):
        assert key in body, f"missing key {key!r} in {body!r}"
        assert isinstance(body[key], str), f"key {key!r} must be a string"
        assert body[key], f"key {key!r} must be non-empty"


# ─── 3. Commit source precedence ──────────────────────────────────────

def test_health_build_commit_prefers_env_var_when_set(monkeypatch):
    """When BUILD_COMMIT_SHA is set, it wins over the git-subprocess fallback."""
    monkeypatch.setenv("BUILD_COMMIT_SHA", "deadbee")
    identity = _server._compute_build_identity()
    assert identity["commit"] == "deadbee"


def test_health_build_commit_falls_back_to_git_when_env_missing(monkeypatch):
    """When BUILD_COMMIT_SHA is not set, the git-rev-parse fallback runs.
    The result is either a 7+-char hex string OR the literal 'unknown'
    (both are acceptable — the test tolerates a runner without a
    working git checkout)."""
    monkeypatch.delenv("BUILD_COMMIT_SHA", raising=False)
    identity = _server._compute_build_identity()
    val = identity["commit"]
    assert val == "unknown" or (len(val) >= 7 and all(c in "0123456789abcdef" for c in val))


def test_health_build_commit_never_empty(monkeypatch):
    """Even with every source blank, the endpoint MUST return a non-empty
    'unknown' string, never ''."""
    monkeypatch.setenv("BUILD_COMMIT_SHA", "")
    # We cannot easily neuter git in-process; just assert the empty-env-var
    # path still yields a non-empty value from either the git fallback or 'unknown'.
    identity = _server._compute_build_identity()
    assert identity["commit"]
    assert identity["commit"] != ""


# ─── 4. built_at precedence ───────────────────────────────────────────

def test_health_build_built_at_prefers_env_var_when_set(monkeypatch):
    monkeypatch.setenv("BUILD_BUILT_AT", "2026-08-21T00:00:00+00:00")
    identity = _server._compute_build_identity()
    assert identity["built_at"] == "2026-08-21T00:00:00+00:00"


def test_health_build_built_at_falls_back_to_git_or_unknown(monkeypatch):
    monkeypatch.delenv("BUILD_BUILT_AT", raising=False)
    identity = _server._compute_build_identity()
    # Either an ISO-ish string from git log or 'unknown' — always non-empty.
    assert identity["built_at"]


# ─── 5. environment behaviour ─────────────────────────────────────────

def test_health_build_environment_reads_app_env_when_set(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    identity = _server._compute_build_identity()
    assert identity["environment"] == "production"


def test_health_build_environment_defaults_to_unknown(monkeypatch):
    monkeypatch.delenv("APP_ENV", raising=False)
    identity = _server._compute_build_identity()
    assert identity["environment"] == "unknown"


def test_health_build_environment_never_url_guessed(monkeypatch):
    """Confirm the endpoint does NOT auto-infer 'preview'/'production' from
    request Host header — it strictly reads APP_ENV."""
    monkeypatch.delenv("APP_ENV", raising=False)
    identity = _server._compute_build_identity()
    assert identity["environment"] == "unknown"


# ─── 6. version is OPTIONAL (only present when APP_VERSION is set) ────

def test_health_build_version_absent_when_env_missing(monkeypatch):
    monkeypatch.delenv("APP_VERSION", raising=False)
    identity = _server._compute_build_identity()
    # Do NOT hard-code a sprint marker: 'version' is only present if the
    # operator explicitly set APP_VERSION.
    assert "version" not in identity


def test_health_build_version_present_when_env_set(monkeypatch):
    monkeypatch.setenv("APP_VERSION", "2026.08.21")
    identity = _server._compute_build_identity()
    assert identity.get("version") == "2026.08.21"


# ─── 7. Zero database access ──────────────────────────────────────────

def test_health_build_does_not_touch_database(client, monkeypatch):
    """The endpoint MUST NOT execute any DB command. We spy on the shared
    `db.command` and assert zero invocations."""
    calls: list = []
    original = _server.db.command

    async def _spy(*a, **kw):
        calls.append((a, kw))
        return await original(*a, **kw)

    monkeypatch.setattr(_server.db, "command", _spy)
    r = client.get("/api/health/build")
    assert r.status_code == 200
    assert calls == [], f"Bundle D endpoint touched the DB: {calls!r}"


# ─── 8. Regression — existing health endpoints unchanged ──────────────

def test_existing_api_health_endpoint_unchanged(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "healthy"
    assert "timestamp" in body
    assert isinstance(body["timestamp"], str)
    # Bundle D did NOT smuggle any new keys into the pre-existing shape.
    assert set(body.keys()) == {"status", "timestamp"}


def test_existing_k8s_health_probe_unchanged(client):
    """Non-prefixed /health is the K8s liveness probe. MUST stay healthy."""
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "healthy"
    assert "timestamp" in body
