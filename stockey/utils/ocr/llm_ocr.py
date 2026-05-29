from __future__ import annotations

import argparse
import base64
import io
import json
import tempfile
from pathlib import Path
from typing import Iterable, Literal

import requests
from environs import Env
from openai import OpenAI
from pdf2image import convert_from_path
from PIL import Image

from utils.codex_cli import run_codex_cli
from utils.poppler import poppler_install_hint, resolve_poppler_path


env = Env()
env.read_env()


DEFAULT_OPENAI_MODEL = "gpt-5-nano"
DEFAULT_GEMINI_MODEL = "gemini-3-flash-preview"
DEFAULT_CODEX_MODEL = env("CODEX_CLI_OCR_MODEL", default=env("CODEX_CLI_MODEL", default="gpt-5.4-mini"))
Provider = Literal["openai", "gemini", "codex", "both"]


OCR_PROMPT = (
    "You are doing OCR on a PDF page image. Extract all visible text faithfully. "
    "Preserve line breaks where they help readability. Do not summarize, explain, "
    "or add commentary. If a page is blank, return an empty string."
)


def parse_pages_spec(pages: str | int | Iterable[int] = "all") -> list[int] | None:
    if pages == "all":
        return None
    if isinstance(pages, int):
        return [pages]
    if not isinstance(pages, str):
        values = sorted({int(value) for value in pages})
        return values

    text = pages.strip().lower()
    if text == "all":
        return None
    values: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start = int(start_text)
            end = int(end_text)
            if end < start:
                raise ValueError(f"Invalid page range: {part}")
            values.update(range(start, end + 1))
        else:
            values.add(int(part))
    if not values:
        raise ValueError("No pages selected")
    return sorted(values)


def render_pdf_pages(pdf_path: str | Path, pages: str | int | Iterable[int] = "all") -> list[tuple[int, Image.Image]]:
    pdf_path = Path(pdf_path)
    page_numbers = parse_pages_spec(pages)
    poppler_path = resolve_poppler_path()
    if page_numbers is None:
        try:
            images = convert_from_path(str(pdf_path), poppler_path=poppler_path)
        except Exception as exc:
            if "poppler" in str(exc).lower() or "pdfinfo" in str(exc).lower():
                raise RuntimeError(f"{exc}. {poppler_install_hint()}") from exc
            raise
        return [(index + 1, image) for index, image in enumerate(images)]

    rendered: list[tuple[int, Image.Image]] = []
    for page_number in page_numbers:
        try:
            images = convert_from_path(
                str(pdf_path),
                first_page=page_number,
                last_page=page_number,
                poppler_path=poppler_path,
            )
        except Exception as exc:
            if "poppler" in str(exc).lower() or "pdfinfo" in str(exc).lower():
                raise RuntimeError(f"{exc}. {poppler_install_hint()}") from exc
            raise
        if not images:
            raise ValueError(f"Could not render page {page_number} from {pdf_path}")
        rendered.append((page_number, images[0]))
    return rendered


def image_to_base64_png(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


def ocr_page_with_openai(
    image: Image.Image,
    *,
    model: str = DEFAULT_OPENAI_MODEL,
    api_key: str | None = None,
) -> str:
    client = OpenAI(api_key=api_key or env("OPENAI_API_KEY"))
    image_b64 = image_to_base64_png(image)
    response = client.responses.create(
        model=model,
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": OCR_PROMPT},
                    {
                        "type": "input_image",
                        "image_url": f"data:image/png;base64,{image_b64}",
                        "detail": "high",
                    },
                ],
            }
        ],
    )
    return (response.output_text or "").strip()


def ocr_page_with_gemini(
    image: Image.Image,
    *,
    model: str = DEFAULT_GEMINI_MODEL,
    api_key: str | None = None,
) -> str:
    image_b64 = image_to_base64_png(image)
    response = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        params={"key": api_key or env("GEMINI_KEY")},
        headers={"Content-Type": "application/json"},
        json={
            "contents": [
                {
                    "parts": [
                        {"text": OCR_PROMPT},
                        {
                            "inline_data": {
                                "mime_type": "image/png",
                                "data": image_b64,
                            }
                        },
                    ]
                }
            ]
        },
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    candidates = payload.get("candidates", [])
    if not candidates:
        return ""
    parts = candidates[0].get("content", {}).get("parts", [])
    texts = [part.get("text", "") for part in parts if part.get("text")]
    return "\n".join(texts).strip()


def ocr_page_with_codex(
    image: Image.Image,
    *,
    model: str = DEFAULT_CODEX_MODEL,
) -> str:
    with tempfile.NamedTemporaryFile(suffix=".png", delete=True) as handle:
        image.save(handle.name, format="PNG")
        return run_codex_cli(OCR_PROMPT, model=model, images=[handle.name]).strip()


def ocr_pdf_with_openai(
    pdf_path: str | Path,
    *,
    pages: str | int | Iterable[int] = "all",
    model: str = DEFAULT_OPENAI_MODEL,
) -> dict[int, str]:
    results: dict[int, str] = {}
    for page_number, image in render_pdf_pages(pdf_path, pages):
        results[page_number] = ocr_page_with_openai(image, model=model)
    return results


def ocr_pdf_with_gemini(
    pdf_path: str | Path,
    *,
    pages: str | int | Iterable[int] = "all",
    model: str = DEFAULT_GEMINI_MODEL,
) -> dict[int, str]:
    results: dict[int, str] = {}
    for page_number, image in render_pdf_pages(pdf_path, pages):
        results[page_number] = ocr_page_with_gemini(image, model=model)
    return results


def ocr_pdf_with_codex(
    pdf_path: str | Path,
    *,
    pages: str | int | Iterable[int] = "all",
    model: str = DEFAULT_CODEX_MODEL,
) -> dict[int, str]:
    results: dict[int, str] = {}
    for page_number, image in render_pdf_pages(pdf_path, pages):
        results[page_number] = ocr_page_with_codex(image, model=model)
    return results


def ocr_pdf(
    pdf_path: str | Path,
    *,
    provider: Provider = "both",
    pages: str | int | Iterable[int] = "all",
    openai_model: str = DEFAULT_OPENAI_MODEL,
    gemini_model: str = DEFAULT_GEMINI_MODEL,
    codex_model: str = DEFAULT_CODEX_MODEL,
) -> dict[str, dict[int, str]]:
    results: dict[str, dict[int, str]] = {}
    if provider in {"openai", "both"}:
        results["openai"] = ocr_pdf_with_openai(pdf_path, pages=pages, model=openai_model)
    if provider in {"gemini", "both"}:
        results["gemini"] = ocr_pdf_with_gemini(pdf_path, pages=pages, model=gemini_model)
    if provider == "codex":
        results["codex"] = ocr_pdf_with_codex(pdf_path, pages=pages, model=codex_model)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="OCR a PDF with Codex CLI, OpenAI GPT-5 nano, or Gemini 3 Flash")
    parser.add_argument("pdf_path", help="Path to the PDF")
    parser.add_argument(
        "--provider",
        choices=["openai", "gemini", "codex", "both"],
        default="codex",
        help="Which provider to use",
    )
    parser.add_argument(
        "--pages",
        default="all",
        help="Pages to OCR: all, 1, 1,3,5, or ranges like 1-3,7",
    )
    parser.add_argument("--openai-model", default=DEFAULT_OPENAI_MODEL)
    parser.add_argument("--gemini-model", default=DEFAULT_GEMINI_MODEL)
    parser.add_argument("--codex-model", default=DEFAULT_CODEX_MODEL)
    args = parser.parse_args()

    try:
        result = ocr_pdf(
            args.pdf_path,
            provider=args.provider,
            pages=args.pages,
            openai_model=args.openai_model,
            gemini_model=args.gemini_model,
            codex_model=args.codex_model,
        )
    except Exception as exc:
        raise SystemExit(str(exc))
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
