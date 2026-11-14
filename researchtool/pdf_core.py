"""Shared PDF helpers for every module of the Research Toolkit client.

One place for: upload/download validation (one size limit, magic-byte check),
content fingerprints, metadata (title / authors / page count) and text-layer
extraction. Reader, Research Gaps and ResearchStudio all build on these.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import re
from typing import IO, Any, Iterable

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover - pypdf is a declared dependency
    PdfReader = None  # type: ignore[assignment,misc]

MAX_PDF_BYTES = 100 * 1024 * 1024
MAX_PDF_LABEL = "100 MB"
PDF_MAGIC = b"%PDF-"
CHUNK_BYTES = 1024 * 1024

GENERIC_TITLES = {
    "untitled", "document", "pdf", "mathpix", "ocr document", "ocr 文档",
    "microsoft word", "acrobat distiller",
}
_UUID = re.compile(r"[{(]?[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}[})]?", re.I)
_HEX = re.compile(r"[0-9a-f]{24,64}", re.I)


class PdfError(ValueError):
    """A PDF that cannot be accepted or read; the message is user-facing (Chinese).

    ``code`` is one of: not_pdf, empty, too_large, encrypted.
    """

    def __init__(self, message: str, code: str = "invalid") -> None:
        super().__init__(message)
        self.code = code


def size_error(max_bytes: int = MAX_PDF_BYTES) -> PdfError:
    label = MAX_PDF_LABEL if max_bytes == MAX_PDF_BYTES else f"{max_bytes / (1024 * 1024):g} MB"
    return PdfError(f"PDF 最大为 {label}。", "too_large")


HEADER_WINDOW = 1024  # the PDF spec lets readers find "%PDF-" anywhere in the first 1 KB


def is_pdf_header(data: bytes) -> bool:
    return PDF_MAGIC in data[:HEADER_WINDOW]


def copy_pdf_stream(
    chunks: Iterable[bytes],
    destination: Path,
    *,
    max_bytes: int = MAX_PDF_BYTES,
) -> int:
    """Write PDF bytes to ``destination`` atomically, enforcing magic bytes and size.

    Leaves nothing behind on failure. Returns the number of bytes written.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".partial")
    total = 0
    head = b""
    try:
        with temporary.open("wb") as handle:
            for chunk in chunks:
                if not chunk:
                    continue
                if len(head) < HEADER_WINDOW:
                    head += chunk[:HEADER_WINDOW - len(head)]
                    if len(head) >= HEADER_WINDOW and not is_pdf_header(head):
                        raise PdfError("输入文件不是 PDF。", "not_pdf")
                total += len(chunk)
                if total > max_bytes:
                    raise size_error(max_bytes)
                handle.write(chunk)
        if not total:
            raise PdfError("PDF 为空。", "empty")
        if not is_pdf_header(head):
            raise PdfError("输入文件不是 PDF。", "not_pdf")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return total


def read_chunks(stream: IO[bytes], size: int = CHUNK_BYTES) -> Iterable[bytes]:
    while chunk := stream.read(size):
        yield chunk


def save_pdf_upload(stream: IO[bytes], destination: Path, *, max_bytes: int = MAX_PDF_BYTES) -> int:
    """Save an uploaded file-like object (e.g. ``FileStorage.stream``) as a validated PDF."""
    return copy_pdf_stream(read_chunks(stream), destination, max_bytes=max_bytes)


def file_fingerprint(path: Path, salt: bytes | str = b"") -> str:
    """SHA-256 of the file contents, optionally prefixed with a namespace salt."""
    digest = hashlib.sha256()
    if salt:
        digest.update(salt.encode() if isinstance(salt, str) else salt)
    with path.open("rb") as handle:
        for chunk in read_chunks(handle):
            digest.update(chunk)
    return digest.hexdigest()


def clean_title(value: Any, limit: int = 500) -> str | None:
    """A displayable title, or None for empty, generic, UUID-like or hash-like values."""
    title = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or ""))
    title = re.sub(r"\s+", " ", title).strip(" \t\r\n-–—_|")
    if not title or len(title) > limit:
        return None
    if _UUID.fullmatch(title) or _HEX.fullmatch(title):
        return None
    if title.casefold() in GENERIC_TITLES:
        return None
    return title


def split_authors(value: Any) -> list[str]:
    raw = re.sub(r"\s+", " ", str(value or "")).strip()[:2_000]
    if not raw:
        return []
    names = (re.sub(r"\s+", " ", item).strip()[:300] for item in re.split(r"\s*(?:;|\band\b)\s*", raw, flags=re.I))
    return list(dict.fromkeys(name for name in names if name))[:50]


def open_pdf(path: Path):
    """Open with pypdf, transparently handling empty-password encryption."""
    if PdfReader is None:
        raise RuntimeError("pypdf 未安装，无法读取 PDF。")
    reader = PdfReader(str(path), strict=False)
    if getattr(reader, "is_encrypted", False):
        try:
            if reader.decrypt("") == 0:
                raise PdfError("PDF 已加密，无法读取。", "encrypted")
        except PdfError:
            raise
        except Exception as exc:
            raise PdfError("PDF 已加密，无法读取。", "encrypted") from exc
    return reader


@dataclass
class PdfInfo:
    title: str | None
    authors: list[str] = field(default_factory=list)
    page_count: int = 0


def pdf_info(path: Path) -> PdfInfo:
    reader = open_pdf(path)
    metadata = reader.metadata
    return PdfInfo(
        title=clean_title(getattr(metadata, "title", None) if metadata else None),
        authors=split_authors(getattr(metadata, "author", None) if metadata else None),
        page_count=len(reader.pages),
    )


def pdf_title(path: Path) -> str | None:
    """Embedded metadata title, or None when absent, generic or unreadable."""
    try:
        return pdf_info(path).title
    except Exception:
        return None


def extract_pages(path: Path, *, max_chars: int | None = None, reader: Any = None) -> list[str]:
    """Text layer per page (stripped). Stops early once ``max_chars`` is reached."""
    reader = reader if reader is not None else open_pdf(path)
    pages: list[str] = []
    total = 0
    for page in reader.pages:
        text = (page.extract_text() or "").strip()
        pages.append(text)
        total += len(text)
        if max_chars is not None and total >= max_chars:
            break
    return pages


def readable_characters(pages: Iterable[str]) -> int:
    return sum(len(re.sub(r"\s+", "", text)) for text in pages)
