from __future__ import annotations

import os
import shutil
from pathlib import Path


COMMON_POPPLER_DIRS = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
)


def resolve_poppler_path() -> str | None:
    explicit = os.getenv("POPPLER_PATH")
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_dir():
            return str(path)
        if path.is_file():
            return str(path.parent)

    for executable in ("pdfinfo", "pdftoppm"):
        found = shutil.which(executable)
        if found:
            return str(Path(found).resolve().parent)

    for directory in COMMON_POPPLER_DIRS:
        path = Path(directory)
        if (path / "pdfinfo").exists() or (path / "pdftoppm").exists():
            return str(path)

    return None


def poppler_install_hint() -> str:
    return (
        "Poppler is required for PDF OCR. Install it with `brew install poppler` on macOS "
        "or `sudo apt-get install poppler-utils` on Ubuntu. If installed outside PATH, set "
        "`POPPLER_PATH` to the directory containing `pdfinfo` and `pdftoppm`."
    )
