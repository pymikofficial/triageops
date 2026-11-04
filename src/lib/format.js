// Formatting helpers. All times are shown for a UK audience (en-GB, Europe/London).

const absoluteFormatter = new Intl.DateTimeFormat('en-GB', {
  day: 'numeric',
  month: 'short',
  hour: '2-digit',
  minute: '2-digit',
  timeZone: 'Europe/London',
  timeZoneName: 'short',
});

const clockFormatter = new Intl.DateTimeFormat('en-GB', {
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  timeZone: 'Europe/London',
});

const integerFormatter = new Intl.NumberFormat('en-GB');

export function toDate(value) {
  if (!value) return null;
  const d = value instanceof Date ? value : new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
}

export function formatAbsolute(value) {
  const d = toDate(value);
  return d ? absoluteFormatter.format(d) : 'Unknown time';
}

export function formatClock(value) {
  const d = toDate(value);
  return d ? clockFormatter.format(d) : '';
}

export function formatInteger(n) {
  const v = Number(n);
  return Number.isFinite(v) ? integerFormatter.format(v) : '0';
}

// Compact duration such as "45 min", "2 h 10 min", "3 d".
export function formatDuration(ms) {
  const totalMinutes = Math.max(0, Math.round(Math.abs(ms) / 60000));
  if (totalMinutes < 1) return 'under 1 min';
  if (totalMinutes < 60) return `${totalMinutes} min`;
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (hours < 10) return minutes ? `${hours} h ${minutes} min` : `${hours} h`;
  if (hours < 48) return `${hours} h`;
  return `${Math.round(hours / 24)} d`;
}

export function formatAgo(value, now = Date.now(), { compact = false } = {}) {
  const d = toDate(value);
  if (!d) return 'Unknown';
  const diff = now - d.getTime();
  if (diff < 45000) return 'just now';
  if (compact && diff >= 3600000) {
    const hours = Math.floor(diff / 3600000);
    return hours < 48 ? `${hours} h ago` : `${Math.round(hours / 24)} d ago`;
  }
  return `${formatDuration(diff)} ago`;
}

export function formatSeconds(ms, digits = 1) {
  const v = Number(ms);
  if (!Number.isFinite(v)) return 'Not recorded';
  return `${(v / 1000).toFixed(digits)} s`;
}

export function formatOffset(ms) {
  const v = Math.max(0, Number(ms) || 0);
  if (v < 60000) return `+${(v / 1000).toFixed(1)} s`;
  return `+${formatDuration(v)}`;
}

export function formatPercent(value) {
  const v = Number(value);
  if (!Number.isFinite(v)) return 'Not recorded';
  const pct = v <= 1 ? v * 100 : v;
  return `${Math.round(pct)}%`;
}

export function toFraction(value) {
  const v = Number(value);
  if (!Number.isFinite(v)) return 0;
  const f = v <= 1 ? v : v / 100;
  return Math.min(1, Math.max(0, f));
}

// SLA state for a ticket. Returns text, tone (normal, soon, breached, met, none) and a title.
export function slaState(ticket, now = Date.now()) {
  const due = toDate(ticket && ticket.sla_due_at);
  if (!due) return { text: 'Not set', tone: 'none', title: 'No SLA has been set yet' };
  const remaining = due.getTime() - now;
  const resolved = ticket.status === 'resolved';
  const title = `SLA due ${formatAbsolute(due)}`;
  if (ticket.sla_breached) {
    return { text: resolved ? 'Breached' : `Overdue ${formatDuration(remaining)}`, tone: 'breached', title };
  }
  if (resolved) return { text: 'Met', tone: 'met', title };
  if (remaining <= 0) return { text: `Overdue ${formatDuration(remaining)}`, tone: 'breached', title };
  return { text: `in ${formatDuration(remaining)}`, tone: remaining < 30 * 60000 ? 'soon' : 'normal', title };
}

export function humanise(key) {
  const s = String(key).replace(/[_-]+/g, ' ').trim();
  return s.charAt(0).toUpperCase() + s.slice(1);
}
