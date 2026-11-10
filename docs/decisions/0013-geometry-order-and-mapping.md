# 0013 — Geometry order, geometry hash, and mapping checks

Date: 2026-11-09. Status: accepted; implemented in
`volu_gen/geometry/` and `volu_gen/mapping/`.

## Context
Animation files, the preview, frame sources and generators all hold one
color per pixel "in geometry order" (decision 0008), and renders are
marked stale by a **geometry hash**. Neither the order nor the hash was
defined. The mapping checks (`architecture.md` → API server) also needed
exact limits.

## Decision

### Geometry order
Strip by strip, top to bottom: slices (z) outermost, then strips across a
slice (x), then pixels down the strip (y):

    i = (z * X + x) * Y + y

- Each strip is a contiguous run, pixel 0 (top) first, the same layout as
  a node's pixel buffer (`protocol.md` → PIXELS). For sections split by
  slice (as on the prototype) a node's buffer is one contiguous slice of
  the frame, so packetizing needs no gather.
- Order is fixed by the config's `geometry.size`; drive nodes don't
  affect it.

### Positions
`(P, 3)` float64 mm in the config's axes: x across a slice, **y downward**
(y = 0 at the top, as the indices), z depth. Pixel (0, 0, 0) is at the
origin; odd slices add `odd_slice_offset` to x and y. Consumers that want
y up (the UI's three.js scene) flip it themselves.

### Geometry hash
sha256 over `b"volu-geometry-1\0"`, the pixel count (u32 LE), and the
positions as float64 LE in geometry order; the first 16 hex characters
(as for track keys). It covers positions and order only, not
`drive_nodes`, so rewiring needs no re-render (decision 0008). A change
to the hashed bytes bumps the tag. Test vector: the prototype's hash is
`c8f51a734795a627`.

### Mapping checks
Building the mapping checks, and reports every problem at once (for the
UI's validation results):
- no two sections overlap, and together they cover every pixel;
- ≤ 64 strips per node;
- ≤ 65,536 pixels per node buffer (the PIXELS offset is a u16);
- `timing.default_fps` fits the longest strip.

The config parser separately rejects unknown or missing keys, wrong types,
sections outside the display or with min > max, and duplicate node ids or
IPs.

### Strip timing
A show takes `leds × 30 µs + 50 µs` (WS2811 reset ≥ 50 µs, datasheet). A
display FPS fits when `leds ≤ floor((1e6 / fps − 50) / 30)`: 1,109 / 553 /
276 / 137 / 67 pixels at 30 / 60 / 120 / 240 / 480 FPS, matching the
"about 1,100 / 550 / 270 / 135 / 65" in decision 0010. Revisit the
constants once the I2S firmware's real show time is measured.

## Alternatives considered
- **Slice-major with y outermost (row by row):** matches no buffer
  layout; every node would need a gather.
- **Hashing the `geometry` block of the YAML:** formatting or key order
  changes would mark renders stale, and two configs with the same
  positions would hash differently.
- **Y up in positions:** two conventions for the same axis (indices down,
  positions up) invite sign errors; one convention everywhere.
