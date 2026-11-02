# Wire protocol (host → drive node)

UDP, port 7777, little-endian, one packet ≤ 1472 bytes (fits 1500 MTU).
Must match `firmware/drive_node/src/config.h`. Reference sender:
`tools/udp_rainbow_sender.py`.

| offset | size | field  | notes                                              |
|-------:|-----:|--------|----------------------------------------------------|
| 0      | 1    | magic  | `0xA7`                                             |
| 1      | 1    | flags  | bit0 `SHOW`: latch all strips after this packet    |
| 2      | 1    | strip  | 0 .. NUM_STRIPS-1                                  |
| 3      | 2    | start  | first pixel index                                  |
| 5      | 2    | count  | pixels in this packet (≤ 488)                      |
| 7      | 3·n  | pixels | R,G,B per pixel                                    |

A frame = all packets for one drive node, with `SHOW` set on the last one.
Strips longer than 488 pixels are split across packets using `start`.

## Known limitations
- No frame/sequence number, timestamp, or acknowledgement.
- No cross-node sync: each node latches independently on its own `SHOW`.
- One strip per packet: 64 strips × 120 FPS ≈ 7,700 small packets/s per
  drive node. Packing several strips per packet may be needed.

The final protocol is expected to be similar to this one, not necessarily
identical. Changes are made here, in the firmware, and in gen_system together.
