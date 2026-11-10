import csv
import io
import json

from printmon.schema import peek_scan_time, process_from_csv, process_from_live, scan_from_frame

from .conftest import PUMP_HEADER


def test_live_frame_normalises_and_keeps_unknown_keys():
    frame = {"timestamp": 123, "src": "pump", "seq": 7, "torque": 35, "water_flow": 12,
             "water_temp": 23361, "P0": 0, "pos": [1, 2, 3, 4, 5], "vel": [0, 0, 0, 0, 0],
             "water_pump": False, "torque_ts": 999, "torque_ok": True, "pump_hz": 184.5}
    rec = process_from_live(frame)
    assert rec["t"] == 123 and rec["src"] == "pump" and rec["seq"] == 7
    assert rec["torque"] == 35.0 and rec["P1"] is None
    assert rec["pos"] == (1.0, 2.0, 3.0, 4.0, 5.0)
    assert rec["water_pump"] is False
    # lossless: fields without a column survive in `extra`
    assert rec["extra"] == {"torque_ts": 999, "torque_ok": True, "pump_hz": 184.5}


def test_node_red_csv_row():
    row = next(csv.DictReader(io.StringIO(
        PUMP_HEADER + '\n1747995514985729800,"[500,400,497.24,0,0]","[0,0,-8400,0,0]",'
                      'false,0,38,17,34,21237,9261,3339,2737,32770,285,290,false,false,true\n')))
    rec = process_from_csv(row)
    assert rec["t"] == 1747995514985729800 and rec["src"] is None
    assert rec["torque"] == 17.0 and rec["water_flow"] == 34.0 and rec["water_temp"] == 21237.0
    assert rec["P0"] == 9261.0 and rec["T1"] == 290.0
    assert rec["pos"] == (500.0, 400.0, 497.24, 0.0, 0.0)
    assert rec["vel"][2] == -8400.0
    assert rec["start"] is False and rec["water_valve"] == 38.0 and rec["mixer"] is True
    assert rec["extra"] == {}


def test_scan_frame_replay_and_live():
    replay = scan_from_frame({"time": 5, "x": [1, 2], "z": [0, 3], "m0": [9, 9],
                              "width": 34.6, "X": 500, "Y": 400, "Z": 35, "C": 0})
    assert replay["pos"] == (500.0, 400.0, 35.0)
    assert replay["x"].dtype.name == "float32" and replay["m0"].tolist() == [9, 9]
    live = scan_from_frame({"time": 6, "x": [1], "z": [2], "width": None, "profile_count": 10,
                            "dropped": 0, "scan_hz": 143.0, "shutter_open": 1.5})
    assert live["pos"] is None and live["width"] is None and live["profile_count"] == 10
    assert live["extra"] == {"scan_hz": 143.0}


def test_peek_scan_time_matches_full_parse():
    line = json.dumps({"time": 1747995518428079900, "x": [0.5] * 300})
    assert peek_scan_time(line) == 1747995518428079900
    other_order = json.dumps({"x": [1], "time": 42})
    assert peek_scan_time(other_order) == 42


def test_nc_set_speed_arrives_as_one_element_array():
    rec = process_from_live({"timestamp": 1, "src": "nc", "set_speed": [55.0],
                             "pos": [1, 2, 3, 90, 0]})
    assert rec["set_speed"] == 55.0 and rec["pos"][3] == 90.0
