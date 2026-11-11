"""`volu serve --display <config>`: start the API server, which starts the
engine and the worker.

    uv run volu serve --display ../../config/displays/prototype.yaml

The display config is fixed for the server's lifetime; switching configs
takes a restart (docs/ui.md → Settings). `--connect-only` starts no
children: the server connects to an engine and a worker started by hand
(`python -m volu_gen.worker …`) on the same ports.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..analysis import default_data_dir
from ..geometry import DisplayConfigError
from ..ipc import DEFAULT_HOST, ENGINE_CONTROL_PORT, ENGINE_PREVIEW_PORT, WORKER_PORT

DEFAULT_PORT = 8770
# zmq.asyncio needs add_reader, which uvicorn's default Windows loop (Proactor) lacks.
LOOP = "asyncio:SelectorEventLoop"


def main(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(prog="volu", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the API server")
    serve.add_argument("--display", type=Path, required=True, help="display config (config/displays/*.yaml)")
    serve.add_argument("--data", type=Path, default=default_data_dir(), help="data dir (volu.db, tracks/)")
    serve.add_argument("--host", default=DEFAULT_HOST, help="HTTP address (the IPC stays on localhost)")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument("--engine-port", type=int, default=ENGINE_CONTROL_PORT, help="engine control")
    serve.add_argument("--preview-port", type=int, default=ENGINE_PREVIEW_PORT, help="engine preview")
    serve.add_argument("--worker-port", type=int, default=WORKER_PORT)
    serve.add_argument("--connect-only", action="store_true",
                       help="start no engine or worker; connect to ones started by hand")
    serve.add_argument("--no-ping", action="store_true", help="no node detection (nodes stay offline)")
    args = ap.parse_args(argv)

    import uvicorn

    from .app import create_app
    from .core import Runtime, Server

    runtime = Runtime(engine_control_port=args.engine_port, engine_preview_port=args.preview_port,
                      worker_port=args.worker_port, spawn=not args.connect_only, ping=not args.no_ping)
    try:
        server = Server.open(args.display.resolve(), args.data.resolve(), runtime)
    except (DisplayConfigError, OSError) as e:
        sys.exit(f"volu serve: {e}")
    for p in server.display.problems:
        print(f"display config problem: {p}", file=sys.stderr)
    try:
        uvicorn.run(create_app(server), host=args.host, port=args.port, loop=LOOP)
    finally:
        server.close()


if __name__ == "__main__":
    main()
