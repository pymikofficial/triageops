// Thin client for the TriageOps REST API. Every call is same origin under /api.

export class ApiError extends Error {
  constructor(message, status = 0, body = null, requestId = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
    // The server's X-Request-ID, so a failure can be matched to its log lines.
    this.requestId = requestId;
  }
}

function messageFrom(data, status) {
  if (data && typeof data.error === 'string' && data.error) return data.error;
  if (data && typeof data.detail === 'string' && data.detail) return data.detail;
  if (data && Array.isArray(data.detail) && data.detail.length > 0) {
    const first = data.detail[0];
    const field = Array.isArray(first.loc) ? first.loc[first.loc.length - 1] : null;
    if (first.msg) return field ? `${String(field)} ${first.msg.charAt(0).toLowerCase()}${first.msg.slice(1)}` : first.msg;
  }
  return `The service returned status ${status}`;
}

export async function apiFetch(path, { method = 'GET', body, timeout = 20000, signal } = {}) {
  const controller = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeout);
  const forwardAbort = () => controller.abort();
  if (signal) {
    if (signal.aborted) controller.abort();
    else signal.addEventListener('abort', forwardAbort, { once: true });
  }

  try {
    const headers = { Accept: 'application/json' };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    const res = await fetch(path, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: controller.signal,
      credentials: 'same-origin',
    });
    const text = await res.text();
    let data = null;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch {
        data = null;
      }
    }
    const requestId = res.headers.get('X-Request-ID');
    if (!res.ok) throw new ApiError(messageFrom(data, res.status), res.status, data, requestId);
    if (data === null) throw new ApiError('The service returned an empty response', res.status, null, requestId);
    return data;
  } catch (err) {
    if (err instanceof ApiError) throw err;
    if (signal && signal.aborted) throw err;
    if (timedOut) throw new ApiError('The request took too long and was stopped', 0);
    throw new ApiError('Could not reach the service', 0);
  } finally {
    clearTimeout(timer);
    if (signal) signal.removeEventListener('abort', forwardAbort);
  }
}

function query(params) {
  const search = new URLSearchParams();
  Object.entries(params || {}).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') search.set(key, String(value));
  });
  const s = search.toString();
  return s ? `?${s}` : '';
}

export const api = {
  health: (opts) => apiFetch('/api/health', { timeout: 12000, ...opts }),
  stats: (opts) => apiFetch('/api/stats', opts),
  tickets: (params, opts) => apiFetch(`/api/tickets${query(params)}`, opts),
  ticket: (ref, opts) => apiFetch(`/api/tickets/${encodeURIComponent(ref)}`, opts),
  createDemoTicket: (payload, opts) =>
    apiFetch('/api/demo/tickets', { method: 'POST', body: payload, timeout: 65000, ...opts }),
  opsMetrics: (days, opts) => apiFetch(`/api/ops/metrics${query({ days })}`, opts),
  chat: (messages, opts) =>
    apiFetch('/api/chat', { method: 'POST', body: { messages }, timeout: 65000, ...opts }),
};
