# 0010 — Display FPS: 60, 120, or 240, set in the UI

Date: 2026-11-09. Status: accepted; not yet implemented. Design in
`../architecture.md` → Engine (Clock), Main flows 6, Settled.

## Context
The drafts assumed one fixed render rate (`timing.max_fps: 120` in the
display config) with a separate, lower playback FPS setting derived from
it by dropping frames. The display should be able to run faster than
120 FPS when its strips are short enough, and slower when they're long.

## Decision
- **One display FPS setting: 60, 120 (default), or 240**, chosen in
  Settings and stored by the server. The display config gives only the
  default.
- **Animations render at the display FPS** in effect when the render is
  queued; the file records its fps.
- **Lowering the display FPS needs no re-render.** The engine wakes at the
  display FPS and picks the current frame from the file, dropping frames.
- **Raising it does.** A file below the display FPS still plays, holding
  each frame, but is flagged stale. Re-rendering is on request (a bulk
  "Re-render stale" action in the Library), never automatic.
- The rate must fit the longest strip (WS2811 ≈ 30 µs per pixel): about
  550 / 270 / 135 pixels per strip at 60 / 120 / 240 FPS. Settings refuses
  a rate the display config can't meet.
- Animation file chunks stay 1 s long: `chunk_frames` = the file's fps.

## Alternatives considered
- **Always render at 240, decimate down:** twice the render time and disk
  of 120 for every track, for a rate most displays won't run.
- **Free-form FPS:** odd rates don't divide evenly into each other, so
  dropping frames gives uneven motion. 60 / 120 / 240 each halve or
  double.

## Consequences
- File sizes and render times scale with the rate: 240 FPS doubles both
  compared with 120.
- Content near the frame rate can alias when frames are dropped: a strobe
  flashing every other frame in a 120 FPS file plays at 60 FPS as solid on
  or solid off. The plan records its fastest strobe rate, and a version
  whose rate exceeds half the current display FPS is flagged stale
  *(proposed; `../generators.md` → Smoothness)*.
- Features (`features.npz`) use a fixed rate of at least the highest
  display FPS, so they don't depend on this setting.
