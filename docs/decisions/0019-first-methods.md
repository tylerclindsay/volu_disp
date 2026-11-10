# 0019 — First methods: drawing space, hits, strobe timing

Date: 2026-11-10. Status: accepted; built
(`gen_system/backend/src/volu_gen/generators/methods/`,
`tests/test_methods.py`). The first four methods of `../generators.md`
(build order step 3): `geometry`, `raindrops`, `strobe`, `galaxy`. This
records the choices they share; each method's own design is in
`../generators.md` → Methods → As built.

## Context
`RenderContext.pos` keeps the display's aspect ratio (the longest axis
spans [-1, 1]). The prototype is 475 × 575 × 175 mm, so in `pos` its depth
is only ±0.3: a sphere sized to fit would be about two pixels across in z
and four in x, and a galaxy disc or a sea surface would barely reach the
front and back slices. The large build's aspect isn't known yet.

`Music.since` gives only the last hit, so a decay built on it drops
suddenly when a weak hit follows a strong one. Feature envelopes are
unsmoothed (decision 0012), and methods may not carry state from frame
to frame, so the usual one-pole smoother can't run in `render`.

A strobe flash shorter than a frame can fall between two frame starts and
be lost, depending on the FPS (decision 0010: any whole number 30–480).

## Decision
- **Methods draw in `ctx.box`:** pixel positions with each axis stretched
  to [-1, 1] (y up), so a shape, a disc or a sea fills the volume
  whatever its aspect. Soft edges are measured in the same space, with
  `ctx.pitch` (the nearest-neighbour spacing in `box`) as the width.
  `pos` stays for methods that need true proportions.
- **Hits** (`common.hit_env`): an instant attack and an exponential decay
  per hit, scaled by its strength, taking the largest over the last four
  hits.
- **Smoothed envelopes** (`common.Signal.envelope`): a lane's envelope is
  smoothed once per segment, with a causal exponential, on a 300 Hz grid
  over the segment's span (the segment, half of it each side for
  crossfades, and a margin). Because the grid is fixed by the segment and
  not by the frames asked for, rendering stays range-independent.
  `Signal.integral` turns a speed into a distance or angle the same way.
- **Strobe flashes** last `duty` of the flash period but **at least one
  frame**, and are point-sampled at frame starts. Every flash then lands
  on the first frame starting at or after its grid time, at any FPS. The
  rate is halved (staying on the grid) until it is at most
  `ctx.strobe_rate`.

## Consequences
- On a shallow display like the prototype, shapes are stretched in depth
  (a sphere becomes an ellipsoid as deep as the display) and z detail is
  coarser than x and y: the prototype has 8 slices against 10 strips and
  12 pixels.
- `box` needs no re-render on its own: it is a function of the geometry,
  which the geometry hash already covers.
- Render speed at 20,800 pixels and 120 FPS (dev PC, one core of numpy),
  per second of track: strobe 0.04 s, galaxy 0.13 s, geometry 0.3 s,
  raindrops 0.3–0.45 s. A 4-minute track renders in about 1–2 minutes at
  120 FPS and four times that at 480. Speed this up only if the large build
  needs it.
- These four methods' looks (sizes, rates, colors, `suits` ranges) are
  starting points, tuned so far only on contact sheets of a stored track;
  they are to be tuned by eye on the display, each change with a version bump.
