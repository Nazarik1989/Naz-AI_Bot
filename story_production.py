"""Validated Story-first treatments and atomic queue manifests for Naz.

The text director is called by ``main.py`` before this module persists a plan.
Paid image/video generation and local composition remain isolated in
``naz_story_worker.py`` and never run in a Telegram scheduler callback.
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
from typing import Any, Callable, Mapping, Sequence

from editorial_orchestrator import EditorialPlan
from story_pack_lock import ensure_private_group_access
from story_video_provider import ProviderError, compact_runway_prompt


STORY_SCHEMA = "naz-story-pack-v7"
PREVIOUS_STORY_SCHEMA = "naz-story-pack-v6"
OLDER_STORY_SCHEMA = "naz-story-pack-v5"
LEGACY_STORY_SCHEMA = "naz-story-pack-v4"
ANCIENT_STORY_SCHEMA = "naz-story-pack-v3"
FIRST_STORY_SCHEMA = "naz-story-pack-v2"
ORIGINAL_STORY_SCHEMA = "naz-story-pack-v1"
SUPPORTED_STORY_SCHEMAS = (
    STORY_SCHEMA,
    PREVIOUS_STORY_SCHEMA,
    OLDER_STORY_SCHEMA,
    LEGACY_STORY_SCHEMA,
    ANCIENT_STORY_SCHEMA,
    FIRST_STORY_SCHEMA,
    ORIGINAL_STORY_SCHEMA,
)
DIRECTOR_VERSION = "reels-semantic-director-v8"
SEMANTIC_CONTRACT_VERSION = "reels-semantic-plan-v1"
TEMPLATE_DIRECTOR_VERSION = "reels-template-director-v1"
MOTION_CONTRACT_VERSION = "bounded-story-arc-v4"
VIDEO_MOTION_PROMPT_VERSION = "runway-image-to-video-motion-v2"
HYBRID_MODEL_ROUTE = "naz-human-gen45-object-turbo-v1"
RUNWAY_VIDEO_CREDITS_PER_SECOND = {"gen4_turbo": 5, "gen4.5": 12}
RENDERER_UNAVAILABLE = "unavailable"
DRAMATURGIC_ROLES = (
    "hook", "problem", "hypothesis", "test", "result", "solution", "conclusion",
)
SHOT_SIZES = ("wide", "medium", "close", "macro")
REFERENCE_ROLES = ("none", "frontal_identity", "three_quarter_identity")
REEL_CROPS = ("tight-center", "left-detail", "right-detail", "wide-center")
CAMERA_MOTIONS = ("slow push", "controlled pan", "handheld follow", "locked with real subject motion")
CAMERA_MOTION_PROMPTS = {
    "slow push": "The camera slowly pushes in",
    "controlled pan": "The camera makes one controlled pan",
    "handheld follow": "A restrained handheld camera follows the action",
    "locked with real subject motion": "The locked-off camera remains still",
}
DIRECTOR_SUBJECT_KINDS = ("naz_human", "physical_object")
DIRECTOR_MOTION_CLASSES = (
    "align", "bend", "carry", "close", "connect", "cut",
    "disconnect", "fold", "grip", "insert", "lift", "lock", "lower", "open",
    "oscillate", "place", "pour", "press", "pull", "push", "remove", "rotate",
    "slide", "unlock",
)
DIRECTOR_SELF_MOTION_CLASSES = (
    "close", "lower", "open", "oscillate", "rotate", "slide",
)
DIRECTOR_BRAND_MARKINGS = ("none", "naz_ai_lab")
VISUALIZATION_KINDS = ("literal", "physical_metaphor")
CORE_THESIS_REF = "core_thesis"

# Each recipe is a pre-vetted, single-predicate piece of blocking. The text
# director selects a recipe; it never composes materials, props or grammar.
# Tuple fields: subject_kind, motion_class, primary prop, optional contact.
DIRECTOR_ACTION_RECIPES: Mapping[str, tuple[str, str, str, str]] = {
    "naz_aligns_module_against_guide": (
        "naz_human", "align", "one titanium module", "against one technical ceramic guide",
    ),
    "naz_bends_cable_around_guide": (
        "naz_human", "bend", "one carbon-sheathed technical cable", "around one titanium guide",
    ),
    "naz_carries_module": (
        "naz_human", "carry", "one blue anodized aluminum module", "",
    ),
    "naz_closes_panel_against_frame": (
        "naz_human", "close", "one smoked-glass panel", "against one titanium frame",
    ),
    "naz_connects_plug_into_socket": (
        "naz_human", "connect", "one titanium plug", "into one technical ceramic socket",
    ),
    "naz_cuts_polymer_sheet_along_guide": (
        "naz_human", "cut", "one transparent technical-polymer sheet", "along one titanium guide",
    ),
    "naz_disconnects_plug_from_socket": (
        "naz_human", "disconnect", "one titanium plug", "from one technical ceramic socket",
    ),
    "naz_folds_polymer_sheet_along_guide": (
        "naz_human", "fold", "one transparent technical-polymer sheet", "along one titanium guide",
    ),
    "naz_grips_lever": (
        "naz_human", "grip", "one milled titanium lever", "",
    ),
    "naz_inserts_module_into_slot": (
        "naz_human", "insert", "one blue anodized aluminum module", "into one technical ceramic slot",
    ),
    "naz_lifts_module_from_bench": (
        "naz_human", "lift", "one titanium module", "from one carbon-fiber workbench",
    ),
    "naz_locks_collar_against_stop": (
        "naz_human", "lock", "one polished titanium collar", "against one technical ceramic stop",
    ),
    "naz_lowers_module_onto_mount": (
        "naz_human", "lower", "one titanium module", "onto one technical ceramic mount",
    ),
    "naz_opens_glass_cover": (
        "naz_human", "open", "one smoked-glass cover", "",
    ),
    "naz_places_module_onto_bench": (
        "naz_human", "place", "one blue anodized aluminum module", "onto one carbon-fiber workbench",
    ),
    "naz_presses_mechanical_button": (
        "naz_human", "press", "one titanium mechanical button", "",
    ),
    "naz_pulls_lever": (
        "naz_human", "pull", "one milled titanium lever", "",
    ),
    "naz_pushes_tray_along_rail": (
        "naz_human", "push", "one blue anodized aluminum tray", "along one titanium rail",
    ),
    "naz_removes_module_from_slot": (
        "naz_human", "remove", "one titanium module", "from one technical ceramic slot",
    ),
    "naz_rotates_dial_within_housing": (
        "naz_human", "rotate", "one titanium dial", "within one smoked-glass housing",
    ),
    "naz_slides_tray_along_rail": (
        "naz_human", "slide", "one blue anodized aluminum tray", "along one titanium rail",
    ),
    "naz_unlocks_latch": (
        "naz_human", "unlock", "one polished titanium latch", "",
    ),
    "mechanism_cover_closes_against_frame": (
        "physical_object", "close", "one smoked-glass cover", "against one titanium frame",
    ),
    "mechanism_platform_lowers_onto_mount": (
        "physical_object", "lower", "one carbon-fiber platform", "onto one technical ceramic mount",
    ),
    "mechanism_cover_opens": (
        "physical_object", "open", "one smoked-glass cover", "",
    ),
    "mechanism_rotor_oscillates_within_housing": (
        "physical_object", "oscillate", "one titanium rotor", "within one smoked-glass housing",
    ),
    "mechanism_rotor_rotates_within_housing": (
        "physical_object", "rotate", "one titanium rotor", "within one technical ceramic housing",
    ),
    "mechanism_tray_slides_along_rail": (
        "physical_object", "slide", "one blue anodized aluminum tray", "along one titanium rail",
    ),
    "mechanism_latch_closes_against_stop": (
        "physical_object", "close", "one polished titanium latch", "against one technical ceramic stop",
    ),
    "mechanism_collar_rotates_within_housing": (
        "physical_object", "rotate", "one blue anodized aluminum collar", "within one titanium housing",
    ),
    "mechanism_door_opens_within_frame": (
        "physical_object", "open", "one titanium door", "within one titanium frame",
    ),
    "mechanism_module_slides_along_guide": (
        "physical_object", "slide", "one transparent technical-polymer module", "along one technical ceramic guide",
    ),
    "mechanism_panel_closes_against_frame": (
        "physical_object", "close", "one carbon-fiber panel", "against one titanium frame",
    ),
    "mechanism_platform_lowers_onto_bench": (
        "physical_object", "lower", "one titanium platform", "onto one carbon-fiber workbench",
    ),
}
DIRECTOR_ACTION_RECIPE_NAMES = tuple(DIRECTOR_ACTION_RECIPES)
DIRECTOR_PRIMARY_SETTINGS: Mapping[str, str] = {
    "integration_workbench": "one dark Naz AI Lab integration workbench",
    "validation_bay": "one enclosed Naz AI Lab validation bay",
    "server_aisle": "one cold Naz AI Lab server aisle",
    "optics_bench": "one smoked-glass Naz AI Lab optics bench",
    "materials_lab": "one sparse Naz AI Lab materials laboratory",
    "machining_cell": "one precise Naz AI Lab machining cell",
    "prototype_bay": "one black-glass Naz AI Lab prototype bay",
    "relay_platform": "one weathered Naz AI Lab relay platform",
    "systems_chamber": "one physical Naz AI Lab systems chamber",
    "fabrication_bench": "one titanium Naz AI Lab fabrication bench",
}
DIRECTOR_PRIMARY_SETTING_CODES = tuple(DIRECTOR_PRIMARY_SETTINGS)
DIRECTOR_STATE_LABELS: Mapping[str, str] = {
    "inactive": "inactive",
    "active": "active",
    "unpowered": "unpowered",
    "powered": "powered",
    "dark": "dark",
    "illuminated": "illuminated by one restrained cobalt indicator",
    "misaligned": "visibly misaligned",
    "aligned": "visibly aligned",
    "blocked": "mechanically blocked",
    "clear": "mechanically clear",
    "disconnected": "physically disconnected",
    "connected": "physically connected",
    "unseated": "outside its mechanical seat",
    "seated": "fully seated",
    "loose": "visibly loose",
    "secured": "mechanically secured",
    "unlocked": "unlocked",
    "locked": "locked against its stop",
    "open": "open",
    "closed": "closed",
    "unstable": "visibly unstable under load",
    "stable": "stable under the same load",
    "stationary": "stationary",
    "moving": "moving under visible mechanical drive",
    "rotating_smoothly": "rotating smoothly under the same visible mechanical drive",
    "unverified": "not yet physically verified",
    "verified": "physically verified",
    "unloaded": "unloaded",
    "under_load": "under one visible mechanical load",
    "incomplete": "visibly incomplete",
    "complete": "visibly complete",
    "exposed": "physically exposed for inspection",
    "covered": "covered by its protective shell",
    "removed": "removed from its seat",
    "installed": "installed in its mechanical seat",
    "raised": "raised above its mount",
    "lowered": "lowered onto its mount",
    "empty": "visibly empty",
    "filled": "visibly filled to its marked limit",
    "intact": "physically intact",
    "separated": "cleanly separated along its guide",
}
DIRECTOR_STATE_CODES = tuple(DIRECTOR_STATE_LABELS)
DIRECTOR_STORY_ARCS: Mapping[str, Mapping[str, Any]] = {
    "module_recovery_mixed": {
        "subject_mode": "mixed",
        "setting": "integration_workbench",
        "continuity_anchor": "the same physical Naz AI Lab prototype module",
        "initial_state": "covered",
        "description": "Naz exposes, realigns and reconnects one module; its driven rotor proves the repair.",
        "description_ru": "Наз открывает, выравнивает и подключает один модуль; вращение механизма подтверждает ремонт.",
        "steps": (
            ("naz_opens_glass_cover", "exposed", 4),
            ("naz_removes_module_from_slot", "removed", 6),
            ("naz_aligns_module_against_guide", "aligned", 4),
            ("naz_inserts_module_into_slot", "installed", 5),
            ("naz_connects_plug_into_socket", "connected", 4),
            ("naz_presses_mechanical_button", "powered", 7),
            ("mechanism_rotor_rotates_within_housing", "rotating_smoothly", 4),
        ),
    },
    "module_recovery_human": {
        "subject_mode": "human",
        "setting": "integration_workbench",
        "continuity_anchor": "the same physical Naz AI Lab prototype module",
        "initial_state": "covered",
        "description": "Naz alone exposes, realigns, reconnects and mechanically verifies one module.",
        "description_ru": "Наз сам открывает, выравнивает, подключает и механически проверяет один модуль.",
        "steps": (
            ("naz_opens_glass_cover", "exposed", 4),
            ("naz_removes_module_from_slot", "removed", 6),
            ("naz_aligns_module_against_guide", "aligned", 4),
            ("naz_inserts_module_into_slot", "installed", 5),
            ("naz_connects_plug_into_socket", "connected", 4),
            ("naz_presses_mechanical_button", "powered", 7),
            ("naz_rotates_dial_within_housing", "illuminated", 4),
        ),
    },
    "connector_calibration_human": {
        "subject_mode": "human",
        "setting": "validation_bay",
        "continuity_anchor": "the same physical Naz AI Lab connector assembly",
        "initial_state": "locked",
        "description": "Naz unlocks, exposes, aligns, reconnects and secures one connector assembly.",
        "description_ru": "Наз разблокирует, открывает, выравнивает, подключает и фиксирует один узел.",
        "steps": (
            ("naz_unlocks_latch", "unlocked", 4),
            ("naz_opens_glass_cover", "exposed", 5),
            ("naz_removes_module_from_slot", "removed", 7),
            ("naz_aligns_module_against_guide", "aligned", 4),
            ("naz_inserts_module_into_slot", "installed", 6),
            ("naz_connects_plug_into_socket", "connected", 4),
            ("naz_locks_collar_against_stop", "secured", 4),
        ),
    },
    "automated_validation_cycle": {
        "subject_mode": "object",
        "setting": "systems_chamber",
        "continuity_anchor": "the same physical Naz AI Lab validation mechanism",
        "initial_state": "closed",
        "description": "One visibly driven mechanism exposes, loads, stabilizes and verifies itself.",
        "description_ru": "Один механизм с видимым приводом открывается, принимает нагрузку и проходит проверочный цикл.",
        "steps": (
            ("mechanism_door_opens_within_frame", "open", 4),
            ("mechanism_cover_opens", "exposed", 5),
            ("mechanism_module_slides_along_guide", "aligned", 4),
            ("mechanism_platform_lowers_onto_mount", "under_load", 4),
            ("mechanism_rotor_oscillates_within_housing", "unstable", 6),
            ("mechanism_latch_closes_against_stop", "secured", 7),
            ("mechanism_rotor_rotates_within_housing", "rotating_smoothly", 4),
        ),
    },
    "actuator_proof_cycle": {
        "subject_mode": "object",
        "setting": "prototype_bay",
        "continuity_anchor": "the same physical Naz AI Lab actuator system",
        "initial_state": "raised",
        "description": "One visibly driven actuator lowers, aligns, locks, loads and proves its cycle.",
        "description_ru": "Один механизм с видимым приводом опускается, выравнивается, фиксируется и подтверждает рабочий цикл.",
        "steps": (
            ("mechanism_platform_lowers_onto_bench", "lowered", 4),
            ("mechanism_tray_slides_along_rail", "moving", 5),
            ("mechanism_collar_rotates_within_housing", "aligned", 4),
            ("mechanism_cover_closes_against_frame", "closed", 6),
            ("mechanism_latch_closes_against_stop", "secured", 4),
            ("mechanism_rotor_oscillates_within_housing", "under_load", 7),
            ("mechanism_rotor_rotates_within_housing", "rotating_smoothly", 4),
        ),
    },
}
DIRECTOR_STORY_ARC_NAMES = tuple(DIRECTOR_STORY_ARCS)
DIRECTOR_ARC_SEMANTIC_AXES: Mapping[str, frozenset[str]] = {
    "module_recovery_mixed": frozenset({"physical_mechanism", "software_validation"}),
    "module_recovery_human": frozenset({"physical_mechanism", "software_validation"}),
    "connector_calibration_human": frozenset({"physical_mechanism", "software_validation"}),
    "automated_validation_cycle": frozenset({
        "physical_mechanism", "software_validation", "publication_workflow",
    }),
    "actuator_proof_cycle": frozenset({"physical_mechanism", "software_validation"}),
}
DIRECTOR_RECIPE_SUMMARIES_RU: Mapping[str, str] = {
    "naz_opens_glass_cover": "Наз открывает дымчатую стеклянную крышку и оставляет узел доступным для проверки.",
    "naz_removes_module_from_slot": "Наз вынимает титановый модуль из керамического паза.",
    "naz_aligns_module_against_guide": "Наз выравнивает титановый модуль по керамической направляющей.",
    "naz_inserts_module_into_slot": "Наз устанавливает синий алюминиевый модуль в керамический паз.",
    "naz_connects_plug_into_socket": "Наз соединяет титановый штекер с керамическим разъёмом.",
    "naz_presses_mechanical_button": "Наз нажимает одну механическую кнопку и подаёт питание на узел.",
    "naz_rotates_dial_within_housing": "Наз поворачивает титановый регулятор до проверочного положения.",
    "naz_unlocks_latch": "Наз разблокирует титановую защёлку.",
    "naz_locks_collar_against_stop": "Наз фиксирует титановое кольцо на механическом упоре.",
    "mechanism_door_opens_within_frame": "Видимый привод открывает титановую дверцу внутри рамы.",
    "mechanism_cover_opens": "Видимый привод открывает дымчатую стеклянную крышку.",
    "mechanism_module_slides_along_guide": "Привод перемещает полимерный модуль по керамической направляющей.",
    "mechanism_platform_lowers_onto_mount": "Привод опускает карбоновую платформу на керамическое крепление.",
    "mechanism_rotor_oscillates_within_housing": "Титановый ротор под нагрузкой совершает контролируемое колебание.",
    "mechanism_latch_closes_against_stop": "Привод закрывает титановую защёлку на керамическом упоре.",
    "mechanism_rotor_rotates_within_housing": "Титановый ротор равномерно вращается внутри корпуса и подтверждает работу узла.",
    "mechanism_platform_lowers_onto_bench": "Привод опускает титановую платформу на карбоновый стенд.",
    "mechanism_tray_slides_along_rail": "Привод перемещает синий алюминиевый лоток по титановой направляющей.",
    "mechanism_collar_rotates_within_housing": "Привод поворачивает синее алюминиевое кольцо внутри корпуса.",
    "mechanism_cover_closes_against_frame": "Привод закрывает дымчатую стеклянную крышку на титановой раме.",
}
CANONICAL_NAZ_SUBJECT = "Naz, the same real adult human founder"
CANONICAL_NAZ_WARDROBE = (
    "the same fitted matte-black technical overshirt, plain black shirt, "
    "tailored black trousers and minimal black boots"
)
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


class DirectorValidationError(StoryPlanError):
    """One fail-closed director rejection containing every safe reason code."""

    def __init__(self, reason_codes: Sequence[str]):
        unique_codes = tuple(dict.fromkeys(str(code) for code in reason_codes if code))
        if not unique_codes:
            unique_codes = ("director_contract_invalid",)
        self.reason_codes = unique_codes
        primary = unique_codes[0] if len(unique_codes) == 1 else "director_contract_invalid"
        super().__init__(primary)


@dataclass(frozen=True, slots=True)
class DirectorScene:
    role: str
    beat_id: str
    semantic_goal: str
    source_fact_refs: tuple[str, ...]
    relation_to_previous: str
    expected_viewer_understanding: str
    visualization_kind: str
    visual_relation_to_beat: str
    setting: str
    subject_kind: str
    subject: str
    motion_class: str
    concrete_action: str
    start_state: str
    end_state: str
    shot_size: str
    camera_motion: str
    admin_summary_ru: str


@dataclass(frozen=True, slots=True)
class DirectorTreatment:
    plan_id: str
    variant_index: int
    visual_concept: str
    core_thesis: str
    thesis_source_fact_refs: tuple[str, ...]
    viewer_problem: str
    hook: str
    payoff: str
    story_spine: str
    story_arc: str
    continuity_anchor: str
    initial_state_code: str
    goal_state_code: str
    primary_setting: str
    admin_concept_ru: str
    scenes: tuple[DirectorScene, ...]
    version: str = DIRECTOR_VERSION


@dataclass(frozen=True, slots=True)
class ScenePlan:
    scene_id: str
    role: str
    beat_id: str
    semantic_goal: str
    source_fact_refs: tuple[str, ...]
    relation_to_previous: str
    expected_viewer_understanding: str
    visualization_kind: str
    visual_relation_to_beat: str
    standalone_meaning: str
    subject_kind: str
    motion_class: str | None
    concrete_action: str
    subject: str
    setting: str
    start_state: str
    end_state: str
    shot_size: str
    camera_motion: str
    admin_summary_ru: str
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
    thesis_source_fact_refs: tuple[str, ...]
    viewer_problem: str
    hook: str
    payoff: str
    visual_concept: str
    story_spine: str
    story_arc: str
    continuity_anchor: str
    initial_state_code: str
    goal_state_code: str
    admin_concept_ru: str
    director_version: str
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


def draft_story_beats(value: str) -> tuple[str, ...]:
    """Extract 4–7 ordered, non-secret narrative beats from an approved draft."""
    text = str(value or "").replace("\r\n", "\n").strip()
    if not text or _SECRET_RE.search(text):
        raise StoryPlanError("director_draft_unsafe")
    paragraphs = [
        " ".join(item.split())
        for item in re.split(r"\n\s*\n+", text)
        if len(" ".join(item.split())) >= 24
    ]
    sentences = [
        " ".join(item.split())
        for item in re.split(r"(?<=[.!?])\s+|\n+", text)
        if len(" ".join(item.split())) >= 24
    ]
    candidates = list(dict.fromkeys(paragraphs if len(paragraphs) >= 4 else sentences))
    if len(candidates) < 4:
        raise StoryPlanError("director_draft_beats_insufficient")
    if len(candidates) > 7:
        indexes = {
            round(position * (len(candidates) - 1) / 6)
            for position in range(7)
        }
        candidates = [candidates[index] for index in sorted(indexes)]
    return tuple(_safe_fact(item) for item in candidates)


_DIRECTOR_TRANSPORT_RE = re.compile(
    r"(?i)(?:\bfact[\s_-]*\d+\b|tied to fact|perform and reveal|folders?:|user focus:|"
    r"project:|\.md\b|\.json\b|source_ref|plan_id)"
)
_DIRECTOR_FORBIDDEN_CLICHE_RE = re.compile(
    r"(?i)(?:screen interface|(?:overloaded\s+)?\bhud\b|random circuit|"
    r"flowing code|code rain)"
)
_SEMANTIC_WORD_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+")
_SEMANTIC_NUMBER_RE = re.compile(r"(?<![\w-])\d+(?:[.,]\d+)?%?")
_SEMANTIC_STOP_WORDS = {
    "about", "after", "again", "also", "because", "before", "becomes",
    "being", "between", "each", "from", "into", "only", "same", "that",
    "their", "then", "this", "through", "under", "with", "without",
    "один", "одна", "одно", "одного", "этот", "эта", "это", "после",
    "перед", "между", "через", "который", "которая", "только", "того",
    "свой", "свою", "однако", "затем", "чтобы", "когда", "потому",
    "physical", "visual", "represents", "shows", "viewer", "scene",
    "mechanism", "object", "result", "state", "process",
}
_PHYSICAL_RELATION_RE = re.compile(
    r"(?i)(?:physical|mechanic|mechanis|module|connector|actuator|rotor|assembly|prototype|"
    r"физичес|материал|механ|модул|разъ[её]м|узел|прототип|ротор|привод)"
)
_STRONG_PHYSICAL_SOURCE_RE = re.compile(
    r"(?i)(?:\bphysical\b|\bmechanic(?:al|s)?\b|\bactuators?\b|"
    r"\brotors?\b|\btitanium\b|\bceramic\b|"
    r"\baluminium\b|\bcarbon\b|механ|физичес|титан|"
    r"керами|алюмин|карбон|ротор|привод)"
)
_SOFTWARE_SOURCE_RE = re.compile(
    r"(?i)(?:build|configuration|release|regression|software|service|deploy|debug|"
    r"error|failure|failed|code|api|provider|test|retry|repository|github|"
    r"сборк|конфигура|релиз|регресс|сервис|депло|ошиб|сбой|код|тест|повтор|"
    r"репозитор|провайдер|модел)"
)
_PUBLICATION_SOURCE_RE = re.compile(
    r"(?i)(?:\bpublish(?:ed|es|ing)?\b|\bpublications?\b|\bposts?\b|"
    r"\btelegram\b|\breels?\b|\bcontent rotation\b|\bschedules?\b|"
    r"\bduplicate posts?\b|\bautopost\b|публикац|опублик|\bпост(?:а|у|ом|ы)?\b|телеграм|рилс|"
    r"ротаци|расписан|автопост|дубл)"
)
_GENERIC_VISUAL_CONCEPTS = {
    "lab", "naz ai lab", "laboratory", "technology", "future lab",
    "cyberpunk lab", "ai laboratory", "digital future", "innovation",
}
_MOTION_RELATION_MARKERS: Mapping[str, tuple[str, ...]] = {
    "align": ("align", "выравн"),
    "bend": ("bend", "bent", "сгиб"),
    "carry": ("carr", "перенос"),
    "close": ("clos", "закрыв"),
    "connect": ("connect", "соедин"),
    "cut": (" cut ", "разрез", "режет"),
    "disconnect": ("disconnect", "отсоедин"),
    "fold": ("fold", "склад"),
    "grip": ("grip", "захват"),
    "insert": ("insert", "встав", "устанав"),
    "lift": ("lift", "подним"),
    "lock": (" lock", "фиксир", "запир"),
    "lower": ("lower", "опуск"),
    "open": ("open", "открыв"),
    "oscillate": ("oscillat", "колеб"),
    "place": ("place", "размещ", "кладет"),
    "pour": ("pour", "налива"),
    "press": ("press", "нажим"),
    "pull": ("pull", "тянет"),
    "push": ("push", "толка"),
    "remove": ("remov", "вынима", "снима"),
    "rotate": ("rotat", "вращ", "поворач"),
    "slide": ("slid", "скольз", "сдвиг"),
    "unlock": ("unlock", "разблок"),
}
_MULTI_ACTION_RELATION_RE = re.compile(
    r"(?i)(?:\btwice\b|\bagain\b|\band then\b|\bfollowed by\b|"
    r"\bдважды\b|\bснова\b|\bи затем\b|\bпосле чего\b)"
)


def _fact_ids(count: int) -> tuple[str, ...]:
    return tuple(f"fact-{index}" for index in range(1, count + 1))


def _beat_ids(plan_id: str, count: int) -> tuple[str, ...]:
    return tuple(
        f"beat-{index:02d}-{role}"
        for index, role in enumerate(_roles(plan_id, count), start=1)
    )


def _semantic_tokens(value: str) -> set[str]:
    tokens: set[str] = set()
    for raw in _SEMANTIC_WORD_RE.findall(str(value).casefold().replace("ё", "е")):
        if raw.isdigit() or len(raw) < 4 or raw in _SEMANTIC_STOP_WORDS:
            continue
        # A small language-agnostic prefix stem is deterministic and tolerates
        # ordinary English/Russian inflection without claiming full NLP.
        tokens.add(raw[:6] if len(raw) > 6 else raw)
    return tokens


# Deterministic, non-claiming language that may connect source terms into a
# privacy-safe semantic brief. Any other content word must be present in the
# cited source facts; this deliberately favours false rejection over invention.
_SEMANTIC_RELATION_VOCABULARY = _semantic_tokens("""
    action beat causal chain connection episode expose final follow hook mapping meaning
    next opening payoff reveal safe semantic stage transition visualise visualize
    physical material mechanism metaphor relation correspond represent map maps show viewer understand
    align bend carry close connect cut disconnect fold grip insert lift lock lower open
    oscillate place pour press pull push remove rotate slide unlock
    действие переход бит причинный цепочка связь показать эпизод раскрыть финальный
    следовать зацепка соответствие смысл следующий открытие развязка безопасный смысловой
    этап визуализировать физический материал зритель понимать
    механизм метафора отношение соответствовать обозначать тест выравнивать сгибать
    переносить закрывать соединять разрезать отсоединять складывать захватывать вставлять
    поднимать фиксировать опускать открывать колебаться размещать наливать нажимать тянуть
    толкать вынимать вращать скользить разблокировать
""")


def _semantic_text_is_grounded(
    value: str,
    facts_by_ref: Mapping[str, str],
    refs: Sequence[str],
) -> bool:
    source_tokens = set().union(
        *(_semantic_tokens(facts_by_ref.get(ref, "")) for ref in refs)
    ) if refs else set()
    value_tokens = _semantic_tokens(value)
    required_anchor_count = min(2, len(source_tokens))
    if (
        not value_tokens
        or not source_tokens
        or len(value_tokens & source_tokens) < required_anchor_count
    ):
        return False
    unsupported = value_tokens - source_tokens - _SEMANTIC_RELATION_VOCABULARY
    return not unsupported


def _raw_source_fragment_in_text(value: str, facts: Sequence[str]) -> bool:
    """Detect exact short facts and substantial verbatim fragments fail-closed."""
    value_words = _SEMANTIC_WORD_RE.findall(str(value).casefold())
    for fact in facts:
        fact_words = _SEMANTIC_WORD_RE.findall(str(fact).casefold())
        if not fact_words:
            continue
        if any(
            value_words[offset:offset + len(fact_words)] == fact_words
            for offset in range(len(value_words) - len(fact_words) + 1)
        ):
            return True
        if len(fact_words) < 4:
            continue
        for index in range(len(fact_words) - 3):
            fragment = fact_words[index:index + 4]
            if len(" ".join(fragment)) < 20:
                continue
            if any(
                value_words[offset:offset + 4] == fragment
                for offset in range(len(value_words) - 3)
            ):
                return True
    return False


def _unknown_semantic_numbers(values: Sequence[str], facts: Sequence[str]) -> tuple[str, ...]:
    known = set(_SEMANTIC_NUMBER_RE.findall(" ".join(facts)))
    unknown = {
        number
        for value in values
        for number in _SEMANTIC_NUMBER_RE.findall(value)
        if number not in known
    }
    return tuple(sorted(unknown))


def _source_semantic_axis(facts: Sequence[str]) -> str:
    source = " ".join(facts)
    # Publishing terms take precedence: a post/rotation episode must not be
    # silently converted into a fictional repair merely because it also names
    # a bot, test or repository.
    if _PUBLICATION_SOURCE_RE.search(source):
        return "publication_workflow"
    if _STRONG_PHYSICAL_SOURCE_RE.search(source):
        return "physical_mechanism"
    if _SOFTWARE_SOURCE_RE.search(source):
        return "software_validation"
    return "unknown"


def _visual_concept_is_generic(value: str) -> bool:
    normalized = " ".join(_SEMANTIC_WORD_RE.findall(str(value).casefold()))
    return normalized in _GENERIC_VISUAL_CONCEPTS or len(_semantic_tokens(value)) < 2


def _visual_relation_names_action(value: str, motion_class: str) -> bool:
    tokens = _SEMANTIC_WORD_RE.findall(str(value).casefold().replace("ё", "е"))
    return any(
        token.startswith(marker.strip())
        for marker in _MOTION_RELATION_MARKERS.get(motion_class, ())
        for token in tokens
    )


def _visual_relation_motion_classes(value: str) -> frozenset[str]:
    named = {
        motion_class
        for motion_class in _MOTION_RELATION_MARKERS
        if _visual_relation_names_action(value, motion_class)
    }
    return frozenset(named)


def _normalized_ref_list(
    value: Any,
    *,
    allowed: Sequence[str],
    code: str,
    errors: list[str],
) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        errors.append(code)
        return ()
    refs = tuple(str(item).strip() for item in value)
    if (
        any(ref not in allowed for ref in refs)
        or len(set(refs)) != len(refs)
    ):
        errors.append(code)
        return ()
    return refs
_DIRECTOR_INTERFACE_ACTION_RE = re.compile(
    r"(?i)(?=.*\b(?:types?|clicks?|taps?|press(?:es)?|swipes?|scrolls?|refreshes?)\b)"
    r"(?=.*\b(?:keyboard|trackpad|screen|laptop|phone|browser|terminal|interface)\b)"
)
_DIRECTOR_MAGIC_ACTION_RE = re.compile(
    r"(?i)\b(?:teleports?|levitates?|materiali[sz]es?|demateriali[sz]es?|morphs?|"
    r"self[- ]assembles?|magically|instant(?:ly)? transforms?)\b"
)
_DIRECTOR_MULTI_ACTION_RE = re.compile(
    r"(?i)(?:[;,]|\bthen\b|\bafter that\b|\bbefore that\b|\bwhile\b|"
    r"\bfollowed by\b|\bas\b|\buntil\b|\bwhen\b)"
)
_DIRECTOR_ABSTRACT_ACTION_RE = re.compile(
    r"(?i)\b(?:decides?|explains?|imagines?|observes?|realizes?|thinks?|understands?|"
    r"verifies?|watches?|waits?)\b"
)
_DIRECTOR_PASSIVE_ACTION_RE = re.compile(
    r"(?i)\b(?:lies?|looks?|remain(?:s|ing)?|rest(?:s|ing)?|"
    r"sit(?:s|ting)?|stand(?:s|ing)?|stare(?:s|ing)?)\b"
)
_DIRECTOR_MOTION_BASE_VERBS = {
    "align": "align", "bend": "bend", "carry": "carry", "close": "close",
    "connect": "connect", "cut": "cut", "disconnect": "disconnect",
    "fold": "fold", "grip": "grip", "insert": "insert", "lift": "lift",
    "lock": "lock", "lower": "lower", "open": "open",
    "oscillate": "oscillate", "place": "place", "pour": "pour",
    "press": "press", "pull": "pull", "push": "push", "remove": "remove",
    "rotate": "rotate", "slide": "slide", "unlock": "unlock",
}
_DIRECTOR_MOTION_THIRD_PERSON_VERBS = {
    **{name: f"{verb}s" for name, verb in _DIRECTOR_MOTION_BASE_VERBS.items()},
    "carry": "carries", "close": "closes", "cut": "cuts", "place": "places",
    "press": "presses", "push": "pushes", "remove": "removes",
    "slide": "slides",
}
_DIRECTOR_MOTION_GERUNDS = {
    "aligning", "bending", "carrying", "closing", "connecting", "cutting",
    "disconnecting", "folding", "gripping", "inserting", "lifting", "locking",
    "lowering", "opening", "oscillating", "placing", "pouring", "pressing",
    "pulling", "pushing", "removing", "rotating", "sliding", "unlocking",
}
def _normalized_action_tokens(value: str) -> list[str]:
    return [
        token for raw in value.casefold().split()
        if (token := re.sub(r"[^a-z-]", "", raw))
    ]


def _has_secondary_motion_predicate(predicate_words: Sequence[str]) -> bool:
    """Detect a second clause without treating action-like nouns as verbs."""
    tokens = _normalized_action_tokens(" ".join(predicate_words))
    if len(tokens) < 2:
        return False
    bases = set(_DIRECTOR_MOTION_BASE_VERBS.values())
    thirds = set(_DIRECTOR_MOTION_THIRD_PERSON_VERBS.values())
    determiners = {"a", "an", "the", "one"}
    for index, token in enumerate(tokens[1:], start=1):
        previous = tokens[index - 1]
        following = tokens[index + 1] if index + 1 < len(tokens) else ""
        if previous == "to" and token in bases:
            return True
        if token in thirds:
            return True
        if token in _DIRECTOR_MOTION_GERUNDS and following in determiners:
            return True
    if "and" in tokens:
        tail = " ".join(tokens[tokens.index("and") + 1:])
        if (
            any(token in thirds for token in _normalized_action_tokens(tail))
            or _DIRECTOR_ABSTRACT_ACTION_RE.search(tail)
            or _DIRECTOR_PASSIVE_ACTION_RE.search(tail)
        ):
            return True
    return False


def _branded_primary_phrase(primary: str, brand_marking: str) -> str:
    if brand_marking not in DIRECTOR_BRAND_MARKINGS or not primary.startswith("one "):
        return ""
    if brand_marking == "naz_ai_lab":
        return primary.replace("one ", "one Naz AI Lab-branded ", 1)
    return primary


def _bounded_state_phrase(continuity_anchor: str, state_code: str) -> str:
    label = DIRECTOR_STATE_LABELS.get(state_code, "")
    if not continuity_anchor or not label:
        return ""
    return f"{continuity_anchor} is {label}"


def _build_atomic_action(
    *,
    action_recipe: str,
    brand_marking: str,
) -> str:
    recipe = DIRECTOR_ACTION_RECIPES.get(action_recipe)
    if recipe is None:
        return ""
    subject_kind, motion_class, primary, contact = recipe
    third_person_verb = _DIRECTOR_MOTION_THIRD_PERSON_VERBS.get(motion_class, "")
    primary = _branded_primary_phrase(primary, brand_marking)
    if not third_person_verb or not primary:
        return ""
    contact_phrase = f" {contact}" if contact else ""
    if subject_kind == "naz_human":
        return f"Naz {third_person_verb} {primary}{contact_phrase}"
    if subject_kind == "physical_object":
        sentence_subject = primary[0].upper() + primary[1:]
        return f"{sentence_subject} {third_person_verb}{contact_phrase}"
    return ""


def _motion_contract_reason_codes(
    *,
    subject_kind: str,
    motion_class: str,
    action: str,
    start_state: str,
    end_state: str,
) -> tuple[str, ...]:
    """Validate one observable physical action independently of the LLM parser."""
    errors: list[str] = []
    base_verb = _DIRECTOR_MOTION_BASE_VERBS.get(motion_class)
    third_person_verb = _DIRECTOR_MOTION_THIRD_PERSON_VERBS.get(motion_class)
    if base_verb is None or third_person_verb is None:
        errors.append("motion_class_invalid")

    normalized_action = " ".join(action.split())
    action_words = normalized_action.split()
    predicate_words = action_words
    if subject_kind == "naz_human":
        if not normalized_action.casefold().startswith("naz "):
            errors.append("naz_action_subject_missing")
        else:
            predicate_words = action_words[1:]
        expected_verb = third_person_verb
        lead_verb = predicate_words[0].strip(".!").casefold() if predicate_words else ""
        known_physical_motion = bool(expected_verb and lead_verb == expected_verb)
    else:
        if normalized_action.casefold().startswith("naz "):
            errors.append("physical_object_action_subject_invalid")
        if motion_class not in DIRECTOR_SELF_MOTION_CLASSES:
            errors.append("motion_subject_incompatible")
        expected_verb = third_person_verb
        normalized_words = [word.strip(".!,").casefold() for word in action_words]
        verb_positions = [
            index for index, word in enumerate(normalized_words)
            if expected_verb and word == expected_verb
        ]
        known_physical_motion = len(verb_positions) == 1 and verb_positions[0] > 0
        predicate_words = (
            action_words[verb_positions[0]:]
            if known_physical_motion
            else action_words
        )
    if expected_verb and not known_physical_motion:
        errors.append("motion_class_mismatch")
    if (
        known_physical_motion
        and subject_kind == "naz_human"
        and len(predicate_words) < 2
    ):
        errors.append("physical_action_missing")
    if _DIRECTOR_INTERFACE_ACTION_RE.search(action):
        errors.append("interface_pantomime")
    if _DIRECTOR_MAGIC_ACTION_RE.search(action):
        errors.append("impossible_action")
    if _DIRECTOR_MULTI_ACTION_RE.search(action):
        errors.append("multi_action")
    if _has_secondary_motion_predicate(predicate_words):
        errors.append("multi_action")
    if _DIRECTOR_PASSIVE_ACTION_RE.search(action):
        errors.append("physical_action_missing")
    if _DIRECTOR_ABSTRACT_ACTION_RE.search(action):
        errors.append("abstract_action")
    if start_state and end_state and start_state.casefold() == end_state.casefold():
        errors.append("state_unchanged")
    return tuple(dict.fromkeys(errors))


def reels_director_prompt(
    plan: EditorialPlan,
    safe_facts: Sequence[str],
    *,
    variant_index: int = 0,
) -> str:
    """Build the bounded prompt for a content-specific, pre-render treatment."""
    facts = tuple(_safe_fact(item) for item in safe_facts)
    if len(facts) < 4:
        raise StoryPlanError("Story-first requires at least four safe causal facts")
    if not 0 <= variant_index <= 99:
        raise StoryPlanError("Story-first variant index must be 0..99")
    count = max(4, min(7, len(facts)))
    story_plan_id = _variant_plan_id(plan.plan_id, variant_index)
    roles = _roles(story_plan_id, count)
    beat_ids = _beat_ids(story_plan_id, count)
    fact_ids = _fact_ids(count)
    available_story_arcs = _story_arc_names_for_plan(plan)
    story_arc_catalog = {
        name: {
            "description": str(DIRECTOR_STORY_ARCS[name]["description"]),
            "ordered_actions": [
                _build_atomic_action(action_recipe=recipe, brand_marking="none")
                for recipe, _ in _story_arc_steps(name, count)
            ],
        }
        for name in available_story_arcs
    }
    brief = {
        "persona": "Naz, a real adult human founder",
        "topic": plan.topic,
        "editorial_thesis_direction": plan.thesis_direction,
        "tension": plan.tension,
        "imagery": plan.imagery,
        "visual_subject_direction": plan.visual_subject_direction,
        "visual_relation": plan.visual_relation,
        "available_story_arcs": story_arc_catalog,
        "variant_index": variant_index,
        "ordered_beats": [
            {"beat_id": beat_id, "role": role, "source_fact_ref": fact_id}
            for beat_id, role, fact_id in zip(beat_ids, roles, fact_ids)
        ],
        "source_facts": {
            fact_id: fact for fact_id, fact in zip(fact_ids, facts[:count])
        },
    }
    return (
        "Act as Reels Maker, the film director for Naz AI Lab. Convert the supplied verified "
        "episode into one immediately understandable, content-specific 9:16 micro-film. Do not reproduce file "
        "paths, headings, metadata, UI, code, dashboards, or the wording 'fact N'. Dramatize "
        "the causal meaning with physically filmable actions; do not merely decorate it with "
        "generic cyberpunk. Naz AI Lab is a flexible world, not one fixed server room. Choose "
        "one primary location and props because they express this episode. Whenever Naz is required, he remains human. Use Deep "
        "Black, Electric Blue, Ultraviolet and Ice Silver with materially believable optical "
        "glass, titanium, anodized aluminium, carbon, technical polymers or ceramic. No gold, "
        "copper branding, random robots, random boards, text, logos or overloaded HUDs. "
        f"Canonical wardrobe throughout: {CANONICAL_NAZ_WARDROBE}. Never invent "
        "armour, robes, glossy sci-fi costumes, ornamental uniforms or wardrobe changes.\n\n"
        "Stage one simple action that can be performed continuously in five seconds per scene. "
        "Every Naz action must begin with Naz, make direct contact with one visible prop, and "
        "cause the stated visible end state. Do not use symbolic gestures, passive watching, "
        "typing, clicking, trackpads, screen pantomime, magical transformation, floating parts, "
        "self-assembly or multi-step choreography. Props obey gravity and ordinary mechanics.\n\n"
        "First derive exactly one short core_thesis from one or more source_facts and cite every "
        "fact-N reference used by core_thesis, viewer_problem, hook or payoff. story_spine and "
        "visual_concept may use terms from any supplied source fact. editorial_thesis_direction "
        "is only an editorial axis, not the episode thesis. The hook and payoff both point to "
        "core_thesis and resolve the same claim. Do not "
        "invent numbers, named results or claims absent from cited facts. visual_concept must be "
        "content-specific; generic labels such as Lab, technology or digital future fail.\n\n"
        "For every ordered beat return its exact beat_id, a privacy-safe semantic_goal, one or "
        "more source_fact_refs, expected_viewer_understanding and visual_relation_to_beat. The "
        "source_fact_refs must include that ordered beat's supplied source_fact_ref; additional "
        "references are allowed. The "
        "first relation_to_previous is exactly 'opening'; every later one explains the transition "
        "from the previous semantic goal and repeats at least one cited content term from both "
        "the previous and current goals. Goals must be distinct. Every substantive noun, verb "
        "and quality term in semantic fields must occur in the cited source facts; use only "
        "neutral connective, cinematic-relation and physical-action words around them. Reorder "
        "and compress source terms: never copy any exact sequence of four or more source words, "
        "and never copy a short source fact in full. Do not quote raw private notes in visual "
        "fields.\n\n"
        "Do not write concrete_action, subject prose, materials, props, states, settings or any "
        "free-form action phrase. Choose exactly one story_arc from available_story_arcs. Each "
        "arc is a complete pre-vetted physical chain: one location, one mechanism, compatible "
        "materials, ordered actions, continuous states and observable final proof. The application "
        "expands it into scenes and identity requirements. Do not combine actions from different "
        "arcs or invent a second plot. Object-only steps move through a visible mechanical drive. "
        "Set visualization_kind to literal only when the cited fact really describes that physical "
        "mechanism. Otherwise physical_metaphor is allowed only when visual_relation_to_beat "
        "names that beat's ordered physical action and states its unambiguous correspondence "
        "without adding a result. It must name only that one motion: no second action, 'again', "
        "'twice', 'and then' or choreography. This relation is preflight evidence only and is "
        "never copied into a render prompt. If no fixed arc maps to the "
        "episode, the treatment must fail closed rather than become decoration.\n\n"
        "Return strict JSON only with this shape: "
        f'{{"director_version":"{DIRECTOR_VERSION}","core_thesis":"...",'
        '"thesis_source_fact_refs":["fact-1"],"viewer_problem":"...",'
        '"hook":"...","hook_thesis_ref":"core_thesis","payoff":"...",'
        '"payoff_thesis_ref":"core_thesis","visual_concept":"...",'
        '"story_spine":"...","story_arc":"module_recovery_mixed",'
        '"scenes":[{"beat_id":"beat-01-hook","semantic_goal":"...",'
        '"source_fact_refs":["fact-1"],"relation_to_previous":"opening",'
        '"expected_viewer_understanding":"...","visualization_kind":"literal|physical_metaphor",'
        '"visual_relation_to_beat":"...","shot_size":"wide|medium|close|macro","camera_motion":'
        '"slow push|controlled pan|handheld follow|locked with real subject motion"}]}. '
        "Do not return role names: the application assigns them deterministically. Return exactly "
        "one scene for every ordered beat, preserving supplied beat IDs and order. "
        "First define one concise story_spine (at most 180 characters) in cause-and-effect form: "
        "Naz or the physical system pursues one visible goal, meets one visible obstacle, "
        "performs one corrective test, "
        "and reaches one observable proof. A viewer with sound off must understand this by scene 2. "
        "The selected story_arc supplies the unresolved initial state, every distinct state change, "
        "the final proof, one continuity anchor and one physical set. Vary shot size inside that set "
        "instead of teleporting. Build one causal chain, not separate illustrations. "
        "Naz identity, every physical object, action and state are injected by the application from "
        "story_arc; do not return those fields. The application also creates exact Russian admin "
        "summaries from that same arc, so the approval card cannot diverge from the render prompts. "
        "Write visual_concept and story_spine in concise English.\n\n"
        + json.dumps(brief, ensure_ascii=False, separators=(",", ":"))
    )


def reels_director_response_format(
    safe_facts: Sequence[str],
    plan: EditorialPlan | None = None,
    *,
    variant_index: int = 0,
) -> dict[str, Any]:
    """OpenAI-compatible strict schema for the single director response."""
    count = max(4, min(7, len(tuple(safe_facts))))
    story_plan_id = (
        _variant_plan_id(plan.plan_id, variant_index) if plan is not None else "schema"
    )
    beat_ids = _beat_ids(story_plan_id, count)
    fact_ids = _fact_ids(count)
    scene_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "beat_id", "semantic_goal", "source_fact_refs",
            "relation_to_previous", "expected_viewer_understanding",
            "visualization_kind", "visual_relation_to_beat",
            "shot_size", "camera_motion",
        ],
        "properties": {
            "beat_id": {"type": "string", "enum": list(beat_ids)},
            "semantic_goal": {"type": "string", "maxLength": 240},
            "source_fact_refs": {
                "type": "array",
                "minItems": 1,
                "maxItems": count,
                "items": {"type": "string", "enum": list(fact_ids)},
            },
            "relation_to_previous": {"type": "string", "maxLength": 240},
            "expected_viewer_understanding": {"type": "string", "maxLength": 240},
            "visualization_kind": {
                "type": "string", "enum": list(VISUALIZATION_KINDS),
            },
            "visual_relation_to_beat": {"type": "string", "maxLength": 300},
            "shot_size": {"type": "string", "enum": list(SHOT_SIZES)},
            "camera_motion": {"type": "string", "enum": list(CAMERA_MOTIONS)},
        },
    }
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "naz_reels_director",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "director_version", "core_thesis", "thesis_source_fact_refs",
                    "viewer_problem", "hook", "hook_thesis_ref", "payoff",
                    "payoff_thesis_ref", "visual_concept", "story_spine",
                    "story_arc", "scenes",
                ],
                "properties": {
                    "director_version": {"type": "string", "enum": [DIRECTOR_VERSION]},
                    "core_thesis": {"type": "string", "maxLength": 300},
                    "thesis_source_fact_refs": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": count,
                        "items": {"type": "string", "enum": list(fact_ids)},
                    },
                    "viewer_problem": {"type": "string", "maxLength": 240},
                    "hook": {"type": "string", "maxLength": 240},
                    "hook_thesis_ref": {
                        "type": "string", "enum": [CORE_THESIS_REF],
                    },
                    "payoff": {"type": "string", "maxLength": 240},
                    "payoff_thesis_ref": {
                        "type": "string", "enum": [CORE_THESIS_REF],
                    },
                    "visual_concept": {"type": "string", "maxLength": 1200},
                    "story_spine": {"type": "string", "maxLength": 180},
                    "story_arc": {
                        "type": "string",
                        "enum": list(_story_arc_names_for_plan(plan)),
                    },
                    "scenes": {
                        "type": "array",
                        "minItems": count,
                        "maxItems": count,
                        "items": scene_schema,
                    },
                },
            },
        },
    }


def _director_text(
    value: Any,
    code_prefix: str,
    errors: list[str],
    *,
    maximum: int = 600,
) -> str:
    """Normalize harmless prose variance and collect safety errors without echoing text."""
    if not isinstance(value, str):
        errors.append(f"{code_prefix}_missing")
        return ""
    text = " ".join(value.split())
    if not text:
        errors.append(f"{code_prefix}_missing")
    elif len(text) > maximum:
        errors.append(f"{code_prefix}_too_long")
    if _SECRET_RE.search(text):
        errors.append(f"{code_prefix}_secret")
    if _DIRECTOR_TRANSPORT_RE.search(text):
        errors.append(f"{code_prefix}_metadata")
    if _DIRECTOR_FORBIDDEN_CLICHE_RE.search(text):
        errors.append(f"{code_prefix}_cliche")
    return text


def _validated_director_text(value: Any, name: str, *, maximum: int = 1200) -> str:
    errors: list[str] = []
    text = _director_text(value, f"director_{name}", errors, maximum=maximum)
    if errors:
        raise DirectorValidationError(errors)
    return text


def _director_ru_text(
    value: Any,
    code_prefix: str,
    errors: list[str],
    *,
    maximum: int = 240,
) -> str:
    text = _director_text(value, code_prefix, errors, maximum=maximum)
    if text and not re.search(r"[А-Яа-яЁё]", text):
        errors.append(f"{code_prefix}_not_russian")
    return text


def _validated_director_ru_text(value: Any, name: str, *, maximum: int = 240) -> str:
    errors: list[str] = []
    text = _director_ru_text(value, f"director_{name}", errors, maximum=maximum)
    if errors:
        raise DirectorValidationError(errors)
    return text


def parse_reels_director_response(
    raw: str,
    plan: EditorialPlan,
    safe_facts: Sequence[str],
    *,
    variant_index: int = 0,
) -> DirectorTreatment:
    """Validate the entire typed treatment before it can become immutable."""
    try:
        payload = json.loads(str(raw).strip())
    except (TypeError, json.JSONDecodeError) as exc:
        raise DirectorValidationError(("director_json_invalid",)) from exc
    if not isinstance(payload, Mapping) or payload.get("director_version") != DIRECTOR_VERSION:
        raise DirectorValidationError(("director_schema_invalid",))
    facts = tuple(_safe_fact(item) for item in safe_facts)
    if len(facts) < 4:
        raise DirectorValidationError(("director_source_facts_insufficient",))
    count = max(4, min(7, len(facts)))
    story_plan_id = _variant_plan_id(plan.plan_id, variant_index)
    expected_roles = _roles(story_plan_id, count)
    expected_beat_ids = _beat_ids(story_plan_id, count)
    allowed_fact_refs = _fact_ids(count)
    rows = payload.get("scenes")
    if not isinstance(rows, list) or len(rows) != count:
        raise DirectorValidationError(("director_scene_count_invalid",))

    errors: list[str] = []
    if set(payload) != {
        "director_version", "core_thesis", "thesis_source_fact_refs",
        "viewer_problem", "hook", "hook_thesis_ref", "payoff",
        "payoff_thesis_ref", "visual_concept", "story_spine", "story_arc",
        "scenes",
    }:
        errors.append("director_schema_invalid")
    core_thesis = _director_text(
        payload.get("core_thesis"), "director_core_thesis", errors, maximum=300,
    )
    thesis_source_fact_refs = _normalized_ref_list(
        payload.get("thesis_source_fact_refs"),
        allowed=allowed_fact_refs,
        code="director_thesis_source_fact_refs_invalid",
        errors=errors,
    )
    viewer_problem = _director_text(
        payload.get("viewer_problem"), "director_viewer_problem", errors, maximum=240,
    )
    hook = _director_text(payload.get("hook"), "director_hook", errors, maximum=240)
    payoff = _director_text(payload.get("payoff"), "director_payoff", errors, maximum=240)
    if (
        payload.get("hook_thesis_ref") != CORE_THESIS_REF
        or payload.get("payoff_thesis_ref") != CORE_THESIS_REF
    ):
        errors.append("director_hook_payoff_mismatch")
    visual_concept = _director_text(
        payload.get("visual_concept"),
        "director_visual_concept",
        errors,
        maximum=1200,
    )
    if visual_concept and _visual_concept_is_generic(visual_concept):
        errors.append("director_visual_concept_generic")
    story_spine = _director_text(
        payload.get("story_spine"),
        "director_story_spine",
        errors,
        maximum=180,
    )
    allowed_story_arcs = _story_arc_names_for_plan(plan)
    story_arc = str(payload.get("story_arc", "")).strip().casefold()
    if story_arc not in allowed_story_arcs:
        errors.append("director_story_arc_invalid")
        selected_story_arc = allowed_story_arcs[0]
    else:
        selected_story_arc = story_arc
    arc = DIRECTOR_STORY_ARCS[selected_story_arc]
    try:
        arc_steps = _story_arc_steps(selected_story_arc, count)
    except StoryPlanError:
        errors.append("director_story_arc_invalid")
        arc_steps = ()
    primary_setting_code = str(arc.get("setting", ""))
    primary_setting = DIRECTOR_PRIMARY_SETTINGS.get(primary_setting_code, "")
    continuity_anchor = str(arc.get("continuity_anchor", ""))
    initial_state_code = str(arc.get("initial_state", ""))
    goal_state_code = arc_steps[-1][1] if arc_steps else ""
    if (
        not primary_setting
        or not continuity_anchor
        or len(continuity_anchor) > 90
        or initial_state_code not in DIRECTOR_STATE_CODES
        or goal_state_code not in DIRECTOR_STATE_CODES
        or initial_state_code == goal_state_code
    ):
        errors.append("director_story_arc_invalid")
    admin_concept_ru = _director_ru_text(
        arc.get("description_ru"),
        "director_admin_concept_ru",
        errors,
    )
    scenes: list[DirectorScene] = []
    actions: list[str] = []
    end_state_codes: list[str] = []
    previous_state_code = initial_state_code
    expected_scene_fields = {
        "beat_id", "semantic_goal", "source_fact_refs",
        "relation_to_previous", "expected_viewer_understanding",
        "visualization_kind", "visual_relation_to_beat",
        "shot_size", "camera_motion",
    }
    for index, (row, expected_role, expected_beat_id, arc_step) in enumerate(
        zip(rows, expected_roles, expected_beat_ids, arc_steps)
    ):
        scene_number = index + 1
        scene_prefix = f"director_scene_{scene_number}"
        scene_error_start = len(errors)
        if not isinstance(row, Mapping):
            errors.append(f"{scene_prefix}_schema_invalid")
            continue
        if set(row) != expected_scene_fields:
            errors.append(f"{scene_prefix}_schema_invalid")

        beat_id = str(row.get("beat_id", "")).strip()
        if beat_id != expected_beat_id:
            errors.append(f"{scene_prefix}_beat_id_invalid")
        semantic_goal = _director_text(
            row.get("semantic_goal"),
            f"{scene_prefix}_semantic_goal",
            errors,
            maximum=240,
        )
        source_fact_refs = _normalized_ref_list(
            row.get("source_fact_refs"),
            allowed=allowed_fact_refs,
            code=f"{scene_prefix}_source_fact_refs_invalid",
            errors=errors,
        )
        relation_to_previous = _director_text(
            row.get("relation_to_previous"),
            f"{scene_prefix}_relation_to_previous",
            errors,
            maximum=240,
        )
        if index == 0:
            if relation_to_previous.casefold() != "opening":
                errors.append(f"{scene_prefix}_relation_to_previous_invalid")
        elif not relation_to_previous or relation_to_previous.casefold() == "opening":
            errors.append(f"{scene_prefix}_relation_to_previous_missing")
        expected_viewer_understanding = _director_text(
            row.get("expected_viewer_understanding"),
            f"{scene_prefix}_expected_viewer_understanding",
            errors,
            maximum=240,
        )
        visualization_kind = str(row.get("visualization_kind", "")).strip().casefold()
        if visualization_kind not in VISUALIZATION_KINDS:
            errors.append(f"{scene_prefix}_visualization_kind_invalid")
        visual_relation_to_beat = _director_text(
            row.get("visual_relation_to_beat"),
            f"{scene_prefix}_visual_relation_to_beat",
            errors,
            maximum=300,
        )

        action_recipe, end_state_code = arc_step
        start_state = _bounded_state_phrase(continuity_anchor, previous_state_code)
        end_state = _bounded_state_phrase(continuity_anchor, end_state_code)
        admin_summary_ru = _director_ru_text(
            DIRECTOR_RECIPE_SUMMARIES_RU.get(action_recipe),
            f"{scene_prefix}_admin_summary_ru",
            errors,
        )

        recipe = DIRECTOR_ACTION_RECIPES.get(action_recipe)
        if recipe is None:
            errors.append("director_story_arc_invalid")
            subject_kind = ""
            motion_class = ""
            primary = ""
        else:
            subject_kind, motion_class, primary, _contact = recipe
        primary = _branded_primary_phrase(primary, "none")
        if subject_kind == "naz_human":
            subject = CANONICAL_NAZ_SUBJECT
        elif subject_kind == "physical_object":
            subject = primary
        else:
            subject = ""
        action = _build_atomic_action(
            action_recipe=action_recipe,
            brand_marking="none",
        )
        if action:
            errors.extend(
                f"{scene_prefix}_{reason}"
                for reason in _motion_contract_reason_codes(
                    subject_kind=subject_kind,
                    motion_class=motion_class,
                    action=action,
                    start_state=start_state,
                    end_state=end_state,
                )
            )

        shot_size = str(row.get("shot_size", "")).strip().casefold()
        camera_motion = str(row.get("camera_motion", "")).strip().casefold()
        if shot_size not in SHOT_SIZES:
            errors.append(f"{scene_prefix}_shot_size_invalid")
        if camera_motion not in CAMERA_MOTIONS:
            errors.append(f"{scene_prefix}_camera_motion_invalid")
        if end_state_code in DIRECTOR_STATE_CODES:
            end_state_codes.append(end_state_code)
            previous_state_code = end_state_code

        if action:
            actions.append(action)
        if len(errors) == scene_error_start:
            scenes.append(
                DirectorScene(
                    role=expected_role,
                    beat_id=beat_id,
                    semantic_goal=semantic_goal,
                    source_fact_refs=source_fact_refs,
                    relation_to_previous=relation_to_previous,
                    expected_viewer_understanding=expected_viewer_understanding,
                    visualization_kind=visualization_kind,
                    visual_relation_to_beat=visual_relation_to_beat,
                    setting=primary_setting,
                    subject_kind=subject_kind,
                    subject=subject,
                    motion_class=motion_class,
                    concrete_action=action,
                    start_state=start_state,
                    end_state=end_state,
                    shot_size=shot_size,
                    camera_motion=camera_motion,
                    admin_summary_ru=admin_summary_ru,
                )
            )

    if len(actions) == count and len({item.casefold() for item in actions}) != count:
        errors.append("director_actions_repetitive")
    if (
        len(actions) != count
        or len(end_state_codes) != count
        or len(set(end_state_codes)) != count
        or not end_state_codes
        or end_state_codes[-1] != goal_state_code
    ):
        errors.append("director_story_arc_invalid")
    if errors:
        raise DirectorValidationError(errors)
    treatment = DirectorTreatment(
        plan_id=story_plan_id,
        variant_index=variant_index,
        story_arc=selected_story_arc,
        visual_concept=visual_concept,
        core_thesis=core_thesis,
        thesis_source_fact_refs=thesis_source_fact_refs,
        viewer_problem=viewer_problem,
        hook=hook,
        payoff=payoff,
        story_spine=story_spine,
        continuity_anchor=continuity_anchor,
        initial_state_code=initial_state_code,
        goal_state_code=goal_state_code,
        primary_setting=primary_setting,
        admin_concept_ru=admin_concept_ru,
        scenes=tuple(scenes),
    )
    semantic_errors = semantic_preflight_reason_codes(
        treatment,
        facts,
        plan=plan,
        variant_index=variant_index,
    )
    if semantic_errors:
        raise DirectorValidationError(semantic_errors)
    return treatment


def validate_provider_prompt(value: str) -> str:
    """Defense-in-depth check immediately before a prompt leaves the host."""
    text = " ".join(str(value).split())
    if not text or len(text) > 6000 or _SECRET_RE.search(text):
        raise StoryPlanError("provider_prompt_unsafe")
    return text


def _roles(plan_id: str, count: int) -> list[str]:
    del plan_id  # Scene variation must never scramble the causal story order.
    sequences = {
        4: ("hook", "problem", "test", "result"),
        5: ("hook", "problem", "test", "result", "conclusion"),
        6: ("hook", "problem", "hypothesis", "test", "result", "conclusion"),
        7: DRAMATURGIC_ROLES,
    }
    try:
        return list(sequences[count])
    except KeyError as exc:
        raise StoryPlanError("scene count must be 4..7") from exc


def _requires_reference(subject: str) -> bool:
    folded = subject.casefold()
    return bool(
        re.search(
            r"(?:\bnaz\b|\bназ\b|\bface\b|\bportrait\b|\bлицо\b|\bпортрет\b)",
            folded,
        )
    )


def _object_only_direction(subject: str) -> bool:
    return bool(
        re.search(
            r"(?i)(?:\bobject[- ]only\b|\bno person\b|\bwithout (?:a )?person\b|"
            r"\bno invented human\b|\bбез человека\b|\bбез людей\b)",
            subject,
        )
    )


def _human_led_direction(subject: str) -> bool:
    return bool(re.search(r"(?i)(?:\bhuman[- ]led\b|\bhuman[- ]performed\b)", subject))


def _story_arc_names_for_plan(plan: EditorialPlan | None) -> tuple[str, ...]:
    if plan is None:
        return DIRECTOR_STORY_ARC_NAMES
    direction = plan.visual_subject_direction
    if _object_only_direction(direction):
        modes = {"object"}
    elif _requires_reference(direction):
        modes = {"human"}
    elif _human_led_direction(direction):
        modes = {"human", "mixed"}
    else:
        modes = {"human", "mixed", "object"}
    arcs = tuple(
        name
        for name, arc in DIRECTOR_STORY_ARCS.items()
        if arc.get("subject_mode") in modes
    )
    if not arcs:
        raise StoryPlanError("director_story_arc_unavailable")
    return arcs


def _story_arc_steps(
    story_arc: str,
    scene_count: int,
) -> tuple[tuple[str, str], ...]:
    arc = DIRECTOR_STORY_ARCS.get(story_arc)
    if arc is None or not 4 <= scene_count <= 7:
        raise StoryPlanError("director_story_arc_invalid")
    rows = arc.get("steps")
    if not isinstance(rows, tuple):
        raise StoryPlanError("director_story_arc_invalid")
    selected = tuple(
        (str(recipe), str(end_state))
        for recipe, end_state, minimum_count in rows
        if int(minimum_count) <= scene_count
    )
    if (
        len(selected) != scene_count
        or len({recipe for recipe, _ in selected}) != scene_count
        or len({end_state for _, end_state in selected}) != scene_count
        or any(recipe not in DIRECTOR_ACTION_RECIPES for recipe, _ in selected)
        or any(recipe not in DIRECTOR_RECIPE_SUMMARIES_RU for recipe, _ in selected)
        or any(end_state not in DIRECTOR_STATE_CODES for _, end_state in selected)
    ):
        raise StoryPlanError("director_story_arc_invalid")
    return selected


def _semantic_goals_repeat(left: str, right: str) -> bool:
    left_tokens = _semantic_tokens(left) - _SEMANTIC_RELATION_VOCABULARY
    right_tokens = _semantic_tokens(right) - _SEMANTIC_RELATION_VOCABULARY
    if not left_tokens or not right_tokens:
        return False
    overlap = len(left_tokens & right_tokens)
    return (
        " ".join(left.casefold().split()) == " ".join(right.casefold().split())
        or overlap / max(1, min(len(left_tokens), len(right_tokens))) >= 0.85
    )


def semantic_preflight_reason_codes(
    treatment: DirectorTreatment,
    safe_facts: Sequence[str],
    *,
    plan: EditorialPlan,
    variant_index: int = 0,
) -> tuple[str, ...]:
    """Deterministically reject ungrounded semantic plans before persistence.

    This is deliberately conservative. It verifies references, lexical evidence,
    transitions and the declared literal/metaphor relation; it does not add a
    second model call or try to rescue a doubtful treatment.
    """
    facts = tuple(_safe_fact(item) for item in safe_facts)
    count = max(4, min(7, len(facts)))
    allowed_refs = _fact_ids(count)
    facts_by_ref = dict(zip(allowed_refs, facts[:count]))
    story_plan_id = _variant_plan_id(plan.plan_id, variant_index)
    expected_roles = _roles(story_plan_id, count)
    expected_beats = _beat_ids(story_plan_id, count)
    errors: list[str] = []
    numeric_claim_invalid = False

    if treatment.plan_id != story_plan_id or treatment.variant_index != variant_index:
        errors.append("director_treatment_plan_binding_invalid")

    thesis_refs = tuple(treatment.thesis_source_fact_refs)
    thesis_facts = tuple(
        facts_by_ref[ref] for ref in thesis_refs if ref in facts_by_ref
    )
    if (
        not thesis_refs
        or any(ref not in facts_by_ref for ref in thesis_refs)
        or len(set(thesis_refs)) != len(thesis_refs)
    ):
        errors.append("director_thesis_source_fact_refs_invalid")
    elif not _semantic_text_is_grounded(treatment.core_thesis, facts_by_ref, thesis_refs):
        errors.append("director_core_thesis_unsupported")

    for name, value in (
        ("viewer_problem", treatment.viewer_problem),
        ("hook", treatment.hook),
        ("payoff", treatment.payoff),
    ):
        if not value or not _semantic_text_is_grounded(value, facts_by_ref, thesis_refs):
            errors.append(f"director_{name}_unsupported")
        if _unknown_semantic_numbers((value,), thesis_facts):
            numeric_claim_invalid = True
    for name, value in (
        ("story_spine", treatment.story_spine),
        ("visual_concept", treatment.visual_concept),
    ):
        if not value or not _semantic_text_is_grounded(value, facts_by_ref, allowed_refs):
            errors.append(f"director_{name}_unsupported")
        if _unknown_semantic_numbers((value,), facts[:count]):
            numeric_claim_invalid = True
    if _unknown_semantic_numbers((treatment.core_thesis,), thesis_facts):
        numeric_claim_invalid = True

    thesis_tokens = _semantic_tokens(treatment.core_thesis)
    thesis_source_tokens = set().union(
        *(_semantic_tokens(facts_by_ref.get(ref, "")) for ref in thesis_refs)
    ) if thesis_refs else set()
    thesis_content_tokens = (
        thesis_tokens & thesis_source_tokens
    ) - _SEMANTIC_RELATION_VOCABULARY
    if (
        not (_semantic_tokens(treatment.hook) & thesis_content_tokens)
        or not (_semantic_tokens(treatment.payoff) & thesis_content_tokens)
    ):
        errors.append("director_hook_payoff_mismatch")
    if _visual_concept_is_generic(treatment.visual_concept):
        errors.append("director_visual_concept_generic")

    if len(treatment.scenes) != count:
        errors.append("director_scene_count_invalid")
        return tuple(dict.fromkeys(errors))

    semantic_values = [
        treatment.core_thesis,
        treatment.viewer_problem,
        treatment.hook,
        treatment.payoff,
        treatment.story_spine,
        treatment.visual_concept,
    ]
    goals: list[str] = []
    previous_goal = ""
    previous_refs: tuple[str, ...] = ()
    source_axis = _source_semantic_axis(facts[:count])
    arc_semantically_allowed = (
        source_axis in DIRECTOR_ARC_SEMANTIC_AXES.get(treatment.story_arc, frozenset())
    )
    for index, scene in enumerate(treatment.scenes):
        prefix = f"director_scene_{index + 1}"
        if scene.role != expected_roles[index] or scene.beat_id != expected_beats[index]:
            errors.append(f"{prefix}_beat_id_invalid")
        refs = tuple(scene.source_fact_refs)
        scene_facts = tuple(
            facts_by_ref[ref] for ref in refs if ref in facts_by_ref
        )
        assigned_ref = allowed_refs[index]
        assigned_fact_tokens = _semantic_tokens(facts_by_ref.get(assigned_ref, ""))
        assigned_anchor_count = min(2, len(assigned_fact_tokens))
        assigned_source_axis = _source_semantic_axis(
            (facts_by_ref.get(assigned_ref, ""),)
        )
        if assigned_source_axis not in DIRECTOR_ARC_SEMANTIC_AXES.get(
            treatment.story_arc, frozenset()
        ):
            arc_semantically_allowed = False
        if (
            not refs
            or any(ref not in facts_by_ref for ref in refs)
            or len(set(refs)) != len(refs)
            or assigned_ref not in refs
        ):
            errors.append(f"{prefix}_source_fact_refs_invalid")
        elif not _semantic_text_is_grounded(scene.semantic_goal, facts_by_ref, refs):
            errors.append(f"{prefix}_semantic_goal_unsupported")
        elif len(
            _semantic_tokens(scene.semantic_goal) & assigned_fact_tokens
        ) < assigned_anchor_count:
            errors.append(f"{prefix}_semantic_goal_unsupported")
        if not _semantic_text_is_grounded(
            scene.expected_viewer_understanding, facts_by_ref, refs,
        ):
            errors.append(f"{prefix}_expected_viewer_understanding_unsupported")
        elif len(
            _semantic_tokens(scene.expected_viewer_understanding)
            & assigned_fact_tokens
        ) < assigned_anchor_count:
            errors.append(f"{prefix}_expected_viewer_understanding_unsupported")
        if not _semantic_text_is_grounded(scene.visual_relation_to_beat, facts_by_ref, refs):
            errors.append(f"{prefix}_visual_relation_unsupported")
        if _unknown_semantic_numbers(
            (
                scene.semantic_goal,
                scene.expected_viewer_understanding,
                scene.visual_relation_to_beat,
            ),
            scene_facts,
        ):
            numeric_claim_invalid = True
        if not _PHYSICAL_RELATION_RE.search(scene.visual_relation_to_beat):
            errors.append(f"{prefix}_visual_relation_unsupported")
            arc_semantically_allowed = False
        if not _visual_relation_names_action(
            scene.visual_relation_to_beat, scene.motion_class,
        ):
            arc_semantically_allowed = False
        named_motion_classes = _visual_relation_motion_classes(
            scene.visual_relation_to_beat
        )
        if (
            named_motion_classes - {scene.motion_class}
            or _MULTI_ACTION_RELATION_RE.search(scene.visual_relation_to_beat)
        ):
            errors.append(f"{prefix}_multiple_actions")
            arc_semantically_allowed = False
        cited_tokens = set().union(
            *(_semantic_tokens(facts_by_ref.get(ref, "")) for ref in refs)
        ) if refs else set()
        relation_tokens = _semantic_tokens(scene.visual_relation_to_beat)
        semantic_goal_tokens = _semantic_tokens(scene.semantic_goal)
        if not (relation_tokens & semantic_goal_tokens & cited_tokens):
            errors.append(f"{prefix}_visual_relation_unsupported")
            arc_semantically_allowed = False
        if (
            len(relation_tokens & assigned_fact_tokens) < assigned_anchor_count
            or not (
                relation_tokens
                & semantic_goal_tokens
                & assigned_fact_tokens
            )
        ):
            errors.append(f"{prefix}_visual_relation_unsupported")
            arc_semantically_allowed = False
        if scene.visualization_kind not in VISUALIZATION_KINDS:
            errors.append(f"{prefix}_visualization_kind_invalid")
        elif scene.visualization_kind == "physical_metaphor":
            required_anchor_count = min(2, len(cited_tokens))
            if (
                len(relation_tokens & cited_tokens) < required_anchor_count
            ):
                errors.append(f"{prefix}_visual_relation_unsupported")
                arc_semantically_allowed = False
        elif scene.visualization_kind == "literal":
            scene_source_axis = _source_semantic_axis(
                tuple(facts_by_ref.get(ref, "") for ref in refs)
            )
            if scene_source_axis != "physical_mechanism":
                errors.append(f"{prefix}_literal_source_mismatch")
                arc_semantically_allowed = False

        if index == 0:
            if scene.relation_to_previous.casefold() != "opening":
                errors.append(f"{prefix}_relation_to_previous_invalid")
        else:
            relation_tokens = _semantic_tokens(scene.relation_to_previous)
            previous_content_tokens = (
                _semantic_tokens(previous_goal) - _SEMANTIC_RELATION_VOCABULARY
            )
            current_content_tokens = (
                _semantic_tokens(scene.semantic_goal) - _SEMANTIC_RELATION_VOCABULARY
            )
            transition_refs = tuple(dict.fromkeys((*previous_refs, *refs)))
            transition_facts = tuple(
                facts_by_ref[ref]
                for ref in transition_refs
                if ref in facts_by_ref
            )
            if (
                not scene.relation_to_previous
                or scene.relation_to_previous.casefold() == "opening"
                or not (relation_tokens & previous_content_tokens)
                or not (relation_tokens & current_content_tokens)
                or not _semantic_text_is_grounded(
                    scene.relation_to_previous, facts_by_ref, transition_refs,
                )
            ):
                errors.append(f"{prefix}_relation_to_previous_missing")
            if _unknown_semantic_numbers(
                (scene.relation_to_previous,), transition_facts,
            ):
                numeric_claim_invalid = True

        semantic_values.extend((
            scene.semantic_goal,
            scene.relation_to_previous,
            scene.expected_viewer_understanding,
            scene.visual_relation_to_beat,
        ))
        if any(_semantic_goals_repeat(scene.semantic_goal, goal) for goal in goals):
            errors.append("director_semantic_goal_duplicate")
        goals.append(scene.semantic_goal)
        previous_goal = scene.semantic_goal
        previous_refs = refs

    if not arc_semantically_allowed:
        errors.append("director_story_arc_semantic_mismatch")
    if any(
        _raw_source_fragment_in_text(value, facts[:count])
        for value in semantic_values
    ):
        errors.append("director_raw_source_copy")
    if numeric_claim_invalid:
        errors.append("director_unknown_numeric_claim")
    return tuple(dict.fromkeys(errors))


def _mentions_human_subject(subject: str) -> bool:
    return bool(
        re.search(
            r"(?i)(?:\bperson\b|\bhuman\b|\bman\b|\bwoman\b|\bfounder\b|"
            r"\bengineer\b|\btechnician\b|\boperator\b|\bчеловек\b|\bмужчин[аы]\b|"
            r"\bженщин[аы]\b|\bинженер\b|\bоператор\b)",
            subject,
        )
    )


def _is_naz_human_subject(subject: str) -> bool:
    return bool(re.search(r"(?i)(?:\bnaz\b|\bназ\b)", subject)) and _mentions_human_subject(subject)


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


def _provider_excerpt(value: str, maximum: int) -> str:
    """Keep the immutable director field intact while bounding provider prose."""
    text = " ".join(str(value).split())
    if len(text) <= maximum:
        return text
    compacted = text[:maximum].rstrip()
    if " " in compacted:
        compacted = compacted.rsplit(" ", 1)[0].rstrip(" ,;:-")
    return compacted


def _compile_scene_prompts(
    *,
    role: str,
    visual_mode: str,
    visual_relation: str,
    semantic_goal: str,
    visual_relation_to_beat: str,
    subject_kind: str,
    subject: str,
    setting: str,
    action: str,
    start_state: str,
    end_state: str,
    shot_size: str,
    camera_motion: str,
    continuity_anchor: str,
    requires_reference: bool,
) -> tuple[str, str, str]:
    """Compile every render prompt from one approved, privacy-safe scene brief."""
    # The relation is evidence for semantic preflight, not motion prose. Passing
    # it downstream could smuggle a second source-grounded verb beside the one
    # canonical action supplied by the selected arc.
    del visual_relation_to_beat
    prompt_setting = _provider_excerpt(setting, 120)
    prompt_subject = _provider_excerpt(subject, 120)
    prompt_action = _provider_excerpt(action, 180)
    prompt_start_state = _provider_excerpt(start_state, 180)
    prompt_end_state = _provider_excerpt(end_state, 180)
    prompt_continuity_anchor = _provider_excerpt(continuity_anchor, 90)
    prompt_semantic_goal = _provider_excerpt(semantic_goal, 180)

    clean_prompt = (
        f"Vertical 9:16 CLEAN video master. {visual_mode}. Role: {role}. "
        f"Narrative meaning only; never animate it as an extra action: {prompt_semantic_goal}. "
        f"Subject: {subject}. "
        f"Setting: {setting}. Start state: {start_state}. One physical action: {action}. "
        f"End state: {end_state}. {visual_relation} "
        "Real motion/action is mandatory. No text, captions, logos, platform buttons, stickers, "
        "reactions or watermarks."
    )
    identity_instruction = (
        f"Use @Naz for identity and build only. Wardrobe: {CANONICAL_NAZ_WARDROBE}; "
        "replace the reference background, pose and light. "
        if requires_reference else "No person is present. "
    )
    subject_instruction = (
        ""
        if requires_reference
        else (
            f"Subject: {prompt_subject}. Show its visible physical hinge, rail or actuator; "
            "nothing floats or moves by itself. "
        )
    )
    keyframe_prompt = (
        f"Cinematic vertical 9:16. {identity_instruction}"
        f"Narrative meaning only; do not depict an extra action: {prompt_semantic_goal}. "
        f"Location: {prompt_setting}. "
        f"{subject_instruction}Start state: {prompt_start_state}. Action: {prompt_action}. "
        f"Story anchor: {prompt_continuity_anchor}. Shot: {shot_size}; camera: {camera_motion}. "
        "Photoreal Naz AI Lab: human intelligence, machine precision. "
        "Deep Black #020309, Electric Blue #185CFF, Ultraviolet #762DFF, Ice Silver #D7E5FF. "
        "Real optical glass, titanium, blue aluminium, carbon, ceramic; cold rim light. "
        "No text, logos, HUD, code, copper, gold, neon cliche, robots or extra people."
    )
    motion_continuity = (
        "Naz keeps the exact face, build and matte-black wardrobe from the first frame. "
        "His hands contact only the visible prop and move with believable weight. "
        if requires_reference
        else (
            "The same single physical object remains in frame and moves with believable weight. "
            "Its visible mechanical actuator, hinge or rail drives the one motion; no self-animation. "
        )
    )
    provider_prompt = (
        "Continuous seamless five-second shot from the supplied directed keyframe. "
        f"Narrative meaning only; do not animate it as another action: {prompt_semantic_goal}. "
        f"{CAMERA_MOTION_PROMPTS[camera_motion]}. One physical action: {prompt_action}. "
        f"The action starts with this visible state: {prompt_start_state}. "
        f"The action finishes with this visible state: {prompt_end_state}. "
        f"{motion_continuity}"
        "The first-frame architecture, materials and lighting remain stable for the entire take."
    )
    try:
        return (
            clean_prompt,
            compact_runway_prompt(
                keyframe_prompt,
                too_long_code="keyframe_prompt_too_long",
            ),
            compact_runway_prompt(provider_prompt),
        )
    except ProviderError as exc:
        raise StoryPlanError(exc.code) from exc


def _scene(
    plan: EditorialPlan, *, continuity_id: str, role: str, index: int, fact: str,
    treatment_key: str,
    directed_scene: DirectorScene | None = None,
    directed_concept: str = "",
    directed_story_spine: str = "",
    directed_continuity_anchor: str = "",
) -> ScenePlan:
    # Both configured Runway tiers accept a five-second master.  Keeping the
    # provider master fixed also makes Reel timing and credit accounting exact.
    duration = 5
    requires_reference = (
        directed_scene.subject_kind == "naz_human"
        if directed_scene is not None
        else _requires_reference(plan.visual_subject_direction)
    )
    subject_kind = (
        directed_scene.subject_kind
        if directed_scene is not None
        else "naz_human" if requires_reference else "physical_object"
    )
    motion_class = directed_scene.motion_class if directed_scene is not None else None
    subject = (
        directed_scene.subject
        if directed_scene is not None
        else CANONICAL_NAZ_SUBJECT
        if requires_reference
        else "one physical Naz AI Lab prototype"
    )
    shot_size = (
        directed_scene.shot_size
        if directed_scene is not None
        else SHOT_SIZES[_rank(plan.plan_id, f"shot:{index}") % len(SHOT_SIZES)]
    )
    reference_role = (
        "three_quarter_identity"
        if requires_reference and shot_size in {"wide", "medium"}
        else "frontal_identity"
        if requires_reference
        else "none"
    )
    treatment = VISUAL_TREATMENTS[treatment_key]
    if directed_scene is not None:
        setting = directed_scene.setting
        action = directed_scene.concrete_action
        start_state = directed_scene.start_state
        end_state = directed_scene.end_state
        camera_motion = directed_scene.camera_motion
        admin_summary_ru = directed_scene.admin_summary_ru
        visual_concept = directed_concept
        story_spine = directed_story_spine
        continuity_anchor = directed_continuity_anchor
    else:
        setting, action, end_state = treatment["beats"][role]
        if not requires_reference:
            action, end_state = OBJECT_ONLY_ACTIONS[role]
        start_state = "before the documented action changes the situation"
        camera_motion = CAMERA_MOTIONS[_rank(plan.plan_id, f"motion:{index}") % len(CAMERA_MOTIONS)]
        admin_summary_ru = ""
        visual_concept = str(treatment["label"])
        story_spine = visual_concept
        continuity_anchor = "one evolving physical Naz AI Lab mechanism"
    semantic_goal = (
        directed_scene.semantic_goal
        if directed_scene is not None
        else f"Advance the approved {role} beat through one observable state change."
    )
    visual_relation_to_beat = (
        directed_scene.visual_relation_to_beat
        if directed_scene is not None
        else "The physical action directly illustrates the approved beat."
    )
    standalone = f"{role}: {semantic_goal}"[:180]
    overlay = semantic_goal[:72]
    continuity = (
        f"continuity_id={continuity_id}",
        f"story_spine={story_spine}",
        f"continuity_anchor={continuity_anchor}",
        f"same canonical Naz face, age and wardrobe across the pack: {CANONICAL_NAZ_WARDROBE}",
        "Deep Black, Electric Blue, Cobalt, Ultraviolet and Ice Silver only",
        "optical glass, titanium, aluminium, carbon and technical ceramic",
        "no cheap cyberpunk, random robots, random people, elderly people or stock imagery",
    )
    clean_prompt, keyframe_prompt, provider_prompt = _compile_scene_prompts(
        role=role,
        visual_mode=plan.visual_mode,
        visual_relation=plan.visual_relation,
        semantic_goal=semantic_goal,
        visual_relation_to_beat=visual_relation_to_beat,
        subject_kind=subject_kind,
        subject=subject,
        setting=setting,
        action=action,
        start_state=start_state,
        end_state=end_state,
        shot_size=shot_size,
        camera_motion=camera_motion,
        continuity_anchor=continuity_anchor,
        requires_reference=requires_reference,
    )
    return ScenePlan(
        scene_id=f"{index + 1:02d}_{role}", role=role, standalone_meaning=standalone,
        beat_id=(directed_scene.beat_id if directed_scene is not None else f"beat-{index + 1:02d}-{role}"),
        semantic_goal=semantic_goal,
        source_fact_refs=(
            directed_scene.source_fact_refs if directed_scene is not None else (f"fact-{index + 1}",)
        ),
        relation_to_previous=(
            directed_scene.relation_to_previous if directed_scene is not None else "opening" if index == 0 else "template"
        ),
        expected_viewer_understanding=(
            directed_scene.expected_viewer_understanding if directed_scene is not None else semantic_goal
        ),
        visualization_kind=(
            directed_scene.visualization_kind if directed_scene is not None else "literal"
        ),
        visual_relation_to_beat=visual_relation_to_beat,
        subject_kind=subject_kind,
        motion_class=motion_class,
        concrete_action=action,
        subject=subject, setting=setting,
        start_state=start_state,
        end_state=end_state,
        shot_size=shot_size,
        camera_motion=camera_motion,
        admin_summary_ru=admin_summary_ru,
        duration_seconds=duration, clean_prompt=clean_prompt, provider_prompt=provider_prompt,
        keyframe_prompt=keyframe_prompt,
        identity_reference_usage="identity_only" if requires_reference else "none",
        story_overlay=overlay,
        text_safe_zone=SAFE_ZONES[_rank(plan.plan_id, f"safe:{index}") % len(SAFE_ZONES)],
        music_cue=f"cue {index + 1}: follow action change; tags={','.join(plan.track_tags)}",
        source_fact_ref=(
            f"{plan.source_ref}#{directed_scene.source_fact_refs[0]}"
            if directed_scene is not None
            else f"{plan.source_ref}#fact-{index + 1}"
        ),
        footage_type="generative",
        continuity_constraints=continuity, secret_warning=False, copyright_warning=False,
        suggested_interactive_sticker="question" if role in {"hypothesis", "test"} else "none",
        requires_naz_reference=requires_reference, reference_role=reference_role,
    )


def _reel_edit(plan: EditorialPlan, scenes: tuple[ScenePlan, ...], *, short: bool) -> ReelEditPlan:
    # The edit must preserve the director's cause-and-effect order. When a pack
    # has fewer than seven scenes, adjacent reframed fragments may repeat a
    # scene, but the timeline never jumps backwards or restarts after the proof.
    shot_count = 7
    quotient, remainder = divmod(shot_count, len(scenes))
    order = [
        scene_index
        for scene_index in range(len(scenes))
        for _ in range(quotient + (1 if scene_index < remainder else 0))
    ]
    shots: list[dict[str, Any]] = []
    for position, scene_index in enumerate(order):
        scene = scenes[scene_index]
        length = 1.8 if short else 2.0
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
        (
            f"{base_plan_id}|{STORY_SCHEMA}|{DIRECTOR_VERSION}|"
            f"{MOTION_CONTRACT_VERSION}|story-variant|{variant_index}"
        ).encode("utf-8")
    ).hexdigest()[:24]


def plan_story_pack(
    plan: EditorialPlan,
    safe_facts: tuple[str, ...],
    *,
    variant_index: int = 0,
    director_treatment: DirectorTreatment | None = None,
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
    if director_treatment is not None:
        if director_treatment.version != DIRECTOR_VERSION or len(director_treatment.scenes) != count:
            raise StoryPlanError("director_treatment_invalid")
        semantic_errors = semantic_preflight_reason_codes(
            director_treatment,
            facts,
            plan=plan,
            variant_index=variant_index,
        )
        if semantic_errors:
            raise DirectorValidationError(semantic_errors)
        if any(
            scene.subject_kind not in DIRECTOR_SUBJECT_KINDS
            or (scene.subject_kind == "naz_human" and scene.subject != CANONICAL_NAZ_SUBJECT)
            or (scene.subject_kind == "physical_object" and _mentions_human_subject(scene.subject))
            for scene in director_treatment.scenes
        ):
            raise StoryPlanError("director_treatment_identity_invalid")
        visual_concept = _validated_director_text(
            director_treatment.visual_concept,
            "visual_concept",
        )
        story_spine = _validated_director_text(
            director_treatment.story_spine,
            "story_spine",
            maximum=180,
        )
        central_thesis = _validated_director_text(
            director_treatment.core_thesis,
            "core_thesis",
            maximum=300,
        )
        thesis_source_fact_refs = tuple(director_treatment.thesis_source_fact_refs)
        viewer_problem = _validated_director_text(
            director_treatment.viewer_problem,
            "viewer_problem",
            maximum=240,
        )
        hook = _validated_director_text(
            director_treatment.hook,
            "hook",
            maximum=240,
        )
        payoff = _validated_director_text(
            director_treatment.payoff,
            "payoff",
            maximum=240,
        )
        story_arc = director_treatment.story_arc
        if story_arc not in _story_arc_names_for_plan(plan):
            raise StoryPlanError("director_story_arc_invalid")
        arc = DIRECTOR_STORY_ARCS[story_arc]
        arc_steps = _story_arc_steps(story_arc, count)
        continuity_anchor = _validated_director_text(
            director_treatment.continuity_anchor,
            "continuity_anchor",
            maximum=90,
        )
        initial_state_code = director_treatment.initial_state_code
        goal_state_code = director_treatment.goal_state_code
        if (
            initial_state_code not in DIRECTOR_STATE_CODES
            or goal_state_code not in DIRECTOR_STATE_CODES
            or initial_state_code == goal_state_code
        ):
            raise StoryPlanError("director_state_contract_invalid")
        expected_setting = DIRECTOR_PRIMARY_SETTINGS.get(str(arc.get("setting", "")), "")
        expected_anchor = str(arc.get("continuity_anchor", ""))
        expected_initial = str(arc.get("initial_state", ""))
        expected_goal = arc_steps[-1][1]
        if (
            continuity_anchor != expected_anchor
            or initial_state_code != expected_initial
            or goal_state_code != expected_goal
        ):
            raise StoryPlanError("director_story_arc_invalid")
        primary_setting = _validated_director_text(
            director_treatment.primary_setting,
            "primary_setting",
            maximum=180,
        )
        if primary_setting != expected_setting:
            raise StoryPlanError("director_story_arc_invalid")
        previous_state_code = initial_state_code
        for scene, (recipe_name, end_state_code) in zip(
            director_treatment.scenes, arc_steps
        ):
            recipe = DIRECTOR_ACTION_RECIPES[recipe_name]
            expected_subject_kind, expected_motion, expected_primary, _ = recipe
            expected_subject = (
                CANONICAL_NAZ_SUBJECT
                if expected_subject_kind == "naz_human"
                else _branded_primary_phrase(expected_primary, "none")
            )
            if (
                scene.setting != expected_setting
                or scene.subject_kind != expected_subject_kind
                or scene.subject != expected_subject
                or scene.motion_class != expected_motion
                or scene.concrete_action
                != _build_atomic_action(
                    action_recipe=recipe_name, brand_marking="none"
                )
                or scene.start_state
                != _bounded_state_phrase(continuity_anchor, previous_state_code)
                or scene.end_state
                != _bounded_state_phrase(continuity_anchor, end_state_code)
                or scene.admin_summary_ru
                != DIRECTOR_RECIPE_SUMMARIES_RU[recipe_name]
            ):
                raise StoryPlanError("director_story_arc_invalid")
            previous_state_code = end_state_code
        admin_concept_ru = _validated_director_ru_text(
            director_treatment.admin_concept_ru,
            "admin_concept_ru",
        )
        if admin_concept_ru != str(arc.get("description_ru", "")):
            raise StoryPlanError("director_story_arc_invalid")
        director_version = DIRECTOR_VERSION
    else:
        visual_concept = str(VISUAL_TREATMENTS[treatment_key]["label"])
        story_spine = visual_concept
        story_arc = ""
        continuity_anchor = "one evolving physical Naz AI Lab mechanism"
        initial_state_code = ""
        goal_state_code = ""
        admin_concept_ru = ""
        director_version = TEMPLATE_DIRECTOR_VERSION
        central_thesis = plan.thesis_direction
        thesis_source_fact_refs = ()
        viewer_problem = ""
        hook = plan.hook
        payoff = plan.ending
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
            directed_scene=director_treatment.scenes[i] if director_treatment is not None else None,
            directed_concept=visual_concept,
            directed_story_spine=story_spine,
            directed_continuity_anchor=continuity_anchor,
        )
        for i, role in enumerate(roles)
    )
    pack = StoryPackPlan(
        plan_id=story_plan_id, base_plan_id=plan.plan_id,
        variant_index=variant_index, continuity_id=continuity_id, persona=plan.persona,
        destination=plan.platform, scheduled_slot=plan.slot, rubric=plan.rubric,
        source_type=plan.source_type, source_ref=plan.source_ref, safe_facts=facts,
        editorial_plan=plan.to_dict(),
        central_thesis=central_thesis,
        thesis_source_fact_refs=thesis_source_fact_refs,
        viewer_problem=viewer_problem,
        hook=hook,
        payoff=payoff,
        visual_concept=visual_concept,
        story_spine=story_spine,
        story_arc=story_arc,
        continuity_anchor=continuity_anchor,
        initial_state_code=initial_state_code,
        goal_state_code=goal_state_code,
        admin_concept_ru=admin_concept_ru,
        director_version=director_version,
        scene_count=count, scenes=scenes,
        reel_edits=(
            _reel_edit(variant_plan, scenes, short=False),
            _reel_edit(variant_plan, scenes, short=True),
        ),
        caption_plan={"main": f"{hook} — {central_thesis}", "short": f"{payoff}: {plan.topic}"},
        music_plan={"tags": list(plan.track_tags), "allowlist_required": True,
                    "consume_publication_rotation": False, "selected_track": None},
        safety_flags=("safe_source_refs_only", "no_private_content", "no_drawn_interactive_ui"),
        copyright_flags=("source_facts_only", "no_unlicensed_footage_selected"),
        policy_versions={
            "orchestrator": plan.orchestrator_version, "content": plan.content_policy_version,
            "visual": plan.visual_policy_version, "music": plan.music_policy_version,
            "semantic": SEMANTIC_CONTRACT_VERSION,
        },
        renderer=renderer_status(),
    )
    validate_story_pack(pack)
    return pack


def validate_story_pack(pack: StoryPackPlan) -> None:
    if not 4 <= pack.scene_count <= 7 or pack.scene_count != len(pack.scenes):
        raise StoryPlanError("scene count must be 4..7")
    if pack.director_version not in {DIRECTOR_VERSION, TEMPLATE_DIRECTOR_VERSION}:
        raise StoryPlanError("unknown director version")
    if pack.renderer not in {"available", RENDERER_UNAVAILABLE}:
        raise StoryPlanError("unknown renderer status")
    previous_end_state = ""
    bounded_end_state_codes: list[str] = []
    arc_steps: tuple[tuple[str, str], ...] = ()
    arc_setting = ""
    if pack.director_version == DIRECTOR_VERSION:
        if pack.story_arc not in DIRECTOR_STORY_ARCS:
            raise StoryPlanError("director story arc is invalid")
        arc = DIRECTOR_STORY_ARCS[pack.story_arc]
        arc_steps = _story_arc_steps(pack.story_arc, pack.scene_count)
        arc_setting = DIRECTOR_PRIMARY_SETTINGS.get(str(arc.get("setting", "")), "")
        if (
            pack.initial_state_code not in DIRECTOR_STATE_CODES
            or pack.goal_state_code not in DIRECTOR_STATE_CODES
            or pack.initial_state_code == pack.goal_state_code
            or pack.continuity_anchor != str(arc.get("continuity_anchor", ""))
            or pack.initial_state_code != str(arc.get("initial_state", ""))
            or pack.goal_state_code != arc_steps[-1][1]
            or not arc_setting
            or pack.admin_concept_ru != str(arc.get("description_ru", ""))
        ):
            raise StoryPlanError("director state contract is invalid")
        previous_end_state = _bounded_state_phrase(
            pack.continuity_anchor, pack.initial_state_code
        )
        try:
            editorial_plan = EditorialPlan.from_dict(pack.editorial_plan)
        except (TypeError, ValueError, KeyError) as exc:
            raise StoryPlanError("editorial_plan_contract_invalid") from exc
        persisted_treatment = DirectorTreatment(
            plan_id=pack.plan_id,
            variant_index=pack.variant_index,
            visual_concept=pack.visual_concept,
            core_thesis=pack.central_thesis,
            thesis_source_fact_refs=tuple(pack.thesis_source_fact_refs),
            viewer_problem=pack.viewer_problem,
            hook=pack.hook,
            payoff=pack.payoff,
            story_spine=pack.story_spine,
            story_arc=pack.story_arc,
            continuity_anchor=pack.continuity_anchor,
            initial_state_code=pack.initial_state_code,
            goal_state_code=pack.goal_state_code,
            primary_setting=arc_setting,
            admin_concept_ru=pack.admin_concept_ru,
            scenes=tuple(
                DirectorScene(
                    role=scene.role,
                    beat_id=scene.beat_id,
                    semantic_goal=scene.semantic_goal,
                    source_fact_refs=tuple(scene.source_fact_refs),
                    relation_to_previous=scene.relation_to_previous,
                    expected_viewer_understanding=scene.expected_viewer_understanding,
                    visualization_kind=scene.visualization_kind,
                    visual_relation_to_beat=scene.visual_relation_to_beat,
                    setting=scene.setting,
                    subject_kind=scene.subject_kind,
                    subject=scene.subject,
                    motion_class=str(scene.motion_class or ""),
                    concrete_action=scene.concrete_action,
                    start_state=scene.start_state,
                    end_state=scene.end_state,
                    shot_size=scene.shot_size,
                    camera_motion=scene.camera_motion,
                    admin_summary_ru=scene.admin_summary_ru,
                )
                for scene in pack.scenes
            ),
            version=pack.director_version,
        )
        semantic_errors = semantic_preflight_reason_codes(
            persisted_treatment,
            pack.safe_facts,
            plan=editorial_plan,
            variant_index=pack.variant_index,
        )
        if semantic_errors:
            raise DirectorValidationError(semantic_errors)
        if pack.policy_versions.get("semantic") != SEMANTIC_CONTRACT_VERSION:
            raise StoryPlanError("semantic_contract_version_invalid")
    elif pack.story_arc or pack.initial_state_code or pack.goal_state_code:
        raise StoryPlanError("template scene cannot claim semantic state contract")
    for scene_index, scene in enumerate(pack.scenes):
        if scene.subject_kind not in DIRECTOR_SUBJECT_KINDS:
            raise StoryPlanError("scene subject kind is invalid")
        if (
            (scene.subject_kind == "naz_human") is not scene.requires_naz_reference
            or (scene.subject_kind == "naz_human" and scene.subject != CANONICAL_NAZ_SUBJECT)
            or (scene.subject_kind == "physical_object" and _mentions_human_subject(scene.subject))
        ):
            raise StoryPlanError("scene subject identity contract is invalid")
        if pack.director_version == DIRECTOR_VERSION:
            recipe_name, expected_end_state_code = arc_steps[scene_index]
            recipe = DIRECTOR_ACTION_RECIPES[recipe_name]
            expected_subject_kind, expected_motion, expected_primary, _ = recipe
            expected_subject = (
                CANONICAL_NAZ_SUBJECT
                if expected_subject_kind == "naz_human"
                else _branded_primary_phrase(expected_primary, "none")
            )
            if (
                scene.setting != arc_setting
                or scene.subject_kind != expected_subject_kind
                or scene.subject != expected_subject
                or scene.motion_class != expected_motion
                or scene.concrete_action
                != _build_atomic_action(
                    action_recipe=recipe_name, brand_marking="none"
                )
                or scene.admin_summary_ru
                != DIRECTOR_RECIPE_SUMMARIES_RU[recipe_name]
            ):
                raise StoryPlanError("scene story arc contract is invalid")
            end_state_code = next(
                (
                    code
                    for code in DIRECTOR_STATE_CODES
                    if scene.end_state
                    == _bounded_state_phrase(pack.continuity_anchor, code)
                ),
                "",
            )
            if scene.start_state != previous_end_state or not end_state_code:
                raise StoryPlanError("scene bounded state contract is invalid")
            if end_state_code != expected_end_state_code:
                raise StoryPlanError("scene story arc state is invalid")
            if (
                end_state_code == pack.goal_state_code
                and scene_index != len(pack.scenes) - 1
            ):
                raise StoryPlanError("scene reaches goal before final proof")
            bounded_end_state_codes.append(end_state_code)
            if scene.motion_class is None or _motion_contract_reason_codes(
                subject_kind=scene.subject_kind,
                motion_class=scene.motion_class,
                action=scene.concrete_action,
                start_state=scene.start_state,
                end_state=scene.end_state,
            ):
                raise StoryPlanError("scene motion contract is invalid")
            previous_end_state = scene.end_state
        elif scene.motion_class is not None:
            raise StoryPlanError("template scene cannot claim semantic motion contract")
        if scene.duration_seconds != 5:
            raise StoryPlanError("Story duration must be exactly 5 seconds")
        if scene.secret_warning or _SECRET_RE.search(scene.clean_prompt):
            raise StoryPlanError("secret warning in scene")
        if "9:16" not in scene.clean_prompt or "Real motion" not in scene.clean_prompt:
            raise StoryPlanError("CLEAN master contract is incomplete")
        if (
            not validate_provider_prompt(scene.provider_prompt)
            or fact_text_in_provider_prompt(scene)
            or _raw_fact_in_render_prompts(scene, pack.safe_facts)
        ):
            raise StoryPlanError("provider prompt is not privacy-minimized")
        if not validate_provider_prompt(scene.keyframe_prompt):
            raise StoryPlanError("keyframe prompt is unsafe")
        expected_prompts = _compile_scene_prompts(
            role=scene.role,
            visual_mode=str(pack.editorial_plan.get("visual_mode", "")),
            visual_relation=str(pack.editorial_plan.get("visual_relation", "")),
            semantic_goal=scene.semantic_goal,
            visual_relation_to_beat=scene.visual_relation_to_beat,
            subject_kind=scene.subject_kind,
            subject=scene.subject,
            setting=scene.setting,
            action=scene.concrete_action,
            start_state=scene.start_state,
            end_state=scene.end_state,
            shot_size=scene.shot_size,
            camera_motion=scene.camera_motion,
            continuity_anchor=pack.continuity_anchor,
            requires_reference=scene.requires_naz_reference,
        )
        if (
            scene.clean_prompt,
            scene.keyframe_prompt,
            scene.provider_prompt,
        ) != expected_prompts:
            raise StoryPlanError("scene_prompt_not_compiled_from_approved_brief")
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
    if pack.director_version == DIRECTOR_VERSION and (
        len(set(bounded_end_state_codes)) != len(pack.scenes)
        or not bounded_end_state_codes
        or bounded_end_state_codes[-1] != pack.goal_state_code
    ):
        raise StoryPlanError("director proof state contract is invalid")
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
        actual = [str(item["source_scene_id"]) for item in edit.shots]
        scene_order = {scene.scene_id: index for index, scene in enumerate(pack.scenes)}
        positions = [scene_order[scene_id] for scene_id in actual]
        if (
            positions[0] != 0
            or positions[-1] != len(pack.scenes) - 1
            or set(positions) != set(range(len(pack.scenes)))
            or any(current > following for current, following in zip(positions, positions[1:]))
        ):
            raise StoryPlanError("Reel must preserve causal scene order")
        if not any(bool(item.get("crop_change_required")) and item.get("reel_crop") for item in edit.shots):
            raise StoryPlanError("Reel requires a real crop change")
        if any(not str(item.get("source", "")).endswith("_clean.mp4") for item in edit.shots):
            raise StoryPlanError("Reel may use CLEAN masters only")


def fact_text_in_provider_prompt(scene: ScenePlan) -> bool:
    """Reject transport references; approved semantic goals may reach Runway."""
    source_ref = " ".join(scene.source_fact_ref.split()).casefold()
    prompt = scene.provider_prompt.casefold()
    return bool(
        (source_ref and source_ref in prompt)
        or _DIRECTOR_TRANSPORT_RE.search(scene.provider_prompt)
    )


def _raw_fact_in_render_prompts(scene: ScenePlan, safe_facts: Sequence[str]) -> bool:
    return any(
        _raw_source_fragment_in_text(prompt, safe_facts)
        for prompt in (
            scene.clean_prompt,
            scene.keyframe_prompt,
            scene.provider_prompt,
        )
    )


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
    if payload.get("schema") not in SUPPORTED_STORY_SCHEMAS:
        raise StoryPlanError("unsupported story manifest schema")
    return payload


def update_manifest(path: Path, mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    payload = read_manifest(path)
    mutator(payload)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_json(path, payload)
    return payload


_IMMUTABLE_PLAN_FIELDS = (
    "plan_id", "base_plan_id", "variant_index", "continuity_id", "persona",
    "destination", "scheduled_slot", "rubric", "source_type", "source_ref",
    "safe_facts", "editorial_plan", "central_thesis", "thesis_source_fact_refs",
    "viewer_problem", "hook", "payoff", "visual_concept",
    "story_spine", "story_arc", "continuity_anchor", "initial_state_code", "goal_state_code",
    "admin_concept_ru", "director_version",
    "scene_count", "scenes", "reel_edits", "caption_plan",
    "safety_flags", "copyright_flags", "policy_versions", "schema",
)
_IMMUTABLE_MUSIC_PLAN_FIELDS = (
    "tags", "allowlist_required", "consume_publication_rotation",
)


def _immutable_plan_fingerprint(payload: Mapping[str, Any]) -> str:
    immutable = {field: payload.get(field) for field in _IMMUTABLE_PLAN_FIELDS}
    music_plan = payload.get("music_plan")
    immutable["music_plan"] = (
        {
            field: music_plan.get(field)
            for field in _IMMUTABLE_MUSIC_PLAN_FIELDS
        }
        if isinstance(music_plan, Mapping)
        else None
    )
    canonical = json.dumps(
        immutable,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _duration_in_range(value: Any, minimum: float, maximum: float) -> bool:
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return False
    return minimum <= duration <= maximum


def manifest_has_current_production_contract(payload: Mapping[str, Any]) -> bool:
    """Return whether a semantic-v8/v7 manifest is safe for the paid worker.

    This deliberately checks the persisted production envelope, not just the
    editorial plan.  Older manifests can still be inspected or repaired by a
    dry-run, but cannot be approved or reach a provider accidentally.
    """
    if payload.get("schema") != STORY_SCHEMA:
        return False
    required_top_level_strings = (
        "plan_id", "base_plan_id", "director_version", "central_thesis",
        "viewer_problem", "hook", "payoff", "visual_concept", "story_spine",
        "story_arc", "continuity_anchor", "initial_state_code", "goal_state_code",
        "admin_concept_ru",
    )
    if any(
        not isinstance(payload.get(field), str) or not payload[field].strip()
        for field in required_top_level_strings
    ):
        return False
    if (
        not isinstance(payload.get("variant_index"), int)
        or isinstance(payload.get("variant_index"), bool)
        or not isinstance(payload.get("thesis_source_fact_refs"), list)
        or not payload["thesis_source_fact_refs"]
        or not all(
            isinstance(ref, str) and ref.strip()
            for ref in payload["thesis_source_fact_refs"]
        )
    ):
        return False
    fingerprint = payload.get("immutable_plan_fingerprint")
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        return False
    try:
        if fingerprint != _immutable_plan_fingerprint(payload):
            return False
    except (TypeError, ValueError):
        return False
    scenes = payload.get("scenes")
    edits = payload.get("reel_edits")
    scene_jobs = payload.get("scene_jobs")
    reel_jobs = payload.get("reel_jobs")
    model_policy = payload.get("model_policy")
    visual_strategy = payload.get("visual_strategy")
    if not all(isinstance(value, list) for value in (scenes, edits, scene_jobs, reel_jobs)):
        return False
    director_version = payload["director_version"]
    if director_version != DIRECTOR_VERSION:
        return False
    concept = payload["visual_concept"]
    story_arc = payload["story_arc"]
    continuity_anchor = payload["continuity_anchor"]
    initial_state_code = payload["initial_state_code"]
    goal_state_code = payload["goal_state_code"]
    if director_version == DIRECTOR_VERSION:
        if story_arc not in DIRECTOR_STORY_ARCS:
            return False
        try:
            arc_steps = _story_arc_steps(story_arc, len(scenes))
        except StoryPlanError:
            return False
        arc = DIRECTOR_STORY_ARCS[story_arc]
        arc_setting = DIRECTOR_PRIMARY_SETTINGS.get(str(arc.get("setting", "")), "")
        try:
            _validated_director_text(concept, "visual_concept")
            _validated_director_text(
                continuity_anchor, "continuity_anchor", maximum=90
            )
        except StoryPlanError:
            return False
        if (
            initial_state_code not in DIRECTOR_STATE_CODES
            or goal_state_code not in DIRECTOR_STATE_CODES
            or initial_state_code == goal_state_code
            or continuity_anchor != str(arc.get("continuity_anchor", ""))
            or initial_state_code != str(arc.get("initial_state", ""))
            or goal_state_code != arc_steps[-1][1]
            or not arc_setting
            or str(payload.get("admin_concept_ru", ""))
            != str(arc.get("description_ru", ""))
        ):
            return False
    if not 4 <= len(scenes) <= 7 or payload.get("scene_count") != len(scenes) or not edits:
        return False
    if not isinstance(model_policy, dict) or (
        model_policy.get("primary_tier") != "primary"
        or model_policy.get("secondary_tier") != "secondary"
        or model_policy.get("automatic_fallback") is not False
    ):
        return False
    hybrid_policy = model_policy.get("scene_route_policy") == HYBRID_MODEL_ROUTE
    if not hybrid_policy:
        return False
    if not isinstance(visual_strategy, dict) or (
        visual_strategy.get("planner") != "reels-maker-directed-scenes-v1"
        or visual_strategy.get("keyframe_required") is not True
        or visual_strategy.get("avatar_usage") != "identity_reference_only"
        or visual_strategy.get("direct_avatar_as_video_first_frame") is not False
        or visual_strategy.get("motion_contract_version") != MOTION_CONTRACT_VERSION
    ):
        return False
    if hybrid_policy and visual_strategy.get("motion_prompt_version") != VIDEO_MOTION_PROMPT_VERSION:
        return False
    scene_ids = [str(item.get("scene_id", "")) for item in scenes if isinstance(item, dict)]
    if len(scene_ids) != len(scenes) or not all(scene_ids) or len(set(scene_ids)) != len(scene_ids):
        return False
    scenes_by_id = {str(item["scene_id"]): item for item in scenes}
    previous_end_state = (
        _bounded_state_phrase(continuity_anchor, initial_state_code)
        if director_version == DIRECTOR_VERSION
        else ""
    )
    bounded_end_state_codes: list[str] = []
    for scene_index, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            return False
        required_scene_strings = (
            "scene_id", "role", "beat_id", "semantic_goal",
            "relation_to_previous", "expected_viewer_understanding",
            "visualization_kind", "visual_relation_to_beat", "setting",
            "subject_kind", "subject", "motion_class", "concrete_action",
            "start_state", "end_state", "shot_size", "camera_motion",
            "admin_summary_ru", "clean_prompt", "keyframe_prompt",
            "provider_prompt", "source_fact_ref",
        )
        if any(
            not isinstance(scene.get(field), str) or not scene[field].strip()
            for field in required_scene_strings
        ):
            return False
        requires_reference = scene.get("requires_naz_reference")
        role = str(scene.get("reference_role", ""))
        shot_size = str(scene.get("shot_size", ""))
        subject_kind = str(scene.get("subject_kind", ""))
        subject = str(scene.get("subject", ""))
        action = str(scene.get("concrete_action", ""))
        start_state = str(scene.get("start_state", ""))
        end_state = str(scene.get("end_state", ""))
        if (
            shot_size not in SHOT_SIZES
            or not _duration_in_range(scene.get("duration_seconds"), 5, 5)
            or not isinstance(requires_reference, bool)
            or role not in REFERENCE_ROLES
            or subject_kind not in DIRECTOR_SUBJECT_KINDS
            or not action
            or not start_state
            or not end_state
            or not str(scene.get("keyframe_prompt", ""))
            or scene.get("identity_reference_usage") not in {"identity_only", "none"}
        ):
            return False
        source_fact_refs = scene.get("source_fact_refs")
        if (
            not isinstance(source_fact_refs, list)
            or not source_fact_refs
            or not all(isinstance(ref, str) and ref.strip() for ref in source_fact_refs)
            or scene.get("visualization_kind") not in VISUALIZATION_KINDS
        ):
            return False
        if (
            requires_reference and role not in {"frontal_identity", "three_quarter_identity"}
        ) or (not requires_reference and role != "none"):
            return False
        if director_version == DIRECTOR_VERSION:
            recipe_name, expected_end_state_code = arc_steps[scene_index]
            recipe = DIRECTOR_ACTION_RECIPES[recipe_name]
            expected_subject_kind, expected_motion, expected_primary, _ = recipe
            expected_subject = (
                CANONICAL_NAZ_SUBJECT
                if expected_subject_kind == "naz_human"
                else _branded_primary_phrase(expected_primary, "none")
            )
            end_state_code = next(
                (
                    code
                    for code in DIRECTOR_STATE_CODES
                    if end_state == _bounded_state_phrase(continuity_anchor, code)
                ),
                "",
            )
            if (
                (subject_kind == "naz_human") is not requires_reference
                or (subject_kind == "naz_human" and subject != CANONICAL_NAZ_SUBJECT)
                or (subject_kind == "physical_object" and _mentions_human_subject(subject))
                or (
                    requires_reference
                    is not (scene.get("identity_reference_usage") == "identity_only")
                )
            ):
                return False
            if (
                str(scene.get("setting", "")) != arc_setting
                or subject_kind != expected_subject_kind
                or subject != expected_subject
                or scene.get("motion_class") != expected_motion
                or action
                != _build_atomic_action(
                    action_recipe=recipe_name, brand_marking="none"
                )
                or end_state_code != expected_end_state_code
                or str(scene.get("admin_summary_ru", ""))
                != DIRECTOR_RECIPE_SUMMARIES_RU[recipe_name]
            ):
                return False
            if (
                start_state != previous_end_state
                or not end_state_code
                or (
                    end_state_code == goal_state_code
                    and scene_index != len(scenes) - 1
                )
            ):
                return False
            motion_class = scene.get("motion_class")
            if not isinstance(motion_class, str) or _motion_contract_reason_codes(
                subject_kind=subject_kind,
                motion_class=motion_class,
                action=action,
                start_state=start_state,
                end_state=end_state,
            ):
                return False
            bounded_end_state_codes.append(end_state_code)
            previous_end_state = end_state
        elif scene.get("motion_class") is not None:
            return False
    if director_version == DIRECTOR_VERSION and (
        len(set(bounded_end_state_codes)) != len(scenes)
        or not bounded_end_state_codes
        or bounded_end_state_codes[-1] != goal_state_code
    ):
        return False
    editorial_payload = payload.get("editorial_plan")
    policy_versions = payload.get("policy_versions")
    safe_facts = payload.get("safe_facts")
    if (
        not isinstance(editorial_payload, dict)
        or not isinstance(policy_versions, dict)
        or policy_versions.get("semantic") != SEMANTIC_CONTRACT_VERSION
        or not isinstance(safe_facts, list)
        or not all(isinstance(fact, str) and fact.strip() for fact in safe_facts)
    ):
        return False
    try:
        editorial_plan = EditorialPlan.from_dict(editorial_payload)
        persisted_treatment = DirectorTreatment(
            plan_id=str(payload.get("plan_id", "")),
            variant_index=int(payload.get("variant_index", -1)),
            visual_concept=concept,
            core_thesis=payload["central_thesis"],
            thesis_source_fact_refs=tuple(payload.get("thesis_source_fact_refs", ())),
            viewer_problem=payload["viewer_problem"],
            hook=payload["hook"],
            payoff=payload["payoff"],
            story_spine=payload["story_spine"],
            story_arc=story_arc,
            continuity_anchor=continuity_anchor,
            initial_state_code=initial_state_code,
            goal_state_code=goal_state_code,
            primary_setting=arc_setting,
            admin_concept_ru=payload["admin_concept_ru"],
            scenes=tuple(
                DirectorScene(
                    role=scene["role"],
                    beat_id=scene["beat_id"],
                    semantic_goal=scene["semantic_goal"],
                    source_fact_refs=tuple(scene.get("source_fact_refs", ())),
                    relation_to_previous=scene["relation_to_previous"],
                    expected_viewer_understanding=scene["expected_viewer_understanding"],
                    visualization_kind=scene["visualization_kind"],
                    visual_relation_to_beat=scene["visual_relation_to_beat"],
                    setting=scene["setting"],
                    subject_kind=scene["subject_kind"],
                    subject=scene["subject"],
                    motion_class=scene["motion_class"],
                    concrete_action=scene["concrete_action"],
                    start_state=scene["start_state"],
                    end_state=scene["end_state"],
                    shot_size=scene["shot_size"],
                    camera_motion=scene["camera_motion"],
                    admin_summary_ru=scene["admin_summary_ru"],
                )
                for scene in scenes
            ),
            version=director_version,
        )
        if semantic_preflight_reason_codes(
            persisted_treatment,
            tuple(str(item) for item in safe_facts),
            plan=editorial_plan,
            variant_index=int(payload.get("variant_index", 0)),
        ):
            return False
        for scene in scenes:
            expected_prompts = _compile_scene_prompts(
                role=str(scene.get("role", "")),
                visual_mode=str(editorial_payload.get("visual_mode", "")),
                visual_relation=str(editorial_payload.get("visual_relation", "")),
                semantic_goal=str(scene.get("semantic_goal", "")),
                visual_relation_to_beat=str(scene.get("visual_relation_to_beat", "")),
                subject_kind=str(scene.get("subject_kind", "")),
                subject=str(scene.get("subject", "")),
                setting=str(scene.get("setting", "")),
                action=str(scene.get("concrete_action", "")),
                start_state=str(scene.get("start_state", "")),
                end_state=str(scene.get("end_state", "")),
                shot_size=str(scene.get("shot_size", "")),
                camera_motion=str(scene.get("camera_motion", "")),
                continuity_anchor=continuity_anchor,
                requires_reference=bool(scene.get("requires_naz_reference")),
            )
            if (
                str(scene.get("clean_prompt", "")),
                str(scene.get("keyframe_prompt", "")),
                str(scene.get("provider_prompt", "")),
            ) != expected_prompts:
                return False
            prompt_text = "\n".join(expected_prompts).casefold()
            if any(
                len(normalized) >= 24 and normalized in prompt_text
                for fact in safe_facts
                if (normalized := " ".join(str(fact).split()).casefold())
            ):
                return False
    except (DirectorValidationError, StoryPlanError, TypeError, ValueError):
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
        if hybrid_policy and (
            route.get("scene_strategy") != HYBRID_MODEL_ROUTE
            or route.get("selected_model")
            != ("gen4.5" if scene["requires_naz_reference"] else "gen4_turbo")
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
        positions = [
            scene_ids.index(str(shot["source_scene_id"]))
            for shot in shots
        ]
        if (
            positions[0] != 0
            or positions[-1] != len(scene_ids) - 1
            or set(positions) != set(range(len(scene_ids)))
            or any(
                current > following
                for current, following in zip(positions, positions[1:])
            )
        ):
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
    immutable_plan_fingerprint = _immutable_plan_fingerprint(payload)
    payload.update({
        "created_at": now, "updated_at": now, "pack_status": "awaiting_approval",
        "immutable_plan_fingerprint": immutable_plan_fingerprint,
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
            "scene_route_policy": HYBRID_MODEL_ROUTE,
        },
        "visual_strategy": {
            "planner": "reels-maker-directed-scenes-v1",
            "keyframe_required": True,
            "avatar_usage": "identity_reference_only",
            "direct_avatar_as_video_first_frame": False,
            "keyframe_provider": "runway",
            "motion_contract_version": MOTION_CONTRACT_VERSION,
            "motion_prompt_version": VIDEO_MOTION_PROMPT_VERSION,
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
            "scene_strategy": HYBRID_MODEL_ROUTE,
            "selected_model": "gen4.5" if scene.requires_naz_reference else "gen4_turbo",
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
    if pack.director_version != DIRECTOR_VERSION:
        raise StoryPlanError("template_treatment_production_forbidden")
    root = Path(storage_root).expanduser().resolve()
    pack_dir = (root / pack.plan_id).resolve()
    if root not in pack_dir.parents:
        raise StoryPlanError("unsafe pack path")
    # Validate the exact JSON shape that will be persisted: dataclasses.asdict
    # keeps tuples in memory, while the manifest contract intentionally accepts
    # only JSON arrays.
    payload = json.loads(json.dumps(_production_payload(pack), ensure_ascii=False))
    if not manifest_has_current_production_contract(payload):
        raise StoryPlanError("story_manifest_contract_stale")
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
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )
    ensure_private_group_access(manifest, directory=False)
    existing = read_manifest(manifest)
    if existing.get("plan_id") != pack.plan_id:
        raise StoryPlanError("stored manifest plan_id mismatch")
    try:
        existing_fingerprint = _immutable_plan_fingerprint(existing)
    except (TypeError, ValueError) as exc:
        raise StoryPlanError("stored_manifest_contract_mismatch") from exc
    if (
        existing.get("immutable_plan_fingerprint") != payload["immutable_plan_fingerprint"]
        or existing.get("immutable_plan_fingerprint") != existing_fingerprint
    ):
        raise StoryPlanError("stored_manifest_contract_mismatch")
    if not manifest_has_current_production_contract(existing):
        raise StoryPlanError("story_manifest_contract_stale")
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
