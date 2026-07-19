# Threads Monitor Bot

Telegram bot for searching and monitoring public Threads posts by keywords through the official Meta Threads API.

## Features

- One-time keyword search with `/search`
- Monitoring of a shared keyword pool
- Telegram notifications for newly discovered posts
- Duplicate notification prevention
- Configurable polling interval
- Optional Telegram user allowlist
- SQLite storage

## Threads API permissions

The application uses:

- `threads_basic` — required for access to the Threads API
- `threads_keyword_search` — required to search Threads posts by keyword

Without Advanced Access, keyword search is limited to posts owned by the authenticated Threads user. Searching public posts requires Meta App Review approval for `threads_keyword_search`.

## Requirements

- Python 3.11+
- Telegram bot token
- Threads user access token with `threads_basic` and `threads_keyword_search`

## Installation

```bash
git clone https://github.com/honeycakeend/threads_monitor.git
cd threads_monitor

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
```

Fill in at least these values in `.env`:

```env
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
THREADS_ACCESS_TOKEN=your_threads_access_token
ALLOWED_USER_IDS=your_telegram_user_id
```

Never commit `.env` or access tokens to the repository.

## Run

```bash
source .venv/bin/activate
python main.py
```

To test Threads keyword search without Telegram:

```bash
python scripts/search_cli.py "keyword" --limit 3
```

## Bot commands

- `/start` — start the bot and enable monitoring for the current chat
- `/help` — show command help
- `/search <phrase>` — run a one-time recent-post search
- `/search top <phrase>` — run a top-post search
- `/add <phrase>` — add a phrase to the monitoring pool
- `/pool` — list monitored phrases
- `/remove <id or phrase>` — remove a phrase
- `/monitor on|off` — enable or disable notifications
- `/interval [minutes]` — view or update the polling interval
- `/run` — check the monitoring pool immediately
- `/status` — show current bot status

## Data handling

The bot stores its operational data in a local SQLite database. This includes monitored phrases, Telegram chat settings, and identifiers of posts that have already been seen or notified.

- [Privacy Policy](https://honeycakeend.github.io/threads_monitor/privacy-policy.html)
- [Terms of Service](https://honeycakeend.github.io/threads_monitor/terms.html)
- [Data Deletion Instructions](https://honeycakeend.github.io/threads_monitor/data-deletion.html)

Privacy and data-deletion contact: [adigitalnyc@gmail.com](mailto:adigitalnyc@gmail.com)

## License

No license has been granted for reuse or redistribution.
