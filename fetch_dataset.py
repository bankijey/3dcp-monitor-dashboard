"""
Download the published dataset from Zenodo into a folder, checking every file's
md5 against the record, and unpack zip archives. Files already there with the
right md5 are not downloaded again.

Run:  python fetch_dataset.py <record id, DOI or record URL> <folder>
      python fetch_dataset.py 10.5281/zenodo.1234567 dataset
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import urllib.request
import zipfile
from pathlib import Path

API = "https://zenodo.org/api/records/"


def record_id(ref: str) -> str:
    """The record number: the last number in a record id, DOI or record URL."""
    return re.findall(r"\d+", ref.split("?")[0])[-1]


def md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(ref: str, folder: str) -> None:
    out = Path(folder)
    out.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(API + record_id(ref)) as r:
        rec = json.load(r)
    for f in rec["files"]:
        name, want = f["key"], f["checksum"].split(":", 1)[-1]
        dst = out / name
        if not dst.exists() or md5(dst) != want:
            print(f"downloading {name} ({f['size'] / 1e6:.1f} MB)")
            urllib.request.urlretrieve(f["links"]["self"], dst)
        got = md5(dst)
        if got != want:
            sys.exit(f"{name}: md5 {got} does not match the record's {want}")
        print(f"{name}: md5 ok")
        if dst.suffix == ".zip":
            with zipfile.ZipFile(dst) as z:
                z.extractall(out)
    print(f"dataset in {out.resolve()}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
