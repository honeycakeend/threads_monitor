import asyncio
import logging
import sys

from bot.config import settings
from bot.handlers import run_bot


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    if not settings.telegram_configured:
        logging.error(
            "TELEGRAM_BOT_TOKEN is missing. Copy .env.example to .env and set the token."
        )
        sys.exit(1)

    if not settings.threads_configured:
        logging.warning(
            "THREADS_ACCESS_TOKEN is missing. Bot will start, but search will fail until configured."
        )

    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
