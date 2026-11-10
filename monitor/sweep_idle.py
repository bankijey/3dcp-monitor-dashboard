"""
Sweep IDLE_TIME to find where the scanner actually stops keeping up.

IDLE_TIME=400 (5 ms cycle) collapsed to ~9 Hz delivered with half the profiles
dropped, so somewhere between 900 and 400 there is a cliff. This measures pure
acquisition -- no websocket, no JSON -- so the result is the sensor + transfer
path only.

For each setting it reports:
    delivered Hz    profiles we successfully read and converted
    produced Hz     from the sensor's own profile_count (what it really emitted)
    dropped         profiles the counter says we missed

Run:  python sweep_idle.py
"""

import ctypes as ct
import time

import pyllt as llt

EXPOSURE_TIME = 100
IDLE_SETTINGS = [900, 800, 700, 600, 500, 400]
RESOLUTION_INDEX = 1
MEASURE_SECONDS = 6.0

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


def check(ret, what):
    if ret < 1:
        raise ValueError("{}: {}".format(what, ret))
    return ret


hLLT = llt.create_llt_device(llt.TInterfaceType.INTF_TYPE_ETHERNET)
check(llt.get_device_interfaces_fast(hLLT, available_interfaces,
                                     len(available_interfaces)), "interfaces")
check(llt.set_device_interface(hLLT, available_interfaces[0], 0), "set interface")
if llt.connect(hLLT) < 1:
    raise ConnectionError("connect failed")
check(llt.get_resolutions(hLLT, available_resolutions,
                          len(available_resolutions)), "resolutions")
resolution = available_resolutions[RESOLUTION_INDEX]
check(llt.set_resolution(hLLT, resolution), "set resolution")

profile_buffer = (ct.c_ubyte * (resolution * 64))()
x = (ct.c_double * resolution)()
z = (ct.c_double * resolution)()
intensities = (ct.c_ushort * resolution)()
m0 = (ct.c_uint32 * resolution)()

check(llt.get_llt_type(hLLT, ct.byref(scanner_type)), "scanner type")
check(llt.set_profile_config(hLLT, llt.TProfileConfig.PROFILE), "profile config")
check(llt.set_feature(hLLT, llt.FEATURE_FUNCTION_TRIGGER, llt.TRIG_INTERNAL), "trigger")
check(llt.set_feature(hLLT, llt.FEATURE_FUNCTION_EXPOSURE_TIME, EXPOSURE_TIME), "exposure")

print("resolution = {} points, {} bytes per profile".format(resolution, resolution * 64))
print("\n{:>6} {:>9} {:>12} {:>12} {:>10} {:>10} {:>10}".format(
    "idle", "cycle ms", "ceiling Hz", "delivered", "produced", "dropped", "MB/s"))

for idle in IDLE_SETTINGS:
    check(llt.set_feature(hLLT, llt.FEATURE_FUNCTION_IDLE_TIME, idle), "idle")
    time.sleep(0.15)
    check(llt.transfer_profiles(hLLT, llt.TTransferProfileType.NORMAL_TRANSFER, 1),
          "start transfer")
    time.sleep(0.2)

    got = 0
    dropped = 0
    first_pc = None
    last_pc = None
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < MEASURE_SECONDS:
        ret = llt.get_actual_profile(hLLT, profile_buffer, len(profile_buffer),
                                     llt.TProfileConfig.PROFILE,
                                     ct.byref(lost_profiles))
        if ret != len(profile_buffer):
            time.sleep(0.0002)
            continue
        llt.convert_profile_2_values(
            hLLT, profile_buffer, resolution, llt.TProfileConfig.PROFILE,
            scanner_type, 0, 1, null_ptr_short, intensities, null_ptr_short,
            x, z, m0, null_ptr_int)
        for i in range(16):
            timestamp[i] = profile_buffer[resolution * 64 - 16 + i]
        llt.timestamp_2_time_and_count(timestamp, ct.byref(shutter_opened),
                                       ct.byref(shutter_closed),
                                       ct.byref(profile_count))
        pc = int(profile_count.value)
        if first_pc is None:
            first_pc = pc
        elif pc - last_pc > 1:
            dropped += pc - last_pc - 1
        last_pc = pc
        got += 1
    span = time.perf_counter() - t0
    llt.transfer_profiles(hLLT, llt.TTransferProfileType.NORMAL_TRANSFER, 0)
    time.sleep(0.1)

    cycle_ms = (EXPOSURE_TIME + idle) / 100.0
    produced = ((last_pc - first_pc) / span) if first_pc is not None else 0
    print("{:>6} {:>9.2f} {:>12.0f} {:>12.1f} {:>10.1f} {:>10} {:>10.2f}".format(
        idle, cycle_ms, 1000.0 / cycle_ms, got / span, produced, dropped,
        got / span * resolution * 64 / 1e6))

llt.disconnect(hLLT)
print("\nDone.")
