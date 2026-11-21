import subprocess
from pathlib import Path

from config import Settings, parse_repos_conf


def run(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    )


def make_settings(
    tmp_path: Path,
    repos: str = "My Vault|Personal/notes.git|main",
    remote_base: str | None = None,
    diff_limit: int = 65536,
) -> tuple[Settings, Path]:
    vaults_root = tmp_path / "vaults"
    vaults_root.mkdir()
    repos_conf = tmp_path / "repos.conf"
    repos_conf.write_text(repos + "\n", encoding="utf-8")

    if remote_base is None:
        remote_base = f"file://{tmp_path}/remotes"

    settings = Settings(
        vaults_root=vaults_root,
        repos_conf=repos_conf,
        auth_token="test-token",
        git_user="ignis",
        git_token="test-git-token",
        remote_base=remote_base,
        author_name="hermes",
        author_email="hermes@homelab.local",
        public_host="ignis-mcp.example.test",
        diff_limit=diff_limit,
    )

    cfg = parse_repos_conf(repos_conf)[0]
    vault = vaults_root / cfg.name
    vault.mkdir()

    remote_root = Path(remote_base.removeprefix("file://"))
    bare = remote_root / cfg.remote_path
    bare.mkdir(parents=True)
    run(bare, "init", "--bare", "-b", cfg.branch)

    run(vault, "init", "-b", cfg.branch)
    run(vault, "config", "user.name", "seed")
    run(vault, "config", "user.email", "seed@example.com")
    (vault / "a.md").write_text("a\n", encoding="utf-8")
    (vault / "b.md").write_text("b\n", encoding="utf-8")
    (vault / "gone.md").write_text("gone\n", encoding="utf-8")
    run(vault, "add", ".")
    run(vault, "commit", "-m", "seed")
    run(vault, "remote", "add", "origin", f"{remote_base.rstrip('/')}/{cfg.remote_path}")
    run(vault, "push", "-u", "origin", cfg.branch)

    return settings, vault


async def call_asgi(app, path: str, headers: list[tuple[bytes, bytes]]) -> int | None:
    response: dict[str, object] = {}

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.start":
            response["status"] = message["status"]

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }

    await app(scope, receive, send)
    return response.get("status")
