from __future__ import annotations

import hmac
import logging

import uvicorn
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from config import Settings, parse_repos_conf
import notes
import vaults

logger = logging.getLogger("git-mcp")


class BearerTokenMiddleware:
    def __init__(self, app: ASGIApp, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] == "http" and (path == "/mcp" or path.startswith("/mcp/")):
            supplied = Headers(scope=scope).get("authorization", "")
            if not hmac.compare_digest(
                supplied.encode("utf-8"), f"Bearer {self.token}".encode("utf-8")
            ):
                response = JSONResponse(
                    {"error": "unauthorized"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_server(settings: Settings) -> MCPServer:
    mcp = MCPServer("git-mcp")

    def call(name: str, vault: str | None, fn, *args, **kwargs):
        try:
            result = fn(settings, *args, **kwargs)
        except vaults.GitOperationError as err:
            logger.info("tool=%s vault=%s outcome=%s", name, vault or "-", err.code)
            raise ToolError(f"{err.code}: {err.message}") from err
        except Exception:
            logger.exception("tool=%s vault=%s outcome=crash", name, vault or "-")
            raise
        extra = ""
        if isinstance(result, dict) and result.get("commit"):
            extra = f" commit={result['commit']}"
        logger.info(
            "tool=%s vault=%s outcome=ok%s", name, vault or "-", extra
        )
        return result

    @mcp.tool()
    def list_vaults() -> dict[str, object]:
        """List configured note vaults and whether each has a git remote."""
        return {"vaults": call("list_vaults", None, vaults.list_vaults)}

    @mcp.tool()
    def list_notes(vault: str, prefix: str | None = None) -> dict[str, object]:
        """List note files (.md, .markdown, .txt, .canvas) in a vault, excluding .git and .obsidian."""
        return call("list_notes", vault, notes.list_notes, vault, prefix)

    @mcp.tool()
    def read_note(vault: str, path: str) -> dict[str, object]:
        """Read a UTF-8 note file from a vault."""
        return call("read_note", vault, notes.read_note, vault, path)

    @mcp.tool()
    def search_notes(
        vault: str, query: str, prefix: str | None = None, limit: int = 20
    ) -> dict[str, object]:
        """Case-insensitively search note filenames and contents in a vault."""
        return call("search_notes", vault, notes.search_notes, vault, query, prefix, limit)

    @mcp.tool()
    def write_note(
        vault: str, path: str, content: str, overwrite: bool = True
    ) -> dict[str, object]:
        """Create or update a UTF-8 note file in a vault."""
        return call("write_note", vault, notes.write_note, vault, path, content, overwrite)

    @mcp.tool()
    def delete_note(vault: str, path: str) -> dict[str, object]:
        """Delete a regular note file from a vault."""
        return call("delete_note", vault, notes.delete_note, vault, path)

    @mcp.tool()
    def git_status(vault: str) -> dict[str, object]:
        """Show branch, upstream, ahead/behind, and changed paths for a vault.

        Does not fetch; ahead/behind reflect the last fetch or successful push.
        """
        return call("git_status", vault, vaults.status, vault)

    @mcp.tool()
    def git_diff(vault: str, staged: bool = False, path: str | None = None) -> dict[str, object]:
        """Show a unified diff for a vault, optionally staged and limited to one literal path.

        Use an exact file or directory path; globs and pathspec magic are rejected.
        """
        return call("git_diff", vault, vaults.diff, vault, staged, path)

    @mcp.tool()
    def git_log(vault: str, limit: int = 10) -> dict[str, object]:
        """Show recent commits for a vault, newest first."""
        return call("git_log", vault, vaults.log, vault, limit)

    @mcp.tool()
    def git_commit(vault: str, message: str, paths: list[str]) -> dict[str, object]:
        """Commit specific regular files in a vault with the agent author identity.

        Paths are literal, must not contain directories or symlinks, and unrelated
        staged changes are left untouched.
        """
        return call("git_commit", vault, vaults.commit, vault, message, paths)

    @mcp.tool()
    def git_push(vault: str) -> dict[str, object]:
        """Push the vault's configured branch; never force-pushes."""
        return call("git_push", vault, vaults.push, vault)

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    return mcp


def create_app(settings: Settings):
    mcp = build_server(settings)
    security = TransportSecuritySettings(
        allowed_hosts=[
            settings.public_host,
            f"{settings.public_host}:*",
            "git-mcp",
            "git-mcp:*",
            "127.0.0.1",
            "127.0.0.1:*",
            "localhost",
            "localhost:*",
        ],
    )
    app = mcp.streamable_http_app(transport_security=security)
    app.add_middleware(BearerTokenMiddleware, token=settings.auth_token)
    return app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = Settings.from_env()
    try:
        vaults.scrub_all_origins(settings)
        for cfg in parse_repos_conf(settings.repos_conf):
            vaults.prepare_vault(settings, cfg)
    except Exception:
        logger.exception("startup vault preparation failed")

    uvicorn.run(create_app(settings), host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
