from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from test_oauth_state_and_storage import build_service

from bot.config import settings
from bot.storage import Database, utc_now


async def connect(database, *, target=-100, account="42"):
    return await database.upsert_threads_connection(
        threads_user_id=account,
        username="team",
        access_token_encrypted=b"encrypted",
        token_type="bearer",
        expires_at=utc_now() + timedelta(days=30),
        connected_by_telegram_user_id=7,
        target_chat_id=target,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_group_selection_moves_only_recipient_and_preserves_settings(
    tmp_path,
    monkeypatch,
    enabled,
):
    monkeypatch.setattr(settings, "threads_oauth_admin_user_ids_raw", "7")
    database = Database(tmp_path / "bot.db")
    await connect(database)
    await database.set_monitoring(-100, enabled)
    await database.add_phrases(["designer"])
    await database.set_poll_interval_minutes(45)
    assert await database.select_notification_group(
        chat_id=-200,
        actor=7,
        expected_chat_id=-100,
        language="ru",
    )
    restarted = Database(tmp_path / "bot.db")
    assert await restarted.get_notification_chat_id() == -200
    assert (await restarted.get_active_threads_account())["primary_chat_id"] == -200
    assert await restarted.list_notification_chats() == ([-200] if enabled else [])
    assert not (await restarted.get_chat_settings(-100))["monitoring_enabled"]
    assert (await restarted.get_chat_settings(-200))["monitoring_enabled"] == enabled
    assert (await restarted.get_chat_settings(-200))["language"] == "ru"
    assert (await restarted.list_phrases())[0]["phrase"] == "designer"
    assert await restarted.get_poll_interval_minutes() == 45
    await connect(restarted, target=-100, account="replacement")
    assert (await restarted.get_active_threads_account())["primary_chat_id"] == -200
    assert await restarted.list_notification_chats() == ([-200] if enabled else [])


@pytest.mark.asyncio
async def test_selection_before_oauth_and_old_link_cannot_restore_previous_group(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(settings, "threads_primary_chat_id", None)
    monkeypatch.setattr(settings, "threads_oauth_admin_user_ids_raw", "7")
    database, _, service = build_service(tmp_path)
    url = await service.create_authorization_url(
        telegram_user_id=7, target_chat_id=-100
    )
    state = parse_qs(urlparse(url).query)["state"][0]
    assert await database.select_notification_group(
        chat_id=-200,
        actor=7,
        expected_chat_id=None,
        language="ru",
    )
    result = await service.complete_authorization(
        raw_state=state, code="authorization-code"
    )
    assert result.target_chat_id == -200
    assert (await database.get_active_threads_account())["primary_chat_id"] == -200
    assert await database.list_notification_chats() == []  # Pause retained.
    await service.disconnect()
    assert await database.get_notification_chat_id() == -200


@pytest.mark.asyncio
async def test_stale_selection_and_revoked_admin_do_not_redirect(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "threads_oauth_admin_user_ids_raw", "7")
    database = Database(tmp_path / "bot.db")
    await connect(database)
    assert await database.select_notification_group(
        chat_id=-200,
        actor=7,
        expected_chat_id=-100,
        language="ru",
    )
    assert not await database.select_notification_group(
        chat_id=-300,
        actor=7,
        expected_chat_id=-100,
        language="ru",
    )
    await database.set_bot_user_role(
        actor_user_id=9,
        target_user_id=7,
        role="operator",
        expected_role="admin",
        fallback_role="admin",
    )
    with pytest.raises(ValueError):
        await database.select_notification_group(
            chat_id=-300,
            actor=7,
            expected_chat_id=-200,
            language="ru",
        )
    assert await database.get_notification_chat_id() == -200


@pytest.mark.asyncio
async def test_explicit_owner_and_database_admin_can_select_but_private_id_cannot(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(settings, "bot_owner_user_ids_raw", "9")
    monkeypatch.setattr(settings, "threads_oauth_admin_user_ids_raw", "")
    monkeypatch.setattr(settings, "threads_primary_chat_id", None)
    database = Database(tmp_path / "bot.db")
    await database.set_bot_user_role(
        actor_user_id=9,
        target_user_id=8,
        role="admin",
        expected_role=None,
        fallback_role=None,
    )
    for actor, previous, target in [(9, None, -100), (8, -100, -200)]:
        assert await database.select_notification_group(
            chat_id=target,
            actor=actor,
            expected_chat_id=previous,
            language="en",
        )
    with pytest.raises(ValueError):
        await database.select_notification_group(
            chat_id=8,
            actor=8,
            expected_chat_id=-200,
            language="en",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("pause", [False, True])
async def test_watcher_uses_destination_selected_during_search(
    tmp_path, monkeypatch, pause
):
    from unittest.mock import AsyncMock

    from bot.models import SearchResult, ThreadPost
    from bot.watcher import SearchWatcher

    monkeypatch.setattr(settings, "threads_oauth_admin_user_ids_raw", "7")
    database = Database(tmp_path / "bot.db")
    await connect(database)
    phrase_id, _ = await database.add_phrase("designer")
    await database.mark_phrase_initialized(phrase_id)
    post = ThreadPost(
        id="new-post",
        text="designer",
        username="author",
        permalink=None,
        timestamp=utc_now(),
        media_type="TEXT_POST",
        has_replies=False,
        is_reply=False,
        is_quote_post=False,
    )

    async def search(*args, **kwargs):
        if pause:
            await database.set_monitoring(-100, False)
        await database.select_notification_group(
            chat_id=-200,
            actor=7,
            expected_chat_id=-100,
            language="ru",
        )
        return SearchResult("designer", [post], 1)

    bot = AsyncMock()
    search_service = AsyncMock()
    search_service.search_by_keyword.side_effect = search
    token_manager = AsyncMock()
    token_manager.get_access_token.return_value = "test-token"
    watcher = SearchWatcher(
        bot,
        database=database,
        search_service=search_service,
        token_manager=token_manager,
    )
    result = await watcher.run_once()
    assert result.errors == 0
    if pause:
        bot.send_message.assert_not_awaited()
        assert not await database.is_post_seen(phrase_id, post.id)
    else:
        assert result.posts_sent == 1
        assert bot.send_message.call_args.args[0] == -200
        assert await database.is_post_notified(post.id)
