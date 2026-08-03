"""Deterministic editorial planning shared by every scheduled Naz release path.

The module is intentionally free of Telegram, VK, SQLite and model clients.  A
route supplies eligible catalog data and published history; ``plan_release`` is
the only function allowed to make categorical editorial choices.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping, Sequence


ORCHESTRATOR_VERSION = "editorial-orchestrator-v1"
GENERATION_PACKAGE_VERSION = "generation-package-v1"
PUBLISHED_HISTORY_LIMIT = 160
_PACKAGE_FIELDS = (
    "final_text",
    "concrete_scene",
    "visual_subject",
    "visual_relation_to_thesis",
    "image_prompt_seed",
    "track_tags",
)
_FORBIDDEN_VISUALS = (
    "random person",
    "random people",
    "generic person",
    "generic people",
    "stock photo",
    "stock scene",
    "elderly person",
    "old woman",
    "old man",
    "grandmother",
    "grandfather",
    "бабушка",
    "дедушка",
    "пожилой человек",
)


class EditorialPlanError(ValueError):
    """The deterministic catalog cannot produce a safe compatible plan."""


class GenerationPackageError(ValueError):
    """A model response is technically invalid and may be retried once."""


class EditorialPlanPolicy:
    """Supported immutable production policies for editorial planning."""

    AUTO = "auto"
    STANDARD_ONLY = "standard_only"
    STANDARD_ONLY_IDENTITY = "standard_only-v1"
    VALUES = frozenset({AUTO, STANDARD_ONLY})

    @classmethod
    def normalize(cls, value: object) -> str:
        normalized = str(value).strip().casefold()
        if normalized not in cls.VALUES:
            raise EditorialPlanError("unknown editorial production policy")
        return normalized

    @classmethod
    def identity_marker(cls, value: object) -> str:
        normalized = cls.normalize(value)
        return cls.AUTO if normalized == cls.AUTO else cls.STANDARD_ONLY_IDENTITY


@dataclass(frozen=True, slots=True)
class EditorialSource:
    source_ref: str
    topic: str
    source_type: str = "catalog"
    rubric_keys: tuple[str, ...] = ()
    safe_facts: tuple[str, ...] = ()
    source_verified: bool = False
    concrete_action: bool = False
    visualizable_process: bool = False
    causal_bits: int = 0
    real_result: bool = False
    contains_secrets: bool = False
    contains_private_data: bool = False


@dataclass(frozen=True, slots=True)
class EditorialRubric:
    key: str
    name: str
    mode: str
    purpose: str
    constraints: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EditorialContext:
    persona: str
    platform: str
    slot: str
    seed: str
    sources: tuple[EditorialSource, ...]
    rubrics: tuple[EditorialRubric, ...]
    pools: Mapping[str, tuple[str, ...]]
    # Diversity depth belongs to the complete persona catalog, even when a
    # route exposes only the subset compatible with the current slot.
    persona_pool_sizes: Mapping[str, int] = field(default_factory=dict)
    semantic_cards: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    published_history: tuple[Mapping[str, Any], ...] = ()
    preferred: Mapping[str, str] = field(default_factory=dict)
    policy_versions: Mapping[str, str] = field(default_factory=dict)
    crosspost_plan_id: str = ""
    production_policy: str = EditorialPlanPolicy.AUTO
    source_metadata: Mapping[str, Mapping[str, str]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EditorialPlan:
    plan_id: str
    persona: str
    platform: str
    slot: str
    rubric: str
    mode: str
    source_type: str
    source_ref: str
    topic: str
    purpose: str
    content_format: str
    production_mode: str
    thesis_direction: str
    epistemic_state: str
    tension: str
    semantic_theme: str
    semantic_card: str
    facet: str
    author_role: str
    emotional_arc: str
    reader_relation: str
    structure: str
    hook: str
    ending: str
    energy: str
    seriousness: str
    tempo: str
    length: str
    humor: str
    imagery: str
    visual_mode: str
    visual_subject_direction: str
    visual_relation: str
    track_tags: tuple[str, ...]
    orchestrator_version: str
    content_policy_version: str
    visual_policy_version: str
    music_policy_version: str
    production_policy: str = EditorialPlanPolicy.AUTO
    source_project: str = ""
    source_date: str = ""

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["track_tags"] = list(self.track_tags)
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EditorialPlan":
        payload = dict(value)
        payload["track_tags"] = tuple(str(item) for item in payload.get("track_tags", ()))
        payload.setdefault("production_policy", EditorialPlanPolicy.AUTO)
        payload.setdefault("source_project", "")
        payload.setdefault("source_date", "")
        payload["production_policy"] = EditorialPlanPolicy.normalize(
            payload["production_policy"]
        )
        plan = cls(**payload)
        validate_plan(plan)
        return plan


@dataclass(frozen=True, slots=True)
class GenerationPackage:
    final_text: str
    concrete_scene: str
    visual_subject: str
    visual_relation_to_thesis: str
    image_prompt_seed: str
    track_tags: tuple[str, ...]


def cooldown_depth(pool_size: int) -> int:
    """About 60% of a pool; the required 17-value example yields 10."""
    if pool_size <= 0:
        return 0
    return max(1, round(pool_size * 0.60))


def _stable_rank(plan_id: str, axis: str, value: str) -> str:
    return hashlib.sha256(f"{plan_id}|{axis}|{value}".encode("utf-8")).hexdigest()


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _last_position(history: Sequence[Mapping[str, Any]], axis: str, value: str) -> int:
    for index in range(len(history) - 1, -1, -1):
        if str(history[index].get(axis, "")) == value:
            return index
    return -1


def _choose(
    *,
    plan_id: str,
    axis: str,
    values: Iterable[str],
    history: Sequence[Mapping[str, Any]],
    preferred: str = "",
    persona_pool_size: int | None = None,
) -> str:
    candidates = _unique(values)
    if not candidates:
        raise EditorialPlanError(f"empty compatible pool for {axis}")
    # Compatibility constrains what may be selected, but the diversity window
    # belongs to the complete persona-wide pool for this axis.
    pool_size = (
        persona_pool_size
        if persona_pool_size is not None and persona_pool_size > 0
        else len(candidates)
    )
    depth = cooldown_depth(pool_size)
    blocked = {
        str(item.get(axis, ""))
        for item in history[-depth:]
        if str(item.get(axis, ""))
    }
    eligible = [value for value in candidates if value not in blocked]
    if not eligible:
        # Compatibility stays hard. Only the oldest diversity cooldown is
        # relaxed, deterministically, so diversity alone never drops a slot.
        oldest = min(_last_position(history, axis, value) for value in candidates)
        eligible = [
            value
            for value in candidates
            if _last_position(history, axis, value) == oldest
        ]
    if preferred and preferred in eligible:
        return preferred
    return min(eligible, key=lambda value: _stable_rank(plan_id, axis, value))


def _plan_id(context: EditorialContext) -> str:
    production_policy = EditorialPlanPolicy.normalize(context.production_policy)
    if context.crosspost_plan_id:
        if production_policy == EditorialPlanPolicy.AUTO:
            return context.crosspost_plan_id
        identity = (
            f"{context.crosspost_plan_id}|production-policy:"
            f"{EditorialPlanPolicy.identity_marker(production_policy)}"
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    identity = "|".join(
        (
            ORCHESTRATOR_VERSION,
            context.persona,
            context.platform,
            context.slot,
            context.seed,
        )
    )
    if production_policy != EditorialPlanPolicy.AUTO:
        identity += (
            "|production-policy:"
            + EditorialPlanPolicy.identity_marker(production_policy)
        )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _axis_values(
    context: EditorialContext,
    rubric: EditorialRubric,
    axis: str,
) -> tuple[str, ...]:
    constrained = tuple(rubric.constraints.get(axis, ()))
    return constrained or tuple(context.pools.get(axis, ()))


def _persona_pool_size(
    context: EditorialContext,
    axis: str,
    fallback: int,
) -> int:
    explicit = context.persona_pool_sizes.get(axis)
    if isinstance(explicit, int) and not isinstance(explicit, bool) and explicit > 0:
        return explicit
    return fallback


def story_first_eligible(source: EditorialSource) -> bool:
    """Central suitability gate; diversity never forces Story-first."""
    return bool(
        source.source_type == "work_chronicle"
        and source.source_verified
        and source.concrete_action
        and source.visualizable_process
        and source.causal_bits >= 4
        and source.real_result
        and len(source.safe_facts) >= 4
        and not source.contains_secrets
        and not source.contains_private_data
    )


def plan_release(context: EditorialContext) -> EditorialPlan:
    """Create one immutable plan from published history and eligible catalogs."""
    if context.persona not in {"naz", "void"}:
        raise EditorialPlanError("unknown persona")
    if context.platform not in {"telegram", "vk"}:
        raise EditorialPlanError("unsupported scheduled platform")
    production_policy = EditorialPlanPolicy.normalize(context.production_policy)
    history = tuple(context.published_history[-PUBLISHED_HISTORY_LIMIT:])
    plan_id = _plan_id(context)
    compatible_rubrics = [
        rubric
        for rubric in context.rubrics
        if any(not source.rubric_keys or rubric.key in source.rubric_keys for source in context.sources)
    ]
    rubric_name = _choose(
        plan_id=plan_id,
        axis="rubric",
        values=(item.name for item in compatible_rubrics),
        history=history,
        preferred=str(context.preferred.get("rubric", "")),
        persona_pool_size=_persona_pool_size(
            context,
            "rubric",
            len(_unique(item.name for item in context.rubrics)),
        ),
    )
    rubric = next(item for item in compatible_rubrics if item.name == rubric_name)
    sources = [
        source
        for source in context.sources
        if not source.rubric_keys or rubric.key in source.rubric_keys
    ]
    source_ref = _choose(
        plan_id=plan_id,
        axis="source_ref",
        values=(item.source_ref for item in sources),
        history=history,
        persona_pool_size=_persona_pool_size(
            context,
            "source_ref",
            len(_unique(item.source_ref for item in context.sources)),
        ),
    )
    source = next(item for item in sources if item.source_ref == source_ref)

    selected: dict[str, str] = {}
    story_first = (
        production_policy == EditorialPlanPolicy.AUTO
        and context.persona == "naz"
        and story_first_eligible(source)
    )
    selected["content_format"] = _choose(
        plan_id=plan_id,
        axis="content_format",
        values=("story_pack",) if story_first else ("text_post",),
        history=history,
        persona_pool_size=_persona_pool_size(context, "content_format", 2),
    )
    selected["production_mode"] = _choose(
        plan_id=plan_id,
        axis="production_mode",
        values=("story_first",) if story_first else ("standard",),
        history=history,
        persona_pool_size=_persona_pool_size(context, "production_mode", 2),
    )
    for axis in (
        "thesis_direction",
        "epistemic_state",
        "tension",
        "semantic_theme",
        "facet",
        "author_role",
        "emotional_arc",
        "reader_relation",
        "structure",
        "hook",
        "ending",
        "energy",
        "seriousness",
        "tempo",
        "length",
        "humor",
        "imagery",
        "visual_mode",
        "visual_subject_direction",
        "visual_relation",
        "track_tags",
    ):
        selected[axis] = _choose(
            plan_id=plan_id,
            axis=axis,
            values=_axis_values(context, rubric, axis),
            history=history,
            preferred=str(context.preferred.get(axis, "")),
            persona_pool_size=_persona_pool_size(
                context,
                axis,
                len(_unique(context.pools.get(axis, ()))),
            ),
        )

    selected["semantic_card"] = _choose(
        plan_id=plan_id,
        axis="semantic_card",
        values=context.semantic_cards.get(
            selected["semantic_theme"],
            _axis_values(context, rubric, "semantic_card"),
        ),
        history=history,
        preferred=str(context.preferred.get("semantic_card", "")),
        persona_pool_size=_persona_pool_size(
            context,
            "semantic_card",
            len(
                _unique(
                    card
                    for cards in context.semantic_cards.values()
                    for card in cards
                )
            ),
        ),
    )

    track_tags = tuple(
        item.strip()
        for item in selected.pop("track_tags").split(",")
        if item.strip()
    )
    source_metadata = context.source_metadata.get(source.source_ref, {})
    plan = EditorialPlan(
        plan_id=plan_id,
        persona=context.persona,
        platform=context.platform,
        slot=context.slot,
        rubric=rubric.name,
        mode=rubric.mode,
        source_type=source.source_type,
        source_ref=source.source_ref,
        topic=source.topic,
        purpose=rubric.purpose,
        track_tags=track_tags,
        orchestrator_version=ORCHESTRATOR_VERSION,
        content_policy_version=str(context.policy_versions.get("content", "content-v1")),
        visual_policy_version=str(context.policy_versions.get("visual", "visual-v1")),
        music_policy_version=str(context.policy_versions.get("music", "music-v1")),
        production_policy=production_policy,
        source_project=str(source_metadata.get("project", "")),
        source_date=str(source_metadata.get("date", "")),
        **selected,
    )
    validate_plan(plan)
    return plan


def validate_plan(plan: EditorialPlan) -> None:
    production_policy = EditorialPlanPolicy.normalize(plan.production_policy)
    if not plan.plan_id or not plan.source_ref or not plan.topic:
        raise EditorialPlanError("plan identity and source are required")
    if production_policy == EditorialPlanPolicy.STANDARD_ONLY and (
        plan.production_mode != "standard" or plan.content_format != "text_post"
    ):
        raise EditorialPlanError("standard-only plan must use standard text production")
    if not plan.visual_subject_direction or not plan.visual_relation:
        raise EditorialPlanError("visual subject and thesis relation are required")
    visual = f"{plan.visual_subject_direction} {plan.visual_relation}".casefold()
    if any(item in visual for item in _FORBIDDEN_VISUALS):
        raise EditorialPlanError("generic or unrelated human visual is forbidden")
    if not plan.track_tags:
        raise EditorialPlanError("track tags are required")


def generation_prompt(
    plan: EditorialPlan,
    *,
    persona_direction: str,
    source_material: str = "",
    technical_retry_reason: str = "",
) -> str:
    retry = ""
    if technical_retry_reason:
        retry = (
            "\nTECHNICAL RETRY: the previous response was unusable because its JSON/schema "
            f"was invalid ({technical_retry_reason[:160]}). Execute the exact same plan_id and "
            "all the same axes. Do not redesign the release.\n"
        )
    return (
        "Execute the immutable EditorialPlan below. You are the writer, managing editor, "
        "semantic editor, dramaturg, voice editor and visual editor in one pass. "
        "Do not choose new axes and do not explain the plan. Return one JSON object only.\n\n"
        f"PERSONA DIRECTION:\n{persona_direction.strip()}\n\n"
        f"EDITORIAL PLAN:\n{json.dumps(plan.to_dict(), ensure_ascii=False, sort_keys=True)}\n\n"
        f"SOURCE MATERIAL (facts only; never invent missing facts):\n{source_material[:5000]}\n"
        f"{retry}\n"
        "Required JSON keys: final_text, concrete_scene, visual_subject, "
        "visual_relation_to_thesis, image_prompt_seed, track_tags.\n"
        "The final text must follow the selected thesis, structure, hook, ending, tone and "
        "length. The concrete scene must be specific. The visual subject and image seed must "
        "depict that same scene and their relation to the thesis must be expressible in one "
        "clear sentence. Never introduce random people, elderly people or grandparents, stock "
        "scenes, generic AI imagery, internal diagnostics, prompt text or publication mechanics. "
        "track_tags must exactly repeat the plan track_tags. Preserve the persona visual canon."
    )


def parse_generation_package(raw: str, plan: EditorialPlan) -> GenerationPackage:
    value = str(raw or "").strip().replace("```json", "").replace("```", "").strip()
    start, end = value.find("{"), value.rfind("}")
    if start < 0 or end <= start:
        raise GenerationPackageError("missing JSON object")
    try:
        payload = json.loads(value[start : end + 1])
    except json.JSONDecodeError as exc:
        raise GenerationPackageError("invalid JSON") from exc
    if not isinstance(payload, dict) or any(field not in payload for field in _PACKAGE_FIELDS):
        raise GenerationPackageError("generation package schema mismatch")
    text = str(payload["final_text"] or "").strip()
    scene = str(payload["concrete_scene"] or "").strip()
    subject = str(payload["visual_subject"] or "").strip()
    relation = str(payload["visual_relation_to_thesis"] or "").strip()
    seed = str(payload["image_prompt_seed"] or "").strip()
    tags_raw = payload["track_tags"]
    if not isinstance(tags_raw, list):
        raise GenerationPackageError("track_tags must be a list")
    tags = tuple(str(item).strip() for item in tags_raw if str(item).strip())
    if tags != plan.track_tags:
        raise GenerationPackageError("track_tags do not match EditorialPlan")
    if min(len(text), len(scene), len(subject), len(relation), len(seed)) < 8:
        raise GenerationPackageError("generation package contains an empty field")
    public = f"{text}\n{scene}\n{subject}\n{relation}\n{seed}".casefold()
    if "diag:" in public or "traceback" in public or "internal exception" in public:
        raise GenerationPackageError("internal diagnostics are forbidden")
    if any(item in f"{subject} {seed}".casefold() for item in _FORBIDDEN_VISUALS):
        raise GenerationPackageError("forbidden generic visual subject")
    return GenerationPackage(text, scene, subject, relation, seed, tags)


def package_visual_brief(plan: EditorialPlan, package: GenerationPackage) -> str:
    return (
        f"Plan ID: {plan.plan_id}. Visual mode: {plan.visual_mode}. "
        f"Subject direction: {plan.visual_subject_direction}. Concrete subject: {package.visual_subject}. "
        f"Scene: {package.concrete_scene}. Thesis relation: {package.visual_relation_to_thesis}. "
        f"Canonical image seed: {package.image_prompt_seed}. Imagery: {plan.imagery}."
    )
