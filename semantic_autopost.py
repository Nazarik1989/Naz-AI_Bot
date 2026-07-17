"""Server-side semantic planning and duplicate gate for Naz autoposting.

The module is deliberately independent from Telegram, VK and SQLite.  It owns
the editorial theme catalogue, builds the semantic-review prompt and enforces
the hard two-generation limit.  Persistence and model calls stay in ``main``
and ``memory``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Awaitable, Callable, Iterable, Mapping, Sequence


THEME_COOLDOWN = 5
SEMANTIC_HISTORY_LIMIT = 8
MAX_GENERATIONS = 2


@dataclass(frozen=True, slots=True)
class SemanticTheme:
    key: str
    label: str
    brief: str
    scenes: tuple[str, ...]
    conclusions: tuple[str, ...]


THEMES: tuple[SemanticTheme, ...] = (
    SemanticTheme(
        "relationships",
        "отношения и дистанция",
        "как люди договариваются, слышат друг друга и сохраняют границы без роли гуру",
        ("неудобная пауза в переписке", "совместная работа, где двое по-разному поняли одно слово"),
        ("ясность иногда важнее немедленного согласия", "дистанция может быть формой уважения, а не холодом"),
    ),
    SemanticTheme(
        "city",
        "город и повседневный маршрут",
        "городская деталь, ритм улицы, транспорта или района и её человеческий смысл",
        ("пустая остановка после поздней смены", "двор, который утром и вечером ощущается разным"),
        ("маршрут меняет внимание сильнее, чем громкая цель", "город становится понятнее через маленькие привычки"),
    ),
    SemanticTheme(
        "work",
        "работа и ремесло",
        "не героизм, а устройство труда, ответственность, темп и цена незаметной работы",
        ("передача незаконченной задачи другому человеку", "скучная проверка перед запуском"),
        ("хорошая работа видна по снижению чужой неопределённости", "ремесло начинается там, где результат можно проверить"),
    ),
    SemanticTheme(
        "creativity",
        "творчество и ограничение",
        "как идея становится формой, что дают ограничения и почему вкус не равен украшательству",
        ("черновик, из которого пришлось убрать любимую деталь", "пустой экран и одно жёсткое ограничение"),
        ("ограничение иногда не мешает идее, а проявляет её", "законченная форма важнее количества красивых заготовок"),
    ),
    SemanticTheme(
        "music",
        "музыка и слушание",
        "трек, тишина, ритм или звук как опыт внимания, а не декоративная ссылка",
        ("один трек в наушниках по дороге домой", "тишина после слишком громкого плейлиста"),
        ("слушание — это действие, а не фон", "ритм помогает заметить состояние раньше слов"),
    ),
    SemanticTheme(
        "game",
        "игра и выбор игрока",
        "механика, риск, удовольствие, идентичность или цена решения внутри игры",
        ("выбор без идеального варианта перед сохранением", "простая механика, которая меняет поведение всей команды"),
        ("интерес рождается не из награды, а из значимого выбора", "хорошая механика уважает время и любопытство игрока"),
    ),
    SemanticTheme(
        "body",
        "телесность и энергия",
        "усталость, темп, сон, движение и физические сигналы без псевдомедицины",
        ("плечи, которые заметили конец рабочего дня раньше головы", "короткая прогулка между двумя сложными задачами"),
        ("тело не саботирует план, а сообщает цену темпа", "пауза может быть частью точной работы"),
    ),
    SemanticTheme(
        "domestic_absurdity",
        "бытовой абсурд",
        "маленькая нелепость дома или в работе, которая открывает неожиданный практический угол",
        ("зарядка нашлась рядом с устройством, которое уже разрядилось", "стикер с напоминанием пережил саму задачу"),
        ("нелепость полезна, когда показывает лишнее действие", "бытовая ошибка иногда честнее большой теории"),
    ),
    SemanticTheme(
        "memory",
        "память и след",
        "что стоит сохранять, что забывать и как прошлое влияет на следующий выбор",
        ("старая заметка, смысл которой изменился через месяц", "история решения без объяснения, зачем оно было принято"),
        ("память полезна не объёмом, а возможностью восстановить причину", "забывание тоже может быть осознанной настройкой"),
    ),
    SemanticTheme(
        "care",
        "забота и надёжность",
        "забота как конкретное действие, снижающее риск или чужую нагрузку",
        ("понятное сообщение об ошибке вместо молчания", "запасной путь, который понадобился только одному человеку"),
        ("забота становится настоящей, когда встроена в действие", "надёжность — это уважение к чужому времени"),
    ),
    SemanticTheme(
        "conflict",
        "конфликт и границы",
        "столкновение целей, правил или ожиданий без обязательной победы одной стороны",
        ("требование сделать быстрее столкнулось с требованием сделать безопасно", "правило помогало команде, пока не стало мешать человеку"),
        ("конфликт полезен, когда показывает реальную цену выбора", "граница объясняет ответственность лучше запрета"),
    ),
    SemanticTheme(
        "practical_future",
        "будущее на практике",
        "не прогноз и не хайп, а ближайшее изменение в привычке, инструменте или ответственности",
        ("новая функция изменила один обычный рабочий шаг", "человек впервые отказался автоматизировать лишнее действие"),
        ("будущее начинается с новой границы ответственности", "полезная технология сначала меняет маленькую привычку"),
    ),
    SemanticTheme(
        "attention",
        "внимание и выбор",
        "куда уходит внимание, что его удерживает и какой выбор остаётся за человеком",
        ("уведомление прервало почти сформулированную мысль", "игровая награда заставила зайти без желания играть"),
        ("внимание — ограниченный выбор, а не бесплатный ресурс", "удержание не всегда означает интерес"),
    ),
)

THEMES_BY_KEY = {theme.key: theme for theme in THEMES}
EXPERIENTIAL_THEME_KEYS = frozenset(
    {"city", "music", "game", "body", "domestic_absurdity", "memory"}
)

# A rubric narrows the catalogue; it does not dictate a ready-made topic.
RUBRIC_THEME_KEYS: Mapping[str, tuple[str, ...]] = {
    "Утренний дожим": ("work", "body", "care", "domestic_absurdity", "attention", "practical_future", "creativity"),
    "AI без магии": ("work", "care", "conflict", "practical_future", "attention", "relationships", "creativity", "city", "music", "game", "body", "domestic_absurdity", "memory"),
    "Баг, который стал системой": ("work", "care", "conflict", "memory", "domestic_absurdity", "relationships", "practical_future"),
    "Naz после смены": ("relationships", "city", "music", "body", "memory", "care", "creativity", "attention"),
    "Naz Dev Log": ("work", "care", "memory", "conflict", "domestic_absurdity", "practical_future", "body"),
    "AI без успешного успеха": ("work", "care", "conflict", "practical_future", "attention", "relationships", "creativity", "city", "music", "game", "body", "domestic_absurdity", "memory"),
    "Ошибка недели": ("work", "care", "conflict", "memory", "domestic_absurdity", "relationships", "practical_future", "body", "creativity"),
    "Полевая заметка Naz": ("relationships", "city", "work", "creativity", "music", "game", "body", "domestic_absurdity", "memory", "care", "conflict", "practical_future", "attention"),
    "Маленький эксперимент": ("relationships", "city", "work", "creativity", "music", "game", "body", "domestic_absurdity", "memory", "care", "conflict", "practical_future", "attention"),
    "Человеческая деталь": ("relationships", "city", "work", "creativity", "music", "body", "domestic_absurdity", "memory", "care", "conflict", "attention"),
    "Игровая лаборатория VK": ("game", "relationships", "creativity", "music", "conflict", "attention", "practical_future"),
    "visual_archive": ("city", "relationships", "creativity", "music", "body", "domestic_absurdity", "memory", "attention"),
}

# A corrective axis should be meaningfully distant, not merely a different key
# from the same clarity/control/responsibility cluster.
DIVERGENT_THEME_KEYS: Mapping[str, tuple[str, ...]] = {
    "relationships": ("game", "music", "city", "body", "domestic_absurdity", "memory"),
    "city": ("game", "music", "body", "domestic_absurdity", "memory"),
    "work": ("body", "domestic_absurdity", "music", "city", "game", "memory"),
    "creativity": ("city", "music", "game", "body", "domestic_absurdity", "memory"),
    "music": ("game", "city", "body", "domestic_absurdity", "memory"),
    "game": ("city", "music", "memory", "body", "domestic_absurdity"),
    "body": ("music", "game", "city", "domestic_absurdity", "memory"),
    "domestic_absurdity": ("city", "music", "game", "body", "memory"),
    "memory": ("game", "music", "city", "body", "domestic_absurdity"),
    "care": ("game", "music", "city", "body", "domestic_absurdity", "memory"),
    "conflict": ("music", "city", "body", "domestic_absurdity", "memory", "game"),
    "practical_future": ("relationships", "city", "music", "game", "body", "domestic_absurdity", "memory"),
    "attention": ("relationships", "city", "music", "game", "body", "domestic_absurdity", "memory"),
}


@dataclass(frozen=True, slots=True)
class SemanticDecision:
    accepted: bool
    reason: str
    central_thesis: str
    conclusion: str
    narrative_shape: str
    key_meanings: tuple[str, ...]
    occupied_theme_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GenerationResult:
    accepted: bool
    text: str
    attempts: int
    decision: SemanticDecision
    theme_key: str = ""


class NoSemanticThemeAvailable(RuntimeError):
    """All rubric-compatible themes are still inside the shared cooldown."""


def compatible_themes(rubric_name: str) -> tuple[SemanticTheme, ...]:
    keys = RUBRIC_THEME_KEYS.get(rubric_name)
    if not keys:
        keys = tuple(theme.key for theme in THEMES)
    return tuple(THEMES_BY_KEY[key] for key in keys)


def select_theme(
    rubric_name: str,
    recent_theme_keys: Iterable[str],
    *,
    platform: str,
    seed: str,
    excluded_theme_keys: Iterable[str] = (),
) -> SemanticTheme:
    """Choose a rubric-compatible axis before generation, across platforms."""
    recent = [str(key) for key in recent_theme_keys if str(key).strip()][-THEME_COOLDOWN:]
    excluded = {
        str(key)
        for key in excluded_theme_keys
        if str(key).strip()
    }
    candidates = [
        theme
        for theme in compatible_themes(rubric_name)
        if theme.key not in recent and theme.key not in excluded
    ]
    if not candidates:
        raise NoSemanticThemeAvailable(
            f"semantic theme cooldown exhausted for rubric={rubric_name!r}"
        )
    return max(
        candidates,
        key=lambda theme: sha256(
            f"{seed}|{platform}|{rubric_name}|{theme.key}".encode("utf-8")
        ).hexdigest(),
    )


def select_correction_theme(
    rubric_name: str,
    recent_theme_keys: Iterable[str],
    *,
    initial_theme_key: str,
    platform: str,
    seed: str,
    excluded_theme_keys: Iterable[str] = (),
) -> SemanticTheme:
    """Prefer a rubric-compatible axis outside the initial theme's meaning cluster."""
    recent = {
        str(key)
        for key in list(recent_theme_keys)[-THEME_COOLDOWN:]
        if str(key).strip()
    }
    preferred = set(DIVERGENT_THEME_KEYS.get(initial_theme_key, ()))
    excluded = {
        str(key)
        for key in excluded_theme_keys
        if str(key).strip()
    }
    candidates = [
        theme
        for theme in compatible_themes(rubric_name)
        if theme.key in preferred
        and theme.key not in recent
        and theme.key not in excluded
        and theme.key != initial_theme_key
    ]
    if not candidates:
        try:
            return select_theme(
                rubric_name,
                recent,
                platform=platform,
                seed=seed,
                excluded_theme_keys=(initial_theme_key, *excluded),
            )
        except NoSemanticThemeAvailable:
            return select_theme(
                rubric_name,
                recent,
                platform=platform,
                seed=seed,
                excluded_theme_keys=(initial_theme_key,),
            )
    return max(
        candidates,
        key=lambda theme: sha256(
            f"{seed}|{platform}|{rubric_name}|{theme.key}".encode("utf-8")
        ).hexdigest(),
    )


def platform_context(platform: str) -> str:
    if platform == "vk":
        return (
            "Площадка: VK. Пиши самостоятельный VK-пост: связные абзацы, без ссылок на Telegram, "
            "Telegram-канал, его лимиты, кнопки или формат."
        )
    if platform == "telegram":
        return (
            "Площадка: Telegram. Пиши самостоятельный Telegram-пост: короткие читаемые абзацы, "
            "без ссылок на VK, музыку VK, группу VK или её механику публикации."
        )
    raise ValueError(f"unsupported autopost platform: {platform}")


def theme_instruction(theme: SemanticTheme, *, platform: str, rubric_name: str) -> str:
    axis_ownership = ""
    if theme.key in EXPERIENTIAL_THEME_KEYS:
        axis_ownership = (
            "\nЭта опытная ось обязана владеть центральным тезисом и финальным выводом. "
            "Техническая рубрика даёт контекст или событие, но не подменяет вывод универсальным "
            "советом о том, как правильно использовать AI, автоматизацию, инструмент или систему."
        )
    return (
        f"{platform_context(platform)}\n"
        f"Server-side смысловая ось выпуска: {theme.label} ({theme.key}).\n"
        f"Граница оси: {theme.brief}.\n"
        f"Рубрика: {rubric_name}. Ось должна раскрыться через материал рубрики, а не подменить его случайной темой.\n"
        "Характер Naz — это точка зрения, ритм и интонация. Не делай его биографию, билдера, бардак, "
        "систему, дожим или одну из core truths обязательной темой и одинаковой моралью поста.\n"
        "У поста должны быть конкретная сцена и собственный вывод, соответствующие выбранной оси."
        f"{axis_ownership}"
    )


def correction_instruction(
    theme: SemanticTheme,
    rejected: SemanticDecision,
    *,
    platform: str,
    rubric_name: str,
) -> str:
    rejected_meanings = "; ".join(rejected.key_meanings) or "(не выделены)"
    return (
        f"{theme_instruction(theme, platform=platform, rubric_name=rubric_name)}\n"
        "Это единственная корректирующая попытка. Первый вариант отклонён как смысловой повтор.\n"
        "Семантическое резюме отклонённого варианта — это запрет на повтор мысли, "
        "а не слова, которые нужно механически заменить:\n"
        f"- причина отказа: {rejected.reason or '(не указана)'}\n"
        f"- центральный тезис: {rejected.central_thesis or '(не указан)'}\n"
        f"- вывод: {rejected.conclusion or '(не указан)'}\n"
        f"- сюжетная форма: {rejected.narrative_shape or '(не указана)'}\n"
        f"- ключевые смыслы: {rejected_meanings}\n"
        "Самостоятельно выбери новую конкретную сцену, которой нет ни в отклонённом варианте, "
        "ни в последних постах из anti-repeat context. Не превращай готовые примеры оси "
        "в обязательный шаблон.\n"
        "Сформулируй существенно другой самостоятельный вывод из новой сцены и границ выбранной оси. "
        "Не используй готовую типовую мораль оси: вывод должен принадлежать именно этому выпуску.\n"
        "Не перефразируй первый вариант, не сохраняй его сюжетный ход и не приходи к его морали другими словами."
    )


def generation_history_context(
    recent_posts: Sequence[Mapping[str, str]],
) -> str:
    """Give the writer semantic context that was previously visible only to the gate."""
    if not recent_posts:
        return ""
    history_blocks = []
    for index, post in enumerate(recent_posts[-SEMANTIC_HISTORY_LIMIT:], start=1):
        platform = str(post.get("platform") or "unknown")
        theme = str(post.get("semantic_theme") or "legacy/unknown")
        content = " ".join(str(post.get("content") or "").split())[:1400]
        if content:
            history_blocks.append(
                f"[{index}] platform={platform}; theme={theme}\n{content}"
            )
    if not history_blocks:
        return ""
    return (
        "SEMANTIC ANTI-REPEAT CONTEXT — последние принятые или подготовленные посты общего "
        "персонажа Naz на Telegram/VK. Это не сырьё для пересказа и не примеры стиля.\n"
        "До написания сравни будущий центральный тезис, вывод, сюжетный ход и набор ключевых "
        "смыслов со всем списком. Выбери действительно другую мысль внутри заданной оси; "
        "не заменяй только лексику, декорацию или метафору.\n\n"
        + "\n\n".join(history_blocks)
    )


def build_gate_prompt(
    candidate: str,
    recent_posts: Sequence[Mapping[str, str]],
    *,
    theme_catalog: Sequence[SemanticTheme] = THEMES,
) -> str:
    history_blocks = []
    for index, post in enumerate(recent_posts[-SEMANTIC_HISTORY_LIMIT:], start=1):
        theme = str(post.get("semantic_theme") or "legacy/unknown")
        content = str(post.get("content") or "")[:3500]
        history_blocks.append(f"[{index}] theme={theme}\n{content}")
    history = "\n\n".join(history_blocks) or "(история пуста)"
    catalog = "\n".join(
        f"- {theme.key}: {theme.label}; {theme.brief}"
        for theme in theme_catalog
    )
    return (
        "Ты — строгий семантический редактор Naz. Сравни кандидат с последними принятыми/подготовленными "
        "постами по смыслу, а не по отдельным словам.\n"
        "Отклони кандидат, если повторяется центральный тезис или вывод, та же мысль пересказана другой "
        "лексикой, близко повторён сюжетный ход, либо почти совпадает набор ключевых смыслов. Общий голос "
        "персонажа сам по себе дублем не считается.\n"
        "Отдельно классифицируй, какие оси из каталога уже заняты ПОСЛЕДНИМИ ПОСТАМИ: ось занята, если "
        "центральный тезис, вывод или основной сюжетный ход хотя бы одного поста существенно относится "
        "к ней. Смотри на содержание, а не на сохранённую theme-метку. Кандидат в occupied_theme_keys "
        "не учитывай. Не придумывай новую ось и не выбирай retry.\n"
        "Верни только JSON без markdown:\n"
        '{"accepted":true|false,"reason":"коротко","central_thesis":"одна мысль",'
        '"conclusion":"эмоциональный/практический вывод","narrative_shape":"сцена→поворот→вывод",'
        '"key_meanings":["смысл 1","смысл 2"],"occupied_theme_keys":["key_из_каталога"]}\n\n'
        f"КАТАЛОГ ОСЕЙ:\n{catalog}\n\n"
        f"ПОСЛЕДНИЕ ПОСТЫ:\n{history}\n\nКАНДИДАТ:\n{candidate[:5000]}"
    )


def parse_gate_response(raw: str) -> SemanticDecision:
    value = str(raw or "").strip()
    value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value, flags=re.IGNORECASE)
    try:
        payload = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("semantic gate returned invalid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("accepted"), bool):
        raise ValueError("semantic gate response lacks boolean accepted")
    meanings = payload.get("key_meanings")
    if not isinstance(meanings, list):
        meanings = []
    occupied = payload.get("occupied_theme_keys")
    if not isinstance(occupied, list):
        occupied = []
    return SemanticDecision(
        accepted=payload["accepted"],
        reason=str(payload.get("reason") or "")[:500],
        central_thesis=str(payload.get("central_thesis") or "")[:1000],
        conclusion=str(payload.get("conclusion") or "")[:1000],
        narrative_shape=str(payload.get("narrative_shape") or "")[:500],
        key_meanings=tuple(str(item)[:300] for item in meanings[:8] if str(item).strip()),
        occupied_theme_keys=tuple(
            dict.fromkeys(
                str(item)[:80]
                for item in occupied[: len(THEMES)]
                if str(item).strip()
            )
        ),
    )


def blocked_decision(reason: str) -> SemanticDecision:
    return SemanticDecision(False, reason[:500], "", "", "", ())


async def generate_with_gate(
    *,
    generate: Callable[[str], Awaitable[str]],
    evaluate: Callable[[str], Awaitable[SemanticDecision]],
    theme: SemanticTheme,
    platform: str,
    rubric_name: str,
    is_model_warning: Callable[[str], bool],
    correction_theme: SemanticTheme | None = None,
    correction_theme_selector: Callable[[SemanticDecision], SemanticTheme | None] | None = None,
) -> GenerationResult:
    """Run exactly one initial generation and at most one corrective generation."""
    first_text = await generate(
        theme_instruction(theme, platform=platform, rubric_name=rubric_name)
    )
    if is_model_warning(first_text):
        first_decision = blocked_decision("generation blocked before semantic review")
    else:
        first_decision = await evaluate(first_text)
    if first_decision.accepted:
        return GenerationResult(True, first_text, 1, first_decision, theme.key)

    if correction_theme is not None:
        retry_theme = correction_theme
    elif correction_theme_selector is not None:
        retry_theme = correction_theme_selector(first_decision)
        if retry_theme is None:
            return GenerationResult(False, "", 1, first_decision, theme.key)
    else:
        retry_theme = theme
    second_text = await generate(
        correction_instruction(
            retry_theme,
            first_decision,
            platform=platform,
            rubric_name=rubric_name,
        )
    )
    if is_model_warning(second_text):
        second_decision = blocked_decision("corrective generation blocked before semantic review")
    else:
        second_decision = await evaluate(second_text)
    if second_decision.accepted:
        return GenerationResult(True, second_text, 2, second_decision, retry_theme.key)
    return GenerationResult(False, "", 2, second_decision, retry_theme.key)
