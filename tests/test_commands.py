from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.types import Update

from bot.config import settings
from bot.handlers import configure_bot_commands, setup_handlers


class FakeAuth:
    def __init__(self):
        self.connect_calls = []
        self.disconnected = False

    async def create_authorization_url(self, **kwargs):
        self.connect_calls.append(kwargs)
        return "https://threads.net/oauth/authorize?state=opaque"

    async def status(self):
        return None

    async def disconnect(self):
        self.disconnected = True
        return True


class MockTelegramSession(BaseSession):
    def __init__(self):
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


def make_update(
    text: str,
    *,
    user_id: int = 7,
    chat_type: str = "private",
    language_code: str | None = "ru",
) -> Update:
    return Update.model_validate(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 1_700_000_000,
                "chat": {"id": user_id if chat_type == "private" else -100, "type": chat_type},
                "from": {
                    "id": user_id,
                    "is_bot": False,
                    "first_name": "Admin",
                    "language_code": language_code,
                },
                "text": text,
                "entities": [{"type": "bot_command", "offset": 0, "length": len(text)}],
            },
        }
    )


@pytest.mark.asyncio
async def test_connect_command_is_admin_only_and_private(monkeypatch):
    monkeypatch.setattr(settings, "threads_oauth_admin_user_ids_raw", "7")
    monkeypatch.setattr(settings, "threads_primary_chat_id", -100999)
    monkeypatch.setattr(settings, "threads_app_id", "app")
    monkeypatch.setattr(settings, "threads_app_secret", "secret")
    monkeypatch.setattr(settings, "threads_token_encryption_key", "configured")
    auth = FakeAuth()
    dp = Dispatcher()
    setup_handlers(
        dp,
        search_service=AsyncMock(),
        database=AsyncMock(),
        watcher=AsyncMock(),
        threads_auth=auth,
    )
    session = MockTelegramSession()
    bot = Bot("123456:TEST_TOKEN", session=session)
    try:
        await dp.feed_update(bot, make_update("/connect_threads"))
        assert auth.connect_calls == [
            {
                "telegram_user_id": 7,
                "target_chat_id": -100999,
                "language": "ru",
            }
        ]
        reply_markup = session.requests[-1].reply_markup
        assert reply_markup.inline_keyboard[0][0].url.startswith(
            "https://threads.net/oauth/authorize"
        )

        session.requests.clear()
        await dp.feed_update(
            bot,
            make_update("/connect_threads", user_id=8),
        )
        assert "нет прав" in session.requests[-1].text

        session.requests.clear()
        await dp.feed_update(
            bot,
            make_update("/connect_threads", chat_type="supergroup"),
        )
        assert "только в личном чате" in session.requests[-1].text
    finally:
        await bot.session.close()


@pytest.mark.asyncio
async def test_status_and_disconnect_commands(monkeypatch):
    monkeypatch.setattr(settings, "threads_oauth_admin_user_ids_raw", "7")
    auth = FakeAuth()
    dp = Dispatcher()
    setup_handlers(
        dp,
        search_service=AsyncMock(),
        database=AsyncMock(),
        watcher=AsyncMock(),
        threads_auth=auth,
    )
    session = MockTelegramSession()
    bot = Bot("123456:TEST_TOKEN", session=session)
    try:
        await dp.feed_update(bot, make_update("/threads_status"))
        assert "/connect_threads" in session.requests[-1].text
        await dp.feed_update(bot, make_update("/disconnect_threads"))
        assert auth.disconnected is True
        assert "отключён" in session.requests[-1].text
    finally:
        await bot.session.close()


@pytest.mark.asyncio
async def test_help_uses_telegram_user_language(monkeypatch):
    monkeypatch.setattr(settings, "threads_oauth_admin_user_ids_raw", "7")
    dp = Dispatcher()
    setup_handlers(
        dp,
        search_service=AsyncMock(),
        database=AsyncMock(),
        watcher=AsyncMock(),
        threads_auth=FakeAuth(),
    )
    session = MockTelegramSession()
    bot = Bot("123456:TEST_TOKEN", session=session)
    try:
        await dp.feed_update(bot, make_update("/help", language_code="ru-RU"))
        assert "Пул фраз" in session.requests[-1].text

        await dp.feed_update(bot, make_update("/help", language_code="fr"))
        assert "Phrase pool" in session.requests[-1].text

        await dp.feed_update(bot, make_update("/help", language_code=None))
        assert "Phrase pool" in session.requests[-1].text
    finally:
        await bot.session.close()


@pytest.mark.asyncio
async def test_bot_command_descriptions_have_english_default_and_russian_locale():
    session = MockTelegramSession()
    bot = Bot("123456:TEST_TOKEN", session=session)
    try:
        await configure_bot_commands(bot)
    finally:
        await bot.session.close()

    assert len(session.requests) == 2
    default_menu, russian_menu = session.requests
    assert default_menu.language_code is None
    assert default_menu.commands[0].description.startswith("Start the bot")
    assert russian_menu.language_code == "ru"
    assert russian_menu.commands[0].description.startswith("Запустить бота")
