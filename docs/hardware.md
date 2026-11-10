# Hardware, geometry, and mapping

Whole-system facts: terminology, the hardware, the display geometry, and
how drive nodes map onto the display. The wire protocol is in
`protocol.md`; host software in `../gen_system/AGENTS.md` and
`architecture.md`.

---

## Terminology
- **Drive node** (also called **main node**) — board with a WT32-ETH01
  (ESP32 + LAN8720 Ethernet), 74HCT245 level shifters, and two IDC
  connectors. Receives pixel data over UDP and drives the LEDs. One IP each.
  Each of its two 245s level-shifts the signals for one edge node.
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
- **Display FPS** is a host UI setting: **any whole number from 30 to 480,
  default 120** (`decisions/0010-display-fps.md`). Animations render
  at it and play only at it: after a change, every animation is unplayable
  until re-rendered. Don't assume 120.
- WS2811 timing is ≈ 30 µs/pixel per strip. A frame takes roughly
  `longest_strip × 30 µs` (+ reset), so strips are capped at **~1,100 /
  ~270 / ~65 pixels** at 30 / 120 / 480 FPS. Validate the chosen FPS
  against the longest strip.

## Display geometry
- **Size is not fixed.** A small prototype is being built now; a much larger
  display will follow. Node count, slices, strips, and pixels per strip all
  come from `config/displays/*.yaml` — never hardcoded.
- Strips hang vertically. Every other slice is offset **sideways by ½ strip
  spacing** and **vertically by ½ pixel spacing**, so nearest-neighbor
  distances are roughly equal throughout the volume.
- Current values (may change): strip spacing 50 mm, pixel spacing 50 mm,
  slice spacing 25 mm → offsets 25 mm. This is a body-centered cubic
  arrangement with a 50 mm cell: each pixel's 8 nearest neighbors are
  ≈ 43 mm away, the next 6 are 50 mm away, so density is visually even.
- Axes: x = across a slice (strip within slice), y = vertical (pixel along
  strip), z = depth (slice).
- Consequence: the display is a **set of 3D points with real coordinates**,
  not a dense `[x][y][z]` array.
- **Prototype:** 10 wide (x) × 12 tall (y) × 8 deep (z) = 80 strips,
  960 pixels. 80 strips > 64 per drive node, so it needs **2 drive nodes**.

## Drive node mapping
- Each drive node owns one configurable **section**: a rectangular box
  (prism or cube) of the display, given as index ranges on x, y, z.
- Each (x, z) column inside a section is one strip on that node. Sections
  must not overlap, must together cover the whole display, and each must hold
  ≤ 64 strips (and strips short enough for the display FPS).
- Defined per display in `config/displays/*.yaml`. Prototype: node 1 owns
  slices z 0–3, node 2 owns z 4–7 (40 strips each).
- **Pixel 0 is at the top of every strip** (y = 0 is the top).
- Strip numbering inside a node — planned default, wiring not final:
  slice by slice, then across (`strip = (z - z0) * width + (x - x0)`);
  strips 0–31 on the first edge node, 32–63 on the second.

## Open questions
- Final physical wiring order of strips within a drive node.
- How multi-node sync tolerance will be measured.
