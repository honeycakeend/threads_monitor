from datetime import timedelta

import httpx
import pytest
from cryptography.fernet import Fernet

from bot.crypto import TokenCipher
from bot.search_service import SearchService
from bot.storage import Database, utc_now
from bot.threads_client import ThreadsClient
from bot.threads_oauth import OAuthToken, ThreadsTokenManager
from bot.watcher import SearchWatcher


class RefreshingOAuthClient:
    def __init__(self):
        self.calls = 0

    async def refresh_long_lived_token(self, token: str) -> OAuthToken:
        self.calls += 1
        assert token == "old-token"
        return OAuthToken("new-token", "bearer", 5_184_000)


@pytest.mark.asyncio
async def test_token_manager_refreshes_due_token_and_persists_ciphertext(tmp_path):
    database = Database(tmp_path / "bot.db")
    cipher = TokenCipher(Fernet.generate_key().decode("ascii"))
    await database.upsert_threads_connection(
        threads_user_id="42",
        username="agent",
        access_token_encrypted=cipher.encrypt("old-token"),
        token_type="bearer",
        expires_at=utc_now() + timedelta(days=2),
        connected_by_telegram_user_id=1,
        target_chat_id=2,
    )
    oauth = RefreshingOAuthClient()
    manager = ThreadsTokenManager(
        database=database,
        cipher=cipher,
        oauth_client=oauth,
        refresh_before=timedelta(days=7),
    )

    assert await manager.get_access_token() == "new-token"
    assert oauth.calls == 1
    account = await database.get_active_threads_account()
    assert account is not None
    assert b"new-token" not in bytes(account["access_token_encrypted"])
    assert cipher.decrypt(bytes(account["access_token_encrypted"])) == "new-token"


@pytest.mark.asyncio
async def test_search_uses_selected_token_in_header_not_query():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer selected-token"
        assert "access_token" not in request.url.params
        return httpx.Response(200, json={"data": []})

    class Manager:
        calls = 0

        async def get_access_token(self):
            self.calls += 1
            return "selected-token"

    manager = Manager()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = SearchService(
            client=ThreadsClient(http_client=client),
            token_manager=manager,
        )
        result = await service.search_by_keyword("test")

    assert result.total == 0
    assert manager.calls == 1
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_watcher_resolves_token_once_per_cycle(tmp_path):
    database = Database(tmp_path / "bot.db")
    cipher = TokenCipher(Fernet.generate_key().decode("ascii"))
    await database.upsert_threads_connection(
        threads_user_id="42",
        username="agent",
        access_token_encrypted=cipher.encrypt("cycle-token"),
        token_type="bearer",
        expires_at=utc_now() + timedelta(days=60),
        connected_by_telegram_user_id=1,
        target_chat_id=-1001,
    )
    await database.add_phrase("first")
    await database.add_phrase("second")
    await database.set_monitoring(-1001, True)

    class Manager:
        calls = 0

        async def get_access_token(self):
            self.calls += 1
            return "cycle-token"

    class Search:
        def __init__(self):
            self.calls = []

        async def search_by_keyword(self, query, *, search_type, access_token):
            self.calls.append((query, access_token))
            return type("Result", (), {"posts": []})()

    class Bot:
        async def send_message(self, *args, **kwargs):
            raise AssertionError("No posts should be sent")

    manager = Manager()
    search = Search()
    watcher = SearchWatcher(
        Bot(),
        database=database,
        search_service=search,
        token_manager=manager,
    )
    stats = await watcher.run_once()

    assert stats.phrases_checked == 2
    assert manager.calls == 1
    assert search.calls == [("first", "cycle-token"), ("second", "cycle-token")]
