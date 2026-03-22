from __future__ import annotations

from typing import List

import pandas as pd
from paddleocr import PaddleOCR


class POCR:
    _instance = None
    _initialized = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if not POCR._initialized:
            self.ocr_engine = PaddleOCR(
                use_angle_cls=True, lang="en", ocr_version="PP-OCRv4"
            )
            POCR._initialized = True

    def ocr_page(self, image_path: str) -> str:
        ocr_results = self.ocr_engine.ocr(image_path)
        texts: List[str] = []
        for res in ocr_results:
            texts.append(" ".join(res['rec_texts']))
        return " ".join(texts)

