import '../css/calibrate.css';
import * as twgl from 'twgl.js';
import {mat4, quat} from 'gl-matrix';
import {createDefaultCalibration, createFisheyeRenderer, mergeCalibration} from './fisheye_renderer';

const canvas = document.querySelector('#preview');
const gl = canvas.getContext('webgl');
const sourceStatus = document.querySelector('#sourceStatus');
const fovReadout = document.querySelector('#fovReadout');
const controls = document.querySelector('#controls');
const sourceFileInput = document.querySelector('#sourceFileInput');
const presetFileInput = document.querySelector('#presetFileInput');
const loadSourceBtn = document.querySelector('#loadSourceBtn');
const togglePlayBtn = document.querySelector('#togglePlayBtn');
const resetViewBtn = document.querySelector('#resetViewBtn');
const flipViewBtn = document.querySelector('#flipViewBtn');
const importPresetBtn = document.querySelector('#importPresetBtn');
const exportPresetBtn = document.querySelector('#exportPresetBtn');
const resetPresetBtn = document.querySelector('#resetPresetBtn');
const seamDebugBtn = document.querySelector('#seamDebugBtn');

const renderer = createFisheyeRenderer(gl, {
    fov: 180,
    calibration: createDefaultCalibration(),
});

const uiState = {
    calibration: renderer.getCalibration(),
    source: null,
    seamDebug: false,
};

const controlBindings = {};
const DEFAULT_FOV = 180;
const MIN_FOV = 120;
const MAX_FOV = 220;
const FOV_STEP = 1.1;
// Reset looks at a lens seam (90deg off the front-lens axis) rather than straight
// down the front lens, so the overlap region is front-and-center for stitch checks.
// Press F to flip 180deg to the other seam.
const SEAM_YAW = Math.PI / 2;

const viewState = {
    mouseDown: false,
    downX: 0,
    downY: 0,
    yaw: SEAM_YAW,
    pitch: 0,
    qView: quat.create(),
};

const PARAMS = [
    {group: 'Projection'},
    {label: 'FOV', path: ['fov'], min: 120, max: 220, step: 0.5},
    {label: 'Aperture', path: ['projection', 'aperture'], min: 0.15, max: 0.45, step: 0.0005},
    {label: 'View correction', path: ['projection', 'viewCorrection'], min: -0.5, max: 0.5, step: 0.0005},
    {group: 'Seam'},
    {label: 'Center yaw (deg)', path: ['seam', 'centerYaw'], min: -Math.PI, max: Math.PI, step: 0.001, displayScale: 180 / Math.PI},
    {label: 'Blend width', path: ['seam', 'width'], min: 0.01, max: 0.4, step: 0.001},
    {label: 'Feather', path: ['seam', 'feather'], min: 0.2, max: 3.0, step: 0.01},
    {group: 'Front lens'},
    {label: 'Center X', path: ['front', 'center', 0], min: 0.1, max: 0.4, step: 0.0005},
    {label: 'Center Y', path: ['front', 'center', 1], min: 0.3, max: 0.7, step: 0.0005},
    {label: 'Scale X', path: ['front', 'scale', 0], min: 0.3, max: 0.7, step: 0.0005},
    {label: 'Scale Y', path: ['front', 'scale', 1], min: 0.7, max: 1.3, step: 0.0005},
    {label: 'Yaw (deg)', path: ['front', 'euler', 1], min: -0.2, max: 0.2, step: 0.0005, displayScale: 180 / Math.PI},
    {label: 'Pitch (deg)', path: ['front', 'euler', 0], min: -0.2, max: 0.2, step: 0.0005, displayScale: 180 / Math.PI},
    {label: 'Roll (deg)', path: ['front', 'euler', 2], min: -0.2, max: 0.2, step: 0.0005, displayScale: 180 / Math.PI},
    {label: 'k1', path: ['front', 'distortion', 0], min: -2, max: 2, step: 0.0005},
    {label: 'k2', path: ['front', 'distortion', 1], min: -2, max: 2, step: 0.0005},
    {group: 'Back lens'},
    {label: 'Center X', path: ['back', 'center', 0], min: 0.6, max: 0.9, step: 0.0005},
    {label: 'Center Y', path: ['back', 'center', 1], min: 0.3, max: 0.7, step: 0.0005},
    {label: 'Scale X', path: ['back', 'scale', 0], min: -0.7, max: -0.3, step: 0.0005},
    {label: 'Scale Y', path: ['back', 'scale', 1], min: 0.7, max: 1.3, step: 0.0005},
    {label: 'Yaw (deg)', path: ['back', 'euler', 1], min: -0.2, max: 0.2, step: 0.0005, displayScale: 180 / Math.PI},
    {label: 'Pitch (deg)', path: ['back', 'euler', 0], min: -0.2, max: 0.2, step: 0.0005, displayScale: 180 / Math.PI},
    {label: 'Roll (deg)', path: ['back', 'euler', 2], min: -0.2, max: 0.2, step: 0.0005, displayScale: 180 / Math.PI},
    {label: 'k1', path: ['back', 'distortion', 0], min: -2, max: 2, step: 0.0005},
    {label: 'k2', path: ['back', 'distortion', 1], min: -2, max: 2, step: 0.0005},
];

function getPath(obj, path) {
    return path.reduce((acc, key) => acc[key], obj);
}

function setPath(obj, path, value) {
    const keys = [...path];
    const finalKey = keys.pop();
    const target = keys.reduce((acc, key) => acc[key], obj);
    target[finalKey] = value;
}

function applyCalibration() {
    renderer.setCalibration(uiState.calibration);
    renderer.setFov(uiState.calibration.fov || DEFAULT_FOV);
    updateFovReadout();
}

function updateFovReadout() {
    fovReadout.textContent = `${renderer.getFov().toFixed(1)} deg`;
}

function setFov(nextFov) {
    const clamped = Math.max(MIN_FOV, Math.min(MAX_FOV, nextFov));
    uiState.calibration.fov = clamped;
    renderer.setFov(clamped);
    updateFovReadout();
    const fovPath = 'fov';
    if (controlBindings[fovPath]) {
        controlBindings[fovPath].writeValue(clamped);
    }
}

function resetView() {
    viewState.yaw = SEAM_YAW;
    viewState.pitch = 0;
    quat.identity(viewState.qView);
    setFov(DEFAULT_FOV);
}

function flipView() {
    viewState.yaw += Math.PI;
}

function buildControls() {
    controls.innerHTML = '';
    Object.keys(controlBindings).forEach((k) => delete controlBindings[k]);
    PARAMS.forEach((cfg) => {
        if (cfg.group) {
            const heading = document.createElement('h2');
            heading.textContent = cfg.group;
            controls.appendChild(heading);
            return;
        }

        const row = document.createElement('div');
        row.className = 'row';

        const label = document.createElement('label');
        label.textContent = cfg.label;

        const slider = document.createElement('input');
        slider.type = 'range';
        slider.min = cfg.min;
        slider.max = cfg.max;
        slider.step = cfg.step;
        slider.setAttribute('data-path', cfg.path.join('.'));

        const number = document.createElement('input');
        number.type = 'number';
        number.min = cfg.min;
        number.max = cfg.max;
        number.step = cfg.step;
        number.setAttribute('data-path', cfg.path.join('.'));

        const writeValue = (val) => {
            slider.value = String(val);
            number.value = String(val);
            const scaled = cfg.displayScale ? val * cfg.displayScale : val;
            label.textContent = `${cfg.label}: ${scaled.toFixed(3)}`;
        };

        const onChange = (val) => {
            const parsed = Number(val);
            if (Number.isNaN(parsed)) return;
            setPath(uiState.calibration, cfg.path, parsed);
            writeValue(parsed);
            applyCalibration();
        };

        const initial = getPath(uiState.calibration, cfg.path);
        writeValue(initial);
        controlBindings[cfg.path.join('.')] = {writeValue};

        slider.addEventListener('input', () => onChange(slider.value));
        number.addEventListener('change', () => onChange(number.value));

        row.appendChild(label);
        row.appendChild(number);
        controls.appendChild(row);
        controls.appendChild(slider);
    });
}

function updateSourceStatus(message) {
    sourceStatus.textContent = message;
}

function loadSource(file) {
    if (!file) return;
    const url = URL.createObjectURL(file);

    if (file.type.startsWith('video/')) {
        const video = document.createElement('video');
        video.src = url;
        video.muted = true;
        video.loop = true;
        video.autoplay = true;
        video.playsInline = true;
        video.addEventListener('canplay', () => {
            video.play();
            uiState.source = video;
            updateSourceStatus(`Video: ${file.name}`);
        });
        video.addEventListener('error', () => {
            updateSourceStatus(`Failed to load video: ${file.name}`);
        });
        return;
    }

    if (file.type.startsWith('image/')) {
        const image = new Image();
        image.onload = () => {
            uiState.source = image;
            updateSourceStatus(`Image: ${file.name}`);
        };
        image.onerror = () => {
            updateSourceStatus(`Failed to load image: ${file.name}`);
        };
        image.src = url;
    }
}

function exportPreset() {
    const payload = {
        schemaVersion: 1,
        calibration: uiState.calibration,
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)], {type: 'application/json'});
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = 'fisheye-calibration.json';
    anchor.click();
    URL.revokeObjectURL(url);
}

function importPreset(file) {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
        try {
            const loaded = JSON.parse(reader.result);
            if (!loaded || !loaded.calibration) {
                    throw new Error('Invalid calibration preset');
                }
                if (loaded.schemaVersion !== 1) {
                    throw new Error(`Unsupported schemaVersion: ${loaded.schemaVersion} (expected 1)`);
                }
                const merged = mergeCalibration(createDefaultCalibration(), loaded.calibration);
            uiState.calibration = merged;
            applyCalibration();
            buildControls();
            updateSourceStatus(`Preset loaded: ${file.name}`);
        } catch (err) {
            updateSourceStatus(`Preset error: ${err.message}`);
        }
    };
    reader.readAsText(file);
}

function bindMouseControls() {
    canvas.addEventListener('mousedown', (evt) => {
        viewState.mouseDown = true;
        viewState.downX = evt.clientX;
        viewState.downY = evt.clientY;
    });

    document.addEventListener('mouseup', () => {
        viewState.mouseDown = false;
    });

    document.addEventListener('mousemove', (evt) => {
        if (!viewState.mouseDown) return;
        const dx = (evt.clientX - viewState.downX) / Math.max(1, canvas.width);
        const dy = (evt.clientY - viewState.downY) / Math.max(1, canvas.height);
        viewState.downX = evt.clientX;
        viewState.downY = evt.clientY;
        viewState.yaw -= dx * 2.5;
        viewState.pitch -= dy * 2.5;
    });

    canvas.addEventListener('wheel', (evt) => {
        evt.preventDefault();
        if (evt.deltaY < 0) {
            setFov(renderer.getFov() / FOV_STEP);
        } else if (evt.deltaY > 0) {
            setFov(renderer.getFov() * FOV_STEP);
        }
    }, {passive: false});
}

function bindKeyboard() {
    document.addEventListener('keydown', (evt) => {
        if (evt.key === 'r' || evt.key === 'R') {
            resetView();
        } else if (evt.key === 'f' || evt.key === 'F') {
            flipView();
        }
    });
}

loadSourceBtn.addEventListener('click', () => sourceFileInput.click());
sourceFileInput.addEventListener('change', () => loadSource(sourceFileInput.files[0]));

togglePlayBtn.addEventListener('click', () => {
    if (uiState.source && uiState.source.tagName === 'VIDEO') {
        if (uiState.source.paused) uiState.source.play();
        else uiState.source.pause();
    }
});

importPresetBtn.addEventListener('click', () => presetFileInput.click());
presetFileInput.addEventListener('change', () => importPreset(presetFileInput.files[0]));
exportPresetBtn.addEventListener('click', () => exportPreset());

resetPresetBtn.addEventListener('click', () => {
    uiState.calibration = {...createDefaultCalibration(), fov: DEFAULT_FOV};
    applyCalibration();
    buildControls();
});

resetViewBtn.addEventListener('click', resetView);
flipViewBtn.addEventListener('click', flipView);

seamDebugBtn.addEventListener('click', () => {
    uiState.seamDebug = !uiState.seamDebug;
    renderer.setShowSeamDebug(uiState.seamDebug);
});

function render() {
    twgl.resizeCanvasToDisplaySize(gl.canvas);
    gl.viewport(0, 0, gl.canvas.width, gl.canvas.height);
    gl.clearColor(0.0, 0.0, 0.0, 1.0);
    gl.clear(gl.COLOR_BUFFER_BIT);

    const qx = quat.create();
    const qy = quat.create();
    quat.rotateX(qx, quat.create(), viewState.pitch);
    quat.rotateY(qy, quat.create(), viewState.yaw);
    quat.multiply(viewState.qView, qy, qx);
    const rotation = mat4.fromQuat(mat4.create(), viewState.qView);

    renderer.draw({
        source: uiState.source,
        rotation,
        width: gl.canvas.width,
        height: gl.canvas.height,
    });
    requestAnimationFrame(render);
}

uiState.calibration.fov = DEFAULT_FOV;
uiState.seamDebug = true; // highlight the seam on load — this tool exists to check the overlap
renderer.setShowSeamDebug(true);
buildControls();
bindMouseControls();
bindKeyboard();
updateFovReadout();
render();
