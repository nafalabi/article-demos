import dataclasses
import shutil

import pytest

from config import parse_repos_conf
from helpers import make_settings, run
from vaults import (
    GitOperationError,
    _sanitize,
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


@pytest.mark.parametrize("bad", ["*.md", ":(exclude)a.md", "[ab].md"])
def test_commit_rejects_glob_and_pathspec_magic(tmp_path, bad):
    settings, vault = make_settings(tmp_path)
    (vault / "a.md").write_text("a2\n", encoding="utf-8")
    (vault / "b.md").write_text("b2\n", encoding="utf-8")

    with pytest.raises(GitOperationError) as err:
        commit(settings, "My Vault", "sweep", [bad])

    assert err.value.code == "invalid_path"
    assert run_git(settings, vault, ["log", "-1", "--format=%s"]).stdout.strip() == "seed"


def test_commit_accepts_literal_bracket_file(tmp_path):
    settings, vault = make_settings(tmp_path)
    (vault / "[note].md").write_text("literal\n", encoding="utf-8")

    result = commit(settings, "My Vault", "literal", ["[note].md"])

    assert result["files"] == ["[note].md"]


def test_commit_accepts_non_ascii_path(tmp_path):
    settings, vault = make_settings(tmp_path)
    (vault / "Cät.md").write_text("unicode\n", encoding="utf-8")

    result = commit(settings, "My Vault", "unicode", ["Cät.md"])

    assert result["files"] == ["Cät.md"]


def test_commit_rejects_deleted_directory(tmp_path):
    settings, vault = make_settings(tmp_path)
    (vault / "old").mkdir()
    (vault / "old" / "a.md").write_text("old\n", encoding="utf-8")
    run_git(settings, vault, ["add", "old"])
    run_git(settings, vault, ["commit", "-m", "add old"])
    shutil.rmtree(vault / "old")

    with pytest.raises(GitOperationError) as err:
        commit(settings, "My Vault", "delete dir", ["old"])

    assert err.value.code == "invalid_path"


def test_status_clean_repo_has_no_changes(tmp_path):
    settings, _ = make_settings(tmp_path)

    result = status(settings, "My Vault")

    assert result["clean"] is True
    assert result["changes"] == []


def test_log_empty_repo_returns_no_commits(tmp_path):
    settings, vault = make_settings(tmp_path)
    run_git(settings, vault, ["checkout", "--orphan", "empty"])
    run_git(settings, vault, ["rm", "-rf", "."])

    assert log(settings, "My Vault", 10) == {"commits": []}


@pytest.mark.parametrize(
    "text",
    ["To ssh://git@git.example.com/x.git", "To git@git.example.com:x.git"],
)
def test_sanitize_hides_ssh_and_scp_urls(text):
    assert "git.example.com" not in _sanitize(text)


def test_scrub_all_origins_handles_nested_repo_and_pushurl(tmp_path):
    settings, _ = make_settings(tmp_path)
    inner = settings.vaults_root / "Outer" / "Inner"
    inner.mkdir(parents=True)
    run_git(settings, inner, ["init", "-b", "main"])
    run_git(settings, inner, ["remote", "add", "origin", "https://u:p@h/x.git"])
    run_git(
        settings,
        inner,
        ["remote", "set-url", "--push", "origin", "https://u:p@h/y.git"],
    )
    run_git(settings, inner, ["remote", "add", "backup", "ssh://git@h/z.git"])
    run_git(
        settings,
        inner,
        ["remote", "add", "useronly", "https://user@h/u.git"],
    )

    scrub_all_origins(settings)

    urls = run_git(settings, inner, ["remote", "get-url", "--all", "origin"]).stdout
    assert "u:p@" not in urls
    assert (
        run_git(settings, inner, ["remote", "get-url", "backup"]).stdout.strip()
        == "ssh://git@h/z.git"
    )
    assert (
        run_git(settings, inner, ["remote", "get-url", "useronly"]).stdout.strip()
        == "https://user@h/u.git"
    )


def test_credential_helper_is_exclusive(tmp_path):
    settings, vault = make_settings(tmp_path)
    marker = tmp_path / "evil-ran"
    evil = tmp_path / "evil-helper"
    evil.write_text(f"#!/bin/sh\ntouch {marker}\necho username=evil\n", encoding="utf-8")
    evil.chmod(0o755)
    run_git(
        settings,
        vault,
        ["config", "--local", "credential.helper", f"!{evil}"],
    )

    result = run_git(
        settings,
        vault,
        ["credential", "fill"],
        input_text="protocol=https\nhost=git.example.com\n\n",
    )

    assert "username=ignis" in result.stdout
    assert not marker.exists()


def test_push_ignores_repo_force_refspec(tmp_path):
    settings, vault = make_settings(tmp_path)
    cfg = parse_repos_conf(settings.repos_conf)[0]
    bare = settings.vaults_root.parent / "remotes" / cfg.remote_path
    other = tmp_path / "other-force"
    run(tmp_path, "clone", str(bare), str(other))
    run(other, "config", "user.name", "other")
    run(other, "config", "user.email", "other@example.com")
    (other / "a.md").write_text("remote\n", encoding="utf-8")
    run(other, "commit", "-am", "remote edit")
    run(other, "push", "origin", cfg.branch)
    (vault / "a.md").write_text("local\n", encoding="utf-8")
    commit(settings, "My Vault", "local edit", ["a.md"])
    run_git(
        settings,
        vault,
        ["config", "remote.origin.push", "+refs/heads/main:refs/heads/main"],
    )

    with pytest.raises(GitOperationError) as err:
        push(settings, "My Vault")

    assert err.value.code == "push_rejected"


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_prepare_vault_rejects_non_directory_dotgit(tmp_path, kind):
    settings, vault = make_settings(tmp_path)
    shutil.rmtree(vault / ".git")
    other = tmp_path / "other-repo"
    other.mkdir()
    run_git(settings, other, ["init", "-b", "main"])
    if kind == "file":
        (vault / ".git").write_text(f"gitdir: {other}/.git\n", encoding="utf-8")
    else:
        (vault / ".git").symlink_to(other / ".git", target_is_directory=True)

    with pytest.raises(GitOperationError) as err:
        prepare_vault(settings, parse_repos_conf(settings.repos_conf)[0])

    assert err.value.code == "not_initialized"


def test_repo_local_fsmonitor_is_not_run(tmp_path):
    settings, vault = make_settings(tmp_path)
    marker = tmp_path / "fsmonitor-ran"
    script = tmp_path / "fsmonitor.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    run_git(settings, vault, ["config", "core.fsmonitor", str(script)])
    (vault / "a.md").write_text("fsm\n", encoding="utf-8")

    commit(settings, "My Vault", "fsm", ["a.md"])

    assert not marker.exists()


def test_repo_local_textconv_is_not_run(tmp_path):
    settings, vault = make_settings(tmp_path)
    marker = tmp_path / "textconv-ran"
    script = tmp_path / "textconv.sh"
    script.write_text(
        f"#!/bin/sh\ntouch {marker}\ncat \"$1\"\n", encoding="utf-8"
    )
    script.chmod(0o755)
    (vault / ".gitattributes").write_text("*.md diff=evil\n", encoding="utf-8")
    run_git(settings, vault, ["config", "diff.evil.textconv", str(script)])
    (vault / "a.md").write_text("textconv\n", encoding="utf-8")

    diff(settings, "My Vault", False, None)

    assert not marker.exists()


def test_operations_on_missing_vault_return_not_initialized(tmp_path):
    settings, vault = make_settings(tmp_path)
    shutil.rmtree(vault)

    for operation in (
        lambda: status(settings, "My Vault"),
        lambda: diff(settings, "My Vault", False, None),
        lambda: log(settings, "My Vault", 10),
    ):
        with pytest.raises(GitOperationError) as err:
            operation()
        assert err.value.code == "not_initialized"


def test_commit_and_diff_accept_space_and_non_ascii_path(tmp_path):
    settings, vault = make_settings(tmp_path)
    note = vault / "My Cät.md"
    note.write_text("one\n", encoding="utf-8")

    commit(settings, "My Vault", "add unicode", ["My Cät.md"])
    note.write_text("two\n", encoding="utf-8")
    result = diff(settings, "My Vault", False, "My Cät.md")

    assert "My Cät.md" in result["diff"]
    assert "two" in result["diff"]


def test_repo_local_clean_filter_is_not_run(tmp_path):
    settings, vault = make_settings(tmp_path)
    marker = tmp_path / "filter-ran"
    script = tmp_path / "filter.sh"
    script.write_text(
        f"#!/bin/sh\ntouch {marker}\ncat\n", encoding="utf-8"
    )
    script.chmod(0o755)
    (vault / ".gitattributes").write_text("*.md filter=evil\n", encoding="utf-8")
    run_git(settings, vault, ["config", "filter.evil.clean", str(script)])
    (vault / "a.md").write_text("filtered\n", encoding="utf-8")

    commit(settings, "My Vault", "filter", ["a.md"])

    assert not marker.exists()
