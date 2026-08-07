import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from bot.config import SearchType, settings
from bot.i18n import Language, choose

THREADS_KEYWORD_SEARCH_DAILY_LIMIT = 2200
MIN_POLL_INTERVAL_MINUTES = 5
MAX_POLL_INTERVAL_MINUTES = 240

SCHEMA = """
CREATE TABLE IF NOT EXISTS phrases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phrase TEXT NOT NULL COLLATE NOCASE,
    search_type TEXT NOT NULL DEFAULT 'RECENT',
    active INTEGER NOT NULL DEFAULT 1,
    initialized INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_phrases_phrase
    ON phrases(phrase);

CREATE TABLE IF NOT EXISTS chat_settings (
    chat_id INTEGER PRIMARY KEY,
    monitoring_enabled INTEGER NOT NULL DEFAULT 1,
    language TEXT NOT NULL DEFAULT 'en'
);

CREATE TABLE IF NOT EXISTS seen_posts (
    post_id TEXT NOT NULL,
    phrase_id INTEGER NOT NULL,
    found_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (post_id, phrase_id),
    FOREIGN KEY (phrase_id) REFERENCES phrases(id)
);

CREATE TABLE IF NOT EXISTS notified_posts (
    post_id TEXT PRIMARY KEY,
    phrase_id INTEGER NOT NULL,
    notified_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (phrase_id) REFERENCES phrases(id)
);

CREATE TABLE IF NOT EXISTS bot_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS threads_oauth_states (
    state_hash TEXT PRIMARY KEY,
    telegram_user_id INTEGER NOT NULL,
    target_chat_id INTEGER NOT NULL,
    purpose TEXT NOT NULL DEFAULT 'primary',
    language TEXT NOT NULL DEFAULT 'en',
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_threads_oauth_states_expiry
    ON threads_oauth_states(expires_at);

CREATE TABLE IF NOT EXISTS threads_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    threads_user_id TEXT NOT NULL UNIQUE,
    username TEXT,
    access_token_encrypted BLOB,
    token_type TEXT NOT NULL DEFAULT 'bearer',
    expires_at TEXT,
    refreshed_at TEXT,
    connected_by_telegram_user_id INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    disconnected_at TEXT
);

CREATE TABLE IF NOT EXISTS threads_chat_bindings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    is_primary INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_by_telegram_user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(account_id, chat_id),
    FOREIGN KEY (account_id) REFERENCES threads_accounts(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_threads_binding_primary_per_account
    ON threads_chat_bindings(account_id)
    WHERE is_primary = 1 AND active = 1;

CREATE TABLE IF NOT EXISTS reviewer_access_grants (
    telegram_user_id INTEGER PRIMARY KEY,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS threads_review_accounts (
    telegram_user_id INTEGER PRIMARY KEY,
    threads_user_id TEXT NOT NULL,
    username TEXT,
    access_token_encrypted BLOB NOT NULL,
    token_type TEXT NOT NULL DEFAULT 'bearer',
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (telegram_user_id)
        REFERENCES reviewer_access_grants(telegram_user_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_threads_review_accounts_threads_user
    ON threads_review_accounts(threads_user_id);

CREATE TABLE IF NOT EXISTS threads_data_deletion_requests (
    confirmation_code TEXT PRIMARY KEY,
    threads_user_id_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_threads_deletion_user_hash
    ON threads_data_deletion_requests(threads_user_id_hash);
"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_db_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def from_db_timestamp(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def split_phrases(raw: str) -> list[str]:
    parts: list[str] = []
    for line in raw.replace("|", "\n").splitlines():
        phrase = line.strip()
        if phrase:
            parts.append(phrase)
    return parts


def estimate_daily_requests(phrase_count: int, interval_minutes: int) -> int:
    if phrase_count <= 0 or interval_minutes <= 0:
        return 0
    cycles_per_day = 1440 / interval_minutes
    return int(phrase_count * cycles_per_day)


def format_interval_advice(
    phrase_count: int,
    interval_minutes: int,
    *,
    language: Language = "en",
) -> str:
    daily = estimate_daily_requests(phrase_count, interval_minutes)
    lines = (
        [
            f"Интервал: <b>{interval_minutes} мин</b>",
            f"Фраз в пуле: <b>{phrase_count}</b>",
            (
                f"≈ <b>{daily}</b> запросов/сутки "
                f"(лимит Meta: {THREADS_KEYWORD_SEARCH_DAILY_LIMIT})"
            ),
        ]
        if language == "ru"
        else [
            f"Interval: <b>{interval_minutes} min</b>",
            f"Phrases in pool: <b>{phrase_count}</b>",
            (
                f"≈ <b>{daily}</b> requests/day "
                f"(Meta limit: {THREADS_KEYWORD_SEARCH_DAILY_LIMIT})"
            ),
        ]
    )
    if daily > THREADS_KEYWORD_SEARCH_DAILY_LIMIT:
        lines.append(
            choose(
                language,
                ru="⚠️ Превышает лимит — увеличьте интервал или уменьшите число фраз.",
                en="⚠️ Above the limit — increase the interval or reduce the pool.",
            )
        )
    elif daily > THREADS_KEYWORD_SEARCH_DAILY_LIMIT * 0.8:
        lines.append(
            choose(
                language,
                ru="⚠️ Близко к лимиту — запас небольшой.",
                en="⚠️ Close to the limit — little capacity remains.",
            )
        )
    return "\n".join(lines)


class Database:
    def __init__(self, path: str | Path | None = None) -> None:
        self._path_obj = Path(path or settings.database_path)
        self._path = str(self._path_obj)

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[aiosqlite.Connection]:
        self._path_obj.parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(self._path)
        db.row_factory = aiosqlite.Row
        try:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.executescript(SCHEMA)
            await self._migrate(db)
            await db.commit()
            yield db
        finally:
            await db.close()

    @staticmethod
    async def _migrate(db: aiosqlite.Connection) -> None:
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='search_queries'"
        )
        if await cursor.fetchone() is not None:
            await db.execute(
                """
                INSERT OR IGNORE INTO phrases (
                    phrase, search_type, active, initialized, created_at
                )
                SELECT query, search_type, active, COALESCE(initialized, 0), created_at
                FROM search_queries
                WHERE active = 1
                """
            )
            await db.execute("DROP TABLE IF EXISTS search_queries")
            await db.execute("DELETE FROM seen_posts")

        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='bot_settings'"
        )
        if await cursor.fetchone() is None:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )

        cursor = await db.execute("PRAGMA table_info(threads_oauth_states)")
        columns = {str(row[1]) for row in await cursor.fetchall()}
        if "purpose" not in columns:
            try:
                await db.execute(
                    """
                    ALTER TABLE threads_oauth_states
                    ADD COLUMN purpose TEXT NOT NULL DEFAULT 'primary'
                    """
                )
            except sqlite3.OperationalError as exc:
                # Multiple startup workers can observe the pre-migration schema
                # concurrently. SQLite has no ADD COLUMN IF NOT EXISTS, so the
                # worker that loses the race treats only this exact result as done.
                if "duplicate column name" not in str(exc).lower():
                    raise
        if "language" not in columns:
            try:
                await db.execute(
                    """
                    ALTER TABLE threads_oauth_states
                    ADD COLUMN language TEXT NOT NULL DEFAULT 'en'
                    """
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise

        cursor = await db.execute("PRAGMA table_info(chat_settings)")
        chat_columns = {str(row[1]) for row in await cursor.fetchall()}
        if "language" not in chat_columns:
            try:
                await db.execute(
                    """
                    ALTER TABLE chat_settings
                    ADD COLUMN language TEXT NOT NULL DEFAULT 'en'
                    """
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise

    async def get_poll_interval_minutes(self) -> int:
        async with self.connection() as db:
            cursor = await db.execute(
                "SELECT value FROM bot_settings WHERE key = 'poll_interval_minutes'"
            )
            row = await cursor.fetchone()
            if row is None:
                return settings.poll_interval_minutes
            return int(row["value"])

    async def set_poll_interval_minutes(self, minutes: int) -> None:
        async with self.connection() as db:
            await db.execute(
                """
                INSERT INTO bot_settings (key, value)
                VALUES ('poll_interval_minutes', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(minutes),),
            )
            await db.commit()

    async def get_chat_settings(self, chat_id: int) -> dict:
        async with self.connection() as db:
            cursor = await db.execute(
                """
                SELECT chat_id, monitoring_enabled, language
                FROM chat_settings
                WHERE chat_id = ?
                """,
                (chat_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return {
                    "chat_id": chat_id,
                    "monitoring_enabled": False,
                    "language": "en",
                }
            return {
                "chat_id": chat_id,
                "monitoring_enabled": bool(row["monitoring_enabled"]),
                "language": str(row["language"]),
            }

    async def set_chat_language(self, chat_id: int, language: Language) -> None:
        async with self.connection() as db:
            await db.execute(
                """
                INSERT INTO chat_settings (chat_id, monitoring_enabled, language)
                VALUES (?, 0, ?)
                ON CONFLICT(chat_id) DO UPDATE SET language = excluded.language
                """,
                (chat_id, language),
            )
            await db.commit()

    async def set_monitoring(self, chat_id: int, enabled: bool) -> None:
        async with self.connection() as db:
            await db.execute(
                """
                INSERT INTO chat_settings (chat_id, monitoring_enabled)
                VALUES (?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET monitoring_enabled = excluded.monitoring_enabled
                """,
                (chat_id, int(enabled)),
            )
            await db.commit()

    async def list_notification_chats(self) -> list[int]:
        async with self.connection() as db:
            cursor = await db.execute(
                """
                SELECT DISTINCT cs.chat_id
                FROM chat_settings AS cs
                JOIN threads_chat_bindings AS b
                  ON b.chat_id = cs.chat_id
                 AND b.active = 1
                JOIN threads_accounts AS a
                  ON a.id = b.account_id
                 AND a.active = 1
                 AND a.access_token_encrypted IS NOT NULL
                WHERE cs.monitoring_enabled = 1
                """
            )
            rows = await cursor.fetchall()
            return [int(row["chat_id"]) for row in rows]

    async def add_phrase(
        self,
        phrase: str,
        search_type: SearchType = SearchType.RECENT,
    ) -> tuple[int, bool]:
        """Return phrase id and whether it was newly created."""
        async with self.connection() as db:
            cursor = await db.execute(
                "SELECT id, active FROM phrases WHERE phrase = ? COLLATE NOCASE",
                (phrase.strip(),),
            )
            existing = await cursor.fetchone()
            if existing is not None:
                await db.execute(
                    """
                    UPDATE phrases
                    SET active = 1, search_type = ?, initialized = 0
                    WHERE id = ?
                    """,
                    (search_type.value, existing["id"]),
                )
                await db.commit()
                return int(existing["id"]), False

            cursor = await db.execute(
                """
                INSERT INTO phrases (phrase, search_type, active, initialized)
                VALUES (?, ?, 1, 0)
                RETURNING id
                """,
                (phrase.strip(), search_type.value),
            )
            row = await cursor.fetchone()
            await db.commit()
            assert row is not None
            return int(row["id"]), True

    async def add_phrases(
        self,
        phrases: list[str],
        search_type: SearchType = SearchType.RECENT,
    ) -> list[tuple[int, str, bool]]:
        results: list[tuple[int, str, bool]] = []
        for phrase in phrases:
            phrase_id, created = await self.add_phrase(phrase, search_type=search_type)
            results.append((phrase_id, phrase, created))
        return results

    async def list_phrases(self) -> list[dict]:
        async with self.connection() as db:
            cursor = await db.execute(
                """
                SELECT id, phrase, search_type, initialized, created_at
                FROM phrases
                WHERE active = 1
                ORDER BY id
                """
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_phrase(self, phrase_id: int) -> dict | None:
        async with self.connection() as db:
            cursor = await db.execute(
                """
                SELECT id, phrase, search_type, initialized, created_at
                FROM phrases
                WHERE id = ? AND active = 1
                """,
                (phrase_id,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def list_active_phrases_for_monitoring(self) -> list[dict]:
        async with self.connection() as db:
            cursor = await db.execute(
                """
                SELECT id, phrase, search_type, initialized
                FROM phrases
                WHERE active = 1
                ORDER BY id
                """
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def remove_phrase_by_id(self, phrase_id: int) -> bool:
        async with self.connection() as db:
            cursor = await db.execute(
                "UPDATE phrases SET active = 0 WHERE id = ? AND active = 1",
                (phrase_id,),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def remove_phrase_by_text(self, phrase: str) -> bool:
        async with self.connection() as db:
            cursor = await db.execute(
                """
                UPDATE phrases SET active = 0
                WHERE phrase = ? COLLATE NOCASE AND active = 1
                """,
                (phrase.strip(),),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def is_post_seen(self, phrase_id: int, post_id: str) -> bool:
        async with self.connection() as db:
            cursor = await db.execute(
                """
                SELECT 1 FROM seen_posts
                WHERE phrase_id = ? AND post_id = ?
                """,
                (phrase_id, post_id),
            )
            return await cursor.fetchone() is not None

    async def mark_post_seen(self, phrase_id: int, post_id: str) -> None:
        async with self.connection() as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO seen_posts (phrase_id, post_id)
                VALUES (?, ?)
                """,
                (phrase_id, post_id),
            )
            await db.commit()

    async def is_post_notified(self, post_id: str) -> bool:
        async with self.connection() as db:
            cursor = await db.execute(
                "SELECT 1 FROM notified_posts WHERE post_id = ?",
                (post_id,),
            )
            return await cursor.fetchone() is not None

    async def mark_post_notified(self, phrase_id: int, post_id: str) -> None:
        async with self.connection() as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO notified_posts (post_id, phrase_id)
                VALUES (?, ?)
                """,
                (post_id, phrase_id),
            )
            await db.commit()

    async def mark_phrase_initialized(self, phrase_id: int) -> None:
        async with self.connection() as db:
            await db.execute(
                "UPDATE phrases SET initialized = 1 WHERE id = ?",
                (phrase_id,),
            )
            await db.commit()

    async def create_oauth_state(
        self,
        *,
        state_hash: str,
        telegram_user_id: int,
        target_chat_id: int,
        purpose: str = "primary",
        language: Language = "en",
        expires_at: datetime,
    ) -> None:
        now = to_db_timestamp(utc_now())
        async with self.connection() as db:
            await db.execute(
                """
                UPDATE threads_oauth_states
                SET consumed_at = ?
                WHERE telegram_user_id = ? AND consumed_at IS NULL
                """,
                (now, telegram_user_id),
            )
            await db.execute(
                """
                INSERT INTO threads_oauth_states (
                    state_hash, telegram_user_id, target_chat_id, purpose, language,
                    expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    state_hash,
                    telegram_user_id,
                    target_chat_id,
                    purpose,
                    language,
                    to_db_timestamp(expires_at),
                    now,
                ),
            )
            await db.execute(
                """
                DELETE FROM threads_oauth_states
                WHERE expires_at <= ? AND consumed_at IS NOT NULL
                """,
                (now,),
            )
            await db.commit()

    async def consume_oauth_state(self, state_hash: str) -> dict | None:
        now = to_db_timestamp(utc_now())
        async with self.connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                UPDATE threads_oauth_states
                SET consumed_at = ?
                WHERE state_hash = ?
                  AND consumed_at IS NULL
                  AND expires_at > ?
                RETURNING telegram_user_id, target_chat_id, purpose, language,
                          expires_at, created_at
                """,
                (now, state_hash, now),
            )
            row = await cursor.fetchone()
            await db.commit()
            return dict(row) if row else None

    async def upsert_reviewer_access_grant(
        self,
        *,
        telegram_user_id: int,
        expires_at: datetime,
    ) -> None:
        now = to_db_timestamp(utc_now())
        async with self.connection() as db:
            await db.execute(
                """
                INSERT INTO reviewer_access_grants (
                    telegram_user_id, expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (telegram_user_id, to_db_timestamp(expires_at), now, now),
            )
            await db.commit()

    async def has_active_reviewer_access(self, telegram_user_id: int) -> bool:
        now = to_db_timestamp(utc_now())
        async with self.connection() as db:
            # Expired grants are credentials too. Removing them also erases the
            # review-only token through the foreign-key cascade.
            await db.execute(
                "DELETE FROM reviewer_access_grants WHERE expires_at <= ?",
                (now,),
            )
            cursor = await db.execute(
                """
                SELECT 1
                FROM reviewer_access_grants
                WHERE telegram_user_id = ? AND expires_at > ?
                """,
                (telegram_user_id, now),
            )
            row = await cursor.fetchone()
            await db.commit()
            return row is not None

    async def upsert_review_threads_connection(
        self,
        *,
        telegram_user_id: int,
        threads_user_id: str,
        username: str | None,
        access_token_encrypted: bytes,
        token_type: str,
        expires_at: datetime,
    ) -> None:
        now = to_db_timestamp(utc_now())
        async with self.connection() as db:
            cursor = await db.execute(
                """
                SELECT 1 FROM reviewer_access_grants
                WHERE telegram_user_id = ? AND expires_at > ?
                """,
                (telegram_user_id, now),
            )
            if await cursor.fetchone() is None:
                raise ValueError("Reviewer access is no longer active")
            await db.execute(
                """
                INSERT INTO threads_review_accounts (
                    telegram_user_id, threads_user_id, username,
                    access_token_encrypted, token_type, expires_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    threads_user_id = excluded.threads_user_id,
                    username = excluded.username,
                    access_token_encrypted = excluded.access_token_encrypted,
                    token_type = excluded.token_type,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (
                    telegram_user_id,
                    threads_user_id,
                    username,
                    access_token_encrypted,
                    token_type,
                    to_db_timestamp(expires_at),
                    now,
                    now,
                ),
            )
            await db.commit()

    async def get_review_threads_account(self, telegram_user_id: int) -> dict | None:
        now = to_db_timestamp(utc_now())
        async with self.connection() as db:
            cursor = await db.execute(
                """
                SELECT a.telegram_user_id, a.threads_user_id, a.username,
                       a.access_token_encrypted, a.token_type, a.expires_at
                FROM threads_review_accounts AS a
                JOIN reviewer_access_grants AS g
                  ON g.telegram_user_id = a.telegram_user_id
                WHERE a.telegram_user_id = ? AND g.expires_at > ?
                """,
                (telegram_user_id, now),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def disconnect_review_threads_account(self, telegram_user_id: int) -> bool:
        async with self.connection() as db:
            cursor = await db.execute(
                "DELETE FROM threads_review_accounts WHERE telegram_user_id = ?",
                (telegram_user_id,),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def upsert_threads_connection(
        self,
        *,
        threads_user_id: str,
        username: str | None,
        access_token_encrypted: bytes,
        token_type: str,
        expires_at: datetime,
        connected_by_telegram_user_id: int,
        target_chat_id: int,
    ) -> int:
        now = to_db_timestamp(utc_now())
        async with self.connection() as db:
            await db.execute("BEGIN IMMEDIATE")

            # Version 1 intentionally has one active Threads account. Rows are retained
            # without credentials so the schema can later support multiple accounts.
            await db.execute(
                """
                UPDATE threads_accounts
                SET active = 0,
                    access_token_encrypted = NULL,
                    disconnected_at = ?,
                    updated_at = ?
                WHERE threads_user_id != ? AND active = 1
                """,
                (now, now, threads_user_id),
            )
            await db.execute(
                """
                UPDATE threads_chat_bindings
                SET active = 0, is_primary = 0, updated_at = ?
                WHERE account_id IN (
                    SELECT id FROM threads_accounts WHERE threads_user_id != ?
                ) AND active = 1
                """,
                (now, threads_user_id),
            )

            await db.execute(
                """
                INSERT INTO threads_accounts (
                    threads_user_id, username, access_token_encrypted, token_type,
                    expires_at, refreshed_at, connected_by_telegram_user_id,
                    active, created_at, updated_at, disconnected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, NULL)
                ON CONFLICT(threads_user_id) DO UPDATE SET
                    username = excluded.username,
                    access_token_encrypted = excluded.access_token_encrypted,
                    token_type = excluded.token_type,
                    expires_at = excluded.expires_at,
                    refreshed_at = excluded.refreshed_at,
                    connected_by_telegram_user_id = excluded.connected_by_telegram_user_id,
                    active = 1,
                    updated_at = excluded.updated_at,
                    disconnected_at = NULL
                """,
                (
                    threads_user_id,
                    username,
                    access_token_encrypted,
                    token_type,
                    to_db_timestamp(expires_at),
                    now,
                    connected_by_telegram_user_id,
                    now,
                    now,
                ),
            )
            cursor = await db.execute(
                "SELECT id FROM threads_accounts WHERE threads_user_id = ?",
                (threads_user_id,),
            )
            row = await cursor.fetchone()
            assert row is not None
            account_id = int(row["id"])

            await db.execute(
                """
                UPDATE threads_chat_bindings
                SET is_primary = 0, updated_at = ?
                WHERE account_id = ? AND active = 1
                """,
                (now, account_id),
            )
            await db.execute(
                """
                INSERT INTO threads_chat_bindings (
                    account_id, chat_id, is_primary, active,
                    created_by_telegram_user_id, created_at, updated_at
                ) VALUES (?, ?, 1, 1, ?, ?, ?)
                ON CONFLICT(account_id, chat_id) DO UPDATE SET
                    is_primary = 1,
                    active = 1,
                    created_by_telegram_user_id = excluded.created_by_telegram_user_id,
                    updated_at = excluded.updated_at
                """,
                (
                    account_id,
                    target_chat_id,
                    connected_by_telegram_user_id,
                    now,
                    now,
                ),
            )
            await db.execute(
                """
                INSERT INTO chat_settings (chat_id, monitoring_enabled)
                VALUES (?, 1)
                ON CONFLICT(chat_id) DO NOTHING
                """,
                (target_chat_id,),
            )
            await db.commit()
            return account_id

    async def get_active_threads_account(self) -> dict | None:
        async with self.connection() as db:
            cursor = await db.execute(
                """
                SELECT
                    a.id, a.threads_user_id, a.username,
                    a.access_token_encrypted, a.token_type, a.expires_at,
                    a.refreshed_at, a.connected_by_telegram_user_id,
                    b.chat_id AS primary_chat_id
                FROM threads_accounts AS a
                LEFT JOIN threads_chat_bindings AS b
                    ON b.account_id = a.id
                   AND b.active = 1
                   AND b.is_primary = 1
                WHERE a.active = 1 AND a.access_token_encrypted IS NOT NULL
                ORDER BY a.updated_at DESC
                LIMIT 1
                """
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def update_threads_token(
        self,
        *,
        account_id: int,
        expected_access_token_encrypted: bytes,
        access_token_encrypted: bytes,
        token_type: str,
        expires_at: datetime,
    ) -> bool:
        now = to_db_timestamp(utc_now())
        async with self.connection() as db:
            cursor = await db.execute(
                """
                UPDATE threads_accounts
                SET access_token_encrypted = ?, token_type = ?, expires_at = ?,
                    refreshed_at = ?, updated_at = ?
                WHERE id = ?
                  AND active = 1
                  AND access_token_encrypted = ?
                """,
                (
                    access_token_encrypted,
                    token_type,
                    to_db_timestamp(expires_at),
                    now,
                    now,
                    account_id,
                    expected_access_token_encrypted,
                ),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def disconnect_active_threads_account(self) -> bool:
        now = to_db_timestamp(utc_now())
        async with self.connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                UPDATE threads_accounts
                SET active = 0, access_token_encrypted = NULL,
                    disconnected_at = ?, updated_at = ?
                WHERE active = 1
                """,
                (now, now),
            )
            await db.execute(
                """
                UPDATE threads_chat_bindings
                SET active = 0, is_primary = 0, updated_at = ?
                WHERE active = 1
                """,
                (now,),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def delete_threads_user_data(self, threads_user_id: str) -> bool:
        async with self.connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "DELETE FROM threads_accounts WHERE threads_user_id = ?",
                (threads_user_id,),
            )
            deleted = cursor.rowcount
            cursor = await db.execute(
                """
                SELECT telegram_user_id
                FROM threads_review_accounts
                WHERE threads_user_id = ?
                """,
                (threads_user_id,),
            )
            reviewer_ids = [int(row["telegram_user_id"]) for row in await cursor.fetchall()]
            cursor = await db.execute(
                "DELETE FROM threads_review_accounts WHERE threads_user_id = ?",
                (threads_user_id,),
            )
            deleted += cursor.rowcount
            if reviewer_ids:
                await db.executemany(
                    "DELETE FROM reviewer_access_grants WHERE telegram_user_id = ?",
                    [(reviewer_id,) for reviewer_id in reviewer_ids],
                )
            await db.commit()
            return deleted > 0

    async def record_data_deletion(
        self,
        *,
        confirmation_code: str,
        threads_user_id_hash: str,
    ) -> str:
        now = to_db_timestamp(utc_now())
        async with self.connection() as db:
            await db.execute(
                """
                INSERT INTO threads_data_deletion_requests (
                    confirmation_code, threads_user_id_hash, status,
                    requested_at, completed_at
                ) VALUES (?, ?, 'completed', ?, ?)
                ON CONFLICT(threads_user_id_hash) DO UPDATE SET
                    status = 'completed',
                    completed_at = excluded.completed_at
                """,
                (confirmation_code, threads_user_id_hash, now, now),
            )
            cursor = await db.execute(
                """
                SELECT confirmation_code
                FROM threads_data_deletion_requests
                WHERE threads_user_id_hash = ?
                """,
                (threads_user_id_hash,),
            )
            row = await cursor.fetchone()
            await db.commit()
            assert row is not None
            return str(row["confirmation_code"])

    async def get_data_deletion_status(self, confirmation_code: str) -> dict | None:
        async with self.connection() as db:
            cursor = await db.execute(
                """
                SELECT confirmation_code, status, requested_at, completed_at
                FROM threads_data_deletion_requests
                WHERE confirmation_code = ?
                """,
                (confirmation_code,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    # Backward-compatible aliases used elsewhere in the project
    async def list_queries(self, chat_id: int) -> list[dict]:
        del chat_id
        return await self.list_phrases()

    async def list_active_queries_for_monitoring(self) -> list[dict]:
        phrases = await self.list_active_phrases_for_monitoring()
        return [
            {
                "id": item["id"],
                "query": item["phrase"],
                "search_type": item["search_type"],
                "initialized": item["initialized"],
            }
            for item in phrases
        ]
