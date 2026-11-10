# volu_disp — AGENTS.md

A volumetric LED display driven by music: hanging WS2811 strips form a 3D
volume, and host software generates animations from audio and streams them
to the display over Ethernet.

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
Details and the reasons behind them are in `docs/hardware.md`.
- Host → Ethernet switch → drive nodes (one IP each, ≤ 64 strips) → two
  edge nodes each (32 strips each) → vertical WS2811 strips.
- **Display size is not fixed:** geometry, nodes, strips, and IPs come from
  `config/displays/*.yaml`.
- The display is a **set of 3D points**, not a dense grid: every other
  slice is offset by ½ strip and ½ pixel spacing. Axes: x across a slice,
  y vertical (y = 0 and pixel 0 at the top), z depth.
- **Display FPS** is a host setting, any whole number from 30 to 480,
  default 120 (`docs/decisions/0010-display-fps.md`). Don't assume 120; the
  longest strip must fit the frame time (≈ 30 µs per pixel).

## Wire protocol
Host → drive node over UDP, port 7777. Spec: `docs/protocol.md`; the firmware
(`firmware/drive_node/src/config.h`) must match it. Each drive node only
receives data for its own strips.
- **v2** (current spec): versioned header with a frame number; PIXELS write
  into a flat per-node buffer (several strips per packet); one broadcast
  SYNC latches all nodes together; nodes double-buffer.
- Implemented by gen_system (`gen_system/backend/src/volu_gen/output/`),
  the firmware (drives the edge nodes' 595s over I2S, decision 0014; not
  yet run on hardware) and `tools/udp_rainbow_sender.py`;
  `tools/fake_node.py` simulates v2 nodes.

---

## Working conventions for agents
- Don't change the wire protocol unilaterally. Update `docs/protocol.md`,
  the firmware, and gen_system together.
- Don't hardcode geometry, node counts, strip lengths, or IPs; read them
  from `config/displays/`.
- Record decisions in `docs/decisions/` (numbered, one per file). When a
  hardware fact changes, update `docs/hardware.md` (and Key facts above).

---

If a bug is worth documenting, it's worth fixing!
