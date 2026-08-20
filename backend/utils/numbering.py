"""Clinic-scoped, year-reset numbering helper.

Single source of truth for every sequential identifier in the app.
Uses the existing `counters` collection pattern already used by INV/YYYY/NNNNNN
and MRD (ACS-YYYY-NNNNNN) — adds the HA module's PO/GRN/TRIAL/JOB/SAL formats.

Usage:
    from utils.numbering import next_number
    po_no = await next_number(db, "po", clinic_id)   # -> "PO-2026-0017"
"""
from __future__ import annotations

from datetime import datetime

from pymongo import ReturnDocument

# Each entry: (prefix, width). `prefix` becomes part of the printed number;
# `width` is the zero-padded counter length. Separator is always `-`.
# Invoice + MRD stay on their legacy `/` format and are minted elsewhere.
_FORMATS: dict[str, tuple[str, int]] = {
    "po":     ("PO",     4),
    "grn":    ("GRN",    4),
    "trial":  ("TRIAL",  4),
    "job":    ("JOB",    4),
    "sale":   ("SAL",    4),
    "qte":    ("QTE",    4),
    "tradein": ("TI",    4),
    "courier": ("CSH",   4),
    "estimate": ("EST",  4),
    "approval": ("APR",  4),
    "amc":      ("AMC",  4),
    "payout":   ("PAY",  4),
    "recovery": ("REC",  4),
    "referral_event": ("REVT", 4),
}


async def next_number(db, kind: str, clinic_id: str, year: int | None = None) -> str:
    """Atomically increments the (kind, clinic, year) counter and returns the
    formatted identifier. Raises KeyError if `kind` isn't registered."""
    if kind not in _FORMATS:
        raise KeyError(f"Unknown numbering kind: {kind!r}. Known: {sorted(_FORMATS)}")
    prefix, width = _FORMATS[kind]
    yr = year or datetime.utcnow().year
    res = await db.counters.find_one_and_update(
        {"_id": f"{kind}:{clinic_id}:{yr}"},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    seq = (res or {}).get("seq", 1)
    return f"{prefix}-{yr}-{str(seq).zfill(width)}"
