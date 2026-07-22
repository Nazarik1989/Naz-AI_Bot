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
import base64
import binascii
import hashlib
import json
import logging
import os
import random
import re
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from functools import wraps
from html import unescape
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont, ImageOps

if os.getenv("NAZ_ENV_LOADED_BY_SYSTEMD") != "1":
    load_dotenv()

from openai import OpenAI
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, NetworkError, TelegramError, TimedOut
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import memory
import controller as naz_controller
import character_state as naz_character
import delegated_messaging
import duo_relationship
import editorial_orchestrator
import gaming_vertical
import naz_editorial_catalog
import naz_vk_music
import scheduled_work
import semantic_autopost
import story_production
import visual_archive
import vk_publish_queue
from prompts import (
    CONTENT_TASK_PROMPTS,
    DEFAULT_CONTENT_GOAL,
    DEFAULT_EXPERT_MODE,
    DEFAULT_VOICE_PROFILE,
    EXPERT_MODES,
    GOALS,
    MATERIAL_RUBRIC,
    VOICE_PROFILES,
    build_naz_direct_image_prompt,
    build_messages,
    format_roles,
    naz_visual_prompt_context,
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
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1").strip().rstrip("/")
OPENAI_IMAGE_MODEL = os.getenv("OPENAI_IMAGE_MODEL", "openai/gpt-image-2").strip()
OPENAI_IMAGE_SIZE = os.getenv("OPENAI_IMAGE_SIZE", "1024x1024").strip()
OPENAI_IMAGE_QUALITY = os.getenv("OPENAI_IMAGE_QUALITY", "medium").strip()
OPENAI_IMAGE_TIMEOUT_SECONDS = max(30, min(env_int("OPENAI_IMAGE_TIMEOUT_SECONDS", 120), 300))
MODEL_NAME = os.getenv("MODEL_NAME", "openai/gpt-4o-mini").strip()
CONTENT_MODEL_NAME = os.getenv("CONTENT_MODEL_NAME", MODEL_NAME).strip()

# Voice messages use the official OpenAI API independently from OpenRouter.
VOICE_MESSAGES_ENABLED = env_bool("VOICE_MESSAGES_ENABLED", False)
VOICE_MESSAGES_ADMIN_ONLY = env_bool("VOICE_MESSAGES_ADMIN_ONLY", True)
VOICE_MESSAGES_CONTACTS_ENABLED = env_bool("VOICE_MESSAGES_CONTACTS_ENABLED", False)
OPENAI_VOICE_API_KEY = os.getenv("OPENAI_VOICE_API_KEY", "").strip()
OPENAI_VOICE_BASE_URL = os.getenv("OPENAI_VOICE_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")
OPENAI_TRANSCRIBE_MODEL = os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-transcribe").strip()
OPENAI_TTS_MODEL = os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts").strip()
OPENAI_TTS_VOICE = os.getenv("OPENAI_TTS_VOICE", "marin").strip()
OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-2").strip()
VOICE_MAX_BYTES = max(1024 * 1024, min(env_int("VOICE_MAX_BYTES", 15 * 1024 * 1024), 20 * 1024 * 1024))
VOICE_MAX_DURATION_SECONDS = max(10, min(env_int("VOICE_MAX_DURATION_SECONDS", 300), 1200))

ADMIN_ID = env_int("ADMIN_ID", 0)
CHANNEL_ID = os.getenv("NAZ_TELEGRAM_CHANNEL_ID", os.getenv("CHANNEL_ID", "")).strip()

HF_TOKEN = os.getenv("HF_TOKEN", "").strip()
HF_MODEL = os.getenv("HF_MODEL", "black-forest-labs/FLUX.1-schnell").strip()
IMAGE_PROVIDER = os.getenv("IMAGE_PROVIDER", "openai").strip().lower()
BFL_API_KEY = os.getenv("BFL_API_KEY", "").strip()
BFL_MODEL = os.getenv("BFL_MODEL", "flux-2-pro").strip().lower()
BFL_API_BASE = os.getenv("BFL_API_BASE", "https://api.bfl.ai/v1").strip().rstrip("/")
BFL_IMAGE_WIDTH = max(512, min(env_int("BFL_IMAGE_WIDTH", 1024), 2048))
BFL_IMAGE_HEIGHT = max(512, min(env_int("BFL_IMAGE_HEIGHT", 1024), 2048))
BFL_POLL_INTERVAL_SECONDS = max(0.5, min(env_float("BFL_POLL_INTERVAL_SECONDS", 1.0), 5.0))
BFL_TIMEOUT_SECONDS = max(30, min(env_int("BFL_TIMEOUT_SECONDS", 150), 300))
FALLBACK_IMAGE_DIR = Path(os.getenv("FALLBACK_IMAGE_DIR", "assets/fallback_images").strip())
VISUAL_ARCHIVE_ENABLED = env_bool("VISUAL_ARCHIVE_ENABLED", False)
VISUAL_ARCHIVE_ROOT = Path(os.getenv("VISUAL_ARCHIVE_ROOT", "images_curated").strip())
VISUAL_ARCHIVE_MANIFEST = Path(
    os.getenv("VISUAL_ARCHIVE_MANIFEST", "images_curated/catalog/publication_candidates.json").strip()
)
VISUAL_ARCHIVE_STATE_FILE = Path(os.getenv("VISUAL_ARCHIVE_STATE_FILE", ".visual_archive_seen.json").strip())
VISUAL_ARCHIVE_REQUIRE_APPROVED = env_bool("VISUAL_ARCHIVE_REQUIRE_APPROVED", True)
VISUAL_ARCHIVE_EVERY_N_POSTS = max(2, min(env_int("VISUAL_ARCHIVE_EVERY_N_POSTS", 3), 12))

BOT_TIMEZONE = os.getenv("BOT_TIMEZONE", "Europe/Moscow").strip()
APP_NAME = os.getenv("APP_NAME", "Naz_AI_Bot").strip()
NAZ_STORIES_FILE = Path(os.getenv("NAZ_STORIES_FILE", "naz_stories.md").strip())
NAZ_STORIES_EXTRA_FILES = tuple(
    Path(item.strip())
    for item in os.getenv("NAZ_STORIES_EXTRA_FILES", "naz_stories_2.md").split(",")
    if item.strip()
)
MONITORED_SOURCES_FILE = Path(os.getenv("MONITORED_SOURCES_FILE", "monitored_sources.json").strip())
SOURCE_SEEN_FILE = Path(os.getenv("SOURCE_SEEN_FILE", ".source_seen.json").strip())
AGENT_CONTENT_INBOX = Path(os.getenv("AGENT_CONTENT_INBOX", "content_inbox/agent_content").strip())

AUTOPOST_ENABLED = env_bool("NAZ_TELEGRAM_AUTO_ON", env_bool("AUTOPOST_ENABLED", True))
AUTOPOST_TIMES = os.getenv("NAZ_TELEGRAM_AUTO_TIMES", os.getenv("AUTOPOST_TIMES", "10:00,14:00,18:00,22:00")).strip()
AUTOPOST_TASKS = os.getenv("NAZ_TELEGRAM_AUTO_TASKS", os.getenv("AUTOPOST_TASKS", "post,viral")).strip()
AUTOPOST_INSIGHT_CHANCE = max(0.0, min(env_float("AUTOPOST_INSIGHT_CHANCE", 0.35), 1.0))
SOURCE_MONITOR_ENABLED = env_bool("SOURCE_MONITOR_ENABLED", False)
SOURCE_MONITOR_TIMES = os.getenv("SOURCE_MONITOR_TIMES", "12:00,18:00").strip()
NAZ_VK_ENABLED = env_bool("NAZ_VK_ENABLED", False)
NAZ_VK_PUBLIC_ID = os.getenv("NAZ_VK_PUBLIC_ID", "").strip()
NAZ_VK_AUTO_ON = env_bool("NAZ_VK_AUTO_ON", False)
NAZ_VK_DAILY_TIME = os.getenv("NAZ_VK_DAILY_TIME", "10:30").strip()
NAZ_VK_GAMING_TIME = os.getenv("NAZ_VK_GAMING_TIME", "16:30").strip()
NAZ_VK_TIMEZONE = os.getenv("NAZ_VK_TIMEZONE", "Europe/Moscow").strip()
NAZ_VK_SCHEDULER = os.getenv("NAZ_VK_SCHEDULER", "systemd").strip().lower()
NAZ_VK_QUEUE_DIR = Path(os.getenv("NAZ_VK_QUEUE_DIR", "/var/lib/void-vk-publisher/queue").strip())
NAZ_VK_TRACK_STATE_FILE = Path(
    os.getenv("NAZ_VK_TRACK_STATE_FILE", "/var/lib/naz-ai-bot/vk_track_rotation.json").strip()
)
NAZ_VK_IMAGE_POLICY = os.getenv("NAZ_VK_IMAGE_POLICY", "required").strip().lower()
NAZ_VK_IMAGE_ATTEMPTS = max(1, min(env_int("NAZ_VK_IMAGE_ATTEMPTS", 2), 3))
NAZ_VK_RECEIPT_SYNC_INTERVAL_SECONDS = max(
    60, env_int("NAZ_VK_RECEIPT_SYNC_INTERVAL_SECONDS", 300)
)
AGENT_CONTENT_SYNC_ENABLED = env_bool("AGENT_CONTENT_SYNC_ENABLED", True)
AGENT_CONTENT_SYNC_TIMES = os.getenv("AGENT_CONTENT_SYNC_TIMES", "23:57").strip()
AGENT_CONTENT_AUTO_PUBLISH = env_bool("AGENT_CONTENT_AUTO_PUBLISH", False)
AGENT_CONTENT_RANDOM_SYNC = env_bool("AGENT_CONTENT_RANDOM_SYNC", True)
AGENT_CONTENT_REUSE_SEEN = env_bool("AGENT_CONTENT_REUSE_SEEN", True)
AGENT_CONTENT_STATE_FILE = Path(os.getenv("AGENT_CONTENT_STATE_FILE", ".agent_content_seen.json").strip())
NAZ_STORY_PACK_ROOT = Path(
    os.getenv("NAZ_STORY_PACK_ROOT", "/var/lib/naz-ai-bot/story-packs").strip()
)
NAZ_SCHEDULED_WORK_DIR = Path(
    os.getenv(
        "NAZ_SCHEDULED_WORK_DIR",
        (
            "/var/lib/naz-ai-bot"
            if os.name != "nt"
            else str(Path(tempfile.gettempdir()) / "naz-ai-bot-scheduled-work")
        ),
    ).strip()
)
CROSSPOST_EXCHANGE_ENABLED = env_bool("CROSSPOST_EXCHANGE_ENABLED", True)
CROSSPOST_EXCHANGE_AUTO_PUBLISH = env_bool("CROSSPOST_EXCHANGE_AUTO_PUBLISH", True)
CROSSPOST_EXCHANGE_DIR = Path(os.getenv("CROSSPOST_EXCHANGE_DIR", "/opt/bot_exchange").strip())
CROSSPOST_EXCHANGE_INTERVAL_SECONDS = max(60, env_int("CROSSPOST_EXCHANGE_INTERVAL_SECONDS", 300))
CROSSPOST_EXCHANGE_MAX_PER_RUN = max(1, min(env_int("CROSSPOST_EXCHANGE_MAX_PER_RUN", 1), 5))
REQUIRE_IMAGES_FOR_CHANNEL_POSTS = env_bool("REQUIRE_IMAGES_FOR_CHANNEL_POSTS", True)
CHANNEL_IMAGE_COUNT = max(1, min(env_int("CHANNEL_IMAGE_COUNT", 1), 2))
ALLOW_IMAGE_FALLBACK = env_bool("ALLOW_IMAGE_FALLBACK", True)
ADMIN_ONLY_CONTENT = env_bool("ADMIN_ONLY_CONTENT", True)


def resolved_naz_schedule_snapshot() -> Dict[str, object]:
    return scheduled_work.resolved_schedule_snapshot(
        telegram_timezone=BOT_TIMEZONE,
        telegram_times=AUTOPOST_TIMES,
        vk_timezone=NAZ_VK_TIMEZONE,
        vk_daily_time=NAZ_VK_DAILY_TIME,
        vk_gaming_time=NAZ_VK_GAMING_TIME,
    )


def resolved_naz_deploy_schedule_snapshot() -> Dict[str, object]:
    """Return the canonical preflight schema with schedule values only."""
    telegram = resolved_naz_schedule_snapshot()["telegram"]
    return {
        "naz.telegram": {
            "daily_times": tuple(telegram["slots"]),
            "weekly_times": (),
        },
        "naz.vk": {
            "daily_times": (NAZ_VK_DAILY_TIME,),
            "weekly_times": (((1, 3, 6), NAZ_VK_GAMING_TIME),),
        },
    }


def active_naz_scheduled_work() -> tuple[dict[str, object], ...]:
    return scheduled_work.active_work(NAZ_SCHEDULED_WORK_DIR)


def scheduled_work_marker(label: str):
    """Decorator exposing safe in-flight state to coordinated deploy checks."""
    def decorate(function):
        @wraps(function)
        async def wrapped(*args, **kwargs):
            with scheduled_work.work_marker(NAZ_SCHEDULED_WORK_DIR, label):
                return await function(*args, **kwargs)
        return wrapped

    return decorate

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
AUTOPOST_SKIP_ALERTS: Dict[str, str] = {}
FALLBACK_AVATAR_CACHE: Dict[str, bytes] = {}

openai_client: Optional[OpenAI] = None
voice_openai_client: Optional[OpenAI] = None

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
BTN_LINKS = "🔗 Связи"
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

BTN_CROSSPOST_STATUS = "🔄 Статус обмена"
BTN_VOID_DRAFT = "🕳 Void → Naz draft"
BTN_VOID_PUBLISH = "📣 Void → Naz в канал"

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


MAIN_KEYBOARD = make_keyboard([[BTN_AI, BTN_CONTENT], [BTN_LINKS, BTN_CONTROL], [BTN_HELP]])
CONTACT_MAIN_KEYBOARD = make_keyboard([[BTN_AI, BTN_CONTENT], [BTN_HELP]])
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
CROSSPOST_KEYBOARD = make_keyboard([[BTN_CROSSPOST_STATUS], [BTN_VOID_DRAFT, BTN_VOID_PUBLISH], [BTN_BACK]])
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


def get_registered_contact(user_id: int) -> Optional[Dict[str, Any]]:
    if not ADMIN_ID or is_admin(user_id):
        return None
    return memory.get_saved_contact(ADMIN_ID, user_id)


def has_registered_access(user_id: int) -> bool:
    return is_admin(user_id) or get_registered_contact(user_id) is not None


def main_keyboard_for(user_id: int) -> ReplyKeyboardMarkup:
    return MAIN_KEYBOARD if is_admin(user_id) else CONTACT_MAIN_KEYBOARD


async def reject_unregistered_user(update: Update) -> bool:
    if not update.effective_user or not update.message:
        return True
    if has_registered_access(update.effective_user.id):
        return False
    await update.message.reply_text(
        "🔒 Доступ к Naz открыт владельцу и сохранённым контактам. Попроси Назара добавить тебя в /contacts.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return True


async def registered_access_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    user_id = update.effective_user.id
    if has_registered_access(user_id):
        return

    text = (update.message.text or "").strip()
    if text.startswith("/start"):
        return
    if memory.get_active_delegation(user_id) and not text.startswith("/"):
        return

    memory.remember_reachable_peer(
        user_id,
        user_display_name(update),
        (delegated_messaging.utc_now() + timedelta(hours=24)).isoformat(timespec="seconds"),
    )
    await ensure_contact_named(update, context)
    await reject_unregistered_user(update)
    raise ApplicationHandlerStop


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


async def send_long_to_chat(bot, chat_id: str | int, text: str) -> List[Any]:
    sent: List[Any] = []
    for chunk in split_telegram_text(text):
        for attempt in range(2):
            try:
                sent.append(
                    await bot.send_message(
                        chat_id=chat_id,
                        text=chunk,
                        disable_web_page_preview=True,
                    )
                )
                break
            except (TimedOut, NetworkError):
                if attempt == 1:
                    raise
                await asyncio.sleep(2)
    return sent


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
            base_url=OPENAI_BASE_URL,
            default_headers={
                "HTTP-Referer": os.getenv("OPENROUTER_SITE_URL", "https://replit.com"),
                "X-Title": APP_NAME,
            },
        )
    return openai_client


def ensure_voice_openai_client() -> OpenAI:
    """Return an official OpenAI client dedicated to speech APIs."""
    global voice_openai_client
    if not OPENAI_VOICE_API_KEY:
        raise RuntimeError("OPENAI_VOICE_API_KEY не найден. Голосовой контур пока выключен.")
    if voice_openai_client is None:
        voice_openai_client = OpenAI(
            api_key=OPENAI_VOICE_API_KEY,
            base_url=OPENAI_VOICE_BASE_URL,
        )
    return voice_openai_client


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


def build_chat_messages(
    user_text: str,
    memory_context: str,
    history: Optional[List[Dict[str, str]]] = None,
    character_context: str = "",
) -> List[Dict[str, str]]:
    system = (
        "Ты Naz_AI_Bot, живой AI-помощник Назара. Это обычный диалог, не пост для канала. "
        "Отвечай коротко: обычно 2-6 предложений. Если пользователь просит подробно, можно больше. "
        "Не превращай простые реплики в эссе, манифест или контент-пост. "
        "Стиль: дружелюбно, честно, чуть иронично, по делу. "
        "Если уместно, задай один короткий уточняющий вопрос. "
        "Не раскрывай приватные данные, токены, ключи, внутренние URL и технические секреты."
    )
    system += (
        "\n\nPLAIN TEXT ONLY: do not use Markdown or HTML. Do not use headings, "
        "backticks, emphasis markers, or Markdown list syntax."
    )
    if memory_context:
        system += "\n\nКраткий контекст памяти, если он реально помогает ответу:\n" + memory_context[:1200]
    if character_context:
        system += "\n\n" + character_context
    messages: List[Dict[str, str]] = [{"role": "system", "content": system}]
    for item in (history or [])[-20:]:
        role = item.get("role")
        content = str(item.get("content", "")).strip()
        if role in {"user", "assistant"} and content:
            messages.append({"role": role, "content": content[:5000]})
    messages.append({"role": "user", "content": user_text})
    return messages


def sanitize_dialog_text(text: str) -> str:
    """Convert model formatting to readable Telegram plain text for dialogue only."""
    value = str(text or "").replace("\r\n", "\n")
    value = re.sub(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)", r"\1 — \2", value)
    value = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", value)
    value = re.sub(r"(?m)^\s*[-*+]\s+", "• ", value)
    value = re.sub(r"(?m)^\s*(\d+)[.)]\s+", r"\1. ", value)
    value = value.replace("```", "").replace("`", "")
    value = value.replace("**", "").replace("__", "")
    value = re.sub(r"(?<!\w)[*_](?=\S)", "", value)
    value = re.sub(r"(?<=\S)[*_](?!\w)", "", value)
    value = re.sub(r"</?[A-Za-z][^>]*>", "", value)
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


async def generate_answer(
    user_id: int,
    user_text: str,
    task: str | None = None,
    source_topic: str | None = None,
    platform: str = "telegram",
    commit_state: bool = True,
    inherit_interactive_context: bool = True,
) -> str:
    """Generate answer through Controller → State → Prompt Builder → GPT."""
    state = memory.load_state(user_id)
    if not inherit_interactive_context:
        # Interactive roles, angles and private memory belong to user-driven
        # requests. Semantic autopost has server-side editorial direction and
        # a shared history of actually published posts.
        state = dict(state)
        state["expert"] = DEFAULT_EXPERT_MODE
        state["expert_mode"] = DEFAULT_EXPERT_MODE
        state["voice"] = DEFAULT_VOICE_PROFILE
        state["voice_profile"] = DEFAULT_VOICE_PROFILE
        state["goal"] = DEFAULT_CONTENT_GOAL
        state["content_goal"] = DEFAULT_CONTENT_GOAL
        state["recent_topics"] = []
        state["best_posts"] = []
        state["rejected_topics"] = []
        state["last_blocked_topic"] = ""
        state["suggested_angles"] = []
        state["selected_angle_index"] = 0
    control = naz_controller.controller(user_text, state, task=task, source_topic=source_topic)

    if control.get("blocked"):
        memory.save_state(user_id, control["state"])
        return control.get("message") or "⚠️ Controller остановил задачу."

    controlled_state = control["state"]
    expert_mode = controlled_state.get("expert_mode", DEFAULT_EXPERT_MODE)
    memory_context = (
        build_user_memory_context(user_id)
        if inherit_interactive_context
        else ""
    )

    if task is None:
        history = memory.get_history(user_id, limit=20)
        character = memory.load_character_state(user_id)
        messages = build_chat_messages(
            user_text,
            memory_context,
            history,
            naz_character.dialogue_context(character),
        )
    else:
        messages = build_messages(
            state=controlled_state,
            expert_mode=expert_mode,
            user_text=control["gpt_input"],
            memory_context=memory_context,
            history=[],
            task=task,
            platform=platform,
        )

    result = await call_gpt(messages, max_tokens=task_max_tokens(task), model=task_model(task))
    if task is None:
        result = sanitize_dialog_text(result)

    if commit_state:
        updated_state = naz_controller.update_memory_after_output(
            controlled_state,
            topic=control.get("topic", source_topic or user_text),
            output=result,
            task=task,
        )
        memory.save_state(user_id, updated_state)
        if task is None:
            memory.save_dialog_turn(user_id, user_text, result)
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
    platform: str = "telegram",
    commit_state: bool = True,
    inherit_interactive_context: bool = True,
) -> str:
    task_title = ACTION_TITLES.get(task, task)
    user_text = (
        f"Тема: {topic}\n\n"
        f"Сделай {task_title}. Сохрани прямой, живой, слегка ироничный голос Naz. "
        "Характер задаёт взгляд и интонацию, но не обязан становиться темой, сюжетом или одинаковой моралью. "
        "Текст должен быть чистым: без служебных заголовков вроде ### Хук, без выдуманной статистики, "
        "без противоречий и без успешного успеха."
    )
    if extra_instruction:
        user_text += f"\n\nДополнительная режиссура:\n{extra_instruction.strip()}"
    if platform == "vk":
        user_text += (
            "\n\nHARD LENGTH LIMIT: keep the final VK post at 700-1400 characters unless explicitly asked longer. "
            "No Telegram references, long lists, essay mode or repeated endings."
        )
    else:
        user_text += (
            "\n\nHARD LENGTH LIMIT: keep the final Telegram post at 700-1100 characters unless explicitly asked longer. "
            "No VK publishing instructions, long lists, essay mode or repeated endings."
        )
    result = await generate_answer(
        user_id,
        user_text,
        task=task,
        source_topic=topic,
        platform=platform,
        commit_state=commit_state,
        inherit_interactive_context=inherit_interactive_context,
    )
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


async def evaluate_autopost_candidate(
    candidate: str,
    recent_posts: List[Dict[str, str]],
) -> semantic_autopost.SemanticDecision:
    """Use a model as a meaning-level judge; invalid review output fails closed."""
    if not recent_posts:
        return semantic_autopost.SemanticDecision(
            True,
            "semantic history is empty",
            "",
            "",
            "",
            (),
        )
    prompt = semantic_autopost.build_gate_prompt(candidate, recent_posts)
    try:
        raw = await call_gpt(
            [
                {
                    "role": "system",
                    "content": (
                        "Ты выполняешь только редакторскую семантическую классификацию. "
                        "Не переписывай пост и верни строго запрошенный JSON."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=700,
            model=CONTENT_MODEL_NAME,
        )
        return semantic_autopost.parse_gate_response(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Semantic autopost gate failed closed: %s", exc)
        return semantic_autopost.blocked_decision(
            f"semantic gate unavailable: {type(exc).__name__}"
        )


async def get_autopost_history_profile(
    user_id: int,
    recent_posts: List[Dict[str, str]],
) -> semantic_autopost.SemanticHistoryProfile:
    digest = semantic_autopost.semantic_history_digest(recent_posts)
    if not recent_posts:
        return semantic_autopost.SemanticHistoryProfile(digest, (), "")

    cached = memory.get_cached_semantic_history_profile(user_id, digest)
    if cached is not None:
        occupied = tuple(
            key
            for key in cached.get("occupied_theme_keys", [])
            if key in semantic_autopost.THEMES_BY_KEY
        )
        logger.info(
            "SEMANTIC_HISTORY_PROFILE cache=hit | digest=%s | occupied=%s",
            digest[:12],
            ",".join(occupied),
        )
        return semantic_autopost.SemanticHistoryProfile(
            digest,
            occupied,
            str(cached.get("exclusion_summary") or ""),
        )

    prompt = semantic_autopost.build_history_profile_prompt(recent_posts)
    try:
        raw = await call_gpt(
            [
                {
                    "role": "system",
                    "content": (
                        "Ты выполняешь только полный семантический аудит истории. "
                        "Верни статус каждого ключа по точной JSON-схеме; не пиши новый пост."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=2200,
            model=CONTENT_MODEL_NAME,
        )
        profile = semantic_autopost.parse_history_profile(
            raw,
            history_digest=digest,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Semantic history profile failed closed: %s", exc)
        raise RuntimeError(
            f"semantic history profile unavailable: {type(exc).__name__}"
        ) from exc
    memory.cache_semantic_history_profile(
        user_id=user_id,
        history_digest=profile.history_digest,
        occupied_theme_keys=profile.occupied_theme_keys,
        exclusion_summary=profile.exclusion_summary,
    )
    logger.info(
        "SEMANTIC_HISTORY_PROFILE cache=miss | digest=%s | occupied=%s",
        digest[:12],
        ",".join(profile.occupied_theme_keys),
    )
    return profile


async def generate_semantic_autopost_candidate(
    *,
    user_id: int,
    platform: str,
    rubric_name: str,
    seed: str,
    generate,
) -> tuple[semantic_autopost.SemanticTheme, semantic_autopost.GenerationResult]:
    """Try a bounded sequence of distinct plans until the semantic gate accepts one."""
    recent_themes = memory.get_recent_semantic_theme_keys(
        user_id,
        limit=semantic_autopost.THEME_COOLDOWN,
    )
    recent_cards = memory.get_recent_semantic_card_keys(user_id)
    rejected_themes = memory.get_recent_rejected_semantic_theme_keys(
        user_id,
        limit=len(semantic_autopost.THEMES),
    )
    recent_posts = memory.get_recent_posts_for_semantic_gate(
        user_id,
        limit=semantic_autopost.SEMANTIC_HISTORY_LIMIT,
    )
    history_profile = await get_autopost_history_profile(user_id, recent_posts)
    # The eight-post profile guides generation and the gate; it is not another
    # hard theme ban. One axis can support genuinely different theses/scenes.
    blocked = set(rejected_themes)
    history_context = semantic_autopost.generation_history_context(recent_posts)
    exclusion_context = semantic_autopost.history_profile_context(history_profile)

    async def evaluate(candidate: str) -> semantic_autopost.SemanticDecision:
        return await evaluate_autopost_candidate(candidate, recent_posts)

    async def generate_with_history(instruction: str) -> str:
        if exclusion_context:
            instruction = f"{instruction}\n\n{exclusion_context}"
        if history_context:
            instruction = f"{instruction}\n\n{history_context}"
        return await generate(instruction)

    total_attempts = 0
    theme: semantic_autopost.SemanticTheme | None = None
    result: semantic_autopost.GenerationResult | None = None
    for plan_index in range(1, semantic_autopost.MAX_RELEASE_PLANS + 1):
        theme = semantic_autopost.select_theme(
            rubric_name,
            recent_themes,
            platform=platform,
            seed=f"{seed}:plan:{plan_index}",
            excluded_theme_keys=blocked,
        )
        card = semantic_autopost.select_card(theme.key, recent_cards)
        plan_result = await semantic_autopost.generate_with_gate(
            generate=generate_with_history,
            evaluate=evaluate,
            theme=theme,
            correction_theme=None,
            correction_theme_selector=None,
            platform=platform,
            rubric_name=rubric_name,
            is_model_warning=is_warning_response,
            card=card,
        )
        total_attempts += plan_result.attempts
        result = semantic_autopost.GenerationResult(
            accepted=plan_result.accepted,
            text=plan_result.text,
            attempts=total_attempts,
            decision=plan_result.decision,
            theme_key=plan_result.theme_key,
            card_key=plan_result.card_key,
        )
        logger.info(
            "SEMANTIC_AUTOPOST gate | platform=%s | rubric=%s | plan=%s/%s | theme=%s | card=%s | attempts=%s | accepted=%s | reason=%s",
            platform,
            rubric_name,
            plan_index,
            semantic_autopost.MAX_RELEASE_PLANS,
            result.theme_key or theme.key,
            result.card_key or card.key,
            result.attempts,
            result.accepted,
            result.decision.reason[:500].replace("\n", " "),
        )
        if result.accepted:
            return theme, result
        memory.record_rejected_semantic_theme(
            user_id=user_id,
            platform=platform,
            semantic_theme=theme.key,
            source_ref=seed,
        )
        logger.info(
            "SEMANTIC_AUTOPOST rejection remembered | platform=%s | theme=%s | source=%s",
            platform,
            theme.key,
            seed,
        )
        blocked.add(theme.key)

    if theme is None or result is None:
        raise semantic_autopost.NoSemanticThemeAvailable(
            f"no semantic release plan for rubric={rubric_name!r}"
        )
    return theme, result


def commit_accepted_autopost_state(
    *,
    user_id: int,
    topic: str,
    task: str,
    platform: str,
    source_ref: str,
    theme: semantic_autopost.SemanticTheme,
    result: semantic_autopost.GenerationResult,
) -> None:
    """Commit model state and semantic cooldown only after draft/queue/publication succeeds."""
    if not result.accepted or not result.text:
        raise ValueError("cannot commit rejected semantic autopost")
    state = memory.load_state(user_id)
    memory.save_state(
        user_id,
        naz_controller.update_memory_after_output(
            state,
            topic=topic,
            output=result.text,
            task=task,
        ),
    )
    memory.record_accepted_semantic_post(
        user_id=user_id,
        platform=platform,
        semantic_theme=theme.key,
        semantic_card=result.card_key,
        central_thesis=result.decision.central_thesis,
        conclusion=result.decision.conclusion,
        narrative_shape=result.decision.narrative_shape,
        key_meanings=result.decision.key_meanings,
        content=result.text,
        source_ref=source_ref,
    )


def naz_story_files() -> tuple[Path, ...]:
    files = (NAZ_STORIES_FILE, *NAZ_STORIES_EXTRA_FILES)
    return tuple(dict.fromkeys(files))


def read_naz_stories() -> str:
    stories = []
    for path in naz_story_files():
        try:
            text = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            continue
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not read %s: %s", path, exc)
            continue
        if text:
            stories.append(text)
    return "\n\n---\n\n".join(stories)


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


async def generate_story_insight(
    user_id: int,
    topic_hint: str = "",
    *,
    save_generated: bool = True,
    extra_instruction: str = "",
    commit_state: bool = True,
    inherit_interactive_context: bool = True,
) -> str:
    excerpt = pick_story_excerpt(topic_hint)
    if not excerpt:
        return "⚠️ Не нашёл файлы историй Naz. Добавь основной или дополнительный источник в настройки."

    topic = topic_hint or "инсайт из личного опыта запуска Naz_AI_Bot"
    user_text = (
        f"Тема/фокус: {topic}\n\n"
        f"Сырьё из историй Naz:\n{excerpt[:2400]}\n\n"
        "Сделай не пересказ, а отдельный инсайт в стиле рубрики Prompt Or Die. "
        "Пиши как вывод из опыта: конкретно, живо, инженерно, без дневникового тона."
    )
    if extra_instruction:
        user_text += f"\n\nДополнительная режиссура:\n{extra_instruction.strip()}"
    story_signature = re.sub(r"\s+", " ", excerpt).strip()[:180]
    result = await generate_answer(
        user_id,
        user_text,
        task="insight",
        source_topic=f"Naz Stories | {topic} | {story_signature}",
        platform="telegram",
        commit_state=commit_state,
        inherit_interactive_context=inherit_interactive_context,
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


async def generate_source_interpretation(
    user_id: int,
    item: Dict[str, str],
    *,
    save_generated: bool = True,
    extra_instruction: str = "",
) -> str:
    frame = random.choice(SOURCE_EDITORIAL_FRAMES)
    character = memory.load_character_state(user_id)
    attitude = duo_relationship.news_attitude(
        "naz",
        item.get("title", ""),
        item.get("summary", ""),
        tension=character.tension,
        curiosity=character.curiosity,
    )
    source_context = (
        f"Рубрика: {item.get('rubric', 'AI-находка дня')}\n"
        f"Редакторский формат: {frame['name']}\n"
        f"Угол: {frame['angle']}\n"
        f"Форма: {frame['format']}\n"
        f"Позиция Naz: {attitude['stance']} — {attitude['tone']}\n"
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
            + (f"\n\nДополнительная режиссура:\n{extra_instruction.strip()}" if extra_instruction else "")
        ),
        memory_context=build_user_memory_context(user_id),
        history=[],
        task="source_interpretation",
        platform="telegram",
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


PERSONAL_DATA_PATTERNS = [
    (
        "email address",
        re.compile(r"\b[A-Z0-9._%+-]{1,64}@[A-Z0-9.-]+\.[A-Z]{2,24}\b", re.I),
    ),
    (
        "phone number",
        re.compile(r"(?<!\w)(?:\+?\d[\s().-]*){10,15}(?!\w)"),
    ),
    ("social handle", re.compile(r"(?<!\w)@[A-Za-z0-9_]{5,32}\b")),
    (
        "street address",
        re.compile(
            r"\b(?:address|street|avenue|road|улиц[аы]?|ул\.|проспект|дом|квартир[аы]?)"
            r"\s+[A-Za-zА-Яа-яЁё0-9][^\n,;]{2,80}",
            re.I,
        ),
    ),
    (
        "medical detail",
        re.compile(
            r"\b(?:medical\s+(?:record|appointment)|patient\s+id|diagnos(?:is|ed)|"
            r"медицинск\w*\s+карт\w*|диагноз\w*|пациент\w*|при[её]м\s+у\s+врач\w*)\b",
            re.I,
        ),
    ),
]


SENSITIVE_PATTERNS = [
    ("telegram bot token", re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{25,}\b")),
    ("openai/openrouter key", re.compile(r"\b(?:sk-or-v1|sk-proj|sk)-[A-Za-z0-9_-]{16,}\b")),
    ("huggingface token", re.compile(r"\bhf_[A-Za-z0-9]{20,}\b")),
    ("secret env name", re.compile(r"\b(?:TOKEN|SECRET|PASSWORD|API_KEY|PRIVATE_KEY|CLIENT_SECRET)\b", re.I)),
    ("ssh/ip detail", re.compile(r"\b(?:ssh\s+\S+@|(?:\d{1,3}\.){3}\d{1,3})\b", re.I)),
    ("internal url", re.compile(r"\b(?:https?://)?(?:localhost|127\.0\.0\.1|0\.0\.0\.0|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+)[^\s]*", re.I)),
] + PERSONAL_DATA_PATTERNS


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
    pool = changed or (dirs if AGENT_CONTENT_REUSE_SEEN else [])
    if not pool:
        return current_bot_date()
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


async def generate_agent_content_package(
    user_id: int,
    date_hint: str = "",
    focus: str = "",
    *,
    save_generated: bool = True,
    extra_instruction: str = "",
) -> Tuple[str, List[str], str]:
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
            + (f"\n\nДополнительная режиссура:\n{extra_instruction.strip()}" if extra_instruction else "")
        ),
        memory_context=build_user_memory_context(user_id),
        history=[],
        task="agent_content_editor",
        platform="telegram",
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
    "Моя ремарка после Void.",
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


async def generate_void_crosspost(
    user_id: int,
    void_text: str,
    *,
    save_generated: bool = True,
    payload: Optional[Dict[str, Any]] = None,
    extra_instruction: str = "",
) -> Tuple[str, List[str]]:
    risks = detect_content_risks(void_text)
    safe_void_text = redact_sensitive_text(void_text)
    if isinstance((payload or {}).get("relationship_snapshot"), dict):
        memory.save_relationship_state(duo_relationship.normalize_state(payload["relationship_snapshot"]))
    relationship = memory.apply_relationship_event("challenge", topic=str((payload or {}).get("topic") or void_text[:200]))
    if payload and payload.get("schema") == "private_thought.v1":
        private_payload = payload
    else:
        private_payload = duo_relationship.build_private_thought_payload(
            speaker="void",
            thought=safe_void_text,
            topic=str((payload or {}).get("topic") or "мысль после разговора"),
            relationship=relationship,
            source_kind=str((payload or {}).get("source_event") or "manual_private_thought"),
        )
    memory.save_private_thought(private_payload, status="received")
    character = memory.load_character_state(user_id)
    reflection = duo_relationship.reflection_brief(
        receiver="naz",
        payload=private_payload,
        relationship=relationship,
        receiver_character_context=naz_character.dialogue_context(character),
    )
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
            f"Рубрика: Мысли после разговора.\n"
            f"Последние выпуски рубрики, чтобы не повторять заход:\n{recent_preview}\n\n"
            f"{reflection}\n\n"
            "Пиши от первого лица Naz. Упоминание VOID или беседы допустимо и желательно, когда звучит естественно. "
            "Не публикуй исходную реплику отдельным блоком и не называй это кросспостом. "
            "Финальный текст — новая мысль Naz, выросшая из разговора."
            + (f"\n\nДополнительная режиссура:\n{extra_instruction.strip()}" if extra_instruction else "")
        ),
        memory_context=build_user_memory_context(user_id),
        history=[],
        task="void_crosspost",
        platform="telegram",
    )
    result = await call_gpt(messages, max_tokens=TASK_MAX_TOKENS["void_crosspost"], model=CONTENT_MODEL_NAME)
    original, originality_reason = duo_relationship.reflection_is_original(private_payload["thought"], result)
    if not original:
        raise ValueError(f"reflection blocked: {originality_reason}")
    if save_generated and not is_warning_response(result):
        memory.save_generated_post(
            user_id=user_id,
            expert_mode=get_user_expert_mode(user_id),
            task="void_crosspost",
            topic="Мысли после разговора",
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


async def build_image_prompt(
    user_id: int,
    topic: str,
    post_text: str,
    variant: int = 1,
    *,
    platform: str = "telegram",
) -> str:
    visual_direction = naz_visual_prompt_context(topic)
    is_material = "material" in topic.casefold() or "матери" in topic.casefold()
    text_policy = (
        "No text except an optional minimal ice-silver MATERIAL / NAZ marking; no other "
        "letters, logos, watermarks, UI captions, charts, or interface screenshots."
        if is_material
        else "No text, letters, logos, watermarks, UI captions, charts, or interface screenshots."
    )
    character_direction = "Naz mood: lively and practical; facet: builder."
    try:
        character = memory.load_character_state(user_id)
        character_direction = (
            f"Naz mood: {naz_character.mood_label(character)}; "
            f"active character facet: {character.facet}."
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Character state unavailable for image prompt: %s", type(exc).__name__)
    messages = [
        {
            "role": "system",
            "content": (
                f"You create image-generation prompts for {platform.upper()} posts only. "
                f"Do not inherit instructions, UI, music or publishing mechanics from the other platform. "
                "Return only one concise English prompt, 60-110 words. "
                "Describe a concrete scene from the post, not abstract AI symbolism. "
                f"{text_policy}\n\n"
                "Canonical Naz visual direction follows. Treat it as a selective design system, "
                "not a fixed composition:\n"
                f"{visual_direction}"
            ),
        },
        {
            "role": "user",
            "content": (
                f"Topic: {topic}\n"
                f"Rubric and context: {topic}\n"
                f"{character_direction}\n"
                f"Variant: {variant}\n\n"
                f"Post text:\n{post_text[:1800]}\n\n"
                "Extract the main scene, conflict, mood, subject, setting, and visual metaphor. "
                "Style: cinematic editorial, realistic lighting, high detail, expressive but not stock-photo. "
                "Keep one dominant subject and use only the canonical cues relevant to this scene."
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


# -----------------------------------------------------------------------------
# Image generation
# -----------------------------------------------------------------------------


async def download_generated_image(url: str) -> bytes:
    if not url.startswith("https://"):
        raise RuntimeError("Images API returned a non-HTTPS image URL")
    try:
        async with httpx.AsyncClient(timeout=OPENAI_IMAGE_TIMEOUT_SECONDS, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Images API URL download failed: {type(exc).__name__}") from exc
    content_type = response.headers.get("content-type", "").lower()
    if not response.content or not content_type.startswith("image/"):
        raise RuntimeError("Images API URL did not return image content")
    return response.content


async def generate_openai_image_bytes(prompt: str, variant: int = 1) -> Optional[bytes]:
    """Generate through the OpenAI-compatible Images API configured for OpenRouter."""
    if not OPENROUTER_API_KEY:
        logger.warning("OPENAI_API_KEY is empty. OpenAI-compatible image generation skipped.")
        return None

    def _request():
        client = ensure_openai_client()
        return client.images.generate(
            model=OPENAI_IMAGE_MODEL,
            prompt=f"{prompt}\nComposition variation: {variant}.",
            size=OPENAI_IMAGE_SIZE,
            quality=OPENAI_IMAGE_QUALITY,
            n=1,
            timeout=OPENAI_IMAGE_TIMEOUT_SECONDS,
        )

    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(_request), timeout=OPENAI_IMAGE_TIMEOUT_SECONDS + 5
        )
        if not getattr(response, "data", None):
            raise RuntimeError("Images API returned no image data")
        item = response.data[0]
        encoded = getattr(item, "b64_json", None)
        if encoded:
            try:
                image = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise RuntimeError("Images API returned invalid base64 image data") from exc
            if not image:
                raise RuntimeError("Images API returned an empty base64 image")
            logger.info("OpenAI-compatible image ready | model=%s | variant=%s", OPENAI_IMAGE_MODEL, variant)
            return image
        url = str(getattr(item, "url", None) or "")
        if url:
            image = await download_generated_image(url)
            logger.info("OpenAI-compatible image URL ready | model=%s | variant=%s", OPENAI_IMAGE_MODEL, variant)
            return image
        raise RuntimeError("Images API response has neither b64_json nor URL")
    except Exception as exc:  # noqa: BLE001
        status_code = getattr(exc, "status_code", None)
        logger.warning(
            "Requested OpenRouter image model is unavailable or generation failed; "
            "continuing with BFL/HF fallback | model=%s | status=%s | error=%s",
            OPENAI_IMAGE_MODEL,
            status_code if status_code is not None else "unknown",
            type(exc).__name__,
        )
        return None


async def generate_bfl_image_bytes(prompt: str, variant: int = 1) -> Optional[bytes]:
    """Generate one image through the official asynchronous BFL API."""
    if not BFL_API_KEY:
        logger.warning("BFL_API_KEY is empty. BFL image generation skipped.")
        return None

    model = BFL_MODEL if re.fullmatch(r"flux-[a-z0-9-]+", BFL_MODEL) else "flux-2-pro"
    endpoint = f"{BFL_API_BASE}/{model}"
    headers = {
        "accept": "application/json",
        "x-key": BFL_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "prompt": f"{prompt}\nComposition variation: {variant}.",
        "width": BFL_IMAGE_WIDTH,
        "height": BFL_IMAGE_HEIGHT,
        "output_format": "jpeg",
    }

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.post(endpoint, headers=headers, json=payload)
            if response.status_code != 200:
                logger.error("BFL submit error %s | %s", response.status_code, response.text[:500])
                return None

            data = response.json()
            polling_url = str(data.get("polling_url") or "")
            if not polling_url.startswith("https://"):
                logger.error("BFL response has no valid polling_url | %s", str(data)[:500])
                return None

            deadline = asyncio.get_running_loop().time() + BFL_TIMEOUT_SECONDS
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(BFL_POLL_INTERVAL_SECONDS)
                poll_response = await client.get(
                    polling_url,
                    headers={"accept": "application/json", "x-key": BFL_API_KEY},
                )
                if poll_response.status_code != 200:
                    logger.error("BFL poll error %s | %s", poll_response.status_code, poll_response.text[:500])
                    return None

                result = poll_response.json()
                status = str(result.get("status") or "")
                if status == "Ready":
                    sample_url = str((result.get("result") or {}).get("sample") or "")
                    if not sample_url.startswith("https://"):
                        logger.error("BFL result has no valid sample URL | %s", str(result)[:500])
                        return None
                    image_response = await client.get(sample_url)
                    content_type = image_response.headers.get("content-type", "")
                    if image_response.status_code == 200 and image_response.content and content_type.startswith("image/"):
                        logger.info("BFL image ready | model=%s | variant=%s", model, variant)
                        return image_response.content
                    logger.error("BFL image download error %s | %s", image_response.status_code, image_response.text[:300])
                    return None
                if status in {"Error", "Failed"}:
                    logger.error("BFL generation failed | %s", str(result)[:500])
                    return None

            logger.error("BFL generation timed out after %ss", BFL_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001
        logger.exception("BFL image request failed: %s", exc)
    return None


class ReferenceImageUnsupportedError(RuntimeError):
    pass


async def openrouter_model_supports_reference() -> bool:
    if not OPENROUTER_API_KEY:
        return False
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{OPENAI_BASE_URL}/images/models",
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
            )
        response.raise_for_status()
        models = response.json().get("data", [])
        record = next((item for item in models if item.get("id") == OPENAI_IMAGE_MODEL), None)
        modalities = ((record or {}).get("architecture") or {}).get("input_modalities") or []
        return "image" in modalities
    except Exception as exc:  # noqa: BLE001
        logger.warning("Reference capability check failed | model=%s | error=%s", OPENAI_IMAGE_MODEL, type(exc).__name__)
        return False


async def generate_reference_image_bytes(prompt: str, reference_data_url: str) -> bytes:
    """Edit using one reference without reference-blind provider fallback."""
    if not await openrouter_model_supports_reference():
        raise ReferenceImageUnsupportedError(
            f"Модель {OPENAI_IMAGE_MODEL} сейчас не подтверждает поддержку изображения-референса."
        )

    def _request():
        return ensure_openai_client().images.generate(
            model=OPENAI_IMAGE_MODEL,
            prompt=prompt,
            size=OPENAI_IMAGE_SIZE,
            quality=OPENAI_IMAGE_QUALITY,
            n=1,
            timeout=OPENAI_IMAGE_TIMEOUT_SECONDS,
            extra_body={
                "input_references": [{"type": "image_url", "image_url": {"url": reference_data_url}}]
            },
        )

    try:
        response = await asyncio.wait_for(asyncio.to_thread(_request), timeout=OPENAI_IMAGE_TIMEOUT_SECONDS + 5)
        item = response.data[0] if getattr(response, "data", None) else None
        encoded = getattr(item, "b64_json", None) if item else None
        if encoded:
            image = base64.b64decode(encoded, validate=True)
            if image:
                return image
        url = str(getattr(item, "url", None) or "") if item else ""
        if url:
            return await download_generated_image(url)
        raise RuntimeError("Images API returned no edited image")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Reference image request rejected | model=%s | error=%s", OPENAI_IMAGE_MODEL, type(exc).__name__)
        raise ReferenceImageUnsupportedError(
            "OpenRouter отклонил редактирование по референсу; генерация с нуля не выполнялась."
        ) from exc


async def generate_hf_image_bytes(prompt: str, variant: int = 1) -> Optional[bytes]:
    """Generate one image through Hugging Face when its credits are available."""
    if not HF_TOKEN:
        logger.warning("HF_TOKEN is empty. Hugging Face image generation skipped.")
        return None

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
    return None


async def generate_image_bytes(prompt: str, variant: int = 1) -> Optional[bytes]:
    """Generate through the preferred provider, then try the configured backup."""
    providers = {
        "openai": (generate_openai_image_bytes, generate_bfl_image_bytes, generate_hf_image_bytes),
        "bfl": (generate_bfl_image_bytes, generate_hf_image_bytes),
        "huggingface": (generate_hf_image_bytes, generate_bfl_image_bytes),
        "hf": (generate_hf_image_bytes, generate_bfl_image_bytes),
    }.get(IMAGE_PROVIDER, (generate_openai_image_bytes, generate_bfl_image_bytes, generate_hf_image_bytes))

    for provider in providers:
        image = await provider(prompt, variant=variant)
        if image:
            return image

    return await fallback_image_bytes() if ALLOW_IMAGE_FALLBACK else None


def load_brand_font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


async def telegram_avatar_bytes(chat_id: str) -> Optional[bytes]:
    """Fetch and cache a Telegram chat avatar without exposing the bot token."""
    if not BOT_TOKEN or not chat_id:
        return None
    if chat_id in FALLBACK_AVATAR_CACHE:
        return FALLBACK_AVATAR_CACHE[chat_id]

    api_base = f"https://api.telegram.org/bot{BOT_TOKEN}"
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            chat_response = await client.get(f"{api_base}/getChat", params={"chat_id": chat_id})
            chat_data = chat_response.json() if chat_response.status_code == 200 else {}
            photo = (chat_data.get("result") or {}).get("photo") or {}
            file_id = str(photo.get("big_file_id") or photo.get("small_file_id") or "")
            if not file_id:
                return None

            file_response = await client.get(f"{api_base}/getFile", params={"file_id": file_id})
            file_data = file_response.json() if file_response.status_code == 200 else {}
            file_path = str((file_data.get("result") or {}).get("file_path") or "")
            if not file_path:
                return None

            avatar_response = await client.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}")
            if avatar_response.status_code == 200 and avatar_response.content:
                FALLBACK_AVATAR_CACHE[chat_id] = avatar_response.content
                return avatar_response.content
    except Exception as exc:  # noqa: BLE001
        logger.warning("Telegram avatar unavailable for %s: %s", chat_id, exc)
    return None


async def brand_avatar_sources() -> List[Tuple[str, Optional[bytes]]]:
    bot_chat_id = ""
    if BOT_TOKEN:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe")
            data = response.json() if response.status_code == 200 else {}
            username = str((data.get("result") or {}).get("username") or "")
            bot_chat_id = f"@{username}" if username else ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("Telegram bot profile unavailable for fallback card: %s", exc)

    return [
        ("NAZ AI BOT", await telegram_avatar_bytes(bot_chat_id)),
        ("@PromptOrDie", await telegram_avatar_bytes(CHANNEL_ID or "@PromptOrDie")),
    ]


def paste_round_avatar(
    image: Image.Image,
    avatar_bytes: Optional[bytes],
    box: Tuple[int, int, int, int],
    accent: Tuple[int, int, int],
) -> None:
    x1, y1, x2, y2 = box
    avatar_size = x2 - x1
    draw = ImageDraw.Draw(image, "RGBA")
    draw.ellipse((x1 - 8, y1 - 8, x2 + 8, y2 + 8), fill=(*accent, 220))
    draw.ellipse((x1, y1, x2, y2), fill=(24, 28, 42, 255))
    if not avatar_bytes:
        return
    try:
        avatar = Image.open(BytesIO(avatar_bytes)).convert("RGB")
        side = min(avatar.width, avatar.height)
        left = (avatar.width - side) // 2
        top = (avatar.height - side) // 2
        avatar = avatar.crop((left, top, left + side, top + side)).resize((avatar_size, avatar_size), Image.Resampling.LANCZOS)
        mask = Image.new("L", (avatar_size, avatar_size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, avatar_size - 1, avatar_size - 1), fill=255)
        image.paste(avatar, (x1, y1), mask)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Fallback avatar rendering failed: %s", exc)


async def fallback_image_bytes() -> Optional[bytes]:
    """Build a local Naz-branded card when every remote image provider fails."""
    try:
        size = 1024
        if FALLBACK_IMAGE_DIR.exists():
            candidates = [
                path for path in FALLBACK_IMAGE_DIR.iterdir()
                if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
            ]
            if candidates:
                source = random.choice(candidates)
                with Image.open(source) as library_image:
                    prepared = ImageOps.fit(
                        library_image.convert("RGB"),
                        (size, size),
                        method=Image.Resampling.LANCZOS,
                    )
                    output = BytesIO()
                    prepared.save(output, format="JPEG", quality=92, optimize=True)
                logger.info("Local fallback image selected | file=%s", source.name)
                return output.getvalue()

        accent_options = [(112, 255, 190), (159, 122, 234), (255, 184, 92)]
        accent = random.choice(accent_options)
        image = Image.new("RGB", (size, size), (8, 10, 18))
        draw = ImageDraw.Draw(image, "RGBA")

        for y in range(size):
            ratio = y / max(1, size - 1)
            color = (
                int(8 + accent[0] * ratio * 0.08),
                int(10 + accent[1] * ratio * 0.07),
                int(18 + accent[2] * ratio * 0.10),
            )
            draw.line((0, y, size, y), fill=color)

        draw.ellipse((-220, -180, 520, 560), fill=(*accent, 24), outline=(*accent, 90), width=3)
        draw.ellipse((610, 560, 1220, 1170), fill=(92, 74, 210, 30), outline=(159, 122, 234, 95), width=3)
        draw.line((92, 170, 932, 170), fill=(*accent, 180), width=4)
        draw.line((92, 854, 680, 854), fill=(*accent, 90), width=2)

        label_font = load_brand_font(28, bold=True)
        title_font = load_brand_font(82, bold=True)
        subtitle_font = load_brand_font(34)
        footer_font = load_brand_font(23)

        draw.text((96, 98), "NAZ // CONTENT SYSTEM", font=label_font, fill=(*accent, 255))
        avatars = await brand_avatar_sources()
        paste_round_avatar(image, avatars[0][1], (105, 290, 365, 550), accent)
        paste_round_avatar(image, avatars[1][1], (655, 290, 915, 550), (159, 122, 234))
        draw.line((390, 420, 630, 420), fill=(*accent, 170), width=5)
        draw.ellipse((494, 404, 526, 436), fill=(244, 246, 252, 255))
        draw.text((134, 590), avatars[0][0], font=footer_font, fill=(220, 224, 235, 255))
        draw.text((695, 590), avatars[1][0], font=footer_font, fill=(220, 224, 235, 255))
        draw.text((96, 685), "SYSTEM VISUAL", font=title_font, fill=(244, 246, 252, 255), stroke_width=1)
        draw.text((96, 795), "AI  •  SYSTEM  •  CONTENT", font=footer_font, fill=(150, 157, 178, 255))

        output = BytesIO()
        image.save(output, format="JPEG", quality=92, optimize=True)
        logger.info("Local Naz branded image fallback generated")
        return output.getvalue()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Local branded image fallback failed: %s", exc)
    return None


async def generate_images_for_post(
    user_id: int,
    topic: str,
    post_text: str,
    count: int = 1,
    *,
    platform: str = "telegram",
    editorial_visual_brief: str = "",
) -> Tuple[List[bytes], str]:
    count = max(1, min(int(count), 4))
    is_material = "material" in topic.casefold() or "матери" in topic.casefold()
    if is_material and count > 1:
        images: List[bytes] = []
        frame_prompts: List[str] = []
        for variant in range(1, count + 1):
            frame_prompt = (
                f"{naz_visual_prompt_context(topic)}\n\n{editorial_visual_brief}\n\n"
                f"MATERIAL frame {variant} of {count}; preserve the same subject and scene continuity. "
                "No text except optional minimal MATERIAL / NAZ marking."
                if editorial_visual_brief
                else await build_image_prompt(
                    user_id, topic, post_text, variant=variant, platform=platform,
                )
            )
            frame_prompts.append(frame_prompt)
            image = await generate_image_bytes(frame_prompt, variant=variant)
            if image:
                images.append(image)
        return images, "\n---\n".join(frame_prompts)

    image_prompt = (
        f"{naz_visual_prompt_context(topic)}\n\n{editorial_visual_brief}\n\n"
        "Render the concrete planned scene only. No unrelated people, stock scene, text, logo, UI or watermark."
        if editorial_visual_brief
        else await build_image_prompt(
            user_id, topic, post_text, variant=1, platform=platform,
        )
    )
    images: List[bytes] = []

    # Последовательно, чтобы не ловить лишние rate limits у image providers.
    for variant in range(1, count + 1):
        img = await generate_image_bytes(image_prompt, variant=variant)
        if img:
            images.append(img)

    return images, image_prompt


async def generate_two_images_for_post(user_id: int, topic: str, post_text: str) -> Tuple[List[bytes], str]:
    return await generate_images_for_post(user_id, topic, post_text, count=2)


@dataclass(frozen=True, slots=True)
class TelegramPublicationReceipt:
    chat_id: str
    message_id: str


def _telegram_publication_receipt(
    result: Any,
    fallback_chat_id: int | str,
) -> TelegramPublicationReceipt:
    if isinstance(result, (list, tuple)):
        message = result[0] if result else None
    else:
        message = result
    chat_value = getattr(message, "chat_id", None)
    if chat_value is None and getattr(message, "chat", None) is not None:
        chat_value = getattr(message.chat, "id", None)
    message_value = getattr(message, "message_id", None)
    return TelegramPublicationReceipt(
        chat_id=str(chat_value if chat_value is not None else fallback_chat_id)[:128],
        message_id=str(message_value if isinstance(message_value, int) else "")[:128],
    )


async def send_post_with_images(
    bot,
    chat_id: int | str,
    post_text: str,
    images: List[bytes],
) -> TelegramPublicationReceipt:
    """Send post. If images fail, text still goes out."""
    if not images:
        sent = await send_long_to_chat(bot, chat_id, post_text)
        return _telegram_publication_receipt(sent, chat_id)
    caption_limit = 1000
    caption = post_text if len(post_text) <= caption_limit else "🖼 Иллюстрации к посту"

    try:
        if len(images) >= 2:
            media = []
            for idx, img in enumerate(images[:2], start=1):
                bio = BytesIO(img)
                bio.name = f"naz_image_{idx}.png"
                media.append(InputMediaPhoto(media=bio, caption=caption if idx == 1 else None))
            primary_result = await bot.send_media_group(chat_id=chat_id, media=media)
        else:
            bio = BytesIO(images[0])
            bio.name = "naz_image.png"
            primary_result = await bot.send_photo(chat_id=chat_id, photo=bio, caption=caption)

        if len(post_text) > caption_limit:
            await send_long_to_chat(bot, chat_id, post_text)
        return _telegram_publication_receipt(primary_result, chat_id)
    except (TelegramError, BadRequest) as exc:
        logger.exception("Telegram image send failed: %s", exc)
        sent = await send_long_to_chat(bot, chat_id, post_text)
        return _telegram_publication_receipt(sent, chat_id)


async def send_observed_scheduled_post(
    *,
    bot,
    chat_id: int | str,
    post_text: str,
    images: List[bytes],
    user_id: int,
    plan_id: str,
) -> TelegramPublicationReceipt:
    try:
        return await send_post_with_images(bot, chat_id, post_text, images)
    except Exception:
        memory.update_editorial_release_event(
            user_id=user_id,
            plan_id=plan_id,
            platform="telegram",
            history_commit_status="failed",
        )
        raise


# -----------------------------------------------------------------------------
# Command handlers
# -----------------------------------------------------------------------------


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    user_id = update.effective_user.id
    if context.args and context.args[0].startswith("delegate_"):
        token = context.args[0].removeprefix("delegate_")
        row = memory.accept_delegation_invite(token, user_id, user_display_name(update))
        if not row:
            await update.message.reply_text("Ссылка недействительна, уже использована или устарела.")
            return
        delegation = delegation_from_row(row)
        intro = delegated_messaging.introduction(delegation)
        await update.message.reply_text(intro)
        memory.save_delegated_message(int(row["id"]), "assistant", intro)
        memory.set_delegation_status(int(row["id"]), "active")
        await context.bot.send_message(
            chat_id=delegation.owner_user_id,
            text=f"{delegation.contact_name} открыл(а) ссылку. Naz начал поручение #{row['id']}.",
        )
        return
    if not is_admin(user_id):
        memory.remember_reachable_peer(
            user_id,
            user_display_name(update),
            (delegated_messaging.utc_now() + timedelta(hours=24)).isoformat(timespec="seconds"),
        )
        await ensure_contact_named(update, context)
        if await reject_unregistered_user(update):
            return
    memory.load_state(user_id)
    name = user_display_name(update)
    sections = (
        "🧠 AI — режимы экспертов\n"
        "🚀 Контент — посты, Reels, планы, картинки\n"
    )
    if is_admin(user_id):
        sections += "🔗 Связи — обмен между ботами\n📊 Центр управления — память, статистика, автопостинг\n"
    sections += "ℹ️ Помощь — доступные команды и описание проекта"
    text = (
        f"🤖 Naz AI\n\n"
        f"{name}, я твой AI-помощник для контента, нейросетей и автоматизации.\n\n"
        f"Выбери раздел:\n\n{sections}"
    )
    await update.message.reply_text(text, reply_markup=main_keyboard_for(user_id))


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user or await reject_unregistered_user(update):
        return
    await update.message.reply_text("Главное меню Naz:", reply_markup=main_keyboard_for(update.effective_user.id))


def delegation_from_row(row: Dict[str, Any]) -> delegated_messaging.Delegation:
    return delegated_messaging.Delegation(
        character_id=str(row["character_id"]),
        owner_user_id=int(row["owner_user_id"]),
        contact_chat_id=int(row["contact_chat_id"]),
        contact_name=str(row.get("contact_name") or row.get("contact_label") or "Собеседник"),
        purpose=str(row["purpose"]),
        status=str(row["status"]),
        max_turns=int(row["max_turns"]),
        turns_used=int(row["turns_used"]),
        expires_at=str(row["expires_at"]),
    )


async def ensure_contact_named(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not ADMIN_ID or is_admin(update.effective_user.id):
        return
    display_name = user_display_name(update)
    if not memory.register_contact_arrival(ADMIN_ID, update.effective_user.id, display_name):
        return
    prompt = await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            f"Мне впервые написал {display_name} (Telegram ID {update.effective_user.id}).\n"
            "Как записать контакт? Ответь на это сообщение одним именем, например: Диман"
        ),
    )
    memory.save_contact_naming_request(prompt.message_id, ADMIN_ID, update.effective_user.id)


async def start_saved_contact_delegation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    contact: Dict[str, Any],
    purpose: str,
) -> None:
    token = delegated_messaging.invite_token()
    expires = (delegated_messaging.utc_now() + timedelta(hours=24)).isoformat(timespec="seconds")
    delegation_id = memory.create_delegation_invite(
        update.effective_user.id, "naz", str(contact["alias"]), purpose, token, expires
    )
    row = memory.accept_delegation_invite(token, int(contact["chat_id"]), str(contact["alias"]))
    if not row:
        raise RuntimeError("Не удалось привязать сохранённый контакт.")
    delegation = delegation_from_row(row)
    intro = delegated_messaging.introduction(delegation)
    await context.bot.send_message(chat_id=delegation.contact_chat_id, text=intro)
    memory.save_delegated_message(delegation_id, "assistant", intro)
    memory.set_delegation_status(delegation_id, "active")
    await update.message.reply_text(
        f"Нашёл {contact['alias']} и начал поручение #{delegation_id}. После разговора удалю сессию и переписку."
    )


async def delegate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Эта команда доступна только Назару.")
        return
    purpose = " ".join(context.args).strip()
    try:
        purpose = delegated_messaging.clean_purpose(purpose)
    except ValueError as exc:
        await update.message.reply_text(f"Использование: /delegate о чём и зачем поговорить\n\n{exc}")
        return
    context.user_data["delegation_purpose"] = purpose
    await update.message.reply_text(
        "Теперь отправь мне карточку нужного Telegram-контакта. Номер телефона я сохранять не буду."
    )


async def delegation_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not update.message.contact:
        return
    if not is_admin(update.effective_user.id):
        return
    purpose = str(context.user_data.pop("delegation_purpose", ""))
    if not purpose:
        await update.message.reply_text("Сначала задай поручение: /delegate о чём поговорить")
        return
    contact = update.message.contact
    label = " ".join(part for part in [contact.first_name, contact.last_name or ""] if part).strip()
    token = delegated_messaging.invite_token()
    expires = (delegated_messaging.utc_now() + timedelta(hours=24)).isoformat(timespec="seconds")
    try:
        delegation_id = memory.create_delegation_invite(
            update.effective_user.id, "naz", label, purpose, token, expires
        )
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return
    if contact.user_id and memory.get_reachable_peer(contact.user_id):
        row = memory.accept_delegation_invite(token, contact.user_id, label)
        if row:
            delegation = delegation_from_row(row)
            intro = delegated_messaging.introduction(delegation)
            await context.bot.send_message(chat_id=contact.user_id, text=intro)
            memory.save_delegated_message(delegation_id, "assistant", intro)
            memory.set_delegation_status(delegation_id, "active")
            await update.message.reply_text(
                f"{label} уже писал(а) боту. Naz представился и начал одноразовый разговор #{delegation_id}."
            )
            return
    bot_user = await context.bot.get_me()
    link = f"https://t.me/{bot_user.username}?start=delegate_{token}"
    await update.message.reply_text(
        f"Поручение #{delegation_id} подготовлено для {label}.\n\n"
        "Перешли человеку эту одноразовую ссылку:\n" + link + "\n\n"
        "Когда человек нажмёт Start, Naz сам представится и начнёт разговор."
    )


async def delegate_accept_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    token = "".join(context.args).strip()
    name = user_display_name(update)
    row = memory.accept_delegation_invite(token, update.effective_user.id, name)
    if not row:
        await update.message.reply_text("Ссылка недействительна, уже использована или устарела.")
        return
    delegation = delegation_from_row(row)
    await update.message.reply_text(
        "Согласие принято. Naz напишет только после отдельного подтверждения Назара. "
        "В любой момент можно ответить «стоп»."
    )
    await context.bot.send_message(
        chat_id=delegation.owner_user_id,
        text=(
            f"{delegation.contact_name} подтвердил(а) одноразовый разговор.\n\n"
            f"Предпросмотр первой реплики:\n{delegated_messaging.introduction(delegation)}\n\n"
            f"Отправить: /delegate_send {row['id']}\nОтменить: /delegate_stop {row['id']}"
        ),
    )


async def delegate_send_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not is_admin(update.effective_user.id):
        return
    try:
        delegation_id = int(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("Использование: /delegate_send ID")
        return
    row = memory.get_delegation(delegation_id)
    if not row or row["owner_user_id"] != update.effective_user.id or row["status"] != "accepted":
        await update.message.reply_text("Нет ожидающего подтверждения поручения с таким ID.")
        return
    delegation = delegation_from_row(row)
    text = delegated_messaging.introduction(delegation)
    await context.bot.send_message(chat_id=delegation.contact_chat_id, text=text)
    memory.save_delegated_message(delegation_id, "assistant", text)
    memory.set_delegation_status(delegation_id, "active")
    await update.message.reply_text(f"Naz начал одноразовый разговор #{delegation_id} с {delegation.contact_name}.")


async def delegate_stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not is_admin(update.effective_user.id):
        return
    try:
        delegation_id = int(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("Использование: /delegate_stop ID")
        return
    row = memory.get_delegation(delegation_id)
    if not row or row["owner_user_id"] != update.effective_user.id:
        await update.message.reply_text("Поручение не найдено.")
        return
    if row.get("contact_chat_id"):
        await context.bot.send_message(chat_id=int(row["contact_chat_id"]), text="Разговор завершён. Спасибо.")
    memory.purge_delegation(delegation_id, "owner_stopped")
    await update.message.reply_text("Поручение завершено; контакт и переписка удалены.")


async def delegate_reply_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not is_admin(update.effective_user.id):
        return
    raw = " ".join(context.args).strip()
    head, sep, text = raw.partition(" ")
    if not sep or not head.isdigit() or not text.strip():
        await update.message.reply_text("Использование: /delegate_reply ID текст")
        return
    delegation_id = int(head)
    row = memory.get_delegation(delegation_id)
    if not row or row["owner_user_id"] != update.effective_user.id or row["status"] != "paused":
        await update.message.reply_text("Нет приостановленного поручения с таким ID.")
        return
    await context.bot.send_message(chat_id=int(row["contact_chat_id"]), text=text.strip())
    memory.save_delegated_message(delegation_id, "assistant", text.strip())
    memory.set_delegation_status(delegation_id, "active")
    await update.message.reply_text("Твой ответ отправлен; Naz может продолжить в рамках поручения.")


async def contact_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not is_admin(update.effective_user.id):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /contact_add TELEGRAM_ID Имя")
        return
    try:
        chat_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("TELEGRAM_ID должен быть числом из /contact_candidates.")
        return
    alias = " ".join(context.args[1:]).strip()
    try:
        chat = await context.bot.get_chat(chat_id)
        display_name = " ".join(part for part in [chat.first_name or "", chat.last_name or ""] if part).strip()
        row = memory.save_named_contact(update.effective_user.id, chat_id, display_name or str(chat_id), alias)
    except (TelegramError, ValueError) as exc:
        await update.message.reply_text(f"Не записал контакт: {exc}")
        return
    await update.message.reply_text(f"Записал: {row['alias']} → {row['display_name']} ({chat_id}).")


async def contact_candidates_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not is_admin(update.effective_user.id):
        return
    ids = memory.list_previous_contact_ids(update.effective_user.id)
    if not ids:
        await update.message.reply_text("Незаписанных прежних собеседников не нашёл.")
        return
    lines = ["Ранее писали боту:"]
    for chat_id in ids:
        try:
            chat = await context.bot.get_chat(chat_id)
            name = " ".join(part for part in [chat.first_name or "", chat.last_name or ""] if part).strip()
        except TelegramError:
            name = "имя недоступно"
        lines.append(f"• {chat_id} — {name}")
    lines.append("\nЗаписать: /contact_add TELEGRAM_ID Имя")
    await update.message.reply_text("\n".join(lines))


async def contacts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not is_admin(update.effective_user.id):
        return
    contacts = memory.list_saved_contacts(update.effective_user.id)
    if not contacts:
        await update.message.reply_text("Сохранённых контактов пока нет.")
        return
    aliases = "\n".join(f"• {item['alias']}" for item in contacts)
    await update.message.reply_text(
        "Сохранённые контакты:\n"
        f"{aliases}\n\n"
        "Текст: Напиши Диману: Привет, созвонимся вечером?\n"
        "Голосовое: Отправь Диману голосовое: Привет, созвонимся вечером?"
    )


async def prepare_contact_message_request(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> bool:
    """Create an outbound preview; never send before an explicit callback confirmation."""
    if not update.effective_user or not update.message or not is_admin(update.effective_user.id):
        return False
    contacts = memory.list_saved_contacts(update.effective_user.id)
    voice_request = delegated_messaging.parse_saved_contact_voice_request(contacts, text)
    request = None if voice_request else delegated_messaging.parse_contact_message_request(text)
    contact = None
    message_text = ""
    spoken_alias = ""
    delivery_kind = "text"
    if voice_request:
        contact, message_text = voice_request
        spoken_alias = str(contact.get("alias", ""))
        delivery_kind = "voice"
    elif request:
        spoken_alias, message_text = request
        contact = delegated_messaging.resolve_saved_contact(contacts, spoken_alias)
    else:
        natural_request = delegated_messaging.parse_saved_contact_message_request(contacts, text)
        if not natural_request:
            return False
        contact, message_text = natural_request
        spoken_alias = str(contact.get("alias", ""))
    if not contact:
        aliases = ", ".join(str(item["alias"]) for item in contacts) or "пока пусто"
        await update.message.reply_text(
            f"Не нашёл один точный контакт «{spoken_alias}». Сохранены: {aliases}."
        )
        return True
    draft = memory.create_pending_contact_message(
        update.effective_user.id,
        int(contact["chat_id"]),
        str(contact["alias"]),
        message_text,
        delivery_kind=delivery_kind,
    )
    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Отправить", callback_data=f"contact_send:{draft['id']}"),
            InlineKeyboardButton("Отмена", callback_data=f"contact_cancel:{draft['id']}"),
        ]]
    )
    format_label = "голосовое (AI-голос Naz)" if delivery_kind == "voice" else "текст"
    await update.message.reply_text(
        "Проверь перед отправкой. Черновик действует 15 минут.\n\n"
        f"Контакт: {contact['alias']}\n\n"
        f"Формат: {format_label}\n\n"
        "Сообщение от Назара:\n"
        f"{message_text}",
        reply_markup=keyboard,
    )
    return True


async def contact_message_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user:
        return
    await query.answer()
    if not is_admin(update.effective_user.id):
        await query.edit_message_text("Это подтверждение доступно только Назару.")
        return
    match = re.fullmatch(r"contact_(send|cancel):(\d+)", str(query.data or ""))
    if not match:
        return
    action, message_id_text = match.groups()
    draft = memory.get_pending_contact_message(int(message_id_text), update.effective_user.id)
    if not draft:
        await query.edit_message_text("Черновик уже обработан или истёк. Создай новый.")
        return
    if action == "cancel":
        memory.delete_pending_contact_message(int(draft["id"]), update.effective_user.id)
        await query.edit_message_text(f"Отменено. Сообщение для {draft['contact_alias']} не отправлено.")
        return
    delivery_kind = str(draft.get("delivery_kind") or "text")
    delivered_text = f"Сообщение от Назара:\n\n{draft['message_text']}"
    try:
        if delivery_kind == "voice":
            audio = await synthesize_voice_bytes(str(draft["message_text"]))
            payload = BytesIO(audio)
            payload.name = "naz-contact-message.ogg"
            await context.bot.send_voice(
                chat_id=int(draft["contact_chat_id"]),
                voice=payload,
                caption="AI-голос Naz по поручению Назара",
            )
        else:
            await context.bot.send_message(
                chat_id=int(draft["contact_chat_id"]),
                text=delivered_text,
                disable_web_page_preview=True,
            )
    except (RuntimeError, TelegramError) as exc:
        logger.warning(
            "Contact message send failed | draft_id=%s | error=%s",
            draft["id"],
            type(exc).__name__,
        )
        await query.edit_message_text(
            f"Не отправил сообщение для {draft['contact_alias']}. Возможно, контакт заблокировал бота. "
            "Черновик сохранён — можно попробовать кнопку ещё раз.",
            reply_markup=InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton("🔁 Повторить", callback_data=f"contact_send:{draft['id']}"),
                    InlineKeyboardButton("Отмена", callback_data=f"contact_cancel:{draft['id']}"),
                ]]
            ),
        )
        return
    memory.delete_pending_contact_message(int(draft["id"]), update.effective_user.id)
    sent_format = "голосовое" if delivery_kind == "voice" else "сообщение"
    await query.edit_message_text(
        f"Отправлено {sent_format} контакту {draft['contact_alias']}.\n\n{delivered_text}"
    )


async def send_delegated_contact_reply(update: Update, text: str, *, as_voice: bool) -> None:
    """Reply inside an active delegation, preferring voice only when the contact used voice."""
    if not as_voice:
        await update.message.reply_text(text)
        return
    try:
        await update.effective_chat.send_action(ChatAction.RECORD_VOICE)
        audio = await synthesize_voice_bytes(text)
        payload = BytesIO(audio)
        payload.name = "naz-delegated-reply.ogg"
        await update.message.reply_voice(
            voice=payload,
            caption="AI-голос Naz — помощник Назара",
        )
    except (RuntimeError, TelegramError):
        logger.warning("Delegated voice reply falling back to text")
        await update.message.reply_text(text)


async def handle_delegated_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    *,
    reply_as_voice: bool = False,
) -> bool:
    if not update.effective_user or not update.message:
        return False
    row = memory.get_active_delegation(update.effective_user.id)
    if not row:
        return False
    delegation_id = int(row["id"])
    delegation = delegation_from_row(row)
    if delegated_messaging.is_stop(text):
        await update.message.reply_text("Остановился. Контакт и переписка удалены.")
        await context.bot.send_message(chat_id=delegation.owner_user_id, text=f"Собеседник остановил поручение #{delegation_id}.")
        memory.purge_delegation(delegation_id, "contact_stopped")
        return True
    risks = delegated_messaging.assess_risk(text)
    memory.save_delegated_message(delegation_id, "contact", text)
    if risks:
        memory.set_delegation_status(delegation_id, "paused")
        await update.message.reply_text("Тут нужно подтверждение Назара. Я поставил разговор на паузу.")
        await context.bot.send_message(
            chat_id=delegation.owner_user_id,
            text=f"Поручение #{delegation_id} на паузе ({', '.join(risks)}). Ответить вручную: /delegate_reply {delegation_id} текст",
        )
        return True
    history = memory.get_delegated_history(delegation_id, limit=24)
    prompt = delegated_messaging.system_prompt(
        delegation=delegation,
        character_context=naz_character.dialogue_context(memory.load_character_state(delegation.owner_user_id)),
        history=history,
    )
    reply = await call_gpt(
        [{"role": "system", "content": prompt}, {"role": "user", "content": text}],
        max_tokens=min(MAX_TOKENS, 500),
    )
    if reply == "OWNER_CONFIRMATION_REQUIRED" or delegated_messaging.assess_risk(reply):
        memory.set_delegation_status(delegation_id, "paused")
        await update.message.reply_text("Мне нужно свериться с Назаром. Поставил разговор на паузу.")
        await context.bot.send_message(
            chat_id=delegation.owner_user_id,
            text=f"Naz остановил поручение #{delegation_id} для твоего ответа: /delegate_reply {delegation_id} текст",
        )
        return True
    await send_delegated_contact_reply(update, reply, as_voice=reply_as_voice)
    memory.save_delegated_message(delegation_id, "assistant", reply)
    turns = memory.increment_delegation_turns(delegation_id)
    if turns >= delegation.max_turns:
        await update.message.reply_text("На этом поручение завершено. Спасибо за разговор.")
        await context.bot.send_message(chat_id=delegation.owner_user_id, text=f"Поручение #{delegation_id} завершено по лимиту.")
        memory.purge_delegation(delegation_id, "turn_limit")
    return True


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.effective_user:
        await update.message.reply_text(help_commands_for(update.effective_user.id), reply_markup=HELP_KEYBOARD)


async def state_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user:
        return
    await reply_long(update, get_state_text(update.effective_user.id), main_keyboard_for(update.effective_user.id))


async def character_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user:
        return
    state = memory.load_character_state(update.effective_user.id)
    await reply_long(update, naz_character.format_status(state), main_keyboard_for(update.effective_user.id))


async def character_event_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Эта команда доступна только админу.")
        return
    allowed = sorted(naz_character.EVENT_DELTAS)
    if not context.args or context.args[0] not in naz_character.EVENT_DELTAS:
        await update.message.reply_text("Используй: /character_event <event>\n" + ", ".join(allowed))
        return
    state = memory.apply_character_event(update.effective_user.id, context.args[0])
    await update.message.reply_text(naz_character.format_status(state))


async def character_set_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Эта команда доступна только админу.")
        return
    if len(context.args) != 2 or context.args[0] not in naz_character.AXES:
        await update.message.reply_text(
            "Используй: /character_set <axis> <0-100>\n" + ", ".join(naz_character.AXES)
        )
        return
    try:
        value = int(context.args[1])
    except ValueError:
        await update.message.reply_text("Значение должно быть числом от 0 до 100.")
        return
    state = memory.set_character_axis(update.effective_user.id, context.args[0], value)
    await update.message.reply_text(naz_character.format_status(state))


async def character_simulate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    count = 10
    if context.args:
        try:
            count = max(1, min(30, int(context.args[0])))
        except ValueError:
            pass
    state = memory.load_character_state(update.effective_user.id)
    plans = naz_character.simulate(
        state,
        memory.get_recent_content_signatures(update.effective_user.id, limit=16),
        count=count,
    )
    lines = ["Naz simulation · состояние базы не изменено", ""]
    for index, plan in enumerate(plans, 1):
        lines.append(
            f"{index}. {plan['event']} → {plan['facet']} · {plan['state']}\n"
            f"   {plan['content_format_label']} / {plan['format']} / {plan['hook']}"
        )
    await reply_long(update, "\n".join(lines), main_keyboard_for(update.effective_user.id))


async def relationship_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(duo_relationship.format_status(memory.load_relationship_state()))


async def relationship_event_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not is_admin(update.effective_user.id):
        return
    if not context.args or context.args[0] not in duo_relationship.EVENT_DELTAS:
        await update.message.reply_text("Используй: /relationship_event <event>\n" + ", ".join(sorted(duo_relationship.EVENT_DELTAS)))
        return
    topic = " ".join(context.args[1:]).strip()
    state = memory.apply_relationship_event(context.args[0], topic=topic)
    await update.message.reply_text(duo_relationship.format_status(state))


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
    await reply_long(update, f"✅ Режим включён: {data['title']}\n{data['short']}", main_keyboard_for(update.effective_user.id))


async def voice_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user:
        return
    if not context.args:
        items = "\n".join(f"{key} — {data['title']}: {data['style']}" for key, data in VOICE_PROFILES.items())
        await reply_long(update, "🎭 Voice profiles:\n\n" + items + "\n\nПример:\n/voice tech_hooligan", main_keyboard_for(update.effective_user.id))
        return
    voice = context.args[0].strip().lower()
    if voice not in VOICE_PROFILES:
        items = "\n".join(VOICE_PROFILES.keys())
        await reply_long(update, f"Такого голоса нет: {voice}\n\nДоступно:\n{items}", main_keyboard_for(update.effective_user.id))
        return
    set_user_voice_profile(update.effective_user.id, voice)
    data = VOICE_PROFILES[voice]
    await reply_long(update, f"✅ Голос включён: {data['title']}\n{data['style']}", main_keyboard_for(update.effective_user.id))


async def goal_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user:
        return
    if not context.args:
        items = "\n".join(f"{key} — {data['title']}" for key, data in GOALS.items())
        await reply_long(update, "🎯 Content goals:\n\n" + items + "\n\nПример:\n/goal engagement", main_keyboard_for(update.effective_user.id))
        return
    goal = context.args[0].strip().lower()
    if goal not in GOALS:
        items = "\n".join(GOALS.keys())
        await reply_long(update, f"Такой цели нет: {goal}\n\nДоступно:\n{items}", main_keyboard_for(update.effective_user.id))
        return
    set_user_content_goal(update.effective_user.id, goal)
    data = GOALS[goal]
    await reply_long(update, f"✅ Цель включена: {data['title']}\n{data['prompt']}", main_keyboard_for(update.effective_user.id))


async def memory_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user:
        return
    if ADMIN_ID and not is_admin(update.effective_user.id):
        await reply_long(update, "🔒 Память и управление доступны только админу.", main_keyboard_for(update.effective_user.id))
        return
    await reply_long(update, memory.format_memory(update.effective_user.id), CONTROL_KEYBOARD)


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user:
        return
    memory.clear_user_memory(update.effective_user.id)
    await reply_long(update, "🧹 Готово. История диалога и заметки памяти очищены для тебя.", main_keyboard_for(update.effective_user.id))


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user:
        return
    if not is_admin(update.effective_user.id):
        await reply_long(update, "🔒 Статистика доступна только админу.", main_keyboard_for(update.effective_user.id))
        return
    await reply_long(update, memory.get_stats(), CONTROL_KEYBOARD)


def dialog_command_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if context.args:
        try:
            return int(context.args[0])
        except (TypeError, ValueError):
            pass
    return update.effective_user.id


async def dialog_context_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not is_admin(update.effective_user.id):
        await reply_long(update, "🔒 Команда доступна только администратору.", MAIN_KEYBOARD)
        return
    target_id = dialog_command_user_id(update, context)
    history = memory.get_history(target_id, limit=20)
    if not history:
        text = f"Диалог user_id={target_id} пуст."
    else:
        lines = [f"Последний контекст user_id={target_id}:"]
        lines.extend(f"{item['role']}: {item['content']}" for item in history)
        text = "\n\n".join(lines)
    await reply_long(update, text, CONTROL_KEYBOARD)


async def dialog_reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not is_admin(update.effective_user.id):
        await reply_long(update, "🔒 Команда доступна только администратору.", MAIN_KEYBOARD)
        return
    target_id = dialog_command_user_id(update, context)
    memory.clear_dialog_history(target_id)
    await reply_long(
        update,
        f"✅ Диалог user_id={target_id} очищен. Характер, настройки и контентная память сохранены.",
        CONTROL_KEYBOARD,
    )


async def vk_queue_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not is_admin(update.effective_user.id):
        await reply_long(update, "🔒 Команда доступна только администратору.", MAIN_KEYBOARD)
        return
    status = vk_publish_queue.queue_status(NAZ_VK_QUEUE_DIR)
    await reply_long(
        update,
        f"VK Publisher producer queue готова.\nPending: {status['path']}",
        CONTROL_KEYBOARD,
    )


@dataclass(frozen=True, slots=True)
class SyncResult:
    receipts_seen: int = 0
    history_inserted: int = 0
    already_recorded: int = 0
    invalid_receipts: int = 0


def sync_completed_naz_vk_jobs() -> SyncResult:
    """Reconcile only validated consumer publication receipts."""
    receipts, invalid_receipts = vk_publish_queue.publication_receipts(
        NAZ_VK_QUEUE_DIR,
        producer="naz",
    )
    inserted = 0
    already = 0
    invalid = invalid_receipts
    user_id = ADMIN_ID or 0
    for receipt in receipts:
        status = memory.reconcile_vk_publication_receipt(user_id, receipt)
        if status == "history_inserted":
            inserted += 1
        elif status == "already_recorded":
            already += 1
        else:
            invalid += 1
    return SyncResult(
        receipts_seen=len(receipts),
        history_inserted=inserted,
        already_recorded=already,
        invalid_receipts=invalid,
    )


async def create_naz_vk_job(
    topic: str,
    *,
    source_ref: str,
    not_before: Optional[datetime] = None,
    rubric_kind: str = "daily",
    slot: str = "",
) -> dict:
    if not NAZ_VK_ENABLED:
        raise vk_publish_queue.QueueError("NAZ_VK_ENABLED выключен")
    if not NAZ_VK_PUBLIC_ID:
        raise vk_publish_queue.QueueError("NAZ_VK_PUBLIC_ID не задан")
    if NAZ_VK_IMAGE_POLICY not in {"required", "text_music"}:
        raise vk_publish_queue.QueueError(
            "NAZ_VK_IMAGE_POLICY must be required or text_music"
        )
    user_id = ADMIN_ID or 0
    rubric_rows = [dict(rubric) for rubric in NAZ_VK_RUBRICS if rubric.get("kind") == rubric_kind]
    if not rubric_rows:
        raise vk_publish_queue.QueueError(f"неизвестный тип VK-рубрики: {rubric_kind}")
    for rubric in rubric_rows:
        rubric["key"] = naz_editorial_catalog.rubric_key(str(rubric["name"]))
    character = naz_character.apply_event(memory.load_character_state(user_id), "new_topic")
    plan = scheduled_plan(
        user_id=user_id,
        platform="vk",
        slot=slot or rubric_kind,
        seed=source_ref,
        rubric_rows=rubric_rows,
        source_rows=(
            {
                "source_ref": source_ref,
                "topic": topic,
                "source_type": "scheduled_topic",
            },
        ),
        character=character,
        persona_rubric_rows=NAZ_VK_RUBRICS,
    )
    memory.update_editorial_release_event(
        user_id=user_id,
        plan_id=plan.plan_id,
        platform="vk",
        slot=plan.slot,
        slot_captured_at=memory.utc_now(),
        generation_package_status="not_run",
        image_qa_status="not_run",
        history_commit_status="pending",
    )
    try:
        package = await generate_scheduled_package(plan, character)
    except ScheduledTechnicalFailure as exc:
        memory.update_editorial_release_event(
            user_id=user_id,
            plan_id=plan.plan_id,
            platform="vk",
            generation_package_status="invalid",
            history_commit_status="not_run",
        )
        raise vk_publish_queue.QueueError(
            "model returned an invalid generation package twice; VK job was not created"
        ) from exc
    except ScheduledContentReject as exc:
        memory.update_editorial_release_event(
            user_id=user_id,
            plan_id=plan.plan_id,
            platform="vk",
            generation_package_status="rejected",
            history_commit_status="not_run",
        )
        raise vk_publish_queue.QueueError(
            f"local quality gate rejected the planned release: {exc}"
        ) from exc
    text = package.final_text
    memory.update_editorial_release_event(
        user_id=user_id,
        plan_id=plan.plan_id,
        platform="vk",
        generation_package_status="accepted",
        image_qa_status="not_run",
    )
    rubric = next(item for item in rubric_rows if str(item["name"]) == plan.rubric)
    image_count = max(1, min(int(str(rubric.get("image_count") or "1")), 4))
    image_topic = f"{plan.rubric}. {plan.topic}" if image_count > 1 else plan.topic
    images, _ = await generate_images_with_retries(
        user_id,
        image_topic,
        text,
        count=image_count,
        attempts=NAZ_VK_IMAGE_ATTEMPTS,
        platform="vk",
        editorial_visual_brief=editorial_orchestrator.package_visual_brief(plan, package),
    )
    if not images and NAZ_VK_IMAGE_POLICY == "required":
        memory.update_editorial_release_event(
            user_id=user_id,
            plan_id=plan.plan_id,
            platform="vk",
            history_commit_status="not_run",
        )
        raise vk_publish_queue.QueueError(
            "VK image policy requires media; job was not enqueued"
        )
    if image_count > 1 and len(images) != image_count:
        memory.update_editorial_release_event(
            user_id=user_id,
            plan_id=plan.plan_id,
            platform="vk",
            history_commit_status="not_run",
        )
        raise vk_publish_queue.QueueError(
            "MATERIAL requires a complete three-frame sequence; job was not enqueued"
        )
    if not images:
        logger.info("VK text+music job allowed by explicit NAZ_VK_IMAGE_POLICY")
    media = [
        vk_publish_queue.MediaInput(f"image-{index}.png", image)
        for index, image in enumerate(images, start=1)
    ]
    now = datetime.now(ZoneInfo(NAZ_VK_TIMEZONE))
    try:
        job = naz_vk_music.enqueue_with_track_rotation(
            NAZ_VK_TRACK_STATE_FILE,
            requested_tags=plan.track_tags,
            seed=plan.plan_id,
            post_topic=plan.topic,
            shared_history_file=NAZ_VK_QUEUE_DIR / "recent-tracks.json",
            enqueue_job=lambda track_query: vk_publish_queue.enqueue(
                NAZ_VK_QUEUE_DIR,
                target_group_id=NAZ_VK_PUBLIC_ID,
                text=text,
                media=media,
                track_query=track_query,
                created_at=now,
                not_before=not_before,
                dedupe_key=hashlib.sha256(f"naz|{NAZ_VK_PUBLIC_ID}|{source_ref}".encode("utf-8")).hexdigest(),
                source_ref=source_ref,
                plan_id=plan.plan_id,
                editorial={
                    **safe_vk_editorial_metadata(plan),
                    "generation_package_status": "accepted",
                    "image_qa_status": "not_run",
                },
            ),
        )
    except vk_publish_queue.DuplicateJobError:
        # The canonical existing job remains authoritative and may still be
        # awaiting its publication receipt.
        raise
    except Exception:
        memory.update_editorial_release_event(
            user_id=user_id,
            plan_id=plan.plan_id,
            platform="vk",
            history_commit_status="failed",
        )
        raise
    plan_dict = plan.to_dict()
    memory.save_generated_post(
        user_id=user_id,
        expert_mode=get_user_expert_mode(user_id),
        task=f"naz_vk_queue:{rubric_kind}:{plan.rubric}",
        topic=plan.topic,
        content=text,
        image_count=len(media),
        published_to_channel=False,
        semantic_theme=plan.semantic_theme,
        semantic_card=plan.semantic_card,
        external_job_id=str(job["job_id"]),
        plan_id=plan.plan_id,
        editorial_plan=plan_dict,
    )
    memory.update_editorial_release_event(
        user_id=user_id,
        plan_id=plan.plan_id,
        platform="vk",
        vk_job_id=str(job["job_id"]),
        history_commit_status="pending",
    )
    return job


async def vk_queue_draft_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not is_admin(update.effective_user.id):
        await reply_long(update, "🔒 Команда доступна только администратору.", MAIN_KEYBOARD)
        return
    topic = extract_topic(update, context, default="AI, контент и автоматизация")
    await send_typing(update)
    try:
        job = await create_naz_vk_job(topic, source_ref=f"manual:{topic.strip().lower()}")
        await reply_long(update, f"✅ Задание VK поставлено: {job['job_id']}", CONTROL_KEYBOARD)
    except vk_publish_queue.DuplicateJobError:
        await reply_long(update, "ℹ️ Такое задание уже находится в очереди.", CONTROL_KEYBOARD)
    except Exception as exc:  # noqa: BLE001
        logger.exception("VK queue draft failed")
        await reply_long(update, f"⚠️ Не удалось поставить задание VK в очередь: {exc}", CONTROL_KEYBOARD)


async def content_command(update: Update, context: ContextTypes.DEFAULT_TYPE, task: str) -> None:
    if not update.effective_user or not update.message:
        return
    user_id = update.effective_user.id
    if not has_registered_access(user_id):
        await update.message.reply_text("🔒 Генерация контента доступна владельцу и сохранённым контактам.", reply_markup=ReplyKeyboardRemove())
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


async def gaming_plan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if await reject_unregistered_user(update):
        return
    topic = extract_topic(update, context, default="игры как пространство для экспериментов")
    plan = gaming_vertical.plan_gaming_content(
        "naz", topic, memory.get_recent_content_signatures(update.effective_user.id), platform="telegram"
    )
    await update.message.reply_text(
        f"🎮 Игровой план Naz\n\nРубрика: {plan['intent']}\nФормат: {plan['format']}\n"
        f"Коммерческий угол: {plan['commercial_angle']}\nТема: {topic}"
    )


async def gaming_command(update: Update, context: ContextTypes.DEFAULT_TYPE, *, commercial: bool = False) -> None:
    if not update.effective_user or not update.message:
        return
    if not has_registered_access(update.effective_user.id):
        await update.message.reply_text("🔒 Игровые черновики доступны владельцу и сохранённым контактам.")
        return
    topic = extract_topic(update, context, default="игры как пространство для экспериментов")
    recent = memory.get_recent_content_signatures(update.effective_user.id)
    plan = gaming_vertical.plan_gaming_content("naz", topic, recent, platform="telegram", commercial=commercial)
    await send_typing(update)
    try:
        result = await generate_content(
            update.effective_user.id,
            topic,
            "post",
            extra_instruction=gaming_vertical.prompt_context("naz", plan),
        )
        await reply_long(update, result, CONTENT_KEYBOARD)
        await update.message.reply_text(
            f"🎮 {plan['intent']} · {plan['format']} · {plan['commercial_angle']}\n"
            "Это черновик: игровая автопубликация пока выключена."
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("gaming_command failed")
        await update.message.reply_text(f"⚠️ Игровой черновик не получился: {exc}")


async def gaming_draft_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await gaming_command(update, context, commercial=False)


async def gaming_commercial_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await gaming_command(update, context, commercial=True)


async def hooks_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await content_command(update, context, "hooks")


async def insight_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    user_id = update.effective_user.id
    if not has_registered_access(user_id):
        await update.message.reply_text("🔒 Рубрика инсайтов доступна владельцу и сохранённым контактам.", reply_markup=ReplyKeyboardRemove())
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
    await reply_long(update, "🚀 Собираю рубрику из историй Naz и публикую в канал.")

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
        queue_naz_post_for_void(post_text, source="publish_insight", topic=topic or "Naz Stories")
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
        queue_naz_post_for_void(post_text, source="publish_source", topic=item.get("title", ""))
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
        queue_naz_post_for_void(post_text, source="publish_agent_content", topic=f"content-agent {date_text}")
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

    safe_context, risks, resolved_date = collect_agent_materials(
        date_text, "ежедневный импорт content-agent"
    )
    if not safe_context:
        return f"⚠️ Agent Content: нет безопасных материалов за {date_text}."
    source_ref = f"agent_content:{resolved_date}:{manifest_hash}"
    character = naz_character.apply_event(memory.load_character_state(user_id), "new_topic")
    source_row = chronicle_source_row(
        source_ref=source_ref,
        safe_context=safe_context,
        risks=risks,
        topic=f"рабочая хроника Naz {resolved_date}",
    )
    plan = scheduled_plan(
        user_id=user_id,
        platform="telegram",
        slot="agent_content_sync",
        seed=source_ref,
        rubric_rows=(
            {
                "key": "agent_content",
                "name": "Рабочая хроника Naz",
                "kind": "work_chronicle",
                "angle": "turn a verified work episode into one coherent release without exposing private material",
                "track_tags": "daily,focus,builder,reflective",
            },
        ),
        source_rows=(source_row,),
        character=character,
    )
    memory.update_editorial_release_event(
        user_id=user_id,
        plan_id=plan.plan_id,
        platform="telegram",
        slot=plan.slot,
        slot_captured_at=memory.utc_now(),
        generation_package_status="not_run",
        image_qa_status="not_run",
        history_commit_status="pending" if publish else "not_run",
    )
    if plan.production_mode == "story_first":
        pack_dir = await asyncio.to_thread(
            story_first_dry_run,
            plan,
            tuple(source_row.get("safe_facts", ())),
        )
        await notify_admin(
            bot,
            f"📦 Agent Content {resolved_date}: Story-first dry-run подготовлен идемпотентно для plan_id {plan.plan_id}. Renderer unavailable; публичная публикация отключена.",
        )
        mark_agent_content_seen(resolved_date, manifest_hash)
        logger.info("STORY_FIRST dry-run ready | plan_id=%s | path=%s", plan.plan_id, pack_dir)
        return f"✅ Agent Content {resolved_date}: Story-first dry-run ready; renderer unavailable."

    try:
        package = await generate_scheduled_package(
            plan,
            character,
            source_material=safe_context,
        )
    except ScheduledTechnicalFailure:
        memory.update_editorial_release_event(
            user_id=user_id,
            plan_id=plan.plan_id,
            platform="telegram",
            generation_package_status="invalid",
            history_commit_status="not_run",
        )
        await notify_admin(
            bot,
            f"⚠️ Agent Content {resolved_date}: модель дважды вернула технически непригодный пакет. Черновик и публичный DIAG не созданы.",
        )
        return f"⚠️ Agent Content {resolved_date}: technical generation failure."
    except ScheduledContentReject as exc:
        memory.update_editorial_release_event(
            user_id=user_id,
            plan_id=plan.plan_id,
            platform="telegram",
            generation_package_status="rejected",
            history_commit_status="not_run",
        )
        await notify_admin(
            bot,
            f"⚠️ Agent Content {resolved_date}: локальная quality-проверка отклонила пакет ({exc}). История не изменена.",
        )
        return f"⚠️ Agent Content {resolved_date}: local quality reject."

    plan_dict = plan.to_dict()
    memory.update_editorial_release_event(
        user_id=user_id,
        plan_id=plan.plan_id,
        platform="telegram",
        generation_package_status="accepted",
        image_qa_status="not_run",
    )
    if not publish:
        memory.save_generated_post(
            user_id=user_id,
            expert_mode=get_user_expert_mode(user_id),
            task="agent_content_editor",
            topic=plan.topic,
            content=package.final_text,
            image_count=0,
            published_to_channel=False,
            semantic_theme=plan.semantic_theme,
            semantic_card=plan.semantic_card,
            plan_id=plan.plan_id,
            editorial_plan=plan_dict,
        )
        await notify_admin(
            bot,
            f"📥 Agent Content {resolved_date}: orchestrated draft готов для plan_id {plan.plan_id}.\n\n"
            f"{package.final_text[:3200]}\n\nДля публикации: /publish_agent_content {resolved_date}",
        )
        mark_agent_content_seen(resolved_date, manifest_hash)
        return f"✅ Agent Content {resolved_date}: orchestrated draft imported."

    images, _ = await generate_images_with_retries(
        user_id,
        f"{plan.rubric}. {plan.topic}",
        package.final_text,
        count=CHANNEL_IMAGE_COUNT,
        editorial_visual_brief=editorial_orchestrator.package_visual_brief(plan, package),
    )
    if REQUIRE_IMAGES_FOR_CHANNEL_POSTS and not images:
        memory.update_editorial_release_event(
            user_id=user_id,
            plan_id=plan.plan_id,
            platform="telegram",
            history_commit_status="not_run",
        )
        await notify_admin(bot, f"⚠️ Agent Content {resolved_date}: planned visual failed. Publication skipped; history unchanged.")
        return f"⚠️ Agent Content {resolved_date}: images failed."
    receipt = await send_observed_scheduled_post(
        bot=bot,
        chat_id=CHANNEL_ID,
        post_text=package.final_text,
        images=images,
        user_id=user_id,
        plan_id=plan.plan_id,
    )
    memory.save_generated_post(
        user_id=user_id,
        expert_mode=get_user_expert_mode(user_id),
        task="agent_content_auto_publish",
        topic=plan.topic,
        content=package.final_text,
        image_count=len(images),
        published_to_channel=True,
        semantic_theme=plan.semantic_theme,
        semantic_card=plan.semantic_card,
        plan_id=plan.plan_id,
        editorial_plan=plan_dict,
    )
    memory.record_content_signature(user_id, plan_dict, plan.topic)
    memory.update_editorial_release_event(
        user_id=user_id,
        plan_id=plan.plan_id,
        platform="telegram",
        telegram_chat_id=receipt.chat_id,
        telegram_message_id=receipt.message_id,
        history_commit_status="committed",
    )
    memory.save_character_state(user_id, character)
    memory.apply_character_event(user_id, "publish")
    queue_naz_post_for_void(package.final_text, source="agent_content_auto_publish", topic=plan.topic)
    mark_agent_content_seen(resolved_date, manifest_hash)
    return f"✅ Agent Content {resolved_date}: imported and published."


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
    if not has_registered_access(user_id):
        await update.message.reply_text("🔒 Генерация image-post доступна владельцу и сохранённым контактам.", reply_markup=ReplyKeyboardRemove())
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
        logger.info("Image generation completed | image_count=%s", len(images))
    except Exception as exc:  # noqa: BLE001
        logger.exception("imagepost failed")
        await reply_long(update, f"⚠️ Image-post не собрался. Причина: {exc}", CONTENT_KEYBOARD)


async def image_only_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    user_id = update.effective_user.id
    if not has_registered_access(user_id):
        await update.message.reply_text("🔒 Генерация картинок доступна владельцу и сохранённым контактам.", reply_markup=ReplyKeyboardRemove())
        return

    topic = extract_topic(update, context, default="AI content automation cinematic poster")
    await update.effective_chat.send_action(ChatAction.UPLOAD_PHOTO)
    prompt = build_naz_direct_image_prompt(topic)
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
        queue_naz_post_for_void(post_text, source="publish", topic=topic)
        await reply_long(update, "✅ Опубликовано в канал.", MAIN_KEYBOARD)
    except Exception as exc:  # noqa: BLE001
        logger.exception("publish failed")
        await reply_long(update, f"⚠️ Не смог опубликовать. Причина: {exc}", MAIN_KEYBOARD)


async def void_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if not is_admin(update.effective_user.id):
        await reply_long(update, "🔒 Приватный разговор с VOID доступен только админу.", MAIN_KEYBOARD)
        return

    void_text = extract_void_text(update, context)
    if not void_text:
        await reply_long(update, "Пришли так: /void текст Void\nИли ответь /void на сообщение Void.", MAIN_KEYBOARD)
        return

    await send_typing(update)
    try:
        post_text, risks = await generate_void_crosspost(update.effective_user.id, void_text)
        prefix = "🕳 Мысли после разговора · draft"
        if risks:
            prefix += "\n⚠️ Риски найдены: " + ", ".join(risks)
        await reply_long(update, f"{prefix}\n\n{post_text}", CONTENT_KEYBOARD)
    except Exception as exc:  # noqa: BLE001
        logger.exception("void crosspost failed")
        await reply_long(update, f"⚠️ Не смог собрать мысль после разговора. Причина: {exc}", MAIN_KEYBOARD)


async def publish_void_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if not is_admin(update.effective_user.id):
        await reply_long(update, "🔒 Публикация рубрики доступна только админу.", MAIN_KEYBOARD)
        return
    if not CHANNEL_ID:
        await reply_long(update, "⚠️ CHANNEL_ID не задан в .env/Replit Secrets.", MAIN_KEYBOARD)
        return

    void_text = extract_void_text(update, context)
    if not void_text:
        await reply_long(update, "Пришли так: /publish_void текст Void\nИли ответь /publish_void на сообщение Void.", MAIN_KEYBOARD)
        return

    await reply_long(update, "🕳 Naz переваривает приватный разговор с VOID и собирает собственную мысль.")
    try:
        post_text, risks = await generate_void_crosspost(update.effective_user.id, void_text, save_generated=False)
        if risks or "НЕ ПУБЛИКОВАТЬ АВТОМАТИЧЕСКИ" in post_text.upper():
            reason = ", ".join(risks) if risks else "модель пометила материал как рискованный"
            await reply_long(update, f"⚠️ Не публикую выпуск: {reason}\n\n{post_text}", MAIN_KEYBOARD)
            return

        images, _ = await generate_images_with_retries(update.effective_user.id, "Void Entity crosspost", post_text, count=CHANNEL_IMAGE_COUNT)
        if REQUIRE_IMAGES_FOR_CHANNEL_POSTS and not images:
            await reply_long(update, "⚠️ Выпуск готов, но картинка не собралась. В канал без изображения не публикую.", MAIN_KEYBOARD)
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
        await reply_long(update, "✅ Выпуск «Мысли после разговора» опубликован.", MAIN_KEYBOARD)
    except Exception as exc:  # noqa: BLE001
        logger.exception("publish_void failed")
        await reply_long(update, f"⚠️ Не смог опубликовать Void-кросспост. Причина: {exc}", MAIN_KEYBOARD)


async def thought_to_void_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if not is_admin(update.effective_user.id):
        await reply_long(update, "🔒 Приватный разговор доступен только админу.", MAIN_KEYBOARD)
        return
    thought = extract_void_text(update, context)
    if len(thought) < 40:
        await reply_long(update, "Используй: /thought_to_void приватная мысль Naz для VOID", MAIN_KEYBOARD)
        return
    try:
        path = queue_naz_private_thought_for_void(
            thought,
            source="manual_private_conversation",
            topic="мысль после разговора",
        )
    except ValueError as exc:
        await reply_long(update, f"⚠️ Мысль не передана: {exc}", MAIN_KEYBOARD)
        return
    await reply_long(
        update,
        f"✅ Приватная мысль передана VOID: {path.name if path else 'exchange disabled'}. "
        "Это не опубликованный пост; VOID должен самостоятельно её переварить.",
        MAIN_KEYBOARD,
    )


# -----------------------------------------------------------------------------
# Bot-to-bot file exchange
# -----------------------------------------------------------------------------


def exchange_dir(direction: str, box: str = "inbox") -> Path:
    return CROSSPOST_EXCHANGE_DIR / direction / box


def ensure_exchange_dirs() -> None:
    for direction in ("void_to_naz", "naz_to_void"):
        for box in ("inbox", "processed", "failed"):
            exchange_dir(direction, box).mkdir(parents=True, exist_ok=True)


def exchange_payload_id(source: str, text: str) -> str:
    raw = f"{source}|{datetime.now(ZoneInfo(BOT_TIMEZONE)).isoformat()}|{text[:500]}"
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:16]


def write_exchange_payload(direction: str, payload: Dict[str, str]) -> Optional[Path]:
    if not CROSSPOST_EXCHANGE_ENABLED:
        return None
    ensure_exchange_dirs()
    payload_id = payload.get("id") or exchange_payload_id(payload.get("source", "naz"), payload.get("text", ""))
    payload["id"] = payload_id
    payload["created_at"] = payload.get("created_at") or datetime.now(ZoneInfo(BOT_TIMEZONE)).isoformat()
    target = exchange_dir(direction, "inbox") / f"{payload_id}.json"
    temp = target.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, target)
    logger.info("Exchange queued | direction=%s | file=%s", direction, target)
    return target


def move_exchange_file(path: Path, direction: str, box: str) -> None:
    target_dir = exchange_dir(direction, box)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / path.name
    if target.exists():
        target = target_dir / f"{path.stem}-{int(datetime.now().timestamp())}{path.suffix}"
    os.replace(path, target)


def exchange_file_count(direction: str, box: str) -> int:
    path = exchange_dir(direction, box)
    if not path.exists():
        return 0
    return sum(1 for item in path.glob("*.json") if item.is_file())


def exchange_status_text() -> str:
    ensure_exchange_dirs()
    return (
        "🔗 Связь Naz ↔ Void\n\n"
        f"Статус: {'включена' if CROSSPOST_EXCHANGE_ENABLED else 'выключена'}\n"
        f"Автопубликация: {'включена' if CROSSPOST_EXCHANGE_AUTO_PUBLISH else 'draft-режим'}\n"
        f"Папка: {CROSSPOST_EXCHANGE_DIR}\n"
        f"Интервал: {CROSSPOST_EXCHANGE_INTERVAL_SECONDS} сек\n"
        f"За проход: {CROSSPOST_EXCHANGE_MAX_PER_RUN}\n\n"
        "Void → Naz:\n"
        f"• ждёт: {exchange_file_count('void_to_naz', 'inbox')}\n"
        f"• обработано: {exchange_file_count('void_to_naz', 'processed')}\n"
        f"• ошибки: {exchange_file_count('void_to_naz', 'failed')}\n\n"
        "Naz → Void:\n"
        f"• ждёт: {exchange_file_count('naz_to_void', 'inbox')}\n"
        f"• обработано: {exchange_file_count('naz_to_void', 'processed')}\n"
        f"• ошибки: {exchange_file_count('naz_to_void', 'failed')}\n\n"
        "Ручные команды:\n"
        "/void текст — собрать черновик Void → Naz\n"
        "/publish_void текст — опубликовать Void → Naz"
    )


def queue_naz_post_for_void(post_text: str, *, source: str, topic: str = "") -> None:
    # Published material is never fed to VOID as relationship input.  The
    # private-thought command below is the only outbound conversational route.
    logger.debug("Published Naz post not queued for VOID | source=%s | topic=%s", source, topic)


def queue_naz_private_thought_for_void(thought: str, *, source: str, topic: str = "") -> Optional[Path]:
    safe_thought = redact_sensitive_text(thought.strip())
    if len(safe_thought) < 40:
        raise ValueError("private thought is too short")
    relationship = memory.apply_relationship_event("challenge", topic=topic or safe_thought[:200])
    payload = duo_relationship.build_private_thought_payload(
        speaker="naz",
        thought=safe_thought,
        topic=topic or "мысль после разговора",
        relationship=relationship,
        source_kind=source,
    )
    payload.update({
        "id": payload["thought_id"],
        "source": "naz_ai_bot",
        "source_event": source,
        "exchange_kind": "private_thought",
        "text": payload["thought"],
        "publish_mode": "auto" if CROSSPOST_EXCHANGE_AUTO_PUBLISH else "draft",
        "adaptation_role": "void_original_reflection_after_private_conversation",
    })
    memory.save_private_thought(payload, status="queued")
    return write_exchange_payload("naz_to_void", payload)


async def process_void_to_naz_exchange(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not CROSSPOST_EXCHANGE_ENABLED:
        return
    if not CHANNEL_ID:
        logger.warning("Exchange skipped: CHANNEL_ID empty")
        return

    ensure_exchange_dirs()
    admin_user_id = ADMIN_ID or 0
    for path in sorted(exchange_dir("void_to_naz", "inbox").glob("*.json"))[:CROSSPOST_EXCHANGE_MAX_PER_RUN]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("source") == "naz_ai_bot":
                move_exchange_file(path, "void_to_naz", "processed")
                continue
            void_text = str(payload.get("text") or payload.get("post") or "").strip()
            if len(void_text) < 40:
                raise ValueError("empty or too short Void payload")

            latest_risks: List[str] = []
            source_ref = f"void_exchange:{path.name}"

            async def generate(instruction: str) -> str:
                nonlocal latest_risks
                candidate, latest_risks = await generate_void_crosspost(
                    admin_user_id,
                    void_text,
                    save_generated=False,
                    payload=payload,
                    extra_instruction=instruction,
                )
                return candidate

            theme, semantic_result = await generate_semantic_autopost_candidate(
                user_id=admin_user_id,
                platform="telegram",
                rubric_name="Мысли после разговора",
                seed=source_ref,
                generate=generate,
            )
            if not semantic_result.accepted:
                raise ValueError("semantic gate rejected both Void → Naz generations")
            post_text = semantic_result.text
            risks = latest_risks
            if risks or "НЕ ПУБЛИКОВАТЬ АВТОМАТИЧЕСКИ" in post_text.upper():
                raise ValueError(", ".join(risks) if risks else "model marked payload as risky")

            if payload.get("publish_mode", "auto") != "auto" or not CROSSPOST_EXCHANGE_AUTO_PUBLISH:
                await notify_admin(context.bot, f"🕳 Void → Naz draft\n\n{post_text}")
                memory.save_generated_post(
                    user_id=admin_user_id,
                    expert_mode=get_user_expert_mode(admin_user_id),
                    task="exchange_void_to_naz_draft",
                    topic=str(payload.get("topic") or "Void Entity crosspost"),
                    content=post_text,
                    image_count=0,
                    published_to_channel=False,
                    semantic_theme=theme.key,
                )
                commit_accepted_autopost_state(
                    user_id=admin_user_id,
                    topic=str(payload.get("topic") or "Void Entity crosspost"),
                    task="void_crosspost",
                    platform="telegram",
                    source_ref=source_ref,
                    theme=theme,
                    result=semantic_result,
                )
                move_exchange_file(path, "void_to_naz", "processed")
                continue

            images, _ = await generate_images_with_retries(admin_user_id, "Void Entity crosspost", post_text, count=CHANNEL_IMAGE_COUNT)
            if REQUIRE_IMAGES_FOR_CHANNEL_POSTS and not images:
                raise ValueError("images required but not generated")

            await send_post_with_images(context.bot, CHANNEL_ID, post_text, images)
            memory.save_generated_post(
                user_id=admin_user_id,
                expert_mode=get_user_expert_mode(admin_user_id),
                task="exchange_void_to_naz",
                topic=str(payload.get("topic") or "Void Entity crosspost"),
                content=post_text,
                image_count=len(images),
                published_to_channel=True,
                semantic_theme=theme.key,
            )
            commit_accepted_autopost_state(
                user_id=admin_user_id,
                topic=str(payload.get("topic") or "Void Entity crosspost"),
                task="void_crosspost",
                platform="telegram",
                source_ref=source_ref,
                theme=theme,
                result=semantic_result,
            )
            move_exchange_file(path, "void_to_naz", "processed")
            logger.info("Exchange published | void_to_naz | file=%s", path.name)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Exchange void_to_naz failed | file=%s | %s", path.name, exc)
            try:
                move_exchange_file(path, "void_to_naz", "failed")
            except Exception:
                logger.exception("Exchange failed file move failed | file=%s", path)


def _crosspost_plan_id(payload: Dict[str, Any], source_ref: str) -> str:
    candidate = str(payload.get("plan_id") or payload.get("thought_id") or "").strip()
    if re.fullmatch(r"[A-Za-z0-9._:-]{8,64}", candidate):
        return candidate
    return hashlib.sha256(f"naz|crosspost|{source_ref}".encode("utf-8")).hexdigest()[:24]


def _exchange_file_ref(path: Path) -> str:
    return hashlib.sha256(path.name.encode("utf-8")).hexdigest()[:12]


_CROSSPOST_MEANING_LEXICON = (
    ("attention", ("attention", "вниман")),
    ("boundaries", ("boundar", "границ")),
    ("choice", ("choice", "выбор")),
    ("consequence", ("consequence", "последств")),
    ("control", ("control", "контрол")),
    ("craft", ("craft", "ремесл", "мастерств")),
    ("failure", ("failure", "ошиб", "сбой")),
    ("human cost", ("human cost", "цена для человека", "человеческ")),
    ("memory", ("memory", "памят")),
    ("responsibility", ("responsib", "ответствен")),
    ("relationship", ("relationship", "отношен")),
    ("silence", ("silence", "тишин", "молчан")),
    ("technology", ("technolog", "технолог", "систем", "алгорит")),
    ("time", ("time", "врем")),
    ("trust", ("trust", "довер")),
    ("uncertainty", ("uncertain", "неопредел", "сомнен")),
    ("work", ("work", "работ", "проект")),
)


def _bounded_crosspost_source(payload: Dict[str, Any], source_text: str) -> str:
    """Build a closed-vocabulary semantic digest with no private excerpts."""
    folded = " ".join(
        f"{payload.get('topic') or ''} {source_text}".casefold().split()
    )
    meanings = [
        label
        for label, stems in _CROSSPOST_MEANING_LEXICON
        if any(stem in folded for stem in stems)
    ][:8]
    if not meanings:
        meanings = ["uncertainty", "human consequence"]
    return (
        "Private conversation source. No quotation or personal detail is available.\n"
        f"Safe semantic meanings: {', '.join(meanings)}.\n"
        "Create a standalone Naz reflection with a new concrete public scene and conclusion."
    )


def _crosspost_publication_privacy_risks(text: str) -> tuple[str, ...]:
    """Return labels only; callers never log matching private values."""
    return tuple(label for label, pattern in PERSONAL_DATA_PATTERNS if pattern.search(text))


async def process_void_to_naz_scheduled_exchange(
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Scheduled crosspost route through EditorialPlan and GenerationPackage."""
    if not CROSSPOST_EXCHANGE_ENABLED:
        return
    if not CHANNEL_ID:
        logger.warning("Exchange skipped: CHANNEL_ID empty")
        return

    ensure_exchange_dirs()
    admin_user_id = ADMIN_ID or 0
    inbox = sorted(exchange_dir("void_to_naz", "inbox").glob("*.json"))
    for path in inbox[:CROSSPOST_EXCHANGE_MAX_PER_RUN]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("invalid Void exchange payload")
            if payload.get("source") == "naz_ai_bot":
                move_exchange_file(path, "void_to_naz", "processed")
                continue
            source_text = str(
                payload.get("text") or payload.get("post") or payload.get("thought") or ""
            ).strip()
            if len(source_text) < 40:
                raise ValueError("empty or too short Void payload")
            source_topic = str(payload.get("topic") or "")
            if detect_content_risks(f"{source_topic}\n{source_text}"):
                raise ValueError("Void exchange source failed private-data safety policy")
            source_ref = (
                "void_exchange:"
                + hashlib.sha256(path.name.encode("utf-8")).hexdigest()[:24]
            )
            crosspost_plan_id = _crosspost_plan_id(payload, source_ref)
            previous_delivery = memory.get_editorial_release_event(
                admin_user_id, crosspost_plan_id, "telegram"
            )
            previous_status = str(
                (previous_delivery or {}).get("history_commit_status") or ""
            )
            if previous_status == "committed":
                move_exchange_file(path, "void_to_naz", "processed")
                logger.info(
                    "Exchange already committed | void_to_naz | plan_id=%s",
                    crosspost_plan_id,
                )
                continue
            if previous_status in {"sending", "failed"}:
                raise RuntimeError("crosspost delivery state requires audit")
            if payload.get("schema") == "private_thought.v1":
                valid, reason = duo_relationship.validate_private_thought_payload(payload)
                if not valid or payload.get("receiver") != "naz":
                    raise ValueError(reason or "private thought is addressed to another persona")
                memory.save_private_thought(payload, status="received")
            if isinstance(payload.get("relationship_snapshot"), dict):
                memory.save_relationship_state(
                    duo_relationship.normalize_state(payload["relationship_snapshot"])
                )
            memory.apply_relationship_event(
                "challenge",
                topic=str(payload.get("topic") or "private conversation")[:200],
            )

            # The private topic remains transient grounding only. Persisted
            # plan/draft metadata carries a generic safe label.
            topic = "Private conversation with VOID"
            character = naz_character.apply_event(
                memory.load_character_state(admin_user_id), "new_topic"
            )
            plan = scheduled_plan(
                user_id=admin_user_id,
                platform="telegram",
                slot="crosspost_exchange",
                seed=source_ref,
                rubric_rows=(
                    {
                        "key": "void_exchange",
                        "name": "Thought after a conversation",
                        "kind": "crosspost",
                        "angle": "create an original Naz reflection after a private VOID conversation",
                        "track_tags": "daily,reflective,dialogue",
                    },
                ),
                source_rows=(
                    {
                        "source_ref": source_ref,
                        "topic": topic,
                        "source_type": "private_crosspost",
                        "rubric_keys": ("void_exchange",),
                    },
                ),
                character=character,
                crosspost_plan_id=crosspost_plan_id,
            )
            publish = (
                payload.get("publish_mode", "auto") == "auto"
                and CROSSPOST_EXCHANGE_AUTO_PUBLISH
            )
            memory.update_editorial_release_event(
                user_id=admin_user_id,
                plan_id=plan.plan_id,
                platform="telegram",
                slot=plan.slot,
                slot_captured_at=memory.utc_now(),
                generation_package_status="not_run",
                image_qa_status="not_run",
                history_commit_status="pending" if publish else "not_run",
            )
            try:
                package = await generate_scheduled_package(
                    plan,
                    character,
                    source_material=_bounded_crosspost_source(payload, source_text),
                )
            except ScheduledTechnicalFailure:
                memory.update_editorial_release_event(
                    user_id=admin_user_id,
                    plan_id=plan.plan_id,
                    platform="telegram",
                    generation_package_status="invalid",
                    history_commit_status="not_run",
                )
                raise
            except ScheduledContentReject:
                memory.update_editorial_release_event(
                    user_id=admin_user_id,
                    plan_id=plan.plan_id,
                    platform="telegram",
                    generation_package_status="rejected",
                    history_commit_status="not_run",
                )
                raise
            if _crosspost_publication_privacy_risks(package.final_text):
                memory.update_editorial_release_event(
                    user_id=admin_user_id,
                    plan_id=plan.plan_id,
                    platform="telegram",
                    generation_package_status="rejected",
                    history_commit_status="not_run",
                )
                raise ScheduledContentReject("crosspost privacy policy")
            original, originality_reason = duo_relationship.reflection_is_original(
                redact_sensitive_text(source_text), package.final_text
            )
            if not original:
                memory.update_editorial_release_event(
                    user_id=admin_user_id,
                    plan_id=plan.plan_id,
                    platform="telegram",
                    generation_package_status="rejected",
                    history_commit_status="not_run",
                )
                raise ScheduledContentReject(
                    f"crosspost originality: {originality_reason}"
                )
            memory.update_editorial_release_event(
                user_id=admin_user_id,
                plan_id=plan.plan_id,
                platform="telegram",
                generation_package_status="accepted",
                image_qa_status="not_run",
            )
            plan_dict = plan.to_dict()

            if not publish:
                await notify_admin(
                    context.bot,
                    f"VOID to Naz draft\n\n{package.final_text}",
                )
                memory.save_generated_post(
                    user_id=admin_user_id,
                    expert_mode=get_user_expert_mode(admin_user_id),
                    task="exchange_void_to_naz_draft",
                    topic=topic,
                    content=package.final_text,
                    image_count=0,
                    published_to_channel=False,
                    semantic_theme=plan.semantic_theme,
                    semantic_card=plan.semantic_card,
                    plan_id=plan.plan_id,
                    editorial_plan=plan_dict,
                )
                move_exchange_file(path, "void_to_naz", "processed")
                continue

            images, _ = await generate_images_with_retries(
                admin_user_id,
                topic,
                package.final_text,
                count=CHANNEL_IMAGE_COUNT,
                editorial_visual_brief=editorial_orchestrator.package_visual_brief(
                    plan, package
                ),
            )
            if REQUIRE_IMAGES_FOR_CHANNEL_POSTS and not images:
                memory.update_editorial_release_event(
                    user_id=admin_user_id,
                    plan_id=plan.plan_id,
                    platform="telegram",
                    history_commit_status="not_run",
                )
                raise ValueError("images required but not generated")
            delivery_claim = memory.claim_editorial_delivery(
                user_id=admin_user_id,
                plan_id=plan.plan_id,
                platform="telegram",
            )
            if delivery_claim == "committed":
                move_exchange_file(path, "void_to_naz", "processed")
                continue
            if delivery_claim != "claimed":
                raise RuntimeError("crosspost delivery state requires audit")
            try:
                receipt = await send_post_with_images(
                    context.bot, CHANNEL_ID, package.final_text, images
                )
            except Exception:
                memory.update_editorial_release_event(
                    user_id=admin_user_id,
                    plan_id=plan.plan_id,
                    platform="telegram",
                    history_commit_status="failed",
                )
                raise
            memory.save_generated_post(
                user_id=admin_user_id,
                expert_mode=get_user_expert_mode(admin_user_id),
                task="exchange_void_to_naz",
                topic=topic,
                content=package.final_text,
                image_count=len(images),
                published_to_channel=True,
                semantic_theme=plan.semantic_theme,
                semantic_card=plan.semantic_card,
                plan_id=plan.plan_id,
                editorial_plan=plan_dict,
            )
            memory.record_content_signature(admin_user_id, plan_dict, topic)
            memory.update_editorial_release_event(
                user_id=admin_user_id,
                plan_id=plan.plan_id,
                platform="telegram",
                telegram_chat_id=receipt.chat_id,
                telegram_message_id=receipt.message_id,
                history_commit_status="committed",
            )
            memory.save_character_state(admin_user_id, character)
            memory.apply_character_event(admin_user_id, "publish")
            move_exchange_file(path, "void_to_naz", "processed")
            logger.info(
                "Exchange published | void_to_naz | plan_id=%s | image_qa_status=not_run",
                plan.plan_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "Exchange void_to_naz failed | file_ref=%s | %s",
                _exchange_file_ref(path),
                exc,
            )
            try:
                move_exchange_file(path, "void_to_naz", "failed")
            except Exception:
                logger.exception(
                    "Exchange failed file move failed | file_ref=%s",
                    _exchange_file_ref(path),
                )


@scheduled_work_marker("crosspost_exchange")
async def crosspost_exchange_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await process_void_to_naz_scheduled_exchange(context)


# -----------------------------------------------------------------------------
# Button handling
# -----------------------------------------------------------------------------


async def handle_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    if not update.message or not update.effective_user:
        return False

    user_id = update.effective_user.id

    if await reject_unregistered_user(update):
        return True

    if text == BTN_BACK:
        USER_PENDING_ACTIONS.pop(user_id, None)
        await update.message.reply_text("Главное меню:", reply_markup=main_keyboard_for(user_id))
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
        await update.message.reply_text("🚀 Выбери, что собрать:", reply_markup=CONTENT_KEYBOARD)
        return True

    if text == BTN_LINKS:
        if not is_admin(user_id):
            await update.message.reply_text("🔒 Связи между ботами доступны только админу.", reply_markup=main_keyboard_for(user_id))
            return True
        await update.message.reply_text(exchange_status_text(), reply_markup=CROSSPOST_KEYBOARD)
        return True

    if text == BTN_CONTROL:
        if not is_admin(user_id):
            await update.message.reply_text("🔒 Центр управления доступен только админу.", reply_markup=main_keyboard_for(user_id))
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
            reply_markup=main_keyboard_for(user_id),
        )
        return True

    if text in CONTENT_BUTTON_TO_ACTION:
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
            "Расписание задаётся через .env: AUTOPOST_TIMES=10:00,14:00,18:00,22:00 и AUTOPOST_TASKS=post,viral."
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

    if text == BTN_CROSSPOST_STATUS:
        if not is_admin(user_id):
            await update.message.reply_text("🔒 Статус обмена доступен только админу.", reply_markup=MAIN_KEYBOARD)
            return True
        await update.message.reply_text(exchange_status_text(), reply_markup=CROSSPOST_KEYBOARD)
        return True

    if text == BTN_VOID_DRAFT:
        if not is_admin(user_id):
            await update.message.reply_text("🔒 Void-кросспостинг доступен только админу.", reply_markup=MAIN_KEYBOARD)
            return True
        USER_PENDING_ACTIONS[user_id] = "void_draft"
        await update.message.reply_text("Пришли текст Void. Я соберу Naz-черновик с моим комментарием.", reply_markup=CROSSPOST_KEYBOARD)
        return True

    if text == BTN_VOID_PUBLISH:
        if not is_admin(user_id):
            await update.message.reply_text("🔒 Публикация Void-кросспоста доступна только админу.", reply_markup=MAIN_KEYBOARD)
            return True
        USER_PENDING_ACTIONS[user_id] = "void_publish"
        await update.message.reply_text("Пришли текст Void. Я соберу кросспост и отправлю в канал.", reply_markup=CROSSPOST_KEYBOARD)
        return True

    if text == BTN_HELP_CAPABILITIES:
        await update.message.reply_text(help_capabilities_for(user_id), reply_markup=HELP_KEYBOARD)
        return True

    if text == BTN_HELP_COMMANDS:
        await update.message.reply_text(help_commands_for(user_id), reply_markup=HELP_KEYBOARD)
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

    if await reject_unregistered_user(update):
        USER_PENDING_ACTIONS.pop(user_id, None)
        return True

    USER_PENDING_ACTIONS.pop(user_id, None)
    topic = text.strip()
    if not topic:
        await update.message.reply_text("Тема пустая. Напиши тему ещё раз.", reply_markup=CONTENT_KEYBOARD)
        return True

    if action in {"void_draft", "void_publish"}:
        if not is_admin(user_id):
            await update.message.reply_text("🔒 Void-кросспостинг доступен только админу.", reply_markup=MAIN_KEYBOARD)
            return True
        await send_typing(update)
        try:
            if action == "void_draft":
                post_text, risks = await generate_void_crosspost(user_id, topic)
                prefix = "🕳 Void → Naz draft"
                if risks:
                    prefix += "\n⚠️ Риски: " + ", ".join(risks)
                await reply_long(update, f"{prefix}\n\n{post_text}", CROSSPOST_KEYBOARD)
                return True

            if not CHANNEL_ID:
                await update.message.reply_text("⚠️ CHANNEL_ID не задан, публиковать некуда.", reply_markup=CROSSPOST_KEYBOARD)
                return True
            post_text, risks = await generate_void_crosspost(user_id, topic, save_generated=False)
            blocked, reason = should_block_publication(post_text, risks)
            if blocked:
                await reply_long(update, f"⚠️ Не публикую Void-кросспост: {reason}\n\n{post_text}", CROSSPOST_KEYBOARD)
                return True
            images, _ = await generate_images_with_retries(user_id, "Void Entity crosspost", post_text, count=CHANNEL_IMAGE_COUNT)
            await publish_to_channel(context.bot, post_text, images=images)
            memory.save_generated_post(
                user_id=user_id,
                expert_mode=get_user_expert_mode(user_id),
                task="publish_void",
                topic="Void Entity crosspost",
                content=post_text,
                image_count=len(images),
                published_to_channel=True,
            )
            await update.message.reply_text("✅ Void-кросспост опубликован в канал.", reply_markup=CROSSPOST_KEYBOARD)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.exception("void pending action failed")
            await update.message.reply_text(f"⚠️ Не смог выполнить Void-кросспост. Причина: {exc}", reply_markup=CROSSPOST_KEYBOARD)
            return True

    await send_typing(update)
    try:
        if action == "image_only":
            prompt = build_naz_direct_image_prompt(topic)
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


IMAGE_INTENT_RE = re.compile(
    r"\b(?:сгенерируй\s+картинк|нарисуй|создай\s+фото|сделай\s+изображени|покажи,?\s+как\s+(?:это|он|она)\s+выглядит)",
    re.I,
)
MAX_DIALOG_REFERENCE_BYTES = 15 * 1024 * 1024
SUPPORTED_AUDIO_SUFFIXES = {".aac", ".flac", ".m4a", ".mp3", ".mp4", ".mpeg", ".mpga", ".ogg", ".wav", ".webm"}


def is_dialog_image_intent(text: str) -> bool:
    return bool(IMAGE_INTENT_RE.search(text or ""))


def validate_reference_image(data: bytes) -> str:
    if not data or len(data) > MAX_DIALOG_REFERENCE_BYTES:
        raise ValueError("Фотография пустая или превышает 15 МБ.")
    try:
        with Image.open(BytesIO(data)) as image:
            image.verify()
            image_format = str(image.format or "").upper()
    except Exception as exc:  # noqa: BLE001
        raise ValueError("Файл не является поддерживаемым изображением.") from exc
    mime = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}.get(image_format)
    if not mime:
        raise ValueError("Поддерживаются фотографии JPEG, PNG и WEBP.")
    return mime


async def download_telegram_photo(message) -> bytes:
    if not getattr(message, "photo", None):
        raise ValueError("В сообщении нет фотографии.")
    photo = message.photo[-1]
    if getattr(photo, "file_size", 0) and photo.file_size > MAX_DIALOG_REFERENCE_BYTES:
        raise ValueError("Фотография превышает 15 МБ.")
    telegram_file = await photo.get_file()
    data = bytes(await telegram_file.download_as_bytearray())
    validate_reference_image(data)
    return data


def telegram_audio_name(message, media) -> str:
    """Build a safe filename so the transcription API can detect the format."""
    if getattr(message, "voice", None) is media:
        return "telegram-voice.ogg"
    original = Path(str(getattr(media, "file_name", "") or "")).name
    suffix = Path(original).suffix.lower()
    if suffix not in SUPPORTED_AUDIO_SUFFIXES:
        mime_suffix = {
            "audio/aac": ".aac",
            "audio/flac": ".flac",
            "audio/m4a": ".m4a",
            "audio/mp4": ".mp4",
            "audio/mpeg": ".mp3",
            "audio/ogg": ".ogg",
            "audio/wav": ".wav",
            "audio/webm": ".webm",
        }.get(str(getattr(media, "mime_type", "") or "").lower())
        suffix = mime_suffix or ""
    if not suffix:
        raise ValueError("Не удалось определить формат аудио. Пришли voice, MP3, M4A, WAV, OGG или WEBM.")
    return f"telegram-audio{suffix}"


async def download_telegram_audio(message) -> Tuple[bytes, str]:
    media = getattr(message, "voice", None) or getattr(message, "audio", None)
    if not media:
        raise ValueError("В сообщении нет голосового или аудиофайла.")
    if getattr(media, "file_size", 0) and media.file_size > VOICE_MAX_BYTES:
        raise ValueError(f"Аудио превышает лимит {VOICE_MAX_BYTES // (1024 * 1024)} МБ.")
    if getattr(media, "duration", 0) and media.duration > VOICE_MAX_DURATION_SECONDS:
        raise ValueError(f"Голосовое длиннее {VOICE_MAX_DURATION_SECONDS // 60} минут.")
    filename = telegram_audio_name(message, media)
    telegram_file = await media.get_file()
    data = bytes(await telegram_file.download_as_bytearray())
    if not data:
        raise ValueError("Telegram вернул пустой аудиофайл.")
    if len(data) > VOICE_MAX_BYTES:
        raise ValueError(f"Аудио превышает лимит {VOICE_MAX_BYTES // (1024 * 1024)} МБ.")
    return data, filename


async def transcribe_voice_bytes(data: bytes, filename: str) -> str:
    def _request() -> str:
        payload = BytesIO(data)
        payload.name = filename
        response = ensure_voice_openai_client().audio.transcriptions.create(
            model=OPENAI_TRANSCRIBE_MODEL,
            file=payload,
        )
        return str(getattr(response, "text", "") or "").strip()

    try:
        transcript = await asyncio.to_thread(_request)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Voice transcription failed | error=%s", type(exc).__name__)
        raise RuntimeError("Не удалось распознать голосовое. Попробуй ещё раз позже.") from exc
    if not transcript:
        raise RuntimeError("Не удалось расслышать речь в голосовом.")
    return transcript


async def synthesize_voice_bytes(text: str) -> bytes:
    clean_text = sanitize_dialog_text(text)
    if not clean_text:
        raise ValueError("Нечего озвучивать.")

    def _request() -> bytes:
        response = ensure_voice_openai_client().audio.speech.create(
            model=OPENAI_TTS_MODEL,
            voice=OPENAI_TTS_VOICE,
            input=clean_text,
            instructions=(
                "Speak naturally in Russian as Naz: a young energetic technology builder, "
                "friendly, confident, lightly ironic, never theatrical. This is an AI-generated voice."
            ),
            response_format="opus",
        )
        content = response.read() if hasattr(response, "read") else getattr(response, "content", b"")
        return bytes(content or b"")

    try:
        audio = await asyncio.to_thread(_request)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Voice synthesis failed | error=%s", type(exc).__name__)
        raise RuntimeError("Не удалось озвучить ответ.") from exc
    if not audio:
        raise RuntimeError("OpenAI вернул пустой голосовой ответ.")
    return audio


async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    if not VOICE_MESSAGES_ENABLED:
        await update.message.reply_text("Голосовые Naz пока выключены в настройках.")
        return
    user_id = update.effective_user.id
    owner_voice = is_admin(user_id)
    active_delegation = None if owner_voice else memory.get_active_delegation(user_id)
    saved_contact = (
        None
        if owner_voice or not VOICE_MESSAGES_CONTACTS_ENABLED
        else get_registered_contact(user_id)
    )
    if VOICE_MESSAGES_ADMIN_ONLY and not owner_voice and not active_delegation and not saved_contact:
        await update.message.reply_text("Голосовой режим доступен владельцу, сохранённым контактам и активным поручениям.")
        return
    if not OPENAI_VOICE_API_KEY:
        await update.message.reply_text("Голосовой API ещё не настроен. Нужен отдельный официальный OpenAI key.")
        return

    try:
        await update.effective_chat.send_action(ChatAction.TYPING)
        data, filename = await download_telegram_audio(update.message)
        transcript = await transcribe_voice_bytes(data, filename)
        if active_delegation:
            await handle_delegated_reply(update, context, transcript, reply_as_voice=True)
            return
        if owner_voice and await prepare_contact_message_request(update, context, transcript):
            return
        answer = sanitize_dialog_text(await generate_answer(update.effective_user.id, transcript))
        await update.effective_chat.send_action(ChatAction.RECORD_VOICE)
        try:
            audio = await synthesize_voice_bytes(answer)
        except RuntimeError:
            logger.warning("Voice reply falling back to text")
            await reply_long(update, answer, main_keyboard_for(user_id))
            return

        payload = BytesIO(audio)
        payload.name = "naz-reply.ogg"
        try:
            await update.message.reply_voice(
                voice=payload,
                caption="AI-голос Naz",
                reply_markup=main_keyboard_for(user_id),
            )
        except BadRequest:
            payload.seek(0)
            await update.message.reply_document(
                document=payload,
                caption="Голосовой ответ Naz",
                reply_markup=main_keyboard_for(user_id),
            )
    except ValueError as exc:
        await update.message.reply_text(sanitize_dialog_text(str(exc)), reply_markup=main_keyboard_for(user_id))
    except RuntimeError as exc:
        await update.message.reply_text(sanitize_dialog_text(str(exc)), reply_markup=main_keyboard_for(user_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Voice message failed | error=%s", type(exc).__name__)
        await update.message.reply_text("Голосовой режим временно недоступен.", reply_markup=main_keyboard_for(user_id))


async def process_dialog_image_request(
    user_id: int,
    instruction: str,
    *,
    reference_bytes: Optional[bytes] = None,
) -> Tuple[bytes, str]:
    clean_instruction = " ".join((instruction or "").split()).strip()
    if not clean_instruction:
        raise ValueError("Добавь описание изображения после команды или к фотографии.")
    if reference_bytes is None:
        prompt = (
            f"Create a concrete image for this user request: {clean_instruction}. "
            "Preserve the requested subject and mood. No text, logos, watermarks, or UI unless explicitly requested."
        )
        image = await generate_image_bytes(prompt)
        if not image:
            raise RuntimeError("Генератор изображений сейчас недоступен. Попробуй позже.")
    else:
        mime = validate_reference_image(reference_bytes)
        with tempfile.TemporaryDirectory(prefix="naz-dialog-image-") as directory:
            reference_path = Path(directory) / "reference.image"
            reference_path.write_bytes(reference_bytes)
            encoded = base64.b64encode(reference_path.read_bytes()).decode("ascii")
            data_url = f"data:{mime};base64,{encoded}"
            image = await generate_reference_image_bytes(clean_instruction, data_url)
    short = clean_instruction[:120].rstrip()
    event = f"[Создано изображение: {short}]"
    memory.save_dialog_turn(user_id, clean_instruction, event)
    return image, event


async def send_dialog_image(update: Update, instruction: str, reference_bytes: Optional[bytes] = None) -> None:
    if not update.message or not update.effective_user:
        return
    if await reject_unregistered_user(update):
        return
    keyboard = main_keyboard_for(update.effective_user.id)
    await update.effective_chat.send_action(ChatAction.UPLOAD_PHOTO)
    try:
        image, _ = await process_dialog_image_request(
            update.effective_user.id, instruction, reference_bytes=reference_bytes
        )
        payload = BytesIO(image)
        payload.name = "naz-image.png"
        caption = sanitize_dialog_text(f"Готово. {instruction[:160]}")
        try:
            await update.message.reply_photo(photo=payload, caption=caption, reply_markup=keyboard)
        except BadRequest:
            payload.seek(0)
            await update.message.reply_document(document=payload, caption=caption, reply_markup=keyboard)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Dialog image request failed | reference=%s | error=%s", bool(reference_bytes), type(exc).__name__)
        message = str(exc) if isinstance(exc, (ValueError, ReferenceImageUnsupportedError)) else (
            "Не удалось создать изображение: генератор временно недоступен. Попробуй позже."
        )
        await update.message.reply_text(sanitize_dialog_text(message), reply_markup=keyboard)


async def dialog_image_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    instruction = " ".join(context.args or []).strip()
    await send_dialog_image(update, instruction)


async def handle_photo_instruction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user or await reject_unregistered_user(update):
        return
    instruction = (update.message.caption or "").strip()
    if not instruction:
        await update.message.reply_text("Добавь к фотографии подпись с инструкцией, что нужно изменить.")
        return
    try:
        reference = await download_telegram_photo(update.message)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return
    await send_dialog_image(update, instruction, reference)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user or not update.message.text:
        return

    user_id = update.effective_user.id
    text = update.message.text.strip()

    if await handle_delegated_reply(update, context, text):
        return

    if is_admin(user_id) and update.message.reply_to_message:
        try:
            named = memory.name_contact_from_reply(update.message.reply_to_message.message_id, user_id, text)
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return
        if named:
            await update.message.reply_text(
                f"Записал: {named['alias']} → {named['display_name']}. Теперь можно сказать: «Напиши {named['alias']}, чтобы…»"
            )
            return

    if is_admin(user_id):
        try:
            if await prepare_contact_message_request(update, context, text):
                return
            request = delegated_messaging.parse_delegation_request(text)
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return
        if request:
            spoken_alias, purpose = request
            contact = delegated_messaging.resolve_saved_contact(memory.list_saved_contacts(user_id), spoken_alias)
            if not contact:
                aliases = ", ".join(str(item["alias"]) for item in memory.list_saved_contacts(user_id)) or "пока пусто"
                await update.message.reply_text(f"Не нашёл один точный контакт «{spoken_alias}». Сохранены: {aliases}.")
                return
            try:
                await start_saved_contact_delegation(update, context, contact, purpose)
            except ValueError as exc:
                await update.message.reply_text(str(exc))
            return

    if not has_registered_access(user_id):
        memory.remember_reachable_peer(
            user_id,
            user_display_name(update),
            (delegated_messaging.utc_now() + timedelta(hours=24)).isoformat(timespec="seconds"),
        )
        await ensure_contact_named(update, context)
        await reject_unregistered_user(update)
        return

    if await handle_menu_button(update, context, text):
        return

    if await handle_pending_action(update, context, text):
        return

    replied = update.message.reply_to_message
    if replied and getattr(replied, "photo", None):
        try:
            reference = await download_telegram_photo(replied)
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return
        await send_dialog_image(update, text, reference)
        return

    if is_dialog_image_intent(text):
        await send_dialog_image(update, text)
        return

    # Default chat through persistent expert mode and SQLite history.
    await send_typing(update)
    try:
        answer = sanitize_dialog_text(await generate_answer(user_id, text))
        await reply_long(update, answer, ANGLE_KEYBOARD if is_angle_engine_message(answer) else main_keyboard_for(user_id))
    except Exception as exc:  # noqa: BLE001
        logger.exception("handle_message failed")
        await reply_long(update, f"⚠️ Naz споткнулся. Причина: {exc}", main_keyboard_for(user_id))


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


NAZ_TELEGRAM_RUBRICS: List[Dict[str, object]] = [
    {
        "name": "Утренний дожим",
        "slots": ["10:00"],
        "task": "post",
        "topics": [
            "Что сегодня можно упростить в AI-системе, пока она не начала шуметь",
            "Один маленький дожим в боте, который экономит день",
            "Почему утро в проекте лучше начинать не с вдохновения, а с проверки контура",
            "Как понять, что бот уже почти работает, но ему не хватает одного скучного шага",
            "Почему рабочий день лучше спасает маленькая проверка, а не большой план",
            "Что проверить в автопостинге до того, как он начнёт писать в канал",
            "Как один лог может заменить час тревоги",
            "Почему кнопка 'работает' ещё не значит, что система готова",
            "Зачем AI-проекту утренний чек без героизма",
            "Как не перепутать прогресс с красивым ответом модели",
            "Почему иногда лучший апгрейд — убрать лишний автоматизм",
            "Как маленькая настройка расписания меняет настроение всего канала",
        ],
        "profile": {
            "name": "Дожиматель",
            "voice": "сфокусированно и прямо, меньше украшений, больше результата",
            "angle": "один практический шаг, который превращает хаос в управляемый процесс",
            "format": "короткий рабочий пост: симптом, действие, результат",
            "avoid": "не обещать чудес и не превращать пост в чеклист ради чеклиста",
        },
    },
    {
        "name": "AI без магии",
        "slots": ["14:00"],
        "task": "post",
        "topics": [
            "Почему нейросети — это не магия, а рабочий инструмент",
            "Как предпринимателю начать использовать AI без хаоса",
            "Где заканчивается генератор текста и начинается контент-система",
            "Почему хороший AI-помощник начинается не с модели, а с понятной задачи",
            "Как объяснить AI-проект человеку, который не хочет слушать про токены",
            "Почему промпт не спасает процесс, если никто не проверяет результат",
            "Что на самом деле покупает человек, когда просит 'сделайте мне AI-бота'",
            "Почему AI без редактора быстро превращается в шумную машинку",
            "Как отличить полезную автоматизацию от красивой игрушки",
            "Почему бизнесу чаще нужен не агент, а нормальная граница ответственности",
            "Что должно быть в AI-системе до первой публичной кнопки",
            "Как говорить про AI без хайпа и без страха",
        ],
        "profile": {
            "name": "Очевидная мелочь",
            "voice": "спокойно и человечно, как будто объясняешь важную вещь без умничанья",
            "angle": "простая деталь, которую все пропускают, а потом из-за неё ломается проект",
            "format": "заметка на один инсайт: мелочь, последствия, зачем это помнить",
            "avoid": "не уходить в мотивацию и не продавать AI как волшебную кнопку",
        },
    },
    {
        "name": "Баг, который стал системой",
        "slots": ["18:00"],
        "task": "viral",
        "topics": [
            "Как маленький баг в интеграции ломает весь пользовательский опыт",
            "Почему AI-проект ломается не из-за модели, а из-за процесса",
            "Что должно быть у Telegram-бота, чтобы он не был игрушкой",
            "Почему одинаковая ошибка в двух ботах не всегда значит один виноватый commit",
            "Как баг становится инструкцией, если перестать сразу чинить всё подряд",
            "Почему 'оно вчера работало' — плохая диагностика, но хороший хук",
            "Как Telegram polling превращает локальный дубль в странную аварию",
            "Почему ошибка доставки страшнее ошибки генерации",
            "Что делать, когда бот молчит, но сервисы по отдельности живые",
            "Почему rollback иногда лечит совесть, но не систему",
            "Как понять, что проблема живёт не в коде, а между сервисами",
            "Почему самый полезный баг — тот, после которого появляется проверка",
        ],
        "profile": {
            "name": "Build in public",
            "voice": "честно, живо, без героизма; как рабочая заметка после реального дожима",
            "angle": "что сломалось, как искали причину, какой вывод остался после починки",
            "format": "мини-история: симптом, ложная версия, настоящая причина, урок",
            "avoid": "не раскрывать секреты, IP, токены, внутренние URL и клиентские детали",
        },
    },
    {
        "name": "Naz после смены",
        "slots": ["22:00"],
        "task": "post",
        "topics": [
            "Как память меняет поведение AI-ассистента в диалоге",
            "Почему автопостинг без редакторской логики быстро становится шумом",
            "Философия контроля: почему AI-системе нужны границы",
            "Почему ботам тоже нужен вечерний разбор полётов",
            "Как отличить живой стиль от набора любимых фраз",
            "Почему канал устаёт не от частоты постов, а от одинакового дыхания",
            "Что значит 'контроль' в системе, которая умеет писать сама",
            "Почему иногда надо не добавлять рубрику, а дать старой рубрике новый темп",
            "Как память помогает не повторять себя, если ей правильно пользоваться",
            "Почему хороший автопостинг должен уметь молчать",
            "Как не превратить AI-канал в аккуратное бубнение",
            "Зачем системе отдельный голос для ночных выводов",
        ],
        "profile": {
            "name": "Философия без тумана",
            "voice": "чуть глубже и тише, но всё равно понятно обычному человеку",
            "angle": "маленькая философская мысль из разработки: про память, контроль, доверие или шум",
            "format": "короткая заметка без списков: наблюдение, поворот, человеческий вывод",
            "avoid": "не уходить в абстрактный мотивационный туман",
        },
    },
]


NAZ_VK_RUBRICS: List[Dict[str, str]] = [
    {
        "name": "Полевая заметка Naz",
        "kind": "daily",
        "angle": "одна конкретная сцена, предмет, действие или встреча; выбранная смысловая ось задаёт предмет, а AI и разработка появляются только если естественно относятся к сцене",
        "format": "связная полевая заметка в собственной форме без обязательной схемы проблема-причина-урок и без выдуманного личного опыта",
        "track_tags": "daily,focus,warm,city",
    },
    {
        "name": "Маленький эксперимент",
        "kind": "daily",
        "angle": "одно небольшое проверяемое действие, выбор или наблюдение и его неожиданное конкретное последствие; не превращать результат в универсальный совет",
        "format": "самостоятельный VK-пост с конкретикой; финалом может быть вопрос, незакрытое напряжение или частный вывод этой сцены",
        "track_tags": "daily,focus,builder,warm",
    },
    {
        "name": "Человеческая деталь",
        "kind": "daily",
        "angle": "один жест, привычка, место, предмет или короткий выбор, через который раскрывается выбранная смысловая ось",
        "format": "наблюдение и развитие мысли без обязательной пользы, системной дисциплины, починки или морали",
        "track_tags": "daily,reflective,warm,city",
    },
    {
        "name": "Игровая лаборатория VK",
        "kind": "gaming",
        "angle": "игровая механика, мод, AI-инструмент или честный эксперимент без выдуманного личного опыта",
        "format": "самостоятельный VK-пост: конкретная игровая деталь, разбор механики и вопрос или вывод для игроков",
        "track_tags": "gaming,mechanic,cyber,arcade,builder,identity,humor",
    },
    MATERIAL_RUBRIC.copy(),
]


def select_naz_vk_rubric(
    kind: str,
    *,
    excluded_theme_keys: Iterable[str] = (),
) -> Dict[str, str]:
    matching = [rubric for rubric in NAZ_VK_RUBRICS if rubric.get("kind") == kind]
    if not matching:
        raise vk_publish_queue.QueueError(f"неизвестный тип VK-рубрики: {kind}")
    excluded = {
        str(key)
        for key in excluded_theme_keys
        if str(key).strip()
    }
    available = [
        rubric
        for rubric in matching
        if any(
            theme.key not in excluded
            for theme in semantic_autopost.compatible_themes(str(rubric["name"]))
        )
    ]
    return random.choice(available or matching)


def select_naz_telegram_rubric(slot: str = "") -> Dict[str, object]:
    matching = [
        rubric for rubric in NAZ_TELEGRAM_RUBRICS
        if slot and slot in [str(item) for item in rubric.get("slots", [])]
    ]
    return random.choice(matching or NAZ_TELEGRAM_RUBRICS)


AUTOPOST_EDITORIAL_DIRECTIONS: List[Dict[str, str]] = [
    {
        "name": "Мини-сцена",
        "shape": "начни с маленькой узнаваемой сцены, потом покажи, какой системный вывод за ней спрятан",
        "rhythm": "короткие фразы, живой поворот, без списка",
        "avoid": "не начинать с тезиса и не заканчивать одинаковым 'дожали'",
    },
    {
        "name": "Анти-совет",
        "shape": "начни с вредного подхода, который хочется сделать на автомате, затем разверни в нормальную практику",
        "rhythm": "чуть резче, но без токсичности; один конфликт, один вывод",
        "avoid": "не делать кликбейт и не морализировать",
    },
    {
        "name": "Тихая философия",
        "shape": "вытащи спокойное наблюдение из конкретной рабочей детали",
        "rhythm": "медленнее, глубже, меньше технических слов",
        "avoid": "не уходить в туман и общие слова про будущее",
    },
    {
        "name": "Разбор ошибки",
        "shape": "покажи симптом, ложную причину, настоящую причину и маленький вывод",
        "rhythm": "плотно и ясно, как заметка после диагностики",
        "avoid": "не пересказывать длинную хронологию",
    },
    {
        "name": "Почти мем",
        "shape": "начни с нелепости разработки или контента, но быстро выведи к пользе",
        "rhythm": "иронично, легче, с одной смешной деталью",
        "avoid": "не превращать в стендап",
    },
    {
        "name": "Письмо себе",
        "shape": "напиши как короткую записку человеку, который завтра снова полезет чинить систему",
        "rhythm": "лично, тепло, без поучения",
        "avoid": "не делать мотивационный пост",
    },
    {
        "name": "Один вопрос",
        "shape": "пост строится вокруг одного вопроса, который меняет взгляд на задачу",
        "rhythm": "вопрос, две-три проверки, вывод",
        "avoid": "не превращать в FAQ",
    },
]


def recent_autopost_topic_text(user_id: int, limit: int = 12) -> str:
    posts = memory.get_recent_generated_posts(user_id, limit=limit)
    chunks = []
    for item in posts:
        topic = str(item.get("topic") or "")
        task = str(item.get("task") or "")
        if "autopost" in task or "source_monitor" in task or "agent_content" in task:
            chunks.append(topic)
    state = memory.load_state(user_id)
    chunks.extend(str(topic) for topic in (state.get("recent_topics") or [])[-12:])
    return "\n".join(chunks)


def is_fresh_autopost_topic(topic: str, recent_text: str) -> bool:
    fingerprint = naz_controller.topic_fingerprint(topic)
    recent_lines = [line.strip() for line in recent_text.splitlines() if line.strip()]
    return not any(naz_controller.is_similar_topic(fingerprint, line) for line in recent_lines)


def select_autopost_topics(user_id: int, rubric: Dict[str, object], limit: int = 7) -> List[str]:
    topics = [str(item) for item in rubric.get("topics", AUTOPOST_TOPICS) if str(item).strip()]
    random.shuffle(topics)
    recent_text = recent_autopost_topic_text(user_id)
    fresh = [topic for topic in topics if is_fresh_autopost_topic(topic, recent_text)]
    return (fresh or topics)[:limit]


def format_autopost_direction(direction: Dict[str, str]) -> str:
    return (
        f"Редакторская форма выпуска: {direction['name']}.\n"
        f"Композиция: {direction['shape']}.\n"
        f"Ритм: {direction['rhythm']}.\n"
        f"Отдельно избегать: {direction['avoid']}.\n"
    )


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


AUTOPOST_IMAGE_ATTEMPTS = 2


async def notify_admin(bot, text: str) -> None:
    if not ADMIN_ID:
        return
    try:
        await send_long_to_chat(bot, ADMIN_ID, text)
    except TelegramError as exc:
        logger.warning("Admin notification failed: %s", exc)


def unique_recent_reasons(reasons: List[str], limit: int = 5) -> List[str]:
    unique: List[str] = []
    for reason in reasons:
        if reason and reason not in unique:
            unique.append(reason)
    return unique[-limit:]


async def notify_autopost_skip_once(bot, reasons: List[str]) -> None:
    unique_reasons = unique_recent_reasons(reasons)
    signature = hashlib.sha256("\n".join(unique_reasons).encode("utf-8")).hexdigest()[:16]
    date_key = current_bot_date()
    if AUTOPOST_SKIP_ALERTS.get(date_key) == signature:
        logger.info("AUTOPOST skip alert suppressed: duplicate failure signature for %s", date_key)
        return
    AUTOPOST_SKIP_ALERTS[date_key] = signature
    if len(AUTOPOST_SKIP_ALERTS) > 10:
        for key in sorted(AUTOPOST_SKIP_ALERTS)[:-10]:
            AUTOPOST_SKIP_ALERTS.pop(key, None)
    await notify_admin(
        bot,
        "⚠️ Автопостинг пропустил слот после нескольких попыток.\n\n"
        + "\n".join(f"- {reason}" for reason in unique_reasons),
    )


class ScheduledTechnicalFailure(RuntimeError):
    """The model failed the generation package contract twice."""


class ScheduledContentReject(RuntimeError):
    """A local non-diversity safety/quality check rejected generated content."""


def scheduled_plan(
    *,
    user_id: int,
    platform: str,
    slot: str,
    seed: str,
    rubric_rows: Iterable[Dict[str, Any]],
    source_rows: Iterable[Dict[str, Any]],
    character: naz_character.CharacterState,
    crosspost_plan_id: str = "",
    persona_rubric_rows: Optional[Iterable[Dict[str, Any]]] = None,
    persona_source_rows: Optional[Iterable[Dict[str, Any]]] = None,
) -> editorial_orchestrator.EditorialPlan:
    """The sole creative decision entrypoint for scheduled Naz routes."""
    rubric_rows = tuple(rubric_rows)
    source_rows = tuple(source_rows)
    if persona_rubric_rows is None or persona_source_rows is None:
        if platform == "telegram":
            telegram_rubrics, telegram_sources = naz_telegram_editorial_catalog_rows()
            if persona_rubric_rows is None:
                persona_rubric_rows = (*telegram_rubrics, *rubric_rows)
            if persona_source_rows is None:
                persona_source_rows = (*telegram_sources, *source_rows)
        elif platform == "vk":
            if persona_rubric_rows is None:
                persona_rubric_rows = NAZ_VK_RUBRICS
            if persona_source_rows is None:
                persona_source_rows = source_rows
    context = naz_editorial_catalog.build_context(
        platform=platform,
        slot=slot,
        seed=seed,
        rubric_rows=rubric_rows,
        source_rows=source_rows,
        published_history=memory.get_recent_content_signatures(user_id, limit=160),
        character=character,
        crosspost_plan_id=crosspost_plan_id,
        persona_rubric_rows=persona_rubric_rows,
        persona_source_rows=persona_source_rows,
    )
    return editorial_orchestrator.plan_release(context)


def naz_telegram_editorial_catalog_rows(
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Resolve the complete Telegram catalog before a slot constrains it."""
    rubric_rows: List[Dict[str, Any]] = []
    source_rows: List[Dict[str, Any]] = []
    for rubric in NAZ_TELEGRAM_RUBRICS:
        row = dict(rubric)
        key = naz_editorial_catalog.rubric_key(str(row["name"]))
        row["key"] = key
        profile = row.get("profile")
        if isinstance(profile, dict):
            row["angle"] = " | ".join(
                str(profile.get(item, "")) for item in ("angle", "format", "voice")
            )
        rubric_rows.append(row)
        for index, topic_value in enumerate(row.get("topics", AUTOPOST_TOPICS)):
            source_rows.append(
                {
                    "source_ref": f"naz-topic:{key}:{index}",
                    "topic": str(topic_value),
                    "source_type": "catalog",
                    "rubric_keys": (key,),
                }
            )
    return rubric_rows, source_rows


def safe_vk_editorial_metadata(plan: editorial_orchestrator.EditorialPlan) -> Dict[str, Any]:
    """Bounded non-private metadata carried by the shared VK queue."""
    return {
        "persona": plan.persona,
        "platform": plan.platform,
        "slot": plan.slot,
        "rubric": plan.rubric,
        "source_type": plan.source_type,
        "purpose": plan.purpose,
        "semantic_theme": plan.semantic_theme,
        "semantic_card": plan.semantic_card,
        "structure": plan.structure,
        "visual_mode": plan.visual_mode,
        "track_tags": list(plan.track_tags),
        "orchestrator_version": plan.orchestrator_version,
        "content_policy_version": plan.content_policy_version,
        "visual_policy_version": plan.visual_policy_version,
        "music_policy_version": plan.music_policy_version,
    }


def scheduled_package_quality_check(
    plan: editorial_orchestrator.EditorialPlan,
    package: editorial_orchestrator.GenerationPackage,
) -> tuple[bool, str]:
    text = package.final_text.strip()
    if is_warning_response(text):
        return False, "model_warning"
    lowered = text.casefold()
    if any(marker in lowered for marker in ("diag:", "traceback", "internal exception", "editorial plan", "plan_id")):
        return False, "internal_metadata"
    if plan.platform == "telegram" and not 450 <= len(text) <= 1800:
        return False, "telegram_length"
    if plan.platform == "vk" and not 450 <= len(text) <= 1900:
        return False, "vk_length"
    if package.visual_relation_to_thesis.casefold() in {"n/a", "none", "unrelated"}:
        return False, "visual_relevance"
    return True, "ok"


async def generate_scheduled_package(
    plan: editorial_orchestrator.EditorialPlan,
    character: naz_character.CharacterState,
    *,
    source_material: str = "",
) -> editorial_orchestrator.GenerationPackage:
    """One generation call plus one schema-only retry with the immutable plan."""
    technical_reason = ""
    for attempt in range(2):
        prompt = editorial_orchestrator.generation_prompt(
            plan,
            persona_direction=naz_editorial_catalog.persona_direction(character),
            source_material=source_material,
            technical_retry_reason=technical_reason,
        )
        raw = await call_gpt(
            [
                {
                    "role": "system",
                    "content": (
                        "Execute the supplied Naz EditorialPlan exactly once. Return strict JSON only. "
                        "Never reveal internal planning, private data, diagnostics or secrets."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=1900,
            temperature=0.65,
            model=CONTENT_MODEL_NAME,
        )
        try:
            package = editorial_orchestrator.parse_generation_package(raw, plan)
        except editorial_orchestrator.GenerationPackageError as exc:
            technical_reason = str(exc)
            if attempt == 0:
                continue
            raise ScheduledTechnicalFailure("generation_package_invalid_twice") from exc
        ok, reason = scheduled_package_quality_check(plan, package)
        if not ok:
            raise ScheduledContentReject(reason)
        return package
    raise ScheduledTechnicalFailure("generation_package_unavailable")


def story_first_dry_run(
    plan: editorial_orchestrator.EditorialPlan,
    safe_facts: tuple[str, ...],
) -> Path:
    pack = story_production.plan_story_pack(plan, safe_facts)
    return story_production.persist_dry_run(pack, NAZ_STORY_PACK_ROOT)


def chronicle_source_row(
    *,
    source_ref: str,
    safe_context: str,
    risks: Iterable[str],
    topic: str,
) -> Dict[str, Any]:
    """Extract objective evidence; plan_release owns the suitability decision."""
    chunks = [
        " ".join(item.split())[:420]
        for item in re.split(r"(?<=[.!?])\s+|\n+", safe_context)
        if len(" ".join(item.split())) >= 24
        and "[REDACTED]" not in item
        and not re.search(r"(?i)(token|api[_ -]?key|password|secret|private message)", item)
    ]
    facts = tuple(dict.fromkeys(chunks))[:7]
    folded = safe_context.casefold()
    action_stems = ("сделал", "добавил", "исправил", "запустил", "проверил", "изменил", "собрал", "failed", "fixed", "tested", "built")
    process_stems = ("шаг", "сначала", "затем", "после", "до ", "лог", "тест", "сбор", "deploy", "build", "retry")
    result_stems = ("результат", "стало", "получилось", "заработал", "исчез", "нашли", "вывод", "result", "worked", "changed")
    causal_stems = ("потому", "поэтому", "из-за", "после", "привел", "привёл", "because", "therefore", "then")
    risk_values = tuple(str(item).casefold() for item in risks)
    return {
        "source_ref": source_ref,
        "topic": topic,
        "source_type": "work_chronicle",
        "safe_facts": facts,
        "source_verified": bool(source_ref and facts),
        "concrete_action": any(stem in folded for stem in action_stems),
        "visualizable_process": any(stem in folded for stem in process_stems),
        "causal_bits": min(7, sum(1 for item in facts if any(stem in item.casefold() for stem in (*action_stems, *causal_stems)))),
        "real_result": any(stem in folded for stem in result_stems),
        "contains_secrets": any("secret" in item or "token" in item or "credential" in item for item in risk_values),
        "contains_private_data": any("private" in item or "personal" in item or "персон" in item for item in risk_values),
    }


async def generate_images_with_retries(
    user_id: int,
    topic: str,
    post_text: str,
    *,
    count: int,
    attempts: int = AUTOPOST_IMAGE_ATTEMPTS,
    platform: str = "telegram",
    editorial_visual_brief: str = "",
) -> Tuple[List[bytes], str]:
    last_prompt = ""
    for attempt in range(1, max(1, attempts) + 1):
        images, image_prompt = await generate_images_for_post(
            user_id,
            topic,
            post_text,
            count=count,
            platform=platform,
            editorial_visual_brief=editorial_visual_brief,
        )
        last_prompt = image_prompt
        if images or not REQUIRE_IMAGES_FOR_CHANNEL_POSTS:
            return images, image_prompt
        logger.warning("Image generation retry %s/%s failed for topic=%s", attempt, attempts, topic)
        if attempt < attempts:
            await asyncio.sleep(3)
    return [], last_prompt


def curated_visual_bytes(path: Path) -> bytes:
    """Prepare an existing curated visual for Telegram without forcing a square crop."""
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        image.thumbnail((1800, 1800), Image.Resampling.LANCZOS)
        output = BytesIO()
        image.save(output, format="JPEG", quality=92, optimize=True)
        return output.getvalue()


async def try_visual_archive_autopost(
    context: ContextTypes.DEFAULT_TYPE,
    admin_user_id: int,
    slot: str,
) -> Optional[bool]:
    if not VISUAL_ARCHIVE_ENABLED:
        return None
    is_visual_turn, slot_counter = visual_archive.claim_visual_turn(
        VISUAL_ARCHIVE_STATE_FILE,
        VISUAL_ARCHIVE_EVERY_N_POSTS,
    )
    if not is_visual_turn:
        logger.info(
            "VISUAL_ARCHIVE normal content turn | counter=%s | cadence=%s",
            slot_counter,
            VISUAL_ARCHIVE_EVERY_N_POSTS,
        )
        return None
    candidate = visual_archive.choose_candidate(
        VISUAL_ARCHIVE_MANIFEST,
        VISUAL_ARCHIVE_STATE_FILE,
        VISUAL_ARCHIVE_ROOT,
        require_approved=VISUAL_ARCHIVE_REQUIRE_APPROVED,
    )
    if not candidate:
        logger.info(
            "VISUAL_ARCHIVE turn has no eligible unused candidates | counter=%s",
            slot_counter,
        )
        return None

    candidate_id = str(candidate["id"])
    image_path = visual_archive.preferred_image_path(VISUAL_ARCHIVE_ROOT, candidate)
    topic = visual_archive.visual_topic(candidate)
    source_ref = f"visual_archive:{candidate_id}:{slot or 'manual'}"
    try:
        async def generate(instruction: str) -> str:
            return await generate_content(
                admin_user_id,
                topic,
                "post",
                save_generated=False,
                extra_instruction=(
                    "Это image-first публикация. Сначала прочитай смысл визуала, затем напиши самостоятельный Naz-пост вокруг него. "
                    "Не переписывай текст с картинки дословно, не упоминай OCR и не описывай изображение как каталог. "
                    f"Рубрика архива: {candidate.get('rubric', 'visual_archive')}. Слот: {slot or 'manual'}.\n"
                    f"{instruction}"
                ),
                platform="telegram",
                commit_state=False,
                inherit_interactive_context=False,
            )

        theme, semantic_result = await generate_semantic_autopost_candidate(
            user_id=admin_user_id,
            platform="telegram",
            rubric_name="visual_archive",
            seed=source_ref,
            generate=generate,
        )
        if not semantic_result.accepted:
            logger.warning(
                "VISUAL_ARCHIVE semantic block | id=%s",
                candidate_id,
            )
            return False
        post_text = semantic_result.text
        image_bytes = curated_visual_bytes(image_path)
        await send_post_with_images(context.bot, CHANNEL_ID, post_text, [image_bytes])
        memory.save_generated_post(
            user_id=admin_user_id,
            expert_mode=get_user_expert_mode(admin_user_id),
            task=f"visual_archive:{candidate.get('rubric', 'visual_archive')}",
            topic=topic[:1000],
            content=post_text,
            image_count=1,
            published_to_channel=True,
            semantic_theme=theme.key,
        )
        queue_naz_post_for_void(
            post_text,
            source=f"visual_archive:{candidate.get('rubric', 'visual_archive')}",
            topic=topic[:1000],
        )
        commit_accepted_autopost_state(
            user_id=admin_user_id,
            topic=topic,
            task="post",
            platform="telegram",
            source_ref=source_ref,
            theme=theme,
            result=semantic_result,
        )
        visual_archive.mark_used(VISUAL_ARCHIVE_STATE_FILE, candidate_id)
        logger.info(
            "VISUAL_ARCHIVE published | id=%s | file=%s | counter=%s | cadence=%s",
            candidate_id,
            image_path,
            slot_counter,
            VISUAL_ARCHIVE_EVERY_N_POSTS,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.exception("VISUAL_ARCHIVE failed | id=%s | error=%s", candidate_id, exc)
        return False


@scheduled_work_marker("telegram_autopost")
async def auto_post_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not CHANNEL_ID:
        logger.warning("AUTOPOST skipped: CHANNEL_ID empty")
        return

    admin_user_id = ADMIN_ID or 0
    if admin_user_id:
        memory.load_state(admin_user_id)
        if get_user_expert_mode(admin_user_id) not in EXPERT_MODES:
            set_user_expert_mode(admin_user_id, DEFAULT_EXPERT_MODE)

    job_data = context.job.data if context.job else {}
    slot = str(job_data.get("slot", "")) if isinstance(job_data, dict) else ""
    slot_captured_at = memory.utc_now()
    logger.info("NAZ_TELEGRAM_AUTO_LOOP started | slot=%s", slot or "manual")

    failure_reasons: List[str] = []
    try:
        character = naz_character.apply_event(
            memory.load_character_state(admin_user_id),
            "new_topic",
        )
        eligible_rubrics = [
            rubric
            for rubric in NAZ_TELEGRAM_RUBRICS
            if not slot or slot in [str(item) for item in rubric.get("slots", [])]
        ] or list(NAZ_TELEGRAM_RUBRICS)
        persona_rubric_rows, persona_source_rows = naz_telegram_editorial_catalog_rows()
        eligible_names = {str(rubric["name"]) for rubric in eligible_rubrics}
        rubric_rows = [
            row for row in persona_rubric_rows if str(row["name"]) in eligible_names
        ]
        eligible_keys = {str(row["key"]) for row in rubric_rows}
        source_rows = [
            row
            for row in persona_source_rows
            if eligible_keys.intersection(str(key) for key in row.get("rubric_keys", ()))
        ]
        seed = f"telegram:{current_bot_date()}:{slot or 'scheduled'}"
        plan = scheduled_plan(
            user_id=admin_user_id,
            platform="telegram",
            slot=slot or "scheduled",
            seed=seed,
            rubric_rows=rubric_rows,
            source_rows=source_rows,
            character=character,
            persona_rubric_rows=persona_rubric_rows,
            persona_source_rows=persona_source_rows,
        )
        memory.update_editorial_release_event(
            user_id=admin_user_id,
            plan_id=plan.plan_id,
            platform="telegram",
            slot=plan.slot,
            slot_captured_at=slot_captured_at,
            generation_package_status="not_run",
            image_qa_status="not_run",
            history_commit_status="pending",
        )
        logger.info(
            "NAZ_TELEGRAM_AUTO_LOOP orchestrated generation | slot=%s | plan_id=%s | rubric=%s",
            slot or "manual",
            plan.plan_id,
            plan.rubric,
        )
        try:
            package = await generate_scheduled_package(plan, character)
        except ScheduledTechnicalFailure:
            memory.update_editorial_release_event(
                user_id=admin_user_id,
                plan_id=plan.plan_id,
                platform="telegram",
                generation_package_status="invalid",
                history_commit_status="not_run",
            )
            failure_reasons.append("generation package remained technically invalid after one retry")
            await notify_admin(
                context.bot,
                "⚠️ Naz не выпустил scheduled-пост: модель дважды вернула технически непригодный пакет. План сохранён неизменным; публичный DIAG не создан.",
            )
            return
        except ScheduledContentReject as exc:
            memory.update_editorial_release_event(
                user_id=admin_user_id,
                plan_id=plan.plan_id,
                platform="telegram",
                generation_package_status="rejected",
                history_commit_status="not_run",
            )
            failure_reasons.append(f"local quality reject: {exc}")
            await notify_autopost_skip_once(context.bot, failure_reasons)
            return

        post_text = package.final_text
        memory.update_editorial_release_event(
            user_id=admin_user_id,
            plan_id=plan.plan_id,
            platform="telegram",
            generation_package_status="accepted",
            image_qa_status="not_run",
        )
        visual_brief = editorial_orchestrator.package_visual_brief(plan, package)
        selected_rubric = next(row for row in rubric_rows if str(row["name"]) == plan.rubric)
        image_count = max(1, min(int(str(selected_rubric.get("image_count") or CHANNEL_IMAGE_COUNT)), 4))
        images, image_prompt = await generate_images_with_retries(
            admin_user_id,
            f"{plan.rubric}. {plan.topic}",
            post_text,
            count=image_count,
            editorial_visual_brief=visual_brief,
        )
        if REQUIRE_IMAGES_FOR_CHANNEL_POSTS and not images:
            memory.update_editorial_release_event(
                user_id=admin_user_id,
                plan_id=plan.plan_id,
                platform="telegram",
                history_commit_status="not_run",
            )
            failure_reasons.append("images required but not generated for the planned subject")
            await notify_autopost_skip_once(context.bot, failure_reasons)
            return

        receipt = await send_observed_scheduled_post(
            bot=context.bot,
            chat_id=CHANNEL_ID,
            post_text=post_text,
            images=images,
            user_id=admin_user_id,
            plan_id=plan.plan_id,
        )
        plan_dict = plan.to_dict()
        saved_topic = f"{plan.topic} | {plan.rubric}"
        saved_task = f"naz_telegram_autopost:{plan.mode}:{plan.rubric}"
        memory.save_generated_post(
            user_id=admin_user_id,
            expert_mode=get_user_expert_mode(admin_user_id),
            task=saved_task,
            topic=saved_topic,
            content=post_text,
            image_count=len(images),
            published_to_channel=True,
            semantic_theme=plan.semantic_theme,
            semantic_card=plan.semantic_card,
            plan_id=plan.plan_id,
            editorial_plan=plan_dict,
        )
        queue_naz_post_for_void(
            post_text,
            source=saved_task,
            topic=saved_topic,
        )
        memory.record_content_signature(admin_user_id, plan_dict, plan.topic)
        memory.update_editorial_release_event(
            user_id=admin_user_id,
            plan_id=plan.plan_id,
            platform="telegram",
            telegram_chat_id=receipt.chat_id,
            telegram_message_id=receipt.message_id,
            history_commit_status="committed",
        )
        memory.save_character_state(admin_user_id, character)
        memory.apply_character_event(admin_user_id, "publish")
        logger.info(
            "AUTOPOST done | plan_id=%s | theme=%s | images=%s | image_qa_status=not_run",
            plan.plan_id,
            plan.semantic_theme,
            len(images),
        )
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("AUTOPOST failed: %s", exc)
        await notify_admin(context.bot, f"⚠️ AUTOPOST failed: {type(exc).__name__}: {exc}")
        # Последний уровень защиты: бот не должен падать из-за автопоста.


@scheduled_work_marker("source_monitor")
async def source_monitor_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not CHANNEL_ID:
        logger.warning("SOURCE_MONITOR skipped: CHANNEL_ID empty")
        return
    if not load_monitored_sources():
        logger.warning("SOURCE_MONITOR skipped: no monitored sources")
        return

    admin_user_id = ADMIN_ID or 0
    slot_captured_at = memory.utc_now()
    logger.info("SOURCE_MONITOR started")

    failure_reasons: List[str] = []
    try:
        candidates = await get_source_candidates(include_seen=False)
        if not candidates:
            logger.info("SOURCE_MONITOR skipped: no fresh candidates")
            return

        character = naz_character.apply_event(
            memory.load_character_state(admin_user_id), "new_topic"
        )
        rubric_names = list(
            dict.fromkeys(str(item.get("rubric") or "Source Monitor") for item in candidates[:12])
        )
        rubric_rows = [
            {
                "key": naz_editorial_catalog.rubric_key(name),
                "name": name,
                "kind": "source_monitor",
                "angle": "interpret one verified source without inventing facts or merely summarizing it",
                "track_tags": "daily,focus,reflective",
            }
            for name in rubric_names
        ]
        source_rows = []
        source_by_ref: Dict[str, Dict[str, Any]] = {}
        for item in candidates[:12]:
            ref = f"source_monitor:{source_item_key(item)}"
            rubric_key = naz_editorial_catalog.rubric_key(str(item.get("rubric") or "Source Monitor"))
            source_rows.append(
                {
                    "source_ref": ref,
                    "topic": str(item.get("title") or "Fresh monitored source"),
                    "source_type": "documented_source",
                    "rubric_keys": (rubric_key,),
                    "source_verified": bool(str(item.get("url") or "").startswith("http")),
                }
            )
            source_by_ref[ref] = item
        plan = scheduled_plan(
            user_id=admin_user_id,
            platform="telegram",
            slot="source_monitor",
            seed=f"source_monitor:{current_bot_date()}",
            rubric_rows=rubric_rows,
            source_rows=source_rows,
            character=character,
        )
        memory.update_editorial_release_event(
            user_id=admin_user_id,
            plan_id=plan.plan_id,
            platform="telegram",
            slot=plan.slot,
            slot_captured_at=slot_captured_at,
            generation_package_status="not_run",
            image_qa_status="not_run",
            history_commit_status="pending",
        )
        item = source_by_ref[plan.source_ref]
        source_material = (
            f"Title: {item.get('title', '')}\n"
            f"Summary: {item.get('summary', '')}\n"
            f"Source: {item.get('source_name', '')}\n"
            f"URL: {item.get('url', '')}"
        )
        try:
            package = await generate_scheduled_package(
                plan, character, source_material=source_material
            )
        except ScheduledTechnicalFailure:
            memory.update_editorial_release_event(
                user_id=admin_user_id,
                plan_id=plan.plan_id,
                platform="telegram",
                generation_package_status="invalid",
                history_commit_status="not_run",
            )
            await notify_admin(
                context.bot,
                "⚠️ Мониторинг источников Naz не выпустил пост: модель дважды вернула технически непригодный пакет. Публичный DIAG не создан.",
            )
            return
        except ScheduledContentReject as exc:
            memory.update_editorial_release_event(
                user_id=admin_user_id,
                plan_id=plan.plan_id,
                platform="telegram",
                generation_package_status="rejected",
                history_commit_status="not_run",
            )
            failure_reasons.append(f"local quality reject: {exc}")
            await notify_autopost_skip_once(context.bot, failure_reasons)
            return
        post_text = package.final_text
        memory.update_editorial_release_event(
            user_id=admin_user_id,
            plan_id=plan.plan_id,
            platform="telegram",
            generation_package_status="accepted",
            image_qa_status="not_run",
        )
        images, image_prompt = await generate_images_with_retries(
            admin_user_id,
            f"{plan.rubric}. {plan.topic}",
            post_text,
            count=CHANNEL_IMAGE_COUNT,
            editorial_visual_brief=editorial_orchestrator.package_visual_brief(plan, package),
        )
        if REQUIRE_IMAGES_FOR_CHANNEL_POSTS and not images:
            memory.update_editorial_release_event(
                user_id=admin_user_id,
                plan_id=plan.plan_id,
                platform="telegram",
                history_commit_status="not_run",
            )
            reason = f"source image failed: {item.get('title', '')}"
            logger.warning("SOURCE_MONITOR blocked: %s", reason)
            return

        receipt = await send_observed_scheduled_post(
            bot=context.bot,
            chat_id=CHANNEL_ID,
            post_text=post_text,
            images=images,
            user_id=admin_user_id,
            plan_id=plan.plan_id,
        )
        mark_source_seen(item)
        saved_task = f"source_monitor:{plan.rubric}"
        saved_topic = plan.topic
        plan_dict = plan.to_dict()
        memory.save_generated_post(
            user_id=admin_user_id,
            expert_mode=get_user_expert_mode(admin_user_id),
            task=saved_task,
            topic=saved_topic,
            content=post_text,
            image_count=len(images),
            published_to_channel=True,
            semantic_theme=plan.semantic_theme,
            semantic_card=plan.semantic_card,
            plan_id=plan.plan_id,
            editorial_plan=plan_dict,
        )
        queue_naz_post_for_void(post_text, source=saved_task, topic=saved_topic)
        memory.record_content_signature(admin_user_id, plan_dict, saved_topic)
        memory.update_editorial_release_event(
            user_id=admin_user_id,
            plan_id=plan.plan_id,
            platform="telegram",
            telegram_chat_id=receipt.chat_id,
            telegram_message_id=receipt.message_id,
            history_commit_status="committed",
        )
        memory.save_character_state(admin_user_id, character)
        memory.apply_character_event(admin_user_id, "publish")
        logger.info(
            "SOURCE_MONITOR done | plan_id=%s | theme=%s | images=%s | image_qa_status=not_run",
            plan.plan_id,
            plan.semantic_theme,
            len(images),
        )
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("SOURCE_MONITOR failed: %s", exc)
        await notify_admin(context.bot, f"⚠️ SOURCE_MONITOR failed: {type(exc).__name__}: {exc}")


@scheduled_work_marker("agent_content_sync")
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
            force=False,
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
            data={"slot": f"{hour:02d}:{minute:02d}", "owner": "naz_telegram"},
        )
        scheduled.append(f"{hour:02d}:{minute:02d}")

    if not scheduled:
        logger.warning("Autoposting enabled, but AUTOPOST_TIMES has no valid times")
        return

    logger.info("Autoposting scheduled at %s %s", ", ".join(scheduled), BOT_TIMEZONE)


def setup_naz_vk_schedule(application: Application) -> None:
    if NAZ_VK_SCHEDULER != "telegram":
        logger.info("Naz VK Telegram scheduler disabled | mode=%s", NAZ_VK_SCHEDULER)
        return
    if not NAZ_VK_ENABLED:
        logger.info("Naz VK disabled")
        return
    if not NAZ_VK_AUTO_ON:
        logger.info("Naz VK schedule disabled")
        return
    if not NAZ_VK_PUBLIC_ID:
        logger.warning("Naz VK schedule enabled, but NAZ_VK_PUBLIC_ID is empty")
        return

    if not application.job_queue:
        logger.warning("Naz VK JobQueue is not available")
        return
    tz = ZoneInfo(NAZ_VK_TIMEZONE)

    def parse_vk_time(raw_time: str, label: str) -> tuple[int, int] | None:
        try:
            hour_text, minute_text = raw_time.split(":", maxsplit=1)
            hour, minute = int(hour_text), int(minute_text)
            if hour not in range(24) or minute not in range(60):
                raise ValueError
        except ValueError:
            logger.warning("Invalid %s value skipped: %s", label, raw_time)
            return None
        return hour, minute

    scheduled = []
    daily = parse_vk_time(NAZ_VK_DAILY_TIME, "NAZ_VK_DAILY_TIME")
    if daily:
        hour, minute = daily
        slot = f"{hour:02d}:{minute:02d}"
        application.job_queue.run_daily(
            naz_vk_queue_job,
            time=time(hour=hour, minute=minute, tzinfo=tz),
            name=f"naz_vk_daily_{hour:02d}_{minute:02d}",
            data={"slot": slot, "rubric_kind": "daily"},
        )
        scheduled.append(f"daily {slot}")
    gaming = parse_vk_time(NAZ_VK_GAMING_TIME, "NAZ_VK_GAMING_TIME")
    if gaming:
        hour, minute = gaming
        slot = f"{hour:02d}:{minute:02d}"
        application.job_queue.run_daily(
            naz_vk_queue_job,
            time=time(hour=hour, minute=minute, tzinfo=tz),
            days=(2, 4, 0),
            name=f"naz_vk_gaming_{hour:02d}_{minute:02d}",
            data={"slot": slot, "rubric_kind": "gaming"},
        )
        scheduled.append(f"gaming Tue/Thu/Sun {slot}")
    if scheduled:
        logger.info("Naz VK queue scheduled at %s %s", ", ".join(scheduled), NAZ_VK_TIMEZONE)
    else:
        logger.warning("Naz VK enabled, but its schedule has no valid times")


@scheduled_work_marker("vk_embedded_producer")
async def naz_vk_queue_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if NAZ_VK_SCHEDULER != "telegram" or not (NAZ_VK_ENABLED and NAZ_VK_AUTO_ON):
        return
    slot = str((context.job.data or {}).get("slot", "manual"))
    rubric_kind = str((context.job.data or {}).get("rubric_kind", "daily"))
    today = datetime.now(ZoneInfo(NAZ_VK_TIMEZONE)).date().isoformat()
    source_ref = f"schedule:{today}:{rubric_kind}:{slot}"
    topic = (
        "Игровая лаборатория Naz VK: механика, мод, AI-инструмент или эксперимент для игроков"
        if rubric_kind == "gaming"
        else "Naz VK: практическая заметка об AI, разработке или контент-системах"
    )
    try:
        job = await create_naz_vk_job(
            topic,
            source_ref=source_ref,
            rubric_kind=rubric_kind,
            slot=slot,
        )
        logger.info("Naz VK job queued | job_id=%s | kind=%s | slot=%s", job["job_id"], rubric_kind, slot)
    except vk_publish_queue.DuplicateJobError:
        logger.info("Naz VK schedule cooldown: slot already queued | %s", source_ref)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Naz VK enqueue failed | slot=%s", slot)
        await notify_admin(context.bot, f"⚠️ Не удалось поставить задание VK ({slot}) в очередь: {exc}")


@scheduled_work_marker("vk_receipt_sync")
async def naz_vk_receipt_sync_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        result = sync_completed_naz_vk_jobs()
    except (OSError, vk_publish_queue.QueueError):
        logger.exception("Naz VK receipt sync unavailable")
        return
    logger.info(
        "Naz VK receipt sync | receipts_seen=%s | history_inserted=%s | "
        "already_recorded=%s | invalid_receipts=%s",
        result.receipts_seen,
        result.history_inserted,
        result.already_recorded,
        result.invalid_receipts,
    )


def setup_naz_vk_receipt_sync(application: Application) -> None:
    """Register receipt reconciliation independently of producer creation."""
    if not NAZ_VK_ENABLED:
        logger.info("Naz VK receipt sync disabled")
        return
    if not application.job_queue:
        logger.warning("Naz VK receipt sync JobQueue is unavailable")
        return
    application.job_queue.run_repeating(
        naz_vk_receipt_sync_job,
        interval=NAZ_VK_RECEIPT_SYNC_INTERVAL_SECONDS,
        first=15,
        name="naz_vk_receipt_sync",
    )
    logger.info(
        "Naz VK receipt sync scheduled | interval=%ss",
        NAZ_VK_RECEIPT_SYNC_INTERVAL_SECONDS,
    )


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


def setup_crosspost_exchange(application: Application) -> None:
    if not CROSSPOST_EXCHANGE_ENABLED:
        logger.info("Crosspost exchange disabled")
        return
    if not application.job_queue:
        logger.warning("JobQueue is not available. Crosspost exchange disabled.")
        return

    ensure_exchange_dirs()
    application.job_queue.run_repeating(
        crosspost_exchange_job,
        interval=CROSSPOST_EXCHANGE_INTERVAL_SECONDS,
        first=30,
        name="naz_crosspost_exchange",
    )
    logger.info(
        "Crosspost exchange enabled | dir=%s | interval=%ss | auto_publish=%s",
        CROSSPOST_EXCHANGE_DIR,
        CROSSPOST_EXCHANGE_INTERVAL_SECONDS,
        CROSSPOST_EXCHANGE_AUTO_PUBLISH,
    )


# -----------------------------------------------------------------------------
# Help texts
# -----------------------------------------------------------------------------


def help_capabilities_text() -> str:
    return (
        "🤖 Что умеет Naz\n\n"
        "Для сохранённых контактов:\n"
        "• текстовый и голосовой диалог с сохранением контекста;\n"
        "• голосовые ответы AI-голосом Naz;\n"
        "• генерация изображений и редактирование присланных фотографий;\n"
        "• посты, сценарии, идеи, заголовки и контент-планы;\n"
        "• игровые идеи и разборы;\n"
        "• безопасный доступ только к пользовательским функциям.\n\n"
        "Только для админа дополнительно:\n"
        "• список и управление контактами;\n"
        "• подготовка текстовых и голосовых сообщений контактам;\n"
        "• обязательный предпросмотр и подтверждение перед отправкой;\n"
        "• делегированные разговоры с контактами;\n"
        "• публикация и автопостинг в Telegram;\n"
        "• подготовка постов Naz для VK с музыкой;\n"
        "• источники и content-agent;\n"
        "• память, статистика, характер Naz и отношения Naz ↔ VOID;\n"
        "• обмен материалами Naz ↔ VOID."
    )


def contact_help_capabilities_text() -> str:
    return (
        "🤖 Что умеет Naz\n\n"
        "Для сохранённых контактов:\n"
        "• текстовый и голосовой диалог с сохранением контекста;\n"
        "• голосовые ответы AI-голосом Naz;\n"
        "• генерация изображений и редактирование присланных фотографий;\n"
        "• посты, сценарии, идеи, заголовки и контент-планы;\n"
        "• игровые идеи и разборы;\n"
        "• безопасный доступ только к пользовательским функциям."
    )


def help_capabilities_for(user_id: int) -> str:
    return help_capabilities_text() if is_admin(user_id) else contact_help_capabilities_text()


def help_commands_text() -> str:
    return (
        "📚 Команды Naz\n\n"
        "/start — главное меню\n"
        "/menu — открыть меню\n"
        "/help — помощь\n"
        "/state — текущий режим\n"
        "/character — живая грань и состояние Naz\n"
        "/character_event event — применить событие (admin)\n"
        "/character_set axis 0-100 — скорректировать состояние (admin)\n"
        "/character_simulate 10 — безопасно показать будущие состояния\n"
        "/relationship — состояние отношений Naz ↔ VOID\n"
        "/relationship_event event — применить событие отношений (admin)\n"
        "/thought_to_void текст — передать приватную мысль VOID\n"
        "/contacts — сохранённые контакты\n"
        "Напиши Диману: текст — подготовить разовое сообщение с подтверждением\n"
        "Отправь Диману голосовое: текст — подготовить AI-голос с подтверждением\n"
        "Напиши Диману, чтобы… — начать разговор с сохранённым контактом\n"
        "/contact_candidates — кто раньше писал боту, но ещё не записан\n"
        "/contact_add ID Имя — записать прежнего собеседника\n"
        "/delegate_stop ID — завершить поручение\n"
        "/roles — список ролей\n"
        "/role marketer — выбрать expert mode\n/voice tech_hooligan — выбрать голос Naz\n/goal engagement — выбрать цель контента\n"
        "/memory — память\n"
        "/clear — очистить свою память\n\n"
        "Контент:\n"
        "/post тема — обычный пост\n"
        "/viral тема — вирусный пост\n"
        "/script тема — сценарий Reels\n"
        "/plan тема — контент-план\n"
        "/gaming тема — игровой черновик Naz\n"
        "/gaming_commercial тема — игровой черновик с проверкой продукта\n"
        "/gaming_plan тема — показать игровую рубрику и формат\n"
        "/hooks тема — заголовки\n"
        "/imagepost тема — пост + 2 картинки\n"
        "/image тема — одна картинка\n"
        "/publish тема — сгенерировать и отправить в канал\n\n"
        "Void-кросспостинг:\n"
        "/void текст или reply — черновик Void → Naz\n"
        "/publish_void текст или reply — опубликовать Void → Naz в канал\n\n"
        "Связь Naz ↔ Void:\n"
        "Кнопка 🔗 Связи показывает очереди обмена и быстрые действия.\n"
        "Автообмен идёт через папки, без Telegram-пинг-понга.\n\n"
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


def contact_help_commands_text() -> str:
    return (
        "📚 Доступные команды Naz\n\n"
        "/start — открыть Naz\n"
        "/menu — главное меню\n"
        "/help — помощь\n"
        "/state — текущий режим\n"
        "/roles — список ролей\n"
        "/role marketer — выбрать экспертный режим\n"
        "/voice tech_hooligan — выбрать стиль ответа\n"
        "/goal engagement — выбрать цель\n"
        "/clear — очистить свою историю\n\n"
        "Контент:\n"
        "/post тема — пост\n"
        "/viral тема — вирусный пост\n"
        "/script тема — сценарий Reels\n"
        "/plan тема — контент-план\n"
        "/hooks тема — заголовки\n"
        "/imagepost тема — пост с картинками\n"
        "/image описание — создать картинку\n\n"
        "Можно просто написать или отправить голосовое. Чтобы изменить фото, пришли его с инструкцией в подписи."
    )


def help_commands_for(user_id: int) -> str:
    return help_commands_text() if is_admin(user_id) else contact_help_commands_text()


def help_about_text() -> str:
    return (
        "💬 О проекте\n\n"
        "Naz — это не учитель успешного успеха.\n"
        "Naz показывает путь через бардак: кривой код, ошибки, сломанные интеграции и дожим результата.\n\n"
        "Текущая архитектура Naz:\n"
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
    application.add_handler(MessageHandler(filters.ALL, registered_access_guard), group=-1)
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("state", state_command))
    application.add_handler(CommandHandler("character", character_command))
    application.add_handler(CommandHandler("character_event", character_event_command))
    application.add_handler(CommandHandler("character_set", character_set_command))
    application.add_handler(CommandHandler("character_simulate", character_simulate_command))
    application.add_handler(CommandHandler("relationship", relationship_command))
    application.add_handler(CommandHandler("relationship_event", relationship_event_command))
    application.add_handler(CommandHandler("roles", roles_command))
    application.add_handler(CommandHandler("role", role_command))
    application.add_handler(CommandHandler("voice", voice_command))
    application.add_handler(CommandHandler("goal", goal_command))
    application.add_handler(CommandHandler("memory", memory_command))
    application.add_handler(CommandHandler("clear", clear_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("dialog_context", dialog_context_command))
    application.add_handler(CommandHandler("dialog_reset", dialog_reset_command))
    application.add_handler(CommandHandler("vk_queue_status", vk_queue_status_command))
    application.add_handler(CommandHandler("vk_queue_draft", vk_queue_draft_command))
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
    application.add_handler(CommandHandler("gaming", gaming_draft_command))
    application.add_handler(CommandHandler("gaming_commercial", gaming_commercial_command))
    application.add_handler(CommandHandler("gaming_plan", gaming_plan_command))
    application.add_handler(CommandHandler("hooks", hooks_command))
    application.add_handler(CommandHandler("insight", insight_command))
    application.add_handler(CommandHandler("imagepost", imagepost_command))
    application.add_handler(CommandHandler("image", dialog_image_command))
    application.add_handler(CommandHandler("publish", publish_command))
    application.add_handler(CommandHandler("publish_insight", publish_insight_command))
    application.add_handler(CommandHandler("void", void_command))
    application.add_handler(CommandHandler("publish_void", publish_void_command))
    application.add_handler(CommandHandler("thought_to_void", thought_to_void_command))
    application.add_handler(CommandHandler("delegate", delegate_command))
    application.add_handler(CommandHandler("delegate_stop", delegate_stop_command))
    application.add_handler(CommandHandler("delegate_reply", delegate_reply_command))
    application.add_handler(CommandHandler("contacts", contacts_command))
    application.add_handler(CommandHandler("contact_candidates", contact_candidates_command))
    application.add_handler(CommandHandler("contact_add", contact_add_command))
    application.add_handler(
        CallbackQueryHandler(contact_message_callback, pattern=r"^contact_(?:send|cancel):\d+$")
    )

    # A shared contact follows /delegate and is used only for this one conversation.
    application.add_handler(MessageHandler(filters.CONTACT, delegation_contact))
    application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice_message))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo_instruction))

    # Text router must be after commands.
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(error_handler)

    setup_autoposting(application)
    setup_naz_vk_schedule(application)
    setup_naz_vk_receipt_sync(application)
    setup_source_monitoring(application)
    setup_agent_content_sync(application)
    setup_crosspost_exchange(application)
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
