import hashlib
import logging
import secrets
from html import escape
from urllib.parse import parse_qs

from aiogram import Bot
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from bot.i18n import Language, choose
from bot.storage import Database
from bot.threads_oauth import (
    REVIEW_OAUTH_PURPOSE,
    InvalidOAuthStateError,
    InvalidSignedRequestError,
    ThreadsAuthService,
    ThreadsOAuthError,
    verify_meta_signed_request,
)

logger = logging.getLogger(__name__)


def create_oauth_app(
    *,
    database: Database,
    threads_auth: ThreadsAuthService,
    bot: Bot,
    app_secret: str,
    public_base_url: str,
) -> FastAPI:
    app = FastAPI(
        title="Threads Monitor OAuth",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"
        )
        return response

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/oauth/threads/callback", response_class=HTMLResponse)
    async def threads_callback(
        state: str | None = None,
        code: str | None = None,
        error: str | None = None,
        error_description: str | None = None,
    ) -> HTMLResponse:
        del error_description  # Never reflect provider text into HTML or logs.
        if not state:
            return _html_page(
                "Authorization incomplete",
                "The state parameter is missing. Start the connection again in Telegram.",
                success=False,
                status_code=400,
            )
        try:
            state_record = await threads_auth.consume_state(state)
        except InvalidOAuthStateError:
            return _html_page(
                "Invalid link",
                "This link expired or was already used. Run /connect_threads again.",
                success=False,
                status_code=400,
            )
        except Exception:
            logger.exception("Internal OAuth state lookup failure")
            return _html_page(
                "Temporary error",
                "The one-time link could not be verified. Start again in Telegram.",
                success=False,
                status_code=500,
            )

        telegram_user_id = int(state_record["telegram_user_id"])
        language: Language = "ru" if state_record.get("language") == "ru" else "en"
        retry_command = "/connect_threads"
        if error:
            await _notify(
                bot,
                telegram_user_id,
                choose(
                    language,
                    ru="Подключение Threads отменено. Если это было случайно, "
                    f"запустите {retry_command} ещё раз.",
                    en="Threads connection was cancelled. Run /connect_threads to retry.",
                ),
            )
            return _html_page(
                choose(
                    language,
                    ru="Подключение отменено",
                    en="Connection cancelled",
                ),
                choose(
                    language,
                    ru="Доступ к Threads не был предоставлен. Это окно можно закрыть.",
                    en="Threads access was not granted. You can close this window.",
                ),
                success=False,
                status_code=400,
                language=language,
            )
        if not code or len(code) > 4096:
            await _notify(
                bot,
                telegram_user_id,
                choose(
                    language,
                    ru="Threads не вернул код авторизации. "
                    f"Запустите {retry_command} ещё раз.",
                    en="Threads did not return an authorization code. "
                    f"Run {retry_command} to retry.",
                ),
            )
            return _html_page(
                choose(
                    language,
                    ru="Авторизация не завершена",
                    en="Authorization incomplete",
                ),
                choose(
                    language,
                    ru="Threads не вернул код авторизации. Это окно можно закрыть.",
                    en="Threads did not return an authorization code. You can close this window.",
                ),
                success=False,
                status_code=400,
                language=language,
            )

        try:
            completed = await threads_auth.finish_authorization(
                state=state_record,
                code=code,
            )
        except ThreadsOAuthError as exc:
            logger.warning("Threads OAuth callback failed: %s", exc)
            await _notify(
                bot,
                telegram_user_id,
                choose(
                    language,
                    ru="Не удалось завершить подключение Threads. Одноразовая ссылка "
                    f"уже погашена; запустите {retry_command} ещё раз.",
                    en="Could not complete the Threads connection. The one-time link "
                    f"has been consumed; run {retry_command} to retry.",
                ),
            )
            return _html_page(
                choose(language, ru="Ошибка подключения", en="Connection error"),
                choose(
                    language,
                    ru="Не удалось получить доступ к Threads. Запустите подключение заново в Telegram.",
                    en="Could not access Threads. Start the connection again in Telegram.",
                ),
                success=False,
                status_code=502,
                language=language,
            )
        except Exception:
            logger.exception("Internal Threads OAuth callback failure")
            await _notify(
                bot,
                telegram_user_id,
                choose(
                    language,
                    ru="Внутренняя ошибка при подключении Threads. Одноразовая "
                    f"ссылка уже погашена; запустите {retry_command} ещё раз.",
                    en="An internal error interrupted the Threads connection. The "
                    f"one-time link has been consumed; run {retry_command} to retry.",
                ),
            )
            return _html_page(
                choose(language, ru="Ошибка подключения", en="Connection error"),
                choose(
                    language,
                    ru="Внутренняя ошибка при сохранении подключения. Повторите попытку в Telegram.",
                    en="An internal error prevented saving the connection. Retry in Telegram.",
                ),
                success=False,
                status_code=500,
                language=language,
            )

        account_label = (
            f"@{escape(completed.username)}"
            if completed.username
            else escape(completed.threads_user_id)
        )
        if completed.purpose == REVIEW_OAUTH_PURPOSE:
            await _notify(
                bot,
                completed.telegram_user_id,
                choose(
                    completed.language,
                    ru="Threads подключён для Meta App Review.\n"
                    f"Аккаунт: {account_label}\n"
                    "Токен зашифрован и изолирован от рабочего аккаунта.\n"
                    "Далее: /search ThreadsAPI",
                    en="Threads connected for Meta App Review.\n"
                    f"Account: {account_label}\n"
                    "The token is encrypted and isolated from the production account.\n"
                    "Next: run /search ThreadsAPI",
                ),
            )
        else:
            await _notify(
                bot,
                completed.telegram_user_id,
                choose(
                    completed.language,
                    ru="Threads успешно подключён.\n"
                    f"Аккаунт: {account_label}\n"
                    f"Основной chat id: {completed.target_chat_id}\n"
                    "Токен сохранён в зашифрованном виде и будет обновляться автоматически.",
                    en="Threads connected successfully.\n"
                    f"Account: {account_label}\n"
                    f"Primary chat id: {completed.target_chat_id}\n"
                    "The token is encrypted at rest and will refresh automatically.",
                ),
            )
        if completed.purpose == REVIEW_OAUTH_PURPOSE:
            return _html_page(
                choose(
                    completed.language,
                    ru="Threads подключён",
                    en="Threads connected",
                ),
                choose(
                    completed.language,
                    ru="Review-подключение готово. Вернитесь в Telegram и выполните /search ThreadsAPI.",
                    en="The review connection is ready. Return to Telegram and run /search ThreadsAPI.",
                ),
                success=True,
                language=completed.language,
            )
        return _html_page(
            choose(
                completed.language,
                ru="Threads подключён",
                en="Threads connected",
            ),
            choose(
                completed.language,
                ru="Подключение завершено. Подтверждение отправлено в Telegram; это окно можно закрыть.",
                en="Connection complete. A confirmation was sent to Telegram; you can close this window.",
            ),
            success=True,
            language=completed.language,
        )

    @app.post("/oauth/threads/deauthorize")
    async def threads_deauthorize(request: Request) -> JSONResponse:
        try:
            payload = verify_meta_signed_request(
                await _read_signed_request(request),
                app_secret,
            )
        except InvalidSignedRequestError:
            return JSONResponse({"success": False}, status_code=400)
        await database.delete_threads_user_data(str(payload["user_id"]))
        logger.info("Processed a valid Threads deauthorization callback")
        return JSONResponse({"success": True})

    @app.post("/oauth/threads/data-deletion")
    async def threads_data_deletion(request: Request) -> JSONResponse:
        try:
            payload = verify_meta_signed_request(
                await _read_signed_request(request),
                app_secret,
            )
        except InvalidSignedRequestError:
            return JSONResponse({"error": "invalid_request"}, status_code=400)

        threads_user_id = str(payload["user_id"])
        await database.delete_threads_user_data(threads_user_id)
        confirmation_code = await database.record_data_deletion(
            confirmation_code=secrets.token_urlsafe(24),
            threads_user_id_hash=hashlib.sha256(
                threads_user_id.encode("utf-8")
            ).hexdigest(),
        )
        status_url = (
            f"{public_base_url.rstrip('/')}/oauth/threads/data-deletion/status/"
            f"{confirmation_code}"
        )
        logger.info("Processed a valid Threads data-deletion callback")
        return JSONResponse(
            {"url": status_url, "confirmation_code": confirmation_code}
        )

    @app.get(
        "/oauth/threads/data-deletion/status/{confirmation_code}",
        response_class=HTMLResponse,
    )
    async def data_deletion_status(confirmation_code: str) -> HTMLResponse:
        status = await database.get_data_deletion_status(confirmation_code)
        if status is None:
            return _html_page(
                "Request not found",
                "Check the data-deletion confirmation link.",
                success=False,
                status_code=404,
            )
        return _html_page(
            "Data deleted",
            "The associated Threads credentials and bindings were deleted.",
            success=True,
        )

    return app


async def _read_signed_request(request: Request) -> str:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
    if content_type != "application/x-www-form-urlencoded":
        raise InvalidSignedRequestError("Unsupported callback content type")
    content_length = request.headers.get("content-length")
    try:
        if content_length and int(content_length) > 65536:
            raise InvalidSignedRequestError("Callback payload is too large")
    except ValueError as exc:
        raise InvalidSignedRequestError("Invalid content length") from exc
    body = await request.body()
    if len(body) > 65536:
        raise InvalidSignedRequestError("Callback payload is too large")
    try:
        values = parse_qs(body.decode("utf-8"), strict_parsing=True)
    except (UnicodeDecodeError, ValueError) as exc:
        raise InvalidSignedRequestError("Malformed callback body") from exc
    signed_requests = values.get("signed_request", [])
    if len(signed_requests) != 1:
        raise InvalidSignedRequestError("Missing signed_request")
    return signed_requests[0]


async def _notify(bot: Bot, chat_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id, text)
    except Exception:  # noqa: BLE001 - notification failures must not undo OAuth state
        logger.warning(
            "Could not send OAuth status notification to Telegram user_id=%s",
            chat_id,
        )


def _html_page(
    title: str,
    message: str,
    *,
    success: bool,
    status_code: int = 200,
    language: str = "en",
) -> HTMLResponse:
    color = "#15803d" if success else "#b91c1c"
    symbol = "✓" if success else "!"
    html = f"""<!doctype html>
<html lang="{escape(language)}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 0; background: #f6f7f8; color: #171717; }}
    main {{ max-width: 34rem; margin: 12vh auto; padding: 2rem; background: white; border-radius: 1rem; box-shadow: 0 8px 30px #0001; }}
    .mark {{ color: {color}; font-size: 2rem; font-weight: 700; }}
    h1 {{ margin: .5rem 0; }}
    p {{ line-height: 1.55; }}
  </style>
</head>
<body><main><div class="mark">{symbol}</div><h1>{escape(title)}</h1><p>{escape(message)}</p></main></body>
</html>"""
    return HTMLResponse(html, status_code=status_code)
