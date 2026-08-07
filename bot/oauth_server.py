import hashlib
import logging
import secrets
from html import escape
from urllib.parse import parse_qs

from aiogram import Bot
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from bot.storage import Database
from bot.threads_oauth import (
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
                "Авторизация не завершена",
                "Отсутствует проверочный параметр state. Запустите подключение заново в Telegram.",
                success=False,
                status_code=400,
            )
        try:
            state_record = await threads_auth.consume_state(state)
        except InvalidOAuthStateError:
            return _html_page(
                "Ссылка недействительна",
                "Ссылка истекла или уже была использована. Запустите /connect_threads заново.",
                success=False,
                status_code=400,
            )
        except Exception:
            logger.exception("Internal OAuth state lookup failure")
            return _html_page(
                "Временная ошибка",
                "Не удалось проверить одноразовую ссылку. Запустите подключение заново в Telegram.",
                success=False,
                status_code=500,
            )

        telegram_user_id = int(state_record["telegram_user_id"])
        if error:
            await _notify(
                bot,
                telegram_user_id,
                "Подключение Threads отменено. Если это было случайно, запустите /connect_threads ещё раз.",
            )
            return _html_page(
                "Подключение отменено",
                "Доступ к Threads не был предоставлен. Это окно можно закрыть.",
                success=False,
                status_code=400,
            )
        if not code or len(code) > 4096:
            await _notify(
                bot,
                telegram_user_id,
                "Threads не вернул код авторизации. Запустите /connect_threads ещё раз.",
            )
            return _html_page(
                "Авторизация не завершена",
                "Threads не вернул код авторизации. Это окно можно закрыть.",
                success=False,
                status_code=400,
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
                "Не удалось завершить подключение Threads. Одноразовая ссылка уже погашена; "
                "запустите /connect_threads ещё раз.",
            )
            return _html_page(
                "Ошибка подключения",
                "Не удалось получить доступ к Threads. Запустите подключение заново в Telegram.",
                success=False,
                status_code=502,
            )
        except Exception:
            logger.exception("Internal Threads OAuth callback failure")
            await _notify(
                bot,
                telegram_user_id,
                "Внутренняя ошибка при подключении Threads. Одноразовая ссылка уже "
                "погашена; запустите /connect_threads ещё раз.",
            )
            return _html_page(
                "Ошибка подключения",
                "Внутренняя ошибка при сохранении подключения. Повторите попытку в Telegram.",
                success=False,
                status_code=500,
            )

        account_label = (
            f"@{escape(completed.username)}"
            if completed.username
            else escape(completed.threads_user_id)
        )
        await _notify(
            bot,
            completed.telegram_user_id,
            "Threads успешно подключён.\n"
            f"Аккаунт: {account_label}\n"
            f"Основной chat id: {completed.target_chat_id}\n"
            "Токен сохранён в зашифрованном виде и будет обновляться автоматически.",
        )
        return _html_page(
            "Threads подключён",
            "Подключение завершено. Подтверждение отправлено в Telegram; это окно можно закрыть.",
            success=True,
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
                "Запрос не найден",
                "Проверьте ссылку подтверждения удаления данных.",
                success=False,
                status_code=404,
            )
        return _html_page(
            "Данные удалены",
            "Связанные с Threads учётные данные и привязки удалены.",
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
) -> HTMLResponse:
    color = "#15803d" if success else "#b91c1c"
    symbol = "✓" if success else "!"
    html = f"""<!doctype html>
<html lang="ru">
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
