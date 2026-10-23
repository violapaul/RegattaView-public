#!/usr/bin/env python3
"""Print ATT handle numbers for each characteristic + its CCCD.

HCI sniffs reference ATT handles, not UUIDs. This maps them so a PacketLogger
capture can be read directly.
"""
import asyncio
import sys
from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic

ADDR = sys.argv[1]


async def main():
    async with BleakClient(ADDR, timeout=20.0) as client:
        for svc in client.services:
            for c in svc.characteristics:
                print(f"char  handle=0x{c.handle:04x} ({c.handle:3d})  {c.uuid}  [{','.join(c.properties)}]")
                for d in c.descriptors:
                    print(f"  desc handle=0x{d.handle:04x} ({d.handle:3d})  {d.uuid}")


if __name__ == "__main__":
    asyncio.run(main())
