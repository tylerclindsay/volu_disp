# 0004 — Audio analysis pipeline

Date: 2026-11-01. Status: accepted (not yet implemented in gen_system).

## Context
Generators need more than a BPM: a beat grid they can trust to the
millisecond, where sections and phrases start, when drums/bass come and go,
and a rough genre and mood. Candidate models were tested with
`tools/analysis_spike/spike.py` on 10 electronic tracks from the library
(house, trance, hardstyle, DnB, UK garage, experimental), checked by ear
with click tracks and Audacity label files. Results: `data/analysis_spike/`
(gitignored), `summary.md` there. Target: an 8 GB NVIDIA card on Windows.

## Decision

### Pipeline
Stages run in this order; each loads its model, processes, and unloads, so
peak VRAM is one model's worth.

| Stage    | Tool                                       | Used for                               |
|----------|--------------------------------------------|----------------------------------------|
| stems    | demucs `htdemucs`                          | drums / bass / other / vocals          |
| beats    | Beat This! (`final0`, no DBN)              | per-frame beat + downbeat probabilities |
| grid     | our fit (below)                            | tempo, beats, downbeats                |
| sections | SongFormer, 150 s windows, 60 s overlap    | section **boundaries only**            |
| genre    | MAEST `discogs-maest-30s-pw-129e`          | top Discogs styles (30 s windows, averaged) |
| mood     | CLAP `laion/larger_clap_general`           | weak mood hint                         |
| phrases  | rules on per-bar stem energy (no model)    | phrases, drop/buildup/breakdown, events |

- **SongFormer labels are ignored.** Its pop labels (verse/chorus/bridge)
  mean nothing for EDM and change from run to run; its boundaries are good.
- **SongFormer runs in windows** because its memory grows with input length
  (a whole 3-min track needs ≈ 7 GB; 4.8 min thrashed a 16 GB card). Each
  window owns the middle of its overlaps, and segments cut only by an
  ownership boundary are rejoined. On Mat Zo - Easy (6.6 min) this matched
  the whole-track boundaries 26/26.
- **Genre:** MAEST's top style was plausible on all 10 tracks (e.g.
  Hardstyle 0.76, Drum n Bass 0.81, UK Garage/Bassline for Shift K3y).
- **Mood** is a hint, not a fact. CLAP cosine to "`<word>` music" for 12
  words; some words score high on everything, so each word is z-scored
  across the whole library. Store the raw cosines per track; compute z
  across the library (it shifts as tracks are added).

### Beat grid
One constant tempo and one continuous grid per track:
1. Fit tempo + phase to Beat This's **beat probabilities** over the whole
   track (search ±8% around the median picked-beat interval — picked beats
   sit on 20 ms frames, so that estimate can be ~6% off).
2. If the fitted tempo is within **0.1 BPM** of a whole number, snap to it.
3. Refit the phase at the final tempo.
4. Pick the downbeat (one of 4 beat phases, 4/4 assumed) from the
   **downbeat probabilities** at the grid points.
5. No phase jumps anywhere in the track.

Why: electronic tracks are made at a fixed DAW tempo, and Beat This's
timing shifts by tens of ms with the sound. On Hard Driver - Spark Inside
the fit said **160.085 BPM**; the truth is **160.000**. 0.085 BPM builds up
to ~60 ms by the end of the track, which was clearly audible on the click
track. Snapping fixed it (A/B/C comparison by ear). Picked beats alone are
worse: they drop out in beatless breakdowns and flip to half time in sparse
parts.

### Phrases and events
- Section starts are snapped to the nearest downbeat; phrases are **8 bars
  counted from each section start** (a shorter phrase fills the gap before
  the next section). Snapping to a global 4-bar grid was tried and moved a
  real drop that starts 2 bars off that grid.
- Drums/bass in and out (stem above −18 dB of its loud level, held ≥ 2 bars)
  are **point events**, not phrase cuts.
- Tags per phrase: `breakdown` (drums on < 25% of bars), `drop` (mix jumps
  ≥ 4 dB into the phrase with drums and bass on, not right after another
  drop), `buildup` (mix rises ≥ 0.35 dB/bar). Thresholds are tuned by ear;
  they are constants in `spike.py`.
- Phrasing and clicks confirmed by ear on Devin Wild - When The Book Is
  Closed.

### Cost
About 10–15 s per 3-min track on the dev PC; stems (5.8 s, timed on one
track) and SongFormer (~1 s per minute of audio) take most of it. Peak
VRAM ≤ 6.7 GB on 9 of 10 tracks and 7.7 GB on Skrillex - Kora, all under
an 8 GB cap. Models download ~4 GB on first run.

### Environment
- Python 3.11, torch 2.11 + torchaudio 2.11 (cu128), Windows 11.
- `transformers` **pinned to 4.51.1**: SongFormer's remote code fails to
  load on 5.x (meta-device init). MAEST and CLAP work on 4.51.1.
- Exact package list: `tools/analysis_spike/requirements.txt`.

## Known gaps (fix in the backend)
- **Drops are only tagged at phrase starts**, so a drop that lands off a
  phrase start is missed. Detect drops per bar, and make a drop start a new
  phrase.
- **Grid accept/reject is fragile.** It uses the median Beat This
  probability at grid points (≥ 0.6); Hard Driver's correct grid scores
  exactly 0.60. Needs a sturdier check.
- **Variable-tempo tracks** (Skrillex - Kora) fall back to Beat This's raw
  beats with `tempo: variable`. Don't chase these further.
- **Studio needs a BPM / grid-offset override** for when the fit is wrong;
  an override replaces the fitted grid and re-runs only the stages that
  depend on it (phrases, per-bar energy).

## Rejected
- `laion/larger_clap_music` — no spread between mood words in transformers
  (every word scores about the same).
- SongFormer on the whole track — memory (above).
- allin1 (all-in-one beats + sections) — doesn't build on Windows.
- Essentia — no Windows wheels.
- Grid fit to the steadiest run of beats only — locks onto one part of the
  track and drifts elsewhere; the whole-track fit is better.
- Grids with phase jumps between sections — produced half-beat errors where
  the grid should have stayed continuous.

## analysis.json (backend `analysis/` output)
One file per track in the analysis cache, keyed by audio hash. Outline:
```jsonc
{
  "version": 1,
  "source": { "sha256": "…", "duration": 184.2 },
  "models": { "stems": "htdemucs", "beats": "beat_this final0", … },
  "tempo": {
    "mode": "grid",              // "grid" | "variable" (raw beats, no grid)
    "bpm": 160.0, "fitted_bpm": 160.085,
    "offset": 0.139,             // first grid beat, s
    "meter": 4, "downbeat_phase": 2, "fit_score": 0.60,
    "override": null,            // { "bpm", "offset" } set from Studio
    "beats": [0.139, 0.514, …], "downbeats": [0.889, …]
  },
  "sections": [ { "start": 0.0, "end": 14.28, "start_bar": 1 }, … ],
  "phrases": [
    { "start": 0.889, "end": 12.889, "start_bar": 1, "bars": 8,
      "section": 0, "tags": ["breakdown"] }, …
  ],
  "events": [ { "bar": 9, "time": 12.889, "event": "bass_out" }, … ],
  "bars": {                      // one value per bar, dB re. the stem's loud level
    "energy_db": { "mix": […], "drums": […], "bass": […],
                   "other": […], "vocals": […] }
  },
  "genre": { "model": "discogs-maest-30s-pw-129e",
             "top": [ { "label": "Electronic---Hardstyle", "score": 0.76 }, … ] },
  "mood": { "model": "laion/larger_clap_general",
            "cos": { "energetic": 0.31, … } }   // z-scores computed library-wide
}
```
