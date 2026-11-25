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


import vaults


def test_write_note_creates_overwrites_and_respects_guardrails(tmp_path):
    settings, vault = make_settings(tmp_path)

    created = notes.write_note(settings, "My Vault", "folder/new.md", "# Title\n")
    assert created == {
        "vault": "My Vault",
        "path": "folder/new.md",
        "created": True,
        "size_bytes": 8,
    }
    assert (vault / "folder" / "new.md").read_text(encoding="utf-8") == "# Title\n"

    with pytest.raises(GitOperationError) as exc:
        notes.write_note(settings, "My Vault", "folder/new.md", "nope", overwrite=False)
    assert exc.value.code == "already_exists"

    updated = notes.write_note(settings, "My Vault", "folder/new.md", "updated\n", overwrite=True)
    assert updated["created"] is False
    assert (vault / "folder" / "new.md").read_text(encoding="utf-8") == "updated\n"

    (vault / "symdir").symlink_to(tmp_path)
    with pytest.raises(GitOperationError) as exc:
        notes.write_note(settings, "My Vault", "symdir/escape.md", "x")
    assert exc.value.code == "invalid_path"

    small_settings = type(settings)(**{**settings.__dict__, "note_size_limit": 4})
    with pytest.raises(GitOperationError) as exc:
        notes.write_note(small_settings, "My Vault", "small.md", "12345")
    assert exc.value.code == "note_too_large"


def test_delete_note_and_commit_flow(tmp_path):
    settings, vault = make_settings(tmp_path)

    notes.write_note(settings, "My Vault", "drafts/plan.md", "v1\n")
    c1 = vaults.commit(settings, "My Vault", "add draft", ["drafts/plan.md"])
    assert c1["files"] == ["drafts/plan.md"]

    deleted = notes.delete_note(settings, "My Vault", "drafts/plan.md")
    assert deleted == {"vault": "My Vault", "path": "drafts/plan.md", "deleted": True}
    assert not (vault / "drafts" / "plan.md").exists()

    c2 = vaults.commit(settings, "My Vault", "remove draft", ["drafts/plan.md"])
    assert c2["files"] == ["drafts/plan.md"]

    with pytest.raises(GitOperationError) as exc:
        notes.delete_note(settings, "My Vault", "drafts/plan.md")
    assert exc.value.code == "not_found"

    (vault / "adir.md").mkdir()
    with pytest.raises(GitOperationError) as exc:
        notes.delete_note(settings, "My Vault", "adir.md")
    assert exc.value.code == "invalid_path"
