from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path
import json

import pytest

import narrative_normalizer_evidence as evidence
import narrative_normalizer_provider as provider


SOURCE_REF = "Project/2026-08-17"
SOURCE_DIGEST = "ab" * 32
SOURCE_CONTRACT_VERSION = "agent-content-source-v1"


def _write_bundle(
    tmp_path: Path,
    files: dict[str, str | bytes] | None = None,
    *,
    source_ref: str = SOURCE_REF,
) -> evidence.SourceDocumentBundle:
    root = tmp_path / "inbox"
    source = root.joinpath(*source_ref.split("/"))
    source.mkdir(parents=True)
    for name, value in (files if files is not None else {"story.txt": "Naz opened the blue notebook."}).items():
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value if type(value) is bytes else value.encode("utf-8"))
    return evidence.build_source_document_bundle(
        root,
        source_ref,
        SOURCE_DIGEST,
        SOURCE_CONTRACT_VERSION,
    )


def _all_segments(bundle: evidence.SourceDocumentBundle) -> tuple[evidence.SourceSegment, ...]:
    return tuple(segment for document in bundle.ordered_documents for segment in document.ordered_segments)


def _quote_payload(
    bundle: evidence.SourceDocumentBundle,
    segment: evidence.SourceSegment,
    exact_text: str | None = None,
    *,
    quote_id: str = "quote-1",
) -> dict[str, object]:
    exact = segment.exact_text if exact_text is None else exact_text
    local = segment.exact_text.index(exact)
    character_start = segment.character_start + local
    character_end = character_start + len(exact)
    document = next(item for item in bundle.ordered_documents if item.document_id == segment.document_id)
    byte_start = len(document.exact_text[:character_start].encode("utf-8"))
    byte_end = len(document.exact_text[:character_end].encode("utf-8"))
    return {
        "quote_id": quote_id,
        "document_id": segment.document_id,
        "segment_id": segment.segment_id,
        "byte_start": byte_start,
        "byte_end": byte_end,
        "character_start": character_start,
        "character_end": character_end,
        "exact_text": exact,
    }


def _base_extraction_payload(
    bundle: evidence.SourceDocumentBundle,
    *,
    segment_index: int = 0,
) -> dict[str, object]:
    segments = _all_segments(bundle)
    selected = segments[segment_index]
    quote = _quote_payload(bundle, selected)
    negated = evidence._NEGATION.search(selected.exact_text) is not None
    uncertain = evidence._UNCERTAINTY.search(selected.exact_text) is not None
    item = {
        "evidence_id": "evidence-1",
        "proposition": selected.exact_text,
        "evidence_kind": "observed_fact",
        "ordered_segment_refs": [selected.segment_id],
        "exact_quotes": [quote],
        "entities": [],
        "numbers": [],
        "dates": [],
        "polarity": "negated" if negated else "affirmed",
        "temporal_relation": None,
        "causal_relation": None,
        "uncertainty": "uncertain" if uncertain else "certain",
        "public_safety": "safe",
    }
    dispositions = []
    for segment in segments:
        if segment.segment_id == selected.segment_id:
            disposition = "evidence"
            evidence_ids = ["evidence-1"]
        elif evidence._is_sensitive(segment.exact_text):
            disposition = "sensitive"
            evidence_ids = []
        else:
            disposition = "irrelevant"
            evidence_ids = []
        dispositions.append({
            "segment_id": segment.segment_id,
            "disposition": disposition,
            "ordered_evidence_ids": evidence_ids,
        })
    return {
        "schema_version": evidence.EVIDENCE_EXTRACTION_CONTRACT_VERSION,
        "source_identity": bundle.source_identity,
        "document_bundle_digest": bundle.bundle_digest,
        "run_id": "extract-run-1",
        "evidence": [item],
        "segment_dispositions": dispositions,
    }


def _parse_base(bundle: evidence.SourceDocumentBundle) -> evidence.EvidenceExtractionBundle:
    return evidence.parse_extraction_response(_base_extraction_payload(bundle), bundle)


def _adjudication_payload(
    extraction: evidence.EvidenceExtractionBundle,
    *,
    decision: str = "supported",
    reason_codes: list[str] | None = None,
) -> dict[str, object]:
    reasons = [] if reason_codes is None and decision == "supported" else (reason_codes or ["unsupported_proposition"])
    return {
        "schema_version": evidence.EVIDENCE_ADJUDICATION_CONTRACT_VERSION,
        "source_identity": extraction.source_identity,
        "extraction_bundle_digest": extraction.bundle_digest,
        "run_id": evidence.adjudication_run_id(extraction),
        "decisions": [
            {
                "evidence_id": item.evidence_id,
                "decision": decision,
                "reason_codes": reasons,
            }
            for item in extraction.ordered_evidence
        ],
    }


class FakeEvidenceClient:
    def __init__(self, responses: list[object]):
        self.responses = list(responses)
        self.requests: list[evidence.EvidenceModelRequest] = []

    def generate_json(self, request: evidence.EvidenceModelRequest) -> object:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _verified(
    bundle: evidence.SourceDocumentBundle,
) -> tuple[evidence.VerifiedEvidenceBundle, FakeEvidenceClient]:
    extraction_raw = _base_extraction_payload(bundle)
    extraction = evidence.parse_extraction_response(extraction_raw, bundle)
    client = FakeEvidenceClient([extraction_raw, _adjudication_payload(extraction)])
    service = evidence.GenericEvidenceService(
        client,
        extraction_model="extract-model-v1",
        adjudication_model="judge-model-v1",
    )
    result = service.resolve(bundle)
    assert result.status == "verified"
    assert result.verified_bundle is not None
    return result.verified_bundle, client


SEGMENTATION_CASES = [
    ("ascii", "story.txt", "Alpha opened the notebook.\n", "plain_text", "text_line", 1),
    ("utf8-ru", "story.txt", "Наз открыл блокнот.\n", "plain_text", "text_line", 1),
    ("emoji", "story.txt", "Naz marked the page 🟦.\n", "plain_text", "text_line", 1),
    ("crlf", "story.txt", "First line.\r\nSecond line.\r\n", "plain_text", "text_line", 2),
    ("no-final-newline", "story.txt", "A final line", "plain_text", "text_line", 1),
    ("blank-lines", "story.txt", "One.\n\nTwo.\n", "plain_text", "text_line", 2),
    ("markdown-heading", "story.md", "# Heading\nBody\n", "markdown", "heading", 2),
    ("markdown-list", "story.md", "- First\n- Second\n", "markdown", "list_item", 2),
    ("markdown-code", "story.md", "```\nvalue = 3\n```\n", "markdown", "code_block", 3),
    ("log", "story.log", "2026-08-17 INFO started\nERROR stopped\n", "log", "log_entry", 2),
    ("yaml", "story.yaml", "owner: Naz\ncount: 2\n", "key_value", "key_value_entry", 2),
    ("email", "story.txt", "From: Naz\nTo: Void\n\nMessage body\n", "email", "email_header", 3),
    ("chat", "story.txt", "Naz: Ready\nVoid: Confirmed\n", "chat", "chat_message", 2),
    ("mixed", "story.txt", "Naz открыл notebook.\n", "mixed_text", "text_line", 1),
    ("json-object", "story.json", '{"name":"Naz","count":2}', "json", "json_scalar", 2),
    ("json-array", "story.json", '["one","two",3]', "json", "json_scalar", 3),
    ("jsonl", "story.jsonl", '{"n":1}\r\n{"n":2}\r\n', "jsonl", "json_scalar", 2),
    ("bom", "story.txt", "\ufeffNaz opened it.\n", "plain_text", "text_line", 1),
]


@pytest.mark.parametrize(
    "case_id,filename,text,media_type,first_kind,segment_count",
    SEGMENTATION_CASES,
    ids=[item[0] for item in SEGMENTATION_CASES],
)
def test_segmentation_preserves_exact_character_and_utf8_spans(
    tmp_path: Path,
    case_id: str,
    filename: str,
    text: str,
    media_type: str,
    first_kind: str,
    segment_count: int,
) -> None:
    del case_id
    bundle = _write_bundle(tmp_path, {filename: text})
    document = bundle.ordered_documents[0]
    assert document.exact_text == text
    assert document.media_type == media_type
    assert len(document.ordered_segments) == segment_count
    assert document.ordered_segments[0].segment_kind == first_kind
    raw = text.encode("utf-8")
    for segment in document.ordered_segments:
        assert text[segment.character_start:segment.character_end] == segment.exact_text
        assert raw[segment.byte_start:segment.byte_end].decode("utf-8") == segment.exact_text


JSON_SPAN_CASES = [
    ("string", '{"name":"Naz"}', "/name", '"Naz"'),
    ("negative", '{"n":-12}', "/n", "-12"),
    ("exponent", '{"n":1e+06}', "/n", "1e+06"),
    ("decimal", '{"n":0.50}', "/n", "0.50"),
    ("true", '{"ok":true}', "/ok", "true"),
    ("false", '{"ok":false}', "/ok", "false"),
    ("null", '{"value":null}', "/value", "null"),
    ("array", '{"items":[1,"two",3]}', "/items/1", '"two"'),
    ("slash-pointer", '{"a/b":7}', "/a~1b", "7"),
    ("tilde-pointer", '{"a~b":8}', "/a~0b", "8"),
    ("unicode", '{"text":"Привет"}', "/text", '"Привет"'),
    ("nested", '{"outer":{"inner":42}}', "/outer/inner", "42"),
    ("escaped", '{"text":"line\\nnext"}', "/text", '"line\\nnext"'),
    ("array-zero", '[{"id":9}]', "/0/id", "9"),
]


@pytest.mark.parametrize(
    "case_id,text,path,raw_value",
    JSON_SPAN_CASES,
    ids=[item[0] for item in JSON_SPAN_CASES],
)
def test_json_scalar_segments_preserve_raw_lexeme_and_pointer(
    tmp_path: Path,
    case_id: str,
    text: str,
    path: str,
    raw_value: str,
) -> None:
    del case_id
    bundle = _write_bundle(tmp_path, {"source.json": text})
    segment = next(item for item in _all_segments(bundle) if item.container_path == path)
    assert segment.exact_text == raw_value


JSONL_SPAN_CASES = [
    ("first", '{"n":1}\n{"n":2}\n', "/0/n", "1"),
    ("second", '{"n":1}\n{"n":2}\n', "/1/n", "2"),
    ("crlf", '{"x":"я"}\r\n{"x":"ё"}\r\n', "/1/x", '"ё"'),
    ("blank", '{"n":1}\n\n{"n":3}\n', "/1/n", "3"),
]


@pytest.mark.parametrize("case_id,text,path,raw_value", JSONL_SPAN_CASES, ids=[item[0] for item in JSONL_SPAN_CASES])
def test_jsonl_record_paths_are_deterministic(
    tmp_path: Path,
    case_id: str,
    text: str,
    path: str,
    raw_value: str,
) -> None:
    del case_id
    bundle = _write_bundle(tmp_path, {"source.jsonl": text})
    segment = next(item for item in _all_segments(bundle) if item.container_path == path)
    assert segment.exact_text == raw_value


CLASSIFICATION_CASES = [
    ("plain", {"a.txt": "A factual line."}, "unknown_but_text_readable"),
    ("markdown", {"a.md": "# Heading\nBody"}, "markdown_like"),
    ("json", {"a.json": '{"n":1}'}, "json_like"),
    ("jsonl", {"a.jsonl": '{"n":1}\n'}, "json_like"),
    ("yaml", {"a.yaml": "key: value\nother: value"}, "json_like"),
    ("log", {"a.log": "2026-08-17 INFO done"}, "log_like"),
    ("chat", {"a.txt": "Naz: One\nVoid: Two"}, "chat_email_like"),
    ("empty", {}, "insufficient"),
    ("binary", {"a.bin": b"\x00\xff"}, "unsupported_binary_container"),
    (
        "readable-plus-binary",
        {"summary.txt": "A factual line.", "primary.pdf": b"%PDF-\x00\xff"},
        "unsupported_binary_container",
    ),
    (
        "readable-plus-invalid-utf8",
        {"summary.txt": "A factual line.", "primary.txt": b"\xff\xfe\x00"},
        "unsupported_binary_container",
    ),
    ("bad-json", {"a.json": '{"n":'}, "parse_error"),
    ("bad-json-escape", {"a.json": '{"x":"\\q"}'}, "parse_error"),
    ("duplicate-json-key", {"a.json": '{"x":1,"x":2}'}, "parse_error"),
    ("trailing-json-comma", {"a.json": '{"x":1,}'}, "parse_error"),
    ("non-finite-json", {"a.json": '{"x":NaN}'}, "parse_error"),
    ("sensitive", {"a.txt": "api_key=sk-this-is-a-secret-value"}, "sensitive"),
]


@pytest.mark.parametrize("case_id,files,expected", CLASSIFICATION_CASES, ids=[item[0] for item in CLASSIFICATION_CASES])
def test_source_coverage_classification_is_explicit(
    tmp_path: Path,
    case_id: str,
    files: dict[str, str | bytes],
    expected: str,
) -> None:
    del case_id
    bundle = _write_bundle(tmp_path, files)
    classification = evidence.classify_source_bundle(bundle)
    assert classification.classification == expected
    assert classification.generic_fallback_candidate == (expected in {
        "unknown_but_text_readable", "markdown_like", "json_like", "log_like", "chat_email_like"
    })


@pytest.mark.parametrize(
    "source_ref",
    ["", "/absolute", "../escape", "a/../b", "a\\b", ".", "a/./b", "a//b"],
    ids=["empty", "absolute", "parent", "embedded-parent", "backslash", "dot", "embedded-dot", "double-slash"],
)
def test_invalid_source_ref_fails_closed(tmp_path: Path, source_ref: str) -> None:
    root = tmp_path / "inbox"
    root.mkdir()
    with pytest.raises(evidence.EvidenceContractError) as captured:
        evidence.build_source_document_bundle(root, source_ref, SOURCE_DIGEST, SOURCE_CONTRACT_VERSION)
    assert captured.value.reason_code == "evidence_source_invalid"


EXTRACTION_SCHEMA_CASES = [
    "drop-schema-version",
    "drop-source-identity",
    "drop-document-digest",
    "drop-run-id",
    "drop-evidence",
    "drop-dispositions",
    "extra-top-key",
    "wrong-schema-version",
    "wrong-source-identity",
    "wrong-document-digest",
    "bad-run-id",
    "evidence-not-list",
    "dispositions-not-list",
    "extra-evidence-key",
    "extra-quote-key",
]


@pytest.mark.parametrize("case", EXTRACTION_SCHEMA_CASES, ids=EXTRACTION_SCHEMA_CASES)
def test_extraction_schema_is_exact_and_source_bound(tmp_path: Path, case: str) -> None:
    bundle = _write_bundle(tmp_path)
    raw = _base_extraction_payload(bundle)
    if case.startswith("drop-"):
        names = {
            "drop-schema-version": "schema_version",
            "drop-source-identity": "source_identity",
            "drop-document-digest": "document_bundle_digest",
            "drop-run-id": "run_id",
            "drop-evidence": "evidence",
            "drop-dispositions": "segment_dispositions",
        }
        raw.pop(names[case])
    elif case == "extra-top-key":
        raw["unexpected"] = True
    elif case == "wrong-schema-version":
        raw["schema_version"] = "future-v9"
    elif case == "wrong-source-identity":
        raw["source_identity"] = "00" * 32
    elif case == "wrong-document-digest":
        raw["document_bundle_digest"] = "00" * 32
    elif case == "bad-run-id":
        raw["run_id"] = "bad id"
    elif case == "evidence-not-list":
        raw["evidence"] = {}
    elif case == "dispositions-not-list":
        raw["segment_dispositions"] = {}
    elif case == "extra-evidence-key":
        raw["evidence"][0]["unexpected"] = True
    elif case == "extra-quote-key":
        raw["evidence"][0]["exact_quotes"][0]["unexpected"] = True
    with pytest.raises(evidence.EvidenceContractError):
        evidence.parse_extraction_response(raw, bundle)


QUOTE_TAMPER_CASES = [
    "document-id",
    "segment-id",
    "byte-start",
    "byte-end",
    "character-start",
    "character-end",
    "exact-text",
    "missing-segment-ref",
    "duplicate-quote-id",
]


@pytest.mark.parametrize("case", QUOTE_TAMPER_CASES, ids=QUOTE_TAMPER_CASES)
def test_quote_span_tampering_fails_closed(tmp_path: Path, case: str) -> None:
    bundle = _write_bundle(tmp_path, {"story.txt": "Наз opened the notebook 🟦."})
    raw = _base_extraction_payload(bundle)
    quote = raw["evidence"][0]["exact_quotes"][0]
    if case == "document-id":
        quote["document_id"] = "document-999"
    elif case == "segment-id":
        quote["segment_id"] = "segment-999-999999"
    elif case == "byte-start":
        quote["byte_start"] += 1
    elif case == "byte-end":
        quote["byte_end"] -= 1
    elif case == "character-start":
        quote["character_start"] += 1
    elif case == "character-end":
        quote["character_end"] -= 1
    elif case == "exact-text":
        quote["exact_text"] = "fabricated content"
    elif case == "missing-segment-ref":
        raw["evidence"][0]["ordered_segment_refs"] = []
    elif case == "duplicate-quote-id":
        raw["evidence"][0]["exact_quotes"].append(deepcopy(quote))
    with pytest.raises(evidence.EvidenceContractError):
        evidence.parse_extraction_response(raw, bundle)


@pytest.mark.parametrize(
    "invented_proposition",
    (
        "The sky turned green.",
        "Workers destroyed the records.",
        "No outage affected customers.",
        "42 records were deleted yesterday.",
    ),
    ids=("unrelated-event", "predicate-substitution", "invented-impact", "invented-date"),
)
def test_model_proposition_must_be_one_exact_source_quote(
    tmp_path: Path,
    invented_proposition: str,
) -> None:
    bundle = _write_bundle(tmp_path, {"source.txt": "Workers migrated 42 records.\n"})
    raw = _base_extraction_payload(bundle)
    raw["evidence"][0]["proposition"] = invented_proposition
    with pytest.raises(evidence.EvidenceContractError, match="evidence_proposition_binding_invalid"):
        evidence.parse_extraction_response(raw, bundle)


VALID_VALUE_CASES = [
    ("number-integer", "42", "numbers", "number"),
    ("number-negative", "-3", "numbers", "number"),
    ("number-positive", "+7", "numbers", "number"),
    ("number-grouped-space", "1 000", "numbers", "number"),
    ("number-decimal", "2.50", "numbers", "number"),
    ("number-exponent", "1e+06", "numbers", "number"),
    ("date-iso", "2026-08-17", "dates", "date"),
    ("date-eu-slash", "17/08/2026", "dates", "date"),
    ("date-eu-dot", "17.08.2026", "dates", "date"),
    ("date-month-long", "August 17, 2026", "dates", "date"),
    ("date-month-short", "Aug 17 2026", "dates", "date"),
    ("entity-ascii", "Naz", "entities", "entity"),
    ("entity-cyrillic", "Москва", "entities", "entity"),
    ("entity-multiword", "Blue Notebook", "entities", "entity"),
]


@pytest.mark.parametrize("case_id,lexeme,field,kind", VALID_VALUE_CASES, ids=[item[0] for item in VALID_VALUE_CASES])
def test_typed_values_require_exact_bound_source_lexemes(
    tmp_path: Path,
    case_id: str,
    lexeme: str,
    field: str,
    kind: str,
) -> None:
    del case_id
    bundle = _write_bundle(tmp_path, {"story.txt": f"The recorded value is {lexeme}."})
    raw = _base_extraction_payload(bundle)
    raw["evidence"][0][field] = [{
        "atom_id": "atom-1",
        "atom_kind": kind,
        "quote_id": "quote-1",
        "exact_lexeme": lexeme,
    }]
    parsed = evidence.parse_extraction_response(raw, bundle)
    assert getattr(parsed.ordered_evidence[0], field)[0].exact_lexeme == lexeme


INVALID_VALUE_CASES = [
    ("number-substring-20", "20", "2", "numbers", "number"),
    ("number-substring-decimal", "2.50", "2", "numbers", "number"),
    ("number-substring-exponent", "1e+06", "1", "numbers", "number"),
    ("number-invalid-token", "12x", "12x", "numbers", "number"),
    ("number-absent", "20", "30", "numbers", "number"),
    ("date-invalid-month", "2026-99-17", "2026-99-17", "dates", "date"),
    ("date-invalid-day", "31/02/2026", "31/02/2026", "dates", "date"),
    ("date-substring", "2026-08-17", "2026", "dates", "date"),
    ("entity-substring", "Anna", "Ann", "entities", "entity"),
    ("entity-absent", "Naz", "Void", "entities", "entity"),
]


@pytest.mark.parametrize(
    "case_id,source_value,claimed_value,field,kind",
    INVALID_VALUE_CASES,
    ids=[item[0] for item in INVALID_VALUE_CASES],
)
def test_unbound_or_malformed_typed_values_fail_closed(
    tmp_path: Path,
    case_id: str,
    source_value: str,
    claimed_value: str,
    field: str,
    kind: str,
) -> None:
    del case_id
    bundle = _write_bundle(tmp_path, {"story.txt": f"The recorded value is {source_value}."})
    raw = _base_extraction_payload(bundle)
    raw["evidence"][0][field] = [{
        "atom_id": "atom-1",
        "atom_kind": kind,
        "quote_id": "quote-1",
        "exact_lexeme": claimed_value,
    }]
    with pytest.raises(evidence.EvidenceContractError) as captured:
        evidence.parse_extraction_response(raw, bundle)
    assert captured.value.reason_code in {"evidence_value_binding_invalid", "evidence_schema_invalid"}


VALID_RELATION_CASES = [
    ("before-en", "before", "temporal_relation", "before", "explicit_relation"),
    ("after-en", "after", "temporal_relation", "after", "explicit_relation"),
    ("sequence-en", "then", "temporal_relation", "sequence", "explicit_sequence"),
    ("before-ru", "до", "temporal_relation", "before", "explicit_relation"),
    ("after-ru", "после", "temporal_relation", "after", "explicit_relation"),
    ("sequence-ru-zatem", "затем", "temporal_relation", "sequence", "explicit_sequence"),
    ("sequence-ru-potom", "потом", "temporal_relation", "sequence", "explicit_sequence"),
    ("because-en", "because", "causal_relation", "because", "explicit_cause"),
    ("therefore-en", "therefore", "causal_relation", "therefore", "explicit_cause"),
    ("caused-en", "caused", "causal_relation", "caused", "explicit_cause"),
    ("because-ru", "потому что", "causal_relation", "because", "explicit_cause"),
    ("therefore-ru", "поэтому", "causal_relation", "therefore", "explicit_cause"),
]


def _relation_payload(
    bundle: evidence.SourceDocumentBundle,
    marker: str,
    field: str,
    relation_kind: str,
    evidence_kind: str,
) -> dict[str, object]:
    raw = _base_extraction_payload(bundle)
    segment = _all_segments(bundle)[0]
    left = _quote_payload(bundle, segment, "Alpha", quote_id="quote-left")
    marker_quote = _quote_payload(bundle, segment, marker, quote_id="quote-marker")
    right = _quote_payload(bundle, segment, "Beta", quote_id="quote-right")
    proposition = _quote_payload(
        bundle,
        segment,
        f"Alpha {marker} Beta",
        quote_id="quote-proposition",
    )
    item = raw["evidence"][0]
    item["exact_quotes"] = [proposition, left, marker_quote, right]
    item["evidence_kind"] = evidence_kind
    item[field] = {
        "relation_kind": relation_kind,
        "marker_quote_id": "quote-marker",
        "left_operand_quote_ids": ["quote-left"],
        "right_operand_quote_ids": ["quote-right"],
    }
    return raw


@pytest.mark.parametrize(
    "case_id,marker,field,relation_kind,evidence_kind",
    VALID_RELATION_CASES,
    ids=[item[0] for item in VALID_RELATION_CASES],
)
def test_explicit_relations_bind_marker_and_ordered_operands(
    tmp_path: Path,
    case_id: str,
    marker: str,
    field: str,
    relation_kind: str,
    evidence_kind: str,
) -> None:
    del case_id
    bundle = _write_bundle(tmp_path, {"story.txt": f"Alpha {marker} Beta"})
    parsed = evidence.parse_extraction_response(
        _relation_payload(bundle, marker, field, relation_kind, evidence_kind),
        bundle,
    )
    assert getattr(parsed.ordered_evidence[0], field).relation_kind == relation_kind


RELATION_TAMPER_CASES = [
    "wrong-marker",
    "swapped-operands",
    "marker-is-operand",
    "unknown-operand",
    "both-relation-families",
    "explicit-cause-missing-relation",
    "explicit-sequence-wrong-kind",
]


@pytest.mark.parametrize("case", RELATION_TAMPER_CASES, ids=RELATION_TAMPER_CASES)
def test_relation_smuggling_fails_closed(tmp_path: Path, case: str) -> None:
    bundle = _write_bundle(tmp_path, {"story.txt": "Alpha before Beta"})
    raw = _relation_payload(bundle, "before", "temporal_relation", "before", "explicit_relation")
    item = raw["evidence"][0]
    relation = item["temporal_relation"]
    if case == "wrong-marker":
        relation["relation_kind"] = "after"
    elif case == "swapped-operands":
        relation["left_operand_quote_ids"], relation["right_operand_quote_ids"] = (
            relation["right_operand_quote_ids"], relation["left_operand_quote_ids"]
        )
    elif case == "marker-is-operand":
        relation["left_operand_quote_ids"] = ["quote-marker"]
    elif case == "unknown-operand":
        relation["left_operand_quote_ids"] = ["quote-unknown"]
    elif case == "both-relation-families":
        item["causal_relation"] = {
            "relation_kind": "because",
            "marker_quote_id": "quote-marker",
            "left_operand_quote_ids": ["quote-left"],
            "right_operand_quote_ids": ["quote-right"],
        }
    elif case == "explicit-cause-missing-relation":
        item["evidence_kind"] = "explicit_cause"
        item["temporal_relation"] = None
    elif case == "explicit-sequence-wrong-kind":
        item["evidence_kind"] = "explicit_sequence"
    with pytest.raises(evidence.EvidenceContractError):
        evidence.parse_extraction_response(raw, bundle)


POLARITY_UNCERTAINTY_CASES = [
    ("affirmed-en", "System is active.", "affirmed", "certain"),
    ("negated-not", "System is not active.", "negated", "certain"),
    ("negated-never", "System never started.", "negated", "certain"),
    ("negated-ru", "Система не активна.", "negated", "certain"),
    ("uncertain-may", "System may be active.", "affirmed", "uncertain"),
    ("uncertain-might", "System might start.", "affirmed", "uncertain"),
    ("uncertain-ru", "Возможно система активна.", "affirmed", "uncertain"),
    ("quoted-negation", "Naz said system is not active.", "quoted", "certain"),
]


@pytest.mark.parametrize(
    "case_id,text,polarity,uncertainty",
    POLARITY_UNCERTAINTY_CASES,
    ids=[item[0] for item in POLARITY_UNCERTAINTY_CASES],
)
def test_polarity_and_uncertainty_are_bound_to_exact_quotes(
    tmp_path: Path,
    case_id: str,
    text: str,
    polarity: str,
    uncertainty: str,
) -> None:
    del case_id
    bundle = _write_bundle(tmp_path, {"story.txt": text})
    raw = _base_extraction_payload(bundle)
    raw["evidence"][0]["polarity"] = polarity
    raw["evidence"][0]["uncertainty"] = uncertainty
    parsed = evidence.parse_extraction_response(raw, bundle)
    assert parsed.ordered_evidence[0].polarity == polarity
    assert parsed.ordered_evidence[0].uncertainty == uncertainty


POLARITY_TAMPER_CASES = [
    ("drop-negation-en", "System is not active.", "affirmed", "certain"),
    ("invent-negation-en", "System is active.", "negated", "certain"),
    ("drop-negation-ru", "Система не активна.", "affirmed", "certain"),
    ("invent-negation-ru", "Система активна.", "negated", "certain"),
    ("drop-uncertainty", "System may start.", "affirmed", "certain"),
    ("invent-uncertainty", "System started.", "affirmed", "uncertain"),
]


@pytest.mark.parametrize(
    "case_id,text,polarity,uncertainty",
    POLARITY_TAMPER_CASES,
    ids=[item[0] for item in POLARITY_TAMPER_CASES],
)
def test_polarity_or_uncertainty_reversal_fails_closed(
    tmp_path: Path,
    case_id: str,
    text: str,
    polarity: str,
    uncertainty: str,
) -> None:
    del case_id
    bundle = _write_bundle(tmp_path, {"story.txt": text})
    raw = _base_extraction_payload(bundle)
    raw["evidence"][0]["polarity"] = polarity
    raw["evidence"][0]["uncertainty"] = uncertainty
    with pytest.raises(evidence.EvidenceContractError):
        evidence.parse_extraction_response(raw, bundle)


DISPOSITION_TAMPER_CASES = [
    "missing",
    "duplicate",
    "reordered",
    "unknown-evidence-id",
    "selected-marked-irrelevant",
    "unselected-marked-evidence",
    "sensitive-marked-irrelevant",
]


@pytest.mark.parametrize("case", DISPOSITION_TAMPER_CASES, ids=DISPOSITION_TAMPER_CASES)
def test_every_segment_requires_one_consistent_disposition(tmp_path: Path, case: str) -> None:
    bundle = _write_bundle(tmp_path, {"story.txt": "Public fact.\nSecond detail.\ntoken=sk-this-is-secret-value"})
    raw = _base_extraction_payload(bundle)
    dispositions = raw["segment_dispositions"]
    if case == "missing":
        dispositions.pop()
    elif case == "duplicate":
        dispositions.append(deepcopy(dispositions[-1]))
    elif case == "reordered":
        dispositions.reverse()
    elif case == "unknown-evidence-id":
        dispositions[0]["ordered_evidence_ids"] = ["evidence-unknown"]
    elif case == "selected-marked-irrelevant":
        dispositions[0]["disposition"] = "irrelevant"
        dispositions[0]["ordered_evidence_ids"] = []
    elif case == "unselected-marked-evidence":
        dispositions[1]["disposition"] = "evidence"
        dispositions[1]["ordered_evidence_ids"] = ["evidence-1"]
    elif case == "sensitive-marked-irrelevant":
        dispositions[-1]["disposition"] = "irrelevant"
    with pytest.raises(evidence.EvidenceContractError):
        evidence.parse_extraction_response(raw, bundle)


ADJUDICATION_TAMPER_CASES = [
    "drop-schema",
    "extra-key",
    "wrong-schema",
    "wrong-source",
    "wrong-extraction",
    "bad-run-id",
    "missing-decision",
    "duplicate-decision",
    "wrong-evidence-id",
    "wrong-evidence-digest",
    "supported-with-reason",
    "rejected-without-reason",
    "unknown-reason",
]


@pytest.mark.parametrize("case", ADJUDICATION_TAMPER_CASES, ids=ADJUDICATION_TAMPER_CASES)
def test_adjudication_is_exact_complete_and_digest_bound(tmp_path: Path, case: str) -> None:
    bundle = _write_bundle(tmp_path)
    extraction = _parse_base(bundle)
    raw = _adjudication_payload(extraction)
    if case == "drop-schema":
        raw.pop("schema_version")
    elif case == "extra-key":
        raw["unexpected"] = True
    elif case == "wrong-schema":
        raw["schema_version"] = "future-v9"
    elif case == "wrong-source":
        raw["source_identity"] = "00" * 32
    elif case == "wrong-extraction":
        raw["extraction_bundle_digest"] = "00" * 32
    elif case == "bad-run-id":
        raw["run_id"] = "invalid run"
    elif case == "missing-decision":
        raw["decisions"] = []
    elif case == "duplicate-decision":
        raw["decisions"].append(deepcopy(raw["decisions"][0]))
    elif case == "wrong-evidence-id":
        raw["decisions"][0]["evidence_id"] = "evidence-unknown"
    elif case == "wrong-evidence-digest":
        raw["decisions"][0]["evidence_digest"] = "00" * 32
    elif case == "supported-with-reason":
        raw["decisions"][0]["reason_codes"] = ["missing_context"]
    elif case == "rejected-without-reason":
        raw["decisions"][0]["decision"] = "rejected"
    elif case == "unknown-reason":
        raw["decisions"][0]["decision"] = "rejected"
        raw["decisions"][0]["reason_codes"] = ["invented_reason"]
    with pytest.raises(evidence.EvidenceContractError):
        evidence.parse_adjudication_response(raw, extraction)


def _ambiguous_extraction_payload(bundle: evidence.SourceDocumentBundle) -> dict[str, object]:
    return {
        "schema_version": evidence.EVIDENCE_EXTRACTION_CONTRACT_VERSION,
        "source_identity": bundle.source_identity,
        "document_bundle_digest": bundle.bundle_digest,
        "run_id": "extract-ambiguous",
        "evidence": [],
        "segment_dispositions": [
            {"segment_id": item.segment_id, "disposition": "ambiguous", "ordered_evidence_ids": []}
            for item in _all_segments(bundle)
        ],
    }


SERVICE_CASES = [
    "verified",
    "empty-source",
    "sensitive-source",
    "unsupported-binary",
    "partial-binary",
    "partial-invalid-utf8",
    "invalid-json",
    "ambiguous",
    "all-irrelevant",
    "rejected",
    "sensitive-rejected",
    "bad-extraction-schema",
    "provider-fails-first",
    "provider-fails-second",
]


@pytest.mark.parametrize("case", SERVICE_CASES, ids=SERVICE_CASES)
def test_generic_evidence_service_has_closed_outcomes_and_call_budget(tmp_path: Path, case: str) -> None:
    if case == "empty-source":
        bundle = _write_bundle(tmp_path, {})
        responses: list[object] = []
        expected = ("source_insufficient", "evidence_source_insufficient", 0)
    elif case == "sensitive-source":
        bundle = _write_bundle(tmp_path, {"source.txt": "password=extremely-sensitive-value"})
        responses = []
        expected = ("sensitive_rejected", "evidence_sensitive", 0)
    elif case == "unsupported-binary":
        bundle = _write_bundle(tmp_path, {"source.bin": b"\x00\xff"})
        responses = []
        expected = ("manual_attention", "evidence_manual_attention", 0)
    elif case == "partial-binary":
        bundle = _write_bundle(
            tmp_path,
            {"summary.txt": "A factual line.", "primary.pdf": b"%PDF-\x00\xff"},
        )
        responses = []
        expected = ("manual_attention", "evidence_manual_attention", 0)
    elif case == "partial-invalid-utf8":
        bundle = _write_bundle(
            tmp_path,
            {"summary.txt": "A factual line.", "primary.txt": b"\xff\xfe\x00"},
        )
        responses = []
        expected = ("manual_attention", "evidence_manual_attention", 0)
    elif case == "invalid-json":
        bundle = _write_bundle(tmp_path, {"source.json": '{"bad":'})
        responses = []
        expected = ("manual_attention", "evidence_manual_attention", 0)
    else:
        bundle = _write_bundle(tmp_path)
        extraction_raw = (
            _ambiguous_extraction_payload(bundle)
            if case == "ambiguous"
            else {
                "schema_version": evidence.EVIDENCE_EXTRACTION_CONTRACT_VERSION,
                "source_identity": bundle.source_identity,
                "document_bundle_digest": bundle.bundle_digest,
                "run_id": "extract-all-irrelevant",
                "evidence": [],
                "segment_dispositions": [
                    {
                        "segment_id": item.segment_id,
                        "disposition": "irrelevant",
                        "ordered_evidence_ids": [],
                    }
                    for item in _all_segments(bundle)
                ],
            }
            if case == "all-irrelevant"
            else _base_extraction_payload(bundle)
        )
        if case == "bad-extraction-schema":
            extraction_raw.pop("run_id")
            responses = [extraction_raw]
            expected = ("failed", "evidence_schema_invalid", 1)
        elif case == "provider-fails-first":
            responses = [RuntimeError("private provider detail")]
            expected = ("failed", "evidence_provider_failed", 1)
        else:
            extraction = evidence.parse_extraction_response(extraction_raw, bundle)
            if case == "provider-fails-second":
                responses = [extraction_raw, RuntimeError("private adjudicator detail")]
                expected = ("failed", "evidence_provider_failed", 2)
            elif case == "ambiguous":
                responses = [extraction_raw, _adjudication_payload(extraction)]
                expected = ("manual_attention", "evidence_manual_attention", 2)
            elif case == "all-irrelevant":
                responses = [extraction_raw, _adjudication_payload(extraction)]
                expected = ("source_insufficient", "evidence_source_insufficient", 2)
            elif case == "rejected":
                responses = [extraction_raw, _adjudication_payload(extraction, decision="rejected")]
                expected = ("source_insufficient", "evidence_source_insufficient", 2)
            elif case == "sensitive-rejected":
                responses = [
                    extraction_raw,
                    _adjudication_payload(extraction, decision="rejected", reason_codes=["sensitive_content"]),
                ]
                expected = ("sensitive_rejected", "evidence_sensitive", 2)
            else:
                responses = [extraction_raw, _adjudication_payload(extraction)]
                expected = ("verified", "evidence_verified", 2)
    client = FakeEvidenceClient(responses)
    service = evidence.GenericEvidenceService(
        client,
        extraction_model="extract-model-v1",
        adjudication_model="judge-model-v1",
    )
    assert type(service) is evidence.GenericEvidenceService
    assert service.extraction_model == "extract-model-v1"
    assert service.adjudication_model == "judge-model-v1"
    result = service.resolve(bundle)
    assert type(result) is evidence.EvidenceResolution
    assert (result.status, result.reason_code, result.model_call_count) == expected
    assert len(client.requests) == result.model_call_count
    assert (result.verified_bundle is not None) == (result.status == "verified")


def test_two_model_requests_have_distinct_kinds_and_no_source_path(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path)
    _, client = _verified(bundle)
    assert [item.request_kind for item in client.requests] == ["evidence_extraction", "evidence_adjudication"]
    assert [item.model for item in client.requests] == ["extract-model-v1", "judge-model-v1"]
    for request in client.requests:
        assert str(tmp_path) not in request.payload_json
        assert SOURCE_REF not in request.payload_json


@pytest.mark.parametrize(
    "secret",
    [
        "api_key=sk-super-sensitive-value",
        "token=ghp_superSensitiveToken12345",
        "password=hunter-two-secret",
        "authorization=Bearer-secret-value",
        "C:\\private\\source\\file.txt",
        "/var/private/source/file.txt",
    ],
    ids=["api-key", "token", "password", "authorization", "windows-path", "posix-path"],
)
def test_sensitive_segments_are_never_sent_to_model(tmp_path: Path, secret: str) -> None:
    bundle = _write_bundle(tmp_path, {"source.txt": f"Public fact.\n{secret}\n"})
    raw = _base_extraction_payload(bundle)
    extraction = evidence.parse_extraction_response(raw, bundle)
    client = FakeEvidenceClient([raw, _adjudication_payload(extraction)])
    result = evidence.GenericEvidenceService(
        client,
        extraction_model="extract",
        adjudication_model="judge",
    ).resolve(bundle)
    assert result.status == "verified"
    assert all(secret not in item.payload_json for item in client.requests)


@pytest.mark.parametrize(
    "private_text",
    [
        "The operator discussed credential handling without publishing a value.",
        r"A document referenced \\internal-host\private-share\item.txt.",
        "A document referenced file:///private/location/item.txt.",
        "The review mentioned ~/private/item.txt.",
    ],
    ids=("credential-marker", "unc-path", "file-uri", "home-path"),
)
def test_evidence_projection_uses_exact_provider_privacy_vocabulary(
    tmp_path: Path,
    private_text: str,
) -> None:
    bundle = _write_bundle(
        tmp_path,
        {"source.txt": f"A safe public observation.\n{private_text}\n"},
    )

    projection = evidence._source_projection(bundle)
    projected_segments = [
        segment
        for document in projection["documents"]
        for segment in document["segments"]
    ]
    withheld = [segment for segment in projected_segments if segment["withheld"]]

    assert len(withheld) == 1
    assert frozenset(withheld[0]) == {"segment_id", "withheld", "segment_digest"}
    assert private_text not in evidence._canonical(projection).decode("utf-8")
    provider._assert_private_payload(
        {"messages": [{"content": evidence._canonical(projection).decode("utf-8")}]},
        secret_values=("test-secret-not-present",),
    )


def test_direct_provider_payload_remains_fail_closed_for_shared_private_text() -> None:
    private_text = "The operator discussed credential handling."

    with pytest.raises(provider.NormalizerProviderError) as caught:
        provider._assert_private_payload(
            {"messages": [{"content": private_text}]},
            secret_values=("test-secret-not-present",),
        )

    assert caught.value.reason_code == provider.PROVIDER_CONFIGURATION_INVALID


@pytest.mark.parametrize(
    "secret",
    [
        "api_key=sk-super-sensitive-value",
        "token=ghp_superSensitiveToken12345",
        "password=hunter-two-secret",
        "authorization=Bearer-secret-value",
        "C:\\private\\source\\file.txt",
        "/var/private/source/file.txt",
    ],
    ids=["api-key", "token", "password", "authorization", "windows-path", "posix-path"],
)
def test_sensitive_text_cannot_be_smuggled_through_public_proposition(tmp_path: Path, secret: str) -> None:
    bundle = _write_bundle(tmp_path, {"source.txt": "A safe public fact."})
    raw = _base_extraction_payload(bundle)
    raw["evidence"][0]["proposition"] = secret
    with pytest.raises(evidence.EvidenceContractError) as captured:
        evidence.parse_extraction_response(raw, bundle)
    assert captured.value.reason_code == "evidence_sensitive"


PERSISTED_TAMPER_CASES = [
    "extra-top-key",
    "drop-contract-version",
    "wrong-contract-version",
    "wrong-source-identity",
    "wrong-document-digest",
    "wrong-extraction-digest",
    "wrong-adjudication-digest",
    "wrong-adjudication-extraction-link",
    "wrong-accepted-id",
    "duplicate-accepted-id",
    "wrong-verified-digest",
    "extra-nested-key",
]


@pytest.mark.parametrize("case", PERSISTED_TAMPER_CASES, ids=PERSISTED_TAMPER_CASES)
def test_persisted_verified_bundle_is_strictly_revalidated(tmp_path: Path, case: str) -> None:
    bundle = _write_bundle(tmp_path)
    verified, _ = _verified(bundle)
    payload = evidence.verified_bundle_to_payload(verified)
    if case == "extra-top-key":
        payload["unexpected"] = True
    elif case == "drop-contract-version":
        payload.pop("contract_version")
    elif case == "wrong-contract-version":
        payload["contract_version"] = "future-v9"
    elif case == "wrong-source-identity":
        payload["source_identity"] = "00" * 32
    elif case == "wrong-document-digest":
        payload["document_bundle_digest"] = "00" * 32
    elif case == "wrong-extraction-digest":
        payload["extraction"]["bundle_digest"] = "00" * 32
    elif case == "wrong-adjudication-digest":
        payload["adjudication"]["bundle_digest"] = "00" * 32
    elif case == "wrong-adjudication-extraction-link":
        payload["adjudication"]["extraction_bundle_digest"] = "00" * 32
    elif case == "wrong-accepted-id":
        payload["accepted_evidence_ids"] = ["evidence-unknown"]
    elif case == "duplicate-accepted-id":
        payload["accepted_evidence_ids"].append(payload["accepted_evidence_ids"][0])
    elif case == "wrong-verified-digest":
        payload["verified_bundle_digest"] = "00" * 32
    elif case == "extra-nested-key":
        payload["extraction"]["unexpected"] = True
    with pytest.raises(evidence.EvidenceContractError):
        evidence.verified_bundle_from_payload(payload, bundle)


def test_verified_bundle_round_trip_and_staleness_detection(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path)
    verified, _ = _verified(bundle)
    payload = evidence.verified_bundle_to_payload(verified)
    assert evidence.verified_bundle_from_payload(payload, bundle) == verified
    changed = _write_bundle(tmp_path / "changed", {"story.txt": "Different source text."})
    with pytest.raises(evidence.EvidenceContractError):
        evidence.revalidate_verified_bundle(changed, verified)


BINDING_TAMPER_CASES = [
    "extra-key",
    "missing-key",
    "wrong-version",
    "wrong-fact-id",
    "wrong-evidence-digest",
    "wrong-source-identity",
    "zero-order",
    "unknown-polarity",
    "unknown-uncertainty",
    "anchor-count",
    "anchor-id",
    "duplicate-anchor-label",
]


@pytest.mark.parametrize("case", BINDING_TAMPER_CASES, ids=BINDING_TAMPER_CASES)
def test_verified_fact_binding_payload_is_exact_and_self_consistent(tmp_path: Path, case: str) -> None:
    bundle = _write_bundle(tmp_path)
    verified, _ = _verified(bundle)
    binding = evidence.build_verified_fact_bindings(bundle, verified)[0]
    payload = evidence.verified_fact_binding_to_payload(binding)
    if case == "extra-key":
        payload["unexpected"] = True
    elif case == "missing-key":
        payload.pop("uncertainty")
    elif case == "wrong-version":
        payload["binding_version"] = "future-v9"
    elif case == "wrong-fact-id":
        payload["fact_id"] = "bad id"
    elif case == "wrong-evidence-digest":
        payload["evidence_digest"] = "not-a-digest"
    elif case == "wrong-source-identity":
        payload["source_identity"] = "00"
    elif case == "zero-order":
        payload["order"] = 0
    elif case == "unknown-polarity":
        payload["polarity"] = "maybe"
    elif case == "unknown-uncertainty":
        payload["uncertainty"] = "perhaps"
    elif case == "anchor-count":
        payload["meaning_anchor_ids"].pop()
    elif case == "anchor-id":
        payload["meaning_anchor_ids"][0] = "meaning-001-0000000000000000"
    elif case == "duplicate-anchor-label":
        payload["public_anchor_labels"].append(payload["public_anchor_labels"][0])
        payload["meaning_anchor_ids"].append(payload["meaning_anchor_ids"][0])
    with pytest.raises(evidence.EvidenceContractError):
        evidence.verified_fact_binding_from_payload(payload)


@pytest.mark.parametrize(
    "proposition,expected_tokens",
    [
        ("Naz opened the blue notebook.", ("naz", "opened", "blue", "notebook")),
        ("Наз открыл синий блокнот.", ("наз", "открыл", "синий", "блокнот")),
        ("VOID measured 42 pulses on 2026-08-17.", ("void", "measured", "pulses")),
        ("Alpha and Alpha moved to Beta.", ("alpha", "moved", "beta")),
        ("The quiet object is on the table.", ("quiet", "object", "table")),
        ("Система не изменила состояние.", ("система", "изменила", "состояние")),
    ],
    ids=["english", "russian", "typed-values", "dedupe", "stopwords", "russian-stopword"],
)
def test_proposition_content_tokens_become_deterministic_meaning_anchors(
    tmp_path: Path,
    proposition: str,
    expected_tokens: tuple[str, ...],
) -> None:
    bundle = _write_bundle(tmp_path, {"story.txt": proposition})
    verified, _ = _verified(bundle)
    binding = evidence.build_verified_fact_bindings(bundle, verified)[0]
    proposition_labels = tuple(
        label.removeprefix("proposition:")
        for label in binding.public_anchor_labels
        if label.startswith("proposition:")
    )
    assert proposition_labels == expected_tokens
    assert evidence.verified_fact_binding_from_payload(
        evidence.verified_fact_binding_to_payload(binding)
    ) == binding


def test_verified_fact_binding_is_immutable_deterministic_and_fully_bound(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path, {"story.txt": "Naz recorded 42 events on 2026-08-17."})
    raw = _base_extraction_payload(bundle)
    quote_id = raw["evidence"][0]["exact_quotes"][0]["quote_id"]
    raw["evidence"][0]["entities"] = [
        {"atom_id": "entity-1", "atom_kind": "entity", "quote_id": quote_id, "exact_lexeme": "Naz"}
    ]
    raw["evidence"][0]["numbers"] = [
        {"atom_id": "number-1", "atom_kind": "number", "quote_id": quote_id, "exact_lexeme": "42"}
    ]
    raw["evidence"][0]["dates"] = [
        {"atom_id": "date-1", "atom_kind": "date", "quote_id": quote_id, "exact_lexeme": "2026-08-17"}
    ]
    extraction = evidence.parse_extraction_response(raw, bundle)
    adjudication = evidence.parse_adjudication_response(_adjudication_payload(extraction), extraction)
    client = FakeEvidenceClient([raw, _adjudication_payload(extraction)])
    result = evidence.GenericEvidenceService(
        client,
        extraction_model="extract",
        adjudication_model="judge",
    ).resolve(bundle)
    assert result.verified_bundle is not None
    first = evidence.build_verified_fact_bindings(bundle, result.verified_bundle)
    second = evidence.build_verified_fact_bindings(bundle, result.verified_bundle)
    assert first == second
    binding = first[0]
    assert binding.numbers == ("42",)
    assert binding.entities == ("Naz",)
    assert binding.dates == ("2026-08-17",)
    assert binding.uncertainty == "certain"
    assert binding.adjudication_identity
    assert adjudication.ordered_decisions[0].evidence_digest == binding.evidence_digest
    with pytest.raises(FrozenInstanceError):
        binding.fact_id = "fact-999"


@pytest.mark.parametrize(
    "bad_value",
    [None, [], (), "{}", 1, True],
    ids=["none", "list", "tuple", "json-string", "integer", "boolean"],
)
def test_public_payload_parsers_reject_non_mapping_types(tmp_path: Path, bad_value: object) -> None:
    bundle = _write_bundle(tmp_path)
    with pytest.raises(evidence.EvidenceContractError):
        evidence.verified_fact_binding_from_payload(bad_value)
    with pytest.raises(evidence.EvidenceContractError):
        evidence.verified_bundle_from_payload(bad_value, bundle)


def test_contract_errors_expose_only_stable_reason_code() -> None:
    error = evidence.EvidenceContractError("private secret source detail")
    assert str(error) == "evidence_schema_invalid"
    assert "private secret" not in str(error)
    assert "private secret" not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None


def _diagnostic_case(
    bundle: evidence.SourceDocumentBundle,
    case_id: str,
) -> object:
    raw: object = deepcopy(_base_extraction_payload(bundle))
    assert type(raw) is dict
    if case_id == "response-type":
        return []
    if case_id == "json-parse":
        return "{"
    if case_id == "top-level-schema":
        raw.pop("run_id")
    elif case_id == "contract-version":
        raw["schema_version"] = "normalizer-evidence-extraction-v999"
    elif case_id == "source-binding":
        raw["source_identity"] = "0" * 64
    elif case_id == "nested-schema":
        raw["evidence"] = {}
    elif case_id == "segment-binding":
        raw["segment_dispositions"][0]["segment_id"] = "missing-segment"
    elif case_id == "quote-binding":
        raw["evidence"][0]["exact_quotes"][0]["character_end"] -= 1
    elif case_id == "proposition-binding":
        raw["evidence"][0]["proposition"] = "Unsupported synthesized proposition."
    elif case_id == "value-binding":
        raw["evidence"][0]["numbers"] = [{
            "atom_id": "number-1",
            "atom_kind": "number",
            "quote_id": "quote-1",
            "exact_lexeme": "999",
        }]
    elif case_id == "relation-binding":
        raw["evidence"][0]["evidence_kind"] = "explicit_cause"
    elif case_id == "semantic-validation":
        raw["evidence"][0]["polarity"] = "negated"
    return raw


@pytest.mark.parametrize(
    "case_id,expected_stage",
    [
        ("response-type", "response_type"),
        ("json-parse", "json_parse"),
        ("top-level-schema", "top_level_schema"),
        ("contract-version", "contract_version"),
        ("source-binding", "source_binding"),
        ("nested-schema", "nested_schema"),
        ("segment-binding", "segment_binding"),
        ("quote-binding", "quote_binding"),
        ("proposition-binding", "proposition_binding"),
        ("value-binding", "value_binding"),
        ("relation-binding", "relation_binding"),
        ("semantic-validation", "semantic_validation"),
    ],
    ids=lambda value: value,
)
def test_extraction_rejection_emits_closed_privacy_safe_diagnostic_stage(
    tmp_path: Path,
    case_id: str,
    expected_stage: str,
) -> None:
    bundle = _write_bundle(tmp_path)
    raw = _diagnostic_case(bundle, case_id)
    with pytest.raises(evidence.EvidenceContractError) as captured:
        evidence.parse_extraction_response(raw, bundle)
    diagnostic = captured.value.diagnostic
    assert type(diagnostic) is evidence.EvidenceValidationDiagnostic
    assert diagnostic.validation_stage == expected_stage
    payload = diagnostic.safe_payload()
    encoded = evidence.json.dumps(payload, ensure_ascii=False, sort_keys=True)
    assert set(payload) == {
        "validation_stage", "stable_subreason", "field_path",
        "response_top_level_exact_type", "top_level_key_set", "missing_keys",
        "extra_keys", "nested_field_types", "list_item_counts",
        "schema_contract_version", "span_quote_validation_category",
        "source_identity_binding_result", "response_byte_size",
        "response_character_size",
    }
    assert "Naz opened the blue notebook" not in encoded
    assert "Unsupported synthesized proposition" not in encoded
    assert captured.value.reason_code != "evidence_verified"


def test_extraction_diagnostic_propagates_without_retry_or_raw_values(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path)
    bad = _base_extraction_payload(bundle)
    bad["evidence"][0]["exact_quotes"][0]["byte_end"] -= 1
    client = FakeEvidenceClient([bad])
    result = evidence.GenericEvidenceService(
        client,
        extraction_model="extract-model-v1",
        adjudication_model="judge-model-v1",
    ).resolve(bundle)
    assert result.status == "failed"
    assert result.model_call_count == 1
    assert len(client.requests) == 1
    assert type(result.diagnostic) is evidence.EvidenceValidationDiagnostic
    assert result.diagnostic.validation_stage == "quote_binding"
    assert "blue notebook" not in evidence.json.dumps(result.diagnostic.safe_payload())


def _semantic_diagnostic_probe(
    tmp_path: Path,
    case_id: str,
) -> tuple[evidence.EvidenceValidationDiagnostic, str]:
    if case_id == "withheld-segment":
        bundle = _write_bundle(
            tmp_path,
            {"story.txt": "Public evidence line.\napi_key=sk-super-sensitive-value\n"},
        )
    elif case_id == "unbound-segment":
        bundle = _write_bundle(tmp_path, {"story.txt": "First public line.\nSecond public line.\n"})
    else:
        bundle = _write_bundle(tmp_path)
    raw = _base_extraction_payload(bundle)
    item = raw["evidence"][0]
    dispositions = raw["segment_dispositions"]
    if case_id == "disposition-partition":
        dispositions[0]["ordered_evidence_ids"] = ["unknown-evidence"]
    elif case_id == "duplicate-disposition":
        dispositions.append(deepcopy(dispositions[0]))
    elif case_id == "unbound-segment":
        item["ordered_segment_refs"].append(dispositions[1]["segment_id"])
    elif case_id == "withheld-segment":
        item["ordered_segment_refs"].append(dispositions[1]["segment_id"])
    elif case_id == "entity-ownership":
        item["entities"] = [{
            "atom_id": "entity-1", "atom_kind": "entity",
            "quote_id": "quote-1", "exact_lexeme": "AbsentEntity",
        }]
    elif case_id == "number-ownership":
        item["numbers"] = [{
            "atom_id": "number-1", "atom_kind": "number",
            "quote_id": "quote-1", "exact_lexeme": "999",
        }]
    elif case_id == "date-ownership":
        item["dates"] = [{
            "atom_id": "date-1", "atom_kind": "date",
            "quote_id": "quote-1", "exact_lexeme": "2026-99-99",
        }]
    elif case_id == "polarity":
        item["polarity"] = "negated"
    elif case_id == "temporal":
        item["evidence_kind"] = "explicit_sequence"
    elif case_id == "causal":
        item["evidence_kind"] = "explicit_cause"
    elif case_id == "duplicate-evidence":
        raw["evidence"].append(deepcopy(item))
    elif case_id == "coverage":
        item["exact_quotes"] = []
    elif case_id == "unsupported-ambiguous":
        item["evidence_kind"] = "insufficient_or_ambiguous"
    elif case_id == "meaning-anchor":
        item["proposition"] = "Unsupported synthesized proposition."
    else:
        raise AssertionError(case_id)
    with pytest.raises(evidence.EvidenceContractError) as captured:
        evidence.parse_extraction_response(raw, bundle)
    assert type(captured.value.diagnostic) is evidence.EvidenceValidationDiagnostic
    return captured.value.diagnostic, evidence.json.dumps(
        captured.value.diagnostic.safe_payload(), ensure_ascii=False, sort_keys=True,
    )


@pytest.mark.parametrize(
    ("case_id", "subreason", "field_path"),
    (
        ("disposition-partition", "disposition_partition_mismatch", "$.segment_dispositions"),
        ("duplicate-disposition", "duplicate_or_missing_segment_disposition", "$.segment_dispositions"),
        ("unbound-segment", "evidence_item_not_bound_to_source_segment", "$.evidence[].ordered_segment_refs"),
        ("withheld-segment", "evidence_references_withheld_segment", "$.evidence[].ordered_segment_refs"),
        ("entity-ownership", "entity_ownership_invalid", "$.evidence[].entities[]"),
        ("number-ownership", "number_ownership_invalid", "$.evidence[].numbers[]"),
        ("date-ownership", "date_ownership_invalid", "$.evidence[].dates[]"),
        ("polarity", "polarity_mismatch", "$.evidence[].polarity"),
        ("temporal", "temporal_relation_mismatch", "$.evidence[].temporal_relation"),
        ("causal", "causal_relation_mismatch", "$.evidence[].causal_relation"),
        ("duplicate-evidence", "duplicate_or_conflicting_evidence", "$.evidence"),
        ("coverage", "evidence_count_or_coverage_policy_invalid", "$.evidence"),
        ("unsupported-ambiguous", "unsupported_or_ambiguous_proposition", "$.evidence[].proposition"),
        ("meaning-anchor", "generic_or_meaning_anchor_rejection", "$.evidence[]"),
    ),
    ids=lambda value: value,
)
def test_semantic_rejection_branches_emit_closed_safe_reason_and_path(
    tmp_path: Path,
    case_id: str,
    subreason: str,
    field_path: str,
) -> None:
    diagnostic, encoded = _semantic_diagnostic_probe(tmp_path, case_id)
    assert diagnostic.stable_subreason == subreason
    assert diagnostic.field_path == field_path
    assert diagnostic.response_top_level_exact_type == "dict"
    assert "Naz opened the blue notebook" not in encoded
    assert "Public evidence line" not in encoded
    assert "sk-super-sensitive-value" not in encoded
    assert "Unsupported synthesized proposition" not in encoded


def test_nested_diagnostic_reports_only_safe_shape_metadata(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path)
    bad = _base_extraction_payload(bundle)
    bad["evidence"][0]["exact_quotes"][0].pop("byte_end")
    bad["evidence"][0]["exact_quotes"][0]["unexpected_field"] = "private value must not escape"
    with pytest.raises(evidence.EvidenceContractError) as captured:
        evidence.parse_extraction_response(bad, bundle)
    payload = captured.value.diagnostic.safe_payload()
    assert payload["field_path"] == "$.evidence[].exact_quotes[]"
    assert payload["missing_keys"] == ["byte_end"]
    assert payload["extra_keys"] == ["unexpected_field"]
    assert {item["field_path"] for item in payload["list_item_counts"]} >= {
        "$.evidence", "$.evidence[].exact_quotes", "$.segment_dispositions",
    }
    assert "private value must not escape" not in evidence.json.dumps(payload)


@pytest.mark.parametrize(
    ("operation", "version"),
    (
        ("evidence_extraction", evidence.EVIDENCE_EXTRACTION_CONTRACT_VERSION),
        ("evidence_adjudication", evidence.EVIDENCE_ADJUDICATION_CONTRACT_VERSION),
    ),
    ids=("extraction", "adjudication"),
)
def test_provider_evidence_schema_is_closed_versioned_and_fresh(operation: str, version: str) -> None:
    required_ids = ("adjudication-run-binding",) if operation == "evidence_adjudication" else ()
    first = evidence.evidence_model_response_schema(operation, version, required_ids)
    second = evidence.evidence_model_response_schema(operation, version, required_ids)
    assert first == second and first is not second
    assert first["type"] == "object"
    assert first["additionalProperties"] is False
    assert set(first["required"]) == set(first["properties"])
    if operation == "evidence_adjudication":
        assert first["properties"]["run_id"]["const"] == required_ids[0]
        assert "evidence_digest" not in first["properties"]["decisions"]["items"]["properties"]
    first["properties"]["caller_mutation"] = {"type": "string"}
    assert "caller_mutation" not in second["properties"]


def _persisted_semantic_failure(reason: str, *, binding: str = "matched"):
    stage = "relation_binding" if "relation" in reason else "semantic_validation"
    diagnostic = evidence.EvidenceValidationDiagnostic(
        stage,
        reason,
        "$.evidence[].temporal_relation"
        if reason.startswith("temporal")
        else "$.evidence[].causal_relation"
        if reason.startswith("causal")
        else "$.evidence[].polarity",
        "str",
        (), (), (),
        (("$.evidence", "list"),),
        (("$.evidence", 5),),
        evidence.EVIDENCE_EXTRACTION_CONTRACT_VERSION,
        "not_applicable",
        "matched" if binding == "matched" else "mismatched",
        100,
        100,
    )
    summary = evidence.EvidenceCoverageSummary(
        6, 6, 6, 6, 0, 0, 0, 5, 0, 0, 0, 0, 0,
        "coverage_hard_invalid",
    )
    return evidence.CoverageFailureEvidence(
        "coverage_hard_invalid", stage, reason, summary, binding,
        evidence_diagnostic=diagnostic,
    )


@pytest.mark.parametrize(
    "reason",
    ("temporal_relation_mismatch", "causal_relation_mismatch", "polarity_mismatch"),
)
def test_persisted_typed_relation_conflicts_project_to_manual_attention(reason):
    projected = evidence.materializable_post_extraction_failure(
        _persisted_semantic_failure(reason)
    )
    assert projected.category == "coverage_incomplete"
    assert projected.summary.reason_code == "coverage_incomplete"
    assert projected.stable_reason == reason


@pytest.mark.parametrize("reason", ("malformed_json", "transport_failure"))
def test_hard_evidence_failures_cannot_be_materialized(reason):
    summary = evidence.EvidenceCoverageSummary(
        1, 1, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 1,
        "coverage_hard_invalid",
    )
    failure = evidence.CoverageFailureEvidence(
        "coverage_hard_invalid", "coverage_validation", reason, summary,
    )
    with pytest.raises(TypeError, match="post extraction failure"):
        evidence.materializable_post_extraction_failure(failure)


def test_source_identity_mismatch_cannot_be_materialized():
    with pytest.raises(TypeError, match="post extraction failure"):
        evidence.materializable_post_extraction_failure(
            _persisted_semantic_failure(
                "temporal_relation_mismatch", binding="mismatched",
            )
        )


@pytest.mark.parametrize(
    "relation_field",
    ("temporal_relation", "causal_relation"),
)
def test_provider_extraction_schema_requires_both_relation_operand_sides(
    relation_field: str,
) -> None:
    schema = evidence.evidence_model_response_schema(
        "evidence_extraction",
        evidence.EVIDENCE_EXTRACTION_CONTRACT_VERSION,
    )
    relation = schema["properties"]["evidence"]["items"]["properties"][relation_field]["anyOf"][0]
    assert relation["properties"]["left_operand_quote_ids"]["minItems"] == 1
    assert relation["properties"]["right_operand_quote_ids"]["minItems"] == 1


@pytest.mark.parametrize(
    ("relation_field", "relation_kind", "subreason"),
    (
        ("temporal_relation", "sequence", "temporal_relation_operands_incomplete"),
        ("causal_relation", "because", "causal_relation_operands_incomplete"),
    ),
)
def test_empty_relation_operand_side_has_exact_safe_diagnostic(
    tmp_path: Path,
    relation_field: str,
    relation_kind: str,
    subreason: str,
) -> None:
    bundle = _write_bundle(tmp_path)
    raw = _base_extraction_payload(bundle)
    raw["evidence"][0][relation_field] = {
        "relation_kind": relation_kind,
        "marker_quote_id": "quote-1",
        "left_operand_quote_ids": ["quote-1"],
        "right_operand_quote_ids": [],
    }
    with pytest.raises(evidence.EvidenceContractError) as captured:
        evidence.parse_extraction_response(raw, bundle)
    diagnostic = captured.value.diagnostic
    assert type(diagnostic) is evidence.EvidenceValidationDiagnostic
    assert diagnostic.stable_subreason == subreason
    assert diagnostic.field_path == f"$.evidence[].{relation_field}"
    assert captured.value.reason_code == "evidence_schema_invalid"


def test_module_has_no_normalizer_cp_or_network_dependency() -> None:
    source = Path(evidence.__file__).read_text(encoding="utf-8")
    assert "import narrative_normalizer" not in source
    assert "import narrative_generation" not in source
    assert "import narrative_translator" not in source
    assert "import requests" not in source
    assert "import httpx" not in source
    assert "import socket" not in source
    assert "urlopen(" not in source


def test_requested_integration_api_is_exported_exactly() -> None:
    required = {
        "build_source_document_bundle",
        "classify_source_bundle",
        "GenericEvidenceService",
        "verified_bundle_to_payload",
        "verified_bundle_from_payload",
        "revalidate_verified_bundle",
        "build_verified_fact_bindings",
        "verified_fact_binding_to_payload",
        "verified_fact_binding_from_payload",
    }
    assert required <= set(evidence.__all__)
    assert tuple(evidence.EvidenceResolution.__dataclass_fields__) == (
        "status", "verified_bundle", "model_call_count", "reason_code", "diagnostic",
        "coverage_summary", "coverage_failure", "fact_relation_summary",
        "selection_receipt",
    )
    assert isinstance(evidence.GenericEvidenceService, type)


def _coverage_payload(bundle, inventory, *, disposition="evidence_candidate"):
    return {
        "schema_version": evidence.EVIDENCE_COVERAGE_CONTRACT_VERSION,
        "source_identity": bundle.source_identity,
        "document_bundle_digest": bundle.bundle_digest,
        "inventory_digest": inventory.inventory_digest,
        "run_id": "coverage-test-run",
        "block_dispositions": {
            block.block_id: (
                "sensitive_withheld"
                if block.sensitivity_status == "sensitive_withheld"
                else disposition
            )
            for block in inventory.ordered_blocks
        },
    }


def _v2_extraction(bundle, inventory, plan):
    legacy = _base_extraction_payload(bundle)
    segment_id = legacy["evidence"][0]["ordered_segment_refs"][0]
    block_id = next(
        block.block_id for block in inventory.ordered_blocks
        if segment_id in block.ordered_segment_ids
    )
    return {
        "schema_version": evidence.EVIDENCE_EXTRACTION_V2_CONTRACT_VERSION,
        "source_identity": bundle.source_identity,
        "document_bundle_digest": bundle.bundle_digest,
        "coverage_plan_digest": plan.plan_digest,
        "run_id": "extract-test-run",
        "evidence": [dict(legacy["evidence"][0], ordered_block_refs=[block_id])],
    }


def _v3_extraction(bundle, inventory, plan, *, invalid_relation=False):
    segments = _all_segments(bundle)
    block_for_segment = {
        segment_id: block.block_id
        for block in inventory.ordered_blocks
        for segment_id in block.ordered_segment_ids
    }
    facts = []
    for index, segment in enumerate(segments[:3], start=1):
        facts.append({
            "fact_id": f"fact-{index}",
            "proposition": segment.exact_text,
            "evidence_kind": "observed_fact",
            "ordered_block_refs": [block_for_segment[segment.segment_id]],
            "ordered_segment_refs": [segment.segment_id],
            "exact_quotes": [_quote_payload(
                bundle, segment, quote_id=f"fact-quote-{index}",
            )],
            "entities": [], "numbers": [], "dates": [],
            "polarity": "affirmed", "uncertainty": "certain",
            "public_safety": "safe",
        })
    relations = []
    if len(segments) > 3:
        relations.append({
            "relation_id": "relation-1",
            "relation_kind": "temporal_after" if invalid_relation else "temporal_before",
            "left_fact_id": "fact-1",
            "right_fact_id": "fact-2",
            "support_quote": _quote_payload(
                bundle, segments[3], quote_id="relation-quote-1",
            ),
        })
    return {
        "schema_version": evidence.EVIDENCE_EXTRACTION_V3_CONTRACT_VERSION,
        "source_identity": bundle.source_identity,
        "document_bundle_digest": bundle.bundle_digest,
        "coverage_plan_digest": plan.plan_digest,
        "run_id": "extract-v3-test-run",
        "facts": facts,
        "relations": relations,
    }


def _span_selection(bundle, plan, segments=None):
    selected = tuple(_all_segments(bundle) if segments is None else segments)
    return {
        "schema_version": evidence.EVIDENCE_SPAN_SELECTION_CONTRACT_VERSION,
        "source_identity": bundle.source_identity,
        "document_bundle_digest": bundle.bundle_digest,
        "coverage_plan_digest": plan.plan_digest,
        "run_id": "span-selection-test-run",
        "selections": [
            {
                "selection_id": f"selection-{index}",
                "segment_id": segment.segment_id,
                "character_start": segment.character_start,
                "character_end": segment.character_end,
            }
            for index, segment in enumerate(selected, start=1)
        ],
    }


def test_span_selection_schema_physically_excludes_model_fact_fields():
    schema = evidence.evidence_model_response_schema(
        "evidence_extraction",
        evidence.EVIDENCE_SPAN_SELECTION_CONTRACT_VERSION,
        ("segment-001-000001",),
    )

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "schema_version", "source_identity", "document_bundle_digest",
        "coverage_plan_digest", "run_id", "selections",
    }
    selection = schema["properties"]["selections"]["items"]
    assert selection["additionalProperties"] is False
    assert set(selection["required"]) == {
        "selection_id", "segment_id", "character_start", "character_end",
    }
    forbidden = {
        "proposition", "fact", "fact_text", "entities", "numbers", "dates",
        "polarity", "uncertainty", "temporal_relation", "causal_relation",
    }
    assert forbidden.isdisjoint(schema["properties"])
    assert forbidden.isdisjoint(selection["properties"])


def test_span_selection_materializes_code_owned_exact_source_substrings(tmp_path):
    bundle = _write_bundle(
        tmp_path,
        {"facts.txt": "Alpha opened the notebook.\nBeta recorded 12 pages.\nGamma closed the cover."},
    )
    inventory = evidence.build_source_block_inventory(bundle)
    plan = evidence.parse_coverage_response(
        _coverage_payload(bundle, inventory), bundle, inventory,
    )
    segments = _all_segments(bundle)
    payload = _span_selection(bundle, plan, segments[:3])

    result = evidence.parse_span_selection_response(
        payload, bundle, inventory, plan,
    )

    assert result.summary.returned_fact_count == 3
    assert result.summary.valid_fact_count == 3
    assert result.summary.rejected_fact_count == 0
    assert tuple(
        item.proposition for item in result.extraction.ordered_evidence
    ) == tuple(item.exact_text for item in segments[:3])
    assert all(
        item.proposition == item.exact_quotes[0].exact_text
        for item in result.extraction.ordered_evidence
    )
    assert result.extraction.ordered_evidence[1].numbers[0].exact_lexeme == "12"


@pytest.mark.parametrize(
    "forbidden_field",
    (
        "proposition", "fact_text", "entities", "numbers", "dates",
        "polarity", "uncertainty", "temporal_relation", "causal_relation",
    ),
)
def test_span_selection_rejects_any_model_supplied_semantic_field(
    tmp_path, forbidden_field,
):
    bundle = _write_bundle(tmp_path)
    inventory = evidence.build_source_block_inventory(bundle)
    plan = evidence.parse_coverage_response(
        _coverage_payload(bundle, inventory), bundle, inventory,
    )
    payload = _span_selection(bundle, plan, _all_segments(bundle)[:1])
    payload["selections"][0][forbidden_field] = "forbidden"

    with pytest.raises(evidence.EvidenceContractError) as caught:
        evidence.parse_span_selection_response(payload, bundle, inventory, plan)

    assert caught.value.reason_code == "evidence_schema_invalid"
    assert caught.value.diagnostic is not None
    assert caught.value.diagnostic.field_path == "$.selections[]"


def test_span_selection_rejects_out_of_segment_and_relation_spans_independently(tmp_path):
    bundle = _write_bundle(
        tmp_path,
        {"facts.txt": "Alpha exists.\nBeta exists after Alpha.\nGamma exists."},
    )
    inventory = evidence.build_source_block_inventory(bundle)
    plan = evidence.parse_coverage_response(
        _coverage_payload(bundle, inventory), bundle, inventory,
    )
    segments = _all_segments(bundle)
    payload = _span_selection(bundle, plan, segments)
    payload["selections"].append({
        "selection_id": "selection-outside",
        "segment_id": segments[0].segment_id,
        "character_start": segments[0].character_start,
        "character_end": segments[0].character_end + 2,
    })

    result = evidence.parse_span_selection_response(
        payload, bundle, inventory, plan,
    )

    assert result.summary.returned_fact_count == 4
    assert result.summary.valid_fact_count == 2
    assert result.summary.rejected_fact_count == 2
    assert result.summary.returned_relation_count == 0
    assert result.summary.verified_fact_summaries == (
        "Alpha exists.", "Gamma exists.",
    )


def test_v3_three_valid_facts_survive_invalid_temporal_relation(tmp_path):
    bundle = _write_bundle(
        tmp_path,
        {"facts.txt": "Alpha exists.\nBeta exists.\nGamma exists.\nAlpha happened before Beta."},
    )
    inventory = evidence.build_source_block_inventory(bundle)
    plan = evidence.parse_coverage_response(
        _coverage_payload(bundle, inventory), bundle, inventory,
    )

    result = evidence.parse_extraction_v3_response(
        _v3_extraction(bundle, inventory, plan, invalid_relation=True),
        bundle, inventory, plan,
    )

    assert len(result.extraction.ordered_evidence) == 3
    assert result.summary.valid_fact_count == 3
    assert result.summary.rejected_fact_count == 0
    assert result.summary.verified_relation_count == 0
    assert result.summary.rejected_relation_count == 1
    assert result.summary.temporal_conflict_count == 1


def test_v3_hidden_temporal_claim_rejects_only_affected_fact(tmp_path):
    bundle = _write_bundle(
        tmp_path,
        {"facts.txt": "Alpha exists.\nBeta exists.\nGamma exists.\nDelta exists.\nDelta happened after Gamma."},
    )
    inventory = evidence.build_source_block_inventory(bundle)
    plan = evidence.parse_coverage_response(
        _coverage_payload(bundle, inventory), bundle, inventory,
    )
    payload = _v3_extraction(bundle, inventory, plan)
    segment = _all_segments(bundle)[4]
    block_id = next(
        block.block_id for block in inventory.ordered_blocks
        if segment.segment_id in block.ordered_segment_ids
    )
    payload["facts"].append({
        "fact_id": "fact-hidden-relation",
        "proposition": segment.exact_text,
        "evidence_kind": "observed_fact",
        "ordered_block_refs": [block_id],
        "ordered_segment_refs": [segment.segment_id],
        "exact_quotes": [_quote_payload(bundle, segment, quote_id="hidden-quote")],
        "entities": [], "numbers": [], "dates": [],
        "polarity": "affirmed", "uncertainty": "certain", "public_safety": "safe",
    })

    result = evidence.parse_extraction_v3_response(payload, bundle, inventory, plan)

    assert result.summary.returned_fact_count == 4
    assert result.summary.valid_fact_count == 3
    assert result.summary.rejected_fact_count == 1
    assert {item.evidence_id for item in result.extraction.ordered_evidence} == {
        "fact-1", "fact-2", "fact-3",
    }


def test_v3_closed_schema_separates_facts_and_relations():
    schema = evidence.evidence_model_response_schema(
        "evidence_extraction",
        evidence.EVIDENCE_EXTRACTION_V3_CONTRACT_VERSION,
        ("block-1",),
    )
    assert set(schema["required"]) == {
        "schema_version", "source_identity", "document_bundle_digest",
        "coverage_plan_digest", "run_id", "facts", "relations",
    }
    fact = schema["properties"]["facts"]["items"]
    assert "temporal_relation" not in fact["properties"]
    assert "causal_relation" not in fact["properties"]
    assert schema["properties"]["relations"]["items"]["additionalProperties"] is False


@pytest.mark.parametrize(
    ("relation_kind", "conflict_field"),
    (("causal", "causal_conflict_count"), ("contradicts", "polarity_conflict_count")),
)
def test_v3_relation_conflict_is_scoped_and_preserves_atomic_facts(
    tmp_path, relation_kind, conflict_field,
):
    bundle = _write_bundle(
        tmp_path,
        {"facts.txt": "Alpha exists.\nBeta exists.\nGamma exists.\nAlpha happened before Beta."},
    )
    inventory = evidence.build_source_block_inventory(bundle)
    plan = evidence.parse_coverage_response(
        _coverage_payload(bundle, inventory), bundle, inventory,
    )
    payload = _v3_extraction(bundle, inventory, plan)
    payload["relations"][0]["relation_kind"] = relation_kind

    result = evidence.parse_extraction_v3_response(payload, bundle, inventory, plan)

    assert result.summary.valid_fact_count == 3
    assert result.summary.rejected_relation_count == 1
    assert getattr(result.summary, conflict_field) == 1


def test_span_selection_complete_path_reaches_verified_evidence(tmp_path):
    bundle = _write_bundle(
        tmp_path,
        {"facts.txt": "Alpha exists.\nBeta exists.\nGamma exists.\nAlpha happened before Beta."},
    )
    inventory = evidence.build_source_block_inventory(bundle)
    coverage = _coverage_payload(bundle, inventory)
    plan = evidence.parse_coverage_response(coverage, bundle, inventory)
    extraction_raw = _span_selection(bundle, plan, _all_segments(bundle)[:3])
    parsed = evidence.parse_span_selection_response(
        extraction_raw, bundle, inventory, plan,
    )
    adjudication = _adjudication_payload(parsed.extraction)
    client = FakeEvidenceClient([coverage, extraction_raw, adjudication])

    result = evidence.GenericEvidenceService(
        client,
        extraction_model="content-model",
        adjudication_model="review-model",
        coverage_v2=True,
    ).resolve(bundle)

    assert result.status == "verified"
    assert result.model_call_count == 3
    assert result.fact_relation_summary.verified_relation_count == 0
    assert all(
        item.temporal_relation is None and item.causal_relation is None
        for item in result.verified_bundle.extraction.ordered_evidence
    )
    adjudication_payload = __import__("json").loads(client.requests[2].payload_json)
    assert "relations" not in adjudication_payload["extraction"]


def test_e9_extraction_checkpoint_is_persisted_before_adjudication(tmp_path):
    bundle = _write_bundle(
        tmp_path,
        {"facts.txt": "Alpha exists.\nBeta exists.\nGamma exists.\nDelta exists."},
    )
    inventory = evidence.build_source_block_inventory(bundle)
    coverage = _coverage_payload(bundle, inventory)
    plan = evidence.parse_coverage_response(coverage, bundle, inventory)
    extraction_raw = _span_selection(bundle, plan, _all_segments(bundle)[:4])
    parsed = evidence.parse_span_selection_response(
        extraction_raw, bundle, inventory, plan,
    )
    client = FakeEvidenceClient([
        coverage, extraction_raw, _adjudication_payload(parsed.extraction),
    ])
    stages = []

    result = evidence.GenericEvidenceService(
        client,
        extraction_model="content-model",
        adjudication_model="review-model",
        coverage_v2=True,
    ).resolve(
        bundle,
        stage_sink=lambda stage, value: (
            stages.append((stage, value)),
            value if stage == "code_owned_extraction" else None,
        )[1],
    )

    assert result.status == "verified"
    assert [stage for stage, _value in stages] == [
        "code_owned_extraction", "adjudication_bundle",
    ]
    checkpoint = stages[0][1]
    assert type(checkpoint) is evidence.CodeOwnedExtractionCheckpoint
    assert checkpoint.selection_receipt.accepted_code_owned_fact_count == 4
    assert checkpoint.selection_receipt.returned_selection_count == 4
    assert checkpoint.selection_receipt.rejected_selection_count == 0
    adjudication_request = json.loads(client.requests[2].payload_json)
    assert (
        adjudication_request["extraction"]["bundle_digest"]
        == checkpoint.extraction.bundle_digest
    )


@pytest.mark.parametrize(
    ("case", "stage", "path", "counter"),
    (
        ("malformed-json", "adjudication_json_parse", "$", None),
        ("wrong-version", "adjudication_contract_version", "$.schema_version", None),
        ("wrong-source", "adjudication_source_binding", "$.source_identity", None),
        ("wrong-bundle", "adjudication_source_binding", "$.extraction_bundle_digest", None),
        ("missing-decision", "adjudication_decision_binding", "$.decisions", "missing_decision_count"),
        ("duplicate-decision", "adjudication_decision_binding", "$.decisions", "duplicate_decision_count"),
        ("unknown-evidence", "adjudication_decision_binding", "$.decisions", "unknown_evidence_id_count"),
        ("extra-evidence-digest", "adjudication_semantic_validation", "$.decisions", None),
    ),
    ids=lambda value: value if type(value) is str else None,
)
def test_e9_adjudication_rejections_are_typed_and_persisted(
    tmp_path, case, stage, path, counter,
):
    bundle = _write_bundle(
        tmp_path,
        {"facts.txt": "Alpha exists.\nBeta exists.\nGamma exists.\nDelta exists."},
    )
    inventory = evidence.build_source_block_inventory(bundle)
    coverage = _coverage_payload(bundle, inventory)
    plan = evidence.parse_coverage_response(coverage, bundle, inventory)
    extraction_raw = _span_selection(bundle, plan, _all_segments(bundle)[:4])
    parsed = evidence.parse_span_selection_response(
        extraction_raw, bundle, inventory, plan,
    )
    response = _adjudication_payload(parsed.extraction)
    if case == "malformed-json":
        response = "not-json"
    elif case == "wrong-version":
        response["schema_version"] = "normalizer-evidence-adjudication-v99"
    elif case == "wrong-source":
        response["source_identity"] = "0" * 64
    elif case == "wrong-bundle":
        response["extraction_bundle_digest"] = "0" * 64
    elif case == "missing-decision":
        response["decisions"].pop()
    elif case == "duplicate-decision":
        response["decisions"].append(dict(response["decisions"][0]))
    elif case == "unknown-evidence":
        response["decisions"][0]["evidence_id"] = "unknown-evidence"
    else:
        response["decisions"][0]["evidence_digest"] = "0" * 64
    client = FakeEvidenceClient([coverage, extraction_raw, response])
    stages = []

    result = evidence.GenericEvidenceService(
        client,
        extraction_model="content-model",
        adjudication_model="review-model",
        coverage_v2=True,
    ).resolve(
        bundle,
        stage_sink=lambda name, value: (
            stages.append((name, value)),
            value if name == "code_owned_extraction" else None,
        )[1],
    )

    assert result.status == "failed"
    assert type(result.diagnostic) is evidence.AdjudicationValidationDiagnostic
    assert result.diagnostic.validation_stage == stage
    assert result.diagnostic.field_path == path
    assert [name for name, _value in stages] == [
        "code_owned_extraction", "adjudication_diagnostic",
    ]
    assert stages[-1][1] == result.diagnostic
    if counter is not None:
        assert getattr(result.diagnostic, counter) >= 1
    if case == "wrong-bundle":
        assert result.diagnostic.extraction_bundle_binding_result == "mismatched"
    if case == "extra-evidence-digest":
        assert result.diagnostic.evidence_digest_mismatch_count == 0
    assert result.model_call_count == 3


@pytest.mark.parametrize("supported_count", (0, 1, 2, 3))
def test_e9_valid_adjudication_maps_supported_fact_count(
    tmp_path, supported_count,
):
    bundle = _write_bundle(
        tmp_path,
        {"facts.txt": "Alpha exists.\nBeta exists.\nGamma exists.\nDelta exists."},
    )
    inventory = evidence.build_source_block_inventory(bundle)
    coverage = _coverage_payload(bundle, inventory)
    plan = evidence.parse_coverage_response(coverage, bundle, inventory)
    extraction_raw = _span_selection(bundle, plan, _all_segments(bundle)[:4])
    parsed = evidence.parse_span_selection_response(
        extraction_raw, bundle, inventory, plan,
    )
    adjudication = _adjudication_payload(parsed.extraction)
    for index, decision in enumerate(adjudication["decisions"]):
        if index >= supported_count:
            decision["decision"] = "rejected"
            decision["reason_codes"] = ["unsupported_proposition"]
    client = FakeEvidenceClient([coverage, extraction_raw, adjudication])
    stages = []

    result = evidence.GenericEvidenceService(
        client,
        extraction_model="content-model",
        adjudication_model="review-model",
        coverage_v2=True,
    ).resolve(
        bundle,
        stage_sink=lambda name, value: (
            stages.append((name, value)),
            value if name == "code_owned_extraction" else None,
        )[1],
    )

    assert result.status == ("verified" if supported_count >= 3 else "manual_attention")
    assert result.fact_relation_summary.valid_fact_count == supported_count
    assert len(result.fact_relation_summary.verified_fact_summaries) == supported_count
    assert [name for name, _value in stages] == [
        "code_owned_extraction", "adjudication_bundle",
    ]
    assert result.model_call_count == 3


def test_e9_verified_bundle_failure_persists_closed_diagnostic(tmp_path, monkeypatch):
    bundle = _write_bundle(
        tmp_path,
        {"facts.txt": "Alpha exists.\nBeta exists.\nGamma exists."},
    )
    inventory = evidence.build_source_block_inventory(bundle)
    coverage = _coverage_payload(bundle, inventory)
    plan = evidence.parse_coverage_response(coverage, bundle, inventory)
    extraction_raw = _span_selection(bundle, plan, _all_segments(bundle)[:3])
    parsed = evidence.parse_span_selection_response(
        extraction_raw, bundle, inventory, plan,
    )
    client = FakeEvidenceClient([
        coverage, extraction_raw, _adjudication_payload(parsed.extraction),
    ])
    stages = []

    def reject_verified(*_args):
        raise evidence.EvidenceContractError("evidence_verified_bundle_invalid")

    monkeypatch.setattr(evidence, "_make_verified", reject_verified)
    result = evidence.GenericEvidenceService(
        client,
        extraction_model="content-model",
        adjudication_model="review-model",
        coverage_v2=True,
    ).resolve(
        bundle,
        stage_sink=lambda name, value: (
            stages.append((name, value)),
            value if name == "code_owned_extraction" else None,
        )[1],
    )

    assert result.status == "failed"
    assert result.diagnostic.validation_stage == "verified_bundle_validation"
    assert [name for name, _value in stages] == [
        "code_owned_extraction", "adjudication_bundle", "adjudication_diagnostic",
    ]


def test_e9_stage_write_failure_stops_before_adjudication(tmp_path):
    bundle = _write_bundle(
        tmp_path,
        {"facts.txt": "Alpha exists.\nBeta exists.\nGamma exists."},
    )
    inventory = evidence.build_source_block_inventory(bundle)
    coverage = _coverage_payload(bundle, inventory)
    plan = evidence.parse_coverage_response(coverage, bundle, inventory)
    extraction_raw = _span_selection(bundle, plan, _all_segments(bundle)[:3])
    client = FakeEvidenceClient([coverage, extraction_raw])

    def fail_write(_stage, _payload):
        raise OSError("private persistence failure")

    with pytest.raises(evidence.EvidenceStagePersistenceError):
        evidence.GenericEvidenceService(
            client,
            extraction_model="content-model",
            adjudication_model="review-model",
            coverage_v2=True,
        ).resolve(bundle, stage_sink=fail_write)
    assert [request.request_kind for request in client.requests] == [
        "evidence_coverage", "evidence_extraction",
    ]


def test_e10_v2_attaches_code_owned_digests_in_canonical_order(tmp_path):
    bundle = _write_bundle(
        tmp_path,
        {"facts.txt": "Alpha exists.\nBeta exists.\nGamma exists.\nDelta exists."},
    )
    inventory = evidence.build_source_block_inventory(bundle)
    plan = evidence.parse_coverage_response(
        _coverage_payload(bundle, inventory), bundle, inventory,
    )
    parsed = evidence.parse_span_selection_response(
        _span_selection(bundle, plan, _all_segments(bundle)[:4]),
        bundle,
        inventory,
        plan,
    )
    response = _adjudication_payload(parsed.extraction)
    response["decisions"][-1]["decision"] = "rejected"
    response["decisions"][-1]["reason_codes"] = ["unsupported_proposition"]
    response["decisions"].reverse()

    adjudication = evidence.parse_adjudication_response(
        response, parsed.extraction,
    )

    assert adjudication.contract_version == "normalizer-evidence-adjudication-v2"
    assert [item.evidence_id for item in adjudication.ordered_decisions] == [
        item.evidence_id for item in parsed.extraction.ordered_evidence
    ]
    assert [item.evidence_digest for item in adjudication.ordered_decisions] == [
        evidence.evidence_digest(item) for item in parsed.extraction.ordered_evidence
    ]
    assert [item.decision for item in adjudication.ordered_decisions].count("supported") == 3
    assert [item.decision for item in adjudication.ordered_decisions].count("rejected") == 1
    assert all("evidence_digest" not in item for item in response["decisions"])


@pytest.mark.parametrize(
    ("case", "reason", "stage"),
    (
        ("extra-digest", "decision_schema_invalid", "adjudication_semantic_validation"),
        ("missing", "missing_evidence_id", "adjudication_decision_binding"),
        ("duplicate", "duplicate_evidence_id", "adjudication_decision_binding"),
        ("unknown", "unknown_evidence_id", "adjudication_decision_binding"),
        ("source", "source_identity_mismatch", "adjudication_source_binding"),
        ("extraction", "extraction_bundle_digest_mismatch", "adjudication_source_binding"),
        ("version", "contract_version_mismatch", "adjudication_contract_version"),
        ("decision", "invalid_decision_value", "adjudication_semantic_validation"),
        ("reason", "invalid_reason_code", "adjudication_semantic_validation"),
        ("malformed", "malformed_json", "adjudication_json_parse"),
    ),
)
def test_e10_v2_failures_have_closed_typed_diagnostics(tmp_path, case, reason, stage):
    bundle = _write_bundle(tmp_path, {"facts.txt": "Alpha exists.\nBeta exists."})
    extraction = _parse_base(bundle)
    response = _adjudication_payload(extraction)
    if case == "extra-digest":
        response["decisions"][0]["evidence_digest"] = "0" * 64
    elif case == "missing":
        response["decisions"].pop()
    elif case == "duplicate":
        response["decisions"].append(dict(response["decisions"][0]))
    elif case == "unknown":
        response["decisions"][0]["evidence_id"] = "unknown-evidence"
    elif case == "source":
        response["source_identity"] = "0" * 64
    elif case == "extraction":
        response["extraction_bundle_digest"] = "0" * 64
    elif case == "version":
        response["schema_version"] = evidence.EVIDENCE_ADJUDICATION_V1_CONTRACT_VERSION
    elif case == "decision":
        response["decisions"][0]["decision"] = "maybe"
    elif case == "reason":
        response["decisions"][0]["decision"] = "rejected"
        response["decisions"][0]["reason_codes"] = ["invented_reason"]
    else:
        response = "{not-json"

    with pytest.raises(evidence.EvidenceContractError) as caught:
        evidence.parse_adjudication_response(response, extraction)

    diagnostic = caught.value.diagnostic
    assert type(diagnostic) is evidence.AdjudicationValidationDiagnostic
    assert diagnostic.stable_reason == reason
    assert diagnostic.validation_stage == stage
    if case != "version":
        assert diagnostic.evidence_digest_mismatch_count == 0


def test_e10_v1_artifact_replays_and_historical_digest_mismatch_is_typed(tmp_path):
    bundle = _write_bundle(tmp_path)
    extraction = _parse_base(bundle)
    response = _adjudication_payload(extraction)
    response["schema_version"] = evidence.EVIDENCE_ADJUDICATION_V1_CONTRACT_VERSION
    for decision, item in zip(response["decisions"], extraction.ordered_evidence, strict=True):
        decision["evidence_digest"] = evidence.evidence_digest(item)
    adjudication = evidence.parse_adjudication_v1_response(response, extraction)
    verified = evidence._make_verified(bundle, extraction, adjudication)

    assert evidence.verified_bundle_from_payload(
        evidence.verified_bundle_to_payload(verified), bundle,
    ) == verified

    response["decisions"][0]["evidence_digest"] = "0" * 64
    with pytest.raises(evidence.EvidenceContractError) as caught:
        evidence.parse_adjudication_v1_response(response, extraction)
    assert caught.value.diagnostic.stable_reason == "evidence_digest_mismatch"
    assert caught.value.diagnostic.evidence_digest_mismatch_count == 1


def test_e10_adjudication_uses_reread_immutable_checkpoint(tmp_path):
    bundle = _write_bundle(
        tmp_path,
        {"facts.txt": "Alpha exists.\nBeta exists.\nGamma exists."},
    )
    inventory = evidence.build_source_block_inventory(bundle)
    coverage = _coverage_payload(bundle, inventory)
    plan = evidence.parse_coverage_response(coverage, bundle, inventory)
    selection = _span_selection(bundle, plan, _all_segments(bundle)[:3])
    parsed = evidence.parse_span_selection_response(selection, bundle, inventory, plan)
    client = FakeEvidenceClient([
        coverage, selection, _adjudication_payload(parsed.extraction),
    ])
    identities = []

    def persist_and_reread(stage, value):
        if stage != "code_owned_extraction":
            return None
        reread = evidence.CodeOwnedExtractionCheckpoint(
            value.coverage_plan_digest,
            evidence.extraction_bundle_from_payload(
                evidence._extraction_payload(value.extraction)
            ),
            evidence.EvidenceSelectionReceipt(**value.selection_receipt.safe_payload()),
        )
        identities.append((id(value), id(reread)))
        return reread

    result = evidence.GenericEvidenceService(
        client,
        extraction_model="content-model",
        adjudication_model="review-model",
        coverage_v2=True,
    ).resolve(bundle, stage_sink=persist_and_reread)

    assert result.status == "verified"
    assert identities and identities[0][0] != identities[0][1]
    assert result.verified_bundle.extraction == parsed.extraction


def test_span_selection_insufficient_facts_yield_useful_manual_attention(tmp_path):
    bundle = _write_bundle(tmp_path, {"facts.txt": "Alpha exists.\nBeta exists."})
    inventory = evidence.build_source_block_inventory(bundle)
    coverage = _coverage_payload(bundle, inventory)
    plan = evidence.parse_coverage_response(coverage, bundle, inventory)
    extraction_raw = _span_selection(bundle, plan)
    client = FakeEvidenceClient([coverage, extraction_raw])

    result = evidence.GenericEvidenceService(
        client,
        extraction_model="content-model",
        adjudication_model="review-model",
        coverage_v2=True,
    ).resolve(bundle)

    assert result.status == "manual_attention"
    assert result.model_call_count == 2
    assert result.coverage_failure.stable_reason == "independent_fact_count_insufficient"
    assert result.fact_relation_summary.valid_fact_count == 2
    assert result.fact_relation_summary.verified_fact_summaries == (
        "Alpha exists.", "Beta exists.",
    )
    assert [item.request_kind for item in client.requests] == [
        "evidence_coverage", "evidence_extraction",
    ]


def test_coverage_v2_twenty_one_segments_have_deterministic_bounded_blocks(tmp_path):
    text = "\n".join(f"Fact {index} is supported." for index in range(1, 22))
    first = _write_bundle(tmp_path, {"facts.txt": text})
    second = evidence.build_source_document_bundle(
        tmp_path / "inbox", SOURCE_REF, SOURCE_DIGEST, SOURCE_CONTRACT_VERSION
    )
    left = evidence.build_source_block_inventory(first)
    right = evidence.build_source_block_inventory(second)
    assert left == right
    assert sum(len(item.ordered_segment_ids) for item in left.ordered_blocks) == 21
    assert all(len(item.ordered_segment_ids) <= evidence.MAX_BLOCK_SEGMENTS for item in left.ordered_blocks)


def test_coverage_v2_six_hundred_thirteen_segments_remain_bounded(tmp_path):
    bundle = _write_bundle(
        tmp_path, {"facts.txt": "\n".join(f"Fact {index}." for index in range(613))}
    )
    inventory = evidence.build_source_block_inventory(bundle)
    assert sum(len(item.ordered_segment_ids) for item in inventory.ordered_blocks) == 613
    assert len(inventory.ordered_blocks) == 39
    assert all(len(item.ordered_segment_ids) <= 16 for item in inventory.ordered_blocks)


def test_coverage_v2_dynamic_schema_requires_every_exact_block_id(tmp_path):
    bundle = _write_bundle(tmp_path, {"facts.txt": "One.\nTwo.\nThree."})
    inventory = evidence.build_source_block_inventory(bundle)
    ids = tuple(item.block_id for item in inventory.ordered_blocks)
    schema = evidence.evidence_model_response_schema(
        "evidence_coverage", evidence.EVIDENCE_COVERAGE_CONTRACT_VERSION, ids
    )
    disposition = schema["properties"]["block_dispositions"]
    assert tuple(disposition["required"]) == ids
    assert disposition["additionalProperties"] is False


@pytest.mark.parametrize("mutation", ("missing", "extra"))
def test_coverage_v2_incomplete_or_extra_block_is_rejected(tmp_path, mutation):
    bundle = _write_bundle(tmp_path, {"facts.txt": "One.\nTwo."})
    inventory = evidence.build_source_block_inventory(bundle)
    payload = _coverage_payload(bundle, inventory)
    if mutation == "missing":
        payload["block_dispositions"].pop(next(iter(payload["block_dispositions"])))
    else:
        payload["block_dispositions"]["block-unknown"] = "context_only"
    with pytest.raises(evidence.EvidenceContractError) as caught:
        evidence.parse_coverage_response(payload, bundle, inventory)
    assert caught.value.reason_code == "evidence_coverage_incomplete"


def test_coverage_v2_duplicate_json_block_is_conflict(tmp_path):
    bundle = _write_bundle(tmp_path)
    inventory = evidence.build_source_block_inventory(bundle)
    payload = _coverage_payload(bundle, inventory)
    block_id = inventory.ordered_blocks[0].block_id
    raw = __import__("json").dumps(payload, separators=(",", ":"))
    raw = raw.replace(
        f'"{block_id}":"evidence_candidate"',
        f'"{block_id}":"evidence_candidate","{block_id}":"context_only"',
    )
    with pytest.raises(evidence.EvidenceContractError) as caught:
        evidence.parse_coverage_response(raw, bundle, inventory)
    assert caught.value.reason_code == "evidence_coverage_incomplete"
    assert caught.value.evidence.category == "coverage_incomplete"


def test_coverage_v2_expands_every_segment_exactly_once(tmp_path):
    bundle = _write_bundle(tmp_path, {"facts.txt": "First fact.\nSecond context.\nThird context."})
    inventory = evidence.build_source_block_inventory(bundle)
    plan = evidence.parse_coverage_response(_coverage_payload(bundle, inventory), bundle, inventory)
    parsed = evidence.parse_extraction_v2_response(
        _v2_extraction(bundle, inventory, plan), bundle, inventory, plan
    )
    expected = tuple(item.segment_id for item in _all_segments(bundle))
    actual = tuple(item.segment_id for item in parsed.ordered_segment_dispositions)
    assert actual == expected
    assert len(actual) == len(set(actual))


@pytest.mark.parametrize(
    "mutation,expected_category,expected_reason",
    (
        ("out-of-range", "coverage_incomplete", "independent_fact_count_insufficient"),
        ("wrong-segment", "coverage_incomplete", "independent_fact_count_insufficient"),
        ("duplicate-span", "coverage_incomplete", "independent_fact_count_insufficient"),
        ("source-binding", "coverage_hard_invalid", "source_or_document_binding_mismatch"),
    ),
)
def test_coverage_v2_post_extraction_rejection_is_typed_and_product_classified(
    tmp_path, mutation, expected_category, expected_reason,
):
    bundle = _write_bundle(tmp_path)
    inventory = evidence.build_source_block_inventory(bundle)
    coverage = _coverage_payload(bundle, inventory)
    plan = evidence.parse_coverage_response(coverage, bundle, inventory)
    extraction = _span_selection(bundle, plan)
    if mutation == "out-of-range":
        extraction["selections"][0]["character_end"] += 1
    elif mutation == "wrong-segment":
        extraction["selections"][0]["segment_id"] = "segment-unknown"
    elif mutation == "duplicate-span":
        extraction["selections"].append(dict(
            extraction["selections"][0], selection_id="selection-duplicate",
        ))
    else:
        extraction["source_identity"] = "0" * 64
    client = FakeEvidenceClient([coverage, extraction])

    result = evidence.GenericEvidenceService(
        client,
        extraction_model="content-model",
        adjudication_model="review-model",
        coverage_v2=True,
    ).resolve(bundle)

    assert result.status == (
        "manual_attention" if expected_category == "coverage_incomplete" else "failed"
    )
    assert result.model_call_count == 2
    assert [item.request_kind for item in client.requests] == [
        "evidence_coverage", "evidence_extraction",
    ]
    failure = result.coverage_failure
    assert type(failure) is evidence.CoverageFailureEvidence
    assert failure.category == expected_category
    assert failure.stable_reason == expected_reason
    if expected_category == "coverage_hard_invalid":
        assert type(failure.evidence_diagnostic) is evidence.EvidenceValidationDiagnostic
        assert failure.evidence_diagnostic.stable_subreason == expected_reason
    else:
        assert failure.evidence_diagnostic is None
        assert type(result.fact_relation_summary) is evidence.FactRelationValidationSummary
    assert evidence.coverage_failure_from_payload(failure.safe_payload()) == failure
    encoded = evidence.json.dumps(failure.safe_payload(), ensure_ascii=False)
    assert "segment-unknown" not in encoded


def test_coverage_v2_direct_parser_never_drops_post_extraction_diagnostic(tmp_path):
    bundle = _write_bundle(tmp_path)
    inventory = evidence.build_source_block_inventory(bundle)
    plan = evidence.parse_coverage_response(
        _coverage_payload(bundle, inventory), bundle, inventory,
    )
    extraction = _v2_extraction(bundle, inventory, plan)
    extraction["evidence"][0]["exact_quotes"][0]["character_end"] -= 1

    with pytest.raises(evidence.EvidenceContractError) as caught:
        evidence.parse_extraction_v2_response(
            extraction, bundle, inventory, plan,
        )

    assert type(caught.value.diagnostic) is evidence.EvidenceValidationDiagnostic
    assert caught.value.diagnostic.validation_stage == "quote_binding"
    assert caught.value.diagnostic.stable_subreason == "quote_span_or_ownership_invalid"


def test_coverage_v2_sensitive_block_text_never_enters_provider_projection(tmp_path):
    bundle = _write_bundle(tmp_path, {"facts.txt": "Public fact.\nAPI_KEY=sk-secret-value"})
    inventory = evidence.build_source_block_inventory(bundle)
    projection = evidence._block_projection(bundle, inventory)
    sensitive = [item for item in projection["blocks"] if item["sensitivity_status"] == "sensitive_withheld"]
    assert sensitive
    assert all("segments" not in item for item in sensitive)
    assert "sk-secret-value" not in __import__("json").dumps(projection)


def test_coverage_v2_ambiguous_plan_returns_useful_manual_summary(tmp_path):
    bundle = _write_bundle(tmp_path)
    inventory = evidence.build_source_block_inventory(bundle)
    response = _coverage_payload(bundle, inventory, disposition="ambiguous")
    client = FakeEvidenceClient([response])
    result = evidence.GenericEvidenceService(
        client,
        extraction_model="content-model",
        adjudication_model="review-model",
        coverage_v2=True,
    ).resolve(bundle)
    assert result.status == "manual_attention"
    assert result.model_call_count == 1
    assert result.coverage_summary is not None
    assert result.coverage_summary.ambiguous_count == len(inventory.ordered_blocks)
    assert [item.request_kind for item in client.requests] == ["evidence_coverage"]


def _coverage_resolution(bundle, response):
    client = FakeEvidenceClient([response])
    result = evidence.GenericEvidenceService(
        client,
        extraction_model="content-model",
        adjudication_model="review-model",
        coverage_v2=True,
    ).resolve(bundle)
    return result, client


def test_coverage_failure_missing_disposition_is_closed_manual_attention(tmp_path):
    bundle = _write_bundle(tmp_path, {"facts.txt": "One.\nTwo."})
    inventory = evidence.build_source_block_inventory(bundle)
    payload = _coverage_payload(bundle, inventory)
    payload["block_dispositions"].pop(next(iter(payload["block_dispositions"])))

    result, client = _coverage_resolution(bundle, payload)

    assert result.status == "manual_attention"
    assert result.coverage_failure is not None
    assert result.coverage_failure.stable_reason == "missing_block_disposition"
    assert result.coverage_summary.missing_disposition_count == 1
    assert len(client.requests) == 1


def test_coverage_failure_duplicate_disposition_is_closed_manual_attention(tmp_path):
    bundle = _write_bundle(tmp_path)
    inventory = evidence.build_source_block_inventory(bundle)
    payload = _coverage_payload(bundle, inventory)
    block_id = inventory.ordered_blocks[0].block_id
    raw = __import__("json").dumps(payload, separators=(",", ":")).replace(
        f'"{block_id}":"evidence_candidate"',
        f'"{block_id}":"evidence_candidate","{block_id}":"evidence_candidate"',
    )

    result, _client = _coverage_resolution(bundle, raw)

    assert result.status == "manual_attention"
    assert result.coverage_failure is not None
    assert result.coverage_failure.stable_reason == "duplicate_block_disposition"
    assert result.coverage_summary.duplicate_disposition_count == 1


def test_coverage_failure_conflicting_disposition_is_closed_manual_attention(tmp_path):
    bundle = _write_bundle(tmp_path)
    inventory = evidence.build_source_block_inventory(bundle)
    payload = _coverage_payload(bundle, inventory)
    block_id = inventory.ordered_blocks[0].block_id
    raw = __import__("json").dumps(payload, separators=(",", ":")).replace(
        f'"{block_id}":"evidence_candidate"',
        f'"{block_id}":"evidence_candidate","{block_id}":"context_only"',
    )

    result, _client = _coverage_resolution(bundle, raw)

    assert result.status == "manual_attention"
    assert result.coverage_failure is not None
    assert result.coverage_failure.stable_reason == "conflicting_block_disposition"
    assert result.coverage_summary.conflicting_disposition_count == 1


def test_coverage_failure_ambiguous_decision_is_closed_manual_attention(tmp_path):
    bundle = _write_bundle(tmp_path)
    inventory = evidence.build_source_block_inventory(bundle)

    result, _client = _coverage_resolution(
        bundle, _coverage_payload(bundle, inventory, disposition="ambiguous"),
    )

    assert result.status == "manual_attention"
    assert result.coverage_failure is not None
    assert result.coverage_failure.stable_reason == "ambiguous_coverage"
    assert result.coverage_summary.ambiguous_count == len(inventory.ordered_blocks)


@pytest.mark.parametrize(
    ("mutation", "stable_reason"),
    (
        ("malformed", "malformed_json"),
        ("schema", "wrong_schema_version"),
        ("source", "source_identity_mismatch"),
        ("scalar", "exact_scalar_type_violation"),
    ),
)
def test_hard_invalid_coverage_never_becomes_manual_attention(
    tmp_path, mutation, stable_reason,
):
    bundle = _write_bundle(tmp_path)
    inventory = evidence.build_source_block_inventory(bundle)
    payload = _coverage_payload(bundle, inventory)
    response = payload
    if mutation == "malformed":
        response = "{not-json"
    elif mutation == "schema":
        payload["schema_version"] = "unknown-coverage-version"
    elif mutation == "source":
        payload["source_identity"] = "0" * 64
    else:
        payload["block_dispositions"] = []

    result, client = _coverage_resolution(bundle, response)

    assert result.status == "failed"
    assert result.coverage_summary is not None
    assert result.coverage_failure is not None
    assert result.coverage_failure.category == "coverage_hard_invalid"
    assert result.coverage_failure.stable_reason == stable_reason
    assert result.coverage_failure.safe_payload()["category"] == "coverage_hard_invalid"
    assert evidence.classify_coverage_failure(result.coverage_failure) is None
    assert len(client.requests) == 1


def test_privacy_boundary_failure_never_becomes_manual_attention(tmp_path):
    bundle = _write_bundle(tmp_path)

    class PrivacyRejectingClient:
        def generate_json(self, request):
            del request
            raise RuntimeError("private provider detail must not escape")

    result = evidence.GenericEvidenceService(
        PrivacyRejectingClient(),
        extraction_model="content-model",
        adjudication_model="review-model",
        coverage_v2=True,
    ).resolve(bundle)

    assert result.status == "failed"
    assert result.reason_code == "evidence_provider_failed"
    assert result.coverage_summary is None
    assert result.coverage_failure is None


def test_twenty_one_expected_blocks_partial_dispositions_return_typed_incomplete(tmp_path):
    body = "\n\n".join(f"Supported fact {index}." for index in range(1, 22))
    bundle = _write_bundle(tmp_path, {"facts.txt": body})
    inventory = evidence.build_source_block_inventory(bundle)
    assert len(inventory.ordered_blocks) == 21
    payload = _coverage_payload(bundle, inventory)
    payload["block_dispositions"].pop(inventory.ordered_blocks[-1].block_id)

    result, client = _coverage_resolution(bundle, payload)

    assert result.status == "manual_attention"
    assert result.model_call_count == 1
    assert result.coverage_failure is not None
    assert result.coverage_failure.category == "coverage_incomplete"
    assert result.coverage_failure.stable_reason == "missing_block_disposition"
    assert result.coverage_failure.summary.block_count == 21
    assert result.coverage_failure.summary.returned_disposition_count == 20
    assert result.coverage_failure.summary.missing_disposition_count == 1
    assert [item.request_kind for item in client.requests] == ["evidence_coverage"]


def test_coverage_failure_safe_payload_round_trips_exactly(tmp_path):
    bundle = _write_bundle(tmp_path, {"facts.txt": "One.\n\nTwo."})
    inventory = evidence.build_source_block_inventory(bundle)
    payload = _coverage_payload(bundle, inventory)
    payload["block_dispositions"].pop(inventory.ordered_blocks[-1].block_id)
    result, _client = _coverage_resolution(bundle, payload)
    assert result.coverage_failure is not None

    encoded = result.coverage_failure.safe_payload()
    restored = evidence.coverage_failure_from_payload(encoded)

    assert restored == result.coverage_failure
    assert restored.safe_payload() == encoded
    assert not any(
        forbidden in __import__("json").dumps(encoded)
        for forbidden in ("Supported fact", "prompt", "credential", str(tmp_path))
    )


def test_transport_diagnostic_round_trips_without_raw_values():
    diagnostic = evidence.ProviderTransportDiagnostic(
        "http_rate_limited", "evidence_coverage", "openai/gpt-5.4-mini",
        429, "rate_limit", "req_safe_123", True, None, 1,
    )
    failure = evidence.coverage_hard_failure_for_request(
        9,
        stable_reason="transport_failure",
        transport_diagnostic=diagnostic,
    )

    payload = failure.safe_payload()
    assert payload["transport_diagnostic"] == diagnostic.safe_payload()
    assert evidence.coverage_failure_from_payload(payload) == failure
    assert frozenset(diagnostic.safe_payload()) == {
        "category", "operation", "model_id", "http_status", "provider_error_code",
        "provider_request_id", "response_received", "timeout_phase",
        "transport_attempt_count", "contract_version",
    }


@pytest.mark.parametrize(
    "mutation",
    (
        lambda value: {key: item for key, item in value.items() if key != "operation"},
        lambda value: dict(value, extra="forbidden"),
        lambda value: dict(value, response_received=1),
        lambda value: dict(value, timeout_phase="not_applicable"),
        lambda value: dict(value, transport_attempt_count=2),
        lambda value: dict(value, contract_version="wrong-version"),
    ),
    ids=("missing", "extra", "bool-coercion", "open-timeout", "retry-count", "version"),
)
def test_transport_diagnostic_payload_is_closed_and_exact(mutation):
    payload = evidence.ProviderTransportDiagnostic(
        "http_rate_limited", "evidence_coverage", "openai/gpt-5.4-mini",
        429, "rate_limit", "req_safe_123", True, None, 1,
    ).safe_payload()

    with pytest.raises(TypeError):
        evidence.provider_transport_diagnostic_from_payload(mutation(payload))


def test_legacy_coverage_failure_payload_remains_readable_and_byte_semantics_stable():
    failure = evidence.coverage_hard_failure_for_request(
        3,
        stable_reason="transport_failure",
    )

    payload = failure.safe_payload()
    assert "transport_diagnostic" not in payload
    assert evidence.coverage_failure_from_payload(payload) == failure


def test_incomplete_segment_partition_is_typed_manual_attention(tmp_path, monkeypatch):
    bundle = _write_bundle(tmp_path, {"facts.txt": "One.\n\nTwo."})
    original = evidence.build_source_block_inventory(bundle)
    shortened_blocks = original.ordered_blocks[:-1]
    inventory_core = {
        "contract_version": evidence.EVIDENCE_COVERAGE_CONTRACT_VERSION,
        "source_identity": bundle.source_identity,
        "document_bundle_digest": bundle.bundle_digest,
        "ordered_blocks": shortened_blocks,
    }
    incomplete = evidence.SourceBlockInventory(
        **inventory_core,
        inventory_digest=evidence._sha(inventory_core),
    )
    monkeypatch.setattr(evidence, "build_source_block_inventory", lambda _bundle: incomplete)
    client = FakeEvidenceClient([])

    result = evidence.GenericEvidenceService(
        client,
        extraction_model="content-model",
        adjudication_model="review-model",
        coverage_v2=True,
    ).resolve(bundle)

    assert result.status == "manual_attention"
    assert result.model_call_count == 0
    assert result.coverage_failure is not None
    assert result.coverage_failure.category == "coverage_incomplete"
    assert result.coverage_failure.stable_reason == "incomplete_segment_partition"
    assert client.requests == []
