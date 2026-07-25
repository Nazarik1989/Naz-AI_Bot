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
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from editorial_orchestrator import EditorialPlan
from story_pack_lock import ensure_private_group_access


STORY_SCHEMA = "naz-story-pack-v4"
PREVIOUS_STORY_SCHEMA = "naz-story-pack-v3"
OLDER_STORY_SCHEMA = "naz-story-pack-v2"
LEGACY_STORY_SCHEMA = "naz-story-pack-v1"
RENDERER_UNAVAILABLE = "unavailable"
DRAMATURGIC_ROLES = (
    "hook", "problem", "hypothesis", "test", "result", "solution", "conclusion",
)
SHOT_SIZES = ("wide", "medium", "close", "macro")
REFERENCE_ROLES = ("none", "frontal_identity", "three_quarter_identity")
REEL_CROPS = ("tight-center", "left-detail", "right-detail", "wide-center")
CAMERA_MOTIONS = ("slow push", "controlled pan", "handheld follow", "locked with real subject motion")
SAFE_ZONES = ("upper-middle", "middle-left", "lower-middle above platform controls")
VISUAL_TREATMENTS = {
    "constraint_recovery": {
        "label": "constraint recovery through physical rerouting",
        "beats": {
            "hook": ("a vast Naz AI Lab server hall during a controlled blackout", "Naz crosses the dark cold aisle carrying one blue task light", "one physical route becomes visible in the darkness"),
            "problem": ("an isolated power-routing chamber beneath the Naz AI Lab", "Naz finds one disconnected titanium bus coupler among dormant machinery", "the broken physical link is exposed"),
            "hypothesis": ("a smoked-glass diagnostics bridge overlooking the server hall", "Naz traces one fibre path by hand from the dead rack to a transparent routing core", "one repair path is selected"),
            "test": ("a narrow maintenance gantry between black server towers", "Naz bridges two blue anodized modules with a precision mechanical connector", "a cold blue pulse crosses the bridge"),
            "result": ("the central infrastructure chamber of Naz AI Lab", "the transparent routing core engages while Naz watches its internal mechanism turn", "successive server rows wake without any screen interface"),
            "solution": ("a circular distribution vault with visible fibre paths", "Naz locks the recovered module into the physical network spine", "the lab settles into a stable energy rhythm"),
            "conclusion": ("the observation platform above the restored Naz AI Lab", "Naz stands above the living infrastructure as one blue arc travels through it", "the system continues working beyond the original limit"),
        },
    },
    "prototype_assembly": {
        "label": "idea becoming a physical prototype",
        "beats": {
            "hook": ("a dark material vault in the Naz AI Lab fabrication wing", "Naz selects one raw titanium blank from negative space", "the unfinished material becomes the single focus"),
            "problem": ("a precision machining cell behind smoked optical glass", "a milling head stops above an incomplete mechanical cavity", "the missing tolerance is physically visible"),
            "hypothesis": ("a carbon worktable under a circular cold task light", "Naz aligns a transparent polymer shell over the titanium core", "the intended internal geometry becomes clear"),
            "test": ("an enclosed prototype assembly bay", "Naz seats the machined core into blue anodized rails", "the exact joints close without force"),
            "result": ("a cold technical-ceramic validation pedestal", "the assembled object opens and reveals its working internal mechanism", "the prototype performs one measurable motion"),
            "solution": ("a sparse Naz AI Lab integration bench", "Naz connects the validated object to one restrained power umbilical", "the object operates as part of a larger system"),
            "conclusion": ("a black exhibition chamber with no audience", "Naz releases the finished prototype and steps into shadow", "the physical object remains active under an ice-silver edge"),
        },
    },
    "network_coordination": {
        "label": "separate parts becoming one connected system",
        "beats": {
            "hook": ("a monumental dark junction hall linking several Naz AI Lab wings", "Naz enters between separated islands of machinery", "the distance between the systems becomes tangible"),
            "problem": ("a glass corridor with interrupted blue fibre paths", "Naz examines a precise mechanical junction that does not meet", "the conflicting connection is isolated"),
            "hypothesis": ("an elevated topology chamber built from glass and titanium", "Naz rotates one physical routing ring to align three distant paths", "a coherent route appears without a HUD"),
            "test": ("a relay bay with three transparent technical modules", "Naz closes the first junction and waits for a physical response downstream", "the far module answers with one cold pulse"),
            "result": ("the central systems atrium of Naz AI Lab", "multiple mechanisms begin moving in a restrained shared rhythm", "separate machines behave as one system"),
            "solution": ("a long infrastructure spine beneath the laboratory", "Naz secures the final silver coupling between the connected branches", "the full route becomes mechanically continuous"),
            "conclusion": ("a high observation bridge above the linked laboratory", "Naz walks beside the single blue path now joining every wing", "coordination remains visible as physical structure"),
        },
    },
    "field_experiment": {
        "label": "laboratory prototype entering the physical world",
        "beats": {
            "hook": ("a rain-dark rooftop relay platform above a restrained future city", "Naz carries a sealed optical-glass prototype into cold wind", "the laboratory object meets an uncontrolled environment"),
            "problem": ("a concrete service level beneath the rooftop array", "the prototype vibrates against an unstable physical mounting", "the environmental failure is visible"),
            "hypothesis": ("a transparent mobile Naz AI Lab module parked beside the relay", "Naz compares two mechanical dampers by touch", "one field adjustment is selected"),
            "test": ("the exposed rooftop mast under blue-violet edge light", "Naz installs the revised carbon brace while the relay moves in wind", "the structure stops oscillating"),
            "result": ("a wide platform between city darkness and the active relay", "the optical core opens and sends one restrained cobalt beam", "the field prototype holds a stable state"),
            "solution": ("a narrow maintenance bridge across the rooftop system", "Naz locks the tested brace into the permanent assembly", "the relay becomes a believable working object"),
            "conclusion": ("the city-facing edge of the completed relay platform", "Naz leaves the functioning device alone against the skyline", "one precise signal continues through the night"),
        },
    },
}
OBJECT_ONLY_ACTIONS = {
    "hook": ("one physical mechanism emerges from darkness under a cold task light", "the object becomes the single readable subject"),
    "problem": ("one joint stops before reaching its exact mechanical seat", "the physical mismatch is exposed"),
    "hypothesis": ("two material interfaces align around one visible tolerance gap", "one testable geometry becomes clear"),
    "test": ("the mechanism performs one controlled load cycle", "the critical joint reaches a stable state"),
    "result": ("the transparent shell reveals the internal assembly engaging", "the mechanism completes its intended motion"),
    "solution": ("the validated module locks into the surrounding physical system", "the complete assembly operates as one object"),
    "conclusion": ("the working object remains alone in negative space", "one restrained blue arc marks the finished state"),
}
TERMINAL_SCENE_STATES = {
    "completed", "terminal_failed", "blocked_reference", "submit_ambiguous",
}
SCENE_STATES = {
    "planned", "queued", "submitted", "in_progress", "downloaded", "composed",
    "completed", "retryable_failed", "terminal_failed", "blocked_reference",
    "awaiting_secondary_approval", "submitting", "submit_ambiguous",
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
    keyframe_prompt: str
    identity_reference_usage: str
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
    reference_role: str


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
    base_plan_id: str
    variant_index: int
    continuity_id: str
    persona: str
    destination: str
    scheduled_slot: str
    rubric: str
    source_type: str
    source_ref: str
    safe_facts: tuple[str, ...]
    editorial_plan: Mapping[str, Any]
    central_thesis: str
    visual_concept: str
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


def _visual_treatment(plan: EditorialPlan, facts: tuple[str, ...]) -> str:
    semantic_input = " ".join((
        plan.topic, plan.thesis_direction, plan.tension, plan.semantic_theme,
        plan.semantic_card, plan.facet, plan.imagery, *facts,
    )).casefold()
    keyword_groups = {
        "constraint_recovery": (
            "credit", "кредит", "limit", "лимит", "законч", "blocked", "блокир",
            "pause", "пауза", "недоступ", "restriction", "огранич", "exhaust",
        ),
        "prototype_assembly": (
            "prototype", "прототип", "build", "собир", "assembly", "сборк",
            "material", "материал", "fabricat", "производ", "launch", "запуск",
        ),
        "network_coordination": (
            "team", "команд", "network", "сеть", "relay",
            "связ", "contact", "контакт", "coordination", "координ",
        ),
        "field_experiment": (
            "city", "город", "street", "улиц", "field", "полев", "outside",
            "внешн", "travel", "поезд", "weather", "погод",
        ),
    }
    scored = {
        key: sum(semantic_input.count(word) for word in words)
        for key, words in keyword_groups.items()
    }
    best = max(scored.values())
    if best:
        candidates = sorted(key for key, score in scored.items() if score == best)
    else:
        candidates = sorted(VISUAL_TREATMENTS)
    return candidates[_rank(plan.plan_id, "visual-treatment") % len(candidates)]


def _scene(
    plan: EditorialPlan, *, continuity_id: str, role: str, index: int, fact: str,
    treatment_key: str,
) -> ScenePlan:
    # Both configured Runway tiers accept a five-second master.  Keeping the
    # provider master fixed also makes Reel timing and credit accounting exact.
    duration = 5
    requires_reference = _requires_reference(plan.visual_subject_direction)
    subject = "Naz, the same adult human founder from the approved identity reference" if requires_reference else "one physical Naz AI Lab prototype"
    shot_size = SHOT_SIZES[_rank(plan.plan_id, f"shot:{index}") % len(SHOT_SIZES)]
    reference_role = (
        "three_quarter_identity"
        if requires_reference and shot_size in {"wide", "medium"}
        else "frontal_identity"
        if requires_reference
        else "none"
    )
    treatment = VISUAL_TREATMENTS[treatment_key]
    setting, action, end_state = treatment["beats"][role]
    if not requires_reference:
        action, end_state = OBJECT_ONLY_ACTIONS[role]
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
    identity_instruction = (
        "Use @Naz only for facial identity, age and human build; replace the reference background, pose, "
        "lighting and framing completely. "
        if requires_reference else "No person is present. "
    )
    keyframe_prompt = (
        f"Vertical cinematic scene composed for a 9:16 centre crop. {identity_instruction}"
        f"Visual concept: {treatment['label']}. Location: {setting}. Subject: {subject}. Exact frozen action: {action}. "
        f"Shot: {shot_size}; leave motion room for a {CAMERA_MOTIONS[_rank(plan.plan_id, f'motion:{index}') % len(CAMERA_MOTIONS)]}. "
        "Human intelligence / machine precision. Deep Black #020309, Midnight Blue #070B20, "
        "Electric Blue #185CFF, Ultraviolet #762DFF and Ice Silver #D7E5FF. "
        "Optical glass, polished titanium, blue anodized aluminium, carbon and technical ceramic. "
        "Deep black background, cold blue rim light, blue-violet edge, high local contrast, one main subject. "
        "Photorealistic physical laboratory, materially believable, no text, logos, HUD, code, copper, gold, "
        "cheap neon cyberpunk, random circuit boards, robots or extra people."
    )
    provider_prompt = (
        f"Animate the supplied directed keyframe as a vertical 9:16 CLEAN video master. Role: {role}. "
        f"Keep the exact subject, Naz identity, laboratory architecture, materials and lighting from the keyframe. "
        f"Physical action: {action}. Camera: "
        f"{CAMERA_MOTIONS[_rank(plan.plan_id, f'motion:{index}') % len(CAMERA_MOTIONS)]}. "
        f"Begin in the supplied pose and end when {end_state}. "
        "Natural restrained human movement and one clear physical state change are mandatory. "
        "No scene replacement, morphing, extra people, text, logos, HUD, platform UI, code or watermarks."
    )
    return ScenePlan(
        scene_id=f"{index + 1:02d}_{role}", role=role, standalone_meaning=standalone,
        concrete_action=action,
        subject=subject, setting=setting,
        start_state="before the documented action changes the situation",
        end_state=end_state,
        shot_size=shot_size,
        camera_motion=CAMERA_MOTIONS[_rank(plan.plan_id, f"motion:{index}") % len(CAMERA_MOTIONS)],
        duration_seconds=duration, clean_prompt=clean_prompt, provider_prompt=provider_prompt,
        keyframe_prompt=keyframe_prompt,
        identity_reference_usage="identity_only" if requires_reference else "none",
        story_overlay=overlay,
        text_safe_zone=SAFE_ZONES[_rank(plan.plan_id, f"safe:{index}") % len(SAFE_ZONES)],
        music_cue=f"cue {index + 1}: follow action change; tags={','.join(plan.track_tags)}",
        source_fact_ref=f"{plan.source_ref}#fact-{index + 1}", footage_type="generative",
        continuity_constraints=continuity, secret_warning=False, copyright_warning=False,
        suggested_interactive_sticker="question" if role in {"hypothesis", "test"} else "none",
        requires_naz_reference=requires_reference, reference_role=reference_role,
    )


def _reel_edit(plan: EditorialPlan, scenes: tuple[ScenePlan, ...], *, short: bool) -> ReelEditPlan:
    order = list(range(len(scenes)))
    order.sort(key=lambda index: _rank(plan.plan_id, f"reel:{short}:{index}"))
    if order == list(range(len(scenes))):
        order = order[1:] + order[:1]
    # Four-on-the-floor and half-time grids can pull a nominal two-second cut
    # slightly earlier.  A 14-second short target still remains >=12 seconds
    # after beat alignment across the approved BPM lanes.
    target_seconds = 14.0 if short else 16.0
    shot_count = int(target_seconds / 2.0)
    # Reuse CLEAN masters deliberately when a pack has fewer cuts than the
    # target Reel.  The crop, in-point and framing still change per fragment.
    order = [order[index % len(order)] for index in range(shot_count)]
    shots: list[dict[str, Any]] = []
    for position, scene_index in enumerate(order):
        scene = scenes[scene_index]
        length = 2.0
        source_shot_size = scene.shot_size
        source_index = SHOT_SIZES.index(source_shot_size)
        reel_shot_size = SHOT_SIZES[
            (source_index + 1 + (_rank(plan.plan_id, f"reframe:{short}:{position}") % 3))
            % len(SHOT_SIZES)
        ]
        crop = REEL_CROPS[_rank(plan.plan_id, f"crop:{short}:{position}") % len(REEL_CROPS)]
        if position == 0 and crop == "wide-center":
            crop = "tight-center"
        shots.append({
            "scene_id": scene.scene_id,
            "shot_size": reel_shot_size,
            "source_scene_id": scene.scene_id,
            "source": f"stories/{scene.scene_id}_clean.mp4",
            "in_seconds": round(0.2 * (position % 4), 1),
            "duration_seconds": length,
            "story_shot_size": scene.shot_size,
            "source_shot_size": source_shot_size,
            "reel_shot_size": reel_shot_size,
            "crop_scale_instruction": (
                f"Reframe and scale the CLEAN {source_shot_size} master to a "
                f"{reel_shot_size} Reel fragment; preserve the documented action."
            ),
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


def _variant_plan_id(base_plan_id: str, variant_index: int) -> str:
    return hashlib.sha256(
        f"{base_plan_id}|{STORY_SCHEMA}|story-variant|{variant_index}".encode("utf-8")
    ).hexdigest()[:24]


def plan_story_pack(
    plan: EditorialPlan,
    safe_facts: tuple[str, ...],
    *,
    variant_index: int = 0,
) -> StoryPackPlan:
    if plan.production_mode != "story_first" or plan.content_format != "story_pack":
        raise StoryPlanError("EditorialPlan is not Story-first")
    facts = tuple(_safe_fact(item) for item in safe_facts)
    if len(facts) < 4:
        raise StoryPlanError("Story-first requires at least four safe causal facts")
    if not 0 <= variant_index <= 99:
        raise StoryPlanError("Story-first variant index must be 0..99")
    story_plan_id = _variant_plan_id(plan.plan_id, variant_index)
    variant_plan = replace(plan, plan_id=story_plan_id)
    count = max(4, min(7, len(facts)))
    treatment_key = _visual_treatment(plan, facts)
    continuity_id = hashlib.sha256(f"naz|{plan.plan_id}|continuity".encode("utf-8")).hexdigest()[:20]
    roles = _roles(story_plan_id, count)
    scenes = tuple(
        _scene(
            variant_plan,
            continuity_id=continuity_id,
            role=role,
            index=i,
            fact=facts[i],
            treatment_key=treatment_key,
        )
        for i, role in enumerate(roles)
    )
    pack = StoryPackPlan(
        plan_id=story_plan_id, base_plan_id=plan.plan_id,
        variant_index=variant_index, continuity_id=continuity_id, persona=plan.persona,
        destination=plan.platform, scheduled_slot=plan.slot, rubric=plan.rubric,
        source_type=plan.source_type, source_ref=plan.source_ref, safe_facts=facts,
        editorial_plan=plan.to_dict(),
        central_thesis=plan.thesis_direction,
        visual_concept=str(VISUAL_TREATMENTS[treatment_key]["label"]),
        scene_count=count, scenes=scenes,
        reel_edits=(
            _reel_edit(variant_plan, scenes, short=False),
            _reel_edit(variant_plan, scenes, short=True),
        ),
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
        if scene.duration_seconds != 5:
            raise StoryPlanError("Story duration must be exactly 5 seconds")
        if scene.secret_warning or _SECRET_RE.search(scene.clean_prompt):
            raise StoryPlanError("secret warning in scene")
        if "9:16" not in scene.clean_prompt or "Real motion" not in scene.clean_prompt:
            raise StoryPlanError("CLEAN master contract is incomplete")
        if not validate_provider_prompt(scene.provider_prompt) or fact_text_in_provider_prompt(scene):
            raise StoryPlanError("provider prompt is not privacy-minimized")
        if not validate_provider_prompt(scene.keyframe_prompt):
            raise StoryPlanError("keyframe prompt is unsafe")
        if scene.requires_naz_reference is not (scene.identity_reference_usage == "identity_only"):
            raise StoryPlanError("identity reference must be used for identity only")
        if scene.requires_naz_reference and "@Naz" not in scene.keyframe_prompt:
            raise StoryPlanError("Naz keyframe prompt must address the identity reference")
        if "replace the reference background" not in scene.keyframe_prompt and scene.requires_naz_reference:
            raise StoryPlanError("Naz keyframe must replace the avatar background")
        if not scene.story_overlay or not scene.text_safe_zone:
            raise StoryPlanError("STORY overlay contract is incomplete")
        if pack.continuity_id not in " ".join(scene.continuity_constraints):
            raise StoryPlanError("visual continuity mismatch")
    for edit in pack.reel_edits:
        if not edit.hook or not edit.conclusion or len(edit.shots) < 3:
            raise StoryPlanError("Reel needs its own hook, conclusion and EDL")
        if not all(0.4 <= float(item["duration_seconds"]) <= 2.0 for item in edit.shots):
            raise StoryPlanError("every Reel fragment must be 0.4..2.0 seconds")
        total_duration = sum(float(item["duration_seconds"]) for item in edit.shots)
        if not 12.0 <= total_duration <= 20.0:
            raise StoryPlanError("Reel duration must be 12..20 seconds")
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
        ensure_private_group_access(temporary, directory=False)
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
        ensure_private_group_access(temporary, directory=False)
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
    if payload.get("schema") not in {
        STORY_SCHEMA, PREVIOUS_STORY_SCHEMA, OLDER_STORY_SCHEMA, LEGACY_STORY_SCHEMA,
    }:
        raise StoryPlanError("unsupported story manifest schema")
    return payload


def update_manifest(path: Path, mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    payload = read_manifest(path)
    mutator(payload)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_json(path, payload)
    return payload


def _duration_in_range(value: Any, minimum: float, maximum: float) -> bool:
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return False
    return minimum <= duration <= maximum


def manifest_has_current_production_contract(payload: Mapping[str, Any]) -> bool:
    """Return whether a v2 manifest is safe for the current paid worker.

    This deliberately checks the persisted production envelope, not just the
    editorial plan.  Old v2 manifests can still be inspected or repaired by a
    dry-run, but cannot be approved or reach a provider accidentally.
    """
    if payload.get("schema") != STORY_SCHEMA:
        return False
    scenes = payload.get("scenes")
    edits = payload.get("reel_edits")
    scene_jobs = payload.get("scene_jobs")
    reel_jobs = payload.get("reel_jobs")
    model_policy = payload.get("model_policy")
    visual_strategy = payload.get("visual_strategy")
    if not all(isinstance(value, list) for value in (scenes, edits, scene_jobs, reel_jobs)):
        return False
    if payload.get("visual_concept") not in {
        str(treatment["label"]) for treatment in VISUAL_TREATMENTS.values()
    }:
        return False
    if not 4 <= len(scenes) <= 7 or payload.get("scene_count") != len(scenes) or not edits:
        return False
    if not isinstance(model_policy, dict) or (
        model_policy.get("primary_tier") != "primary"
        or model_policy.get("secondary_tier") != "secondary"
        or model_policy.get("automatic_fallback") is not False
    ):
        return False
    if not isinstance(visual_strategy, dict) or (
        visual_strategy.get("planner") != "reels-maker-directed-scenes-v1"
        or visual_strategy.get("keyframe_required") is not True
        or visual_strategy.get("avatar_usage") != "identity_reference_only"
        or visual_strategy.get("direct_avatar_as_video_first_frame") is not False
    ):
        return False
    scene_ids = [str(item.get("scene_id", "")) for item in scenes if isinstance(item, dict)]
    if len(scene_ids) != len(scenes) or not all(scene_ids) or len(set(scene_ids)) != len(scene_ids):
        return False
    scenes_by_id = {str(item["scene_id"]): item for item in scenes}
    for scene in scenes:
        requires_reference = scene.get("requires_naz_reference")
        role = str(scene.get("reference_role", ""))
        shot_size = str(scene.get("shot_size", ""))
        if (
            shot_size not in SHOT_SIZES
            or not _duration_in_range(scene.get("duration_seconds"), 5, 5)
            or not isinstance(requires_reference, bool)
            or role not in REFERENCE_ROLES
            or not str(scene.get("keyframe_prompt", ""))
            or scene.get("identity_reference_usage") not in {"identity_only", "none"}
        ):
            return False
        if (
            requires_reference and role not in {"frontal_identity", "three_quarter_identity"}
        ) or (not requires_reference and role != "none"):
            return False
    if len(scene_jobs) != len(scenes):
        return False
    job_ids = [str(item.get("scene_id", "")) for item in scene_jobs if isinstance(item, dict)]
    if (
        len(job_ids) != len(scene_jobs)
        or not all(job_ids)
        or set(job_ids) != set(scene_ids)
        or len(set(job_ids)) != len(job_ids)
    ):
        return False
    route_fields = {
        "tier", "primary_model", "secondary_model", "primary_failure_code",
        "secondary_requested_at", "secondary_approved_at",
    }
    for job in scene_jobs:
        scene_id = str(job.get("scene_id", ""))
        scene = scenes_by_id[scene_id]
        route = job.get("model_route")
        state = str(job.get("state", ""))
        submit_intent = job.get("submit_intent")
        if (
            state not in SCENE_STATES
            or not _duration_in_range(job.get("planned_duration_seconds"), 5, 5)
            or job.get("requires_naz_reference") is not scene["requires_naz_reference"]
            or str(job.get("reference_role", "")) != str(scene["reference_role"])
            or str(job.get("clean_path", "")) != f"stories/{scene_id}_clean.mp4"
            or str(job.get("story_path", "")) != f"stories/{scene_id}_story.mp4"
            or str(job.get("keyframe_path", "")) != f"keyframes/{scene_id}.jpg"
            or job.get("keyframe_state") not in {
                "planned", "queued", "submitting", "submitted", "in_progress",
                "ready", "terminal_failed", "submit_ambiguous",
            }
            or "keyframe_submit_intent" not in job
            or job.get("keyframe_submit_intent") is not None
            and not isinstance(job.get("keyframe_submit_intent"), dict)
            or "submit_intent" not in job
            or submit_intent is not None and not isinstance(submit_intent, dict)
            or state in {"submitting", "submit_ambiguous"}
            and not isinstance(submit_intent, dict)
            or not isinstance(route, dict)
            or not route_fields.issubset(route)
            or route.get("tier") not in {"primary", "secondary"}
        ):
            return False
    edit_ids: list[str] = []
    for edit in edits:
        if not isinstance(edit, dict) or not isinstance(edit.get("shots"), list):
            return False
        edit_id = str(edit.get("edit_id", ""))
        if not edit_id or not str(edit.get("hook", "")) or not str(edit.get("conclusion", "")):
            return False
        edit_ids.append(edit_id)
        shots = edit["shots"]
        if len(shots) < 3:
            return False
        total_duration = 0.0
        for shot in shots:
            if not isinstance(shot, dict):
                return False
            if not _duration_in_range(shot.get("duration_seconds"), 0.4, 2.0):
                return False
            total_duration += float(shot["duration_seconds"])
            if (
                str(shot.get("source_shot_size", "")) not in SHOT_SIZES
                or str(shot.get("reel_shot_size", "")) not in SHOT_SIZES
                or not str(shot.get("source_scene_id", ""))
                or not str(shot.get("crop_scale_instruction", ""))
                or str(shot.get("source_scene_id", "")) not in scene_ids
                or not bool(shot.get("crop_change_required"))
                or str(shot.get("reel_crop", "")) not in REEL_CROPS
                or not str(shot.get("source", "")).endswith("_clean.mp4")
            ):
                return False
        if not 12.0 <= total_duration <= 20.0:
            return False
        if not any(shot["source_shot_size"] != shot["reel_shot_size"] for shot in shots):
            return False
        if [str(shot["source_scene_id"]) for shot in shots] == scene_ids[: len(shots)]:
            return False
    if len(set(edit_ids)) != len(edit_ids) or len(reel_jobs) != len(edits):
        return False
    reel_job_ids = [str(item.get("edit_id", "")) for item in reel_jobs if isinstance(item, dict)]
    if len(reel_job_ids) != len(reel_jobs) or set(reel_job_ids) != set(edit_ids):
        return False
    for job in reel_jobs:
        edit_id = str(job.get("edit_id", ""))
        if (
            str(job.get("state", "")) not in {"planned", "blocked_music", "completed"}
            or str(job.get("path", "")) != f"reels/{edit_id}.mp4"
            or not {"media_probe", "checksum", "failure_code"}.issubset(job)
        ):
            return False
    return True


def _manifest_has_current_reel_contract(payload: Mapping[str, Any]) -> bool:
    """Compatibility alias for callers that used the old private checker."""
    return manifest_has_current_production_contract(payload)


def _preserve_render_statuses(payload: dict[str, Any], existing: Mapping[str, Any]) -> None:
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


def _production_payload(pack: StoryPackPlan) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    payload = pack.to_dict()
    payload.update({
        "created_at": now, "updated_at": now, "pack_status": "awaiting_approval",
        "approval": {
            "status": "awaiting_approval", "requested_at": now,
            "approved_at": None, "superseded_at": None,
        },
        "delivery": {"status": "not_ready", "sent_files": [], "completed_at": None},
        "renderer": {"status": "awaiting_approval", "name": "ffmpeg"},
        "composer": {"status": "planned", "name": "ffmpeg"},
        "provider": {"name": None, "model": None},
        "model_policy": {
            "primary_tier": "primary", "secondary_tier": "secondary",
            "automatic_fallback": False,
        },
        "visual_strategy": {
            "planner": "reels-maker-directed-scenes-v1",
            "keyframe_required": True,
            "avatar_usage": "identity_reference_only",
            "direct_avatar_as_video_first_frame": False,
            "keyframe_provider": "runway",
        },
        "visual_identity_qa": {"status": "not_run", "blocking": False},
    })
    payload["scene_jobs"] = [{
        "scene_id": scene.scene_id, "state": "planned", "external_job_id": None,
        "attempts": 0, "submitted_at": None, "provider_status": None,
        "planned_duration_seconds": scene.duration_seconds, "actual_duration_seconds": None,
        "clean_path": f"stories/{scene.scene_id}_clean.mp4",
        "story_path": f"stories/{scene.scene_id}_story.mp4",
        "master_relation": "STORY is a local overlay of this CLEAN master",
        "media_probe": None, "clean_checksum": None, "story_checksum": None,
        "technical_qa": {"status": "not_run"},
        "visual_identity_qa": {"status": "not_run", "blocking": False},
        "failure_code": None, "requires_naz_reference": scene.requires_naz_reference,
        "reference_role": scene.reference_role,
        "keyframe_path": f"keyframes/{scene.scene_id}.jpg",
        "keyframe_state": "planned", "keyframe_external_job_id": None,
        "keyframe_attempts": 0, "keyframe_submitted_at": None,
        "keyframe_provider_status": None, "keyframe_checksum": None,
        "keyframe_failure_code": None, "keyframe_submit_intent": None,
        "submit_intent": None,
        "model_route": {
            "tier": "primary", "primary_model": None, "secondary_model": None,
            "primary_failure_code": None, "secondary_requested_at": None,
            "secondary_approved_at": None,
        },
    } for scene in pack.scenes]
    payload["reel_jobs"] = [{
        "edit_id": edit.edit_id, "state": "planned",
        "path": f"reels/{edit.edit_id}.mp4", "media_probe": None,
        "checksum": None, "failure_code": None,
    } for edit in pack.reel_edits]
    payload["expected_outputs"] = {
        "stories": [{"clean": job["clean_path"], "story": job["story_path"], "status": "planned"} for job in payload["scene_jobs"]],
        "reels": [job["path"] for job in payload["reel_jobs"]],
    }
    payload["overlay_policy"] = "local overlay on matching CLEAN master; no scene regeneration"
    return payload


def persist_story_queue(pack: StoryPackPlan, storage_root: Path) -> Path:
    """Atomically create a resumable current-schema item, or return that exact pack."""
    root = Path(storage_root).expanduser().resolve()
    pack_dir = (root / pack.plan_id).resolve()
    if root not in pack_dir.parents:
        raise StoryPlanError("unsafe pack path")
    manifest = pack_dir / "story_manifest.json"
    stories_dir = pack_dir / "stories"
    reels_dir = pack_dir / "reels"
    keyframes_dir = pack_dir / "keyframes"
    stories_dir.mkdir(parents=True, exist_ok=True)
    reels_dir.mkdir(parents=True, exist_ok=True)
    keyframes_dir.mkdir(parents=True, exist_ok=True)
    ensure_private_group_access(pack_dir, directory=True)
    ensure_private_group_access(stories_dir, directory=True)
    ensure_private_group_access(reels_dir, directory=True)
    ensure_private_group_access(keyframes_dir, directory=True)
    created = _atomic_create_text(
        manifest,
        json.dumps(_production_payload(pack), ensure_ascii=False, indent=2) + "\n",
    )
    ensure_private_group_access(manifest, directory=False)
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
    ensure_private_group_access(pack_dir / "caption_pack.md", directory=False)
    return pack_dir


def persist_dry_run(pack: StoryPackPlan, storage_root: Path) -> Path:
    """Repair/read legacy dry-runs without creating fake media or provider jobs."""
    root = Path(storage_root).expanduser().resolve()
    pack_dir = (root / pack.plan_id).resolve()
    if root not in pack_dir.parents:
        raise StoryPlanError("unsafe pack path")
    manifest = pack_dir / "story_manifest.json"
    existing: Mapping[str, Any] | None = None
    if manifest.is_file():
        ensure_private_group_access(pack_dir, directory=True)
        ensure_private_group_access(manifest, directory=False)
        existing = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(existing, dict) or existing.get("plan_id") != pack.plan_id:
            raise StoryPlanError("stored manifest plan_id mismatch")
        if manifest_has_current_production_contract(existing):
            return pack_dir
    stories_dir = pack_dir / "stories"
    reels_dir = pack_dir / "reels"
    stories_dir.mkdir(parents=True, exist_ok=True)
    reels_dir.mkdir(parents=True, exist_ok=True)
    ensure_private_group_access(pack_dir, directory=True)
    ensure_private_group_access(stories_dir, directory=True)
    ensure_private_group_access(reels_dir, directory=True)
    payload = _production_payload(pack)
    if existing is not None:
        _preserve_render_statuses(payload, existing)
    atomic_json(manifest, payload)
    ensure_private_group_access(manifest, directory=False)
    caption = (
        f"# Caption pack\n\nPlan ID: {pack.plan_id}\nContinuity ID: {pack.continuity_id}\n\n"
        f"Main: {pack.caption_plan['main']}\n\nShort: {pack.caption_plan['short']}\n"
    )
    if not (pack_dir / "caption_pack.md").exists():
        _atomic_create_text(pack_dir / "caption_pack.md", caption)
    ensure_private_group_access(pack_dir / "caption_pack.md", directory=False)
    return pack_dir
