# 0005 — Per-bar drop detection

Date: 2026-11-01, revised 2026-11-02. Status: accepted; implemented in
`gen_system/backend/src/volu_gen/analysis/phrases.py`. Replaces the drop rule
in 0004 → Phrases and events.

## Context
0004 tagged a phrase `drop` when the mix jumped ≥ 4 dB into it with drums
and bass on in its first bar. Two problems (0004 → Known gaps): drops off a
phrase start were missed, and the bass check depended on how demucs split
the kick. With seeded stems, Devin Wild - When The Book Is Closed lost its
verified drop at bar 69: in that bar the bass stem reads −35 dB and drums
+2 dB, because the hardstyle kick, which carries the bass, landed in drums.

## Decision
Drops are found at every bar, and each drop starts a new phrase (phrases are
8 bars counted from each section start **and** each drop). A bar is a drop
when all of these hold:

| Check | Rule |
|---|---|
| jump | mix of this bar + the next is ≥ 4 dB above the 2 bars before |
| low end | the new `low` lane is on (> −18 dB) |
| drums | on in the bar |

- **Arrival:** if the bar isn't yet at the new level (it is more than 2 dB
  under the next bar, e.g. a one-bar drum fill before the drop), the drop
  moves to the first of the next 2 bars that is, with drums and low on. It
  stays put if none is. This only changes where a drop sits, never whether
  it is found. (A first version rejected such candidates instead; without
  either, the jump peaks on the fill and Porter's drop moves from 33 to 32.)
- **Dedupe:** of drops within 2 bars of each other, the biggest jump wins.
- **Same drop:** a jump less than 8 bars (one phrase) after the previous
  drop is the same drop returning from a short break. Exactly 8 bars later
  counts as a new drop. Devin bar 111 (8 after 103) is a separate drop by
  this rule; by ear it's part of the 103 drop, but a rule that merges it
  would rest on that one bar. Accepted as is.
- **`low` lane:** drums + bass summed, low-passed at 150 Hz, as per-bar dB
  like the stems. It reads the same whichever stem demucs puts the kick in.
  Stored in `bars.energy_db.low`; analysis.json `version` 2.
- Tried and rejected: a 4-bar "before" window (to require a longer lull) —
  it lost 6 confirmed drops.

### Commented out: level and drum-hold checks
Two more checks came from listening and are kept in `find_drops`, commented
out, with their tests skipped:

- **level:** the drop's first 2 bars average ≥ −3 dB (0 dB = the track's
  loud level). Rejects a riser into a drop (Devin bar 93: −5.2 dB).
- **drum hold:** drums on in ≥ 3 of the drop's first 4 bars. Rejects one bar
  of drums opening a buildup (Mat zo - Easy bar 42, Porter Robinson -
  Language bars 33 and 113).

Each was fitted to one to three cases on the same tracks it was judged on.
Confirmed drops reach ≥ −1.6 dB, a thin margin, and all are
four-on-the-floor with drums on 4/4 bars, so the hold check is untested on
half-time, dubstep and trap drops. A held-out check (12 tracks, mostly
dubstep/trap/half-time) was set up but not listened to; the user chose to
take the false drops below over rules that might miss real ones elsewhere.
Bring them back only after a listening check on tracks they weren't fitted to.

On the 10 test tracks (Kora excluded), with the user's ground truth:

| Variant | Hits | Misses | False drops |
|---|---|---|---|
| neither (current) | 20 | 0 | 5: Devin 93, 111; Mat zo 42; Porter 33, 113 |
| + level | 20 | 0 | 2: Devin 111; Mat zo 42 |
| + hold | 20 | 0 | 2: Devin 93, 111 |
| + both | 20 | 0 | 1: Devin 111 |

## Results (10 test tracks, vs. 0004's rule)
- Devin Wild: +69 (verified), +103 (verified by ear, starts at 103), +111
  (part of the 103 drop by ear; see "Same drop"), +93 (riser; false).
- Mat zo - Easy: +42 (start of the buildup; false).
- Porter Robinson - Language: +33, +113 (start of the buildup; false).
- Virtual Self - ANGEL VOICES: +226. By ear it's the 210 drop continuing
  after a 2-bar break, but it can count as a separate drop; left as is.
- Skrillex - Kora: +15, +83, +178. Variable tempo; its "bars" are Beat
  This's raw downbeats, so per-bar rules mean little there. Not tuned for it.
- Other tracks: unchanged.

## Known gaps
- False drops at a riser and at one bar of drums opening a buildup (above).
- `buildup` is still a mix slope across the phrase. A short phrase before a
  drop (Devin 101–102) has too few bars for it, and a riser whose mix
  doesn't rise (Devin 93–100) isn't tagged.
