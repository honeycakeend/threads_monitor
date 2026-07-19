from typing import Any

import httpx

from bot.config import MediaType, SearchMode, SearchType, settings
from bot.models import SearchResult, ThreadPost

DEFAULT_FIELDS = (
    "id,text,media_type,permalink,timestamp,username,"
    "has_replies,is_quote_post,is_reply"
)


class ThreadsAPIError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ThreadsClient:
    def __init__(
        self,
        access_token: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self._access_token = access_token or settings.threads_access_token
        self._base_url = (base_url or settings.threads_api_base).rstrip("/")

    async def keyword_search(
        self,
        query: str,
        *,
        search_type: SearchType | None = None,
        search_mode: SearchMode = SearchMode.KEYWORD,
        media_type: MediaType | None = None,
        limit: int | None = None,
        since: int | str | None = None,
        until: int | str | None = None,
        author_username: str | None = None,
    ) -> SearchResult:
        if not self._access_token:
            raise ThreadsAPIError(
                "THREADS_ACCESS_TOKEN is not configured. Add it to .env when ready."
            )

        params: dict[str, Any] = {
            "q": query.strip(),
            "search_type": (search_type or settings.default_search_type).value,
            "search_mode": search_mode.value,
            "fields": DEFAULT_FIELDS,
            "limit": min(limit or settings.default_search_limit, 100),
            "access_token": self._access_token,
        }

        if media_type is not None:
            params["media_type"] = media_type.value
        if since is not None:
            params["since"] = since
        if until is not None:
            params["until"] = until
        if author_username:
            params["author_username"] = author_username.lstrip("@")

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"{self._base_url}/keyword_search",
                params=params,
            )

        if response.status_code >= 400:
            self._raise_api_error(response)

        payload = response.json()
        posts = [ThreadPost.from_api(item) for item in payload.get("data", [])]
        return SearchResult(query=query.strip(), posts=posts, total=len(posts))

    @staticmethod
    def _raise_api_error(response: httpx.Response) -> None:
        try:
            payload = response.json()
            message = payload.get("error", {}).get("message", response.text)
        except ValueError:
            message = response.text
        raise ThreadsAPIError(message, status_code=response.status_code)
