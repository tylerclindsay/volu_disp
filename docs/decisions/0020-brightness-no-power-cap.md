# 0020 — Brightness only, no power cap

Date: 2026-11-10. Status: accepted (not yet implemented).

## Context
The engine was to apply a global brightness and a power cap at playback
(`../architecture.md` → Engine → Output). A power cap needs the display's
current draw, and the hardware has no current sensing. Estimating it from
pixel values (current per channel × duty cycle, summed per supply) would
be too inaccurate to trust as a limit.

## Decision
- The engine applies **global brightness only**: one linear scale on the
  frame values, 0–100 %, default 100 %, set from the UI's bottom bar or
  Settings and changed live through `configure`, like the sync offset.
- Brightness is applied to the frames sent to the nodes and to the
  preview frames, so the preview matches the hardware (`../ui.md` →
  Output).
- There is **no power cap** in the engine, the UI, or the display config.
- The firmware's `GLOBAL_BRIGHTNESS` stays at 255 except for bench tests.

## Consequences
- Keeping the supplies within their rating is up to the hardware design
  and the brightness setting.
- A power cap can be reconsidered if the hardware gains current sensing
  (e.g. reported in protocol type 3 status packets).
