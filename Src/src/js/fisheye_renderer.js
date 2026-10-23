import * as twgl from 'twgl.js';
import {mat4} from 'gl-matrix';
import {FISHEYE_FRAGMENT_SHADER, FISHEYE_VERTEX_SHADER} from './fisheye_shader';
import {EQUIRECT_FRAGMENT_SHADER, EQUIRECT_VERTEX_SHADER} from './equirect_shader';

const DEFAULT_CALIBRATION = {
    front: {
        center: [0.25, 0.5],
        scale: [0.5, 1.0],
        euler: [0.0, 0.0, 0.0],
        distortion: [0.0, 0.0],
    },
    back: {
        center: [0.75, 0.5],
        scale: [-0.5, 1.0],
        euler: [0.0, 0.0, 0.0],
        distortion: [0.0, 0.0],
    },
    projection: {
        aperture: 0.296,
        viewCorrection: -0.2,
    },
    seam: {
        centerYaw: 0.0,
        width: 0.1,
        feather: 1.0,
    },
};

// Per-projection FOV defaults applied at switch time. The fisheye path keeps
// the historical 180; equirect benefits from a tighter starting view because
// the unwrapped texture stretches near the poles.
const DEFAULT_FOV_BY_PROJECTION = {
    fisheye: 180,
    equirect: 100,
};

export function mergeCalibration(defaults, override) {
    if (!override || typeof override !== 'object') return structuredClone(defaults);
    const out = structuredClone(defaults);
    const mergeObject = (target, src) => {
        Object.entries(src).forEach(([k, v]) => {
            if (Array.isArray(v)) {
                target[k] = [...v];
            } else if (v && typeof v === 'object') {
                target[k] = target[k] || {};
                mergeObject(target[k], v);
            } else if (v !== undefined && v !== null) {
                target[k] = v;
            }
        });
    };
    mergeObject(out, override);
    return out;
}

function toUniforms(calibration) {
    return {
        u_frontCenter: calibration.front.center,
        u_backCenter: calibration.back.center,
        u_frontScale: calibration.front.scale,
        u_backScale: calibration.back.scale,
        u_frontEuler: calibration.front.euler,
        u_backEuler: calibration.back.euler,
        u_frontDistortion: calibration.front.distortion,
        u_backDistortion: calibration.back.distortion,
        u_aperture: calibration.projection.aperture,
        u_viewCorrection: calibration.projection.viewCorrection,
        u_seamCenterYaw: calibration.seam.centerYaw,
        u_seamWidth: calibration.seam.width,
        u_seamFeather: calibration.seam.feather,
    };
}

export function createDefaultCalibration() {
    return structuredClone(DEFAULT_CALIBRATION);
}

export function createFisheyeRenderer(gl, options = {}) {
    twgl.setDefaults({attribPrefix: 'a_'});
    const plane = twgl.primitives.createXYQuadBufferInfo(gl);
    const programInfoFisheye = twgl.createProgramInfo(gl, [FISHEYE_VERTEX_SHADER, FISHEYE_FRAGMENT_SHADER]);
    const programInfoEquirect = twgl.createProgramInfo(gl, [EQUIRECT_VERTEX_SHADER, EQUIRECT_FRAGMENT_SHADER]);
    const calibration = mergeCalibration(DEFAULT_CALIBRATION, options.calibration);
    let projection = options.projection === 'equirect' ? 'equirect' : 'fisheye';

    const uniforms = {
        u_texture: options.texture || twgl.createTexture(gl, {
            src: options.textureSrc || [20, 30, 40, 255],
            // Frames replace level 0 at their native size (often 3840x1920).
            // A 1x1 placeholder otherwise enables mipmaps/repeat wrapping,
            // making the later non-power-of-two video texture incomplete.
            auto: false,
            min: gl.LINEAR,
            mag: gl.LINEAR,
            wrap: gl.CLAMP_TO_EDGE,
        }),
        u_rotation: mat4.create(),
        u_fov: options.fov || DEFAULT_FOV_BY_PROJECTION[projection],
        u_resolution: [gl.canvas.width, gl.canvas.height],
        u_showSeamDebug: 0.0,
        ...toUniforms(calibration),
    };

    function setCalibration(nextCalibration) {
        const merged = mergeCalibration(DEFAULT_CALIBRATION, nextCalibration);
        Object.assign(calibration, merged);
        Object.assign(uniforms, toUniforms(calibration));
    }

    function setProjection(mode) {
        const next = mode === 'equirect' ? 'equirect' : 'fisheye';
        if (next === projection) {
            console.log(`[renderer] setProjection(${next}) — already in this mode`);
            return;
        }
        console.log(`[renderer] setProjection ${projection} -> ${next} (fov=${DEFAULT_FOV_BY_PROJECTION[next]})`);
        projection = next;
        uniforms.u_fov = DEFAULT_FOV_BY_PROJECTION[projection];
    }

    return {
        programInfo: programInfoFisheye,
        plane,
        uniforms,
        getCalibration: () => structuredClone(calibration),
        setCalibration,
        setProjection,
        getProjection: () => projection,
        getDefaultFov: () => DEFAULT_FOV_BY_PROJECTION[projection],
        setFov: (fov) => {
            uniforms.u_fov = fov;
        },
        getFov: () => uniforms.u_fov,
        setShowSeamDebug: (enabled) => {
            uniforms.u_showSeamDebug = enabled ? 1.0 : 0.0;
        },
        draw: ({source, rotation, width, height}) => {
            if (width && height) {
                uniforms.u_resolution = [width, height];
            } else {
                uniforms.u_resolution = [gl.canvas.width, gl.canvas.height];
            }

            if (rotation) {
                mat4.copy(uniforms.u_rotation, rotation);
            }

            if (source) {
                gl.bindTexture(gl.TEXTURE_2D, uniforms.u_texture);
                gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, source);
            }

            const programInfo = projection === 'equirect' ? programInfoEquirect : programInfoFisheye;
            gl.useProgram(programInfo.program);
            twgl.setBuffersAndAttributes(gl, programInfo, plane);
            twgl.setUniforms(programInfo, uniforms);
            twgl.drawBufferInfo(gl, plane);
        },
    };
}
