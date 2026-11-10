# 0014 — Edge node output: I2S1 parallel DMA into the 74HCT595s

Date: 2026-11-10. Status: accepted; implemented in
`firmware/drive_node/src/edge_out.cpp`. Simulated, not yet run on
hardware (see Bring-up).

## Context
A drive node reaches its 64 strips through two edge nodes, each with four
74HCT595 shift registers (`hardware.md`). The drive node has one serial
data line per 595 (8 in all), plus a shift clock (SRCLK) and a latch
clock (RCLK) shared by all eight. The firmware used plain per-pin FastLED
outputs, which can't drive shift registers; FastLED has no 595
"virtual pin" driver.

GPIOs (WT32-ETH01):

| Signal | GPIO | Signal | GPIO |
|---|---|---|---|
| E1_Q1SER | 17 | E2_Q1SER | 33 |
| E1_Q2SER | 12 | E2_Q2SER | 32 |
| E1_Q3SER | 14 | E2_Q3SER | 3 (U0RXD) |
| E1_Q4SER | 15 | E2_Q4SER | 1 (U0TXD) |
| SRCLK | 4 | RCLK | 2 |
| E1_5V_PRESENT | 39 (in) | E2_5V_PRESENT | 36 (in) |
| E1_PWR_GOOD | 5 (in) | E2_PWR_GOOD | 35 (in) |

## Decision

### Strip numbering
Strip `s` of a node is on edge node `s / 32` (E1, E2), 595 `(s % 32) / 8`
(Q1..Q4) and output `s % 8` (A..H), i.e. edge connector `J(s % 32 + 1)`.
The 8 serial lines ("chains") are `c = s / 8`. This fixes the planned
default in `hardware.md` → Drive node mapping.

### Waveform
I2S1 in LCD (parallel) mode, 32-bit samples, sample clock 80 MHz /
`I2S_CLK_DIV` = **20 MHz**. Each sample drives the 8 serial lines (sample
bits 16–23) and RCLK (bit 24); the I2S WS (LCD write) clock is SRCLK, so
every sample shifts one bit into all eight 595s at once.

- A WS2811 bit is **3 slots** (high, data, low); a slot is **8 samples**,
  one per 595 output. The first sample of a slot ends up on QH.
- RCLK is high during sample 0 of every slot: its rising edge latches the
  8 bits shifted in during the previous slot, so all 64 outputs change
  together once per slot (one slot late, which doesn't matter).
- At 20 MHz: slot 400 ns, bit 1.2 µs (833 kHz), T0H 0.4 µs, T1H 0.8 µs,
  **28.8 µs per pixel**. After the last pixel, latch-only (low) slots
  for the reset, `LED_RESET_US` = 50 µs, rounded up to whole DMA buffers.

A show takes `(ceil(leds / 2) + 1) × 57.6 µs` (8.0 ms at 276 pixels).
That is within decision 0013's `leds × 30 µs + 50 µs` model for strips
of ≥ 31 pixels. At the model's longest allowed strip, ≥ 49 µs of every
frame period is left for starting the next show (worst case ≈ 470 FPS).

### Pipeline
- The interrupt reads the frame in place from the node's `front` buffer
  and bit-transposes it into a ring of 3 DMA buffers (2 pixel rows each,
  13.8 KB) as each buffer drains. The high and low slots are constant and
  only rewritten when needed. Nothing is copied or encoded between shows;
  the show task just swaps `pending` and `front`. Memory: 3 frame buffers
  (back, pending, front; ≈ 159 KB at 64 × 276) plus the ring.
- The interrupt is in IRAM (it keeps running during flash writes, e.g.
  OTA) and runs on core 1. Estimated ~15–20 % of core 1 during a show;
  `dma late` in the stats counts refills that came too late.

### UART0
IO1/IO3 are E2 Q4/Q3 data, so the firmware prints its boot banner on
serial, then hands the pins to I2S. Logs and the stats line go as text
over UDP to port 7778 of the host that last sent a valid packet
(`tools/node_stats.py`). This is a debug channel, not part of the wire
protocol. `KEEP_SERIAL=1` (bench only) keeps serial and leaves strips
48–63 undriven. Keep a USB-serial adapter's TX off IO3 while the node
runs, since both would drive the pin.

### Edge node power
`5V_PRESENT` (low = the edge node's 595 supply is up) gates that edge
node's 4 serial lines (held low otherwise, to keep logic levels off an
unpowered 595; SRCLK/RCLK are shared and keep running). If neither edge
node is powered, no show runs. `PWR_GOOD` (low = 12V hot-swap good) is
reported only; the pull-up makes it read high (not good) when the edge
node is unplugged.
Changes are logged, and an edge node that powers up while no frames
arrive is blanked (595 outputs are random at power-up).

## Alternatives considered
- **FastLED / hpwit's I2SClocklessVirtualLedDriver:** FastLED (3.10) has
  no shift-register driver; hpwit's library is an outside dependency
  with its own buffering and timing choices. The protocol needs only one
  fixed chipset, so a ~300-line driver is simpler than adapting either.
- **esp_lcd i80 bus:** handles I2S LCD mode, but sends whole prebuilt
  transactions with gaps between them. A full frame is ~320 KB of
  samples, too large to prebuild.
- **4 slots per bit at 26.7 MHz** (T0H 0.3 µs, T1H 0.6 µs, closer to the
  datasheet): SRCLK above the 74HCT595's guaranteed 25 MHz over a ribbon
  cable. **3 slots at 16 MHz:** 1.5 µs bits and T0H 0.5 µs, too close
  to the WS2811's 0/1 threshold.
- **Encoding the frame between shows (show task) or copying it into the
  output:** 0.7–2 ms per frame at 64 × 276, which breaks 120 FPS at the
  longest strips.

## Bring-up (to verify on hardware)
- On a scope: SRCLK at 20 MHz, 8 cycles per RCLK pulse; SER stable at the
  SRCLK rising edge (flip `SRCLK_INVERT` if not).
- The stats line's `show max` should match `edgeShowUs()` (printed at
  boot); a mismatch means the I2S clock or WS behavior differs from this
  design.
- Lane mapping (sample bit 16 + n → line n) is taken from FastLED's
  bench-tested I2S1 driver in the same register setup.
- Heap at 64 × 276 (three 53 KB buffers next to Ethernet and lwIP).
