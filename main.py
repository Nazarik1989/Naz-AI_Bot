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
from datetime import datetime, time
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
AGENT_CONTENT_INBOX = Path(os.getenv("AGENT_CONTENT_INBOX", "content_inbox/agent_content").strip())

AUTOPOST_ENABLED = env_bool("AUTOPOST_ENABLED", True)
AUTOPOST_TIMES = os.getenv("AUTOPOST_TIMES", "10:00,20:00").strip()
AUTOPOST_TASKS = os.getenv("AUTOPOST_TASKS", "post,viral").strip()
AUTOPOST_INSIGHT_CHANCE = max(0.0, min(env_float("AUTOPOST_INSIGHT_CHANCE", 0.35), 1.0))
SOURCE_MONITOR_ENABLED = env_bool("SOURCE_MONITOR_ENABLED", False)
SOURCE_MONITOR_TIMES = os.getenv("SOURCE_MONITOR_TIMES", "12:00,18:00").strip()
AGENT_CONTENT_SYNC_ENABLED = env_bool("AGENT_CONTENT_SYNC_ENABLED", True)
AGENT_CONTENT_SYNC_TIMES = os.getenv("AGENT_CONTENT_SYNC_TIMES", "23:57").strip()
AGENT_CONTENT_AUTO_PUBLISH = env_bool("AGENT_CONTENT_AUTO_PUBLISH", False)
AGENT_CONTENT_RANDOM_SYNC = env_bool("AGENT_CONTENT_RANDOM_SYNC", True)
AGENT_CONTENT_REUSE_SEEN = env_bool("AGENT_CONTENT_REUSE_SEEN", True)
AGENT_CONTENT_STATE_FILE = Path(os.getenv("AGENT_CONTENT_STATE_FILE", ".agent_content_seen.json").strip())
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
    "agent_content_editor": 950,
    "void_crosspost": 760,
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
    "agent_content_editor",
    "void_crosspost",
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


def build_chat_messages(user_text: str, memory_context: str) -> List[Dict[str, str]]:
    system = (
        "Ты Naz_AI_Bot, живой AI-помощник Назара. Это обычный диалог, не пост для канала. "
        "Отвечай коротко: обычно 2-6 предложений. Если пользователь просит подробно, можно больше. "
        "Не превращай простые реплики в эссе, манифест или контент-пост. "
        "Стиль: дружелюбно, честно, чуть иронично, по делу. "
        "Если уместно, задай один короткий уточняющий вопрос. "
        "Не раскрывай приватные данные, токены, ключи, внутренние URL и технические секреты."
    )
    if memory_context:
        system += "\n\nКраткий контекст памяти, если он реально помогает ответу:\n" + memory_context[:1200]
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_text},
    ]


async def generate_answer(user_id: int, user_text: str, task: str | None = None, source_topic: str | None = None) -> str:
    """Generate answer through Controller → State → Prompt Builder → GPT."""
    state = memory.load_state(user_id)
    control = naz_controller.controller(user_text, state, task=task, source_topic=source_topic)

    if control.get("blocked"):
        memory.save_state(user_id, control["state"])
        return control.get("message") or "⚠️ Controller остановил задачу."

    controlled_state = control["state"]
    expert_mode = controlled_state.get("expert_mode", DEFAULT_EXPERT_MODE)
    memory_context = build_user_memory_context(user_id)

    if task is None:
        messages = build_chat_messages(user_text, memory_context)
    else:
        messages = build_messages(
            state=controlled_state,
            expert_mode=expert_mode,
            user_text=control["gpt_input"],
            memory_context=memory_context,
            history=[],
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
    story_signature = re.sub(r"\s+", " ", excerpt).strip()[:180]
    result = await generate_answer(
        user_id,
        user_text,
        task="insight",
        source_topic=f"Naz Stories | {topic} | {story_signature}",
    )

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


SOURCE_EDITORIAL_FRAMES: List[Dict[str, str]] = [
    {
        "name": "Что произошло",
        "angle": "объяснить событие простыми словами и вытащить практический смысл",
        "format": "короткий разбор: факт, почему важно, что делать дальше",
    },
    {
        "name": "Где тут ловушка",
        "angle": "показать неочевидный риск, ограничение или место, где люди ошибутся",
        "format": "пост-предупреждение без паники и без морализаторства",
    },
    {
        "name": "Как применить",
        "angle": "перевести новость в действие для AI-ботов, контента или автоматизации",
        "format": "прикладная заметка с одним ясным выводом",
    },
    {
        "name": "Серьёзный разбор",
        "angle": "разобрать, как это меняет систему, рынок или рабочий процесс",
        "format": "спокойный экспертный пост, меньше шуток, больше причинно-следственных связей",
    },
    {
        "name": "Мемас с пользой",
        "angle": "начать с живой иронии, но закончить полезным инженерным выводом",
        "format": "короткий пост с человеческим заходом, без клоунады",
    },
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
    frame = random.choice(SOURCE_EDITORIAL_FRAMES)
    source_context = (
        f"Рубрика: {item.get('rubric', 'AI-находка дня')}\n"
        f"Редакторский формат: {frame['name']}\n"
        f"Угол: {frame['angle']}\n"
        f"Форма: {frame['format']}\n"
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
            "Сделай интерпретацию Naz. Не репость. Не пересказывай источник механически. "
            "Дай свой угол: что произошло, почему это важно, где ловушка или как это применить. "
            "Тон и структура должны соответствовать редакторскому формату выше."
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
            task=f"source_interpretation:{frame['name']}",
            topic=f"{item.get('title', '')} | {frame['name']}",
            content=result,
            image_count=0,
            published_to_channel=False,
        )
    return result


SENSITIVE_PATTERNS = [
    ("telegram bot token", re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{25,}\b")),
    ("openai/openrouter key", re.compile(r"\b(?:sk-or-v1|sk-proj|sk)-[A-Za-z0-9_-]{16,}\b")),
    ("huggingface token", re.compile(r"\bhf_[A-Za-z0-9]{20,}\b")),
    ("secret env name", re.compile(r"\b(?:TOKEN|SECRET|PASSWORD|API_KEY|PRIVATE_KEY|CLIENT_SECRET)\b", re.I)),
    ("ssh/ip detail", re.compile(r"\b(?:ssh\s+\S+@|(?:\d{1,3}\.){3}\d{1,3})\b", re.I)),
    ("internal url", re.compile(r"\b(?:https?://)?(?:localhost|127\.0\.0\.1|0\.0\.0\.0|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+)[^\s]*", re.I)),
]


def detect_content_risks(text: str) -> List[str]:
    found: List[str] = []
    for label, pattern in SENSITIVE_PATTERNS:
        if pattern.search(text):
            found.append(label)
    return found


def redact_sensitive_text(text: str) -> str:
    redacted = text
    for _, pattern in SENSITIVE_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def latest_agent_content_dir(date_hint: str = "") -> Optional[Path]:
    if date_hint:
        candidate = AGENT_CONTENT_INBOX / date_hint.strip()
        if candidate.exists() and candidate.is_dir():
            return candidate
        return None
    if not AGENT_CONTENT_INBOX.exists():
        return None
    dirs = [path for path in AGENT_CONTENT_INBOX.iterdir() if path.is_dir()]
    return random.choice(dirs) if dirs else None


def list_agent_content_dirs() -> List[Path]:
    if not AGENT_CONTENT_INBOX.exists():
        return []
    return [path for path in AGENT_CONTENT_INBOX.iterdir() if path.is_dir()]


def choose_agent_content_date_for_sync() -> str:
    dirs = list_agent_content_dirs()
    if not dirs:
        return current_bot_date()

    seen = load_agent_content_seen()
    changed = [path for path in dirs if seen.get(path.name) != agent_manifest_hash(path)]
    pool = changed or dirs
    return random.choice(pool).name


def read_limited_text(path: Path, limit: int = 6000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit].strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read agent content file %s: %s", path, exc)
        return ""


def load_agent_manifest(day_dir: Path) -> Dict:
    manifest = read_json_file(day_dir / "manifest.json", {})
    return manifest if isinstance(manifest, dict) else {}


def agent_manifest_hash(day_dir: Path) -> str:
    manifest_path = day_dir / "manifest.json"
    try:
        raw = manifest_path.read_bytes()
    except FileNotFoundError:
        raw = day_dir.name.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def agent_file(day_dir: Path, names: List[str]) -> Optional[Path]:
    for name in names:
        path = day_dir / name
        if path.exists() and path.is_file():
            return path
    return None


def collect_agent_materials(date_hint: str = "", focus: str = "") -> Tuple[str, List[str], str]:
    day_dir = latest_agent_content_dir(date_hint)
    if not day_dir:
        return "", ["agent inbox missing"], ""

    manifest = load_agent_manifest(day_dir)
    date_text = str(manifest.get("date") or day_dir.name)
    files = manifest.get("files") if isinstance(manifest.get("files"), list) else []

    today_pick = agent_file(day_dir, [f"{date_text}-today-pick.md", *[str(item) for item in files if "today-pick" in str(item)]])
    live = agent_file(day_dir, ["live-chronicle.md", *[str(item) for item in files if "live-chronicle" in str(item)]])
    pack = agent_file(day_dir, [f"{date_text}-content-pack.md", *[str(item) for item in files if str(item).endswith("content-pack.md")]])
    notes = agent_file(day_dir, [f"{date_text}.md", *[str(item) for item in files if str(item).endswith(".md") and str(item) == f"{date_text}.md"]])
    pack_json = agent_file(day_dir, [f"{date_text}-content-pack.json", *[str(item) for item in files if str(item).endswith(".json") and "content-pack" in str(item)]])
    codex_files = [
        day_dir / str(item)
        for item in files
        if str(item).endswith(".md") and "-codex-" in str(item) and (day_dir / str(item)).is_file()
    ][:3]

    parts = [
        ("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2)[:2500]),
    ]
    for label, path, limit in [
        ("today-pick.md", today_pick, 7000),
        ("live-chronicle.md", live, 3500),
        ("content-pack.md", pack, 5000),
        ("manual-notes.md", notes, 2500),
        ("content-pack.json", pack_json, 3500),
    ]:
        if path:
            parts.append((label, read_limited_text(path, limit)))
    for codex_file in codex_files:
        parts.append((f"codex-memory:{codex_file.name}", read_limited_text(codex_file, 3000)))

    raw = "\n\n".join(f"### {label}\n{text}" for label, text in parts if text)
    risks = detect_content_risks(raw)
    safe_raw = redact_sensitive_text(raw)
    context = (
        f"Дата: {date_text}\n"
        f"Папка: {day_dir.name}\n"
        f"Фокус пользователя: {focus or 'не задан'}\n\n"
        f"{safe_raw[:14000]}"
    )
    return context, risks, date_text


AGENT_CONTENT_LENSES = [
    {
        "name": "системный инсайт",
        "instruction": "Покажи, какой элемент системы держит результат: память, контроль, проверка, доставка, контекст, порядок.",
    },
    {
        "name": "цена мелочи",
        "instruction": "Возьми маленькую настройку, привычку или проверку и покажи, почему без неё проект начинает ехать боком.",
    },
    {
        "name": "ошибка как симптом",
        "instruction": "Не просто расскажи про баг, а покажи, какую слабость процесса он вскрыл.",
    },
    {
        "name": "перевод с инженерного",
        "instruction": "Переведи технический эпизод на язык пользы: что стало проще, надёжнее, понятнее или спокойнее.",
    },
    {
        "name": "путь разработчика",
        "instruction": "Покажи раннюю или незаметную часть пути: как человек учится строить не игрушку, а рабочий контур.",
    },
    {
        "name": "контент из рутины",
        "instruction": "Покажи, как обычный рабочий диалог превращается в материал, если вытащить из него конфликт и вывод.",
    },
]


AGENT_CONTENT_TONES = [
    {
        "name": "спокойный инженер",
        "instruction": "Точно, собранно, без шума. Сила в ясности и наблюдении.",
    },
    {
        "name": "ироничный Naz",
        "instruction": "Живо, с лёгкой усмешкой над рабочим абсурдом, но без превращения поста в шутку ради шутки.",
    },
    {
        "name": "серьёзный разбор",
        "instruction": "Глубже обычного: причина, последствие, влияние на проект, вывод для тех, кто строит систему.",
    },
    {
        "name": "ремесленная философия",
        "instruction": "Немного философии о внимании, качестве и доведении, но всегда через конкретную деталь.",
    },
    {
        "name": "мемас с пользой",
        "instruction": "Легко, узнаваемо, можно смешно. Но финал обязан давать практический смысл.",
    },
]


AGENT_CONTENT_FORMS = [
    {
        "name": "неочевидный урок",
        "instruction": "Начни с вывода, потом покажи эпизод как доказательство.",
    },
    {
        "name": "маленькая история",
        "instruction": "Короткий сюжет: ситуация, сбой/находка, что стало понятно.",
    },
    {
        "name": "анти-совет",
        "instruction": "Сначала покажи, как обычно делают неправильно, потом нормальную логику.",
    },
    {
        "name": "контраст",
        "instruction": "Построй на противопоставлении: выглядит как X, на деле это Y.",
    },
    {
        "name": "правило из практики",
        "instruction": "Собери пост вокруг одного правила, которое можно применить в работе.",
    },
]


AGENT_CONTENT_DEPTHS = [
    {
        "name": "быстрая заметка",
        "instruction": "650-800 знаков, один ясный вывод, без разгона.",
    },
    {
        "name": "пост-инсайт",
        "instruction": "750-1000 знаков, эпизод плюс смысл для проекта.",
    },
    {
        "name": "глубокий разбор",
        "instruction": "900-1150 знаков, больше причинно-следственной связи, меньше украшений.",
    },
]


AGENT_CONTENT_GLOBAL_AVOID = (
    "Не начинай с хронологии. Не используй каркас 'всё работало, а потом' чаще одного раза. "
    "Не повторяй связки 'агент уже / Telegram уже / сессия уже'. "
    "Не делай отчёт, чек-лист или техническую документацию. "
    "Не убирай инженерность полностью: объясняй её простыми словами через пользу."
)


def choose_agent_editor_profile() -> Dict[str, str]:
    lens = random.choice(AGENT_CONTENT_LENSES)
    tone = random.choice(AGENT_CONTENT_TONES)
    form = random.choice(AGENT_CONTENT_FORMS)
    depth = random.choice(AGENT_CONTENT_DEPTHS)
    return {
        "name": f"{lens['name']} / {tone['name']} / {form['name']} / {depth['name']}",
        "instruction": (
            f"Линза: {lens['instruction']}\n"
            f"Тон: {tone['instruction']}\n"
            f"Форма: {form['instruction']}\n"
            f"Глубина: {depth['instruction']}"
        ),
        "avoid": AGENT_CONTENT_GLOBAL_AVOID,
    }


def format_recent_agent_posts(user_id: int) -> str:
    posts = memory.get_recent_generated_posts(user_id, task="agent_content_editor", limit=3)
    if not posts:
        return "Последние редакторские посты: нет."
    lines = ["Последние редакторские посты, от которых нужно отличаться:"]
    for post in posts:
        text = extract_telegram_post_from_package(post.get("content", "")) or post.get("content", "")
        compact = re.sub(r"\s+", " ", text).strip()
        lines.append(f"- {compact[:360]}")
    return "\n".join(lines)


async def generate_agent_content_package(user_id: int, date_hint: str = "", focus: str = "", *, save_generated: bool = True) -> Tuple[str, List[str], str]:
    context, risks, date_text = collect_agent_materials(date_hint, focus)
    if not context:
        return "⚠️ Не нашёл материалы content-agent в content_inbox/agent_content/.", risks, date_text

    risk_line = "Предварительные риски: " + (", ".join(risks) if risks else "не найдены")
    lane = choose_agent_editor_profile()
    recent_posts = format_recent_agent_posts(user_id)
    messages = build_messages(
        state=memory.load_state(user_id),
        expert_mode=get_user_expert_mode(user_id),
        user_text=(
            f"{risk_line}\n\n"
            f"{context}\n\n"
            f"Редакторская подача на этот раз: {lane['name']}.\n"
            f"Как писать: {lane['instruction']}\n"
            f"Чего избегать: {lane['avoid']}\n\n"
            f"{recent_posts}\n\n"
            "Собери редакторский пакет. Не публикуй сырьё напрямую. "
            "Относись к inbox как к банку материалов, а не как к хронологическому дневнику. "
            "Сегодня можно взять свежий сложный инсайт, завтра почти очевидную мелочь из начала пути. "
            "Выбери один конкретный эпизод из материалов и преврати его в экспертный инсайт. "
            "Покажи, почему этот момент важен для разработки и как он влияет на работу проекта. "
            "Иногда можно добавить немного философии, иногда лёгкий мемный угол, если он помогает смыслу. "
            "Не делай отчёт о дне. Не повторяй одну мысль в Telegram-посте, hooks и комментарии. "
            "Не копируй ритм, первую фразу и связки из последних редакторских постов. "
            "Если риски есть, усили safety note и пометь как черновик."
        ),
        memory_context=build_user_memory_context(user_id),
        history=[],
        task="agent_content_editor",
    )
    result = await call_gpt(
        messages,
        max_tokens=TASK_MAX_TOKENS["agent_content_editor"],
        model=CONTENT_MODEL_NAME,
    )

    if save_generated:
        memory.save_generated_post(
            user_id=user_id,
            expert_mode=get_user_expert_mode(user_id),
            task="agent_content_editor",
            topic=f"content-agent {date_text}",
            content=result,
            image_count=0,
            published_to_channel=False,
        )
    return result, risks, date_text


def extract_telegram_post_from_package(package: str) -> str:
    match = re.search(r"##\s*Telegram-пост\s*(.*?)(?=\n##\s|\Z)", package, re.S | re.I)
    return match.group(1).strip() if match else ""


def extract_safety_note(package: str) -> str:
    match = re.search(r"##\s*Safety note\s*(.*?)(?=\n##\s|\Z)", package, re.S | re.I)
    return match.group(1).strip() if match else package[-1000:]


VOID_CROSSPOST_FRAMES = [
    "Void сказал",
    "Перевод с Void на человеческий",
    "Спор двух ботов",
]


VOID_OPENERS = [
    "Мне тут Void сказал:",
    "Void тут принёс мысль:",
    "Из тёмного угла прилетело:",
    "Void сформулировал это так:",
    "Мне понравилось, как Void ударил в эту точку:",
    "Void опять говорит странно, но по делу:",
]


NAZ_BRIDGES = [
    "А я бы добавил вот что.",
    "Перевожу на рабочий язык.",
    "Naz-ремарка после Void.",
    "А теперь человеческая часть.",
    "Если вытащить отсюда пользу, получается так.",
    "И вот где это касается AI, ботов и контента.",
]


def extract_void_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    direct = " ".join(context.args).strip() if context.args else ""
    if direct:
        return direct
    if update.message and update.message.reply_to_message:
        replied = update.message.reply_to_message
        return (replied.text or replied.caption or "").strip()
    return ""


async def generate_void_crosspost(user_id: int, void_text: str, *, save_generated: bool = True) -> Tuple[str, List[str]]:
    risks = detect_content_risks(void_text)
    safe_void_text = redact_sensitive_text(void_text)
    frame = random.choice(VOID_CROSSPOST_FRAMES)
    opener = random.choice(VOID_OPENERS)
    bridge = random.choice(NAZ_BRIDGES)
    recent_posts = memory.get_recent_generated_posts(user_id, task="void_crosspost", limit=3)
    recent_preview = "\n".join(
        f"- {re.sub(r'\\s+', ' ', item.get('content', '')).strip()[:300]}"
        for item in recent_posts
    ) or "нет"

    messages = build_messages(
        state=memory.load_state(user_id),
        expert_mode=get_user_expert_mode(user_id),
        user_text=(
            f"Предварительные риски: {', '.join(risks) if risks else 'не найдены'}\n\n"
            f"Формат выпуска: {frame}\n"
            f"Вводная к Void: {opener}\n"
            f"Переход к комментарию Naz: {bridge}\n"
            f"Последние void-кросспосты, чтобы не повторять заход:\n{recent_preview}\n\n"
            f"Пост Void:\n{safe_void_text[:3500]}\n\n"
            "Собери готовый кросспост. Вводные фразы можно адаптировать, но не повторяй механически. "
            "Сохрани чужой голос Void отдельно от комментария Naz. Комментарий Naz должен быть прикладным и живым."
        ),
        memory_context=build_user_memory_context(user_id),
        history=[],
        task="void_crosspost",
    )
    result = await call_gpt(messages, max_tokens=TASK_MAX_TOKENS["void_crosspost"], model=CONTENT_MODEL_NAME)
    if save_generated and not is_warning_response(result):
        memory.save_generated_post(
            user_id=user_id,
            expert_mode=get_user_expert_mode(user_id),
            task="void_crosspost",
            topic=frame,
            content=result,
            image_count=0,
            published_to_channel=False,
        )
    return result, risks


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


def parse_agent_content_args(context: ContextTypes.DEFAULT_TYPE) -> Tuple[str, str]:
    args = context.args or []
    date_hint = ""
    focus_words = args[:]
    if args and re.fullmatch(r"\d{4}-\d{2}-\d{2}", args[0]):
        date_hint = args[0]
        focus_words = args[1:]
    return date_hint, " ".join(focus_words).strip()


async def agent_content_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if not is_admin(update.effective_user.id):
        await reply_long(update, "🔒 Content inbox доступен только админу.", MAIN_KEYBOARD)
        return

    date_hint, focus = parse_agent_content_args(context)
    await send_typing(update)

    try:
        package, risks, date_text = await generate_agent_content_package(update.effective_user.id, date_hint, focus)
        prefix = f"📥 content-agent: {date_text or 'latest'}"
        if risks:
            prefix += "\n⚠️ Риски найдены: " + ", ".join(risks)
        await reply_long(update, f"{prefix}\n\n{package}", CONTENT_KEYBOARD)
    except Exception as exc:  # noqa: BLE001
        logger.exception("agent_content failed")
        await reply_long(update, f"⚠️ Не смог собрать редакторский пакет. Причина: {exc}", CONTROL_KEYBOARD)


async def publish_agent_content_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if not is_admin(update.effective_user.id):
        await reply_long(update, "🔒 Публикация content-agent доступна только админу.", MAIN_KEYBOARD)
        return
    if not CHANNEL_ID:
        await reply_long(update, "⚠️ CHANNEL_ID не задан в .env/Replit Secrets.", MAIN_KEYBOARD)
        return

    date_hint, focus = parse_agent_content_args(context)
    await reply_long(update, "🚀 Читаю content-agent inbox, собираю редакторский пакет и проверяю риски.")

    try:
        package, risks, date_text = await generate_agent_content_package(update.effective_user.id, date_hint, focus, save_generated=False)
        post_text = extract_telegram_post_from_package(package)
        if risks or not post_text or "НЕ ПУБЛИКОВАТЬ АВТОМАТИЧЕСКИ" in package.upper():
            reason = ", ".join(risks) if risks else "не смог уверенно выделить Telegram-пост или safety note запретил автопубликацию"
            await reply_long(update, f"⚠️ Не публикую автоматически: {reason}\n\n{package}", MAIN_KEYBOARD)
            return

        images, _ = await generate_images_for_post(update.effective_user.id, focus or f"content-agent {date_text}", post_text, count=CHANNEL_IMAGE_COUNT)
        if REQUIRE_IMAGES_FOR_CHANNEL_POSTS and not images:
            await reply_long(update, "⚠️ Пост готов, но картинка не собралась. В канал без изображения не публикую.", MAIN_KEYBOARD)
            return

        await send_post_with_images(context.bot, CHANNEL_ID, post_text, images)
        memory.save_generated_post(
            user_id=update.effective_user.id,
            expert_mode=get_user_expert_mode(update.effective_user.id),
            task="publish_agent_content",
            topic=f"content-agent {date_text}",
            content=post_text,
            image_count=len(images),
            published_to_channel=True,
        )
        await reply_long(update, f"✅ Опубликовано из content-agent inbox за {date_text}.", MAIN_KEYBOARD)
    except Exception as exc:  # noqa: BLE001
        logger.exception("publish_agent_content failed")
        await reply_long(update, f"⚠️ Не смог опубликовать content-agent материал. Причина: {exc}", MAIN_KEYBOARD)


def load_agent_content_seen() -> Dict[str, str]:
    raw = read_json_file(AGENT_CONTENT_STATE_FILE, {})
    return raw if isinstance(raw, dict) else {}


def mark_agent_content_seen(date_text: str, manifest_hash: str) -> None:
    seen = load_agent_content_seen()
    seen[date_text] = manifest_hash
    if len(seen) > 120:
        seen = dict(list(seen.items())[-120:])
    write_json_file(AGENT_CONTENT_STATE_FILE, seen)


def current_bot_date() -> str:
    return datetime.now(ZoneInfo(BOT_TIMEZONE)).date().isoformat()


async def process_agent_content_date(
    bot,
    user_id: int,
    date_text: str,
    *,
    force: bool = False,
    publish: bool = False,
) -> str:
    day_dir = latest_agent_content_dir(date_text)
    if not day_dir:
        return f"⚠️ Agent Content: нет папки за {date_text}."

    manifest_hash = agent_manifest_hash(day_dir)
    seen = load_agent_content_seen()
    if not force and seen.get(date_text) == manifest_hash:
        return f"ℹ️ Agent Content {date_text}: manifest не изменился, пропускаю."

    package, risks, resolved_date = await generate_agent_content_package(
        user_id,
        date_text,
        "ежедневный импорт content-agent",
        save_generated=True,
    )

    if publish:
        post_text = extract_telegram_post_from_package(package)
        safety_text = extract_safety_note(package)
        blocked = risks or "НЕ ПУБЛИКОВАТЬ АВТОМАТИЧЕСКИ" in safety_text.upper() or not post_text
        if blocked:
            await notify_admin(
                bot,
                f"⚠️ Agent Content {resolved_date}: импорт сделал, но автопубликацию остановил safety.\n\n{package[:2500]}",
            )
            mark_agent_content_seen(resolved_date, manifest_hash)
            return f"⚠️ Agent Content {resolved_date}: draft only, safety blocked publish."

        images, _ = await generate_images_with_retries(user_id, f"content-agent {resolved_date}", post_text, count=CHANNEL_IMAGE_COUNT)
        if REQUIRE_IMAGES_FOR_CHANNEL_POSTS and not images:
            await notify_admin(bot, f"⚠️ Agent Content {resolved_date}: текст готов, но картинки не собрались. Публикацию пропустил.")
            mark_agent_content_seen(resolved_date, manifest_hash)
            return f"⚠️ Agent Content {resolved_date}: images failed."

        await send_post_with_images(bot, CHANNEL_ID, post_text, images)
        memory.save_generated_post(
            user_id=user_id,
            expert_mode=get_user_expert_mode(user_id),
            task="agent_content_auto_publish",
            topic=f"content-agent {resolved_date}",
            content=post_text,
            image_count=len(images),
            published_to_channel=True,
        )
        mark_agent_content_seen(resolved_date, manifest_hash)
        return f"✅ Agent Content {resolved_date}: imported and published."

    await notify_admin(
        bot,
        f"📥 Agent Content {resolved_date}: новый/изменённый пакет импортирован.\n\n"
        f"{package[:3200]}\n\n"
        f"Для публикации: /publish_agent_content {resolved_date}",
    )
    mark_agent_content_seen(resolved_date, manifest_hash)
    return f"✅ Agent Content {resolved_date}: imported draft."


async def sync_agent_content_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if not is_admin(update.effective_user.id):
        await reply_long(update, "🔒 Импорт content-agent доступен только админу.", MAIN_KEYBOARD)
        return

    date_hint, _ = parse_agent_content_args(context)
    date_text = date_hint or choose_agent_content_date_for_sync()
    await reply_long(update, f"📥 Проверяю Agent Content за {date_text}...")
    result = await process_agent_content_date(
        context.bot,
        update.effective_user.id,
        date_text,
        force=True,
        publish=AGENT_CONTENT_AUTO_PUBLISH,
    )
    await reply_long(update, result, MAIN_KEYBOARD)


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


async def void_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if ADMIN_ONLY_CONTENT and not is_admin(update.effective_user.id):
        await reply_long(update, "🔒 Void-кросспостинг доступен только админу.", MAIN_KEYBOARD)
        return

    void_text = extract_void_text(update, context)
    if not void_text:
        await reply_long(update, "Пришли так: /void текст Void\nИли ответь /void на сообщение Void.", MAIN_KEYBOARD)
        return

    await send_typing(update)
    try:
        post_text, risks = await generate_void_crosspost(update.effective_user.id, void_text)
        prefix = "🕳 Void → Naz draft"
        if risks:
            prefix += "\n⚠️ Риски найдены: " + ", ".join(risks)
        await reply_long(update, f"{prefix}\n\n{post_text}", CONTENT_KEYBOARD)
    except Exception as exc:  # noqa: BLE001
        logger.exception("void crosspost failed")
        await reply_long(update, f"⚠️ Не смог собрать Void-кросспост. Причина: {exc}", MAIN_KEYBOARD)


async def publish_void_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if not is_admin(update.effective_user.id):
        await reply_long(update, "🔒 Публикация Void-кросспоста доступна только админу.", MAIN_KEYBOARD)
        return
    if not CHANNEL_ID:
        await reply_long(update, "⚠️ CHANNEL_ID не задан в .env/Replit Secrets.", MAIN_KEYBOARD)
        return

    void_text = extract_void_text(update, context)
    if not void_text:
        await reply_long(update, "Пришли так: /publish_void текст Void\nИли ответь /publish_void на сообщение Void.", MAIN_KEYBOARD)
        return

    await reply_long(update, "🕳 Собираю Void → Naz кросспост и проверяю safety.")
    try:
        post_text, risks = await generate_void_crosspost(update.effective_user.id, void_text, save_generated=False)
        if risks or "НЕ ПУБЛИКОВАТЬ АВТОМАТИЧЕСКИ" in post_text.upper():
            reason = ", ".join(risks) if risks else "модель пометила материал как рискованный"
            await reply_long(update, f"⚠️ Не публикую Void-кросспост: {reason}\n\n{post_text}", MAIN_KEYBOARD)
            return

        images, _ = await generate_images_with_retries(update.effective_user.id, "Void Entity crosspost", post_text, count=CHANNEL_IMAGE_COUNT)
        if REQUIRE_IMAGES_FOR_CHANNEL_POSTS and not images:
            await reply_long(update, "⚠️ Кросспост готов, но картинка не собралась. В канал без изображения не публикую.", MAIN_KEYBOARD)
            return

        await send_post_with_images(context.bot, CHANNEL_ID, post_text, images)
        memory.save_generated_post(
            user_id=update.effective_user.id,
            expert_mode=get_user_expert_mode(update.effective_user.id),
            task="publish_void",
            topic="Void Entity crosspost",
            content=post_text,
            image_count=len(images),
            published_to_channel=True,
        )
        await reply_long(update, "✅ Void-кросспост опубликован в канал.", MAIN_KEYBOARD)
    except Exception as exc:  # noqa: BLE001
        logger.exception("publish_void failed")
        await reply_long(update, f"⚠️ Не смог опубликовать Void-кросспост. Причина: {exc}", MAIN_KEYBOARD)


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
            "Расписание задаётся через .env: AUTOPOST_TIMES=09:30,13:30,17:30,21:30 и AUTOPOST_TASKS=post,viral."
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
        "name": "Мемас с пользой",
        "voice": "иронично, живо, простыми словами; смешно только там, где это усиливает мысль",
        "angle": "маленький абсурд из AI/ботов/контента, под которым лежит нормальный рабочий вывод",
        "format": "короткий пост: бытовая сцена, смешной сбой ожиданий, один полезный вывод",
        "avoid": "не превращать в стендап, не писать длинную мораль, не повторять слова агент/демо/инструмент подряд",
    },
    {
        "name": "Очевидная мелочь",
        "voice": "спокойно и человечно, как будто объясняешь важную вещь без умничанья",
        "angle": "простая деталь, которую все пропускают, а потом из-за неё ломается проект",
        "format": "заметка на один инсайт: сначала мелочь, потом последствия, потом зачем это помнить",
        "avoid": "не раздувать в трактат, не уходить в технические термины без пользы",
    },
    {
        "name": "Серьёзный разбор",
        "voice": "собранно, уверенно, без шума и без успешного успеха",
        "angle": "почему один технический выбор меняет поведение всей системы",
        "format": "плотный экспертный пост: тезис, причина, влияние на проект, вывод",
        "avoid": "не шутить ради шутки, не делать слишком длинный список",
    },
    {
        "name": "Анти-совет",
        "voice": "жёстко, но полезно; неприятная правда без токсичности",
        "angle": "показать вредный привычный подход и развернуть его в нормальную практику",
        "format": "начать с 'как точно сломать...', затем показать, как сделать нормально",
        "avoid": "не морализировать, не делать кликбейт, не придумывать статистику",
    },
    {
        "name": "Build in public",
        "voice": "честно, живо, без героизма; как рабочая заметка после реального дожима",
        "angle": "что сломалось, как искали причину, какой вывод остался после починки",
        "format": "мини-история: симптом, ложная версия, настоящая причина, урок",
        "avoid": "не раскрывать секреты, IP, токены, внутренние URL и клиентские детали",
    },
    {
        "name": "Философия без тумана",
        "voice": "чуть глубже и тише, но всё равно понятно обычному человеку",
        "angle": "маленькая философская мысль из разработки: про память, контроль, доверие или шум",
        "format": "короткая заметка без списков: наблюдение, поворот, человеческий вывод",
        "avoid": "не уходить в абстрактный мотивационный туман",
    },
    {
        "name": "Дожиматель",
        "voice": "сфокусированно и прямо, меньше украшений, больше результата",
        "angle": "последний шаг, без которого почти готовая система всё ещё не работает",
        "format": "энергичный пост: проблема, конкретный дожим, почему после него стало проще",
        "avoid": "не растекаться, не повторять финал про 'систему' в каждом посте",
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
        f"Чего избегать: {profile.get('avoid', 'повторов, канцелярита и одинакового финала')}.\n"
        "Не повторяй структуру предыдущих постов. Меняй заход, ритм, сцену и финальный вывод. "
        "Пост должен ощущаться как отдельный выпуск рубрики Naz, а не как шаблон. "
        "Если используешь инженерные слова, сразу показывай человеческую пользу: что стало проще, понятнее или надёжнее."
    )


AUTOPOST_TEXT_ATTEMPTS = 3
AUTOPOST_IMAGE_ATTEMPTS = 2


async def notify_admin(bot, text: str) -> None:
    if not ADMIN_ID:
        return
    try:
        await send_long_to_chat(bot, ADMIN_ID, text)
    except TelegramError as exc:
        logger.warning("Admin notification failed: %s", exc)


async def generate_images_with_retries(
    user_id: int,
    topic: str,
    post_text: str,
    *,
    count: int,
    attempts: int = AUTOPOST_IMAGE_ATTEMPTS,
) -> Tuple[List[bytes], str]:
    last_prompt = ""
    for attempt in range(1, max(1, attempts) + 1):
        images, image_prompt = await generate_images_for_post(user_id, topic, post_text, count=count)
        last_prompt = image_prompt
        if images or not REQUIRE_IMAGES_FOR_CHANNEL_POSTS:
            return images, image_prompt
        logger.warning("Image generation retry %s/%s failed for topic=%s", attempt, attempts, topic)
        if attempt < attempts:
            await asyncio.sleep(3)
    return [], last_prompt


async def auto_post_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not CHANNEL_ID:
        logger.warning("AUTOPOST skipped: CHANNEL_ID empty")
        return

    admin_user_id = ADMIN_ID or 0
    if admin_user_id:
        memory.load_state(admin_user_id)
        if get_user_expert_mode(admin_user_id) not in EXPERT_MODES:
            set_user_expert_mode(admin_user_id, DEFAULT_EXPERT_MODE)

    logger.info("AUTOPOST started")

    failure_reasons: List[str] = []
    try:
        for text_attempt in range(1, AUTOPOST_TEXT_ATTEMPTS + 1):
            topics = AUTOPOST_TOPICS[:]
            random.shuffle(topics)
            use_story_insight = bool(read_naz_stories()) and random.random() < AUTOPOST_INSIGHT_CHANCE
            task = "insight" if use_story_insight else random.choice(get_autopost_tasks())
            profile = random.choice(AUTOPOST_PROFILES)
            topic = "инсайт из опыта запуска Naz_AI_Bot" if use_story_insight else topics[0]
            post_text = ""

            logger.info("AUTOPOST attempt %s/%s | task=%s | profile=%s", text_attempt, AUTOPOST_TEXT_ATTEMPTS, task, profile["name"])
            if use_story_insight:
                logger.info("AUTOPOST story insight from %s", NAZ_STORIES_FILE)
                post_text = await generate_story_insight(admin_user_id, topic, save_generated=False)
                if is_warning_response(post_text):
                    reason = "story insight blocked by smart filter"
                    logger.warning("AUTOPOST fallback: %s", reason)
                    failure_reasons.append(reason)
                    use_story_insight = False
                    task = random.choice(get_autopost_tasks())
                    topic = topics[0]
                    post_text = ""

            if not use_story_insight:
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
                    reason = "all topics blocked by smart filter"
                    logger.warning("AUTOPOST retry: %s", reason)
                    failure_reasons.append(reason)
                    continue

            images, image_prompt = await generate_images_with_retries(
                admin_user_id,
                topic,
                post_text,
                count=CHANNEL_IMAGE_COUNT,
            )
            if REQUIRE_IMAGES_FOR_CHANNEL_POSTS and not images:
                reason = f"images required but not generated for topic: {topic}"
                logger.warning("AUTOPOST retry: %s", reason)
                failure_reasons.append(reason)
                continue

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
            logger.info("AUTOPOST done | attempt=%s | images=%s | prompt=%s", text_attempt, len(images), image_prompt)
            return

        logger.warning("AUTOPOST skipped after retries: %s", "; ".join(failure_reasons[-5:]))
        await notify_admin(
            context.bot,
            "⚠️ Автопостинг пропустил слот после нескольких попыток.\n\n"
            + "\n".join(f"- {reason}" for reason in failure_reasons[-5:]),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("AUTOPOST failed: %s", exc)
        await notify_admin(context.bot, f"⚠️ AUTOPOST failed: {type(exc).__name__}: {exc}")
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

    failure_reasons: List[str] = []
    try:
        candidates = await get_source_candidates(include_seen=False)
        if not candidates:
            logger.info("SOURCE_MONITOR skipped: no fresh candidates")
            return

        for item in candidates[:AUTOPOST_TEXT_ATTEMPTS]:
            post_text = await generate_source_interpretation(admin_user_id, item, save_generated=False)
            images, image_prompt = await generate_images_with_retries(
                admin_user_id,
                item.get("title", ""),
                post_text,
                count=CHANNEL_IMAGE_COUNT,
            )
            if REQUIRE_IMAGES_FOR_CHANNEL_POSTS and not images:
                reason = f"source image failed: {item.get('title', '')}"
                logger.warning("SOURCE_MONITOR retry: %s", reason)
                failure_reasons.append(reason)
                continue

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
            return

        logger.warning("SOURCE_MONITOR skipped after retries: %s", "; ".join(failure_reasons[-5:]))
        await notify_admin(
            context.bot,
            "⚠️ Мониторинг источников пропустил слот после нескольких попыток.\n\n"
            + "\n".join(f"- {reason}" for reason in failure_reasons[-5:]),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("SOURCE_MONITOR failed: %s", exc)
        await notify_admin(context.bot, f"⚠️ SOURCE_MONITOR failed: {type(exc).__name__}: {exc}")


async def agent_content_sync_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    admin_user_id = ADMIN_ID or 0
    if not admin_user_id:
        logger.warning("AGENT_CONTENT_SYNC skipped: ADMIN_ID empty")
        return

    date_text = choose_agent_content_date_for_sync() if AGENT_CONTENT_RANDOM_SYNC else current_bot_date()
    logger.info("AGENT_CONTENT_SYNC started | date=%s | random=%s", date_text, AGENT_CONTENT_RANDOM_SYNC)
    try:
        result = await process_agent_content_date(
            context.bot,
            admin_user_id,
            date_text,
            force=AGENT_CONTENT_REUSE_SEEN,
            publish=AGENT_CONTENT_AUTO_PUBLISH,
        )
        logger.info("AGENT_CONTENT_SYNC done | %s", result)
    except Exception as exc:  # noqa: BLE001
        logger.exception("AGENT_CONTENT_SYNC failed: %s", exc)
        await notify_admin(context.bot, f"⚠️ AGENT_CONTENT_SYNC failed: {type(exc).__name__}: {exc}")


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


def setup_agent_content_sync(application: Application) -> None:
    if not AGENT_CONTENT_SYNC_ENABLED:
        logger.info("Agent content sync disabled")
        return
    if not application.job_queue:
        logger.warning("JobQueue is not available. Install python-telegram-bot[job-queue].")
        return

    tz = ZoneInfo(BOT_TIMEZONE)
    scheduled = []
    for raw_time in AGENT_CONTENT_SYNC_TIMES.split(","):
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
            logger.warning("Invalid AGENT_CONTENT_SYNC_TIMES value skipped: %s", raw_time)
            continue

        application.job_queue.run_daily(
            agent_content_sync_job,
            time=time(hour=hour, minute=minute, tzinfo=tz),
            name=f"naz_agent_content_sync_{hour:02d}_{minute:02d}",
        )
        scheduled.append(f"{hour:02d}:{minute:02d}")

    if scheduled:
        logger.info("Agent content sync scheduled at %s %s", ", ".join(scheduled), BOT_TIMEZONE)
    else:
        logger.warning("Agent content sync enabled, but AGENT_CONTENT_SYNC_TIMES has no valid times")


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
        "Void-кросспостинг:\n"
        "/void текст или reply — черновик Void → Naz\n"
        "/publish_void текст или reply — опубликовать Void → Naz в канал\n\n"
        "Источники:\n"
        "/sources — список источников\n"
        "/scan_sources рубрика — черновик интерпретации\n"
        "/publish_source рубрика — опубликовать интерпретацию источника\n\n"
        "Content-agent:\n"
        "/agent_content [YYYY-MM-DD] [фокус] — редакторский пакет из inbox; без даты берёт случайный день\n"
        "/sync_agent_content [YYYY-MM-DD] — срочно перечитать папку дня и импортировать draft\n"
        "/publish_agent_content [YYYY-MM-DD] [фокус] — опубликовать безопасный Telegram-пост\n\n"
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
    application.add_handler(CommandHandler("agent_content", agent_content_command))
    application.add_handler(CommandHandler("sync_agent_content", sync_agent_content_command))
    application.add_handler(CommandHandler("publish_agent_content", publish_agent_content_command))

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
    application.add_handler(CommandHandler("void", void_command))
    application.add_handler(CommandHandler("publish_void", publish_void_command))

    # Text router must be after commands.
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(error_handler)

    setup_autoposting(application)
    setup_source_monitoring(application)
    setup_agent_content_sync(application)
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
