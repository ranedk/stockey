"""Text embeddings for passage search (docs/SCALE_OUT_PRD.md section 4.1). A small open model
(bge-small-en-v1.5, 384 numbers per text) run on the CPU. It only turns text into vectors for
finding similar passages; it writes nothing and judges nothing -- Claude does all the reading.
"""
from __future__ import annotations

import os
from functools import lru_cache

import numpy as np

MODEL = os.environ.get("INFRA_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
DIM = 384
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "   # bge's query side
THREADS = int(os.environ.get("INFRA_EMBED_THREADS", "8"))   # leave the main server room
BATCH = 32
MAX_TOKENS = 512


@lru_cache(maxsize=1)
def _model():
    import torch
    from transformers import AutoModel, AutoTokenizer
    torch.set_num_threads(THREADS)
    tok = AutoTokenizer.from_pretrained(MODEL)
    model = AutoModel.from_pretrained(MODEL).eval()
    return tok, model


def embed(texts: list[str], query: bool = False) -> np.ndarray:
    """Unit-length vectors, one row per text."""
    import torch
    tok, model = _model()
    out = []
    for i in range(0, len(texts), BATCH):
        part = [(QUERY_PREFIX + t) if query else t for t in texts[i:i + BATCH]]
        enc = tok(part, padding=True, truncation=True, max_length=MAX_TOKENS, return_tensors="pt")
        with torch.inference_mode():
            cls = model(**enc).last_hidden_state[:, 0]
        out.append(torch.nn.functional.normalize(cls, dim=-1).numpy())
    return np.vstack(out) if out else np.zeros((0, DIM), dtype=np.float32)


def to_pg(v: np.ndarray) -> str:
    """pgvector's text form."""
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"
