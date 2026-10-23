import * as utils from './utils';
import * as shared from './shared';
import {GOOGLE_CLIENT_ID, TOKEN_STORAGE_KEY} from './auth_config';

const statusEl = () => document.getElementById('status');

function showStatus(msg, isError) {
    const el = statusEl();
    if (!el) return;
    el.textContent = msg || '';
    el.className = isError ? 'error' : '';
}

/**
 * Google Identity Services calls this with {credential: <ID token JWT>}.
 * Verify with the Lambda; on success, persist and redirect back.
 */
function onGoogleCredential(response) {
    const idToken = response && response.credential;
    if (!idToken) {
        showStatus('No credential returned from Google.', true);
        return;
    }
    showStatus('Verifying...');
    // Stash before checkAuthorization so signAWSUrl/lambdaRequest can read it.
    localStorage.setItem(TOKEN_STORAGE_KEY, idToken);
    const result = shared.verifyToken(idToken);
    if (result.ok) {
        utils.redirectWithParams(null);
        return;
    }
    localStorage.removeItem(TOKEN_STORAGE_KEY);
    if (result.reason === 'expired') {
        showStatus('Your access has expired — email billey.viola@gmail.com to renew.', true);
    } else if (result.reason === 'not on allowlist') {
        showStatus('This Google account isn’t on the team allowlist yet. Email billey.viola@gmail.com to be added.', true);
    } else {
        showStatus('Sign-in didn’t go through: ' + (result.reason || 'unknown error'), true);
    }
}

function initGoogleSignIn() {
    if (!window.google || !window.google.accounts || !window.google.accounts.id) {
        // Library not loaded yet; retry shortly.
        setTimeout(initGoogleSignIn, 200);
        return;
    }
    if (!GOOGLE_CLIENT_ID || GOOGLE_CLIENT_ID.startsWith('REPLACE_ME')) {
        showStatus('Google client ID not configured. See Src/src/js/auth_config.js.', true);
        return;
    }
    window.google.accounts.id.initialize({
        client_id: GOOGLE_CLIENT_ID,
        callback: onGoogleCredential,
        auto_select: true,
        ux_mode: 'popup',
    });
    window.google.accounts.id.renderButton(
        document.getElementById('signInButton'),
        {theme: 'outline', size: 'large', text: 'signin_with', shape: 'rectangular'}
    );
    // One Tap: silent re-auth if the user has an active Google session.
    window.google.accounts.id.prompt();
}

/** If the user already has a valid session, skip the sign-in screen.
 * Prefers the long-lived app session token; falls back to a stale Google
 * ID token only as a one-time migration path for clients that signed in
 * before session tokens existed.
 */
function authorizedAndLeave() {
    if (shared.checkAuthorization()) {
        utils.redirectWithParams(null);
        return true;
    }
    return false;
}

if (!authorizedAndLeave()) {
    initGoogleSignIn();
}
