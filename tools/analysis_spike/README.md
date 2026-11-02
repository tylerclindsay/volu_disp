# Analysis spike

Throwaway evaluation of audio-analysis models for gen_system, run on real
tracks before choosing them for the architecture. Not part of gen_system.

| Stage      | Model                                    | Output                                   |
|------------|------------------------------------------|------------------------------------------|
| `stems`    | demucs `htdemucs`                        | drums / bass / other / vocals            |
| `beats`    | Beat This! (`final0`)                    | beats, downbeats, BPM, meter             |
| `sections` | SongFormer                               | labeled sections (intro, verse, chorus…) |
| `genre`    | MAEST `discogs-maest-30s-pw-129e`        | top Discogs styles                       |
| `mood`     | CLAP `laion/larger_clap_general`         | descriptor ranking, track + per section  |
| `phrases`  | rules on stem energy (no model)          | phrase grid, drop/buildup/breakdown tags |

## Setup (Windows, Python 3.11, NVIDIA GPU)
```
uv venv -p 3.11 .venv
uv pip install -p .venv torch==2.11.0 torchaudio==2.11.0 --index-url https://download.pytorch.org/whl/cu128
uv pip install -p .venv -r requirements.txt
```
Needs `ffmpeg` on PATH. Models download to the Hugging Face / torch caches
on first run (~4 GB total).

## Run
```
.venv\Scripts\python spike.py D:\music\some_folder track.mp3
```
- Results go to `../../data/analysis_spike/` (gitignored); `--out` changes it.
- `--stages beats,sections` runs a subset; stems are cached per track.
- VRAM is capped at 8 GB by default (`--vram-limit-gb`) to match the target
  card; `summary.md` reports time and peak VRAM per stage and track.

## Checking results by ear
Per track folder:
- `check_clicks.wav` — the mix with clicks: high on downbeats, low on beats,
  a long chime at each phrase start.
- `labels_*.txt` — Audacity label tracks (File → Import → Labels) for
  downbeats, sections, and phrases, to line up against the waveform.
- `analysis.json` — everything, including per-bar energy per stem.

Phrase rule thresholds are constants at the top of `spike.py`.
