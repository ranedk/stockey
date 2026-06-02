from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from environs import Env

from utils.store import get_file_content, save_file_content


env = Env()
env.read_env()


DEFAULT_TEXT_EXCERPT_CHARS = env.int("BLOB_TEXT_EXCERPT_CHARS", 1200)


@dataclass(frozen=True)
class TextBlobMetadata:
    key: str
    sha256: str
    char_count: int
    byte_count: int
    excerpt: str


def text_blob_metadata(text: str, *, key: str, excerpt_chars: int | None = None) -> TextBlobMetadata:
    normalized = text or ""
    payload = normalized.encode("utf-8")
    limit = DEFAULT_TEXT_EXCERPT_CHARS if excerpt_chars is None else max(int(excerpt_chars), 0)
    return TextBlobMetadata(
        key=key,
        sha256=hashlib.sha256(payload).hexdigest(),
        char_count=len(normalized),
        byte_count=len(payload),
        excerpt=normalized[:limit],
    )


def put_text_blob(text: str, *, key: str, excerpt_chars: int | None = None) -> TextBlobMetadata:
    metadata = text_blob_metadata(text, key=key, excerpt_chars=excerpt_chars)
    save_file_content(key, text)
    return metadata


def get_text_blob(key: str) -> str:
    return get_file_content(key).decode("utf-8")


def metadata_to_row(prefix: str, metadata: TextBlobMetadata | None) -> dict[str, Any]:
    if metadata is None:
        return {
            f"{prefix}_s3_key": None,
            f"{prefix}_sha256": None,
            f"{prefix}_chars": None,
            f"{prefix}_bytes": None,
            f"{prefix}_excerpt": None,
        }
    return {
        f"{prefix}_s3_key": metadata.key,
        f"{prefix}_sha256": metadata.sha256,
        f"{prefix}_chars": metadata.char_count,
        f"{prefix}_bytes": metadata.byte_count,
        f"{prefix}_excerpt": metadata.excerpt,
    }
