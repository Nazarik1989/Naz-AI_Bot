from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import content_inbox_scout_reel as reel
import content_inbox_scout_runway as bridge
import main
import naz_story_worker
import story_pack_control
import story_production
from story_media_composer import MediaProbe
from test_content_inbox_scout_reel import ADMIN, button_texts, prepared_selection


def runway_pack(tmp_path: Path):
    _run, _candidate, _ready, selected, material, _created = asyncio.run(prepared_selection(tmp_path))
    pack = bridge.create_runway_pack(
        tmp_path / "reels",
        tmp_path / "state",
        tmp_path / "story-packs",
        selected.selection_id,
        admin_id=ADMIN,
        expected_admin_id=ADMIN,
        bridge_request_id="scout-runway-test-01",
        risk_detector=lambda _text: [],
        created_timestamp="2026-09-02T12:00:00Z",
    )
    return selected, material, pack


def test_default_build_is_runway_and_local_storyboard_is_secondary():
    selected = SimpleNamespace(selection_id="css-" + "a" * 24)
    labels = button_texts(main.inbox_scout_selected_keyboard(selected))
    assert labels[0] == "Собрать в Runway"
    assert "Показать технический сториборд" in labels
    assert "Собрать Reel" not in labels


def test_bridge_creates_one_current_story_pack_without_provider(tmp_path):
    selected, material, pack = runway_pack(tmp_path)
    before = {
        "selection": (selected.selection_dir / "selection.json").read_bytes(),
        "ready": next((tmp_path / "state" / "runs").glob("*/prepared/*/ready-material.json")).read_bytes(),
    }
    duplicate = bridge.create_runway_pack(
        tmp_path / "reels", tmp_path / "state", tmp_path / "story-packs",
        selected.selection_id, admin_id=ADMIN, expected_admin_id=ADMIN,
        bridge_request_id="scout-runway-test-01", risk_detector=lambda _text: [],
        created_timestamp="2026-09-02T12:00:00Z",
    )
    assert duplicate.plan_id == pack.plan_id and duplicate.created is False
    payload = story_production.read_manifest(pack.manifest_path)
    assert story_production.manifest_has_current_production_contract(payload)
    assert payload["schema"] == story_production.STORY_SCHEMA
    assert len(payload["scene_jobs"]) == 5
    assert len(payload["reel_jobs"]) == 1
    assert all(job["keyframe_state"] == "planned" and job["external_job_id"] is None for job in payload["scene_jobs"])
    assert all(scene["story_overlay"] == stored["screen_text"] for scene, stored in zip(payload["scenes"], material["scenes"]))
    assert sum(float(shot["duration_seconds"]) for shot in payload["reel_edits"][0]["shots"]) == 15.0
    assert payload["voice_over_plan"]["text"] == material["reel_voice_over"]
    assert payload["voice_over_plan"]["calls"] == 0
    assert payload["music_plan"]["mode"] == "voice_over_only"
    assert payload["scout_runway_bridge"]["local_storyboard"] == {
        "render_profile": reel.RENDER_PROFILE,
        "artifact_role": "local_storyboard",
        "publishable": False,
        "superseded_by_runway_flow": True,
    }
    assert (selected.selection_dir / "selection.json").read_bytes() == before["selection"]
    assert next((tmp_path / "state" / "runs").glob("*/prepared/*/ready-material.json")).read_bytes() == before["ready"]


def test_existing_local_storyboard_job_is_immutable_and_non_publishable(tmp_path):
    _run, _candidate, _ready, selected, material, _created = asyncio.run(prepared_selection(tmp_path))
    local, _ = reel.reserve_job(tmp_path / "reels", selected, material)
    before = (local.job_dir / "job.json").read_bytes()
    pack = bridge.create_runway_pack(
        tmp_path / "reels", tmp_path / "state", tmp_path / "story-packs",
        selected.selection_id, admin_id=ADMIN, expected_admin_id=ADMIN,
        bridge_request_id="scout-runway-local-audit-01", risk_detector=lambda _text: [],
    )
    assert (local.job_dir / "job.json").read_bytes() == before
    classification = story_production.read_manifest(pack.manifest_path)["scout_runway_bridge"]["local_storyboard"]
    assert classification["publishable"] is False
    assert classification["superseded_by_runway_flow"] is True


def test_bridge_routes_keyframes_and_video_through_canonical_worker_contract(tmp_path):
    _selected, _material, pack = runway_pack(tmp_path)
    payload = story_production.read_manifest(pack.manifest_path)
    routes = [job["model_route"]["selected_model"] for job in payload["scene_jobs"]]
    assert routes == list(pack.model_routes)
    assert set(routes) <= {"gen4_turbo", "gen4.5"}
    assert payload["visual_strategy"]["keyframe_provider"] == "runway"
    assert payload["visual_strategy"]["keyframe_required"] is True
    source = Path(naz_story_worker.__file__).read_text(encoding="utf-8")
    assert "provider_from_environment" in source
    assert "KeyframeRequest" in source and "SceneRequest" in source
    bridge_source = Path(bridge.__file__).read_text(encoding="utf-8")
    assert "RunwayVideoProvider" not in bridge_source
    assert "httpx" not in bridge_source and "requests." not in bridge_source


def test_prompts_are_text_free_and_privacy_minimized(tmp_path):
    _selected, _material, pack = runway_pack(tmp_path)
    payload = story_production.read_manifest(pack.manifest_path)
    forbidden = ("telegram", "sqlite", "database row", "source path", "hash", "log", "username")
    for scene in payload["scenes"]:
        prompt = f"{scene['keyframe_prompt']} {scene['provider_prompt']}".casefold()
        assert all(re.search(rf"\b{re.escape(token)}\b", prompt) is None for token in forbidden)
        assert scene["story_overlay"] not in prompt


def test_approval_card_is_exact_and_zero_paid_calls(tmp_path):
    _selected, _material, pack = runway_pack(tmp_path)
    text = bridge.approval_card_text(pack)
    assert "✅ Материал готов к Runway" in text
    assert "15 секунд · 5 сцен" in text
    assert "5 ключевых кадров" in text and "5 видеосцен" in text
    assert "gen4_image" in text and "музыка не используется" in text
    labels = button_texts(main.inbox_scout_runway_keyboard(pack.plan_id))
    assert labels == ["Подтвердить генерацию", "Другой визуальный вариант", "Отменить"]
    payload = story_production.read_manifest(pack.manifest_path)
    assert payload["approval"]["status"] == "awaiting_approval"
    assert payload["voice_over_plan"]["calls"] == 0
    assert all(job["keyframe_external_job_id"] is None and job["external_job_id"] is None for job in payload["scene_jobs"])


def test_new_visual_variant_preserves_story_voice_and_has_no_media_jobs(tmp_path):
    selected, material, pack = runway_pack(tmp_path)
    plan = bridge._editorial_plan(selected)
    treatment = story_production.parse_reels_director_response(
        json.dumps(bridge._director_payload(plan, 1)),
        plan,
        bridge.SCENE_FACTS,
        variant_index=1,
    )
    variant = bridge.create_variant(
        tmp_path / "reels", tmp_path / "state", tmp_path / "story-packs", pack.plan_id,
        admin_id=ADMIN, expected_admin_id=ADMIN, risk_detector=lambda _text: [],
        director_treatment=treatment,
    )
    assert variant.plan_id != pack.plan_id
    new_payload = story_production.read_manifest(variant.manifest_path)
    old_payload = story_production.read_manifest(pack.manifest_path)
    assert old_payload["pack_status"] == "superseded"
    assert new_payload["voice_over_plan"]["text"] == material["reel_voice_over"]
    assert [scene["story_overlay"] for scene in new_payload["scenes"]] == [scene["screen_text"] for scene in material["scenes"]]
    assert all(job["external_job_id"] is None and job["keyframe_external_job_id"] is None for job in new_payload["scene_jobs"])


def test_build_callback_creates_approval_card_not_local_renderer(tmp_path):
    _run, _candidate, _ready, selected, _material, _created = asyncio.run(prepared_selection(tmp_path))
    query = SimpleNamespace(data=reel.callback_data("build", selected.selection_id), answer=AsyncMock())
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=ADMIN))
    bot = SimpleNamespace(send_message=AsyncMock(), send_video=AsyncMock())
    context = SimpleNamespace(bot=bot)
    with (
        patch.object(main, "ADMIN_ID", ADMIN),
        patch.object(main, "NAZ_CONTENT_INBOX_SCOUT_ROOT", tmp_path / "state"),
        patch.object(main, "NAZ_CONTENT_INBOX_SCOUT_REEL_ROOT", tmp_path / "reels"),
        patch.object(main, "NAZ_STORY_PACK_ROOT", tmp_path / "story-packs"),
        patch.object(main, "detect_content_risks", return_value=[]),
        patch.object(reel, "reserve_job") as local_job,
        patch.object(reel, "render_job", new=AsyncMock()) as local_render,
    ):
        asyncio.run(main.content_inbox_scout_reel_callback(update, context))
    local_job.assert_not_called()
    local_render.assert_not_awaited()
    bot.send_video.assert_not_awaited()
    assert "готов к Runway" in bot.send_message.await_args.kwargs["text"]


def test_voice_call_is_after_approval_and_bounded_to_one(tmp_path):
    _selected, material, pack = runway_pack(tmp_path)
    try:
        bridge.reserve_voice_call(tmp_path / "story-packs", pack.plan_id)
    except bridge.ScoutRunwayError as exc:
        assert exc.reason_code == "content_scout_runway_approval_required"
    else:
        raise AssertionError("voice call reserved before approval")
    story_pack_control.confirm_generation(tmp_path / "story-packs", pack.plan_id)
    reservation = bridge.reserve_voice_call(tmp_path / "story-packs", pack.plan_id)
    assert reservation == (material["reel_voice_over"], hashlib.sha256(material["reel_voice_over"].encode()).hexdigest())
    bridge.complete_voice_call(tmp_path / "story-packs", pack.plan_id, b"one-opus-call")
    assert bridge.reserve_voice_call(tmp_path / "story-packs", pack.plan_id) is None
    payload = story_production.read_manifest(pack.manifest_path)
    assert payload["voice_over_plan"]["calls"] == 1
    assert payload["voice_over_plan"]["status"] == "ready"


def test_voice_only_worker_composition_has_no_music_and_one_final_reel(tmp_path):
    _selected, _material, pack = runway_pack(tmp_path)
    story_pack_control.confirm_generation(tmp_path / "story-packs", pack.plan_id)
    bridge.reserve_voice_call(tmp_path / "story-packs", pack.plan_id)
    voice_path = bridge.complete_voice_call(tmp_path / "story-packs", pack.plan_id, b"voice")
    payload = story_production.read_manifest(pack.manifest_path)
    for job in payload["scene_jobs"]:
        job["state"] = "completed"
        job["actual_duration_seconds"] = 5.0
    story_production.atomic_json(pack.manifest_path, payload)
    composer = SimpleNamespace(
        safe_output=lambda root, relative: root / relative,
        compose_voice_reel=lambda **kwargs: MediaProbe(15.0, 1080, 1920, "h264", "yuv420p", "30/1", 1.0),
    )
    with patch.object(naz_story_worker, "checksum", return_value=hashlib.sha256(b"voice").hexdigest()):
        naz_story_worker._compose_reels(payload, pack.manifest_path, SimpleNamespace(), composer)
    result = story_production.read_manifest(pack.manifest_path)
    assert result["reel_jobs"][0]["state"] == "completed"
    assert result["reel_jobs"][0]["audio_present"] is True
    assert result["reel_jobs"][0]["music_present"] is False
    assert result["pack_status"] == "completed"
    assert voice_path.is_file()


def test_scout_delivery_returns_only_final_reel(tmp_path):
    _selected, _material, pack = runway_pack(tmp_path)
    payload = story_production.read_manifest(pack.manifest_path)
    payload["pack_status"] = "completed"
    for job in payload["scene_jobs"]:
        job["state"] = "completed"
        path = pack.manifest_path.parent / job["story_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"story")
    reel_job = payload["reel_jobs"][0]
    reel_job["state"] = "completed"
    reel_path = pack.manifest_path.parent / reel_job["path"]
    reel_path.parent.mkdir(parents=True, exist_ok=True)
    reel_path.write_bytes(b"reel")
    story_production.atomic_json(pack.manifest_path, payload)
    assert story_pack_control.delivery_files(pack.manifest_path) == [reel_path.resolve()]
