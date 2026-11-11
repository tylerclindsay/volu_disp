# 0010 — Display FPS: any whole number from 30 to 480, set in the UI

Date: 2026-11-09, revised the same day. Status: accepted; partly
implemented: the setting and its checks, FPS re-render jobs and the
confirmation counts (`gen_system/backend/src/volu_gen/server/`), and
renders at the display FPS (`worker/`). The engine side and the cancel
of a running render are still to come. Design in `../architecture.md` →
Engine (Clock), Main flows 6, Storage layout → `animations/` (Lifecycle),
Settled.

## Context
The drafts assumed one fixed render rate (`timing.max_fps: 120` in the
display config) with a separate, lower playback FPS setting derived from
it by dropping frames. The display should be able to run faster than
120 FPS when its strips are short enough, and slower when they're long.

The first version of this decision allowed 60, 120, or 240 FPS and played
a render at another rate by dropping or holding frames. Dropping frames
changes how content looks (a strobe flashing every other frame plays as
solid on or off), holding them judders, and a free choice of rate makes
both uneven, since rates no longer divide into each other.

## Decision
- **One display FPS setting: any whole number from 30 to 480**, default
  120, chosen in Settings and stored by the server. The display config
  gives only the default (`timing.default_fps`).
- **Animations render at the display FPS**; the file records its fps.
- **A render plays only at its own FPS.** The engine never drops, holds,
  or resamples frames. Any version (draft, saved, active) whose fps
  differs from the display FPS is **not playable** until it is
  re-rendered at the display FPS.
- **Changing the display FPS** (`../architecture.md` → Main flows 6):
  - Settings shows how many versions the change makes unplayable and asks
    for confirmation. Playback stops, since the loaded track can't play
    at the new rate.
  - The server queues an **FPS re-render** of the active version of
    every track in the play queue, so the queue becomes playable again
    without further action. Other tracks are re-rendered manually (a
    Library row action, a Library bulk action, or a button on the active
    version in Studio), at import-work priority. An FPS re-render covers
    only the track's active version.
  - An FPS re-render uses the version's stored track params and plan
    unchanged (segment times aside, recomputed from bars on the current
    grid as for every render) (plans don't depend on the FPS, `../generators.md` → Plan),
    so it looks the same at the new rate. If the generator or a method
    version changed since, the server re-plans first and the new plan
    overwrites the stored one. The re-render replaces the version's file
    in place; switching back to the old rate means re-rendering again.
  - A render job takes the display FPS **when it starts**, not when it is
    queued. A render running when the FPS changes is cancelled and
    re-queued.
- **The rate must fit the longest strip** (WS2811 ≈ 30 µs per pixel):
  highest FPS ≈ `1 / (longest_strip × 30 µs + reset)`, about 1,100 / 550 /
  270 / 135 / 65 pixels per strip at 30 / 60 / 120 / 240 / 480 FPS.
  Settings refuses a rate the display config can't meet.
- The preview FPS setting stays ≤ the display FPS; lowering the display
  FPS below it lowers it too.
- Animation file chunks stay 1 s long: `chunk_frames` = the file's fps.

## Alternatives considered
- **60 / 120 / 240, lower rates by dropping frames** (the first version):
  too coarse a choice, and dropping frames aliases fast content.
- **Resample any render to any rate at playback** (nearest frame): uneven
  frame spacing whenever the rates don't divide, plus the same aliasing.
- **Always render at 480, decimate down:** four times the render time and
  disk of 120 for every track, and only rates dividing 480 come out even.
- **Keep one file per rate for each version:** switching back would be
  instant, but disk multiplies; can be added later if rates change often.
- **Re-render nothing automatically:** after any FPS change the whole
  queue would be unplayable until the user acted.
- **Re-render every active version automatically** (an earlier draft of
  this decision): replaced by automatic re-renders for queued tracks
  only, with the rest on request.

## Consequences
- An FPS change is expensive: every queued track re-renders, minutes per
  track on the large display, and every other track stays unplayable
  until re-rendered by hand. The confirmation shows the counts and an
  estimated time. Until a queued track is re-rendered, the play queue
  skips it, as it does any unrendered track (`../architecture.md` → Main
  flows 2). A track added to the queue later is re-rendered then.
- File sizes and render times scale with the rate: 30 FPS is a quarter
  of 120, 480 four times.
- Methods work in seconds, never in frames, so they look the same at any
  rate. The one per-frame limit is strobe's: at most one on/off cycle per
  two frames, so below 120 FPS its 60 flashes/s ceiling drops to half the
  FPS (`../generators.md` → Smoothness).
- At 480 FPS a frame lasts 2.08 ms. The engine's 1 ms Windows timer
  resolution is then up to half a frame of wake jitter, and per-frame work
  (wake, packetize, PIXELS, SYNC, preview) must fit in 2 ms of Python.
  Bytes per second per strip don't grow (the strip length cap keeps
  pixels × FPS constant), but packet and SYNC rates do. Measure with the
  timing test (`../../gen_system/AGENTS.md` §7) at 30 and 480 FPS; use a
  high-resolution waitable timer or a short spin if needed.
- Features (`features.npz`) use a fixed rate independent of the display
  FPS; `Music` interpolates them to frame times.
- The protocol is unchanged; it never fixed a rate (`../protocol.md`).
