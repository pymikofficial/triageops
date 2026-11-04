// Turns ticket_events rows into readable timeline entries. The detail column is free
// form JSON, so this reads the common keys first and falls back to a key and value list.
import { formatPercent, formatSeconds, humanise } from './format.js';

export const EVENT_LABEL = {
  received: 'Received',
  masked: 'PII masked',
  kb_matched: 'Knowledge base searched',
  triaged: 'Triaged',
  rule_applied: 'Business rule applied',
  escalated: 'Escalated',
  alert_sent: 'Alert sent',
  alert_skipped: 'Alert skipped',
  sla_breached: 'SLA breached',
  error: 'Error',
  human_review: 'Human review requested',
};

export const EVENT_TONE = {
  received: 'neutral',
  masked: 'neutral',
  kb_matched: 'neutral',
  triaged: 'accent',
  rule_applied: 'warn',
  escalated: 'danger',
  alert_sent: 'danger',
  alert_skipped: 'muted',
  sla_breached: 'danger',
  error: 'danger',
  human_review: 'warn',
};

const TEXT_KEYS = ['text', 'message', 'detail', 'description', 'rule', 'reason', 'summary', 'note'];

function asText(value) {
  if (value === null || value === undefined || value === '') return null;
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (Array.isArray(value)) {
    const parts = value.map((v) => (typeof v === 'object' && v ? v.title || v.slug || v.name || null : asText(v))).filter(Boolean);
    return parts.length ? parts.join(', ') : null;
  }
  return null;
}

function formatValue(key, value) {
  if (/latency|_ms$/.test(key) && Number.isFinite(Number(value))) return formatSeconds(value);
  if (/similarity|confidence/.test(key) && Number.isFinite(Number(value))) return formatPercent(value);
  return asText(value);
}

// Returns { text, facts: [[label, value]], items: [{title, meta}] } for one event.
export function describeEvent(event) {
  const detail = event && event.detail;
  const out = { text: null, facts: [], items: [] };
  if (detail === null || detail === undefined) return out;
  if (typeof detail !== 'object') {
    out.text = asText(detail);
    return out;
  }

  const used = new Set();
  for (const key of TEXT_KEYS) {
    if (typeof detail[key] === 'string' && detail[key].trim()) {
      out.text = detail[key];
      used.add(key);
      break;
    }
  }

  if (detail.from !== undefined && detail.to !== undefined) {
    out.facts.push(['Change', `${asText(detail.from)} to ${asText(detail.to)}`]);
    used.add('from');
    used.add('to');
  }

  const listKey = ['matches', 'articles', 'kb_refs', 'results'].find((k) => Array.isArray(detail[k]));
  if (listKey) {
    used.add(listKey);
    out.items = detail[listKey].map((m) =>
      typeof m === 'object' && m
        ? { title: m.title || m.slug || 'Article', meta: m.similarity !== undefined ? formatPercent(m.similarity) : null }
        : { title: String(m), meta: null },
    );
  }

  Object.entries(detail).forEach(([key, value]) => {
    if (used.has(key)) return;
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      Object.entries(value).forEach(([k2, v2]) => {
        const text = formatValue(k2, v2);
        if (text !== null) out.facts.push([humanise(k2), text]);
      });
      return;
    }
    const text = formatValue(key, value);
    if (text !== null) out.facts.push([humanise(key), text]);
  });

  return out;
}

export function eventLabel(event, ticket) {
  if (event.type === 'triaged' && ticket && ticket.ai_model === 'rules-fallback') return 'Triaged by keyword rules';
  if (event.type === 'triaged') return 'Triaged by Claude';
  return EVENT_LABEL[event.type] || humanise(event.type || 'Event');
}
