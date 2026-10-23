/**
 * v1 `.log` binary parser for sailview.
 *
 * The byte layout is owned by `Libraries/sailview_log_v1.py` on the Python
 * side; this module is the JS peer. Kept pure (no DOM, no globals, no fetch)
 * so the dispatcher introduced in Stage E can wrap it.
 *
 * Byte layout (little-endian):
 *   offset 0      int32  num_rows
 *   offset 4      int32  num_cols
 *   offset 8      int32  msec_per_sample
 *   offset 12     int32  multiplier
 *   offset 16     float64 max_values[num_cols]
 *   offset 16+8C  float64 min_values[num_cols]
 *   offset 16+16C uint16  payload[num_rows * num_cols], column-major
 *
 * Decoding (per the original `sailview.js::readLog`, preserved exactly so
 * existing deployed `.log` files continue to render identically):
 *
 *   const range = max_values[i] - min_values[i];
 *   const scale = range / multiplier;
 *   decoded[j]  = uint16[j] * scale + min_values[i];
 */

/**
 * Parse a v1 `.log` ArrayBuffer.
 *
 * @param {ArrayBuffer} arrayBuffer
 * @param {?Object} columnNumbers Optional index → name map (e.g.
 *   `state.videoMetadata.columns_numbers`). When provided, the returned
 *   `data` is keyed by column name; when omitted, it is keyed by numeric
 *   index as a string.
 * @return {{
 *   rows: number, cols: number,
 *   msecs: number, multiplier: number,
 *   maxValues: Float64Array, minValues: Float64Array,
 *   data: Object<string, Float64Array>
 * }}
 */
export function parseLogV1(arrayBuffer, columnNumbers) {
    let offset = 0;
    let length = 4 * 4;
    const [rows, cols, msecPerSample, multiplier] = new Int32Array(
        arrayBuffer.slice(offset, offset + length));

    offset += length;
    length = 8 * cols;
    const maxValues = new Float64Array(arrayBuffer.slice(offset, offset + length));
    offset += length;
    const minValues = new Float64Array(arrayBuffer.slice(offset, offset + length));
    offset += length;

    const colLength = 2 * rows;
    const data = {};
    for (let i = 0; i < cols; i++) {
        const colData = new Uint16Array(
            arrayBuffer.slice(offset + colLength * i,
                              offset + colLength * (i + 1)));
        const min = minValues[i];
        const range = maxValues[i] - min;
        const scale = range / Number(multiplier);
        const scaled = new Float64Array(colData.length);
        for (let j = 0; j < colData.length; j++) {
            scaled[j] = (Number(colData[j]) * scale) + min;
        }
        const key = (columnNumbers && columnNumbers[i] !== undefined)
            ? columnNumbers[i] : String(i);
        data[key] = scaled;
    }

    return {
        rows: Number(rows),
        cols: Number(cols),
        msecs: Number(msecPerSample),
        multiplier: Number(multiplier),
        maxValues, minValues,
        data,
    };
}


/**
 * If `data.row_times` has a non-monotonic tail (seen on some generated
 * logs where the final few rows repeat or backtrack), return the number
 * of leading rows that are strictly non-decreasing. The sailview loader
 * truncates all columns to this length so the map and instruments don't
 * jump near the end of a clip.
 *
 * @param {Float64Array|undefined} rowTimes
 * @param {number} rowsDefault fallback when `rowTimes` is missing/empty
 * @return {number}
 */
export function usableRowPrefix(rowTimes, rowsDefault) {
    if (!rowTimes || rowTimes.length <= 1) return rowsDefault;
    let i = 1;
    while (i < rowTimes.length && rowTimes[i] >= rowTimes[i - 1]) i++;
    return i;
}
