# data/

Local, gitignored data. Only this README is tracked.

- `library/` — source audio files
- `tracks/<sha256[:16]>/` — per-track data, keyed by audio hash: demucs
  stems, analysis results, waveform peaks, and rendered animations
  (`docs/architecture.md` → Storage layout)
