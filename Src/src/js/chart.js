/* eslint-disable require-jsdoc */

import * as L from 'leaflet';

export class Chart {
    constructor(columnData, skip = 2) {
        this.columnData = columnData;
        this.skip = skip;

        this.mymap = L.map('mapid');

        this.lat = columnData.latitude;
        this.lon = columnData.longitude;
        this.track = this.extractTrack(0, this.lat.length);

/*         L.tileLayer('https://tile.openstreetmap.org/ {z}/ {x}/ {y}.png', {
            maxZoom: 19,
            attribution: '© <a href="http://www.openstreetmap.org/copyright">OpenStreetMap</a>'
        }).addTo(this.mymap);
 */
        // const noaaTiles = 'https://tileservice.charts.noaa.gov/tiles/50000_1/{z}/{x}/{y}.png';
        const noaaTiles = '/www/SeattleTiles/{z}/{x}/{y}.png';

        L.tileLayer(noaaTiles, {
            maxZoom: 15,
            attribution: 'Foobar',
            id: 'mapbox/streets-v11',
            tileSize: 512,
            zoomOffset: -1,
            detectRetina: true,
            minNativeZoom: 12,
            opacity: 0.5,
        }).addTo(this.mymap);

        this.trackLine = L.polyline(this.track, {
            color: 'green',
            weight: 3,
            smoothFactor: 1,
        }).addTo(this.mymap);

        this.segmentLine = L.polyline([], {
            color: 'orange',
            weight: 3,
            smoothFactor: 1,
        });
        this.segmentLine.addTo(this.mymap);

        this.marker = L.circleMarker(this.track[0], {radius: 5, color: 'red'}).addTo(this.mymap);

        this.mymap.fitBounds(this.trackLine.getBounds());
    }

    extractTrack(indexFrom, indexTo) {
        const track = [];
        // collect data for plotting a track
        for (let i = indexFrom; i < indexTo; i += this.skip) {
            track.push([this.lat[i], this.lon[i]]);
        }
        return track;
    }

    updatePoint(index) {
        this.marker.setLatLng([this.lat[index], this.lon[index]]);
    }

    updateSegment(indexFrom, indexTo) {
        this.segmentLine.setLatLngs(this.extractTrack(indexFrom, indexTo));
    }
}
