from datetime import timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from bot.config import SearchMode, SearchType
from bot.models import ThreadPost
from bot.search_service import SearchService, matches_post_text
from bot.storage import Database, utc_now
from bot.threads_client import ThreadsClient
from bot.watcher import SearchWatcher


@pytest.mark.parametrize(
    "query,text,is_reply,expected",
    [
        ("ищу дизайнера", "Срочно ищу дизайнера для проекта", False, True),
        ("ищу дизайнера", "ИЩУ   ДИЗАЙНЕРА", False, True),
        ("ищу дизайнера", "Ищу\n\tдизайнера", False, True),
        ("ищу дизайнера", "Ищу\u00a0дизайнера", False, True),
        ("café", "Cafe\u0301 nearby", False, True),
        ("ищу дизайнера", "Ищу дизайнера", True, False),
        ("ищу дизайнера", "Мой новый проект", False, False),
        ("ищу дизайнера", "Ищу опытного дизайнера", False, True),
        ("ищу дизайнера", "дизайнера ищу", False, True),
        ("ищу дизайнера", None, False, False),
        ("ищу дизайнера", "", False, False),
        (" ", "любой пост", False, False),
        ("ищу дизайнера", "Ищу хорошего дизайнера", False, True),
        ("ищу дизайнера", "Ищу! Хорошего, опытного дизайнера.", False, True),
        ("ищу дизайнера", "Ищу хорошего дизайнера", True, False),
        ("ищу дизайнера", "Ищу разработчика", False, False),
        ("ищу дизайнера", "Резюме дизайнера", False, False),
        ("ищу дизайнера", "Неищу дизайнера", False, False),
        ("ищу дизайнера", "Ищу дизайнера123", False, False),
        ("ищу дизайнера", "Ищу дизайнеров", False, True),
        ("ищу дизайнера", "Ищу дизайнер", False, True),
        ("!!!", "Любой пост!", False, False),
        ("ищу дизайнера", "Ищем опытных дизайнеров", False, True),
        ("ищу дизайнера", "Дизайнеры, ищет вас наша команда", False, True),
        ("ищу дизайнера", "Ищу дизайнеров", True, False),
        ("ищу дизайнера", "Ищу дизайнерский стул", False, False),
        ("ищу дизайнера", "Ищу дизайн сайта", False, False),
        ("проект", "Помогаю с проектами", False, True),
        ("ищу дизайнера", "Неищу дизайнеров", False, False),
        ("designer", "designers wanted", False, False),
    ],
)
def test_all_query_words_must_be_in_own_non_reply_text(query, text, is_reply, expected):
    post = ThreadPost.from_api({"id": "post", "text": text, "is_reply": is_reply})
    assert matches_post_text(post, query) is expected


def api_posts():
    return [
        {"id": "original", "text": "ИЩУ\nдизайнера", "is_reply": False},
        {"id": "with-comments", "text": "Ищу дизайнера", "has_replies": True},
        {"id": "reply", "text": "Ищу дизайнера", "is_reply": True},
        {
            "id": "comment-only",
            "text": "Мой проект",
            "has_replies": True,
            "replies": {"data": [{"text": "ищу дизайнера"}]},
        },
        {
            "id": "quote-only",
            "text": "Посмотрите на это",
            "is_quote_post": True,
            "quoted_post": {"text": "ищу дизайнера"},
        },
        {"id": "own-quote-text", "text": "Ищу дизайнера", "is_quote_post": True},
        {"id": "username-only", "username": "ищу дизайнера", "text": "Портфолио"},
        {"id": "empty", "has_replies": True},
        {"id": "extra-words", "text": "Ищу хорошего дизайнера"},
        {"id": "reordered", "text": "Дизайнера для сайта ищу"},
        {"id": "plural", "text": "Ищу хороших дизайнеров"},
        {"id": "verb-form", "text": "Ищем дизайнеров"},
        {"id": "reply-plural", "text": "Ищем дизайнеров", "is_reply": True},
        {"id": "related-word", "text": "Ищу дизайнерский стул"},
        {"id": "missing-word", "text": "Посоветуйте хорошего дизайнера"},
        {
            "id": "split-own-comment",
            "text": "Ищу специалиста",
            "has_replies": True,
            "replies": {"data": [{"text": "дизайнеров"}]},
        },
        {
            "id": "split-own-quote",
            "text": "Ищу специалиста",
            "is_quote_post": True,
            "quoted_post": {"text": "дизайнеров"},
        },
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("sort", [SearchType.RECENT, SearchType.TOP])
async def test_real_search_pipeline_filters_api_results_and_updates_total(sort):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.params["search_type"] == sort.value
        assert "is_reply" in request.url.params["fields"]
        return httpx.Response(200, json={"data": api_posts()})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = SearchService(client=ThreadsClient(http_client=client))
        result = await service.search_by_keyword(
            "ищу дизайнера",
            search_type=sort,
            access_token="test-token",
        )
    assert [post.id for post in result.posts] == [
        "original",
        "with-comments",
        "own-quote-text",
        "extra-words",
        "reordered",
        "plural",
        "verb-form",
    ]
    assert result.total == 7
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_topic_search_does_not_require_literal_phrase_in_post_text():
    def handler(request):
        assert request.url.params["search_mode"] == "TAG"
        return httpx.Response(200, json={"data": [{"id": "tag", "text": "Hello"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = SearchService(client=ThreadsClient(http_client=client))
        result = await service.search_by_tag("#design", access_token="test-token")
        explicit_mode = await service.search_by_keyword(
            "design",
            search_mode=SearchMode.TAG,
            access_token="test-token",
        )
    assert result.total == explicit_mode.total == 1


@pytest.mark.asyncio
async def test_watcher_sends_matching_originals_and_does_not_mark_replies_seen(
    tmp_path,
):
    db = Database(tmp_path / "bot.db")
    await db.upsert_threads_connection(
        threads_user_id="account",
        username="team",
        access_token_encrypted=b"ciphertext",
        token_type="bearer",
        expires_at=utc_now() + timedelta(days=30),
        connected_by_telegram_user_id=1,
        target_chat_id=-100,
    )
    phrase_id, _ = await db.add_phrase("ищу дизайнера")
    await db.mark_phrase_initialized(phrase_id)
    token_manager = AsyncMock()
    token_manager.get_access_token.return_value = "test-token"
    bot = AsyncMock()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": api_posts()})
        )
    ) as client:
        service = SearchService(client=ThreadsClient(http_client=client))
        watcher = SearchWatcher(
            bot, database=db, search_service=service, token_manager=token_manager
        )
        stats = await watcher.run_once()
        repeat = await watcher.run_once()
    assert stats.errors == 0 and stats.posts_sent == 7
    assert repeat.posts_sent == 0
    assert bot.send_message.await_count == 7
    assert not await db.is_post_seen(phrase_id, "reply")
    assert not await db.is_post_seen(phrase_id, "comment-only")
    assert not await db.is_post_seen(phrase_id, "quote-only")

    assert not await db.is_post_seen(phrase_id, "split-own-comment")
    assert not await db.is_post_seen(phrase_id, "split-own-quote")
