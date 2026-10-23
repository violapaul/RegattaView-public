

import * as utils from './utils';
import {LAMBDA_URL, TOKEN_STORAGE_KEY, SESSION_STORAGE_KEY} from './auth_config';

const lambdaURL = LAMBDA_URL;

/**
 * Attach credentials to a Lambda query.
 *
 * Preferred: the long-lived app session token (HMAC-signed JWT issued by the
 * Lambda after Google sign-in). HMAC verify is microseconds and decouples
 * the user-facing session lifetime from Google's 1 h ID-token expiry.
 *
 * Fallback: the Google ID token. Sent only during the initial sign-in
 * handshake (login.js → TEST) before we've received a session token from
 * the Lambda, or during a one-time migration from a pre-session-token
 * client.
 *
 * Legacy {username, password} cookie auth was removed 2026-04-26 along with
 * the user-facing legacy login form; the Lambda still accepts those for the
 * iOS Uploader.
 */
function attachCredentials(query) {
    if (typeof localStorage === 'undefined') return query;
    const session = localStorage.getItem(SESSION_STORAGE_KEY);
    if (session) {
        query.session_token = session;
        return query;
    }
    const idToken = localStorage.getItem(TOKEN_STORAGE_KEY);
    if (idToken) {
        query.id_token = idToken;
    }
    return query;
}

/**
 * Pick up a freshly-minted session token from any successful Lambda
 * response and persist it. After the first successful call following
 * sign-in, the Google ID token becomes redundant — we drop it from
 * storage to avoid confusion (and to make `attachCredentials` switch
 * cleanly to the session-token path on the next call).
 */
function harvestSessionToken(res) {
    if (typeof localStorage === 'undefined') return;
    if (res && res.status === 200 && typeof res.session_token === 'string'
        && res.session_token.length > 0) {
        localStorage.setItem(SESSION_STORAGE_KEY, res.session_token);
        localStorage.removeItem(TOKEN_STORAGE_KEY);
    }
}

/**
 * If the Lambda returned 401 ("invalid token") clear the cached credentials
 * so the next checkAuthorization() falls through and the user is sent back
 * to the login page. 403 responses (expired / not on allowlist) are NOT
 * cleared so we can show a tailored error rather than silently looping
 * back to login.
 */
function maybeClearStaleToken(res) {
    if (!res || typeof localStorage === 'undefined') return;

    // 401 invalid_token: signature mismatch / malformed / Lambda secret
    // rotated. Drop everything; next checkAuthorization() bounces to login.
    if (res.status === 401 && res.reason === 'invalid token') {
        localStorage.removeItem(TOKEN_STORAGE_KEY);
        localStorage.removeItem(SESSION_STORAGE_KEY);
        return;
    }

    // 403 expired / not on allowlist: the user was removed (or their
    // allowlist entry expired) mid-session. Clear creds AND force a
    // page reload so they get redirected to login.html with a clean
    // "your access has ended" message rather than a silently-broken
    // sailview tab. Without this, an open tab keeps 403'ing forever.
    if (res.status === 403
            && (res.reason === 'expired' || res.reason === 'not on allowlist')) {
        localStorage.removeItem(TOKEN_STORAGE_KEY);
        localStorage.removeItem(SESSION_STORAGE_KEY);
        if (typeof window !== 'undefined' && window.location) {
            window.location.reload();
        }
    }
}

/**
 * POST to the lambda function and get the return
 * @param {*} query
 * @returns {*}
 */
export function lambdaRequest(query) {
    const jsonQuery = JSON.stringify(query);
    const xhr = new XMLHttpRequest();
    xhr.open('POST', lambdaURL, false);
    xhr.setRequestHeader('Content-type', 'application/json');

    // Sync XHR throws (not just sets xhr.status) on network errors and on
    // cross-origin failures where the upstream response has no CORS headers
    // (e.g. an API Gateway 502). Swallow those so a single flaky signed-URL
    // request doesn't blow up the entire render pipeline.
    try {
        xhr.send(jsonQuery);
    } catch (e) {
        console.warn('lambdaRequest network error:', e);
        return {status: 0, error: String(e)};
    }

    const status = xhr.status;
    let jsonResponse;
    try {
        jsonResponse = JSON.parse(xhr.response);
    } catch (e) {
        console.warn('Failed to parse lambda response (status', status, '):', e);
        jsonResponse = {error: 'Invalid JSON response'};
    }
    jsonResponse.status = status;
    maybeClearStaleToken(jsonResponse);
    harvestSessionToken(jsonResponse);
    return jsonResponse;
}

/**
 * Verify a Google ID token end-to-end (signature, allowlist, expiry) by
 * sending TEST to the Lambda. Used by login.js right after the GIS
 * callback. The Lambda mints a session token on success and includes it in
 * the response; harvestSessionToken (inside lambdaRequest) persists it.
 * @param {string} idToken
 * @returns {{ok: boolean, reason?: string, identity?: string}}
 */
export function verifyToken(idToken) {
    const res = lambdaRequest({
        method: 'TEST', bucket: 'None', filename: 'None', id_token: idToken,
    });
    if (res.status === 200) {
        return {ok: true, identity: res.identity};
    }
    return {ok: false, reason: res.reason || res.message || 'unknown'};
}

/**
 * Check that the cached credentials (preferred: session token; fallback:
 * Google ID token) are still valid by sending TEST to the Lambda. Returns
 * false fast (no network) if neither is stored.
 * @returns {boolean} true if authorized
 */
export function checkAuthorization() {
    const query = {method: 'TEST', bucket: 'None', filename: 'None'};
    attachCredentials(query);
    if (!query.session_token && !query.id_token) return false;
    return lambdaRequest(query).status === 200;
}

/**
 * Sign a URL for AWS
 * @param {string} filepath
 * @returns {string}
 */
export function signAWSUrl(filepath) {
    filepath = 'www/' + filepath;
    const query = attachCredentials({
        method: 'GET', bucket: 'regattaanalysis', filename: filepath,
    });
    const res = lambdaRequest(query);
    if (res.status === 200) {
        return res.url;
    }
    console.warn(`signAWSUrl ${filepath} failed: ${res.status} ${res.message || ''}`);
}

/**
 * Async, non-blocking POST to the lambda. Use this whenever the caller can
 * await — sync XHR (lambdaRequest) blocks the JS thread, which serializes
 * sign requests at startup and adds ~200–800 ms each.
 * @param {*} query
 * @returns {Promise<*>}
 */
export function lambdaRequestAsync(query) {
    return new Promise((resolve) => {
        const xhr = new XMLHttpRequest();
        xhr.open('POST', lambdaURL, true);
        xhr.setRequestHeader('Content-type', 'application/json');
        xhr.onload = () => {
            const status = xhr.status;
            let json;
            try { json = JSON.parse(xhr.response); }
            catch (e) { json = {error: 'Invalid JSON response'}; }
            json.status = status;
            maybeClearStaleToken(json);
            harvestSessionToken(json);
            resolve(json);
        };
        xhr.onerror = () => resolve({status: 0, error: 'Network error'});
        xhr.send(JSON.stringify(query));
    });
}

/**
 * Async sign — Promise<string|undefined>. Lets callers fire many sign
 * requests in parallel via Promise.all().
 */
export function signAWSUrlAsync(filepath) {
    const fp = 'www/' + filepath;
    const query = attachCredentials({
        method: 'GET', bucket: 'regattaanalysis', filename: fp,
    });
    return lambdaRequestAsync(query).then((res) => {
        if (res.status === 200) return res.url;
        console.warn(`signAWSUrlAsync ${fp} failed: ${res.status} ${res.message || ''}`);
        return undefined;
    });
}

const documentVersions = new Map();
const documentSaves = new Map();

async function documentRequest(query) {
    const response = await lambdaRequestAsync(attachCredentials(query));
    if (response.status !== 200) throw new Error(response.message || 'Could not save. Please retry.');
    return response;
}

export async function readDataJSON(wwwPath) {
    if (utils.isLocal()) return utils.fetchJSON(devUrlFor(wwwPath));
    const response = await documentRequest({method: 'READ_JSON', filename: 'www/' + wwwPath});
    documentVersions.set(wwwPath, response.etag);
    return response.data;
}

function saveDocument(wwwPath, data, callback) {
    if (!documentVersions.has(wwwPath)) {
        if (callback) callback('Reload this page before saving.');
        return;
    }
    const snapshot = JSON.parse(JSON.stringify(data));
    const previous = documentSaves.get(wwwPath) || Promise.resolve();
    const saving = previous.catch(() => {}).then(async () => {
        const response = await documentRequest({method: 'SAVE_JSON', filename: 'www/' + wwwPath,
            data: snapshot, etag: documentVersions.get(wwwPath)});
        documentVersions.set(wwwPath, response.etag);
        return response;
    });
    documentSaves.set(wwwPath, saving);
    saving.then(value => callback && callback(null, value), error => callback && callback(error.message));
}

/**
 * Save a JSON document via the Lambda's validated SAVE_JSON. Insta360/ prefix is injected.
 * @param {string} filepath - path relative to Insta360/
 * @param {Object} data - object to serialize and upload
 * @param {function} callback - called with (error, response)
 */
export function uploadJSON(filepath, data, callback) {
    putDataJSON('Insta360/' + filepath, data, callback);
}

/**
 * Upload a JSON object to an arbitrary www/<path> key in S3 (no Insta360/ prefix).
 * @param {string} wwwPath - path under www/ (e.g. 'Analyze/events_edits.json')
 */
export function uploadJSONToPath(wwwPath, data, callback) {
    saveDocument(wwwPath, data, callback);
}

/**
 * Dev-mode URL for a www/<path> resource. In dev the webpack server mirrors the S3 layout:
 *   www/Analyze/X  -> /Analyze/X
 *   www/Comments/X -> /Comments/X
 */
function devUrlFor(wwwPath) {
    return '/' + wwwPath;
}

/**
 * Get a URL to fetch content at www/<wwwPath>. Dev: local webpack; prod: signed S3.
 */
export function dataGetUrl(wwwPath) {
    if (utils.isLocal()) return devUrlFor(wwwPath);
    return signAWSUrl(wwwPath);
}

/**
 * PUT a JSON payload to www/<wwwPath>. Dev: local webpack middleware; prod: signed S3.
 */
export function putDataJSON(wwwPath, data, callback) {
    if (utils.isLocal()) {
        const xhr = new XMLHttpRequest();
        xhr.open('PUT', devUrlFor(wwwPath), true);
        xhr.setRequestHeader('Content-Type', 'application/json');
        xhr.onload = () => {
            if (xhr.status >= 200 && xhr.status < 300) {
                if (callback) callback(null, xhr.response);
            } else {
                if (callback) callback(xhr.status, xhr.response);
            }
        };
        xhr.onerror = () => { if (callback) callback('Network error', null); };
        xhr.send(JSON.stringify(data, null, 2));
        return;
    }
    uploadJSONToPath(wwwPath, data, callback);
}

/**
 * Get the list of videos and associated metadata
 * @param {*} func - function for callback func(error, data)
 */
export function getMetadata(func) {
    const url = signAWSUrl('Logs/video_poses_logs.json');
    utils.getJSON(url, func);
}

// ---- Comments ------------------------------------------------------------
// Layout in S3:
//   www/Comments/index.json              — alias registry (grows dynamically)
//   www/Comments/<alias>/comments.json   — one file per distinct alias
// Google account identity is verified by Lambda for every production write.
// Identity is the free-form alias the user picks on first comment (cookie
// `comment_alias`). Alias is also the path segment, constrained to
// [A-Za-z0-9_-]{1,30} so it's a safe path and easy to validate.
// Lambda owns alias registration and appends; a user cannot replace another
// account's file. Local development writes stay on the loopback dev server.

const COMMENTS_INDEX = 'Comments/index.json';
export const ALIAS_REGEX = /^[A-Za-z0-9_-]{1,30}$/;

function commentsPathFor(alias) {
    return `Comments/${alias}/comments.json`;
}

export function isValidAlias(alias) {
    return typeof alias === 'string' && ALIAS_REGEX.test(alias);
}

/**
 * Load Comments/index.json. Returns {version, users:[]} (empty on 404 / network error).
 * Deduplicates by alias, keeping the most-recently-seen row.
 * @param {function} cb - cb(null, index)
 */
export function loadCommentIndex(cb) {
    readDataJSON(COMMENTS_INDEX).then(data => cb(null, data || {version: 1, users: []}),
        error => cb(error.message, {version: 1, users: []}));
}

/**
 * Load all comments across all registered aliases. Callback receives a flat list
 * sorted by created_ts desc. Each entry is stamped with its directory-derived alias
 * on read, so a stray `alias` field inside a comment object can't spoof attribution.
 * @param {function} cb - cb(null, [comments])
 */
export function loadAllComments(cb) {
    loadCommentIndex(async (error, index) => {
        if (error) return cb(error, []);
        const users = (index.users || []).filter(u => isValidAlias(u.alias));
        const results = await Promise.allSettled(users.map(async u => {
            const data = await readDataJSON(commentsPathFor(u.alias));
            return (data?.comments || []).map(c => ({...c, alias: u.alias}));
        }));
        const flat = results.filter(r => r.status === 'fulfilled').flatMap(r => r.value);
        flat.sort((a, b) => (b.created_ts || 0) - (a.created_ts || 0));
        const failed = results.find(r => r.status === 'rejected');
        cb(failed?.reason?.message || null, flat);
    });
}

/**
 * Ensure `alias` has a row in Comments/index.json. Idempotent: if the alias already
 * exists, updates last_seen_ts; otherwise appends a new row. Rejects invalid charsets.
 * Production rejects an alias already owned by another Google account.
 * @param {string} alias
 * @param {function} cb - cb(err)
 */
export function ensureAliasRegistered(alias, cb) {
    if (!isValidAlias(alias)) return cb('Invalid alias');
    if (!utils.isLocal()) {
        documentRequest({method: 'REGISTER_ALIAS', alias}).then(() => cb(null), error => cb(error.message));
        return;
    }
    const now = Math.floor(Date.now() / 1000);
    loadCommentIndex((_, index) => {
        const others = (index.users || []).filter(u => u.alias !== alias);
        const existing = (index.users || []).find(u => u.alias === alias);
        others.push({
            alias,
            first_seen_ts: existing ? (existing.first_seen_ts || now) : now,
            last_seen_ts: now,
        });
        const newIndex = {version: 1, updated_ts: now, users: others};
        putDataJSON(COMMENTS_INDEX, newIndex, (err) => cb(err || null));
    });
}

/**
 * Append a comment to Comments/<alias>/comments.json, where alias comes from the
 * `comment_alias` cookie. Caller must call ensureAliasRegistered first (once, on
 * first post / alias change).
 * @param {{text:string, target:object}} comment
 * @param {function} cb - cb(err, stored)
 */
export function appendMyComment(comment, cb) {
    const alias = utils.getCookie('comment_alias');
    if (!alias) return cb('No alias set');
    if (!isValidAlias(alias)) return cb('Invalid alias in cookie');
    if (!utils.isLocal()) {
        documentRequest({method: 'APPEND_COMMENT', alias, comment})
            .then(response => cb(null, response.comment), error => cb(error.message));
        return;
    }
    const path = commentsPathFor(alias);
    utils.getJSON(dataGetUrl(path), (err, data) => {
        const now = Math.floor(Date.now() / 1000);
        const file = (err || !data) ? {version: 1, alias, comments: []} : data;
        const stored = {
            id: `c-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
            created_ts: now,
            text: comment.text,
            target: comment.target,
        };
        file.version = 1;
        file.alias = alias;
        file.updated_ts = now;
        file.comments = (file.comments || []).concat([stored]);
        putDataJSON(path, file, (err) => cb(err || null, stored));
    });
}
