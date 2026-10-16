"""Owner-only access management through Telegram buttons."""

from html import escape

from aiogram import Dispatcher, F
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot.access import EDITABLE_ROLES, AccessDeniedError, AccessService
from bot.i18n import Language, choose, language_from_code


class AccessInput(StatesGroup):
    user_id = State()
    confirm = State()


def role_name(role: str | None, language: Language) -> str:
    names = {
        "owner": ("Владелец", "Owner"),
        "operator": ("Настройки", "Settings"),
        "admin": ("Настройки + Threads", "Settings + Threads"),
        "blocked": ("Доступ отозван", "Access revoked"),
        None: ("Нет доступа", "No access"),
    }
    return names[role][0 if language == "ru" else 1]


class AccessPanel:
    PAGE_SIZE = 8

    def __init__(self, access: AccessService, show) -> None:
        self.access = access
        self.show = show

    def allowed(self, message: Message) -> bool:
        return (
            message.chat.type == ChatType.PRIVATE
            and message.from_user is not None
            and self.access.is_owner(message.from_user.id)
        )

    async def home(self, language: Language, page: int = 0, notice: str = ""):
        users = await self.access.users()
        page = max(0, min(page, max(0, (len(users) - 1) // self.PAGE_SIZE)))
        text = choose(
            language,
            ru="<b>Пользователи и доступ</b>\n\nДобавляйте пользователей по числовому Telegram ID. Только владелец может выдавать и отзывать права.\n\nНастройки — поиск, фразы, частота и уведомления.\nНастройки + Threads — дополнительно подключение, смена и отключение общего аккаунта.",
            en="<b>Users and access</b>\n\nAdd users by numeric Telegram ID. Only the owner can grant or revoke access.\n\nSettings — search, phrases, frequency and notifications.\nSettings + Threads — also connect, change and disconnect the shared account.",
        )
        if notice:
            text += "\n\n" + notice
        rows = [
            [
                (
                    choose(language, ru="+ Добавить пользователя", en="+ Add user"),
                    "access:add",
                )
            ]
        ]
        for user in users[page * self.PAGE_SIZE : (page + 1) * self.PAGE_SIZE]:
            user_id = user["telegram_user_id"]
            rows.append(
                [
                    (
                        f"{user_id} · {role_name(user['role'], language)}",
                        f"access:user:{user_id}",
                    )
                ]
            )
        nav = []
        if page:
            nav.append(("←", f"access:page:{page - 1}"))
        if (page + 1) * self.PAGE_SIZE < len(users):
            nav.append(("→", f"access:page:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([(choose(language, ru="← В меню", en="← Menu"), "home")])
        return text, rows

    async def card(self, target: int, language: Language, notice: str = ""):
        role = await self.access.role(target)
        stored = await self.access.db.get_bot_user(target)
        username = stored.get("username") if stored else None
        text = choose(
            language,
            ru=f"<b>Доступ пользователя</b>\n\nTelegram ID: <code>{target}</code>\nПрава: {role_name(role, language)}",
            en=f"<b>User access</b>\n\nTelegram ID: <code>{target}</code>\nRole: {role_name(role, language)}",
        )
        if username:
            text += f"\n@{escape(username)}"
        if notice:
            text += "\n\n" + notice
        rows = []
        if role == "owner":
            text += choose(
                language,
                ru="\n\nПрава владельца защищены и не меняются через бота.",
                en="\n\nOwner access is protected and cannot be changed through the bot.",
            )
        else:
            for key in ("operator", "admin"):
                rows.append([(role_name(key, language), f"access:role:{target}:{key}")])
            if role not in {None, "blocked"}:
                rows.append(
                    [
                        (
                            choose(language, ru="Отозвать доступ", en="Revoke access"),
                            f"access:role:{target}:blocked",
                        )
                    ]
                )
        rows.append(
            [(choose(language, ru="← К пользователям", en="← Users"), "access")]
        )
        return text, rows

    async def handle(
        self,
        message: Message,
        owner: int,
        language: Language,
        action: str,
        callback: CallbackQuery,
        state: FSMContext,
    ):
        if not self.allowed(message):
            await callback.answer(
                choose(
                    language,
                    ru="Управление доступом доступно только владельцу в личном чате.",
                    en="Only the owner can manage access in a private chat.",
                ),
                show_alert=True,
            )
            return
        previous = await state.get_data()
        previous_state = await state.get_state()
        await state.clear()
        await callback.answer()
        if action == "access:add":
            screen = (
                choose(
                    language,
                    ru="<b>Добавить пользователя</b>\n\nПришлите числовой Telegram ID пользователя. Например: 123456789. Имя @username не подходит.",
                    en="<b>Add user</b>\n\nSend the user's numeric Telegram ID. For example: 123456789. An @username cannot be used.",
                ),
                [[(choose(language, ru="Отмена", en="Cancel"), "access")]],
            )
            await state.set_state(AccessInput.user_id)
            await state.update_data(panel_id=message.message_id)
        elif action.startswith("access:user:"):
            raw = action.removeprefix("access:user:")
            if not raw.isascii() or not raw.isdigit() or not 0 < int(raw) < 2**52:
                return
            screen = await self.card(int(raw), language)
        elif action.startswith("access:role:"):
            parts = action.split(":")
            if (
                len(parts) != 4
                or not parts[2].isascii()
                or not parts[2].isdigit()
                or parts[3] not in EDITABLE_ROLES
            ):
                return
            target, role = int(parts[2]), parts[3]
            if not 0 < target < 2**52:
                return
            current = await self.access.role(target)
            if current == "owner":
                screen = await self.card(target, language)
            else:
                text = choose(
                    language,
                    ru=f"<b>Подтвердите изменение прав</b>\n\nTelegram ID: <code>{target}</code>\nСейчас: {role_name(current, language)}\nПосле сохранения: <b>{role_name(role, language)}</b>\n\n",
                    en=f"<b>Confirm access change</b>\n\nTelegram ID: <code>{target}</code>\nCurrent: {role_name(current, language)}\nAfter saving: <b>{role_name(role, language)}</b>\n\n",
                )
                text += choose(
                    language,
                    ru="Пользователь потеряет доступ к боту. Уже подключённый общий Threads-аккаунт останется подключённым."
                    if role == "blocked"
                    else "Пользователь не сможет выдавать права другим людям. Настройки и аккаунт общие для команды.",
                    en="The user will lose bot access. The shared Threads account will remain connected."
                    if role == "blocked"
                    else "The user will not be able to grant access to others. Settings and the account are shared by the team.",
                )
                screen = (
                    text,
                    [
                        [
                            (
                                choose(
                                    language, ru="Сохранить права", en="Save access"
                                ),
                                "access:confirm",
                            )
                        ],
                        [
                            (
                                choose(language, ru="Отмена", en="Cancel"),
                                f"access:user:{target}",
                            )
                        ],
                    ],
                )
                await state.set_state(AccessInput.confirm)
                await state.update_data(
                    target=target,
                    role=role,
                    expected_role=current,
                    panel_id=message.message_id,
                )
        elif action == "access:confirm":
            if (
                previous_state != AccessInput.confirm.state
                or previous.get("panel_id") != message.message_id
            ):
                screen = await self.home(
                    language,
                    notice=choose(
                        language,
                        ru="Подтверждение устарело или отменено. Выберите пользователя заново.",
                        en="Confirmation expired or was cancelled. Select the user again.",
                    ),
                )
            else:
                try:
                    saved = await self.access.set_role(
                        actor=owner,
                        target=previous["target"],
                        role=previous["role"],
                        expected_role=previous["expected_role"],
                    )
                except AccessDeniedError:
                    saved = False
                screen = await self.card(
                    previous["target"],
                    language,
                    choose(
                        language,
                        ru="Права сохранены и действуют сразу."
                        if saved
                        else "Права уже изменились. Проверьте их и повторите действие.",
                        en="Access saved and effective immediately."
                        if saved
                        else "Access has already changed. Check the current role and try again.",
                    ),
                )
        else:
            page = action.removeprefix("access:page:")
            screen = await self.home(
                language, int(page) if page.isascii() and page.isdigit() else 0
            )
        await self.show(message, owner, screen)


def setup_access_input(dp: Dispatcher, panel: AccessPanel) -> None:
    @dp.message(AccessInput.user_id, F.text, ~F.text.startswith("/"))
    async def accept_id(message: Message, state: FSMContext):
        if not panel.allowed(message):
            await state.clear()
            return
        language = language_from_code(message.from_user.language_code)
        raw = message.text.strip()
        if not raw.isascii() or not raw.isdigit() or not 0 < int(raw) < 2**52:
            await message.answer(
                choose(
                    language,
                    ru="Нужен числовой Telegram ID, например 123456789. Отмена: кнопка «Меню» или /cancel.",
                    en="Enter a numeric Telegram ID, for example 123456789. Cancel: Menu or /cancel.",
                )
            )
            return
        await state.clear()
        await panel.show(
            message,
            message.from_user.id,
            await panel.card(int(raw), language),
            edit=False,
        )
