"""Story-first planning and resumable dry-run storage for Naz.

No video provider or public publisher is implemented here.  CLEAN masters are
required inputs for future rendering; Story overlays and Reel EDLs are planned
without pretending that still images are video.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from editorial_orchestrator import EditorialPlan


STORY_SCHEMA = "naz-story-pack-v1"
RENDERER_UNAVAILABLE = "unavailable"
DRAMATURGIC_ROLES = (
    "hook", "problem", "hypothesis", "test", "result", "solution", "conclusion",
)
SHOT_SIZES = ("wide", "medium", "close", "macro")
CAMERA_MOTIONS = ("slow push", "controlled pan", "handheld follow", "locked with real subject motion")
SAFE_ZONES = ("upper-middle", "middle-left", "lower-middle above platform controls")
_SECRET_RE = re.compile(
    r"(?i)(?:token|api[_ -]?key|password|secret|authorization|bearer|private message|личн(?:ое|ая) сообщен)"
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
    story_overlay: str
    text_safe_zone: str
    music_cue: str
    source_fact_ref: str
    footage_type: str
    continuity_constraints: tuple[str, ...]
    secret_warning: bool
    copyright_warning: bool
    suggested_interactive_sticker: str


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
    source_ref: str
    central_thesis: str
    scene_count: int
    scenes: tuple[ScenePlan, ...]
    reel_edits: tuple[ReelEditPlan, ...]
    caption_plan: Mapping[str, str]
    music_plan: Mapping[str, Any]
    safety_flags: tuple[str, ...]
    copyright_flags: tuple[str, ...]
    renderer: str = RENDERER_UNAVAILABLE
    schema: str = STORY_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def renderer_status() -> str:
    # ffmpeg alone is not a scene renderer. This may become available only
    # after a real video-provider adapter and its credentials are implemented
    # and verified end to end.
    return RENDERER_UNAVAILABLE


def _rank(plan_id: str, key: str) -> int:
    return int(hashlib.sha256(f"{plan_id}|{key}".encode("utf-8")).hexdigest()[:12], 16)


def _safe_fact(value: str) -> str:
    text = " ".join(str(value).split())[:500]
    if not text or _SECRET_RE.search(text):
        raise StoryPlanError("unsafe or empty source fact")
    return text


def _roles(plan_id: str, count: int) -> list[str]:
    # Roles are a pool, not a fixed template. Keep a path with a beginning and
    # an earned end while deterministically rotating the middle.
    middle = list(DRAMATURGIC_ROLES[1:-1])
    middle.sort(key=lambda item: _rank(plan_id, item))
    start = "hook" if _rank(plan_id, "opening") % 2 == 0 else middle[0]
    end = "conclusion" if _rank(plan_id, "ending") % 2 == 0 else "result"
    candidates = [
        role for role in ("hook", *middle, "result", "conclusion")
        if role not in {start, end}
    ]
    return [start, *candidates[: max(0, count - 2)], end]


def _scene(
    plan: EditorialPlan,
    *,
    continuity_id: str,
    role: str,
    index: int,
    fact: str,
) -> ScenePlan:
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
        f"Start with the fact not yet resolved; end with a visibly changed state. "
        f"{plan.visual_relation} Canonical Naz continuity: {'; '.join(continuity)}. "
        "Real motion/action is mandatory. No text, captions, logos, platform buttons, stickers, "
        "reactions or watermarks."
    )
    return ScenePlan(
        scene_id=f"{index + 1:02d}_{role}",
        role=role,
        standalone_meaning=standalone,
        concrete_action=f"Perform and reveal the action documented by: {fact}"[:300],
        subject=subject,
        setting=setting,
        start_state="before the documented action changes the situation",
        end_state="after the documented result is physically visible",
        shot_size=SHOT_SIZES[_rank(plan.plan_id, f"shot:{index}") % len(SHOT_SIZES)],
        camera_motion=CAMERA_MOTIONS[_rank(plan.plan_id, f"motion:{index}") % len(CAMERA_MOTIONS)],
        duration_seconds=duration,
        clean_prompt=clean_prompt,
        story_overlay=overlay,
        text_safe_zone=SAFE_ZONES[_rank(plan.plan_id, f"safe:{index}") % len(SAFE_ZONES)],
        music_cue=f"cue {index + 1}: follow action change; tags={','.join(plan.track_tags)}",
        source_fact_ref=f"{plan.source_ref}#fact-{index + 1}",
        footage_type="generative",
        continuity_constraints=continuity,
        secret_warning=False,
        copyright_warning=False,
        suggested_interactive_sticker="question" if role in {"hypothesis", "test"} else "none",
    )


def _reel_edit(plan: EditorialPlan, scenes: tuple[ScenePlan, ...], *, short: bool) -> ReelEditPlan:
    order = list(range(len(scenes)))
    order.sort(key=lambda index: _rank(plan.plan_id, f"reel:{short}:{index}"))
    if order == list(range(len(scenes))):
        order = order[1:] + order[:1]
    shots = []
    for position, scene_index in enumerate(order):
        scene = scenes[scene_index]
        length = 0.4 + ((_rank(plan.plan_id, f"cut:{short}:{position}") % 17) / 10)
        source_shot_size = scene.shot_size
        source_index = SHOT_SIZES.index(source_shot_size)
        reel_shot_size = SHOT_SIZES[
            (source_index + 1 + (_rank(plan.plan_id, f"reframe:{short}:{position}") % 3))
            % len(SHOT_SIZES)
        ]
        shots.append(
            {
                # Legacy aliases remain additive for stored v1 manifests.
                "scene_id": scene.scene_id,
                "shot_size": reel_shot_size,
                "source_scene_id": scene.scene_id,
                "source": f"stories/{scene.scene_id}_clean.mp4",
                "in_seconds": 0.2 * (position % 4),
                "duration_seconds": round(min(2.0, length), 1),
                "source_shot_size": source_shot_size,
                "reel_shot_size": reel_shot_size,
                "crop_scale_instruction": (
                    f"Reframe and scale the CLEAN {source_shot_size} master to a "
                    f"{reel_shot_size} Reel fragment; preserve the documented action."
                ),
            }
        )
    if short:
        shots = shots[: max(3, len(shots) - 1)]
    beat_map = tuple(round(index * 0.8, 1) for index in range(len(shots) + 1))
    return ReelEditPlan(
        edit_id="reel_edit_short" if short else "reel_edit_main",
        hook=f"Reel hook: reveal the first visible contradiction in {plan.topic}"[:160],
        conclusion=f"New Reel conclusion: {plan.ending} after the documented result"[:200],
        shots=tuple(shots),
        beat_map=beat_map,
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
    scenes = tuple(
        _scene(plan, continuity_id=continuity_id, role=role, index=index, fact=facts[index])
        for index, role in enumerate(roles)
    )
    pack = StoryPackPlan(
        plan_id=plan.plan_id,
        continuity_id=continuity_id,
        source_ref=plan.source_ref,
        central_thesis=plan.thesis_direction,
        scene_count=count,
        scenes=scenes,
        reel_edits=(
            _reel_edit(plan, scenes, short=False),
            _reel_edit(plan, scenes, short=True),
        ),
        caption_plan={
            "main": f"{plan.hook} — {plan.thesis_direction}",
            "short": f"{plan.ending}: {plan.topic}",
        },
        music_plan={
            "tags": list(plan.track_tags),
            "allowlist_required": True,
            "shared_last_8_required": True,
            "selected_track": None,
        },
        safety_flags=("safe_source_refs_only", "no_private_content", "no_drawn_interactive_ui"),
        copyright_flags=("source_facts_only", "no_unlicensed_footage_selected"),
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
        if not scene.story_overlay or not scene.text_safe_zone:
            raise StoryPlanError("STORY overlay contract is incomplete")
        if pack.continuity_id not in " ".join(scene.continuity_constraints):
            raise StoryPlanError("visual continuity mismatch")
    for edit in pack.reel_edits:
        if not edit.hook or not edit.conclusion or len(edit.shots) < 3:
            raise StoryPlanError("Reel needs its own hook, conclusion and EDL")
        durations = [float(item["duration_seconds"]) for item in edit.shots]
        if not all(0.4 <= value <= 2.0 for value in durations):
            raise StoryPlanError("every Reel fragment must be 0.4..2.0 seconds")
        for item in edit.shots:
            source_size = str(item.get("source_shot_size", ""))
            reel_size = str(item.get("reel_shot_size", ""))
            if source_size not in SHOT_SIZES or reel_size not in SHOT_SIZES:
                raise StoryPlanError("Reel shot sizes must use the canonical enum")
            if not str(item.get("source_scene_id", "")) or not str(
                item.get("crop_scale_instruction", "")
            ):
                raise StoryPlanError("Reel fragment source and crop/scale instruction are required")
            if str(item["source_scene_id"]) not in {
                scene.scene_id for scene in pack.scenes
            }:
                raise StoryPlanError("Reel fragment references an unknown CLEAN scene")
        if not any(
            item["source_shot_size"] != item["reel_shot_size"]
            for item in edit.shots
        ):
            raise StoryPlanError("Reel must change shot size for at least one fragment")
        sequential = [scene.scene_id for scene in pack.scenes][: len(edit.shots)]
        actual = [str(item["source_scene_id"]) for item in edit.shots]
        if actual == sequential:
            raise StoryPlanError("Reel cannot be a sequential Story concatenation")


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    os.close(descriptor)
    temporary = Path(raw)
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _duration_in_range(value: Any, minimum: float, maximum: float) -> bool:
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return False
    return minimum <= duration <= maximum


def _manifest_has_current_reel_contract(payload: Mapping[str, Any]) -> bool:
    scenes = payload.get("scenes")
    edits = payload.get("reel_edits")
    if not isinstance(scenes, list) or not isinstance(edits, list):
        return False
    if not 4 <= len(scenes) <= 7 or not edits:
        return False
    scene_ids = [str(item.get("scene_id", "")) for item in scenes if isinstance(item, dict)]
    if len(scene_ids) != len(scenes) or len(set(scene_ids)) != len(scene_ids):
        return False
    for scene in scenes:
        if (
            str(scene.get("shot_size", "")) not in SHOT_SIZES
            or not _duration_in_range(scene.get("duration_seconds"), 4, 8)
        ):
            return False
    for edit in edits:
        if not isinstance(edit, dict) or not isinstance(edit.get("shots"), list):
            return False
        shots = edit["shots"]
        if len(shots) < 3:
            return False
        for shot in shots:
            if not isinstance(shot, dict):
                return False
            if not _duration_in_range(shot.get("duration_seconds"), 0.4, 2.0):
                return False
            if (
                str(shot.get("source_shot_size", "")) not in SHOT_SIZES
                or str(shot.get("reel_shot_size", "")) not in SHOT_SIZES
                or not str(shot.get("source_scene_id", ""))
                or not str(shot.get("crop_scale_instruction", ""))
                or str(shot.get("source_scene_id", "")) not in scene_ids
            ):
                return False
        if not any(
            shot["source_shot_size"] != shot["reel_shot_size"] for shot in shots
        ):
            return False
        if [str(shot["source_scene_id"]) for shot in shots] == scene_ids[: len(shots)]:
            return False
    return True


def _preserve_render_statuses(
    payload: dict[str, Any],
    existing: Mapping[str, Any],
) -> None:
    previous_outputs = existing.get("expected_outputs")
    current_outputs = payload.get("expected_outputs")
    if not isinstance(previous_outputs, dict) or not isinstance(current_outputs, dict):
        return
    previous_stories = previous_outputs.get("stories")
    current_stories = current_outputs.get("stories")
    if not isinstance(previous_stories, list) or not isinstance(current_stories, list):
        return
    statuses = {
        str(item.get("clean", "")): str(item.get("status", ""))
        for item in previous_stories
        if isinstance(item, dict) and item.get("clean") and item.get("status")
    }
    for item in current_stories:
        if isinstance(item, dict) and str(item.get("clean", "")) in statuses:
            item["status"] = statuses[str(item["clean"])]


def persist_dry_run(pack: StoryPackPlan, storage_root: Path) -> Path:
    """Create or resume an idempotent pack; never creates fake video files."""
    root = Path(storage_root).expanduser().resolve()
    pack_dir = root / pack.plan_id
    manifest = pack_dir / "story_manifest.json"
    existing: Mapping[str, Any] | None = None
    if manifest.is_file():
        existing = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(existing, dict) or existing.get("plan_id") != pack.plan_id:
            raise StoryPlanError("stored manifest plan_id mismatch")
        if _manifest_has_current_reel_contract(existing):
            return pack_dir
    (pack_dir / "stories").mkdir(parents=True, exist_ok=True)
    (pack_dir / "reels").mkdir(parents=True, exist_ok=True)
    payload = pack.to_dict()
    payload["expected_outputs"] = {
        "stories": [
            {
                "clean": f"stories/{scene.scene_id}_clean.mp4",
                "story": f"stories/{scene.scene_id}_story.mp4",
                "status": "awaiting_renderer",
            }
            for scene in pack.scenes
        ],
        "reels": ["reels/reel_edit_main.mp4", "reels/reel_edit_short.mp4"],
    }
    payload["overlay_policy"] = "local overlay on matching CLEAN master; no scene regeneration"
    payload["renderer"] = RENDERER_UNAVAILABLE
    if existing is not None:
        _preserve_render_statuses(payload, existing)
    _atomic_text(manifest, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    caption = (
        f"# Caption pack\n\nPlan ID: {pack.plan_id}\nContinuity ID: {pack.continuity_id}\n\n"
        f"Main: {pack.caption_plan['main']}\n\nShort: {pack.caption_plan['short']}\n"
    )
    _atomic_text(pack_dir / "caption_pack.md", caption)
    return pack_dir
