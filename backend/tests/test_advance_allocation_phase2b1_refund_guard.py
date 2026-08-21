"""Advance Allocation · Phase 2B.1 · SAFETY-CORRECTION regression suite.

Strict scope for this file (a single controlled checkpoint):
    * Prove `POST /api/billing/invoices/{id}/refund` rejects
      `method="advance"` with a 400 BEFORE any invoice / idempotency
      side-effect is triggered.
    * Prove all pre-existing legitimate refund methods (`cash`, `upi`,
      `card`, `bank_transfer`, `insurance`) still pass the router
      guard — the endpoint returns 404 (invoice not found) for a
      bogus invoice_id, proving the guard fires ONLY for "advance".
    * Prove the fix is a pure ROUTER-level fail-fast: the `RefundCreate`
      Pydantic model itself still admits `"advance"` so the (future)
      Phase 2B.2 allocation-void writer can emit compensating refunds.

Do NOT expand this file with allocation-engine coverage. This is a
controlled safety-checkpoint only.
"""
from __future__ import annotations

import sys
import pathlib
import uuid

import pytest
import requests

sys.path.append(str(pathlib.Path(__file__).resolve().parents[1]))
from _helpers import API, ACCOUNTS_EMAIL, ACCOUNTS_PASSWORD, H, login  # noqa: E402
from models._canonical import RefundCreate  # noqa: E402


@pytest.fixture(scope="module")
def accounts_token() -> str:
    return login(ACCOUNTS_EMAIL, ACCOUNTS_PASSWORD)


def _bogus_invoice_id() -> str:
    # Deterministically shaped ID that will NOT exist in any tenant.
    return f"INV-2B1-SAFETY-{uuid.uuid4().hex[:12].upper()}"


# ─────────────────────────────────────────────────────────────────────
# 1) Router-level guard blocks method="advance" with 400
# ─────────────────────────────────────────────────────────────────────

def test_manual_refund_rejects_method_advance(accounts_token):
    """Manual refund POST with method='advance' MUST fail-fast with 400
    BEFORE any invoice lookup or idempotency-key write."""
    r = requests.post(
        f"{API}/billing/invoices/{_bogus_invoice_id()}/refund",
        headers=H(accounts_token),
        json={
            "amount": 100.0,
            "method": "advance",
            "reason": "attempted smuggle",
        },
        timeout=15,
    )
    assert r.status_code == 400, (
        f"Expected 400 for method='advance', got {r.status_code}: {r.text[:400]}"
    )
    detail = (r.json().get("detail") or "").lower()
    assert "advance" in detail
    assert "reserved" in detail or "cannot" in detail, r.text


def test_manual_refund_rejects_method_advance_with_idempotency_key(accounts_token):
    """Guard MUST fire even when a valid Idempotency-Key is present —
    confirms the key is NOT consumed on a rejected `advance` refund."""
    key = f"ref-safety-{uuid.uuid4().hex[:16]}"
    r = requests.post(
        f"{API}/billing/invoices/{_bogus_invoice_id()}/refund",
        headers={**H(accounts_token), "Idempotency-Key": key},
        json={
            "amount": 100.0,
            "method": "advance",
            "reason": "attempted smuggle with idem key",
        },
        timeout=15,
    )
    assert r.status_code == 400, r.text
    # Second attempt with the SAME key must ALSO be rejected fresh
    # (i.e. the key was not consumed) — confirms fail-fast placement.
    r2 = requests.post(
        f"{API}/billing/invoices/{_bogus_invoice_id()}/refund",
        headers={**H(accounts_token), "Idempotency-Key": key},
        json={
            "amount": 100.0,
            "method": "advance",
            "reason": "attempted smuggle with idem key",
        },
        timeout=15,
    )
    assert r2.status_code == 400, r2.text
    # No replay header should be present (key wasn't stored on first call).
    assert r2.headers.get("Idempotency-Replay") is None


# ─────────────────────────────────────────────────────────────────────
# 2) Legitimate methods still pass the router guard
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("method", ["cash", "upi", "card", "bank_transfer", "insurance"])
def test_manual_refund_allows_all_legacy_methods(accounts_token, method):
    """For every legacy method the router guard MUST NOT fire — the
    endpoint proceeds to the invoice existence check and returns 404
    for a bogus invoice_id. This proves the guard is method-specific."""
    r = requests.post(
        f"{API}/billing/invoices/{_bogus_invoice_id()}/refund",
        headers=H(accounts_token),
        json={
            "amount": 100.0,
            "method": method,
            "reason": "regression probe",
        },
        timeout=15,
    )
    assert r.status_code == 404, (
        f"Legacy method={method!r} unexpectedly blocked/succeeded: "
        f"{r.status_code}: {r.text[:400]}"
    )
    detail = (r.json().get("detail") or "").lower()
    assert "not found" in detail or "invoice" in detail, r.text


# ─────────────────────────────────────────────────────────────────────
# 3) Pydantic-level surface is unchanged (allocation-void writer path
#    remains open to emit method="advance" refunds in Phase 2B.2).
# ─────────────────────────────────────────────────────────────────────

def test_refund_create_pydantic_still_accepts_advance_method():
    """`RefundCreate` itself MUST still validate `method='advance'` so
    the (future) allocation-void writer can build the same payload
    internally. The guard is intentionally at the ROUTER layer only."""
    body = RefundCreate(method="advance", amount=1.0, reason="Void allocation")
    assert body.method == "advance"


def test_refund_create_pydantic_still_accepts_all_legacy_methods():
    for m in ("cash", "upi", "card", "bank_transfer", "insurance"):
        body = RefundCreate(method=m, amount=1.0, reason="probe")
        assert body.method == m
