import {renderPlaylistLabel} from './security';
/* eslint-disable require-jsdoc */
import '../css/video.css'; // Import the CSS file
import * as utils from './utils';
import * as shared from './shared';
import * as he from './htmlElements';
import * as sparkline from './sparkline';
import * as viewer360 from './viewer360';
import { buildVideoPicker } from './video_picker';
const $ = document.getElementById.bind(document);

// grab the url parameters
const urlParameters = utils.urlParameters();

// Page-level state. Viewer-owned state (pose_data, log_data, stabilize,
// skipRender, sensor calibration) lives inside viewer360 now.
const state = {
    sparkLength: urlParameters.sparkLength ? urlParameters.sparkLength : 10000,
    video: null,        // alias for viewer.video, set immediately below
    videoMetadata: null,
};

const params = {
    fov: 180,
};

// Mount the shared 360 viewer. Viewer360 creates its own <canvas> + <video>
// inside the mount container with the right iOS attributes, owns the WebGL
// fisheye renderer, pose stabilization, and pointer controls. Sailview
// drives play/pause/seek through `viewer.video`, instrument-panel updates
// through `onFrame`, and viewer-state toggles through the public API
// (reset, setStabilize, setSkipFrames). Keyboard shortcuts stay in this
// file's keyDownHandler — viewer360's built-in R/F/S handler is suppressed
// via `keyboard: false` to avoid double-firing.
const viewer = viewer360.create($('viewer-mount'), {
    fov: params.fov,
    keyboard: false,
    seedFirstFrame: true,
    onFrame: ({videoTimeMs}) => updateInstruments(videoTimeMs),
});
const canvas = viewer.canvas;
state.video = viewer.video;
// viewer360 creates the <video> element with muted=true so autoplay can't
// be blocked by browser policy. Sailview wants sound on by default — if
// unmuted autoplay is refused (no MEI / no prior gesture) viewer360's
// loadVideo falls back to muted playback; user presses M to unmute.
state.video.muted = false;

//  Control bar elements ///////////////////////////////////
const progressBar = document.querySelector('.progress-bar');
const timeCurrent = document.querySelector('.time-current');
const timeDuration = document.querySelector('.time-duration');
const speedBtn = document.querySelector('.speed-btn');
const muteBtn = document.querySelector('.mute-btn');
const fullscreenBtn = document.querySelector('.fullscreen-btn');
const videoPickerRoot = document.querySelector('.video-picker');
let videoPicker = null;

// Next-video popup elements
const nextVideoOverlay = document.querySelector('#nextVideoOverlay');
const nextVideoMessage = document.querySelector('#nextVideoMessage');
const nextVideoContinueBtn = document.querySelector('#nextVideoContinue');
const nextVideoPauseBtn = document.querySelector('#nextVideoPause');

// Speed control state
const speeds = [1, 1.5, 2, 4];
let speedIndex = 0;

// Display instrument data from log /////////////////////////////
const dataBlockElement = $('datablock');
const cornerTime = $('cornerTime');

// Curated list of channels users can pick from
const AVAILABLE_CHANNELS = [
    {key: 'dt', label: 'DT'},
    {key: 'fps', label: 'FPS'},
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
    {key: 'depth', label: 'DEPTH'},
    {key: 'rudder', label: 'RUDDER'},
    {key: 'turn_rate', label: 'TURN RATE'},
];

const SENSOR_COOKIE = 'sensorFields';
const DEFAULT_FIELDS = ['dt', 'hdg', 'twd', 'tws', 'spd', 'awa', 'twa', 'heel'];

// Container div for dynamic data field rows (keeps fields above the add button and map)
const fieldsContainer = document.createElement('div');
dataBlockElement.appendChild(fieldsContainer);

// Array of active field objects: {channel, span, sparkline, rowEl}
const dataFields = [];

// Set true while we hydrate the panel from a URL param so the per-field
// addField() calls don't clobber the receiver's saved cookie layout.
let _suppressFieldsSave = false;

function saveFieldsToCookie() {
    if (_suppressFieldsSave) return;
    utils.setCookie(SENSOR_COOKIE, JSON.stringify(dataFields.map((f) => f.channel)), 365);
}

function removeField(field) {
    const idx = dataFields.indexOf(field);
    if (idx >= 0) {
        dataFields.splice(idx, 1);
        field.rowEl.remove();
        saveFieldsToCookie();
    }
}

function addField(channel) {
    const field = he.addDataField(
        fieldsContainer,
        channel,
        AVAILABLE_CHANNELS,
        (newChannel) => {
            field.channel = newChannel;
            field.rowEl.setAttribute('data-channel', newChannel);
            saveFieldsToCookie();
        },
        () => removeField(field),
    );
    field.rowEl.setAttribute('data-channel', channel);
    sparkline.sparkline(field.sparkline, [1, 1, 1, 1, 1]);
    dataFields.push(field);
    saveFieldsToCookie();
    return field;
}

// Add-field button (appended after fieldsContainer, before map)
he.addAddButton(dataBlockElement, () => {
    const usedChannels = dataFields.map((f) => f.channel);
    const next = AVAILABLE_CHANNELS.find((c) => !usedChannels.includes(c.key));
    addField(next ? next.key : AVAILABLE_CHANNELS[0].key);
});

// Load persisted or default fields. `?reset=1` wipes the sensorFields cookie
// before loading so the user can recover from a truncated/empty list (e.g.
// after poking around on a phone) without opening Safari's Storage inspector.
//
// Precedence: ?fields=<csv> (a shared link) > sensorFields cookie > defaults.
// Unknown keys in ?fields are filtered out so a typo can't leave the panel
// empty. URL-driven loads suppress the cookie write so a shared link doesn't
// silently overwrite the receiver's saved layout.
if (urlParameters.reset === '1') {
    utils.eraseCookie(SENSOR_COOKIE);
}
const _savedFields = utils.getCookie(SENSOR_COOKIE);
const _validKeys = new Set(AVAILABLE_CHANNELS.map((c) => c.key));
const _urlFields = urlParameters.fields
    ? urlParameters.fields.split(',').map((s) => s.trim()).filter((s) => _validKeys.has(s))
    : null;
let _initialChannels;
const _fromUrl = !!(_urlFields && _urlFields.length);
if (_fromUrl) {
    _initialChannels = _urlFields;
} else {
    try {
        _initialChannels = _savedFields ? JSON.parse(_savedFields) : DEFAULT_FIELDS;
        if (!Array.isArray(_initialChannels) || _initialChannels.length === 0) {
            _initialChannels = DEFAULT_FIELDS;
        }
    } catch (_) {
        _initialChannels = DEFAULT_FIELDS;
    }
}
_suppressFieldsSave = _fromUrl;
_initialChannels.forEach((ch) => addField(ch));
_suppressFieldsSave = false;

// Also add a map to display the track and current position
const trackMap = he.addMap(dataBlockElement, 'map');

dataBlockElement.style.visibility = 'hidden';

/**
 * View a video with index and at a give time.
 * @param {*} videoRecord
 * @param {*} time
 */
function viewVideo(videoRecord, time) {
    state.videoRecord = videoRecord;
    if (videoPicker) {
        videoPicker.setSelected(videoRecord);
    }

    // Keep the URL bar honest so copy-URL / share reflects the current video.
    // Uses replaceState rather than pushState to avoid populating back/forward
    // history with every switch. Safe inside an iOS PWA — replaceState does
    // not navigate, so standalone mode is preserved.
    try {
        const params = new URLSearchParams(window.location.search);
        params.set('video', videoRecord.basename);
        params.delete('time'); // stale after switching videos
        params.delete('utc');  // wall-clock entry param; stale once a clip is chosen
        const qs = params.toString();
        history.replaceState(null, '', window.location.pathname + (qs ? '?' + qs : ''));
    } catch (_) { /* sandboxed contexts can refuse replaceState */ }

    console.log(`View Video : ${videoRecord.basename} ${videoRecord.PoseFile}`);
    console.log(`About to read video file: ${videoRecord.basename}.mp4`);

    // Serialize the sign requests so they hit a single warm Lambda
    // container. Parallel fan-out forces AWS to spawn fresh containers,
    // and the current handler does a ~9 s JWKS fetch on every cold start
    // (until the Lambda-side cache lands). Async (non-blocking) XHR keeps
    // the JS thread responsive; only the network is serial.
    const localDev = window.location.hostname === 'localhost' && urlParameters.local === '1';
    const useLocal = (runLocal && window.location.hostname === 'localhost') || localDev;
    const signOne = (logical) => useLocal
        ? Promise.resolve('/Insta360/' + logical)
        : shared.signAWSUrlAsync('Insta360/' + logical);

    const slogLogical = videoRecord.LogFile
        ? (videoRecord.LogFile.endsWith('.log')
            ? videoRecord.LogFile.slice(0, -4) + '.slog'
            : videoRecord.LogFile + '.slog')
        : null;

    (async () => {
        const mp4Url = await signOne(videoRecord.basename + '.mp4');
        const posesUrl = await signOne(videoRecord.PoseFile);
        let signLog;
        if (videoRecord.LogFile) {
            const slogUrl = await signOne(slogLogical);
            const logUrl  = await signOne(videoRecord.LogFile);
            signLog = {slog: slogUrl, log: logUrl};
        }
        viewer.loadVideo({
            videoUrl: mp4Url,
            poseUrl: posesUrl,
            logPath: videoRecord.LogFile ? 'Insta360/' + videoRecord.LogFile : null,
            signLog,
            columnsMap: state.videoMetadata.columns_numbers,
            basename: videoRecord.basename,
            startTime: time,
            projection: videoRecord.projection,
            // Autoplay on load. The away-pause handler below halts playback
            // (and shows the overlay) any time the tab/window loses focus,
            // so unattended tabs don't bleed S3 egress.
            autoplay: true,
        });
        state.video.basename = videoRecord.basename;
        dataBlockElement.style.visibility = videoRecord.LogFile ? 'visible' : 'hidden';
    })();
}

// Wire control-bar chrome to the viewer-owned <video>. Viewer360 already set
// playsInline + the iOS attributes, owns the canvas/WebGL/pose path, and
// handles the "play, wait for playing+timeupdate, seek, pause" first-frame
// seeding via the seedFirstFrame option. All this IIFE adds is the playback
// UI: play/pause/skip/scrub/speed/mute/fullscreen, the next-video popup
// trigger, and convenience methods on state.video that keyActions calls.
(function wireChrome() {
    const playBtn = document.querySelector('.play-btn');
    const skipBack = document.querySelector('.skip-back');
    const skipFwd = document.querySelector('.skip-fwd');
    const v = state.video;

    v.playPauseMedia = function() {
        if (v.paused) {
            playBtn.setAttribute('data-icon', 'u');
            v.play();
        } else {
            playBtn.setAttribute('data-icon', 'P');
            v.pause();
        }
    };

    v.mediaBackward = function() {
        v.currentTime = Math.max(0, v.currentTime - 5);
    };

    v.mediaForward = function() {
        v.currentTime = Math.min(v.duration || Infinity, v.currentTime + 5);
    };

    v.setTime = function() {
        timeCurrent.textContent = utils.timeString(v.currentTime);
        if (v.duration && !isNaN(v.duration)) {
            timeDuration.textContent = utils.timeString(v.duration);
            progressBar.value = (v.currentTime / v.duration) * 1000;
        }
    };

    playBtn.addEventListener('click', v.playPauseMedia);
    skipBack.addEventListener('click', v.mediaBackward);
    skipFwd.addEventListener('click', v.mediaForward);
    v.addEventListener('timeupdate', v.setTime);

    progressBar.addEventListener('input', () => {
        if (v.duration) {
            v.currentTime = (progressBar.value / 1000) * v.duration;
        }
    });

    speedBtn.addEventListener('click', () => {
        speedIndex = (speedIndex + 1) % speeds.length;
        v.playbackRate = speeds[speedIndex];
        speedBtn.textContent = speeds[speedIndex] + 'x';
    });

    muteBtn.addEventListener('click', () => {
        v.muted = !v.muted;
        muteBtn.classList.toggle('muted', v.muted);
    });

    // iPhone Safari refuses Element.requestFullscreen(); iPad Safari (13+)
    // supports it. When real fullscreen isn't available, fall back to a hint
    // pointing the user at "Add to Home Screen" — the only way to hide
    // Safari chrome on iPhone.
    fullscreenBtn.addEventListener('click', () => {
        const doc = document.documentElement;
        const exit = document.exitFullscreen
            || document.webkitExitFullscreen
            || document.webkitCancelFullScreen;
        const enter = doc.requestFullscreen
            || doc.webkitRequestFullscreen
            || doc.webkitRequestFullScreen;
        const inFullscreen = document.fullscreenElement
            || document.webkitFullscreenElement;
        if (inFullscreen) {
            if (exit) exit.call(document);
        } else if (enter) {
            const p = enter.call(doc);
            if (p && typeof p.catch === 'function') p.catch(() => showFullscreenHint());
        } else {
            showFullscreenHint();
        }
    });

    v.addEventListener('ended', () => showNextVideoPrompt(), true);
})();

function showFullscreenHint() {
    if (document.querySelector('#fullscreenHint')) return;
    const hint = document.createElement('div');
    hint.id = 'fullscreenHint';
    hint.textContent = 'For fullscreen: Share → Add to Home Screen, then launch from the icon.';
    hint.style.cssText = [
        'position:fixed', 'bottom:calc(140px + env(safe-area-inset-bottom))',
        'left:50%', 'transform:translateX(-50%)',
        'max-width:min(360px, calc(100vw - 32px))',
        'padding:10px 14px', 'background:rgba(0,0,0,0.75)',
        'color:white', 'border-radius:8px', 'font-size:13px',
        'z-index:200', 'backdrop-filter:blur(8px)',
        'text-align:center', 'line-height:1.4',
    ].join(';');
    document.body.appendChild(hint);
    setTimeout(() => hint.remove(), 4500);
}

// Thin wrappers around viewer accessors. Existing call sites (instrument
// panel, sparklines, BoatView, keyActions) read through these so we don't
// have to touch every getLog/getLogColumn/getLogHistory site.
function getLog(timeMS, columnName) { return viewer.getLog(timeMS, columnName); }
function getLogColumn(columnName) { return viewer.getLogColumn(columnName); }
function getLogHistory(timeMS, columnName, timeWindowMS) {
    return viewer.getLogHistory(timeMS, columnName, timeWindowMS);
}

// BoatView is rebuilt whenever the underlying log changes, keyed off
// viewer.getLogBasename(). Triggering on basename (rather than a manual
// reset flag toggled in viewVideo) closes a race where updateInstruments
// could fire AFTER resetBoatView() but BEFORE viewer.loadVideo() cleared
// the old log — building a new BoatView against stale clip-N-1 arrays.
let boatViewBasename = null;
function ensureBoatView() {
    const currentBasename = viewer.getLogBasename();
    if (!currentBasename || currentBasename === boatViewBasename) return;
    const lat = getLogColumn('map_lat');
    const lon = getLogColumn('map_lon');
    const hdg = getLogColumn('hdg');
    if (!lat || !lon || !hdg || lat.length === 0) return;
    // Pass the session's magnetic variation (plumbed through the .bin since
    // Stage 4) so BoatView can rotate the magnetic `hdg` onto the
    // true-frame projected map. Fallback to the legacy +15.4 hardcode when
    // an older .bin doesn't carry `variation` — keeps historical videos
    // rendering as before.
    const variationCol = getLogColumn('variation');
    let sessionVariation;
    if (variationCol && variationCol.length > 0 && isFinite(variationCol[0])) {
        sessionVariation = variationCol[0];
    } else {
        sessionVariation = 15.4;
        console.warn(
            'sailview: .bin lacks a `variation` column; falling back to ' +
            'the legacy 15.4° Seattle declination. Regenerate the log ' +
            'via race_upload.py to pick up the per-session value.'
        );
    }
    trackMap.boatView = new sparkline.BoatView(trackMap.map,
        lat, lon, hdg, {variation: sessionVariation});
    boatViewBasename = currentBasename;
}

// ============================================================
// Mobile floating action buttons
// ============================================================
// Three translucent FABs (play/pause, toggle controls, toggle instruments)
// are the only chrome visible by default on coarse-pointer devices. The
// existing control bar and info panel are revealed via a `.mobile-visible`
// class on each. The FABs themselves are always present in the DOM; the
// coarse-pointer media query gates their visibility via .mobile-fab display.
(function initMobileFabs() {
    const fabPlay = document.querySelector('#fabPlay');
    const fabReset = document.querySelector('#fabReset');
    const fabControls = document.querySelector('#fabControls');
    const fabInstruments = document.querySelector('#fabInstruments');
    const controlsBar = document.querySelector('.controls-bar');
    const playBtn = document.querySelector('.play-btn');
    const infoPanel = dataBlockElement;
    if (!fabPlay || !fabControls || !fabInstruments) return;

    // Reset = same behavior as the R keyboard shortcut: zero out pan + restore
    // the default FOV.
    if (fabReset) {
        fabReset.addEventListener('click', () => viewer.reset());
    }

    // Play/pause: delegate to the canonical .play-btn click so all existing
    // state (icon swap, audio, etc.) stays in one place.
    fabPlay.addEventListener('click', () => {
        if (playBtn) playBtn.click();
    });

    // Mirror the play-btn's data-icon swap (done by state.video.playPauseMedia)
    // onto the FAB so the glyph always reflects what the next tap will do.
    function syncPlayGlyph() {
        const icon = playBtn ? playBtn.getAttribute('data-icon') : 'P';
        // 'u' in the Heydings font = pause bars (video is playing) → show ⏸.
        // anything else ('P' = play triangle / video paused) → show ▶.
        fabPlay.innerHTML = icon === 'u' ? '&#x23F8;' : '&#x25B6;';
    }
    syncPlayGlyph();
    if (playBtn) {
        new MutationObserver(syncPlayGlyph).observe(playBtn, {
            attributes: true, attributeFilter: ['data-icon'],
        });
    }

    fabControls.addEventListener('click', () => {
        const visible = controlsBar.classList.toggle('mobile-visible');
        fabControls.classList.toggle('active', visible);
    });

    fabInstruments.addEventListener('click', () => {
        const visible = infoPanel.classList.toggle('mobile-visible');
        fabInstruments.classList.toggle('active', visible);
    });
})();

// ============================================================
// Event & Playlist system
// ============================================================

// Playlist control bar elements
const playlistSelect = document.querySelector('.playlist-select');
const snippetSelect = document.querySelector('.snippet-select');
const snippetPrev = document.querySelector('.snippet-prev');
const snippetNext = document.querySelector('.snippet-next');
const playlistControls = document.querySelector('.playlist-controls');

// Event marker overlay elements
const eventMarkerOverlay = $('eventMarkerOverlay');
const emEventType = $('emEventType');
const emComment = $('emComment');
const emPlaylistSel = $('emPlaylist');
const emNewPlaylistRow = document.querySelector('.em-new-playlist');
const emNewPlaylistName = $('emNewPlaylistName');
const emSave = $('emSave');
const emCancel = $('emCancel');

/**
 * Loads playlists.json (auto) and curated.json from S3, merges into state.
 */
function loadPlaylistData() {
    state.allEvents = {};
    state.allPlaylists = [];
    state.curatedEvents = {};
    state.curatedPlaylists = [];

    // Serialize sign + fetch (see viewVideo: Lambda parallel fan-out hits
    // 9 s cold-start JWKS fetches per container).
    const localDev = window.location.hostname === 'localhost' && urlParameters.local === '1';
    const useLocal = (runLocal && window.location.hostname === 'localhost') || localDev;
    const signOne = (logical) => useLocal
        ? Promise.resolve('/Insta360/' + logical)
        : shared.signAWSUrlAsync('Insta360/' + logical);

    (async () => {
        // Playlists are currently unused — superseded by review.html's
        // event-based flow. Both files may simply not exist; signed-S3 with
        // BlockPublicAccess returns 403 (not 404) for missing keys, so accept
        // both as "absent" and stay quiet on either.
        const ABSENT = {optionalStatuses: [403, 404]};
        const autoUrl = await signOne('playlists.json');
        try {
            const data = await utils.fetchJSONOptional(autoUrl, null, ABSENT);
            if (data) {
                (data.events || []).forEach((e) => { state.allEvents[e.id] = e; });
                state.allPlaylists = data.playlists || [];
                console.log(`Loaded ${Object.keys(state.allEvents).length} auto events, ${state.allPlaylists.length} auto playlists`);
            }
        } catch (err) {
            utils.surfaceError(`Auto playlists failed to load: ${err.message}`, {key: 'auto-playlists'});
        }

        try {
            const data = await shared.readDataJSON('Insta360/curated.json');
            if (data) {
                (data.events || []).forEach((e) => {
                    state.allEvents[e.id] = e;
                    state.curatedEvents[e.id] = e;
                });
                state.curatedPlaylists = data.playlists || [];
                console.log(`Loaded ${Object.keys(state.curatedEvents).length} curated events, ${state.curatedPlaylists.length} curated playlists`);
            }
        } catch (err) {
            utils.surfaceError(`Curated playlists failed to load: ${err.message}`, {key: 'curated-playlists'});
        }

        populatePlaylistDropdown();
        if (urlParameters.playlist) {
            activatePlaylistById(urlParameters.playlist);
        }
    })();
}

function allMergedPlaylists() {
    return [...state.allPlaylists, ...state.curatedPlaylists];
}

function populatePlaylistDropdown() {
    playlistSelect.innerHTML = '';

    const noneOpt = document.createElement('option');
    noneOpt.value = '';
    noneOpt.text = '-- Browse --';
    playlistSelect.add(noneOpt);

    const playlists = allMergedPlaylists();
    const byDate = {};
    const noDate = [];

    playlists.forEach((pl) => {
        if (pl.date) {
            if (!byDate[pl.date]) byDate[pl.date] = [];
            byDate[pl.date].push(pl);
        } else {
            noDate.push(pl);
        }
    });

    const sortedDates = Object.keys(byDate).sort().reverse();
    sortedDates.forEach((date) => {
        const group = document.createElement('optgroup');
        group.label = date;
        byDate[date].forEach((pl) => {
            const opt = document.createElement('option');
            opt.value = pl.id;
            opt.text = pl.title;
            group.appendChild(opt);
        });
        playlistSelect.add(group);
    });

    if (noDate.length > 0) {
        const group = document.createElement('optgroup');
        group.label = 'All Days';
        noDate.forEach((pl) => {
            const opt = document.createElement('option');
            opt.value = pl.id;
            opt.text = pl.title;
            group.appendChild(opt);
        });
        playlistSelect.add(group);
    }
}

function activatePlaylistById(playlistId) {
    const pl = allMergedPlaylists().find((p) => p.id === playlistId);
    if (pl) {
        playlistSelect.value = playlistId;
        playlist.setPlaylist(pl);
    }
}

/**
 * Save curated events + playlists to S3.
 */
function saveCuratedData() {
    const data = {
        version: 1,
        events: Object.values(state.curatedEvents),
        playlists: state.curatedPlaylists,
    };
    shared.uploadJSON('curated.json', data, (err) => {
        if (err) alert(`Playlist save failed: ${err}`);
        else console.log('Curated data saved to S3');
    });
}

// ============================================================
// Event Marker (replaces PlaylistConstructor)
// ============================================================

function showEventMarker() {
    if (!state.video) return;
    state.video.pause();

    emComment.value = '';
    emNewPlaylistName.value = state.videoRecord ? state.videoRecord.CreateDate.split(' ')[0] : '';
    emNewPlaylistRow.style.display = 'none';

    // Populate curated playlist options
    emPlaylistSel.innerHTML = '<option value="">-- None --</option><option value="__new__">+ New Playlist</option>';
    state.curatedPlaylists.forEach((pl) => {
        const opt = document.createElement('option');
        opt.value = pl.id;
        opt.text = pl.title;
        emPlaylistSel.add(opt);
    });

    eventMarkerOverlay.style.display = 'flex';
    emComment.focus();
}

function hideEventMarker() {
    eventMarkerOverlay.style.display = 'none';
    if (state.video) state.video.play();
}

emPlaylistSel.addEventListener('change', () => {
    emNewPlaylistRow.style.display = emPlaylistSel.value === '__new__' ? 'flex' : 'none';
});

emCancel.addEventListener('click', hideEventMarker);

emSave.addEventListener('click', () => {
    const videoName = state.video ? state.video.basename : 'unknown';
    const time = state.video ? state.video.currentTime : 0;
    const date = state.videoRecord ? state.videoRecord.CreateDate.split(' ')[0].replace(/-/g, '/') : '';

    const event = {
        id: `curated-${videoName}-${time.toFixed(1)}`,
        video: videoName,
        time: Math.round(time * 10) / 10,
        type: emEventType.value,
        comment: emComment.value,
        source: 'curated',
        date: date,
    };

    state.allEvents[event.id] = event;
    state.curatedEvents[event.id] = event;

    // Handle playlist assignment
    let targetPlaylistId = emPlaylistSel.value;
    if (targetPlaylistId === '__new__' && emNewPlaylistName.value.trim()) {
        const newPl = {
            id: 'curated-' + Date.now(),
            title: emNewPlaylistName.value.trim(),
            date: date,
            source: 'curated',
            padding: 10,
            events: [],
        };
        state.curatedPlaylists.push(newPl);
        targetPlaylistId = newPl.id;
    }

    if (targetPlaylistId && targetPlaylistId !== '__new__') {
        const pl = state.curatedPlaylists.find((p) => p.id === targetPlaylistId);
        if (pl && !pl.events.includes(event.id)) {
            pl.events.push(event.id);
        }
    }

    saveCuratedData();
    populatePlaylistDropdown();
    hideEventMarker();
});

// Required to get the canvas to capture keydown events.  By default it cannot,
// but by setting the tabindex and focus it will.
canvas.addEventListener('mouseover', (e) => {
    canvas.setAttribute('tabindex', '0');
    canvas.focus();
});

// Auto-pause when the user isn't watching (Space switch, ⌘-Tab, tab switch,
// minimize). Each MP4 is ~2 GB so unwatched playback burns S3 egress fast.
// Two signals are needed because they catch different things:
//   - `blur`/`focus`              → Space switch and ⌘-Tab (tab still "visible")
//   - `visibilitychange`/hidden   → tab switch, minimize
// Resume + hide overlay only if WE paused (don't fight a manual pause).
const awayOverlay = document.getElementById('awayOverlay');
let pausedByAway = false;

function isAway() {
    return document.hidden || !document.hasFocus();
}

function onAway() {
    if (!state.video || state.video.paused || pausedByAway) return;
    pausedByAway = true;
    state.video.pause();
    if (awayOverlay) awayOverlay.style.display = 'flex';
}

function onBack() {
    if (!pausedByAway) return;
    pausedByAway = false;
    if (awayOverlay) awayOverlay.style.display = 'none';
    if (state.video) state.video.play().catch(() => {});
}

window.addEventListener('blur', onAway);
window.addEventListener('focus', () => { if (!isAway()) onBack(); });
document.addEventListener('visibilitychange', () => {
    if (document.hidden) onAway();
    else if (!isAway()) onBack();
});

/**
 * Hide or unhide the help screen.
 */
function toggleHelpInfo() {
    const docDiv = $('docDiv');
    if (docDiv.style.display == 'none') {
        docDiv.style.display = 'block';
    } else {
        docDiv.style.display = 'none';
    }
}

// Dismiss help by tapping/clicking the overlay — required on mobile where
// there is no keyboard for the "h" shortcut.
$('docDiv').addEventListener('click', () => {
    $('docDiv').style.display = 'none';
});

// Run it once to hide help
// toggleHelpInfo();

/**
 * Show a prompt when a video ends offering to advance to the next one or stay paused.
 */
function showNextVideoPrompt() {
    if (!state.videoMetadata || !state.videoRecord) return;
    const videos = state.videoMetadata.videos;
    const currentIndex = state.videoRecord.index;
    const nextRecord = videos[currentIndex + 1];

    if (nextRecord) {
        nextVideoMessage.textContent = `Continue to next recording? (${nextRecord.CreateDate})`;
        nextVideoContinueBtn.style.display = 'inline-block';
        nextVideoPauseBtn.textContent = 'Stay Here';
    } else {
        nextVideoMessage.textContent = 'End of recordings — no more videos.';
        nextVideoContinueBtn.style.display = 'none';
        nextVideoPauseBtn.textContent = 'Close';
    }
    nextVideoOverlay.style.display = 'flex';
}

function hideNextVideoPrompt() {
    nextVideoOverlay.style.display = 'none';
}

// Wire up the popup buttons after DOM is ready.
nextVideoContinueBtn.addEventListener('click', () => {
    hideNextVideoPrompt();
    const videos = state.videoMetadata.videos;
    const nextRecord = videos[state.videoRecord.index + 1];
    if (nextRecord) {
        viewVideo(nextRecord, 0);
    }
});

nextVideoPauseBtn.addEventListener('click', () => {
    hideNextVideoPrompt();
});

const FRAME_DURATION = 1 / 29.97;
const SKIP_SECONDS = 5;

const keyActions = {
    ' ': () => state.video.playPauseMedia(),
    'ArrowLeft': () => {
        state.video.pause();
        state.video.currentTime -= FRAME_DURATION;
    },
    'ArrowRight': () => {
        state.video.pause();
        state.video.currentTime += FRAME_DURATION;
    },
    'ArrowDown': () => {
        state.video.currentTime = Math.max(0, state.video.currentTime - SKIP_SECONDS);
    },
    'ArrowUp': () => {
        state.video.currentTime = Math.min(state.video.duration || Infinity, state.video.currentTime + SKIP_SECONDS);
    },
    'r': () => viewer.reset(),
    'f': () => viewer.flip(),
    's': () => viewer.setStabilize(!viewer.getStabilize()),
    'p': () => viewer.setSkipFrames(!viewer.getSkipFrames()),
    'z': () => trackMap.boatView.zoom(),
    '.': () => trackMap.boatView.zoomAndCenter(),
    'm': () => { state.video.muted = !state.video.muted; },
    'l': () => {
        const time = state.video.currentTime * 1000;
        trackMap.boatView.start(getLog(time, 'twd'));
    },
    'c': () => {
        const loc = window.location;
        const url = loc.origin + loc.pathname;
        const query = new URLSearchParams({
            video: state.video.basename,
            time: state.video.currentTime,
        });
        const fields = dataFields.map((f) => f.channel).join(',');
        if (fields) query.set('fields', fields);
        navigator.clipboard.writeText(url + '?' + query.toString());
    },
    'n': () => showEventMarker(),
    'k': () => postSailviewComment(),
    '[': () => playlist.prev(),
    ']': () => playlist.next(),
    'h': () => toggleHelpInfo(),
};

// ---- Per-moment comments -------------------------------------------------
// Prompts for an alias the first time, then asks for a comment and posts it
// against the current video moment (basename + currentTime).
function postSailviewComment() {
    if (!state.video) return;
    state.video.pause();

    const post = () => {
        const text = window.prompt(`Comment @ ${state.video.basename} t=${state.video.currentTime.toFixed(1)}s`);
        if (!text || !text.trim()) return;
        shared.appendMyComment({
            text: text.trim(),
            target: {
                kind: 'sailview',
                video: state.video.basename,
                t: Math.round(state.video.currentTime * 10) / 10,
            },
        }, (err, stored) => {
            if (err) { alert(`Comment failed: ${err}`); return; }
            console.log('[sailview] posted comment:', stored);
        });
    };

    const cached = utils.getCookie('comment_alias');
    if (shared.isValidAlias(cached)) {
        shared.ensureAliasRegistered(cached, err => {
            if (err) {
                utils.setCookie('comment_alias', '', -1);
                alert(`Could not register alias: ${err}. Please choose a different name.`);
                return;
            }
            post();
        });
        return;
    }

    let msg = 'Pick a display name for your comments.\nAllowed: letters, numbers, dash, underscore. Max 30 chars.';
    for (let attempt = 0; attempt < 3; attempt++) {
        const raw = window.prompt(msg, utils.getCookie('username') || '');
        if (raw == null) return;
        const alias = raw.trim();
        if (!shared.isValidAlias(alias)) {
            msg = `"${alias}" is not a valid alias. Use only letters, numbers, dash, underscore (1–30 chars).`;
            continue;
        }
        shared.ensureAliasRegistered(alias, (err) => {
            if (err) { alert(`Could not register alias: ${err}`); return; }
            utils.setCookie('comment_alias', alias, 365);
            post();
        });
        return;
    }
}

function keyDownHandler(event) {
    // Don't intercept keys when typing in an input/textarea/select
    const tag = event.target.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
    // Close event marker on Escape
    if (event.key === 'Escape' && eventMarkerOverlay.style.display === 'flex') {
        hideEventMarker();
        return;
    }
    const action = keyActions[event.key];
    if (action) {
        action();
    }
}

document.addEventListener('keydown', keyDownHandler);

/**
 * Format a Unix timestamp (ms) as a datetime string.
 * @param {Number} unixMs
 * @returns {string}
 */
function formatDateTime(unixMs) {
    const now = new Date(unixMs);
    const month = (now.getMonth() + 1).toString().padStart(2, '0');
    const day = now.getDate().toString().padStart(2, '0');
    const date = now.getFullYear() + '-' + month + '-' + day;
    const hours = now.getHours().toString().padStart(2, '0');
    const minutes = now.getMinutes().toString().padStart(2, '0');
    const seconds = now.getSeconds().toString().padStart(2, '0');
    return date + ' ' + hours + ':' + minutes + ':' + seconds;
}

/** Plays a list of event snippets with prev/next navigation. */
class PlaylistPlayer {
    constructor() {
        this.snippets = [];
        this.playlistData = null;
        this.currentPosition = 0;
        this.active = false;
        this.commentElem = $('displayComment');
    }

    /** Load a playlist object, resolve event IDs to snippets, and start playing. */
    setPlaylist(playlistData) {
        this.playlistData = playlistData;
        const padding = playlistData.padding || 10;

        this.snippets = (playlistData.events || []).map((eventId) => {
            const ev = state.allEvents[eventId];
            if (!ev) return null;
            return {
                name: ev.video,
                start: Math.max(0, ev.time - padding),
                end: ev.time + padding,
                comment: ev.comment || '',
                type: ev.type || '',
                eventId: ev.id,
            };
        }).filter(Boolean);

        if (this.snippets.length === 0) {
            console.log('Playlist has no resolvable events');
            this.stop();
            return;
        }

        this.currentPosition = 0;
        playlistControls.classList.add('active');
        this._populateSnippetDropdown();
        this._playCurrent();
    }

    stop() {
        this.active = false;
        this.snippets = [];
        this.playlistData = null;
        this.commentElem.innerHTML = '';
        playlistControls.classList.remove('active');
        snippetSelect.innerHTML = '';
    }

    next() {
        if (this.snippets.length === 0) return;
        this.currentPosition = (this.currentPosition + 1) % this.snippets.length;
        this._playCurrent();
    }

    prev() {
        if (this.snippets.length === 0) return;
        this.currentPosition = (this.currentPosition - 1 + this.snippets.length) % this.snippets.length;
        this._playCurrent();
    }

    goTo(index) {
        if (index >= 0 && index < this.snippets.length) {
            this.currentPosition = index;
            this._playCurrent();
        }
    }

    _playCurrent() {
        if (!state.videoMetadata || !state.videoMetadata.nameMap) {
            setTimeout(() => this._playCurrent(), 500);
            return;
        }

        const snippet = this.snippets[this.currentPosition];
        const videoRecord = state.videoMetadata.nameMap[snippet.name];
        if (videoRecord) {
            if (state.video && state.video.basename === snippet.name) {
                state.video.currentTime = snippet.start;
                if (state.video.paused) state.video.play();
            } else {
                viewVideo(videoRecord, snippet.start);
            }
            this.active = true;
            this._updateDisplay(snippet);
            snippetSelect.value = this.currentPosition;
        } else {
            console.log(`No video record for ${snippet.name}, skipping`);
            this.next();
        }
    }

    _updateDisplay(snippet) {
        const title = this.playlistData ? this.playlistData.title : '';
        const pos = `${this.currentPosition + 1}/${this.snippets.length}`;
        renderPlaylistLabel(this.commentElem, title, pos, snippet, utils.timeString);
    }

    _populateSnippetDropdown() {
        snippetSelect.innerHTML = '';
        this.snippets.forEach((s, i) => {
            const opt = document.createElement('option');
            opt.value = i;
            const typeTag = s.type ? `[${s.type}]` : '';
            opt.text = `#${i + 1} ${typeTag} ${s.comment || s.name}`.substring(0, 60);
            snippetSelect.add(opt);
        });
    }

    monitor(video) {
        if (this.active && video && video.ready && this.snippets.length > 0) {
            const snippet = this.snippets[this.currentPosition];
            if (video.currentTime >= snippet.end) {
                this.next();
            }
        }
    }
}

const playlist = new PlaylistPlayer();

// Wire up playlist UI
playlistSelect.addEventListener('change', () => {
    const plId = playlistSelect.value;
    if (!plId) {
        playlist.stop();
    } else {
        activatePlaylistById(plId);
    }
});

snippetSelect.addEventListener('change', () => {
    playlist.goTo(parseInt(snippetSelect.value));
});

snippetPrev.addEventListener('click', () => playlist.prev());
snippetNext.addEventListener('click', () => playlist.next());

let timeBefore = 0;

/**
 * Per-frame UI tick. Called from viewer360's render loop (post-draw) once
 * per drawn frame, only when the video has decoded at least one frame —
 * the same gate sailview's old render() used (state.video.ready). The
 * fisheye draw, pose stabilization, and pointer-driven viewpoint all live
 * inside viewer360 now; this function is purely instrument-panel + map +
 * playlist housekeeping. `timeMs` is `viewer.video.currentTime * 1000`.
 */
function updateInstruments(timeMs) {
    const now = performance.now() * 0.001;
    const dt = now - timeBefore;
    timeBefore = now;
    const currentFPS = dt > 0 ? 1 / dt : 0;

    playlist.monitor(state.video);
    ensureBoatView();

    if (cornerTime) {
        const unixTime = getLog(timeMs, 'row_times');
        if (unixTime) cornerTime.innerText = formatDateTime(unixTime);
    }

    dataFields.forEach((field) => {
        if (field.channel === 'fps') {
            field.span.innerText = currentFPS.toFixed(1);
        } else if (field.channel === 'dt') {
            const unixTime = getLog(timeMs, 'row_times');
            if (unixTime) field.span.innerText = formatDateTime(unixTime);
        } else {
            const val = getLog(timeMs, field.channel);
            if (val) {
                // Only SPD keeps a fractional digit; every other instrument
                // reads better rounded to the nearest integer on a phone.
                const digits = field.channel === 'spd' ? 1 : 0;
                field.span.innerText = val.toFixed(digits);
                // While the video is still seeking near t=0, the trailing
                // window collapses to 0 or 1 samples and would produce a
                // malformed SVG path (Math.min/max of empty / 0*Infinity).
                // Skip just that transient case; let real data anomalies
                // (NaN inside the slice, etc.) surface as before.
                const history = getLogHistory(timeMs, field.channel, state.sparkLength);
                if (history && history.length >= 2) {
                    sparkline.sparkline(field.sparkline, history);
                }
            }
        }
    });

    if (trackMap.boatView) {
        trackMap.boatView.update(getLog(timeMs, 'map_lat'), getLog(timeMs, 'map_lon'), getLog(timeMs, 'hdg'));
    }
}

function readMetadata() {
    const localDev = window.location.hostname === 'localhost' && urlParameters.local === '1';
    const useLocal = (runLocal && window.location.hostname === 'localhost') || localDev;
    const urlPromise = useLocal
        ? Promise.resolve('/Insta360/videos.json')
        : shared.signAWSUrlAsync('Insta360/videos.json');
    urlPromise.then((url) => readMetadataAt(url));
}

/**
 * Resolve a wall-clock instant (Unix ms) to the clip that covers it plus the
 * in-clip offset (seconds), using each record's GPS-aligned `StartUnixMs`
 * anchor (set by sync_video_to_bb; see create_videos_json). Picks the latest
 * clip starting at or before the target; if the target precedes every clip,
 * falls back to the earliest clip at t=0. Records without `StartUnixMs`
 * (legacy clips lacking a `.slog` anchor) are skipped. Returns null if no
 * clip carries an anchor.
 */
function resolveVideoForUtc(videos, utcMs) {
    if (!Number.isFinite(utcMs)) return null;
    let best = null;
    let earliest = null;
    for (const v of videos) {
        if (typeof v.StartUnixMs !== 'number') continue;
        if (!earliest || v.StartUnixMs < earliest.StartUnixMs) earliest = v;
        if (v.StartUnixMs <= utcMs && (!best || v.StartUnixMs > best.StartUnixMs)) best = v;
    }
    const record = best || earliest;
    if (!record) return null;
    return {record, offsetSec: Math.max(0, (utcMs - record.StartUnixMs) / 1000)};
}

function readMetadataAt(url) {
    utils.getJSON(url, (err, data) => {
        if (err !== null) {
            console.log('Something went wrong loading video metadata: ' + err);
        } else {
            state.videoMetadata = data;
            state.videoMetadata.columns_numbers = utils.reverseMapping(state.videoMetadata.columns);
            state.videoMetadata.nameMap = {};
            // Keep videos in chronological order (oldest first) for sequential playback.
            // Create a dictionary of video names to video records.
            state.videoMetadata.videos.forEach((v, index) => {
                // eslint-disable-next-line no-unused-vars
                const [basename, extension] = utils.filenameSplit(v.SourceFile);
                v.basename = basename;
                v.index = index;
                state.videoMetadata.nameMap[basename] = v;
            });

            videoPicker = buildVideoPicker(
                videoPickerRoot,
                state.videoMetadata.videos,
                (record) => {
                    state.videoRecord = record;
                    viewVideo(record, 0);
                },
            );

            // Default to the first video of the most recent day so the viewer
            // starts at the beginning of the last session and plays forward.
            const videos = state.videoMetadata.videos;
            const lastDate = videos[videos.length - 1].CreateDate.split(' ')[0];
            const defaultIndex = videos.findIndex(v => v.CreateDate.startsWith(lastDate));
            let videoRecord = videos[defaultIndex >= 0 ? defaultIndex : 0];
            let startTime = urlParameters.time || 0;
            if (urlParameters.video) {
                videoRecord = state.videoMetadata.nameMap[urlParameters.video] || videoRecord;
            } else if (urlParameters.utc) {
                // Wall-clock deep link (e.g. from tracks.html's `v` shortcut):
                // resolve the clip covering that instant + the in-clip offset.
                const resolved = resolveVideoForUtc(videos, Number(urlParameters.utc) * 1000);
                if (resolved) {
                    videoRecord = resolved.record;
                    startTime = resolved.offsetSec;
                }
            }
            viewVideo(videoRecord, startTime);
            loadPlaylistData();
        }
    });
}

/// Setup for local testing — gated on runLocal=false so this is a developer
/// escape hatch, not a production code path.
function localTest() {
    state.videoMetadata = {
        // Keep in sync with columns.json (which is produced from
        // Libraries/sailview_log_v1.py::COLUMN_LIST). This hardcoded
        // fallback is only used by the runLocal path.
        "columns": {
            "row_seconds": 0, "spd": 1, "sog": 2, "latitude": 3, "longitude": 4,
            "turn_rate": 5, "rudder": 6, "depth": 7, "altitude": 8,
            "zg100_pitch": 9, "zg100_roll": 10, "awa": 11, "aws": 12,
            "twd": 13, "tws": 14, "twa": 15, "hdg": 16, "cog": 17,
            "map_lon": 18, "map_lat": 19, "row_times": 20, "variation": 21,
        },
    };
    state.videoMetadata.columns_numbers = utils.reverseMapping(state.videoMetadata.columns);
    viewer.loadVideo({
        videoUrl: '/Insta360/video.mp4',
        poseUrl: '/Insta360/video.poses',
        logPath: 'video.log',
        signLog: (path) => '/Insta360/' + path,
        columnsMap: state.videoMetadata.columns_numbers,
        basename: 'video',
        startTime: 0,
    });
    state.video.basename = 'video';
    dataBlockElement.style.visibility = 'visible';
}

const runLocal = false;
const currentPath = window.location.pathname;

if (!shared.checkAuthorization()) {
    utils.redirectWithParams('login.html', currentPath);
} else if (runLocal && window.location.hostname === 'localhost') {
    localTest();
} else {
    readMetadata();
}
