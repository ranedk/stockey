from __future__ import annotations

import tempfile
import time
import mimetypes
from pathlib import Path
from typing import Union
from typing import Iterator

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError
from environs import Env

# ---------------------------------------------------------------------------
# Environment & client bootstrap
# ---------------------------------------------------------------------------

env = Env()
env.read_env()

AWS_ACCESS_KEY_ID: str = env("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY: str = env("AWS_SECRET_ACCESS_KEY")
AWS_REGION: str = env.str("AWS_REGION", "us-east-1")
AWS_S3_ENDPOINT_URL: str = env.str("AWS_S3_ENDPOINT_URL", "https://s3.amazonaws.com")
AWS_BUCKET_NAME: str = env.str("AWS_BUCKET_NAME", "stockeydata")
_CLIENT: boto3.client | None = None  # cache


def _get_client() -> boto3.client:
    """
    Return a singleton boto3 S3 client.
    """
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = boto3.client(
            "s3",
            region_name=AWS_REGION,
            endpoint_url=AWS_S3_ENDPOINT_URL,
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
            config=Config(
                signature_version="s3v4",
                connect_timeout=15,
                read_timeout=60,
                retries={"max_attempts": 5, "mode": "standard"},
            ),
        )
    return _CLIENT


# botocore's client-level retries cover the initial request, but NOT a timeout that strikes while the
# response body is streaming (Body.read()). A single such read timeout previously aborted the whole
# multi-hour bhavcopy backfill (2017-06-18: "Read timed out"). Retry the full get_object+read on these
# transient network/timeout errors so a blip self-heals instead of killing the run.
_DOWNLOAD_MAX_ATTEMPTS = 5


def _download_bytes(key: str) -> bytes:
    """Download an object's bytes, retrying the full get_object+read on transient S3 errors."""
    last_exc: Exception | None = None
    for attempt in range(1, _DOWNLOAD_MAX_ATTEMPTS + 1):
        try:
            s3 = _get_client()
            return s3.get_object(Bucket=AWS_BUCKET_NAME, Key=key)["Body"].read()
        except (BotoCoreError, ConnectionError) as exc:  # ReadTimeout/ConnectTimeout/IncompleteRead are BotoCoreError subclasses; NoSuchKey (ClientError) is not caught and fails fast
            last_exc = exc
            if attempt == _DOWNLOAD_MAX_ATTEMPTS:
                break
            time.sleep(min(2 ** attempt, 30))
    assert last_exc is not None
    raise last_exc


# ---------------------------------------------------------------------------
# Upload helpers
# ---------------------------------------------------------------------------

def _detect_mime(key: str) -> str:
    """
    Guess MIME type from the object key; default to binary.
    """
    content_type, _ = mimetypes.guess_type(key)
    return content_type or "application/octet-stream"


def save_file_content(
    key: str,
    content: Union[bytes, str],
    disposition: str = "inline",
) -> dict:
    """
    Upload in-memory data (bytes or str) to S3.

    Parameters
    ----------
    key : str
        Object key in S3.
    content : Union[bytes, str]
        Data to upload.
    disposition : str, optional
        Content-Disposition header (default “inline”).
    """
    if isinstance(content, str):
        content = content.encode()  # utf-8 by default

    s3 = _get_client()
    return s3.put_object(
        Bucket=AWS_BUCKET_NAME,
        Key=key,
        Body=content,
        ContentDisposition=disposition,
        ContentType=_detect_mime(key),
    )


def save_file(
    file_path: Union[str, Path],
    prefix: str | None = None,
    key: str | None = None,
    disposition: str = "inline",
) -> None:
    """
    Upload a local file directly to S3, streaming via boto3.upload_file.

    Parameters
    ----------
    file_path : str | Path
        Path to the local file.
    prefix : str | None, optional
        Object key prefix to use in S3 (defaults to the file name).
    key : str | None, optional
        Object key to use in S3 (defaults to the file name).
    disposition : str, optional
        Content-Disposition header (default “inline”).
    """
    path = Path(file_path).expanduser().resolve(strict=True)
    key = key or path.name

    if prefix:
        key = f"{prefix}/{key}"

    extra_args = {
        "ContentType": _detect_mime(key),
        "ContentDisposition": disposition,
    }

    s3 = _get_client()
    s3.upload_file(str(path), AWS_BUCKET_NAME, key, ExtraArgs=extra_args)


def get_file_content(key: str) -> bytes:
    """ Download a file from S3 and return its content as bytes.  """
    return _download_bytes(key)


def get_file_handle(key: str) -> bytes:
    """ Download a file from S3 and return its content as a file handle. """
    s3 = _get_client()
    return s3.get_object(Bucket=AWS_BUCKET_NAME, Key=key)["Body"]


def get_as_temp_file(key: str) -> bytes:
    """ Download a file from S3 and return its content as a temporary file. """
    content = _download_bytes(key)
    with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
        tmp_file.write(content)
    return tmp_file.name


def delete_file(key: str) -> None:
    """Delete an object from the store (used to evict corrupt/placeholder downloads so the
    downloader treats the date as missing and re-fetches)."""
    s3 = _get_client()
    s3.delete_object(Bucket=AWS_BUCKET_NAME, Key=key)


def list_files(prefix: str) -> Iterator[str]:
    """
    Lazily iterate over all S3 object keys that start with *prefix*.

    Uses Boto3's paginator under the hood, so it seamlessly handles
    buckets with more than 1 000 objects without loading everything
    into memory at once.

    Example:
        for key in list_files("raw/2025/"):
            process(key)
    """
    s3 = _get_client()
    paginator = s3.get_paginator("list_objects_v2")

    for page in paginator.paginate(Bucket=AWS_BUCKET_NAME, Prefix=prefix):
        for obj in page.get("Contents", []):
            yield obj["Key"]