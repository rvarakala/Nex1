"""Shared branch-scope Mongo filter for HA inventory routers.

NAV-010 · Phase 2B · INV-014.

Consolidates the two identical `_branch_scope(user)` helpers previously
duplicated across ``routers/ha_inventory.py`` and ``routers/ha_procurement.py``.
Both prior implementations produced literally identical dicts for the same
user (verified by inspection prior to this refactor); this helper preserves
that behaviour EXACTLY. No new fields, no new short-circuits, no reformat
of the ``branch_id: {"$in": [...]}`` shape.

Semantics
---------
- Users with a role in ``auth.CLINIC_WIDE_ROLES`` see every branch in
  their clinic: ``{"clinic_id": user["clinic_id"]}``.
- Every other user is restricted to the branches enumerated in
  ``user.get("branch_ids")``, defaulting to ``[]`` when absent:
  ``{"clinic_id": user["clinic_id"], "branch_id": {"$in": [...]}}``.

The helper deliberately does NOT consult ``additional_clinic_ids`` (that
is `stock_transfers._accessible_clinic_ids` territory) or any group /
`clinic_groups` membership (that is `stock_requests._accessible_clinic_ids_for_requests`
territory). Behaviour of those two helpers is out of scope for INV-014
and must NOT be folded in here — they have deliberately different
semantics.
"""
from __future__ import annotations

from auth import CLINIC_WIDE_ROLES


def branch_scope(user: dict) -> dict:
    """Return a Mongo filter fragment that restricts to branches this user can see.

    Behaviourally identical to the two prior copies in ``ha_inventory.py``
    and ``ha_procurement.py`` (see INV-014 in NAV-010 Phase 2B).
    """
    if user["role"] in CLINIC_WIDE_ROLES:
        return {"clinic_id": user["clinic_id"]}
    return {
        "clinic_id": user["clinic_id"],
        "branch_id": {"$in": user.get("branch_ids") or []},
    }
