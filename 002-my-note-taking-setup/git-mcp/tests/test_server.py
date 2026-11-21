import pytest

from mcp import Client

from helpers import call_asgi, make_settings
from server import BearerTokenMiddleware, build_server


@pytest.mark.anyio
async def test_tools_and_unknown_vault(tmp_path):
    settings, _ = make_settings(tmp_path)
    async with Client(build_server(settings)) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert names == {
            "list_vaults",
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

    status = await call_asgi(middleware, path="/health", headers=[])
    assert status is None
    assert seen == ["/mcp", "/health"]
