import { CATEGORIES, CHANNELS, CHANNEL_LABEL, PRIORITIES, PRIORITY_META, STATUSES, STATUS_LABEL, labelFor } from '../lib/taxonomy.js';
import { formatInteger } from '../lib/format.js';

function Kpi({ label, value, note, tone }) {
  return (
    <div className={`kpi${tone ? ` kpi-${tone}` : ''}`}>
      <dt className="kpi-label">{label}</dt>
      <dd className="kpi-value">{value}</dd>
      {note ? <dd className="kpi-note">{note}</dd> : null}
    </div>
  );
}

export function KpiStrip({ stats, openP1 }) {
  const s = stats || {};
  // Until stats arrive the values render as quiet placeholder blocks.
  const placeholder = '0';
  const avg = Number(s.avg_latency_ms);
  const avgText = Number.isFinite(avg) && avg > 0 ? (avg / 1000).toFixed(1) : stats ? '0.0' : placeholder;
  const p1 = Number.isFinite(Number(openP1)) ? Number(openP1) : null;
  const breaches = Number(s.sla_breached) || 0;
  const injections = Number(s.injection_flagged) || 0;

  return (
    <dl className={`kpi-strip${stats ? '' : ' is-loading'}`} aria-busy={stats ? undefined : 'true'}>
      <Kpi
        label="Total tickets"
        value={stats ? formatInteger(s.total) : placeholder}
        note={stats ? `${formatInteger(s.last_24h)} in the last 24 hours` : null}
      />
      <Kpi
        label="Open P1"
        value={p1 === null ? (stats ? 'n/a' : placeholder) : formatInteger(p1)}
        note={stats ? `${formatInteger(s.open)} open tickets in total` : null}
        tone={p1 > 0 ? 'critical' : null}
      />
      <Kpi
        label="Average AI triage time"
        value={
          <>
            {avgText}
            {stats ? <span className="kpi-unit"> s</span> : null}
          </>
        }
        note={stats ? 'From arrival to a triaged ticket' : null}
      />
      <Kpi
        label="SLA breaches"
        value={stats ? formatInteger(breaches) : placeholder}
        note={stats ? (breaches > 0 ? 'Tickets past their SLA due time' : 'Every ticket within its SLA') : null}
        tone={breaches > 0 ? 'critical' : null}
      />
      <Kpi
        label="Injection attempts caught"
        value={stats ? formatInteger(injections) : placeholder}
        note={stats ? 'Held for human review' : null}
      />
    </dl>
  );
}

function BarList({ rows, max, ariaLabel }) {
  return (
    <ul className="bar-list" aria-label={ariaLabel}>
      {rows.map((row) => {
        const pct = max > 0 ? Math.max(row.value > 0 ? 2 : 0, Math.round((row.value / max) * 100)) : 0;
        return (
          <li key={row.key} className="bar-row">
            <span className="bar-label" title={row.title}>
              {row.label}
            </span>
            <span className="bar-track" aria-hidden="true">
              <span className={`bar-fill ${row.className || ''}`} style={{ width: `${pct}%` }} />
            </span>
            <span className="bar-value">{formatInteger(row.value)}</span>
          </li>
        );
      })}
    </ul>
  );
}

function orderedEntries(record, order) {
  const r = record || {};
  const known = order.map((key) => [key, Number(r[key]) || 0]);
  const extra = Object.keys(r)
    .filter((k) => !order.includes(k))
    .map((k) => [k, Number(r[k]) || 0]);
  return [...known, ...extra];
}

export function Breakdown({ stats, loading }) {
  const s = stats || {};
  const priorityRows = PRIORITIES.map((p) => ({
    key: p,
    label: (
      <>
        <span className="bar-key">{p}</span> {PRIORITY_META[p].label}
      </>
    ),
    title: `${p} ${PRIORITY_META[p].label}, SLA ${PRIORITY_META[p].sla}`,
    value: Number((s.by_priority || {})[p]) || 0,
    className: `bar-${p.toLowerCase()}`,
  }));
  const priorityMax = Math.max(0, ...priorityRows.map((r) => r.value));

  const categoryRows = orderedEntries(s.by_category, CATEGORIES)
    .filter(([, v]) => v > 0)
    .sort((a, b) => b[1] - a[1])
    .map(([key, value]) => ({ key, label: key, title: key, value, className: 'bar-category' }));
  const categoryMax = Math.max(0, ...categoryRows.map((r) => r.value));

  const channels = orderedEntries(s.by_channel, CHANNELS);
  const statuses = orderedEntries(s.by_status, STATUSES);

  return (
    <div className="breakdown">
      <section className="breakdown-block" aria-labelledby="by-priority">
        <h2 id="by-priority" className="block-title">
          By priority
        </h2>
        <BarList rows={priorityRows} max={priorityMax} ariaLabel="Tickets by priority" />
      </section>
      <section className="breakdown-block" aria-labelledby="by-category">
        <h2 id="by-category" className="block-title">
          By category
        </h2>
        {categoryRows.length > 0 ? (
          <BarList rows={categoryRows} max={categoryMax} ariaLabel="Tickets by category" />
        ) : (
          <p className="block-empty">{stats ? 'No tickets categorised yet.' : loading ? 'Loading' : 'Not available until the service responds.'}</p>
        )}
      </section>
      <section className="breakdown-block breakdown-chips" aria-labelledby="by-channel">
        <h2 id="by-channel" className="block-title">
          By channel
        </h2>
        <ul className="chip-list" aria-label="Tickets by channel">
          {channels.map(([key, value]) => (
            <li key={key} className={`count-chip channel-chip-${key}${value === 0 ? ' is-zero' : ''}`}>
              <span>{labelFor(CHANNEL_LABEL, key)}</span>
              <strong>{formatInteger(value)}</strong>
            </li>
          ))}
        </ul>
        <h2 className="block-title block-title-spaced" id="by-status">
          By status
        </h2>
        <ul className="chip-list" aria-labelledby="by-status">
          {statuses.map(([key, value]) => (
            <li key={key} className={`count-chip status-chip-${key}${value === 0 ? ' is-zero' : ''}`}>
              <span>{labelFor(STATUS_LABEL, key)}</span>
              <strong>{formatInteger(value)}</strong>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
