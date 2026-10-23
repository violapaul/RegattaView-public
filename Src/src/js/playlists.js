import * as shared from './shared';
import * as utils from './utils';
import m from 'mithril';

const state = {
    playlists: [],
    events: {},
};

const EVENT_TYPE_LABELS = {
    tack: 'Tacks',
    gybe: 'Gybes',
    start: 'Starts',
    finish: 'Finishes',
    rounding: 'Roundings',
    mark: 'Marks',
    situation: 'Situations',
    other: 'Other',
};

function countEventTypes(playlist, allEvents) {
    const counts = {};
    (playlist.events || []).forEach((eid) => {
        const ev = allEvents[eid];
        if (ev) {
            counts[ev.type] = (counts[ev.type] || 0) + 1;
        }
    });
    return counts;
}

function typeSummary(counts) {
    return Object.entries(counts)
        .map(([type, count]) => `${count} ${EVENT_TYPE_LABELS[type] || type}`)
        .join(', ');
}

function makeCard(playlist, allEvents) {
    const eventCount = (playlist.events || []).length;
    const counts = countEventTypes(playlist, allEvents);
    const summary = typeSummary(counts);
    const sourceTag = playlist.source === 'auto'
        ? m('span.tag.is-info.is-light', 'auto')
        : m('span.tag.is-success.is-light', 'curated');

    const playUrl = `sailview.html?playlist=${encodeURIComponent(playlist.id)}`;

    return m('.card', {style: 'margin-bottom: 1.5rem;'},
        m('header.card-header',
            m('p.card-header-title', [
                playlist.title,
                m('span', {style: 'margin-left: 8px;'}, sourceTag),
            ]),
            m('a.card-header-icon', {href: playUrl},
                m('span.icon', m('i.fas.fa-play-circle'))),
        ),
        m('.card-content',
            m('.content', [
                playlist.date ? m('p', [
                    m('strong', 'Date: '),
                    playlist.date,
                    ` \u2014 ${eventCount} events`,
                ]) : m('p', `${eventCount} events`),
                summary ? m('p.has-text-grey', summary) : null,
                playlist.description ? m('p', playlist.description) : null,
                m('a.button.is-rounded.is-link.is-small', {href: playUrl}, 'Play'),
            ]),
        ),
    );
}

function renderPlaylists(playlists, allEvents) {
    const container = document.getElementById('playlistCards');
    const loading = document.getElementById('loadingMessage');
    loading.style.display = 'none';

    if (playlists.length === 0) {
        m.render(container, m('p.has-text-grey', 'No playlists found. Run generate_events.py or mark events in the viewer.'));
        return;
    }

    const byDate = {};
    const noDate = [];
    playlists.forEach((pl) => {
        if (pl.date) {
            if (!byDate[pl.date]) byDate[pl.date] = [];
            byDate[pl.date].push(pl);
        } else {
            noDate.push(pl);
        }
    });

    const sections = [];
    const sortedDates = Object.keys(byDate).sort().reverse();

    sortedDates.forEach((date) => {
        sections.push(m('h3.title.is-4', {style: 'margin-top: 2rem;'}, date));
        byDate[date].forEach((pl) => {
            sections.push(makeCard(pl, allEvents));
        });
    });

    if (noDate.length > 0) {
        sections.push(m('h3.title.is-4', {style: 'margin-top: 2rem;'}, 'All Days'));
        noDate.forEach((pl) => {
            sections.push(makeCard(pl, allEvents));
        });
    }

    m.render(container, m('div', sections));
}

function loadData() {
    if (!shared.checkAuthorization()) {
        utils.redirectWithParams('login.html', window.location.pathname);
        return;
    }

    const autoUrl = shared.signAWSUrl('Insta360/playlists.json');

    let pending = 2;
    const allEvents = {};
    let allPlaylists = [];

    function done() {
        pending--;
        if (pending <= 0) {
            renderPlaylists(allPlaylists, allEvents);
        }
    }

    // Playlists are currently unused — superseded by review.html's
    // event-based flow. Signed-S3 with BlockPublicAccess returns 403 (not
    // 404) for missing keys, so accept both as "absent" and stay quiet.
    const ABSENT = {optionalStatuses: [403, 404]};
    utils.fetchJSONOptional(autoUrl, null, ABSENT).then((data) => {
        if (data) {
            (data.events || []).forEach((e) => { allEvents[e.id] = e; });
            allPlaylists = allPlaylists.concat(data.playlists || []);
        }
    }).catch((err) => {
        utils.surfaceError(`Auto playlists failed to load: ${err.message}`, {key: 'auto-playlists'});
    }).finally(done);

    shared.readDataJSON('Insta360/curated.json').then((data) => {
        if (data) {
            (data.events || []).forEach((e) => { allEvents[e.id] = e; });
            allPlaylists = allPlaylists.concat(data.playlists || []);
        }
    }).catch((err) => {
        utils.surfaceError(`Curated playlists failed to load: ${err.message}`, {key: 'curated-playlists'});
    }).finally(done);
}

loadData();
