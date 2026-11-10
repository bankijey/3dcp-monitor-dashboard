"""
Write the dashboard as a static site: the page's files, Plotly's script and one
JSON file per print, so a static host (GitHub Pages, a folder on a web server)
serves it without Python. The JSON files are what dashboard/server.py answers on
its GET routes, at the same paths, so the page does not know the difference.

Run:  python -m dashboard.export_static --out site      (PRINT_DATA chooses the data folder)
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from dashboard import data as D
from dashboard.server import PLOTLY_JS, STATIC


def dump(path: Path, obj) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, separators=(",", ":"), allow_nan=False)   # NaN would break the page
    path.write_text(text, encoding="utf-8")
    return len(text)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Write the print dashboard as a static site.")
    ap.add_argument("--out", default="site", help="target folder, replaced (default: site)")
    args = ap.parse_args(argv)
    out = Path(args.out).resolve()
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(STATIC, out)
    (out / "vendor").mkdir()
    shutil.copyfile(PLOTLY_JS, out / "vendor" / "plotly.min.js")
    (out / ".nojekyll").touch()                      # GitHub Pages: serve every file as is

    cat = D.catalogue()
    kept = []
    for row in cat["rows"]:
        eid = row["id"]
        keys = (("corpus", eid),)
        try:
            n = dump(out / "api" / "replay" / f"{eid}.json", D.replay(eid, keys))
        except ValueError as e:
            print(f"skipped {eid}: {e}")
            continue
        kept.append(row)
        print(f"{eid}: replay {n / 1e6:.2f} MB")
    cat["rows"] = kept
    dump(out / "api" / "catalogue.json", cat)
    site = D.site()
    dump(out / "api" / "site.json", site)
    if site["width_control"]:                        # the step test and its cross-sections
        n = dump(out / "api" / "width-control.json", D.width_control())
        lo, hi = (int(v) for v in D.ROI)
        spots = [(layer, x) for layer in D.SECTION_LAYERS for x in range(lo, hi + 1, int(D.X_BIN))]
        size = sum(dump(out / "api" / "width-control" / "profiles" / str(layer) / f"{x}.json",
                        D.scan_profiles(layer, x)) for layer, x in spots)
        print(f"step test: {n / 1e6:.2f} MB, {len(spots)} cross-sections {size / 1e6:.2f} MB")
    total = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    print(f"{out}: {len(kept)} prints, {total / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
