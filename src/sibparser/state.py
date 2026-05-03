"""SQLite-backed run state, dedup cache and resumable progress tracking.

Schema:

* ``categories``        - discovered category tree (path string + URL).
* ``products``          - discovered products with their source category and parsed status.
* ``files``             - file uploads keyed by source URL or content hash. Used to dedup
                          identical certificates (same URL or same content) so we upload
                          them once to the Drive ``_shared`` folder and create shortcuts.
* ``folders``           - cache of created Drive folder IDs by path.
* ``runs``              - completed run metadata (selected scope + finish time).
"""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    url            TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    parent_url     TEXT,
    path           TEXT NOT NULL,
    discovered_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    url            TEXT PRIMARY KEY,
    product_id     TEXT NOT NULL,
    name           TEXT,
    category_url   TEXT NOT NULL,
    category_path  TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending',
    error          TEXT,
    parsed_at      TEXT,
    drive_folder_id TEXT
);
CREATE INDEX IF NOT EXISTS ix_products_status ON products(status);
CREATE INDEX IF NOT EXISTS ix_products_category ON products(category_url);

CREATE TABLE IF NOT EXISTS files (
    source_url     TEXT PRIMARY KEY,
    sha256         TEXT,
    drive_file_id  TEXT NOT NULL,
    drive_parent_id TEXT NOT NULL,
    name           TEXT NOT NULL,
    size_bytes     INTEGER,
    uploaded_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_files_sha256 ON files(sha256);

CREATE TABLE IF NOT EXISTS folders (
    path           TEXT PRIMARY KEY,
    drive_id       TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    scope          TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    status         TEXT NOT NULL,
    summary        TEXT
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class State:
    """Lightweight wrapper around a SQLite database file.

    All public methods are short, synchronous and thread-safe. The runner uses
    them from worker threads while the FastAPI server reads from the request
    thread.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            try:
                yield conn
            finally:
                conn.close()

    # -- categories -----------------------------------------------------

    def upsert_category(
        self, url: str, name: str, parent_url: str | None, path: str
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO categories(url, name, parent_url, path, discovered_at)
                   VALUES(?, ?, ?, ?, ?)
                   ON CONFLICT(url) DO UPDATE SET name=excluded.name,
                       parent_url=excluded.parent_url, path=excluded.path""",
                (url, name, parent_url, path, _now()),
            )

    def list_categories(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM categories ORDER BY path")]

    # -- products -------------------------------------------------------

    def upsert_product(
        self,
        url: str,
        product_id: str,
        category_url: str,
        category_path: str,
        name: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO products(url, product_id, name, category_url, category_path, status)
                   VALUES(?, ?, ?, ?, ?, 'pending')
                   ON CONFLICT(url) DO UPDATE SET name=COALESCE(excluded.name, products.name),
                       category_url=excluded.category_url, category_path=excluded.category_path""",
                (url, product_id, name, category_url, category_path),
            )

    def mark_product(
        self,
        url: str,
        status: str,
        error: str | None = None,
        drive_folder_id: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE products SET status=?, error=?, parsed_at=?, drive_folder_id=COALESCE(?, drive_folder_id)
                   WHERE url=?""",
                (status, error, _now(), drive_folder_id, url),
            )

    def get_product_status(self, url: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT status FROM products WHERE url=?", (url,)).fetchone()
            return row["status"] if row else None

    def list_products(self, status: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM products WHERE status=? ORDER BY category_path, product_id",
                    (status,),
                )
            else:
                rows = conn.execute(
                    "SELECT * FROM products ORDER BY category_path, product_id"
                )
            return [dict(r) for r in rows]

    # -- files / dedup --------------------------------------------------

    def lookup_file_by_url(self, url: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM files WHERE source_url=?", (url,)).fetchone()
            return dict(row) if row else None

    def lookup_file_by_sha256(self, sha256: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM files WHERE sha256=? LIMIT 1", (sha256,)
            ).fetchone()
            return dict(row) if row else None

    def remember_file(
        self,
        source_url: str,
        sha256: str | None,
        drive_file_id: str,
        drive_parent_id: str,
        name: str,
        size_bytes: int | None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO files(source_url, sha256, drive_file_id, drive_parent_id, name, size_bytes, uploaded_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source_url) DO UPDATE SET sha256=excluded.sha256,
                       drive_file_id=excluded.drive_file_id, drive_parent_id=excluded.drive_parent_id,
                       name=excluded.name, size_bytes=excluded.size_bytes, uploaded_at=excluded.uploaded_at""",
                (source_url, sha256, drive_file_id, drive_parent_id, name, size_bytes, _now()),
            )

    # -- folder cache ---------------------------------------------------

    def get_folder(self, path: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT drive_id FROM folders WHERE path=?", (path,)).fetchone()
            return row["drive_id"] if row else None

    def remember_folder(self, path: str, drive_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO folders(path, drive_id, created_at) VALUES(?, ?, ?)
                   ON CONFLICT(path) DO UPDATE SET drive_id=excluded.drive_id""",
                (path, drive_id, _now()),
            )

    # -- runs -----------------------------------------------------------

    def start_run(self, scope: str) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO runs(scope, started_at, status) VALUES(?, ?, 'running')",
                (scope, _now()),
            )
            row_id = cur.lastrowid
            assert row_id is not None
            return row_id

    def finish_run(self, run_id: int, status: str, summary: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET finished_at=?, status=?, summary=? WHERE id=?",
                (_now(), status, summary, run_id),
            )
