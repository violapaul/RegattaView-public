export const FISHEYE_VERTEX_SHADER = `
attribute vec4 a_position;
void main() {
    gl_Position = a_position;
}
`;

export const FISHEYE_FRAGMENT_SHADER = `
#define M_PI 3.1415926535897932384626433832795

precision mediump float;

uniform sampler2D u_texture;
uniform vec2 u_resolution;
uniform mat4 u_rotation;
uniform float u_fov;

uniform vec2 u_frontCenter;
uniform vec2 u_backCenter;
uniform vec2 u_frontScale;
uniform vec2 u_backScale;
uniform vec3 u_frontEuler;
uniform vec3 u_backEuler;
uniform vec2 u_frontDistortion;
uniform vec2 u_backDistortion;
uniform float u_aperture;
uniform float u_viewCorrection;
uniform float u_seamCenterYaw;
uniform float u_seamWidth;
uniform float u_seamFeather;
uniform float u_showSeamDebug;

vec3 rotateEuler(vec3 v, vec3 euler) {
    float cx = cos(euler.x);
    float sx = sin(euler.x);
    float cy = cos(euler.y);
    float sy = sin(euler.y);
    float cz = cos(euler.z);
    float sz = sin(euler.z);

    mat3 rx = mat3(
        1.0, 0.0, 0.0,
        0.0, cx, -sx,
        0.0, sx, cx
    );
    mat3 ry = mat3(
        cy, 0.0, sy,
        0.0, 1.0, 0.0,
        -sy, 0.0, cy
    );
    mat3 rz = mat3(
        cz, -sz, 0.0,
        sz, cz, 0.0,
        0.0, 0.0, 1.0
    );

    return rz * ry * rx * v;
}

// OpenCV fisheye distortion is applied to theta (angle from optical axis):
// theta_d = theta * (1 + k1*theta^2 + k2*theta^4)
float correctedTheta(float theta, vec2 distortion) {
    float t2 = theta * theta;
    float t4 = t2 * t2;
    return theta * (1.0 + distortion.x * t2 + distortion.y * t4);
}

void main() {
    float scale = length(u_resolution);
    vec2 nc = (2.0 * gl_FragCoord.xy - u_resolution) / scale;
    nc = nc * vec2(1.0, -1.0);

    float fov = radians(u_fov) / (1.0 + u_viewCorrection);
    float theta = atan(nc.y, nc.x);
    float ncLen = length(nc);
    float phi = (ncLen + u_viewCorrection * pow(ncLen, 3.0)) * fov / 2.0;

    vec4 ray4 = vec4(sin(phi) * cos(theta), sin(phi) * sin(theta), -cos(phi), 1.0);
    ray4 = u_rotation * ray4;
    vec3 worldRay = normalize(ray4.xyz);

    vec3 frontRay = rotateEuler(worldRay, u_frontEuler);
    vec3 backRay = rotateEuler(worldRay, u_backEuler);

    float frontLen = length(frontRay.xy);
    float backLen = length(backRay.xy);
    float frontTheta = atan(frontRay.y, frontRay.x);
    float backTheta = atan(backRay.y, backRay.x);

    float frontAngle = correctedTheta(atan(frontLen, -frontRay.z), u_frontDistortion);
    float frontR = u_aperture * frontAngle;
    vec2 frontUV = u_frontCenter + vec2(
        u_frontScale.x * frontR * cos(frontTheta),
        u_frontScale.y * frontR * sin(frontTheta)
    );

    float backAngle = correctedTheta(atan(backLen, backRay.z), u_backDistortion);
    float backR = u_aperture * backAngle;
    vec2 backUV = u_backCenter + vec2(
        u_backScale.x * backR * cos(backTheta),
        u_backScale.y * backR * sin(backTheta)
    );

    vec4 colorFront = texture2D(u_texture, frontUV);
    vec4 colorBack = texture2D(u_texture, backUV);

    vec3 seamRay = rotateEuler(worldRay, vec3(0.0, u_seamCenterYaw, 0.0));
    float width = max(0.001, u_seamWidth);
    float blend = smoothstep(-width, width, seamRay.z);
    blend = pow(clamp(blend, 0.0, 1.0), max(0.05, u_seamFeather));

    vec4 color = mix(colorFront, colorBack, blend);

    if (u_showSeamDebug > 0.5) {
        float seamBand = 1.0 - smoothstep(width * 0.5, width * 1.5, abs(seamRay.z));
        vec3 seamColor = mix(vec3(0.0, 0.7, 1.0), vec3(1.0, 0.5, 0.0), blend);
        color.rgb = mix(color.rgb, seamColor, 0.35 * seamBand);
        // Crisp center line at the exact seam (z==0) so the overlap is unmistakable.
        float seamLine = 1.0 - smoothstep(0.0, 0.0035, abs(seamRay.z));
        color.rgb = mix(color.rgb, vec3(1.0, 0.0, 1.0), seamLine);
    }

    gl_FragColor = color;
}
`;
