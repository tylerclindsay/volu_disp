# gen_system — AGENTS.md

Host software for the volumetric display: a UI plus a backend that
(1) generates animations from source audio and (2) plays that audio back
while streaming pixel data to the display over Ethernet, in sync.

Read first:
- `../AGENTS.md` — hardware, terminology, geometry (whole-system facts).
- `../docs/requirements.md` — what gen_system must do.
- `../docs/protocol.md` — UDP wire protocol to the drive nodes.
- `../config/displays/` — display configs.

> **Status: pre-architecture.** Treat "Recommended approach" below as
> guidance, not decisions. Once `../docs/architecture.md` is written, it
> overrides this file — update this file to match. Do not scaffold large
> structures or pick frameworks beyond what a task needs until then.

## Layout
- `backend/` — Python package `src/volu_gen/` (not yet scaffolded):
  `geometry/`, `analysis/`, `generators/`, `animfile/`, `output/`,
  `playback/`, `server/`, plus `tests/`.
- `ui/` — Vite + React + TypeScript (not yet scaffolded).
- Runtime data (audio, stems cache, animation files) goes in `../data/`.

---

## Recommended approach

### 1. Separate offline generation from real-time playback
- **Generation** (audio → stems → BPM → animation) is offline/batch: it can be
  slow (demucs especially), and be re-run. Cache stems and analysis per track
  (keyed by audio hash) so re-generating animations doesn't re-run demucs.
  Output is a deterministic, pre-rendered **animation file**: audio
  reference + frame rate + voxel frames + metadata (BPM, generator
  version/params, geometry hash).
- Animations are rendered at the **maximum frame rate** (`timing.max_fps`,
  currently 120). Lower playback FPS settings are derived by
  decimating/resampling at playback, not by regenerating.
- **Playback** only reads an animation file and does two things: play audio
  and send frames. No analysis or rendering in the hot path.
- Live/reactive modes (if wanted later) should still produce frames through
  the same frame → output pipeline.

### 2. The audio clock is the master clock
- Derive the current frame index from the audio device's playback position
  (samples played), not from wall-clock timers or `sleep` loops.
- Add a configurable latency offset (audio output latency vs. network + LED
  latch latency) and a way to calibrate it by eye/ear (e.g. a flash + click
  test pattern).
- Per frame: send all PIXELS packets to all drive nodes, then one broadcast
  SYNC (protocol v2). The sender keeps its own frame sequence counter,
  separate from the animation frame index.

### 3. Keep the UI out of the timing path
- The playback engine runs as its own process/service; the UI is a client
  (HTTP for control, WebSocket for status/preview). A UI freeze, reload, or
  crash must never stall audio or frames.
- The UI shows state the engine reports; it does not compute timing itself.

### 4. Layered data model
Keep these as distinct, testable layers:
1. **Geometry** — reads the display config and produces a 3D position for
   every pixel (including odd-slice offsets). The UI preview and generators
   both use it.
2. **Voxel frames** — one color per pixel, indexed in geometry order.
   Generators treat the display as a point cloud: evaluate a continuous 3D
   field at each pixel's position rather than assume a grid.
3. **Mapping** — pixel → (drive node, strip, pixel index), built from each
   drive node's rectangular `section` in the display config (see
   `../AGENTS.md` → Drive node mapping). Validate on load: no overlaps, full
   coverage, ≤ 64 strips per node. Must match the firmware's strip layout.
4. **Output** — packetizer (mapping + frame → UDP packets) and sender.
Generators never know about nodes, strips, or packets.

### 5. Hardware-free development
- The UI's 3D visualization doubles as a **virtual display**, driven by the
  same frames/geometry.
- Build a **fake drive node** (`../tools/fake_node.py`): a UDP listener that
  decodes the protocol and validates packets.
- Keep `../tools/udp_rainbow_sender.py`-style test patterns (solid colors,
  per-node/per-strip IDs, axis sweeps) for verifying mapping on hardware.

### 6. Single source of truth for shared formats
- The UDP protocol, animation-file format, display config, and mapping schema
  are each defined once (spec + constants module) and covered by tests with
  fixed test vectors (known frame → exact expected bytes).
- Version the animation file and config formats from day one.

### 7. Testing priorities
- Packetizer: byte-exact tests against `../docs/protocol.md` (v2), including
  packets spanning strip boundaries, node buffers larger than 486 pixels,
  and the SYNC packet.
- Geometry/mapping: every physical pixel mapped exactly once; offsets
  produce the expected positions.
- Timing: a headless playback test that measures frame-send jitter and
  drift against the audio clock over a full-length track, at 120 FPS.
- Generators: deterministic output for a given audio + params (snapshot tests).

### 8. Tech stack (tentative — not decided)
- Frontend: React + TypeScript (Vite). 3D preview with react-three-fiber.
- Generation: Python (demucs is Python/PyTorch; numpy, librosa or similar
  for BPM/analysis).
- Playback: start in Python; move the frame sender to a compiled language
  only if measured jitter requires it.
- Dev machine is Windows 11; keep scripts and paths cross-platform.

---

## Working conventions for agents
- Follow the conventions in `../AGENTS.md` (protocol changes, no hardcoded
  geometry/IPs, record decisions).
- Prefer small, runnable increments (e.g. "fake node + packetizer + test"
  before "full UI").
