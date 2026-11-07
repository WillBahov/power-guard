"""Reading the live adapter/battery watts that the Stats app already samples."""
import json
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sensor import StatsSensor, parse_power, sensor_feed  # noqa: E402

RECORD = json.dumps({
    "adapterPower": 62.4, "batteryPower": -11.2, "ACwatts": 94,
    "level": 0.76, "isCharging": False, "powerSource": "AC Power",
}).encode()


def test_stats_sensor_reads_the_latest_record(tmp_path):
    d = tmp_path / "lldb"
    d.mkdir()
    (d / "000001.log").write_bytes(b"binary junk\x00" + RECORD + b"\x00trailing")
    sensor = StatsSensor(str(d))
    feed = sensor_feed(sensor, False, use_smc=False)
    assert feed["name"] == "Stats"
    assert feed["dc_in"] == 62.4            # "DC in" - watts from the adapter
    assert feed["battery"] == -11.2        # negative = the battery is feeding the gap
    assert feed["adapter_rating"] == 94


def test_stats_sensor_is_none_when_absent(tmp_path):
    assert StatsSensor(str(tmp_path)).reading() is None


def test_powermetrics_parser():
    p = parse_power("Battery Power: -18.5 W\nSystem Power: 91.2 W\n")
    assert p["battery power"] == -18.5
    assert p["system power"] == 91.2
