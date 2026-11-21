from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import threading

from config import (
    Settings,
    VaultConfig,
    build_remote_url,
    clean_origin_url,
    parse_repos_conf,
    remote_is_configured,
)


CREDENTIAL_HELPER = (
    """!f() { printf 'username=%s\\npassword=%s\\n' """
    """"$GIT_USER" "$MCP_GIT_TOKEN"; }; f"""
)

_GIT_CONFIG = [
    "-c",
    "safe.directory=*",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.pager=cat",
    "-c",
    f"credential.helper={CREDENTIAL_HELPER}",
]

_URL = re.compile(r"https?://\S+")
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class GitOperationError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _sanitize(text: str) -> str:
    return _URL.sub("<remote>", text.strip())[:2000]


def _lock_for(path: Path) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(str(path), threading.Lock())


def _classify(stderr: str) -> str:
    lowered = stderr.lower()
    if "index.lock" in lowered:
        return "locked"
    if any(
        marker in lowered
        for marker in ("non-fast-forward", "fetch first", "[rejected]", "failed to push some refs")
    ):
        return "push_rejected"
    if any(
        marker in lowered
        for marker in (
            "nothing to commit",
            "nothing added to commit",
            "no changes added to commit",
        )
    ):
        return "no_changes"
    return "git_error"


def run_git(
    settings: Settings,
    cwd: Path,
    args: list[str],
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_USER": settings.git_user,
            "MCP_GIT_TOKEN": settings.git_token,
            "GIT_AUTHOR_NAME": settings.author_name,
            "GIT_AUTHOR_EMAIL": settings.author_email,
            "GIT_COMMITTER_NAME": settings.author_name,
            "GIT_COMMITTER_EMAIL": settings.author_email,
        }
    )
    try:
        result = subprocess.run(
            ["git", *_GIT_CONFIG, *args],
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitOperationError("timeout", f"git {' '.join(args)} timed out") from exc

    if result.returncode != 0:
        detail = _sanitize(result.stderr or result.stdout or "git failed")
        raise GitOperationError(_classify(result.stderr or result.stdout), detail)
    return result


def _try_git(
    settings: Settings, cwd: Path, args: list[str], timeout: int = 30
) -> subprocess.CompletedProcess[str] | None:
    try:
        return run_git(settings, cwd, args, timeout=timeout)
    except GitOperationError:
        return None


def _origin(settings: Settings, cwd: Path) -> str | None:
    result = _try_git(settings, cwd, ["remote", "get-url", "origin"])
    if result is None:
        return None
    return result.stdout.strip() or None


def resolve_vault(settings: Settings, name: str) -> VaultConfig:
    for cfg in parse_repos_conf(settings.repos_conf):
        if cfg.name == name:
            return cfg
    raise GitOperationError("unknown_vault", f"no vault named {name!r}")


def vault_path(settings: Settings, cfg: VaultConfig) -> Path:
    root = settings.vaults_root.resolve()
    candidate = (settings.vaults_root / cfg.name).resolve()
    if candidate == root or root not in candidate.parents:
        raise GitOperationError("invalid_path", "vault name escapes the vault root")
    return candidate


def _is_tracked(directory: Path, rel: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(directory), "ls-files", "--error-unmatch", "--", rel],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def validate_commit_path(directory: Path, rel: str) -> Path:
    path = Path(rel)
    if path.is_absolute() or ".." in path.parts or ".git" in path.parts:
        raise GitOperationError("invalid_path", f"unsafe commit path: {rel!r}")
    if not path.parts:
        raise GitOperationError("invalid_path", "empty commit path")

    full = directory / path
    if full.is_symlink():
        raise GitOperationError("invalid_path", f"symlink rejected: {rel!r}")
    if full.exists() and not full.is_file():
        raise GitOperationError("invalid_path", f"not a regular file: {rel!r}")

    root = directory.resolve()
    resolved = full.resolve()
    if root not in resolved.parents:
        raise GitOperationError("invalid_path", f"path escapes the vault: {rel!r}")

    if not full.exists() and not _is_tracked(directory, rel):
        raise GitOperationError("invalid_path", f"no such file: {rel!r}")
    return full


def validate_diff_path(directory: Path, rel: str) -> str:
    path = Path(rel)
    if path.is_absolute() or ".." in path.parts or ".git" in path.parts:
        raise GitOperationError("invalid_path", f"unsafe diff path: {rel!r}")
    resolved = (directory / path).resolve()
    root = directory.resolve()
    if resolved != root and root not in resolved.parents:
        raise GitOperationError("invalid_path", f"path escapes the vault: {rel!r}")
    return rel


def scrub_all_origins(settings: Settings) -> None:
    if not settings.vaults_root.is_dir():
        return
    for config in sorted(settings.vaults_root.glob("*/.git/config")):
        directory = config.parent.parent
        origin = _origin(settings, directory)
        if origin is None:
            continue
        cleaned = clean_origin_url(origin)
        if cleaned != origin:
            _try_git(settings, directory, ["remote", "set-url", "origin", cleaned])


def prepare_vault(settings: Settings, cfg: VaultConfig) -> None:
    directory = vault_path(settings, cfg)
    if not directory.is_dir():
        return

    remote = build_remote_url(settings.remote_base, cfg.remote_path)
    configured = remote_is_configured(remote)

    if not (directory / ".git").is_dir():
        run_git(settings, directory, ["init", "-b", cfg.branch])
        if configured:
            run_git(settings, directory, ["remote", "add", "origin", remote])
        return

    origin = _origin(settings, directory)
    if configured:
        if origin is None:
            run_git(settings, directory, ["remote", "add", "origin", remote])
        elif origin != remote:
            run_git(settings, directory, ["remote", "set-url", "origin", remote])
    elif origin is not None:
        cleaned = clean_origin_url(origin)
        if cleaned != origin:
            run_git(settings, directory, ["remote", "set-url", "origin", cleaned])


def list_vaults(settings: Settings) -> list[dict]:
    vaults = []
    for cfg in parse_repos_conf(settings.repos_conf):
        directory = settings.vaults_root / cfg.name
        initialized = (directory / ".git").is_dir()
        origin = _origin(settings, directory) if initialized else None
        vaults.append(
            {
                "name": cfg.name,
                "branch": cfg.branch,
                "initialized": initialized,
                "remote_configured": bool(origin)
                and remote_is_configured(clean_origin_url(origin)),
            }
        )
    return vaults


def status(settings: Settings, name: str) -> dict:
    cfg = resolve_vault(settings, name)
    directory = vault_path(settings, cfg)
    with _lock_for(directory):
        prepare_vault(settings, cfg)
        result = run_git(
            settings, directory, ["status", "--porcelain=v2", "--branch"]
        )

    branch = None
    upstream = None
    ahead = 0
    behind = 0
    changes: list[str] = []
    for line in result.stdout.splitlines():
        if line.startswith("# branch.head "):
            branch = line.removeprefix("# branch.head ")
        elif line.startswith("# branch.upstream "):
            upstream = line.removeprefix("# branch.upstream ")
        elif line.startswith("# branch.ab "):
            match = re.fullmatch(r"# branch\.ab \+(\d+) -(\d+)", line)
            if match:
                ahead, behind = int(match.group(1)), int(match.group(2))
        elif line:
            changes.append(line)

    return {
        "branch": branch,
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
        "clean": not changes,
        "changes": changes,
    }


def diff(
    settings: Settings, name: str, staged: bool, path: str | None
) -> dict:
    cfg = resolve_vault(settings, name)
    directory = vault_path(settings, cfg)
    with _lock_for(directory):
        prepare_vault(settings, cfg)
        args = ["diff", "--no-ext-diff", "--no-color"]
        if staged:
            args.append("--cached")
        if path:
            args.extend(["--", validate_diff_path(directory, path)])
        result = run_git(settings, directory, args)

    raw = result.stdout
    encoded = raw.encode("utf-8", "replace")
    truncated = len(encoded) > settings.diff_limit
    if truncated:
        raw = encoded[: settings.diff_limit].decode("utf-8", "ignore")
    return {"diff": raw, "truncated": truncated}


def log(settings: Settings, name: str, limit: int) -> dict:
    cfg = resolve_vault(settings, name)
    directory = vault_path(settings, cfg)
    limit = max(1, min(50, int(limit)))
    with _lock_for(directory):
        prepare_vault(settings, cfg)
        result = run_git(
            settings,
            directory,
            [
                "log",
                f"-n{limit}",
                "--format=%h%x09%an%x09%ae%x09%aI%x09%s",
            ],
        )

    commits = []
    for line in result.stdout.splitlines():
        parts = line.split("\t", 4)
        if len(parts) != 5:
            continue
        commits.append(
            {
                "hash": parts[0],
                "author": parts[1],
                "email": parts[2],
                "date": parts[3],
                "subject": parts[4],
            }
        )
    return {"commits": commits}


def commit(
    settings: Settings, name: str, message: str, paths: list[str]
) -> dict:
    if not message or not message.strip():
        raise GitOperationError("no_changes", "commit message is required")
    if len(message) > 2000:
        raise GitOperationError("invalid_path", "commit message is too long")
    if not paths:
        raise GitOperationError("invalid_path", "at least one path is required")
    if len(paths) > 100:
        raise GitOperationError("invalid_path", "too many paths")

    cfg = resolve_vault(settings, name)
    directory = vault_path(settings, cfg)

    with _lock_for(directory):
        prepare_vault(settings, cfg)
        if not (directory / ".git").is_dir():
            raise GitOperationError("not_initialized", f"vault {name!r} is not a repo")

        validated = [(rel, validate_commit_path(directory, rel)) for rel in paths]
        intent_added: list[str] = []
        for rel, full in validated:
            if full.exists() and not _is_tracked(directory, rel):
                run_git(settings, directory, ["add", "--intent-to-add", "--", rel])
                intent_added.append(rel)

        try:
            run_git(
                settings,
                directory,
                ["commit", "--only", "-m", message, "--", *[rel for rel, _ in validated]],
            )
        except GitOperationError:
            for rel in intent_added:
                _try_git(
                    settings,
                    directory,
                    ["update-index", "--force-remove", "--", rel],
                )
            raise

        head = run_git(settings, directory, ["rev-parse", "HEAD"]).stdout.strip()

    return {
        "commit": head,
        "files": [rel for rel, _ in validated],
        "author": f"{settings.author_name} <{settings.author_email}>",
    }


def push(settings: Settings, name: str) -> dict:
    cfg = resolve_vault(settings, name)
    directory = vault_path(settings, cfg)

    with _lock_for(directory):
        prepare_vault(settings, cfg)
        if not (directory / ".git").is_dir():
            raise GitOperationError("not_initialized", f"vault {name!r} is not a repo")

        current = run_git(
            settings, directory, ["rev-parse", "--abbrev-ref", "HEAD"]
        ).stdout.strip()
        if current != cfg.branch:
            raise GitOperationError(
                "branch_mismatch",
                f"vault is on {current!r}, configured branch is {cfg.branch!r}",
            )

        origin = _origin(settings, directory)
        if origin is None:
            raise GitOperationError("no_remote", f"vault {name!r} has no origin")
        origin = clean_origin_url(origin)
        if not remote_is_configured(origin):
            raise GitOperationError(
                "no_remote", f"vault {name!r} remote is not configured"
            )
        if origin.startswith(("http://", "https://")) and not settings.git_token:
            raise GitOperationError(
                "no_credentials", "MCP_GIT_TOKEN is empty"
            )

        result = run_git(
            settings,
            directory,
            ["push", "--porcelain", "--no-verify", "origin", cfg.branch],
            timeout=60,
        )

    detail = _sanitize(result.stdout + result.stderr)
    return {"branch": cfg.branch, "remote": "origin", "result": detail}
