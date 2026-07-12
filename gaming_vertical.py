"""Editorial planning for the Naz/VOID gaming vertical."""

from __future__ import annotations

from hashlib import sha256
from typing import Any, Iterable


RUBRICS = {
    "naz": (
        ("on_my_skin", "На своей шкуре", "проверка идеи, механики или инструмента без позы эксперта"),
        ("under_the_hood", "Под капотом", "как устроена одна игровая механика и почему она работает"),
        ("patch_no_marketing", "Патчноут без маркетинга", "что обновление реально меняет для игрока"),
        ("builder_lab", "Игровая лаборатория", "мод, AI-инструмент, прототип или автоматизация для игроков"),
        ("digital_wardrobe", "Цифровой гардероб", "скины как дизайн, идентичность и экономика без серых сделок"),
        ("sharp_respawn", "Респаун с сарказмом", "смешное наблюдение об игре, сообществе или индустрии"),
    ),
    "void": (
        ("after_save", "После сохранения", "человеческая мысль, которая остаётся после игровой сессии"),
        ("attention_loop", "Петля внимания", "гринд, ежедневные награды и цена удержания игрока"),
        ("world_remembers", "Мир помнит", "история, атмосфера, выбор и след игрока в виртуальном мире"),
        ("price_of_progress", "Цена прогресса", "монетизация и момент, когда игра начинает играть человеком"),
        ("quiet_multiplayer", "Тихий мультиплеер", "одиночество, дружба и близость между аватарами"),
        ("old_player_smiles", "Старый игрок усмехнулся", "спокойная ирония над очередной великой революцией"),
    ),
}

FORMATS = (
    ("field_note", "полевая заметка", "одна конкретная деталь и честный вывод"),
    ("mechanic_breakdown", "разбор механики", "хук, принцип работы, влияние на игрока"),
    ("micro_review", "микрорецензия", "не оценка из десяти, а один ясный тезис"),
    ("player_diary", "дневник игрока", "сцена или переживание без выдуманного личного опыта"),
    ("duo_question", "вопрос для спора", "позиция персонажа и настоящий выбор для аудитории"),
    ("builder_pitch", "лабораторный питч", "проблема игрока, маленький продукт, способ проверки спроса"),
)

COMMERCIAL_ANGLES = (
    ("none", "без продажи", "сначала ценность и разговор с аудиторией"),
    ("original_asset", "оригинальный ассет", "свой скин, модель, текстура или оформление в разрешённой creator-экосистеме"),
    ("player_tool", "инструмент игрока", "бот, аналитика, помощник сообщества или автоматизация"),
    ("audience_test", "проверка спроса", "опрос, лист ожидания или прототип без обещаний купить"),
)


def _pick(items: tuple[tuple[str, str, str], ...], recent_keys: list[str], seed: str) -> tuple[str, str, str]:
    digest = int(sha256(seed.encode("utf-8")).hexdigest()[:12], 16)

    def score(item: tuple[str, str, str]) -> tuple[int, int]:
        novelty = 1 if item[0] not in recent_keys else 0
        tie = digest ^ int(sha256(item[0].encode("utf-8")).hexdigest()[:12], 16)
        return novelty, tie

    return max(items, key=score)


def plan_gaming_content(
    character_id: str,
    topic: str,
    recent: Iterable[dict[str, Any]],
    *,
    platform: str = "telegram",
    commercial: bool = False,
) -> dict[str, str]:
    character = "naz" if character_id.casefold() == "naz" else "void"
    history = list(recent)
    recent_rubrics = [str(item.get("facet", "")).removeprefix("gaming_") for item in history[-10:]]
    recent_formats = [str(item.get("content_format", "")) for item in history[-8:]]
    rubric = _pick(RUBRICS[character], recent_rubrics, f"{character}:{topic}:rubric")
    content_format = _pick(FORMATS, recent_formats, f"{character}:{topic}:format")
    angles = COMMERCIAL_ANGLES[1:] if commercial else COMMERCIAL_ANGLES[:1]
    commercial_angle = _pick(angles, [], f"{character}:{topic}:commerce")
    return {
        "platform": platform,
        "facet": f"gaming_{rubric[0]}",
        "intent": rubric[1],
        "format": content_format[1],
        "content_format": content_format[0],
        "content_kind": "text",
        "hook": "игровая деталь",
        "media": "игровой кадр или оригинальный концепт без чужих логотипов",
        "topic": topic,
        "rubric_brief": rubric[2],
        "format_brief": content_format[2],
        "commercial_angle": commercial_angle[1],
        "commercial_brief": commercial_angle[2],
    }


def prompt_context(character_id: str, plan: dict[str, str]) -> str:
    character = "Naz" if character_id.casefold() == "naz" else "VOID"
    voice = (
        "Naz любопытный молодой билдер: быстро проверяет идеи, смеётся над пафосом и честно говорит, чего ещё не знает."
        if character == "Naz"
        else "VOID взрослый наблюдатель: видит за механикой человека, не морализирует и допускает искреннее удовольствие от игры."
    )
    return (
        f"Игровая вертикаль. Автор: {character}. {voice}\n"
        f"Рубрика: {plan['intent']} — {plan['rubric_brief']}.\n"
        f"Формат: {plan['format']} — {plan['format_brief']}.\n"
        f"Коммерческий угол: {plan['commercial_angle']} — {plan['commercial_brief']}.\n"
        "Не утверждай, что персонаж лично играл или тестировал игру, если в исходных данных нет такого опыта. "
        "Не пересказывай пресс-релиз и не выдумывай патчноуты, цены или факты. "
        "Не рекламируй продажу аккаунтов, передачу логинов, серые сделки со скинами или гарантированный заработок. "
        "Допустимы только оригинальные ассеты, официальные creator-площадки, инструменты для игроков и честная проверка спроса. "
        "Сделай самостоятельный живой текст, а не рубрикатор или бизнес-план."
    )
