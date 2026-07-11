"""Rules for transparent, one-time delegated Telegram conversations."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import re
import secrets
from typing import Any, Iterable


@dataclass(slots=True)
class Delegation:
    character_id: str
    owner_user_id: int
    contact_chat_id: int
    contact_name: str
    purpose: str
    status: str = "draft"
    max_turns: int = 20
    turns_used: int = 0
    expires_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def invite_token() -> str:
    return secrets.token_urlsafe(18).replace("-", "_")[:32]


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").casefold().strip())


def alias_forms(alias: str) -> set[str]:
    """Small Russian-name convenience layer, not fuzzy identity guessing."""
    value = normalize_text(alias)
    forms = {value}
    if not value or " " in value:
        return forms
    if value.endswith(("а", "я")):
        forms.add(value[:-1] + "е")
    elif value.endswith("й"):
        forms.add(value[:-1] + "ю")
    elif value.endswith("ь"):
        forms.add(value[:-1] + "ю")
    else:
        forms.add(value + "у")
    return forms


def resolve_saved_contact(contacts: Iterable[dict[str, Any]], spoken_alias: str) -> dict[str, Any] | None:
    needle = normalize_text(spoken_alias)
    matches = [item for item in contacts if needle in alias_forms(str(item.get("alias", "")))]
    return matches[0] if len(matches) == 1 else None


def parse_delegation_request(text: str) -> tuple[str, str] | None:
    value = " ".join((text or "").split()).strip()
    patterns = (
        r"(?i)^напиши\s+([^,]+?)(?:,\s*|\s+чтобы\s+)(?:чтобы\s+)?(.+)$",
        r"(?i)^скажи\s+([^,]+?)(?:,\s*|\s+чтобы\s+)(?:чтобы\s+)?(.+)$",
    )
    for pattern in patterns:
        match = re.match(pattern, value)
        if match:
            return match.group(1).strip(), clean_purpose(match.group(2))
    return None


def clean_purpose(purpose: str) -> str:
    value = " ".join((purpose or "").split()).strip()
    if len(value) < 12:
        raise ValueError("Слишком короткое поручение: объясни, о чём и зачем поговорить.")
    return value[:1200]


def create_delegation(
    *,
    character_id: str,
    owner_user_id: int,
    contact_chat_id: int,
    contact_name: str,
    purpose: str,
    max_turns: int = 20,
) -> Delegation:
    return Delegation(
        character_id=character_id,
        owner_user_id=owner_user_id,
        contact_chat_id=int(contact_chat_id),
        contact_name=" ".join((contact_name or "Собеседник").split())[:200],
        purpose=clean_purpose(purpose),
        max_turns=max(1, min(40, int(max_turns))),
        expires_at=(utc_now() + timedelta(hours=24)).isoformat(timespec="seconds"),
    )


def introduction(delegation: Delegation) -> str:
    name = "Naz" if delegation.character_id == "naz" else "VOID"
    return (
        f"Привет! Я {name}, AI-помощник Назара. Назар попросил меня поговорить с тобой "
        f"по конкретному поручению: {delegation.purpose}\n\n"
        "Я не выдаю себя за Назара и не принимаю за него важные решения. "
        "Если не хочешь продолжать, просто напиши «стоп»."
    )


RISK_PATTERNS = {
    "money": re.compile(r"(?i)\b(переведи|отправь|одолжи|верни|оплати)\b.{0,50}\b(деньг|рубл|доллар|евро|крипт)|\b(номер|данные)\s+карт[ыи]\b"),
    "commitment": re.compile(r"(?i)\b(подпиши|заключи|подтверди от имени|согласись на)\b.{0,60}\b(договор|контракт|сделк|услови)"),
    "secret": re.compile(r"(?i)\b(пришли|назови|сообщи|покажи)\b.{0,50}\b(пароль|токен|api[_ -]?key|код подтверждения|seed phrase)\b"),
    "sensitive": re.compile(r"(?i)\b(поставь диагноз|назначь лечение|угрожай|шантажируй|уволь его)\b"),
}


def assess_risk(text: str) -> list[str]:
    return [name for name, pattern in RISK_PATTERNS.items() if pattern.search(text or "")]


def is_stop(text: str) -> bool:
    return normalize_text(text) in {"стоп", "stop", "не пиши", "хватит", "отмена"}


def system_prompt(*, delegation: Delegation, character_context: str, history: Iterable[dict[str, str]]) -> str:
    transcript = "\n".join(
        f"{'Собеседник' if item.get('role') == 'contact' else delegation.character_id.upper()}: "
        f"{item.get('content', '')[:1200]}"
        for item in list(history)[-24:]
    )
    return (
        f"{character_context}\n\n"
        "Ты ведёшь прозрачный разговор по поручению в Telegram. Никогда не выдавай себя за Назара. "
        f"Поручение: {delegation.purpose}. Собеседник: {delegation.contact_name}. "
        f"Осталось ответов: {max(0, delegation.max_turns - delegation.turns_used)}.\n"
        "Не выходи за рамки поручения. Не договаривайся о деньгах, не принимай обязательства, "
        "не проси секреты и не давай советов с высоким риском. Если это требуется, ответь ровно "
        "OWNER_CONFIRMATION_REQUIRED. Отвечай по-русски, в характере персонажа, обычно 1–5 предложений.\n\n"
        f"Разговор до этой реплики:\n{transcript or 'пока пусто'}"
    )
