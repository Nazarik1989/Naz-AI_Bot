"""Closed, versioned live-run profiles for the Narrative Normalizer.

This module owns only immutable policy values.  Importing it performs no
environment reads, provider construction, filesystem access, or process work.
"""
from __future__ import annotations

from dataclasses import dataclass


CANARY_RUN_PROFILE = "normalizer-live-run-canary-v1"
FIRST_FIVE_RUN_PROFILE = "normalizer-live-run-first-five-v1"

# These are the internal provider operation names for the five generic E3
# stages.  Repair is intentionally absent from both live profiles.
GENERIC_LIVE_OPERATIONS = (
    "evidence_coverage",
    "evidence_extraction",
    "evidence_adjudication",
    "generation",
    "adjudication",
)


@dataclass(frozen=True, slots=True)
class LiveRunProfileRule:
    profile: str
    source_count: int
    call_budget: int
    allowed_operations: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            type(self.profile) is not str
            or type(self.source_count) is not int
            or type(self.call_budget) is not int
            or type(self.allowed_operations) is not tuple
            or self.source_count < 1
            or self.call_budget != self.source_count * len(self.allowed_operations)
            or len(set(self.allowed_operations)) != len(self.allowed_operations)
            or any(type(item) is not str or not item for item in self.allowed_operations)
        ):
            raise TypeError("live run profile")


LIVE_RUN_PROFILE_RULES = (
    LiveRunProfileRule(CANARY_RUN_PROFILE, 1, 5, GENERIC_LIVE_OPERATIONS),
    LiveRunProfileRule(FIRST_FIVE_RUN_PROFILE, 5, 25, GENERIC_LIVE_OPERATIONS),
)

LIVE_RUN_PROFILES = tuple(rule.profile for rule in LIVE_RUN_PROFILE_RULES)


def resolve_live_run_profile(value: object) -> LiveRunProfileRule:
    """Return the one code-owned rule matching an exact plain profile value."""

    if type(value) is not str:
        raise ValueError("live run profile")
    matches = tuple(rule for rule in LIVE_RUN_PROFILE_RULES if rule.profile == value)
    if len(matches) != 1:
        raise ValueError("live run profile")
    return matches[0]


__all__ = (
    "CANARY_RUN_PROFILE",
    "FIRST_FIVE_RUN_PROFILE",
    "GENERIC_LIVE_OPERATIONS",
    "LIVE_RUN_PROFILE_RULES",
    "LIVE_RUN_PROFILES",
    "LiveRunProfileRule",
    "resolve_live_run_profile",
)
