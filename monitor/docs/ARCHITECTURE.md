# Architecture, decisions and roadmap

Scope: acquisition → storage → monitoring for the BAM 3D concrete printer, on one
lab workstation (an 8-core laptop with 32 GB RAM, a 6 GB GPU and about 700 GB of
free disk). Decisions are recorded with the measurement that drove them.

## Data volumes (measured)

| Stream | Rate | Bronze size (Parquet, zstd) |
|---|---:|---:|
| Scanner profiles, 320 pts | 143 Hz | ~1.5 KB/profile → **~0.8 GB/hour** |
| OPC frames (pump / gantry / NC) | ~850 frames/s combined | ~0.2 GB/hour |
| **Total** | | **~1 GB per printing hour → ~700 h on the free disk** |

This is a **single-machine, gigabyte-scale** problem. Nearly every tool decision below
follows from that one fact.

## Decisions

**D1. Fan-out lives in the acquisition services.** Each service serialises a frame once
and broadcasts it (`printmon/broadcast.py`); every client has its own cursor into a
ring buffer. A slow client skips ahead and the skip is logged - it can never block the
sensor or starve another client. *Why:* the previous scan service handed each profile
to exactly one client and stopped scanning when any client disconnected; adding a
viewer next to Node-RED would have split the data between them.

**D2. Bronze = lossless Parquet on local disk, queried in place with DuckDB.** Hive
partitions by stream / origin / date; 60 s files written atomically; session
manifests; unknown fields preserved in `extra`. *Why:* measured 4.5–6.4× smaller than
Node-RED's CSV/NDJSON, readable by every tool in the ecosystem, zero servers to run,
and replayed/emulated data is partitioned apart from lab data by construction.

**D3. Live monitoring is a desktop PyQtGraph app, not a web dashboard.** *Why:*
Node-RED's 11 `ui_chart` nodes held 10 000 bezier-interpolated points each and had to
be throttled to 1–2 Hz. The PyQtGraph viewer renders the full-rate stream; after
profiling, frame cost fell from 86 ms to 5 ms (the fix was axis/grid repaint, not data
volume). Grafana's refresh floor makes it a trends tool, not a live-waveform tool -
right for gold-layer dashboards later, wrong for this.

**D4. Dead-reckoned position, validated.** The NC position arrives at 10 Hz (a hard
~100 ms read latency in the NC namespace). Extrapolating along the reported
velocity (mm/min) cut position error at the worst point of each gap from 12–17.5 mm
to **1.1–1.6 mm median**, over ~30 000 intervals across three recorded runs. It is a
test (`tests/test_processing.py`), so the assumption is re-checked on every change.

**D5. Node-RED is retired role by role, not replaced wholesale** (see below).

**D6. Site configuration lives in `.env`, not in code.** Equipment endpoints and OPC UA
node ids are read through `printmon/config.py` (environment, then `.env`, then for
endpoints a deliberately unreachable `.invalid` default that fails loudly). A test asserts
no tracked file contains an equipment address. *Why:* the repository is portable and
shareable, a second rig needs only its own `.env`, and nothing about the lab network or
the PLC programs is published.

**D7. Raw values are stored; engineering units are a display concern.** Conversions
(`printmon/units.py`) come from the lab's analysis notebook. *Why:* a recalibration must
not invalidate recorded data, and the dashboard and the offline analysis must agree.

## Tool evaluation

| Tool | Verdict | Reasoning |
|---|---|---|
| **DuckDB** | **Adopted** | Embedded, SQL directly on Parquet, uses all 16 threads. Faster than a cluster at this size. |
| **Parquet + pyarrow** | **Adopted** | The storage contract. Everything reads it; nothing locks us in. |
| **Polars** | Next (silver) | Lazy, multi-threaded dataframes for the alignment/feature transforms. |
| **Kafka** | **No** | One producer host, one consumer host, ~2 MB/s. Kafka's value is durable multi-consumer streams across machines - it would add a JVM cluster to operate for no gain. The in-process broadcaster already gives every consumer every frame. |
| Redpanda / NATS / MQTT | When needed | If data must leave this PC (second machine, cloud), add a lightweight broker then. MQTT also suits PLC-side integration. |
| **Spark** | **No** | Designed for data that does not fit one machine. ~1 GB/hour fits comfortably; DuckDB outperforms Spark below ~1 TB on one node. |
| InfluxDB / TimescaleDB / QuestDB | Optional (gold) | Good behind Grafana for *low-rate aggregates* (per-second KPIs). Poor fit for 320-point profile arrays. `node-red-contrib-influxdb` is already installed. |
| Dagster | Next | Asset-based orchestration maps 1:1 onto bronze → silver → gold. Lighter than Airflow for one machine. |
| pandera / Great Expectations | Next | Data contracts on silver: width range, monotonic time, drop counters at zero. |
| DVC | Next | Version datasets alongside code without putting GB into git. |

A measured "no" is worth more than an unjustified "yes". If demonstrating Kafka is a
goal, do it as an experiment: swap the broadcaster for Redpanda on a branch and
publish the latency/throughput comparison.

## GPU: where it helps and where it does not

- **Rendering (in use):** the 3D bead is an OpenGL surface on the RTX 3000.
- **Live state estimation (not needed):** 143 profiles × 320 points per second is
  microseconds of CPU work. The next step is a **Kalman filter/smoother** fusing NC
  position, velocity and commanded toolpath - mathematically better than
  dead-reckoning, and still CPU-trivial.
- **Where the GPU pays off:** batched offline work - reprocessing entire runs
  (millions of profiles) with CuPy/PyTorch, particle filters with many particles, and
  **ML models** (predicting bead geometry from pump/process signals, CNNs on the
  profile stack). CUDA compute capability 7.5, 6 GB VRAM - fine for all of these.

## Node-RED: retire by role

| Role today | Status | Next |
|---|---|---|
| Live charts (11 × `ui_chart`) | **Superseded** by `printmon view` | Remove chart nodes once operators are comfortable. |
| Saving data (CSV / NDJSON file nodes) | **Superseded** by `printmon record` | Run both in parallel; compare row counts with `printmon lake`; then disable. Note: Node-RED's scan file now grows ~3.4× faster at 143 Hz. |
| Run metadata form | Still needed | Send the form to the recorder so sessions carry experiment IDs (iteration 2). |
| Process control (start, sliders, water, width control) | **Keep** | Move to a small audited control service + HMI later. Interlocks belong in the PLC regardless. |

## Roadmap (short iterations, each shippable)

1. **Done - acquisition fan-out, live viewer (sensor selection, gauges, engineering units,
   pause/rewind, 3D bead + layer width maps), bronze recorder, emulator, test suite, CI.**
2. Lab validation of (1); git + CI live; run metadata from Node-RED into session manifests; parity check, then retire Node-RED file saving.
3. **Silver:** single clock, per-profile Kalman-estimated XYZ, per-run tables, data-quality checks, DuckDB views.
4. **Gold:** per-run and per-segment KPIs (width stability, corner behaviour), Grafana or generated reports; offline whole-part 3D reconstruction (PyVista/Open3D) with animation export.
5. NC → PLC position mirror in IndraWorks, then re-measure; GPU/ML exploration on the accumulated dataset.

Working agreement: every change via pull request; CI green on Windows before merge;
off-machine development against `printmon emulate`; lab PC deploys with
`git pull && pip install -e .`.
