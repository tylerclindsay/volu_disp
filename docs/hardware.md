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
- LEDs: 12V WS2811, GRB order, driven by the firmware's own **I2S1
  parallel DMA** driver into the 595s (`firmware/drive_node/src/edge_out.cpp`,
  `decisions/0014-edge-node-output.md`): SRCLK 20 MHz, 3 slots × 8
  samples per WS2811 bit (1.2 µs), 28.8 µs per pixel. Written and
  simulated; not yet run on hardware (bring-up checklist in 0014).
- Drive node GPIOs: see **Drive node GPIO assignment** below.
- Strip layout per node: `NUM_STRIPS`, `LEDS_PER_STRIP` in
  `firmware/drive_node/src/config.h` (or `-D` build flags), default the
  40 × 12 bench (`config/displays/bench.yaml`).
- Firmware buffering: PIXELS write a back buffer; a latch copies it to a
  pending buffer at once; when the LEDs are free the show task swaps
  pending and front, and the DMA interrupt encodes front on the fly.
  Three buffers of `NUM_STRIPS (rounded up to 8) × LEDS_PER_STRIP × 3`
  bytes (≈ 159 KB at 64 × 276) plus a 13.8 KB DMA ring on the heap. The
  stats line reports the measured show time (`show max`) to check against
  the expected time printed at boot.
- Drive nodes use static IPs `192.168.1.(100 + NODE_ID)`, OTA updates via
  espota, and blank their LEDs after `DATA_TIMEOUT_MS` (3 s) without valid
  packets.
- **Display FPS** is a host UI setting: **any whole number from 30 to 480,
  default 120** (`decisions/0010-display-fps.md`). Animations render
  at it and play only at it: after a change, every animation is unplayable
  until re-rendered. Don't assume 120.
- WS2811 timing is ≈ 30 µs/pixel per strip (28.8 µs in the firmware). A
  frame takes roughly `longest_strip × 30 µs` (+ reset), so strips are
  capped at **~1,100 / ~270 / ~65 pixels** at 30 / 120 / 480 FPS.
  Validate the chosen FPS against the longest strip.

## Drive node GPIO assignment
Source of truth: `firmware/drive_node/src/config.h` (`PIN_*`, `ETH_*`).
Change a pin there and here together. Net names follow the drive node
schematic (`E<edge>_Q<register>SER` = serial data into one 595).

**LED outputs** (I2S1 → 74HCT245 → IDC → edge node 595s). Each data line
feeds one 595, which fans out to 8 strips (outputs A–H = `s % 8`):

| GPIO | Net         | Edge node | Strips (node-local) | Connectors |
|------|-------------|-----------|---------------------|------------|
| IO5  | E1_Q1SER    | E1        | 0–7                 | J1–J8      |
| IO12 | E1_Q2SER    | E1        | 8–15                | J9–J16     |
| IO14 | E1_Q3SER    | E1        | 16–23               | J17–J24    |
| IO15 | E1_Q4SER    | E1        | 24–31               | J25–J32    |
| IO33 | E2_Q1SER    | E2        | 32–39               | J1–J8      |
| IO32 | E2_Q2SER    | E2        | 40–47               | J9–J16     |
| IO3  | E2_Q3SER    | E2        | 48–55               | J17–J24    |
| IO1  | E2_Q4SER    | E2        | 56–63               | J25–J32    |
| IO4  | SRCLK       | both      | shift clock, shared (20 MHz) | |
| IO2  | RCLK        | both      | latch clock, shared  | |

The serial data lines of an edge node without 5V are held low.

**Edge node power status inputs** (active low; pulled up on the drive node
PCB). IO39, IO36 and IO35 are input-only pins; IO17 is a normal GPIO used
as an input:

| GPIO | Signal         | Low means                         | Unplugged reads |
|------|----------------|-----------------------------------|-----------------|
| IO39 | E1_5V_PRESENT  | E1's 5V (595 supply) is up        | high (off)      |
| IO36 | E2_5V_PRESENT  | E2's 5V is up                     | high (off)      |
| IO17 | E1_PWR_GOOD    | E1's 12V hot-swap is good         | high (not good) |
| IO35 | E2_PWR_GOOD    | E2's 12V hot-swap is good         | high (not good) |

`5V_PRESENT` gates output (`EDGE_POWER_CHECK`); `PWR_GOOD` is only
reported in the stats line.

**Ethernet** (WT32-ETH01 internal, LAN8720; don't reuse): IO23 MDC,
IO18 MDIO, IO16 PHY power, IO0 50 MHz RMII clock in, plus the fixed RMII
pins (IO19, 21, 22, 25, 26, 27).

**Gotchas**
- **UART0 (IO1 TX, IO3 RX) carries E2 Q4/Q3 data.** Serial output stops
  after the boot banner; logs and stats then go as UDP text to port 7778
  of the host that last sent a valid packet (`tools/node_stats.py`).
  Disconnect a USB-serial adapter's TX from IO3 while the node runs.
  `-DKEEP_SERIAL=1` keeps serial and leaves strips 48–63 undriven.
- **IO35, IO36, IO39 are input-only with no internal pull-ups.** On a bare
  WT32 (no drive node PCB) they float, so the stats line can report an
  edge node as `on` when nothing is connected.
- **Strapping pins in use:** IO12 (MTDI: must be low at reset, or the
  flash runs at 1.8 V and the module won't boot), IO2 (must be low or
  floating to enter download mode), IO15 (low at reset silences the ROM
  boot log), IO5 (E1 Q1 data; its strap only sets SDIO slave timing,
  which the drive node doesn't use, and drives a 74HCT245 input, which
  doesn't load it, so the internal pull-up keeps the default).
  Nothing on the drive node may pull IO12 high during reset.
- The 595s' outputs are random at power-up, so the firmware starts the LED
  output (and blanks) before bringing up Ethernet.

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
- Strip `s` plugs into edge node `s / 32`, connector `J(s % 32 + 1)`
  (595 `Q(s % 32 / 8 + 1)`, output A–H = `s % 8`); fixed by the firmware
  (decision 0014).

## Open questions
- Final physical wiring order of strips within a drive node.
- How multi-node sync tolerance will be measured.
