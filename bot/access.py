"""Persistent bot roles with protected, server-configured owners."""

from bot.config import settings
from bot.storage import Database

EDITABLE_ROLES = frozenset({"operator", "admin", "blocked"})


class AccessDeniedError(ValueError):
    pass


class AccessService:
    def __init__(self, database: Database) -> None:
        self.db = database

    def is_owner(self, user_id: int) -> bool:
        return user_id in settings.bot_owner_user_ids

    def configured_role(self, user_id: int) -> str | None:
        if self.is_owner(user_id):
            return "owner"
        if user_id in settings.threads_oauth_admin_user_ids:
            return "admin"
        if user_id in settings.allowed_user_ids:
            return "operator"
        return None

    async def role(self, user_id: int) -> str | None:
        if self.is_owner(user_id):
            return "owner"
        user = await self.db.get_bot_user(user_id)
        if isinstance(user, dict):
            return user["role"]
        return self.configured_role(user_id)

    async def can_manage_threads(self, user_id: int) -> bool:
        return await self.role(user_id) in {"owner", "admin"}

    async def users(self) -> list[dict]:
        stored = {
            user["telegram_user_id"]: user for user in await self.db.list_bot_users()
        }
        ids = (
            set(stored)
            | set(settings.bot_owner_user_ids)
            | set(settings.allowed_user_ids)
            | set(settings.threads_oauth_admin_user_ids)
        )
        return [
            {
                "telegram_user_id": user_id,
                "role": "owner"
                if self.is_owner(user_id)
                else stored[user_id]["role"]
                if user_id in stored
                else self.configured_role(user_id),
                "username": stored.get(user_id, {}).get("username"),
            }
            for user_id in sorted(ids)
        ]

    async def set_role(
        self, *, actor: int, target: int, role: str, expected_role: str | None
    ) -> bool:
        if not self.is_owner(actor):
            raise AccessDeniedError("Only the bot owner can manage access")
        if self.is_owner(target):
            raise AccessDeniedError("Owner access cannot be changed through the bot")
        if role not in EDITABLE_ROLES or not 0 < target < 2**52:
            raise AccessDeniedError("Invalid user ID or role")
        return await self.db.set_bot_user_role(
            actor_user_id=actor,
            target_user_id=target,
            role=role,
            expected_role=expected_role,
            fallback_role=self.configured_role(target),
        )
