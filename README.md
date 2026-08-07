# Threads Monitor Bot

Telegram bot for searching and monitoring Threads posts through Meta's official Threads API. A Threads account is connected by an allowlisted administrator with OAuth; access tokens are never sent through Telegram or stored in plaintext.

## Features

- One-time keyword search with `/search`
- Monitoring of a shared keyword pool and Telegram notifications
- One agent Threads account and one primary internal Telegram chat in version 1
- Private-chat `/connect_threads` OAuth flow with a random, one-time state
- Encrypted long-lived token storage in SQLite and automatic pre-expiry refresh
- Meta deauthorization and data-deletion callbacks with `signed_request` verification
- Duplicate-notification prevention and configurable polling interval

## Threads permissions

The app requests exactly:

- `threads_basic` — identify the authorized Threads account and use its access token
- `threads_keyword_search` — search Threads posts by keyword for one-time searches and monitoring

Without Advanced Access, keyword search is limited to posts owned by the authorized app tester/account. Public search of other accounts' posts must not be presented as working until Meta approves `threads_keyword_search`.

## OAuth flow and security

1. An OAuth administrator sends `/connect_threads` in a private chat with the bot.
2. The bot creates a cryptographically random state, stores only its SHA-256 hash with a short TTL, and binds it to the Telegram administrator and target notification chat.
3. The administrator opens the Threads authorization window. Meta redirects to the public HTTPS callback.
4. The callback atomically consumes the state, exchanges the authorization code for a short-lived token, immediately exchanges it for a long-lived token, and reads `/me` to bind the Threads identity.
5. Only the long-lived token is encrypted with Fernet and persisted. The key is supplied only through `THREADS_TOKEN_ENCRYPTION_KEY`.
6. The bot confirms the connection in Telegram. The watcher obtains one current token per cycle and reuses it for that cycle's searches.

The Uvicorn access log is disabled because OAuth `code` and `state` arrive in the callback query string. API bearer tokens are sent in the `Authorization` header for search/profile calls. Meta's token exchange/refresh endpoints require sensitive query/form parameters, so application code never logs request URLs or payloads.

OAuth state is valid for 10 minutes by default and can be used once. Creating a new link invalidates earlier unused links for that administrator.

Token request shapes follow Meta's [Threads access-token documentation](https://developers.facebook.com/docs/threads/get-started/get-access-tokens-and-permissions/), [long-lived token guide](https://developers.facebook.com/docs/threads/get-started/long-lived-tokens/), and the official [Meta Threads API Postman collection](https://www.postman.com/meta/threads/documentation/dht3nzz/threads-api).

## Requirements

- Python 3.11+
- Telegram bot token
- Threads-enabled Meta app and its Threads App ID/secret
- Public HTTPS endpoint for the OAuth server
- SQLite (included with Python)

## Local installation

```bash
git clone https://github.com/honeycakeend/threads_monitor.git
cd threads_monitor

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
```

Generate a token-encryption key once:

```bash
.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Store the output in `THREADS_TOKEN_ENCRYPTION_KEY` and back it up securely. Replacing or losing it makes a stored token unreadable; the account must then be reconnected. Never commit `.env`, access tokens, authorization codes, the app secret, or the encryption key.

## Required environment variables

| Variable | Purpose |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token |
| `ALLOWED_USER_IDS` | Comma-separated Telegram users allowed to use the bot; keep non-empty in production |
| `THREADS_OAUTH_ADMIN_USER_IDS` | Comma-separated users allowed to connect/status/disconnect Threads; falls back to `ALLOWED_USER_IDS` only when empty |
| `THREADS_PRIMARY_CHAT_ID` | One chat that receives watcher notifications; group IDs are normally negative |
| `THREADS_APP_ID` | Threads App ID shown in the Meta Threads use case |
| `THREADS_APP_SECRET` | Matching Threads App Secret; also verifies Meta `signed_request` callbacks |
| `THREADS_TOKEN_ENCRYPTION_KEY` | Fernet key for token encryption at rest |
| `PUBLIC_BASE_URL` | `https://threads-auth.adigitalnyc.com` |
| `THREADS_REDIRECT_URI` | `https://threads-auth.adigitalnyc.com/oauth/threads/callback` |

All optional/defaulted variables are documented in [.env.example](.env.example).

## Run

```bash
source .venv/bin/activate
python main.py
```

The process runs Telegram long polling, the watcher, token-refresh worker, and a loopback FastAPI/Uvicorn listener. Put Caddy or Nginx in front of `127.0.0.1:8080`; do not expose Uvicorn directly.

Health check:

```bash
curl http://127.0.0.1:8080/healthz
```

To test keyword search without Telegram after connecting the account:

```bash
python scripts/search_cli.py "keyword" --limit 3
```

## Bot commands

- `/connect_threads` — create an OAuth button; OAuth administrator, private chat only
- `/threads_status` — show connected account, bound chat, and token expiry; OAuth administrator, private chat only
- `/disconnect_threads` — disable the account and erase its stored ciphertext; OAuth administrator, private chat only
- `/start` — start the bot and enable monitoring for the current chat
- `/help` — show command help
- `/search <phrase>` — one-time recent-post search
- `/search top <phrase>` — top-post search
- `/add <phrase>` — add a phrase to the monitoring pool
- `/pool` — list monitored phrases
- `/remove <id or phrase>` — remove a phrase
- `/monitor on|off` — enable or disable notifications
- `/interval [minutes]` — view or update the polling interval
- `/run` — check the monitoring pool immediately
- `/status` — show operational status

Only an active Threads chat binding is eligible for watcher notifications. Connecting the account creates/enables the primary binding without overriding a later explicit `/monitor off` choice.

## Public endpoints

- Callback: `https://threads-auth.adigitalnyc.com/oauth/threads/callback`
- Deauthorize: `https://threads-auth.adigitalnyc.com/oauth/threads/deauthorize`
- Data deletion: `https://threads-auth.adigitalnyc.com/oauth/threads/data-deletion`
- Health: `https://threads-auth.adigitalnyc.com/healthz`

The deauthorize and data-deletion endpoints accept Meta's `application/x-www-form-urlencoded` POST containing `signed_request`. The implementation verifies HMAC-SHA256 with `THREADS_APP_SECRET`, requires a `user_id`, and rejects other formats. Data deletion removes the Threads credential/account binding and returns Meta's required confirmation code plus a public status URL. This relies on Meta sending the documented signed-request format and on `THREADS_APP_SECRET` matching the exact Threads app that generated it.

## Meta App Review preparation

Configure the Meta Threads use case with:

- Valid OAuth Redirect URI: `https://threads-auth.adigitalnyc.com/oauth/threads/callback`
- Deauthorize Callback URL: `https://threads-auth.adigitalnyc.com/oauth/threads/deauthorize`
- Data Deletion Request URL: `https://threads-auth.adigitalnyc.com/oauth/threads/data-deletion`
- Website URL: `https://threads-auth.adigitalnyc.com`
- Permissions requested: `threads_basic`, `threads_keyword_search`

Before submitting, add the agent Threads account as an app/test user and accept the invitation in Threads. Record a reviewer screencast that shows:

1. The administrator opening the bot in a private Telegram chat.
2. `/connect_threads` and the OAuth consent screen showing both permissions.
3. The success page and Telegram confirmation.
4. `/threads_status` without revealing credentials.
5. `/search` and watcher behavior against content owned by the authorized test account.
6. `/disconnect_threads`, followed by a disconnected status.

The review instructions must provide Meta a working test path (including Telegram access/allowlisting when required) and explain that public third-party post search is the capability requested by `threads_keyword_search`; development-mode validation uses the authorized test account's own posts.

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for the VPS, reverse-proxy, Meta dashboard, and manual verification checklist.

## Data handling

SQLite stores monitoring phrases, chat settings, seen/notified post IDs, hashed OAuth state, Threads account metadata, encrypted token ciphertext, chat bindings, and data-deletion confirmation records. Raw OAuth state, short-lived tokens, authorization codes, app secrets, encryption keys, and plaintext long-lived tokens are not stored.

- [Privacy Policy](https://honeycakeend.github.io/threads_monitor/privacy-policy.html)
- [Terms of Service](https://honeycakeend.github.io/threads_monitor/terms.html)
- [Data Deletion Instructions](https://honeycakeend.github.io/threads_monitor/data-deletion.html)

Privacy and data-deletion contact: [adigitalnyc@gmail.com](mailto:adigitalnyc@gmail.com)

## Tests

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
```

Tests cover OAuth state TTL/replay, callback success/error behavior, encrypted persistence and disconnect, token refresh/selection, bearer-header search, one token lookup per watcher cycle, signed-request callbacks, and Telegram authorization commands.

## License

No license has been granted for reuse or redistribution.
