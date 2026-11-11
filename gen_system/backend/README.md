# gen_system/backend

Python package `volu_gen` (`src/volu_gen/`). Implemented so far:
**`analysis/`** — stems, beat grid, sections, phrases, genre, mood
(`../../docs/decisions/0004-audio-analysis.md`, drops:
`0005-per-bar-drops.md`), waveform peaks for the UI
(`0006-waveform-peaks.md`) and stem features for generators
(`0012-stem-features.md`). **`animfile/`** — animation file read/write
(`0008-animation-file-format.md`). **`geometry/`** — display config loading,
pixel positions, geometry order and hash; **`mapping/`** — pixel → drive
node / strip / pixel and the config checks (`0013-geometry-order-and-mapping.md`).
**`output/`** — protocol v2, packetizer, UDP sender and a simulated node
(`../../docs/protocol.md`).
**`generators/`** — `Music` (analysis, features and audio as
queries over time), `RenderContext`, the method interface and registry,
the plan model, the director (`make_plan`: track → segments, methods,
params, transitions, palette; `../../docs/generators.md`) and the render
job (`render_anim`: plan → blended, quantized frames → `.anim`;
`../../docs/decisions/0018-render-output.md`), and the first methods:
`geometry`, `raindrops`, `strobe`, `galaxy`
(`../../docs/decisions/0019-first-methods.md`).
**`server/`** — the API server for the UI (`../../docs/architecture.md` →
API server, HTTP API): SQLite DB, settings, track import and removal,
analysis and peaks, planning, versions, presets, the play queue and the
job queue. Still to come: the other methods, the worker and engine
processes (and the server's link to them), and the WebSocket.

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
  (`analysis.json`, `stems/`, `peaks.npz`, `features.npz`). A track dir can be passed instead
  of an audio file. Finished stages are skipped; `--force stems,beats`
  recomputes; `--stages` runs a subset.
- `--check` writes `check/clicks.wav` (high click on downbeats, low on
  beats, chime on phrase starts, a second higher chime on drops) and Audacity label files (File → Import →
  Labels). Check any change to grid or phrase rules by ear with these.
  With features built it also writes `check/onsets_<class>.wav` per onset
  class (kick, hat): the drums stem with a click per onset, louder for stronger ones.
- `--vram-limit-gb 8` emulates the 8 GB target card.
- `--stages peaks` (re)builds only the waveform peaks from stored stems
  (CPU, ~1–3 s/track); they are redone automatically when stems are newer.
- `--stages features` (re)builds only the stem features (envelopes,
  spectra, stereo width, kick/hat onsets; CPU, ~16 s for a 6.6-min track), redone the
  same way.
- `--override BPM DOWNBEAT_S` sets a user grid on one analyzed track (time
  of any downbeat); `--clear-override` returns to the fit. Only the
  grid-dependent steps (phrases, per-bar energy) are re-run.

## API server
```
uv run volu serve --display ../../config/displays/prototype.yaml
```
Serves the UI API on `http://127.0.0.1:8770/api` (schema at `/docs`). It
keeps its state in `../../data/volu.db` (`--data` for another data dir).
Imports queue `analyze` jobs, but nothing runs them until the worker
exists, so a track added through the server stays `pending` and can't be
planned yet. Tracks the CLI analyzed show as orphaned directories until
they are added through the server.

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
phrases, mood normalization, waveform peaks, stem features, geometry, mapping, protocol test vectors, packetizer, loopback UDP) with synthetic inputs; no GPU or audio needed.
