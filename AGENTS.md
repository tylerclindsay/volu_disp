# volu_disp — AGENTS.md

A volumetric LED display driven by music: hanging WS2811 strips form a 3D
volume, and host software generates animations from audio and streams them
to the display over Ethernet.

This file holds **whole-system facts** (hardware, geometry, protocol).
Software guidance for the host is in `gen_system/AGENTS.md`.

---

## Repo layout
```
AGENTS.md            this file
docs/                requirements, architecture, protocol spec, decisions/
config/displays/     display configs (geometry, nodes, strips, max FPS)
firmware/drive_node/ drive node firmware (PlatformIO, Arduino, FastLED)
pcb/                 KiCad projects (see Hardware)
gen_system/          host software: backend/ (Python), ui/ (React)
tools/               standalone test/dev tools (e.g. udp_rainbow_sender.py)
data/                gitignored: audio library, stem/analysis cache, animations
```

## Terminology
- **Drive node** (also called **main node**) — board with a WT32-ETH01
  (ESP32 + LAN8720 Ethernet), 74HCT245 level shifters, and two IDC
  connectors. Receives pixel data over UDP and drives the LEDs. One IP each.
  PCB: `pcb/volu-disp-node`. Firmware: `firmware/drive_node`.
- **Edge node** — board connected to a drive node by IDC ribbon cable, with
  four 74HCT595 shift registers (8 outputs each). Supplies power and data to
  **32 strips**. PCB: `pcb/volu-disp-node-edge`.
- **Strip interface** — small board at each strip's connection
  (12V, GND, data). PCB: `pcb/volu-disp-strip-interface`.
- **Strip** — one vertical hanging 12V WS2811 LED strip.
- **Pixel / voxel** — one LED; a point in the display's 3D volume.
- **Slice** — one vertical 2D plane of strips.
- **Host** — the PC running gen_system.

Note: PCB folder names predate the terminology (`volu-disp-node` is the drive
node, `volu-disp-node-edge` the edge node); they are kept to avoid renaming
KiCad projects. Firmware hostnames are `volu-disp-node-<NODE_ID>`.

## Hardware
- Topology: host PC → unmanaged Ethernet switch → N drive nodes → 2 edge
  nodes each → 32 strips each = up to **64 strips per drive node**.
- Data path: WT32 I2S outputs (serial data + clock) → 74HCT245 level shifters
  on the drive node → IDC ribbon cable → 74HCT595 shift registers on the edge
  node, each fanning one serial data line out to 8 strips. Strips update in
  parallel; WS2811 timing per strip is unchanged.
- LEDs: 12V WS2811, GRB order, driven by FastLED's ESP32 **I2S** driver.
- The firmware is currently a 2-strip × 10-LED test setup using plain per-pin
  FastLED outputs (`NUM_STRIPS`, `STRIP_LEDS[]` in
  `firmware/drive_node/src/main.cpp`); not yet moved to I2S or 64 strips.
- Drive nodes use static IPs `192.168.1.(100 + NODE_ID)`, OTA updates via
  espota, and blank their LEDs after `DATA_TIMEOUT_MS` (3 s) without valid
  packets.
- WS2811 timing is ≈ 30 µs/pixel per strip. A frame takes roughly
  `longest_strip × 30 µs` (+ reset), so at 120 FPS (8.33 ms) strips are capped
  at **~270 pixels**. Validate any frame rate against the longest strip.

## Display geometry
- **Size is not fixed.** A small prototype is being built now; a much larger
  display will follow. Node count, slices, strips, and pixels per strip all
  come from `config/displays/*.yaml` — never hardcoded.
- Strips hang vertically. Every other slice is offset **sideways by ½ strip
  spacing** and **vertically by ½ pixel spacing**, so nearest-neighbor
  distances are roughly equal throughout the volume.
- Current values (may change): strip spacing 50 mm, pixel spacing 50 mm →
  offsets 25 mm.
- Consequence: the display is a **set of 3D points with real coordinates**,
  not a dense `[x][y][z]` array.

## Wire protocol
Host → drive node over UDP, port 7777. Spec: `docs/protocol.md`; the firmware
(`firmware/drive_node/src/config.h`) must match it. Each drive node only
receives data for its own strips.

---

## Working conventions for agents
- Don't change the wire protocol unilaterally. Update `docs/protocol.md`,
  the firmware, and gen_system together.
- Don't hardcode geometry, node counts, strip lengths, or IPs; read them
  from `config/displays/`.
- Record decisions in `docs/decisions/` (numbered, one per file) and update
  this file when facts above change.
- Open hardware questions: spacing between slices; prototype dimensions;
  purpose of the 5th 74HCT595 on the edge node schematic; whether the
  protocol should pack multiple strips per packet; how multi-node sync
  tolerance will be measured.
