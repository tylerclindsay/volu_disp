# 0018 — Render output: linear PWM values, centered crossfades

Date: 2026-11-10. Status: accepted; built
(`gen_system/backend/src/volu_gen/generators/render.py`,
`tests/test_render.py`). Settles two points `../generators.md` left
open: the "fixed gamma" of the render job, and where a crossfade sits
relative to the segment boundary.

## Context
Methods return float32 linear RGB (`../generators.md` → Methods): values
proportional to emitted light, with palette colors converted from sRGB
(`RenderContext.colors`). The render job turns them into the uint8 frames of
the `.anim` (decision 0008), which playback sends to the LEDs unchanged
apart from global brightness (engine; there is no power cap, decision
0020).

WS2811 drivers turn each byte into a PWM duty cycle, and an LED's light
output is close to linear in duty cycle. The firmware applies no curve
(`config.h` → `GLOBAL_BRIGHTNESS`, a linear scale, kept at 255).

A plan entry's crossfade is "over `beats` beats" (`Transition`) but the
plan didn't say whether the fade ends at, starts at, or straddles the
segment's first downbeat.

## Decision
- **The file holds PWM values, linear in light: `round(clip(x, 0, 1) × 255)`,
  no gamma curve.** Methods already work in linear light, so a gamma here
  would apply a perceptual curve twice and wash out dark tones. The
  "fixed gamma" in `../generators.md` is therefore 1. Intensity (the track
  param) is a linear scale applied before clipping.
- **A crossfade is centered on the downbeat** that starts the new segment:
  `beats × beat length` at that downbeat, half before it and half after,
  with a smoothstep weight. Each half is at most half of the segment on
  that side, so neighboring fades never overlap. Weights are written as
  `ramp(t, start_i, h_i) − ramp(t, end_i, h_i+1)`, so every frame's weights
  sum to exactly 1 (a cut is a step: a frame starting on the downbeat
  belongs to the new segment). Blending is in linear light.
- **The render job refuses a stale plan** (`StalePlan`): another director
  version, a method version that differs from the registered one, or an
  unregistered method. The server re-plans first (`../architecture.md` →
  Lifecycle); the worker renders exactly the plan it is given.
- **Method output is checked** for shape, float32 and finite values; a
  bad method fails the render with its id and frame range instead of
  writing garbage.

## Consequences
- Low levels quantize coarsely: linear values under 0.5/255 are black and
  the bottom few steps are visible as steps on a slow fade to black. This
  is the LEDs' 8-bit PWM, not the file; a curve would not add PWM
  resolution. Temporal dithering at the display FPS could hide it, at some
  cost in compression; try it only if fades to black look stepped on the
  display.
- If the LEDs turn out visibly non-linear at low duty, correct it here (a
  measured curve in `to_uint8`) with a generator version bump, not in
  methods.
- The UI preview shows file frames on an sRGB screen, so it must encode
  them (linear → sRGB) to look like the display.
- A centered fade means the outgoing method renders up to half a fade past
  its segment's end and the incoming one up to half a fade before its
  start, which methods already allow (`../generators.md` → Methods).
