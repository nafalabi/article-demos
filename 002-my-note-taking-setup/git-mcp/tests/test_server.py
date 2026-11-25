import pytest

from mcp import Client

from helpers import call_asgi, make_settings
from server import BearerTokenMiddleware, build_server, create_app


@pytest.mark.anyio
async def test_tools_and_unknown_vault(tmp_path):
    settings, _ = make_settings(tmp_path)
    async with Client(build_server(settings)) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert names == {
            "list_vaults",
            "list_notes",
            "read_note",
            "search_notes",
            "write_note",
            "delete_note",
            "git_status",
            "git_diff",
            "git_log",
            "git_commit",
            "git_push",
        }
        bad = await client.call_tool("git_status", {"vault": "nope"})
        assert bad.is_error is True
        assert "unknown_vault" in bad.content[0].text


@pytest.mark.anyio
async def test_note_write_then_commit_via_mcp_tools(tmp_path):
    settings, _ = make_settings(tmp_path)
    async with Client(build_server(settings)) as client:
        written = await client.call_tool(
            "write_note",
            {"vault": "My Vault", "path": "agent/hello.md", "content": "hi\n"},
        )
        assert written.is_error is False
        assert written.structured_content["created"] is True

        read_back = await client.call_tool(
            "read_note",
            {"vault": "My Vault", "path": "agent/hello.md"},
        )
        assert read_back.structured_content["content"] == "hi\n"

        committed = await client.call_tool(
            "git_commit",
            {"vault": "My Vault", "message": "add hello", "paths": ["agent/hello.md"]},
        )
        assert committed.is_error is False
        assert committed.structured_content["files"] == ["agent/hello.md"]


@pytest.mark.anyio
async def test_create_app_allows_internal_git_mcp_host(tmp_path):
    settings, _ = make_settings(tmp_path)
    app = create_app(settings)
    init_body = (
        b'{"jsonrpc":"2.0","id":1,"method":"initialize",'
        b'"params":{"protocolVersion":"2025-11-25","capabilities":{},'
        b'"clientInfo":{"name":"test","version":"0"}}}'
    )
    base_headers = [
        (b"authorization", b"Bearer test-token"),
        (b"content-type", b"application/json"),
        (b"accept", b"application/json, text/event-stream"),
    ]

    async with app.router.lifespan_context(app):
        status = await call_asgi(
            app,
            path="/mcp",
            headers=[*base_headers, (b"host", b"git-mcp:8080")],
            body=init_body,
        )
        assert status == 200

        rejected = await call_asgi(
            app,
            path="/mcp",
            headers=[*base_headers, (b"host", b"evil.example.com")],
            body=init_body,
        )
        assert rejected == 421


@pytest.mark.anyio
async def test_commit_tool_returns_author_and_hash(tmp_path):
    settings, vault = make_settings(tmp_path)
    (vault / "note.md").write_text("hi\n", encoding="utf-8")
    async with Client(build_server(settings)) as client:
        result = await client.call_tool(
            "git_commit",
            {"vault": "My Vault", "message": "hi", "paths": ["note.md"]},
        )
    assert result.is_error is False
    assert result.structured_content["author"] == "hermes <hermes@homelab.local>"
    assert result.structured_content["commit"]


@pytest.mark.anyio
async def test_bearer_middleware_blocks_mcp_without_token():
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["path"])

    middleware = BearerTokenMiddleware(inner, token="secret")

    status = await call_asgi(middleware, path="/mcp", headers=[])
    assert status == 401
    assert seen == []

    status = await call_asgi(
        middleware,
        path="/mcp",
        headers=[(b"authorization", b"Bearer secret")],
    )
    assert status is None
    assert seen == ["/mcp"]

    status = await call_asgi(
        middleware,
        path="/mcp",
        headers=[(b"authorization", b"Bearer wrong")],
    )
    assert status == 401
    assert seen == ["/mcp"]

    status = await call_asgi(middleware, path="/health", headers=[])
    assert status is None
    assert seen == ["/mcp", "/health"]
