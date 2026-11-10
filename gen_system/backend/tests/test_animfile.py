import json
import struct
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest
import zstandard as zstd

from volu_gen.animfile import (FILTER_DELTA, FILTER_NONE, FORMAT_VERSION,
                               AnimFormatError, AnimReader, AnimWriter)
from volu_gen.animfile import tmp_path as anim_tmp_path
from volu_gen.animfile.format import FOOTER, HEADER, INDEX_ENTRY, delta_decode, delta_encode


def meta_for(P: int, frame_count: int, fps: int = 120, **extra) -> dict:
    """Valid writer metadata for P pixels and frame_count frames, with a fixed created time."""
    return {"render_id": "01JTEST", "track_sha256": "ab" * 32, "geometry_hash": "g1",
            "pixel_count": P, "fps": fps, "frame_count": frame_count,
            "generator": {"id": "test", "version": 1}, "params": {"speed": 2.5},
            "analysis": {"version": 3, "tempo_override": None},
            "created": "2026-11-09T00:00:00+00:00", **extra}


def smooth(n: int, P: int, seed: int = 0) -> np.ndarray:
    """Slowly changing frames: the delta filter wins."""
    rng = np.random.default_rng(seed)
    base = rng.integers(0, 256, (P, 3))
    return ((base + np.arange(n)[:, None, None]) % 256).astype(np.uint8)


def noise(n: int, P: int, seed: int = 0) -> np.ndarray:
    """Independent random frames, a few repeated: no filter beats the other by much."""
    return np.random.default_rng(seed).integers(0, 256, (n, P, 3), dtype=np.uint8)


def write(path, frames, chunk_frames=None, **meta):
    """Write frames to path with meta_for metadata (extra keys from meta); returns path."""
    with AnimWriter(path, meta_for(frames.shape[1], len(frames), **meta),
                    chunk_frames=chunk_frames) as w:
        w.write(frames)
    return path


def index_of(path):
    """The chunk index of a finished file as (offset, comp_len, filter) tuples."""
    b = path.read_bytes()
    index_offset, n, _ = FOOTER.unpack(b[-FOOTER.size:])
    return [INDEX_ENTRY.unpack_from(b, index_offset + k * INDEX_ENTRY.size) for k in range(n)]


@pytest.mark.parametrize("n", [0, 1, 7, 10, 23, 30])
def test_round_trip_and_short_last_chunk(tmp_path, n):
    """Frames read back exactly by chunk and by frame, for empty, partial and whole last
    chunks; no .tmp remains."""
    frames = smooth(n, 13)
    p = write(tmp_path / "a.anim", frames, chunk_frames=10)
    with AnimReader(p) as r:
        assert len(r) == n and r.chunk_count == -(-n // 10)
        assert r.meta["params"] == {"speed": 2.5} and r.meta["codec"] == "zstd"
        assert r.meta["chunk_frames"] == 10
        if n:
            np.testing.assert_array_equal(np.concatenate([r.chunk(k) for k in range(r.chunk_count)]),
                                          frames)
            assert r.chunk(r.chunk_count - 1).shape == (n - 10 * (r.chunk_count - 1), 13, 3)
        for i in range(n):
            np.testing.assert_array_equal(r.frame(i), frames[i])
        with pytest.raises(IndexError):
            r.frame(n)
        with pytest.raises(IndexError):
            r.chunk(r.chunk_count)
    assert not tmp_path_exists(p)


@pytest.mark.parametrize("fps", [30, 97, 480])
def test_chunks_default_to_one_second(tmp_path, fps):
    """Without chunk_frames, a chunk holds fps frames (1 s at the file's rate)."""
    p = write(tmp_path / "a.anim", smooth(fps + 1, 3), fps=fps)
    with AnimReader(p) as r:
        assert r.meta["chunk_frames"] == fps and r.chunk_count == 2


def tmp_path_exists(p):
    """Whether the writer's <name>.anim.tmp for p exists."""
    return anim_tmp_path(p).exists()


def test_filter_chosen_per_chunk(tmp_path):
    """Chunk 0 changes slowly, so delta wins. Chunk 1 shuffles three noise
    frames: raw repeats exactly while their pairwise differences vary more, so
    raw wins. Both decode."""
    rng = np.random.default_rng(1)
    shuffled = noise(3, 50)[rng.integers(0, 3, 20)]
    frames = np.concatenate([smooth(20, 50), shuffled])
    p = write(tmp_path / "a.anim", frames, chunk_frames=20)
    assert [f for _, _, f in index_of(p)] == [FILTER_DELTA, FILTER_NONE]
    with AnimReader(p) as r:
        np.testing.assert_array_equal(np.concatenate([r.chunk(0), r.chunk(1)]), frames)


def test_delta_wraps_mod_256():
    """Deltas wrap mod 256 and decode back to the original frames."""
    frames = np.array([[[250, 0, 128]], [[4, 255, 0]], [[0, 1, 255]]], np.uint8)
    d = delta_encode(frames)
    np.testing.assert_array_equal(d[1], [[10, 255, 128]])
    np.testing.assert_array_equal(delta_decode(d), frames)


def test_write_in_pieces_and_single_frames(tmp_path):
    """Batches, single (P, 3) frames and empty writes across chunk edges give the same file
    content."""
    frames = noise(25, 4)
    p = tmp_path / "a.anim"
    with AnimWriter(p, meta_for(4, 25), chunk_frames=8) as w:
        w.write(frames[:3])
        w.write(frames[3])        # (P, 3)
        w.write(frames[4:4])      # empty
        w.write(frames[4:25])
        assert w.frames_written == 25
    with AnimReader(p) as r:
        np.testing.assert_array_equal(np.stack([r.frame(i) for i in range(25)]), frames)


def test_exact_bytes(tmp_path):
    """Container bytes built by hand from the spec (decision 0008); chunk
    payloads compressed with the same zstd settings."""
    P, cf = 16, 4
    frames = smooth(7, P)  # random first frame, then +1 per frame: delta clearly wins
    p = write(tmp_path / "a.anim", frames, chunk_frames=cf)

    meta = {**meta_for(P, 7), "codec": "zstd", "chunk_frames": cf}
    meta_b = json.dumps(meta, sort_keys=True, separators=(",", ":")).encode()
    cctx = zstd.ZstdCompressor(level=3, write_checksum=True, write_content_size=True)
    chunks = [cctx.compress(delta_encode(frames[s:s + cf]).tobytes()) for s in (0, cf)]
    head = b"VOLUANIM" + struct.pack("<HI", FORMAT_VERSION, len(meta_b)) + meta_b
    index_offset = len(head) + sum(map(len, chunks))
    index = (struct.pack("<QIB", len(head), len(chunks[0]), FILTER_DELTA)
             + struct.pack("<QIB", len(head) + len(chunks[0]), len(chunks[1]), FILTER_DELTA))
    footer = struct.pack("<QI", index_offset, 2) + b"VOLUEND1"
    assert p.read_bytes() == head + b"".join(chunks) + index + footer
    assert HEADER.size == 14 and INDEX_ENTRY.size == 13 and FOOTER.size == 20


def test_unknown_meta_keys_are_kept(tmp_path):
    """Metadata keys the format does not know survive a round trip."""
    p = write(tmp_path / "a.anim", noise(3, 2), future_key=[1, 2])
    with AnimReader(p) as r:
        assert r.meta["future_key"] == [1, 2]


def test_created_defaults_to_now(tmp_path):
    """The writer fills created with a UTC ISO 8601 time when meta lacks it."""
    m = meta_for(2, 1)
    del m["created"]
    with AnimWriter(tmp_path / "a.anim", m) as w:
        w.write(noise(1, 2))
    with AnimReader(tmp_path / "a.anim") as r:
        assert r.meta["created"].endswith("+00:00")


def test_writer_rejects_bad_input(tmp_path):
    """Missing or invalid meta, wrong dtype or shape, and too many frames raise ValueError; the
    file still finishes after rejected writes."""
    with pytest.raises(ValueError, match="missing"):
        AnimWriter(tmp_path / "a.anim", {"pixel_count": 2})
    with pytest.raises(ValueError, match="pixel_count"):
        AnimWriter(tmp_path / "a.anim", meta_for(0, 1))
    with AnimWriter(tmp_path / "b.anim", meta_for(2, 2)) as w:
        with pytest.raises(ValueError, match="uint8"):
            w.write(np.zeros((1, 2, 3), np.float32))
        with pytest.raises(ValueError, match="uint8"):
            w.write(np.zeros((1, 3, 3), np.uint8))
        with pytest.raises(ValueError, match="more than"):
            w.write(np.zeros((3, 2, 3), np.uint8))
        w.write(np.zeros((2, 2, 3), np.uint8))
    assert (tmp_path / "b.anim").exists()


def test_too_few_frames_leaves_nothing(tmp_path):
    """Closing short of frame_count raises and leaves neither the file nor the .tmp."""
    p = tmp_path / "a.anim"
    with pytest.raises(ValueError, match="wrote 1 of"):
        with AnimWriter(p, meta_for(2, 5)) as w:
            w.write(noise(1, 2))
    assert not p.exists() and not tmp_path_exists(p)


def test_exception_aborts_and_removes_tmp(tmp_path):
    """An exception mid-write (a cancel) deletes the .tmp and creates no file."""
    p = tmp_path / "a.anim"
    with pytest.raises(RuntimeError):
        with AnimWriter(p, meta_for(2, 500), chunk_frames=10) as w:
            w.write(noise(100, 2))
            assert tmp_path_exists(p)
            raise RuntimeError("cancelled")
    assert not p.exists() and not tmp_path_exists(p)


def test_reader_is_thread_safe(tmp_path):
    """Concurrent chunk() calls from several threads each return the right frames."""
    frames = noise(200, 30)
    p = write(tmp_path / "a.anim", frames, chunk_frames=10)
    with AnimReader(p) as r, ThreadPoolExecutor(4) as ex:
        out = list(ex.map(r.chunk, [k % 20 for k in range(200)]))
    for k, c in enumerate(out):
        s = (k % 20) * 10
        np.testing.assert_array_equal(c, frames[s:s + 10])


def test_chunk_is_writable_copy(tmp_path):
    """chunk() returns a writable array, not a read-only view of the decoded bytes."""
    p = write(tmp_path / "a.anim", noise(5, 2))
    with AnimReader(p) as r:
        c = r.chunk(0)
        c[0, 0, 0] ^= 1  # would raise on a read-only frombuffer view


# --- corruption -------------------------------------------------------------

@pytest.fixture
def good(tmp_path):
    """A valid 30-frame file in three chunks, for the corruption tests to damage."""
    return write(tmp_path / "good.anim", smooth(30, 5), chunk_frames=10)


def corrupt(path, offset, data: bytes):
    """Overwrite path's bytes at offset with data."""
    b = bytearray(path.read_bytes())
    b[offset:offset + len(data)] = data
    path.write_bytes(bytes(b))


def test_bad_header_magic(good):
    """A damaged header magic is rejected."""
    corrupt(good, 0, b"X")
    with pytest.raises(AnimFormatError, match="header magic"):
        AnimReader(good)


def test_unsupported_version(good):
    """A newer format_version is rejected."""
    corrupt(good, 8, struct.pack("<H", FORMAT_VERSION + 1))
    with pytest.raises(AnimFormatError, match="format_version"):
        AnimReader(good)


def test_truncated_file(good):
    """A truncated file fails the footer check; one shorter than header + footer fails earlier."""
    good.write_bytes(good.read_bytes()[:-5])
    with pytest.raises(AnimFormatError, match="footer magic"):
        AnimReader(good)
    good.write_bytes(b"VOLUANIM")
    with pytest.raises(AnimFormatError, match="too short"):
        AnimReader(good)


def test_bad_chunk_count(good):
    """A footer chunk_count that disagrees with frame_count / chunk_frames is rejected."""
    size = good.stat().st_size
    corrupt(good, size - FOOTER.size + 8, struct.pack("<I", 2))
    with pytest.raises(AnimFormatError, match="chunk_count"):
        AnimReader(good)


def test_unknown_filter(good):
    """An index entry with an unknown filter id is rejected on open."""
    off = FOOTER.unpack(good.read_bytes()[-FOOTER.size:])[0]
    corrupt(good, off + 12, b"\x07")
    with pytest.raises(AnimFormatError, match="unknown filter 7"):
        AnimReader(good)


def test_unsupported_codec(tmp_path):
    """A codec other than zstd in the metadata is rejected."""
    p = write(tmp_path / "a.anim", noise(2, 2))
    b = p.read_bytes()
    corrupt(p, 0, b[:HEADER.size] + b[HEADER.size:].replace(b'"zstd"', b'"lz4x"', 1))
    with pytest.raises(AnimFormatError, match="codec"):
        AnimReader(p)


def test_corrupt_chunk_fails_checksum(good):
    """A flipped byte inside a chunk fails that chunk's decode only; other chunks still read."""
    off, length, _ = index_of(good)[1]
    b = good.read_bytes()
    corrupt(good, off + length // 2, bytes([b[off + length // 2] ^ 0xFF]))
    with AnimReader(good) as r:
        r.chunk(0)
        with pytest.raises(AnimFormatError, match="chunk 1"):
            r.chunk(1)
