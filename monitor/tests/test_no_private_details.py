"""Site details belong in .env, not in tracked files. This guards every tracked
file of the repository, documents included, against equipment addresses, OPC UA
node ids, literal IP addresses and e-mail addresses."""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent      # the repository root

# Written as regexes so this file does not match its own checks.
PATTERNS = {
    "equipment address": re.compile(r"opc\.tcp://\d"),
    "OPC UA node id": re.compile(r"ns=\d+;[sig]="),
    "IP address": re.compile(r"\b(?!127\.0\.0\.1\b)(?!0\.0\.0\.0\b)(?:\d{1,3}\.){3}\d{1,3}\b"),
    "e-mail address": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
}
SKIP_SUFFIXES = (".png", ".parquet", ".ico", ".mp4")
ALLOWED = {"publica@bam.de", "zenodo@bam.de"}                 # public contact addresses


def test_no_private_details_in_tracked_files():
    tracked = subprocess.run(["git", "ls-files"], capture_output=True, text=True, cwd=ROOT)
    if tracked.returncode != 0:
        pytest.skip("not a git checkout")
    offenders = []
    for name in tracked.stdout.split():
        path = ROOT / name
        if not path.is_file() or path.suffix in SKIP_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line in text.splitlines():
            for what, pattern in PATTERNS.items():
                hits = [h for h in pattern.findall(line) if h not in ALLOWED]
                if hits:
                    offenders.append("{} ({}): {}".format(name, what, line.strip()[:90]))
    assert not offenders, "private details in tracked files:\n" + "\n".join(offenders)
