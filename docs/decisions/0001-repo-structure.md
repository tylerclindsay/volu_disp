# 0001 — Repo structure

Date: 2026-11-01. Status: accepted.

## Decision
- `volu_disp_edge/` → `firmware/drive_node/`. The firmware runs on the drive
  node; "edge" now means the shift-register board, so the old name was
  misleading.
- `scripts/` → `tools/`.
- `pcb/` folder names are kept as-is (renaming KiCad projects is error-prone).
- Whole-system facts live in the root `AGENTS.md`; host software guidance in
  `gen_system/AGENTS.md`.
- Shared display config lives in `config/displays/` at the repo root, since
  both gen_system and the firmware depend on it.
- Generated and user data (audio, stems, animations) lives in `data/`, which
  is gitignored.
