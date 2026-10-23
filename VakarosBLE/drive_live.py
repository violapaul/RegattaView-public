#!/usr/bin/env python3
"""Drive Atlas 2 live nav stream by replicating the app's poll protocol.

Handshake: enable CCCDs -> write 0002 -> write 020b, 020901 -> then poll
0203 <seq16 LE> 0000 at ~5 Hz. Nav stream (ac510005) flows while polled.

Usage: drive_live.py <addr> [duration_s] [start_seq_hex]
"""
import asyncio, json, math, struct, sys, time
from bleak import BleakClient

ADDR = sys.argv[1]
DURATION = float(sys.argv[2]) if len(sys.argv) > 2 else 14.0
START_SEQ = int(sys.argv[3], 16) if len(sys.argv) > 3 else None

CMD  = "ac510002-0000-5a11-0076-616b61726f73"
ST_A = "ac510003-0000-5a11-0076-616b61726f73"
ST_B = "ac510005-0000-5a11-0076-616b61726f73"
TSL, TSH = 1_700_000_000_000, 1_900_000_000_000

RAW = open("live_raw.jsonl", "a")
buf = bytearray(); navs = []; status_pkts = []


def decode():
    i = 0
    while i + 45 <= len(buf):
        if buf[i] == 0x02:
            ts = struct.unpack_from("<Q", buf, i + 1)[0]
            qw, qx, qy, qz = struct.unpack_from("<ffff", buf, i + 29)
            nrm = math.sqrt(qw*qw+qx*qx+qy*qy+qz*qz)
            if TSL <= ts <= TSH and 0.98 <= nrm <= 1.02:
                lat = struct.unpack_from("<i", buf, i+9)[0]*1e-7
                lon = struct.unpack_from("<i", buf, i+13)[0]*1e-7
                f0  = struct.unpack_from("<I", buf, i+17)[0]
                a   = struct.unpack_from("<f", buf, i+21)[0]
                b   = struct.unpack_from("<f", buf, i+25)[0]
                navs.append((ts, lat, lon, f0, a, b, qw, qx, qy, qz)); i += 45; continue
        i += 1
    del buf[:i]


def on_b(_c, data):
    RAW.write(json.dumps({"t": time.monotonic(), "ch": "0x21", "hex": bytes(data).hex()})+"\n")
    buf.extend(bytes(data)[1:]); decode()

def on_a(_c, data):
    b = bytes(data); status_pkts.append(b)
    RAW.write(json.dumps({"t": time.monotonic(), "ch": "0x1c", "hex": b.hex()})+"\n")


async def main():
    global START_SEQ
    async with BleakClient(ADDR, timeout=20.0) as client:
        await client.start_notify(ST_A, on_a)
        await client.start_notify(ST_B, on_b)
        await client.write_gatt_char(CMD, bytes.fromhex("0002"), response=False)
        await asyncio.sleep(0.5)
        await client.write_gatt_char(CMD, bytes.fromhex("020b"), response=False)
        await client.write_gatt_char(CMD, bytes.fromhex("020901"), response=False)
        await asyncio.sleep(0.5)
        # derive a start sequence from the status packet if not given
        if START_SEQ is None:
            # log-head index = offset 27 of the 33B state header (type 0x0203)
            seqs = [struct.unpack_from("<H", p, 27)[0]
                    for p in status_pkts if len(p) == 33 and p[1] == 0x02]
            head = max(seqs) if seqs else 0
            START_SEQ = (head - 0x30) & 0xFFFF  # start slightly behind, catch up
        print(f"status pkts: {[p[:8].hex() for p in status_pkts]}", file=sys.stderr)
        print(f"polling from seq=0x{START_SEQ:04x} ...", file=sys.stderr)
        seq = START_SEQ
        t_end = time.monotonic() + DURATION
        while time.monotonic() < t_end:
            try:
                await client.write_gatt_char(CMD, b"\x02\x03" + struct.pack("<H", seq) + b"\x00\x00", response=False)
            except Exception as e:
                print(f"  [poll write failed: {e} — link likely dropped]", file=sys.stderr)
                if not client.is_connected:
                    print("  disconnected; ending capture (data preserved).", file=sys.stderr)
                    break
            seq = (seq + 1) & 0xFFFF
            await asyncio.sleep(0.2)
        for u in (ST_A, ST_B):
            try: await client.stop_notify(u)
            except Exception: pass
    print(f"\nnav records decoded: {len(navs)}", file=sys.stderr)
    if navs:
        ts0 = navs[0][0]
        for k in range(0, len(navs), max(1, len(navs)//12)):
            ts,lat,lon,f0,a,b,qw,qx,qy,qz = navs[k]
            yaw = math.degrees(math.atan2(2*(qw*qz+qx*qy), 1-2*(qy*qy+qz*qz)))
            print(f"  +{(ts-ts0)/1000:6.2f}s {lat:.6f},{lon:.6f} f0={f0} a={a:.2f} b={b:.1f} yaw={yaw:.1f}")


if __name__ == "__main__":
    asyncio.run(main())
