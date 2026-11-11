"""Tracks and versions in the DB, and their files under `data/tracks/`
(docs/architecture.md → Storage layout, Lifecycle)."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from ..analysis import TrackDir, import_track
from ..analysis.track import AUDIO_EXTS, SOURCE_STEM, TRACKS_DIR
from ..animfile import FORMAT_VERSION
from ..generators import DIRECTOR_VERSION, METHODS, DirectorParams
from . import db as dbm
from . import jobs
from .display import Display


class Library:
    def __init__(self, db: sqlite3.Connection, data_dir: Path, display: Display):
        self.db = db
        self.data_dir = Path(data_dir)
        self.display = display

    @property
    def tracks_dir(self) -> Path:
        return self.data_dir / TRACKS_DIR

    def track_dir(self, key: str) -> TrackDir:
        return TrackDir(self.tracks_dir / key)

    def audio_path(self, key: str) -> str | None:
        """The track's source audio, relative to the data dir (without
        reading its analysis.json)."""
        found = sorted((self.tracks_dir / key).glob(SOURCE_STEM + ".*"))
        return found[0].relative_to(self.data_dir).as_posix() if found else None

    # -- import
    def import_file(self, path: Path, original: str | None = None) -> TrackDir:
        """Copy one audio file in (blocking: run off the event loop)."""
        return import_track(path, self.data_dir, original)

    def add(self, t: TrackDir) -> tuple[int, bool]:
        """The track's row, made (with an `analyze` job) if it has none.
        Returns (track id, created). A directory without a row (kept after
        a removal) gets a new row; analysis then skips finished stages."""
        key = t.dir.name
        row = self.db.execute("SELECT id FROM tracks WHERE key = ?", (key,)).fetchone()
        if row is not None:
            return row["id"], False
        src = t.data["source"]
        with self.db:
            track_id = self.db.execute(
                "INSERT INTO tracks (key, name, original_path, added_at) VALUES (?, ?, ?, ?)",
                (key, t.name, src.get("original_path"), dbm.now())).lastrowid
        self.sync_analysis(track_id, t, "pending")
        jobs.queue_analyze(self.db, track_id, key)
        return track_id, True

    def sync_analysis(self, track_id: int, t: TrackDir, status: str):
        """Copy what the Library shows from analysis.json into the row."""
        tempo = t.data.get("tempo") or {}
        with self.db:
            self.db.execute(
                "UPDATE tracks SET analysis = ?, duration = ?, bpm = ?, tempo_mode = ?, grid_override = ? "
                "WHERE id = ?",
                (status, t.data["source"].get("duration"), tempo.get("bpm"), tempo.get("mode"),
                 dbm.dumps(tempo.get("override")), track_id))

    # -- queries
    def track(self, track_id: int) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
        if row is None:
            raise KeyError(f"no track {track_id}")
        return row

    def version(self, version_id: int) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM versions WHERE id = ?", (version_id,)).fetchone()
        if row is None:
            raise KeyError(f"no version {version_id}")
        return row

    def draft(self, track_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM versions WHERE track_id = ? AND kind = 'draft'",
                               (track_id,)).fetchone()

    def versions(self, track_id: int) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM versions WHERE track_id = ? "
                               "ORDER BY kind = 'saved', id", (track_id,)).fetchall()

    # -- versions
    def set_draft_plan(self, track_id: int, params: DirectorParams, plan: dict) -> int:
        """Store track params and plan in the track's draft row, making the
        row if needed. A track with no active version gets the draft."""
        params_json = dbm.dumps(params.model_dump(mode="json"))
        draft = self.draft(track_id)
        with self.db:
            if draft is None:
                vid = self.db.execute(
                    "INSERT INTO versions (track_id, kind, created_at, params, plan) VALUES (?, 'draft', ?, ?, ?)",
                    (track_id, dbm.now(), params_json, dbm.dumps(plan))).lastrowid
            else:
                vid = draft["id"]
                self.db.execute("UPDATE versions SET params = ?, plan = ? WHERE id = ?",
                                (params_json, dbm.dumps(plan), vid))
            self.db.execute("UPDATE tracks SET active_version_id = ? WHERE id = ? AND active_version_id IS NULL",
                            (vid, track_id))
        return vid

    def ensure_draft(self, track_id: int, params: DirectorParams) -> int:
        """The track's draft row; a new one has `params` and no plan yet
        (it is planned when its render starts)."""
        d = self.draft(track_id)
        if d is not None:
            return d["id"]
        with self.db:
            vid = self.db.execute(
                "INSERT INTO versions (track_id, kind, created_at, params) VALUES (?, 'draft', ?, ?)",
                (track_id, dbm.now(), dbm.dumps(params.model_dump(mode="json")))).lastrowid
            self.db.execute("UPDATE tracks SET active_version_id = ? WHERE id = ? AND active_version_id IS NULL",
                            (vid, track_id))
        return vid

    def apply_render(self, version_id: int, result: dict):
        """A finished render: the version gets the new file (the old one is
        deleted by cleanup once nothing uses it)."""
        with self.db:
            self.db.execute(
                "UPDATE versions SET render_id = ?, anim_path = ?, fps = ?, pixel_count = ?, geometry_hash = ?, "
                "format_version = ?, generator_version = ?, method_versions = ?, grid_override = ?, rendered_at = ? "
                "WHERE id = ?",
                (result["render_id"], result["anim_path"], result["fps"], result["pixel_count"],
                 result["geometry_hash"], result["format_version"], result["generator_version"],
                 dbm.dumps(result["method_versions"]), dbm.dumps(result["grid_override"]), dbm.now(), version_id))

    def anim_paths(self) -> set[str]:
        """Every animation file a version row references."""
        return {r[0] for r in self.db.execute("SELECT anim_path FROM versions WHERE anim_path IS NOT NULL")}

    def save_draft(self, track_id: int, name: str | None) -> int:
        """Save the rendered draft as a version: same params, plan and file
        (no copy); the two rows share the file until the next draft render."""
        d = self.draft(track_id)
        if d is None or d["anim_path"] is None:
            raise ValueError("the draft has not been rendered")
        cols = [c for c in d.keys() if c not in ("id", "kind", "name", "created_at")]
        with self.db:
            return self.db.execute(
                f"INSERT INTO versions (kind, name, created_at, {', '.join(cols)}) "
                f"VALUES ('saved', ?, ?, {', '.join('?' * len(cols))})",
                (name, dbm.now(), *(d[c] for c in cols))).lastrowid

    def set_active(self, track_id: int, version_id: int):
        if self.version(version_id)["track_id"] != track_id:
            raise KeyError(f"version {version_id} is not a version of track {track_id}")
        with self.db:
            self.db.execute("UPDATE tracks SET active_version_id = ? WHERE id = ?", (version_id, track_id))

    # -- status
    def stale_reasons(self, v: sqlite3.Row, track: sqlite3.Row) -> list[str]:
        """Why a rendered version's file is stale (Lifecycle). A stale file
        still plays if it is playable."""
        if v["anim_path"] is None:
            return []
        reasons = []
        if v["geometry_hash"] != self.display.geometry.hash:
            reasons.append("geometry")
        if v["generator_version"] != DIRECTOR_VERSION:
            reasons.append("generator")
        # the file's method versions, not the row's plan: that can be newer than the file
        if any(m not in METHODS or METHODS[m].version != ver
               for m, ver in (dbm.loads(v["method_versions"]) or {}).items()):
            reasons.append("method")
        if v["format_version"] != FORMAT_VERSION:
            reasons.append("format")
        if dbm.loads(v["grid_override"]) != dbm.loads(track["grid_override"]):
            reasons.append("grid")
        return reasons

    def playable(self, v: sqlite3.Row, display_fps: int) -> bool:
        """Rendered at the display FPS, for this pixel count, in a format the
        reader still supports."""
        return (v["anim_path"] is not None and v["fps"] == display_fps
                and v["pixel_count"] == self.display.geometry.pixel_count
                and v["format_version"] == FORMAT_VERSION)

    def version_dict(self, v: sqlite3.Row, track: sqlite3.Row, display_fps: int, plan: bool = True) -> dict:
        d = {k: v[k] for k in ("id", "track_id", "kind", "name", "created_at", "render_id", "fps",
                               "generator_version", "rendered_at")}
        d["params"] = dbm.loads(v["params"])
        if plan:
            d["plan"] = dbm.loads(v["plan"])
        d["active"] = track["active_version_id"] == v["id"]
        d["rendered"] = v["anim_path"] is not None
        d["playable"] = self.playable(v, display_fps)
        d["fps_mismatch"] = d["rendered"] and v["fps"] != display_fps
        d["stale"] = self.stale_reasons(v, track)
        return d

    def animation_status(self, track: sqlite3.Row, display_fps: int) -> str:
        """none | rendering | ready | stale | fps_mismatch | failed (the
        active version, as the Library shows it)."""
        job = self.db.execute("SELECT state FROM jobs WHERE kind = 'render' AND track_id = ? "
                              "ORDER BY id DESC LIMIT 1", (track["id"],)).fetchone()
        if job is not None and job["state"] in jobs.OPEN:
            return "rendering"
        v = (self.version(track["active_version_id"])
             if track["active_version_id"] is not None else None)
        if v is None or v["anim_path"] is None:
            return "failed" if job is not None and job["state"] == jobs.FAILED else "none"
        if v["fps"] != display_fps:
            return "fps_mismatch"
        if self.stale_reasons(v, track):
            return "stale"
        return "ready"

    def track_dict(self, track: sqlite3.Row, display_fps: int) -> dict:
        d = dict(track)
        d["grid_override"] = dbm.loads(d["grid_override"])
        d["animation"] = self.animation_status(track, display_fps)
        return d

    # -- removal and storage
    def remove(self, track_id: int, delete_data: bool) -> Path | None:
        """Delete the track's row (its versions, queue entries and jobs go
        with it). Returns the directory to delete, if asked."""
        t = self.track(track_id)
        with self.db:
            self.db.execute("DELETE FROM tracks WHERE id = ?", (track_id,))
        return self.tracks_dir / t["key"] if delete_data else None

    def orphans(self) -> list[Path]:
        """Track directories with no DB row (kept after a removal, or made
        by the analysis CLI)."""
        if not self.tracks_dir.is_dir():
            return []
        keys = {r["key"] for r in self.db.execute("SELECT key FROM tracks")}
        return [d for d in sorted(self.tracks_dir.iterdir()) if d.is_dir() and d.name not in keys]

    def orphan_name(self, d: Path) -> str:
        return TrackDir(d).name if any(d.glob(SOURCE_STEM + ".*")) else d.name


def dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file())


def delete_dir(path: Path):
    shutil.rmtree(path, ignore_errors=False)


def scan(folder: Path) -> list[Path]:
    """Every audio file under a folder."""
    return [f for f in sorted(Path(folder).rglob("*")) if f.is_file() and f.suffix.lower() in AUDIO_EXTS]
