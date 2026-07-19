from enum import StrEnum
from pathlib import Path

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / ".env"


class SearchType(StrEnum):
    TOP = "TOP"
    RECENT = "RECENT"


class SearchMode(StrEnum):
    KEYWORD = "KEYWORD"
    TAG = "TAG"


class MediaType(StrEnum):
    TEXT = "TEXT"
    IMAGE = "IMAGE"
    VIDEO = "VIDEO"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE if ENV_FILE.exists() else None,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    telegram_bot_token: str = ""
    allowed_user_ids_raw: str = Field(default="", validation_alias="ALLOWED_USER_IDS")

    threads_access_token: str = ""
    threads_api_base: str = "https://graph.threads.net/v1.0"

    database_path: Path = Path("./data/bot.db")

    default_search_type: SearchType = SearchType.RECENT
    default_search_limit: int = 25
    poll_interval_minutes: int = 15

    @computed_field  # type: ignore[prop-decorator]
    @property
    def allowed_user_ids(self) -> list[int]:
        raw = self.allowed_user_ids_raw.strip()
        if not raw:
            return []
        return [int(part.strip()) for part in raw.split(",") if part.strip()]

    @property
    def threads_configured(self) -> bool:
        return bool(self.threads_access_token.strip())

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token.strip())


settings = Settings()
