# gen_system — AGENTS.md

Software for the volumetric display audiovisual experience: a React UI plus a
backend that (1) generates animations from source audio and (2) plays that
audio back while streaming pixel data to the display over Ethernet, in sync.

> **Status: pre-architecture.** Requirements and architecture are not final.
> Treat everything under "Recommended approach" as guidance, not decisions.
> When a requirements/architecture doc lands (planned: `docs/requirements.md`,
> `docs/architecture.md`), it overrides this file — update this file to match.
> Do not scaffold large structures or pick frameworks beyond what a task needs
> until those docs exist.

---

## Facts about the system

### Repo context (`volu_disp/`)
- `gen_system/` — this project (host software).
- `volu_disp_edge/` — ESP32 firmware for the LED receiver nodes (PlatformIO,
  Arduino framework, FastLED). Board: WT32-ETH01 (ESP32 + LAN8720 Ethernet).
- `scripts/udp_rainbow_sender.py` — reference sender for the wire protocol.
  Good starting point for test tools.
- `pcb/` — KiCad hardware: `volu-disp-node`, `volu-disp-node-edge`,
  `volu-disp-strip-interface`. A "main node" board is still being designed;
  its role in the data path is not yet defined.

### Display hardware (as currently built)
- The display is made of addressable LED strips (WS2811, GRB order) driven by
  multiple edge nodes. Each node drives several strips (`NUM_STRIPS`,
  `STRIP_LEDS[]` in `volu_disp_edge/src/main.cpp`; currently a 2×10 test setup —
  the real geometry and voxel count are not yet fixed).
- Nodes are on a wired Ethernet LAN with static IPs `192.168.1.(100 + NODE_ID)`,
  hostname `volu-disp-node-<NODE_ID>`, OTA firmware updates via espota.
- Nodes blank their LEDs after `DATA_TIMEOUT_MS` (3 s) with no valid packets.
- LED refresh time grows with strip length (WS2811 ≈ 30 µs/pixel), so maximum
  frame rate depends on the longest strip per node. Account for this when
  choosing frame rate.

### Wire protocol (source of truth: `volu_disp_edge/src/config.h`)
UDP, port 7777, little-endian, one packet ≤ 1472 bytes (fits 1500 MTU):

| offset | size | field  | notes                                              |
|-------:|-----:|--------|----------------------------------------------------|
| 0      | 1    | magic  | `0xA7`                                             |
| 1      | 1    | flags  | bit0 `SHOW`: latch all strips after this packet    |
| 2      | 1    | strip  | 0 .. NUM_STRIPS-1                                  |
| 3      | 2    | start  | first pixel index                                  |
| 5      | 2    | count  | pixels in this packet (≤ 488)                      |
| 7      | 3·n  | pixels | R,G,B per pixel                                    |

A frame = all packets for a node, with `SHOW` set on the last one.
Current limitations to keep in mind: no frame/sequence number, no timestamp,
no cross-node sync mechanism (each node latches independently on its own
`SHOW`), no acknowledgement. If gen_system needs any of these, it is a
protocol change that must be made in the firmware and here together.

---

## Recommended approach

These are suggestions for how to build this well. Confirm against the
architecture doc once it exists.

### 1. Separate offline generation from real-time playback
- **Generation** (audio → animation) is offline/batch: it can be slow, use
  heavy analysis (beat/onset tracking, spectral features, stems), and be
  re-run. Its output is a deterministic, pre-rendered **show file**: audio
  reference + frame rate + voxel frames (+ metadata, generator version/params).
- **Playback** only reads a show file and does two things: play audio and send
  frames. No analysis or rendering in the hot path. This keeps timing simple
  and makes shows reproducible.
- Live/reactive modes (if wanted later) should still produce frames through the
  same frame → output pipeline.

### 2. The audio clock is the master clock
- Derive the current frame index from the audio device's playback position
  (samples played), not from wall-clock timers or `sleep` loops. This prevents
  drift over long tracks.
- Add a configurable latency offset (audio output latency vs. network + LED
  latch latency) and a way to calibrate it by eye/ear (e.g. a flash + click
  test pattern).
- Send all nodes' packets for a frame back-to-back and set `SHOW` last per
  node, so nodes latch as close together as possible.

### 3. Keep the UI out of the timing path
- The playback engine should run as its own process/service; the React UI is
  a client (HTTP for control, WebSocket for status/preview). A UI freeze,
  reload, or crash must never stall audio or frames.
- The UI shows state the engine reports; it does not compute timing itself.

### 4. Layered data model
Keep these as distinct, testable layers:
1. **Voxel space** — logical 3D grid/point set with coordinates; what
   generators and the UI preview work in.
2. **Mapping** — config file mapping each voxel → (node, strip, pixel index),
   plus per-node IP and strip lengths. This must match the firmware's
   `STRIP_LEDS` layout; generate one from the other if possible.
3. **Output** — packetizer (mapping + frame → UDP packets) and sender.
Generators should never know about nodes, strips, or packets.

### 5. Hardware-free development
- Build a **virtual display**: a 3D preview in the UI (e.g. three.js /
  react-three-fiber) driven by the same frames/mapping.
- Build a **fake node**: a UDP listener that decodes the protocol and validates
  packets (and can feed the preview). Most work should be testable without
  the real display.
- Keep `scripts/udp_rainbow_sender.py`-style test patterns (solid colors,
  per-node/per-strip IDs, axis sweeps) for verifying mapping on real hardware.

### 6. Single source of truth for shared formats
- The UDP protocol, show-file format, and mapping schema should each be
  defined once (spec + constants module) and covered by tests with fixed
  test vectors (known frame → exact expected bytes).
- Version the show file and mapping formats from day one.

### 7. Testing priorities
- Packetizer: byte-exact tests against the table above, including strips
  longer than 488 pixels and multi-packet frames.
- Mapping: every physical pixel mapped exactly once; validate against strip
  lengths.
- Timing: a headless playback test that measures frame-send jitter and
  drift against the audio clock over a full-length track.
- Generators: deterministic output for a given audio + params (snapshot tests).

### 8. Tech stack (tentative — not decided)
- Frontend: React + TypeScript (Vite). 3D preview with react-three-fiber.
- Generation: Python fits well (numpy, librosa or similar for audio analysis).
- Playback: start in the same language as the backend; move the frame sender
  to a compiled language only if measured jitter requires it.
- Dev machine is Windows 11; keep scripts and paths cross-platform.

---

## Working conventions for agents
- Don't change the wire protocol unilaterally — it lives in the firmware.
  Propose protocol changes and update both sides together.
- Don't hardcode display geometry, node counts, or IPs in code; read them from
  the mapping/config.
- Prefer small, runnable increments (e.g. "fake node + packetizer + test"
  before "full UI").
- Record architectural decisions in `docs/` as they are made, and update this
  file when facts above change.
- Open questions to resolve in the requirements session: final voxel
  geometry/count, target frame rate, number of nodes, role of the main node
  board, show-file format, whether live/reactive modes are in scope, audio
  output device/hardware, and how multi-node sync tolerance will be measured.
