# 0012 — Stem features for generators

Date: 2026-11-09. Status: accepted; not yet built. Values marked
*(tune)* are starting points to check by eye and ear on the test tracks.

## Context
Generators (`../generators.md`) need to know, at any time in a track,
what each stem is doing: when kicks and snares hit, how loud the bass is,
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
| Envelope reference | lane | one value | the lane's **loud level**: 95th percentile of the envelope over frames above −60 dBFS; the same idea as `bars.energy_db` (decision 0004) |
| Band spectrum | lane | 100 Hz | 24 log-spaced bands, 40 Hz – 16 kHz, power in dBFS |
| Stereo width | lane | 100 Hz | `side_rms / (mid_rms + side_rms)`, 0 = mono, 0.5 = uncorrelated, 1 = out of phase; `NaN` where the lane is below −60 dBFS |
| Onsets | class | event list | time (s) and strength (0–1) of each detected hit |

Onset classes:

| Class | Source | Band | Min gap | Use |
|---|---|---|---|---|
| `kick` | drums stem | 30–150 Hz | 60 ms | the main driver: pulses, big drops, flashes |
| `snare` | drums stem | 150 Hz – 4 kHz | 60 ms | accents: firework launches, ripples |
| `hat` | drums stem | 6–16 kHz | 30 ms | fine detail: twinkle, sparkle |
| `bass` | bass stem | full | 80 ms | note starts: swells, terrain pulses |
| `other` | other stem | full | 50 ms | melodic/synth hits |
| `vocals` | vocals stem | full | 80 ms | syllables / phrase starts |

The drum classes are **band classes, not drum recognition**: a snare with
a heavy low body can also trigger `kick`, a tom lands in whichever band
holds most of its energy. Methods should treat them as "low / mid / high
hit". A drum classifier can replace them later behind the same arrays.

### How it is computed
All from the 44.1 kHz decode (`BASE_SR`), like every other stage; times
are seconds in that domain and match `analysis.json` and `peaks.npz`.

- **Envelope:** RMS over a 441-sample (10 ms) Hann window centered on each
  frame, hop 147 (the peaks level-0 bucket), so frame `k` is at exactly
  `k · 147 / 44100` s.
- **Band spectrum and width:** STFT, `n_fft` 4096 (93 ms) Hann, hop 441
  (10 ms), centered (frame `k` at `k · 441 / 44100` s). Bands are
  triangular weights over the power bins; the lowest three bands are only
  1–2 bins wide, so they are smoother than the rest. The long window is
  needed for bass resolution and suits the display: spectra fade in and
  out over ~90 ms instead of flickering. Width uses the same frames:
  mid = (L + R)/2, side = (L − R)/2, RMS of each.
- **Onsets:** for each class, a short STFT (`n_fft` 1024, hop 147) of its
  source stem, summed over the class band → log-power spectral flux
  (half-wave rectified frame difference) → peak picking with an adaptive
  threshold (moving median over 0.5 s + delta *(tune)*) and the class's
  minimum gap. `librosa.onset` provides both steps. Time = the flux peak's
  frame (3.3 ms resolution). Strength = flux peak divided by the 95th
  percentile of the class's peak values in the track, clipped to 1.
- Tracks without stored stems are skipped with a log line (as `peaks`).
- Memory: one lane decoded at a time (~85 MB for a 4-min stereo stem).
  Expected time: a few seconds per track, dominated by the STFTs and
  decoding the mix *(measure)*.

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
  onsets < 0.1 MB: about **11 MB**, less compressed *(measure)*; small
  next to the stems (~180 MB).
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
  onsets are times, so a hit lands on the frame that contains it at any
  rate.
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
- **A drum transcription model** (e.g. kick/snare/hat classifiers):
  better labels but another GPU model and stage; band classes on the
  demucs drums stem are a good first version.

## Open questions
1. Onset thresholds, gaps, and band edges *(tune)* on the test tracks,
   with `check/` click tracks per class for listening.
2. Whether 24 bands is enough for the large build's `spectrogram`
   (frequency across x on a wide display); bands are computed from the
   stored STFT settings, so more bands is only a version bump.
3. Whether `vocals` and `other` onsets are useful enough to keep.
