/**
 * notes.js — Race Notes viewer (club-gated).
 *
 * Two modes:
 *   notes.html               -> index: list from Analyze/Notes/notes_index.json
 *   notes.html?note=<slug>   -> render Analyze/Notes/<slug>/note.html in an iframe
 *
 * The note HTML is pre-rendered by Scripts/build_notes.py (pandoc, self-contained:
 * figures embedded), so this page does NOT render markdown — it just auth-gates and
 * injects. Data fetches go through shared.dataGetUrl: dev serves local Analyze/,
 * prod returns a Lambda-signed S3 URL (Analyze/* is 403 to anon), so notes are
 * club-only by construction — same pattern as lesson.js.
 */
import * as shared from './shared';
import * as utils from './utils';

const $ = (id) => document.getElementById(id);

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

function showError(msg) {
    const e = $('error');
    e.textContent = msg;
    e.classList.remove('hidden');
}

function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g,
        (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
}

async function boot() {
    if (!shared.checkAuthorization()) {
        utils.redirectWithParams('login.html', window.location.pathname + window.location.search);
        return;
    }
    const slug = new URLSearchParams(window.location.search).get('note');
    if (slug) await showNote(slug);
    else await showList();
}

async function showNote(slug) {
    $('title').textContent = 'Race Note';
    let html;
    try {
        html = await fetchText(`Analyze/Notes/${slug}/note.html`);
    } catch (e) {
        showError(`Could not load note "${slug}": ${e.message}`);
        return;
    }
    const frame = $('frame');
    frame.srcdoc = html;        // self-contained note (embedded figures + <base target=_blank>)
    frame.classList.remove('hidden');
}

async function showList() {
    $('all-notes').classList.add('hidden');
    let idx;
    try {
        idx = await fetchJSON('Analyze/Notes/notes_index.json');
    } catch (e) {
        showError(`Could not load notes index: ${e.message}`);
        return;
    }
    const list = $('list');
    list.classList.remove('hidden');
    const notes = idx.notes || [];
    if (!notes.length) {
        list.innerHTML = '<p class="empty">No notes published yet.</p>';
        return;
    }
    list.innerHTML = notes.map((n) => `
        <a class="note-card" href="notes.html?note=${encodeURIComponent(n.slug)}">
          <div class="note-meta">${escapeHtml(n.date)}${n.type ? ' · ' + escapeHtml(n.type) : ''}</div>
          <div class="note-title">${escapeHtml(n.title || n.slug)}</div>
          <div class="note-headline">${escapeHtml(n.headline)}</div>
        </a>`).join('');
}

boot().catch((e) => showError(`Boot failed: ${e.message || e}`));
