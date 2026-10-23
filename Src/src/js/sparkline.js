/* eslint-disable valid-jsdoc */

import {vec2} from 'gl-matrix';


/**
 * Convert value to a normalized coordinate between zero and height.
 * @param {Number} max - max value taken on
 * @param {Number} height - height
 * @param {Number} offset - offset
 * @param {Number} value - value to convert
 * @returns {Number}
 */
function getY(max, height, offset, value) {
    return height * (1 - (value / max)) + offset;
}

/**
 * Remove the children of this element
 * @param {*} svg
 */
function removeChildren(svg) {
    while (svg.firstChild) svg.removeChild(svg.firstChild);
}

/**
 * build an html element is attributes
 * @param {*} tag
 * @param {*} attrs
 * @returns {*}
 */
function buildElement(tag, attrs) {
    const element = document.createElementNS('http://www.w3.org/2000/svg', tag);

    Object.keys(attrs).forEach((name) => {
        element.setAttribute(name, attrs[name]);
    });
    return element;
}

/**
 * Create a sparkline graph as an SVG
 * @param {*} svg
 * @param {*} values
 * @param {*} options
 */
export function sparkline(svg, values) {
    removeChildren(svg);

    // Define how big should be the spot area.
    const spotRadius = 2;
    const spotDiameter = spotRadius * 2;

    // width and height
    const rect = svg.getBoundingClientRect();
    const width = rect.width;
    const fullHeight = rect.height;
    const height = fullHeight - spotDiameter;

    // The maximum value. This is used to calculate the Y coord of
    // each sparkline datapoint.
    const min = Math.min(...values);
    const max = Math.max(...values);
    const range = Math.max(0.3, max - min);

    // Cache the last item index.
    const lastItemIndex = values.length - 1;

    // Calculate the X coord base step.
    const offset = width / lastItemIndex;

    // Hold the line coordinates.
    const pathY = getY(range, height, spotRadius, values[0] - min);
    let pathCoords = `M${spotDiameter} ${pathY}`;

    values.forEach((value, index) => {
        const x = index * offset + spotDiameter;
        const y = getY(range, height, spotRadius, value - min);

        pathCoords += ` L ${x} ${y}`;
    });

    const path = buildElement('path', {
        class: 'sparkline--line',
        d: pathCoords,
        fill: 'none',
    });

    const fillCoords = `${pathCoords} V ${fullHeight} L ${spotDiameter} ${fullHeight} Z`;

    const fill = buildElement('path', {
        class: 'sparkline--fill',
        d: fillCoords,
        stroke: 'none',
    });

    svg.appendChild(fill);
    svg.appendChild(path);
}


/**
 * Compute the range and stats of entries.
 * @param {*} entries
 * @returns {Object}
 */
function range(entries) {
    const res = {
        min: Math.min(...entries),
        max: Math.max(...entries),
    };
    res.span = res.max - res.min;
    res.center = (res.max + res.min) / 2;
    return res;
}


/** helper to construct vec2 */
function v2(a, b) {
    return vec2.fromValues(a, b);
}

/** scales a vec2 */
function v2scale(v, s) {
    return vec2.scale(vec2.create(), v, s);
}

/** subtracts a vec2 */
function v2sub(v1, v2) {
    return vec2.sub(vec2.create(), v1, v2);
}

/** Convert a list to a string with seperator */
function str(list, sep = ' ') {
    let res = null;
    for (const e of list) {
        if (res) {
            res += sep + String(e);
        } else {
            res = String(e);
        }
    }
    return res;
}

/**
 *
 * @param {*} svg
 * @param {*} entriesX
 * @param {*} entriesY
 * @param {*} options
 * @returns {*}
 */
export function map(svg, entriesY, entriesX, options) {
    removeChildren(svg);

    // Define how big should be the spot area.
    const spotRadius = 0.02;

    // Get the SVG element's full height.
    // This is used
    const size = v2(
        parseFloat(svg.attributes.height.value),
        parseFloat(svg.attributes.width.value),
    );
    const minSize = Math.min(size[0], size[1]);
    const scaled = v2scale(size, 1.2 / minSize);
    const centered = v2sub(scaled, v2scale(scaled, 0.5));

    svg.setAttribute('viewBox', str([-centered[0], -centered[1], scaled[0], scaled[1]]));
    svg.setAttribute('stroke-width', '0.01');
    svg.setAttribute('transform', 'scale(1, -1)');

    // Find the range of each coordinate so they can be plotted on the screen
    const rangeX = range(entriesX);
    const rangeY = range(entriesY);
    // Span of the largest determines the scale
    rangeX.span = rangeY.span = Math.max(rangeX.span, rangeY.span);

    /**
     * Normalize val so it lies in side of size with border
     * @param {*} val
     * @param {*} range
      * @returns {Number}
     */
    function normalize(valx, valy) {
        return [
            (valx - rangeX.center) / rangeX.span,
            (rangeY.center - valy) / rangeY.span,
        ];
    }
    let pt = normalize(entriesX[0], entriesY[0]);
    let pathCoords = `M${pt[0]} ${pt[1]}`;

    entriesX.forEach((xraw, index) => {
        const yraw = entriesY[index];
        pt = normalize(xraw, yraw);
        pathCoords += ` L ${pt[0]} ${pt[1]}`;
    });

    const path = buildElement('path', {
        class: 'sparkline--line',
        d: pathCoords,
        fill: 'none',
    });

    svg.appendChild(path);

    const spot = buildElement('circle', {
        class: 'map_spot',
        id: 'map_spot',
        cx: 0.0,
        cy: 0.0,
        r: spotRadius,
    });

    svg.appendChild(spot);

    return function(yraw, xraw) {
        const pt = normalize(xraw, yraw);
        spot.setAttribute('cx', pt[0]);
        spot.setAttribute('cy', pt[1]);
    };
}

// The outline of a J105 like boat.  Its 10.5 meters long
const BOAT_OUTLINE = [
    [0.0, 1.1604, 1.5478, 1.6176, 1.3146, 0.6837, 0.0, 0.0, -0.6837, -1.3146, -1.6176, -1.5478, -1.1604, 0.0],
    [-4.8956, -4.8956, -2.0036, 0.0798, 2.1243, 4.007, 5.5836, 5.5836, 4.007, 2.1243, 0.0798, -2.0036, -4.8956, -4.8956],
];

/**
 *
 */
export class BoatView {
    /**
     *
     * @param {*} svg
     * @param {*} entriesY
     * @param {*} entriesX
     * @param {*} headings
     * @param {*} options
     */
    constructor(svg, entriesY, entriesX, headings, options) {
        this.svg = svg;
        this.entriesY = entriesY;
        this.entriesX = entriesX;
        this.headings = headings;
        this.options = options || {};
        this.currentPos = null;
        this.currentHeading = null;
        this.zoomLevel = 1;
        this.windUpDeg = null;
        // Magnetic declination applied to `heading` when rendering on a
        // true-frame map (Leaflet). Caller supplies the session's
        // variation (per binary-log column, sourced from G.MAGNETIC_VARIATION
        // at .bin write time). Pre-Stage-4 this was a hardcoded +15.4°;
        // the fallback preserves legacy .bin behavior for old files that
        // don't carry the variation column.
        this.variation = (options && options.variation != null)
            ? options.variation
            : 15.4;
        // When rendering in a wind-relative (wind-up) scene, the caller
        // has already pre-subtracted TWD from HDG (both same frame), so
        // the displayed angle is frame-free and should NOT get the
        // magnetic→true correction applied on top. Callers set
        // `absoluteFrame: false` for wind-up, or leave it true/unset for
        // a north-up Leaflet map.
        this.absoluteFrame = (options && options.absoluteFrame === false)
            ? false
            : true;
        this.init();
    }

    /**
     * Make the rendered scene "wind-up": rotate the SVG so the true wind
     * direction (`twd`, compass degrees where the wind comes FROM) appears
     * at the top of the viewport. Pass null to disable.
     */
    setWindUp(twd) {
        this.windUpDeg = (twd == null || isNaN(twd)) ? null : twd;
        this._applySvgTransform();
    }

    _applySvgTransform() {
        if (!this.scene) return;
        // Data is (east, north) in meters; SVG viewBox is y-DOWN, so we flip
        // y to get north pointing up on screen. The wind-up rotation is then
        // applied around the visible viewBox center.
        //
        // The flip/rotate center MUST be stable (viewBox center) -- if it
        // tracked currentPos, the scene would shift every time the boat
        // moved and break wind-up orientation.
        //
        // Order in an SVG `transform=` attribute is applied right-to-left
        // when transforming a point, so the chain below reads:
        //     1. translate(-cx,-cy)  (move center to origin)
        //     2. scale(1,-1)         (flip y)
        //     3. rotate(-twd)        (wind-up)
        //     4. translate(cx,cy)    (move back)
        const vb = this.svg.viewBox && this.svg.viewBox.baseVal;
        const cx = vb ? vb.x + vb.width / 2 : this.minX + this.maxWidth / 2;
        const cy = vb ? vb.y + vb.height / 2 : this.minY + this.maxWidth / 2;
        const rot = (this.windUpDeg != null) ? -this.windUpDeg : 0;
        const t = `translate(${cx}, ${cy}) rotate(${rot}) scale(1, -1) translate(${-cx}, ${-cy})`;
        this.scene.setAttribute('transform', t);
    }

    setViewBox(x, y, width, height, strokeWidth) {
        this.svg.setAttribute('viewBox', str([x, y, width, height]));
        if (strokeWidth != null) {
            this.svg.setAttribute('stroke-width', String(strokeWidth));
        }
        // Keep the wind-up transform anchored to the visible viewport rather
        // than the original dataset bounds.
        this._applySvgTransform();
    }

    /**
     *
     */
    init() {
        removeChildren(this.svg);

        const rangeX = range(this.entriesX);
        const rangeY = range(this.entriesY);

        this.maxWidth = 1.2 * Math.max(rangeX.span, rangeY.span);
        this.minX = rangeX.center - this.maxWidth / 2;
        this.minY = rangeY.center - this.maxWidth / 2;

        this.setViewBox(this.minX, this.minY, this.maxWidth, this.maxWidth, this.maxWidth / 100);

        // All content lives in an inner <g> so we can safely apply the y-flip
        // (and any wind-up rotation) using an SVG 1.1 transform. Applying the
        // transform to the outer <svg> is SVG 2 only, and flips around the
        // user-space origin (0,0) which is nowhere near the viewBox center.
        this.scene = this.buildElement('g', {class: 'boatview-scene'});
        this.svg.appendChild(this.scene);

        this.boat = this.buildElement('path', {
            class: 'boat',
            d: this.makeCoords(BOAT_OUTLINE[0], BOAT_OUTLINE[1]),
            fill: 'none',
        });

        this.track = this.buildElement('path', {
            class: 'sparkline--line',
            d: this.makeCoords(this.entriesX, this.entriesY),
            fill: 'none',
        });

        this.startingLine = this.buildElement('path', {
            class: 'startingLine',
            d: this.makeCoords([0, 0], [0, 0]),
            fill: 'none',
        });

        this.markCircle = this.buildElement('circle', {
            class: 'markCircle',
            cx: 0,
            cy: 0,
            r: 40,
            fill: 'none',
            transform: `translate(${this.entriesX[0]}, ${this.entriesY[0]})`,
        });

        this.scene.appendChild(this.markCircle);
        this.scene.appendChild(this.startingLine);
        this.scene.appendChild(this.track);
        this.scene.appendChild(this.boat);

        this._applySvgTransform();
    }

    /**
     *
     * @param {*} xs
     * @param {*} ys
     * @returns
     */
    makeCoords(xs, ys) {
        let pathCoords = `M ${xs[0]} ${ys[0]}`;

        xs.forEach((x, index) => {
            const y = ys[index];
            pathCoords += ` L ${x} ${y}`;
        });

        return pathCoords;
    }

    /**
     *
     * @param {*} tag
     * @param {*} attrs
     * @returns
     */
    buildElement(tag, attrs) {
        const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
        // enumerate keys in attrs and set them on the element
        Object.keys(attrs).forEach((key) => {
            el.setAttribute(key, attrs[key]);
        });
        return el;
    }

    compassOffset(bearingDeg, length) {
        const rad = bearingDeg * Math.PI / 180;
        return [length * Math.sin(rad), length * Math.cos(rad)];
    }

    /**
     *
     * @param {*} yraw
     * @param {*} xraw
     * @param {*} heading
     */
    update(yraw, xraw, heading) {
        this.currentPos = [xraw, yraw];
        this.currentHeading = heading;
        // The scene transform (y-flip + optional wind-up rotate) is stable;
        // only the boat glyph's position/heading changes per frame.
        //
        // Absolute (north-up) frame: `heading` is magnetic (from the .bin's
        //   `hdg` column, sourced from PGN 127250 Reference=Magnetic on
        //   Peer Gynt). The Leaflet/map coordinate system is true, so we
        //   add the session's magnetic variation to rotate into true.
        //
        // Wind-relative frame: caller has already subtracted TWD from HDG
        //   (both in the same magnetic frame), so the displayed value is
        //   frame-free. Do not re-apply variation.
        const scale = this.options.boatScale || 1;
        const correction = this.absoluteFrame ? this.variation : 0;
        this.boat.setAttribute(
            'transform',
            `translate(${xraw}, ${yraw}) rotate(${-(heading + correction)}) scale(${scale})`,
        );
    }

    /**
     *
     * @param {*} lineOrientation
     */
    start(lineOrientation) {
        if (!this.currentPos) return;
        const [cx, cy] = this.currentPos;
        const lineHalf = this.maxWidth * 0.28;
        const [ldx, ldy] = this.compassOffset(lineOrientation + 90, lineHalf);

        this.startingLine.setAttribute('d', this.makeCoords(
            [cx - ldx, cx + ldx],
            [cy - ldy, cy + ldy],
        ));
        this.markCircle.setAttribute(
            'transform',
            `translate(${this.currentPos[0]}, ${this.currentPos[1]}) rotate(0)`,
        );

        if (!this.options.fixedViewport) {
            this.zoomAndCenter();
        }
    }

    /**
     *
     * @param {*} upOrDown
     */
    zoom(upOrDown) {
        if (this.zoomLevel < 33) {
            this.zoomLevel = this.zoomLevel * 2;
            this.zoomAndCenter();
        } else {
            this.setViewBox(this.minX, this.minY, this.maxWidth, this.maxWidth, this.maxWidth / 100);
            this.zoomLevel = 1;
            this.zoomAndCenter();
        }
    }

    /** Zoom and center the viewport. */
    zoomAndCenter() {
        if (!this.currentPos) return;
        const width = this.maxWidth / (2 * this.zoomLevel);
        this.markCircle.setAttribute(
            'transform',
            `translate(${this.currentPos[0]}, ${this.currentPos[1]}) rotate(${0})`,
        );
        this.setViewBox(
            this.currentPos[0] - width,
            this.currentPos[1] - width,
            2 * width,
            2 * width,
            width / 50,
        );
    }
}


export default sparkline;
