"""Telegram settings screens and per-user input flows."""

from collections.abc import Awaitable, Callable
from html import escape

from aiogram import Dispatcher, F
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from bot.config import SearchType, settings
from bot.i18n import (
    INSTRUCTIONS,
    REVIEW_HELP_TEXT,
    Language,
    choose,
    language_from_code,
)
from bot.search_service import SearchService, format_search_result
from bot.storage import (
    MAX_POLL_INTERVAL_MINUTES,
    MIN_POLL_INTERVAL_MINUTES,
    Database,
    format_interval_advice,
    split_phrases,
    utc_now,
)
from bot.threads_client import ThreadsAPIError
from bot.threads_oauth import ThreadsAuthService, ThreadsNotConnectedError
from bot.watcher import SearchWatcher


class PanelInput(StatesGroup):
    phrases = State()
    interval = State()
    search = State()
    search_type = State()
    disconnect = State()


LABELS = {
    "home": ("← В меню", "← Menu"),
    "phrases": ("Фразы для отслеживания", "Tracked phrases"),
    "add": ("+ Добавить фразы", "+ Add phrases"),
    "interval": ("Частота проверки", "Check frequency"),
    "custom": ("Указать свой интервал", "Custom interval"),
    "run": ("Проверить сейчас", "Check now"),
    "search": ("Найти публикации", "Find posts"),
    "account": ("Подключение Threads", "Threads connection"),
    "connect": ("Подключить Threads", "Connect Threads"),
    "change_account": ("Сменить аккаунт", "Change account"),
    "disconnect": ("Отключить аккаунт", "Disconnect account"),
    "confirm_disconnect": ("Да, отключить", "Yes, disconnect"),
    "instructions": ("Инструкция", "Instructions"),
    "cancel": ("Отмена", "Cancel"),
    "recent": ("Последние", "Recent"),
    "top": ("Популярные", "Popular"),
}


def label(key: str, language: Language) -> str:
    return LABELS[key][0 if language == "ru" else 1]


def keyboard(owner: int, rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=text, callback_data=f"ui:{owner}:{action}")
                for text, action in row
            ]
            for row in rows
        ]
    )


def is_admin(message: Message) -> bool:
    return (
        message.chat.type == ChatType.PRIVATE
        and message.from_user is not None
        and message.from_user.id in settings.threads_oauth_admin_user_ids
    )


def navigation_keyboard(
    language: Language, *, group: bool = False
) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=choose(language, ru="Меню", en="Menu")),
                KeyboardButton(text=label("instructions", language)),
            ]
        ],
        is_persistent=True,
        resize_keyboard=True,
        one_time_keyboard=False,
        # Bot API 10.3: reply to this bot so group privacy mode can deliver
        # text-button messages. Older aiogram versions serialize extra fields.
        **({"force_reply": True} if group else {}),
    )


async def send_navigation(message: Message, language: Language) -> None:
    await message.answer(
        choose(
            language,
            ru="Настройки и инструкция — на кнопках внизу чата.",
            en="Use the buttons below for settings and instructions.",
        ),
        reply_markup=navigation_keyboard(
            language, group=message.chat.type != ChatType.PRIVATE
        ),
    )


def connection_key(account: dict | None) -> str:
    """Invalidate an old confirmation after reconnecting or changing accounts."""
    if not account:
        return ""
    return str((account.get("id"), account.get("username"), account.get("expires_at")))


async def edit_screen(
    message: Message, text: str, markup: InlineKeyboardMarkup
) -> None:
    try:
        await message.edit_text(
            text, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in exc.message.lower():
            raise


class SettingsPanel:
    PAGE_SIZE = 8

    def __init__(
        self,
        database: Database,
        threads_auth: ThreadsAuthService,
    ) -> None:
        self.db = database
        self.auth = threads_auth

    async def target(self) -> tuple[dict | None, int | None]:
        account = await self.auth.status()
        target = account.get("primary_chat_id") if account else None
        return (
            account,
            target if target is not None else settings.threads_primary_chat_id,
        )

    async def home(self, message: Message, language: Language):
        account, target = await self.target()
        phrases = await self.db.list_phrases()
        interval = await self.db.get_poll_interval_minutes()
        enabled = bool(
            target is not None
            and (await self.db.get_chat_settings(target))["monitoring_enabled"]
        )
        expired = bool(
            account and account.get("expires_at") and account["expires_at"] <= utc_now()
        )
        ready = bool(
            account and not expired and phrases and enabled and target is not None
        )
        if not account or expired:
            status = choose(language, ru="Подключите Threads", en="Connect Threads")
            next_step = choose(
                language,
                ru="Откройте «Подключение Threads». Подключить аккаунт может администратор в личном чате.",
                en="Open Threads connection. An administrator connects the account in a private chat.",
            )
        elif not phrases:
            status = choose(
                language, ru="Добавьте первую фразу", en="Add your first phrase"
            )
            next_step = choose(
                language,
                ru="Откройте «Фразы для отслеживания» и укажите, что искать.",
                en="Open Tracked phrases and enter what to search for.",
            )
        elif target is None:
            status = choose(
                language, ru="Рабочий чат не настроен", en="Work chat is not configured"
            )
            next_step = choose(
                language,
                ru="Попросите администратора настроить чат уведомлений.",
                en="Ask your administrator to configure the notification chat.",
            )
        elif not enabled:
            status = choose(
                language, ru="Уведомления приостановлены", en="Notifications paused"
            )
            next_step = choose(
                language,
                ru="Включите уведомления, чтобы получать новые совпадения.",
                en="Enable notifications to receive new matches.",
            )
        else:
            status = choose(language, ru="Готов к работе", en="Ready")
            next_step = choose(
                language,
                ru="Новые совпадения будут приходить в рабочий чат.",
                en="New matches will arrive in your work chat.",
            )
        recipient = choose(language, ru="не настроен", en="not configured")
        if target is not None:
            recipient = (
                choose(language, ru="этот чат", en="this chat")
                if target == message.chat.id
                else str(target)
            )
            if target != message.chat.id:
                try:
                    chat = await message.bot.get_chat(target)
                    recipient = chat.title or chat.full_name or str(target)
                except (TelegramBadRequest, TelegramForbiddenError):
                    pass
        username = (
            "@" + account["username"]
            if account and account.get("username")
            else choose(
                language,
                ru="подключён" if account else "не подключён",
                en="connected" if account else "not connected",
            )
        )
        text = choose(
            language,
            ru=f"<b>Threads Monitor</b>\n\n{'🟢' if ready else '⚪'} <b>{status}</b>\n\nАккаунт Threads: {escape(username)}\nФраз для отслеживания: {len(phrases)}\nПроверка: каждые {interval} мин\nУведомления: {'включены' if enabled else 'приостановлены'}\nПолучатель: {escape(recipient)}\n\n{next_step}",
            en=f"<b>Threads Monitor</b>\n\n{'🟢' if ready else '⚪'} <b>{status}</b>\n\nThreads account: {escape(username)}\nTracked phrases: {len(phrases)}\nCheck: every {interval} min\nNotifications: {'enabled' if enabled else 'paused'}\nRecipient: {escape(recipient)}\n\n{next_step}",
        )
        rows = [[(label(key, language), key)] for key in ("phrases", "interval")]
        rows.append(
            [
                (
                    choose(
                        language,
                        ru="Приостановить уведомления"
                        if enabled
                        else "Включить уведомления",
                        en="Pause notifications" if enabled else "Enable notifications",
                    ),
                    "notify:off" if enabled else "notify:on",
                )
            ]
        )
        rows.extend([[(label(key, language), key)] for key in ("run", "search")])
        if is_admin(message):
            rows.append([(label("account", language), "account")])
        rows.append([(label("instructions", language), "instructions")])
        return text, rows

    async def phrases(self, language: Language, page: int = 0, notice: str = ""):
        phrases = await self.db.list_phrases()
        page = max(0, min(page, max(0, (len(phrases) - 1) // self.PAGE_SIZE)))
        visible = phrases[page * self.PAGE_SIZE : (page + 1) * self.PAGE_SIZE]
        text = f"<b>{label('phrases', language)}</b> · {len(phrases)}\n\n"
        if notice:
            text += notice + "\n\n"
        text += (
            choose(
                language,
                ru="Список общий для команды. Чтобы удалить фразу, нажмите × рядом с ней.",
                en="This list is shared by the team. Tap × next to a phrase to remove it.",
            )
            if phrases
            else choose(
                language,
                ru="Пока здесь пусто. Добавьте слова или фразы, которые нужно искать в Threads.",
                en="No phrases yet. Add words or phrases to search for in Threads.",
            )
        )
        rows = [[(label("add", language), "add")]]
        for item in visible:
            # Keep both button labels and the message within Telegram limits.
            text += f"\n\n• {escape(item['phrase'][:180])}"
            rows.append([(f"× {item['phrase'][:45]}", f"delete:{item['id']}:{page}")])
        nav = []
        if page:
            nav.append(("←", f"phrases:{page - 1}"))
        if (page + 1) * self.PAGE_SIZE < len(phrases):
            nav.append(("→", f"phrases:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([(label("home", language), "home")])
        return text, rows

    async def interval(self, language: Language, notice: str = ""):
        current = await self.db.get_poll_interval_minutes()
        count = len(await self.db.list_phrases())
        text = f"<b>{label('interval', language)}</b>\n\n"
        if notice:
            text += notice + "\n\n"
        text += format_interval_advice(count, current, language=language)
        text += choose(
            language,
            ru="\n\nЧем чаще проверка, тем больше запросов. Расчёт примерный, без разового поиска. Настройка общая для команды.",
            en="\n\nMore frequent checks use more requests. This estimate excludes one-time searches. This setting is shared by the team.",
        )
        rows = [
            [
                (
                    f"{'✓ ' if current == value else ''}{value} "
                    + choose(language, ru="мин", en="min"),
                    f"every:{value}",
                )
                for value in values
            ]
            for values in ((15, 30, 60), (120, 240))
        ]
        rows.extend(
            [
                [(label("custom", language), "custom")],
                [(label("home", language), "home")],
            ]
        )
        return text, rows

    async def account(self, language: Language):
        account = await self.auth.status()
        text = f"<b>{label('account', language)}</b>\n\n"
        if account:
            username = escape(account.get("username") or "—")
            expiry = account.get("expires_at")
            expired = bool(expiry and expiry <= utc_now())
            text += choose(
                language,
                ru=f"Аккаунт: @{username}\n"
                + (
                    "Подключение истекло. Подключите аккаунт снова."
                    if expired
                    else "Аккаунт подключён."
                ),
                en=f"Account: @{username}\n"
                + (
                    "Connection expired. Connect the account again."
                    if expired
                    else "Account connected."
                ),
            )
            rows = [[(label("connect", language), "connect")]] if expired else []
            rows.append([(label("change_account", language), "change_account")])
            rows.append([(label("disconnect", language), "disconnect")])
        else:
            text += choose(
                language,
                ru="Подключите аккаунт, чтобы искать публикации и получать уведомления. Кнопка откроет официальную страницу авторизации Threads.",
                en="Connect an account to search posts and receive notifications. The button opens the official Threads authorization page.",
            )
            rows = [[(label("connect", language), "connect")]]
        rows.append([(label("home", language), "home")])
        return text, rows


def setup_panel_handlers(
    dp: Dispatcher,
    *,
    database: Database,
    threads_auth: ThreadsAuthService,
    search_service: SearchService,
    watcher: SearchWatcher,
    connect: Callable[..., Awaitable[bool]],
) -> SettingsPanel:
    panel = SettingsPanel(database, threads_auth)

    async def show(message: Message, owner: int, screen, *, edit: bool = True):
        text, rows = screen
        markup = keyboard(owner, rows)
        if edit:
            await edit_screen(message, text, markup)
        else:
            await message.answer(
                text,
                reply_markup=markup,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

    @dp.message(Command("menu", "settings", "cancel"))
    @dp.message(
        F.text.strip()
        .casefold()
        .in_({"меню", "настройки", "menu", "settings", "отмена", "cancel"})
    )
    async def open_menu(
        message: Message, state: FSMContext, reviewer_mode: bool = False
    ):
        await state.clear()
        language = language_from_code(message.from_user.language_code)
        if reviewer_mode:
            await message.answer(REVIEW_HELP_TEXT[language])
            return
        await send_navigation(message, language)
        await show(
            message,
            message.from_user.id,
            await panel.home(message, language),
            edit=False,
        )

    @dp.message(Command("instructions"))
    @dp.message(F.text.strip().casefold().in_({"инструкция", "instructions"}))
    async def instructions(
        message: Message, state: FSMContext, reviewer_mode: bool = False
    ):
        await state.clear()
        language = language_from_code(message.from_user.language_code)
        if reviewer_mode:
            await message.answer(REVIEW_HELP_TEXT[language])
            return
        await send_navigation(message, language)
        await show(
            message,
            message.from_user.id,
            (INSTRUCTIONS[language], [[(label("home", language), "home")]]),
            edit=False,
        )

    @dp.callback_query(F.data.startswith("ui:"))
    async def on_button(
        callback: CallbackQuery, state: FSMContext, reviewer_mode: bool = False
    ):
        language = language_from_code(callback.from_user.language_code)
        parts = callback.data.split(":", 2)
        if len(parts) != 3 or parts[1] != str(callback.from_user.id) or reviewer_mode:
            await callback.answer(
                choose(
                    language,
                    ru="Откройте своё меню командой /menu.",
                    en="Open your own menu with /menu.",
                ),
                show_alert=True,
            )
            return
        if not isinstance(callback.message, Message):
            await callback.answer()
            return
        # The callback message was sent by the bot. Use the actual actor for permissions.
        message = callback.message.model_copy(update={"from_user": callback.from_user})
        owner = callback.from_user.id
        action = parts[2]
        previous = await state.get_data()
        previous_state = await state.get_state()
        await state.clear()
        await callback.answer()
        back = [[(label("home", language), "home")]]
        if action in {
            "account",
            "connect",
            "change_account",
            "disconnect",
            "confirm_disconnect",
        } and not is_admin(message):
            await show(
                message,
                owner,
                (
                    choose(
                        language,
                        ru="Подключением управляет администратор в личном чате с ботом.",
                        en="An administrator manages the connection in a private chat.",
                    ),
                    back,
                ),
            )
            return
        if action == "home":
            await send_navigation(message, language)
            screen = await panel.home(message, language)
        elif action == "instructions":
            screen = INSTRUCTIONS[language], back
        elif action == "phrases" or action.startswith("phrases:"):
            page = action.partition(":")[2]
            screen = await panel.phrases(language, int(page) if page.isdigit() else 0)
        elif action.startswith("delete:"):
            _, phrase_id, page = action.split(":")
            if not phrase_id.isdigit() or not page.isdigit():
                return
            phrase = await database.get_phrase(int(phrase_id))
            if phrase:
                await database.remove_phrase_by_id(int(phrase_id))
            notice = choose(
                language,
                ru="Фраза удалена." if phrase else "Фраза уже удалена.",
                en="Phrase removed." if phrase else "Phrase already removed.",
            )
            screen = await panel.phrases(language, int(page), notice)
        elif action == "interval" or action.startswith("every:"):
            notice = ""
            if action.startswith("every:"):
                value = action.partition(":")[2]
                if (
                    not value.isdigit()
                    or not MIN_POLL_INTERVAL_MINUTES
                    <= int(value)
                    <= MAX_POLL_INTERVAL_MINUTES
                ):
                    return
                await database.set_poll_interval_minutes(int(value))
                notice = choose(
                    language,
                    ru="Сохранено. Применится после текущего цикла проверки.",
                    en="Saved. Applies after the current check cycle.",
                )
            screen = await panel.interval(language, notice)
        elif action.startswith("notify:"):
            account, target = await panel.target()
            enabled = action == "notify:on"
            if target is None or (
                enabled
                and (
                    not account
                    or (
                        account.get("expires_at") and account["expires_at"] <= utc_now()
                    )
                    or not await database.list_phrases()
                )
            ):
                screen = await panel.home(message, language)
            else:
                await database.set_chat_language(target, language)
                await database.set_monitoring(target, enabled)
                screen = await panel.home(message, language)
        elif action == "account":
            screen = await panel.account(language)
        elif action in {"connect", "change_account"}:
            connected = (
                await connect(message, switch_account=True)
                if action == "change_account"
                else await connect(message)
            )
            account_back = [[(label("account", language), "account")], *back]
            screen = (
                choose(
                    language,
                    ru="Завершите подключение по ссылке в сообщении ниже, затем вернитесь в меню."
                    if connected
                    else "Не удалось начать подключение. Подробности — в сообщении ниже.",
                    en="Complete authorization using the link below, then return to the menu."
                    if connected
                    else "Could not start authorization. See the message below for details.",
                ),
                account_back,
            )
        elif action == "disconnect":
            await state.set_state(PanelInput.disconnect)
            await state.update_data(
                disconnect_key=connection_key(await threads_auth.status()),
                panel_id=message.message_id,
            )
            screen = (
                choose(
                    language,
                    ru="<b>Отключить Threads?</b>\n\nПоиск и уведомления остановятся. Список фраз сохранится. Для продолжения потребуется снова подключить аккаунт.",
                    en="<b>Disconnect Threads?</b>\n\nSearch and notifications will stop. Your phrases will be kept. Connect the account again to resume.",
                ),
                [
                    [(label("confirm_disconnect", language), "confirm_disconnect")],
                    [(label("cancel", language), "account")],
                ],
            )
        elif action == "confirm_disconnect":
            if (
                previous_state == PanelInput.disconnect.state
                and previous.get("panel_id") == message.message_id
                and previous.get("disconnect_key")
                and previous["disconnect_key"]
                == connection_key(await threads_auth.status())
            ):
                await threads_auth.disconnect()
            screen = await panel.account(language)
        elif action in {"add", "custom", "search"}:
            input_state = {
                "add": PanelInput.phrases,
                "custom": PanelInput.interval,
                "search": PanelInput.search,
            }[action]
            prompts = {
                "add": (
                    "Пришлите фразы для отслеживания — каждую с новой строки. Например:\nищу дизайнера\nнужен сайт",
                    "Send phrases to track, one per line. For example:\nlooking for a designer\nneed a website",
                ),
                "custom": (
                    f"Пришлите интервал в минутах: от {MIN_POLL_INTERVAL_MINUTES} до {MAX_POLL_INTERVAL_MINUTES}.",
                    f"Send an interval in minutes: {MIN_POLL_INTERVAL_MINUTES}–{MAX_POLL_INTERVAL_MINUTES}.",
                ),
                "search": (
                    "Пришлите слово или фразу для разового поиска. В список отслеживания она не попадёт.",
                    "Send a word or phrase for a one-time search. It will not be added to tracked phrases.",
                ),
            }
            prompt_text = prompts[action][0 if language == "ru" else 1]
            await show(
                message, owner, (prompt_text, [[(label("cancel", language), "home")]])
            )
            # ForceReply lets the bot receive replies in groups with privacy mode enabled.
            prompt = await message.answer(
                f'<a href="tg://user?id={owner}">{escape(callback.from_user.first_name)}</a>, '
                + prompt_text,
                reply_markup=ForceReply(selective=True),
                parse_mode="HTML",
            )
            await state.set_state(input_state)
            await state.update_data(
                panel_id=message.message_id, prompt_id=prompt.message_id
            )
            return
        elif action in {"recent", "top"}:
            query = (
                previous.get("query")
                if previous_state == PanelInput.search_type.state
                and previous.get("panel_id") == message.message_id
                else None
            )
            if not query:
                screen = (
                    choose(
                        language,
                        ru="Этот поиск уже завершён или отменён. Нажмите «Найти публикации», чтобы начать новый.",
                        en="This search has ended or was cancelled. Choose Find posts to start again.",
                    ),
                    back,
                )
            else:
                await show(
                    message,
                    owner,
                    (
                        choose(language, ru="Ищу публикации…", en="Searching posts…"),
                        back,
                    ),
                )
                try:
                    result = await search_service.search_by_keyword(
                        query,
                        search_type=SearchType.TOP
                        if action == "top"
                        else SearchType.RECENT,
                    )
                    for chunk in format_search_result(result, language=language):
                        await message.answer(
                            chunk, parse_mode="HTML", disable_web_page_preview=True
                        )
                    screen = (
                        choose(
                            language,
                            ru="Поиск завершён. Результаты — в сообщениях ниже.",
                            en="Search complete. Results are in the messages below.",
                        ),
                        [[(label("search", language), "search")], *back],
                    )
                except (ThreadsAPIError, ThreadsNotConnectedError):
                    screen = (
                        choose(
                            language,
                            ru="Не удалось выполнить поиск. Проверьте подключение Threads и повторите попытку.",
                            en="Search failed. Check the Threads connection and try again.",
                        ),
                        back,
                    )
        elif action == "run":
            account, target = await panel.target()
            if (
                not account
                or not await database.list_phrases()
                or target not in await database.list_notification_chats()
            ):
                screen = await panel.home(message, language)
            else:
                await show(
                    message,
                    owner,
                    (
                        choose(language, ru="Проверяю фразы…", en="Checking phrases…"),
                        back,
                    ),
                )
                stats = await watcher.run_once()
                if stats.not_connected:
                    screen = (
                        choose(
                            language,
                            ru="Подключение Threads истекло. Администратору нужно подключить аккаунт снова.",
                            en="Threads connection expired. An administrator needs to connect the account again.",
                        ),
                        back,
                    )
                else:
                    text = choose(
                        language,
                        ru=f"<b>Проверка завершена</b>\n\nПроверено фраз: {stats.phrases_checked}\nОтправлено уведомлений: {stats.posts_sent}\nОшибок: {stats.errors}\n\nПервая проверка запоминает существующие посты. Уведомления приходят только о новых.",
                        en=f"<b>Check complete</b>\n\nPhrases checked: {stats.phrases_checked}\nNotifications sent: {stats.posts_sent}\nErrors: {stats.errors}\n\nThe first check remembers existing posts. Notifications are sent only for newer matches.",
                    )
                    screen = text, back
        else:
            screen = await panel.home(message, language)
        await show(message, owner, screen)

    @dp.message(PanelInput.phrases, F.text, ~F.text.startswith("/"))
    @dp.message(PanelInput.interval, F.text, ~F.text.startswith("/"))
    @dp.message(PanelInput.search, F.text, ~F.text.startswith("/"))
    async def accept_input(message: Message, state: FSMContext):
        language = language_from_code(message.from_user.language_code)
        data = await state.get_data()
        if message.chat.type != ChatType.PRIVATE and (
            not message.reply_to_message
            or message.reply_to_message.message_id != data.get("prompt_id")
        ):
            return
        current = await state.get_state()
        raw = message.text.strip()
        if not raw:
            await message.answer(
                choose(
                    language,
                    ru="Пришлите текст или отмените ввод: /cancel.",
                    en="Send text or cancel with /cancel.",
                )
            )
            return
        if current == PanelInput.interval.state:
            if (
                not raw.isascii()
                or not raw.isdigit()
                or not MIN_POLL_INTERVAL_MINUTES
                <= int(raw)
                <= MAX_POLL_INTERVAL_MINUTES
            ):
                await message.answer(
                    choose(
                        language,
                        ru=f"Нужно целое число от {MIN_POLL_INTERVAL_MINUTES} до {MAX_POLL_INTERVAL_MINUTES}. Попробуйте ещё раз или нажмите /cancel.",
                        en=f"Enter a whole number from {MIN_POLL_INTERVAL_MINUTES} to {MAX_POLL_INTERVAL_MINUTES}. Try again or use /cancel.",
                    )
                )
                return
            await database.set_poll_interval_minutes(int(raw))
            screen = await panel.interval(
                language,
                choose(
                    language,
                    ru="Сохранено. Применится после текущего цикла проверки.",
                    en="Saved. Applies after the current check cycle.",
                ),
            )
        elif current == PanelInput.phrases.state:
            phrases = split_phrases(raw)
            if (
                not phrases
                or len(phrases) > 50
                or any(len(phrase) > 300 for phrase in phrases)
            ):
                await message.answer(
                    choose(
                        language,
                        ru="Пришлите от 1 до 50 фраз, до 300 символов каждая. Отмена: /cancel.",
                        en="Send 1–50 phrases, up to 300 characters each. Cancel: /cancel.",
                    )
                )
                return
            added = await database.add_phrases(phrases)
            notice = choose(
                language,
                ru=f"Добавлено новых фраз: {sum(item[2] for item in added)}.\nПервая проверка запомнит существующие посты; уведомления будут только о новых.",
                en=f"New phrases added: {sum(item[2] for item in added)}.\nThe first check will remember existing posts; only newer matches will be notified.",
            )
            screen = await panel.phrases(language, notice=notice)
        else:
            if len(raw) > 300:
                await message.answer(
                    choose(
                        language,
                        ru="Сократите запрос до 300 символов. Отмена: /cancel.",
                        en="Keep your query within 300 characters. Cancel: /cancel.",
                    )
                )
                return
            await state.set_state(PanelInput.search_type)
            await state.update_data(query=raw)
            screen = (
                choose(
                    language,
                    ru=f"<b>Найти публикации</b>\n\nЗапрос: {escape(raw)}\nКакие публикации показать?",
                    en=f"<b>Find posts</b>\n\nQuery: {escape(raw)}\nWhich posts would you like to see?",
                ),
                [
                    [(label(key, language), key) for key in ("recent", "top")],
                    [(label("cancel", language), "home")],
                ],
            )
        if current != PanelInput.search.state:
            await state.clear()
        await send_navigation(message, language)
        text, rows = screen
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=data["panel_id"],
                text=text,
                reply_markup=keyboard(message.from_user.id, rows),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except TelegramBadRequest:
            await show(message, message.from_user.id, screen, edit=False)

    return panel
