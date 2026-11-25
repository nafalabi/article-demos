from __future__ import annotations

import base64
import binascii
import codecs
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import socket
import tempfile
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from config import Settings
from notes import (
    EXCLUDED_DIRS,
    _check_no_symlinks,
    _require_vault_dir,
    _validate_prefix,
    _validate_rel_parts,
)
from vaults import GitOperationError, _lock_for

ATTACHMENT_MIME_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
}

CHUNK_SIZE = 65536
_HEADER_SNIFF_BYTES = 8192
_DOWNLOAD_TIMEOUT_SECONDS = 30
_MAX_REDIRECTS = 3
_ALLOWED_DOWNLOAD_HOST_SUFFIX = "oaiusercontent.com"
_ALLOWED_OPENAI_BLOB_HOST = re.compile(
    r"^(?:oai|sdmnt)[a-z0-9]+\.blob\.core\.windows\.net$"
)


def _is_allowed_download_host(host: str) -> bool:
    if host == _ALLOWED_DOWNLOAD_HOST_SUFFIX or host.endswith(
        f".{_ALLOWED_DOWNLOAD_HOST_SUFFIX}"
    ):
        return True
    return bool(_ALLOWED_OPENAI_BLOB_HOST.fullmatch(host))


def _validate_attachment_path(directory: Path, raw: str) -> tuple[PurePosixPath, Path, str]:
    rel = _validate_rel_parts(raw, allow_empty=False, allow_obsidian=False)
    posix = PurePosixPath(*rel.parts)
    ext = posix.suffix.lower()
    mime_type = ATTACHMENT_MIME_TYPES.get(ext)
    if mime_type is None:
        raise GitOperationError(
            "invalid_path",
            f"unsupported attachment extension for {raw!r}; allowed: {sorted(ATTACHMENT_MIME_TYPES)}",
        )
    full = _check_no_symlinks(directory, rel)
    return posix, full, mime_type


def _validate_parent_dirs(directory: Path, full: Path, raw: str) -> None:
    cur = directory
    for part in full.relative_to(directory).parent.parts:
        cur = cur / part
        if cur.is_symlink():
            raise GitOperationError("invalid_path", f"symlink rejected: {raw!r}")
        if cur.exists() and not cur.is_dir():
            raise GitOperationError("invalid_path", f"parent is not a directory: {raw!r}")


def _validate_attachment_header(ext: str, header: bytes) -> None:
    if not header:
        raise GitOperationError("invalid_attachment", "attachment content is empty")
    if ext == ".png":
        if not header.startswith(b"\x89PNG\r\n\x1a\n"):
            raise GitOperationError("invalid_attachment", "content does not match PNG signature")
    elif ext in (".jpg", ".jpeg"):
        if not header.startswith(b"\xff\xd8\xff"):
            raise GitOperationError("invalid_attachment", "content does not match JPEG signature")
    elif ext == ".pdf":
        if not header.startswith(b"%PDF-"):
            raise GitOperationError("invalid_attachment", "content does not match PDF signature")
    elif ext == ".svg":
        text = header.decode("utf-8", errors="ignore").lstrip("\ufeff \t\r\n").lower()
        if "<svg" not in text:
            raise GitOperationError("invalid_attachment", "content does not contain an <svg element")


def _validate_download_url(url: str) -> urllib.parse.SplitResult:
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise GitOperationError("invalid_url", f"malformed download URL: {exc}") from exc

    if parsed.scheme.lower() != "https":
        raise GitOperationError("invalid_url", "download_url must use https")
    if parsed.username is not None or parsed.password is not None:
        raise GitOperationError("invalid_url", "download_url must not embed credentials")

    host = (parsed.hostname or "").rstrip(".").lower()
    if not host:
        raise GitOperationError("invalid_url", "download_url is missing a hostname")
    if not _is_allowed_download_host(host):
        raise GitOperationError(
            "invalid_url",
            f"download_url host {host!r} is not an allowed OpenAI file host",
        )

    try:
        port = parsed.port
    except ValueError as exc:
        raise GitOperationError("invalid_url", "invalid port in download_url") from exc
    if port is not None and port != 443:
        raise GitOperationError("invalid_url", "download_url must use port 443")

    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise GitOperationError("download_failed", f"DNS resolution failed for {host!r}") from exc
    if not infos:
        raise GitOperationError("download_failed", f"no addresses resolved for {host!r}")

    for info in infos:
        addr_str = info[4][0].split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(addr_str)
        except ValueError as exc:
            raise GitOperationError("invalid_url", f"invalid resolved address: {addr_str!r}") from exc
        if not ip.is_global:
            raise GitOperationError(
                "invalid_url",
                f"download_url resolved to non-global address {addr_str!r}",
            )
    return parsed


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, max_redirects: int = _MAX_REDIRECTS):
        super().__init__()
        self.max_redirects = max_redirects
        self._redirects = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self._redirects += 1
        if self._redirects > self.max_redirects:
            raise GitOperationError("download_failed", "too many redirects")
        target = urllib.parse.urljoin(req.full_url, newurl)
        _validate_download_url(target)
        return super().redirect_request(req, fp, code, msg, headers, target)


def _decode_base64_payload(raw: str) -> bytes:
    payload = raw.strip()
    if payload.lower().startswith("data:") and "," in payload:
        header, rest = payload.split(",", 1)
        if ";base64" not in header.lower():
            raise GitOperationError("invalid_base64", "data URL must be base64-encoded")
        payload = rest
    compact = "".join(payload.split())
    if not compact:
        raise GitOperationError("invalid_base64", "base64 content is empty")
    try:
        return base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise GitOperationError("invalid_base64", f"invalid base64 payload: {exc}") from exc


def _stream_download_to_temp(
    url: str,
    ext: str,
    temp_dir: Path,
    size_limit: int | None,
    opener: urllib.request.OpenerDirector | None = None,
) -> tuple[Path, int]:
    _validate_download_url(url)
    active_opener = opener or urllib.request.build_opener(_SafeRedirectHandler())
    req = urllib.request.Request(url, method="GET")

    tmp_path: Path | None = None
    try:
        with active_opener.open(req, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as resp:
            content_length = resp.headers.get("Content-Length") if resp.headers else None
            if content_length is not None and size_limit is not None:
                try:
                    declared = int(content_length)
                except ValueError:
                    declared = -1
                if declared > size_limit:
                    raise GitOperationError(
                        "attachment_too_large",
                        f"attachment exceeds {size_limit} byte limit",
                    )

            with tempfile.NamedTemporaryFile(
                dir=temp_dir, prefix=".upload-", delete=False
            ) as tmp:
                tmp_path = Path(tmp.name)
                header_buf = bytearray()
                header_checked = False
                svg_decoder = (
                    codecs.getincrementaldecoder("utf-8")("strict")
                    if ext == ".svg"
                    else None
                )
                total = 0

                while True:
                    chunk = resp.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    total += len(chunk)
                    if size_limit is not None and total > size_limit:
                        raise GitOperationError(
                            "attachment_too_large",
                            f"attachment exceeds {size_limit} byte limit",
                        )
                    if len(header_buf) < _HEADER_SNIFF_BYTES:
                        need = _HEADER_SNIFF_BYTES - len(header_buf)
                        header_buf.extend(chunk[:need])
                    if not header_checked and len(header_buf) >= 16:
                        if ext != ".svg" or b"<svg" in bytes(header_buf).lower():
                            _validate_attachment_header(ext, bytes(header_buf))
                            header_checked = True
                    if svg_decoder is not None:
                        try:
                            svg_decoder.decode(chunk, final=False)
                        except UnicodeDecodeError as exc:
                            raise GitOperationError(
                                "invalid_attachment", "SVG attachment is not valid UTF-8"
                            ) from exc
                    tmp.write(chunk)

                if svg_decoder is not None:
                    try:
                        svg_decoder.decode(b"", final=True)
                    except UnicodeDecodeError as exc:
                        raise GitOperationError(
                            "invalid_attachment", "SVG attachment is not valid UTF-8"
                        ) from exc
                if not header_checked:
                    _validate_attachment_header(ext, bytes(header_buf))
                tmp.flush()

        return tmp_path, total
    except GitOperationError:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise GitOperationError("download_failed", f"failed to download attachment: {exc}") from exc


def _write_bytes_to_temp(data: bytes, ext: str, temp_dir: Path) -> Path:
    _validate_attachment_header(ext, data[:_HEADER_SNIFF_BYTES])
    if ext == ".svg":
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GitOperationError(
                "invalid_attachment", "SVG attachment is not valid UTF-8"
            ) from exc
    with tempfile.NamedTemporaryFile(dir=temp_dir, prefix=".upload-", delete=False) as tmp:
        tmp.write(data)
        tmp.flush()
        return Path(tmp.name)


def list_attachments(settings: Settings, name: str, prefix: str | None = None) -> dict:
    """List supported attachment files (.png, .jpg, .jpeg, .svg, .pdf) in a vault."""
    directory = _require_vault_dir(settings, name)
    entries: list[dict[str, object]] = []
    with _lock_for(directory):
        search_root = _validate_prefix(directory, prefix)
        items: list[tuple[PurePosixPath, Path, str]] = []
        for cur_dir, dirnames, filenames in os.walk(search_root, followlinks=False):
            cur_path = Path(cur_dir)
            dirnames[:] = [
                d
                for d in sorted(dirnames)
                if d not in EXCLUDED_DIRS and not (cur_path / d).is_symlink()
            ]
            for fname in sorted(filenames):
                full = cur_path / fname
                if full.is_symlink() or not full.is_file():
                    continue
                mime_type = ATTACHMENT_MIME_TYPES.get(full.suffix.lower())
                if mime_type is None:
                    continue
                rel = PurePosixPath(*full.relative_to(directory).parts)
                items.append((rel, full, mime_type))
        items.sort(key=lambda item: item[0].as_posix())
        for rel, full, mime_type in items:
            entries.append(
                {
                    "path": rel.as_posix(),
                    "mime_type": mime_type,
                    "size_bytes": full.stat().st_size,
                }
            )
    return {"vault": name, "attachments": entries}


def read_attachment(settings: Settings, name: str, path: str) -> dict:
    """Read an attachment file from a vault and return its base64-encoded contents."""
    directory = _require_vault_dir(settings, name)
    with _lock_for(directory):
        posix, full, mime_type = _validate_attachment_path(directory, path)
        if not full.exists():
            raise GitOperationError("not_found", f"no such attachment: {path!r}")
        if not full.is_file():
            raise GitOperationError("invalid_path", f"not a regular file: {path!r}")
        size = full.stat().st_size
        if settings.attachment_size_limit is not None and size > settings.attachment_size_limit:
            raise GitOperationError(
                "attachment_too_large",
                f"attachment exceeds {settings.attachment_size_limit} byte limit",
            )
        raw = full.read_bytes()
    return {
        "vault": name,
        "path": posix.as_posix(),
        "mime_type": mime_type,
        "size_bytes": len(raw),
        "content_base64": base64.b64encode(raw).decode("ascii"),
    }


def _resolve_file_download_url(file: dict[str, Any] | str) -> str:
    payload: Any = file
    if isinstance(payload, str):
        raw = payload.strip()
        if not raw:
            raise GitOperationError("invalid_file", "file reference is empty")
        if raw.startswith("{"):
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise GitOperationError(
                    "invalid_file", f"file JSON string is malformed: {exc}"
                ) from exc
        elif raw.lower().startswith(("https://", "http://")):
            return raw
        else:
            raise GitOperationError(
                "invalid_file",
                f"file reference {raw!r} was not resolved to a download_url; pass content_base64 instead",
            )

    if not isinstance(payload, dict):
        raise GitOperationError("invalid_file", "file parameter must be an object or URL string")
    download_url = payload.get("download_url")
    if not isinstance(download_url, str) or not download_url.strip():
        raise GitOperationError(
            "invalid_file",
            "file object is missing a 'download_url'; pass content_base64 instead",
        )
    return download_url.strip()


def upload_attachment(
    settings: Settings,
    name: str,
    path: str,
    content_base64: str | None = None,
    file: dict[str, Any] | str | None = None,
    *,
    opener: urllib.request.OpenerDirector | None = None,
) -> dict:
    """Upload a new attachment from base64 or a ChatGPT file download_url without overwriting."""
    has_b64 = content_base64 is not None
    has_file = file is not None
    if has_b64 == has_file:
        raise GitOperationError(
            "invalid_input",
            "provide exactly one of 'content_base64' or 'file'",
        )

    directory = _require_vault_dir(settings, name)
    with _lock_for(directory):
        posix, full, mime_type = _validate_attachment_path(directory, path)
        _validate_parent_dirs(directory, full, path)
        if full.exists():
            raise GitOperationError("already_exists", f"attachment already exists: {path!r}")

    ext = posix.suffix.lower()
    tmp_path: Path | None = None
    try:
        if content_base64 is not None:
            raw = _decode_base64_payload(content_base64)
            if (
                settings.attachment_size_limit is not None
                and len(raw) > settings.attachment_size_limit
            ):
                raise GitOperationError(
                    "attachment_too_large",
                    f"attachment exceeds {settings.attachment_size_limit} byte limit",
                )
            tmp_path = _write_bytes_to_temp(raw, ext, directory)
            size_bytes = len(raw)
        else:
            assert file is not None
            download_url = _resolve_file_download_url(file)
            tmp_path, size_bytes = _stream_download_to_temp(
                download_url,
                ext,
                directory,
                settings.attachment_size_limit,
                opener=opener,
            )

        with _lock_for(directory):
            _, full, _ = _validate_attachment_path(directory, path)
            _validate_parent_dirs(directory, full, path)
            if full.exists():
                raise GitOperationError(
                    "already_exists", f"attachment already exists: {path!r}"
                )
            full.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(tmp_path, full)
            except FileExistsError as exc:
                raise GitOperationError(
                    "already_exists", f"attachment already exists: {path!r}"
                ) from exc
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)

    return {
        "vault": name,
        "path": posix.as_posix(),
        "mime_type": mime_type,
        "size_bytes": size_bytes,
    }


def delete_attachment(settings: Settings, name: str, path: str) -> dict:
    """Delete an existing attachment file from a vault."""
    directory = _require_vault_dir(settings, name)
    with _lock_for(directory):
        posix, full, _ = _validate_attachment_path(directory, path)
        if not full.exists():
            raise GitOperationError("not_found", f"no such attachment: {path!r}")
        if not full.is_file():
            raise GitOperationError("invalid_path", f"not a regular file: {path!r}")
        full.unlink()
    return {"vault": name, "path": posix.as_posix(), "deleted": True}
