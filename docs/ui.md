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
- **Sync** button → popover to fix audio/light alignment while listening
  (below).

## Sync
The A/V sync offset (`architecture.md` → Engine → Clock) is easy to reach
and changes take effect immediately, during playback, with no reload.
- Popover from the bottom bar, available on every page:
  - **Display offset** (ms, positive = lights earlier): slider plus
    −5 / −1 / +1 / +5 ms nudge buttons and a typed value; reset to the last
    calibrated value.
  - **Preview offset** (ms): the same controls for the 3D preview.
  - Shows which audio device + mode the values belong to.
- Keyboard nudges for the display offset (e.g. `[` / `]` = ±1 ms, with
  Shift ±5 ms), so it can be tuned by ear without looking.
- Values are saved per audio device + output mode. The same setting is
  shown in Settings and written by the calibration on the Display page.

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
(none / rendering / ready / stale / FPS mismatch), actions.
- **Add files** (upload from the browser) / **Scan folder** (type a folder
  path; every audio file under it is imported). Import **copies** each file
  into the track's own data directory; the original is not used afterwards
  (`architecture.md` → Storage layout). A file that is already imported
  shows its existing track. Adding a track starts the pipeline
  automatically (stems → analysis → render with default parameters).
- BPM column shows `variable` for tracks without a constant grid
  (`decisions/0004-audio-analysis.md`).
- Row actions: **Play now**, **Add to queue**, open in Studio.
- "Stale" = the active version was rendered against a different geometry
  hash, generator or method version, file format version, or beat grid;
  it still
  plays if its pixel count matches and its format version is still
  readable (`architecture.md` → Lifecycle). **Re-render stale** queues them all (like import work).
- "FPS mismatch" = the active version was rendered at another rate than
  the display FPS; it **doesn't play** until re-rendered. After a display
  FPS change, tracks in the play queue are re-rendered automatically
  (Settings); the row shows that progress. Others are re-rendered by hand:
  a row action, or **Re-render FPS mismatch** for all of them (like import
  work). Either re-renders the active version in place with its params
  and plan (`architecture.md` → Main flows 6).

### Player
```
┌─────────────────────────────────────────┬─────────────────────┐
│                                         │ Now: Track A        │
│        3D preview (~65%)                │ BPM 128.0  beat ●   │
│                                         │ Queue               │
│                                         │  1. Track C   ✓     │
│                                         │  2. Track B   ◔     │
├─────────────────────────────────────────┴─────────────────────┤
│ section / phrase bands, vocals / drums / bass / other lanes,  │
│ beat grid, playhead; click to seek                            │
└───────────────────────────────────────────────────────────────┘
```
- **Queue:** ordered list, drag to reorder, remove. No playlists. Tracks
  play end to end; when one ends the next starts immediately (no fades).
- A queued track plays its **active** version. A track without one is
  rendered first (so is one whose active version is at another rate than
  the display FPS); its entry shows progress, and it is skipped if it isn't
  ready when reached: it stays at the front of the queue and plays once
  ready (`architecture.md`, Main flows).
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
- Header: track, detected BPM and beat-grid offset, both **editable to
  override** a wrong grid fit (e.g. nudge the offset by ms or by one beat,
  type the true BPM). An override replaces the fitted grid and re-runs only
  the grid-dependent analysis (phrases, per-bar energy) — no model re-runs;
  "reset" returns to the fit. Variable-tempo tracks show `tempo: variable`
  and can be forced onto a grid the same way.
- Stem lanes carry **section and phrase bands** above them: section
  boundaries, 8-bar phrases with drop / buildup / breakdown tags, and
  drums/bass in/out markers, aligned to the beat grid. They show what the
  generator sees and make a bad grid or phrasing obvious at a glance.
- **Parameters** come in two levels (`generators.md` → Studio parameters):
  - **Track params**, constant for the whole track: intensity, change
    rate, palette, enabled methods and weights, strobe max rate, seed.
  - **Instance params**, per use of a method: a **segment lane** above the
    stem lanes shows the plan; select a block to edit that instance's
    params. Edited instances are pinned across re-plans; "reset" returns
    a field to automatic.
  - No automation *within* a segment in v1.
- The segment lane updates as soon as a track param changes (planning is
  instant); frames follow on **Render**.
- The parameter form is **generated from the generator's parameter schema**
  (e.g. pydantic → JSON Schema): sliders, color pickers, stem selects. Adding
  a parameter needs no UI code.
- **Render loop:** edit params (nothing renders automatically) → **Render**
  (Ctrl+Enter) re-renders the **entire track** → while rendering, playback
  continues on the old frames → when done, playback resumes at the same
  playhead on the new frames. A draft or version at another rate than the
  display FPS shows "rendered at N FPS: render to play" and doesn't play.
  On the active version, the version bar offers an FPS re-render: it
  re-renders that version in place with its params and plan, like the
  Library's action.
- **Versions:** one **Draft** slot per track, overwritten by every render.
  **Save draft as version** keeps it with its parameters and plan
  (including instance edits); **Set active** picks what the queue plays.
  Selecting a version loads its parameters and plan into the form.
- **Presets:** named sets of track params per generator, reusable across
  tracks (instance edits are track-specific and not in presets). Undo
  covers track and instance param edits.
- If full renders get too slow on the large display, add a "draft quality"
  option (cheaper method settings, not a lower FPS: a render at another
  rate than the display FPS doesn't play) before reaching for the loop
  preview.

### Display
- 3D view colored by drive node section (optionally by strip). Hover a pixel
  → `node · strip · pixel · (x, y, z)`.
- Node cards: id, IP, online, last packet sent; telemetry once type 3 exists.
- Test patterns: solid color, node ID, strip ID, x/y/z sweeps, blink strip N.
- Latency calibration: flash + click loop with an ms offset slider; sets the
  display offset (see Sync).

### Settings
- **Display FPS: any whole number from 30 to 480, default 120**
  (`decisions/0010-display-fps.md`). Renders use it, and a render plays
  only at its own rate. Changing it asks for confirmation first, showing
  how many versions become unplayable and the estimated re-render time;
  on confirm, playback stops and the active version of every track in the
  play queue is queued for re-render at the new rate. Other tracks are
  re-rendered from the Library or Studio (`architecture.md` → Main flows
  6). A rate the
  longest strip can't meet is refused (≈ 1,100 / 270 / 65 pixels per
  strip at 30 / 120 / 480 FPS); the field shows the display's maximum.
- Preview FPS (≤ display FPS): set to the UI monitor's refresh rate.
  Lowering the display FPS below it lowers it too.
- Audio output device and mode; display and preview offsets (same values as
  the Sync popover).
- Network interface for the display.
- Active display config: read-only summary + validation results.
- Storage: disk used by track data; orphaned track directories (no library
  entry) with sizes and a delete action.

## Deferred (not v1)
- **Loop preview in Studio:** render only a bar-snapped loop region, re-render
  it in the background on every param change, swap new frames in at the
  playhead without stopping; A/B between two parameter sets. Needs generators
  that can render any time range independently (no state carried across
  frames, or recomputed from a short look-back window). Write generators that
  way where it's cheap, so this stays possible.
- Parameter automation within a segment.
- **Manual plan editor in Studio:** beyond v1's instance params, choose
  which method runs on which beats, bars, or sections, and change segment
  boundaries and transitions; edited entries are pinned across re-plans
  (`generators.md` → Plan).
- Playlists, crossfades.
- A separate "show mode" UI.
