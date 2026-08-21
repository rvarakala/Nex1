"""NAV-012 · Idempotency-Key utility.

Design summary (see /app/memory/PRD.md NAV-012 closure block after deploy):

* Optional `Idempotency-Key` header on `POST /billing/invoices/{id}/payments`,
  `POST /billing/invoices/{id}/refund`, and `POST /referral-partners/{pid}/
  payouts`.
* Header format: `[A-Za-z0-9_\\-]{8,128}`. Missing → transparent no-op.
* Uniqueness scope: `(clinic_id, scope, idempotency_key)` — enforced by
  a partial-unique compound index on `db.idempotency_keys`.
* State machine:  `in_flight` → `completed` | `failed`.
* Replay: both HTTP status AND body are cached and replayed byte-for-byte.
  Replayed responses carry the `Idempotency-Replay: true` header.
* Concurrency: the UNIQUE index arbitrates. Second live arriver receives
  409. A `completed`/`failed` record replays. A stale `in_flight` is
  resolved by looking up the pre-registered `operation_ref` to detect
  whether the business row landed:
    - business row EXISTS  → rebuild response, flip to `completed`,
                             replay to caller. NO second financial write.
    - business row MISSING → CAS-takeover the slot with a fresh
                             `created_at` + `correlation_id`; caller
                             retries the business op with the new corr.
    - takeover LOST        → someone else already took the slot →
                             409, log, do not touch money.
* Crash safety: business ops embed the pre-registered `correlation_id`
  as `idempotency_correlation_id` on the created row, so recovery is
  deterministic and does not rely on the natural 24h TTL.
* TTL: 24h on `expires_at` via a TTL index.  A stale record surfaced
  BEFORE the TTL sweep is handled through the operation-exists check.

The utility DOES NOT modify existing behaviour when the header is
absent; every route that opts in must check `IdempotencyContext.enabled`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

try:
    from bson import ObjectId
except ImportError:  # pragma: no cover
    ObjectId = None  # type: ignore

_log = logging.getLogger(__name__)

# Key format — Stripe-compatible. Reject anything else early.
_KEY_RE = re.compile(r"^[A-Za-z0-9_\-]{8,128}$")

# Any `in_flight` record older than this is considered a crash / abandoned
# request and eligible for the operation-exists check + CAS takeover.
# Chosen well above any realistic FastAPI + Motor billing request latency.
STALE_THRESHOLD_SECONDS = 90

# TTL for the idempotency record itself (from create_at) — 24 hours.
TTL_SECONDS = 24 * 3600

SUPPORTED_SCOPES = (
    "payment",
    "refund",
    "payout",
    "advance_receipt",
    # NAV-011 · Phase 2B.1 · Scope reserved for the (future) Phase 2B.2
    # allocation writer. Adding it here is a pure schema/registration
    # change — no router uses this scope yet, so `IdempotencyContext.
    # enter(scope="advance_allocation", ...)` cannot be triggered by
    # any live endpoint until Phase 2B.2 lands.
    "advance_allocation",
)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _parse_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _strip_mongo(value):
    """Recursively convert Mongo-native types (ObjectId, datetime) to
    JSON-safe types, and drop residual ``_id`` fields introduced by
    ``$push``-generated sub-documents.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if ObjectId is not None and isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat() if value.tzinfo else value.replace(tzinfo=timezone.utc).isoformat()
    if isinstance(value, list):
        return [_strip_mongo(v) for v in value]
    if isinstance(value, dict):
        return {k: _strip_mongo(v) for k, v in value.items() if k != "_id"}
    return value


def _canonical_hash(payload: Any) -> str:
    """SHA-256 of the payload dict, canonicalised so key order and
    whitespace do not create false payload mismatches on replay.
    """
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"),
        default=str, ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def extract_idempotency_key(request: Optional[Request]) -> Optional[str]:
    """Return the validated key or `None` if absent.  Raises 400 on a
    malformed key (never allow a garbage key to silently disable
    protection).
    """
    if request is None:
        return None
    raw = request.headers.get("Idempotency-Key") or request.headers.get("idempotency-key")
    if raw is None or raw == "":
        return None
    raw = raw.strip()
    if not _KEY_RE.match(raw):
        raise HTTPException(
            status_code=400,
            detail="Idempotency-Key must match [A-Za-z0-9_-]{8,128}",
        )
    return raw


@dataclass
class IdempotencyContext:
    """Manages the lifecycle of a single idempotent operation.

    Typical usage in a route handler::

        idem = await IdempotencyContext.enter(
            request, db,
            scope="payment", clinic_id=user["clinic_id"],
            actor=user,
            payload=payload.model_dump(),
            route="/api/billing/invoices/{invoice_id}/payments",
            operation_collection="payments",
            operation_field="idempotency_correlation_id",
        )
        if idem.replayed:
            return idem.replay_response()
        try:
            # ...perform the business op, embedding idem.correlation_id
            #    into the created row...
            body = ...serialised response...
            await idem.complete(http_status=200, response_body=body,
                                operation_id=<actual business id>)
            return body
        except HTTPException as exc:
            await idem.fail(http_status=exc.status_code,
                            response_body={"detail": exc.detail})
            raise
    """

    db: Any
    enabled: bool
    scope: str
    clinic_id: str
    key: Optional[str]
    route: str
    correlation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    operation_collection: Optional[str] = None
    operation_field: str = "idempotency_correlation_id"
    request_hash: Optional[str] = None
    actor: dict = field(default_factory=dict)
    # Replay state — populated only when this key already ran.
    replayed: bool = False
    replay_http_status: Optional[int] = None
    replay_body: Any = None
    replay_reason: Optional[str] = None
    # Book-keeping.
    created_at: Optional[datetime] = None
    reclaimed: bool = False

    # ─── Public entry point ───────────────────────────────────────────
    @classmethod
    async def enter(
        cls,
        request: Optional[Request],
        db: Any,
        *,
        scope: str,
        clinic_id: str,
        actor: dict,
        payload: Any,
        route: str,
        operation_collection: str,
        operation_field: str = "idempotency_correlation_id",
    ) -> "IdempotencyContext":
        if scope not in SUPPORTED_SCOPES:
            raise ValueError(f"Unsupported idempotency scope: {scope!r}")
        key = extract_idempotency_key(request)
        if key is None:
            return cls(
                db=db, enabled=False, scope=scope, clinic_id=clinic_id,
                key=None, route=route,
                operation_collection=operation_collection,
                operation_field=operation_field,
                actor=actor,
            )
        request_hash = _canonical_hash(payload)
        ctx = cls(
            db=db, enabled=True, scope=scope, clinic_id=clinic_id,
            key=key, route=route, request_hash=request_hash,
            operation_collection=operation_collection,
            operation_field=operation_field,
            actor={
                "user_id": actor.get("user_id"),
                "name": actor.get("name"),
                "role": actor.get("role"),
            },
        )
        await ctx._acquire_or_replay()
        return ctx

    # ─── Acquire / replay flow ────────────────────────────────────────
    async def _acquire_or_replay(self) -> None:
        now = _now_utc()
        doc = {
            "clinic_id": self.clinic_id,
            "idempotency_key": self.key,
            "scope": self.scope,
            "route": self.route,
            "request_hash": self.request_hash,
            "status": "in_flight",
            "http_status": None,
            "response_body": None,
            "operation_ref": {
                "collection": self.operation_collection,
                "field": self.operation_field,
                "value": self.correlation_id,
            },
            "created_at": _iso(now),
            "completed_at": None,
            "expires_at": now + timedelta(seconds=TTL_SECONDS),
            "actor": self.actor,
            "failure": None,
        }
        try:
            await self.db.idempotency_keys.insert_one(dict(doc))
            self.created_at = now
            return
        except DuplicateKeyError:
            pass

        # A record with this key already exists — decide replay / 409 / takeover.
        existing = await self.db.idempotency_keys.find_one(
            {"clinic_id": self.clinic_id, "scope": self.scope,
             "idempotency_key": self.key},
            {"_id": 0},
        )
        if existing is None:
            # TTL swept between DuplicateKeyError and this read — retry once.
            try:
                await self.db.idempotency_keys.insert_one(dict(doc))
                self.created_at = now
                return
            except DuplicateKeyError:
                # Someone re-created it in the last microsecond → serve 409.
                raise HTTPException(
                    status_code=409,
                    detail="Idempotency-Key operation still in progress; retry shortly",
                )

        # Payload-mismatch guard is unconditional (Stripe-compatible).
        if existing.get("request_hash") != self.request_hash:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Idempotency-Key reused with a different payload — "
                    "the same key must always carry the same request body."
                ),
            )

        status = existing.get("status")
        if status in ("completed", "failed"):
            self.replayed = True
            self.replay_http_status = int(existing.get("http_status") or 200)
            self.replay_body = existing.get("response_body")
            self.replay_reason = status
            return

        # status == "in_flight" — inspect age.
        existing_created = _parse_dt(existing.get("created_at")) or now
        age_seconds = (now - existing_created).total_seconds()
        if age_seconds < STALE_THRESHOLD_SECONDS:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Idempotency-Key operation still in progress; "
                    f"retry after {STALE_THRESHOLD_SECONDS - int(age_seconds)} s"
                ),
            )

        # ── Stale in_flight — attempt crash recovery. ────────────────
        op_ref = existing.get("operation_ref") or {}
        landed = await self._business_op_exists(op_ref)
        if landed is True:
            # The prior request DID commit; rebuild a minimal response
            # and flip to completed. No second financial write happens.
            rebuilt_body, rebuilt_status = await self._rebuild_response(op_ref)
            flip_res = await self.db.idempotency_keys.find_one_and_update(
                {"clinic_id": self.clinic_id, "scope": self.scope,
                 "idempotency_key": self.key, "status": "in_flight"},
                {"$set": {
                    "status": "completed",
                    "http_status": rebuilt_status,
                    "response_body": rebuilt_body,
                    "completed_at": _iso(_now_utc()),
                    "failure": None,
                }},
                return_document=ReturnDocument.AFTER,
            )
            self.replayed = True
            self.replay_http_status = rebuilt_status
            self.replay_body = rebuilt_body
            self.replay_reason = "recovered_completed"
            _log.warning(
                "NAV-012 idempotency crash-recovery: business op existed for "
                "key=%s scope=%s clinic=%s corr=%s → rebuilt+completed",
                self.key, self.scope, self.clinic_id, op_ref.get("value"),
            )
            return
        if landed is False:
            # Business op never landed. Try to seize the slot atomically.
            new_now = _now_utc()
            new_corr = uuid.uuid4().hex
            takeover = await self.db.idempotency_keys.find_one_and_update(
                {"clinic_id": self.clinic_id, "scope": self.scope,
                 "idempotency_key": self.key, "status": "in_flight",
                 "created_at": existing.get("created_at")},
                {"$set": {
                    "created_at": _iso(new_now),
                    "expires_at": new_now + timedelta(seconds=TTL_SECONDS),
                    "operation_ref.value": new_corr,
                    "actor": self.actor,
                    "request_hash": self.request_hash,
                }},
                return_document=ReturnDocument.AFTER,
            )
            if takeover is None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Idempotency-Key operation still in progress; "
                        "retry shortly"
                    ),
                )
            self.correlation_id = new_corr
            self.created_at = new_now
            self.reclaimed = True
            _log.warning(
                "NAV-012 idempotency crash-recovery: business op MISSING for "
                "key=%s scope=%s clinic=%s → CAS takeover with corr=%s",
                self.key, self.scope, self.clinic_id, new_corr,
            )
            return

        # landed is None → ambiguous. Refuse to write money.
        _log.error(
            "NAV-012 idempotency crash-recovery: AMBIGUOUS state for "
            "key=%s scope=%s clinic=%s — refusing to execute financial op",
            self.key, self.scope, self.clinic_id,
        )
        raise HTTPException(
            status_code=409,
            detail=(
                "Prior request state is ambiguous; refusing to execute again. "
                "Please contact support with this idempotency key."
            ),
        )

    async def _business_op_exists(self, op_ref: dict) -> Optional[bool]:
        """Return True if the previously-registered business row exists,
        False if it definitely does not exist, or None if we cannot tell.
        """
        coll = op_ref.get("collection")
        field_name = op_ref.get("field")
        value = op_ref.get("value")
        if not coll or not field_name or not value:
            return None  # never got far enough to register a ref
        try:
            row = await self.db[coll].find_one(
                {field_name: value, "clinic_id": self.clinic_id},
                {"_id": 0, field_name: 1},
            )
            return row is not None
        except Exception:
            _log.exception(
                "NAV-012 idempotency crash-recovery: business op lookup "
                "failed for coll=%s field=%s value=%s",
                coll, field_name, value,
            )
            return None

    async def _rebuild_response(self, op_ref: dict) -> tuple[Any, int]:
        """Reconstruct a minimal successful response from the persisted
        business row.  Used only during crash-recovery replay when the
        prior request's response body was never cached.  Output is
        JSON-safe (datetimes → ISO strings, ObjectIds stripped) so
        replay flows through `JSONResponse` unchanged.
        """
        coll = op_ref["collection"]
        field_name = op_ref["field"]
        value = op_ref["value"]
        row = await self.db[coll].find_one(
            {field_name: value, "clinic_id": self.clinic_id},
            {"_id": 0},
        )
        if coll == "payments":
            invoice_id = row.get("invoice_id") if row else None
            if invoice_id:
                inv = await self.db.invoices.find_one(
                    {"invoice_id": invoice_id, "clinic_id": self.clinic_id},
                    {"_id": 0},
                )
                if inv:
                    return _strip_mongo(inv), 200
        if coll == "partner_payouts":
            if row:
                return _strip_mongo(row), 200
        if coll == "advance_allocations":
            # NAV-011 · Phase 2B.2 · Crash-recovery rebuild for the
            # allocation writer. Return the persisted allocation row —
            # the client can re-derive fresh invoice / advance snapshots
            # via the normal read endpoints if it needs them.
            if row:
                return _strip_mongo(row), 200
        return (_strip_mongo(row or {}), 200)

    # ─── Finalisers ───────────────────────────────────────────────────
    async def complete(
        self,
        *,
        http_status: int,
        response_body: Any,
        operation_id: Optional[str] = None,
    ) -> None:
        if not self.enabled:
            return
        # Coerce to JSON-safe types so replay can round-trip through
        # `JSONResponse` byte-for-byte.
        safe_body = _strip_mongo(jsonable_encoder(response_body))
        await self.db.idempotency_keys.update_one(
            {"clinic_id": self.clinic_id, "scope": self.scope,
             "idempotency_key": self.key},
            {"$set": {
                "status": "completed",
                "http_status": int(http_status),
                "response_body": safe_body,
                "completed_at": _iso(_now_utc()),
                "operation_ref.id_value": operation_id,
                "failure": None,
            }},
        )

    async def fail(
        self,
        *,
        http_status: int,
        response_body: Any,
        detail: Optional[str] = None,
    ) -> None:
        if not self.enabled:
            return
        safe_body = _strip_mongo(jsonable_encoder(response_body))
        await self.db.idempotency_keys.update_one(
            {"clinic_id": self.clinic_id, "scope": self.scope,
             "idempotency_key": self.key},
            {"$set": {
                "status": "failed",
                "http_status": int(http_status),
                "response_body": safe_body,
                "completed_at": _iso(_now_utc()),
                "failure": {"detail": detail or ""},
            }},
        )

    def replay_response(self) -> tuple[Any, int, dict]:
        """Return `(body, status_code, extra_headers)` for a replayed
        request. The route wrapper is responsible for building the
        actual `Response`/`JSONResponse` object.
        """
        return (
            self.replay_body,
            int(self.replay_http_status or 200),
            {"Idempotency-Replay": "true"},
        )


__all__ = [
    "IdempotencyContext",
    "extract_idempotency_key",
    "STALE_THRESHOLD_SECONDS",
    "TTL_SECONDS",
    "SUPPORTED_SCOPES",
]
