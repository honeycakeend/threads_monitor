from urllib.parse import parse_qs

import httpx
import pytest

from bot.threads_oauth import ThreadsOAuthClient, ThreadsOAuthError


@pytest.mark.asyncio
async def test_oauth_client_uses_documented_exchange_and_refresh_requests():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/oauth/access_token":
            assert request.method == "POST"
            form = parse_qs(request.content.decode())
            assert form == {
                "client_id": ["app-id"],
                "client_secret": ["app-secret"],
                "grant_type": ["authorization_code"],
                "redirect_uri": ["https://example.test/callback"],
                "code": ["authorization-code"],
            }
            return httpx.Response(
                200,
                json={"access_token": "short-token", "user_id": "42"},
            )
        if request.url.path == "/access_token":
            assert request.url.params["grant_type"] == "th_exchange_token"
            assert request.url.params["client_secret"] == "app-secret"
            assert "access_token" not in request.url.params
            assert request.headers["authorization"] == "Bearer short-token"
            return httpx.Response(
                200,
                json={
                    "access_token": "long-token",
                    "token_type": "bearer",
                    "expires_in": 5_184_000,
                },
            )
        if request.url.path == "/refresh_access_token":
            assert request.url.params["grant_type"] == "th_refresh_token"
            assert "access_token" not in request.url.params
            assert request.headers["authorization"] == "Bearer long-token"
            return httpx.Response(
                200,
                json={
                    "access_token": "refreshed-token",
                    "token_type": "bearer",
                    "expires_in": 5_184_000,
                },
            )
        if request.url.path == "/v1.0/me":
            assert request.headers["authorization"] == "Bearer long-token"
            assert "access_token" not in request.url.params
            return httpx.Response(200, json={"id": "42", "username": "agent"})
        raise AssertionError(f"Unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = ThreadsOAuthClient(
            app_id="app-id",
            app_secret="app-secret",
            redirect_uri="https://example.test/callback",
            http_client=http_client,
        )
        short = await client.exchange_authorization_code("authorization-code")
        long = await client.exchange_long_lived_token(short.access_token)
        profile = await client.get_profile(long.access_token)
        refreshed = await client.refresh_long_lived_token(long.access_token)

    assert short.user_id == profile.id == "42"
    assert long.access_token == "long-token"
    assert refreshed.access_token == "refreshed-token"
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_oauth_error_logs_only_endpoint_and_numeric_meta_codes():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": 190,
                    "error_subcode": "460",
                    "message": "provider echoed authorization-code and app-secret",
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = ThreadsOAuthClient(
            app_id="app-id",
            app_secret="app-secret",
            redirect_uri="https://example.test/callback",
            http_client=http_client,
        )
        with pytest.raises(ThreadsOAuthError) as exc_info:
            await client.exchange_authorization_code("authorization-code")

    message = str(exc_info.value)
    assert message == (
        "Meta rejected the OAuth request at /oauth/access_token "
        "(HTTP 400; code=190; error_subcode=460)"
    )
    assert "authorization-code" not in message
    assert "app-secret" not in message


@pytest.mark.asyncio
async def test_long_lived_exchange_retries_observed_meta_transport_rejection():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            assert request.headers["authorization"] == "Bearer short-token"
            assert "access_token" not in request.url.params
            return httpx.Response(
                400,
                json={"error": {"code": 452, "error_subcode": 4_279_019}},
            )

        assert "authorization" not in request.headers
        assert request.url.params["access_token"] == "short-token"
        return httpx.Response(
            200,
            json={
                "access_token": "long-token",
                "token_type": "bearer",
                "expires_in": 5_184_000,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = ThreadsOAuthClient(
            app_id="app-id",
            app_secret="app-secret",
            redirect_uri="https://example.test/callback",
            http_client=http_client,
        )
        token = await client.exchange_long_lived_token("short-token")

    assert token.access_token == "long-token"
    assert len(calls) == 2
