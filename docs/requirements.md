# Requirements

Status: draft — collected from discussion, not final.

## Display
- Display size, layout, and node count are configurable (prototype now,
  larger build later). See `config/displays/`.
- Target frame rate **120 FPS**, configurable in the UI.

## Generation pipeline
Triggered from the UI when the user adds an audio file:
1. **Stem separation** with demucs → four stems: `vocals`, `drums`, `bass`,
   `other` ("other" is the remaining instrumental content).
2. **BPM detection** on the track.
3. **Animation generator** — uses the stems and BPM (in different parts of
   the animation) to create graphics synchronized to the music. What the
   animations look like is not yet decided.
4. Output: an **animation file** of structured frame data, rendered at the
   maximum frame rate (currently 120 FPS, a single config value). Lower
   playback frame rates are derived from it at playback.

## UI
- Settings, including playback frame rate.
- Song selector reading local audio files on the PC; play songs.
- While a song is selected/playing: a **3D visualization** of the display
  output, **waveforms of each stem**, and the **detected BPM**, while the
  animation's pixel data streams to the display over UDP in sync with audio.

## Open questions
- Animation style/design.
- Animation file format.
- Whether live/reactive modes are in scope.
- Audio output device.
