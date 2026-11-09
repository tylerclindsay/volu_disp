# data/

Local, gitignored data. Only this README is tracked.

- `tracks/<sha256[:16]>/` — per-track data, keyed by audio hash: the
  imported source audio (`source.<ext>`), demucs stems, analysis results,
  waveform peaks, and rendered animations (`docs/architecture.md` → Storage
  layout)
