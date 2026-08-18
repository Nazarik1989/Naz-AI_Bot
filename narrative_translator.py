"""Pure explicit-context validation for the Narrative Translator MVP.

The validation context is supplied by the future integration layer.  This
module checks exact structural and digest binding against that context; it does
not authenticate the origin of the context, read persistence, or compute state
transitions.  It also does not perform general semantic entailment.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, is_dataclass
from difflib import SequenceMatcher
from typing import Any, Iterable, Sequence, TypeVar


HUMAN_STORY_SCHEMA = "human-story-package-v1"
STORYBOARD_BRIEF_SCHEMA = "storyboard-narrative-brief-v1"
VALIDATION_CONTRACT_VERSION = "narrative-translator-contract-v1"

REASON_CODES = frozenset({
    "human_story_schema_invalid", "human_story_plan_binding_invalid", "human_story_source_binding_invalid",
    "human_story_story_first_required", "source_fact_missing", "source_fact_identity_changed",
    "source_fact_duplicate", "source_fact_ref_unknown", "source_fact_text_changed", "observed_claim_unsupported",
    "character_state_snapshot_missing", "character_state_snapshot_duplicate", "character_state_snapshot_extra",
    "character_state_snapshot_invalid", "character_state_snapshot_stale", "character_state_binding_invalid",
    "relationship_state_snapshot_missing", "relationship_state_snapshot_invalid", "relationship_state_snapshot_stale",
    "relationship_state_binding_invalid", "character_authority_duplicate", "character_authority_extra",
    "character_authority_missing", "character_policy_duplicate", "character_policy_extra", "character_policy_missing",
    "semantic_evidence_duplicate", "semantic_evidence_extra", "semantic_evidence_missing", "semantic_evidence_conflict",
    "character_evidence_duplicate", "character_evidence_extra", "character_evidence_missing", "character_evidence_conflict",
    "relationship_authority_unexpected", "relationship_evidence_unexpected", "relationship_evidence_missing",
    "relationship_evidence_conflict", "relationship_continuity_evidence_invalid", "visual_evidence_missing", "visual_evidence_conflict",
    "visual_grounding_evidence_invalid", "nonhuman_presence_policy_invalid", "source_nonhuman_agent_policy_required",
    "character_canon_missing", "character_canon_duplicate", "character_canon_extra", "character_canon_conflict",
    "character_canon_binding_invalid", "canon_source_id_duplicate", "character_personality_canon_missing", "character_relationship_canon_missing",
    "visual_canon_missing", "visual_canon_conflict", "visual_direction_unsupported", "generic_human_visual",
    "generic_robot_visual", "visual_motif_duplicate", "visual_motif_conflict", "visual_subject_duplicate",
    "visual_subject_fact_ref_duplicate", "snapshot_scalar_type_invalid", "snapshot_revision_invalid",
    "snapshot_axis_invalid", "authority_revision_invalid", "character_interpretation_too_short",
    "character_interpretations_collapsed", "forced_dual_interpretation_formula", "narrative_structure_repeated",
    "narrative_text_too_similar", "narrative_hook_too_similar", "narrative_ending_too_similar",
    "confidence_insufficient", "storyboard_fact_loss", "storyboard_fact_changed", "storyboard_scope_violation",
})
_STATE_AXES = ("energy", "warmth", "tension", "curiosity", "confidence", "sociability")
_REL_AXES = ("trust", "warmth", "friction", "curiosity", "respect")
_STORY_FIELDS = ("hook", "human_problem", "tension", "turning_point", "resolution")
_CANON_KINDS = frozenset({"personality", "visual", "world", "relationship"})
_SUBJECT_KINDS = frozenset({"object", "naz", "void", "source_human", "source_nonhuman_agent", "generic_human", "generic_robot"})
T = TypeVar("T")


def _require_plain_str(value: object, *, field_name: str, allow_empty: bool = False) -> str:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be an exact str")
    if not allow_empty and not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    return value


def _require_optional_plain_str(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_plain_str(value, field_name=field_name)


def _require_plain_int(
    value: object,
    *,
    field_name: str,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if type(value) is not int:
        raise TypeError(f"{field_name} must be an exact int")
    if minimum is not None and value < minimum:
        raise ValueError(f"{field_name} is below minimum")
    if maximum is not None and value > maximum:
        raise ValueError(f"{field_name} is above maximum")
    return value


def _tuple_of_plain_strings(value: Any, field: str) -> tuple[str, ...]:
    if type(value) not in (list, tuple):
        raise TypeError(f"{field} must be an exact list or tuple")
    result = tuple(value)
    for item in result:
        _require_plain_str(item, field_name=f"{field} item")
    return result


def _optional_tuple_of_plain_strings(value: Any, field: str) -> tuple[str, ...]:
    return () if value is None else _tuple_of_plain_strings(value, field)


def _tuple_of_dataclasses(value: Any, cls: type[T], field: str) -> tuple[T, ...]:
    if type(value) not in (list, tuple):
        raise TypeError(f"{field} must be an exact list or tuple")
    result = tuple(value)
    if any(type(item) is not cls for item in result):
        raise TypeError(f"{field} items must be {cls.__name__}")
    return result


def _text(value: Any) -> str:
    return " ".join(value.split()) if type(value) is str else ""


def _norm(value: Any) -> str:
    return _text(value).casefold()


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def canonical_payload(value: Any) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_payload(value).encode("utf-8")).hexdigest()


def _digest_format(value: Any) -> bool:
    return type(value) is str and bool(re.fullmatch(r"[0-9a-f]{64}", value))


class HumanStoryValidationError(ValueError):
    def __init__(self, errors: Iterable[str]):
        self.reason_codes = tuple(sorted(set(code for code in errors if code in REASON_CODES))) or ("human_story_schema_invalid",)
        super().__init__(",".join(self.reason_codes))


@dataclass(frozen=True, slots=True)
class EditorialPlanBinding:
    plan_id: str
    source_ref: str
    production_mode: str
    content_format: str

    def __post_init__(self) -> None:
        for name in ("plan_id", "source_ref", "production_mode", "content_format"):
            _require_plain_str(getattr(self, name), field_name=name)


@dataclass(frozen=True, slots=True)
class CanonSourceRef:
    character_id: str
    source_id: str
    source_path: str
    source_version: str
    source_hash: str
    kind: str

    def __post_init__(self) -> None:
        for name in ("character_id", "source_id", "source_path", "source_version", "source_hash", "kind"):
            _require_plain_str(getattr(self, name), field_name=name)


@dataclass(frozen=True, slots=True)
class CharacterCanonSnapshot:
    character_id: str
    canon_refs: tuple[CanonSourceRef, ...]
    snapshot_ref: str
    conflict_reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_plain_str(self.character_id, field_name="character_id")
        _require_plain_str(self.snapshot_ref, field_name="snapshot_ref")
        object.__setattr__(self, "canon_refs", _tuple_of_dataclasses(self.canon_refs, CanonSourceRef, "canon_refs"))
        object.__setattr__(self, "conflict_reason_codes", _tuple_of_plain_strings(self.conflict_reason_codes, "conflict_reason_codes"))


@dataclass(frozen=True, slots=True)
class CharacterStateSnapshot:
    character_id: str; core_version: str; revision: int; energy: int; warmth: int; tension: int; curiosity: int; confidence: int; sociability: int; facet: str; previous_facet: str; mood_label: str; last_event: str; recent_events: tuple[str, ...]; snapshot_ref: str
    def __post_init__(self) -> None:
        for name in ("character_id", "core_version", "facet", "previous_facet", "mood_label", "last_event", "snapshot_ref"):
            _require_plain_str(getattr(self, name), field_name=name)
        _require_plain_int(self.revision, field_name="revision", minimum=0)
        for name in _STATE_AXES:
            _require_plain_int(getattr(self, name), field_name=name, minimum=0, maximum=100)
        object.__setattr__(self, "recent_events", _tuple_of_plain_strings(self.recent_events, "recent_events"))


@dataclass(frozen=True, slots=True)
class RelationshipStateSnapshot:
    version: str; revision: int; trust: int; warmth: int; friction: int; curiosity: int; respect: int; mode: str; last_topic: str; unresolved_topics: tuple[str, ...]; inside_jokes: tuple[str, ...]; changed_minds: tuple[str, ...]; snapshot_ref: str
    def __post_init__(self) -> None:
        for name in ("version", "mode", "last_topic", "snapshot_ref"):
            _require_plain_str(getattr(self, name), field_name=name)
        _require_plain_int(self.revision, field_name="revision", minimum=0)
        for name in _REL_AXES:
            _require_plain_int(getattr(self, name), field_name=name, minimum=0, maximum=100)
        for name in ("unresolved_topics", "inside_jokes", "changed_minds"):
            object.__setattr__(self, name, _tuple_of_plain_strings(getattr(self, name), name))


@dataclass(frozen=True, slots=True)
class CharacterSnapshotAuthority:
    character_id: str; expected_snapshot_ref: str; expected_revision: int; expected_core_version: str

    def __post_init__(self) -> None:
        for name in ("character_id", "expected_snapshot_ref", "expected_core_version"):
            _require_plain_str(getattr(self, name), field_name=name)
        _require_plain_int(self.expected_revision, field_name="expected_revision", minimum=0)


@dataclass(frozen=True, slots=True)
class RelationshipSnapshotAuthority:
    expected_snapshot_ref: str; expected_revision: int; expected_version: str

    def __post_init__(self) -> None:
        _require_plain_str(self.expected_snapshot_ref, field_name="expected_snapshot_ref")
        _require_plain_str(self.expected_version, field_name="expected_version")
        _require_plain_int(self.expected_revision, field_name="expected_revision", minimum=0)


@dataclass(frozen=True, slots=True)
class SourceFact:
    fact_id: str; text: str

    def __post_init__(self) -> None:
        _require_plain_str(self.fact_id, field_name="fact_id")
        _require_plain_str(self.text, field_name="text")


@dataclass(frozen=True, slots=True)
class GroundedStatement:
    text: str; source_fact_refs: tuple[str, ...]; inference_kind: str; editorial_refs: tuple[str, ...] = (); canon_refs: tuple[str, ...] = ()
    def __post_init__(self) -> None:
        _require_plain_str(self.text, field_name="text")
        _require_plain_str(self.inference_kind, field_name="inference_kind")
        for name in ("source_fact_refs", "editorial_refs", "canon_refs"):
            object.__setattr__(self, name, _tuple_of_plain_strings(getattr(self, name), name))


@dataclass(frozen=True, slots=True)
class CharacterInterpretation:
    character_id: str; text: str; source_fact_refs: tuple[str, ...]; canon_refs: tuple[str, ...]; state_snapshot_ref: str; relationship_snapshot_ref: str | None; interpretation_mode: str; thematic_axis: str; emotional_register: str; rhetorical_form: str; narrative_distance: str; humor_mode: str; sarcasm_target: str | None; ending_mode: str; continuity_basis: tuple[str, ...]
    def __post_init__(self) -> None:
        for name in ("character_id", "text", "state_snapshot_ref", "interpretation_mode", "thematic_axis", "emotional_register", "rhetorical_form", "narrative_distance", "humor_mode", "ending_mode"):
            _require_plain_str(getattr(self, name), field_name=name)
        _require_optional_plain_str(self.relationship_snapshot_ref, field_name="relationship_snapshot_ref")
        _require_optional_plain_str(self.sarcasm_target, field_name="sarcasm_target")
        for name in ("source_fact_refs", "canon_refs", "continuity_basis"):
            object.__setattr__(self, name, _tuple_of_plain_strings(getattr(self, name), name))


@dataclass(frozen=True, slots=True)
class DuoNarrativeContext:
    presence_mode: str; relationship_snapshot_ref: str | None; interaction_mode: str | None; relation_to_story: str | None; source_fact_refs: tuple[str, ...]
    def __post_init__(self) -> None:
        _require_plain_str(self.presence_mode, field_name="presence_mode")
        _require_optional_plain_str(self.relationship_snapshot_ref, field_name="relationship_snapshot_ref")
        _require_optional_plain_str(self.interaction_mode, field_name="interaction_mode")
        _require_optional_plain_str(self.relation_to_story, field_name="relation_to_story")
        object.__setattr__(self, "source_fact_refs", _tuple_of_plain_strings(self.source_fact_refs, "duo_source_fact_refs"))


@dataclass(frozen=True, slots=True)
class VisualSubjectRef:
    subject_kind: str; character_id: str | None; source_fact_refs: tuple[str, ...]; identity_canon_refs: tuple[str, ...]
    def __post_init__(self) -> None:
        _require_plain_str(self.subject_kind, field_name="subject_kind")
        _require_optional_plain_str(self.character_id, field_name="character_id")
        object.__setattr__(self, "source_fact_refs", _tuple_of_plain_strings(self.source_fact_refs, "subject_fact_refs"))
        object.__setattr__(self, "identity_canon_refs", _tuple_of_plain_strings(self.identity_canon_refs, "identity_canon_refs"))


@dataclass(frozen=True, slots=True)
class VisualDirection:
    mode_hint: str; narrative_subject: str; human_presence_policy: str; nonhuman_presence_policy: str; approved_motifs: tuple[str, ...]; excluded_motifs: tuple[str, ...]; source_fact_refs: tuple[str, ...]; visual_canon_refs: tuple[str, ...]; subjects: tuple[VisualSubjectRef, ...]
    def __post_init__(self) -> None:
        for name in ("mode_hint", "narrative_subject", "human_presence_policy", "nonhuman_presence_policy"):
            _require_plain_str(getattr(self, name), field_name=name)
        for name in ("approved_motifs", "excluded_motifs", "source_fact_refs", "visual_canon_refs"):
            object.__setattr__(self, name, _tuple_of_plain_strings(getattr(self, name), name))
        object.__setattr__(self, "subjects", _tuple_of_dataclasses(self.subjects, VisualSubjectRef, "subjects"))


@dataclass(frozen=True, slots=True)
class CharacterEvidencePolicy:
    character_id: str; authority_ref: str; rules_version: str

    def __post_init__(self) -> None:
        for name in ("character_id", "authority_ref", "rules_version"):
            _require_plain_str(getattr(self, name), field_name=name)


@dataclass(frozen=True, slots=True)
class EvidenceAuthorityPolicy:
    policy_contract_version: str; semantic_authority_ref: str; semantic_rules_version: str; character_policies: tuple[CharacterEvidencePolicy, ...]; relationship_authority_ref: str | None; relationship_rules_version: str | None; visual_authority_ref: str; visual_rules_version: str
    def __post_init__(self) -> None:
        for name in ("policy_contract_version", "semantic_authority_ref", "semantic_rules_version", "visual_authority_ref", "visual_rules_version"):
            _require_plain_str(getattr(self, name), field_name=name)
        _require_optional_plain_str(self.relationship_authority_ref, field_name="relationship_authority_ref")
        _require_optional_plain_str(self.relationship_rules_version, field_name="relationship_rules_version")
        object.__setattr__(self, "character_policies", _tuple_of_dataclasses(self.character_policies, CharacterEvidencePolicy, "character_policies"))


@dataclass(frozen=True, slots=True)
class SemanticGroundingEvidence:
    plan_id: str; source_ref: str; statement_digest: str; source_fact_refs: tuple[str, ...]; authority_ref: str; rules_version: str; decision: str
    def __post_init__(self) -> None:
        for name in ("plan_id", "source_ref", "statement_digest", "authority_ref", "rules_version", "decision"):
            _require_plain_str(getattr(self, name), field_name=name)
        object.__setattr__(self, "source_fact_refs", _tuple_of_plain_strings(self.source_fact_refs, "semantic_fact_refs"))


@dataclass(frozen=True, slots=True)
class CharacterContinuityEvidence:
    plan_id: str; source_ref: str; character_id: str; state_snapshot_ref: str; state_revision: int; relationship_snapshot_ref: str | None; interpretation_digest: str; authority_ref: str; rules_version: str; decision: str

    def __post_init__(self) -> None:
        for name in ("plan_id", "source_ref", "character_id", "state_snapshot_ref", "interpretation_digest", "authority_ref", "rules_version", "decision"):
            _require_plain_str(getattr(self, name), field_name=name)
        _require_plain_int(self.state_revision, field_name="state_revision", minimum=0)
        _require_optional_plain_str(self.relationship_snapshot_ref, field_name="relationship_snapshot_ref")


@dataclass(frozen=True, slots=True)
class RelationshipContinuityEvidence:
    plan_id: str; source_ref: str; presence_mode: str; interaction_mode: str | None; relation_to_story: str | None; primary_character_id: str; secondary_character_id: str; primary_interpretation_digest: str; secondary_interpretation_digest: str; relationship_snapshot_ref: str; relationship_revision: int; relationship_version: str; source_fact_refs: tuple[str, ...]; duo_context_digest: str; authority_ref: str; rules_version: str; decision: str
    def __post_init__(self) -> None:
        for name in ("plan_id", "source_ref", "presence_mode", "primary_character_id", "secondary_character_id", "primary_interpretation_digest", "secondary_interpretation_digest", "relationship_snapshot_ref", "relationship_version", "duo_context_digest", "authority_ref", "rules_version", "decision"):
            _require_plain_str(getattr(self, name), field_name=name)
        _require_optional_plain_str(self.interaction_mode, field_name="interaction_mode")
        _require_optional_plain_str(self.relation_to_story, field_name="relation_to_story")
        _require_plain_int(self.relationship_revision, field_name="relationship_revision", minimum=0)
        object.__setattr__(self, "source_fact_refs", _tuple_of_plain_strings(self.source_fact_refs, "relationship_fact_refs"))


@dataclass(frozen=True, slots=True)
class VisualGroundingEvidence:
    plan_id: str; source_ref: str; visual_digest: str; authority_ref: str; rules_version: str; decision: str

    def __post_init__(self) -> None:
        for name in ("plan_id", "source_ref", "visual_digest", "authority_ref", "rules_version", "decision"):
            _require_plain_str(getattr(self, name), field_name=name)


@dataclass(frozen=True, slots=True)
class NarrativeDiversitySignature:
    primary_character_id: str; secondary_character_id: str | None; presence_mode: str; hook_fingerprint: str; human_problem_fingerprint: str; tension_fingerprint: str; turning_point_fingerprint: str; resolution_fingerprint: str; primary_interpretation_fingerprint: str; secondary_interpretation_fingerprint: str | None; visual_mode: str; ending_mode: str

    def __post_init__(self) -> None:
        for name in ("primary_character_id", "presence_mode", "hook_fingerprint", "human_problem_fingerprint", "tension_fingerprint", "turning_point_fingerprint", "resolution_fingerprint", "primary_interpretation_fingerprint", "visual_mode", "ending_mode"):
            _require_plain_str(getattr(self, name), field_name=name)
        _require_optional_plain_str(self.secondary_character_id, field_name="secondary_character_id")
        _require_optional_plain_str(self.secondary_interpretation_fingerprint, field_name="secondary_interpretation_fingerprint")


@dataclass(frozen=True, slots=True)
class NarrativeDiversityContext:
    recent_signatures: tuple[NarrativeDiversitySignature, ...]
    def __post_init__(self) -> None: object.__setattr__(self, "recent_signatures", _tuple_of_dataclasses(self.recent_signatures, NarrativeDiversitySignature, "recent_signatures"))


@dataclass(frozen=True, slots=True)
class ConfidenceAssessment:
    level: str; reason_codes: tuple[str, ...]
    def __post_init__(self) -> None:
        _require_plain_str(self.level, field_name="confidence_level")
        object.__setattr__(self, "reason_codes", _tuple_of_plain_strings(self.reason_codes, "confidence_reason_codes"))


@dataclass(frozen=True, slots=True)
class HumanStoryPackage:
    schema: str; plan_id: str; source_ref: str; source_facts: tuple[SourceFact, ...]; hook: GroundedStatement; human_problem: GroundedStatement; tension: GroundedStatement; turning_point: GroundedStatement; resolution: GroundedStatement; primary_interpretation: CharacterInterpretation; secondary_interpretation: CharacterInterpretation | None; character_states: tuple[CharacterStateSnapshot, ...]; character_canons: tuple[CharacterCanonSnapshot, ...]; relationship_state: RelationshipStateSnapshot | None; duo_context: DuoNarrativeContext; visual_direction: VisualDirection; story_type: str; confidence: ConfidenceAssessment
    def __post_init__(self) -> None:
        for name in ("schema", "plan_id", "source_ref", "story_type"):
            _require_plain_str(getattr(self, name), field_name=name)
        object.__setattr__(self, "source_facts", _tuple_of_dataclasses(self.source_facts, SourceFact, "source_facts"))
        object.__setattr__(self, "character_states", _tuple_of_dataclasses(self.character_states, CharacterStateSnapshot, "character_states"))
        object.__setattr__(self, "character_canons", _tuple_of_dataclasses(self.character_canons, CharacterCanonSnapshot, "character_canons"))
        for name, cls in (("hook", GroundedStatement), ("human_problem", GroundedStatement), ("tension", GroundedStatement), ("turning_point", GroundedStatement), ("resolution", GroundedStatement), ("primary_interpretation", CharacterInterpretation), ("duo_context", DuoNarrativeContext), ("visual_direction", VisualDirection), ("confidence", ConfidenceAssessment)):
            if type(getattr(self, name)) is not cls:
                raise TypeError(name)
        if self.secondary_interpretation is not None and type(self.secondary_interpretation) is not CharacterInterpretation: raise TypeError("secondary_interpretation")
        if self.relationship_state is not None and type(self.relationship_state) is not RelationshipStateSnapshot: raise TypeError("relationship_state")


@dataclass(frozen=True, slots=True)
class HumanStoryValidationContext:
    plan: EditorialPlanBinding; expected_source_facts: tuple[SourceFact, ...]; character_snapshot_authorities: tuple[CharacterSnapshotAuthority, ...]; relationship_snapshot_authority: RelationshipSnapshotAuthority | None; semantic_grounding_evidence: tuple[SemanticGroundingEvidence, ...]; character_continuity_evidence: tuple[CharacterContinuityEvidence, ...]; relationship_continuity_evidence: RelationshipContinuityEvidence | None; visual_grounding_evidence: VisualGroundingEvidence; authority_policy: EvidenceAuthorityPolicy; diversity_context: NarrativeDiversityContext
    def __post_init__(self) -> None:
        if type(self.plan) is not EditorialPlanBinding: raise TypeError("plan")
        object.__setattr__(self, "expected_source_facts", _tuple_of_dataclasses(self.expected_source_facts, SourceFact, "expected_source_facts"))
        object.__setattr__(self, "character_snapshot_authorities", _tuple_of_dataclasses(self.character_snapshot_authorities, CharacterSnapshotAuthority, "character_snapshot_authorities"))
        object.__setattr__(self, "semantic_grounding_evidence", _tuple_of_dataclasses(self.semantic_grounding_evidence, SemanticGroundingEvidence, "semantic_grounding_evidence"))
        object.__setattr__(self, "character_continuity_evidence", _tuple_of_dataclasses(self.character_continuity_evidence, CharacterContinuityEvidence, "character_continuity_evidence"))
        if self.relationship_snapshot_authority is not None and type(self.relationship_snapshot_authority) is not RelationshipSnapshotAuthority: raise TypeError("relationship_snapshot_authority")
        if self.relationship_continuity_evidence is not None and type(self.relationship_continuity_evidence) is not RelationshipContinuityEvidence: raise TypeError("relationship_continuity_evidence")
        if type(self.visual_grounding_evidence) is not VisualGroundingEvidence: raise TypeError("visual_grounding_evidence")
        if type(self.authority_policy) is not EvidenceAuthorityPolicy: raise TypeError("authority_policy")
        if type(self.diversity_context) is not NarrativeDiversityContext: raise TypeError("diversity_context")


@dataclass(frozen=True, slots=True)
class ValidatedHumanStoryPackage:
    package: HumanStoryPackage; package_digest: str; derived_diversity_signature: NarrativeDiversitySignature; validation_contract_version: str

    def __post_init__(self) -> None:
        if type(self.package) is not HumanStoryPackage: raise TypeError("package")
        if type(self.derived_diversity_signature) is not NarrativeDiversitySignature: raise TypeError("derived_diversity_signature")
        _require_plain_str(self.package_digest, field_name="package_digest")
        _require_plain_str(self.validation_contract_version, field_name="validation_contract_version")


@dataclass(frozen=True, slots=True)
class CharacterStateReceiptRef:
    character_id: str; snapshot_ref: str; revision: int; core_version: str

    def __post_init__(self) -> None:
        for name in ("character_id", "snapshot_ref", "core_version"):
            _require_plain_str(getattr(self, name), field_name=name)
        _require_plain_int(self.revision, field_name="revision", minimum=0)


@dataclass(frozen=True, slots=True)
class RelationshipStateReceiptRef:
    snapshot_ref: str; revision: int; version: str

    def __post_init__(self) -> None:
        _require_plain_str(self.snapshot_ref, field_name="snapshot_ref")
        _require_plain_str(self.version, field_name="version")
        _require_plain_int(self.revision, field_name="revision", minimum=0)


@dataclass(frozen=True, slots=True)
class CanonReceiptRef:
    character_id: str; source_id: str; source_version: str; source_hash: str; kind: str

    def __post_init__(self) -> None:
        for name in ("character_id", "source_id", "source_version", "source_hash", "kind"):
            _require_plain_str(getattr(self, name), field_name=name)


@dataclass(frozen=True, slots=True)
class StoryboardNarrativeBrief:
    schema: str; validation_contract_version: str; plan_id: str; source_ref: str; source_facts: tuple[SourceFact, ...]; hook: GroundedStatement; human_problem: GroundedStatement; tension: GroundedStatement; turning_point: GroundedStatement; resolution: GroundedStatement; primary_interpretation: CharacterInterpretation; secondary_interpretation: CharacterInterpretation | None; duo_context: DuoNarrativeContext; character_state_refs: tuple[CharacterStateReceiptRef, ...]; relationship_state_ref: RelationshipStateReceiptRef | None; canon_refs: tuple[CanonReceiptRef, ...]; visual_direction: VisualDirection; package_digest: str; derived_diversity_signature: NarrativeDiversitySignature
    def __post_init__(self) -> None:
        for name in ("schema", "validation_contract_version", "plan_id", "source_ref", "package_digest"):
            _require_plain_str(getattr(self, name), field_name=name)
        object.__setattr__(self, "source_facts", _tuple_of_dataclasses(self.source_facts, SourceFact, "brief_source_facts"))
        object.__setattr__(self, "character_state_refs", _tuple_of_dataclasses(self.character_state_refs, CharacterStateReceiptRef, "character_state_refs"))
        object.__setattr__(self, "canon_refs", _tuple_of_dataclasses(self.canon_refs, CanonReceiptRef, "canon_refs"))
        for name, cls in (("hook", GroundedStatement), ("human_problem", GroundedStatement), ("tension", GroundedStatement), ("turning_point", GroundedStatement), ("resolution", GroundedStatement), ("primary_interpretation", CharacterInterpretation), ("duo_context", DuoNarrativeContext), ("visual_direction", VisualDirection), ("derived_diversity_signature", NarrativeDiversitySignature)):
            if type(getattr(self, name)) is not cls: raise TypeError(name)
        if self.secondary_interpretation is not None and type(self.secondary_interpretation) is not CharacterInterpretation: raise TypeError("secondary_interpretation")
        if self.relationship_state_ref is not None and type(self.relationship_state_ref) is not RelationshipStateReceiptRef: raise TypeError("relationship_state_ref")


def package_digest(package: HumanStoryPackage) -> str: return _digest(package)
def _facts_digest(facts: Sequence[SourceFact]) -> str: return _digest(tuple((item.fact_id, item.text) for item in facts))
def _statement_digest(item: GroundedStatement, plan: EditorialPlanBinding, rules: str) -> str: return _digest((VALIDATION_CONTRACT_VERSION, plan.plan_id, plan.source_ref, item.text, item.inference_kind, item.source_fact_refs, item.editorial_refs, item.canon_refs, rules))
def _interpretation_digest(item: CharacterInterpretation, state: CharacterStateSnapshot, plan: EditorialPlanBinding, rules: str) -> str: return _digest((VALIDATION_CONTRACT_VERSION, plan.plan_id, plan.source_ref, item, state.snapshot_ref, state.revision, item.relationship_snapshot_ref, item.continuity_basis, item.source_fact_refs, item.canon_refs, rules))
def _visual_digest(item: VisualDirection, plan: EditorialPlanBinding, rules: str) -> str: return _digest((VALIDATION_CONTRACT_VERSION, plan.plan_id, plan.source_ref, item.mode_hint, item.narrative_subject, item.human_presence_policy, item.nonhuman_presence_policy, item.subjects, item.approved_motifs, item.excluded_motifs, item.source_fact_refs, item.visual_canon_refs, rules))


def character_interpretation_digest(item: CharacterInterpretation) -> str:
    """Digest the complete locked interpretation value without contextual inference."""
    if type(item) is not CharacterInterpretation:
        raise TypeError("item must be CharacterInterpretation")
    return _digest({"payload_version": "locked-character-interpretation-v1", "interpretation": item})


def relationship_continuity_payload(
    *,
    contract_version: str,
    plan_id: str,
    source_ref: str,
    duo_context: DuoNarrativeContext,
    primary_interpretation: CharacterInterpretation,
    secondary_interpretation: CharacterInterpretation,
    relationship_snapshot: RelationshipStateSnapshot,
    rules_version: str,
) -> dict[str, object]:
    """Return the single versioned payload locked by relationship evidence."""
    for name, value in (("contract_version", contract_version), ("plan_id", plan_id), ("source_ref", source_ref), ("rules_version", rules_version)):
        _require_plain_str(value, field_name=name)
    if type(duo_context) is not DuoNarrativeContext: raise TypeError("duo_context")
    if type(primary_interpretation) is not CharacterInterpretation: raise TypeError("primary_interpretation")
    if type(secondary_interpretation) is not CharacterInterpretation: raise TypeError("secondary_interpretation")
    if type(relationship_snapshot) is not RelationshipStateSnapshot: raise TypeError("relationship_snapshot")
    return {
        "payload_version": "relationship-continuity-v1",
        "validation_contract_version": contract_version,
        "plan_id": plan_id,
        "source_ref": source_ref,
        "presence_mode": duo_context.presence_mode,
        "interaction_mode": duo_context.interaction_mode,
        "relation_to_story": duo_context.relation_to_story,
        "source_fact_refs": list(duo_context.source_fact_refs),
        "primary_character_id": primary_interpretation.character_id,
        "secondary_character_id": secondary_interpretation.character_id,
        "primary_interpretation_digest": character_interpretation_digest(primary_interpretation),
        "secondary_interpretation_digest": character_interpretation_digest(secondary_interpretation),
        "relationship_snapshot_ref": relationship_snapshot.snapshot_ref,
        "relationship_revision": relationship_snapshot.revision,
        "relationship_version": relationship_snapshot.version,
        "authority_rules_version": rules_version,
    }


def relationship_continuity_digest(
    *,
    plan: EditorialPlanBinding,
    duo_context: DuoNarrativeContext,
    primary_interpretation: CharacterInterpretation,
    secondary_interpretation: CharacterInterpretation,
    relationship_snapshot: RelationshipStateSnapshot,
    rules_version: str,
) -> str:
    return _digest(relationship_continuity_payload(
        contract_version=VALIDATION_CONTRACT_VERSION,
        plan_id=plan.plan_id,
        source_ref=plan.source_ref,
        duo_context=duo_context,
        primary_interpretation=primary_interpretation,
        secondary_interpretation=secondary_interpretation,
        relationship_snapshot=relationship_snapshot,
        rules_version=rules_version,
    ))

def _ids(items: Sequence[Any], getter) -> tuple[set[Any], set[Any]]:
    seen: set[Any] = set(); duplicates: set[Any] = set()
    for item in items:
        key = getter(item)
        if key in seen: duplicates.add(key)
        seen.add(key)
    return seen, duplicates

def _cardinality(items: Sequence[Any], expected: set[Any], getter, duplicate: str, extra: str, missing: str, errors: set[str]) -> None:
    actual, duplicates = _ids(items, getter)
    if duplicates: errors.add(duplicate)
    if actual - expected: errors.add(extra)
    if expected - actual: errors.add(missing)

def _validate_integer(value: Any, *, revision=False, errors: set[str]) -> int | None:
    if type(value) is not int: errors.add("snapshot_scalar_type_invalid"); return None
    if revision and value < 0: errors.add("snapshot_revision_invalid")
    if not revision and not 0 <= value <= 100: errors.add("snapshot_axis_invalid")
    return value

def _validate_state(state: CharacterStateSnapshot, authority: CharacterSnapshotAuthority, errors: set[str]) -> None:
    if not all(_text(value) for value in (state.character_id, state.core_version, state.snapshot_ref)): errors.add("character_state_snapshot_invalid")
    _validate_integer(state.revision, revision=True, errors=errors)
    for axis in _STATE_AXES: _validate_integer(getattr(state, axis), errors=errors)
    if type(authority.expected_revision) is not int or authority.expected_revision < 0: errors.add("authority_revision_invalid")
    elif authority.character_id != state.character_id or authority.expected_core_version != state.core_version: errors.add("character_state_binding_invalid")
    elif authority.expected_snapshot_ref != state.snapshot_ref or authority.expected_revision != state.revision: errors.add("character_state_snapshot_stale")

def _validate_relationship(state: RelationshipStateSnapshot, authority: RelationshipSnapshotAuthority, errors: set[str]) -> None:
    _validate_integer(state.revision, revision=True, errors=errors)
    for axis in _REL_AXES: _validate_integer(getattr(state, axis), errors=errors)
    if type(authority.expected_revision) is not int or authority.expected_revision < 0: errors.add("authority_revision_invalid")
    elif authority.expected_version != state.version: errors.add("relationship_state_binding_invalid")
    elif authority.expected_snapshot_ref != state.snapshot_ref or authority.expected_revision != state.revision: errors.add("relationship_state_snapshot_stale")

def _short(text: str) -> bool:
    words = [word for word in re.findall(r"[^\W\d_]+", _norm(text), flags=re.UNICODE) if word not in {"naz", "void"}]
    return len(_text(text)) < 20 or len(words) < 4

def _shape(text: str) -> str:
    value = _norm(text).replace("—", "-").replace("–", "-")
    return " ".join(re.findall(r"<character>|[^\W_]+", re.sub(r"\b(?:naz|void)\b", "<character>", value), flags=re.UNICODE))

def _collapsed(first: CharacterInterpretation, second: CharacterInterpretation) -> bool:
    a, b = _shape(first.text), _shape(second.text)
    if a == b: return True
    return min(len(a.split()), len(b.split())) >= 4 and SequenceMatcher(None, a, b).ratio() >= .94

def _signature(package: HumanStoryPackage) -> NarrativeDiversitySignature:
    primary, secondary = package.primary_interpretation, package.secondary_interpretation
    return NarrativeDiversitySignature(primary.character_id, secondary.character_id if secondary else None, package.duo_context.presence_mode, _norm(package.hook.text), _norm(package.human_problem.text), _norm(package.tension.text), _norm(package.turning_point.text), _norm(package.resolution.text), _norm(primary.text), _norm(secondary.text) if secondary else None, _norm(package.visual_direction.mode_hint), _norm(primary.ending_mode))

def _check_diversity(signature: NarrativeDiversitySignature, context: NarrativeDiversityContext, errors: set[str]) -> None:
    for prior in context.recent_signatures:
        if signature == prior: errors.add("narrative_structure_repeated")
        if SequenceMatcher(None, signature.hook_fingerprint, prior.hook_fingerprint).ratio() >= .94: errors.add("narrative_hook_too_similar")
        if SequenceMatcher(None, signature.resolution_fingerprint, prior.resolution_fingerprint).ratio() >= .94: errors.add("narrative_ending_too_similar")
        candidate = " ".join((signature.hook_fingerprint, signature.human_problem_fingerprint, signature.tension_fingerprint, signature.turning_point_fingerprint, signature.resolution_fingerprint))
        old = " ".join((prior.hook_fingerprint, prior.human_problem_fingerprint, prior.tension_fingerprint, prior.turning_point_fingerprint, prior.resolution_fingerprint))
        if SequenceMatcher(None, candidate, old).ratio() >= .94: errors.add("narrative_text_too_similar")

def _validate_visual(visual: VisualDirection, facts: dict[str, SourceFact], canons: dict[str, dict[str, CanonSourceRef]], errors: set[str]) -> None:
    if visual.mode_hint not in {"cinematic", "documentary", "artifact"} or visual.human_presence_policy not in {"none", "canonical_only", "source_grounded"}: errors.add("visual_direction_unsupported")
    if visual.nonhuman_presence_policy not in {"none", "source_grounded"}: errors.add("nonhuman_presence_policy_invalid")
    approved, excluded = [_norm(x) for x in visual.approved_motifs], [_norm(x) for x in visual.excluded_motifs]
    if len(approved) != len(set(approved)) or len(excluded) != len(set(excluded)): errors.add("visual_motif_duplicate")
    if set(approved) & set(excluded): errors.add("visual_motif_conflict")
    all_visual = {ref.source_id: ref for refs in canons.values() for ref in refs.values() if ref.kind == "visual"}
    if not visual.visual_canon_refs or any(ref not in all_visual for ref in visual.visual_canon_refs): errors.add("visual_canon_missing")
    subject_digests: set[str] = set()
    for subject in visual.subjects:
        digest = _digest(subject)
        if digest in subject_digests: errors.add("visual_subject_duplicate")
        subject_digests.add(digest)
        if subject.subject_kind not in _SUBJECT_KINDS: errors.add("visual_direction_unsupported"); continue
        if len(subject.source_fact_refs) != len(set(subject.source_fact_refs)): errors.add("visual_subject_fact_ref_duplicate")
        if subject.subject_kind == "generic_human": errors.add("generic_human_visual"); continue
        if subject.subject_kind == "generic_robot": errors.add("generic_robot_visual"); continue
        if subject.subject_kind in {"naz", "void"}:
            owned = canons.get(subject.subject_kind, {})
            if visual.human_presence_policy != "canonical_only" or subject.character_id != subject.subject_kind: errors.add("visual_direction_unsupported")
            if not subject.identity_canon_refs or any(ref not in owned or owned[ref].kind != "visual" for ref in subject.identity_canon_refs): errors.add("visual_canon_missing")
        if subject.subject_kind in {"source_human", "source_nonhuman_agent"} and (not subject.source_fact_refs or any(ref not in facts for ref in subject.source_fact_refs)): errors.add("visual_direction_unsupported")
        if subject.subject_kind == "source_human" and visual.human_presence_policy != "source_grounded": errors.add("visual_direction_unsupported")
        if subject.subject_kind == "source_nonhuman_agent" and visual.nonhuman_presence_policy != "source_grounded": errors.add("source_nonhuman_agent_policy_required")

def validate_human_story_package(package: HumanStoryPackage, context: HumanStoryValidationContext) -> ValidatedHumanStoryPackage:
    if type(package) is not HumanStoryPackage or type(context) is not HumanStoryValidationContext: raise HumanStoryValidationError(("human_story_schema_invalid",))
    errors: set[str] = set(); plan = context.plan
    if package.schema != HUMAN_STORY_SCHEMA: errors.add("human_story_schema_invalid")
    if plan.production_mode != "story_first" or plan.content_format != "story_pack": errors.add("human_story_story_first_required")
    if package.plan_id != plan.plan_id: errors.add("human_story_plan_binding_invalid")
    if package.source_ref != plan.source_ref: errors.add("human_story_source_binding_invalid")
    if tuple((x.fact_id, x.text) for x in package.source_facts) != tuple((x.fact_id, x.text) for x in context.expected_source_facts):
        if tuple(x.fact_id for x in package.source_facts) != tuple(x.fact_id for x in context.expected_source_facts): errors.add("source_fact_identity_changed")
        if tuple(x.text for x in package.source_facts) != tuple(x.text for x in context.expected_source_facts): errors.add("source_fact_text_changed")
    facts: dict[str, SourceFact] = {}; fact_ids, fact_duplicates = _ids(package.source_facts, lambda x: x.fact_id)
    if fact_duplicates: errors.add("source_fact_duplicate")
    if not package.source_facts: errors.add("source_fact_missing")
    for fact in package.source_facts: facts[fact.fact_id] = fact
    statements = tuple(getattr(package, name) for name in _STORY_FIELDS)
    for item in statements:
        if item.inference_kind not in {"observed", "bounded_interpretation"}: errors.add("human_story_schema_invalid")
        if not item.source_fact_refs: errors.add("source_fact_missing")
        if len(item.source_fact_refs) != len(set(item.source_fact_refs)): errors.add("source_fact_duplicate")
        if any(ref not in facts for ref in item.source_fact_refs): errors.add("source_fact_ref_unknown")
        if item.inference_kind == "observed" and _norm(item.text) not in {_norm(facts[ref].text) for ref in item.source_fact_refs if ref in facts}: errors.add("observed_claim_unsupported")
    primary, secondary, mode = package.primary_interpretation, package.secondary_interpretation, package.duo_context.presence_mode
    used = {primary.character_id} | ({secondary.character_id} if secondary else set())
    relation_needed = mode in {"implicit", "explicit"}
    if mode not in {"none", "implicit", "explicit"} or (mode == "none" and (secondary is not None or package.relationship_state is not None)) or (relation_needed and (secondary is None or package.relationship_state is None)): errors.add("forced_dual_interpretation_formula")
    if secondary and secondary.character_id == primary.character_id: errors.add("forced_dual_interpretation_formula")
    if secondary and _collapsed(primary, secondary): errors.add("character_interpretations_collapsed")
    if any(_short(x.text) for x in (primary, secondary) if x is not None): errors.add("character_interpretation_too_short")
    _cardinality(package.character_states, used, lambda x: x.character_id, "character_state_snapshot_duplicate", "character_state_snapshot_extra", "character_state_snapshot_missing", errors)
    _cardinality(package.character_canons, used, lambda x: x.character_id, "character_canon_duplicate", "character_canon_extra", "character_canon_missing", errors)
    _cardinality(context.character_snapshot_authorities, used, lambda x: x.character_id, "character_authority_duplicate", "character_authority_extra", "character_authority_missing", errors)
    _cardinality(context.authority_policy.character_policies, used, lambda x: x.character_id, "character_policy_duplicate", "character_policy_extra", "character_policy_missing", errors)
    states = {x.character_id: x for x in package.character_states}; authorities = {x.character_id: x for x in context.character_snapshot_authorities}
    canon_maps: dict[str, dict[str, CanonSourceRef]] = {}
    for snapshot in package.character_canons:
        source_ids: set[str] = set()
        for ref in snapshot.canon_refs:
            if ref.character_id != snapshot.character_id or ref.kind not in _CANON_KINDS or not all(_text(v) for v in (ref.source_id, ref.source_path, ref.source_version, ref.source_hash)): errors.add("character_canon_binding_invalid")
            if ref.source_id in source_ids:
                errors.add("canon_source_id_duplicate")
                continue
            source_ids.add(ref.source_id)
        if snapshot.conflict_reason_codes: errors.add("character_canon_conflict")
        refs: dict[str, CanonSourceRef] = {}
        for ref in snapshot.canon_refs:
            if ref.source_id not in refs:
                refs[ref.source_id] = ref
        canon_maps[snapshot.character_id] = refs
    for character in used:
        if character in states and character in authorities: _validate_state(states[character], authorities[character], errors)
    if relation_needed:
        if context.relationship_snapshot_authority is None: errors.add("relationship_state_snapshot_missing")
        elif package.relationship_state is not None: _validate_relationship(package.relationship_state, context.relationship_snapshot_authority, errors)
        if context.relationship_continuity_evidence is None: errors.add("relationship_evidence_missing")
        if context.authority_policy.relationship_authority_ref is None or context.authority_policy.relationship_rules_version is None: errors.add("relationship_evidence_missing")
    elif context.relationship_snapshot_authority is not None: errors.add("relationship_authority_unexpected")
    elif context.relationship_continuity_evidence is not None: errors.add("relationship_evidence_unexpected")
    interpretations = tuple(x for x in (primary, secondary) if x is not None)
    expected_semantic: dict[str, tuple[str, ...]] = {}
    for item in statements:
        if item.inference_kind == "bounded_interpretation": expected_semantic[_statement_digest(item, plan, context.authority_policy.semantic_rules_version)] = item.source_fact_refs
    for item in interpretations:
        if item.character_id not in states: continue
        expected_semantic[_interpretation_digest(item, states[item.character_id], plan, context.authority_policy.semantic_rules_version)] = item.source_fact_refs
    _cardinality(context.semantic_grounding_evidence, set(expected_semantic), lambda x: x.statement_digest, "semantic_evidence_duplicate", "semantic_evidence_extra", "semantic_evidence_missing", errors)
    _cardinality(context.character_continuity_evidence, used, lambda x: x.character_id, "character_evidence_duplicate", "character_evidence_extra", "character_evidence_missing", errors)
    semantic = {x.statement_digest: x for x in context.semantic_grounding_evidence}; character_evidence = {x.character_id: x for x in context.character_continuity_evidence}; policies = {x.character_id: x for x in context.authority_policy.character_policies}
    for item in interpretations:
        state = states.get(item.character_id); canon = canon_maps.get(item.character_id, {})
        if state is None: continue
        if item.state_snapshot_ref != state.snapshot_ref: errors.add("character_state_binding_invalid")
        if not item.canon_refs or not any(ref in canon and canon[ref].kind == "personality" for ref in item.canon_refs): errors.add("character_personality_canon_missing")
        if any(ref not in canon for ref in item.canon_refs): errors.add("character_canon_binding_invalid")
        digest = _interpretation_digest(item, state, plan, context.authority_policy.semantic_rules_version); sem = semantic.get(digest); cont = character_evidence.get(item.character_id); policy = policies.get(item.character_id)
        if sem and (sem.plan_id != plan.plan_id or sem.source_ref != plan.source_ref or sem.source_fact_refs != item.source_fact_refs or sem.authority_ref != context.authority_policy.semantic_authority_ref or sem.rules_version != context.authority_policy.semantic_rules_version or sem.decision != "supported"): errors.add("semantic_evidence_conflict")
        if cont and (cont.plan_id != plan.plan_id or cont.source_ref != plan.source_ref or cont.state_snapshot_ref != state.snapshot_ref or cont.state_revision != state.revision or cont.relationship_snapshot_ref != item.relationship_snapshot_ref or cont.interpretation_digest != digest or policy is None or cont.authority_ref != policy.authority_ref or cont.rules_version != policy.rules_version or cont.decision != "supported"): errors.add("character_evidence_conflict")
    if relation_needed and secondary is not None and package.relationship_state is not None:
        if package.duo_context.relationship_snapshot_ref != package.relationship_state.snapshot_ref or primary.relationship_snapshot_ref != package.relationship_state.snapshot_ref or secondary.relationship_snapshot_ref != package.relationship_state.snapshot_ref:
            errors.add("relationship_state_binding_invalid")
        if not package.duo_context.source_fact_refs or len(package.duo_context.source_fact_refs) != len(set(package.duo_context.source_fact_refs)) or any(ref not in facts for ref in package.duo_context.source_fact_refs):
            errors.add("source_fact_ref_unknown")
        for character in used:
            if not any(ref.kind == "relationship" for ref in canon_maps.get(character, {}).values()): errors.add("character_relationship_canon_missing")
        evidence = context.relationship_continuity_evidence
        rules_version = context.authority_policy.relationship_rules_version
        expected_duo_digest = None
        if type(rules_version) is str and rules_version.strip():
            expected_duo_digest = relationship_continuity_digest(
                plan=plan,
                duo_context=package.duo_context,
                primary_interpretation=primary,
                secondary_interpretation=secondary,
                relationship_snapshot=package.relationship_state,
                rules_version=rules_version,
            )
        if evidence and (
            evidence.plan_id != plan.plan_id
            or evidence.source_ref != plan.source_ref
            or evidence.presence_mode != mode
            or evidence.interaction_mode != package.duo_context.interaction_mode
            or evidence.relation_to_story != package.duo_context.relation_to_story
            or evidence.primary_character_id != primary.character_id
            or evidence.secondary_character_id != secondary.character_id
            or evidence.primary_interpretation_digest != character_interpretation_digest(primary)
            or evidence.secondary_interpretation_digest != character_interpretation_digest(secondary)
            or evidence.relationship_snapshot_ref != package.relationship_state.snapshot_ref
            or evidence.relationship_revision != package.relationship_state.revision
            or evidence.relationship_version != package.relationship_state.version
            or evidence.source_fact_refs != package.duo_context.source_fact_refs
            or expected_duo_digest is None
            or evidence.duo_context_digest != expected_duo_digest
            or evidence.authority_ref != context.authority_policy.relationship_authority_ref
            or evidence.rules_version != rules_version
            or evidence.decision != "supported"
        ):
            errors.add("relationship_continuity_evidence_invalid")
    _validate_visual(package.visual_direction, facts, canon_maps, errors)
    visual_digest = _visual_digest(package.visual_direction, plan, context.authority_policy.visual_rules_version); visual = context.visual_grounding_evidence
    if visual.plan_id != plan.plan_id or visual.source_ref != plan.source_ref or visual.visual_digest != visual_digest or visual.authority_ref != context.authority_policy.visual_authority_ref or visual.rules_version != context.authority_policy.visual_rules_version or visual.decision != "supported": errors.add("visual_grounding_evidence_invalid")
    if package.confidence.level != "high" or package.confidence.reason_codes: errors.add("confidence_insufficient")
    signature = _signature(package); _check_diversity(signature, context.diversity_context, errors)
    if errors: raise HumanStoryValidationError(errors)
    return ValidatedHumanStoryPackage(package, package_digest(package), signature, VALIDATION_CONTRACT_VERSION)

def build_storyboard_narrative_brief(package: HumanStoryPackage, context: HumanStoryValidationContext) -> StoryboardNarrativeBrief:
    validated = validate_human_story_package(package, context)
    states = tuple(CharacterStateReceiptRef(x.character_id, x.snapshot_ref, x.revision, x.core_version) for x in package.character_states)
    canon = tuple(CanonReceiptRef(ref.character_id, ref.source_id, ref.source_version, ref.source_hash, ref.kind) for snapshot in package.character_canons for ref in snapshot.canon_refs)
    relation = None if package.relationship_state is None else RelationshipStateReceiptRef(package.relationship_state.snapshot_ref, package.relationship_state.revision, package.relationship_state.version)
    return StoryboardNarrativeBrief(STORYBOARD_BRIEF_SCHEMA, VALIDATION_CONTRACT_VERSION, package.plan_id, package.source_ref, package.source_facts, package.hook, package.human_problem, package.tension, package.turning_point, package.resolution, package.primary_interpretation, package.secondary_interpretation, package.duo_context, states, relation, canon, package.visual_direction, validated.package_digest, validated.derived_diversity_signature)

def validate_storyboard_narrative_brief_structure(brief: StoryboardNarrativeBrief) -> None:
    """Validate only locally provable brief structure; external authority is out of scope."""
    if type(brief) is not StoryboardNarrativeBrief:
        raise HumanStoryValidationError(("storyboard_scope_violation",))
    errors: set[str] = set()
    if brief.schema != STORYBOARD_BRIEF_SCHEMA or brief.validation_contract_version != VALIDATION_CONTRACT_VERSION or not _digest_format(brief.package_digest):
        errors.add("storyboard_scope_violation")
    if type(brief.source_facts) is not tuple or any(type(item) is not SourceFact for item in brief.source_facts):
        errors.add("storyboard_fact_loss")
        facts: dict[str, SourceFact] = {}
    else:
        fact_ids, duplicates = _ids(brief.source_facts, lambda item: item.fact_id)
        if duplicates or not brief.source_facts:
            errors.add("storyboard_fact_loss")
        facts = {item.fact_id: item for item in brief.source_facts}
    statements = tuple(getattr(brief, name) for name in _STORY_FIELDS)
    for statement in statements:
        if type(statement) is not GroundedStatement or not statement.source_fact_refs or any(ref not in facts for ref in statement.source_fact_refs):
            errors.add("storyboard_fact_changed")
    for ref in brief.visual_direction.source_fact_refs:
        if ref not in facts: errors.add("storyboard_fact_changed")
    for subject in brief.visual_direction.subjects:
        if any(ref not in facts for ref in subject.source_fact_refs): errors.add("storyboard_fact_changed")
    primary, secondary, mode = brief.primary_interpretation, brief.secondary_interpretation, brief.duo_context.presence_mode
    used = {primary.character_id} | ({secondary.character_id} if secondary else set())
    state_ids, state_duplicates = _ids(brief.character_state_refs, lambda item: item.character_id)
    if state_duplicates or state_ids != used:
        errors.add("storyboard_scope_violation")
    state_by_id = {item.character_id: item for item in brief.character_state_refs}
    primary_state = state_by_id.get(primary.character_id)
    if primary_state is None or primary_state.snapshot_ref != primary.state_snapshot_ref:
        errors.add("storyboard_scope_violation")
    if secondary is None:
        if mode != "none": errors.add("storyboard_scope_violation")
    else:
        secondary_state = state_by_id.get(secondary.character_id)
        if secondary.character_id == primary.character_id or secondary_state is None or secondary_state.snapshot_ref != secondary.state_snapshot_ref:
            errors.add("storyboard_scope_violation")
    if mode == "none":
        if brief.relationship_state_ref is not None or brief.duo_context.relationship_snapshot_ref is not None:
            errors.add("storyboard_scope_violation")
    elif mode in {"implicit", "explicit"}:
        if brief.relationship_state_ref is None or brief.duo_context.relationship_snapshot_ref != brief.relationship_state_ref.snapshot_ref:
            errors.add("storyboard_scope_violation")
    else:
        errors.add("storyboard_scope_violation")
    signature = brief.derived_diversity_signature
    if type(signature) is not NarrativeDiversitySignature or signature.primary_character_id != primary.character_id or signature.secondary_character_id != (secondary.character_id if secondary else None) or signature.presence_mode != mode:
        errors.add("storyboard_scope_violation")
    if type(brief.canon_refs) is not tuple or any(type(item) is not CanonReceiptRef or hasattr(item, "source_path") for item in brief.canon_refs):
        errors.add("storyboard_scope_violation")
    _, canon_duplicates = _ids(brief.canon_refs, lambda item: (item.character_id, item.source_id))
    if canon_duplicates:
        errors.add("storyboard_scope_violation")
    director_fields = ("story_arc", "scene", "role", "action", "prop", "setting", "state_transition", "camera", "camera_language", "shot", "provider_prompt", "render_prompt")
    if any(hasattr(brief, name) for name in director_fields): errors.add("storyboard_scope_violation")
    if errors: raise HumanStoryValidationError(errors)
