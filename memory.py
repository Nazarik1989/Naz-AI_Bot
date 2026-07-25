"""SQLite memory layer for Naz_AI_Bot v2.4.

Stores:
- old-compatible fields: expert_mode, memory_enabled
- new JSON state: expert, voice, goal, recent_topics, content_count, banned_topics, best_posts
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional

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
        _ensure_column(conn, "generated_posts", "semantic_card", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "generated_posts", "external_job_id", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "generated_posts", "plan_id", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "generated_posts", "editorial_plan_json", "TEXT NOT NULL DEFAULT ''")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS generated_artifacts (
                artifact_id TEXT PRIMARY KEY,
                owner_user_id INTEGER NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('text', 'image')),
                mode TEXT NOT NULL DEFAULT '',
                topic TEXT NOT NULL DEFAULT '',
                source_ref TEXT NOT NULL DEFAULT '',
                current_version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        _ensure_column(conn, "generated_artifacts", "source_ref", "TEXT NOT NULL DEFAULT ''")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS generated_artifact_versions (
                artifact_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                telegram_file_id TEXT NOT NULL DEFAULT '',
                revision_kind TEXT NOT NULL DEFAULT 'generated',
                created_at TEXT NOT NULL,
                PRIMARY KEY (artifact_id, version),
                FOREIGN KEY (artifact_id) REFERENCES generated_artifacts(artifact_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS generated_artifact_bindings (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                artifact_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                owner_user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (chat_id, message_id),
                FOREIGN KEY (artifact_id) REFERENCES generated_artifacts(artifact_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_generated_revisions (
                revision_id TEXT PRIMARY KEY,
                artifact_id TEXT NOT NULL,
                owner_user_id INTEGER NOT NULL,
                base_version INTEGER NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('text_replace', 'image_instruction')),
                proposed_content TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (artifact_id) REFERENCES generated_artifacts(artifact_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_generated_publications (
                publication_id TEXT PRIMARY KEY,
                artifact_id TEXT NOT NULL,
                owner_user_id INTEGER NOT NULL,
                version INTEGER NOT NULL,
                destination TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                reason_code TEXT NOT NULL DEFAULT '',
                receipt_chat_id TEXT NOT NULL DEFAULT '',
                receipt_message_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (artifact_id) REFERENCES generated_artifacts(artifact_id)
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS editorial_release_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                plan_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                slot TEXT NOT NULL DEFAULT '',
                slot_captured_at TEXT NOT NULL DEFAULT '',
                generation_package_status TEXT NOT NULL DEFAULT 'not_run',
                image_qa_status TEXT NOT NULL DEFAULT 'not_run',
                telegram_chat_id TEXT NOT NULL DEFAULT '',
                telegram_message_id TEXT NOT NULL DEFAULT '',
                vk_job_id TEXT NOT NULL DEFAULT '',
                vk_receipt_id TEXT NOT NULL DEFAULT '',
                history_commit_status TEXT NOT NULL DEFAULT 'not_run',
                updated_at TEXT NOT NULL,
                UNIQUE(user_id, plan_id, platform)
            )
            """
        )

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
        _ensure_column(conn, "content_signatures", "plan_id", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "content_signatures", "source_ref", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "content_signatures", "editorial_plan_json", "TEXT NOT NULL DEFAULT ''")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_signatures_published_plan "
            "ON content_signatures(user_id, character_id, plan_id) WHERE plan_id <> ''"
        )
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
        _ensure_column(conn, "autopost_semantic_history", "semantic_card", "TEXT NOT NULL DEFAULT ''")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS autopost_semantic_profiles (
                user_id INTEGER NOT NULL,
                character_id TEXT NOT NULL,
                history_digest TEXT NOT NULL,
                occupied_themes_json TEXT NOT NULL,
                exclusion_summary TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                PRIMARY KEY (user_id, character_id, history_digest)
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
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_generated_artifact_owner "
            "ON generated_artifacts(owner_user_id, updated_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pending_generated_revision_owner "
            "ON pending_generated_revisions(owner_user_id, status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pending_generated_publication_owner "
            "ON pending_generated_publications(owner_user_id, status)"
        )
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


def create_generated_artifact(
    owner_user_id: int,
    kind: str,
    content: str = "",
    *,
    mode: str = "",
    topic: str = "",
    source_ref: str = "",
    telegram_file_id: str = "",
) -> Dict[str, Any]:
    """Create the first immutable version of a user-editable generated artifact."""
    if kind not in {"text", "image"}:
        raise ValueError("Unsupported generated artifact kind")
    artifact_id = secrets.token_urlsafe(12)
    now = utc_now()
    init_db()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO generated_artifacts(
                artifact_id, owner_user_id, kind, mode, topic, source_ref,
                current_version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (artifact_id, owner_user_id, kind, mode[:80], topic[:500], source_ref[:200], now, now),
        )
        conn.execute(
            """
            INSERT INTO generated_artifact_versions(
                artifact_id, version, content, telegram_file_id, revision_kind, created_at
            ) VALUES (?, 1, ?, ?, 'generated', ?)
            """,
            (artifact_id, content, telegram_file_id, now),
        )
    return {
        "artifact_id": artifact_id,
        "owner_user_id": owner_user_id,
        "kind": kind,
        "mode": mode[:80],
        "topic": topic[:500],
        "source_ref": source_ref[:200],
        "current_version": 1,
        "content": content,
        "telegram_file_id": telegram_file_id,
    }


def bind_generated_artifact_message(
    artifact_id: str,
    version: int,
    owner_user_id: int,
    chat_id: int,
    message_id: int,
) -> None:
    init_db()
    with db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO generated_artifact_bindings(
                chat_id, message_id, artifact_id, version, owner_user_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (chat_id, message_id, artifact_id, version, owner_user_id, utc_now()),
        )


def get_generated_artifact_for_reply(
    chat_id: int,
    message_id: int,
    owner_user_id: int,
) -> Optional[Dict[str, Any]]:
    init_db()
    with db() as conn:
        row = conn.execute(
            """
            SELECT a.*, b.version AS bound_version, v.content, v.telegram_file_id
            FROM generated_artifact_bindings b
            JOIN generated_artifacts a ON a.artifact_id=b.artifact_id
            JOIN generated_artifact_versions v
              ON v.artifact_id=b.artifact_id AND v.version=a.current_version
            WHERE b.chat_id=? AND b.message_id=? AND b.owner_user_id=?
              AND a.owner_user_id=?
            """,
            (chat_id, message_id, owner_user_id, owner_user_id),
        ).fetchone()
    return dict(row) if row else None


def create_pending_generated_revision(
    artifact_id: str,
    owner_user_id: int,
    base_version: int,
    kind: str,
    proposed_content: str,
) -> Dict[str, Any]:
    if kind not in {"text_replace", "image_instruction"}:
        raise ValueError("Unsupported generated revision kind")
    proposed = proposed_content.strip()
    if not proposed:
        raise ValueError("Revision must not be empty")
    revision_id = secrets.token_urlsafe(9)
    now = utc_now()
    init_db()
    with db() as conn:
        artifact = conn.execute(
            "SELECT owner_user_id, current_version FROM generated_artifacts WHERE artifact_id=?",
            (artifact_id,),
        ).fetchone()
        if not artifact or int(artifact["owner_user_id"]) != owner_user_id:
            raise ValueError("Generated artifact is unavailable")
        if int(artifact["current_version"]) != base_version:
            raise ValueError("Generated artifact version is stale")
        conn.execute(
            """
            UPDATE pending_generated_revisions
            SET status='superseded', updated_at=?
            WHERE artifact_id=? AND owner_user_id=? AND status='pending'
            """,
            (now, artifact_id, owner_user_id),
        )
        conn.execute(
            """
            INSERT INTO pending_generated_revisions(
                revision_id, artifact_id, owner_user_id, base_version,
                kind, proposed_content, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (revision_id, artifact_id, owner_user_id, base_version, kind, proposed, now, now),
        )
    return {
        "revision_id": revision_id,
        "artifact_id": artifact_id,
        "owner_user_id": owner_user_id,
        "base_version": base_version,
        "kind": kind,
        "proposed_content": proposed,
        "status": "pending",
    }


def get_pending_generated_revision(revision_id: str, owner_user_id: int) -> Optional[Dict[str, Any]]:
    init_db()
    with db() as conn:
        row = conn.execute(
            """
            SELECT r.*, a.kind AS artifact_kind, a.mode, a.topic, a.source_ref,
                   a.current_version, v.telegram_file_id
            FROM pending_generated_revisions r
            JOIN generated_artifacts a ON a.artifact_id=r.artifact_id
            JOIN generated_artifact_versions v
              ON v.artifact_id=a.artifact_id AND v.version=a.current_version
            WHERE r.revision_id=? AND r.owner_user_id=? AND a.owner_user_id=?
            """,
            (revision_id, owner_user_id, owner_user_id),
        ).fetchone()
    return dict(row) if row else None


def cancel_pending_generated_revision(revision_id: str, owner_user_id: int) -> bool:
    init_db()
    with db() as conn:
        result = conn.execute(
            """
            UPDATE pending_generated_revisions SET status='cancelled', updated_at=?
            WHERE revision_id=? AND owner_user_id=? AND status='pending'
            """,
            (utc_now(), revision_id, owner_user_id),
        )
    return result.rowcount == 1


def begin_image_generated_revision(revision_id: str, owner_user_id: int) -> Optional[Dict[str, Any]]:
    """Atomically claim a current image revision before making the paid provider call."""
    init_db()
    with db() as conn:
        row = conn.execute(
            """
            SELECT r.*, a.current_version, v.telegram_file_id
            FROM pending_generated_revisions r
            JOIN generated_artifacts a ON a.artifact_id=r.artifact_id
            JOIN generated_artifact_versions v
              ON v.artifact_id=a.artifact_id AND v.version=a.current_version
            WHERE r.revision_id=? AND r.owner_user_id=? AND a.owner_user_id=?
              AND r.kind='image_instruction' AND r.status='pending'
              AND r.base_version=a.current_version
            """,
            (revision_id, owner_user_id, owner_user_id),
        ).fetchone()
        if not row:
            return None
        result = conn.execute(
            """
            UPDATE pending_generated_revisions SET status='processing', updated_at=?
            WHERE revision_id=? AND owner_user_id=? AND status='pending'
            """,
            (utc_now(), revision_id, owner_user_id),
        )
        if result.rowcount != 1:
            return None
    return dict(row)


def fail_image_generated_revision(revision_id: str, owner_user_id: int) -> None:
    init_db()
    with db() as conn:
        conn.execute(
            """
            UPDATE pending_generated_revisions SET status='failed', updated_at=?
            WHERE revision_id=? AND owner_user_id=? AND status='processing'
            """,
            (utc_now(), revision_id, owner_user_id),
        )


def apply_generated_revision(
    revision_id: str,
    owner_user_id: int,
    *,
    telegram_file_id: str = "",
) -> Optional[Dict[str, Any]]:
    """Apply once, only if the pending revision still targets the current version."""
    init_db()
    with db() as conn:
        row = conn.execute(
            """
            SELECT r.*, a.kind, a.current_version
            FROM pending_generated_revisions r
            JOIN generated_artifacts a ON a.artifact_id=r.artifact_id
            WHERE r.revision_id=? AND r.owner_user_id=? AND a.owner_user_id=?
              AND r.status IN ('pending', 'processing')
            """,
            (revision_id, owner_user_id, owner_user_id),
        ).fetchone()
        if not row or int(row["base_version"]) != int(row["current_version"]):
            return None
        if row["kind"] == "image_instruction" and row["status"] != "processing":
            return None
        if row["kind"] == "text_replace" and row["status"] != "pending":
            return None
        new_version = int(row["current_version"]) + 1
        now = utc_now()
        conn.execute(
            """
            INSERT INTO generated_artifact_versions(
                artifact_id, version, content, telegram_file_id, revision_kind, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                row["artifact_id"], new_version, row["proposed_content"],
                telegram_file_id, row["kind"], now,
            ),
        )
        updated = conn.execute(
            """
            UPDATE generated_artifacts SET current_version=?, updated_at=?
            WHERE artifact_id=? AND owner_user_id=? AND current_version=?
            """,
            (new_version, now, row["artifact_id"], owner_user_id, row["base_version"]),
        )
        if updated.rowcount != 1:
            raise RuntimeError("Generated artifact changed during revision")
        conn.execute(
            """
            UPDATE pending_generated_revisions SET status='applied', updated_at=?
            WHERE revision_id=?
            """,
            (now, revision_id),
        )
    return {
        "artifact_id": row["artifact_id"],
        "version": new_version,
        "kind": row["kind"],
        "content": row["proposed_content"],
        "telegram_file_id": telegram_file_id,
    }


def create_pending_generated_publication(
    artifact_id: str,
    owner_user_id: int,
    version: int,
    destination: str = "telegram_channel",
) -> Dict[str, Any]:
    publication_id = secrets.token_urlsafe(9)
    now = utc_now()
    init_db()
    with db() as conn:
        artifact = conn.execute(
            """
            SELECT owner_user_id, kind, mode, source_ref, current_version
            FROM generated_artifacts WHERE artifact_id=?
            """,
            (artifact_id,),
        ).fetchone()
        if not artifact or int(artifact["owner_user_id"]) != owner_user_id:
            raise ValueError("Generated artifact is unavailable")
        if artifact["kind"] != "text" or int(artifact["current_version"]) != int(version):
            raise ValueError("Generated artifact version is stale")
        if artifact["mode"] != "agent_content_sync" or not str(artifact["source_ref"] or ""):
            raise ValueError("Generated artifact is not publishable")
        existing = conn.execute(
            """
            SELECT status FROM pending_generated_publications
            WHERE artifact_id=? AND version=? AND destination=?
              AND status IN ('processing', 'published', 'needs_audit')
            ORDER BY created_at DESC LIMIT 1
            """,
            (artifact_id, int(version), destination),
        ).fetchone()
        if existing:
            raise ValueError(f"Generated artifact publication is {existing['status']}")
        conn.execute(
            """
            UPDATE pending_generated_publications
            SET status='superseded', updated_at=?
            WHERE artifact_id=? AND owner_user_id=? AND status='pending'
            """,
            (now, artifact_id, owner_user_id),
        )
        conn.execute(
            """
            INSERT INTO pending_generated_publications(
                publication_id, artifact_id, owner_user_id, version,
                destination, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (publication_id, artifact_id, owner_user_id, int(version), destination, now, now),
        )
    return {
        "publication_id": publication_id,
        "artifact_id": artifact_id,
        "owner_user_id": owner_user_id,
        "version": int(version),
        "destination": destination,
        "status": "pending",
    }


def get_pending_generated_publication(
    publication_id: str,
    owner_user_id: int,
) -> Optional[Dict[str, Any]]:
    init_db()
    with db() as conn:
        row = conn.execute(
            """
            SELECT p.*, a.kind, a.mode, a.topic, a.source_ref, a.current_version,
                   v.content
            FROM pending_generated_publications p
            JOIN generated_artifacts a ON a.artifact_id=p.artifact_id
            JOIN generated_artifact_versions v
              ON v.artifact_id=p.artifact_id AND v.version=p.version
            WHERE p.publication_id=? AND p.owner_user_id=? AND a.owner_user_id=?
            """,
            (publication_id, owner_user_id, owner_user_id),
        ).fetchone()
    return dict(row) if row else None


def cancel_pending_generated_publication(publication_id: str, owner_user_id: int) -> bool:
    init_db()
    with db() as conn:
        result = conn.execute(
            """
            UPDATE pending_generated_publications
            SET status='cancelled', updated_at=?
            WHERE publication_id=? AND owner_user_id=? AND status='pending'
            """,
            (utc_now(), publication_id, owner_user_id),
        )
    return result.rowcount == 1


def begin_generated_publication(
    publication_id: str,
    owner_user_id: int,
) -> Optional[Dict[str, Any]]:
    """Claim one approved version before any paid media or external send."""
    init_db()
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT p.*, a.kind, a.mode, a.topic, a.source_ref, a.current_version,
                   v.content
            FROM pending_generated_publications p
            JOIN generated_artifacts a ON a.artifact_id=p.artifact_id
            JOIN generated_artifact_versions v
              ON v.artifact_id=p.artifact_id AND v.version=p.version
            WHERE p.publication_id=? AND p.owner_user_id=? AND a.owner_user_id=?
              AND p.status='pending' AND p.version=a.current_version
            """,
            (publication_id, owner_user_id, owner_user_id),
        ).fetchone()
        if not row:
            return None
        updated = conn.execute(
            """
            UPDATE pending_generated_publications SET status='processing', updated_at=?
            WHERE publication_id=? AND owner_user_id=? AND status='pending'
            """,
            (utc_now(), publication_id, owner_user_id),
        )
        if updated.rowcount != 1:
            return None
    return dict(row)


def finish_generated_publication(
    publication_id: str,
    owner_user_id: int,
    *,
    receipt_chat_id: str,
    receipt_message_id: str,
) -> bool:
    init_db()
    with db() as conn:
        result = conn.execute(
            """
            UPDATE pending_generated_publications
            SET status='published', reason_code='', receipt_chat_id=?,
                receipt_message_id=?, updated_at=?
            WHERE publication_id=? AND owner_user_id=? AND status='processing'
            """,
            (
                str(receipt_chat_id)[:128], str(receipt_message_id)[:128], utc_now(),
                publication_id, owner_user_id,
            ),
        )
    return result.rowcount == 1


def fail_generated_publication(
    publication_id: str,
    owner_user_id: int,
    reason_code: str,
    *,
    needs_audit: bool = False,
) -> bool:
    init_db()
    with db() as conn:
        result = conn.execute(
            """
            UPDATE pending_generated_publications
            SET status=?, reason_code=?, updated_at=?
            WHERE publication_id=? AND owner_user_id=? AND status='processing'
            """,
            (
                "needs_audit" if needs_audit else "failed",
                str(reason_code)[:120], utc_now(), publication_id, owner_user_id,
            ),
        )
    return result.rowcount == 1


def get_generated_post_by_plan(user_id: int, plan_id: str) -> Optional[Dict[str, Any]]:
    init_db()
    with db() as conn:
        row = conn.execute(
            """
            SELECT id, user_id, task, topic, semantic_theme, semantic_card,
                   plan_id, editorial_plan_json, published_to_channel
            FROM generated_posts
            WHERE user_id=? AND plan_id=?
            ORDER BY id DESC LIMIT 1
            """,
            (user_id, str(plan_id)[:64]),
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    try:
        result["editorial_plan"] = json.loads(str(result.pop("editorial_plan_json") or "{}"))
    except json.JSONDecodeError:
        result["editorial_plan"] = {}
    return result


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
            SELECT platform, facet, intent, format, content_format, content_kind, hook, media,
                   topic, plan_id, source_ref, editorial_plan_json, created_at
            FROM content_signatures
            WHERE user_id = ? AND character_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (user_id, naz_character.CHARACTER_ID, max(1, limit)),
        ).fetchall()
    result: List[Dict[str, Any]] = []
    for row in reversed(rows):
        item: Dict[str, Any] = dict(row)
        try:
            payload = json.loads(str(item.pop("editorial_plan_json", "") or "{}"))
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict):
            item.update(payload)
        result.append(item)
    return result


def _record_content_signature_conn(
    conn: sqlite3.Connection,
    user_id: int,
    plan: Dict[str, Any],
    topic: str,
) -> bool:
    plan_id = str(plan.get("plan_id", ""))[:64]
    inserted = conn.execute(
        """
        INSERT OR IGNORE INTO content_signatures(
            user_id, character_id, platform, facet, intent, format, content_format, content_kind,
            hook, media, topic, plan_id, source_ref, editorial_plan_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            naz_character.CHARACTER_ID,
            str(plan.get("platform", "telegram")),
            str(plan.get("facet", "explorer")),
            str(plan.get("purpose", plan.get("intent", "исследовать"))),
            str(plan.get("structure", plan.get("format", "маленькая история"))),
            str(plan.get("content_format", "text_story")),
            (
                "video"
                if str(plan.get("production_mode", "")) == "story_first"
                else str(plan.get("content_kind", "text"))
            ),
            str(plan.get("hook", "наблюдение")),
            str(plan.get("visual_mode", plan.get("media", "редакционная иллюстрация"))),
            topic[:1000],
            plan_id,
            str(plan.get("source_ref", ""))[:1000],
            json.dumps(plan, ensure_ascii=False, separators=(",", ":"))[:16000],
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
    return inserted.rowcount == 1


def record_content_signature(user_id: int, plan: Dict[str, Any], topic: str) -> None:
    """Record one confirmed publication; plan_id makes crossposts idempotent."""
    with db() as conn:
        _record_content_signature_conn(conn, user_id, plan, topic)


GENERATION_PACKAGE_STATUSES = frozenset(
    {"not_run", "accepted", "rejected", "invalid", "unavailable"}
)
IMAGE_QA_STATUSES = frozenset({"not_run", "accepted", "rejected", "unavailable"})
HISTORY_COMMIT_STATUSES = frozenset(
    {"not_run", "pending", "sending", "committed", "failed"}
)


def _bounded(value: Any, limit: int) -> str:
    return str(value or "")[:limit]


def _upsert_editorial_release_event_conn(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    plan_id: str,
    platform: str,
    slot: Optional[str] = None,
    slot_captured_at: Optional[str] = None,
    generation_package_status: Optional[str] = None,
    image_qa_status: Optional[str] = None,
    telegram_chat_id: Optional[str] = None,
    telegram_message_id: Optional[str] = None,
    vk_job_id: Optional[str] = None,
    vk_receipt_id: Optional[str] = None,
    history_commit_status: Optional[str] = None,
) -> None:
    clean_plan_id = _bounded(plan_id, 64)
    clean_platform = _bounded(platform, 16)
    if not clean_plan_id or clean_platform not in {"telegram", "vk"}:
        raise ValueError("invalid editorial release identity")
    if generation_package_status is not None and generation_package_status not in GENERATION_PACKAGE_STATUSES:
        raise ValueError("invalid generation package status")
    if image_qa_status is not None and image_qa_status not in IMAGE_QA_STATUSES:
        raise ValueError("invalid image QA status")
    if history_commit_status is not None and history_commit_status not in HISTORY_COMMIT_STATUSES:
        raise ValueError("invalid history commit status")

    row = conn.execute(
        """
        SELECT slot, slot_captured_at, generation_package_status, image_qa_status,
               telegram_chat_id, telegram_message_id, vk_job_id, vk_receipt_id,
               history_commit_status
        FROM editorial_release_events
        WHERE user_id=? AND plan_id=? AND platform=?
        """,
        (user_id, clean_plan_id, clean_platform),
    ).fetchone()
    values = dict(row) if row else {
        "slot": "",
        "slot_captured_at": "",
        "generation_package_status": "not_run",
        "image_qa_status": "not_run",
        "telegram_chat_id": "",
        "telegram_message_id": "",
        "vk_job_id": "",
        "vk_receipt_id": "",
        "history_commit_status": "not_run",
    }
    updates = {
        "slot": (_bounded(slot, 80) if slot is not None else None),
        "slot_captured_at": (_bounded(slot_captured_at, 64) if slot_captured_at is not None else None),
        "generation_package_status": generation_package_status,
        "image_qa_status": image_qa_status,
        "telegram_chat_id": (_bounded(telegram_chat_id, 128) if telegram_chat_id is not None else None),
        "telegram_message_id": (_bounded(telegram_message_id, 128) if telegram_message_id is not None else None),
        "vk_job_id": (_bounded(vk_job_id, 128) if vk_job_id is not None else None),
        "vk_receipt_id": (_bounded(vk_receipt_id, 128) if vk_receipt_id is not None else None),
        "history_commit_status": history_commit_status,
    }
    for key, value in updates.items():
        if value is not None:
            if key in {"slot", "slot_captured_at"} and values[key]:
                continue
            if (
                values["history_commit_status"] == "committed"
                and key in {
                    "generation_package_status",
                    "image_qa_status",
                    "history_commit_status",
                }
                and not (key == "history_commit_status" and value == "committed")
            ):
                continue
            if (
                key == "history_commit_status"
                and values["history_commit_status"] == "committed"
                and value != "committed"
            ):
                continue
            values[key] = value
    conn.execute(
        """
        INSERT INTO editorial_release_events(
            user_id, plan_id, platform, slot, slot_captured_at,
            generation_package_status, image_qa_status, telegram_chat_id,
            telegram_message_id, vk_job_id, vk_receipt_id,
            history_commit_status, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, plan_id, platform) DO UPDATE SET
            slot=excluded.slot,
            slot_captured_at=excluded.slot_captured_at,
            generation_package_status=excluded.generation_package_status,
            image_qa_status=excluded.image_qa_status,
            telegram_chat_id=excluded.telegram_chat_id,
            telegram_message_id=excluded.telegram_message_id,
            vk_job_id=excluded.vk_job_id,
            vk_receipt_id=excluded.vk_receipt_id,
            history_commit_status=excluded.history_commit_status,
            updated_at=excluded.updated_at
        """,
        (
            user_id,
            clean_plan_id,
            clean_platform,
            values["slot"],
            values["slot_captured_at"],
            values["generation_package_status"],
            values["image_qa_status"],
            values["telegram_chat_id"],
            values["telegram_message_id"],
            values["vk_job_id"],
            values["vk_receipt_id"],
            values["history_commit_status"],
            utc_now(),
        ),
    )


def update_editorial_release_event(**values: Any) -> None:
    """Persist bounded operational metadata; never accepts post or prompt text."""
    with db() as conn:
        _upsert_editorial_release_event_conn(conn, **values)


def get_editorial_release_event(
    user_id: int,
    plan_id: str,
    platform: str,
) -> Optional[Dict[str, Any]]:
    init_db()
    with db() as conn:
        row = conn.execute(
            """
            SELECT user_id, plan_id, platform, slot, slot_captured_at,
                   generation_package_status, image_qa_status, telegram_chat_id,
                   telegram_message_id, vk_job_id, vk_receipt_id,
                   history_commit_status, updated_at
            FROM editorial_release_events
            WHERE user_id=? AND plan_id=? AND platform=?
            """,
            (user_id, str(plan_id), str(platform)),
        ).fetchone()
    return dict(row) if row else None


def claim_editorial_delivery(
    *,
    user_id: int,
    plan_id: str,
    platform: str,
) -> str:
    """Atomically claim one external send; never auto-retry an ambiguous send."""
    clean_plan_id = _bounded(plan_id, 64)
    clean_platform = _bounded(platform, 16)
    if not clean_plan_id or clean_platform not in {"telegram", "vk"}:
        raise ValueError("invalid editorial release identity")
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT history_commit_status FROM editorial_release_events
            WHERE user_id=? AND plan_id=? AND platform=?
            """,
            (user_id, clean_plan_id, clean_platform),
        ).fetchone()
        status = str(row["history_commit_status"] if row else "not_run")
        if status == "committed":
            return "committed"
        if status in {"sending", "failed"}:
            return "blocked"
        _upsert_editorial_release_event_conn(
            conn,
            user_id=user_id,
            plan_id=clean_plan_id,
            platform=clean_platform,
            history_commit_status="sending",
        )
        return "claimed"


def get_recent_semantic_theme_keys(user_id: int, limit: int = 5) -> List[str]:
    """Return themes from confirmed Telegram/VK publications only."""
    init_db()
    with db() as conn:
        rows = conn.execute(
            """
            SELECT semantic_theme
            FROM (
                SELECT semantic_theme, created_at
                FROM autopost_semantic_history
                WHERE user_id = ? AND character_id = ?
                  AND semantic_theme <> '' AND platform <> 'vk'
                UNION ALL
                SELECT semantic_theme, created_at
                FROM generated_posts
                WHERE user_id = ? AND published_to_channel = 1
                  AND semantic_theme <> '' AND task LIKE 'naz_vk_queue:%'
            )
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (
                user_id,
                naz_character.CHARACTER_ID,
                user_id,
                max(1, limit),
            ),
        ).fetchall()
    return [str(row["semantic_theme"]) for row in reversed(rows)]


def get_recent_semantic_card_keys(user_id: int, limit: int = 80) -> List[str]:
    """Return meaning cards from confirmed Telegram/VK publications only."""
    init_db()
    with db() as conn:
        rows = conn.execute(
            """
            SELECT semantic_card
            FROM (
                SELECT semantic_card, created_at
                FROM autopost_semantic_history
                WHERE user_id = ? AND character_id = ?
                  AND semantic_card <> '' AND platform <> 'vk'
                UNION ALL
                SELECT semantic_card, created_at
                FROM generated_posts
                WHERE user_id = ? AND published_to_channel = 1
                  AND semantic_card <> '' AND task LIKE 'naz_vk_queue:%'
            )
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (
                user_id,
                naz_character.CHARACTER_ID,
                user_id,
                max(1, limit),
            ),
        ).fetchall()
    return [str(row["semantic_card"]) for row in reversed(rows)]


def get_recent_rejected_semantic_theme_keys(user_id: int, limit: int = 12) -> List[str]:
    """Return blocked axes since the latest confirmed publication."""
    init_db()
    with db() as conn:
        rows = conn.execute(
            """
            SELECT semantic_theme, MAX(id) AS latest_id
            FROM autopost_semantic_rejections
            WHERE user_id = ? AND character_id = ? AND semantic_theme <> ''
              AND created_at > COALESCE(
                  (
                      SELECT MAX(created_at)
                      FROM (
                          SELECT created_at
                          FROM autopost_semantic_history
                          WHERE user_id = ? AND character_id = ?
                          UNION ALL
                          SELECT created_at
                          FROM generated_posts
                          WHERE user_id = ? AND published_to_channel = 1
                            AND semantic_theme <> ''
                      )
                  ),
                  ''
              )
            GROUP BY semantic_theme
            ORDER BY latest_id DESC
            LIMIT ?
            """,
            (
                user_id,
                naz_character.CHARACTER_ID,
                user_id,
                naz_character.CHARACTER_ID,
                user_id,
                max(1, limit),
            ),
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
            WHERE user_id = ? AND character_id = ? AND platform <> 'vk'
            ORDER BY id DESC
            LIMIT ?
            """,
            (user_id, naz_character.CHARACTER_ID, fetch_limit),
        ).fetchall()
        generated_rows = conn.execute(
            """
            SELECT semantic_theme, content, task, created_at, id
            FROM generated_posts
            WHERE user_id = ? AND published_to_channel = 1
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


def get_cached_semantic_history_profile(
    user_id: int,
    history_digest: str,
) -> Optional[Dict[str, Any]]:
    clean_digest = str(history_digest or "").strip()
    if not clean_digest:
        return None
    init_db()
    with db() as conn:
        row = conn.execute(
            """
            SELECT occupied_themes_json, exclusion_summary
            FROM autopost_semantic_profiles
            WHERE user_id = ? AND character_id = ? AND history_digest = ?
            """,
            (user_id, naz_character.CHARACTER_ID, clean_digest),
        ).fetchone()
    if not row:
        return None
    try:
        occupied = json.loads(row["occupied_themes_json"] or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(occupied, list):
        return None
    return {
        "history_digest": clean_digest,
        "occupied_theme_keys": [
            str(item)[:120]
            for item in occupied
            if str(item).strip()
        ],
        "exclusion_summary": str(row["exclusion_summary"] or ""),
    }


def cache_semantic_history_profile(
    *,
    user_id: int,
    history_digest: str,
    occupied_theme_keys: Iterable[str],
    exclusion_summary: str,
) -> None:
    clean_digest = str(history_digest or "").strip()
    if not clean_digest:
        raise ValueError("semantic history profile requires a digest")
    occupied = [
        str(item)[:120]
        for item in occupied_theme_keys
        if str(item).strip()
    ]
    init_db()
    with db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO autopost_semantic_profiles(
                user_id, character_id, history_digest, occupied_themes_json,
                exclusion_summary, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                naz_character.CHARACTER_ID,
                clean_digest,
                json.dumps(occupied, ensure_ascii=False),
                str(exclusion_summary or "")[:5000],
                utc_now(),
            ),
        )
        conn.execute(
            """
            DELETE FROM autopost_semantic_profiles
            WHERE user_id = ? AND character_id = ? AND history_digest NOT IN (
                SELECT history_digest
                FROM autopost_semantic_profiles
                WHERE user_id = ? AND character_id = ?
                ORDER BY created_at DESC
                LIMIT 12
            )
            """,
            (
                user_id,
                naz_character.CHARACTER_ID,
                user_id,
                naz_character.CHARACTER_ID,
            ),
        )


def record_accepted_semantic_post(
    *,
    user_id: int,
    platform: str,
    semantic_theme: str,
    semantic_card: str = "",
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
                user_id, character_id, platform, semantic_theme, semantic_card, central_thesis,
                conclusion, narrative_shape, key_meanings_json, content,
                content_hash, source_ref, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                naz_character.CHARACTER_ID,
                str(platform)[:40],
                clean_theme[:120],
                str(semantic_card or "")[:160],
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
    semantic_card: str = "",
    external_job_id: str = "",
    plan_id: str = "",
    editorial_plan: Optional[Dict[str, Any]] = None,
) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO generated_posts(
                user_id, expert_mode, task, topic, content, image_count,
                published_to_channel, semantic_theme, semantic_card, external_job_id,
                plan_id, editorial_plan_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                str(semantic_card or "")[:160],
                str(external_job_id or "")[:120],
                str(plan_id or "")[:64],
                json.dumps(editorial_plan or {}, ensure_ascii=False, separators=(",", ":"))[:16000],
                utc_now(),
            ),
        )


def get_unpublished_vk_jobs(user_id: int, limit: int = 100) -> List[Dict[str, Any]]:
    """Return only Naz job ids already known to this producer; never list consumer state."""
    init_db()
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, external_job_id, content, topic, plan_id, editorial_plan_json
            FROM generated_posts
            WHERE user_id = ? AND task LIKE 'naz_vk_queue:%'
              AND published_to_channel = 0 AND external_job_id <> ''
            ORDER BY id DESC
            LIMIT ?
            """,
            (user_id, max(1, limit)),
        ).fetchall()
    return [dict(row) for row in rows]


def mark_vk_generated_post_published(user_id: int, row_id: int) -> bool:
    """Promote one queued VK draft only after its exact job is confirmed done."""
    with db() as conn:
        row = conn.execute(
            """
            SELECT topic, editorial_plan_json
            FROM generated_posts
            WHERE id = ? AND user_id = ? AND task LIKE 'naz_vk_queue:%'
              AND published_to_channel = 0
            """,
            (int(row_id), user_id),
        ).fetchone()
        if row is None:
            return False
        updated = conn.execute(
            """
            UPDATE generated_posts
            SET published_to_channel = 1
            WHERE id = ? AND user_id = ? AND task LIKE 'naz_vk_queue:%'
              AND published_to_channel = 0
            """,
            (int(row_id), user_id),
        )
        if updated.rowcount != 1:
            return False
        try:
            plan = json.loads(str(row["editorial_plan_json"] or "{}"))
        except json.JSONDecodeError:
            plan = {}
        if isinstance(plan, dict) and str(plan.get("plan_id", "")):
            _record_content_signature_conn(conn, user_id, plan, str(row["topic"] or ""))
        return True


def reconcile_vk_publication_receipt(
    user_id: int,
    receipt: Mapping[str, str],
) -> str:
    """Atomically promote one known VK draft from one validated receipt.

    Returns ``history_inserted``, ``already_recorded`` or ``invalid_receipt``.
    A receipt cannot select a draft by source text and cannot store queue
    payload content in persona history.
    """
    job_id = _bounded(receipt.get("job_id"), 128)
    source_ref = _bounded(receipt.get("source_ref"), 1000)
    published_at = _bounded(receipt.get("published_at"), 64)
    if (
        receipt.get("schema") != "vk_publication_receipt.v1"
        or receipt.get("producer") != "naz"
        or not job_id
        or not source_ref
        or not published_at
    ):
        return "invalid_receipt"
    with db() as conn:
        row = conn.execute(
            """
            SELECT id, topic, plan_id, editorial_plan_json, published_to_channel
            FROM generated_posts
            WHERE user_id=? AND task LIKE 'naz_vk_queue:%' AND external_job_id=?
            ORDER BY id DESC LIMIT 1
            """,
            (user_id, job_id),
        ).fetchone()
        if row is None:
            return "invalid_receipt"
        try:
            plan = json.loads(str(row["editorial_plan_json"] or "{}"))
        except json.JSONDecodeError:
            return "invalid_receipt"
        if (
            not isinstance(plan, dict)
            or not str(plan.get("plan_id", ""))
            or str(plan.get("plan_id")) != str(row["plan_id"] or "")
            or str(plan.get("source_ref", "")) != source_ref
        ):
            return "invalid_receipt"

        committed = conn.execute(
            """
            SELECT 1 FROM editorial_release_events
            WHERE user_id=? AND plan_id=? AND history_commit_status='committed'
            LIMIT 1
            """,
            (user_id, str(plan["plan_id"])),
        ).fetchone()
        if bool(row["published_to_channel"]):
            # A legacy deployment could flip the draft flag before its history
            # write. Repair that state exactly once. The durable committed
            # event then prevents an old receipt from resurrecting history
            # after the normal 80-row persona-history pruning.
            inserted = False
            if committed is None:
                inserted = _record_content_signature_conn(
                    conn, user_id, plan, str(row["topic"] or "")
                )
            _upsert_editorial_release_event_conn(
                conn,
                user_id=user_id,
                plan_id=str(plan["plan_id"]),
                platform="vk",
                slot=str(plan.get("slot", "")),
                vk_job_id=job_id,
                vk_receipt_id=job_id,
                history_commit_status="committed",
            )
            return "history_inserted" if inserted else "already_recorded"

        updated = conn.execute(
            """
            UPDATE generated_posts SET published_to_channel=1
            WHERE id=? AND user_id=? AND published_to_channel=0
            """,
            (int(row["id"]), user_id),
        )
        if updated.rowcount != 1:
            return "already_recorded"
        inserted = False
        if committed is None:
            inserted = _record_content_signature_conn(
                conn, user_id, plan, str(row["topic"] or "")
            )
        _upsert_editorial_release_event_conn(
            conn,
            user_id=user_id,
            plan_id=str(plan["plan_id"]),
            platform="vk",
            slot=str(plan.get("slot", "")),
            vk_job_id=job_id,
            vk_receipt_id=job_id,
            history_commit_status="committed",
        )
        return "history_inserted" if inserted else "already_recorded"


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
