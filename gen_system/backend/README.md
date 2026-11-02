# gen_system/backend

Python package `volu_gen` (`src/volu_gen/`). Implemented so far:
**`analysis/`** — stems, beat grid, sections, phrases, genre, mood
(`../../docs/decisions/0004-audio-analysis.md`, drops:
`0005-per-bar-drops.md`). Still to come: geometry,
generators, animation file I/O, output (mapping, packetizer, UDP sender),
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
```
- Results are cached in `../../data/cache/<sha256[:16]>/` (`analysis.json`
  and `stems/`), keyed by audio content. Finished stages are skipped;
  `--force stems,beats` recomputes; `--stages` runs a subset.
- `--check` writes `check/clicks.wav` (high click on downbeats, low on
  beats, chime on phrase starts, a second higher chime on drops) and Audacity label files (File → Import →
  Labels). Check any change to grid or phrase rules by ear with these.
- `--vram-limit-gb 8` emulates the 8 GB target card.
- `--override BPM DOWNBEAT_S` sets a user grid on one analyzed track (time
  of any downbeat); `--clear-override` returns to the fit. Only the
  grid-dependent steps (phrases, per-bar energy) are re-run.

## Tests
```
uv run pytest
```
Unit tests cover the pure-numpy parts (grid fit, windowed sections,
phrases, mood normalization) with synthetic inputs; no GPU or audio needed.
