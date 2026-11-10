"""
Signal registry: every plottable sensor, where it comes from, and its unit conversion.

Conversions are taken verbatim from the lab's calibration notebook:
    process_pump(), cell 326   torque, T0/T1, P0..P3, waterFlow, feed
    cell 242                   waterTemp
so the live dashboard and the offline analysis agree on engineering values.

Raw values are what the controllers report and are what the recorder stores. Conversion
happens only at display time, so switching raw <-> engineering applies to the whole
history instantly and stored data never depends on a calibration that may change.
"""

from dataclasses import dataclass, field

import numpy as np


def _ident(v):
    return v


@dataclass(frozen=True)
class Signal:
    key: str            # record key (or derived name)
    label: str
    stream: str         # pump | gantry | nc -- which acquisition thread produces it
    raw_unit: str
    eng_unit: str
    to_eng: object = field(default=_ident, compare=False)
    boolean: bool = False

    def convert(self, values, engineering):
        return self.to_eng(values) if engineering else values

    def unit(self, engineering):
        return self.eng_unit if engineering else self.raw_unit


SIGNALS = {s.key: s for s in (
    Signal("set_speed", "setSpeed", "nc", "%", "%"),
    Signal("feed", "feed (XY)", "nc", "mm/min", "mm/min"),
    Signal("torque", "torque", "pump", "counts", "%", lambda v: 100.0 * v / 65535.0),
    Signal("P0", "P0", "gantry", "counts", "bar", lambda v: 1.25 * 40.0 * (v - 6000.0) / 24000.0),
    Signal("P1", "P1", "gantry", "counts", "bar", lambda v: 40.0 * v / 30000.0),
    Signal("P2", "P2", "gantry", "counts", "bar", lambda v: 40.0 * v / 30000.0),
    Signal("P3", "P3", "gantry", "counts", "bar", lambda v: 40.0 * v / 30000.0),
    Signal("T0", "T0", "gantry", "counts", "°C", lambda v: v / 10.0),
    Signal("T1", "T1", "gantry", "counts", "°C", lambda v: v / 10.0),
    Signal("water_temp", "waterTemp", "pump", "counts", "°C", lambda v: v / 1000.0),
    Signal("water_flow", "waterFlow", "pump", "counts", "l/min",
           lambda v: v * 20.4 / 65535.0),
    Signal("mixer", "mixer", "pump", "on/off", "on/off", boolean=True),
    Signal("C", "C (rotary)", "nc", "°", "°"),
)}

DEFAULT_SELECTION = ("set_speed", "feed", "torque", "P0", "P1", "water_flow")


def feed_from_vel(vel):
    """Resolved XY path velocity, as in the notebook: sqrt(vx^2 + vy^2), mm/min."""
    return float(np.hypot(vel[0], vel[1]))
