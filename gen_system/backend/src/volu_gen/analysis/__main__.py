"""Analyze audio files into the per-track data dirs.

    uv run python -m volu_gen.analysis <audio files or folders> [--check]
    uv run python -m volu_gen.analysis <file> --override 160 0.889
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .pipeline import STAGES, analyze, set_override
from .track import TrackDir, default_data_dir

AUDIO_EXTS = {".wav", ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".aiff", ".aif"}


def find_tracks(inputs: list[str]) -> list[Path]:
    tracks = []
    for item in inputs:
        p = Path(item)
        if p.is_dir():
            tracks += sorted(f for f in p.rglob("*") if f.suffix.lower() in AUDIO_EXTS)
        elif p.is_file():
            tracks.append(p)
        else:
            sys.exit(f"not found: {item}")
    return tracks


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", help="audio files or folders")
    ap.add_argument("--data", type=Path, default=default_data_dir(), help="data dir (track data goes in <data>/tracks)")
    ap.add_argument("--stages", default=",".join(STAGES), help=f"comma list from {STAGES}")
    ap.add_argument("--force", default="", help="comma list of stages to recompute even if already done")
    ap.add_argument("--device", default=None)
    ap.add_argument("--vram-limit-gb", type=float, default=0.0,
                    help="cap this process's VRAM, e.g. 8 to emulate the target card (0 = no cap)")
    ap.add_argument("--check", action="store_true", help="write click track + Audacity labels per track")
    ap.add_argument("--override", nargs=2, type=float, metavar=("BPM", "DOWNBEAT_S"),
                    help="set a user grid (BPM + time of a downbeat) on one analyzed track")
    ap.add_argument("--clear-override", action="store_true")
    args = ap.parse_args()

    tracks = [TrackDir(p, args.data) for p in find_tracks(args.inputs)]

    if args.override or args.clear_override:
        if len(tracks) != 1 or "tempo" not in tracks[0].data:
            sys.exit("--override needs exactly one already-analyzed track")
        bpm, offset = args.override if args.override else (None, None)
        set_override(tracks[0], bpm, offset)
    else:
        import torch
        device = args.device or ("cuda" if torch.cuda.is_available() else "cpu")
        if device == "cuda" and args.vram_limit_gb:
            total = torch.cuda.get_device_properties(0).total_memory / 1e9
            torch.cuda.set_per_process_memory_fraction(min(1.0, args.vram_limit_gb / total))
            print(f"VRAM capped at {args.vram_limit_gb} GB of {total:.1f} GB")
        stats = analyze(tracks, [s for s in args.stages.split(",") if s],
                        {s for s in args.force.split(",") if s}, device)
        for name, st in stats.items():
            print(f"{name}: " + ", ".join(
                f"{k} {v['seconds']} s/{v.get('peak_vram_gb', '-')} GB{' ERR' if 'error' in v else ''}"
                for k, v in st.items()))

    if args.check:
        from .checks import write_checks
        for t in tracks:
            if "tempo" in t.data:
                write_checks(t)
                t.drop_audio()
    for t in tracks:
        tempo = t.data.get("tempo", {})
        print(f"{t.name}: {tempo.get('mode')} {tempo.get('bpm')} BPM, "
              f"{len(t.data.get('phrases', []))} phrases -> {t.dir}")


if __name__ == "__main__":
    main()
