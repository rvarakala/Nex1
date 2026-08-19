"""HA Quick Sale — single-form sale + fitting + invoice creator.

The classical sales flow lives in `ha_sales.py` and requires a Quotation
to exist first (with serial-item assignment, margin floor checks, etc.).
That flow is correct for clinics that want full margin governance, but it
is overkill for the common walk-in case where the front-desk just records
"Mrs Sharma bought a Phonak Bolero V70 for ₹85,000 today."

This router exposes ONE endpoint:

    POST /api/ha/quick-sale

…that takes a flat payload (HA make/model/serial, MRP, sale price,
discount, payment mode, advance/full, extended-warranty flag, notes) and
atomically writes:

  * `ha_quick_sales` doc — the source of truth for this simple flow
  * `ha_fittings`     doc — so the sale shows up on /ha/fittings
  * `invoices`         doc — so it shows up under Billing → Invoices and
    contributes to the Accounts/Revenue Dashboard

It does NOT touch `serial_items` (those are governed by the rich sale
flow's state machine). The serial number captured here is treated as a
free-text identifier the audiologist typed in.

If `payment_status="fully_paid"` we also stamp the invoice as paid so
revenue dashboards reflect it immediately.
"""
import logging
import re
import uuid
from datetime import datetime, timezone, date
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import get_current_user, require_roles, user_can_see_branch
from billing import _next_invoice_no
from database import get_db
from utils.ha_states import transition_serial
from utils.numbering import next_number

router = APIRouter(prefix="/api/ha", tags=["ha-quick-sale"])
log = logging.getLogger("audinexa.ha_quick_sale")


# ─── Models ─────────────────────────────────────────────────────────

class QuickSaleIn(BaseModel):
    """Single-form HA sale input. Everything the audiologist needs in one shot."""

    # Patient + branch
    patient_id: str
    branch_id: Optional[str] = None        # default: user's primary branch

    # Hearing aid details
    brand: str = Field(..., min_length=1, max_length=80)
    model: str = Field(..., min_length=1, max_length=120)
    ha_type: Literal["BTE", "RIC", "ITE", "ITC", "CIC", "IIC", "POCKET", "OTHER"] = "BTE"
    # Side-specific serial numbers — one OR both must be provided based on `side`.
    # `serial_number` (legacy, single field) still accepted for backward compat
    # and mapped to whichever side the caller picked.
    serial_number: Optional[str] = Field(None, min_length=1, max_length=80)
    serial_left: Optional[str] = Field(None, min_length=1, max_length=80)
    serial_right: Optional[str] = Field(None, min_length=1, max_length=80)
    side: Literal["left", "right", "both"] = "both"
    fitting_date: str                       # ISO YYYY-MM-DD

    # Warranty
    warranty_months: int = Field(12, ge=0, le=240)
    extended_warranty: bool = False
    extended_warranty_months: Optional[int] = Field(None, ge=0, le=240)
    extended_warranty_source: Optional[Literal["clinic", "manufacturer"]] = None

    # Pricing
    mrp: float = Field(..., ge=0)
    sale_price: float = Field(..., ge=0)    # post-discount, what the patient actually pays
    discount_amount: Optional[float] = Field(None, ge=0)
    gst_rate: float = Field(18.0, ge=0, le=28)

    # Payment
    payment_status: Literal["fully_paid", "advance_paid", "unpaid"] = "fully_paid"
    payment_mode: Optional[Literal["cash", "upi", "card", "bank_transfer", "cheque"]] = None
    payment_date: Optional[str] = None       # ISO YYYY-MM-DD when first payment landed
    advance_amount: Optional[float] = Field(None, ge=0)
    expected_payment_date: Optional[str] = None

    # Misc
    notes: Optional[str] = None

    # Device spec — colour + power + wire/tube length. For side='both'
    # the shape is {left: {…}, right: {…}} so audiologists can capture
    # asymmetric fits (different wires/powers per ear). For single-ear
    # sides it's a flat dict. Backend forwards this untouched to the
    # ha_quick_sales, ha_fittings and invoice line docs so print
    # templates + inventory ops can re-render "2M R" style shorthand.
    spec: Optional[dict] = None


class QuickSaleOut(BaseModel):
    quick_sale_id: str
    sale_no: str
    fitting_id: str
    invoice_id: str
    invoice_no: str
    total: float
    paid: float
    balance: float
    status: str
    fitting_url: str                        # frontend deep link the UI can navigate to
    inventory_consumed: List[str] = Field(default_factory=list)  # serial_ids transitioned IN_STOCK→SOLD
    inventory_unmatched: List[str] = Field(default_factory=list) # serial_no values w/o inventory record


# ─── Helpers ────────────────────────────────────────────────────────

def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _normalise_serial(s: Optional[str]) -> Optional[str]:
    """Trim + uppercase. Returns None for empty / None."""
    if s is None:
        return None
    s = s.strip().upper()
    return s or None


def _resolve_serials(payload: QuickSaleIn) -> dict:
    """Map the side-aware payload into a {side → serial_no} dict (canonicalised).

    Accepts the legacy single-field `serial_number` and re-routes it to the
    correct side based on `payload.side`. Raises 400 on missing/duplicate.
    """
    side = payload.side
    left = _normalise_serial(payload.serial_left)
    right = _normalise_serial(payload.serial_right)
    legacy = _normalise_serial(payload.serial_number)

    if side == "both":
        # Both must be filled (split fields preferred)
        left = left or (legacy if not right else None)
        right = right or (legacy if not left else None)
        if not left or not right or left == right:
            raise HTTPException(
                400,
                "Both ears requires two distinct serial numbers — one for the left ear and one for the right.",
            )
        return {"left": left, "right": right}

    if side == "left":
        s = left or legacy
        if not s:
            raise HTTPException(400, "Left serial number is required.")
        return {"left": s}

    # right
    s = right or legacy
    if not s:
        raise HTTPException(400, "Right serial number is required.")
    return {"right": s}


async def _validate_and_consume_serials(
    db, clinic_id: str, branch_id: str, serials_by_side: dict,
    sale_no: str, patient: dict, actor_user_id: str,
) -> tuple[list[str], list[str]]:
    """For each serial number requested:

      • Look up `serial_items` in this clinic.
      • If found and IN_STOCK → transition to SOLD.
      • If found and SOLD/RESERVED/TRIAL_OUT/etc → HARD REJECT (409) with details.
      • If not found → return it in `unmatched` so caller can flag the fitting
        without blocking the sale (Q1c hybrid mode).

    Returns ``(consumed_serial_ids, unmatched_serial_nos)``.
    """
    consumed_ids: list[str] = []
    unmatched: list[str] = []
    seen_in_request: set[str] = set()

    for side, serial_no in serials_by_side.items():
        if serial_no in seen_in_request:
            raise HTTPException(400, f"Same serial '{serial_no}' supplied for multiple sides.")
        seen_in_request.add(serial_no)

        # Case-insensitive lookup; we already uppercased via _normalise_serial.
        si = await db.serial_items.find_one(
            {"clinic_id": clinic_id, "serial_no": serial_no}, {"_id": 0},
        )
        if not si:
            unmatched.append(serial_no)
            log.info(f"quick-sale serial '{serial_no}' not in inventory — flagged unmatched")
            continue

        # Branch guard: a serial belongs to one branch's stock. Selling it from
        # a different branch is a stock-transfer event, not a sale — block here.
        if si.get("branch_id") and si["branch_id"] != branch_id:
            raise HTTPException(
                409,
                f"Serial {serial_no} is in branch '{si['branch_id']}' stock — "
                f"transfer it to '{branch_id}' first.",
            )

        state = si.get("state", "IN_STOCK")
        if state == "SOLD":
            patient_ref = si.get("current_patient_id") or "another patient"
            raise HTTPException(
                409,
                f"Serial {serial_no} is already SOLD (to {patient_ref}). "
                f"Use a different serial or void the previous sale.",
            )
        if state not in {"IN_STOCK", "RESERVED"}:
            raise HTTPException(
                409,
                f"Serial {serial_no} is currently {state} — only IN_STOCK or RESERVED units can be sold.",
            )

        # Transition to SOLD with full state-machine audit
        try:
            await transition_serial(
                db, si["serial_id"], "SOLD",
                actor_user_id=actor_user_id,
                ref_doc={"kind": "quick_sale", "id": sale_no},
                note=f"Quick-sale {sale_no} → patient {patient.get('name','')} ({patient.get('patient_id')})",
            )
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"Failed to consume serial {serial_no}: {exc}")

        # Stamp current_patient_id so warranty/AMC tracking can find this unit.
        await db.serial_items.update_one(
            {"serial_id": si["serial_id"]},
            {"$set": {"current_patient_id": patient["patient_id"], "updated_at": datetime.now(timezone.utc).isoformat()}},
        )
        consumed_ids.append(si["serial_id"])
        log.info(f"quick-sale consumed serial {serial_no} (id={si['serial_id']}, side={side})")

    return consumed_ids, unmatched


# ─── Live serial lookup (for in-form validation) ─────────────────────

@router.get("/serials/lookup")
async def lookup_serial(
    serial_no: str,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Live validation for the Quick Sale modal. Returns one of:

      • ``available`` — serial is IN_STOCK (or RESERVED) and ready to sell
      • ``conflict``  — serial is SOLD / TRIAL_OUT / SERVICE_IN — *block* sale
      • ``not_found`` — typed serial isn't in inventory; sale will be flagged

    The frontend uses this to draw a green ✓ / red ✗ / amber ⚠ badge next to
    the input field while the user types (debounced).
    """
    sn = _normalise_serial(serial_no)
    if not sn:
        return {"status": "not_found", "serial_no": ""}

    si = await db.serial_items.find_one(
        {"clinic_id": user["clinic_id"], "serial_no": sn}, {"_id": 0},
    )
    if not si:
        return {"status": "not_found", "serial_no": sn}

    # Branch isolation — a serial in another branch's stock isn't usable here
    user_branches = set(user.get("branch_ids") or [])
    if user_branches and si.get("branch_id") and si["branch_id"] not in user_branches:
        return {
            "status": "conflict",
            "serial_no": sn,
            "reason": f"Stock at branch '{si['branch_id']}' — transfer first",
        }

    state = si.get("state", "IN_STOCK")
    if state in {"IN_STOCK", "RESERVED"}:
        return {
            "status": "available",
            "serial_no": sn,
            "serial_id": si["serial_id"],
            "state": state,
            "product_id": si.get("product_id"),
            "branch_id": si.get("branch_id"),
        }

    # Anything else (SOLD, TRIAL_OUT, SERVICE_IN, DAMAGED, RETIRED, RETURNED) → conflict
    return {
        "status": "conflict",
        "serial_no": sn,
        "state": state,
        "reason": (
            f"Already SOLD to {si.get('current_patient_id','another patient')}"
            if state == "SOLD" else f"Currently {state}"
        ),
    }


def _ensure_branch(user: dict, branch_id: Optional[str]) -> str:
    bid = branch_id or (user.get("branch_ids") or [None])[0]
    if not bid:
        raise HTTPException(400, "branch_id required (no default branch on this user)")
    if not user_can_see_branch(user, bid):
        raise HTTPException(403, "Branch access denied")
    return bid


def _calc_totals(payload: QuickSaleIn) -> dict:
    """Returns totals consistent with how Invoice rows compute (GST inclusive of sale_price)."""
    sale_price = round(float(payload.sale_price), 2)
    mrp = round(float(payload.mrp), 2)
    discount_amt = round(
        float(payload.discount_amount) if payload.discount_amount is not None else max(0.0, mrp - sale_price),
        2,
    )
    # We treat sale_price as GROSS (GST-inclusive) — most clinics quote final amount.
    # invoice_total == sale_price; tax computed back from inclusive.
    gst_rate = float(payload.gst_rate or 0)
    if gst_rate > 0:
        taxable = round(sale_price / (1 + gst_rate / 100.0), 2)
        gst_amount = round(sale_price - taxable, 2)
    else:
        taxable = sale_price
        gst_amount = 0.0
    return {
        "mrp": mrp,
        "sale_price": sale_price,
        "discount_amount": discount_amt,
        "taxable": taxable,
        "gst_rate": gst_rate,
        "gst_amount": gst_amount,
        "total": sale_price,
    }


# ─── Endpoint ───────────────────────────────────────────────────────

@router.post("/quick-sale", response_model=QuickSaleOut)
async def create_quick_sale(
    payload: QuickSaleIn,
    user=Depends(require_roles(
        "front_desk", "audiologist", "inventory_manager", "clinic_owner", "super_admin",
    )),
    db=Depends(get_db),
):
    """One-shot HA sale: writes quick-sale + fitting + invoice docs atomically."""

    # ── Validate patient ──
    branch_id = _ensure_branch(user, payload.branch_id)
    patient = await db.patients.find_one(
        {"patient_id": payload.patient_id, "clinic_id": user["clinic_id"]},
        {"_id": 0, "patient_id": 1, "name": 1, "phone": 1, "mrd_no": 1},
    )
    if not patient:
        raise HTTPException(404, "Patient not found in this clinic")

    # ── Validate sale_price ≤ mrp (sanity) ──
    if payload.sale_price > payload.mrp + 0.5:
        raise HTTPException(400, "Sale price cannot exceed MRP")

    # ── Resolve & validate serial numbers per ear ──
    # Map { side → uppercased serial_no }, e.g. {'left':'PHO-RIC-A1', 'right':'PHO-RIC-A2'}
    serials_by_side = _resolve_serials(payload)

    totals = _calc_totals(payload)
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    # ── Compute payment state ──
    paid = 0.0
    if payload.payment_status == "fully_paid":
        paid = totals["total"]
    elif payload.payment_status == "advance_paid":
        if not payload.advance_amount or payload.advance_amount <= 0:
            raise HTTPException(400, "advance_amount required when payment_status=advance_paid")
        if payload.advance_amount > totals["total"] + 0.5:
            raise HTTPException(400, "advance_amount cannot exceed total")
        paid = round(float(payload.advance_amount), 2)
    balance = round(totals["total"] - paid, 2)

    # ── Allocate IDs / numbers ──
    sale_no = await next_number(db, "sale", user["clinic_id"])               # SAL-YYYY-NNNN (reuses existing seq)
    invoice_no = await _next_invoice_no(db, user["clinic_id"])               # INV/YYYY/NNNNNN
    quick_sale_id = f"QSL-{uuid.uuid4().hex[:10].upper()}"
    fitting_id = f"FIT-{uuid.uuid4().hex[:10].upper()}"
    invoice_id = f"INV-{uuid.uuid4().hex[:10].upper()}"

    # ── Consume inventory (HARD REJECT if any serial is already SOLD/etc;
    # OK if serial isn't tracked — flagged in unmatched). This runs BEFORE we
    # write any of our docs so a serial conflict doesn't leave orphan rows.
    consumed_serial_ids, unmatched_serials = await _validate_and_consume_serials(
        db, user["clinic_id"], branch_id, serials_by_side,
        sale_no=sale_no, patient=patient, actor_user_id=user["user_id"],
    )

    # ── Build & insert documents (ordered: quick_sale → fitting → invoice) ──
    quick_sale_doc = {
        "quick_sale_id": quick_sale_id,
        "sale_no": sale_no,
        "clinic_id": user["clinic_id"],
        "branch_id": branch_id,
        "patient_id": patient["patient_id"],
        "patient_name": patient.get("name", ""),
        "patient_phone": patient.get("phone", ""),
        "mrd_no": patient.get("mrd_no", ""),
        # HA
        "brand": payload.brand.strip(),
        "model": payload.model.strip(),
        "ha_type": payload.ha_type,
        # Side-aware serial fields. We keep `serial_number` as the
        # display-friendly aggregate ("LEFT/RIGHT") for legacy clients.
        "side": payload.side,
        "serial_left": serials_by_side.get("left"),
        "serial_right": serials_by_side.get("right"),
        "serial_number": " / ".join([s for s in (serials_by_side.get("left"), serials_by_side.get("right")) if s]),
        "consumed_serial_ids": consumed_serial_ids,
        "unmatched_serials": unmatched_serials,
        "inventory_tracked": len(unmatched_serials) == 0,
        "fitting_date": payload.fitting_date,
        # Warranty
        "warranty_months": payload.warranty_months,
        "extended_warranty": payload.extended_warranty,
        "extended_warranty_months": payload.extended_warranty_months,
        "extended_warranty_source": payload.extended_warranty_source,
        # Pricing
        "mrp": totals["mrp"],
        "sale_price": totals["sale_price"],
        "discount_amount": totals["discount_amount"],
        "gst_rate": totals["gst_rate"],
        "gst_amount": totals["gst_amount"],
        "taxable_amount": totals["taxable"],
        "total": totals["total"],
        # Payment
        "payment_status": payload.payment_status,
        "payment_mode": payload.payment_mode,
        "payment_date": payload.payment_date or (_today_iso() if payload.payment_status == "fully_paid" else None),
        "advance_amount": paid if payload.payment_status == "advance_paid" else (totals["total"] if payload.payment_status == "fully_paid" else 0.0),
        "amount_paid": paid,
        "balance_due": balance,
        "expected_payment_date": payload.expected_payment_date,
        # Linked
        "fitting_id": fitting_id,
        "invoice_id": invoice_id,
        "invoice_no": invoice_no,
        # Misc
        "notes": payload.notes or "",
        "status": "completed" if payload.payment_status == "fully_paid" else "open",
        # Device spec — persisted as-is so filters (colour, receiver
        # power) and print templates can render "Beige · 2M Receiver".
        "spec": payload.spec or {},
        # Audit
        "created_at": now_iso,
        "created_by": user["user_id"],
        "audiologist_name": user.get("name", ""),
    }
    await db.ha_quick_sales.insert_one(quick_sale_doc)

    # ── Lightweight Fitting record so it appears in the Fitting Ledger ──
    # NOTE: schema must match Pydantic `Fitting` model in models_ha.py so the
    # existing GET /api/ha/fittings/{id} endpoint validates and returns it.
    # Build one FittingSerial per ear so audiogram + warranty tracking can map
    # each device to the correct ear.
    fitting_serials_payload = []
    if payload.side == "both":
        fitting_serials_payload.append({"serial_id": serials_by_side["left"], "side": "left"})
        fitting_serials_payload.append({"serial_id": serials_by_side["right"], "side": "right"})
    else:
        fitting_serials_payload.append({
            "serial_id": serials_by_side[payload.side],
            "side": payload.side,
        })
    visit_at = now_iso
    notes_serials = ", ".join([f"{s['side'].upper()}={s['serial_id']}" for s in fitting_serials_payload])
    fitting_doc = {
        "fitting_id": fitting_id,
        "clinic_id": user["clinic_id"],
        "branch_id": branch_id,
        "patient_id": patient["patient_id"],
        "patient_name": patient.get("name", ""),
        "audiologist_user_id": user["user_id"],
        "audiologist_name": user.get("name", ""),
        "sale_no": sale_no,
        "serials": fitting_serials_payload,
        "status": "active",
        "first_fit_at": payload.fitting_date,
        "completed_at": None,
        "visits": [{
            "visit_id": f"FV-{uuid.uuid4().hex[:8].upper()}",
            "kind": "first_fit",
            "at": visit_at,
            "actor_user_id": user["user_id"],
            "actor_name": user.get("name", ""),
            "notes": (
                f"HA sale recorded via Quick Sale form. "
                f"Brand: {payload.brand}, Model: {payload.model}, Type: {payload.ha_type}, "
                f"Side: {payload.side}. Serials: {notes_serials}. "
                f"Inventory consumed: {len(consumed_serial_ids)} unit(s)"
                + (f" (unmatched: {', '.join(unmatched_serials)})" if unmatched_serials else "")
                + f". Warranty: {payload.warranty_months} months"
                + (f" + {payload.extended_warranty_months} months extended ({payload.extended_warranty_source})"
                   if payload.extended_warranty else "")
                + "."
            ),
            "adjustments": [],
        }],
        "aided_audiogram": None,
        "rem": None,
        "notes": payload.notes or "",
        "created_by_user_id": user["user_id"],
        "created_at": now,
        "updated_at": now_iso,
        # Extra denormalised fields (Pydantic ignores extras due to extra="ignore"):
        "quick_sale_id": quick_sale_id,
        "source": "quick_sale",
        "ha_brand": payload.brand,
        "ha_model": payload.model,
        "ha_type": payload.ha_type,
        "warranty_months": payload.warranty_months,
        "extended_warranty": payload.extended_warranty,
        "serial_left": serials_by_side.get("left"),
        "serial_right": serials_by_side.get("right"),
        "consumed_serial_ids": consumed_serial_ids,
        "unmatched_serials": unmatched_serials,
        "inventory_tracked": len(unmatched_serials) == 0,
        # Money snapshot for the Fittings table (kept in sync via mark-paid):
        "sale_total": totals["total"],
        "amount_paid": paid,
        "balance_due": balance,
        "payment_status": payload.payment_status,
        "invoice_id": invoice_id,
        "invoice_no": invoice_no,
        # Device spec — same blob mirrored on the fitting doc so the
        # Fitting Ledger + patient timeline can display "Beige · 2M R"
        # without a second lookup on ha_quick_sales.
        "spec": payload.spec or {},
    }
    await db.ha_fittings.insert_one(fitting_doc)

    # ── Invoice doc (slot into existing Billing module) ──
    # NOTE: shape must match Pydantic `Invoice` in models/_canonical.py.
    #
    # Money math (Indian GST convention):
    #   subtotal      = qty × MRP        (pre-discount, pre-tax)
    #   discount      = payload discount (or MRP − sale_price if not given)
    #   taxable_value = subtotal − discount   (== _calc_totals["taxable"] when gst=0)
    #   tax           = GST on taxable_value  (extracted from GST-inclusive sale_price)
    #   grand_total   = taxable_value + tax   (== sale_price when gst-inclusive)
    #
    # Prior bug: `subtotal` was set to `inv_taxable` (post-discount), then the
    # Discount line was rendered separately in the invoice popup — the math
    # visually didn't add up (Subtotal 1.65L − Discount 10k = Grand Total 1.65L?).
    # Fixed by writing MRP × qty as the subtotal and unit_price.
    inv_qty = 1
    inv_unit_price = totals["mrp"]                    # qty=1, unit_price == pre-discount MRP
    inv_subtotal = round(totals["mrp"] * inv_qty, 2)  # gross line value before discount
    inv_taxable = totals["taxable"]                   # post-discount, pre-tax
    inv_total_tax = totals["gst_amount"]
    # Simple intra-state split: 50/50 CGST+SGST. Quick-sale skips inter-state IGST detection.
    inv_cgst = round(inv_total_tax / 2.0, 2)
    inv_sgst = round(inv_total_tax - inv_cgst, 2)
    inv_line_total = round(inv_taxable + inv_total_tax, 2)
    invoice_doc = {
        "invoice_id": invoice_id,
        "invoice_no": invoice_no,
        "clinic_id": user["clinic_id"],

        "patient_id": patient["patient_id"],
        "patient_name": patient.get("name", ""),
        "patient_mobile": patient.get("phone", ""),
        "mrd": patient.get("mrd_no", ""),

        "invoice_date": now,

        "lines": [{
            "line_id": uuid.uuid4().hex[:8],
            "description": (
                f"Hearing Aid — {payload.brand} {payload.model} ({payload.ha_type}, {payload.side}) "
                f"· S/N {' / '.join([s for s in (serials_by_side.get('left'), serials_by_side.get('right')) if s])}"
            ),
            "quantity": inv_qty,
            "unit_price": inv_unit_price,
            "discount_amount": totals["discount_amount"],
            "discount_type": "flat",
            "discount_value": totals["discount_amount"],
            "is_taxable": totals["gst_rate"] > 0,
            "gst_rate": totals["gst_rate"],
            "taxable_value": inv_taxable,
            "cgst_amount": inv_cgst,
            "sgst_amount": inv_sgst,
            "igst_amount": 0.0,
            "line_total": inv_line_total,
            "product_type": "Hearing Aid",
            "make": payload.brand,
            "model": payload.model,
            "serial_numbers": [s for s in (serials_by_side.get("left"), serials_by_side.get("right")) if s],
        }],

        "subtotal": inv_subtotal,
        "discount_total": totals["discount_amount"],
        "cgst_total": inv_cgst,
        "sgst_total": inv_sgst,
        "igst_total": 0.0,
        "tax_total": inv_total_tax,
        "grand_total": inv_line_total,
        "rounded_total": inv_line_total,
        "round_off": 0.0,

        "paid_total": paid,
        "due_total": balance,

        "status": "paid" if payload.payment_status == "fully_paid" else (
            "partial" if payload.payment_status == "advance_paid" else "draft"
        ),

        "payments": [],
        "notes": (
            f"Auto-created from HA Quick Sale {sale_no}. Payment mode: {payload.payment_mode or '—'}."
        ),
        "created_at": now,
        "created_by_user_id": user["user_id"],

        # Extra denormalised fields (Pydantic ignores extras):
        "source": "ha_quick_sale",
        "ha_quick_sale_id": quick_sale_id,
        "ha_sale_no": sale_no,
        "fitting_id": fitting_id,
    }
    if paid > 0:
        invoice_doc["payments"] = [{
            "payment_id": f"PAY-{uuid.uuid4().hex[:8].upper()}",
            "clinic_id": user["clinic_id"],
            "invoice_id": invoice_id,
            "method": payload.payment_mode or "cash",
            "amount": paid,
            "reference": None,
            "paid_at": now,
            "received_by_user_id": user["user_id"],
            "notes": "Initial payment captured via HA Quick Sale.",
        }]
    # NAV-008 · Route through the retry-safe insert helper so a
    # concurrent counter collision (extremely rare, only possible if
    # a raw insert bypassed the counter) transparently renews the
    # invoice_no. On persistent conflict the helper raises a
    # controlled 500 instead of leaking a Mongo E11000.
    from billing import _insert_invoice_with_retry
    await _insert_invoice_with_retry(db, invoice_doc, user["clinic_id"])
    invoice_no = invoice_doc["invoice_no"]

    log.info(
        f"quick-sale created clinic={user['clinic_id']} sale_no={sale_no} "
        f"invoice_no={invoice_no} fitting={fitting_id} paid={paid} balance={balance}"
    )

    return QuickSaleOut(
        quick_sale_id=quick_sale_id,
        sale_no=sale_no,
        fitting_id=fitting_id,
        invoice_id=invoice_id,
        invoice_no=invoice_no,
        total=totals["total"],
        paid=paid,
        balance=balance,
        status=quick_sale_doc["status"],
        fitting_url="/ha/fittings",
        inventory_consumed=consumed_serial_ids,
        inventory_unmatched=unmatched_serials,
    )


@router.get("/quick-sales")
async def list_quick_sales(
    limit: int = 50,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    q = {"clinic_id": user["clinic_id"]}
    if user.get("branch_ids"):
        q["branch_id"] = {"$in": user["branch_ids"]}
    rows: List[dict] = []
    cursor = db.ha_quick_sales.find(q, {"_id": 0}).sort("created_at", -1).limit(int(limit))
    async for r in cursor:
        rows.append(r)
    return rows


# ─── Mark balance paid (settle advance-paid sale) ─────────────────────

class MarkBalancePaidIn(BaseModel):
    """Settle the remaining balance on an advance-paid quick-sale.

    Defaults: amount = current balance_due, mode = original payment_mode,
    payment_date = today. Caller can override any of them.
    """
    amount: Optional[float] = Field(None, ge=0, description="Defaults to current balance_due")
    payment_mode: Optional[Literal["cash", "upi", "card", "bank_transfer", "cheque"]] = None
    payment_date: Optional[str] = None
    reference: Optional[str] = None
    notes: Optional[str] = None


class MarkBalancePaidOut(BaseModel):
    quick_sale_id: str
    invoice_id: str
    invoice_no: str
    total: float
    amount_paid: float
    balance_due: float
    payment_status: str
    invoice_status: str


@router.post("/quick-sales/{quick_sale_id}/mark-paid", response_model=MarkBalancePaidOut)
async def mark_balance_paid(
    quick_sale_id: str,
    payload: MarkBalancePaidIn,
    user=Depends(require_roles(
        "front_desk", "accounts", "clinic_owner", "super_admin",
    )),
    db=Depends(get_db),
):
    """Capture a follow-up payment against an advance-paid Quick Sale and roll
    the invoice + fitting denorms forward atomically."""
    qs = await db.ha_quick_sales.find_one(
        {"quick_sale_id": quick_sale_id, "clinic_id": user["clinic_id"]},
        {"_id": 0},
    )
    if not qs:
        raise HTTPException(404, "Quick Sale not found")
    if not user_can_see_branch(user, qs["branch_id"]):
        raise HTTPException(403, "Branch access denied")
    if qs.get("payment_status") == "fully_paid":
        raise HTTPException(409, "This sale is already fully paid.")

    current_balance = round(float(qs.get("balance_due") or 0), 2)
    if current_balance <= 0:
        raise HTTPException(409, "No outstanding balance on this sale.")

    pay_amount = round(float(payload.amount) if payload.amount is not None else current_balance, 2)
    if pay_amount <= 0:
        raise HTTPException(400, "amount must be > 0")
    if pay_amount > current_balance + 0.5:
        raise HTTPException(400, f"Payment cannot exceed balance ({current_balance:.2f})")

    pay_mode = payload.payment_mode or qs.get("payment_mode") or "cash"
    pay_date_iso = payload.payment_date or _today_iso()
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    new_paid = round(float(qs.get("amount_paid") or 0) + pay_amount, 2)
    new_balance = round(float(qs.get("total") or 0) - new_paid, 2)
    fully_settled = new_balance <= 0.005
    new_payment_status = "fully_paid" if fully_settled else "advance_paid"

    # 1) Update ha_quick_sales
    await db.ha_quick_sales.update_one(
        {"quick_sale_id": quick_sale_id},
        {"$set": {
            "amount_paid": new_paid,
            "balance_due": max(0.0, new_balance),
            "payment_status": new_payment_status,
            "payment_mode": pay_mode,
            "last_payment_at": now_iso,
            "last_payment_date": pay_date_iso,
            "status": "completed" if fully_settled else "open",
        }},
    )

    # 2) Update invoice — append a Payment, recompute paid_total/due_total/status
    invoice = await db.invoices.find_one({"invoice_id": qs["invoice_id"]}, {"_id": 0})
    if invoice:
        new_payment = {
            "payment_id": f"PAY-{uuid.uuid4().hex[:8].upper()}",
            "clinic_id": user["clinic_id"],
            "invoice_id": invoice["invoice_id"],
            "method": pay_mode,
            "amount": pay_amount,
            "reference": payload.reference,
            "paid_at": now,
            "received_by_user_id": user["user_id"],
            "notes": payload.notes or "Balance settlement via Mark balance paid.",
        }
        new_inv_paid = round(float(invoice.get("paid_total") or 0) + pay_amount, 2)
        grand_total = float(invoice.get("grand_total") or invoice.get("rounded_total") or qs.get("total") or 0)
        new_inv_due = max(0.0, round(grand_total - new_inv_paid, 2))
        new_inv_status = "paid" if new_inv_due <= 0.005 else "partial"
        await db.invoices.update_one(
            {"invoice_id": invoice["invoice_id"]},
            {
                "$push": {"payments": new_payment},
                "$set": {
                    "paid_total": new_inv_paid,
                    "due_total": new_inv_due,
                    "status": new_inv_status,
                },
            },
        )
    else:
        new_inv_status = "paid" if fully_settled else "partial"
        new_inv_paid = new_paid

    # 3) Sync fitting denorm (so the Fittings table reflects new balance/status)
    await db.ha_fittings.update_one(
        {"quick_sale_id": quick_sale_id},
        {"$set": {
            "amount_paid": new_paid,
            "balance_due": max(0.0, new_balance),
            "payment_status": new_payment_status,
            "updated_at": now_iso,
        }},
    )

    # 4) Audit
    await db.audit_logs.insert_one({
        "kind": "quick_sale_balance_paid",
        "quick_sale_id": quick_sale_id,
        "invoice_id": qs.get("invoice_id"),
        "clinic_id": user["clinic_id"],
        "actor_user_id": user["user_id"],
        "actor_name": user.get("name", ""),
        "amount": pay_amount,
        "mode": pay_mode,
        "balance_after": max(0.0, new_balance),
        "fully_settled": fully_settled,
        "at": now_iso,
    })

    log.info(
        f"quick-sale balance paid clinic={user['clinic_id']} qs={quick_sale_id} "
        f"+₹{pay_amount} → paid=₹{new_paid} balance=₹{max(0.0,new_balance)} "
        f"settled={fully_settled}"
    )

    return MarkBalancePaidOut(
        quick_sale_id=quick_sale_id,
        invoice_id=qs["invoice_id"],
        invoice_no=qs.get("invoice_no") or "",
        total=float(qs.get("total") or 0),
        amount_paid=new_paid,
        balance_due=max(0.0, new_balance),
        payment_status=new_payment_status,
        invoice_status=new_inv_status,
    )



# ─── Sync inventory (back-fill missing serial_items from a quick-sale) ─

class SyncInventoryOut(BaseModel):
    quick_sale_id: str
    created_serial_ids: List[str]
    skipped: List[dict]                  # {serial_no, reason} for ones we couldn't create
    inventory_tracked: bool


@router.post("/quick-sales/{quick_sale_id}/sync-inventory", response_model=SyncInventoryOut)
async def sync_inventory(
    quick_sale_id: str,
    user=Depends(require_roles(
        "inventory_manager", "clinic_owner", "super_admin",
    )),
    db=Depends(get_db),
):
    """Back-fill ``serial_items`` rows for serials the audiologist typed in
    the Quick Sale form that didn't already exist in inventory.

    Each missing serial is created in state ``SOLD`` (because the unit has
    obviously left the store), linked to the patient + brand/model from the
    sale, and the parent quick-sale doc + fitting doc are flipped to
    ``inventory_tracked=True``.

    Idempotent: if a serial is already in inventory under another sale, we
    skip it and return the reason rather than corrupting state."""
    qs = await db.ha_quick_sales.find_one(
        {"quick_sale_id": quick_sale_id, "clinic_id": user["clinic_id"]},
        {"_id": 0},
    )
    if not qs:
        raise HTTPException(404, "Quick Sale not found")
    if not user_can_see_branch(user, qs.get("branch_id")):
        raise HTTPException(403, "Branch access denied")

    unmatched: list[str] = list(qs.get("unmatched_serials") or [])
    if not unmatched:
        return SyncInventoryOut(
            quick_sale_id=quick_sale_id,
            created_serial_ids=[],
            skipped=[],
            inventory_tracked=True,
        )

    # Try to find a matching product in the catalogue (brand + model exact match,
    # then brand-only). A product_id is optional on serial_items, so if we miss
    # we still create the row.
    brand = (qs.get("brand") or "").strip()
    model = (qs.get("model") or "").strip()
    product = None
    if brand and model:
        product = await db.ha_products.find_one(
            {"clinic_id": user["clinic_id"], "make": {"$regex": f"^{re.escape(brand)}$", "$options": "i"},
             "model": {"$regex": f"^{re.escape(model)}$", "$options": "i"}},
            {"_id": 0, "product_id": 1},
        )
    if not product and brand:
        product = await db.ha_products.find_one(
            {"clinic_id": user["clinic_id"], "make": {"$regex": f"^{re.escape(brand)}$", "$options": "i"}},
            {"_id": 0, "product_id": 1},
        )
    product_id = product["product_id"] if product else None

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    created_ids: list[str] = list(qs.get("consumed_serial_ids") or [])
    new_created: list[str] = []
    skipped: list[dict] = []
    still_unmatched: list[str] = []

    fitting_date = qs.get("fitting_date") or now_iso[:10]

    for serial_no in unmatched:
        serial_no_n = (serial_no or "").strip().upper()
        if not serial_no_n:
            continue

        # If a row was created since the sale was logged, just link it instead
        # of creating a duplicate.
        existing = await db.serial_items.find_one(
            {"clinic_id": user["clinic_id"], "serial_no": serial_no_n}, {"_id": 0},
        )
        if existing:
            if existing.get("state") == "SOLD" and existing.get("current_patient_id") == qs.get("patient_id"):
                # Already linked to this patient — silently absorb (idempotent retry).
                if existing["serial_id"] not in created_ids:
                    created_ids.append(existing["serial_id"])
                continue
            if existing.get("state") == "SOLD":
                skipped.append({
                    "serial_no": serial_no_n,
                    "reason": f"Already SOLD to {existing.get('current_patient_id') or 'another patient'}; manual reconciliation needed.",
                })
                still_unmatched.append(serial_no_n)
                continue
            # IN_STOCK / RESERVED / etc — transition it to SOLD on this sale.
            try:
                await transition_serial(
                    db, existing["serial_id"], "SOLD",
                    actor_user_id=user["user_id"],
                    ref_doc={"kind": "quick_sale", "id": qs.get("sale_no")},
                    note=f"Back-filled via sync-inventory on quick-sale {quick_sale_id}",
                )
                await db.serial_items.update_one(
                    {"serial_id": existing["serial_id"]},
                    {"$set": {
                        "current_patient_id": qs.get("patient_id"),
                        "updated_at": now_iso,
                    }},
                )
                created_ids.append(existing["serial_id"])
            except Exception as exc:  # noqa: BLE001
                skipped.append({"serial_no": serial_no_n, "reason": f"Transition failed: {exc}"})
                still_unmatched.append(serial_no_n)
            continue

        # No existing row — create a fresh SOLD entry.
        new_serial_id = f"SI-{uuid.uuid4().hex[:10].upper()}"
        await db.serial_items.insert_one({
            "serial_id": new_serial_id,
            "clinic_id": user["clinic_id"],
            "branch_id": qs.get("branch_id"),
            "product_id": product_id,
            "serial_no": serial_no_n,
            "state": "SOLD",
            # Default to `saleable` pool so the Inventory Board's group-by-pool
            # aggregation doesn't bucket sync'd Quick-Sale units under "unknown".
            "pool": "saleable",
            "current_patient_id": qs.get("patient_id"),
            "received_at": fitting_date,
            "created_at": now_iso,
            "updated_at": now_iso,
            "source": "quick_sale_sync",
            "make": brand,
            "model": model,
            "ha_type": qs.get("ha_type"),
            "history": [{
                "at": now_iso,
                "actor_user_id": user["user_id"],
                "from_state": None,
                "to_state": "SOLD",
                "ref_doc": {"kind": "quick_sale", "id": qs.get("sale_no")},
                "note": f"Back-filled via Sync Inventory on Quick Sale {quick_sale_id}.",
            }],
        })
        created_ids.append(new_serial_id)
        new_created.append(new_serial_id)
        log.info(f"sync-inventory created serial_item {new_serial_id} for {serial_no_n} (qs={quick_sale_id})")

    inv_tracked = len(still_unmatched) == 0

    # Update parent docs
    await db.ha_quick_sales.update_one(
        {"quick_sale_id": quick_sale_id},
        {"$set": {
            "consumed_serial_ids": created_ids,
            "unmatched_serials": still_unmatched,
            "inventory_tracked": inv_tracked,
            "inventory_synced_at": now_iso if new_created or not still_unmatched else None,
            "inventory_synced_by": user["user_id"],
        }},
    )
    await db.ha_fittings.update_one(
        {"quick_sale_id": quick_sale_id},
        {"$set": {
            "consumed_serial_ids": created_ids,
            "unmatched_serials": still_unmatched,
            "inventory_tracked": inv_tracked,
            "updated_at": now_iso,
        }},
    )

    await db.audit_logs.insert_one({
        "kind": "quick_sale_inventory_synced",
        "quick_sale_id": quick_sale_id,
        "clinic_id": user["clinic_id"],
        "actor_user_id": user["user_id"],
        "actor_name": user.get("name", ""),
        "created_serial_ids": new_created,
        "skipped_count": len(skipped),
        "still_unmatched": still_unmatched,
        "at": now_iso,
    })

    return SyncInventoryOut(
        quick_sale_id=quick_sale_id,
        created_serial_ids=new_created,
        skipped=skipped,
        inventory_tracked=inv_tracked,
    )
