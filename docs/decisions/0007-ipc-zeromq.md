# 0007 — IPC over ZeroMQ

Date: 2026-11-09. Status: accepted; amended 2026-11-11 (preview HWM, see
Amendment). Implemented: the messages and sockets (`volu_gen/ipc/`) and
the worker side (`volu_gen/worker/`). The server's side and the engine
are still to come. Design in `../architecture.md` → IPC.

## Context
The API server talks to two child processes: the engine (real-time audio +
frames) and the worker (GPU jobs). The IPC must:
- let a restarted server reconnect to a running engine without stopping
  playback;
- cost the engine as little as possible: it is Python, and anything its
  control thread does competes for the GIL with the frame thread;
- integrate with the server's single asyncio loop, on Windows first;
- carry low-rate control (commands, ~20 Hz status) and a preview stream of
  up to the preview FPS (≤ the display FPS, decision 0010) × 60 KB (20k
  pixels; ≈ 7.2 MB/s at 120 FPS, linear in the rate) where only the newest
  frame matters.

The first draft used `multiprocessing.connection` (named pipes, a
`Listener` in each child). Nothing was built on it. Its problems:
- Windows `PipeConnection`s can't be awaited in asyncio, so the server
  needs a blocking reader thread per connection to bridge them.
- Latest-only preview has to be hand-built on a byte stream.
- Reconnect after a server restart is manual.
- pickle by default: opaque, Python-only, and only the authkey stands
  between a local process and arbitrary code execution.

## Decision
**ZeroMQ (`pyzmq`) over `tcp://127.0.0.1`, MessagePack-encoded control
messages.**

| Link | Sockets | Notes |
|---|---|---|
| Server ↔ engine control | engine binds `ROUTER`, server connects `DEALER` | engine replies to the server identity from the latest `hello` |
| Engine → server preview | engine binds `PUB`, server connects `SUB` | `CONFLATE` on the `SUB`: latest-only. A `PUB` never blocks (it drops at its HWM), so the engine never blocks. The `PUB` keeps the default HWM (Amendment) |
| Server ↔ worker jobs | worker binds `ROUTER`, server connects `DEALER` | same as engine control |

- **The child binds, the server connects**, so a restarted server just
  reconnects. ZeroMQ reconnects automatically in either start order.
- **Engine I/O happens in libzmq's own C thread**, outside the GIL; a send
  copies the bytes and returns.
- **asyncio-native on the server** via `zmq.asyncio`; no bridge threads.
- **Control messages:** the same pydantic models, dumped to dicts and
  encoded with MessagePack. No pickle.
- **Preview messages** are one frame each (`CONFLATE` does not support
  multipart): a small fixed header (frame index, track id) followed by the
  raw RGB bytes in geometry order.
- **Ports** are fixed defaults (one per link), overridable by config; the
  server passes them to the children at spawn, and a restarted server reads
  the same values. Binding to `127.0.0.1` raises no Windows Firewall
  prompt; a port clash fails at startup with a clear error.

### Connection lifecycle
ZeroMQ hides connections: no disconnect errors, and messages to an absent
peer are queued and delivered when it returns. Without care, a command sent
while the engine restarts would act late on fresh state. So:
1. **`ZMQ_IMMEDIATE` on the server's `DEALER`s:** nothing is queued while
   the peer is absent. Sends are non-blocking; `EAGAIN` means "peer down".
2. **`hello` handshake:** on connect the server sends `hello`; the child
   replies `hello` (its instance id and current state); the server then
   sends the full `configure` (engine) or re-submits the job (worker). This
   replaces "accept returned". The engine keeps no state that `configure`
   doesn't restore.
3. **Liveness:** the server watches its children's process handles for
   crashes; ZeroMQ heartbeats (`ZMQ_HEARTBEAT_IVL` / `_TIMEOUT`, e.g.
   1 s / 3 s) catch a hung peer. The engine's `status` stream doubles as a
   heartbeat. The worker exits when the server has been gone longer than
   the heartbeat timeout; the engine does not (it plays out current + next).

## Amendment (2026-11-11): no `SNDHWM` 1 on the preview `PUB`
The first version set `SNDHWM` 1 on the `PUB`. Measured with pyzmq 27.3
on localhost, a `PUB` with a small HWM drops the **last** frames of a
burst, which are the newest, even when the `SUB` keeps up. With 5,000
frames sent in a tight loop to a `CONFLATE` `SUB`, the last frame
received was 4951 of 4999 at HWM 1 and 2, 4954 at 4, 4966 at 16 and 4996
at 64. Only the default HWM (1000) delivered the last frame. With HWM 1
and a burst of 5 frames, the `SUB` received none of them. Latest-only
must deliver the newest frame, so the `PUB` keeps the default HWM:
- The engine still never blocks: a `PUB` drops at its HWM.
- Frames queue at the `PUB` only while the server process is frozen. The
  `SUB`'s I/O thread reads from TCP and conflates outside the server's
  GIL. The worst case is 1,000 frames (≈ 60 MB at 20k pixels).

`volu_gen/ipc/sockets.py` → `bind_pub`; test:
`tests/test_ipc.py::test_preview_is_latest_only`.

## Alternatives considered
- **`multiprocessing.connection`** — problems above.
- **Localhost TCP + asyncio streams** — no dependency, but framing,
  reconnect and latest-only are hand-built, and the engine needs its own
  event loop.
- **Shared memory for preview** (`multiprocessing.shared_memory`) — zero
  copy, but adds polling and torn-frame handling. Kept in reserve if the
  preview on/off timing test shows ZeroMQ adds jitter to the frame thread.
- **gRPC / HTTP between processes** — heavy, poor fit for a latest-only
  stream.

## Consequences
- New dependencies: `pyzmq`, `msgpack`. pyzmq is actively released
  (27.3.0, 2026-11-04; wheels for CPython 3.9–3.15 incl. free-threaded).
  The bundled libzmq is in maintenance mode (4.3.5, 2023-10); our use
  (DEALER/ROUTER, PUB/SUB, localhost TCP) is its most-used path, so slow
  upstream fixes are an accepted risk.
- Spike early: `zmq.asyncio` inside uvicorn on Windows (Proactor event
  loop), and engine frame timing with the preview on vs. off.
- ZeroMQ sockets are not thread-safe: the engine's control thread owns both
  engine sockets; the frame thread only writes the shared preview slot.
