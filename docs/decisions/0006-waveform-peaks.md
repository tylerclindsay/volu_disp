# 0006 — Waveform peaks for stem lanes

Date: 2026-11-07. Status: accepted; implemented in
`gen_system/backend/src/volu_gen/analysis/peaks.py` (stage `peaks`).

## Context
The Player and Studio draw the mix and the four stems as lanes on a custom
canvas (`../ui.md`, `0003-ui-design.md`) from peaks the backend computes.
The Player shows a whole track; Studio zooms in to the beat grid. Lanes must
share one scale so stems can be compared. Peaks come from cached audio only:
no model runs, and demucs never re-runs for them.

## Decision

### What a peak is
- Per time bucket, per lane: **min, max, rms**, each over both stereo
  channels together (a channel that is louder on one side is never
  under-reported). min/max draw the outline and match the raw waveform at
  deep zoom; rms draws the denser body inside it.
- Lanes: `mix, vocals, drums, bass, other`.
- **Scale: linear sample value** as int16, `round(x · 32767)`, clipped to
  ±1. All lanes share this absolute scale (stems sum to the mix). The UI may
  convert to dB at draw time; int16 resolves ≈ −90 dB, so a dB view stays
  smooth.

### Resolution and levels
- Level 0: buckets of **147 samples at 44.1 kHz = 300 buckets/s**
  (3.33 ms). 147 divides 44100, so bucket k starts at exactly
  `k / rate` (`k · bucket_k / sample_rate`).
- Level k has bucket `147 · 2^k` samples (rate `300 / 2^k`), built from
  level k−1: min of mins, max of maxes, rms from the sample-weighted mean of
  squares. Levels stop at the last one with rate ≥ 1/s: **9 levels**
  (300 … 1.17/s).
- Bucket count per level is `ceil(n_samples / bucket_k)`; the last bucket
  may be partial and is computed from the samples it has.
- The UI picks the coarsest level with ≥ 1 bucket per pixel. A whole 396 s
  track on ~1500 px uses level 6 (4.7/s, ~1.9k buckets).

### Size (6.6-min track, 396 s)
Level 0: 118,800 buckets × 5 lanes × 3 values × 2 B ≈ 3.6 MB; all levels
≈ 7.1 MB raw, stored compressed (real sizes in Results).

### Storage
- `<data>/cache/<sha256[:16]>/peaks.npz` (`np.savez_compressed`), next to
  `analysis.json` and `stems/`. Not in analysis.json, which stays small and
  **unchanged** (its `version` is not bumped).
- Arrays: `level0` … `level8`, each int16 shaped `(lanes, 3, n_k)` with
  the middle axis `[min, max, rms]`; metadata `version`
  (`PEAKS_VERSION`, now 1), `sample_rate`, `bucket` (level-0 samples, 147),
  `n_samples`, `lanes`.
- Read with `read_peaks()` (`analysis/peaks.py`). The future API serves one
  level as raw little-endian int16 bytes plus the metadata as JSON; the UI
  wraps the bytes in an `Int16Array` with no parsing.
- A format change bumps `PEAKS_VERSION`; files with another version are
  recomputed.
- **Sample rate is the decode's, not the source file's.** Peaks are made
  from the 44.1 kHz decode (`BASE_SR`; `load_audio` resamples, stems are
  saved at it), so `sample_rate` is always 44100 and `n_samples` counts
  decoded samples. Readers should work in seconds: bucket k of level L
  starts at `k · bucket · 2^L / sample_rate`. That matches the original
  file within one decoded sample, and matches analysis.json times. Anything
  that maps seconds to samples of the original file (e.g. playback of a
  48 kHz file) must use that file's own rate. Changing `BASE_SR` needs a
  bucket size that divides it, plus a `PEAKS_VERSION` bump.

### Pipeline stage
- Stage `peaks`, CPU only, run after the model stages (like `phrases`). It
  writes nothing to analysis.json, including `models`.
- Re-runs when: forced (`--force peaks`); `peaks.npz` is missing or has
  another `version`; or any stem file is newer than `peaks.npz` (catches
  stems re-run in an earlier invocation that skipped `peaks`). The mix needs
  no check: the cache dir is keyed by its content hash.
- Tracks without cached stems are skipped with a log line.
- Memory: stems are streamed from FLAC in blocks (a multiple of 147
  samples); the mix uses the decoded `TrackCache.audio`, dropped after.

## Results (10 test tracks, 2026-11-07)
`--stages peaks` on cached stems, dev PC. Time includes the ffmpeg decode of
the mix (most of it); no GPU used.

| Track | Length | Time | peaks.npz |
|---|---|---|---|
| Hard Driver - Spark Inside | 147.9 s | 1.45 s | 2.2 MB |
| Devin Wild - When The Book Is Closed | 183.8 s | 1.18 s | 2.7 MB |
| Chris Lorenzo - Take Me As I Am | 186.8 s | 2.33 s | 3.0 MB |
| Dimension - Desire | 215.2 s | 1.28 s | 3.4 MB |
| Skrillex - Kora | 220.1 s | 1.82 s | 3.3 MB |
| Shift K3y - In Your Arms (Extended) | 231.1 s | 1.97 s | 3.6 MB |
| Hayley Williams - Stay the Night (Extended Remix) | 288.8 s | 2.11 s | 4.6 MB |
| Porter Robinson - Language | 368.5 s | 2.74 s | 5.4 MB |
| Virtual Self - ANGEL VOICES | 392.3 s | 2.81 s | 6.0 MB |
| Mat zo - Easy (Extended Mix) | 396.3 s | 2.97 s | 6.3 MB |

- Compression saves little (6.3 MB vs 7.1 MB raw for 396 s): about
  16 KB per second of audio.
- Rendered for a by-eye check: lanes with section/phrase bands per track,
  and an overlay of raw samples on the stored peaks at levels 0, 2 and 4
  (Devin Wild, at the bar-69 drop). Raw samples stay inside min/max at every
  level; stored min/max equal a direct whole-array computation within 1
  int16 step (rounding).
- Sources at 48 kHz (Devin Wild) are peaked from the 44.1 kHz decode, like
  every other stage; time stays exact in that domain.
