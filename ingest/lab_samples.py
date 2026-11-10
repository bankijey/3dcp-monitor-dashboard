"""
Lab samples taken at the nozzle during a print: Haegermann spread, fresh
density, air content and, where weighed, the mass collected over the sample
window (a timed weighing of the output). One row per sample.

Sources (xlsx or csv, any number of files in the lab folders):
    sample sheets   print (recorded date), start, end (s since the print's
                    first logged sample), d1, d2 [, dm], density, lp
                    [, error] [, experiment_id]
    mass files      print, start, end, mass_kg [, source]
    exclusions      print, start, end, reason (samples left out on purpose)
Rows are matched to an experiment by recorded date + window (the real print
of that day whose log covers the window, most printing first) unless the
sheet names the experiment_id. Mass rows join their sample by (print, start,
end).
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa

LAB_FILE = "lab_samples.parquet"
LAB_VIEW = "fct_lab_sample"
LAB_GRAIN = ("One row per lab sample taken at the nozzle during a print "
             "(window start_time..end_time)")
LAB_SAMPLE = [  # name, arrow type, DuckDB type, description
    ("experiment_id", pa.string(), "VARCHAR", "Experiment the sample was taken from"),
    ("sample_no", pa.int64(), "BIGINT", "1, 2, ... in time order within the experiment"),
    ("window_start_s", pa.float64(), "DOUBLE", "Window start, s since the experiment's "
                                                "first logged sample"),
    ("window_end_s", pa.float64(), "DOUBLE", "Window end, s since the first logged sample"),
    ("start_time", pa.timestamp("us", tz="UTC"), "TIMESTAMPTZ", "Window start (UTC)"),
    ("end_time", pa.timestamp("us", tz="UTC"), "TIMESTAMPTZ", "Window end (UTC)"),
    ("spread_d1_mm", pa.float64(), "DOUBLE", "Haegermann spread, first diameter (mm)"),
    ("spread_d2_mm", pa.float64(), "DOUBLE", "Haegermann spread, second diameter (mm)"),
    ("spread_mm", pa.float64(), "DOUBLE", "Haegermann spread, mean of d1 and d2 (mm)"),
    ("density_kg_m3", pa.float64(), "DOUBLE", "Fresh density (kg/m3)"),
    ("air_content_pct", pa.float64(), "DOUBLE", "Air content (LP, %)"),
    ("mass_kg", pa.float64(), "DOUBLE", "Mass collected at the nozzle over the window "
                                        "(kg); NULL = not weighed"),
    ("mass_flow_kg_min", pa.float64(), "DOUBLE", "mass_kg / window length (kg/min)"),
    ("volume_flow_l_min", pa.float64(), "DOUBLE", "mass flow / density (L/min): the "
                                                  "true output for that window"),
    ("flagged_error", pa.bool_(), "BOOLEAN", "Marked as erroneous in the source sheet"),
    ("match", pa.string(), "VARCHAR", "How the experiment was found: named in the "
                                      "sheet, or by date + window"),
    ("source_file", pa.string(), "VARCHAR", "Sheet the sample came from (+ mass file)"),
]


def _read(path: Path) -> pd.DataFrame:
    df = pd.read_excel(path) if path.suffix.lower() in (".xlsx", ".xls") else pd.read_csv(path)
    df.columns = [str(c).strip().lower() for c in df.columns]
    df["_file"] = path.name
    return df


def read_sources(dirs) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    sheets, masses, excl = [], [], []
    for d in dirs or []:
        for f in sorted(Path(d).glob("*")):
            if f.suffix.lower() not in (".xlsx", ".xls", ".csv") or f.name.startswith("~$"):
                continue
            df = _read(f)
            if {"print", "start", "end"} <= set(df.columns):
                if "mass_kg" in df.columns:
                    masses.append(df)
                elif "reason" in df.columns and "density" not in df.columns:
                    excl.append(df)
                elif "density" in df.columns or "d1" in df.columns:
                    sheets.append(df)
    cat = lambda xs: pd.concat(xs, ignore_index=True) if xs else pd.DataFrame()
    return cat(sheets), cat(masses), cat(excl)


def _match(row, dim: pd.DataFrame) -> tuple[str | None, str]:
    if isinstance(row.get("experiment_id"), str) and row["experiment_id"] in set(dim.experiment_id):
        return row["experiment_id"], "named in the sheet"
    day = dim[dim.experiment_date.astype(str) == str(row["print"])[:10]]
    if "run_type" in day:
        real = day[day.run_type == "print"]
        day = real if len(real) else day
    day = day[day.duration_s.fillna(0) >= float(row["end"])]
    if day.empty:
        return None, "no experiment of that day covers the window"
    best = day.sort_values(["print_minutes" if "print_minutes" in day else "duration_s"],
                           ascending=False).iloc[0]
    return best.experiment_id, f"date + window ({len(day)} candidate{'s' * (len(day) > 1)})"


def build(dirs, dim: pd.DataFrame) -> tuple[pa.Table | None, list[str]]:
    sheets, masses, excl = read_sources(dirs)
    notes: list[str] = []
    if sheets.empty:
        return None, ["no lab sample sheets found"]
    key = lambda df: (df["print"].astype(str).str[:10] + "|" + df["start"].astype(float).astype(str)
                      + "|" + df["end"].astype(float).astype(str))
    if not excl.empty:
        drop = key(sheets).isin(set(key(excl)))
        notes += [f"excluded {r['print']} {r['start']}-{r['end']}: {x}" for r, x in
                  zip(sheets[drop].to_dict("records"),
                      excl.set_index(key(excl)).reason.reindex(key(sheets[drop])))]
        sheets = sheets[~drop].reset_index(drop=True)
    if not masses.empty:
        mk = masses.assign(_k=key(masses)).set_index("_k")
        sheets["mass_kg"] = key(sheets).map(mk.mass_kg)
        sheets["_mass_file"] = key(sheets).map(mk._file)
        missing = set(mk.index) - set(key(sheets)) - (set(key(excl)) if not excl.empty else set())
        if missing:
            notes.append(f"{len(missing)} mass rows without a matching sample")
    else:
        sheets["mass_kg"] = np.nan
        sheets["_mass_file"] = None
    starts = dim.set_index("experiment_id").start_time
    rows = []
    for r in sheets.to_dict("records"):
        eid, how = _match(r, dim)
        if eid is None:
            notes.append(f"sample {r['print']} {r['start']}-{r['end']}: {how}")
            continue
        t0 = pd.Timestamp(starts[eid])
        d1, d2 = r.get("d1"), r.get("d2")
        spread = (r.get("dm") if pd.notna(r.get("dm")) else
                  np.nanmean([d1, d2]) if pd.notna(d1) or pd.notna(d2) else np.nan)
        minutes = (float(r["end"]) - float(r["start"])) / 60
        mass = r.get("mass_kg")
        mflow = mass / minutes if pd.notna(mass) and minutes > 0 else np.nan
        dens = r.get("density")
        rows.append(dict(
            experiment_id=eid, window_start_s=float(r["start"]), window_end_s=float(r["end"]),
            start_time=(t0 + timedelta(seconds=float(r["start"]))).to_pydatetime(),
            end_time=(t0 + timedelta(seconds=float(r["end"]))).to_pydatetime(),
            spread_d1_mm=d1, spread_d2_mm=d2, spread_mm=spread, density_kg_m3=dens,
            air_content_pct=r.get("lp"), mass_kg=mass, mass_flow_kg_min=mflow,
            volume_flow_l_min=mflow / (dens / 1000) if pd.notna(mflow) and pd.notna(dens) else np.nan,
            flagged_error=bool(r.get("error") == 1), match=how,
            source_file=r["_file"] + (f" + {r['_mass_file']}" if isinstance(r.get("_mass_file"), str) else "")))
    df = pd.DataFrame(rows).sort_values(["experiment_id", "window_start_s"])
    df.insert(1, "sample_no", df.groupby("experiment_id").cumcount() + 1)
    schema = pa.schema([(n, t) for n, t, _, _ in LAB_SAMPLE])
    tbl = pa.Table.from_pandas(df[[n for n, *_ in LAB_SAMPLE]], schema=schema, preserve_index=False)
    return tbl, notes
