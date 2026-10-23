// Shared `.poses` reader for offline Node tools (verify_poses.mjs, snap.mjs).
//
// This is the single Node-side home for the .poses binary format. The logic is
// identical to the browser viewer (Src/src/js/viewer360.js::parsePoseBuffer /
// ::getPose) and the format is specified in POSES_FORMAT.md. Column order is
// (x, y, z, w) in the WebGL convention, so getPose() output feeds straight into
// gl-matrix `quat` with no permutation.

import fs from 'node:fs';
import zlib from 'node:zlib';

/**
 * Transparently decode an on-disk .poses buffer. v2 files ship brotli-
 * compressed (Content-Encoding: br on S3); the local copies may be brotli or
 * raw. v1 files are historically uncompressed. Auto-detects by magic bytes.
 */
export function decompressIfNeeded(buf) {
    if (buf.length >= 2 && buf[0] === 0x1f && buf[1] === 0x8b) {
        return zlib.gunzipSync(buf);
    }
    if (buf.length >= 4 && buf[0] === 0x50 && buf[1] === 0x53 &&
        buf[2] === 0x30 && buf[3] === 0x32) {
        return buf;  // uncompressed v2 ('PS02')
    }
    if (buf.length >= 8 &&
        buf[4] === 0 && buf[5] === 0 && buf[6] === 0 && buf[7] === 0) {
        return buf;  // plausible legacy v1 uncompressed (rows fits in uint32)
    }
    return zlib.brotliDecompressSync(buf);
}

/** Same logic as Src/src/js/{viewer360,sailview}.js::parsePoseBuffer. */
export function parsePoseBuffer(bufNode) {
    const ab = bufNode.buffer.slice(bufNode.byteOffset, bufNode.byteOffset + bufNode.byteLength);
    const dv = new DataView(ab);
    const headerBytes = 32;
    let rows, cols, msecs, multiplier, isDelta, version;
    if (dv.getUint8(0) === 0x50 && dv.getUint8(1) === 0x53 &&
        dv.getUint8(2) === 0x30 && dv.getUint8(3) === 0x32) {
        const flags = dv.getUint32(4, true);
        rows = dv.getUint32(8, true);
        cols = dv.getUint32(12, true);
        msecs = dv.getUint32(16, true);
        multiplier = dv.getUint32(20, true);
        isDelta = (flags & 1) !== 0;
        version = 2;
    } else {
        const legacy = new BigUint64Array(ab.slice(0, headerBytes));
        rows = Number(legacy[0]);
        cols = Number(legacy[1]);
        msecs = Number(legacy[2]);
        multiplier = Number(legacy[3]);
        isDelta = false;
        version = 1;
    }
    const data = new Int16Array(ab.slice(headerBytes));
    if (isDelta) {
        for (let i = cols; i < data.length; i++) {
            data[i] = data[i] + data[i - cols];
        }
    }
    return {version, rows, cols, msecs, multiplier, data};
}

/**
 * Nearest-neighbor quaternion lookup at a video time, in ms. Same rounding the
 * viewer uses (Math.round(timeMS / msecs)). Returns [x, y, z, w].
 */
export function getPose(pose, timeMS) {
    const {msecs, multiplier: m, cols, data, rows} = pose;
    const row = Math.max(0, Math.min(rows - 1, Math.round(timeMS / msecs)));
    const off = row * cols;
    return [data[off] / m, data[off + 1] / m, data[off + 2] / m, data[off + 3] / m];
}

/** Read + decode + parse a .poses file from disk in one call. */
export function loadPoses(path) {
    return parsePoseBuffer(decompressIfNeeded(fs.readFileSync(path)));
}
