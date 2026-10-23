/**
 * label.js — streamlined map-only labeling tool for race starts.
 *
 * Reads /Analyze/events_index.json (same file as review.html, filtered to
 * type==='start') and lazy-fetches per-event detail from
 * /Analyze/Events/<id>.json. Persists labels to /Analyze/events_edits.json.
 * Extends the edits schema with:
 *   - line_override: { pin_ll, committee_ll }
 *   - marks: { windward_ll, leeward_ll }
 * These are ignored by older clients (review.js reads only status/notes/trim).
 * Each edit carries label_schema_version: 2; the document stays version 1
 * (the Lambda's SAVE_JSON schema in Lambda/src/documents.py).
 */
import '../css/label.css';
import * as shared from './shared';
import {reviewStatus} from './security';
import * as utils from './utils';
import * as viewer360 from './viewer360';

// ---- State ----------------------------------------------------------------
let startsData = { starts: [] };
let editsData = { version: 1, edits: {} };
let selectedId = null;
let saveTimer = null;

// Per-event detail cache (mirrors review.js). The event index gives us the
// preview track and summary; the heavy track / inferred line / features come
// from Analyze/Events/<id>.json on first selection.
const detailCache = new Map();
const detailPending = new Map();

let map = null;
// Viewer + video-sync state. The video is slaved to the scrubber in both
// directions: dragging the scrubber seeks the video; video.timeupdate updates
// the scrubber and the boat marker on the map.
let viewer = null;
let videosJson = null;
let videosColumnsMap = null;
let videosFetching = false;
let currentVideoBasename = null;
let currentVideoRecord = null;
let syncingFromVideo = false;  // re-entrancy guard for scrubber ↔ video

// All overlay layers for the selected candidate. Single-valued layers use a
// named slot; the track is a list because it renders as two colored segments.
const layers = {
    tracks: [],           // L.Polyline[]  — all rendered track segments
    inferredLine: null,   // L.Polyline
    overrideLine: null,   // L.Polyline
    pin: null,            // L.Marker (draggable)
    committee: null,      // L.Marker (draggable)
    windward: null,       // L.Marker (draggable)
    leeward: null,        // L.Marker (draggable)
    boat: null,           // L.CircleMarker
    windArrow: null,      // L.Polyline
};

const LABEL_SCHEMA_VERSION = 2;

// Colors (must match label.css variables conceptually)
const COLOR = {
    inferred: '#8b949e',
    override: '#f0883e',
    windward: '#f85149',
    leeward: '#3fb950',
    // Match review.html: prestart leg in red, post-start beat in blue.
    track_pre: '#d73a49',
    track_post: '#2188ff',
    boat: '#f0883e',
    wind: '#d29922',
};

// ---- Boot -----------------------------------------------------------------
function fetchJSON(wwwPath) {
    // Saves need the ETag that only the validated document read records.
    if (wwwPath === 'Analyze/events_edits.json') return shared.readDataJSON(wwwPath);
    return new Promise((resolve, reject) => {
        utils.getJSON(shared.dataGetUrl(wwwPath), (err, data) => {
            if (err) reject(new Error(`fetch ${wwwPath} failed: ${err}`));
            else resolve(data);
        });
    });
}

/**
 * Project an events_index.json entry (type==='start') into the legacy
 * start-shaped object label.js was written against. Mirrors the helper in
 * review.js — kept duplicated rather than shared so each entry point can
 * evolve independently if start labelling diverges from review.
 */
function asStartView(ev) {
    return {
        id: ev.id,
        type: ev.type,
        date: ev.date,
        file: ev.file,
        race_id: ev.race_id || null,
        label: ev.label || null,
        status: ev.status || 'pending',
        notes: ev.notes || '',
        confidence: ev.confidence,
        start_time_unix: ev.t_key_unix,
        window_begin: ev.t_start_unix,
        window_end: ev.t_end_unix,
        first_beat_end_unix: ev.t_end_unix,
        // ``track`` is overwritten by the full per-event track once detail
        // loads; until then the preview keeps the map non-empty.
        track: ev.preview_track || [],
    };
}

function ensureDetail(id) {
    if (detailCache.has(id)) return Promise.resolve(detailCache.get(id));
    if (detailPending.has(id)) return detailPending.get(id);
    const p = fetchJSON(`Analyze/Events/${id}.json`)
        .then((detail) => {
            detailCache.set(id, detail);
            detailPending.delete(id);
            const s = (startsData.starts || []).find(x => x.id === id);
            if (s && detail) Object.assign(s, detail);
            return detail;
        })
        .catch((err) => {
            detailPending.delete(id);
            console.error(`detail load failed for ${id}:`, err);
            return null;
        });
    detailPending.set(id, p);
    return p;
}

async function boot() {
    if (!shared.checkAuthorization()) {
        utils.redirectWithParams('login.html', window.location.pathname);
        return;
    }

    try {
        const eventsDoc = await fetchJSON('Analyze/events_index.json');
        const events = (eventsDoc && eventsDoc.events) || [];
        startsData = {
            starts: events
                .filter(e => e.type === 'start')
                .map(asStartView),
        };
    } catch (e) {
        document.getElementById('map-empty').textContent =
            `Could not load events_index.json: ${e.message}. Run Analyze/find_starts.py first.`;
        return;
    }

    try {
        const edits = await fetchJSON('Analyze/events_edits.json');
        if (edits && edits.edits) {
            editsData = edits;
            editsData.version = 1;
        }
    } catch (e) {
        // ok on first run
    }

    applyEdits();
    wireControls();
    renderStats();
    renderList();

    // Respect ?start=<id> for deep-linking.
    const urlStart = new URLSearchParams(window.location.search).get('start');
    if (urlStart && startsData.starts.some(s => s.id === urlStart)) {
        selectStart(urlStart);
    }
}

function applyEdits() {
    const edits = editsData.edits || {};
    (startsData.starts || []).forEach(s => {
        const e = edits[s.id];
        if (!e) return;
        if (e.status != null) s.status = reviewStatus(e.status);
        if (e.line_override != null) s.line_override = e.line_override;
        if (e.marks != null) s.marks = e.marks;
    });
}

function upsertEdit(id, patch) {
    if (!editsData.edits) editsData.edits = {};
    const prev = editsData.edits[id] || {};
    const merged = {
        ...prev,
        ...patch,
        label_schema_version: LABEL_SCHEMA_VERSION,
        updated_ts: Math.floor(Date.now() / 1000),
    };
    // Legacy field from the cookie-login era; the Lambda logs the real editor.
    delete merged.updated_by;
    editsData.edits[id] = merged;
    editsData.updated_ts = merged.updated_ts;
    scheduleSave();
}

function scheduleSave() {
    setSaveStatus('saving');
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(saveNow, 500);
}

function saveNow() {
    saveTimer = null;
    shared.putDataJSON('Analyze/events_edits.json', editsData, (err) => {
        setSaveStatus(err ? 'error' : 'saved');
    });
}

function setSaveStatus(state) {
    const el = document.getElementById('save-status');
    el.classList.remove('saving', 'error');
    if (state === 'saving') { el.classList.add('saving'); el.textContent = 'saving…'; }
    else if (state === 'error') { el.classList.add('error'); el.textContent = 'save failed'; }
    else el.textContent = 'saved';
}

// ---- Controls / keyboard --------------------------------------------------
function wireControls() {
    document.getElementById('filter-status').addEventListener('change', () => {
        renderList();
    });
    document.getElementById('sort-by').addEventListener('change', () => {
        renderList();
    });
    document.getElementById('btn-confirm').addEventListener('click', () => setStatus('confirmed'));
    document.getElementById('btn-discard').addEventListener('click', () => setStatus('discarded'));
    document.getElementById('btn-pending').addEventListener('click', () => setStatus('pending'));

    document.getElementById('scrubber').addEventListener('input', onScrub);
    document.getElementById('play-btn').addEventListener('click', togglePlay);

    document.addEventListener('keydown', onKey);
    window.addEventListener('beforeunload', (e) => {
        if (saveTimer) { saveNow(); }
    });
}

// ---- Playback ------------------------------------------------------------
const FRAME_DURATION_S = 1 / 30;   // ~33 ms, one frame at 30 fps
const SKIP_S = 5;                  // Up/Down skip window

function togglePlay() {
    if (!viewer) return;
    const v = viewer.video;
    if (v.paused || v.ended) {
        v.play().catch(e => console.warn('[label] play() rejected', e));
    } else {
        v.pause();
    }
}

function syncPlayBtn() {
    const btn = document.getElementById('play-btn');
    if (!btn || !viewer) return;
    const playing = !viewer.video.paused && !viewer.video.ended;
    btn.textContent = playing ? '❚❚' : '▶';
    btn.classList.toggle('playing', playing);
}

function setPlaybackRate(multFn) {
    if (!viewer) return;
    const cur = viewer.video.playbackRate || 1;
    const next = Math.max(0.25, Math.min(4, multFn(cur)));
    viewer.video.playbackRate = next;
    const el = document.getElementById('playback-rate');
    if (el) el.textContent = `${next.toFixed(2)}×`;
}

function onKey(ev) {
    if (ev.target && (ev.target.tagName === 'INPUT' || ev.target.tagName === 'TEXTAREA' || ev.target.tagName === 'SELECT')) return;
    // Playback keys don't require a selected candidate beyond the viewer
    // existing, but all other keys do. j/k navigate the list without a
    // selection so they're handled first.
    const key = ev.key;  // preserve Space, Arrow keys, etc. in their raw form
    const lower = key.toLowerCase();

    // --- Playback ---------------------------------------------------------
    if (key === ' ') { ev.preventDefault(); togglePlay(); return; }
    if (key === 'ArrowLeft') {
        if (!viewer) return;
        ev.preventDefault();
        viewer.video.pause();
        viewer.video.currentTime = Math.max(0, viewer.video.currentTime - FRAME_DURATION_S);
        return;
    }
    if (key === 'ArrowRight') {
        if (!viewer) return;
        ev.preventDefault();
        viewer.video.pause();
        const dur = viewer.video.duration || Infinity;
        viewer.video.currentTime = Math.min(dur, viewer.video.currentTime + FRAME_DURATION_S);
        return;
    }
    if (key === 'ArrowDown') {
        if (!viewer) return;
        ev.preventDefault();
        viewer.video.currentTime = Math.max(0, viewer.video.currentTime - SKIP_S);
        return;
    }
    if (key === 'ArrowUp') {
        if (!viewer) return;
        ev.preventDefault();
        const dur = viewer.video.duration || Infinity;
        viewer.video.currentTime = Math.min(dur, viewer.video.currentTime + SKIP_S);
        return;
    }
    if (lower === 'd' && !ev.shiftKey) {
        if (!viewer) return;
        ev.preventDefault();
        setPlaybackRate(r => r * 1.1);
        return;
    }
    if (lower === 's' && !ev.shiftKey) {
        if (!viewer) return;
        ev.preventDefault();
        setPlaybackRate(r => r * 0.9);
        return;
    }

    // --- Labeling ---------------------------------------------------------
    if (!selectedId && !['j', 'k'].includes(lower)) return;
    switch (lower) {
        case 'c':
            ev.preventDefault(); setStatus('confirmed'); break;
        case 'x':
            ev.preventDefault(); setStatus('discarded'); break;
        case 'j':
            ev.preventDefault(); step(+1); break;
        case 'k':
            ev.preventDefault(); step(-1); break;
        case 'p':
            ev.preventDefault(); dropEndpointAtCursor('pin'); break;
        case 'o':   // 'c' is already confirm; use 'o' as committee alias
            ev.preventDefault(); dropEndpointAtCursor('committee'); break;
        case 'w':
            ev.preventDefault();
            if (ev.shiftKey) clearMark('windward'); else dropMarkAtCursor('windward');
            break;
        case 'l':
            ev.preventDefault();
            if (ev.shiftKey) clearMark('leeward'); else dropMarkAtCursor('leeward');
            break;
        case 'u':
            ev.preventDefault(); copyInferredToOverride(); break;
        case 'r':
            ev.preventDefault();
            if (ev.shiftKey) clearMarks(); else clearOverride();
            break;
    }
}

function step(delta) {
    const visible = getFilteredSorted();
    if (!visible.length) return;
    let i = selectedId ? visible.findIndex(s => s.id === selectedId) : -1;
    i = Math.max(0, Math.min(visible.length - 1, i + delta));
    selectStart(visible[i].id);
}

// ---- List rendering -------------------------------------------------------
function getFilteredSorted() {
    const status = document.getElementById('filter-status').value;
    const sort = document.getElementById('sort-by').value;
    let arr = (startsData.starts || []).slice();
    if (status !== 'all') arr = arr.filter(s => s.status === status);
    arr.sort((a, b) => {
        if (sort === 'confidence') return (b.confidence || 0) - (a.confidence || 0);
        return b.start_time_unix - a.start_time_unix;
    });
    return arr;
}

function renderStats() {
    const s = startsData.starts || [];
    const c = s.filter(x => x.status === 'confirmed').length;
    const p = s.filter(x => x.status === 'pending').length;
    const d = s.filter(x => x.status === 'discarded').length;
    document.getElementById('stat-total').textContent = s.length;
    document.getElementById('stat-confirmed').textContent = c;
    document.getElementById('stat-pending').textContent = p;
    document.getElementById('stat-discarded').textContent = d;
}

const LOCAL_TZ = 'America/Los_Angeles';
const _dayFmt = new Intl.DateTimeFormat('en-CA',
    { timeZone: LOCAL_TZ, year: 'numeric', month: '2-digit', day: '2-digit' });
const _timeFmt = new Intl.DateTimeFormat('en-GB',
    { timeZone: LOCAL_TZ, hour: '2-digit', minute: '2-digit', hour12: false });

function localDateTime(unixSec) {
    if (unixSec == null || !Number.isFinite(unixSec)) return '';
    const d = new Date(unixSec * 1000);
    return `${_dayFmt.format(d)} ${_timeFmt.format(d)}`;  // YYYY-MM-DD HH:MM, Pacific
}

function renderList() {
    const container = document.getElementById('start-list');
    container.innerHTML = '';
    const visible = getFilteredSorted();
    visible.forEach(s => {
        const card = document.createElement('div');
        card.className = 'start-card' + (s.id === selectedId ? ' selected' : '');
        card.dataset.id = s.id;
        const status = reviewStatus(s.status);
        const conf = s.confidence != null ? Math.round(s.confidence * 100) + '%' : '—';
        const local = localDateTime(s.start_time_unix);
        card.innerHTML = `
            <div class="card-main">
              <div class="card-local">${local}</div>
              <div class="card-id">${utils.htmlEncode(s.id)}</div>
            </div>
            <span class="conf">${conf}</span>
            <span class="status-chip ${status}">${status}</span>
        `;
        card.addEventListener('click', () => selectStart(s.id));
        container.appendChild(card);
    });
}

function selectStart(id) {
    selectedId = id;
    document.querySelectorAll('.start-card').forEach(el => {
        el.classList.toggle('selected', el.dataset.id === id);
    });
    // Render immediately with the preview (so the card responds), then
    // re-render after the heavy detail (full track, inferred line, features)
    // arrives. The selectedId guard avoids stale renders if the user advances
    // the list while a detail fetch is still in flight.
    renderDetail();
    ensureDetail(id).then((detail) => {
        if (detail && selectedId === id) renderDetail();
    });
}

// ---- Map rendering --------------------------------------------------------
function getStart(id) { return (startsData.starts || []).find(s => s.id === id); }

function ensureMap() {
    if (map) return;
    map = L.map('map', {
        zoomControl: true,
        preferCanvas: true,
    }).setView([47.687, -122.438], 14);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        attribution: '&copy; OpenStreetMap',
        maxZoom: 19,
    }).addTo(map);
}

function renderDetail() {
    const s = getStart(selectedId);
    if (!s) {
        document.getElementById('map-empty').style.display = '';
        document.getElementById('map-content').style.display = 'none';
        return;
    }
    document.getElementById('map-empty').style.display = 'none';
    document.getElementById('map-content').style.display = '';

    document.getElementById('detail-title').textContent = s.id;
    const conf = s.confidence != null ? (s.confidence * 100).toFixed(0) + '%' : '—';
    const schema = s.feature_schema_version ? ` · features v${s.feature_schema_version}` : '';
    document.getElementById('detail-subtitle').textContent =
        `${s.file} · conf ${conf}${schema}`;

    // Status button highlighting.
    const status = s.status || 'pending';
    document.getElementById('btn-confirm').classList.toggle('active', status === 'confirmed');
    document.getElementById('btn-discard').classList.toggle('active', status === 'discarded');
    document.getElementById('btn-pending').classList.toggle('active', status === 'pending');

    // Reset scrubber.
    const scrubber = document.getElementById('scrubber');
    scrubber.value = 0;
    document.getElementById('scrubber-time').textContent = '0 s';

    ensureMap();
    clearLayers();
    renderTrack(s);
    renderInferredLine(s);
    renderOverrideLine(s);
    renderMarks(s);
    renderBoatMarker(s, 0);
    renderWindArrow(s);
    fitMapTo(s);
    renderInfoPanel(s);
    loadVideoForStart(s);
}

function clearLayers() {
    // Tracks are a list; individually remove each so switching candidates
    // doesn't leave the previous red/blue polylines on the map.
    (layers.tracks || []).forEach(l => { if (l) map.removeLayer(l); });
    layers.tracks = [];
    Object.keys(layers).forEach(k => {
        if (k === 'tracks') return;
        const l = layers[k];
        if (l) map.removeLayer(l);
        layers[k] = null;
    });
}

function renderTrack(s) {
    const track = s.track || [];
    if (!track.length) return;
    // Split at the gun, sharing the boundary vertex so pre and post join.
    const start_t = s.start_time_unix;
    let splitIdx = track.findIndex(([, , t]) => t > start_t);
    if (splitIdx < 0) splitIdx = track.length;
    const pre = track.slice(0, splitIdx).map(([la, lo]) => [la, lo]);
    const post = track.slice(Math.max(0, splitIdx - 1)).map(([la, lo]) => [la, lo]);
    if (pre.length > 1) {
        layers.tracks.push(
            L.polyline(pre, {color: COLOR.track_pre, weight: 3, opacity: 0.9}).addTo(map));
    }
    if (post.length > 1) {
        layers.tracks.push(
            L.polyline(post, {color: COLOR.track_post, weight: 3, opacity: 0.95}).addTo(map));
    }
}

function renderInferredLine(s) {
    const line = s.start_line_inferred;
    if (!line || !line.pin_ll || !line.committee_ll) return;
    const latlngs = [line.pin_ll, line.committee_ll];
    layers.inferredLine = L.polyline(latlngs, {
        color: COLOR.inferred,
        weight: 3,
        dashArray: '6, 6',
        opacity: 0.8,
    }).bindTooltip('inferred line', {permanent: false, direction: 'top'}).addTo(map);
}

function renderOverrideLine(s) {
    const ov = s.line_override;
    if (!ov || !ov.pin_ll || !ov.committee_ll) return;
    const latlngs = [ov.pin_ll, ov.committee_ll];
    layers.overrideLine = L.polyline(latlngs, {
        color: COLOR.override,
        weight: 3,
        opacity: 0.95,
    }).addTo(map);
    layers.pin = L.marker(ov.pin_ll, {
        draggable: true, title: 'Pin (port end)',
        icon: endpointIcon('P', COLOR.override),
    }).addTo(map).on('dragend', () => onEndpointMoved());
    layers.committee = L.marker(ov.committee_ll, {
        draggable: true, title: 'Committee (stbd end)',
        icon: endpointIcon('C', COLOR.override),
    }).addTo(map).on('dragend', () => onEndpointMoved());
}

function renderMarks(s) {
    const marks = s.marks || {};
    if (marks.windward_ll) {
        layers.windward = L.marker(marks.windward_ll, {
            draggable: true, title: 'Windward mark',
            icon: markIcon('W', COLOR.windward),
        }).addTo(map).on('dragend', () => onMarkMoved('windward'));
    }
    if (marks.leeward_ll) {
        layers.leeward = L.marker(marks.leeward_ll, {
            draggable: true, title: 'Leeward mark',
            icon: markIcon('L', COLOR.leeward),
        }).addTo(map).on('dragend', () => onMarkMoved('leeward'));
    }
}

function renderBoatMarker(s, t_rel) {
    const pos = interpolateTrack(s, t_rel);
    if (!pos) return;
    layers.boat = L.circleMarker(pos, {
        radius: 6, color: COLOR.boat, fillColor: COLOR.boat, fillOpacity: 0.9, weight: 2,
    }).addTo(map);
}

function renderWindArrow(s) {
    const line = s.start_line_inferred;
    if (!line) return;
    const twd = line.course_bearing_deg;
    if (twd == null || !Number.isFinite(twd)) return;
    // Draw a short arrow from the inferred midpoint toward the wind source.
    const mid = midpoint(line.pin_ll, line.committee_ll);
    const head = offsetLatLon(mid, twd, 120);
    layers.windArrow = L.polyline([mid, head], {
        color: COLOR.wind, weight: 2, opacity: 0.8, dashArray: '2, 4',
    }).bindTooltip(`TWD ${Math.round(twd)}°`, {permanent: false, direction: 'top'}).addTo(map);
}

function fitMapTo(s) {
    const pts = [];
    const t = s.track || [];
    t.forEach(([la, lo]) => pts.push([la, lo]));
    if (s.start_line_inferred) {
        pts.push(s.start_line_inferred.pin_ll, s.start_line_inferred.committee_ll);
    }
    if (pts.length) {
        map.fitBounds(L.latLngBounds(pts).pad(0.15));
    }
}

function renderInfoPanel(s) {
    const il = s.start_line_inferred;
    document.getElementById('inferred-line-info').textContent = il
        ? `bearing ${fmt(il.bearing_deg)}° · twd ${fmt(il.course_bearing_deg)}° · ${il.source}`
        : '(none)';
    const ov = s.line_override;
    document.getElementById('override-line-info').textContent = ov
        ? `pin ${fmtLL(ov.pin_ll)} · committee ${fmtLL(ov.committee_ll)}`
        : '(none — press u to copy inferred, p/o to drop endpoints)';
    const marks = s.marks || {};
    const markParts = [];
    if (marks.windward_ll) markParts.push(`W ${fmtLL(marks.windward_ll)}`);
    if (marks.leeward_ll) markParts.push(`L ${fmtLL(marks.leeward_ll)}`);
    document.getElementById('marks-info').textContent = markParts.length ? markParts.join(' · ') : '(none)';

    // Feature table.
    const tbl = document.getElementById('features-table');
    tbl.innerHTML = '';
    const feats = s.features || {};
    const order = [
        'd_line_at_cross_m', 'cross_dir_sign', 'cross_speed_kts',
        'd_line_min_prestart_m', 'time_within_30m_prestart_s', 'along_line_at_cross_m',
        'prestart_tortuosity', 'prestart_tack_count', 'prestart_rel_cog_std', 'prestart_low_speed_frac',
        'post60_upwind_m', 'post60_best_single_tack_upwind_m',
        'post60_mean_upwind_vmg_kts', 'post60_first_tack_time_s',
        'approach_hdg_settled', 'reverse_cross_within_90s',
        'pre_frac', 'post_frac', 'hdg_std',
    ];
    order.forEach(k => {
        if (!(k in feats)) return;
        const v = feats[k];
        const row1 = document.createElement('span');
        row1.className = 'k'; row1.textContent = k;
        const row2 = document.createElement('span');
        row2.className = 'v'; row2.textContent = v == null ? '—' : fmt(v);
        tbl.appendChild(row1); tbl.appendChild(row2);
    });
}

// ---- Handlers: status + line/mark edits ----------------------------------
function setStatus(newStatus) {
    const s = getStart(selectedId);
    if (!s) return;
    s.status = newStatus;
    upsertEdit(selectedId, {status: newStatus});
    document.getElementById('btn-confirm').classList.toggle('active', newStatus === 'confirmed');
    document.getElementById('btn-discard').classList.toggle('active', newStatus === 'discarded');
    document.getElementById('btn-pending').classList.toggle('active', newStatus === 'pending');
    renderStats();
    // Update the card in the list without a full re-render to preserve scroll.
    const card = document.querySelector(`.start-card[data-id="${selectedId}"]`);
    if (card) {
        const chip = card.querySelector('.status-chip');
        chip.className = 'status-chip ' + newStatus;
        chip.textContent = newStatus;
    }
}

function dropEndpointAtCursor(which) {
    const s = getStart(selectedId);
    if (!s) return;
    const center = map.getCenter();
    const ll = [center.lat, center.lng];
    const ov = s.line_override || {pin_ll: null, committee_ll: null};
    ov[which + '_ll'] = ll;
    if (!ov.pin_ll) ov.pin_ll = ll;
    if (!ov.committee_ll) ov.committee_ll = ll;
    s.line_override = ov;
    upsertEdit(selectedId, {line_override: ov});
    refreshOverrideLayer(s);
    renderInfoPanel(s);
}

function onEndpointMoved() {
    const s = getStart(selectedId);
    if (!s || !s.line_override) return;
    const pinLL = layers.pin ? [layers.pin.getLatLng().lat, layers.pin.getLatLng().lng] : s.line_override.pin_ll;
    const ccLL = layers.committee ? [layers.committee.getLatLng().lat, layers.committee.getLatLng().lng] : s.line_override.committee_ll;
    s.line_override = {pin_ll: pinLL, committee_ll: ccLL};
    upsertEdit(selectedId, {line_override: s.line_override});
    if (layers.overrideLine) layers.overrideLine.setLatLngs([pinLL, ccLL]);
    renderInfoPanel(s);
}

function refreshOverrideLayer(s) {
    if (layers.overrideLine) { map.removeLayer(layers.overrideLine); layers.overrideLine = null; }
    if (layers.pin)          { map.removeLayer(layers.pin); layers.pin = null; }
    if (layers.committee)    { map.removeLayer(layers.committee); layers.committee = null; }
    renderOverrideLine(s);
}

function copyInferredToOverride() {
    const s = getStart(selectedId);
    if (!s || !s.start_line_inferred) return;
    const il = s.start_line_inferred;
    s.line_override = {pin_ll: il.pin_ll, committee_ll: il.committee_ll};
    upsertEdit(selectedId, {line_override: s.line_override});
    refreshOverrideLayer(s);
    renderInfoPanel(s);
}

function clearOverride() {
    const s = getStart(selectedId);
    if (!s) return;
    s.line_override = null;
    upsertEdit(selectedId, {line_override: null});
    refreshOverrideLayer(s);
    renderInfoPanel(s);
}

function dropMarkAtCursor(which) {
    const s = getStart(selectedId);
    if (!s) return;
    const center = map.getCenter();
    const ll = [center.lat, center.lng];
    const marks = s.marks || {};
    marks[which + '_ll'] = ll;
    s.marks = marks;
    upsertEdit(selectedId, {marks});
    refreshMarkLayer(s, which);
    renderInfoPanel(s);
}

function onMarkMoved(which) {
    const s = getStart(selectedId);
    if (!s) return;
    const layer = layers[which];
    if (!layer) return;
    const ll = [layer.getLatLng().lat, layer.getLatLng().lng];
    s.marks = s.marks || {};
    s.marks[which + '_ll'] = ll;
    upsertEdit(selectedId, {marks: s.marks});
    renderInfoPanel(s);
}

function refreshMarkLayer(s, which) {
    const layer = layers[which];
    if (layer) { map.removeLayer(layer); layers[which] = null; }
    const ll = (s.marks || {})[which + '_ll'];
    if (!ll) return;
    const color = which === 'windward' ? COLOR.windward : COLOR.leeward;
    const letter = which === 'windward' ? 'W' : 'L';
    layers[which] = L.marker(ll, {
        draggable: true, title: `${which} mark (right-click to remove)`,
        icon: markIcon(letter, color),
    }).addTo(map)
      .on('dragend', () => onMarkMoved(which))
      .on('contextmenu', (ev) => {
          L.DomEvent.preventDefault(ev.originalEvent);
          clearMark(which);
      });
}

function clearMark(which) {
    const s = getStart(selectedId);
    if (!s) return;
    const marks = {...(s.marks || {})};
    delete marks[which + '_ll'];
    s.marks = Object.keys(marks).length ? marks : null;
    upsertEdit(selectedId, {marks: s.marks});
    const layer = layers[which];
    if (layer) { map.removeLayer(layer); layers[which] = null; }
    renderInfoPanel(s);
}

function clearMarks() {
    const s = getStart(selectedId);
    if (!s) return;
    s.marks = null;
    upsertEdit(selectedId, {marks: null});
    if (layers.windward) { map.removeLayer(layers.windward); layers.windward = null; }
    if (layers.leeward) { map.removeLayer(layers.leeward); layers.leeward = null; }
    renderInfoPanel(s);
}

// ---- Scrubber + video sync ------------------------------------------------
function onScrub(ev) {
    const t_rel = +ev.target.value;
    document.getElementById('scrubber-time').textContent = `${t_rel} s`;
    const s = getStart(selectedId);
    if (!s) return;
    const pos = interpolateTrack(s, t_rel);
    if (pos && layers.boat) layers.boat.setLatLng(pos);
    // Drive the video from the scrubber, unless the scrubber change itself
    // came from a video timeupdate (guarded to avoid ping-pong).
    if (!syncingFromVideo && viewer) {
        syncVideoToScrubber(s, t_rel);
    }
}

function ensureVideosJson(cb) {
    if (videosJson) { cb(null); return; }
    if (videosFetching) return;
    videosFetching = true;
    const url = shared.signAWSUrl('Insta360/videos.json');
    if (!url) {
        videosFetching = false;
        cb(new Error('Could not sign videos.json URL'));
        return;
    }
    utils.getJSON(url, (err, data) => {
        videosFetching = false;
        if (err || !data) { cb(err || new Error('Empty videos.json')); return; }
        videosJson = data;
        (videosJson.videos || []).forEach((v) => {
            if (!v.basename && v.SourceFile) {
                const [basename] = utils.filenameSplit(v.SourceFile);
                v.basename = basename;
            }
        });
        videosColumnsMap = {};
        if (videosJson.columns) {
            for (const [name, idx] of Object.entries(videosJson.columns)) {
                videosColumnsMap[idx] = name;
            }
        }
        cb(null);
    });
}

function ensureViewer() {
    if (viewer) return viewer;
    const mount = document.getElementById('video-mount');
    if (!mount) return null;
    viewer = viewer360.create(mount, { keyboard: false, stabilize: true });
    if (viewer.canvas) viewer.canvas.tabIndex = -1;

    // Play-button label follows actual playback state.
    viewer.video.addEventListener('play',  syncPlayBtn);
    viewer.video.addEventListener('pause', syncPlayBtn);
    viewer.video.addEventListener('ended', syncPlayBtn);

    // Slave the scrubber + boat marker to the video while it plays.
    let lastTick = 0;
    viewer.video.addEventListener('timeupdate', () => {
        const now = performance.now();
        if (now - lastTick < 100) return;
        lastTick = now;
        const s = getStart(selectedId);
        if (!s) return;
        const ct = viewer.video.currentTime;
        const unixMs = viewer.getUnixAtVideoMs(ct * 1000);
        if (unixMs == null || !Number.isFinite(unixMs)) return;
        const rel = Math.round((unixMs - s.start_time_unix * 1000) / 1000);
        const clamped = Math.max(-300, Math.min(600, rel));
        const scrubber = document.getElementById('scrubber');
        if (String(scrubber.value) !== String(clamped)) {
            syncingFromVideo = true;
            scrubber.value = clamped;
            scrubber.dispatchEvent(new Event('input'));
            syncingFromVideo = false;
        }
    });
    return viewer;
}

function loadVideoForStart(s) {
    ensureVideosJson((err) => {
        if (err) {
            setVideoMessage(`Video unavailable: ${err.message || err}`);
            return;
        }
        const unixMs = s.start_time_unix * 1000;
        const record = viewer360.pickVideoForUnix(videosJson.videos, unixMs);
        if (!record) {
            setVideoMessage(`No video clip covers ${s.id}.`);
            return;
        }
        setVideoMessage(null);
        ensureViewer();
        if (record.basename !== currentVideoBasename) {
            currentVideoBasename = record.basename;
            currentVideoRecord = record;
            viewer.loadVideo({
                videoUrl: shared.signAWSUrl('Insta360/' + record.basename + '.mp4'),
                poseUrl:  shared.signAWSUrl('Insta360/' + record.PoseFile),
                logUrl:   record.LogFile ? shared.signAWSUrl('Insta360/' + record.LogFile) : null,
                columnsMap: videosColumnsMap,
                startTime: 0,
            });
        }
        // Seek only after BOTH the log XHR has completed AND the video has
        // fired loadedmetadata. ``viewer.loadVideo`` registers its own
        // loadedmetadata handler that sets ``video.currentTime = 0``; if we
        // seek before that handler runs, it silently clobbers us back to the
        // start of the clip. Waiting for ``video.readyState >= 1`` (which
        // implies loadedmetadata already fired) avoids the race.
        whenVideoAndLogReady(() => {
            viewer.seekToUnix(unixMs);
        });
    });
}

function syncVideoToScrubber(s, t_rel) {
    if (!viewer) return;
    const unixMs = (s.start_time_unix + t_rel) * 1000;
    // Whole-clip range may be null while a new clip is still loading; in
    // that case the pending whenVideoAndLogReady in loadVideoForStart will
    // land the correct initial seek, so a bailout here is the right thing.
    if (!viewer.getUnixRange() || viewer.video.readyState < 1) return;
    viewer.seekToUnix(unixMs);
}

function whenVideoAndLogReady(fn, { timeoutMs = 8000, pollMs = 50 } = {}) {
    const started = performance.now();
    const tick = () => {
        const logReady = viewer && viewer.getUnixRange() != null;
        const videoReady = viewer && viewer.video && viewer.video.readyState >= 1;
        if (logReady && videoReady) { fn(); return; }
        if (performance.now() - started > timeoutMs) {
            console.warn('[label] timed out waiting for video/log',
                         { logReady, videoReady });
            return;
        }
        setTimeout(tick, pollMs);
    };
    tick();
}

function setVideoMessage(msg) {
    const el = document.getElementById('video-message');
    if (!el) return;
    if (msg) {
        el.textContent = msg;
        el.style.display = '';
    } else {
        el.textContent = '';
        el.style.display = 'none';
    }
}

function interpolateTrack(s, t_rel) {
    const track = s.track || [];
    if (!track.length) return null;
    const target = s.start_time_unix + t_rel;
    // Find bracketing samples.
    let i = 0;
    while (i < track.length - 1 && track[i + 1][2] < target) i++;
    if (i === track.length - 1) return [track[i][0], track[i][1]];
    const [la, lo, t0] = track[i];
    const [la1, lo1, t1] = track[i + 1];
    if (t1 === t0) return [la, lo];
    const a = Math.max(0, Math.min(1, (target - t0) / (t1 - t0)));
    return [la + a * (la1 - la), lo + a * (lo1 - lo)];
}

// ---- Leaflet icon helpers -------------------------------------------------
function endpointIcon(letter, color) {
    return L.divIcon({
        className: '',
        html: `<div style="width:22px;height:22px;border-radius:50%;background:${color};
                          border:2px solid #0d1117;color:#0d1117;font-weight:bold;
                          display:flex;align-items:center;justify-content:center;
                          font-size:12px;font-family:ui-monospace,Menlo,monospace;">${letter}</div>`,
        iconSize: [22, 22], iconAnchor: [11, 11],
    });
}

function markIcon(letter, color) {
    return L.divIcon({
        className: '',
        html: `<div style="width:20px;height:20px;border-radius:3px;background:${color};
                          border:2px solid #0d1117;color:#0d1117;font-weight:bold;
                          display:flex;align-items:center;justify-content:center;
                          font-size:11px;font-family:ui-monospace,Menlo,monospace;">${letter}</div>`,
        iconSize: [20, 20], iconAnchor: [10, 10],
    });
}

// ---- Math helpers ---------------------------------------------------------
function midpoint(a, b) { return [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2]; }

function offsetLatLon(ll, bearing_deg, meters) {
    // Flat-earth approximation; fine for ±200 m at mid-latitudes.
    const lat = ll[0], lon = ll[1];
    const dn = meters * Math.cos(bearing_deg * Math.PI / 180);
    const de = meters * Math.sin(bearing_deg * Math.PI / 180);
    const dlat = dn / 111320;
    const dlon = de / (111320 * Math.cos(lat * Math.PI / 180));
    return [lat + dlat, lon + dlon];
}

function fmt(v) {
    if (v == null) return '—';
    if (typeof v !== 'number') return String(v);
    if (!Number.isFinite(v)) return '—';
    if (Math.abs(v) >= 100) return v.toFixed(0);
    if (Math.abs(v) >= 10) return v.toFixed(1);
    return v.toFixed(2);
}

function fmtLL(ll) {
    if (!ll) return '—';
    return `${ll[0].toFixed(5)}, ${ll[1].toFixed(5)}`;
}

// ---- Start ----------------------------------------------------------------
boot();
