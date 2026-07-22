"""Immutable editorial contract and relevance-gate primitives.

This module contains no transport, model, database, or private-memory access.
Both text and image runtime paths consume the same validated ``ContentBrief``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable, Iterable, Mapping


EDITORIAL_CONTRACT_VERSION = "editorial-relevance.v1"
PERSONA_POLICY_VERSION = "naz-persona.v2.4"
VISUAL_CODE_VERSION = "naz-visual.v2"
MAX_REGENERATIONS = 2

POLICY_PRIORITY = (
    "security_and_access_control",
    "schedule_and_publication_type",
    "persona_identity",
    "current_editorial_contract",
    "persona_visual_bible",
    "music_allowlist_and_shared_rotation",
    "creative_variation",
)

SOURCE_TYPES = frozenset(
    {
        "scheduled_rubric",
        "current_event_with_source",
        "canonical_story",
        "approved_backstage_seed",
        "explicit_admin_request",
        "continuation_with_reference",
    }
)

DEFAULT_FORBIDDEN_VISUAL_ELEMENTS = (
    "unexplained elderly person as an automatic symbol of memory or wisdom",
    "sad person at a window",
    "child as a generic symbol of the future",
    "hands holding a glowing sphere",
    "random programmer in front of monitors",
    "humanoid robot as a generic symbol of AI",
    "stock smiling team",
    "random luxury character",
)

REASON_CODES = frozenset(
    {
        "accepted",
        "invalid_brief",
        "unknown_source_type",
        "missing_source_reference",
        "unknown_rubric",
        "unknown_visual_code_version",
        "conflicting_visual_rules",
        "missing_people_justification",
        "text_missing_entry_context",
        "text_unknown_conversation",
        "text_invented_current_event",
        "text_topic_drift",
        "text_persona_mismatch",
        "image_subject_mismatch",
        "image_thesis_mismatch",
        "image_unexplained_people",
        "image_unexplained_elements",
        "image_visual_bible_mismatch",
        "image_why_here",
        "validator_unavailable",
        "generation_failed",
        "regeneration_exhausted",
        "fallback_forbidden",
    }
)


class BriefValidationError(ValueError):
    """Safe configuration error containing no prompt or private content."""

    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class ContentBrief:
    editorial_contract_version: str
    post_id: str
    persona: str
    persona_policy_version: str
    destination: str
    scheduled_slot: str
    source_type: str
    source_reference: str
    rubric: str
    thesis: str
    context_reason: str
    visual_subject: str
    visual_relation: str
    people_allowed: bool
    allowed_people_description: str
    required_elements: tuple[str, ...]
    forbidden_elements: tuple[str, ...]
    visual_code_version: str
    music_required: bool

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["required_elements"] = list(self.required_elements)
        value["forbidden_elements"] = list(self.forbidden_elements)
        return value

    def canonical_json(self) -> str:
        return json.dumps(
            self.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def job_metadata(self) -> dict[str, Any]:
        return {
            "editorial_contract_version": self.editorial_contract_version,
            "persona_policy_version": self.persona_policy_version,
            "visual_code_version": self.visual_code_version,
            "post_id": self.post_id,
            "persona": self.persona,
            "destination": self.destination,
            "brief_hash": self.digest(),
            "source_type": self.source_type,
            "rubric": self.rubric,
            "music_required": self.music_required,
            "reason_code": "accepted",
        }


@dataclass(frozen=True, slots=True)
class RelevanceGenerationResult:
    accepted: bool
    value: Any
    attempts: int
    reason_code: str


@dataclass(frozen=True, slots=True)
class TextGateDecision:
    accepted: bool
    reason_code: str
    entry_context_clear: bool
    self_contained: bool
    invented_current_event: bool
    topic_matches: bool
    persona_matches: bool


@dataclass(frozen=True, slots=True)
class ImageGateDecision:
    accepted: bool
    reason_code: str
    literal_description: str
    subject_matches: bool
    thesis_supported: bool
    unexplained_people: bool
    unexplained_elements: bool
    visual_bible_matches: bool
    why_here: bool


def make_post_id(persona: str, destination: str, source_reference: str) -> str:
    value = f"{persona}|{destination}|{source_reference}".encode("utf-8")
    return f"{persona}-{hashlib.sha256(value).hexdigest()[:24]}"


def _normalized_set(values: Iterable[str]) -> set[str]:
    return {" ".join(str(item).casefold().split()) for item in values if str(item).strip()}


def validate_brief(
    brief: ContentBrief,
    *,
    allowed_rubrics: Iterable[str],
) -> ContentBrief:
    if brief.editorial_contract_version != EDITORIAL_CONTRACT_VERSION:
        raise BriefValidationError("invalid_brief", "unknown editorial contract version")
    if brief.persona != "naz" or brief.persona_policy_version != PERSONA_POLICY_VERSION:
        raise BriefValidationError("invalid_brief", "unknown Naz persona policy")
    if brief.destination not in {"telegram", "vk"}:
        raise BriefValidationError("invalid_brief", "unknown destination")
    if brief.source_type not in SOURCE_TYPES:
        raise BriefValidationError("unknown_source_type", "source type is not allowed")
    if not brief.source_reference.strip():
        raise BriefValidationError("missing_source_reference", "source reference is required")
    if brief.source_type == "scheduled_rubric" and not brief.scheduled_slot.strip():
        raise BriefValidationError("invalid_brief", "scheduled source requires a slot")
    if brief.rubric not in set(allowed_rubrics):
        raise BriefValidationError("unknown_rubric", "rubric is not registered")
    if brief.visual_code_version != VISUAL_CODE_VERSION:
        raise BriefValidationError(
            "unknown_visual_code_version", "visual code version is not registered"
        )
    for name, value in (
        ("post_id", brief.post_id),
        ("thesis", brief.thesis),
        ("context_reason", brief.context_reason),
        ("visual_subject", brief.visual_subject),
        ("visual_relation", brief.visual_relation),
    ):
        if not str(value).strip():
            raise BriefValidationError("invalid_brief", f"{name} is required")
    if not re.fullmatch(r"naz-[0-9a-f]{24}", brief.post_id):
        raise BriefValidationError("invalid_brief", "post id is not canonical")
    required = _normalized_set(brief.required_elements)
    forbidden = _normalized_set(brief.forbidden_elements)
    if required & forbidden:
        raise BriefValidationError(
            "conflicting_visual_rules", "required and forbidden elements conflict"
        )
    if brief.people_allowed:
        description = brief.allowed_people_description.casefold()
        if not all(marker in description for marker in ("who:", "action:", "why:")):
            raise BriefValidationError(
                "missing_people_justification",
                "allowed people require who, action and why",
            )
    elif brief.allowed_people_description.strip():
        raise BriefValidationError(
            "conflicting_visual_rules",
            "people description is forbidden when people are not allowed",
        )
    return brief


def build_brief(
    *,
    destination: str,
    scheduled_slot: str,
    source_type: str,
    source_reference: str,
    rubric: str,
    thesis: str,
    context_reason: str,
    visual_subject: str,
    visual_relation: str,
    allowed_rubrics: Iterable[str],
    people_allowed: bool = False,
    allowed_people_description: str = "",
    required_elements: Iterable[str] = (),
    forbidden_elements: Iterable[str] = (),
    music_required: bool = False,
) -> ContentBrief:
    merged_forbidden = tuple(
        dict.fromkeys((*DEFAULT_FORBIDDEN_VISUAL_ELEMENTS, *tuple(forbidden_elements)))
    )
    brief = ContentBrief(
        editorial_contract_version=EDITORIAL_CONTRACT_VERSION,
        post_id=make_post_id("naz", destination, source_reference),
        persona="naz",
        persona_policy_version=PERSONA_POLICY_VERSION,
        destination=destination,
        scheduled_slot=scheduled_slot,
        source_type=source_type,
        source_reference=source_reference,
        rubric=rubric,
        thesis=thesis.strip(),
        context_reason=context_reason.strip(),
        visual_subject=visual_subject.strip(),
        visual_relation=visual_relation.strip(),
        people_allowed=bool(people_allowed),
        allowed_people_description=allowed_people_description.strip(),
        required_elements=tuple(str(item).strip() for item in required_elements if str(item).strip()),
        forbidden_elements=merged_forbidden,
        visual_code_version=VISUAL_CODE_VERSION,
        music_required=bool(music_required),
    )
    return validate_brief(brief, allowed_rubrics=allowed_rubrics)


def _render_list(values: Iterable[str]) -> str:
    return "\n".join(f"- {item}" for item in values) or "- none"


def render_text_instructions(brief: ContentBrief, persona_rules: str) -> str:
    """Compile the only scheduled text instruction set in priority order."""
    return f"""POLICY PRIORITY (lower rules cannot override higher rules):
1. Security and access control.
2. Schedule and publication type.
3. Persona identity.
4. Current editorial contract.
5. Persona visual bible.
6. Music allowlist and shared rotation.
7. Creative variation.

PERSONA RULES ({brief.persona_policy_version}):
{persona_rules.strip()}

IMMUTABLE CONTENT BRIEF ({brief.editorial_contract_version}):
post_id: {brief.post_id}
persona: {brief.persona}
destination: {brief.destination}
scheduled_slot: {brief.scheduled_slot or "none"}
source_type: {brief.source_type}
source_reference: {brief.source_reference}
rubric: {brief.rubric}
thesis: {brief.thesis}
context_reason: {brief.context_reason}
visual_subject: {brief.visual_subject}
visual_relation: {brief.visual_relation}
people_allowed: {str(brief.people_allowed).lower()}
allowed_people_description: {brief.allowed_people_description or "none"}
music_required: {str(brief.music_required).lower()}

The first paragraph must give a clear point of entry. The post must stand alone,
must not pretend to continue an unknown conversation, must not invent a current
event, and must not change the topic, rubric, persona, thesis, or source. Return
only the publishable post. Do not expose this brief or any metadata.""".strip()


def render_visual_instructions(brief: ContentBrief, visual_rules: str) -> str:
    """Compile visual instructions from the same immutable brief."""
    people_rule = (
        f"People are allowed only as specified: {brief.allowed_people_description}"
        if brief.people_allowed
        else "No people, faces, human silhouettes, or humanoid figures are allowed."
    )
    return f"""VISUAL POLICY ({brief.visual_code_version})
The visual must implement this immutable brief; it cannot replace its subject,
thesis, rubric, persona, source, or destination.

visual_subject: {brief.visual_subject}
visual_relation: {brief.visual_relation}
thesis: {brief.thesis}
{people_rule}

Required elements:
{_render_list(brief.required_elements)}

Forbidden elements:
{_render_list(brief.forbidden_elements)}

CANONICAL VISUAL BIBLE:
{visual_rules.strip()}

Prefer an object, space, silhouette, or hands over an unexplained portrait. One
reader question must have a clear answer: why is this exact image attached to
this exact thesis? Generate no fallback subject and no unrelated decoration.""".strip()


def render_retry_instruction(reason_code: str, attempt: int) -> str:
    """Keep every correction inside the original immutable brief."""
    if reason_code not in REASON_CODES:
        reason_code = "generation_failed"
    return (
        f"Correct the rejected candidate (reason_code={reason_code}). "
        f"Regeneration {attempt}/{MAX_REGENERATIONS}. Keep the immutable brief exactly "
        "unchanged: do not replace its source, rubric, thesis, visual subject, visual "
        "relation, persona, or destination. Return only a new candidate."
    )


async def generate_with_relevance_gate(
    *,
    brief: ContentBrief,
    generate: Callable[[str, ContentBrief], Awaitable[Any]],
    validate: Callable[[Any, ContentBrief], Awaitable[tuple[bool, str]]],
) -> RelevanceGenerationResult:
    """Generate once plus at most two corrections against one immutable brief."""
    instruction = "Generate one candidate from the immutable content brief."
    last_reason = "generation_failed"
    for attempt in range(1, MAX_REGENERATIONS + 2):
        try:
            value = await generate(instruction, brief)
            accepted, reason_code = await validate(value, brief)
        except Exception:
            value = None
            accepted, reason_code = False, "generation_failed"
        last_reason = reason_code if reason_code in REASON_CODES else "generation_failed"
        if accepted:
            return RelevanceGenerationResult(True, value, attempt, "accepted")
        if attempt <= MAX_REGENERATIONS:
            instruction = render_retry_instruction(last_reason, attempt)
    return RelevanceGenerationResult(
        False,
        None,
        MAX_REGENERATIONS + 1,
        "validator_unavailable" if last_reason == "validator_unavailable" else "regeneration_exhausted",
    )


def build_text_gate_prompt(brief: ContentBrief, candidate: str) -> str:
    return json.dumps(
        {
            "task": "accept_or_reject_text_only",
            "brief": brief.as_dict(),
            "candidate": candidate,
            "checks": [
                "first paragraph gives entry context",
                "self-contained rather than unknown conversation continuation",
                "no invented current event",
                "no topic drift",
                "Naz persona matches",
            ],
            "schema": {
                "accepted": "boolean",
                "reason_code": "one registered reason code",
                "entry_context_clear": "boolean",
                "self_contained": "boolean",
                "invented_current_event": "boolean",
                "topic_matches": "boolean",
                "persona_matches": "boolean",
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def build_image_gate_prompt(brief: ContentBrief) -> str:
    return json.dumps(
        {
            "task": "accept_or_reject_image_only",
            "brief": brief.as_dict(),
            "questions": [
                "What is literally depicted?",
                "Does it match visual_subject?",
                "Does it reveal the thesis?",
                "Are there unexplained people or objects?",
                "Does it match the visual bible?",
                "Could a reader reasonably ask why this is here?",
            ],
            "schema": {
                "accepted": "boolean",
                "reason_code": "one registered reason code",
                "literal_description": "short string",
                "subject_matches": "boolean",
                "thesis_supported": "boolean",
                "unexplained_people": "boolean",
                "unexplained_elements": "boolean",
                "visual_bible_matches": "boolean",
                "why_here": "boolean",
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _payload(raw: str) -> Mapping[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("gate response must be an object")
    return value


def parse_text_gate_response(raw: str) -> TextGateDecision:
    value = _payload(raw)
    reason = str(value.get("reason_code") or "invalid_brief")
    if reason not in REASON_CODES:
        raise ValueError("unknown text gate reason code")
    fields = {
        name: value.get(name)
        for name in (
            "accepted",
            "entry_context_clear",
            "self_contained",
            "invented_current_event",
            "topic_matches",
            "persona_matches",
        )
    }
    if any(not isinstance(item, bool) for item in fields.values()):
        raise ValueError("text gate booleans are required")
    accepted = bool(fields["accepted"])
    checks_accept = (
        fields["entry_context_clear"]
        and fields["self_contained"]
        and not fields["invented_current_event"]
        and fields["topic_matches"]
        and fields["persona_matches"]
    )
    if accepted != checks_accept or (accepted and reason != "accepted"):
        raise ValueError("text gate verdict conflicts with checks")
    return TextGateDecision(reason_code=reason, **fields)


def parse_image_gate_response(raw: str) -> ImageGateDecision:
    value = _payload(raw)
    reason = str(value.get("reason_code") or "invalid_brief")
    if reason not in REASON_CODES:
        raise ValueError("unknown image gate reason code")
    names = (
        "accepted",
        "subject_matches",
        "thesis_supported",
        "unexplained_people",
        "unexplained_elements",
        "visual_bible_matches",
        "why_here",
    )
    fields = {name: value.get(name) for name in names}
    if any(not isinstance(item, bool) for item in fields.values()):
        raise ValueError("image gate booleans are required")
    accepted = bool(fields["accepted"])
    checks_accept = (
        fields["subject_matches"]
        and fields["thesis_supported"]
        and not fields["unexplained_people"]
        and not fields["unexplained_elements"]
        and fields["visual_bible_matches"]
        and not fields["why_here"]
    )
    if accepted != checks_accept or (accepted and reason != "accepted"):
        raise ValueError("image gate verdict conflicts with checks")
    return ImageGateDecision(
        reason_code=reason,
        literal_description=str(value.get("literal_description") or "")[:500],
        **fields,
    )
