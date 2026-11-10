"""
Drop-in replacement for scan.py with the transfer-restart bottleneck removed.

What changed, and why:

1. TRANSMISSION STARTED ONCE. scan.py calls transfer_profiles(..., 1) at the top
   of scanTransfer and transfer_profiles(..., 0) at the bottom -- it starts and
   stops the sensor stream for EVERY profile. That handshake costs tens of ms and
   is why you see ~30 Hz from a sensor configured for a 10 ms cycle. Here the
   stream is started once and left running.

2. THE 10 ms POLL SLEEP IS GONE. scan.py sleeps (idle+exposure)/100000 = 10 ms
   whenever a profile is not yet ready, so a near-miss costs a full extra cycle.
   This spins with a short sleep instead.

3. EXPOSURE / IDLE EXPOSED AS CONFIG. Units are 10 us. exposure=100, idle=900
   gives a 10 ms cycle = 100 Hz ceiling. You are idling 9 ms of every 10 ms;
   lowering IDLE_TIME is where the remaining headroom is.

4. HARDWARE TIMESTAMPS RETURNED. scan.py decodes shutter_opened/shutter_closed/
   profile_count and then discards them. They are the only clock that reflects
   when light actually hit the sensor. profile_count also gives exact drop
   detection: any jump > 1 is a profile you lost, counted rather than guessed at.

5. ACQUISITION DECOUPLED FROM THE WEBSOCKET, so a slow consumer cannot throttle
   the sensor.

6. FAN-OUT TO MANY CONSUMERS. Every connected client receives every frame --
   Node-RED, the viewer and the recorder no longer compete for profiles.
   Only the client that sent "start_scan" (Node-RED) stops the scan by
   disconnecting; passive listeners can come and go freely. A client that falls
   too far behind skips ahead, and the skip is logged per client.

Output keys from scan.py are unchanged. New keys are additive:
    profile_count   sensor-side profile counter
    dropped         profiles lost since the previous frame (from profile_count)
    shutter_open / shutter_close    sensor hardware timestamps
    scan_hz         measured acquisition rate

x/z are rounded to 4 decimals (0.1 um) -- far below sensor resolution, and it
cuts the JSON size by roughly a third.

Run:  python scan_fast.py
"""

import asyncio
import ctypes as ct
import json
import os
import threading
import time
from datetime import datetime

import numpy as np
import pyllt as llt
import websockets

from printmon import config
from printmon.broadcast import Broadcaster

WS_HOST, WS_PORT = config.SCAN_WS_HOST, config.SCAN_WS_PORT

# Units of 10 us. 100 + 900 = 10 ms cycle = 100 Hz ceiling.
# Drop IDLE_TIME to raise the ceiling; the sensor will report an error if you
# ask for a cycle shorter than it can service at the chosen resolution.
EXPOSURE_TIME = int(os.environ.get("SCAN_EXPOSURE", 100))
# 600 (7 ms cycle, 143 Hz) is the measured optimum -- see sweep_idle.py.
# Zero dropped profiles at 600; at 500 the transfer collapses to ~9 Hz with half
# the profiles lost, and it does not degrade gracefully. Do not go below 600
# without re-running the sweep.
IDLE_TIME = int(os.environ.get("SCAN_IDLE", 600))

# Index into the sensor's available_resolutions. Lower resolution = higher max
# profile rate. scan.py used 1; try 2 or 3 if you want rate over point density.
RESOLUTION_INDEX = 1

# Frames retained for slow clients (~1.8 s at 143 Hz). A client further behind
# than this skips ahead rather than holding up the sensor.
QUEUE_DEPTH = 256

# Send every Nth point of the x/z arrays. 1 = full profile. Raise this if
# Node-RED's JSON parsing becomes the bottleneck at high rates.
DECIMATE = 1
SEND_PROFILES = True  # False = width only, much cheaper over the wire

RATE_REPORT_SECONDS = 2.0

scanner_type = ct.c_int(0)
null_ptr_short = ct.POINTER(ct.c_ushort)()
null_ptr_int = ct.POINTER(ct.c_uint)()

timestamp = (ct.c_ubyte * 16)()
available_resolutions = (ct.c_uint * 4)()
available_interfaces = (ct.c_uint * 6)()
lost_profiles = ct.c_int()
shutter_opened = ct.c_double(0.0)
shutter_closed = ct.c_double(0.0)
profile_count = ct.c_uint(0)


def _check(ret, what):
    if ret < 1:
        raise ValueError("{}: {}".format(what, ret))
    return ret


def setup():
    global hLLT, resolution, profile_buffer, x, z, intensities, m0

    hLLT = llt.create_llt_device(llt.TInterfaceType.INTF_TYPE_ETHERNET)
    _check(llt.get_device_interfaces_fast(hLLT, available_interfaces,
                                          len(available_interfaces)),
           "Error getting interfaces")
    _check(llt.set_device_interface(hLLT, available_interfaces[config.SCANNER_INTERFACE], 0),
           "Error setting device interface")

    ret = llt.connect(hLLT)
    if ret < 1:
        raise ConnectionError("Error connect: " + str(ret))

    _check(llt.get_resolutions(hLLT, available_resolutions,
                               len(available_resolutions)),
           "Error getting resolutions")
    resolution = available_resolutions[RESOLUTION_INDEX]
    _check(llt.set_resolution(hLLT, resolution), "Error setting resolution")

    profile_buffer = (ct.c_ubyte * (resolution * 64))()
    x = (ct.c_double * resolution)()
    z = (ct.c_double * resolution)()
    intensities = (ct.c_ushort * resolution)()
    m0 = (ct.c_uint32 * resolution)()

    _check(llt.get_llt_type(hLLT, ct.byref(scanner_type)), "Error scanner type")
    _check(llt.set_profile_config(hLLT, llt.TProfileConfig.PROFILE),
           "Error setting profile config")
    _check(llt.set_feature(hLLT, llt.FEATURE_FUNCTION_TRIGGER, llt.TRIG_INTERNAL),
           "Error setting trigger")
    _check(llt.set_feature(hLLT, llt.FEATURE_FUNCTION_EXPOSURE_TIME, EXPOSURE_TIME),
           "Error setting exposure time")
    _check(llt.set_feature(hLLT, llt.FEATURE_FUNCTION_IDLE_TIME, IDLE_TIME),
           "Error setting idle time")

    # Parameter changes can take up to 120 ms to apply.
    time.sleep(0.12)

    cycle_ms = (EXPOSURE_TIME + IDLE_TIME) / 100.0
    print("Parameters set. scanner type={} resolution={} cycle={:.2f} ms -> {:.0f} Hz ceiling"
          .format(scanner_type.value, resolution, cycle_ms, 1000.0 / cycle_ms))


# --- profile processing (unchanged maths from scan.py) ----------------------

def interpolate_missing_values(arr):
    non_missing_indices = np.where(arr != 0)[0]
    if len(non_missing_indices) < 2:
        return arr
    interpolated_arr = arr.copy()
    interpolated_arr[arr == 0] = np.interp(
        np.where(arr == 0)[0], non_missing_indices, arr[non_missing_indices])
    return interpolated_arr


def fix_z(x_in, z_in):
    x_arr = np.array(x_in)
    z_arr = np.array(z_in)
    z2 = interpolate_missing_values(z_arr)
    x_arr[(np.where(z_arr == 0))] = 0
    x2 = interpolate_missing_values(x_arr)
    return x2, z2


def compute_width(x_in, z_in):
    xfix, zfix = fix_z(x_in, z_in)
    height = np.median(zfix[125:185])
    split = height - 5
    x1 = xfix[np.argmin(np.abs(zfix[:160] - split))]
    x2 = xfix[np.argmin(np.abs(zfix[160:] - split)) + 160]
    return float(np.clip(np.abs(x1 - x2), 20, 50))


# --- acquisition ------------------------------------------------------------

_bus = Broadcaster(depth=QUEUE_DEPTH)

_running = threading.Event()
_stop = threading.Event()


def acquire_loop():
    """Runs for the life of the process. Streams only while _running is set."""
    streaming = False
    last_count = None
    count = 0
    t_report = time.perf_counter()
    hz = 0.0

    while not _stop.is_set():
        if not _running.is_set():
            if streaming:
                llt.transfer_profiles(hLLT, llt.TTransferProfileType.NORMAL_TRANSFER, 0)
                streaming = False
                last_count = None
                print("Scan stopped @ {}".format(datetime.now()))
            _running.wait(0.1)
            continue

        if not streaming:
            _check(llt.transfer_profiles(hLLT, llt.TTransferProfileType.NORMAL_TRANSFER, 1),
                   "Error starting transfer profiles")
            streaming = True
            count = 0
            t_report = time.perf_counter()
            print("Scan started @ {}".format(datetime.now()))

        ret = llt.get_actual_profile(hLLT, profile_buffer, len(profile_buffer),
                                     llt.TProfileConfig.PROFILE,
                                     ct.byref(lost_profiles))
        if ret != len(profile_buffer):
            if ret == llt.ERROR_PROFTRANS_NO_NEW_PROFILE:
                # Short spin instead of scan.py's fixed 10 ms penalty.
                time.sleep(0.0002)
                continue
            print("Error get profile buffer data: {}".format(ret))
            time.sleep(0.01)
            continue

        ret = llt.convert_profile_2_values(
            hLLT, profile_buffer, resolution, llt.TProfileConfig.PROFILE,
            scanner_type, 0, 1, null_ptr_short, intensities, null_ptr_short,
            x, z, m0, null_ptr_int)
        if ret & llt.CONVERT_X == 0 or ret & llt.CONVERT_Z == 0 or ret & llt.CONVERT_MAXIMUM == 0:
            print("Error converting data: {}".format(ret))
            continue

        for i in range(16):
            timestamp[i] = profile_buffer[resolution * 64 - 16 + i]
        llt.timestamp_2_time_and_count(timestamp, ct.byref(shutter_opened),
                                       ct.byref(shutter_closed),
                                       ct.byref(profile_count))

        pc = int(profile_count.value)
        dropped = 0
        if last_count is not None:
            gap = pc - last_count
            if gap > 1:
                dropped = gap - 1
        last_count = pc

        x_arr = np.round(np.ctypeslib.as_array(x), 4)
        z_arr = np.round(np.ctypeslib.as_array(z), 4)

        frame = {
            "time": int(time.time_ns()),
            "shutter_open": float(shutter_opened.value),
            "shutter_close": float(shutter_closed.value),
            "profile_count": pc,
            "dropped": dropped,
            "lost_profiles": int(lost_profiles.value),
            "m0": list(m0),
            "scan_hz": round(hz, 2),
        }
        try:
            frame["width"] = compute_width(x_arr, z_arr)
        except Exception as e:
            frame["width"] = None
            frame["width_error"] = str(e)

        if SEND_PROFILES:
            frame["x"] = x_arr[::DECIMATE].tolist()
            frame["z"] = z_arr[::DECIMATE].tolist()

        # Serialised once, delivered to every connected client.
        _bus.publish(json.dumps(frame))

        count += 1
        now = time.perf_counter()
        elapsed = now - t_report
        if elapsed >= RATE_REPORT_SECONDS:
            hz = count / elapsed
            print("scan   {:7.1f} Hz   ({:.2f} ms/profile)".format(
                hz, 1000.0 / hz if hz else 0))
            count = 0
            t_report = now


# --- websocket --------------------------------------------------------------

# The client that last sent "start_scan". Only its disconnect stops the scan, so
# passive listeners (viewer, recorder) can connect and leave without side effects.
_owner = None


async def stream_frames(websocket, who):
    missed_total = 0
    try:
        async for frames, missed in _bus.frames():
            if missed:
                missed_total += missed
                print("{}: fell behind, skipped {} frames (total {})".format(
                    who, missed, missed_total))
            for payload in frames:
                await websocket.send(payload)
    except websockets.ConnectionClosed:
        pass


async def ws_server(websocket, path=None):
    global _owner
    who = "{} {}".format(websocket.remote_address, websocket.path)
    print("client connected: {}".format(who))
    sender = asyncio.create_task(stream_frames(websocket, who))
    try:
        async for command in websocket:
            print("{}: {}".format(who, command))
            if command == "start_scan":
                _owner = websocket
                _running.set()
            elif command == "stop_scan":
                _running.clear()
    except websockets.ConnectionClosed:
        pass
    finally:
        sender.cancel()
        if _owner is websocket:
            _owner = None
            _running.clear()
        print("client disconnected: {}".format(who))


async def main():
    setup()
    _bus.bind(asyncio.get_running_loop())
    threading.Thread(target=acquire_loop, daemon=True).start()
    # compression=None: localhost traffic, deflate would only burn CPU.
    async with websockets.serve(ws_server, WS_HOST, WS_PORT, compression=None):
        print("Scanner websocket running on ws://{}:{}".format(WS_HOST, WS_PORT))
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
