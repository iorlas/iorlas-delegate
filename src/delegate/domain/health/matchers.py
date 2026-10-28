"""FailureText: vendor text with what varies per run replaced, so two runs of one failure compare equal.

Examples: "Request 4821 failed at 2026-10-24T02:31:53Z id 3f2a9c1b77" and "Request 77 failed at 2026-10-24T09:00:00Z id aa00bb11cc" both
become "request # failed at # id #". Table test: tests/domain/test_health.py.
"""
import hashlib
import re

TIME = re.compile(r"\d{4}-\d\d-\d\d[t ]\d\d:\d\d(:\d\d)?(\.\d+)?z?")
IDS = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\b[0-9a-f]{8,}\b|\bses_\w+")
PATH = re.compile(r"(?:/[\w.@~-]+){2,}")
NUMBER = re.compile(r"\d+")
SPACE = re.compile(r"\s+")


def norm(text: str | None) -> str:
    s = (text or "").lower()
    s = TIME.sub("#", s)
    s = IDS.sub("#", s)
    s = PATH.sub("<p>", s)
    s = NUMBER.sub("#", s)
    return SPACE.sub(" ", s).strip()[:200]


def fingerprint(outcome: str, source: str, text: str | None) -> str:
    """12 chars naming one failure: the same outcome from the same channel with the same normalised text."""
    return hashlib.sha1(f"{outcome}|{source}|{norm(text)}".encode()).hexdigest()[:12]
