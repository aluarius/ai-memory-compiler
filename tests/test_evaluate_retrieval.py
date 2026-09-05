from __future__ import annotations

import json


def test_gold_fixture_covers_diverse_questions_and_retains_original_miss():
    from evaluate_retrieval import DEFAULT_FIXTURE
    cases = json.loads(DEFAULT_FIXTURE.read_text(encoding="utf-8"))["cases"]
    assert len(cases) >= 30
    assert len({case["query"] for case in cases}) == len(cases)
    assert len({case["domain"] for case in cases}) >= 8
    assert all(case["source_paths"] and case["evidence"] for case in cases)
    original = next(case for case in cases if case["query"] ==
                    "Почему регулярное выражение не находит русские слова, хотя на английском работает?")
    assert original["expected_paths"] == ["concepts/js-regex-unicode-cyrillic", "connections/unicode-silent-destruction-patterns"]


def fixture(tmp_path, expected="concepts/backup"):
    articles = tmp_path / "knowledge" / "concepts"
    articles.mkdir(parents=True)
    (articles / "backup.md").write_text("---\ntitle: SQLite Backup\n---\nsqlite backup\n", encoding="utf-8")
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps({"cases": [{"query": "sqlite backup", "expected_paths": [expected]}]}), encoding="utf-8")
    return path


def test_evaluator_reports_bm25_and_unavailable_hybrid_separately(tmp_path):
    from evaluate_retrieval import evaluate
    report = evaluate(tmp_path, fixture(tmp_path))
    assert report["modes"]["bm25"]["hit_at_5"] == 1.0
    assert report["modes"]["bm25"]["latency_ms"]["p50"] >= 0
    assert report["modes"]["hybrid"]["status"] == "unavailable"
    assert report["modes"]["hybrid"]["hit_at_5"] is None
    assert report["status"] == "failed"


def test_evaluator_rejects_missing_expected_paths_instead_of_scoring_zero(tmp_path):
    from evaluate_retrieval import evaluate
    report = evaluate(tmp_path, fixture(tmp_path, "concepts/nonexistent"))
    assert report["status"] == "invalid_fixture"
    assert report["missing_expected_paths"] == ["concepts/nonexistent"]
    assert report["modes"] == {}


def test_evaluator_rejects_empty_gold_cases(tmp_path):
    from evaluate_retrieval import evaluate
    path = tmp_path / "fixture.json"
    path.write_text('{"cases": []}', encoding="utf-8")
    assert evaluate(tmp_path, path)["status"] == "invalid_fixture"


def test_evaluator_rejects_duplicate_questions_that_bias_hit_rate(tmp_path):
    from evaluate_retrieval import evaluate
    path = fixture(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["cases"].append({"query": "  SQLITE   backup ", "expected_paths": ["concepts/backup"]})
    path.write_text(json.dumps(data), encoding="utf-8")
    assert evaluate(tmp_path, path, modes=("bm25",))["status"] == "invalid_fixture"


def test_hit_rate_keeps_real_misses_and_has_reproducible_fixture_hash(tmp_path):
    import hashlib
    from evaluate_retrieval import evaluate
    path = fixture(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["cases"].append({"query": "unmatchedterm", "expected_paths": ["concepts/backup"]})
    path.write_text(json.dumps(data), encoding="utf-8")
    report = evaluate(tmp_path, path, modes=("bm25",))
    assert report["fixture_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert report["modes"]["bm25"]["hit_at_5"] == 0.5
    assert report["modes"]["bm25"]["queries"][1]["hit"] is False
