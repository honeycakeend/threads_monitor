from datetime import datetime
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

    threads_oauth_admin_user_ids_raw: str = Field(
        default="",
        validation_alias="THREADS_OAUTH_ADMIN_USER_IDS",
    )
    threads_primary_chat_id: int | None = None

    threads_app_id: str = ""
    threads_app_secret: str = ""
    threads_token_encryption_key: str = ""
    threads_redirect_uri: str = Field(
        default="https://threads-auth.adigitalnyc.com/oauth/threads/callback",
        validation_alias="THREADS_REDIRECT_URI",
    )
    threads_oauth_authorize_url: str = "https://threads.net/oauth/authorize"
    threads_graph_base: str = "https://graph.threads.net"
    threads_api_base: str = "https://graph.threads.net/v1.0"
    threads_oauth_state_ttl_minutes: int = Field(default=10, ge=5, le=60)
    threads_token_refresh_before_days: int = Field(default=7, ge=2, le=30)
    threads_token_refresh_check_hours: int = Field(default=6, ge=1, le=24)

    threads_review_access_code_hash: str = Field(
        default="",
        validation_alias="THREADS_REVIEW_ACCESS_CODE_HASH",
    )
    threads_review_access_expires_at: datetime | None = Field(
        default=None,
        validation_alias="THREADS_REVIEW_ACCESS_EXPIRES_AT",
    )

    oauth_server_host: str = "127.0.0.1"
    oauth_server_port: int = Field(default=8080, ge=1, le=65535)
    public_base_url: str = "https://threads-auth.adigitalnyc.com"

    database_path: Path = Path("./data/bot.db")

    default_search_type: SearchType = SearchType.RECENT
    default_search_limit: int = 25
    poll_interval_minutes: int = 15

    @computed_field  # type: ignore[prop-decorator]
    @property
    def allowed_user_ids(self) -> list[int]:
        return self._parse_id_list(self.allowed_user_ids_raw)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def threads_oauth_admin_user_ids(self) -> list[int]:
        explicit = self._parse_id_list(self.threads_oauth_admin_user_ids_raw)
        return explicit or self.allowed_user_ids

    @staticmethod
    def _parse_id_list(raw: str) -> list[int]:
        value = raw.strip()
        if not value:
            return []
        return [int(part.strip()) for part in value.split(",") if part.strip()]

    @property
    def threads_oauth_configured(self) -> bool:
        credentials_present = all(
            (
                self.threads_app_id.strip(),
                self.threads_app_secret.strip(),
                self.threads_token_encryption_key.strip(),
                self.threads_redirect_uri.strip(),
            )
        )
        return bool(
            credentials_present
            and self.threads_redirect_uri.startswith("https://")
            and self.public_base_url.startswith("https://")
        )

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token.strip())


settings = Settings()
