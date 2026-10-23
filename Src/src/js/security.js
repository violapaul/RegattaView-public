// Keep URL validation independent of the browser so it can be regression tested.
const PAGES = new Set(['index', 'sailview', 'review', 'tracks', 'graphs', 'calibrate',
    'playlists', 'label', 'lesson', 'lessons', 'notes', 'login', 'privacy']);

export function safeReturnUrl(value, currentUrl) {
    const base = new URL(currentUrl);
    const fallback = new URL('sailview.html', base);
    try {
        const target = new URL(value || 'sailview.html', base);
        const match = target.pathname.match(/^\/(?:www\/sailview\/)?([a-z]+)\.html$/);
        if (!['https:', 'http:'].includes(target.protocol) || target.origin !== base.origin ||
            target.username || target.password || !match || !PAGES.has(match[1])) return fallback;
        return target;
    } catch (_) {
        return fallback;
    }
}

export function reviewStatus(value) {
    return ['pending', 'confirmed', 'discarded'].includes(value) ? value : 'pending';
}

export function renderPlaylistLabel(element, title, position, snippet, timeString) {
    const heading = document.createElement('b');
    heading.textContent = String(title || '');
    const type = snippet.type ? ` [${snippet.type}]` : '';
    element.replaceChildren(heading, document.createTextNode(` ${position}${type}`),
        document.createElement('br'), document.createTextNode(
            `${snippet.name} ${timeString(snippet.start)}-${timeString(snippet.end)}`));
    if (snippet.comment) element.append(document.createElement('br'),
        document.createTextNode(String(snippet.comment)));
}
