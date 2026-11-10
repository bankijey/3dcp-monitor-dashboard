"""
Normalisation of every input format into two record types.

Live frames (inputs_fast / scan_fast websockets) and recorded files (Node-RED
CSV / NDJSON) use different field names and encodings. Everything downstream --
viewer, recorder, tests -- consumes only these normalised records, so it cannot
tell live from replay.

Normalisation is lossless: any input key without a dedicated field is kept in
`extra`, so a new field added upstream is stored, never silently dropped.

ProcessRecord (dict)
    t            int      host clock, ns since epoch
    src          str|None pump | gantry | nc for live frames; None for replay rows
                          (a replay row carries every field at once)
    seq          int|None frame sequence number from inputs_fast
    torque, water_flow, water_temp          pump, raw controller units
    P0..P3, T0, T1                          gantry I/O, raw counts
    water_pump, solenoid_valve, mixer       pump actuator states
    pos, vel     tuple[5]|None              NC axis position (mm) / velocity
    start, water_valve                      operator commands (Node-RED CSV only)
    set_speed                               G-code S word (live: NC spindle override)
    extra        dict                       everything else, verbatim

ScanRecord (dict)
    t            int      host clock, ns
    x, z         float32 ndarray            profile, mm; z is height-like
    m0           uint32 ndarray|None        per-point intensity maximum
    width        float|None                 bead width from the scanner service
    profile_count, dropped, lost_profiles   int|None   sensor counter / losses
    shutter_open, shutter_close             float|None sensor hardware clock
    pos          tuple|None                 X, Y, Z merged in by Node-RED (replay only)
    extra        dict                       everything else, verbatim
"""

import json

import numpy as np

PUMP_FIELDS = ("torque", "water_flow", "water_temp")
GANTRY_FIELDS = ("P0", "P1", "P2", "P3", "T0", "T1")
ACTUATOR_FIELDS = ("water_pump", "solenoid_valve", "mixer")
COMMAND_FIELDS = ("start", "set_speed", "water_valve")

_PROCESS_KEYS = set(PUMP_FIELDS + GANTRY_FIELDS + ACTUATOR_FIELDS + COMMAND_FIELDS) | {
    "timestamp", "src", "seq", "pos", "vel"}
_SCAN_KEYS = {"time", "x", "z", "m0", "width", "profile_count", "dropped",
              "lost_profiles", "shutter_open", "shutter_close", "X", "Y", "Z", "C"}

# Node-RED CSV column -> normalised name
_CSV_RENAME = {
    "waterFlow": "water_flow",
    "waterTemp": "water_temp",
    "setSpeed": "set_speed",
    "waterValve": "water_valve",
    "waterPump": "water_pump",
    "solenoidValve": "solenoid_valve",
}


def _num(v):
    if v is None or v == "" or isinstance(v, bool):
        return None if not isinstance(v, bool) else float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v):
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _vec(v):
    if v is None or v == "":
        return None
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return None
    if isinstance(v, (list, tuple)):
        try:
            return tuple(float(a) for a in v)
        except (TypeError, ValueError):
            return None
    return None


def _first(v):
    """NC values arrive as one-element arrays (e.g. the spindle override); take the value."""
    if isinstance(v, (list, tuple)):
        return v[0] if v else None
    return v


def _bool(v):
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() == "true"
    return bool(v)


def _process(src_map, t, src):
    rec = {"t": int(t), "src": src, "seq": _int(src_map.get("seq"))}
    for k in PUMP_FIELDS + GANTRY_FIELDS:
        rec[k] = _num(src_map.get(k))
    for k in ACTUATOR_FIELDS:
        rec[k] = _bool(src_map.get(k))
    rec["pos"] = _vec(src_map.get("pos"))
    rec["vel"] = _vec(src_map.get("vel"))
    rec["start"] = _bool(src_map.get("start"))
    rec["set_speed"] = _num(_first(src_map.get("set_speed")))
    rec["water_valve"] = _num(src_map.get("water_valve"))
    rec["extra"] = {k: v for k, v in src_map.items() if k not in _PROCESS_KEYS}
    return rec


def process_from_live(frame):
    """A frame from ws://.../8001 (inputs_fast.py)."""
    return _process(frame, frame["timestamp"], frame.get("src"))


def process_from_csv(row):
    """A row (dict from csv.DictReader) of Node-RED's pump CSV."""
    row = {_CSV_RENAME.get((k or "").strip(), (k or "").strip()): v for k, v in row.items()}
    return _process(row, row["timestamp"], None)


def scan_from_frame(frame):
    """A scan frame -- live from scan_fast.py or a line of Node-RED's scan NDJSON."""
    x = np.asarray(frame.get("x") or (), dtype=np.float32)
    z = np.asarray(frame.get("z") or (), dtype=np.float32)
    m0 = frame.get("m0")
    pos = None
    if "X" in frame and "Y" in frame:
        pos = (_num(frame.get("X")), _num(frame.get("Y")), _num(frame.get("Z")))
        if pos[0] is None or pos[1] is None:
            pos = None
    return {
        "t": int(frame["time"]),
        "x": x,
        "z": z,
        "m0": np.asarray(m0, dtype=np.uint32) if m0 else None,
        "width": _num(frame.get("width")),
        "profile_count": _int(frame.get("profile_count")),
        "dropped": _int(frame.get("dropped")),
        "lost_profiles": _int(frame.get("lost_profiles")),
        "shutter_open": _num(frame.get("shutter_open")),
        "shutter_close": _num(frame.get("shutter_close")),
        "pos": pos,
        "extra": {k: v for k, v in frame.items() if k not in _SCAN_KEYS},
    }


def peek_scan_time(line):
    """
    Timestamp of an NDJSON scan line without parsing the whole line.

    Scan lines are ~8 KB; replay only needs the full parse for frames it
    actually emits. Node-RED writes "time" as the first key -- fall back to a
    full parse if that ever changes.
    """
    head = line[:64]
    i = head.find('"time":')
    if i >= 0:
        j = k = i + 7
        while k < len(head) and (head[k].isdigit() or head[k] == " "):
            k += 1
        digits = head[j:k].strip()
        if digits and k < len(head):
            return int(digits)
    return int(json.loads(line)["time"])
