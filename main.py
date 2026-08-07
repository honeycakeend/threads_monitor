import asyncio
import logging
import sys

from bot.config import settings
from bot.handlers import run_bot

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    # HTTPX emits full request URLs at INFO. Meta's exchange/refresh endpoints
    # require secrets in query parameters, so those logs must stay disabled.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    if not settings.telegram_configured:
        logger.error(
            "TELEGRAM_BOT_TOKEN is missing. Copy .env.example to .env and set the token."
        )
        sys.exit(1)

    if not settings.threads_oauth_configured:
        logger.error(
            "Threads OAuth is not configured. Fill THREADS_APP_ID, THREADS_APP_SECRET, "
            "THREADS_TOKEN_ENCRYPTION_KEY and THREADS_REDIRECT_URI."
        )
        sys.exit(1)

    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
