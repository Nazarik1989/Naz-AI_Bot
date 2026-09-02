"""Immutable Scout-to-Story bridge for the canonical Runway worker."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import content_inbox_scout as scout
import content_inbox_scout_reel as scout_reel
import editorial_orchestrator
import story_production
from story_pack_lock import StoryPackLock, StoryPackLockError


BRIDGE_SCHEMA = "content-inbox-scout-runway-bridge-v1"
COMPOSITION_SCHEMA = "content-inbox-scout-voice-composition-v1"
VOICE_SCHEMA = "content-inbox-scout-voice-over-v1"
BRIDGE_REQUEST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,95}$")
PLAN_ID_RE = re.compile(r"^[a-f0-9]{24}$")
DIGEST_RE = re.compile(r"^[a-f0-9]{64}$")
SCENE_FACTS = (
    "A software check exposed a Naz response with missing previous context.",
    "A software storage check confirmed that conversation history remained available.",
    "A software integration check located disconnected transfer between memory and generation.",
    "A software isolation test restored context transfer and kept user lanes separate.",
    "A regression test confirmed prior context in the generated Naz response.",
)
SEMANTIC_GOALS = (
    "Reveal Naz response and missing previous context.",
    "Show storage check and available conversation history.",
    "Expose memory-to-generation transfer as disconnected.",
    "Show separate user lanes after restored isolation test.",
    "Confirm regression test and prior context in generated Naz response.",
)


class ScoutRunwayError(RuntimeError):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True, slots=True)
class ScoutRunwayPack:
    bridge_request_id: str
    bridge_digest: str
    plan_id: str
    selection_id: str
    title: str
    duration_seconds: int
    scene_count: int
    model_routes: tuple[str, ...]
    keyframe_jobs: int
    video_jobs: int
    credit_estimate: int
    bridge_path: Path
    manifest_path: Path
    created: bool


def _canonical(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest_text(value: str) -> str:
    return _digest_bytes(value.encode("utf-8"))


def _write_exact(path: Path, value: Mapping[str, Any], reason: str) -> bool:
    data = _canonical(value)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != data:
            raise ScoutRunwayError(reason)
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return True


def _editorial_plan(selected: scout_reel.SelectedMaterial) -> editorial_orchestrator.EditorialPlan:
    base_id = _digest_text(f"{BRIDGE_SCHEMA}|{selected.selection_id}|{selected.ready_material_artifact_digest}")[:24]
    return editorial_orchestrator.EditorialPlan(
        plan_id=base_id,
        persona="Naz",
        platform="telegram",
        slot="operator-approved",
        rubric="Naz AI Lab",
        mode="work_chronicle",
        source_type="content_inbox_scout_selection",
        source_ref=selected.selection_id,
        topic=selected.title,
        purpose="show a verified engineering cause, correction and regression proof",
        content_format="story_pack",
        production_mode="story_first",
        thesis_direction="stored history must be transferred into response generation",
        epistemic_state="verified",
        tension="history exists but generation does not receive it",
        semantic_theme="context transfer",
        semantic_card="failure, preserved memory, broken transfer, repair, proof",
        facet="engineering reliability",
        author_role="technical operator",
        emotional_arc="confusion to verified clarity",
        reader_relation="transparent engineering explanation",
        structure="problem, evidence, cause, repair, regression proof",
        hook="Naz answers as though the previous message did not exist.",
        ending="Regression checks show that prior context reaches the response.",
        energy="restrained",
        seriousness="high",
        tempo="precise",
        length="short",
        humor="none",
        imagery="dark graphite, electric blue and cold silver physical mechanisms",
        visual_mode="cinematic",
        visual_subject_direction="one coherent Naz AI Lab world with memory modules and signal paths",
        visual_relation="a disconnected signal path is restored while user lanes remain separated",
        track_tags=("technology", "restrained"),
        orchestrator_version=editorial_orchestrator.ORCHESTRATOR_VERSION,
        content_policy_version="content-inbox-scout-runway-content-v1",
        visual_policy_version="content-inbox-scout-runway-visual-v1",
        music_policy_version="content-inbox-scout-voice-only-v1",
    )


def _director_payload(plan: editorial_orchestrator.EditorialPlan, variant_index: int) -> dict[str, Any]:
    count = len(SCENE_FACTS)
    roles = story_production._roles(
        story_production._variant_plan_id(plan.plan_id, variant_index), count
    )
    beat_ids = story_production._beat_ids(
        story_production._variant_plan_id(plan.plan_id, variant_index), count
    )
    story_arc = story_production._story_arc_names_for_plan(plan)[0]
    arc_steps = story_production._story_arc_steps(story_arc, count)
    understandings = (
        "Viewer understands missing context in the Naz response.",
        "Viewer understands available history after the storage check.",
        "Viewer understands disconnected memory-generation transfer.",
        "Viewer understands separate user lanes and restored transfer.",
        "Viewer understands prior context after regression test response confirmation.",
    )
    relations = (
        "opening",
        "Storage check of conversation history follows missing Naz response context.",
        "Disconnected memory transfer follows the available conversation history check.",
        "Isolation test and restored context transfer follow disconnected memory generation.",
        "Regression test of generated response follows restored transfer and separate user lanes.",
    )
    visual_anchors = (
        "Naz response and missing context",
        "storage check and conversation history",
        "memory and generation transfer",
        "isolation test and separate user lanes",
        "regression test and generated response",
    )
    scenes = []
    for index, (role, beat_id, goal, step) in enumerate(
        zip(roles, beat_ids, SEMANTIC_GOALS, arc_steps), start=1
    ):
        motion = story_production.DIRECTOR_ACTION_RECIPES[step[0]][1]
        scenes.append({
            "beat_id": beat_id,
            "semantic_goal": goal,
            "source_fact_refs": [f"fact-{index}"],
            "relation_to_previous": relations[index - 1],
            "expected_viewer_understanding": understandings[index - 1],
            "visualization_kind": "physical_metaphor",
            "visual_relation_to_beat": (
                f"Physical {motion} action maps {visual_anchors[index - 1]}."
            ),
            "shot_size": story_production.SHOT_SIZES[(index - 1) % len(story_production.SHOT_SIZES)],
            "camera_motion": story_production.CAMERA_MOTIONS[(index - 1) % len(story_production.CAMERA_MOTIONS)],
        })
    return {
        "director_version": story_production.DIRECTOR_VERSION,
        "core_thesis": "Missing context follows disconnected transfer; available history, isolation and regression check show restored response generation.",
        "thesis_source_fact_refs": ["fact-1", "fact-2", "fact-3", "fact-4", "fact-5"],
        "viewer_problem": "Missing previous context in a Naz response; conversation history remains available.",
        "hook": "Reveal missing previous context in the Naz response.",
        "hook_thesis_ref": "core_thesis",
        "payoff": "Show regression confirmation after context transfer restores the generated response.",
        "payoff_thesis_ref": "core_thesis",
        "visual_concept": (
            "Physical mechanism maps available history, disconnected memory transfer, isolated user lanes and regression confirmation."
        ),
        "story_spine": "Missing context, available history, disconnected transfer, isolation test, regression confirmation.",
        "story_arc": story_arc,
        "scenes": scenes,
    }


def _reel_edit(scenes: Sequence[story_production.ScenePlan]) -> story_production.ReelEditPlan:
    indexes = (0, 0, 1, 2, 2, 3, 4, 4)
    durations = (2.0, 1.0, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0)
    starts = (0.0, 2.0, 0.0, 0.0, 2.0, 0.0, 0.0, 2.0)
    shots: list[dict[str, Any]] = []
    for position, (scene_index, duration, start) in enumerate(zip(indexes, durations, starts)):
        scene = scenes[scene_index]
        source_index = story_production.SHOT_SIZES.index(scene.shot_size)
        reel_size = story_production.SHOT_SIZES[(source_index + 1) % len(story_production.SHOT_SIZES)]
        shots.append({
            "scene_id": scene.scene_id,
            "shot_size": reel_size,
            "source_scene_id": scene.scene_id,
            "source": f"stories/{scene.scene_id}_clean.mp4",
            "in_seconds": start,
            "duration_seconds": duration,
            "story_shot_size": scene.shot_size,
            "source_shot_size": scene.shot_size,
            "reel_shot_size": reel_size,
            "crop_scale_instruction": "Reframe the CLEAN master while preserving the approved action.",
            "reel_crop": ("tight-center", "left-detail", "right-detail", "wide-center")[position % 4],
            "crop_change_required": True,
        })
    return story_production.ReelEditPlan(
        edit_id="scout_runway_reel",
        hook="Naz responds without the previous context.",
        conclusion="The restored path makes the prior context visible in the response.",
        shots=tuple(shots),
        beat_map=tuple(float(index) for index in range(16)),
    )


def _build_pack(
    selected: scout_reel.SelectedMaterial,
    material: Mapping[str, Any],
    *,
    variant_index: int = 0,
    director_treatment: story_production.DirectorTreatment | None = None,
) -> story_production.StoryPackPlan:
    plan = _editorial_plan(selected)
    treatment = director_treatment or story_production.parse_reels_director_response(
        json.dumps(_director_payload(plan, variant_index), ensure_ascii=False),
        plan,
        SCENE_FACTS,
        variant_index=variant_index,
    )
    pack = story_production.plan_story_pack(
        plan, SCENE_FACTS, variant_index=variant_index, director_treatment=treatment
    )
    stored_scenes = scout_reel._validate_scenes(material)
    scenes = tuple(
        dataclasses.replace(scene, story_overlay=str(stored["screen_text"]))
        for scene, stored in zip(pack.scenes, stored_scenes)
    )
    pack = dataclasses.replace(
        pack,
        scenes=scenes,
        reel_edits=(_reel_edit(scenes),),
        caption_plan={"main": str(material["caption"]), "short": str(material["caption"])},
        music_plan={
            "tags": [],
            "allowlist_required": False,
            "consume_publication_rotation": False,
            "selected_track": None,
            "mode": "voice_over_only",
        },
    )
    story_production.validate_story_pack(pack)
    return pack


def _bridge_record(
    selected: scout_reel.SelectedMaterial,
    material: Mapping[str, Any],
    pack: story_production.StoryPackPlan,
    *,
    bridge_request_id: str,
    created_timestamp: str,
) -> dict[str, Any]:
    scenes = scout_reel._validate_scenes(material)
    return {
        "schema_version": BRIDGE_SCHEMA,
        "admin_id": selected.admin_id,
        "scout_run_id": selected.run_id,
        "candidate_id": selected.candidate_id,
        "selection_id": selected.selection_id,
        "selection_digest": _digest_bytes((selected.selection_dir / "selection.json").read_bytes()),
        "ready_material_digest": selected.ready_material_artifact_digest,
        "title": selected.title,
        "voice_over_digest": selected.voice_over_digest,
        "scene_content_digests": [
            _digest_bytes(_canonical(dict(scene))) for scene in scenes
        ],
        "duration_seconds": selected.duration_seconds,
        "output_language": "ru",
        "story_pack_id": pack.plan_id,
        "bridge_request_id": bridge_request_id,
        "created_timestamp": created_timestamp,
        "local_storyboard": {
            "render_profile": scout_reel.RENDER_PROFILE,
            "artifact_role": "local_storyboard",
            "publishable": False,
            "superseded_by_runway_flow": True,
        },
    }


def _result(record: Mapping[str, Any], bridge_path: Path, manifest_path: Path, created: bool) -> ScoutRunwayPack:
    manifest = story_production.read_manifest(manifest_path)
    routes = tuple(str(job["model_route"]["selected_model"]) for job in manifest["scene_jobs"])
    credits = 5 * len(routes) + sum(
        int(job["planned_duration_seconds"]) * story_production.RUNWAY_VIDEO_CREDITS_PER_SECOND[route]
        for job, route in zip(manifest["scene_jobs"], routes)
    )
    return ScoutRunwayPack(
        bridge_request_id=str(record["bridge_request_id"]),
        bridge_digest=_digest_bytes(_canonical(record)),
        plan_id=str(record["story_pack_id"]),
        selection_id=str(record["selection_id"]),
        title=str(record["title"]),
        duration_seconds=int(record["duration_seconds"]),
        scene_count=len(record["scene_content_digests"]),
        model_routes=routes,
        keyframe_jobs=len(routes),
        video_jobs=len(routes),
        credit_estimate=credits,
        bridge_path=bridge_path,
        manifest_path=manifest_path,
        created=created,
    )


def _attach_extensions(
    pack_dir: Path,
    record: Mapping[str, Any],
    selected: scout_reel.SelectedMaterial,
    material: Mapping[str, Any],
) -> Path:
    manifest_path = pack_dir / "story_manifest.json"
    with StoryPackLock(pack_dir):
        manifest = story_production.read_manifest(manifest_path)
        extension = {
            "bridge_schema": BRIDGE_SCHEMA,
            "bridge_digest": _digest_bytes(_canonical(record)),
            "bridge_request_id": record["bridge_request_id"],
            "selection_id": selected.selection_id,
            "title": selected.title,
            "duration_seconds": selected.duration_seconds,
            "scene_count": selected.scene_count,
            "local_storyboard": dict(record["local_storyboard"]),
        }
        voice_plan = {
            "schema_version": VOICE_SCHEMA,
            "text": str(material["reel_voice_over"]),
            "text_digest": selected.voice_over_digest,
            "path": "voice/scout-voice.opus",
            "max_calls": 1,
            "calls": 0,
            "status": "awaiting_approval",
            "audio_digest": None,
            "music_present": False,
        }
        composition = {
            "schema_version": COMPOSITION_SCHEMA,
            "mode": "voice_over_only",
            "duration_seconds": 15,
            "scene_count": 5,
            "screen_text_digests": [
                _digest_text(str(scene["screen_text"]))
                for scene in scout_reel._validate_scenes(material)
            ],
            "music_present": False,
        }
        for key, value in (
            ("scout_runway_bridge", extension),
            ("scout_composition", composition),
        ):
            existing = manifest.get(key)
            if existing is not None and existing != value:
                raise ScoutRunwayError("content_scout_runway_manifest_conflict")
            manifest[key] = value
        existing_voice = manifest.get("voice_over_plan")
        if existing_voice is None:
            manifest["voice_over_plan"] = voice_plan
        elif (
            not isinstance(existing_voice, dict)
            or existing_voice.get("schema_version") != VOICE_SCHEMA
            or existing_voice.get("text") != voice_plan["text"]
            or existing_voice.get("text_digest") != voice_plan["text_digest"]
            or existing_voice.get("path") != voice_plan["path"]
            or existing_voice.get("max_calls") != 1
            or existing_voice.get("music_present") is not False
            or existing_voice.get("calls") not in {0, 1}
            or existing_voice.get("status") not in {"awaiting_approval", "submitting", "ready"}
        ):
            raise ScoutRunwayError("content_scout_runway_manifest_conflict")
        story_production.atomic_json(manifest_path, manifest)
    if not story_production.manifest_has_current_production_contract(
        story_production.read_manifest(manifest_path)
    ):
        raise ScoutRunwayError("content_scout_runway_story_contract_invalid")
    return manifest_path


def create_runway_pack(
    state_root: Path,
    scout_root: Path,
    story_pack_root: Path,
    selection_id: str,
    *,
    admin_id: int,
    expected_admin_id: int,
    bridge_request_id: str,
    risk_detector: scout.RiskDetector,
    created_timestamp: str | None = None,
) -> ScoutRunwayPack:
    if type(bridge_request_id) is not str or not BRIDGE_REQUEST_RE.fullmatch(bridge_request_id):
        raise ScoutRunwayError("content_scout_runway_request_invalid")
    selected = scout_reel.load_selection(
        state_root, selection_id, admin_id=admin_id, expected_admin_id=expected_admin_id
    )
    _run, material = scout_reel.load_selected_ready_material(
        scout_root, selected, risk_detector=risk_detector
    )
    pack = _build_pack(selected, material)
    timestamp = scout_reel._timestamp(created_timestamp)
    record = _bridge_record(
        selected, material, pack,
        bridge_request_id=bridge_request_id,
        created_timestamp=timestamp,
    )
    bridge_path = selected.selection_dir / "runway" / f"{_digest_text(bridge_request_id)}.json"
    if bridge_path.exists():
        existing_record = scout_reel._read_json(bridge_path)
        if (
            {key: value for key, value in existing_record.items() if key != "created_timestamp"}
            != {key: value for key, value in record.items() if key != "created_timestamp"}
        ):
            raise ScoutRunwayError("content_scout_runway_request_conflict")
        record = existing_record
        created = False
    else:
        created = _write_exact(bridge_path, record, "content_scout_runway_request_conflict")
    pack_dir = story_production.persist_story_queue(pack, story_pack_root)
    manifest_path = _attach_extensions(pack_dir, record, selected, material)
    return _result(record, bridge_path, manifest_path, created)


def create_variant(
    state_root: Path,
    scout_root: Path,
    story_pack_root: Path,
    current_plan_id: str,
    *,
    admin_id: int,
    expected_admin_id: int,
    risk_detector: scout.RiskDetector,
    director_treatment: story_production.DirectorTreatment,
) -> ScoutRunwayPack:
    current, current_path = load_bridge_for_plan(story_pack_root, current_plan_id)
    if (
        current.get("approval", {}).get("status") != "awaiting_approval"
        or any(job.get("external_job_id") or job.get("keyframe_external_job_id") for job in current.get("scene_jobs", []))
    ):
        raise ScoutRunwayError("content_scout_runway_variant_unavailable")
    selection_id = str(current["scout_runway_bridge"]["selection_id"])
    selected = scout_reel.load_selection(
        state_root, selection_id, admin_id=admin_id, expected_admin_id=expected_admin_id
    )
    _run, material = scout_reel.load_selected_ready_material(
        scout_root, selected, risk_detector=risk_detector
    )
    variant_index = int(current.get("variant_index", 0)) + 1
    pack = _build_pack(
        selected,
        material,
        variant_index=variant_index,
        director_treatment=director_treatment,
    )
    request_id = f"scout-runway-{selection_id[4:]}-v{variant_index}"
    record = _bridge_record(
        selected,
        material,
        pack,
        bridge_request_id=request_id,
        created_timestamp=scout_reel._timestamp(),
    )
    bridge_path = selected.selection_dir / "runway" / f"{_digest_text(request_id)}.json"
    created = _write_exact(bridge_path, record, "content_scout_runway_request_conflict")
    new_dir = story_production.persist_story_queue(pack, story_pack_root)
    manifest_path = _attach_extensions(new_dir, record, selected, material)
    with StoryPackLock(current_path.parent):
        old = story_production.read_manifest(current_path)
        if old.get("approval", {}).get("status") != "awaiting_approval":
            raise ScoutRunwayError("content_scout_runway_variant_unavailable")
        old["approval"]["status"] = "superseded"
        old["approval"]["superseded_at"] = scout_reel._timestamp()
        old["pack_status"] = "superseded"
        old["superseded_by_plan_id"] = pack.plan_id
        story_production.atomic_json(current_path, old)
    return _result(record, bridge_path, manifest_path, created)


def approval_card_text(pack: ScoutRunwayPack) -> str:
    models = "\n".join(
        f"- Сцена {index}: {model}" for index, model in enumerate(pack.model_routes, start=1)
    )
    return (
        f"✅ Материал готов к Runway\n\n{pack.title}\n\n"
        f"{pack.duration_seconds} секунд · {pack.scene_count} сцен\n\n"
        "Визуальная концепция: единый Naz AI Lab; сохранённые модули памяти, "
        "разорванный и восстановленный сигнальный путь, раздельные пользовательские линии.\n\n"
        f"Модели:\n{models}\n\n"
        f"Будут созданы:\n- {pack.keyframe_jobs} ключевых кадров в Runway (gen4_image);\n"
        f"- {pack.video_jobs} видеосцен в Runway;\n"
        "- один приватный Reel с русской озвучкой;\n- музыка не используется.\n\n"
        f"Оценка: {pack.credit_estimate} Runway credits.\n"
        "Платные вызовы начнутся только после подтверждения."
    )


def load_bridge_for_plan(story_pack_root: Path, plan_id: str) -> tuple[dict[str, Any], Path]:
    if type(plan_id) is not str or not PLAN_ID_RE.fullmatch(plan_id):
        raise ScoutRunwayError("content_scout_runway_plan_invalid")
    manifest_path = story_pack_root / plan_id / "story_manifest.json"
    manifest = story_production.read_manifest(manifest_path)
    bridge = manifest.get("scout_runway_bridge")
    if (
        type(bridge) is not dict
        or bridge.get("bridge_schema") != BRIDGE_SCHEMA
        or bridge.get("selection_id") is None
        or type(bridge.get("bridge_digest")) is not str
        or not DIGEST_RE.fullmatch(bridge["bridge_digest"])
        or manifest.get("plan_id") != plan_id
    ):
        raise ScoutRunwayError("content_scout_runway_bridge_invalid")
    return manifest, manifest_path


def reserve_voice_call(story_pack_root: Path, plan_id: str) -> tuple[str, str] | None:
    manifest, path = load_bridge_for_plan(story_pack_root, plan_id)
    try:
        with StoryPackLock(path.parent):
            manifest = story_production.read_manifest(path)
            if manifest.get("approval", {}).get("status") != "approved":
                raise ScoutRunwayError("content_scout_runway_approval_required")
            voice = manifest.get("voice_over_plan")
            if type(voice) is not dict or voice.get("schema_version") != VOICE_SCHEMA:
                raise ScoutRunwayError("content_scout_runway_voice_invalid")
            if voice.get("status") == "ready":
                return None
            if voice.get("status") != "awaiting_approval" or voice.get("calls") != 0:
                raise ScoutRunwayError("content_scout_runway_voice_call_unavailable")
            voice["calls"] = 1
            voice["status"] = "submitting"
            story_production.atomic_json(path, manifest)
            return str(voice["text"]), str(voice["text_digest"])
    except StoryPackLockError as exc:
        raise ScoutRunwayError("content_scout_runway_busy") from exc


def complete_voice_call(story_pack_root: Path, plan_id: str, audio: bytes) -> Path:
    if type(audio) is not bytes or not audio or len(audio) > 16 * 1024 * 1024:
        raise ScoutRunwayError("content_scout_runway_voice_invalid")
    manifest, path = load_bridge_for_plan(story_pack_root, plan_id)
    with StoryPackLock(path.parent):
        manifest = story_production.read_manifest(path)
        voice = manifest.get("voice_over_plan")
        if type(voice) is not dict or voice.get("status") != "submitting" or voice.get("calls") != 1:
            raise ScoutRunwayError("content_scout_runway_voice_call_unavailable")
        relative = str(voice.get("path", ""))
        destination = (path.parent / relative).resolve()
        if path.parent.resolve() not in destination.parents or destination.suffix != ".opus":
            raise ScoutRunwayError("content_scout_runway_voice_invalid")
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest = _digest_bytes(audio)
        if destination.exists():
            if destination.read_bytes() != audio:
                raise ScoutRunwayError("content_scout_runway_voice_conflict")
        else:
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o660)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(audio)
                handle.flush()
                os.fsync(handle.fileno())
        voice["status"] = "ready"
        voice["audio_digest"] = digest
        story_production.atomic_json(path, manifest)
        return destination
