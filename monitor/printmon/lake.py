"""
Summarise the storage layer with DuckDB -- what was recorded, at what rate, how complete.

    python -m printmon lake                 all sessions
    python -m printmon lake --origin live   lab recordings only

DuckDB queries the Parquet files in place: no server, no load step. The same
SQL works unchanged if the lake later moves to object storage.
"""

import argparse
import sys
from pathlib import Path

from . import config


def _print_table(rel):
    """Plain-ASCII table: DuckDB's .show() uses box-drawing characters that the
    Windows console code page (cp1252) cannot encode."""
    cols = rel.columns
    rows = [["" if v is None else str(v) for v in r] for r in rel.fetchall()]
    widths = [max([len(c)] + [len(r[i]) for r in rows]) for i, c in enumerate(cols)]
    line = "  ".join(c.ljust(w) for c, w in zip(cols, widths, strict=True))
    print(line)
    print("-" * len(line))
    for r in rows:
        print("  ".join(v.rjust(w) for v, w in zip(r, widths, strict=True)))


def _glob(root, stream):
    return (Path(root) / "bronze" / stream / "**" / "*.parquet").as_posix()


def main(argv=None):
    p = argparse.ArgumentParser(prog="printmon lake", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--lake", default=None)
    p.add_argument("--origin", default=None, help="live | replay")
    opts = p.parse_args(argv)

    import duckdb

    root = Path(opts.lake or config.LAKE_DIR)
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")  # otherwise TIMESTAMPTZ casts render in local time
    where = "WHERE origin = '{}'".format(opts.origin) if opts.origin else ""

    queries = {
        "process": """
            SELECT origin, session, src,
                   count(*)                                             AS rows,
                   min(t)::TIMESTAMP                                    AS first_utc,
                   round(epoch(max(t)) - epoch(min(t)), 1)              AS span_s,
                   round(count(*) / nullif(epoch(max(t)) - epoch(min(t)), 0), 1) AS hz,
                   -- acquisition-to-storage latency; meaningless for replays (old data, new clock)
                   round(median(epoch_ns(recv_t) - epoch_ns(t))
                         FILTER (WHERE origin = 'live') / 1e6, 2)       AS median_lag_ms
            FROM read_parquet('{glob}', hive_partitioning = true)
            {where}
            GROUP BY ALL ORDER BY first_utc, src
        """,
        "scan": """
            SELECT origin, session,
                   count(*)                                             AS profiles,
                   min(t)::TIMESTAMP                                    AS first_utc,
                   round(epoch(max(t)) - epoch(min(t)), 1)              AS span_s,
                   round(count(*) / nullif(epoch(max(t)) - epoch(min(t)), 0), 1) AS hz,
                   coalesce(sum(dropped), 0)                            AS dropped,
                   round(avg(width), 2)                                 AS mean_width_mm,
                   round(sum(len(x)) / count(*), 0)                     AS pts_per_profile
            FROM read_parquet('{glob}', hive_partitioning = true)
            {where}
            GROUP BY ALL ORDER BY first_utc
        """,
    }
    found = False
    for stream, sql in queries.items():
        files = list((root / "bronze" / stream).rglob("*.parquet"))
        if not files:
            continue
        found = True
        size = sum(f.stat().st_size for f in files) / 1e6
        print("\n== {}  ({} files, {:.1f} MB)".format(stream, len(files), size))
        _print_table(con.sql(sql.format(glob=_glob(root, stream), where=where)))
    if not found:
        print("nothing recorded yet under {}".format(root / "bronze"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
