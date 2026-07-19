from html import escape

from bot.config import SearchMode, SearchType
from bot.models import SearchResult, ThreadPost
from bot.threads_client import ThreadsClient


class SearchService:
    def __init__(self, client: ThreadsClient | None = None) -> None:
        self._client = client or ThreadsClient()

    async def search_by_keyword(
        self,
        query: str,
        *,
        search_type: SearchType = SearchType.RECENT,
        search_mode: SearchMode = SearchMode.KEYWORD,
        limit: int | None = None,
    ) -> SearchResult:
        return await self._client.keyword_search(
            query,
            search_type=search_type,
            search_mode=search_mode,
            limit=limit,
        )

    async def search_by_tag(
        self,
        tag: str,
        *,
        search_type: SearchType = SearchType.TOP,
        limit: int | None = None,
    ) -> SearchResult:
        normalized = tag.lstrip("#").strip()
        return await self._client.keyword_search(
            normalized,
            search_type=search_type,
            search_mode=SearchMode.TAG,
            limit=limit,
        )


def format_post(post: ThreadPost, index: int) -> str:
    text = (post.text or "").strip()
    if len(text) > 400:
        text = text[:397] + "..."

    username = escape(post.username or "unknown")
    lines = [
        f"<b>{index}. @{username}</b>",
    ]

    if post.timestamp:
        lines.append(f"🕐 {post.timestamp.strftime('%Y-%m-%d %H:%M UTC')}")

    if text:
        lines.append(escape(text))

    flags = []
    if post.has_replies:
        flags.append("replies")
    if post.is_reply:
        flags.append("reply")
    if post.is_quote_post:
        flags.append("quote")

    if flags:
        lines.append(f"🏷 {', '.join(flags)}")

    if post.permalink:
        lines.append(f'🔗 <a href="{post.permalink}">Открыть в Threads</a>')

    return "\n".join(lines)


def format_search_result(result: SearchResult, *, max_posts: int = 10) -> list[str]:
    if not result.posts:
        return [f'По запросу «{result.query}» ничего не найдено.']

    header = (
        f'🔍 Запрос: <b>{escape(result.query)}</b>\n'
        f"Найдено: {result.total}"
    )
    messages = [header]

    chunk = ""
    shown = 0
    for index, post in enumerate(result.posts, start=1):
        if shown >= max_posts:
            messages.append(f"... и ещё {result.total - max_posts} пост(ов)")
            break

        block = format_post(post, index) + "\n\n"
        if len(chunk) + len(block) > 3500:
            messages.append(chunk.rstrip())
            chunk = block
        else:
            chunk += block
        shown += 1

    if chunk.strip():
        messages.append(chunk.rstrip())

    return messages
