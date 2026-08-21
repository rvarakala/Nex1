# ADVANCE ALLOCATION · PHASE 1 · AUDIT ONLY

> **Read-only architectural blueprint. NO code, tests, DB migrations, indexes
> or deployments are produced by this document. Awaiting explicit approval
> before Phase 2B implementation begins.**
>
> Scope: how a Phase-2A `Advance Receipt` will safely allocate to a real
> `Invoice` (Phase 2B) without breaking money, GST, inventory, serial state
> or referral commissions.
>
> Style: recommend **ONE** architecture per section; no speculative
> alternatives unless a genuine decision requires them.

---

## 1. Current Architecture (as of Phase 2A · in production)

### 1.1 Advance Receipt (Phase 2A — receipt-only, deployed)
* Collection: `db.advance_receipts` (isolated, never touches invoices/payments/inventory).
* Numbering: `AR/YYYY/NNNNNN`, clinic-scoped, year-reset, via atomic `db.counters`.
* Fields of interest for allocation:
  * `receipt_id` (`AR-<uuid12>`), `receipt_no`, `clinic_id`, `branch_id`, `patient_id`
  * `received_amount` (float, > 0), `method`, `reference`, `purpose_note`
  * `status ∈ {active, voided}` (CAS-guarded transitions only)
  * `received_at`, `created_at`, `voided_at`, actor fields
* No `available_balance`, no `allocated_total`, no `allocations[]` — **this is
  the primary gap Phase 2B must close.**
* `POST /api/advance-receipts` requires mandatory `Idempotency-Key` (scope
  `advance_receipt` already registered in `SUPPORTED_SCOPES`).
* Audit trail: `db.advance_audit_events` (kind ∈ `{created, voided}`).

### 1.2 Invoice / Payment (billing.py — production)
* `db.invoices` carries `paid_total`, `refunded_total`, `due_total`, `status
  ∈ {draft, partial, paid, cancelled, refunded, partially_refunded}`, and
  embedded `payments[]` array. Aggregation-pipeline `find_one_and_update`
  is the sole atomic writer.
* `db.payments` is the top-level ledger (`payment_id`, `kind ∈ {payment,
  refund}`, `amount` — refunds stored NEGATIVE by convention).
* `record_payment_atomic()`:
  1. `db.payments.insert_one(...)` (top-level first);
  2. aggregation-pipeline conditional `find_one_and_update` on the invoice
     with `$expr` overpayment / refund guard;
  3. compensating `db.payments.delete_one(...)` on match failure.
* `record_refund_atomic()`: symmetric, decreases `paid_total`, increases
  `refunded_total`, re-derives status.
* Monetary tolerance: `MONEY_TOL = 0.01` everywhere.
* Auto-flip side-effects on `partial → paid` transition:
  * `routers.ha_sales.mark_sale_paid_internal(...)` (if `linked_sale_no`)
  * `utils.accessory_stock.auto_decrement_accessory_stock(...)` (if any
    invoice line has `accessory_stock_decremented != True`)
  * Both are wrapped in try/except; failure logged but non-fatal.

### 1.3 Referral Attribution (referral_partners.py)
* `_attribute_revenue()` computes commissionable revenue per invoice as
  `max(0, paid_total)` (NAV-011 rule — refunds are already netted into
  `paid_total` via NAV-009's negative-amount refund rows).
* HA-sale revenue is gated on the linked invoice's `paid_total > 0`.
* `partner_recovery_ledger` (Bundle 5) handles claw-back when refunds land
  after a payout has already been cut.

### 1.4 Serial-Item State Machine (utils/ha_states.py)
* 10 states; every transition via `transition_serial()` CAS + `serial_events`
  append-only audit.
* Money moves DO NOT alter serial state. Serial state is driven by the
  HA-sale / trial / loaner state machines, not by payment capture.

### 1.5 Idempotency (utils/idempotency.py)
* Compound uniqueness on `(clinic_id, scope, idempotency_key)`.
* Request-hash mismatch → 422; stale in-flight → operation-exists check +
  CAS takeover; completed/failed → byte-for-byte replay with header
  `Idempotency-Replay: true`.
* Currently supported scopes: `payment`, `refund`, `payout`, `advance_receipt`.
  Phase 2B must add `advance_allocation`.

### 1.6 Gaps that block Phase 2B
1. Advance Receipt has no balance ledger (`available_balance`,
   `allocated_total`).
2. No `advance_allocations` collection or state machine.
3. No `advance_allocation` scope in the idempotency utility.
4. `record_payment_atomic()` does not carry an `advance_receipt_id` /
   `allocation_id` correlation on the payment row → cannot trace the
   money back to the source advance.
5. Void of Advance Receipt is currently unconditional on
   `status=active`; must be re-guarded so a receipt with any live
   allocations cannot be voided.

---

## 2. Recommended Allocation Architecture (ONE model)

**Model: “Ledger-backed decrement + reuse of `record_payment_atomic`.”**

The advance is a *closed-loop* balance ledger. Each allocation:

1. **CAS-decrements** the advance receipt's `available_balance` (source of truth).
2. **Reuses** `record_payment_atomic()` to add exactly `amount` to the target
   invoice — same aggregation-pipeline update, same overpayment guard, same
   `db.payments` insert, same auto-flip side-effects.
3. Persists a **new ledger row in `db.advance_allocations`** carrying the
   dual reference `(advance_receipt_id, allocation_id, invoice_id,
   payment_id, correlation_id)` and a CAS-guarded `status`.
4. Writes an **`advance_audit_events`** row (`kind="allocated"`) and mirrors
   an entry into the existing `serial_events`-style audit if the target
   invoice's paid transition triggers HA / accessory side-effects.

### Why this model (single-decision rationale)

* **Zero downstream rewrites.** `_attribute_revenue()`, `mark_sale_paid_internal`,
  `auto_decrement_accessory_stock`, invoice status ladder, refund flow, GST
  breakdown — none of them change. Every existing invariant is preserved
  because the money still lands via `record_payment_atomic` and still
  bumps `paid_total` on the same collection.
* **Traceable.** The `payment` row carries `advance_receipt_id` +
  `allocation_id` fields; downstream reports (Payments & Refunds ledger,
  daily collections, GSTR-1) can filter by `method == "advance"` and
  identify origin advance in one join.
* **Financially safe.** Two CAS points (advance decrement, invoice payment
  guard) with a compensating rollback pattern that already exists in
  `record_payment_atomic`. No transactions required — works on standalone
  Mongo (Preview) and on replica-set (Production, once configured).
* **Reversible.** Void of an allocation flows the money back both ways —
  refund the invoice (`record_refund_atomic` with `method="advance"`) AND
  re-credit the advance (`$inc: available_balance +amt`).

### Rejected alternatives (only where a decision was genuinely required)

* **Direct edit of `advance_receipts.received_amount`** — rejected. Breaks
  auditability of "how much money did we take vs how much did we use".
* **Skipping `db.payments` and only writing to `advance_allocations`** —
  rejected. Would silently exclude allocated revenue from `_attribute_revenue`,
  GST reports, daily collections and the Payments & Refunds view.
* **Splitting each allocation into a per-invoice line-item on the advance
  receipt itself** — rejected. Nests financial state inside a document
  meant to be immutable-except-for-void; contradicts NAV-009 dual-write
  guarantees.

---

## 3. Financial Model

### 3.1 Ownership of money
Money flows through exactly three collections after Phase 2B is live:

| Stage                       | Collection             | Money-in-hand impact |
|-----------------------------|------------------------|-----------------------|
| Advance received            | `advance_receipts`     | `+received_amount`   |
| Allocation → Invoice        | `advance_allocations` + `payments` + `invoices` | `0` (internal move; total unchanged) |
| Refund of invoice           | `payments` (kind=refund, negative) + `invoices` | `-amount`            |
| Void of allocation          | `advance_allocations.status=voided` + refund row + `$inc available_balance +amt` | `0`  |
| Void of advance receipt     | `advance_receipts.status=voided` (only if `allocated_total == 0`) | undefined (see 3.4) |

### 3.2 Balance derivation on the advance receipt
* `available_balance = received_amount − allocated_total`
* `allocated_total = SUM(advance_allocations.amount WHERE status='active')`
* `total_refunded_from_advance` (informational only, Phase 2C) —
  populated only on future refund-to-advance flow. Out of scope for 2B.

### 3.3 Revenue recognition timing
* Advance received → **no revenue**. No GST. No commission.
* Allocation lands (paid_total increases) → **revenue recognised** on the
  target invoice at the invoice's GST posture, exactly as if a fresh
  cash/UPI payment had arrived. `_attribute_revenue` will pick it up on
  its next call.
* Refund on the invoice → revenue reversal via existing NAV-009 negative
  refund row.

### 3.4 Void of an Advance Receipt
* Phase 2A currently voids on `status=active`. Phase 2B **tightens** the
  CAS match:
  `{receipt_id, clinic_id, status: "active", allocated_total: 0}`.
* If `allocated_total > 0` → **409** with clear guidance to void the
  allocation(s) first.
* Void of an ADVANCE never touches money for the clinic — the received
  amount stays on the books until either (a) refunded via Phase 2C, or
  (b) allocated then refunded via the invoice refund flow. Phase 2A
  chose this deliberately; Phase 2B preserves it.

---

## 4. Data Model

### 4.1 Additive changes to `advance_receipts` (Phase 2B)
```
+ available_balance : float   # server-computed, initialised to received_amount
+ allocated_total   : float   # denormalised sum; kept in sync by CAS updates
```

* Backfill (one-time, controlled): for every existing row set
  `available_balance = received_amount`, `allocated_total = 0` for
  `status='active'`, and both to `0` for `status='voided'`.
* Ordering: the two fields are numeric mirrors of the ledger; the
  ledger (`advance_allocations`) remains the source of truth. If they
  ever diverge, the ledger wins during reconciliation.

### 4.2 New collection: `db.advance_allocations`
```
allocation_id                : "AA-<uuid12>"          (unique per clinic)
allocation_no                : "AA/YYYY/NNNNNN"       (atomic counter, clinic-scoped, year-reset)
clinic_id                    : str                    (tenant scope)
branch_id                    : Optional[str]

advance_receipt_id           : str                    (FK → advance_receipts.receipt_id)
advance_receipt_no           : str                    (denormalised for read paths)

invoice_id                   : str                    (FK → invoices.invoice_id)
invoice_no                   : str                    (denormalised)

patient_id                   : str                    (must match both parents)

amount                       : float, > 0             (partial allocations OK)
correlation_id               : str                    (from IdempotencyContext)
payment_id                   : Optional[str]          (FK → payments.payment_id, set post-CAS)

status                       : "active" | "voided"
created_at                   : ISO datetime (UTC)
created_by_user_id           : str
created_by_name              : Optional[str]

voided_at                    : Optional[ISO]
voided_by_user_id            : Optional[str]
voided_by_name               : Optional[str]
void_reason                  : Optional[str]
void_refund_payment_id       : Optional[str]          (payment.payment_id created by the void)
```

### 4.3 New collection: `db.advance_audit_events` (extended kinds)
Existing kinds: `created`, `voided`. Add:
* `allocated` — payload `{allocation_id, invoice_id, amount, remaining_balance}`
* `allocation_voided` — payload `{allocation_id, invoice_id, amount, refund_payment_id, reason}`

### 4.4 Extension to `db.payments` (nullable, additive)
```
+ advance_receipt_id : Optional[str]
+ allocation_id      : Optional[str]
```
Both populated only for allocation-sourced payments and their compensating
refunds. Report queries filter by `method == "advance"` OR by presence of
these two fields.

### 4.5 Indexes to be planned (NOT created in Phase 1)
Documented for the Phase 2B index-installer:
* `advance_allocations`:
  * unique `(clinic_id, allocation_id)`
  * unique `(clinic_id, allocation_no)`
  * `(clinic_id, advance_receipt_id, status)` — for balance recompute
  * `(clinic_id, invoice_id)` — for invoice→allocations view
* `payments`:
  * partial index `(clinic_id, advance_receipt_id)` where field exists
* `advance_audit_events`:
  * `(clinic_id, receipt_id, at)` — already implicit if not present.

---

## 5. Allocation State Machine

| Trigger                                            | From         | To         |
|----------------------------------------------------|--------------|------------|
| Create allocation (successful `record_payment_atomic`) | *(new row)* | `active`   |
| Void allocation (accounts / clinic_owner)          | `active`     | `voided`   |
| Void when already voided                           | `voided`     | *409*      |

* **Terminal:** `voided`. No `re-activate` transition.
* CAS invariant: transitions are `find_one_and_update` guarded on
  `(allocation_id, clinic_id, status="active")`.
* Concurrent void race → the loser gets `matched_count = 0` and a 409
  with the current state fetched for a helpful message.

## 6. Refund / Reversal State Machine

Two independently reversible paths — **do not conflate them**:

**Path A: Void the allocation** (money returns to the advance)
1. CAS: `advance_allocations.status: active → voided`.
2. `record_refund_atomic(invoice_id, amount, method="advance",
   reason="Advance allocation void: <reason>")`.
3. `advance_receipts.$inc({available_balance: +amt, allocated_total: -amt})`
   guarded on `{receipt_id, clinic_id, allocated_total: {$gte: amt}}`.
4. If any step fails after step 1, compensating rollback flips
   allocation back to `active` and returns 5xx; NO half-state.
5. Audit: `advance_audit_events kind="allocation_voided"`.

**Path B: Refund the invoice** (money leaves the clinic; the advance
 is not restored — the money was already recognised as spent by the
 patient on that invoice)
* Uses existing `record_refund_atomic` unchanged. Refund method is
  whatever the operator chose (cash/UPI/bank). No advance re-credit.
* Referral commission recovery ledger auto-emits per existing NAV-011
  rules — no new logic.

Rationale: separating the two paths keeps the accounting story clear —
"we took the money BACK from the patient" (Path B) ≠ "we re-directed
the advance elsewhere" (Path A).

---

## 7. Financial Invariants (must always hold)

Named for tests/monitoring. All amounts are within `MONEY_TOL = 0.01`.

| # | Invariant                                                                                 |
|---|-------------------------------------------------------------------------------------------|
| I1 | `available_balance == received_amount − allocated_total` (per receipt)                    |
| I2 | `allocated_total == SUM(advance_allocations.amount WHERE status='active')` (per receipt)  |
| I3 | For every `active` allocation: matching `payments` row exists with the same `allocation_id`, `amount`, `method="advance"`, `advance_receipt_id`. |
| I4 | For every `voided` allocation: an offsetting refund row exists in `payments` with `kind='refund'`, negative amount, `allocation_id` back-link. |
| I5 | `SUM(payments.amount WHERE method='advance' AND kind='payment')` on an invoice ≤ its `paid_total` at that moment. |
| I6 | `available_balance ≥ 0` always. Enforced by CAS `$gte` on decrement.                       |
| I7 | Voided advance receipt ⇒ `allocated_total == 0` AND `available_balance == received_amount` at the moment of void. |
| I8 | An invoice's `paid_total` INCLUDING allocation-sourced payments respects NAV-009 refunded-total / cancelled guards (no allocation to cancelled or refunded invoice). |
| I9 | Referral commission derived from allocation-sourced revenue exactly equals commission derived from a hypothetical direct-cash payment of the same amount at the same time. |
| I10 | Idempotency-Key replay of the same allocation request produces byte-identical response and zero additional money movement. |

---

## 8. Concurrency Strategy

Two hot paths — both handled by pure CAS (no multi-doc transactions).

### 8.1 Two concurrent allocations from the SAME advance receipt
* Attacker: two `POST /allocate` calls, receipt has ₹1000 balance, each
  requesting ₹800.
* Guard: `advance_receipts.find_one_and_update(
    {receipt_id, clinic_id, status:'active', available_balance:{$gte: amt}},
    {$inc:{available_balance:-amt, allocated_total:+amt}}, return_document=AFTER)`.
* Loser: `matched_count = 0` → 409 with current balance (fetched for message).
* Winner: proceeds to `record_payment_atomic`; if THAT fails, run
  compensating `$inc: {available_balance:+amt, allocated_total:-amt}` on
  the same doc. This second `$inc` cannot fail — the doc exists (we just
  updated it) and there is no `$gte` guard on rollback.

### 8.2 Concurrent allocation + invoice payment
* Guard: `record_payment_atomic` already CAS-checks `due_total ≥ amount`
  and `refunded_total ≤ 0`. The allocation flow just plugs into the
  existing guard — no new race.

### 8.3 Concurrent allocation + advance void
* Void CAS is `{status:'active', allocated_total: 0}`. Allocation CAS is
  `{status:'active', available_balance:{$gte:amt}}`. If void wins first,
  allocation's `status:'active'` predicate fails (status is now 'voided')
  → 409. If allocation wins first, void's `allocated_total: 0` predicate
  fails → 409. Both are safe.

### 8.4 Concurrent allocation-void + fresh invoice payment
* The invoice refund emitted by the allocation-void uses `record_refund
  _atomic`, which itself CAS-guards `paid_total ≥ amt`. If fresh cash
  arrives simultaneously, both operations succeed independently — the
  invoice ledger stays consistent.

**No multi-doc transactions required.** The compensating-delete pattern
already proven by NAV-009 covers every failure branch.

---

## 9. Idempotency Strategy

* Add `"advance_allocation"` to `utils/idempotency.py::SUPPORTED_SCOPES`.
* Route: `POST /api/advance-receipts/{receipt_id}/allocations` — **mandatory**
  `Idempotency-Key` header (same policy as advance-receipt CREATE; missing
  → 400).
* Route: `POST /api/advance-receipts/{receipt_id}/allocations/{allocation_id}/void`
  — **optional** header (same policy as payment / refund routes).
* `operation_collection = "advance_allocations"`, `operation_field =
  "idempotency_correlation_id"`. The pre-registered `correlation_id`
  will be stamped on the `advance_allocations` row (`idempotency_correlation_id`)
  AND on the corresponding `payments` row for double-anchor recovery.
* Crash-recovery rebuild: on stale `in_flight` with the business row
  present, `_rebuild_response` returns the full allocation doc + the
  updated invoice snapshot. Extend `_rebuild_response` accordingly
  (already handles `payments` and `partner_payouts` cases; add
  `advance_allocations`).
* Request-hash mismatch → 422 (Stripe-compatible), unchanged.
* TTL: 24h (existing default).

---

## 10. Inventory / Serial Interaction

* **Zero direct interaction.** Allocation is a MONEY move; serial-item
  state transitions happen at HA-sale / trial / loaner events, which
  occur at invoice creation time or later HA workflow steps.
* Indirect effect: an allocation that flips the invoice `partial → paid`
  triggers the existing auto-flip hooks:
  * `mark_sale_paid_internal(clinic_id, linked_sale_no, actor, invoice_no)`
  * `auto_decrement_accessory_stock(...)` (only for lines not already
    decremented at invoice-create time).
* These hooks live INSIDE `add_payment` today. Phase 2B's allocation
  endpoint MUST invoke the same hooks post-`record_payment_atomic` so a
  paid-via-allocation invoice behaves identically to a paid-via-cash
  invoice. Recommended: extract the two hook calls into a small helper
  in `billing.py` (`_on_paid_transition(...)`) and call it from both
  paths — but this refactor is **implementation-time only**, not part
  of this Phase 1 audit.
* Serial-item state machine (`ha_states.py`) remains untouched.

---

## 11. Invoice / Payment Interaction

* Target invoice must satisfy:
  * `clinic_id == advance.clinic_id`
  * `patient_id == advance.patient_id` (strict same-patient policy in 2B)
  * `status ∉ {cancelled, refunded, partially_refunded}` (existing NAV-012 F-15)
  * `due_total ≥ amount − MONEY_TOL` (existing NAV-009 CAS guard)
* Payment row shape emitted by allocation:
  ```
  method                = "advance"
  kind                  = "payment"
  amount                = +amt
  advance_receipt_id    = <FK>
  allocation_id         = <FK>
  received_by_user_id   = <actor>
  reference             = f"Allocation from {receipt_no}"   # optional convenience
  ```
* Existing endpoints that consume `db.payments` require **no change**
  (list_payments_and_refunds, collections_summary, referral attribution,
  daily KPIs) — they all treat `method` as an opaque enum.
* `PAYMENT_METHODS` catalogue in `models/_canonical.py` must gain
  `"advance"` — this is the ONLY schema-adjacent extension in Phase 2B.
  It must be additive; downstream consumers should already accept
  arbitrary strings for `method` (used in reporting only).

---

## 12. Revenue / GST Implications

* **Advance Receipt**: never in the GST base. Purpose is acknowledgment,
  not supply. Confirmed by the Phase 2A HTML template's explicit
  disclaimer.
* **Allocation to invoice**: the invoice's GST breakdown (`cgst_total /
  sgst_total / igst_total / tax_total / round_off / rounded_total`) is
  **frozen at invoice-create time** in `_compute_line` +
  `_apply_tax_split`. Allocations do not add lines and do not reopen
  taxes.
* **Rule of Supply of Services (GST §31(3)(d) — advance)**: GST law
  requires a "Receipt Voucher" when advance is received against a
  taxable supply. Phase 2A's Advance Receipt is deliberately positioned
  as a **generic patient advance** with a disclaimer that a Tax Invoice
  will be issued at delivery — this preserves the current legal
  posture. Phase 2B does not need to emit adjustment vouchers because
  the allocation ties the money to a Tax Invoice that carries the full
  taxable supply at issue-time.
* **Refund on the invoice** (either standalone or via allocation-void):
  existing `record_refund_atomic` produces a NEGATIVE amount row and
  decreases `paid_total` — GSTR-1 side is handled by the invoice's
  refund reconciliation, unchanged.

---

## 13. RBAC Matrix

| Action                                          | Roles Allowed                                        | Reference |
|-------------------------------------------------|------------------------------------------------------|-----------|
| Allocate (`POST /allocate`)                     | `front_desk`, `accounts`, `clinic_owner` (bypass: `super_admin`, `founder`) | mirrors `record_payment_atomic` (`_PAYMENT_ROLES`) |
| Void allocation                                 | `accounts`, `clinic_owner` (bypass as above)         | mirrors advance void + billing refund gates |
| List allocations                                | `front_desk`, `accounts`, `clinic_owner`, `audiologist` (read-only) | mirrors advance receipt READ |
| Read a single allocation                        | same as list                                          | — |
| Void an advance receipt with `allocated_total>0`| **denied** (409) — must void allocations first        | — |
| Cross-tenant                                    | **denied** by `clinic_id` scoping                     | Existing tenant guard on every DB query |

* Referral partner role (`referral_partner`) has NO access to allocation
  routes — they only view attribution reports.
* Patient portal has NO access — allocation is a clinic-side action.

---

## 14. Audit Trail

Every state transition writes one **append-only** row into
`advance_audit_events`. NEW kinds required for Phase 2B:

| Kind                  | Trigger                                    | Payload                                                                              |
|-----------------------|--------------------------------------------|---------------------------------------------------------------------------------------|
| `allocated`           | Successful `POST /allocate`                | `{allocation_id, allocation_no, invoice_id, invoice_no, amount, remaining_balance}`  |
| `allocation_voided`   | Successful void of allocation              | `{allocation_id, invoice_id, amount, refund_payment_id, reason}`                     |

Existing kinds (`created`, `voided`) remain unchanged.

* `serial_events` — **not written by allocation** (no state change).
* `referral_audit_events` — **not written by allocation directly**. The
  auto-recovery on downstream refund still uses NAV-011 pipes.
* `payments` row itself is an implicit audit anchor (append-only in
  practice — refunds do not delete, they insert negative rows).

---

## 15. Patient UX Recommendation

* **Patient Profile → Advances Tab** (already exists from Phase 2A).
  Extend each row to show a computed `Available` chip:
  * If `available_balance == received_amount` → chip green "Fully Available".
  * If `0 < available_balance < received_amount` → chip amber "Partially Used".
  * If `available_balance == 0` → chip grey "Fully Used".
* **Action buttons on an active row:**
  * `View Receipt` (existing).
  * `Allocate to Invoice` — opens `<AllocateAdvanceModal>` with:
     * A live-searched picker of the SAME patient's open invoices
       (`due_total > 0`, status ∈ {partial, draft-with-payments}).
     * An amount input pre-filled to `min(available_balance, invoice.due_total)`.
     * Read-only summary: source advance no + amount + available;
       target invoice no + due; resulting new due.
     * Idempotency-Key: generated client-side (uuid4 hex) on modal open.
     * On success → toast + refresh Advances tab + refresh invoice list.
  * `Void Receipt` (existing) — **disabled** when `allocated_total > 0`
    with tooltip "Void the allocations first".
* **Invoice Detail** page: below the `payments[]` table, add a small
  chip "Includes advance of ₹X from AR/YYYY/NNNNNN" when any payment
  row on that invoice has `method='advance'`. Click → jumps to the
  source advance receipt.
* **NEW view — Allocations sub-tab under Advances:** a chronological
  list of `advance_allocations` for the patient, with columns
  `AA No | Date | From (AR No) | To (Invoice No) | Amount | Status |
  Actor | Actions`. Void action per row.
* **No new founder-dashboard tile.** Multi-tenant boundary preserved,
  per the earlier Phase 2A revert decision.

---

## 16. Branch / Tenant Rules

* **Tenant (clinic)**: strict — an allocation cannot cross `clinic_id`.
  Enforced by every DB query already carrying `clinic_id: user["clinic_id"]`.
* **Branch**: recommendation is **allow cross-branch WITHIN the same
  clinic** by default (patients often walk into Branch A but pay at
  Branch B). The `advance_allocations.branch_id` is stamped from the
  actor's branch context, NOT the advance's origin branch, so multi-
  branch financial closeouts remain accurate.
* If a future policy demands per-branch containment, expose a boolean
  clinic setting `restrict_advance_allocation_to_branch` and add a
  post-CAS guard. Not in Phase 2B scope.
* Branch deactivation edge case (unrelated M-S6 finding, out of scope):
  a user of a deactivated branch cannot log in anyway, so no new
  allocation path exists from that branch.

---

## 17. Important Edge Cases

Each edge case + the expected outcome. Every P0 case here MUST be
covered by pytest in Phase 2B.

| # | Case                                                                                                        | Expected                                                                 |
|---|-------------------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------|
| E1  | Two concurrent allocations exceed available balance                                                       | Loser 409 "insufficient available balance"; ledger unchanged.            |
| E2  | Allocation from a **voided** advance                                                                     | 409 "advance receipt is voided".                                          |
| E3  | Allocation to a **cancelled** / **refunded** / **partially_refunded** invoice                            | 400 "cannot add payment to <status> invoice" (existing NAV-012 F-15).    |
| E4  | Allocation amount > invoice `due_total`                                                                  | 400 "payment amount exceeds due balance" (existing NAV-009 CAS).         |
| E5  | Allocation amount ≤ 0 or NaN                                                                             | 400 "amount must be > 0" (Pydantic + explicit finite check).             |
| E6  | Allocation payload with `Idempotency-Key` missing                                                        | 400 "Idempotency-Key required".                                          |
| E7  | Allocation replay with same key + same payload                                                           | Byte-identical response + header `Idempotency-Replay: true`.             |
| E8  | Allocation replay with same key + DIFFERENT payload                                                      | 422 (Stripe-compatible).                                                  |
| E9  | Cross-tenant: allocate advance of clinic A to invoice of clinic B                                        | 404 on advance OR 404 on invoice (whichever query hits the tenant guard first). |
| E10 | Cross-patient: allocate Patient A's advance to Patient B's invoice                                       | 400 "patient mismatch".                                                   |
| E11 | Partial allocation (amount < available)                                                                  | 200; `available_balance` decreases; advance stays `active`.               |
| E12 | Fully-consume allocation (amount == available)                                                           | 200; `available_balance = 0`; advance stays `active`; UI shows "Fully Used". |
| E13 | Multiple allocations across different invoices                                                           | Independent success; balance ledger deducts correctly.                    |
| E14 | Void allocation → refund emitted → then someone tries to void it again                                   | 409 "allocation is already voided".                                       |
| E15 | Void allocation on an invoice that has since been fully refunded via another path                       | 400 (invoice already refunded — cannot emit fresh refund row on top).     |
| E16 | Void advance receipt while it has an active allocation                                                   | 409 "cannot void — advance has active allocations; void allocations first". |
| E17 | Void advance receipt with only VOIDED allocations                                                        | 200; void succeeds (`allocated_total == 0`).                              |
| E18 | Allocation flips invoice `partial → paid`                                                                | Existing hooks fire: `mark_sale_paid_internal` + `auto_decrement_accessory_stock`. |
| E19 | Rounding: allocation of ₹1000.005 (float noise)                                                          | Pydantic rounds to 2 decimals; CAS uses `MONEY_TOL = 0.01` tolerance.     |
| E20 | Crash between advance CAS and `record_payment_atomic`                                                    | On restart + idempotent retry: `_rebuild_response` sees `advance_allocations` row exists → replay success. If the allocation row is missing, `available_balance` is restored by the compensating $inc that runs in the same request. |
| E21 | Advance receipt from a MULTI-BRANCH clinic (branch A) allocated at branch B                              | Allowed; `advance_allocations.branch_id = branch B`. Audit records both.  |
| E22 | Referral commission KPI at the moment of allocation                                                      | Existing `_attribute_revenue` picks up the fresh `paid_total`; no drift.  |
| E23 | Concurrent invoice refund vs concurrent allocation on the same invoice                                   | Both CAS-guarded independently; one may fail with 400/409, the other succeeds. |
| E24 | Historical Phase-2A advance without `available_balance`/`allocated_total` fields                         | One-time backfill during Phase 2B deployment; router treats missing = full balance (safety fallback). |

---

## 18. P0 / P1 / P2 / P3 Risks + Recommended Implementation Phases

### 18.1 Risk classification

| Rank | Risk                                                                                                     | Mitigation lives in section |
|------|----------------------------------------------------------------------------------------------------------|-----------------------------|
| **P0** | Dual-write drift: money on the invoice but no ledger row on the advance, or vice versa                | §2, §8.1, §17 E20           |
| **P0** | Over-allocation of an advance receipt (concurrent allocations sum > available)                        | §8.1, §17 E1                |
| **P0** | Allocation to a refunded / partially_refunded / cancelled invoice                                     | §11, §17 E3                 |
| **P0** | Overpayment of an invoice via allocation                                                              | §8.2, §17 E4                |
| **P0** | Idempotency-Key missing / replay divergence                                                           | §9, §17 E6–E8               |
| **P1** | Void of advance receipt while active allocations exist                                                | §5, §6 Path A, §17 E16      |
| **P1** | Cross-tenant allocation                                                                               | §13, §16, §17 E9            |
| **P1** | Cross-patient allocation                                                                              | §13, §17 E10                |
| **P1** | Allocation from a voided advance                                                                      | §5, §17 E2                  |
| **P1** | Void of allocation on an already-refunded invoice                                                     | §6, §17 E15                 |
| **P1** | Referral commission drift after allocation                                                            | §3.3, §11, §17 E22          |
| **P2** | Cross-branch policy (currently permissive)                                                            | §16                         |
| **P2** | Backfill of `available_balance` / `allocated_total` on historical advance receipts                    | §4.1, §17 E24               |
| **P2** | Extending `_rebuild_response` for the new scope                                                       | §9                          |
| **P2** | Adding `"advance"` to `PAYMENT_METHODS` catalogue                                                     | §11                         |
| **P3** | Reporting KPI additions (Advances Ledger tab, daily allocation summary)                               | §15                         |
| **P3** | Refund-to-advance re-credit flow                                                                      | Deferred to Phase 2C        |
| **P3** | Family-group cross-patient allocation                                                                 | Deferred; policy call needed |
| **P3** | Founder-dashboard aggregation (explicitly out of scope by prior decision)                             | §15                         |

### 18.2 Recommended implementation phases (chronological, atomic)

**Phase 2B.1 — Model + Idempotency + Backfill (backend, ~1 day)**
* Extend `models/_advance.py` with `available_balance`, `allocated_total`
  fields and a new `AdvanceAllocation` / `AdvanceAllocationCreate` /
  `AdvanceAllocationVoidIn` model.
* Add `"advance_allocation"` to `SUPPORTED_SCOPES` in
  `utils/idempotency.py`.
* Add `"advance"` to `PAYMENT_METHODS`.
* Add controlled one-shot backfill of `available_balance` /
  `allocated_total` on server startup guarded by a completion sentinel
  (`db.backfills`).

**Phase 2B.2 — Allocation writer (backend, ~1 day)**
* Implement `POST /api/advance-receipts/{id}/allocations` with mandatory
  Idempotency-Key.
* Sequence: advance-CAS decrement → `record_payment_atomic` (with
  `method="advance"`, `advance_receipt_id`, `allocation_id`,
  `idempotency_correlation_id`) → insert `advance_allocations` row →
  audit `allocated` → complete idempotency.
* On any step-2/3 failure: compensating $inc on the advance; return
  the original 4xx.

**Phase 2B.3 — Void allocation (backend, ~1 day)**
* Implement `POST /api/advance-receipts/{id}/allocations/{alloc_id}/void`.
* Sequence: `advance_allocations` CAS `active → voided` →
  `record_refund_atomic` on the invoice → advance `$inc` re-credit →
  audit `allocation_voided`.
* Extend advance-receipt void endpoint's CAS to include
  `allocated_total: 0`.

**Phase 2B.4 — Read paths (backend, ~½ day)**
* `GET /api/advance-receipts/{id}` now returns `available_balance`,
  `allocated_total`, `allocations[]` (limited to 25 most recent).
* `GET /api/advance-receipts/{id}/allocations` (list, paginated).
* Extend `_rebuild_response` in `utils/idempotency.py` for the new
  scope.

**Phase 2B.5 — Frontend (~1 day)**
* `AllocateAdvanceModal` (invoice picker, amount, idem-key,
  read-only preview, confirm).
* Advances tab row extension (available chip, Allocate button,
  disabled Void when allocated_total>0).
* Invoice detail chip for allocation-sourced payments.

**Phase 2B.6 — Tests (~1 day)**
* pytest suite `backend/tests/test_advance_allocation_phase2b.py`
  covering §17 E1–E24 exhaustively.
* Include a stress test spawning N concurrent allocation coroutines
  against the same advance (asyncio.gather) to prove §8.1.

**Phase 2B.7 — Pre-deploy audit + strict `git diff` review + prod deploy.**

**Phase 2C (later)** — Refund of unallocated advance directly to
customer + Advance Ledger PDF/CSV export.

**Phase 2D (later)** — Historical duplicate reconciliation (out of
scope of Advance Allocation itself, tracked separately).

---

## Blocking financial-integrity conflicts

**None encountered.** Every path can be built on:
* NAV-009's proven insert-first + CAS-then-compensate writer,
* NAV-011's already-correct `paid_total`-based referral attribution,
* NAV-012's mandatory-Idempotency-Key policy for financial POSTs.

## Unrelated issues documented briefly

* `PAYMENT_METHODS` catalogue location was not re-verified in Phase 1;
  Phase 2B.1 must confirm it lives in `models/_canonical.py` and add
  `"advance"` there.
* Multi-branch M-S6 (deactivation → user logout) remains open in
  a separate audit; it does not block Phase 2B, but a deactivated-branch
  user could still call the allocation endpoint if their JWT is still
  live — same shape as every other tenant route today.

---

**END OF AUDIT — NO IMPLEMENTATION UNTIL EXPLICIT USER AUTHORIZATION.**
