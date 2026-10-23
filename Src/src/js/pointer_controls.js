/**
 * pointer_controls.js
 *
 * Unified pointer-event pan/zoom for the 360 canvas. Handles mouse, touch,
 * and pen through PointerEvents so the same code drives desktop drag and
 * mobile touch gestures.
 *
 * Gestures:
 *   - Single pointer drag  → pan (yaw + pitch). No roll — ever.
 *   - Two-finger pinch     → zoom (FOV).
 *   - Double-tap (touch)   → reset viewpoint + FOV.
 *   - Wheel                → zoom (desktop only; no wheel on touch).
 *
 * Orientation model: the viewpoint is stored as two scalars (yaw, pitch) and
 * the quaternion is rebuilt every update as:
 *
 *     q = rotateY(yaw) · rotateX(pitch)
 *
 * i.e. yaw in the world (post-stabilization) frame, pitch in the camera's
 * local frame. That keeps the horizon horizontal regardless of history — no
 * drift, no accumulated roll, no user-inducible horizon tilt. Pitch clamps
 * to just shy of ±π/2 to avoid gimbal flip at the poles.
 *
 * Returns an object compatible with the legacy state.mouse contract:
 *   { viewpoint: quat, reset(), flip(), mouseDown, X, Y, deltaX, deltaY }
 * Callers read `viewpoint` every frame in their render loop.
 *
 * Renderer contract: requires { setFov(n), getFov() }.
 */
import {quat} from 'gl-matrix';

const DOUBLE_TAP_MS = 300;
const DOUBLE_TAP_PX = 20;
const FOV_MIN = 20;
const FOV_MAX = 170;
// Keep pitch safely inside ±90° so the up-vector doesn't flip (gimbal lock).
const PITCH_LIMIT = Math.PI / 2 - 0.01;

/**
 * Attach pointer + wheel controls to a canvas and return the viewpoint state.
 * @param {HTMLCanvasElement} canvas
 * @param {{setFov: (n:number)=>void, getFov: ()=>number}} renderer
 * @param {{defaultFov?: number}} [options]
 * @return {Object} state object with `viewpoint`, `reset`, `flip`, etc.
 */
export function attachPointerControls(canvas, renderer, options = {}) {
    const defaultFov = options.defaultFov ?? 180;

    const res = {
        X: 0, Y: 0,
        mouseDown: false,
        deltaX: 0, deltaY: 0,
        downX: 0, downY: 0,
        yaw: 0,
        pitch: 0,
        viewpoint: quat.create(),
    };

    function rebuildViewpoint() {
        quat.identity(res.viewpoint);
        quat.rotateY(res.viewpoint, res.viewpoint, res.yaw);
        quat.rotateX(res.viewpoint, res.viewpoint, res.pitch);
    }

    res.reset = () => {
        res.yaw = 0;
        res.pitch = 0;
        rebuildViewpoint();
    };
    res.flip = () => {
        // Swing 180° around the world up-axis.
        res.yaw += Math.PI;
        rebuildViewpoint();
    };
    res.reset();

    // pointerId -> {clientX, clientY, downClientX, downClientY}
    const pointers = new Map();
    let yawStart = 0;
    let pitchStart = 0;
    let pinchStartDist = 0;
    let pinchStartFov = 0;
    let lastTapTime = 0;
    let lastTapX = 0;
    let lastTapY = 0;

    // Without touch-action:none, Safari steals single-finger vertical drags
    // for page scroll before pointermove fires.
    canvas.style.touchAction = 'none';

    function updateNormalized(clientX, clientY) {
        const rect = canvas.getBoundingClientRect();
        res.X = ((clientX - rect.left) / canvas.width) - 0.5;
        // bottom is 0 in WebGL
        res.Y = ((rect.height - (clientY - rect.top) - 1) / canvas.height) - 0.5;
    }

    function dist(a, b) {
        return Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
    }

    function clamp(v, lo, hi) {
        return Math.max(lo, Math.min(hi, v));
    }

    function seatPan(clientX, clientY) {
        updateNormalized(clientX, clientY);
        res.downX = res.X;
        res.downY = res.Y;
        res.deltaX = 0;
        res.deltaY = 0;
        yawStart = res.yaw;
        pitchStart = res.pitch;
        res.mouseDown = true;
    }

    canvas.addEventListener('pointerdown', (e) => {
        // Match legacy left-button-only behavior so right/middle click still
        // yield their default handling (context menu, etc.) on desktop.
        if (e.pointerType === 'mouse' && e.button !== 0) return;

        try { canvas.setPointerCapture(e.pointerId); } catch (_) { /* ignore */ }

        pointers.set(e.pointerId, {
            clientX: e.clientX,
            clientY: e.clientY,
            downClientX: e.clientX,
            downClientY: e.clientY,
        });

        if (pointers.size === 1) {
            seatPan(e.clientX, e.clientY);
        } else if (pointers.size === 2) {
            // Second finger down: leave pan, start pinch.
            res.mouseDown = false;
            const [a, b] = [...pointers.values()];
            pinchStartDist = dist(a, b);
            pinchStartFov = renderer.getFov();
        }
        e.preventDefault();
    });

    canvas.addEventListener('pointermove', (e) => {
        const p = pointers.get(e.pointerId);
        if (!p) return;
        p.clientX = e.clientX;
        p.clientY = e.clientY;
        updateNormalized(e.clientX, e.clientY);

        if (pointers.size === 1 && res.mouseDown) {
            res.deltaX = res.X - res.downX;
            res.deltaY = res.Y - res.downY;
            // Yaw around world Y, pitch around the camera's local X — this
            // is always well-defined and never introduces roll, regardless
            // of the order of user actions.
            res.yaw = yawStart + res.deltaX * 2.0 * Math.PI;
            res.pitch = clamp(
                pitchStart + res.deltaY * Math.PI,
                -PITCH_LIMIT,
                PITCH_LIMIT,
            );
            rebuildViewpoint();
            e.preventDefault();
        } else if (pointers.size >= 2) {
            const [a, b] = [...pointers.values()];
            const d = dist(a, b);
            if (d > 0 && pinchStartDist > 0) {
                // Fingers spreading (d > start) → narrower FOV (zoom in),
                // same direction as wheel-up on desktop.
                const ratio = pinchStartDist / d;
                renderer.setFov(clamp(pinchStartFov * ratio, FOV_MIN, FOV_MAX));
            }
            e.preventDefault();
        }
    }, {capture: true});

    function release(e) {
        const p = pointers.get(e.pointerId);
        if (!p) return;
        const movedPx = Math.hypot(
            e.clientX - p.downClientX,
            e.clientY - p.downClientY,
        );
        pointers.delete(e.pointerId);
        try { canvas.releasePointerCapture(e.pointerId); } catch (_) { /* ignore */ }

        if (pointers.size === 0) {
            res.mouseDown = false;

            // Double-tap-to-reset (touch/pen only, negligible movement).
            if (e.pointerType !== 'mouse' && movedPx < DOUBLE_TAP_PX) {
                const now = (typeof performance !== 'undefined' && performance.now)
                    ? performance.now() : Date.now();
                const tapGapPx = Math.hypot(e.clientX - lastTapX, e.clientY - lastTapY);
                if (now - lastTapTime < DOUBLE_TAP_MS && tapGapPx < DOUBLE_TAP_PX) {
                    res.reset();
                    renderer.setFov(defaultFov);
                    lastTapTime = 0;
                } else {
                    lastTapTime = now;
                    lastTapX = e.clientX;
                    lastTapY = e.clientY;
                }
            }
        } else if (pointers.size === 1) {
            // Dropped from pinch back to single-finger pan: re-seat pan
            // baseline at the remaining pointer so the next move doesn't jump.
            const [remaining] = [...pointers.values()];
            seatPan(remaining.clientX, remaining.clientY);
        }
    }

    canvas.addEventListener('pointerup', release);
    canvas.addEventListener('pointercancel', release);

    canvas.addEventListener('wheel', (e) => {
        const delta = e.wheelDelta ? e.wheelDelta / 40.0 : e.detail ? -e.detail : 0;
        if (delta > 0) renderer.setFov(renderer.getFov() * 1.1);
        else if (delta < 0) renderer.setFov(renderer.getFov() / 1.1);
        e.preventDefault();
    }, {passive: false});

    return res;
}
