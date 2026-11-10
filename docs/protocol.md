# Wire protocol (host → drive node)

**Current version: v2.** The host implements it in `volu_gen/output/`
(`protocol.py` holds the constants and byte layout; `nodesim.py` the
receive rules below). The firmware and `tools/udp_rainbow_sender.py` still
speak v1 (see the end of this file).
Decision record: `decisions/0002-protocol-v2.md`.

## Transport
- UDP, port 7777, all multi-byte fields little-endian.
- One packet ≤ 1472 bytes of UDP payload (fits a 1500-byte MTU).
- PIXELS packets are unicast to each drive node's IP. SYNC packets are
  broadcast to the subnet (`192.168.1.255`). On Windows, bind the sending
  socket to the display network interface and enable `SO_BROADCAST`.

## Common header (8 bytes)

| offset | size | field   | notes                                          |
|-------:|-----:|---------|------------------------------------------------|
| 0      | 1    | magic   | `0xA7`                                         |
| 1      | 1    | version | `2`                                            |
| 2      | 1    | type    | `1` PIXELS, `2` SYNC, `3` reserved (status)    |
| 3      | 1    | flags   | bit0 `SHOW_NOW` (PIXELS only); others must be 0 |
| 4      | 4    | frame   | u32 frame sequence number (see Frames)         |

Nodes drop packets with the wrong magic or version, and ignore unknown types.

## PIXELS (type 1)

| offset | size | field  | notes                                           |
|-------:|-----:|--------|-------------------------------------------------|
| 8      | 2    | offset | first pixel index in the node's pixel buffer    |
| 10     | 2    | count  | pixels in this packet (≤ 486)                   |
| 12     | 3·n  | pixels | R,G,B per pixel                                 |

- Max 486 pixels per packet: `(1472 - 12) / 3`.
- **Node pixel buffer:** all of a node's strips laid out end to end. Pixel
  `p` of strip `s` is at `offset = s × LEDS_PER_STRIP + p`. Pixel 0 is the top
  of the strip. All strips on a node have the same length (a node's section
  is a box), so `LEDS_PER_STRIP` = the section's height. The firmware's
  compiled `NUM_STRIPS` and `LEDS_PER_STRIP` must match the display config.
  The host can't check this until node status (type 3) exists; until then
  it is checked by hand when flashing.
- A packet may span strip boundaries. The host should fill packets fully
  (fewest packets per frame).
- Colors are always RGB on the wire; the firmware converts to the strips'
  color order (GRB).
- Pixels beyond the end of the buffer are ignored (the packet is clipped).
- `SHOW_NOW`: latch immediately after this packet, without waiting for SYNC.
  For test tools and single-node use.

## SYNC (type 2)
Header only (8 bytes). Tells every node to show frame `frame`. Sent once per
frame, broadcast, **after** all PIXELS packets for that frame have been sent
to all nodes. One broadcast packet latches all nodes within tens of µs of
each other.

## Frames
- `frame` is a sender sequence counter: it increments by 1 for every frame
  sent and is independent of the animation frame index (so seeking or
  changing FPS doesn't make it jump). It does not wrap in practice (u32 at
  480 FPS ≈ 103 days).
- Each node keeps two buffers: a **back buffer** that PIXELS write into and a
  **front buffer** being clocked out to the LEDs.
- On PIXELS with frame `F`:
  - `F` is 1–8 frames older than the newest frame seen → late, dropped.
  - Otherwise `F` becomes the current frame (this also covers a host
    restart, where the counter starts over) and the pixels are written.
- On SYNC with frame `F` (or a PIXELS packet with `SHOW_NOW`): copy back →
  front and show. If a show is still in progress, the latch waits for it to
  finish; only the newest pending latch is kept.
- **Missing packets:** the frame is shown anyway. Regions with no new data
  keep the previous frame's pixels (the back buffer is never cleared between
  frames). The node counts the frame as incomplete in its stats.
- If no valid packet arrives for `DATA_TIMEOUT_MS` (3 s), the node blanks.

## Timing constraints
- The frame rate is the host's **display FPS**: any whole number from 30
  to 480 (default 120), set in the UI (`decisions/0010-display-fps.md`).
  The protocol itself doesn't fix a rate.
- A show takes ≈ `LEDS_PER_STRIP × 30 µs` + reset. The frame period must be
  longer than that: ≤ ~1,100 / ~270 / ~65 pixels per strip at
  30 / 120 / 480 FPS.
- Per-node bandwidth: `NUM_STRIPS × LEDS_PER_STRIP × 3 × FPS` bytes/s.
  Since strip length caps pixels × FPS, the worst case is ≈ 50 Mbit/s at
  any rate (e.g. 64 × 270 at 120 FPS) of the WT32's 100 Mbit link — test
  early on hardware.
- Packet rate does grow with the rate: at least one PIXELS packet per node
  plus one SYNC per frame, so ≥ 960 packets/s per node at 480 FPS. Test
  the node's receive loop at the highest rate the display will use.

## Reserved
- Type `3`: status/telemetry from nodes (frame counts, incomplete frames,
  drops). Not specified yet.

---

## Appendix: v1 (what the firmware implements today)
Single 7-byte header, no version or frame number, one strip per packet:

| offset | size | field  | notes                                           |
|-------:|-----:|--------|-------------------------------------------------|
| 0      | 1    | magic  | `0xA7`                                          |
| 1      | 1    | flags  | bit0 `SHOW`: latch all strips after this packet |
| 2      | 1    | strip  | 0 .. NUM_STRIPS-1                               |
| 3      | 2    | start  | first pixel index                               |
| 5      | 2    | count  | pixels in this packet (≤ 488)                   |
| 7      | 3·n  | pixels | R,G,B per pixel                                 |

Remove this appendix once the firmware and tools are on v2.
