#!/usr/bin/env python3
"""Scan BLE and list devices, sorted by signal, flagging likely Vakaros units.

macOS CoreBluetooth note: device identifiers are per-host UUIDs, NOT MAC
addresses. That UUID is what we connect with from this Mac.
"""
import asyncio
import sys
from bleak import BleakScanner

DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0

# Vakaros product names seen in the wild: "Atlas", "Atlas 2".
HINTS = ("vakaros", "atlas")


async def main():
    print(f"Scanning {DURATION:.0f}s ...", file=sys.stderr)
    devices = await BleakScanner.discover(timeout=DURATION, return_adv=True)

    rows = []
    for addr, (dev, adv) in devices.items():
        name = adv.local_name or dev.name or ""
        rows.append((adv.rssi if adv.rssi is not None else -999, addr, name, adv))

    rows.sort(reverse=True)
    print(f"\nFound {len(rows)} devices:\n")
    for rssi, addr, name, adv in rows:
        likely = any(h in (name or "").lower() for h in HINTS)
        flag = "  <-- LIKELY VAKAROS" if likely else ""
        svcs = ",".join(adv.service_uuids) if adv.service_uuids else "-"
        mfr = ""
        if adv.manufacturer_data:
            mfr = " ".join(f"{cid:#06x}:{data.hex()}" for cid, data in adv.manufacturer_data.items())
        print(f"[{rssi:4d} dBm] {addr}  name={name!r:24} svcs={svcs}{flag}")
        if mfr:
            print(f"             mfr_data: {mfr}")


if __name__ == "__main__":
    asyncio.run(main())
