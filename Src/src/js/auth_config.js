// Frontend auth configuration. Edit this file in place after creating the
// Google OAuth 2.0 Web client (Google Cloud Console -> APIs & Services ->
// Credentials -> Create Credentials -> OAuth client ID -> Web application).
//
// The client ID is NOT a secret — Google enforces origin restrictions, so
// it's safe to commit. The corresponding client SECRET (which we do not
// use here, since this is a public-client browser flow) is the secret part.

export const GOOGLE_CLIENT_ID =
    '439534228893-uu9mee0ifuguuhpkf06me6jmacsg0dko.apps.googleusercontent.com';

// Where the auth/sign Lambda is reachable.
export const LAMBDA_URL =
    'https://blsywilg2fba6564nixqxwntyy0iagoj.lambda-url.us-west-2.on.aws/';

// Storage key for the Google ID token. Stored in localStorage so it
// survives page navigation; the token itself has a ~1h expiry enforced
// by Google + the Lambda. Used only during the brief sign-in handshake;
// after the Lambda returns a session token (below), we drop the Google
// token and use the session token from then on.
export const TOKEN_STORAGE_KEY = 'google_id_token';

// Storage key for the long-lived app session token. Issued by the Lambda
// (HMAC-SHA256 JWT) once Google sign-in succeeds; signed with a server-
// side secret. Default TTL is 30 days (see Lambda SESSION_TTL_S env var).
// Sent on every subsequent Lambda call instead of the Google ID token —
// HMAC verify is microseconds vs RSA's ~50 ms, and the session lives well
// past Google's 1 h ID-token expiry so users don't get bounced hourly.
export const SESSION_STORAGE_KEY = 'regatta_session_token';
