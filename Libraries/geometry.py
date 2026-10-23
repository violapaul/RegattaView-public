
import numpy as np
import math
from matplotlib import pyplot as plt
from numba import jit, jit_module

from .log_helper import LOG

def norm(vec):
    return np.sum(vec**2)**0.5 + 1e-6  # avoid divide by zero

def norm_vec(vec):
    return vec / norm(vec)

def log_debug(*args):
    message = str.format(*args)
    LOG.logger.debug(message)

def small_angle(angle):
    """Deals with the 2pi ambiguity of angles."""
    a = math.fmod(angle, 2 * math.pi)
    if a > math.pi:
        return a - (2 * math.pi)
    else:
        return a

# Basic code for manipulating matrices, rotations, quarternions, etc.
def cross_mat(v):
    return np.array([[0, -v[2], v[1]],
                     [v[2], 0, -v[0]],
                     [-v[1], v[0], 0]])

def q_identity():
    return np.array([0.0, 0.0, 0.0, 1.0])

def q_vw(q):
    """Break a quarternion into 'axis' part and angle part."""
    return q[:3], q[3]

def xyzw_q(x, y, z, w):
    return np.array([x, y, z, w])

def vw_q(v, w):
    res = np.zeros(4)
    res[:3] = v
    res[3] = w
    return res

def axis_angle_q(axis, angle):
    "Equation 2.39 in Hartley and Zisserman."
    norm_axis = np.asarray(axis) / norm(axis)
    return vw_q(math.sin(angle/2) * norm_axis, math.cos(angle/2))

def axis_angle_twist(axis, angle):
    return (angle/norm(axis)) * np.asarray(axis)

def twist_q(twist):
    angle = norm(twist)
    return axis_angle_q(twist / angle, angle)

def q_twist(q):
    v, w = q_vw(q)
    n = norm(v)
    angle = 2 * math.atan2(n, w)
    return (angle/n) * v

def q_angle(q):
    v, w = q_vw(q)
    n = norm(v)
    return 2 * math.atan2(n, w)

def q_inverse(q):
    v, w = q_vw(q)
    return vw_q(v, -w)

def q_compose(q0, q1):
    "Equation 2.42."
    v0, w0 = q_vw(q0)
    v1, w1 = q_vw(q1)
    return vw_q(np.cross(v0, v1) + w0*v1 + w1*v0, w0*w1 - np.dot(v0, v1))

def q_mat(q):
    "Equation 2.40."
    cx = cross_mat(q[:3])
    return np.identity(3) + 2 * q[3] * cx + 2 * np.dot(cx, cx)

def twist_mat(twist):
    "Rodriguez or Equation 2.34."
    vtwist = np.asarray(twist)
    angle = norm(vtwist)
    if math.fabs(angle) > 0:
        normal_vector = twist / angle
        cx = cross_mat(normal_vector)
        return np.identity(3) + math.sin(angle) * cx + (1 - math.cos(angle)) * np.dot(cx, cx)
    else:
        return np.identity(3)

def twist_inverse(twist):
    return -1 * twist

def rt_mat(rotation, translation):
    res = np.identity(4)
    res[:3, :3] = rotation
    res[:3,3] = translation
    return res

def vec3(x, y, z):
    return np.array([x, y, z], dtype=np.float64)

degtorad = 1.0 # math.pi / 180.0

def estimate_pose(initial_q, accel, gyro, accel_gain = 0.005, gyro_gain = 0.5):
    print("one")
    # using opengl conventions
    dn_hat = vec3(1, 0, 0)
    bw_hat = vec3(0, 1, 0)
    rt_hat = vec3(0, 0, 1)

    res = np.zeros((len(accel), 4))

    # Let's assume that the initial acceleration points down.
    down_measured = norm_vec(accel[0, :])
    gravity_error = 1.0 * np.cross(down_measured, dn_hat)
    current_q = q_compose(twist_q(gravity_error), initial_q)

    # current_q = initial_q
    for i in range(0, len(accel)):
        # print(accel_gain, gyro_gain)
        accel_vec = accel[i, :]
        gyro_vec = gyro[i, :]

        cam_2_world = q_mat(current_q)
        rt_est = np.dot(cam_2_world, rt_hat)  # should point right
        dn_est = np.dot(cam_2_world, dn_hat)  # should point down
        bw_est = np.dot(cam_2_world, bw_hat)  # should point backward

        delta_t = 0.001

        # each gyro measures a twist around its axis.  If they are small enough, they can combined
        # into a single twist vector.  Note, the gyros are in the camera frame, so they need to be
        # pre-multiplied.

        gyro_twist = delta_t * gyro_gain * gyro_vec.astype(np.float64)
        # note this is pre-multiplied by the current rotation.
        current_q = q_compose(current_q, twist_q(gyro_twist))

        # update the pose so that down is correct (this corrects for drift)
        # down_measured = norm_vec(-1.0 * accel_vec)
        down_measured = norm_vec(accel_vec)
        gravity_error = 1.0 * accel_gain * np.cross(down_measured, dn_est)
        # This is post-multiplied by the current rotation, since it is in the world frame.
        current_q = q_compose(twist_q(gravity_error), current_q)

        # The goal of the code above is to stabilize viewpoint, not keep it fixed in space.  For the
        # most part the viewpoint should be forward algined with the direction of the boat.  So as the
        # boat tacks, the viewpoint should rotate with the boat. Gyros deal with high frequency
        # rotations.  E.G. Pounding through the waves.  But they are famous for having drift.  The
        # acceleroemters can measure "down", so we gradually correct the camera down to be actually
        # aligned with gravity down (helpful when the boat is heeled over).  

        # The final alignment has nothing to do with sensors.  It uses camera "forward" (actually
        # backward) which is a fixed vector and it rotates the viewpoint so that viewpoint forward
        # is aligned with the camera forward.  

        # This is the twist that would rotate the camera backward.
        bw_error = -0.5 * accel_gain * np.cross(bw_hat, bw_est)
        # Take the component that is aligned.
        bw_angle = np.dot(bw_error, dn_est)
        current_q = q_compose(axis_angle_q(dn_est, bw_angle), current_q)
    
        res[i, :] = current_q
    return res

jit_module(nopython=True, error_model="numpy")
