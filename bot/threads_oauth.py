import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from bot.crypto import TokenCipher
from bot.i18n import Language
from bot.storage import Database, from_db_timestamp, utc_now

logger = logging.getLogger(__name__)

THREADS_SCOPES = ("threads_basic", "threads_keyword_search")
PRIMARY_OAUTH_PURPOSE = "primary"
REVIEW_OAUTH_PURPOSE = "review"


class ThreadsOAuthError(RuntimeError):
    """A safe-to-display Threads OAuth error without credential material."""


class MetaOAuthRequestError(ThreadsOAuthError):
    """A structured Meta error containing only safe numeric diagnostics."""

    def __init__(
        self,
        *,
        path: str,
        status_code: int,
        code: int | None,
        error_subcode: int | None,
    ) -> None:
        self.path = path
        self.status_code = status_code
        self.code = code
        self.error_subcode = error_subcode
        numeric_details = [
            f"{name}={value}"
            for name, value in (("code", code), ("error_subcode", error_subcode))
            if value is not None
        ]
        details = f"; {'; '.join(numeric_details)}" if numeric_details else ""
        super().__init__(
            f"Meta rejected the OAuth request at {path} "
            f"(HTTP {status_code}{details})"
        )


class InvalidOAuthStateError(ThreadsOAuthError):
    pass


class ThreadsNotConnectedError(ThreadsOAuthError):
    pass


class InvalidSignedRequestError(ThreadsOAuthError):
    pass


@dataclass(slots=True)
class OAuthToken:
    access_token: str
    token_type: str
    expires_in: int
    user_id: str | None = None


@dataclass(slots=True)
class ThreadsProfile:
    id: str
    username: str | None


@dataclass(slots=True)
class CompletedAuthorization:
    telegram_user_id: int
    target_chat_id: int
    threads_user_id: str
    username: str | None
    expires_at: datetime
    purpose: str = PRIMARY_OAUTH_PURPOSE
    language: Language = "en"


class ThreadsOAuthClient:
    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        redirect_uri: str,
        graph_base: str = "https://graph.threads.net",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._redirect_uri = redirect_uri
        self._graph_base = graph_base.rstrip("/")
        self._http_client = http_client

    async def exchange_authorization_code(self, code: str) -> OAuthToken:
        response = await self._request(
            "POST",
            "/oauth/access_token",
            data={
                "client_id": self._app_id,
                "client_secret": self._app_secret,
                "grant_type": "authorization_code",
                "redirect_uri": self._redirect_uri,
                "code": code,
            },
        )
        payload = self._json_payload(response)
        return OAuthToken(
            access_token=self._required_string(payload, "access_token"),
            user_id=self._optional_identifier(payload.get("user_id")),
            token_type=str(payload.get("token_type") or "bearer"),
            expires_in=int(payload.get("expires_in") or 0),
        )

    async def exchange_long_lived_token(self, short_lived_token: str) -> OAuthToken:
        try:
            response = await self._request(
                "GET",
                "/access_token",
                params={
                    "grant_type": "th_exchange_token",
                    "client_secret": self._app_secret,
                },
                headers={"Authorization": f"Bearer {short_lived_token}"},
            )
        except MetaOAuthRequestError as exc:
            # Meta's current Postman collection uses OAuth bearer auth, while
            # deployments can still require the historically documented
            # access_token parameter. Retry only for that observed transport
            # rejection so ordinary failures never produce duplicate calls.
            if (exc.code, exc.error_subcode) != (452, 4_279_019):
                raise
            logger.info(
                "Retrying Threads long-lived token exchange with the "
                "access_token parameter"
            )
            response = await self._request(
                "GET",
                "/access_token",
                params={
                    "grant_type": "th_exchange_token",
                    "client_secret": self._app_secret,
                    "access_token": short_lived_token,
                },
            )
        return self._parse_long_lived_token(response)

    async def refresh_long_lived_token(self, access_token: str) -> OAuthToken:
        response = await self._request(
            "GET",
            "/refresh_access_token",
            params={
                "grant_type": "th_refresh_token",
            },
            headers={"Authorization": f"Bearer {access_token}"},
        )
        return self._parse_long_lived_token(response)

    async def get_profile(self, access_token: str) -> ThreadsProfile:
        response = await self._request(
            "GET",
            "/v1.0/me",
            params={"fields": "id,username"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        payload = self._json_payload(response)
        return ThreadsProfile(
            id=self._required_string(payload, "id"),
            username=(str(payload["username"]) if payload.get("username") else None),
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            if self._http_client is not None:
                response = await self._http_client.request(
                    method,
                    f"{self._graph_base}{path}",
                    **kwargs,
                )
            else:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.request(
                        method,
                        f"{self._graph_base}{path}",
                        **kwargs,
                    )
        except httpx.HTTPError:
            # HTTPX exceptions can include the full query string. OAuth token
            # endpoints put credentials there, so suppress the original exception.
            raise ThreadsOAuthError("Could not reach the Meta OAuth service") from None
        if response.status_code >= 400:
            code, error_subcode = self._numeric_meta_error_details(response)
            raise MetaOAuthRequestError(
                path=path,
                status_code=response.status_code,
                code=code,
                error_subcode=error_subcode,
            )
        return response

    def _parse_long_lived_token(self, response: httpx.Response) -> OAuthToken:
        payload = self._json_payload(response)
        try:
            expires_in = int(payload.get("expires_in") or 0)
        except (TypeError, ValueError) as exc:
            raise ThreadsOAuthError("Meta returned an invalid token lifetime") from exc
        if expires_in <= 0:
            raise ThreadsOAuthError("Meta did not return a valid token lifetime")
        return OAuthToken(
            access_token=self._required_string(payload, "access_token"),
            token_type=str(payload.get("token_type") or "bearer"),
            expires_in=expires_in,
        )

    @staticmethod
    def _json_payload(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise ThreadsOAuthError("Meta returned a non-JSON OAuth response") from exc
        if not isinstance(payload, dict):
            raise ThreadsOAuthError("Meta returned an unexpected OAuth response")
        return payload

    @staticmethod
    def _required_string(payload: dict[str, Any], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, (str, int)) or not str(value):
            raise ThreadsOAuthError(f"Meta OAuth response is missing {key}")
        return str(value)

    @staticmethod
    def _optional_identifier(value: Any) -> str | None:
        if value is None:
            return None
        return str(value)

    @staticmethod
    def _numeric_meta_error_details(
        response: httpx.Response,
    ) -> tuple[int | None, int | None]:
        # Provider error text is deliberately not reflected: some providers echo
        # submitted parameters, which could turn an OAuth code into log content.
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict) or not isinstance(payload.get("error"), dict):
            return None, None

        error = payload["error"]

        def numeric_value(key: str) -> int | None:
            value = error.get(key)
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.isascii() and value.isdigit():
                return int(value)
            return None

        return numeric_value("code"), numeric_value("error_subcode")


class ThreadsAuthService:
    def __init__(
        self,
        *,
        database: Database,
        cipher: TokenCipher,
        oauth_client: ThreadsOAuthClient,
        app_id: str,
        redirect_uri: str,
        authorize_url: str = "https://threads.net/oauth/authorize",
        state_ttl_minutes: int = 10,
    ) -> None:
        self._db = database
        self._cipher = cipher
        self._oauth_client = oauth_client
        self._app_id = app_id
        self._redirect_uri = redirect_uri
        self._authorize_url = authorize_url
        self._state_ttl = timedelta(minutes=max(1, state_ttl_minutes))

    async def create_authorization_url(
        self,
        *,
        telegram_user_id: int,
        target_chat_id: int,
        purpose: str = PRIMARY_OAUTH_PURPOSE,
        language: Language = "en",
    ) -> str:
        if purpose not in {PRIMARY_OAUTH_PURPOSE, REVIEW_OAUTH_PURPOSE}:
            raise ThreadsOAuthError("Unsupported OAuth authorization purpose")
        raw_state = secrets.token_urlsafe(32)
        await self._db.create_oauth_state(
            state_hash=self.hash_state(raw_state),
            telegram_user_id=telegram_user_id,
            target_chat_id=target_chat_id,
            purpose=purpose,
            language=language,
            expires_at=utc_now() + self._state_ttl,
        )
        query = urlencode(
            {
                "client_id": self._app_id,
                "redirect_uri": self._redirect_uri,
                "scope": ",".join(THREADS_SCOPES),
                "response_type": "code",
                "state": raw_state,
            }
        )
        return f"{self._authorize_url}?{query}"

    async def complete_authorization(
        self,
        *,
        raw_state: str,
        code: str,
    ) -> CompletedAuthorization:
        state = await self.consume_state(raw_state)
        return await self.finish_authorization(state=state, code=code)

    async def finish_authorization(
        self,
        *,
        state: dict,
        code: str,
    ) -> CompletedAuthorization:
        short_lived = await self._oauth_client.exchange_authorization_code(code)
        long_lived = await self._oauth_client.exchange_long_lived_token(
            short_lived.access_token
        )
        profile = await self._oauth_client.get_profile(long_lived.access_token)
        if short_lived.user_id and short_lived.user_id != profile.id:
            raise ThreadsOAuthError("Threads account identity changed during OAuth")

        expires_at = utc_now() + timedelta(seconds=long_lived.expires_in)
        telegram_user_id = int(state["telegram_user_id"])
        purpose = str(state.get("purpose") or PRIMARY_OAUTH_PURPOSE)
        language: Language = "ru" if state.get("language") == "ru" else "en"
        encrypted_token = self._cipher.encrypt(long_lived.access_token)
        if purpose == REVIEW_OAUTH_PURPOSE:
            try:
                await self._db.upsert_review_threads_connection(
                    telegram_user_id=telegram_user_id,
                    threads_user_id=profile.id,
                    username=profile.username,
                    access_token_encrypted=encrypted_token,
                    token_type=long_lived.token_type,
                    expires_at=expires_at,
                )
            except ValueError as exc:
                raise ThreadsOAuthError(
                    "Temporary reviewer access expired during OAuth"
                ) from exc
        elif purpose == PRIMARY_OAUTH_PURPOSE:
            await self._db.upsert_threads_connection(
                threads_user_id=profile.id,
                username=profile.username,
                access_token_encrypted=encrypted_token,
                token_type=long_lived.token_type,
                expires_at=expires_at,
                connected_by_telegram_user_id=telegram_user_id,
                target_chat_id=int(state["target_chat_id"]),
            )
            await self._db.set_chat_language(
                int(state["target_chat_id"]),
                language,
            )
        else:
            raise ThreadsOAuthError("OAuth state has an unsupported purpose")
        return CompletedAuthorization(
            telegram_user_id=telegram_user_id,
            target_chat_id=int(state["target_chat_id"]),
            threads_user_id=profile.id,
            username=profile.username,
            expires_at=expires_at,
            purpose=purpose,
            language=language,
        )

    async def consume_state(self, raw_state: str) -> dict:
        if not 20 <= len(raw_state) <= 512:
            raise InvalidOAuthStateError("OAuth state is invalid or expired")
        state = await self._db.consume_oauth_state(self.hash_state(raw_state))
        if state is None:
            raise InvalidOAuthStateError("OAuth state is invalid, expired, or already used")
        return state

    async def status(self) -> dict | None:
        account = await self._db.get_active_threads_account()
        if account is None:
            return None
        account["expires_at"] = from_db_timestamp(account.get("expires_at"))
        account.pop("access_token_encrypted", None)
        return account

    async def disconnect(self) -> bool:
        return await self._db.disconnect_active_threads_account()

    async def review_status(self, telegram_user_id: int) -> dict | None:
        account = await self._db.get_review_threads_account(telegram_user_id)
        if account is None:
            return None
        account["expires_at"] = from_db_timestamp(account.get("expires_at"))
        account.pop("access_token_encrypted", None)
        return account

    async def get_review_access_token(self, telegram_user_id: int) -> str:
        account = await self._db.get_review_threads_account(telegram_user_id)
        if account is None:
            raise ThreadsNotConnectedError(
                "Review Threads account is not connected"
            )
        expires_at = from_db_timestamp(account.get("expires_at"))
        if expires_at is None or expires_at <= utc_now():
            raise ThreadsNotConnectedError(
                "Review Threads access token expired; reconnect the account"
            )
        encrypted = account.get("access_token_encrypted")
        if encrypted is None:
            raise ThreadsNotConnectedError(
                "Review Threads account has no stored credential"
            )
        return self._cipher.decrypt(bytes(encrypted))

    async def disconnect_review(self, telegram_user_id: int) -> bool:
        return await self._db.disconnect_review_threads_account(telegram_user_id)

    @staticmethod
    def hash_state(raw_state: str) -> str:
        return hashlib.sha256(raw_state.encode("utf-8")).hexdigest()


class ThreadsTokenManager:
    def __init__(
        self,
        *,
        database: Database,
        cipher: TokenCipher,
        oauth_client: ThreadsOAuthClient,
        refresh_before: timedelta = timedelta(days=7),
    ) -> None:
        self._db = database
        self._cipher = cipher
        self._oauth_client = oauth_client
        self._refresh_before = refresh_before
        self._refresh_lock = asyncio.Lock()

    async def get_access_token(self) -> str:
        account = await self._db.get_active_threads_account()
        if account is None:
            raise ThreadsNotConnectedError("Threads account is not connected")

        expires_at = from_db_timestamp(account.get("expires_at"))
        if expires_at is None or expires_at <= utc_now():
            raise ThreadsNotConnectedError(
                "Threads access token has expired; reconnect the account"
            )

        if expires_at - utc_now() <= self._refresh_before:
            try:
                await self.refresh_if_due()
                account = await self._db.get_active_threads_account() or account
            except ThreadsOAuthError:
                logger.warning("Threads token refresh failed; using still-valid token")

        encrypted = account.get("access_token_encrypted")
        if encrypted is None:
            raise ThreadsNotConnectedError("Threads account has no stored credential")
        return self._cipher.decrypt(bytes(encrypted))

    async def refresh_if_due(self, *, force: bool = False) -> bool:
        async with self._refresh_lock:
            account = await self._db.get_active_threads_account()
            if account is None:
                return False
            expires_at = from_db_timestamp(account.get("expires_at"))
            if expires_at is None or expires_at <= utc_now():
                raise ThreadsNotConnectedError(
                    "Threads access token has expired; reconnect the account"
                )
            if not force and expires_at - utc_now() > self._refresh_before:
                return False

            encrypted = account.get("access_token_encrypted")
            if encrypted is None:
                raise ThreadsNotConnectedError("Threads account has no stored credential")
            current_token = self._cipher.decrypt(bytes(encrypted))
            refreshed = await self._oauth_client.refresh_long_lived_token(current_token)
            new_expires_at = utc_now() + timedelta(seconds=refreshed.expires_in)
            updated = await self._db.update_threads_token(
                account_id=int(account["id"]),
                expected_access_token_encrypted=bytes(encrypted),
                access_token_encrypted=self._cipher.encrypt(refreshed.access_token),
                token_type=refreshed.token_type,
                expires_at=new_expires_at,
            )
            if not updated:
                logger.info(
                    "Skipped Threads token refresh write because the credential changed"
                )
                return False
            logger.info(
                "Refreshed Threads token for account_id=%s; expires_at=%s",
                account["id"],
                new_expires_at.isoformat(),
            )
            return True


class TokenRefreshWorker:
    def __init__(
        self,
        token_manager: ThreadsTokenManager,
        *,
        check_interval: timedelta = timedelta(hours=6),
    ) -> None:
        self._token_manager = token_manager
        self._check_seconds = max(60.0, check_interval.total_seconds())
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="threads-token-refresh")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._token_manager.refresh_if_due()
            except ThreadsNotConnectedError:
                logger.warning("Threads token is expired; administrator must reconnect")
            except Exception:
                logger.exception("Automatic Threads token refresh failed")

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._check_seconds)
            except asyncio.TimeoutError:
                continue


def verify_meta_signed_request(signed_request: str, app_secret: str) -> dict[str, Any]:
    if not signed_request or not app_secret:
        raise InvalidSignedRequestError("Missing signed request or app secret")
    try:
        encoded_signature, encoded_payload = signed_request.split(".", 1)
        supplied_signature = _base64url_decode(encoded_signature)
        payload_bytes = _base64url_decode(encoded_payload)
        payload = json.loads(payload_bytes)
    except (
        ValueError,
        UnicodeDecodeError,
        UnicodeEncodeError,
        json.JSONDecodeError,
        binascii.Error,
    ) as exc:
        raise InvalidSignedRequestError("Malformed signed request") from exc

    expected_signature = hmac.new(
        app_secret.encode("utf-8"),
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise InvalidSignedRequestError("Invalid signed request signature")
    if not isinstance(payload, dict) or str(payload.get("algorithm", "")).upper() != "HMAC-SHA256":
        raise InvalidSignedRequestError("Unsupported signed request algorithm")
    user_id = payload.get("user_id")
    if (
        isinstance(user_id, bool)
        or not isinstance(user_id, (str, int))
        or not str(user_id)
        or len(str(user_id)) > 256
    ):
        raise InvalidSignedRequestError("Signed request has no user_id")
    return payload


def _base64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))
