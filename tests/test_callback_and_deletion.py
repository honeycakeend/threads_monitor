import base64
import hashlib
import hmac
import json
from datetime import timedelta
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet

from bot.crypto import TokenCipher
from bot.oauth_server import (
    DATA_DELETION_INSTRUCTIONS_URL,
    PRIVACY_POLICY_URL,
    TERMS_OF_SERVICE_URL,
    create_oauth_app,
)
from bot.storage import Database, utc_now
from bot.threads_oauth import (
    REVIEW_OAUTH_PURPOSE,
    OAuthToken,
    ThreadsAuthService,
    ThreadsProfile,
)


class FakeOAuthClient:
    async def exchange_authorization_code(self, code: str) -> OAuthToken:
        assert code == "valid-code"
        return OAuthToken("short-token", "bearer", 0, user_id="42")

    async def exchange_long_lived_token(self, token: str) -> OAuthToken:
        assert token == "short-token"
        return OAuthToken("long-token", "bearer", 5_184_000)

    async def get_profile(self, token: str) -> ThreadsProfile:
        assert token == "long-token"
        return ThreadsProfile("42", "agent")


def signed_request(payload: dict, secret: str) -> str:
    encoded_payload = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    signature = hmac.new(
        secret.encode(), encoded_payload.encode(), hashlib.sha256
    ).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).decode().rstrip("=")
    return f"{encoded_signature}.{encoded_payload}"


def build_app(tmp_path):
    database = Database(tmp_path / "bot.db")
    cipher = TokenCipher(Fernet.generate_key().decode("ascii"))
    auth = ThreadsAuthService(
        database=database,
        cipher=cipher,
        oauth_client=FakeOAuthClient(),
        app_id="app",
        redirect_uri="https://threads-auth.adigitalnyc.com/oauth/threads/callback",
    )
    bot = AsyncMock()
    app = create_oauth_app(
        database=database,
        threads_auth=auth,
        bot=bot,
        app_secret="app-secret",
        public_base_url="https://threads-auth.adigitalnyc.com",
    )
    return database, cipher, auth, bot, app


@pytest.mark.asyncio
async def test_homepage_is_public_and_links_legal_pages(tmp_path):
    _, _, _, _, app = build_app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        home = await client.get("/")
        robots = await client.get("/robots.txt")
        deletion_get = await client.get("/oauth/threads/data-deletion")

    assert home.status_code == 200
    assert "text/html" in home.headers["content-type"]
    assert "Threads Monitor Bot" in home.text
    assert PRIVACY_POLICY_URL in home.text
    assert TERMS_OF_SERVICE_URL in home.text
    assert DATA_DELETION_INSTRUCTIONS_URL in home.text
    assert robots.status_code == 200
    assert "Allow: /" in robots.text
    assert deletion_get.status_code == 405


@pytest.mark.asyncio
async def test_callback_succeeds_notifies_and_rejects_replay(tmp_path):
    database, _, auth, bot, app = build_app(tmp_path)
    auth_url = await auth.create_authorization_url(
        telegram_user_id=77,
        target_chat_id=-100123,
        language="ru",
    )
    state = parse_qs(urlparse(auth_url).query)["state"][0]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        response = await client.get(
            "/oauth/threads/callback",
            params={"state": state, "code": "valid-code"},
        )
        replay = await client.get(
            "/oauth/threads/callback",
            params={"state": state, "code": "valid-code"},
        )

    assert response.status_code == 200
    assert "Threads подключён" in response.text
    assert response.headers["cache-control"] == "no-store"
    assert replay.status_code == 400
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.args[0] == 77
    assert await database.get_active_threads_account() is not None
    assert (await database.get_chat_settings(-100123))["language"] == "ru"


@pytest.mark.asyncio
async def test_review_callback_keeps_token_isolated_and_notifies_in_english(tmp_path):
    database, _, auth, bot, app = build_app(tmp_path)
    await database.upsert_reviewer_access_grant(
        telegram_user_id=88,
        expires_at=utc_now() + timedelta(days=30),
    )
    auth_url = await auth.create_authorization_url(
        telegram_user_id=88,
        target_chat_id=88,
        purpose=REVIEW_OAUTH_PURPOSE,
    )
    state = parse_qs(urlparse(auth_url).query)["state"][0]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        response = await client.get(
            "/oauth/threads/callback",
            params={"state": state, "code": "valid-code"},
        )

    assert response.status_code == 200
    assert "Threads connected" in response.text
    assert await database.get_active_threads_account() is None
    assert await database.get_review_threads_account(88) is not None
    assert "Meta App Review" in bot.send_message.await_args.args[1]


@pytest.mark.asyncio
async def test_callback_denial_consumes_state_without_creating_account(tmp_path):
    database, _, auth, bot, app = build_app(tmp_path)
    auth_url = await auth.create_authorization_url(
        telegram_user_id=77,
        target_chat_id=-100123,
        language="ru",
    )
    state = parse_qs(urlparse(auth_url).query)["state"][0]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        denied = await client.get(
            "/oauth/threads/callback",
            params={"state": state, "error": "access_denied"},
        )
        replay = await client.get(
            "/oauth/threads/callback",
            params={"state": state, "code": "valid-code"},
        )

    assert denied.status_code == 400
    assert "Подключение отменено" in denied.text
    assert replay.status_code == 400
    bot.send_message.assert_awaited_once()
    assert await database.get_active_threads_account() is None


@pytest.mark.asyncio
async def test_data_deletion_verifies_signature_deletes_and_returns_status_url(tmp_path):
    database, cipher, _, _, app = build_app(tmp_path)
    await database.upsert_threads_connection(
        threads_user_id="42",
        username="agent",
        access_token_encrypted=cipher.encrypt("token"),
        token_type="bearer",
        expires_at=utc_now() + timedelta(days=60),
        connected_by_telegram_user_id=77,
        target_chat_id=-100123,
    )
    valid = signed_request(
        {"algorithm": "HMAC-SHA256", "user_id": "42"},
        "app-secret",
    )
    transport = httpx.ASGITransport(app=app)
    headers = {"content-type": "application/x-www-form-urlencoded"}
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        invalid = await client.post(
            "/oauth/threads/data-deletion",
            content=urlencode({"signed_request": valid + "tampered"}),
            headers=headers,
        )
        response = await client.post(
            "/oauth/threads/data-deletion",
            content=urlencode({"signed_request": valid}),
            headers=headers,
        )
        payload = response.json()
        status = await client.get(urlparse(payload["url"]).path)

    assert invalid.status_code == 400
    assert response.status_code == 200
    assert payload["url"].startswith(
        "https://threads-auth.adigitalnyc.com/oauth/threads/data-deletion/status/"
    )
    assert payload["confirmation_code"]
    assert status.status_code == 200
    assert await database.get_active_threads_account() is None


@pytest.mark.asyncio
async def test_deauthorize_is_idempotent_for_valid_signed_request(tmp_path):
    _, _, _, _, app = build_app(tmp_path)
    valid = signed_request(
        {"algorithm": "HMAC-SHA256", "user_id": "missing-user"},
        "app-secret",
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        response = await client.post(
            "/oauth/threads/deauthorize",
            content=urlencode({"signed_request": valid}),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
    assert response.status_code == 200
    assert response.json() == {"success": True}


@pytest.mark.asyncio
async def test_deauthorize_deletes_matching_review_token_and_grant(tmp_path):
    database, cipher, _, _, app = build_app(tmp_path)
    await database.upsert_reviewer_access_grant(
        telegram_user_id=88,
        expires_at=utc_now() + timedelta(days=30),
    )
    await database.upsert_review_threads_connection(
        telegram_user_id=88,
        threads_user_id="review-user",
        username="reviewer",
        access_token_encrypted=cipher.encrypt("review-token"),
        token_type="bearer",
        expires_at=utc_now() + timedelta(days=60),
    )
    valid = signed_request(
        {"algorithm": "HMAC-SHA256", "user_id": "review-user"},
        "app-secret",
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        response = await client.post(
            "/oauth/threads/deauthorize",
            content=urlencode({"signed_request": valid}),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )

    assert response.status_code == 200
    assert await database.get_review_threads_account(88) is None
    assert not await database.has_active_reviewer_access(88)
