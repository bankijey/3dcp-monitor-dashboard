"""
printmon command line.

    python -m printmon view    [--sim ...]     live monitor / simulation
    python -m printmon record  [--sim ...]     full-rate storage (bronze Parquet)
    python -m printmon emulate [--run ...]     fake inputs_fast/scan_fast from a recorded run
    python -m printmon runs                    list recorded runs available for replay
    python -m printmon lake                    summarise what has been recorded
"""

import sys


def _runs(argv):
    from .sources import discover_runs, run_time_bounds

    for r in discover_runs():
        first, last = run_time_bounds(r)
        dur = (last - first) / 1e9 if first else 0
        sizes = sum(p.stat().st_size for p in (r.pump, r.scan) if p) / 1e6
        print("{:<48} {:>7.0f} s  {:>8.0f} MB  [{}]".format(
            r.name, dur, sizes, "+".join(r.streams)))
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv.pop(0) if argv else "help"
    if cmd == "view":
        from .viewer import main as view_main
        return view_main(argv)
    if cmd == "record":
        from .recorder import main as record_main
        return record_main(argv)
    if cmd == "runs":
        return _runs(argv)
    if cmd == "emulate":
        from .emulate import main as emulate_main
        return emulate_main(argv)
    if cmd == "lake":
        from .lake import main as lake_main
        return lake_main(argv)
    print(__doc__)
    return 0 if cmd in ("help", "-h", "--help") else 2


if __name__ == "__main__":
    sys.exit(main())
