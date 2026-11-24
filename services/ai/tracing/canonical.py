"""Canonical encoding and digests for the turn trace ledger.

Every run owns a chain: the digest of an event is a SHA-256 over the canonical
JSON of the event envelope, which includes the previous digest. Because the
workbench recomputes the same values when a person opens a trace, the canonical
form here and ``web/src/lib/server/trace/digest.ts`` must stay byte-identical.
Conventions that make that possible:

* keys are sorted, separators are ``,`` and ``:`` with no spaces;
* non-ASCII characters are emitted as UTF-8, not escaped;
* payload values are strings, integers, booleans, null, or nested objects of
  those - floats are rejected because Python and JavaScript format them
  differently.

Changing the algorithm therefore means bumping ``ALGORITHM``/``SEAL_VERSION``
rather than editing the constants in place.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

ALGORITHM = "sha256.canonical-json.v1"
SEAL_VERSION = "hmac-sha256.v1"

_PAYLOAD_SCALAR = (str, int, bool, type(None))


class UncensusedValueError(ValueError):
    """Raised when a payload value cannot be canonicalised consistently."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _check_value(value: Any, path: str = "payload") -> None:
    if isinstance(value, _PAYLOAD_SCALAR):
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _check_value(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise UncensusedValueError(f"{path} has a non-string key")
            _check_value(item, f"{path}.{key}")
        return
    raise UncensusedValueError(f"{path} has an unsupported value of type {type(value).__name__}")


def event_digest(
    *,
    run_id: str,
    seq: int,
    event_id: str,
    span_id: str,
    parent_span_id: str | None,
    type: str,
    ts: str,
    payload: dict[str, Any],
    previous_digest: str | None,
) -> str:
    """Digest one event envelope, chaining it to ``previous_digest``."""
    _check_value(payload)
    envelope = {
        "algorithm": ALGORITHM,
        "event": payload,
        "eventId": event_id,
        "parentSpanId": parent_span_id,
        "previousDigest": previous_digest,
        "runId": run_id,
        "seq": seq,
        "spanId": span_id,
        "ts": ts,
        "type": type,
    }
    return hashlib.sha256(canonical_json(envelope).encode("utf-8")).hexdigest()


def seal_digest(key: bytes, *, run_id: str, last_seq: int, head_digest: str | None) -> str:
    """Keyed seal over the finished chain head.

    The seal lets a reader who only has database access detect a wholesale
    rewrite of the chain, because re-signing requires the application key.
    """
    body = canonical_json(
        {
            "algorithm": SEAL_VERSION,
            "headDigest": head_digest,
            "lastSeq": last_seq,
            "runId": run_id,
        }
    )
    return hmac.new(key, body.encode("utf-8"), hashlib.sha256).hexdigest()
