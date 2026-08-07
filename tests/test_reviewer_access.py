import asyncio
import hashlib
from collections.abc import AsyncGenerator
from datetime import timedelta
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import aiosqlite
import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.types import Update
from cryptography.fernet import Fernet

from bot.config import settings
from bot.crypto import TokenCipher
from bot.handlers import AccessMiddleware, setup_handlers
from bot.models import SearchResult
from bot.reviewer_access import ReviewerAccessService
from bot.storage import Database, to_db_timestamp, utc_now
from bot.threads_oauth import (
    REVIEW_OAUTH_PURPOSE,
    OAuthToken,
    ThreadsAuthService,
    ThreadsProfile,
)


class FakeOAuthClient:
    async def exchange_authorization_code(self, code: str) -> OAuthToken:
        assert code == "authorization-code"
        return OAuthToken("short-secret", "bearer", 0, user_id="review-42")

    async def exchange_long_lived_token(self, token: str) -> OAuthToken:
        assert token == "short-secret"
        return OAuthToken("review-long-secret", "bearer", 5_184_000)

    async def get_profile(self, token: str) -> ThreadsProfile:
        assert token == "review-long-secret"
        return ThreadsProfile("review-42", "meta_reviewer")


class MockTelegramSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests = []

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        return True

    async def close(self):
        return None

    async def stream_content(
        self,
        url: str,
        headers=None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        if False:
            yield b""


def make_update(text: str, *, user_id: int = 9001) -> Update:
    command_length = len(text.split(" ", 1)[0])
    return Update.model_validate(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 1_700_000_000,
                "chat": {"id": user_id, "type": "private"},
                "from": {"id": user_id, "is_bot": False, "first_name": "Reviewer"},
                "text": text,
                "entities": [
                    {"type": "bot_command", "offset": 0, "length": command_length}
                ],
            },
        }
    )


@pytest.mark.asyncio
async def test_hashed_review_code_grants_and_expires_access(tmp_path):
    database = Database(tmp_path / "bot.db")
    code = "review-code-with-enough-entropy-123456"
    service = ReviewerAccessService(
        database=database,
        code_hash=hashlib.sha256(code.encode("ascii")).hexdigest(),
        expires_at=utc_now() + timedelta(days=30),
    )

    assert await service.redeem_start_parameter(
        telegram_user_id=11,
        start_parameter=f"review_{code}",
    )
    assert await service.is_active(11)
    assert not await service.redeem_start_parameter(
        telegram_user_id=12,
        start_parameter="review_wrong-code-with-enough-entropy",
    )

    async with aiosqlite.connect(tmp_path / "bot.db") as db:
        rows = await (await db.execute("SELECT * FROM reviewer_access_grants")).fetchall()
        assert len(rows) == 1
        await db.execute(
            "UPDATE reviewer_access_grants SET expires_at = ?",
            (to_db_timestamp(utc_now() - timedelta(seconds=1)),),
        )
        await db.commit()

    assert code.encode("ascii") not in (tmp_path / "bot.db").read_bytes()
    assert not await service.is_active(11)


@pytest.mark.asyncio
async def test_existing_oauth_state_table_is_migrated_without_data_loss(tmp_path):
    database_path = tmp_path / "bot.db"
    async with aiosqlite.connect(database_path) as db:
        await db.execute(
            """
            CREATE TABLE chat_settings (
                chat_id INTEGER PRIMARY KEY,
                monitoring_enabled INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        await db.execute(
            "INSERT INTO chat_settings (chat_id, monitoring_enabled) VALUES (2, 1)"
        )
        await db.execute(
            """
            CREATE TABLE threads_oauth_states (
                state_hash TEXT PRIMARY KEY,
                telegram_user_id INTEGER NOT NULL,
                target_chat_id INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                consumed_at TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            INSERT INTO threads_oauth_states (
                state_hash, telegram_user_id, target_chat_id,
                expires_at, created_at
            ) VALUES ('existing', 1, 2, '2099-01-01T00:00:00+00:00',
                      '2026-01-01T00:00:00+00:00')
            """
        )
        await db.commit()

    database = Database(database_path)
    async with database.connection() as db:
        row = await (await db.execute(
            "SELECT purpose, language FROM threads_oauth_states "
            "WHERE state_hash = 'existing'"
        )).fetchone()
        chat_row = await (await db.execute(
            "SELECT monitoring_enabled, language FROM chat_settings WHERE chat_id = 2"
        )).fetchone()

    assert row is not None
    assert row["purpose"] == "primary"
    assert row["language"] == "en"
    assert chat_row is not None
    assert chat_row["monitoring_enabled"] == 1
    assert chat_row["language"] == "en"


@pytest.mark.asyncio
async def test_oauth_state_migration_tolerates_concurrent_connections(tmp_path):
    database_path = tmp_path / "bot.db"
    async with aiosqlite.connect(database_path) as db:
        await db.execute(
            """
            CREATE TABLE chat_settings (
                chat_id INTEGER PRIMARY KEY,
                monitoring_enabled INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE threads_oauth_states (
                state_hash TEXT PRIMARY KEY,
                telegram_user_id INTEGER NOT NULL,
                target_chat_id INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                consumed_at TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        await db.commit()

    database = Database(database_path)

    async def open_connection() -> None:
        async with database.connection() as db:
            await db.execute("SELECT 1")

    await asyncio.gather(*(open_connection() for _ in range(8)))

    async with aiosqlite.connect(database_path) as db:
        columns = await (await db.execute(
            "PRAGMA table_info(threads_oauth_states)"
        )).fetchall()
        chat_columns = await (await db.execute(
            "PRAGMA table_info(chat_settings)"
        )).fetchall()
    assert [row[1] for row in columns].count("purpose") == 1
    assert [row[1] for row in columns].count("language") == 1
    assert [row[1] for row in chat_columns].count("language") == 1


@pytest.mark.asyncio
async def test_review_oauth_token_is_encrypted_and_isolated_from_primary(tmp_path):
    database = Database(tmp_path / "bot.db")
    cipher = TokenCipher(Fernet.generate_key().decode("ascii"))
    await database.upsert_threads_connection(
        threads_user_id="primary-1",
        username="production",
        access_token_encrypted=cipher.encrypt("production-secret"),
        token_type="bearer",
        expires_at=utc_now() + timedelta(days=60),
        connected_by_telegram_user_id=1,
        target_chat_id=-100123,
    )
    await database.upsert_reviewer_access_grant(
        telegram_user_id=9001,
        expires_at=utc_now() + timedelta(days=30),
    )
    auth = ThreadsAuthService(
        database=database,
        cipher=cipher,
        oauth_client=FakeOAuthClient(),
        app_id="app-id",
        redirect_uri="https://example.test/oauth/threads/callback",
    )

    auth_url = await auth.create_authorization_url(
        telegram_user_id=9001,
        target_chat_id=9001,
        purpose=REVIEW_OAUTH_PURPOSE,
    )
    completed = await auth.complete_authorization(
        raw_state=parse_qs(urlparse(auth_url).query)["state"][0],
        code="authorization-code",
    )

    assert completed.purpose == REVIEW_OAUTH_PURPOSE
    primary = await database.get_active_threads_account()
    assert primary is not None
    assert primary["threads_user_id"] == "primary-1"
    review = await database.get_review_threads_account(9001)
    assert review is not None
    encrypted = bytes(review["access_token_encrypted"])
    assert b"review-long-secret" not in encrypted
    assert await auth.get_review_access_token(9001) == "review-long-secret"

    assert await auth.disconnect_review(9001)
    assert await database.get_review_threads_account(9001) is None
    assert await database.get_active_threads_account() is not None


@pytest.mark.asyncio
async def test_reviewer_deep_link_scopes_commands_and_uses_review_token(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(settings, "allowed_user_ids_raw", "7")
    monkeypatch.setattr(settings, "threads_oauth_admin_user_ids_raw", "7")
    monkeypatch.setattr(settings, "threads_app_id", "app")
    monkeypatch.setattr(settings, "threads_app_secret", "secret")
    monkeypatch.setattr(settings, "threads_token_encryption_key", "configured")
    database = Database(tmp_path / "bot.db")
    code = "review-code-with-enough-entropy-123456"
    reviewer_access = ReviewerAccessService(
        database=database,
        code_hash=hashlib.sha256(code.encode("ascii")).hexdigest(),
        expires_at=utc_now() + timedelta(days=30),
    )
    threads_auth = AsyncMock()
    threads_auth.create_authorization_url.return_value = (
        "https://threads.net/oauth/authorize?state=opaque"
    )
    threads_auth.get_review_access_token.return_value = "isolated-review-token"
    search_service = AsyncMock()
    search_service.search_by_keyword.return_value = SearchResult("ThreadsAPI", [], 0)

    dispatcher = Dispatcher()
    dispatcher.message.middleware(AccessMiddleware(reviewer_access, database))
    setup_handlers(
        dispatcher,
        search_service=search_service,
        database=database,
        watcher=AsyncMock(),
        threads_auth=threads_auth,
        reviewer_access=reviewer_access,
    )
    session = MockTelegramSession()
    bot = Bot("123456:TEST_TOKEN", session=session)
    try:
        await dispatcher.feed_update(bot, make_update("/search ThreadsAPI"))
        assert "do not have access" in session.requests[-1].text

        await dispatcher.feed_update(bot, make_update(f"/start review_{code}"))
        assert "Meta App Review access" in session.requests[-1].text
        assert (await database.get_chat_settings(9001))["language"] == "en"

        await dispatcher.feed_update(bot, make_update("/add production-phrase"))
        assert "not available" in session.requests[-1].text
        assert await database.list_phrases() == []

        await dispatcher.feed_update(bot, make_update("/connect_threads"))
        threads_auth.create_authorization_url.assert_awaited_once_with(
            telegram_user_id=9001,
            target_chat_id=9001,
            purpose=REVIEW_OAUTH_PURPOSE,
            language="en",
        )

        await dispatcher.feed_update(bot, make_update("/search ThreadsAPI"))
        search_service.search_by_keyword.assert_awaited_once_with(
            "ThreadsAPI",
            search_type="RECENT",
            access_token="isolated-review-token",
        )
    finally:
        await bot.session.close()
