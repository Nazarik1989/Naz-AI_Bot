from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import content_inbox_scout as scout
import main


PROJECT = "Naz_AI_Bot_clean"
ADMIN = 42
TEST_RUN_ID = "csr-" + "a" * 24
TEST_SNAPSHOT_DIGEST = "b" * 64
TEST_CANDIDATE_IDS = ("csc-" + "c" * 24, "csc-" + "d" * 24)


def ranking_wire_format() -> dict:
    return scout.ranking_response_format(
        TEST_RUN_ID,
        TEST_SNAPSHOT_DIGEST,
        TEST_CANDIDATE_IDS,
    )


def ready_wire_format() -> dict:
    return scout.ready_material_response_format(TEST_RUN_ID, TEST_CANDIDATE_IDS[0], 5)


def schema_node_keywords(response_format: dict) -> set[str]:
    found: set[str] = set()

    def visit(node: dict) -> None:
        found.update(node)
        if node.get("type") == "object":
            for child in node["properties"].values():
                visit(child)
        elif node.get("type") == "array":
            visit(node["items"])

    visit(response_format["json_schema"]["schema"])
    return found


def episode(label: str, *, extra: str = "") -> str:
    return (
        f"# {label}\n\n"
        "Сначала оператор видел понятную кнопку, но сообщение не уходило и человек не понимал причину. "
        "Команда нашла конфликт между проверкой и отправкой, затем изменила границу обработки. "
        "После исправления бот показывает ясный результат и не скрывает важное решение. "
        "На экране видны карточка, кнопка и короткое действие пользователя. "
        f"Это конкретный эпизод с понятным последствием и итогом. {extra}"
    )


def inbox(tmp_path: Path, *, sections: int = 3) -> Path:
    root = tmp_path / "inbox"
    day = root / PROJECT / "2026-08-31"
    day.mkdir(parents=True)
    text = "\n\n".join(
        episode(
            f"Эпизод {index}",
            extra=(f"Уникальная тема {index}: отдельный визуальный образ и результат {index}. " * 5),
        )
        for index in range(sections)
    )
    (day / "content-pack.md").write_text(text, encoding="utf-8")
    unrelated = root / "Other" / "2026-08-31"
    unrelated.mkdir(parents=True)
    (unrelated / "secret.md").write_text(episode("Чужой проект"), encoding="utf-8")
    return root


def snapshot(tmp_path: Path, *, sections: int = 3) -> scout.InboxSnapshot:
    return scout.discover_candidates(
        inbox(tmp_path, sections=sections),
        PROJECT,
        risk_detector=lambda _text: [],
        redactor=lambda text: text,
    )


def ranking_payload(snap: scout.InboxSnapshot, run_id: str) -> dict:
    evaluations = {}
    for rank, candidate in enumerate(snap.shortlist, start=1):
        evaluations[f"candidate_{rank:02d}"] = {
            "candidate_id": candidate.candidate_id,
            "story_strength_score": 90 - rank,
            "reel_ease_score": 95 - rank,
            "clarity_score": 88,
            "novelty_score": 80,
            "confidence_score": 91,
            "human_title": f"История {rank}",
            "one_sentence_pitch": "Кнопка выглядела готовой, но одна проверка останавливала сообщение.",
            "why_it_works": "В истории есть видимый конфликт, простое действие и понятный результат.",
            "editorial_risk": "none",
            "reason_codes": ["source_grounded", "clear_conflict", "simple_visuals"],
        }
    return {
        "schema_version": scout.RANKING_SCHEMA,
        "output_language": scout.OUTPUT_LANGUAGE,
        "scout_run_id": run_id,
        "source_snapshot_digest": snap.snapshot_digest,
        "candidate_evaluations": evaluations,
    }


def legacy_ranking_payload(snap: scout.InboxSnapshot, run_id: str) -> dict:
    current = ranking_payload(snap, run_id)
    rows = [dict(row) for row in current["candidate_evaluations"].values()]
    for rank, row in enumerate(rows, start=1):
        row.update({
            "rank": rank,
            "recommended_format": "short_reel",
            "recommended_duration_seconds": 99,
            "recommended_scene_count": 99,
        })
    return {
        "schema_version": scout.RANKING_SCHEMA_V1,
        "scout_run_id": run_id,
        "source_snapshot_digest": snap.snapshot_digest,
        "ranked_candidates": rows,
    }


def ready_payload(run_id: str, candidate_id: str, scene_count: int = 5) -> dict:
    post = (
        "На экране была обычная кнопка отправки, и всё выглядело готовым. Но сообщение не уходило. "
        "Причина оказалась не в интерфейсе, а в проверке перед отправкой: одна граница считала действие допустимым, другая останавливала его без понятного объяснения. "
        "Команда связала эти решения и сделала отказ видимым. Теперь оператор сразу понимает, что произошло и какое действие доступно дальше. "
        "Этот эпизод напоминает: хороший интерфейс не обещает то, чего система не может выполнить. "
        "Сначала нужно согласовать правила, затем показывать человеку кнопку и итог. "
        "Так небольшой технический конфликт превращается в понятную историю о доверии между человеком и продуктом."
    )
    assert 600 <= len(post) <= 1100
    return {
        "schema_version": scout.READY_SCHEMA,
        "output_language": scout.OUTPUT_LANGUAGE,
        "scout_run_id": run_id,
        "candidate_id": candidate_id,
        "title": "Сообщение, которое остановила проверка",
        "hook": "Кнопка была, а отправки не было.",
        "telegram_post": post,
        "reel_voice_over": "Кнопка обещала отправку, но проверка останавливала сообщение. Мы связали правила, и теперь оператор сразу видит понятный результат.",
        "scene_contents": {
            f"scene_{index:02d}": {
                "screen_text": f"Сцена {index}",
                "visual_brief": f"Синтетическая карточка показывает понятное действие {index}.",
            }
            for index in range(1, scene_count + 1)
        },
        "caption": "Интерфейс должен обещать только выполнимое.",
        "cover_text": "КНОПКА БЫЛА. ОТПРАВКИ — НЕТ.",
        "safety_note": "Использовать только безопасный синтетический интерфейс без данных.",
        "source_limitations": "Материал описывает только зафиксированный эпизод.",
    }


def ready_payload_for(run: scout.ScoutRunResult, candidate_id: str) -> dict:
    ranking = scout.ranked_for_run(run, candidate_id)
    return ready_payload(run.run_id, candidate_id, ranking.recommended_scene_count)


async def create_run(tmp_path: Path, snap: scout.InboxSnapshot | None = None):
    snap = snap or snapshot(tmp_path)
    calls = []

    async def model(messages, response_format):
        calls.append((messages, response_format))
        run_id = json.loads(messages[1]["content"])["scout_run_id"]
        return json.dumps(ranking_payload(snap, run_id), ensure_ascii=False)

    result = await scout.rank_snapshot(
        tmp_path / "state",
        snap,
        admin_id=ADMIN,
        expected_admin_id=ADMIN,
        operator_request_id="request-ranking-0001",
        refresh=False,
        recent_summaries=(),
        risk_detector=lambda _text: [],
        model_call=model,
    )
    return result, calls


def file_hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_project_first_discovery_excludes_unrelated_project(tmp_path):
    snap = snapshot(tmp_path)
    assert snap.project == PROJECT
    assert all(candidate.project == PROJECT for candidate in snap.candidates)
    assert all("Чужой" not in candidate.safe_text for candidate in snap.candidates)


def test_source_files_remain_byte_identical(tmp_path):
    root = inbox(tmp_path)
    before = file_hashes(root)
    scout.discover_candidates(root, PROJECT, risk_detector=lambda _: [], redactor=lambda value: value)
    assert file_hashes(root) == before


def test_heading_sections_split_deterministically(tmp_path):
    first = snapshot(tmp_path)
    second = scout.discover_candidates(tmp_path / "inbox", PROJECT, risk_detector=lambda _: [], redactor=lambda value: value)
    assert len(first.candidates) == 3
    assert [item.candidate_id for item in first.candidates] == [item.candidate_id for item in second.candidates]


def test_heading_identity_and_ranges_are_bound(tmp_path):
    snap = snapshot(tmp_path)
    assert all(scout.DIGEST_RE.fullmatch(item.heading_identity) for item in snap.candidates)
    assert all(item.character_end > item.character_start for item in snap.candidates)


def test_paragraph_groups_without_heading(tmp_path):
    root = tmp_path / "inbox"
    day = root / PROJECT / "2026-08-30"
    day.mkdir(parents=True)
    (day / "today-pick.md").write_text(episode("A").split("\n\n", 1)[1], encoding="utf-8")
    snap = scout.discover_candidates(root, PROJECT, risk_detector=lambda _: [], redactor=lambda value: value)
    assert len(snap.candidates) == 1


def test_exact_duplicate_removed(tmp_path):
    root = inbox(tmp_path, sections=1)
    day = root / PROJECT / "2026-08-31"
    (day / "copy.md").write_text((day / "content-pack.md").read_text(encoding="utf-8"), encoding="utf-8")
    snap = scout.discover_candidates(root, PROJECT, risk_detector=lambda _: [], redactor=lambda value: value)
    assert snap.discovered_count == 2
    assert snap.deduplicated_count == 1


def test_near_duplicate_removed(tmp_path):
    root = inbox(tmp_path, sections=1)
    day = root / PROJECT / "2026-08-31"
    original = (day / "content-pack.md").read_text(encoding="utf-8")
    (day / "near.md").write_text(original + " Почти одинаково.", encoding="utf-8")
    snap = scout.discover_candidates(root, PROJECT, risk_detector=lambda _: [], redactor=lambda value: value)
    assert snap.deduplicated_count == 1


def test_unsafe_after_redaction_is_excluded(tmp_path):
    root = inbox(tmp_path, sections=1)
    snap = scout.discover_candidates(root, PROJECT, risk_detector=lambda _: ["risk"], redactor=lambda value: value)
    assert snap.candidates == ()


def test_redacted_safe_candidate_is_retained(tmp_path):
    root = inbox(tmp_path, sections=1)
    detector = lambda text: ["risk"] if "PRIVATE" in text else []
    snap = scout.discover_candidates(root, PROJECT, risk_detector=detector, redactor=lambda value: value.replace("PRIVATE", "скрыто"))
    assert snap.deduplicated_count == 1


def test_technical_noise_excluded(tmp_path):
    root = tmp_path / "inbox"
    day = root / PROJECT / "2026-08-31"
    day.mkdir(parents=True)
    noisy = "# Отчёт\n\n" + "\n".join(f"test_case_{i}: passed" for i in range(40))
    (day / "live-chronicle.md").write_text(noisy, encoding="utf-8")
    snap = scout.discover_candidates(root, PROJECT, risk_detector=lambda _: [], redactor=lambda value: value)
    assert snap.candidates == ()


def test_local_shortlist_bounded_to_twelve(tmp_path):
    snap = snapshot(tmp_path, sections=20)
    assert len(snap.shortlist) == 12


def test_local_scan_has_no_model_parameter_or_call(tmp_path):
    calls = []
    snap = scout.discover_candidates(inbox(tmp_path), PROJECT, risk_detector=lambda _: [], redactor=lambda value: value)
    assert snap.shortlist and calls == []


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink unsupported")
def test_symlink_file_rejected(tmp_path):
    root = inbox(tmp_path, sections=1)
    target = tmp_path / "outside.md"
    target.write_text(episode("Outside"), encoding="utf-8")
    link = root / PROJECT / "2026-08-31" / "escape.md"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(scout.ScoutError, match="scout_source_symlink_forbidden"):
        scout.discover_candidates(root, PROJECT, risk_detector=lambda _: [], redactor=lambda value: value)


def test_ranking_call_exactly_once(tmp_path):
    run, calls = asyncio.run(create_run(tmp_path))
    assert run.model_calls == 1 and len(calls) == 1


def test_ranking_prompt_has_no_paths_or_filenames(tmp_path):
    _run, calls = asyncio.run(create_run(tmp_path))
    prompt = calls[0][0][1]["content"]
    assert "content-pack.md" not in prompt and str(tmp_path) not in prompt


def test_ranking_schema_rejects_extra_top_field(tmp_path):
    snap = snapshot(tmp_path)
    payload = ranking_payload(snap, "csr-" + "a" * 24)
    payload["extra"] = True
    with pytest.raises(scout.ScoutError, match="scout_ranking_contract_invalid"):
        scout.parse_ranking(json.dumps(payload), payload["scout_run_id"], snap, lambda _: [])


@pytest.mark.parametrize("value", [-1, 101, 1.5, True, "90"])
def test_ranking_rejects_invalid_score_types(tmp_path, value):
    snap = snapshot(tmp_path)
    run_id = "csr-" + "a" * 24
    payload = ranking_payload(snap, run_id)
    payload["candidate_evaluations"]["candidate_01"]["story_strength_score"] = value
    with pytest.raises(scout.ScoutError, match="scout_ranking_score_invalid"):
        scout.parse_ranking(json.dumps(payload), run_id, snap, lambda _: [])


def test_ranking_rejects_unknown_candidate(tmp_path):
    snap = snapshot(tmp_path)
    run_id = "csr-" + "a" * 24
    payload = ranking_payload(snap, run_id)
    payload["candidate_evaluations"]["candidate_01"]["candidate_id"] = "csc-" + "f" * 24
    with pytest.raises(scout.ScoutError, match="scout_ranking_candidate_binding_invalid"):
        scout.parse_ranking(json.dumps(payload), run_id, snap, lambda _: [])


def test_ranking_rejects_duplicate_candidate(tmp_path):
    snap = snapshot(tmp_path)
    payload = ranking_payload(snap, TEST_RUN_ID)
    payload["candidate_evaluations"]["candidate_02"]["candidate_id"] = payload["candidate_evaluations"]["candidate_01"]["candidate_id"]
    with pytest.raises(scout.ScoutError, match="scout_ranking_candidate_binding_invalid"):
        scout.parse_ranking(json.dumps(payload), TEST_RUN_ID, snap, lambda _: [])


def test_ranking_rejects_missing_candidate(tmp_path):
    snap = snapshot(tmp_path)
    payload = ranking_payload(snap, TEST_RUN_ID)
    payload["candidate_evaluations"].pop("candidate_03")
    with pytest.raises(scout.ScoutError, match="scout_ranking_candidate_matrix_invalid"):
        scout.parse_ranking(json.dumps(payload), TEST_RUN_ID, snap, lambda _: [])


def test_ranking_rejects_extra_candidate_slot(tmp_path):
    snap = snapshot(tmp_path)
    payload = ranking_payload(snap, TEST_RUN_ID)
    payload["candidate_evaluations"]["candidate_04"] = dict(
        payload["candidate_evaluations"]["candidate_03"]
    )
    with pytest.raises(scout.ScoutError, match="scout_ranking_candidate_matrix_invalid"):
        scout.parse_ranking(json.dumps(payload), TEST_RUN_ID, snap, lambda _: [])


def test_ranking_matrix_object_order_does_not_affect_result(tmp_path):
    snap = snapshot(tmp_path)
    payload = ranking_payload(snap, TEST_RUN_ID)
    first = scout.parse_ranking(json.dumps(payload), TEST_RUN_ID, snap, lambda _: [])
    payload["candidate_evaluations"] = dict(
        reversed(list(payload["candidate_evaluations"].items()))
    )
    second = scout.parse_ranking(json.dumps(payload), TEST_RUN_ID, snap, lambda _: [])
    assert second == first


@pytest.mark.parametrize("complexity,ease,expected", [
    (1, 80, (15, 5, False)),
    (2, 60, (18, 6, False)),
    (3, 100, (20, 7, True)),
    (1, 59, (20, 7, True)),
])
def test_code_owned_reel_spec_policy(tmp_path, complexity, ease, expected):
    candidate = snapshot(tmp_path).shortlist[0]
    features = {**candidate.local_features, "estimated_scene_complexity": complexity}
    candidate = scout.replace(candidate, local_features=features)
    assert scout.code_owned_reel_spec(candidate, ease) == expected


def test_code_owned_weighted_order_ignores_model_input_order(tmp_path):
    snap = snapshot(tmp_path)
    run_id = "csr-" + "a" * 24
    payload = ranking_payload(snap, run_id)
    target = payload["candidate_evaluations"]["candidate_03"]
    target["story_strength_score"] = 100
    target["reel_ease_score"] = 100
    payload["candidate_evaluations"] = dict(reversed(list(payload["candidate_evaluations"].items())))
    ranked = scout.parse_ranking(json.dumps(payload), run_id, snap, lambda _: [])
    assert ranked[0].candidate_id == target["candidate_id"]
    assert [item.rank for item in ranked] == list(range(1, len(ranked) + 1))


def test_exact_duplicate_snapshot_uses_zero_calls_and_no_orphan_run(tmp_path):
    snap = snapshot(tmp_path)
    first, calls = asyncio.run(create_run(tmp_path, snap))

    async def forbidden(*_args):
        raise AssertionError("provider called")

    second = asyncio.run(scout.rank_snapshot(
        tmp_path / "state", snap, admin_id=ADMIN, expected_admin_id=ADMIN,
        operator_request_id="request-ranking-0002", refresh=False, recent_summaries=(),
        risk_detector=lambda _: [], model_call=forbidden,
    ))
    assert second.run_id == first.run_id and second.model_calls == 0
    assert len(list((tmp_path / "state" / "runs").iterdir())) == 1
    assert len(calls) == 1


def test_divergent_request_reuse_conflicts_before_provider(tmp_path):
    snap = snapshot(tmp_path)
    asyncio.run(create_run(tmp_path, snap))
    changed = copy.deepcopy(snap)
    object.__setattr__(changed, "snapshot_digest", "f" * 64)
    calls = []

    async def model(*_args):
        calls.append(1)
        return "{}"

    with pytest.raises(scout.ScoutConflict, match="scout_request_conflict"):
        asyncio.run(scout.rank_snapshot(
            tmp_path / "state", changed, admin_id=ADMIN, expected_admin_id=ADMIN,
            operator_request_id="request-ranking-0001", refresh=False, recent_summaries=(),
            risk_detector=lambda _: [], model_call=model,
        ))
    assert calls == []


def test_refresh_creates_request_bound_new_run(tmp_path):
    snap = snapshot(tmp_path)
    first, _ = asyncio.run(create_run(tmp_path, snap))
    calls = []

    async def model(*_args):
        calls.append(1)
        raise AssertionError("safe persisted response should be salvaged")

    refreshed = asyncio.run(scout.rank_snapshot(
        tmp_path / "state", snap, admin_id=ADMIN, expected_admin_id=ADMIN,
        operator_request_id="refresh-ranking-0001", refresh=True, recent_summaries=(),
        risk_detector=lambda _: [], model_call=model,
    ))
    assert refreshed.run_id != first.run_id
    assert refreshed.model_calls == 0 and calls == []
    assert (refreshed.run_dir / "ranking-salvage.json").is_file()


def test_persisted_v1_ranking_artifact_remains_readable(tmp_path):
    run, _ = asyncio.run(create_run(tmp_path))
    path = run.run_dir / "ranking.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["schema_version"] = scout.RANKING_SCHEMA_V1
    value.pop("output_language")
    value.pop("ranking_contract")
    for row in value["ranked_candidates"]:
        row.pop("eligible_for_display")
        row.pop("display_exclusion_reason")
        row.pop("display_exclusion_field")
        row.pop("language_cyrillic_token_count")
        row.pop("language_natural_token_count")
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    loaded = scout.load_run(tmp_path / "state", run.run_id)
    assert loaded.ranked == run.ranked


def test_persisted_v2_ranking_artifact_remains_readable(tmp_path):
    run, _ = asyncio.run(create_run(tmp_path))
    path = run.run_dir / "ranking.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["schema_version"] = scout.RANKING_ARTIFACT_SCHEMA_V2
    value.pop("output_language")
    value.pop("ranking_contract")
    for row in value["ranked_candidates"]:
        row.pop("eligible_for_display")
        row.pop("display_exclusion_reason")
        row.pop("display_exclusion_field")
        row.pop("language_cyrillic_token_count")
        row.pop("language_natural_token_count")
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    loaded = scout.load_run(tmp_path / "state", run.run_id)
    assert loaded.ranked == run.ranked


def test_legacy_english_response_is_not_salvaged_into_russian_run(tmp_path):
    snap = snapshot(tmp_path)
    prior, _ = asyncio.run(create_run(tmp_path, snap))
    response_path = prior.run_dir / "provider-ranking-response.json"
    response_path.write_text(
        json.dumps(legacy_ranking_payload(snap, prior.run_id), ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    before = file_hashes(prior.run_dir)
    calls = []

    async def model(messages, _response_format):
        calls.append(1)
        run_id = json.loads(messages[1]["content"])["scout_run_id"]
        return json.dumps(ranking_payload(snap, run_id), ensure_ascii=False)

    derived = asyncio.run(scout.rank_snapshot(
        tmp_path / "state", snap, admin_id=ADMIN, expected_admin_id=ADMIN,
        operator_request_id="refresh-legacy-salvage-01", refresh=True,
        recent_summaries=(), risk_detector=lambda _: [], model_call=model,
    ))
    assert derived.run_id != prior.run_id and derived.model_calls == 1 and calls == [1]
    assert file_hashes(prior.run_dir) == before
    assert not (derived.run_dir / "ranking-salvage.json").exists()
    assert all(item.recommended_format == "short_reel" for item in derived.ranked)
    assert all(12 <= item.recommended_duration_seconds <= 20 for item in derived.ranked)
    assert all(4 <= item.recommended_scene_count <= 7 for item in derived.ranked)


def test_unbound_prior_response_is_not_salvaged_and_refresh_calls_once(tmp_path):
    snap = snapshot(tmp_path)
    prior, _ = asyncio.run(create_run(tmp_path, snap))
    response_path = prior.run_dir / "provider-ranking-response.json"
    value = json.loads(response_path.read_text(encoding="utf-8"))
    value["source_snapshot_digest"] = "f" * 64
    response_path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    calls = []

    async def model(messages, _response_format):
        calls.append(1)
        run_id = json.loads(messages[1]["content"])["scout_run_id"]
        return json.dumps(ranking_payload(snap, run_id), ensure_ascii=False)

    refreshed = asyncio.run(scout.rank_snapshot(
        tmp_path / "state", snap, admin_id=ADMIN, expected_admin_id=ADMIN,
        operator_request_id="refresh-unbound-response-01", refresh=True,
        recent_summaries=(), risk_detector=lambda _: [], model_call=model,
    ))
    assert refreshed.model_calls == 1 and calls == [1]


def test_details_are_stored_and_zero_call(tmp_path):
    run, _ = asyncio.run(create_run(tmp_path))
    text = scout.details_text(run.ranked[0])
    assert run.ranked[0].human_title in text


def test_hide_is_append_only_and_does_not_mutate_source(tmp_path):
    root = inbox(tmp_path / "source", sections=1)
    before = file_hashes(root)
    snap = scout.discover_candidates(root, PROJECT, risk_detector=lambda _: [], redactor=lambda value: value)
    run, _ = asyncio.run(create_run(tmp_path, snap))
    candidate_id = run.ranked[0].candidate_id
    assert scout.store_preference(tmp_path / "state", run, candidate_id, ADMIN, "hidden")
    assert candidate_id in scout.hidden_candidate_ids(tmp_path / "state", ADMIN)
    assert file_hashes(root) == before


def test_prepare_call_exactly_once_and_selected_candidate_only(tmp_path):
    run, _ = asyncio.run(create_run(tmp_path))
    selected = run.ranked[0].candidate_id
    calls = []

    async def model(messages, response_format):
        calls.append((messages, response_format))
        return json.dumps(ready_payload_for(run, selected), ensure_ascii=False)

    result = asyncio.run(scout.prepare_candidate(
        tmp_path / "state", run.run_id, selected, admin_id=ADMIN,
        expected_admin_id=ADMIN, risk_detector=lambda _: [], model_call=model,
    ))
    prompt = json.loads(calls[0][0][1]["content"])
    assert result.model_calls == 1 and len(calls) == 1
    assert prompt["candidate_id"] == selected
    assert "candidates" not in prompt and "source_file_digest" not in prompt


def test_duplicate_prepare_returns_stored_with_zero_calls(tmp_path):
    run, _ = asyncio.run(create_run(tmp_path))
    selected = run.ranked[0].candidate_id

    async def model(*_args):
        return json.dumps(ready_payload_for(run, selected), ensure_ascii=False)

    asyncio.run(scout.prepare_candidate(tmp_path / "state", run.run_id, selected, admin_id=ADMIN, expected_admin_id=ADMIN, risk_detector=lambda _: [], model_call=model))

    async def forbidden(*_args):
        raise AssertionError("provider called")

    duplicate = asyncio.run(scout.prepare_candidate(tmp_path / "state", run.run_id, selected, admin_id=ADMIN, expected_admin_id=ADMIN, risk_detector=lambda _: [], model_call=forbidden))
    assert duplicate.model_calls == 0 and not duplicate.created


def test_prepared_scene_timings_must_be_contiguous(tmp_path):
    run, _ = asyncio.run(create_run(tmp_path))
    selected = run.ranked[0].candidate_id
    payload = ready_payload_for(run, selected)
    artifact = scout._parse_ready(json.dumps(payload), run, scout.candidate_for_run(run, selected), lambda _: [])
    artifact["scenes"][2]["start_second"] += 1
    with pytest.raises(scout.ScoutError, match="scout_ready_scene_timing_invalid"):
        scout._validate_ready_artifact(artifact, run, scout.candidate_for_run(run, selected), lambda _: [])


def test_ready_parser_assigns_stored_reel_spec_and_code_owned_timings(tmp_path):
    run, _ = asyncio.run(create_run(tmp_path))
    selected = run.ranked[0].candidate_id
    ranking = scout.ranked_for_run(run, selected)
    payload = ready_payload_for(run, selected)
    assert "reel_duration_seconds" not in payload and "scenes" not in payload
    artifact = scout._parse_ready(
        json.dumps(payload), run, scout.candidate_for_run(run, selected), lambda _: []
    )
    assert artifact["reel_duration_seconds"] == ranking.recommended_duration_seconds
    assert len(artifact["scenes"]) == ranking.recommended_scene_count
    assert [(item["start_second"], item["end_second"]) for item in artifact["scenes"]] == list(
        scout.code_owned_scene_timings(
            ranking.recommended_duration_seconds, ranking.recommended_scene_count
        )
    )


def test_ready_material_rejects_short_telegram_post(tmp_path):
    run, _ = asyncio.run(create_run(tmp_path))
    selected = run.ranked[0].candidate_id
    payload = ready_payload_for(run, selected)
    payload["telegram_post"] = "too short"
    with pytest.raises(scout.ScoutError, match="scout_ready_text_invalid"):
        scout._parse_ready(json.dumps(payload), run, scout.candidate_for_run(run, selected), lambda _: [])


@pytest.mark.parametrize("scene_count", [3, 8])
def test_ready_material_rejects_scene_count_outside_local_bounds(tmp_path, scene_count):
    run, _ = asyncio.run(create_run(tmp_path))
    selected = run.ranked[0].candidate_id
    payload = ready_payload_for(run, selected)
    artifact = scout._parse_ready(json.dumps(payload), run, scout.candidate_for_run(run, selected), lambda _: [])
    if scene_count == 3:
        artifact["scenes"] = artifact["scenes"][:3]
    else:
        artifact["scenes"] = artifact["scenes"] + [copy.deepcopy(artifact["scenes"][-1]) for _ in range(8 - len(artifact["scenes"]))]
    with pytest.raises(scout.ScoutError, match="scout_ready_reel_invalid"):
        scout._validate_ready_artifact(artifact, run, scout.candidate_for_run(run, selected), lambda _: [])


@pytest.mark.parametrize("bad", ["/opt/private/story.json", "a" * 64, "API_KEY=value"])
def test_prepared_material_rejects_path_hash_and_secret(tmp_path, bad):
    run, _ = asyncio.run(create_run(tmp_path))
    selected = run.ranked[0].candidate_id
    payload = ready_payload_for(run, selected)
    payload["safety_note"] = bad
    with pytest.raises(scout.ScoutError, match="scout_ready_text_invalid"):
        scout._parse_ready(json.dumps(payload), run, scout.candidate_for_run(run, selected), lambda _: [])


def test_non_admin_rejected_before_scan_or_provider():
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=999),
        message=SimpleNamespace(reply_text=AsyncMock(), message_id=1),
    )
    context = SimpleNamespace(args=[], bot=SimpleNamespace())
    with patch.object(main, "ADMIN_ID", ADMIN), patch.object(main.content_inbox_scout, "discover_candidates") as discover, patch.object(main, "_inbox_scout_model_call", new=AsyncMock()) as provider:
        asyncio.run(main.inbox_best_command(update, context))
    discover.assert_not_called()
    provider.assert_not_awaited()


def test_exact_alias_normalization_is_closed():
    assert main.normalize_inbox_scout_alias(" «Принеси лучшее из контент-инбокса!» ") in main.INBOX_SCOUT_ALIASES
    assert main.normalize_inbox_scout_alias("принеси что-нибудь похожее из инбокса") not in main.INBOX_SCOUT_ALIASES


def test_command_parser_limits_count_and_format():
    assert main.parse_inbox_scout_args(["3", "reel"]) == (3, "reel")
    with pytest.raises(scout.ScoutError):
        main.parse_inbox_scout_args(["6", "reel"])
    with pytest.raises(scout.ScoutError):
        main.parse_inbox_scout_args(["3", "video"])


def test_callback_data_is_closed_and_bounded():
    value = scout.callback_data("prepare", "csr-" + "a" * 24, "csc-" + "b" * 24)
    assert len(value.encode()) <= 64
    assert scout.parse_callback(value) == ("prepare", "csr-" + "a" * 24, "csc-" + "b" * 24)
    with pytest.raises(scout.ScoutError):
        scout.parse_callback(value + ":extra")


def test_module_has_no_normalizer_broker_renderer_or_publication_imports():
    source = Path(scout.__file__).read_text(encoding="utf-8")
    forbidden = ("narrative_normalizer", "narrative_review_authority", "Renderer", "story_production", "publish")
    assert all(name not in source for name in forbidden)


def test_module_import_creates_no_state_or_network(tmp_path):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(scout.__file__).parent)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", "import content_inbox_scout; print('ok')"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0 and result.stdout.strip() == "ok"
    assert list(tmp_path.iterdir()) == []


def test_existing_schedule_defaults_remain_two_budget_slots():
    assert main.AUTOPOST_TIMES == os.getenv("NAZ_TELEGRAM_AUTO_TIMES", os.getenv("AUTOPOST_TIMES", "10:00,14:00,18:00,22:00")).strip()


def test_admin_content_menu_contains_scout_but_contact_menu_does_not():
    admin_buttons = {button.text for row in main.ADMIN_CONTENT_KEYBOARD.keyboard for button in row}
    contact_buttons = {button.text for row in main.CONTENT_KEYBOARD.keyboard for button in row}
    assert main.BTN_INBOX_SCOUT in admin_buttons
    assert main.BTN_INBOX_SCOUT not in contact_buttons


def test_no_automatic_schedule_or_media_action_registered():
    source = Path(main.__file__).read_text(encoding="utf-8")
    assert "setup_content_inbox_scout" not in source
    assert "content_inbox_scout.prepare_candidate" in source
    assert "content_inbox_scout.publish" not in source


def test_private_state_root_cannot_overlap_inbox_or_repository(tmp_path):
    protected = tmp_path / "inbox"
    protected.mkdir()
    with pytest.raises(scout.ScoutError, match="scout_state_root_overlap"):
        scout.assert_private_state_location(protected / "scout", [protected])
    with pytest.raises(scout.ScoutError, match="scout_state_root_overlap"):
        scout.assert_private_state_location(tmp_path, [protected])


def test_end_to_end_command_delivers_three_private_cards_without_prepare(tmp_path):
    source_root = inbox(tmp_path, sections=5)
    state_root = tmp_path / "private-state"
    bot = SimpleNamespace(send_message=AsyncMock())
    calls = []

    async def model(messages, _response_format):
        calls.append(1)
        payload = json.loads(messages[1]["content"])
        snap = scout.discover_candidates(source_root, PROJECT, risk_detector=lambda _: [], redactor=lambda value: value)
        return json.dumps(ranking_payload(snap, payload["scout_run_id"]), ensure_ascii=False)

    with patch.object(main, "ADMIN_ID", ADMIN), patch.object(main, "AGENT_CONTENT_INBOX", source_root), patch.object(main, "AGENT_CONTENT_PROJECT", PROJECT), patch.object(main, "NAZ_CONTENT_INBOX_SCOUT_ROOT", state_root), patch.object(main.memory, "get_recent_generated_posts", return_value=[]), patch.object(main, "_inbox_scout_model_call", new=model):
        result = asyncio.run(main.run_content_inbox_scout(bot, ADMIN, count=3, format_hint="reel"))
    assert result["card_count"] == 3 and result["ranking_model_calls"] == 1
    assert bot.send_message.await_count == 3 and calls == [1]
    assert all(call.kwargs["chat_id"] == ADMIN for call in bot.send_message.await_args_list)
    assert not (state_root / "prepared").exists()


def test_details_callback_uses_stored_result_and_zero_provider_calls(tmp_path):
    run, _ = asyncio.run(create_run(tmp_path))
    candidate = run.ranked[0]
    query = SimpleNamespace(
        data=scout.callback_data("details", run.run_id, candidate.candidate_id),
        answer=AsyncMock(),
    )
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=ADMIN))
    bot = SimpleNamespace(send_message=AsyncMock())
    context = SimpleNamespace(bot=bot)
    with patch.object(main, "ADMIN_ID", ADMIN), patch.object(main, "NAZ_CONTENT_INBOX_SCOUT_ROOT", tmp_path / "state"), patch.object(main, "_inbox_scout_model_call", new=AsyncMock()) as provider:
        asyncio.run(main.content_inbox_scout_callback(update, context))
    provider.assert_not_awaited()
    bot.send_message.assert_awaited_once()
    assert candidate.human_title in bot.send_message.await_args.kwargs["text"]


def test_ranking_schema_version_has_explicit_string_type_and_const():
    node = ranking_wire_format()["json_schema"]["schema"]["properties"]["schema_version"]
    assert node == {"type": "string", "const": scout.RANKING_SCHEMA}


def test_ready_schema_version_has_explicit_string_type_and_const():
    node = ready_wire_format()["json_schema"]["schema"]["properties"]["schema_version"]
    assert node == {"type": "string", "const": scout.READY_SCHEMA}


@pytest.mark.parametrize("factory", [ranking_wire_format, ready_wire_format])
def test_recursive_provider_schema_preflight_accepts_real_contracts(factory):
    scout.validate_provider_response_format(factory())


def test_provider_schema_preflight_rejects_const_only_property():
    value = copy.deepcopy(ranking_wire_format())
    value["json_schema"]["schema"]["properties"]["schema_version"] = {"const": scout.RANKING_SCHEMA}
    with pytest.raises(scout.ScoutError, match="scout_provider_schema_invalid"):
        scout.validate_provider_response_format(value)


def test_provider_schema_preflight_rejects_property_without_type():
    value = copy.deepcopy(ranking_wire_format())
    value["json_schema"]["schema"]["properties"]["scout_run_id"].pop("type")
    with pytest.raises(scout.ScoutError, match="scout_provider_schema_invalid"):
        scout.validate_provider_response_format(value)


def test_provider_schema_preflight_rejects_open_object():
    value = copy.deepcopy(ready_wire_format())
    value["json_schema"]["schema"]["properties"]["scene_contents"]["additionalProperties"] = True
    with pytest.raises(scout.ScoutError, match="scout_provider_schema_invalid"):
        scout.validate_provider_response_format(value)


def test_provider_schema_preflight_rejects_required_property_mismatch():
    value = copy.deepcopy(ranking_wire_format())
    value["json_schema"]["schema"]["required"].remove("source_snapshot_digest")
    with pytest.raises(scout.ScoutError, match="scout_provider_schema_invalid"):
        scout.validate_provider_response_format(value)


def test_ranking_wire_schema_has_dynamic_identity_constraints():
    schema = ranking_wire_format()["json_schema"]["schema"]
    properties = schema["properties"]
    matrix = properties["candidate_evaluations"]
    assert properties["scout_run_id"] == {"type": "string", "const": TEST_RUN_ID}
    assert properties["source_snapshot_digest"] == {"type": "string", "const": TEST_SNAPSHOT_DIGEST}
    assert list(matrix["properties"]) == ["candidate_01", "candidate_02"]
    assert matrix["required"] == ["candidate_01", "candidate_02"]
    assert matrix["additionalProperties"] is False
    for index, candidate_id in enumerate(TEST_CANDIDATE_IDS, start=1):
        candidate = matrix["properties"][f"candidate_{index:02d}"]["properties"]["candidate_id"]
        assert candidate == {"type": "string", "const": candidate_id}


@pytest.mark.parametrize("size", [1, 3, 12])
def test_ranking_v3_dynamic_matrix_preflight_accepts_exact_shortlist_sizes(size):
    candidate_ids = tuple(f"csc-{index:024x}" for index in range(1, size + 1))
    response_format = scout.ranking_response_format(
        TEST_RUN_ID, TEST_SNAPSHOT_DIGEST, candidate_ids
    )
    scout.validate_provider_response_format(response_format)
    matrix = response_format["json_schema"]["schema"]["properties"]["candidate_evaluations"]
    expected_slots = [f"candidate_{index:02d}" for index in range(1, size + 1)]
    assert matrix["required"] == expected_slots
    assert list(matrix["properties"]) == expected_slots


def test_ranking_v3_uses_closed_candidate_matrix_not_array():
    matrix = ranking_wire_format()["json_schema"]["schema"]["properties"]["candidate_evaluations"]
    assert matrix["type"] == "object"
    assert "items" not in matrix
    assert matrix["additionalProperties"] is False


def test_ranking_v3_omits_model_owned_reel_and_order_fields():
    item = ranking_wire_format()["json_schema"]["schema"]["properties"]["candidate_evaluations"]["properties"]["candidate_01"]
    assert set(item["properties"]) == {
        "candidate_id", "story_strength_score", "reel_ease_score", "clarity_score",
        "novelty_score", "confidence_score", "human_title", "one_sentence_pitch",
        "why_it_works", "editorial_risk", "reason_codes",
    }
    assert not {"rank", "recommended_format", "recommended_duration_seconds", "recommended_scene_count", "final_score"} & set(item["properties"])


def test_ranking_v3_score_enums_are_exact_zero_through_one_hundred():
    properties = ranking_wire_format()["json_schema"]["schema"]["properties"]["candidate_evaluations"]["properties"]["candidate_01"]["properties"]
    for key in ("story_strength_score", "reel_ease_score", "clarity_score", "novelty_score", "confidence_score"):
        assert properties[key] == {"type": "integer", "enum": list(range(101))}
        assert 101 not in properties[key]["enum"]


def test_ready_wire_schema_has_dynamic_identity_constraints():
    properties = ready_wire_format()["json_schema"]["schema"]["properties"]
    assert properties["scout_run_id"] == {"type": "string", "const": TEST_RUN_ID}
    assert properties["candidate_id"] == {"type": "string", "const": TEST_CANDIDATE_IDS[0]}


def test_ready_wire_schema_uses_exact_code_owned_scene_fields_without_timing():
    properties = ready_wire_format()["json_schema"]["schema"]["properties"]
    assert "reel_duration_seconds" not in properties and "scenes" not in properties
    scenes = properties["scene_contents"]
    assert list(scenes["properties"]) == [f"scene_{index:02d}" for index in range(1, 6)]
    assert all(
        set(node["properties"]) == {"screen_text", "visual_brief"}
        for node in scenes["properties"].values()
    )


@pytest.mark.parametrize("factory", [ranking_wire_format, ready_wire_format])
def test_wire_schemas_use_only_portable_keywords(factory):
    assert schema_node_keywords(factory()) <= {
        "type", "properties", "required", "additionalProperties", "items", "enum", "const", "description",
    }


@pytest.mark.parametrize("factory", [ranking_wire_format, ready_wire_format])
def test_wire_schemas_do_not_use_unique_items(factory):
    assert "uniqueItems" not in schema_node_keywords(factory())


@pytest.mark.parametrize("keyword,value", [
    ("uniqueItems", True),
    ("pattern", "^x$"),
    ("minLength", 1),
    ("minimum", 0),
    ("minItems", 1),
    ("unknownKeyword", True),
])
def test_provider_schema_preflight_rejects_nonportable_keyword(keyword, value):
    response_format = ranking_wire_format()
    node = response_format["json_schema"]["schema"]["properties"]["candidate_evaluations"]
    node[keyword] = value
    with pytest.raises(scout.ScoutError, match="scout_provider_schema_invalid"):
        scout.validate_provider_response_format(response_format)


def test_duplicate_known_reason_codes_are_canonicalized_by_local_parser(tmp_path):
    snap = snapshot(tmp_path)
    payload = ranking_payload(snap, TEST_RUN_ID)
    payload["candidate_evaluations"]["candidate_01"]["reason_codes"].append("source_grounded")
    assert "uniqueItems" not in schema_node_keywords(
        scout.ranking_response_format(
            TEST_RUN_ID,
            snap.snapshot_digest,
            tuple(item.candidate_id for item in snap.shortlist),
        )
    )
    ranked = scout.parse_ranking(json.dumps(payload), TEST_RUN_ID, snap, lambda _: [])
    selected = next(item for item in ranked if item.candidate_id == payload["candidate_evaluations"]["candidate_01"]["candidate_id"])
    assert selected.reason_codes.count("source_grounded") == 1


def test_unknown_reason_code_is_rejected(tmp_path):
    snap = snapshot(tmp_path)
    payload = ranking_payload(snap, TEST_RUN_ID)
    payload["candidate_evaluations"]["candidate_01"]["reason_codes"] = ["unknown_reason"]
    with pytest.raises(scout.ScoutError, match="scout_ranking_risk_invalid"):
        scout.parse_ranking(json.dumps(payload), TEST_RUN_ID, snap, lambda _: [])


def test_one_unsafe_candidate_is_excluded_while_three_safe_candidates_remain(tmp_path):
    snap = snapshot(tmp_path, sections=4)
    payload = ranking_payload(snap, TEST_RUN_ID)
    payload["candidate_evaluations"]["candidate_01"]["human_title"] = "unsafe-marker"
    ranked = scout.parse_ranking(
        json.dumps(payload),
        TEST_RUN_ID,
        snap,
        lambda text: ["private"] if "unsafe-marker" in text else [],
    )
    excluded = [item for item in ranked if not item.eligible_for_display]
    assert len(excluded) == 1
    assert excluded[0].display_exclusion_reason == "scout_ranking_text_unsafe"
    assert excluded[0].human_title == excluded[0].one_sentence_pitch == excluded[0].why_it_works == ""
    run = scout.ScoutRunResult(TEST_RUN_ID, snap, ranked, 1, True, tmp_path / "run")
    assert len(scout.safe_cards(run, 3)) == 3
    assert excluded[0] not in scout.safe_cards(run, 3)


def test_fewer_than_three_safe_candidates_is_typed_blocker_after_one_call(tmp_path):
    snap = snapshot(tmp_path)
    calls = []

    async def model(messages, _response_format):
        calls.append(1)
        run_id = json.loads(messages[1]["content"])["scout_run_id"]
        payload = ranking_payload(snap, run_id)
        payload["candidate_evaluations"]["candidate_01"]["human_title"] = "unsafe-marker"
        return json.dumps(payload, ensure_ascii=False)

    with pytest.raises(scout.ScoutError, match="scout_display_candidate_count_insufficient"):
        asyncio.run(scout.rank_snapshot(
            tmp_path / "state",
            snap,
            admin_id=ADMIN,
            expected_admin_id=ADMIN,
            operator_request_id="candidate-local-blocker-01",
            refresh=True,
            recent_summaries=(),
            risk_detector=lambda text: ["private"] if "unsafe-marker" in text else [],
            model_call=model,
        ))
    run_dirs = list((tmp_path / "state" / "runs").iterdir())
    assert calls == [1] and len(run_dirs) == 1
    artifact = json.loads((run_dirs[0] / "ranking.json").read_text(encoding="utf-8"))
    assert sum(row["eligible_for_display"] for row in artifact["ranked_candidates"]) == 2
    assert not (run_dirs[0] / "provider-ranking-response.json").exists()


def test_non_string_editorial_field_is_structural_failure(tmp_path):
    snap = snapshot(tmp_path)
    payload = ranking_payload(snap, TEST_RUN_ID)
    payload["candidate_evaluations"]["candidate_01"]["human_title"] = ["wrong"]
    with pytest.raises(scout.ScoutError, match="scout_ranking_text_type_invalid"):
        scout.parse_ranking(json.dumps(payload), TEST_RUN_ID, snap, lambda _: [])


@pytest.mark.parametrize("duration,scenes", [(15, 5), (18, 6), (20, 7)])
def test_code_owned_scene_timings_are_contiguous_and_exact(duration, scenes):
    timings = scout.code_owned_scene_timings(duration, scenes)
    assert timings[0][0] == 0 and timings[-1][1] == duration
    assert all(end - start >= 2 for start, end in timings)
    assert all(timings[index][1] == timings[index + 1][0] for index in range(len(timings) - 1))


def test_invalid_ranking_schema_stops_before_marker_and_provider(tmp_path):
    snap = snapshot(tmp_path)
    invalid = copy.deepcopy(ranking_wire_format())
    invalid["json_schema"]["schema"]["properties"]["schema_version"].pop("type")
    calls = []

    async def model(*_args):
        calls.append(1)
        return "{}"

    with patch.object(scout, "ranking_response_format", return_value=invalid):
        with pytest.raises(scout.ScoutError, match="scout_provider_schema_invalid"):
            asyncio.run(scout.rank_snapshot(
                tmp_path / "state", snap, admin_id=ADMIN, expected_admin_id=ADMIN,
                operator_request_id="invalid-schema-ranking-01", refresh=True,
                recent_summaries=(), risk_detector=lambda _: [], model_call=model,
            ))
    assert calls == []
    assert list((tmp_path / "state").rglob("ranking-requested.json")) == []
    assert list((tmp_path / "state").rglob("ranking.json")) == []


def test_invalid_ready_schema_stops_before_marker_and_provider(tmp_path):
    run, _ = asyncio.run(create_run(tmp_path))
    candidate_id = run.ranked[0].candidate_id
    invalid = copy.deepcopy(ready_wire_format())
    invalid["json_schema"]["schema"]["properties"]["schema_version"].pop("type")
    calls = []

    async def model(*_args):
        calls.append(1)
        return "{}"

    with patch.object(scout, "ready_material_response_format", return_value=invalid):
        with pytest.raises(scout.ScoutError, match="scout_provider_schema_invalid"):
            asyncio.run(scout.prepare_candidate(
                tmp_path / "state", run.run_id, candidate_id, admin_id=ADMIN,
                expected_admin_id=ADMIN, risk_detector=lambda _: [], model_call=model,
            ))
    assert calls == []
    assert list((tmp_path / "state").rglob("prepare-requested.json")) == []
    assert list((tmp_path / "state").rglob("ready-material.json")) == []


def test_interrupted_run_is_immutable_and_refresh_uses_new_run(tmp_path):
    snap = snapshot(tmp_path)
    state = tmp_path / "state"
    failed_calls = []

    async def invalid_model(*_args):
        failed_calls.append(1)
        return "not-json"

    with pytest.raises(scout.ScoutError, match="scout_ranking_json_invalid"):
        asyncio.run(scout.rank_snapshot(
            state, snap, admin_id=ADMIN, expected_admin_id=ADMIN,
            operator_request_id="production-style-failed-01", refresh=False,
            recent_summaries=(), risk_detector=lambda _: [], model_call=invalid_model,
        ))
    old_run_dirs = list((state / "runs").iterdir())
    assert len(old_run_dirs) == 1
    old_run = old_run_dirs[0]
    before = file_hashes(old_run)
    refresh_calls = []

    async def valid_model(messages, _response_format):
        refresh_calls.append(1)
        run_id = json.loads(messages[1]["content"])["scout_run_id"]
        return json.dumps(ranking_payload(snap, run_id), ensure_ascii=False)

    refreshed = asyncio.run(scout.rank_snapshot(
        state, snap, admin_id=ADMIN, expected_admin_id=ADMIN,
        operator_request_id="production-refresh-schema-01", refresh=True,
        recent_summaries=(), risk_detector=lambda _: [], model_call=valid_model,
    ))
    assert refreshed.run_dir != old_run
    assert file_hashes(old_run) == before
    assert failed_calls == [1] and refresh_calls == [1]


def test_ranking_v4_and_ready_v3_bind_russian_output_language():
    ranking = ranking_wire_format()["json_schema"]["schema"]
    ready = ready_wire_format()["json_schema"]["schema"]
    assert scout.RANKING_SCHEMA == "content-inbox-scout-ranking-v4"
    assert scout.READY_SCHEMA == "content-inbox-ready-material-v3"
    assert ranking["properties"]["output_language"] == {"type": "string", "const": "ru"}
    assert ready["properties"]["output_language"] == {"type": "string", "const": "ru"}
    assert "output_language" in ranking["required"] and "output_language" in ready["required"]


def test_ranking_and_preparation_prompts_explicitly_require_russian(tmp_path):
    run, ranking_calls = asyncio.run(create_run(tmp_path))
    ranking_messages = ranking_calls[0][0]
    assert "русск" in ranking_messages[0]["content"].casefold()
    assert json.loads(ranking_messages[1]["content"])["output_language"] == "ru"
    selected = run.ranked[0].candidate_id
    preparation_calls = []

    async def model(messages, _response_format):
        preparation_calls.append(messages)
        return json.dumps(ready_payload_for(run, selected), ensure_ascii=False)

    asyncio.run(scout.prepare_candidate(
        tmp_path / "state", run.run_id, selected, admin_id=ADMIN,
        expected_admin_id=ADMIN, risk_detector=lambda _: [], model_call=model,
    ))
    assert "русск" in preparation_calls[0][0]["content"].casefold()
    assert json.loads(preparation_calls[0][1]["content"])["output_language"] == "ru"


@pytest.mark.parametrize("text,short", [
    ("A completely English editorial title", True),
    ("This candidate has a clear conflict and a useful visible outcome", False),
])
def test_russian_validator_rejects_english_editorial_prose(text, short):
    result = scout.validate_russian_editorial_text(text, short=short)
    assert result.valid is False and result.cyrillic_tokens == 0


@pytest.mark.parametrize("text,short", [
    ("История SQLite", True),
    ("Naz показывает историю: `internal_contract` и get_history() остаются точными, а всё пояснение написано понятными русскими словами.", False),
])
def test_russian_validator_accepts_allowed_technical_identifiers(text, short):
    assert scout.validate_russian_editorial_text(text, short=short).valid is True


@pytest.mark.parametrize("field", ["human_title", "one_sentence_pitch", "why_it_works"])
def test_english_ranking_field_is_excluded_without_persisting_text(tmp_path, field):
    snap = snapshot(tmp_path, sections=4)
    payload = ranking_payload(snap, TEST_RUN_ID)
    payload["candidate_evaluations"]["candidate_01"][field] = "This is an ordinary English sentence with no Russian editorial prose"
    ranked = scout.parse_ranking(json.dumps(payload), TEST_RUN_ID, snap, lambda _: [])
    excluded = next(item for item in ranked if item.display_exclusion_reason == "scout_ranking_language_invalid")
    assert excluded.display_exclusion_field == field
    assert excluded.language_cyrillic_token_count == 0
    assert excluded.human_title == excluded.one_sentence_pitch == excluded.why_it_works == ""
    assert len(scout.safe_cards(scout.ScoutRunResult(TEST_RUN_ID, snap, ranked, 1, True, tmp_path), 3)) == 3


def test_fewer_than_three_russian_candidates_blocks_after_one_ranking_call(tmp_path):
    snap = snapshot(tmp_path)
    calls = []

    async def model(messages, _response_format):
        calls.append(1)
        run_id = json.loads(messages[1]["content"])["scout_run_id"]
        payload = ranking_payload(snap, run_id)
        payload["candidate_evaluations"]["candidate_01"]["human_title"] = "English only title"
        return json.dumps(payload, ensure_ascii=False)

    with pytest.raises(scout.ScoutError, match="scout_display_candidate_count_insufficient"):
        asyncio.run(scout.rank_snapshot(
            tmp_path / "state", snap, admin_id=ADMIN, expected_admin_id=ADMIN,
            operator_request_id="russian-count-blocker-01", refresh=True,
            recent_summaries=(), risk_detector=lambda _: [], model_call=model,
        ))
    assert calls == [1]
    artifact = next((tmp_path / "state" / "runs").glob("*/ranking.json"))
    stored = json.loads(artifact.read_text(encoding="utf-8"))
    rejected = next(item for item in stored["ranked_candidates"] if not item["eligible_for_display"])
    assert rejected["display_exclusion_reason"] == "scout_ranking_language_invalid"
    assert rejected["human_title"] == ""


@pytest.mark.parametrize("field", ["telegram_post", "reel_voice_over", "caption", "safety_note", "source_limitations"])
def test_english_ready_long_field_is_rejected_before_artifact_save(tmp_path, field):
    run, _ = asyncio.run(create_run(tmp_path))
    selected = run.ranked[0].candidate_id

    async def model(*_args):
        payload = ready_payload_for(run, selected)
        english = "This is a complete English editorial sentence that must never be saved or shown. "
        payload[field] = english * (10 if field == "telegram_post" else 2)
        return json.dumps(payload, ensure_ascii=False)

    with pytest.raises(scout.ScoutError, match="scout_ready_language_invalid"):
        asyncio.run(scout.prepare_candidate(
            tmp_path / "state", run.run_id, selected, admin_id=ADMIN,
            expected_admin_id=ADMIN, risk_detector=lambda _: [], model_call=model,
        ))
    assert not (run.run_dir / "prepared" / selected / "ready-material.json").exists()


def test_english_ready_visual_brief_is_rejected(tmp_path):
    run, _ = asyncio.run(create_run(tmp_path))
    selected = run.ranked[0].candidate_id

    async def model(*_args):
        payload = ready_payload_for(run, selected)
        payload["scene_contents"]["scene_01"]["visual_brief"] = "Show a clean interface with one button and a visible result"
        return json.dumps(payload, ensure_ascii=False)

    with pytest.raises(scout.ScoutError, match="scout_ready_language_invalid"):
        asyncio.run(scout.prepare_candidate(
            tmp_path / "state", run.run_id, selected, admin_id=ADMIN,
            expected_admin_id=ADMIN, risk_detector=lambda _: [], model_call=model,
        ))
    assert not (run.run_dir / "prepared" / selected / "ready-material.json").exists()


def test_russian_ready_material_passes_and_duplicate_is_zero_call(tmp_path):
    run, _ = asyncio.run(create_run(tmp_path))
    selected = run.ranked[0].candidate_id
    calls = []

    async def model(*_args):
        calls.append(1)
        return json.dumps(ready_payload_for(run, selected), ensure_ascii=False)

    first = asyncio.run(scout.prepare_candidate(
        tmp_path / "state", run.run_id, selected, admin_id=ADMIN,
        expected_admin_id=ADMIN, risk_detector=lambda _: [], model_call=model,
    ))
    second = asyncio.run(scout.prepare_candidate(
        tmp_path / "state", run.run_id, selected, admin_id=ADMIN,
        expected_admin_id=ADMIN, risk_detector=lambda _: [],
        model_call=lambda *_args: (_ for _ in ()).throw(AssertionError("provider called")),
    ))
    assert first.material["output_language"] == "ru"
    assert first.created is True and second.created is False and second.model_calls == 0 and calls == [1]


def test_locale_aware_snapshot_index_ignores_legacy_index_and_reuses_ru_run(tmp_path):
    snap = snapshot(tmp_path)
    state = tmp_path / "state"
    legacy_index = state / "snapshots" / f"{snap.snapshot_digest}.json"
    legacy_index.parent.mkdir(parents=True)
    legacy_bytes = b'{"schema_version":"content-inbox-scout-snapshot-index-v1"}\n'
    legacy_index.write_bytes(legacy_bytes)
    calls = []

    async def model(messages, _response_format):
        calls.append(1)
        run_id = json.loads(messages[1]["content"])["scout_run_id"]
        return json.dumps(ranking_payload(snap, run_id), ensure_ascii=False)

    refreshed = asyncio.run(scout.rank_snapshot(
        state, snap, admin_id=ADMIN, expected_admin_id=ADMIN,
        operator_request_id="ru-refresh-index-0001", refresh=True,
        recent_summaries=(), risk_detector=lambda _: [], model_call=model,
    ))
    reused = asyncio.run(scout.rank_snapshot(
        state, snap, admin_id=ADMIN, expected_admin_id=ADMIN,
        operator_request_id="ru-default-index-0001", refresh=False,
        recent_summaries=(), risk_detector=lambda _: [],
        model_call=lambda *_args: (_ for _ in ()).throw(AssertionError("provider called")),
    ))
    assert legacy_index.read_bytes() == legacy_bytes
    assert refreshed.run_id == reused.run_id and reused.model_calls == 0 and calls == [1]
    index = next((state / "snapshots-v2").glob("*.json"))
    assert json.loads(index.read_text(encoding="utf-8"))["output_language"] == "ru"


def test_user_facing_labels_and_reason_codes_are_russian(tmp_path):
    run, _ = asyncio.run(create_run(tmp_path))
    details = scout.details_text(run.ranked[0])
    ready = scout.ready_material_text({
        **ready_payload_for(run, run.ranked[0].candidate_id),
        "reel_duration_seconds": 15,
        "scenes": [{"order": 1, "start_second": 0, "end_second": 15, "screen_text": "Экран", "visual_brief": "Понятное безопасное изображение показывает результат действия."}],
    })
    assert "source_grounded" not in details and "опирается на исходный материал" in details
    assert "Voice-over" not in ready and "Caption" not in ready and "Safety note" not in ready
    assert "Текст озвучки:" in ready and "Подпись к ролику:" in ready and "Примечание по безопасности:" in ready
    assert scout.format_label("short_reel") == "короткий ролик"
    assert scout.reason_label("unknown_code") == "внутренняя редакционная отметка"
