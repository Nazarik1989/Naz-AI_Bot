from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import content_inbox_scout as scout
import content_inbox_scout_reel as reel
import main
from test_content_inbox_scout import ADMIN, create_run, file_hashes, ready_payload_for


async def prepared_selection(tmp_path: Path, *, candidate_index: int = 0):
    with patch.object(scout, "code_owned_reel_spec", return_value=(15, 5, False)):
        run, _calls = await create_run(tmp_path)
    candidate_id = run.ranked[candidate_index].candidate_id

    async def model(*_args):
        return json.dumps(ready_payload_for(run, candidate_id), ensure_ascii=False)

    await scout.prepare_candidate(
        tmp_path / "state",
        run.run_id,
        candidate_id,
        admin_id=ADMIN,
        expected_admin_id=ADMIN,
        risk_detector=lambda _text: [],
        model_call=model,
    )
    selected, material, created = reel.promote_selection(
        tmp_path / "reels",
        tmp_path / "state",
        run.run_id,
        candidate_id,
        admin_id=ADMIN,
        expected_admin_id=ADMIN,
        selection_request_id=f"selection-request-{candidate_index:02d}",
        risk_detector=lambda _text: [],
        created_timestamp="2026-09-02T10:00:00Z",
    )
    ready_path = run.run_dir / "prepared" / candidate_id / "ready-material.json"
    return run, candidate_id, ready_path, selected, material, created


def button_texts(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def test_prepared_button_is_renamed_for_reel():
    markup = main.inbox_scout_keyboard("csr-" + "a" * 24, "csc-" + "b" * 24, prepared=True)
    assert button_texts(markup)[0] == "Выбрать для Reel"
    assert "Выбрать" not in button_texts(markup)


def test_select_creates_one_immutable_exactly_bound_artifact_without_provider(tmp_path):
    run, candidate_id, ready, selected, material, created = asyncio.run(prepared_selection(tmp_path))
    assert created is True
    assert selected.run_id == run.run_id and selected.candidate_id == candidate_id
    assert selected.ready_material_artifact_digest == hashlib.sha256(ready.read_bytes()).hexdigest()
    assert selected.voice_over_digest == hashlib.sha256(material["reel_voice_over"].encode()).hexdigest()
    assert selected.duration_seconds == 15 and selected.scene_count == 5
    assert list(selected.selection_dir.glob("selection.json"))
    if os.name != "nt":
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o700 for path in (tmp_path / "reels").rglob("*") if path.is_dir())
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in (tmp_path / "reels").rglob("*") if path.is_file())


def test_duplicate_select_is_byte_idempotent(tmp_path):
    run, candidate_id, _ready, selected, _material, _created = asyncio.run(prepared_selection(tmp_path))
    before = file_hashes(tmp_path / "reels")
    duplicate, _material, created = reel.promote_selection(
        tmp_path / "reels", tmp_path / "state", run.run_id, candidate_id,
        admin_id=ADMIN, expected_admin_id=ADMIN,
        selection_request_id="selection-request-00", risk_detector=lambda _text: [],
        created_timestamp="2026-09-02T11:00:00Z",
    )
    assert duplicate.selection_id == selected.selection_id and created is False
    assert file_hashes(tmp_path / "reels") == before


def test_divergent_reuse_of_selection_request_conflicts_before_selection_mutation(tmp_path):
    run, _candidate_id, _ready, _selected, _material, _created = asyncio.run(prepared_selection(tmp_path))
    second_id = run.ranked[1].candidate_id

    async def model(*_args):
        return json.dumps(ready_payload_for(run, second_id), ensure_ascii=False)

    asyncio.run(scout.prepare_candidate(
        tmp_path / "state", run.run_id, second_id, admin_id=ADMIN,
        expected_admin_id=ADMIN, risk_detector=lambda _text: [], model_call=model,
    ))
    before = file_hashes(tmp_path / "reels")
    with pytest.raises(reel.ScoutReelConflict, match="content_scout_selection_request_conflict"):
        reel.promote_selection(
            tmp_path / "reels", tmp_path / "state", run.run_id, second_id,
            admin_id=ADMIN, expected_admin_id=ADMIN,
            selection_request_id="selection-request-00", risk_detector=lambda _text: [],
        )
    assert file_hashes(tmp_path / "reels") == before


def test_missing_ready_material_is_rejected_without_selection(tmp_path):
    run, _ = asyncio.run(create_run(tmp_path))
    with pytest.raises(scout.ScoutError, match="scout_ready_material_missing"):
        reel.promote_selection(
            tmp_path / "reels", tmp_path / "state", run.run_id, run.ranked[0].candidate_id,
            admin_id=ADMIN, expected_admin_id=ADMIN,
            selection_request_id="selection-missing-01", risk_detector=lambda _text: [],
        )
    assert not (tmp_path / "reels").exists()


def test_legacy_or_english_ready_material_cannot_be_selected(tmp_path):
    run, _ = asyncio.run(create_run(tmp_path))
    candidate_id = run.ranked[0].candidate_id
    directory = run.run_dir / "prepared" / candidate_id
    directory.mkdir(parents=True)
    legacy = ready_payload_for(run, candidate_id)
    legacy["schema_version"] = scout.READY_SCHEMA_V1
    legacy.pop("output_language")
    (directory / "ready-material.json").write_text(json.dumps(legacy), encoding="utf-8")
    with pytest.raises(scout.ScoutError, match="scout_ready_language_contract_invalid"):
        reel.promote_selection(
            tmp_path / "reels", tmp_path / "state", run.run_id, candidate_id,
            admin_id=ADMIN, expected_admin_id=ADMIN,
            selection_request_id="selection-legacy-01", risk_detector=lambda _text: [],
        )


def test_ready_card_has_build_path_and_no_manual_work_dead_end(tmp_path):
    *_prefix, selected, _material, _created = asyncio.run(prepared_selection(tmp_path))
    text = reel.selection_card_text(selected)
    labels = button_texts(main.inbox_scout_selected_keyboard(selected))
    assert "готов к сборке" in text and "ручной работы" not in text
    assert labels == ["Собрать Reel", "Показать материал", "Другой из TOP", "Отменить"]


def test_select_callback_promotes_prepared_material_with_zero_new_model_calls(tmp_path):
    with patch.object(scout, "code_owned_reel_spec", return_value=(15, 5, False)):
        run, _calls = asyncio.run(create_run(tmp_path))
    candidate_id = run.ranked[0].candidate_id

    async def model(*_args):
        return json.dumps(ready_payload_for(run, candidate_id), ensure_ascii=False)

    asyncio.run(scout.prepare_candidate(
        tmp_path / "state", run.run_id, candidate_id, admin_id=ADMIN,
        expected_admin_id=ADMIN, risk_detector=lambda _text: [], model_call=model,
    ))
    query = SimpleNamespace(
        data=scout.callback_data("select", run.run_id, candidate_id), answer=AsyncMock()
    )
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=ADMIN))
    bot = SimpleNamespace(send_message=AsyncMock())
    context = SimpleNamespace(bot=bot)
    with (
        patch.object(main, "ADMIN_ID", ADMIN),
        patch.object(main, "NAZ_CONTENT_INBOX_SCOUT_ROOT", tmp_path / "state"),
        patch.object(main, "NAZ_CONTENT_INBOX_SCOUT_REEL_ROOT", tmp_path / "reels"),
        patch.object(main, "detect_content_risks", return_value=[]),
        patch.object(main, "_inbox_scout_model_call", new=AsyncMock()) as provider,
    ):
        asyncio.run(main.content_inbox_scout_callback(update, context))
    provider.assert_not_awaited()
    assert bot.send_message.await_count == 1
    sent = bot.send_message.await_args.kwargs
    assert "готов к сборке" in sent["text"] and "ручной работы" not in sent["text"]
    assert button_texts(sent["reply_markup"])[0] == "Собрать Reel"
    assert len(list((tmp_path / "reels" / "selections").glob("css-*/selection.json"))) == 1


@pytest.mark.parametrize("action", ["show", "other"])
def test_stored_selection_navigation_is_zero_provider(action, tmp_path):
    run, _candidate_id, _ready, selected, _material, _created = asyncio.run(prepared_selection(tmp_path))
    query = SimpleNamespace(data=reel.callback_data(action, selected.selection_id), answer=AsyncMock())
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=ADMIN))
    bot = SimpleNamespace(send_message=AsyncMock())
    context = SimpleNamespace(bot=bot)
    with (
        patch.object(main, "ADMIN_ID", ADMIN),
        patch.object(main, "NAZ_CONTENT_INBOX_SCOUT_ROOT", tmp_path / "state"),
        patch.object(main, "NAZ_CONTENT_INBOX_SCOUT_REEL_ROOT", tmp_path / "reels"),
        patch.object(main, "_inbox_scout_model_call", new=AsyncMock()) as provider,
    ):
        asyncio.run(main.content_inbox_scout_reel_callback(update, context))
    provider.assert_not_awaited()
    assert bot.send_message.await_count >= 1
    if action == "other":
        assert bot.send_message.await_count == min(3, len(run.ranked))


@pytest.mark.parametrize("action", ["build", "show", "other", "cancel", "publish", "remake"])
def test_non_admin_reel_action_is_rejected_before_state_or_provider(action, tmp_path):
    query = SimpleNamespace(data=f"scoutreel:{action}:{'a' * 24}", answer=AsyncMock())
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=999))
    context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    with patch.object(main, "ADMIN_ID", ADMIN), patch.object(reel, "load_selection") as load:
        asyncio.run(main.content_inbox_scout_reel_callback(update, context))
    load.assert_not_called()
    query.answer.assert_awaited_once_with("Scout Reel доступен только администратору.", show_alert=True)


def test_bound_ready_material_detects_any_artifact_change(tmp_path):
    _run, _candidate_id, ready, selected, _material, _created = asyncio.run(prepared_selection(tmp_path))
    ready.write_bytes(ready.read_bytes() + b" ")
    with pytest.raises((scout.ScoutError, reel.ScoutReelError)):
        reel.load_selected_ready_material(tmp_path / "state", selected, risk_detector=lambda _text: [])


def test_build_reserves_one_job_with_exact_ordered_scenes_and_voice(tmp_path):
    _run, _candidate_id, _ready, selected, material, _created = asyncio.run(prepared_selection(tmp_path))
    job, created = reel.reserve_job(tmp_path / "reels", selected, material)
    duplicate, duplicate_created = reel.reserve_job(tmp_path / "reels", selected, material)
    assert created is True and duplicate_created is False and duplicate.job_id == job.job_id
    assert tuple(item["order"] for item in job.ordered_scenes) == (1, 2, 3, 4, 5)
    assert tuple((item["start_second"], item["end_second"]) for item in job.ordered_scenes) == ((0, 3), (3, 6), (6, 9), (9, 12), (12, 15))
    assert job.voice_over_digest == hashlib.sha256(material["reel_voice_over"].encode()).hexdigest()


def test_render_passes_exact_voice_once_and_persists_closed_receipt(tmp_path):
    _run, _candidate_id, _ready, selected, material, _created = asyncio.run(prepared_selection(tmp_path))
    seen: list[str] = []

    async def tts(text: str) -> bytes:
        seen.append(text)
        return b"synthetic-opus"

    def command(args, timeout=180):
        if args[0] == "ffmpeg":
            Path(args[-1]).write_bytes(b"private-mp4")
        return subprocess.CompletedProcess(args, 0, stdout=b"{}", stderr=b"")

    technical = {
        "duration": 15.0,
        "video": {"codec_name": "h264", "width": 1080, "height": 1920, "pix_fmt": "yuv420p", "avg_frame_rate": "30/1"},
        "audio": {"codec_name": "aac"},
    }
    with (
        patch.object(reel, "_audio_duration", return_value=12.0),
        patch.object(reel.shutil, "which", return_value="tool"),
        patch.object(reel, "_run_command", side_effect=command),
        patch.object(reel, "_validate_output", return_value=technical),
    ):
        first = asyncio.run(reel.render_job(tmp_path / "reels", selected, material, tts_call=tts))
        second = asyncio.run(reel.render_job(tmp_path / "reels", selected, material, tts_call=tts))
    assert seen == [material["reel_voice_over"]]
    assert first.tts_calls == 1 and first.render_calls == 1
    assert second.tts_calls == 0 and second.render_calls == 0
    receipt = first.receipt
    assert receipt["duration_seconds"] == 15.0
    assert receipt["resolution"] == "1080x1920" and receipt["fps"] == 30
    assert receipt["video_codec"] == "h264" and receipt["pixel_format"] == "yuv420p"
    assert receipt["audio_present"] is True and receipt["music_present"] is False
    assert receipt["scene_count"] == 5
    preview_state = json.loads((first.job.job_dir / "states" / "preview_ready.json").read_text(encoding="utf-8"))
    assert preview_state["output_digest"] == receipt["output_sha256"]
    assert not list((tmp_path / "reels").rglob("*.lock"))


def test_invalid_output_is_never_returned_and_tts_is_not_retried(tmp_path):
    _run, _candidate_id, _ready, selected, material, _created = asyncio.run(prepared_selection(tmp_path))
    calls = []

    async def tts(text: str) -> bytes:
        calls.append(text)
        return b"synthetic-opus"

    def command(args, timeout=180):
        if args[0] == "ffmpeg":
            Path(args[-1]).write_bytes(b"invalid-mp4")
        return subprocess.CompletedProcess(args, 0, stdout=b"{}", stderr=b"")

    with (
        patch.object(reel, "_audio_duration", return_value=12.0),
        patch.object(reel.shutil, "which", return_value="tool"),
        patch.object(reel, "_run_command", side_effect=command),
        patch.object(reel, "_validate_output", side_effect=reel.ScoutReelError("content_scout_reel_output_invalid")),
    ):
        with pytest.raises(reel.ScoutReelError, match="content_scout_reel_output_invalid"):
            asyncio.run(reel.render_job(tmp_path / "reels", selected, material, tts_call=tts))
    assert len(calls) == 1
    assert not (next((tmp_path / "reels" / "jobs").iterdir()) / "preview.mp4").exists()
    assert not list((tmp_path / "reels").rglob("*.lock"))


def test_missing_local_render_tools_reject_before_tts(tmp_path):
    _run, _candidate_id, _ready, selected, material, _created = asyncio.run(prepared_selection(tmp_path))
    tts = AsyncMock(return_value=b"unused")
    with patch.object(reel.shutil, "which", return_value=None):
        with pytest.raises(reel.ScoutReelError, match="content_scout_reel_tools_unavailable"):
            asyncio.run(reel.render_job(tmp_path / "reels", selected, material, tts_call=tts))
    tts.assert_not_awaited()
    assert not list((tmp_path / "reels").rglob("*.lock"))


def test_concurrent_duplicate_build_cannot_repeat_tts(tmp_path):
    _run, _candidate_id, _ready, selected, material, _created = asyncio.run(prepared_selection(tmp_path))
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def tts(text: str) -> bytes:
        calls.append(text)
        entered.set()
        await release.wait()
        raise RuntimeError("synthetic stop")

    async def probe():
        first = asyncio.create_task(reel.render_job(tmp_path / "reels", selected, material, tts_call=tts))
        await entered.wait()
        second = await asyncio.gather(
            reel.render_job(tmp_path / "reels", selected, material, tts_call=tts),
            return_exceptions=True,
        )
        release.set()
        first_result = await asyncio.gather(first, return_exceptions=True)
        return first_result[0], second[0]

    with patch.object(reel.shutil, "which", return_value="tool"):
        first_result, second_result = asyncio.run(probe())
    assert isinstance(first_result, reel.ScoutReelError)
    assert isinstance(second_result, reel.ScoutReelError)
    assert second_result.reason_code == "content_scout_reel_render_in_progress"
    assert len(calls) == 1
    assert not list((tmp_path / "reels").rglob("*.lock"))


def test_output_validator_requires_video_audio_and_exact_technical_profile(tmp_path):
    path = tmp_path / "preview.mp4"
    path.write_bytes(b"x")
    valid = {
        "format": {"duration": "15.000"},
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "width": 1080, "height": 1920, "pix_fmt": "yuv420p", "avg_frame_rate": "30/1"},
            {"codec_type": "audio", "codec_name": "aac"},
        ],
    }
    with patch.object(reel, "_probe", return_value=valid):
        assert reel._validate_output(path, 15)["duration"] == 15.0
    for field, bad in (("width", 720), ("height", 1280), ("pix_fmt", "yuv444p"), ("avg_frame_rate", "25/1")):
        broken = json.loads(json.dumps(valid))
        broken["streams"][0][field] = bad
        with patch.object(reel, "_probe", return_value=broken), pytest.raises(reel.ScoutReelError):
            reel._validate_output(path, 15)


def test_selection_cancel_does_not_delete_ready_material(tmp_path):
    _run, _candidate_id, ready, selected, _material, _created = asyncio.run(prepared_selection(tmp_path))
    original = ready.read_bytes()
    reel.cancel_selection(tmp_path / "reels", selected.selection_id, admin_id=ADMIN, expected_admin_id=ADMIN)
    assert ready.read_bytes() == original
    assert (selected.selection_dir / "states" / "cancelled.json").is_file()
    with pytest.raises(reel.ScoutReelError, match="content_scout_selection_terminal"):
        reel.reserve_job(tmp_path / "reels", selected, _material,)


def test_publish_and_remake_callbacks_do_not_call_publication_or_provider(tmp_path):
    _run, _candidate_id, _ready, selected, _material, _created = asyncio.run(prepared_selection(tmp_path))
    for action in ("publish", "remake"):
        query = SimpleNamespace(data=reel.callback_data(action, selected.selection_id), answer=AsyncMock())
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=ADMIN))
        bot = SimpleNamespace(send_message=AsyncMock())
        context = SimpleNamespace(bot=bot)
        with (
            patch.object(main, "ADMIN_ID", ADMIN),
            patch.object(main, "NAZ_CONTENT_INBOX_SCOUT_ROOT", tmp_path / "state"),
            patch.object(main, "NAZ_CONTENT_INBOX_SCOUT_REEL_ROOT", tmp_path / "reels"),
            patch.object(main, "_inbox_scout_model_call", new=AsyncMock()) as provider,
        ):
            asyncio.run(main.content_inbox_scout_reel_callback(update, context))
        provider.assert_not_awaited()


def test_reel_module_does_not_import_narrative_or_remote_media_providers():
    source = Path(reel.__file__).read_text(encoding="utf-8")
    forbidden = (
        "narrative_normalizer", "openrouter", "runway", "image_generation",
        "story_production", "publication", "telegram_post(",
    )
    assert all(value not in source.casefold() for value in forbidden)


def test_real_renderer_contract_when_ffmpeg_is_available(tmp_path):
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe are not installed on this test platform")
    _run, _candidate_id, _ready, selected, material, _created = asyncio.run(prepared_selection(tmp_path))
    audio_path = tmp_path / "voice.opus"
    subprocess.run([
        "ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i",
        "sine=frequency=440:duration=1", "-c:a", "libopus", str(audio_path),
    ], check=True)

    async def tts(text: str) -> bytes:
        assert text == material["reel_voice_over"]
        return audio_path.read_bytes()

    result = asyncio.run(reel.render_job(tmp_path / "reels", selected, material, tts_call=tts))
    assert result.receipt["resolution"] == "1080x1920"
    assert 14.8 <= result.receipt["duration_seconds"] <= 15.2
