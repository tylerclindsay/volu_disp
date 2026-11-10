# 0009 — Audio output: WASAPI shared by default, exclusive as a setting

Date: 2026-11-09. Status: accepted; not yet implemented. Design in
`../architecture.md` → Engine (Clock) and Settled.

## Context
The engine plays audio through PortAudio (`sounddevice`) on Windows and
derives the frame index from the audio clock (`stream.time` vs. DAC
timestamps). `display_offset` absorbs whatever output latency there is, so
absolute latency barely matters. What matters is that latency is **stable
and correctly reported**.

## Decision
**WASAPI, shared mode by default; exclusive mode as a user setting.**
- Shared mode coexists with other apps and doesn't fail when the device is
  busy. The Windows mixer runs at its own sample rate, so the engine
  resamples to the device rate (or uses
  `WasapiSettings(auto_convert=True)`).
- Exclusive mode gives lower, tighter latency but locks the device.
- Calibration (`display_offset`) is stored per (device, mode), since the
  offset differs between them.

## Alternatives considered
- **Exclusive only:** best timing, but fails whenever another app holds the
  device and blocks all other audio on the PC.
- **MME / DirectSound:** higher and less accurately reported latency;
  they're legacy layers on top of WASAPI anyway.
- **ASIO:** needs third-party drivers; not available for most devices.

## Consequences
- Before relying on it, verify with the calibration flash + click that
  shared-mode latency is stable and reported correctly. If it isn't,
  revisit (e.g. make exclusive the default).
