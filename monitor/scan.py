import asyncio
import websockets
import json
import numpy as np
import ctypes as ct
import time
import pyllt as llt
from datetime import datetime
global noProfileReceived


# Init profile buffer and timestamp info
noProfileRecieved = True
exposure_time = 100
idle_time = 900
timestamp = (ct.c_ubyte*16)()
available_resolutions = (ct.c_uint*4)()
available_interfaces = (ct.c_uint*6)()
lost_profiles = ct.c_int()
shutter_opened = ct.c_double(0.0)
shutter_closed = ct.c_double(0.0)
profile_count = ct.c_uint(0)



# Parametrize transmission
scanner_type = ct.c_int(0)

# Null pointer if data not necessary
null_ptr_short = ct.POINTER(ct.c_ushort)()
null_ptr_int = ct.POINTER(ct.c_uint)()

# Create instance (device handle)
hLLT = llt.create_llt_device(llt.TInterfaceType.INTF_TYPE_ETHERNET)

# Get available interfaces
ret = llt.get_device_interfaces_fast(hLLT, available_interfaces, len(available_interfaces))
if ret < 1:
    raise ValueError("Error getting interfaces : " + str(ret))

# Set IP address
ret = llt.set_device_interface(hLLT, available_interfaces[0], 0)
if ret < 1:
    raise ValueError("Error setting device interface: " + str(ret))

# Connect
ret = llt.connect(hLLT)
if ret < 1:
    raise ConnectionError("Error connect: " + str(ret))

# Get available resolutions
ret = llt.get_resolutions(hLLT, available_resolutions, len(available_resolutions))
if ret < 1:
    raise ValueError("Error getting resolutions : " + str(ret))

# Set max. resolution
resolution = available_resolutions[1]
ret = llt.set_resolution(hLLT, resolution)
if ret < 1:
    raise ValueError("Error getting resolutions : " + str(ret))
    
# Declare measuring data arrays
profile_buffer = (ct.c_ubyte*(resolution*64))()
x = (ct.c_double * resolution)()
z = (ct.c_double * resolution)()
intensities = (ct.c_ushort * resolution)()
m0 = (ct.c_uint32 * resolution)()


# Scanner type
ret = llt.get_llt_type(hLLT, ct.byref(scanner_type))
if ret < 1:
    raise ValueError("Error scanner type: " + str(ret))

# Set profile config
ret = llt.set_profile_config(hLLT, llt.TProfileConfig.PROFILE)
if ret < 1:
    raise ValueError("Error setting profile config: " + str(ret))

# Set trigger
ret = llt.set_feature(hLLT, llt.FEATURE_FUNCTION_TRIGGER, llt.TRIG_INTERNAL)
if ret < 1:
    raise ValueError("Error setting trigger: " + str(ret))

# Set exposure time
ret = llt.set_feature(hLLT, llt.FEATURE_FUNCTION_EXPOSURE_TIME, exposure_time)
if ret < 1:
    raise ValueError("Error setting exposure time: " + str(ret))

# Set idle time
ret = llt.set_feature(hLLT, llt.FEATURE_FUNCTION_IDLE_TIME, idle_time)
if ret < 1:
    raise ValueError("Error idle time: " + str(ret))

#Wait until all parameters are set before starting the transmission (this can take up to 120ms)
time.sleep(0.12)
print('Parameters set!')


def interpolate_missing_values(arr):
    """
    Interpolate missing values (0s) in a NumPy array.

    Parameters:
    - arr (numpy.ndarray): The input NumPy array.

    Returns:
    - numpy.ndarray: The array with missing values filled using interpolation.
    """
    # Find indices of non-missing (non-zero) values
    non_missing_indices = np.where(arr != 0)[0]

    if len(non_missing_indices) < 2:
        # Insufficient non-missing values for interpolation
        return arr

    # Interpolate missing values based on non-missing values
    interpolated_arr = arr.copy()
    interpolated_arr[arr == 0] = np.interp(
        np.where(arr == 0)[0],  # Indices of missing values
        non_missing_indices,     # Indices of non-missing values
        arr[non_missing_indices] # Non-missing values
    )

    return interpolated_arr

def fix_z(x, z):
    x = np.array(x)
    z = np.array(z)

    z2 = interpolate_missing_values(z)
    x[(np.where(z==0))] = 0
    x2 = interpolate_missing_values(x)
    return x2, z2
    
def compute_scandata(data):
    xfix, zfix = fix_z(data['x'], data['z'])
    zflip = np.abs(zfix - np.max(zfix))
    height = np.median(zfix[125:185])
    split =  height - 5
    x1 = xfix[np.argmin(np.abs(zfix[:160] - split))]
    x2 = xfix[np.argmin(np.abs(zfix[160:] - split)) + 160]
    width = np.clip(np.abs(x1 - x2), 20, 50)
    return {
        'width': width,
        'xfix': xfix, 'zfix': zfix, 'topic':'compute'}
    


def scanTransfer(hLLT, noProfileReceived, x,z,intensities, m0,profile_buffer,lost_profiles,resolution):
    ret = llt.transfer_profiles(hLLT, llt.TTransferProfileType.NORMAL_TRANSFER, 1)
    if ret < 1:
        raise ValueError("Error starting transfer profiles: " + str(ret))

    while(noProfileReceived):
        ret = llt.get_actual_profile(hLLT, profile_buffer, len(profile_buffer), llt.TProfileConfig.PROFILE,
                                    ct.byref(lost_profiles))
        if ret != len(profile_buffer):
            if (ret == llt.ERROR_PROFTRANS_NO_NEW_PROFILE):
                time.sleep((idle_time+exposure_time)/100000)
                continue
            else:
                raise ValueError("Error get profile buffer data: " + str(ret))
                noProfileReceived = False
        else:

            noProfileReceived = False


    ret = llt.convert_profile_2_values(hLLT, profile_buffer, resolution, llt.TProfileConfig.PROFILE, scanner_type, 0, 1,
                                null_ptr_short, intensities, null_ptr_short, x, z, m0, null_ptr_int)

    # ret = llt.convert_profile_2_values(hLLT, profile_buffer, resolution, llt.TProfileConfig.PROFILE, scanner_type, 0, 1,
    #                         null_ptr_short, intensities, null_ptr_short, x, z, m0, null_ptr_int)
    if ret & llt.CONVERT_X == 0 or ret & llt.CONVERT_Z == 0 or ret & llt.CONVERT_MAXIMUM == 0:
        raise ValueError("Error converting data: " + str(ret))

    # Output of profile count
    for i in range(16):
        timestamp[i] = profile_buffer[resolution * 64 - 16 + i]

    llt.timestamp_2_time_and_count(timestamp, ct.byref(shutter_opened), ct.byref(shutter_closed), ct.byref(profile_count))


    list_x = [xlist for xlist in x]
    list_z = [zlist for zlist in z]
    list_intensities = [intenslist for intenslist in intensities]
    list_m0 = [m0l for m0l in m0]
    # Stop transmission
    ret = llt.transfer_profiles(hLLT, llt.TTransferProfileType.NORMAL_TRANSFER, 0)
    if ret < 1:
        raise ValueError("Error stopping transfer profiles: " + str(ret))
    # cancel(hLLT=hLLT)
    
    
    return {
        'time': int(time.time_ns()),
        "x":list_x,
        "z":list_z,
        # "intensities":list_intensities,
        "m0":list_m0,
        # 'topic' : 'raw'
        }


async def scanReal(websocket):
    while True:
        scans = scanTransfer(hLLT, noProfileRecieved,x,z,intensities, m0,profile_buffer,lost_profiles,resolution)
        scans['width'] = compute_scandata(scans)['width']
        await websocket.send(json.dumps(scans))
        await asyncio.sleep(1e-4)

        # await websocket.send(json.dumps(processed))
        # await asyncio.sleep(1e-5)
        
        


# Define the WebSocket server
async def ws_server(websocket, path):
    while True:
        command = await websocket.recv()
        print (command)
        if command == "start_scan":
            # Start the function
            try:
                task = asyncio.create_task(scanReal(websocket))
                print(f"Scan started @ {datetime.now()}")
            except Exception as e:
                print(e)
        elif command == "stop_scan":
            # Stop the function
            try:
                task.cancel()
                time.sleep(0.5)
                print(f"Scan stopped @ {datetime.now()}")
            except Exception as e:
                print(e)

# Start the WebSocket server
async def main():
    async with websockets.serve(ws_server, "localhost", 8764):
        await asyncio.Future()  # run forever

asyncio.run(main())