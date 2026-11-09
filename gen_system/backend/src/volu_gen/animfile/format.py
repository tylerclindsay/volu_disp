"""Animation file (.anim) read/write (decision 0008): a header and JSON
metadata, then frames in independently zstd-compressed chunks (each chunk
optionally temporal-delta filtered), then a chunk index and a footer.
Frames are uint8 (P, 3) RGB in geometry order; frame i shows at i / fps s.
"""

from __future__ import annotations

import json
import os
import struct
import threading
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import zstandard as zstd

FORMAT_VERSION = 1
MAGIC = b"VOLUANIM"
END_MAGIC = b"VOLUEND1"
CODEC = "zstd"
CHUNK_FRAMES = 120  # 1 s at 120 FPS
ZSTD_LEVEL = 3

FILTER_NONE = 0
FILTER_DELTA = 1    # first frame as is, then frame[j] - frame[j-1] mod 256
FILTERS = (FILTER_NONE, FILTER_DELTA)

HEADER = struct.Struct("<8sHI")   # magic, format_version, meta_len
INDEX_ENTRY = struct.Struct("<QIB")  # offset, comp_len, filter (13 B)
FOOTER = struct.Struct("<QI8s")   # index_offset, chunk_count, magic

# Supplied by the caller; the writer adds codec, chunk_frames and (if absent) created.
REQUIRED_META = ("render_id", "track_sha256", "geometry_hash", "pixel_count", "fps",
                 "frame_count", "generator", "params", "analysis")


class AnimFormatError(ValueError):
    """The file is not a valid .anim of a supported version."""


def chunk_count(frame_count: int, chunk_frames: int) -> int:
    """Chunks needed for frame_count frames: ceil(frame_count / chunk_frames)."""
    return -(-frame_count // chunk_frames)


def encode_meta(meta: dict) -> bytes:
    """Metadata as compact UTF-8 JSON with sorted keys, so equal metadata
    always gives equal bytes."""
    return json.dumps(meta, sort_keys=True, separators=(",", ":")).encode()


def delta_encode(frames: np.ndarray) -> np.ndarray:
    """FILTER_DELTA on (n, P, 3) uint8: the first frame as is, each later
    frame minus the one before, mod 256. Returns a new array."""
    d = frames.copy()
    d[1:] = frames[1:] - frames[:-1]  # uint8 wraps mod 256
    return d


def delta_decode(d: np.ndarray) -> np.ndarray:
    """Inverse of delta_encode: cumulative sum over frames, mod 256."""
    return np.cumsum(d, axis=0, dtype=np.uint8)


def tmp_path(path: Path) -> Path:
    """Where AnimWriter writes before the final rename: <name>.anim.tmp."""
    return path.with_name(path.name + ".tmp")


class AnimWriter:
    """Streams frames into a .anim. Writes <path>.tmp and renames it to path
    on a clean close; on an exception (e.g. a cooperative cancel) the .tmp is
    deleted. meta must hold REQUIRED_META; frame_count is fixed up front and
    exactly that many frames must be written.

        with AnimWriter(path, meta) as w:
            for frames in generator:   # (n, P, 3) or (P, 3) uint8
                w.write(frames)
    """

    def __init__(self, path: Path, meta: dict, *, chunk_frames: int = CHUNK_FRAMES,
                 level: int = ZSTD_LEVEL):
        """Validate meta, open <path>.tmp and write the header and metadata.
        chunk_frames is stored in the file; level (zstd) only affects size
        and encode time. Raises ValueError on missing or invalid meta."""
        missing = [k for k in REQUIRED_META if k not in meta]
        if missing:
            raise ValueError(f"meta is missing {missing}")
        self.path = Path(path)
        self.meta = {**meta, "codec": CODEC, "chunk_frames": int(chunk_frames)}
        self.meta.setdefault("created", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        _check_meta(self.meta)
        self.pixel_count = self.meta["pixel_count"]
        self.frame_count = self.meta["frame_count"]
        self.chunk_frames = self.meta["chunk_frames"]
        self._cctx = zstd.ZstdCompressor(level=level, write_checksum=True, write_content_size=True)
        self._buf = np.empty((self.chunk_frames, self.pixel_count, 3), np.uint8)
        self._fill = 0
        self._written = 0
        self._index: list[tuple[int, int, int]] = []
        self._tmp = tmp_path(self.path)
        self._f = open(self._tmp, "wb")
        meta_bytes = encode_meta(self.meta)
        self._f.write(HEADER.pack(MAGIC, FORMAT_VERSION, len(meta_bytes)))
        self._f.write(meta_bytes)

    @property
    def frames_written(self) -> int:
        """Frames accepted so far (including ones still buffered)."""
        return self._written

    def write(self, frames: np.ndarray):
        """Append (n, P, 3) or (P, 3) uint8 frames. Full chunks are compressed
        and written as they fill. Raises ValueError on a wrong dtype or shape,
        or past frame_count."""
        frames = np.asarray(frames)
        if frames.ndim == 2:
            frames = frames[None]
        if frames.dtype != np.uint8 or frames.shape[1:] != (self.pixel_count, 3):
            raise ValueError(f"expected uint8 (n, {self.pixel_count}, 3), got "
                             f"{frames.dtype} {frames.shape}")
        if self._written + len(frames) > self.frame_count:
            raise ValueError(f"more than frame_count={self.frame_count} frames written")
        i = 0
        while i < len(frames):
            n = min(len(frames) - i, self.chunk_frames - self._fill)
            self._buf[self._fill:self._fill + n] = frames[i:i + n]
            self._fill += n
            self._written += n
            i += n
            if self._fill == self.chunk_frames:
                self._flush()

    def _flush(self):
        """Compress the buffered frames as one chunk with whichever filter is
        smaller (raw on a tie), write it and record its index entry."""
        chunk = self._buf[:self._fill]
        raw = self._cctx.compress(chunk.tobytes())
        delta = self._cctx.compress(delta_encode(chunk).tobytes())
        filt, data = (FILTER_DELTA, delta) if len(delta) < len(raw) else (FILTER_NONE, raw)
        self._index.append((self._f.tell(), len(data), filt))
        self._f.write(data)
        self._fill = 0

    def close(self):
        """Flush the last (possibly short) chunk, write the index and footer,
        and rename <path>.tmp to path. Raises ValueError if fewer than
        frame_count frames were written; the .tmp is then left for abort()
        (the context manager does that). No-op once closed."""
        if self._f.closed:
            return
        if self._written != self.frame_count:
            raise ValueError(f"wrote {self._written} of frame_count={self.frame_count} frames")
        if self._fill:
            self._flush()
        index_offset = self._f.tell()
        for entry in self._index:
            self._f.write(INDEX_ENTRY.pack(*entry))
        self._f.write(FOOTER.pack(index_offset, len(self._index), END_MAGIC))
        self._f.close()
        os.replace(self._tmp, self.path)

    def abort(self):
        """Close and delete <path>.tmp; path itself is never touched."""
        self._f.close()
        self._tmp.unlink(missing_ok=True)

    def __enter__(self):
        """Return the writer."""
        return self

    def __exit__(self, exc_type, exc, tb):
        """close() on a clean exit (abort() if that fails); abort() on an
        exception, which then propagates."""
        if exc_type is None:
            try:
                self.close()
            except BaseException:
                self.abort()
                raise
        else:
            self.abort()


class AnimReader:
    """Random access to a .anim by chunk or frame. Validates the container on
    open and each chunk on decode (AnimFormatError). chunk() is safe to call
    from several threads; the engine's prefetch thread uses it, never the
    frame thread. Keeps the file open (and undeletable on Windows) until
    close()."""

    def __init__(self, path: Path):
        """Open path and read and validate the header, metadata, footer and
        index. Raises AnimFormatError (and closes the file) if invalid."""
        self.path = Path(path)
        self._f = open(self.path, "rb")
        self._lock = threading.Lock()
        try:
            self._open()
        except BaseException:
            self._f.close()
            raise

    def _open(self):
        """Parse and validate everything but the chunk payloads; sets meta,
        the shortcut attributes and the chunk index."""
        f = self._f
        size = os.fstat(f.fileno()).st_size
        if size < HEADER.size + FOOTER.size:
            raise AnimFormatError("file too short")
        magic, version, meta_len = HEADER.unpack(f.read(HEADER.size))
        if magic != MAGIC:
            raise AnimFormatError("not a .anim file (bad header magic)")
        if version != FORMAT_VERSION:
            raise AnimFormatError(f"unsupported format_version {version}")
        data_start = HEADER.size + meta_len
        if data_start + FOOTER.size > size:
            raise AnimFormatError("metadata runs past the end of the file")
        try:
            self.meta = json.loads(f.read(meta_len))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise AnimFormatError(f"bad metadata JSON: {e}") from None
        try:
            _check_meta(self.meta)
        except ValueError as e:
            raise AnimFormatError(str(e)) from None
        if self.meta["codec"] != CODEC:
            raise AnimFormatError(f"unsupported codec {self.meta['codec']!r}")
        self.pixel_count = self.meta["pixel_count"]
        self.fps = self.meta["fps"]
        self.frame_count = self.meta["frame_count"]
        self.chunk_frames = self.meta["chunk_frames"]

        f.seek(size - FOOTER.size)
        index_offset, n, end_magic = FOOTER.unpack(f.read(FOOTER.size))
        if end_magic != END_MAGIC:
            raise AnimFormatError("bad footer magic (truncated or unfinished file?)")
        if n != chunk_count(self.frame_count, self.chunk_frames):
            raise AnimFormatError(f"chunk_count {n} does not match frame_count "
                                  f"{self.frame_count} / chunk_frames {self.chunk_frames}")
        if index_offset < data_start or index_offset + n * INDEX_ENTRY.size + FOOTER.size != size:
            raise AnimFormatError("chunk index is not where the footer says")
        f.seek(index_offset)
        raw = f.read(n * INDEX_ENTRY.size)
        self._index = [INDEX_ENTRY.unpack_from(raw, k * INDEX_ENTRY.size) for k in range(n)]
        for k, (off, length, filt) in enumerate(self._index):
            if off < data_start or off + length > index_offset:
                raise AnimFormatError(f"chunk {k} lies outside the data section")
            if filt not in FILTERS:
                raise AnimFormatError(f"chunk {k} has unknown filter {filt}")

    @property
    def chunk_count(self) -> int:
        """Number of chunks in the file."""
        return len(self._index)

    @property
    def duration(self) -> float:
        """Length of the animation in seconds (frame_count / fps)."""
        return self.frame_count / self.fps

    def __len__(self) -> int:
        """frame_count."""
        return self.frame_count

    def chunk_len(self, k: int) -> int:
        """Frames in chunk k (the last chunk may be short)."""
        return min(self.chunk_frames, self.frame_count - k * self.chunk_frames)

    def chunk(self, k: int) -> np.ndarray:
        """Decode chunk k: frames k·chunk_frames … as a new, writable
        (n, P, 3) uint8 array (n = chunk_len(k)). Raises IndexError out of
        range, AnimFormatError on a checksum or size mismatch."""
        if not 0 <= k < len(self._index):
            raise IndexError(f"chunk {k} out of range (0..{len(self._index) - 1})")
        off, length, filt = self._index[k]
        with self._lock:
            self._f.seek(off)
            data = self._f.read(length)
        n = self.chunk_len(k)
        expected = n * self.pixel_count * 3
        try:
            raw = zstd.ZstdDecompressor().decompress(data, max_output_size=expected)
        except zstd.ZstdError as e:
            raise AnimFormatError(f"chunk {k}: {e}") from None
        if len(raw) != expected:
            raise AnimFormatError(f"chunk {k}: decoded {len(raw)} bytes, expected {expected}")
        frames = np.frombuffer(raw, np.uint8).reshape(n, self.pixel_count, 3)
        return delta_decode(frames) if filt == FILTER_DELTA else frames.copy()

    def frame(self, i: int) -> np.ndarray:
        """One (P, 3) frame. Decodes its whole chunk: for playback, cache
        chunk() results instead."""
        if not 0 <= i < self.frame_count:
            raise IndexError(f"frame {i} out of range (0..{self.frame_count - 1})")
        k, j = divmod(i, self.chunk_frames)
        return self.chunk(k)[j]

    def close(self):
        """Close the file (it can then be deleted, including on Windows)."""
        self._f.close()

    def __enter__(self):
        """Return the reader."""
        return self

    def __exit__(self, *exc):
        """close()."""
        self.close()


def _check_meta(meta: dict):
    """Check the fields the container itself depends on: pixel_count, fps
    and chunk_frames positive ints, frame_count a non-negative int, codec
    present. Raises ValueError."""
    for k in ("pixel_count", "fps", "chunk_frames"):
        v = meta.get(k)
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            raise ValueError(f"meta {k} must be a positive int, got {v!r}")
    fc = meta.get("frame_count")
    if not isinstance(fc, int) or isinstance(fc, bool) or fc < 0:
        raise ValueError(f"meta frame_count must be a non-negative int, got {fc!r}")
    if "codec" not in meta:
        raise ValueError("meta is missing codec")
