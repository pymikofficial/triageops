import { CHANNEL_LABEL, PRIORITY_META, SENTIMENT_LABEL, STATUS_LABEL, labelFor } from '../lib/taxonomy.js';

export function PriorityBadge({ priority, showLabel = false, size }) {
  if (!priority) {
    return <span className="priority-badge priority-none">Pending</span>;
  }
  const meta = PRIORITY_META[priority];
  return (
    <span
      className={`priority-badge priority-${priority.toLowerCase()}${size === 'lg' ? ' priority-badge-lg' : ''}`}
      title={meta ? `${priority} ${meta.label}, SLA ${meta.sla}` : priority}
    >
      <span className="priority-dot" aria-hidden="true" />
      {priority}
      {showLabel && meta ? <span className="priority-word">{meta.label}</span> : null}
    </span>
  );
}

export function StatusBadge({ status }) {
  return (
    <span className={`status-badge status-${status || 'unknown'}`}>
      <span className="status-dot" aria-hidden="true" />
      {labelFor(STATUS_LABEL, status)}
    </span>
  );
}

export function ChannelTag({ channel }) {
  return <span className={`channel-tag channel-${channel || 'unknown'}`}>{labelFor(CHANNEL_LABEL, channel)}</span>;
}

export function SentimentText({ sentiment }) {
  return <span className={`sentiment sentiment-${sentiment || 'unknown'}`}>{labelFor(SENTIMENT_LABEL, sentiment)}</span>;
}
