"""File cleanup under `data/tracks/` (docs/architecture.md → Storage
layout, Lifecycle). The server deletes only what the system itself
replaced or left behind:
- animation files that no version row references and that no queued or
  running render will write, unless the engine has them open (it reports
  `released` when it closes one);
- leftover `*.tmp` files, at startup only, after any surviving worker is
  gone (a running worker may still be writing one).

Blocking file I/O: callers run these off the event loop.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from ..analysis.track import ANIM_DIR, TRACKS_DIR


def _rel(data_dir: Path, path: Path) -> str:
    return path.relative_to(data_dir).as_posix()


def unused_anims(data_dir: Path, keep: Iterable[str]) -> list[Path]:
    """Animation files whose data-dir-relative path is not in `keep`."""
    keep = set(keep)
    return [p for p in sorted((Path(data_dir) / TRACKS_DIR).glob(f"*/{ANIM_DIR}/*.anim"))
            if _rel(data_dir, p) not in keep]


def delete_files(paths: Iterable[Path]) -> list[Path]:
    """Delete what can be deleted. A file that is still open fails on
    Windows (PermissionError) and is left for a later sweep."""
    gone = []
    for p in paths:
        try:
            p.unlink()
            gone.append(p)
        except FileNotFoundError:
            pass
        except PermissionError:
            continue
    return gone


def sweep_anims(data_dir: Path, keep: Iterable[str]) -> list[Path]:
    return delete_files(unused_anims(data_dir, keep))


def leftover_tmp(data_dir: Path) -> list[Path]:
    """`*.tmp` files under tracks/: interrupted imports and uploads
    (`tracks/.import-*.tmp`, `.upload-*.tmp`) and interrupted worker
    outputs (`<name>.tmp` next to their target)."""
    root = Path(data_dir) / TRACKS_DIR
    return sorted(p for p in root.rglob("*.tmp") if p.is_file()) if root.is_dir() else []
