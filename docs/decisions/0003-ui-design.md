# 0003 — UI design and stack (v1)

Date: 2026-11-01. Status: accepted (not yet implemented).

## Context
Single user; the engine always runs on the same PC as the UI. Generator
parameters will be tweaked often. See `../ui.md` for the full design.

## Decision
- Stack: Vite + React + TypeScript, Mantine v7 (dark only), react-three-fiber
  + drei for the 3D preview, custom canvas stem waveforms from
  backend-computed peaks, TanStack Query (HTTP) + Zustand (WebSocket live
  state), TanStack Router.
- The browser never plays audio; the engine plays it and is the only clock.
- Pages: Library, Player, Studio, Display, Settings, plus a persistent
  transport/status bar.
- Playback is a simple queue: tracks play end to end, no playlists or fades.
- Studio re-renders the **entire track** on each Render; one overwritten
  Draft slot per track, explicit saved versions, one active version.
- UI-set generator parameters are constant across a track.
- Hardware output is armed automatically when drive nodes are detected (ping
  until node status packets exist), in Playback and Studio; manual override.

## Deferred
Loop-region preview with background re-render and A/B; per-section
parameters; playlists/crossfades. See `../ui.md` → Deferred.
