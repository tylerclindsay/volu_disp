# 0002 — Protocol v2

Date: 2026-11-01. Status: accepted (not yet implemented).

## Context
v1 sends one strip per packet and latches each node on its own `SHOW`
packet. With 64 strips per node at 120 FPS that is ~7,700 small packets/s
per node, and nodes latch at different times (skew = time to send the rest
of the frame). The prototype's whole per-node frame (40 × 12 pixels =
1,440 bytes) fits in one packet.

## Decision
See `../protocol.md` for the full spec.
- Add a `version` byte and a u32 `frame` sequence number to every packet.
- PIXELS address a flat per-node pixel buffer (`offset` + `count`), so one
  packet can carry several strips. This matches the FastLED I2S driver's
  single contiguous buffer.
- A broadcast **SYNC** packet latches all nodes at once.
- Nodes double-buffer so incoming data never overwrites pixels being
  clocked out.
- Incomplete frames are shown anyway (missing regions keep the previous
  frame) and counted, rather than skipped — loss on a wired LAN is rare and
  skipping looks like stutter.
- Status replies from nodes are deferred; packet type `3` is reserved.
- Strip layout stays compile-time in the firmware; the host validates its
  config against it. A runtime config packet may be added later.
- Colors are RGB on the wire; the firmware handles color order.
