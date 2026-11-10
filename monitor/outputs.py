import asyncio
import websockets
from opcua import Client, ua
import json
from printmon import config
from printmon.config import PUMP_OPCUA_URL

# connect reader
reader = Client(PUMP_OPCUA_URL)
try:
    reader.connect()
    reader.load_type_definitions()
except Exception as e:
    print(e)
    pass

# connect writer
writer = Client(PUMP_OPCUA_URL)
# gantry_writer = Client(GANTRY_OPCUA_URL)

try:
    writer.connect()
    writer.load_type_definitions()
    
    # gantry_writer.connect()
    # gantry_writer.load_type_definitions()
except Exception as e:
    print(e)
    pass


# toggle Livebit by subscription
try:    
    class LivebitHandler(object):
        def datachange_notification(self, node, value, data):
            # print("Livebit:", value)
            heartbeat_out = writer.get_node(config.node("PUMP_HEARTBEAT_OUT"))
            heartbeat_out.set_value(ua.Variant(value, ua.VariantType.Boolean))


    heartbeat_in = writer.get_node(config.node("PUMP_HEARTBEAT_IN"))
    subHandler = LivebitHandler()
    sub = reader.create_subscription(100, subHandler)
    subscription = sub.subscribe_data_change(heartbeat_in)

    Remote_start = writer.get_node(config.node("PUMP_REMOTE_START"))
    Pump_speed = writer.get_node(config.node("PUMP_SPEED_SETPOINT"))
    Water_valve = writer.get_node(config.node("PUMP_WATER_VALVE"))
    # feed_override = gantry_writer.get_node(config.node("GANTRY_FEED_OVERRIDE"))

except Exception as e:
    print(e)
    pass


def val_to_uint(value):
    # Parse the percentage value from the message
    percentage = float(value)
    if percentage < 0 or percentage > 100:
        raise ValueError('Percentage value must be between 0 and 100')
    
    # Convert the percentage value to a numerical value
    max_value = (2 ** 16) - 1
    uint_value = round((percentage / 100) * max_value)
    
    # Send the numerical value back to the client
    # response = str(value)
    return uint_value


async def recieve_commands(websocket):
    # Continuously receive messages from the client
    async for message in websocket:
        try:                         
            outputs = json.loads((message))
            start = outputs['start']
            setSpeed = outputs['setSpeed']
            waterValve = outputs['waterValve']
    
            try:
                # Attempt to send commands to pump
                Remote_start.set_value(ua.Variant(start, ua.VariantType.Boolean))
                Pump_speed.set_value(ua.Variant(val_to_uint(setSpeed), ua.VariantType.UInt16))
                Water_valve.set_value(ua.Variant(val_to_uint(waterValve), ua.VariantType.UInt16))     
                print(dict(start = start, setSpeed = setSpeed, waterValve = waterValve))
            
            except Exception as e:
                # Print error and do nothing. Extra logic if necessary
                print(e)
                pass
            
        
        except ValueError as e:
            # Send an error message back to the client if the percentage value is invalid
            await websocket.send(str(e))

# Start the websocket server on port 8765
async def main():
    port = 8765
    async with websockets.serve(recieve_commands, "localhost", port):
        print(f'Socket running on port {port} running')
        await asyncio.Future()  # Run forever
        
asyncio.run(main())
