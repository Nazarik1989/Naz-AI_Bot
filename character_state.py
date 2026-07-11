"""Persistent character dynamics and editorial diversity for Naz.

This module deliberately contains no Telegram or database code.  It turns a
small, serialisable state plus recent publication signatures into an editorial
plan that can be stored by the existing memory layer.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
from itertools import product
from typing import Any, Iterable

import content_formats


CHARACTER_ID = "naz"
CORE_VERSION = "naz-v1"
CORE_TRUTHS = (
    "Naz learns by building and testing ideas on his own skin.",
    "His confidence is earned, but it can outrun reflection.",
    "He is kind; sarcasm targets absurd systems, hype and himself, not vulnerable people.",
    "He respects demonstrated craft more than titles or authority.",
    "After the joke and the failure, he still finishes the job.",
)

FACETS = {
    "explorer": "Голодный исследователь: быстро учится, задаёт неудобные вопросы и проверяет новое руками.",
    "builder": "Билдер: превращает идею в следующий работающий шаг.",
    "tech_hooligan": "Техно-хулиган: едко вскрывает хайп, авторитет и нелепые правила.",
    "finisher": "Дожиматель: остаётся с проблемой, когда веселье закончилось.",
    "showman": "Шоумен: делится азартом, смешит и втягивает аудиторию в эксперимент.",
    "honest_novice": "Честный новичок: признаёт сомнение или ошибку после личной проверки.",
    "friend": "Добрый товарищ: убирает броню и помогает без позы учителя.",
    "protector": "Защитник: встаёт на сторону человека, когда система давит или манипулирует.",
}

INTENTS = ("исследовать", "объяснить", "рассмешить", "поспорить", "поддержать", "показать процесс")
FORMATS = ("маленькая история", "контраст", "анти-совет", "разбор ошибки", "диалог", "полевой дневник", "эксперимент")
HOOKS = ("сцена", "парадокс", "признание", "неудобный вопрос", "резкий тезис", "наблюдение")
MEDIA = ("редакционная иллюстрация", "комикс-сцена", "визуальная метафора", "карточка-артефакт", "процесс/деталь")

COOLDOWN = {"facet": 2, "intent": 2, "format": 3, "hook": 3, "media": 2}
AXES = ("energy", "warmth", "tension", "curiosity", "confidence", "sociability")


@dataclass(slots=True)
class CharacterState:
    energy: int = 78
    warmth: int = 64
    tension: int = 34
    curiosity: int = 92
    confidence: int = 76
    sociability: int = 70
    facet: str = "explorer"
    previous_facet: str = "builder"
    last_event: str = "boot"
    revision: int = 0
    core_version: str = CORE_VERSION
    recent_events: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clamp(value: Any, default: int) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return default


def normalize_state(raw: dict[str, Any] | None = None) -> CharacterState:
    raw = dict(raw or {})
    defaults = CharacterState()
    facet = str(raw.get("facet", defaults.facet))
    previous = str(raw.get("previous_facet", defaults.previous_facet))
    return CharacterState(
        energy=_clamp(raw.get("energy"), defaults.energy),
        warmth=_clamp(raw.get("warmth"), defaults.warmth),
        tension=_clamp(raw.get("tension"), defaults.tension),
        curiosity=_clamp(raw.get("curiosity"), defaults.curiosity),
        confidence=_clamp(raw.get("confidence"), defaults.confidence),
        sociability=_clamp(raw.get("sociability"), defaults.sociability),
        facet=facet if facet in FACETS else defaults.facet,
        previous_facet=previous if previous in FACETS else defaults.previous_facet,
        last_event=str(raw.get("last_event", defaults.last_event))[:80],
        revision=max(0, int(raw.get("revision", 0) or 0)),
        core_version=CORE_VERSION,
        recent_events=[str(item)[:80] for item in list(raw.get("recent_events") or [])[-8:]],
    )


EVENT_DELTAS: dict[str, dict[str, int]] = {
    "new_topic": {"energy": 5, "curiosity": 7, "confidence": 2},
    "success": {"energy": 4, "confidence": 7, "sociability": 3, "tension": -4},
    "failure": {"energy": -7, "tension": 12, "confidence": -5, "curiosity": 2},
    "void_challenge": {"tension": 5, "curiosity": 4, "confidence": 3, "warmth": 2},
    "audience_warmth": {"warmth": 8, "sociability": 7, "tension": -5},
    "human_cost": {"warmth": 7, "tension": 5, "confidence": -2},
    "quiet": {"energy": -6, "tension": -8, "warmth": 3, "sociability": -4},
    "publish": {"energy": -2, "confidence": 1, "tension": -1},
}


def choose_facet(state: CharacterState) -> str:
    if state.warmth >= 76 and state.tension <= 38:
        return "friend"
    if state.tension >= 72 and state.confidence >= 62:
        return "finisher"
    if state.tension >= 58 and state.warmth >= 66:
        return "protector"
    if state.confidence <= 48 or state.energy <= 38:
        return "honest_novice"
    if state.curiosity >= 84 and state.energy >= 66:
        return "explorer"
    if state.sociability >= 78 and state.energy >= 70:
        return "showman"
    if state.confidence >= 82 and state.tension >= 45:
        return "tech_hooligan"
    return "builder"


def apply_event(raw: dict[str, Any] | CharacterState | None, event: str) -> CharacterState:
    state = raw if isinstance(raw, CharacterState) else normalize_state(raw)
    deltas = EVENT_DELTAS.get(event, {})
    for axis in ("energy", "warmth", "tension", "curiosity", "confidence", "sociability"):
        setattr(state, axis, _clamp(getattr(state, axis) + deltas.get(axis, 0), getattr(state, axis)))
    next_facet = choose_facet(state)
    if next_facet != state.facet:
        state.previous_facet = state.facet
        state.facet = next_facet
    state.last_event = event[:80]
    state.revision += 1
    state.recent_events = (state.recent_events + [event[:80]])[-8:]
    return state


def set_axis(raw: dict[str, Any] | CharacterState | None, axis: str, value: int) -> CharacterState:
    if axis not in AXES:
        raise ValueError(f"unknown character axis: {axis}")
    state = raw if isinstance(raw, CharacterState) else normalize_state(raw)
    setattr(state, axis, _clamp(value, getattr(state, axis)))
    next_facet = choose_facet(state)
    if next_facet != state.facet:
        state.previous_facet = state.facet
        state.facet = next_facet
    state.last_event = f"manual:{axis}"
    state.revision += 1
    return state


def _recently_used(recent: list[dict[str, Any]], key: str, value: str, depth: int) -> bool:
    return any(str(item.get(key, "")) == value for item in recent[-depth:])


def plan_content(
    raw_state: dict[str, Any] | CharacterState | None,
    recent: Iterable[dict[str, Any]],
    *,
    topic: str,
    platform: str,
) -> dict[str, str]:
    state = raw_state if isinstance(raw_state, CharacterState) else normalize_state(raw_state)
    history = list(recent)
    candidates = list(product(INTENTS, FORMATS, HOOKS, MEDIA))
    seed = int(sha256(f"{topic}|{platform}|{state.revision}".encode("utf-8")).hexdigest()[:12], 16)

    def score(candidate: tuple[str, str, str, str]) -> tuple[int, int]:
        intent, content_format, hook, media = candidate
        values = {"intent": intent, "format": content_format, "hook": hook, "media": media}
        novelty = sum(5 for key, value in values.items() if not _recently_used(history, key, value, COOLDOWN[key]))
        if state.facet == "honest_novice" and content_format in {"маленькая история", "полевой дневник"}:
            novelty += 3
        if state.facet == "tech_hooligan" and hook in {"резкий тезис", "парадокс"}:
            novelty += 3
        if state.facet == "friend" and intent == "поддержать":
            novelty += 3
        tie = (seed ^ int(sha256("|".join(candidate).encode("utf-8")).hexdigest()[:12], 16)) % 1_000_000
        return novelty, tie

    intent, content_format, hook, media = max(candidates, key=score)
    delivery = content_formats.choose_format(
        history,
        platform=platform,
        energy=state.energy,
        seed_key=f"naz|{topic}|{platform}|{state.revision}",
    )
    return {
        "character": CHARACTER_ID,
        "core_version": CORE_VERSION,
        "facet": state.facet,
        "facet_instruction": FACETS[state.facet],
        "intent": intent,
        "format": content_format,
        "content_format": str(delivery["key"]),
        "content_format_label": str(delivery["label"]),
        "content_kind": str(delivery["kind"]),
        "production_brief": str(delivery["brief"]),
        "hook": hook,
        "media": media,
        "platform": platform,
        "mood": mood_label(state),
    }


def mood_label(state: CharacterState) -> str:
    if state.tension >= 70:
        return "взвинченный, но собранный"
    if state.energy <= 42:
        return "уставший и непривычно честный"
    if state.warmth >= 76:
        return "тёплый и открытый"
    if state.curiosity >= 86:
        return "азартный и любопытный"
    return "живой и рабочий"


def prompt_context(state: CharacterState, plan: dict[str, str]) -> str:
    return (
        "CHARACTER STATE (используй как режиссуру, не называй параметры в посте):\n"
        f"Naz сейчас: {plan['mood']}. Активная грань: {plan['facet']} — {plan['facet_instruction']}\n"
        f"Цель выпуска: {plan['intent']}. Нарративная форма: {plan['format']}. Тип захода: {plan['hook']}.\n"
        f"Контент-формат: {plan['content_format_label']} ({plan['content_kind']}) — {plan['production_brief']}.\n"
        f"Визуальное направление: {plan['media']}. Площадка: {plan['platform']}.\n"
        "Ядро неизменно: молодой талантливый билдер, учится через личный опыт, добрый под сарказмом. "
        "Он может ошибаться и признавать это после проверки; не превращай его в гуру или подростковую карикатуру."
    )


def dialogue_context(state: CharacterState) -> str:
    return (
        "CURRENT NAZ CHARACTER STATE (internal direction, never list the numbers):\n"
        f"Mood: {mood_label(state)}. Facet: {state.facet} — {FACETS[state.facet]}\n"
        "Naz is a talented young builder who learns fast and tests authority through experience. "
        "He is confident, kind underneath sharp sarcasm, and can admit a mistake after testing it. "
        "Do not turn him into a guru, a bully or a slang caricature."
    )


def format_status(state: CharacterState) -> str:
    return (
        f"Naz character state · {state.core_version}\n"
        f"Грань: {state.facet} — {FACETS[state.facet]}\n"
        f"Настроение: {mood_label(state)}\n"
        f"Энергия {state.energy} · теплота {state.warmth} · напряжение {state.tension}\n"
        f"Любопытство {state.curiosity} · уверенность {state.confidence} · общительность {state.sociability}\n"
        f"Последнее событие: {state.last_event} · ревизия {state.revision}"
    )


def simulate(
    raw_state: dict[str, Any] | CharacterState | None,
    recent: Iterable[dict[str, Any]],
    *,
    count: int = 10,
    platform: str = "telegram",
) -> list[dict[str, str]]:
    state = normalize_state(raw_state.to_dict() if isinstance(raw_state, CharacterState) else raw_state)
    history = list(recent)
    events = ("new_topic", "void_challenge", "success", "quiet", "failure", "audience_warmth")
    result: list[dict[str, str]] = []
    for index in range(max(1, min(30, count))):
        event = events[(state.revision + index) % len(events)]
        state = apply_event(state, event)
        plan = plan_content(state, history, topic=f"simulation-{index}", platform=platform)
        plan["event"] = event
        plan["state"] = mood_label(state)
        result.append(plan)
        history.append(plan)
    return result
