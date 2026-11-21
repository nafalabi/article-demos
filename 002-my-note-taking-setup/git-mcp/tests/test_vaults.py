import dataclasses

import pytest

from config import parse_repos_conf
from helpers import make_settings, run
from vaults import (
    GitOperationError,
    commit,
    diff,
    log,
    prepare_vault,
    push,
    run_git,
    scrub_all_origins,
    status,
    validate_commit_path,
)


def test_commit_only_named_file_leaves_other_staged_changes(tmp_path):
    settings, vault = make_settings(tmp_path)
    (vault / "a.md").write_text("a2\n", encoding="utf-8")
    (vault / "b.md").write_text("b2\n", encoding="utf-8")
    run_git(settings, vault, ["add", "b.md"])

    result = commit(settings, "My Vault", "agent edit", ["a.md"])

    assert result["files"] == ["a.md"]
    assert run_git(settings, vault, ["show", "HEAD:a.md"]).stdout == "a2\n"
    assert "b.md" in run_git(settings, vault, ["diff", "--cached", "--name-only"]).stdout


def test_commit_uses_agent_author_and_accepts_deleted_file(tmp_path):
    settings, vault = make_settings(tmp_path)
    (vault / "gone.md").unlink()

    result = commit(settings, "My Vault", "delete note", ["gone.md"])

    assert result["author"] == "hermes <hermes@homelab.local>"
    show = run_git(
        settings, vault, ["show", "-s", "--format=%an <%ae>", result["commit"]]
    ).stdout.strip()
    assert show == "hermes <hermes@homelab.local>"


@pytest.mark.parametrize(
    "bad", ["../x", "/etc/passwd", ".git/config", ".", "note.md", "link.md"]
)
def test_validate_commit_path_rejects_unsafe_paths(tmp_path, bad):
    settings, vault = make_settings(tmp_path)
    (vault / "note.md").mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("secret\n", encoding="utf-8")
    (vault / "link.md").symlink_to(outside)

    with pytest.raises(GitOperationError) as err:
        validate_commit_path(vault, bad)

    assert err.value.code == "invalid_path"


def test_scrub_all_origins_cleans_unlisted_vaults(tmp_path):
    settings, _ = make_settings(tmp_path)
    unlisted = settings.vaults_root / "Unlisted"
    unlisted.mkdir()
    run_git(settings, unlisted, ["init", "-b", "main"])
    run_git(settings, unlisted, ["remote", "add", "origin", "https://u:p@h/x.git"])

    scrub_all_origins(settings)

    assert (
        "u:p@"
        not in run_git(settings, unlisted, ["remote", "get-url", "origin"]).stdout
    )


def test_prepare_vault_does_not_clobber_with_placeholder(tmp_path):
    settings, vault = make_settings(tmp_path)
    settings = dataclasses.replace(settings, remote_base="CHANGE_ME_GIT_BASE_URL")
    run_git(
        settings,
        vault,
        ["remote", "set-url", "origin", "https://git.example.com/Real/notes.git"],
    )

    prepare_vault(settings, parse_repos_conf(settings.repos_conf)[0])

    assert (
        run_git(settings, vault, ["remote", "get-url", "origin"]).stdout.strip()
        == "https://git.example.com/Real/notes.git"
    )


def test_status_reports_branch_and_changes(tmp_path):
    settings, vault = make_settings(tmp_path)
    (vault / "a.md").write_text("changed\n", encoding="utf-8")
    (vault / "new.md").write_text("new\n", encoding="utf-8")

    result = status(settings, "My Vault")

    assert result["branch"] == "main"
    assert result["clean"] is False
    assert any("a.md" in line for line in result["changes"])
    assert any("new.md" in line for line in result["changes"])


def test_diff_truncates_at_limit(tmp_path):
    settings, vault = make_settings(tmp_path)
    (vault / "a.md").write_text("x" * 200 + "\n", encoding="utf-8")
    settings = dataclasses.replace(settings, diff_limit=20)

    result = diff(settings, "My Vault", False, None)

    assert result["truncated"] is True
    assert len(result["diff"]) <= 20


def test_log_clamps_limit(tmp_path):
    settings, _ = make_settings(tmp_path)

    result = log(settings, "My Vault", 0)

    assert len(result["commits"]) == 1
    assert result["commits"][0]["subject"] == "seed"


def test_push_rejects_non_fast_forward(tmp_path):
    settings, vault = make_settings(tmp_path)
    cfg = parse_repos_conf(settings.repos_conf)[0]
    bare = settings.vaults_root.parent / "remotes" / cfg.remote_path
    other = tmp_path / "other"
    run(tmp_path, "clone", str(bare), str(other))
    run(other, "config", "user.name", "other")
    run(other, "config", "user.email", "other@example.com")
    (other / "a.md").write_text("remote\n", encoding="utf-8")
    run(other, "commit", "-am", "remote edit")
    run(other, "push", "origin", cfg.branch)

    (vault / "a.md").write_text("local\n", encoding="utf-8")
    commit(settings, "My Vault", "local edit", ["a.md"])

    with pytest.raises(GitOperationError) as err:
        push(settings, "My Vault")

    assert err.value.code == "push_rejected"


def test_push_rejects_branch_mismatch(tmp_path):
    settings, vault = make_settings(tmp_path)
    run_git(settings, vault, ["checkout", "-b", "other"])

    with pytest.raises(GitOperationError) as err:
        push(settings, "My Vault")

    assert err.value.code == "branch_mismatch"


def test_locked_index_returns_locked(tmp_path):
    settings, vault = make_settings(tmp_path)
    (vault / "a.md").write_text("changed\n", encoding="utf-8")
    (vault / ".git" / "index.lock").write_text("", encoding="utf-8")

    with pytest.raises(GitOperationError) as err:
        commit(settings, "My Vault", "x", ["a.md"])

    assert err.value.code == "locked"
