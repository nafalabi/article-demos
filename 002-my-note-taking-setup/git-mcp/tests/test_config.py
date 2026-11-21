import pytest

from config import Settings, VaultConfig, build_remote_url, clean_origin_url, parse_repos_conf


def test_parse_repos_conf_preserves_spaces_and_unicode(tmp_path):
    conf = tmp_path / "repos.conf"
    conf.write_text(
        "# comment\nMy Vault|Personal/notes.git|main\n\nÜnïcode|X/y.git|notes\n",
        encoding="utf-8",
    )
    assert parse_repos_conf(conf) == [
        VaultConfig("My Vault", "Personal/notes.git", "main"),
        VaultConfig("Ünïcode", "X/y.git", "notes"),
    ]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://u:p@git.example.com/x.git", "https://git.example.com/x.git"),
        ("http://u:p@h/x.git", "http://h/x.git"),
        ("ssh://git@git.example.com/x.git", "ssh://git@git.example.com/x.git"),
        ("https://user@git.example.com/x.git", "https://user@git.example.com/x.git"),
    ],
)
def test_clean_origin_url(url, expected):
    assert clean_origin_url(url) == expected


def test_build_remote_url_handles_trailing_slash():
    assert build_remote_url("https://h/", "A/b.git") == "https://h/A/b.git"


def test_build_remote_url_strips_credentials_from_base():
    assert (
        build_remote_url("https://u:p@git.example.com/", "A/b.git")
        == "https://git.example.com/A/b.git"
    )


def test_settings_from_env_rejects_short_or_placeholder_token(monkeypatch):
    monkeypatch.setenv("MCP_PUBLIC_HOST", "ignis-mcp.example.test")
    for token in ("short", "CHANGE_ME_MCP_AUTH_TOKEN"):
        monkeypatch.setenv("MCP_AUTH_TOKEN", token)
        with pytest.raises(RuntimeError):
            Settings.from_env()

    monkeypatch.setenv("MCP_AUTH_TOKEN", "a" * 32)
    assert Settings.from_env().auth_token == "a" * 32
