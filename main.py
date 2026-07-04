"""Naz_AI_Bot v2.4.

Telegram AI Content OS:
- кнопочное меню второго уровня;
- persistent expert modes;
- OpenRouter GPT generation;
- SQLite memory layer;
- Hugging Face image generation;
- channel autoposting;
- admin protection.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import xml.etree.ElementTree as ET
from datetime import time
from html import unescape
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv

load_dotenv()

from openai import OpenAI
from telegram import InputMediaPhoto, KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, NetworkError, TelegramError, TimedOut
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import memory
import controller as naz_controller
from prompts import (
    CONTENT_TASK_PROMPTS,
    DEFAULT_CONTENT_GOAL,
    DEFAULT_EXPERT_MODE,
    DEFAULT_VOICE_PROFILE,
    EXPERT_MODES,
    GOALS,
    VOICE_PROFILES,
    build_messages,
    format_roles,
)

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OPENROUTER_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
MODEL_NAME = os.getenv("MODEL_NAME", "openai/gpt-4o-mini").strip()
CONTENT_MODEL_NAME = os.getenv("CONTENT_MODEL_NAME", MODEL_NAME).strip()

ADMIN_ID = env_int("ADMIN_ID", 0)
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()

HF_TOKEN = os.getenv("HF_TOKEN", "").strip()
HF_MODEL = os.getenv("HF_MODEL", "black-forest-labs/FLUX.1-schnell").strip()

BOT_TIMEZONE = os.getenv("BOT_TIMEZONE", "Europe/Moscow").strip()
APP_NAME = os.getenv("APP_NAME", "Naz_AI_Bot").strip()
NAZ_STORIES_FILE = Path(os.getenv("NAZ_STORIES_FILE", "naz_stories.md").strip())
MONITORED_SOURCES_FILE = Path(os.getenv("MONITORED_SOURCES_FILE", "monitored_sources.json").strip())
SOURCE_SEEN_FILE = Path(os.getenv("SOURCE_SEEN_FILE", ".source_seen.json").strip())

AUTOPOST_ENABLED = env_bool("AUTOPOST_ENABLED", True)
AUTOPOST_TIMES = os.getenv("AUTOPOST_TIMES", "10:00,20:00").strip()
AUTOPOST_TASKS = os.getenv("AUTOPOST_TASKS", "post,viral").strip()
AUTOPOST_INSIGHT_CHANCE = max(0.0, min(env_float("AUTOPOST_INSIGHT_CHANCE", 0.35), 1.0))
SOURCE_MONITOR_ENABLED = env_bool("SOURCE_MONITOR_ENABLED", False)
SOURCE_MONITOR_TIMES = os.getenv("SOURCE_MONITOR_TIMES", "12:00,18:00").strip()
REQUIRE_IMAGES_FOR_CHANNEL_POSTS = env_bool("REQUIRE_IMAGES_FOR_CHANNEL_POSTS", True)
CHANNEL_IMAGE_COUNT = max(1, min(env_int("CHANNEL_IMAGE_COUNT", 1), 2))
ALLOW_IMAGE_FALLBACK = env_bool("ALLOW_IMAGE_FALLBACK", True)
ADMIN_ONLY_CONTENT = env_bool("ADMIN_ONLY_CONTENT", True)

MAX_TOKENS = env_int("MAX_TOKENS", 900)
TEMPERATURE = env_float("TEMPERATURE", 0.8)

TASK_MAX_TOKENS = {
    "post": 430,
    "viral": 420,
    "angle_post": 430,
    "script": 620,
    "hooks": 650,
    "plan": 900,
    "image_prompt": 180,
    "insight": 430,
    "source_interpretation": 520,
}

CONTENT_MODEL_TASKS = {
    "post",
    "viral",
    "angle_post",
    "script",
    "plan",
    "hooks",
    "image_prompt",
    "insight",
    "source_interpretation",
}

# In-memory fast state. Persistent truth is SQLite.
USER_MODES: Dict[int, str] = {}
USER_PENDING_ACTIONS: Dict[int, str] = {}

openai_client: Optional[OpenAI] = None

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("Naz_AI_Bot")
logging.getLogger("httpx").setLevel(logging.WARNING)

# -----------------------------------------------------------------------------
# Menu buttons
# -----------------------------------------------------------------------------

BTN_AI = "🧠 AI"
BTN_CONTENT = "🚀 Контент"
BTN_CONTROL = "📊 Центр управления"
BTN_HELP = "ℹ️ Помощь"
BTN_BACK = "🔙 Назад"

# AI Matrix navigation: WHAT / HOW / WHY
BTN_EXPERT_MENU = "🧠 Expert Mode"
BTN_VOICE_MENU = "🎭 Voice Profile"
BTN_GOAL_MENU = "🎯 Content Goal"

BTN_COPYWRITER = "✍️ Копирайтер"
BTN_MARKETER = "📈 Маркетолог"
BTN_STORYTELLER = "📚 Сторителлер"
BTN_CRITIC = "🧨 Критик"
BTN_STRATEGIST = "♟ Стратег"
BTN_RESEARCHER = "🔎 Исследователь"
BTN_AI_EXPERT = "🧠 AI Эксперт"
BTN_AI_BUSINESS = "💰 AI Бизнес"

BTN_TECH_HOOLIGAN = "🔥 Техно-хулиган"
BTN_STUBBORN_ENGINEER = "🛠 Упрямый инженер"
BTN_ARCHITECT_OF_CHAOS = "🧩 Архитектор хаоса"
BTN_GOOD_BASTARD = "😈 Добрый подонок"
BTN_SMART_SLACKER = "😎 Умный раздолбай"
BTN_CRAFTSMAN = "🧱 Ремесленник"
BTN_BUILDER = "🏗 Builder"
BTN_FINISHER = "🏁 Дожиматель"

BTN_GOAL_GROWTH = "📈 Рост"
BTN_GOAL_ENGAGEMENT = "💬 Вовлечение"
BTN_GOAL_TRUST = "🤝 Доверие"
BTN_GOAL_EDUCATION = "📚 Обучение"
BTN_GOAL_SALES = "💰 Продажи"

BTN_POST = "✍️ Написать пост"
BTN_VIRAL = "🔥 Вирусный пост"
BTN_REELS = "📱 Reels"
BTN_PLAN = "📅 Контент-план"
BTN_HOOKS = "🎯 Заголовки"
BTN_IMAGE = "🖼 Картинка"

BTN_STATS = "📈 Статистика"
BTN_MEMORY = "🧠 Память"
BTN_AUTOPOST = "📢 Автопостинг"
BTN_SETTINGS = "⚙️ Настройки"

# Angle Engine v2.4
BTN_ANGLE_1 = "1️⃣ Угол 1"
BTN_ANGLE_2 = "2️⃣ Угол 2"
BTN_ANGLE_3 = "3️⃣ Угол 3"
BTN_ANGLE_4 = "4️⃣ Угол 4"
BTN_ANGLE_5 = "5️⃣ Угол 5"
BTN_NEW_ANGLES = "🔁 Другие углы"
BTN_WRITE_ANGLE = "✍️ Написать по углу"
BTN_CLEAR_RECENT = "🧹 Очистить recent"

BTN_HELP_CAPABILITIES = "🤖 Что умеет бот"
BTN_HELP_COMMANDS = "📚 Команды"
BTN_HELP_ABOUT = "💬 О проекте"

EXPERT_BUTTON_TO_MODE = {
    BTN_COPYWRITER: "copywriter",
    BTN_MARKETER: "marketer",
    BTN_STORYTELLER: "storyteller",
    BTN_CRITIC: "critic",
    BTN_STRATEGIST: "strategist",
    BTN_RESEARCHER: "researcher",
}

# Legacy aliases remain available through /role ai_expert and /role ai_business.
AI_BUTTON_TO_MODE = EXPERT_BUTTON_TO_MODE

VOICE_BUTTON_TO_PROFILE = {
    BTN_TECH_HOOLIGAN: "tech_hooligan",
    BTN_STUBBORN_ENGINEER: "stubborn_engineer",
    BTN_ARCHITECT_OF_CHAOS: "architect_of_chaos",
    BTN_GOOD_BASTARD: "good_bastard",
    BTN_SMART_SLACKER: "smart_slacker",
    BTN_CRAFTSMAN: "craftsman",
    BTN_BUILDER: "builder",
    BTN_FINISHER: "finisher",
}

GOAL_BUTTON_TO_GOAL = {
    BTN_GOAL_GROWTH: "growth",
    BTN_GOAL_ENGAGEMENT: "engagement",
    BTN_GOAL_TRUST: "trust",
    BTN_GOAL_EDUCATION: "education",
    BTN_GOAL_SALES: "sales",
}

ANGLE_BUTTON_TO_INDEX = {
    BTN_ANGLE_1: 0,
    BTN_ANGLE_2: 1,
    BTN_ANGLE_3: 2,
    BTN_ANGLE_4: 3,
    BTN_ANGLE_5: 4,
}

CONTENT_BUTTON_TO_ACTION = {
    BTN_POST: "post",
    BTN_VIRAL: "viral",
    BTN_REELS: "script",
    BTN_PLAN: "plan",
    BTN_HOOKS: "hooks",
    BTN_IMAGE: "image_only",
}

ACTION_TITLES = {
    "post": "пост",
    "viral": "вирусный пост",
    "script": "сценарий Reels",
    "plan": "контент-план",
    "hooks": "заголовки/хуки",
    "image_only": "картинку",
    "angle_post": "пост по выбранному углу",
    "insight": "Naz Stories insight",
}


def make_keyboard(rows: List[List[str]]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(text) for text in row] for row in rows],
        resize_keyboard=True,
        is_persistent=True,
    )


MAIN_KEYBOARD = make_keyboard([[BTN_AI, BTN_CONTENT], [BTN_CONTROL, BTN_HELP]])
AI_KEYBOARD = make_keyboard([[BTN_EXPERT_MENU], [BTN_VOICE_MENU, BTN_GOAL_MENU], [BTN_BACK]])
EXPERT_KEYBOARD = make_keyboard([
    [BTN_COPYWRITER, BTN_MARKETER],
    [BTN_STORYTELLER, BTN_CRITIC],
    [BTN_STRATEGIST, BTN_RESEARCHER],
    [BTN_BACK],
])
VOICE_KEYBOARD = make_keyboard([
    [BTN_TECH_HOOLIGAN, BTN_STUBBORN_ENGINEER],
    [BTN_ARCHITECT_OF_CHAOS, BTN_GOOD_BASTARD],
    [BTN_SMART_SLACKER, BTN_CRAFTSMAN],
    [BTN_BUILDER, BTN_FINISHER],
    [BTN_BACK],
])
GOAL_KEYBOARD = make_keyboard([
    [BTN_GOAL_GROWTH, BTN_GOAL_ENGAGEMENT],
    [BTN_GOAL_TRUST, BTN_GOAL_EDUCATION],
    [BTN_GOAL_SALES],
    [BTN_BACK],
])
CONTENT_KEYBOARD = make_keyboard([[BTN_POST, BTN_VIRAL], [BTN_REELS, BTN_PLAN], [BTN_HOOKS, BTN_IMAGE], [BTN_BACK]])
CONTROL_KEYBOARD = make_keyboard([[BTN_STATS, BTN_MEMORY], [BTN_AUTOPOST, BTN_SETTINGS], [BTN_BACK]])
ANGLE_KEYBOARD = make_keyboard([
    [BTN_ANGLE_1, BTN_ANGLE_2],
    [BTN_ANGLE_3, BTN_ANGLE_4],
    [BTN_ANGLE_5],
    [BTN_WRITE_ANGLE, BTN_NEW_ANGLES],
    [BTN_CLEAR_RECENT, BTN_BACK],
])
HELP_KEYBOARD = make_keyboard([[BTN_HELP_CAPABILITIES], [BTN_HELP_COMMANDS, BTN_HELP_ABOUT], [BTN_BACK]])

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def is_admin(user_id: int) -> bool:
    return bool(ADMIN_ID and user_id == ADMIN_ID)


def user_display_name(update: Update) -> str:
    user = update.effective_user
    if not user:
        return "друг"
    return user.first_name or user.username or "друг"


def get_user_expert_mode(user_id: int) -> str:
    if user_id in USER_MODES:
        return USER_MODES[user_id]
    state = memory.load_state(user_id)
    mode = state.get("expert_mode") or DEFAULT_EXPERT_MODE
    USER_MODES[user_id] = mode
    return mode


def set_user_expert_mode(user_id: int, mode: str) -> str:
    if mode not in EXPERT_MODES:
        mode = DEFAULT_EXPERT_MODE
    USER_MODES[user_id] = mode
    memory.set_expert_mode(user_id, mode)
    return mode


def set_user_voice_profile(user_id: int, voice: str) -> str:
    if voice not in VOICE_PROFILES:
        voice = DEFAULT_VOICE_PROFILE
    memory.set_voice_profile(user_id, voice)
    return voice


def set_user_content_goal(user_id: int, goal: str) -> str:
    if goal not in GOALS:
        goal = DEFAULT_CONTENT_GOAL
    memory.set_content_goal(user_id, goal)
    return goal


def get_state_text(user_id: int) -> str:
    state = memory.load_state(user_id)
    mode = state.get("expert_mode", DEFAULT_EXPERT_MODE)
    voice = state.get("voice_profile", DEFAULT_VOICE_PROFILE)
    goal = state.get("content_goal", DEFAULT_CONTENT_GOAL)
    mode_data = EXPERT_MODES.get(mode, EXPERT_MODES[DEFAULT_EXPERT_MODE])
    voice_data = VOICE_PROFILES.get(voice, VOICE_PROFILES[DEFAULT_VOICE_PROFILE])
    goal_data = GOALS.get(goal, GOALS[DEFAULT_CONTENT_GOAL])
    recent_topics = state.get("recent_topics") or []
    recent_text = "нет" if not recent_topics else "\n".join(f"• {t}" for t in recent_topics[-5:])
    angles = state.get("suggested_angles") or []
    if angles:
        selected_index = int(state.get("selected_angle_index", 0) or 0)
        angle_lines = []
        for idx, angle in enumerate(angles[:5], start=1):
            if isinstance(angle, dict):
                mark = "→" if idx - 1 == selected_index else "•"
                angle_lines.append(f"{mark} {idx}. {angle.get('emoji', '')} {angle.get('title', 'Угол')}: {angle.get('hook', '')[:110]}")
        angles_text = "\n".join(angle_lines)
    else:
        angles_text = "нет"

    return (
        "🤖 Состояние Naz\n\n"
        f"WHAT / Expert: {mode_data['title']} — {mode_data['short']}\n"
        f"HOW / Voice: {voice_data['title']} — {voice_data['style']}\n"
        f"WHY / Goal: {goal_data['title']}\n"
        f"Mode: {state.get('mode', 'hybrid')}\n"
        f"Content count: {state.get('content_count', 0)}\n"
        f"Quality: {state.get('quality_profile', 'naz_clean_v24')} / {state.get('content_rules_version', 'v2.4')}\n"
        f"Angle Engine: {state.get('angle_engine_version', 'v2.4')}\n"
        f"Память: {'включена' if state.get('memory_enabled') else 'выключена'}\n\n"
        f"Последние темы:\n{recent_text}\n\n"
        f"Углы последнего повтора:\n{angles_text}\n\n"
        "Логика: Controller решает, GPT исполняет."
    )


def extract_topic(update: Update, context: ContextTypes.DEFAULT_TYPE, default: str = "AI, контент и автоматизация") -> str:
    if context.args:
        return " ".join(context.args).strip()
    if update.message and update.message.text:
        parts = update.message.text.split(maxsplit=1)
        if len(parts) == 2:
            return parts[1].strip()
    return default


async def send_typing(update: Update) -> None:
    if update.effective_chat:
        try:
            await update.effective_chat.send_action(ChatAction.TYPING)
        except TelegramError:
            pass


async def reply_long(update: Update, text: str, keyboard: ReplyKeyboardMarkup | None = None) -> None:
    if not update.message:
        return
    chunks = split_telegram_text(text)
    for i, chunk in enumerate(chunks):
        try:
            await update.message.reply_text(
                chunk,
                reply_markup=keyboard if i == len(chunks) - 1 else None,
                disable_web_page_preview=True,
            )
        except (TimedOut, NetworkError) as exc:
            logger.warning("Telegram reply timed out/skipped: %s", exc)
            return
        except TelegramError as exc:
            logger.warning("Telegram reply failed/skipped: %s", exc)
            return


async def send_long_to_chat(bot, chat_id: str | int, text: str) -> None:
    for chunk in split_telegram_text(text):
        for attempt in range(2):
            try:
                await bot.send_message(chat_id=chat_id, text=chunk, disable_web_page_preview=True)
                break
            except (TimedOut, NetworkError):
                if attempt == 1:
                    raise
                await asyncio.sleep(2)


def split_telegram_text(text: str, limit: int = 3900) -> List[str]:
    if len(text) <= limit:
        return [text]

    chunks: List[str] = []
    current = ""
    for paragraph in text.split("\n"):
        candidate = f"{current}\n{paragraph}" if current else paragraph
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            while len(paragraph) > limit:
                chunks.append(paragraph[:limit])
                paragraph = paragraph[limit:]
            current = paragraph
    if current:
        chunks.append(current)
    return chunks


def ensure_openai_client() -> OpenAI:
    global openai_client
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENAI_API_KEY не найден. Добавь ключ OpenRouter в Secrets/.env.")
    if openai_client is None:
        openai_client = OpenAI(
            api_key=OPENROUTER_API_KEY,
            base_url="https://openrouter.ai/api/v1",
            default_headers={
                "HTTP-Referer": os.getenv("OPENROUTER_SITE_URL", "https://replit.com"),
                "X-Title": APP_NAME,
            },
        )
    return openai_client


async def call_gpt(
    messages: List[Dict[str, str]],
    max_tokens: int = MAX_TOKENS,
    temperature: float = TEMPERATURE,
    model: str | None = None,
) -> str:
    """Call OpenRouter using OpenAI-compatible SDK in a thread."""

    def _request() -> str:
        client = ensure_openai_client()
        response = client.chat.completions.create(
            model=model or MODEL_NAME,
            messages=messages,
            max_tokens=max(16, max_tokens),
            temperature=temperature,
        )
        content = response.choices[0].message.content if response.choices else ""
        return (content or "").strip()

    try:
        result = await asyncio.to_thread(_request)
    except Exception as exc:  # noqa: BLE001
        logger.exception("OpenRouter error")
        raise RuntimeError(f"OpenRouter не ответил нормально: {exc}") from exc

    if not result:
        raise RuntimeError("OpenRouter вернул пустой ответ.")
    return result


def task_max_tokens(task: str | None) -> int:
    if not task:
        return MAX_TOKENS
    return min(MAX_TOKENS, TASK_MAX_TOKENS.get(task, MAX_TOKENS))


def task_model(task: str | None) -> str:
    if task in CONTENT_MODEL_TASKS:
        return CONTENT_MODEL_NAME
    return MODEL_NAME


# -----------------------------------------------------------------------------
# Prompted generation
# -----------------------------------------------------------------------------


def build_user_memory_context(user_id: int) -> str:
    try:
        return memory.get_memory_context(user_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Memory context failed: %s", exc)
        return "Память временно недоступна."


async def generate_answer(user_id: int, user_text: str, task: str | None = None, source_topic: str | None = None) -> str:
    """Generate answer through Controller → State → Prompt Builder → GPT."""
    state = memory.load_state(user_id)
    control = naz_controller.controller(user_text, state, task=task, source_topic=source_topic)

    if control.get("blocked"):
        memory.save_state(user_id, control["state"])
        return control.get("message") or "⚠️ Controller остановил задачу."

    controlled_state = control["state"]
    expert_mode = controlled_state.get("expert_mode", DEFAULT_EXPERT_MODE)
    history = memory.get_history(user_id, limit=20) if task is None else []
    memory_context = build_user_memory_context(user_id)

    messages = build_messages(
        state=controlled_state,
        expert_mode=expert_mode,
        user_text=control["gpt_input"],
        memory_context=memory_context,
        history=history,
        task=task,
    )

    result = await call_gpt(messages, max_tokens=task_max_tokens(task), model=task_model(task))

    updated_state = naz_controller.update_memory_after_output(
        controlled_state,
        topic=control.get("topic", source_topic or user_text),
        output=result,
        task=task,
    )
    memory.save_state(user_id, updated_state)
    return result


def is_warning_response(text: str) -> bool:
    return (text or "").lstrip().startswith("⚠️")


async def generate_content(
    user_id: int,
    topic: str,
    task: str,
    *,
    save_generated: bool = True,
    extra_instruction: str = "",
) -> str:
    task_title = ACTION_TITLES.get(task, task)
    user_text = (
        f"Тема: {topic}\n\n"
        f"Сделай {task_title}. Стиль Naz: путь через бардак, баги, систему и дожим. "
        "Текст должен быть чистым: без служебных заголовков вроде ### Хук, без выдуманной статистики, "
        "без противоречий и без успешного успеха."
    )
    if extra_instruction:
        user_text += f"\n\nДополнительная режиссура:\n{extra_instruction.strip()}"
    user_text += (
        "\n\nHARD LENGTH LIMIT: keep the final answer compact for Telegram. "
        "For post/viral/angle_post use 700-1100 characters unless the user explicitly asks longer. "
        "No long lists, no essay mode, no repeated endings."
    )
    result = await generate_answer(user_id, user_text, task=task, source_topic=topic)
    if save_generated and not is_warning_response(result):
        memory.save_generated_post(
            user_id=user_id,
            expert_mode=get_user_expert_mode(user_id),
            task=task,
            topic=topic,
            content=result,
            image_count=0,
            published_to_channel=False,
        )
    return result


def read_naz_stories() -> str:
    try:
        return NAZ_STORIES_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read %s: %s", NAZ_STORIES_FILE, exc)
        return ""


def split_story_blocks(text: str) -> List[str]:
    paragraphs = [part.strip() for part in text.split("\n\n") if part.strip()]
    blocks: List[str] = []
    current: List[str] = []
    current_len = 0

    for paragraph in paragraphs:
        if paragraph.startswith("```"):
            continue
        clean = paragraph.strip()
        if not clean:
            continue
        if current and current_len + len(clean) > 2200:
            blocks.append("\n\n".join(current))
            current = []
            current_len = 0
        current.append(clean)
        current_len += len(clean)

    if current:
        blocks.append("\n\n".join(current))

    return [block for block in blocks if len(block) > 250]


def pick_story_excerpt(query: str = "") -> str:
    blocks = split_story_blocks(read_naz_stories())
    if not blocks:
        return ""

    words = [word.lower() for word in query.split() if len(word) > 3]
    if words:
        scored = []
        for block in blocks:
            lowered = block.lower()
            score = sum(lowered.count(word) for word in words)
            if score:
                scored.append((score, block))
        if scored:
            scored.sort(key=lambda item: item[0], reverse=True)
            return random.choice([block for _, block in scored[:4]])

    return random.choice(blocks)


async def generate_story_insight(user_id: int, topic_hint: str = "", *, save_generated: bool = True) -> str:
    excerpt = pick_story_excerpt(topic_hint)
    if not excerpt:
        return "⚠️ Не нашёл naz_stories.md. Закинь файл в папку проекта или задай NAZ_STORIES_FILE в .env."

    topic = topic_hint or "инсайт из личного опыта запуска Naz_AI_Bot"
    user_text = (
        f"Тема/фокус: {topic}\n\n"
        f"Сырьё из naz_stories.md:\n{excerpt[:2400]}\n\n"
        "Сделай не пересказ, а отдельный инсайт в стиле рубрики Prompt Or Die. "
        "Пиши как вывод из опыта: конкретно, живо, инженерно, без дневникового тона."
    )
    result = await generate_answer(user_id, user_text, task="insight", source_topic=f"Naz Stories | {topic}")

    if save_generated and not is_warning_response(result):
        memory.save_generated_post(
            user_id=user_id,
            expert_mode=get_user_expert_mode(user_id),
            task="insight",
            topic=topic,
            content=result,
            image_count=0,
            published_to_channel=False,
        )
    return result


SOURCE_RUBRICS = [
    "AI-находка дня",
    "Разбор чужого кейса",
    "Инженерный дневник",
    "Prompt or Die",
    "Ботостройка",
    "Ошибка недели",
    "GitHub/релизы",
]


def read_json_file(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read JSON %s: %s", path, exc)
        return default


def write_json_file(path: Path, data) -> None:
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not write JSON %s: %s", path, exc)


def load_monitored_sources() -> List[Dict[str, str]]:
    raw = read_json_file(MONITORED_SOURCES_FILE, [])
    if not isinstance(raw, list):
        return []

    sources: List[Dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url", "")).strip()
        if not url:
            continue
        enabled = item.get("enabled", True)
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() not in {"0", "false", "no", "off"}
        sources.append(
            {
                "name": str(item.get("name") or url).strip(),
                "type": str(item.get("type") or "rss").strip(),
                "url": url,
                "rubric": str(item.get("rubric") or "AI-находка дня").strip(),
                "enabled": bool(enabled),
            }
        )
    return sources


def load_seen_sources() -> Dict[str, str]:
    raw = read_json_file(SOURCE_SEEN_FILE, {})
    return raw if isinstance(raw, dict) else {}


def source_item_key(item: Dict[str, str]) -> str:
    base = item.get("link") or f"{item.get('source_url', '')}|{item.get('title', '')}"
    return hashlib.sha256(base.encode("utf-8", errors="ignore")).hexdigest()[:24]


def mark_source_seen(item: Dict[str, str]) -> None:
    seen = load_seen_sources()
    seen[source_item_key(item)] = item.get("title", "")[:240]
    if len(seen) > 500:
        seen = dict(list(seen.items())[-500:])
    write_json_file(SOURCE_SEEN_FILE, seen)


def clean_feed_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    value = unescape(value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:1200]


def tag_name(tag: str) -> str:
    return tag.rsplit("}", maxsplit=1)[-1].lower()


def first_text(node: ET.Element, names: List[str]) -> str:
    wanted = {name.split(":")[-1].lower() for name in names}
    for child in list(node):
        if tag_name(child.tag) in wanted and child.text:
            return clean_feed_text(child.text)
    return ""


def first_link(node: ET.Element, names: List[str]) -> str:
    wanted = {name.split(":")[-1].lower() for name in names}
    for found in list(node):
        if tag_name(found.tag) not in wanted:
            continue
        href = found.attrib.get("href")
        if href:
            return href.strip()
        if found.text:
            return clean_feed_text(found.text)
    return ""


def parse_feed_entries(xml_text: str, source: Dict[str, str]) -> List[Dict[str, str]]:
    root = ET.fromstring(xml_text)
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "media": "http://search.yahoo.com/mrss/",
        "dc": "http://purl.org/dc/elements/1.1/",
    }
    entries = root.findall(".//item") or root.findall(".//atom:entry", ns)
    parsed: List[Dict[str, str]] = []

    for entry in entries[:20]:
        title = first_text(entry, ["title", "atom:title"])
        link = first_link(entry, ["link", "atom:link"])
        published = first_text(entry, ["pubDate", "published", "updated", "atom:published", "atom:updated"])
        summary = first_text(entry, ["description", "summary", "content", "atom:summary", "atom:content"])
        if not title:
            continue
        parsed.append(
            {
                "source_name": source["name"],
                "source_type": source["type"],
                "source_url": source["url"],
                "rubric": source["rubric"],
                "title": title,
                "link": link or source["url"],
                "published": published,
                "summary": summary,
            }
        )
    return parsed


async def fetch_source_entries(source: Dict[str, str]) -> List[Dict[str, str]]:
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
            response = await client.get(source["url"], headers={"User-Agent": "Naz_AI_Bot/2.4"})
        response.raise_for_status()
        return parse_feed_entries(response.text, source)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Source fetch failed | %s | %s", source.get("name"), exc)
        return []


async def get_source_candidates(query: str = "", *, include_seen: bool = False) -> List[Dict[str, str]]:
    sources = [source for source in load_monitored_sources() if source.get("enabled")]
    if query:
        needle = query.lower()
        sources = [
            source
            for source in sources
            if needle in source.get("name", "").lower()
            or needle in source.get("type", "").lower()
            or needle in source.get("rubric", "").lower()
        ] or sources

    seen = load_seen_sources()
    candidates: List[Dict[str, str]] = []
    for source in sources:
        for item in await fetch_source_entries(source):
            if include_seen or source_item_key(item) not in seen:
                candidates.append(item)
    return candidates


def format_source_item(item: Dict[str, str]) -> str:
    return (
        f"{item.get('rubric', 'AI-находка дня')} | {item.get('source_name', '')}\n"
        f"{item.get('title', '')}\n"
        f"{item.get('published', '')}\n"
        f"{item.get('link', '')}"
    ).strip()


async def generate_source_interpretation(user_id: int, item: Dict[str, str], *, save_generated: bool = True) -> str:
    source_context = (
        f"Рубрика: {item.get('rubric', 'AI-находка дня')}\n"
        f"Тип источника: {item.get('source_type', 'rss')}\n"
        f"Источник: {item.get('source_name', '')}\n"
        f"Заголовок: {item.get('title', '')}\n"
        f"Дата: {item.get('published', '')}\n"
        f"Ссылка: {item.get('link', '')}\n\n"
        f"Краткое описание/summary:\n{item.get('summary', '')[:1200]}"
    )
    messages = build_messages(
        state=memory.load_state(user_id),
        expert_mode=get_user_expert_mode(user_id),
        user_text=(
            f"{source_context}\n\n"
            "Сделай интерпретацию Naz. Не репость. Объясни, что здесь важно для AI-ботов, контента, автоматизации или инженерной сборки."
        ),
        memory_context=build_user_memory_context(user_id),
        history=[],
        task="source_interpretation",
    )
    result = await call_gpt(
        messages,
        max_tokens=TASK_MAX_TOKENS["source_interpretation"],
        model=CONTENT_MODEL_NAME,
    )

    if save_generated:
        memory.save_generated_post(
            user_id=user_id,
            expert_mode=get_user_expert_mode(user_id),
            task="source_interpretation",
            topic=item.get("title", ""),
            content=result,
            image_count=0,
            published_to_channel=False,
        )
    return result


def is_angle_engine_message(text: str) -> bool:
    return text.startswith("⚠️ Эта тема") or "Разворачиваем тему через новый угол" in text


def format_current_angles(user_id: int) -> str:
    state = memory.load_state(user_id)
    topic = state.get("last_blocked_topic") or state.get("last_input") or "AI, контент и автоматизация"
    angles = state.get("suggested_angles") or []
    if not angles:
        state = naz_controller.refresh_angles(state, topic)
        memory.save_state(user_id, state)
        angles = state.get("suggested_angles") or []
    return naz_controller.format_angles_message(topic, angles)


async def generate_selected_angle_content(user_id: int) -> str:
    state = memory.load_state(user_id)
    topic = state.get("last_blocked_topic") or state.get("last_input") or "AI, контент и автоматизация"
    angle = naz_controller.get_selected_angle(state)
    if not angle:
        state = naz_controller.refresh_angles(state, topic)
        memory.save_state(user_id, state)
        angle = naz_controller.get_selected_angle(state)
    if not angle:
        return "⚠️ Нет выбранного угла. Нажми 🔁 Другие углы или выбери 1️⃣–5️⃣."

    user_text = naz_controller.build_angle_user_input(topic, angle)
    source_topic = f"{topic} | angle:{angle.get('kind', 'angle')} | {angle.get('title', 'Угол')}"
    result = await generate_answer(user_id, user_text, task="angle_post", source_topic=source_topic)

    if not result.startswith("⚠️"):
        memory.save_generated_post(
            user_id=user_id,
            expert_mode=get_user_expert_mode(user_id),
            task="angle_post",
            topic=source_topic,
            content=result,
            image_count=0,
            published_to_channel=False,
        )
    return result


async def build_image_prompt(user_id: int, topic: str, post_text: str, variant: int = 1) -> str:
    messages = [
        {
            "role": "system",
            "content": (
                "You create image-generation prompts for Telegram channel posts. "
                "Return only one concise English prompt, 60-110 words. "
                "Describe a concrete scene from the post, not abstract AI symbolism. "
                "No text, letters, logos, watermarks, UI captions, charts, or interface screenshots."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Topic: {topic}\n"
                f"Variant: {variant}\n\n"
                f"Post text:\n{post_text[:1800]}\n\n"
                "Extract the main scene, conflict, mood, subject, setting, and visual metaphor. "
                "Style: cinematic editorial, realistic lighting, high detail, expressive but not stock-photo."
            ),
        },
    ]
    prompt = await call_gpt(
        messages,
        max_tokens=TASK_MAX_TOKENS["image_prompt"],
        temperature=0.55,
        model=CONTENT_MODEL_NAME,
    )
    return prompt.strip().strip('"')

    user_text = (
        f"Тема: {topic}\n\n"
        f"Текст поста:\n{post_text[:2500]}\n\n"
        f"Сделай prompt для изображения. Вариант #{variant}.\n\n"
        "Сначала мысленно выдели из поста: главную сцену, конфликт, эмоцию, объект/героя, место и визуальную метафору. "
        "Верни только готовый английский image prompt, без пояснений. "
        "Картинка должна быть про конкретную сцену поста, а не абстрактно про AI. "
        "Не добавляй текст, буквы, логотипы, интерфейсные надписи или watermark."
    )
    prompt = await generate_answer(user_id, user_text, task="image_prompt")
    return prompt.strip().strip('"')


# -----------------------------------------------------------------------------
# Image generation
# -----------------------------------------------------------------------------


async def generate_image_bytes(prompt: str, variant: int = 1) -> Optional[bytes]:
    """Generate one image through Hugging Face.

    Returns bytes on success, None on failure. The bot must not crash because
    image generation is an external dependency.
    """
    if not HF_TOKEN:
        logger.warning("HF_TOKEN is empty. Image generation skipped.")
        return await fallback_image_bytes() if ALLOW_IMAGE_FALLBACK else None

    endpoint = f"https://router.huggingface.co/hf-inference/models/{HF_MODEL}"
    headers = {
        "Authorization": f"Bearer {HF_TOKEN}",
        "Accept": "image/png",
        "Content-Type": "application/json",
    }
    payload = {
        "inputs": f"{prompt}, variation {variant}",
        "parameters": {
            "num_inference_steps": 4,
            "guidance_scale": 0.0,
            "width": 1024,
            "height": 1024,
        },
        "options": {"wait_for_model": True},
    }

    try:
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(endpoint, headers=headers, json=payload)

        content_type = response.headers.get("content-type", "")
        if response.status_code == 200 and content_type.startswith("image/"):
            return response.content

        logger.error("HF image error %s | %s", response.status_code, response.text[:500])
    except Exception as exc:  # noqa: BLE001
        logger.exception("HF image request failed: %s", exc)

    return await fallback_image_bytes() if ALLOW_IMAGE_FALLBACK else None


async def fallback_image_bytes() -> Optional[bytes]:
    """Fallback picture to prove Telegram delivery path still works."""
    try:
        seed = random.randint(1000, 999999)
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.get(f"https://picsum.photos/seed/naz-{seed}/1024/1024")
        if response.status_code == 200 and response.content:
            return response.content
    except Exception as exc:  # noqa: BLE001
        logger.warning("Fallback image failed: %s", exc)
    return None


async def generate_images_for_post(user_id: int, topic: str, post_text: str, count: int = 1) -> Tuple[List[bytes], str]:
    count = max(1, min(int(count), 2))
    image_prompt = await build_image_prompt(user_id, topic, post_text, variant=1)
    images: List[bytes] = []

    # Последовательно, чтобы не ловить лишние rate limits на HF.
    for variant in range(1, count + 1):
        img = await generate_image_bytes(image_prompt, variant=variant)
        if img:
            images.append(img)

    return images, image_prompt


async def generate_two_images_for_post(user_id: int, topic: str, post_text: str) -> Tuple[List[bytes], str]:
    return await generate_images_for_post(user_id, topic, post_text, count=2)


async def send_post_with_images(bot, chat_id: int | str, post_text: str, images: List[bytes]) -> None:
    """Send post. If images fail, text still goes out."""
    if not images:
        await send_long_to_chat(bot, chat_id, post_text)
        return

    caption_limit = 1000
    caption = post_text if len(post_text) <= caption_limit else "🖼 Иллюстрации к посту"

    try:
        if len(images) >= 2:
            media = []
            for idx, img in enumerate(images[:2], start=1):
                bio = BytesIO(img)
                bio.name = f"naz_image_{idx}.png"
                media.append(InputMediaPhoto(media=bio, caption=caption if idx == 1 else None))
            await bot.send_media_group(chat_id=chat_id, media=media)
        else:
            bio = BytesIO(images[0])
            bio.name = "naz_image.png"
            await bot.send_photo(chat_id=chat_id, photo=bio, caption=caption)

        if len(post_text) > caption_limit:
            await send_long_to_chat(bot, chat_id, post_text)
    except (TelegramError, BadRequest) as exc:
        logger.exception("Telegram image send failed: %s", exc)
        await send_long_to_chat(bot, chat_id, post_text)


# -----------------------------------------------------------------------------
# Command handlers
# -----------------------------------------------------------------------------


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    user_id = update.effective_user.id
    memory.load_state(user_id)
    name = user_display_name(update)
    text = (
        f"🤖 Naz AI\n\n"
        f"{name}, я твой AI-помощник для контента, нейросетей и автоматизации.\n\n"
        "Выбери раздел:\n\n"
        "🧠 AI — режимы экспертов\n"
        "🚀 Контент — посты, Reels, планы, картинки\n"
        "📊 Центр управления — память, статистика, автопостинг\n"
        "ℹ️ Помощь — команды и описание проекта"
    )
    await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text("Главное меню Naz:", reply_markup=MAIN_KEYBOARD)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(help_commands_text(), reply_markup=HELP_KEYBOARD)


async def state_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user:
        return
    await reply_long(update, get_state_text(update.effective_user.id), MAIN_KEYBOARD)


async def roles_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply_long(update, format_roles(), AI_KEYBOARD)


async def role_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user:
        return
    if not context.args:
        await reply_long(update, "Напиши режим после команды. Например:\n/role marketer\n\n" + format_roles(), AI_KEYBOARD)
        return
    mode = context.args[0].strip().lower()
    if mode not in EXPERT_MODES:
        await reply_long(update, f"Такого режима нет: {mode}\n\n" + format_roles(), AI_KEYBOARD)
        return
    set_user_expert_mode(update.effective_user.id, mode)
    data = EXPERT_MODES[mode]
    await reply_long(update, f"✅ Режим включён: {data['title']}\n{data['short']}", MAIN_KEYBOARD)


async def voice_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user:
        return
    if not context.args:
        items = "\n".join(f"{key} — {data['title']}: {data['style']}" for key, data in VOICE_PROFILES.items())
        await reply_long(update, "🎭 Voice profiles:\n\n" + items + "\n\nПример:\n/voice tech_hooligan", MAIN_KEYBOARD)
        return
    voice = context.args[0].strip().lower()
    if voice not in VOICE_PROFILES:
        items = "\n".join(VOICE_PROFILES.keys())
        await reply_long(update, f"Такого голоса нет: {voice}\n\nДоступно:\n{items}", MAIN_KEYBOARD)
        return
    set_user_voice_profile(update.effective_user.id, voice)
    data = VOICE_PROFILES[voice]
    await reply_long(update, f"✅ Голос включён: {data['title']}\n{data['style']}", MAIN_KEYBOARD)


async def goal_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user:
        return
    if not context.args:
        items = "\n".join(f"{key} — {data['title']}" for key, data in GOALS.items())
        await reply_long(update, "🎯 Content goals:\n\n" + items + "\n\nПример:\n/goal engagement", MAIN_KEYBOARD)
        return
    goal = context.args[0].strip().lower()
    if goal not in GOALS:
        items = "\n".join(GOALS.keys())
        await reply_long(update, f"Такой цели нет: {goal}\n\nДоступно:\n{items}", MAIN_KEYBOARD)
        return
    set_user_content_goal(update.effective_user.id, goal)
    data = GOALS[goal]
    await reply_long(update, f"✅ Цель включена: {data['title']}\n{data['prompt']}", MAIN_KEYBOARD)


async def memory_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user:
        return
    if ADMIN_ID and not is_admin(update.effective_user.id):
        await reply_long(update, "🔒 Память и управление доступны только админу.", MAIN_KEYBOARD)
        return
    await reply_long(update, memory.format_memory(update.effective_user.id), CONTROL_KEYBOARD)


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user:
        return
    memory.clear_user_memory(update.effective_user.id)
    await reply_long(update, "🧹 Готово. История диалога и заметки памяти очищены для тебя.", MAIN_KEYBOARD)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user:
        return
    if not is_admin(update.effective_user.id):
        await reply_long(update, "🔒 Статистика доступна только админу.", MAIN_KEYBOARD)
        return
    await reply_long(update, memory.get_stats(), CONTROL_KEYBOARD)


async def content_command(update: Update, context: ContextTypes.DEFAULT_TYPE, task: str) -> None:
    if not update.effective_user or not update.message:
        return
    user_id = update.effective_user.id
    if ADMIN_ONLY_CONTENT and not is_admin(user_id):
        await update.message.reply_text("🔒 Генерация контента сейчас доступна только админу.", reply_markup=MAIN_KEYBOARD)
        return

    topic = extract_topic(update, context, default="AI, контент и автоматизация")
    await send_typing(update)

    try:
        result = await generate_content(user_id, topic, task)
        await reply_long(update, result, ANGLE_KEYBOARD if is_angle_engine_message(result) else CONTENT_KEYBOARD)
    except Exception as exc:  # noqa: BLE001
        logger.exception("content_command failed")
        await reply_long(update, f"⚠️ Не смог сгенерировать материал. Причина: {exc}", CONTENT_KEYBOARD)


async def post_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await content_command(update, context, "post")


async def viral_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await content_command(update, context, "viral")


async def script_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await content_command(update, context, "script")


async def plan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await content_command(update, context, "plan")


async def hooks_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await content_command(update, context, "hooks")


async def insight_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    user_id = update.effective_user.id
    if ADMIN_ONLY_CONTENT and not is_admin(user_id):
        await update.message.reply_text("🔒 Рубрика инсайтов сейчас доступна только админу.", reply_markup=MAIN_KEYBOARD)
        return

    topic = extract_topic(update, context, default="")
    await send_typing(update)

    try:
        result = await generate_story_insight(user_id, topic)
        await reply_long(update, result, CONTENT_KEYBOARD)
    except Exception as exc:  # noqa: BLE001
        logger.exception("insight failed")
        await reply_long(update, f"⚠️ Инсайт не собрался. Причина: {exc}", CONTENT_KEYBOARD)


async def publish_insight_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if not is_admin(update.effective_user.id):
        await reply_long(update, "🔒 Публикация в канал доступна только админу.", MAIN_KEYBOARD)
        return
    if not CHANNEL_ID:
        await reply_long(update, "⚠️ CHANNEL_ID не задан в .env/Replit Secrets.", MAIN_KEYBOARD)
        return

    topic = extract_topic(update, context, default="")
    await reply_long(update, "🚀 Собираю рубрику из naz_stories.md и публикую в канал.")

    try:
        post_text = await generate_story_insight(update.effective_user.id, topic, save_generated=False)
        if is_warning_response(post_text):
            await reply_long(update, post_text, MAIN_KEYBOARD)
            return

        image_topic = topic or "инженерный инсайт из опыта запуска AI-бота"
        images, _ = await generate_images_for_post(update.effective_user.id, image_topic, post_text, count=CHANNEL_IMAGE_COUNT)
        if REQUIRE_IMAGES_FOR_CHANNEL_POSTS and not images:
            await reply_long(update, "⚠️ Инсайт готов, но картинка не собралась. В канал без изображения не публикую.", MAIN_KEYBOARD)
            return

        await send_post_with_images(context.bot, CHANNEL_ID, post_text, images)
        memory.save_generated_post(
            user_id=update.effective_user.id,
            expert_mode=get_user_expert_mode(update.effective_user.id),
            task="publish_insight",
            topic=topic or "Naz Stories",
            content=post_text,
            image_count=len(images),
            published_to_channel=True,
        )
        await reply_long(update, "✅ Инсайт опубликован в канал.", MAIN_KEYBOARD)
    except Exception as exc:  # noqa: BLE001
        logger.exception("publish_insight failed")
        await reply_long(update, f"⚠️ Не смог опубликовать инсайт. Причина: {exc}", MAIN_KEYBOARD)


async def sources_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user:
        return
    if not is_admin(update.effective_user.id):
        await reply_long(update, "🔒 Источники доступны только админу.", MAIN_KEYBOARD)
        return

    sources = load_monitored_sources()
    if not sources:
        await reply_long(
            update,
            "⚠️ Источники пока не заданы. Создай monitored_sources.json рядом с ботом. Пример есть в monitored_sources.example.json.",
            CONTROL_KEYBOARD,
        )
        return

    lines = [
        "🛰 Источники мониторинга",
        f"Файл: {MONITORED_SOURCES_FILE}",
        f"Автомониторинг: {'включён' if SOURCE_MONITOR_ENABLED else 'выключен'}",
        f"Время: {SOURCE_MONITOR_TIMES}",
        "",
    ]
    for idx, source in enumerate(sources, start=1):
        status = "on" if source.get("enabled") else "off"
        lines.append(f"{idx}. [{status}] {source['rubric']} | {source['type']} | {source['name']}")
    lines.append("\nРубрики: " + ", ".join(SOURCE_RUBRICS))
    await reply_long(update, "\n".join(lines), CONTROL_KEYBOARD)


async def scan_sources_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if not is_admin(update.effective_user.id):
        await reply_long(update, "🔒 Скан источников доступен только админу.", MAIN_KEYBOARD)
        return

    query = extract_topic(update, context, default="")
    await send_typing(update)

    try:
        candidates = await get_source_candidates(query, include_seen=False)
        if not candidates:
            await reply_long(update, "⚠️ Свежих источников не нашёл. Проверь /sources или добавь RSS/Atom URL.", CONTROL_KEYBOARD)
            return

        item = random.choice(candidates[:8])
        post_text = await generate_source_interpretation(update.effective_user.id, item, save_generated=False)
        await reply_long(update, f"{post_text}\n\n---\nЧерновик из:\n{format_source_item(item)}", CONTENT_KEYBOARD)
    except Exception as exc:  # noqa: BLE001
        logger.exception("scan_sources failed")
        await reply_long(update, f"⚠️ Не смог собрать интерпретацию источника. Причина: {exc}", CONTROL_KEYBOARD)


async def publish_source_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if not is_admin(update.effective_user.id):
        await reply_long(update, "🔒 Публикация источников доступна только админу.", MAIN_KEYBOARD)
        return
    if not CHANNEL_ID:
        await reply_long(update, "⚠️ CHANNEL_ID не задан в .env/Replit Secrets.", MAIN_KEYBOARD)
        return

    query = extract_topic(update, context, default="")
    await reply_long(update, "🚀 Ищу свежий источник, делаю интерпретацию Naz и готовлю публикацию.")

    try:
        candidates = await get_source_candidates(query, include_seen=False)
        if not candidates:
            await reply_long(update, "⚠️ Свежих источников не нашёл. Ничего не публикую.", MAIN_KEYBOARD)
            return

        item = random.choice(candidates[:8])
        post_text = await generate_source_interpretation(update.effective_user.id, item, save_generated=False)
        images, _ = await generate_images_for_post(update.effective_user.id, item.get("title", ""), post_text, count=CHANNEL_IMAGE_COUNT)
        if REQUIRE_IMAGES_FOR_CHANNEL_POSTS and not images:
            await reply_long(update, "⚠️ Пост по источнику готов, но картинка не собралась. В канал без изображения не публикую.", MAIN_KEYBOARD)
            return

        await send_post_with_images(context.bot, CHANNEL_ID, post_text, images)
        mark_source_seen(item)
        memory.save_generated_post(
            user_id=update.effective_user.id,
            expert_mode=get_user_expert_mode(update.effective_user.id),
            task="publish_source",
            topic=item.get("title", ""),
            content=post_text,
            image_count=len(images),
            published_to_channel=True,
        )
        await reply_long(update, f"✅ Опубликовано по источнику:\n{format_source_item(item)}", MAIN_KEYBOARD)
    except Exception as exc:  # noqa: BLE001
        logger.exception("publish_source failed")
        await reply_long(update, f"⚠️ Не смог опубликовать источник. Причина: {exc}", MAIN_KEYBOARD)


async def imagepost_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    user_id = update.effective_user.id
    if ADMIN_ONLY_CONTENT and not is_admin(user_id):
        await update.message.reply_text("🔒 Генерация image-post сейчас доступна только админу.", reply_markup=MAIN_KEYBOARD)
        return

    topic = extract_topic(update, context, default="AI, контент и автоматизация")
    await send_typing(update)

    try:
        post_text = await generate_content(user_id, topic, "post", save_generated=False)
        if is_warning_response(post_text):
            await reply_long(update, post_text, ANGLE_KEYBOARD if is_angle_engine_message(post_text) else CONTENT_KEYBOARD)
            return

        await update.effective_chat.send_action(ChatAction.UPLOAD_PHOTO)
        images, image_prompt = await generate_two_images_for_post(user_id, topic, post_text)
        await send_post_with_images(context.bot, update.effective_chat.id, post_text, images)

        memory.save_generated_post(
            user_id=user_id,
            expert_mode=get_user_expert_mode(user_id),
            task="imagepost",
            topic=topic,
            content=post_text,
            image_count=len(images),
            published_to_channel=False,
        )
        logger.info("Image prompt: %s", image_prompt)
    except Exception as exc:  # noqa: BLE001
        logger.exception("imagepost failed")
        await reply_long(update, f"⚠️ Image-post не собрался. Причина: {exc}", CONTENT_KEYBOARD)


async def image_only_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    user_id = update.effective_user.id
    if ADMIN_ONLY_CONTENT and not is_admin(user_id):
        await update.message.reply_text("🔒 Генерация картинок сейчас доступна только админу.", reply_markup=MAIN_KEYBOARD)
        return

    topic = extract_topic(update, context, default="AI content automation cinematic poster")
    await update.effective_chat.send_action(ChatAction.UPLOAD_PHOTO)
    prompt = (
        f"{topic}, cinematic editorial, modern AI content system, realistic lighting, "
        "high detail, no text, no letters, no logo, no watermark"
    )
    image = await generate_image_bytes(prompt)
    if image:
        bio = BytesIO(image)
        bio.name = "naz_image.png"
        await update.message.reply_photo(photo=bio, caption="🖼 Готово", reply_markup=CONTENT_KEYBOARD)
    else:
        await update.message.reply_text("⚠️ Картинка не сгенерировалась. Проверь HF_TOKEN и доступ к модели.", reply_markup=CONTENT_KEYBOARD)


async def publish_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate image post and publish it to Telegram channel."""
    if not update.effective_user or not update.message:
        return
    if not is_admin(update.effective_user.id):
        await reply_long(update, "🔒 Публикация в канал доступна только админу.", MAIN_KEYBOARD)
        return
    if not CHANNEL_ID:
        await reply_long(update, "⚠️ CHANNEL_ID не задан в .env/Replit Secrets.", MAIN_KEYBOARD)
        return

    topic = extract_topic(update, context, default="AI, контент и автоматизация")
    await reply_long(update, f"🚀 Собираю публикацию в канал по теме:\n{topic}")

    try:
        post_text = await generate_content(update.effective_user.id, topic, "post", save_generated=False)
        if is_warning_response(post_text):
            await reply_long(update, post_text, ANGLE_KEYBOARD if is_angle_engine_message(post_text) else MAIN_KEYBOARD)
            return

        images, _ = await generate_images_for_post(update.effective_user.id, topic, post_text, count=CHANNEL_IMAGE_COUNT)
        if REQUIRE_IMAGES_FOR_CHANNEL_POSTS and not images:
            await reply_long(update, "⚠️ Пост готов, но картинки не собрались. В канал без изображения не публикую.", MAIN_KEYBOARD)
            return
        await send_post_with_images(context.bot, CHANNEL_ID, post_text, images)
        memory.save_generated_post(
            user_id=update.effective_user.id,
            expert_mode=get_user_expert_mode(update.effective_user.id),
            task="publish",
            topic=topic,
            content=post_text,
            image_count=len(images),
            published_to_channel=True,
        )
        await reply_long(update, "✅ Опубликовано в канал.", MAIN_KEYBOARD)
    except Exception as exc:  # noqa: BLE001
        logger.exception("publish failed")
        await reply_long(update, f"⚠️ Не смог опубликовать. Причина: {exc}", MAIN_KEYBOARD)


# -----------------------------------------------------------------------------
# Button handling
# -----------------------------------------------------------------------------


async def handle_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    if not update.message or not update.effective_user:
        return False

    user_id = update.effective_user.id

    if text == BTN_BACK:
        USER_PENDING_ACTIONS.pop(user_id, None)
        await update.message.reply_text("Главное меню:", reply_markup=MAIN_KEYBOARD)
        return True

    if text == BTN_AI:
        state = memory.load_state(user_id)
        expert = EXPERT_MODES.get(state.get("expert_mode"), EXPERT_MODES[DEFAULT_EXPERT_MODE])
        voice = VOICE_PROFILES.get(state.get("voice_profile"), VOICE_PROFILES[DEFAULT_VOICE_PROFILE])
        goal = GOALS.get(state.get("content_goal"), GOALS[DEFAULT_CONTENT_GOAL])
        await update.message.reply_text(
            "🧠 AI Matrix Naz\n\n"
            "WHAT — что делать: Expert Mode\n"
            "HOW — как говорить: Voice Profile\n"
            "WHY — зачем говорить: Content Goal\n\n"
            f"Сейчас:\n{expert['title']} + {voice['title']} + {goal['title']}",
            reply_markup=AI_KEYBOARD,
        )
        return True

    if text == BTN_EXPERT_MENU:
        await update.message.reply_text("🧠 Выбери WHAT: что должен делать Naz.", reply_markup=EXPERT_KEYBOARD)
        return True

    if text == BTN_VOICE_MENU:
        await update.message.reply_text("🎭 Выбери HOW: каким голосом Naz должен говорить.", reply_markup=VOICE_KEYBOARD)
        return True

    if text == BTN_GOAL_MENU:
        await update.message.reply_text("🎯 Выбери WHY: зачем мы делаем этот контент.", reply_markup=GOAL_KEYBOARD)
        return True

    if text == BTN_CONTENT:
        if ADMIN_ONLY_CONTENT and not is_admin(user_id):
            await update.message.reply_text("🔒 Раздел контента сейчас доступен только админу.", reply_markup=MAIN_KEYBOARD)
            return True
        await update.message.reply_text("🚀 Выбери, что собрать:", reply_markup=CONTENT_KEYBOARD)
        return True

    if text == BTN_CONTROL:
        if not is_admin(user_id):
            await update.message.reply_text("🔒 Центр управления доступен только админу.", reply_markup=MAIN_KEYBOARD)
            return True
        await update.message.reply_text("📊 Центр управления Naz:", reply_markup=CONTROL_KEYBOARD)
        return True

    if text == BTN_HELP:
        await update.message.reply_text("ℹ️ Раздел помощи:", reply_markup=HELP_KEYBOARD)
        return True

    if text in EXPERT_BUTTON_TO_MODE:
        mode = set_user_expert_mode(user_id, EXPERT_BUTTON_TO_MODE[text])
        data = EXPERT_MODES[mode]
        state = memory.load_state(user_id)
        voice = VOICE_PROFILES.get(state.get("voice_profile"), VOICE_PROFILES[DEFAULT_VOICE_PROFILE])
        goal = GOALS.get(state.get("content_goal"), GOALS[DEFAULT_CONTENT_GOAL])
        await update.message.reply_text(
            f"✅ Expert Mode включён: {data['title']}\n\n"
            f"WHAT: {data['short']}\n"
            f"HOW: {voice['title']}\n"
            f"WHY: {goal['title']}\n\n"
            "Матрица сохранена. Следующие сообщения идут через неё.",
            reply_markup=AI_KEYBOARD,
        )
        return True

    if text in VOICE_BUTTON_TO_PROFILE:
        voice_key = set_user_voice_profile(user_id, VOICE_BUTTON_TO_PROFILE[text])
        voice = VOICE_PROFILES[voice_key]
        state = memory.load_state(user_id)
        expert = EXPERT_MODES.get(state.get("expert_mode"), EXPERT_MODES[DEFAULT_EXPERT_MODE])
        goal = GOALS.get(state.get("content_goal"), GOALS[DEFAULT_CONTENT_GOAL])
        await update.message.reply_text(
            f"✅ Voice Profile включён: {voice['title']}\n\n"
            f"WHAT: {expert['title']}\n"
            f"HOW: {voice['style']}\n"
            f"WHY: {goal['title']}\n\n"
            "Теперь Naz не просто отвечает. Он говорит выбранным голосом.",
            reply_markup=AI_KEYBOARD,
        )
        return True

    if text in GOAL_BUTTON_TO_GOAL:
        goal_key = set_user_content_goal(user_id, GOAL_BUTTON_TO_GOAL[text])
        goal = GOALS[goal_key]
        state = memory.load_state(user_id)
        expert = EXPERT_MODES.get(state.get("expert_mode"), EXPERT_MODES[DEFAULT_EXPERT_MODE])
        voice = VOICE_PROFILES.get(state.get("voice_profile"), VOICE_PROFILES[DEFAULT_VOICE_PROFILE])
        await update.message.reply_text(
            f"✅ Content Goal включён: {goal['title']}\n\n"
            f"WHAT: {expert['title']}\n"
            f"HOW: {voice['title']}\n"
            f"WHY: {goal['prompt']}\n\n"
            "Теперь Naz понимает не только что писать, но и зачем.",
            reply_markup=AI_KEYBOARD,
        )
        return True

    if text in ANGLE_BUTTON_TO_INDEX:
        state = memory.load_state(user_id)
        state = naz_controller.select_angle(state, ANGLE_BUTTON_TO_INDEX[text])
        memory.save_state(user_id, state)
        angle = naz_controller.get_selected_angle(state)
        if angle:
            await update.message.reply_text(
                f"✅ Угол выбран: {angle.get('emoji', '')} {angle.get('title', 'Угол')}\n\n"
                f"{angle.get('hook', '')}\n\n"
                "Теперь нажми ✍️ Написать по углу.",
                reply_markup=ANGLE_KEYBOARD,
            )
        else:
            await update.message.reply_text("⚠️ Углы не найдены. Нажми 🔁 Другие углы.", reply_markup=ANGLE_KEYBOARD)
        return True

    if text == BTN_NEW_ANGLES:
        state = memory.load_state(user_id)
        topic = state.get("last_blocked_topic") or state.get("last_input") or "AI, контент и автоматизация"
        state = naz_controller.refresh_angles(state, topic)
        memory.save_state(user_id, state)
        await update.message.reply_text(naz_controller.format_angles_message(topic, state.get("suggested_angles", [])), reply_markup=ANGLE_KEYBOARD)
        return True

    if text == BTN_WRITE_ANGLE:
        await send_typing(update)
        try:
            result = await generate_selected_angle_content(user_id)
            await reply_long(update, result, ANGLE_KEYBOARD)
        except Exception as exc:  # noqa: BLE001
            logger.exception("angle generation failed")
            await update.message.reply_text(f"⚠️ Не получилось написать по углу. Причина: {exc}", reply_markup=ANGLE_KEYBOARD)
        return True

    if text == BTN_CLEAR_RECENT:
        memory.clear_recent_topics(user_id)
        await update.message.reply_text(
            "🧹 Recent topics очищены.\n\nТеперь можно снова писать по старой теме, но лучше всё равно искать новый угол.",
            reply_markup=MAIN_KEYBOARD,
        )
        return True

    if text in CONTENT_BUTTON_TO_ACTION:
        if ADMIN_ONLY_CONTENT and not is_admin(user_id):
            await update.message.reply_text("🔒 Генерация контента сейчас доступна только админу.", reply_markup=MAIN_KEYBOARD)
            return True
        action = CONTENT_BUTTON_TO_ACTION[text]
        USER_PENDING_ACTIONS[user_id] = action
        title = ACTION_TITLES.get(action, "материал")
        await update.message.reply_text(
            f"Ок. Напиши тему, и я соберу {title}.\n\n"
            "Пример: AI-бот для Telegram-канала про нейросети",
            reply_markup=CONTENT_KEYBOARD,
        )
        return True

    if text == BTN_MEMORY:
        if not is_admin(user_id):
            await update.message.reply_text("🔒 Память доступна только админу.", reply_markup=MAIN_KEYBOARD)
            return True
        await update.message.reply_text(memory.format_memory(user_id), reply_markup=CONTROL_KEYBOARD)
        return True

    if text == BTN_STATS:
        if not is_admin(user_id):
            await update.message.reply_text("🔒 Статистика доступна только админу.", reply_markup=MAIN_KEYBOARD)
            return True
        await update.message.reply_text(memory.get_stats(), reply_markup=CONTROL_KEYBOARD)
        return True

    if text == BTN_AUTOPOST:
        if not is_admin(user_id):
            await update.message.reply_text("🔒 Автопостинг доступен только админу.", reply_markup=MAIN_KEYBOARD)
            return True
        msg = (
            "📢 Автопостинг\n\n"
            f"Статус: {'включён' if AUTOPOST_ENABLED else 'выключен'}\n"
            f"Время: {AUTOPOST_TIMES or 'не задано'}, timezone: {BOT_TIMEZONE}\n"
            f"Форматы: {', '.join(get_autopost_tasks())}\n"
            f"Канал: {CHANNEL_ID or 'не задан'}\n\n"
            "Расписание задаётся через .env: AUTOPOST_TIMES=10:00,20:00 и AUTOPOST_TASKS=post,viral."
        )
        await update.message.reply_text(msg, reply_markup=CONTROL_KEYBOARD)
        return True

    if text == BTN_SETTINGS:
        if not is_admin(user_id):
            await update.message.reply_text("🔒 Настройки доступны только админу.", reply_markup=MAIN_KEYBOARD)
            return True
        state = memory.load_state(user_id)
        await update.message.reply_text(
            "⚙️ Настройки Naz\n\n"
            f"Expert: {state.get('expert_mode')}\n"
            f"Voice: {state.get('voice_profile')}\n"
            f"Goal: {state.get('content_goal')}\n\n"
            "Команды настройки:\n"
            "/role copywriter\n"
            "/voice tech_hooligan\n"
            "/goal engagement",
            reply_markup=CONTROL_KEYBOARD,
        )
        return True

    if text == BTN_HELP_CAPABILITIES:
        await update.message.reply_text(help_capabilities_text(), reply_markup=HELP_KEYBOARD)
        return True

    if text == BTN_HELP_COMMANDS:
        await update.message.reply_text(help_commands_text(), reply_markup=HELP_KEYBOARD)
        return True

    if text == BTN_HELP_ABOUT:
        await update.message.reply_text(help_about_text(), reply_markup=HELP_KEYBOARD)
        return True

    return False


async def handle_pending_action(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    if not update.message or not update.effective_user:
        return False
    user_id = update.effective_user.id
    action = USER_PENDING_ACTIONS.get(user_id)
    if not action:
        return False

    USER_PENDING_ACTIONS.pop(user_id, None)
    topic = text.strip()
    if not topic:
        await update.message.reply_text("Тема пустая. Напиши тему ещё раз.", reply_markup=CONTENT_KEYBOARD)
        return True

    await send_typing(update)
    try:
        if action == "image_only":
            prompt = (
                f"{topic}, cinematic editorial, modern AI content system, realistic lighting, "
                "high detail, no text, no letters, no logo, no watermark"
            )
            await update.effective_chat.send_action(ChatAction.UPLOAD_PHOTO)
            image = await generate_image_bytes(prompt)
            if image:
                bio = BytesIO(image)
                bio.name = "naz_image.png"
                await update.message.reply_photo(photo=bio, caption="🖼 Готово", reply_markup=CONTENT_KEYBOARD)
            else:
                await update.message.reply_text("⚠️ Картинка не сгенерировалась. Проверь HF_TOKEN.", reply_markup=CONTENT_KEYBOARD)
            return True

        result = await generate_content(user_id, topic, action)
        await reply_long(update, result, ANGLE_KEYBOARD if is_angle_engine_message(result) else CONTENT_KEYBOARD)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.exception("pending action failed")
        await update.message.reply_text(f"⚠️ Не получилось выполнить задачу. Причина: {exc}", reply_markup=CONTENT_KEYBOARD)
        return True


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user or not update.message.text:
        return

    user_id = update.effective_user.id
    text = update.message.text.strip()

    if await handle_menu_button(update, context, text):
        return

    if await handle_pending_action(update, context, text):
        return

    # Default chat through persistent expert mode and SQLite history.
    await send_typing(update)
    try:
        answer = await generate_answer(user_id, text)
        memory.save_message(user_id, "user", text)
        memory.save_message(user_id, "assistant", answer)
        await reply_long(update, answer, ANGLE_KEYBOARD if is_angle_engine_message(answer) else MAIN_KEYBOARD)
    except Exception as exc:  # noqa: BLE001
        logger.exception("handle_message failed")
        await reply_long(update, f"⚠️ Naz споткнулся. Причина: {exc}", MAIN_KEYBOARD)


# -----------------------------------------------------------------------------
# Autoposting
# -----------------------------------------------------------------------------


AUTOPOST_TOPICS = [
    "Как AI-боты меняют Telegram-каналы и контент",
    "Почему нейросети — это не магия, а рабочий инструмент",
    "Как предпринимателю начать использовать AI без хаоса",
    "Контент-система вместо случайных постов",
    "AI-агенты: что реально можно автоматизировать уже сейчас",
    "Ошибки при запуске первого AI-бота",
    "Почему AI-проект ломается не из-за модели, а из-за процесса",
    "Как не утонуть в промптах, когда нужен рабочий результат",
    "Что должно быть у Telegram-бота, чтобы он не был игрушкой",
    "Как память меняет поведение AI-ассистента в диалоге",
    "Почему автопостинг без редакторской логики быстро становится шумом",
    "Как проверить AI-систему перед запуском на VPS",
    "Где заканчивается генератор текста и начинается контент-система",
    "Почему картинки к постам должны быть частью сценария, а не украшением",
    "Как маленький баг в интеграции ломает весь пользовательский опыт",
    "Что предпринимателю важно понимать перед внедрением AI-бота",
]


AUTOPOST_PROFILES: List[Dict[str, str]] = [
    {
        "name": "Техно-хулиган",
        "voice": "дерзкий инженерный голос, живо, с ощущением сборки на ходу",
        "angle": "конфликт: хотели магию, получили систему, которую надо довести",
        "format": "короткий Telegram-пост с сильным хуком и спокойным выводом",
    },
    {
        "name": "Упрямый инженер",
        "voice": "спокойно, точно, без шума, как человек, который чинит до результата",
        "angle": "диагностика: где именно ломается процесс и какой следующий шаг",
        "format": "практический пост с 3-5 наблюдениями без списочной канцелярщины",
    },
    {
        "name": "Архитектор хаоса",
        "voice": "иронично и системно, через бардак к структуре",
        "angle": "система: почему хаос перестаёт быть хаосом, когда его разложили по слоям",
        "format": "мини-история с конфликтом, поворотом и выводом",
    },
    {
        "name": "Добрый подонок",
        "voice": "жёстко, но полезно; неприятная правда без токсичности",
        "angle": "антипаттерн: что люди делают не так и почему это дорого обходится",
        "format": "провокационный пост без кликбейта и фальшивых цифр",
    },
    {
        "name": "Ремесленник",
        "voice": "собранно, аккуратно, с уважением к качеству",
        "angle": "качество: что отличает рабочий инструмент от красивого демо",
        "format": "плотный редакторский пост без лишней пыли",
    },
    {
        "name": "Дожиматель",
        "voice": "сфокусированно и прямо, меньше украшений, больше результата",
        "angle": "дожим: что сделать последним шагом, чтобы оно реально заработало",
        "format": "энергичный пост с финалом про доведение до результата",
    },
]


def get_autopost_tasks() -> List[str]:
    allowed = {"post", "viral"}
    tasks = [item.strip() for item in AUTOPOST_TASKS.split(",") if item.strip()]
    tasks = [task for task in tasks if task in allowed]
    return tasks or ["post"]


def format_autopost_profile(profile: Dict[str, str]) -> str:
    return (
        f"Профиль выпуска: {profile['name']}.\n"
        f"Голос: {profile['voice']}.\n"
        f"Угол: {profile['angle']}.\n"
        f"Формат: {profile['format']}.\n"
        "Не повторяй структуру предыдущих постов. Меняй заход, ритм, сцену и финальный вывод. "
        "Пост должен ощущаться как отдельный выпуск рубрики Naz, а не как шаблон."
    )


async def auto_post_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not CHANNEL_ID:
        logger.warning("AUTOPOST skipped: CHANNEL_ID empty")
        return

    admin_user_id = ADMIN_ID or 0
    if admin_user_id:
        memory.load_state(admin_user_id)
        if get_user_expert_mode(admin_user_id) not in EXPERT_MODES:
            set_user_expert_mode(admin_user_id, DEFAULT_EXPERT_MODE)

    topics = AUTOPOST_TOPICS[:]
    random.shuffle(topics)
    use_story_insight = bool(read_naz_stories()) and random.random() < AUTOPOST_INSIGHT_CHANCE
    task = "insight" if use_story_insight else random.choice(get_autopost_tasks())
    profile = random.choice(AUTOPOST_PROFILES)
    topic = "инсайт из опыта запуска Naz_AI_Bot" if use_story_insight else topics[0]
    logger.info("AUTOPOST started | task=%s | profile=%s", task, profile["name"])

    try:
        post_text = ""
        if use_story_insight:
            logger.info("AUTOPOST story insight from %s", NAZ_STORIES_FILE)
            post_text = await generate_story_insight(admin_user_id, topic, save_generated=False)
            if is_warning_response(post_text):
                logger.warning("AUTOPOST story insight skipped by smart filter")
                return
        else:
            for candidate_topic in topics:
                topic = candidate_topic
                logger.info("AUTOPOST candidate topic=%s", topic)
                post_text = await generate_content(
                    admin_user_id,
                    topic,
                    task,
                    save_generated=False,
                    extra_instruction=format_autopost_profile(profile),
                )
                if not is_warning_response(post_text):
                    break
            else:
                logger.warning("AUTOPOST skipped: all topics blocked by smart filter")
                return

        images, image_prompt = await generate_images_for_post(admin_user_id, topic, post_text, count=CHANNEL_IMAGE_COUNT)
        if REQUIRE_IMAGES_FOR_CHANNEL_POSTS and not images:
            logger.warning("AUTOPOST skipped: images are required but none were generated")
            return
        await send_post_with_images(context.bot, CHANNEL_ID, post_text, images)
        memory.save_generated_post(
            user_id=admin_user_id,
            expert_mode=get_user_expert_mode(admin_user_id),
            task=f"autopost:{task}:{profile['name']}",
            topic=f"{topic} | {profile['name']}",
            content=post_text,
            image_count=len(images),
            published_to_channel=True,
        )
        logger.info("AUTOPOST done | images=%s | prompt=%s", len(images), image_prompt)
    except Exception as exc:  # noqa: BLE001
        logger.exception("AUTOPOST failed: %s", exc)
        # Последний уровень защиты: бот не должен падать из-за автопоста.


async def source_monitor_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not CHANNEL_ID:
        logger.warning("SOURCE_MONITOR skipped: CHANNEL_ID empty")
        return
    if not load_monitored_sources():
        logger.warning("SOURCE_MONITOR skipped: no monitored sources")
        return

    admin_user_id = ADMIN_ID or 0
    logger.info("SOURCE_MONITOR started")

    try:
        candidates = await get_source_candidates(include_seen=False)
        if not candidates:
            logger.info("SOURCE_MONITOR skipped: no fresh candidates")
            return

        item = random.choice(candidates[:10])
        post_text = await generate_source_interpretation(admin_user_id, item, save_generated=False)
        images, image_prompt = await generate_images_for_post(admin_user_id, item.get("title", ""), post_text, count=CHANNEL_IMAGE_COUNT)
        if REQUIRE_IMAGES_FOR_CHANNEL_POSTS and not images:
            logger.warning("SOURCE_MONITOR skipped: images are required but none were generated")
            return

        await send_post_with_images(context.bot, CHANNEL_ID, post_text, images)
        mark_source_seen(item)
        memory.save_generated_post(
            user_id=admin_user_id,
            expert_mode=get_user_expert_mode(admin_user_id),
            task=f"source_monitor:{item.get('rubric', '')}",
            topic=item.get("title", ""),
            content=post_text,
            image_count=len(images),
            published_to_channel=True,
        )
        logger.info("SOURCE_MONITOR done | %s | images=%s | prompt=%s", item.get("title", ""), len(images), image_prompt)
    except Exception as exc:  # noqa: BLE001
        logger.exception("SOURCE_MONITOR failed: %s", exc)


def setup_autoposting(application: Application) -> None:
    if not AUTOPOST_ENABLED:
        logger.info("Autoposting disabled")
        return
    if not application.job_queue:
        logger.warning("JobQueue is not available. Install python-telegram-bot[job-queue].")
        return

    tz = ZoneInfo(BOT_TIMEZONE)
    scheduled = []
    for raw_time in AUTOPOST_TIMES.split(","):
        raw_time = raw_time.strip()
        if not raw_time:
            continue
        try:
            hour_text, minute_text = raw_time.split(":", maxsplit=1)
            hour = int(hour_text)
            minute = int(minute_text)
            if hour not in range(24) or minute not in range(60):
                raise ValueError
        except ValueError:
            logger.warning("Invalid AUTOPOST_TIMES value skipped: %s", raw_time)
            continue

        application.job_queue.run_daily(
            auto_post_job,
            time=time(hour=hour, minute=minute, tzinfo=tz),
            name=f"naz_autopost_{hour:02d}_{minute:02d}",
        )
        scheduled.append(f"{hour:02d}:{minute:02d}")

    if not scheduled:
        logger.warning("Autoposting enabled, but AUTOPOST_TIMES has no valid times")
        return

    logger.info("Autoposting scheduled at %s %s", ", ".join(scheduled), BOT_TIMEZONE)


def setup_source_monitoring(application: Application) -> None:
    if not SOURCE_MONITOR_ENABLED:
        logger.info("Source monitoring disabled")
        return
    if not application.job_queue:
        logger.warning("JobQueue is not available. Install python-telegram-bot[job-queue].")
        return

    tz = ZoneInfo(BOT_TIMEZONE)
    scheduled = []
    for raw_time in SOURCE_MONITOR_TIMES.split(","):
        raw_time = raw_time.strip()
        if not raw_time:
            continue
        try:
            hour_text, minute_text = raw_time.split(":", maxsplit=1)
            hour = int(hour_text)
            minute = int(minute_text)
            if hour not in range(24) or minute not in range(60):
                raise ValueError
        except ValueError:
            logger.warning("Invalid SOURCE_MONITOR_TIMES value skipped: %s", raw_time)
            continue

        application.job_queue.run_daily(
            source_monitor_job,
            time(hour=hour, minute=minute, tzinfo=tz),
            name=f"naz_source_monitor_{hour:02d}_{minute:02d}",
        )
        scheduled.append(f"{hour:02d}:{minute:02d}")

    if scheduled:
        logger.info("Source monitoring scheduled at %s %s", ", ".join(scheduled), BOT_TIMEZONE)
    else:
        logger.warning("Source monitoring enabled, but SOURCE_MONITOR_TIMES has no valid times")


# -----------------------------------------------------------------------------
# Help texts
# -----------------------------------------------------------------------------


def help_capabilities_text() -> str:
    return (
        "🤖 Что умеет Naz_AI_Bot v2\n\n"
        "🧠 Режимы экспертов:\n"
        "• Копирайтер\n"
        "• Маркетолог\n"
        "• AI Эксперт\n"
        "• AI Бизнес\n\n"
        "🚀 Контент:\n"
        "• посты\n"
        "• вирусные посты\n"
        "• сценарии Reels\n"
        "• контент-планы\n"
        "• хуки/заголовки\n"
        "• картинки через Hugging Face\n\n"
        "📊 Управление:\n"
        "• память SQLite\n"
        "• статистика\n"
        "• автопостинг в канал\n"
        "• защита админ-функций\n"
        "• Angle Engine: новые углы вместо повторов"
    )


def help_commands_text() -> str:
    return (
        "📚 Команды Naz\n\n"
        "/start — главное меню\n"
        "/menu — открыть меню\n"
        "/help — помощь\n"
        "/state — текущий режим\n"
        "/roles — список ролей\n"
        "/role marketer — выбрать expert mode\n/voice tech_hooligan — выбрать голос Naz\n/goal engagement — выбрать цель контента\n"
        "/memory — память\n"
        "/clear — очистить свою память\n\n"
        "Контент:\n"
        "/post тема — обычный пост\n"
        "/viral тема — вирусный пост\n"
        "/script тема — сценарий Reels\n"
        "/plan тема — контент-план\n"
        "/hooks тема — заголовки\n"
        "/imagepost тема — пост + 2 картинки\n"
        "/image тема — одна картинка\n"
        "/publish тема — сгенерировать и отправить в канал\n\n"
        "Источники:\n"
        "/sources — список источников\n"
        "/scan_sources рубрика — черновик интерпретации\n"
        "/publish_source рубрика — опубликовать интерпретацию источника\n\n"
        "Админ:\n"
        "/stats — статистика"
    )


def help_about_text() -> str:
    return (
        "💬 О проекте\n\n"
        "Naz — это не учитель успешного успеха.\n"
        "Naz показывает путь через бардак: кривой код, ошибки, сломанные интеграции и дожим результата.\n\n"
        "Архитектура v2.1:\n"
        "Controller → State → Smart Filter → GPT → Image → Posting → Memory Update\n\n"
        "Матрица личности:\n"
        "WHAT = Expert Mode\n"
        "HOW = Voice Profile\n"
        "WHY = Content Goal\n"
        "MEMORY = пользовательский и системный контекст\n\n"
        "GPT больше не управляет ботом. GPT исполняет решение Controller-а.\n"
        "VOID держим отдельно и не смешиваем с Naz_AI_Bot."
    )


# -----------------------------------------------------------------------------
# Error handler and bootstrap
# -----------------------------------------------------------------------------


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error: %s", context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ Внутренняя ошибка. Я записал её в лог и не упал.")
        except TelegramError:
            pass


def validate_config() -> None:
    missing = []
    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not OPENROUTER_API_KEY:
        missing.append("OPENAI_API_KEY")
    if missing:
        raise RuntimeError("Не хватает переменных окружения: " + ", ".join(missing))

    if not ADMIN_ID:
        logger.warning("ADMIN_ID is empty/0. Admin protection will block admin-only actions.")
    if not CHANNEL_ID:
        logger.warning("CHANNEL_ID is empty. Channel publishing/autoposting will be skipped.")
    if not HF_TOKEN:
        logger.warning("HF_TOKEN is empty. Image generation will use fallback only if enabled.")


def build_application() -> Application:
    validate_config()
    memory.init_db()

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .connect_timeout(20)
        .read_timeout(60)
        .write_timeout(60)
        .pool_timeout(20)
        .build()
    )

    # Core commands
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("state", state_command))
    application.add_handler(CommandHandler("roles", roles_command))
    application.add_handler(CommandHandler("role", role_command))
    application.add_handler(CommandHandler("voice", voice_command))
    application.add_handler(CommandHandler("goal", goal_command))
    application.add_handler(CommandHandler("memory", memory_command))
    application.add_handler(CommandHandler("clear", clear_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("sources", sources_command))
    application.add_handler(CommandHandler("scan_sources", scan_sources_command))
    application.add_handler(CommandHandler("publish_source", publish_source_command))

    # Content commands
    application.add_handler(CommandHandler("post", post_command))
    application.add_handler(CommandHandler("viral", viral_command))
    application.add_handler(CommandHandler("script", script_command))
    application.add_handler(CommandHandler("plan", plan_command))
    application.add_handler(CommandHandler("hooks", hooks_command))
    application.add_handler(CommandHandler("insight", insight_command))
    application.add_handler(CommandHandler("imagepost", imagepost_command))
    application.add_handler(CommandHandler("image", image_only_command))
    application.add_handler(CommandHandler("publish", publish_command))
    application.add_handler(CommandHandler("publish_insight", publish_insight_command))

    # Text router must be after commands.
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(error_handler)

    setup_autoposting(application)
    setup_source_monitoring(application)
    return application


def main() -> None:
    logger.info("Starting Naz_AI_Bot v2.4")

    # Python 3.14+ on Windows may not create a default event loop automatically.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    application = build_application()
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
