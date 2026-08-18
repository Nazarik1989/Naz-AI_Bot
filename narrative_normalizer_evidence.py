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

from dataclasses import asdict, dataclass, fields, is_dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Mapping, Protocol, Sequence
import json
import re


SOURCE_DOCUMENT_CONTRACT_VERSION = "normalizer-source-document-v1"
EVIDENCE_EXTRACTION_CONTRACT_VERSION = "normalizer-evidence-extraction-v1"
EVIDENCE_ADJUDICATION_CONTRACT_VERSION = "normalizer-evidence-adjudication-v1"
VERIFIED_EVIDENCE_CONTRACT_VERSION = "normalizer-verified-evidence-v1"
VERIFIED_FACT_BINDING_VERSION = "normalizer-verified-fact-binding-v1"

MAX_DOCUMENT_BYTES = 2_000_000
MAX_SOURCE_BYTES = 8_000_000
MAX_DOCUMENTS = 128
MAX_SEGMENTS = 20_000

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
POLARITIES = frozenset({"affirmed", "negated", "quoted"})
UNCERTAINTIES = frozenset({"certain", "uncertain", "ambiguous"})
PUBLIC_SAFETY = frozenset({"safe", "sensitive", "mixed", "unknown"})
SEGMENT_DISPOSITIONS = frozenset({"evidence", "irrelevant", "sensitive", "ambiguous"})
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
_SECRET = re.compile(
    r"(?:api[_-]?key|token|secret|password|authorization|credential)\s*[:=]\s*\S+|"
    r"(?:sk|ghp|xox[baprs])[-_A-Za-z0-9]{12,}",
    re.IGNORECASE,
)
_ABSOLUTE_PATH = re.compile(
    r"(?:[A-Za-z]:[\\/]|(?<![A-Za-z0-9._-])/(?!/)[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*)"
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


class EvidenceContractError(ValueError):
    """Privacy-safe public failure for the evidence boundary."""

    def __init__(self, reason_code: str):
        code = reason_code if reason_code in REASON_CODES else "evidence_schema_invalid"
        super().__init__(code)
        self.reason_code = code
        self.__cause__ = None
        self.__context__ = None


def _raise(reason_code: str) -> None:
    raise EvidenceContractError(reason_code) from None


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
    return bool(_SECRET.search(text) or _ABSOLUTE_PATH.search(text))


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

    def __post_init__(self) -> None:
        if self.request_kind not in {"evidence_extraction", "evidence_adjudication"}:
            raise ValueError("request_kind")
        _plain(self.model, "model")
        _plain(self.payload_json, "payload_json")
        _plain(self.response_schema_version, "response_schema_version")


class EvidenceModelClient(Protocol):
    def generate_json(self, request: EvidenceModelRequest) -> Mapping[str, object] | str:
        """Return one strict extraction or adjudication response."""


@dataclass(frozen=True, slots=True)
class EvidenceResolution:
    status: str
    verified_bundle: VerifiedEvidenceBundle | None
    model_call_count: int
    reason_code: str

    def __post_init__(self) -> None:
        _enum(self.status, RESOLUTION_STATUSES, "status")
        _plain_int(self.model_call_count, "model_call_count")
        if self.model_call_count > 2:
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
_DECISION_KEYS = frozenset({"evidence_id", "evidence_digest", "decision", "reason_codes"})
_ADJUDICATION_RESPONSE_KEYS = frozenset({
    "schema_version", "source_identity", "extraction_bundle_digest", "run_id", "decisions",
})


def _quote_from(value: object) -> EvidenceQuote:
    raw = _exact_mapping(value, _QUOTE_KEYS)
    try:
        return EvidenceQuote(**raw)
    except (TypeError, ValueError):
        _raise("evidence_schema_invalid")


def _atom_from(value: object, expected_kind: str) -> EvidenceAtom:
    raw = _exact_mapping(value, _ATOM_KEYS)
    try:
        atom = EvidenceAtom(**raw)
    except (TypeError, ValueError):
        _raise("evidence_schema_invalid")
    if atom.atom_kind != expected_kind:
        _raise("evidence_schema_invalid")
    return atom


def _relation_from(value: object, allowed: frozenset[str]) -> EvidenceRelation | None:
    if value is None:
        return None
    raw = _exact_mapping(value, _RELATION_KEYS)
    try:
        relation = EvidenceRelation(
            relation_kind=raw["relation_kind"],
            marker_quote_id=raw["marker_quote_id"],
            left_operand_quote_ids=tuple(_exact_list(raw["left_operand_quote_ids"])),
            right_operand_quote_ids=tuple(_exact_list(raw["right_operand_quote_ids"])),
        )
    except (TypeError, ValueError):
        _raise("evidence_schema_invalid")
    if relation.relation_kind not in allowed:
        _raise("evidence_schema_invalid")
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
            temporal_relation=_relation_from(raw["temporal_relation"], TEMPORAL_RELATIONS),
            causal_relation=_relation_from(raw["causal_relation"], CAUSAL_RELATIONS),
            uncertainty=raw["uncertainty"],
            public_safety=raw["public_safety"],
        )
    except EvidenceContractError:
        raise
    except (TypeError, ValueError):
        _raise("evidence_schema_invalid")


def _disposition_from(value: object) -> SegmentDisposition:
    raw = _exact_mapping(value, _DISPOSITION_KEYS)
    try:
        return SegmentDisposition(
            raw["segment_id"],
            raw["disposition"],
            tuple(_exact_list(raw["ordered_evidence_ids"])),
        )
    except (TypeError, ValueError):
        _raise("evidence_schema_invalid")


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
    raw = _exact_mapping(_mapping_response(response), _EXTRACTION_RESPONSE_KEYS)
    if (
        raw["schema_version"] != EVIDENCE_EXTRACTION_CONTRACT_VERSION
        or raw["source_identity"] != source_bundle.source_identity
        or raw["document_bundle_digest"] != source_bundle.bundle_digest
    ):
        _raise("evidence_source_binding_invalid")
    try:
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
    except EvidenceContractError:
        raise
    except (TypeError, ValueError):
        _raise("evidence_schema_invalid")
    validate_extraction(source_bundle, result)
    return result


def evidence_digest(evidence: SourceEvidence) -> str:
    if type(evidence) is not SourceEvidence:
        raise TypeError("evidence")
    return _sha(evidence)


def _quote_order_key(quote: EvidenceQuote, *, end: bool = False) -> tuple[str, int]:
    return (quote.document_id, quote.character_end if end else quote.character_start)


def _validate_relation(relation: EvidenceRelation, quotes: Mapping[str, EvidenceQuote]) -> None:
    all_ids = {relation.marker_quote_id, *relation.left_operand_quote_ids, *relation.right_operand_quote_ids}
    if any(item not in quotes for item in all_ids):
        _raise("evidence_relation_binding_invalid")
    if relation.marker_quote_id in relation.left_operand_quote_ids or relation.marker_quote_id in relation.right_operand_quote_ids:
        _raise("evidence_relation_binding_invalid")
    marker = quotes[relation.marker_quote_id].exact_text
    rules = _TEMPORAL_MARKERS if relation.relation_kind in TEMPORAL_RELATIONS else _CAUSAL_MARKERS
    if rules[relation.relation_kind].search(marker) is None:
        _raise("evidence_relation_binding_invalid")
    marker_quote = quotes[relation.marker_quote_id]
    left_quotes = tuple(quotes[item] for item in relation.left_operand_quote_ids)
    right_quotes = tuple(quotes[item] for item in relation.right_operand_quote_ids)
    if (
        max((_quote_order_key(item, end=True) for item in left_quotes)) > _quote_order_key(marker_quote)
        or _quote_order_key(marker_quote, end=True) > min((_quote_order_key(item) for item in right_quotes))
    ):
        _raise("evidence_relation_binding_invalid")


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
        _raise("evidence_schema_invalid")
    disposition_ids = tuple(item.segment_id for item in extraction.ordered_segment_dispositions)
    if disposition_ids != tuple(item.segment_id for item in segments) or len(disposition_ids) != len(set(disposition_ids)):
        _raise("evidence_segment_binding_invalid")
    disposition_by_id = {item.segment_id: item for item in extraction.ordered_segment_dispositions}
    known_evidence = set(evidence_ids)
    refs_by_evidence = {
        item.evidence_id: frozenset(item.ordered_segment_refs)
        for item in extraction.ordered_evidence
    }
    for disposition in extraction.ordered_segment_dispositions:
        segment = segment_by_id[disposition.segment_id]
        if any(item not in known_evidence for item in disposition.ordered_evidence_ids):
            _raise("evidence_segment_binding_invalid")
        if any(disposition.segment_id not in refs_by_evidence[item] for item in disposition.ordered_evidence_ids):
            _raise("evidence_segment_binding_invalid")
        if _is_sensitive(segment.exact_text) and disposition.disposition != "sensitive":
            _raise("evidence_sensitive")
    global_quotes: set[str] = set()
    for evidence in extraction.ordered_evidence:
        if _is_sensitive(evidence.proposition):
            _raise("evidence_sensitive")
        if evidence.ordered_segment_refs:
            try:
                positions = tuple(segment_order[item] for item in evidence.ordered_segment_refs)
            except KeyError:
                _raise("evidence_segment_binding_invalid")
            if positions != tuple(sorted(positions)):
                _raise("evidence_segment_binding_invalid")
        for segment_id in evidence.ordered_segment_refs:
            disposition = disposition_by_id[segment_id]
            if disposition.disposition != "evidence" or evidence.evidence_id not in disposition.ordered_evidence_ids:
                _raise("evidence_segment_binding_invalid")
        quotes: dict[str, EvidenceQuote] = {}
        for quote in evidence.exact_quotes:
            if quote.quote_id in global_quotes or quote.quote_id in quotes:
                _raise("evidence_quote_binding_invalid")
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
                _raise("evidence_quote_binding_invalid")
            raw = document.exact_text.encode("utf-8")
            try:
                byte_text = raw[quote.byte_start:quote.byte_end].decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                _raise("evidence_quote_binding_invalid")
            if document.exact_text[quote.character_start:quote.character_end] != quote.exact_text or byte_text != quote.exact_text:
                _raise("evidence_quote_binding_invalid")
        # The extractor's prose is not evidence.  A public proposition must
        # be an exact, independently replayable source span; adjudication may
        # accept or reject that span but cannot authorize invented wording.
        # Relation evidence can include additional operand/marker quotes as
        # long as one quote is the complete asserted proposition.
        if (
            evidence.evidence_kind != "insufficient_or_ambiguous"
            and evidence.proposition not in {item.exact_text for item in quotes.values()}
        ):
            _raise("evidence_proposition_binding_invalid")
        all_atoms = (*evidence.entities, *evidence.numbers, *evidence.dates)
        atom_ids = tuple(item.atom_id for item in all_atoms)
        if len(atom_ids) != len(set(atom_ids)):
            _raise("evidence_value_binding_invalid")
        for atom in all_atoms:
            quote = quotes.get(atom.quote_id)
            if quote is None or not _contains_bound_lexeme(quote.exact_text, atom.exact_lexeme, atom.atom_kind):
                _raise("evidence_value_binding_invalid")
            if atom.atom_kind == "number" and _NUMBER.fullmatch(atom.exact_lexeme) is None:
                _raise("evidence_value_binding_invalid")
            if atom.atom_kind == "date" and not _valid_date_lexeme(atom.exact_lexeme):
                _raise("evidence_value_binding_invalid")
        if evidence.temporal_relation is not None:
            _validate_relation(evidence.temporal_relation, quotes)
        if evidence.causal_relation is not None:
            _validate_relation(evidence.causal_relation, quotes)
        if evidence.temporal_relation is not None and evidence.causal_relation is not None:
            _raise("evidence_relation_binding_invalid")
        if evidence.evidence_kind == "explicit_sequence" and (
            evidence.temporal_relation is None or evidence.temporal_relation.relation_kind != "sequence"
        ):
            _raise("evidence_relation_binding_invalid")
        if evidence.evidence_kind == "explicit_cause" and evidence.causal_relation is None:
            _raise("evidence_relation_binding_invalid")
        quote_text = "\n".join(item.exact_text for item in evidence.exact_quotes)
        negated = _NEGATION.search(quote_text) is not None
        if (evidence.polarity == "negated") != negated and evidence.polarity != "quoted":
            _raise("evidence_polarity_invalid")
        uncertain = _UNCERTAINTY.search(quote_text) is not None
        if evidence.uncertainty == "certain" and uncertain:
            _raise("evidence_uncertainty_invalid")
        if evidence.uncertainty == "uncertain" and not uncertain:
            _raise("evidence_uncertainty_invalid")
        if evidence.public_safety == "safe" and any(_is_sensitive(item.exact_text) for item in evidence.exact_quotes):
            _raise("evidence_sensitive")


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


def parse_adjudication_response(
    response: Mapping[str, object] | str,
    extraction: EvidenceExtractionBundle,
) -> EvidenceAdjudicationBundle:
    raw = _exact_mapping(_mapping_response(response), _ADJUDICATION_RESPONSE_KEYS)
    if (
        raw["schema_version"] != EVIDENCE_ADJUDICATION_CONTRACT_VERSION
        or raw["source_identity"] != extraction.source_identity
        or raw["extraction_bundle_digest"] != extraction.bundle_digest
    ):
        _raise("evidence_adjudication_conflict")
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
        raise
    except (TypeError, ValueError):
        _raise("evidence_schema_invalid")
    expected = tuple((item.evidence_id, evidence_digest(item)) for item in extraction.ordered_evidence)
    actual = tuple((item.evidence_id, item.evidence_digest) for item in result.ordered_decisions)
    if actual != expected or len(actual) != len(set(item[0] for item in actual)):
        _raise("evidence_adjudication_incomplete")
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


class GenericEvidenceService:
    """Exactly one extraction and one adjudication call on a valid generic path."""

    def __init__(
        self,
        client: EvidenceModelClient,
        *,
        extraction_model: str,
        adjudication_model: str,
    ):
        if not callable(getattr(client, "generate_json", None)):
            raise TypeError("client")
        self._client = client
        self.extraction_model = _plain(extraction_model, "extraction_model")
        self.adjudication_model = _plain(adjudication_model, "adjudication_model")

    def resolve(self, bundle: SourceDocumentBundle) -> EvidenceResolution:
        if type(bundle) is not SourceDocumentBundle:
            raise TypeError("bundle")
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
            return EvidenceResolution("failed", None, calls, error.reason_code)
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
            return EvidenceResolution("failed", None, calls, error.reason_code)
        return EvidenceResolution("verified", verified, calls, "evidence_verified")


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
    "ADJUDICATION_REASON_CODES",
    "CAUSAL_RELATIONS",
    "COVERAGE_CLASSIFICATIONS",
    "EVIDENCE_ADJUDICATION_CONTRACT_VERSION",
    "EVIDENCE_EXTRACTION_CONTRACT_VERSION",
    "EVIDENCE_KINDS",
    "EvidenceAdjudicationBundle",
    "EvidenceAtom",
    "EvidenceContractError",
    "EvidenceDecision",
    "EvidenceExtractionBundle",
    "EvidenceModelClient",
    "EvidenceModelRequest",
    "EvidenceQuote",
    "EvidenceRelation",
    "EvidenceResolution",
    "GenericEvidenceService",
    "PUBLIC_SAFETY",
    "REASON_CODES",
    "RESOLUTION_STATUSES",
    "SOURCE_DOCUMENT_CONTRACT_VERSION",
    "SEGMENT_DISPOSITIONS",
    "SourceDocument",
    "SourceDocumentBundle",
    "SourceEvidence",
    "SourceSegment",
    "SegmentDisposition",
    "TEMPORAL_RELATIONS",
    "VERIFIED_EVIDENCE_CONTRACT_VERSION",
    "VERIFIED_FACT_BINDING_VERSION",
    "VerifiedEvidenceBundle",
    "VerifiedFactBinding",
    "build_source_document_bundle",
    "build_verified_fact_bindings",
    "classify_source_bundle",
    "evidence_digest",
    "parse_adjudication_response",
    "parse_extraction_response",
    "revalidate_verified_bundle",
    "source_identity",
    "validate_extraction",
    "verified_bundle_from_payload",
    "verified_bundle_to_payload",
    "verified_fact_binding_from_payload",
    "verified_fact_binding_to_payload",
]
