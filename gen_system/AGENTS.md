# gen_system — AGENTS.md

Host software for the volumetric display: a UI plus a backend that
(1) generates animations from source audio and (2) plays that audio back
while streaming pixel data to the display over Ethernet, in sync.

Read first:
- `../AGENTS.md` and `../docs/hardware.md` — terminology, hardware,
  geometry, drive node mapping (whole-system facts).
- `../docs/architecture.md` — processes, IPC, flows, storage.
- `../docs/generators.md` — the director, plans, and methods.
- `../docs/ui.md` — the UI.
- `../docs/protocol.md` — UDP wire protocol to the drive nodes.
- `../config/displays/` — display configs.

> **Status: architecture in draft.** `../docs/architecture.md` is the
> working architecture (process model, IPC, flows, package layout) and
> overrides "Recommended approach" below wherever they differ. It is still
> being refined — check its open questions before building on an
> undecided part, and update it when the design moves.

## Layout
- `backend/` — Python package `src/volu_gen/` (uv project; see its
  README): `analysis/`, `animfile/`, `geometry/`, `mapping/` and
  `output/` (implemented), `generators/` (inputs, method interface, plan
  model, director, render job and the first four methods built), then
  `engine/`, `worker/`, `server/`, `ipc/`, plus `tests/` (see `../docs/architecture.md` → package layout).
- `ui/` — Vite + React + TypeScript (not yet scaffolded).
- Runtime data (audio, per-track data dirs with stems, analysis and
  animation files) goes in `../data/` (`../docs/architecture.md` → storage).

---

## Recommended approach

### 1. Separate offline generation from real-time playback
- **Generation** (audio → stems → analysis → animation) is offline/batch: it
  can be slow (demucs especially), and be re-run. Store stems and analysis
  per track (keyed by audio hash) so re-generating animations doesn't re-run
  demucs.
  Output is a deterministic, pre-rendered **animation file**: voxel
  frames + metadata (track SHA-256, frame rate, generator version, track
  params, plan, geometry hash; `../docs/decisions/0008-animation-file-format.md`).
  The audio is not in the file; playback reads the track's own source.
- Animations are rendered at the **display FPS**: a UI setting, any whole
  number from 30 to 480, default 120
  (`../docs/decisions/0010-display-fps.md`). Never assume 120. A render
  plays only at its own FPS: playback never drops, holds, or resamples
  frames, so after an FPS change every render at the old rate is
  unplayable until re-rendered.
- **Playback** only reads an animation file and does two things: play audio
  and send frames. No analysis or rendering in the hot path.
- Live/reactive modes (if wanted later) should still produce frames through
  the same frame → output pipeline.

### 1a. Audio analysis (`analysis/`)
Decided — see `../docs/decisions/0004-audio-analysis.md` for the pipeline,
the reasons, and the `analysis.json` outline. Waveform peaks for the UI's
stem lanes: `../docs/decisions/0006-waveform-peaks.md` (`peaks.npz` per
track, not in analysis.json). Key rules:
- Port from `../tools/analysis_spike/spike.py`; it is the reference
  implementation. Re-check any change by ear (click track + Audacity labels,
  as the spike writes them) on the spike's tracks before trusting it.
- One model on the GPU at a time (load, run, unload); stay under 8 GB VRAM.
  SongFormer runs in 150 s windows with 60 s overlap — never whole-track.
- Beat grid: one constant tempo and one continuous grid per track, snapped
  to a whole BPM when within 0.1. No phase jumps. Don't "improve" this with
  local tempo tracking.
- SongFormer gives boundaries only; don't use its labels. Phrases are
  8 bars from each section start and each drop; drops are found per bar
  (`../docs/decisions/0005-per-bar-drops.md`); drums/bass in/out are
  events, not cuts.
- Store raw CLAP cosines per track; z-score mood words across the library.
- A user BPM / grid-offset override replaces the fitted grid and re-runs only
  the grid-dependent steps (phrases, per-bar energy), not the models.
- **Tune analysis for the common case.** Don't add rules, checks or
  thresholds for a minority of tracks (variable tempo, a grid that scores
  just under the cutoff, odd drops); the Studio BPM / grid-offset override
  covers those. The grid accept/reject check stays as is (0004 → Known gaps).
- **Pin `transformers==4.51.1`.** SongFormer's remote code breaks on
  transformers 5.x. Python 3.11, torch 2.11 cu128.
- Stem features (`features.npz`, `../docs/decisions/0012-stem-features.md`):
  a CPU stage after `peaks`, at fixed rates. It does not re-run on a grid
  override or a display FPS change. Onsets are stored for `kick` and `hat`
  only, from the ADTOF drum transcription model on the drums stem, not
  frequency bands (`../docs/decisions/0016-drum-onsets-adtof.md`); the
  other classes weren't reliable (`../docs/decisions/0017-kick-and-hat-onsets-only.md`).
  Check any onset change by
  ear with the `--check` click tracks (`onsets_<class>.wav`); counts per
  beat can look right while the hits are wrong.

### 1b. Generators (`generators/`)
Design: `../docs/generators.md`. Key rules:
- Two steps: the director makes a **plan** (segments, methods, params) and
  the render job renders exactly that plan. Planning runs in the API server
  (`../docs/decisions/0011-planning-in-server.md`).
- The server imports `generators/`, so **no heavy imports (torch) at module
  level**.
- Methods carry **no state frame to frame** and must render any frame range
  of their segment. They work in seconds, never assume a frame rate, and
  return float32 linear RGB.
- The render job blends in linear light and writes linear PWM values (no
  gamma curve); it refuses stale plans rather than re-planning
  (`../docs/decisions/0018-render-output.md`).
- Bump a method's `version` whenever its output changes for the same params.
- Randomness: a per-segment RNG seeded from `(seed, segment start bar)`.
- Smoothness first: soft edges about one pixel pitch wide, eased param
  changes, kicks as a fast attack with a smooth decay.

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
- The playback engine runs as its own process; the UI is a client of the
  API server only (HTTP for control, WebSocket for status/preview), which
  relays to the engine. A UI freeze, reload, or crash must never stall
  audio or frames.
- The UI shows state the engine reports (via the server); it does not
  compute timing itself.

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
   `../docs/hardware.md` → Drive node mapping). Validate on load: no
   overlaps, full coverage, ≤ 64 strips per node, longest strip fits the
   display FPS. Must match the firmware's strip layout.
4. **Output** — packetizer (mapping + frame → UDP packets) and sender.
Generators never know about nodes, strips, or packets.

### 5. Hardware-free development
- The UI's 3D visualization doubles as a **virtual display**, driven by the
  same frames/geometry.
- **Fake drive nodes** (`../tools/fake_node.py`, built on
  `volu_gen.output.NodeSim`): UDP listeners that decode the protocol,
  apply the node's receive rules, and report bad, late or incomplete
  frames. Drive them with `python -m volu_gen.output <config> --loopback`.
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
  drift against the audio clock over a full-length track, at 30, 120, and
  480 FPS and an odd rate in between (e.g. 97).
- Generators: the same analysis, features, track params, edits, and seed
  give the same plan; pinned segments survive a re-plan; snapshot tests of
  a render from a fixed plan.

### 8. Tech stack (partly decided; see `../docs/architecture.md`)
- Frontend: decided — see `../docs/ui.md` (Vite + React + TS, Mantine,
  react-three-fiber, TanStack Query, Zustand).
- Generation: Python 3.11 + PyTorch. Analysis models are decided (see 1a);
  `transformers` pinned to 4.51.1.
- IPC: ZeroMQ (`../docs/decisions/0007-ipc-zeromq.md`). Audio output:
  WASAPI shared by default (`../docs/decisions/0009-audio-output-api.md`).
- Playback: start in Python; move the frame sender to a compiled language
  only if measured jitter requires it.
- Dev machine is Windows 11; keep scripts and paths cross-platform.

---

## Working conventions for agents
- Follow the conventions in `../AGENTS.md` (protocol changes, no hardcoded
  geometry/IPs, record decisions).
- Prefer small, runnable increments (e.g. "fake node + packetizer + test"
  before "full UI").
