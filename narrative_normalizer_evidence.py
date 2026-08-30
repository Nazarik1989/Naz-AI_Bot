"""Normalizer-owned open-domain source evidence boundary.

This module intentionally has no dependency on Narrative CP1/CP2 or on the
runtime normalizer.  It turns immutable UTF-8 source documents into exact
segments, validates model-proposed evidence against byte and character spans,
requires a separate adjudication decision for every evidence item, and emits
only code-verified fact bindings.

The module performs no network I/O.  Model access is dependency-injected via
``EvidenceModelClient`` and tests use in-memory fakes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields, is_dataclass, replace
from datetime import datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Protocol, Sequence
import json
import re

import model_boundary_privacy as privacy


SOURCE_DOCUMENT_CONTRACT_VERSION = "normalizer-source-document-v1"
EVIDENCE_EXTRACTION_CONTRACT_VERSION = "normalizer-evidence-extraction-v1"
EVIDENCE_COVERAGE_CONTRACT_VERSION = "normalizer-evidence-coverage-v2"
EVIDENCE_EXTRACTION_V2_CONTRACT_VERSION = "normalizer-evidence-extraction-v2"
EVIDENCE_EXTRACTION_V3_CONTRACT_VERSION = "normalizer-evidence-extraction-v3"
EVIDENCE_SPAN_SELECTION_CONTRACT_VERSION = "normalizer-evidence-span-selection-v1"
EVIDENCE_ADJUDICATION_CONTRACT_VERSION = "normalizer-evidence-adjudication-v1"
CODE_OWNED_EXTRACTION_CHECKPOINT_VERSION = "normalizer-code-owned-extraction-v1"
EVIDENCE_SELECTION_RECEIPT_VERSION = "normalizer-evidence-selection-receipt-v1"
ADJUDICATION_DIAGNOSTIC_VERSION = "normalizer-adjudication-diagnostic-v1"
VERIFIED_EVIDENCE_CONTRACT_VERSION = "normalizer-verified-evidence-v1"
VERIFIED_FACT_BINDING_VERSION = "normalizer-verified-fact-binding-v1"

MAX_DOCUMENT_BYTES = 2_000_000
MAX_SOURCE_BYTES = 8_000_000
MAX_DOCUMENTS = 128
MAX_SEGMENTS = 20_000
MAX_BLOCK_SEGMENTS = 16
MAX_BLOCK_CHARACTERS = 4_096

MEDIA_TYPES = frozenset({
    "plain_text",
    "markdown",
    "json",
    "jsonl",
    "log",
    "key_value",
    "chat",
    "email",
    "mixed_text",
})
SEGMENT_KINDS = frozenset({
    "text_line",
    "heading",
    "list_item",
    "code_block",
    "log_entry",
    "json_scalar",
    "key_value_entry",
    "chat_message",
    "email_header",
    "email_body",
    "unknown_text",
})
COVERAGE_CLASSIFICATIONS = frozenset({
    "known_deterministic_grammar",
    "unknown_but_text_readable",
    "json_like",
    "log_like",
    "markdown_like",
    "chat_email_like",
    "insufficient",
    "sensitive",
    "unsupported_binary_container",
    "parse_error",
})
EVIDENCE_KINDS = frozenset({
    "observed_fact",
    "explicit_relation",
    "explicit_cause",
    "explicit_sequence",
    "quoted_statement",
    "source_supported_interpretation",
    "insufficient_or_ambiguous",
})
ATOM_KINDS = frozenset({"entity", "number", "date"})
TEMPORAL_RELATIONS = frozenset({"before", "after", "sequence"})
CAUSAL_RELATIONS = frozenset({"because", "therefore", "caused"})
FACT_RELATION_KINDS = frozenset({
    "temporal_before", "temporal_after", "temporal_overlap",
    "causal", "enables", "contradicts",
})
ATOMIC_EVIDENCE_KINDS = frozenset({"observed_fact", "quoted_statement"})
MIN_INDEPENDENT_FACTS = 3
POLARITIES = frozenset({"affirmed", "negated", "quoted"})
UNCERTAINTIES = frozenset({"certain", "uncertain", "ambiguous"})
PUBLIC_SAFETY = frozenset({"safe", "sensitive", "mixed", "unknown"})
SEGMENT_DISPOSITIONS = frozenset({"evidence", "irrelevant", "sensitive", "ambiguous"})
BLOCK_DISPOSITIONS = frozenset({
    "evidence_candidate", "context_only", "structural", "sensitive_withheld", "ambiguous",
})
EVIDENCE_DECISIONS = frozenset({"supported", "rejected", "ambiguous"})
RESOLUTION_STATUSES = frozenset({
    "verified",
    "source_insufficient",
    "manual_attention",
    "sensitive_rejected",
    "failed",
})

REASON_CODES = frozenset({
    "evidence_verified",
    "evidence_source_invalid",
    "evidence_source_changed",
    "evidence_source_too_large",
    "evidence_source_parse_error",
    "evidence_schema_invalid",
    "evidence_source_binding_invalid",
    "evidence_segment_binding_invalid",
    "evidence_quote_binding_invalid",
    "evidence_proposition_binding_invalid",
    "evidence_value_binding_invalid",
    "evidence_relation_binding_invalid",
    "evidence_polarity_invalid",
    "evidence_uncertainty_invalid",
    "evidence_sensitive",
    "evidence_adjudication_incomplete",
    "evidence_adjudication_conflict",
    "evidence_source_insufficient",
    "evidence_manual_attention",
    "evidence_provider_failed",
    "evidence_verified_bundle_invalid",
    "evidence_coverage_incomplete",
    "evidence_coverage_conflict",
})
ADJUDICATION_REASON_CODES = frozenset({
    "unsupported_proposition",
    "ambiguous_source",
    "missing_context",
    "sensitive_content",
    "quote_conflict",
    "relation_conflict",
    "polarity_conflict",
    "uncertainty_conflict",
})

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_NUMBER = re.compile(
    r"[+-]?(?:(?:\d{1,3}(?:[ _\u00a0]\d{3})+)|\d+)(?:[.,]\d+)?(?:[eE][+-]?\d+)?"
)
_DATE = re.compile(
    r"(?:\d{4}[-/.]\d{2}[-/.]\d{2}|\d{2}[-/.]\d{2}[-/.]\d{4}|"
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)
_NEGATION = re.compile(
    r"\b(?:not|never|no|without|isn't|aren't|wasn't|weren't|didn't|doesn't|don't|"
    r"не|нет|никогда|без)\b",
    re.IGNORECASE,
)
_UNCERTAINTY = re.compile(
    r"\b(?:may|might|possibly|perhaps|unclear|uncertain|unknown|apparently|"
    r"возможно|вероятно|неясно|неизвестно|предположительно)\b",
    re.IGNORECASE,
)
_TEMPORAL_MARKERS = {
    "before": re.compile(r"\b(?:before|prior\s+to|до|раньше)\b", re.IGNORECASE),
    "after": re.compile(r"\b(?:after|following|после|позже)\b", re.IGNORECASE),
    "sequence": re.compile(
        r"(?:\bthen\b|\b(?:затем|потом)\b|\bfirst\b[^.\n]{0,120}\bthen\b|"
        r"\bсначала\b[^.\n]{0,120}\b(?:затем|потом)\b)",
        re.IGNORECASE,
    ),
}
_CAUSAL_MARKERS = {
    "because": re.compile(r"\b(?:because|since|потому\s+что|так\s+как)\b", re.IGNORECASE),
    "therefore": re.compile(r"\b(?:therefore|thus|поэтому|следовательно)\b", re.IGNORECASE),
    "caused": re.compile(r"\b(?:caused|led\s+to|вызвал\w*|прив[её]л\w*\s+к)\b", re.IGNORECASE),
}
_LOG_LINE = re.compile(
    r"^\s*(?:\d{4}-\d{2}-\d{2}[ T]|\[?(?:DEBUG|INFO|WARN(?:ING)?|ERROR|CRITICAL)\]?)",
    re.IGNORECASE,
)
_KEY_VALUE_LINE = re.compile(r"^\s*[A-Za-zА-Яа-яЁё0-9_.-]{1,80}\s*[:=]\s*\S")
_CHAT_LINE = re.compile(r"^\s*(?:\[[^\]\r\n]{1,40}\]\s*)?[A-Za-zА-Яа-яЁё][^:\r\n]{0,40}:\s+\S")
_EMAIL_HEADER = re.compile(r"^\s*(?:from|to|cc|bcc|subject|date|от|кому|тема|дата)\s*:", re.IGNORECASE)


_PROPOSITION_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "been", "by", "for", "from",
    "has", "have", "in", "is", "it", "of", "on", "or", "that", "the", "this",
    "to", "was", "were", "will", "with",
    "а", "без", "был", "была", "были", "быть", "в", "во", "для", "до", "его",
    "ее", "если", "есть", "же", "за", "и", "из", "или", "их", "к", "как", "на",
    "не", "но", "о", "он", "она", "они", "от", "по", "при", "с", "со", "так",
    "то", "у", "что", "это", "этот", "я",
})


_DIAGNOSTIC_STAGES = frozenset({
    "response_type",
    "json_parse",
    "top_level_schema",
    "contract_version",
    "source_binding",
    "nested_schema",
    "segment_binding",
    "quote_binding",
    "proposition_binding",
    "value_binding",
    "relation_binding",
    "semantic_validation",
    "provider_boundary",
})
_DIAGNOSTIC_TYPE_LABELS = frozenset({
    "dict", "list", "str", "int", "float", "bool", "null", "other",
})
_DIAGNOSTIC_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,79}\Z")
_DIAGNOSTIC_PATH = re.compile(r"\$(?:\.[A-Za-z][A-Za-z0-9_-]{0,79}|\[\]){0,8}\Z")


_SEMANTIC_REJECTIONS = {
    "disposition_partition_mismatch": (
        "segment_binding", "disposition_partition_mismatch", "$.segment_dispositions", "not_applicable",
    ),
    "duplicate_or_missing_segment_disposition": (
        "segment_binding", "duplicate_or_missing_segment_disposition", "$.segment_dispositions", "not_applicable",
    ),
    "evidence_item_not_bound_to_source_segment": (
        "segment_binding", "evidence_item_not_bound_to_source_segment", "$.evidence[].ordered_segment_refs", "not_applicable",
    ),
    "evidence_references_withheld_segment": (
        "segment_binding", "evidence_references_withheld_segment", "$.evidence[].ordered_segment_refs", "not_applicable",
    ),
    "entity_ownership_invalid": (
        "value_binding", "entity_ownership_invalid", "$.evidence[].entities[]", "not_applicable",
    ),
    "number_ownership_invalid": (
        "value_binding", "number_ownership_invalid", "$.evidence[].numbers[]", "not_applicable",
    ),
    "date_ownership_invalid": (
        "value_binding", "date_ownership_invalid", "$.evidence[].dates[]", "not_applicable",
    ),
    "polarity_mismatch": (
        "semantic_validation", "polarity_mismatch", "$.evidence[].polarity", "not_applicable",
    ),
    "temporal_relation_mismatch": (
        "relation_binding", "temporal_relation_mismatch", "$.evidence[].temporal_relation", "not_applicable",
    ),
    "temporal_relation_operands_incomplete": (
        "relation_binding", "temporal_relation_operands_incomplete", "$.evidence[].temporal_relation", "not_applicable",
    ),
    "causal_relation_mismatch": (
        "relation_binding", "causal_relation_mismatch", "$.evidence[].causal_relation", "not_applicable",
    ),
    "causal_relation_operands_incomplete": (
        "relation_binding", "causal_relation_operands_incomplete", "$.evidence[].causal_relation", "not_applicable",
    ),
    "duplicate_or_conflicting_evidence": (
        "semantic_validation", "duplicate_or_conflicting_evidence", "$.evidence", "not_applicable",
    ),
    "evidence_count_or_coverage_policy_invalid": (
        "semantic_validation", "evidence_count_or_coverage_policy_invalid", "$.evidence", "not_applicable",
    ),
    "unsupported_or_ambiguous_proposition": (
        "proposition_binding", "unsupported_or_ambiguous_proposition", "$.evidence[].proposition", "not_applicable",
    ),
    "generic_or_meaning_anchor_rejection": (
        "proposition_binding", "generic_or_meaning_anchor_rejection", "$.evidence[]", "not_applicable",
    ),
    "quote_span_or_ownership_invalid": (
        "quote_binding", "quote_span_or_ownership_invalid", "$.evidence[].exact_quotes[]", "exact_span_invalid",
    ),
    "duplicate_quote_or_conflicting_evidence": (
        "semantic_validation", "duplicate_or_conflicting_evidence", "$.evidence[].exact_quotes[]", "not_applicable",
    ),
    "uncertainty_mismatch": (
        "semantic_validation", "uncertainty_mismatch", "$.evidence[].uncertainty", "not_applicable",
    ),
    "privacy_classification_rejected": (
        "semantic_validation", "privacy_classification_rejected", "$.evidence[]", "not_applicable",
    ),
}


@dataclass(frozen=True, slots=True)
class EvidenceValidationDiagnostic:
    """Closed privacy-safe description of one rejected model response."""

    validation_stage: str
    stable_subreason: str
    field_path: str
    response_top_level_exact_type: str
    top_level_key_set: tuple[str, ...]
    missing_keys: tuple[str, ...]
    extra_keys: tuple[str, ...]
    nested_field_types: tuple[tuple[str, str], ...]
    list_item_counts: tuple[tuple[str, int], ...]
    schema_contract_version: str
    span_quote_validation_category: str
    source_identity_binding_result: str
    response_byte_size: int
    response_character_size: int

    def __post_init__(self) -> None:
        if self.validation_stage not in _DIAGNOSTIC_STAGES:
            raise ValueError("validation_stage")
        for value, name in (
            (self.stable_subreason, "stable_subreason"),
            (self.schema_contract_version, "schema_contract_version"),
            (self.span_quote_validation_category, "span_quote_validation_category"),
            (self.source_identity_binding_result, "source_identity_binding_result"),
        ):
            if type(value) is not str or _DIAGNOSTIC_KEY.fullmatch(value) is None:
                raise TypeError(name)
        if type(self.field_path) is not str or _DIAGNOSTIC_PATH.fullmatch(self.field_path) is None:
            raise TypeError("field_path")
        if self.response_top_level_exact_type not in _DIAGNOSTIC_TYPE_LABELS:
            raise ValueError("response_top_level_exact_type")
        for values, name in (
            (self.top_level_key_set, "top_level_key_set"),
            (self.missing_keys, "missing_keys"),
            (self.extra_keys, "extra_keys"),
        ):
            if (
                type(values) is not tuple
                or any(type(item) is not str or _DIAGNOSTIC_KEY.fullmatch(item) is None for item in values)
                or tuple(sorted(values)) != values
                or len(values) != len(set(values))
            ):
                raise TypeError(name)
        if (
            type(self.nested_field_types) is not tuple
            or any(
                type(item) is not tuple
                or len(item) != 2
                or type(item[0]) is not str
                or _DIAGNOSTIC_PATH.fullmatch(item[0]) is None
                or item[1] not in _DIAGNOSTIC_TYPE_LABELS
                for item in self.nested_field_types
            )
        ):
            raise TypeError("nested_field_types")
        if (
            type(self.list_item_counts) is not tuple
            or any(
                type(item) is not tuple
                or len(item) != 2
                or type(item[0]) is not str
                or _DIAGNOSTIC_PATH.fullmatch(item[0]) is None
                or type(item[1]) is not int
                or isinstance(item[1], bool)
                or item[1] < 0
                for item in self.list_item_counts
            )
        ):
            raise TypeError("list_item_counts")
        for value, name in (
            (self.response_byte_size, "response_byte_size"),
            (self.response_character_size, "response_character_size"),
        ):
            if type(value) is not int or isinstance(value, bool) or value < 0:
                raise TypeError(name)
        if self.source_identity_binding_result not in {"matched", "mismatched", "unavailable"}:
            raise ValueError("source_identity_binding_result")

    def safe_payload(self) -> dict[str, object]:
        return {
            "validation_stage": self.validation_stage,
            "stable_subreason": self.stable_subreason,
            "field_path": self.field_path,
            "response_top_level_exact_type": self.response_top_level_exact_type,
            "top_level_key_set": list(self.top_level_key_set),
            "missing_keys": list(self.missing_keys),
            "extra_keys": list(self.extra_keys),
            "nested_field_types": [
                {"field_path": path, "exact_type": exact_type}
                for path, exact_type in self.nested_field_types
            ],
            "list_item_counts": [
                {"field_path": path, "item_count": count}
                for path, count in self.list_item_counts
            ],
            "schema_contract_version": self.schema_contract_version,
            "span_quote_validation_category": self.span_quote_validation_category,
            "source_identity_binding_result": self.source_identity_binding_result,
            "response_byte_size": self.response_byte_size,
            "response_character_size": self.response_character_size,
        }


_EVIDENCE_VALIDATION_DIAGNOSTIC_KEYS = frozenset({
    "validation_stage", "stable_subreason", "field_path",
    "response_top_level_exact_type", "top_level_key_set", "missing_keys",
    "extra_keys", "nested_field_types", "list_item_counts",
    "schema_contract_version", "span_quote_validation_category",
    "source_identity_binding_result", "response_byte_size",
    "response_character_size",
})


def evidence_validation_diagnostic_from_payload(
    value: object,
) -> EvidenceValidationDiagnostic:
    """Recreate a diagnostic only from its closed privacy-safe payload."""

    if type(value) is not dict or frozenset(value) != _EVIDENCE_VALIDATION_DIAGNOSTIC_KEYS:
        raise TypeError("evidence validation diagnostic")
    nested = value["nested_field_types"]
    counts = value["list_item_counts"]
    if (
        type(nested) is not list
        or any(type(item) is not dict or frozenset(item) != {"field_path", "exact_type"} for item in nested)
        or type(counts) is not list
        or any(type(item) is not dict or frozenset(item) != {"field_path", "item_count"} for item in counts)
    ):
        raise TypeError("evidence validation diagnostic")
    try:
        return EvidenceValidationDiagnostic(
            validation_stage=value["validation_stage"],
            stable_subreason=value["stable_subreason"],
            field_path=value["field_path"],
            response_top_level_exact_type=value["response_top_level_exact_type"],
            top_level_key_set=tuple(value["top_level_key_set"]),
            missing_keys=tuple(value["missing_keys"]),
            extra_keys=tuple(value["extra_keys"]),
            nested_field_types=tuple(
                (item["field_path"], item["exact_type"]) for item in nested
            ),
            list_item_counts=tuple(
                (item["field_path"], item["item_count"]) for item in counts
            ),
            schema_contract_version=value["schema_contract_version"],
            span_quote_validation_category=value["span_quote_validation_category"],
            source_identity_binding_result=value["source_identity_binding_result"],
            response_byte_size=value["response_byte_size"],
            response_character_size=value["response_character_size"],
        )
    except (KeyError, TypeError, ValueError):
        raise TypeError("evidence validation diagnostic") from None


class EvidenceContractError(ValueError):
    """Privacy-safe public failure for the evidence boundary."""

    def __init__(
        self,
        reason_code: str,
        diagnostic: EvidenceValidationDiagnostic | AdjudicationValidationDiagnostic | None = None,
        *,
        semantic_rejection: str | None = None,
    ):
        code = reason_code if reason_code in REASON_CODES else "evidence_schema_invalid"
        super().__init__(code)
        self.reason_code = code
        self.diagnostic = (
            diagnostic
            if type(diagnostic) in {
                EvidenceValidationDiagnostic,
                AdjudicationValidationDiagnostic,
            }
            else None
        )
        self._semantic_rejection = (
            semantic_rejection if semantic_rejection in _SEMANTIC_REJECTIONS else None
        )
        self.__cause__ = None
        self.__context__ = None


def _raise(reason_code: str, *, semantic_rejection: str | None = None) -> None:
    raise EvidenceContractError(reason_code, semantic_rejection=semantic_rejection) from None


def _plain(value: object, name: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str or (not allow_empty and not value.strip()) or "\x00" in value:
        raise TypeError(name)
    return value


def _plain_int(value: object, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise TypeError(name)
    return value


def _safe_id(value: object, name: str) -> str:
    if type(value) is not str or _SAFE_ID.fullmatch(value) is None:
        raise TypeError(name)
    return value


def _hex64(value: object, name: str) -> str:
    if type(value) is not str or _HEX64.fullmatch(value) is None:
        raise TypeError(name)
    return value


def _enum(value: object, allowed: frozenset[str], name: str) -> str:
    if type(value) is not str or value not in allowed:
        raise ValueError(name)
    return value


def _typed_tuple(value: object, cls: type, name: str, *, allow_empty: bool = True) -> tuple:
    if type(value) is not tuple or (not allow_empty and not value) or any(type(item) is not cls for item in value):
        raise TypeError(name)
    return value


def _strings(value: object, name: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    if type(value) is not tuple or (not allow_empty and not value):
        raise TypeError(name)
    if any(type(item) is not str or not item for item in value) or len(value) != len(set(value)):
        raise TypeError(name)
    return value


def _to_data(value: object) -> object:
    if is_dataclass(value):
        return {field.name: _to_data(getattr(value, field.name)) for field in fields(value)}
    if type(value) is tuple:
        return [_to_data(item) for item in value]
    if type(value) is list:
        return [_to_data(item) for item in value]
    if type(value) is dict:
        return {str(key): _to_data(item) for key, item in value.items()}
    return value


def _canonical(value: object) -> bytes:
    return json.dumps(
        _to_data(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha(value: object) -> str:
    return sha256(_canonical(value)).hexdigest()


def _exact_mapping(value: object, keys: frozenset[str]) -> dict[str, object]:
    if type(value) is not dict or frozenset(value) != keys:
        _raise("evidence_schema_invalid")
    return value


def _exact_list(value: object) -> list[object]:
    if type(value) is not list:
        _raise("evidence_schema_invalid")
    return value


def _safe_source_ref(value: object) -> str:
    text = _plain(value, "source_ref")
    if "\\" in text:
        raise ValueError("source_ref")
    path = PurePosixPath(text)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("source_ref")
    return text


def source_identity(source_ref: str, source_digest: str, source_contract_version: str) -> str:
    _safe_source_ref(source_ref)
    _hex64(source_digest, "source_digest")
    _plain(source_contract_version, "source_contract_version")
    payload = source_ref.encode("utf-8") + b"\0" + source_digest.encode("ascii") + b"\0" + source_contract_version.encode("utf-8")
    return sha256(payload).hexdigest()


def _is_sensitive(text: str) -> bool:
    return privacy.contains_forbidden_outbound_text(text)


def _proposition_anchor_labels(proposition: str) -> tuple[str, ...]:
    """Return stable language-neutral content-token anchors in source order."""

    seen: set[str] = set()
    labels: list[str] = []
    for token in re.findall(r"[^\W\d_][^\W_]{2,}", proposition.casefold(), flags=re.UNICODE):
        if token in _PROPOSITION_STOPWORDS or token in seen:
            continue
        seen.add(token)
        labels.append(f"proposition:{token}")
    return tuple(labels)


@dataclass(frozen=True, slots=True)
class SourceSegment:
    segment_id: str
    document_id: str
    byte_start: int
    byte_end: int
    character_start: int
    character_end: int
    exact_text: str
    segment_kind: str
    container_path: str | None
    line_start: int
    line_end: int

    def __post_init__(self) -> None:
        _safe_id(self.segment_id, "segment_id")
        _safe_id(self.document_id, "document_id")
        _plain_int(self.byte_start, "byte_start")
        _plain_int(self.byte_end, "byte_end", minimum=1)
        _plain_int(self.character_start, "character_start")
        _plain_int(self.character_end, "character_end", minimum=1)
        if self.byte_end <= self.byte_start or self.character_end <= self.character_start:
            raise ValueError("segment span")
        _plain(self.exact_text, "exact_text")
        _enum(self.segment_kind, SEGMENT_KINDS, "segment_kind")
        if self.container_path is not None:
            _plain(self.container_path, "container_path", allow_empty=True)
            if self.container_path and not self.container_path.startswith("/"):
                raise ValueError("container_path")
        _plain_int(self.line_start, "line_start", minimum=1)
        _plain_int(self.line_end, "line_end", minimum=1)
        if self.line_end < self.line_start:
            raise ValueError("line span")


@dataclass(frozen=True, slots=True)
class SourceDocument:
    document_id: str
    document_order: int
    media_type: str
    exact_text: str
    exact_utf8_digest: str
    ordered_segments: tuple[SourceSegment, ...]
    parse_error: bool = False

    def __post_init__(self) -> None:
        _safe_id(self.document_id, "document_id")
        _plain_int(self.document_order, "document_order", minimum=1)
        _enum(self.media_type, MEDIA_TYPES, "media_type")
        _plain(self.exact_text, "exact_text", allow_empty=True)
        _hex64(self.exact_utf8_digest, "exact_utf8_digest")
        if sha256(self.exact_text.encode("utf-8")).hexdigest() != self.exact_utf8_digest:
            raise ValueError("document digest")
        _typed_tuple(self.ordered_segments, SourceSegment, "ordered_segments")
        if type(self.parse_error) is not bool:
            raise TypeError("parse_error")
        previous = (-1, -1)
        segment_ids: set[str] = set()
        raw = self.exact_text.encode("utf-8")
        for segment in self.ordered_segments:
            if segment.document_id != self.document_id or segment.segment_id in segment_ids:
                raise ValueError("segment document binding")
            segment_ids.add(segment.segment_id)
            if (segment.character_start, segment.byte_start) < previous:
                raise ValueError("segment order")
            previous = (segment.character_start, segment.byte_start)
            if self.exact_text[segment.character_start:segment.character_end] != segment.exact_text:
                raise ValueError("segment character binding")
            try:
                byte_text = raw[segment.byte_start:segment.byte_end].decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValueError("segment byte binding") from error
            if byte_text != segment.exact_text:
                raise ValueError("segment byte binding")


@dataclass(frozen=True, slots=True)
class SourceDocumentBundle:
    source_identity: str
    source_ref: str
    source_digest: str
    source_contract_version: str
    ordered_documents: tuple[SourceDocument, ...]
    unsupported_file_count: int
    bundle_digest: str

    def __post_init__(self) -> None:
        _hex64(self.source_identity, "source_identity")
        _safe_source_ref(self.source_ref)
        _hex64(self.source_digest, "source_digest")
        _plain(self.source_contract_version, "source_contract_version")
        _typed_tuple(self.ordered_documents, SourceDocument, "ordered_documents")
        _plain_int(self.unsupported_file_count, "unsupported_file_count")
        _hex64(self.bundle_digest, "bundle_digest")
        if self.source_identity != source_identity(self.source_ref, self.source_digest, self.source_contract_version):
            raise ValueError("source identity")
        if tuple(item.document_order for item in self.ordered_documents) != tuple(range(1, len(self.ordered_documents) + 1)):
            raise ValueError("document order")
        if len({item.document_id for item in self.ordered_documents}) != len(self.ordered_documents):
            raise ValueError("document ids")
        payload = {
            "contract_version": SOURCE_DOCUMENT_CONTRACT_VERSION,
            "source_identity": self.source_identity,
            "source_ref": self.source_ref,
            "source_digest": self.source_digest,
            "source_contract_version": self.source_contract_version,
            "ordered_documents": self.ordered_documents,
            "unsupported_file_count": self.unsupported_file_count,
        }
        if self.bundle_digest != _sha(payload):
            raise ValueError("bundle digest")


@dataclass(frozen=True, slots=True)
class CoverageClassification:
    classification: str
    document_count: int
    segment_count: int
    sensitive_segment_count: int
    generic_fallback_candidate: bool

    def __post_init__(self) -> None:
        _enum(self.classification, COVERAGE_CLASSIFICATIONS, "classification")
        _plain_int(self.document_count, "document_count")
        _plain_int(self.segment_count, "segment_count")
        _plain_int(self.sensitive_segment_count, "sensitive_segment_count")
        if type(self.generic_fallback_candidate) is not bool:
            raise TypeError("generic_fallback_candidate")

    def safe_summary(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SourceBlock:
    """Immutable, deterministic group of adjacent source segments."""

    block_id: str
    source_identity: str
    document_id: str
    ordered_segment_ids: tuple[str, ...]
    character_start: int
    character_end: int
    byte_start: int
    byte_end: int
    block_kind: str
    sensitivity_status: str
    block_digest: str

    def __post_init__(self) -> None:
        _safe_id(self.block_id, "block_id")
        _hex64(self.source_identity, "source_identity")
        _safe_id(self.document_id, "document_id")
        _strings(self.ordered_segment_ids, "ordered_segment_ids", allow_empty=False)
        for name in ("character_start", "byte_start"):
            _plain_int(getattr(self, name), name)
        for name in ("character_end", "byte_end"):
            _plain_int(getattr(self, name), name, minimum=1)
        if self.character_end <= self.character_start or self.byte_end <= self.byte_start:
            raise ValueError("block span")
        _enum(self.block_kind, SEGMENT_KINDS, "block_kind")
        _enum(self.sensitivity_status, {"public", "sensitive_withheld"}, "sensitivity_status")
        _hex64(self.block_digest, "block_digest")
        if len(self.ordered_segment_ids) > MAX_BLOCK_SEGMENTS:
            raise ValueError("block segment bound")


@dataclass(frozen=True, slots=True)
class SourceBlockInventory:
    contract_version: str
    source_identity: str
    document_bundle_digest: str
    ordered_blocks: tuple[SourceBlock, ...]
    inventory_digest: str

    def __post_init__(self) -> None:
        if self.contract_version != EVIDENCE_COVERAGE_CONTRACT_VERSION:
            raise ValueError("contract_version")
        _hex64(self.source_identity, "source_identity")
        _hex64(self.document_bundle_digest, "document_bundle_digest")
        _typed_tuple(self.ordered_blocks, SourceBlock, "ordered_blocks")
        _hex64(self.inventory_digest, "inventory_digest")
        ids = tuple(item.block_id for item in self.ordered_blocks)
        if len(ids) != len(set(ids)):
            raise ValueError("block ids")
        payload = {
            "contract_version": self.contract_version,
            "source_identity": self.source_identity,
            "document_bundle_digest": self.document_bundle_digest,
            "ordered_blocks": self.ordered_blocks,
        }
        if self.inventory_digest != _sha(payload):
            raise ValueError("inventory_digest")


@dataclass(frozen=True, slots=True)
class BlockCoverageDecision:
    block_id: str
    disposition: str

    def __post_init__(self) -> None:
        _safe_id(self.block_id, "block_id")
        _enum(self.disposition, BLOCK_DISPOSITIONS, "disposition")


@dataclass(frozen=True, slots=True)
class EvidenceCoveragePlan:
    contract_version: str
    source_identity: str
    document_bundle_digest: str
    inventory_digest: str
    run_id: str
    ordered_decisions: tuple[BlockCoverageDecision, ...]
    plan_digest: str

    def __post_init__(self) -> None:
        if self.contract_version != EVIDENCE_COVERAGE_CONTRACT_VERSION:
            raise ValueError("contract_version")
        for value, name in (
            (self.source_identity, "source_identity"),
            (self.document_bundle_digest, "document_bundle_digest"),
            (self.inventory_digest, "inventory_digest"),
            (self.plan_digest, "plan_digest"),
        ):
            _hex64(value, name)
        _safe_id(self.run_id, "run_id")
        _typed_tuple(self.ordered_decisions, BlockCoverageDecision, "ordered_decisions")
        ids = tuple(item.block_id for item in self.ordered_decisions)
        if len(ids) != len(set(ids)):
            raise ValueError("block decisions")
        payload = {
            "contract_version": self.contract_version,
            "source_identity": self.source_identity,
            "document_bundle_digest": self.document_bundle_digest,
            "inventory_digest": self.inventory_digest,
            "run_id": self.run_id,
            "ordered_decisions": self.ordered_decisions,
        }
        if self.plan_digest != _sha(payload):
            raise ValueError("plan_digest")


@dataclass(frozen=True, slots=True)
class EvidenceCoverageSummary:
    block_count: int
    segment_count: int
    returned_disposition_count: int
    valid_disposition_count: int
    missing_disposition_count: int
    duplicate_disposition_count: int
    conflicting_disposition_count: int
    evidence_candidate_count: int
    context_only_count: int
    structural_count: int
    sensitive_count: int
    ambiguous_count: int
    omitted_count: int
    reason_code: str

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if field.name == "reason_code":
                _enum(value, {
                    "coverage_complete", "coverage_incomplete", "coverage_hard_invalid",
                }, field.name)
            else:
                _plain_int(value, field.name)


@dataclass(frozen=True, slots=True)
class FactRelationValidationSummary:
    """Privacy-safe counts from independent fact and relation validation."""

    returned_fact_count: int
    valid_fact_count: int
    rejected_fact_count: int
    returned_relation_count: int
    verified_relation_count: int
    rejected_relation_count: int
    temporal_conflict_count: int
    causal_conflict_count: int
    polarity_conflict_count: int
    verified_fact_summaries: tuple[str, ...]

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if field.name == "verified_fact_summaries":
                _strings(value, field.name)
                if len(value) > 7 or any(_is_sensitive(item) for item in value):
                    raise ValueError(field.name)
            else:
                _plain_int(value, field.name)
        if (
            self.valid_fact_count + self.rejected_fact_count != self.returned_fact_count
            or self.verified_relation_count + self.rejected_relation_count
            != self.returned_relation_count
            or len(self.verified_fact_summaries) > self.valid_fact_count
        ):
            raise ValueError("fact relation counts")

    def safe_payload(self) -> dict[str, object]:
        return {
            field.name: (
                list(getattr(self, field.name))
                if field.name == "verified_fact_summaries"
                else getattr(self, field.name)
            )
            for field in fields(self)
        }


@dataclass(frozen=True, slots=True)
class EvidenceSelectionReceipt:
    """Privacy-safe accounting for model-selected source spans."""

    returned_selection_count: int
    accepted_code_owned_fact_count: int
    rejected_selection_count: int
    unknown_segment_count: int
    invalid_span_count: int
    duplicate_count: int
    overlap_count: int
    structural_metadata_only_count: int
    sensitive_count: int
    relation_bearing_span_count: int
    too_short_count: int
    too_long_count: int
    verified_relation_count: int = 0

    def __post_init__(self) -> None:
        for field in fields(self):
            _plain_int(getattr(self, field.name), field.name)
        classified = sum((
            self.unknown_segment_count,
            self.invalid_span_count,
            self.duplicate_count,
            self.overlap_count,
            self.structural_metadata_only_count,
            self.sensitive_count,
            self.relation_bearing_span_count,
            self.too_short_count,
            self.too_long_count,
        ))
        if (
            self.accepted_code_owned_fact_count + self.rejected_selection_count
            != self.returned_selection_count
            or classified != self.rejected_selection_count
            or self.verified_relation_count != 0
        ):
            raise ValueError("selection receipt counts")

    def safe_payload(self) -> dict[str, int]:
        return {field.name: getattr(self, field.name) for field in fields(self)}


_ADJUDICATION_VALIDATION_STAGES = frozenset({
    "adjudication_response_type",
    "adjudication_json_parse",
    "adjudication_top_level_schema",
    "adjudication_contract_version",
    "adjudication_source_binding",
    "adjudication_decision_binding",
    "adjudication_semantic_validation",
    "verified_bundle_validation",
})


@dataclass(frozen=True, slots=True)
class AdjudicationValidationDiagnostic:
    """Closed, persisted diagnostic for every post-extraction blocker."""

    category: str
    stable_reason: str
    validation_stage: str
    field_path: str
    response_exact_type: str
    schema_version: str
    source_binding_result: str
    extraction_bundle_binding_result: str
    expected_decision_count: int
    returned_decision_count: int
    supported_decision_count: int
    rejected_decision_count: int
    ambiguous_decision_count: int
    missing_decision_count: int
    duplicate_decision_count: int
    unknown_evidence_id_count: int
    evidence_digest_mismatch_count: int
    transport_attempt_count: int
    response_received: bool

    def __post_init__(self) -> None:
        for value, name in (
            (self.category, "category"),
            (self.stable_reason, "stable_reason"),
            (self.schema_version, "schema_version"),
            (self.source_binding_result, "source_binding_result"),
            (self.extraction_bundle_binding_result, "extraction_bundle_binding_result"),
        ):
            if type(value) is not str or _DIAGNOSTIC_KEY.fullmatch(value) is None:
                raise TypeError(name)
        if self.validation_stage not in _ADJUDICATION_VALIDATION_STAGES:
            raise ValueError("validation_stage")
        if type(self.field_path) is not str or _DIAGNOSTIC_PATH.fullmatch(self.field_path) is None:
            raise TypeError("field_path")
        if self.response_exact_type not in _DIAGNOSTIC_TYPE_LABELS:
            raise ValueError("response_exact_type")
        if self.source_binding_result not in {"matched", "mismatched", "unavailable"}:
            raise ValueError("source_binding_result")
        if self.extraction_bundle_binding_result not in {"matched", "mismatched", "unavailable"}:
            raise ValueError("extraction_bundle_binding_result")
        for field in fields(self):
            if field.name.endswith("_count"):
                _plain_int(getattr(self, field.name), field.name)
        if type(self.response_received) is not bool:
            raise TypeError("response_received")

    def safe_payload(self) -> dict[str, object]:
        return {field.name: getattr(self, field.name) for field in fields(self)}


@dataclass(frozen=True, slots=True)
class CodeOwnedExtractionCheckpoint:
    coverage_plan_digest: str
    extraction: EvidenceExtractionBundle
    selection_receipt: EvidenceSelectionReceipt

    def __post_init__(self) -> None:
        _hex64(self.coverage_plan_digest, "coverage_plan_digest")
        if type(self.extraction) is not EvidenceExtractionBundle:
            raise TypeError("extraction")
        if type(self.selection_receipt) is not EvidenceSelectionReceipt:
            raise TypeError("selection_receipt")


class EvidenceStagePersistenceError(RuntimeError):
    """Signals that durable evidence state could not be written or reread."""


EvidenceStageSink = Callable[[str, object], None]


_COVERAGE_FAILURE_CATEGORIES = frozenset({
    "coverage_incomplete", "coverage_hard_invalid",
})
COVERAGE_FAILURE_REASONS = frozenset({
    "missing_block_disposition", "unexpected_block_disposition",
    "duplicate_block_disposition", "conflicting_block_disposition",
    "ambiguous_coverage", "no_evidence_candidate", "incomplete_segment_partition",
    "malformed_json", "wrong_schema_version", "unknown_or_extra_field",
    "exact_scalar_type_violation", "source_identity_mismatch",
    "privacy_violation", "transport_failure", "unsupported_object_type",
    "invalid_disposition_enum",
    "disposition_partition_mismatch",
    "duplicate_or_missing_segment_disposition",
    "evidence_item_not_bound_to_source_segment",
    "duplicate_or_conflicting_evidence",
    "evidence_count_or_coverage_policy_invalid",
    "unsupported_or_ambiguous_proposition",
    "generic_or_meaning_anchor_rejection",
    "temporal_relation_mismatch", "temporal_relation_operands_incomplete",
    "causal_relation_mismatch", "causal_relation_operands_incomplete",
    "polarity_mismatch", "evidence_references_withheld_segment",
    "independent_fact_count_insufficient",
})

_POST_EXTRACTION_INCOMPLETE_REASONS = frozenset({
    "disposition_partition_mismatch",
    "duplicate_or_missing_segment_disposition",
    "evidence_item_not_bound_to_source_segment",
    "duplicate_or_conflicting_evidence",
    "evidence_count_or_coverage_policy_invalid",
    "unsupported_or_ambiguous_proposition",
    "generic_or_meaning_anchor_rejection",
    "temporal_relation_mismatch",
    "temporal_relation_operands_incomplete",
    "causal_relation_mismatch",
    "causal_relation_operands_incomplete",
    "polarity_mismatch",
    "evidence_references_withheld_segment",
})
_COVERAGE_SOURCE_BINDING_RESULTS = frozenset({
    "matched", "mismatched", "not_checked",
})

_TRANSPORT_FAILURE_CATEGORIES = frozenset({
    "timeout", "dns_or_connect_failure", "tls_failure", "connection_reset",
    "http_bad_request", "http_authentication_failure", "http_permission_denied",
    "http_not_found", "http_rate_limited", "http_server_error",
    "response_read_failure", "response_validation_failure",
    "provider_configuration_invalid", "unknown_transport_failure",
})
_TRANSPORT_TIMEOUT_PHASES = frozenset({
    "connect", "read", "write", "pool", "request", "unknown",
})
_SAFE_PROVIDER_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_TRANSPORT_OPERATION = re.compile(
    r"(?:evidence_coverage|evidence_extraction|evidence_adjudication|generation|adjudication|repair)\Z"
)
_TRANSPORT_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
PROVIDER_TRANSPORT_DIAGNOSTIC_VERSION = "normalizer-provider-transport-diagnostic-v1"


@dataclass(frozen=True, slots=True)
class ProviderTransportDiagnostic:
    """Closed provider diagnostic containing no request or response content."""

    category: str
    operation: str
    model_id: str
    http_status: int | None
    provider_error_code: str | None
    provider_request_id: str | None
    response_received: bool
    timeout_phase: str | None
    transport_attempt_count: int
    contract_version: str = PROVIDER_TRANSPORT_DIAGNOSTIC_VERSION

    def __post_init__(self) -> None:
        _enum(self.category, _TRANSPORT_FAILURE_CATEGORIES, "category")
        if type(self.operation) is not str or _TRANSPORT_OPERATION.fullmatch(self.operation) is None:
            raise TypeError("operation")
        if type(self.model_id) is not str or _TRANSPORT_MODEL.fullmatch(self.model_id) is None:
            raise TypeError("model_id")
        if self.http_status is not None and (
            type(self.http_status) is not int or not 100 <= self.http_status <= 599
        ):
            raise TypeError("http_status")
        for name in ("provider_error_code", "provider_request_id"):
            value = getattr(self, name)
            if value is not None and (
                type(value) is not str or _SAFE_PROVIDER_VALUE.fullmatch(value) is None
            ):
                raise TypeError(name)
        if type(self.response_received) is not bool:
            raise TypeError("response_received")
        if self.timeout_phase is not None:
            _enum(self.timeout_phase, _TRANSPORT_TIMEOUT_PHASES, "timeout_phase")
        if self.category == "timeout" and self.timeout_phase is None:
            raise ValueError("timeout_phase")
        if self.category != "timeout" and self.timeout_phase is not None:
            raise ValueError("timeout_phase")
        if self.http_status is not None and not self.response_received:
            raise ValueError("response_received")
        expected_statuses = {
            "http_bad_request": {400},
            "http_authentication_failure": {401},
            "http_permission_denied": {403},
            "http_not_found": {404},
            "http_rate_limited": {429},
            "http_server_error": set(range(500, 600)),
        }
        if self.category in expected_statuses and self.http_status not in expected_statuses[self.category]:
            raise ValueError("http_status")
        if (
            self.category not in expected_statuses
            and self.category != "unknown_transport_failure"
            and self.http_status is not None
        ):
            raise ValueError("http_status")
        if (
            type(self.transport_attempt_count) is not int
            or isinstance(self.transport_attempt_count, bool)
            or self.transport_attempt_count != 1
        ):
            raise TypeError("transport_attempt_count")
        if (
            type(self.contract_version) is not str
            or self.contract_version != PROVIDER_TRANSPORT_DIAGNOSTIC_VERSION
        ):
            raise ValueError("contract_version")

    def safe_payload(self) -> dict[str, object]:
        return {
            "category": self.category,
            "operation": self.operation,
            "model_id": self.model_id,
            "http_status": self.http_status,
            "provider_error_code": self.provider_error_code,
            "provider_request_id": self.provider_request_id,
            "response_received": self.response_received,
            "timeout_phase": self.timeout_phase,
            "transport_attempt_count": self.transport_attempt_count,
            "contract_version": self.contract_version,
        }


_TRANSPORT_DIAGNOSTIC_KEYS = frozenset({
    "category", "operation", "model_id", "http_status", "provider_error_code",
    "provider_request_id", "response_received", "timeout_phase",
    "transport_attempt_count", "contract_version",
})


def provider_transport_diagnostic_from_payload(
    value: object,
) -> ProviderTransportDiagnostic:
    if type(value) is not dict or frozenset(value) != _TRANSPORT_DIAGNOSTIC_KEYS:
        raise TypeError("provider transport diagnostic")
    try:
        return ProviderTransportDiagnostic(**value)
    except (TypeError, ValueError):
        raise TypeError("provider transport diagnostic") from None


@dataclass(frozen=True, slots=True)
class CoverageFailureEvidence:
    """Closed privacy-safe evidence for one coverage outcome decision."""

    category: str
    validation_stage: str
    stable_reason: str
    summary: EvidenceCoverageSummary
    source_binding_result: str = "not_checked"
    contract_version: str = EVIDENCE_COVERAGE_CONTRACT_VERSION
    transport_diagnostic: ProviderTransportDiagnostic | None = None
    evidence_diagnostic: EvidenceValidationDiagnostic | None = None

    def __post_init__(self) -> None:
        _enum(self.category, _COVERAGE_FAILURE_CATEGORIES, "category")
        if self.validation_stage != "coverage_validation" and self.validation_stage not in _DIAGNOSTIC_STAGES:
            raise ValueError("validation_stage")
        if self.evidence_diagnostic is None:
            _enum(self.stable_reason, COVERAGE_FAILURE_REASONS, "stable_reason")
        elif (
            type(self.stable_reason) is not str
            or _DIAGNOSTIC_KEY.fullmatch(self.stable_reason) is None
        ):
            raise TypeError("stable_reason")
        if type(self.summary) is not EvidenceCoverageSummary:
            raise TypeError("summary")
        _enum(
            self.source_binding_result,
            _COVERAGE_SOURCE_BINDING_RESULTS,
            "source_binding_result",
        )
        if self.contract_version != EVIDENCE_COVERAGE_CONTRACT_VERSION:
            raise ValueError("contract_version")
        if self.summary.reason_code != self.category:
            raise ValueError("summary")
        if self.transport_diagnostic is not None and (
            type(self.transport_diagnostic) is not ProviderTransportDiagnostic
            or self.category != "coverage_hard_invalid"
            or self.stable_reason not in {
                "transport_failure", "unsupported_object_type", "privacy_violation",
            }
        ):
            raise TypeError("transport_diagnostic")
        if self.evidence_diagnostic is not None and (
            type(self.evidence_diagnostic) is not EvidenceValidationDiagnostic
            or self.transport_diagnostic is not None
            or self.validation_stage != self.evidence_diagnostic.validation_stage
            or self.stable_reason != self.evidence_diagnostic.stable_subreason
            or self.source_binding_result
            != {
                "matched": "matched",
                "mismatched": "mismatched",
                "unavailable": "not_checked",
            }[self.evidence_diagnostic.source_identity_binding_result]
        ):
            raise TypeError("evidence_diagnostic")

    def safe_payload(self) -> dict[str, object]:
        """Return the closed, text-free diagnostic persisted by the Normalizer."""

        result = {
            "category": self.category,
            "stable_reason": self.stable_reason,
            "validation_stage": self.validation_stage,
            "contract_version": self.contract_version,
            "source_binding_result": self.source_binding_result,
            "block_count": self.summary.block_count,
            "segment_count": self.summary.segment_count,
            "returned_disposition_count": self.summary.returned_disposition_count,
            "valid_disposition_count": self.summary.valid_disposition_count,
            "missing_disposition_count": self.summary.missing_disposition_count,
            "duplicate_disposition_count": self.summary.duplicate_disposition_count,
            "conflicting_disposition_count": self.summary.conflicting_disposition_count,
            "evidence_candidate_count": self.summary.evidence_candidate_count,
            "context_only_count": self.summary.context_only_count,
            "structural_count": self.summary.structural_count,
            "withheld_count": self.summary.sensitive_count,
            "ambiguous_count": self.summary.ambiguous_count,
            "omitted_count": self.summary.omitted_count,
        }
        if self.transport_diagnostic is not None:
            result["transport_diagnostic"] = self.transport_diagnostic.safe_payload()
        if self.evidence_diagnostic is not None:
            result["evidence_diagnostic"] = self.evidence_diagnostic.safe_payload()
        return result


_COVERAGE_FAILURE_PAYLOAD_KEYS = frozenset({
    "category", "stable_reason", "validation_stage", "contract_version",
    "source_binding_result", "block_count", "segment_count",
    "returned_disposition_count", "valid_disposition_count",
    "missing_disposition_count", "duplicate_disposition_count",
    "conflicting_disposition_count", "evidence_candidate_count",
    "context_only_count", "structural_count", "withheld_count",
    "ambiguous_count", "omitted_count",
    "transport_diagnostic",
    "evidence_diagnostic",
})

_COVERAGE_FAILURE_OPTIONAL_KEYS = frozenset({
    "transport_diagnostic", "evidence_diagnostic",
})


def coverage_failure_from_payload(value: object) -> CoverageFailureEvidence:
    """Recreate one diagnostic only from its exact privacy-safe schema."""

    if (
        type(value) is not dict
        or not (_COVERAGE_FAILURE_PAYLOAD_KEYS - _COVERAGE_FAILURE_OPTIONAL_KEYS)
        <= frozenset(value)
        or frozenset(value) - _COVERAGE_FAILURE_PAYLOAD_KEYS
    ):
        raise TypeError("coverage failure payload")
    category = value["category"]
    try:
        summary = EvidenceCoverageSummary(
            block_count=value["block_count"],
            segment_count=value["segment_count"],
            returned_disposition_count=value["returned_disposition_count"],
            valid_disposition_count=value["valid_disposition_count"],
            missing_disposition_count=value["missing_disposition_count"],
            duplicate_disposition_count=value["duplicate_disposition_count"],
            conflicting_disposition_count=value["conflicting_disposition_count"],
            evidence_candidate_count=value["evidence_candidate_count"],
            context_only_count=value["context_only_count"],
            structural_count=value["structural_count"],
            sensitive_count=value["withheld_count"],
            ambiguous_count=value["ambiguous_count"],
            omitted_count=value["omitted_count"],
            reason_code=category,
        )
        return CoverageFailureEvidence(
            category=category,
            validation_stage=value["validation_stage"],
            stable_reason=value["stable_reason"],
            summary=summary,
            source_binding_result=value["source_binding_result"],
            contract_version=value["contract_version"],
            transport_diagnostic=(
                None
                if value.get("transport_diagnostic") is None
                else provider_transport_diagnostic_from_payload(
                    value["transport_diagnostic"]
                )
            ),
            evidence_diagnostic=(
                None
                if value.get("evidence_diagnostic") is None
                else evidence_validation_diagnostic_from_payload(
                    value["evidence_diagnostic"]
                )
            ),
        )
    except (TypeError, ValueError):
        raise TypeError("coverage failure payload") from None


class CoverageValidationError(EvidenceContractError):
    """Coverage rejection carrying typed evidence rather than exception prose."""

    def __init__(self, evidence: CoverageFailureEvidence):
        if type(evidence) is not CoverageFailureEvidence:
            raise TypeError("coverage failure evidence")
        reason_code = (
            "evidence_coverage_incomplete"
            if evidence.category == "coverage_incomplete"
            else "evidence_source_binding_invalid"
            if evidence.stable_reason == "source_identity_mismatch"
            else "evidence_schema_invalid"
        )
        super().__init__(reason_code)
        self.evidence = evidence


def classify_coverage_failure(
    value: CoverageFailureEvidence,
) -> EvidenceCoverageSummary | None:
    """Map only code-owned decision-partition failures to manual attention."""

    if type(value) is not CoverageFailureEvidence:
        raise TypeError("coverage failure evidence")
    if value.category == "coverage_incomplete":
        return value.summary
    return None


def coverage_hard_failure(
    inventory: SourceBlockInventory,
    *,
    stable_reason: str,
    source_binding_result: str = "not_checked",
    transport_diagnostic: ProviderTransportDiagnostic | None = None,
) -> CoverageFailureEvidence:
    """Create one closed hard-invalid diagnostic without response content."""

    if type(inventory) is not SourceBlockInventory:
        raise TypeError("inventory")
    return CoverageFailureEvidence(
        category="coverage_hard_invalid",
        validation_stage="coverage_validation",
        stable_reason=stable_reason,
        summary=_coverage_summary_from_values(
            inventory,
            (),
            reason_code="coverage_hard_invalid",
            returned=0,
            missing=len(inventory.ordered_blocks),
        ),
        source_binding_result=source_binding_result,
        transport_diagnostic=transport_diagnostic,
    )


def coverage_hard_failure_for_request(
    required_block_count: int,
    *,
    stable_reason: str,
    transport_diagnostic: ProviderTransportDiagnostic | None = None,
) -> CoverageFailureEvidence:
    """Provider-boundary projection using only request-owned safe counts."""

    _plain_int(required_block_count, "required_block_count")
    return CoverageFailureEvidence(
        category="coverage_hard_invalid",
        validation_stage="coverage_validation",
        stable_reason=stable_reason,
        summary=EvidenceCoverageSummary(
            block_count=required_block_count,
            segment_count=0,
            returned_disposition_count=0,
            valid_disposition_count=0,
            missing_disposition_count=required_block_count,
            duplicate_disposition_count=0,
            conflicting_disposition_count=0,
            evidence_candidate_count=0,
            context_only_count=0,
            structural_count=0,
            sensitive_count=0,
            ambiguous_count=0,
            omitted_count=required_block_count,
            reason_code="coverage_hard_invalid",
        ),
        source_binding_result="not_checked",
        transport_diagnostic=transport_diagnostic,
    )


def _char_to_byte_offsets(text: str) -> tuple[int, ...]:
    offsets = [0]
    current = 0
    for character in text:
        current += len(character.encode("utf-8"))
        offsets.append(current)
    return tuple(offsets)


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _infer_media_type(suffix: str, text: str) -> str:
    suffix = suffix.casefold()
    if suffix == ".md":
        return "markdown"
    if suffix == ".log":
        return "log"
    if suffix == ".json":
        return "json"
    if suffix == ".jsonl":
        return "jsonl"
    if suffix in {".yaml", ".yml"}:
        return "key_value"
    lines = tuple(line for line in text.splitlines() if line.strip())
    if lines and sum(bool(_EMAIL_HEADER.match(line)) for line in lines[:12]) >= 2:
        return "email"
    if lines and sum(bool(_CHAT_LINE.match(line)) for line in lines) >= max(2, len(lines) // 2):
        return "chat"
    if lines and sum(bool(_LOG_LINE.match(line)) for line in lines) >= max(2, len(lines) // 2):
        return "log"
    if lines and sum(bool(_KEY_VALUE_LINE.match(line)) for line in lines) >= max(2, len(lines) // 2):
        return "key_value"
    if any(line.lstrip().startswith(("#", "- ", "* ", "> ", "```")) for line in lines):
        return "markdown"
    if re.search(r"[А-Яа-яЁё]", text) and re.search(r"[A-Za-z]", text):
        return "mixed_text"
    return "plain_text"


def _line_segments(document_id: str, text: str, media_type: str) -> tuple[tuple[int, int, str, str, None, int, int], ...]:
    segments: list[tuple[int, int, str, str, None, int, int]] = []
    cursor = 0
    in_code = False
    email_headers = media_type == "email"
    for line_number, raw_line in enumerate(text.splitlines(keepends=True), start=1):
        content = raw_line.rstrip("\r\n")
        start = cursor
        end = start + len(content)
        cursor += len(raw_line)
        if not content.strip():
            if media_type == "email":
                email_headers = False
            continue
        stripped = content.lstrip()
        if media_type == "markdown" and stripped.startswith("```"):
            kind = "code_block"
            in_code = not in_code
        elif media_type == "markdown" and in_code:
            kind = "code_block"
        elif media_type == "markdown" and stripped.startswith("#"):
            kind = "heading"
        elif media_type == "markdown" and stripped.startswith(("- ", "* ", "+ ", "> ")):
            kind = "list_item"
        elif media_type == "log":
            kind = "log_entry"
        elif media_type == "key_value":
            kind = "key_value_entry"
        elif media_type == "chat":
            kind = "chat_message"
        elif media_type == "email":
            kind = "email_header" if email_headers else "email_body"
        elif media_type in {"plain_text", "mixed_text"}:
            kind = "text_line"
        else:
            kind = "unknown_text"
        segments.append((start, end, content, kind, None, line_number, line_number))
    if text and not text.splitlines(keepends=True):
        segments.append((0, len(text), text, "text_line", None, 1, 1))
    return tuple(segments)


class _JsonSpanParser:
    def __init__(self, text: str, *, base: int = 0, path_prefix: str = ""):
        self.text = text
        self.base = base
        self.path_prefix = path_prefix
        self.index = 0
        self.spans: list[tuple[int, int, str]] = []

    def parse(self) -> tuple[tuple[int, int, str], ...]:
        self._skip()
        self._value("")
        self._skip()
        if self.index != len(self.text):
            raise ValueError("trailing json")
        return tuple(self.spans)

    def _skip(self) -> None:
        while self.index < len(self.text) and self.text[self.index] in " \t\r\n":
            self.index += 1

    def _pointer(self, path: str) -> str:
        return f"{self.path_prefix}{path}"

    @staticmethod
    def _escape_pointer(value: str) -> str:
        return value.replace("~", "~0").replace("/", "~1")

    def _string_end(self) -> int:
        if self.index >= len(self.text) or self.text[self.index] != '"':
            raise ValueError("json string")
        cursor = self.index + 1
        while cursor < len(self.text):
            character = self.text[cursor]
            if character == '"':
                return cursor + 1
            if ord(character) < 0x20:
                raise ValueError("json control")
            if character == "\\":
                cursor += 1
                if cursor >= len(self.text) or self.text[cursor] not in '"\\/bfnrtu':
                    raise ValueError("json escape")
                if self.text[cursor] == "u":
                    if cursor + 4 >= len(self.text) or re.fullmatch(r"[0-9A-Fa-f]{4}", self.text[cursor + 1:cursor + 5]) is None:
                        raise ValueError("json unicode")
                    cursor += 4
            cursor += 1
        raise ValueError("unterminated json string")

    def _value(self, path: str) -> None:
        self._skip()
        if self.index >= len(self.text):
            raise ValueError("json value")
        character = self.text[self.index]
        if character == "{":
            self._object(path)
            return
        if character == "[":
            self._array(path)
            return
        start = self.index
        if character == '"':
            self.index = self._string_end()
        else:
            number = re.match(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?", self.text[self.index:])
            if number is not None:
                self.index += number.end()
            else:
                literal = next((item for item in ("true", "false", "null") if self.text.startswith(item, self.index)), None)
                if literal is None:
                    raise ValueError("json scalar")
                self.index += len(literal)
        self.spans.append((self.base + start, self.base + self.index, self._pointer(path)))

    def _object(self, path: str) -> None:
        self.index += 1
        self._skip()
        if self.index < len(self.text) and self.text[self.index] == "}":
            self.index += 1
            return
        while True:
            self._skip()
            start = self.index
            end = self._string_end()
            try:
                key = json.loads(self.text[start:end])
            except json.JSONDecodeError as error:
                raise ValueError("json key") from error
            if type(key) is not str:
                raise ValueError("json key")
            self.index = end
            self._skip()
            if self.index >= len(self.text) or self.text[self.index] != ":":
                raise ValueError("json colon")
            self.index += 1
            child = f"{path}/{self._escape_pointer(key)}"
            self._value(child)
            self._skip()
            if self.index < len(self.text) and self.text[self.index] == ",":
                self.index += 1
                continue
            if self.index < len(self.text) and self.text[self.index] == "}":
                self.index += 1
                return
            raise ValueError("json object")

    def _array(self, path: str) -> None:
        self.index += 1
        self._skip()
        if self.index < len(self.text) and self.text[self.index] == "]":
            self.index += 1
            return
        index = 0
        while True:
            self._value(f"{path}/{index}")
            index += 1
            self._skip()
            if self.index < len(self.text) and self.text[self.index] == ",":
                self.index += 1
                continue
            if self.index < len(self.text) and self.text[self.index] == "]":
                self.index += 1
                return
            raise ValueError("json array")


def _strict_json_loads(text: str) -> object:
    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate json key")
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise ValueError(f"non-finite json constant: {value}")

    try:
        return json.loads(text, object_pairs_hook=reject_duplicate_keys, parse_constant=reject_constant)
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError("invalid json") from error


def _json_spans(text: str, media_type: str) -> tuple[tuple[int, int, str], ...]:
    if media_type == "json":
        _strict_json_loads(text)
        return _JsonSpanParser(text).parse()
    spans: list[tuple[int, int, str]] = []
    cursor = 0
    record_index = 0
    for raw_line in text.splitlines(keepends=True):
        content = raw_line.rstrip("\r\n")
        if content.strip():
            leading = len(content) - len(content.lstrip())
            trailing = len(content.rstrip())
            body = content[leading:trailing]
            _strict_json_loads(body)
            parser = _JsonSpanParser(body, base=cursor + leading, path_prefix=f"/{record_index}")
            spans.extend(parser.parse())
            record_index += 1
        cursor += len(raw_line)
    return tuple(spans)


def _make_document(document_id: str, order: int, media_type: str, text: str) -> SourceDocument:
    offsets = _char_to_byte_offsets(text)
    parse_error = False
    raw_segments: tuple[tuple[int, int, str, str, str | None, int, int], ...]
    if media_type in {"json", "jsonl"}:
        try:
            spans = _json_spans(text, media_type)
            raw_segments = tuple(
                (
                    start,
                    end,
                    text[start:end],
                    "json_scalar",
                    path,
                    _line_number(text, start),
                    _line_number(text, max(start, end - 1)),
                )
                for start, end, path in spans
            )
        except ValueError:
            parse_error = True
            raw_segments = _line_segments(document_id, text, "unknown")
    else:
        raw_segments = _line_segments(document_id, text, media_type)
    segments = tuple(
        SourceSegment(
            segment_id=f"segment-{order:03d}-{index:06d}",
            document_id=document_id,
            byte_start=offsets[start],
            byte_end=offsets[end],
            character_start=start,
            character_end=end,
            exact_text=exact,
            segment_kind=kind,
            container_path=container_path,
            line_start=line_start,
            line_end=line_end,
        )
        for index, (start, end, exact, kind, container_path, line_start, line_end) in enumerate(raw_segments, start=1)
        if exact.strip()
    )
    return SourceDocument(
        document_id=document_id,
        document_order=order,
        media_type=media_type,
        exact_text=text,
        exact_utf8_digest=sha256(text.encode("utf-8")).hexdigest(),
        ordered_segments=segments,
        parse_error=parse_error,
    )


_SOURCE_SUFFIXES = frozenset({".md", ".txt", ".log", ".json", ".jsonl", ".yaml", ".yml"})


def build_source_document_bundle(
    inbox_root: Path,
    source_ref: str,
    source_digest: str,
    source_contract_version: str,
) -> SourceDocumentBundle:
    """Read and segment one source without importing runtime/CP contracts."""

    try:
        root = Path(inbox_root).resolve(strict=True)
        safe_ref = _safe_source_ref(source_ref)
        _hex64(source_digest, "source_digest")
        _plain(source_contract_version, "source_contract_version")
        source_path = root.joinpath(*PurePosixPath(safe_ref).parts).resolve(strict=True)
        source_path.relative_to(root)
        cursor = root
        for part in source_path.relative_to(root).parts:
            cursor = cursor / part
            if cursor.is_symlink():
                _raise("evidence_source_invalid")
        if not source_path.is_dir():
            _raise("evidence_source_invalid")
        files = sorted(source_path.rglob("*"), key=lambda item: item.relative_to(source_path).as_posix())
        documents: list[SourceDocument] = []
        unsupported = 0
        total_bytes = 0
        for path in files:
            if path.is_symlink():
                _raise("evidence_source_invalid")
            if not path.is_file() or path.name == "narrative_ready.json":
                continue
            if path.suffix.casefold() not in _SOURCE_SUFFIXES:
                unsupported += 1
                continue
            raw = path.read_bytes()
            if len(raw) > MAX_DOCUMENT_BYTES:
                _raise("evidence_source_too_large")
            total_bytes += len(raw)
            if total_bytes > MAX_SOURCE_BYTES or len(documents) >= MAX_DOCUMENTS:
                _raise("evidence_source_too_large")
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                unsupported += 1
                continue
            order = len(documents) + 1
            media_type = _infer_media_type(path.suffix, text)
            documents.append(_make_document(f"document-{order:03d}", order, media_type, text))
        segment_count = sum(len(item.ordered_segments) for item in documents)
        if segment_count > MAX_SEGMENTS:
            _raise("evidence_source_too_large")
        identity = source_identity(safe_ref, source_digest, source_contract_version)
        payload = {
            "contract_version": SOURCE_DOCUMENT_CONTRACT_VERSION,
            "source_identity": identity,
            "source_ref": safe_ref,
            "source_digest": source_digest,
            "source_contract_version": source_contract_version,
            "ordered_documents": tuple(documents),
            "unsupported_file_count": unsupported,
        }
        return SourceDocumentBundle(
            source_identity=identity,
            source_ref=safe_ref,
            source_digest=source_digest,
            source_contract_version=source_contract_version,
            ordered_documents=tuple(documents),
            unsupported_file_count=unsupported,
            bundle_digest=_sha(payload),
        )
    except EvidenceContractError:
        raise
    except (OSError, ValueError, TypeError):
        _raise("evidence_source_invalid")


def _segments(bundle: SourceDocumentBundle) -> tuple[SourceSegment, ...]:
    return tuple(segment for document in bundle.ordered_documents for segment in document.ordered_segments)


def classify_source_bundle(
    bundle: SourceDocumentBundle,
    *,
    deterministic_fast_path: bool = False,
) -> CoverageClassification:
    if type(bundle) is not SourceDocumentBundle:
        raise TypeError("bundle")
    segments = _segments(bundle)
    sensitive_count = sum(_is_sensitive(item.exact_text) for item in segments)
    # A readable companion file cannot prove that an unreadable/binary member
    # is irrelevant.  Silently extracting only the readable subset would make
    # source coverage incomplete, so any unsupported member requires human
    # attention before a model boundary is crossed.
    if bundle.unsupported_file_count:
        classification = "unsupported_binary_container"
    elif any(item.parse_error for item in bundle.ordered_documents):
        classification = "parse_error"
    elif not segments:
        classification = "insufficient"
    elif sensitive_count == len(segments):
        classification = "sensitive"
    elif deterministic_fast_path:
        classification = "known_deterministic_grammar"
    else:
        media = {item.media_type for item in bundle.ordered_documents}
        if media & {"json", "jsonl", "key_value"}:
            classification = "json_like"
        elif "log" in media:
            classification = "log_like"
        elif "markdown" in media:
            classification = "markdown_like"
        elif media & {"chat", "email"}:
            classification = "chat_email_like"
        else:
            classification = "unknown_but_text_readable"
    generic = classification in {
        "unknown_but_text_readable",
        "json_like",
        "log_like",
        "markdown_like",
        "chat_email_like",
    }
    return CoverageClassification(
        classification,
        len(bundle.ordered_documents),
        len(segments),
        sensitive_count,
        generic,
    )


def build_source_block_inventory(bundle: SourceDocumentBundle) -> SourceBlockInventory:
    """Build stable bounded blocks without model judgement or filesystem state."""

    if type(bundle) is not SourceDocumentBundle:
        raise TypeError("bundle")
    blocks: list[SourceBlock] = []
    for document in bundle.ordered_documents:
        pending: list[SourceSegment] = []

        def flush() -> None:
            if not pending:
                return
            index = len(blocks) + 1
            sensitive = _is_sensitive(pending[0].exact_text)
            content_binding = {
                "source_identity": bundle.source_identity,
                "document_id": document.document_id,
                "ordered_segments": tuple(
                    (item.segment_id, item.exact_text, item.byte_start, item.byte_end)
                    for item in pending
                ),
                "block_kind": pending[0].segment_kind,
                "sensitivity_status": "sensitive_withheld" if sensitive else "public",
            }
            digest = _sha(content_binding)
            blocks.append(SourceBlock(
                block_id=f"block-{index:05d}-{digest[:16]}",
                source_identity=bundle.source_identity,
                document_id=document.document_id,
                ordered_segment_ids=tuple(item.segment_id for item in pending),
                character_start=pending[0].character_start,
                character_end=pending[-1].character_end,
                byte_start=pending[0].byte_start,
                byte_end=pending[-1].byte_end,
                block_kind=pending[0].segment_kind,
                sensitivity_status="sensitive_withheld" if sensitive else "public",
                block_digest=digest,
            ))
            pending.clear()

        for segment in document.ordered_segments:
            if pending:
                previous = pending[-1]
                gap = document.exact_text[previous.character_end:segment.character_start]
                projected_characters = segment.character_end - pending[0].character_start
                boundary = (
                    segment.segment_kind != pending[0].segment_kind
                    or segment.container_path != pending[0].container_path
                    or _is_sensitive(segment.exact_text) != _is_sensitive(pending[0].exact_text)
                    or segment.segment_kind in {"heading", "list_item"}
                    or "\n\n" in gap.replace("\r\n", "\n")
                    or len(pending) >= MAX_BLOCK_SEGMENTS
                    or projected_characters > MAX_BLOCK_CHARACTERS
                )
                if boundary:
                    flush()
            pending.append(segment)
        flush()
    payload = {
        "contract_version": EVIDENCE_COVERAGE_CONTRACT_VERSION,
        "source_identity": bundle.source_identity,
        "document_bundle_digest": bundle.bundle_digest,
        "ordered_blocks": tuple(blocks),
    }
    return SourceBlockInventory(
        contract_version=EVIDENCE_COVERAGE_CONTRACT_VERSION,
        source_identity=bundle.source_identity,
        document_bundle_digest=bundle.bundle_digest,
        ordered_blocks=tuple(blocks),
        inventory_digest=_sha(payload),
    )


def validate_source_block_partition(
    bundle: SourceDocumentBundle,
    inventory: SourceBlockInventory,
) -> CoverageFailureEvidence | None:
    """Prove that every source segment belongs to exactly one ordered block."""

    if type(bundle) is not SourceDocumentBundle or type(inventory) is not SourceBlockInventory:
        raise TypeError("source block partition")
    expected = tuple(item.segment_id for item in _segments(bundle))
    actual = tuple(
        segment_id
        for block in inventory.ordered_blocks
        for segment_id in block.ordered_segment_ids
    )
    if actual == expected and len(actual) == len(set(actual)):
        return None
    valid = len(set(actual) & set(expected))
    missing = len(set(expected) - set(actual))
    duplicate = max(0, len(actual) - len(set(actual)))
    conflicting = len(set(actual) - set(expected))
    return CoverageFailureEvidence(
        category="coverage_incomplete",
        validation_stage="coverage_validation",
        stable_reason="incomplete_segment_partition",
        summary=EvidenceCoverageSummary(
            block_count=len(inventory.ordered_blocks),
            segment_count=len(expected),
            returned_disposition_count=0,
            valid_disposition_count=valid,
            missing_disposition_count=missing,
            duplicate_disposition_count=duplicate,
            conflicting_disposition_count=conflicting,
            evidence_candidate_count=0,
            context_only_count=0,
            structural_count=0,
            sensitive_count=0,
            ambiguous_count=0,
            omitted_count=max(0, len(inventory.ordered_blocks)),
            reason_code="coverage_incomplete",
        ),
        source_binding_result="matched",
    )


@dataclass(frozen=True, slots=True)
class EvidenceQuote:
    quote_id: str
    document_id: str
    segment_id: str
    byte_start: int
    byte_end: int
    character_start: int
    character_end: int
    exact_text: str

    def __post_init__(self) -> None:
        _safe_id(self.quote_id, "quote_id")
        _safe_id(self.document_id, "document_id")
        _safe_id(self.segment_id, "segment_id")
        _plain_int(self.byte_start, "byte_start")
        _plain_int(self.byte_end, "byte_end", minimum=1)
        _plain_int(self.character_start, "character_start")
        _plain_int(self.character_end, "character_end", minimum=1)
        if self.byte_end <= self.byte_start or self.character_end <= self.character_start:
            raise ValueError("quote span")
        _plain(self.exact_text, "exact_text")


@dataclass(frozen=True, slots=True)
class EvidenceAtom:
    atom_id: str
    atom_kind: str
    quote_id: str
    exact_lexeme: str

    def __post_init__(self) -> None:
        _safe_id(self.atom_id, "atom_id")
        _enum(self.atom_kind, ATOM_KINDS, "atom_kind")
        _safe_id(self.quote_id, "quote_id")
        _plain(self.exact_lexeme, "exact_lexeme")


@dataclass(frozen=True, slots=True)
class EvidenceRelation:
    relation_kind: str
    marker_quote_id: str
    left_operand_quote_ids: tuple[str, ...]
    right_operand_quote_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.relation_kind not in TEMPORAL_RELATIONS | CAUSAL_RELATIONS:
            raise ValueError("relation_kind")
        _safe_id(self.marker_quote_id, "marker_quote_id")
        _strings(self.left_operand_quote_ids, "left_operand_quote_ids", allow_empty=False)
        _strings(self.right_operand_quote_ids, "right_operand_quote_ids", allow_empty=False)
        if set(self.left_operand_quote_ids) & set(self.right_operand_quote_ids):
            raise ValueError("relation operands")


@dataclass(frozen=True, slots=True)
class SourceEvidence:
    evidence_id: str
    proposition: str
    evidence_kind: str
    ordered_segment_refs: tuple[str, ...]
    exact_quotes: tuple[EvidenceQuote, ...]
    entities: tuple[EvidenceAtom, ...]
    numbers: tuple[EvidenceAtom, ...]
    dates: tuple[EvidenceAtom, ...]
    polarity: str
    temporal_relation: EvidenceRelation | None
    causal_relation: EvidenceRelation | None
    uncertainty: str
    public_safety: str

    def __post_init__(self) -> None:
        _safe_id(self.evidence_id, "evidence_id")
        _plain(self.proposition, "proposition")
        _enum(self.evidence_kind, EVIDENCE_KINDS, "evidence_kind")
        _strings(self.ordered_segment_refs, "ordered_segment_refs")
        _typed_tuple(self.exact_quotes, EvidenceQuote, "exact_quotes")
        for name in ("entities", "numbers", "dates"):
            _typed_tuple(getattr(self, name), EvidenceAtom, name)
        _enum(self.polarity, POLARITIES, "polarity")
        _enum(self.uncertainty, UNCERTAINTIES, "uncertainty")
        _enum(self.public_safety, PUBLIC_SAFETY, "public_safety")
        if self.temporal_relation is not None:
            if type(self.temporal_relation) is not EvidenceRelation or self.temporal_relation.relation_kind not in TEMPORAL_RELATIONS:
                raise TypeError("temporal_relation")
        if self.causal_relation is not None:
            if type(self.causal_relation) is not EvidenceRelation or self.causal_relation.relation_kind not in CAUSAL_RELATIONS:
                raise TypeError("causal_relation")
        if self.evidence_kind == "insufficient_or_ambiguous":
            if self.exact_quotes or self.entities or self.numbers or self.dates or self.uncertainty != "ambiguous":
                raise ValueError("insufficient evidence")
        elif not self.exact_quotes or not self.ordered_segment_refs:
            raise ValueError("evidence quotes")


@dataclass(frozen=True, slots=True)
class SegmentDisposition:
    segment_id: str
    disposition: str
    ordered_evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _safe_id(self.segment_id, "segment_id")
        _enum(self.disposition, SEGMENT_DISPOSITIONS, "disposition")
        _strings(self.ordered_evidence_ids, "ordered_evidence_ids")
        if (self.disposition == "evidence") != bool(self.ordered_evidence_ids):
            raise ValueError("segment disposition")


@dataclass(frozen=True, slots=True)
class EvidenceExtractionBundle:
    source_identity: str
    document_bundle_digest: str
    contract_version: str
    run_id: str
    ordered_evidence: tuple[SourceEvidence, ...]
    ordered_segment_dispositions: tuple[SegmentDisposition, ...]
    bundle_digest: str

    def __post_init__(self) -> None:
        _hex64(self.source_identity, "source_identity")
        _hex64(self.document_bundle_digest, "document_bundle_digest")
        if self.contract_version != EVIDENCE_EXTRACTION_CONTRACT_VERSION:
            raise ValueError("contract_version")
        _safe_id(self.run_id, "run_id")
        _typed_tuple(self.ordered_evidence, SourceEvidence, "ordered_evidence")
        _typed_tuple(self.ordered_segment_dispositions, SegmentDisposition, "ordered_segment_dispositions")
        _hex64(self.bundle_digest, "bundle_digest")
        payload = {
            "source_identity": self.source_identity,
            "document_bundle_digest": self.document_bundle_digest,
            "contract_version": self.contract_version,
            "run_id": self.run_id,
            "ordered_evidence": self.ordered_evidence,
            "ordered_segment_dispositions": self.ordered_segment_dispositions,
        }
        if self.bundle_digest != _sha(payload):
            raise ValueError("extraction digest")


@dataclass(frozen=True, slots=True)
class EvidenceDecision:
    evidence_id: str
    evidence_digest: str
    decision: str
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        _safe_id(self.evidence_id, "evidence_id")
        _hex64(self.evidence_digest, "evidence_digest")
        _enum(self.decision, EVIDENCE_DECISIONS, "decision")
        _strings(self.reason_codes, "reason_codes")
        if any(item not in ADJUDICATION_REASON_CODES for item in self.reason_codes):
            raise ValueError("reason_codes")
        if (self.decision == "supported") == bool(self.reason_codes):
            raise ValueError("decision reasons")


@dataclass(frozen=True, slots=True)
class EvidenceAdjudicationBundle:
    source_identity: str
    extraction_bundle_digest: str
    contract_version: str
    run_id: str
    ordered_decisions: tuple[EvidenceDecision, ...]
    bundle_digest: str

    def __post_init__(self) -> None:
        _hex64(self.source_identity, "source_identity")
        _hex64(self.extraction_bundle_digest, "extraction_bundle_digest")
        if self.contract_version != EVIDENCE_ADJUDICATION_CONTRACT_VERSION:
            raise ValueError("contract_version")
        _safe_id(self.run_id, "run_id")
        _typed_tuple(self.ordered_decisions, EvidenceDecision, "ordered_decisions")
        _hex64(self.bundle_digest, "bundle_digest")
        payload = {
            "source_identity": self.source_identity,
            "extraction_bundle_digest": self.extraction_bundle_digest,
            "contract_version": self.contract_version,
            "run_id": self.run_id,
            "ordered_decisions": self.ordered_decisions,
        }
        if self.bundle_digest != _sha(payload):
            raise ValueError("adjudication digest")


@dataclass(frozen=True, slots=True)
class VerifiedEvidenceBundle:
    contract_version: str
    source_identity: str
    document_bundle_digest: str
    extraction: EvidenceExtractionBundle
    adjudication: EvidenceAdjudicationBundle
    accepted_evidence_ids: tuple[str, ...]
    verified_bundle_digest: str

    def __post_init__(self) -> None:
        if self.contract_version != VERIFIED_EVIDENCE_CONTRACT_VERSION:
            raise ValueError("contract_version")
        _hex64(self.source_identity, "source_identity")
        _hex64(self.document_bundle_digest, "document_bundle_digest")
        if type(self.extraction) is not EvidenceExtractionBundle or type(self.adjudication) is not EvidenceAdjudicationBundle:
            raise TypeError("verified evidence")
        _strings(self.accepted_evidence_ids, "accepted_evidence_ids", allow_empty=False)
        _hex64(self.verified_bundle_digest, "verified_bundle_digest")
        payload = {
            "contract_version": self.contract_version,
            "source_identity": self.source_identity,
            "document_bundle_digest": self.document_bundle_digest,
            "extraction_bundle_digest": self.extraction.bundle_digest,
            "adjudication_bundle_digest": self.adjudication.bundle_digest,
            "accepted_evidence_ids": self.accepted_evidence_ids,
        }
        if self.verified_bundle_digest != _sha(payload):
            raise ValueError("verified digest")


@dataclass(frozen=True, slots=True)
class VerifiedFactBinding:
    binding_version: str
    fact_id: str
    evidence_id: str
    evidence_digest: str
    exact_supporting_quote: str
    public_proposition: str
    ordered_segment_refs: tuple[str, ...]
    source_identity: str
    order: int
    numbers: tuple[str, ...]
    entities: tuple[str, ...]
    dates: tuple[str, ...]
    polarity: str
    temporal_relation: str | None
    causal_relation: str | None
    uncertainty: str
    adjudication_identity: str
    meaning_anchor_ids: tuple[str, ...]
    public_anchor_labels: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.binding_version != VERIFIED_FACT_BINDING_VERSION:
            raise ValueError("binding_version")
        _safe_id(self.fact_id, "fact_id")
        _safe_id(self.evidence_id, "evidence_id")
        _hex64(self.evidence_digest, "evidence_digest")
        _plain(self.exact_supporting_quote, "exact_supporting_quote")
        _plain(self.public_proposition, "public_proposition")
        _strings(self.ordered_segment_refs, "ordered_segment_refs", allow_empty=False)
        _hex64(self.source_identity, "source_identity")
        _plain_int(self.order, "order", minimum=1)
        for name in ("numbers", "entities", "dates"):
            _strings(getattr(self, name), name)
        _enum(self.polarity, POLARITIES, "polarity")
        if self.temporal_relation is not None and self.temporal_relation not in TEMPORAL_RELATIONS:
            raise ValueError("temporal_relation")
        if self.causal_relation is not None and self.causal_relation not in CAUSAL_RELATIONS:
            raise ValueError("causal_relation")
        _enum(self.uncertainty, UNCERTAINTIES, "uncertainty")
        _hex64(self.adjudication_identity, "adjudication_identity")
        _strings(self.meaning_anchor_ids, "meaning_anchor_ids", allow_empty=False)
        _strings(self.public_anchor_labels, "public_anchor_labels", allow_empty=False)
        if len(self.meaning_anchor_ids) != len(self.public_anchor_labels):
            raise ValueError("meaning anchors")
        expected_anchor_ids = tuple(
            f"meaning-{index:03d}-{sha256(label.encode('utf-8')).hexdigest()[:16]}"
            for index, label in enumerate(self.public_anchor_labels, start=1)
        )
        if self.meaning_anchor_ids != expected_anchor_ids:
            raise ValueError("meaning anchors")


@dataclass(frozen=True, slots=True)
class EvidenceModelRequest:
    request_kind: str
    model: str
    payload_json: str
    response_schema_version: str
    required_block_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.request_kind not in {"evidence_coverage", "evidence_extraction", "evidence_adjudication"}:
            raise ValueError("request_kind")
        _plain(self.model, "model")
        _plain(self.payload_json, "payload_json")
        _plain(self.response_schema_version, "response_schema_version")
        _strings(self.required_block_ids, "required_block_ids")
        if len(self.required_block_ids) != len(set(self.required_block_ids)):
            raise ValueError("required_block_ids")
        if self.request_kind == "evidence_coverage":
            if (
                self.response_schema_version != EVIDENCE_COVERAGE_CONTRACT_VERSION
                or not self.required_block_ids
            ):
                raise ValueError("required_block_ids")
        elif self.request_kind != "evidence_extraction" and self.required_block_ids:
            raise ValueError("required_block_ids")


class EvidenceModelClient(Protocol):
    def generate_json(
        self, request: EvidenceModelRequest,
    ) -> Mapping[str, object] | str | CoverageFailureEvidence:
        """Return one strict extraction or adjudication response."""


@dataclass(frozen=True, slots=True)
class EvidenceResolution:
    status: str
    verified_bundle: VerifiedEvidenceBundle | None
    model_call_count: int
    reason_code: str
    diagnostic: EvidenceValidationDiagnostic | AdjudicationValidationDiagnostic | None = None
    coverage_summary: EvidenceCoverageSummary | None = None
    coverage_failure: CoverageFailureEvidence | None = None
    fact_relation_summary: FactRelationValidationSummary | None = None
    selection_receipt: EvidenceSelectionReceipt | None = None

    def __post_init__(self) -> None:
        _enum(self.status, RESOLUTION_STATUSES, "status")
        _plain_int(self.model_call_count, "model_call_count")
        if self.model_call_count > 3:
            raise ValueError("model_call_count")
        if type(self.reason_code) is not str or self.reason_code not in REASON_CODES:
            raise ValueError("reason_code")
        if (self.status == "verified") != (type(self.verified_bundle) is VerifiedEvidenceBundle):
            raise ValueError("verified bundle")
        expected_reason = {
            "verified": "evidence_verified",
            "source_insufficient": "evidence_source_insufficient",
            "manual_attention": "evidence_manual_attention",
            "sensitive_rejected": "evidence_sensitive",
        }.get(self.status)
        if expected_reason is not None and self.reason_code != expected_reason:
            raise ValueError("reason_code")
        if self.diagnostic is not None and type(self.diagnostic) not in {
            EvidenceValidationDiagnostic,
            AdjudicationValidationDiagnostic,
        }:
            raise TypeError("diagnostic")
        if self.status != "failed" and self.diagnostic is not None:
            raise ValueError("diagnostic")
        if self.coverage_summary is not None and type(self.coverage_summary) is not EvidenceCoverageSummary:
            raise TypeError("coverage_summary")
        if self.coverage_failure is not None and type(self.coverage_failure) is not CoverageFailureEvidence:
            raise TypeError("coverage_failure")
        if (
            self.fact_relation_summary is not None
            and type(self.fact_relation_summary) is not FactRelationValidationSummary
        ):
            raise TypeError("fact_relation_summary")
        if (
            self.selection_receipt is not None
            and type(self.selection_receipt) is not EvidenceSelectionReceipt
        ):
            raise TypeError("selection_receipt")
        if (
            self.coverage_failure is not None
            and self.coverage_summary is not None
            and self.coverage_failure.summary != self.coverage_summary
        ):
            raise ValueError("coverage_failure")


_QUOTE_KEYS = frozenset({
    "quote_id", "document_id", "segment_id", "byte_start", "byte_end",
    "character_start", "character_end", "exact_text",
})
_ATOM_KEYS = frozenset({"atom_id", "atom_kind", "quote_id", "exact_lexeme"})
_RELATION_KEYS = frozenset({
    "relation_kind", "marker_quote_id", "left_operand_quote_ids", "right_operand_quote_ids",
})
_EVIDENCE_KEYS = frozenset({
    "evidence_id", "proposition", "evidence_kind", "ordered_segment_refs", "exact_quotes",
    "entities", "numbers", "dates", "polarity", "temporal_relation", "causal_relation",
    "uncertainty", "public_safety",
})
_DISPOSITION_KEYS = frozenset({"segment_id", "disposition", "ordered_evidence_ids"})
_EXTRACTION_RESPONSE_KEYS = frozenset({
    "schema_version", "source_identity", "document_bundle_digest", "run_id", "evidence",
    "segment_dispositions",
})
_COVERAGE_RESPONSE_KEYS = frozenset({
    "schema_version", "source_identity", "document_bundle_digest", "inventory_digest",
    "run_id", "block_dispositions",
})
_EXTRACTION_V2_RESPONSE_KEYS = frozenset({
    "schema_version", "source_identity", "document_bundle_digest", "coverage_plan_digest",
    "run_id", "evidence",
})
_V3_FACT_KEYS = frozenset({
    "fact_id", "proposition", "evidence_kind", "ordered_block_refs",
    "ordered_segment_refs", "exact_quotes", "entities", "numbers", "dates",
    "polarity", "uncertainty", "public_safety",
})
_V3_RELATION_KEYS = frozenset({
    "relation_id", "relation_kind", "left_fact_id", "right_fact_id",
    "support_quote",
})
_EXTRACTION_V3_RESPONSE_KEYS = frozenset({
    "schema_version", "source_identity", "document_bundle_digest",
    "coverage_plan_digest", "run_id", "facts", "relations",
})
_SPAN_SELECTION_KEYS = frozenset({
    "selection_id", "segment_id", "character_start", "character_end",
})
_SPAN_SELECTION_RESPONSE_KEYS = frozenset({
    "schema_version", "source_identity", "document_bundle_digest",
    "coverage_plan_digest", "run_id", "selections",
})
_DECISION_KEYS = frozenset({"evidence_id", "evidence_digest", "decision", "reason_codes"})
_ADJUDICATION_RESPONSE_KEYS = frozenset({
    "schema_version", "source_identity", "extraction_bundle_digest", "run_id", "decisions",
})


def _closed_object_schema(properties: dict[str, object]) -> dict[str, object]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def evidence_model_response_schema(
    request_kind: str,
    response_schema_version: str,
    required_block_ids: tuple[str, ...] = (),
) -> dict[str, object]:
    """Return the code-owned strict provider schema for one evidence operation."""

    if type(request_kind) is not str or type(response_schema_version) is not str:
        raise TypeError("evidence response schema")
    safe_id = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"}
    hex64 = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    string_array = {"type": "array", "items": {"type": "string"}}
    if request_kind == "evidence_coverage":
        if (
            response_schema_version != EVIDENCE_COVERAGE_CONTRACT_VERSION
            or type(required_block_ids) is not tuple
            or not required_block_ids
            or len(required_block_ids) != len(set(required_block_ids))
            or any(type(item) is not str or _SAFE_ID.fullmatch(item) is None for item in required_block_ids)
        ):
            raise ValueError("evidence response schema")
        disposition_map = {
            "type": "object",
            "properties": {
                item: {"type": "string", "enum": sorted(BLOCK_DISPOSITIONS)}
                for item in required_block_ids
            },
            "required": list(required_block_ids),
            "additionalProperties": False,
        }
        return _closed_object_schema({
            "schema_version": {"type": "string", "const": EVIDENCE_COVERAGE_CONTRACT_VERSION},
            "source_identity": dict(hex64),
            "document_bundle_digest": dict(hex64),
            "inventory_digest": dict(hex64),
            "run_id": dict(safe_id),
            "block_dispositions": disposition_map,
        })
    if request_kind == "evidence_extraction":
        if response_schema_version not in {
            EVIDENCE_EXTRACTION_CONTRACT_VERSION,
            EVIDENCE_EXTRACTION_V2_CONTRACT_VERSION,
            EVIDENCE_EXTRACTION_V3_CONTRACT_VERSION,
            EVIDENCE_SPAN_SELECTION_CONTRACT_VERSION,
        }:
            raise ValueError("evidence response schema")
        if response_schema_version == EVIDENCE_SPAN_SELECTION_CONTRACT_VERSION:
            if (
                type(required_block_ids) is not tuple
                or not required_block_ids
                or len(required_block_ids) != len(set(required_block_ids))
                or any(
                    type(item) is not str or _SAFE_ID.fullmatch(item) is None
                    for item in required_block_ids
                )
            ):
                raise ValueError("evidence response schema")
            selection = _closed_object_schema({
                "selection_id": dict(safe_id),
                "segment_id": {
                    "type": "string", "enum": list(required_block_ids),
                },
                "character_start": {"type": "integer", "minimum": 0},
                "character_end": {"type": "integer", "minimum": 1},
            })
            return _closed_object_schema({
                "schema_version": {
                    "type": "string",
                    "const": EVIDENCE_SPAN_SELECTION_CONTRACT_VERSION,
                },
                "source_identity": dict(hex64),
                "document_bundle_digest": dict(hex64),
                "coverage_plan_digest": dict(hex64),
                "run_id": dict(safe_id),
                "selections": {"type": "array", "items": selection},
            })
        quote = _closed_object_schema({
            "quote_id": dict(safe_id),
            "document_id": dict(safe_id),
            "segment_id": dict(safe_id),
            "byte_start": {"type": "integer", "minimum": 0},
            "byte_end": {"type": "integer", "minimum": 0},
            "character_start": {"type": "integer", "minimum": 0},
            "character_end": {"type": "integer", "minimum": 0},
            "exact_text": {"type": "string"},
        })
        atom = _closed_object_schema({
            "atom_id": dict(safe_id),
            "atom_kind": {"type": "string", "enum": sorted(ATOM_KINDS)},
            "quote_id": dict(safe_id),
            "exact_lexeme": {"type": "string"},
        })
        relation_operands = {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
        }
        relation = _closed_object_schema({
            "relation_kind": {
                "type": "string",
                "enum": sorted(TEMPORAL_RELATIONS | CAUSAL_RELATIONS),
            },
            "marker_quote_id": dict(safe_id),
            "left_operand_quote_ids": dict(relation_operands),
            "right_operand_quote_ids": dict(relation_operands),
        })
        evidence_item = _closed_object_schema({
            "evidence_id": dict(safe_id),
            "proposition": {"type": "string"},
            "evidence_kind": {"type": "string", "enum": sorted(EVIDENCE_KINDS)},
            "ordered_segment_refs": dict(string_array),
            "exact_quotes": {"type": "array", "items": quote},
            "entities": {"type": "array", "items": atom},
            "numbers": {"type": "array", "items": atom},
            "dates": {"type": "array", "items": atom},
            "polarity": {"type": "string", "enum": sorted(POLARITIES)},
            "temporal_relation": {"anyOf": [relation, {"type": "null"}]},
            "causal_relation": {"anyOf": [relation, {"type": "null"}]},
            "uncertainty": {"type": "string", "enum": sorted(UNCERTAINTIES)},
            "public_safety": {"type": "string", "enum": sorted(PUBLIC_SAFETY)},
        })
        if response_schema_version == EVIDENCE_EXTRACTION_V3_CONTRACT_VERSION:
            if (
                type(required_block_ids) is not tuple
                or not required_block_ids
                or len(required_block_ids) != len(set(required_block_ids))
            ):
                raise ValueError("evidence response schema")
            fact = _closed_object_schema({
                "fact_id": dict(safe_id),
                "proposition": {"type": "string"},
                "evidence_kind": {
                    "type": "string", "enum": sorted(ATOMIC_EVIDENCE_KINDS),
                },
                "ordered_block_refs": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(required_block_ids)},
                    "minItems": 1,
                },
                "ordered_segment_refs": dict(string_array),
                "exact_quotes": {"type": "array", "items": quote, "minItems": 1},
                "entities": {"type": "array", "items": atom},
                "numbers": {"type": "array", "items": atom},
                "dates": {"type": "array", "items": atom},
                "polarity": {"type": "string", "enum": sorted(POLARITIES)},
                "uncertainty": {"type": "string", "enum": sorted(UNCERTAINTIES)},
                "public_safety": {"type": "string", "enum": sorted(PUBLIC_SAFETY)},
            })
            relation_v3 = _closed_object_schema({
                "relation_id": dict(safe_id),
                "relation_kind": {"type": "string", "enum": sorted(FACT_RELATION_KINDS)},
                "left_fact_id": dict(safe_id),
                "right_fact_id": dict(safe_id),
                "support_quote": quote,
            })
            return _closed_object_schema({
                "schema_version": {
                    "type": "string", "const": EVIDENCE_EXTRACTION_V3_CONTRACT_VERSION,
                },
                "source_identity": dict(hex64),
                "document_bundle_digest": dict(hex64),
                "coverage_plan_digest": dict(hex64),
                "run_id": dict(safe_id),
                "facts": {"type": "array", "items": fact},
                "relations": {"type": "array", "items": relation_v3},
            })
        if response_schema_version == EVIDENCE_EXTRACTION_V2_CONTRACT_VERSION:
            if (
                type(required_block_ids) is not tuple
                or not required_block_ids
                or len(required_block_ids) != len(set(required_block_ids))
            ):
                raise ValueError("evidence response schema")
            evidence_item["properties"]["ordered_block_refs"] = {
                "type": "array",
                "items": {"type": "string", "enum": list(required_block_ids)},
                "minItems": 1,
            }
            evidence_item["required"].append("ordered_block_refs")
            return _closed_object_schema({
                "schema_version": {"type": "string", "const": EVIDENCE_EXTRACTION_V2_CONTRACT_VERSION},
                "source_identity": dict(hex64),
                "document_bundle_digest": dict(hex64),
                "coverage_plan_digest": dict(hex64),
                "run_id": dict(safe_id),
                "evidence": {"type": "array", "items": evidence_item},
            })
        disposition = _closed_object_schema({
            "segment_id": dict(safe_id),
            "disposition": {"type": "string", "enum": sorted(SEGMENT_DISPOSITIONS)},
            "ordered_evidence_ids": dict(string_array),
        })
        return _closed_object_schema({
            "schema_version": {"type": "string", "const": EVIDENCE_EXTRACTION_CONTRACT_VERSION},
            "source_identity": dict(hex64),
            "document_bundle_digest": dict(hex64),
            "run_id": dict(safe_id),
            "evidence": {"type": "array", "items": evidence_item},
            "segment_dispositions": {"type": "array", "items": disposition},
        })
    if request_kind == "evidence_adjudication":
        if response_schema_version != EVIDENCE_ADJUDICATION_CONTRACT_VERSION:
            raise ValueError("evidence response schema")
        decision = _closed_object_schema({
            "evidence_id": dict(safe_id),
            "evidence_digest": dict(hex64),
            "decision": {"type": "string", "enum": sorted(EVIDENCE_DECISIONS)},
            "reason_codes": {
                "type": "array",
                "items": {"type": "string", "enum": sorted(ADJUDICATION_REASON_CODES)},
            },
        })
        return _closed_object_schema({
            "schema_version": {"type": "string", "const": EVIDENCE_ADJUDICATION_CONTRACT_VERSION},
            "source_identity": dict(hex64),
            "extraction_bundle_digest": dict(hex64),
            "run_id": dict(safe_id),
            "decisions": {"type": "array", "items": decision},
        })
    raise ValueError("evidence response schema")


def _diagnostic_type(value: object) -> str:
    return {
        dict: "dict",
        list: "list",
        str: "str",
        int: "int",
        float: "float",
        bool: "bool",
        type(None): "null",
    }.get(type(value), "other")


def _diagnostic_key(value: object) -> str:
    if type(value) is str and _DIAGNOSTIC_KEY.fullmatch(value) is not None:
        return value
    return "unsafe-key"


def _diagnostic_dimensions(response: object) -> tuple[int, int]:
    if type(response) is str:
        return len(response.encode("utf-8")), len(response)
    try:
        encoded = json.dumps(
            response,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, OverflowError):
        return 0, 0
    return len(encoded.encode("utf-8")), len(encoded)


def _safe_contract_version(value: object, expected: str) -> str:
    if type(value) is str and _DIAGNOSTIC_KEY.fullmatch(value) is not None:
        return value
    if value is None:
        return "missing"
    return f"invalid-{_diagnostic_type(value)}"


def _first_extraction_shape_issue(raw: object) -> tuple[str, str, str]:
    if type(raw) is not dict:
        return "response_type", "top_level_exact_type_invalid", "$"
    if frozenset(raw) != _EXTRACTION_RESPONSE_KEYS:
        return "top_level_schema", "top_level_key_set_invalid", "$"
    scalar_fields = (
        "schema_version", "source_identity", "document_bundle_digest", "run_id",
    )
    for name in scalar_fields:
        if type(raw[name]) is not str:
            return "nested_schema", "exact_scalar_type_invalid", f"$.{name}"
    for name in ("evidence", "segment_dispositions"):
        if type(raw[name]) is not list:
            return "nested_schema", "exact_list_type_invalid", f"$.{name}"
    for item in raw["evidence"]:
        if type(item) is not dict:
            return "nested_schema", "list_item_exact_type_invalid", "$.evidence[]"
        if frozenset(item) != _EVIDENCE_KEYS:
            return "nested_schema", "nested_key_set_invalid", "$.evidence[]"
        for name in (
            "evidence_id", "proposition", "evidence_kind", "polarity",
            "uncertainty", "public_safety",
        ):
            if type(item[name]) is not str:
                return "nested_schema", "exact_scalar_type_invalid", f"$.evidence[].{name}"
        for name in ("ordered_segment_refs", "exact_quotes", "entities", "numbers", "dates"):
            if type(item[name]) is not list:
                return "nested_schema", "exact_list_type_invalid", f"$.evidence[].{name}"
        for quote in item["exact_quotes"]:
            if type(quote) is not dict:
                return "nested_schema", "list_item_exact_type_invalid", "$.evidence[].exact_quotes[]"
            if frozenset(quote) != _QUOTE_KEYS:
                return "nested_schema", "nested_key_set_invalid", "$.evidence[].exact_quotes[]"
            for name in ("quote_id", "document_id", "segment_id", "exact_text"):
                if type(quote[name]) is not str:
                    return "nested_schema", "exact_scalar_type_invalid", f"$.evidence[].exact_quotes[].{name}"
            for name in ("byte_start", "byte_end", "character_start", "character_end"):
                if type(quote[name]) is not int:
                    return "nested_schema", "exact_scalar_type_invalid", f"$.evidence[].exact_quotes[].{name}"
        for name in ("entities", "numbers", "dates"):
            for atom in item[name]:
                if type(atom) is not dict:
                    return "nested_schema", "list_item_exact_type_invalid", f"$.evidence[].{name}[]"
                if frozenset(atom) != _ATOM_KEYS:
                    return "nested_schema", "nested_key_set_invalid", f"$.evidence[].{name}[]"
                for field in _ATOM_KEYS:
                    if type(atom[field]) is not str:
                        return "nested_schema", "exact_scalar_type_invalid", f"$.evidence[].{name}[].{field}"
        for name in ("temporal_relation", "causal_relation"):
            relation = item[name]
            if relation is None:
                continue
            if type(relation) is not dict:
                return "nested_schema", "exact_mapping_type_invalid", f"$.evidence[].{name}"
            if frozenset(relation) != _RELATION_KEYS:
                return "nested_schema", "nested_key_set_invalid", f"$.evidence[].{name}"
            for field in ("relation_kind", "marker_quote_id"):
                if type(relation[field]) is not str:
                    return "nested_schema", "exact_scalar_type_invalid", f"$.evidence[].{name}.{field}"
            for field in ("left_operand_quote_ids", "right_operand_quote_ids"):
                if type(relation[field]) is not list:
                    return "nested_schema", "exact_list_type_invalid", f"$.evidence[].{name}.{field}"
    for item in raw["segment_dispositions"]:
        if type(item) is not dict:
            return "nested_schema", "list_item_exact_type_invalid", "$.segment_dispositions[]"
        if frozenset(item) != _DISPOSITION_KEYS:
            return "nested_schema", "nested_key_set_invalid", "$.segment_dispositions[]"
        for name in ("segment_id", "disposition"):
            if type(item[name]) is not str:
                return "nested_schema", "exact_scalar_type_invalid", f"$.segment_dispositions[].{name}"
        if type(item["ordered_evidence_ids"]) is not list:
            return "nested_schema", "exact_list_type_invalid", "$.segment_dispositions[].ordered_evidence_ids"
    return "semantic_validation", "code_owned_validation_rejected", "$"


def _nested_key_delta(raw: object) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if type(raw) is not dict or type(raw.get("evidence")) is not list:
        return (), ()
    candidates: list[tuple[object, frozenset[str]]] = []
    for item in raw["evidence"]:
        candidates.append((item, _EVIDENCE_KEYS))
        if type(item) is not dict:
            continue
        for quote in item.get("exact_quotes", ()) if type(item.get("exact_quotes")) is list else ():
            candidates.append((quote, _QUOTE_KEYS))
        for name in ("entities", "numbers", "dates"):
            for atom in item.get(name, ()) if type(item.get(name)) is list else ():
                candidates.append((atom, _ATOM_KEYS))
        for name in ("temporal_relation", "causal_relation"):
            relation = item.get(name)
            if relation is not None:
                candidates.append((relation, _RELATION_KEYS))
    if type(raw.get("segment_dispositions")) is list:
        candidates.extend((item, _DISPOSITION_KEYS) for item in raw["segment_dispositions"])
    for value, expected in candidates:
        if type(value) is not dict:
            continue
        actual = frozenset(item for item in value if type(item) is str)
        if frozenset(value) != expected:
            return (
                tuple(sorted(expected - actual)),
                tuple(sorted({_diagnostic_key(item) for item in value if item not in expected})),
            )
    return (), ()


def _nested_shape_inventory(
    raw: object,
) -> tuple[tuple[tuple[str, str], ...], tuple[tuple[str, int], ...]]:
    if type(raw) is not dict:
        return (), ()
    types: list[tuple[str, str]] = []
    counts: list[tuple[str, int]] = []

    def add(path: str, value: object) -> None:
        if len(types) < 96:
            types.append((path, _diagnostic_type(value)))
        if type(value) is list and len(counts) < 48:
            counts.append((path, len(value)))

    for name in sorted(_EXTRACTION_RESPONSE_KEYS | _EXTRACTION_V2_RESPONSE_KEYS):
        if name in raw:
            add(f"$.{name}", raw[name])
    evidence_items = raw.get("evidence")
    if type(evidence_items) is list:
        for item in evidence_items:
            add("$.evidence[]", item)
            if type(item) is not dict:
                continue
            for name in sorted(_EVIDENCE_KEYS):
                if name in item:
                    add(f"$.evidence[].{name}", item[name])
            if "ordered_block_refs" in item:
                add("$.evidence[].ordered_block_refs", item["ordered_block_refs"])
            quote_items = item.get("exact_quotes")
            if type(quote_items) is list:
                for quote in quote_items:
                    add("$.evidence[].exact_quotes[]", quote)
                    if type(quote) is dict:
                        for name in sorted(_QUOTE_KEYS):
                            if name in quote:
                                add(f"$.evidence[].exact_quotes[].{name}", quote[name])
            for name in ("entities", "numbers", "dates"):
                atom_items = item.get(name)
                if type(atom_items) is list:
                    for atom in atom_items:
                        add(f"$.evidence[].{name}[]", atom)
                        if type(atom) is dict:
                            for field in sorted(_ATOM_KEYS):
                                if field in atom:
                                    add(f"$.evidence[].{name}[].{field}", atom[field])
    dispositions = raw.get("segment_dispositions")
    if type(dispositions) is list:
        for item in dispositions:
            add("$.segment_dispositions[]", item)
            if type(item) is dict:
                for name in sorted(_DISPOSITION_KEYS):
                    if name in item:
                        add(f"$.segment_dispositions[].{name}", item[name])
    return tuple(types), tuple(counts)


def _semantic_diagnostic(
    reason_code: str,
    semantic_rejection: str | None,
) -> tuple[str, str, str, str]:
    if semantic_rejection in _SEMANTIC_REJECTIONS:
        return _SEMANTIC_REJECTIONS[semantic_rejection]
    return {
        "evidence_source_binding_invalid": (
            "source_binding", "source_or_document_binding_mismatch", "$.source_identity", "not_applicable",
        ),
        "evidence_segment_binding_invalid": (
            "segment_binding", "segment_coverage_or_order_invalid", "$.segment_dispositions", "not_applicable",
        ),
        "evidence_quote_binding_invalid": (
            "quote_binding", "exact_byte_or_character_span_invalid", "$.evidence[].exact_quotes[]", "exact_span_invalid",
        ),
        "evidence_proposition_binding_invalid": (
            "proposition_binding", "proposition_not_exact_quote", "$.evidence[].proposition", "exact_quote_missing",
        ),
        "evidence_value_binding_invalid": (
            "value_binding", "entity_number_or_date_binding_invalid", "$.evidence[]", "not_applicable",
        ),
        "evidence_relation_binding_invalid": (
            "relation_binding", "relation_operand_or_marker_invalid", "$.evidence[]", "not_applicable",
        ),
        "evidence_polarity_invalid": (
            "semantic_validation", "polarity_conflict", "$.evidence[].polarity", "not_applicable",
        ),
        "evidence_uncertainty_invalid": (
            "semantic_validation", "uncertainty_conflict", "$.evidence[].uncertainty", "not_applicable",
        ),
        "evidence_sensitive": (
            "semantic_validation", "privacy_classification_rejected", "$.evidence[]", "not_applicable",
        ),
    }.get(reason_code, (
        "semantic_validation", "code_owned_validation_rejected", "$", "not_applicable",
    ))


def _extraction_diagnostic(
    response: object,
    source_bundle: SourceDocumentBundle,
    reason_code: str,
    semantic_rejection: str | None = None,
) -> EvidenceValidationDiagnostic:
    byte_size, character_size = _diagnostic_dimensions(response)
    decoded: object = response
    json_invalid = False
    if type(response) is str:
        try:
            decoded = json.loads(response)
        except (json.JSONDecodeError, RecursionError):
            json_invalid = True
    if type(decoded) is dict:
        safe_keys = tuple(sorted({_diagnostic_key(item) for item in decoded}))
        actual_keys = frozenset(item for item in decoded if type(item) is str)
        missing = tuple(sorted(_EXTRACTION_RESPONSE_KEYS - actual_keys))
        extra = tuple(sorted({_diagnostic_key(item) for item in decoded if item not in _EXTRACTION_RESPONSE_KEYS}))
        nested, counts = _nested_shape_inventory(decoded)
        version = _safe_contract_version(decoded.get("schema_version"), EVIDENCE_EXTRACTION_CONTRACT_VERSION)
        binding = (
            "matched"
            if type(decoded.get("source_identity")) is str
            and decoded.get("source_identity") == source_bundle.source_identity
            else "mismatched"
            if type(decoded.get("source_identity")) is str
            else "unavailable"
        )
    else:
        safe_keys = ()
        missing = tuple(sorted(_EXTRACTION_RESPONSE_KEYS))
        extra = ()
        nested = ()
        counts = ()
        version = "missing"
        binding = "unavailable"
    if json_invalid:
        stage, subreason, field_path, span_category = (
            "json_parse", "json_document_invalid", "$", "not_applicable",
        )
    else:
        stage, subreason, field_path = _first_extraction_shape_issue(decoded)
        span_category = "not_applicable"
        if stage == "nested_schema" and subreason == "nested_key_set_invalid":
            missing, extra = _nested_key_delta(decoded)
        if stage == "semantic_validation":
            stage, subreason, field_path, span_category = _semantic_diagnostic(
                reason_code,
                semantic_rejection,
            )
        if (
            type(decoded) is dict
            and frozenset(decoded) == _EXTRACTION_RESPONSE_KEYS
            and type(decoded.get("schema_version")) is str
            and decoded.get("schema_version") != EVIDENCE_EXTRACTION_CONTRACT_VERSION
        ):
            stage, subreason, field_path = "contract_version", "contract_version_mismatch", "$.schema_version"
    return EvidenceValidationDiagnostic(
        validation_stage=stage,
        stable_subreason=subreason,
        field_path=field_path,
        response_top_level_exact_type=_diagnostic_type(response),
        top_level_key_set=safe_keys,
        missing_keys=missing,
        extra_keys=extra,
        nested_field_types=nested,
        list_item_counts=counts,
        schema_contract_version=version,
        span_quote_validation_category=span_category,
        source_identity_binding_result=binding,
        response_byte_size=byte_size,
        response_character_size=character_size,
    )


def _first_extraction_v2_shape_issue(raw: object) -> tuple[str, str, str]:
    if type(raw) is not dict:
        return "response_type", "top_level_exact_type_invalid", "$"
    if frozenset(raw) != _EXTRACTION_V2_RESPONSE_KEYS:
        return "top_level_schema", "top_level_key_set_invalid", "$"
    for name in (
        "schema_version", "source_identity", "document_bundle_digest",
        "coverage_plan_digest", "run_id",
    ):
        if type(raw[name]) is not str:
            return "nested_schema", "exact_scalar_type_invalid", f"$.{name}"
    if type(raw["evidence"]) is not list:
        return "nested_schema", "exact_list_type_invalid", "$.evidence"
    expected_item_keys = _EVIDENCE_KEYS | {"ordered_block_refs"}
    for item in raw["evidence"]:
        if type(item) is not dict:
            return "nested_schema", "list_item_exact_type_invalid", "$.evidence[]"
        if frozenset(item) != expected_item_keys:
            return "nested_schema", "nested_key_set_invalid", "$.evidence[]"
        if type(item["ordered_block_refs"]) is not list:
            return "nested_schema", "exact_list_type_invalid", "$.evidence[].ordered_block_refs"
        reduced = {key: value for key, value in item.items() if key != "ordered_block_refs"}
        projected = {
            "schema_version": EVIDENCE_EXTRACTION_CONTRACT_VERSION,
            "source_identity": raw["source_identity"],
            "document_bundle_digest": raw["document_bundle_digest"],
            "run_id": raw["run_id"],
            "evidence": [reduced],
            "segment_dispositions": [],
        }
        stage, reason, path = _first_extraction_shape_issue(projected)
        if stage != "semantic_validation":
            return stage, reason, path
    return "semantic_validation", "code_owned_validation_rejected", "$"


def _extraction_v2_diagnostic(
    response: object,
    source_bundle: SourceDocumentBundle,
    reason_code: str,
    semantic_rejection: str | None = None,
) -> EvidenceValidationDiagnostic:
    byte_size, character_size = _diagnostic_dimensions(response)
    decoded: object = response
    json_invalid = False
    if type(response) is str:
        try:
            decoded = json.loads(response)
        except (json.JSONDecodeError, RecursionError):
            json_invalid = True
    if type(decoded) is dict:
        safe_keys = tuple(sorted({_diagnostic_key(item) for item in decoded}))
        actual_keys = frozenset(item for item in decoded if type(item) is str)
        missing = tuple(sorted(_EXTRACTION_V2_RESPONSE_KEYS - actual_keys))
        extra = tuple(sorted({
            _diagnostic_key(item) for item in decoded
            if item not in _EXTRACTION_V2_RESPONSE_KEYS
        }))
        nested, counts = _nested_shape_inventory(decoded)
        version = _safe_contract_version(
            decoded.get("schema_version"), EVIDENCE_EXTRACTION_V2_CONTRACT_VERSION,
        )
        binding = (
            "matched"
            if type(decoded.get("source_identity")) is str
            and decoded.get("source_identity") == source_bundle.source_identity
            else "mismatched"
            if type(decoded.get("source_identity")) is str
            else "unavailable"
        )
    else:
        safe_keys = ()
        missing = tuple(sorted(_EXTRACTION_V2_RESPONSE_KEYS))
        extra = ()
        nested = ()
        counts = ()
        version = "missing"
        binding = "unavailable"
    if json_invalid:
        stage, subreason, field_path, span_category = (
            "json_parse", "json_document_invalid", "$", "not_applicable",
        )
    else:
        stage, subreason, field_path = _first_extraction_v2_shape_issue(decoded)
        span_category = "not_applicable"
        if stage == "semantic_validation":
            stage, subreason, field_path, span_category = _semantic_diagnostic(
                reason_code, semantic_rejection,
            )
        if (
            type(decoded) is dict
            and frozenset(decoded) == _EXTRACTION_V2_RESPONSE_KEYS
            and type(decoded.get("schema_version")) is str
            and decoded.get("schema_version") != EVIDENCE_EXTRACTION_V2_CONTRACT_VERSION
        ):
            stage, subreason, field_path = (
                "contract_version", "contract_version_mismatch", "$.schema_version",
            )
    return EvidenceValidationDiagnostic(
        validation_stage=stage,
        stable_subreason=subreason,
        field_path=field_path,
        response_top_level_exact_type=_diagnostic_type(response),
        top_level_key_set=safe_keys,
        missing_keys=missing,
        extra_keys=extra,
        nested_field_types=nested,
        list_item_counts=counts,
        schema_contract_version=version,
        span_quote_validation_category=span_category,
        source_identity_binding_result=binding,
        response_byte_size=byte_size,
        response_character_size=character_size,
    )


def _quote_from(value: object) -> EvidenceQuote:
    raw = _exact_mapping(value, _QUOTE_KEYS)
    try:
        return EvidenceQuote(**raw)
    except (TypeError, ValueError):
        _raise("evidence_schema_invalid", semantic_rejection="quote_span_or_ownership_invalid")


def _atom_from(value: object, expected_kind: str) -> EvidenceAtom:
    raw = _exact_mapping(value, _ATOM_KEYS)
    try:
        atom = EvidenceAtom(**raw)
    except (TypeError, ValueError):
        _raise("evidence_schema_invalid", semantic_rejection=f"{expected_kind}_ownership_invalid")
    if atom.atom_kind != expected_kind:
        _raise("evidence_schema_invalid", semantic_rejection=f"{expected_kind}_ownership_invalid")
    return atom


def _relation_from(
    value: object,
    allowed: frozenset[str],
    semantic_rejection: str,
) -> EvidenceRelation | None:
    if value is None:
        return None
    raw = _exact_mapping(value, _RELATION_KEYS)
    left_operands = _exact_list(raw["left_operand_quote_ids"])
    right_operands = _exact_list(raw["right_operand_quote_ids"])
    if not left_operands or not right_operands:
        incomplete_rejection = (
            "temporal_relation_operands_incomplete"
            if semantic_rejection == "temporal_relation_mismatch"
            else "causal_relation_operands_incomplete"
        )
        _raise("evidence_schema_invalid", semantic_rejection=incomplete_rejection)
    try:
        relation = EvidenceRelation(
            relation_kind=raw["relation_kind"],
            marker_quote_id=raw["marker_quote_id"],
            left_operand_quote_ids=tuple(left_operands),
            right_operand_quote_ids=tuple(right_operands),
        )
    except (TypeError, ValueError):
        _raise("evidence_schema_invalid", semantic_rejection=semantic_rejection)
    if relation.relation_kind not in allowed:
        _raise("evidence_schema_invalid", semantic_rejection=semantic_rejection)
    return relation


def _evidence_from(value: object) -> SourceEvidence:
    raw = _exact_mapping(value, _EVIDENCE_KEYS)
    try:
        return SourceEvidence(
            evidence_id=raw["evidence_id"],
            proposition=raw["proposition"],
            evidence_kind=raw["evidence_kind"],
            ordered_segment_refs=tuple(_exact_list(raw["ordered_segment_refs"])),
            exact_quotes=tuple(_quote_from(item) for item in _exact_list(raw["exact_quotes"])),
            entities=tuple(_atom_from(item, "entity") for item in _exact_list(raw["entities"])),
            numbers=tuple(_atom_from(item, "number") for item in _exact_list(raw["numbers"])),
            dates=tuple(_atom_from(item, "date") for item in _exact_list(raw["dates"])),
            polarity=raw["polarity"],
            temporal_relation=_relation_from(
                raw["temporal_relation"], TEMPORAL_RELATIONS, "temporal_relation_mismatch",
            ),
            causal_relation=_relation_from(
                raw["causal_relation"], CAUSAL_RELATIONS, "causal_relation_mismatch",
            ),
            uncertainty=raw["uncertainty"],
            public_safety=raw["public_safety"],
        )
    except EvidenceContractError:
        raise
    except (TypeError, ValueError) as error:
        rejection = {
            "evidence_kind": "unsupported_or_ambiguous_proposition",
            "insufficient evidence": "unsupported_or_ambiguous_proposition",
            "ordered_segment_refs": "evidence_count_or_coverage_policy_invalid",
            "exact_quotes": "evidence_count_or_coverage_policy_invalid",
            "evidence quotes": "evidence_count_or_coverage_policy_invalid",
            "polarity": "polarity_mismatch",
            "temporal_relation": "temporal_relation_mismatch",
            "causal_relation": "causal_relation_mismatch",
            "uncertainty": "uncertainty_mismatch",
            "public_safety": "privacy_classification_rejected",
        }.get(str(error), "generic_or_meaning_anchor_rejection")
        _raise("evidence_schema_invalid", semantic_rejection=rejection)


def _disposition_from(value: object) -> SegmentDisposition:
    raw = _exact_mapping(value, _DISPOSITION_KEYS)
    try:
        return SegmentDisposition(
            raw["segment_id"],
            raw["disposition"],
            tuple(_exact_list(raw["ordered_evidence_ids"])),
        )
    except (TypeError, ValueError) as error:
        rejection = (
            "duplicate_or_missing_segment_disposition"
            if str(error) in {"segment_id", "ordered_evidence_ids"}
            else "disposition_partition_mismatch"
        )
        _raise("evidence_schema_invalid", semantic_rejection=rejection)


def _mapping_response(value: Mapping[str, object] | str) -> dict[str, object]:
    if type(value) is str:
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            _raise("evidence_schema_invalid")
    if type(value) is not dict:
        _raise("evidence_schema_invalid")
    return value


def parse_extraction_response(
    response: Mapping[str, object] | str,
    source_bundle: SourceDocumentBundle,
) -> EvidenceExtractionBundle:
    try:
        raw = _exact_mapping(_mapping_response(response), _EXTRACTION_RESPONSE_KEYS)
        if (
            raw["schema_version"] != EVIDENCE_EXTRACTION_CONTRACT_VERSION
            or raw["source_identity"] != source_bundle.source_identity
            or raw["document_bundle_digest"] != source_bundle.bundle_digest
        ):
            _raise("evidence_source_binding_invalid")
        evidence = tuple(_evidence_from(item) for item in _exact_list(raw["evidence"]))
        dispositions = tuple(_disposition_from(item) for item in _exact_list(raw["segment_dispositions"]))
        payload = {
            "source_identity": raw["source_identity"],
            "document_bundle_digest": raw["document_bundle_digest"],
            "contract_version": EVIDENCE_EXTRACTION_CONTRACT_VERSION,
            "run_id": raw["run_id"],
            "ordered_evidence": evidence,
            "ordered_segment_dispositions": dispositions,
        }
        result = EvidenceExtractionBundle(**payload, bundle_digest=_sha(payload))
        validate_extraction(source_bundle, result)
        return result
    except EvidenceContractError as error:
        if error.diagnostic is None:
            error.diagnostic = _extraction_diagnostic(
                response,
                source_bundle,
                error.reason_code,
                error._semantic_rejection,
            )
        raise error from None
    except (TypeError, ValueError):
        diagnostic = _extraction_diagnostic(response, source_bundle, "evidence_schema_invalid")
        raise EvidenceContractError("evidence_schema_invalid", diagnostic) from None


def evidence_digest(evidence: SourceEvidence) -> str:
    if type(evidence) is not SourceEvidence:
        raise TypeError("evidence")
    return _sha(evidence)


def _quote_order_key(quote: EvidenceQuote, *, end: bool = False) -> tuple[str, int]:
    return (quote.document_id, quote.character_end if end else quote.character_start)


def _validate_relation(
    relation: EvidenceRelation,
    quotes: Mapping[str, EvidenceQuote],
    semantic_rejection: str,
) -> None:
    all_ids = {relation.marker_quote_id, *relation.left_operand_quote_ids, *relation.right_operand_quote_ids}
    if any(item not in quotes for item in all_ids):
        _raise("evidence_relation_binding_invalid", semantic_rejection=semantic_rejection)
    if relation.marker_quote_id in relation.left_operand_quote_ids or relation.marker_quote_id in relation.right_operand_quote_ids:
        _raise("evidence_relation_binding_invalid", semantic_rejection=semantic_rejection)
    marker = quotes[relation.marker_quote_id].exact_text
    rules = _TEMPORAL_MARKERS if relation.relation_kind in TEMPORAL_RELATIONS else _CAUSAL_MARKERS
    if rules[relation.relation_kind].search(marker) is None:
        _raise("evidence_relation_binding_invalid", semantic_rejection=semantic_rejection)
    marker_quote = quotes[relation.marker_quote_id]
    left_quotes = tuple(quotes[item] for item in relation.left_operand_quote_ids)
    right_quotes = tuple(quotes[item] for item in relation.right_operand_quote_ids)
    if (
        max((_quote_order_key(item, end=True) for item in left_quotes)) > _quote_order_key(marker_quote)
        or _quote_order_key(marker_quote, end=True) > min((_quote_order_key(item) for item in right_quotes))
    ):
        _raise("evidence_relation_binding_invalid", semantic_rejection=semantic_rejection)


def _contains_bound_lexeme(text: str, lexeme: str, atom_kind: str) -> bool:
    for match in re.finditer(re.escape(lexeme), text, flags=re.UNICODE):
        before = text[match.start() - 1] if match.start() else ""
        after = text[match.end()] if match.end() < len(text) else ""
        if before and (before.isalnum() or before == "_"):
            continue
        if after and (after.isalnum() or after == "_"):
            continue
        if atom_kind == "number":
            before_before = text[match.start() - 2] if match.start() >= 2 else ""
            after_after = text[match.end() + 1] if match.end() + 1 < len(text) else ""
            if before in ".," and before_before.isdigit():
                continue
            if after in ".," and after_after.isdigit():
                continue
        return True
    return False


def _valid_date_lexeme(lexeme: str) -> bool:
    if _DATE.fullmatch(lexeme) is None:
        return False
    normalized = lexeme.replace("/", "-").replace(".", "-")
    for pattern in ("%Y-%m-%d", "%d-%m-%Y", "%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y"):
        try:
            datetime.strptime(normalized if "%m" in pattern else lexeme, pattern)
        except ValueError:
            continue
        return True
    return False


def validate_extraction(source_bundle: SourceDocumentBundle, extraction: EvidenceExtractionBundle) -> None:
    if type(source_bundle) is not SourceDocumentBundle or type(extraction) is not EvidenceExtractionBundle:
        raise TypeError("evidence validation")
    if extraction.source_identity != source_bundle.source_identity or extraction.document_bundle_digest != source_bundle.bundle_digest:
        _raise("evidence_source_binding_invalid")
    segments = _segments(source_bundle)
    segment_by_id = {item.segment_id: item for item in segments}
    document_by_id = {item.document_id: item for item in source_bundle.ordered_documents}
    segment_order = {item.segment_id: index for index, item in enumerate(segments)}
    evidence_ids = tuple(item.evidence_id for item in extraction.ordered_evidence)
    if len(evidence_ids) != len(set(evidence_ids)):
        _raise("evidence_schema_invalid", semantic_rejection="duplicate_or_conflicting_evidence")
    disposition_ids = tuple(item.segment_id for item in extraction.ordered_segment_dispositions)
    if disposition_ids != tuple(item.segment_id for item in segments) or len(disposition_ids) != len(set(disposition_ids)):
        _raise("evidence_segment_binding_invalid", semantic_rejection="duplicate_or_missing_segment_disposition")
    disposition_by_id = {item.segment_id: item for item in extraction.ordered_segment_dispositions}
    known_evidence = set(evidence_ids)
    refs_by_evidence = {
        item.evidence_id: frozenset(item.ordered_segment_refs)
        for item in extraction.ordered_evidence
    }
    for disposition in extraction.ordered_segment_dispositions:
        segment = segment_by_id[disposition.segment_id]
        if any(item not in known_evidence for item in disposition.ordered_evidence_ids):
            _raise("evidence_segment_binding_invalid", semantic_rejection="disposition_partition_mismatch")
        if any(disposition.segment_id not in refs_by_evidence[item] for item in disposition.ordered_evidence_ids):
            _raise("evidence_segment_binding_invalid", semantic_rejection="evidence_item_not_bound_to_source_segment")
        if _is_sensitive(segment.exact_text) and disposition.disposition != "sensitive":
            _raise("evidence_sensitive", semantic_rejection="evidence_references_withheld_segment")
    global_quotes: set[str] = set()
    for evidence in extraction.ordered_evidence:
        if _is_sensitive(evidence.proposition):
            _raise("evidence_sensitive", semantic_rejection="privacy_classification_rejected")
        if evidence.ordered_segment_refs:
            try:
                positions = tuple(segment_order[item] for item in evidence.ordered_segment_refs)
            except KeyError:
                _raise("evidence_segment_binding_invalid", semantic_rejection="evidence_item_not_bound_to_source_segment")
            if positions != tuple(sorted(positions)):
                _raise("evidence_segment_binding_invalid", semantic_rejection="disposition_partition_mismatch")
        for segment_id in evidence.ordered_segment_refs:
            disposition = disposition_by_id[segment_id]
            if disposition.disposition != "evidence" or evidence.evidence_id not in disposition.ordered_evidence_ids:
                rejection = (
                    "evidence_references_withheld_segment"
                    if disposition.disposition == "sensitive"
                    else "evidence_item_not_bound_to_source_segment"
                )
                _raise("evidence_segment_binding_invalid", semantic_rejection=rejection)
        quotes: dict[str, EvidenceQuote] = {}
        for quote in evidence.exact_quotes:
            if quote.quote_id in global_quotes or quote.quote_id in quotes:
                _raise("evidence_quote_binding_invalid", semantic_rejection="duplicate_quote_or_conflicting_evidence")
            global_quotes.add(quote.quote_id)
            quotes[quote.quote_id] = quote
            segment = segment_by_id.get(quote.segment_id)
            document = document_by_id.get(quote.document_id)
            if (
                segment is None
                or document is None
                or segment.document_id != quote.document_id
                or quote.segment_id not in evidence.ordered_segment_refs
                or quote.character_start < segment.character_start
                or quote.character_end > segment.character_end
                or quote.byte_start < segment.byte_start
                or quote.byte_end > segment.byte_end
                or _is_sensitive(quote.exact_text)
            ):
                _raise("evidence_quote_binding_invalid", semantic_rejection="quote_span_or_ownership_invalid")
            raw = document.exact_text.encode("utf-8")
            try:
                byte_text = raw[quote.byte_start:quote.byte_end].decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                _raise("evidence_quote_binding_invalid", semantic_rejection="quote_span_or_ownership_invalid")
            if document.exact_text[quote.character_start:quote.character_end] != quote.exact_text or byte_text != quote.exact_text:
                _raise("evidence_quote_binding_invalid", semantic_rejection="quote_span_or_ownership_invalid")
        # The extractor's prose is not evidence.  A public proposition must
        # be an exact, independently replayable source span; adjudication may
        # accept or reject that span but cannot authorize invented wording.
        # Relation evidence can include additional operand/marker quotes as
        # long as one quote is the complete asserted proposition.
        if (
            evidence.evidence_kind != "insufficient_or_ambiguous"
            and evidence.proposition not in {item.exact_text for item in quotes.values()}
        ):
            _raise("evidence_proposition_binding_invalid", semantic_rejection="generic_or_meaning_anchor_rejection")
        all_atoms = (*evidence.entities, *evidence.numbers, *evidence.dates)
        atom_ids = tuple(item.atom_id for item in all_atoms)
        if len(atom_ids) != len(set(atom_ids)):
            _raise("evidence_value_binding_invalid", semantic_rejection="duplicate_or_conflicting_evidence")
        for atom in all_atoms:
            rejection = f"{atom.atom_kind}_ownership_invalid"
            quote = quotes.get(atom.quote_id)
            if quote is None or not _contains_bound_lexeme(quote.exact_text, atom.exact_lexeme, atom.atom_kind):
                _raise("evidence_value_binding_invalid", semantic_rejection=rejection)
            if atom.atom_kind == "number" and _NUMBER.fullmatch(atom.exact_lexeme) is None:
                _raise("evidence_value_binding_invalid", semantic_rejection="number_ownership_invalid")
            if atom.atom_kind == "date" and not _valid_date_lexeme(atom.exact_lexeme):
                _raise("evidence_value_binding_invalid", semantic_rejection="date_ownership_invalid")
        if evidence.temporal_relation is not None:
            _validate_relation(evidence.temporal_relation, quotes, "temporal_relation_mismatch")
        if evidence.causal_relation is not None:
            _validate_relation(evidence.causal_relation, quotes, "causal_relation_mismatch")
        if evidence.temporal_relation is not None and evidence.causal_relation is not None:
            _raise("evidence_relation_binding_invalid", semantic_rejection="duplicate_or_conflicting_evidence")
        if evidence.evidence_kind == "explicit_sequence" and (
            evidence.temporal_relation is None or evidence.temporal_relation.relation_kind != "sequence"
        ):
            _raise("evidence_relation_binding_invalid", semantic_rejection="temporal_relation_mismatch")
        if evidence.evidence_kind == "explicit_cause" and evidence.causal_relation is None:
            _raise("evidence_relation_binding_invalid", semantic_rejection="causal_relation_mismatch")
        quote_text = "\n".join(item.exact_text for item in evidence.exact_quotes)
        negated = _NEGATION.search(quote_text) is not None
        if (evidence.polarity == "negated") != negated and evidence.polarity != "quoted":
            _raise("evidence_polarity_invalid", semantic_rejection="polarity_mismatch")
        uncertain = _UNCERTAINTY.search(quote_text) is not None
        if evidence.uncertainty == "certain" and uncertain:
            _raise("evidence_uncertainty_invalid", semantic_rejection="uncertainty_mismatch")
        if evidence.uncertainty == "uncertain" and not uncertain:
            _raise("evidence_uncertainty_invalid", semantic_rejection="uncertainty_mismatch")
        if evidence.public_safety == "safe" and any(_is_sensitive(item.exact_text) for item in evidence.exact_quotes):
            _raise("evidence_sensitive", semantic_rejection="privacy_classification_rejected")


def _decision_from(value: object) -> EvidenceDecision:
    raw = _exact_mapping(value, _DECISION_KEYS)
    try:
        return EvidenceDecision(
            raw["evidence_id"],
            raw["evidence_digest"],
            raw["decision"],
            tuple(_exact_list(raw["reason_codes"])),
        )
    except (TypeError, ValueError):
        _raise("evidence_schema_invalid")


def _adjudication_diagnostic(
    response: object,
    extraction: EvidenceExtractionBundle,
    *,
    validation_stage: str,
    stable_reason: str,
    field_path: str,
    response_received: bool = True,
) -> AdjudicationValidationDiagnostic:
    raw: object = response
    if type(response) is str:
        try:
            raw = json.loads(response)
        except json.JSONDecodeError:
            raw = None
    decisions = raw.get("decisions") if type(raw) is dict else None
    values = decisions if type(decisions) is list else []
    expected = {
        item.evidence_id: evidence_digest(item)
        for item in extraction.ordered_evidence
    }
    ids: list[str] = []
    supported = rejected = ambiguous = mismatched = unknown = 0
    for item in values:
        if type(item) is not dict:
            continue
        evidence_id = item.get("evidence_id")
        decision = item.get("decision")
        if type(evidence_id) is str:
            ids.append(evidence_id)
            if evidence_id not in expected:
                unknown += 1
            elif item.get("evidence_digest") != expected[evidence_id]:
                mismatched += 1
        if decision == "supported":
            supported += 1
        elif decision == "rejected":
            rejected += 1
        elif decision == "ambiguous":
            ambiguous += 1
    known = {item for item in ids if item in expected}
    duplicate = len(ids) - len(set(ids))
    schema_value = raw.get("schema_version") if type(raw) is dict else None
    return AdjudicationValidationDiagnostic(
        category="blocked_adjudication",
        stable_reason=stable_reason,
        validation_stage=validation_stage,
        field_path=field_path,
        response_exact_type=_diagnostic_type(response),
        schema_version=(
            schema_value
            if type(schema_value) is str and _DIAGNOSTIC_KEY.fullmatch(schema_value)
            else "missing"
        ),
        source_binding_result=(
            "matched"
            if type(raw) is dict and raw.get("source_identity") == extraction.source_identity
            else "mismatched"
            if type(raw) is dict and type(raw.get("source_identity")) is str
            else "unavailable"
        ),
        extraction_bundle_binding_result=(
            "matched"
            if type(raw) is dict and raw.get("extraction_bundle_digest") == extraction.bundle_digest
            else "mismatched"
            if type(raw) is dict and type(raw.get("extraction_bundle_digest")) is str
            else "unavailable"
        ),
        expected_decision_count=len(expected),
        returned_decision_count=len(values),
        supported_decision_count=supported,
        rejected_decision_count=rejected,
        ambiguous_decision_count=ambiguous,
        missing_decision_count=len(set(expected) - known),
        duplicate_decision_count=duplicate,
        unknown_evidence_id_count=unknown,
        evidence_digest_mismatch_count=mismatched,
        transport_attempt_count=1,
        response_received=response_received,
    )


def adjudication_transport_diagnostic(
    error: BaseException,
    extraction: EvidenceExtractionBundle,
) -> AdjudicationValidationDiagnostic:
    safe = getattr(error, "diagnostic", None)
    category = getattr(safe, "category", None)
    response_received = getattr(safe, "response_received", False)
    return _adjudication_diagnostic(
        None,
        extraction,
        validation_stage="adjudication_response_type",
        stable_reason=(
            category
            if type(category) is str and _DIAGNOSTIC_KEY.fullmatch(category)
            else "provider_transport_failed"
        ),
        field_path="$",
        response_received=bool(response_received),
    )


def parse_adjudication_response(
    response: Mapping[str, object] | str,
    extraction: EvidenceExtractionBundle,
) -> EvidenceAdjudicationBundle:
    if type(response) not in {dict, str}:
        diagnostic = _adjudication_diagnostic(
            response, extraction,
            validation_stage="adjudication_response_type",
            stable_reason="response_exact_type_invalid",
            field_path="$",
        )
        raise EvidenceContractError("evidence_schema_invalid", diagnostic) from None
    if type(response) is str:
        try:
            decoded = json.loads(response)
        except json.JSONDecodeError:
            diagnostic = _adjudication_diagnostic(
                response, extraction,
                validation_stage="adjudication_json_parse",
                stable_reason="malformed_json",
                field_path="$",
            )
            raise EvidenceContractError("evidence_schema_invalid", diagnostic) from None
    else:
        decoded = response
    if type(decoded) is not dict or frozenset(decoded) != _ADJUDICATION_RESPONSE_KEYS:
        diagnostic = _adjudication_diagnostic(
            response, extraction,
            validation_stage="adjudication_top_level_schema",
            stable_reason="top_level_schema_invalid",
            field_path="$",
        )
        raise EvidenceContractError("evidence_schema_invalid", diagnostic) from None
    raw = decoded
    if raw["schema_version"] != EVIDENCE_ADJUDICATION_CONTRACT_VERSION:
        diagnostic = _adjudication_diagnostic(
            response, extraction,
            validation_stage="adjudication_contract_version",
            stable_reason="contract_version_mismatch",
            field_path="$.schema_version",
        )
        raise EvidenceContractError("evidence_adjudication_conflict", diagnostic) from None
    if raw["source_identity"] != extraction.source_identity:
        diagnostic = _adjudication_diagnostic(
            response, extraction,
            validation_stage="adjudication_source_binding",
            stable_reason="source_identity_mismatch",
            field_path="$.source_identity",
        )
        raise EvidenceContractError("evidence_adjudication_conflict", diagnostic) from None
    if raw["extraction_bundle_digest"] != extraction.bundle_digest:
        diagnostic = _adjudication_diagnostic(
            response, extraction,
            validation_stage="adjudication_source_binding",
            stable_reason="extraction_bundle_digest_mismatch",
            field_path="$.extraction_bundle_digest",
        )
        raise EvidenceContractError("evidence_adjudication_conflict", diagnostic) from None
    try:
        decisions = tuple(_decision_from(item) for item in _exact_list(raw["decisions"]))
        payload = {
            "source_identity": raw["source_identity"],
            "extraction_bundle_digest": raw["extraction_bundle_digest"],
            "contract_version": EVIDENCE_ADJUDICATION_CONTRACT_VERSION,
            "run_id": raw["run_id"],
            "ordered_decisions": decisions,
        }
        result = EvidenceAdjudicationBundle(**payload, bundle_digest=_sha(payload))
    except EvidenceContractError:
        diagnostic = _adjudication_diagnostic(
            response, extraction,
            validation_stage="adjudication_semantic_validation",
            stable_reason="decision_schema_invalid",
            field_path="$.decisions[]",
        )
        raise EvidenceContractError("evidence_schema_invalid", diagnostic) from None
    except (TypeError, ValueError):
        diagnostic = _adjudication_diagnostic(
            response, extraction,
            validation_stage="adjudication_semantic_validation",
            stable_reason="decision_schema_invalid",
            field_path="$.decisions[]",
        )
        raise EvidenceContractError("evidence_schema_invalid", diagnostic) from None
    expected = tuple((item.evidence_id, evidence_digest(item)) for item in extraction.ordered_evidence)
    actual = tuple((item.evidence_id, item.evidence_digest) for item in result.ordered_decisions)
    if actual != expected or len(actual) != len(set(item[0] for item in actual)):
        diagnostic = _adjudication_diagnostic(
            response, extraction,
            validation_stage="adjudication_decision_binding",
            stable_reason="decision_binding_incomplete",
            field_path="$.decisions",
        )
        raise EvidenceContractError("evidence_adjudication_incomplete", diagnostic) from None
    return result


def _make_verified(
    source_bundle: SourceDocumentBundle,
    extraction: EvidenceExtractionBundle,
    adjudication: EvidenceAdjudicationBundle,
) -> VerifiedEvidenceBundle:
    accepted = tuple(
        decision.evidence_id
        for decision in adjudication.ordered_decisions
        if decision.decision == "supported"
    )
    evidence_by_id = {item.evidence_id: item for item in extraction.ordered_evidence}
    if not accepted or any(
        evidence_by_id[item].evidence_kind == "insufficient_or_ambiguous"
        or evidence_by_id[item].uncertainty == "ambiguous"
        or evidence_by_id[item].public_safety != "safe"
        for item in accepted
    ):
        _raise("evidence_verified_bundle_invalid")
    payload = {
        "contract_version": VERIFIED_EVIDENCE_CONTRACT_VERSION,
        "source_identity": source_bundle.source_identity,
        "document_bundle_digest": source_bundle.bundle_digest,
        "extraction_bundle_digest": extraction.bundle_digest,
        "adjudication_bundle_digest": adjudication.bundle_digest,
        "accepted_evidence_ids": accepted,
    }
    return VerifiedEvidenceBundle(
        contract_version=VERIFIED_EVIDENCE_CONTRACT_VERSION,
        source_identity=source_bundle.source_identity,
        document_bundle_digest=source_bundle.bundle_digest,
        extraction=extraction,
        adjudication=adjudication,
        accepted_evidence_ids=accepted,
        verified_bundle_digest=_sha(payload),
    )


def _source_projection(bundle: SourceDocumentBundle) -> dict[str, object]:
    documents = []
    for document in bundle.ordered_documents:
        segments = []
        for segment in document.ordered_segments:
            if _is_sensitive(segment.exact_text):
                segments.append({
                    "segment_id": segment.segment_id,
                    "withheld": True,
                    "segment_digest": sha256(segment.exact_text.encode("utf-8")).hexdigest(),
                })
            else:
                segments.append({
                    "segment_id": segment.segment_id,
                    "withheld": False,
                    "exact_text": segment.exact_text,
                    "segment_kind": segment.segment_kind,
                    "container_path": segment.container_path,
                })
        documents.append({
            "document_id": document.document_id,
            "media_type": document.media_type,
            "segments": segments,
        })
    return {
        "source_identity": bundle.source_identity,
        "document_bundle_digest": bundle.bundle_digest,
        "documents": documents,
    }


def _block_projection(
    bundle: SourceDocumentBundle,
    inventory: SourceBlockInventory,
    *,
    selected_block_ids: frozenset[str] | None = None,
) -> dict[str, object]:
    segments = {item.segment_id: item for item in _segments(bundle)}
    projected: list[dict[str, object]] = []
    for block in inventory.ordered_blocks:
        if selected_block_ids is not None and block.block_id not in selected_block_ids:
            continue
        item: dict[str, object] = {
            "block_id": block.block_id,
            "block_digest": block.block_digest,
            "document_id": block.document_id,
            "ordered_segment_ids": list(block.ordered_segment_ids),
            "block_kind": block.block_kind,
            "sensitivity_status": block.sensitivity_status,
        }
        if block.sensitivity_status == "public":
            item["segments"] = [
                {
                    "segment_id": segment_id,
                    "exact_text": segments[segment_id].exact_text,
                    "byte_start": segments[segment_id].byte_start,
                    "byte_end": segments[segment_id].byte_end,
                    "character_start": segments[segment_id].character_start,
                    "character_end": segments[segment_id].character_end,
                }
                for segment_id in block.ordered_segment_ids
            ]
        projected.append(item)
    return {
        "schema_version": EVIDENCE_COVERAGE_CONTRACT_VERSION,
        "source_identity": bundle.source_identity,
        "document_bundle_digest": bundle.bundle_digest,
        "inventory_digest": inventory.inventory_digest,
        "blocks": projected,
    }


class _CoverageJsonObject(list[tuple[object, object]]):
    """Internal lossless JSON object used only to detect duplicate decisions."""


def _coverage_summary_from_values(
    inventory: SourceBlockInventory,
    values: Iterable[str],
    *,
    reason_code: str,
    returned: int,
    missing: int = 0,
    duplicate: int = 0,
    conflicting: int = 0,
) -> EvidenceCoverageSummary:
    counts = {item: 0 for item in BLOCK_DISPOSITIONS}
    for value in values:
        if type(value) is str and value in counts:
            counts[value] += 1
    valid = sum(counts.values())
    return EvidenceCoverageSummary(
        block_count=len(inventory.ordered_blocks),
        segment_count=sum(len(item.ordered_segment_ids) for item in inventory.ordered_blocks),
        returned_disposition_count=returned,
        valid_disposition_count=valid,
        missing_disposition_count=missing,
        duplicate_disposition_count=duplicate,
        conflicting_disposition_count=conflicting,
        evidence_candidate_count=counts["evidence_candidate"],
        context_only_count=counts["context_only"],
        structural_count=counts["structural"],
        sensitive_count=counts["sensitive_withheld"],
        ambiguous_count=counts["ambiguous"],
        omitted_count=max(0, len(inventory.ordered_blocks) - valid),
        reason_code=reason_code,
    )


def _reject_coverage(
    inventory: SourceBlockInventory,
    *,
    category: str,
    stable_reason: str,
    returned: int = 0,
    missing: int = 0,
    duplicate: int = 0,
    conflicting: int = 0,
    valid_values: Iterable[str] = (),
) -> None:
    if category not in _COVERAGE_FAILURE_CATEGORIES:
        raise TypeError("coverage category")
    source_binding_result = (
        "mismatched" if stable_reason == "source_identity_mismatch"
        else "not_checked" if stable_reason in {
            "malformed_json", "wrong_schema_version", "unknown_or_extra_field",
            "unsupported_object_type",
        }
        else "matched"
    )
    raise CoverageValidationError(CoverageFailureEvidence(
        category=category,
        validation_stage="coverage_validation",
        stable_reason=stable_reason,
        summary=_coverage_summary_from_values(
            inventory,
            tuple(valid_values),
            reason_code=category,
            returned=returned,
            missing=missing,
            duplicate=duplicate,
            conflicting=conflicting,
        ),
        source_binding_result=source_binding_result,
    )) from None


def _coverage_response_object(
    value: Mapping[str, object] | str,
    inventory: SourceBlockInventory,
) -> tuple[dict[str, object], _CoverageJsonObject | dict[str, object]]:
    if type(value) is str:
        try:
            decoded = json.loads(value, object_pairs_hook=_CoverageJsonObject)
        except json.JSONDecodeError:
            _reject_coverage(
                inventory, category="coverage_hard_invalid", stable_reason="malformed_json",
            )
        if type(decoded) is not _CoverageJsonObject:
            _reject_coverage(
                inventory, category="coverage_hard_invalid", stable_reason="unsupported_object_type",
            )
        top_pairs = decoded
        top_keys = tuple(item[0] for item in top_pairs)
        if any(type(key) is not str for key in top_keys) or len(top_keys) != len(set(top_keys)):
            _reject_coverage(
                inventory, category="coverage_hard_invalid", stable_reason="unknown_or_extra_field",
            )
        raw = dict(top_pairs)
    elif type(value) is dict:
        raw = value
    else:
        _reject_coverage(
            inventory, category="coverage_hard_invalid", stable_reason="unsupported_object_type",
        )
    if frozenset(raw) != _COVERAGE_RESPONSE_KEYS:
        _reject_coverage(
            inventory, category="coverage_hard_invalid", stable_reason="unknown_or_extra_field",
        )
    decisions = raw["block_dispositions"]
    if type(decisions) not in {dict, _CoverageJsonObject}:
        _reject_coverage(
            inventory, category="coverage_hard_invalid", stable_reason="exact_scalar_type_violation",
        )
    return raw, decisions


def parse_coverage_response(
    response: Mapping[str, object] | str,
    bundle: SourceDocumentBundle,
    inventory: SourceBlockInventory,
) -> EvidenceCoveragePlan:
    if type(bundle) is not SourceDocumentBundle or type(inventory) is not SourceBlockInventory:
        raise TypeError("coverage response")
    raw, decisions_object = _coverage_response_object(response, inventory)
    scalar_fields = (
        "schema_version", "source_identity", "document_bundle_digest", "inventory_digest", "run_id",
    )
    if any(type(raw[name]) is not str for name in scalar_fields):
        _reject_coverage(
            inventory, category="coverage_hard_invalid", stable_reason="exact_scalar_type_violation",
        )
    if raw["schema_version"] != EVIDENCE_COVERAGE_CONTRACT_VERSION:
        _reject_coverage(
            inventory, category="coverage_hard_invalid", stable_reason="wrong_schema_version",
        )
    if (
        raw["source_identity"] != bundle.source_identity
        or raw["document_bundle_digest"] != bundle.bundle_digest
        or raw["inventory_digest"] != inventory.inventory_digest
    ):
        _reject_coverage(
            inventory, category="coverage_hard_invalid", stable_reason="source_identity_mismatch",
        )
    try:
        _safe_id(raw["run_id"], "run_id")
    except (TypeError, ValueError):
        _reject_coverage(
            inventory, category="coverage_hard_invalid", stable_reason="exact_scalar_type_violation",
        )

    pairs = (
        list(decisions_object.items())
        if type(decisions_object) is dict
        else list(decisions_object)
    )
    if any(type(key) is not str or type(value) is not str for key, value in pairs):
        _reject_coverage(
            inventory,
            category="coverage_hard_invalid",
            stable_reason="exact_scalar_type_violation",
            returned=len(pairs),
        )
    if any(value not in BLOCK_DISPOSITIONS for _, value in pairs):
        _reject_coverage(
            inventory,
            category="coverage_hard_invalid",
            stable_reason="invalid_disposition_enum",
            returned=len(pairs),
        )
    decisions_raw: dict[str, str] = {}
    duplicate = 0
    conflicting = 0
    for block_id, disposition in pairs:
        if block_id in decisions_raw:
            duplicate += 1
            conflicting += decisions_raw[block_id] != disposition
        else:
            decisions_raw[block_id] = disposition
    if duplicate:
        _reject_coverage(
            inventory,
            category="coverage_incomplete",
            stable_reason=(
                "conflicting_block_disposition" if conflicting else "duplicate_block_disposition"
            ),
            returned=len(pairs),
            duplicate=duplicate,
            conflicting=conflicting,
            valid_values=decisions_raw.values(),
        )
    expected_ids = tuple(item.block_id for item in inventory.ordered_blocks)
    missing_ids = frozenset(expected_ids) - frozenset(decisions_raw)
    unexpected_ids = frozenset(decisions_raw) - frozenset(expected_ids)
    if unexpected_ids:
        _reject_coverage(
            inventory,
            category="coverage_incomplete",
            stable_reason="unexpected_block_disposition",
            returned=len(pairs),
            missing=len(missing_ids),
            conflicting=len(unexpected_ids),
            valid_values=(
                decisions_raw[key] for key in expected_ids if key in decisions_raw
            ),
        )
    if missing_ids:
        _reject_coverage(
            inventory,
            category="coverage_incomplete",
            stable_reason="missing_block_disposition",
            returned=len(pairs),
            missing=len(missing_ids),
            valid_values=decisions_raw.values(),
        )
    try:
        decisions = tuple(
            BlockCoverageDecision(block_id, decisions_raw[block_id])
            for block_id in expected_ids
        )
    except (TypeError, ValueError):
        _reject_coverage(
            inventory,
            category="coverage_hard_invalid",
            stable_reason="exact_scalar_type_violation",
            returned=len(pairs),
        )
    block_by_id = {item.block_id: item for item in inventory.ordered_blocks}
    for decision in decisions:
        sensitive = block_by_id[decision.block_id].sensitivity_status == "sensitive_withheld"
        if sensitive != (decision.disposition == "sensitive_withheld"):
            _reject_coverage(
                inventory,
                category="coverage_incomplete",
                stable_reason="conflicting_block_disposition",
                returned=len(pairs),
                conflicting=1,
                valid_values=decisions_raw.values(),
            )
    payload = {
        "contract_version": EVIDENCE_COVERAGE_CONTRACT_VERSION,
        "source_identity": bundle.source_identity,
        "document_bundle_digest": bundle.bundle_digest,
        "inventory_digest": inventory.inventory_digest,
        "run_id": raw["run_id"],
        "ordered_decisions": decisions,
    }
    try:
        return EvidenceCoveragePlan(**payload, plan_digest=_sha(payload))
    except (TypeError, ValueError):
        _reject_coverage(
            inventory,
            category="coverage_hard_invalid",
            stable_reason="exact_scalar_type_violation",
            returned=len(pairs),
        )


def _coverage_summary(
    inventory: SourceBlockInventory,
    plan: EvidenceCoveragePlan | None,
    reason_code: str,
) -> EvidenceCoverageSummary:
    counts = {item: 0 for item in BLOCK_DISPOSITIONS}
    if plan is not None:
        for decision in plan.ordered_decisions:
            counts[decision.disposition] += 1
    decided = sum(counts.values())
    return _coverage_summary_from_values(
        inventory,
        tuple(
            decision.disposition
            for decision in (() if plan is None else plan.ordered_decisions)
        ),
        reason_code=reason_code,
        returned=decided,
        missing=max(0, len(inventory.ordered_blocks) - decided),
    )


def _coverage_manual_failure(
    inventory: SourceBlockInventory,
    plan: EvidenceCoveragePlan,
    *,
    stable_reason: str,
) -> CoverageFailureEvidence:
    return CoverageFailureEvidence(
        category="coverage_incomplete",
        validation_stage="coverage_validation",
        stable_reason=stable_reason,
        summary=_coverage_summary(inventory, plan, "coverage_incomplete"),
        source_binding_result="matched",
    )


def _post_extraction_failure(
    inventory: SourceBlockInventory,
    plan: EvidenceCoveragePlan,
    diagnostic: EvidenceValidationDiagnostic,
) -> CoverageFailureEvidence:
    """Project one typed extraction rejection into a closed product outcome."""

    if (
        type(inventory) is not SourceBlockInventory
        or type(plan) is not EvidenceCoveragePlan
        or type(diagnostic) is not EvidenceValidationDiagnostic
    ):
        raise TypeError("post extraction failure")
    category = (
        "coverage_incomplete"
        if diagnostic.stable_subreason in _POST_EXTRACTION_INCOMPLETE_REASONS
        else "coverage_hard_invalid"
    )
    summary = _coverage_summary(inventory, plan, category)
    return CoverageFailureEvidence(
        category=category,
        validation_stage=diagnostic.validation_stage,
        stable_reason=diagnostic.stable_subreason,
        summary=summary,
        source_binding_result={
            "matched": "matched",
            "mismatched": "mismatched",
            "unavailable": "not_checked",
        }[diagnostic.source_identity_binding_result],
        evidence_diagnostic=diagnostic,
    )


def materializable_post_extraction_failure(
    failure: CoverageFailureEvidence,
) -> CoverageFailureEvidence:
    """Return the closed manual projection for one persisted semantic conflict."""

    if (
        type(failure) is not CoverageFailureEvidence
        or type(failure.evidence_diagnostic) is not EvidenceValidationDiagnostic
        or failure.evidence_diagnostic.stable_subreason
        not in _POST_EXTRACTION_INCOMPLETE_REASONS
        or failure.source_binding_result != "matched"
        or failure.transport_diagnostic is not None
    ):
        raise TypeError("post extraction failure")
    summary = replace(failure.summary, reason_code="coverage_incomplete")
    return CoverageFailureEvidence(
        category="coverage_incomplete",
        validation_stage=failure.evidence_diagnostic.validation_stage,
        stable_reason=failure.evidence_diagnostic.stable_subreason,
        summary=summary,
        source_binding_result="matched",
        evidence_diagnostic=failure.evidence_diagnostic,
    )


def parse_extraction_v2_response(
    response: Mapping[str, object] | str,
    bundle: SourceDocumentBundle,
    inventory: SourceBlockInventory,
    plan: EvidenceCoveragePlan,
) -> EvidenceExtractionBundle:
    try:
        raw = _exact_mapping(_mapping_response(response), _EXTRACTION_V2_RESPONSE_KEYS)
        if (
            raw["schema_version"] != EVIDENCE_EXTRACTION_V2_CONTRACT_VERSION
            or raw["source_identity"] != bundle.source_identity
            or raw["document_bundle_digest"] != bundle.bundle_digest
            or raw["coverage_plan_digest"] != plan.plan_digest
        ):
            _raise("evidence_source_binding_invalid")
        block_by_id = {item.block_id: item for item in inventory.ordered_blocks}
        selected = tuple(
            item.block_id for item in plan.ordered_decisions
            if item.disposition == "evidence_candidate"
        )
        selected_set = frozenset(selected)
        evidence_items: list[SourceEvidence] = []
        refs_by_evidence: dict[str, tuple[str, ...]] = {}
        for value in _exact_list(raw["evidence"]):
            if type(value) is not dict or frozenset(value) != _EVIDENCE_KEYS | {"ordered_block_refs"}:
                _raise("evidence_schema_invalid")
            block_refs = value["ordered_block_refs"]
            if (
                type(block_refs) is not list
                or not block_refs
                or any(type(item) is not str for item in block_refs)
                or len(block_refs) != len(set(block_refs))
                or any(item not in selected_set for item in block_refs)
            ):
                _raise(
                    "evidence_segment_binding_invalid",
                    semantic_rejection="evidence_item_not_bound_to_source_segment",
                )
            reduced = {key: item for key, item in value.items() if key != "ordered_block_refs"}
            evidence_item = _evidence_from(reduced)
            allowed_segments = {
                segment_id
                for block_id in block_refs
                for segment_id in block_by_id[block_id].ordered_segment_ids
            }
            if not set(evidence_item.ordered_segment_refs) <= allowed_segments:
                _raise(
                    "evidence_segment_binding_invalid",
                    semantic_rejection="evidence_item_not_bound_to_source_segment",
                )
            evidence_items.append(evidence_item)
            refs_by_evidence[evidence_item.evidence_id] = evidence_item.ordered_segment_refs
        evidence_by_segment: dict[str, list[str]] = {}
        for evidence_id, refs in refs_by_evidence.items():
            for segment_id in refs:
                evidence_by_segment.setdefault(segment_id, []).append(evidence_id)
        disposition_by_block = {item.block_id: item.disposition for item in plan.ordered_decisions}
        block_for_segment = {
            segment_id: block
            for block in inventory.ordered_blocks
            for segment_id in block.ordered_segment_ids
        }
        dispositions: list[SegmentDisposition] = []
        for segment in _segments(bundle):
            block = block_for_segment[segment.segment_id]
            evidence_ids = tuple(evidence_by_segment.get(segment.segment_id, ()))
            planned = disposition_by_block[block.block_id]
            disposition = (
                "sensitive" if planned == "sensitive_withheld"
                else "ambiguous" if planned == "ambiguous"
                else "evidence" if evidence_ids
                else "irrelevant"
            )
            dispositions.append(SegmentDisposition(segment.segment_id, disposition, evidence_ids))
        payload = {
            "source_identity": bundle.source_identity,
            "document_bundle_digest": bundle.bundle_digest,
            "contract_version": EVIDENCE_EXTRACTION_CONTRACT_VERSION,
            "run_id": raw["run_id"],
            "ordered_evidence": tuple(evidence_items),
            "ordered_segment_dispositions": tuple(dispositions),
        }
        result = EvidenceExtractionBundle(**payload, bundle_digest=_sha(payload))
        validate_extraction(bundle, result)
        return result
    except EvidenceContractError as error:
        if error.diagnostic is None:
            error.diagnostic = _extraction_v2_diagnostic(
                response, bundle, error.reason_code, error._semantic_rejection,
            )
        raise error from None
    except (TypeError, ValueError):
        diagnostic = _extraction_v2_diagnostic(
            response, bundle, "evidence_schema_invalid",
        )
        raise EvidenceContractError("evidence_schema_invalid", diagnostic) from None


@dataclass(frozen=True, slots=True)
class IndependentExtractionResult:
    extraction: EvidenceExtractionBundle
    summary: FactRelationValidationSummary
    selection_receipt: EvidenceSelectionReceipt | None = None

    def __post_init__(self) -> None:
        if type(self.extraction) is not EvidenceExtractionBundle:
            raise TypeError("extraction")
        if type(self.summary) is not FactRelationValidationSummary:
            raise TypeError("summary")
        if (
            self.selection_receipt is not None
            and type(self.selection_receipt) is not EvidenceSelectionReceipt
        ):
            raise TypeError("selection_receipt")


def _v3_diagnostic(
    response: object,
    bundle: SourceDocumentBundle,
    stable_subreason: str,
    field_path: str = "$",
) -> EvidenceValidationDiagnostic:
    byte_size, character_size = _diagnostic_dimensions(response)
    raw: object = response
    if type(response) is str:
        try:
            raw = json.loads(response)
        except json.JSONDecodeError:
            raw = None
    safe_keys = (
        tuple(sorted(_diagnostic_key(item) for item in raw))
        if type(raw) is dict
        else ()
    )
    actual = frozenset(raw) if type(raw) is dict else frozenset()
    version = (
        _safe_contract_version(raw.get("schema_version"), EVIDENCE_EXTRACTION_V3_CONTRACT_VERSION)
        if type(raw) is dict
        else "missing"
    )
    binding = (
        "matched"
        if type(raw) is dict and raw.get("source_identity") == bundle.source_identity
        else "mismatched"
        if type(raw) is dict and type(raw.get("source_identity")) is str
        else "unavailable"
    )
    return EvidenceValidationDiagnostic(
        validation_stage="semantic_validation",
        stable_subreason=stable_subreason,
        field_path=field_path,
        response_top_level_exact_type=_diagnostic_type(response),
        top_level_key_set=safe_keys,
        missing_keys=tuple(sorted(_EXTRACTION_V3_RESPONSE_KEYS - actual)),
        extra_keys=tuple(sorted(
            _diagnostic_key(item) for item in actual - _EXTRACTION_V3_RESPONSE_KEYS
        )),
        nested_field_types=(),
        list_item_counts=tuple(
            (f"$.{name}", len(raw[name]))
            for name in ("facts", "relations")
            if type(raw) is dict and type(raw.get(name)) is list
        ),
        schema_contract_version=version,
        span_quote_validation_category=(
            "rejected" if "quote" in stable_subreason or "span" in stable_subreason
            else "not_applicable"
        ),
        source_identity_binding_result=binding,
        response_byte_size=byte_size,
        response_character_size=character_size,
    )


def _atomic_fact_has_relation_claim(proposition: str) -> bool:
    return any(
        pattern.search(proposition) is not None
        for pattern in (*_TEMPORAL_MARKERS.values(), *_CAUSAL_MARKERS.values())
    )


def _fact_dispositions(
    bundle: SourceDocumentBundle,
    facts: tuple[SourceEvidence, ...],
) -> tuple[SegmentDisposition, ...]:
    refs: dict[str, list[str]] = {}
    for fact in facts:
        for segment_id in fact.ordered_segment_refs:
            refs.setdefault(segment_id, []).append(fact.evidence_id)
    return tuple(
        SegmentDisposition(
            segment.segment_id,
            "sensitive" if _is_sensitive(segment.exact_text)
            else "evidence" if segment.segment_id in refs
            else "irrelevant",
            tuple(refs.get(segment.segment_id, ())),
        )
        for segment in _segments(bundle)
    )


def _validate_one_atomic_fact(
    bundle: SourceDocumentBundle,
    fact: SourceEvidence,
    run_id: str,
) -> None:
    if (
        fact.evidence_kind not in ATOMIC_EVIDENCE_KINDS
        or _atomic_fact_has_relation_claim(fact.proposition)
    ):
        _raise(
            "evidence_proposition_binding_invalid",
            semantic_rejection="unsupported_or_ambiguous_proposition",
        )
    payload = {
        "source_identity": bundle.source_identity,
        "document_bundle_digest": bundle.bundle_digest,
        "contract_version": EVIDENCE_EXTRACTION_CONTRACT_VERSION,
        "run_id": run_id,
        "ordered_evidence": (fact,),
        "ordered_segment_dispositions": _fact_dispositions(bundle, (fact,)),
    }
    candidate = EvidenceExtractionBundle(**payload, bundle_digest=_sha(payload))
    validate_extraction(bundle, candidate)


def _quote_is_exact_source_span(
    quote: EvidenceQuote,
    bundle: SourceDocumentBundle,
) -> bool:
    segment = next(
        (item for item in _segments(bundle) if item.segment_id == quote.segment_id), None
    )
    document = next(
        (item for item in bundle.ordered_documents if item.document_id == quote.document_id), None
    )
    if (
        segment is None
        or document is None
        or segment.document_id != quote.document_id
        or quote.character_start < segment.character_start
        or quote.character_end > segment.character_end
        or quote.byte_start < segment.byte_start
        or quote.byte_end > segment.byte_end
        or _is_sensitive(quote.exact_text)
    ):
        return False
    try:
        byte_text = document.exact_text.encode("utf-8")[
            quote.byte_start:quote.byte_end
        ].decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return False
    return (
        document.exact_text[quote.character_start:quote.character_end]
        == quote.exact_text == byte_text
    )


_TEMPORAL_OVERLAP_MARKER = re.compile(
    r"\b(?:while|during|simultaneously|concurrently|at\s+the\s+same\s+time|"
    r"одновременно|в\s+то\s+же\s+время|пока)\b",
    re.IGNORECASE,
)
_CONTRADICTION_MARKER = re.compile(
    r"\b(?:but|however|contradicts?|whereas|но|однако|противоречит|тогда\s+как)\b",
    re.IGNORECASE,
)


def _relation_is_code_verified(
    raw: dict[str, object],
    valid_by_id: Mapping[str, SourceEvidence],
    bundle: SourceDocumentBundle,
) -> tuple[bool, str]:
    try:
        relation_id = raw["relation_id"]
        relation_kind = raw["relation_kind"]
        left_id = raw["left_fact_id"]
        right_id = raw["right_fact_id"]
        if any(type(item) is not str for item in (relation_id, relation_kind, left_id, right_id)):
            return False, "causal"
        _safe_id(relation_id, "relation_id")
        if relation_kind not in FACT_RELATION_KINDS or left_id == right_id:
            return False, "causal"
        if left_id not in valid_by_id or right_id not in valid_by_id:
            return False, "polarity" if relation_kind == "contradicts" else (
                "temporal" if relation_kind.startswith("temporal_") else "causal"
            )
        quote = _quote_from(raw["support_quote"])
    except (EvidenceContractError, TypeError, ValueError, KeyError):
        return False, "causal"
    if not _quote_is_exact_source_span(quote, bundle):
        return False, "temporal" if relation_kind.startswith("temporal_") else "causal"
    marker = quote.exact_text
    if relation_kind == "temporal_before":
        verified = _TEMPORAL_MARKERS["before"].search(marker) is not None
        category = "temporal"
    elif relation_kind == "temporal_after":
        verified = _TEMPORAL_MARKERS["after"].search(marker) is not None
        category = "temporal"
    elif relation_kind == "temporal_overlap":
        verified = _TEMPORAL_OVERLAP_MARKER.search(marker) is not None
        category = "temporal"
    elif relation_kind in {"causal", "enables"}:
        verified = any(pattern.search(marker) is not None for pattern in _CAUSAL_MARKERS.values())
        category = "causal"
    else:
        left = valid_by_id[left_id]
        right = valid_by_id[right_id]
        verified = (
            _CONTRADICTION_MARKER.search(marker) is not None
            and left.polarity != right.polarity
        )
        category = "polarity"
    return verified, category


def parse_extraction_v3_response(
    response: Mapping[str, object] | str,
    bundle: SourceDocumentBundle,
    inventory: SourceBlockInventory,
    plan: EvidenceCoveragePlan,
) -> IndependentExtractionResult:
    """Validate atomic facts independently and relations on a separate boundary."""

    try:
        raw = _exact_mapping(_mapping_response(response), _EXTRACTION_V3_RESPONSE_KEYS)
        if raw["schema_version"] != EVIDENCE_EXTRACTION_V3_CONTRACT_VERSION:
            _raise("evidence_schema_invalid")
        if (
            raw["source_identity"] != bundle.source_identity
            or raw["document_bundle_digest"] != bundle.bundle_digest
            or raw["coverage_plan_digest"] != plan.plan_digest
        ):
            _raise("evidence_source_binding_invalid")
        _safe_id(raw["run_id"], "run_id")
        fact_values = _exact_list(raw["facts"])
        relation_values = _exact_list(raw["relations"])
    except EvidenceContractError as error:
        if error.diagnostic is None:
            error.diagnostic = _v3_diagnostic(
                response, bundle,
                "source_or_document_binding_mismatch"
                if error.reason_code == "evidence_source_binding_invalid"
                else "schema_or_contract_invalid",
            )
        raise error from None
    except (TypeError, ValueError):
        diagnostic = _v3_diagnostic(response, bundle, "schema_or_contract_invalid")
        raise EvidenceContractError("evidence_schema_invalid", diagnostic) from None

    selected = frozenset(
        item.block_id for item in plan.ordered_decisions
        if item.disposition == "evidence_candidate"
    )
    block_by_id = {item.block_id: item for item in inventory.ordered_blocks}
    raw_ids = [
        value.get("fact_id") if type(value) is dict else None
        for value in fact_values
    ]
    duplicate_ids = {
        item for item in raw_ids
        if type(item) is str and raw_ids.count(item) > 1
    }
    valid: list[SourceEvidence] = []
    rejected_facts = 0
    polarity_conflicts = 0
    for value in fact_values:
        try:
            fact_raw = _exact_mapping(value, _V3_FACT_KEYS)
            block_refs = _exact_list(fact_raw["ordered_block_refs"])
            if (
                not block_refs
                or any(type(item) is not str or item not in selected for item in block_refs)
                or len(block_refs) != len(set(block_refs))
            ):
                _raise(
                    "evidence_segment_binding_invalid",
                    semantic_rejection="evidence_item_not_bound_to_source_segment",
                )
            if fact_raw["fact_id"] in duplicate_ids:
                _raise(
                    "evidence_schema_invalid",
                    semantic_rejection="duplicate_or_conflicting_evidence",
                )
            allowed_segments = {
                segment_id for block_id in block_refs
                for segment_id in block_by_id[block_id].ordered_segment_ids
            }
            if not set(_exact_list(fact_raw["ordered_segment_refs"])) <= allowed_segments:
                _raise(
                    "evidence_segment_binding_invalid",
                    semantic_rejection="evidence_item_not_bound_to_source_segment",
                )
            if (
                fact_raw["public_safety"] != "safe"
                or _is_sensitive(fact_raw["proposition"])
                or any(
                    type(item) is dict
                    and type(item.get("exact_text")) is str
                    and _is_sensitive(item["exact_text"])
                    for item in _exact_list(fact_raw["exact_quotes"])
                )
            ):
                _raise(
                    "evidence_sensitive",
                    semantic_rejection="privacy_classification_rejected",
                )
            legacy = {
                "evidence_id": fact_raw["fact_id"],
                **{
                    key: item for key, item in fact_raw.items()
                    if key not in {"fact_id", "ordered_block_refs"}
                },
                "temporal_relation": None,
                "causal_relation": None,
            }
            fact = _evidence_from(legacy)
            _validate_one_atomic_fact(bundle, fact, raw["run_id"])
            valid.append(fact)
        except EvidenceContractError as error:
            if error.reason_code == "evidence_sensitive":
                diagnostic = _v3_diagnostic(
                    response, bundle, "privacy_classification_rejected", "$.facts[]",
                )
                raise EvidenceContractError("evidence_sensitive", diagnostic) from None
            rejected_facts += 1
            polarity_conflicts += int(error._semantic_rejection == "polarity_mismatch")
        except (TypeError, ValueError, KeyError):
            diagnostic = _v3_diagnostic(response, bundle, "schema_or_contract_invalid", "$.facts[]")
            raise EvidenceContractError("evidence_schema_invalid", diagnostic) from None

    valid_by_id = {item.evidence_id: item for item in valid}
    relation_ids: set[str] = set()
    verified_relations = 0
    rejected_relations = 0
    temporal_conflicts = 0
    causal_conflicts = 0
    for value in relation_values:
        if type(value) is not dict or frozenset(value) != _V3_RELATION_KEYS:
            diagnostic = _v3_diagnostic(response, bundle, "schema_or_contract_invalid", "$.relations[]")
            raise EvidenceContractError("evidence_schema_invalid", diagnostic) from None
        relation_id = value.get("relation_id")
        if type(relation_id) is str and relation_id not in relation_ids:
            relation_ids.add(relation_id)
            verified, category = _relation_is_code_verified(value, valid_by_id, bundle)
        else:
            verified, category = False, "causal"
        if verified:
            verified_relations += 1
        else:
            rejected_relations += 1
            temporal_conflicts += int(category == "temporal")
            causal_conflicts += int(category == "causal")
            polarity_conflicts += int(category == "polarity")

    dispositions = _fact_dispositions(bundle, tuple(valid))
    payload = {
        "source_identity": bundle.source_identity,
        "document_bundle_digest": bundle.bundle_digest,
        "contract_version": EVIDENCE_EXTRACTION_CONTRACT_VERSION,
        "run_id": raw["run_id"],
        "ordered_evidence": tuple(valid),
        "ordered_segment_dispositions": dispositions,
    }
    extraction = EvidenceExtractionBundle(**payload, bundle_digest=_sha(payload))
    validate_extraction(bundle, extraction)
    summary = FactRelationValidationSummary(
        returned_fact_count=len(fact_values),
        valid_fact_count=len(valid),
        rejected_fact_count=rejected_facts,
        returned_relation_count=len(relation_values),
        verified_relation_count=verified_relations,
        rejected_relation_count=rejected_relations,
        temporal_conflict_count=temporal_conflicts,
        causal_conflict_count=causal_conflicts,
        polarity_conflict_count=polarity_conflicts,
        verified_fact_summaries=tuple(item.proposition for item in valid[:7]),
    )
    return IndependentExtractionResult(extraction, summary)


def _span_selection_diagnostic(
    response: object,
    bundle: SourceDocumentBundle,
    stable_subreason: str,
    field_path: str = "$",
) -> EvidenceValidationDiagnostic:
    byte_size, character_size = _diagnostic_dimensions(response)
    raw: object = response
    if type(response) is str:
        try:
            raw = json.loads(response)
        except json.JSONDecodeError:
            raw = None
    safe_keys = (
        tuple(sorted(_diagnostic_key(item) for item in raw))
        if type(raw) is dict
        else ()
    )
    actual = frozenset(raw) if type(raw) is dict else frozenset()
    version = (
        _safe_contract_version(
            raw.get("schema_version"), EVIDENCE_SPAN_SELECTION_CONTRACT_VERSION,
        )
        if type(raw) is dict
        else "missing"
    )
    binding = (
        "matched"
        if type(raw) is dict and raw.get("source_identity") == bundle.source_identity
        else "mismatched"
        if type(raw) is dict and type(raw.get("source_identity")) is str
        else "unavailable"
    )
    return EvidenceValidationDiagnostic(
        validation_stage=(
            "semantic_validation"
            if stable_subreason.startswith("selection_")
            else "top_level_schema"
        ),
        stable_subreason=stable_subreason,
        field_path=field_path,
        response_top_level_exact_type=_diagnostic_type(response),
        top_level_key_set=safe_keys,
        missing_keys=tuple(sorted(_SPAN_SELECTION_RESPONSE_KEYS - actual)),
        extra_keys=tuple(sorted(
            _diagnostic_key(item)
            for item in actual - _SPAN_SELECTION_RESPONSE_KEYS
        )),
        nested_field_types=(),
        list_item_counts=(
            (("$.selections", len(raw["selections"])),)
            if type(raw) is dict and type(raw.get("selections")) is list
            else ()
        ),
        schema_contract_version=version,
        span_quote_validation_category=(
            "rejected" if stable_subreason.startswith("selection_")
            else "not_applicable"
        ),
        source_identity_binding_result=binding,
        response_byte_size=byte_size,
        response_character_size=character_size,
    )


def _code_owned_atoms(
    selection_id: str,
    quote_id: str,
    exact_text: str,
) -> tuple[tuple[EvidenceAtom, ...], tuple[EvidenceAtom, ...]]:
    numbers = tuple(
        EvidenceAtom(
            f"number-{sha256(f'{selection_id}:{index}:number'.encode()).hexdigest()[:24]}",
            "number",
            quote_id,
            match.group(0),
        )
        for index, match in enumerate(_NUMBER.finditer(exact_text), start=1)
    )
    dates = tuple(
        EvidenceAtom(
            f"date-{sha256(f'{selection_id}:{index}:date'.encode()).hexdigest()[:24]}",
            "date",
            quote_id,
            match.group(0),
        )
        for index, match in enumerate(_DATE.finditer(exact_text), start=1)
    )
    return numbers, dates


def parse_span_selection_response(
    response: Mapping[str, object] | str,
    bundle: SourceDocumentBundle,
    inventory: SourceBlockInventory,
    plan: EvidenceCoveragePlan,
) -> IndependentExtractionResult:
    """Turn model-selected offsets into code-owned exact-span facts.

    The model is permitted to select only an existing segment and an absolute
    character interval.  It never supplies factual prose or semantic fields;
    every public fact and quote is derived from the persisted source bytes.
    """

    try:
        raw = _exact_mapping(
            _mapping_response(response), _SPAN_SELECTION_RESPONSE_KEYS,
        )
        if raw["schema_version"] != EVIDENCE_SPAN_SELECTION_CONTRACT_VERSION:
            _raise("evidence_schema_invalid")
        if (
            raw["source_identity"] != bundle.source_identity
            or raw["document_bundle_digest"] != bundle.bundle_digest
            or raw["coverage_plan_digest"] != plan.plan_digest
        ):
            _raise("evidence_source_binding_invalid")
        _safe_id(raw["run_id"], "run_id")
        selection_values = _exact_list(raw["selections"])
    except EvidenceContractError as error:
        if error.diagnostic is None:
            error.diagnostic = _span_selection_diagnostic(
                response,
                bundle,
                (
                    "source_or_document_binding_mismatch"
                    if error.reason_code == "evidence_source_binding_invalid"
                    else "schema_or_contract_invalid"
                ),
            )
        raise error from None
    except (TypeError, ValueError):
        diagnostic = _span_selection_diagnostic(
            response, bundle, "schema_or_contract_invalid",
        )
        raise EvidenceContractError("evidence_schema_invalid", diagnostic) from None

    selected_blocks = frozenset(
        item.block_id for item in plan.ordered_decisions
        if item.disposition == "evidence_candidate"
    )
    allowed_segment_ids = frozenset(
        segment_id
        for block in inventory.ordered_blocks
        if block.block_id in selected_blocks and block.sensitivity_status == "public"
        for segment_id in block.ordered_segment_ids
    )
    segment_by_id = {item.segment_id: item for item in _segments(bundle)}
    document_by_id = {
        item.document_id: item for item in bundle.ordered_documents
    }
    seen_ids: set[str] = set()
    seen_spans: set[tuple[str, int, int]] = set()
    accepted_spans: list[tuple[str, int, int]] = []
    accepted: list[SourceEvidence] = []
    rejection_counts = {
        "unknown_segment_count": 0,
        "invalid_span_count": 0,
        "duplicate_count": 0,
        "overlap_count": 0,
        "structural_metadata_only_count": 0,
        "sensitive_count": 0,
        "relation_bearing_span_count": 0,
        "too_short_count": 0,
        "too_long_count": 0,
    }
    for value in selection_values:
        try:
            selection = _exact_mapping(value, _SPAN_SELECTION_KEYS)
            selection_id = _safe_id(selection["selection_id"], "selection_id")
            segment_id = _safe_id(selection["segment_id"], "segment_id")
            start = _plain_int(selection["character_start"], "character_start")
            end = _plain_int(selection["character_end"], "character_end", minimum=1)
        except (EvidenceContractError, TypeError, ValueError):
            diagnostic = _span_selection_diagnostic(
                response, bundle, "schema_or_contract_invalid", "$.selections[]",
            )
            raise EvidenceContractError("evidence_schema_invalid", diagnostic) from None
        span_key = (segment_id, start, end)
        segment = segment_by_id.get(segment_id)
        document = (
            None if segment is None else document_by_id.get(segment.document_id)
        )
        if selection_id in seen_ids or span_key in seen_spans:
            rejection_counts["duplicate_count"] += 1
            continue
        seen_ids.add(selection_id)
        seen_spans.add(span_key)
        if segment is None or document is None or segment_id not in allowed_segment_ids:
            rejection_counts["unknown_segment_count"] += 1
            continue
        if (
            start < segment.character_start
            or end > segment.character_end
            or end <= start
        ):
            rejection_counts["invalid_span_count"] += 1
            continue
        if any(
            prior_segment == segment_id and start < prior_end and end > prior_start
            for prior_segment, prior_start, prior_end in accepted_spans
        ):
            rejection_counts["overlap_count"] += 1
            continue
        exact_text = document.exact_text[start:end]
        if not exact_text or exact_text != exact_text.strip():
            rejection_counts["invalid_span_count"] += 1
            continue
        if len(exact_text) < 3:
            rejection_counts["too_short_count"] += 1
            continue
        if len(exact_text) > 2000:
            rejection_counts["too_long_count"] += 1
            continue
        if segment.segment_kind in {"heading", "email_header"}:
            rejection_counts["structural_metadata_only_count"] += 1
            continue
        if _is_sensitive(exact_text):
            rejection_counts["sensitive_count"] += 1
            continue
        if _atomic_fact_has_relation_claim(exact_text):
            rejection_counts["relation_bearing_span_count"] += 1
            continue
        byte_start = len(document.exact_text[:start].encode("utf-8"))
        byte_end = len(document.exact_text[:end].encode("utf-8"))
        quote_id = (
            "quote-"
            + sha256(
                f"{selection_id}:{segment_id}:{start}:{end}".encode("utf-8")
            ).hexdigest()[:24]
        )
        quote = EvidenceQuote(
            quote_id,
            document.document_id,
            segment_id,
            byte_start,
            byte_end,
            start,
            end,
            exact_text,
        )
        numbers, dates = _code_owned_atoms(selection_id, quote_id, exact_text)
        fact = SourceEvidence(
            evidence_id=selection_id,
            proposition=exact_text,
            evidence_kind="observed_fact",
            ordered_segment_refs=(segment_id,),
            exact_quotes=(quote,),
            entities=(),
            numbers=numbers,
            dates=dates,
            polarity="negated" if _NEGATION.search(exact_text) else "affirmed",
            temporal_relation=None,
            causal_relation=None,
            uncertainty=(
                "uncertain" if _UNCERTAINTY.search(exact_text) else "certain"
            ),
            public_safety="safe",
        )
        try:
            _validate_one_atomic_fact(bundle, fact, raw["run_id"])
        except EvidenceContractError:
            rejection_counts["invalid_span_count"] += 1
            continue
        accepted.append(fact)
        accepted_spans.append(span_key)

    dispositions = _fact_dispositions(bundle, tuple(accepted))
    payload = {
        "source_identity": bundle.source_identity,
        "document_bundle_digest": bundle.bundle_digest,
        "contract_version": EVIDENCE_EXTRACTION_CONTRACT_VERSION,
        "run_id": raw["run_id"],
        "ordered_evidence": tuple(accepted),
        "ordered_segment_dispositions": dispositions,
    }
    extraction = EvidenceExtractionBundle(**payload, bundle_digest=_sha(payload))
    validate_extraction(bundle, extraction)
    rejected = sum(rejection_counts.values())
    summary = FactRelationValidationSummary(
        returned_fact_count=len(selection_values),
        valid_fact_count=len(accepted),
        rejected_fact_count=rejected,
        returned_relation_count=0,
        verified_relation_count=0,
        rejected_relation_count=0,
        temporal_conflict_count=0,
        causal_conflict_count=0,
        polarity_conflict_count=0,
        verified_fact_summaries=tuple(item.proposition for item in accepted[:7]),
    )
    receipt = EvidenceSelectionReceipt(
        returned_selection_count=len(selection_values),
        accepted_code_owned_fact_count=len(accepted),
        rejected_selection_count=rejected,
        verified_relation_count=0,
        **rejection_counts,
    )
    return IndependentExtractionResult(extraction, summary, receipt)


class GenericEvidenceService:
    """Legacy evidence flow plus an explicitly enabled coverage-v2 flow."""

    def __init__(
        self,
        client: EvidenceModelClient,
        *,
        extraction_model: str,
        adjudication_model: str,
        coverage_v2: bool = False,
    ):
        if not callable(getattr(client, "generate_json", None)):
            raise TypeError("client")
        self._client = client
        self.extraction_model = _plain(extraction_model, "extraction_model")
        self.adjudication_model = _plain(adjudication_model, "adjudication_model")
        if type(coverage_v2) is not bool:
            raise TypeError("coverage_v2")
        self.coverage_v2 = coverage_v2

    @staticmethod
    def _persist_stage(
        stage_sink: EvidenceStageSink | None,
        stage: str,
        payload: object,
    ) -> None:
        if stage_sink is None:
            return
        if not callable(stage_sink):
            raise TypeError("stage_sink")
        try:
            stage_sink(stage, payload)
        except EvidenceStagePersistenceError:
            raise
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except Exception as error:
            raise EvidenceStagePersistenceError("evidence stage persistence") from None

    def resolve(
        self,
        bundle: SourceDocumentBundle,
        *,
        stage_sink: EvidenceStageSink | None = None,
    ) -> EvidenceResolution:
        if type(bundle) is not SourceDocumentBundle:
            raise TypeError("bundle")
        if self.coverage_v2:
            return self._resolve_v2(bundle, stage_sink=stage_sink)
        classification = classify_source_bundle(bundle)
        if classification.classification == "insufficient":
            return EvidenceResolution("source_insufficient", None, 0, "evidence_source_insufficient")
        if classification.classification == "sensitive":
            return EvidenceResolution("sensitive_rejected", None, 0, "evidence_sensitive")
        if classification.classification in {"parse_error", "unsupported_binary_container"}:
            return EvidenceResolution("manual_attention", None, 0, "evidence_manual_attention")
        calls = 0
        try:
            projection = _source_projection(bundle)
            extraction_request = EvidenceModelRequest(
                "evidence_extraction",
                self.extraction_model,
                _canonical(projection).decode("utf-8"),
                EVIDENCE_EXTRACTION_CONTRACT_VERSION,
            )
            calls += 1
            extraction_raw = self._client.generate_json(extraction_request)
            extraction = parse_extraction_response(extraction_raw, bundle)
            adjudication_payload = {
                "source": projection,
                "extraction": _to_data(extraction),
            }
            adjudication_request = EvidenceModelRequest(
                "evidence_adjudication",
                self.adjudication_model,
                _canonical(adjudication_payload).decode("utf-8"),
                EVIDENCE_ADJUDICATION_CONTRACT_VERSION,
            )
            calls += 1
            adjudication_raw = self._client.generate_json(adjudication_request)
            adjudication = parse_adjudication_response(adjudication_raw, extraction)
        except EvidenceContractError as error:
            return EvidenceResolution("failed", None, calls, error.reason_code, error.diagnostic)
        except Exception:
            return EvidenceResolution("failed", None, calls, "evidence_provider_failed")
        dispositions = {item.disposition for item in extraction.ordered_segment_dispositions}
        decisions = {item.decision for item in adjudication.ordered_decisions}
        if "ambiguous" in dispositions or "ambiguous" in decisions or any(
            item.evidence_kind == "insufficient_or_ambiguous" or item.uncertainty == "ambiguous"
            for item in extraction.ordered_evidence
        ):
            return EvidenceResolution("manual_attention", None, calls, "evidence_manual_attention")
        if "rejected" in decisions:
            sensitive = any("sensitive_content" in item.reason_codes for item in adjudication.ordered_decisions)
            return EvidenceResolution(
                "sensitive_rejected" if sensitive else "source_insufficient",
                None,
                calls,
                "evidence_sensitive" if sensitive else "evidence_source_insufficient",
            )
        if not extraction.ordered_evidence or "supported" not in decisions:
            return EvidenceResolution(
                "source_insufficient",
                None,
                calls,
                "evidence_source_insufficient",
            )
        try:
            verified = _make_verified(bundle, extraction, adjudication)
        except EvidenceContractError as error:
            return EvidenceResolution("failed", None, calls, error.reason_code, error.diagnostic)
        return EvidenceResolution("verified", verified, calls, "evidence_verified")

    def _resolve_v2(
        self,
        bundle: SourceDocumentBundle,
        *,
        stage_sink: EvidenceStageSink | None = None,
    ) -> EvidenceResolution:
        classification = classify_source_bundle(bundle)
        if classification.classification == "insufficient":
            return EvidenceResolution("source_insufficient", None, 0, "evidence_source_insufficient")
        if classification.classification == "sensitive":
            return EvidenceResolution("sensitive_rejected", None, 0, "evidence_sensitive")
        if classification.classification in {"parse_error", "unsupported_binary_container"}:
            return EvidenceResolution("manual_attention", None, 0, "evidence_manual_attention")
        inventory = build_source_block_inventory(bundle)
        partition_failure = validate_source_block_partition(bundle, inventory)
        if partition_failure is not None:
            return EvidenceResolution(
                "manual_attention", None, 0, "evidence_manual_attention", None,
                partition_failure.summary, partition_failure,
            )
        calls = 0
        plan: EvidenceCoveragePlan | None = None
        try:
            coverage_payload = _block_projection(bundle, inventory)
            coverage_request = EvidenceModelRequest(
                "evidence_coverage",
                self.extraction_model,
                _canonical(coverage_payload).decode("utf-8"),
                EVIDENCE_COVERAGE_CONTRACT_VERSION,
                tuple(item.block_id for item in inventory.ordered_blocks),
            )
            calls += 1
            coverage_response = self._client.generate_json(coverage_request)
            if type(coverage_response) is CoverageFailureEvidence:
                boundary_failure = coverage_hard_failure(
                    inventory,
                    stable_reason=coverage_response.stable_reason,
                    source_binding_result=coverage_response.source_binding_result,
                    transport_diagnostic=coverage_response.transport_diagnostic,
                )
                return EvidenceResolution(
                    "failed", None, calls, "evidence_provider_failed", None,
                    boundary_failure.summary, boundary_failure,
                )
            plan = parse_coverage_response(
                coverage_response, bundle, inventory
            )
        except CoverageValidationError as error:
            summary = classify_coverage_failure(error.evidence)
            if summary is None:
                return EvidenceResolution(
                    "failed", None, calls, error.reason_code, None,
                    error.evidence.summary, error.evidence,
                )
            return EvidenceResolution(
                "manual_attention", None, calls, "evidence_manual_attention", None,
                summary, error.evidence,
            )
        except EvidenceContractError as error:
            return EvidenceResolution("failed", None, calls, error.reason_code, error.diagnostic)
        except Exception:
            return EvidenceResolution("failed", None, calls, "evidence_provider_failed")
        summary = _coverage_summary(inventory, plan, "coverage_complete")
        if summary.ambiguous_count or not summary.evidence_candidate_count:
            failure = _coverage_manual_failure(
                inventory,
                plan,
                stable_reason=(
                    "ambiguous_coverage" if summary.ambiguous_count else "no_evidence_candidate"
                ),
            )
            return EvidenceResolution(
                "manual_attention", None, calls, "evidence_manual_attention", None,
                failure.summary, failure,
            )
        selected = frozenset(
            item.block_id for item in plan.ordered_decisions
            if item.disposition == "evidence_candidate"
        )
        try:
            extraction_payload = _block_projection(
                bundle, inventory, selected_block_ids=selected
            )
            extraction_payload.update({
                "schema_version": EVIDENCE_SPAN_SELECTION_CONTRACT_VERSION,
                "coverage_plan_digest": plan.plan_digest,
            })
            allowed_segment_ids = tuple(
                segment_id
                for block in inventory.ordered_blocks
                if block.block_id in selected and block.sensitivity_status == "public"
                for segment_id in block.ordered_segment_ids
            )
            extraction_request = EvidenceModelRequest(
                "evidence_extraction",
                self.extraction_model,
                _canonical(extraction_payload).decode("utf-8"),
                EVIDENCE_SPAN_SELECTION_CONTRACT_VERSION,
                allowed_segment_ids,
            )
            calls += 1
            independent = parse_span_selection_response(
                self._client.generate_json(extraction_request), bundle, inventory, plan
            )
            extraction = independent.extraction
            fact_relation_summary = independent.summary
            if independent.selection_receipt is None:
                raise EvidenceStagePersistenceError("selection receipt missing")
            self._persist_stage(
                stage_sink,
                "code_owned_extraction",
                CodeOwnedExtractionCheckpoint(
                    plan.plan_digest,
                    extraction,
                    independent.selection_receipt,
                ),
            )
        except EvidenceStagePersistenceError:
            raise
        except EvidenceContractError as error:
            diagnostic = error.diagnostic
            if type(diagnostic) is not EvidenceValidationDiagnostic:
                diagnostic = _extraction_v2_diagnostic(
                    {}, bundle, error.reason_code, error._semantic_rejection,
                )
            failure = _post_extraction_failure(inventory, plan, diagnostic)
            return EvidenceResolution(
                (
                    "manual_attention"
                    if failure.category == "coverage_incomplete"
                    else "failed"
                ),
                None,
                calls,
                (
                    "evidence_manual_attention"
                    if failure.category == "coverage_incomplete"
                    else error.reason_code
                ),
                None,
                failure.summary,
                failure,
            )
        except Exception:
            return EvidenceResolution("failed", None, calls, "evidence_provider_failed", None, summary)
        if fact_relation_summary.valid_fact_count < MIN_INDEPENDENT_FACTS:
            failure = _coverage_manual_failure(
                inventory, plan, stable_reason="independent_fact_count_insufficient",
            )
            return EvidenceResolution(
                "manual_attention", None, calls, "evidence_manual_attention", None,
                failure.summary, failure, fact_relation_summary,
                selection_receipt=independent.selection_receipt,
            )
        try:
            adjudication_payload = {
                "source": _source_projection(bundle),
                "coverage_plan_digest": plan.plan_digest,
                "extraction": _to_data(extraction),
            }
            adjudication_request = EvidenceModelRequest(
                "evidence_adjudication",
                self.adjudication_model,
                _canonical(adjudication_payload).decode("utf-8"),
                EVIDENCE_ADJUDICATION_CONTRACT_VERSION,
            )
            calls += 1
            try:
                adjudication_response = self._client.generate_json(adjudication_request)
            except (KeyboardInterrupt, SystemExit, GeneratorExit):
                raise
            except Exception as error:
                diagnostic = adjudication_transport_diagnostic(error, extraction)
                self._persist_stage(stage_sink, "adjudication_diagnostic", diagnostic)
                return EvidenceResolution(
                    "failed", None, calls, "evidence_provider_failed", diagnostic,
                    summary, None, fact_relation_summary,
                    selection_receipt=independent.selection_receipt,
                )
            adjudication = parse_adjudication_response(
                adjudication_response, extraction
            )
            self._persist_stage(stage_sink, "adjudication_bundle", adjudication)
        except EvidenceStagePersistenceError:
            raise
        except EvidenceContractError as error:
            diagnostic = error.diagnostic
            if type(diagnostic) is not AdjudicationValidationDiagnostic:
                diagnostic = _adjudication_diagnostic(
                    None,
                    extraction,
                    validation_stage="adjudication_semantic_validation",
                    stable_reason="adjudication_contract_rejected",
                    field_path="$",
                    response_received=True,
                )
            self._persist_stage(stage_sink, "adjudication_diagnostic", diagnostic)
            return EvidenceResolution(
                "failed", None, calls, error.reason_code, diagnostic, summary,
                None, fact_relation_summary,
                selection_receipt=independent.selection_receipt,
            )
        except Exception:
            diagnostic = _adjudication_diagnostic(
                None,
                extraction,
                validation_stage="adjudication_semantic_validation",
                stable_reason="adjudication_internal_rejection",
                field_path="$",
                response_received=True,
            )
            self._persist_stage(stage_sink, "adjudication_diagnostic", diagnostic)
            return EvidenceResolution(
                "failed", None, calls, "evidence_provider_failed", diagnostic, summary,
                None, fact_relation_summary,
                selection_receipt=independent.selection_receipt,
            )
        decisions = {item.decision for item in adjudication.ordered_decisions}
        supported_ids = frozenset(
            item.evidence_id for item in adjudication.ordered_decisions
            if item.decision == "supported"
        )
        supported = tuple(
            item.proposition for item in extraction.ordered_evidence
            if item.evidence_id in supported_ids
        )[:7]
        supported_summary = FactRelationValidationSummary(
            returned_fact_count=len(extraction.ordered_evidence),
            valid_fact_count=len(supported_ids),
            rejected_fact_count=len(extraction.ordered_evidence) - len(supported_ids),
            returned_relation_count=0,
            verified_relation_count=0,
            rejected_relation_count=0,
            temporal_conflict_count=0,
            causal_conflict_count=0,
            polarity_conflict_count=0,
            verified_fact_summaries=supported,
        )
        if "ambiguous" in decisions or any(
            item.evidence_kind == "insufficient_or_ambiguous" or item.uncertainty == "ambiguous"
            for item in extraction.ordered_evidence
        ):
            failure = _coverage_manual_failure(
                inventory, plan, stable_reason="ambiguous_coverage",
            )
            return EvidenceResolution(
                "manual_attention", None, calls, "evidence_manual_attention", None,
                failure.summary, failure, supported_summary,
                selection_receipt=independent.selection_receipt,
            )
        if len(supported_ids) < MIN_INDEPENDENT_FACTS:
            supported = tuple(
                item.proposition for item in extraction.ordered_evidence
                if item.evidence_id in supported_ids
            )[:7]
            failure = _coverage_manual_failure(
                inventory,
                plan,
                stable_reason=(
                    "no_evidence_candidate"
                    if not supported_ids
                    else "independent_fact_count_insufficient"
                ),
            )
            return EvidenceResolution(
                "manual_attention", None, calls, "evidence_manual_attention", None,
                failure.summary, failure, supported_summary,
                selection_receipt=independent.selection_receipt,
            )
        try:
            verified = _make_verified(bundle, extraction, adjudication)
        except EvidenceContractError as error:
            diagnostic = AdjudicationValidationDiagnostic(
                category="blocked_adjudication",
                stable_reason="verified_bundle_revalidation_failed",
                validation_stage="verified_bundle_validation",
                field_path="$",
                response_exact_type="dict",
                schema_version=EVIDENCE_ADJUDICATION_CONTRACT_VERSION,
                source_binding_result="matched",
                extraction_bundle_binding_result="matched",
                expected_decision_count=len(extraction.ordered_evidence),
                returned_decision_count=len(adjudication.ordered_decisions),
                supported_decision_count=len(supported_ids),
                rejected_decision_count=sum(
                    item.decision == "rejected" for item in adjudication.ordered_decisions
                ),
                ambiguous_decision_count=sum(
                    item.decision == "ambiguous" for item in adjudication.ordered_decisions
                ),
                missing_decision_count=0,
                duplicate_decision_count=0,
                unknown_evidence_id_count=0,
                evidence_digest_mismatch_count=0,
                transport_attempt_count=1,
                response_received=True,
            )
            self._persist_stage(stage_sink, "adjudication_diagnostic", diagnostic)
            return EvidenceResolution(
                "failed", None, calls, error.reason_code, diagnostic, summary,
                None, supported_summary,
                selection_receipt=independent.selection_receipt,
            )
        return EvidenceResolution(
            "verified", verified, calls, "evidence_verified", None, summary,
            None, supported_summary,
            selection_receipt=independent.selection_receipt,
        )


def _extraction_payload(extraction: EvidenceExtractionBundle) -> dict[str, object]:
    return {
        "source_identity": extraction.source_identity,
        "document_bundle_digest": extraction.document_bundle_digest,
        "contract_version": extraction.contract_version,
        "run_id": extraction.run_id,
        "ordered_evidence": _to_data(extraction.ordered_evidence),
        "ordered_segment_dispositions": _to_data(extraction.ordered_segment_dispositions),
        "bundle_digest": extraction.bundle_digest,
    }


def _adjudication_payload(adjudication: EvidenceAdjudicationBundle) -> dict[str, object]:
    return {
        "source_identity": adjudication.source_identity,
        "extraction_bundle_digest": adjudication.extraction_bundle_digest,
        "contract_version": adjudication.contract_version,
        "run_id": adjudication.run_id,
        "ordered_decisions": _to_data(adjudication.ordered_decisions),
        "bundle_digest": adjudication.bundle_digest,
    }


def verified_bundle_to_payload(bundle: VerifiedEvidenceBundle) -> dict[str, object]:
    if type(bundle) is not VerifiedEvidenceBundle:
        raise TypeError("bundle")
    return {
        "contract_version": bundle.contract_version,
        "source_identity": bundle.source_identity,
        "document_bundle_digest": bundle.document_bundle_digest,
        "extraction": _extraction_payload(bundle.extraction),
        "adjudication": _adjudication_payload(bundle.adjudication),
        "accepted_evidence_ids": list(bundle.accepted_evidence_ids),
        "verified_bundle_digest": bundle.verified_bundle_digest,
    }


_PERSISTED_VERIFIED_KEYS = frozenset({
    "contract_version", "source_identity", "document_bundle_digest", "extraction", "adjudication",
    "accepted_evidence_ids", "verified_bundle_digest",
})
_PERSISTED_EXTRACTION_KEYS = frozenset({
    "source_identity", "document_bundle_digest", "contract_version", "run_id", "ordered_evidence",
    "ordered_segment_dispositions", "bundle_digest",
})
_PERSISTED_ADJUDICATION_KEYS = frozenset({
    "source_identity", "extraction_bundle_digest", "contract_version", "run_id", "ordered_decisions",
    "bundle_digest",
})


def verified_bundle_from_payload(
    payload: object,
    source_bundle: SourceDocumentBundle,
) -> VerifiedEvidenceBundle:
    raw = _exact_mapping(payload, _PERSISTED_VERIFIED_KEYS)
    extraction_raw = _exact_mapping(raw["extraction"], _PERSISTED_EXTRACTION_KEYS)
    adjudication_raw = _exact_mapping(raw["adjudication"], _PERSISTED_ADJUDICATION_KEYS)
    try:
        evidence = tuple(_evidence_from(item) for item in _exact_list(extraction_raw["ordered_evidence"]))
        dispositions = tuple(_disposition_from(item) for item in _exact_list(extraction_raw["ordered_segment_dispositions"]))
        extraction = EvidenceExtractionBundle(
            source_identity=extraction_raw["source_identity"],
            document_bundle_digest=extraction_raw["document_bundle_digest"],
            contract_version=extraction_raw["contract_version"],
            run_id=extraction_raw["run_id"],
            ordered_evidence=evidence,
            ordered_segment_dispositions=dispositions,
            bundle_digest=extraction_raw["bundle_digest"],
        )
        decisions = tuple(_decision_from(item) for item in _exact_list(adjudication_raw["ordered_decisions"]))
        adjudication = EvidenceAdjudicationBundle(
            source_identity=adjudication_raw["source_identity"],
            extraction_bundle_digest=adjudication_raw["extraction_bundle_digest"],
            contract_version=adjudication_raw["contract_version"],
            run_id=adjudication_raw["run_id"],
            ordered_decisions=decisions,
            bundle_digest=adjudication_raw["bundle_digest"],
        )
        verified = VerifiedEvidenceBundle(
            contract_version=raw["contract_version"],
            source_identity=raw["source_identity"],
            document_bundle_digest=raw["document_bundle_digest"],
            extraction=extraction,
            adjudication=adjudication,
            accepted_evidence_ids=tuple(_exact_list(raw["accepted_evidence_ids"])),
            verified_bundle_digest=raw["verified_bundle_digest"],
        )
    except EvidenceContractError:
        raise
    except (TypeError, ValueError):
        _raise("evidence_verified_bundle_invalid")
    revalidate_verified_bundle(source_bundle, verified)
    return verified


def revalidate_verified_bundle(
    source_bundle: SourceDocumentBundle,
    verified_bundle: VerifiedEvidenceBundle,
) -> VerifiedEvidenceBundle:
    if type(source_bundle) is not SourceDocumentBundle or type(verified_bundle) is not VerifiedEvidenceBundle:
        raise TypeError("verified bundle")
    if (
        verified_bundle.source_identity != source_bundle.source_identity
        or verified_bundle.document_bundle_digest != source_bundle.bundle_digest
        or verified_bundle.extraction.source_identity != source_bundle.source_identity
        or verified_bundle.adjudication.source_identity != source_bundle.source_identity
        or verified_bundle.adjudication.extraction_bundle_digest != verified_bundle.extraction.bundle_digest
    ):
        _raise("evidence_source_binding_invalid")
    validate_extraction(source_bundle, verified_bundle.extraction)
    expected = tuple(
        (item.evidence_id, evidence_digest(item))
        for item in verified_bundle.extraction.ordered_evidence
    )
    actual = tuple(
        (item.evidence_id, item.evidence_digest)
        for item in verified_bundle.adjudication.ordered_decisions
    )
    if actual != expected:
        _raise("evidence_adjudication_incomplete")
    reconstructed = _make_verified(source_bundle, verified_bundle.extraction, verified_bundle.adjudication)
    if reconstructed != verified_bundle:
        _raise("evidence_verified_bundle_invalid")
    return verified_bundle


def build_verified_fact_bindings(
    source_bundle: SourceDocumentBundle,
    verified_bundle: VerifiedEvidenceBundle,
) -> tuple[VerifiedFactBinding, ...]:
    revalidate_verified_bundle(source_bundle, verified_bundle)
    evidence_by_id = {item.evidence_id: item for item in verified_bundle.extraction.ordered_evidence}
    decision_by_id = {item.evidence_id: item for item in verified_bundle.adjudication.ordered_decisions}
    segment_order = {item.segment_id: index for index, item in enumerate(_segments(source_bundle))}
    ordered = sorted(
        (evidence_by_id[item] for item in verified_bundle.accepted_evidence_ids),
        key=lambda item: (
            min(segment_order[ref] for ref in item.ordered_segment_refs),
            min(quote.character_start for quote in item.exact_quotes),
            item.evidence_id,
        ),
    )
    bindings: list[VerifiedFactBinding] = []
    for order, evidence in enumerate(ordered, start=1):
        decision = decision_by_id[evidence.evidence_id]
        supporting_quote = next(
            item for item in evidence.exact_quotes
            if item.exact_text == evidence.proposition
        )
        public_anchor_labels = tuple(
            [*_proposition_anchor_labels(evidence.proposition)]
            + [*(f"entity:{item.exact_lexeme}" for item in evidence.entities)]
            + [*(f"number:{item.exact_lexeme}" for item in evidence.numbers)]
            + [*(f"date:{item.exact_lexeme}" for item in evidence.dates)]
            + [f"polarity:{evidence.polarity}"]
            + ([] if evidence.temporal_relation is None else [f"temporal:{evidence.temporal_relation.relation_kind}"])
            + ([] if evidence.causal_relation is None else [f"causal:{evidence.causal_relation.relation_kind}"])
            + [f"uncertainty:{evidence.uncertainty}"]
        )
        meaning_anchor_ids = tuple(
            f"meaning-{index:03d}-{sha256(label.encode('utf-8')).hexdigest()[:16]}"
            for index, label in enumerate(public_anchor_labels, start=1)
        )
        bindings.append(VerifiedFactBinding(
            binding_version=VERIFIED_FACT_BINDING_VERSION,
            fact_id=f"fact-{order}",
            evidence_id=evidence.evidence_id,
            evidence_digest=evidence_digest(evidence),
            exact_supporting_quote=supporting_quote.exact_text,
            public_proposition=evidence.proposition,
            ordered_segment_refs=evidence.ordered_segment_refs,
            source_identity=source_bundle.source_identity,
            order=order,
            numbers=tuple(item.exact_lexeme for item in evidence.numbers),
            entities=tuple(item.exact_lexeme for item in evidence.entities),
            dates=tuple(item.exact_lexeme for item in evidence.dates),
            polarity=evidence.polarity,
            temporal_relation=None if evidence.temporal_relation is None else evidence.temporal_relation.relation_kind,
            causal_relation=None if evidence.causal_relation is None else evidence.causal_relation.relation_kind,
            uncertainty=evidence.uncertainty,
            adjudication_identity=_sha({
                "adjudication_bundle_digest": verified_bundle.adjudication.bundle_digest,
                "decision": decision,
            }),
            meaning_anchor_ids=meaning_anchor_ids,
            public_anchor_labels=public_anchor_labels,
        ))
    return tuple(bindings)


_VERIFIED_FACT_BINDING_KEYS = frozenset({
    "binding_version",
    "fact_id",
    "evidence_id",
    "evidence_digest",
    "exact_supporting_quote",
    "public_proposition",
    "ordered_segment_refs",
    "source_identity",
    "order",
    "numbers",
    "entities",
    "dates",
    "polarity",
    "temporal_relation",
    "causal_relation",
    "uncertainty",
    "adjudication_identity",
    "meaning_anchor_ids",
    "public_anchor_labels",
})


def verified_fact_binding_to_payload(binding: VerifiedFactBinding) -> dict[str, object]:
    """Return the strict JSON-safe projection of one verified fact binding."""

    if type(binding) is not VerifiedFactBinding:
        raise TypeError("binding")
    return {
        "binding_version": binding.binding_version,
        "fact_id": binding.fact_id,
        "evidence_id": binding.evidence_id,
        "evidence_digest": binding.evidence_digest,
        "exact_supporting_quote": binding.exact_supporting_quote,
        "public_proposition": binding.public_proposition,
        "ordered_segment_refs": list(binding.ordered_segment_refs),
        "source_identity": binding.source_identity,
        "order": binding.order,
        "numbers": list(binding.numbers),
        "entities": list(binding.entities),
        "dates": list(binding.dates),
        "polarity": binding.polarity,
        "temporal_relation": binding.temporal_relation,
        "causal_relation": binding.causal_relation,
        "uncertainty": binding.uncertainty,
        "adjudication_identity": binding.adjudication_identity,
        "meaning_anchor_ids": list(binding.meaning_anchor_ids),
        "public_anchor_labels": list(binding.public_anchor_labels),
    }


def verified_fact_binding_from_payload(value: object) -> VerifiedFactBinding:
    """Strictly replay a binding without requiring raw source documents."""

    raw = _exact_mapping(value, _VERIFIED_FACT_BINDING_KEYS)
    try:
        return VerifiedFactBinding(
            binding_version=raw["binding_version"],
            fact_id=raw["fact_id"],
            evidence_id=raw["evidence_id"],
            evidence_digest=raw["evidence_digest"],
            exact_supporting_quote=raw["exact_supporting_quote"],
            public_proposition=raw["public_proposition"],
            ordered_segment_refs=tuple(_exact_list(raw["ordered_segment_refs"])),
            source_identity=raw["source_identity"],
            order=raw["order"],
            numbers=tuple(_exact_list(raw["numbers"])),
            entities=tuple(_exact_list(raw["entities"])),
            dates=tuple(_exact_list(raw["dates"])),
            polarity=raw["polarity"],
            temporal_relation=raw["temporal_relation"],
            causal_relation=raw["causal_relation"],
            uncertainty=raw["uncertainty"],
            adjudication_identity=raw["adjudication_identity"],
            meaning_anchor_ids=tuple(_exact_list(raw["meaning_anchor_ids"])),
            public_anchor_labels=tuple(_exact_list(raw["public_anchor_labels"])),
        )
    except EvidenceContractError:
        raise
    except (TypeError, ValueError):
        _raise("evidence_verified_bundle_invalid")


__all__ = [
    "ADJUDICATION_DIAGNOSTIC_VERSION",
    "ADJUDICATION_REASON_CODES",
    "CAUSAL_RELATIONS",
    "COVERAGE_CLASSIFICATIONS",
    "COVERAGE_FAILURE_REASONS",
    "BLOCK_DISPOSITIONS",
    "EVIDENCE_COVERAGE_CONTRACT_VERSION",
    "EVIDENCE_ADJUDICATION_CONTRACT_VERSION",
    "EVIDENCE_EXTRACTION_CONTRACT_VERSION",
    "EVIDENCE_EXTRACTION_V2_CONTRACT_VERSION",
    "EVIDENCE_EXTRACTION_V3_CONTRACT_VERSION",
    "EVIDENCE_SPAN_SELECTION_CONTRACT_VERSION",
    "EVIDENCE_SELECTION_RECEIPT_VERSION",
    "EVIDENCE_KINDS",
    "EvidenceAdjudicationBundle",
    "AdjudicationValidationDiagnostic",
    "CodeOwnedExtractionCheckpoint",
    "EvidenceCoveragePlan",
    "EvidenceCoverageSummary",
    "FactRelationValidationSummary",
    "CoverageFailureEvidence",
    "ProviderTransportDiagnostic",
    "CoverageValidationError",
    "EvidenceAtom",
    "EvidenceContractError",
    "EvidenceDecision",
    "EvidenceExtractionBundle",
    "EvidenceModelClient",
    "EvidenceModelRequest",
    "EvidenceQuote",
    "EvidenceRelation",
    "EvidenceResolution",
    "EvidenceSelectionReceipt",
    "EvidenceStagePersistenceError",
    "EvidenceValidationDiagnostic",
    "GenericEvidenceService",
    "PUBLIC_SAFETY",
    "PROVIDER_TRANSPORT_DIAGNOSTIC_VERSION",
    "REASON_CODES",
    "RESOLUTION_STATUSES",
    "SOURCE_DOCUMENT_CONTRACT_VERSION",
    "SEGMENT_DISPOSITIONS",
    "SourceDocument",
    "SourceDocumentBundle",
    "SourceEvidence",
    "SourceSegment",
    "SourceBlock",
    "SourceBlockInventory",
    "BlockCoverageDecision",
    "SegmentDisposition",
    "TEMPORAL_RELATIONS",
    "VERIFIED_EVIDENCE_CONTRACT_VERSION",
    "VERIFIED_FACT_BINDING_VERSION",
    "VerifiedEvidenceBundle",
    "VerifiedFactBinding",
    "build_source_document_bundle",
    "build_source_block_inventory",
    "build_verified_fact_bindings",
    "classify_source_bundle",
    "classify_coverage_failure",
    "coverage_failure_from_payload",
    "coverage_hard_failure",
    "coverage_hard_failure_for_request",
    "materializable_post_extraction_failure",
    "provider_transport_diagnostic_from_payload",
    "evidence_digest",
    "evidence_model_response_schema",
    "parse_adjudication_response",
    "parse_extraction_response",
    "parse_coverage_response",
    "parse_extraction_v2_response",
    "parse_extraction_v3_response",
    "parse_span_selection_response",
    "revalidate_verified_bundle",
    "source_identity",
    "validate_extraction",
    "validate_source_block_partition",
    "verified_bundle_from_payload",
    "verified_bundle_to_payload",
    "verified_fact_binding_from_payload",
    "verified_fact_binding_to_payload",
]
