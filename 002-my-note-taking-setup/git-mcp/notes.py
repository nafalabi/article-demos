from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

from config import Settings
from vaults import GitOperationError, _lock_for, resolve_vault, vault_path

ALLOWED_EXTENSIONS = frozenset({".md", ".markdown", ".txt", ".canvas"})
EXCLUDED_DIRS = frozenset({".git", ".obsidian"})
EXPLICIT_ROOT_CONFIGS = frozenset({".gitignore", ".gitattributes"})


def _is_explicit_config_path(posix: PurePosixPath) -> bool:
    if len(posix.parts) == 1 and posix.parts[0] in EXPLICIT_ROOT_CONFIGS:
        return True
    if (
        len(posix.parts) == 2
        and posix.parts[0] == ".obsidian"
        and posix.suffix.lower() == ".json"
    ):
        return True
    return False


def _require_vault_dir(settings: Settings, name: str) -> Path:
    cfg = resolve_vault(settings, name)
    directory = vault_path(settings, cfg)
    if directory.is_symlink() or not directory.is_dir():
        raise GitOperationError("not_initialized", f"vault {name!r} has no directory")
    return directory


def _validate_rel_parts(
    raw: str, *, allow_empty: bool = False, allow_obsidian: bool = False
) -> Path:
    if not raw or not raw.strip():
        if allow_empty:
            return Path()
        raise GitOperationError("invalid_path", "path is required")
    if "\\" in raw or "\x00" in raw:
        raise GitOperationError("invalid_path", f"unsafe path: {raw!r}")
    rel = Path(raw)
    if rel.is_absolute() or not rel.parts:
        raise GitOperationError("invalid_path", f"unsafe path: {raw!r}")
    for part in rel.parts:
        if part in (".", "..") or part == ".git":
            raise GitOperationError("invalid_path", f"unsafe path component: {raw!r}")
        if part == ".obsidian" and not allow_obsidian:
            raise GitOperationError("invalid_path", f"unsafe path component: {raw!r}")
    return rel


def _check_no_symlinks(root: Path, rel: Path) -> Path:
    cur = root
    for part in rel.parts:
        cur = cur / part
        if cur.is_symlink():
            raise GitOperationError("invalid_path", f"symlink rejected: {rel.as_posix()!r}")
    resolved_root = root.resolve()
    resolved = cur.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise GitOperationError("invalid_path", f"path escapes the vault: {rel.as_posix()!r}")
    return cur


def _validate_note_path(directory: Path, raw: str) -> tuple[PurePosixPath, Path]:
    rel = _validate_rel_parts(raw, allow_empty=False, allow_obsidian=True)
    posix = PurePosixPath(*rel.parts)
    if not _is_explicit_config_path(posix):
        if ".obsidian" in posix.parts:
            raise GitOperationError("invalid_path", f"unsafe path component: {raw!r}")
        if posix.suffix.lower() not in ALLOWED_EXTENSIONS:
            raise GitOperationError(
                "invalid_path",
                f"unsupported note extension for {raw!r}; allowed: {sorted(ALLOWED_EXTENSIONS)}",
            )
    full = _check_no_symlinks(directory, rel)
    return posix, full


def _validate_prefix(directory: Path, prefix: str | None) -> Path:
    if prefix is None or not prefix.strip():
        return directory
    rel = _validate_rel_parts(prefix, allow_empty=True)
    target = _check_no_symlinks(directory, rel)
    if not target.exists():
        raise GitOperationError("not_found", f"no such prefix: {prefix!r}")
    if not target.is_dir():
        raise GitOperationError("invalid_path", f"prefix is not a directory: {prefix!r}")
    return target


def _iter_note_files(directory: Path, search_root: Path) -> list[tuple[PurePosixPath, Path]]:
    items: list[tuple[PurePosixPath, Path]] = []
    for cur_dir, dirnames, filenames in os.walk(search_root, followlinks=False):
        cur_path = Path(cur_dir)
        kept: list[str] = []
        for d in sorted(dirnames):
            if d in EXCLUDED_DIRS:
                continue
            if (cur_path / d).is_symlink():
                continue
            kept.append(d)
        dirnames[:] = kept

        for fname in sorted(filenames):
            full = cur_path / fname
            if full.is_symlink() or not full.is_file():
                continue
            if full.suffix.lower() not in ALLOWED_EXTENSIONS:
                continue
            rel = PurePosixPath(*full.relative_to(directory).parts)
            items.append((rel, full))
    items.sort(key=lambda item: item[0].as_posix())
    return items


def list_notes(settings: Settings, name: str, prefix: str | None = None) -> dict:
    """List allowed note files in a vault, optionally filtered by a relative subdirectory."""
    directory = _require_vault_dir(settings, name)
    with _lock_for(directory):
        search_root = _validate_prefix(directory, prefix)
        files = _iter_note_files(directory, search_root)
        entries = [
            {"path": rel.as_posix(), "size_bytes": full.stat().st_size}
            for rel, full in files
        ]
    return {"vault": name, "notes": entries}


def read_note(settings: Settings, name: str, path: str) -> dict:
    """Read a UTF-8 note file from a vault within the configured size limit."""
    directory = _require_vault_dir(settings, name)
    with _lock_for(directory):
        posix, full = _validate_note_path(directory, path)
        if not full.exists():
            raise GitOperationError("not_found", f"no such note: {path!r}")
        if not full.is_file():
            raise GitOperationError("invalid_path", f"not a regular file: {path!r}")
        size = full.stat().st_size
        if size > settings.note_size_limit:
            raise GitOperationError(
                "note_too_large",
                f"note exceeds {settings.note_size_limit} byte limit",
            )
        raw = full.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GitOperationError("invalid_encoding", f"note is not valid UTF-8: {path!r}") from exc
    return {
        "vault": name,
        "path": posix.as_posix(),
        "content": text,
        "size_bytes": len(raw),
    }


def search_notes(
    settings: Settings, name: str, query: str, prefix: str | None = None, limit: int = 20
) -> dict:
    """Case-insensitively search note paths and UTF-8 contents in a vault."""
    needle = query.strip().lower()
    if not needle:
        raise GitOperationError("invalid_query", "search query is required")
    limit = max(1, min(100, int(limit)))

    directory = _require_vault_dir(settings, name)
    matches: list[dict] = []
    truncated = False

    with _lock_for(directory):
        search_root = _validate_prefix(directory, prefix)
        for rel, full in _iter_note_files(directory, search_root):
            rel_str = rel.as_posix()
            matched_content = False
            if full.stat().st_size <= settings.note_size_limit:
                try:
                    text = full.read_bytes().decode("utf-8")
                except UnicodeDecodeError:
                    text = None
                if text is not None:
                    for lineno, line in enumerate(text.splitlines(), start=1):
                        if needle in line.lower():
                            matches.append(
                                {"path": rel_str, "line": lineno, "snippet": line[:200]}
                            )
                            matched_content = True
                            if len(matches) >= limit:
                                truncated = True
                                break
            if truncated:
                break
            if not matched_content and needle in rel_str.lower():
                matches.append({"path": rel_str, "line": None, "snippet": rel_str})
                if len(matches) >= limit:
                    truncated = True
                    break

    return {
        "vault": name,
        "query": query,
        "matches": matches,
        "truncated": truncated,
    }


def write_note(
    settings: Settings,
    name: str,
    path: str,
    content: str,
    overwrite: bool = True,
) -> dict:
    """Create or overwrite a UTF-8 note file in a vault within the size limit."""
    encoded = content.encode("utf-8")
    if len(encoded) > settings.note_size_limit:
        raise GitOperationError(
            "note_too_large",
            f"note exceeds {settings.note_size_limit} byte limit",
        )

    directory = _require_vault_dir(settings, name)
    with _lock_for(directory):
        posix, full = _validate_note_path(directory, path)
        if full.exists() and not full.is_file():
            raise GitOperationError("invalid_path", f"not a regular file: {path!r}")
        existed = full.exists()
        if existed and not overwrite:
            raise GitOperationError("already_exists", f"note already exists: {path!r}")

        cur = directory
        for part in full.relative_to(directory).parent.parts:
            cur = cur / part
            if cur.is_symlink():
                raise GitOperationError("invalid_path", f"symlink rejected: {path!r}")
            if cur.exists() and not cur.is_dir():
                raise GitOperationError("invalid_path", f"parent is not a directory: {path!r}")

        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(encoded)

    return {
        "vault": name,
        "path": posix.as_posix(),
        "created": not existed,
        "size_bytes": len(encoded),
    }


def delete_note(settings: Settings, name: str, path: str) -> dict:
    """Delete a regular note file from a vault."""
    directory = _require_vault_dir(settings, name)
    with _lock_for(directory):
        posix, full = _validate_note_path(directory, path)
        if not full.exists():
            raise GitOperationError("not_found", f"no such note: {path!r}")
        if not full.is_file():
            raise GitOperationError("invalid_path", f"not a regular file: {path!r}")
        full.unlink()
    return {"vault": name, "path": posix.as_posix(), "deleted": True}
