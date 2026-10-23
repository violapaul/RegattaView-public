import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
const code = await fs.readFile(new URL('../src/js/security.js', import.meta.url), 'utf8');
const {safeReturnUrl, reviewStatus} = await import('data:text/javascript;base64,' + Buffer.from(code).toString('base64'));
const base = 'https://regattaview.net/login.html';
test('login rejects executable, cross-origin, and non-application destinations', () => {
    for (const value of ['javascript:alert(1)', 'JaVaScRiPt:alert(1)', 'data:text/html,hello', '//evil.invalid/', 'https://evil.invalid/review.html', 'https://regattaview.net@evil.invalid/review.html', '/atc/index.html', '/www/Analyze/note.html', '/assets/payload.svg', '\\evil.invalid\\x', 'http://regattaview.net/review.html']) {
        assert.equal(safeReturnUrl(value, base).href, 'https://regattaview.net/sailview.html', value);
    }
});
test('login preserves valid deep links and query values', () => {
    for (const value of ['/review.html?day=2026-10-19&tab=comments', '/www/sailview/sailview.html?video=VID_1&time=12', 'notes.html?note=a%26b']) {
        assert.equal(safeReturnUrl(value, base).href, new URL(value, base).href);
    }
    assert.equal(safeReturnUrl('review.html?day=2026-10-19', 'http://localhost:9000/login.html').origin, 'http://localhost:9000');
});
test('untrusted review statuses cannot reach HTML attributes', () => {
    for (const value of ['<img src=x onerror=alert(1)>', 'confirmed" onmouseover="alert(1)', null, {}, ['confirmed']]) assert.equal(reviewStatus(value), 'pending');
    for (const value of ['pending','confirmed','discarded']) assert.equal(reviewStatus(value), value);
});
