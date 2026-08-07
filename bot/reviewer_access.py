import hashlib
import hmac
from datetime import datetime, timezone

from bot.storage import Database, utc_now

REVIEW_START_PREFIX = "review_"


class ReviewerAccessService:
    """Redeem a temporary, hashed review credential for a Telegram user."""

    def __init__(
        self,
        *,
        database: Database,
        code_hash: str,
        expires_at: datetime | None,
    ) -> None:
        normalized_hash = code_hash.strip().lower()
        self._code_hash = (
            normalized_hash
            if len(normalized_hash) == 64
            and all(character in "0123456789abcdef" for character in normalized_hash)
            else ""
        )
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        self._expires_at = (
            expires_at.astimezone(timezone.utc) if expires_at is not None else None
        )
        self._db = database

    @property
    def configured(self) -> bool:
        return bool(
            self._code_hash
            and self._expires_at is not None
            and self._expires_at > utc_now()
        )

    async def redeem_start_parameter(
        self,
        *,
        telegram_user_id: int,
        start_parameter: str,
    ) -> bool:
        if not self.configured or not start_parameter.startswith(REVIEW_START_PREFIX):
            return False
        code = start_parameter.removeprefix(REVIEW_START_PREFIX)
        if not 20 <= len(code) <= 56 or not code.isascii():
            return False
        candidate_hash = hashlib.sha256(code.encode("ascii")).hexdigest()
        if not hmac.compare_digest(candidate_hash, self._code_hash):
            return False
        assert self._expires_at is not None
        await self._db.upsert_reviewer_access_grant(
            telegram_user_id=telegram_user_id,
            expires_at=self._expires_at,
        )
        return True

    async def is_active(self, telegram_user_id: int) -> bool:
        active_grant = await self._db.has_active_reviewer_access(telegram_user_id)
        return self.configured and active_grant
