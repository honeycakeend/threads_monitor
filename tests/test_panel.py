from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.methods import EditMessageText, GetChat, GetChatMember, GetMe, SendMessage
from aiogram.types import (
    Chat,
    ChatMemberMember,
    Message,
    ReplyKeyboardMarkup,
    Update,
    User,
)
from test_commands import MockTelegramSession, make_update

from bot.config import settings
from bot.handlers import AccessMiddleware, setup_handlers
from bot.models import SearchResult
from bot.reviewer_access import ReviewerAccessService
from bot.storage import Database, utc_now
from bot.threads_oauth import ThreadsNotConnectedError, ThreadsOAuthError
from bot.watcher import WatcherStats


class PanelSession(MockTelegramSession):
    def __init__(self):
        super().__init__()
        self.next_id = 100
        self.send_allowed = True

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if isinstance(method, GetMe):
            return User(
                id=bot.id,
                is_bot=True,
                first_name="Bot",
                username="alsmm_threads_monitor_bot",
            )
        if isinstance(method, GetChatMember):
            if not self.send_allowed:
                from aiogram.exceptions import TelegramForbiddenError

                raise TelegramForbiddenError(method=method, message="Forbidden")
            return ChatMemberMember(
                user={"id": bot.id, "is_bot": True, "first_name": "Bot"}
            )
        if isinstance(method, GetChat):
            return Chat(id=method.chat_id, type="supergroup", title="Команда")
        if isinstance(method, (SendMessage, EditMessageText)):
            self.next_id += 1
            return Message.model_validate(
                {
                    "message_id": getattr(method, "message_id", None) or self.next_id,
                    "date": 1_700_000_000,
                    "chat": {
                        "id": method.chat_id,
                        "type": "private" if method.chat_id > 0 else "supergroup",
                    },
                    "text": method.text,
                },
                context={"bot": bot},
            )
        return True


class Harness:
    async def text(self, text, *, user=7, language="ru", group=False, reply_to=None):
        update = make_update(
            text,
            user_id=user,
            language_code=language,
            chat_type="supergroup" if group else "private",
        )
        changes = {}
        if not text.startswith("/"):
            changes["entities"] = []
        if reply_to is not None:
            changes["reply_to_message"] = update.message.model_copy(
                update={"message_id": reply_to}
            )
        update = update.model_copy(
            update={"message": update.message.model_copy(update=changes)}
        )
        await self.dp.feed_update(self.bot, update)

    async def click(
        self,
        action,
        *,
        user=7,
        owner=7,
        language="ru",
        group=False,
        message_id=50,
        chat_id=-100,
    ):
        update = Update.model_validate(
            {
                "update_id": 2,
                "callback_query": {
                    "id": "click",
                    "chat_instance": "chat",
                    "data": f"ui:{owner}:{action}",
                    "from": {
                        "id": user,
                        "is_bot": False,
                        "first_name": "Yan",
                        "language_code": language,
                    },
                    "message": {
                        "message_id": message_id,
                        "date": 1_700_000_000,
                        "chat": {
                            "id": chat_id if group else user,
                            "type": "supergroup" if group else "private",
                        },
                        "from": {"id": 123456, "is_bot": True, "first_name": "Bot"},
                        "text": "Panel",
                    },
                },
            }
        )
        await self.dp.feed_update(self.bot, update)

    @property
    def last(self):
        return next(
            request
            for request in reversed(self.session.requests)
            if isinstance(request, (SendMessage, EditMessageText))
        )

    @property
    def buttons(self):
        return [
            button for row in self.last.reply_markup.inline_keyboard for button in row
        ]


@pytest_asyncio.fixture
async def app(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_user_ids_raw", "7,8")
    monkeypatch.setattr(settings, "threads_oauth_admin_user_ids_raw", "7")
    monkeypatch.setattr(settings, "threads_primary_chat_id", -100)
    monkeypatch.setattr(settings, "threads_app_id", "app")
    monkeypatch.setattr(settings, "threads_app_secret", "secret")
    monkeypatch.setattr(settings, "threads_token_encryption_key", "configured")
    harness = Harness()
    harness.db = Database(tmp_path / "panel.db")
    harness.auth = AsyncMock()
    harness.auth.status.return_value = {
        "username": "team",
        "primary_chat_id": -100,
        "expires_at": utc_now() + timedelta(days=30),
    }
    harness.auth.create_authorization_url.return_value = (
        "https://threads.net/oauth/authorize?state=opaque"
    )
    harness.search = AsyncMock()
    harness.search.search_by_keyword.return_value = SearchResult("designer", [], 0)
    harness.watcher = AsyncMock()
    harness.watcher.run_once.return_value = WatcherStats()
    harness.review = ReviewerAccessService(
        database=harness.db, code_hash="", expires_at=None
    )
    harness.dp = Dispatcher()
    harness.dp.message.middleware(AccessMiddleware(harness.review, harness.db))
    harness.dp.callback_query.middleware(AccessMiddleware(harness.review, harness.db))
    setup_handlers(
        harness.dp,
        database=harness.db,
        threads_auth=harness.auth,
        search_service=harness.search,
        watcher=harness.watcher,
        reviewer_access=harness.review,
    )
    harness.session = PanelSession()
    harness.bot = Bot("123456:TEST_TOKEN", session=harness.session)
    try:
        yield harness
    finally:
        await harness.dp.storage.close()
        await harness.bot.session.close()


@pytest.mark.asyncio
async def test_start_and_add_preserve_paused_notifications(app):
    await app.db.set_monitoring(7, False)
    await app.db.set_monitoring(-100, False)
    await app.text("/start")
    assert "Добавьте первую фразу" in app.last.text
    assert "Получатель: Команда" in app.last.text
    assert any(button.text == "Инструкция" for button in app.buttons)
    await app.text("/add designer")
    assert not (await app.db.get_chat_settings(7))["monitoring_enabled"]
    assert not (await app.db.get_chat_settings(-100))["monitoring_enabled"]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/instructions", "Инструкция", "инструкция"])
async def test_instructions_are_short_and_reachable(app, text):
    await app.text(text)
    assert "Как пользоваться ботом" in app.last.text
    assert "Найти публикации" in app.last.text
    assert len(app.last.text) < 2500
    assert any(button.callback_data.endswith(":home") for button in app.buttons)


@pytest.mark.asyncio
async def test_menu_and_instructions_are_localized(app):
    await app.text("/menu", language="en")
    assert "Tracked phrases" in [button.text for button in app.buttons]
    await app.text("Instructions", language="en")
    assert "How to use the bot" in app.last.text


@pytest.mark.asyncio
async def test_add_flow_edits_panel_and_does_not_enable_notifications(app):
    await app.click("add")
    await app.text("ищу дизайнера\nнужен сайт")
    assert {row["phrase"] for row in await app.db.list_phrases()} == {
        "ищу дизайнера",
        "нужен сайт",
    }
    assert isinstance(app.last, EditMessageText)
    assert app.last.message_id == 50
    assert "Добавлено новых фраз: 2" in app.last.text
    assert not (await app.db.get_chat_settings(-100))["monitoring_enabled"]


@pytest.mark.asyncio
async def test_shared_chat_input_redirects_and_does_not_capture_conversation(app):
    await app.click("add", group=True)
    assert "Ввод в личном чате" in app.last.text
    assert any(button.url and "start=input_add" in button.url for button in app.buttons)
    await app.text("обычный разговор", group=True)
    await app.text("designer", group=True)
    assert await app.db.list_phrases() == []
    await app.text("/start input_add")
    await app.text("designer")
    assert (await app.db.list_phrases())[0]["phrase"] == "designer"


@pytest.mark.asyncio
async def test_cancel_and_commands_clear_pending_input(app):
    await app.click("add")
    await app.click("home")
    await app.text("do not save")
    assert await app.db.list_phrases() == []
    await app.click("add")
    await app.text("/help")
    await app.text("do not save either")
    assert await app.db.list_phrases() == []


@pytest.mark.asyncio
async def test_interval_only_accepts_supported_presets(app):
    await app.click("interval")
    assert [
        b.callback_data.rsplit(":", 1)[-1]
        for b in app.buttons
        if ":every:" in b.callback_data
    ] == ["10", "30", "60", "120", "240"]
    for value in ("abc", "0", "5", "15", "45", "999", "³"):
        await app.click(f"every:{value}")
        assert "Выберите интервал кнопкой" in app.last.text
        assert await app.db.get_poll_interval_minutes() == 30
    await app.click("every:60")
    assert await app.db.get_poll_interval_minutes() == 60
    assert "Сохранено" in app.last.text
    assert any(button.text == "✓ 60 мин" for button in app.buttons)


@pytest.mark.asyncio
async def test_notifications_control_primary_chat_from_private_chat(app):
    await app.db.add_phrases(["designer"])
    await app.click("notify:on")
    assert (await app.db.get_chat_settings(-100))["monitoring_enabled"]
    assert not (await app.db.get_chat_settings(7))["monitoring_enabled"]
    assert "Готов к работе" in app.last.text
    await app.click("notify:off")
    assert not (await app.db.get_chat_settings(-100))["monitoring_enabled"]


@pytest.mark.asyncio
async def test_phrase_list_paginates_and_deletes_correct_phrase(app):
    await app.db.add_phrases([f"phrase {index}" for index in range(12)])
    await app.click("phrases")
    assert "phrase 7" in app.last.text and "phrase 8" not in app.last.text
    await app.click("phrases:1")
    item = (await app.db.list_phrases())[9]
    await app.click(f"delete:{item['id']}:1")
    assert await app.db.get_phrase(item["id"]) is None
    assert "Фраза удалена" in app.last.text


@pytest.mark.asyncio
async def test_one_time_search_uses_selected_sort_without_saving_query(app):
    await app.click("search")
    await app.text("designer")
    assert "Какие публикации" in app.last.text
    await app.click("top")
    app.search.search_by_keyword.assert_awaited_once_with("designer", search_type="TOP")
    assert await app.db.list_phrases() == []
    await app.click("top")
    assert app.search.search_by_keyword.await_count == 1


@pytest.mark.asyncio
async def test_search_error_is_actionable(app):
    app.search.search_by_keyword.side_effect = ThreadsNotConnectedError("expired")
    await app.click("search")
    await app.text("designer")
    await app.click("recent")
    assert "Проверьте подключение" in app.last.text


@pytest.mark.asyncio
async def test_account_actions_require_admin_and_private_chat(app):
    await app.text("/menu", user=8)
    assert not any(button.text == "Подключение Threads" for button in app.buttons)
    for action in (
        "account",
        "connect",
        "change_account",
        "disconnect",
        "confirm_disconnect",
    ):
        await app.click(action, user=8, owner=8)
        await app.click(action, group=True)
    app.auth.disconnect.assert_not_awaited()
    app.auth.create_authorization_url.assert_not_awaited()


@pytest.mark.asyncio
async def test_disconnect_is_confirmed_and_connect_uses_actor(app):
    await app.click("connect")
    app.auth.create_authorization_url.assert_awaited_once_with(
        telegram_user_id=7, target_chat_id=-100, language="ru"
    )
    await app.click("disconnect")
    app.auth.disconnect.assert_not_awaited()
    assert "Отключить Threads?" in app.last.text
    await app.click("account")
    app.auth.disconnect.assert_not_awaited()
    await app.text("/disconnect_threads")
    app.auth.disconnect.assert_not_awaited()
    await app.click("confirm_disconnect", message_id=app.session.next_id)
    app.auth.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_or_outdated_confirmation_cannot_disconnect(app):
    await app.click("disconnect")
    await app.click("account")
    await app.click("confirm_disconnect")
    app.auth.disconnect.assert_not_awaited()
    await app.click("disconnect")
    app.auth.status.return_value["expires_at"] = utc_now() + timedelta(days=60)
    await app.click("confirm_disconnect")
    app.auth.disconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_access_can_read_instructions_but_cannot_use_working_panel(app):
    app.review.is_active = AsyncMock(return_value=True)
    await app.text("Инструкция", user=9001)
    assert "Meta App Review" in app.last.text
    await app.click("notify:on", user=9001, owner=9001)
    assert not (await app.db.get_chat_settings(-100))["monitoring_enabled"]
    assert app.session.requests[-1].show_alert


@pytest.mark.asyncio
async def test_failed_connect_does_not_tell_user_to_follow_missing_link(
    app, monkeypatch
):
    monkeypatch.setattr(settings, "threads_app_id", "")
    await app.click("connect")
    assert "Не удалось начать подключение" in app.last.text
    app.auth.create_authorization_url.assert_not_awaited()


@pytest.mark.asyncio
async def test_callbacks_reject_unauthorized_users_and_other_owners(app):
    await app.db.add_phrases(["designer"])
    await app.click("notify:on", user=9001, owner=9001)
    await app.click("notify:on", user=8, owner=7)
    assert not (await app.db.get_chat_settings(-100))["monitoring_enabled"]
    assert app.session.requests[-1].show_alert


@pytest.mark.asyncio
async def test_expired_account_is_not_shown_as_ready(app):
    app.auth.status.return_value["expires_at"] = utc_now() - timedelta(seconds=1)
    await app.db.add_phrases(["designer"])
    await app.db.set_monitoring(-100, True)
    await app.text("/menu")
    assert "Подключите Threads" in app.last.text
    assert "Готов к работе" not in app.last.text


@pytest.mark.asyncio
async def test_run_uses_watcher_and_reports_results(app):
    await app.db.add_phrases(["designer"])
    # Notification eligibility is tested separately against real bindings in watcher tests.
    app.db.list_notification_chats = AsyncMock(return_value=[-100])
    stats = WatcherStats()
    stats.phrases_checked = 1
    stats.posts_sent = 2
    app.watcher.run_once.return_value = stats
    await app.click("run")
    app.watcher.run_once.assert_awaited_once()
    assert "Отправлено уведомлений: 2" in app.last.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "language,button", [("ru", "Сменить аккаунт"), ("en", "Change account")]
)
async def test_account_change_is_available_only_for_existing_connection(
    app, language, button
):
    await app.click("account", language=language)
    assert any(item.text == button for item in app.buttons)
    app.auth.status.return_value = None
    await app.click("account", language=language)
    assert not any(item.text == button for item in app.buttons)


@pytest.mark.asyncio
async def test_account_change_starts_oauth_without_disconnecting_or_changing_settings(
    app,
):
    await app.db.add_phrases(["designer"])
    await app.db.set_poll_interval_minutes(45)
    await app.db.set_monitoring(-100, False)
    await app.click("change_account")
    app.auth.create_authorization_url.assert_awaited_once_with(
        telegram_user_id=7, target_chat_id=-100, language="ru"
    )
    oauth_message = next(
        request
        for request in reversed(app.session.requests)
        if isinstance(request, SendMessage) and "Смена аккаунта Threads" in request.text
    )
    assert "только после успешной авторизации" in oauth_message.text
    assert "проверьте имя профиля" in oauth_message.text
    assert oauth_message.reply_markup.inline_keyboard[0][0].url.startswith(
        "https://threads.net/oauth/authorize"
    )
    assert any(button.callback_data.endswith(":account") for button in app.buttons)
    await app.click("account")
    assert "@team" in app.last.text
    app.auth.disconnect.assert_not_awaited()
    assert (await app.db.list_phrases())[0]["phrase"] == "designer"
    assert await app.db.get_poll_interval_minutes() == 45
    assert not (await app.db.get_chat_settings(-100))["monitoring_enabled"]


@pytest.mark.asyncio
async def test_account_change_failure_preserves_current_account(app):
    app.auth.create_authorization_url.side_effect = ThreadsOAuthError(
        "Cannot start authorization"
    )
    await app.click("change_account")
    assert "Не удалось начать подключение" in app.last.text
    app.auth.disconnect.assert_not_awaited()
    await app.click("account")
    assert "@team" in app.last.text


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/start", "/menu", "/instructions"])
async def test_entry_points_install_persistent_bottom_buttons(app, command):
    await app.text(command)
    markup = next(
        request.reply_markup
        for request in app.session.requests
        if isinstance(request, SendMessage)
        and isinstance(request.reply_markup, ReplyKeyboardMarkup)
    )
    assert [button.text for button in markup.keyboard[0]] == ["Меню", "Инструкция"]
    assert markup.is_persistent and markup.resize_keyboard
    assert markup.one_time_keyboard is False
    await app.text("Меню")
    assert "Threads Monitor" in app.last.text
    await app.text("Инструкция")
    assert "Как сменить аккаунт" in app.last.text


@pytest.mark.asyncio
async def test_group_navigation_uses_replies_without_changing_privacy(app):
    await app.text("/menu", group=True, language="en")
    markup = next(
        request.reply_markup
        for request in app.session.requests
        if isinstance(request, SendMessage)
        and isinstance(request.reply_markup, ReplyKeyboardMarkup)
    )
    assert [button.text for button in markup.keyboard[0]] == ["Menu", "Instructions"]
    assert markup.model_dump()["force_reply"] is True
    await app.text("Menu", group=True, reply_to=101, language="en")
    assert "Threads Monitor" in app.last.text
    await app.text("Instructions", group=True, reply_to=101, language="en")
    assert "Changing accounts" in app.last.text


@pytest.mark.asyncio
async def test_input_keeps_bottom_keyboard_without_extra_hint_message(app):
    await app.text("/menu")
    app.session.requests.clear()
    await app.click("add")
    prompt = app.last
    assert isinstance(prompt.reply_markup, ReplyKeyboardMarkup)
    assert prompt.reply_markup.is_persistent
    app.session.requests.clear()
    await app.text("designer")
    assert isinstance(app.last, EditMessageText)
    assert not any(isinstance(request, SendMessage) for request in app.session.requests)


@pytest.mark.asyncio
async def test_group_selection_is_admin_only_and_only_in_other_group(app):
    await app.text("/menu")
    assert not any(
        button.callback_data.endswith(":destination") for button in app.buttons
    )
    await app.click("home", group=True, chat_id=-200)
    assert any(
        button.text == "Получать уведомления в этой группе" for button in app.buttons
    )
    await app.click("home", group=True)
    assert not any(
        button.callback_data.endswith(":destination") for button in app.buttons
    )
    for group in (False, True):
        await app.click("destination", group=group, user=8, owner=8, chat_id=-200)
        assert "правами" in app.last.text
        await app.click(
            "confirm_destination", group=group, user=8, owner=8, chat_id=-200
        )
    assert await app.db.get_notification_chat_id() == -100


@pytest.mark.asyncio
async def test_group_selection_requires_confirmation_and_keeps_pause(app):
    await app.db.set_monitoring(-100, False)
    await app.click("destination", group=True, chat_id=-200)
    assert "единственным получателем" in app.last.text
    assert await app.db.get_notification_chat_id() == -100
    await app.click("confirm_destination", group=True, chat_id=-200)
    assert await app.db.get_notification_chat_id() == -200
    assert "Группа выбрана" in app.last.text
    assert "Получатель: этот чат" in app.last.text
    assert not (await app.db.get_chat_settings(-200))["monitoring_enabled"]
    await app.text("/menu")
    assert "Получатель: Команда" in app.last.text
    await app.db.add_phrases(["designer"])
    await app.click("notify:on")
    assert (await app.db.get_chat_settings(-200))["monitoring_enabled"]
    await app.click("change_account")
    app.auth.create_authorization_url.assert_awaited_once_with(
        telegram_user_id=7,
        target_chat_id=-200,
        language="ru",
    )


@pytest.mark.asyncio
async def test_cancelled_and_wrong_message_group_confirmations_do_nothing(app):
    await app.click("destination", group=True, chat_id=-200)
    await app.click("home", group=True, chat_id=-200)
    await app.click("confirm_destination", group=True, chat_id=-200)
    assert await app.db.get_notification_chat_id() == -100
    await app.click("destination", group=True, chat_id=-200)
    await app.click("confirm_destination", group=True, chat_id=-200, message_id=51)
    assert await app.db.get_notification_chat_id() == -100
    await app.click("destination", group=True, chat_id=-200)
    await app.click("confirm_destination", group=True, chat_id=-300)
    assert await app.db.get_notification_chat_id() == -100


@pytest.mark.asyncio
async def test_group_selection_rechecks_bot_sending_permission_on_confirmation(app):
    app.session.send_allowed = False
    await app.click("destination", group=True, chat_id=-200)
    assert "Разрешите" in app.last.text
    app.session.send_allowed = True
    await app.click("destination", group=True, chat_id=-200)
    app.session.send_allowed = False
    await app.click("confirm_destination", group=True, chat_id=-200)
    assert "Разрешите" in app.last.text
    assert await app.db.get_notification_chat_id() == -100


@pytest.mark.asyncio
async def test_can_start_oauth_after_group_selection_without_server_chat_setting(
    app, monkeypatch
):
    monkeypatch.setattr(settings, "threads_primary_chat_id", None)
    await app.click("connect")
    app.auth.create_authorization_url.assert_not_awaited()
    await app.click("destination", group=True, chat_id=-200)
    await app.click("confirm_destination", group=True, chat_id=-200)
    await app.click("connect")
    app.auth.create_authorization_url.assert_awaited_once_with(
        telegram_user_id=7,
        target_chat_id=-200,
        language="ru",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("group", [False, True])
async def test_navigation_hint_not_repeated_by_buttons_or_entry_points(app, group):
    await app.text("/menu", group=group)
    hints = lambda: [
        request
        for request in app.session.requests
        if isinstance(request, SendMessage)
        and request.text == "Настройки и инструкция — на кнопках внизу чата."
    ]
    assert len(hints()) == 1
    await app.click("phrases", group=group)
    await app.click("home", group=group)
    await app.text("Меню", group=group)
    await app.text("Инструкция", group=group)
    await app.click("home", group=group)
    await app.text("/menu", group=group)
    assert len(hints()) == 1
    # Stored across Database instances/restarts; /start explicitly reinstalls it.
    restarted = Database(app.db._path)
    assert await restarted.get_navigation_keyboard_version(-100 if group else 7)
    await app.text("/start", group=group)
    assert len(hints()) == 2


@pytest.mark.asyncio
async def test_old_custom_interval_buttons_and_links_open_presets(app):
    await app.click("custom", group=True)
    assert not any(b.url for b in app.buttons)
    assert not any(b.text == "Указать свой интервал" for b in app.buttons)
    await app.text("/start input_interval")
    assert any(b.text == "10 мин" for b in app.buttons)
    await app.text("45")
    assert await app.db.get_poll_interval_minutes() == 30
    app.watcher.reschedule.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_keyboard_installation_is_retried(app, monkeypatch):
    from aiogram.exceptions import TelegramNetworkError

    original = app.session.make_request

    async def fail_hint(bot, method, timeout=None):
        if isinstance(method, SendMessage) and isinstance(
            method.reply_markup, ReplyKeyboardMarkup
        ):
            raise TelegramNetworkError(
                method=method, message="Temporary network failure"
            )
        return await original(bot, method, timeout)

    monkeypatch.setattr(app.session, "make_request", fail_hint)
    with pytest.raises(TelegramNetworkError):
        await app.text("/menu")
    assert await app.db.get_navigation_keyboard_version(7) is None
    monkeypatch.setattr(app.session, "make_request", original)
    await app.text("/menu")
    assert await app.db.get_navigation_keyboard_version(7)


@pytest.mark.asyncio
async def test_ten_minutes_is_a_direct_group_button_and_wakes_scheduler(app):
    await app.click("interval", group=True)
    assert any(
        b.text == "10 мин" and b.callback_data.endswith(":every:10")
        for b in app.buttons
    )
    await app.click("every:10", group=True)
    assert await app.db.get_poll_interval_minutes() == 10
    assert any(b.text == "✓ 10 мин" for b in app.buttons)
    app.watcher.reschedule.assert_awaited_once()


@pytest.mark.asyncio
async def test_private_search_link_opens_search_step_without_group_input(app):
    await app.click("search", group=True)
    assert any(b.url and "start=input_search" in b.url for b in app.buttons)
    await app.text("/start input_search")
    await app.text("Threads API")
    assert "Какие публикации" in app.last.text
    await app.click("recent", message_id=app.last.message_id)
    app.search.search_by_keyword.assert_awaited_once()


@pytest.mark.asyncio
async def test_input_links_reject_unauthorized_and_reviewer_users(app):
    for user in [9001, 9002]:
        app.review.is_active = AsyncMock(return_value=user == 9002)
        await app.text("/start input_interval", user=user)
        await app.text("5", user=user)
    assert await app.db.get_poll_interval_minutes() != 5


@pytest.mark.asyncio
async def test_status_and_monitor_use_work_group_from_private_chat(app):
    await app.db.add_phrases(["designer"])
    await app.db.set_monitoring(7, True)
    await app.db.set_monitoring(-100, False)
    await app.text("/status")
    assert "Уведомления: приостановлены" in app.last.text
    assert "Получатель: Команда" in app.last.text
    await app.text("/monitor on")
    assert (await app.db.get_chat_settings(-100))["monitoring_enabled"]
    await app.text("/status", group=True)
    assert "Уведомления: включены" in app.last.text
    await app.text("/monitor off")
    assert not (await app.db.get_chat_settings(-100))["monitoring_enabled"]
    assert (await app.db.get_chat_settings(7))["monitoring_enabled"]


@pytest.mark.asyncio
async def test_interval_command_wakes_scheduler(app):
    await app.text("/interval 10")
    assert await app.db.get_poll_interval_minutes() == 10
    app.watcher.reschedule.assert_awaited_once()
