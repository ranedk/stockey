from __future__ import annotations

import argparse
import json
import mimetypes
import os
import tempfile
from pathlib import Path
from typing import Literal

import requests
from environs import Env
from openai import OpenAI

from advisory.fallback_telemetry import record_local_fallback_event


env = Env()
env.read_env()


DEFAULT_OPENAI_MODEL = "gpt-4o-mini-transcribe"
DEFAULT_GEMINI_MODEL = "gemini-3-flash-preview"
Provider = Literal["openai", "gemini", "both"]


TRANSCRIBE_PROMPT = (
    "Transcribe the spoken audio faithfully. Return only the transcript text. "
    "Do not summarize, label speakers, or add commentary."
)


def download_audio_file(audio_url: str) -> tuple[str, str]:
    response = requests.get(audio_url, timeout=120)
    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "").split(";")[0].strip()
    suffix = Path(audio_url.split("?", 1)[0]).suffix
    if not suffix:
        suffix = mimetypes.guess_extension(content_type or "") or ".bin"
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    handle.write(response.content)
    handle.flush()
    handle.close()
    mime_type = content_type or (mimetypes.guess_type(handle.name)[0] or "application/octet-stream")
    return handle.name, mime_type


def cleanup_temp_file(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError as exc:
        record_local_fallback_event(
            module="utils.transcribe.llm_transcribe",
            fallback_type="transcribe_temp_file_cleanup_missing",
            source="cleanup_temp_file",
            severity="warn",
            reason="Transcription cleanup could not remove a temporary file because it was already missing.",
            error=exc,
            metadata={"path": str(path)},
        )


def persist_bytes_to_temp_file(data: bytes, suffix: str) -> str:
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    handle.write(data)
    handle.flush()
    handle.close()
    return handle.name


def transcribe_with_openai(
    audio_path: str,
    *,
    model: str = DEFAULT_OPENAI_MODEL,
    api_key: str | None = None,
) -> str:
    client = OpenAI(api_key=api_key or env("OPENAI_API_KEY"))
    with open(audio_path, "rb") as audio_file:
        transcription = client.audio.transcriptions.create(
            model=model,
            file=audio_file,
            response_format="text",
        )
    if isinstance(transcription, str):
        return transcription.strip()
    return getattr(transcription, "text", "").strip()


def _gemini_upload_file(audio_path: str, mime_type: str, api_key: str) -> dict:
    num_bytes = os.path.getsize(audio_path)
    start_response = requests.post(
        "https://generativelanguage.googleapis.com/upload/v1beta/files",
        params={"key": api_key},
        headers={
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(num_bytes),
            "X-Goog-Upload-Header-Content-Type": mime_type,
            "Content-Type": "application/json",
        },
        json={"file": {"display_name": Path(audio_path).name}},
        timeout=120,
    )
    start_response.raise_for_status()
    upload_url = start_response.headers.get("x-goog-upload-url")
    if not upload_url:
        raise RuntimeError("Gemini file upload did not return an upload URL")

    with open(audio_path, "rb") as handle:
        upload_response = requests.post(
            upload_url,
            headers={
                "Content-Length": str(num_bytes),
                "X-Goog-Upload-Offset": "0",
                "X-Goog-Upload-Command": "upload, finalize",
            },
            data=handle,
            timeout=300,
        )
    upload_response.raise_for_status()
    payload = upload_response.json()
    file_info = payload.get("file")
    if not file_info:
        raise RuntimeError(f"Gemini file upload returned no file metadata: {payload}")
    return file_info


def transcribe_with_gemini(
    audio_path: str,
    *,
    mime_type: str,
    model: str = DEFAULT_GEMINI_MODEL,
    api_key: str | None = None,
) -> str:
    key = api_key or env("GEMINI_KEY")
    file_info = _gemini_upload_file(audio_path, mime_type, key)
    response = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        params={"key": key},
        headers={"Content-Type": "application/json"},
        json={
            "contents": [
                {
                    "parts": [
                        {"text": TRANSCRIBE_PROMPT},
                        {
                            "file_data": {
                                "mime_type": file_info["mimeType"],
                                "file_uri": file_info["uri"],
                            }
                        },
                    ]
                }
            ]
        },
        timeout=300,
    )
    response.raise_for_status()
    payload = response.json()
    candidates = payload.get("candidates", [])
    if not candidates:
        return ""
    parts = candidates[0].get("content", {}).get("parts", [])
    texts = [part.get("text", "") for part in parts if part.get("text")]
    return "\n".join(texts).strip()


def transcribe_audio_url(
    audio_url: str,
    *,
    provider: Provider = "both",
    openai_model: str = DEFAULT_OPENAI_MODEL,
    gemini_model: str = DEFAULT_GEMINI_MODEL,
) -> dict[str, str]:
    temp_path, mime_type = download_audio_file(audio_url)
    try:
        results: dict[str, str] = {}
        if provider in {"openai", "both"}:
            results["openai"] = transcribe_with_openai(temp_path, model=openai_model)
        if provider in {"gemini", "both"}:
            results["gemini"] = transcribe_with_gemini(temp_path, mime_type=mime_type, model=gemini_model)
        return results
    finally:
        cleanup_temp_file(temp_path)


def transcribe_audio_bytes(
    audio_bytes: bytes,
    *,
    suffix: str,
    mime_type: str,
    provider: Provider = "both",
    openai_model: str = DEFAULT_OPENAI_MODEL,
    gemini_model: str = DEFAULT_GEMINI_MODEL,
) -> dict[str, str]:
    temp_path = persist_bytes_to_temp_file(audio_bytes, suffix=suffix)
    try:
        results: dict[str, str] = {}
        if provider in {"openai", "both"}:
            results["openai"] = transcribe_with_openai(temp_path, model=openai_model)
        if provider in {"gemini", "both"}:
            results["gemini"] = transcribe_with_gemini(temp_path, mime_type=mime_type, model=gemini_model)
        return results
    finally:
        cleanup_temp_file(temp_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Transcribe an audio URL with Gemini and OpenAI")
    parser.add_argument("audio_url", help="Audio URL (mp3, wav, mp4, m4a, webm, etc.)")
    parser.add_argument(
        "--provider",
        choices=["openai", "gemini", "both"],
        default="both",
        help="Which provider to use",
    )
    parser.add_argument("--openai-model", default=DEFAULT_OPENAI_MODEL)
    parser.add_argument("--gemini-model", default=DEFAULT_GEMINI_MODEL)
    args = parser.parse_args()

    try:
        result = transcribe_audio_url(
            args.audio_url,
            provider=args.provider,
            openai_model=args.openai_model,
            gemini_model=args.gemini_model,
        )
    except Exception as exc:
        raise SystemExit(str(exc))
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
