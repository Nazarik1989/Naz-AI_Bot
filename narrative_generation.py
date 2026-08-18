"""Pure, provider-neutral narrative generation for Checkpoint 2.

The module turns explicit immutable context into three model-authored drafts,
binds all authoritative data in code, and delegates the final trust decision to
the locked Checkpoint 1 validator.  Importing it performs no I/O.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import asdict, dataclass, is_dataclass
from difflib import SequenceMatcher
from typing import Any, Callable, Mapping, Protocol

import narrative_translator as contract


GENERATION_SCHEMA = "narrative-generation-drafts-v1"
ADJUDICATION_SCHEMA = "narrative-adjudication-batch-v1"
GENERATION_CONTRACT_VERSION = "narrative-generation-contract-v1"
DRAFT_BINDING_VERSION = "narrative-draft-binding-v1"
AUTHORITY_CONTEXT_BINDING_VERSION = "narrative-authority-context-binding-v1"
REPAIR_SCHEMA_VERSION = "narrative-generation-repair-v1"
REPAIR_RULES_VERSION = "structural-and-context-repair-v1"

SEMANTIC_AUTHORITY = "local-narrative-adjudicator"
CHARACTER_AUTHORITY = "local-character-continuity-adjudicator"
RELATIONSHIP_AUTHORITY = "local-relationship-adjudicator"
VISUAL_AUTHORITY = "local-visual-adjudicator"
SEMANTIC_RULES = "narrative-adjudication-v1"
CHARACTER_RULES = "character-continuity-adjudication-v1"
RELATIONSHIP_RULES = "relationship-adjudication-v1"
VISUAL_RULES = "visual-grounding-adjudication-v1"

STORY_FIELDS = ("hook", "human_problem", "tension", "turning_point", "resolution")
CHARACTER_IDS = frozenset({"naz", "void"})
PRESENCE_MODES = frozenset({"none", "implicit", "explicit"})
VISUAL_MODES = frozenset({"cinematic", "documentary", "artifact"})
SUBJECT_KINDS = frozenset({"object", "naz", "void", "source_human", "source_nonhuman_agent"})
FORBIDDEN_AUTHORITY_KEYS = frozenset({
    "plan_id", "source_ref", "state_snapshot_ref", "relationship_snapshot_ref",
    "revision", "core_version", "source_hash", "authority_ref", "rules_version",
    "statement_digest", "interpretation_digest", "visual_digest", "duo_context_digest",
    "snapshot_ref", "canon_snapshot_ref", "canon_version", "source_version",
    "validation_contract_version", "policy_contract_version", "evidence",
    "draft_digest", "relationship_payload_digest", "visual_payload_digest",
    "editorial_alignment_digest", "authority_context_digest",
})
ADJUDICATION_REASON_CODES = frozenset({
    "candidate_unsupported", "insufficient_support", "unsupported_fact",
    "continuity_conflict", "relationship_conflict", "visual_ungrounded",
})


class NarrativeGenerationError(ValueError):
    """Safe failure carrying only stable reason codes."""

    def __init__(self, *reason_codes: str, repairable: bool = False):
        codes = tuple(sorted(set(reason_codes))) or ("narrative_generation_failed",)
        self.reason_codes = codes
        self.repairable = repairable
        super().__init__(",".join(codes))


def _plain_str(value: object, field: str) -> str:
    if type(value) is not str or not value.strip():
        raise NarrativeGenerationError(f"{field}_invalid", repairable=True)
    return value


def _optional_str(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _plain_str(value, field)


def _plain_int(value: object, field: str, *, minimum: int | None = None) -> int:
    if type(value) is not int or (minimum is not None and value < minimum):
        raise NarrativeGenerationError(f"{field}_invalid", repairable=True)
    return value


def _strings(value: object, field: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    if type(value) not in (tuple, list):
        raise NarrativeGenerationError(f"{field}_invalid", repairable=True)
    result = tuple(_plain_str(item, f"{field}_item") for item in value)
    if not allow_empty and not result:
        raise NarrativeGenerationError(f"{field}_missing", repairable=True)
    if len(result) != len(set(result)):
        raise NarrativeGenerationError(f"{field}_duplicate", repairable=True)
    return result


def _typed_tuple(value: object, cls: type, field: str, *, allow_empty: bool = True) -> tuple:
    if type(value) not in (tuple, list) or any(type(item) is not cls for item in value):
        raise TypeError(field)
    result = tuple(value)
    if not allow_empty and not result:
        raise ValueError(field)
    return result


def _canonical(value: object) -> str:
    if is_dataclass(value):
        value = asdict(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


@dataclass(frozen=True, slots=True)
class NarrativeEditorialContext:
    plan_id: str
    source_ref: str
    production_mode: str
    content_format: str
    semantic_theme: str
    semantic_card: str
    facet: str
    author_role: str
    emotional_arc: str
    reader_relation: str
    structure: str
    hook: str
    ending: str
    energy: str
    seriousness: str
    tempo: str
    humor: str
    imagery: str
    visual_mode: str
    visual_subject_direction: str
    visual_relation: str
    editorial_ref_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            if name != "editorial_ref_ids":
                _plain_str(getattr(self, name), name)
        object.__setattr__(self, "editorial_ref_ids", _strings(self.editorial_ref_ids, "editorial_ref_ids", allow_empty=False))
        if self.production_mode != "story_first" or self.content_format != "story_pack":
            raise ValueError("story_first story_pack context required")


@dataclass(frozen=True, slots=True)
class CharacterPromptContext:
    character_id: str
    canon_ref_ids: tuple[str, ...]
    canon_version: str
    state_snapshot_ref: str
    prompt_text: str

    def __post_init__(self) -> None:
        _plain_str(self.character_id, "character_id")
        if self.character_id not in CHARACTER_IDS:
            raise ValueError("unknown character")
        object.__setattr__(self, "canon_ref_ids", _strings(self.canon_ref_ids, "canon_ref_ids", allow_empty=False))
        for name in ("canon_version", "state_snapshot_ref", "prompt_text"):
            _plain_str(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class RelationshipPromptContext:
    relationship_snapshot_ref: str
    prompt_text: str

    def __post_init__(self) -> None:
        _plain_str(self.relationship_snapshot_ref, "relationship_snapshot_ref")
        _plain_str(self.prompt_text, "prompt_text")


@dataclass(frozen=True, slots=True)
class NarrativeGenerationInput:
    source_ref: str
    source_facts: tuple[contract.SourceFact, ...]
    editorial_plan: NarrativeEditorialContext
    naz_state: contract.CharacterStateSnapshot
    void_state: contract.CharacterStateSnapshot
    relationship_state: contract.RelationshipStateSnapshot | None
    naz_canon: contract.CharacterCanonSnapshot
    void_canon: contract.CharacterCanonSnapshot
    naz_prompt_context: CharacterPromptContext
    void_prompt_context: CharacterPromptContext
    relationship_prompt_context: RelationshipPromptContext | None
    diversity_context: contract.NarrativeDiversityContext

    def __post_init__(self) -> None:
        _plain_str(self.source_ref, "source_ref")
        object.__setattr__(self, "source_facts", _typed_tuple(self.source_facts, contract.SourceFact, "source_facts", allow_empty=False))
        if type(self.editorial_plan) is not NarrativeEditorialContext:
            raise TypeError("editorial_plan")
        for value, cls, field in (
            (self.naz_state, contract.CharacterStateSnapshot, "naz_state"),
            (self.void_state, contract.CharacterStateSnapshot, "void_state"),
            (self.naz_canon, contract.CharacterCanonSnapshot, "naz_canon"),
            (self.void_canon, contract.CharacterCanonSnapshot, "void_canon"),
            (self.naz_prompt_context, CharacterPromptContext, "naz_prompt_context"),
            (self.void_prompt_context, CharacterPromptContext, "void_prompt_context"),
            (self.diversity_context, contract.NarrativeDiversityContext, "diversity_context"),
        ):
            if type(value) is not cls:
                raise TypeError(field)
        if self.relationship_state is not None and type(self.relationship_state) is not contract.RelationshipStateSnapshot:
            raise TypeError("relationship_state")
        if self.relationship_prompt_context is not None and type(self.relationship_prompt_context) is not RelationshipPromptContext:
            raise TypeError("relationship_prompt_context")
        if self.source_ref != self.editorial_plan.source_ref:
            raise ValueError("source binding")
        if (self.naz_state.character_id, self.void_state.character_id) != ("naz", "void"):
            raise ValueError("state character binding")
        if (self.naz_canon.character_id, self.void_canon.character_id) != ("naz", "void"):
            raise ValueError("canon character binding")
        if (self.naz_prompt_context.character_id, self.void_prompt_context.character_id) != ("naz", "void"):
            raise ValueError("prompt character binding")
        if self.naz_prompt_context.state_snapshot_ref != self.naz_state.snapshot_ref or self.void_prompt_context.state_snapshot_ref != self.void_state.snapshot_ref:
            raise ValueError("prompt state binding")
        naz_ids = {item.source_id for item in self.naz_canon.canon_refs}
        void_ids = {item.source_id for item in self.void_canon.canon_refs}
        if set(self.naz_prompt_context.canon_ref_ids) != naz_ids or set(self.void_prompt_context.canon_ref_ids) != void_ids:
            raise ValueError("prompt canon binding")
        if self.relationship_prompt_context is not None:
            if self.relationship_state is None or self.relationship_prompt_context.relationship_snapshot_ref != self.relationship_state.snapshot_ref:
                raise ValueError("relationship prompt binding")
        fact_ids = tuple(item.fact_id for item in self.source_facts)
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("duplicate facts")


@dataclass(frozen=True, slots=True)
class NarrativeModelRequest:
    request_kind: str
    model: str
    system_prompt: str
    user_prompt: str
    response_schema: Mapping[str, object]


class NarrativeModelClient(Protocol):
    def generate_json(self, request: NarrativeModelRequest) -> Mapping[str, object] | str:
        """Return a structured mapping or a raw JSON document."""


@dataclass(frozen=True, slots=True)
class DraftStatement:
    text: str
    inference_kind: str
    source_fact_refs: tuple[str, ...]
    editorial_refs: tuple[str, ...]
    canon_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DraftCharacterInterpretation:
    character_id: str
    text: str
    source_fact_refs: tuple[str, ...]
    canon_refs: tuple[str, ...]
    interpretation_mode: str
    thematic_axis: str
    emotional_register: str
    rhetorical_form: str
    narrative_distance: str
    humor_mode: str
    sarcasm_target: str | None
    ending_mode: str
    continuity_basis: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DraftVisualSubject:
    subject_kind: str
    character_id: str | None
    source_fact_refs: tuple[str, ...]
    identity_canon_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DraftVisualDirection:
    mode_hint: str
    narrative_subject: str
    human_presence_policy: str
    nonhuman_presence_policy: str
    approved_motifs: tuple[str, ...]
    excluded_motifs: tuple[str, ...]
    source_fact_refs: tuple[str, ...]
    visual_canon_refs: tuple[str, ...]
    subjects: tuple[DraftVisualSubject, ...]


@dataclass(frozen=True, slots=True)
class NarrativeDraft:
    candidate_id: str
    rank: int
    primary_character_id: str
    secondary_character_id: str | None
    presence_mode: str
    hook: DraftStatement
    human_problem: DraftStatement
    tension: DraftStatement
    turning_point: DraftStatement
    resolution: DraftStatement
    primary_interpretation: DraftCharacterInterpretation
    secondary_interpretation: DraftCharacterInterpretation | None
    interaction_mode: str | None
    relation_to_story: str | None
    visual_direction: DraftVisualDirection
    story_type: str


@dataclass(frozen=True, slots=True)
class NarrativeAuthorityContextBinding:
    binding_version: str
    plan_binding_digest: str
    source_payload_digest: str
    naz_state_digest: str
    void_state_digest: str
    relationship_state_digest: str | None
    naz_canon_digest: str
    void_canon_digest: str
    naz_prompt_context_digest: str
    void_prompt_context_digest: str
    relationship_prompt_context_digest: str | None
    diversity_context_digest: str
    evidence_policy_digest: str
    validation_contract_version: str
    authority_context_digest: str


@dataclass(frozen=True, slots=True)
class StatementBinding:
    statement_name: str
    statement_digest: str


@dataclass(frozen=True, slots=True)
class NarrativeDraftBindings:
    candidate_id: str
    authority_context_digest: str
    draft_digest: str
    statement_bindings: tuple[StatementBinding, ...]
    primary_interpretation_digest: str
    secondary_interpretation_digest: str | None
    relationship_payload_digest: str | None
    visual_payload_digest: str
    editorial_alignment_digest: str


@dataclass(frozen=True, slots=True)
class StatementDecision:
    statement_name: str
    statement_digest: str
    decision: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ContinuityDecision:
    character_id: str
    interpretation_digest: str
    decision: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RelationshipDecision:
    relationship_payload_digest: str
    decision: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VisualDecision:
    visual_payload_digest: str
    decision: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CandidateAdjudication:
    candidate_id: str
    authority_context_digest: str
    draft_digest: str
    statement_decisions: tuple[StatementDecision, ...]
    primary_continuity: ContinuityDecision
    secondary_continuity: ContinuityDecision | None
    relationship_continuity: RelationshipDecision | None
    visual_grounding: VisualDecision
    editorial_alignment: EditorialAlignmentDecision
    overall_decision: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EditorialAlignmentDecision:
    editorial_alignment_digest: str
    decision: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NarrativeAdjudicationBatch:
    candidates: tuple[CandidateAdjudication, ...]


@dataclass(frozen=True, slots=True)
class NarrativeCandidateResult:
    candidate_id: str
    rank: int
    accepted: bool
    reason_codes: tuple[str, ...]
    package: contract.HumanStoryPackage | None
    package_digest: str | None


@dataclass(frozen=True, slots=True)
class NarrativeGenerationResult:
    run_id: str
    candidates: tuple[NarrativeCandidateResult, ...]
    accepted_candidate_ids: tuple[str, ...]
    selected_candidate_id: str | None
    model_call_count: int
    generation_model: str
    adjudication_model: str
    reason_codes: tuple[str, ...] = ()


def generation_response_schema() -> dict[str, object]:
    """Full strict JSON schema; the local parser remains authoritative."""
    string = {"type": "string", "minLength": 1}
    optional_string = {"anyOf": [string, {"type": "null"}]}
    strings = {"type": "array", "items": string, "uniqueItems": True}

    def obj(properties: dict[str, object]) -> dict[str, object]:
        return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}

    statement = obj({"text": string, "inference_kind": {"enum": ["observed", "bounded_interpretation"]}, "source_fact_refs": strings, "editorial_refs": strings, "canon_refs": strings})
    interpretation = obj({
        "character_id": {"enum": ["naz", "void"]}, "text": string, "source_fact_refs": strings,
        "canon_refs": strings, "interpretation_mode": string, "thematic_axis": string,
        "emotional_register": string, "rhetorical_form": string, "narrative_distance": string,
        "humor_mode": string, "sarcasm_target": optional_string, "ending_mode": string,
        "continuity_basis": strings,
    })
    subject = obj({"subject_kind": {"enum": sorted(SUBJECT_KINDS)}, "character_id": optional_string, "source_fact_refs": strings, "identity_canon_refs": strings})
    visual = obj({
        "mode_hint": {"enum": sorted(VISUAL_MODES)}, "narrative_subject": string,
        "human_presence_policy": {"enum": ["none", "canonical_only", "source_grounded"]},
        "nonhuman_presence_policy": {"enum": ["none", "source_grounded"]},
        "approved_motifs": strings, "excluded_motifs": strings, "source_fact_refs": strings,
        "visual_canon_refs": strings, "subjects": {"type": "array", "items": subject},
    })
    candidate = obj({
        "candidate_id": string, "rank": {"type": "integer", "minimum": 1},
        "primary_character_id": {"enum": ["naz", "void"]}, "secondary_character_id": optional_string,
        "presence_mode": {"enum": sorted(PRESENCE_MODES)},
        **{name: statement for name in STORY_FIELDS},
        "primary_interpretation": interpretation,
        "secondary_interpretation": {"anyOf": [interpretation, {"type": "null"}]},
        "interaction_mode": optional_string, "relation_to_story": optional_string,
        "visual_direction": visual, "story_type": string,
    })
    root = obj({"schema": {"const": GENERATION_SCHEMA}, "candidates": {"type": "array", "items": candidate, "minItems": 3, "maxItems": 3}})
    return {"name": GENERATION_SCHEMA, "strict": True, "schema": root}


def adjudication_response_schema() -> dict[str, object]:
    string = {"type": "string", "minLength": 1}
    digest = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    decision = {"enum": ["supported", "rejected"]}
    strings = {"type": "array", "items": string, "uniqueItems": True}

    def obj(properties: dict[str, object]) -> dict[str, object]:
        return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}

    relationship = obj({"relationship_payload_digest": digest, "decision": decision, "reason_codes": strings})
    visual = obj({"visual_payload_digest": digest, "decision": decision, "reason_codes": strings})
    editorial = obj({"editorial_alignment_digest": digest, "decision": decision, "reason_codes": strings})
    continuity = obj({"character_id": {"enum": ["naz", "void"]}, "interpretation_digest": digest, "decision": decision, "reason_codes": strings})
    statement = obj({"statement_name": {"enum": list(STORY_FIELDS)}, "statement_digest": digest, "decision": decision, "reason_codes": strings})
    candidate = obj({
        "candidate_id": string, "authority_context_digest": digest, "draft_digest": digest,
        "statement_decisions": {"type": "array", "items": statement},
        "primary_continuity": continuity, "secondary_continuity": {"anyOf": [continuity, {"type": "null"}]},
        "relationship_continuity": {"anyOf": [relationship, {"type": "null"}]},
        "visual_grounding": visual, "editorial_alignment": editorial,
        "overall_decision": decision, "reason_codes": strings,
    })
    root = obj({"schema": {"const": ADJUDICATION_SCHEMA}, "candidates": {"type": "array", "items": candidate, "minItems": 3, "maxItems": 3}})
    return {"name": ADJUDICATION_SCHEMA, "strict": True, "schema": root}


def _exact(mapping: object, keys: set[str], field: str) -> dict[str, object]:
    if type(mapping) is not dict or set(mapping) != keys:
        raise NarrativeGenerationError(f"{field}_schema_invalid", repairable=True)
    return mapping


def _json_document(response: Mapping[str, object] | str) -> dict[str, object]:
    if type(response) is dict:
        return response
    if type(response) is not str:
        raise NarrativeGenerationError("model_response_type_invalid", repairable=True)
    text = response.strip()
    if not text or text.startswith("```"):
        raise NarrativeGenerationError("model_response_json_invalid", repairable=True)
    try:
        decoded = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise NarrativeGenerationError("model_response_json_invalid", repairable=True) from exc
    if type(decoded) is not dict:
        raise NarrativeGenerationError("model_response_schema_invalid", repairable=True)
    return decoded


def _json_strings(value: object, field: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    if type(value) is not list:
        raise NarrativeGenerationError(f"{field}_invalid", repairable=True)
    return _strings(value, field, allow_empty=allow_empty)


def _no_authority_injection(value: object) -> None:
    if type(value) is dict:
        for key in value:
            lowered = str(key).casefold()
            authority_like = any(token in lowered for token in ("authority", "revision", "snapshot", "digest", "hash", "rules_version", "policy_contract"))
            if key in FORBIDDEN_AUTHORITY_KEYS or authority_like:
                raise NarrativeGenerationError("model_authority_injection", repairable=False)
        for item in value.values():
            _no_authority_injection(item)
    elif type(value) is list:
        for item in value:
            _no_authority_injection(item)


def _known_refs(context: NarrativeGenerationInput) -> tuple[set[str], dict[str, set[str]], set[str]]:
    facts = {item.fact_id for item in context.source_facts}
    canon = {
        "naz": {item.source_id for item in context.naz_canon.canon_refs},
        "void": {item.source_id for item in context.void_canon.canon_refs},
    }
    return facts, canon, set(context.editorial_plan.editorial_ref_ids)


def _parse_statement(value: object, field: str, facts: set[str], canon: set[str], editorial: set[str]) -> DraftStatement:
    item = _exact(value, {"text", "inference_kind", "source_fact_refs", "editorial_refs", "canon_refs"}, field)
    statement = DraftStatement(
        _plain_str(item["text"], f"{field}_text"),
        _plain_str(item["inference_kind"], f"{field}_inference_kind"),
        _json_strings(item["source_fact_refs"], f"{field}_source_fact_refs", allow_empty=False),
        _json_strings(item["editorial_refs"], f"{field}_editorial_refs"),
        _json_strings(item["canon_refs"], f"{field}_canon_refs"),
    )
    if statement.inference_kind not in {"observed", "bounded_interpretation"}:
        raise NarrativeGenerationError("draft_inference_kind_invalid", repairable=True)
    if not set(statement.source_fact_refs) <= facts:
        raise NarrativeGenerationError("draft_source_fact_ref_unknown", repairable=False)
    if not set(statement.canon_refs) <= canon:
        raise NarrativeGenerationError("draft_canon_ref_unknown", repairable=False)
    if not set(statement.editorial_refs) <= editorial:
        raise NarrativeGenerationError("draft_editorial_ref_unknown", repairable=False)
    return statement


def _parse_interpretation(value: object, field: str, expected_character: str, facts: set[str], canon: dict[str, set[str]]) -> DraftCharacterInterpretation:
    keys = {"character_id", "text", "source_fact_refs", "canon_refs", "interpretation_mode", "thematic_axis", "emotional_register", "rhetorical_form", "narrative_distance", "humor_mode", "sarcasm_target", "ending_mode", "continuity_basis"}
    item = _exact(value, keys, field)
    character_id = _plain_str(item["character_id"], f"{field}_character_id")
    if character_id != expected_character:
        raise NarrativeGenerationError("draft_character_binding_invalid", repairable=False)
    result = DraftCharacterInterpretation(
        character_id=character_id,
        text=_plain_str(item["text"], f"{field}_text"),
        source_fact_refs=_json_strings(item["source_fact_refs"], f"{field}_source_fact_refs", allow_empty=False),
        canon_refs=_json_strings(item["canon_refs"], f"{field}_canon_refs", allow_empty=False),
        interpretation_mode=_plain_str(item["interpretation_mode"], f"{field}_interpretation_mode"),
        thematic_axis=_plain_str(item["thematic_axis"], f"{field}_thematic_axis"),
        emotional_register=_plain_str(item["emotional_register"], f"{field}_emotional_register"),
        rhetorical_form=_plain_str(item["rhetorical_form"], f"{field}_rhetorical_form"),
        narrative_distance=_plain_str(item["narrative_distance"], f"{field}_narrative_distance"),
        humor_mode=_plain_str(item["humor_mode"], f"{field}_humor_mode"),
        sarcasm_target=_optional_str(item["sarcasm_target"], f"{field}_sarcasm_target"),
        ending_mode=_plain_str(item["ending_mode"], f"{field}_ending_mode"),
        continuity_basis=_json_strings(item["continuity_basis"], f"{field}_continuity_basis", allow_empty=False),
    )
    if not set(result.source_fact_refs) <= facts:
        raise NarrativeGenerationError("draft_source_fact_ref_unknown", repairable=False)
    if not set(result.canon_refs) <= canon[character_id]:
        raise NarrativeGenerationError("draft_canon_ref_unknown", repairable=False)
    return result


def _parse_visual(value: object, facts: set[str], canon: dict[str, set[str]]) -> DraftVisualDirection:
    keys = {"mode_hint", "narrative_subject", "human_presence_policy", "nonhuman_presence_policy", "approved_motifs", "excluded_motifs", "source_fact_refs", "visual_canon_refs", "subjects"}
    item = _exact(value, keys, "visual_direction")
    if type(item["subjects"]) is not list:
        raise NarrativeGenerationError("visual_subjects_invalid", repairable=True)
    subjects: list[DraftVisualSubject] = []
    all_canon = set().union(*canon.values())
    for raw in item["subjects"]:
        subject = _exact(raw, {"subject_kind", "character_id", "source_fact_refs", "identity_canon_refs"}, "visual_subject")
        parsed = DraftVisualSubject(
            _plain_str(subject["subject_kind"], "subject_kind"),
            _optional_str(subject["character_id"], "subject_character_id"),
            _json_strings(subject["source_fact_refs"], "subject_source_fact_refs"),
            _json_strings(subject["identity_canon_refs"], "subject_identity_canon_refs"),
        )
        if parsed.subject_kind not in SUBJECT_KINDS:
            raise NarrativeGenerationError("visual_subject_kind_invalid", repairable=False)
        if not set(parsed.source_fact_refs) <= facts:
            raise NarrativeGenerationError("draft_source_fact_ref_unknown", repairable=False)
        if parsed.character_id is not None and parsed.character_id not in CHARACTER_IDS:
            raise NarrativeGenerationError("draft_character_binding_invalid", repairable=False)
        owned = canon.get(parsed.character_id or "", set())
        if not set(parsed.identity_canon_refs) <= owned:
            raise NarrativeGenerationError("draft_canon_ref_unknown", repairable=False)
        subjects.append(parsed)
    result = DraftVisualDirection(
        mode_hint=_plain_str(item["mode_hint"], "visual_mode_hint"),
        narrative_subject=_plain_str(item["narrative_subject"], "visual_narrative_subject"),
        human_presence_policy=_plain_str(item["human_presence_policy"], "human_presence_policy"),
        nonhuman_presence_policy=_plain_str(item["nonhuman_presence_policy"], "nonhuman_presence_policy"),
        approved_motifs=_json_strings(item["approved_motifs"], "approved_motifs"),
        excluded_motifs=_json_strings(item["excluded_motifs"], "excluded_motifs"),
        source_fact_refs=_json_strings(item["source_fact_refs"], "visual_source_fact_refs", allow_empty=False),
        visual_canon_refs=_json_strings(item["visual_canon_refs"], "visual_canon_refs", allow_empty=False),
        subjects=tuple(subjects),
    )
    if result.mode_hint not in VISUAL_MODES:
        raise NarrativeGenerationError("visual_mode_invalid", repairable=False)
    if not set(result.source_fact_refs) <= facts:
        raise NarrativeGenerationError("draft_source_fact_ref_unknown", repairable=False)
    if not set(result.visual_canon_refs) <= all_canon:
        raise NarrativeGenerationError("draft_canon_ref_unknown", repairable=False)
    return result


def validate_draft_against_generation_input(
    draft: NarrativeDraft,
    context: NarrativeGenerationInput,
) -> None:
    """Validate only structural compatibility with explicitly supplied inputs."""
    incompatible = False
    if context.relationship_state is None:
        incompatible = (
            draft.presence_mode != "none"
            or draft.secondary_character_id is not None
            or draft.secondary_interpretation is not None
            or draft.interaction_mode is not None
            or draft.relation_to_story is not None
        )

    visual = draft.visual_direction
    if visual.human_presence_policy not in {"none", "canonical_only", "source_grounded"}:
        incompatible = True
    if visual.nonhuman_presence_policy not in {"none", "source_grounded"}:
        incompatible = True
    for subject in visual.subjects:
        if subject.subject_kind in CHARACTER_IDS:
            incompatible = incompatible or (
                visual.human_presence_policy != "canonical_only"
                or subject.character_id != subject.subject_kind
                or not subject.identity_canon_refs
            )
        elif subject.subject_kind == "source_human":
            incompatible = incompatible or visual.human_presence_policy != "source_grounded" or not subject.source_fact_refs
        elif subject.subject_kind == "source_nonhuman_agent":
            incompatible = incompatible or visual.nonhuman_presence_policy != "source_grounded" or not subject.source_fact_refs
        elif subject.subject_kind == "object":
            incompatible = incompatible or subject.character_id is not None or bool(subject.identity_canon_refs)

    if incompatible:
        raise NarrativeGenerationError("generation_candidate_context_incompatible", repairable=True)


def parse_generation_response(response: Mapping[str, object] | str, context: NarrativeGenerationInput) -> tuple[NarrativeDraft, ...]:
    payload = _json_document(response)
    _no_authority_injection(payload)
    root = _exact(payload, {"schema", "candidates"}, "generation_response")
    if root["schema"] != GENERATION_SCHEMA or type(root["candidates"]) is not list or len(root["candidates"]) != 3:
        raise NarrativeGenerationError("generation_candidate_count_invalid", repairable=True)
    facts, canon, editorial = _known_refs(context)
    all_canon = set().union(*canon.values())
    drafts: list[NarrativeDraft] = []
    candidate_keys = {"candidate_id", "rank", "primary_character_id", "secondary_character_id", "presence_mode", *STORY_FIELDS, "primary_interpretation", "secondary_interpretation", "interaction_mode", "relation_to_story", "visual_direction", "story_type"}
    for raw in root["candidates"]:
        item = _exact(raw, candidate_keys, "candidate")
        primary = _plain_str(item["primary_character_id"], "primary_character_id")
        secondary = _optional_str(item["secondary_character_id"], "secondary_character_id")
        mode = _plain_str(item["presence_mode"], "presence_mode")
        if primary not in CHARACTER_IDS or secondary not in CHARACTER_IDS | {None} or mode not in PRESENCE_MODES:
            raise NarrativeGenerationError("draft_character_mode_invalid", repairable=False)
        if mode == "none" and secondary is not None or mode != "none" and (secondary is None or secondary == primary):
            raise NarrativeGenerationError("draft_character_mode_invalid", repairable=False)
        secondary_interp = None
        if secondary is not None:
            secondary_interp = _parse_interpretation(item["secondary_interpretation"], "secondary_interpretation", secondary, facts, canon)
        elif item["secondary_interpretation"] is not None:
            raise NarrativeGenerationError("draft_character_mode_invalid", repairable=False)
        draft = NarrativeDraft(
            candidate_id=_plain_str(item["candidate_id"], "candidate_id"),
            rank=_plain_int(item["rank"], "rank", minimum=1),
            primary_character_id=primary,
            secondary_character_id=secondary,
            presence_mode=mode,
            hook=_parse_statement(item["hook"], "hook", facts, all_canon, editorial),
            human_problem=_parse_statement(item["human_problem"], "human_problem", facts, all_canon, editorial),
            tension=_parse_statement(item["tension"], "tension", facts, all_canon, editorial),
            turning_point=_parse_statement(item["turning_point"], "turning_point", facts, all_canon, editorial),
            resolution=_parse_statement(item["resolution"], "resolution", facts, all_canon, editorial),
            primary_interpretation=_parse_interpretation(item["primary_interpretation"], "primary_interpretation", primary, facts, canon),
            secondary_interpretation=secondary_interp,
            interaction_mode=_optional_str(item["interaction_mode"], "interaction_mode"),
            relation_to_story=_optional_str(item["relation_to_story"], "relation_to_story"),
            visual_direction=_parse_visual(item["visual_direction"], facts, canon),
            story_type=_plain_str(item["story_type"], "story_type"),
        )
        if mode == "none" and (draft.interaction_mode is not None or draft.relation_to_story is not None):
            raise NarrativeGenerationError("draft_character_mode_invalid", repairable=False)
        if mode != "none" and (draft.interaction_mode is None or draft.relation_to_story is None):
            raise NarrativeGenerationError("draft_character_mode_invalid", repairable=False)
        drafts.append(draft)
    ids = [item.candidate_id for item in drafts]
    if len(ids) != len(set(ids)):
        raise NarrativeGenerationError("generation_candidate_id_duplicate", repairable=True)
    _ensure_draft_diversity(tuple(drafts))
    for draft in drafts:
        validate_draft_against_generation_input(draft, context)
    return tuple(drafts)


def _duo_source_fact_refs(draft: NarrativeDraft) -> tuple[str, ...]:
    return tuple(dict.fromkeys(ref for name in STORY_FIELDS for ref in getattr(draft, name).source_fact_refs))


def plan_binding_payload(context: NarrativeGenerationInput) -> dict[str, object]:
    return {
        "payload_version": AUTHORITY_CONTEXT_BINDING_VERSION,
        "editorial_plan": asdict(context.editorial_plan),
    }


def source_binding_payload(context: NarrativeGenerationInput) -> dict[str, object]:
    return {
        "payload_version": AUTHORITY_CONTEXT_BINDING_VERSION,
        "source_ref": context.source_ref,
        "ordered_facts": tuple((item.fact_id, item.text) for item in context.source_facts),
    }


def evidence_policy_binding_payload(context: NarrativeGenerationInput) -> dict[str, object]:
    relationship_enabled = context.relationship_state is not None
    return {
        "payload_version": AUTHORITY_CONTEXT_BINDING_VERSION,
        "generation_contract_version": GENERATION_CONTRACT_VERSION,
        "generation_schema_version": GENERATION_SCHEMA,
        "adjudication_schema_version": ADJUDICATION_SCHEMA,
        "draft_binding_version": DRAFT_BINDING_VERSION,
        "validation_contract_version": contract.VALIDATION_CONTRACT_VERSION,
        "semantic": (SEMANTIC_AUTHORITY, SEMANTIC_RULES),
        "characters": (
            ("naz", CHARACTER_AUTHORITY, CHARACTER_RULES),
            ("void", CHARACTER_AUTHORITY, CHARACTER_RULES),
        ),
        "relationship": (
            (RELATIONSHIP_AUTHORITY, RELATIONSHIP_RULES)
            if relationship_enabled else None
        ),
        "visual": (VISUAL_AUTHORITY, VISUAL_RULES),
    }


def build_authority_context_binding(context: NarrativeGenerationInput) -> NarrativeAuthorityContextBinding:
    components = {
        "binding_version": AUTHORITY_CONTEXT_BINDING_VERSION,
        "plan_binding_digest": contract._digest(plan_binding_payload(context)),
        "source_payload_digest": contract._digest(source_binding_payload(context)),
        "naz_state_digest": contract._digest(asdict(context.naz_state)),
        "void_state_digest": contract._digest(asdict(context.void_state)),
        "relationship_state_digest": (
            None if context.relationship_state is None else contract._digest(asdict(context.relationship_state))
        ),
        "naz_canon_digest": contract._digest(asdict(context.naz_canon)),
        "void_canon_digest": contract._digest(asdict(context.void_canon)),
        "naz_prompt_context_digest": contract._digest(asdict(context.naz_prompt_context)),
        "void_prompt_context_digest": contract._digest(asdict(context.void_prompt_context)),
        "relationship_prompt_context_digest": (
            None
            if context.relationship_prompt_context is None
            else contract._digest(asdict(context.relationship_prompt_context))
        ),
        "diversity_context_digest": contract._digest(asdict(context.diversity_context)),
        "evidence_policy_digest": contract._digest(evidence_policy_binding_payload(context)),
        "validation_contract_version": contract.VALIDATION_CONTRACT_VERSION,
    }
    return NarrativeAuthorityContextBinding(
        **components,
        authority_context_digest=contract._digest(components),
    )


def statement_binding_payload(draft: NarrativeDraft, statement_name: str) -> dict[str, object]:
    if statement_name not in STORY_FIELDS:
        raise ValueError("unknown statement name")
    item = getattr(draft, statement_name)
    return {
        "binding_version": DRAFT_BINDING_VERSION,
        "candidate_id": draft.candidate_id,
        "statement_name": statement_name,
        "text": item.text,
        "inference_kind": item.inference_kind,
        "source_fact_refs": item.source_fact_refs,
        "editorial_refs": item.editorial_refs,
        "canon_refs": item.canon_refs,
    }


def interpretation_binding_payload(draft: NarrativeDraft, item: DraftCharacterInterpretation) -> dict[str, object]:
    return {
        "binding_version": DRAFT_BINDING_VERSION,
        "candidate_id": draft.candidate_id,
        "character_id": item.character_id,
        "text": item.text,
        "source_fact_refs": item.source_fact_refs,
        "canon_refs": item.canon_refs,
        "interpretation_mode": item.interpretation_mode,
        "thematic_axis": item.thematic_axis,
        "emotional_register": item.emotional_register,
        "rhetorical_form": item.rhetorical_form,
        "narrative_distance": item.narrative_distance,
        "humor_mode": item.humor_mode,
        "sarcasm_target": item.sarcasm_target,
        "ending_mode": item.ending_mode,
        "continuity_basis": item.continuity_basis,
    }


def relationship_binding_payload(
    draft: NarrativeDraft,
    primary_digest: str,
    secondary_digest: str | None,
) -> dict[str, object] | None:
    if draft.secondary_character_id is None:
        return None
    return {
        "binding_version": DRAFT_BINDING_VERSION,
        "candidate_id": draft.candidate_id,
        "presence_mode": draft.presence_mode,
        "interaction_mode": draft.interaction_mode,
        "relation_to_story": draft.relation_to_story,
        "source_fact_refs": _duo_source_fact_refs(draft),
        "primary_character_id": draft.primary_character_id,
        "secondary_character_id": draft.secondary_character_id,
        "primary_interpretation_digest": primary_digest,
        "secondary_interpretation_digest": secondary_digest,
    }


def visual_binding_payload(draft: NarrativeDraft) -> dict[str, object]:
    item = draft.visual_direction
    return {
        "binding_version": DRAFT_BINDING_VERSION,
        "candidate_id": draft.candidate_id,
        "mode_hint": item.mode_hint,
        "narrative_subject": item.narrative_subject,
        "human_presence_policy": item.human_presence_policy,
        "nonhuman_presence_policy": item.nonhuman_presence_policy,
        "subjects": tuple(asdict(subject) for subject in item.subjects),
        "approved_motifs": item.approved_motifs,
        "excluded_motifs": item.excluded_motifs,
        "source_fact_refs": item.source_fact_refs,
        "visual_canon_refs": item.visual_canon_refs,
    }


def editorial_alignment_payload(draft: NarrativeDraft, context: NarrativeGenerationInput) -> dict[str, object]:
    plan = context.editorial_plan
    return {
        "binding_version": DRAFT_BINDING_VERSION,
        "candidate_id": draft.candidate_id,
        "plan_id": plan.plan_id,
        "source_ref": plan.source_ref,
        "plan": asdict(plan),
        "candidate_fields": {
            "visual_mode": draft.visual_direction.mode_hint,
            "primary_ending_mode": draft.primary_interpretation.ending_mode,
            "secondary_ending_mode": None if draft.secondary_interpretation is None else draft.secondary_interpretation.ending_mode,
            "presence_mode": draft.presence_mode,
            "story_type": draft.story_type,
        },
    }


def build_draft_bindings(
    draft: NarrativeDraft,
    context: NarrativeGenerationInput,
    authority_binding: NarrativeAuthorityContextBinding | None = None,
) -> NarrativeDraftBindings:
    authority = build_authority_context_binding(context) if authority_binding is None else authority_binding
    statements = tuple(
        StatementBinding(name, contract._digest(statement_binding_payload(draft, name)))
        for name in STORY_FIELDS
    )
    primary = contract._digest(interpretation_binding_payload(draft, draft.primary_interpretation))
    secondary = None if draft.secondary_interpretation is None else contract._digest(interpretation_binding_payload(draft, draft.secondary_interpretation))
    relationship_payload = relationship_binding_payload(draft, primary, secondary)
    relationship = None if relationship_payload is None else contract._digest(relationship_payload)
    visual = contract._digest(visual_binding_payload(draft))
    editorial = contract._digest(editorial_alignment_payload(draft, context))
    draft_digest = contract._digest({
        "binding_version": DRAFT_BINDING_VERSION,
        "candidate_id": draft.candidate_id,
        "authority_context_digest": authority.authority_context_digest,
        "rank": draft.rank,
        "primary_character_id": draft.primary_character_id,
        "secondary_character_id": draft.secondary_character_id,
        "story_type": draft.story_type,
        "statement_bindings": tuple(asdict(item) for item in statements),
        "primary_interpretation_digest": primary,
        "secondary_interpretation_digest": secondary,
        "relationship_payload_digest": relationship,
        "visual_payload_digest": visual,
        "editorial_alignment_digest": editorial,
    })
    return NarrativeDraftBindings(
        draft.candidate_id,
        authority.authority_context_digest,
        draft_digest,
        statements,
        primary,
        secondary,
        relationship,
        visual,
        editorial,
    )


def _norm(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).casefold()
    value = value.replace("—", "-").replace("–", "-").replace("−", "-")
    value = re.sub(r"\b(?:naz|void)\b", "<character>", value)
    value = re.sub(r"[^\w<>]+", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def _ensure_draft_diversity(drafts: tuple[NarrativeDraft, ...]) -> None:
    """Reject textual bundles at or above 0.94 deterministic similarity.

    This is deliberately a narrow near-duplicate check, not semantic equivalence.
    IDs, ranks, character IDs, presence and all style labels are excluded.
    """
    bundles = []
    for item in drafts:
        texts = [getattr(item, field).text for field in STORY_FIELDS]
        texts.append(item.primary_interpretation.text)
        if item.secondary_interpretation is not None:
            texts.append(item.secondary_interpretation.text)
        bundles.append(" | ".join(_norm(text) for text in texts))
    if len(set(bundles)) != 3:
        raise NarrativeGenerationError("generation_candidates_not_substantively_diverse", repairable=False)
    for left in range(3):
        for right in range(left + 1, 3):
            if SequenceMatcher(None, bundles[left], bundles[right]).ratio() >= 0.94:
                raise NarrativeGenerationError("generation_candidates_not_substantively_diverse", repairable=False)


def _reason_codes(value: object, field: str) -> tuple[str, ...]:
    result = _json_strings(value, field)
    if any(re.fullmatch(r"[a-z][a-z0-9_]{0,63}", item) is None or item not in ADJUDICATION_REASON_CODES for item in result):
        raise NarrativeGenerationError(f"{field}_invalid", repairable=True)
    return result


def _decision(value: object, reason_codes: tuple[str, ...], field: str) -> str:
    if type(value) is not str or value not in {"supported", "rejected"}:
        raise NarrativeGenerationError(f"{field}_invalid", repairable=False)
    if value == "supported" and reason_codes:
        raise NarrativeGenerationError(f"{field}_reason_conflict", repairable=False)
    if value == "rejected" and not reason_codes:
        raise NarrativeGenerationError(f"{field}_reason_missing", repairable=False)
    return value


def _digest_echo(value: object, field: str) -> str:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise NarrativeGenerationError(f"{field}_invalid", repairable=False)
    return value


def _parse_continuity(value: object, field: str) -> ContinuityDecision:
    item = _exact(value, {"character_id", "interpretation_digest", "decision", "reason_codes"}, field)
    reasons = _reason_codes(item["reason_codes"], f"{field}_reason_codes")
    return ContinuityDecision(
        _plain_str(item["character_id"], f"{field}_character_id"),
        _digest_echo(item["interpretation_digest"], f"{field}_interpretation_digest"),
        _decision(item["decision"], reasons, f"{field}_decision"),
        reasons,
    )


def _parse_bound_decision(value: object, field: str, digest_field: str, cls: type) -> object:
    item = _exact(value, {digest_field, "decision", "reason_codes"}, field)
    reasons = _reason_codes(item["reason_codes"], f"{field}_reason_codes")
    return cls(
        _digest_echo(item[digest_field], f"{field}_{digest_field}"),
        _decision(item["decision"], reasons, f"{field}_decision"),
        reasons,
    )


def parse_adjudication_response(
    response: Mapping[str, object] | str,
    drafts: tuple[NarrativeDraft, ...],
    bindings: tuple[NarrativeDraftBindings, ...],
) -> NarrativeAdjudicationBatch:
    payload = _json_document(response)
    root = _exact(payload, {"schema", "candidates"}, "adjudication_response")
    if root["schema"] != ADJUDICATION_SCHEMA or type(root["candidates"]) is not list:
        raise NarrativeGenerationError("adjudication_schema_invalid", repairable=True)
    by_id = {item.candidate_id: item for item in drafts}
    parsed: list[CandidateAdjudication] = []
    keys = {"candidate_id", "authority_context_digest", "draft_digest", "statement_decisions", "primary_continuity", "secondary_continuity", "relationship_continuity", "visual_grounding", "editorial_alignment", "overall_decision", "reason_codes"}
    for raw in root["candidates"]:
        item = _exact(raw, keys, "candidate_adjudication")
        candidate_id = _plain_str(item["candidate_id"], "adjudication_candidate_id")
        if candidate_id not in by_id:
            raise NarrativeGenerationError("adjudication_candidate_extra", repairable=False)
        if type(item["statement_decisions"]) is not list:
            raise NarrativeGenerationError("statement_decisions_invalid", repairable=True)
        statements: list[StatementDecision] = []
        for raw_decision in item["statement_decisions"]:
            decision = _exact(raw_decision, {"statement_name", "statement_digest", "decision", "reason_codes"}, "statement_decision")
            reasons = _reason_codes(decision["reason_codes"], "statement_reason_codes")
            statements.append(StatementDecision(
                _plain_str(decision["statement_name"], "statement_name"),
                _digest_echo(decision["statement_digest"], "statement_digest"),
                _decision(decision["decision"], reasons, "statement_decision"),
                reasons,
            ))
        primary = _parse_continuity(item["primary_continuity"], "primary_continuity")
        secondary = None if item["secondary_continuity"] is None else _parse_continuity(item["secondary_continuity"], "secondary_continuity")
        relationship = None if item["relationship_continuity"] is None else _parse_bound_decision(item["relationship_continuity"], "relationship_continuity", "relationship_payload_digest", RelationshipDecision)
        visual = _parse_bound_decision(item["visual_grounding"], "visual_grounding", "visual_payload_digest", VisualDecision)
        editorial = _parse_bound_decision(item["editorial_alignment"], "editorial_alignment", "editorial_alignment_digest", EditorialAlignmentDecision)
        reasons = _reason_codes(item["reason_codes"], "candidate_reason_codes")
        parsed.append(CandidateAdjudication(
            candidate_id,
            _digest_echo(item["authority_context_digest"], "authority_context_digest"),
            _digest_echo(item["draft_digest"], "draft_digest"),
            tuple(statements), primary,
            secondary, relationship, visual, editorial,
            _decision(item["overall_decision"], reasons, "overall_decision"), reasons,
        ))
    ids = [item.candidate_id for item in parsed]
    expected = set(by_id)
    if len(ids) != len(set(ids)):
        raise NarrativeGenerationError("adjudication_candidate_duplicate", repairable=False)
    if set(ids) != expected:
        raise NarrativeGenerationError("adjudication_candidate_missing", repairable=False)
    binding_by_id = {item.candidate_id: item for item in bindings}
    if set(binding_by_id) != expected:
        raise NarrativeGenerationError("adjudication_binding_set_invalid", repairable=False)
    for decision in parsed:
        _validate_adjudication_cardinality(decision, by_id[decision.candidate_id])
    return NarrativeAdjudicationBatch(tuple(parsed))


def _validate_adjudication_cardinality(item: CandidateAdjudication, draft: NarrativeDraft) -> None:
    expected_statements = {name for name in STORY_FIELDS if getattr(draft, name).inference_kind == "bounded_interpretation"}
    actual = [decision.statement_name for decision in item.statement_decisions]
    if len(actual) != len(set(actual)):
        raise NarrativeGenerationError("adjudication_statement_duplicate", repairable=False)
    if set(actual) != expected_statements:
        raise NarrativeGenerationError("adjudication_statement_cardinality_invalid", repairable=False)
    if item.primary_continuity.character_id != draft.primary_character_id:
        raise NarrativeGenerationError("adjudication_primary_continuity_invalid", repairable=False)
    if draft.secondary_character_id is None:
        if item.secondary_continuity is not None or item.relationship_continuity is not None:
            raise NarrativeGenerationError("adjudication_continuity_extra", repairable=False)
    else:
        if item.secondary_continuity is None or item.secondary_continuity.character_id != draft.secondary_character_id or item.relationship_continuity is None:
            raise NarrativeGenerationError("adjudication_continuity_missing", repairable=False)


def _prompt_context(context: NarrativeGenerationInput) -> dict[str, object]:
    plan = context.editorial_plan
    state_by_id = {"naz": context.naz_state, "void": context.void_state}
    return {
        "source_facts": [{"fact_id": item.fact_id, "text": item.text} for item in context.source_facts],
        "editorial_direction": {
            name: getattr(plan, name)
            for name in (
                "semantic_theme", "semantic_card", "facet", "author_role", "emotional_arc",
                "reader_relation", "structure", "hook", "ending", "energy", "seriousness",
                "tempo", "humor", "imagery", "visual_mode", "visual_subject_direction",
                "visual_relation",
            )
        },
        "editorial_ref_ids": list(plan.editorial_ref_ids),
        "characters": [
            {
                "character_id": item.character_id,
                "canon_ref_ids": list(item.canon_ref_ids),
                "prompt_context": item.prompt_text,
                "state_summary": {
                    "energy": state_by_id[item.character_id].energy,
                    "warmth": state_by_id[item.character_id].warmth,
                    "tension": state_by_id[item.character_id].tension,
                    "curiosity": state_by_id[item.character_id].curiosity,
                    "confidence": state_by_id[item.character_id].confidence,
                    "sociability": state_by_id[item.character_id].sociability,
                    "facet": state_by_id[item.character_id].facet,
                    "mood_label": state_by_id[item.character_id].mood_label,
                },
            }
            for item in (context.naz_prompt_context, context.void_prompt_context)
        ],
        "relationship_prompt_context": None if context.relationship_prompt_context is None else context.relationship_prompt_context.prompt_text,
        "relationship_state_summary": None if context.relationship_state is None else {
            "snapshot_ref": context.relationship_state.snapshot_ref,
            "revision": context.relationship_state.revision,
            "version": context.relationship_state.version,
            "trust": context.relationship_state.trust,
            "warmth": context.relationship_state.warmth,
            "friction": context.relationship_state.friction,
            "curiosity": context.relationship_state.curiosity,
            "respect": context.relationship_state.respect,
            "mode": context.relationship_state.mode,
        },
        "recent_diversity_signatures": [asdict(item) for item in context.diversity_context.recent_signatures],
    }


def build_generation_request(context: NarrativeGenerationInput, model: str) -> NarrativeModelRequest:
    system = (
        "Return JSON only. Create exactly three substantially different narrative drafts. "
        "Use only supplied fact, editorial, and canon reference IDs. Characters may be primary, "
        "secondary, absent, quiet, humorous, serious, aligned, or contrasting as the material warrants. "
        "Do not invent authority fields, IDs, revisions, hashes, evidence, policy names, or validation decisions. "
        "Each bounded interpretation must name supporting fact refs. Do not validate your own drafts."
    )
    user = _canonical({"task": "draft_three_human_story_candidates", "context": _prompt_context(context)})
    return NarrativeModelRequest("generation", _plain_str(model, "model"), system, user, generation_response_schema())


def build_adjudication_request(
    context: NarrativeGenerationInput,
    drafts: tuple[NarrativeDraft, ...],
    bindings: tuple[NarrativeDraftBindings, ...],
    model: str,
) -> NarrativeModelRequest:
    system = (
        "Return JSON only. Independently adjudicate every supplied candidate. Provide exactly one "
        "decision for every bounded story statement, one continuity decision for each used character, "
        "one relationship decision only for duo candidates, and one visual grounding decision. "
        "The only successful decision token is supported. Never create digests, revisions, authority refs, "
        "policy names, or new facts."
    )
    binding_by_id = {item.candidate_id: item for item in bindings}
    if set(binding_by_id) != {item.candidate_id for item in drafts}:
        raise NarrativeGenerationError("adjudication_binding_set_invalid")
    safe_drafts = [
        {"draft": asdict(item), "bindings": asdict(binding_by_id[item.candidate_id])}
        for item in drafts
    ]
    user = _canonical({"task": "adjudicate_narrative_candidates", "context": _prompt_context(context), "candidates": safe_drafts})
    return NarrativeModelRequest("adjudication", _plain_str(model, "model"), system, user, adjudication_response_schema())


def build_repair_request(
    original: NarrativeModelRequest,
    model: str,
    malformed_response: Mapping[str, object] | str,
    reason_codes: tuple[str, ...] = (),
) -> NarrativeModelRequest:
    system = (
        "Repair JSON structure or explicit-input compatibility only. Return one JSON object matching the "
        "supplied schema. Preserve substantive content and reference choices except where a supplied "
        "context-compatibility reason requires a structural field change. Do not solve semantic, grounding, "
        "continuity, editorial, or diversity problems."
    )
    user = _canonical({
        "task": "repair_generation_or_adjudication_structure",
        "repair_schema_version": REPAIR_SCHEMA_VERSION,
        "repair_rules_version": REPAIR_RULES_VERSION,
        "original_request_kind": original.request_kind,
        "reason_codes": reason_codes,
        "schema": original.response_schema,
        "malformed_response": malformed_response,
    })
    return NarrativeModelRequest("repair", _plain_str(model, "model"), system, user, original.response_schema)


def _state_for(context: NarrativeGenerationInput, character_id: str) -> contract.CharacterStateSnapshot:
    return context.naz_state if character_id == "naz" else context.void_state


def _canon_for(context: NarrativeGenerationInput, character_id: str) -> contract.CharacterCanonSnapshot:
    return context.naz_canon if character_id == "naz" else context.void_canon


def _statement(item: DraftStatement) -> contract.GroundedStatement:
    return contract.GroundedStatement(item.text, item.source_fact_refs, item.inference_kind, item.editorial_refs, item.canon_refs)


def _interpretation(item: DraftCharacterInterpretation, context: NarrativeGenerationInput, relation_ref: str | None) -> contract.CharacterInterpretation:
    state = _state_for(context, item.character_id)
    return contract.CharacterInterpretation(
        character_id=item.character_id,
        text=item.text,
        source_fact_refs=item.source_fact_refs,
        canon_refs=item.canon_refs,
        state_snapshot_ref=state.snapshot_ref,
        relationship_snapshot_ref=relation_ref,
        interpretation_mode=item.interpretation_mode,
        thematic_axis=item.thematic_axis,
        emotional_register=item.emotional_register,
        rhetorical_form=item.rhetorical_form,
        narrative_distance=item.narrative_distance,
        humor_mode=item.humor_mode,
        sarcasm_target=item.sarcasm_target,
        ending_mode=item.ending_mode,
        continuity_basis=item.continuity_basis,
    )


def _visual(item: DraftVisualDirection) -> contract.VisualDirection:
    return contract.VisualDirection(
        mode_hint=item.mode_hint,
        narrative_subject=item.narrative_subject,
        human_presence_policy=item.human_presence_policy,
        nonhuman_presence_policy=item.nonhuman_presence_policy,
        approved_motifs=item.approved_motifs,
        excluded_motifs=item.excluded_motifs,
        source_fact_refs=item.source_fact_refs,
        visual_canon_refs=item.visual_canon_refs,
        subjects=tuple(contract.VisualSubjectRef(x.subject_kind, x.character_id, x.source_fact_refs, x.identity_canon_refs) for x in item.subjects),
    )


def assemble_human_story_package(draft: NarrativeDraft, context: NarrativeGenerationInput) -> contract.HumanStoryPackage:
    relation = context.relationship_state if draft.presence_mode != "none" else None
    if draft.presence_mode != "none" and relation is None:
        raise NarrativeGenerationError("relationship_state_unavailable")
    relation_ref = None if relation is None else relation.snapshot_ref
    primary = _interpretation(draft.primary_interpretation, context, relation_ref)
    secondary = None if draft.secondary_interpretation is None else _interpretation(draft.secondary_interpretation, context, relation_ref)
    used = (draft.primary_character_id,) if draft.secondary_character_id is None else (draft.primary_character_id, draft.secondary_character_id)
    return contract.HumanStoryPackage(
        schema=contract.HUMAN_STORY_SCHEMA,
        plan_id=context.editorial_plan.plan_id,
        source_ref=context.source_ref,
        source_facts=context.source_facts,
        hook=_statement(draft.hook),
        human_problem=_statement(draft.human_problem),
        tension=_statement(draft.tension),
        turning_point=_statement(draft.turning_point),
        resolution=_statement(draft.resolution),
        primary_interpretation=primary,
        secondary_interpretation=secondary,
        character_states=tuple(_state_for(context, character) for character in used),
        character_canons=tuple(_canon_for(context, character) for character in used),
        relationship_state=relation,
        duo_context=contract.DuoNarrativeContext(draft.presence_mode, relation_ref, draft.interaction_mode, draft.relation_to_story, _duo_source_fact_refs(draft)),
        visual_direction=_visual(draft.visual_direction),
        story_type=draft.story_type,
        confidence=contract.ConfidenceAssessment("high", ()),
    )


def _all_supported(item: CandidateAdjudication, package: contract.HumanStoryPackage) -> tuple[str, ...]:
    errors: set[str] = set(item.reason_codes)
    if item.overall_decision != "supported":
        errors.add("adjudication_overall_unsupported")
    decisions = {decision.statement_name: decision for decision in item.statement_decisions}
    for name in STORY_FIELDS:
        statement = getattr(package, name)
        if statement.inference_kind != "bounded_interpretation":
            continue
        decision = decisions[name]
        if decision.decision != "supported":
            errors.add("adjudication_statement_unsupported")
        errors.update(decision.reason_codes)
    for decision in (item.primary_continuity, item.secondary_continuity):
        if decision is not None:
            if decision.decision != "supported":
                errors.add("adjudication_character_unsupported")
            errors.update(decision.reason_codes)
    if item.relationship_continuity is not None:
        if item.relationship_continuity.decision != "supported":
            errors.add("adjudication_relationship_unsupported")
        errors.update(item.relationship_continuity.reason_codes)
    if item.visual_grounding.decision != "supported":
        errors.add("adjudication_visual_unsupported")
    errors.update(item.visual_grounding.reason_codes)
    if item.editorial_alignment.decision != "supported":
        errors.add("generation_editorial_alignment_invalid")
    errors.update(item.editorial_alignment.reason_codes)
    return tuple(sorted(errors))


def _binding_errors(item: CandidateAdjudication, expected: NarrativeDraftBindings, draft: NarrativeDraft) -> tuple[str, ...]:
    invalid = (
        item.candidate_id != expected.candidate_id
        or item.authority_context_digest != expected.authority_context_digest
        or item.draft_digest != expected.draft_digest
    )
    bounded_names = {name for name in STORY_FIELDS if getattr(draft, name).inference_kind == "bounded_interpretation"}
    expected_statements = {
        binding.statement_name: binding.statement_digest
        for binding in expected.statement_bindings
        if binding.statement_name in bounded_names
    }
    actual_statements = {decision.statement_name: decision.statement_digest for decision in item.statement_decisions}
    invalid = invalid or actual_statements != expected_statements
    invalid = invalid or item.primary_continuity.interpretation_digest != expected.primary_interpretation_digest
    if expected.secondary_interpretation_digest is None:
        invalid = invalid or item.secondary_continuity is not None
    else:
        invalid = invalid or item.secondary_continuity is None or item.secondary_continuity.interpretation_digest != expected.secondary_interpretation_digest
    if expected.relationship_payload_digest is None:
        invalid = invalid or item.relationship_continuity is not None
    else:
        invalid = invalid or item.relationship_continuity is None or item.relationship_continuity.relationship_payload_digest != expected.relationship_payload_digest
    invalid = invalid or item.visual_grounding.visual_payload_digest != expected.visual_payload_digest
    invalid = invalid or item.editorial_alignment.editorial_alignment_digest != expected.editorial_alignment_digest
    return ("generation_adjudication_binding_invalid",) if invalid else ()


def _editorial_errors(draft: NarrativeDraft, context: NarrativeGenerationInput) -> tuple[str, ...]:
    plan = context.editorial_plan
    mismatch = draft.visual_direction.mode_hint != plan.visual_mode
    mismatch = mismatch or draft.primary_interpretation.ending_mode != plan.ending
    if draft.secondary_interpretation is not None:
        mismatch = mismatch or draft.secondary_interpretation.ending_mode != plan.ending
    return ("generation_editorial_locked_axis_mismatch",) if mismatch else ()


def build_validation_context(
    package: contract.HumanStoryPackage,
    context: NarrativeGenerationInput,
    adjudication: CandidateAdjudication,
) -> contract.HumanStoryValidationContext:
    plan = contract.EditorialPlanBinding(
        context.editorial_plan.plan_id,
        context.source_ref,
        context.editorial_plan.production_mode,
        context.editorial_plan.content_format,
    )
    used_interpretations = tuple(item for item in (package.primary_interpretation, package.secondary_interpretation) if item is not None)
    state_by_id = {item.character_id: item for item in package.character_states}
    statement_decisions = {item.statement_name: item for item in adjudication.statement_decisions}
    continuity_decisions = {adjudication.primary_continuity.character_id: adjudication.primary_continuity}
    if adjudication.secondary_continuity is not None:
        continuity_decisions[adjudication.secondary_continuity.character_id] = adjudication.secondary_continuity
    semantic: list[contract.SemanticGroundingEvidence] = []
    for name in STORY_FIELDS:
        statement = getattr(package, name)
        if statement.inference_kind == "bounded_interpretation":
            semantic.append(contract.SemanticGroundingEvidence(
                plan.plan_id, plan.source_ref, contract._statement_digest(statement, plan, SEMANTIC_RULES),
                statement.source_fact_refs, SEMANTIC_AUTHORITY, SEMANTIC_RULES, statement_decisions[name].decision,
            ))
    for interpretation in used_interpretations:
        state = state_by_id[interpretation.character_id]
        semantic.append(contract.SemanticGroundingEvidence(
            plan.plan_id, plan.source_ref, contract._interpretation_digest(interpretation, state, plan, SEMANTIC_RULES),
            interpretation.source_fact_refs, SEMANTIC_AUTHORITY, SEMANTIC_RULES, continuity_decisions[interpretation.character_id].decision,
        ))
    character_evidence = tuple(
        contract.CharacterContinuityEvidence(
            plan.plan_id, plan.source_ref, item.character_id, state_by_id[item.character_id].snapshot_ref,
            state_by_id[item.character_id].revision, item.relationship_snapshot_ref,
            contract._interpretation_digest(item, state_by_id[item.character_id], plan, SEMANTIC_RULES),
            CHARACTER_AUTHORITY, CHARACTER_RULES, continuity_decisions[item.character_id].decision,
        )
        for item in used_interpretations
    )
    relationship_authority = None
    relationship_evidence = None
    if package.relationship_state is not None and package.secondary_interpretation is not None:
        relation = package.relationship_state
        relationship_authority = contract.RelationshipSnapshotAuthority(relation.snapshot_ref, relation.revision, relation.version)
        relationship_evidence = contract.RelationshipContinuityEvidence(
            plan.plan_id, plan.source_ref, package.duo_context.presence_mode, package.duo_context.interaction_mode,
            package.duo_context.relation_to_story, package.primary_interpretation.character_id,
            package.secondary_interpretation.character_id, contract.character_interpretation_digest(package.primary_interpretation),
            contract.character_interpretation_digest(package.secondary_interpretation), relation.snapshot_ref, relation.revision,
            relation.version, package.duo_context.source_fact_refs,
            contract.relationship_continuity_digest(
                plan=plan, duo_context=package.duo_context, primary_interpretation=package.primary_interpretation,
                secondary_interpretation=package.secondary_interpretation, relationship_snapshot=relation,
                rules_version=RELATIONSHIP_RULES,
            ),
            RELATIONSHIP_AUTHORITY, RELATIONSHIP_RULES, adjudication.relationship_continuity.decision,
        )
    policy = contract.EvidenceAuthorityPolicy(
        contract.VALIDATION_CONTRACT_VERSION, SEMANTIC_AUTHORITY, SEMANTIC_RULES,
        tuple(contract.CharacterEvidencePolicy(item.character_id, CHARACTER_AUTHORITY, CHARACTER_RULES) for item in used_interpretations),
        RELATIONSHIP_AUTHORITY if relationship_evidence is not None else None,
        RELATIONSHIP_RULES if relationship_evidence is not None else None,
        VISUAL_AUTHORITY, VISUAL_RULES,
    )
    return contract.HumanStoryValidationContext(
        plan=plan,
        expected_source_facts=context.source_facts,
        character_snapshot_authorities=tuple(contract.CharacterSnapshotAuthority(state.character_id, state.snapshot_ref, state.revision, state.core_version) for state in package.character_states),
        relationship_snapshot_authority=relationship_authority,
        semantic_grounding_evidence=tuple(semantic),
        character_continuity_evidence=character_evidence,
        relationship_continuity_evidence=relationship_evidence,
        visual_grounding_evidence=contract.VisualGroundingEvidence(
            plan.plan_id, plan.source_ref, contract._visual_digest(package.visual_direction, plan, VISUAL_RULES),
            VISUAL_AUTHORITY, VISUAL_RULES, adjudication.visual_grounding.decision,
        ),
        authority_policy=policy,
        diversity_context=context.diversity_context,
    )


def build_fail_closed_validation_context(
    package: contract.HumanStoryPackage,
    context: NarrativeGenerationInput,
) -> contract.HumanStoryValidationContext:
    """Create a validator input with no positive evidence after binding failure."""
    plan = contract.EditorialPlanBinding(
        context.editorial_plan.plan_id, context.source_ref,
        context.editorial_plan.production_mode, context.editorial_plan.content_format,
    )
    interpretations = tuple(item for item in (package.primary_interpretation, package.secondary_interpretation) if item is not None)
    relationship_authority = None
    if package.relationship_state is not None:
        relationship_authority = contract.RelationshipSnapshotAuthority(
            package.relationship_state.snapshot_ref,
            package.relationship_state.revision,
            package.relationship_state.version,
        )
    return contract.HumanStoryValidationContext(
        plan=plan,
        expected_source_facts=context.source_facts,
        character_snapshot_authorities=tuple(
            contract.CharacterSnapshotAuthority(item.character_id, item.snapshot_ref, item.revision, item.core_version)
            for item in package.character_states
        ),
        relationship_snapshot_authority=relationship_authority,
        semantic_grounding_evidence=(),
        character_continuity_evidence=(),
        relationship_continuity_evidence=None,
        visual_grounding_evidence=contract.VisualGroundingEvidence(
            plan.plan_id, plan.source_ref, "0" * 64, VISUAL_AUTHORITY, VISUAL_RULES, "rejected",
        ),
        authority_policy=contract.EvidenceAuthorityPolicy(
            contract.VALIDATION_CONTRACT_VERSION, SEMANTIC_AUTHORITY, SEMANTIC_RULES,
            tuple(contract.CharacterEvidencePolicy(item.character_id, CHARACTER_AUTHORITY, CHARACTER_RULES) for item in interpretations),
            RELATIONSHIP_AUTHORITY if package.relationship_state is not None else None,
            RELATIONSHIP_RULES if package.relationship_state is not None else None,
            VISUAL_AUTHORITY, VISUAL_RULES,
        ),
        diversity_context=context.diversity_context,
    )


def _prompt_context_digest(value: CharacterPromptContext | RelationshipPromptContext | None) -> str | None:
    return None if value is None else contract._digest(asdict(value))


def _input_run_id(
    context: NarrativeGenerationInput,
    generation_model: str,
    adjudication_model: str,
    repair_model: str | None = None,
) -> str:
    safe = {
        "contract": GENERATION_CONTRACT_VERSION,
        "validation_contract_version": contract.VALIDATION_CONTRACT_VERSION,
        "generation_schema_version": GENERATION_SCHEMA,
        "adjudication_schema_version": ADJUDICATION_SCHEMA,
        "binding_version": DRAFT_BINDING_VERSION,
        "authority_context_binding_version": AUTHORITY_CONTEXT_BINDING_VERSION,
        "repair_schema_version": REPAIR_SCHEMA_VERSION,
        "repair_rules_version": REPAIR_RULES_VERSION,
        "source_ref": context.source_ref,
        "facts": tuple((item.fact_id, item.text) for item in context.source_facts),
        "plan": asdict(context.editorial_plan),
        "naz_state": asdict(context.naz_state),
        "void_state": asdict(context.void_state),
        "relationship_state": None if context.relationship_state is None else asdict(context.relationship_state),
        "naz_canon": asdict(context.naz_canon),
        "void_canon": asdict(context.void_canon),
        "naz_prompt_context_digest": _prompt_context_digest(context.naz_prompt_context),
        "void_prompt_context_digest": _prompt_context_digest(context.void_prompt_context),
        "relationship_prompt_context_digest": _prompt_context_digest(context.relationship_prompt_context),
        "diversity_context": asdict(context.diversity_context),
        "generation_model": generation_model,
        "adjudication_model": adjudication_model,
        "repair_model": repair_model,
        "rules_versions": (SEMANTIC_RULES, CHARACTER_RULES, RELATIONSHIP_RULES, VISUAL_RULES),
    }
    return contract._digest(safe)[:24]


class NarrativeGenerationService:
    """Orchestrate one draft call, one adjudication call, and at most one repair."""

    def __init__(self, client: NarrativeModelClient, *, generation_model: str, adjudication_model: str, repair_model: str | None = None):
        self._client = client
        self.generation_model = _plain_str(generation_model, "generation_model")
        self.adjudication_model = _plain_str(adjudication_model, "adjudication_model")
        self.configured_repair_model = None if repair_model is None else _plain_str(repair_model, "repair_model")
        self.repair_model = self.generation_model if repair_model is None else self.configured_repair_model

    def _call_parse(
        self,
        request: NarrativeModelRequest,
        parser: Callable[[Mapping[str, object] | str], object],
        calls: list[str],
        repair_used: list[bool],
    ) -> object:
        calls.append(request.request_kind)
        response = self._client.generate_json(request)
        try:
            return parser(response)
        except NarrativeGenerationError as error:
            if not error.repairable or repair_used[0]:
                raise
            repair_used[0] = True
            repair = build_repair_request(request, self.repair_model, response, error.reason_codes)
            calls.append("repair")
            return parser(self._client.generate_json(repair))

    def generate(self, context: NarrativeGenerationInput) -> NarrativeGenerationResult:
        if type(context) is not NarrativeGenerationInput:
            raise TypeError("context")
        calls: list[str] = []
        repair_used = [False]
        generation_request = build_generation_request(context, self.generation_model)
        drafts = self._call_parse(generation_request, lambda response: parse_generation_response(response, context), calls, repair_used)
        assert type(drafts) is tuple
        internal_assembly_failure = False
        try:
            packages = tuple(assemble_human_story_package(draft, context) for draft in drafts)
        except NarrativeGenerationError:
            raise
        except Exception:
            internal_assembly_failure = True
        if internal_assembly_failure:
            raise NarrativeGenerationError(
                "narrative_generation_internal_assembly_error",
                repairable=False,
            ) from None
        authority_binding = build_authority_context_binding(context)
        bindings = tuple(build_draft_bindings(draft, context, authority_binding) for draft in drafts)
        binding_by_id = {item.candidate_id: item for item in bindings}
        adjudication_request = build_adjudication_request(context, drafts, bindings, self.adjudication_model)
        adjudications = self._call_parse(adjudication_request, lambda response: parse_adjudication_response(response, drafts, bindings), calls, repair_used)
        assert type(adjudications) is NarrativeAdjudicationBatch
        by_id = {item.candidate_id: item for item in adjudications.candidates}
        results: list[NarrativeCandidateResult] = []
        for draft, assembled_package in zip(drafts, packages, strict=True):
            adjudication = by_id[draft.candidate_id]
            package: contract.HumanStoryPackage | None = None
            package_digest: str | None = None
            errors: set[str] = set()
            try:
                package = assembled_package
                binding_errors = _binding_errors(adjudication, binding_by_id[draft.candidate_id], draft)
                errors.update(binding_errors)
                errors.update(_editorial_errors(draft, context))
                errors.update(_all_supported(adjudication, package))
                if binding_errors:
                    validation_context = build_fail_closed_validation_context(package, context)
                else:
                    validation_context = build_validation_context(package, context, adjudication)
                try:
                    validated = contract.validate_human_story_package(package, validation_context)
                    package_digest = validated.package_digest
                except contract.HumanStoryValidationError as error:
                    errors.update(error.reason_codes)
            except NarrativeGenerationError as error:
                errors.update(error.reason_codes)
            accepted = not errors and package_digest is not None
            results.append(NarrativeCandidateResult(
                draft.candidate_id, draft.rank, accepted, tuple(sorted(errors)), package if accepted else None,
                package_digest if accepted else None,
            ))
        accepted_results = [item for item in results if item.accepted]
        accepted_results.sort(key=lambda item: (item.rank, item.package_digest or ""))
        selected = accepted_results[0].candidate_id if accepted_results else None
        top_reasons = () if selected is not None else ("narrative_generation_no_valid_candidate",)
        return NarrativeGenerationResult(
            run_id=_input_run_id(
                context,
                self.generation_model,
                self.adjudication_model,
                self.configured_repair_model,
            ),
            candidates=tuple(results),
            accepted_candidate_ids=tuple(item.candidate_id for item in accepted_results),
            selected_candidate_id=selected,
            model_call_count=len(calls),
            generation_model=self.generation_model,
            adjudication_model=self.adjudication_model,
            reason_codes=top_reasons,
        )


def safe_result_summary(result: NarrativeGenerationResult) -> dict[str, object]:
    """Return diagnostics that cannot expose prompts, raw replies, or state notes."""
    return {
        "run_id": result.run_id,
        "candidate_ids": [item.candidate_id for item in result.candidates],
        "accepted_candidate_ids": list(result.accepted_candidate_ids),
        "selected_candidate_id": result.selected_candidate_id,
        "candidate_reasons": {item.candidate_id: list(item.reason_codes) for item in result.candidates},
        "candidate_digests": {item.candidate_id: item.package_digest for item in result.candidates},
        "model_call_count": result.model_call_count,
        "generation_model": result.generation_model,
        "adjudication_model": result.adjudication_model,
        "reason_codes": list(result.reason_codes),
    }
