# Requirements

Status: draft — collected from discussion, not final.

## Display
- Display size, layout, and node count are configurable (prototype now,
  larger build later). See `config/displays/`.
- Prototype: 10 × 12 × 8 (x wide × y tall × z deep), 2 drive nodes.
- Configurable mapping: each drive node is assigned a rectangular section
  (prism or cube) of the display.
- Display frame rate: **any whole number from 30 to 480 FPS, default
  120**, set in the UI (`decisions/0010-display-fps.md`).

## Generation pipeline
Triggered from the UI when the user adds an audio file:
1. **Stem separation** with demucs → four stems: `vocals`, `drums`, `bass`,
   `other` ("other" is the remaining instrumental content).
2. **Analysis** (`decisions/0004-audio-analysis.md`):
   - **Beat grid** — one constant tempo and continuous beat/downbeat grid
     per track (Beat This); variable-tempo tracks fall back to raw beats,
     flagged `tempo: variable`.
   - **Sections** — boundaries from SongFormer.
   - **Phrases** — 8-bar phrases from each section start and drop, tagged drop /
     buildup / breakdown; drums/bass in/out events; per-bar energy per stem.
   - **Genre** (MAEST) and **mood** (CLAP, a weak hint).
   - Must fit an 8 GB NVIDIA card. BPM and grid offset can be overridden
     from the UI.
3. **Animation generator** — uses the stems and analysis (in different
   parts of the animation) to create graphics synchronized to the music.
   What the animations look like is not yet decided.
4. Output: an **animation file** of structured frame data (format:
   `decisions/0008-animation-file-format.md`), rendered at the display
   frame rate. An animation plays only at the rate it was rendered at:
   after the display frame rate changes, every animation is unplayable
   until it is re-rendered at the new rate.

## UI
- Settings, including the display frame rate.
- Import local audio files (copied into the system's track data); song
  selector; play songs.
- While a song is selected/playing: a **3D visualization** of the display
  output, **waveforms of each stem** with sections and phrases, and the
  **detected BPM**, while the
  animation's pixel data streams to the display over UDP in sync with audio.
- A play queue (tracks play end to end; no playlists or fades in v1).
- Tweak generator parameters (track params, constant per track, plus
  per-instance params for each segment a method runs in;
  `generators.md` → Studio parameters) and re-render the whole track from
  the UI.
- Full UI design: `ui.md`.

## Open questions
- Animation style/design. Direction in `generators.md`: a director picks
  one of several methods (rain, fireworks, geometry, …) per song segment.
- Whether live/reactive modes are in scope.
- Audio output device.
