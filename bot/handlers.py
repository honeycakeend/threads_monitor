import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from html import escape
from typing import Any

import httpx
import uvicorn
from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from bot.config import SearchType, settings
from bot.crypto import TokenCipher
from bot.oauth_server import create_oauth_app
from bot.search_service import SearchService, format_search_result
from bot.storage import (
    MAX_POLL_INTERVAL_MINUTES,
    MIN_POLL_INTERVAL_MINUTES,
    Database,
    format_interval_advice,
    split_phrases,
)
from bot.threads_client import ThreadsAPIError, ThreadsClient
from bot.threads_oauth import (
    ThreadsAuthService,
    ThreadsNotConnectedError,
    ThreadsOAuthClient,
    ThreadsOAuthError,
    ThreadsTokenManager,
    TokenRefreshWorker,
)
from bot.watcher import SearchWatcher

HELP_TEXT = """
<b>Threads Monitor Bot</b>

<b>Пул фраз</b> (общая база для всех пользователей бота)
/add &lt;фраза&gt; — добавить фразу в пул
/add фраза1 | фраза2 — несколько фраз сразу
/pool — посмотреть весь пул
/list — то же, что /pool
/remove &lt;id&gt; — удалить фразу по номеру
/remove &lt;текст фразы&gt; — удалить по тексту

<b>Мониторинг</b>
/monitor on — включить уведомления в этот чат
/monitor off — выключить уведомления
/interval — текущий интервал и расход лимита
/interval 30 — интервал проверки в минутах
/run — проверить пул прямо сейчас
/status — статус

<b>Разовый поиск</b>
/search &lt;фраза&gt; — поиск один раз без сохранения

<b>Подключение Threads (администратор, личный чат)</b>
/connect_threads — безопасно подключить аккаунт через OAuth
/threads_status — статус подключения
/disconnect_threads — удалить сохранённый токен и отключить аккаунт

<b>Примеры</b>
/add python asyncio
/add startup idea | marketing threads
/pool
/remove 2
/remove startup idea
/monitor on
/interval 30
""".strip()


class AccessMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        allowed_users = set(settings.allowed_user_ids) | set(
            settings.threads_oauth_admin_user_ids
        )
        if not allowed_users:
            return await handler(event, data)

        user = getattr(event, "from_user", None)
        if user and user.id in allowed_users:
            return await handler(event, data)

        if isinstance(event, Message):
            await event.answer("У вас нет доступа к этому боту.")
        return None


def _format_pool(phrases: list[dict]) -> str:
    lines = [f"<b>Пул фраз</b> ({len(phrases)}):"]
    for item in phrases:
        lines.append(f"<b>{item['id']}.</b> {escape(item['phrase'])}")
    lines.append("\nУдалить: /remove &lt;id&gt; или /remove &lt;фраза&gt;")
    return "\n".join(lines)


async def _require_oauth_admin(message: Message) -> bool:
    if message.chat.type != ChatType.PRIVATE:
        await message.answer("Эта команда доступна только в личном чате с ботом.")
        return False
    user = message.from_user
    if not settings.threads_oauth_admin_user_ids:
        await message.answer("Список администраторов OAuth не настроен на сервере.")
        return False
    if user is None or user.id not in settings.threads_oauth_admin_user_ids:
        await message.answer("У вас нет прав на управление подключением Threads.")
        return False
    return True


def setup_handlers(
    dp: Dispatcher,
    *,
    search_service: SearchService,
    database: Database,
    watcher: SearchWatcher,
    threads_auth: ThreadsAuthService,
) -> None:
    @dp.message(Command("start"))
    async def cmd_start(message: Message) -> None:
        await database.set_monitoring(message.chat.id, True)
        await message.answer(
            "Привет! Я мониторю Threads по фразам из общего пула и присылаю ссылки на новые посты.\n\n"
            + HELP_TEXT,
            disable_web_page_preview=True,
        )

    @dp.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        await message.answer(HELP_TEXT, disable_web_page_preview=True)

    @dp.message(Command("connect_threads"))
    async def cmd_connect_threads(message: Message) -> None:
        if not await _require_oauth_admin(message):
            return
        if not settings.threads_oauth_configured:
            await message.answer(
                "OAuth Threads настроен не полностью. Проверьте переменные окружения."
            )
            return
        if settings.threads_primary_chat_id is None:
            await message.answer("THREADS_PRIMARY_CHAT_ID не настроен на сервере.")
            return
        assert message.from_user is not None
        try:
            url = await threads_auth.create_authorization_url(
                telegram_user_id=message.from_user.id,
                target_chat_id=settings.threads_primary_chat_id,
            )
        except ThreadsOAuthError as exc:
            await message.answer(f"Не удалось начать авторизацию: {escape(str(exc))}")
            return
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Подключить Threads", url=url)]
            ]
        )
        await message.answer(
            "Нажмите кнопку и подтвердите доступ в Threads. Ссылка одноразовая "
            f"и действует {settings.threads_oauth_state_ttl_minutes} мин.",
            reply_markup=keyboard,
        )

    @dp.message(Command("threads_status"))
    async def cmd_threads_status(message: Message) -> None:
        if not await _require_oauth_admin(message):
            return
        account = await threads_auth.status()
        if account is None:
            await message.answer("Threads не подключён. Используйте /connect_threads")
            return
        username = escape(account.get("username") or "неизвестно")
        expires_at = account.get("expires_at")
        expiry = expires_at.strftime("%Y-%m-%d %H:%M UTC") if expires_at else "неизвестно"
        await message.answer(
            "<b>Threads подключён</b>\n"
            f"Аккаунт: @{username}\n"
            f"Threads user id: <code>{escape(account['threads_user_id'])}</code>\n"
            f"Основной chat id: <code>{account.get('primary_chat_id')}</code>\n"
            f"Токен действует до: {expiry}"
        )

    @dp.message(Command("disconnect_threads"))
    async def cmd_disconnect_threads(message: Message) -> None:
        if not await _require_oauth_admin(message):
            return
        disconnected = await threads_auth.disconnect()
        if disconnected:
            await message.answer(
                "Threads отключён. Зашифрованный токен удалён из активной записи."
            )
        else:
            await message.answer("Threads уже отключён.")

    @dp.message(Command("search"))
    async def cmd_search(message: Message, command: CommandObject) -> None:
        args = (command.args or "").strip()
        if not args:
            await message.answer("Укажите запрос: /search python asyncio")
            return

        search_type = SearchType.RECENT
        if args.lower().startswith("top "):
            search_type = SearchType.TOP
            args = args[4:].strip()

        if not args:
            await message.answer("Укажите фразу после команды.")
            return

        await _run_search(message, search_service, args, search_type=search_type)

    @dp.message(Command("add"))
    async def cmd_add(message: Message, command: CommandObject) -> None:
        raw = (command.args or "").strip()
        if not raw and message.text:
            lines = message.text.split("\n", 1)
            if len(lines) > 1:
                raw = lines[1].strip()

        phrases = split_phrases(raw)
        if not phrases:
            await message.answer(
                "Укажите фразу для пула:\n"
                "/add python asyncio\n"
                "или\n"
                "/add фраза1 | фраза2"
            )
            return

        added = await database.add_phrases(phrases)
        await database.set_monitoring(message.chat.id, True)
        lines = ["<b>Обновлён пул фраз:</b>"]
        for phrase_id, phrase, created in added:
            action = "добавлена" if created else "обновлена"
            lines.append(f"{phrase_id}. {escape(phrase)} — {action}")
        lines.append(
            "\nПри первой проверке текущие посты не придут — только новые после добавления."
        )
        await message.answer("\n".join(lines))

    @dp.message(Command("pool", "list"))
    async def cmd_pool(message: Message) -> None:
        phrases = await database.list_phrases()
        if not phrases:
            await message.answer("Пул пуст. Добавьте фразы: /add python asyncio")
            return
        await message.answer(_format_pool(phrases))

    @dp.message(Command("remove", "delete", "del"))
    async def cmd_remove(message: Message, command: CommandObject) -> None:
        target = (command.args or "").strip()
        if not target:
            await message.answer(
                "Укажите id или текст фразы:\n"
                "/remove 2\n"
                "/remove startup idea"
            )
            return

        if target.isdigit():
            phrase = await database.get_phrase(int(target))
            if phrase is None:
                await message.answer(f"Фраза с id={target} не найдена.")
                return
            removed = await database.remove_phrase_by_id(int(target))
            deleted_text = phrase["phrase"]
        else:
            removed = await database.remove_phrase_by_text(target)
            deleted_text = target

        if removed:
            await message.answer(f'Фраза «{escape(deleted_text)}» удалена из пула.')
        else:
            await message.answer(f'Фраза «{escape(target)}» не найдена в пуле.')

    @dp.message(Command("monitor"))
    async def cmd_monitor(message: Message, command: CommandObject) -> None:
        arg = (command.args or "").strip().lower()
        if arg not in {"on", "off"}:
            await message.answer("Использование: /monitor on или /monitor off")
            return

        enabled = arg == "on"
        await database.set_monitoring(message.chat.id, enabled)
        if enabled:
            interval = await database.get_poll_interval_minutes()
            await message.answer(
                f"Уведомления включены для этого чата.\n"
                f"Проверка пула каждые {interval} мин."
            )
        else:
            await message.answer("Уведомления выключены для этого чата.")

    @dp.message(Command("interval"))
    async def cmd_interval(message: Message, command: CommandObject) -> None:
        raw = (command.args or "").strip()
        phrases = await database.list_phrases()
        phrase_count = len(phrases)

        if not raw:
            interval = await database.get_poll_interval_minutes()
            await message.answer(format_interval_advice(phrase_count, interval))
            return

        if not raw.isdigit():
            await message.answer(
                f"Укажите интервал в минутах ({MIN_POLL_INTERVAL_MINUTES}–{MAX_POLL_INTERVAL_MINUTES}):\n"
                "/interval 30"
            )
            return

        minutes = int(raw)
        if not MIN_POLL_INTERVAL_MINUTES <= minutes <= MAX_POLL_INTERVAL_MINUTES:
            await message.answer(
                f"Интервал должен быть от {MIN_POLL_INTERVAL_MINUTES} до "
                f"{MAX_POLL_INTERVAL_MINUTES} минут."
            )
            return

        await database.set_poll_interval_minutes(minutes)
        await message.answer(
            "Интервал обновлён.\n"
            + format_interval_advice(phrase_count, minutes)
            + "\n\nПрименится после текущего цикла проверки."
        )

    @dp.message(Command("status"))
    async def cmd_status(message: Message) -> None:
        chat = await database.get_chat_settings(message.chat.id)
        phrases = await database.list_phrases()
        interval = await database.get_poll_interval_minutes()
        monitoring = "включены" if chat["monitoring_enabled"] else "выключены"
        threads_account = await threads_auth.status()
        await message.answer(
            f"<b>Статус</b>\n"
            f"Фраз в пуле: {len(phrases)}\n"
            f"Уведомления в этом чате: {monitoring}\n"
            f"Интервал проверки: {interval} мин\n"
            f"Threads API: {'подключён' if threads_account else 'не подключён'}\n\n"
            + format_interval_advice(len(phrases), interval)
        )

    @dp.message(Command("run"))
    async def cmd_run(message: Message) -> None:
        phrases = await database.list_phrases()
        if not phrases:
            await message.answer("Пул пуст. Добавьте фразы: /add python")
            return

        chat = await database.get_chat_settings(message.chat.id)
        if not chat["monitoring_enabled"]:
            await message.answer(
                "Уведомления в этом чате выключены. Включите: /monitor on"
            )
            return

        await message.answer("Запускаю проверку пула...")
        stats = await watcher.run_once()
        if stats.not_connected:
            await message.answer(
                "Threads не подключён или токен истёк. Администратор должен выполнить "
                "/connect_threads в личном чате."
            )
            return
        await message.answer(
            f"Готово.\n"
            f"Проверено фраз: {stats.phrases_checked}\n"
            f"Отправлено уведомлений: {stats.posts_sent}\n"
            f"Ошибок: {stats.errors}"
        )


async def _run_search(
    message: Message,
    search_service: SearchService,
    query: str,
    *,
    search_type: SearchType,
) -> None:
    await message.answer(f'Ищу «{escape(query)}» ({search_type.value})...')
    try:
        result = await search_service.search_by_keyword(query, search_type=search_type)
    except (ThreadsAPIError, ThreadsNotConnectedError) as exc:
        await message.answer(f"Ошибка Threads API:\n{exc}")
        return

    await _send_results(message, result)


async def _send_results(message: Message, result) -> None:
    for chunk in format_search_result(result):
        await message.answer(chunk, disable_web_page_preview=True)


def create_dispatcher(
    *,
    search_service: SearchService | None = None,
    database: Database | None = None,
    watcher: SearchWatcher | None = None,
    threads_auth: ThreadsAuthService,
    token_manager: ThreadsTokenManager,
    bot: Bot | None = None,
) -> Dispatcher:
    db = database or Database()
    search = search_service or SearchService(token_manager=token_manager)
    if bot is None:
        raise ValueError("bot is required to create dispatcher with watcher")
    watch = watcher or SearchWatcher(
        bot,
        database=db,
        search_service=search,
        token_manager=token_manager,
    )

    dp = Dispatcher()
    dp.message.middleware(AccessMiddleware())
    setup_handlers(
        dp,
        search_service=search,
        database=db,
        watcher=watch,
        threads_auth=threads_auth,
    )
    return dp


async def run_bot() -> None:
    if not settings.telegram_configured:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not configured. Copy .env.example to .env and fill it in."
        )

    if not settings.threads_oauth_configured:
        raise RuntimeError(
            "Threads OAuth is not configured. Set THREADS_APP_ID, THREADS_APP_SECRET, "
            "THREADS_TOKEN_ENCRYPTION_KEY and THREADS_REDIRECT_URI."
        )

    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    database = Database()
    cipher = TokenCipher(settings.threads_token_encryption_key)

    async with httpx.AsyncClient(timeout=30.0) as http_client:
        oauth_client = ThreadsOAuthClient(
            app_id=settings.threads_app_id,
            app_secret=settings.threads_app_secret,
            redirect_uri=settings.threads_redirect_uri,
            graph_base=settings.threads_graph_base,
            http_client=http_client,
        )
        threads_auth = ThreadsAuthService(
            database=database,
            cipher=cipher,
            oauth_client=oauth_client,
            app_id=settings.threads_app_id,
            redirect_uri=settings.threads_redirect_uri,
            authorize_url=settings.threads_oauth_authorize_url,
            state_ttl_minutes=settings.threads_oauth_state_ttl_minutes,
        )
        token_manager = ThreadsTokenManager(
            database=database,
            cipher=cipher,
            oauth_client=oauth_client,
            refresh_before=timedelta(days=settings.threads_token_refresh_before_days),
        )
        search_service = SearchService(
            client=ThreadsClient(
                base_url=settings.threads_api_base,
                http_client=http_client,
            ),
            token_manager=token_manager,
        )
        watcher = SearchWatcher(
            bot,
            database=database,
            search_service=search_service,
            token_manager=token_manager,
        )
        refresh_worker = TokenRefreshWorker(
            token_manager,
            check_interval=timedelta(
                hours=settings.threads_token_refresh_check_hours
            ),
        )
        dp = create_dispatcher(
            search_service=search_service,
            database=database,
            watcher=watcher,
            threads_auth=threads_auth,
            token_manager=token_manager,
            bot=bot,
        )
        oauth_app = create_oauth_app(
            database=database,
            threads_auth=threads_auth,
            bot=bot,
            app_secret=settings.threads_app_secret,
            public_base_url=settings.public_base_url,
        )
        server = uvicorn.Server(
            uvicorn.Config(
                oauth_app,
                host=settings.oauth_server_host,
                port=settings.oauth_server_port,
                log_level="info",
                access_log=False,
                proxy_headers=True,
                forwarded_allow_ips="127.0.0.1",
            )
        )

        await watcher.start()
        await refresh_worker.start()
        bot_task = asyncio.create_task(dp.start_polling(bot), name="telegram-polling")
        server_task = asyncio.create_task(server.serve(), name="oauth-http-server")
        tasks = {bot_task, server_task}
        try:
            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                exception = task.exception()
                if exception is not None:
                    raise exception
            for task in pending:
                task.cancel()
        finally:
            server.should_exit = True
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await refresh_worker.stop()
            await watcher.stop()
            await bot.session.close()
