// Optimistic updates for analyst actions. Each function takes the drawer data
// ({ ticket, events }) and returns what it should look like once the server agrees.
// Pending events carry pending: true until the real response replaces them.

let counter = 0;

function pendingEvent(type, detail) {
  counter += 1;
  return { id: `pending-${counter}`, type, created_at: new Date().toISOString(), detail, pending: true };
}

function withEvents(data, ticketChanges, events) {
  return {
    ...data,
    ticket: { ...data.ticket, ...ticketChanges },
    events: [...(data.events || []), ...events],
  };
}

export function optimisticReply(data, text) {
  const edited = text.trim() !== String(data.ticket.suggested_reply || '').trim();
  return withEvents(data, {}, [pendingEvent('reply_approved', { text: text.trim(), edited, by: 'analyst' })]);
}

export function optimisticOverride(data, { priority, team, reason }) {
  const changes = {};
  const events = [];
  [
    ['priority', priority],
    ['team', team],
  ].forEach(([field, value]) => {
    if (!value || value === data.ticket[field]) return;
    changes[field] = value;
    events.push(pendingEvent('human_override', { field, from: data.ticket[field] ?? null, to: value, reason, by: 'analyst' }));
  });
  return withEvents(data, changes, events);
}

export function optimisticResolve(data, note) {
  return withEvents(data, { status: 'resolved' }, [
    pendingEvent('resolved', { note, from: data.ticket.status, to: 'resolved', by: 'analyst' }),
  ]);
}

export function statusBeforeResolution(events) {
  const last = [...(events || [])].reverse().find((e) => e.type === 'resolved');
  const previous = last && last.detail && last.detail.from;
  return previous && previous !== 'resolved' ? previous : 'triaged';
}

export function optimisticReopen(data, note) {
  const status = statusBeforeResolution(data.events);
  const detail = { from: 'resolved', to: status, by: 'analyst' };
  if (note) detail.note = note;
  return withEvents(data, { status }, [pendingEvent('reopened', detail)]);
}
