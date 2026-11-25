import pytest

from helpers import make_settings
import notes
from vaults import GitOperationError


def test_list_notes_excludes_dotgit_obsidian_symlinks_and_non_notes(tmp_path):
    settings, vault = make_settings(tmp_path)
    (vault / "sub").mkdir()
    (vault / "sub" / "idea.MD").write_text("# Idea\n", encoding="utf-8")
    (vault / "scratch.txt").write_text("hi\n", encoding="utf-8")
    (vault / "board.canvas").write_text("{}\n", encoding="utf-8")
    (vault / "image.png").write_bytes(b"\x89PNG")
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian" / "app.md").write_text("internal\n", encoding="utf-8")
    (vault / "link.md").symlink_to(vault / "a.md")

    listed = notes.list_notes(settings, "My Vault")
    assert listed["vault"] == "My Vault"
    paths = [item["path"] for item in listed["notes"]]
    assert paths == ["a.md", "b.md", "board.canvas", "gone.md", "scratch.txt", "sub/idea.MD"]

    sub_listed = notes.list_notes(settings, "My Vault", prefix="sub")
    assert [item["path"] for item in sub_listed["notes"]] == ["sub/idea.MD"]


def test_read_note_returns_content_and_enforces_guardrails(tmp_path):
    settings, vault = make_settings(tmp_path)
    (vault / "sub").mkdir()
    (vault / "sub" / "daily.md").write_text("hello world\n", encoding="utf-8")
    (vault / "binary.md").write_bytes(b"\xff\xfe\x00")
    (vault / "big.md").write_text("x" * 70000, encoding="utf-8")
    (vault / "secret.png").write_bytes(b"png")

    result = notes.read_note(settings, "My Vault", "sub/daily.md")
    assert result == {
        "vault": "My Vault",
        "path": "sub/daily.md",
        "content": "hello world\n",
        "size_bytes": 12,
    }

    for bad_path in (
        "",
        "/etc/passwd",
        "../outside.md",
        ".git/config",
        ".obsidian/app.md",
        "secret.png",
        "no_ext",
    ):
        with pytest.raises(GitOperationError) as exc:
            notes.read_note(settings, "My Vault", bad_path)
        assert exc.value.code == "invalid_path"

    with pytest.raises(GitOperationError) as exc:
        notes.read_note(settings, "My Vault", "missing.md")
    assert exc.value.code == "not_found"

    with pytest.raises(GitOperationError) as exc:
        notes.read_note(settings, "My Vault", "binary.md")
    assert exc.value.code == "invalid_encoding"

    small_settings = type(settings)(**{**settings.__dict__, "note_size_limit": 100})
    with pytest.raises(GitOperationError) as exc:
        notes.read_note(small_settings, "My Vault", "big.md")
    assert exc.value.code == "note_too_large"


def test_search_notes_matches_paths_and_content_case_insensitively(tmp_path):
    settings, vault = make_settings(tmp_path)
    (vault / "projects").mkdir()
    (vault / "projects" / "Tunnel.md").write_text("OpenAI integration\n", encoding="utf-8")
    (vault / "daily.md").write_text("Line one\nCheck TUNNEL status\n", encoding="utf-8")
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian" / "tunnel.md").write_text("tunnel\n", encoding="utf-8")

    res = notes.search_notes(settings, "My Vault", "tunnel")
    assert res["vault"] == "My Vault"
    assert res["query"] == "tunnel"
    assert res["truncated"] is False
    assert res["matches"] == [
        {"path": "daily.md", "line": 2, "snippet": "Check TUNNEL status"},
        {"path": "projects/Tunnel.md", "line": None, "snippet": "projects/Tunnel.md"},
    ]

    with pytest.raises(GitOperationError) as exc:
        notes.search_notes(settings, "My Vault", "   ")
    assert exc.value.code == "invalid_query"
