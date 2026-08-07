from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import aiosqlite
import pytest
from cryptography.fernet import Fernet

from bot.crypto import TokenCipher, TokenEncryptionError
from bot.storage import Database, utc_now
from bot.threads_oauth import (
    InvalidOAuthStateError,
    OAuthToken,
    ThreadsAuthService,
    ThreadsProfile,
)


class FakeOAuthClient:
    async def exchange_authorization_code(self, code: str) -> OAuthToken:
        assert code == "authorization-code"
        return OAuthToken("short-secret", "bearer", 0, user_id="threads-42")

    async def exchange_long_lived_token(self, token: str) -> OAuthToken:
        assert token == "short-secret"
        return OAuthToken("long-secret", "bearer", 5_184_000)

    async def get_profile(self, token: str) -> ThreadsProfile:
        assert token == "long-secret"
        return ThreadsProfile("threads-42", "agent_account")


def build_service(tmp_path):
    database = Database(tmp_path / "bot.db")
    cipher = TokenCipher(Fernet.generate_key().decode("ascii"))
    service = ThreadsAuthService(
        database=database,
        cipher=cipher,
        oauth_client=FakeOAuthClient(),
        app_id="app-id",
        redirect_uri="https://threads-auth.adigitalnyc.com/oauth/threads/callback",
        state_ttl_minutes=10,
    )
    return database, cipher, service


@pytest.mark.asyncio
async def test_state_is_hashed_bound_and_one_time(tmp_path):
    _, _, service = build_service(tmp_path)
    url = await service.create_authorization_url(
        telegram_user_id=101,
        target_chat_id=-100999,
        language="ru",
    )
    query = parse_qs(urlparse(url).query)
    raw_state = query["state"][0]

    assert query["scope"] == ["threads_basic,threads_keyword_search"]
    assert query["redirect_uri"] == [
        "https://threads-auth.adigitalnyc.com/oauth/threads/callback"
    ]

    async with aiosqlite.connect(tmp_path / "bot.db") as db:
        row = await (await db.execute(
            "SELECT state_hash, telegram_user_id, target_chat_id, language "
            "FROM threads_oauth_states"
        )).fetchone()
    assert row is not None
    assert row[0] == service.hash_state(raw_state)
    assert raw_state not in row[0]
    assert row[1:] == (101, -100999, "ru")

    consumed = await service.consume_state(raw_state)
    assert consumed["telegram_user_id"] == 101
    assert consumed["language"] == "ru"
    with pytest.raises(InvalidOAuthStateError):
        await service.consume_state(raw_state)


@pytest.mark.asyncio
async def test_expired_state_is_rejected(tmp_path):
    database, _, service = build_service(tmp_path)
    raw_state = "x" * 32
    await database.create_oauth_state(
        state_hash=service.hash_state(raw_state),
        telegram_user_id=1,
        target_chat_id=2,
        expires_at=utc_now() - timedelta(seconds=1),
    )
    with pytest.raises(InvalidOAuthStateError):
        await service.consume_state(raw_state)


@pytest.mark.asyncio
async def test_callback_completion_encrypts_token_and_disconnect_removes_it(tmp_path):
    database, cipher, service = build_service(tmp_path)
    url = await service.create_authorization_url(
        telegram_user_id=101,
        target_chat_id=-100999,
    )
    raw_state = parse_qs(urlparse(url).query)["state"][0]
    completed = await service.complete_authorization(
        raw_state=raw_state,
        code="authorization-code",
    )

    account = await database.get_active_threads_account()
    assert account is not None
    encrypted = bytes(account["access_token_encrypted"])
    assert b"long-secret" not in encrypted
    assert cipher.decrypt(encrypted) == "long-secret"
    assert account["threads_user_id"] == completed.threads_user_id == "threads-42"
    assert account["primary_chat_id"] == -100999

    assert await service.disconnect() is True
    assert await database.get_active_threads_account() is None
    async with aiosqlite.connect(tmp_path / "bot.db") as db:
        row = await (await db.execute(
            "SELECT access_token_encrypted, active FROM threads_accounts"
        )).fetchone()
    assert row == (None, 0)


def test_cipher_rejects_wrong_key():
    first = TokenCipher(Fernet.generate_key().decode("ascii"))
    second = TokenCipher(Fernet.generate_key().decode("ascii"))
    encrypted = first.encrypt("credential")
    with pytest.raises(TokenEncryptionError):
        second.decrypt(encrypted)
