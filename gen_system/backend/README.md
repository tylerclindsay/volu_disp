# gen_system/backend

Python package `volu_gen` (`src/volu_gen/`). Implemented so far:
**`analysis/`** — stems, beat grid, sections, phrases, genre, mood
(`../../docs/decisions/0004-audio-analysis.md`, drops:
`0005-per-bar-drops.md`), and waveform peaks for the UI
(`0006-waveform-peaks.md`). **`animfile/`** — animation file read/write
(`0008-animation-file-format.md`). **`geometry/`** — display config loading,
pixel positions, geometry order and hash; **`mapping/`** — pixel → drive
node / strip / pixel and the config checks (`0013-geometry-order-and-mapping.md`).
**`output/`** — protocol v2, packetizer, UDP sender and a simulated node
(`../../docs/protocol.md`).
Still to come: generators,
playback, and the HTTP/WebSocket server for the UI.

## Setup (Windows, Python 3.11, NVIDIA GPU)
```
uv sync
```
Installs torch 2.11 cu128 from the PyTorch index and pins
`transformers==4.51.1` (SongFormer breaks on 5.x). Needs `ffmpeg` on PATH.
Models download to the Hugging Face / torch caches on first run (~4 GB).

## Analyze tracks
```
uv run python -m volu_gen.analysis <audio files or folders> [--check]
uv run python -m volu_gen.analysis --all          # every stored track
```
- Audio files are **imported**: copied to
  `../../data/tracks/<sha256[:16]>/source.<ext>`, keyed by content
  (re-importing the same file is a no-op). Results go next to it
  (`analysis.json`, `stems/`, `peaks.npz`). A track dir can be passed instead
  of an audio file. Finished stages are skipped; `--force stems,beats`
  recomputes; `--stages` runs a subset.
- `--check` writes `check/clicks.wav` (high click on downbeats, low on
  beats, chime on phrase starts, a second higher chime on drops) and Audacity label files (File → Import →
  Labels). Check any change to grid or phrase rules by ear with these.
- `--vram-limit-gb 8` emulates the 8 GB target card.
- `--stages peaks` (re)builds only the waveform peaks from stored stems
  (CPU, ~1–3 s/track); they are redone automatically when stems are newer.
- `--override BPM DOWNBEAT_S` sets a user grid on one analyzed track (time
  of any downbeat); `--clear-override` returns to the fit. Only the
  grid-dependent steps (phrases, per-bar energy) are re-run.

## Test patterns and fake nodes
```
uv run python ../../tools/fake_node.py ../../config/displays/prototype.yaml --base-port 9000
uv run python -m volu_gen.output ../../config/displays/prototype.yaml --loopback 9000 --pattern sweep
```
The first runs every drive node of the config as a fake node on
`127.0.0.1:9000+k` and prints per-node FPS, packet rate and problems
(bad, late, clipped, incomplete) each second. The second sends a test
pattern (`nodes`, `rainbow`, `sweep`) over protocol v2; without
`--loopback` it goes to the configured node IPs (`--bind` the display
network interface's IP on Windows). Real nodes need v2 firmware.

## Tests
```
uv run pytest
```
Unit tests cover the pure-numpy parts (grid fit, windowed sections,
phrases, mood normalization, waveform peaks, geometry, mapping, protocol test vectors, packetizer, loopback UDP) with synthetic inputs; no GPU or audio needed.
