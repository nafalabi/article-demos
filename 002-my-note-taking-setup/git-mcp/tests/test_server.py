import pytest

from mcp import Client

from helpers import call_asgi, make_settings
from server import BearerTokenMiddleware, build_server, create_app


@pytest.mark.anyio
async def test_tools_and_unknown_vault(tmp_path):
    settings, _ = make_settings(tmp_path)
    async with Client(build_server(settings)) as client:
        tools = (await client.list_tools()).tools
        names = {tool.name for tool in tools}
        assert names == {
            "list_vaults",
            "list_notes",
            "read_note",
            "search_notes",
            "write_note",
            "delete_note",
            "list_attachments",
            "read_attachment",
            "upload_attachment",
            "delete_attachment",
            "git_status",
            "git_diff",
            "git_log",
            "git_commit",
            "git_push",
        }
        upload_tool = next(tool for tool in tools if tool.name == "upload_attachment")
        assert upload_tool.meta == {"openai/fileParams": ["file"]}
        assert upload_tool.input_schema["$defs"]["OpenAIFile"] == {
            "type": "object",
            "properties": {
                "download_url": {"type": "string"},
                "file_id": {"type": "string"},
                "mime_type": {"type": "string"},
                "file_name": {"type": "string"},
            },
            "required": ["download_url", "file_id"],
            "additionalProperties": False,
        }
        assert upload_tool.input_schema["properties"]["file"] == {
            "$ref": "#/$defs/OpenAIFile",
            "title": "File",
            "description": "ChatGPT file reference resolved via openai/fileParams.",
        }
        wire = upload_tool.model_dump(by_alias=True, exclude_none=True)
        assert wire["_meta"] == {"openai/fileParams": ["file"]}
        assert "anyOf" not in wire["inputSchema"]["properties"]["file"]
        bad = await client.call_tool("git_status", {"vault": "nope"})
        assert bad.is_error is True
        assert "unknown_vault" in bad.content[0].text


@pytest.mark.anyio
async def test_upload_attachment_mcp_accepts_dict_and_string_file_param(tmp_path, monkeypatch):
    import socket
    import urllib.request
    import attachments

    def fake_getaddrinfo(host, port, type=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    class FakeResp:
        headers = {}

        def __init__(self, data: bytes):
            self._chunks = [data]

        def read(self, size=-1):
            return self._chunks.pop(0) if self._chunks else b""

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakeOpener:
        def open(self, req, timeout=30):
            return FakeResp(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(urllib.request, "build_opener", lambda *a, **kw: FakeOpener())

    settings, vault = make_settings(tmp_path)
    async with Client(build_server(settings)) as client:
        ok_dict = await client.call_tool(
            "upload_attachment",
            {
                "vault": "My Vault",
                "path": "chatgpt/dict.png",
                "file": {
                    "file_id": "file-1",
                    "download_url": "https://files.oaiusercontent.com/dict.png",
                    "mime_type": "image/png",
                    "file_name": "dict.png",
                },
            },
        )
        assert ok_dict.is_error is False
        assert (vault / "chatgpt" / "dict.png").exists()

        ok_str = await client.call_tool(
            "upload_attachment",
            {
                "vault": "My Vault",
                "path": "chatgpt/str.png",
                "file": '{"file_id": "file-2", "download_url": "https://files.oaiusercontent.com/str.png"}',
            },
        )
        assert ok_str.is_error is False
        assert (vault / "chatgpt" / "str.png").exists()

        unresolved = await client.call_tool(
            "upload_attachment",
            {
                "vault": "My Vault",
                "path": "chatgpt/bad.png",
                "file": "/mnt/data/chart.png",
            },
        )
        assert unresolved.is_error is True
        assert "invalid_file" in unresolved.content[0].text


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

        uploaded = await client.call_tool(
            "upload_attachment",
            {
                "vault": "My Vault",
                "path": "assets/hello.png",
                "content_base64": "iVBORw0KGgoAAAANSUhEUg==",
            },
        )
        assert uploaded.is_error is False
        assert uploaded.structured_content["mime_type"] == "image/png"

        committed = await client.call_tool(
            "git_commit",
            {
                "vault": "My Vault",
                "message": "add hello",
                "paths": ["agent/hello.md", "assets/hello.png"],
            },
        )
        assert committed.is_error is False
        assert committed.structured_content["files"] == [
            "agent/hello.md",
            "assets/hello.png",
        ]


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
