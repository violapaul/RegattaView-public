// snap.mjs — capture a corrected (dewarped + horizon-leveled) still from a
// RegattaView 360 video.
//
// Raw fisheye crops are unreadable: the lens is barrel-distorted and the camera
// is mounted tilted on a heeling boat. This reproduces exactly what the viewer
// shows — it runs the *production* fisheye GLSL (Src/src/js/fisheye_shader.js)
// over the frame and levels the horizon with the per-frame .poses IMU
// quaternion (the same inv(pose)·viewpoint compose as viewer360.js:361-371).
//
// Pipeline: ffmpeg decodes one HEVC frame → PNG → loaded as a texture in a
// headless-Chromium WebGL context (system Chrome via playwright-core; headless
// native WebGL — `gl` — won't build under a path with spaces) → dewarp+level →
// canvas.toDataURL → optional caption/encode via ffmpeg.
//
// Usage:
//   node scripts/snap.mjs --video <path.mp4> --time <clipSeconds> --out <img>
//   node scripts/snap.mjs --video <path.mp4> --unix <unixSeconds> --out <img>
//
// Flags:
//   --video <path>     (required) source .mp4
//   --time <sec>       clip-relative time, OR
//   --unix <sec>       wall-clock unix seconds (resolved via sibling/--slog .slog)
//   --out <path>       output image (default: ./snap.jpg); ext picks format
//   --yaw <deg>        pan right (default 0 = forward over the bow)
//   --pitch <deg>      tilt up (default 0 = level)
//   --fov <deg>        field of view (default 130; smaller = zoomed in / tighter,
//                      larger = wider / more context). Useful range ~70-150:
//                      ~90 = natural "photo" crop, ~130 = immersive cockpit-wide
//                      (whole crew + nearby boats), >=150 starts to show fisheye
//                      edge stretch + horizon bow. Pick per shot.
//   --width <px>       output width  (default 1280)
//   --height <px>      output height (default 720)
//   --projection <p>   'fisheye' (default) or 'equirect' (VIA_*/VIRB clips)
//   --poses <path>     override .poses (default: sibling of --video)
//   --slog <path>      override .slog used by --unix (default: sibling)
//   --label "<text>"   burn a caption top-left
//   --no-stabilize     skip horizon leveling (debug: shows the raw camera tilt)
//   --chrome <path>    Chrome/Chromium executable (default: system Google Chrome)

import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';
import os from 'node:os';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {quat, mat4} from 'gl-matrix';
import {chromium} from 'playwright-core';
import {loadPoses, getPose} from './lib/poses.mjs';
import {
    FISHEYE_FRAGMENT_SHADER, FISHEYE_VERTEX_SHADER,
} from '../src/js/fisheye_shader.js';
import {
    EQUIRECT_FRAGMENT_SHADER, EQUIRECT_VERTEX_SHADER,
} from '../src/js/equirect_shader.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CALIB = path.join(HERE, '..', 'src', 'assets', 'calibration', 'default.json');
const DEFAULT_CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';

// ---- args ----------------------------------------------------------------
function parseArgs(argv) {
    const a = {
        out: 'snap.jpg', yaw: 0, pitch: 0, fov: 130, width: 1280, height: 720,
        projection: 'fisheye', stabilize: true, chrome: DEFAULT_CHROME,
    };
    for (let i = 2; i < argv.length; i++) {
        const k = argv[i];
        const next = () => argv[++i];
        switch (k) {
            case '--video': a.video = next(); break;
            case '--time': a.time = parseFloat(next()); break;
            case '--unix': a.unix = parseFloat(next()); break;
            case '--out': a.out = next(); break;
            case '--yaw': a.yaw = parseFloat(next()); break;
            case '--pitch': a.pitch = parseFloat(next()); break;
            case '--fov': a.fov = parseFloat(next()); break;
            case '--width': a.width = parseInt(next(), 10); break;
            case '--height': a.height = parseInt(next(), 10); break;
            case '--projection': a.projection = next(); break;
            case '--poses': a.poses = next(); break;
            case '--slog': a.slog = next(); break;
            case '--label': a.label = next(); break;
            case '--no-stabilize': a.stabilize = false; break;
            case '--chrome': a.chrome = next(); break;
            default: throw new Error(`unknown flag: ${k}`);
        }
    }
    if (!a.video) throw new Error('--video is required');
    if (a.time == null && a.unix == null) throw new Error('one of --time or --unix is required');
    return a;
}

// ---- .slog header → start_unix_ms (for --unix resolution) ----------------
// Reads only the SLOG_FORMAT.md fixed header (offset 16, float64 start_unix_ms).
// Mapping is uniform clip time = unix - start (matches build_figs / the
// viewer's row_times = start + i*msec_per_sample).
function slogStartUnixMs(slogPath) {
    let buf = fs.readFileSync(slogPath);
    if (buf.length >= 2 && buf[0] === 0x1f && buf[1] === 0x8b) buf = zlib.gunzipSync(buf);
    const dv = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
    const magic = dv.getUint32(0, true);
    if (magic !== 0x474f4c53) throw new Error(`${slogPath}: bad SLOG magic 0x${magic.toString(16)}`);
    return dv.getFloat64(16, true);
}

// ---- in-page render (raw WebGL; reuses the production GLSL verbatim) -------
// Loads the PNG frame as a DOM <img> texture — identical upload path to the
// viewer's <video> texture (default UNPACK orientation), so output matches prod.
function renderInPage({fragSrc, vertSrc, uniforms, rotation, fov, width, height, imgURL}) {
    return new Promise((resolve, reject) => {
        const img = new Image();
        img.onload = () => {
            try {
                const canvas = document.createElement('canvas');
                canvas.width = width; canvas.height = height;
                const gl = canvas.getContext('webgl', {preserveDrawingBuffer: true});
                if (!gl) throw new Error('no webgl');
                const compile = (type, src) => {
                    const s = gl.createShader(type);
                    gl.shaderSource(s, src); gl.compileShader(s);
                    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
                        throw new Error('shader: ' + gl.getShaderInfoLog(s));
                    }
                    return s;
                };
                const prog = gl.createProgram();
                gl.attachShader(prog, compile(gl.VERTEX_SHADER, vertSrc));
                gl.attachShader(prog, compile(gl.FRAGMENT_SHADER, fragSrc));
                gl.linkProgram(prog);
                if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
                    throw new Error('link: ' + gl.getProgramInfoLog(prog));
                }
                gl.useProgram(prog);

                const quad = gl.createBuffer();
                gl.bindBuffer(gl.ARRAY_BUFFER, quad);
                gl.bufferData(gl.ARRAY_BUFFER,
                    new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
                const loc = gl.getAttribLocation(prog, 'a_position');
                gl.enableVertexAttribArray(loc);
                gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);

                const tex = gl.createTexture();
                gl.bindTexture(gl.TEXTURE_2D, tex);
                gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
                gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
                gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
                gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
                gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, img);

                const u1 = (n, v) => { const l = gl.getUniformLocation(prog, n); if (l) gl.uniform1f(l, v); };
                const uv = (n, v) => {
                    const l = gl.getUniformLocation(prog, n);
                    if (!l) return;
                    if (v.length === 2) gl.uniform2fv(l, v);
                    else gl.uniform3fv(l, v);
                };
                const ut = gl.getUniformLocation(prog, 'u_texture');
                if (ut) gl.uniform1i(ut, 0);
                const ur = gl.getUniformLocation(prog, 'u_rotation');
                if (ur) gl.uniformMatrix4fv(ur, false, rotation);
                u1('u_fov', fov);
                uv('u_resolution', [width, height]);
                u1('u_showSeamDebug', 0);
                for (const [k, v] of Object.entries(uniforms)) {
                    if (Array.isArray(v)) uv(k, v); else u1(k, v);
                }

                gl.viewport(0, 0, width, height);
                gl.clearColor(0, 0, 0, 1);
                gl.clear(gl.COLOR_BUFFER_BIT);
                gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
                resolve(canvas.toDataURL('image/png'));
            } catch (e) {
                reject(e);
            }
        };
        img.onerror = () => reject(new Error('image decode failed in browser'));
        img.src = imgURL;
    });
}

// Calibration JSON → fragment-shader uniforms. Mirrors the private
// fisheye_renderer.js::toUniforms (Src/src/js/fisheye_renderer.js:57-73).
function toUniforms(c) {
    return {
        u_frontCenter: c.front.center, u_backCenter: c.back.center,
        u_frontScale: c.front.scale, u_backScale: c.back.scale,
        u_frontEuler: c.front.euler, u_backEuler: c.back.euler,
        u_frontDistortion: c.front.distortion, u_backDistortion: c.back.distortion,
        u_aperture: c.projection.aperture, u_viewCorrection: c.projection.viewCorrection,
        u_seamCenterYaw: c.seam.centerYaw, u_seamWidth: c.seam.width,
        u_seamFeather: c.seam.feather,
    };
}

function run(cmd, cmdArgs) {
    const r = spawnSync(cmd, cmdArgs, {stdio: ['ignore', 'pipe', 'pipe']});
    if (r.status !== 0) {
        throw new Error(`${cmd} failed (${r.status}): ${r.stderr ? r.stderr.toString() : ''}`);
    }
    return r;
}

async function main() {
    const a = parseArgs(process.argv);

    // Resolve clip time.
    let clipTime = a.time;
    if (clipTime == null) {
        const slog = a.slog || a.video.replace(/\.mp4$/i, '.slog');
        if (!fs.existsSync(slog)) throw new Error(`--unix needs a .slog; not found: ${slog}`);
        clipTime = a.unix - slogStartUnixMs(slog) / 1000;
    }
    if (clipTime < 0) throw new Error(`resolved clip time is negative (${clipTime.toFixed(2)}s)`);

    // Decode one frame to PNG.
    const tmpFrame = path.join(os.tmpdir(), `snap_frame_${process.pid}.png`);
    run('ffmpeg', ['-y', '-loglevel', 'error', '-ss', String(clipTime),
        '-i', a.video, '-frames:v', '1', '-f', 'image2', '-c:v', 'png', tmpFrame]);
    const imgURL = 'data:image/png;base64,' + fs.readFileSync(tmpFrame).toString('base64');

    // Pose-leveled rotation = inv(pose) · viewpoint  (viewer360.js:364-371).
    // viewpoint = rotateY(yaw)·rotateX(pitch) (pointer_controls.js:59-62).
    const posePath = a.poses || a.video.replace(/\.mp4$/i, '.poses');
    const pose = fs.existsSync(posePath) ? loadPoses(posePath) : null;
    if (!pose && a.stabilize) {
        console.warn(`[snap] no .poses at ${posePath}; horizon will NOT be leveled`);
    }
    const vp = quat.identity(quat.create());
    quat.rotateY(vp, vp, a.yaw * Math.PI / 180);
    quat.rotateX(vp, vp, a.pitch * Math.PI / 180);
    const qcam = quat.create();
    if (a.stabilize && pose) {
        quat.invert(qcam, quat.fromValues(...getPose(pose, clipTime * 1000)));
    } else {
        quat.identity(qcam);
    }
    const q = quat.multiply(quat.create(), qcam, vp);
    const rotation = Array.from(mat4.fromQuat(mat4.create(), q));

    // Choose shader + uniforms by projection.
    const isEqui = a.projection === 'equirect';
    const calib = JSON.parse(fs.readFileSync(CALIB, 'utf8')).calibration;
    const uniforms = isEqui ? {} : toUniforms(calib);
    const fragSrc = isEqui ? EQUIRECT_FRAGMENT_SHADER : FISHEYE_FRAGMENT_SHADER;
    const vertSrc = isEqui ? EQUIRECT_VERTEX_SHADER : FISHEYE_VERTEX_SHADER;

    // Render in headless Chrome.
    const browser = await chromium.launch({
        executablePath: a.chrome, headless: true,
        args: ['--use-gl=angle', '--use-angle=metal', '--ignore-gpu-blocklist',
            '--enable-webgl', '--enable-accelerated-2d-canvas'],
    });
    let dataURL;
    try {
        const page = await browser.newPage();
        page.on('console', (m) => { if (m.type() === 'error') console.error('[page]', m.text()); });
        dataURL = await page.evaluate(renderInPage, {
            fragSrc, vertSrc, uniforms, rotation, fov: a.fov,
            width: a.width, height: a.height, imgURL,
        });
    } finally {
        await browser.close();
    }

    // Write output. Encode/caption via ffmpeg unless a bare .png with no label.
    const pngBuf = Buffer.from(dataURL.split(',')[1], 'base64');
    const wantPng = /\.png$/i.test(a.out);
    if (wantPng && !a.label) {
        fs.writeFileSync(a.out, pngBuf);
    } else {
        const tmpOut = path.join(os.tmpdir(), `snap_out_${process.pid}.png`);
        fs.writeFileSync(tmpOut, pngBuf);
        const vf = a.label
            ? [`drawtext=text='${String(a.label).replace(/'/g, "\\'").replace(/:/g, '\\:')}':` +
               `x=24:y=24:fontsize=${Math.round(a.width / 38)}:fontcolor=white:` +
               `box=1:boxcolor=black@0.55:boxborderw=10`]
            : [];
        run('ffmpeg', ['-y', '-loglevel', 'error', '-i', tmpOut,
            ...(vf.length ? ['-vf', vf[0]] : []), '-q:v', '3', a.out]);
        fs.unlinkSync(tmpOut);
    }
    fs.unlinkSync(tmpFrame);

    console.log(`[snap] ${a.out}  (clip t=${clipTime.toFixed(2)}s, ` +
        `yaw=${a.yaw} pitch=${a.pitch} fov=${a.fov}, ` +
        `${a.stabilize && pose ? 'leveled' : 'NOT leveled'}, ${a.projection})`);
}

main().catch((e) => { console.error('[snap] error:', e.message); process.exit(1); });
