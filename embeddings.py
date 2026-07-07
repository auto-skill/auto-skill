"""Local gte-small embeddings, bit-compatible with Supabase Edge Functions.

Uses the exact ONNX weights the Supabase Edge runtime runs
(huggingface.co/Supabase/gte-small, onnx/model_quantized.onnx) so document
embeddings written by this machine and query embeddings computed at the edge
live in the same vector space. Mean-pooled over the attention mask, then
L2-normalized, matching session.run(text, {mean_pool: true, normalize: true}).
"""
import hashlib
import json
import re
from pathlib import Path

import numpy as np
import onnxruntime
from huggingface_hub import hf_hub_download
from tokenizers import Tokenizer

MODEL_REPO = "Supabase/gte-small"
MODEL_FILE = "onnx/model_quantized.onnx"
MAX_TOKENS = 512
EMBED_DIM = 384

# gte-small's 512-token window is ~1,800-2,000 chars of English; text beyond
# that is truncated by the tokenizer anyway, so don't bother sending it.
MAX_EMBED_CHARS = 2000
MAX_CONTENT_CHARS = 1500

_session = None
_tokenizer = None


def _load():
    global _session, _tokenizer
    if _session is None:
        model_path = hf_hub_download(MODEL_REPO, MODEL_FILE)
        tokenizer_path = hf_hub_download(MODEL_REPO, "tokenizer.json")
        _session = onnxruntime.InferenceSession(model_path, providers=["CPUExecutionProvider"])
        _tokenizer = Tokenizer.from_file(tokenizer_path)
        _tokenizer.enable_truncation(max_length=MAX_TOKENS)
    return _session, _tokenizer


def embed_texts(texts: list[str], batch_size: int = 32) -> list[list[float]]:
    """Embed a list of strings -> list of 384-dim L2-normalized vectors."""
    session, tokenizer = _load()
    out: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        batch = [t if t.strip() else " " for t in texts[i:i + batch_size]]
        encodings = tokenizer.encode_batch(batch)
        max_len = max(len(e.ids) for e in encodings)
        input_ids = np.zeros((len(batch), max_len), dtype=np.int64)
        attention_mask = np.zeros((len(batch), max_len), dtype=np.int64)
        token_type_ids = np.zeros((len(batch), max_len), dtype=np.int64)
        for j, enc in enumerate(encodings):
            input_ids[j, :len(enc.ids)] = enc.ids
            attention_mask[j, :len(enc.ids)] = enc.attention_mask
        (hidden,) = session.run(None, {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "token_type_ids": token_type_ids,
        })
        mask = attention_mask[:, :, None].astype(np.float32)
        pooled = (hidden * mask).sum(axis=1) / np.clip(mask.sum(axis=1), 1e-9, None)
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        pooled = pooled / np.clip(norms, 1e-9, None)
        out.extend(pooled.astype(np.float32).tolist())
    return out


_FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.S)
_BADGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_HTML_TAG_RE = re.compile(r"<[^>]{1,200}>")
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_WS_RE = re.compile(r"\s+")


def clean_markdown(text: str) -> str:
    text = _FRONTMATTER_RE.sub(" ", text)
    text = _BADGE_RE.sub(" ", text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = _LINK_RE.sub(r"\1", text)  # keep link text, drop URLs
    return _WS_RE.sub(" ", text).strip()


def build_embed_text(skill: dict, content: str = "") -> str:
    """The canonical text a skill is embedded from. Changing this invalidates
    all stored embeddings (embedding_text_hash catches that per row)."""
    parts = [skill.get("name") or ""]
    if skill.get("description"):
        parts.append(skill["description"])
    tags = skill.get("tags") or []
    if tags:
        parts.append(" ".join(str(t) for t in tags))
    if content:
        parts.append(clean_markdown(content)[:MAX_CONTENT_CHARS])
    return _WS_RE.sub(" ", ". ".join(p for p in parts if p).strip())[:MAX_EMBED_CHARS]


def embed_text_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


class LibraryContent:
    """url -> saved .md content lookup, backed by skills_library/index.json."""

    def __init__(self, library_dir: Path | None = None):
        self.library_dir = library_dir or Path(__file__).parent / "skills_library"
        self.files_dir = self.library_dir / "files"
        self._index: dict[str, str] = {}
        self._hash_index: dict[str, str] = {}
        index_path = self.library_dir / "index.json"
        if index_path.exists():
            try:
                raw = json.loads(index_path.read_text(encoding="utf-8"))
                self._index = {url: entry.get("file", "") for url, entry in raw.items() if entry.get("file")}
                self._hash_index = {
                    entry.get("content_hash", ""): entry.get("file", "")
                    for entry in raw.values()
                    if entry.get("content_hash") and entry.get("file")
                }
            except Exception:
                self._index = {}
                self._hash_index = {}

    def get(self, url: str) -> str:
        filename = self._index.get(url)
        if not filename:
            return ""
        try:
            return (self.files_dir / filename).read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""

    def get_by_hash(self, hash_value: str) -> str:
        filename = self._hash_index.get(hash_value)
        if not filename:
            return ""
        try:
            return (self.files_dir / filename).read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""
