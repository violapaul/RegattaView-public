import {safeReturnUrl} from './security';

/**
 * Create a reverse mapping from an object (swapping keys and values).
 * @param {Object} obj - Object with string keys and values
 * @returns {Object} - New object with keys and values swapped
 */
export function reverseMapping(obj) {
    const reversed = {};
    Object.entries(obj).forEach(([key, value]) => {
        reversed[value] = key;
    });
    return reversed;
}

/**
 * Fetch JSON from `url`. Resolves with parsed body on 2xx; rejects on non-2xx,
 * network error, or parse failure. The rejection Error has `.status` (HTTP
 * status, or 0 on network/parse error) and `.url` so callers can act on it
 * without re-string-matching messages.
 *
 * Use this for *required* config (calibration, presets, allowlist) — failures
 * should be visible. For data that is legitimately allowed to be missing
 * (e.g. empty playlists, no comments yet), use `fetchJSONOptional`.
 */
export function fetchJSON(url) {
    return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open('GET', url, true);
        xhr.responseType = 'json';
        xhr.onload = () => {
            const status = xhr.status;
            if (status >= 200 && status < 300) {
                if (xhr.response === null) {
                    const e = new Error(`fetchJSON: parse failed for ${url} (status ${status})`);
                    e.status = status; e.url = url;
                    reject(e);
                    return;
                }
                resolve(xhr.response);
            } else {
                const e = new Error(`fetchJSON: HTTP ${status} for ${url}`);
                e.status = status; e.url = url;
                reject(e);
            }
        };
        xhr.onerror = () => {
            const e = new Error(`fetchJSON: network error for ${url}`);
            e.status = 0; e.url = url;
            reject(e);
        };
        xhr.send();
    });
}

/**
 * Like fetchJSON, but resolves with `defaults` for "expected absent" responses
 * (404 by default; pass `optionalStatuses` to extend). Other failures still
 * reject — a 500 is not the same as a missing file and should surface.
 */
export function fetchJSONOptional(url, defaults, {optionalStatuses = [404]} = {}) {
    return fetchJSON(url).catch((err) => {
        if (optionalStatuses.includes(err.status)) return defaults;
        throw err;
    });
}

/**
 * Show a non-blocking error banner at the top of the page. Use for failures
 * the user needs to know about (config didn't load, instruments may be wrong)
 * but shouldn't kill the page over. Calls coalesce: repeated errors with the
 * same key overwrite rather than stack.
 */
export function surfaceError(message, {key = null, durationMs = 0} = {}) {
    if (typeof document === 'undefined') return;
    const id = key ? `errbanner-${key}` : `errbanner-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    let host = document.getElementById('errbanner-host');
    if (!host) {
        host = document.createElement('div');
        host.id = 'errbanner-host';
        host.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:99999;display:flex;flex-direction:column;pointer-events:none;';
        document.body.appendChild(host);
    }
    let el = document.getElementById(id);
    if (!el) {
        el = document.createElement('div');
        el.id = id;
        el.style.cssText = 'background:#a40000;color:#fff;padding:6px 12px;font:13px/1.4 -apple-system,Helvetica,sans-serif;border-bottom:1px solid #600;pointer-events:auto;display:flex;justify-content:space-between;align-items:center;gap:12px;';
        const close = document.createElement('button');
        close.textContent = '×';
        close.style.cssText = 'background:transparent;border:0;color:#fff;font-size:18px;line-height:1;cursor:pointer;padding:0 4px;';
        close.onclick = () => el.remove();
        const span = document.createElement('span');
        el.appendChild(span);
        el.appendChild(close);
        host.appendChild(el);
    }
    el.firstChild.textContent = message;
    if (durationMs > 0) setTimeout(() => el && el.remove(), durationMs);
    console.error('[surfaceError]', message);
}

/**
 * Legacy callback-style JSON fetch. Prefer `fetchJSON`. Kept for callsites
 * not yet migrated. Status code (or 0 on network error) is passed as `err`.
 * @deprecated
 */
export function getJSON(url, callback) {
    fetchJSON(url).then(
        (data) => callback(null, data),
        (err) => callback(err.status || 1, null),
    );
};

/**
 * push a download to the browser:
 * @param {*} filename
 * @param {*} text
 */
export function download(filename, text) {
    const element = document.createElement('a');
    element.setAttribute('href', 'data:text/plain;charset=utf-8,' + encodeURIComponent(text));
    element.setAttribute('download', filename);

    element.style.display = 'none';
    document.body.appendChild(element);

    element.click();

    document.body.removeChild(element);
}

/**
 * Return the url parameters as object:
 * @returns {Object}
 */
export function urlParameters() {
    const usp = new URLSearchParams(window.location.search);
    const res = {};
    for (const k of usp.keys()) {
        console.log(k);
        res[k] = usp.get(k);
    }
    return res;
}

/**
 * split a filename into the basename and extension
 * @param {string} filename
 * @return {[string, string]}
 */
export function filenameSplit(filename) {
    const components = filename.split('.');
    const basename = components.slice(0, -1).join('.');
    const extension = components[components.length - 1];
    return [basename, extension];
}

/**
 * Convert a time (in seconds) a user friendly string.
 * @param {Number} timeSeconds
 * @returns {String}
 */
export function timeString(timeSeconds) {
    const minutes = Math.floor(timeSeconds / 60);
    const seconds = Math.floor(timeSeconds - minutes * 60);
    const zeroPad = (num, places) => String(num).padStart(places, '0');
    return zeroPad(minutes, 2) + ':' + zeroPad(seconds, 2);
}

/**
 * Encode an arbitrary string so it will print in HTML.  Sort of ci
 * @param {string} s
 * @returns {string}
 */
export function htmlEncode(s) {
    const el = document.createElement('div');
    el.innerText = el.textContent = s;
    return el.innerHTML;
}

/**
 * Find min and max of array.
 * @param {Array} array
 * @returns {[Number, Number]}
 */
export function minmax(array) {
    let min = array[0];
    let max = array[0];
    let i = array.length;
    while (--i) {
        if (min > array[i]) {
            min = array[i];
        } else if (max < array[i]) {
            max = array[i];
        }
    }
    return [min, max];
}

/**
 * Find closest element
 * @param {Array} vals
 * @param {Number} item
 * @returns {Number}
 */
export function binarySearch(vals, item) {
    let low = 0;
    let high = vals.length - 1;

    while (low <= high) {
        const mid = Math.floor((low + high) / 2);
        const guess = vals[mid];

        if (guess === item) {
            return mid;
        }

        if (guess > item) {
            high = mid - 1;
        } else {
            low = mid + 1;
        }
    }

    return low; // if not found
}

/**
 * Quick hack to throttle an event.
 * @param {Number} minMS - number of seconds to wait
 * @returns {*} A function which returns true if minMS milliseconds have passed
 * since the last time it returned true.
 */
export function throttler(minMS) {
    let previous = Date.now();
    return () => {
        const now = Date.now();
        if (now - previous > minMS) {
            previous = now;
            return true;
        } else {
            return false;
        }
    };
}

/**
 * quick helper to return val if it exists or default
 * @param {*} val
 * @param {*} defaultVal
 * @returns {*}
 */
export function valOr(val, defaultVal) {
    if (val) {
        return val;
    } else {
        return defaultVal;
    }
}

/**
 * Capitalize a string.
 * @param {string} string
 * @returns {string}
 */
export function capitalize(string) {
    return string.charAt(0).toUpperCase() + string.slice(1);
}

/**
 * Sets the cookie
 * @param {*} name
 * @param {*} value
 * @param {*} days
 */
export function setCookie(name, value, days) {
    let expires = '';
    if (days) {
        const date = new Date();
        date.setTime(date.getTime() + (days * 24 * 60 * 60 * 1000));
        expires = '; expires=' + date.toUTCString();
    }
    document.cookie = name + '=' + (value || '') + expires + '; path=/';
}

/**
 * Gets the cookie
 * @param {*} name
 * @returns {*} cookie
 */
export function getCookie(name) {
    const nameEQ = name + '=';
    const ca = document.cookie.split(';');
    for (let i = 0; i < ca.length; i++) {
        let c = ca[i];
        while (c.charAt(0) == ' ') c = c.substring(1, c.length);
        if (c.indexOf(nameEQ) == 0) return c.substring(nameEQ.length, c.length);
    }
    return null;
}

/**
 * Erases the cookie
 * @param {*} name
 */
export function eraseCookie(name) {
    document.cookie = name + '=; Path=/; Expires=Thu, 01 Jan 1970 00:00:01 GMT;';
}

/**
 * Takes the current URL params, and creates a URL params string
 * @returns {string}
 */
export function rebuildURLParams(params) {
    const query = new URLSearchParams(params).toString();
    return query ? '?' + query : '';
}

/**
 * Redirect with the current URL params.  
 * @param {string} url  if specified, this is the redirect url.  If not it attempts to use the
 * future parameter.
 */
export function redirectWithParams(url, future) {
    const params = new URLSearchParams(window.location.search);
    const target = safeReturnUrl(url || params.get('future'), window.location.href);
    params.delete('future');
    for (const [key, value] of params) {
        if (!target.searchParams.has(key)) target.searchParams.set(key, value);
    }
    // A saved return URL can itself contain query parameters. Keep it encoded
    // as one value rather than concatenating a second '?' into the destination.
    target.searchParams.delete('future');
    if (future) target.searchParams.set('future', safeReturnUrl(future, window.location.href).href);
    window.location.assign(target.href);
}


/**
 * Function that returns true if running locally.
 * @returns {boolean}
 */
export function isLocal() {
    // WARNING:  return false for now
    // return false;
    return window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1';
}
