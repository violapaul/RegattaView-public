export const EQUIRECT_VERTEX_SHADER = `
attribute vec4 a_position;
void main() {
    gl_Position = a_position;
}
`;

// Standard equirectangular viewer: rectilinear (pinhole) projection of the
// screen, sphere-lookup of the result. Forward is -Z; u_fov is the horizontal
// field of view in degrees.
export const EQUIRECT_FRAGMENT_SHADER = `
#define M_PI 3.1415926535897932384626433832795

precision mediump float;

uniform sampler2D u_texture;
uniform vec2 u_resolution;
uniform mat4 u_rotation;
uniform float u_fov;

void main() {
    vec2 ndc = (gl_FragCoord.xy / u_resolution) * 2.0 - 1.0;
    float aspect = u_resolution.x / u_resolution.y;
    float tanHalf = tan(radians(u_fov) * 0.5);

    vec3 cameraRay = normalize(vec3(
        ndc.x * aspect * tanHalf,
        ndc.y * tanHalf,
        -1.0
    ));

    vec3 worldRay = normalize((u_rotation * vec4(cameraRay, 0.0)).xyz);

    float lon = atan(worldRay.x, -worldRay.z);
    float lat = asin(clamp(worldRay.y, -1.0, 1.0));
    vec2 uv = vec2(0.5 + lon / (2.0 * M_PI), 0.5 - lat / M_PI);

    gl_FragColor = texture2D(u_texture, uv);
}
`;
