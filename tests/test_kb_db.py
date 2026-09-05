from __future__ import annotations

from pathlib import Path
import hashlib

import kb_db
import pytest


@pytest.mark.parametrize("query,background,distractor", [
    ("Как и почему это работает в Tokio?", "General implementation details and reference material.",
     "Как и почему это работает, если это нужно в этом случае."),
    ("How and why does this work with Tokio?", "Общие сведения о реализации и справочные материалы.",
     "How and why does this work with these examples?"),
])
def test_function_words_do_not_dominate_a_mixed_language_corpus(query, background, distractor):
    def record(path, title, body):
        return {"path": path, "title": title, "summary": "Reference note", "body": body,
                "updated": "2026-09-05", "sources": [],
                "content_hash": hashlib.sha256(body.encode()).hexdigest()}

    records = [record(f"concepts/background-{i}", f"Reference {i}", background) for i in range(60)]
    records.append(record("concepts/tokio", "Tokio concurrency", "Tokio schedules asynchronous tasks."))
    records.extend(record(f"concepts/unrelated-{i}", f"Unrelated subject {i}", distractor) for i in range(8))

    assert kb_db.search_records(query, records, 5)[0]["path"] == "concepts/tokio"


@pytest.mark.parametrize("query", ["как и почему это", "how and why this"])
def test_function_word_only_query_has_no_lexical_candidates(query):
    record = {"path": "concepts/unrelated", "title": "Unrelated", "summary": "Reference",
              "body": query, "updated": "2026-09-05", "sources": [], "content_hash": query}

    assert kb_db.search_records(query, [record], 5) == []


def test_single_keyword_search_remains_literal_for_language_keywords():
    record = {"path": "concepts/condition", "title": "Control flow", "summary": "Reference",
              "body": "if", "updated": "2026-09-05", "sources": [], "content_hash": "if"}

    assert kb_db.search_records("if", [record], 5)[0]["path"] == "concepts/condition"


@pytest.mark.parametrize("query", [
    "NOT IN", "DO WHILE", '"not in"', '"do while"',
    "Почему SQL NOT IN не возвращает строки с NULL?", "Как работает DO WHILE в C?",
    "Why does NOT IN exclude rows with NULL?", "How does DO WHILE work?",
])
def test_explicit_code_keywords_survive_function_word_filtering(query):
    record = {"path": "concepts/keywords", "title": "Language expressions", "summary": "Reference",
              "body": "NOT IN and DO WHILE", "updated": "2026-09-05", "sources": [], "content_hash": "keywords"}

    assert kb_db.search_records(query, [record], 5)[0]["path"] == "concepts/keywords"


HEADER = "# Index\n\n| Article | Summary | Compiled From | Updated |\n|---|---|---|---|\n"


@pytest.fixture(autouse=True)
def isolated_root(tmp_path, monkeypatch):
    monkeypatch.setattr(kb_db, "KNOWLEDGE_DIR", tmp_path / "knowledge")


def _setup_kb(monkeypatch, tmp_path: Path) -> Path:
    knowledge_dir = tmp_path / "knowledge"
    concepts = knowledge_dir / "concepts"
    concepts.mkdir(parents=True)

    (concepts / "docker-deploy.md").write_text(
        '---\ntitle: "Docker Deploy Patterns"\n---\n\n'
        "Force-recreate nginx after rsync; bind mounts pin old inodes.\n",
        encoding="utf-8",
    )
    (concepts / "vue-refactor.md").write_text(
        '---\ntitle: "Vue Refactor"\n---\n\n'
        "Extract visual subtrees. A docker aside mentioned once.\n",
        encoding="utf-8",
    )
    (concepts / "russian-topic.md").write_text(
        '---\ntitle: "Боевой рантайм"\n---\n\n'
        "Регенерация хитов в бою завязана на battle_users.\n",
        encoding="utf-8",
    )
    (knowledge_dir / "index.md").write_text(HEADER + "\n".join([
        "| [[concepts/docker-deploy]] | nginx recreate and stale inodes | daily/a.md, daily/b.md, daily/c.md | 2026-07-01 |",
        "| [[concepts/vue-refactor]] | vue subtree extraction | daily/b.md | 2026-07-16 |",
        "| [[concepts/russian-topic]] | регенерация в бою | daily/c.md | 2026-06-01 |",
    ]) + "\n", encoding="utf-8")

    monkeypatch.setattr(kb_db, "KNOWLEDGE_DIR", knowledge_dir)
    monkeypatch.setattr(kb_db, "INDEX_FILE", knowledge_dir / "index.md")
    monkeypatch.setattr(
        kb_db, "list_wiki_articles", lambda: sorted(concepts.glob("*.md"))
    )
    return tmp_path / "kb-index.sqlite"


def test_rebuild_and_search_roundtrip(monkeypatch, tmp_path: Path) -> None:
    db = _setup_kb(monkeypatch, tmp_path)

    count = kb_db.rebuild_index(db_path=db)
    assert count == 3

    results = kb_db.search("nginx rsync", db_path=db)
    assert results and results[0]["path"] == "concepts/docker-deploy"
    assert results[0]["title"] == "Docker Deploy Patterns"
    assert results[0]["updated"] == "2026-07-01"
    assert "nginx" in results[0]["snippet"].lower()


def test_search_supports_russian(monkeypatch, tmp_path: Path) -> None:
    db = _setup_kb(monkeypatch, tmp_path)
    kb_db.rebuild_index(db_path=db)

    results = kb_db.search("регенерация", db_path=db)

    assert results and results[0]["path"] == "concepts/russian-topic"


def test_search_ranks_title_match_above_body_mention(monkeypatch, tmp_path: Path) -> None:
    db = _setup_kb(monkeypatch, tmp_path)
    kb_db.rebuild_index(db_path=db)

    results = kb_db.search("docker", db_path=db)

    paths = [r["path"] for r in results]
    assert paths.index("concepts/docker-deploy") < paths.index("concepts/vue-refactor")


def test_search_sanitizes_punctuation(monkeypatch, tmp_path: Path) -> None:
    db = _setup_kb(monkeypatch, tmp_path)
    kb_db.rebuild_index(db_path=db)

    # raw FTS5 would raise on unbalanced quotes / operators
    results = kb_db.search('what\'s "nginx" AND (rsync?', db_path=db)
    assert results and results[0]["path"] == "concepts/docker-deploy"

    assert kb_db.search("§ №», ...", db_path=db) == []


def test_search_returns_none_without_db(tmp_path: Path) -> None:
    assert kb_db.search("anything", db_path=tmp_path / "missing.sqlite") is None


def test_rebuild_is_idempotent(monkeypatch, tmp_path: Path) -> None:
    db = _setup_kb(monkeypatch, tmp_path)
    kb_db.rebuild_index(db_path=db)
    count = kb_db.rebuild_index(db_path=db)

    assert count == 3
    assert len(kb_db.search("docker", db_path=db)) == 2  # no duplicate rows


def test_compile_index_slice_recent_candidates_and_pointer(monkeypatch, tmp_path: Path) -> None:
    db = _setup_kb(monkeypatch, tmp_path)
    kb_db.rebuild_index(db_path=db)
    monkeypatch.setattr(kb_db, "_today", lambda: "2026-07-17")

    log_text = "Session about nginx deploy: rsync recreate, stale inode pinning."
    view = kb_db.compile_index_slice(log_text, db_path=db)

    assert view is not None
    # recent row (updated within window)
    assert "concepts/vue-refactor" in view
    # FTS candidate from the log text despite an old updated date
    assert "concepts/docker-deploy" in view
    # pointer + anti-duplication instruction
    assert "index.md" in view
    assert "grep" in view.lower() or "Grep" in view


def test_compile_index_slice_none_without_db(monkeypatch, tmp_path: Path) -> None:
    _setup_kb(monkeypatch, tmp_path)

    assert kb_db.compile_index_slice("text", db_path=tmp_path / "missing.sqlite") is None


def test_find_similar_pairs_returns_mutual_neighbours(monkeypatch, tmp_path: Path) -> None:
    knowledge_dir = tmp_path / "knowledge"
    concepts = knowledge_dir / "concepts"
    concepts.mkdir(parents=True)

    # two articles on the same topic, one unrelated
    (concepts / "presence-system.md").write_text(
        '---\ntitle: "Online Presence System"\n---\n\n'
        "Online presence counters track logged-in players via the online table.\n",
        encoding="utf-8",
    )
    (concepts / "presence-audit.md").write_text(
        '---\ntitle: "Online Presence Audit"\n---\n\n'
        "Audit of online presence counters comparing the online table to legacy.\n",
        encoding="utf-8",
    )
    (concepts / "pastry-recipes.md").write_text(
        '---\ntitle: "Pastry Recipes"\n---\n\nButter flour sugar baking temperatures.\n',
        encoding="utf-8",
    )
    (knowledge_dir / "index.md").write_text(HEADER + "\n".join([
        "| [[concepts/presence-system]] | presence counters | daily/a.md | 2026-05-26 |",
        "| [[concepts/presence-audit]] | presence audit | daily/b.md | 2026-05-27 |",
        "| [[concepts/pastry-recipes]] | baking | daily/c.md | 2026-05-01 |",
    ]) + "\n", encoding="utf-8")

    monkeypatch.setattr(kb_db, "KNOWLEDGE_DIR", knowledge_dir)
    monkeypatch.setattr(kb_db, "INDEX_FILE", knowledge_dir / "index.md")
    monkeypatch.setattr(kb_db, "list_wiki_articles", lambda: sorted(concepts.glob("*.md")))
    db = tmp_path / "kb.sqlite"
    kb_db.rebuild_index(db_path=db)

    pairs = kb_db.find_similar_pairs(db_path=db)

    assert pairs is not None
    assert len(pairs) == 1
    a, b = pairs[0]["a"], pairs[0]["b"]
    assert {a, b} == {"concepts/presence-system", "concepts/presence-audit"}
    assert pairs[0]["linked"] is False  # neither wikilinks the other
    # BM25 IDF goes non-positive when a term appears in most of a tiny corpus,
    # so only the ordering of scores is meaningful here, not their sign.
    assert isinstance(pairs[0]["score"], float)


def test_find_similar_pairs_marks_already_linked(monkeypatch, tmp_path: Path) -> None:
    knowledge_dir = tmp_path / "knowledge"
    concepts = knowledge_dir / "concepts"
    concepts.mkdir(parents=True)
    (concepts / "alpha.md").write_text(
        '---\ntitle: "Dungeon Runtime"\n---\n\nDungeon runtime tables and instances.\n'
        "See [[concepts/beta]].\n", encoding="utf-8",
    )
    (concepts / "beta.md").write_text(
        '---\ntitle: "Dungeon Bots"\n---\n\nDungeon runtime bot instances and tables.\n'
        "See [[concepts/alpha]].\n", encoding="utf-8",
    )
    (knowledge_dir / "index.md").write_text(HEADER + "\n".join([
        "| [[concepts/alpha]] | dungeon runtime | daily/a.md | 2026-06-01 |",
        "| [[concepts/beta]] | dungeon bots | daily/b.md | 2026-06-02 |",
    ]) + "\n", encoding="utf-8")
    monkeypatch.setattr(kb_db, "KNOWLEDGE_DIR", knowledge_dir)
    monkeypatch.setattr(kb_db, "INDEX_FILE", knowledge_dir / "index.md")
    monkeypatch.setattr(kb_db, "list_wiki_articles", lambda: sorted(concepts.glob("*.md")))
    db = tmp_path / "kb.sqlite"
    kb_db.rebuild_index(db_path=db)

    pairs = kb_db.find_similar_pairs(db_path=db)

    assert pairs and pairs[0]["linked"] is True


def test_find_similar_pairs_none_without_db(tmp_path: Path) -> None:
    assert kb_db.find_similar_pairs(db_path=tmp_path / "missing.sqlite") is None


def test_find_similar_pairs_separates_strong_from_weak_overlap(
    monkeypatch, tmp_path: Path
) -> None:
    """Both pairs are mutually-nearest, so rank-only scoring ties them and the
    top-N degenerates into an alphabetical slice. A near-verbatim pair must
    outscore a pair sharing only one term."""
    knowledge_dir = tmp_path / "knowledge"
    concepts = knowledge_dir / "concepts"
    concepts.mkdir(parents=True)
    # Three mutually-nearest pairs of decreasing overlap. Rank-based scoring
    # gives all three the same value (each side is the other's top hit), which
    # is exactly the degeneracy this guards against.
    specs = {
        "aa-strong-one": "dungeon runtime instance tables spawn rows registry cleanup",
        "ab-strong-two": "dungeon runtime instance tables spawn rows registry cleanup",
        "mm-medium-one": "payment ledger operation alpha beta gamma delta epsilon",
        "mn-medium-two": "payment ledger operation zeta eta theta iota kappa",
        "zy-weak-one": "cron alpha1 beta1 gamma1 delta1 epsilon1 zeta1 eta1",
        "zz-weak-two": "cron theta1 iota1 kappa1 lambda1 mu1 nu1 xi1",
    }
    rows = []
    for slug, body in specs.items():
        (concepts / f"{slug}.md").write_text(
            f'---\ntitle: "{slug}"\n---\n\n{body}\n', encoding="utf-8"
        )
        # the summary IS the similarity profile — put the overlap there
        rows.append(f"| [[concepts/{slug}]] | {body} | daily/a.md | 2026-06-01 |")
    # filler documents so BM25 IDF stays positive (a term shared by most of a
    # tiny corpus scores ~0 and would flatten every pair)
    for i in range(12):
        slug = f"filler-{i:02d}"
        text = f"unrelated{i} filler{i} content{i} topic{i} noise{i}"
        (concepts / f"{slug}.md").write_text(
            f'---\ntitle: "{slug}"\n---\n\n{text}\n', encoding="utf-8"
        )
        rows.append(f"| [[concepts/{slug}]] | {text} | daily/a.md | 2026-06-01 |")
    (knowledge_dir / "index.md").write_text(HEADER + "\n".join(rows) + "\n", encoding="utf-8")
    monkeypatch.setattr(kb_db, "KNOWLEDGE_DIR", knowledge_dir)
    monkeypatch.setattr(kb_db, "INDEX_FILE", knowledge_dir / "index.md")
    monkeypatch.setattr(kb_db, "list_wiki_articles", lambda: sorted(concepts.glob("*.md")))
    db = tmp_path / "kb.sqlite"
    kb_db.rebuild_index(db_path=db)

    pairs = kb_db.find_similar_pairs(db_path=db)
    by_pair = {tuple(sorted((p["a"], p["b"]))): p["score"] for p in pairs}

    strong = by_pair[("concepts/aa-strong-one", "concepts/ab-strong-two")]
    medium = by_pair[("concepts/mm-medium-one", "concepts/mn-medium-two")]
    weak = by_pair[("concepts/zy-weak-one", "concepts/zz-weak-two")]
    assert strong > medium > weak, f"{strong=} {medium=} {weak=} must be ordered"
    assert [p["score"] for p in pairs] == sorted((p["score"] for p in pairs), reverse=True)
