"""Controller layer for Naz_AI_Bot v2.4.

GPT не управляет ботом. GPT исполняет решение Controller-а.
Controller отвечает за: intent, state, smart filter, angle engine,
чистый контекст, Content Quality Layer и memory update.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional

from prompts import (
    DEFAULT_CONTENT_GOAL,
    DEFAULT_EXPERT_MODE,
    DEFAULT_VOICE_PROFILE,
    EXPERT_MODES,
    GOALS,
    VOICE_PROFILES,
)

VALID_GOALS = set(GOALS.keys())
CONTENT_TASKS = {"post", "viral", "script", "plan", "hooks", "imagepost", "publish", "autopost", "angle_post", "insight"}
ANGLE_ENGINE_VERSION = "v2.4"

CONTENT_WORDS = (
    "пост", "текст", "сторис", "stories", "reels", "рилс", "shorts", "сценарий",
    "контент", "заголов", "хук", "карусель", "тред", "напиши", "сгенерируй",
    "опубликуй", "сделай", "придумай", "упакуй", "оформи", "перепиши",
)

ANALYTICS_WORDS = ("статус", "статистика", "память", "метрики", "сколько", "история")

BANNED_GENERIC_TOPICS = (
    "успешный успех",
    "как добиться успеха",
    "секрет успеха",
    "мотивация для предпринимателей",
    "нейросети изменят мир",
    "искусственный интеллект это будущее",
)

ANGLE_PATTERNS: List[Dict[str, str]] = [
    {
        "kind": "conflict",
        "title": "Конфликт",
        "emoji": "⚔️",
        "hook": "Telegram был не виноват. Но мы ругались именно на него.",
        "instruction": "Поверни тему через конфликт: казалось, что виноват один слой, а проблема оказалась в другом.",
    },
    {
        "kind": "mistake",
        "title": "Ошибка",
        "emoji": "🧨",
        "hook": "Самая дорогая ошибка — чинить не тот кусок системы.",
        "instruction": "Поверни тему через конкретную ошибку: что сломалось, как искали и что поняли.",
    },
    {
        "kind": "instruction",
        "title": "Инструкция",
        "emoji": "🛠",
        "hook": "Если бот не работает, не надо паниковать. Надо идти по цепочке.",
        "instruction": "Поверни тему в практический алгоритм: диагностика, проверка, фикс, тест.",
    },
    {
        "kind": "lesson",
        "title": "Урок",
        "emoji": "📌",
        "hook": "Проблема была не в коде. Проблема была в отсутствии слоя управления.",
        "instruction": "Поверни тему через вывод: какой принцип стал понятен после багов.",
    },
    {
        "kind": "provocation",
        "title": "Провокация",
        "emoji": "🔥",
        "hook": "Если бот пишет всё подряд — это не AI-система. Это генератор шума.",
        "instruction": "Поверни тему через спорный тезис, который заставляет пересмотреть подход.",
    },
    {
        "kind": "case",
        "title": "Кейс",
        "emoji": "🧪",
        "hook": "Мы думали, что проверяем картинки. На самом деле проверяли архитектуру.",
        "instruction": "Поверни тему как мини-кейс: было, сломалось, проверили, нашли, починили.",
    },
    {
        "kind": "anti_pattern",
        "title": "Антипаттерн",
        "emoji": "🚫",
        "hook": "Плохой бот начинается там, где GPT решает всё сам.",
        "instruction": "Покажи, как делать не надо, и какой слой нужен вместо хаоса.",
    },
    {
        "kind": "personal",
        "title": "Личный опыт",
        "emoji": "🧠",
        "hook": "Был момент, когда проще было закрыть VS Code и сделать вид, что так и задумано.",
        "instruction": "Поверни тему через личный момент: усталость, раздражение, красный Shell, но дожим.",
    },
    {
        "kind": "system",
        "title": "Система",
        "emoji": "🧩",
        "hook": "Хаос перестал быть хаосом, когда мы разложили его по слоям.",
        "instruction": "Поверни тему через архитектуру: controller, state, filter, GPT, memory.",
    },
    {
        "kind": "diagnostic",
        "title": "Диагностика",
        "emoji": "🔎",
        "hook": "Когда всё падает, первый вопрос не “почему”. Первый вопрос — “где именно”.",
        "instruction": "Поверни тему через метод поиска причины: исключаем Telegram, потом картинки, потом генератор.",
    },
]


def normalize_text(value: str) -> str:
    value = (value or "").lower().strip()
    value = re.sub(r"[^а-яёa-z0-9\s-]", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def normalize_state(state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return safe state object with all required keys."""
    state = dict(state or {})

    expert = state.get("expert") or state.get("expert_mode") or DEFAULT_EXPERT_MODE
    if expert not in EXPERT_MODES:
        expert = DEFAULT_EXPERT_MODE

    voice = state.get("voice") or state.get("voice_profile") or DEFAULT_VOICE_PROFILE
    if voice not in VOICE_PROFILES:
        voice = DEFAULT_VOICE_PROFILE

    goal = state.get("goal") or state.get("content_goal") or DEFAULT_CONTENT_GOAL
    if goal not in VALID_GOALS:
        goal = DEFAULT_CONTENT_GOAL

    recent_topics = state.get("recent_topics") or []
    banned_topics = state.get("banned_topics") or []
    best_posts = state.get("best_posts") or []
    rejected_topics = state.get("rejected_topics") or []
    suggested_angles = state.get("suggested_angles") or []

    if not isinstance(recent_topics, list):
        recent_topics = []
    if not isinstance(banned_topics, list):
        banned_topics = []
    if not isinstance(best_posts, list):
        best_posts = []
    if not isinstance(rejected_topics, list):
        rejected_topics = []
    if not isinstance(suggested_angles, list):
        suggested_angles = []

    content_count = state.get("content_count", 0)
    try:
        content_count = int(content_count)
    except (TypeError, ValueError):
        content_count = 0

    selected_angle_index = state.get("selected_angle_index", 0)
    try:
        selected_angle_index = int(selected_angle_index)
    except (TypeError, ValueError):
        selected_angle_index = 0

    angle_generation_round = state.get("angle_generation_round", 0)
    try:
        angle_generation_round = int(angle_generation_round)
    except (TypeError, ValueError):
        angle_generation_round = 0

    return {
        "mode": state.get("mode") or "hybrid",
        "expert": expert,
        "expert_mode": expert,
        "voice": voice,
        "voice_profile": voice,
        "goal": goal,
        "content_goal": goal,
        "last_input": state.get("last_input") or "",
        "recent_topics": recent_topics[-20:],
        "content_count": content_count,
        "banned_topics": banned_topics[-50:],
        "best_posts": best_posts[-10:],
        "rejected_topics": rejected_topics[-20:],
        "quality_profile": state.get("quality_profile") or "naz_clean_v24",
        "content_rules_version": "v2.4",
        "angle_engine_version": ANGLE_ENGINE_VERSION,
        "last_blocked_topic": state.get("last_blocked_topic") or "",
        "suggested_angles": suggested_angles[:5],
        "selected_angle_index": max(0, min(selected_angle_index, 4)),
        "angle_generation_round": angle_generation_round,
        "memory_enabled": bool(state.get("memory_enabled", True)),
    }


def is_content_request(user_input: str, task: Optional[str] = None) -> bool:
    if task in CONTENT_TASKS:
        return True
    text = normalize_text(user_input)
    return any(word in text for word in CONTENT_WORDS)


def detect_mode(user_input: str, task: Optional[str] = None) -> str:
    """Determine high-level route without asking GPT."""
    text = normalize_text(user_input)

    if is_content_request(user_input, task):
        return "content_generation"
    if any(word in text for word in ANALYTICS_WORDS):
        return "analytics"
    return "chat"


def is_similar_topic(a: str, b: str) -> bool:
    a_norm = normalize_text(a)
    b_norm = normalize_text(b)
    if not a_norm or not b_norm:
        return False
    if a_norm == b_norm:
        return True
    if a_norm in b_norm or b_norm in a_norm:
        return True
    return SequenceMatcher(None, a_norm, b_norm).ratio() >= 0.82


def topic_fingerprint(topic: str) -> str:
    """Make topic more stable for duplicate detection."""
    text = normalize_text(topic)
    prefixes = (
        "напиши пост про", "напиши пост о", "сделай пост про", "сделай пост о",
        "сгенерируй пост про", "придумай пост про", "тема", "напиши текст про",
    )
    for prefix in prefixes:
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    return text[:300]


def suggest_angles(topic: str, state: Optional[Dict[str, Any]] = None, count: int = 5, offset: Optional[int] = None) -> List[Dict[str, str]]:
    """Return deterministic angle suggestions for an already-used topic."""
    clean_state = normalize_state(state)
    round_offset = clean_state.get("angle_generation_round", 0) if offset is None else offset
    topic_norm = topic_fingerprint(topic)
    seed = sum(ord(ch) for ch in topic_norm) % len(ANGLE_PATTERNS)
    start = (seed + int(round_offset)) % len(ANGLE_PATTERNS)

    angles: List[Dict[str, str]] = []
    for i in range(count):
        base = ANGLE_PATTERNS[(start + i) % len(ANGLE_PATTERNS)]
        angles.append(
            {
                "kind": base["kind"],
                "title": base["title"],
                "emoji": base["emoji"],
                "hook": base["hook"],
                "instruction": base["instruction"],
                "source_topic": topic[:500],
            }
        )
    return angles


def format_angles_message(topic: str, angles: List[Dict[str, str]]) -> str:
    lines = [
        "⚠️ Эта тема уже была недавно.",
        "",
        "Не повторяем одно и то же под другим соусом.",
        "Разворачиваем тему через новый угол:",
        "",
    ]
    for idx, angle in enumerate(angles[:5], start=1):
        lines.extend(
            [
                f"{idx}. {angle.get('emoji', '•')} {angle.get('title', 'Угол')}",
                f"   {angle.get('hook', '')}",
                "",
            ]
        )
    lines.extend(
        [
            "Что дальше:",
            "1️⃣–5️⃣ — выбрать угол",
            "✍️ Написать по углу — сгенерировать пост",
            "🔁 Другие углы — дать новые заходы",
            "🧹 Очистить recent — сбросить последние темы",
        ]
    )
    return "\n".join(lines).strip()


def save_angles_to_state(state: Dict[str, Any], topic: str, angles: List[Dict[str, str]]) -> Dict[str, Any]:
    state = normalize_state(state)
    state["last_blocked_topic"] = topic[:800]
    state["suggested_angles"] = angles[:5]
    state["selected_angle_index"] = 0
    return state


def refresh_angles(state: Dict[str, Any], topic: Optional[str] = None) -> Dict[str, Any]:
    state = normalize_state(state)
    base_topic = topic or state.get("last_blocked_topic") or state.get("last_input") or "AI, контент и автоматизация"
    state["angle_generation_round"] = int(state.get("angle_generation_round", 0)) + 1
    angles = suggest_angles(base_topic, state, offset=state["angle_generation_round"])
    return save_angles_to_state(state, base_topic, angles)


def select_angle(state: Dict[str, Any], index: int) -> Dict[str, Any]:
    state = normalize_state(state)
    state["selected_angle_index"] = max(0, min(int(index), 4))
    return state


def get_selected_angle(state: Dict[str, Any]) -> Optional[Dict[str, str]]:
    state = normalize_state(state)
    angles = state.get("suggested_angles") or []
    if not angles:
        return None
    idx = max(0, min(int(state.get("selected_angle_index", 0)), len(angles) - 1))
    angle = angles[idx]
    return angle if isinstance(angle, dict) else None


def build_angle_user_input(topic: str, angle: Dict[str, str]) -> str:
    return "\n".join(
        [
            "Controller выбрал новый угол для уже использованной темы.",
            "Не повторяй старый пост. Не пересказывай тему теми же словами.",
            "",
            f"Базовая тема: {topic}",
            f"Новый угол: {angle.get('emoji', '')} {angle.get('title', 'Угол')}",
            f"Хук-направление: {angle.get('hook', '')}",
            f"Инструкция угла: {angle.get('instruction', '')}",
            "",
            "Сделай Telegram-пост в стиле Naz.",
            "Фокус: новый конфликт, новый вывод, новая сцена. Старую тему не повторять.",
        ]
    )


def smart_filter(topic: str, state: Dict[str, Any]) -> Dict[str, Any]:
    """Filter duplicates, banned and too-generic topics."""
    topic_norm = topic_fingerprint(topic)
    state = normalize_state(state)

    for generic in BANNED_GENERIC_TOPICS:
        if is_similar_topic(topic_norm, generic) or generic in topic_norm:
            state["rejected_topics"].append(topic[:300])
            state["last_blocked_topic"] = topic[:800]
            state = refresh_angles(state, topic)
            return {
                "ok": False,
                "reason": "too_generic",
                "state": state,
                "message": (
                    "⚠️ Тема слишком общая и пахнет мотивационным туманом.\n\n"
                    "Naz не будет делать пост из воздуха. Выбери конкретный угол ниже.\n\n"
                    + format_angles_message(topic, state["suggested_angles"])
                ),
            }

    for banned in state["banned_topics"]:
        if is_similar_topic(topic_norm, str(banned)):
            state["rejected_topics"].append(topic[:300])
            state["last_blocked_topic"] = topic[:800]
            state = refresh_angles(state, topic)
            return {
                "ok": False,
                "reason": "banned_topic",
                "state": state,
                "message": "⚠️ Эту тему лучше не трогать: она в стоп-листе памяти Naz.",
            }

    for old_topic in state["recent_topics"][-20:]:
        if is_similar_topic(topic_norm, str(old_topic)):
            state["rejected_topics"].append(topic[:300])
            state = refresh_angles(state, topic)
            return {
                "ok": False,
                "reason": "duplicate_topic",
                "state": state,
                "message": format_angles_message(topic, state["suggested_angles"]),
            }

    return {"ok": True, "reason": "ok", "message": "", "state": state}


def build_clean_gpt_input(user_input: str, state: Dict[str, Any], task: Optional[str], source_topic: Optional[str]) -> str:
    """Give GPT a clean task. Controller decides. GPT executes."""
    state = normalize_state(state)
    topic = source_topic or user_input

    lines = [
        "Задача от Controller-а:",
        f"mode: {state['mode']}",
        f"expert: {state['expert']}",
        f"voice: {state['voice']}",
        f"goal: {state['goal']}",
        f"quality_profile: {state['quality_profile']}",
        f"angle_engine: {state['angle_engine_version']}",
    ]

    if task:
        lines.append(f"content_task: {task}")

    selected_angle = get_selected_angle(state)
    if selected_angle:
        lines.append(f"selected_angle: {selected_angle.get('title')} — {selected_angle.get('instruction')}")

    if state["recent_topics"]:
        lines.append("recent_topics: " + "; ".join(map(str, state["recent_topics"][-8:])))

    lines.extend(
        [
            "",
            "Правила исполнения v2.4:",
            "- Выдай готовый материал сразу. Не пиши вступление 'Вот пост' или объяснение, что ты сделал.",
            "- Не учи успеху и не продавай мотивационный воздух.",
            "- Не используй общие фразы: 'важно не сдаваться', 'ошибки — это опыт', 'в современном мире', 'каждый бизнес должен'.",
            "- Показывай путь через бардак, баги, кривой код, сломанные интеграции, красный Shell и дожим.",
            "- Пиши короткими абзацами. Один абзац — одна мысль. Лучше 1–3 строки, чем простыня.",
            "- Убирай канцелярит, шаблонные CTA и корпоративную вату.",
            "- Не используй markdown-заголовки вида '### Хук', '### Мысль', 'CTA', если пользователь явно не просил структуру.",
            "- Не выдумывай статистику, исследования, кейсы, имена и цифры.",
            "- Если выбран angle_post: делай новый угол, а не повтор той же темы.",
            "- Финал должен звучать как Naz: спокойно, живо, через дожим результата.",
            "",
            "Ввод пользователя:",
            user_input.strip(),
        ]
    )

    if source_topic and source_topic.strip() != user_input.strip():
        lines.extend(["", "Тема материала:", source_topic.strip()])

    return "\n".join(lines)


def controller(
    user_input: str,
    state: Optional[Dict[str, Any]],
    *,
    task: Optional[str] = None,
    source_topic: Optional[str] = None,
) -> Dict[str, Any]:
    """Main routing function."""
    clean_state = normalize_state(state)
    mode = detect_mode(user_input, task=task)
    clean_state["mode"] = mode
    clean_state["last_input"] = user_input[:2000]

    topic = (source_topic or user_input or "").strip()

    # angle_post intentionally bypasses duplicate blocking: it is a controlled rewrite
    # with a new angle selected by Controller.
    if (task in CONTENT_TASKS or mode == "content_generation") and topic and task != "angle_post":
        decision = smart_filter(topic, clean_state)
        clean_state = decision.get("state", clean_state)
        if not decision["ok"]:
            return {
                "blocked": True,
                "message": decision["message"],
                "reason": decision["reason"],
                "state": clean_state,
                "topic": topic,
                "gpt_input": "",
            }

    return {
        "blocked": False,
        "message": "",
        "reason": "ok",
        "state": clean_state,
        "topic": topic,
        "gpt_input": build_clean_gpt_input(user_input, clean_state, task, source_topic),
    }


def update_memory_after_output(state: Dict[str, Any], topic: str, output: str, task: Optional[str] = None) -> Dict[str, Any]:
    """Update state after successful content generation."""
    state = normalize_state(state)
    topic = (topic or "").strip()
    task_key = task or state.get("mode")

    if (task in CONTENT_TASKS or state.get("mode") == "content_generation") and topic:
        fingerprint = topic_fingerprint(topic)
        if not any(is_similar_topic(fingerprint, old) for old in state["recent_topics"]):
            state["recent_topics"].append(fingerprint[:300])
            state["recent_topics"] = state["recent_topics"][-20:]
        state["content_count"] = int(state.get("content_count", 0)) + 1

        # Эвристика: сохраняем плотные материалы как best_posts без отдельного GPT-оценщика.
        if output and len(output) >= 450:
            state["best_posts"].append({"topic": fingerprint[:200], "task": task_key, "preview": output[:500]})
            state["best_posts"] = state["best_posts"][-10:]

    return state
