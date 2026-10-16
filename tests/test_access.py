from urllib.parse import parse_qs, urlparse

import pytest
from test_oauth_state_and_storage import build_service
from test_panel import app as app  # noqa: PLC0414 - shared pytest fixture

from bot.access import AccessDeniedError, AccessService
from bot.config import settings
from bot.storage import Database, utc_now
from bot.threads_oauth import InvalidOAuthStateError, ThreadsOAuthError


@pytest.fixture(autouse=True)
def configure_owner(monkeypatch):
    monkeypatch.setattr(settings, "bot_owner_user_ids_raw", "7")


async def change(app, target, role, *, expected=None):
    return await AccessService(app.db).set_role(
        actor=7, target=target, role=role, expected_role=expected
    )


@pytest.mark.asyncio
async def test_access_menu_is_only_for_owner_in_private_chat(app):
    await app.text("/menu")
    assert any(button.text == "Пользователи и доступ" for button in app.buttons)
    await app.text("/menu", user=8)
    assert not any(button.text == "Пользователи и доступ" for button in app.buttons)
    await app.text("/menu", group=True)
    assert not any(button.text == "Пользователи и доступ" for button in app.buttons)
    await app.click("access", group=True)
    assert app.session.requests[-1].show_alert


@pytest.mark.asyncio
async def test_owner_can_add_numeric_id_and_confirm_admin_access(app):
    await app.click("access")
    await app.click("access:add")
    for invalid in ("@username", "0", "-100", "³", str(2**52)):
        await app.text(invalid)
        assert "числовой Telegram ID" in app.last.text
    await app.text("9")
    assert "<code>9</code>" in app.last.text
    assert "Нет доступа" in app.last.text
    await app.click("access:role:9:admin")
    assert "Подтвердите изменение прав" in app.last.text
    assert await AccessService(app.db).role(9) is None
    await app.click("access:confirm")
    assert await AccessService(app.db).role(9) == "admin"
    assert "действуют сразу" in app.last.text
    await app.text("/menu", user=9)
    assert any(button.text == "Подключение Threads" for button in app.buttons)
    assert not any(button.text == "Пользователи и доступ" for button in app.buttons)
    await app.text("/connect_threads", user=9)
    app.auth.create_authorization_url.assert_awaited_once_with(
        telegram_user_id=9, target_chat_id=-100, language="ru"
    )


@pytest.mark.asyncio
async def test_operators_can_use_settings_but_not_threads_or_access(app):
    assert await change(app, 9, "operator")
    await app.text("/add query", user=9)
    assert (await app.db.list_phrases())[0]["phrase"] == "query"
    await app.text("/menu", user=9)
    assert not any(
        button.text in {"Подключение Threads", "Пользователи и доступ"}
        for button in app.buttons
    )
    await app.text("/connect_threads", user=9)
    assert "нет прав" in app.last.text
    await app.click("change_account", user=9, owner=9)
    await app.click("access:role:10:admin", user=9, owner=9)
    assert app.session.requests[-1].show_alert
    app.auth.create_authorization_url.assert_not_awaited()
    assert await AccessService(app.db).role(10) is None


@pytest.mark.asyncio
async def test_admin_cannot_manage_users_or_promote_self(app):
    assert await change(app, 9, "admin")
    await app.click("access", user=9, owner=9)
    assert app.session.requests[-1].show_alert
    with pytest.raises(AccessDeniedError):
        await AccessService(app.db).set_role(
            actor=9, target=10, role="admin", expected_role=None
        )
    with pytest.raises(AccessDeniedError):
        await AccessService(app.db).set_role(
            actor=7, target=9, role="owner", expected_role="admin"
        )


@pytest.mark.asyncio
async def test_owner_cannot_be_removed_even_by_another_owner(app, monkeypatch):
    monkeypatch.setattr(settings, "bot_owner_user_ids_raw", "7,8")
    await app.click("access:role:7:blocked")
    await app.click("access:confirm")
    assert await AccessService(app.db).role(7) == "owner"
    with pytest.raises(AccessDeniedError):
        await AccessService(app.db).set_role(
            actor=8, target=7, role="blocked", expected_role="owner"
        )


@pytest.mark.asyncio
async def test_revoke_overrides_legacy_allowlist_and_review_access(app):
    assert await AccessService(app.db).role(8) == "operator"
    assert await change(app, 8, "blocked", expected="operator")
    await app.text("/menu", user=8)
    assert "отозван" in app.last.text
    await app.text("/start review_example", user=8)
    assert "отозван" in app.last.text
    await app.click("notify:on", user=8, owner=8)
    assert app.session.requests[-1].show_alert
    assert not (await app.db.get_chat_settings(-100))["monitoring_enabled"]


@pytest.mark.asyncio
async def test_downgrade_takes_effect_for_legacy_oauth_admin(app, monkeypatch):
    monkeypatch.setattr(settings, "threads_oauth_admin_user_ids_raw", "7,9")
    assert await AccessService(app.db).role(9) == "admin"
    assert await change(app, 9, "operator", expected="admin")
    await app.text("/connect_threads", user=9)
    assert "нет прав" in app.last.text
    app.auth.create_authorization_url.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_stale_and_wrong_message_confirmations_cannot_grant(app):
    await app.click("access:role:9:admin")
    await app.click("access:user:9")
    await app.click("access:confirm")
    assert await AccessService(app.db).role(9) is None
    await app.click("access:role:9:admin")
    await app.click("access:confirm", message_id=999)
    assert await AccessService(app.db).role(9) is None
    await app.click("access:role:9:admin")
    assert await change(app, 9, "operator")
    await app.click("access:confirm")
    assert await AccessService(app.db).role(9) == "operator"
    assert "уже изменились" in app.last.text


@pytest.mark.asyncio
async def test_navigation_cancels_user_id_input(app):
    await app.click("access:add")
    await app.text("Меню")
    assert "Threads Monitor" in app.last.text
    await app.text("9")
    assert await AccessService(app.db).role(9) is None


@pytest.mark.asyncio
async def test_roles_survive_new_service_instances_and_are_audited(app):
    assert await change(app, 9, "admin")
    new_service = AccessService(Database(app.db._path))
    assert await new_service.role(9) == "admin"
    assert await change(app, 9, "blocked", expected="admin")
    assert await new_service.role(9) == "blocked"
    async with app.db.connection() as db:
        rows = await (
            await db.execute(
                "SELECT actor_user_id, target_user_id, previous_role, new_role FROM bot_access_audit ORDER BY id"
            )
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        (7, 9, None, "admin"),
        (7, 9, "admin", "blocked"),
    ]


@pytest.mark.asyncio
async def test_empty_access_configuration_does_not_open_bot_to_everyone(
    app, monkeypatch
):
    for name in (
        "allowed_user_ids_raw",
        "threads_oauth_admin_user_ids_raw",
        "bot_owner_user_ids_raw",
    ):
        monkeypatch.setattr(settings, name, "")
    await app.text("/menu", user=9)
    assert "нет доступа" in app.last.text


@pytest.mark.asyncio
async def test_revocation_invalidates_existing_primary_oauth_link(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "threads_oauth_admin_user_ids_raw", "101")
    db, _, auth = build_service(tmp_path)
    url = await auth.create_authorization_url(telegram_user_id=101, target_chat_id=-100)
    raw_state = parse_qs(urlparse(url).query)["state"][0]
    await AccessService(db).set_role(
        actor=7, target=101, role="blocked", expected_role="admin"
    )
    with pytest.raises(InvalidOAuthStateError):
        await auth.complete_authorization(
            raw_state=raw_state, code="authorization-code"
        )
    assert await db.get_active_threads_account() is None


@pytest.mark.asyncio
async def test_revocation_after_state_consumption_prevents_account_replacement(
    tmp_path, monkeypatch
):
    from datetime import timedelta

    monkeypatch.setattr(settings, "threads_oauth_admin_user_ids_raw", "101")
    db, cipher, auth = build_service(tmp_path)
    await db.upsert_threads_connection(
        threads_user_id="original",
        username="original",
        access_token_encrypted=cipher.encrypt("original-token"),
        token_type="bearer",
        expires_at=utc_now() + timedelta(days=30),
        connected_by_telegram_user_id=7,
        target_chat_id=-100,
    )
    url = await auth.create_authorization_url(telegram_user_id=101, target_chat_id=-100)
    state = await auth.consume_state(parse_qs(urlparse(url).query)["state"][0])
    await AccessService(db).set_role(
        actor=7, target=101, role="operator", expected_role="admin"
    )
    with pytest.raises(ThreadsOAuthError, match="revoked"):
        await auth.finish_authorization(state=state, code="authorization-code")
    assert (await db.get_active_threads_account())["threads_user_id"] == "original"
