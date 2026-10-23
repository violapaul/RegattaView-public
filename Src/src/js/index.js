/* eslint-disable require-jsdoc */
// import {map} from '@bokeh/bokehjs/build/js/lib/api/linalg';

import * as shared from './shared';
import * as ux from './shared_ux';
import m from 'mithril';
// import { NoEmitOnErrorsPlugin } from 'webpack';

const state = {
    raceData: null,
};

function addOpenners() {
    const cardToggles = document.getElementsByClassName('card-toggle');
    for (let i = 0; i < cardToggles.length; i++) {
        cardToggles[i].addEventListener('click', (e) => {
            e.currentTarget.parentElement.parentElement.childNodes[1].classList.toggle('is-hidden');
        });
    }
}

// Load the JSON data that describes available race data.
shared.getMetadata((error, data) => {
    if (error !== null) {
        alert('Something went wrong loading log file: ' + error);
    } else {
        state.raceData = data;
        const dataRows = document.getElementById('datarows');
        const reverseLogs = data.logs.reverse();
        m.render(dataRows, m('div', ...ux.raceCards(reverseLogs.slice(0, 1000), true)));
        addOpenners();
    }
});

