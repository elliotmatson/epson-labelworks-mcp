"""The static-token middleware must never look like an OAuth-protected server."""

import asyncio

from epson_labelworks.server import BearerAuth


async def _inner(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"inner"})


def _call(path: str, auth: bytes | None = None, token: str | None = "secret"):
    sent = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request", "body": b""}

    headers = [(b"authorization", auth)] if auth else []
    scope = {"type": "http", "path": path, "headers": headers}
    asyncio.run(BearerAuth(_inner, token)(scope, receive, send))
    start = sent[0]
    return start["status"], dict(start["headers"])


def test_discovery_paths_are_plain_404_without_token():
    for p in ("/.well-known/oauth-protected-resource",
              "/.well-known/oauth-protected-resource/mcp",
              "/.well-known/oauth-authorization-server"):
        status, headers = _call(p)
        assert status == 404 and b"www-authenticate" not in headers


def test_bad_token_is_403_without_challenge():
    for auth in (None, b"Bearer wrong", b"secret"):
        status, headers = _call("/mcp", auth)
        assert status == 403 and b"www-authenticate" not in headers


def _call_headers(headers, token="secret"):
    sent = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request", "body": b""}

    scope = {"type": "http", "path": "/mcp", "method": "POST", "headers": headers}
    asyncio.run(BearerAuth(_inner, token)(scope, receive, send))
    return sent[0]["status"]


def test_api_key_header_works_when_gateway_overwrites_authorization():
    # Obot-style: its own bearer in Authorization, our token in X-API-Key
    assert _call_headers([(b"authorization", b"Bearer obot-internal"),
                          (b"x-api-key", b"secret")]) == 200
    assert _call_headers([(b"x-api-key", b"wrong")]) == 403


def test_good_token_and_open_mode_pass_through():
    assert _call("/mcp", b"Bearer secret")[0] == 200
    assert _call("/mcp", None, token=None)[0] == 200
    assert _call("/healthz")[0] == 200
