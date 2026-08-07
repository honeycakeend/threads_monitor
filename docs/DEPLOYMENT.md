# OAuth, VPS, and Meta App Review deployment checklist

This document is a runbook only. The repository does not deploy itself or modify DNS/Meta configuration.

## Final public values

- VPS: `178.105.42.32`
- DNS A record: `threads-auth.adigitalnyc.com -> 178.105.42.32` (confirmed publicly resolving)
- Public base URL: `https://threads-auth.adigitalnyc.com`
- OAuth callback: `https://threads-auth.adigitalnyc.com/oauth/threads/callback`
- Deauthorize callback: `https://threads-auth.adigitalnyc.com/oauth/threads/deauthorize`
- Data-deletion callback: `https://threads-auth.adigitalnyc.com/oauth/threads/data-deletion`

## 1. Pre-deployment checks

Run locally:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/python -m py_compile main.py bot/*.py scripts/*.py
```

Confirm that `.env` and `data/` are untracked. Do not copy a development database containing real credentials into a public artifact.

## 2. Prepare the VPS

The existing helper installs the app into `/opt/threads_bot`, creates the unprivileged `threadsbot` user, installs dependencies, and installs (but does not start) `threads-bot.service`:

```bash
sudo bash deploy/setup.sh
```

Review `/opt/threads_bot/.env` and set permissions:

```bash
sudo chown threadsbot:threadsbot /opt/threads_bot/.env
sudo chmod 600 /opt/threads_bot/.env
sudo install -d -o threadsbot -g threadsbot -m 700 /opt/threads_bot/data
```

Generate the Fernet key on a trusted machine and put it only in the service environment:

```bash
/opt/threads_bot/.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Required production values:

```env
TELEGRAM_BOT_TOKEN=...
ALLOWED_USER_IDS=...
THREADS_OAUTH_ADMIN_USER_IDS=...
THREADS_PRIMARY_CHAT_ID=...
THREADS_APP_ID=...
THREADS_APP_SECRET=...
THREADS_TOKEN_ENCRYPTION_KEY=...
PUBLIC_BASE_URL=https://threads-auth.adigitalnyc.com
THREADS_REDIRECT_URI=https://threads-auth.adigitalnyc.com/oauth/threads/callback
OAUTH_SERVER_HOST=127.0.0.1
OAUTH_SERVER_PORT=8080
DATABASE_PATH=./data/bot.db
```

`THREADS_APP_ID` and `THREADS_APP_SECRET` must be the Threads credentials shown inside the Meta "Access the Threads API" use case. Do not substitute an ID/secret belonging to a different Meta product.

Back up `THREADS_TOKEN_ENCRYPTION_KEY` in a secrets manager. A database backup without that key cannot restore the token. A key rotation requires either decrypt-and-reencrypt tooling or, for this version, `/disconnect_threads` followed by a new OAuth connection.

## 3. Reverse proxy and TLS

Keep Uvicorn on `127.0.0.1:8080`. Allow inbound TCP 80/443 to Caddy/Nginx, but do not expose port 8080 publicly.

### Caddy (recommended)

Install Caddy using its official package instructions, then:

```bash
sudo install -m 0644 deploy/Caddyfile.example /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Caddy will obtain TLS after DNS and ports 80/443 are reachable. The example intentionally does not enable access logging, because the callback query string contains a one-time OAuth code and state.

### Nginx alternative

Obtain a certificate for `threads-auth.adigitalnyc.com`, replace the certificate paths if needed, then install `deploy/nginx.conf.example` as a site configuration. Validate with `sudo nginx -t` before reloading. The example disables access logging for this virtual host.

## 4. Start and verify the service

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now threads-bot
sudo systemctl status threads-bot --no-pager
sudo journalctl -u threads-bot -n 100 --no-pager
curl -fsS http://127.0.0.1:8080/healthz
curl -fsS https://threads-auth.adigitalnyc.com/healthz
```

Expected health response:

```json
{"status":"ok"}
```

Never paste or search for tokens/codes in logs. Uvicorn access logging is disabled by application configuration. Keep reverse-proxy access logging disabled unless query-string redaction is explicitly configured.

## 5. Meta app configuration

In the Meta App Dashboard, open the Threads use case and enter exactly:

- Website URL: `https://threads-auth.adigitalnyc.com`
- Valid OAuth Redirect URI: `https://threads-auth.adigitalnyc.com/oauth/threads/callback`
- Deauthorize Callback URL: `https://threads-auth.adigitalnyc.com/oauth/threads/deauthorize`
- Data Deletion Request URL: `https://threads-auth.adigitalnyc.com/oauth/threads/data-deletion`

Request only:

- `threads_basic`
- `threads_keyword_search`

Add the single agent Threads account as an app role/tester and accept the invitation from that Threads account. While the app lacks Advanced Access for `threads_keyword_search`, validate searches only against posts owned by this authorized test account.

The deauthorize/data-deletion endpoints require Meta's documented form POST with `signed_request`, signed HMAC-SHA256 using the configured Threads App Secret. A different secret, payload format, or algorithm is rejected with HTTP 400. Confirm callback tests from the same Threads app in the dashboard before review submission.

## 6. Legal URLs

The current legal pages can continue to be served with GitHub Pages:

- Privacy: `https://honeycakeend.github.io/threads_monitor/privacy-policy.html`
- Terms: `https://honeycakeend.github.io/threads_monitor/terms.html`
- User-facing deletion instructions: `https://honeycakeend.github.io/threads_monitor/data-deletion.html`

The Meta **Data Deletion Request URL** is the HTTPS POST callback on `threads-auth.adigitalnyc.com`, not the static instruction page.

## 7. Manual end-to-end verification

1. In the primary internal Telegram chat, send `/start` or `/monitor on` and verify the bot can post there.
2. In a private chat, have an ID listed in `THREADS_OAUTH_ADMIN_USER_IDS` send `/connect_threads`.
3. Verify the bot returns an HTTPS button and no token/code is visible in Telegram.
4. Open the button while signed into the designated Threads test account. Confirm the consent screen requests only `threads_basic` and `threads_keyword_search`.
5. Complete consent. Verify the browser shows the minimal success page and Telegram receives the account/chat confirmation.
6. Send `/threads_status`. Verify username, Threads user ID, primary chat ID, and expiry are shown, but no token is shown.
7. Create a distinctive post on the authorized test account. Run `/search <distinctive phrase>` and verify the result.
8. Add another distinctive phrase with `/add`, run `/run` once to initialize, then create a matching post and run `/run` again. Verify one notification reaches only the bound primary chat and is not duplicated on another run.
9. Verify `/connect_threads` fails in a group and for a Telegram user outside the OAuth-admin allowlist.
10. Verify reusing the completed callback URL returns an expired/already-used state page.
11. Use Meta's dashboard/test facility to send a deauthorize callback. Verify `/threads_status` becomes disconnected and search no longer runs.
12. Reconnect, then use Meta's data-deletion callback test. Verify the JSON response contains `url` and `confirmation_code`, the returned status URL displays completion, and the account credential/binding is removed.
13. Reconnect again if the service must remain operational after destructive callback testing.

## 8. App Review evidence

Prepare a concise reviewer description and screencast covering:

- The private Telegram `/connect_threads` entry point and allowlisted administrator model
- OAuth consent and success confirmation
- `/threads_status` with no secrets
- A one-time keyword search and watcher notification
- The development-mode limitation: only the authorized test account's own posts are used before approval
- Why `threads_keyword_search` is required for public third-party post discovery after approval
- `/disconnect_threads` and the user-data deletion path

Give reviewers a usable test route. If Telegram access is allowlisted, add the supplied reviewer Telegram ID for the review window or provide equivalent precise access instructions. Do not give reviewers production secrets.

Suggested permission explanations:

- **threads_basic:** identifies the one Threads account that explicitly authorizes the bot and permits authenticated Threads API calls. Account ID/username are displayed in connection status; the long-lived token is encrypted at rest.
- **threads_keyword_search:** searches by administrator-defined phrases for immediate results and periodic monitoring. Matching public posts are sent only to the configured internal Telegram chat. The app does not scrape Threads or bypass API limits.

## 9. Operational checks after deployment

- Monitor token expiry with `/threads_status`.
- The refresh worker checks every six hours by default and refreshes within seven days of expiry; Meta permits refresh only for a valid long-lived token, so an already-expired/revoked token requires reconnecting.
- Back up `/opt/threads_bot/data/bot.db` and the encryption key separately and securely.
- Rotate/retain system logs so phrase text and numeric chat IDs are not kept longer than necessary.
- After code updates, run tests before restarting the service.
