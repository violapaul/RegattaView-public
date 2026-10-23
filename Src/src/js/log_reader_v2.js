/**
 * v2 `.slog` binary parser for sailview.
 *
 * Companion to `Libraries/sailview_log_v2.py`. Keep both in byte-sync.
 *
 * Byte layout (little-endian):
 *   HEADER (30 bytes fixed):
 *     offset  0   uint32   magic = 0x474F4C53 ('SLOG' LE)
 *     offset  4   uint16   version = 1
 *     offset  6   uint16   num_cols
 *     offset  8   uint32   num_rows
 *     offset 12   uint32   msec_per_sample
 *     offset 16   float64  start_unix_ms
 *     offset 24   float32  variation_deg
 *     offset 28   uint16   proj4_len
 *     offset 30   bytes    proj4[proj4_len]
 *   PER-COLUMN BLOCK (12 bytes × num_cols):
 *     0  uint8    col_id
 *     1  uint8    col_bits
 *     2  uint8    col_smoother
 *     3  uint8    col_source (0=primary, 1=fallback)
 *     4  float32  col_min
 *     8  float32  col_max
 *   PAYLOAD (per column, in column order):
 *     2 bytes uint16  first quantized sample
 *     2*(R-1) int16   delta-encoded remaining samples
 */

export const SLOG_MAGIC = 0x474F4C53;
export const SLOG_VERSION = 1;

const COL_NAME_BY_ID = {
    0: 'spd',
    1: 'sog',
    2: 'awa',
    3: 'aws',
    4: 'hdg',
    5: 'cog',
    6: 'twd',
    7: 'tws',
    8: 'zg100_roll',
    9: 'rudder',
    10: 'depth',
    11: 'latitude',
    12: 'longitude',
    13: 'gtwd',
    14: 'gtws',
    15: 'gtwa',
};


/**
 * Parse a v2 `.slog` ArrayBuffer.
 *
 * The returned `data` object mirrors the v1 reader's shape so that
 * downstream code (sailview.js, review.js, graphs.js) sees identical
 * keys. Derived channels (`map_lat`, `map_lon`, `twa`, `row_times`,
 * `row_seconds`) are synthesized from header scalars + primary channels
 * so the rest of the app stays oblivious to the format change.
 */
export function parseSlog(arrayBuffer) {
    const view = new DataView(arrayBuffer);

    const magic = view.getUint32(0, true);
    if (magic !== SLOG_MAGIC) {
        throw new Error(
            `parseSlog: bad magic 0x${magic.toString(16)}, expected 0x${SLOG_MAGIC.toString(16)}`);
    }
    const version = view.getUint16(4, true);
    if (version !== SLOG_VERSION) {
        throw new Error(`parseSlog: unsupported version ${version}`);
    }
    const numCols = view.getUint16(6, true);
    const numRows = view.getUint32(8, true);
    const msec = view.getUint32(12, true);
    const startUnixMs = view.getFloat64(16, true);
    const variationDeg = view.getFloat32(24, true);
    const proj4Len = view.getUint16(28, true);

    const proj4 = new TextDecoder('utf-8').decode(
        new Uint8Array(arrayBuffer, 30, proj4Len));

    // Per-column metadata block
    let offset = 30 + proj4Len;
    const columns = [];
    for (let c = 0; c < numCols; c++) {
        const colId = view.getUint8(offset);
        const colBits = view.getUint8(offset + 1);
        const colSmoother = view.getUint8(offset + 2);
        const colSource = view.getUint8(offset + 3);
        const colMin = view.getFloat32(offset + 4, true);
        const colMax = view.getFloat32(offset + 8, true);
        columns.push({
            colId,
            name: COL_NAME_BY_ID[colId] || `unknown_${colId}`,
            bits: colBits,
            smoother: colSmoother,
            source: colSource,
            min: colMin,
            max: colMax,
        });
        offset += 12;
    }

    // Payload: per column, uint16 first sample + int16 deltas
    const data = {};
    const sourceFlags = {};
    for (const col of columns) {
        const first = view.getUint16(offset, true);
        offset += 2;
        const deltaCount = numRows - 1;
        const deltas = new Int16Array(arrayBuffer, offset, deltaCount);
        offset += 2 * deltaCount;

        // Decode: cumsum(deltas) + first -> quantized -> float via (max-min)/(2^bits - 1)
        const quantMax = (1 << col.bits) - 1;
        const span = col.max - col.min;
        const scale = span === 0 ? 0 : span / quantMax;
        const decoded = new Float64Array(numRows);
        let acc = first;
        decoded[0] = acc * scale + col.min;
        for (let j = 1; j < numRows; j++) {
            acc += deltas[j - 1];
            decoded[j] = acc * scale + col.min;
        }
        data[col.name] = decoded;
        sourceFlags[col.name] = col.source;
    }

    // --- JS-side derived channels -------------------------------------

    // row_seconds: i * msec / 1000
    const rowSeconds = new Float64Array(numRows);
    for (let i = 0; i < numRows; i++) rowSeconds[i] = i * msec / 1000;
    data.row_seconds = rowSeconds;

    // row_times: start_unix_ms + i * msec (as a Float64Array to match v1)
    const rowTimes = new Float64Array(numRows);
    for (let i = 0; i < numRows; i++) rowTimes[i] = startUnixMs + i * msec;
    data.row_times = rowTimes;

    // variation column (constant per session) — plumbed for code that reads
    // getLogColumn('variation') (see sailview.js:613).
    const variation = new Float64Array(numRows);
    variation.fill(variationDeg);
    data.variation = variation;

    // twa: twd - (hdg + variation), wrapped to (-180, 180]
    if (data.twd && data.hdg) {
        const twa = new Float64Array(numRows);
        const twd = data.twd;
        const hdg = data.hdg;
        for (let i = 0; i < numRows; i++) {
            let a = twd[i] - (hdg[i] + variationDeg);
            a = ((a + 540) % 360) - 180;
            twa[i] = a;
        }
        data.twa = twa;
    }

    // turn_rate: d(hdg)/dt in °/s, with compass-wrap aware difference.
    // sailview.js:85 exposes 'TURN RATE' as a user-addable field; v1 shipped
    // it as its own column. We recompute from hdg here so the v2 reader
    // populates the same key.
    if (data.hdg) {
        const hdg = data.hdg;
        const dtSec = msec / 1000;
        const turnRate = new Float64Array(numRows);
        for (let i = 0; i < numRows; i++) {
            if (i === 0) { turnRate[0] = 0; continue; }
            let d = hdg[i] - hdg[i - 1];
            // Unwrap 360° jumps
            if (d > 180) d -= 360;
            else if (d < -180) d += 360;
            turnRate[i] = d / dtSec;
        }
        data.turn_rate = turnRate;
    }

    // map_lat, map_lon: project lat/lon via the session's PROJ4 origin.
    // Small-angle equirectangular is accurate to ~0.1% within a 2 km radius
    // of origin — sufficient for race-course visualization in Seattle. If
    // we ever need tight fidelity with the Python-side G.MAP output, swap
    // in a real proj4 implementation here.
    if (data.latitude && data.longitude) {
        const origin = _parseProj4Origin(proj4);
        if (origin) {
            const {lat0, lon0, k0} = origin;
            const R = 6378137.0;  // WGS84 semi-major axis (meters)
            const cosLat0 = Math.cos(lat0 * Math.PI / 180);
            const mapLat = new Float64Array(numRows);
            const mapLon = new Float64Array(numRows);
            for (let i = 0; i < numRows; i++) {
                mapLat[i] = R * k0 * (data.latitude[i] - lat0) * Math.PI / 180;
                mapLon[i] = R * k0 * (data.longitude[i] - lon0) * cosLat0
                            * Math.PI / 180;
            }
            data.map_lat = mapLat;
            data.map_lon = mapLon;
        }
    }

    return {
        rows: numRows,
        cols: numCols,
        msecs: msec,
        data,
        sourceFlags,
        meta: {
            startUnixMs,
            variationDeg,
            proj4,
            columns,
        },
    };
}


/**
 * Parse the PROJ4 string for lat_0, lon_0, and k_0 parameters. Returns
 * null on parse failure; caller falls back to skipping map_lat/map_lon.
 */
function _parseProj4Origin(proj4) {
    const getParam = (name) => {
        const re = new RegExp(`\\+${name}=(-?[0-9.]+)`);
        const m = proj4.match(re);
        return m ? parseFloat(m[1]) : null;
    };
    const lat0 = getParam('lat_0');
    const lon0 = getParam('lon_0');
    const k0 = getParam('k_0') ?? 1.0;
    if (lat0 == null || lon0 == null) return null;
    return {lat0, lon0, k0};
}
