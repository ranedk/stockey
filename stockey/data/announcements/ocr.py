from __future__ import annotations

from typing import List

import pandas as pd
from paddleocr import PPStructure, PaddleOCR


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
                use_angle_cls=True,
                lang="en",
                ocr_version="PP-OCRv4",
            )
            self.structure_engine = PPStructure(
                layout=False,
                ocr=False,
                table=True,
                lang="en",
                table_model_dir="en_ppstructure_mobile_v2.0_SLANet",
            )
            POCR._initialized = True

    def ocr_page(self, image_path: str) -> str:
        ocr_results = self.ocr_engine.ocr(image_path, cls=True)
        texts: List[str] = []
        for region in ocr_results:
            if region:
                for item in region:
                    if item:
                        texts.append(item[-1][0])
        return "\n".join(texts)

    def ocr_tables(self, image_path: str) -> List[str]:
        structure_results = self.structure_engine(image_path)
        tables: List[str] = []
        for item in structure_results:
            if item["type"] == "table":
                table_html = item["res"]["html"]
                tables.append(pd.DataFrame(pd.read_html(table_html)[0]).to_csv())
        return tables
