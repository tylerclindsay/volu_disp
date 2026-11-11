"""`volu serve --display <config>`: start the API server.

    uv run volu serve --display ../../config/displays/prototype.yaml

The display config is fixed for the server's lifetime; switching configs
takes a restart (docs/ui.md → Settings).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..analysis import default_data_dir
from ..geometry import DisplayConfigError

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8770


def main(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(prog="volu", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the API server")
    serve.add_argument("--display", type=Path, required=True, help="display config (config/displays/*.yaml)")
    serve.add_argument("--data", type=Path, default=default_data_dir(), help="data dir (volu.db, tracks/)")
    serve.add_argument("--host", default=DEFAULT_HOST)
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args(argv)

    import uvicorn

    from .app import Server, create_app

    try:
        server = Server.open(args.display.resolve(), args.data)
    except (DisplayConfigError, OSError) as e:
        sys.exit(f"volu serve: {e}")
    for p in server.display.problems:
        print(f"display config problem: {p}", file=sys.stderr)
    try:
        uvicorn.run(create_app(server), host=args.host, port=args.port)
    finally:
        server.close()


if __name__ == "__main__":
    main()
