// Bokeh's formatter dependency has no upstream fix for GHSA-hp3w-g68c-fv3c.
// Reject excessive work and return a harmless label instead of crashing a plot.
const core = require('sprintf-js/src/sprintf');
const token = /^%(?:([1-9]\d*)\$|\(([^)]+)\))?(\+)?(0|'[^$])?(-)?(\d+)?(?:\.(\d+))?([b-gijostTuvxX])/;
function permitted(format) {
    if (typeof format !== 'string' || format.length > 1024) return false;
    let count = 0;
    for (let i = 0; i < format.length;) {
        if (format[i] !== '%') { i++; continue; }
        if (format[i + 1] === '%') { i += 2; continue; }
        const match = token.exec(format.slice(i));
        if (!match || ++count > 64 || Number(match[1] || 0) > 1000 ||
            Number(match[6] || 0) > 1000 || Number(match[7] || 0) > 100 ||
            (match[8] === 'g' && match[7] !== undefined && Number(match[7]) === 0)) return false;
        i += match[0].length;
    }
    return true;
}
function sprintf(format, ...args) {
    if (!permitted(format)) return '[invalid format]';
    try { return core.sprintf(format, ...args); }
    catch (_) { return '[invalid format]'; }
}
function vsprintf(format, args) { return sprintf(format, ...args); }
module.exports = {sprintf, vsprintf};
