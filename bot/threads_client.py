import logging
from typing import Any

import httpx

from bot.config import MediaType, SearchMode, SearchType, settings
from bot.models import SearchResult, ThreadPost

logger = logging.getLogger(__name__)
_MAX_LOG_BODY = 4000

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

        url = f"{self._base_url}/keyword_search"
        self._log_request("GET", url, params)

        try:
            if self._http_client is not None:
                response = await self._http_client.get(
                    url,
                    params=params,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
            else:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.get(
                        url,
                        params=params,
                        headers={"Authorization": f"Bearer {access_token}"},
                    )
        except httpx.HTTPError:
            raise ThreadsAPIError("Could not reach the Threads API") from None

        self._log_response(response)

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
        logger.info(
            "Threads keyword_search q=%r type=%s mode=%s http=%s posts=%s",
            query.strip(),
            params["search_type"],
            params["search_mode"],
            response.status_code,
            len(posts),
        )
        return SearchResult(query=query.strip(), posts=posts, total=len(posts))

    @classmethod
    def _log_request(cls, method: str, url: str, params: dict[str, Any]) -> None:
        if not settings.threads_api_debug:
            return
        logger.info("Threads request %s %s params=%s", method, url, params)

    @classmethod
    def _log_response(cls, response: httpx.Response) -> None:
        excerpt = cls._response_excerpt(response)
        if settings.threads_api_debug:
            logger.info(
                "Threads response HTTP %s %s body=%s",
                response.status_code,
                cls._safe_request_url(response),
                excerpt,
            )
            return
        if response.status_code >= 400:
            logger.warning(
                "Threads API error HTTP %s body=%s",
                response.status_code,
                excerpt,
            )

    @staticmethod
    def _safe_request_url(response: httpx.Response) -> str:
        url = response.request.url
        params = [
            (key, value)
            for key, value in url.params.multi_items()
            if key.lower() != "access_token"
        ]
        return str(url.copy_with(params=params))

    @staticmethod
    def _response_excerpt(response: httpx.Response) -> str:
        text = response.text.replace("\n", " ").strip()
        if len(text) > _MAX_LOG_BODY:
            return text[:_MAX_LOG_BODY] + "...[truncated]"
        return text

    @classmethod
    def _raise_api_error(cls, response: httpx.Response) -> None:
        excerpt = cls._response_excerpt(response)
        raise ThreadsAPIError(
            f"Threads API rejected the request (HTTP {response.status_code}): {excerpt}",
            status_code=response.status_code,
        )
