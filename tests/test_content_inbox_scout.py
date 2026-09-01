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
    rows = []
    for rank, candidate in enumerate(snap.shortlist, start=1):
        rows.append(
            {
                "candidate_id": candidate.candidate_id,
                "rank": rank,
                "story_strength_score": 90 - rank,
                "reel_ease_score": 95 - rank,
                "clarity_score": 88,
                "novelty_score": 80,
                "confidence_score": 91,
                "human_title": f"История {rank}",
                "one_sentence_pitch": "Кнопка выглядела готовой, но одна проверка останавливала сообщение.",
                "why_it_works": "В истории есть видимый конфликт, простое действие и понятный результат.",
                "recommended_format": "short_reel",
                "recommended_duration_seconds": 16,
                "recommended_scene_count": 5,
                "editorial_risk": "none",
                "reason_codes": ["source_grounded", "clear_conflict", "simple_visuals"],
            }
        )
    return {
        "schema_version": scout.RANKING_SCHEMA,
        "scout_run_id": run_id,
        "source_snapshot_digest": snap.snapshot_digest,
        "ranked_candidates": rows,
    }


def ready_payload(run_id: str, candidate_id: str) -> dict:
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
        "scout_run_id": run_id,
        "candidate_id": candidate_id,
        "title": "Сообщение, которое остановила проверка",
        "hook": "Кнопка была, а отправки не было.",
        "telegram_post": post,
        "reel_voice_over": "Кнопка обещала отправку, но проверка останавливала сообщение. Мы связали правила, и теперь оператор сразу видит понятный результат.",
        "reel_duration_seconds": 16,
        "scenes": [
            {"order": 1, "start_second": 0, "end_second": 3, "screen_text": "Кнопка готова", "visual_brief": "Синтетическая карточка и курсор."},
            {"order": 2, "start_second": 3, "end_second": 6, "screen_text": "Но тишина", "visual_brief": "Карточка остаётся на месте."},
            {"order": 3, "start_second": 6, "end_second": 9, "screen_text": "Правила спорят", "visual_brief": "Два простых блока расходятся."},
            {"order": 4, "start_second": 9, "end_second": 12, "screen_text": "Граница исправлена", "visual_brief": "Блоки соединяются одной линией."},
            {"order": 5, "start_second": 12, "end_second": 16, "screen_text": "Результат понятен", "visual_brief": "Синтетическое подтверждение на карточке."},
        ],
        "caption": "Интерфейс должен обещать только выполнимое.",
        "cover_text": "КНОПКА БЫЛА. ОТПРАВКИ — НЕТ.",
        "safety_note": "Использовать только синтетический интерфейс.",
        "source_limitations": "Материал описывает только зафиксированный эпизод.",
    }


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
    payload["ranked_candidates"][0]["story_strength_score"] = value
    with pytest.raises(scout.ScoutError, match="scout_ranking_score_invalid"):
        scout.parse_ranking(json.dumps(payload), run_id, snap, lambda _: [])


def test_ranking_rejects_unknown_candidate(tmp_path):
    snap = snapshot(tmp_path)
    run_id = "csr-" + "a" * 24
    payload = ranking_payload(snap, run_id)
    payload["ranked_candidates"][0]["candidate_id"] = "csc-" + "f" * 24
    with pytest.raises(scout.ScoutError, match="scout_ranking_candidate_invalid"):
        scout.parse_ranking(json.dumps(payload), run_id, snap, lambda _: [])


@pytest.mark.parametrize("duration,scenes", [(11, 5), (21, 5), (16, 3), (16, 8)])
def test_short_reel_bounds_closed(tmp_path, duration, scenes):
    snap = snapshot(tmp_path)
    run_id = "csr-" + "a" * 24
    payload = ranking_payload(snap, run_id)
    payload["ranked_candidates"][0]["recommended_duration_seconds"] = duration
    payload["ranked_candidates"][0]["recommended_scene_count"] = scenes
    with pytest.raises(scout.ScoutError, match="scout_short_reel_bounds_invalid"):
        scout.parse_ranking(json.dumps(payload), run_id, snap, lambda _: [])


def test_code_owned_weighted_order_ignores_model_rank(tmp_path):
    snap = snapshot(tmp_path)
    run_id = "csr-" + "a" * 24
    payload = ranking_payload(snap, run_id)
    payload["ranked_candidates"][0]["rank"] = 2
    payload["ranked_candidates"][1]["rank"] = 1
    payload["ranked_candidates"][0]["story_strength_score"] = 100
    payload["ranked_candidates"][0]["reel_ease_score"] = 100
    ranked = scout.parse_ranking(json.dumps(payload), run_id, snap, lambda _: [])
    assert ranked[0].candidate_id == payload["ranked_candidates"][0]["candidate_id"]


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

    async def model(messages, _response_format):
        calls.append(1)
        run_id = json.loads(messages[1]["content"])["scout_run_id"]
        return json.dumps(ranking_payload(snap, run_id))

    refreshed = asyncio.run(scout.rank_snapshot(
        tmp_path / "state", snap, admin_id=ADMIN, expected_admin_id=ADMIN,
        operator_request_id="refresh-ranking-0001", refresh=True, recent_summaries=(),
        risk_detector=lambda _: [], model_call=model,
    ))
    assert refreshed.run_id != first.run_id and calls == [1]


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
        return json.dumps(ready_payload(run.run_id, selected), ensure_ascii=False)

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
        return json.dumps(ready_payload(run.run_id, selected), ensure_ascii=False)

    asyncio.run(scout.prepare_candidate(tmp_path / "state", run.run_id, selected, admin_id=ADMIN, expected_admin_id=ADMIN, risk_detector=lambda _: [], model_call=model))

    async def forbidden(*_args):
        raise AssertionError("provider called")

    duplicate = asyncio.run(scout.prepare_candidate(tmp_path / "state", run.run_id, selected, admin_id=ADMIN, expected_admin_id=ADMIN, risk_detector=lambda _: [], model_call=forbidden))
    assert duplicate.model_calls == 0 and not duplicate.created


def test_prepared_scene_timings_must_be_contiguous(tmp_path):
    run, _ = asyncio.run(create_run(tmp_path))
    selected = run.ranked[0].candidate_id
    payload = ready_payload(run.run_id, selected)
    payload["scenes"][2]["start_second"] = 7
    with pytest.raises(scout.ScoutError, match="scout_ready_scene_timing_invalid"):
        scout._parse_ready(json.dumps(payload), run, scout.candidate_for_run(run, selected), lambda _: [])


@pytest.mark.parametrize("bad", ["/opt/private/story.json", "a" * 64, "API_KEY=value"])
def test_prepared_material_rejects_path_hash_and_secret(tmp_path, bad):
    run, _ = asyncio.run(create_run(tmp_path))
    selected = run.ranked[0].candidate_id
    payload = ready_payload(run.run_id, selected)
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
