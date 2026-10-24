"""SQLite persistence with schema versioning (PRAGMA user_version) and FTS5."""
from __future__ import annotations

import datetime as dt
import json
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

MIGRATIONS: list[str] = [
    # v1: core schema
    """
    CREATE TABLE projects(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL);
    CREATE TABLE runs(id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
        parent_run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL,
        question TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
        provider TEXT NOT NULL DEFAULT '', sensitive INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL, finished_at TEXT, report_md TEXT NOT NULL DEFAULT '',
        stats_json TEXT NOT NULL DEFAULT '{}', stages_json TEXT NOT NULL DEFAULT '[]',
        limitations_json TEXT NOT NULL DEFAULT '[]', error TEXT NOT NULL DEFAULT '',
        browser_prompt TEXT NOT NULL DEFAULT '', channel TEXT NOT NULL DEFAULT 'web');
    CREATE INDEX idx_runs_created ON runs(created_at);
    CREATE TABLE sources(id INTEGER PRIMARY KEY AUTOINCREMENT, canonical_url TEXT NOT NULL UNIQUE,
        url TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', domain TEXT NOT NULL DEFAULT '',
        source_type TEXT NOT NULL DEFAULT 'unverified', published TEXT, retrieved_at TEXT NOT NULL,
        content_hash TEXT NOT NULL DEFAULT '', text TEXT NOT NULL DEFAULT '', excerpt TEXT NOT NULL DEFAULT '',
        origin TEXT NOT NULL DEFAULT 'web', quality REAL NOT NULL DEFAULT 0, duplicate_of INTEGER,
        meta_json TEXT NOT NULL DEFAULT '{}');
    CREATE INDEX idx_sources_hash ON sources(content_hash);
    CREATE TABLE run_sources(run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
        label TEXT NOT NULL, rank INTEGER NOT NULL DEFAULT 0, query TEXT NOT NULL DEFAULT '',
        snippet TEXT NOT NULL DEFAULT '', provider TEXT NOT NULL DEFAULT '', fetched INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(run_id, source_id));
    CREATE TABLE passages(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE, text TEXT NOT NULL);
    CREATE TABLE claims(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        text TEXT NOT NULL, strength TEXT NOT NULL DEFAULT 'unverified', independent_count INTEGER NOT NULL DEFAULT 0,
        uncertainty TEXT NOT NULL DEFAULT '', origin TEXT NOT NULL DEFAULT 'model');
    CREATE TABLE claim_evidence(claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
        source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
        stance TEXT NOT NULL DEFAULT 'support', quote TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(claim_id, source_id, stance));
    CREATE TABLE notes(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        kind TEXT NOT NULL DEFAULT 'note', provider TEXT NOT NULL DEFAULT '', body TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE page_cache(url TEXT PRIMARY KEY, fetched_at TEXT NOT NULL, status INTEGER NOT NULL,
        content_type TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '', published TEXT,
        text TEXT NOT NULL DEFAULT '', final_url TEXT NOT NULL DEFAULT '', meta_json TEXT NOT NULL DEFAULT '{}');
    CREATE VIRTUAL TABLE fts USING fts5(kind UNINDEXED, ref_id UNINDEXED, title, body, tokenize='porter unicode61');
    """,
]

SCHEMA_VERSION = len(MIGRATIONS)


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def fts_query(q: str) -> str:
    """Turn free text into a safe FTS5 query (quoted terms, implicit AND)."""
    terms = ["".join(ch for ch in t if ch.isalnum()) for t in q.split()]
    terms = [t for t in terms if t][:12]
    return " ".join(f'"{t}"*' for t in terms)


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.migrate()

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA journal_mode=WAL")
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def version(self) -> int:
        with self.conn() as c:
            return c.execute("PRAGMA user_version").fetchone()[0]

    def migrate(self) -> None:
        with self._lock, self.conn() as c:
            v = c.execute("PRAGMA user_version").fetchone()[0]
            if v > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database schema v{v} is newer than this ATLAS build (v{SCHEMA_VERSION}). "
                    "Upgrade ATLAS or restore an older backup.")
            for i in range(v, SCHEMA_VERSION):
                c.executescript("BEGIN;" + MIGRATIONS[i] + f"PRAGMA user_version={i + 1};COMMIT;")

    # ---- generic helpers -------------------------------------------------
    def q(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple = ()) -> dict | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def ex(self, sql: str, args: tuple = ()) -> int:
        with self.conn() as c:
            return c.execute(sql, args).lastrowid

    # ---- search index ----------------------------------------------------
    def index(self, kind: str, ref_id: int, title: str, body: str) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM fts WHERE kind=? AND ref_id=?", (kind, ref_id))
            c.execute("INSERT INTO fts(kind,ref_id,title,body) VALUES(?,?,?,?)",
                      (kind, ref_id, title, body[:200000]))

    def unindex(self, kind: str, ref_ids: list[int]) -> None:
        with self.conn() as c:
            c.executemany("DELETE FROM fts WHERE kind=? AND ref_id=?", [(kind, i) for i in ref_ids])

    def search(self, text: str, kind: str | None = None, limit: int = 30) -> list[dict]:
        fq = fts_query(text)
        if not fq:
            return []
        sql = ("SELECT kind, ref_id, title, snippet(fts, 3, '[', ']', '…', 18) AS snippet "
               "FROM fts WHERE fts MATCH ?")
        args: list = [fq]
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        sql += " ORDER BY rank LIMIT ?"
        args.append(limit)
        return self.q(sql, tuple(args))

    # ---- backup / restore -----------------------------------------------
    def backup(self, dest: Path) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as src:
            out = sqlite3.connect(dest)
            try:
                src.backup(out)
            finally:
                out.close()
        return dest

    def restore(self, src: Path) -> None:
        src = Path(src)
        chk = sqlite3.connect(src)
        try:
            ok = chk.execute("PRAGMA integrity_check").fetchone()[0]
            ver = chk.execute("PRAGMA user_version").fetchone()[0]
        finally:
            chk.close()
        if ok != "ok" or ver > SCHEMA_VERSION:
            raise ValueError("Backup is corrupt or from a newer ATLAS version.")
        with self._lock:
            safety = self.path.with_suffix(".pre-restore")
            if self.path.exists():
                shutil.copy2(self.path, safety)
            for suf in ("-wal", "-shm"):
                p = Path(str(self.path) + suf)
                if p.exists():
                    p.unlink()
            shutil.copy2(src, self.path)
            self.migrate()

    # ---- cascading delete ------------------------------------------------
    def delete_runs(self, ids: list[int]) -> int:
        ids = [int(i) for i in ids]
        if not ids:
            return 0
        with self.conn() as c:
            marks = ",".join("?" * len(ids))
            n = c.execute(f"DELETE FROM runs WHERE id IN ({marks})", ids).rowcount  # nosec B608 - placeholders only, ids are ints
            c.execute(f"DELETE FROM fts WHERE kind IN ('run','note') AND ref_id IN ({marks})", ids)  # nosec B608
            # prune sources no longer referenced by any run
            orphans = [r[0] for r in c.execute(
                "SELECT id FROM sources WHERE id NOT IN (SELECT source_id FROM run_sources)").fetchall()]
            if orphans:
                om = ",".join("?" * len(orphans))
                c.execute(f"DELETE FROM sources WHERE id IN ({om})", orphans)  # nosec B608
                c.execute(f"DELETE FROM fts WHERE kind='source' AND ref_id IN ({om})", orphans)  # nosec B608
        return n


def j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)
