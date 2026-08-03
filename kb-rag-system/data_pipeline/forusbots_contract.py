"""Shared durable-submit contract between the ticket worker and ForUsBots."""

from __future__ import annotations

import hashlib


FORUSBOTS_IDEMPOTENCY_CONTRACT = "forusbots-submit-v1"
FORUSBOTS_IDEMPOTENT_OPERATIONS = frozenset({"participant", "plan"})


def derive_forusbots_idempotency_key(scope: str, operation: str) -> str:
    """Return an opaque stable key for one durable upstream operation."""
    if not isinstance(scope, str) or not scope:
        raise ValueError("ForUsBots durable scope is required")
    if operation not in FORUSBOTS_IDEMPOTENT_OPERATIONS:
        raise ValueError("ForUsBots operation is invalid")
    raw = f"{FORUSBOTS_IDEMPOTENCY_CONTRACT}|{scope}|{operation}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
