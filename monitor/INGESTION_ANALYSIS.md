# Ingestion Rate Analysis - BAM 3D Concrete Printer

**Date:** 2026-08-14 · **Scope:** maximise acquisition frequency across pump, gantry and laser profile scanner before building the data pipeline.

## Summary

Measured every acquisition path against live hardware. Three of four streams were running far below their real ceiling, for reasons unrelated to the sensors themselves. Scanner throughput increased **4.8×** and pump **3.7×** with software changes only. Gantry position remains capped at 10 Hz by a controller-side constraint that cannot be fixed from Python.

| Stream | Before | After | Limiting factor |
|---|---:|---:|---|
| Laser scanner | ~30 Hz | **143 Hz** | Ethernet transfer cliff below a 7 ms cycle |
| Pump (torque, flow, temp) | 50 Hz | **185 Hz** | 4.55 ms OPC UA round-trip |
| Gantry I/O (P0–P3, T0, T1) | - | **~650 Hz** | 0.88 ms OPC UA round-trip |
| **Gantry position / velocity** | 10 Hz | **10 Hz** | **NC namespace read latency - unresolved** |

At F8000 (133 mm/s) this moves scanner sampling from 4.4 mm to **0.93 mm** between profiles.

## Findings

**1. The 10 Hz position rate is NC namespace read latency, not a filter or a publishing interval.**
Position lives in the NC namespace, served by the CNC kernel gateway. A Read against that namespace blocks for **99.93 ms** regardless of how many nodes are requested. PLC application variables, on the same controller and network, return in **0.88 ms** - a 113× difference. This explains why position arrives at a rock-steady 10 Hz whether the axes move or not. Batching still helps within the NC namespace: two nodes cost the same ~100 ms as one.

**2. The scanner was restarting its transmission for every profile.**
`scan.py` called `transfer_profiles(start)` and `transfer_profiles(stop)` around each single profile. That handshake, plus a fixed 10 ms poll sleep, consumed ~70% of every cycle. Starting the stream once lifted delivery from 30 Hz to exactly the configured 100 Hz ceiling with zero dropped profiles.

**3. The scanner has a hard transfer cliff, and it does not degrade gracefully.**
Sweeping idle time: 10 ms → 100 Hz, 9 ms → 111 Hz, 8 ms → 125 Hz, 7 ms → 143 Hz, all with zero loss. At 6 ms it collapses to **9 Hz with half the profiles lost**, and 5 ms is byte-identical - a fallback mode, not progressive overload. The cliff sits at 2.93 MB/s, far below link capacity, so it is a sensor-side limit rather than bandwidth. **Do not set idle below 600 without re-running the sweep.**

**4. OPC UA subscriptions are unusable on the XM42 from `python-opcua`.**
The server grants the subscription (revised publishing down to 10 ms), delivers exactly one notification, then returns `BadNoSubscription` on every subsequent publish. Node-RED's client keeps them alive; `python-opcua` does not. All Python acquisition should poll. Separately, the pump's subscription publishing interval floors at 100 ms - but polling bypasses this entirely and reaches 220 Hz.

**5. Sequential per-node reads were the main pump bottleneck.**
`inputs.py` issued one Read per node - 13 sequential round-trips per cycle, each blocking the asyncio event loop, and repeated per connected dashboard. Batching into one Read per server and moving acquisition to background threads gave 3.7×.

**6. Controller clocks are not disciplined.**
PLC `SourceTimestamp` and host clock disagree by a fixed offset (~5 min per site knowledge; measured gap larger). Additionally, both servers appear to stamp at *read* time with 1 ms resolution rather than at sample time, so `SourceTimestamp` is not a true acquisition time. **Use host time as the single reference until controller NTP is configured.**

## Method

Read-only probes against live hardware: [probe_rates.py](probe_rates.py) (subscription revised intervals vs. delivered rate), [probe_effective_rate.py](probe_effective_rate.py) (poll rate vs. rate of genuinely new values), [sweep_idle.py](sweep_idle.py) (scanner idle-time sweep with sensor-side `profile_count` loss detection). Optimised services [inputs_fast.py](inputs_fast.py) and [scan_fast.py](scan_fast.py) were verified end-to-end through their websockets. Machine was idle during measurement, so "new value" rates for gantry channels are not meaningful and should be re-measured mid-print.

## Recommendations

1. **Mirror axis position from the NC into a PLC variable** (IndraWorks change). The only route past 10 Hz, and the largest remaining gain: 13.3 mm → sub-millimetre spatial registration.
2. **Consider position-triggered scanning.** The scanCONTROL accepts an external trigger; driving it from gantry motion makes profiles spatially uniform and removes the dependence on fast position feedback altogether.
3. **Interpolate position from G-code in the meantime.** Toolpath and feed override are both logged; 10 Hz anchors plus commanded geometry recover sub-millimetre position everywhere except corners and accelerations.
4. **Configure NTP on both controllers**, or drop `SourceTimestamp` from the join key.
5. **Watch JSON throughput into Node-RED** - now 1.24 MB/s from the scanner alone. Use `DECIMATE` or move raw profiles out of Node-RED entirely.
6. **Track the `dropped` / `queue_dropped` counters.** They are the only thing that distinguishes healthy high-rate acquisition from a silent collapse.
