"""NAV-008 · Counter Reconciliation Script

Purpose
-------
The Preview NAV-008 audit surfaced a class of duplicates caused by
invoice writers that DID NOT increment `db.counters._id="invoice:
{clinic_id}:{year}"` when assigning invoice numbers. Concrete examples:

  * `scripts/seed_demo_premium.py` (fixed in the same NAV-008 sprint
    but historical seed runs left the counter behind).
  * The retired uuid-hex generators in `ha_custom_ha_orders.py` and
    `ha_ear_moulds.py` (also fixed in the same sprint).
  * Ad-hoc migrations / raw admin inserts.

The consequence: a fresh call to `_next_invoice_no` after such a bypass
can hand out a number that was already used, producing the observed
Preview duplicate `INV/2026/000004`.

This script closes the underlying condition by advancing the counter
to `max(existing_decimal_seq_in_clinic_year)` for every clinic-year
observed in `db.invoices`. It uses `$max` (never lowers a counter) so
running it twice — or racing another instance — is safe.

Scope
-----
* READ-ONLY on `db.invoices`.
* Additive-only on `db.counters` — writes only via `$max` with
  `upsert=True`. Never lowers a counter, never deletes, never touches
  invoice_no values, never renumbers.
* Does NOT create the compound unique index (that lives in
  `server.py` startup).
* Does NOT touch `db.payments`, `db.ha_sales`, `db.activity_logs`,
  or any other collection.
* Does NOT touch invoices whose `invoice_no` is missing / null / in
  a non-canonical format (they are skipped, not modified).

Safety gate
-----------
Execution is refused unless the environment variable
`NAV008_MIGRATE=1` is set. This mirrors the NAV-007 kill-switch
convention so accidental runs are impossible.

Usage
-----
    # Dry-run (no writes at all — reports what would change):
    NAV008_MIGRATE=1 python backend/scripts/nav008_counter_reconcile.py --dry-run

    # Live run (still additive-only — cannot lower counters):
    NAV008_MIGRATE=1 python backend/scripts/nav008_counter_reconcile.py

Approvals
---------
Preview execution requires the standard NAV-008 preview sign-off.
Production execution requires an EXPLICIT separate authorization from
the operator (per NAV-008 Phase 3 stop condition).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from collections import defaultdict
from pathlib import Path


CANONICAL_INVOICE_NO_RE = re.compile(r"^INV/(\d{4})/(\d{6})$")


async def _run(dry_run: bool) -> int:
    if os.environ.get("NAV008_MIGRATE") != "1":
        print(
            "REFUSED · Set NAV008_MIGRATE=1 to authorise this migration.\n"
            "         (Additive-only counter sync. No invoice data touched.)"
        )
        return 2

    # Late import so `--help` works without touching Motor.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    from motor.motor_asyncio import AsyncIOMotorClient

    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ["DB_NAME"]]
    print(f"→ Connected to db={os.environ['DB_NAME']!r}")

    # Build max-seq map: {(clinic_id, year): max_seq}
    max_seq: dict[tuple[str, int], int] = defaultdict(int)
    non_canonical = 0
    missing_number = 0
    scanned = 0

    async for doc in db.invoices.find(
        {},
        {"_id": 0, "clinic_id": 1, "invoice_no": 1},
    ):
        scanned += 1
        ino = doc.get("invoice_no")
        cid = doc.get("clinic_id")
        if not ino or not cid:
            missing_number += 1
            continue
        m = CANONICAL_INVOICE_NO_RE.match(ino)
        if not m:
            non_canonical += 1
            continue
        year = int(m.group(1))
        seq = int(m.group(2))
        key = (cid, year)
        if seq > max_seq[key]:
            max_seq[key] = seq

    print(f"→ Scanned {scanned} invoice(s):")
    print(f"    canonical INV/YYYY/NNNNNN         : {sum(1 for _ in max_seq)}")
    print(f"    non-canonical (hex / IMP / other) : {non_canonical}  (skipped)")
    print(f"    missing invoice_no                : {missing_number}  (skipped)")
    print()

    if not max_seq:
        print("→ Nothing to reconcile. All done.")
        client.close()
        return 0

    # Report + apply.
    updated = 0
    unchanged = 0
    for (cid, year), seq in sorted(max_seq.items()):
        counter_key = f"invoice:{cid}:{year}"
        existing = await db.counters.find_one({"_id": counter_key}, {"_id": 0, "seq": 1})
        current = int((existing or {}).get("seq") or 0)
        if current >= seq:
            unchanged += 1
            print(f"    · {counter_key:<60} current={current:>6}  max_used={seq:>6}  → no-op")
            continue
        print(f"    · {counter_key:<60} current={current:>6}  max_used={seq:>6}  → advance to {seq}")
        if not dry_run:
            await db.counters.update_one(
                {"_id": counter_key},
                {"$max": {"seq": seq}},
                upsert=True,
            )
        updated += 1

    print()
    print(f"→ Reconciliation summary:")
    print(f"    counters advanced : {updated}")
    print(f"    counters unchanged: {unchanged}")
    print(f"    mode              : {'DRY-RUN (no writes)' if dry_run else 'LIVE'}")
    print()
    print("→ Post-run invariant: no counter is EVER lower than the highest")
    print("  canonical decimal invoice_no observed in its clinic-year. New")
    print("  invoices created via `_next_invoice_no` are therefore guaranteed")
    print("  to receive a higher number than any existing invoice.")
    client.close()
    return 0


def _main() -> int:
    ap = argparse.ArgumentParser(
        description="NAV-008 counter reconciliation (additive-only).",
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="Report changes without touching db.counters.")
    args = ap.parse_args()
    return asyncio.run(_run(dry_run=args.dry_run))


if __name__ == "__main__":
    raise SystemExit(_main())
