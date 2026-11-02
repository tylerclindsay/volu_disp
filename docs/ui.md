# UI design (v1)

Status: agreed design, not yet implemented. Decision record:
`decisions/0003-ui-design.md`.

Single user, running on the same PC as the engine. The UI is a remote
control: the **engine plays the audio and sends frames**; the UI sends
commands and draws what the engine reports. The browser never plays audio
and never does timing.

## Stack
| Concern        | Choice                                                      |
|----------------|-------------------------------------------------------------|
| Build          | Vite + React + TypeScript                                   |
| Components     | Mantine v7, dark theme only; Tabler icons                   |
| 3D preview     | react-three-fiber + drei; pixels as one `InstancedMesh` (or `Points`) at real coordinates from the geometry endpoint; bloom via `@react-three/postprocessing` |
| Stem waveforms | Custom canvas, drawn from peaks computed by the backend     |
| Server state   | TanStack Query (HTTP: library, settings, jobs, versions)    |
| Live state     | Zustand store fed by one WebSocket (playback position, engine/node status, job progress, binary preview frames) |
| Routing        | TanStack Router                                             |
| Charts         | uPlot, if/when node telemetry is graphed                    |

## App shell
Left nav rail: **Library · Player · Studio · Display · Settings**.

Persistent bottom bar on every page:
- Transport: play/pause/stop/next, title, BPM, position + seek bar, FPS.
- Engine mode: **Playback** (queue) or **Studio**.
- Status: engine connected, each drive node online/offline, output
  virtual/armed.
- Jobs indicator → drawer with stem separation / analysis / render progress.

## Engine modes
One audio device and one display, so the engine is in one mode at a time:
- **Playback** — plays the queue.
- **Studio** — plays the track open in Studio. Entering Studio pauses the
  queue.

## Output
- The virtual display (3D preview) always shows the frames being played.
- Hardware output is **armed automatically when drive nodes are detected**,
  in both modes. The bottom bar toggle overrides it (e.g. disarm to work
  silently on the hardware).
- Until node status packets (protocol type 3) exist, "detected" means the
  node's IP from the display config answers a ping. A node that isn't
  detected shows offline; armed output still goes to the nodes that are.

## Pages

### Library
Table of tracks: title, length, stems status, BPM, animation status
(none / rendering / ready / stale), actions.
- **Add files** / **Scan folder**. Adding a track starts the pipeline
  automatically (stems → BPM → render with default parameters).
- Row actions: **Play now**, **Add to queue**, open in Studio.
- "Stale" = the active version was rendered against a different geometry
  hash or generator version.

### Player
```
┌─────────────────────────────────────────┬─────────────────────┐
│                                         │ Now: Track A        │
│        3D preview (~65%)                │ BPM 128.0  beat ●   │
│                                         │ Queue               │
│                                         │  1. Track C   ✓     │
│                                         │  2. Track B   ◔     │
├─────────────────────────────────────────┴─────────────────────┤
│ vocals / drums / bass / other lanes, beat grid, playhead;     │
│ click to seek                                                 │
└───────────────────────────────────────────────────────────────┘
```
- **Queue:** ordered list, drag to reorder, remove. No playlists. Tracks
  play end to end; when one ends the next starts immediately (no fades).
- A queued track plays its **active** version. A track without one is
  rendered first; its entry shows progress, and it is skipped if it isn't
  ready when reached (or: playback waits — decide when implementing).
- 3D preview: orbit, view presets (front/side/top/iso), slice-isolate
  slider, pixel size and bloom controls.

### Studio
```
┌ Track ▾ │ Generator ▾ │ Preset ▾ [Save] [Undo] ─────────────────┐
│                                   │ Parameters (grouped)         │
│        3D preview                 │   changed params marked *    │
│                                   │                              │
├───────────────────────────────────┤ [Render]  progress · ETA     │
│ stem lanes + beat grid + playhead │ "draft out of date"          │
├───────────────────────────────────┴──────────────────────────────┤
│ Draft · v3 (active) · v2     [Save draft as version] [Set active] │
└───────────────────────────────────────────────────────────────────┘
```
- Header: track, detected BPM (editable to correct detection), beat-grid
  offset.
- **Parameters** are constant for the whole track (no per-section values or
  automation in v1). The generator may still vary its output over time from
  the stems and BPM; the user-set parameters don't.
- The parameter form is **generated from the generator's parameter schema**
  (e.g. pydantic → JSON Schema): sliders, color pickers, stem selects. Adding
  a parameter needs no UI code.
- **Render loop:** edit params (nothing renders automatically) → **Render**
  (Ctrl+Enter) re-renders the **entire track** → while rendering, playback
  continues on the old frames → when done, playback resumes at the same
  playhead on the new frames.
- **Versions:** one **Draft** slot per track, overwritten by every render.
  **Save draft as version** keeps it with its parameters; **Set active**
  picks what the queue plays. Selecting a version loads its parameters into
  the form.
- **Presets:** named parameter sets per generator, reusable across tracks.
  Undo covers parameter edits.
- If full renders get too slow on the large display, add a "draft quality"
  option (e.g. render at 30 FPS) before reaching for the loop preview.

### Display
- 3D view colored by drive node section (optionally by strip). Hover a pixel
  → `node · strip · pixel · (x, y, z)`.
- Node cards: id, IP, online, last packet sent; telemetry once type 3 exists.
- Test patterns: solid color, node ID, strip ID, x/y/z sweeps, blink strip N.
- Latency calibration: flash + click loop with an ms offset slider.

### Settings
- Playback FPS (validated against the longest strip: warn above ~270 pixels
  per strip at 120 FPS).
- Audio output device, latency offset.
- Network interface for the display.
- Active display config: read-only summary + validation results.
- Library folders.

## Deferred (not v1)
- **Loop preview in Studio:** render only a bar-snapped loop region, re-render
  it in the background on every param change, swap new frames in at the
  playhead without stopping; A/B between two parameter sets. Needs generators
  that can render any time range independently (no state carried across
  frames, or recomputed from a short look-back window). Write generators that
  way where it's cheap, so this stays possible.
- Parameters that change over the track (per-section values / automation).
- Playlists, crossfades.
- A separate "show mode" UI.
