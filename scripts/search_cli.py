"""CLI for testing Threads keyword search without Telegram."""

import argparse
import asyncio
import sys
from datetime import timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.config import SearchMode, SearchType, settings
from bot.crypto import TokenCipher
from bot.search_service import SearchService, format_search_result
from bot.storage import Database
from bot.threads_client import ThreadsAPIError, ThreadsClient
from bot.threads_oauth import (
    ThreadsNotConnectedError,
    ThreadsOAuthClient,
    ThreadsTokenManager,
)


async def run(args: argparse.Namespace) -> int:
    if not settings.threads_oauth_configured:
        print("Error: Threads OAuth is not configured", file=sys.stderr)
        return 1

    async with httpx.AsyncClient(timeout=30.0) as http_client:
        oauth_client = ThreadsOAuthClient(
            app_id=settings.threads_app_id,
            app_secret=settings.threads_app_secret,
            redirect_uri=settings.threads_redirect_uri,
            graph_base=settings.threads_graph_base,
            http_client=http_client,
        )
        token_manager = ThreadsTokenManager(
            database=Database(),
            cipher=TokenCipher(settings.threads_token_encryption_key),
            oauth_client=oauth_client,
            refresh_before=timedelta(days=settings.threads_token_refresh_before_days),
        )
        service = SearchService(
            client=ThreadsClient(http_client=http_client),
            token_manager=token_manager,
        )

        try:
            if args.mode == "tag":
                result = await service.search_by_tag(
                    args.query,
                    search_type=SearchType(args.search_type),
                    limit=args.limit,
                )
            else:
                result = await service.search_by_keyword(
                    args.query,
                    search_type=SearchType(args.search_type),
                    search_mode=SearchMode.KEYWORD,
                    limit=args.limit,
                )
        except (ThreadsAPIError, ThreadsNotConnectedError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

    for chunk in format_search_result(result, max_posts=args.limit):
        print(chunk)
        print("-" * 40)

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Search Threads posts by keyword")
    parser.add_argument("query", help="Keyword or phrase")
    parser.add_argument(
        "--search-type",
        choices=["TOP", "RECENT"],
        default="RECENT",
        help="TOP = popular, RECENT = latest",
    )
    parser.add_argument(
        "--mode",
        choices=["keyword", "tag"],
        default="keyword",
        help="Search by keyword or topic tag",
    )
    parser.add_argument("--limit", type=int, default=10, help="Max posts to show")
    args = parser.parse_args()

    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
