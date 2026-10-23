#!/usr/bin/env python3
"""Decode an Apple PacketLogger (.pklg) capture down to ATT operations.

No Wireshark/tshark needed. Parses PacketLogger records -> HCI ACL ->
L2CAP (reassembling fragments) -> ATT, and prints every ATT write /
notification / read with timestamp, direction, handle, and hex value.

Vakaros Atlas 2 handles (from live GATT enumeration) are annotated inline:
    0x0019 ac510002 write   (cmd A)
    0x001b ac510003 notify  (stream A)
    0x001d   CCCD of 0x001b
    0x001e ac510004 write   (cmd B)
    0x0020 ac510005 notify  (stream B)
    0x0022   CCCD of 0x0020

Usage: parse_pklg.py <file.pklg> [--all]
  --all  also print MTU/discovery/read traffic (default: only the vendor
         service handles + CCCD writes, which is what matters here).
"""
import struct
import sys

PATH = sys.argv[1]
SHOW_ALL = "--all" in sys.argv[2:]

HANDLE_NAME = {
    0x0019: "ac510002 WRITE(cmdA)",
    0x001b: "ac510003 NOTIFY(streamA)",
    0x001d: "  cccd(streamA)",
    0x001e: "ac510004 WRITE(cmdB)",
    0x0020: "ac510005 NOTIFY(streamB)",
    0x0022: "  cccd(streamB)",
}
VENDOR_HANDLES = set(HANDLE_NAME)

ATT_OPS = {
    0x01: "ERROR_RSP", 0x02: "MTU_REQ", 0x03: "MTU_RSP",
    0x04: "FIND_INFO_REQ", 0x05: "FIND_INFO_RSP",
    0x08: "READ_BY_TYPE_REQ", 0x09: "READ_BY_TYPE_RSP",
    0x0A: "READ_REQ", 0x0B: "READ_RSP",
    0x10: "READ_BY_GROUP_REQ", 0x11: "READ_BY_GROUP_RSP",
    0x12: "WRITE_REQ", 0x13: "WRITE_RSP", 0x52: "WRITE_CMD",
    0x16: "PREP_WRITE_REQ", 0x18: "EXEC_WRITE_REQ",
    0x1B: "NOTIFY", 0x1D: "INDICATE", 0x1E: "CONFIRM",
    0x0C: "READ_BLOB_REQ", 0x0D: "READ_BLOB_RSP",
}
WRITE_OR_NOTIFY = {0x12, 0x52, 0x1B, 0x1D}  # ops with handle+value layout


def records(buf):
    """Yield (ts, type, payload) PacketLogger records."""
    off, n = 0, len(buf)
    while off + 4 <= n:
        (length,) = struct.unpack_from(">I", buf, off)
        off += 4
        if length < 9 or off + length > n:
            break
        secs, usecs = struct.unpack_from(">II", buf, off)
        typ = buf[off + 8]
        payload = buf[off + 9: off + length]
        off += length
        yield secs + usecs / 1e6, typ, payload


def main():
    with open(PATH, "rb") as f:
        buf = f.read()

    t0 = None
    # reassembly state per (acl_handle, direction)
    asm = {}
    n_att = 0

    for ts, typ, payload in records(buf):
        if typ not in (0x02, 0x03):  # only ACL sent/received
            continue
        direction = "TX->dev" if typ == 0x02 else "RX<-dev"
        if len(payload) < 4:
            continue
        h, plen = struct.unpack_from("<HH", payload, 0)
        acl_handle = h & 0x0FFF
        pb = (h >> 12) & 0x3
        acl_data = payload[4:4 + plen]
        key = (acl_handle, direction)

        if pb == 0x1:  # continuation fragment
            if key in asm:
                asm[key]["buf"] += acl_data
        else:  # start fragment
            if len(acl_data) < 4:
                continue
            l2_len, cid = struct.unpack_from("<HH", acl_data, 0)
            asm[key] = {"buf": acl_data[4:], "need": l2_len, "cid": cid}

        st = asm.get(key)
        if not st or len(st["buf"]) < st["need"]:
            continue  # wait for more fragments
        l2 = st["buf"][: st["need"]]
        cid = st["cid"]
        del asm[key]

        if cid != 0x0004:  # ATT channel only
            continue
        if not l2:
            continue
        op = l2[0]
        opname = ATT_OPS.get(op, f"OP_{op:#04x}")

        if op in WRITE_OR_NOTIFY and len(l2) >= 3:
            handle = struct.unpack_from("<H", l2, 1)[0]
            value = l2[3:]
            is_vendor = handle in VENDOR_HANDLES
            if not (SHOW_ALL or is_vendor):
                continue
            if t0 is None:
                t0 = ts
            tag = HANDLE_NAME.get(handle, "")
            mark = "  <===" if is_vendor else ""
            n_att += 1
            print(f"[{ts - t0:8.3f}] {direction}  {opname:9s} h=0x{handle:04x} "
                  f"{tag:26s} len={len(value):3d}  {value.hex()}{mark}")
        elif SHOW_ALL:
            if t0 is None:
                t0 = ts
            print(f"[{ts - t0:8.3f}] {direction}  {opname:9s}  {l2.hex()}")

    print(f"\n{n_att} relevant ATT ops.", file=sys.stderr)
    if n_att == 0:
        print("No vendor-handle traffic found. Re-run with --all to see everything,",
              file=sys.stderr)
        print("or confirm the capture covered an active Connect-app session.", file=sys.stderr)


if __name__ == "__main__":
    main()
