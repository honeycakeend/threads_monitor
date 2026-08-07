import asyncio
import logging
from html import escape

from aiogram import Bot

from bot.config import SearchType
from bot.models import ThreadPost
from bot.search_service import SearchService, format_post
from bot.storage import Database
from bot.threads_client import ThreadsAPIError
from bot.threads_oauth import ThreadsNotConnectedError, ThreadsTokenManager

logger = logging.getLogger(__name__)


class WatcherStats:
    __slots__ = ("errors", "not_connected", "phrases_checked", "posts_sent")

    def __init__(self) -> None:
        self.phrases_checked = 0
        self.posts_sent = 0
        self.errors = 0
        self.not_connected = False


class SearchWatcher:
    def __init__(
        self,
        bot: Bot,
        *,
        database: Database | None = None,
        search_service: SearchService | None = None,
        token_manager: ThreadsTokenManager,
    ) -> None:
        self._bot = bot
        self._db = database or Database()
        self._search = search_service or SearchService()
        self._token_manager = token_manager
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="search-watcher")
        interval = await self._db.get_poll_interval_minutes()
        logger.info("Search watcher started (interval=%s min)", interval)

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def run_once(self) -> WatcherStats:
        stats = WatcherStats()
        phrases = await self._db.list_active_phrases_for_monitoring()
        stats.phrases_checked = len(phrases)
        if not phrases:
            return stats

        chat_ids = await self._db.list_notification_chats()
        if not chat_ids:
            logger.info("Watcher skipped: no chats with monitoring enabled")
            return stats

        try:
            access_token = await self._token_manager.get_access_token()
        except ThreadsNotConnectedError:
            stats.not_connected = True
            logger.info("Watcher skipped: Threads account is not connected")
            return stats

        for phrase in phrases:
            try:
                sent = await self._process_phrase(phrase, chat_ids, access_token)
                stats.posts_sent += sent
            except ThreadsAPIError as exc:
                stats.errors += 1
                logger.error("Search failed for phrase %s: %s", phrase["phrase"], exc)
            except Exception:
                stats.errors += 1
                logger.exception("Unexpected error for phrase %s", phrase["phrase"])

        return stats

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                stats = await self.run_once()
                if stats.phrases_checked:
                    interval = await self._db.get_poll_interval_minutes()
                    logger.info(
                        "Watcher cycle: phrases=%s sent=%s errors=%s interval=%s min",
                        stats.phrases_checked,
                        stats.posts_sent,
                        stats.errors,
                        interval,
                    )
            except Exception:
                logger.exception("Watcher cycle failed")

            interval_seconds = (await self._db.get_poll_interval_minutes()) * 60
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                continue

    async def _process_phrase(
        self,
        phrase: dict,
        chat_ids: list[int],
        access_token: str,
    ) -> int:
        search_type = SearchType(phrase["search_type"])
        result = await self._search.search_by_keyword(
            phrase["phrase"],
            search_type=search_type,
            access_token=access_token,
        )

        initialized = bool(phrase["initialized"])
        sent = 0

        for post in result.posts:
            if await self._db.is_post_seen(phrase["id"], post.id):
                continue

            await self._db.mark_post_seen(phrase["id"], post.id)

            if not initialized:
                continue

            if await self._db.is_post_notified(post.id):
                continue

            for chat_id in chat_ids:
                await self._send_post(
                    chat_id=chat_id,
                    phrase_text=phrase["phrase"],
                    post=post,
                )
                sent += 1

            await self._db.mark_post_notified(phrase["id"], post.id)

        if not initialized:
            await self._db.mark_phrase_initialized(phrase["id"])

        return sent

    async def _send_post(self, chat_id: int, phrase_text: str, post: ThreadPost) -> None:
        body = format_post(post, index=1)
        message = (
            f'🔔 <b>Новый пост</b> по фразе «{escape(phrase_text)}»\n\n{body}'
        )
        await self._bot.send_message(
            chat_id,
            message,
            disable_web_page_preview=False,
        )
