from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

import main
import naz_story_worker as worker
import runway_reference_health as health
import story_pack_control
import story_production
from story_video_provider import ProviderJob, RunwayVideoProvider
from tests.test_content_inbox_scout_runway import (
    button_texts,
    current_frontal_recovery_pack,
)
from tests.test_story_runtime import DummyComposer, MockTransport, config, make_pack
from story_video_provider import FakeVideoProvider


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def route(*, role: str = "three_quarter_identity", file_digest: str | None = None) -> health.ReferenceRoute:
    file_digest = file_digest or digest(role)
    return health.ReferenceRoute(
        "runway",
        "gen4_image",
        health.REFERENCE_PROFILE_VERSION,
        health.PROMPT_POLICY_VERSION,
        role,
        file_digest,
        health.reference_set_digest(((role, file_digest),)),
    )


@pytest.mark.parametrize(
    ("provider_code", "expected"),
    [
        ("INTERNAL.BAD_OUTPUT.CODE01", "bad_output"),
        ("ASSET.INVALID", "asset_invalid"),
        ("INPUT.INVALID", "unknown_terminal"),
        ("SAFETY.INPUT.TEXT", "moderation_terminal"),
        ("INPUT_PREPROCESSING.SAFETY.TEXT", "input_safety_terminal"),
        ("INPUT_PREPROCESSING.INTERNAL", "provider_preprocessing_internal"),
        ("THIRD_PARTY.UNAVAILABLE", "provider_dependency_unavailable"),
        ("INTERNAL", "provider_internal_unknown"),
        (None, "provider_internal_unknown"),
        ("private prose / not allowlisted", "unknown_terminal"),
    ],
)
def test_provider_failure_classification_is_closed(provider_code, expected):
    decision = health.classify_provider_failure_code(provider_code)
    assert decision.normalized_category == expected
    assert decision.same_input_retry is False
    assert decision.automatic_retry is (
        expected in {"provider_preprocessing_internal", "provider_dependency_unavailable"}
    )


def test_provider_retrieve_persists_only_closed_failure_category():
    transport = MockTransport([(
        200,
        {"Content-Type": "application/json"},
        json.dumps({
            "status": "FAILED",
            "failureCode": "INTERNAL.BAD_OUTPUT.CODE01",
            "failure": "private prompt and provider prose",
        }).encode("utf-8"),
    )])
    provider = RunwayVideoProvider(api_key="secret", transport=transport)
    result = provider.retrieve("task-safe")
    assert result.failure_code == "bad_output"
    assert result.provider_failure_code == "INTERNAL.BAD_OUTPUT.CODE01"
    assert result.failure_category == "bad_output"
    assert result.corrected_input_required is True
    assert "private" not in repr(result)


def test_reference_digest_change_creates_new_health_identity():
    assert route(file_digest=digest("a")).identity != route(file_digest=digest("b")).identity


def test_two_consecutive_terminal_failures_quarantine_exact_route(tmp_path):
    registry = health.ReferenceHealthRegistry(tmp_path / "health")
    current = route()
    registry.record_terminal(current, plan_id="a" * 24, scene_id="01_hook", category="bad_output")
    assert registry.health_state(current) == "revalidation_required"
    registry.record_terminal(current, plan_id="a" * 24, scene_id="02_problem", category="bad_output")
    assert registry.health_state(current) == "quarantined"


def test_successful_keyframe_marks_exact_route_healthy(tmp_path):
    registry = health.ReferenceHealthRegistry(tmp_path / "health")
    current = route(role="frontal_identity")
    registry.record_terminal(current, plan_id="b" * 24, scene_id="01_hook", category="bad_output")
    registry.record_success(current, plan_id="b" * 24, scene_id="01_hook")
    record = registry.snapshot()["records"][current.identity]
    assert record["health_state"] == "degraded"
    assert record["consecutive_terminal_count"] == 0


def test_registry_is_private_atomic_and_append_only(tmp_path):
    registry = health.ReferenceHealthRegistry(tmp_path / "health")
    current = route()
    registry.record_terminal(current, plan_id="c" * 24, scene_id="01_hook", category="bad_output")
    first_events = {item.name: item.read_bytes() for item in registry.events_root.iterdir()}
    registry.record_terminal(current, plan_id="c" * 24, scene_id="02_problem", category="bad_output")
    assert all((registry.events_root / name).read_bytes() == raw for name, raw in first_events.items())
    if os.name != "nt":
        assert (registry.root.stat().st_mode & 0o777) == 0o700
        assert (registry.state_path.stat().st_mode & 0o777) == 0o600
        assert all((item.stat().st_mode & 0o777) == 0o600 for item in registry.events_root.iterdir())


def test_new_story_contract_separates_anchor_from_camera_view(tmp_path):
    _pack, _pack_dir, manifest = make_pack(
        tmp_path, approved=False, story_arc="module_recovery_human"
    )
    payload = story_production.read_manifest(manifest)
    identity_scenes = [scene for scene in payload["scenes"] if scene["requires_naz_reference"]]
    assert identity_scenes
    assert {scene["identity_anchor_role"] for scene in identity_scenes} == {"frontal_identity"}
    assert all(scene["desired_view_role"] in {"frontal", "three_quarter"} for scene in identity_scenes)
    assert all(scene["auxiliary_reference_roles"] == [
        "three_quarter_identity", "full_body_identity"
    ] for scene in identity_scenes)


def make_references(root: Path) -> tuple[Path, dict[str, worker.ReferenceSelection]]:
    ref_root = root / "references"
    ref_root.mkdir()
    result = {}
    for role in ("frontal_identity", "three_quarter_identity", "full_body_identity"):
        path = ref_root / f"{role}.jpg"
        path.write_bytes(role.encode("ascii"))
        result[role] = worker.ReferenceSelection(path.resolve(), role)
    return ref_root, result


def test_frontal_reference_is_first_even_for_three_quarter_view(tmp_path):
    _root, catalog = make_references(tmp_path)
    selected = worker._identity_reference_set(catalog, "three_quarter_identity")
    assert [item.role for item in selected] == [
        "frontal_identity", "three_quarter_identity", "full_body_identity"
    ]


def test_quarantined_auxiliary_is_excluded_before_submit(tmp_path):
    _root, catalog = make_references(tmp_path)
    registry = health.ReferenceHealthRegistry(tmp_path / "health")
    auxiliary_digest = health.sha256_file(catalog["three_quarter_identity"].path)
    frontal_digest = health.sha256_file(catalog["frontal_identity"].path)
    quarantined = health.ReferenceRoute(
        "runway", "gen4_image", health.REFERENCE_PROFILE_VERSION,
        health.PROMPT_POLICY_VERSION,
        "three_quarter_identity", auxiliary_digest,
        health.reference_set_digest((
            ("frontal_identity", frontal_digest),
            ("three_quarter_identity", auxiliary_digest),
        )),
    )
    registry.import_route_evidence(
        quarantined,
        successful_count=0,
        terminal_count=2,
        consecutive_terminal_count=2,
        last_failure_category="bad_output",
        health_state="quarantined",
        evidence_id=digest("quarantine"),
    )
    selected = worker._identity_reference_set(
        catalog, "three_quarter_identity", registry=registry
    )
    assert [item.role for item in selected] == ["frontal_identity", "full_body_identity"]


def _identity_pack(tmp_path: Path):
    ref_root, _catalog = make_references(tmp_path)
    (ref_root / "naz-reference-profile.json").write_text(json.dumps({
        "schema": "naz-reference-profile.v2",
        "persona": "naz",
        "reference_files": {
            "frontal_identity": "frontal_identity.jpg",
            "three_quarter_identity": "three_quarter_identity.jpg",
            "full_body_identity": "full_body_identity.jpg",
        },
        "body_profile": {},
    }), encoding="utf-8")
    pack, _pack_dir, manifest = make_pack(
        tmp_path / "packs", keyframes_ready=False, story_arc="module_recovery_human"
    )
    payload = story_production.read_manifest(manifest)
    first = payload["scenes"][0]
    first.update({
        "reference_role": "three_quarter_identity",
        "identity_anchor_role": "frontal_identity",
        "desired_view_role": "three_quarter",
        "auxiliary_reference_roles": ["three_quarter_identity", "full_body_identity"],
    })
    payload["scene_jobs"][0].update({
        "reference_role": "three_quarter_identity",
        "identity_anchor_role": "frontal_identity",
        "desired_view_role": "three_quarter",
        "auxiliary_reference_roles": ["three_quarter_identity", "full_body_identity"],
    })
    payload["immutable_plan_fingerprint"] = story_production._immutable_plan_fingerprint(payload)
    story_production.atomic_json(manifest, payload)
    cfg = config(
        tmp_path / "packs",
        reference_path=ref_root,
        reference_health_root=tmp_path / "health",
    )
    return pack, manifest, cfg


def test_bad_output_never_repeats_unchanged_input(tmp_path):
    pack, manifest, cfg = _identity_pack(tmp_path)
    provider = FakeVideoProvider()
    worker.process_pack(pack.plan_id, config=cfg, provider=provider, composer=DummyComposer())
    first = story_production.read_manifest(manifest)["scene_jobs"][0]
    provider.jobs[first["keyframe_external_job_id"]] = ProviderJob(
        first["keyframe_external_job_id"],
        "terminal_failed",
        failure_code="bad_output",
        provider_failure_code="INTERNAL.BAD_OUTPUT.CODE01",
        failure_category="bad_output",
        corrected_input_required=True,
    )
    worker.process_pack(pack.plan_id, config=cfg, provider=provider, composer=DummyComposer())
    blocked = story_production.read_manifest(manifest)["scene_jobs"][0]
    assert blocked["state"] == blocked["keyframe_state"] == "terminal_failed"
    assert blocked["keyframe_provider_failure_code"] == "INTERNAL.BAD_OUTPUT.CODE01"
    assert blocked["keyframe_failure_category"] == "bad_output"
    assert blocked["keyframe_automatic_fallbacks"] == 0
    worker.process_pack(pack.plan_id, config=cfg, provider=provider, composer=DummyComposer())
    assert sum(item.scene_id == "01_hook" for item in provider.keyframe_submissions) == 1


def test_quarantined_auxiliary_gets_one_frontal_only_fallback(tmp_path):
    pack, manifest, cfg = _identity_pack(tmp_path)
    provider = FakeVideoProvider()
    worker.process_pack(pack.plan_id, config=cfg, provider=provider, composer=DummyComposer())
    first = story_production.read_manifest(manifest)["scene_jobs"][0]
    route_value = worker._route_from_intent(first["keyframe_submit_intent"])
    assert route_value is not None
    health.ReferenceHealthRegistry(cfg.reference_health_root).record_terminal(
        route_value, plan_id=pack.plan_id, scene_id="02_problem", category="bad_output"
    )
    provider.jobs[first["keyframe_external_job_id"]] = ProviderJob(
        first["keyframe_external_job_id"],
        "terminal_failed",
        failure_code="bad_output",
        provider_failure_code="INTERNAL.BAD_OUTPUT.CODE01",
        failure_category="bad_output",
        corrected_input_required=True,
    )
    worker.process_pack(pack.plan_id, config=cfg, provider=provider, composer=DummyComposer())
    queued = story_production.read_manifest(manifest)["scene_jobs"][0]
    assert queued["state"] == queued["keyframe_state"] == "queued"
    assert queued["keyframe_automatic_fallbacks"] == 1
    worker.process_pack(pack.plan_id, config=cfg, provider=provider, composer=DummyComposer())
    fallback = story_production.read_manifest(manifest)["scene_jobs"][0]
    submissions = [
        item for item in provider.keyframe_submissions if item.scene_id == "01_hook"
    ]
    assert len(submissions) == 2
    assert len(submissions[-1].reference_paths) == 1
    assert fallback["keyframe_submit_intent"]["approval_scope"] == "automatic_frontal_fallback"


def test_transient_retry_is_delayed_bounded_and_changes_prompt(tmp_path):
    pack, manifest, cfg = _identity_pack(tmp_path)
    provider = FakeVideoProvider()
    worker.process_pack(pack.plan_id, config=cfg, provider=provider, composer=DummyComposer())
    first = story_production.read_manifest(manifest)["scene_jobs"][0]
    first_prompt = provider.keyframe_submissions[0].prompt
    provider.jobs[first["keyframe_external_job_id"]] = ProviderJob(
        first["keyframe_external_job_id"],
        "terminal_failed",
        failure_code="provider_preprocessing_internal",
        provider_failure_code="INPUT_PREPROCESSING.INTERNAL",
        failure_category="provider_preprocessing_internal",
        automatic_retry_allowed=True,
        delayed_retry_eligible=True,
    )
    worker.process_pack(pack.plan_id, config=cfg, provider=provider, composer=DummyComposer())
    queued = story_production.read_manifest(manifest)["scene_jobs"][0]
    assert queued["keyframe_retry_phase"] == "automatic_delayed"
    worker.process_pack(pack.plan_id, config=cfg, provider=provider, composer=DummyComposer())
    assert sum(item.scene_id == "01_hook" for item in provider.keyframe_submissions) == 1
    queued["keyframe_retry_not_before"] = "2000-01-01T00:00:00+00:00"
    story_production.atomic_json(manifest, story_production.read_manifest(manifest) | {
        "scene_jobs": [queued, *story_production.read_manifest(manifest)["scene_jobs"][1:]]
    })
    worker.process_pack(pack.plan_id, config=cfg, provider=provider, composer=DummyComposer())
    submissions = [
        item for item in provider.keyframe_submissions if item.scene_id == "01_hook"
    ]
    assert len(submissions) == 2
    assert submissions[1].prompt != first_prompt
    retry = story_production.read_manifest(manifest)["scene_jobs"][0]
    provider.jobs[retry["keyframe_external_job_id"]] = ProviderJob(
        retry["keyframe_external_job_id"],
        "terminal_failed",
        failure_code="provider_preprocessing_internal",
        failure_category="provider_preprocessing_internal",
        automatic_retry_allowed=True,
        delayed_retry_eligible=True,
    )
    worker.process_pack(pack.plan_id, config=cfg, provider=provider, composer=DummyComposer())
    worker.process_pack(pack.plan_id, config=cfg, provider=provider, composer=DummyComposer())
    assert sum(item.scene_id == "01_hook" for item in provider.keyframe_submissions) == 2


@pytest.mark.parametrize(
    "failure_code",
    ["moderation_terminal", "input_safety_terminal", "asset_invalid", "unknown_terminal"],
)
def test_hard_failures_do_not_auto_retry(tmp_path, failure_code):
    pack, manifest, cfg = _identity_pack(tmp_path)
    provider = FakeVideoProvider()
    worker.process_pack(pack.plan_id, config=cfg, provider=provider, composer=DummyComposer())
    first = story_production.read_manifest(manifest)["scene_jobs"][0]
    provider.jobs[first["keyframe_external_job_id"]] = ProviderJob(
        first["keyframe_external_job_id"], "terminal_failed", failure_code=failure_code
    )
    worker.process_pack(pack.plan_id, config=cfg, provider=provider, composer=DummyComposer())
    current = story_production.read_manifest(manifest)["scene_jobs"][0]
    assert current["state"] == "terminal_failed"
    assert current["keyframe_automatic_fallbacks"] == 0
    assert len(provider.keyframe_submissions) == 1


def test_no_provider_call_before_initial_approval(tmp_path):
    pack, _pack_dir, _manifest = make_pack(
        tmp_path, approved=False, keyframes_ready=False, story_arc="module_recovery_human"
    )
    provider = FakeVideoProvider()
    assert worker.process_pack(
        pack.plan_id, config=config(tmp_path), provider=provider, composer=DummyComposer()
    ) == "awaiting_approval"
    assert provider.keyframe_submissions == []


def _audited_tasks() -> dict[str, dict[str, object]]:
    return {
        "01_hook": {
            "status": "SUCCEEDED",
            "failure_code": None,
            "task_identity_digest": digest("scene-01-task"),
        },
        "02_problem": {
            "status": "FAILED",
            "failure_code": "INTERNAL.BAD_OUTPUT.CODE01",
            "task_identity_digest": digest("scene-02-task"),
        },
        "05_conclusion": {
            "status": "FAILED",
            "failure_code": "INTERNAL.BAD_OUTPUT.CODE01",
            "task_identity_digest": digest("scene-05-task"),
        },
    }


def _current_failure_pack(tmp_path: Path):
    pack = current_frontal_recovery_pack(tmp_path)
    payload = story_production.read_manifest(pack.manifest_path)
    for job in payload["scene_jobs"]:
        if job["scene_id"] in {"01_hook", "02_problem", "05_conclusion"}:
            job["keyframe_attempts"] = 2
    first = payload["scene_jobs"][0]
    for field, content in (
        ("keyframe_path", b"current-keyframe-01"),
        ("clean_path", b"current-clean-01"),
        ("story_path", b"current-story-01"),
    ):
        artifact = pack.manifest_path.parent / first[field]
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(content)
        first[field.replace("_path", "_checksum")] = hashlib.sha256(content).hexdigest()
    first.update({
        "state": "completed",
        "attempts": 1,
        "external_job_id": "current-video-task-0",
        "provider_status": "completed",
        "failure_code": None,
        "keyframe_state": "ready",
        "keyframe_provider_status": "completed",
        "keyframe_failure_code": None,
    })
    story_production.atomic_json(pack.manifest_path, payload)
    return pack


def test_current_plan_health_import_is_provider_free_and_manifest_exact(tmp_path):
    pack = _current_failure_pack(tmp_path)
    before = pack.manifest_path.read_bytes()
    _ref_root, catalog = make_references(tmp_path)
    result = story_pack_control.import_current_plan_reference_health(
        tmp_path / "story-packs",
        pack.plan_id,
        health_root=tmp_path / "health",
        references={role: value.path for role, value in catalog.items()},
        audited_tasks=_audited_tasks(),
    )
    assert result["frontal_route_state"] == "degraded"
    assert result["three_quarter_route_state"] == "quarantined"
    assert result["provider_calls"] == 0
    assert pack.manifest_path.read_bytes() == before
    plan = story_pack_control.current_runway_failure_decision(
        tmp_path / "story-packs", pack.plan_id, health_root=tmp_path / "health"
    )
    assert plan["completed_scene_ids"] == ("01_hook", "03_test", "04_result")
    assert plan["blocked_scene_ids"] == ("02_problem", "05_conclusion")
    assert plan["scene_failures"]["02_problem"]["automatic_retry"] is False
    assert plan["scene_failures"]["05_conclusion"]["same_input_retry"] is False
    card = story_pack_control.current_runway_failure_decision_card(
        tmp_path / "story-packs", pack.plan_id, health_root=tmp_path / "health"
    )
    assert "Готово:\n3/5 сцен" in card
    assert card.count("INTERNAL.BAD_OUTPUT.CODE01 → bad_output") == 2
    assert "Новых генераций не выполнялось" in card
    assert button_texts(main.inbox_scout_runway_failure_keyboard(pack.plan_id)) == [
        "Подготовить план замены 2 сцен",
        "Обновить статус",
        "Отменить текущий план",
    ]


def test_current_decision_proposal_is_immutable_provider_free_and_cost_gated(tmp_path):
    pack = _current_failure_pack(tmp_path)
    _ref_root, catalog = make_references(tmp_path)
    story_pack_control.import_current_plan_reference_health(
        tmp_path / "story-packs",
        pack.plan_id,
        health_root=tmp_path / "health",
        references={role: value.path for role, value in catalog.items()},
        audited_tasks=_audited_tasks(),
    )
    before = pack.manifest_path.read_bytes()
    first = story_pack_control.propose_current_runway_scene_revisions(
        tmp_path / "story-packs", pack.plan_id,
        health_root=tmp_path / "health", admin_id=7, expected_admin_id=7,
    )
    second = story_pack_control.propose_current_runway_scene_revisions(
        tmp_path / "story-packs", pack.plan_id,
        health_root=tmp_path / "health", admin_id=7, expected_admin_id=7,
    )
    assert first == second
    assert first["provider_calls"] == 0
    assert first["separate_cost_approval_required"] is True
    assert pack.manifest_path.read_bytes() == before
    proposals = list((pack.manifest_path.parent / "recovery-proposals").glob("*.json"))
    assert len(proposals) == 1
    assert json.loads(proposals[0].read_text(encoding="utf-8"))["generation_authorized"] is False


def test_old_draft_control_cannot_requeue_completed_scene_or_create_attempt_three(tmp_path):
    pack = _current_failure_pack(tmp_path)
    before = pack.manifest_path.read_bytes()
    with pytest.raises(
        story_production.StoryPlanError,
        match="current frontal reference retry unavailable",
    ):
        story_pack_control.approve_current_frontal_reference_retry(
            tmp_path / "story-packs", pack.plan_id,
            admin_id=7, expected_admin_id=7,
        )
    assert pack.manifest_path.read_bytes() == before
    payload = story_production.read_manifest(pack.manifest_path)
    assert payload["scene_jobs"][0]["state"] == "completed"
    assert all(
        job["keyframe_attempts"] == 2
        for job in payload["scene_jobs"]
        if job["scene_id"] in {"02_problem", "05_conclusion"}
    )


def test_current_audit_requires_exact_safe_failure_codes(tmp_path):
    pack = _current_failure_pack(tmp_path)
    _ref_root, catalog = make_references(tmp_path)
    audit = _audited_tasks()
    audit["02_problem"]["failure_code"] = None
    with pytest.raises(health.ReferenceHealthError, match="reference_health_migration_invalid"):
        story_pack_control.import_current_plan_reference_health(
            tmp_path / "story-packs", pack.plan_id,
            health_root=tmp_path / "health",
            references={role: value.path for role, value in catalog.items()},
            audited_tasks=audit,
        )


def test_current_plan_import_rejects_changed_completed_asset(tmp_path):
    pack = _current_failure_pack(tmp_path)
    payload = story_production.read_manifest(pack.manifest_path)
    completed = next(job for job in payload["scene_jobs"] if job["scene_id"] == "03_test")
    (pack.manifest_path.parent / completed["clean_path"]).write_bytes(b"changed")
    _ref_root, catalog = make_references(tmp_path)
    with pytest.raises(story_production.StoryPlanError, match="current completed scene evidence invalid"):
        story_pack_control.import_current_plan_reference_health(
            tmp_path / "story-packs",
            pack.plan_id,
            health_root=tmp_path / "health",
            references={role: value.path for role, value in catalog.items()},
            audited_tasks=_audited_tasks(),
        )


def test_approval_summary_displays_primary_and_maximum_recovery_cost(tmp_path):
    _pack, _dir, manifest = make_pack(tmp_path, approved=False)
    summary = story_pack_control.safe_summary(story_production.read_manifest(manifest))
    assert "Основной лимит" in summary
    assert "Максимальный лимит" in summary
    assert "автоматического fallback" in summary
