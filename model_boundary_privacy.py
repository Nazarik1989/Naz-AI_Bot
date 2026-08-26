"""Shared fail-closed privacy vocabulary for model boundaries."""

from __future__ import annotations

import re


CREDENTIAL_MARKER = re.compile(
    r"(?i)(?:api[_-]?key|authorization|bearer\s|password|credential|"
    r"narrative_normalizer_trust_key|naz_ai_bot\.sqlite3|"
    r"review-authority|registry\.json|sk-[A-Za-z0-9_-]{8,})"
)
CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?im)^\s*(?:[A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)[A-Z0-9_]*)\s*[:=]\s*\S+"
)
ENV_ASSIGNMENT = re.compile(
    r"(?m)(?:^|\n)\s*[A-Z][A-Z0-9_]{1,63}\s*=\s*[^\n]+(?:\n|$)"
)
ABSOLUTE_PATH = re.compile(
    r"(?:[A-Za-z]:[\\/]|\\\\[^\\/\s]+[\\/][^\s]+|file:///(?:[^\s]+)|"
    r"(?<![A-Za-z0-9_:/])/(?!/)[^\s\"'<>\]\)]+|(?<![A-Za-z0-9_])~[\\/])"
)


def contains_forbidden_outbound_text(value: str) -> bool:
    """Return whether exact text is forbidden at a live provider boundary."""

    if type(value) is not str:
        raise TypeError("value")
    return any(pattern.search(value) is not None for pattern in (
        CREDENTIAL_MARKER,
        CREDENTIAL_ASSIGNMENT,
        ENV_ASSIGNMENT,
        ABSOLUTE_PATH,
    ))


__all__ = (
    "ABSOLUTE_PATH",
    "CREDENTIAL_ASSIGNMENT",
    "CREDENTIAL_MARKER",
    "ENV_ASSIGNMENT",
    "contains_forbidden_outbound_text",
)
