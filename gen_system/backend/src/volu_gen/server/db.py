"""The server's SQLite database, `data/volu.db` (docs/architecture.md →
API server). The server is its only writer.

The schema version is SQLite's `user_version`. A new version adds a step to
MIGRATIONS; an existing step never changes.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

DB_NAME = "volu.db"

MIGRATIONS = [
    # 1: tracks, versions, presets, settings, play queue, job queue
    """
    CREATE TABLE tracks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,   -- ids are never reused
        key TEXT NOT NULL UNIQUE,            -- track dir name: sha256[:16] of the source
        name TEXT NOT NULL,
        original_path TEXT,
        added_at TEXT NOT NULL,
        -- a copy of what the Library shows; analysis.json is the source of truth
        analysis TEXT NOT NULL DEFAULT 'pending',   -- pending | done | failed
        duration REAL,
        bpm REAL,
        tempo_mode TEXT,                     -- grid | variable
        grid_override TEXT,                  -- JSON: analysis.json tempo.override
        active_version_id INTEGER REFERENCES versions(id) ON DELETE SET NULL
    );
    CREATE TABLE versions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,   -- ids are never reused
        track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK (kind IN ('draft', 'saved')),
        name TEXT,
        created_at TEXT NOT NULL,
        params TEXT NOT NULL,                -- JSON: DirectorParams
        plan TEXT,                           -- JSON: Plan; NULL until planned
        -- the rendered file; all NULL until the first render
        render_id TEXT,
        anim_path TEXT,                      -- relative to the data dir
        fps INTEGER,
        pixel_count INTEGER,
        geometry_hash TEXT,
        format_version INTEGER,
        generator_version INTEGER,
        grid_override TEXT,                  -- JSON: the tempo override the file was rendered on
        rendered_at TEXT
    );
    CREATE UNIQUE INDEX versions_one_draft ON versions(track_id) WHERE kind = 'draft';
    CREATE TABLE presets (
        id INTEGER PRIMARY KEY,
        generator TEXT NOT NULL,
        name TEXT NOT NULL,
        params TEXT NOT NULL,                -- JSON: the generator's track params
        UNIQUE (generator, name)
    );
    CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL                  -- JSON
    );
    CREATE TABLE play_queue (
        pos INTEGER PRIMARY KEY,
        track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE
    );
    CREATE TABLE jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,   -- ids are never reused
        kind TEXT NOT NULL,                  -- analyze | regrid | render
        track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
        version_id INTEGER REFERENCES versions(id) ON DELETE CASCADE,
        spec TEXT NOT NULL,                  -- JSON
        priority INTEGER NOT NULL,           -- jobs.Priority
        state TEXT NOT NULL DEFAULT 'queued',   -- queued | running | done | failed | cancelled
        created_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        result TEXT,                         -- JSON
        error TEXT,
        traceback TEXT
    );
    CREATE INDEX jobs_state ON jobs(state, priority, id);
    """,
]


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def dumps(value) -> str | None:
    return None if value is None else json.dumps(value, separators=(",", ":"))


def loads(text: str | None):
    return None if text is None else json.loads(text)


def connect(path: Path | str) -> sqlite3.Connection:
    """Open the database (":memory:" for tests) and bring its schema up to
    date. Rows are sqlite3.Row; use `with db:` around writes."""
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        db.execute("PRAGMA journal_mode = WAL")
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version > len(MIGRATIONS):
        raise RuntimeError(f"{path}: schema version {version} is newer than this server "
                           f"({len(MIGRATIONS)})")
    for v in range(version, len(MIGRATIONS)):
        db.executescript(f"BEGIN; {MIGRATIONS[v]}; PRAGMA user_version = {v + 1}; COMMIT;")
    return db
