# Architecture (gen_system)

Status: **working draft.** This is the current state of the host software
architecture. It is expected to change as it is refined and during
development: update it in place when the design moves, and record settled,
non-obvious choices in `decisions/`. It overrides the "Recommended approach"
section of `gen_system/AGENTS.md` wherever the two differ.

Open questions are collected at the end.

Related: `requirements.md`, `ui.md`, `generators.md`, `protocol.md`,
`decisions/0004-audio-analysis.md`, `decisions/0006-waveform-peaks.md`,
`decisions/0007-ipc-zeromq.md`, `decisions/0008-animation-file-format.md`,
`decisions/0009-audio-output-api.md`, `decisions/0010-display-fps.md`,
`decisions/0011-planning-in-server.md`, `decisions/0012-stem-features.md`,
`decisions/0016-drum-onsets-adtof.md`.

---

## Principles
- **Offline generation, dumb playback.** Audio → stems → analysis →
  animation file happens ahead of time and can be slow. Playback only reads
  files: no analysis or rendering in the hot path.
- **The audio clock is the master clock.** The frame index is derived from
  the audio device's playback position, never from wall-clock timers.
- **The UI is a remote control.** It sends commands and draws what the
  backend reports. A UI freeze, reload, or crash never affects audio or
  frames.
- **One writer per store.** Every file or database has exactly one process
  that writes it. (The server also deletes files under `tracks/` for
  cleanup; it never writes them after import.)
- **Real-time threads never block on anything** — no IPC, no file I/O, no
  waiting on other processes.

## Processes

```mermaid
flowchart LR
    UI["UI (browser)"]
    subgraph backend ["Backend processes"]
        SRV["API server<br/>SQLite · queues · settings · planning<br/>supervises engine + worker"]
        ENG["Engine<br/>audio out · frame clock · UDP"]
        WRK["Worker<br/>stems · analysis · render<br/>one job at a time"]
    end
    DB[("data/volu.db")]
    DATA[("data/tracks/")]
    NODES["Drive nodes"]

    UI <-->|"HTTP: control, queries"| SRV
    SRV -->|"WebSocket: status, jobs, binary preview"| UI
    SRV <-->|"ZeroMQ control"| ENG
    ENG -->|"ZeroMQ preview (latest-only)"| SRV
    SRV <-->|"ZeroMQ jobs"| WRK
    SRV -->|"writes"| DB
    WRK -->|"writes (tmp + atomic rename)"| DATA
    DATA -->|"reads: audio, anim files"| ENG
    DATA -.->|"reads: analysis, features, peaks"| SRV
    SRV -.->|"import; cleanup deletes"| DATA
    ENG -->|"UDP PIXELS + SYNC"| NODES
    SRV -.->|"ping (node detection)"| NODES
```

| Process | Role | Writes | Never does |
|---|---|---|---|
| API server | Coordinator; the only holder of persistent state; plans renders | SQLite; new track dirs on import; deletes unreferenced animation files, leftover `*.tmp` files, and orphaned track dirs (on request) | Timing, model inference, rendering frames |
| Engine | Plays audio, sends frames | nothing | DB access, HTTP, models |
| Worker | Runs GPU/batch jobs | `data/tracks/` (everything after import) | DB access, playback |

The UI talks only to the API server.

### API server
- FastAPI + uvicorn, one asyncio loop. Entry point (`volu serve`); spawns
  the engine and the worker, and respawns them if they die.
- **Startup finds running children first.** Before spawning, the server
  sends `hello` to each child's port with a short timeout; `hello`
  replies carry the child's PID.
  - **Engine answers:** the server adopts it (it may be playing out
    after a server crash). With no process handle, crashes of an adopted
    engine are caught by heartbeats only; it is respawned if they stop.
  - **Worker answers:** the server kills it by PID and spawns a fresh
    one, then re-queues the job it was running. Kill stays the only
    recovery path for jobs.
  - The startup `*.tmp` cleanup runs only once the old worker is gone, so
    it never deletes a file still being written.
- **SQLite** holds: tracks (key, name, original path, duration, analysis
  status, BPM, tempo mode), versions (draft/saved, track params, plan,
  generator version, fps, format version, geometry hash, file path),
  presets, settings, the play queue, and the job queue. The server is its
  only writer.
- **Imports tracks** (see Storage layout → Import): copies uploaded or
  scanned audio into a new track directory, then hands it to the worker.
- Loads and validates the active display config (no overlaps, full
  coverage, ≤ 64 strips per node, longest strip fits the display FPS) and
  computes the **geometry hash**. It
  passes the config path and hash to the engine and worker, so all three
  agree on geometry.
- **Plans renders** (`generators.md` → Plan, `decisions/0011-planning-in-server.md`):
  runs the generator's director in-process to turn `analysis.json`,
  `features.npz`, track params, and the user's pinned instance edits into a
  plan. Planning is pure Python with no models (milliseconds), so it runs
  on demand: on every track-param or instance edit, after a grid
  override, and before a render whose version has no plan yet (the
  default render on import) or whose plan was made by another generator
  or method version (the new plan overwrites the stored one). Otherwise a
  render uses the stored plan unchanged, e.g. an FPS re-render (Main flows
  6), except that before every render the server recomputes each
  segment's `start` / `end` from its bars on the current grid. It runs off the event loop (`asyncio.to_thread`) and is
  deterministic. The worker only renders the plan it is given.
- **Node detection:** pings each node's configured IP and pushes the online
  set to the engine. Moves into the engine once protocol type 3 (status)
  exists.
- **UI API:** HTTP for library, analysis, peaks, settings, versions,
  presets, generator and method parameter schemas, plans (get the plan;
  change track params or an instance's overrides and get the re-plan back
  in the response); one WebSocket for live state
  (engine status, job progress, node status, binary preview frames).
  Preview frames are latest-only: a slow browser drops frames, never queues
  them.
- **Engine state is a function of server state.** On every engine
  (re)connect the server sends a full `configure`; the engine keeps no
  persistent state of its own.
- Owns the **play queue** and **job queue** and their policies (priority,
  preloading the next track, cleanup of unreferenced animation files).
- Owns the **engine mode** (`ui.md` → Engine modes): **Playback** plays
  the queue, **Studio** the track open in Studio (Main flows 7). The
  engine doesn't know the mode; it only gets the matching `load`s.

### Engine
The real-time process, kept as small as possible. It receives file paths
and commands, plays audio, and sends frames. Raised process priority; 1 ms
timer resolution on Windows.

**Threads**

| Thread | Does | Never does |
|---|---|---|
| Audio callback (PortAudio via `sounddevice`) | Copies samples from the preloaded buffer; records `(dac_time, sample_index)` per buffer, where `sample_index` is one output counter that runs on across tracks; switches current → next at the exact sample (gapless) and records the counter value where `next` starts | I/O, allocation, long locks |
| Frame | Each frame: read clock → pick frame index → take it from a decoded chunk → packetize → PIXELS to every online node (when armed) → one SYNC broadcast; write the preview frame (picked with `preview_offset`) to the preview slot | IPC, file loading |
| Loader | Decodes audio and opens animation files into the `current` / `next` slots; for `swap_anim`, decodes the new file's playhead chunk before the swap (decision 0008) | Anything timed |
| Prefetch | Keeps the playhead's animation chunk and the next one decoded (decision 0008); after a seek, decodes the new playhead's chunk first | Anything timed |
| Control | IPC with the server; publishes status (~20 Hz) and preview (preview FPS setting, ≤ display FPS) from shared slots | Block the real-time threads |

Real-time threads share state with the control thread only through small
lock-guarded slots or atomic reference swaps.

**Clock.** The frame thread reads `stream.time`, the same time base as
PortAudio's DAC timestamps:

```
heard_pos(now) = sample_index + (now − dac_time) × sr      # sample at the speaker now
frame          = floor((heard_pos / sr + display_offset) × fps)
```

`sr` is the output stream's sample rate: `sample_index` counts samples
sent to the device, after any resampling to the device rate (decision
0009), not samples of the source file.

**Track boundaries.** The counter never resets, so one target time
(`heard_pos / sr + offset`) covers a gapless track change. Each slot
records the counter value where its audio starts; the frame thread takes
the frame from whichever slot's range holds the target time, at its
time from that slot's start. With a positive offset the lights reach the
next track's first frames before its audio does; with a negative offset
the previous track's last frames still show after its audio has ended.
The preview does the same with `preview_offset`. Before the first track
starts, or past the end of the last, the frame is black.

The frame thread wakes at the **display FPS** (a UI setting: any whole
number from 30 to 480; see Settled → Display FPS). A loaded animation is
always at that rate: the engine refuses to `load` or `swap_anim` a file
whose fps differs, and on a `configure` with a new display FPS it stops
and unloads any animation at the old rate.

- `display_offset` is the **A/V sync offset**, a user setting in ms
  (positive = lights earlier). It covers audio output latency, network and
  latch time, and anything else that misaligns sound and light. Set by the
  calibration flash + click, and adjustable by hand from the UI at any time
  (`ui.md` → Sync).
  - **Applied live:** a change goes out in `configure` and takes effect at
    the next frame, with no reload or playback interruption. The jump is at
    most the change itself; nudges are small.
  - **Stored per audio device + output mode** (e.g. WASAPI shared vs.
    exclusive), since their latency differs; switching device switches
    offset.
  - Resolution is finer than a frame (33 ms at 30 FPS, 8.3 ms at 120,
    2.1 ms at 480): the offset shifts which frame is picked, so small
    nudges take effect at frame boundaries.
- `preview_offset` is the same idea for the UI preview, which has its own
  path (ZeroMQ → WebSocket → browser → monitor). The frame thread picks the
  preview frame separately at `heard_pos / sr + preview_offset`, so
  the hardware offset never makes the preview run early or late.
- Frames are never dropped, held, or resampled to fit another rate: a file
  at another fps is not playable until re-rendered (Main flows 6). The one
  hold is a late frame: if the playhead's chunk isn't decoded yet (just
  after a seek), the frame thread repeats the last frame and counts it in
  `status` (decision 0008).
- At high rates the 1 ms timer resolution is a large share of the frame
  (2.08 ms at 480 FPS). Measure wake jitter there; use a high-resolution
  waitable timer or spin the last part of the wait if needed.
- The protocol sender's frame counter is separate from the frame index and
  keeps incrementing across seeks, track changes, and test patterns.
- Smooth the clock (e.g. a filter over the anchors) only if measured jitter
  calls for it.

**Frame sources.** `AnimationSource`, `TestPatternSource`,
`CalibrationSource` (flash + click mixed into the audio). Each maps a frame
index → `colors[P, 3]` in geometry order, so test patterns and calibration
use the same output path as playback.

**Output.** Mapping + packetizer → UDP sender (`protocol.md`, v2). When
output is disarmed, frames are still produced and previewed but not sent.
Armed output goes only to nodes in the online set. Global brightness and a
power cap are applied here, adjustable live like the sync offset
(`generators.md` → Output and render job).

**Slots.** `current` and `next` each hold decoded audio + an open animation
file. `next` is preloaded so a track change is gapless. After a change the
old `current` becomes `previous` and keeps its animation open until both
target times (display and preview) are past its end; only then does the
engine close it and send `released`. `swap_anim` replaces
the animation in `current` at the first frame after the loader has decoded
the new file's playhead chunk, keeping audio and playhead.

### Worker
- One process, one job at a time, below-normal priority. Respects the GPU
  rules in `decisions/0004-audio-analysis.md` (one model loaded at a time,
  < 8 GB VRAM).
- **Jobs are pure:** a spec in, files plus a result message out. No DB.
- All outputs are written to `<name>.tmp` (`.anim.tmp`, `peaks.npz.tmp`,
  …) and atomically renamed, so
  **killing the worker is always safe**. Kill + respawn is the cancel
  mechanism for model stages; render jobs may also cancel cooperatively
  between chunks.

| Job | Spec | Result |
|---|---|---|
| `analyze` | one track, stages, force set | key, BPM/grid summary, status |
| `regrid` | key, BPM / grid-offset override | updated phrases, events, bars (CPU, seconds) |
| `render` | key, generator id + track params, plan (made by the server, `generators.md`), fps (the display FPS when the job starts), geometry hash, output path | anim path, frame count, generator version |

**Tracks are processed in series.** A multi-file upload or folder scan
queues one `analyze` job per track, and each track gets its default
`render` before the next track's `analyze` starts, so the first tracks
become playable while the rest wait. There is no second worker and no
parallel processing. Cost: each track loads and unloads every model itself
instead of sharing one load across a batch (seconds per stage, small next
to demucs).

**Priority:** Studio render and `regrid` > render for a queued track >
import work (`analyze` + its default render, FIFO; also "Re-render stale"
and manual FPS re-renders). A track's default render goes to the front of
import work when its `analyze` finishes, so it runs before the next
track's `analyze`. Queueing a track whose import isn't finished moves its
remaining import work (`analyze` and its default render) up to "render
for a queued track". Jobs are never preempted mid-run: a
higher-priority job starts when the current job finishes, which is at most
one track's analysis or one render (minutes per track on the large
display, decision 0010).

## IPC
ZeroMQ (`pyzmq`) over `tcp://127.0.0.1`; full rationale in
`decisions/0007-ipc-zeromq.md`.

| Link | Sockets |
|---|---|
| Server ↔ engine control | engine binds `ROUTER`, server connects `DEALER` |
| Engine → server preview | engine binds `PUB`, server connects `SUB` (`CONFLATE`: latest-only; the engine never blocks) |
| Server ↔ worker jobs | worker binds `ROUTER`, server connects `DEALER` |

- **The child binds, the server connects**, so a restarted server can
  reconnect to a running engine; ZeroMQ reconnects automatically.
- **Messages:** pydantic models in one shared module
  (`volu_gen/ipc/messages.py`), encoded with MessagePack (no pickle).
  Preview frames are a small fixed header + raw RGB bytes on their own
  socket, so bulk data never delays control messages.
- **Connection lifecycle** (ZeroMQ hides connections and would otherwise
  queue messages for an absent peer):
  - `ZMQ_IMMEDIATE` on the server's sockets: nothing is queued while a
    child is down; a failed send means "peer down".
  - `hello` handshake on every (re)connect, then the server sends the full
    `configure` (engine) or re-submits the job (worker).
  - Liveness: process handles for crashes, ZeroMQ heartbeats for hangs.
- Ports are fixed, configurable defaults passed to the children at spawn.
- The engine's control thread owns its sockets (they are not thread-safe);
  libzmq does the I/O in its own C thread, outside the GIL.

**Server → engine**

| Message | Fields / meaning |
|---|---|
| `hello` | sent on connect; the engine replies `hello` |
| `configure` | display config path, geometry hash, audio device + output mode (shared/exclusive), display FPS, preview FPS, display offset (ms), preview offset (ms), network interface, armed, online nodes, brightness + power cap. Full snapshot; sent on connect and on any change |
| `load` | slot (`current`/`next`), track id, audio path, anim path, start time |
| `swap_anim` | anim path; replace current animation at the playhead |
| `play` / `pause` / `stop` / `seek{t}` | transport |
| `clear_next` | drop the preloaded next track |
| `test{pattern, params}` / `test_off` | test patterns, calibration |
| `shutdown` | |

**Engine → server**

| Message | Fields / meaning |
|---|---|
| `hello` | reply to the server's `hello`: engine instance id, PID, current state (loaded slots, position) |
| `status` (~20 Hz; also the heartbeat) | state, track id, position (s), frame, actual fps, late frames, audio xruns |
| `preview` | binary RGB frame, geometry order (at the preview FPS setting, latest-only) |
| `track_started` / `track_ended` | track id |
| `released` | anim path no longer open (safe to delete) |
| `error` | kind, message |

**Server → worker:** `hello`, `submit{job_id, kind, spec}`,
`cancel{job_id}` (render jobs only, honored between chunks). Any other
cancel = kill and respawn; the new worker reconnects and says `hello`.

**Worker → server:** `hello{pid, job_id}`, `progress{job_id, stage, track, frac, eta}`,
`done{job_id, result}`, `failed{job_id, error, traceback}`, `log{line}`.

## Main flows
1. **Add track.** `POST /tracks` (file upload) or `POST /tracks/scan`
   (folder path) → server imports each file into `tracks/<key>/` (a file
   already in the library just returns its track; one whose directory
   exists without a DB row, e.g. kept after removal, gets a new row and
   goes through the same steps, which skip finished stages) → inserts the row, queues
   `analyze` → worker `progress` → UI jobs drawer → `done` → server updates the track,
   plans it with the default track params, and queues `render` with that
   plan (creating the track's draft row) → `done` → server marks the
   draft rendered and sets it active → the next imported track's `analyze` starts (see
   Worker: tracks are processed in series).
2. **Play the queue.** Server resolves each track's active version →
   `load(current)`, `load(next)`, `play` → at the end of the track the
   engine switches to `next` at the sample and emits `track_ended` /
   `track_started` → server pops the queue and sends the new `load(next)`.
   **Unrendered tracks are skipped**, never waited for: `next` is the first
   queued track whose active version is playable (Lifecycle: rendered at
   the display FPS, matching pixel count, readable format version). Adding
   a track to the queue whose active version is at another fps queues
   its FPS re-render at "render for a queued track" priority (decision
   0010). Skipped tracks keep their place
   at the front of the queue and keep rendering; if one becomes ready
   before the current track ends, the server replaces `next` with it
   (`clear_next` + `load(next)`).
3. **Studio edit + re-render.** A track-param or instance edit → server
   re-plans at once, keeping pinned segments, and returns the plan (the UI
   redraws the segment lane; the draft is marked out of date) → **Render**
   → `render` (top priority) with that plan writes
   `tracks/<key>/animations/<render-id>.anim` → server sends `swap_anim` → engine
   switches at the next frame and reports `released{old}` → server deletes
   the old draft file, unless a saved version now uses it (Lifecycle). **Every render gets a new filename**: Windows cannot
   replace or delete a file that is open or memory-mapped.
4. **Grid override.** `regrid` job → server re-plans the draft (segments
   are anchored by bar, so pinned ones keep their place) and notifies the
   UI that analysis and plan changed (it refetches) → the user re-renders
   when ready. Saved versions keep their plans; every version rendered on
   the old grid is now stale (Lifecycle), and re-rendering one moves its
   segments onto the new grid without re-planning.
5. **Seek / pause / test patterns / calibration.** UI → server → one
   engine command each; the UI reflects only what `status` reports.
6. **Display FPS change** (Settings, `decisions/0010-display-fps.md`).
   The UI confirms first, showing how many versions become unplayable →
   server sends `stop`, stores the rate, and sends `configure` → the
   engine wakes at the new rate (test patterns and calibration keep
   working; no animation is loaded). Every version whose fps differs is
   now unplayable. The server cancels and re-queues a running render, and
   queues an **FPS re-render** of the active version of every track in the
   play queue, at "render for a queued track" priority. Other tracks are
   re-rendered manually: a Library row action, a Library bulk
   "Re-render FPS mismatch", or a button on the active version in Studio's
   version bar; these run like import work. An FPS re-render covers only
   the active version. It uses the version's stored track params and plan
   unchanged (segment times are recomputed from bars on the current grid,
   as for every render; re-planned first only if the generator or a method
   version changed; API server above) and **replaces the version's file in place**:
   same version row, new render id; the old file is deleted as in
   Lifecycle below.
7. **Enter / leave Studio** (`ui.md` → Engine modes). Entering: server
   pauses, sends `clear_next`, and `load(current)` of the Studio track's
   selected version, paused at the start. Studio doesn't keep a playhead
   per track: opening a track, or switching to another, starts at 0.
   Leaving: server reloads the queue's current track at the position it
   was left at, **paused**, plus its `next`; the user presses play.

## Failure handling

| Crash | Effect |
|---|---|
| UI | None. |
| Server | Playback continues: the engine plays out current + next, then idles. Worker exits once the server has been gone past the heartbeat timeout. A restarted server adopts the running engine (API server → Startup), re-sends `configure`, reconciles the queue from the engine's `hello` state, kills any surviving worker, and re-queues jobs that were running. |
| Worker (e.g. OOM) | Job marked failed; worker respawned; queue continues. Atomic renames mean no partial files. |
| Engine | Server respawns it, re-sends `configure`, reloads the current track **paused** at the last reported position, and the preloaded `next`. |

## Data layers
Distinct, separately testable layers (from `gen_system/AGENTS.md`):
1. **Geometry** — display config → a 3D position for every pixel (with
   odd-slice offsets). Used by generators, the UI preview, and the mapping.
2. **Voxel frames** — one RGB color per pixel, in geometry order
   (strip by strip, top to bottom: `i = (z * X + x) * Y + y`, decision 0013).
   Generators evaluate fields at pixel positions; they never assume a grid
   and never know about nodes, strips, or packets.
3. **Mapping** — pixel → (drive node, strip, pixel index) from each node's
   `section`; validated on load.
4. **Output** — packetizer (mapping + frame → UDP packets) and sender.

## Storage layout
`data/` is gitignored (`data/README.md`).

| Path under `data/` | Contents | Writer |
|---|---|---|
| `volu.db` | SQLite: tracks, versions, presets, settings, queues | server |
| `tracks/<key>/` | Per-track data: source audio, stems, analysis, peaks, features, animations (below) | server creates it on import; worker after that (the server only deletes) |

**`<key>`** is the first 16 hex digits of the SHA-256 of the imported
file's bytes. Importing the same file again (under any name or path) finds
the existing track; a re-encoded or edited file is a new track. The DB maps
track id ↔ key.

**Import copies the audio in.** There is no library folder that the system
reads audio from: every track's audio lives in its own directory as
`source.<ext>`. Reasons:
- Browser uploads have no path to reference, only bytes.
- Tag edits or moves of the original change nothing: the key is the hash of
  the copy taken at import.
- Tracks keep playing and rendering when the original's drive is missing.
- A track directory is self-contained, so the engine and worker read only
  `data/`, and backing up the library is copying one folder.

The cost is one extra copy of each source file (FLAC ≈ 17–72 MB per track
in the current library), small next to the stems and animations.

**This is stored data, not a cache.** It holds user input that cannot be
regenerated (the grid override), results that took minutes of GPU per
track and may not reproduce exactly on newer models, and the renders the
user approved. The server deletes only what the system itself replaced or
left behind: superseded draft files, a version's old file after an FPS
re-render, animation files no DB row references, and leftover `*.tmp`
files (Lifecycle). Everything else under `tracks/` is deleted only on
request (removing a track, orphaned track directories).

How the files in a track directory derive from each other:

```mermaid
flowchart LR
    ORIG["original file<br/>(anywhere)"]
    SRC["source.‹ext›"]
    STEMS["stems/*.flac"]
    AJ["analysis.json"]
    PK["peaks.npz"]
    FT["features.npz"]
    ANIM["animations/‹render-id›.anim"]
    ENG["engine"]
    UI["UI (via server)"]
    PLAN["planning (server)"]

    ORIG -->|"import (copy)"| SRC
    SRC -->|"demucs"| STEMS
    SRC -->|"analysis models"| AJ
    STEMS -->|"analysis models"| AJ
    SRC -->|"peaks stage"| PK
    STEMS -->|"peaks stage"| PK
    SRC -->|"features stage"| FT
    STEMS -->|"features stage"| FT
    AJ --> PLAN
    FT --> PLAN
    PLAN -->|"plan"| ANIM
    SRC -->|"render job (samples)"| ANIM
    STEMS -->|"render job (samples)"| ANIM
    AJ -->|"render job"| ANIM
    FT -->|"render job"| ANIM
    SRC -->|"audio playback"| ENG
    ANIM -->|"frames"| ENG
    AJ --> UI
    PK --> UI
```

### `data/tracks/<key>/` — per-track data
Implemented by `TrackDir` and `import_track` (`analysis/track.py`).

**Import** (`import_track`):
- Reads the original once, hashing while it copies into
  `tracks/.import-<uuid>.tmp`, then renames the copy to
  `tracks/<key>/source.<ext>` (extension lowercased; same volume, so the
  rename is atomic).
- If `<key>` already has a source, the copy is discarded and the existing
  track is returned without writing to it.
- A new track gets an `analysis.json` with only the `source` block: `sha256`,
  `name` (the original filename without extension), `original_path` (where
  it came from; informational only, never read back).
- Sources: browser upload (**Add files**), a folder path typed in the UI
  (**Scan folder**, imports every audio file under it), or the CLI
  (`python -m volu_gen.analysis <files or folders>`). The CLI is dev-only:
  it bypasses the server, so its tracks get no DB row and show up in
  Settings as orphaned track directories.

**Removing a track** from the library asks whether to also
delete its directory. Keeping it makes re-adding the same file instant (no
demucs). Settings lists orphaned track directories (no DB row) with their
size and can delete them.

**Audio-derived files** — independent of the display and the generators:

| File | Contents | Produced by | Size (current library) |
|---|---|---|---|
| `source.<ext>` | The imported audio, byte-for-byte; any format ffmpeg decodes | import | ~17–72 MB (FLAC) |
| `stems/vocals.flac`, `drums.flac`, `bass.flac`, `other.flac` | demucs `htdemucs` stems, stereo, 44.1 kHz (`BASE_SR`), FLAC | `stems` stage | ~45–180 MB per track total; the bulk of the directory |
| `analysis.json` | Tempo/grid (incl. the user `override`), sections, phrases, events, per-bar energy, genre, mood, model ids. Schema: decision 0004 | model stages + `phrases`; `regrid` jobs | ~30 KB |
| `peaks.npz` | min/max/rms pyramid for the mix and four stems, int16. Format: decision 0006 | `peaks` stage | ~16 KB per second of audio |
| `features.npz` | Stem features for generators: per-lane envelopes (300 Hz), 24-band spectra and stereo width (100 Hz), onset lists for kick and hat from the drums stem via ADTOF (decisions 0016, 0017). Fixed rates, independent of the display FPS and the grid. Format: decision 0012 | `features` stage (CPU, after `peaks`) | ~8 MB for a 6.6-min track |
| `check/` | Listening aids: `clicks.wav`, `onsets_<class>.wav`, Audacity `labels_*.txt` | `analysis/checks.py`, on request | dev only; never read by the system; safe to delete |

- The decoded mix is **not** stored: `source.<ext>` is decoded whenever
  needed (analysis, peaks, features, render jobs, engine playback).
- **`analysis.json` is the source of truth** for analysis, including the
  BPM / grid-offset override (`tempo.override`, written by `regrid` jobs).
  The DB keeps only a copy of what the Library table shows (BPM, tempo mode,
  status), updated from job results.
- **Invalidation** (implemented in `analysis/pipeline.py`, `track.py`):
  - A model stage re-runs when its output is missing, its model id in
    `models` changed, or it is forced.
  - `phrases`, `events`, `bars` are re-derived whenever an earlier stage
    re-ran or the override changed.
  - An `analysis.json` of an older compatible `version` keeps model outputs
    and drops only the derived keys; any other version is rebuilt.
  - `peaks.npz` and `features.npz` are recomputed when their `version`
    differs or any stem file is newer than them (decisions 0006, 0012);
    neither re-runs on a grid override or an FPS change.

**`animations/` — rendered animations.**
One file per render: `animations/<render-id>.anim`. Unlike the files
above, these depend on the display geometry and the generator. The
**server picks the render id**
(a ULID or UUID) and the output path when it queues the job, so the DB row
exists before the file does. The worker writes `<render-id>.anim.tmp` in
the same directory and renames it when the job finishes.

**What a file holds.** Format: `decisions/0008-animation-file-format.md`.
A single file: header + JSON metadata, then frames in independently
zstd-compressed chunks of 1 s (`chunk_frames` = the file's fps, 30–480
frames; each chunk optionally temporal-delta filtered), then a chunk
index and footer.
Lossless; any frame decodes from the start of its chunk. It holds:

| Part | Contents |
|---|---|
| Metadata | format version · render id · track SHA-256 · geometry hash · pixel count `P` · fps · frame count · generator id + version · track params (JSON) · plan (JSON) · `analysis.json` version and the tempo override in effect · created time |
| Frames | `frame_count × P × 3` uint8 RGB in geometry order; frame `i` shows at `i / fps` s from the start of the audio |

Not in the file:
- **Audio.** The engine plays the track's `source.<ext>` from the same
  directory.
- **The node/strip mapping.** Applied at playback, so rewiring drive nodes
  (with unchanged pixel positions) needs no re-render. The geometry hash
  therefore covers only pixel positions and order, not `drive_nodes`.

**Size** (uncompressed, `P × 3 × fps` bytes/s; shown at the default
120 FPS, linear in the rate: 480 is 4×, 30 is ¼; compressed from synthetic
benchmarks in decision 0008, 4–50× depending on content, to be
re-measured on real renders):

| Display | Pixels | Per second | 4-min track | 4-min track, compressed (est.) |
|---|---|---|---|---|
| Prototype | 960 | 346 KB | ~83 MB | ~5–20 MB |
| Large build (example) | 20,000 | 7.2 MB | ~1.7 GB | ~35–420 MB |

**Lifecycle** (all in the DB; files only follow it):
- **Draft:** one draft row per track, created by its first render. Each
  Studio or default render gives that row a new file and render id; the
  previous draft file is deleted once the engine has `released` it
  (immediately, if it isn't loaded), unless a saved version uses it.
- **FPS re-render** (Main flows 6): replaces the active version's file in
  place, whether that version is the draft or a saved one. The row keeps
  its params and plan and gets the new file, fps, and render id; the old
  file is deleted once `released`.
- **Save draft as version:** creates a saved row with the draft's params,
  plan, and file (no file copy). The two rows share the file until the
  draft's next render gives the draft a new one.
- **Active:** a pointer to one version, draft or saved; what the queue
  plays. After import it is the draft, so Studio renders change what the
  queue plays; to freeze a version, save it and set it active.
- **Loaded file replaced:** whenever a render replaces the file of a
  version the engine has loaded (a Studio render, a re-render of the
  active version), the server sends `swap_anim`.
- **Stale:** the file's geometry hash, generator version, any method
  version in its plan, format version, or tempo override in effect
  differs from the current one. It
  still plays if the pixel count matches, its fps is the display FPS, and
  the reader still supports its format version (decision 0008 rejects
  unsupported ones); the UI flags it. Re-rendering a version stale by
  generator or method version re-plans it first (API server above); one
  stale only by the grid keeps its plan.
- **FPS mismatch:** the file's fps differs from the display FPS. **Not
  playable**: the queue skips it, Studio can't play it, and the engine
  refuses it. Cleared by an FPS re-render (Main flows 6), or by switching
  back to the file's rate.
- **Cleanup:** the server deletes files that no DB row references and that
  the engine doesn't have open. On startup, once any surviving worker is
  gone, it also removes leftover `*.tmp` files.

## Backend package layout
| `volu_gen/` module | Purpose |
|---|---|
| `analysis/` | implemented (decision 0004) |
| `geometry/` | display config → positions, geometry hash; implemented (decision 0013) |
| `mapping/` | pixel → node/strip/index, validation; implemented (decision 0013) |
| `animfile/` | animation file read/write; implemented (decision 0008) |
| `generators/` | director, plan, methods (`generators.md`); `Music`, `RenderContext`, the method interface, the plan model, the director, the render job and the first methods (`geometry`, `raindrops`, `strobe`, `galaxy`; decision 0019) implemented, the other methods to come. Imported by the server (planning: director + each method's `choose_params`) and the worker (rendering), so no torch or other heavy imports at module level |
| `output/` | protocol v2, packetizer, UDP sender, simulated node, test-pattern CLI; implemented (brightness and power cap still to come) |
| `engine/` | engine process (threads, clock, sources, slots) |
| `worker/` | worker process (job runner) |
| `server/` | API server (FastAPI, DB, queues, supervision) |
| `ipc/` | message models, connection helpers |

## Settled
Former open questions, now part of the design above:
- **Playback across a server restart:** yes. The engine keeps playing
  (Failure handling); a restarted server reconnects to the engine's bound
  `ROUTER` socket (IPC).
- **Imports are processed in series**, one track at a time through analysis
  and its default render; no second worker, no parallelization (Worker).
- **Unrendered queued tracks are skipped**, not waited for (Main flows 2);
  queueing one still being imported promotes its import work (Worker).
- **Plans are anchored by bars:** a grid override marks versions stale,
  and re-rendering moves their segments onto the new grid without
  re-planning, so a saved version keeps its look (Main flows 4).
- **One draft row per track**; saving shares the file; active may be the
  draft (Lifecycle).
- **Server restart adopts the engine and replaces the worker**
  (API server → Startup).
- **Leaving Studio returns the queue paused** where it was; Studio keeps
  no playhead per track (Main flows 7).
- **Track changes are seamless for lights too:** one output sample
  counter across tracks; frames come from whichever slot holds the offset
  target time (Engine → Clock).
- **Display FPS** (`decisions/0010-display-fps.md`): a Settings value,
  **any whole number from 30 to 480 (default 120)**. Animations render at
  the display FPS in effect when the render starts, and play only at
  their own rate: after a change, every render at another rate is
  unplayable until re-rendered (Engine → Clock; Main flows 6; Lifecycle).
  The longest strip must fit the frame time: at ≈ 30 µs per pixel, about
  1,100 / 270 / 65 pixels per strip at 30 / 120 / 480 FPS; Settings
  refuses a rate the display config can't meet.
- **Planning runs in the API server**, not as a worker job
  (`decisions/0011-planning-in-server.md`; API server above).
- **Preview rate: up to the display FPS.** The preview FPS setting
  (≤ display FPS) is set by the user to the UI monitor's refresh rate;
  frames beyond it would never be drawn. Cost at 120 FPS (linear in the
  rate):
  - Data: prototype 2.9 KB/frame ≈ 350 KB/s; 20k pixels 60 KB/frame
    ≈ 7.2 MB/s. Trivial on localhost, where v1 runs the browser (`ui.md`).
    A browser on another machine would need ~58 Mbit/s (fine wired,
    marginal on Wi-Fi).
  - Browser: one 60 KB color-buffer upload per frame; bloom is the
    heaviest part and wants a discrete GPU at 20k pixels.
  - Engine: preview sends share the GIL with the frame thread. **Measure**
    frame timing with the preview on vs. off once the engine exists; lower
    the setting if it adds jitter.
  - Frames arrive unsynchronized with vsync, so an occasional frame shows
    twice or is skipped. Accepted; a small jitter buffer in the browser can
    fix it later if it is visible.
  - Latest-only delivery means a slow browser just shows fewer frames.
- **Animation file format:** lossless, 1 s chunks, per-chunk temporal
  delta + zstd, chunk index at the end; decoded by an engine prefetch
  thread (Storage layout → `animations/`, Engine threads,
  `decisions/0008-animation-file-format.md`). Decode costs ≤ ~1.5 % of a
  core at 20k pixels and 120 FPS (linear in the rate: ~6 % at 480), so
  engine CPU is not a constraint.
- **Audio output API: WASAPI shared by default, exclusive as a setting**
  (`decisions/0009-audio-output-api.md`). Absolute latency barely matters
  here: frames are scheduled from the audio clock and `display_offset`
  absorbs whatever latency there is. What matters is that latency is
  **stable and correctly reported** (`stream.time` vs. DAC timestamps).
  - Shared mode coexists with other apps and won't fail when the device is
    busy; the Windows mixer runs at its own sample rate, so the engine
    resamples to the device rate (or uses
    `WasapiSettings(auto_convert=True)`).
  - Exclusive mode gives lower, tighter latency but locks the device.
  - Calibration is stored per (device, mode), since the offset differs.
  - Still to verify with the calibration flash + click once the engine
    exists.
- **Brightness and power cap are applied by the engine at playback**,
  not baked into renders, and change live like the sync offset (Engine →
  Output, `configure`).
- **Removing a track asks whether to also delete its directory**; kept
  directories make re-adding instant, and Settings lists and deletes
  orphaned ones (Storage layout → `data/tracks/<key>/`).
- **Stem features** (`features.npz`, `decisions/0012-stem-features.md`):
  a CPU analysis stage after `peaks`, at fixed rates independent of the
  display FPS and the grid. Onsets (kick and hat only) come from the ADTOF
  drum transcription model on the drums stem
  (`decisions/0016-drum-onsets-adtof.md`, `decisions/0017-kick-and-hat-onsets-only.md`).

## Open questions
1. **Generator interface** — design in progress in `generators.md`: a
   director that plans segments and picks a method per segment, then
   renders the plan. Its own open questions are listed there.
