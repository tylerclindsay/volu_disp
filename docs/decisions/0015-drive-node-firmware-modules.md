# 0015 — Drive node firmware: split into modules

Date: 2026-11-10. Status: accepted; implemented in
`firmware/drive_node/src/`. A pure refactor: no change to behaviour,
`config.h`, the wire protocol, or output timing.

## Context
After the protocol v2 and edge node output rewrite (0002, 0014), the
firmware was two files: `main.cpp` (~330 lines) and `edge_out.cpp`
(~320). Several functions each did several jobs:

- `loop()`: packet receive, data timeout, edge power polling and the
  stats report, the last two with their own function-`static` state.
- `setup()`: banner, buffer allocation, output init, power pins, serial
  handoff, show task, Ethernet, OTA, UDP.
- `handlePacket()`: header checks, SYNC, the late-frame window, frame
  tracking and the pixel copy.
- `edge_out.cpp`: `edgeInit()` (DMA ring allocation plus ~30 lines of I2S
  registers), `fillPixel()` (idle area, constant slots and data slots in
  one), `edgeShow()` (frame prep plus starting the DMA).

The underlying tangle was shared state: frame buffers, receive state and
twelve stats counters were file globals touched from all over `main.cpp`.

## Decision
Each piece of state gets one owning module with a small header; `main.cpp`
is only setup order and the loop schedule.

| File | Owns | Interface |
|---|---|---|
| `log.cpp/.h` | serial on/off, UDP log target | `logBegin`, `logLine`, `logSerialOff`, `logUseUdp`, `logSetHost` |
| `frames.cpp/.h` | back/pending/front buffers, mutex, show task | `framesInit`, `framesStart`, `framesBack`, `framesLatch`, `framesBlank`, `framesSetChains` |
| `receiver.cpp/.h` | protocol v2 parsing, current frame state | `handlePacket`, `receiverReset`, `receiverFrame` |
| `edge_power.cpp/.h` | power status pins, last reading | `edgePowerInit`, `edgePowerPoll`, `edgePowerChains`, `edgePowerState` |
| `stats.cpp/.h` | all counters (one struct), report timer | `stats`, `statsPoll` |
| `edge_out.cpp/.h` | I2S1 output (unchanged interface) | as before |
| `main.cpp` | UDP socket, data timeout, setup order | `setup`, `loop` |

`loop()` reads as its schedule: receive packets, check the data timeout,
poll edge power, report stats. `setup()` is a list of named steps whose
order is kept and commented (serial must be released before the LED pins
are attached; the show task must exist before the first blank).

Dependencies point one way: `main` → `receiver` → `frames` → `edge_out`;
`edge_power`, `stats`, `log` are leaves that others call. `edge_power`
doesn't touch the frames: `main` passes its chain mask on, so the module
stays a plain input reader.

`edge_out.cpp` stays one file, with its big functions split
(`allocRing`/`configI2s`, `writeIdle`/`writeFixedSlots`/`writeData`,
`startTx`, `finishShow`). Everything the DMA interrupt calls must be in
IRAM (it runs while flash is busy, e.g. during OTA): the new helpers are
`always_inline` into the existing `IRAM_ATTR` functions, and keeping the
interrupt path in one file makes that easy to check.

## Alternatives considered
- **Only split functions within the two files:** shorter functions, but
  the shared globals would still tie every part of `main.cpp` together.
- **C++ classes per module:** there is one instance of each; free
  functions over file-`static` state match `edge_out.h` and add no
  ceremony.
- **Splitting `edge_out.cpp`:** would spread the IRAM interrupt path
  across files, where a call to a non-IRAM function is easy to miss.

## Checks
Build before and after; flash and RAM use should be about the same, and
`onDmaEof`/`fillBuf` must still link at IRAM addresses (0x4008xxxx).
