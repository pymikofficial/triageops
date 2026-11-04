// Shared taxonomy, mirrors CONTRACT.md. Labels follow UK English and sentence case.

export const PRIORITIES = ['P1', 'P2', 'P3', 'P4'];

export const PRIORITY_META = {
  P1: { label: 'Critical', sla: '1 hour', colour: '#C62828' },
  P2: { label: 'High', sla: '4 hours', colour: '#E65100' },
  P3: { label: 'Normal', sla: '24 hours', colour: '#2F5BEA' },
  P4: { label: 'Low', sla: '72 hours', colour: '#5F6670' },
};

export const CATEGORIES = [
  'Network',
  'Hardware',
  'Software',
  'Access & Identity',
  'Security',
  'Email & M365',
  'Backup & Recovery',
  'Billing & Account',
  'Other',
];

export const TEAMS = ['Service Desk L1', 'Infrastructure L2', 'Security Ops', 'Cloud & M365', 'Account Management'];

export const STATUSES = ['new', 'triaged', 'escalated', 'resolved', 'failed'];

export const STATUS_LABEL = {
  new: 'New',
  triaged: 'Triaged',
  escalated: 'Escalated',
  resolved: 'Resolved',
  failed: 'Failed',
};

export const CHANNELS = ['email', 'whatsapp', 'portal', 'webhook'];

export const CHANNEL_LABEL = {
  email: 'Email',
  whatsapp: 'WhatsApp',
  portal: 'Portal',
  webhook: 'Webhook',
};

export const SENTIMENT_LABEL = {
  positive: 'Positive',
  neutral: 'Neutral',
  frustrated: 'Frustrated',
  angry: 'Angry',
};

// Matches OPEN_STATUSES in triageops/taxonomy.py.
export const OPEN_STATUSES = new Set(['new', 'triaged', 'escalated']);

export function labelFor(map, key) {
  if (!key) return 'Not set';
  return map[key] || String(key).charAt(0).toUpperCase() + String(key).slice(1);
}
