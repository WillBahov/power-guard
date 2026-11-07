"""Decoding the 80-byte AppleSMC parameter struct, and the live power keys.

Only the pure parts are asserted with synthetic buffers; the ctypes calls are
exercised on a real Mac and must degrade to None rather than raise.
"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from smc import (PARAM_SIZE, OFF_KEY, OFF_DATA, OFF_DATA_SIZE, OFF_DATA_TYPE,
                 OFF_RESULT, fourcc, parse_param, float_value, median,
                 smc_reading)  # noqa: E402


def make_buf(key="PDTR", size=4, data=b"\x8e\x22\x45\x42", result=0, data_type=0x666c7420):
    buf = bytearray(PARAM_SIZE)
    struct.pack_into("<I", buf, OFF_KEY, fourcc(key))
    struct.pack_into("<I", buf, OFF_DATA_SIZE, size)
    struct.pack_into("<I", buf, OFF_DATA_TYPE, data_type)
    buf[OFF_RESULT] = result
    buf[OFF_DATA:OFF_DATA + size] = data
    return bytes(buf)


def test_fourcc_is_big_endian_ashmc_expects():
    assert fourcc("PDTR") == 0x50445452
    assert fourcc("CHTE") == 0x43485445


def test_parse_param_decodes_key_size_and_data():
    parsed = parse_param(make_buf("PSTR", 4, b"\x00\x00\xc0\x42"))
    assert parsed["key"] == fourcc("PSTR")
    assert parsed["data_size"] == 4
    assert parsed["data"][:4] == b"\x00\x00\xc0\x42"
    assert parsed["result"] == 0


def test_parse_param_rejects_short_buffer():
    assert parse_param(b"\x00" * 40) == {}


def test_float_value_reads_little_endian_flt():
    # 0x42c00000 as a float is 48.0
    assert float_value(parse_param(make_buf("PDTR", 4, struct.pack("<f", 48.0))), 4) == 48.0
    # the reply does not echo data_size, so an explicit size is required
    assert float_value(parse_param(make_buf("PDTR", 0, b"")), None) is None


def test_median_of_three_samples():
    assert median([58.0, 44.0, 71.0]) == 58.0
    assert median([None, 12.0]) == 12.0
    assert median([None, None]) is None


def test_smc_reading_returns_watts_or_none():
    reading = smc_reading()
    if reading is None:
        return                        # no AppleSMC on this machine: fine, no crash
    assert reading["source"] == "SMC"
    for key in ("dc_in", "system_load", "battery"):
        assert reading[key] is None or isinstance(reading[key], float)
    assert reading["adapter_present"] is True
    # PDTR/PSTR/SBAP are the keys the guard actually steers on
    assert reading["dc_in"] is not None or reading["system_load"] is not None
