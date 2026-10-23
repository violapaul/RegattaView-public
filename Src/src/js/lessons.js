/**
 * lessons.js — Lessons landing page.
 *
 * Reads Analyze/Transcripts/index.json (built by Scripts/build_lesson_index.py
 * after each summarize_transcript.py run), filters to mode === "lesson", and
 * renders one row per recording. Each row links to lesson.html?audio=<basename>.
 */

import '../css/lesson.css';
import * as shared from './shared';
import * as utils from './utils';
import * as viewer360 from './viewer360';

const $ = (id) => document.getElementById(id);

async function boot() {
    if (!shared.checkAuthorization()) {
        utils.redirectWithParams('login.html', window.location.pathname + window.location.search);
        return;
    }

    let doc;
    try {
        doc = await fetchJSON('Analyze/Transcripts/index.json');
    } catch (e) {
        renderEmpty(`Could not load Analyze/Transcripts/index.json (${e.message}).

Generate it with:
  conda run --no-capture-output -n sailing python Scripts/build_lesson_index.py

The manifest is also rebuilt automatically at the end of every
Scripts/summarize_transcript.py run.`);
        return;
    }

    const lessons = (doc.recordings || []).filter((r) => r.mode === 'lesson');
    if (!lessons.length) {
        renderEmpty('No lessons yet. Run:\n  ' +
            'conda run --no-capture-output -n sailing python Scripts/transcribe_audio.py <audio>\n' +
            '  conda run --no-capture-output -n sailing python Scripts/summarize_transcript.py ' +
            '<transcript.json> --mode lesson');
        return;
    }

    $('lessons-meta').textContent =
        `${lessons.length} lesson${lessons.length === 1 ? '' : 's'} · ` +
        `index generated ${doc.generated_utc || '(unknown)'}`;

    renderList(lessons);
}

async function fetchJSON(wwwPath) {
    const url = shared.dataGetUrl(wwwPath);
    if (!url) throw new Error(`signing failed for ${wwwPath}`);
    const res = await fetch(url, {credentials: 'omit'});
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
}

function renderList(lessons) {
    const list = $('lessons-list');
    list.innerHTML = '';
    for (const r of lessons) {
        list.appendChild(renderRow(r));
    }
}

function renderRow(r) {
    const a = document.createElement('a');
    a.className = 'lessons-row';
    a.href = `lesson.html?audio=${encodeURIComponent(r.audio_basename)}`;

    const dateStr = r.audio_start_utc ? formatDate(r.audio_start_utc) : '—';
    const dur = formatDuration(r.audio_duration_s);
    const counts = `${r.keypoint_count || 0} keypoints` +
        (r.skill_count ? ` · ${r.skill_count} skills` : '');

    const top = document.createElement('div');
    top.className = 'lessons-row-top';
    const dateEl = document.createElement('span');
    dateEl.className = 'lessons-date';
    dateEl.textContent = dateStr;
    const baseEl = document.createElement('span');
    baseEl.className = 'lessons-basename';
    baseEl.textContent = r.audio_basename;
    const durEl = document.createElement('span');
    durEl.className = 'lessons-meta';
    durEl.textContent = dur;
    const countsEl = document.createElement('span');
    countsEl.className = 'lessons-meta';
    countsEl.textContent = counts;
    top.append(dateEl, baseEl, durEl, countsEl);

    const headline = document.createElement('div');
    headline.className = 'lessons-headline';
    headline.textContent = r.headline || '';

    a.append(top, headline);
    return a;
}

function formatDate(iso) {
    // Pacific local — lessons happen on the boat in Seattle; UTC numbers
    // (e.g. "17:06 UTC") aren't intuitive for at-a-glance scanning.
    const ms = Date.parse(iso);
    if (!Number.isFinite(ms)) return iso;
    const wc = viewer360.wallClockInTz(ms, 'America/Los_Angeles'); // "YYYY-MM-DD HH:MM:SS"
    return `${wc.slice(0, 16)} PT`;
}

function formatDuration(seconds) {
    if (!Number.isFinite(seconds)) return '—';
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    if (h > 0) return `${h}h ${m}m`;
    return `${m}m`;
}

function renderEmpty(msg) {
    $('lessons-list').classList.add('hidden');
    const el = $('lessons-empty');
    el.textContent = msg;
    el.classList.remove('hidden');
}

function showError(msg) {
    const el = $('error-banner');
    el.textContent = msg;
    el.classList.remove('hidden');
    console.error('[lessons]', msg);
}

boot().catch((e) => showError(`Boot failed: ${e.message || e}`));
