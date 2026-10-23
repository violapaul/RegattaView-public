/**
 * tracks.js - AIS sailing-day index and per-day track viewer.
 *
 * Data layout:
 *   Analyze/AISDays/index.json
 *   Analyze/AISDays/<YYYY-MM-DD>.json
 */

import '../css/tracks.css';
import * as shared from './shared';
import * as utils from './utils';
import * as he from './htmlElements';
import * as sparkline from './sparkline';
import {loadLog} from './log_load';

// Class-B AIS drops to a 3-minute (180 s) report cadence when a boat is slow or
// stationary, so a 180 s bound sits right on the knife's edge and the marker
// flickers out on every missed slot. 400 s bridges one missed 3-minute report
// while still hiding genuine multi-minute reception gaps. A parked boat's dot
// can now be up to ~6.5 min stale; a racing boat (30 s cadence) is unaffected.
const AIS_MAX_GAP_SEC = 400;
// How far back each boat's trail extends from the playhead, in seconds.
// User-adjustable via the track-length slider; 20 min suits a typical short
// buoy race, while the upper bound (2 h) covers a full day's session.
const TRACK_LENGTH_MIN_SEC = 5 * 60;
const TRACK_LENGTH_MAX_SEC = 2 * 60 * 60;
const TRACK_LENGTH_STEP_SEC = 5 * 60;
const TRACK_LENGTH_DEFAULT_SEC = 20 * 60;
const TRACK_LENGTH_KEY = 'tracksTrailLengthSec';
let trackHistorySec = TRACK_LENGTH_DEFAULT_SEC;
const STORAGE_PREFIX = 'tracks_viewer_state_';
const EGO_KEY = '__ego__';
const TRACK_COLORS = [
    '#ffd60a', '#ff375f', '#32d74b', '#ff9f0a',
    '#bf5af2', '#64d2ff', '#a3ff12', '#ff6bff',
];

// ---- Instrument overlay constants --------------------------------------
// Mirrors sailview's AVAILABLE_CHANNELS but drops `fps` (no video here).
// `dt` is a synthetic channel that renders the clock from `currentTime`.
const INSTR_AVAILABLE = [
    {key: 'dt', label: 'DT'},
    {key: 'hdg', label: 'HDG'},
    {key: 'cog', label: 'COG'},
    {key: 'spd', label: 'SPD'},
    {key: 'sog', label: 'SOG'},
    {key: 'awa', label: 'AWA'},
    {key: 'aws', label: 'AWS'},
    {key: 'twd', label: 'TWD'},
    {key: 'tws', label: 'TWS'},
    {key: 'twa', label: 'TWA'},
    {key: 'gtwd', label: 'GTWD'},
    {key: 'gtws', label: 'GTWS'},
    {key: 'gtwa', label: 'GTWA'},
    {key: 'heel', label: 'HEEL'},
    {key: 'rudder', label: 'RUDDER'},
    {key: 'turn_rate', label: 'TURN'},
    {key: 'depth', label: 'DEPTH'},
];
const INSTR_DEFAULT_FIELDS = ['dt', 'hdg', 'twd', 'tws', 'spd', 'awa', 'twa', 'heel'];
const INSTR_COOKIE_KEY = 'tracksSensorFields';
// Sparkline window in seconds. Wider than sailview's 10 s — at 60× playback,
// 120 s of game time elapses in 2 s of wall time, so 120 s keeps the trend
// readable across the speed ladder.
const INSTR_SPARK_SEC = 120;
// Max wall-time delta between scrubber and nearest log row before a sample is
// considered out-of-band (multi-session gap, missing parquet, etc.). Bigger
// than parquet's native ~0.1 s spacing so 1 Hz quantization never trips it.
const INSTR_MAX_GAP_S = 30;
const INSTR_ONE_DECIMAL = new Set(['spd', 'sog', 'aws', 'tws', 'gtws', 'heel', 'depth']);
// `heel` is synthesized from `zg100_roll - cal.heel.offset` by
// applySensorCalibration (mirrors viewer360.js:158-164), so reads of
// data['heel'] get the calibrated value directly — no alias needed.
const INSTR_CHANNEL_ALIAS = {};

// Sensor calibration applied to the per-day .log AFTER fetch, matching
// what sailview's viewer360 does. Same JSON file the viewer reads, so any
// change to instrument calibration affects both pages atomically.
const SENSOR_CAL_URL = '/assets/calibration/sensor_calibration.json';
const sensorCalibration = {
    spd: {scale: 1.0, offset: 0.0},
    heading: {offset: 0.0},
    heel: {offset: 0.0},
    awa: {offset: 0.0},
    depth: {offset: 0.0},
};
let sensorCalibrationLoaded = false;

const SPEEDS = [1, 4, 16, 60];

// ---- Fleet (per-AIS-boat speed overlay) constants ----------------------
// Sparkline window for each boat's SOG history. 5 min ~= 10 AIS samples at
// typical Class B ~30 s broadcast cadence, enough to read a trend.
const FLEET_SPARK_SEC = 300;
// Max time-gap (sec) between scrubber and nearest track sample before we
// show '--' / no marker. Wider than the AIS rate so quiet boats don't
// blink off.
const FLEET_MAX_GAP_S = 120;

// ---- Ladder lines (wind-perpendicular rungs) ---------------------------
// End-to-end rung length capped to whichever is shorter at current zoom:
//  - LADDER_MAX_M  in real-world units (so close-zoom rungs convey scale)
//  - LADDER_MAX_PX on screen (so wide-zoom rungs don't dwarf the chart)
const LADDER_MAX_M = 200;
const LADDER_MAX_PX = 100;
const LADDER_TOGGLE_KEY = 'tracksLadderLines';
let ladderOn = false;
// Lazy per-boat layer cache, keyed by EGO_KEY or mmsi string.
//   {key -> {line, label}}
const ladderLayers = new Map();

let map = null;
let tracksData = null;
let currentTime = null;
let egoLayer = null;
let egoLayerHalo = null;
let egoMarker = null;
let boatLayers = {};
let boatState = new Map();
let selectedBoatKey = null;

// Instrument panel + playback state.
let logData = null;          // parseLogV1 / parseSlog result, or null if absent
let logColIdx = null;        // {channelKey: colIndex} for fast log lookups
const dataFields = [];       // [{channel, span, sparkline, rowEl}, ...]
let fieldsContainer = null;  // div inside #instrument-panel
let suppressFieldsSave = false;
let isPlaying = false;
let speedIdx = 0;
let playRaf = null;
let playLastWallMs = 0;

// Fleet panel: lazy per-mmsi DOM cache keyed by EGO_KEY or mmsi string.
//   {mmsi -> {rowEl, swatchEl, nameEl, sogEl, distEl, sparkEl}}
const fleetRows = new Map();

function leaf() {
    return window.L;
}

function dataGetJSON(wwwPath) {
    let url = shared.dataGetUrl(wwwPath);
    if (utils.isLocal() && wwwPath.startsWith('Analyze/')) {
        url += (url.includes('?') ? '&' : '?') + `v=${Date.now()}`;
    }
    return utils.fetchJSON(url);
}

function boatName(boat) {
    const name = (boat.name || '').trim();
    return boat.race_name || name || `MMSI ${boat.mmsi}`;
}

function isFleetBoat(boat) {
    return (boat.fleet_tags || []).includes('j105');
}

function colorFor(mmsi) {
    let h = 0;
    const s = String(mmsi);
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) & 0xffff;
    return TRACK_COLORS[h % TRACK_COLORS.length];
}

function trackStyles(color, selected, isEgo = false) {
    return {
        halo: selected ? {
            color: isEgo ? '#ffd60a' : '#05070a',
            weight: 7,
            opacity: 0.9,
        } : null,
        line: {
            color,
            weight: selected ? 4 : 2,
            opacity: selected ? 1 : 0.85,
        },
    };
}

function formatTime(ts) {
    return new Date(ts * 1000).toLocaleString([], {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
    });
}

function formatDuration(sec) {
    const s = Math.max(0, Math.round(sec));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const r = s % 60;
    if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(r).padStart(2, '0')}`;
    return `${m}:${String(r).padStart(2, '0')}`;
}

// Compact label for the trail-length slider: "20 min", "1 h", "1 h 30 min".
function formatTrackLength(sec) {
    const totalMin = Math.round(sec / 60);
    const h = Math.floor(totalMin / 60);
    const m = totalMin % 60;
    if (h === 0) return `${m} min`;
    if (m === 0) return `${h} h`;
    return `${h} h ${m} min`;
}

function upperBoundTrack(track, t) {
    let lo = 0;
    let hi = track.length;
    while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (track[mid][2] <= t) lo = mid + 1;
        else hi = mid;
    }
    return lo - 1;
}

function sampleAt(track, t, maxGapSec) {
    if (!track || track.length === 0) return null;
    const prevIdx = upperBoundTrack(track, t);
    const prev = prevIdx >= 0 ? track[prevIdx] : null;
    const next = prevIdx + 1 < track.length ? track[prevIdx + 1] : null;

    // 4-tuples carry SOG (knots) in slot 3; 3-tuples (ego_track) don't.
    // Linearly interpolate SOG too when both neighbors have it; if only one
    // side has it, fall through to that side's value; if neither, return
    // sog: null so callers can show '--'.
    const lerpSog = (a, b, f) => {
        if (a == null && b == null) return null;
        if (a == null) return b;
        if (b == null) return a;
        return a * (1 - f) + b * f;
    };

    if (prev && next) {
        const dt = next[2] - prev[2];
        if (dt > 0 && dt <= maxGapSec) {
            const f = Math.max(0, Math.min(1, (t - prev[2]) / dt));
            const out = {
                lat: prev[0] * (1 - f) + next[0] * f,
                lon: prev[1] * (1 - f) + next[1] * f,
            };
            if (prev.length >= 4 || next.length >= 4) {
                out.sog = lerpSog(prev[3] ?? null, next[3] ?? null, f);
            }
            return out;
        }
    }

    if (prev && t - prev[2] <= maxGapSec) {
        const out = {lat: prev[0], lon: prev[1]};
        if (prev.length >= 4) out.sog = prev[3] ?? null;
        return out;
    }
    return null;
}

// ---- Per-day instrument log access -------------------------------------

/** Resolve a UI channel key (e.g. 'heel') to a .log column name (e.g. 'zg100_roll'). */
function instrChannelColumn(channel) {
    return INSTR_CHANNEL_ALIAS[channel] || channel;
}

/**
 * Binary-search the .log's row_times column for the largest index whose
 * timestamp is <= targetMs. The .log is uniform 1 Hz within a session but may
 * have jumps between sessions, so we don't rely on `msec_per_sample` indexing.
 */
function logRowAtMs(targetMs) {
    if (!logData) return -1;
    const times = logData.data.row_times;
    if (!times || times.length === 0) return -1;
    if (targetMs <= times[0]) return 0;
    if (targetMs >= times[times.length - 1]) return times.length - 1;
    let lo = 0;
    let hi = times.length - 1;
    while (lo < hi) {
        const mid = (lo + hi + 1) >> 1;
        if (times[mid] <= targetMs) lo = mid;
        else hi = mid - 1;
    }
    return lo;
}

/** Look up a single channel value at the given Unix seconds, or null if no .log / out of band. */
function getLogValue(unixSec, channel) {
    if (!logData) return null;
    const colName = instrChannelColumn(channel);
    const col = logData.data[colName];
    if (!col) return null;
    const targetMs = unixSec * 1000;
    const i = logRowAtMs(targetMs);
    if (i < 0) return null;
    const rowMs = logData.data.row_times[i];
    if (Math.abs(targetMs - rowMs) > INSTR_MAX_GAP_S * 1000) return null;
    return col[i];
}

/** Return [tStartSec, channel] history of values inside the sparkline window. */
function getLogHistory(unixSec, channel, windowSec) {
    if (!logData) return [];
    const colName = instrChannelColumn(channel);
    const col = logData.data[colName];
    if (!col) return [];
    const targetMs = unixSec * 1000;
    const startMs = targetMs - windowSec * 1000;
    const lo = logRowAtMs(startMs);
    const hi = logRowAtMs(targetMs);
    if (hi < lo) return [];
    const out = new Array(hi - lo + 1);
    for (let i = lo; i <= hi; i++) out[i - lo] = col[i];
    return out;
}

function formatChannelValue(channel, value) {
    if (value == null || Number.isNaN(value)) return '--';
    if (INSTR_ONE_DECIMAL.has(channel)) return value.toFixed(1);
    return Math.round(value).toString();
}

function formatClockFromUnixSec(t) {
    if (t == null) return '--';
    return new Date(t * 1000).toLocaleTimeString([], {hour12: false});
}


// ---- Instrument panel build / persistence ------------------------------

function loadInstrCookie() {
    try {
        const raw = localStorage.getItem(INSTR_COOKIE_KEY);
        const arr = raw ? JSON.parse(raw) : null;
        if (!Array.isArray(arr)) return null;
        const valid = new Set(INSTR_AVAILABLE.map((c) => c.key));
        const filtered = arr.filter((k) => valid.has(k));
        return filtered.length ? filtered : null;
    } catch (_) {
        return null;
    }
}

function saveInstrCookie() {
    if (suppressFieldsSave) return;
    try {
        const keys = dataFields.map((f) => f.channel);
        localStorage.setItem(INSTR_COOKIE_KEY, JSON.stringify(keys));
    } catch (_) {
        // private mode — ignore
    }
}

function addInstrField(channel) {
    const field = he.addDataField(
        fieldsContainer, channel, INSTR_AVAILABLE,
        (newCh) => {
            field.channel = newCh;
            field.rowEl.setAttribute('data-channel', newCh);
            // Re-render immediately so the user sees the new channel's value
            // without waiting for the next playback frame or scrubber tick.
            updateInstrumentsForTime();
            saveInstrCookie();
        },
        () => {
            const idx = dataFields.indexOf(field);
            if (idx >= 0) {
                dataFields.splice(idx, 1);
                field.rowEl.remove();
                saveInstrCookie();
            }
        },
    );
    field.rowEl.setAttribute('data-channel', channel);
    // Seed the sparkline SVG with a flat line so its layout box is sized
    // correctly before the first real sample arrives.
    sparkline.sparkline(field.sparkline, [1, 1, 1, 1, 1]);
    dataFields.push(field);
    return field;
}

function buildInstrumentPanel() {
    const panel = document.getElementById('instrument-panel');
    panel.innerHTML = '';
    dataFields.length = 0;
    fieldsContainer = document.createElement('div');
    panel.appendChild(fieldsContainer);

    he.addAddButton(panel, () => {
        const used = new Set(dataFields.map((f) => f.channel));
        const next = INSTR_AVAILABLE.find((c) => !used.has(c.key)) || INSTR_AVAILABLE[0];
        addInstrField(next.key);
        saveInstrCookie();
    });

    const saved = loadInstrCookie();
    suppressFieldsSave = true;
    try {
        (saved || INSTR_DEFAULT_FIELDS).forEach(addInstrField);
    } finally {
        suppressFieldsSave = false;
    }
}


// ---- Per-day .log fetch ------------------------------------------------

/**
 * Fetch the sensor calibration JSON once per page load (same file
 * sailview's viewer360 uses, so the two pages stay in lockstep).
 */
async function loadSensorCalibrationOnce() {
    if (sensorCalibrationLoaded) return;
    try {
        const data = await utils.fetchJSON(SENSOR_CAL_URL);
        if (data.spd) Object.assign(sensorCalibration.spd, data.spd);
        if (data.heading) Object.assign(sensorCalibration.heading, data.heading);
        if (data.heel) Object.assign(sensorCalibration.heel, data.heel);
        if (data.awa) Object.assign(sensorCalibration.awa, data.awa);
        if (data.depth) Object.assign(sensorCalibration.depth, data.depth);
        sensorCalibrationLoaded = true;
    } catch (err) {
        utils.surfaceError(`Sensor calibration failed to load (${err.message}). SPD/heel/AWA may be uncalibrated.`,
            {key: 'sensor-cal'});
    }
}

/**
 * Apply spd scale+offset, heel offset, awa offset, depth offset to the
 * per-day .log columns. Mirrors viewer360.js::applySensorCalibration so
 * tracks.html displays the same calibrated numbers sailview shows.
 *
 * `heel` is synthesized as a new column (= zg100_roll - heel.offset) so
 * existing reads of data['heel'] see the calibrated value; data['zg100_roll']
 * stays raw for callers that want it.
 */
function applySensorCalibration(data) {
    const cal = sensorCalibration;
    if (data.spd && (cal.spd.scale !== 1.0 || cal.spd.offset !== 0.0)) {
        const raw = data.spd;
        const out = new Float64Array(raw.length);
        for (let i = 0; i < raw.length; i++) out[i] = raw[i] * cal.spd.scale + cal.spd.offset;
        data.spd = out;
    }
    if (data.zg100_roll) {
        const raw = data.zg100_roll;
        const out = new Float64Array(raw.length);
        const offset = cal.heel.offset;
        for (let i = 0; i < raw.length; i++) out[i] = raw[i] - offset;
        data.heel = out;
    }
    if (data.awa && cal.awa.offset !== 0.0) {
        const raw = data.awa;
        const out = new Float64Array(raw.length);
        for (let i = 0; i < raw.length; i++) out[i] = raw[i] + cal.awa.offset;
        data.awa = out;
    }
    if (cal.heading && cal.heading.offset !== 0.0) {
        // Compass offset (Analyze/HEADING_CALIBRATION.md): HDG and TWD are both
        // compass/earth-referenced so both shift; TWA (boat-relative) is left as-is,
        // which keeps twa = twd-(hdg+var) consistent. turn_rate is offset-invariant.
        const hoff = cal.heading.offset;
        for (const key of ['hdg', 'twd']) {
            const raw = data[key];
            if (!raw) continue;
            const out = new Float64Array(raw.length);
            for (let i = 0; i < raw.length; i++) out[i] = ((raw[i] + hoff) % 360 + 360) % 360;
            data[key] = out;
        }
    }
    if (data.depth && cal.depth.offset !== 0.0) {
        const raw = data.depth;
        const out = new Float64Array(raw.length);
        for (let i = 0; i < raw.length; i++) out[i] = raw[i] + cal.depth.offset;
        data.depth = out;
    }
}

async function loadDayLog(date, logFile, columnsMap) {
    if (!logFile || !columnsMap) {
        logData = null;
        logColIdx = null;
        return;
    }
    const wwwPath = `Analyze/AISDays/${logFile}`;
    const logUrl = shared.dataGetUrl(wwwPath);
    // Pass the object form (`{log: ...}`) so loadLog skips the `.slog`-first
    // probe — we don't produce `.slog` for per-day logs.
    const columnNumbers = utils.reverseMapping(columnsMap);
    try {
        // Calibration must be loaded BEFORE applySensorCalibration so the
        // first day-load reflects the right scale/offset. Subsequent loads
        // are no-ops (sensorCalibrationLoaded guards the fetch).
        await loadSensorCalibrationOnce();
        const parsed = await loadLog(wwwPath, {log: logUrl}, columnNumbers);
        applySensorCalibration(parsed.data);
        logData = parsed;
        logColIdx = columnsMap;
    } catch (err) {
        logData = null;
        logColIdx = null;
        utils.surfaceError(`Per-day log fetch failed for ${date}: ${err.message}`,
            {key: 'tracks-log'});
    }
}


// ---- Per-frame instrument update ---------------------------------------

function updateInstrumentsForTime() {
    const panel = document.getElementById('instrument-panel');
    if (!panel) return;
    if (!logData || currentTime == null) {
        panel.hidden = true;
        return;
    }
    panel.hidden = false;
    for (const field of dataFields) {
        const ch = field.channel;
        if (ch === 'dt') {
            field.span.innerText = formatClockFromUnixSec(currentTime);
            // Sparkline-for-clock would be meaningless; leave the seeded
            // flat line in place.
            continue;
        }
        const v = getLogValue(currentTime, ch);
        field.span.innerText = formatChannelValue(ch, v);
        const hist = getLogHistory(currentTime, ch, INSTR_SPARK_SEC);
        if (hist.length >= 2) sparkline.sparkline(field.sparkline, hist);
    }
}


// ---- Playback ----------------------------------------------------------

function clamp(v, lo, hi) {
    return Math.max(lo, Math.min(hi, v));
}

function setTimeSeconds(t) {
    if (!tracksData) return;
    const t0 = tracksData.time_start;
    const t1 = tracksData.time_end;
    currentTime = clamp(t, t0, t1);
    const range = document.getElementById('time-range');
    if (range) range.value = Math.round(currentTime - t0);
    updateMapForTime();
    if (currentTime >= t1) stopPlay();
}

function setPlayBtnIcon() {
    const btn = document.getElementById('play-btn');
    if (!btn) return;
    // HeydingsControlsRegular: 'P' = play glyph, 'u' = pause glyph.
    // (Matches sailview.js::playPauseMedia. `;` is not a glyph and renders
    // blank, which looks like the click did nothing.)
    btn.setAttribute('data-icon', isPlaying ? 'u' : 'P');
}

function setSpeedBtnLabel() {
    const btn = document.getElementById('speed-btn');
    if (btn) btn.textContent = SPEEDS[speedIdx] + 'x';
}

function startPlay() {
    if (isPlaying || !tracksData) return;
    isPlaying = true;
    playLastWallMs = performance.now();
    setPlayBtnIcon();
    const tick = (now) => {
        if (!isPlaying) return;
        const dt = (now - playLastWallMs) / 1000;
        playLastWallMs = now;
        setTimeSeconds(currentTime + dt * SPEEDS[speedIdx]);
        if (isPlaying) playRaf = requestAnimationFrame(tick);
    };
    playRaf = requestAnimationFrame(tick);
}

function stopPlay() {
    if (!isPlaying && playRaf == null) {
        setPlayBtnIcon();
        return;
    }
    isPlaying = false;
    if (playRaf != null) cancelAnimationFrame(playRaf);
    playRaf = null;
    setPlayBtnIcon();
}

function togglePlay() {
    if (isPlaying) stopPlay();
    else startPlay();
}

function cycleSpeed() {
    speedIdx = (speedIdx + 1) % SPEEDS.length;
    setSpeedBtnLabel();
    // Reset the RAF baseline so the next tick doesn't double-advance time.
    playLastWallMs = performance.now();
}

function skipSeconds(s) {
    if (!tracksData) return;
    setTimeSeconds(currentTime + s);
}


function historyPoints(track, t, sample) {
    const pts = [];
    const startTime = t - trackHistorySec;
    const startIdx = Math.max(0, upperBoundTrack(track, startTime) + 1);
    const endIdx = upperBoundTrack(track, t);
    for (let i = startIdx; i <= endIdx; i++) {
        pts.push([track[i][0], track[i][1]]);
    }
    if (sample && (pts.length === 0
            || pts[pts.length - 1][0] !== sample.lat
            || pts[pts.length - 1][1] !== sample.lon)) {
        pts.push([sample.lat, sample.lon]);
    }
    return pts;
}

function getBoatPrefs(mmsi, defaults = {show: true, track: false}) {
    const key = String(mmsi);
    if (!boatState.has(key)) {
        boatState.set(key, {...defaults});
    }
    return boatState.get(key);
}

function getEgoPrefs() {
    return getBoatPrefs(EGO_KEY, {show: true, track: true});
}

function saveBoatState() {
    if (!tracksData) return;
    const obj = {};
    boatState.forEach((value, key) => { obj[key] = value; });
    localStorage.setItem(STORAGE_PREFIX + tracksData.date, JSON.stringify(obj));
}

function loadBoatState(date, boats) {
    boatState = new Map();
    let stored = {};
    try {
        stored = JSON.parse(localStorage.getItem(STORAGE_PREFIX + date) || '{}');
    } catch (e) {
        stored = {};
    }
    for (const boat of boats) {
        const mmsi = String(boat.mmsi);
        const existing = stored[mmsi] || {};
        // J/105 fleet boats default to tracked (the common use case is
        // watching the fleet); everything else defaults to show-only. A
        // stored explicit choice always wins, so an untracked J/105 stays
        // untracked across reloads.
        const trackDefault = isFleetBoat(boat);
        boatState.set(mmsi, {
            show: existing.show !== false,
            track: 'track' in existing ? existing.track === true : trackDefault,
        });
    }
    const egoExisting = stored[EGO_KEY] || {};
    boatState.set(EGO_KEY, {
        show: egoExisting.show !== false,
        track: egoExisting.track !== false,
    });
}

function initMap() {
    if (map) return;
    const L = leaf();
    map = L.map('map', {
        zoomControl: true,
        // Disable the default bottom-right attribution control and re-add
        // it on the bottom-LEFT so the bottom-right corner is free for the
        // overlay legend (#map-overlay).
        attributionControl: false,
        keyboard: false,
    });
    L.control.attribution({position: 'bottomleft'}).addTo(map);
    const openStreetMap = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        attribution: '© OpenStreetMap contributors',
        maxZoom: 18,
    });
    const seattleCharts = L.tileLayer('/SeattleTiles/{z}/{x}/{y}.png', {
        attribution: 'NOAA nautical chart tiles',
        maxNativeZoom: 14,
        maxZoom: 18,
        opacity: 0.28,
    });
    openStreetMap.addTo(map);
    L.control.layers({
        'OpenStreetMap': openStreetMap,
    }, {
        'NOAA chart/depth overlay': seattleCharts,
    }, {position: 'topleft'}).addTo(map);
    // Rung pixel-cap depends on zoom; re-render rungs whenever the zoom
    // level settles. Pan doesn't change pixel scale, so `zoomend` is
    // sufficient and avoids per-pan thrash.
    map.on('zoomend', updateLadderLayers);
}

function clearMapLayers() {
    if (egoLayerHalo) { egoLayerHalo.remove(); egoLayerHalo = null; }
    if (egoLayer) { egoLayer.remove(); egoLayer = null; }
    if (egoMarker) { egoMarker.remove(); egoMarker = null; }
    for (const mmsi in boatLayers) {
        const g = boatLayers[mmsi];
        if (g.trailHalo) g.trailHalo.remove();
        if (g.trail) g.trail.remove();
        if (g.marker) g.marker.remove();
    }
    boatLayers = {};
    clearLadderLayers();
}

function renderStaticMap() {
    const L = leaf();
    clearMapLayers();
    map.invalidateSize();

    const bounds = L.latLngBounds([]);
    const extendSampled = (track, every = 10) => {
        for (let i = 0; i < track.length; i += every) bounds.extend([track[i][0], track[i][1]]);
        if (track.length) bounds.extend([track[track.length - 1][0], track[track.length - 1][1]]);
    };
    extendSampled(tracksData.ego_track || [], 10);
    if (bounds.isValid()) {
        map.fitBounds(bounds, {padding: [8, 8]});
    } else {
        for (const boat of tracksData.boats || []) extendSampled(boat.track || [], 20);
        if (bounds.isValid()) map.fitBounds(bounds, {padding: [8, 8]});
    }

    setTimeout(() => map.invalidateSize(), 0);
}

function updateEgoLayers() {
    const L = leaf();
    const prefs = getEgoPrefs();
    const selected = selectedBoatKey === EGO_KEY;
    const track = tracksData.ego_track || [];
    const sample = prefs.show ? sampleAt(track, currentTime, 30) : null;
    const counts = {visible: 0, tracks: 0};
    if (!prefs.show || !sample) {
        if (egoMarker) { egoMarker.remove(); egoMarker = null; }
        if (egoLayerHalo) { egoLayerHalo.remove(); egoLayerHalo = null; }
        if (egoLayer) { egoLayer.remove(); egoLayer = null; }
        return counts;
    }

    counts.visible = 1;
    const latLng = [sample.lat, sample.lon];
    const markerStyle = {
        radius: selected ? 9 : 7,
        color: selected ? '#ffd60a' : '#05070a',
        weight: selected ? 4 : 2,
        fillColor: '#ffffff',
        fillOpacity: 1,
    };
    if (egoMarker) egoMarker.setLatLng(latLng).setStyle(markerStyle);
    else {
        egoMarker = L.circleMarker(latLng, markerStyle)
            .addTo(map)
            .bindTooltip((tracksData.ego && tracksData.ego.name) || 'Our boat', {direction: 'top', offset: [0, -6]})
            .on('click', () => selectBoat(EGO_KEY));
    }

    if (prefs.track) {
        const pts = historyPoints(track, currentTime, sample);
        if (pts.length >= 2) {
            counts.tracks = 1;
            const styles = trackStyles('#ffffff', selected, true);
            if (styles.halo) {
                if (egoLayerHalo) egoLayerHalo.setLatLngs(pts).setStyle(styles.halo);
                else egoLayerHalo = L.polyline(pts, styles.halo).addTo(map);
            } else if (egoLayerHalo) {
                egoLayerHalo.remove();
                egoLayerHalo = null;
            }
            if (egoLayer) egoLayer.setLatLngs(pts).setStyle(styles.line);
            else egoLayer = L.polyline(pts, styles.line).addTo(map);
            if (egoLayerHalo) egoLayerHalo.bringToFront();
            egoLayer.bringToFront();
            if (egoMarker) egoMarker.bringToFront();
        }
    } else if (egoLayer || egoLayerHalo) {
        if (egoLayerHalo) { egoLayerHalo.remove(); egoLayerHalo = null; }
        if (egoLayer) egoLayer.remove();
        egoLayer = null;
    }
    return counts;
}

function removeBoatLayer(mmsi) {
    const g = boatLayers[mmsi];
    if (!g) return;
    if (g.trailHalo) { g.trailHalo.remove(); g.trailHalo = null; }
    if (g.trail) { g.trail.remove(); g.trail = null; }
    if (g.marker) { g.marker.remove(); g.marker = null; }
}

function updateBoatLayers(counts = {visible: 0, tracks: 0}) {
    const L = leaf();
    let visible = counts.visible;
    let tracks = counts.tracks;

    for (const boat of tracksData.boats || []) {
        const mmsi = String(boat.mmsi);
        const prefs = getBoatPrefs(mmsi);
        const sample = prefs.show ? sampleAt(boat.track || [], currentTime, AIS_MAX_GAP_SEC) : null;
        if (!prefs.show || !sample) {
            removeBoatLayer(mmsi);
            continue;
        }

        visible += 1;
        const color = colorFor(mmsi);
        const selected = selectedBoatKey === mmsi;
        let g = boatLayers[mmsi];
        if (!g) g = boatLayers[mmsi] = {};

        const latLng = [sample.lat, sample.lon];
        const markerStyle = {
            radius: selected ? 8 : 5,
            color: selected ? '#ffffff' : '#05070a',
            weight: selected ? 3 : 2,
            fillColor: color,
            fillOpacity: 1,
        };
        if (g.marker) g.marker.setLatLng(latLng).setStyle(markerStyle);
        else {
            g.marker = L.circleMarker(latLng, markerStyle)
                .addTo(map)
                .bindTooltip(utils.htmlEncode(boatName(boat)), {direction: 'top', offset: [0, -5]})
                .on('click', () => selectBoat(mmsi));
        }

        if (prefs.track) {
            const pts = historyPoints(boat.track || [], currentTime, sample);
            if (pts.length >= 2) {
                tracks += 1;
                const styles = trackStyles(color, selected);
                if (styles.halo) {
                    if (g.trailHalo) g.trailHalo.setLatLngs(pts).setStyle(styles.halo);
                    else g.trailHalo = L.polyline(pts, styles.halo).addTo(map);
                } else if (g.trailHalo) {
                    g.trailHalo.remove();
                    g.trailHalo = null;
                }
                if (g.trail) g.trail.setLatLngs(pts).setStyle(styles.line);
                else g.trail = L.polyline(pts, styles.line).addTo(map);
                if (g.trailHalo) g.trailHalo.bringToFront();
                g.trail.bringToFront();
                if (g.marker) g.marker.bringToFront();
            } else if (g.trail || g.trailHalo) {
                if (g.trailHalo) { g.trailHalo.remove(); g.trailHalo = null; }
                if (g.trail) g.trail.remove();
                g.trail = null;
            }
        } else if (g.trail || g.trailHalo) {
            if (g.trailHalo) { g.trailHalo.remove(); g.trailHalo = null; }
            if (g.trail) g.trail.remove();
            g.trail = null;
        }
    }

    document.getElementById('active-count').textContent = `${visible} visible, ${tracks} tracks`;
}

function updateTimeUI() {
    if (!tracksData) return;
    document.getElementById('time-readout').textContent =
        `${formatTime(currentTime)}  (+${formatDuration(currentTime - tracksData.time_start)})`;
}


// ---- Fleet (per-boat speed) overlay ------------------------------------

/** Flat-earth distance (m) between two {lat, lon} samples, anchored on `latRefDeg`. */
function flatDistanceM(a, b, latRefDeg) {
    if (!a || !b) return null;
    const R = 6371000.0;
    const cosRef = Math.cos(latRefDeg * Math.PI / 180);
    const dx = (b.lon - a.lon) * Math.PI / 180 * R * cosRef;
    const dy = (b.lat - a.lat) * Math.PI / 180 * R;
    return Math.hypot(dx, dy);
}

/** Pull SOG samples within [t - windowSec, t] from a 4-tuple AIS track. */
function historySogs(track, t, windowSec) {
    if (!track || track.length === 0) return [];
    const startT = t - windowSec;
    // upperBoundTrack returns the largest idx with ts <= t.
    const hi = upperBoundTrack(track, t);
    let lo = hi;
    while (lo > 0 && track[lo - 1][2] >= startT) lo--;
    const out = [];
    for (let i = lo; i <= hi; i++) {
        const p = track[i];
        if (p && p.length >= 4 && p[3] != null) out.push(p[3]);
    }
    return out;
}

/** Build a fleet row's DOM lazily; returns the cache entry. */
function ensureFleetRow(key, displayName, color) {
    let entry = fleetRows.get(key);
    if (entry) return entry;
    const panel = document.getElementById('fleet-panel');
    // The HTML may pre-date the JS (stale dev-server template) — bail
    // quietly rather than crashing the whole page on a null appendChild.
    if (!panel) return null;
    const row = document.createElement('div');
    row.className = 'fleet-row';
    row.dataset.id = key;

    const swatch = document.createElement('span');
    swatch.className = 'fleet-swatch';
    row.appendChild(swatch);

    const name = document.createElement('span');
    name.className = 'fleet-name';
    row.appendChild(name);

    const sog = document.createElement('span');
    sog.className = 'fleet-sog';
    row.appendChild(sog);

    const dist = document.createElement('span');
    dist.className = 'fleet-dist';
    row.appendChild(dist);

    const spark = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    spark.setAttribute('class', 'fleet-spark sparkline');
    row.appendChild(spark);

    row.addEventListener('click', () => selectBoat(key));
    panel.appendChild(row);

    entry = {rowEl: row, swatchEl: swatch, nameEl: name, sogEl: sog, distEl: dist, sparkEl: spark};
    entry.swatchEl.style.background = color;
    entry.nameEl.textContent = displayName;
    fleetRows.set(key, entry);
    return entry;
}

function removeFleetRow(key) {
    const entry = fleetRows.get(key);
    if (!entry) return;
    entry.rowEl.remove();
    fleetRows.delete(key);
}

/** Recompute per-boat live SOG/distance and update the fleet panel DOM. */
function updateFleetPanel() {
    const panel = document.getElementById('fleet-panel');
    // Defensive: HTML may be stale (dev server didn't pick up the template
    // change) — bail rather than crash.
    if (!panel) return;
    if (!tracksData) { panel.hidden = true; return; }

    // Live keys (tracked AIS boats, plus ego if tracked). Anything in
    // fleetRows that isn't in `live` will be removed below.
    const live = new Set();

    // Ego sample is needed both for distance and (if ego.track) its own row.
    const egoSample = sampleAt(tracksData.ego_track || [], currentTime, 30);
    const latRef = egoSample ? egoSample.lat : (tracksData.ego_track && tracksData.ego_track[0]
        ? tracksData.ego_track[0][0] : 47.6);

    // Ego row first when BOTH Show and Track are on (mirrors sidebar).
    if (getEgoPrefs().show && getEgoPrefs().track && egoSample) {
        live.add(EGO_KEY);
        const egoName = (tracksData.ego && tracksData.ego.name) || 'Our boat';
        const entry = ensureFleetRow(EGO_KEY, `${egoName} (us)`, '#ffffff');
        if (entry) {
            const egoSog = getLogValue(currentTime, 'sog');
            entry.sogEl.textContent = (egoSog == null || Number.isNaN(egoSog))
                ? '--' : egoSog.toFixed(1);
            entry.distEl.textContent = '';   // self-distance is 0; suppress
            const hist = (logData ? getLogHistory(currentTime, 'sog', FLEET_SPARK_SEC) : [])
                .filter((v) => v != null && !Number.isNaN(v));
            if (hist.length >= 2) sparkline.sparkline(entry.sparkEl, hist);
            entry.rowEl.classList.toggle('selected', selectedBoatKey === EGO_KEY);
        }
    }

    for (const boat of tracksData.boats || []) {
        const mmsi = String(boat.mmsi);
        const prefs = getBoatPrefs(mmsi);
        // Require BOTH Show and Track: if Show is off the boat is hidden on
        // the map entirely, so showing its speed in the overlay is confusing
        // (you can't see where it is). Track alone is a stale opt-in that
        // accumulates as the user clicks boats over a session.
        if (!prefs.show || !prefs.track) continue;
        const sample = sampleAt(boat.track || [], currentTime, FLEET_MAX_GAP_S);
        if (!sample) continue;

        live.add(mmsi);
        const entry = ensureFleetRow(mmsi, boatName(boat), colorFor(mmsi));
        if (!entry) continue;
        const sog = sample.sog;
        entry.sogEl.textContent = (sog == null || Number.isNaN(sog))
            ? '--' : sog.toFixed(1);
        const dist = egoSample ? flatDistanceM(sample, egoSample, latRef) : null;
        entry.distEl.textContent = (dist == null) ? '' : `${Math.round(dist)} m`;
        const hist = historySogs(boat.track || [], currentTime, FLEET_SPARK_SEC);
        if (hist.length >= 2) sparkline.sparkline(entry.sparkEl, hist);
        entry.rowEl.classList.toggle('selected', selectedBoatKey === mmsi);
    }

    // Garbage-collect rows that are no longer live.
    for (const key of Array.from(fleetRows.keys())) {
        if (!live.has(key)) removeFleetRow(key);
    }

    panel.hidden = fleetRows.size === 0;
}


function updateMapForTime() {
    updateTimeUI();
    const egoCounts = updateEgoLayers();
    updateBoatLayers(egoCounts);
    updateInstrumentsForTime();
    updateFleetPanel();
    updateLadderLayers();
}


// ---- Ladder lines (wind-perpendicular rungs + signed gap labels) -------

/**
 * From TWD (degrees, "wind from" bearing), return the upwind unit vector
 * `u` (points toward the wind source) and a rung-direction unit vector
 * `p` perpendicular to `u`. All in local (east, north) meters.
 */
function windAxisVectors(twdDeg) {
    const r = twdDeg * Math.PI / 180.0;
    return {
        uE: Math.sin(r), uN: Math.cos(r),     // toward the wind source
        pE: Math.cos(r), pN: -Math.sin(r),    // perpendicular (rung direction)
    };
}

// Web Mercator: meters per pixel = (2π · R / 256 / 2^zoom) · cos(lat).
// Equivalent to the well-known 156543.03392 · cos(lat) / 2^zoom.
const _MPP_BASE = 156543.03392;
function metersPerPixel(latDeg, zoom) {
    return _MPP_BASE * Math.cos(latDeg * Math.PI / 180.0) / Math.pow(2, zoom);
}

/** Return [(lat+, lon+), (lat-, lon-)] endpoints of a rung centered at the boat. */
function ladderEndpoints(latDeg, lonDeg, pE, pN, halfLenM) {
    const latRad = latDeg * Math.PI / 180.0;
    const dLat = (halfLenM * pN) / 111320.0;
    const dLon = (halfLenM * pE) / (111320.0 * Math.cos(latRad));
    return [
        [latDeg + dLat, lonDeg + dLon],
        [latDeg - dLat, lonDeg - dLon],
    ];
}

/** Signed projection of (B - A) onto upwind unit vector. + = B upwind of A. */
function projectedGapM(aLat, aLon, bLat, bLon, uE, uN) {
    const R = 6371000.0;
    const latRefRad = ((aLat + bLat) * 0.5) * Math.PI / 180.0;
    const dE = (bLon - aLon) * Math.PI / 180.0 * R * Math.cos(latRefRad);
    const dN = (bLat - aLat) * Math.PI / 180.0 * R;
    return dE * uE + dN * uN;
}

function clearLadderLayers() {
    for (const [, entry] of ladderLayers) {
        if (entry.line) entry.line.remove();
        if (entry.label) entry.label.remove();
    }
    ladderLayers.clear();
}

function ensureLadderEntry(key, color) {
    let entry = ladderLayers.get(key);
    if (entry) return entry;
    const L = leaf();
    entry = {
        line: L.polyline([[0, 0], [0, 0]], {
            color,
            weight: 2,
            opacity: 0.85,
            dashArray: '4,4',
            interactive: false,
        }).addTo(map),
        label: null,
    };
    ladderLayers.set(key, entry);
    return entry;
}

function ensureLadderLabel(entry) {
    if (entry.label) return entry.label;
    const L = leaf();
    entry.label = L.marker([0, 0], {
        icon: L.divIcon({
            className: 'ladder-label',
            html: '',
            // Sized loosely; CSS lets text overflow. iconAnchor offsets so
            // the label floats up-right of the rung endpoint we anchor it to.
            iconSize: [50, 14],
            iconAnchor: [-6, 18],
        }),
        interactive: false,
        keyboard: false,
    }).addTo(map);
    return entry.label;
}

function removeLadderEntry(key) {
    const entry = ladderLayers.get(key);
    if (!entry) return;
    if (entry.line) entry.line.remove();
    if (entry.label) entry.label.remove();
    ladderLayers.delete(key);
}

/**
 * Per-frame: draw a wind-perpendicular rung through ego and through every
 * (show && track) AIS boat. Non-ego rungs carry a label of the signed
 * upwind gap to ego (+ = boat is upwind, − = downwind). Hidden entirely
 * when the toggle is off or TWD isn't available at currentTime.
 */
function updateLadderLayers() {
    if (!ladderOn || !tracksData || !map) {
        if (ladderLayers.size > 0) clearLadderLayers();
        return;
    }
    const twdDeg = getLogValue(currentTime, 'twd');
    if (twdDeg == null || Number.isNaN(twdDeg)) {
        if (ladderLayers.size > 0) clearLadderLayers();
        return;
    }
    const {uE, uN, pE, pN} = windAxisVectors(twdDeg);

    const egoPrefs = getEgoPrefs();
    const egoSample = (egoPrefs.show && egoPrefs.track)
        ? sampleAt(tracksData.ego_track || [], currentTime, INSTR_MAX_GAP_S)
        : null;

    const zoom = map.getZoom();
    const live = new Set();

    const placeRung = (key, lat, lon, color, gapText) => {
        const mpp = metersPerPixel(lat, zoom);
        const halfLen = Math.min(LADDER_MAX_M, LADDER_MAX_PX * mpp) / 2;
        const [p1, p2] = ladderEndpoints(lat, lon, pE, pN, halfLen);
        const entry = ensureLadderEntry(key, color);
        entry.line.setLatLngs([p1, p2]);
        entry.line.setStyle({color});
        if (gapText != null) {
            const label = ensureLadderLabel(entry);
            label.setLatLng(p1);
            const el = label.getElement();
            if (el) el.textContent = gapText;
        } else if (entry.label) {
            entry.label.remove();
            entry.label = null;
        }
        live.add(key);
    };

    if (egoSample) {
        placeRung(EGO_KEY, egoSample.lat, egoSample.lon, '#ffffff', null);
    }

    for (const boat of tracksData.boats || []) {
        const mmsi = String(boat.mmsi);
        const prefs = getBoatPrefs(mmsi);
        if (!prefs.show || !prefs.track) continue;
        const sample = sampleAt(boat.track || [], currentTime, FLEET_MAX_GAP_S);
        if (!sample) continue;
        let gapText = null;
        if (egoSample) {
            const gapM = projectedGapM(
                egoSample.lat, egoSample.lon,
                sample.lat, sample.lon,
                uE, uN,
            );
            const sign = gapM >= 0 ? '+' : '−';
            gapText = `${sign}${Math.abs(Math.round(gapM))} m`;
        }
        placeRung(mmsi, sample.lat, sample.lon, colorFor(mmsi), gapText);
    }

    for (const key of Array.from(ladderLayers.keys())) {
        if (!live.has(key)) removeLadderEntry(key);
    }
}

function selectBoat(id) {
    selectedBoatKey = String(id);
    const prefs = selectedBoatKey === EGO_KEY ? getEgoPrefs() : getBoatPrefs(selectedBoatKey);
    prefs.show = true;
    prefs.track = true;
    saveBoatState();
    renderBoatList();
    updateMapForTime();
}

function filterText(boat) {
    return [
        boatName(boat), boat.name, boat.mmsi, boat.callsign,
        (boat.sail_numbers || []).join(' '), (boat.fleet_tags || []).join(' '),
    ].join(' ').toLowerCase();
}

function filteredBoats() {
    const term = document.getElementById('boat-search').value.trim().toLowerCase();
    const j105Only = document.getElementById('filter-j105').checked;
    const namedOnly = document.getElementById('filter-named').checked;
    const selectedOnly = document.getElementById('filter-selected').checked;
    const sortMode = document.getElementById('sort-boats').value;
    let boats = (tracksData.boats || []).filter((boat) => {
        if (term && !filterText(boat).includes(term)) return false;
        if (j105Only && !isFleetBoat(boat)) return false;
        if (namedOnly && !(boat.name || '').trim()) return false;
        if (selectedOnly && !getBoatPrefs(boat.mmsi).show && selectedBoatKey !== String(boat.mmsi)) return false;
        return true;
    });

    const nameKey = (boat) => boatName(boat).toUpperCase();
    if (sortMode === 'distance') {
        boats.sort((a, b) => a.min_dist_m - b.min_dist_m || nameKey(a).localeCompare(nameKey(b)));
    } else if (sortMode === 'name') {
        boats.sort((a, b) => nameKey(a).localeCompare(nameKey(b)));
    } else if (sortMode === 'points') {
        boats.sort((a, b) => (b.track || []).length - (a.track || []).length || nameKey(a).localeCompare(nameKey(b)));
    } else {
        boats.sort((a, b) => Number(isFleetBoat(b)) - Number(isFleetBoat(a))
            || a.min_dist_m - b.min_dist_m
            || nameKey(a).localeCompare(nameKey(b)));
    }
    return boats;
}

function appendBoatRow(frag, {id, name, meta, color, prefs, rowClass = ''}) {
    const row = document.createElement('div');
    const selectedClass = String(id) === selectedBoatKey ? 'selected' : '';
    row.className = `boat-row ${rowClass} ${selectedClass}`.trim();

    const main = document.createElement('div');
    main.className = 'boat-main';

    const title = document.createElement('div');
    title.className = 'boat-title';
    const swatch = document.createElement('span');
    swatch.className = 'boat-swatch';
    swatch.style.background = color;
    const nameEl = document.createElement('span');
    nameEl.className = 'boat-name';
    nameEl.textContent = name;
    title.appendChild(swatch);
    title.appendChild(nameEl);
    if (rowClass.includes('fleet')) {
        const badge = document.createElement('span');
        badge.className = 'fleet-badge';
        badge.textContent = 'J/105';
        title.appendChild(badge);
    }

    const metaEl = document.createElement('div');
    metaEl.className = 'boat-meta';
    metaEl.textContent = meta;
    main.appendChild(title);
    main.appendChild(metaEl);

    row.addEventListener('click', (e) => {
        if (e.target.closest('input') || e.target.closest('label')) return;
        selectBoat(id);
    });

    const showLabel = document.createElement('label');
    showLabel.className = 'boat-toggle';
    const showInput = document.createElement('input');
    showInput.type = 'checkbox';
    showInput.checked = prefs.show;
    showInput.addEventListener('change', () => {
        prefs.show = showInput.checked;
        trackInput.disabled = !prefs.show;
        saveBoatState();
        updateMapForTime();
    });
    showLabel.appendChild(showInput);
    showLabel.append('Show');

    const trackLabel = document.createElement('label');
    trackLabel.className = 'boat-toggle';
    const trackInput = document.createElement('input');
    trackInput.type = 'checkbox';
    trackInput.checked = prefs.track;
    trackInput.disabled = !prefs.show;
    trackInput.addEventListener('change', () => {
        prefs.track = trackInput.checked;
        saveBoatState();
        updateMapForTime();
    });
    trackLabel.appendChild(trackInput);
    trackLabel.append('Track');

    row.dataset.id = id;
    row.appendChild(main);
    row.appendChild(showLabel);
    row.appendChild(trackLabel);
    frag.appendChild(row);
}

function renderBoatList() {
    const list = document.getElementById('boat-list');
    list.innerHTML = '';
    const boats = filteredBoats();

    const frag = document.createDocumentFragment();
    const ego = tracksData.ego || {name: 'Our boat'};
    appendBoatRow(frag, {
        id: EGO_KEY,
        name: `${ego.name || 'Our boat'} (us)`,
        meta: `${(ego.sail_numbers || []).length ? `Sail ${(ego.sail_numbers || []).join('/')} | ` : ''}${(tracksData.ego_track || []).length} ego pts | ${ego.mmsi || 'ego track'}`,
        color: '#ffffff',
        prefs: getEgoPrefs(),
        rowClass: 'ego',
    });

    for (const boat of boats) {
        const mmsi = String(boat.mmsi);
        const actual = boat.name && boat.race_name && boat.name !== boat.race_name ? `AIS ${boat.name} | ` : '';
        const sail = (boat.sail_numbers || []).join('/');
        appendBoatRow(frag, {
            id: mmsi,
            name: boatName(boat),
            meta: `${sail ? `Sail ${sail} | ` : ''}${actual}${Math.round(boat.min_dist_m)} m min | ${boat.track.length} pts | ${mmsi}`,
            color: colorFor(mmsi),
            prefs: getBoatPrefs(mmsi),
            rowClass: isFleetBoat(boat) ? 'fleet' : '',
        });
    }

    if (boats.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'boat-row';
        empty.textContent = 'No boats match the filters.';
        frag.appendChild(empty);
    }
    list.appendChild(frag);
}

function updateStats() {
    const s = tracksData.summary || {};
    document.getElementById('stats').textContent =
        `${tracksData.date}: ${s.boats_kept} nearby boats, ${s.fleet_tagged} J/105 tags, ${s.kept_positions} AIS points`;
    document.getElementById('subtitle').textContent = 'Nearby AIS tracks with J/105 filtering';
}

function setAllPrefs(patch) {
    Object.assign(getEgoPrefs(), patch);
    for (const boat of filteredBoats()) {
        Object.assign(getBoatPrefs(boat.mmsi), patch);
    }
    saveBoatState();
    renderBoatList();
    updateMapForTime();
}

// `shareParams` (a URLSearchParams) is passed only on the initial page load
// from a shared link; manual date loads (the Load button) pass null so they
// start from clean defaults instead of inheriting a previous link's state.
async function loadDate(date, shareParams = null) {
    document.getElementById('index-view').hidden = true;
    document.getElementById('day-view').hidden = false;
    document.getElementById('stats').textContent = 'Loading...';
    document.getElementById('date-input').value = date;
    // Stop any in-flight playback before swapping out the day's data so the
    // RAF loop doesn't briefly advance time against the next day's bounds.
    stopPlay();
    speedIdx = 0;
    setSpeedBtnLabel();
    const path = `Analyze/AISDays/${date}.json`;
    tracksData = await dataGetJSON(path);
    loadBoatState(tracksData.date, tracksData.boats || []);
    applyShareBoatState(shareParams);
    selectedBoatKey = EGO_KEY;

    // Scrubber time: from the link's `t` (clamped) or the day's start.
    currentTime = tracksData.time_start;
    if (shareParams) {
        const t = shareParams.get('t');
        if (t != null && t !== '' && Number.isFinite(Number(t))) {
            currentTime = clamp(tracksData.time_start + Number(t),
                tracksData.time_start, tracksData.time_end);
        }
    }

    initMap();
    const range = document.getElementById('time-range');
    range.min = 0;
    range.max = Math.max(0, Math.round(tracksData.time_end - tracksData.time_start));
    range.value = Math.round(currentTime - tracksData.time_start);
    document.getElementById('time-end-readout').textContent = formatTime(tracksData.time_end);

    // Build the instrument panel before loading the .log so the rows appear
    // even on days without instruments — they just show '--'.
    buildInstrumentPanel();
    await loadDayLog(date, tracksData.log_file, tracksData.columns);

    updateStats();
    renderStaticMap();
    // renderStaticMap auto-fits to the tracks; a shared link's explicit
    // center+zoom must override that, so apply it afterwards.
    applyShareView(shareParams);
    renderBoatList();
    updateMapForTime();
    // Preserve the full shared params in the URL so a reload reproduces the
    // same view; otherwise fall back to the canonical date-only URL.
    const canonical = shareParams ? `?${shareParams.toString()}` : `?date=${encodeURIComponent(date)}`;
    history.replaceState(null, '', canonical);
}

function renderIndex(index) {
    document.getElementById('day-view').hidden = true;
    document.getElementById('index-view').hidden = false;
    document.getElementById('subtitle').textContent = 'Sailing days with nearby AIS';
    document.getElementById('stats').textContent = `${(index.days || []).length} AIS days`;
    const list = document.getElementById('day-list');
    list.innerHTML = '';
    const frag = document.createDocumentFragment();
    for (const day of index.days || []) {
        const a = document.createElement('a');
        a.className = 'day-card';
        a.href = `tracks.html?date=${encodeURIComponent(day.date)}`;
        const main = document.createElement('div');
        const title = document.createElement('div');
        title.className = 'day-title';
        title.textContent = day.date;
        const meta = document.createElement('div');
        meta.className = 'day-meta';
        meta.textContent = `${formatTime(day.time_start)} -> ${formatTime(day.time_end)} | ${day.source_logs.join(', ')}`;
        main.appendChild(title);
        main.appendChild(meta);
        const counts = document.createElement('div');
        counts.className = 'day-counts';
        counts.textContent = `${day.boats_kept} boats\n${day.fleet_tagged} J/105`;
        a.appendChild(main);
        a.appendChild(counts);
        frag.appendChild(a);
    }
    if ((index.days || []).length === 0) {
        const empty = document.createElement('p');
        empty.textContent = 'No AIS day payloads have been generated yet.';
        frag.appendChild(empty);
    }
    list.appendChild(frag);
}

async function loadIndex() {
    const index = await dataGetJSON('Analyze/AISDays/index.json');
    renderIndex(index);
    history.replaceState(null, '', 'tracks.html');
}

// ---- Shareable links ---------------------------------------------------
// Pressing `c` copies a URL that reproduces the current view: date, scrubber
// time, map center+zoom, and the exact set of shown/tracked boats. On load,
// boot() passes the URL's params back into loadDate(), which applies them.

let toastTimer = null;
function showToast(msg) {
    let el = document.getElementById('tracks-toast');
    if (!el) {
        el = document.createElement('div');
        el.id = 'tracks-toast';
        el.style.cssText = 'position:fixed;bottom:84px;left:50%;transform:translateX(-50%);'
            + 'background:rgba(5,7,10,.92);color:#fff;padding:8px 16px;border-radius:6px;'
            + 'font:13px/1.4 -apple-system,Helvetica,sans-serif;z-index:99999;'
            + 'pointer-events:none;opacity:0;transition:opacity .2s;';
        document.body.appendChild(el);
    }
    el.textContent = msg;
    el.style.opacity = '1';
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.style.opacity = '0'; }, 1800);
}

/**
 * Build a share URL for the current view. Boat state is encoded as
 * deviations from the show-all default: `track` lists tracked keys and
 * `hide` lists hidden keys (both small in the common case). The pair fully
 * determines state on load regardless of the page's evolving defaults.
 */
function buildShareUrl() {
    const params = new URLSearchParams();
    params.set('date', tracksData.date);
    params.set('t', String(Math.round(currentTime - tracksData.time_start)));
    if (map) {
        const c = map.getCenter();
        params.set('ll', `${c.lat.toFixed(5)},${c.lng.toFixed(5)}`);
        params.set('z', String(map.getZoom()));
    }
    const tracked = [];
    const hidden = [];
    boatState.forEach((v, k) => {
        if (v.track) tracked.push(k);
        if (!v.show) hidden.push(k);
    });
    if (tracked.length) params.set('track', tracked.join(','));
    if (hidden.length) params.set('hide', hidden.join(','));
    return `${location.origin}${location.pathname}?${params.toString()}`;
}

function copyShareLink() {
    if (!tracksData) return;
    const url = buildShareUrl();
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(url).then(
            () => showToast('Link copied to clipboard'),
            () => window.prompt('Copy this link:', url),
        );
    } else {
        window.prompt('Copy this link:', url);
    }
}

/** Override show/track from a share link's `track`/`hide` lists, if present. */
function applyShareBoatState(shareParams) {
    if (!shareParams) return;
    const track = shareParams.get('track');
    const hide = shareParams.get('hide');
    if (track === null && hide === null) return;
    const trackSet = new Set((track || '').split(',').filter(Boolean));
    const hideSet = new Set((hide || '').split(',').filter(Boolean));
    boatState.forEach((v, k) => {
        v.show = !hideSet.has(k);
        v.track = trackSet.has(k);
    });
}

/** Apply a share link's `ll` (center) + `z` (zoom), overriding the auto-fit. */
function applyShareView(shareParams) {
    if (!shareParams || !map) return;
    const ll = shareParams.get('ll');
    const z = shareParams.get('z');
    if (ll) {
        const [la, lo] = ll.split(',').map(Number);
        if (Number.isFinite(la) && Number.isFinite(lo)) {
            const zoom = z != null && z !== '' ? Number(z) : map.getZoom();
            map.setView([la, lo], zoom);
            return;
        }
    }
    if (z != null && z !== '' && Number.isFinite(Number(z))) map.setZoom(Number(z));
}

/**
 * Open the sailview video viewer at the current scrubber instant. sailview
 * resolves `utc` (Unix seconds) to the clip covering that moment + the in-clip
 * offset (see resolveVideoForUtc there). Opens in a new tab so the map stays
 * put; if no clip covers the instant, sailview lands on the nearest one.
 */
function openVideoAtTime() {
    if (!tracksData || currentTime == null) return;
    const url = `sailview.html?utc=${Math.round(currentTime)}`;
    window.open(url, '_blank', 'noopener');
}

function bindControls() {
    document.getElementById('load-btn').addEventListener('click', () => {
        loadDate(document.getElementById('date-input').value).catch((err) => {
            utils.surfaceError(`AIS load failed: ${err.message}`, {key: 'tracks-load'});
            document.getElementById('stats').textContent = 'Load failed';
        });
    });
    for (const id of ['boat-search', 'filter-j105', 'filter-named', 'filter-selected', 'sort-boats']) {
        document.getElementById(id).addEventListener('input', renderBoatList);
        document.getElementById(id).addEventListener('change', renderBoatList);
    }
    document.getElementById('show-all').addEventListener('click', () => setAllPrefs({show: true}));
    document.getElementById('show-none').addEventListener('click', () => setAllPrefs({show: false}));
    document.getElementById('tracks-all').addEventListener('click', () => setAllPrefs({show: true, track: true}));
    document.getElementById('tracks-none').addEventListener('click', () => setAllPrefs({track: false}));

    const range = document.getElementById('time-range');
    range.addEventListener('input', (e) => {
        if (!tracksData) return;
        currentTime = tracksData.time_start + Number(e.target.value);
        updateMapForTime();
    });
    // Auto-pause when the user grabs the scrubber. Matches sailview /
    // standard media-player behavior; the user picked this in plan-mode Q.
    range.addEventListener('pointerdown', () => {
        if (isPlaying) stopPlay();
    });

    document.getElementById('play-btn').addEventListener('click', togglePlay);
    document.getElementById('skip-back-btn').addEventListener('click', () => skipSeconds(-10));
    document.getElementById('skip-fwd-btn').addEventListener('click', () => skipSeconds(+10));
    document.getElementById('speed-btn').addEventListener('click', cycleSpeed);

    const trackLength = document.getElementById('track-length');
    const trackLengthValue = document.getElementById('track-length-value');
    if (trackLength) {
        try {
            const saved = parseInt(localStorage.getItem(TRACK_LENGTH_KEY), 10);
            if (Number.isFinite(saved)) {
                trackHistorySec = clamp(saved, TRACK_LENGTH_MIN_SEC, TRACK_LENGTH_MAX_SEC);
            }
        } catch (_) { /* default stands */ }
        trackLength.min = String(TRACK_LENGTH_MIN_SEC);
        trackLength.max = String(TRACK_LENGTH_MAX_SEC);
        trackLength.step = String(TRACK_LENGTH_STEP_SEC);
        trackLength.value = String(trackHistorySec);
        const renderLabel = () => {
            if (trackLengthValue) trackLengthValue.textContent = formatTrackLength(trackHistorySec);
        };
        renderLabel();
        trackLength.addEventListener('input', () => {
            trackHistorySec = clamp(Number(trackLength.value),
                TRACK_LENGTH_MIN_SEC, TRACK_LENGTH_MAX_SEC);
            renderLabel();
            try { localStorage.setItem(TRACK_LENGTH_KEY, String(trackHistorySec)); } catch (_) {}
            if (tracksData) updateMapForTime();
        });
    }

    const ladderToggle = document.getElementById('ladder-toggle');
    if (ladderToggle) {
        try {
            ladderOn = localStorage.getItem(LADDER_TOGGLE_KEY) === '1';
        } catch (_) { ladderOn = false; }
        ladderToggle.checked = ladderOn;
        ladderToggle.addEventListener('change', () => {
            ladderOn = ladderToggle.checked;
            try { localStorage.setItem(LADDER_TOGGLE_KEY, ladderOn ? '1' : '0'); } catch (_) {}
            updateLadderLayers();
        });
    }

    document.addEventListener('keydown', (e) => {
        // Ignore when the user is typing in an input/select (e.g. the boat
        // search box) — otherwise space/arrows interfere with text editing.
        const t = e.target;
        if (t && (t.tagName === 'INPUT' || t.tagName === 'SELECT' || t.tagName === 'TEXTAREA')) {
            return;
        }
        if (!tracksData) return;
        if (e.code === 'Space') {
            togglePlay();
            e.preventDefault();
        } else if (e.code === 'ArrowLeft') {
            skipSeconds(-10);
            e.preventDefault();
        } else if (e.code === 'ArrowRight') {
            skipSeconds(+10);
            e.preventDefault();
        } else if (e.code === 'KeyC' && !e.metaKey && !e.ctrlKey) {
            // Plain `c` only — leave Cmd/Ctrl+C for normal text copy.
            copyShareLink();
            e.preventDefault();
        } else if (e.code === 'KeyV' && !e.metaKey && !e.ctrlKey) {
            // Plain `v` — open the sailview video viewer at this instant.
            openVideoAtTime();
            e.preventDefault();
        }
    });
}

async function boot() {
    if (!shared.checkAuthorization()) {
        utils.redirectWithParams('login.html', window.location.pathname);
        return;
    }
    if (!leaf()) {
        utils.surfaceError('Leaflet failed to load; AIS map cannot start.', {key: 'leaflet'});
        return;
    }

    bindControls();
    const params = new URLSearchParams(window.location.search);
    const date = params.get('date');
    try {
        if (date) await loadDate(date, params);
        else await loadIndex();
    } catch (err) {
        utils.surfaceError(`AIS tracks load failed: ${err.message}`, {key: 'tracks-load'});
        document.getElementById('stats').textContent = 'Load failed';
    }
}

document.addEventListener('DOMContentLoaded', boot);
