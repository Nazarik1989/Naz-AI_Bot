from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import main
import naz_story_worker as worker
import story_pack_control as control
import story_production
from story_video_provider import FakeVideoProvider
from tests.test_content_inbox_scout_reel import ADMIN, button_texts
from tests.test_runway_reference_health import (
    _audited_tasks,
    _current_failure_pack,
    make_references,
)
from tests.test_story_runtime import DummyComposer, config


def _setup(tmp_path: Path, *, ready_voice: bool = False):
    pack = _current_failure_pack(tmp_path)
    if ready_voice:
        payload = story_production.read_manifest(pack.manifest_path)
        voice = payload["voice_over_plan"]
        audio = pack.manifest_path.parent / voice["path"]
        audio.parent.mkdir(parents=True, exist_ok=True)
        audio.write_bytes(b"existing-voice")
        voice.update({
            "status": "ready",
            "calls": 1,
            "audio_digest": hashlib.sha256(audio.read_bytes()).hexdigest(),
        })
        story_production.atomic_json(pack.manifest_path, payload)
    _ref_root, catalog = make_references(tmp_path)
    control.import_current_plan_reference_health(
        tmp_path / "story-packs",
        pack.plan_id,
        health_root=tmp_path / "health",
        references={role: value.path for role, value in catalog.items()},
        audited_tasks=_audited_tasks(),
    )
    proposal = control.propose_current_runway_scene_revisions(
        tmp_path / "story-packs",
        pack.plan_id,
        health_root=tmp_path / "health",
        admin_id=ADMIN,
        expected_admin_id=ADMIN,
    )
    proposal_path = pack.manifest_path.parent / "recovery-proposals" / (
        proposal["proposal_id"] + ".json"
    )
    proposal_bytes = proposal_path.read_bytes()
    parent_bytes = pack.manifest_path.read_bytes()
    revision = control.create_corrected_scene_revision_plan(
        tmp_path / "story-packs",
        pack.plan_id,
        admin_id=ADMIN,
        expected_admin_id=ADMIN,
    )
    return pack, proposal_path, proposal_bytes, parent_bytes, revision


def _plan(tmp_path: Path, revision_id: str) -> dict:
    return control.read_corrected_scene_revision_plan(
        tmp_path / "story-packs", revision_id
    )


def _approve(tmp_path: Path, revision_id: str) -> str:
    plan = _plan(tmp_path, revision_id)
    token = control.corrected_scene_revision_callback_token(
        plan, action="approve", admin_id=ADMIN
    )
    return control.approve_corrected_scene_revision_plan(
        tmp_path / "story-packs",
        revision_id,
        callback_token=token,
        admin_id=ADMIN,
        expected_admin_id=ADMIN,
    )


def test_exact_proposal_is_reused_byte_identically(tmp_path):
    pack, path, before, parent_before, revision = _setup(tmp_path)
    duplicate = control.create_corrected_scene_revision_plan(
        tmp_path / "story-packs", pack.plan_id,
        admin_id=ADMIN, expected_admin_id=ADMIN,
    )
    assert duplicate == revision
    assert path.read_bytes() == before
    assert pack.manifest_path.read_bytes() == parent_before
    assert len(list(path.parent.glob("*.json"))) == 1


@pytest.mark.parametrize("count", [0, 2])
def test_zero_or_multiple_exact_proposals_fail_closed(tmp_path, count):
    pack = _current_failure_pack(tmp_path)
    if count:
        _ref_root, catalog = make_references(tmp_path)
        control.import_current_plan_reference_health(
            tmp_path / "story-packs", pack.plan_id,
            health_root=tmp_path / "health",
            references={role: value.path for role, value in catalog.items()},
            audited_tasks=_audited_tasks(),
        )
        proposal = control.propose_current_runway_scene_revisions(
            tmp_path / "story-packs", pack.plan_id,
            health_root=tmp_path / "health", admin_id=ADMIN,
            expected_admin_id=ADMIN,
        )
        proposal_root = pack.manifest_path.parent / "recovery-proposals"
        original = proposal_root / f"{proposal['proposal_id']}.json"
        (proposal_root / ("f" * 24 + ".json")).write_bytes(original.read_bytes())
    with pytest.raises(story_production.StoryPlanError, match="proposal ambiguous"):
        control.create_corrected_scene_revision_plan(
            tmp_path / "story-packs", pack.plan_id,
            admin_id=ADMIN, expected_admin_id=ADMIN,
        )


def test_revision_plan_is_immutable_bound_and_cost_is_code_owned(tmp_path):
    pack, proposal_path, proposal_bytes, parent_bytes, revision = _setup(tmp_path)
    plan = _plan(tmp_path, revision["revision_plan_id"])
    assert plan["schema"] == control.CORRECTED_SCENE_REVISION_SCHEMA
    assert plan["proposal_digest"] == hashlib.sha256(proposal_bytes).hexdigest()
    assert plan["parent_manifest_digest"] == hashlib.sha256(parent_bytes).hexdigest()
    assert plan["parent_plan_id"] == pack.plan_id
    assert plan["approval_status"] == "awaiting_cost_approval"
    assert plan["generation_authorized"] is False
    assert plan["credit_estimate"] == {
        "keyframe_credits": 10, "video_credits": 50, "ceiling": 60,
    }
    assert revision["completed_checksum_count"] == 9
    assert proposal_path.read_bytes() == proposal_bytes


def test_corrected_inputs_are_new_object_only_routes(tmp_path):
    pack, _path, _proposal, _parent, revision = _setup(tmp_path)
    plan = _plan(tmp_path, revision["revision_plan_id"])
    parent = story_production.read_manifest(pack.manifest_path)
    old = {row["scene_id"]: row["keyframe_prompt"] for row in parent["scenes"]}
    assert [row["scene_id"] for row in plan["corrected_scenes"]] == [
        "02_problem", "05_conclusion"
    ]
    for row in plan["corrected_scenes"]:
        assert row["requires_naz_reference"] is False
        assert row["identity_reference"] is None
        assert row["keyframe_model"] == "gen4_image"
        assert row["video_model"] == "gen4_turbo"
        assert row["keyframe_prompt"] != old[row["scene_id"]]
        assert "@Naz" not in row["keyframe_prompt"]
        assert row["keyframe_input_digest"] == control._digest_value({
            key: value for key, value in row.items()
            if key not in {"revision_id", "keyframe_input_digest"}
        })
    assert "visible empty gap" in plan["corrected_scenes"][0]["keyframe_prompt"]
    assert "remain physically separate" in plan["corrected_scenes"][1]["keyframe_prompt"]


def test_completed_assets_are_private_copies_and_checksum_exact(tmp_path):
    pack, _path, _proposal, _parent, revision = _setup(tmp_path)
    plan = _plan(tmp_path, revision["revision_plan_id"])
    child = tmp_path / "story-packs" / revision["revision_plan_id"]
    parent = story_production.read_manifest(pack.manifest_path)
    jobs = {row["scene_id"]: row for row in parent["scene_jobs"]}
    matches = 0
    for record in plan["completed_scenes"]:
        job = jobs[record["scene_id"]]
        for role, field in (
            ("keyframe", "keyframe_path"), ("clean", "clean_path"),
            ("story", "story_path"),
        ):
            assert (child / job[field]).resolve() != (
                pack.manifest_path.parent / job[field]
            ).resolve()
            assert hashlib.sha256((child / job[field]).read_bytes()).hexdigest() == (
                record["asset_checksums"][role]
            )
            matches += 1
    assert matches == 9


def test_approval_keyboard_is_closed_bound_and_under_telegram_limit(tmp_path):
    _pack, _path, _proposal, _parent, revision = _setup(tmp_path)
    revision_id = revision["revision_plan_id"]
    plan = _plan(tmp_path, revision_id)
    tokens = {
        action: control.corrected_scene_revision_callback_token(
            plan, action=action, admin_id=ADMIN
        )
        for action in ("approve", "technical", "status", "cancel")
    }
    keyboard = main.inbox_scout_runway_revision_keyboard(
        revision_id,
        approve_token=tokens["approve"],
        technical_token=tokens["technical"],
        status_token=tokens["status"],
        cancel_token=tokens["cancel"],
    )
    assert button_texts(keyboard) == [
        "Подтвердить генерацию 2 сцен", "Показать технический план",
        "Обновить статус", "Отменить замену",
    ]
    assert all(len(button.callback_data.encode("utf-8")) <= 64 for row in keyboard.inline_keyboard for button in row)
    assert "60 Runway credits" in control.corrected_scene_revision_card(
        tmp_path / "story-packs", revision_id
    )


def test_revision_action_sends_real_cost_keyboard_without_provider_or_tts(tmp_path):
    pack, _path, _proposal, _parent, _revision = _setup(tmp_path)
    query = SimpleNamespace(data=f"scoutrw:revision:{pack.plan_id}", answer=AsyncMock())
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=ADMIN))
    bot = SimpleNamespace(send_message=AsyncMock())
    with (
        patch.object(main, "ADMIN_ID", ADMIN),
        patch.object(main, "NAZ_STORY_PACK_ROOT", tmp_path / "story-packs"),
        patch.object(main, "NAZ_RUNWAY_REFERENCE_HEALTH_ROOT", tmp_path / "health"),
        patch.object(main, "_synthesize_scout_reel_voice", new=AsyncMock()) as tts,
    ):
        asyncio.run(main.content_inbox_scout_runway_callback(update, SimpleNamespace(bot=bot)))
    tts.assert_not_awaited()
    sent = bot.send_message.await_args.kwargs
    assert "60 Runway credits" in sent["text"]
    assert button_texts(sent["reply_markup"])[0] == "Подтвердить генерацию 2 сцен"


def test_approval_is_provider_free_parent_immutable_and_idempotent(tmp_path):
    pack, proposal_path, proposal_before, parent_before, revision = _setup(tmp_path)
    revision_id = revision["revision_plan_id"]
    assert _approve(tmp_path, revision_id) == "approved"
    child = tmp_path / "story-packs" / revision_id
    approval_before = (child / "approval.json").read_bytes()
    runtime_before = (child / "revision-runtime.json").read_bytes()
    assert _approve(tmp_path, revision_id) == "already_approved"
    assert (child / "approval.json").read_bytes() == approval_before
    assert (child / "revision-runtime.json").read_bytes() == runtime_before
    assert pack.manifest_path.read_bytes() == parent_before
    assert proposal_path.read_bytes() == proposal_before


@pytest.mark.parametrize("stale", ["proposal", "completed_asset"])
def test_stale_proposal_or_completed_asset_rejects_before_approval(tmp_path, stale):
    pack, proposal_path, _proposal, _parent, revision = _setup(tmp_path)
    revision_id = revision["revision_plan_id"]
    if stale == "proposal":
        proposal_path.write_bytes(proposal_path.read_bytes() + b" ")
    else:
        payload = story_production.read_manifest(pack.manifest_path)
        job = next(row for row in payload["scene_jobs"] if row["scene_id"] == "01_hook")
        (pack.manifest_path.parent / job["clean_path"]).write_bytes(b"changed")
    with pytest.raises(story_production.StoryPlanError):
        _approve(tmp_path, revision_id)
    child = tmp_path / "story-packs" / revision_id
    assert not (child / "approval.json").exists()
    assert not (child / "revision-runtime.json").exists()


def test_wrong_admin_or_divergent_token_rejects_without_mutation(tmp_path):
    _pack, _path, _proposal, _parent, revision = _setup(tmp_path)
    revision_id = revision["revision_plan_id"]
    child = tmp_path / "story-packs" / revision_id
    with pytest.raises(story_production.StoryPlanError, match="binding invalid"):
        control.approve_corrected_scene_revision_plan(
            tmp_path / "story-packs", revision_id,
            callback_token="0" * 16, admin_id=ADMIN, expected_admin_id=ADMIN,
        )
    assert not (child / "approval.json").exists()
    assert not (child / "revision-runtime.json").exists()


def test_approved_runtime_queues_only_two_new_revision_jobs(tmp_path):
    pack, _path, _proposal, parent_before, revision = _setup(tmp_path, ready_voice=True)
    revision_id = revision["revision_plan_id"]
    assert _approve(tmp_path, revision_id) == "approved"
    _plan_value, runtime, runtime_path = control.validate_corrected_scene_revision_for_worker(
        tmp_path / "story-packs", revision_id
    )
    jobs = {row["scene_id"]: row for row in runtime["scene_jobs"]}
    assert [sid for sid, row in jobs.items() if row["state"] == "queued"] == [
        "02_problem", "05_conclusion"
    ]
    assert all(jobs[sid]["state"] == "completed" for sid in ("01_hook", "03_test", "04_result"))
    assert all(jobs[sid]["keyframe_attempts"] == 0 for sid in control.CORRECTED_SCENE_IDS)
    assert runtime["voice_over_plan"]["status"] == "ready"
    assert runtime["voice_over_plan"]["calls"] == 1
    assert pack.manifest_path.read_bytes() == parent_before
    assert runtime_path.is_file()


def test_worker_submits_new_object_scene_once_and_polls_on_restart(tmp_path):
    pack, _path, _proposal, parent_before, revision = _setup(tmp_path)
    revision_id = revision["revision_plan_id"]
    _approve(tmp_path, revision_id)
    provider = FakeVideoProvider()
    cfg = config(tmp_path / "story-packs")
    assert worker.process_pack(
        revision_id, config=cfg, provider=provider, composer=DummyComposer()
    ) == "queued"
    assert len(provider.keyframe_submissions) == 1
    request = provider.keyframe_submissions[0]
    assert request.scene_id == "02_problem"
    assert request.reference_path is None and request.reference_paths == ()
    assert "@Naz" not in request.prompt
    worker.process_pack(revision_id, config=cfg, provider=provider, composer=DummyComposer())
    assert len(provider.keyframe_submissions) == 1
    parent = story_production.read_manifest(pack.manifest_path)
    assert pack.manifest_path.read_bytes() == parent_before
    assert next(row for row in parent["scene_jobs"] if row["scene_id"] == "02_problem")["keyframe_attempts"] == 2
    runtime = control.read_corrected_scene_runtime(
        tmp_path / "story-packs" / revision_id / "revision-runtime.json"
    )
    revised = next(row for row in runtime["scene_jobs"] if row["scene_id"] == "02_problem")
    assert revised["keyframe_attempts"] == 1


def test_worker_factory_uses_existing_canonical_object_route(tmp_path):
    _pack, _path, _proposal, _parent, revision = _setup(tmp_path)
    revision_id = revision["revision_plan_id"]
    _approve(tmp_path, revision_id)
    provider = FakeVideoProvider()
    with patch.object(worker, "provider_from_environment", return_value=provider) as factory:
        worker.process_pack(
            revision_id,
            config=config(tmp_path / "story-packs"),
            composer=DummyComposer(),
            env={"NAZ_VIDEO_PROVIDER": "runway"},
        )
    factory.assert_called_once_with(
        {"NAZ_VIDEO_PROVIDER": "runway"}, model_override="gen4_turbo"
    )


def test_queue_discovers_approved_revision_but_not_awaiting_plan(tmp_path):
    _pack, _path, _proposal, _parent, revision = _setup(tmp_path)
    revision_id = revision["revision_plan_id"]
    assert revision_id not in worker._queued_plan_ids(tmp_path / "story-packs")
    _approve(tmp_path, revision_id)
    assert revision_id in worker._queued_plan_ids(tmp_path / "story-packs")


def test_runtime_preserves_final_scene_order_and_no_publication_contract(tmp_path):
    _pack, _path, _proposal, _parent, revision = _setup(tmp_path, ready_voice=True)
    revision_id = revision["revision_plan_id"]
    _approve(tmp_path, revision_id)
    runtime = control.read_corrected_scene_runtime(
        tmp_path / "story-packs" / revision_id / "revision-runtime.json"
    )
    assert [row["scene_id"] for row in runtime["scene_jobs"]] == [
        "01_hook", "02_problem", "03_test", "04_result", "05_conclusion"
    ]
    assert runtime["music_plan"]["mode"] == "voice_over_only"
    assert "publication" not in runtime
    assert runtime["delivery"]["status"] == "not_ready"


def test_revision_callback_approval_calls_no_provider_tts_or_publication(tmp_path):
    _pack, _path, _proposal, _parent, revision = _setup(tmp_path)
    revision_id = revision["revision_plan_id"]
    plan = _plan(tmp_path, revision_id)
    token = control.corrected_scene_revision_callback_token(
        plan, action="approve", admin_id=ADMIN
    )
    query = SimpleNamespace(
        data=f"scoutrv:a:{revision_id}:{token}", answer=AsyncMock()
    )
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=ADMIN))
    bot = SimpleNamespace(send_message=AsyncMock())
    with (
        patch.object(main, "ADMIN_ID", ADMIN),
        patch.object(main, "NAZ_STORY_PACK_ROOT", tmp_path / "story-packs"),
        patch.object(main, "NAZ_STORY_RENDER_ENABLED", True),
        patch.object(worker, "provider_from_environment") as provider_factory,
        patch.object(main, "_synthesize_scout_reel_voice", new=AsyncMock()) as tts,
    ):
        asyncio.run(
            main.content_inbox_scout_runway_revision_callback(
                update, SimpleNamespace(bot=bot)
            )
        )
    provider_factory.assert_not_called()
    tts.assert_not_awaited()
    assert "Стоимость подтверждена" in bot.send_message.await_args.kwargs["text"]
