# 0016 — Drum onsets from a drum transcription model (ADTOF)

Date: 2026-11-10. Status: accepted; built
(`gen_system/backend/src/volu_gen/analysis/features.py` → `drum_hits`,
`tests/test_features.py`). Replaces the band onset classes `kick` /
`snare` / `hat` of decision 0012; the rest of 0012 stands. Only `kick` and
`hat` are stored now (`0017-kick-and-hat-onsets-only.md`).

## Context
Decision 0012 found drum onsets as spectral-flux peaks in three frequency
bands of the demucs drums stem (30–150 Hz, 150 Hz – 4 kHz, 6–16 kHz). The
`--check` click tracks showed this doesn't work: in almost every test track
one kick gave several clicks, and snares and other drums with a low body
were counted as kicks. Thresholds can't fix either. A band can't tell a
snare's body from a kick, and the extra clicks come from rises inside one
long, pitch-swept EDM kick. Counts per beat had looked right (Virus: 0.98
kicks per beat) only because doubles and snares made up for missed kicks.

Methods need "a kick hit here" to be right, since kicks drive most of them
(`../generators.md`).

## Decision
Drum onsets come from **ADTOF** (Zehren et al., Frame_RNN; the PyTorch
port `xavriley/ADTOF-pytorch`, pinned to a commit, weights bundled) run
on the **demucs drums stem**, with each hit then **snapped to the attack**
in that stem.

- Classes: `kick`, `snare`, `tom`, `hat`, `cymbal` (ADTOF's five). `tom`
  and `cymbal` are new; `bass`, `other`, `vocals` stay band-flux classes
  from 0012.
- **Model:** ADTOF's log-filtered spectrogram (12 bands/octave, 2048-point
  frames, 100 fps) of the stem's mono sum → per-class hit probability per
  10 ms frame. Peaks are picked with ADTOF's own peak picker and per-class
  thresholds (`FRAME_RNN_THRESHOLDS`: 0.22, 0.24, 0.32, 0.22, 0.30).
- **Input is the drums stem, not the mix.** On the mix ADTOF hears
  percussive synths as drums: in a breakdown of Porter Robinson
  *Language* (3:35–4:00, drums stem 37–48 dB below its loud level) it found
  19 kicks and 95 cymbals, one per synth note. On the drums stem it found
  none there. Kick and snare counts elsewhere barely change.
- **Snap:** ADTOF's frames are 10 ms and its peaks read 10–20 ms early.
  Each hit moves to the strongest full-band flux peak (0012's flux, 3.3 ms
  frames) of the drums stem from 20 ms before to 40 ms after it. Two hits of
  one class that snap to the same attack become one.
- **Strength:** the flux at the snapped peak (how loud the attack is, not
  model confidence) divided by the class's 95th percentile, clipped to 1,
  as in 0012.
- **CPU, inside the `features` stage:** ADTOF is small (two conv blocks,
  three 60-unit bidirectional GRUs). It takes about 6 s for a 6.6-min track
  on the CPU, so the stage stays CPU-only and needs no new GPU stage. The
  stage now takes about 8–21 s per track (was about 15 s for a 6.6-min
  track). The model loads once per process. GPU and CPU outputs differ by
  up to 0.003, which flips a rare hit right at a threshold.
- `FEATURES_VERSION` 2: older files are recomputed.

### Results (2026-11-10, 22 test tracks, by ear)
"Timed correctly; some mistakes, but usable." In drops, kick patterns
come out as you'd expect: four on the floor in house, trance and big room
(Virus, Shift K3y, Hayley Williams), syncopated patterns that repeat bar
to bar (Virtual Self *ANGEL VOICES*), one kick per bar in halftime dubstep.
Known mistakes:
- Hardstyle (Hard Driver *Spark Inside*, Devin Wild): about 20% of kicks
  come within 100 ms of the previous one, probably the long kick counted
  twice.
- Skrillex *Kora*: one snare in the whole track.
- On the drums stem there are about 80% fewer cymbals than on the mix,
  mostly synths no longer counted. Some real cymbals may be lost too.
- About 10% of Virus kicks snap ~30 ms early, probably onto something just
  before the kick.

## Alternatives considered
- **Band classes with better thresholds** (0012): see Context.
- **drumsep** (Hybrid Demucs splitting a drums stem into kick, snare,
  cymbals, toms; MIT), then onsets per piece: fast (2–4 s, 2.3 GB VRAM),
  and kick/snare overlap fell from 68% to 3% on Virus. But it was trained
  on acoustic kits: it puts an EDM kick's attack in `toms` and its body in
  `kick` about 60 ms late, and a hardstyle kick mostly in `toms`. With kick
  + toms, extra hits remained in the separated stems, about 90 ms after a
  hit and as strong as it, which an envelope can't tell from real hits.
- **LarsNet** (five-piece drum separation, trained on StemGMD): not tried;
  also acoustic-kit training data, and a non-commercial model license.
- **ADTOF on the mix:** best timing, but synths become drums (Decision).

## Consequences
- New dependency `adtof-pytorch` (git, pinned) plus `pretty_midi` and
  `mido`, which it imports. **License:** ADTOF's models are CC BY-NC-SA 4.0
  (non-commercial, share-alike); the converted weights carry that over.
  Fine for a personal, non-commercial build; any commercial use needs
  another model.
- Methods can now treat `kick` as a real drum label, not a low band hit
  (`snare` was later dropped as unreliable, decision 0017).

## Open questions
1. Hardstyle double kicks: a "no second kick within one 16th unless at
   least as strong" rule would likely drop them without touching real kick
   rolls. Check by ear before adding (gen_system/AGENTS.md → tune for the
   common case).
2. ~~Whether the lost cymbals matter to any method~~: cymbals are no longer
   stored (decision 0017).
