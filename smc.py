"""Live power watts from the AppleSMC user client - no root, no Stats, no powermetrics.

Measured on macOS 27.2 (M-series, 100 W adapter, 2026-11-06): `IOServiceOpen` on
`AppleSMC` succeeds from an ordinary unprivileged process with user-client type 0,
and the power keys read faster than once per second. This is the sensor the guard
has been missing: it replaces both the Stats LevelDB scrape and `sudo powermetrics`.

Writing is a different matter. A write attempt from an ordinary process returns
kIOReturnNotPrivileged (0xe00002c1) - so the SMC is a free *reader* and a root-only
*actuator*. That is why ChargeWatch and Battery-Toolkit install a root LaunchDaemon
instead of reading the keys in-process.

Keys this machine answers:

    PDTR        DC power in from the adapter (W)
    PSTR        system load (W)
    SBAP        battery power (W); negative = the cell is feeding the gap
    SBAV        battery voltage (V)
    ID0R        battery current (A)
    TB0T/1T/2T  cell temperatures (C)
    AC-W        active AC port index; >= 0 means the plug is physically in
    CHIE/CHSC/CHHC  charge state (present)
    CHTE/CH0C/CH0J  charge-disable / adapter-cloak (absent here)

`PDTR` and `PSTR` are instantaneous and jump around (68 W then 36 W in consecutive
ticks), so `reading()` returns a median of the last three samples as the headline
number and keeps the raw values for anyone who wants the jitter.

Stdlib + ctypes only. Every call degrades to None instead of raising.
"""
from __future__ import annotations

import ctypes
import struct
from collections import deque

_IOKIT = "/System/Library/Frameworks/IOKit.framework/IOKit"

# The SMC user client takes an 80-byte parameter struct. These offsets are the
# kernel ABI, confirmed against SMCKit, macmon and Battery-Toolkit.
PARAM_SIZE = 80
OFF_KEY = 0
OFF_DATA_SIZE = 28
OFF_DATA_TYPE = 32
OFF_ATTRIBUTES = 36
OFF_RESULT = 40
OFF_STATUS = 41
OFF_COMMAND = 42
OFF_INDEX = 44
OFF_DATA = 48

CMD_READ = 5
CMD_INDEX = 8
CMD_KEYINFO = 9
CMD_WRITE = 6

KIOReturnSuccess = 0

POWER_KEYS = ("PDTR", "PSTR", "SBAP", "SBAV", "ID0R")
TEMP_KEYS = ("TB0T", "TB1T", "TB2T")
# Keys a charge-control actuator would need. Only the ones that exist can be
# written, and only by root.
CHARGE_KEYS = ("CHTE", "CH0C", "CHIE", "CH0J")
NOT_PRIVILEGED = 0xE00002C1


def parse_param(buf: bytes) -> dict:
    """Decode one 80-byte SMC parameter struct. Pure, so it is testable."""
    if len(buf) < PARAM_SIZE:
        return {}
    return {
        "key": struct.unpack_from("<I", buf, OFF_KEY)[0],
        "data_size": struct.unpack_from("<I", buf, OFF_DATA_SIZE)[0],
        "data_type": struct.unpack_from("<I", buf, OFF_DATA_TYPE)[0],
        "attributes": buf[OFF_ATTRIBUTES],
        "result": buf[OFF_RESULT],
        "status": buf[OFF_STATUS],
        "data": bytes(buf[OFF_DATA:OFF_DATA + 32]),
    }


def fourcc(name: str) -> int:
    """'PDTR' -> 0x50445452, the way the SMC names its keys."""
    value = 0
    for ch in name.encode():
        value = (value << 8) | ch
    return value


def float_value(parsed: dict, size: int | None = None) -> float | None:
    """A 'flt ' key is four little-endian bytes.

    The response struct does not echo `data_size` (it comes back as 0), so the
    size has to come from the key's metadata, not from the reply.
    """
    size = size if size is not None else parsed.get("data_size")
    if size != 4 or parsed.get("data") is None:
        return None
    return struct.unpack("<f", parsed["data"][:4])[0]


def median(values):
    values = [v for v in values if v is not None]
    if not values:
        return None
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


class _SMC:
    """A lazily opened, process-long handle to the AppleSMC user client."""

    def __init__(self):
        self._io = None
        self._conn = None
        self._failed = False
        self._info: dict[str, tuple[int, bytes]] = {}
        self._recent: dict[str, deque] = {k: deque(maxlen=3) for k in POWER_KEYS}
        self._ceiling = None            # highest DC-in seen since we opened

    def _load(self) -> bool:
        if self._conn is not None:
            return True
        if self._failed:
            return False
        try:
            libc = ctypes.cdll.LoadLibrary("libc.dylib")
            io = ctypes.cdll.LoadLibrary(_IOKIT)
        except OSError:
            self._failed = True
            return False
        io.IOServiceGetMatchingService.argtypes = [ctypes.c_ulong, ctypes.c_void_p]
        io.IOServiceGetMatchingService.restype = ctypes.c_ulong
        io.IOServiceMatching.restype = ctypes.c_void_p
        io.IOServiceOpen.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_int,
                                     ctypes.POINTER(ctypes.c_ulong)]
        io.IOServiceOpen.restype = ctypes.c_int
        io.IOConnectCallStructMethod.argtypes = [ctypes.c_ulong, ctypes.c_uint32,
                                                ctypes.c_void_p, ctypes.c_size_t,
                                                ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t)]
        io.IOConnectCallStructMethod.restype = ctypes.c_int
        io.IOServiceClose.argtypes = [ctypes.c_ulong]
        io.IOObjectRelease.argtypes = [ctypes.c_ulong]

        service = io.IOServiceGetMatchingService(0, io.IOServiceMatching(b"AppleSMC"))
        if not service:
            self._failed = True
            return False
        conn = ctypes.c_ulong(0)
        if io.IOServiceOpen(service, libc.mach_task_self(), 0, ctypes.byref(conn)) != 0:
            io.IOObjectRelease(service)
            self._failed = True
            return False
        io.IOObjectRelease(service)
        self._io = io
        self._conn = conn.value
        return True

    def _call(self, key: str, command: int, size: int = 0, index: int = 0):
        if not self._load():
            return None
        buf_in = (ctypes.c_uint8 * PARAM_SIZE)()
        buf_out = (ctypes.c_uint8 * PARAM_SIZE)()
        struct.pack_into("<I", buf_in, OFF_KEY, fourcc(key))
        struct.pack_into("<I", buf_in, OFF_DATA_SIZE, size)
        struct.pack_into("<I", buf_in, OFF_INDEX, index)
        buf_in[OFF_COMMAND] = command
        out_size = ctypes.c_size_t(PARAM_SIZE)
        kr = self._io.IOConnectCallStructMethod(
            self._conn, 2, buf_in, PARAM_SIZE, buf_out, ctypes.byref(out_size))
        if kr != KIOReturnSuccess:
            return None
        parsed = parse_param(bytes(buf_out))
        if parsed.get("result") != 0:
            return None
        return parsed

    def key_info(self, key: str) -> dict | None:
        """Metadata for a key: does it exist, how big is it, what type."""
        if key in self._info:
            return self._info[key]
        parsed = self._call(key, CMD_KEYINFO)
        if not parsed:
            return None
        # The key type is the fourCC at OFF_DATA_TYPE ("flt ", "int ", "u8 "),
        # not part of the returned data block.
        info = {"size": parsed["data_size"],
                "attributes": parsed["attributes"],
                "type": struct.pack(">I", parsed["data_type"]).decode("latin-1").strip()}
        self._info[key] = info
        return info

    def read_float(self, key: str) -> float | None:
        info = self.key_info(key)
        if not info or info["size"] != 4:
            return None
        parsed = self._call(key, CMD_READ, info["size"])
        if not parsed:
            return None
        return float_value(parsed, info["size"])

    def read_int(self, key: str) -> int | None:
        info = self.key_info(key)
        if not info or not 0 < info["size"] <= 4:
            return None
        parsed = self._call(key, CMD_READ, info["size"])
        if not parsed:
            return None
        return int.from_bytes(parsed["data"][:info["size"]], "big")

    def write(self, key: str, value: int) -> bool:
        """Write a key. From an unprivileged process this returns False with
        kIOReturnNotPrivileged; under root it succeeds on the charge keys."""
        info = self.key_info(key)
        if not info:
            return False
        buf_in = (ctypes.c_uint8 * PARAM_SIZE)()
        buf_out = (ctypes.c_uint8 * PARAM_SIZE)()
        struct.pack_into("<I", buf_in, OFF_KEY, fourcc(key))
        struct.pack_into("<I", buf_in, OFF_DATA_SIZE, info["size"])
        struct.pack_into("<I", buf_in, OFF_INDEX, value)
        buf_in[OFF_COMMAND] = CMD_WRITE
        # Data bytes are little-endian (the same way the 'flt ' values come back),
        # so CHTE=1 is [0x01, 0, 0, 0] - matching Battery-Toolkit's on/off tables.
        for i in range(info["size"]):
            buf_in[OFF_DATA + i] = (value >> (8 * i)) & 0xFF
        out_size = ctypes.c_size_t(PARAM_SIZE)
        kr = self._io.IOConnectCallStructMethod(
            self._conn, 2, buf_in, PARAM_SIZE, buf_out, ctypes.byref(out_size))
        return kr == KIOReturnSuccess and buf_out[OFF_RESULT] == 0

    def reading(self) -> dict | None:
        if not self._load():
            return None
        raw = {key: self.read_float(key) for key in POWER_KEYS}
        if all(v is None for v in raw.values()):
            return None
        for key, value in raw.items():
            if value is not None:
                self._recent[key].append(value)
        dc_in = raw.get("PDTR")
        if dc_in is not None and (self._ceiling is None or dc_in > self._ceiling):
            self._ceiling = dc_in
        temps = [self.read_float(k) for k in TEMP_KEYS]
        ac_port = self.read_int("AC-W")
        return {
            "source": "SMC",
            "dc_in": median(self._recent["PDTR"]),
            "dc_in_raw": dc_in,
            "system_load": median(self._recent["PSTR"]),
            "battery": median(self._recent["SBAP"]),
            "battery_raw": raw.get("SBAP"),
            "battery_voltage": raw.get("SBAV"),
            "battery_current": raw.get("ID0R"),
            "cell_temps": [t for t in temps if t is not None],
            "adapter_present": ac_port is not None and ac_port >= 0,
            "observed_adapter_ceiling": self._ceiling,
        }

    def charge_capability(self) -> dict:
        """Which charge-control keys this Mac exposes, and whether they look writable."""
        present = {}
        for key in CHARGE_KEYS:
            info = self.key_info(key)
            if info:
                present[key] = {"size": info["size"],
                                "attributes": info["attributes"],
                                "writable_flag": bool(info["attributes"] & 0x80)}
        return {
            "keys": present,
            "charge_disable": any(k in present for k in ("CHTE", "CH0C")),
            "adapter_cloak": any(k in present for k in ("CHIE", "CH0J")),
        }

    def close(self) -> None:
        if self._conn is not None and self._io is not None:
            self._io.IOServiceClose(self._conn)
            self._conn = None


_handle = _SMC()


def smc_reading() -> dict | None:
    """Current watts from the SMC, or None if the node is unreachable."""
    return _handle.reading()


def smc_charge_capability() -> dict:
    """What a charge-control actuator could do on this machine."""
    return _handle.charge_capability()


def smc_write(key: str, value: int) -> bool:
    """Attempt a write. Needs root for anything that matters."""
    return _handle.write(key, value)


if __name__ == "__main__":
    # python3 smc.py                      -> live readings
    # python3 smc.py probe                -> key table for charge control
    # sudo python3 smc.py write CHTE 1    -> act on a charge key (root required)
    import sys
    import time

    if len(sys.argv) > 1 and sys.argv[1] == "probe":
        cap = smc_charge_capability()
        print("charge-control keys present:", cap)
        for key, info in cap["keys"].items():
            print(f"  {key}: size={info['size']} attrs={info['attributes']:#04x} "
                  f"writable-flag={info['writable_flag']}")
    elif len(sys.argv) > 3 and sys.argv[1] == "write":
        key, value = sys.argv[2], int(sys.argv[3])
        ok = smc_write(key, value)
        print(f"write {key}={value}: {'ok' if ok else 'refused'}")
        sys.exit(0 if ok else 1)
    else:
        for _ in range(10):
            print(smc_reading())
            time.sleep(1)
