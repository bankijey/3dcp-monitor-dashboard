import asyncio
import websockets
import json
import time
from opcua import Client
from printmon import config
from printmon.config import GANTRY_OPCUA_URL, PUMP_OPCUA_URL

try:
    pump_url = PUMP_OPCUA_URL
    pump_reader = Client(pump_url)
    pump_reader.connect()
    pump_reader.load_type_definitions()
    print(f"Pump opc ua server @ {pump_url} connected")
    torque = pump_reader.get_node(config.node("PUMP_TORQUE"))
    waterFlow = pump_reader.get_node(config.node("PUMP_WATER_FLOW"))
    waterTemp = pump_reader.get_node(config.node("PUMP_WATER_TEMP"))
    

    
    solenoidValve = pump_reader.get_node(config.node("PUMP_SOLENOID_VALVE"))
    waterPump = pump_reader.get_node(config.node("PUMP_WATER_PUMP"))
    mixer = pump_reader.get_node(config.node("PUMP_MIXER"))
    pump_available = True
    
except Exception as e:
    print(f'Error with Pump: {e}')
    pump_available = False
    pass

try:
    
    gantry_url = GANTRY_OPCUA_URL
    gantry_reader = Client(gantry_url)

    gantry_reader.connect()
    gantry_reader.load_type_definitions()
    print(f"Motion system opc ua server @ {gantry_url} connected")

    P0 = gantry_reader.get_node(config.node("GANTRY_P0"))
    P1 = gantry_reader.get_node(config.node("GANTRY_P1"))
    P2 = gantry_reader.get_node(config.node("GANTRY_P2"))
    P3 = gantry_reader.get_node(config.node("GANTRY_P3"))
    T0 = gantry_reader.get_node(config.node("GANTRY_T0"))
    T1 = gantry_reader.get_node(config.node("GANTRY_T1"))
    
    gantry_available = True
    
except Exception as e:
    print(f'Error with Gantry: {e}')
    gantry_available = False
    pass

node_values = {}


# Define the websocket parameters
async def handler(websocket):
    global node_values
    while True:
        node_values["timestamp"] = time.time_ns()
        if pump_available:
            node_values["torque"] = torque.get_value()
            node_values["water_flow"] = waterFlow.get_value()
            node_values["water_temp"] = waterTemp.get_value()
            node_values["water_pump"] = waterPump.get_value()

            node_values["solenoid_valve"] = solenoidValve.get_value()
            node_values["mixer"] = mixer.get_value()

        if gantry_available:
            # node_values["pos"] = axisPos.get_value()
            # node_values["vel"] = axisVel.get_value()
            node_values["P0"] = P0.get_value()
            node_values["P1"] = P1.get_value()
            node_values["P2"] = P2.get_value()
            node_values["P3"] = P3.get_value()
            node_values["T0"] = T0.get_value()
            node_values["T1"] = T1.get_value()

        await websocket.send(json.dumps(node_values))
        await asyncio.sleep(0.0001)

async def main():
    async with websockets.serve(handler, "localhost", 8001):
        print("Websocket server running")
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
