/* eslint-disable require-jsdoc */
// import {map} from '@bokeh/bokehjs/build/js/lib/api/linalg';

import * as palettes from '@bokeh/bokehjs/build/js/lib/api/palettes';
import {InstrumentLog} from './instrument_log';
import * as utils from './utils';
import * as shared from './shared';
import * as ux from './shared_ux';
import m from 'mithril';
import {Chart} from './chart';

// const m = require˜('mithril');
const bokeh = require('@bokeh/bokehjs');
bokeh.set_log_level('info');

function num2hexcolor(num) {
    return '#' + Number(num).toString(16).padStart(6, '0');
}
function hexcolor2rgb(color) {
    const r = parseInt(color.substr(1, 2), 16);
    const g = parseInt(color.substr(3, 2), 16);
    const b = parseInt(color.substr(5, 2), 16);
    return [r, g, b];
}
function is_dark([r, g, b]) {
    const l = 1 - (0.299 * r + 0.587 * g + 0.114 * b) / 255;
    return l >= 0.6;
}
function norm_palette(palette = 'Spectral11') {
    if (Array.isArray(palette)) {
        return palette;
    } else {
        const p = palettes[palette];
        return p.map(num2hexcolor);
    }
}
const state = {
    raceData: null, // data which describes the available videos and metadata columns
    logBase: utils.valOr(utils.urlParameters().logfile, '2021-03-06_09:39'),
};

function logBinaryFile(base) {
    return base + '.bin';
}

function logPandasFile(base) {
    return base + '.pd.gz';
}

function findMatchingLog(logs, basename) {
    const pandasFile = logPandasFile(basename);
    for (const log of logs) {
        if (log.LogFile == pandasFile) {
            return log;
        }
    }
    return null;
}

const plotParams = {
    tools: 'pan,xwheel_zoom,box_zoom,reset,save',
    height: 200,
    sizing_mode: 'stretch_width',
    width_policy: 'max',
    lod_factor: 1000,
    x_axis_type: 'datetime',
    // Bokeh 2's regl backend compiles JavaScript dynamically. Canvas preserves
    // the plots/interactions without requiring unsafe-eval in the site's CSP.
    output_backend: 'canvas',
};

const plotColumns = [
    ['spd', 'sog'],
    ['twa', 'awa'],
    ['tws', 'aws'],
    ['hdg', 'cog'],
    ['zg100_roll', 'rudder'],
];

function makeHoverTool(colNames) {
    const formatters = {'@timestamp': 'datetime'};
    let tip = '@timestamp{%H:%M:%S}';
    colNames.forEach((col) => {
        formatters['@' + col] = 'printf';
        tip = tip + ', @' + col + '{%.1f}';
    });
    const params = {
        formatters: formatters,
        tooltips: [['', tip]],
    };
    return new bokeh.HoverTool(params);
}

const colors = norm_palette('Set1_9');

function loadLogData(logFile) {
    state.logData = new InstrumentLog({
        logDir: 'FullLogs/',
        timestamp: 0.0,
        skip: 10, // downsample the data, raw data is 10/sec
        logFile: logFile,
        columnMap: state.raceData.columns,
    });


    state.logData.load(() => {
        const columnData = state.logData.getColumnData();
        const chart = new Chart(columnData);

        const tapThrottler = utils.throttler(100);
        function onTap(e) {
            if (tapThrottler()) {
                console.log('OnTap', e);
                const index = utils.binarySearch(columnData.timestamp, e.x);
                chart.updatePoint(index);
                const from = utils.binarySearch(columnData.timestamp, e.origin.x_range.start);
                const to = utils.binarySearch(columnData.timestamp, e.origin.x_range.end);
                console.log('OnPan', from, to);
                chart.updateSegment(from, to);
            }
        }

        const xrangeThrottler = utils.throttler(100);
        function xrangeCallback(figure) {
            return () => {
                if (xrangeThrottler()) {
                    console.log('on xrange change');
                    const from = utils.binarySearch(columnData.timestamp, figure.x_range.start);
                    const to = utils.binarySearch(columnData.timestamp, figure.x_range.end);
                    chart.updateSegment(from, to);
                }
            };
        }


        state.bokehData = new bokeh.ColumnDataSource({data: columnData});

        state.figures = [];
        let figure = null;
        for (const colNames of plotColumns) {
            if (state.figures.length == 0) {
                figure = bokeh.Plotting.figure(plotParams);
                state.figures.push(figure);
            } else {
                figure = bokeh.Plotting.figure({
                    x_range: state.figures[0].x_range,
                    ...plotParams,
                });
                state.figures.push(figure);
            }
            colNames.forEach((col, index) => {
                console.log(col + ', ' + index);
                figure.line({field: 'timestamp'}, {field: col}, {
                    source: state.bokehData,
                    line_width: 2,
                    legend: col + ':',
                    color: colors[index],
                });
            });
            figure.legend.click_policy = 'hide';
            figure.add_tools(makeHoverTool(colNames));

            figure.js_event_callbacks['tap'] = [{execute: onTap}];
            figure.x_range.on_change(figure.x_range, xrangeCallback(figure));
        }
        const column = gridPlot(state.figures);

        // show the plot, appending it to the end of the current section
        bokeh.Plotting.show(column, '#graphs').then((pv) => {
            window.stuff.pv = pv;
        });
    });
}

function gridPlot(figures) {
    const figLists = figures.map((f) => {
        return [f];
    });
    return bokeh.Plotting.gridplot(figLists,
        {
            sizing_mode: 'stretch_width',
        });
}

window.stuff = state;

// Load the JSON data that describes available log data.
shared.getMetadata((error, data) => {
    if (error !== null) {
        console.log('Something went wrong: ' + error);
    } else {
        state.raceData = data;
        const matchingLog = findMatchingLog(data.logs, state.logBase);
        if (matchingLog) {
            const dataRows = document.getElementById('datarows');
            m.render(dataRows, m('div', ...ux.raceCards([matchingLog], false)));
        }
        loadLogData(logBinaryFile(state.logBase));
    }
});


