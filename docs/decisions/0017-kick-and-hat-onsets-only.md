# 0017 — Keep only kick and hat onsets

Date: 2026-11-10. Status: accepted; built
(`gen_system/backend/src/volu_gen/analysis/features.py`,
`tests/test_features.py`). Narrows the onset classes of decisions 0012 and
0016; the rest of both stands.

## Context
`features.npz` stored onsets for eight classes: `kick`, `snare`, `tom`,
`hat`, `cymbal` (ADTOF on the drums stem, decision 0016) and `bass`,
`other`, `vocals` (band flux on their stems, decision 0012). Listening to
the `--check` click tracks, only `kick` and `hat` gave reliable hits. The
others were too often wrong or missing to drive a method (0016 already
lists one snare in all of Skrillex *Kora* and about 80% of cymbals lost on
the drums stem; the flux classes were never tuned by ear, 0012 open
question 4).

## Decision
- `features.npz` stores onsets for **`kick` and `hat` only**
  (`CLASSES`), both from ADTOF on the drums stem with 0016's snap and
  strength. ADTOF still outputs all five classes; the other three are
  dropped after the model.
- The flux onset classes (`bass`, `other`, `vocals`), their per-class
  thresholds and the peak picker (`pick_onsets`) are removed. The
  full-band flux of the drums stem stays: ADTOF hits snap to it.
- `--check` writes `onsets_kick.wav` / `onsets_hat.wav` and their labels,
  both over the drums stem.
- `FEATURES_VERSION` 3: older files are recomputed.
- The stage no longer runs onset STFTs on the bass, other and vocals stems:
  about 16 s for a 6.6-min track (was about 21 s; measured, model already
  loaded). `features.npz` is 8.1 MB there (was 8.2 MB).

## Consequences
- Methods use `kick` for the main pulse and `hat` for fine detail. Accents
  that were meant to come from `snare` (e.g. `fireworks` launches) and
  bass note starts use something else: bass via `env("bass")`, accents via
  kicks, phrases or drops, until a class is reliable enough to add back.
- Adding a class back is a `CLASSES` entry (an ADTOF class) and a version
  bump, after checking it by ear with `--check`.

## Alternatives considered
- **Keep all classes, mark some as unreliable:** methods would still be
  tempted to use them, and every file would carry data nobody should read.
- **Tune the weak classes first:** possible later per class; not worth
  holding up methods for.
