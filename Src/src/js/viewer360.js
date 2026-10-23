/**
 * viewer360.js
 *
 * Lightweight, reusable 360-degree video viewer. Extracted from sailview.js
 * so other apps (e.g. the race-start review tool) can embed 360 playback
 * without pulling in the full sailview UI.
 *
 * Responsibilities:
 *   - Create a <canvas>, WebGL fisheye renderer, and <video> in a container.
 *   - Load optional .poses + .log binary sidecars for stabilization and
 *     Unix-time <-> video-time mapping.
 *   - Handle mouse drag (pan), wheel (zoom), and R/F/S keyboard shortcuts.
 *   - Let the parent app drive play/pause/seek (no controls chrome).
 *
 * Parent is responsible for: picking which video to load, fetching signed
 * URLs, and invoking loadVideo()/seekToUnix()/play()/pause() as needed.
 *
 * Exposed as a UMD bundle via webpack (window.Viewer360.create).
 */

import * as twgl from 'twgl.js';
import {quat, mat4} from 'gl-matrix';
import {createDefaultCalibration, createFisheyeRenderer} from './fisheye_renderer';
import * as utils from './utils';
import * as shared from './shared';
import {attachPointerControls} from './pointer_controls';
import {loadLog, usableRowPrefix} from './log_load';

/**
 * @param {HTMLElement} container element to mount the viewer inside
 * @param {Object} [options]
 * @param {string} [options.calibrationUrl] URL to calibration JSON (defaults
 *     to the preset bundled with sailview)
 * @param {string} [options.sensorCalUrl] URL to sensor calibration JSON
 * @param {boolean} [options.keyboard=true] attach R/F/S keyboard shortcuts
 *     to window
 * @param {boolean} [options.stabilize=true] start with pose stabilization on
 * @param {boolean} [options.skipFrames=true] render at ~30FPS instead of 60
 * @param {number} [options.fov=180] starting field of view
 * @param {function} [options.onFrame] callback fired once per drawn frame
 *     (after renderer.draw, only when the video has decoded at least one
 *     frame). Receives `{videoTimeMs, viewer}`. Use this to update overlays
 *     and instrument panels in lockstep with the WebGL frame.
 * @param {boolean} [options.seedFirstFrame=false] after loadVideo, briefly
 *     play() and pause() so one decoded frame lands in the texture before
 *     the user sees a blank canvas. Matches sailview's longstanding "play,
 *     wait for playing+timeupdate, seek, pause" dance. Ignored when
 *     `loadVideo({autoplay: true})` is used.
 * @returns {Viewer} handle with load/seek/play/pause/etc
 */
export function create(container, options = {}) {
    const opts = Object.assign({
        calibrationUrl: './assets/calibration/default.json',
        sensorCalUrl: './assets/calibration/sensor_calibration.json',
        keyboard: true,
        stabilize: true,
        skipFrames: true,
        fov: 180,
        onFrame: null,
        seedFirstFrame: false,
    }, options);

    // --- DOM setup ----------------------------------------------------------
    const canvas = document.createElement('canvas');
    canvas.style.display = 'block';
    canvas.style.width = '100%';
    canvas.style.height = '100%';
    canvas.tabIndex = 0; // focusable so it can receive key events if needed
    container.appendChild(canvas);

    const video = document.createElement('video');
    video.autoplay = false;
    video.muted = true;
    video.loop = false;
    video.crossOrigin = '';
    video.style.display = 'none';
    // iOS Safari defaults to native-fullscreen-on-play for <video>, which
    // hijacks the WebGL canvas. playsInline keeps the video inline as a
    // texture source for the fisheye shader.
    video.playsInline = true;
    video.setAttribute('playsinline', '');
    video.setAttribute('webkit-playsinline', '');
    container.appendChild(video);

    const gl = canvas.getContext('webgl');
    if (!gl) throw new Error('WebGL not supported');

    const renderer = createFisheyeRenderer(gl, {
        fov: opts.fov,
        calibration: createDefaultCalibration(),
    });
    const uniforms = renderer.uniforms;

    // --- State --------------------------------------------------------------
    const state = {
        video,
        canvas,
        renderer,
        pose_data: null,
        log_data: null,
        stabilize: opts.stabilize,
        skipRender: opts.skipFrames,
        ready: false,
        initialTime: 0,
        lastCenterLog: 0,
        mouse: null,
        columnsMap: null, // { rowIndex -> columnName }
        sensorCalibration: {
            spd: {scale: 1.0, offset: 0.0},
            heading: {offset: 0.0},
            heel: {offset: 0.0},
            awa: {offset: 0.0},
            depth: {offset: 0.0},
        },
    };

    // --- Calibration --------------------------------------------------------
    // Both presets are required config: a missing/malformed file means the
    // viewer is rendering with wrong fisheye math or wrong instrument scaling.
    // Surface the error rather than silently defaulting.
    function loadCalibrationPreset() {
        return utils.fetchJSON(opts.calibrationUrl).then((data) => {
            if (!data || !data.calibration) {
                throw new Error(`fisheye preset ${opts.calibrationUrl}: missing 'calibration' field`);
            }
            if (data.schemaVersion !== 1) {
                throw new Error(`fisheye preset ${opts.calibrationUrl}: unsupported schemaVersion ${data.schemaVersion}`);
            }
            renderer.setCalibration(data.calibration);
            if (data.calibration.fov) renderer.setFov(data.calibration.fov);
        }).catch((err) => {
            utils.surfaceError(`Fisheye preset failed to load (${err.message}). Lens correction may be wrong.`,
                {key: 'fisheye-preset'});
        });
    }

    function loadSensorCalibration() {
        // Returned promise also gates fetchLog so applySensorCalibration sees
        // calibrated values rather than defaults of {scale:1, offset:0}.
        return utils.fetchJSON(opts.sensorCalUrl).then((data) => {
            if (data.spd) state.sensorCalibration.spd = data.spd;
            if (data.heading) state.sensorCalibration.heading = data.heading;
            if (data.heel) state.sensorCalibration.heel = data.heel;
            if (data.awa) state.sensorCalibration.awa = data.awa;
            if (data.depth) state.sensorCalibration.depth = data.depth;
        }).catch((err) => {
            utils.surfaceError(`Sensor calibration failed to load (${err.message}). SPD/heel/AWA may be uncalibrated.`,
                {key: 'sensor-cal'});
        });
    }

    function applySensorCalibration(data) {
        const cal = state.sensorCalibration;
        if (data['spd'] && (cal.spd.scale !== 1.0 || cal.spd.offset !== 0.0)) {
            const raw = data['spd'];
            const out = new Float64Array(raw.length);
            for (let i = 0; i < raw.length; i++) out[i] = raw[i] * cal.spd.scale + cal.spd.offset;
            data['spd'] = out;
        }
        if (data['zg100_roll']) {
            const raw = data['zg100_roll'];
            const out = new Float64Array(raw.length);
            const offset = cal.heel.offset;
            for (let i = 0; i < raw.length; i++) out[i] = raw[i] - offset;
            data['heel'] = out;
        }
        if (data['awa'] && cal.awa.offset !== 0.0) {
            const raw = data['awa'];
            const out = new Float64Array(raw.length);
            for (let i = 0; i < raw.length; i++) out[i] = raw[i] + cal.awa.offset;
            data['awa'] = out;
        }
        if (cal.heading && cal.heading.offset !== 0.0) {
            // Compass offset: the RC42 heading reads ~-2.3° (Analyze/HEADING_CALIBRATION.md).
            // HDG and TWD are both compass/earth-referenced, so both shift by the
            // offset; TWA is boat-relative (masthead-derived, compass-independent)
            // and stays unchanged. parseSlog set twa = twd-(hdg+var), so shifting
            // hdg and twd equally preserves that identity. turn_rate is a
            // difference, hence offset-invariant — left as parsed.
            const hoff = cal.heading.offset;
            for (const key of ['hdg', 'twd']) {
                const raw = data[key];
                if (!raw) continue;
                const out = new Float64Array(raw.length);
                for (let i = 0; i < raw.length; i++) out[i] = ((raw[i] + hoff) % 360 + 360) % 360;
                data[key] = out;
            }
        }
        if (data['depth'] && cal.depth.offset !== 0.0) {
            const raw = data['depth'];
            const out = new Float64Array(raw.length);
            for (let i = 0; i < raw.length; i++) out[i] = raw[i] + cal.depth.offset;
            data['depth'] = out;
        }
    }

    // --- Pose/log loading ---------------------------------------------------
    // Parse a .poses ArrayBuffer into {rows, cols, msecs, multiplier, data}.
    // Supports format v2 (magic 'PS02', uint32 header, delta-encoded int16) and
    // legacy v1 (four uint64 LE, raw int16).
    function parsePoseBuffer(buf) {
        const dv = new DataView(buf);
        const headerBytes = 32;
        let rows, cols, msecs, multiplier, isDelta;
        if (dv.getUint8(0) === 0x50 && dv.getUint8(1) === 0x53 &&
            dv.getUint8(2) === 0x30 && dv.getUint8(3) === 0x32) {
            const flags = dv.getUint32(4, true);
            rows = dv.getUint32(8, true);
            cols = dv.getUint32(12, true);
            msecs = dv.getUint32(16, true);
            multiplier = dv.getUint32(20, true);
            isDelta = (flags & 1) !== 0;
        } else {
            const legacy = new BigUint64Array(buf.slice(0, headerBytes));
            rows = Number(legacy[0]);
            cols = Number(legacy[1]);
            msecs = Number(legacy[2]);
            multiplier = Number(legacy[3]);
            isDelta = false;
        }
        const data = new Int16Array(buf.slice(headerBytes));
        const expected = rows * cols;
        if (data.length !== expected) {
            console.error(`[viewer360] .poses payload size mismatch: got ${data.length} int16s, expected ${expected} (rows=${rows} cols=${cols}) — file may be truncated.`);
        }
        if (isDelta) {
            // Cumsum per column; Int16Array store wraps mod 2^16 which inverts
            // the int16 modular arithmetic used by the writer.
            for (let i = cols; i < data.length; i++) {
                data[i] = data[i] + data[i - cols];
            }
        }
        return {rows, cols, msecs, multiplier, data};
    }

    function readPoses(url) {
        const req = new XMLHttpRequest();
        req.open('GET', url, true);
        req.responseType = 'arraybuffer';
        req.onload = () => {
            const buf = req.response;
            if (!buf) return;
            state.pose_data = parsePoseBuffer(buf);
        };
        req.onerror = () => console.error('Failed to load pose file');
        req.send(null);
    }

    // Fetch + parse a log via log_load.js (which dispatches between v2 .slog
    // and v1 .log with automatic fallback). Sensor calibration and the
    // usableRowPrefix truncation guard live here because they're viewer-
    // level concerns; log_load owns format dispatch and parsing only.
    function fetchLog(logPath, signLog, columnsMap, basename = null) {
        state.columnsMap = columnsMap;
        Promise.all([
            loadLog(logPath, signLog, columnsMap),
            state.sensorCalReady,
        ]).then(([parsed]) => {
            const {rows, cols, msecs, data} = parsed;
            applySensorCalibration(data);
            const usableRows = usableRowPrefix(data.row_times, Number(rows));
            if (usableRows < Number(rows)) {
                console.warn(`[viewer360] log row_times non-monotonic after row ${usableRows - 1}; truncating (had ${rows}, using ${usableRows})`);
            }
            state.log_data = {
                rows: usableRows, cols: Number(cols),
                data, msecs: Number(msecs), basename,
            };
        }).catch((err) => {
            console.error(`[viewer360] failed to load log ${logPath}: ${err.message}`);
        });
    }

    function getPose(timeMS) {
        if (!state.pose_data) return null;
        const {msecs, multiplier: m, cols, data} = state.pose_data;
        const row = Math.round(timeMS / msecs);
        const off = row * cols;
        return quat.fromValues(data[off] / m, data[off + 1] / m, data[off + 2] / m, data[off + 3] / m);
    }

    function getLog(timeMS, columnName) {
        if (!state.log_data) return undefined;
        const col = state.log_data.data[columnName];
        if (!col) return undefined;
        const lastRow = Math.max(0, state.log_data.rows - 1);
        const row = Math.max(0, Math.min(lastRow, Math.round(timeMS / state.log_data.msecs)));
        return col[row];
    }

    function getLogColumn(columnName) {
        return state.log_data ? state.log_data.data[columnName] : undefined;
    }

    function getLogHistory(timeMS, columnName, windowMs) {
        if (!state.log_data) return undefined;
        const col = state.log_data.data[columnName];
        if (!col) return undefined;
        const msPerRow = state.log_data.msecs;
        const rows = state.log_data.rows;
        const rowEnd = Math.max(0, Math.min(rows, Math.round(timeMS / msPerRow)));
        const rowStart = Math.max(0, Math.min(rowEnd, Math.round(Math.max(0, timeMS - windowMs) / msPerRow)));
        return col.slice(rowStart, rowEnd);
    }

    function getUnixAtVideoMs(timeMS) {
        const rowTimes = state.log_data && state.log_data.data.row_times;
        if (!rowTimes || !rowTimes.length) return undefined;
        const msecs = state.log_data.msecs;
        const rowf = timeMS / msecs;
        const lastIdx = state.log_data.rows - 1;
        if (rowf <= 0) {
            return rowTimes[0] + rowf * msecs;
        }
        if (rowf >= lastIdx) {
            return rowTimes[lastIdx] + (rowf - lastIdx) * msecs;
        }
        const lo = Math.floor(rowf);
        const hi = Math.ceil(rowf);
        if (lo === hi) return rowTimes[lo];
        const frac = rowf - lo;
        return rowTimes[lo] + frac * (rowTimes[hi] - rowTimes[lo]);
    }

    // --- Mouse (pan + zoom) -------------------------------------------------
    function setupMouse() {
        state.mouse = attachPointerControls(canvas, renderer, {defaultFov: opts.fov});
    }

    // --- Keyboard (R/F/S) ---------------------------------------------------
    function onKeyDown(event) {
        const tag = event.target.tagName;
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
        switch (event.key) {
        case 'r':
            state.mouse.reset();
            renderer.setFov(renderer.getDefaultFov());
            break;
        case 'f':
            state.mouse.flip();
            break;
        case 's':
            state.stabilize = !state.stabilize;
            break;
        }
    }

    // --- Render loop --------------------------------------------------------
    // Camera quaternion setup (sailview convention):
    //   The camera's native coords have z down, x right, y back. WebGL has y
    //   up, z back. We rotate into world, apply pose, then rotate back.
    const qCameraToWorld = quat.create();
    const qWorldToCamera = quat.create();
    const qtmp = quat.create();
    const qcamera = quat.create();

    function render() {
        twgl.resizeCanvasToDisplaySize(canvas);
        gl.viewport(0, 0, canvas.width, canvas.height);

        if (state.pose_data && video.readyState >= 2) {
            const q = getPose(video.currentTime * 1000);
            if (state.stabilize && q) {
                quat.multiply(qcamera, qWorldToCamera,
                    quat.multiply(qcamera, quat.invert(qcamera, q), qCameraToWorld));
            } else {
                quat.identity(qcamera);
            }
        }

        mat4.fromQuat(uniforms.u_rotation, quat.multiply(qtmp, qcamera, state.mouse.viewpoint));
        renderer.draw({
            source: (video.readyState >= 2) ? video : null,
            rotation: uniforms.u_rotation,
            width: canvas.width,
            height: canvas.height,
        });

        const now = performance.now();
        if (now - state.lastCenterLog > 1000) {
            state.lastCenterLog = now;
            const m = uniforms.u_rotation;
            const wx = -m[8], wy = -m[9], wz = -m[10];
            const lon = Math.atan2(wx, -wz);
            const lat = Math.asin(Math.max(-1, Math.min(1, wy)));
            const u = 0.5 + lon / (2 * Math.PI);
            const v = 0.5 - lat / Math.PI;
            console.log(`[center] worldRay=(${wx.toFixed(3)}, ${wy.toFixed(3)}, ${wz.toFixed(3)}) ` +
                `lon=${(lon * 180 / Math.PI).toFixed(1)}° lat=${(lat * 180 / Math.PI).toFixed(1)}° ` +
                `uv=(${u.toFixed(3)}, ${v.toFixed(3)}) stab=${state.stabilize}`);
        }

        if (opts.onFrame && video.readyState >= 2) {
            opts.onFrame({videoTimeMs: video.currentTime * 1000, viewer: api});
        }

        if (state.skipRender) {
            requestAnimationFrame(skipFrame);
        } else {
            requestAnimationFrame(render);
        }
    }
    function skipFrame() { requestAnimationFrame(render); }

    // --- Public API ---------------------------------------------------------
    const api = {
        /** The underlying <video> element — parent can control play/pause. */
        video,
        /** The canvas element. */
        canvas,

        /**
         * Load a new video and optional pose/log sidecars.
         * @param {Object} cfg
         * @param {string} cfg.videoUrl
         * @param {string} [cfg.poseUrl]
         * @param {string} [cfg.logPath] logical path ending in `.log`. When
         *     provided with `signLog`, viewer360 dispatches through
         *     `loadLog` (v2 `.slog` first, falls back to v1 `.log`).
         * @param {(function(string):string)|{slog?:string,log:string}} [cfg.signLog]
         *     Either a sync `(path) => signedUrl` function, or a pre-signed
         *     `{slog?, log}` URL pair. See log_load.js::loadLog.
         * @param {Object} [cfg.columnsMap] index -> column name, from videos.json
         * @param {number} [cfg.startTime=0] initial currentTime in seconds
         * @param {boolean} [cfg.autoplay=false]
         * @param {string} [cfg.projection='fisheye'] 'fisheye' (Insta360) or
         *     'equirect' (VIRB-era equirectangular MP4s).
         */
        loadVideo(cfg) {
            if (!cfg.videoUrl) {
                console.error('viewer360.loadVideo missing videoUrl', cfg);
                return;
            }
            state.pose_data = null;
            state.log_data = null;
            video.removeAttribute('src');
            video.load();
            const projection = cfg.projection || 'fisheye';
            console.log(`[viewer360] loadVideo projection=${projection} basename=${cfg.basename || '?'}`);
            renderer.setProjection(projection);
            if (cfg.poseUrl) readPoses(cfg.poseUrl);
            if (cfg.logPath && cfg.signLog && cfg.columnsMap) {
                fetchLog(cfg.logPath, cfg.signLog, cfg.columnsMap, cfg.basename || null);
            }
            video.src = cfg.videoUrl;
            state.initialTime = cfg.startTime || 0;
            if (opts.seedFirstFrame && !cfg.autoplay) {
                // Force one decoded frame into the texture so the canvas
                // isn't black on first paint. Mirrors sailview's longstanding
                // dance: play(), wait for playing+timeupdate, seek, pause.
                let playing = false, timeupdated = false;
                const finish = () => {
                    if (!(playing && timeupdated)) return;
                    video.removeEventListener('playing', onPlaying);
                    video.removeEventListener('timeupdate', onTimeupdate);
                    video.currentTime = state.initialTime;
                    video.pause();
                };
                const onPlaying = () => { playing = true; finish(); };
                const onTimeupdate = () => { timeupdated = true; finish(); };
                video.addEventListener('playing', onPlaying);
                video.addEventListener('timeupdate', onTimeupdate);
                video.play().catch(() => {});
            } else {
                video.addEventListener('loadedmetadata', () => {
                    video.currentTime = state.initialTime;
                }, {once: true});
                if (cfg.autoplay) {
                    // Try unmuted autoplay. Browsers with MEI / prior gesture
                    // on this origin will allow it. On a cold first visit
                    // they refuse — fall back to muted playback so the page
                    // still moves; user presses M to unmute.
                    const wasUnmuted = !video.muted;
                    video.play().catch(() => {
                        if (!wasUnmuted) return;
                        video.muted = true;
                        video.play().catch(() => {});
                    });
                }
            }
        },

        /**
         * Seek to a Unix ms timestamp using log's row_times (if loaded).
         *
         * The log binary for a clip is often a few seconds shorter than the
         * video (the sensor stream flushes before the video does), so we
         * extrapolate past the log's endpoints at the log's sample rate and
         * let the video's own duration clamp us if we've gone too far. The
         * seek only fails (returns false) if no log is loaded at all.
         */
        seekToUnix(unixMs) {
            const rowTimes = state.log_data && state.log_data.data.row_times;
            if (!rowTimes || !rowTimes.length) return false;
            // Honor the truncation in readLog (rows may be < rowTimes.length
            // when the tail is non-monotonic).
            const usable = state.log_data.rows;
            const first = rowTimes[0], last = rowTimes[usable - 1];
            const msecs = state.log_data.msecs;
            const lastRowVideoT = (usable - 1) * msecs / 1000;

            let t;
            if (unixMs >= first && unixMs <= last) {
                let lo = 0, hi = usable - 1;
                while (lo < hi) {
                    const mid = (lo + hi) >> 1;
                    if (rowTimes[mid] < unixMs) lo = mid + 1;
                    else hi = mid;
                }
                let idx = lo;
                if (idx > 0 &&
                    Math.abs(rowTimes[idx - 1] - unixMs) < Math.abs(rowTimes[idx] - unixMs)) {
                    idx--;
                }
                t = idx * msecs / 1000;
            } else if (unixMs < first) {
                t = (unixMs - first) / 1000;
            } else {
                t = lastRowVideoT + (unixMs - last) / 1000;
            }

            // If the video's duration isn't known yet, trust the extrapolated
            // time; the <video> element will clamp internally if needed.
            const dur = isFinite(video.duration) ? video.duration : Infinity;
            video.currentTime = Math.max(0, Math.min(dur, t));
            return true;
        },

        /** Returns [firstUnixMs, lastUnixMs] from the currently-loaded log, or null. */
        getUnixRange() {
            const rt = state.log_data && state.log_data.data.row_times;
            if (!rt || !rt.length) return null;
            const usable = state.log_data.rows;
            return [rt[0], rt[usable - 1]];
        },
        getLogBasename() {
            return state.log_data ? state.log_data.basename : null;
        },

        clearMedia() {
            state.pose_data = null;
            state.log_data = null;
            video.pause();
            video.removeAttribute('src');
            video.load();
        },

        /** Milliseconds between log samples, or null if no log is loaded. */
        getLogMsecs() {
            return state.log_data ? state.log_data.msecs : null;
        },

        /** Sets whether pose-based stabilization is applied. */
        setStabilize(on) { state.stabilize = !!on; },

        /** Reads the current stabilization flag (for toggle bindings). */
        getStabilize() { return state.stabilize; },

        /** Sets whether the render loop alternates skip/draw to hit ~30 FPS. */
        setSkipFrames(on) { state.skipRender = !!on; },

        /** Reads the current skip-frames flag (for toggle bindings). */
        getSkipFrames() { return state.skipRender; },

        /** Reset viewpoint (R key equivalent). */
        reset() {
            state.mouse.reset();
            renderer.setFov(renderer.getDefaultFov());
        },

        /** Flip viewpoint 180° (F key equivalent). */
        flip() { state.mouse.flip(); },

        /** Instrument log accessors (if log was loaded). */
        getLog, getLogColumn, getLogHistory, getUnixAtVideoMs,

        /** Clean up listeners and DOM. Parent must still remove container contents if desired. */
        destroy() {
            if (opts.keyboard) window.removeEventListener('keydown', onKeyDown);
            video.pause();
            video.src = '';
            state.skipRender = false;
            // Loop will still run once; simplest to set a flag and break.
            state.destroyed = true;
        },
    };

    // --- Boot ---------------------------------------------------------------
    loadCalibrationPreset();
    state.sensorCalReady = loadSensorCalibration();
    setupMouse();
    if (opts.keyboard) window.addEventListener('keydown', onKeyDown);
    requestAnimationFrame(render);

    return api;
}

/** Convenience: fetch an AWS-signed URL via shared.signAWSUrl. */
export function signAWSUrl(path) {
    return shared.signAWSUrl(path);
}

/** Convenience: check whether a stored Google ID token is still valid. */
export function isAuthorized() {
    return shared.checkAuthorization();
}

/**
 * Return a "YYYY-MM-DD HH:MM:SS" wall-clock string for a unix-ms instant in a
 * given IANA timezone (US/Pacific for CreateDate values in this project).
 */
export function wallClockInTz(unixMs, timeZone = 'America/Los_Angeles') {
    // en-CA 24h format is "YYYY-MM-DD, HH:MM:SS", convenient for sorting.
    const s = new Intl.DateTimeFormat('en-CA', {
        timeZone, hour12: false,
        year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', second: '2-digit',
    }).format(new Date(unixMs));
    return s.replace(', ', ' ');
}

/**
 * Normalize a CreateDate like "2026/04/08 19:13:48" or "2026:04:08 19:13:48"
 * (both seen in the wild) to "2026-04-08 19:13:48" for lexicographic comparison
 * with wallClockInTz() output.
 */
function normalizeCreateDate(cd) {
    if (!cd || cd.length < 19) return '';
    // First 10 chars are the date separators; time keeps its colons.
    return cd.slice(0, 10).replace(/[/:]/g, '-') + cd.slice(10, 19);
}

/**
 * Pick the recording that contains (or most likely contains) `unixMs`.
 *
 * Strategy: CreateDate is authored in US/Pacific. Convert `unixMs` to a
 * Pacific wall-clock string and choose the latest video whose CreateDate is
 * <= target. This handles multi-clip days correctly (each Insta360 file is
 * ~30 min). Only returns a record whose CreateDate is on the same Pacific
 * calendar day as the target.
 *
 * Returns null if no candidate is found. Callers should also verify via the
 * loaded log's row_times range — this is a best-effort selection based on
 * CreateDate alone, without peeking at each log file.
 */
export function pickVideoForUnix(videos, unixMs) {
    if (!videos || !videos.length) return null;

    // Precise path: any record with StartUnixMs (GPS-aligned, written by
    // the Python pipeline from the .slog header) participates in a numeric
    // sort. Pick the latest whose start is <= target.
    const precise = videos
        .filter((v) => Number.isFinite(v.StartUnixMs))
        .sort((a, b) => a.StartUnixMs - b.StartUnixMs);
    if (precise.length) {
        let pick = null;
        for (const v of precise) {
            if (v.StartUnixMs <= unixMs) pick = v;
            else break;
        }
        if (pick) return pick;
    }

    // Legacy path: Pacific-wall-clock CreateDate (camera EXIF, drifts
    // ~tens of seconds vs GPS).
    const targetKey = wallClockInTz(unixMs, 'America/Los_Angeles'); // "YYYY-MM-DD HH:MM:SS"
    const targetDay = targetKey.slice(0, 10);

    const candidates = [];
    for (const v of videos) {
        if (Number.isFinite(v.StartUnixMs)) continue;  // already considered above
        const key = normalizeCreateDate(v.CreateDate);
        if (key) candidates.push({v, key});
    }
    candidates.sort((a, b) => a.key.localeCompare(b.key));

    let pick = null;
    for (const c of candidates) {
        if (c.key <= targetKey) pick = c;
        else break;
    }
    if (!pick) return null;
    if (pick.key.slice(0, 10) !== targetDay) return null;
    return pick.v;
}

/**
 * Seconds from the start of `record`'s video to wall-clock instant
 * `unixMs`. Prefers GPS-aligned `StartUnixMs` (precise) and falls back to
 * Pacific-wall-clock CreateDate parsing (drifts ~tens of seconds vs GPS).
 *
 * Returns 0 if neither anchor is parseable. Caller should clamp to 0 if a
 * negative offset (target precedes clip start) would be a bug for them.
 */
export function clipOffsetForUnix(record, unixMs) {
    if (!record || !Number.isFinite(unixMs)) return 0;
    if (Number.isFinite(record.StartUnixMs)) {
        return (unixMs - record.StartUnixMs) / 1000;
    }
    const cd = record.CreateDate;
    if (!cd || cd.length < 19) return 0;
    // Convert both sides to Pacific wall-clock strings, then parse as UTC;
    // the tz offset cancels because both values are in the same zone. Same
    // trick used by review.js's clipOffsetSeconds.
    const clipKey = cd.slice(0, 10).replace(/[/:]/g, '-') + cd.slice(10, 19);
    const targetKey = wallClockInTz(unixMs, 'America/Los_Angeles');
    const clipStart = Date.parse(clipKey.replace(' ', 'T') + 'Z');
    const target = Date.parse(targetKey.replace(' ', 'T') + 'Z');
    if (!Number.isFinite(clipStart) || !Number.isFinite(target)) return 0;
    return (target - clipStart) / 1000;
}

// Bundle everything under a single default export so the UMD global is flat:
//   window.Viewer360.create(container, opts)
//   window.Viewer360.signAWSUrl(path)
export default {
    create,
    signAWSUrl,
    isAuthorized,
    pickVideoForUnix,
    clipOffsetForUnix,
    wallClockInTz,
};
