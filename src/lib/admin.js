// Analyst console session and API calls. The ADMIN_TOKEN typed on the sign in screen is
// kept in sessionStorage, so it lasts until the tab closes and is never written to disk
// as a cookie. It is only ever sent as a bearer header to /api/admin on the same origin.
import { useEffect, useState } from 'react';
import { ApiError, apiFetch } from './api.js';

const STORAGE_KEY = 'triageops.analystToken';
const CHANGE_EVENT = 'triageops:analyst-session';

// Used only when sessionStorage is blocked.
let memoryToken = '';

export function getToken() {
  try {
    return window.sessionStorage.getItem(STORAGE_KEY) || '';
  } catch {
    return '';
  }
}

function announce() {
  window.dispatchEvent(new Event(CHANGE_EVENT));
}

export function signIn(token) {
  try {
    window.sessionStorage.setItem(STORAGE_KEY, token);
  } catch {
    // Storage can be blocked. The session then lasts until the page reloads.
    memoryToken = token;
  }
  announce();
}

export function signOut() {
  memoryToken = '';
  try {
    window.sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // Nothing stored, nothing to remove.
  }
  announce();
}

function currentToken() {
  return getToken() || memoryToken;
}

// Re-renders the caller when the analyst signs in or out, in this tab or another one.
export function useAnalystSession() {
  const [token, setToken] = useState(currentToken);
  useEffect(() => {
    const update = () => setToken(currentToken());
    window.addEventListener(CHANGE_EVENT, update);
    window.addEventListener('storage', update);
    return () => {
      window.removeEventListener(CHANGE_EVENT, update);
      window.removeEventListener('storage', update);
    };
  }, []);
  return { signedIn: Boolean(token) };
}

function authHeaders(token = currentToken()) {
  return { Authorization: `Bearer ${token}` };
}

async function call(path, body) {
  try {
    return await apiFetch(path, { method: 'POST', body: body || {}, headers: authHeaders() });
  } catch (err) {
    // A rejected token ends the session, so the drawer stops offering actions.
    if (err instanceof ApiError && err.status === 401) signOut();
    throw err;
  }
}

function ticketPath(ref, action) {
  return `/api/admin/tickets/${encodeURIComponent(ref)}/${action}`;
}

export const adminApi = {
  login: (token) => apiFetch('/api/admin/login', { method: 'POST', body: {}, headers: authHeaders(token) }),
  approveReply: (ref, text) => call(ticketPath(ref, 'reply'), { text }),
  override: (ref, changes) => call(ticketPath(ref, 'override'), changes),
  resolve: (ref, note) => call(ticketPath(ref, 'resolve'), { note }),
  reopen: (ref, note) => call(ticketPath(ref, 'reopen'), note ? { note } : {}),
};

// Downloads the overrides CSV. A plain link cannot carry the bearer header, so the file
// is fetched and handed to the browser as a blob.
export async function downloadOverridesCsv() {
  let res;
  try {
    res = await fetch('/api/admin/overrides.csv', { headers: authHeaders(), credentials: 'same-origin' });
  } catch {
    throw new ApiError('Could not reach the service', 0);
  }
  if (!res.ok) {
    if (res.status === 401) signOut();
    let message = `The service returned status ${res.status}`;
    try {
      const data = await res.json();
      if (data && data.error) message = data.error;
    } catch {
      // Not JSON, keep the generic message.
    }
    throw new ApiError(message, res.status);
  }
  const blob = await res.blob();
  const disposition = res.headers.get('content-disposition') || '';
  const match = disposition.match(/filename="([^"]+)"/);
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = match ? match[1] : 'triageops-overrides.csv';
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// Short, plain messages for the errors an analyst can act on.
export function adminErrorMessage(err) {
  if (!err) return '';
  if (err.status === 401) return 'Your analyst session has ended. Sign in again to continue.';
  if (err.status === 429) return 'Too many failed sign in attempts from this connection. Please wait up to 15 minutes and try again.';
  if (err.status === 503 && /not configured/i.test(err.message)) return 'The analyst console is not configured on this server.';
  if (err.status === 409 || err.status === 422 || err.status === 404) return err.message;
  if (err.status >= 500) return 'The service could not save this change. Please try again in a moment.';
  return `${err.message}. Check your connection and try again.`;
}
