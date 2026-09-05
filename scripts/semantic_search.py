"""Disposable multilingual embeddings. Only the explicit index CLI downloads models."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Protocol

import kb_db
from config import ROOT_DIR

MODEL_NAME = "intfloat/multilingual-e5-small"
MODEL_FILE = "onnx/model.onnx"
MODEL_FORMAT = "mean-normalized:e5-prefix-v1"


class SemanticUnavailable(RuntimeError):
    """A missing, stale, or unusable optional semantic backend."""


class EmbeddingBackend(Protocol):
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class FastEmbedBackend:
    """Local CPU inference using the model author's official ONNX artifact."""

    def __init__(self, root: Path, *, model: str = MODEL_NAME, allow_download: bool = False) -> None:
        try:
            from fastembed import TextEmbedding
            from fastembed.common.model_description import ModelSource, PoolingType
        except ImportError as exc:
            raise SemanticUnavailable("install the semantic extra and run semantic_search.py index") from exc
        if model != MODEL_NAME:
            raise SemanticUnavailable(f"unsupported model: {model}")
        if model not in {row["model"] for row in TextEmbedding.list_supported_models()}:
            TextEmbedding.add_custom_model(
                model=model, pooling=PoolingType.MEAN, normalization=True,
                sources=ModelSource(hf=MODEL_NAME), dim=384, model_file=MODEL_FILE,
                description="Official multilingual E5 small ONNX", license="MIT", size_in_gb=0.47,
            )
        cache = Path(root) / "scripts" / ".models"
        if not allow_download and not cache.exists():
            raise SemanticUnavailable("model cache missing; run semantic_search.py index")
        try:
            self.model = TextEmbedding(model_name=model, cache_dir=str(cache), threads=2,
                                       providers=["CPUExecutionProvider"],
                                       local_files_only=not allow_download)
        except Exception as exc:
            raise SemanticUnavailable(f"model could not load locally ({type(exc).__name__})") from exc

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        try:
            return [vector.tolist() for vector in self.model.embed(
                [f"passage: {text}" for text in texts], batch_size=16)]
        except Exception as exc:
            raise SemanticUnavailable(f"local model inference failed ({type(exc).__name__})") from exc

    def embed_query(self, text: str) -> list[float]:
        try:
            return next(iter(self.model.embed([f"query: {text}"]))).tolist()
        except Exception as exc:
            raise SemanticUnavailable(f"local model inference failed ({type(exc).__name__})") from exc


def _fingerprint(article: dict) -> str:
    text = json.dumps([article["content_hash"], article["title"], article["summary"]], ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _document(article: dict) -> str:
    # E5 truncates at 512 tokens; lead with the curated title and summary.
    return f"{article['title']}\n{article['summary']}\n{article['body']}"


def _unit(vector: list[float]) -> list[float]:
    if not vector or any(not math.isfinite(value) for value in vector):
        raise SemanticUnavailable("invalid embedding vector")
    magnitude = math.sqrt(sum(value * value for value in vector))
    if not magnitude:
        raise SemanticUnavailable("zero embedding vector")
    return [float(value) / magnitude for value in vector]


class SemanticIndex:
    def __init__(self, root: Path, *, backend: EmbeddingBackend | None = None,
                 model: str = MODEL_NAME) -> None:
        self.root = Path(root)
        self.path = self.root / "scripts" / "semantic-index.sqlite"
        self.model = model
        self.identity = f"{model}:{MODEL_FILE}:{MODEL_FORMAT}"
        self.backend = backend

    def _backend(self, *, allow_download: bool = False) -> EmbeddingBackend:
        if self.backend is None:
            self.backend = FastEmbedBackend(self.root, model=self.model, allow_download=allow_download)
        return self.backend

    def rebuild(self, articles: list[dict], *, allow_download: bool = False) -> int:
        """Compute outside the transaction; atomically replace the disposable index."""
        try:
            previous = self._stored()
        except SemanticUnavailable:
            previous = {}
        cached = {}
        for article in articles:
            row = previous.get(article["path"])
            if row is not None and row[0] == _fingerprint(article):
                try:
                    cached[article["path"]] = _unit(json.loads(row[1]))
                except (TypeError, ValueError, SemanticUnavailable):
                    continue
        changed = [article for article in articles if article["path"] not in cached]
        vectors = self._backend(allow_download=allow_download).embed_documents(
            [_document(article) for article in changed]) if changed else []
        if len(vectors) != len(changed):
            raise SemanticUnavailable("embedding count does not match article count")
        cached.update({article["path"]: _unit(vector)
                       for article, vector in zip(changed, vectors, strict=True)})
        normalized = [cached[article["path"]] for article in articles]
        if len({len(vector) for vector in normalized}) > 1:
            raise SemanticUnavailable("embedding dimensions differ")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            with conn:
                conn.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                conn.execute("CREATE TABLE IF NOT EXISTS embeddings ("
                             "path TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, vector TEXT NOT NULL)")
                conn.execute("DELETE FROM embeddings")
                conn.execute("INSERT OR REPLACE INTO metadata VALUES ('model', ?)", (self.identity,))
                conn.executemany("INSERT INTO embeddings VALUES (?, ?, ?)",
                                 [(article["path"], _fingerprint(article), json.dumps(vector))
                                  for article, vector in zip(articles, normalized, strict=True)])
        finally:
            conn.close()
        return len(articles)

    def _stored(self) -> dict[str, tuple[str, str]]:
        if not self.path.exists():
            raise SemanticUnavailable("index missing; run semantic_search.py index")
        try:
            conn = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                identity = conn.execute("SELECT value FROM metadata WHERE key='model'").fetchone()
                if identity is None or identity[0] != self.identity:
                    raise SemanticUnavailable("index model differs; reindex required")
                stored = {path: (fingerprint, vector) for path, fingerprint, vector in conn.execute(
                    "SELECT path, fingerprint, vector FROM embeddings")}
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise SemanticUnavailable("index unreadable; reindex required") from exc
        return stored

    def search(self, query: str, articles: list[dict], limit: int = 10) -> list[dict]:
        """Read cached vectors only; verify all candidate hashes before inference."""
        stored = self._stored()
        for article in articles:
            row = stored.get(article["path"])
            if row is None or row[0] != _fingerprint(article):
                raise SemanticUnavailable("index stale; reindex required")
        if not articles or not query.strip() or limit <= 0:
            return []
        try:
            query_vector = _unit(self._backend().embed_query(query))
            scored = []
            for article in articles:
                vector = _unit(json.loads(stored[article["path"]][1]))
                if len(vector) != len(query_vector):
                    raise SemanticUnavailable("index dimensions differ; reindex required")
                score = sum(a * b for a, b in zip(query_vector, vector, strict=True))
                scored.append({**article, "semantic_score": score,
                               "snippet": article["summary"] or article["body"][:240]})
        except (TypeError, ValueError) as exc:
            raise SemanticUnavailable("invalid cached embedding; reindex required") from exc
        scored.sort(key=lambda article: (-article["semantic_score"], article["path"]))
        return scored[:limit]


@lru_cache(maxsize=2)
def _cached_index(root: Path) -> SemanticIndex:
    return SemanticIndex(root)


def hybrid_search(query: str, articles: list[dict], *, root: Path, limit: int = 10,
                  index: SemanticIndex | None = None) -> list[dict]:
    """Order by best provider rank, then reciprocal-rank agreement and path.

    ``hybrid_rank`` is the best provider rank, not the output position.
    ``hybrid_score`` is only the RRF tie-breaker, not a global sortable score.
    """
    count = max(limit * 4, 40)
    lexical = kb_db.search_records(query, articles, count)
    semantic = (index or _cached_index(Path(root).resolve())).search(query, articles, count)
    scores: dict[str, float] = {}
    best_ranks: dict[str, int] = {}
    records = {}
    for ranked in (semantic, lexical):
        for rank, article in enumerate(ranked, start=1):
            path = article["path"]
            best_ranks[path] = min(best_ranks.get(path, rank), rank)
            scores[path] = scores.get(path, 0.0) + 1 / (60 + rank)
            records[path] = article
    # Summed RRF alone lets two tail matches outrank even a unique first hit.
    # Best-rank ordering keeps both providers' leaders; agreement orders rank ties.
    paths = sorted(scores, key=lambda path: (best_ranks[path], -scores[path], path))
    return [{**records[path], "hybrid_rank": best_ranks[path], "hybrid_score": scores[path]}
            for path in paths[:limit]]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["index"])
    parser.add_argument("--root", type=Path, default=ROOT_DIR)
    parser.add_argument("--offline", action="store_true", help="Reindex only with already cached model files")
    args = parser.parse_args()
    try:
        count = SemanticIndex(args.root).rebuild(kb_db.article_records(args.root), allow_download=not args.offline)
    except SemanticUnavailable as exc:
        print(json.dumps({"status": "unavailable", "error": str(exc)}))
        return 1
    print(json.dumps({"status": "ok", "articles": count, "model": MODEL_NAME}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
