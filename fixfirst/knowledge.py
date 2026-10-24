"""Document library: manuals and service docs (knowledge/docs + uploads), chunked and
searchable with BM25. It backs the citations on every playbook step and, when
ENABLE_DOC_QA is on, an informational lookup tool for the agent."""
import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from rank_bm25 import BM25Okapi

from .config import DATA_DIR, KNOWLEDGE_DIR

UPLOAD_DIR = DATA_DIR / "docs"
ALLOWED = {".md", ".txt", ".pdf"}
_TOKEN = re.compile(r"[a-z0-9]+|[ऀ-ॿ]+")


@dataclass
class Chunk:
    doc: str
    title: str
    source: str | None
    url: str | None
    appliance: str | None
    location: str
    text: str


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def _front_matter(text: str) -> tuple[dict, str]:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return yaml.safe_load(text[3:end]) or {}, text[end + 4:]
    return {}, text


def _chunks_from_markdown(name: str, text: str) -> list[Chunk]:
    meta, body = _front_matter(text)
    title = meta.get("title") or name
    chunks, heading, buf = [], title, []

    def flush():
        joined = "\n".join(buf).strip()
        if joined:
            chunks.append(Chunk(name, title, meta.get("source"), meta.get("url"), meta.get("appliance"),
                                heading, joined))

    for line in body.splitlines():
        if line.startswith("#"):
            flush()
            heading, buf = line.lstrip("#").strip(), []
        else:
            buf.append(line)
    flush()
    return chunks


def _chunks_from_pdf(path: Path) -> list[Chunk]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    chunks = []
    for i, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        for j in range(0, len(text), 1200):
            part = text[j:j + 1200].strip()
            if part:
                chunks.append(Chunk(path.name, path.stem, None, None, None, f"page {i}", part))
    return chunks


class DocLibrary:
    def __init__(self, dirs: list[Path] | None = None):
        self.dirs = dirs or [KNOWLEDGE_DIR / "docs", UPLOAD_DIR]
        self.reindex()

    def reindex(self) -> None:
        self.chunks: list[Chunk] = []
        self.files: list[dict] = []
        for d in self.dirs:
            if not d.exists():
                continue
            for path in sorted(d.iterdir()):
                if path.suffix.lower() not in ALLOWED:
                    continue
                if path.suffix.lower() == ".pdf":
                    new = _chunks_from_pdf(path)
                else:
                    new = _chunks_from_markdown(path.name, path.read_text(encoding="utf-8", errors="ignore"))
                self.chunks.extend(new)
                meta = new[0] if new else None
                self.files.append({"name": path.name, "title": meta.title if meta else path.stem,
                                   "source": meta.source if meta else None, "url": meta.url if meta else None,
                                   "chunks": len(new), "uploaded": d == UPLOAD_DIR})
        corpus = [_tokens(f"{c.title} {c.location} {c.text}") for c in self.chunks]
        self.bm25 = BM25Okapi(corpus) if corpus else None

    def search(self, query: str, k: int = 4, appliance: str | None = None) -> list[dict]:
        if not self.bm25 or not query.strip():
            return []
        scores = self.bm25.get_scores(_tokens(query))
        ranked = sorted(range(len(self.chunks)), key=lambda i: scores[i], reverse=True)
        out = []
        for i in ranked:
            c = self.chunks[i]
            if scores[i] <= 0:
                break
            if appliance and c.appliance not in (None, appliance, "all"):
                continue
            out.append({"doc": c.doc, "title": c.title, "location": c.location, "source": c.source,
                        "url": c.url, "score": round(float(scores[i]), 3), "text": c.text[:900]})
            if len(out) >= k:
                break
        return out

    def save_upload(self, filename: str, data: bytes) -> Path:
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).name)[:120]
        if Path(name).suffix.lower() not in ALLOWED:
            raise ValueError("Only .pdf, .md and .txt files are supported")
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        path = UPLOAD_DIR / name
        path.write_bytes(data)
        self.reindex()
        return path
