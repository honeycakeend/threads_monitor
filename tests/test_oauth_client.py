from urllib.parse import parse_qs

import httpx
import pytest

from bot.threads_oauth import ThreadsOAuthClient


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
