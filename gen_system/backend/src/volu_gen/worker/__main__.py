"""Run the worker: one process, one job at a time, below-normal priority.

    uv run python -m volu_gen.worker --display ../../config/displays/prototype.yaml

The API server starts it with the server's display config, data dir and
port. Run by hand (for development), connect to it with a DEALER on the
port; `--server-timeout 0` keeps it running with no server.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from ..analysis import default_data_dir
from ..geometry import DisplayConfigError, Geometry, load_display_config
from ..ipc import DEFAULT_HOST, HEARTBEAT_TIMEOUT_MS, WORKER_PORT, PortInUse
from .worker import Worker, lower_priority


def main(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(prog="python -m volu_gen.worker", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--display", type=Path, required=True, help="display config (config/displays/*.yaml)")
    ap.add_argument("--data", type=Path, default=default_data_dir(), help="data dir (tracks/)")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=WORKER_PORT)
    ap.add_argument("--server-timeout", type=float, default=HEARTBEAT_TIMEOUT_MS / 1000,
                    help="exit after this many s with no server connected (0: never)")
    ap.add_argument("--device", default=None, help="torch device for analysis (default: cuda if available)")
    args = ap.parse_args(argv)

    try:
        geometry = Geometry.from_config(load_display_config(args.display))
    except (DisplayConfigError, OSError) as e:
        sys.exit(f"worker: {e}")
    lower_priority()
    try:
        worker = Worker(args.data, geometry, args.port, args.host, server_timeout=args.server_timeout,
                        device=args.device)
    except PortInUse as e:
        sys.exit(f"worker: {e}")
    print(f"worker: pid {os.getpid()} on {args.host}:{args.port}, geometry {geometry.hash}, data {args.data}",
          file=sys.stderr, flush=True)
    try:
        worker.run()
    except KeyboardInterrupt:
        pass
    alive = worker.job_alive
    worker.close()
    if alive:   # a job thread can't be stopped; its .tmp file is cleaned up by the server
        os._exit(0)


if __name__ == "__main__":
    main()
