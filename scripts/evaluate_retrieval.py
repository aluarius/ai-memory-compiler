"""Evaluate Russian gold questions without hiding missing paths or unavailable models."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import time
from pathlib import Path

import kb_db
from config import ROOT_DIR
from semantic_search import MODEL_NAME, SemanticIndex, SemanticUnavailable, hybrid_search

DEFAULT_FIXTURE = ROOT_DIR / "tests" / "fixtures" / "retrieval-ru.json"


def evaluate(root: Path, fixture: Path, *, modes: tuple[str, ...] = ("bm25", "hybrid"),
             index_root: Path | None = None) -> dict:
    report = {"status": "ok", "fixture": str(fixture), "missing_expected_paths": [], "modes": {}}
    try:
        fixture_bytes = fixture.read_bytes()
        data = json.loads(fixture_bytes)
        report["fixture_sha256"] = hashlib.sha256(fixture_bytes).hexdigest()
        cases = data["cases"]
        if not isinstance(cases, list) or not cases:
            raise ValueError("fixture must contain at least one case")
        unique_queries = set()
        for case in cases:
            if not isinstance(case.get("query"), str) or not case["query"].strip():
                raise ValueError("each case requires a nonempty query")
            normalized = " ".join(case["query"].casefold().split())
            if normalized in unique_queries:
                raise ValueError("duplicate query in fixture")
            unique_queries.add(normalized)
            expected = case.get("expected_paths")
            if not isinstance(expected, list) or not expected or any(not isinstance(p, str) or not p for p in expected):
                raise ValueError("each case requires nonempty expected_paths")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return {**report, "status": "invalid_fixture", "error": str(exc)}
    articles = kb_db.article_records(root)
    available = {article["path"] for article in articles}
    expected = {path for case in cases for path in case["expected_paths"]}
    missing = sorted(expected - available)
    report.update({"articles": len(articles), "questions": len(cases), "missing_expected_paths": missing})
    if missing:
        return {**report, "status": "invalid_fixture"}
    for case in cases:
        if case.get("project"):
            candidates = {a["path"] for a in articles if kb_db.matches_project(a, case["project"])}
            if not candidates.intersection(case["expected_paths"]):
                return {**report, "status": "invalid_fixture",
                        "error": f"expected paths excluded by project filter: {case['query']}"}
    index = SemanticIndex(index_root or root)
    for mode in modes:
        result = {"status": "ok", "backend": "sqlite-fts5-bm25" if mode == "bm25" else f"bm25+{MODEL_NAME}",
                  "hit_at_5": None, "latency_ms": None, "queries": []}
        report["modes"][mode] = result
        latencies = []
        for case in cases:
            started = time.perf_counter()
            try:
                candidates = [a for a in articles if not case.get("project") or kb_db.matches_project(a, case["project"])]
                if mode == "bm25":
                    hits = kb_db.search_records(case["query"], candidates, 5)
                elif mode == "hybrid":
                    hits = hybrid_search(case["query"], candidates, root=root, limit=5, index=index)
                else:
                    raise ValueError(f"unsupported mode: {mode}")
            except SemanticUnavailable as exc:
                result.update({"status": "unavailable", "error": str(exc)})
                report["status"] = "failed"
                break
            except Exception as exc:
                result.update({"status": "failed", "error": f"{type(exc).__name__}: {exc}"})
                report["status"] = "failed"
                break
            elapsed = (time.perf_counter() - started) * 1000
            latencies.append(elapsed)
            paths = [hit["path"] for hit in hits]
            result["queries"].append({"query": case["query"], "expected_paths": case["expected_paths"],
                                      "domain": case.get("domain"),
                                      "returned_paths": paths, "hit": bool(set(paths).intersection(case["expected_paths"])),
                                      "latency_ms": round(elapsed, 3)})
        if result["status"] == "ok":
            result["hit_at_5"] = sum(query["hit"] for query in result["queries"]) / len(cases)
            result["latency_ms"] = {"mean": round(statistics.mean(latencies), 3),
                                    "p50": round(statistics.median(latencies), 3),
                                    "p95": round(sorted(latencies)[math.ceil(len(latencies) * .95) - 1], 3),
                                    "first_query": round(latencies[0], 3)}
            if result["hit_at_5"] == 0:
                result["warning"] = "No gold question retrieved an expected article; inspect per-query results."
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT_DIR)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--index-root", type=Path, help="Use a disposable index outside a read-only corpus")
    parser.add_argument("--mode", action="append", choices=["bm25", "hybrid"])
    args = parser.parse_args()
    report = evaluate(args.root, args.fixture, modes=tuple(args.mode or ("bm25", "hybrid")), index_root=args.index_root)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
