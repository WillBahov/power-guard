"""Live battery/adapter state from the IOKit `AppleSmartBattery` node.

WhatBattery (github.com/WillBahov/whatbattery) reads this node with plain
IOKit calls - no entitlement, no kernel extension, no root. That is the whole
trick: an ordinary unprivileged process *can* open `AppleSmartBattery` and read
its keys one at a time. What it cannot do is open `AppleSMC`, which is why
WhatBattery ships a signed entitlement for the live SMC rails (PPBR/PDTR) and
we cannot copy that here.

This module deliberately mirrors only what power-guard needs:

  * `ExternalConnected`   - is the adapter attached at all
  * `IsCharging`          - is current actually flowing into the cell
  * `FullyCharged`        - the gauge says the pack is full
  * `ChargerData.NotChargingReason` - *why* charging is stopped when plugged in
  * `AdapterDetails.Watts` - the adapter's rating, so the ceiling is real
  * `Amperage`/`InstantAmperage`/`Voltage` - battery current and voltage

Stdlib only (ctypes). Python 3.9+, macOS. Every call degrades to None rather
than raising, exactly like WhatBattery's readers: if the node is missing (a
desktop Mac) or a key is absent, the caller falls back to pmset/SOC drift.

Do NOT bulk-fetch `BatteryData` as one blob. The kernel can hand back a
malformed binary plist during teardown and CoreFoundation aborts the process
inside IOCFUnserializeBinary - WhatCable issue #181, carried here as the
per-key read loop. Read keys individually.
"""
from __future__ import annotations

import ctypes

_IOKIT = "/System/Library/Frameworks/IOKit.framework/IOKit"
_COREFOUNDATION = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"

# CoreFoundation number type ids on macOS (CFGetTypeID of a CFNumber).
# We only ever call CFNumberGetValue on values we have confirmed are numbers.
_CF_NUMBER_TYPE_IDS = {21, 22}   # observed: bool-ish and int/float CFNumbers


class _SmartBattery:
    """A lazily opened handle to the AppleSmartBattery IOKit node."""

    def __init__(self):
        self._io = None
        self._cf = None
        self._entry = None
        self._failed = False

    def _load(self):
        if self._io is not None:
            return True
        if self._failed:
            return False
        try:
            self._io = ctypes.cdll.LoadLibrary(_IOKIT)
            self._cf = ctypes.cdll.LoadLibrary(_COREFOUNDATION)
        except OSError:
            self._failed = True
            return False
        cf, io = self._cf, self._io
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFGetTypeID.restype = ctypes.c_ulong
        cf.CFGetTypeID.argtypes = [ctypes.c_void_p]
        cf.CFNumberGetValue.restype = ctypes.c_bool
        cf.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        cf.CFDictionaryGetValue.restype = ctypes.c_void_p
        cf.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        io.IOServiceMatching.restype = ctypes.c_void_p
        io.IOServiceGetMatchingServices.argtypes = [
            ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)
        ]
        io.IOIteratorNext.argtypes = [ctypes.c_int]
        io.IOIteratorNext.restype = ctypes.c_int
        io.IOObjectRelease.argtypes = [ctypes.c_int]
        io.IORegistryEntryCreateCFProperty.argtypes = [
            ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32
        ]
        io.IORegistryEntryCreateCFProperty.restype = ctypes.c_void_p
        return True

    def _entry_or_open(self):
        if self._entry is not None:
            return self._entry
        if not self._load():
            return None
        cf, io = self._cf, self._io
        it = ctypes.c_int(0)
        if io.IOServiceGetMatchingServices(0, io.IOServiceMatching(b"AppleSmartBattery"),
                                          ctypes.byref(it)) != 0:
            self._failed = True
            return None
        entry = io.IOIteratorNext(it.value)
        io.IOObjectRelease(it.value)
        if not entry:
            self._failed = True
            return None
        # Desktop Macs expose the node but report BatteryInstalled == false.
        installed = self._number(entry, "BatteryInstalled")
        if installed is not None and not installed:
            io.IOObjectRelease(entry)
            self._failed = True
            return None
        self._entry = entry
        return entry

    def _number(self, entry, key):
        """Read one key, returning int/float/bool or None. Never touches a dict."""
        cf, io = self._cf, self._io
        s = cf.CFStringCreateWithCString(None, key.encode(), 0)
        v = io.IORegistryEntryCreateCFProperty(entry, s, None, 0)
        if not v:
            return None
        if cf.CFGetTypeID(v) not in _CF_NUMBER_TYPE_IDS:
            return None
        buf = ctypes.c_int64(0)
        if cf.CFNumberGetValue(v, 4, ctypes.byref(buf)):
            return buf.value
        dbl = ctypes.c_double(0)
        if cf.CFNumberGetValue(v, 5, ctypes.byref(dbl)):
            return dbl.value
        return None

    def _dict_number(self, entry, dict_key, sub_key):
        """Read a number nested inside a dictionary-valued key."""
        cf, io = self._cf, self._io
        s = cf.CFStringCreateWithCString(None, dict_key.encode(), 0)
        d = io.IORegistryEntryCreateCFProperty(entry, s, None, 0)
        if not d:
            return None
        sub = cf.CFStringCreateWithCString(None, sub_key.encode(), 0)
        val = cf.CFDictionaryGetValue(d, sub)
        if not val:
            return None
        buf = ctypes.c_int64(0)
        if cf.CFNumberGetValue(val, 4, ctypes.byref(buf)):
            return buf.value
        dbl = ctypes.c_double(0)
        if cf.CFNumberGetValue(val, 5, ctypes.byref(dbl)):
            return dbl.value
        return None

    def reading(self) -> dict | None:
        """The current battery/adapter state, or None if unavailable."""
        entry = self._entry_or_open()
        if entry is None:
            return None
        volts = self._number(entry, "Voltage")          # millivolts
        amps = self._number(entry, "InstantAmperage")   # milliamps
        if amps is None:
            amps = self._number(entry, "Amperage")
        # mV * mA = micro-watts; divide by 1e6 for watts.
        watts = (volts * amps / 1_000_000.0) if (volts is not None and amps is not None) else None
        return {
            "source": "AppleSmartBattery",
            "external_connected": bool(self._number(entry, "ExternalConnected")),
            "is_charging": bool(self._number(entry, "IsCharging")),
            "fully_charged": bool(self._number(entry, "FullyCharged")),
            "soc_percent": self._number(entry, "CurrentCapacity"),
            "voltage_mv": volts,
            "amperage_ma": amps,
            "battery_watts": watts,          # + = charging, - = discharging, 0 = parked
            "adapter_watts": self._dict_number(entry, "AdapterDetails", "Watts"),
            "not_charging_reason": self._dict_number(entry, "ChargerData", "NotChargingReason"),
        }


# Shared handle so repeated calls do not reopen the IOKit service.
_handle = _SmartBattery()


def smart_battery_reading() -> dict | None:
    """Public entry point: current AppleSmartBattery state or None."""
    return _handle.reading()
