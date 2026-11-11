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
job queue; job dispatch to the worker, the engine link, the player
(Playback and Studio), supervision of the engine and the worker, file
cleanup, node detection and the WebSocket. **`ipc/`** — the messages
between the server, engine and worker, and their ZeroMQ sockets
(`../../docs/decisions/0007-ipc-zeromq.md`). **`worker/`** — the worker
process: `analyze`, `regrid` and `render` jobs, one at a time. Still to
come: the other methods and the engine.

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
Serves the UI API on `http://127.0.0.1:8770/api` (schema at `/docs`,
live state on the WebSocket `/api/ws`). It keeps its state in
`../../data/volu.db` (`--data` for another data dir). It starts the
worker and runs the job queue on it: an imported track is analyzed, then
gets its default render. It starts the engine too, once
`volu_gen.engine` exists; until then it logs that the engine is not
built, and the transport routes return 409. Ctrl+C stops the server and
its children. Tracks the CLI analyzed show as orphaned directories until
they are added through the server.
- `--connect-only`: start no engine or worker; connect to ones started
  by hand on the same ports (for example a worker in a debugger). The
  server then can't cancel a running analysis and deletes no leftover
  `*.tmp` files.
- `--no-ping`: no node detection (all drive nodes stay offline, so
  output stays disarmed unless `PUT /api/output/armed`).
- `--host`, `--port` (HTTP, default `127.0.0.1:8770`); `--engine-port`,
  `--preview-port`, `--worker-port` (IPC, default 8771–8773, always on
  localhost).

The server runs uvicorn on `asyncio.SelectorEventLoop`: `zmq.asyncio`
does not work on the Windows default (Proactor) loop
(`../../docs/decisions/0007-ipc-zeromq.md` → Amendment 2).

## Worker
```
uv run python -m volu_gen.worker --display ../../config/displays/prototype.yaml
```
The server starts it (`volu serve`). It can also run alone. It binds
`tcp://127.0.0.1:8773` and takes jobs from a DEALER on that port
(`volu_gen.ipc`). It exits 3 s after the last server disconnects
(`--server-timeout 0`: never). Analysis jobs use CUDA when available
(`--device` to choose).

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
The IPC, worker and server link tests (`test_server_links.py`: a real
worker with a fake analysis, a fake engine, the WebSocket) use real
ZeroMQ sockets on free localhost ports.
