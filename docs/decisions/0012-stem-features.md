# 0012 — Stem features for generators

Date: 2026-11-09. Status: accepted; built 2026-11-10
(`gen_system/backend/src/volu_gen/analysis/features.py`,
`tests/test_features.py`). The drum onset classes were replaced the same
day by a drum transcription model: `0016-drum-onsets-adtof.md`. Only
`kick` and `hat` onsets are kept; the flux classes are gone
(`0017-kick-and-hat-onsets-only.md`). Values marked *(tune)* are starting points to
check by eye and ear on the test tracks.

## Context
Generators (`../generators.md`) need to know, at any time in a track,
what each stem is doing: when kicks and hats hit, how loud the bass is,
which frequencies are active, how wide the stereo image is. The analysis
in `analysis.json` (decision 0004) is per bar or per phrase, far too
coarse for that. Raw samples are available to methods, but computing
onsets and spectra inside every method would be slow and inconsistent
between methods, and the API server's planner (decision 0011) can't
afford it at all.

Constraints:
- **Independent of the display FPS** (30–480, decision 0010): an FPS
  change must never re-run this stage.
- **Independent of the beat grid:** a grid override (`regrid`) must not
  re-run it either. Features are in seconds; `Music` relates them to bars.
- Cheap enough to read in the server for every re-plan (milliseconds once
  cached).
- CPU only, like `phrases` and `peaks`.

## Decision

### What is stored
Lanes, as in peaks: `mix, vocals, drums, bass, other`.

| Feature | Per | Rate | Contents |
|---|---|---|---|
| Envelope | lane | 300 Hz | RMS, both channels together, linear (the peaks scale: stems sum to the mix). Unsmoothed: methods apply their own attack/release |
| Envelope reference | lane | one value | the lane's **loud level**: 95th percentile of the envelope over frames above −60 dBFS; the same idea as `bars.energy_db` (decision 0004). A lane that is never above −60 dBFS (no vocals) gets 10⁻³ (−60 dBFS), so its normalized envelope stays below 1 |
| Band spectrum | lane | 100 Hz | 24 log-spaced bands, 40 Hz – 16 kHz, power in dBFS (0 dB = mean square 1, so a full-scale sine is −3 dB); −120 dB for a silent band |
| Stereo width | lane | 100 Hz | `side_rms / (mid_rms + side_rms)`, 0 = mono, 0.5 = uncorrelated, 1 = out of phase; `NaN` where the lane is below −60 dBFS |
| Onsets | class | event list | time (s) and strength (0–1) of each detected hit |

Onset classes:

| Class | Source | Band | Min gap | Use |
|---|---|---|---|---|
| `kick` | drums stem, ADTOF (0016) | — | — | the main driver: pulses, big drops, flashes |
| `hat` | drums stem, ADTOF (0016) | — | — | fine detail: twinkle, sparkle |

`snare`, `tom`, `cymbal` (ADTOF) and the flux classes `bass` (80 ms gap),
`other` (50 ms), `vocals` (80 ms) were stored until decision 0017 dropped
them as unreliable.

The drum classes were first **band classes** of the drums stem (kick
30–150 Hz, snare 150 Hz – 4 kHz, hat 6–16 kHz) with the flux method below.
By ear they failed: several clicks per kick, and snares counted as kicks.
They are now drum recognition (decision 0016).

### How it is computed
All from the 44.1 kHz decode (`BASE_SR`), like every other stage; times
are seconds in that domain and match `analysis.json` and `peaks.npz`.

- **Envelope:** RMS over a 441-sample (10 ms) Hann window centered on each
  frame, hop 147 (the peaks level-0 bucket), so frame `k` is at exactly
  `k · 147 / 44100` s.
- **Band spectrum and width:** STFT, `n_fft` 4096 (93 ms) Hann, hop 441
  (10 ms), centered (frame `k` at `k · 441 / 44100` s). Bands are
  triangular weights in log frequency, peaking at each band's geometric
  center and summing to 1 for every bin between 40 Hz and 16 kHz, so band
  powers add up to the power in that range. The lowest three bands are only
  1–2 bins wide, so they are smoother than the rest. The long window is
  needed for bass resolution and suits the display: spectra fade in and
  out over ~90 ms instead of flickering. Width uses the same frames:
  mid = (L + R)/2, side = (L − R)/2, RMS of each (from the bins, by
  Parseval). Bin power is scaled so a frame's bins sum to its windowed
  mean square.
- **Onsets** (as built for `bass`, `other`, `vocals`, removed in decision
  0017; the full-band flux of the drums stem remains, since decision 0016
  snaps its hits to it): for each class, a short STFT (`n_fft` 1024, hop 147, so
  3.3 ms frames) of its source stem (mid and side power summed). Per band:
  - **Magnitude spectral flux:** the sum over the band's bins of the
    half-wave rectified rise in magnitude since 3 frames (10 ms) earlier.
  - **Normalized** by the band's loud magnitude (95th percentile of the
    band's bin sum over frames above −60 dBFS), so 1 is a rise from silence
    to loud within 10 ms. A band never above −60 dBFS (stem bleed, an
    instrumental's vocals) has no onsets.
  - **Peak picking:** local maxima above both the moving median + `delta`
    and `local` × the moving max, both over 0.5 s; then at least the
    class's gap apart, the stronger of two close peaks winning.
  - Time = the peak's frame. Strength = the peak divided by the 95th
    percentile of the class's kept peaks in the track, clipped to 1.

  This replaced the planned log-power flux with a median-only threshold
  (`librosa.onset`) after plotting the test tracks. Log flux fired on
  ripple in long, pitch-swept kick bodies (3–4 "kicks" per beat in 4/4
  drops) and ~10 ms early. With a median-only threshold the small bumps in
  a kick's decay passed, since the flux is near zero between hits; the
  moving max removes them. Scaling `delta` by the flux's own percentile
  turned a near-silent lane's noise into full-strength onsets; the band's
  loud level does not. Counts then looked right (Martin Garrix *Virus*:
  0.98 kicks per beat), but listening showed doubled kicks and snares
  counted as kicks; see decision 0016.
- Tracks without stored stems are skipped with a log line (as `peaks`).
- Memory: one lane decoded at a time (~85 MB for a 4-min stereo stem);
  STFTs run in blocks of 2048 frames.
- Time (measured): about 21 s for a 6.6-min track: ~6 s of ADTOF on the
  CPU (decision 0016), the rest in the STFTs (`n_fft` 4096 on five lanes,
  1024 at a 3.3 ms hop on four). Since decision 0017 the 1024 STFT runs
  on the drums stem only: about 16 s for the same track (measured). Small
  next to the model stages, and it runs once per track.

### Storage
- `<data>/tracks/<key>/features.npz` (`np.savez_compressed`), written
  to a temp name and renamed. Not in `analysis.json`, which stays small;
  its `version` is not bumped.
- Arrays:

| Array | dtype | Shape |
|---|---|---|
| `env` | float16 | `(lanes, n_env)` |
| `env_ref` | float32 | `(lanes,)` |
| `spec` | float16 | `(lanes, 24, n_spec)` |
| `width` | float16 | `(lanes, n_spec)` |
| `onset_<class>_t` | float64 | `(n,)`, sorted |
| `onset_<class>_s` | float32 | `(n,)` |

- Metadata arrays: `version` (`FEATURES_VERSION`, starting at 1),
  `sample_rate` (44100), `env_hop` (147), `spec_hop` (441), `n_fft`
  (4096), `band_edges` (25 Hz values), `n_samples`, `lanes`, `classes`.
- Size for a 396 s track, raw: env 1.2 MB, spec 9.5 MB, width 0.4 MB,
  onsets < 0.1 MB: about **11 MB**, **8.1 MB** compressed (measured, with
  kick and hat onsets);
  small next to the stems (~180 MB).
- Read with `read_features()` (`analysis/features.py`), which returns the
  arrays plus their times. A format change bumps `FEATURES_VERSION`; files
  with another version are recomputed.

### Pipeline stage
- Stage `features`, CPU only, after `peaks`. Writes nothing to
  `analysis.json`, including `models`.
- Re-runs when: forced (`--force features`); `features.npz` is missing or
  has another `version`; or any stem file is newer than it (the `peaks`
  rule).
- Not re-run by `regrid` or an FPS change.
- Part of the `analyze` job, so a track's default render waits for it.
- `--check` also writes, per class, `check/onsets_<class>.wav` (the drums
  stem at −6 dB with a click per onset, louder for stronger ones) and
  `check/labels_onsets_<class>.txt` (Audacity labels with the strength).

### Reading: `Music`
`Music` (`../generators.md` → Inputs) wraps the file; methods never index
the arrays themselves. Queries are vectorized over an array of times:
- `env(lane, t, db=False)` → linearly interpolated envelope, divided by
  `env_ref` (1.0 = the lane's loud level; peaks go above), or in dB re.
  that level.
- `spectrum(lane, t)` → `[n, 24]` dB, interpolated between frames.
- `width(lane, t)`, `width_stats(lane, t0, t1)` → mean width over a range
  (ignoring silent frames), for the director's `vectorscope` choice.
- `onsets(cls, t0, t1, min_strength=0)` → times and strengths in a range;
  `since(cls, t)` / `until(cls, t)` → time since the last / until the next
  hit, the common building block for decays (kick → 100–300 ms decay,
  `../generators.md` → Smoothness).
- Interpolation between frames makes every query valid at any frame rate;
  onsets are times, so a hit shows from the first frame starting at or
  after it, at any rate (`../generators.md` → Inputs, `RenderContext`).
- The API server loads a track's features once per Studio session and
  keeps them in memory (~11 MB per track) for re-planning.

## Alternatives considered
- **Features at the display FPS:** would re-run the stage on every FPS
  change and tie the files to a setting (decision 0010 rules this out).
- **Inside `analysis.json`:** megabytes of numbers in JSON, read on every
  analysis query; kept separate like peaks.
- **Reuse `peaks.npz` level 0 as the envelope:** same rate and scale, but
  peak RMS is over a rectangular 3.3 ms bucket (aliasing-prone for
  driving lights) and the file's format is owned by the UI. A 10 ms Hann
  RMS is cheap to compute once more.
- **Log-power flux with `librosa.onset`** (the first plan): see How it is
  computed → Onsets.
- **A drum transcription model** (e.g. kick/snare/hat classifiers): first
  put off as another GPU model and stage; adopted in decision 0016 after
  the band classes failed by ear (it runs on the CPU, in this stage).

## Open questions
1. ~~Flux onset thresholds for `bass`, `other`, `vocals`~~: classes
   removed (decision 0017).
2. **Hardstyle kicks are split between stems.** demucs puts the attack in
   `drums` and the long distorted body in `bass` (Hard Driver *Spark
   Inside*: 5% of the energy below 150 Hz in its drops is in `drums`).
   `kick` from `drums` alone still finds those kicks on the beat, from the
   attack, but their strength follows the attack only (also with ADTOF,
   decision 0016). Taking `kick` from
   drums + bass was tried and rejected: every bass note became a kick
   (3–5 per beat). The body shows in `env("bass")`.
3. Whether 24 bands is enough for the large build's `spectrogram`
   (frequency across x on a wide display); bands are computed from the
   stored STFT settings, so more bands is only a version bump.
4. ~~Whether `vocals` and `other` onsets are useful enough to keep~~: not
   kept (decision 0017).
