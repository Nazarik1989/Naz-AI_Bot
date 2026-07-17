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
MAX_RELEASE_PLANS = 3


@dataclass(frozen=True, slots=True)
class SemanticTheme:
    key: str
    label: str
    brief: str
    scenes: tuple[str, ...]
    conclusions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SemanticCard:
    key: str
    theme_key: str
    scene: str
    tension: str
    thesis: str
    conclusion_boundary: str


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

SEMANTIC_CARDS: tuple[SemanticCard, ...] = (
    SemanticCard("relationships_pause", "relationships", "пауза в переписке или разговоре", "скорость ответа против точности понимания", "пауза может быть способом не исказить ответ, а не знаком холода", "не сводить вывод к универсальному совету отвечать медленнее"),
    SemanticCard("relationships_help", "relationships", "помощь, о которой не просили", "доброе намерение против чужой самостоятельности", "поддержка остаётся поддержкой, пока оставляет человеку право отказаться", "не морализировать про личные границы вообще"),
    SemanticCard("relationships_shared_word", "relationships", "двое по-разному поняли одно бытовое слово или договорённость", "кажущееся согласие против реального смысла", "уточнение общего языка иногда важнее формального согласия", "не завершать тезисом «надо лучше коммуницировать»"),
    SemanticCard("city_detour", "city", "случайный обход привычного городского маршрута", "эффективность пути против нового наблюдения", "город раскрывается не только через цель, но и через вынужденные отклонения", "не превращать прогулку в совет по продуктивности"),
    SemanticCard("city_after_close", "city", "место после закрытия: погасшая витрина, пустой двор или остановка", "назначение места против его жизни вне расписания", "городская деталь меняет смысл, когда исчезает обслуживающая её функция", "не делать вывод про одиночество как универсальную драму"),
    SemanticCard("city_shared_silence", "city", "короткое молчаливое соседство незнакомых людей", "анонимность против временной общности", "совместное пространство иногда создаёт связь без знакомства", "не приписывать незнакомцам выдуманные биографии"),
    SemanticCard("work_handoff", "work", "передача незавершённой работы другому человеку", "желание выглядеть закончившим против ясности состояния", "качество работы включает честно обозначенный незакрытый край", "не сводить вывод к чек-листам и контролю мелочей"),
    SemanticCard("work_stop", "work", "остановка процесса из-за слабого, но странного сигнала", "темп против цены продолжения вслепую", "профессионализм иногда проявляется в своевременной остановке, а не в героическом продолжении", "не повторять мораль «слушай тело» или «проверяй всё»"),
    SemanticCard("work_remove_ritual", "work", "ненужный шаг, который продолжают выполнять по привычке", "ритуал надёжности против реальной пользы", "ремесло умеет не только добавлять процедуры, но и убирать пустые", "не рекламировать автоматизацию как готовый ответ"),
    SemanticCard("creativity_cut", "creativity", "любимая деталь, которую пришлось убрать из законченной работы", "привязанность автора против целостности формы", "отказ от удачного фрагмента может сделать замысел слышнее", "не заканчивать лозунгом про смелость отпускать"),
    SemanticCard("creativity_constraint", "creativity", "жёсткое ограничение материала, цвета, длины или времени", "свобода вариантов против ясности решения", "ограничение может проявить выбор, который прятался в изобилии", "не объявлять любые ограничения полезными"),
    SemanticCard("creativity_unfinished", "creativity", "черновик, который ценен не как будущий шедевр", "завершённость против сохранённого направления мысли", "иногда функция черновика — не стать финалом, а удержать поворот", "не романтизировать вечную незавершённость"),
    SemanticCard("music_changed_room", "music", "один трек по-разному звучит в комнате и в движении", "неизменная запись против меняющегося слушателя", "контекст слышания участвует в музыке почти как ещё один инструмент", "не делать музыку лекарством от любого состояния"),
    SemanticCard("music_after_sound", "music", "несколько секунд после окончания альбома или концерта", "желание немедленно заполнить тишину против остаточного звучания", "часть музыкального опыта возникает уже после последней ноты", "не превращать тишину в превосходство над шумом"),
    SemanticCard("music_wrong_track", "music", "неподходящий по настроению трек, который неожиданно сработал", "ожидаемое соответствие против полезного несовпадения", "музыка иногда меняет состояние именно потому, что не подтверждает его", "не советовать универсальные плейлисты"),
    SemanticCard("game_build_cost", "game", "выбор билда, который закрывает другие возможности", "идеальный вариант против цены специализации", "интерес решения появляется там, где выбор действительно что-то отнимает", "не переносить игровую механику в банальный совет про жизнь"),
    SemanticCard("game_side_path", "game", "пропущенная побочная ветка из-за спешки к цели", "эффективное прохождение против любопытства", "игровой маршрут выражает приоритет игрока не хуже финального результата", "не утверждать, что медленная игра всегда правильнее"),
    SemanticCard("game_team_resource", "game", "игрок тратит редкий ресурс ради команды", "личная эффективность против общего темпа", "командная механика делает цену помощи видимой и потому честной", "не сводить к морали о самопожертвовании"),
    SemanticCard("body_temperature", "body", "температура воздуха, одежды или предмета меняет восприятие сцены", "абстрактный план против физического качества момента", "телесная деталь может менять смысл происходящего без диагноза и самокопания", "не заканчивать призывом «слушай сигналы тела»"),
    SemanticCard("body_stairs", "body", "разговор меняется после подъёма, дороги или другой простой нагрузки", "содержание слов против ритма дыхания и движения", "мысль существует не отдельно от темпа, в котором её произносят", "не делать вывод про отдых и продуктивность"),
    SemanticCard("body_hands", "body", "руки запоминают способ действия раньше формулировки", "объяснение против навыка", "часть знания живёт в повторённом движении, а не в красивом описании", "не объявлять интуицию безошибочной"),
    SemanticCard("domestic_wrong_place", "domestic_absurdity", "обычный предмет устойчиво живёт в нелогичном месте", "правильный порядок против удобства реальной жизни", "бытовая система честнее схемы, если отражает фактическое действие", "не возвращаться к коробкам, складам и генеральной уборке"),
    SemanticCard("domestic_duplicate", "domestic_absurdity", "дома обнаруживаются два одинаковых предмета, потому что первый постоянно терялся", "борьба с причиной против практичного обхода", "иногда дублирование — не бардак, а дешёвая плата за спокойный быт", "не превращать в урок про резервирование систем"),
    SemanticCard("domestic_label", "domestic_absurdity", "старая подпись пережила предмет или задачу", "память обозначения против изменившейся реальности", "ярлык может продолжать управлять действием после исчезновения причины", "не сводить к совету чаще обновлять списки"),
    SemanticCard("memory_receipt", "memory", "случайная бумажная мелочь сохраняет порядок событий", "ничтожность предмета против точности хронологии", "память иногда держится не на важном, а на правильно расположенном следе", "не романтизировать хранение любого хлама"),
    SemanticCard("memory_old_hint", "memory", "старая подсказка или название показывает прежний способ думать", "нынешнее самоописание против следа старой версии себя", "след прошлого ценен не ностальгией, а разницей между тогдашним и нынешним выбором", "не повторять сюжет про очистку дисков, архивов и удаление лишнего"),
    SemanticCard("memory_missing_piece", "memory", "в знакомой истории обнаруживается отсутствующая деталь", "уверенность воспоминания против неполноты источника", "пробел иногда честнее уверенно достроенного прошлого", "не заканчивать недоверием ко всей памяти"),
    SemanticCard("care_corridor_light", "care", "для поздно возвращающегося человека оставляют удобную мелочь в пространстве", "незаметность действия против реального облегчения", "забота может менять среду, не требуя благодарности и отчёта", "не повторять мораль про снижение чужой нагрузки вообще"),
    SemanticCard("care_mismatch", "care", "заранее обозначают различие вкуса, режима или ограничения", "удобство большинства против конкретного несовпадения", "надёжная забота учитывает различие до того, как оно станет проблемой", "не превращать в инструкцию по сервису"),
    SemanticCard("care_return", "care", "вещь возвращают так, чтобы следующий человек мог сразу ею пользоваться", "формальное возвращение против закрытого цикла", "уважение проявляется в состоянии, в котором после нас продолжают действие", "не сводить вывод к проверкам, зарядке и чек-листам"),
    SemanticCard("conflict_shared_resource", "conflict", "двум людям одновременно нужен один ресурс или пространство", "равные основания против невозможности полного удовлетворения", "справедливое решение может распределять не удобство, а неудобство", "не объявлять компромисс универсальной добродетелью"),
    SemanticCard("conflict_rule_exception", "conflict", "понятное правило сталкивается с редким исключением", "предсказуемость для всех против точности в одном случае", "зрелое правило умеет показать цену исключения, а не делать вид, что её нет", "не сводить к лозунгу про гибкость"),
    SemanticCard("conflict_different_clock", "conflict", "одна задача срочная для одного человека и обычная для другого", "разные временные масштабы против общей работы", "часть конфликта исчезает, когда стороны называют не позицию, а свой дедлайн", "не заканчивать советом «просто поговорите»"),
    SemanticCard("future_default", "practical_future", "новая настройка по умолчанию незаметно меняет повседневный выбор", "удобство автоматического решения против потери осознанного момента", "будущее чаще входит через дефолт, чем через громкое изобретение", "не делать технологию злодеем или спасителем"),
    SemanticCard("future_new_refusal", "practical_future", "новый инструмент создаёт действие, от которого приходится сознательно отказываться", "расширение возможностей против новой ответственности", "полезность технологии видна и по тому, какие лишние действия она позволяет не делать", "не повторять мораль про автоматизацию всего"),
    SemanticCard("future_visible_exception", "practical_future", "автоматизация делает редкое исключение заметнее обычного процесса", "масштабирование нормы против ценности отклонения", "хорошая система будущего не прячет исключения, а оставляет им понятный выход", "не уходить в архитектурный или AI-жаргон"),
    SemanticCard("attention_unfinished_thought", "attention", "уведомление приходит в момент почти сформулированной мысли", "мгновенный внешний сигнал против хрупкого внутреннего хода", "потеря внимания измеряется не минутами, а исчезнувшей связью между мыслями", "не завершать банальным призывом отключить уведомления"),
    SemanticCard("attention_background_change", "attention", "в знакомом фоне меняется один звук, свет или ритм", "привычное игнорирование против значимого отклонения", "внимание часто включается не громкостью, а нарушением ожидаемого рисунка", "не превращать наблюдение в проверку перед действием"),
    SemanticCard("attention_single_choice", "attention", "среди множества вариантов человек замечает один по неожиданной причине", "изобилие вариантов против личного критерия", "выбор становится своим, когда появляется причина, не заданная витриной", "не делать вывод про минимализм и отказ от выбора"),
)

CARDS_BY_THEME = {
    theme.key: tuple(card for card in SEMANTIC_CARDS if card.theme_key == theme.key)
    for theme in THEMES
}

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


@dataclass(frozen=True, slots=True)
class SemanticHistoryProfile:
    history_digest: str
    occupied_theme_keys: tuple[str, ...]
    exclusion_summary: str


@dataclass(frozen=True, slots=True)
class GenerationResult:
    accepted: bool
    text: str
    attempts: int
    decision: SemanticDecision
    theme_key: str = ""
    card_key: str = ""


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
    compatible = {theme.key for theme in compatible_themes(rubric_name)}
    start = 0
    if recent and recent[-1] in THEMES_BY_KEY:
        start = (
            next(index for index, theme in enumerate(THEMES) if theme.key == recent[-1])
            + 1
        ) % len(THEMES)
    ordered = (*THEMES[start:], *THEMES[:start])
    candidates = [
        theme
        for theme in ordered
        if theme.key in compatible
        and theme.key not in recent
        and theme.key not in excluded
    ]
    if not candidates:
        raise NoSemanticThemeAvailable(
            f"semantic theme cooldown exhausted for rubric={rubric_name!r}"
        )
    return candidates[0]


def select_card(theme_key: str, recent_card_keys: Iterable[str]) -> SemanticCard:
    """Rotate the editorial meanings inside one axis; never choose randomly."""
    cards = CARDS_BY_THEME.get(theme_key, ())
    if not cards:
        raise ValueError(f"semantic card catalog is empty for theme={theme_key!r}")
    recent = [str(key) for key in recent_card_keys if str(key).strip()]
    last_index = -1
    for key in reversed(recent):
        match = next((index for index, card in enumerate(cards) if card.key == key), None)
        if match is not None:
            last_index = match
            break
    return cards[(last_index + 1) % len(cards)]


def card_instruction(card: SemanticCard) -> str:
    return (
        f"Server-side смысловая карточка выпуска: {card.key}.\n"
        f"Поле конкретной сцены: {card.scene}.\n"
        f"Смысловое напряжение: {card.tension}.\n"
        f"Направление центрального тезиса: {card.thesis}.\n"
        f"Граница вывода: {card.conclusion_boundary}.\n"
        "Карточка задаёт уникальный смысл этого выпуска, но не готовый текст. "
        "Не перечисляй её поля и не превращай их в служебные заголовки."
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


def semantic_history_digest(
    recent_posts: Sequence[Mapping[str, str]],
) -> str:
    payload = [
        {
            "platform": str(post.get("platform") or ""),
            "semantic_theme": str(post.get("semantic_theme") or ""),
            "content": str(post.get("content") or ""),
        }
        for post in recent_posts[-SEMANTIC_HISTORY_LIMIT:]
    ]
    return sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def build_history_profile_prompt(
    recent_posts: Sequence[Mapping[str, str]],
) -> str:
    history_blocks = []
    for index, post in enumerate(recent_posts[-SEMANTIC_HISTORY_LIMIT:], start=1):
        platform = str(post.get("platform") or "unknown")
        stored_theme = str(post.get("semantic_theme") or "legacy/unknown")
        content = str(post.get("content") or "")[:3500]
        history_blocks.append(
            f"[{index}] platform={platform}; stored_theme={stored_theme}\n{content}"
        )
    history = "\n\n".join(history_blocks) or "(история пуста)"
    catalog = "\n".join(
        f"- {theme.key}: {theme.label}; {theme.brief}"
        for theme in THEMES
    )
    schema_items = ",".join(
        f'"{theme.key}":{{"occupied":true|false,"reason":"коротко"}}'
        for theme in THEMES
    )
    return (
        "Ты — отдельный семантический аудитор истории Naz. Кандидата нового поста здесь нет. "
        "Проверь КАЖДУЮ ось каталога по последним опубликованным постам.\n"
        "occupied=true, если центральный тезис, вывод или основной сюжетный ход хотя бы одного поста "
        "существенно занимает эту ось. Не ориентируйся на stored_theme: legacy-метка может быть пустой "
        "или неточной. Упоминание темы без центральной роли не делает ось занятой.\n"
        "Обязателен ровно один статус для КАЖДОГО ключа каталога. Нельзя пропускать ключи, добавлять новые "
        "или анализировать только самый заметный конфликт.\n"
        "Верни только JSON без markdown по точной схеме:\n"
        f'{{"themes":{{{schema_items}}}}}\n\n'
        f"КАТАЛОГ:\n{catalog}\n\nПОСЛЕДНИЕ ОПУБЛИКОВАННЫЕ ПОСТЫ:\n{history}"
    )


def parse_history_profile(
    raw: str,
    *,
    history_digest: str,
) -> SemanticHistoryProfile:
    value = str(raw or "").strip()
    value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value, flags=re.IGNORECASE)
    try:
        payload = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("semantic history profile returned invalid JSON") from exc
    statuses = payload.get("themes") if isinstance(payload, dict) else None
    expected = set(THEMES_BY_KEY)
    if not isinstance(statuses, dict) or set(statuses) != expected:
        raise ValueError("semantic history profile must contain every theme exactly once")
    occupied: list[str] = []
    summary: list[str] = []
    for theme in THEMES:
        status = statuses.get(theme.key)
        if not isinstance(status, dict) or not isinstance(status.get("occupied"), bool):
            raise ValueError(f"semantic history profile has invalid status for {theme.key}")
        reason = " ".join(str(status.get("reason") or "").split())[:300]
        if status["occupied"]:
            occupied.append(theme.key)
            summary.append(f"{theme.key}: {reason or 'ось уже занята историей'}")
    return SemanticHistoryProfile(
        history_digest=history_digest,
        occupied_theme_keys=tuple(occupied),
        exclusion_summary="\n".join(summary),
    )


def history_profile_context(profile: SemanticHistoryProfile) -> str:
    if not profile.occupied_theme_keys:
        return ""
    return (
        "SERVER SEMANTIC EXCLUSION LEDGER — уже занятые центральные смыслы последних публикаций. "
        "Не повторяй их под другим ярлыком, сценой или метафорой:\n"
        f"{profile.exclusion_summary}"
    )


def build_gate_prompt(
    candidate: str,
    recent_posts: Sequence[Mapping[str, str]],
) -> str:
    history_blocks = []
    for index, post in enumerate(recent_posts[-SEMANTIC_HISTORY_LIMIT:], start=1):
        theme = str(post.get("semantic_theme") or "legacy/unknown")
        content = str(post.get("content") or "")[:3500]
        history_blocks.append(f"[{index}] theme={theme}\n{content}")
    history = "\n\n".join(history_blocks) or "(история пуста)"
    return (
        "Ты — строгий семантический редактор Naz. Сравни кандидат с последними принятыми/подготовленными "
        "постами по смыслу, а не по отдельным словам.\n"
        "Отклони кандидат, если повторяется центральный тезис или вывод, та же мысль пересказана другой "
        "лексикой, близко повторён сюжетный ход, либо почти совпадает набор ключевых смыслов. Общий голос "
        "персонажа сам по себе дублем не считается.\n"
        "Верни только JSON без markdown:\n"
        '{"accepted":true|false,"reason":"коротко","central_thesis":"одна мысль",'
        '"conclusion":"эмоциональный/практический вывод","narrative_shape":"сцена→поворот→вывод",'
        '"key_meanings":["смысл 1","смысл 2"]}\n\n'
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
    return SemanticDecision(
        accepted=payload["accepted"],
        reason=str(payload.get("reason") or "")[:500],
        central_thesis=str(payload.get("central_thesis") or "")[:1000],
        conclusion=str(payload.get("conclusion") or "")[:1000],
        narrative_shape=str(payload.get("narrative_shape") or "")[:500],
        key_meanings=tuple(str(item)[:300] for item in meanings[:8] if str(item).strip()),
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
    card: SemanticCard | None = None,
) -> GenerationResult:
    """Run exactly one initial generation and at most one corrective generation."""
    first_instruction = theme_instruction(theme, platform=platform, rubric_name=rubric_name)
    if card is not None:
        first_instruction += "\n" + card_instruction(card)
    first_text = await generate(first_instruction)
    if is_model_warning(first_text):
        first_decision = blocked_decision("generation blocked before semantic review")
    else:
        first_decision = await evaluate(first_text)
    if first_decision.accepted:
        return GenerationResult(
            True, first_text, 1, first_decision, theme.key, card.key if card else ""
        )

    if correction_theme is not None:
        retry_theme = correction_theme
    elif correction_theme_selector is not None:
        retry_theme = correction_theme_selector(first_decision)
        if retry_theme is None:
            return GenerationResult(
                False, "", 1, first_decision, theme.key, card.key if card else ""
            )
    else:
        retry_theme = theme
    second_instruction = correction_instruction(
        retry_theme,
        first_decision,
        platform=platform,
        rubric_name=rubric_name,
    )
    if card is not None:
        second_instruction += (
            "\n" + card_instruction(card)
            + "\nВо второй попытке сохрани смысловую карточку, но выбери другую "
            "конкретную сцену внутри её поля и иной финальный ход."
        )
    second_text = await generate(second_instruction)
    if is_model_warning(second_text):
        second_decision = blocked_decision("corrective generation blocked before semantic review")
    else:
        second_decision = await evaluate(second_text)
    if second_decision.accepted:
        return GenerationResult(
            True, second_text, 2, second_decision, retry_theme.key, card.key if card else ""
        )
    return GenerationResult(
        False, "", 2, second_decision, retry_theme.key, card.key if card else ""
    )
