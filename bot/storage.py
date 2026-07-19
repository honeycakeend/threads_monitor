from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiosqlite

from bot.config import SearchType, settings

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
    monitoring_enabled INTEGER NOT NULL DEFAULT 1
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
"""


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


def format_interval_advice(phrase_count: int, interval_minutes: int) -> str:
    daily = estimate_daily_requests(phrase_count, interval_minutes)
    lines = [
        f"Интервал: <b>{interval_minutes} мин</b>",
        f"Фраз в пуле: <b>{phrase_count}</b>",
        f"≈ <b>{daily}</b> запросов/сутки (лимит Meta: {THREADS_KEYWORD_SEARCH_DAILY_LIMIT})",
    ]
    if daily > THREADS_KEYWORD_SEARCH_DAILY_LIMIT:
        lines.append("⚠️ Превышает лимит — увеличьте интервал или уменьшите число фраз.")
    elif daily > THREADS_KEYWORD_SEARCH_DAILY_LIMIT * 0.8:
        lines.append("⚠️ Близко к лимиту — запас небольшой.")
    return "\n".join(lines)


class Database:
    def __init__(self, path: str | None = None) -> None:
        self._path = str(path or settings.database_path)

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[aiosqlite.Connection]:
        settings.database_path.parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(self._path)
        db.row_factory = aiosqlite.Row
        try:
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
        if await cursor.fetchone() is None:
            return

        await db.execute(
            """
            INSERT OR IGNORE INTO phrases (phrase, search_type, active, initialized, created_at)
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
                SELECT chat_id, monitoring_enabled
                FROM chat_settings
                WHERE chat_id = ?
                """,
                (chat_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return {"chat_id": chat_id, "monitoring_enabled": False}
            return {
                "chat_id": chat_id,
                "monitoring_enabled": bool(row["monitoring_enabled"]),
            }

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
                SELECT chat_id FROM chat_settings
                WHERE monitoring_enabled = 1
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
