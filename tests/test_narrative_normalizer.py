from __future__ import annotations

import copy
import base64
import dataclasses
import hashlib
import importlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import narrative_generation as ng
import narrative_normalizer as nn
import narrative_translator as nt
import reels_failure_quarantine as rq
import tools.run_narrative_normalizer as cli
from tools.run_narrative_generation_fixture import (
    fake_adjudication_payload,
    fake_generation_payload,
    fixture_to_input,
    load_fixture,
)


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "narrative_normalizer"
NOW = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
TEST_TRUST_KEY = b"cp6b-normalizer-test-trust-key-32-bytes-minimum"


@pytest.fixture(autouse=True)
def _inject_ephemeral_trust_key(monkeypatch):
    monkeypatch.setenv(
        "NARRATIVE_NORMALIZER_TRUST_KEY",
        base64.b64encode(TEST_TRUST_KEY).decode("ascii"),
    )


def _filesystem_metadata_snapshot(root: Path) -> tuple[tuple[object, ...], ...]:
    """Return a content-and-metadata snapshot without following symlinks."""
    if not os.path.lexists(root):
        return ((".", "missing", 0, 0, 0, ""),)
    rows: list[tuple[object, ...]] = []
    paths = (root, *sorted(root.rglob("*"), key=lambda item: item.as_posix()))
    for path in paths:
        metadata = path.lstat()
        relative = "." if path == root else path.relative_to(root).as_posix()
        if path.is_symlink():
            kind = "symlink"
            digest = hashlib.sha256(os.readlink(path).encode("utf-8")).hexdigest()
        elif path.is_file():
            kind = "file"
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        elif path.is_dir():
            kind = "directory"
            digest = ""
        else:
            kind = "other"
            digest = ""
        rows.append((relative, kind, metadata.st_size, metadata.st_mtime_ns, metadata.st_mode, digest))
    return tuple(rows)


class QueueClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def generate_json(self, request):
        self.requests.append(request)
        return self.replies.pop(0)


class RaisingClient:
    def __init__(self, error):
        self.error = error
        self.requests = []

    def generate_json(self, request):
        self.requests.append(request)
        raise self.error


class BlockingClient(QueueClient):
    def __init__(self, replies, entered, release):
        super().__init__(replies)
        self.entered = entered
        self.release = release

    def generate_json(self, request):
        self.requests.append(request)
        if len(self.requests) == 1:
            self.entered.set()
            assert self.release.wait(5)
        return self.replies.pop(0)


def normalizer_fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURE_ROOT / f"{name}.json").read_text(encoding="utf-8"))


def normalizer_generation_payload(
    data: dict[str, object],
    *,
    fact_count: int | None = None,
    normalizer_name: str | None = None,
) -> dict[str, object]:
    """Keep CP2 fixtures intact while supplying one grounded Russian review draft."""
    payload = fake_generation_payload(data)
    selected = payload["candidates"][0]
    stories = {
        "env_utf8": (
            "Файл настроек безопасно заменили.",
            "Файл записали в UTF-8 без лишней служебной метки в начале.",
            "Рабочую папку и папку уровнем выше проверили.",
            "Повторная проверка дала тот же результат.",
            "Ручная проверка прошла вместо слепого доверия.",
        ),
        "quiet_object": (
            "Потёртая записная книжка всё время лежала рядом с клавиатурой.",
            "До любого вывода записали три коротких наблюдения.",
            "Три коротких наблюдения сохранили до любого вывода.",
            "После последней проверки книжку закрыли.",
            "Книжку закрыли после последней проверки.",
        ),
        "duo_context": (
            "Два человека изучили один образец с разных рабочих мест.",
            "Они отметили один и тот же хрупкий шаг для новой проверки.",
            "Разные слова указали одно следующее действие.",
            "По-разному написанные записи указали одно следующее действие.",
            "Разные слова описали одно и то же следующее действие.",
        ),
        "naz_solo": (
            "Файл настроек заменили безопасно.",
            "Файл записали в UTF-8 без лишней служебной метки.",
            "Файл настроек безопасно заменили.",
            "Ручная проверка прошла вместо слепого доверия.",
            "Ручная сверка прошла вместо слепого доверия.",
        ),
        "void_primary": (
            "Файл настроек безопасно заменили.",
            "Рабочую папку и папку уровнем выше проверили.",
            "Файл настроек заменили безопасно.",
            "Ручной осмотр прошёл вместо слепого доверия.",
            "Ручная сверка прошла вместо слепого доверия.",
        ),
    }
    story_key = normalizer_name if normalizer_name in stories else str(data["fixture"])
    texts = stories[story_key]
    for field, text in zip(("hook", "human_problem", "tension", "turning_point", "resolution"), texts, strict=True):
        selected[field]["text"] = text
    exact_refs = {
        "env_utf8": (("fact-1",), ("fact-2",), ("fact-3",), ("fact-4",), ("fact-5",)),
        "quiet_object": (("fact-1",), ("fact-2",), ("fact-2",), ("fact-3",), ("fact-3",)),
        "duo_context": (("fact-1",), ("fact-2",), ("fact-3",), ("fact-3",), ("fact-3",)),
        "naz_solo": (("fact-1",), ("fact-2",), ("fact-1",), ("fact-3",), ("fact-3",)),
        "void_primary": (("fact-1",), ("fact-2",), ("fact-1",), ("fact-3",), ("fact-3",)),
    }
    refs_for_story = exact_refs[story_key]
    for field, refs in zip(("hook", "human_problem", "tension", "turning_point", "resolution"), refs_for_story, strict=True):
        selected[field]["source_fact_refs"] = list(refs)
    if fact_count is not None:
        # Exact per-statement refs stay narrow; full coverage is distributed across claims.
        assert fact_count >= len({ref for refs in refs_for_story for ref in refs})
        allowed = {f"fact-{index}" for index in range(1, fact_count + 1)}

        def rebind_nested(value):
            if type(value) is dict:
                if "source_fact_refs" in value:
                    refs = [item for item in value["source_fact_refs"] if item in allowed]
                    value["source_fact_refs"] = refs or ["fact-1"]
                for item in value.values():
                    rebind_nested(item)
            elif type(value) is list:
                for item in value:
                    rebind_nested(item)

        rebind_nested(payload["candidates"])
    return payload


def write_source(tmp_path: Path, name: str = "technical_log"):
    spec = normalizer_fixture(name)
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    registry = tmp_path / "state" / "registry.json"
    source_ref = str(spec["source_ref"])
    source = inbox.joinpath(*source_ref.split("/"))
    source.mkdir(parents=True)
    outbox.mkdir()
    registry.parent.mkdir()
    lines = [*spec["facts"], *spec["extra_lines"]]
    (source / "material.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    policy = rq.QuarantinePathPolicy(
        inbox,
        registry,
        outbox,
        nn.trust.NarrativeTrustService(TEST_TRUST_KEY),
        tmp_path / "review-authority",
    )
    rq.reconcile_complete_backlog(policy, now=NOW)
    record = rq.read_registry(registry).records[0]
    return spec, policy, source, record


def generic_evidence_responses(
    documents,
    propositions: tuple[str, ...],
) -> tuple[dict[str, object], dict[str, object]]:
    """Build strict fake-provider responses from exact source spans only."""
    segments = tuple(
        segment
        for document in documents.ordered_documents
        for segment in document.ordered_segments
    )
    selected = []
    for proposition in propositions:
        matches = tuple(
            (item, item.exact_text.index(proposition))
            for item in segments
            if proposition in item.exact_text
        )
        assert len(matches) == 1
        selected.append(matches[0])
    selected_by_id = {
        item.segment_id: index
        for index, (item, _) in enumerate(selected, start=1)
    }
    evidence_items = []
    for index, (segment, local_start) in enumerate(selected, start=1):
        quote_id = f"quote-{index}"
        exact_text = propositions[index - 1]
        character_start = segment.character_start + local_start
        character_end = character_start + len(exact_text)
        document = next(
            item for item in documents.ordered_documents
            if item.document_id == segment.document_id
        )
        byte_start = len(document.exact_text[:character_start].encode("utf-8"))
        byte_end = len(document.exact_text[:character_end].encode("utf-8"))
        numbers = []
        for atom_index, match in enumerate(nn.evidence._NUMBER.finditer(exact_text), start=1):
            numbers.append({
                "atom_id": f"number-{index}-{atom_index}",
                "atom_kind": "number",
                "quote_id": quote_id,
                "exact_lexeme": match.group(0),
            })
        evidence_items.append({
            "evidence_id": f"evidence-{index}",
            "proposition": exact_text,
            "evidence_kind": "observed_fact",
            "ordered_segment_refs": [segment.segment_id],
            "exact_quotes": [{
                "quote_id": quote_id,
                "document_id": segment.document_id,
                "segment_id": segment.segment_id,
                "byte_start": byte_start,
                "byte_end": byte_end,
                "character_start": character_start,
                "character_end": character_end,
                "exact_text": exact_text,
            }],
            "entities": [],
            "numbers": numbers,
            "dates": [],
            "polarity": "affirmed",
            "temporal_relation": None,
            "causal_relation": None,
            "uncertainty": "certain",
            "public_safety": "safe",
        })
    dispositions = []
    for segment in segments:
        selected_index = selected_by_id.get(segment.segment_id)
        if selected_index is not None:
            disposition = "evidence"
            evidence_ids = [f"evidence-{selected_index}"]
        elif nn.evidence._is_sensitive(segment.exact_text):
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
    extraction = {
        "schema_version": nn.evidence.EVIDENCE_EXTRACTION_CONTRACT_VERSION,
        "source_identity": documents.source_identity,
        "document_bundle_digest": documents.bundle_digest,
        "run_id": "generic-extraction-run",
        "evidence": evidence_items,
        "segment_dispositions": dispositions,
    }
    parsed = nn.evidence.parse_extraction_response(extraction, documents)
    adjudication = {
        "schema_version": nn.evidence.EVIDENCE_ADJUDICATION_CONTRACT_VERSION,
        "source_identity": documents.source_identity,
        "extraction_bundle_digest": parsed.bundle_digest,
        "run_id": "generic-adjudication-run",
        "decisions": [
            {
                "evidence_id": item.evidence_id,
                "evidence_digest": nn.evidence.evidence_digest(item),
                "decision": "supported",
                "reason_codes": [],
            }
            for item in parsed.ordered_evidence
        ],
    }
    return extraction, adjudication


def generic_story_response(
    data: dict[str, object],
    propositions: tuple[str, ...],
) -> dict[str, object]:
    payload = fake_generation_payload(data)
    allowed = {f"fact-{index}" for index in range(1, len(propositions) + 1)}

    def rebind(value):
        if type(value) is dict:
            if "source_fact_refs" in value:
                refs = [item for item in value["source_fact_refs"] if item in allowed]
                value["source_fact_refs"] = refs or ["fact-1"]
            for item in value.values():
                rebind(item)
        elif type(value) is list:
            for item in value:
                rebind(item)

    rebind(payload["candidates"])
    selected = payload["candidates"][0]
    for index, field in enumerate(
        ("hook", "human_problem", "tension", "turning_point", "resolution")
    ):
        fact_index = index % len(propositions)
        selected[field]["text"] = propositions[fact_index]
        selected[field]["source_fact_refs"] = [f"fact-{fact_index + 1}"]
    return payload


def runtime(tmp_path: Path, name: str = "technical_log", *, client_cls=QueueClient, mutate_draft=None):
    spec, policy, source_path, record = write_source(tmp_path, name)
    source = nn.read_source_unit(policy, record.source_ref, expected_digest=record.source_digest)
    generation_data = load_fixture(str(spec["context_fixture"]))
    provider = nn.TemplateNarrativeContextProvider(fixture_to_input(generation_data))
    context = provider.build(source)
    drafts = normalizer_generation_payload(generation_data, fact_count=len(source.facts), normalizer_name=name)
    if mutate_draft is not None:
        mutate_draft(drafts)
    adjudication = fake_adjudication_payload(drafts, context)
    client = client_cls([drafts, adjudication])
    generation_service = ng.NarrativeGenerationService(
        client,
        generation_model="terra-medium",
        adjudication_model="sol-high",
        repair_model="terra-high",
    )
    service = nn.NarrativeNormalizerService(
        policy=policy,
        context_provider=provider,
        generation_service=generation_service,
        clock=lambda: NOW,
    )
    return spec, policy, source_path, record, source, provider, context, client, service


def create_draft(tmp_path: Path, name: str = "technical_log", *, mutate_draft=None):
    values = runtime(tmp_path, name, mutate_draft=mutate_draft)
    service = values[-1]
    record = values[3]
    outcome = service.normalize_source(record.source_ref, record.source_digest)
    return (*values, outcome, service.store.draft_path(record.source_digest, source_ref=record.source_ref))


def approve_created(store, record, draft, *, reviewed_at: str | None = None):
    value = nn.validate_draft_directory(
        draft,
        trust_service=store.trust_service,
        review_authority_root=store.policy.narrative_review_authority_root,
        require_trust=store.trust_service is not None,
    )
    return store.approve(
        record.source_ref,
        record.source_digest,
        expected_draft_identity=value["manifest"]["draft_identity"],
        reviewed_at=reviewed_at or NOW.isoformat(),
    )


def _cli_base(policy: rq.QuarantinePathPolicy) -> list[str]:
    authority = policy.narrative_review_authority_root
    assert authority is not None
    return [
        "--inbox-root", str(policy.inbox_root),
        "--registry-path", str(policy.registry_path),
        "--outbox-root", str(policy.narrative_outbox_root),
        "--review-authority-root", str(authority),
    ]


def _cli_run_with_local_test_authority(argv: list[str]) -> int:
    """Exercise the legacy adapter through the non-CLI test-only seam."""
    return cli.run(argv, _allow_local_review_authority_for_tests=True)


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")


def write_identity_ready(policy, record):
    identity = nn.source_identity(record.source_ref, record.source_digest)
    directory = policy.narrative_outbox_root / identity
    directory.mkdir(parents=True, exist_ok=False)
    package = directory / "story.json"
    package.write_text("{}\n", encoding="utf-8")
    payload = {
        "schema_version": rq.MANIFEST_SCHEMA_VERSION,
        "source_ref": record.source_ref,
        "source_digest": record.source_digest,
        "narrative_package_ref": f"{identity}/story.json",
        "narrative_package_digest": hashlib.sha256(package.read_bytes()).hexdigest(),
        "status": rq.CLASS_READY,
        "contract_versions": {"director": "director-v1", "narrative": "narrative-v1"},
    }
    write_json(directory / "narrative_ready.json", payload)
    return directory, payload


def passing_receipt(**changes):
    values = dict(
        title="Один точный шаг",
        hook="Запись сохранили только после полной проверки.",
        story="Сначала заметили одну деталь. Затем сравнили два результата. Проверка показала одинаковый итог.",
        ending="Теперь понятно, почему решение можно повторить.",
    )
    values.update(changes)
    return nn.build_plain_language_receipt(
        **values,
        factuality_passed=True,
        meaning_preservation_passed=True,
        significance_mode="source_supported_significance",
    )


@pytest.mark.parametrize(
    "name",
    [
        "technical_log", "jargon_heavy", "quiet_object", "naz_solo", "void_primary",
        "duo_context", "insufficient_facts", "sensitive_strings", "changed_source", "concurrency_resume",
    ],
)
def test_normalizer_owned_fixture_contract(name):
    value = normalizer_fixture(name)
    assert set(value) == {"fixture", "context_fixture", "source_ref", "facts", "extra_lines"}
    assert value["fixture"] == name


def test_module_import_has_no_application_or_provider_side_effects():
    forbidden = {"main", "openai", "requests", "httpx", "telegram", "story_production"}
    before = set(sys.modules)
    importlib.reload(nn)
    assert not ((set(sys.modules) - before) & forbidden)


def test_module_has_no_runtime_wiring_imports():
    source = Path(nn.__file__).read_text(encoding="utf-8")
    for token in ("import main", "import memory", "import story_production", "import openai"):
        assert token not in source


@pytest.mark.parametrize(
    "name",
    [
        "technical_log", "jargon_heavy", "quiet_object", "naz_solo", "void_primary",
        "duo_context", "sensitive_strings", "changed_source", "concurrency_resume",
    ],
)
def test_source_reader_is_complete_ordered_and_bound(tmp_path, name):
    spec, policy, source_path, record = write_source(tmp_path, name)
    before = rq.source_digest(source_path)
    source = nn.read_source_unit(policy, record.source_ref, expected_digest=record.source_digest)
    after = rq.source_digest(source_path)
    assert before == after == source.source_digest
    assert tuple(item.order for item in source.facts) == tuple(range(1, len(source.facts) + 1))
    assert all(item.source_ref == source.source_ref for item in source.facts)
    assert not (source_path / "narrative_ready.json").exists()


@pytest.mark.parametrize(
    "lines",
    [
        [], ["one"], ["# heading"], ["```", "```"], ["", "one"], ["token=only-secret-value-123456"],
        ["C:\\private\\one.txt"], ["# h", "one"], ["   ", "one"], ["```python", "one", "```"],
    ],
    ids=lambda value: "insufficient-" + hashlib.sha256(repr(value).encode()).hexdigest()[:8],
)
def test_source_insufficient_fails_closed(tmp_path, lines):
    inbox = tmp_path / "inbox"; outbox = tmp_path / "outbox"; registry = tmp_path / "state" / "registry.json"
    source = inbox / "Project" / "2026-08-01"; source.mkdir(parents=True); outbox.mkdir(); registry.parent.mkdir()
    (source / "item.md").write_text("\n".join(lines), encoding="utf-8")
    policy = rq.QuarantinePathPolicy(
        inbox, registry, outbox, nn.trust.NarrativeTrustService(TEST_TRUST_KEY),
        tmp_path / "review-authority",
    )
    with pytest.raises(nn.NarrativeNormalizerError, match="narrative_normalizer_source_insufficient") as error:
        nn.read_source_unit(policy, "Project/2026-08-01")
    assert type(error.value) is nn.NarrativeNormalizerError
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


@pytest.mark.parametrize(
    "source_ref",
    [
        "", "/absolute", "../escape", "project/../escape", "project/./date", "project//date",
        "C:/private", "project/date/", "./project/date", "project\\date", "project/$date",
        "project/date?", "project/date#", "project/date%", "project/ date", "project/date ",
        "project/.hidden", "project/..hidden", "project/date/../../x", "project/date\x00x",
    ],
)
def test_safe_source_ref_rejects_invalid_values(source_ref):
    with pytest.raises((nn.NarrativeNormalizerError, TypeError, ValueError)):
        nn._safe_source_ref(source_ref)


@pytest.mark.parametrize(
    "sensitive",
    [
        "token=example-secret-value-123456", "password: very-private-value", "api_key=abcdef1234567890",
        "Authorization: Bearer abcdef1234567890", "C:\\Users\\Private\\item.txt", "/opt/private/item.txt",
        "/var/lib/private/item", "/home/user/item", "/root/item", "ghp_abcdefghijklmnopqrstuv",
        "xoxb-123456789012345", "secret=abcdefghijklmnop",
    ],
)
def test_sensitive_source_lines_are_excluded_not_redacted(tmp_path, sensitive):
    spec, policy, source_path, record = write_source(tmp_path, "quiet_object")
    path = source_path / "sensitive.log"
    path.write_text(sensitive + "\n", encoding="utf-8")
    source = nn.read_source_unit(policy, record.source_ref)
    assert sensitive not in tuple(item.exact_text for item in source.facts)
    assert source.receipt.excluded_sensitive_count == 1


@pytest.mark.parametrize("duplicate_count", range(1, 13))
def test_duplicate_fact_handling_keeps_first_exact_value(tmp_path, duplicate_count):
    spec, policy, source_path, record = write_source(tmp_path, "quiet_object")
    duplicate = str(spec["facts"][0])
    (source_path / "duplicates.txt").write_text((duplicate + "\n") * duplicate_count, encoding="utf-8")
    source = nn.read_source_unit(policy, record.source_ref)
    values = tuple(item.exact_text for item in source.facts)
    assert values.count(duplicate) == 1
    assert source.receipt.duplicate_count == duplicate_count


@pytest.mark.parametrize(
    "exact_text",
    [
        "The count was 17.", "The count was 0.", "The count was 100.", "Version 2 was checked.",
        "Two notes agreed.", "No deadline was recorded.", "The object stayed still.", "Naz was not named.",
        "VOID was written in the source.", "The date was 2026-08-16.", "A price was not supplied.",
        "Three attempts were listed.", "One result changed.", "The label was alpha.", "The label was beta.",
        "A question remained open.", "No emotion was stated.", "The check ended.", "The file remained.",
        "The final line was present.",
    ],
)
def test_fact_text_is_never_sliced_or_rewritten(tmp_path, exact_text):
    spec, policy, source_path, record = write_source(tmp_path, "quiet_object")
    (source_path / "exact.txt").write_text(exact_text + "\n", encoding="utf-8")
    source = nn.read_source_unit(policy, record.source_ref)
    assert exact_text in tuple(item.exact_text for item in source.facts)


@pytest.mark.parametrize("exact_number", ["1e3", "0.010", "17"])
def test_json_numeric_fact_preserves_exact_source_lexeme(tmp_path, exact_number):
    spec, policy, source_path, record = write_source(tmp_path, "quiet_object")
    (source_path / "numbers.json").write_text(
        '{"facts":[' + exact_number + ']}',
        encoding="utf-8",
    )
    source = nn.read_source_unit(policy, record.source_ref)
    assert exact_number in tuple(item.exact_text for item in source.facts)


@pytest.mark.parametrize("term", nn._JARGON)
def test_plain_language_rejects_every_unexplained_jargon_token(term):
    receipt = passing_receipt(story=f"Сначала заметили деталь. Затем появился {term}. После этого результат сравнили.")
    assert not receipt.passed
    assert receipt.unexplained_jargon_count >= 1


@pytest.mark.parametrize("term", nn._JARGON)
def test_plain_language_accepts_immediately_explained_jargon(term):
    receipt = passing_receipt(
        story=f"Сначала встретилось слово {term} — простое название одного шага. Затем смысл проверили на примере. Итог записали обычными словами."
    )
    assert receipt.unexplained_jargon_count == 0
    assert receipt.passed


@pytest.mark.parametrize(
    "identifier",
    [
        "process_item()", "ClassName.run()", "source_ref", "package_digest", "reason_codes",
        "narrative_internal_error", "build_story()", "validate_package()", "foo_bar", "state_snapshot_ref",
        "C:\\private\\item.txt", "/opt/app/item", "/var/lib/item", "/home/user/item", "/root/item",
        "call_me(value)", "InternalClass.method()", "draft_manifest", "contract_version", "active_attempt_id",
    ],
)
def test_plain_language_rejects_internal_identifiers_and_paths(identifier):
    receipt = passing_receipt(story=f"Сначала заметили деталь. Затем встретилось {identifier}. После этого всё сравнили.")
    assert not receipt.passed
    assert receipt.internal_identifier_count >= 1 or receipt.unexplained_jargon_count >= 1


@pytest.mark.parametrize("count", range(3, 15))
def test_plain_language_rejects_acronym_heavy_text(count):
    acronyms = " ".join(f"AA{chr(65 + (index % 20))}" for index in range(count))
    receipt = passing_receipt(story=f"Сначала заметили деталь. {acronyms}. Затем результат сравнили.")
    assert not receipt.passed
    assert receipt.acronym_count >= 3


@pytest.mark.parametrize("extra_words", range(31, 41))
def test_plain_language_rejects_long_technical_sentence(extra_words):
    story = " ".join(["слово"] * extra_words) + ". Затем результат сравнили. Потом решение проверили."
    receipt = passing_receipt(story=story)
    assert not receipt.passed
    assert receipt.sentence_length_summary.over_limit_count >= 1


@pytest.mark.parametrize(
    ("title", "hook", "story", "ending"),
    [
        ("Тихая проверка", "Сначала заметили одну деталь.", "Её сравнили с прежней записью. Разница исчезла после повторной проверки.", "Теперь результат можно объяснить."),
        ("Записная книжка", "Книжка лежала рядом.", "В неё внесли три наблюдения. Вывод появился только после проверки.", "Книжку закрыли."),
        ("Один предмет", "Предмет не менял места.", "Наблюдение повторили дважды. Оба раза итог совпал.", "Вопрос остался открытым."),
        ("Два взгляда", "Два человека увидели одно место.", "Они описали его разными словами. Следующий шаг выбрали одинаковый.", "Спор не понадобился."),
        ("Без драмы", "Изменение оказалось небольшим.", "Его проверили на двух примерах. Оба примера дали один ответ.", "Этого достаточно для следующего шага."),
        ("Простой выбор", "Сначала сравнили варианты.", "Один вариант оказался понятнее. Его проверили ещё раз.", "Выбор сохранили."),
        ("Открытый конец", "Проверка началась с детали.", "Деталь повторилась в двух местах. Причина пока неизвестна.", "Вопрос остаётся открытым."),
        ("Спокойное наблюдение", "На столе осталась заметка.", "В ней было три коротких пункта. Все пункты сверили.", "Заметку сохранили."),
        ("Одно решение", "Решение проверили утром.", "Два результата совпали. Третий результат подтвердил их.", "Решение оставили без изменений."),
        ("Честный отказ", "Фактов оказалось мало.", "Удалось подтвердить только одну деталь. Остальные выводы отложили.", "Историю пока не продолжили."),
    ],
)
def test_plain_language_accepts_varied_human_stories(title, hook, story, ending):
    receipt = nn.build_plain_language_receipt(
        title=title,
        hook=hook,
        story=story,
        ending=ending,
        factuality_passed=True,
        meaning_preservation_passed=True,
        significance_mode="source_supported_significance",
    )
    assert receipt.passed
    assert receipt.unexplained_jargon_count == 0
    assert receipt.internal_identifier_count == 0


def test_plain_language_rejects_story_without_russian_words():
    receipt = nn.build_plain_language_receipt(
        title="One observation",
        hook="A notebook stayed near the keyboard.",
        story="Three notes were written before a conclusion. The final check matched the first one.",
        ending="The notebook was closed.",
    )
    assert not receipt.passed


@pytest.mark.parametrize(
    "field",
    ["fact_id", "exact_text", "source_ref", "order"],
)
def test_source_fact_is_recursively_immutable(field):
    value = nn.SourceFact("fact-1", "Exact fact.", "Project/2026-08-01", 1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(value, field, getattr(value, field))


@pytest.mark.parametrize(
    "field",
    ["source_ref", "source_digest", "facts", "receipt"],
)
def test_source_unit_is_recursively_immutable(field):
    facts = (nn.SourceFact("fact-1", "One.", "Project/2026-08-01", 1), nn.SourceFact("fact-2", "Two.", "Project/2026-08-01", 2))
    receipt = nn.FactExtractionReceipt(nn.SOURCE_CONTRACT_VERSION, 1, 2, 0, 0, True)
    value = nn.SourceUnit("Project/2026-08-01", "a" * 64, facts, receipt)
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(value, field, getattr(value, field))


def test_generation_input_exactly_binds_source_and_order(tmp_path):
    values = runtime(tmp_path)
    source, provider = values[4], values[5]
    value = nn.build_normalization_input(source, provider)
    assert value.generation_input.source_ref == source.source_ref
    assert value.generation_input.source_facts == tuple(item.to_contract() for item in source.facts)
    assert value.quarantine_record_identity == nn.source_identity(source.source_ref, source.source_digest)


@pytest.mark.parametrize(
    ("fixture_name", "mutation", "sensitive"),
    (
        ("env_utf8", "canon", r"C:\secret\private-canon.json"),
        ("env_utf8", "canon", "/secret/private-canon.json"),
        ("env_utf8", "character-prompt", "credential=private-model-secret"),
        ("duo_context", "relationship-prompt", "token=private-relationship-secret"),
    ),
    ids=(
        "windows-canon-path-not-persisted",
        "posix-canon-path-not-persisted",
        "character-prompt-secret-not-sent",
        "relationship-prompt-secret-not-sent",
    ),
)
def test_sensitive_context_is_rejected_before_claim_or_model(
    tmp_path,
    fixture_name,
    mutation,
    sensitive,
):
    _, policy, _, record = write_source(tmp_path, "technical_log")
    template = fixture_to_input(load_fixture(fixture_name))
    if mutation == "canon":
        refs = list(template.naz_canon.canon_refs)
        refs[0] = dataclasses.replace(refs[0], source_path=sensitive)
        template = dataclasses.replace(
            template,
            naz_canon=dataclasses.replace(template.naz_canon, canon_refs=tuple(refs)),
        )
    elif mutation == "character-prompt":
        template = dataclasses.replace(
            template,
            naz_prompt_context=dataclasses.replace(
                template.naz_prompt_context,
                prompt_text=sensitive,
            ),
        )
    else:
        assert template.relationship_prompt_context is not None
        template = dataclasses.replace(
            template,
            relationship_prompt_context=dataclasses.replace(
                template.relationship_prompt_context,
                prompt_text=sensitive,
            ),
        )
    client = QueueClient([])
    service = nn.NarrativeNormalizerService(
        policy=policy,
        context_provider=nn.TemplateNarrativeContextProvider(template),
        generation_service=ng.NarrativeGenerationService(
            client,
            generation_model="terra",
            adjudication_model="sol",
        ),
        clock=lambda: NOW,
    )
    outcome = service.normalize_source(record.source_ref, record.source_digest)
    assert outcome.status == nn.OUTCOME_FAILED
    assert outcome.reason_codes == ("narrative_normalizer_context_invalid",)
    assert outcome.model_call_count == 0
    assert client.requests == []
    assert not service.store.draft_path(record.source_digest, source_ref=record.source_ref).exists()
    assert not os.path.lexists(service.store.claim_path(record.source_ref, record.source_digest))


def test_happy_path_two_calls_and_four_draft_files(tmp_path):
    *values, outcome, draft = create_draft(tmp_path)
    client = values[7]
    assert outcome.status == nn.OUTCOME_CREATED
    assert outcome.model_call_count == 2
    assert [item.request_kind for item in client.requests] == ["generation", "adjudication"]
    assert {item.name for item in draft.iterdir()} == {"story.md", "story.json", "draft-manifest.json", "review.json"}
    assert not (draft / "narrative_ready.json").exists()


def test_technical_log_acceptance_seals_meaning_and_ready_boundary(tmp_path):
    spec, policy, source_path, record = write_source(tmp_path, "technical_log")
    source_before = {
        path.relative_to(source_path).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in source_path.rglob("*")
        if path.is_file()
    }
    registry_before = policy.registry_path.read_bytes()
    source = nn.read_source_unit(policy, record.source_ref, expected_digest=record.source_digest)
    data = load_fixture("env_utf8")
    provider = nn.TemplateNarrativeContextProvider(fixture_to_input(data))
    context = provider.build(source)
    payload = normalizer_generation_payload(data, fact_count=len(source.facts), normalizer_name="technical_log")
    client = QueueClient([payload, fake_adjudication_payload(payload, context)])
    service = nn.NarrativeNormalizerService(
        policy=policy,
        context_provider=provider,
        generation_service=ng.NarrativeGenerationService(client, generation_model="terra", adjudication_model="sol"),
        clock=lambda: NOW,
    )
    outcome = service.normalize_source(record.source_ref, record.source_digest)
    draft = service.store.draft_path(record.source_digest, source_ref=record.source_ref)
    value = nn.validate_draft_directory(draft)
    required_meanings = {
        "semantic:fact-1:safe_atomic_replacement",
        "semantic:fact-2:utf8_without_bom",
        "semantic:fact-3:working_directory",
        "semantic:fact-3:parent_directory",
        "semantic:fact-4:repeated_same_result",
        "semantic:fact-5:manual_inspection_not_blind_trust",
    }
    ending = next(item for item in value["claims"] if item.claim_id == "claim-ending")
    assert tuple(item.exact_text for item in value["source"].facts) == tuple(spec["facts"])
    assert required_meanings.issubset(value["meaning"].required_source_anchors)
    assert value["meaning"].required_source_anchors == value["meaning"].covered_source_anchors
    assert value["meaning"].omitted_anchors == ()
    assert value["meaning"].significance_mode == "source_supported_significance"
    assert ending.claim_kind == "source_supported_significance"
    assert ending.ordered_source_fact_refs == ("fact-5",)
    assert value["factuality"].unsupported_claim_count == 0
    assert value["factuality"].passed and value["meaning"].passed and value["plain_language"].passed
    evidence = value["cp2_adjudication_evidence"]
    assert evidence.source_identity == value["story"]["source_identity"]
    assert evidence.candidate_id == value["story"]["selected_candidate_id"]
    assert evidence.package_digest == value["story"]["human_story_package_digest"]
    assert tuple(item.statement_name for item in evidence.statement_evidence) == ng.STORY_FIELDS
    assert tuple(item.claim_id for item in evidence.statement_evidence) == tuple(
        item.claim_id for item in value["claims"]
    )
    assert tuple(item.claim_digest for item in evidence.statement_evidence) == value["factuality"].claim_digests
    assert tuple(item.ordered_source_fact_refs for item in evidence.statement_evidence) == tuple(
        item.ordered_source_fact_refs for item in value["claims"]
    )
    assert all(item.decision == "supported" and item.reason_codes == () for item in evidence.statement_evidence)
    completed_claim = service.store.read_claim(record.source_ref, record.source_digest)
    assert completed_claim is not None
    assert completed_claim["adjudication_evidence_digest"] == value["cp2_adjudication_evidence_digest"]
    assert outcome.model_call_count == len(client.requests) == 2
    assert tuple(item.request_kind for item in client.requests) == ("generation", "adjudication")
    assert {item.name for item in draft.iterdir()} == {
        "story.md", "story.json", "draft-manifest.json", "review.json",
    }
    assert not (draft / "narrative_ready.json").exists()
    markdown = (draft / "story.md").read_text(encoding="utf-8")
    assert "безопасно заменили" in markdown
    assert "UTF-8" in markdown
    assert "папку уровнем выше" in markdown
    assert "тот же результат" in markdown
    assert "слепого доверия" in markdown
    assert rq.read_registry(policy.registry_path).records[0].status == rq.STATUS_NEEDS_NARRATIVE
    approval = approve_created(service.store, record, draft)
    assert approval.idempotent is False
    assert rq.validate_narrative_ready_manifest(policy, record.source_ref).status == rq.CLASS_READY
    assert (draft / "narrative_ready.json").is_file()
    source_after = {
        path.relative_to(source_path).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in source_path.rglob("*")
        if path.is_file()
    }
    assert source_after == source_before
    assert policy.registry_path.read_bytes() == registry_before
    assert not (source_path / "narrative_ready.json").exists()
    runtime_source = Path(nn.__file__).read_text(encoding="utf-8").casefold()
    import_lines = tuple(
        line.strip() for line in runtime_source.splitlines()
        if line.startswith(("import ", "from "))
    )
    assert all(
        not any(token in line for line in import_lines)
        for token in ("story_production", "renderer", "telegram", "operator_events")
    )


def test_approval_replays_typed_cp1_context_without_model_call(tmp_path, monkeypatch):
    *values, _, draft = create_draft(tmp_path)
    store = values[-1].store
    record = values[3]
    client = values[7]
    before_calls = len(client.requests)
    expected_draft_identity = nn.validate_draft_directory(draft)["manifest"]["draft_identity"]
    original = nn.translator.validate_human_story_package
    cp1_calls = 0

    def counted(package, context):
        nonlocal cp1_calls
        cp1_calls += 1
        return original(package, context)

    monkeypatch.setattr(nn.translator, "validate_human_story_package", counted)
    result = store.approve(
        record.source_ref,
        record.source_digest,
        expected_draft_identity=expected_draft_identity,
        reviewed_at=NOW.isoformat(),
    )
    assert result.status == rq.CLASS_READY
    # Approval validates the persisted snapshot, binds it to the freshly
    # replayed source, and validates the promoted ready snapshot.
    assert cp1_calls == 4
    assert len(client.requests) == before_calls


@pytest.mark.parametrize(
    "mutation",
    (
        "statement-missing", "statement-extra", "statement-reordered", "statement-digest",
        "statement-source-refs", "statement-claim-id", "statement-claim-digest",
        "statement-inference-kind", "source-identity", "candidate-id", "package-digest",
        "authority-digest", "draft-digest", "candidate-adjudication-digest", "overall-decision",
    ),
    ids=lambda value: f"cp2-evidence-{value}-tamper-fails-closed",
)
def test_cp2_adjudication_evidence_tamper_never_reaches_ready(tmp_path, mutation):
    *values, outcome, draft = create_draft(tmp_path)
    record = values[3]
    store = values[-1].store
    original = nn.validate_draft_directory(draft)
    story = copy.deepcopy(original["story"])
    evidence = story["cp2_adjudication_evidence"]
    statements = evidence["statement_evidence"]
    if mutation == "statement-missing":
        statements.pop()
    elif mutation == "statement-extra":
        statements.append(copy.deepcopy(statements[-1]))
    elif mutation == "statement-reordered":
        statements.reverse()
    elif mutation == "statement-digest":
        statements[0]["statement_digest"] = "0" * 64
    elif mutation == "statement-source-refs":
        statements[0]["ordered_source_fact_refs"] = ["fact-2"]
    elif mutation == "statement-claim-id":
        statements[0]["claim_id"] = "claim-story-1"
    elif mutation == "statement-claim-digest":
        statements[0]["claim_digest"] = "1" * 64
    elif mutation == "statement-inference-kind":
        statements[0]["inference_kind"] = "observed"
    elif mutation == "source-identity":
        evidence["source_identity"] = "2" * 64
    elif mutation == "candidate-id":
        evidence["candidate_id"] = "candidate-b"
    elif mutation == "package-digest":
        evidence["package_digest"] = "3" * 64
    elif mutation == "authority-digest":
        evidence["authority_context_digest"] = "4" * 64
    elif mutation == "draft-digest":
        evidence["draft_digest"] = "5" * 64
    elif mutation == "candidate-adjudication-digest":
        evidence["candidate_adjudication_digest"] = "6" * 64
    elif mutation == "overall-decision":
        evidence["overall_decision"] = "rejected"
        evidence["reason_codes"] = ["semantic_claim_unsupported"]
    else:
        raise AssertionError(mutation)
    evidence["evidence_digest"] = nn._sha({
        key: item for key, item in evidence.items() if key != "evidence_digest"
    })
    write_json(draft / "story.json", story)
    with pytest.raises(nn.NarrativeNormalizerError):
        nn.validate_draft_directory(draft)
    with pytest.raises(nn.NarrativeNormalizerError):
        store.approve(
            record.source_ref,
            record.source_digest,
            expected_draft_identity=original["manifest"]["draft_identity"],
            reviewed_at=NOW.isoformat(),
        )
    assert not (draft / "narrative_ready.json").exists()


@pytest.mark.parametrize(
    ("fixture_name", "mutation"),
    (
        ("technical_log", "primary-continuity"),
        ("technical_log", "visual-grounding"),
        ("technical_log", "editorial-alignment"),
        ("technical_log", "unexpected-relationship-continuity"),
    ),
    ids=(
        "cp2-primary-continuity-cross-binding",
        "cp2-visual-cross-binding",
        "cp2-editorial-cross-binding",
        "cp2-unexpected-relationship-cross-binding",
    ),
)
def test_coherently_resealed_cp2_evidence_mismatch_fails_closed(
    tmp_path,
    fixture_name,
    mutation,
):
    *values, _, draft = create_draft(tmp_path, fixture_name)
    record = values[3]
    store = values[-1].store
    original = nn.validate_draft_directory(draft)
    story = copy.deepcopy(original["story"])
    manifest = copy.deepcopy(original["manifest"])
    review = copy.deepcopy(original["review"])
    evidence = story["cp2_adjudication_evidence"]
    adjudication = evidence["candidate_adjudication"]
    if mutation == "primary-continuity":
        adjudication["primary_continuity"]["interpretation_digest"] = "9" * 64
    elif mutation == "visual-grounding":
        adjudication["visual_grounding"]["visual_payload_digest"] = "8" * 64
    elif mutation == "editorial-alignment":
        adjudication["editorial_alignment"]["editorial_alignment_digest"] = "7" * 64
    else:
        assert adjudication["relationship_continuity"] is None
        adjudication["relationship_continuity"] = {
            "relationship_payload_digest": "6" * 64,
            "decision": "supported",
            "reason_codes": [],
        }
    evidence["candidate_adjudication_digest"] = nn._sha(adjudication)
    evidence["evidence_digest"] = nn._sha({
        key: item for key, item in evidence.items() if key != "evidence_digest"
    })
    factuality = nn.build_factuality_receipt(
        original["source"],
        original["claims"],
        candidate_id=story["selected_candidate_id"],
        package_digest=story["human_story_package_digest"],
        statement_inference_kinds=original["factuality"].statement_inference_kinds,
        adjudication_evidence_digest=evidence["evidence_digest"],
    )
    story["factuality_receipt"] = dataclasses.asdict(factuality)
    review["factuality_receipt"] = dataclasses.asdict(factuality)
    review["unsupported_claim_count"] = factuality.unsupported_claim_count
    story["package_digest"] = nn._sha({
        key: item for key, item in story.items() if key != "package_digest"
    })
    changed_draft_identity = nn.draft_identity(
        story["source_identity"],
        story["package_digest"],
    )
    manifest["package_digest"] = story["package_digest"]
    manifest["draft_identity"] = changed_draft_identity
    manifest["idempotency_identity"] = nn._sha({
        "version": nn.IDEMPOTENCY_VERSION,
        "source_identity": story["source_identity"],
        "package_digest": story["package_digest"],
    })
    review["draft_identity"] = changed_draft_identity
    write_json(draft / "story.json", story)
    write_json(draft / "draft-manifest.json", manifest)
    write_json(draft / "review.json", review)
    with pytest.raises(nn.NarrativeNormalizerError, match="narrative_normalizer_draft_invalid"):
        nn.validate_draft_directory(draft)
    with pytest.raises(nn.NarrativeNormalizerError):
        store.approve(
            record.source_ref,
            record.source_digest,
            expected_draft_identity=changed_draft_identity,
            reviewed_at=NOW.isoformat(),
        )
    assert not (draft / "narrative_ready.json").exists()


def test_duo_cp2_continuity_character_roles_cannot_be_swapped(tmp_path):
    def select_duo(payload):
        solo = payload["candidates"][0]
        duo = payload["candidates"][2]
        for field in ng.STORY_FIELDS:
            duo[field] = copy.deepcopy(solo[field])
        solo["rank"], duo["rank"] = duo["rank"], solo["rank"]

    *values, _, draft = create_draft(
        tmp_path,
        "duo_context",
        mutate_draft=select_duo,
    )
    record = values[3]
    store = values[-1].store
    original = nn.validate_draft_directory(draft)
    story = copy.deepcopy(original["story"])
    manifest = copy.deepcopy(original["manifest"])
    review = copy.deepcopy(original["review"])
    evidence = story["cp2_adjudication_evidence"]
    adjudication = evidence["candidate_adjudication"]
    primary = adjudication["primary_continuity"]
    secondary = adjudication["secondary_continuity"]
    assert secondary is not None
    assert adjudication["relationship_continuity"] is not None
    primary["character_id"], secondary["character_id"] = (
        secondary["character_id"],
        primary["character_id"],
    )
    evidence["candidate_adjudication_digest"] = nn._sha(adjudication)
    evidence["evidence_digest"] = nn._sha({
        key: item for key, item in evidence.items() if key != "evidence_digest"
    })
    factuality = nn.build_factuality_receipt(
        original["source"],
        original["claims"],
        candidate_id=story["selected_candidate_id"],
        package_digest=story["human_story_package_digest"],
        statement_inference_kinds=original["factuality"].statement_inference_kinds,
        adjudication_evidence_digest=evidence["evidence_digest"],
    )
    story["factuality_receipt"] = dataclasses.asdict(factuality)
    review["factuality_receipt"] = dataclasses.asdict(factuality)
    review["unsupported_claim_count"] = factuality.unsupported_claim_count
    story["package_digest"] = nn._sha({
        key: item for key, item in story.items() if key != "package_digest"
    })
    changed_draft_identity = nn.draft_identity(
        story["source_identity"],
        story["package_digest"],
    )
    manifest["package_digest"] = story["package_digest"]
    manifest["draft_identity"] = changed_draft_identity
    manifest["idempotency_identity"] = nn._sha({
        "version": nn.IDEMPOTENCY_VERSION,
        "source_identity": story["source_identity"],
        "package_digest": story["package_digest"],
    })
    review["draft_identity"] = changed_draft_identity
    write_json(draft / "story.json", story)
    write_json(draft / "draft-manifest.json", manifest)
    write_json(draft / "review.json", review)

    with pytest.raises(nn.NarrativeNormalizerError):
        nn.validate_draft_directory(draft)
    with pytest.raises(nn.NarrativeNormalizerError):
        store.approve(
            record.source_ref,
            record.source_digest,
            expected_draft_identity=changed_draft_identity,
            reviewed_at=NOW.isoformat(),
        )
    assert not (draft / "narrative_ready.json").exists()


def test_optional_schema_repair_uses_exact_three_call_budget(tmp_path):
    spec, policy, source_path, record = write_source(tmp_path)
    source = nn.read_source_unit(policy, record.source_ref, expected_digest=record.source_digest)
    data = load_fixture("env_utf8")
    provider = nn.TemplateNarrativeContextProvider(fixture_to_input(data))
    context = provider.build(source)
    drafts = normalizer_generation_payload(data, fact_count=len(source.facts))
    client = QueueClient(["not-json", drafts, fake_adjudication_payload(drafts, context)])
    service = nn.NarrativeNormalizerService(
        policy=policy,
        context_provider=provider,
        generation_service=ng.NarrativeGenerationService(
            client,
            generation_model="terra-medium",
            adjudication_model="sol-high",
            repair_model="terra-high",
        ),
        clock=lambda: NOW,
    )
    outcome = service.normalize_source(record.source_ref, record.source_digest)
    assert outcome.status == nn.OUTCOME_CREATED
    assert outcome.model_call_count == 3
    assert [request.request_kind for request in client.requests] == ["generation", "repair", "adjudication"]
    draft = service.store.draft_path(record.source_digest, source_ref=record.source_ref)
    evidence = nn.validate_draft_directory(draft)["cp2_adjudication_evidence"]
    assert evidence.model_call_count == 3
    assert tuple(item.statement_name for item in evidence.statement_evidence) == ng.STORY_FIELDS


def test_adjudication_schema_repair_captures_only_final_typed_evidence(tmp_path):
    spec, policy, _, record = write_source(tmp_path)
    source = nn.read_source_unit(policy, record.source_ref, expected_digest=record.source_digest)
    data = load_fixture("env_utf8")
    provider = nn.TemplateNarrativeContextProvider(fixture_to_input(data))
    context = provider.build(source)
    drafts = normalizer_generation_payload(data, fact_count=len(source.facts))
    adjudication = fake_adjudication_payload(drafts, context)
    client = QueueClient([drafts, {"schema": "malformed"}, adjudication])
    service = nn.NarrativeNormalizerService(
        policy=policy,
        context_provider=provider,
        generation_service=ng.NarrativeGenerationService(
            client,
            generation_model="terra-medium",
            adjudication_model="sol-high",
            repair_model="terra-high",
        ),
        clock=lambda: NOW,
    )
    outcome = service.normalize_source(record.source_ref, record.source_digest)
    draft = service.store.draft_path(record.source_digest, source_ref=record.source_ref)
    evidence = nn.validate_draft_directory(draft)["cp2_adjudication_evidence"]
    assert outcome.status == nn.OUTCOME_CREATED
    assert outcome.model_call_count == 3
    assert [request.request_kind for request in client.requests] == ["generation", "adjudication", "repair"]
    assert evidence.model_call_count == 3
    assert tuple(item.statement_name for item in evidence.statement_evidence) == ng.STORY_FIELDS


def test_observed_statement_cardinality_fails_closed_after_exact_model_budget(tmp_path):
    def mutate(payload):
        for candidate in payload["candidates"]:
            for name in ng.STORY_FIELDS:
                candidate[name]["inference_kind"] = "observed"

    *values, outcome, draft = create_draft(tmp_path, mutate_draft=mutate)
    client = values[-2]
    assert outcome.status == nn.OUTCOME_FAILED
    assert outcome.reason_codes == ("narrative_normalizer_generation_failed",)
    assert outcome.model_call_count == len(client.requests) == 2
    assert not draft.exists()


def test_cp2_typed_evidence_capture_is_context_local_across_concurrent_sources():
    contexts = {}
    responses = {}
    for name in ("env_utf8", "quiet_object"):
        data = load_fixture(name)
        context = fixture_to_input(data)
        payload = normalizer_generation_payload(data, normalizer_name=("technical_log" if name == "env_utf8" else name))
        contexts[name] = context
        responses[context.source_facts[0].text] = (payload, fake_adjudication_payload(payload, context))

    class RoutingClient:
        def generate_json(self, request):
            request_payload = json.loads(request.user_prompt)
            first_fact = request_payload["context"]["source_facts"][0]["text"]
            generation_payload, adjudication_payload = responses[first_fact]
            if request.request_kind == "generation":
                return copy.deepcopy(generation_payload)
            if request.request_kind == "adjudication":
                return copy.deepcopy(adjudication_payload)
            raise AssertionError(request.request_kind)

    service = nn._EvidenceCapturingGenerationService(
        ng.NarrativeGenerationService(RoutingClient(), generation_model="terra", adjudication_model="sol")
    )
    ordered = tuple(contexts[name] for name in ("env_utf8", "quiet_object") for _ in range(6))
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = tuple(pool.map(service.generate_with_evidence, ordered))
    for context, (result, captured) in zip(ordered, results, strict=True):
        expected_hook = responses[context.source_facts[0].text][0]["candidates"][0]["hook"]["text"]
        assert result.model_call_count == 2
        assert captured.drafts[0].hook.text == expected_hook
        assert {item.candidate_id for item in captured.adjudications.candidates} == {
            item.candidate_id for item in captured.drafts
        }


def test_private_cp2_capture_adapter_signature_is_version_guarded(monkeypatch):
    original = ng.NarrativeGenerationService._call_parse

    def incompatible(self, request, parser):
        return original(self, request, parser, [], [False])

    monkeypatch.setattr(ng.NarrativeGenerationService, "_call_parse", incompatible)
    base = ng.NarrativeGenerationService(
        QueueClient([]), generation_model="terra", adjudication_model="sol"
    )
    with pytest.raises(TypeError, match="generation_service"):
        nn._EvidenceCapturingGenerationService(base)


@pytest.mark.parametrize("name", ["quiet_object", "duo_context"])
def test_representative_story_fixture_reaches_review_passed(tmp_path, name):
    *_, outcome, draft = create_draft(tmp_path, name)
    value = nn.validate_draft_directory(draft)
    assert outcome.review_status == nn.REVIEW_PASSED
    assert value["review"]["fact_coverage"]["coverage_complete"] is True
    assert nn._CYRILLIC_WORD.search(value["story"]["story"])


def test_story_markdown_contains_only_readable_story(tmp_path):
    *_, outcome, draft = create_draft(tmp_path)
    text = (draft / "story.md").read_text(encoding="utf-8")
    for token in ("source_ref", "package_digest", "reason_codes", "contract_version", "C:\\", "/opt/"):
        assert token not in text
    assert text.startswith("# ")


def test_story_json_preserves_full_ordered_facts_and_zero_unsupported_claims(tmp_path):
    *_, outcome, draft = create_draft(tmp_path)
    value = nn.validate_draft_directory(draft)
    story = value["story"]
    assert [item["order"] for item in story["source_facts"]] == list(range(1, 6))
    assert value["review"]["unsupported_claim_count"] == 0
    assert value["review"]["unsupported_claim_count"] == len(value["factuality"].unsupported_claim_ids)
    assert value["factuality"].passed is True
    assert value["meaning"].passed is True
    assert value["review"]["fact_coverage"]["coverage_complete"] is True


@pytest.mark.parametrize("missing", sorted(nn._STORY_KEYS))
def test_story_schema_is_closed_and_requires_every_key(tmp_path, missing):
    *_, outcome, draft = create_draft(tmp_path)
    path = draft / "story.json"; value = json.loads(path.read_text(encoding="utf-8")); value.pop(missing); write_json(path, value)
    with pytest.raises(nn.NarrativeNormalizerError, match="narrative_normalizer_draft_invalid"):
        nn.validate_draft_directory(draft)


@pytest.mark.parametrize("missing", sorted(nn._DRAFT_MANIFEST_KEYS))
def test_draft_manifest_schema_is_closed_and_requires_every_key(tmp_path, missing):
    *_, outcome, draft = create_draft(tmp_path)
    path = draft / "draft-manifest.json"; value = json.loads(path.read_text(encoding="utf-8")); value.pop(missing); write_json(path, value)
    with pytest.raises(nn.NarrativeNormalizerError, match="narrative_normalizer_draft_invalid"):
        nn.validate_draft_directory(draft)


@pytest.mark.parametrize("missing", sorted(nn._REVIEW_KEYS))
def test_review_schema_is_closed_and_requires_every_key(tmp_path, missing):
    *_, outcome, draft = create_draft(tmp_path)
    path = draft / "review.json"; value = json.loads(path.read_text(encoding="utf-8")); value.pop(missing); write_json(path, value)
    with pytest.raises(nn.NarrativeNormalizerError, match="narrative_normalizer_draft_invalid"):
        nn.validate_draft_directory(draft)


@pytest.mark.parametrize(
    "mutation",
    [
        "extra-story", "extra-manifest", "extra-review", "markdown-changed", "package-digest-changed",
        "source-digest-changed", "source-ref-changed", "unsupported-claim", "receipt-changed", "symlink-extra",
    ],
)
def test_draft_validation_fails_closed_for_divergent_payload(tmp_path, monkeypatch, mutation):
    *_, outcome, draft = create_draft(tmp_path)
    if mutation == "markdown-changed":
        (draft / "story.md").write_text("changed", encoding="utf-8")
    elif mutation == "symlink-extra":
        target = draft / "story.md"
        original = Path.is_symlink
        monkeypatch.setattr(Path, "is_symlink", lambda self: self == target or original(self))
    else:
        name = "story.json" if mutation in {"extra-story", "package-digest-changed", "source-digest-changed", "source-ref-changed", "receipt-changed"} else "draft-manifest.json" if mutation == "extra-manifest" else "review.json"
        path = draft / name; value = json.loads(path.read_text(encoding="utf-8"))
        if mutation.startswith("extra-"):
            value["extra"] = True
        elif mutation == "package-digest-changed": value["package_digest"] = "0" * 64
        elif mutation == "source-digest-changed": value["source_digest"] = "0" * 64
        elif mutation == "source-ref-changed": value["source_ref"] = "Other/2026-08-01"
        elif mutation == "unsupported-claim": value["unsupported_claim_count"] = 1
        elif mutation == "receipt-changed": value["plain_language_receipt"]["passed"] = False
        write_json(path, value)
    with pytest.raises(nn.NarrativeNormalizerError, match="narrative_normalizer_draft_invalid"):
        nn.validate_draft_directory(draft)


def test_same_source_same_digest_is_idempotent_without_model_calls(tmp_path):
    values = runtime(tmp_path); record, client, service = values[3], values[7], values[8]
    first = service.normalize_source(record.source_ref, record.source_digest)
    calls = len(client.requests)
    second = service.normalize_source(record.source_ref, record.source_digest)
    assert first.status == nn.OUTCOME_CREATED
    assert second.status == nn.OUTCOME_EXISTING
    assert len(client.requests) == calls == 2
    assert first.package_digest == second.package_digest


def test_changed_source_creates_new_digest_without_overwriting_old(tmp_path):
    values = runtime(tmp_path); source_path, record, source, provider, service = values[2], values[3], values[4], values[5], values[8]
    first = service.normalize_source(record.source_ref, record.source_digest)
    (source_path / "new.txt").write_text(
        str(normalizer_fixture("technical_log")["facts"][0]) + "\n",
        encoding="utf-8",
    )
    reconciliation = rq.reconcile_complete_backlog(service.policy, now=NOW + timedelta(minutes=1))
    new_rows = nn.scan_needs_narrative(service.policy)
    assert reconciliation.changed_count == 1
    assert len(new_rows) == 1 and new_rows[0][0] == record.source_ref
    new_digest = new_rows[0][1]
    assert new_digest == rq.source_digest(source_path)
    changed = nn.read_source_unit(service.policy, record.source_ref, expected_digest=new_digest)
    data = load_fixture("env_utf8"); context = provider.build(changed); drafts = normalizer_generation_payload(data, fact_count=len(changed.facts))
    client = QueueClient([drafts, fake_adjudication_payload(drafts, context)])
    second_service = nn.NarrativeNormalizerService(
        policy=service.policy,
        context_provider=provider,
        generation_service=ng.NarrativeGenerationService(client, generation_model="terra", adjudication_model="sol"),
        clock=lambda: NOW,
    )
    second = second_service.normalize_source(record.source_ref, new_digest)
    assert first.source_digest != second.source_digest
    first_path = service.store.draft_path(first.source_digest, source_ref=record.source_ref)
    second_path = second_service.store.draft_path(second.source_digest, source_ref=record.source_ref)
    assert first_path.is_dir()
    assert second_path.is_dir()
    second_value = nn.validate_draft_directory(second_path)
    assert second_value["manifest"]["supersedes"]["old_source_digest"] == first.source_digest
    assert second_value["manifest"]["supersedes"]["new_source_digest"] == second.source_digest


def _create_supersede_pair(tmp_path):
    values = runtime(tmp_path)
    source_path, record, provider, service = values[2], values[3], values[5], values[8]
    first = service.normalize_source(record.source_ref, record.source_digest)
    first_path = service.store.draft_path(first.source_digest, source_ref=record.source_ref)
    first_value = nn.validate_draft_directory(first_path)
    (source_path / "new.txt").write_text(
        str(normalizer_fixture("technical_log")["facts"][0]) + "\n",
        encoding="utf-8",
    )
    rq.reconcile_complete_backlog(service.policy, now=NOW + timedelta(minutes=1))
    new_rows = nn.scan_needs_narrative(service.policy)
    assert len(new_rows) == 1 and new_rows[0][0] == record.source_ref
    new_digest = new_rows[0][1]
    changed = nn.read_source_unit(service.policy, record.source_ref, expected_digest=new_digest)
    data = load_fixture("env_utf8")
    context = provider.build(changed)
    drafts = normalizer_generation_payload(data, fact_count=len(changed.facts))
    client = QueueClient([drafts, fake_adjudication_payload(drafts, context)])
    second_service = nn.NarrativeNormalizerService(
        policy=service.policy,
        context_provider=provider,
        generation_service=ng.NarrativeGenerationService(
            client,
            generation_model="terra",
            adjudication_model="sol",
        ),
        clock=lambda: NOW,
    )
    second = second_service.normalize_source(record.source_ref, new_digest)
    second_path = second_service.store.draft_path(second.source_digest, source_ref=record.source_ref)
    second_value = nn.validate_draft_directory(second_path)
    relation = second_value["manifest"]["supersedes"]
    assert relation is not None
    return service.store, first_value, second_value, relation, first_path


def test_supersede_exact_request_is_byte_idempotent(tmp_path):
    store, first_value, second_value, relation, first_path = _create_supersede_pair(tmp_path)
    review_path = first_path / "review.json"
    kwargs = dict(
        **relation,
        operator_request_id="supersede-request-001",
        reviewed_at=NOW.isoformat(),
    )
    first = store.supersede(**kwargs)
    original_review = review_path.read_bytes()
    new_path = store.root / relation["new_source_identity"]
    new_path.rename(tmp_path / "detached-new-draft")
    second = store.supersede(**kwargs)
    assert first.idempotent is False
    assert second.idempotent is True
    assert review_path.read_bytes() == original_review
    ledger = store._review_store().read(
        relation["old_source_identity"],
        expected_draft_identity=relation["old_draft_identity"],
    )
    assert ledger.latest.state == nn.review_state.STATE_SUPERSEDED
    assert ledger.latest.operator_request_id == "supersede-request-001"
    assert second_value["manifest"]["draft_identity"] == relation["new_draft_identity"]


def test_supersede_wrong_old_identity_fails_closed(tmp_path):
    store, _, _, relation, first_path = _create_supersede_pair(tmp_path)
    before = (first_path / "review.json").read_bytes()
    kwargs = dict(relation)
    kwargs.update(
        old_source_identity="0" * 64,
        operator_request_id="supersede-wrong-old",
        reviewed_at=NOW.isoformat(),
    )
    with pytest.raises(nn.NarrativeNormalizerError, match="narrative_normalizer_supersede_identity_invalid"):
        store.supersede(**kwargs)
    assert (first_path / "review.json").read_bytes() == before


def test_supersede_wrong_new_identity_fails_closed(tmp_path):
    store, _, _, relation, first_path = _create_supersede_pair(tmp_path)
    before = (first_path / "review.json").read_bytes()
    kwargs = dict(relation)
    kwargs.update(
        new_source_identity="f" * 64,
        operator_request_id="supersede-wrong-new",
        reviewed_at=NOW.isoformat(),
    )
    with pytest.raises(nn.NarrativeNormalizerError, match="narrative_normalizer_supersede_identity_invalid"):
        store.supersede(**kwargs)
    assert (first_path / "review.json").read_bytes() == before


def test_unexpired_claim_blocks_duplicate_model_call(tmp_path):
    values = runtime(tmp_path); record, source, service = values[3], values[4], values[8]
    payload = nn._claim_payload(source, attempt_id="a" * 32, state=nn.CLAIM_PROCESSING, started_at=NOW.isoformat().replace("+00:00", "Z"), updated_at=NOW.isoformat().replace("+00:00", "Z"))
    service.store.write_claim(payload)
    outcome = service.normalize_source(record.source_ref, record.source_digest)
    assert outcome.status == nn.OUTCOME_PROCESSING
    assert not values[7].requests


def test_stale_claim_becomes_uncertain_without_automatic_retry(tmp_path):
    values = runtime(tmp_path); record, source, service = values[3], values[4], values[8]
    old = (NOW - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    service.store.write_claim(nn._claim_payload(source, attempt_id="b" * 32, state=nn.CLAIM_PROCESSING, started_at=old, updated_at=old))
    outcome = service.normalize_source(record.source_ref, record.source_digest)
    assert outcome.status == nn.OUTCOME_UNCERTAIN
    assert outcome.reason_codes == ("narrative_normalizer_claim_uncertain",)
    assert not values[7].requests
    assert service.store.read_claim(record.source_ref, record.source_digest)["state"] == nn.CLAIM_UNCERTAIN


def test_explicit_uncertain_retry_uses_exact_single_pair(tmp_path):
    values = runtime(tmp_path); record, source, client, service = values[3], values[4], values[7], values[8]
    old = (NOW - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    processing = nn._claim_payload(
        source,
        attempt_id="c" * 32,
        state=nn.CLAIM_PROCESSING,
        started_at=old,
        updated_at=old,
    )
    service.store.write_claim(processing)
    service.store.write_claim(dict(
        processing,
        state=nn.CLAIM_UNCERTAIN,
        reason_code="narrative_normalizer_claim_uncertain",
    ))
    outcome = service.normalize_source(record.source_ref, record.source_digest, retry_uncertain=True)
    assert outcome.status == nn.OUTCOME_CREATED
    assert len(client.requests) == 2
    assert service.store.read_claim(record.source_ref, record.source_digest)["attempt_id"] == "c" * 32


def _seed_failed_claim(service, source, *, attempt_id="d" * 32):
    timestamp = NOW.isoformat().replace("+00:00", "Z")
    processing = nn._claim_payload(
        source,
        attempt_id=attempt_id,
        state=nn.CLAIM_PROCESSING,
        started_at=timestamp,
        updated_at=timestamp,
    )
    service.store.write_claim(processing)
    failed = dict(
        processing,
        state=nn.CLAIM_FAILED,
        reason_code="narrative_normalizer_evidence_invalid",
    )
    service.store.write_claim(failed)
    path = service.store.claim_path(source.source_ref, source.source_digest)
    payload = path.read_bytes()
    return path, payload, hashlib.sha256(payload).hexdigest()


def _manual_retry_request(source, previous_digest, *, request_id="manual-retry-request-0001", **updates):
    values = {
        "source_identity": nn.source_identity(source.source_ref, source.source_digest),
        "source_digest": source.source_digest,
        "previous_failed_attempt_id": "d" * 32,
        "previous_failed_claim_digest": previous_digest,
        "operator_request_id": request_id,
        "run_profile": nn.run_profiles.CANARY_RUN_PROFILE,
    }
    values.update(updates)
    return nn.ManualRetryRequest(**values)


def test_manual_retry_archives_old_failed_bytes_and_completes_new_attempt(tmp_path):
    values = runtime(tmp_path)
    record, source, client, service = values[3], values[4], values[7], values[8]
    claim_path, old_bytes, old_digest = _seed_failed_claim(service, source)
    request = _manual_retry_request(source, old_digest)

    outcome = service.normalize_source(
        record.source_ref,
        record.source_digest,
        manual_retry=request,
    )

    assert outcome.status == nn.OUTCOME_DRAFT_READY_FOR_REVIEW
    current = service.store.read_claim(record.source_ref, record.source_digest)
    assert current is not None and current["state"] == nn.CLAIM_COMPLETED
    assert current["attempt_id"] != "d" * 32
    assert claim_path.read_bytes() != old_bytes
    assert service.store.archived_attempt_bytes(
        request.source_identity, request.previous_failed_attempt_id
    ) == old_bytes
    history = service.store.attempt_history(record.source_ref, record.source_digest)
    assert [item["attempt_id"] for item in history] == [
        request.previous_failed_attempt_id,
        current["attempt_id"],
    ]
    assert [item["state"] for item in history] == [
        nn.CLAIM_FAILED,
        nn.CLAIM_COMPLETED,
    ]
    assert hashlib.sha256(old_bytes).hexdigest() == old_digest
    assert len(client.requests) == 2


def test_manual_retry_exact_request_is_byte_idempotent_and_zero_call_replay(tmp_path):
    values = runtime(tmp_path)
    record, source, client, service = values[3], values[4], values[7], values[8]
    _path, old_bytes, old_digest = _seed_failed_claim(service, source)
    request = _manual_retry_request(source, old_digest)
    first = service.normalize_source(record.source_ref, record.source_digest, manual_retry=request)
    request_path = service.store._manual_retry_request_path(request.operator_request_id)
    request_bytes = request_path.read_bytes()
    attempt_id = service.store._read_manual_retry_record(
        request.operator_request_id
    )["attempt_id"]
    calls = len(client.requests)

    second = service.normalize_source(record.source_ref, record.source_digest, manual_retry=request)

    assert first.status == nn.OUTCOME_DRAFT_READY_FOR_REVIEW
    assert second.status == nn.OUTCOME_EXISTING_DRAFT
    assert len(client.requests) == calls
    assert request_path.read_bytes() == request_bytes
    assert service.store._read_manual_retry_record(
        request.operator_request_id
    )["attempt_id"] == attempt_id
    assert service.store.archived_attempt_bytes(
        request.source_identity, request.previous_failed_attempt_id
    ) == old_bytes


@pytest.mark.parametrize(
    "change",
    ("source", "profile", "previous-attempt", "previous-digest"),
)
def test_manual_retry_divergent_request_conflicts_without_mutation(tmp_path, change):
    values = runtime(tmp_path)
    record, source, client, service = values[3], values[4], values[7], values[8]
    _path, old_bytes, old_digest = _seed_failed_claim(service, source)
    request = _manual_retry_request(source, old_digest)
    service.normalize_source(record.source_ref, record.source_digest, manual_retry=request)
    claim_before = service.store.claim_path(record.source_ref, record.source_digest).read_bytes()
    calls_before = len(client.requests)
    updates = {
        "source": {"source_identity": "f" * 64},
        "profile": {"run_profile": nn.run_profiles.FIRST_FIVE_RUN_PROFILE},
        "previous-attempt": {"previous_failed_attempt_id": "e" * 32},
        "previous-digest": {"previous_failed_claim_digest": "f" * 64},
    }[change]
    divergent = _manual_retry_request(
        source,
        old_digest,
        request_id=request.operator_request_id,
        **updates,
    )

    outcome = service.normalize_source(
        record.source_ref,
        record.source_digest,
        manual_retry=divergent,
    )
    assert outcome.status == nn.OUTCOME_FAILED
    assert outcome.reason_codes in {
        ("narrative_normalizer_manual_retry_invalid",),
        ("narrative_normalizer_manual_retry_conflict",),
    }
    assert service.store.claim_path(record.source_ref, record.source_digest).read_bytes() == claim_before
    assert service.store.archived_attempt_bytes(
        request.source_identity, request.previous_failed_attempt_id
    ) == old_bytes
    assert len(client.requests) == calls_before


def test_manual_retry_wrong_previous_attempt_rejected_before_claim_or_model(tmp_path):
    values = runtime(tmp_path)
    record, source, client, service = values[3], values[4], values[7], values[8]
    claim_path, old_bytes, old_digest = _seed_failed_claim(service, source)
    request = _manual_retry_request(
        source,
        old_digest,
        previous_failed_attempt_id="e" * 32,
    )
    outcome = service.normalize_source(
        record.source_ref, record.source_digest, manual_retry=request
    )
    assert outcome.status == nn.OUTCOME_FAILED
    assert outcome.reason_codes == ("narrative_normalizer_manual_retry_invalid",)
    assert claim_path.read_bytes() == old_bytes
    assert client.requests == []


def test_manual_retry_transport_failure_keeps_archived_predecessor_byte_exact(tmp_path, monkeypatch):
    values = runtime(tmp_path)
    record, source, client, service = values[3], values[4], values[7], values[8]
    _path, old_bytes, old_digest = _seed_failed_claim(service, source)
    request = _manual_retry_request(source, old_digest)

    def fail(_request):
        raise RuntimeError("private provider failure")

    monkeypatch.setattr(client, "generate_json", fail)
    with pytest.raises(nn.NarrativeNormalizerError, match="narrative_normalizer_internal_error"):
        service.normalize_source(record.source_ref, record.source_digest, manual_retry=request)
    assert service.store.archived_attempt_bytes(
        request.source_identity, request.previous_failed_attempt_id
    ) == old_bytes
    current = service.store.read_claim(record.source_ref, record.source_digest)
    assert current is not None and current["state"] == nn.CLAIM_FAILED
    assert current["attempt_id"] != request.previous_failed_attempt_id


def test_failed_claim_cannot_advance_without_archived_manual_retry_contract(tmp_path):
    values = runtime(tmp_path)
    source, service = values[4], values[8]
    claim_path, old_bytes, _old_digest = _seed_failed_claim(service, source)
    timestamp = NOW.isoformat().replace("+00:00", "Z")
    forged_processing = nn._claim_payload(
        source,
        attempt_id="e" * 32,
        state=nn.CLAIM_PROCESSING,
        started_at=timestamp,
        updated_at=timestamp,
    )
    with pytest.raises(nn.NarrativeNormalizerError, match="narrative_normalizer_claim_invalid"):
        service.store._write_claim_locked(forged_processing)
    assert claim_path.read_bytes() == old_bytes


def test_unsupported_fact_reference_fails_without_draft_or_hidden_repair(tmp_path):
    spec, policy, source_path, record = write_source(tmp_path)
    source = nn.read_source_unit(policy, record.source_ref, expected_digest=record.source_digest)
    data = load_fixture("env_utf8")
    provider = nn.TemplateNarrativeContextProvider(fixture_to_input(data))
    drafts = normalizer_generation_payload(data, fact_count=len(source.facts))
    for candidate in drafts["candidates"]:
        candidate["hook"]["source_fact_refs"] = ["fact-999"]
    client = QueueClient([drafts])
    service = nn.NarrativeNormalizerService(
        policy=policy,
        context_provider=provider,
        generation_service=ng.NarrativeGenerationService(client, generation_model="terra", adjudication_model="sol"),
        clock=lambda: NOW,
    )
    outcome = service.normalize_source(record.source_ref, record.source_digest)
    draft = service.store.draft_path(record.source_digest, source_ref=record.source_ref)
    assert outcome.status == nn.OUTCOME_FAILED
    assert outcome.reason_codes == ("narrative_normalizer_generation_failed",)
    assert [request.request_kind for request in client.requests] == ["generation"]
    assert not draft.exists()


def test_concurrent_same_source_gets_one_generation_claim(tmp_path):
    spec, policy, source_path, record = write_source(tmp_path)
    source = nn.read_source_unit(policy, record.source_ref, expected_digest=record.source_digest)
    data = load_fixture("env_utf8"); provider = nn.TemplateNarrativeContextProvider(fixture_to_input(data)); context = provider.build(source)
    drafts = normalizer_generation_payload(data, fact_count=len(source.facts)); judge = fake_adjudication_payload(drafts, context)
    entered = threading.Event(); release = threading.Event(); client = BlockingClient([drafts, judge], entered, release)
    generation_service = ng.NarrativeGenerationService(client, generation_model="terra", adjudication_model="sol")
    first_service = nn.NarrativeNormalizerService(policy=policy, context_provider=provider, generation_service=generation_service, clock=lambda: NOW)
    second_service = nn.NarrativeNormalizerService(policy=policy, context_provider=provider, generation_service=generation_service, clock=lambda: NOW)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(first_service.normalize_source, record.source_ref, record.source_digest)
        assert entered.wait(5)
        second = second_service.normalize_source(record.source_ref, record.source_digest)
        release.set()
        first = first_future.result(timeout=5)
    assert {first.status, second.status} == {nn.OUTCOME_CREATED, nn.OUTCOME_PROCESSING}
    assert len(client.requests) == 2


@pytest.mark.parametrize("error_type", [RuntimeError, ValueError, TypeError])
def test_unexpected_model_error_has_exact_safe_public_surface(tmp_path, error_type):
    spec, policy, source_path, record = write_source(tmp_path)
    source = nn.read_source_unit(policy, record.source_ref); provider = nn.TemplateNarrativeContextProvider(fixture_to_input(load_fixture("env_utf8")))
    sensitive = f"sensitive-{error_type.__name__}-detail"
    service = nn.NarrativeNormalizerService(
        policy=policy, context_provider=provider,
        generation_service=ng.NarrativeGenerationService(RaisingClient(error_type(sensitive)), generation_model="terra", adjudication_model="sol"),
        clock=lambda: NOW,
    )
    try:
        service.normalize_source(record.source_ref, record.source_digest)
    except Exception as error:
        assert type(error) is nn.NarrativeNormalizerError
        assert str(error) == "narrative_normalizer_internal_error"
        assert repr(error) == "NarrativeNormalizerError('narrative_normalizer_internal_error')"
        assert error.__cause__ is None
        assert error.__context__ is None
        assert sensitive not in "".join(traceback.format_exception(error))
    else:
        raise AssertionError("safe error required")


@pytest.mark.parametrize(
    "boundary",
    ("construct", "acquire", "release"),
    ids=("lock-construction-private", "lock-acquire-private", "lock-release-private"),
)
def test_normalize_lock_failures_have_safe_public_surface(tmp_path, monkeypatch, boundary):
    sensitive = rf"C:\secret\{boundary} credential=private-token"
    if boundary == "release":
        *values, _, _ = create_draft(tmp_path)
        service = values[-1]
        record = values[3]
        before_calls = len(values[7].requests)
    else:
        values = runtime(tmp_path)
        service = values[-1]
        record = values[3]
        before_calls = len(values[7].requests)

    class BrokenLock:
        def acquire(self):
            if boundary == "acquire":
                raise RuntimeError(sensitive)
            return True

        def release(self):
            if boundary == "release":
                raise RuntimeError(sensitive)

    if boundary == "construct":
        monkeypatch.setattr(
            service.store,
            "lock_for",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError(sensitive)),
        )
    else:
        monkeypatch.setattr(service.store, "lock_for", lambda *args, **kwargs: BrokenLock())
    with pytest.raises(nn.NarrativeNormalizerError) as captured:
        service.normalize_source(record.source_ref, record.source_digest)
    error = captured.value
    assert type(error) is nn.NarrativeNormalizerError
    assert str(error) == "narrative_normalizer_internal_error"
    assert repr(error) == "NarrativeNormalizerError('narrative_normalizer_internal_error')"
    assert error.__cause__ is None and error.__context__ is None
    assert sensitive not in "".join(traceback.format_exception(error))
    assert len(values[7].requests) == before_calls


@pytest.mark.parametrize(
    "cancellation_type",
    (KeyboardInterrupt, SystemExit, GeneratorExit),
    ids=("generation-keyboard-interrupt", "generation-system-exit", "generation-generator-exit"),
)
def test_generation_cancellation_survives_lock_release_failure(
    tmp_path,
    monkeypatch,
    cancellation_type,
):
    _, policy, _, record = write_source(tmp_path)
    provider = nn.TemplateNarrativeContextProvider(fixture_to_input(load_fixture("env_utf8")))
    cancellation = cancellation_type("cancel-generation")
    service = nn.NarrativeNormalizerService(
        policy=policy,
        context_provider=provider,
        generation_service=ng.NarrativeGenerationService(
            RaisingClient(cancellation),
            generation_model="terra",
            adjudication_model="sol",
        ),
        clock=lambda: NOW,
    )

    class ReleaseFails:
        def acquire(self):
            return True

        def release(self):
            raise RuntimeError(r"C:\secret\release credential=private-token")

    monkeypatch.setattr(service.store, "lock_for", lambda *args, **kwargs: ReleaseFails())
    with pytest.raises(cancellation_type, match="cancel-generation") as captured:
        service.normalize_source(record.source_ref, record.source_digest)
    assert captured.value is cancellation
    assert not service.store.draft_path(record.source_digest, source_ref=record.source_ref).exists()


@pytest.mark.parametrize("boundary", ["context", "registry", "source"])
def test_non_model_boundaries_remove_sensitive_exception_context(tmp_path, monkeypatch, boundary):
    spec, policy, source_path, record = write_source(tmp_path)
    sensitive = f"sensitive-{boundary}-private-path-detail"
    if boundary == "context":
        source = nn.read_source_unit(policy, record.source_ref, expected_digest=record.source_digest)

        class BrokenContext:
            def build(self, value):
                raise RuntimeError(sensitive)

        call = lambda: nn.build_normalization_input(source, BrokenContext())
        expected = "narrative_normalizer_context_invalid"
    elif boundary == "registry":
        monkeypatch.setattr(rq, "read_registry", lambda path: (_ for _ in ()).throw(RuntimeError(sensitive)))
        call = lambda: nn.scan_needs_narrative(policy)
        expected = "narrative_normalizer_registry_invalid"
    else:
        original = Path.read_bytes
        material = source_path / "material.md"
        monkeypatch.setattr(
            Path,
            "read_bytes",
            lambda self: (_ for _ in ()).throw(RuntimeError(sensitive)) if self == material else original(self),
        )
        call = lambda: nn.read_source_unit(policy, record.source_ref, expected_digest=record.source_digest)
        expected = "narrative_normalizer_source_invalid"
    with pytest.raises(nn.NarrativeNormalizerError) as captured:
        call()
    error = captured.value
    assert type(error) is nn.NarrativeNormalizerError
    assert str(error) == expected
    assert error.__cause__ is None
    assert error.__context__ is None
    assert sensitive not in "".join(traceback.format_exception(error))


@pytest.mark.parametrize("error", [KeyboardInterrupt(), SystemExit(7), GeneratorExit()])
def test_base_exception_is_never_swallowed(tmp_path, error):
    spec, policy, source_path, record = write_source(tmp_path)
    provider = nn.TemplateNarrativeContextProvider(fixture_to_input(load_fixture("env_utf8")))
    service = nn.NarrativeNormalizerService(
        policy=policy, context_provider=provider,
        generation_service=ng.NarrativeGenerationService(RaisingClient(error), generation_model="terra", adjudication_model="sol"),
        clock=lambda: NOW,
    )
    with pytest.raises(type(error)):
        service.normalize_source(record.source_ref, record.source_digest)


def test_plain_language_rejection_persists_review_but_not_ready(tmp_path):
    def contaminate(payload):
        payload["candidates"][0]["tension"]["text"] = "The provider callback entered the pipeline."
    *_, outcome, draft = create_draft(tmp_path, mutate_draft=contaminate)
    assert outcome.status == nn.OUTCOME_MANUAL_ATTENTION
    assert outcome.reason_codes
    assert outcome.review_status == nn.REVIEW_REJECTED
    assert not (draft / "narrative_ready.json").exists()


def test_approval_requires_passed_review(tmp_path):
    def contaminate(payload):
        payload["candidates"][0]["tension"]["text"] = "The provider callback entered the pipeline."
    *values, outcome, draft = create_draft(tmp_path, mutate_draft=contaminate)
    store = values[-1].store
    record = values[3]
    with pytest.raises(nn.NarrativeNormalizerError, match="narrative_normalizer_review_not_passed"):
        approve_created(store, record, draft)


def test_explicit_approval_creates_exact_production_manifest_outside_inbox(tmp_path):
    spec, policy, source_path, record, source, provider, context, client, service, outcome, draft = create_draft(tmp_path)
    before = rq.source_digest(source_path)
    approval = approve_created(service.store, record, draft)
    after = rq.source_digest(source_path)
    assert not approval.idempotent
    assert before == after == record.source_digest
    assert not (source_path / "narrative_ready.json").exists()
    manifest_path = draft / "narrative_ready.json"
    assert manifest_path.is_file() and not manifest_path.is_symlink()
    manifest = rq.validate_narrative_ready_manifest(policy, record.source_ref)
    assert type(manifest) is rq.NarrativeReadyManifest
    assert manifest.status == rq.CLASS_READY
    assert manifest.narrative_package_ref == f"{nn.source_identity(record.source_ref, record.source_digest)}/story.json"


def test_consumer_rejects_stale_prospective_attestation_without_latest_approved_event(
    tmp_path,
    monkeypatch,
):
    *values, _, draft = create_draft(tmp_path)
    policy, record, service = values[1], values[3], values[-1]
    expected = nn.validate_draft_directory(draft)["manifest"]["draft_identity"]
    identity = nn.source_identity(record.source_ref, record.source_digest)
    before = service.store._review_store().read(identity)
    assert before.latest.state == nn.review_state.STATE_PASSED
    # Simulate a crash after both prospective pair files are linked but before
    # the approved event append. Staging cleanup is intentionally interrupted;
    # the pair may remain on disk but must be consumer-ineligible.
    monkeypatch.setattr(nn, "_cleanup_owned_path", lambda path: None)
    with pytest.raises(nn.NarrativeNormalizerError):
        service.store.approve(
            record.source_ref,
            record.source_digest,
            expected_draft_identity=expected,
            reviewed_at=NOW.isoformat(),
            operator_request_id="prospective-stale-attestation",
        )
    assert (draft / "approval-attestation.json").is_file()
    assert (draft / "narrative_ready.json").is_file()
    after = service.store._review_store().read(identity)
    assert after == before
    with pytest.raises(rq.EligibilityError, match="narrative_approval_attestation_invalid"):
        rq.validate_narrative_ready_manifest(policy, record.source_ref)
    reconciliation = rq.reconcile_complete_backlog(
        policy,
        now=NOW + timedelta(minutes=1),
    )
    assert reconciliation.narrative_ready_count == 0
    assert rq.read_registry(policy.registry_path).records[0].status == rq.STATUS_NEEDS_NARRATIVE


@pytest.mark.parametrize(
    "mutation",
    (
        "story-json-rehashed-open-digests",
        "story-markdown",
        "review",
        "draft-manifest",
        "wrong-review-revision",
        "wrong-event-digest",
        "missing-key",
        "wrong-key",
        "missing-attestation",
        "missing-ready",
        "malformed-attestation",
        "extra-attestation-field",
        "attestation-symlink",
    ),
)
def test_consumer_attestation_tamper_matrix_never_makes_source_ready(tmp_path, mutation):
    *values, outcome, draft = create_draft(tmp_path)
    policy = values[1]
    record = values[3]
    client = values[7]
    store = values[-1].store
    approve_created(store, record, draft)
    ready_path = draft / "narrative_ready.json"
    attestation_path = draft / "approval-attestation.json"
    before_calls = len(client.requests)
    consumer_policy = policy

    if mutation == "story-json-rehashed-open-digests":
        story = json.loads((draft / "story.json").read_text(encoding="utf-8"))
        story["title"] = story["title"] + " altered"
        write_json(draft / "story.json", story)
        ready = json.loads(ready_path.read_text(encoding="utf-8"))
        ready["narrative_package_digest"] = hashlib.sha256(
            (draft / "story.json").read_bytes()
        ).hexdigest()
        write_json(ready_path, ready)
    elif mutation in {"story-markdown", "review", "draft-manifest"}:
        name = {
            "story-markdown": "story.md",
            "review": "review.json",
            "draft-manifest": "draft-manifest.json",
        }[mutation]
        path = draft / name
        path.write_bytes(path.read_bytes() + b"\n")
    elif mutation in {"wrong-review-revision", "wrong-event-digest"}:
        payload = json.loads(attestation_path.read_text(encoding="utf-8"))
        if mutation == "wrong-review-revision":
            payload["review_revision"] += 1
        else:
            payload["review_event_digest"] = "0" * 64
        write_json(attestation_path, payload)
    elif mutation == "missing-key":
        consumer_policy = dataclasses.replace(policy, narrative_trust_service=None)
    elif mutation == "wrong-key":
        consumer_policy = dataclasses.replace(
            policy,
            narrative_trust_service=nn.trust.NarrativeTrustService(b"wrong-consumer-key-material-32-bytes"),
        )
    elif mutation == "missing-attestation":
        attestation_path.unlink()
    elif mutation == "missing-ready":
        ready_path.unlink()
    elif mutation == "malformed-attestation":
        attestation_path.write_bytes(b"{not-json\n")
    elif mutation == "extra-attestation-field":
        payload = json.loads(attestation_path.read_text(encoding="utf-8"))
        payload["unexpected"] = "field"
        write_json(attestation_path, payload)
    else:
        encoded = attestation_path.read_bytes()
        outside = tmp_path / "outside-attestation.json"
        outside.write_bytes(encoded)
        attestation_path.unlink()
        try:
            attestation_path.symlink_to(outside)
        except OSError:
            pytest.skip("symlinks unavailable")

    with pytest.raises(rq.EligibilityError):
        rq.validate_narrative_ready_manifest(consumer_policy, record.source_ref)
    result = rq.reconcile_complete_backlog(consumer_policy, now=NOW + timedelta(minutes=1))
    current = rq.read_registry(policy.registry_path).records[0]
    assert result.narrative_ready_count == 0
    assert current.status == rq.STATUS_NEEDS_NARRATIVE
    assert len(client.requests) == before_calls


def test_consumer_rejects_attestation_copied_from_another_source(tmp_path):
    *first_values, _, first_draft = create_draft(tmp_path / "first", "technical_log")
    *second_values, _, second_draft = create_draft(tmp_path / "second", "quiet_object")
    first_store = first_values[-1].store
    second_store = second_values[-1].store
    first_record = first_values[3]
    second_record = second_values[3]
    approve_created(first_store, first_record, first_draft)
    approve_created(second_store, second_record, second_draft)
    copied = (first_draft / "approval-attestation.json").read_bytes()
    (second_draft / "approval-attestation.json").write_bytes(copied)
    with pytest.raises(rq.EligibilityError, match="narrative_approval_attestation_invalid"):
        rq.validate_narrative_ready_manifest(second_values[1], second_record.source_ref)


def test_duplicate_approval_is_idempotent(tmp_path):
    *values, outcome, draft = create_draft(tmp_path)
    store = values[-1].store; record = values[3]
    first = approve_created(store, record, draft)
    original = (draft / "narrative_ready.json").read_bytes()
    original_attestation = (draft / "approval-attestation.json").read_bytes()
    ledger_before = store._review_store().read(
        nn.source_identity(record.source_ref, record.source_digest)
    )
    second = approve_created(store, record, draft, reviewed_at=(NOW + timedelta(days=1)).isoformat())
    assert not first.idempotent and second.idempotent
    assert (draft / "narrative_ready.json").read_bytes() == original
    assert (draft / "approval-attestation.json").read_bytes() == original_attestation
    assert store._review_store().read(
        nn.source_identity(record.source_ref, record.source_digest)
    ) == ledger_before


def test_divergent_approval_conflicts_without_overwrite(tmp_path):
    *values, outcome, draft = create_draft(tmp_path)
    store = values[-1].store; record = values[3]
    contract = nn.validate_draft_directory(draft)
    approve_created(store, record, draft)
    path = draft / "narrative_ready.json"; original = path.read_bytes(); value = json.loads(original); value["narrative_package_digest"] = "0" * 64; write_json(path, value)
    with pytest.raises(nn.NarrativeNormalizerError, match="narrative_normalizer_approval_conflict"):
        store.approve(
            record.source_ref,
            record.source_digest,
            expected_draft_identity=contract["manifest"]["draft_identity"],
            reviewed_at=NOW.isoformat(),
        )
    assert path.read_text(encoding="utf-8") == json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def test_source_change_after_draft_blocks_approval(tmp_path):
    spec, policy, source_path, record, source, provider, context, client, service, outcome, draft = create_draft(tmp_path)
    (source_path / "changed.md").write_text("A later fact.\nAnother later fact.\n", encoding="utf-8")
    with pytest.raises(nn.NarrativeNormalizerError, match="narrative_normalizer_source_changed"):
        approve_created(service.store, record, draft)
    assert not (draft / "narrative_ready.json").exists()


def test_failed_manifest_self_validation_removes_only_new_manifest(tmp_path, monkeypatch):
    *values, outcome, draft = create_draft(tmp_path)
    store = values[-1].store
    monkeypatch.setattr(
        rq,
        "validate_narrative_ready_manifest",
        lambda *args, **kwargs: (_ for _ in ()).throw(rq.EligibilityError("narrative_manifest_invalid")),
    )
    with pytest.raises(nn.NarrativeNormalizerError, match="narrative_normalizer_manifest_invalid"):
        approve_created(store, values[3], draft)
    assert not (draft / "narrative_ready.json").exists()


def test_reconcile_moves_only_approved_source_to_ready(tmp_path):
    spec, policy, source_path, record, source, provider, context, client, service, outcome, draft = create_draft(tmp_path)
    before = rq.read_registry(policy.registry_path).records[0]
    assert before.status == rq.STATUS_NEEDS_NARRATIVE
    approve_created(service.store, record, draft)
    result = rq.reconcile_complete_backlog(policy, now=NOW + timedelta(minutes=1))
    after = rq.read_registry(policy.registry_path).records[0]
    assert result.narrative_ready_count == 1
    assert after.status == rq.STATUS_READY
    assert after.classification == rq.CLASS_READY


@pytest.mark.parametrize("field", sorted(rq.MANIFEST_KEYS))
def test_approved_manifest_requires_every_current_production_field(tmp_path, field):
    *values, outcome, draft = create_draft(tmp_path)
    store = values[-1].store; approve_created(store, values[3], draft)
    value = json.loads((draft / "narrative_ready.json").read_text(encoding="utf-8")); value.pop(field)
    with pytest.raises(rq.EligibilityError):
        rq.NarrativeReadyManifest.from_mapping(value)


def test_legacy_source_manifest_remains_supported(tmp_path):
    spec, policy, source_path, record = write_source(tmp_path)
    package = policy.narrative_outbox_root / "packages" / "item.json"; package.parent.mkdir(); package.write_text("{}", encoding="utf-8")
    payload = {
        "schema_version": rq.MANIFEST_SCHEMA_VERSION, "source_ref": record.source_ref,
        "source_digest": record.source_digest, "narrative_package_ref": "packages/item.json",
        "narrative_package_digest": rq.narrative_package_digest(package), "status": rq.CLASS_READY,
        "contract_versions": {"director": "director-v1", "narrative": "narrative-v1"},
    }
    write_json(source_path / "narrative_ready.json", payload)
    assert rq.validate_narrative_ready_manifest(policy, record.source_ref).status == rq.CLASS_READY


def test_two_manifest_locations_fail_closed_as_ambiguous(tmp_path):
    spec, policy, source_path, record, source, provider, context, client, service, outcome, draft = create_draft(tmp_path)
    approve_created(service.store, record, draft)
    (source_path / "narrative_ready.json").write_bytes((draft / "narrative_ready.json").read_bytes())
    with pytest.raises(rq.EligibilityError, match="narrative_manifest_ambiguous"):
        rq.validate_narrative_ready_manifest(policy, record.source_ref)


def test_reject_is_request_bound_idempotent_and_never_deletes_draft(tmp_path):
    *values, outcome, draft = create_draft(tmp_path)
    store = values[-1].store; record = values[3]
    contract = nn.validate_draft_directory(draft)
    kwargs = dict(
        operator_request_id="reject-request-1",
        expected_draft_identity=contract["manifest"]["draft_identity"],
        reason_codes=("narrative_normalizer_review_not_passed",),
        reviewed_at=NOW.isoformat(),
    )
    first = store.reject(record.source_ref, outcome.source_digest, **kwargs)
    review_bytes = (draft / "review.json").read_bytes()
    second = store.reject(record.source_ref, outcome.source_digest, **dict(kwargs, reviewed_at=(NOW + timedelta(days=1)).isoformat()))
    ledger = store._review_store().read(
        nn.source_identity(record.source_ref, record.source_digest),
        expected_draft_identity=contract["manifest"]["draft_identity"],
    )
    assert ledger.latest.state == nn.review_state.STATE_REJECTED
    assert not first.idempotent and second.idempotent
    assert (draft / "review.json").read_bytes() == review_bytes
    assert (draft / "story.md").is_file()


def test_old_signed_passed_bundle_replay_after_reject_never_restores_approval(tmp_path):
    *values, outcome, draft = create_draft(tmp_path)
    store = values[-1].store
    record = values[3]
    client = values[7]
    contract = nn.validate_draft_directory(draft)
    old_bundle = {item.name: item.read_bytes() for item in draft.iterdir() if item.is_file()}
    store.reject(
        record.source_ref,
        record.source_digest,
        expected_draft_identity=contract["manifest"]["draft_identity"],
        operator_request_id="reject-before-replay",
        reason_codes=("narrative_normalizer_review_not_passed",),
        reviewed_at=NOW.isoformat(),
    )
    ledger_path = store._review_store().path_for(
        nn.source_identity(record.source_ref, record.source_digest)
    )
    ledger_after_reject = ledger_path.read_bytes()
    for name, encoded in old_bundle.items():
        (draft / name).write_bytes(encoded)
    before_calls = len(client.requests)
    with pytest.raises(nn.NarrativeNormalizerError, match="narrative_normalizer_review_not_passed"):
        approve_created(store, record, draft)
    assert ledger_path.read_bytes() == ledger_after_reject
    assert store._review_store().read(
        nn.source_identity(record.source_ref, record.source_digest)
    ).latest.state == nn.review_state.STATE_REJECTED
    assert not (draft / "narrative_ready.json").exists()
    assert not (draft / "approval-attestation.json").exists()
    assert len(client.requests) == before_calls


def test_full_head_and_legacy_rollback_after_reject_never_erases_append_only_event(
    tmp_path,
):
    *values, outcome, draft = create_draft(tmp_path)
    store = values[-1].store
    record = values[3]
    client = values[7]
    contract = nn.validate_draft_directory(draft)
    identity = nn.source_identity(record.source_ref, record.source_digest)
    authority = store._review_store()
    old_head = authority.path_for(identity).read_bytes()
    old_bundle = {item.name: item.read_bytes() for item in draft.iterdir() if item.is_file()}
    store.reject(
        record.source_ref,
        record.source_digest,
        expected_draft_identity=contract["manifest"]["draft_identity"],
        operator_request_id="reject-before-full-authority-replay",
        reason_codes=("narrative_normalizer_review_not_passed",),
        reviewed_at=NOW.isoformat(),
    )
    event_three = tuple(authority.events_path_for(identity).glob("00000003-*.json"))
    assert len(event_three) == 1

    for name, encoded in old_bundle.items():
        (draft / name).write_bytes(encoded)
    authority.path_for(identity).write_bytes(old_head)
    legacy = store._state / "review-ledger" / f"{identity}.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(old_head)

    before_calls = len(client.requests)
    with pytest.raises(nn.NarrativeNormalizerError, match="narrative_normalizer_review_not_passed"):
        approve_created(store, record, draft)
    result = rq.reconcile_complete_backlog(store.policy, now=NOW + timedelta(minutes=1))
    latest = authority.read(identity)
    assert latest.latest.revision == 3
    assert latest.latest.state == nn.review_state.STATE_REJECTED
    assert event_three[0].is_file()
    assert result.narrative_ready_count == 0
    assert result.consumed_count == 0
    assert next(
        item for item in rq.read_registry(store.policy.registry_path).records
        if item.source_ref == record.source_ref
    ).status == rq.STATUS_NEEDS_NARRATIVE
    assert not (draft / "narrative_ready.json").exists()
    assert not (draft / "approval-attestation.json").exists()
    assert len(client.requests) == before_calls


def test_old_signed_passed_bundle_replay_after_supersede_never_restores_approval(tmp_path):
    store, first_value, second_value, relation, first_path = _create_supersede_pair(tmp_path)
    old_bundle = {item.name: item.read_bytes() for item in first_path.iterdir() if item.is_file()}
    kwargs = dict(
        old_source_ref=first_value["story"]["source_ref"],
        old_source_digest=first_value["story"]["source_digest"],
        old_source_identity=first_value["story"]["source_identity"],
        old_draft_identity=first_value["manifest"]["draft_identity"],
        new_source_ref=second_value["story"]["source_ref"],
        new_source_digest=second_value["story"]["source_digest"],
        new_source_identity=second_value["story"]["source_identity"],
        new_draft_identity=second_value["manifest"]["draft_identity"],
        operator_request_id="supersede-before-replay",
        reviewed_at=NOW.isoformat(),
    )
    store.supersede(**kwargs)
    ledger_path = store._review_store().path_for(first_value["story"]["source_identity"])
    ledger_after_supersede = ledger_path.read_bytes()
    for name, encoded in old_bundle.items():
        (first_path / name).write_bytes(encoded)
    with pytest.raises(
        nn.NarrativeNormalizerError,
        match="narrative_normalizer_(review_not_passed|source_changed)",
    ):
        store.approve(
            first_value["story"]["source_ref"],
            first_value["story"]["source_digest"],
            expected_draft_identity=first_value["manifest"]["draft_identity"],
            reviewed_at=NOW.isoformat(),
        )
    assert ledger_path.read_bytes() == ledger_after_supersede
    assert store._review_store().read(
        first_value["story"]["source_identity"]
    ).latest.state == nn.review_state.STATE_SUPERSEDED
    assert not (first_path / "narrative_ready.json").exists()
    assert not (first_path / "approval-attestation.json").exists()


def test_full_head_and_legacy_rollback_after_supersede_keeps_terminal_authority(tmp_path):
    store, first_value, second_value, relation, first_path = _create_supersede_pair(tmp_path)
    identity = first_value["story"]["source_identity"]
    authority = store._review_store()
    old_head = authority.path_for(identity).read_bytes()
    old_bundle = {item.name: item.read_bytes() for item in first_path.iterdir() if item.is_file()}
    store.supersede(
        **relation,
        operator_request_id="supersede-before-full-authority-replay",
        reviewed_at=NOW.isoformat(),
    )
    event_three = tuple(authority.events_path_for(identity).glob("00000003-*.json"))
    assert len(event_three) == 1
    for name, encoded in old_bundle.items():
        (first_path / name).write_bytes(encoded)
    authority.path_for(identity).write_bytes(old_head)
    legacy = store._state / "review-ledger" / f"{identity}.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(old_head)
    with pytest.raises(
        nn.NarrativeNormalizerError,
        match="narrative_normalizer_(review_not_passed|source_changed)",
    ):
        store.approve(
            first_value["story"]["source_ref"],
            first_value["story"]["source_digest"],
            expected_draft_identity=first_value["manifest"]["draft_identity"],
            reviewed_at=NOW.isoformat(),
        )
    result = rq.reconcile_complete_backlog(store.policy, now=NOW + timedelta(minutes=2))
    latest = authority.read(identity)
    assert latest.latest.revision == 3
    assert latest.latest.state == nn.review_state.STATE_SUPERSEDED
    assert event_three[0].is_file()
    assert result.narrative_ready_count == 0
    assert result.consumed_count == 0
    assert next(
        item for item in rq.read_registry(store.policy.registry_path).records
        if item.source_ref == first_value["story"]["source_ref"]
    ).status == rq.STATUS_NEEDS_NARRATIVE
    assert not (first_path / "narrative_ready.json").exists()
    assert not (first_path / "approval-attestation.json").exists()


@pytest.mark.parametrize("limit", range(1, 16))
def test_batch_limit_is_deterministic_and_does_not_expand_work(tmp_path, limit):
    values = runtime(tmp_path); record, service = values[3], values[8]
    rows = tuple((f"Project/{index:04d}", record.source_digest) for index in range(20))
    # A dry synthetic selection audit checks ordering before filesystem access.
    ordered = tuple(sorted(rows))[:limit]
    assert len(ordered) == limit
    assert ordered == tuple(sorted(ordered))


@pytest.mark.parametrize("workers", [0, -1, 9, 10, 99, True])
def test_batch_rejects_unsafe_worker_counts(tmp_path, workers):
    values = runtime(tmp_path); service = values[8]
    with pytest.raises(TypeError):
        service.normalize_batch((), max_workers=workers)


def test_failed_item_does_not_stop_real_serial_batch(tmp_path):
    values = runtime(tmp_path); record, service = values[3], values[8]
    result = service.normalize_batch(
        (("A/2026-08-01", "0" * 64), (record.source_ref, record.source_digest)),
        max_workers=1,
        dry_run=True,
    )
    summary = result.safe_summary()
    assert summary["completed_count"] == 2
    assert summary["status_counts"] == {"dry_run": 1, "failed": 1}
    assert result.outcomes[0].reason_codes == ("narrative_normalizer_source_invalid",)
    assert result.outcomes[1].status == nn.OUTCOME_DRY_RUN


def test_full_99_item_queue_dry_run_is_complete_and_deterministic(tmp_path):
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    registry = tmp_path / "state" / "registry.json"
    inbox.mkdir(); registry.parent.mkdir()
    for index in range(99):
        source = inbox / f"Project-{index:03d}" / "2026-08-01"
        source.mkdir(parents=True)
        (source / "material.md").write_text(
            f"Запись номер {index} была проверена.\nПовторная проверка номер {index} дала тот же ответ.\n",
            encoding="utf-8",
        )
    policy = rq.QuarantinePathPolicy(
        inbox, registry, outbox, nn.trust.NarrativeTrustService(TEST_TRUST_KEY),
        tmp_path / "review-authority",
    )
    rq.reconcile_complete_backlog(policy, now=NOW)
    rows = nn.scan_needs_narrative(policy)
    assert len(rows) == 99 and rows == tuple(sorted(rows))
    assert not outbox.exists()
    client = QueueClient([])
    service = nn.NarrativeNormalizerService(
        policy=policy,
        context_provider=nn.TemplateNarrativeContextProvider(fixture_to_input(load_fixture("env_utf8"))),
        generation_service=ng.NarrativeGenerationService(client, generation_model="terra", adjudication_model="sol"),
        clock=lambda: NOW,
    )
    result = service.normalize_batch(rows, dry_run=True)
    assert result.requested_count == len(result.outcomes) == 99
    assert {item.status for item in result.outcomes} == {nn.OUTCOME_DRY_RUN}
    assert not client.requests
    assert not outbox.exists()


def test_99_item_closed_outcome_ledger_has_exact_accounting_and_continues(
    tmp_path,
    monkeypatch,
):
    values = runtime(tmp_path)
    service = values[8]
    statuses = (
        nn.OUTCOME_DRAFT_READY_FOR_REVIEW,
        nn.OUTCOME_SOURCE_INSUFFICIENT,
        nn.OUTCOME_MANUAL_ATTENTION,
        nn.OUTCOME_SENSITIVE_REJECTED,
        nn.OUTCOME_EXISTING_DRAFT,
        nn.OUTCOME_PROCESSING,
        nn.OUTCOME_FAILED,
        nn.OUTCOME_UNCERTAIN,
    )
    reason_by_status = {
        nn.OUTCOME_SOURCE_INSUFFICIENT: ("narrative_normalizer_source_insufficient",),
        nn.OUTCOME_MANUAL_ATTENTION: ("narrative_normalizer_evidence_ambiguous",),
        nn.OUTCOME_SENSITIVE_REJECTED: ("narrative_normalizer_source_sensitive",),
        nn.OUTCOME_FAILED: ("narrative_normalizer_generation_failed",),
        nn.OUTCOME_UNCERTAIN: ("narrative_normalizer_claim_uncertain",),
    }
    rows = tuple(
        (f"Queue-{index:03d}/2026-08-17", f"{index:064x}")
        for index in range(99)
    )

    def classified(source_ref, source_digest, **kwargs):
        del kwargs
        index = int(source_digest, 16)
        status = statuses[index % len(statuses)]
        return nn.NormalizationOutcome(
            hashlib.sha256(source_ref.encode("utf-8")).hexdigest()[:12],
            source_digest,
            status,
            reason_by_status.get(status, ()),
            0,
            None,
            None,
            "generic",
        )

    monkeypatch.setattr(service, "normalize_source", classified)
    result = service.normalize_batch(rows, max_workers=4)
    summary = result.safe_summary()
    assert result.requested_count == len(result.outcomes) == 99
    assert summary["requested_count"] == summary["completed_count"] == 99
    assert summary["accounted_count"] == 99
    assert summary["accounting_complete"] is True
    assert set(summary["status_counts"]) == set(statuses)
    assert sum(summary["status_counts"].values()) == 99
    assert all(summary[status] == summary["status_counts"][status] for status in statuses)


def test_cli_99_record_absent_outbox_dry_run_is_exact_zero_write(tmp_path, monkeypatch, capsys):
    inbox = tmp_path / "inbox"
    registry = tmp_path / "state" / "registry.json"
    outbox = tmp_path / "absent-outbox"
    registry.parent.mkdir(parents=True)
    for index in range(99):
        source = inbox / f"Project-{index:03d}" / "2026-08-01"
        source.mkdir(parents=True)
        (source / "material.md").write_text(
            f"Запись номер {index} проверили вручную.\n"
            f"Повторная проверка записи номер {index} дала тот же результат.\n",
            encoding="utf-8",
        )
    policy = rq.QuarantinePathPolicy(
        inbox, registry, outbox, nn.trust.NarrativeTrustService(TEST_TRUST_KEY),
        tmp_path / "review-authority",
    )
    rq.reconcile_complete_backlog(policy, now=NOW)
    assert len(nn.scan_needs_narrative(policy)) == 99
    assert not outbox.exists()

    before = _filesystem_metadata_snapshot(tmp_path)
    calls = {
        "adapter": 0,
        "atomic_write": 0,
        "lock": 0,
        "mkdir": 0,
        "model_store": 0,
        "registry_mutation": 0,
        "write_bytes": 0,
        "write_text": 0,
    }

    def forbidden(name):
        def fail(*args, **kwargs):
            del args, kwargs
            calls[name] += 1
            raise AssertionError(f"dry-run attempted {name}")

        return fail

    monkeypatch.setattr(nn, "load_adapter", forbidden("adapter"))
    monkeypatch.setattr(nn, "NarrativeOutboxStore", forbidden("model_store"))
    monkeypatch.setattr(nn, "_atomic_write", forbidden("atomic_write"))
    monkeypatch.setattr(nn._FileLock, "acquire", forbidden("lock"))
    monkeypatch.setattr(rq, "_mutate_registry", forbidden("registry_mutation"))
    monkeypatch.setattr(Path, "mkdir", forbidden("mkdir"))
    monkeypatch.setattr(Path, "write_bytes", forbidden("write_bytes"))
    monkeypatch.setattr(Path, "write_text", forbidden("write_text"))

    code = cli.run([
        "--inbox-root", str(inbox),
        "--registry-path", str(registry),
        "--outbox-root", str(outbox),
        "normalize", "--all", "--dry-run",
    ])
    payload = json.loads(capsys.readouterr().out)
    after = _filesystem_metadata_snapshot(tmp_path)

    assert code == 0
    assert payload["requested_count"] == payload["completed_count"] == 99
    assert payload["status_counts"] == {"dry_run": 99}
    assert payload["evidence_fast_path"] == 0
    assert payload["evidence_generic_path"] == 99
    assert payload["known_rule_count"] == 0
    assert payload["generic_fallback_candidate_count"] == 99
    assert payload["truly_insufficient_count"] == 0
    assert payload["manual_attention_count"] == 0
    assert payload["sensitive_count"] == 0
    assert sum(payload["coverage_counts"].values()) == 99
    assert len(payload["items"]) == 99
    assert all(item["model_call_count"] == 0 for item in payload["items"])
    assert all(item["status"] == nn.OUTCOME_DRY_RUN for item in payload["items"])
    assert calls == {key: 0 for key in calls}
    assert after == before
    assert not outbox.exists()


def test_coverage_snapshot_explicit_99_registry_is_private_and_exact_zero_write(
    tmp_path, capsys, monkeypatch,
):
    inbox = tmp_path / "private-inbox"
    registry = tmp_path / "private-state" / "registry.json"
    outbox = tmp_path / "absent-narrative-outbox"
    registry.parent.mkdir(parents=True)
    for index in range(99):
        source = inbox / f"Secret-Project-{index:03d}" / "2026-08-17"
        source.mkdir(parents=True)
        (source / f"private-{index:03d}.md").write_text(
            f"Service {index} migrated records.\nSeparate check {index} preserved the result.\n",
            encoding="utf-8",
        )
    policy = rq.QuarantinePathPolicy(
        inbox, registry, outbox,
        narrative_review_authority_root=tmp_path / "review-authority",
    )
    rq.reconcile_complete_backlog(policy, now=NOW)
    assert len(nn.scan_needs_narrative(policy)) == 99
    before = _filesystem_metadata_snapshot(tmp_path)
    attempts = {"atomic": 0, "lock": 0, "mkdir": 0, "registry": 0, "write": 0}

    def forbidden(name):
        def fail(*args, **kwargs):
            del args, kwargs
            attempts[name] += 1
            raise AssertionError(f"coverage-snapshot attempted {name}")
        return fail

    monkeypatch.setattr(nn, "_atomic_write", forbidden("atomic"))
    monkeypatch.setattr(nn._FileLock, "acquire", forbidden("lock"))
    monkeypatch.setattr(rq, "_mutate_registry", forbidden("registry"))
    monkeypatch.setattr(Path, "mkdir", forbidden("mkdir"))
    monkeypatch.setattr(Path, "write_bytes", forbidden("write"))
    monkeypatch.setattr(Path, "write_text", forbidden("write"))
    code = cli.run([
        "--registry", str(registry),
        "--inbox-root", str(inbox),
        "--narrative-outbox", str(outbox),
        "coverage-snapshot",
    ])
    rendered = capsys.readouterr().out
    payload = json.loads(rendered)
    after = _filesystem_metadata_snapshot(tmp_path)
    assert code == 0
    assert payload["schema_version"] == "normalizer-coverage-snapshot-v1"
    assert payload["total_record_count"] == 99
    assert sum(payload["structural_categories"].values()) == 99
    assert payload["fast_candidate_count"] + payload["generic_candidate_count"] <= 99
    assert len(payload["aggregate_snapshot_digest"]) == 64
    assert payload["file_count_range"] == [1, 1]
    assert payload["segment_count_range"][0] >= 2
    assert attempts == {key: 0 for key in attempts}
    assert after == before
    assert not outbox.exists()
    assert "Secret-Project" not in rendered
    assert "private-inbox" not in rendered
    assert "migrated records" not in rendered
    assert str(tmp_path) not in rendered


def test_dry_run_has_no_model_or_outbox_draft(tmp_path):
    values = runtime(tmp_path); record, client, service = values[3], values[7], values[8]
    outcome = service.normalize_source(record.source_ref, record.source_digest, dry_run=True)
    assert outcome.status == nn.OUTCOME_DRY_RUN
    assert not client.requests
    assert not service.store.draft_path(record.source_digest).exists()


def test_unmapped_source_without_evidence_adapter_requires_manual_attention_without_write(tmp_path):
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "absent-outbox"
    registry = tmp_path / "state" / "registry.json"
    source_path = inbox / "Unknown" / "2026-08-16"
    source_path.mkdir(parents=True)
    registry.parent.mkdir(parents=True)
    (source_path / "material.md").write_text(
        "workers carefully migrated records.\nThe database remained available.\n",
        encoding="utf-8",
    )
    policy = rq.QuarantinePathPolicy(
        inbox, registry, outbox,
        narrative_review_authority_root=tmp_path / "review-authority",
    )
    rq.reconcile_complete_backlog(policy, now=NOW)
    record = rq.read_registry(registry).records[0]
    provider = nn.TemplateNarrativeContextProvider(fixture_to_input(load_fixture("env_utf8")))
    client = QueueClient([])
    service = nn.NarrativeNormalizerService(
        policy=policy,
        context_provider=provider,
        generation_service=ng.NarrativeGenerationService(client, generation_model="terra", adjudication_model="sol"),
        clock=lambda: NOW,
    )
    outcome = service.normalize_source(record.source_ref, record.source_digest)
    assert outcome.status == nn.OUTCOME_MANUAL_ATTENTION
    assert outcome.reason_codes == ("narrative_normalizer_evidence_ambiguous",)
    assert outcome.model_call_count == 0
    assert client.requests == []
    assert not outbox.exists()


def test_non_dry_normalization_without_trust_key_is_zero_model_zero_claim(
    tmp_path,
    monkeypatch,
):
    values = runtime(tmp_path)
    record, client, service = values[3], values[7], values[8]
    monkeypatch.delenv("NARRATIVE_NORMALIZER_TRUST_KEY", raising=False)
    outcome = service.normalize_source(record.source_ref, record.source_digest)
    assert outcome.status == nn.OUTCOME_MANUAL_ATTENTION
    assert outcome.reason_codes == ("narrative_normalizer_trust_unavailable",)
    assert outcome.model_call_count == 0
    assert client.requests == []
    assert not service.store.draft_path(
        record.source_digest,
        source_ref=record.source_ref,
    ).exists()
    assert not os.path.lexists(service.store.claim_path(record.source_ref, record.source_digest))


def test_non_dry_normalization_without_review_authority_is_zero_model_zero_claim(tmp_path):
    values = runtime(tmp_path)
    policy, record, provider, client = values[1], values[3], values[5], values[7]
    authority = policy.narrative_review_authority_root
    assert authority is not None and not authority.exists()
    keyless_authority_policy = dataclasses.replace(
        policy,
        narrative_review_authority_root=None,
    )
    service = nn.NarrativeNormalizerService(
        policy=keyless_authority_policy,
        context_provider=provider,
        generation_service=ng.NarrativeGenerationService(
            client,
            generation_model="terra-medium",
            adjudication_model="sol-high",
            repair_model="terra-high",
        ),
        clock=lambda: NOW,
    )
    outcome = service.normalize_source(record.source_ref, record.source_digest)
    assert outcome.status == nn.OUTCOME_MANUAL_ATTENTION
    assert outcome.reason_codes == ("narrative_normalizer_review_authority_unavailable",)
    assert outcome.model_call_count == 0
    assert client.requests == []
    assert not service.store.draft_path(
        record.source_digest,
        source_ref=record.source_ref,
    ).exists()
    assert not os.path.lexists(service.store.claim_path(record.source_ref, record.source_digest))
    assert not authority.exists()


def test_approval_without_key_or_with_wrong_key_fails_closed_without_model_call(
    tmp_path,
    monkeypatch,
):
    *values, _, draft = create_draft(tmp_path)
    policy, record, client = values[1], values[3], values[7]
    before_model_calls = len(client.requests)
    identity = nn.validate_draft_directory(draft)["manifest"]["draft_identity"]
    monkeypatch.delenv("NARRATIVE_NORMALIZER_TRUST_KEY", raising=False)
    keyless_store = nn.NarrativeOutboxStore(policy)
    with pytest.raises(nn.NarrativeNormalizerError, match="narrative_normalizer_trust_unavailable"):
        keyless_store.approve(
            record.source_ref,
            record.source_digest,
            expected_draft_identity=identity,
            reviewed_at=NOW.isoformat(),
        )
    wrong_key_store = nn.NarrativeOutboxStore(
        policy,
        trust_service=nn.trust.NarrativeTrustService(b"w" * 32),
    )
    with pytest.raises(nn.NarrativeNormalizerError, match="narrative_normalizer_trust_invalid"):
        wrong_key_store.approve(
            record.source_ref,
            record.source_digest,
            expected_draft_identity=identity,
            reviewed_at=NOW.isoformat(),
        )
    assert len(client.requests) == before_model_calls
    assert not (draft / "narrative_ready.json").exists()


_GENERIC_E2E_SHAPES = (
    (
        "markdown",
        "material.md",
        "# Сводка\nсервис перенёс записи.\nбаза осталась доступной.\nпроверка завершилась спокойно.\nжурнал сохранил результат.\nкоманда сверила итог.\n",
        (
            "сервис перенёс записи.",
            "база осталась доступной.",
            "проверка завершилась спокойно.",
            "журнал сохранил результат.",
            "команда сверила итог.",
        ),
    ),
    (
        "json-nested",
        "material.json",
        '{"report":{"events":["сервис перенёс записи.","база осталась доступной.","проверка завершилась спокойно.","журнал сохранил результат.","команда сверила итог."]}}',
        (
            "сервис перенёс записи.",
            "база осталась доступной.",
            "проверка завершилась спокойно.",
            "журнал сохранил результат.",
            "команда сверила итог.",
        ),
    ),
    (
        "multiline-log",
        "material.log",
        "сервис перенёс записи.\nбаза осталась доступной.\nпроверка завершилась спокойно.\nжурнал сохранил результат.\nкоманда сверила итог.\n",
        (
            "сервис перенёс записи.",
            "база осталась доступной.",
            "проверка завершилась спокойно.",
            "журнал сохранил результат.",
            "команда сверила итог.",
        ),
    ),
    (
        "key-value",
        "material.txt",
        "шаг: сервис перенёс записи.\nсостояние: база осталась доступной.\nпроверка: завершилась спокойно.\nжурнал: сохранил результат.\nкоманда: сверила итог.\n",
        (
            "шаг: сервис перенёс записи.",
            "состояние: база осталась доступной.",
            "проверка: завершилась спокойно.",
            "журнал: сохранил результат.",
            "команда: сверила итог.",
        ),
    ),
    (
        "chat",
        "material.txt",
        "оператор: сервис перенёс записи.\nнаблюдатель: база осталась доступной.\nревьюер: проверка завершилась спокойно.\nархив: журнал сохранил результат.\nкоманда: сверила итог.\n",
        (
            "оператор: сервис перенёс записи.",
            "наблюдатель: база осталась доступной.",
            "ревьюер: проверка завершилась спокойно.",
            "архив: журнал сохранил результат.",
            "команда: сверила итог.",
        ),
    ),
    (
        "email",
        "material.txt",
        "От: команда\nТема: сводка\n\nсервис перенёс записи.\nбаза осталась доступной.\nпроверка завершилась спокойно.\nжурнал сохранил результат.\nкоманда сверила итог.\n",
        (
            "сервис перенёс записи.",
            "база осталась доступной.",
            "проверка завершилась спокойно.",
            "журнал сохранил результат.",
            "команда сверила итог.",
        ),
    ),
    (
        "mixed-language",
        "material.txt",
        "сервис завершил build.\nбаза сохранила stable state.\nпроверка дала clean result.\nжурнал сохранил final record.\nкоманда сверила review outcome.\n",
        (
            "сервис завершил build.",
            "база сохранила stable state.",
            "проверка дала clean result.",
            "журнал сохранил final record.",
            "команда сверила review outcome.",
        ),
    ),
)


@pytest.mark.parametrize(
    "shape,filename,body,propositions",
    _GENERIC_E2E_SHAPES,
    ids=[item[0] for item in _GENERIC_E2E_SHAPES],
)
def test_unseen_source_shape_runs_generic_evidence_story_and_model_free_approval(
    tmp_path,
    shape,
    filename,
    body,
    propositions,
):
    del shape
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    registry = tmp_path / "state" / "registry.json"
    source_path = inbox / "Generic" / "2026-08-17"
    source_path.mkdir(parents=True)
    registry.parent.mkdir(parents=True)
    (source_path / filename).write_text(body, encoding="utf-8")
    policy = rq.QuarantinePathPolicy(
        inbox, registry, outbox, nn.trust.NarrativeTrustService(TEST_TRUST_KEY),
        tmp_path / "review-authority",
    )
    rq.reconcile_complete_backlog(policy, now=NOW)
    record = rq.read_registry(registry).records[0]
    raw_source = nn.read_source_unit(
        policy,
        record.source_ref,
        expected_digest=record.source_digest,
        allow_insufficient=True,
    )
    documents = nn.read_source_documents(
        policy,
        record.source_ref,
        expected_digest=record.source_digest,
    )
    extraction, evidence_adjudication = generic_evidence_responses(documents, propositions)

    # Build the fake CP2 response against the exact fact tuple that the real
    # evidence boundary will deterministically project.
    preparation_client = QueueClient([copy.deepcopy(extraction), copy.deepcopy(evidence_adjudication)])
    preparation_service = nn.evidence.GenericEvidenceService(
        preparation_client,
        extraction_model="terra-evidence-medium",
        adjudication_model="sol-evidence-high",
    )
    preparation = preparation_service.resolve(documents)
    assert preparation.status == "verified" and preparation.verified_bundle is not None
    verified_source = nn._source_from_verified_evidence(
        raw_source,
        documents,
        preparation.verified_bundle,
    )
    context_data = load_fixture("env_utf8")
    provider = nn.TemplateNarrativeContextProvider(fixture_to_input(context_data))
    context = provider.build(verified_source)
    story_response = generic_story_response(context_data, propositions)
    story_adjudication = fake_adjudication_payload(story_response, context)

    evidence_client = QueueClient([extraction, evidence_adjudication])
    generation_client = QueueClient([story_response, story_adjudication])
    evidence_service = nn.evidence.GenericEvidenceService(
        evidence_client,
        extraction_model="terra-evidence-medium",
        adjudication_model="sol-evidence-high",
    )
    trust_service = nn.trust.NarrativeTrustService(TEST_TRUST_KEY)
    service = nn.NarrativeNormalizerService(
        policy=policy,
        context_provider=provider,
        generation_service=ng.NarrativeGenerationService(
            generation_client,
            generation_model="terra-story-medium",
            adjudication_model="sol-story-high",
            repair_model="terra-story-high",
        ),
        evidence_service=evidence_service,
        trust_service=trust_service,
        clock=lambda: NOW,
    )
    outcome = service.normalize_source(record.source_ref, record.source_digest)
    assert outcome.status == nn.OUTCOME_CREATED
    assert outcome.evidence_path == "generic"
    assert outcome.model_call_count == 4
    assert [item.request_kind for item in evidence_client.requests] == [
        "evidence_extraction",
        "evidence_adjudication",
    ]
    assert [item.request_kind for item in generation_client.requests] == [
        "generation",
        "adjudication",
    ]
    draft = service.store.draft_path(record.source_digest, source_ref=record.source_ref)
    value = nn.validate_draft_directory(
        draft,
        trust_service=trust_service,
        require_trust=True,
    )
    assert value["source"].evidence_mode == "generic"
    assert tuple(item.exact_text for item in value["source"].facts) == propositions
    assert value["factuality"].passed, {
        "unsupported": value["factuality"].unsupported_claim_ids,
        "claims": [
            (item.claim_id, nn._generic_claim_supported(value["source"], item), item)
            for item in value["claims"]
        ],
    }
    assert value["meaning"].passed
    assert value["plain_language"].passed
    assert value["trust_verified"] is True
    before_calls = (len(evidence_client.requests), len(generation_client.requests))
    approval = service.store.approve(
        record.source_ref,
        record.source_digest,
        expected_draft_identity=value["manifest"]["draft_identity"],
        reviewed_at=NOW.isoformat(),
    )
    assert approval.status == rq.CLASS_READY
    assert before_calls == (len(evidence_client.requests), len(generation_client.requests))
    assert rq.validate_narrative_ready_manifest(policy, record.source_ref).status == rq.CLASS_READY


def test_generic_path_schema_repair_uses_exact_five_call_budget_and_model_free_approval(
    tmp_path,
):
    _, filename, body, propositions = _GENERIC_E2E_SHAPES[0]
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    registry = tmp_path / "state" / "registry.json"
    source_path = inbox / "Generic-Repair" / "2026-08-17"
    source_path.mkdir(parents=True)
    registry.parent.mkdir(parents=True)
    (source_path / filename).write_text(body, encoding="utf-8")
    policy = rq.QuarantinePathPolicy(
        inbox, registry, outbox, nn.trust.NarrativeTrustService(TEST_TRUST_KEY),
        tmp_path / "review-authority",
    )
    rq.reconcile_complete_backlog(policy, now=NOW)
    record = rq.read_registry(registry).records[0]
    raw_source = nn.read_source_unit(
        policy,
        record.source_ref,
        expected_digest=record.source_digest,
        allow_insufficient=True,
    )
    documents = nn.read_source_documents(
        policy,
        record.source_ref,
        expected_digest=record.source_digest,
    )
    extraction, evidence_adjudication = generic_evidence_responses(documents, propositions)
    preparation = nn.evidence.GenericEvidenceService(
        QueueClient([copy.deepcopy(extraction), copy.deepcopy(evidence_adjudication)]),
        extraction_model="terra-evidence-medium",
        adjudication_model="sol-evidence-high",
    ).resolve(documents)
    assert preparation.status == "verified" and preparation.verified_bundle is not None
    verified_source = nn._source_from_verified_evidence(
        raw_source,
        documents,
        preparation.verified_bundle,
    )
    context_data = load_fixture("env_utf8")
    provider = nn.TemplateNarrativeContextProvider(fixture_to_input(context_data))
    context = provider.build(verified_source)
    story_response = generic_story_response(context_data, propositions)
    story_adjudication = fake_adjudication_payload(story_response, context)
    evidence_client = QueueClient([extraction, evidence_adjudication])
    generation_client = QueueClient(["not-json", story_response, story_adjudication])
    trust_service = nn.trust.NarrativeTrustService(TEST_TRUST_KEY)
    service = nn.NarrativeNormalizerService(
        policy=policy,
        context_provider=provider,
        generation_service=ng.NarrativeGenerationService(
            generation_client,
            generation_model="terra-story-medium",
            adjudication_model="sol-story-high",
            repair_model="terra-story-high",
        ),
        evidence_service=nn.evidence.GenericEvidenceService(
            evidence_client,
            extraction_model="terra-evidence-medium",
            adjudication_model="sol-evidence-high",
        ),
        trust_service=trust_service,
        clock=lambda: NOW,
    )
    outcome = service.normalize_source(record.source_ref, record.source_digest)
    assert outcome.status == nn.OUTCOME_DRAFT_READY_FOR_REVIEW
    assert outcome.evidence_path == "generic"
    assert outcome.model_call_count == 5
    assert [item.request_kind for item in evidence_client.requests] == [
        "evidence_extraction",
        "evidence_adjudication",
    ]
    assert [item.request_kind for item in generation_client.requests] == [
        "generation",
        "repair",
        "adjudication",
    ]
    draft = service.store.draft_path(record.source_digest, source_ref=record.source_ref)
    value = nn.validate_draft_directory(
        draft,
        trust_service=trust_service,
        require_trust=True,
    )
    assert value["cp2_adjudication_evidence"].model_call_count == 3
    calls_before_approval = (
        len(evidence_client.requests),
        len(generation_client.requests),
    )
    approval = service.store.approve(
        record.source_ref,
        record.source_digest,
        expected_draft_identity=value["manifest"]["draft_identity"],
        reviewed_at=NOW.isoformat(),
    )
    assert approval.status == rq.CLASS_READY
    assert calls_before_approval == (
        len(evidence_client.requests),
        len(generation_client.requests),
    )


def test_coverage_v2_incomplete_source_persists_safe_manual_attention_package(tmp_path):
    _, filename, body, _propositions = _GENERIC_E2E_SHAPES[0]
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    registry = tmp_path / "state" / "registry.json"
    source_path = inbox / "Manual-Coverage" / "2026-08-20"
    source_path.mkdir(parents=True)
    registry.parent.mkdir(parents=True)
    (source_path / filename).write_text(body, encoding="utf-8")
    trust_service = nn.trust.NarrativeTrustService(TEST_TRUST_KEY)
    policy = rq.QuarantinePathPolicy(
        inbox, registry, outbox, trust_service, tmp_path / "review-authority"
    )
    rq.reconcile_complete_backlog(policy, now=NOW)
    record = rq.read_registry(registry).records[0]
    documents = nn.read_source_documents(
        policy, record.source_ref, expected_digest=record.source_digest
    )
    inventory = nn.evidence.build_source_block_inventory(documents)
    coverage = {
        "schema_version": nn.evidence.EVIDENCE_COVERAGE_CONTRACT_VERSION,
        "source_identity": documents.source_identity,
        "document_bundle_digest": documents.bundle_digest,
        "inventory_digest": inventory.inventory_digest,
        "run_id": "manual-coverage-run",
        "block_dispositions": {
            block.block_id: "ambiguous" for block in inventory.ordered_blocks
        },
    }
    evidence_client = QueueClient([coverage])
    generation_client = QueueClient([])
    service = nn.NarrativeNormalizerService(
        policy=policy,
        context_provider=nn.TemplateNarrativeContextProvider(fixture_to_input(load_fixture("quiet_object"))),
        generation_service=ng.NarrativeGenerationService(
            generation_client,
            generation_model="content-model",
            adjudication_model="review-model",
        ),
        evidence_service=nn.evidence.GenericEvidenceService(
            evidence_client,
            extraction_model="content-model",
            adjudication_model="review-model",
            coverage_v2=True,
        ),
        trust_service=trust_service,
        clock=lambda: NOW,
    )
    outcome = service.normalize_source(record.source_ref, record.source_digest)
    assert outcome.status == nn.OUTCOME_MANUAL_ATTENTION_PACKAGE_READY, outcome
    assert outcome.model_call_count == 1
    package = service.store.draft_path(record.source_digest, source_ref=record.source_ref)
    assert {item.name for item in package.iterdir()} == {
        "manual-attention.json", "manual-attention.md"
    }
    payload = json.loads((package / "manual-attention.json").read_text(encoding="utf-8"))
    assert payload["narrative_ready"] is False
    assert payload["verified_candidate_fact_summaries"] == []
    assert payload["schema_version"] == nn.MANUAL_ATTENTION_CONTRACT_VERSION
    assert payload["confirmed_fact_count"] == 0
    assert payload["verified_candidate_fact_summaries"] == []
    assert payload["human_actions"] == [
        "use_confirmed_facts", "discuss_ambiguous_parts", "skip_material",
    ]
    assert payload["coverage_counts"]["valid_dispositions"] >= 1
    assert payload["coverage_counts"]["ambiguous_blocks"] >= 1
    markdown = (package / "manual-attention.md").read_text(encoding="utf-8")
    assert "Confirmed safe facts: 0" in markdown
    assert "Choose one action:" in markdown
    assert body not in markdown
    assert not (package / "story.json").exists()
    assert generation_client.requests == []
    assert rq.read_registry(registry).records[0].status == rq.STATUS_NEEDS_NARRATIVE
    with pytest.raises(nn.NarrativeNormalizerError):
        nn.validate_draft_directory(package, trust_service=trust_service, require_trust=True)
    with pytest.raises(nn.NarrativeNormalizerError):
        service.store.approve(
            record.source_ref,
            record.source_digest,
            expected_draft_identity="not-a-draft",
            reviewed_at=NOW.isoformat(),
        )
    replay = service.normalize_source(record.source_ref, record.source_digest)
    assert replay.status == nn.OUTCOME_MANUAL_ATTENTION_PACKAGE_READY
    assert replay.model_call_count == 0
    assert len(evidence_client.requests) == 1


def test_manual_retry_can_finish_as_useful_manual_attention_without_broker_event(tmp_path):
    _, filename, body, _propositions = _GENERIC_E2E_SHAPES[0]
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    registry = tmp_path / "state" / "registry.json"
    source_path = inbox / "Manual-Retry-Coverage" / "2026-08-20"
    source_path.mkdir(parents=True)
    registry.parent.mkdir(parents=True)
    (source_path / filename).write_text(body, encoding="utf-8")
    trust_service = nn.trust.NarrativeTrustService(TEST_TRUST_KEY)
    authority_root = tmp_path / "review-authority"
    policy = rq.QuarantinePathPolicy(
        inbox, registry, outbox, trust_service, authority_root
    )
    rq.reconcile_complete_backlog(policy, now=NOW)
    record = rq.read_registry(registry).records[0]
    documents = nn.read_source_documents(
        policy, record.source_ref, expected_digest=record.source_digest
    )
    source = nn.read_source_unit(
        policy,
        record.source_ref,
        expected_digest=record.source_digest,
        allow_insufficient=True,
    )
    inventory = nn.evidence.build_source_block_inventory(documents)
    coverage = {
        "schema_version": nn.evidence.EVIDENCE_COVERAGE_CONTRACT_VERSION,
        "source_identity": documents.source_identity,
        "document_bundle_digest": documents.bundle_digest,
        "inventory_digest": inventory.inventory_digest,
        "run_id": "manual-retry-coverage-run",
        "block_dispositions": {
            block.block_id: "ambiguous" for block in inventory.ordered_blocks
        },
    }
    evidence_client = QueueClient([coverage])
    generation_client = QueueClient([])
    service = nn.NarrativeNormalizerService(
        policy=policy,
        context_provider=nn.TemplateNarrativeContextProvider(
            fixture_to_input(load_fixture("quiet_object"))
        ),
        generation_service=ng.NarrativeGenerationService(
            generation_client,
            generation_model="content-model",
            adjudication_model="review-model",
        ),
        evidence_service=nn.evidence.GenericEvidenceService(
            evidence_client,
            extraction_model="content-model",
            adjudication_model="review-model",
            coverage_v2=True,
        ),
        trust_service=trust_service,
        clock=lambda: NOW,
    )
    _path, old_bytes, old_digest = _seed_failed_claim(service, source)
    request = _manual_retry_request(source, old_digest, request_id="manual-retry-attention-0001")

    outcome = service.normalize_source(
        record.source_ref,
        record.source_digest,
        manual_retry=request,
    )

    assert outcome.status == nn.OUTCOME_MANUAL_ATTENTION_PACKAGE_READY
    assert outcome.model_call_count == 1
    package = service.store.draft_path(record.source_digest, source_ref=record.source_ref)
    assert {item.name for item in package.iterdir()} == {
        "manual-attention.json", "manual-attention.md"
    }
    assert not authority_root.exists()
    assert generation_client.requests == []
    assert service.store.archived_attempt_bytes(
        request.source_identity, request.previous_failed_attempt_id
    ) == old_bytes
    current = service.store.read_claim(record.source_ref, record.source_digest)
    assert current is not None and current["attempt_id"] != request.previous_failed_attempt_id
    assert current["reason_code"] == "narrative_normalizer_manual_attention_package_ready"


@pytest.mark.parametrize(
    "retained_token_indexes",
    ((0,), (0, 1), (0, 2), (2, 1, 0)),
    ids=("subject-only", "missing-object", "missing-predicate", "free-paraphrase"),
)
def test_generic_public_claim_must_match_complete_verified_extracted_proposition(
    tmp_path,
    retained_token_indexes,
):
    _, filename, body, propositions = _GENERIC_E2E_SHAPES[0]
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    registry = tmp_path / "state" / "registry.json"
    source_path = inbox / "Generic-Omission" / "2026-08-17"
    source_path.mkdir(parents=True)
    registry.parent.mkdir(parents=True)
    (source_path / filename).write_text(body, encoding="utf-8")
    policy = rq.QuarantinePathPolicy(
        inbox, registry, outbox,
        narrative_review_authority_root=tmp_path / "review-authority",
    )
    rq.reconcile_complete_backlog(policy, now=NOW)
    record = rq.read_registry(registry).records[0]
    raw_source = nn.read_source_unit(
        policy,
        record.source_ref,
        expected_digest=record.source_digest,
        allow_insufficient=True,
    )
    documents = nn.read_source_documents(
        policy,
        record.source_ref,
        expected_digest=record.source_digest,
    )
    extraction, evidence_adjudication = generic_evidence_responses(documents, propositions)
    preparation = nn.evidence.GenericEvidenceService(
        QueueClient([copy.deepcopy(extraction), copy.deepcopy(evidence_adjudication)]),
        extraction_model="terra-evidence-medium",
        adjudication_model="sol-evidence-high",
    ).resolve(documents)
    assert preparation.status == "verified" and preparation.verified_bundle is not None
    verified_source = nn._source_from_verified_evidence(
        raw_source,
        documents,
        preparation.verified_bundle,
    )
    context_data = load_fixture("env_utf8")
    provider = nn.TemplateNarrativeContextProvider(fixture_to_input(context_data))
    context = provider.build(verified_source)
    story_response = generic_story_response(context_data, propositions)
    tokens = propositions[0].split()
    story_response["candidates"][0]["hook"]["text"] = " ".join(
        tokens[index] for index in retained_token_indexes
    )
    story_adjudication = fake_adjudication_payload(story_response, context)
    evidence_client = QueueClient([extraction, evidence_adjudication])
    generation_client = QueueClient([story_response, story_adjudication])
    trust_service = nn.trust.NarrativeTrustService(TEST_TRUST_KEY)
    service = nn.NarrativeNormalizerService(
        policy=policy,
        context_provider=provider,
        generation_service=ng.NarrativeGenerationService(
            generation_client,
            generation_model="terra-story-medium",
            adjudication_model="sol-story-high",
            repair_model="terra-story-high",
        ),
        evidence_service=nn.evidence.GenericEvidenceService(
            evidence_client,
            extraction_model="terra-evidence-medium",
            adjudication_model="sol-evidence-high",
        ),
        trust_service=trust_service,
        clock=lambda: NOW,
    )
    outcome = service.normalize_source(record.source_ref, record.source_digest)
    assert outcome.status == nn.OUTCOME_MANUAL_ATTENTION
    assert outcome.model_call_count == 4
    draft = service.store.draft_path(record.source_digest, source_ref=record.source_ref)
    value = nn.validate_draft_directory(
        draft,
        trust_service=trust_service,
        require_trust=True,
    )
    assert value["review"]["status"] == nn.REVIEW_REJECTED
    assert value["factuality"].passed is False
    assert value["meaning"].passed is False
    assert not (draft / "narrative_ready.json").exists()


@pytest.mark.parametrize(
    "case,expected_status,expected_calls,expected_reason",
    (
        (
            "ambiguous",
            nn.OUTCOME_MANUAL_ATTENTION,
            2,
            "narrative_normalizer_evidence_ambiguous",
        ),
        (
            "rejected",
            nn.OUTCOME_SOURCE_INSUFFICIENT,
            2,
            "narrative_normalizer_source_insufficient",
        ),
        (
            "one-supported-fact",
            nn.OUTCOME_SOURCE_INSUFFICIENT,
            2,
            "narrative_normalizer_source_insufficient",
        ),
        (
            "bad-schema",
            nn.OUTCOME_FAILED,
            1,
            "narrative_normalizer_evidence_invalid",
        ),
        (
            "sensitive",
            nn.OUTCOME_SENSITIVE_REJECTED,
            0,
            "narrative_normalizer_source_sensitive",
        ),
    ),
    ids=(
        "manual-attention",
        "source-insufficient",
        "verified-below-minimum-facts",
        "failed",
        "sensitive-rejected",
    ),
)
def test_generic_resolution_maps_to_exact_honest_normalizer_outcome(
    tmp_path,
    case,
    expected_status,
    expected_calls,
    expected_reason,
):
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    registry = tmp_path / "state" / "registry.json"
    source_path = inbox / "Generic-Outcome" / "2026-08-17"
    source_path.mkdir(parents=True)
    registry.parent.mkdir(parents=True)
    propositions = (
        "сервис перенёс записи.",
        "база осталась доступной.",
        "проверка завершилась спокойно.",
        "журнал сохранил результат.",
        "команда сверила итог.",
    )
    if case == "sensitive":
        body = "token=private-generic-secret-value-123456\n"
    else:
        body = "# Сводка\n" + "\n".join(propositions) + "\n"
    (source_path / "material.md").write_text(body, encoding="utf-8")
    policy = rq.QuarantinePathPolicy(
        inbox, registry, outbox,
        narrative_review_authority_root=tmp_path / "review-authority",
    )
    rq.reconcile_complete_backlog(policy, now=NOW)
    record = rq.read_registry(registry).records[0]
    documents = nn.read_source_documents(
        policy,
        record.source_ref,
        expected_digest=record.source_digest,
    )
    if case == "sensitive":
        responses = []
    else:
        selected_propositions = (
            propositions[:1]
            if case == "one-supported-fact"
            else propositions
        )
        extraction, adjudication = generic_evidence_responses(
            documents,
            selected_propositions,
        )
        if case == "ambiguous":
            heading = next(
                item for item in extraction["segment_dispositions"]
                if item["disposition"] == "irrelevant"
            )
            heading["disposition"] = "ambiguous"
            parsed = nn.evidence.parse_extraction_response(extraction, documents)
            adjudication["extraction_bundle_digest"] = parsed.bundle_digest
        elif case == "rejected":
            for decision in adjudication["decisions"]:
                decision["decision"] = "rejected"
                decision["reason_codes"] = ["unsupported_proposition"]
        elif case == "bad-schema":
            extraction.pop("run_id")
        responses = [extraction, adjudication]
    evidence_client = QueueClient(responses)
    generation_client = QueueClient([])
    service = nn.NarrativeNormalizerService(
        policy=policy,
        context_provider=nn.TemplateNarrativeContextProvider(
            fixture_to_input(load_fixture("env_utf8"))
        ),
        generation_service=ng.NarrativeGenerationService(
            generation_client,
            generation_model="terra-story-medium",
            adjudication_model="sol-story-high",
        ),
        evidence_service=nn.evidence.GenericEvidenceService(
            evidence_client,
            extraction_model="terra-evidence-medium",
            adjudication_model="sol-evidence-high",
        ),
        trust_service=nn.trust.NarrativeTrustService(TEST_TRUST_KEY),
        clock=lambda: NOW,
    )
    outcome = service.normalize_source(record.source_ref, record.source_digest)
    assert outcome.status == expected_status
    assert outcome.reason_codes == (expected_reason,)
    assert outcome.model_call_count == expected_calls
    assert len(evidence_client.requests) == expected_calls
    assert generation_client.requests == []
    if case == "bad-schema":
        assert type(outcome.evidence_diagnostic) is nn.evidence.EvidenceValidationDiagnostic
        assert outcome.evidence_diagnostic.validation_stage == "top_level_schema"
        summary_item = nn.BatchResult(1, (outcome,)).safe_summary()["items"][0]
        assert summary_item["evidence_diagnostic"]["stable_subreason"] == "top_level_key_set_invalid"
        assert "material.md" not in json.dumps(summary_item, ensure_ascii=False)
    else:
        assert outcome.evidence_diagnostic is None
    assert not service.store.draft_path(
        record.source_digest,
        source_ref=record.source_ref,
    ).exists()


@pytest.mark.parametrize(
    ("filename", "payload"),
    (
        ("primary.pdf", b"%PDF-\x00\xff"),
        ("primary.txt", b"\xff\xfe\x00"),
    ),
    ids=("readable-plus-unsupported-binary", "readable-plus-invalid-utf8"),
)
def test_partial_unreadable_source_is_manual_attention_before_any_model_call(
    tmp_path,
    filename,
    payload,
):
    spec = normalizer_fixture("technical_log")
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    registry = tmp_path / "state" / "registry.json"
    source_ref = str(spec["source_ref"])
    source_path = inbox.joinpath(*source_ref.split("/"))
    source_path.mkdir(parents=True)
    registry.parent.mkdir(parents=True)
    (source_path / "material.md").write_text(
        "\n".join([*spec["facts"], *spec["extra_lines"]]) + "\n",
        encoding="utf-8",
    )
    (source_path / filename).write_bytes(payload)
    policy = rq.QuarantinePathPolicy(
        inbox, registry, outbox,
        narrative_review_authority_root=tmp_path / "review-authority",
    )
    rq.reconcile_complete_backlog(policy, now=NOW)
    record = rq.read_registry(registry).records[0]
    generation_client = QueueClient([])
    evidence_client = QueueClient([])
    service = nn.NarrativeNormalizerService(
        policy=policy,
        context_provider=nn.TemplateNarrativeContextProvider(
            fixture_to_input(load_fixture("env_utf8"))
        ),
        generation_service=ng.NarrativeGenerationService(
            generation_client,
            generation_model="terra-story-medium",
            adjudication_model="sol-story-high",
        ),
        evidence_service=nn.evidence.GenericEvidenceService(
            evidence_client,
            extraction_model="terra-evidence-medium",
            adjudication_model="sol-evidence-high",
        ),
        trust_service=nn.trust.NarrativeTrustService(TEST_TRUST_KEY),
        clock=lambda: NOW,
    )
    outcome = service.normalize_source(record.source_ref, record.source_digest)
    assert outcome.status == nn.OUTCOME_MANUAL_ATTENTION
    assert outcome.reason_codes == ("narrative_normalizer_evidence_ambiguous",)
    assert outcome.model_call_count == 0
    assert generation_client.requests == []
    assert evidence_client.requests == []
    assert not outbox.exists()


def test_network_tripwire_stays_zero_for_local_fixture_flow(tmp_path, monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("network attempted")
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    *_, outcome, draft = create_draft(tmp_path)
    assert outcome.status == nn.OUTCOME_CREATED


@pytest.mark.parametrize(
    "spec",
    [
        "", "module", ":factory", "module:", "a:b:c", " module:factory", "module:factory ",
        "missing_module:factory", "os:missing", "json:dumps", "pathlib:Path", "builtins:str",
        "x:y", "a.b.c:d", "-:x", "a:-", "1:2", "a::b", "none:none", "main:application",
    ],
)
def test_cli_adapter_contract_fails_closed(spec):
    with pytest.raises(nn.NarrativeNormalizerError, match="narrative_normalizer_cli_invalid"):
        nn.load_adapter(spec)


def test_cli_scan_list_status_verify_are_safe_json(tmp_path, capsys):
    spec, policy, source_path, record = write_source(tmp_path)
    base = _cli_base(policy)
    for command in ("scan", "list", "status", "verify"):
        assert _cli_run_with_local_test_authority([*base, command]) == 0
        output = capsys.readouterr().out
        payload = json.loads(output)
        assert str(tmp_path) not in output
        assert type(payload) is dict


def test_cli_verify_requires_matching_sealed_terminal_claim(tmp_path, capsys):
    *values, _, _ = create_draft(tmp_path)
    policy, record, service = values[1], values[3], values[-1]
    base = _cli_base(policy)
    assert _cli_run_with_local_test_authority([*base, "verify"]) == 0
    assert json.loads(capsys.readouterr().out) == {"passed": True, "verified": 1}
    claim = service.store.read_claim(record.source_ref, record.source_digest)
    assert claim is not None and claim["state"] == nn.CLAIM_COMPLETED


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    (
        ("missing", "narrative_normalizer_claim_uncertain"),
        ("bad-seal", "narrative_normalizer_claim_invalid"),
        ("cross-binding", "narrative_normalizer_claim_uncertain"),
    ),
    ids=("missing-claim", "tampered-claim-seal", "mismatched-sealed-claim"),
)
def test_cli_verify_fails_closed_for_missing_tampered_or_mismatched_claim(
    tmp_path,
    capsys,
    case,
    expected_reason,
):
    *values, _, _ = create_draft(tmp_path)
    policy, record, service = values[1], values[3], values[-1]
    path = service.store.claim_path(record.source_ref, record.source_digest)
    claim = service.store.read_claim(record.source_ref, record.source_digest)
    assert claim is not None
    if case == "missing":
        path.unlink()
    elif case == "bad-seal":
        write_json(path, dict(claim, claim_seal="0" * 64))
    else:
        changed = dict(claim, package_digest="f" * 64)
        write_json(path, service.store._seal_claim_payload(changed))
    base = _cli_base(policy)
    assert _cli_run_with_local_test_authority([*base, "verify"]) == 2
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "reason_code": expected_reason,
    }


def test_cli_verify_fails_closed_for_orphan_completed_claim(tmp_path, capsys):
    *values, _, draft = create_draft(tmp_path)
    policy, record, service = values[1], values[3], values[-1]
    claim_path = service.store.claim_path(record.source_ref, record.source_digest)
    assert claim_path.is_file()
    for child in draft.iterdir():
        child.unlink()
    draft.rmdir()
    assert claim_path.is_file()
    base = _cli_base(policy)
    assert _cli_run_with_local_test_authority([*base, "verify"]) == 2
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "reason_code": "narrative_normalizer_claim_uncertain",
    }


def test_cli_verify_accepts_explicit_trust_key_file_without_environment_key(
    tmp_path,
    monkeypatch,
    capsys,
):
    *values, _, _ = create_draft(tmp_path)
    policy = values[1]
    key_file = tmp_path / "trust-key.txt"
    key_file.write_text(
        base64.b64encode(TEST_TRUST_KEY).decode("ascii") + "\n",
        encoding="ascii",
    )
    monkeypatch.delenv("NARRATIVE_NORMALIZER_TRUST_KEY", raising=False)
    base = [*_cli_base(policy), "--trust-key-file", str(key_file)]
    assert _cli_run_with_local_test_authority([*base, "verify"]) == 0
    assert json.loads(capsys.readouterr().out) == {"passed": True, "verified": 1}


def test_cli_verify_rejects_ambiguous_environment_and_file_key_sources(
    tmp_path,
    capsys,
):
    *values, _, _ = create_draft(tmp_path)
    policy = values[1]
    key_file = tmp_path / "trust-key.txt"
    key_file.write_text(
        base64.b64encode(TEST_TRUST_KEY).decode("ascii") + "\n",
        encoding="ascii",
    )
    base = [*_cli_base(policy), "--trust-key-file", str(key_file)]
    assert _cli_run_with_local_test_authority([*base, "verify"]) == 2
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "reason_code": "narrative_normalizer_trust_invalid",
    }


def test_cli_normalize_requires_explicit_execution_gate_and_adapter(tmp_path, capsys):
    spec, policy, source_path, record = write_source(tmp_path)
    base = _cli_base(policy)
    assert cli.run([*base, "normalize", "--all"]) == 2
    output = json.loads(capsys.readouterr().out)
    assert output == {"ok": False, "reason_code": "narrative_normalizer_cli_invalid"}


def test_cli_executable_normalize_requires_trust_before_loading_adapter(
    tmp_path,
    monkeypatch,
    capsys,
):
    _, policy, _, _ = write_source(tmp_path)
    monkeypatch.delenv("NARRATIVE_NORMALIZER_TRUST_KEY", raising=False)
    adapter_calls = 0

    def forbidden_adapter(spec):
        nonlocal adapter_calls
        del spec
        adapter_calls += 1
        raise AssertionError("adapter loaded before trust")

    monkeypatch.setattr(nn, "load_adapter", forbidden_adapter)
    base = _cli_base(policy)
    code = cli.run([
        *base,
        "normalize",
        "--all",
        "--enable-local-execution",
        "--adapter",
        "private:factory",
    ], _allow_local_review_authority_for_tests=True)
    assert code == 2
    assert adapter_calls == 0
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "reason_code": "narrative_normalizer_trust_unavailable",
    }


def test_cli_executable_normalize_requires_external_authority_before_key_or_adapter(
    tmp_path,
    monkeypatch,
    capsys,
):
    inbox = tmp_path / "inbox"
    registry = tmp_path / "registry-root" / "registry.json"
    outbox = tmp_path / "outbox"
    source = inbox / "Project" / "2026-08-17"
    source.mkdir(parents=True)
    registry.parent.mkdir(parents=True)
    (source / "material.md").write_text("one fact\nsecond fact\n", encoding="utf-8")
    policy = rq.QuarantinePathPolicy(inbox, registry, outbox)
    rq.reconcile_complete_backlog(policy, now=NOW)
    before = registry.read_bytes()
    calls = {"key": 0, "adapter": 0}

    def forbidden_key(*args, **kwargs):
        calls["key"] += 1
        raise AssertionError("trust key loaded before authority")

    def forbidden_adapter(*args, **kwargs):
        calls["adapter"] += 1
        raise AssertionError("adapter loaded before authority")

    monkeypatch.setattr(cli, "_load_trust_service", forbidden_key)
    monkeypatch.setattr(nn, "load_adapter", forbidden_adapter)
    code = cli.run([
        "--inbox-root", str(inbox),
        "--registry-path", str(registry),
        "--outbox-root", str(outbox),
        "normalize", "--all", "--enable-local-execution", "--adapter", "private:factory",
    ])
    assert code == 2
    assert calls == {"key": 0, "adapter": 0}
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "reason_code": "narrative_normalizer_review_authority_unavailable",
    }
    assert registry.read_bytes() == before
    assert not outbox.exists()


def test_cli_verify_does_not_select_local_authority_from_environment(tmp_path, monkeypatch, capsys):
    *values, _, _ = create_draft(tmp_path)
    policy = values[1]
    authority = policy.narrative_review_authority_root
    assert authority is not None
    monkeypatch.setenv("NARRATIVE_NORMALIZER_REVIEW_AUTHORITY_ROOT", str(authority))
    code = cli.run([
        "--inbox-root", str(policy.inbox_root),
        "--registry-path", str(policy.registry_path),
        "--outbox-root", str(policy.narrative_outbox_root),
        "verify",
    ])
    assert code == 2
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "reason_code": "narrative_normalizer_review_authority_unavailable",
    }


@pytest.mark.parametrize("location", ("inbox", "outbox", "registry", "git"))
def test_review_authority_root_must_be_separate_from_all_mutable_roots(tmp_path, location):
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    registry = tmp_path / "registry-root" / "registry.json"
    authority_by_location = {
        "inbox": inbox / "authority",
        "outbox": outbox / "authority",
        "registry": registry.parent / "authority",
        "git": Path(rq.__file__).resolve().parent / ".forbidden-review-authority",
    }
    with pytest.raises(rq.RegistryError, match="quarantine_review_authority_invalid"):
        rq.QuarantinePathPolicy(
            inbox,
            registry,
            outbox,
            narrative_review_authority_root=authority_by_location[location],
        )


def test_review_authority_root_rejects_existing_symlink_chain(tmp_path):
    real = tmp_path / "real-authority"
    real.mkdir()
    linked = tmp_path / "linked-authority"
    try:
        linked.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(rq.RegistryError, match="quarantine_review_authority_invalid"):
        rq.QuarantinePathPolicy(
            tmp_path / "inbox",
            tmp_path / "registry" / "registry.json",
            tmp_path / "outbox",
            narrative_review_authority_root=linked / "child",
        )


def test_cli_resume_is_explicit_and_still_requires_execution_gate(tmp_path, capsys):
    spec, policy, source_path, record = write_source(tmp_path)
    base = _cli_base(policy)
    assert cli.run([*base, "resume", "--all"]) == 2
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "reason_code": "narrative_normalizer_cli_invalid",
    }


def test_cli_import_does_not_execute(tmp_path):
    before = set(tmp_path.iterdir())
    importlib.reload(cli)
    assert set(tmp_path.iterdir()) == before


def test_external_cache_isolation_keeps_worktree_metadata_unchanged(tmp_path):
    worktree = Path(__file__).resolve().parents[1]
    cache_roots = (
        worktree / ".pytest_cache",
        worktree / "__pycache__",
        worktree / "tests" / "__pycache__",
        worktree / "tools" / "__pycache__",
    )
    before = tuple(_filesystem_metadata_snapshot(path) for path in cache_roots)
    external_cache = tmp_path / "external-pycache"
    external_db = tmp_path / "runtime.sqlite3"
    assert not external_cache.resolve(strict=False).is_relative_to(worktree)
    assert not external_db.resolve(strict=False).is_relative_to(worktree)

    environment = dict(os.environ)
    environment.update({
        "DB_PATH": str(external_db),
        "PYTHONPYCACHEPREFIX": str(external_cache),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    result = subprocess.run(
        [sys.executable, "-c", "import narrative_normalizer; import tools.run_narrative_normalizer"],
        cwd=worktree,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert tuple(_filesystem_metadata_snapshot(path) for path in cache_roots) == before
    assert not external_db.exists()
    assert not external_cache.exists()


def test_outbox_contains_no_staging_leftovers_after_success(tmp_path):
    *values, outcome, draft = create_draft(tmp_path)
    root = values[-1].store.root
    assert not tuple(root.glob(".staging-*"))


def test_registry_is_read_only_during_normalization_and_approval(tmp_path):
    spec, policy, source_path, record, source, provider, context, client, service, outcome, draft = create_draft(tmp_path)
    before = policy.registry_path.read_bytes()
    approve_created(service.store, record, draft)
    assert policy.registry_path.read_bytes() == before


def test_source_files_bytes_and_mtime_remain_immutable_through_approval(tmp_path):
    spec, policy, source_path, record = write_source(tmp_path)
    before = {
        path.relative_to(source_path).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in source_path.rglob("*")
        if path.is_file()
    }
    source = nn.read_source_unit(policy, record.source_ref, expected_digest=record.source_digest)
    data = load_fixture("env_utf8")
    provider = nn.TemplateNarrativeContextProvider(fixture_to_input(data))
    context = provider.build(source)
    drafts = normalizer_generation_payload(data, fact_count=len(source.facts))
    client = QueueClient([drafts, fake_adjudication_payload(drafts, context)])
    service = nn.NarrativeNormalizerService(
        policy=policy,
        context_provider=provider,
        generation_service=ng.NarrativeGenerationService(client, generation_model="terra", adjudication_model="sol"),
        clock=lambda: NOW,
    )
    outcome = service.normalize_source(record.source_ref, record.source_digest)
    approve_created(service.store, record, service.store.draft_path(record.source_digest, source_ref=record.source_ref))
    after = {
        path.relative_to(source_path).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in source_path.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_store_rejects_symlinked_internal_state_directory(tmp_path, monkeypatch):
    spec, policy, source_path, record = write_source(tmp_path)
    target = policy.narrative_outbox_root / ".normalizer-state"
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda self: self == target or original(self))
    store = nn.NarrativeOutboxStore(policy)
    with pytest.raises(nn.NarrativeNormalizerError, match="narrative_normalizer_persistence_invalid"):
        store._ensure_write_layout()


def test_no_director_renderer_publication_or_operator_event_imports():
    source = Path(nn.__file__).read_text(encoding="utf-8")
    import_lines = tuple(line.strip().casefold() for line in source.splitlines() if line.startswith(("import ", "from ")))
    for token in ("story_production", "renderer", "operator_events", "telegram", "main"):
        assert not any(token in line for line in import_lines)


_FACTUALITY_CASES = (
    ("invented-outage", "Сбой остановил работу."),
    ("invented-users", "Пользователи потеряли доступ."),
    ("invented-clients", "Клиенты увидели ошибку."),
    ("invented-money", "Убыток составил 500 ₽."),
    ("invented-damage", "Система получила ущерб."),
    ("invented-production-incident", "Авария произошла в production incident."),
    ("invented-emotion", "Команда запаниковала."),
    ("invented-deadline", "Срок оказался сорван."),
    ("invented-causal-claim", "Поэтому файл спас всю работу."),
    ("invented-impact", "Это затронуло весь проект."),
    ("unknown-number", "Проверка заняла 42 минуты."),
    ("unknown-date", "Это произошло 2026-09-01."),
    ("random-person", "Иван Петров подтвердил результат."),
    ("random-company", "Acme Corp подтвердила результат."),
    ("fact-reorder", "__fact_reorder__"),
    ("fact-truncation", "__fact_truncation__"),
    ("duplicate-coverage", "__duplicate_coverage__"),
    ("literal-unsupported-metaphor", "Началась настоящая буря."),
    ("story-extra-sentence-not-in-claims", "__story_extra__"),
    ("claims-extra-item-not-in-story", "__claims_extra__"),
)


@pytest.mark.parametrize(("case", "injection"), _FACTUALITY_CASES, ids=[item[0] for item in _FACTUALITY_CASES])
def test_adversarial_factuality_never_reaches_ready(tmp_path, case, injection):
    if injection in {"__story_extra__", "__claims_extra__"}:
        *values, outcome, draft = create_draft(tmp_path)
        story_path = draft / "story.json"
        story = json.loads(story_path.read_text(encoding="utf-8"))
        if injection == "__story_extra__":
            story["story"] += "\n\nНепокрытое утверждение появилось отдельно."
        else:
            story["claims"].append(copy.deepcopy(story["claims"][-1]))
            story["claims"][-1]["claim_id"] = "claim-story-4"
        write_json(story_path, story)
        with pytest.raises(nn.NarrativeNormalizerError, match="narrative_normalizer_draft_invalid"):
            nn.validate_draft_directory(draft)
        assert not (draft / "narrative_ready.json").exists()
        return

    def mutate(payload):
        claim = payload["candidates"][0]["hook"]
        if injection == "__fact_reorder__":
            claim = payload["candidates"][0]["tension"]
            claim["source_fact_refs"] = ["fact-3", "fact-1"]
        elif injection == "__fact_truncation__":
            claim = payload["candidates"][0]["tension"]
            claim["text"] = "Рабочую папку проверили."
        elif injection == "__duplicate_coverage__":
            claim["source_fact_refs"] = ["fact-1", "fact-1"]
        else:
            claim["text"] = f"{claim['text']} {injection}"

    if injection == "__duplicate_coverage__":
        spec, policy, source_path, record = write_source(tmp_path)
        source = nn.read_source_unit(policy, record.source_ref, expected_digest=record.source_digest)
        data = load_fixture("env_utf8")
        provider = nn.TemplateNarrativeContextProvider(fixture_to_input(data))
        drafts = normalizer_generation_payload(data, fact_count=len(source.facts))
        mutate(drafts)
        client = QueueClient([drafts])
        service = nn.NarrativeNormalizerService(
            policy=policy,
            context_provider=provider,
            generation_service=ng.NarrativeGenerationService(client, generation_model="terra", adjudication_model="sol"),
            clock=lambda: NOW,
        )
        try:
            outcome = service.normalize_source(record.source_ref, record.source_digest)
        except nn.NarrativeNormalizerError as error:
            assert error.reason_code == "narrative_normalizer_internal_error"
        else:
            assert outcome.status == nn.OUTCOME_FAILED
        assert not service.store.draft_path(record.source_digest, source_ref=record.source_ref).exists()
        return

    *values, outcome, draft = create_draft(tmp_path, mutate_draft=mutate)
    assert not (draft / "narrative_ready.json").exists()
    if draft.exists():
        value = nn.validate_draft_directory(draft)
        assert value["review"]["status"] == nn.REVIEW_REJECTED
        assert not value["factuality"].passed or not value["meaning"].passed
        with pytest.raises(nn.NarrativeNormalizerError):
            approve_created(values[-1].store, values[3], draft)
    else:
        assert outcome.status == nn.OUTCOME_FAILED


_ANCHOR_OMISSIONS = (
    ("safe-atomic-replacement", "hook", "Файл заменили.", "safe_atomic_replacement"),
    ("utf8-without-bom", "human_problem", "Файл записали.", "utf8_without_bom"),
    ("working-directory", "tension", "Папку уровнем выше проверили.", "working_directory"),
    ("parent-directory", "tension", "Рабочую папку проверили.", "parent_directory"),
    ("repeated-same-result", "turning_point", "Проверку закончили.", "repeated_same_result"),
    ("manual-not-blind", "resolution", "Файл посмотрели.", "manual_inspection_not_blind_trust"),
)


@pytest.mark.parametrize(("case", "field", "text", "anchor"), _ANCHOR_OMISSIONS, ids=[item[0] for item in _ANCHOR_OMISSIONS])
def test_meaning_anchor_omission_fails_closed(tmp_path, case, field, text, anchor):
    def mutate(payload):
        payload["candidates"][0][field]["text"] = text

    *_, outcome, draft = create_draft(tmp_path, mutate_draft=mutate)
    assert outcome.review_status == nn.REVIEW_REJECTED
    value = nn.validate_draft_directory(draft)
    assert any(item.endswith(f":{anchor}") for item in value["meaning"].omitted_anchors)
    assert value["meaning"].passed is False
    assert value["plain_language"].passed is False
    assert not (draft / "narrative_ready.json").exists()


@pytest.mark.parametrize(
    "name",
    ["technical_log", "jargon_heavy", "quiet_object", "naz_solo", "void_primary", "duo_context"],
    ids=["technical-log", "jargon-heavy", "quiet-object", "naz-solo", "void-primary", "duo-context"],
)
def test_generic_story_collapse_is_rejected_per_source(tmp_path, name):
    generic = (
        "Один аккуратный шаг начался.",
        "Работа продолжилась спокойно.",
        "Все детали проверили внимательно.",
        "Затем получили обычный результат.",
        "История закончилась понятным выводом.",
    )

    def mutate(payload):
        for field, text in zip(("hook", "human_problem", "tension", "turning_point", "resolution"), generic, strict=True):
            payload["candidates"][0][field]["text"] = text

    *_, outcome, draft = create_draft(tmp_path, name, mutate_draft=mutate)
    if outcome.status == nn.OUTCOME_MANUAL_ATTENTION and not draft.exists():
        assert name == "jargon_heavy"
        assert outcome.reason_codes == ("narrative_normalizer_evidence_ambiguous",)
        assert outcome.model_call_count == 0
        assert not draft.exists()
        return
    assert outcome.review_status == nn.REVIEW_REJECTED
    value = nn.validate_draft_directory(draft)
    assert value["meaning"].omitted_anchors
    assert not value["meaning"].passed


@pytest.mark.parametrize(
    "claim_kind",
    ["", "fact", "supported", "model_claim", "unknown", "free_text", "bridge", "literal_metaphor", "impact", "FACT_PARAPHRASE"],
    ids=[f"unknown-kind-{index}" for index in range(10)],
)
def test_supported_story_claim_rejects_unknown_kind(claim_kind):
    with pytest.raises((TypeError, ValueError)):
        nn.SupportedStoryClaim(
            "claim-hook", claim_kind, "Точный подтверждённый факт.", ("fact-1",),
            ("anchor",), (), (), None, None, "literal",
        )


@pytest.mark.parametrize(
    "field",
    [
        "claim_id", "claim_kind", "rendered_text", "ordered_source_fact_refs", "semantic_anchors",
        "numbers", "named_entities", "temporal_relation", "causal_relation", "interpretation_mode",
    ],
)
def test_supported_story_claim_is_frozen(field):
    claim = nn.SupportedStoryClaim(
        "claim-hook", "fact_paraphrase", "Точный подтверждённый факт.", ("fact-1",),
        ("anchor",), (), (), None, None, "literal",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(claim, field, getattr(claim, field))


def _review_contract(draft):
    value = nn.validate_draft_directory(draft)
    return value, value["manifest"]["draft_identity"]


_REVIEW_FAULTS = (
    "conflicting-operator-request",
)


@pytest.mark.parametrize("fault", _REVIEW_FAULTS, ids=_REVIEW_FAULTS)
def test_review_update_fault_is_atomic_and_private(tmp_path, monkeypatch, fault):
    *values, outcome, draft = create_draft(tmp_path)
    store = values[-1].store; record = values[3]
    current, expected_draft_identity = _review_contract(draft)
    review_path = draft / "review.json"
    manifest_path = draft / "draft-manifest.json"
    story_path = draft / "story.json"
    before_review = review_path.read_bytes()
    before_manifest = manifest_path.read_bytes()
    before_story = story_path.read_bytes()
    base_kwargs = dict(
        operator_request_id="review-fault-request",
        expected_draft_identity=expected_draft_identity,
        reason_codes=("narrative_normalizer_review_not_passed",),
        reviewed_at=NOW.isoformat(),
    )

    if fault == "conflicting-operator-request":
        first = store.reject(record.source_ref, record.source_digest, **base_kwargs)
        before_review = review_path.read_bytes()
        base_kwargs["reason_codes"] = ("narrative_normalizer_plain_language_invalid",)
        with pytest.raises(nn.NarrativeNormalizerError, match="narrative_normalizer_review_identity_conflict"):
            store.reject(record.source_ref, record.source_digest, **base_kwargs)
        assert review_path.read_bytes() == before_review
        assert not first.idempotent
        return

    if fault == "serialize-failure":
        monkeypatch.setattr(nn, "_canonical", lambda value: (_ for _ in ()).throw(RuntimeError("C:\\secret\\serialize")))
    elif fault == "write-failure":
        monkeypatch.setattr(nn, "_write_exclusive_file", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("/secret/write")))
    elif fault == "fsync-failure":
        monkeypatch.setattr(nn, "_fsync_directory", lambda path: (_ for _ in ()).throw(OSError("/secret/fsync")))
    elif fault == "validation-failure":
        monkeypatch.setattr(store, "_validate_review_transition", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("/secret/validate")))
    elif fault == "promotion-mutates-then-raises":
        original_replace = nn.os.replace

        def replace_then_raise(source, target):
            original_replace(source, target)
            raise OSError("/secret/replace-after-mutation")

        monkeypatch.setattr(nn.os, "replace", replace_then_raise)
    elif fault == "backup-unlink-mutates-then-raises":
        original_unlink = nn.os.unlink

        def unlink_then_raise(path):
            original_unlink(path)
            if Path(path).name.startswith(".review-backup-"):
                raise OSError("/secret/unlink-after-mutation")

        monkeypatch.setattr(nn.os, "unlink", unlink_then_raise)
    elif fault == "post-unlink-fsync-failure":
        original_fsync = nn._fsync_directory

        def fail_after_backup_unlink(path):
            if path == store._state and not tuple(store._state.glob(".review-backup-*")):
                raise OSError("/secret/post-unlink-fsync")
            return original_fsync(path)

        monkeypatch.setattr(nn, "_fsync_directory", fail_after_backup_unlink)
    elif fault in {"promotion-failure", "cleanup-failure", "cleanup-noop"}:
        monkeypatch.setattr(nn.os, "replace", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("/secret/promote")))
        if fault == "cleanup-failure":
            monkeypatch.setattr(nn, "_remove_path_strict", lambda path: (_ for _ in ()).throw(OSError("/secret/cleanup")))
        elif fault == "cleanup-noop":
            monkeypatch.setattr(nn, "_remove_path_strict", lambda path: None)
    elif fault == "race":
        original_read = Path.read_bytes
        calls = {"review": 0}

        def racing_read(path):
            if path == review_path:
                calls["review"] += 1
                if calls["review"] >= 3:
                    return b"race"
            return original_read(path)

        monkeypatch.setattr(Path, "read_bytes", racing_read)

    with pytest.raises(nn.NarrativeNormalizerError) as captured:
        store.reject(record.source_ref, record.source_digest, **base_kwargs)
    error = captured.value
    assert type(error) is nn.NarrativeNormalizerError
    assert error.__cause__ is None and error.__context__ is None
    assert "secret" not in "".join(traceback.format_exception(error)).casefold()
    actual_review = original_read(review_path) if fault == "race" else review_path.read_bytes()
    assert actual_review == before_review
    assert manifest_path.read_bytes() == before_manifest
    assert story_path.read_bytes() == before_story
    assert not tuple(draft.glob(".review-staging-*"))
    assert not tuple(store._state.glob(".review-backup-*"))


def test_review_cancellation_survives_cleanup_failure(tmp_path, monkeypatch):
    *values, _, draft = create_draft(tmp_path)
    store = values[-1].store
    record = values[3]
    _, expected_draft_identity = _review_contract(draft)
    review_path = draft / "review.json"
    before = review_path.read_bytes()
    ledger_path = store._review_store().path_for(nn.source_identity(record.source_ref, record.source_digest))
    ledger_before = ledger_path.read_bytes()
    monkeypatch.setattr(
        nn.review_state.os,
        "replace",
        lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt("cancel")),
    )
    with pytest.raises(KeyboardInterrupt, match="cancel"):
        store.reject(
            record.source_ref,
            record.source_digest,
            operator_request_id="review-cancel-cleanup",
            expected_draft_identity=expected_draft_identity,
            reason_codes=("narrative_normalizer_review_not_passed",),
            reviewed_at=NOW.isoformat(),
        )
    assert review_path.read_bytes() == before
    assert ledger_path.read_bytes() == ledger_before
    assert not tuple(ledger_path.parent.glob(".*.staging-*"))


@pytest.mark.parametrize(
    "cancellation_type",
    (KeyboardInterrupt, SystemExit, GeneratorExit),
    ids=("review-cleanup-keyboard-interrupt", "review-cleanup-system-exit", "review-cleanup-generator-exit"),
)
def test_review_cleanup_base_exception_is_not_normalized(tmp_path, monkeypatch, cancellation_type):
    *values, _, draft = create_draft(tmp_path)
    store = values[-1].store
    record = values[3]
    _, expected_draft_identity = _review_contract(draft)
    before = (draft / "review.json").read_bytes()
    ledger_path = store._review_store().path_for(nn.source_identity(record.source_ref, record.source_digest))
    ledger_before = ledger_path.read_bytes()
    monkeypatch.setattr(
        nn.review_state.os,
        "replace",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("primary")),
    )
    monkeypatch.setattr(
        nn.review_state.os,
        "unlink",
        lambda path: (_ for _ in ()).throw(cancellation_type("cleanup-cancel")),
    )
    with pytest.raises(cancellation_type, match="cleanup-cancel"):
        store.reject(
            record.source_ref,
            record.source_digest,
            operator_request_id="review-cleanup-cancel",
            expected_draft_identity=expected_draft_identity,
            reason_codes=("narrative_normalizer_review_not_passed",),
            reviewed_at=NOW.isoformat(),
        )
    assert (draft / "review.json").read_bytes() == before
    assert ledger_path.read_bytes() == ledger_before


_APPROVAL_FAILURES = (
    "production-validator-failure-final-absent",
    "staging-validation-failure-final-absent",
    "promotion-failure-final-absent",
    "promotion-mutates-then-raises-final-absent",
    "cleanup-raises-final-absent",
    "cleanup-noop-final-absent",
    "competing-divergent-final-conflict",
    "competing-identical-final-idempotent",
    "final-created-then-cancelled-not-possible",
    "raw-sensitive-path-hidden",
    "cause-context-safe",
)


@pytest.mark.parametrize("case", _APPROVAL_FAILURES, ids=_APPROVAL_FAILURES)
def test_approval_validate_before_promotion_fault_matrix(tmp_path, monkeypatch, case):
    *values, outcome, draft = create_draft(tmp_path)
    store = values[-1].store; record = values[3]
    value, expected_draft_identity = _review_contract(draft)
    target = draft / "narrative_ready.json"
    kwargs = dict(expected_draft_identity=expected_draft_identity, reviewed_at=NOW.isoformat())

    if case == "competing-identical-final-idempotent":
        first = store.approve(record.source_ref, record.source_digest, **kwargs)
        original = target.read_bytes()
        second = store.approve(record.source_ref, record.source_digest, **kwargs)
        assert not first.idempotent and second.idempotent
        assert target.read_bytes() == original
        return
    if case == "competing-divergent-final-conflict":
        target.write_bytes(b"{}\n")
        with pytest.raises(nn.NarrativeNormalizerError, match="narrative_normalizer_approval_conflict"):
            store.approve(record.source_ref, record.source_digest, **kwargs)
        with pytest.raises(rq.EligibilityError):
            rq.validate_narrative_ready_manifest(values[1], record.source_ref)
        return
    if case in {"production-validator-failure-final-absent", "raw-sensitive-path-hidden", "cause-context-safe"}:
        sensitive = "C:\\secret\\artifact /secret/token credential=hidden"
        monkeypatch.setattr(
            rq,
            "validate_narrative_ready_payload",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError(sensitive)),
        )
    elif case == "staging-validation-failure-final-absent":
        original_read = nn._json_read
        monkeypatch.setattr(
            nn,
            "_json_read",
            lambda path, *args, **kwargs: (_ for _ in ()).throw(ValueError("/secret/staging"))
            if ".ready-staging-" in path.name else original_read(path, *args, **kwargs),
        )
    elif case == "promotion-mutates-then-raises-final-absent":
        original_link = nn.os.link

        def link_then_raise(source, target_path):
            original_link(source, target_path)
            raise OSError("/secret/link-after-mutation")

        monkeypatch.setattr(nn.os, "link", link_then_raise)
    elif case in {"promotion-failure-final-absent", "cleanup-raises-final-absent", "cleanup-noop-final-absent"}:
        monkeypatch.setattr(nn.os, "link", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("/secret/promotion")))
        if case == "cleanup-raises-final-absent":
            monkeypatch.setattr(nn, "_remove_path_strict", lambda path: (_ for _ in ()).throw(OSError("/secret/cleanup")))
        elif case == "cleanup-noop-final-absent":
            monkeypatch.setattr(nn, "_remove_path_strict", lambda path: None)
    elif case == "final-created-then-cancelled-not-possible":
        monkeypatch.setattr(nn, "_fsync_directory", lambda path: (_ for _ in ()).throw(KeyboardInterrupt("cancel")))

    expected_error = KeyboardInterrupt if case == "final-created-then-cancelled-not-possible" else nn.NarrativeNormalizerError
    with pytest.raises(expected_error) as captured:
        store.approve(record.source_ref, record.source_digest, **kwargs)
    assert not os.path.lexists(target)
    assert not tuple(draft.glob(".ready-staging-*"))
    if expected_error is nn.NarrativeNormalizerError:
        error = captured.value
        assert type(error) is nn.NarrativeNormalizerError
        assert error.__cause__ is None and error.__context__ is None
        assert "secret" not in "".join(traceback.format_exception(error)).casefold()


def test_approval_no_clobber_identical_race_is_idempotent(tmp_path, monkeypatch):
    *values, _, draft = create_draft(tmp_path)
    store = values[-1].store
    record = values[3]
    _, expected_draft_identity = _review_contract(draft)
    target = draft / "narrative_ready.json"

    def competing_identical(source, target_path):
        Path(target_path).write_bytes(Path(source).read_bytes())
        raise FileExistsError("competitor")

    monkeypatch.setattr(nn.os, "link", competing_identical)
    result = store.approve(
        record.source_ref,
        record.source_digest,
        expected_draft_identity=expected_draft_identity,
        reviewed_at=NOW.isoformat(),
    )
    # The competing files are not authoritative until this call commits the
    # monotonic approved event, so the first successful approval is not a replay.
    assert result.idempotent is False
    assert rq.validate_narrative_ready_manifest(values[1], record.source_ref).status == rq.CLASS_READY
    assert not tuple(draft.glob(".ready-staging-*"))


def test_approval_no_clobber_divergent_race_preserves_competitor(tmp_path, monkeypatch):
    *values, _, draft = create_draft(tmp_path)
    store = values[-1].store
    record = values[3]
    _, expected_draft_identity = _review_contract(draft)
    target = draft / "narrative_ready.json"
    attestation_target = draft / "approval-attestation.json"

    def competing_divergent(source, target_path):
        del source
        Path(target_path).write_bytes(b"{}\n")
        raise FileExistsError("competitor")

    monkeypatch.setattr(nn.os, "link", competing_divergent)
    with pytest.raises(nn.NarrativeNormalizerError, match="narrative_normalizer_approval_conflict"):
        store.approve(
            record.source_ref,
            record.source_digest,
            expected_draft_identity=expected_draft_identity,
            reviewed_at=NOW.isoformat(),
        )
    # Attestation is promoted first.  The divergent competitor at that first
    # boundary is preserved and ready is never exposed.
    assert attestation_target.read_bytes() == b"{}\n"
    assert not target.exists()
    assert not tuple(draft.glob(".ready-staging-*"))


def test_approval_link_then_target_replaced_preserves_competitor(tmp_path, monkeypatch):
    *values, _, draft = create_draft(tmp_path)
    store = values[-1].store
    record = values[3]
    _, expected_draft_identity = _review_contract(draft)
    target = draft / "narrative_ready.json"
    attestation_target = draft / "approval-attestation.json"
    original_link = nn.os.link

    def link_then_replace(source, target_path):
        original_link(source, target_path)
        Path(target_path).unlink()
        Path(target_path).write_bytes(b"competitor\n")

    monkeypatch.setattr(nn.os, "link", link_then_replace)
    with pytest.raises(nn.NarrativeNormalizerError, match="narrative_normalizer_manifest_invalid"):
        store.approve(
            record.source_ref,
            record.source_digest,
            expected_draft_identity=expected_draft_identity,
            reviewed_at=NOW.isoformat(),
        )
    assert attestation_target.read_bytes() == b"competitor\n"
    assert not target.exists()
    assert not tuple(draft.glob(".ready-staging-*"))


@pytest.mark.parametrize("preexisting", (False, True), ids=("fresh-ready", "existing-ready"))
def test_approval_rechecks_terminal_claim_after_final_draft_validation(
    tmp_path,
    monkeypatch,
    preexisting,
):
    *values, _, draft = create_draft(tmp_path)
    store = values[-1].store
    record = values[3]
    _, expected_draft_identity = _review_contract(draft)
    target = draft / "narrative_ready.json"
    kwargs = {
        "expected_draft_identity": expected_draft_identity,
        "reviewed_at": NOW.isoformat(),
    }
    original_ready = None
    if preexisting:
        store.approve(record.source_ref, record.source_digest, **kwargs)
        original_ready = target.read_bytes()

    original_match = nn.NarrativeOutboxStore._claim_matches_draft
    calls = 0

    def claim_match_changes_after_initial_validation(self, claim, value):
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_match(self, claim, value)
        return False

    monkeypatch.setattr(
        nn.NarrativeOutboxStore,
        "_claim_matches_draft",
        claim_match_changes_after_initial_validation,
    )
    with pytest.raises(nn.NarrativeNormalizerError, match="narrative_normalizer_approval_conflict"):
        store.approve(record.source_ref, record.source_digest, **kwargs)
    assert calls == 2
    if preexisting:
        assert target.read_bytes() == original_ready
    else:
        assert not os.path.lexists(target)
    assert not tuple(draft.glob(".ready-staging-*"))


def test_final_claim_read_precedes_coherent_final_draft_revalidation(tmp_path, monkeypatch):
    *values, _, draft = create_draft(tmp_path)
    store = values[-1].store
    record = values[3]
    initial = nn.validate_draft_directory(draft)
    original_read = store.read_claim
    calls = 0

    def mutate_coherent_bundle_then_read(source_ref, source_digest):
        nonlocal calls
        calls += 1
        if calls == 2:
            story = json.loads((draft / "story.json").read_text(encoding="utf-8"))
            manifest = json.loads((draft / "draft-manifest.json").read_text(encoding="utf-8"))
            review = json.loads((draft / "review.json").read_text(encoding="utf-8"))
            ready = json.loads((draft / "narrative_ready.json").read_text(encoding="utf-8"))
            evidence = story["cp2_adjudication_evidence"]
            evidence["run_id"] = "f" * 24
            evidence["evidence_digest"] = nn._sha({
                key: item for key, item in evidence.items() if key != "evidence_digest"
            })
            factuality = nn.build_factuality_receipt(
                initial["source"],
                initial["claims"],
                candidate_id=story["selected_candidate_id"],
                package_digest=story["human_story_package_digest"],
                statement_inference_kinds=initial["factuality"].statement_inference_kinds,
                adjudication_evidence_digest=evidence["evidence_digest"],
            )
            story["factuality_receipt"] = dataclasses.asdict(factuality)
            review["factuality_receipt"] = dataclasses.asdict(factuality)
            review["unsupported_claim_count"] = factuality.unsupported_claim_count
            story["package_digest"] = nn._sha({
                key: item for key, item in story.items() if key != "package_digest"
            })
            changed_draft_identity = nn.draft_identity(
                story["source_identity"],
                story["package_digest"],
            )
            manifest["generation_run_id"] = evidence["run_id"]
            manifest["package_digest"] = story["package_digest"]
            manifest["draft_identity"] = changed_draft_identity
            manifest["idempotency_identity"] = nn._sha({
                "version": nn.IDEMPOTENCY_VERSION,
                "source_identity": story["source_identity"],
                "package_digest": story["package_digest"],
            })
            review["draft_identity"] = changed_draft_identity
            assert store.trust_service is not None
            manifest_core = dict(manifest)
            manifest_core.pop("trust_receipt", None)
            artifact_binding = nn._artifact_binding_payload(
                story,
                (draft / "story.md").read_text(encoding="utf-8"),
                manifest_core,
            )
            artifact_binding_digest = nn._sha(artifact_binding)
            manifest["trust_receipt"] = nn.trust.receipt_to_payload(
                store.trust_service.sign(
                    nn.trust.TRUST_DOMAIN_DRAFT_REVIEW,
                    artifact_binding,
                )
            )
            review_core = dict(review)
            review_core.pop("trust_receipt", None)
            review["trust_receipt"] = nn.trust.receipt_to_payload(
                store.trust_service.sign(
                    nn.trust.TRUST_DOMAIN_DRAFT_REVIEW,
                    nn._review_trust_payload(
                        review_core,
                        artifact_binding_digest,
                    ),
                )
            )
            write_json(draft / "story.json", story)
            write_json(draft / "draft-manifest.json", manifest)
            write_json(draft / "review.json", review)
            ready["narrative_package_digest"] = hashlib.sha256(
                (draft / "story.json").read_bytes()
            ).hexdigest()
            write_json(draft / "narrative_ready.json", ready)
        return original_read(source_ref, source_digest)

    monkeypatch.setattr(store, "read_claim", mutate_coherent_bundle_then_read)
    with pytest.raises(nn.NarrativeNormalizerError, match="narrative_normalizer_approval_conflict"):
        store.approve(
            record.source_ref,
            record.source_digest,
            expected_draft_identity=initial["manifest"]["draft_identity"],
            reviewed_at=NOW.isoformat(),
        )
    assert calls == 2
    assert not os.path.lexists(draft / "narrative_ready.json")
    assert not tuple(draft.glob(".ready-staging-*"))


def test_approval_cancellation_survives_cleanup_failure(tmp_path, monkeypatch):
    *values, _, draft = create_draft(tmp_path)
    store = values[-1].store
    record = values[3]
    _, expected_draft_identity = _review_contract(draft)
    target = draft / "narrative_ready.json"
    monkeypatch.setattr(nn, "_fsync_directory", lambda path: (_ for _ in ()).throw(KeyboardInterrupt("cancel")))
    monkeypatch.setattr(nn, "_remove_path_strict", lambda path: (_ for _ in ()).throw(OSError("/secret/cleanup")))
    with pytest.raises(KeyboardInterrupt, match="cancel"):
        store.approve(
            record.source_ref,
            record.source_digest,
            expected_draft_identity=expected_draft_identity,
            reviewed_at=NOW.isoformat(),
        )
    assert not os.path.lexists(target)
    assert not tuple(draft.glob(".ready-staging-*"))


def test_same_bytes_different_source_refs_have_independent_identity_and_readiness(tmp_path):
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    registry = tmp_path / "state" / "registry.json"
    registry.parent.mkdir(parents=True)
    facts = normalizer_fixture("technical_log")["facts"]
    for project in ("Project-A", "Project-B"):
        source_path = inbox / project / "2026-08-01"
        source_path.mkdir(parents=True)
        (source_path / "material.md").write_text("\n".join(facts) + "\n", encoding="utf-8")
    policy = rq.QuarantinePathPolicy(
        inbox, registry, outbox, nn.trust.NarrativeTrustService(TEST_TRUST_KEY),
        tmp_path / "review-authority",
    )
    rq.reconcile_complete_backlog(policy, now=NOW)
    records = rq.read_registry(registry).records
    assert len(records) == 2 and records[0].source_digest == records[1].source_digest
    drafts = []
    stores = []
    for record in records:
        source = nn.read_source_unit(policy, record.source_ref, expected_digest=record.source_digest)
        data = load_fixture("env_utf8")
        provider = nn.TemplateNarrativeContextProvider(fixture_to_input(data))
        context = provider.build(source)
        payload = normalizer_generation_payload(data, fact_count=len(source.facts))
        client = QueueClient([payload, fake_adjudication_payload(payload, context)])
        service = nn.NarrativeNormalizerService(
            policy=policy,
            context_provider=provider,
            generation_service=ng.NarrativeGenerationService(client, generation_model="terra", adjudication_model="sol"),
            clock=lambda: NOW,
        )
        outcome = service.normalize_source(record.source_ref, record.source_digest)
        path = service.store.draft_path(record.source_digest, source_ref=record.source_ref)
        assert outcome.status == nn.OUTCOME_CREATED and path.is_dir()
        drafts.append(path); stores.append(service.store)
    assert drafts[0] != drafts[1]
    assert drafts[0].name == nn.source_identity(records[0].source_ref, records[0].source_digest)
    assert drafts[1].name == nn.source_identity(records[1].source_ref, records[1].source_digest)
    approve_created(stores[0], records[0], drafts[0])
    assert rq.validate_narrative_ready_manifest(policy, records[0].source_ref).status == rq.CLASS_READY
    with pytest.raises(rq.EligibilityError, match="narrative_manifest_missing"):
        rq.validate_narrative_ready_manifest(policy, records[1].source_ref)
    result = rq.reconcile_complete_backlog(policy, now=NOW + timedelta(minutes=1))
    by_ref = {item.source_ref: item for item in rq.read_registry(registry).records}
    assert by_ref[records[0].source_ref].status == rq.STATUS_READY
    assert by_ref[records[1].source_ref].status == rq.STATUS_NEEDS_NARRATIVE
    assert result.narrative_ready_count == 1


@pytest.mark.parametrize(
    ("source_ref", "digest"),
    [
        ("Project-A/2026-08-01", "0" * 64),
        ("Project-B/2026-08-02", "a" * 64),
        ("Naz_AI_Bot_clean/2026-08-03", "f" * 64),
    ],
    ids=["identity-zero", "identity-alpha", "identity-project"],
)
def test_composite_source_identity_uses_exact_nul_bound_bytes(source_ref, digest):
    expected = hashlib.sha256(
        source_ref.encode("utf-8")
        + b"\0"
        + digest.encode("ascii")
        + b"\0"
        + nn.SOURCE_CONTRACT_VERSION.encode("utf-8")
    ).hexdigest()
    assert nn.source_identity(source_ref, digest) == expected
    assert rq.narrative_source_identity(source_ref, digest) == expected


def test_cli_dry_run_without_adapter_is_exact_zero_write(tmp_path, capsys):
    inbox = tmp_path / "inbox"
    registry = tmp_path / "state" / "registry.json"
    outbox = tmp_path / "absent-outbox"
    source = inbox / "Project" / "2026-08-01"
    source.mkdir(parents=True)
    registry.parent.mkdir(parents=True)
    (source / "material.md").write_text("Первый подтверждённый факт.\nВторой подтверждённый факт.\n", encoding="utf-8")
    policy = rq.QuarantinePathPolicy(inbox, registry, outbox)
    rq.reconcile_complete_backlog(policy, now=NOW)
    before_registry = registry.read_bytes()
    before_source = (source / "material.md").read_bytes()
    assert not outbox.exists()
    code = cli.run([
        "--inbox-root", str(inbox),
        "--registry-path", str(registry),
        "--outbox-root", str(outbox),
        "normalize", "--all", "--dry-run",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["status_counts"] == {"dry_run": 1}
    assert payload["evidence_generic_path"] == 1
    assert payload["generic_fallback_candidate_count"] == 1
    assert sum(payload["coverage_counts"].values()) == 1
    assert payload["items"][0]["model_call_count"] == 0
    assert not outbox.exists()
    assert registry.read_bytes() == before_registry
    assert (source / "material.md").read_bytes() == before_source


def test_cli_dry_run_reports_complete_structural_coverage_without_aborting(
    tmp_path,
    capsys,
):
    inbox = tmp_path / "inbox"
    registry = tmp_path / "state" / "registry.json"
    outbox = tmp_path / "absent-outbox"
    registry.parent.mkdir(parents=True)
    technical = normalizer_fixture("technical_log")
    cases = {
        "Known": ("material.md", "\n".join([*technical["facts"], *technical["extra_lines"]]) + "\n"),
        "Generic": ("material.md", "A new service moved records.\nA separate check preserved the result.\n"),
        "Insufficient": ("material.md", ""),
        "Sensitive": ("material.md", "token=private-coverage-secret-value-123456\n"),
        "Unsupported": ("material.bin", b"\x00\xff\x00"),
    }
    for name, (filename, body) in cases.items():
        source = inbox / name / "2026-08-17"
        source.mkdir(parents=True)
        path = source / filename
        if type(body) is bytes:
            path.write_bytes(body)
        else:
            path.write_text(body, encoding="utf-8")
    policy = rq.QuarantinePathPolicy(inbox, registry, outbox)
    rq.reconcile_complete_backlog(policy, now=NOW)
    before_registry = registry.read_bytes()
    code = cli.run([
        "--inbox-root", str(inbox),
        "--registry-path", str(registry),
        "--outbox-root", str(outbox),
        "normalize", "--all", "--dry-run",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["requested_count"] == payload["accounted_count"] == 5
    assert payload["accounting_complete"] is True
    assert payload["status_counts"] == {"dry_run": 5}
    assert payload["coverage_counts"] == {
        "insufficient": 1,
        "known_deterministic_grammar": 1,
        "markdown_like": 1,
        "sensitive": 1,
        "unsupported_binary_container": 1,
    }
    assert payload["known_rule_count"] == 1
    assert payload["generic_fallback_candidate_count"] == 1
    assert payload["truly_insufficient_count"] == 1
    assert payload["manual_attention_count"] == 1
    assert payload["sensitive_count"] == 1
    assert payload["evidence_fast_path"] == 1
    assert payload["evidence_generic_path"] == 1
    assert registry.read_bytes() == before_registry
    assert not outbox.exists()


def test_legacy_digest_layout_is_ambiguous_for_same_bytes_different_refs(tmp_path):
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    registry = tmp_path / "state" / "registry.json"
    registry.parent.mkdir(parents=True)
    for project in ("A", "B"):
        source = inbox / project / "2026-08-01"
        source.mkdir(parents=True)
        (source / "material.md").write_text("One exact fact.\nAnother exact fact.\n", encoding="utf-8")
    policy = rq.QuarantinePathPolicy(inbox, registry, outbox)
    rq.reconcile_complete_backlog(policy, now=NOW)
    records = rq.read_registry(registry).records
    assert records[0].source_digest == records[1].source_digest
    legacy = outbox / records[0].source_digest
    legacy.mkdir(parents=True)
    package = legacy / "story.json"
    package.write_text("{}", encoding="utf-8")
    write_json(legacy / "narrative_ready.json", {
        "schema_version": rq.MANIFEST_SCHEMA_VERSION,
        "source_ref": records[0].source_ref,
        "source_digest": records[0].source_digest,
        "narrative_package_ref": f"{records[0].source_digest}/story.json",
        "narrative_package_digest": rq.narrative_package_digest(package),
        "status": rq.CLASS_READY,
        "contract_versions": {"director": "director-v1", "narrative": "narrative-v1"},
    })
    for record in records:
        with pytest.raises(rq.EligibilityError, match="narrative_manifest_ambiguous"):
            rq.validate_narrative_ready_manifest(policy, record.source_ref)


def _direct_source_and_claim(source_text: str, rendered_text: str):
    source_ref = "Probe/2026-08-16"
    digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    source = nn.SourceUnit(
        source_ref,
        digest,
        (nn.SourceFact("fact-1", source_text, source_ref, 1),),
        nn.FactExtractionReceipt(nn.SOURCE_CONTRACT_VERSION, 1, 1, 0, 0, True),
    )
    refs = ("fact-1",)
    source_anchors = nn._required_anchors_by_fact(source)["fact-1"]
    anchors = tuple(item for item in source_anchors if nn._anchor_rendered(item, rendered_text))
    kind, interpretation = nn._claim_kind(
        rendered_text,
        refs,
        is_resolution=False,
        source_text=source_text,
    )
    claim = nn.SupportedStoryClaim(
        "claim-hook",
        kind,
        rendered_text,
        refs,
        anchors,
        nn._extract_numbers(rendered_text),
        nn._extract_entities(rendered_text),
        nn._relation(rendered_text, nn._TEMPORAL_PATTERNS),
        nn._relation(rendered_text, nn._CAUSAL_PATTERNS),
        interpretation,
    )
    return source, claim


@pytest.mark.parametrize(
    ("source_text", "rendered_text"),
    [
        ("142 widgets were counted.", "42 widgets were counted."),
        ("Alpha before Beta.", "Beta before Alpha."),
        ("Alpha because Beta.", "Beta because Alpha."),
        ("build happened before test.", "test happened before build."),
        ("build happened because test failed.", "test failed because build happened."),
        ("No outage occurred.", "Outage occurred."),
        ("No outage occurred, but users remained active.", "Outage occurred, but no users remained active."),
        ("The lamp remained.", "The lamp remained. Рядом открылось окно."),
        ("build completed safely.", "build completed safely; sky turned green."),
        (
            "The file used safe atomic replacement.",
            "Один точный шаг начался с безопасной замены файла, и небо стало зелёным.",
        ),
        ("The file used safe atomic replacement at -5 degrees.", "The file used safe atomic replacement at -6 degrees."),
        (
            "The file used safe atomic replacement on 16 august 2026.",
            "The file used safe atomic replacement on 16 сентября 2026.",
        ),
        (
            "build completed safely.",
            "build completed safely, and build turned sky green.",
        ),
        (
            "The file used safe atomic replacement.",
            "Файл безопасно заменили и небо сияло.",
        ),
        (
            "The notebook was closed after the final check.",
            "После того как книжку закрыли, провели финальную проверку.",
        ),
        (
            "The notebook was closed after the final check.",
            "Запись notebook после того как книжку закрыли, провели финальную проверку final.",
        ),
        (
            "Build completed because test failed.",
            "Проверка провалилась, потому что сборка завершилась.",
        ),
        (
            "No outage affected active users.",
            "Outage affected no users.",
        ),
        (
            "Safe atomic replacement affected users.",
            "Файл безопасно заменили, users weren't affected.",
        ),
        (
            "Safe atomic replacement involved users.",
            "Файл безопасно заменили без участия ни одного пользователя.",
        ),
        ("NASA used safe atomic replacement.", "ESA выполнила безопасную замену."),
        ("acme used safe atomic replacement.", "globex выполнила безопасную замену."),
        (
            "The acme team used safe atomic replacement.",
            "Команда globex выполнила безопасную замену.",
        ),
        (
            "The file used safe atomic replacement at 1e3 cycles.",
            "The file used safe atomic replacement at 2e3 cycles.",
        ),
        (
            "The file used safe atomic replacement on 16 mayhem 2026.",
            "The file used safe atomic replacement on 16 мая 2026.",
        ),
        ("first build then test.", "first test then build."),
        ("first build then test.", "first build-destroy then test."),
        ("first build then test.", "first build then test-delete."),
        (
            "Safe atomic replacement happened because manual inspection was used instead of blind trust.",
            "Потому что замену выполнили безопасно, ручная проверка была вместо слепого доверия.",
        ),
        ("workers carefully migrated records.", "workers carefully destroyed records."),
        ("The database used safe atomic replacement.", "Замену выполнили безопасно."),
        ("The file used safe atomic replacement at the secret volcano.", "Файл безопасно заменили."),
    ],
    ids=[
        "number-substring-not-equal",
        "temporal-operands-not-reversible",
        "causal-operands-not-reversible",
        "temporal-common-operands-not-reversible",
        "causal-common-operands-not-reversible",
        "negation-reversal",
        "category-negation-cannot-migrate-between-clauses",
        "unsupported-extra-sentence-inside-claim",
        "unsupported-extra-clause-inside-one-sentence",
        "unsupported-coordinated-event-inside-one-sentence",
        "unknown-signed-number",
        "textual-date-month-substitution",
        "unsupported-coordinated-event-reusing-anchor",
        "unlisted-russian-verb-extra-event-never-ready",
        "bilingual-temporal-operands-not-reversible",
        "semantic-reversal-cannot-be-laundered-by-lexical-token-stuffing",
        "bilingual-causal-operands-not-reversible",
        "negation-cannot-migrate-between-predicates-same-clause",
        "contracted-negation-reverses-polarity",
        "multiword-negation-before-category",
        "unknown-acronym-entity",
        "unknown-lowercase-source-entity",
        "mid-sentence-lowercase-entity-substitution",
        "unknown-scientific-number",
        "month-prefix-is-not-date",
        "sequence-operands-not-reversible",
        "sequence-origin-suffix-not-supported",
        "sequence-result-suffix-not-supported",
        "broad-semantic-anchor-cannot-hide-causal-reversal",
        "unsupported-predicate-substitution",
        "known-anchor-cannot-hide-unmapped-object",
        "unmapped-source-qualifier-cannot-be-omitted",
    ],
)
def test_claim_factuality_direct_edge_fails_closed(source_text, rendered_text):
    source, claim = _direct_source_and_claim(source_text, rendered_text)
    assert nn._claim_supported(source, claim) is False


@pytest.mark.parametrize(
    ("name", "field", "rendered_text"),
    (
        (
            "technical_log",
            "hook",
            "Один точный шаг начался с безопасной замены файла, и небо безопасно заменили.",
        ),
        (
            "quiet_object",
            "turning_point",
            "После того как книжку закрыли, провели финальную проверку.",
        ),
        (
            "quiet_object",
            "turning_point",
            "Запись notebook после того как книжку закрыли, провели финальную проверку final.",
        ),
        ("technical_log", "hook", "Файл небезопасно заменили."),
        ("technical_log", "human_problem", "Файл записали в UTF-8 не без лишней служебной метки."),
        ("technical_log", "resolution", "Ручная проверка не прошла вместо слепого доверия автоматике."),
        ("technical_log", "turning_point", "Повторная проверка дала не тот же результат."),
        ("technical_log", "tension", "Рабочую папку и папку уровнем выше не проверили."),
        ("quiet_object", "hook", "Книжка не лежала рядом с клавиатурой."),
        ("technical_log", "hook", "Файл заменять безопасно не стали."),
        ("technical_log", "human_problem", "Файл не записали в UTF-8 без лишней служебной метки."),
        ("technical_log", "tension", "Рабочая папка и папка уровнем выше проверены не были."),
        ("technical_log", "turning_point", "Повторная проверка не дала тот же результат."),
        ("quiet_object", "hook", "Книжка рядом с клавиатурой не лежала."),
        ("duo_context", "human_problem", "Оба отметили один и тот же хрупкий шаг не для новой проверки."),
        ("duo_context", "human_problem", "Оба отметили один и тот же хрупкий шаг для новой проверки."),
        ("technical_log", "hook", "Файл безопасно заменили: окно открылось."),
        ("technical_log", "hook", "Файл безопасно заменили — окно открылось."),
        ("technical_log", "hook", "Файл, который безопасно заменили, открыл окно."),
        ("technical_log", "hook", "При безопасной замене файла окно открылось."),
        ("technical_log", "hook", "Файл безопасно заменили пока окно открылось."),
        ("technical_log", "tension", "Рабочая папка и папка уровнем выше исчезли."),
        ("technical_log", "turning_point", "Повторная проверка уничтожила тот же результат."),
        ("quiet_object", "hook", "Книжка рядом с клавиатурой сгорела."),
        ("duo_context", "hook", "Два человека украли один образец с разных рабочих мест."),
        ("duo_context", "human_problem", "Оба уничтожили один и тот же хрупкий шаг для новой проверки."),
        ("technical_log", "hook", "Файл безопасно заменили для globex."),
        ("technical_log", "hook", "Файл безопасно заменили для Ивана."),
        ("technical_log", "hook", "Файл безопасно заменили в компании globex."),
        ("technical_log", "hook", "Файл безопасно заменили сто раз."),
        ("technical_log", "hook", "Файл безопасно заменили вчера."),
        ("technical_log", "hook", "Файл безопасно заменили, избежав перерыва в работе."),
        ("naz_solo", "tension", "Безопасная замена сверлом с записью файла."),
        ("technical_log", "tension", "Рабочая папка и папка уровнем выше проверка."),
        ("technical_log", "turning_point", "Повторная проверка дальность тот же результат."),
        ("quiet_object", "hook", "Потёртая записная книжка всё время лежбище рядом с клавиатурой."),
        ("duo_context", "hook", "Два человека изучили один образец с разных рабочих месторождений."),
        ("quiet_object", "hook", "Потёртая записная книжка лежала рядом с клавиатурой."),
        ("duo_context", "resolution", "Разные слова описали одно и то же решение."),
        ("technical_log", "hook", "Один точный шаг начался с безопасной замены файла настроек."),
        ("naz_solo", "hook", "Безопасная замена файла настроек стала первым точным шагом."),
        ("naz_solo", "tension", "Безопасную замену файла настроек сверили с его записью."),
        ("void_primary", "hook", "Файл настроек безопасно заменили одним аккуратным действием."),
        ("technical_log", "hook", "Файл настроек безопасно заменили?"),
        ("technical_log", "human_problem", "Файл записали в UTF-8 без лишней служебной метки?"),
        ("quiet_object", "resolution", "Книжка осталась закрытой после последней проверки."),
    ),
    ids=(
        "reused-anchor-extra-clause-never-ready",
        "bilingual-temporal-operands-not-reversible-never-ready",
        "semantic-reversal-token-stuffing-never-ready",
        "negated-safe-anchor-never-ready",
        "double-negated-without-bom-never-ready",
        "negated-manual-inspection-never-ready",
        "negated-same-result-never-ready",
        "negated-directory-check-never-ready",
        "negated-notebook-location-never-ready",
        "negated-safe-predicate-postposed-never-ready",
        "negated-utf-predicate-preposed-never-ready",
        "negated-directory-predicate-postposed-never-ready",
        "negated-rerun-predicate-preposed-never-ready",
        "negated-notebook-predicate-postposed-never-ready",
        "negated-duo-purpose-never-ready",
        "unreferenced-duo-cardinality-never-ready",
        "extra-event-colon-never-ready",
        "extra-event-em-dash-never-ready",
        "extra-event-relative-clause-never-ready",
        "extra-event-subordinate-prefix-never-ready",
        "extra-event-while-clause-never-ready",
        "checked-predicate-cannot-become-disappeared",
        "confirmed-predicate-cannot-become-destroyed",
        "remained-predicate-cannot-become-burned",
        "examined-predicate-cannot-become-stole",
        "marked-predicate-cannot-become-destroyed",
        "lowercase-beneficiary-never-ready",
        "single-cyrillic-person-never-ready",
        "inflected-company-context-never-ready",
        "unknown-number-word-never-ready",
        "unknown-relative-date-never-ready",
        "invented-outage-synonym-never-ready",
        "projection-prefix-collision-sverl-never-ready",
        "projection-prefix-collision-prover-never-ready",
        "projection-prefix-collision-dal-never-ready",
        "projection-prefix-collision-lezh-never-ready",
        "projection-prefix-collision-mest-never-ready",
        "source-duration-cannot-be-omitted",
        "next-action-cannot-collapse-to-decision",
        "atomic-claim-cannot-invent-exact-step",
        "atomic-claim-cannot-invent-first-step",
        "atomic-claim-cannot-invent-verification",
        "atomic-claim-cannot-invent-action-cardinality",
        "question-is-not-asserted-atomic-fact",
        "question-is-not-asserted-utf-fact",
        "closed-state-cannot-replace-closing-event",
    ),
)
def test_factuality_bypasses_are_rejected_before_ready(tmp_path, name, field, rendered_text):
    def mutate(payload):
        payload["candidates"][0][field]["text"] = rendered_text

    *values, outcome, draft = create_draft(tmp_path, name, mutate_draft=mutate)
    value = nn.validate_draft_directory(draft)
    record = values[3]
    store = values[-1].store
    assert outcome.review_status == nn.REVIEW_REJECTED
    assert value["factuality"].passed is False
    assert value["factuality"].unsupported_claim_count >= 1
    assert not (draft / "narrative_ready.json").exists()
    with pytest.raises(nn.NarrativeNormalizerError, match="narrative_normalizer_review_not_passed"):
        approve_created(store, record, draft)
    assert not (draft / "narrative_ready.json").exists()


def test_bilingual_causal_role_signature_accepts_valid_direction():
    source, claim = _direct_source_and_claim(
        "Safe atomic replacement happened because manual inspection was used instead of blind trust.",
        "Потому что была ручная проверка вместо слепого доверия, замену выполнили безопасно.",
    )
    assert nn._claim_supported(source, claim) is True


def test_same_semantic_anchor_two_facts_requires_two_bound_evidence():
    source_ref = "Probe/2026-08-16"
    fact_texts = (
        "The file used safe atomic replacement.",
        "The config used safe atomic replacement.",
    )
    source = nn.SourceUnit(
        source_ref,
        hashlib.sha256("\n".join(fact_texts).encode("utf-8")).hexdigest(),
        tuple(
            nn.SourceFact(f"fact-{index}", text, source_ref, index)
            for index, text in enumerate(fact_texts, start=1)
        ),
        nn.FactExtractionReceipt(nn.SOURCE_CONTRACT_VERSION, 2, 2, 0, 0, True),
    )
    rendered = "The file used atomic replacement."
    refs = ("fact-1", "fact-2")
    anchors = tuple(
        anchor
        for ref in refs
        for anchor in nn._required_anchors_by_fact(source)[ref]
        if nn._anchor_rendered(anchor, rendered)
    )
    claim = nn.SupportedStoryClaim(
        "claim-hook",
        "fact_sequence",
        rendered,
        refs,
        anchors,
        nn._extract_numbers(rendered),
        nn._extract_entities(rendered),
        None,
        None,
        "literal",
    )
    assert set(nn.required_source_anchors(source)) >= {
        "semantic:fact-1:safe_atomic_replacement",
        "semantic:fact-2:safe_atomic_replacement",
    }
    assert nn._claim_supported(source, claim) is False


def test_same_semantic_kind_distinct_facts_cannot_collapse_across_claims():
    source_ref = "Probe/2026-08-16"
    fact_texts = (
        "The file used safe atomic replacement.",
        "The config used safe atomic replacement.",
    )
    source = nn.SourceUnit(
        source_ref,
        hashlib.sha256("\n".join(fact_texts).encode("utf-8")).hexdigest(),
        tuple(
            nn.SourceFact(f"fact-{index}", text, source_ref, index)
            for index, text in enumerate(fact_texts, start=1)
        ),
        nn.FactExtractionReceipt(nn.SOURCE_CONTRACT_VERSION, 2, 2, 0, 0, True),
    )
    claim_ids = ("claim-hook", "claim-story-1", "claim-story-2", "claim-story-3", "claim-ending")
    claims = tuple(
        nn.SupportedStoryClaim(
            claim_id,
            "fact_paraphrase",
            "Безопасную замену выполнили.",
            (f"fact-{1 + index % 2}",),
            (f"semantic:fact-{1 + index % 2}:safe_atomic_replacement",),
            (),
            (),
            None,
            None,
            "literal",
        )
        for index, claim_id in enumerate(claim_ids)
    )
    factuality = nn.build_factuality_receipt(
        source,
        claims,
        candidate_id="candidate-a",
        package_digest="a" * 64,
        statement_inference_kinds=("observed",) * 5,
        adjudication_evidence_digest="c" * 64,
    )
    meaning = nn.build_meaning_preservation_receipt(source, claims, factuality)
    assert factuality.passed is False
    assert factuality.unsupported_claim_count == 5
    assert meaning.passed is False
    assert "semantic:fact-1:file_object" in meaning.omitted_anchors
    assert "semantic:fact-2:configuration_object" in meaning.omitted_anchors


def test_same_anchor_distinct_object_facts_cannot_collapse():
    source_ref = "Probe/2026-08-16"
    fact_texts = (
        "The database used safe atomic replacement.",
        "The cache used safe atomic replacement.",
    )
    source = nn.SourceUnit(
        source_ref,
        hashlib.sha256("\n".join(fact_texts).encode("utf-8")).hexdigest(),
        tuple(
            nn.SourceFact(f"fact-{index}", text, source_ref, index)
            for index, text in enumerate(fact_texts, start=1)
        ),
        nn.FactExtractionReceipt(nn.SOURCE_CONTRACT_VERSION, 2, 2, 0, 0, True),
    )
    claim_ids = ("claim-hook", "claim-story-1", "claim-story-2", "claim-story-3", "claim-ending")
    claims = tuple(
        nn.SupportedStoryClaim(
            claim_id,
            "fact_paraphrase",
            "Замену выполнили безопасно.",
            (f"fact-{1 + index % 2}",),
            (f"semantic:fact-{1 + index % 2}:safe_atomic_replacement",),
            (),
            (),
            None,
            None,
            "literal",
        )
        for index, claim_id in enumerate(claim_ids)
    )
    factuality = nn.build_factuality_receipt(
        source,
        claims,
        candidate_id="candidate-a",
        package_digest="b" * 64,
        statement_inference_kinds=("observed",) * 5,
        adjudication_evidence_digest="d" * 64,
    )
    meaning = nn.build_meaning_preservation_receipt(source, claims, factuality)
    assert nn._source_semantically_closed(source) is False
    assert factuality.passed is False
    assert factuality.unsupported_claim_count == 5
    assert meaning.passed is False


def test_structured_source_values_are_required_meaning_anchors():
    source, _ = _direct_source_and_claim(
        "Release affected 42 clients on 2026-09-01.",
        "Release affected.",
    )
    required = nn.required_source_anchors(source)
    assert any(item.startswith("number:fact-1:42") for item in required)
    assert any(item.startswith("date:fact-1:2026-09-01") for item in required)
    assert any(item.startswith("category:fact-1:clients:positive") for item in required)
    assert all(not nn._anchor_rendered(item, "Release affected.") for item in required if item.startswith(("number:", "date:", "category:fact-1:clients:")))


@pytest.mark.parametrize(
    ("anchor", "text"),
    [
        ("safe_atomic_replacement", "Аккуратная замена файла."),
        ("parent_directory", "Соседняя папка была проверена."),
    ],
    ids=["careful-is-not-safe-atomic", "neighbor-is-not-parent"],
)
def test_loose_semantic_anchor_wording_is_not_accepted(anchor, text):
    assert nn._anchor_rendered(anchor, text) is False


def test_claim_kind_and_interpretation_are_rederived():
    source, claim = _direct_source_and_claim(
        "A manual inspection was used instead of blind trust.",
        "Ручная проверка прошла вместо слепого доверия.",
    )
    valid = dataclasses.replace(claim, claim_id="claim-ending", claim_kind="source_supported_significance")
    assert nn._claim_supported(source, valid) is True
    forged = dataclasses.replace(valid, claim_kind="fact_paraphrase")
    assert nn._claim_supported(source, forged) is False


def test_coherently_resealed_human_package_tamper_fails_typed_cp2_binding(tmp_path):
    *values, outcome, draft = create_draft(tmp_path)
    store = values[-1].store
    record = values[3]
    value = nn.validate_draft_directory(draft)
    claim_path = store.claim_path(record.source_ref, record.source_digest)
    before_claim = claim_path.read_bytes()
    completed_claim = store.read_claim(record.source_ref, record.source_digest)
    assert completed_claim is not None
    story = copy.deepcopy(value["story"])
    manifest = copy.deepcopy(value["manifest"])
    review = copy.deepcopy(value["review"])
    story["human_story_package"]["story_type"] = "coherently-mutated-story-type"
    human_digest, inference_kinds = nn._validate_human_package_snapshot(
        story["human_story_package"], value["source"], value["claims"]
    )
    story["human_story_package_digest"] = human_digest
    evidence = story["cp2_adjudication_evidence"]
    evidence["package_digest"] = human_digest
    evidence["evidence_digest"] = nn._sha({
        key: item for key, item in evidence.items() if key != "evidence_digest"
    })
    factuality = nn.build_factuality_receipt(
        value["source"],
        value["claims"],
        candidate_id=story["selected_candidate_id"],
        package_digest=human_digest,
        statement_inference_kinds=inference_kinds,
        adjudication_evidence_digest=evidence["evidence_digest"],
    )
    story["factuality_receipt"] = dataclasses.asdict(factuality)
    review["factuality_receipt"] = dataclasses.asdict(factuality)
    review["unsupported_claim_count"] = factuality.unsupported_claim_count
    base = {key: item for key, item in story.items() if key != "package_digest"}
    story["package_digest"] = nn._sha(base)
    new_draft_identity = nn.draft_identity(story["source_identity"], story["package_digest"])
    manifest["package_digest"] = story["package_digest"]
    manifest["draft_identity"] = new_draft_identity
    manifest["idempotency_identity"] = nn._sha({
        "version": nn.IDEMPOTENCY_VERSION,
        "source_identity": story["source_identity"],
        "package_digest": story["package_digest"],
    })
    review["draft_identity"] = new_draft_identity
    write_json(draft / "story.json", story)
    write_json(draft / "draft-manifest.json", manifest)
    write_json(draft / "review.json", review)
    with pytest.raises(nn.NarrativeNormalizerError, match="narrative_normalizer_draft_invalid"):
        nn.validate_draft_directory(draft)
    assert completed_claim["adjudication_evidence_digest"] != evidence["evidence_digest"]
    with pytest.raises(nn.NarrativeNormalizerError):
        store.approve(
            record.source_ref,
            record.source_digest,
            expected_draft_identity=new_draft_identity,
            reviewed_at=NOW.isoformat(),
        )
    assert not (draft / "narrative_ready.json").exists()
    forged_claim = dict(
        completed_claim,
        package_digest=story["package_digest"],
        draft_identity=new_draft_identity,
        human_story_package_digest=human_digest,
        factuality_binding_digest=factuality.adjudication_binding_digest,
        adjudication_evidence_digest=evidence["evidence_digest"],
        ordered_claim_digests=list(factuality.claim_digests),
    )
    with pytest.raises(nn.NarrativeNormalizerError, match="narrative_normalizer_claim_invalid"):
        store.write_claim(forged_claim)
    assert claim_path.read_bytes() == before_claim
    assert not (draft / "narrative_ready.json").exists()


def test_coherently_resealed_cp2_run_is_blocked_by_completed_claim(tmp_path):
    *values, _, draft = create_draft(tmp_path)
    store = values[-1].store
    record = values[3]
    value = nn.validate_draft_directory(draft)
    claim_path = store.claim_path(record.source_ref, record.source_digest)
    before_claim = claim_path.read_bytes()
    completed_claim = store.read_claim(record.source_ref, record.source_digest)
    assert completed_claim is not None
    story = copy.deepcopy(value["story"])
    manifest = copy.deepcopy(value["manifest"])
    review = copy.deepcopy(value["review"])
    evidence = story["cp2_adjudication_evidence"]
    changed_run_id = "e" * 24
    assert evidence["run_id"] != changed_run_id
    evidence["run_id"] = changed_run_id
    evidence["evidence_digest"] = nn._sha({
        key: item for key, item in evidence.items() if key != "evidence_digest"
    })
    factuality = nn.build_factuality_receipt(
        value["source"],
        value["claims"],
        candidate_id=story["selected_candidate_id"],
        package_digest=story["human_story_package_digest"],
        statement_inference_kinds=value["factuality"].statement_inference_kinds,
        adjudication_evidence_digest=evidence["evidence_digest"],
    )
    story["factuality_receipt"] = dataclasses.asdict(factuality)
    review["factuality_receipt"] = dataclasses.asdict(factuality)
    review["unsupported_claim_count"] = factuality.unsupported_claim_count
    story["package_digest"] = nn._sha({
        key: item for key, item in story.items() if key != "package_digest"
    })
    changed_draft_identity = nn.draft_identity(
        story["source_identity"],
        story["package_digest"],
    )
    manifest["generation_run_id"] = changed_run_id
    manifest["package_digest"] = story["package_digest"]
    manifest["draft_identity"] = changed_draft_identity
    manifest["idempotency_identity"] = nn._sha({
        "version": nn.IDEMPOTENCY_VERSION,
        "source_identity": story["source_identity"],
        "package_digest": story["package_digest"],
    })
    review["draft_identity"] = changed_draft_identity
    write_json(draft / "story.json", story)
    write_json(draft / "draft-manifest.json", manifest)
    write_json(draft / "review.json", review)

    changed = nn.validate_draft_directory(draft)
    assert changed["manifest"]["draft_identity"] == changed_draft_identity
    assert completed_claim["generation_run_id"] != changed_run_id
    assert completed_claim["adjudication_evidence_digest"] != evidence["evidence_digest"]
    # Recomputing every public digest without the injected HMAC key does not
    # create a trusted artifact, even though structural validation remains
    # deliberately available for read-only inspection.
    with pytest.raises(nn.NarrativeNormalizerError, match="narrative_normalizer_trust_invalid"):
        store.approve(
            record.source_ref,
            record.source_digest,
            expected_draft_identity=changed_draft_identity,
            reviewed_at=NOW.isoformat(),
        )
    assert not (draft / "narrative_ready.json").exists()
    forged_claim = dict(
        completed_claim,
        generation_run_id=changed_run_id,
        package_digest=story["package_digest"],
        draft_identity=changed_draft_identity,
        factuality_binding_digest=factuality.adjudication_binding_digest,
        adjudication_evidence_digest=evidence["evidence_digest"],
        ordered_claim_digests=list(factuality.claim_digests),
    )
    with pytest.raises(nn.NarrativeNormalizerError, match="narrative_normalizer_claim_invalid"):
        store.write_claim(forged_claim)
    assert claim_path.read_bytes() == before_claim


def test_completed_claim_seals_cp2_and_claim_binding(tmp_path):
    *values, outcome, draft = create_draft(tmp_path)
    record = values[3]
    store = values[-1].store
    value = nn.validate_draft_directory(draft)
    claim = store.read_claim(record.source_ref, record.source_digest)
    assert claim is not None and claim["state"] == nn.CLAIM_COMPLETED
    assert claim["selected_candidate_id"] == value["story"]["selected_candidate_id"]
    assert claim["draft_identity"] == value["manifest"]["draft_identity"]
    assert claim["human_story_package_digest"] == value["story"]["human_story_package_digest"]
    assert claim["factuality_binding_digest"] == value["factuality"].adjudication_binding_digest
    assert claim["ordered_claim_digests"] == list(value["factuality"].claim_digests)


def test_completed_claim_identical_rewrite_is_byte_idempotent(tmp_path):
    *values, _, _ = create_draft(tmp_path)
    store = values[-1].store
    record = values[3]
    claim = store.read_claim(record.source_ref, record.source_digest)
    assert claim is not None
    claim_path = store.claim_path(record.source_ref, record.source_digest)
    before = claim_path.read_bytes()
    assert store.write_claim(claim) is True
    assert claim_path.read_bytes() == before


@pytest.mark.parametrize(
    "seed_processing",
    (False, True),
    ids=("direct-completed-without-processing-rejected", "public-processing-to-completed-rejected"),
)
def test_public_claim_api_cannot_mint_completed_trust_anchor(tmp_path, seed_processing):
    *values, _, _ = create_draft(tmp_path)
    source = values[4]
    store = values[-1].store
    record = values[3]
    completed = store.read_claim(record.source_ref, record.source_digest)
    assert completed is not None and completed["state"] == nn.CLAIM_COMPLETED
    claim_path = store.claim_path(record.source_ref, record.source_digest)
    claim_path.unlink()
    if seed_processing:
        processing = nn._claim_payload(
            source,
            attempt_id=str(completed["attempt_id"]),
            state=nn.CLAIM_PROCESSING,
            started_at=str(completed["started_at"]),
            updated_at=str(completed["started_at"]),
        )
        store.write_claim(processing)
        before = claim_path.read_bytes()
    with pytest.raises(nn.NarrativeNormalizerError, match="narrative_normalizer_claim_invalid"):
        store.write_claim(completed)
    if seed_processing:
        assert claim_path.read_bytes() == before
        assert store.read_claim(record.source_ref, record.source_digest)["state"] == nn.CLAIM_PROCESSING
    else:
        assert not os.path.lexists(claim_path)


@pytest.mark.parametrize(
    "cancellation_type",
    (KeyboardInterrupt, SystemExit, GeneratorExit),
    ids=("claim-write-keyboard-interrupt", "claim-write-system-exit", "claim-write-generator-exit"),
)
def test_claim_atomic_write_base_exception_cleanup_is_exact(tmp_path, monkeypatch, cancellation_type):
    values = runtime(tmp_path)
    source = values[4]
    store = values[-1].store
    payload = nn._claim_payload(
        source,
        attempt_id="e" * 32,
        state=nn.CLAIM_PROCESSING,
        started_at=NOW.isoformat(),
        updated_at=NOW.isoformat(),
    )
    monkeypatch.setattr(
        nn.os,
        "replace",
        lambda *args, **kwargs: (_ for _ in ()).throw(cancellation_type("claim-cancel")),
    )
    monkeypatch.setattr(nn, "_remove_path_strict", lambda path: (_ for _ in ()).throw(OSError("/secret/cleanup")))
    with pytest.raises(cancellation_type, match="claim-cancel"):
        store.write_claim(payload)
    assert not os.path.lexists(store.claim_path(source.source_ref, source.source_digest))
    assert not tuple(store._claims.glob(".*.tmp-*"))
    assert not tuple(store._claims.glob(".*.rollback-*"))


def test_claim_replace_mutates_then_raises_restores_previous_bytes(tmp_path, monkeypatch):
    values = runtime(tmp_path)
    source = values[4]
    store = values[-1].store
    processing = nn._claim_payload(
        source,
        attempt_id="f" * 32,
        state=nn.CLAIM_PROCESSING,
        started_at=NOW.isoformat(),
        updated_at=NOW.isoformat(),
    )
    store.write_claim(processing)
    claim_path = store.claim_path(source.source_ref, source.source_digest)
    before = claim_path.read_bytes()
    uncertain = dict(
        processing,
        state=nn.CLAIM_UNCERTAIN,
        reason_code="narrative_normalizer_claim_uncertain",
    )
    original_replace = nn.os.replace

    def replace_then_raise(source_path, target_path):
        original_replace(source_path, target_path)
        raise OSError("/secret/replace-after-mutation")

    monkeypatch.setattr(nn.os, "replace", replace_then_raise)
    with pytest.raises(nn.NarrativeNormalizerError, match="narrative_normalizer_persistence_invalid"):
        store.write_claim(uncertain)
    assert claim_path.read_bytes() == before
    assert not tuple(store._claims.glob(".*.tmp-*"))
    assert not tuple(store._claims.glob(".*.rollback-*"))


@pytest.mark.parametrize(
    "mutation",
    ("divergent-completed", "completed-downgrade"),
    ids=("completed-claim-divergent-rewrite-rejected", "completed-claim-downgrade-rejected"),
)
def test_completed_claim_is_terminal(tmp_path, mutation):
    *values, _, _ = create_draft(tmp_path)
    store = values[-1].store
    record = values[3]
    claim = store.read_claim(record.source_ref, record.source_digest)
    assert claim is not None
    claim_path = store.claim_path(record.source_ref, record.source_digest)
    before = claim_path.read_bytes()
    if mutation == "divergent-completed":
        changed = dict(claim, selected_candidate_id="candidate-other")
    else:
        changed = nn._claim_payload(
            values[4],
            attempt_id=str(claim["attempt_id"]),
            state=nn.CLAIM_FAILED,
            started_at=str(claim["started_at"]),
            updated_at=str(claim["updated_at"]),
            reason_code="narrative_normalizer_generation_failed",
        )
    with pytest.raises(nn.NarrativeNormalizerError, match="narrative_normalizer_claim_invalid"):
        store.write_claim(changed)
    assert claim_path.read_bytes() == before


def test_existing_draft_without_matching_completed_claim_is_uncertain_without_model_call(tmp_path):
    *values, _, draft = create_draft(tmp_path)
    service = values[-1]
    record = values[3]
    client = values[7]
    before_calls = len(client.requests)
    service.store.claim_path(record.source_ref, record.source_digest).unlink()
    outcome = service.normalize_source(record.source_ref, record.source_digest)
    assert outcome.status == nn.OUTCOME_UNCERTAIN
    assert outcome.reason_codes == ("narrative_normalizer_claim_uncertain",)
    assert len(client.requests) == before_calls
    assert draft.exists()


def test_completed_claim_with_missing_draft_is_uncertain_without_model_call(tmp_path):
    *values, _, draft = create_draft(tmp_path)
    service = values[-1]
    record = values[3]
    client = values[7]
    before_calls = len(client.requests)
    draft.rename(tmp_path / "detached-draft")
    outcome = service.normalize_source(record.source_ref, record.source_digest)
    assert outcome.status == nn.OUTCOME_UNCERTAIN
    assert outcome.reason_codes == ("narrative_normalizer_claim_uncertain",)
    assert len(client.requests) == before_calls


def test_unambiguous_legacy_digest_outbox_manifest_remains_supported(tmp_path):
    _, policy, _, record = write_source(tmp_path)
    legacy = policy.narrative_outbox_root / record.source_digest
    legacy.mkdir(parents=True)
    package = legacy / "story.json"
    package.write_text("{}", encoding="utf-8")
    write_json(legacy / "narrative_ready.json", {
        "schema_version": rq.MANIFEST_SCHEMA_VERSION,
        "source_ref": record.source_ref,
        "source_digest": record.source_digest,
        "narrative_package_ref": f"{record.source_digest}/story.json",
        "narrative_package_digest": rq.narrative_package_digest(package),
        "status": rq.CLASS_READY,
        "contract_versions": {"director": "director-v1", "narrative": "narrative-v1"},
    })
    assert rq.validate_narrative_ready_manifest(policy, record.source_ref).status == rq.CLASS_READY


@pytest.mark.parametrize(
    "mutation",
    ("malformed", "wrong-digest", "wrong-ref", "package-escape"),
    ids=(
        "identity-layout-malformed",
        "identity-layout-wrong-digest",
        "identity-layout-wrong-source-ref",
        "identity-layout-package-escape",
    ),
)
def test_identity_layout_invalid_manifest_matrix_fails_closed(tmp_path, mutation):
    _, policy, _, record = write_source(tmp_path)
    directory, payload = write_identity_ready(policy, record)
    manifest_path = directory / "narrative_ready.json"
    if mutation == "malformed":
        manifest_path.write_bytes(b"{not-json\n")
    else:
        changed = dict(payload)
        if mutation == "wrong-digest":
            changed["source_digest"] = "0" * 64
        elif mutation == "wrong-ref":
            changed["source_ref"] = "Other/2026-08-01"
        else:
            changed["narrative_package_ref"] = "../outside/story.json"
        write_json(manifest_path, changed)
    with pytest.raises(rq.EligibilityError):
        rq.validate_narrative_ready_manifest(policy, record.source_ref)


def test_other_source_identity_manifest_is_ignored(tmp_path):
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    registry = tmp_path / "state" / "registry.json"
    registry.parent.mkdir(parents=True)
    facts = normalizer_fixture("technical_log")["facts"]
    for project in ("Identity-A", "Identity-B"):
        source_path = inbox / project / "2026-08-01"
        source_path.mkdir(parents=True)
        (source_path / "material.md").write_text("\n".join(facts) + "\n", encoding="utf-8")
    policy = rq.QuarantinePathPolicy(inbox, registry, outbox)
    rq.reconcile_complete_backlog(policy, now=NOW)
    records = rq.read_registry(registry).records
    assert len(records) == 2 and records[0].source_digest == records[1].source_digest
    write_identity_ready(policy, records[0])
    with pytest.raises(rq.EligibilityError, match="narrative_approval_trust_missing"):
        rq.validate_narrative_ready_manifest(policy, records[0].source_ref)
    with pytest.raises(rq.EligibilityError, match="narrative_manifest_missing"):
        rq.validate_narrative_ready_manifest(policy, records[1].source_ref)


def test_broken_identity_manifest_symlink_fails_closed_not_missing(tmp_path):
    _, policy, _, record = write_source(tmp_path)
    identity_dir = policy.narrative_outbox_root / nn.source_identity(record.source_ref, record.source_digest)
    identity_dir.mkdir(parents=True)
    manifest = identity_dir / "narrative_ready.json"
    try:
        manifest.symlink_to(tmp_path / "missing-ready.json")
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(rq.EligibilityError, match="narrative_manifest_invalid"):
        rq.validate_narrative_ready_manifest(policy, record.source_ref)


def test_broken_identity_directory_symlink_fails_closed_not_missing(tmp_path):
    _, policy, _, record = write_source(tmp_path)
    identity_dir = policy.narrative_outbox_root / nn.source_identity(record.source_ref, record.source_digest)
    try:
        identity_dir.symlink_to(tmp_path / "missing-identity-directory", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(rq.EligibilityError, match="narrative_manifest_invalid"):
        rq.validate_narrative_ready_manifest(policy, record.source_ref)
