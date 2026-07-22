"""Naz catalog adapter for Editorial Orchestrator v1.

This module exposes data only.  It never selects a value; all final choices are
made by :func:`editorial_orchestrator.plan_release`.
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable, Mapping

import character_state
import content_formats
import semantic_autopost
from editorial_orchestrator import EditorialContext, EditorialRubric, EditorialSource


POLICY_VERSIONS = {
    "content": "naz-content-v2.4-orchestrated",
    "visual": "naz-visual-canon-v1",
    "music": "naz-vk-allowlist-last8-v1",
}


BASE_POOLS: dict[str, tuple[str, ...]] = {
    "thesis_direction": (
        "show what a small practical choice changes for a person",
        "separate demonstrated craft from confident appearance",
        "trace responsibility through a handoff",
        "test an assumption against one observable consequence",
        "show how a useful boundary makes action clearer",
        "locate the hidden work behind apparent simplicity",
        "distinguish progress from a polished response",
        "show a failure as information rather than identity",
        "find the human cost of an absurd system rule",
        "show maintenance as active authorship",
        "contrast hype with a repeatable working practice",
        "show how trust is built through a concrete reliable action",
    ),
    "epistemic_state": (
        "personally tested observation",
        "bounded inference from a concrete scene",
        "honest unresolved question",
        "documented source interpretation",
        "working hypothesis with visible limits",
    ),
    "tension": (
        "speed versus understanding",
        "confidence versus evidence",
        "automation versus responsibility",
        "play versus consequence",
        "simplicity versus hidden work",
        "control versus trust",
        "finish versus endless improvement",
    ),
    "semantic_theme": tuple(theme.key for theme in semantic_autopost.THEMES),
    "semantic_card": tuple(card.key for card in semantic_autopost.SEMANTIC_CARDS),
    "facet": tuple(character_state.FACETS),
    "author_role": (
        "builder reporting from the workbench",
        "curious tester",
        "friendly technical translator",
        "self-critical finisher",
        "sharp observer of absurd systems",
        "honest novice after a real check",
    ),
    "emotional_arc": (
        "curiosity to earned clarity",
        "confidence to correction",
        "irritation to a workable action",
        "comic recognition to serious consequence",
        "uncertainty to a bounded conclusion",
        "pressure to calm completion",
    ),
    "reader_relation": (
        "think beside the reader",
        "share a field note without teaching from above",
        "invite one concrete check",
        "name a shared frustration without blaming the user",
        "leave room for disagreement",
    ),
    "structure": tuple(character_state.FORMATS)
    + tuple(
        str(item["label"])
        for item in content_formats.FORMAT_REGISTRY
        if item.get("ready") and "telegram" in item.get("platforms", set())
    ),
    "hook": tuple(character_state.HOOKS),
    "ending": (
        "specific changed consequence",
        "bounded practical conclusion",
        "open question earned by the scene",
        "quiet reversal of the opening assumption",
        "one action the author will actually test",
        "unresolved but precisely named tension",
    ),
    "energy": ("low", "measured", "alive", "driven"),
    "seriousness": ("light", "balanced", "serious", "quietly weighty"),
    "tempo": ("slow", "measured", "brisk", "punchy"),
    "length": ("700-900 characters", "850-1100 characters", "1000-1400 characters"),
    "humor": ("none", "dry", "self-directed", "one restrained absurd detail"),
    "imagery": (
        "concrete workbench detail",
        "physical consequence of a system decision",
        "tactile object in use",
        "threshold between idea and working result",
        "small failure visible in a real environment",
    ),
    "visual_mode": tuple(character_state.MEDIA) + ("MATERIAL sequence",),
    "visual_subject_direction": (
        "one tool, device or material carrying evidence of use",
        "one specific work surface at the moment a result changes",
        "one interface consequence represented through a real object and action",
        "a process detail with hands only when the action requires them",
        "an object-only scene with no invented human hero",
        "the canonical Naz presence only when the thesis genuinely concerns him",
    ),
    "visual_relation": (
        "the subject visibly carries the consequence named by the thesis",
        "the scene shows the exact constraint that creates the conclusion",
        "the object makes the hidden work in the thesis physically legible",
        "the before-and-after state visualizes the plan's central tension",
        "the chosen action and its result are visible in the same scene",
    ),
    "track_tags": (
        "daily,focus,builder", "daily,warm,reflective", "daily,systems,calm",
        "gaming,mechanic,energy", "gaming,identity,reflective",
    ),
}


SEMANTIC_CARDS = {
    theme.key: tuple(card.key for card in semantic_autopost.CARDS_BY_THEME[theme.key])
    for theme in semantic_autopost.THEMES
}


def rubric_key(name: str) -> str:
    return hashlib.sha256(str(name).encode("utf-8")).hexdigest()[:12]


def build_context(
    *,
    platform: str,
    slot: str,
    seed: str,
    rubric_rows: Iterable[Mapping[str, Any]],
    source_rows: Iterable[Mapping[str, Any]],
    published_history: Iterable[Mapping[str, Any]],
    character: character_state.CharacterState,
    crosspost_plan_id: str = "",
    persona_rubric_rows: Iterable[Mapping[str, Any]] | None = None,
    persona_source_rows: Iterable[Mapping[str, Any]] | None = None,
) -> EditorialContext:
    rubric_rows = tuple(rubric_rows)
    source_rows = tuple(source_rows)
    complete_rubric_rows = (
        tuple(persona_rubric_rows)
        if persona_rubric_rows is not None
        else rubric_rows
    )
    complete_source_rows = (
        tuple(persona_source_rows)
        if persona_source_rows is not None
        else source_rows
    )
    rubrics: list[EditorialRubric] = []
    for row in rubric_rows:
        name = str(row.get("name") or "Naz release")
        key = str(row.get("key") or rubric_key(name))
        kind = str(row.get("kind") or row.get("task") or "daily")
        compatible_themes = tuple(
            theme.key for theme in semantic_autopost.compatible_themes(name)
        )
        track_tags = str(row.get("track_tags") or ("gaming,mechanic,energy" if kind == "gaming" else "daily,focus,warm"))
        material = "material" in name.casefold() or "матери" in name.casefold()
        constraints: dict[str, tuple[str, ...]] = {
            "semantic_theme": compatible_themes,
            "track_tags": (track_tags,),
            "length": (("700-1100 characters",) if platform == "telegram" else ("700-1400 characters",)),
        }
        if material:
            constraints.update(
                {
                    "visual_mode": ("MATERIAL sequence",),
                    "imagery": ("tactile object in use",),
                    "visual_subject_direction": ("one material object with visible wear, touch and use; no invented person",),
                }
            )
        rubrics.append(
            EditorialRubric(
                key=key,
                name=name,
                mode=kind,
                purpose=str(row.get("angle") or row.get("brief") or row.get("task") or "make one useful original Naz release"),
                constraints=constraints,
            )
        )
    sources = tuple(
        EditorialSource(
            source_ref=str(row.get("source_ref") or f"catalog:{index}"),
            topic=str(row.get("topic") or "AI, craft and practical systems"),
            source_type=str(row.get("source_type") or "catalog"),
            rubric_keys=tuple(str(item) for item in row.get("rubric_keys", ())),
            safe_facts=tuple(str(item)[:500] for item in row.get("safe_facts", ()) if str(item).strip()),
            source_verified=bool(row.get("source_verified", False)),
            concrete_action=bool(row.get("concrete_action", False)),
            visualizable_process=bool(row.get("visualizable_process", False)),
            causal_bits=max(0, int(row.get("causal_bits", 0) or 0)),
            real_result=bool(row.get("real_result", False)),
            contains_secrets=bool(row.get("contains_secrets", False)),
            contains_private_data=bool(row.get("contains_private_data", False)),
        )
        for index, row in enumerate(source_rows)
    )
    preferred_energy = "driven" if character.energy >= 72 else "measured"
    preferred_humor = "dry" if character.facet in {"tech_hooligan", "showman"} else "self-directed"
    persona_pool_sizes = {
        axis: len(dict.fromkeys(str(value) for value in values if str(value)))
        for axis, values in BASE_POOLS.items()
    }
    persona_pool_sizes.update(
        {
            "rubric": len(
                dict.fromkeys(
                    str(row.get("name") or "Naz release")
                    for row in (complete_rubric_rows or rubric_rows)
                )
            ),
            "source_ref": len(
                dict.fromkeys(
                    str(row.get("source_ref") or f"catalog:{index}")
                    for index, row in enumerate(complete_source_rows or source_rows)
                )
            ),
            "content_format": 2,
            "production_mode": 2,
        }
    )
    return EditorialContext(
        persona="naz",
        platform=platform,
        slot=slot,
        seed=seed,
        sources=sources,
        rubrics=tuple(rubrics),
        pools=BASE_POOLS,
        persona_pool_sizes=persona_pool_sizes,
        semantic_cards=SEMANTIC_CARDS,
        published_history=tuple(published_history),
        preferred={
            "facet": character.facet,
            "energy": preferred_energy,
            "humor": preferred_humor,
        },
        policy_versions=POLICY_VERSIONS,
        crosspost_plan_id=crosspost_plan_id,
    )


def persona_direction(character: character_state.CharacterState) -> str:
    return (
        f"Naz is a young, kind, technically gifted builder. Current facet: {character.facet} — "
        f"{character_state.FACETS[character.facet]} Current mood: {character_state.mood_label(character)}. "
        "Sarcasm targets hype, absurd systems and himself, never vulnerable people. He learns by "
        "testing, may be wrong, and does not speak as an infallible guru. Character is the lens, "
        "not a mandatory topic or repeated moral. Preserve the canonical Naz visual identity."
    )
