
# RegattaView -- Frontend

A webpack-bundled JavaScript web app for replaying sailing races with interactive 360-degree video, synchronized instrument data overlays, sparkline charts, and a live boat-track map.

## Pages

| Page | Entry Point | Description |
|------|-------------|-------------|
| **sailview.html** | `sailview.js` | Main viewer -- Insta360 dual-fisheye 360 video with instrument overlay |
| **review.html** | `review.js` | Race Start Review -- inspect starts, set status/notes (persisted to `events_edits.json` overlay), read and post user comments |
| **tracks.html** | `tracks.js` | AIS day index and per-day track viewer with nearby-boat and J/105 filters |
| **calibrate.html** | `calibrate.js` | Local-only dual-fisheye calibration tuner (seam, lens centers, distortion, presets) |
| **index.html** | `index.js` | Race catalog -- expandable cards listing available races with links |
| **graphs.html** | `graphs.js` | Bokeh-based interactive time-series plots of sensor data |
| **login.html** | `login.js` | Authentication gate (username/password via AWS Lambda) |

## Architecture

### 360 Video Rendering

The video viewers use **WebGL** (via `twgl.js`) to render 360 video in real time:

1. Each video frame is uploaded as a WebGL texture
2. A GLSL fragment shader reprojects the image from its native format into a perspective view
3. Mouse drag rotates the viewpoint (quaternion-based); scroll wheel adjusts FOV
4. Camera stabilization applies the inverse of the gyroscope-derived pose quaternion

The fisheye GLSL shader is defined in `fisheye_renderer.js` and blends the front and back fisheye hemispheres from the Insta360 camera.

### Data Overlay

When a video has an associated log file, the viewer shows:
- **Instrument readouts**: heading, true/apparent wind direction and speed, boat speed, TWA, AWA, depth
- **Sparkline charts**: trailing-window history for each instrument channel
- **Boat-track SVG map**: full race track with a J/105 outline that moves and rotates in real time
- **Starting line**: placed at the current boat position, perpendicular to wind direction

### Authentication

All S3 data is private. Pages check for credentials in browser cookies on load; missing/invalid credentials redirect to `login.html`. Login POSTs to an AWS Lambda that validates and returns signed URLs. Credentials are stored in cookies (30-day expiry). `sailview.js` supports a `runLocal = true` flag for local dev without auth.

### AIS Tracks

`tracks.html` loads generated AIS day payloads from `Analyze/AISDays/` using the
same dev/prod data path as `review.html`:

- `Analyze/AISDays/index.json` — landing-page list of sailing days with usable AIS.
- `Analyze/AISDays/<YYYY-MM-DD>.json` — one local sailing-day payload with our
  ego track plus nearby AIS boats.

The page uses Leaflet with OpenStreetMap by default and an optional local NOAA
Seattle chart/depth overlay served at `/SeattleTiles/...` in development. It
supports search, J/105-only filtering, named-only filtering, selected-only
filtering, sorting, per-boat show/track toggles, and a scrubber with two hours
of trailing track history.

## Keyboard Shortcuts (Video Viewers)

| Key | Action |
|-----|--------|
| Space | Play / Pause |
| Left / Right Arrow | Step one frame backward / forward |
| Up / Down Arrow | Jump 5 seconds forward / backward |
| R | Reset viewpoint and zoom |
| F | Flip viewpoint 180 degrees |
| S | Toggle camera stabilization |
| P | Toggle 30 / 60 FPS rendering |
| M | Mute / unmute audio |
| Z | Zoom the track map |
| . (period) | Center track map on current boat position |
| L | Place starting line at current position, perpendicular to wind |
| C | Copy current video + timestamp as a shareable URL |
| N | Mark an event (pauses video, opens event marker dialog) |
| K | Post a user comment pinned to the current video moment (sailview only) |
| H | Toggle help overlay |

## Project Structure

```
Src/
├── src/
│   ├── js/
│   │   ├── sailview.js        <- Insta360 360 video viewer (dual-fisheye shader)
│   │   ├── tracks.js          <- AIS day index + per-day Leaflet track viewer
│   │   ├── index.js           <- race catalog page
│   │   ├── graphs.js          <- Bokeh time-series plotting page
│   │   ├── login.js           <- authentication page
│   │   ├── shared.js          <- AWS Lambda client, auth, S3 URL signing
│   │   ├── shared_ux.js       <- Mithril components for race index cards
│   │   ├── utils.js           <- cookies, URL params, time formatting
│   │   ├── htmlElements.js    <- DOM builders: sliders, dropdowns, data fields, maps
│   │   ├── sparkline.js       <- SVG sparklines and BoatView track map
│   │   ├── chart.js           <- chart helpers for the graphs page
│   │   └── instrument_log.js  <- binary log file loader for graphs page
│   ├── html/                  <- HTML templates (one per page)
│   ├── css/
│   │   └── video.css          <- styles for the video viewer UI
│   └── assets/
│       ├── fonts/             <- custom icon font for player controls
│       └── images/            <- test images, placeholder fisheye frames
├── package.json
├── webpack.config.js          <- Webpack 5 build config
└── .eslintrc.js               <- ESLint config (currently disabled in webpack)
```

## Dependencies

### Runtime

| Package | Purpose |
|---------|---------|
| `twgl.js` ^4.19 | WebGL helper (texture upload, shader compilation, draw calls) |
| `gl-matrix` ^3.3 | Quaternion and matrix math for camera pose and stabilization |
| `@bokeh/bokehjs` ^2.2 | Interactive plotting on the graphs page |
| `mithril` ^2.0 | Lightweight virtual DOM for the race catalog UI |
| `leaflet` ^1.7 | Interactive maps in `chart.js` for track visualization |

### Build

Webpack 5, webpack-dev-server, css-loader, MiniCssExtractPlugin, HtmlWebpackPlugin, CopyPlugin.

## Getting Started

```bash
npm install
npm run start     # dev server at http://localhost:9000, opens sailview.html
npm run build     # production build to dist/
```

If you hit OpenSSL errors on older Node versions:

```bash
export NODE_OPTIONS=--openssl-legacy-provider
npm run build
```

## Deployment

```bash
cd Src && npm run build     # build to dist/
cd .. && ./aws_sync.sh      # sync to s3://regattaanalysis/www/
```
