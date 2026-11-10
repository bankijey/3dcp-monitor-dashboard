import numpy as np
from collections import deque
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from scipy.signal import savgol_filter
import asyncio
import websockets
import json
from opcua import Client, ua
import json
import time
from printmon import config
from printmon.config import GANTRY_OPCUA_URL

try:
    gantry_writer = Client(GANTRY_OPCUA_URL)
    gantry_writer.connect()
    gantry_writer.load_type_definitions()
    feed_override = gantry_writer.get_node(config.node("GANTRY_FEED_OVERRIDE"))
    connected = True


except Exception as e:
    print(e)
    connected = False
    
class ExtrusionController:
    def __init__(self, target_width, initial_feedrate=1.0,
                 max_override=1.5, min_override=0.5,
                 window_size=30, sg_window=11, sg_order=2):
        # Control parameters
        self.target_width = target_width
        self.feedrate = initial_feedrate
        self.max_override = max_override
        self.min_override = min_override

        # Data buffers
        self.width_buffer = deque(maxlen=window_size)
        self.feedrate_buffer = deque(maxlen=window_size)

        # Outlier detection parameters
        self.sg_window = sg_window
        self.sg_order = sg_order
        self.residual_threshold = 3.5  # Modified Z-score threshold

        # Adaptive control parameters
        self.Kp = 0.5  # Initial proportional gain
        self.error_history = deque(maxlen=100)

    def _detect_outliers(self, new_width):
        if len(self.width_buffer) < self.sg_window:
            return False

        # Savitzky-Golay filtering for trend estimation
        window = list(self.width_buffer) + [new_width]
        try:
            trend = savgol_filter(window, self.sg_window, self.sg_order)
            residuals = np.array(window) - trend
        except:
            return False

        # Median Absolute Deviation (MAD) for outlier detection
        median = np.median(residuals)
        mad = np.median(np.abs(residuals - median))
        modified_zscore = 0.6745 * (new_width - trend[-1]) / (mad + 1e-9)

        return abs(modified_zscore) > self.residual_threshold


    def _update_control_parameters(self):
        # Ensure buffers are synchronized and have enough data
        min_len = min(len(self.width_buffer), len(self.feedrate_buffer))
        if min_len < 10:
            return

        # Use synchronized buffer segments
        widths = np.array(list(self.width_buffer)[-min_len:])
        feedrates = np.array(list(self.feedrate_buffer)[-min_len:])

        delta_w = np.diff(widths)
        delta_f = np.diff(feedrates)

        if len(delta_w) != len(delta_f) or len(delta_w) == 0:
            return

        # Calculate response using numpy's nan-safe operations
        with np.errstate(divide='ignore', invalid='ignore'):
            response_ratios = delta_w / delta_f
            valid_ratios = response_ratios[np.isfinite(response_ratios)]

            if len(valid_ratios) == 0:
                return

            response = np.nanmedian(valid_ratios)
        print(f"Response: {response}")
        
        # error = self.error_history[-1]
        # if response != 0:
        #      # In _update_control_parameters()
        #     if abs(error) < 1.0:  # Near target
        #         self.Kp = np.clip(0.5 / abs(response), -0.3, 0.3)
        #     elif abs(error) > 3.0:  # Large deviation
        #         self.Kp = np.clip(1.2 / abs(response), -3, 3)
        #     else:  # Moderate error
        #         self.Kp = np.clip(0.7 / abs(response), 0.1, 1)
        if response != 0:
            self.Kp = np.clip(0.5 / abs(response), 0.1, 2.0)
            # self.Kp = np.clip(0.7 / abs(response), -3.0, 3.0)

    def input_output_plot(self):
        # Create a figure with subplots
        fig = make_subplots(specs=[[{"secondary_y": True}]])

        # Add traces for width and feedrate
        fig.add_trace(
            go.Scatter(x=np.arange(len(self.width_buffer)), y=np.array(self.width_buffer), name="Width"),
            secondary_y=False,
        )

        fig.add_trace(
            go.Scatter(x=np.arange(len(self.feedrate_buffer)), y=np.array(self.feedrate_buffer),  line_shape = "hvh",  name="Feedrate"),
            secondary_y=True,
        )

        # Update layout for better visualization
        fig.update_layout(
            title_text="Width and Feedrate",  # Set the title of the plot
            xaxis_title="Data Points",  # Label the x-axis
        )

        fig.update_yaxes(title_text="Width", secondary_y=False, range = [15, 50])  # Label primary y-axis
        fig.update_yaxes(title_text="Feedrate" ,secondary_y=True)  # Label secondary y-axis

        return fig


    def update(self, measured_width):
        # Detect and handle outliers
        if self._detect_outliers(measured_width):
            return self.feedrate

        self.width_buffer.append(measured_width)

        # Calculate width error
        error = measured_width - self.target_width
        self.error_history.append(error)

        # Update control parameters from historical data
        self._update_control_parameters()

        # Proportional control with smoothing
        smoothed_error = np.median(list(self.error_history)[-5:])
        feedrate_adjust = 1 - self.Kp * smoothed_error / self.target_width

        # Apply feedrate override limits
        new_feedrate = np.clip(self.feedrate/feedrate_adjust,
                              self.min_override, self.max_override)

        self.feedrate_buffer.append(new_feedrate)
        self.feedrate = new_feedrate
        # print(f"feed buffer {list(self.feedrate_buffer)[-3:]}, width buffer {list(self.width_buffer)[-3:]}")
        print(f"Kp {self.Kp}")
        # print(f"Error {self.error_history}")
        # print(f"max override {self.max_override}, min override {self.min_override}, feedrate adjust {feedrate_adjust}")
        return new_feedrate

last_width = 40
async def compute_output(websocket, path):
    controller = ExtrusionController(target_width=33)
    # Continuously receive messages from the client
    async for message in websocket:
        # print(message)
        global last_width
        data = json.loads((message))
        measured = data.get('measured_width', last_width)
        
        
        # try:
        #     measured /= (list(controller.feedrate_buffer)[-1]+random.gauss(0, 0.1))
        # except:
        #     pass
        target = data.get('target_width', 35)
        controller.target_width = target
        
        new_feedrate = controller.update(measured)
        if connected:
            feed_override.set_value(int(new_feedrate*100), ua.VariantType.UInt16)
        
        
        print(f"Target: {target:.1f}mm Measured: {measured:.2f}mm FOVRD: {new_feedrate:.2f}")
        
        
        payload = {
            'timestamp': time.time_ns(),
            'FOVRD': new_feedrate,
            'feed_buffer': list(controller.feedrate_buffer),
            'width_buffer': list(controller.width_buffer)
            }
        last_width = data.get(measured, last_width - (2*new_feedrate))
        await websocket.send(json.dumps(payload))
        await asyncio.sleep(1e-5)
        
    
# Start the websocket server on port 8765
async def main():
    async with websockets.serve(compute_output, "localhost", 8564):
        print(f'Socket running on port {8564} running')
        await asyncio.Future()  # Run forever
        
asyncio.run(main())
    
   