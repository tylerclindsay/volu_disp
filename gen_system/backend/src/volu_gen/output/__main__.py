"""Send a test pattern to the display (protocol v2) or to fake nodes.

    uv run python -m volu_gen.output ../../config/displays/prototype.yaml --pattern nodes
    uv run python -m volu_gen.output ../../config/displays/prototype.yaml --loopback 9000

Patterns: `nodes` (one solid color per drive node: checks the mapping),
`rainbow` (hue by height, scrolling), `sweep` (a plane crossing x, then
y, then z: checks axes and odd-slice offsets). Timing is a plain sleep
loop: a test tool, not the engine's audio clock.
"""

import argparse
import colorsys
import time

import numpy as np

from ..geometry import load_display_config
from ..mapping import Mapping
from .sender import Sender


def pattern_nodes(m: Mapping, t: float) -> np.ndarray:
    colors = np.array([colorsys.hsv_to_rgb(k / len(m.nodes), 1, 1) for k in range(len(m.nodes))])
    return colors[m.node_of]


def pattern_rainbow(m: Mapping, t: float) -> np.ndarray:
    y = m.geometry.positions[:, 1]
    h = (y / max(np.ptp(y), 1) + 0.25 * t) % 1.0
    return np.stack([colorsys.hsv_to_rgb(v, 1, 1) for v in h])


def pattern_sweep(m: Mapping, t: float) -> np.ndarray:
    """A soft plane, one pixel pitch wide, crossing each axis in 2 s."""
    pos = m.geometry.positions
    axis = int(t // 2) % 3
    lo, hi = pos[:, axis].min(), pos[:, axis].max()
    at = lo + (t % 2) / 2 * (hi - lo)
    pitch = (m.config.geometry.strip_spacing, m.config.geometry.pixel_spacing,
             m.config.geometry.slice_spacing)[axis]
    v = np.clip(1 - np.abs(pos[:, axis] - at) / pitch, 0, 1)
    return np.outer(v, [(1, 0.2, 0.2), (0.2, 1, 0.2), (0.2, 0.2, 1)][axis])


PATTERNS = {"nodes": pattern_nodes, "rainbow": pattern_rainbow, "sweep": pattern_sweep}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("config", help="display config (config/displays/*.yaml)")
    ap.add_argument("--pattern", choices=PATTERNS, default="sweep")
    ap.add_argument("--fps", type=int, help="display FPS (default: the config's)")
    ap.add_argument("--brightness", type=float, default=0.25, help="0–1 (default 0.25)")
    ap.add_argument("--loopback", type=int, metavar="BASE_PORT",
                    help="send to fake nodes at 127.0.0.1:BASE_PORT+k (tools/fake_node.py)")
    ap.add_argument("--seconds", type=float, help="stop after this long (default: run until Ctrl+C)")
    ap.add_argument("--bind", help="local IP of the display network interface")
    ap.add_argument("--broadcast", help="SYNC address (default: the nodes' /24 broadcast)")
    args = ap.parse_args()

    mapping = Mapping(load_display_config(args.config))
    fps = args.fps or mapping.config.default_fps
    mapping.check_fps(fps)
    sender = (Sender.loopback(mapping, args.loopback) if args.loopback is not None
              else Sender.for_display(mapping, args.bind, args.broadcast))
    fn = PATTERNS[args.pattern]
    targets = ", ".join(f"{h}:{p}" for h, p in sender.targets)
    print(f"{args.pattern} at {fps} FPS to {targets}; SYNC to "
          f"{', '.join(f'{h}:{p}' for h, p in sender.sync_targets)}. Ctrl+C to stop.")

    period = 1 / fps
    t0 = next_t = time.perf_counter()
    try:
        with sender:
            while True:
                t = time.perf_counter() - t0
                if args.seconds is not None and t >= args.seconds:
                    break
                rgb = np.clip(fn(mapping, t) * args.brightness, 0, 1)
                sender.send_frame((rgb * 255 + 0.5).astype(np.uint8))
                next_t += period
                delay = next_t - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
                else:
                    next_t = time.perf_counter()
    except KeyboardInterrupt:
        pass
    print(f"sent {sender.stats['frames']} frames, {sender.stats['packets']} packets, "
          f"{sender.stats['send_errors']} send errors")


if __name__ == "__main__":
    main()
