"""Deterministic Story-first planning and atomic queue manifests for Naz.

Planning is intentionally provider-free and fast.  Paid generation and local
media composition live in ``naz_story_worker.py`` and never run in a Telegram
scheduler callback.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from editorial_orchestrator import EditorialPlan


STORY_SCHEMA = "naz-story-pack-v2"
LEGACY_STORY_SCHEMA = "naz-story-pack-v1"
RENDERER_UNAVAILABLE = "unavailable"
DRAMATURGIC_ROLES = (
    "hook", "problem", "hypothesis", "test", "result", "solution", "conclusion",
)
SHOT_SIZES = ("macro", "close", "medium", "wide", "over-shoulder")
REEL_CROPS = ("tight-center", "left-detail", "right-detail", "wide-center")
CAMERA_MOTIONS = ("slow push", "controlled pan", "handheld follow", "locked with real subject motion")
SAFE_ZONES = ("upper-middle", "middle-left", "lower-middle above platform controls")
TERMINAL_SCENE_STATES = {"completed", "terminal_failed", "blocked_reference"}
SCENE_STATES = {
    "planned", "queued", "submitted", "in_progress", "downloaded", "composed",
    "completed", "retryable_failed", "terminal_failed", "blocked_reference",
}
_SECRET_RE = re.compile(
    r"(?i)(?:token|api[_ -]?key|password|secret|authorization|bearer|private message|"
    r"личн(?:ое|ая)\s+сообщен|sk-[a-z0-9_-]{10,}|\d{7,12}:[a-z0-9_-]{20,})"
)


class StoryPlanError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ScenePlan:
    scene_id: str
    role: str
    standalone_meaning: str
    concrete_action: str
    subject: str
    setting: str
    start_state: str
    end_state: str
    shot_size: str
    camera_motion: str
    duration_seconds: int
    clean_prompt: str
    provider_prompt: str
    story_overlay: str
    text_safe_zone: str
    music_cue: str
    source_fact_ref: str
    footage_type: str
    continuity_constraints: tuple[str, ...]
    secret_warning: bool
    copyright_warning: bool
    suggested_interactive_sticker: str
    requires_naz_reference: bool


@dataclass(frozen=True, slots=True)
class ReelEditPlan:
    edit_id: str
    hook: str
    conclusion: str
    shots: tuple[Mapping[str, Any], ...]
    beat_map: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class StoryPackPlan:
    plan_id: str
    continuity_id: str
    persona: str
    destination: str
    scheduled_slot: str
    rubric: str
    source_type: str
    source_ref: str
    central_thesis: str
    scene_count: int
    scenes: tuple[ScenePlan, ...]
    reel_edits: tuple[ReelEditPlan, ...]
    caption_plan: Mapping[str, str]
    music_plan: Mapping[str, Any]
    safety_flags: tuple[str, ...]
    copyright_flags: tuple[str, ...]
    policy_versions: Mapping[str, str]
    renderer: str = RENDERER_UNAVAILABLE
    schema: str = STORY_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def renderer_status() -> str:
    """Planning never promises renderer availability."""
    return RENDERER_UNAVAILABLE


def _rank(plan_id: str, key: str) -> int:
    return int(hashlib.sha256(f"{plan_id}|{key}".encode("utf-8")).hexdigest()[:12], 16)


def _safe_fact(value: str) -> str:
    text = " ".join(str(value).split())[:500]
    if not text or _SECRET_RE.search(text):
        raise StoryPlanError("unsafe or empty source fact")
    return text


def validate_provider_prompt(value: str) -> str:
    """Defense-in-depth check immediately before a prompt leaves the host."""
    text = " ".join(str(value).split())
    if not text or len(text) > 6000 or _SECRET_RE.search(text):
        raise StoryPlanError("provider_prompt_unsafe")
    return text


def _roles(plan_id: str, count: int) -> list[str]:
    middle = list(DRAMATURGIC_ROLES[1:-1])
    middle.sort(key=lambda item: _rank(plan_id, item))
    start = "hook" if _rank(plan_id, "opening") % 2 == 0 else middle[0]
    end = "conclusion" if _rank(plan_id, "ending") % 2 == 0 else "result"
    candidates = [role for role in ("hook", *middle, "result", "conclusion") if role not in {start, end}]
    return [start, *candidates[: max(0, count - 2)], end]


def _requires_reference(subject: str) -> bool:
    folded = subject.casefold()
    return any(word in folded for word in ("naz", "наз", "face", "portrait", "лицо", "портрет"))


def _scene(plan: EditorialPlan, *, continuity_id: str, role: str, index: int, fact: str) -> ScenePlan:
    duration = 4 + (_rank(plan.plan_id, f"duration:{index}") % 5)
    subject = plan.visual_subject_direction
    setting = f"A real Naz work chronology setting tied to fact {index + 1}"
    standalone = f"{role}: {fact}"[:180]
    overlay = standalone[:72]
    continuity = (
        f"continuity_id={continuity_id}",
        "same canonical Naz face, age, clothing and human-digital boundary across the pack",
        "Deep Black, Electric Blue, Cobalt, Ultraviolet and Ice Silver only",
        "optical glass, titanium, aluminium, carbon and technical ceramic",
        "no cheap cyberpunk, random robots, random people, elderly people or stock imagery",
    )
    clean_prompt = (
        f"Vertical 9:16 CLEAN video master. {plan.visual_mode}. Role: {role}. "
        f"Concrete action and verified fact: {fact}. Subject: {subject}. Setting: {setting}. "
        "Start with the fact not yet resolved; end with a visibly changed state. "
        f"{plan.visual_relation} Canonical Naz continuity: {'; '.join(continuity)}. "
        "Real motion/action is mandatory. No text, captions, logos, platform buttons, stickers, "
        "reactions or watermarks."
    )
    provider_prompt = (
        f"Vertical 9:16 CLEAN video master. {plan.visual_mode}. Role: {role}. "
        f"Subject: {subject}. Setting: {setting}. Camera: "
        f"{CAMERA_MOTIONS[_rank(plan.plan_id, f'motion:{index}') % len(CAMERA_MOTIONS)]}. "
        "Begin before the action changes the physical state and end after a clearly visible change. "
        f"{plan.visual_relation} Canonical Naz continuity: {'; '.join(continuity)}. "
        "Real motion/action is mandatory. No text, captions, logos, platform UI, stickers, "
        "reactions or watermarks. Do not depict messages, code, credentials or private records."
    )
    return ScenePlan(
        scene_id=f"{index + 1:02d}_{role}", role=role, standalone_meaning=standalone,
        concrete_action=f"Perform and reveal the action documented by: {fact}"[:300],
        subject=subject, setting=setting,
        start_state="before the documented action changes the situation",
        end_state="after the documented result is physically visible",
        shot_size=SHOT_SIZES[_rank(plan.plan_id, f"shot:{index}") % len(SHOT_SIZES)],
        camera_motion=CAMERA_MOTIONS[_rank(plan.plan_id, f"motion:{index}") % len(CAMERA_MOTIONS)],
        duration_seconds=duration, clean_prompt=clean_prompt, provider_prompt=provider_prompt,
        story_overlay=overlay,
        text_safe_zone=SAFE_ZONES[_rank(plan.plan_id, f"safe:{index}") % len(SAFE_ZONES)],
        music_cue=f"cue {index + 1}: follow action change; tags={','.join(plan.track_tags)}",
        source_fact_ref=f"{plan.source_ref}#fact-{index + 1}", footage_type="generative",
        continuity_constraints=continuity, secret_warning=False, copyright_warning=False,
        suggested_interactive_sticker="question" if role in {"hypothesis", "test"} else "none",
        requires_naz_reference=_requires_reference(subject),
    )


def _reel_edit(plan: EditorialPlan, scenes: tuple[ScenePlan, ...], *, short: bool) -> ReelEditPlan:
    order = list(range(len(scenes)))
    order.sort(key=lambda index: _rank(plan.plan_id, f"reel:{short}:{index}"))
    if order == list(range(len(scenes))):
        order = order[1:] + order[:1]
    if short:
        order = order[: max(3, len(order) - 1)]
    shots: list[dict[str, Any]] = []
    for position, scene_index in enumerate(order):
        scene = scenes[scene_index]
        length = round(0.4 + ((_rank(plan.plan_id, f"cut:{short}:{position}") % 17) / 10), 1)
        crop = REEL_CROPS[_rank(plan.plan_id, f"crop:{short}:{position}") % len(REEL_CROPS)]
        if position == 0 and crop == "wide-center":
            crop = "tight-center"
        shots.append({
            "scene_id": scene.scene_id,
            "source": f"stories/{scene.scene_id}_clean.mp4",
            "in_seconds": round(0.2 * (position % 4), 1),
            "duration_seconds": min(2.0, length),
            "story_shot_size": scene.shot_size,
            "reel_crop": crop,
            "crop_change_required": True,
        })
    beat_map = tuple(round(index * 0.4, 1) for index in range(1 + len(shots) * 5))
    return ReelEditPlan(
        edit_id="reel_edit_short" if short else "reel_edit_main",
        hook=f"Reel hook: reveal the first visible contradiction in {plan.topic}"[:160],
        conclusion=f"New Reel conclusion: {plan.ending} after the documented result"[:200],
        shots=tuple(shots), beat_map=beat_map,
    )


def plan_story_pack(plan: EditorialPlan, safe_facts: tuple[str, ...]) -> StoryPackPlan:
    if plan.production_mode != "story_first" or plan.content_format != "story_pack":
        raise StoryPlanError("EditorialPlan is not Story-first")
    facts = tuple(_safe_fact(item) for item in safe_facts)
    if len(facts) < 4:
        raise StoryPlanError("Story-first requires at least four safe causal facts")
    count = max(4, min(7, len(facts)))
    continuity_id = hashlib.sha256(f"naz|{plan.plan_id}|continuity".encode("utf-8")).hexdigest()[:20]
    roles = _roles(plan.plan_id, count)
    scenes = tuple(_scene(plan, continuity_id=continuity_id, role=role, index=i, fact=facts[i]) for i, role in enumerate(roles))
    pack = StoryPackPlan(
        plan_id=plan.plan_id, continuity_id=continuity_id, persona=plan.persona,
        destination=plan.platform, scheduled_slot=plan.slot, rubric=plan.rubric,
        source_type=plan.source_type, source_ref=plan.source_ref,
        central_thesis=plan.thesis_direction, scene_count=count, scenes=scenes,
        reel_edits=(_reel_edit(plan, scenes, short=False), _reel_edit(plan, scenes, short=True)),
        caption_plan={"main": f"{plan.hook} — {plan.thesis_direction}", "short": f"{plan.ending}: {plan.topic}"},
        music_plan={"tags": list(plan.track_tags), "allowlist_required": True,
                    "consume_publication_rotation": False, "selected_track": None},
        safety_flags=("safe_source_refs_only", "no_private_content", "no_drawn_interactive_ui"),
        copyright_flags=("source_facts_only", "no_unlicensed_footage_selected"),
        policy_versions={
            "orchestrator": plan.orchestrator_version, "content": plan.content_policy_version,
            "visual": plan.visual_policy_version, "music": plan.music_policy_version,
        },
        renderer=renderer_status(),
    )
    validate_story_pack(pack)
    return pack


def validate_story_pack(pack: StoryPackPlan) -> None:
    if not 4 <= pack.scene_count <= 7 or pack.scene_count != len(pack.scenes):
        raise StoryPlanError("scene count must be 4..7")
    if pack.renderer not in {"available", RENDERER_UNAVAILABLE}:
        raise StoryPlanError("unknown renderer status")
    for scene in pack.scenes:
        if not 4 <= scene.duration_seconds <= 8:
            raise StoryPlanError("Story duration must be 4..8 seconds")
        if scene.secret_warning or _SECRET_RE.search(scene.clean_prompt):
            raise StoryPlanError("secret warning in scene")
        if "9:16" not in scene.clean_prompt or "Real motion" not in scene.clean_prompt:
            raise StoryPlanError("CLEAN master contract is incomplete")
        if not validate_provider_prompt(scene.provider_prompt) or fact_text_in_provider_prompt(scene):
            raise StoryPlanError("provider prompt is not privacy-minimized")
        if not scene.story_overlay or not scene.text_safe_zone:
            raise StoryPlanError("STORY overlay contract is incomplete")
        if pack.continuity_id not in " ".join(scene.continuity_constraints):
            raise StoryPlanError("visual continuity mismatch")
    for edit in pack.reel_edits:
        if not edit.hook or not edit.conclusion or len(edit.shots) < 3:
            raise StoryPlanError("Reel needs its own hook, conclusion and EDL")
        if not all(0.4 <= float(item["duration_seconds"]) <= 2.0 for item in edit.shots):
            raise StoryPlanError("every Reel cut must be 0.4..2.0 seconds")
        sequential = [scene.scene_id for scene in pack.scenes][: len(edit.shots)]
        actual = [str(item["scene_id"]) for item in edit.shots]
        if actual == sequential:
            raise StoryPlanError("Reel cannot be a sequential Story concatenation")
        if not any(bool(item.get("crop_change_required")) and item.get("reel_crop") for item in edit.shots):
            raise StoryPlanError("Reel requires a real crop change")
        if any(not str(item.get("source", "")).endswith("_clean.mp4") for item in edit.shots):
            raise StoryPlanError("Reel may use CLEAN masters only")


def fact_text_in_provider_prompt(scene: ScenePlan) -> bool:
    """Ensure the outbound prompt does not repeat the local factual sentence."""
    _, _, fact = scene.standalone_meaning.partition(":")
    normalized_fact = " ".join(fact.split()).casefold()
    return bool(len(normalized_fact) >= 24 and normalized_fact in scene.provider_prompt.casefold())


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
    os.close(descriptor)
    temporary = Path(raw)
    try:
        temporary.write_text(value, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_create_text(path: Path, value: str) -> bool:
    """Publish a new file atomically without ever replacing an old artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
            return True
        except FileExistsError:
            return False
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def read_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") not in {STORY_SCHEMA, LEGACY_STORY_SCHEMA}:
        raise StoryPlanError("unsupported story manifest schema")
    return payload


def update_manifest(path: Path, mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    payload = read_manifest(path)
    mutator(payload)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_json(path, payload)
    return payload


def _production_payload(pack: StoryPackPlan) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    payload = pack.to_dict()
    payload.update({
        "created_at": now, "updated_at": now, "pack_status": "queued",
        "renderer": {"status": "queued", "name": "ffmpeg"},
        "composer": {"status": "planned", "name": "ffmpeg"},
        "provider": {"name": None, "model": None},
        "visual_identity_qa": {"status": "not_run", "blocking": False},
    })
    payload["scene_jobs"] = [{
        "scene_id": scene.scene_id, "state": "queued", "external_job_id": None,
        "attempts": 0, "submitted_at": None, "provider_status": None,
        "planned_duration_seconds": scene.duration_seconds, "actual_duration_seconds": None,
        "clean_path": f"stories/{scene.scene_id}_clean.mp4",
        "story_path": f"stories/{scene.scene_id}_story.mp4",
        "master_relation": "STORY is a local overlay of this CLEAN master",
        "media_probe": None, "clean_checksum": None, "story_checksum": None,
        "technical_qa": {"status": "not_run"},
        "visual_identity_qa": {"status": "not_run", "blocking": False},
        "failure_code": None, "requires_naz_reference": scene.requires_naz_reference,
    } for scene in pack.scenes]
    payload["reel_jobs"] = [{
        "edit_id": edit.edit_id, "state": "planned",
        "path": f"reels/{edit.edit_id}.mp4", "media_probe": None,
        "checksum": None, "failure_code": None,
    } for edit in pack.reel_edits]
    payload["expected_outputs"] = {
        "stories": [{"clean": job["clean_path"], "story": job["story_path"], "status": "queued"} for job in payload["scene_jobs"]],
        "reels": [job["path"] for job in payload["reel_jobs"]],
    }
    payload["overlay_policy"] = "local overlay on matching CLEAN master; no scene regeneration"
    return payload


def persist_story_queue(pack: StoryPackPlan, storage_root: Path) -> Path:
    """Atomically create a resumable v2 queue item, or return an existing pack."""
    root = Path(storage_root).expanduser().resolve()
    pack_dir = (root / pack.plan_id).resolve()
    if root not in pack_dir.parents:
        raise StoryPlanError("unsafe pack path")
    manifest = pack_dir / "story_manifest.json"
    (pack_dir / "stories").mkdir(parents=True, exist_ok=True)
    (pack_dir / "reels").mkdir(parents=True, exist_ok=True)
    created = _atomic_create_text(
        manifest,
        json.dumps(_production_payload(pack), ensure_ascii=False, indent=2) + "\n",
    )
    existing = read_manifest(manifest)
    if existing.get("plan_id") != pack.plan_id:
        raise StoryPlanError("stored manifest plan_id mismatch")
    caption = (
        f"# Caption pack\n\nPlan ID: {pack.plan_id}\nContinuity ID: {pack.continuity_id}\n\n"
        f"Main: {pack.caption_plan['main']}\n\nShort: {pack.caption_plan['short']}\n"
    )
    if created:
        _atomic_create_text(pack_dir / "caption_pack.md", caption)
    elif not (pack_dir / "caption_pack.md").exists():
        _atomic_create_text(pack_dir / "caption_pack.md", caption)
    return pack_dir


def persist_dry_run(pack: StoryPackPlan, storage_root: Path) -> Path:
    """Backward-compatible name; persists a real v2 queue but makes no API call."""
    return persist_story_queue(pack, storage_root)
