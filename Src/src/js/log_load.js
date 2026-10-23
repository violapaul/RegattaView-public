/**
 * Log fetcher + format dispatcher for sailview.
 *
 * Selects v1 (`.log`) or v2 (`.slog`) based on a feature flag, fetches the
 * appropriate URL, and returns a parsed log in a shape that matches what
 * the v1 reader used to return so downstream code doesn't change.
 *
 * **Default: v2 enabled.** `.slog` is fetched first; on 404 (or any fetch
 * error) we fall back to `.log` and log a single warning.
 *
 * Opt-out mechanisms (force v1 / skip the `.slog` round-trip):
 *   - URL parameter `?slog=0`
 *   - localStorage.sailviewSlog === '0'
 *
 * Opt-in mechanisms (equivalent to the default, kept for explicitness):
 *   - URL parameter `?slog=1`
 *   - localStorage.sailviewSlog === '1'
 */

import {parseLogV1, usableRowPrefix} from './log_reader_v1';
import {parseSlog} from './log_reader_v2';


export function slogFlagEnabled() {
    try {
        const url = new URL(window.location.href);
        const p = url.searchParams.get('slog');
        if (p === '0') return false;
        if (p === '1') return true;
    } catch (e) { /* ignore */ }
    try {
        if (window.localStorage) {
            const v = window.localStorage.getItem('sailviewSlog');
            if (v === '0') return false;
            if (v === '1') return true;
        }
    } catch (e) { /* ignore */ }
    return true;  // default: prefer .slog, with automatic fallback to .log on 404
}


function _fetchArrayBuffer(url) {
    return new Promise((resolve, reject) => {
        const req = new XMLHttpRequest();
        req.open('GET', url, true);
        req.responseType = 'arraybuffer';
        req.onload = () => {
            if (req.status >= 200 && req.status < 300) {
                resolve(req.response);
            } else {
                const err = new Error(`HTTP ${req.status} for ${url}`);
                err.status = req.status;
                reject(err);
            }
        };
        req.onerror = () => reject(new Error(`network error for ${url}`));
        req.send(null);
    });
}


/**
 * Fetch + parse a log. When v2 is enabled and the `.slog` is available,
 * returns the v2-parsed result; otherwise falls back to v1 `.log`.
 *
 * `signUrl` may be either:
 *   - a function `(logicalPath) => signedUrl` (sync), or
 *   - an object `{slog: signedSlogUrl, log: signedLogUrl}` with URLs
 *     already obtained in parallel by the caller. The object form avoids
 *     a synchronous Lambda round-trip inside loadLog.
 *
 * @param {string} logPath       Logical path ending in `.log`
 * @param {(function(string): string)|{slog?: string, log: string}} signUrl
 * @param {?Object} columnNumbers   index → name map for v1 (ignored for v2)
 * @return {Promise<{rows, cols, msecs, data, sourceFlags?, meta?}>}
 */
export async function loadLog(logPath, signUrl, columnNumbers) {
    const presigned = (typeof signUrl === 'object' && signUrl !== null);
    const slogUrlOf = (lp) => presigned ? signUrl.slog : signUrl(
        lp.endsWith('.log') ? lp.slice(0, -4) + '.slog' : lp + '.slog');
    const logUrlOf  = (lp) => presigned ? signUrl.log  : signUrl(lp);

    const wantSlog = slogFlagEnabled() && (!presigned || !!signUrl.slog);
    if (wantSlog) {
        const slogUrl = slogUrlOf(logPath);
        try {
            const buf = await _fetchArrayBuffer(slogUrl);
            console.log(`[slog] using v2 .slog (${buf.byteLength} B decompressed) from ${logPath}`);
            const parsed = parseSlog(buf);
            return parsed;
        } catch (err) {
            console.warn(
                `[slog] .slog fetch failed (${err.message}); falling back to .log`);
            // fall through to v1
        }
    }
    const logUrl = logUrlOf(logPath);
    const buf = await _fetchArrayBuffer(logUrl);
    return {
        ...parseLogV1(buf, columnNumbers),
        sourceFlags: null,
        meta: null,
    };
}


// Re-export so call sites can still invoke `usableRowPrefix` via this module.
export {usableRowPrefix};
