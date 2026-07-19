from html import escape
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, TelegramObject

from bot.config import SearchType, settings
from bot.search_service import SearchService, format_search_result
from bot.storage import (
    Database,
    MAX_POLL_INTERVAL_MINUTES,
    MIN_POLL_INTERVAL_MINUTES,
    format_interval_advice,
    split_phrases,
)
from bot.threads_client import ThreadsAPIError
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
        if not settings.allowed_user_ids:
            return await handler(event, data)

        user = getattr(event, "from_user", None)
        if user and user.id in settings.allowed_user_ids:
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


def setup_handlers(
    dp: Dispatcher,
    *,
    search_service: SearchService,
    database: Database,
    watcher: SearchWatcher,
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
            await message.answer(f'Фраза «{deleted_text}» удалена из пула.')
        else:
            await message.answer(f'Фраза «{target}» не найдена в пуле.')

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
        await message.answer(
            f"<b>Статус</b>\n"
            f"Фраз в пуле: {len(phrases)}\n"
            f"Уведомления в этом чате: {monitoring}\n"
            f"Интервал проверки: {interval} мин\n"
            f"Threads API: {'ok' if settings.threads_configured else 'нет токена'}\n\n"
            + format_interval_advice(len(phrases), interval)
        )

    @dp.message(Command("run"))
    async def cmd_run(message: Message) -> None:
        if not settings.threads_configured:
            await message.answer("THREADS_ACCESS_TOKEN не настроен в .env")
            return

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
    await message.answer(f'Ищу «{query}» ({search_type.value})...')
    try:
        result = await search_service.search_by_keyword(query, search_type=search_type)
    except ThreadsAPIError as exc:
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
    bot: Bot | None = None,
) -> Dispatcher:
    db = database or Database()
    search = search_service or SearchService()
    if bot is None:
        raise ValueError("bot is required to create dispatcher with watcher")
    watch = watcher or SearchWatcher(bot, database=db, search_service=search)

    dp = Dispatcher()
    dp.message.middleware(AccessMiddleware())
    setup_handlers(dp, search_service=search, database=db, watcher=watch)
    return dp


async def run_bot() -> None:
    if not settings.telegram_configured:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not configured. Copy .env.example to .env and fill it in."
        )

    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    database = Database()
    search_service = SearchService()
    watcher = SearchWatcher(bot, database=database, search_service=search_service)
    dp = create_dispatcher(
        search_service=search_service,
        database=database,
        watcher=watcher,
        bot=bot,
    )

    await watcher.start()
    try:
        await dp.start_polling(bot)
    finally:
        await watcher.stop()
