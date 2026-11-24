"""Minimal SSE frame helpers used by the tap.

Kept local so the tap can be unit-tested without importing the streaming stack.
"""

from __future__ import annotations


def sse_event_type(raw: str) -> str:
    for line in raw.split("\n"):
        if line.startswith("event:"):
            return line[len("event:") :].strip()
    return "message"


def sse_event_data(raw: str) -> str:
    for line in raw.split("\n"):
        if line.startswith("data:"):
            return line[len("data:") :].strip()
    return ""
