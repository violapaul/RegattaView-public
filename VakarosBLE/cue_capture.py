#!/usr/bin/env python3
"""Cued ping-press capture: drives the Atlas live stream AND prints timed press
cues (with terminal bell) so the operator presses on cue. Cue times are logged
into the same monotonic clock as the BLE data for exact correlation.

Run it yourself (in this directory) so you see the cues live:
  python cue_capture.py [device-uuid]

The device UUID defaults to the one used during development; pass your own as
argv[1] (macOS CoreBluetooth ids are per-host — find yours with scan.py).
"""
import asyncio, json, os, struct, sys, time
from bleak import BleakClient

# macOS CoreBluetooth per-host id (developer default; override via argv[1]).
ADDR = sys.argv[1] if len(sys.argv) > 1 else "204E4874-3B0F-F4C3-91F2-3F2C0520E206"
CMD  = "ac510002-0000-5a11-0076-616b61726f73"
ST_A = "ac510003-0000-5a11-0076-616b61726f73"
ST_B = "ac510005-0000-5a11-0076-616b61726f73"

# (offset_seconds, n_presses, label)
CUES = [(10, 1, "PRESS  >>> ONCE <<<"),
        (22, 2, "PRESS  >>> TWICE <<<  (two quick)"),
        (34, 3, "PRESS  >>> THREE TIMES <<<  (three quick)")]
DURATION = 44

RAW = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "live_raw.jsonl"), "a")
status_pkts = []


def log(ch, payload):
    RAW.write(json.dumps({"t": time.monotonic(), "ch": ch, **payload}) + "\n"); RAW.flush()

def on_a(_c, data):
    b = bytes(data); status_pkts.append(b); log("0x1c", {"hex": b.hex()})
def on_b(_c, data):
    log("0x21", {"hex": bytes(data).hex()})


async def cue_loop(t0):
    print("\n=== GET READY — keep the Atlas by the Mac ===", flush=True)
    for off, n, label in CUES:
        while time.monotonic() - t0 < off - 3:
            await asyncio.sleep(0.1)
        for c in (3, 2, 1):
            print(f"  {label}  in {c}...", flush=True); await asyncio.sleep(1.0)
        log("cue", {"n": n, "off": off})
        print(f"\a\n>>>>>>>>>>  {label}   ({n}x)  <<<<<<<<<<\n", flush=True)
    print("...hold, finishing capture...", flush=True)


async def main():
    print(f"Connecting to Atlas {ADDR} ...", flush=True)
    async with BleakClient(ADDR, timeout=20.0) as client:
        await client.start_notify(ST_A, on_a)
        await client.start_notify(ST_B, on_b)
        await client.write_gatt_char(CMD, bytes.fromhex("0002"), response=False)
        await asyncio.sleep(0.5)
        await client.write_gatt_char(CMD, bytes.fromhex("020b"), response=False)
        await client.write_gatt_char(CMD, bytes.fromhex("020901"), response=False)
        await asyncio.sleep(0.5)
        seqs = [struct.unpack_from("<H", p, 27)[0] for p in status_pkts if len(p) == 33 and p[1] == 0x02]
        seq = ((max(seqs) if seqs else 0) - 0x30) & 0xFFFF
        t0 = time.monotonic()
        cue_task = asyncio.create_task(cue_loop(t0))
        while time.monotonic() - t0 < DURATION:
            try:
                await client.write_gatt_char(CMD, b"\x02\x03" + struct.pack("<H", seq) + b"\x00\x00", response=False)
            except Exception as e:
                if not client.is_connected:
                    print(f"[link dropped: {e}]", flush=True); break
            seq = (seq + 1) & 0xFFFF
            await asyncio.sleep(0.2)
        await cue_task
        for u in (ST_A, ST_B):
            try: await client.stop_notify(u)
            except Exception: pass
    print("\nDONE — capture saved to live_raw.jsonl.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
