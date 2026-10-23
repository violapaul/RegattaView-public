#!/usr/bin/env python3
"""Subscribe to the Vakaros notify characteristics and log raw packets.

Usage: notify_log.py <address> [duration_s]
Subscribes to ac510003 and ac510005, printing every notification with a
monotonic timestamp, the source characteristic, length, and hex payload.
Also appends JSONL to capture.jsonl for offline analysis.

Move the unit during capture (tilt = heel/pitch, rotate = heading) so byte
fields can later be correlated with physical motion.
"""
import asyncio
import json
import sys
import time

from bleak import BleakClient

ADDR = sys.argv[1]
DURATION = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0

SVC = "ac510001-0000-5a11-0076-616b61726f73"
NOTIFY = {
    "ac510003-0000-5a11-0076-616b61726f73": "0003",
    "ac510005-0000-5a11-0076-616b61726f73": "0005",
}

t0 = time.monotonic()
counts = {"0003": 0, "0005": 0}
out = open("capture.jsonl", "a")


def make_handler(short):
    def handler(char, data: bytearray):
        dt = time.monotonic() - t0
        counts[short] += 1
        b = bytes(data)
        print(f"[{dt:7.3f}] {short}  len={len(b):3d}  {b.hex()}")
        out.write(json.dumps({"t": round(dt, 4), "ch": short, "hex": b.hex()}) + "\n")
        out.flush()
    return handler


async def main():
    print(f"Connecting to {ADDR} ...", file=sys.stderr)
    async with BleakClient(ADDR, timeout=20.0) as client:
        print(f"Connected: {client.is_connected}. Subscribing ...", file=sys.stderr)
        for uuid, short in NOTIFY.items():
            await client.start_notify(uuid, make_handler(short))
            print(f"  subscribed {short}", file=sys.stderr)
        print(f"Listening {DURATION:.0f}s — MOVE THE UNIT (tilt + rotate) ...\n", file=sys.stderr)
        await asyncio.sleep(DURATION)
        for uuid in NOTIFY:
            try:
                await client.stop_notify(uuid)
            except Exception:
                pass
    print(f"\nPackets: {counts}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
