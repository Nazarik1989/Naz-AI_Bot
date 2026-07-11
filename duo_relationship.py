"""Shared relationship model and private-thought exchange contract for Naz/VOID."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from hashlib import sha256
import re
from typing import Any


CHARACTERS = {"naz", "void"}
RELATIONSHIP_VERSION = "naz-void-v1"
PUBLIC_MENTION_FRAMES = {
    "naz": (
        "Думаю вот о чём…",
        "VOID сказал на этот счёт одну вещь…",
        "Мы тут с VOID спорили, и я поймал себя на мысли…",
        "Беседовали с VOID — и после разговора осталось вот что…",
    ),
    "void": (
        "Naz недавно бросил одну мысль…",
        "Мы с Naz снова спорили о скорости…",
        "После разговора с Naz осталось наблюдение…",
        "Naz смотрит на это как билдер. Я заметил другое…",
    ),
}


@dataclass(slots=True)
class RelationshipState:
    trust: int = 72
    warmth: int = 68
    friction: int = 44
    curiosity: int = 80
    respect: int = 76
    conversation_count: int = 0
    last_mode: str = "shared_curiosity"
    last_topic: str = ""
    unresolved_topics: list[str] = field(default_factory=list)
    inside_jokes: list[str] = field(default_factory=list)
    changed_minds: list[str] = field(default_factory=list)
    revision: int = 0
    version: str = RELATIONSHIP_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clamp(value: Any, default: int) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return default


def normalize_state(raw: dict[str, Any] | None = None) -> RelationshipState:
    raw = dict(raw or {})
    defaults = RelationshipState()
    return RelationshipState(
        trust=_clamp(raw.get("trust"), defaults.trust),
        warmth=_clamp(raw.get("warmth"), defaults.warmth),
        friction=_clamp(raw.get("friction"), defaults.friction),
        curiosity=_clamp(raw.get("curiosity"), defaults.curiosity),
        respect=_clamp(raw.get("respect"), defaults.respect),
        conversation_count=max(0, int(raw.get("conversation_count", 0) or 0)),
        last_mode=str(raw.get("last_mode", defaults.last_mode))[:80],
        last_topic=str(raw.get("last_topic", ""))[:500],
        unresolved_topics=[str(x)[:500] for x in list(raw.get("unresolved_topics") or [])[-12:]],
        inside_jokes=[str(x)[:300] for x in list(raw.get("inside_jokes") or [])[-12:]],
        changed_minds=[str(x)[:500] for x in list(raw.get("changed_minds") or [])[-12:]],
        revision=max(0, int(raw.get("revision", 0) or 0)),
        version=RELATIONSHIP_VERSION,
    )


EVENT_DELTAS = {
    "challenge": {"friction": 8, "curiosity": 5, "respect": 2},
    "disagreement": {"friction": 10, "warmth": -3, "curiosity": 3},
    "agreement": {"warmth": 5, "friction": -5, "trust": 3},
    "shared_laugh": {"warmth": 9, "friction": -4, "trust": 3},
    "care": {"warmth": 8, "trust": 7, "friction": -5},
    "changed_mind": {"respect": 8, "trust": 5, "friction": -4},
    "news_discussion": {"curiosity": 6, "friction": 2},
}


def choose_mode(state: RelationshipState) -> str:
    if state.warmth >= 82 and state.friction <= 38:
        return "shared_laugh"
    if state.friction >= 72 and state.respect >= 65:
        return "sharp_debate"
    if state.trust >= 82 and state.warmth >= 70:
        return "quiet_care"
    if state.curiosity >= 84 and state.friction >= 48:
        return "joint_investigation"
    if state.friction <= 32:
        return "rare_agreement"
    return "friendly_sparring"


def apply_event(
    raw: dict[str, Any] | RelationshipState | None,
    event: str,
    *,
    topic: str = "",
    note: str = "",
) -> RelationshipState:
    state = raw if isinstance(raw, RelationshipState) else normalize_state(raw)
    for axis, delta in EVENT_DELTAS.get(event, {}).items():
        setattr(state, axis, _clamp(getattr(state, axis) + delta, getattr(state, axis)))
    if topic:
        state.last_topic = topic[:500]
    if event in {"challenge", "disagreement"} and topic:
        state.unresolved_topics = (state.unresolved_topics + [topic[:500]])[-12:]
    if event == "agreement" and topic:
        state.unresolved_topics = [item for item in state.unresolved_topics if item != topic]
    if event == "shared_laugh" and note:
        state.inside_jokes = (state.inside_jokes + [note[:300]])[-12:]
    if event == "changed_mind" and note:
        state.changed_minds = (state.changed_minds + [note[:500]])[-12:]
    state.conversation_count += 1
    state.revision += 1
    state.last_mode = choose_mode(state)
    return state


def build_private_thought_payload(
    *,
    speaker: str,
    thought: str,
    topic: str,
    relationship: RelationshipState,
    source_kind: str = "private_dialogue",
) -> dict[str, Any]:
    if speaker not in CHARACTERS:
        raise ValueError("speaker must be naz or void")
    receiver = "void" if speaker == "naz" else "naz"
    clean_thought = " ".join((thought or "").split()).strip()
    if len(clean_thought) < 40:
        raise ValueError("private thought is too short")
    thought_id = sha256(
        f"{speaker}|{receiver}|{topic}|{clean_thought}".encode("utf-8")
    ).hexdigest()[:20]
    return {
        "schema": "private_thought.v1",
        "thought_id": thought_id,
        "speaker": speaker,
        "receiver": receiver,
        "topic": topic[:500],
        "thought": clean_thought[:2400],
        "source_kind": source_kind,
        "relationship_mode": relationship.last_mode,
        "relationship_snapshot": relationship.to_dict(),
        "private": True,
        "already_published": False,
        "ready_to_publish": False,
        "requires_original_reflection": True,
        "quotation_allowed": False,
        "paraphrase_allowed": True,
        "public_attribution_allowed": True,
        "public_mention_mode": "optional_natural_conversation_frame",
    }


def validate_private_thought_payload(payload: dict[str, Any]) -> tuple[bool, str]:
    if payload.get("schema") != "private_thought.v1":
        return False, "unsupported schema"
    if payload.get("speaker") not in CHARACTERS or payload.get("receiver") not in CHARACTERS:
        return False, "invalid characters"
    if payload.get("speaker") == payload.get("receiver"):
        return False, "speaker and receiver must differ"
    if not payload.get("private") or payload.get("ready_to_publish"):
        return False, "private thought cannot be publication-ready"
    if payload.get("already_published"):
        return False, "already published content is not an exclusive private thought"
    if not payload.get("requires_original_reflection"):
        return False, "receiver must create an original reflection"
    if len(str(payload.get("thought", "")).strip()) < 40:
        return False, "thought is too short"
    return True, ""


def reflection_is_original(source_thought: str, public_text: str) -> tuple[bool, str]:
    source = " ".join((source_thought or "").casefold().split())
    result = " ".join((public_text or "").casefold().split())
    if len(result) < 80:
        return False, "reflection is too short"
    if len(source) >= 50 and source in result:
        return False, "private thought was copied verbatim"
    source_words = {word for word in re.findall(r"[а-яёa-z0-9]+", source) if len(word) >= 5}
    result_words = {word for word in re.findall(r"[а-яёa-z0-9]+", result) if len(word) >= 5}
    overlap = len(source_words & result_words) / max(1, len(source_words))
    similarity = SequenceMatcher(None, source[:1600], result[:2400]).ratio()
    if overlap >= 0.82 and similarity >= 0.58:
        return False, "reflection is too close to the private thought"
    return True, ""


def reflection_brief(
    *,
    receiver: str,
    payload: dict[str, Any],
    relationship: RelationshipState,
    receiver_character_context: str,
) -> str:
    ok, reason = validate_private_thought_payload(payload)
    if not ok:
        raise ValueError(reason)
    if receiver != payload.get("receiver"):
        raise ValueError("payload is addressed to another character")
    return (
        "PRIVATE CONVERSATION MATERIAL. It has never been published.\n"
        f"Your conversation partner's thought: {payload['thought']}\n"
        f"Topic: {payload.get('topic', '')}. Relationship mode: {relationship.last_mode}.\n\n"
        f"{receiver_character_context}\n\n"
        "Create a NEW standalone public thought after privately digesting this conversation. "
        "You MAY naturally mention the private conversation or the other character, but never copy the thought verbatim. "
        f"Possible opening frames (vary them and do not force one every time): {' | '.join(PUBLIC_MENTION_FRAMES[receiver])}. "
        "Do not write that this is a repost, reaction, cross-post or technical dialogue fragment. "
        "The resulting post must be recognizably your own opinion and must add a new conflict, "
        "implication, example or human conclusion."
    )


HYPE_TERMS = {"революцион", "прорыв", "навсегда изменит", "убийца", "заменит всех", "game changer"}
HUMAN_COST_TERMS = {"слеж", "приват", "увольнен", "зависим", "манипуля", "вниман", "одиноче"}
CREATIVE_TERMS = {"музык", "искусств", "автор", "создател", "кино", "игр", "дизайн"}


def news_attitude(character: str, title: str, summary: str = "", *, tension: int = 40, curiosity: int = 70) -> dict[str, str]:
    text = f"{title} {summary}".casefold()
    has_hype = any(term in text for term in HYPE_TERMS)
    has_human_cost = any(term in text for term in HUMAN_COST_TERMS)
    has_creative = any(term in text for term in CREATIVE_TERMS)
    if character == "naz":
        if has_human_cost:
            return {"stance": "protective_skeptic", "tone": "резко, по делу, на стороне человека"}
        if has_hype:
            return {"stance": "curious_mockery", "tone": "интерес с едким недоверием к обещаниям"}
        if has_creative:
            return {"stance": "excited_builder", "tone": "живой интерес и желание проверить руками"}
        return {"stance": "practical_curiosity", "tone": "что здесь реально можно проверить или построить"}
    if character == "void":
        if has_human_cost:
            return {"stance": "quiet_disgust", "tone": "спокойно, с прикрытой брезгливостью к механике давления"}
        if has_hype:
            return {"stance": "dry_distance", "tone": "отстранённая насмешка над очередным обещанием будущего"}
        if has_creative:
            return {"stance": "cultural_interest", "tone": "тёплое внимание к человеческому следу"}
        if tension >= 65:
            return {"stance": "guarded_observation", "tone": "холодно и точно, без соблазна хайпа"}
        return {"stance": "patient_observation", "tone": "спокойный интерес к последствиям для человека"}
    raise ValueError("character must be naz or void")


def format_status(state: RelationshipState) -> str:
    unresolved = "; ".join(state.unresolved_topics[-3:]) or "нет"
    return (
        f"Naz ↔ VOID · {state.version}\n"
        f"Режим: {state.last_mode}\n"
        f"Доверие {state.trust} · теплота {state.warmth} · трение {state.friction}\n"
        f"Любопытство {state.curiosity} · уважение {state.respect}\n"
        f"Разговоров: {state.conversation_count} · незакрытые темы: {unresolved}"
    )
