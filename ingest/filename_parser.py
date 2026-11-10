"""
Parse 3DCP experiment filenames into structured metadata.

Canonical pattern (double-underscore separated tokens):
    2000-01-01__BOARD__F10000__S0__W40__run_0.csv

Older conventions found in the corpus are handled too:
    2000-01-02__1114__Mix_A__9000__0__5.6__3   date, HHMM time, board,
                                                    then bare F S W run
    2000-01-03__Mix_A__9000__0__6.5__0              same, without the time
    2000-01-04T08_19_0000000Z__Mix_A__F9000__...    date with a time suffix
    test__F9000__S0__W40__run_1                     no date (the ingest fills
                                                    it from the folder or data)

Token meaning:
    <date>     ISO date YYYY-MM-DD, optionally followed by a time suffix
    <board>    specimen / substrate label (free text)
    F<num>     feed rate / flow setpoint
    S<num>     setSpeed setpoint
    W<num>     water / width setpoint (may be decimal, e.g. W6.5)
    run_<int>  repetition index

The parser is deliberately tolerant: unknown tokens are kept in `extra`, so a
new naming drift never breaks ingest.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import date
from pathlib import Path
from typing import Any

_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(.*)$")
_HHMM_RE = re.compile(r"^\d{4}$")
_RUN_RE = re.compile(r"^run[_-]?(\d+)(.*)$", re.IGNORECASE)   # run_4, run2, run_4_OFF
_PREFIXED_RE = re.compile(r"^([FSW])(-?\d+(?:\.\d+)?)$")
_NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?$")

_PREFIX_FIELD = {"F": "feed", "S": "set_speed", "W": "width"}
# old convention: bare numbers after the board, in this order
_POSITIONAL = ("feed", "set_speed", "width", "run")
_INT_FIELDS = ("feed", "set_speed", "run")


@dataclass
class ExperimentMeta:
    filename: str
    stem: str
    exp_date: date | None = None
    board: str | None = None
    feed: int | None = None
    set_speed: int | None = None
    width: float | None = None
    run: int | None = None
    extra: dict[str, str] = field(default_factory=dict)
    parse_ok: bool = True
    parse_warnings: list[str] = field(default_factory=list)

    def as_row(self) -> dict[str, Any]:
        d = asdict(self)
        d["exp_date"] = self.exp_date.isoformat() if self.exp_date else None
        d["extra"] = ";".join(f"{k}={v}" for k, v in self.extra.items())
        d["parse_warnings"] = ";".join(self.parse_warnings)
        return d


def _set_number(meta: ExperimentMeta, fname: str, text: str) -> None:
    v = float(text)
    if fname in _INT_FIELDS:
        if not v.is_integer():
            meta.parse_warnings.append(f"non-integer {fname} {text} rounded")
        v = int(round(v))
    setattr(meta, fname, v)


def parse_experiment_filename(path: str | Path) -> ExperimentMeta:
    p = Path(path)
    stem = p.stem
    meta = ExperimentMeta(filename=p.name, stem=stem)

    tokens = stem.split("__")
    if len(tokens) < 2:
        meta.parse_warnings.append("fewer than 2 tokens; non-standard filename")
    # Old names carry bare numbers (F S W run by position); new names prefix
    # every value, so a bare number there is a label (e.g. board '123456').
    prefixed = any(_PREFIXED_RE.match(t) for t in tokens)

    bare: list[str] = []
    for i, tok in enumerate(tokens):
        if i == 0:
            m = _DATE_RE.match(tok)
            if m:
                try:
                    meta.exp_date = date(int(m[1]), int(m[2]), int(m[3]))
                except ValueError:
                    meta.parse_warnings.append(f"bad date token: {tok}")
                if m[4]:
                    meta.extra["time"] = m[4].lstrip("T_")
                continue

        if i == 1 and meta.exp_date and _HHMM_RE.match(tok):
            meta.extra["time"] = tok                      # 2000-01-02__1114__...
            continue

        rm = _RUN_RE.match(tok)
        if rm:
            meta.run = int(rm[1])
            if rm[2].strip(" _-"):
                meta.extra["run_suffix"] = rm[2].strip(" _-")   # e.g. OFF, Copy
            continue

        pm = _PREFIXED_RE.match(tok)
        if pm:
            _set_number(meta, _PREFIX_FIELD[pm[1]], pm[2])
            continue

        is_num = bool(_NUM_RE.match(tok))
        if is_num and not prefixed and meta.board is not None:
            bare.append(tok)                              # old positional values
            continue

        if meta.board is None and not (is_num and not prefixed):
            meta.board = tok
            continue

        meta.extra[f"tok{i}"] = tok

    # old convention: bare numbers after the board fill feed, set_speed, width,
    # run in that order (only the fields a prefixed token did not already set)
    free = [f for f in _POSITIONAL if getattr(meta, f) is None]
    for j, tok in enumerate(bare):
        if j < len(free):
            _set_number(meta, free[j], tok)
        else:
            meta.extra[f"num{j}"] = tok

    if meta.exp_date is None:
        meta.parse_warnings.append("no date in filename")
    meta.parse_ok = not meta.parse_warnings
    return meta


if __name__ == "__main__":
    for name in [
        "2000-01-01__BOARD__F10000__S0__W40__run_0.csv",
        "2000-01-02__1114__Mix_A__9000__0__5.6__3.csv",
        "2000-01-03__Mix_A__9000__0__6.5__0.csv",
        "2000-01-03__Mix_A__F9000__S0__W6.5__run2.csv",
        "2000-01-04T08_19_0000000Z__Mix_A__F9000__S0__W5.5__run_0.json",
        "test__F9000__S0__W40__run_1.csv",
    ]:
        m = parse_experiment_filename(name)
        print(name)
        for k, v in m.as_row().items():
            print(f"   {k:16} {v}")
        print()


# shape families from the object word in the board name (German and English)
_SHAPES = [
    ("bucket", r"kuebel|kübel|eimer|bucket"),
    ("chair", r"chair|stuhl"),
    ("cylinder", r"cyl|cynl|zylind|rohr"),
    ("arc", r"arc|bogen"),
    ("board", r"board"),
    ("stone", r"stone|stein"),
    ("border", r"umrand|umdre"),
    ("beam", r"beam|balken"),
    ("rectangle", r"rect"),
    ("pyramid", r"pyr"),
    ("lines", r"line"),
]


def shape_family(board: str | None) -> str:
    """Printed object family from the board name: board, arc, chair, cylinder,
    stone, border, beam, rectangle, pyramid, lines, bucket (several joined with
    '+', e.g. 'bucket+chair'), or 'unknown' when the name holds no object word
    (material names, dates, tests)."""
    name = (board or "").lower()
    hits = [fam for fam, rx in _SHAPES if re.search(rx, name)]
    return "+".join(hits) if hits else "unknown"
