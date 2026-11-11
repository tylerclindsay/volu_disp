# volu_disp — AGENTS.md

A volumetric LED display driven by music. Hanging WS2811 strips form a 3D
volume. Host software generates animations from audio and streams them to the
display over Ethernet.

- Terminology, hardware, display geometry, drive node mapping:
  `docs/hardware.md`.
- Host software guidance: `gen_system/AGENTS.md`.

---

## Repo layout
```
AGENTS.md            this file
docs/                hardware, requirements, architecture, generators, ui, protocol spec, decisions/
config/displays/     display configs (geometry, nodes, strips, default FPS)
firmware/drive_node/ drive node firmware (PlatformIO, Arduino, own I2S driver)
pcb/                 KiCad projects (names: docs/hardware.md → Terminology)
gen_system/          host software: backend/ (Python), ui/ (React)
tools/               standalone test/dev tools (e.g. udp_rainbow_sender.py)
data/                gitignored: SQLite DB, per-track data (source audio, stems, analysis, peaks, features, animations)
```

## Key facts
`docs/hardware.md` gives the details and the reasons for these facts.
- Data path: host → Ethernet switch → drive nodes → two edge nodes for each
  drive node → vertical WS2811 strips. Each drive node has one IP and ≤ 64
  strips. Each edge node has 32 strips.
- **Display size is not fixed.** Geometry, nodes, strips, and IPs come from
  `config/displays/*.yaml`.
- The display is a **set of 3D points**, not a dense grid. Every other slice
  is offset by ½ strip and ½ pixel spacing. Axes: x across a slice, y
  vertical (y = 0 and pixel 0 at the top), z depth.
- **Display FPS** is a host setting. It is any whole number from 30 to 480,
  and the default is 120 (`docs/decisions/0010-display-fps.md`). Do not
  assume 120. The longest strip must fit the frame time (≈ 30 µs per pixel).

## Wire protocol
The host sends data to the drive nodes over UDP, port 7777. The spec is
`docs/protocol.md`. The firmware (`firmware/drive_node/src/config.h`) must
match it. Each drive node receives data only for its own strips.
- **v2** (current spec):
  - The header has a version and a frame number.
  - PIXELS packets write into a flat buffer for each node. One packet can
    carry several strips.
  - One broadcast SYNC latches all nodes together.
  - Nodes double-buffer.
- These parts implement v2:
  - gen_system (`gen_system/backend/src/volu_gen/output/`)
  - the firmware, which drives the 595s of the edge nodes over I2S
    (decision 0014). The firmware has not run on hardware yet.
  - `tools/udp_rainbow_sender.py`

  `tools/fake_node.py` simulates v2 nodes.

---

## Working conventions for agents
- Do not change the wire protocol unilaterally. Update `docs/protocol.md`,
  the firmware, and gen_system together.
- Do not hardcode geometry, node counts, strip lengths, or IPs. Read them
  from `config/displays/`.
- Record decisions in `docs/decisions/` (numbered, one for each file).
- When a hardware fact changes, update `docs/hardware.md` and Key facts
  above.

---

If a bug is worth documenting, it is worth fixing!
