# 0011 — Render planning runs in the API server

Date: 2026-11-09. Status: accepted; implemented
(`gen_system/backend/src/volu_gen/server/planning.py`, `POST
/tracks/{id}/plan`). The re-plan before a render comes with the server's
job dispatch. Design in
`../generators.md` → Plan and `../architecture.md` → API server.

## Context
A render is two steps (`../generators.md`): the director makes a **plan**
(segments, a method per segment, each instance's params, transitions),
then methods render it. Studio shows the plan as a segment lane, and the
user edits instance params on it. Every track-param or instance edit, and
every grid override, changes the plan.

The worker runs one job at a time and never preempts: a Studio job queued
behind another job waits for it to finish: up to one track's analysis
(tens of seconds) or one render (minutes per track on the large display).

## Decision
**The API server makes plans in-process; the worker only renders.**
- Planning is pure Python over `analysis.json` and `features.npz`
  (director + each method's `choose_params`): no models, no GPU,
  milliseconds. The server runs it off the event loop
  (`asyncio.to_thread`).
- It runs on demand: on every track-param or instance edit (the HTTP
  response carries the new plan), after a grid override, and before a
  render whose version has no plan yet or whose plan was made by another
  generator or method version (the new plan overwrites the stored one).
  Otherwise a render uses the stored plan unchanged, e.g. an FPS
  re-render (decision 0010), apart from segment times, which are
  recomputed from bars on the current grid before every render. The `render` job spec carries the plan.
- The server stores track params and plan in the version's DB row; the
  `.anim` metadata carries a copy.

## Alternatives considered
- **Plan inside the render job:** the plan exists only after a render, so
  Studio can't show the effect of a track-param change until frames are
  done, and an edit made against an old plan is ambiguous.
- **A separate `plan` worker job:** correct, but waits behind analysis;
  the segment lane would lag edits by seconds or more.

## Consequences
- `volu_gen.generators` is imported by both the server and the worker, so
  it must stay light at import time: no torch or other heavy imports at
  module level. Methods that need torch import it inside `render`.
- A planning bug surfaces as an error in an HTTP response, not a crashed
  server: planning is wrapped and reported like any request failure.
- Determinism matters twice: the plan the user saw is the plan that
  renders, because the server hands it over rather than the worker
  re-deriving it.
