#!/usr/bin/env python3
"""Connect to a BLE peripheral and dump its full GATT profile.

Usage: gatt_dump.py <address-or-uuid>
Lists every service + characteristic with properties; reads the value of any
characteristic that is 'read'-able (safe). Flags notify/indicate characteristics
(those are where streaming telemetry lives).
"""
import asyncio
import sys
from bleak import BleakClient

ADDR = sys.argv[1]


def props(c):
    return ",".join(c.properties)


async def main():
    print(f"Connecting to {ADDR} ...", file=sys.stderr)
    async with BleakClient(ADDR, timeout=20.0) as client:
        print(f"Connected: {client.is_connected}\n")
        for svc in client.services:
            print(f"SERVICE {svc.uuid}  ({svc.description})")
            for c in svc.characteristics:
                tag = ""
                if "notify" in c.properties or "indicate" in c.properties:
                    tag = "   <== STREAM (notify/indicate)"
                print(f"  CHAR {c.uuid}  [{props(c)}]  {c.description}{tag}")
                if "read" in c.properties:
                    try:
                        val = await client.read_gatt_char(c)
                        printable = val.decode("utf-8", "replace") if val else ""
                        print(f"       read = {val.hex()}  {printable!r}")
                    except Exception as e:
                        print(f"       read failed: {e}")
                for d in c.descriptors:
                    try:
                        dv = await client.read_gatt_descriptor(d.handle)
                        print(f"       descr {d.uuid} = {dv.hex()}")
                    except Exception:
                        pass
        print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
