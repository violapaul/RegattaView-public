/**
 * lesson.js — Lesson Overview page.
 *
 * Renders three artifacts produced by the transcribe / summarize pipeline
 * (see docs/AUDIO_TRANSCRIBE.md):
 *   Analyze/Transcripts/<basename>.summary.md
 *   Analyze/Transcripts/<basename>.keypoints.json
 *   Analyze/Transcripts/<basename>.transcript.<engine>.json   (lazy)
 *
 * URL params:
 *   ?audio=<basename>     required.
 *   ?engine=<tag>         optional. Picks transcript flavour for "Show
 *                         transcript". Defaults to inspecting Insta360
 *                         videos.json + adjacent transcript files; in dev
 *                         we just probe a couple of common engine tags.
 *   ?video=<basename>     optional. If set, that one video is used for ALL
 *                         keypoint deep-links. Otherwise the page fetches
 *                         Insta360/videos.json and auto-resolves each
 *                         keypoint's UTC to whichever clip covers it.
 *
 * Data fetches go through shared.dataGetUrl so dev (local Analyze/) and
 * prod (Lambda-signed S3) both work without a switch.
 */

import '../css/lesson.css';
import * as shared from './shared';
import * as utils from './utils';
import * as viewer360 from './viewer360';

const $ = (id) => document.getElementById(id);

const TZ = 'America/Los_Angeles';
const TZ_LABEL = 'PT';

/** UTC ms -> "HH:MM:SS" in Pacific local. */
function pacificHms(utcMs) {
    if (!Number.isFinite(utcMs)) return '—';
    return viewer360.wallClockInTz(utcMs, TZ).slice(11, 19);
}

const TRANSCRIPT_ENGINE_PROBE_ORDER = [
    'openai-gpt-4o-transcribe',
    'openai-gpt-4o-mini-transcribe',
    'openai-whisper-1',
    'mlx-large-v3-mlx',
    'mlx-large-v3',
];

// ---- Boot -----------------------------------------------------------------

async function boot() {
    if (!shared.checkAuthorization()) {
        utils.redirectWithParams('login.html', window.location.pathname + window.location.search);
        return;
    }

    const params = new URLSearchParams(window.location.search);
    const audio = params.get('audio');
    const videoOverride = params.get('video');
    const engineHint = params.get('engine');

    if (!audio) {
        showError('Missing ?audio=<basename> URL parameter.\n\n' +
                  'Example: lesson.html?audio=shilshole_clip60m');
        return;
    }

    $('lesson-title').textContent = audio;

    // Fetch summary + keypoints in parallel.
    const [summary, keypoints] = await Promise.all([
        fetchText(`Analyze/Transcripts/${audio}.summary.md`)
            .catch((e) => { showError(`Could not load summary.md: ${e.message}`); return ''; }),
        fetchJSON(`Analyze/Transcripts/${audio}.keypoints.json`)
            .catch((e) => { showError(`Could not load keypoints.json: ${e.message}`); return null; }),
    ]);

    if (!keypoints) return;
    state.keypoints = keypoints;
    state.audioBasename = audio;
    state.engineHint = engineHint;

    // Resolve videos for deep-linking (best-effort; no link if it fails).
    state.manualVideoBase = videoOverride || null;
    state.videos = await loadVideosIndex().catch((e) => {
        console.warn('[lesson] videos.json fetch failed; deep-links disabled', e);
        return [];
    });

    renderHeader(summary, keypoints);
    renderSummary(summary);
    renderKeypoints(keypoints);
    wireTranscriptToggle();
}

// ---- State ----------------------------------------------------------------

const state = {
    keypoints: null,         // raw keypoints JSON
    audioBasename: null,
    engineHint: null,
    videos: [],              // raw videos.json rows + {basename}
    manualVideoBase: null,   // ?video=<base> URL override; bypasses videos.json
    transcript: null,        // lazy-loaded transcript JSON
    transcriptEngine: null,  // tag actually fetched
};

// ---- Fetch helpers --------------------------------------------------------

async function fetchText(wwwPath) {
    const url = shared.dataGetUrl(wwwPath);
    if (!url) throw new Error(`signing failed for ${wwwPath}`);
    const res = await fetch(url, {credentials: 'omit'});
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.text();
}

async function fetchJSON(wwwPath) {
    const url = shared.dataGetUrl(wwwPath);
    if (!url) throw new Error(`signing failed for ${wwwPath}`);
    const res = await fetch(url, {credentials: 'omit'});
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
}

// ---- Video index ----------------------------------------------------------

/**
 * Fetch Insta360/videos.json. Each row's CreateDate is per-SEGMENT (Insta360
 * splits at ~30 min) and authored in US/Pacific wall-clock — same convention
 * `viewer360.pickVideoForUnix` and `review.js` use. We don't reinvent the
 * UTC→clip mapping; we hand off to those canonical helpers.
 */
async function loadVideosIndex() {
    // Insta360/videos.json only lives in S3 (the local dev server doesn't
    // mirror /Insta360). Match sailview.js: always go through the Lambda,
    // even on localhost, so dev and prod resolve deep-links uniformly.
    const url = await shared.signAWSUrlAsync('Insta360/videos.json');
    if (!url) throw new Error('signing failed for Insta360/videos.json');
    const res = await fetch(url, {credentials: 'omit'});
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    return (data.videos || []).map((v) => {
        const [base] = utils.filenameSplit(v.SourceFile || '');
        return {...v, basename: base};
    });
}

/**
 * Given a keypoint UTC, find the covering video and the offset within it.
 * Returns {basename, offsetSeconds, exact} or null if no clip covers the UTC.
 *
 * Uses viewer360.pickVideoForUnix (same logic as review.js) so we stay in
 * sync with whatever heuristics that already handles for the rest of the app.
 *
 * `?video=<base>` URL override forces a single video with offset=0 (sailview
 * will clamp if the UTC isn't actually inside that clip).
 */
function videoLinkForUtc(utcMs) {
    if (!state.videos.length) return null;
    if (state.manualVideoBase) {
        return {basename: state.manualVideoBase, offsetSeconds: 0, exact: false, precise: false};
    }
    const record = viewer360.pickVideoForUnix(state.videos, utcMs);
    if (!record) return null;
    const offset = Math.max(0, viewer360.clipOffsetForUnix(record, utcMs));
    return {
        basename: record.basename,
        offsetSeconds: offset,
        exact: true,
        precise: Number.isFinite(record.StartUnixMs),
    };
}

function buildSailviewUrl(basename, offsetSeconds) {
    const qs = new URLSearchParams({
        video: basename,
        time: offsetSeconds.toFixed(2),
    });
    return `sailview.html?${qs.toString()}`;
}

function anchorTag(link) {
    if (!link.exact) return 'override';
    return link.precise ? 'GPS' : 'EXIF';
}

// ---- Header ---------------------------------------------------------------

function renderHeader(summary, kp) {
    const startUtc = kp.audio_start_utc;
    const startMs = startUtc ? Date.parse(startUtc) : NaN;
    const startPacific = Number.isFinite(startMs)
        ? viewer360.wallClockInTz(startMs, 'America/Los_Angeles')
        : '';
    const datePart = startPacific.slice(0, 10);
    const startTime = startPacific.slice(11, 19);
    const mode = kp.mode || 'race';
    const n = (kp.keypoints || []).length;
    const speakerNote = state.manualVideoBase
        ? `video override: ${state.manualVideoBase}`
        : state.videos.length
            ? `${state.videos.length} video${state.videos.length === 1 ? '' : 's'} indexed`
            : 'no video index';
    $('lesson-meta').textContent =
        `${datePart} · audio start ${startTime} ${TZ_LABEL} · mode=${mode} · ${n} keypoint${n === 1 ? '' : 's'} · ${speakerNote}`;
    $('keypoint-count').textContent = `${n} total`;
}

// ---- Summary markdown rendering -------------------------------------------

/**
 * Tiny markdown renderer for the subset our summarizer actually emits:
 *   - # / ## / ### headers
 *   - **bold** and *italic*
 *   - `code`
 *   - "- " bullet lists (single level)
 *   - paragraphs
 *   - HH:MM:SS timestamps inline -> clickable .md-time link
 *
 * Defensive: input is generated by the LLM so we escape everything before
 * applying our own inline transforms.
 */
function renderSummary(md) {
    const dateMs = anchorDateUtcMs(state.keypoints.audio_start_utc);
    const html = renderMarkdown(md, dateMs);
    $('summary').innerHTML = html;
}

function anchorDateUtcMs(audioStartUtc) {
    // The summary timestamps are HH:MM:SS UTC, no date. Use the audio's
    // start day (UTC) to expand; if a timestamp wraps past 24:00 in the
    // recording we'd need to handle multi-day, but lesson recordings are
    // capped at ~7-8 hours so a single anchor day is enough.
    if (!audioStartUtc) return null;
    const d = new Date(audioStartUtc);
    return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), 0, 0, 0);
}

function renderMarkdown(md, anchorDateMs) {
    const lines = md.split(/\r?\n/);
    const out = [];
    let i = 0;
    let inList = false;
    while (i < lines.length) {
        const raw = lines[i];
        if (/^\s*$/.test(raw)) {
            if (inList) { out.push('</ul>'); inList = false; }
            i++;
            continue;
        }
        const h = raw.match(/^(#{1,6})\s+(.*)$/);
        if (h) {
            if (inList) { out.push('</ul>'); inList = false; }
            const level = Math.min(h[1].length, 6);
            out.push(`<h${level}>${renderInline(h[2], anchorDateMs)}</h${level}>`);
            i++;
            continue;
        }
        const bullet = raw.match(/^\s*[-*]\s+(.*)$/);
        if (bullet) {
            if (!inList) { out.push('<ul>'); inList = true; }
            out.push(`<li>${renderInline(bullet[1], anchorDateMs)}</li>`);
            i++;
            continue;
        }
        // Paragraph (close any open list first)
        if (inList) { out.push('</ul>'); inList = false; }
        out.push(`<p>${renderInline(raw, anchorDateMs)}</p>`);
        i++;
    }
    if (inList) out.push('</ul>');
    return out.join('\n');
}

function renderInline(s, anchorDateMs) {
    let t = escapeHtml(s);
    // `code`
    t = t.replace(/`([^`]+)`/g, (_, c) => `<code>${c}</code>`);
    // **bold**
    t = t.replace(/\*\*([^*]+)\*\*/g, (_, b) => `<strong>${b}</strong>`);
    // *italic* (after **bold** so ** isn't mis-split)
    t = t.replace(/(^|[^*])\*([^*\n]+)\*/g, (_, pre, e) => `${pre}<em>${e}</em>`);
    // HH:MM:SS timestamps -> clickable time chips
    t = t.replace(/\b(\d{2}:\d{2}:\d{2})\b/g, (_, hms) => formatTimeLink(hms, anchorDateMs));
    return t;
}

function formatTimeLink(hms, anchorDateMs) {
    if (anchorDateMs == null) return `<span class="md-time disabled">${hms}</span>`;
    const [h, m, s] = hms.split(':').map(Number);
    const utcMs = anchorDateMs + ((h * 60 + m) * 60 + s) * 1000;
    const localHms = pacificHms(utcMs);
    const link = videoLinkForUtc(utcMs);
    if (!link) return `<span class="md-time disabled" title="${hms} UTC">${localHms}</span>`;
    const href = buildSailviewUrl(link.basename, link.offsetSeconds);
    const titleParts = [
        `${link.basename} +${link.offsetSeconds.toFixed(1)}s · ${hms} UTC · anchor=${anchorTag(link)}`,
    ];
    if (!link.exact) titleParts.push('(approximate; ?video= override, no UTC range check)');
    const title = titleParts.join(' ');
    return `<a class="md-time" href="${href}" target="sailview-player" rel="noopener" title="${escapeAttr(title)}">${localHms}</a>`;
}

function escapeHtml(s) {
    return String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}
function escapeAttr(s) { return escapeHtml(s); }

// ---- Keypoints ------------------------------------------------------------

function renderKeypoints(kp) {
    const list = $('keypoint-list');
    list.innerHTML = '';
    const items = kp.keypoints || [];
    for (const item of items) {
        list.appendChild(renderKeypoint(item));
    }
}

function renderKeypoint(k) {
    const el = document.createElement('div');
    el.className = 'kp';

    const utcMs = Date.parse(k.utc);
    const link = Number.isFinite(utcMs) ? videoLinkForUtc(utcMs) : null;
    const hms = Number.isFinite(utcMs) ? pacificHms(utcMs) : '—';
    const utcHover = Number.isFinite(utcMs) ? `${k.utc} (${TZ_LABEL})` : '';

    let timeNode;
    if (link) {
        timeNode = document.createElement('a');
        timeNode.className = 'kp-time';
        timeNode.href = buildSailviewUrl(link.basename, link.offsetSeconds);
        // Named target reuses a single tab across clicks (instead of opening
        // a new one each time). First click opens "sailview-player"; later
        // clicks navigate within it.
        timeNode.target = 'sailview-player';
        timeNode.rel = 'noopener';
        timeNode.title = link.exact
            ? `${link.basename} +${link.offsetSeconds.toFixed(1)}s · ${utcHover} · anchor=${anchorTag(link)}`
            : `${link.basename} (approximate; ?video= override) · ${utcHover}`;
        timeNode.textContent = hms;
    } else {
        timeNode = document.createElement('span');
        timeNode.className = 'kp-time disabled';
        timeNode.textContent = hms;
        timeNode.title = `no video covers this UTC · ${utcHover}`;
    }

    const row1 = document.createElement('div');
    row1.className = 'kp-row1';
    row1.appendChild(timeNode);

    if (k.category) {
        const cat = document.createElement('span');
        cat.className = 'kp-cat';
        cat.textContent = k.category;
        row1.appendChild(cat);
    }
    if (k.confidence) {
        const conf = document.createElement('span');
        conf.className = `kp-conf ${k.confidence}`;
        conf.textContent = k.confidence;
        row1.appendChild(conf);
    }
    el.appendChild(row1);

    if (k.label) {
        const label = document.createElement('div');
        label.className = 'kp-label';
        label.textContent = k.label;
        el.appendChild(label);
    }
    if (k.summary) {
        const sum = document.createElement('div');
        sum.className = 'kp-summary';
        sum.textContent = k.summary;
        el.appendChild(sum);
    }
    if (k.excerpt) {
        const ex = document.createElement('div');
        ex.className = 'kp-excerpt';
        ex.textContent = k.excerpt;
        el.appendChild(ex);
    }
    return el;
}

// ---- Transcript overlay ---------------------------------------------------

function wireTranscriptToggle() {
    $('show-transcript-btn').addEventListener('click', async () => {
        if (!state.transcript) {
            try {
                $('show-transcript-btn').textContent = 'Loading…';
                $('show-transcript-btn').disabled = true;
                state.transcript = await loadTranscript();
            } catch (e) {
                showError(`Could not load transcript: ${e.message}`);
                $('show-transcript-btn').textContent = 'Show transcript';
                $('show-transcript-btn').disabled = false;
                return;
            }
            renderTranscript(state.transcript);
            $('show-transcript-btn').textContent = 'Show transcript';
            $('show-transcript-btn').disabled = false;
        }
        $('transcript-overlay').classList.remove('hidden');
    });
    $('close-transcript-btn').addEventListener('click', () => {
        $('transcript-overlay').classList.add('hidden');
    });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') $('transcript-overlay').classList.add('hidden');
    });
}

async function loadTranscript() {
    const probes = state.engineHint
        ? [state.engineHint, ...TRANSCRIPT_ENGINE_PROBE_ORDER.filter((t) => t !== state.engineHint)]
        : TRANSCRIPT_ENGINE_PROBE_ORDER;
    let lastErr = null;
    for (const tag of probes) {
        const wwwPath = `Analyze/Transcripts/${state.audioBasename}.transcript.${tag}.json`;
        try {
            const data = await fetchJSON(wwwPath);
            state.transcriptEngine = tag;
            return data;
        } catch (e) {
            lastErr = e;
        }
    }
    throw lastErr || new Error('no transcript file found');
}

const COALESCE_GAP_MS = 700;

function renderTranscript(tr) {
    const body = $('transcript-body');
    body.innerHTML = '';
    const segs = (tr.segments || []).slice().sort((a, b) => a.start_utc.localeCompare(b.start_utc));
    let cur = null;
    const lines = [];
    for (const s of segs) {
        const startMs = Date.parse(s.start_utc);
        const endMs = Date.parse(s.end_utc);
        const spk = s.speaker || s.channel || '?';
        if (cur && cur.spk === spk && (startMs - cur.endMs) < COALESCE_GAP_MS) {
            cur.text += ' ' + (s.text || '').trim();
            cur.endMs = endMs;
            continue;
        }
        if (cur) lines.push(cur);
        cur = {startMs, endMs, spk, text: (s.text || '').trim()};
    }
    if (cur) lines.push(cur);

    for (const ln of lines) {
        const row = document.createElement('div');
        row.className = 'tx-line';
        const t = document.createElement('span');
        t.className = 'tx-time';
        t.textContent = new Date(ln.startMs).toISOString().slice(11, 19);
        const sp = document.createElement('span');
        sp.className = 'tx-spk';
        sp.textContent = `[${ln.spk}]`;
        const tx = document.createElement('span');
        tx.className = 'tx-text';
        tx.textContent = ln.text;
        row.append(t, sp, tx);
        body.appendChild(row);
    }

    const meta = $('transcript-meta');
    meta.textContent = `${tr.model || 'unknown'} · ${segs.length} segments · ${lines.length} coalesced lines`;
}

// ---- Errors ---------------------------------------------------------------

function showError(msg) {
    const el = $('error-banner');
    el.textContent = msg;
    el.classList.remove('hidden');
    console.error('[lesson]', msg);
}

// ---- Run ------------------------------------------------------------------

boot().catch((e) => showError(`Boot failed: ${e.message || e}`));
