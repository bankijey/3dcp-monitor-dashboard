"""
Data model of the 3DCP corpus: a star schema with one dimension table and
three fact tables, all keyed by experiment_id (facts also by event_time).

  dim_experiment  grain: one row per experiment          DuckDB table (catalog)
  fct_pump        grain: one row per pump sample         Parquet dataset pump/
  fct_motion      grain: one row per motion update       Parquet dataset motion/
  fct_scan        grain: one row per laser profile       Parquet dataset scan/

Each source device (pump logger, motion controller, laser scanner) samples on
its own clock, so each gets its own fact table; they are combined at query
time with an as-of join on event_time rather than stored pre-joined.

Conventions
  * lower snake_case identifiers (SQL is case-insensitive: 'X' and 'x' clash)
  * event_time  TIMESTAMP(ns, UTC): when the source took the sample; NULL when
    the logger clock was unset (epoch 0)
  * *_raw       sensor value exactly as logged (ADC counts etc.). Its physical
                meaning depends on the sensor era, so calibrate before
                comparing eras (see dim_experiment.sensor_era)
  * no suffix   already in physical units (units in the column description)
  * conformed schema: every file of a table has every column; a channel or
    axis a machine did not have in that period is NULL (never NaN)

Storage layout (Hive-style date partitions; experiment_id also a column):
  data/<dataset>/experiment_date=YYYY-MM-DD/<experiment_id>.parquet
"""
from __future__ import annotations

import re

import pyarrow as pa

MODEL_VERSION = "1.2"        # stored in every Parquet file's metadata

TS = pa.timestamp("ns", tz="UTC")
F64 = pa.float64()
LIST_F32 = pa.list_(pa.float32())
STR = pa.string()
BOOL = pa.bool_()

KEY_COLUMNS = [
    ("experiment_id", STR, "Experiment key, joins to dim_experiment"),
    ("event_time", TS, "When the source took the sample (UTC); NULL if the "
                       "logger clock was unset"),
    ("sample_seq", pa.int64(), "0-based position of the sample in its source log "
                               "(logging order); orders samples whose timestamps "
                               "tie or are missing. fct_motion keeps the pump-log "
                               "position it was read from"),
]

# ------------------------------------------------------------------ fct_pump
PUMP_COLUMNS = KEY_COLUMNS + [
    ("start", BOOL, "Print program running"),
    ("set_speed", F64, "Pump speed setpoint as logged"),
    ("water_valve", F64, "Water valve setpoint (%)"),
    ("torque_raw", F64, "Pump torque, raw counts. % = 100*raw/65535 (all eras)"),
    ("water_flow_raw", F64, "Water flow, raw counts. L/min = 20.4*raw/65535"),
    ("water_temp_raw", F64, "Water temperature as logged: degC floats in 2023, "
                            "from 2024 integer counts of 1/1000 degC (degC = raw / 1000)"),
    ("p0_raw", F64, "Pressure P0 (2024-02+), raw counts. bar = "
                    "1.25*40*(raw-6000)/24000; 4-20 mA loop, <5400 = sensor fault"),
    ("p1_raw", F64, "Pressure P1, raw. new era bar = 40*raw/30000; "
                    "old era bar = 4*raw"),
    ("p2_raw", F64, "Pressure P2, raw. new era bar = 40*raw/30000; "
                    "old era bar = raw*40000/65536"),
    ("p3_raw", F64, "Pressure P3 (2024-02+), raw. bar = 40*raw/30000"),
    ("t0_raw", F64, "Temperature T0 (2024-02+), raw. degC = raw/10"),
    ("t1_raw", F64, "Temperature T1, raw. new era degC = raw/10; "
                    "old era degC = 5*raw"),
    ("water_pump", BOOL, "Water pump on"),
    ("solenoid_valve", BOOL, "Solenoid valve open"),
    ("mixer", BOOL, "Mixer (dry-mortar feed) running"),
]
# source log column -> fct_pump column
PUMP_SOURCE_NAMES = {
    "start": "start", "setSpeed": "set_speed", "waterValve": "water_valve",
    "torque": "torque_raw", "waterFlow": "water_flow_raw",
    "waterTemp": "water_temp_raw", "P0": "p0_raw", "P1": "p1_raw",
    "P2": "p2_raw", "P3": "p3_raw", "T0": "t0_raw", "T1": "t1_raw",
    "waterPump": "water_pump", "solenoidValve": "solenoid_valve",
    "mixer": "mixer",
}
# raw values at or above this are a dead / saturated channel
SENTINEL = 32768

# ---------------------------------------------------------------- fct_motion
# The source pos[] / vel[] arrays are ordered [X, Y, Z, C, B]; 3-axis logs
# (2023-05 .. 2025-01) have X, Y, Z only, so c and b are NULL there.
AXES = ("x", "y", "z", "c", "b")
_AXIS_DOC = {"x": "X", "y": "Y", "z": "Z", "c": "C (rotary)", "b": "B (rotary)"}
MOTION_COLUMNS = KEY_COLUMNS + [
    (f"pos_{a}", F64, f"{_AXIS_DOC[a]} position ("
                      f"{'deg' if a in 'cb' else 'mm'}); NULL if not logged")
    for a in AXES
] + [
    (f"vel_{a}", F64, f"{_AXIS_DOC[a]} velocity ("
                      f"{'deg/min' if a in 'cb' else 'mm/min'}); NULL if not logged")
    for a in AXES
]

# ------------------------------------------------------------------ fct_scan
SCAN_COLUMNS = KEY_COLUMNS + [
    ("send_time", TS, "Batch send time (2023 envelope logs only)"),
    ("profile_x", LIST_F32, "Lateral coordinate of each profile point (mm), "
                            "320 points"),
    ("profile_z", LIST_F32, "Height of each profile point (mm); 0 = no return"),
    ("profile_m0", LIST_F32, "Scanner m0 channel per point"),
    ("profile_intensities", LIST_F32, "Scanner intensity per point (2023-2024)"),
    ("head_x", F64, "Head X logged with the profile (mm), 2024-09+"),
    ("head_y", F64, "Head Y logged with the profile (mm), 2024-09+"),
    ("head_z", F64, "Head Z logged with the profile (mm), 2024-09+"),
    ("head_c", F64, "Head C logged with the profile (deg), 2025-03+"),
    ("width", F64, "Width value logged by the scanner stream, 2025+ "
                   "(meaning unconfirmed)"),
    ("topic", STR, "Message topic (2023-09 .. 2024-07)"),
]
# source scan key -> fct_scan column (other keys: snake_cased, kept as extras)
SCAN_SOURCE_NAMES = {
    "time": "event_time", "sendtime": "send_time", "x": "profile_x",
    "z": "profile_z", "m0": "profile_m0", "intensities": "profile_intensities",
    "X": "head_x", "Y": "head_y", "Z": "head_z", "C": "head_c",
    "width": "width", "topic": "topic",
}

DATASETS = {"pump": PUMP_COLUMNS, "motion": MOTION_COLUMNS, "scan": SCAN_COLUMNS}
FACT_VIEWS = {"pump": "fct_pump", "motion": "fct_motion", "scan": "fct_scan"}
FACT_GRAIN = {
    "pump": "One row per pump-logger sample (raw sensor values).",
    "motion": "One row per real position/velocity update; readings the logger "
              "repeated between controller updates are removed. As-of join on "
              "event_time to attach position to pump or scan rows (on "
              "sample_seq to rebuild the pump log exactly).",
    "scan": "One row per laser line profile (320 points).",
}


def arrow_schema(columns) -> pa.Schema:
    return pa.schema([pa.field(n, t) for n, t, _ in columns])


def snake(name: str) -> str:
    """camelCase / mixed case -> lower snake_case."""
    s = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name)
    return re.sub(r"[^0-9a-zA-Z]+", "_", s).strip("_").lower()


# ------------------------------------------------------------ dim_experiment
_RANGE_UNITS = {"p0": "bar", "p1": "bar", "p2": "bar", "p3": "bar",
                "t0": "degC", "t1": "degC", "torque": "%", "feed": "mm/min"}
DIM_EXPERIMENT = [
    ("experiment_id", "VARCHAR PRIMARY KEY",
     "Source file stem with its leading date corrected to the recorded date; "
     "folder-prefixed when the name has no date, suffixed when it clashes"),
    ("experiment_date", "DATE",
     "Recorded date (Europe/Berlin) of the first valid sample; the file-name "
     "date only when the logger clock was unset"),
    ("source_name_date", "DATE",
     "Date written in the source file name (often a day early: the pump "
     "logger names files with its session start date)"),
    ("pairing", "VARCHAR",
     "How pump and scan were matched: name (same file name) or time "
     "(scan time span >= 50% inside the pump log); NULL = single source"),
    ("board", "VARCHAR", "Specimen / substrate label from the file name"),
    ("run", "BIGINT", "Repetition index from the file name"),
    ("feed_nominal", "BIGINT", "F token of the file name: programmed head feed "
                               "(mm/min)"),
    ("set_speed_nominal", "BIGINT", "S token of the file name"),
    ("w_nominal", "DOUBLE", "W token of the file name (meaning unconfirmed)"),
    ("name_parse_ok", "BOOLEAN", "File name matched a known convention"),
    ("name_warnings", "VARCHAR", "File-name parsing notes"),
    ("name_extra", "VARCHAR", "Unrecognised file-name tokens"),
    ("source_pump_file", "VARCHAR", "Pump CSV, relative to the pump source folder"),
    ("source_scan_file", "VARCHAR", "Scan JSON, relative to the scan source folder"),
    ("source_pump_bytes", "BIGINT", "Size of the pump source file"),
    ("source_scan_bytes", "BIGINT", "Size of the scan source file"),
    ("pump_path", "VARCHAR", "fct_pump file, relative to the data folder"),
    ("motion_path", "VARCHAR", "fct_motion file, relative to the data folder"),
    ("scan_path", "VARCHAR", "fct_scan file, relative to the data folder"),
    ("pump_rows", "BIGINT", "Rows in fct_pump"),
    ("motion_rows", "BIGINT", "Rows in fct_motion (after removing repeats)"),
    ("scan_rows", "BIGINT", "Rows in fct_scan"),
    ("start_time", "TIMESTAMPTZ", "First valid sample (UTC)"),
    ("end_time", "TIMESTAMPTZ", "Last valid sample (UTC)"),
    ("duration_s", "DOUBLE", "end_time - start_time (s)"),
    ("pump_rate_hz", "DOUBLE", "Median pump sampling rate"),
    ("motion_rate_hz", "DOUBLE", "Median rate of real position updates while moving"),
    ("scan_rate_hz", "DOUBLE", "Median scan profile rate"),
    ("source_time_unit", "VARCHAR", "Unit of the source timestamps (normalised to ns)"),
    ("pump_header", "VARCHAR", "'file', or 'inferred:<layout>' for header-less logs"),
    ("scan_layout", "VARCHAR", "Scan log layout: envelope / flat / nested"),
    ("sensor_era", "VARCHAR", "Calibration era: none (no P/T sensors), old (P1 "
                              "P2 T1 as volt-like floats), new (ADC counts, "
                              "P0-P3 T0 T1; from 2024-02-11)"),
    ("n_pos_axes", "BIGINT", "Position axes logged: 0, 3 (XYZ) or 5 (XYZCB)"),
    ("n_vel_axes", "BIGINT", "Velocity axes logged: 0, 3 or 5"),
    ("pump_channels", "VARCHAR", "Source channels present in the pump log"),
    ("scan_channels", "VARCHAR", "Profile arrays present in the scan log"),
    ("profile_points", "VARCHAR", "Points per scan profile (320 so far)"),
    ("scan_pump_overlap", "DOUBLE", "Share of the scan time span inside the pump "
                                    "time span; well below 1 = likely mismatched pair"),
    ("scan_orientation", "VARCHAR", "Scan z direction: upright (z up; scanner settings "
                                    "loaded) or inverted (z = distance from the scanner, "
                                    "bead top = smallest value). Detected from the "
                                    "profiles; it changed per session, not by date. "
                                    "NULL = no bead seen"),
    ("scan_orientation_share", "DOUBLE", "Share of decisive profiles that agree with "
                                         "scan_orientation (1 = unanimous)"),
    ("scan_orientation_votes", "BIGINT", "Profiles that showed a bead clearly enough "
                                         "to vote on the orientation"),
    ("scan_orientation_source", "VARCHAR", "profiles (own scan), or same day (no bead "
                                           "seen; taken from the same day's session)"),
    ("scan_empty_share", "DOUBLE", "Share of sampled profiles with no valid point"),
    ("c_axis_rotates", "BOOLEAN", "C turns while printing (> 5 deg). The scanner sees "
                                  "the fresh bead only while travelling within 5 deg "
                                  "of C (+X without a C axis); NULL = no C axis"),
    ("run_type", "VARCHAR", "print | not a print | unsure. From the experiment's own "
                            "signals: material flowing (torque >= 8 % while the head "
                            "moves) for >= 2 min, >= 2 stacked layers; or set by hand "
                            "in ingest/run_type_overrides.csv"),
    ("shape", "VARCHAR", "Printed object family: board, arc, chair, cylinder, stone, "
                         "border, beam, rectangle, pyramid, lines, bucket (several "
                         "joined with +), unknown; from the name or set by hand in "
                         "ingest/shape_overrides.csv"),
    ("shape_source", "VARCHAR", "name, or manual (shape_overrides.csv)"),
    ("run_type_reason", "VARCHAR", "Why, e.g. 'head moved without material (dry run / "
                                   "levelling / scan)'"),
    ("run_type_source", "VARCHAR", "signals, or manual (run_type_overrides.csv)"),
    ("print_minutes", "DOUBLE", "Minutes the head moved with material flowing "
                                "(torque >= 8 %)"),
    ("printed_layers", "BIGINT", "Highest layer reached while moving (previous-pass "
                                 "layers)"),
    *[(f"{c}_{b}", "DOUBLE", f"Observed {b} of {c} ({u}, calibrated; dead-channel "
                             f"values excluded)")
      for c, u in _RANGE_UNITS.items() for b in ("min", "max")],
    *[(f"vel_{a}_{s}", "DOUBLE", f"Observed {s} of vel_{a} (mm/min)")
      for a in ("x", "y") for s in ("min", "max", "mean")],
    ("set_speed_min", "DOUBLE", "Observed min of set_speed"),
    ("set_speed_max", "DOUBLE", "Observed max of set_speed"),
    ("water_valve_min", "DOUBLE", "Observed min of water_valve (%)"),
    ("water_valve_max", "DOUBLE", "Observed max of water_valve (%)"),
    ("ingest_notes", "VARCHAR", "Non-fatal notes (date taken from data, extra "
                                "channels kept, unreadable lines skipped)"),
    ("ingest_error", "VARCHAR", "Why a stream failed to ingest (NULL = fine)"),
    ("model_version", "VARCHAR", "Data-model version the row was written with "
                                 "(--incremental re-ingests older rows)"),
    ("ingested_at", "TIMESTAMPTZ DEFAULT now()", "When this row was written"),
]
DIM_INSERT_COLUMNS = [c for c, _, _ in DIM_EXPERIMENT if c != "ingested_at"]
