"""SQLite memory layer for Naz_AI_Bot v2.4.

Stores:
- old-compatible fields: expert_mode, memory_enabled
- new JSON state: expert, voice, goal, recent_topics, content_count, banned_topics, best_posts
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

import character_state as naz_character
import duo_relationship
from controller import normalize_state
from prompts import (
    DEFAULT_CONTENT_GOAL,
    DEFAULT_EXPERT_MODE,
    DEFAULT_VOICE_PROFILE,
    EXPERT_MODES,
    GOALS,
    VOICE_PROFILES,
)

DB_PATH = os.getenv("DB_PATH", "naz_ai_bot.sqlite3")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def db() -> Iterable[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [row["name"] for row in rows]


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    if column not in _table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def default_state() -> Dict:
    return normalize_state(
        {
            "mode": "hybrid",
            "expert": DEFAULT_EXPERT_MODE,
            "expert_mode": DEFAULT_EXPERT_MODE,
            "voice": DEFAULT_VOICE_PROFILE,
            "voice_profile": DEFAULT_VOICE_PROFILE,
            "goal": DEFAULT_CONTENT_GOAL,
            "content_goal": DEFAULT_CONTENT_GOAL,
            "recent_topics": [],
            "content_count": 0,
            "banned_topics": [],
            "best_posts": [],
            "rejected_topics": [],
            "last_blocked_topic": "",
            "suggested_angles": [],
            "selected_angle_index": 0,
            "angle_generation_round": 0,
            "angle_engine_version": "v2.4",
            "quality_profile": "naz_clean_v24",
            "content_rules_version": "v2.4",
            "memory_enabled": True,
        }
    )


def init_db() -> None:
    """Create and migrate all required tables. Safe on every startup."""
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_state (
                user_id INTEGER PRIMARY KEY,
                expert_mode TEXT NOT NULL DEFAULT 'copywriter',
                memory_enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        _ensure_column(conn, "user_state", "state_json", "TEXT")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                title TEXT,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS realtime_voice_summary_sessions (
                user_id INTEGER NOT NULL,
                session_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (user_id, session_id)
            )
            """
        )
        legacy_realtime_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='realtime_voice_deliveries'"
        ).fetchone()
        if legacy_realtime_table:
            conn.execute(
                """
                INSERT OR IGNORE INTO realtime_voice_summary_sessions(user_id, session_id, created_at)
                SELECT user_id, idempotency_key, created_at FROM realtime_voice_deliveries
                """
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS generated_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                expert_mode TEXT NOT NULL,
                task TEXT NOT NULL,
                topic TEXT NOT NULL,
                content TEXT NOT NULL,
                image_count INTEGER NOT NULL DEFAULT 0,
                published_to_channel INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        _ensure_column(conn, "generated_posts", "semantic_theme", "TEXT NOT NULL DEFAULT ''")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS character_states (
                user_id INTEGER PRIMARY KEY,
                character_id TEXT NOT NULL,
                state_json TEXT NOT NULL,
                core_version TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS content_signatures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                character_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                facet TEXT NOT NULL,
                intent TEXT NOT NULL,
                format TEXT NOT NULL,
                content_format TEXT NOT NULL DEFAULT 'text_story',
                content_kind TEXT NOT NULL DEFAULT 'text',
                hook TEXT NOT NULL,
                media TEXT NOT NULL,
                topic TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        _ensure_column(conn, "content_signatures", "content_format", "TEXT NOT NULL DEFAULT 'text_story'")
        _ensure_column(conn, "content_signatures", "content_kind", "TEXT NOT NULL DEFAULT 'text'")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS autopost_semantic_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                character_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                semantic_theme TEXT NOT NULL,
                central_thesis TEXT NOT NULL DEFAULT '',
                conclusion TEXT NOT NULL DEFAULT '',
                narrative_shape TEXT NOT NULL DEFAULT '',
                key_meanings_json TEXT NOT NULL DEFAULT '[]',
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                source_ref TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS autopost_semantic_rejections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                character_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                semantic_theme TEXT NOT NULL,
                source_ref TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(user_id, character_id, platform, semantic_theme, source_ref)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS relationship_states (
                relationship_id TEXT PRIMARY KEY,
                state_json TEXT NOT NULL,
                version TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS private_thoughts (
                thought_id TEXT PRIMARY KEY,
                speaker TEXT NOT NULL,
                receiver TEXT NOT NULL,
                topic TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'new',
                created_at TEXT NOT NULL,
                consumed_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS saved_contacts (
                chat_id INTEGER PRIMARY KEY,
                owner_user_id INTEGER NOT NULL,
                alias TEXT,
                display_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending_name',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(owner_user_id, alias)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS contact_naming_requests (
                prompt_message_id INTEGER PRIMARY KEY,
                owner_user_id INTEGER NOT NULL,
                contact_chat_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_contact_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_user_id INTEGER NOT NULL,
                contact_chat_id INTEGER NOT NULL,
                contact_alias TEXT NOT NULL,
                message_text TEXT NOT NULL,
                delivery_kind TEXT NOT NULL DEFAULT 'text',
                status TEXT NOT NULL DEFAULT 'pending',
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        _ensure_column(conn, "pending_contact_messages", "delivery_kind", "TEXT NOT NULL DEFAULT 'text'")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reachable_peers (
                chat_id INTEGER PRIMARY KEY,
                display_name TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS delegation_invites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT NOT NULL UNIQUE,
                owner_user_id INTEGER NOT NULL,
                character_id TEXT NOT NULL,
                contact_label TEXT NOT NULL,
                purpose TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'waiting',
                contact_chat_id INTEGER,
                contact_name TEXT,
                max_turns INTEGER NOT NULL DEFAULT 20,
                turns_used INTEGER NOT NULL DEFAULT 0,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS delegated_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                delegation_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS delegation_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_user_id INTEGER NOT NULL,
                character_id TEXT NOT NULL,
                outcome TEXT NOT NULL,
                turns_used INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

        conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_user_created ON chat_history(user_id, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_user_created ON memory_items(user_id, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_created ON generated_posts(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signatures_user_created ON content_signatures(user_id, id)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_semantic_history_user_created "
            "ON autopost_semantic_history(user_id, id)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_semantic_history_content "
            "ON autopost_semantic_history(user_id, content_hash)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_delegation_contact ON delegation_invites(contact_chat_id, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_delegated_messages ON delegated_messages(delegation_id, id)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pending_contact_owner ON pending_contact_messages(owner_user_id, status)"
        )


def create_delegation_invite(
    owner_user_id: int,
    character_id: str,
    contact_label: str,
    purpose: str,
    token: str,
    expires_at: str,
    max_turns: int = 20,
) -> int:
    """Create one pending recipient binding; never store a phone number."""
    init_db()
    with db() as conn:
        active = conn.execute(
            "SELECT id FROM delegation_invites WHERE owner_user_id=? AND status IN ('accepted','active','paused')",
            (owner_user_id,),
        ).fetchone()
        if active:
            raise ValueError(f"Сначала заверши текущее поручение #{active['id']}.")
        stale = conn.execute(
            "SELECT id FROM delegation_invites WHERE owner_user_id=?",
            (owner_user_id,),
        ).fetchall()
        for row in stale:
            conn.execute("DELETE FROM delegated_messages WHERE delegation_id=?", (row["id"],))
        conn.execute("DELETE FROM delegation_invites WHERE owner_user_id=?", (owner_user_id,))
        now = utc_now()
        cur = conn.execute(
            """
            INSERT INTO delegation_invites(
                token, owner_user_id, character_id, contact_label, purpose, status,
                max_turns, expires_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'waiting', ?, ?, ?, ?)
            """,
            (token, owner_user_id, character_id, contact_label[:200], purpose[:1200], max_turns, expires_at, now, now),
        )
        return int(cur.lastrowid)


def remember_reachable_peer(chat_id: int, display_name: str, expires_at: str) -> None:
    """Remember a user who contacted the bot first, only for short-lived matching."""
    init_db()
    with db() as conn:
        conn.execute("DELETE FROM reachable_peers WHERE expires_at<=?", (utc_now(),))
        conn.execute(
            """INSERT INTO reachable_peers(chat_id, display_name, expires_at, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET
                   display_name=excluded.display_name, expires_at=excluded.expires_at""",
            (chat_id, display_name[:200], expires_at, utc_now()),
        )


def register_contact_arrival(owner_user_id: int, chat_id: int, display_name: str) -> bool:
    """Return True only when the owner should be asked to name this new contact."""
    init_db()
    with db() as conn:
        row = conn.execute("SELECT status FROM saved_contacts WHERE chat_id=?", (chat_id,)).fetchone()
        if row:
            return False
        now = utc_now()
        conn.execute(
            """INSERT INTO saved_contacts(chat_id, owner_user_id, display_name, status, created_at, updated_at)
               VALUES (?, ?, ?, 'pending_name', ?, ?)""",
            (chat_id, owner_user_id, display_name[:200], now, now),
        )
        return True


def save_contact_naming_request(prompt_message_id: int, owner_user_id: int, contact_chat_id: int) -> None:
    with db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO contact_naming_requests(
                prompt_message_id, owner_user_id, contact_chat_id, created_at
            ) VALUES (?, ?, ?, ?)""",
            (prompt_message_id, owner_user_id, contact_chat_id, utc_now()),
        )


def name_contact_from_reply(prompt_message_id: int, owner_user_id: int, alias: str) -> Optional[Dict[str, Any]]:
    clean_alias = " ".join((alias or "").split()).strip()[:80]
    if not clean_alias:
        return None
    with db() as conn:
        request = conn.execute(
            "SELECT * FROM contact_naming_requests WHERE prompt_message_id=? AND owner_user_id=?",
            (prompt_message_id, owner_user_id),
        ).fetchone()
        if not request:
            return None
        duplicate = conn.execute(
            "SELECT chat_id FROM saved_contacts WHERE owner_user_id=? AND lower(alias)=lower(?) AND chat_id<>?",
            (owner_user_id, clean_alias, request["contact_chat_id"]),
        ).fetchone()
        if duplicate:
            raise ValueError("Такое имя уже занято другим контактом.")
        conn.execute(
            """UPDATE saved_contacts SET alias=?, status='saved', updated_at=? WHERE chat_id=?""",
            (clean_alias, utc_now(), request["contact_chat_id"]),
        )
        conn.execute("DELETE FROM contact_naming_requests WHERE prompt_message_id=?", (prompt_message_id,))
        row = conn.execute("SELECT * FROM saved_contacts WHERE chat_id=?", (request["contact_chat_id"],)).fetchone()
    return dict(row) if row else None


def list_saved_contacts(owner_user_id: int) -> List[Dict[str, Any]]:
    init_db()
    with db() as conn:
        rows = conn.execute(
            "SELECT chat_id, alias, display_name FROM saved_contacts WHERE owner_user_id=? AND status='saved' ORDER BY alias",
            (owner_user_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_saved_contact(owner_user_id: int, chat_id: int) -> Optional[Dict[str, Any]]:
    init_db()
    with db() as conn:
        row = conn.execute(
            """SELECT chat_id, alias, display_name FROM saved_contacts
               WHERE owner_user_id=? AND chat_id=? AND status='saved'""",
            (int(owner_user_id), int(chat_id)),
        ).fetchone()
    return dict(row) if row else None


def create_pending_contact_message(
    owner_user_id: int,
    contact_chat_id: int,
    contact_alias: str,
    message_text: str,
    delivery_kind: str = "text",
    ttl_minutes: int = 15,
) -> Dict[str, Any]:
    """Store a short-lived outbound draft that still requires owner confirmation."""
    init_db()
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(minutes=max(1, min(60, int(ttl_minutes))))).isoformat(timespec="seconds")
    timestamp = now.isoformat(timespec="seconds")
    if delivery_kind not in {"text", "voice"}:
        raise ValueError("Недопустимый формат сообщения контакту.")
    with db() as conn:
        conn.execute(
            "DELETE FROM pending_contact_messages WHERE status<>'pending' OR expires_at<=?",
            (timestamp,),
        )
        cur = conn.execute(
            """
            INSERT INTO pending_contact_messages(
                owner_user_id, contact_chat_id, contact_alias, message_text,
                delivery_kind, status, expires_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (
                int(owner_user_id),
                int(contact_chat_id),
                " ".join((contact_alias or "Контакт").split())[:80],
                message_text[:3500],
                delivery_kind,
                expires_at,
                timestamp,
                timestamp,
            ),
        )
        row = conn.execute("SELECT * FROM pending_contact_messages WHERE id=?", (cur.lastrowid,)).fetchone()
    return dict(row)


def get_pending_contact_message(message_id: int, owner_user_id: int) -> Optional[Dict[str, Any]]:
    init_db()
    now = utc_now()
    with db() as conn:
        conn.execute(
            "UPDATE pending_contact_messages SET status='expired', updated_at=? WHERE status='pending' AND expires_at<=?",
            (now, now),
        )
        row = conn.execute(
            """SELECT * FROM pending_contact_messages
               WHERE id=? AND owner_user_id=? AND status='pending' AND expires_at>?""",
            (int(message_id), int(owner_user_id), now),
        ).fetchone()
    return dict(row) if row else None


def delete_pending_contact_message(message_id: int, owner_user_id: int) -> bool:
    """Erase a confirmed or cancelled outbound draft, including its message text."""
    with db() as conn:
        cur = conn.execute(
            "DELETE FROM pending_contact_messages WHERE id=? AND owner_user_id=?",
            (int(message_id), int(owner_user_id)),
        )
    return cur.rowcount == 1


def save_named_contact(owner_user_id: int, chat_id: int, display_name: str, alias: str) -> Dict[str, Any]:
    clean_alias = " ".join((alias or "").split()).strip()[:80]
    if not clean_alias:
        raise ValueError("Имя контакта пустое.")
    init_db()
    with db() as conn:
        duplicate = conn.execute(
            "SELECT chat_id FROM saved_contacts WHERE owner_user_id=? AND lower(alias)=lower(?) AND chat_id<>?",
            (owner_user_id, clean_alias, chat_id),
        ).fetchone()
        if duplicate:
            raise ValueError("Такое имя уже занято другим контактом.")
        now = utc_now()
        conn.execute(
            """INSERT INTO saved_contacts(chat_id, owner_user_id, alias, display_name, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'saved', ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET owner_user_id=excluded.owner_user_id,
                   alias=excluded.alias, display_name=excluded.display_name, status='saved', updated_at=excluded.updated_at""",
            (chat_id, owner_user_id, clean_alias, display_name[:200], now, now),
        )
        row = conn.execute("SELECT * FROM saved_contacts WHERE chat_id=?", (chat_id,)).fetchone()
    return dict(row)


def list_previous_contact_ids(owner_user_id: int, limit: int = 30) -> List[int]:
    init_db()
    with db() as conn:
        rows = conn.execute(
            """SELECT DISTINCT user_id FROM chat_history
               WHERE user_id<>? AND user_id NOT IN (
                   SELECT chat_id FROM saved_contacts WHERE owner_user_id=? AND status='saved'
               ) ORDER BY user_id DESC LIMIT ?""",
            (owner_user_id, owner_user_id, max(1, limit)),
        ).fetchall()
    return [int(row["user_id"]) for row in rows]


def get_reachable_peer(chat_id: int) -> Optional[Dict[str, Any]]:
    init_db()
    with db() as conn:
        conn.execute("DELETE FROM reachable_peers WHERE expires_at<=?", (utc_now(),))
        row = conn.execute("SELECT * FROM reachable_peers WHERE chat_id=?", (chat_id,)).fetchone()
    return dict(row) if row else None


def forget_reachable_peer(chat_id: int) -> None:
    with db() as conn:
        conn.execute("DELETE FROM reachable_peers WHERE chat_id=?", (chat_id,))


def get_delegation(delegation_id: int) -> Optional[Dict[str, Any]]:
    init_db()
    with db() as conn:
        row = conn.execute("SELECT * FROM delegation_invites WHERE id=?", (delegation_id,)).fetchone()
    return dict(row) if row else None


def accept_delegation_invite(token: str, contact_chat_id: int, contact_name: str) -> Optional[Dict[str, Any]]:
    init_db()
    now = utc_now()
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM delegation_invites WHERE token=? AND status='waiting' AND expires_at>?",
            (token, now),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            """UPDATE delegation_invites
               SET status='accepted', contact_chat_id=?, contact_name=?, updated_at=?
               WHERE id=?""",
            (contact_chat_id, contact_name[:200], now, row["id"]),
        )
        result = dict(row)
        result.update(status="accepted", contact_chat_id=contact_chat_id, contact_name=contact_name[:200], updated_at=now)
        return result


def set_delegation_status(delegation_id: int, status: str) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE delegation_invites SET status=?, updated_at=? WHERE id=?",
            (status, utc_now(), delegation_id),
        )


def get_active_delegation(contact_chat_id: int) -> Optional[Dict[str, Any]]:
    init_db()
    with db() as conn:
        row = conn.execute(
            """SELECT * FROM delegation_invites
               WHERE contact_chat_id=? AND status='active' AND expires_at>?
               ORDER BY id DESC LIMIT 1""",
            (contact_chat_id, utc_now()),
        ).fetchone()
    return dict(row) if row else None


def save_delegated_message(delegation_id: int, role: str, content: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO delegated_messages(delegation_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (delegation_id, role, content[:8000], utc_now()),
        )


def get_delegated_history(delegation_id: int, limit: int = 10) -> List[Dict[str, str]]:
    with db() as conn:
        rows = conn.execute(
            """SELECT role, content FROM delegated_messages
               WHERE delegation_id=? ORDER BY id DESC LIMIT ?""",
            (delegation_id, max(1, limit)),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def increment_delegation_turns(delegation_id: int) -> int:
    with db() as conn:
        conn.execute(
            "UPDATE delegation_invites SET turns_used=turns_used+1, updated_at=? WHERE id=?",
            (utc_now(), delegation_id),
        )
        row = conn.execute("SELECT turns_used FROM delegation_invites WHERE id=?", (delegation_id,)).fetchone()
    return int(row["turns_used"]) if row else 0


def purge_delegation(delegation_id: int, outcome: str) -> None:
    """Erase recipient identity and transcript; retain only anonymous operational audit."""
    with db() as conn:
        row = conn.execute("SELECT * FROM delegation_invites WHERE id=?", (delegation_id,)).fetchone()
        if not row:
            return
        conn.execute(
            """INSERT INTO delegation_audit(owner_user_id, character_id, outcome, turns_used, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (row["owner_user_id"], row["character_id"], outcome[:80], row["turns_used"], utc_now()),
        )
        conn.execute("DELETE FROM delegated_messages WHERE delegation_id=?", (delegation_id,))
        if row["contact_chat_id"] is not None:
            conn.execute("DELETE FROM reachable_peers WHERE chat_id=?", (row["contact_chat_id"],))
        conn.execute("DELETE FROM delegation_invites WHERE id=?", (delegation_id,))


def load_character_state(user_id: int) -> naz_character.CharacterState:
    init_db()
    with db() as conn:
        row = conn.execute(
            "SELECT state_json FROM character_states WHERE user_id = ? AND character_id = ?",
            (user_id, naz_character.CHARACTER_ID),
        ).fetchone()
    if not row:
        state = naz_character.CharacterState()
        save_character_state(user_id, state)
        return state
    try:
        raw = json.loads(row["state_json"] or "{}")
    except json.JSONDecodeError:
        raw = {}
    return naz_character.normalize_state(raw if isinstance(raw, dict) else {})


def save_character_state(user_id: int, state: naz_character.CharacterState) -> None:
    normalized = naz_character.normalize_state(state.to_dict())
    with db() as conn:
        conn.execute(
            """
            INSERT INTO character_states(user_id, character_id, state_json, core_version, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                character_id = excluded.character_id,
                state_json = excluded.state_json,
                core_version = excluded.core_version,
                updated_at = excluded.updated_at
            """,
            (
                user_id,
                naz_character.CHARACTER_ID,
                json.dumps(normalized.to_dict(), ensure_ascii=False),
                normalized.core_version,
                utc_now(),
            ),
        )


def apply_character_event(user_id: int, event: str) -> naz_character.CharacterState:
    state = naz_character.apply_event(load_character_state(user_id), event)
    save_character_state(user_id, state)
    return state


def set_character_axis(user_id: int, axis: str, value: int) -> naz_character.CharacterState:
    state = naz_character.set_axis(load_character_state(user_id), axis, value)
    save_character_state(user_id, state)
    return state


def get_recent_content_signatures(user_id: int, limit: int = 12) -> List[Dict[str, str]]:
    init_db()
    with db() as conn:
        rows = conn.execute(
            """
            SELECT platform, facet, intent, format, content_format, content_kind, hook, media, topic, created_at
            FROM content_signatures
            WHERE user_id = ? AND character_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (user_id, naz_character.CHARACTER_ID, max(1, limit)),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def record_content_signature(user_id: int, plan: Dict[str, str], topic: str) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO content_signatures(
                user_id, character_id, platform, facet, intent, format, content_format, content_kind,
                hook, media, topic, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                naz_character.CHARACTER_ID,
                str(plan.get("platform", "telegram")),
                str(plan.get("facet", "explorer")),
                str(plan.get("intent", "исследовать")),
                str(plan.get("format", "маленькая история")),
                str(plan.get("content_format", "text_story")),
                str(plan.get("content_kind", "text")),
                str(plan.get("hook", "наблюдение")),
                str(plan.get("media", "редакционная иллюстрация")),
                topic[:1000],
                utc_now(),
            ),
        )
        conn.execute(
            """
            DELETE FROM content_signatures
            WHERE user_id = ? AND id NOT IN (
                SELECT id FROM content_signatures
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT 80
            )
            """,
            (user_id, user_id),
        )


def get_recent_semantic_theme_keys(user_id: int, limit: int = 5) -> List[str]:
    """Return accepted Naz themes across every publishing platform."""
    init_db()
    with db() as conn:
        rows = conn.execute(
            """
            SELECT semantic_theme
            FROM autopost_semantic_history
            WHERE user_id = ? AND character_id = ? AND semantic_theme <> ''
            ORDER BY id DESC
            LIMIT ?
            """,
            (user_id, naz_character.CHARACTER_ID, max(1, limit)),
        ).fetchall()
    return [str(row["semantic_theme"]) for row in reversed(rows)]


def get_recent_rejected_semantic_theme_keys(user_id: int, limit: int = 10) -> List[str]:
    """Return recently blocked axes separately from accepted-theme history."""
    init_db()
    with db() as conn:
        rows = conn.execute(
            """
            SELECT semantic_theme
            FROM autopost_semantic_rejections
            WHERE user_id = ? AND character_id = ? AND semantic_theme <> ''
            ORDER BY id DESC
            LIMIT ?
            """,
            (user_id, naz_character.CHARACTER_ID, max(1, limit)),
        ).fetchall()
    return [str(row["semantic_theme"]) for row in reversed(rows)]


def record_rejected_semantic_theme(
    *,
    user_id: int,
    platform: str,
    semantic_theme: str,
    source_ref: str,
) -> None:
    """Persist only rejection metadata; never treat it as an accepted post."""
    clean_theme = str(semantic_theme or "").strip()
    if not clean_theme:
        return
    init_db()
    with db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO autopost_semantic_rejections(
                user_id, character_id, platform, semantic_theme, source_ref, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                naz_character.CHARACTER_ID,
                str(platform)[:40],
                clean_theme[:120],
                str(source_ref or "")[:1000],
                utc_now(),
            ),
        )


def get_recent_posts_for_semantic_gate(user_id: int, limit: int = 8) -> List[Dict[str, str]]:
    """Combine semantic records with legacy drafts/posts without losing old data."""
    init_db()
    fetch_limit = max(8, limit * 3)
    with db() as conn:
        semantic_rows = conn.execute(
            """
            SELECT semantic_theme, content, platform, created_at, id
            FROM autopost_semantic_history
            WHERE user_id = ? AND character_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (user_id, naz_character.CHARACTER_ID, fetch_limit),
        ).fetchall()
        generated_rows = conn.execute(
            """
            SELECT semantic_theme, content, task, created_at, id
            FROM generated_posts
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (user_id, fetch_limit),
        ).fetchall()

    combined: List[Dict[str, str]] = []
    seen_hashes: set[str] = set()
    for row in semantic_rows:
        content = str(row["content"] or "")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        seen_hashes.add(digest)
        combined.append(
            {
                "semantic_theme": str(row["semantic_theme"] or ""),
                "content": content,
                "platform": str(row["platform"] or ""),
                "created_at": str(row["created_at"] or ""),
                "_order": f"1:{int(row['id']):020d}",
            }
        )
    for row in generated_rows:
        content = str(row["content"] or "")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if digest in seen_hashes:
            continue
        combined.append(
            {
                "semantic_theme": str(row["semantic_theme"] or ""),
                "content": content,
                "platform": "vk" if "vk" in str(row["task"] or "").casefold() else "telegram",
                "created_at": str(row["created_at"] or ""),
                "_order": f"0:{int(row['id']):020d}",
            }
        )
    combined.sort(key=lambda item: (item["created_at"], item["_order"]))
    return [
        {key: value for key, value in item.items() if key != "_order"}
        for item in combined[-max(1, limit):]
    ]


def record_accepted_semantic_post(
    *,
    user_id: int,
    platform: str,
    semantic_theme: str,
    central_thesis: str,
    conclusion: str,
    narrative_shape: str,
    key_meanings: Iterable[str],
    content: str,
    source_ref: str,
) -> None:
    """Persist one accepted theme only after its draft/publication was committed."""
    clean_content = str(content or "").strip()
    clean_theme = str(semantic_theme or "").strip()
    if not clean_content or not clean_theme:
        raise ValueError("accepted semantic post requires content and semantic_theme")
    digest = hashlib.sha256(clean_content.encode("utf-8")).hexdigest()
    with db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO autopost_semantic_history(
                user_id, character_id, platform, semantic_theme, central_thesis,
                conclusion, narrative_shape, key_meanings_json, content,
                content_hash, source_ref, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                naz_character.CHARACTER_ID,
                str(platform)[:40],
                clean_theme[:120],
                str(central_thesis or "")[:1000],
                str(conclusion or "")[:1000],
                str(narrative_shape or "")[:500],
                json.dumps([str(item)[:300] for item in key_meanings][:8], ensure_ascii=False),
                clean_content[:12000],
                digest,
                str(source_ref or "")[:1000],
                utc_now(),
            ),
        )


def load_relationship_state() -> duo_relationship.RelationshipState:
    init_db()
    with db() as conn:
        row = conn.execute(
            "SELECT state_json FROM relationship_states WHERE relationship_id='naz-void'"
        ).fetchone()
    if not row:
        state = duo_relationship.RelationshipState()
        save_relationship_state(state)
        return state
    try:
        raw = json.loads(row["state_json"] or "{}")
    except json.JSONDecodeError:
        raw = {}
    return duo_relationship.normalize_state(raw if isinstance(raw, dict) else {})


def save_relationship_state(state: duo_relationship.RelationshipState) -> None:
    normalized = duo_relationship.normalize_state(state.to_dict())
    with db() as conn:
        conn.execute(
            """
            INSERT INTO relationship_states(relationship_id, state_json, version, updated_at)
            VALUES ('naz-void', ?, ?, ?)
            ON CONFLICT(relationship_id) DO UPDATE SET
                state_json=excluded.state_json, version=excluded.version, updated_at=excluded.updated_at
            """,
            (json.dumps(normalized.to_dict(), ensure_ascii=False), normalized.version, utc_now()),
        )


def apply_relationship_event(event: str, *, topic: str = "", note: str = "") -> duo_relationship.RelationshipState:
    state = duo_relationship.apply_event(load_relationship_state(), event, topic=topic, note=note)
    save_relationship_state(state)
    return state


def save_private_thought(payload: Dict[str, Any], status: str = "new") -> None:
    ok, reason = duo_relationship.validate_private_thought_payload(payload)
    if not ok:
        raise ValueError(reason)
    with db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO private_thoughts(
                thought_id, speaker, receiver, topic, payload_json, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["thought_id"], payload["speaker"], payload["receiver"],
                str(payload.get("topic", ""))[:1000], json.dumps(payload, ensure_ascii=False), status, utc_now(),
            ),
        )


def _decode_state_json(raw: Optional[str]) -> Dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def load_state(user_id: int) -> Dict:
    """Load JSON state, creating defaults when missing."""
    init_db()
    now = utc_now()
    with db() as conn:
        row = conn.execute("SELECT * FROM user_state WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            state = default_state()
            conn.execute(
                """
                INSERT INTO user_state(user_id, expert_mode, memory_enabled, state_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, state["expert_mode"], int(state["memory_enabled"]), json.dumps(state, ensure_ascii=False), now, now),
            )
            return state

        state_json = _decode_state_json(row["state_json"] if "state_json" in row.keys() else None)
        merged = default_state()
        merged.update(state_json)
        merged["expert_mode"] = row["expert_mode"] or merged.get("expert_mode") or DEFAULT_EXPERT_MODE
        merged["expert"] = merged.get("expert") or merged["expert_mode"]
        merged["memory_enabled"] = bool(row["memory_enabled"])
        return normalize_state(merged)


def save_state(user_id: int, state: Dict) -> None:
    state = normalize_state(state)
    now = utc_now()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO user_state(user_id, expert_mode, memory_enabled, state_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                expert_mode = excluded.expert_mode,
                memory_enabled = excluded.memory_enabled,
                state_json = excluded.state_json,
                updated_at = excluded.updated_at
            """,
            (
                user_id,
                state["expert_mode"],
                int(state["memory_enabled"]),
                json.dumps(state, ensure_ascii=False),
                now,
                now,
            ),
        )


def set_expert_mode(user_id: int, expert_mode: str) -> Dict:
    state = load_state(user_id)
    if expert_mode not in EXPERT_MODES:
        expert_mode = DEFAULT_EXPERT_MODE
    state["expert"] = expert_mode
    state["expert_mode"] = expert_mode
    save_state(user_id, state)
    return state


def set_voice_profile(user_id: int, voice_profile: str) -> Dict:
    state = load_state(user_id)
    if voice_profile not in VOICE_PROFILES:
        voice_profile = DEFAULT_VOICE_PROFILE
    state["voice"] = voice_profile
    state["voice_profile"] = voice_profile
    save_state(user_id, state)
    return state


def set_content_goal(user_id: int, goal: str) -> Dict:
    state = load_state(user_id)
    if goal not in GOALS:
        goal = DEFAULT_CONTENT_GOAL
    state["goal"] = goal
    state["content_goal"] = goal
    save_state(user_id, state)
    return state


def set_memory_enabled(user_id: int, enabled: bool) -> Dict:
    state = load_state(user_id)
    state["memory_enabled"] = enabled
    save_state(user_id, state)
    return state


def save_message(user_id: int, role: str, content: str) -> None:
    if role not in {"user", "assistant"} or not content:
        return
    state = load_state(user_id)
    if not state.get("memory_enabled", True):
        return
    with db() as conn:
        conn.execute(
            "INSERT INTO chat_history(user_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (user_id, role, content[:8000], utc_now()),
        )
        conn.execute(
            """
            DELETE FROM chat_history
            WHERE user_id = ?
              AND id NOT IN (
                SELECT id FROM chat_history
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT 40
              )
            """,
            (user_id, user_id),
        )


def save_dialog_turn(user_id: int, user_content: str, assistant_content: str) -> None:
    """Persist one complete dialog turn in a single SQLite transaction."""
    if not user_content or not assistant_content:
        return
    state = load_state(user_id)
    if not state.get("memory_enabled", True):
        return
    now = utc_now()
    with db() as conn:
        conn.executemany(
            "INSERT INTO chat_history(user_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (
                (user_id, "user", user_content[:8000], now),
                (user_id, "assistant", assistant_content[:8000], now),
            ),
        )
        conn.execute(
            """
            DELETE FROM chat_history
            WHERE user_id = ?
              AND id NOT IN (
                SELECT id FROM chat_history
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT 40
              )
            """,
            (user_id, user_id),
        )


def get_history(user_id: int, limit: int = 10) -> List[Dict[str, str]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT role, content FROM chat_history WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]


def clear_dialog_history(user_id: int) -> None:
    """Clear conversation transcript without touching state or content memory."""
    with db() as conn:
        conn.execute("DELETE FROM chat_history WHERE user_id = ?", (user_id,))


def add_memory_item(user_id: int, kind: str, content: str, title: Optional[str] = None) -> None:
    if not content:
        return
    with db() as conn:
        conn.execute(
            "INSERT INTO memory_items(user_id, kind, title, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, kind, title, content[:8000], utc_now()),
        )


def save_realtime_voice_summary_once(
    user_id: int,
    session_id: str,
    content: str,
    *,
    memory_enabled: bool,
) -> bool:
    """Atomically record one persistent Hub session and optionally its summary."""
    now = utc_now()
    with db() as conn:
        inserted = conn.execute(
            """
            INSERT OR IGNORE INTO realtime_voice_summary_sessions(user_id, session_id, created_at)
            VALUES (?, ?, ?)
            """,
            (user_id, session_id, now),
        )
        if inserted.rowcount != 1:
            return False
        if not memory_enabled:
            return False
        conn.execute(
            """
            INSERT INTO memory_items(user_id, kind, title, content, created_at)
            VALUES (?, 'realtime_voice_summary', 'Realtime Voice Hub', ?, ?)
            """,
            (user_id, content[:8000], now),
        )
    return True


def get_memory_context(user_id: int, limit: int = 8) -> str:
    state = load_state(user_id)
    if not state.get("memory_enabled", True):
        return "Память отключена для этого пользователя."

    with db() as conn:
        rows = conn.execute(
            "SELECT kind, title, content, created_at FROM memory_items WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()

    lines = [
        f"expert: {state['expert_mode']}",
        f"voice: {state['voice_profile']}",
        f"goal: {state['content_goal']}",
        f"recent_topics: {state.get('recent_topics', [])[-10:]}",
        f"content_count: {state.get('content_count', 0)}",
        f"quality_profile: {state.get('quality_profile', 'naz_clean_v24')}",
        f"content_rules_version: {state.get('content_rules_version', 'v2.4')}",
        f"angle_engine: {state.get('angle_engine_version', 'v2.4')}",
        f"last_blocked_topic: {state.get('last_blocked_topic', '')}",
        f"suggested_angles: {[a.get('title') for a in state.get('suggested_angles', []) if isinstance(a, dict)]}",
    ]

    if rows:
        lines.append("memory_items:")
        for row in rows:
            title = f" — {row['title']}" if row["title"] else ""
            lines.append(f"[{row['kind']}{title}] {row['content'][:600]}")
    else:
        lines.append("memory_items: пока нет сохранённых заметок.")

    return "\n".join(lines)


def save_generated_post(
    *,
    user_id: int,
    expert_mode: str,
    task: str,
    topic: str,
    content: str,
    image_count: int = 0,
    published_to_channel: bool = False,
    semantic_theme: str = "",
) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO generated_posts(
                user_id, expert_mode, task, topic, content, image_count,
                published_to_channel, semantic_theme, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                expert_mode,
                task,
                topic[:1000],
                content[:12000],
                image_count,
                int(published_to_channel),
                str(semantic_theme or "")[:120],
                utc_now(),
            ),
        )


def get_recent_generated_posts(user_id: int, task: str = "", limit: int = 3) -> List[Dict[str, str]]:
    query = "SELECT task, topic, content, created_at FROM generated_posts WHERE user_id = ?"
    params: List[Any] = [user_id]
    if task:
        query += " AND task = ?"
        params.append(task)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    with db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def format_memory(user_id: int, limit: int = 10) -> str:
    state = load_state(user_id)
    with db() as conn:
        memories = conn.execute(
            "SELECT kind, title, content, created_at FROM memory_items WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        posts = conn.execute(
            "SELECT task, topic, created_at FROM generated_posts WHERE user_id = ? ORDER BY id DESC LIMIT 5",
            (user_id,),
        ).fetchall()

    lines = [
        "🧠 Память Naz",
        "",
        f"Expert: {state['expert_mode']}",
        f"Voice: {state['voice_profile']}",
        f"Goal: {state['content_goal']}",
        f"Mode: {state.get('mode', 'hybrid')}",
        f"Content count: {state.get('content_count', 0)}",
        f"Quality: {state.get('quality_profile', 'naz_clean_v24')} / {state.get('content_rules_version', 'v2.4')}",
        f"Angle Engine: {state.get('angle_engine_version', 'v2.4')}",
        f"Память: {'включена' if state['memory_enabled'] else 'выключена'}",
        "",
    ]

    recent = state.get("recent_topics") or []
    if recent:
        lines.append("Последние темы:")
        for topic in recent[-10:]:
            lines.append(f"• {str(topic)[:180]}")
        lines.append("")

    angles = state.get("suggested_angles") or []
    if angles:
        selected_index = int(state.get("selected_angle_index", 0) or 0)
        lines.append("Углы последнего повтора:")
        for idx, angle in enumerate(angles[:5], start=1):
            if isinstance(angle, dict):
                mark = "→" if idx - 1 == selected_index else "•"
                lines.append(f"{mark} {idx}. {angle.get('emoji', '')} {angle.get('title', 'Угол')}: {angle.get('hook', '')[:140]}")
        lines.append("")

    if memories:
        lines.append("Последние заметки:")
        for row in memories:
            title = f" — {row['title']}" if row["title"] else ""
            lines.append(f"• {row['kind']}{title}: {row['content'][:250]}")
    else:
        lines.append("Заметок памяти пока нет.")

    if posts:
        lines.extend(["", "Последние генерации:"])
        for row in posts:
            lines.append(f"• {row['task']}: {row['topic'][:120]}")

    return "\n".join(lines)


def clear_user_memory(user_id: int) -> None:
    state = load_state(user_id)
    state["recent_topics"] = []
    state["best_posts"] = []
    state["rejected_topics"] = []
    state["last_blocked_topic"] = ""
    state["suggested_angles"] = []
    state["selected_angle_index"] = 0
    state["angle_generation_round"] = 0
    state["content_count"] = 0
    save_state(user_id, state)
    with db() as conn:
        conn.execute("DELETE FROM chat_history WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM memory_items WHERE user_id = ?", (user_id,))


def clear_recent_topics(user_id: int) -> Dict:
    """Clear only anti-duplicate content memory, without deleting chat/memory notes."""
    state = load_state(user_id)
    state["recent_topics"] = []
    state["rejected_topics"] = []
    state["last_blocked_topic"] = ""
    state["suggested_angles"] = []
    state["selected_angle_index"] = 0
    state["angle_generation_round"] = 0
    save_state(user_id, state)
    return state


def get_stats() -> str:
    with db() as conn:
        users = conn.execute("SELECT COUNT(*) AS c FROM user_state").fetchone()["c"]
        messages = conn.execute("SELECT COUNT(*) AS c FROM chat_history").fetchone()["c"]
        memories = conn.execute("SELECT COUNT(*) AS c FROM memory_items").fetchone()["c"]
        posts = conn.execute("SELECT COUNT(*) AS c FROM generated_posts").fetchone()["c"]
        published = conn.execute("SELECT COUNT(*) AS c FROM generated_posts WHERE published_to_channel = 1").fetchone()["c"]

    return (
        "📈 Статистика Naz\n\n"
        f"Пользователей: {users}\n"
        f"Сообщений в истории: {messages}\n"
        f"Заметок памяти: {memories}\n"
        f"Сгенерировано материалов: {posts}\n"
        f"Опубликовано в канал: {published}"
    )
