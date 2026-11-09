# 0008 — Animation file format

Date: 2026-11-09. Status: accepted; not yet implemented (`volu_gen/animfile/`).
Design context in `../architecture.md` → Storage layout → `animations/`.

## Context
A render job writes one `animations/<render-id>.anim` per render; the
engine plays it. The file must:
- hold the metadata listed in `../architecture.md` (render id, track
  SHA-256, geometry hash, `P`, fps, frame count, generator id + version,
  params, analysis version + tempo override, created time) and
  `frame_count × P × 3` uint8 RGB frames in geometry order;
- give fast random access by frame index (seek, `swap_anim`) without any
  file I/O or decoding on the engine's real-time threads;
- stay a reasonable size at scale: uncompressed, a 4-min track at 120 FPS is
  ~83 MB on the prototype (960 px) and ~1.7 GB at 20k pixels, and renders
  are stored data, not a cache;
- be written sequentially to `<render-id>.anim.tmp` and atomically renamed,
  with cooperative cancel between chunks;
- be versioned from day one (`../../gen_system/AGENTS.md` §6).

## Decision
**A custom single-file container: frames in independently compressed
chunks of `chunk_frames` frames (default 120 = 1 s at 120 FPS), each
optionally temporal-delta filtered and compressed with zstd, with a chunk
index at the end of the file.** Lossless.

### Layout
All integers little-endian.

```
header   8 B  magic   b"VOLUANIM"
         u16  format_version            (1)
         u32  meta_len
meta     meta_len bytes of UTF-8 JSON (below)
chunks   chunk 0 … chunk N-1, back to back
index    N × { u64 offset, u32 comp_len, u8 filter }   (13 B each, packed)
footer   u64  index_offset
         u32  chunk_count (N)
         8 B  magic   b"VOLUEND1"
```

- `N = ceil(frame_count / chunk_frames)`. Chunk `k` holds frames
  `k·chunk_frames …` up to `chunk_frames` of them; the last chunk may be
  shorter. Frame `i` is in chunk `i // chunk_frames`.
- The index is at the end so the writer streams chunks and never seeks
  back. A reader reads the header + meta, then the 20-byte footer, then the
  index (a few KB).

### Metadata (JSON)
| Key | Type | Meaning |
|---|---|---|
| `render_id` | str | ULID/UUID chosen by the server |
| `track_sha256` | str | full SHA-256 of `source.<ext>` |
| `geometry_hash` | str | hash of pixel positions and order |
| `pixel_count` | int | `P` |
| `fps` | int | frame rate; frame `i` shows at `i / fps` s from audio start |
| `frame_count` | int | total frames |
| `chunk_frames` | int | frames per chunk (a frame count, not seconds) |
| `codec` | str | `"zstd"` |
| `generator` | object | `{"id": str, "version": str}` |
| `params` | object | generator parameters |
| `analysis` | object | `{"version": int, "tempo_override": object \| null}` |
| `created` | str | ISO 8601 UTC |

Unknown keys are ignored on read, so adding keys does not need a format
version bump.

### Chunks
- A chunk's decoded payload is `n × P × 3` uint8: frames in order, each
  frame RGB interleaved in geometry order (the layout the preview and
  packetizer use).
- **Filter** (per chunk, from the index), applied before compression:
  - `0` — none.
  - `1` — temporal delta: the chunk's first frame is stored as is; each
    later frame as `frame[j] − frame[j−1]` mod 256 (uint8 wraparound).
    Decode is a cumulative sum over the frame axis in uint8.
  - Other ids are reserved (candidates: near-lossless quantization, a
    channel-planar layout, uncompressed raw for memory-mapping).
- The filtered payload is compressed as **one zstd frame with the content
  checksum on**, so corruption is detected on decode.
- **The encoder picks the filter per chunk** by compressing both and keeping
  the smaller. zstd level is an encoder setting (default 3), not part of
  the format.

### Reading and validation
- Reject: wrong header or footer magic, an unsupported `format_version`,
  `chunk_count ≠ ceil(frame_count / chunk_frames)`, an unknown filter or
  codec, a decoded size `≠ n × P × 3`, or a zstd checksum failure.
- API (`volu_gen/animfile/`): `AnimWriter(path, meta)` (context manager;
  buffers frames into chunks, writes `.tmp`, renames on clean exit, deletes
  `.tmp` on error) and `AnimReader(path)` with `.meta`, `.chunk(k)` →
  `(n, P, 3)` uint8 array, and `.frame(i)`.
- Tests use fixed test vectors: a small known file → exact bytes, and
  round trips of every filter, including a short last chunk.

### Engine use
- The loader opens the file and reads header, meta and index.
- A **prefetch thread** keeps the playhead's chunk and the next one
  decoded in a small ring (≈ 0.35 MB per chunk on the prototype, 7.2 MB at
  20k pixels). The frame thread only takes a reference to a decoded chunk.
  If the chunk is not ready (just after a seek), it repeats the last frame
  and counts a late frame.
- Seek costs one chunk decode (a few ms). For `swap_anim`, the loader
  decodes the playhead's chunk of the new file before the swap.

## Results (synthetic, 2026-11-09)
No generator exists yet, so these come from four synthetic patterns
(`../../tools/anim_codec_bench.py`) at 120 FPS, 1 s chunks, dev PC, one
core. **Re-measure on real generator output** and add the numbers here.

| Pattern | 960 px: raw → delta (zstd 3) | 20k px: raw → delta (zstd 3) | Decode, 20k px, per 1 s chunk (raw / delta) |
|---|---|---|---|
| Smooth moving hue field | 5.4× → 15.2× | 15.1× → 45.2× | 3.1 / 11.5 ms |
| Beat-synced expanding sphere | 4.4× → 5.0× | 7.4× → 7.3× | 5.1 / 14.4 ms |
| Random decaying sparkles (worst case) | 3.2× → 4.0× | 4.0× → 4.4× | 7.1 / 16.4 ms |
| Mostly black + beat strobe | ~5000× | ~20000× | 1.4 / 10.4 ms |

- **Decode cost is negligible:** worst case 16 ms of CPU per second of
  20k-pixel frames (~1.5 % of a core). Engine CPU does not constrain the
  codec choice.
- **zstd vs. LZ4:** LZ4 decodes slightly faster but compresses ~1.5–2×
  worse (sparkle: 2.1× vs 3.2× raw), so it gains nothing here.
- **Delta filter:** ~3× on smooth content, small gains or roughly neutral
  elsewhere; hence per-chunk choice rather than always on.
- **zstd level 9 vs 3:** 10–25 % smaller, 3–5× slower to encode (render
  time only). Default 3; revisit if disk use matters more than render time.
- Expected 4-min track at 20k pixels: **~35–420 MB** instead of 1.7 GB.

## Alternatives considered
- **Raw memory-mapped frames** — simplest and zero-decode, but 1.7 GB per
  track at scale (~170 GB for 100 tracks). Kept as a possible future filter
  id.
- **`.npy` / `.npz`** — `.npz` compresses the whole array with deflate (slow
  decode, no random access); `.npy` is uncompressed.
- **HDF5 / zarr** — chunked and compressed, but heavy dependencies for ~150
  lines of container code. HDF5 has file-locking quirks on Windows; zarr is a
  directory of files, which breaks single-file atomic rename.
- **Video codecs** (H.264/HEVC, lossless FFV1) — lossy modes and chroma
  subsampling bleed colors between voxels; the geometry is not a 2D grid,
  so frames would need an artificial image layout; puts ffmpeg in the
  playback path.
- **Per-frame compression** — no temporal delta, the largest gain on
  smooth content.
- **Near-lossless now** — lossless already gives 4–50× and decode is cheap.
  Can be added later as a filter id if real animations compress badly.

## Consequences
- New dependency: `zstandard` (`compression.zstd` is stdlib only from
  Python 3.14; the backend is pinned to 3.11).
- The engine gains a prefetch thread; real-time threads still do no file
  I/O or decoding.
- Open: whether prefetch decoding competes with the frame thread for the
  GIL. zstd and numpy's cumulative sum should mostly release it; measure
  together with the preview on/off timing test (`0007-ipc-zeromq.md`).
- A container change bumps `format_version`. Files with another version are
  stale (the UI offers a re-render); they are not migrated.
