"""Platform-aware content format registry shared by the editorial planner."""

from __future__ import annotations

from hashlib import sha256
from typing import Any, Iterable


FORMAT_REGISTRY = (
    {"key": "text_story", "kind": "text", "platforms": {"telegram", "vk"}, "ready": True, "min_energy": 20, "label": "маленькая история", "brief": "сцена, поворот и личный вывод"},
    {"key": "dialogue_reflection", "kind": "text", "platforms": {"telegram", "vk"}, "ready": True, "requires_context": True, "min_energy": 20, "label": "мысль после разговора", "brief": "естественно упомянуть беседу и развить собственную позицию"},
    {"key": "field_note", "kind": "text", "platforms": {"telegram", "vk"}, "ready": True, "min_energy": 15, "label": "полевая заметка", "brief": "конкретная деталь из жизни, работы или наблюдения"},
    {"key": "micro_essay", "kind": "text", "platforms": {"telegram", "vk"}, "ready": True, "min_energy": 15, "label": "микроэссе", "brief": "одна мысль, несколько точных поворотов, плотный финал"},
    {"key": "visual_comic", "kind": "visual", "platforms": {"telegram", "vk"}, "ready": False, "min_energy": 45, "label": "комикс-сцена", "brief": "2–4 кадра с действием, репликой и визуальным панчем"},
    {"key": "carousel", "kind": "visual", "platforms": {"telegram", "vk"}, "ready": False, "min_energy": 35, "label": "карусель", "brief": "серия самостоятельных карточек с развитием мысли"},
    {"key": "found_artifact", "kind": "visual", "platforms": {"telegram", "vk"}, "ready": True, "min_energy": 20, "label": "найденный артефакт", "brief": "страница дневника, досье, записка, схема или фрагмент переписки"},
    {"key": "poll", "kind": "interactive", "platforms": {"telegram", "vk"}, "ready": False, "min_energy": 40, "label": "опрос", "brief": "реальный выбор без очевидно правильного ответа"},
    {"key": "branching_story", "kind": "interactive", "platforms": {"telegram", "vk"}, "ready": False, "min_energy": 55, "label": "ветвящаяся история", "brief": "аудитория выбирает следующий ход персонажа"},
    {"key": "voice_note", "kind": "audio", "platforms": {"telegram", "vk", "audio"}, "ready": False, "min_energy": 25, "label": "голосовая заметка", "brief": "естественный монолог с паузами, не прочитанный пост"},
    {"key": "mini_podcast", "kind": "audio", "platforms": {"telegram", "vk", "audio"}, "ready": False, "min_energy": 50, "label": "мини-подкаст", "brief": "разговор Naz и VOID с конфликтом и открытым финалом"},
    {"key": "track_commentary", "kind": "audio", "platforms": {"vk", "audio"}, "ready": False, "min_energy": 25, "label": "мысль через трек", "brief": "музыка становится частью смысла, а не украшением"},
    {"key": "short_monologue", "kind": "video", "platforms": {"short_video"}, "ready": False, "min_energy": 50, "label": "короткий монолог", "brief": "хук, три визуальных бита, живой финал"},
    {"key": "duo_scene", "kind": "video", "platforms": {"short_video"}, "ready": False, "min_energy": 65, "label": "сцена Naz/VOID", "brief": "два взгляда сталкиваются в короткой сцене"},
    {"key": "visual_essay", "kind": "video", "platforms": {"short_video"}, "ready": False, "min_energy": 35, "label": "визуальное эссе", "brief": "голос, музыка и последовательность образов"},
)


def choose_format(
    recent: Iterable[dict[str, Any]], *, platform: str, energy: int, seed_key: str,
    include_production: bool = False, has_private_thought: bool = False,
) -> dict[str, Any]:
    history = list(recent)
    candidates = [
        item for item in FORMAT_REGISTRY
        if platform in item["platforms"] and energy >= int(item["min_energy"])
        and (include_production or bool(item["ready"]))
        and (has_private_thought or not bool(item.get("requires_context")))
    ]
    if not candidates:
        candidates = [item for item in FORMAT_REGISTRY if platform in item["platforms"] and item["ready"]]
    recent_keys = [str(item.get("content_format", "")) for item in history[-8:]]
    recent_kinds = [str(item.get("content_kind", "")) for item in history[-3:]]
    seed = int(sha256(seed_key.encode("utf-8")).hexdigest()[:12], 16)

    def score(item: dict[str, Any]) -> tuple[int, int]:
        novelty = 8 if item["key"] not in recent_keys else 0
        novelty += 3 if item["kind"] not in recent_kinds else 0
        tie = (seed ^ int(sha256(str(item["key"]).encode()).hexdigest()[:12], 16)) % 1_000_000
        return novelty, tie

    return dict(max(candidates, key=score))


def production_backlog(platform: str) -> list[dict[str, Any]]:
    return [dict(item) for item in FORMAT_REGISTRY if platform in item["platforms"] and not item["ready"]]
