"""
Configuration: equipment endpoints, service ports and paths.

Values resolve in this order: real environment variable, then `.env` in the project
root, then the built-in default.

Lab equipment addresses and OPC UA node ids are site details, not code. They live in
`.env`, which is gitignored; `.env.example` documents every key. The default addresses
below deliberately point at unreachable `.invalid` hosts, so a missing `.env` fails
loudly and obviously instead of quietly trying to reach someone else's network.
"""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = Path(os.environ.get("PRINTMON_ENV", PROJECT_ROOT / ".env"))


def load_env_file(path):
    """Minimal .env reader: KEY=value, '#' comments, optional surrounding quotes."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return {}
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.split(" #")[0].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


_ENV = load_env_file(ENV_FILE)


def get(key, default=None):
    """A real environment variable wins over `.env`, which wins over the default."""
    return os.environ.get(key, _ENV.get(key, default))


# --- equipment (set these in .env) -------------------------------------------------
PUMP_OPCUA_URL = get("PUMP_OPCUA_URL", "opc.tcp://pump-plc.invalid:4840")
GANTRY_OPCUA_URL = get("GANTRY_OPCUA_URL", "opc.tcp://gantry-plc.invalid:4840")
# Index into the scanner's enumerated interfaces (LLT get_device_interfaces_fast).
SCANNER_INTERFACE = int(get("SCANNER_INTERFACE", "0"))

# --- OPC UA node ids (set these in .env) --------------------------------------------
# Which controller variable holds which signal names the PLC program's variables, so
# it is site configuration too. One key per signal, NODE_<SIGNAL>; .env.example lists them.
_missing_nodes = set()


def node(signal):
    """The node id of one signal (`NODE_<signal>` in .env). Raises if it is not set."""
    value = get("NODE_" + signal)
    if not value:
        raise KeyError("NODE_{} is not set in {}".format(signal, ENV_FILE))
    return value


def nodes(signals):
    """{payload key: signal} -> {payload key: node id}, for the signals that are set.
    The others are left out and named by warn_if_unconfigured."""
    found = {}
    for key, signal in signals.items():
        value = get("NODE_" + signal)
        if value:
            found[key] = value
        else:
            _missing_nodes.add("NODE_" + signal)
    return found


# --- local services ----------------------------------------------------------------
INPUTS_WS_HOST = get("INPUTS_WS_HOST", "localhost")
INPUTS_WS_PORT = int(get("INPUTS_WS_PORT", "8001"))
SCAN_WS_HOST = get("SCAN_WS_HOST", "localhost")
SCAN_WS_PORT = int(get("SCAN_WS_PORT", "8764"))

# Full-rate endpoints the viewer and recorder subscribe to.
INPUTS_URL = get("PRINTMON_INPUTS_URL",
                 "ws://{}:{}/full".format(INPUTS_WS_HOST, INPUTS_WS_PORT))
SCAN_URL = get("PRINTMON_SCAN_URL", "ws://{}:{}/full".format(SCAN_WS_HOST, SCAN_WS_PORT))

# --- paths --------------------------------------------------------------------------
DATA_DIR = Path(get("PRINTMON_DATA", PROJECT_ROOT / "data"))
LAKE_DIR = Path(get("PRINTMON_LAKE", PROJECT_ROOT / "lake"))


def missing_equipment(*urls):
    """Which of the given endpoints are still placeholders."""
    return [u for u in urls if ".invalid" in str(u)]


def warn_if_unconfigured(*urls):
    """Print a clear hint when .env has not been filled in. Returns True if unconfigured."""
    missing = missing_equipment(*urls)
    if missing or _missing_nodes:
        print("=" * 72)
        if missing:
            print(" No equipment address configured for: {}".format(", ".join(missing)))
        if _missing_nodes:
            print(" No OPC UA node id configured for: {}".format(", ".join(sorted(_missing_nodes))))
        print(" Copy .env.example to .env and fill it in for this site (see README).")
        print(" Looked for: {}".format(ENV_FILE))
        print("=" * 72)
    return bool(missing or _missing_nodes)
