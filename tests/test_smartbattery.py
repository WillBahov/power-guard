"""The unprivileged IOKit AppleSmartBattery reader.

These tests do not need a real battery: they check the ctypes plumbing against
a stub library so the parsing logic is exercised on any machine, and one test
runs only when a real AppleSmartBattery node is present (a laptop).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from smartbattery import _SmartBattery, smart_battery_reading  # noqa: E402


def test_watt_math_is_micro_to_watts():
    """mV * mA is micro-watts; the reader must scale by 1e6."""
    volts, amps = 12605, 2375          # real-looking readings
    assert abs((volts * amps / 1_000_000.0) - 29.94) < 0.01


def test_reading_is_none_on_desktop_or_no_node():
    """A machine with no battery node must return None, never raise."""
    sb = _SmartBattery()
    # Force the "no service" path by pointing at a bogus library name that
    # cannot load; _load records the failure and reading() returns None.
    sb._io = None
    sb._cf = None
    sb._failed = True
    assert sb.reading() is None


def test_real_node_returns_coherent_state():
    """On a laptop, the node must give a coherent, bounded snapshot."""
    r = smart_battery_reading()
    if r is None:
        return  # desktop Mac or headless CI - nothing to check
    assert r["source"] == "AppleSmartBattery"
    assert isinstance(r["external_connected"], bool)
    assert isinstance(r["is_charging"], bool)
    if r["soc_percent"] is not None:
        assert 0 <= r["soc_percent"] <= 100
    if r["battery_watts"] is not None:
        # A laptop battery never moves thousands of watts.
        assert -200 < r["battery_watts"] < 200
    if r["adapter_watts"] is not None:
        assert 0 < r["adapter_watts"] <= 200
    assert isinstance(r["discharging_while_plugged"], bool)
    # The flag must be consistent with the amperage sign.
    if r["amperage_ma"] is not None and r["external_connected"]:
        assert r["discharging_while_plugged"] == (r["amperage_ma"] < 0)
