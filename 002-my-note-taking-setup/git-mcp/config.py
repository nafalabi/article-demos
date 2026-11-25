from dataclasses import dataclass
import os
from pathlib import Path
import re


_CREDENTIAL_URL = re.compile(r"^(https?://)[^/@:]+:[^/@]*@")


@dataclass(frozen=True)
class VaultConfig:
    name: str
    remote_path: str
    branch: str


@dataclass(frozen=True)
class Settings:
    vaults_root: Path
    repos_conf: Path
    auth_token: str
    git_user: str
    git_token: str
    remote_base: str
    author_name: str
    author_email: str
    public_host: str
    diff_limit: int = 65536
    note_size_limit: int = 262144
    attachment_size_limit: int | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        auth_token = os.environ.get("MCP_AUTH_TOKEN", "")
        public_host = os.environ.get("MCP_PUBLIC_HOST", "")
        if not auth_token:
            raise RuntimeError("MCP_AUTH_TOKEN is required")
        if len(auth_token) < 32 or "CHANGE_ME" in auth_token:
            raise RuntimeError(
                "MCP_AUTH_TOKEN must be at least 32 characters; generate one with "
                "'openssl rand -hex 32'"
            )
        if not public_host:
            raise RuntimeError("MCP_PUBLIC_HOST is required")

        return cls(
            vaults_root=Path(os.environ.get("VAULTS_ROOT", "/vaults")),
            repos_conf=Path(
                os.environ.get("REPOS_CONF", "/opt/git-ui/repos.conf")
            ),
            auth_token=auth_token,
            git_user=os.environ.get("GIT_USER", ""),
            git_token=os.environ.get("MCP_GIT_TOKEN", ""),
            remote_base=os.environ.get(
                "GIT_REMOTE_BASE_URL", "CHANGE_ME_GIT_BASE_URL"
            ),
            author_name=os.environ.get("MCP_GIT_AUTHOR_NAME", "hermes"),
            author_email=os.environ.get(
                "MCP_GIT_AUTHOR_EMAIL", "hermes@homelab.local"
            ),
            public_host=public_host,
            diff_limit=int(os.environ.get("DIFF_LIMIT", "65536")),
            note_size_limit=int(os.environ.get("NOTE_SIZE_LIMIT", "262144")),
            attachment_size_limit=_parse_optional_size_limit(
                os.environ.get("ATTACHMENT_SIZE_LIMIT")
            ),
        )


def _parse_optional_size_limit(raw: str | None) -> int | None:
    if raw is None:
        return None
    stripped = raw.strip()
    if not stripped or stripped.lower() in {"0", "none", "unlimited"}:
        return None
    value = int(stripped)
    if value < 0:
        raise ValueError("ATTACHMENT_SIZE_LIMIT must be non-negative")
    return value


def parse_repos_conf(path: Path) -> list[VaultConfig]:
    vaults: list[VaultConfig] = []
    if not path.is_file():
        return vaults

    for lineno, raw in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) != 3 or not all(parts):
            raise ValueError(f"{path}:{lineno}: expected name|remote|branch")
        vaults.append(VaultConfig(*parts))
    return vaults


def clean_origin_url(url: str) -> str:
    return _CREDENTIAL_URL.sub(r"\1", url)


def build_remote_url(remote_base: str, remote_path: str) -> str:
    base = clean_origin_url(remote_base).rstrip("/")
    return f"{base}/{remote_path.lstrip('/')}"


def remote_is_configured(url: str) -> bool:
    return bool(url) and "CHANGE_ME" not in url
