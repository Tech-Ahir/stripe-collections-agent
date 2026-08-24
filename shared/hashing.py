"""Canonical hashing.

Two things in this system depend on a hash meaning exactly one thing:

  * `proposals.payload_hash` — the operator approves a specific letter. The gateway
    re-derives the hash from its own copy of the payload and compares. If they differ it
    refuses. Approving one letter therefore cannot be used to send a different one.
  * `audit_events.hash` — each row commits to the row before it, so tampering is visible.

Both need a byte-for-byte deterministic serialisation of a dict, hence `canonical_json`:
sorted keys, no insignificant whitespace, no non-deterministic float or unicode escaping.
Comparison is constant-time and strict. There is deliberately no "normalise then compare"
path — a payload that differs by a space is a different payload.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

PREFIX = "sha256:"

#: The prev_hash of the first audit row. Nothing precedes it.
GENESIS_HASH = PREFIX + "0" * 64


def canonical_json(payload: Any) -> str:
    """Deterministic JSON: sorted keys, tight separators, real UTF-8."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def hash_payload(payload: Any) -> str:
    """`sha256:<hex>` over the canonical JSON of `payload`."""
    return PREFIX + sha256_hex(canonical_json(payload))


def hashes_equal(left: str | None, right: str | None) -> bool:
    """Constant-time, exact comparison. No trimming, no case folding, no coercion."""
    if not left or not right:
        return False
    return hmac.compare_digest(left, right)
