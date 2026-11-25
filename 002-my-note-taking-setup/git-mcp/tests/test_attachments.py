import base64
import io
import socket
import urllib.error
import urllib.request

import pytest

import attachments
from helpers import make_settings
from vaults import GitOperationError

PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF"
PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
SVG_BYTES = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"></svg>\n'


class _ChunkedResponse:
    def __init__(self, chunks: list[bytes], headers: dict[str, str] | None = None):
        self._chunks = list(chunks)
        self.headers = headers or {}
        self.read_calls = 0

    def read(self, size: int = -1) -> bytes:
        self.read_calls += 1
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeOpener:
    def __init__(self, response: _ChunkedResponse):
        self.response = response
        self.urls: list[str] = []

    def open(self, req: urllib.request.Request, timeout: int = 30):
        self.urls.append(req.full_url)
        return self.response


def _patch_public_dns(monkeypatch, ip: str = "93.184.216.34") -> None:
    def fake_getaddrinfo(host, port, type=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


@pytest.mark.parametrize(
    ("filename", "payload", "expected_mime"),
    [
        ("images/pic.png", PNG_BYTES, "image/png"),
        ("images/photo.jpg", JPEG_BYTES, "image/jpeg"),
        ("images/photo2.JPEG", JPEG_BYTES, "image/jpeg"),
        ("diagrams/flow.svg", SVG_BYTES, "image/svg+xml"),
        ("docs/spec.pdf", PDF_BYTES, "application/pdf"),
    ],
)
def test_upload_read_list_delete_base64_attachments(tmp_path, filename, payload, expected_mime):
    settings, vault = make_settings(tmp_path)
    b64 = base64.b64encode(payload).decode("ascii")

    uploaded = attachments.upload_attachment(
        settings, "My Vault", filename, content_base64=b64
    )
    assert uploaded == {
        "vault": "My Vault",
        "path": filename,
        "mime_type": expected_mime,
        "size_bytes": len(payload),
    }
    assert (vault / filename).read_bytes() == payload

    read_back = attachments.read_attachment(settings, "My Vault", filename)
    assert read_back["mime_type"] == expected_mime
    assert read_back["size_bytes"] == len(payload)
    assert base64.b64decode(read_back["content_base64"]) == payload

    listed = attachments.list_attachments(settings, "My Vault")
    assert listed == {
        "vault": "My Vault",
        "attachments": [
            {"path": filename, "mime_type": expected_mime, "size_bytes": len(payload)}
        ],
    }

    deleted = attachments.delete_attachment(settings, "My Vault", filename)
    assert deleted == {"vault": "My Vault", "path": filename, "deleted": True}
    assert not (vault / filename).exists()


def test_upload_supports_data_url_and_prevents_overwrites(tmp_path):
    settings, _ = make_settings(tmp_path)
    b64 = base64.b64encode(PNG_BYTES).decode("ascii")

    attachments.upload_attachment(
        settings,
        "My Vault",
        "assets/logo.png",
        content_base64=f"data:image/png;base64,{b64}",
    )

    with pytest.raises(GitOperationError) as exc:
        attachments.upload_attachment(
            settings,
            "My Vault",
            "assets/logo.png",
            content_base64=b64,
        )
    assert exc.value.code == "already_exists"


def test_attachment_size_limit_disabled_by_default_and_configurable(tmp_path):
    settings, vault = make_settings(tmp_path)
    assert settings.attachment_size_limit is None

    large_png = PNG_BYTES + (b"x" * 200000)
    res = attachments.upload_attachment(
        settings,
        "My Vault",
        "assets/big.png",
        content_base64=base64.b64encode(large_png).decode("ascii"),
    )
    assert res["size_bytes"] == len(large_png)

    limited = type(settings)(**{**settings.__dict__, "attachment_size_limit": 100})
    with pytest.raises(GitOperationError) as exc:
        attachments.upload_attachment(
            limited,
            "My Vault",
            "assets/too-big.png",
            content_base64=base64.b64encode(large_png).decode("ascii"),
        )
    assert exc.value.code == "attachment_too_large"
    assert not (vault / "assets" / "too-big.png").exists()

    with pytest.raises(GitOperationError) as exc:
        attachments.read_attachment(limited, "My Vault", "assets/big.png")
    assert exc.value.code == "attachment_too_large"


def test_streaming_download_writes_in_chunks_and_enforces_limit(tmp_path, monkeypatch):
    _patch_public_dns(monkeypatch)
    settings, vault = make_settings(tmp_path)

    chunks = [PNG_BYTES[:4], PNG_BYTES[4:], b"a" * 1024, b"b" * 1024]
    resp = _ChunkedResponse(chunks)
    opener = _FakeOpener(resp)

    res = attachments.upload_attachment(
        settings,
        "My Vault",
        "chatgpt/chart.png",
        file={
            "file_id": "file-123",
            "download_url": "https://files.oaiusercontent.com/file-123.png",
        },
        opener=opener,
    )
    assert opener.urls == ["https://files.oaiusercontent.com/file-123.png"]
    assert resp.read_calls == 5
    expected = b"".join(chunks)
    assert res["size_bytes"] == len(expected)
    assert (vault / "chatgpt" / "chart.png").read_bytes() == expected

    # Enforce configurable limit mid-stream without leaving partial files behind.
    limited = type(settings)(**{**settings.__dict__, "attachment_size_limit": 500})
    big_resp = _ChunkedResponse([PNG_BYTES, b"c" * 400, b"d" * 400])
    with pytest.raises(GitOperationError) as exc:
        attachments.upload_attachment(
            limited,
            "My Vault",
            "chatgpt/oversized.png",
            file={"download_url": "https://files.oaiusercontent.com/big.png"},
            opener=_FakeOpener(big_resp),
        )
    assert exc.value.code == "attachment_too_large"
    assert not (vault / "chatgpt" / "oversized.png").exists()
    assert list(vault.glob(".upload-*")) == []


def test_streaming_download_ssrf_and_redirect_protection(tmp_path, monkeypatch):
    _patch_public_dns(monkeypatch)
    settings, _ = make_settings(tmp_path)

    for bad_url in (
        "http://files.oaiusercontent.com/a.png",
        "https://user:pass@files.oaiusercontent.com/a.png",
        "https://evil.example.com/a.png",
        "https://oaiusercontent.com.evil.com/a.png",
        "https://attacker.blob.core.windows.net/a.png",
        "https://oai.attacker.blob.core.windows.net/a.png",
        "https://files.oaiusercontent.com:8443/a.png",
        "file:///etc/passwd",
    ):
        with pytest.raises(GitOperationError) as exc:
            attachments.upload_attachment(
                settings,
                "My Vault",
                "bad.png",
                file={"download_url": bad_url},
            )
        assert exc.value.code == "invalid_url"

    for allowed_url, target in (
        ("https://oaisdmntprindiasocentral.blob.core.windows.net/files/a.png?sv=1", "chatgpt/blob-oai.png"),
        ("https://sdmntwestus.blob.core.windows.net/files/b.png?sv=1", "chatgpt/blob-sdmnt.png"),
    ):
        uploaded = attachments.upload_attachment(
            settings,
            "My Vault",
            target,
            file={"file_id": "file_123", "download_url": allowed_url},
            opener=_FakeOpener(_ChunkedResponse([PNG_BYTES])),
        )
        assert uploaded["size_bytes"] == len(PNG_BYTES)

    # Private / metadata IP DNS resolution must be rejected.
    _patch_public_dns(monkeypatch, ip="169.254.169.254")
    with pytest.raises(GitOperationError) as exc:
        attachments.upload_attachment(
            settings,
            "My Vault",
            "ssrf.png",
            file={"download_url": "https://files.oaiusercontent.com/a.png"},
        )
    assert exc.value.code == "invalid_url"

    # Redirects to non-allowed hosts must be blocked by _SafeRedirectHandler.
    _patch_public_dns(monkeypatch, ip="93.184.216.34")
    handler = attachments._SafeRedirectHandler()
    req = urllib.request.Request("https://files.oaiusercontent.com/a.png")
    with pytest.raises(GitOperationError) as exc:
        handler.redirect_request(
            req, io.BytesIO(), 302, "Found", {}, "http://127.0.0.1/secret"
        )
    assert exc.value.code == "invalid_url"


def test_attachment_path_and_signature_validation(tmp_path):
    settings, vault = make_settings(tmp_path)
    (vault / ".obsidian").mkdir()
    (vault / "sym").symlink_to(tmp_path)

    for bad_path in (
        "",
        "../escape.png",
        ".git/hook.png",
        ".obsidian/icon.png",
        "sym/out.png",
        "notes/readme.md",
        "script.exe",
    ):
        with pytest.raises(GitOperationError) as exc:
            attachments.upload_attachment(
                settings,
                "My Vault",
                bad_path,
                content_base64=base64.b64encode(PNG_BYTES).decode("ascii"),
            )
        assert exc.value.code == "invalid_path"

    with pytest.raises(GitOperationError) as exc:
        attachments.upload_attachment(
            settings,
            "My Vault",
            "fake.png",
            content_base64=base64.b64encode(b"not a png").decode("ascii"),
        )
    assert exc.value.code == "invalid_attachment"

    with pytest.raises(GitOperationError) as exc:
        attachments.upload_attachment(
            settings,
            "My Vault",
            "bad.svg",
            content_base64=base64.b64encode(b"<html>nope</html>").decode("ascii"),
        )
    assert exc.value.code == "invalid_attachment"


def test_upload_accepts_string_file_references_and_rejects_unresolved(tmp_path, monkeypatch):
    _patch_public_dns(monkeypatch)
    settings, vault = make_settings(tmp_path)

    res1 = attachments.upload_attachment(
        settings,
        "My Vault",
        "chatgpt/from-json.png",
        file='{"file_id": "file-1", "download_url": "https://files.oaiusercontent.com/file-1.png"}',
        opener=_FakeOpener(_ChunkedResponse([PNG_BYTES])),
    )
    assert res1["size_bytes"] == len(PNG_BYTES)
    assert (vault / "chatgpt" / "from-json.png").read_bytes() == PNG_BYTES

    res2 = attachments.upload_attachment(
        settings,
        "My Vault",
        "chatgpt/from-url.png",
        file="https://files.oaiusercontent.com/file-2.png",
        opener=_FakeOpener(_ChunkedResponse([PNG_BYTES])),
    )
    assert res2["size_bytes"] == len(PNG_BYTES)

    for unresolved in ("file-abc123", "/mnt/data/chart.png", "chat_upload"):
        with pytest.raises(GitOperationError) as exc:
            attachments.upload_attachment(
                settings,
                "My Vault",
                "chatgpt/unresolved.png",
                file=unresolved,
            )
        assert exc.value.code == "invalid_file"

    with pytest.raises(GitOperationError) as exc:
        attachments.upload_attachment(
            settings,
            "My Vault",
            "chatgpt/ssrf-str.png",
            file="https://evil.example.com/file.png",
        )
    assert exc.value.code == "invalid_url"
