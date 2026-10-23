
import m from 'mithril';
import * as utils from './utils';

/**
 * Create a Bulma button link.
 * @param {*} link
 * @param {*} text
 * @returns {*}
 */
function buttonLink(link, text) {
    return m('a', {
        class: 'button is-rounded is-link', // foobar
        href: link,
    },
    text);
}

/**
 * Create a link to the graphing page for this log.
 * @param {Object} logEntry
 * @returns {*}
 */
function outLinks(logEntry) {
    const metadata = logEntry.metadata;
    const elements = [];
    if (logEntry['video_files']) {
        const videoFile = logEntry['video_files'][0]['SourceFile'];
        const base = videoFile.replace('.MP4', '');
        const url = `./sailview.html?video=${base}`;
        elements.push(buttonLink(url, 'Video'));
    }
    if (metadata.file) {
        const base = metadata.file.replace('.pd.gz', '');
        const url = `./graphs.html?logfile=${base}`;
        elements.push(buttonLink(url, 'Log Graph'));
    }
    return elements;
}

/**
 * Create a card which shows the date, title, and metadata for a log record.
 * @param {Object} logEntry
 * @param {boolean} isContentHidden
 * @returns {*}
 */
function makeCard(logEntry, isContentHidden) {
    const metadata = logEntry.metadata;
    const isHidden = isContentHidden ? ' is-hidden' : '';
    const mainTitles = 'description conditions performance learnings'.split(' ');
    const information = [];
    for (const key of mainTitles) {
        if (metadata[key]) {
            information.push(m('h4', utils.capitalize(key)));
            information.push(m('p', metadata[key]));
        }
    }
    const rest = [];
    for (const [key, value] of Object.entries(metadata)) {
        if (mainTitles.indexOf(key) == -1) {
            rest.push(m('li', [
                m('strong', key),
                m.trust(':&nbsp;&nbsp;'),
                String(value)]));
        }
    }
    information.push(m('ul', rest));

    // Sometimes the title is just the date!
    const title = metadata.date + ((metadata.date === metadata.title) ? '' : ': ' + metadata.title);
    const res =
        m('div', {class: 'card is-fullwidth'},
            m('header.card-header',
                m('p', {class: 'card-header-title card-toggle'}, title),
                ...outLinks(logEntry),
                m('a', {class: 'card-header-icon card-toggle'},
                    m('i', {class: 'fa fa-angle-down'}))),
            m('div', {class: 'card-content' + isHidden},
                m('div.content', information)));
    return res;
}

/**
 * Create a set of cards which show race info.
 * @param {*} logs
 * @param {*} isContentHidden
 * @returns {*}
 */
export function raceCards(logs, isContentHidden) {
    const cards = [];
    for (const log of logs) {
        const md = log.metadata;
        if (md) {
            cards.push(makeCard(log, isContentHidden));
        }
    }
    return cards;
}
