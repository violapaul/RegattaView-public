// Hierarchical video picker for sailview: replaces a flat <select> with a
// year > month > video tree. Most recent year and last 2 months are open
// by default; older years stay collapsed. The picker keeps its own DOM
// state and exposes setSelected() so external callers (next-video advance,
// playlist seek, share-link load) can sync the visible selection.

const MONTH_ABBR = [
    'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
    'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
];

// CreateDate comes in two flavors:
//   "2020:04:04 10:01:10"  (EXIF colon form, older VIRB-era records)
//   "2026/04/29 19:36:06"  (slash form, modern Insta360 records)
// Both are fixed-width "YYYY?MM?DD HH:MM:SS", so slice by index.
function parseDate(s) {
    if (!s || s.length < 16) return null;
    return {
        year: s.slice(0, 4),
        month: s.slice(5, 7),
        day: s.slice(8, 10),
        time: s.slice(11, 16),
    };
}

function shortLabel(record) {
    const d = parseDate(record.CreateDate);
    if (!d) return record.basename || '?';
    const month = MONTH_ABBR[parseInt(d.month, 10) - 1] || d.month;
    const log = record.LogFile ? ' *' : '';
    const audioOnly = record.AudioOnly ? ' (audio only)' : '';
    return `${month} ${d.day}, ${d.year} ${d.time}${log}${audioOnly}`;
}

export function buildVideoPicker(root, videos, onSelect) {
    const toggle = root.querySelector('.video-picker-toggle');
    const label = root.querySelector('.video-picker-label');
    const panel = root.querySelector('.video-picker-panel');

    // Group videos as year -> month -> day -> [video, ...]. Insertion
    // order matters: years/months/days are sorted ascending so the most
    // recent entries land at the bottom (closest to the toggle button).
    const byYear = new Map();
    for (const v of videos) {
        const d = parseDate(v.CreateDate);
        if (!d) continue;
        if (!byYear.has(d.year)) byYear.set(d.year, new Map());
        const months = byYear.get(d.year);
        if (!months.has(d.month)) months.set(d.month, new Map());
        const days = months.get(d.month);
        if (!days.has(d.day)) days.set(d.day, []);
        days.get(d.day).push(v);
    }

    const years = [...byYear.keys()].sort();
    if (years.length === 0) return { setSelected() {} };
    const latestYear = years[years.length - 1];

    // "Last 2 months" is computed across all years so we don't accidentally
    // open Jan + Feb of the latest year when the most recent activity was
    // last December.
    const allYM = [];
    byYear.forEach((months, y) => months.forEach((_, m) => allYM.push(`${y}-${m}`)));
    allYM.sort();
    const openMonths = new Set(allYM.slice(-2));

    const monthCount = (months) => [...months.values()]
        .reduce((acc, days) => acc + [...days.values()].reduce((a, l) => a + l.length, 0), 0);

    // "Today" key (local time, matches CreateDate which is also local).
    // Only the day matching this auto-opens — every other day stays
    // collapsed so the panel doesn't avalanche when a month has many
    // sailing days.
    const now = new Date();
    const todayKey = `${now.getFullYear()}-`
        + `${String(now.getMonth() + 1).padStart(2, '0')}-`
        + `${String(now.getDate()).padStart(2, '0')}`;

    panel.innerHTML = '';
    for (const y of years) {
        const yDetails = document.createElement('details');
        yDetails.className = 'vp-year';
        if (y === latestYear) yDetails.open = true;
        const ySummary = document.createElement('summary');
        const months = byYear.get(y);
        const yCount = monthCount(months);
        ySummary.innerHTML =
            `<span class="vp-name">${y}</span>` +
            `<span class="vp-count">${yCount}</span>`;
        yDetails.appendChild(ySummary);

        const monthKeys = [...months.keys()].sort();
        for (const m of monthKeys) {
            const monthOpen = openMonths.has(`${y}-${m}`);
            const monthAbbr = MONTH_ABBR[parseInt(m, 10) - 1] || m;
            const days = months.get(m);
            const mCount = [...days.values()].reduce((a, l) => a + l.length, 0);

            const mDetails = document.createElement('details');
            mDetails.className = 'vp-month';
            if (monthOpen) mDetails.open = true;
            const mSummary = document.createElement('summary');
            mSummary.innerHTML =
                `<span class="vp-name">${monthAbbr}</span>` +
                `<span class="vp-count">${mCount}</span>`;
            mDetails.appendChild(mSummary);

            const dayKeys = [...days.keys()].sort();
            for (const day of dayKeys) {
                const list = days.get(day);
                const dDetails = document.createElement('details');
                dDetails.className = 'vp-day';
                // Only today auto-opens; every other day stays collapsed
                // so the panel reads as a compact list of sail-days.
                if (`${y}-${m}-${day}` === todayKey) dDetails.open = true;
                const dSummary = document.createElement('summary');
                dSummary.innerHTML =
                    `<span class="vp-name">${monthAbbr} ${day}</span>` +
                    `<span class="vp-count">${list.length}</span>`;
                dDetails.appendChild(dSummary);

                for (const v of list) {
                    const pd = parseDate(v.CreateDate);
                    const item = document.createElement('button');
                    item.type = 'button';
                    item.className = 'vp-item';
                    item.dataset.index = String(v.index);
                    const log = v.LogFile ? '<span class="vp-log">log</span>' : '';
                    const audio = v.AudioOnly ? '<span class="vp-log">audio</span>' : '';
                    item.innerHTML =
                        `<span class="vp-when">${pd.time}</span>${audio}${log}`;
                    item.title = v.basename || v.SourceFile || '';
                    item.addEventListener('click', () => {
                        closePicker();
                        onSelect(v);
                    });
                    dDetails.appendChild(item);
                }
                mDetails.appendChild(dDetails);
            }
            yDetails.appendChild(mDetails);
        }
        panel.appendChild(yDetails);
    }

    function openPicker() {
        panel.hidden = false;
        root.classList.add('open');
        const sel = panel.querySelector('.vp-item.selected');
        if (sel) sel.scrollIntoView({ block: 'center' });
    }
    function closePicker() {
        panel.hidden = true;
        root.classList.remove('open');
    }

    toggle.addEventListener('click', (e) => {
        e.stopPropagation();
        if (panel.hidden) openPicker(); else closePicker();
    });
    panel.addEventListener('click', (e) => e.stopPropagation());
    document.addEventListener('click', (e) => {
        if (!root.contains(e.target)) closePicker();
    });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && !panel.hidden) closePicker();
    });

    function setSelected(record) {
        if (!record) return;
        panel.querySelectorAll('.vp-item.selected')
            .forEach((el) => el.classList.remove('selected'));
        const sel = panel.querySelector(`.vp-item[data-index="${record.index}"]`);
        if (sel) {
            sel.classList.add('selected');
            // Walk up and open ancestor <details> so the selection is
            // findable when the user opens the panel.
            let p = sel.parentElement;
            while (p && p !== panel) {
                if (p.tagName === 'DETAILS') p.open = true;
                p = p.parentElement;
            }
        }
        label.textContent = shortLabel(record);
        toggle.title = `Select Video — ${record.basename || ''}`;
    }

    return { setSelected };
}
