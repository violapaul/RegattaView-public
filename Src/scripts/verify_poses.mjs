// Offline end-to-end test for the .poses reader.
//
// Parses two .poses files (legacy v1 and new v2 delta+brotli), runs the same
// parse + nearest-neighbor getPose logic the browser viewer uses, and compares
// the resulting quaternion streams. Does not render; just prints stats.
//
// Usage:
//   node Src/scripts/verify_poses.mjs <legacy_v1.poses> <new_v2.poses>
//
// The v1 file is assumed to be uncompressed on disk (matches historical S3
// layout). The v2 file is auto-detected: brotli-compressed on disk if the
// first bytes are not our 'PS02' magic and not gzip magic.

import fs from 'node:fs';
import {decompressIfNeeded, parsePoseBuffer, getPose} from './lib/poses.mjs';

function quatDot(a, b) { return a[0]*b[0] + a[1]*b[1] + a[2]*b[2] + a[3]*b[3]; }
function quatNorm(a) { return Math.sqrt(quatDot(a, a)); }

function main() {
    const [,, oldPath, newPath] = process.argv;
    if (!oldPath || !newPath) {
        console.error('Usage: node verify_poses.mjs <legacy_v1.poses> <new_v2.poses>');
        process.exit(2);
    }

    const oldRaw = fs.readFileSync(oldPath);
    const newRaw = fs.readFileSync(newPath);
    const oldDecoded = decompressIfNeeded(oldRaw);
    const newDecoded = decompressIfNeeded(newRaw);

    const oldPose = parsePoseBuffer(oldDecoded);
    const newPose = parsePoseBuffer(newDecoded);

    const fmt = (p, path, onDisk, afterDecode) =>
        `${path}\n` +
        `    on-disk bytes: ${onDisk.toLocaleString().padStart(10)}\n` +
        `    decoded bytes: ${afterDecode.toLocaleString().padStart(10)}\n` +
        `    header: version=${p.version} rows=${p.rows} cols=${p.cols} ` +
        `msec_per_sample=${p.msecs} multiplier=${p.multiplier}\n` +
        `    duration: ${(p.rows * p.msecs / 1000).toFixed(1)}s`;

    console.log('=== File summary ===');
    console.log(fmt(oldPose, oldPath, oldRaw.length, oldDecoded.length));
    console.log(fmt(newPose, newPath, newRaw.length, newDecoded.length));

    // Sanity: both should cover roughly the same duration.
    const oldDurMs = oldPose.rows * oldPose.msecs;
    const newDurMs = newPose.rows * newPose.msecs;
    const durMs = Math.min(oldDurMs, newDurMs);

    // Unit-norm check on decoded quaternions.
    const checkNorm = (p, label) => {
        let maxErr = 0;
        for (let r = 0; r < p.rows; r++) {
            const off = r * p.cols;
            const q = [p.data[off] / p.multiplier, p.data[off+1] / p.multiplier,
                       p.data[off+2] / p.multiplier, p.data[off+3] / p.multiplier];
            const err = Math.abs(quatNorm(q) - 1);
            if (err > maxErr) maxErr = err;
        }
        console.log(`  ${label}: max |q|-1 over all rows = ${maxErr.toExponential(3)}`);
    };
    console.log('\n=== Unit-norm check ===');
    checkNorm(oldPose, 'legacy');
    checkNorm(newPose, 'new   ');

    // Compare quaternions sampled at a dense grid of video times.
    console.log('\n=== Quaternion comparison (nearest-neighbor at frame times) ===');
    const stepMs = 1000 / 30;  // 30 fps
    const N = Math.floor(durMs / stepMs);
    let maxAngDeg = 0;
    let sumAngDeg = 0;
    let worstT = 0, worstQOld = null, worstQNew = null;
    for (let k = 0; k < N; k++) {
        const t = k * stepMs;
        const qOld = getPose(oldPose, t);
        const qNew = getPose(newPose, t);
        // Quaternion angular distance: 2 * acos(|dot|). q and -q are equivalent.
        const d = Math.abs(quatDot(qOld, qNew));
        const clamped = Math.min(1, d);
        const angRad = 2 * Math.acos(clamped);
        const angDeg = angRad * 180 / Math.PI;
        sumAngDeg += angDeg;
        if (angDeg > maxAngDeg) {
            maxAngDeg = angDeg;
            worstT = t;
            worstQOld = qOld;
            worstQNew = qNew;
        }
    }
    console.log(`  frames compared: ${N}`);
    console.log(`  mean angular diff: ${(sumAngDeg / N).toFixed(4)}°`);
    console.log(`  max  angular diff: ${maxAngDeg.toFixed(4)}° at t=${(worstT / 1000).toFixed(2)}s`);
    console.log(`    legacy qw/qx/qy/qz = ${worstQOld.map(x => x.toFixed(5)).join(', ')}`);
    console.log(`    new    qw/qx/qy/qz = ${worstQNew.map(x => x.toFixed(5)).join(', ')}`);

    // A few explicit sample points for human inspection.
    console.log('\n=== Sample points ===');
    for (const t of [0, 1000, 10_000, 60_000, Math.floor(durMs / 2), durMs - 100]) {
        if (t < 0 || t >= durMs) continue;
        const qOld = getPose(oldPose, t);
        const qNew = getPose(newPose, t);
        console.log(`  t=${(t/1000).toFixed(2)}s`);
        console.log(`    legacy = [${qOld.map(x => x.toFixed(5)).join(', ')}]`);
        console.log(`    new    = [${qNew.map(x => x.toFixed(5)).join(', ')}]`);
    }

    // Pass/fail heuristic: at 30 Hz decimation with int16 quantization, the
    // irreducible quaternion error between stored 100 Hz and stored 30 Hz is
    // dominated by boat rotation rate × (33 ms / 2) + ~1/32767 quantization.
    // A few degrees at worst frame is expected (boat turning). Flag > 5°.
    const tolDeg = 5.0;
    console.log('');
    if (maxAngDeg > tolDeg) {
        console.log(`FAIL: max angular diff ${maxAngDeg.toFixed(2)}° exceeds tolerance ${tolDeg}°`);
        process.exit(1);
    } else {
        console.log(`PASS: max angular diff ${maxAngDeg.toFixed(2)}° within tolerance ${tolDeg}°`);
    }
}

main();
