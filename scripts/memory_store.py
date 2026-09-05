"""Transactional, authoritative local memory; Markdown files are exports."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from article_schema import StoreValidationError, article_path, parse_article, source_path

SCHEMA_VERSION = 1


class StoreConflict(RuntimeError):
    """A stale snapshot or lease cannot overwrite newer memory."""


def content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def timestamp() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def project_matches(projects: Sequence[str], project: str) -> bool:
    query = project.rstrip("/")
    for candidate in projects:
        candidate = candidate.rstrip("/")
        if candidate == query or Path(candidate).name == Path(query).name:
            return True
        if candidate.startswith("/") and query.startswith(candidate + "/"):
            return True
    return False


class MemoryStore:
    """One root's durable data. Construction never creates or migrates a database."""

    def __init__(self, root: Path, *, readonly: bool = False) -> None:
        self.root = root.resolve()
        self.db_path = self.root / "scripts" / "memory.sqlite"
        self.readonly = readonly

    @staticmethod
    def is_initialized(root: Path) -> bool:
        store = MemoryStore(root, readonly=True)
        if not store.db_path.is_file():
            return False
        with store.connect(readonly=True) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version != SCHEMA_VERSION:
                raise StoreValidationError(f"Unsupported memory schema: {version}")
            return True

    @contextmanager
    def connect(self, readonly: bool = False) -> Iterator[sqlite3.Connection]:
        read = readonly or self.readonly
        uri = self.db_path.as_uri() + ("?mode=ro" if read else "?mode=rw")
        conn = sqlite3.connect(uri, uri=True, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        if self.readonly:
            raise PermissionError("This memory store is read-only")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def initialize(self) -> None:
        if self.readonly:
            raise PermissionError("This memory store is read-only")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise StoreValidationError(f"Unsupported memory schema: {version}")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sources (
                    path TEXT PRIMARY KEY, content TEXT NOT NULL,
                    content_hash TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0,
                    updated TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS source_events (
                    event_key TEXT PRIMARY KEY, source_path TEXT NOT NULL REFERENCES sources(path),
                    created TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS articles (
                    id TEXT NOT NULL UNIQUE, path TEXT PRIMARY KEY, title TEXT NOT NULL,
                    summary TEXT NOT NULL, body TEXT NOT NULL, content_hash TEXT NOT NULL,
                    created TEXT NOT NULL, updated TEXT NOT NULL, revision INTEGER NOT NULL,
                    sources_json TEXT NOT NULL, projects_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS article_sources (
                    article_path TEXT NOT NULL REFERENCES articles(path) ON DELETE CASCADE,
                    source_path TEXT NOT NULL REFERENCES sources(path),
                    PRIMARY KEY(article_path, source_path)
                );
                CREATE TABLE IF NOT EXISTS relations (
                    source TEXT NOT NULL REFERENCES articles(path) ON DELETE CASCADE,
                    target TEXT NOT NULL REFERENCES articles(path) ON DELETE CASCADE,
                    PRIMARY KEY(source, target)
                );
                CREATE TABLE IF NOT EXISTS revisions (
                    id INTEGER PRIMARY KEY, article_id TEXT NOT NULL, path TEXT NOT NULL,
                    revision INTEGER NOT NULL, body TEXT NOT NULL, summary TEXT NOT NULL,
                    projects_json TEXT NOT NULL, created TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(article_id, revision)
                );
                CREATE TABLE IF NOT EXISTS article_usage (
                    path TEXT PRIMARY KEY, count INTEGER NOT NULL, last_read TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, dedup_key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
                    context TEXT NOT NULL, metadata_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','running','failed','done','quarantined')),
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                    lease_token TEXT, lease_until REAL, last_error TEXT,
                    created TEXT NOT NULL, updated TEXT NOT NULL, result TEXT
                );
                CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(status, next_attempt, created);
                CREATE TABLE IF NOT EXISTS runtime_events (
                    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, detail TEXT NOT NULL, created TEXT NOT NULL
                );
                INSERT OR IGNORE INTO metadata VALUES ('generation', '0');
                PRAGMA user_version=1;
                COMMIT;
            """)
        finally:
            conn.close()

    @staticmethod
    def _get(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
        row = conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def _set(conn: sqlite3.Connection, key: str, value: Any) -> None:
        conn.execute("INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, json.dumps(value, ensure_ascii=False)))

    def get_state(self, namespace: str, default: Any = None) -> Any:
        with self.connect(readonly=True) as conn:
            return self._get(conn, namespace, default)

    def set_state(self, namespace: str, value: Any) -> None:
        with self.transaction() as conn:
            self._set(conn, namespace, value)

    def update_state(self, namespace: str, mutator: Callable[[dict], None]) -> dict:
        with self.transaction() as conn:
            state = self._get(conn, namespace, {})
            if not isinstance(state, dict):
                raise StoreValidationError(f"State namespace {namespace} is not a mapping")
            mutator(state)
            self._set(conn, namespace, state)
            return state

    def generation(self) -> int:
        return int(self.get_state("generation", 0))

    def snapshot(self) -> dict:
        """Read one consistent generation for compilation, export, and evaluation."""
        with self.connect(readonly=True) as conn:
            conn.execute("BEGIN")
            return {
                "generation": int(self._get(conn, "generation", 0)),
                "articles": [self._article(row) for row in conn.execute("SELECT * FROM articles ORDER BY path")],
                "sources": [dict(row) for row in conn.execute("SELECT * FROM sources ORDER BY path")],
                "pipeline": self._get(conn, "pipeline", {}),
                "build_log": self._get(conn, "build_log", "# Build Log\n\n"),
                "export_state": self._get(conn, "exports", {}),
            }

    @staticmethod
    def _article(row: sqlite3.Row) -> dict:
        result = dict(row)
        result["sources"] = json.loads(result.pop("sources_json"))
        result["projects"] = json.loads(result.pop("projects_json"))
        return result

    def list_articles(self, project: str | None = None) -> list[dict]:
        with self.connect(readonly=True) as conn:
            rows = [self._article(row) for row in conn.execute("SELECT * FROM articles ORDER BY path")]
        return [row for row in rows if project is None or project_matches(row["projects"], project)]

    def read_article(self, path: str) -> dict | None:
        path = article_path(path)
        with self.connect(readonly=True) as conn:
            row = conn.execute("SELECT * FROM articles WHERE path=?", (path,)).fetchone()
            return self._article(row) if row else None

    def revisions(self, path: str) -> list[dict]:
        with self.connect(readonly=True) as conn:
            rows = conn.execute("SELECT * FROM revisions WHERE path=? ORDER BY id", (article_path(path),))
            return [{**dict(row), "deleted": bool(row["deleted"])} for row in rows]

    def _import_source(self, conn: sqlite3.Connection, path: str, content: str, archived: bool) -> None:
        path = source_path(path)
        old = conn.execute("SELECT content FROM sources WHERE path=?", (path,)).fetchone()
        if old and not content.startswith(old[0]):
            raise StoreConflict(f"Source is append-only: {path}")
        conn.execute("""INSERT INTO sources VALUES (?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET
                     content=excluded.content, content_hash=excluded.content_hash,
                     archived=excluded.archived, updated=excluded.updated""",
                     (path, content, content_hash(content), int(archived), timestamp()))

    def import_source(self, path: str, content: str, *, archived: bool = False) -> None:
        with self.transaction() as conn:
            self._import_source(conn, path, content, archived)

    def list_sources(self, include_archive: bool = True) -> list[dict]:
        with self.connect(readonly=True) as conn:
            where = "" if include_archive else " WHERE archived=0"
            return [dict(row) for row in conn.execute("SELECT * FROM sources" + where + " ORDER BY path")]

    def read_source(self, path: str) -> str | None:
        with self.connect(readonly=True) as conn:
            row = conn.execute("SELECT content FROM sources WHERE path=?", (source_path(path),)).fetchone()
            return row[0] if row else None

    def _append_source(self, conn: sqlite3.Connection, path: str, text: str, event_key: str) -> bool:
        path = source_path(path)
        if conn.execute("SELECT 1 FROM source_events WHERE event_key=?", (event_key,)).fetchone():
            return False
        old = conn.execute("SELECT content FROM sources WHERE path=?", (path,)).fetchone()
        body = old[0] if old else f"# Daily Log: {Path(path).stem}\n\n## Sessions\n\n"
        self._import_source(conn, path, body + text, False)
        conn.execute("INSERT INTO source_events VALUES (?,?,?)", (event_key, path, timestamp()))
        return True

    def append_source(self, path: str, text: str, *, event_key: str) -> bool:
        with self.transaction() as conn:
            return self._append_source(conn, path, text, event_key)

    def commit_articles(
        self, changes: list[dict], *, source_updates: dict[str, dict] | None = None,
        expected_generation: int | None = None, build_entry: str | None = None,
        deletions: Sequence[str] = (),
    ) -> int:
        parsed = [parse_article(change) for change in changes]
        paths = [item["path"] for item in parsed]
        removed = {article_path(path) for path in deletions}
        if len(paths) != len(set(paths)) or removed.intersection(paths):
            raise StoreValidationError("Duplicate or conflicting article changes")
        with self.transaction() as conn:
            generation = int(self._get(conn, "generation", 0))
            if expected_generation is not None and generation != expected_generation:
                raise StoreConflict("Knowledge changed while the result was being prepared")
            current = {row["path"]: self._article(row) for row in conn.execute("SELECT * FROM articles")}
            if removed - current.keys():
                raise StoreValidationError("Cannot delete a missing article")
            known = (current.keys() | set(paths)) - removed
            sources = {row[0] for row in conn.execute("SELECT path FROM sources")}
            final = {key: parse_article(value) for key, value in current.items() if key not in removed}
            final.update({item["path"]: item for item in parsed})
            for item in final.values():
                missing_links = set(item["links"]) - known
                missing_sources = (set(item["sources"]) | set(item["source_links"])) - sources
                if missing_links or missing_sources:
                    raise StoreValidationError(f"{item['path']}: missing references {sorted(missing_links | missing_sources)}")
            for item in parsed:
                old = current.get(item["path"])
                projects = json.dumps(item["projects"], ensure_ascii=False)
                if old and old["body"] == item["body"] and old["summary"] == item["summary"] and old["projects"] == item["projects"]:
                    continue
                article_id = old["id"] if old else str(uuid.uuid4())
                revision = old["revision"] + 1 if old else 1
                conn.execute("""INSERT INTO articles VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(path) DO UPDATE SET title=excluded.title, summary=excluded.summary,
                    body=excluded.body, content_hash=excluded.content_hash, updated=excluded.updated,
                    revision=excluded.revision, sources_json=excluded.sources_json, projects_json=excluded.projects_json""",
                    (article_id, item["path"], item["title"], item["summary"], item["body"], content_hash(item["body"]),
                     item["created"], item["updated"], revision, json.dumps(item["sources"]), projects))
                conn.execute("""INSERT INTO revisions
                    (article_id,path,revision,body,summary,projects_json,created) VALUES (?,?,?,?,?,?,?)""",
                    (article_id, item["path"], revision, item["body"], item["summary"], projects, timestamp()))
            for path in removed:
                old = current[path]
                conn.execute("""INSERT INTO revisions
                    (article_id,path,revision,body,summary,projects_json,created,deleted) VALUES (?,?,?,?,?,?,?,1)""",
                    (old["id"], path, old["revision"] + 1, old["body"], old["summary"], json.dumps(old["projects"]), timestamp()))
                conn.execute("DELETE FROM articles WHERE path=?", (path,))
            conn.execute("DELETE FROM relations")
            conn.execute("DELETE FROM article_sources")
            for item in final.values():
                conn.executemany("INSERT INTO relations VALUES (?,?)", [(item["path"], p) for p in item["links"]])
                conn.executemany("INSERT INTO article_sources VALUES (?,?)", [(item["path"], p) for p in item["sources"]])
            if source_updates:
                for name, checkpoint in source_updates.items():
                    row = conn.execute("SELECT content FROM sources WHERE path=?", (source_path(f"daily/{Path(name).name}"),)).fetchone()
                    size = checkpoint.get("size")
                    digest = checkpoint.get("hash")
                    if not row or not isinstance(size, int) or size < 0 or not isinstance(digest, str) or len(digest) not in (16, 64):
                        raise StoreValidationError(f"Invalid ingestion checkpoint: {name}")
                    raw = row[0].encode("utf-8")
                    if size > len(raw) or hashlib.sha256(raw[:size]).hexdigest()[:len(digest)] != digest:
                        raise StoreConflict(f"Ingestion checkpoint does not match source: {name}")
                pipeline = self._get(conn, "pipeline", {})
                pipeline.setdefault("ingested", {}).update(source_updates)
                self._set(conn, "pipeline", pipeline)
            if build_entry:
                old_log = self._get(conn, "build_log", "")
                self._set(conn, "build_log", old_log + build_entry)
            self._set(conn, "generation", generation + 1)
            return generation + 1

    def record_read(self, path: str) -> None:
        with self.transaction() as conn:
            conn.execute("""INSERT INTO article_usage VALUES (?,1,?) ON CONFLICT(path)
                DO UPDATE SET count=count+1,last_read=excluded.last_read""", (article_path(path), timestamp()))

    def usage_counts(self) -> dict[str, int]:
        with self.connect(readonly=True) as conn:
            return dict(conn.execute("SELECT path,count FROM article_usage"))

    def enqueue(self, context: str, metadata: dict, *, identity: str, kind: str = "flush") -> str:
        with self.transaction() as conn:
            return self._enqueue(conn, context, metadata, identity=identity, kind=kind)

    @staticmethod
    def _enqueue(conn: sqlite3.Connection, context: str, metadata: dict, *, identity: str, kind: str = "flush") -> str:
        if not context.strip():
            raise StoreValidationError("Cannot enqueue an empty context")
        key = content_hash(f"{kind}\0{identity}\0{context}")
        job_id = str(uuid.uuid4())
        conn.execute("""INSERT OR IGNORE INTO jobs
            (id,dedup_key,kind,context,metadata_json,status,created,updated) VALUES (?,?,?,?,?,'pending',?,?)""",
            (job_id, key, kind, context, json.dumps(metadata, ensure_ascii=False), timestamp(), timestamp()))
        return conn.execute("SELECT id FROM jobs WHERE dedup_key=?", (key,)).fetchone()[0]

    def capture_batch(self, key: str | None, *, expected: dict | None, checkpoint: dict | None,
                      captures: list[dict]) -> list[str]:
        """Persist every context and its source checkpoint in the same transaction."""
        with self.transaction() as conn:
            state = self._get(conn, "capture_checkpoints", {})
            if key is not None and state.get(key) != expected:
                raise StoreConflict("Capture checkpoint changed concurrently")
            ids = [self._enqueue(conn, **capture) for capture in captures]
            if key is not None:
                state[key] = checkpoint
                self._set(conn, "capture_checkpoints", state)
            return ids

    def claim_job(self, job_id: str | None = None, *, force: bool = False, lease_seconds: float = 1500) -> dict | None:
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("Lease duration must be positive")
        now = time.time()
        with self.transaction() as conn:
            conditions = ["kind='flush'", "(status IN ('pending','failed') OR (status='running' AND lease_until < ?))"]
            params: list[Any] = [now]
            if not force:
                conditions.append("next_attempt<=?")
                params.append(now)
            if job_id:
                conditions.append("id=?")
                params.append(job_id)
            row = conn.execute("SELECT * FROM jobs WHERE " + " AND ".join(conditions) + " ORDER BY created,rowid LIMIT 1", params).fetchone()
            if not row:
                return None
            token = str(uuid.uuid4())
            conn.execute("""UPDATE jobs SET status='running',attempts=attempts+1,
                lease_token=?,lease_until=?,updated=? WHERE id=?""", (token, now + lease_seconds, timestamp(), row["id"]))
            result = dict(conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())
            result["metadata"] = json.loads(result.pop("metadata_json"))
            return result

    @staticmethod
    def _require_lease(conn: sqlite3.Connection, job_id: str, token: str) -> None:
        row = conn.execute("SELECT status,lease_token,lease_until FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row or row["status"] != "running" or row["lease_token"] != token or row["lease_until"] < time.time():
            raise StoreConflict("Job lease expired or belongs to another worker")

    def complete_job(self, job_id: str, token: str, response: str, daily_path: str) -> None:
        with self.transaction() as conn:
            self._require_lease(conn, job_id, token)
            if response:
                self._append_source(conn, daily_path, response, f"job:{job_id}")
            conn.execute("""UPDATE jobs SET status='done',result=?,lease_token=NULL,
                lease_until=NULL,last_error=NULL,updated=? WHERE id=?""", (response, timestamp(), job_id))

    def fail_job(self, job_id: str, token: str, error: str, *, delay_seconds: float = 300, quarantine: bool = False) -> None:
        with self.transaction() as conn:
            self._require_lease(conn, job_id, token)
            conn.execute("""UPDATE jobs SET status=?,last_error=?,next_attempt=?,lease_token=NULL,
                lease_until=NULL,updated=? WHERE id=?""", ("quarantined" if quarantine else "failed", error[:2000],
                time.time() + delay_seconds, timestamp(), job_id))

    def jobs(self, statuses: Sequence[str] = ()) -> list[dict]:
        with self.connect(readonly=True) as conn:
            where = " WHERE status IN (" + ",".join("?" for _ in statuses) + ")" if statuses else ""
            rows = conn.execute("SELECT * FROM jobs" + where + " ORDER BY created,rowid", list(statuses))
            result = []
            for row in rows:
                item = dict(row)
                item["metadata"] = json.loads(item.pop("metadata_json"))
                result.append(item)
            return result

    def event(self, kind: str, detail: str) -> None:
        with self.transaction() as conn:
            conn.execute("INSERT INTO runtime_events(kind,detail,created) VALUES (?,?,?)", (kind, detail[:4000], timestamp()))

    def backup(self, destination: Path) -> None:
        if destination.exists():
            raise FileExistsError(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.connect(readonly=True) as conn:
            target = sqlite3.connect(destination)
            try:
                conn.backup(target)
                # Publish a self-contained snapshot, not a WAL-mode main file
                # whose first read-only connection may require missing sidecars.
                target.execute("PRAGMA journal_mode=DELETE")
            finally:
                target.close()

    def integrity_check(self) -> list[str]:
        with self.connect(readonly=True) as conn:
            messages = [row[0] for row in conn.execute("PRAGMA integrity_check")]
            errors = [message for message in messages if message != "ok"]
            errors.extend(str(tuple(row)) for row in conn.execute("PRAGMA foreign_key_check"))
            return errors


def is_initialized(root: Path) -> bool:
    return MemoryStore.is_initialized(root)
