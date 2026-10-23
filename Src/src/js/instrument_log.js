import * as utils from './utils';
import * as shared from './shared';

/**
 * Slice from an array, more like a python slice.
 * @param {Array} array
 * @param {Number} start
 * @param {Number} end
 * @param {Number} step
 * @returns {Array}
 */
function fullSlice(array, start, end, step) {
    if (step) {
        const res = [];
        for (let i = start; i < end; i += step) {
            res.push(array[i]);
        }
        return res;
    } else {
        return array.slice(start, end);
    }
}

/**
 * Read and access instrument logs stored in a binary file.
 */
export class InstrumentLog {
    /**
     * @param {Object} params
     * @param {Object} columnMap
     */
    constructor(params) {
        this.logDir = params.logDir;
        this.logFile = params.logFile;
        this.timestamp = params.timestamp;
        this.deltaT = utils.valOr(params.deltaT, 100);
        this.skip = utils.valOr(params.skip, null);
        this.columnMap = params.columnMap;
        this.timestamp = utils.valOr(Number(params.timestamp), 0);
        this.url = this.logDir + this.logFile;
    }

    /** load the log data
     *
     * @param {*} callback - called with "this" when done
     */
    load(callback) {
        console.log(`Loading log file ${this.logFile}...`)
        console.log(`Loading log file ${this.url}...`)
        const awsUrl = shared.signAWSUrl(this.url);
        if (awsUrl) {
            const oReq = new XMLHttpRequest();
            oReq.open('GET', awsUrl, true);
            oReq.responseType = 'arraybuffer';

            const myThis = this;

            oReq.onload = function(oEvent) {
                const arrayBuffer = oReq.response; // Note: not oReq.responseText
                if (arrayBuffer) {
                    console.log(`Read log file ${myThis.url} with ${arrayBuffer.byteLength}`);
                    const [rows, cols] = new BigUint64Array(arrayBuffer.slice(0, 16));
                    [myThis.rows, myThis.cols] = [Number(rows), Number(cols)];
                    console.log(` Rows ${myThis.rows} Cols ${myThis.cols}`);
                    myThis.data = new Float64Array(arrayBuffer.slice(16));
                    callback(myThis);
                }
            };
            oReq.send(null);
        }
    }

    /**
     * Get the instrument log data for a particular time.
     * @param {Number} timeMS
     * @param {string} columnName
     * @returns {Number}
    */
    getData(timeMS, columnName) {
        if (this.data) {
            const columnNumber = this.columnMap[columnName];
            if (columnNumber) {
                const rowOffset = Math.round(timeMS / this.deltaT); // One row every deltaT
                const colOffset = columnNumber * this.data.rows;
                return this.data[rowOffset + colOffset];
            }
        }
        console.log('WARNING, log not read!');
    }

    /**
     * @returns {Object} map from column name to array data.
     */
    getColumnData() {
        const cols = {};
        for (const key of Object.keys(this.columnMap)) {
            cols[key] = this.getColumn(key);
        }
        cols.timestamp = cols.row_seconds.map((v) => {
            // Javascript timestamp is in ms, unix is in seconds.
            return 1000.0 * v + Number(this.timestamp);
        });
        return cols;
    }

    /**
     * Get the instrument log data for a column
     * @param {string} columnName
     * @returns {Number}
    */
    getColumn(columnName) {
        if (this.data) {
            const columnNumber = this.columnMap[columnName];
            if (columnNumber != null) {
                const colOffset = columnNumber * this.rows;
                return fullSlice(this.data, colOffset, colOffset + this.rows, this.skip);
            }
        }
        console.log('WARNING, log not read!');
    }

    /**
     * Returns a "trailing window" of log data for a given time
     * @param {Number} timeMS
     * @param {string} columnName
     * @param {Number} timeWindowMS
     * @returns {Array}
     */
    getHistory(timeMS, columnName, timeWindowMS) {
        if (this.data) {
            const columnNumber = this.columnMap[columnName];
            if (columnNumber) {
                const rowEnd = Math.round(timeMS / this.deltaT); // One row every deltaT
                const rowStart = Math.round(Math.max(0, timeMS - timeWindowMS) / this.deltaT);
                const colOffset = columnNumber * this.rows;
                return fullSlice(this.data, colOffset + rowStart, colOffset + rowEnd, this.skip);
            }
        }
        console.log('WARNING, log not read!');
    }
}
