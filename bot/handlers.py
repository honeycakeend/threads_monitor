import asyncio
import logging
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
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from bot.config import SearchType, settings
from bot.crypto import TokenCipher
from bot.i18n import (
    BOT_COMMANDS,
    HELP_TEXT,
    REVIEW_HELP_TEXT,
    Language,
    choose,
    language_from_code,
)
from bot.oauth_server import create_oauth_app
from bot.reviewer_access import REVIEW_START_PREFIX, ReviewerAccessService
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
    REVIEW_OAUTH_PURPOSE,
    ThreadsAuthService,
    ThreadsNotConnectedError,
    ThreadsOAuthClient,
    ThreadsOAuthError,
    ThreadsTokenManager,
    TokenRefreshWorker,
)
from bot.watcher import SearchWatcher

logger = logging.getLogger(__name__)

REVIEWER_COMMANDS = frozenset(
    {
        "start",
        "help",
        "connect_threads",
        "threads_status",
        "search",
        "disconnect_threads",
    }
)


class AccessMiddleware(BaseMiddleware):
    def __init__(
        self,
        reviewer_access: ReviewerAccessService,
        database: Database | None = None,
    ) -> None:
        self._reviewer_access = reviewer_access
        self._database = database

    async def _remember_language(self, event: TelegramObject) -> None:
        if self._database is None or not isinstance(event, Message):
            return
        user = event.from_user
        if user is None:
            return
        await self._database.set_chat_language(
            event.chat.id,
            language_from_code(user.language_code),
        )

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
            await self._remember_language(event)
            return await handler(event, data)

        user = getattr(event, "from_user", None)
        if user and user.id in allowed_users:
            data["reviewer_mode"] = False
            await self._remember_language(event)
            return await handler(event, data)

        if isinstance(event, Message) and user is not None:
            language = language_from_code(user.language_code)
            command, argument = _command_and_argument(event)
            if command == "start" and argument.startswith(REVIEW_START_PREFIX):
                data["reviewer_mode"] = False
                return await handler(event, data)
            if await self._reviewer_access.is_active(user.id):
                if command in REVIEWER_COMMANDS:
                    data["reviewer_mode"] = True
                    await self._remember_language(event)
                    return await handler(event, data)
                await event.answer(
                    choose(
                        language,
                        ru="Эта команда недоступна в режиме Meta App Review.\n\n",
                        en="This command is not available in Meta App Review mode.\n\n",
                    )
                    + REVIEW_HELP_TEXT[language]
                )
                return None
            await event.answer(
                choose(
                    language,
                    ru="У вас нет доступа к этому боту.",
                    en="You do not have access to this bot.",
                )
            )
        return None


def _command_and_argument(message: Message) -> tuple[str, str]:
    text = (message.text or "").strip()
    if not text.startswith("/"):
        return "", ""
    command_token, _, argument = text.partition(" ")
    command = command_token[1:].split("@", 1)[0].lower()
    return command, argument.strip()


def _message_language(message: Message) -> Language:
    return language_from_code(
        message.from_user.language_code if message.from_user is not None else None
    )


async def _remember_chat_language(
    message: Message,
    database: Database,
    language: Language,
) -> None:
    await database.set_chat_language(message.chat.id, language)


def _format_pool(phrases: list[dict], language: Language) -> str:
    lines = [
        choose(
            language,
            ru=f"<b>Пул фраз</b> ({len(phrases)}):",
            en=f"<b>Phrase pool</b> ({len(phrases)}):",
        )
    ]
    for item in phrases:
        lines.append(f"<b>{item['id']}.</b> {escape(item['phrase'])}")
    lines.append(
        choose(
            language,
            ru="\nУдалить: /remove &lt;id&gt; или /remove &lt;фраза&gt;",
            en="\nRemove: /remove &lt;id&gt; or /remove &lt;phrase&gt;",
        )
    )
    return "\n".join(lines)


async def _require_oauth_admin(message: Message) -> bool:
    language = _message_language(message)
    if message.chat.type != ChatType.PRIVATE:
        await message.answer(
            choose(
                language,
                ru="Эта команда доступна только в личном чате с ботом.",
                en="This command is only available in a private chat with the bot.",
            )
        )
        return False
    user = message.from_user
    if not settings.threads_oauth_admin_user_ids:
        await message.answer(
            choose(
                language,
                ru="Список администраторов OAuth не настроен на сервере.",
                en="The OAuth administrator list is not configured on the server.",
            )
        )
        return False
    if user is None or user.id not in settings.threads_oauth_admin_user_ids:
        await message.answer(
            choose(
                language,
                ru="У вас нет прав на управление подключением Threads.",
                en="You are not allowed to manage the Threads connection.",
            )
        )
        return False
    return True


def setup_handlers(
    dp: Dispatcher,
    *,
    search_service: SearchService,
    database: Database,
    watcher: SearchWatcher,
    threads_auth: ThreadsAuthService,
    reviewer_access: ReviewerAccessService | None = None,
) -> None:
    reviewer_access = reviewer_access or ReviewerAccessService(
        database=database,
        code_hash=settings.threads_review_access_code_hash,
        expires_at=settings.threads_review_access_expires_at,
    )

    @dp.message(Command("start"))
    async def cmd_start(
        message: Message,
        command: CommandObject,
        reviewer_mode: bool = False,
    ) -> None:
        language = _message_language(message)
        start_parameter = (command.args or "").strip()
        if start_parameter.startswith(REVIEW_START_PREFIX):
            if message.chat.type != ChatType.PRIVATE or message.from_user is None:
                await message.answer(
                    choose(
                        language,
                        ru="Доступ Meta App Review можно активировать только в личном чате.",
                        en="Meta App Review access can only be activated in a private chat.",
                    )
                )
                return
            redeemed = await reviewer_access.redeem_start_parameter(
                telegram_user_id=message.from_user.id,
                start_parameter=start_parameter,
            )
            if not redeemed:
                await message.answer(
                    choose(
                        language,
                        ru="Ссылка Meta App Review недействительна или истекла.",
                        en="This Meta App Review link is invalid or has expired.",
                    )
                )
                return
            await _remember_chat_language(message, database, language)
            await message.answer(REVIEW_HELP_TEXT[language])
            return
        if reviewer_mode:
            await message.answer(REVIEW_HELP_TEXT[language])
            return
        await _remember_chat_language(message, database, language)
        await database.set_monitoring(message.chat.id, True)
        await message.answer(
            choose(
                language,
                ru="Привет! Я мониторю Threads по фразам из общего пула и "
                "присылаю ссылки на новые посты.\n\n",
                en="Hi! I monitor Threads using the shared phrase pool and send "
                "links to new matching posts.\n\n",
            )
            + HELP_TEXT[language],
            disable_web_page_preview=True,
        )

    @dp.message(Command("help"))
    async def cmd_help(message: Message, reviewer_mode: bool = False) -> None:
        language = _message_language(message)
        await message.answer(
            REVIEW_HELP_TEXT[language] if reviewer_mode else HELP_TEXT[language],
            disable_web_page_preview=True,
        )

    @dp.message(Command("connect_threads"))
    async def cmd_connect_threads(
        message: Message,
        reviewer_mode: bool = False,
    ) -> None:
        language = _message_language(message)
        if reviewer_mode:
            if message.chat.type != ChatType.PRIVATE or message.from_user is None:
                await message.answer(
                    choose(
                        language,
                        ru="Эта команда доступна только в личном чате.",
                        en="This command is only available in a private chat.",
                    )
                )
                return
        elif not await _require_oauth_admin(message):
            return
        if not settings.threads_oauth_configured:
            await message.answer(
                choose(
                    language,
                    ru="OAuth Threads настроен не полностью. Проверьте переменные окружения.",
                    en="Threads OAuth is not fully configured. Check the server environment.",
                )
            )
            return
        if not reviewer_mode and settings.threads_primary_chat_id is None:
            await message.answer(
                choose(
                    language,
                    ru="THREADS_PRIMARY_CHAT_ID не настроен на сервере.",
                    en="THREADS_PRIMARY_CHAT_ID is not configured on the server.",
                )
            )
            return
        assert message.from_user is not None
        try:
            if reviewer_mode:
                url = await threads_auth.create_authorization_url(
                    telegram_user_id=message.from_user.id,
                    target_chat_id=message.chat.id,
                    purpose=REVIEW_OAUTH_PURPOSE,
                    language=language,
                )
            else:
                assert settings.threads_primary_chat_id is not None
                url = await threads_auth.create_authorization_url(
                    telegram_user_id=message.from_user.id,
                    target_chat_id=settings.threads_primary_chat_id,
                    language=language,
                )
        except ThreadsOAuthError as exc:
            await message.answer(
                choose(
                    language,
                    ru=f"Не удалось начать авторизацию: {escape(str(exc))}",
                    en=f"Could not start authorization: {escape(str(exc))}",
                )
            )
            return
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=choose(
                            language,
                            ru="Подключить Threads",
                            en="Connect Threads",
                        ),
                        url=url,
                    )
                ]
            ]
        )
        await message.answer(
            choose(
                language,
                ru="Нажмите кнопку и предоставьте threads_basic и "
                "threads_keyword_search. Одноразовая ссылка действует "
                f"{settings.threads_oauth_state_ttl_minutes} мин.",
                en="Open the button and grant threads_basic and "
                "threads_keyword_search. This one-time link is valid for "
                f"{settings.threads_oauth_state_ttl_minutes} minutes.",
            ),
            reply_markup=keyboard,
        )

    @dp.message(Command("threads_status"))
    async def cmd_threads_status(
        message: Message,
        reviewer_mode: bool = False,
    ) -> None:
        language = _message_language(message)
        if reviewer_mode:
            if message.from_user is None:
                return
            account = await threads_auth.review_status(message.from_user.id)
        else:
            if not await _require_oauth_admin(message):
                return
            account = await threads_auth.status()
        if account is None:
            await message.answer(
                choose(
                    language,
                    ru="Threads не подключён. Используйте /connect_threads",
                    en="Threads is not connected. Use /connect_threads",
                )
            )
            return
        unknown = choose(language, ru="неизвестно", en="unknown")
        username = escape(account.get("username") or unknown)
        expires_at = account.get("expires_at")
        expiry = expires_at.strftime("%Y-%m-%d %H:%M UTC") if expires_at else unknown
        if reviewer_mode:
            await message.answer(
                choose(
                    language,
                    ru="<b>Review-подключение Threads активно</b>\n"
                    f"Аккаунт: @{username}\n"
                    f"Токен действует до: {expiry}",
                    en="<b>Review Threads connection is active</b>\n"
                    f"Account: @{username}\n"
                    f"Token expires: {expiry}",
                )
            )
        else:
            await message.answer(
                choose(
                    language,
                    ru="<b>Threads подключён</b>\n"
                    f"Аккаунт: @{username}\n"
                    f"Threads user id: <code>{escape(account['threads_user_id'])}</code>\n"
                    f"Основной chat id: <code>{account.get('primary_chat_id')}</code>\n"
                    f"Токен действует до: {expiry}",
                    en="<b>Threads is connected</b>\n"
                    f"Account: @{username}\n"
                    f"Threads user id: <code>{escape(account['threads_user_id'])}</code>\n"
                    f"Primary chat id: <code>{account.get('primary_chat_id')}</code>\n"
                    f"Token expires: {expiry}",
                )
            )

    @dp.message(Command("disconnect_threads"))
    async def cmd_disconnect_threads(
        message: Message,
        reviewer_mode: bool = False,
    ) -> None:
        language = _message_language(message)
        if reviewer_mode:
            if message.from_user is None:
                return
            disconnected = await threads_auth.disconnect_review(message.from_user.id)
            await message.answer(
                choose(
                    language,
                    ru=(
                        "Review-подключение Threads удалено."
                        if disconnected
                        else "Review-подключение Threads уже отключено."
                    ),
                    en=(
                        "Review Threads connection deleted."
                        if disconnected
                        else "Review Threads is already disconnected."
                    ),
                )
            )
            return
        if not await _require_oauth_admin(message):
            return
        disconnected = await threads_auth.disconnect()
        if disconnected:
            await message.answer(
                choose(
                    language,
                    ru="Threads отключён. Зашифрованный токен удалён из активной записи.",
                    en="Threads is disconnected. The encrypted token has been deleted.",
                )
            )
        else:
            await message.answer(
                choose(
                    language,
                    ru="Threads уже отключён.",
                    en="Threads is already disconnected.",
                )
            )

    @dp.message(Command("search"))
    async def cmd_search(
        message: Message,
        command: CommandObject,
        reviewer_mode: bool = False,
    ) -> None:
        language = _message_language(message)
        args = (command.args or "").strip()
        if not args:
            await message.answer(
                choose(
                    language,
                    ru="Укажите запрос: /search python asyncio",
                    en="Enter a query: /search python asyncio",
                )
            )
            return

        search_type = SearchType.RECENT
        if args.lower().startswith("top "):
            search_type = SearchType.TOP
            args = args[4:].strip()

        if not args:
            await message.answer(
                choose(
                    language,
                    ru="Укажите фразу после команды.",
                    en="Enter a phrase after the command.",
                )
            )
            return

        access_token = None
        if reviewer_mode:
            if message.from_user is None:
                return
            try:
                access_token = await threads_auth.get_review_access_token(
                    message.from_user.id
                )
            except ThreadsNotConnectedError:
                await message.answer(
                    choose(
                        language,
                        ru="Сначала подключите тестовый Threads-аккаунт командой /connect_threads",
                        en="Connect a Threads test account first with /connect_threads",
                    )
                )
                return

        await _run_search(
            message,
            search_service,
            args,
            search_type=search_type,
            access_token=access_token,
            language=language,
        )

    @dp.message(Command("add"))
    async def cmd_add(message: Message, command: CommandObject) -> None:
        language = _message_language(message)
        await _remember_chat_language(message, database, language)
        raw = (command.args or "").strip()
        if not raw and message.text:
            lines = message.text.split("\n", 1)
            if len(lines) > 1:
                raw = lines[1].strip()

        phrases = split_phrases(raw)
        if not phrases:
            await message.answer(
                choose(
                    language,
                    ru="Укажите фразу для пула:\n"
                    "/add python asyncio\n"
                    "или\n"
                    "/add фраза1 | фраза2",
                    en="Enter a phrase for the pool:\n"
                    "/add python asyncio\n"
                    "or\n"
                    "/add phrase one | phrase two",
                )
            )
            return

        added = await database.add_phrases(phrases)
        await database.set_monitoring(message.chat.id, True)
        lines = [
            choose(
                language,
                ru="<b>Обновлён пул фраз:</b>",
                en="<b>Updated phrase pool:</b>",
            )
        ]
        for phrase_id, phrase, created in added:
            action = choose(
                language,
                ru="добавлена" if created else "обновлена",
                en="added" if created else "updated",
            )
            lines.append(f"{phrase_id}. {escape(phrase)} — {action}")
        lines.append(
            choose(
                language,
                ru="\nПри первой проверке текущие посты не придут — только новые после добавления.",
                en="\nThe first check initializes existing posts; only newer matches are notified.",
            )
        )
        await message.answer("\n".join(lines))

    @dp.message(Command("pool", "list"))
    async def cmd_pool(message: Message) -> None:
        language = _message_language(message)
        phrases = await database.list_phrases()
        if not phrases:
            await message.answer(
                choose(
                    language,
                    ru="Пул пуст. Добавьте фразы: /add python asyncio",
                    en="The pool is empty. Add phrases: /add python asyncio",
                )
            )
            return
        await message.answer(_format_pool(phrases, language))

    @dp.message(Command("remove", "delete", "del"))
    async def cmd_remove(message: Message, command: CommandObject) -> None:
        language = _message_language(message)
        target = (command.args or "").strip()
        if not target:
            await message.answer(
                choose(
                    language,
                    ru="Укажите id или текст фразы:\n"
                    "/remove 2\n"
                    "/remove startup idea",
                    en="Enter a phrase ID or text:\n"
                    "/remove 2\n"
                    "/remove startup idea",
                )
            )
            return

        if target.isdigit():
            phrase = await database.get_phrase(int(target))
            if phrase is None:
                await message.answer(
                    choose(
                        language,
                        ru=f"Фраза с id={target} не найдена.",
                        en=f"Phrase with id={target} was not found.",
                    )
                )
                return
            removed = await database.remove_phrase_by_id(int(target))
            deleted_text = phrase["phrase"]
        else:
            removed = await database.remove_phrase_by_text(target)
            deleted_text = target

        if removed:
            await message.answer(
                choose(
                    language,
                    ru=f'Фраза «{escape(deleted_text)}» удалена из пула.',
                    en=f'Phrase “{escape(deleted_text)}” was removed from the pool.',
                )
            )
        else:
            await message.answer(
                choose(
                    language,
                    ru=f'Фраза «{escape(target)}» не найдена в пуле.',
                    en=f'Phrase “{escape(target)}” was not found in the pool.',
                )
            )

    @dp.message(Command("monitor"))
    async def cmd_monitor(message: Message, command: CommandObject) -> None:
        language = _message_language(message)
        await _remember_chat_language(message, database, language)
        arg = (command.args or "").strip().lower()
        if arg not in {"on", "off"}:
            await message.answer(
                choose(
                    language,
                    ru="Использование: /monitor on или /monitor off",
                    en="Usage: /monitor on or /monitor off",
                )
            )
            return

        enabled = arg == "on"
        await database.set_monitoring(message.chat.id, enabled)
        if enabled:
            interval = await database.get_poll_interval_minutes()
            await message.answer(
                choose(
                    language,
                    ru="Уведомления включены для этого чата.\n"
                    f"Проверка пула каждые {interval} мин.",
                    en="Notifications are enabled for this chat.\n"
                    f"The pool is checked every {interval} minutes.",
                )
            )
        else:
            await message.answer(
                choose(
                    language,
                    ru="Уведомления выключены для этого чата.",
                    en="Notifications are disabled for this chat.",
                )
            )

    @dp.message(Command("interval"))
    async def cmd_interval(message: Message, command: CommandObject) -> None:
        language = _message_language(message)
        await _remember_chat_language(message, database, language)
        raw = (command.args or "").strip()
        phrases = await database.list_phrases()
        phrase_count = len(phrases)

        if not raw:
            interval = await database.get_poll_interval_minutes()
            await message.answer(
                format_interval_advice(
                    phrase_count,
                    interval,
                    language=language,
                )
            )
            return

        if not raw.isdigit():
            await message.answer(
                choose(
                    language,
                    ru=f"Укажите интервал в минутах "
                    f"({MIN_POLL_INTERVAL_MINUTES}–{MAX_POLL_INTERVAL_MINUTES}):\n"
                    "/interval 30",
                    en=f"Enter an interval in minutes "
                    f"({MIN_POLL_INTERVAL_MINUTES}–{MAX_POLL_INTERVAL_MINUTES}):\n"
                    "/interval 30",
                )
            )
            return

        minutes = int(raw)
        if not MIN_POLL_INTERVAL_MINUTES <= minutes <= MAX_POLL_INTERVAL_MINUTES:
            await message.answer(
                choose(
                    language,
                    ru=f"Интервал должен быть от {MIN_POLL_INTERVAL_MINUTES} до "
                    f"{MAX_POLL_INTERVAL_MINUTES} минут.",
                    en=f"The interval must be between {MIN_POLL_INTERVAL_MINUTES} and "
                    f"{MAX_POLL_INTERVAL_MINUTES} minutes.",
                )
            )
            return

        await database.set_poll_interval_minutes(minutes)
        await message.answer(
            choose(
                language,
                ru="Интервал обновлён.\n",
                en="Interval updated.\n",
            )
            + format_interval_advice(
                phrase_count,
                minutes,
                language=language,
            )
            + choose(
                language,
                ru="\n\nПрименится после текущего цикла проверки.",
                en="\n\nIt will apply after the current check cycle.",
            )
        )

    @dp.message(Command("status"))
    async def cmd_status(message: Message) -> None:
        language = _message_language(message)
        await _remember_chat_language(message, database, language)
        chat = await database.get_chat_settings(message.chat.id)
        phrases = await database.list_phrases()
        interval = await database.get_poll_interval_minutes()
        monitoring = choose(
            language,
            ru="включены" if chat["monitoring_enabled"] else "выключены",
            en="enabled" if chat["monitoring_enabled"] else "disabled",
        )
        threads_account = await threads_auth.status()
        await message.answer(
            choose(
                language,
                ru=f"<b>Статус</b>\n"
                f"Фраз в пуле: {len(phrases)}\n"
                f"Уведомления в этом чате: {monitoring}\n"
                f"Интервал проверки: {interval} мин\n"
                f"Threads API: "
                f"{'подключён' if threads_account else 'не подключён'}\n\n",
                en=f"<b>Status</b>\n"
                f"Phrases in pool: {len(phrases)}\n"
                f"Notifications in this chat: {monitoring}\n"
                f"Check interval: {interval} min\n"
                f"Threads API: "
                f"{'connected' if threads_account else 'not connected'}\n\n",
            )
            + format_interval_advice(
                len(phrases),
                interval,
                language=language,
            )
        )

    @dp.message(Command("run"))
    async def cmd_run(message: Message) -> None:
        language = _message_language(message)
        await _remember_chat_language(message, database, language)
        phrases = await database.list_phrases()
        if not phrases:
            await message.answer(
                choose(
                    language,
                    ru="Пул пуст. Добавьте фразы: /add python",
                    en="The pool is empty. Add phrases: /add python",
                )
            )
            return

        chat = await database.get_chat_settings(message.chat.id)
        if not chat["monitoring_enabled"]:
            await message.answer(
                choose(
                    language,
                    ru="Уведомления в этом чате выключены. Включите: /monitor on",
                    en="Notifications are disabled in this chat. Enable: /monitor on",
                )
            )
            return

        await message.answer(
            choose(
                language,
                ru="Запускаю проверку пула...",
                en="Checking the phrase pool...",
            )
        )
        stats = await watcher.run_once()
        if stats.not_connected:
            await message.answer(
                choose(
                    language,
                    ru="Threads не подключён или токен истёк. Администратор должен "
                    "выполнить /connect_threads в личном чате.",
                    en="Threads is disconnected or its token expired. An administrator "
                    "must run /connect_threads in a private chat.",
                )
            )
            return
        await message.answer(
            choose(
                language,
                ru=f"Готово.\n"
                f"Проверено фраз: {stats.phrases_checked}\n"
                f"Отправлено уведомлений: {stats.posts_sent}\n"
                f"Ошибок: {stats.errors}",
                en=f"Done.\n"
                f"Phrases checked: {stats.phrases_checked}\n"
                f"Notifications sent: {stats.posts_sent}\n"
                f"Errors: {stats.errors}",
            )
        )


async def _run_search(
    message: Message,
    search_service: SearchService,
    query: str,
    *,
    search_type: SearchType,
    access_token: str | None = None,
    language: Language,
) -> None:
    await message.answer(
        choose(
            language,
            ru=f'Ищу «{escape(query)}» ({search_type.value})...',
            en=f'Searching for “{escape(query)}” ({search_type.value})...',
        )
    )
    try:
        result = await search_service.search_by_keyword(
            query,
            search_type=search_type,
            access_token=access_token,
        )
    except (ThreadsAPIError, ThreadsNotConnectedError) as exc:
        await message.answer(
            choose(
                language,
                ru=f"Ошибка Threads API:\n{exc}",
                en=f"Threads API error:\n{exc}",
            )
        )
        return

    await _send_results(message, result, language=language)


async def _send_results(
    message: Message,
    result,
    *,
    language: Language,
) -> None:
    for chunk in format_search_result(result, language=language):
        await message.answer(chunk, disable_web_page_preview=True)


async def configure_bot_commands(bot: Bot) -> None:
    default_commands = [
        BotCommand(command=command, description=description)
        for command, description in BOT_COMMANDS["en"]
    ]
    russian_commands = [
        BotCommand(command=command, description=description)
        for command, description in BOT_COMMANDS["ru"]
    ]
    try:
        await bot.set_my_commands(default_commands)
        await bot.set_my_commands(russian_commands, language_code="ru")
    except Exception:  # noqa: BLE001 - menu localization must not stop the bot
        logger.warning("Could not update localized Telegram command descriptions")


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
    reviewer_access = ReviewerAccessService(
        database=db,
        code_hash=settings.threads_review_access_code_hash,
        expires_at=settings.threads_review_access_expires_at,
    )

    dp = Dispatcher()
    dp.message.middleware(AccessMiddleware(reviewer_access, database))
    setup_handlers(
        dp,
        search_service=search,
        database=db,
        watcher=watch,
        threads_auth=threads_auth,
        reviewer_access=reviewer_access,
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
    await configure_bot_commands(bot)
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
