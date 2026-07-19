from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class ThreadPost:
    id: str
    text: str | None
    username: str | None
    permalink: str | None
    timestamp: datetime | None
    media_type: str | None
    has_replies: bool
    is_reply: bool
    is_quote_post: bool

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "ThreadPost":
        timestamp_raw = raw.get("timestamp")
        timestamp = None
        if timestamp_raw:
            timestamp = datetime.fromisoformat(timestamp_raw.replace("Z", "+00:00"))

        return cls(
            id=str(raw["id"]),
            text=raw.get("text"),
            username=raw.get("username"),
            permalink=raw.get("permalink"),
            timestamp=timestamp,
            media_type=raw.get("media_type"),
            has_replies=bool(raw.get("has_replies")),
            is_reply=bool(raw.get("is_reply")),
            is_quote_post=bool(raw.get("is_quote_post")),
        )


@dataclass(slots=True)
class SearchResult:
    query: str
    posts: list[ThreadPost]
    total: int
