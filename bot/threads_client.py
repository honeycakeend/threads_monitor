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
        base_url: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = (base_url or settings.threads_api_base).rstrip("/")
        self._http_client = http_client

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
        access_token: str,
    ) -> SearchResult:
        if not access_token:
            raise ThreadsAPIError("Threads account is not connected")

        params: dict[str, Any] = {
            "q": query.strip(),
            "search_type": (search_type or settings.default_search_type).value,
            "search_mode": search_mode.value,
            "fields": DEFAULT_FIELDS,
            "limit": min(limit or settings.default_search_limit, 100),
        }

        if media_type is not None:
            params["media_type"] = media_type.value
        if since is not None:
            params["since"] = since
        if until is not None:
            params["until"] = until
        if author_username:
            params["author_username"] = author_username.lstrip("@")

        try:
            if self._http_client is not None:
                response = await self._http_client.get(
                    f"{self._base_url}/keyword_search",
                    params=params,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
            else:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.get(
                        f"{self._base_url}/keyword_search",
                        params=params,
                        headers={"Authorization": f"Bearer {access_token}"},
                    )
        except httpx.HTTPError:
            raise ThreadsAPIError("Could not reach the Threads API") from None

        if response.status_code >= 400:
            self._raise_api_error(response)

        try:
            payload = response.json()
            raw_posts = payload.get("data", [])
            if not isinstance(raw_posts, list):
                raise TypeError
            posts = [ThreadPost.from_api(item) for item in raw_posts]
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ThreadsAPIError("Threads API returned an unexpected response") from None
        return SearchResult(query=query.strip(), posts=posts, total=len(posts))

    @staticmethod
    def _raise_api_error(response: httpx.Response) -> None:
        raise ThreadsAPIError(
            f"Threads API rejected the request (HTTP {response.status_code})",
            status_code=response.status_code,
        )
