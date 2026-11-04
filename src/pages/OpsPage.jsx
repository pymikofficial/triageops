import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../lib/api.js';
import { useDocumentTitle, usePolling } from '../lib/hooks.js';
import { formatInteger, formatPercent } from '../lib/format.js';
import { PRIORITIES, PRIORITY_META } from '../lib/taxonomy.js';
import './OpsPage.css';

const RANGES = [7, 14, 30];
const REFRESH_MS = 30000;
const FALLBACK_MODEL = 'rules-fallback';

const compactFormatter = new Intl.NumberFormat('en-GB', { notation: 'compact', maximumFractionDigits: 1 });
const dayFormatter = new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short', timeZone: 'UTC' });
const longDayFormatter = new Intl.DateTimeFormat('en-GB', {
  weekday: 'short',
  day: 'numeric',
  month: 'short',
  timeZone: 'UTC',
});

// Triage on Haiku costs fractions of a cent, so small amounts keep more decimals.
export function formatUsd(value) {
  const v = Number(value);
  if (!Number.isFinite(v)) return 'Not recorded';
  if (v === 0) return '$0.00';
  if (v >= 1) return `$${v.toFixed(2)}`;
  if (v >= 0.01) return `$${v.toFixed(3)}`;
  if (v >= 0.0001) return `$${v.toFixed(4)}`;
  return 'under $0.0001';
}

function formatTokens(value) {
  const v = Number(value);
  return Number.isFinite(v) ? compactFormatter.format(v) : '0';
}

function formatMs(value) {
  if (value === null || value === undefined) return 'None yet';
  const v = Number(value);
  return v >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${Math.round(v)} ms`;
}

function dayLabel(iso, long = false) {
  const d = new Date(`${iso}T00:00:00Z`);
  return Number.isNaN(d.getTime()) ? iso : (long ? longDayFormatter : dayFormatter).format(d);
}

function modelLabel(model) {
  return model === FALLBACK_MODEL ? 'Keyword rules' : model;
}

function Kpi({ label, value, note, tone }) {
  return (
    <div className={`kpi${tone ? ` kpi-${tone}` : ''}`}>
      <dt className="kpi-label">{label}</dt>
      <dd className="kpi-value">{value}</dd>
      {note ? <dd className="kpi-note">{note}</dd> : null}
    </div>
  );
}

function OpsKpis({ data }) {
  const t = data ? data.totals : {};
  const latency = data ? data.latency_ms : {};
  const sla = data ? data.sla : {};
  const fallbackRate = data ? data.fallback_rate : null;
  const placeholder = '0';
  return (
    <dl className={`kpi-strip ops-kpis${data ? '' : ' is-loading'}`} aria-busy={data ? undefined : 'true'}>
      <Kpi
        label="Estimated spend"
        value={data ? formatUsd(t.cost_usd) : placeholder}
        note={data ? (t.cost_per_ticket_usd !== null ? `${formatUsd(t.cost_per_ticket_usd)} per triaged ticket` : 'No triaged tickets yet') : null}
      />
      <Kpi
        label="Tokens"
        value={data ? formatTokens(t.input_tokens + t.output_tokens) : placeholder}
        note={data ? `${formatTokens(t.input_tokens)} in, ${formatTokens(t.output_tokens)} out` : null}
      />
      <Kpi
        label="Median triage time"
        value={data ? formatMs(latency.p50) : placeholder}
        note={data ? `p50 across ${formatInteger(latency.samples)} Claude calls` : null}
      />
      <Kpi
        label="p95 triage time"
        value={data ? formatMs(latency.p95) : placeholder}
        note={data ? 'One call in twenty is slower than this' : null}
      />
      <Kpi
        label="Fallback rate"
        value={data ? (fallbackRate === null ? 'None yet' : formatPercent(fallbackRate)) : placeholder}
        note={data ? `${formatInteger(t.fallback)} triaged by keyword rules` : null}
        tone={fallbackRate !== null && fallbackRate >= 0.1 ? 'critical' : null}
      />
      <Kpi
        label="SLA breaches"
        value={data ? formatInteger(sla.breached) : placeholder}
        note={data ? (sla.breach_rate === null ? 'No SLAs set yet' : `${formatPercent(sla.breach_rate)} of tickets with an SLA`) : null}
        tone={sla.breached > 0 ? 'critical' : null}
      />
    </dl>
  );
}

function niceMax(value) {
  if (!(value > 0)) return 0;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const step = [1, 2, 2.5, 5, 10].find((s) => s * magnitude >= value);
  return step * magnitude;
}

function CostChart({ daily }) {
  const [active, setActive] = useState(null);
  const max = niceMax(Math.max(0, ...daily.map((d) => d.cost_usd)));
  const labelEvery = daily.length > 14 ? 5 : 2;
  const total = daily.reduce((sum, d) => sum + d.cost_usd, 0);
  const activeDay = active === null ? null : daily[active];

  return (
    <div className="cost-chart">
      <div className="cost-plot" role="img" aria-label={`Estimated triage cost per day, ${formatUsd(total)} in total. The table below lists every day.`}>
        <div className="cost-grid" aria-hidden="true">
          {[1, 0.5, 0].map((f) => (
            <div key={f} className="cost-gridline" style={{ bottom: `${f * 100}%` }}>
              <span className="cost-gridlabel">{max > 0 ? formatUsd(max * f) : f === 0 ? '$0.00' : ''}</span>
            </div>
          ))}
        </div>
        <div className="cost-columns" onMouseLeave={() => setActive(null)}>
          {daily.map((d, i) => {
            const pct = max > 0 ? (d.cost_usd / max) * 100 : 0;
            return (
              <div
                key={d.date}
                className={`cost-slot${active === i ? ' is-active' : ''}`}
                tabIndex={0}
                aria-label={`${dayLabel(d.date, true)}: ${formatUsd(d.cost_usd)}, ${formatInteger(d.tickets)} tickets`}
                onMouseEnter={() => setActive(i)}
                onFocus={() => setActive(i)}
                onBlur={() => setActive(null)}
              >
                <span className="cost-column" style={{ height: `${d.cost_usd > 0 ? Math.max(pct, 1.5) : 0}%` }} />
              </div>
            );
          })}
        </div>
        {activeDay ? (
          <div
            className={`cost-tooltip${active > daily.length / 2 ? ' is-left' : ''}`}
            style={{ left: `${((active + 0.5) / daily.length) * 100}%` }}
            aria-hidden="true"
          >
            <strong>{dayLabel(activeDay.date, true)}</strong>
            <span>{formatUsd(activeDay.cost_usd)}</span>
            <span>
              {formatInteger(activeDay.tickets)} tickets, {formatTokens(activeDay.input_tokens + activeDay.output_tokens)} tokens
            </span>
            <span>p95 {formatMs(activeDay.p95_ms)}</span>
          </div>
        ) : null}
      </div>
      <div className="cost-axis" aria-hidden="true">
        {daily.map((d, i) => {
          // Count back from today so the latest day is always labelled. Narrow screens keep every other label.
          const step = daily.length - 1 - i;
          if (step % labelEvery !== 0) return <span key={d.date} />;
          return (
            <span key={d.date} className={step % (labelEvery * 2) === 0 ? undefined : 'is-minor'}>
              {dayLabel(d.date)}
            </span>
          );
        })}
      </div>
    </div>
  );
}

function DailyTable({ daily }) {
  return (
    <details className="ops-table-details">
      <summary>Show every day as a table</summary>
      <div className="table-wrap">
        <table className="queue-table ops-table">
          <thead>
            <tr>
              <th scope="col">Day</th>
              <th scope="col">Tickets</th>
              <th scope="col">Input tokens</th>
              <th scope="col">Output tokens</th>
              <th scope="col">Cost</th>
              <th scope="col">p50</th>
              <th scope="col">p95</th>
              <th scope="col">Fallback</th>
              <th scope="col">SLA breaches</th>
            </tr>
          </thead>
          <tbody>
            {[...daily].reverse().map((d) => (
              <tr key={d.date}>
                <th scope="row">{dayLabel(d.date, true)}</th>
                <td>{formatInteger(d.tickets)}</td>
                <td>{formatInteger(d.input_tokens)}</td>
                <td>{formatInteger(d.output_tokens)}</td>
                <td>{formatUsd(d.cost_usd)}</td>
                <td>{formatMs(d.p50_ms)}</td>
                <td>{formatMs(d.p95_ms)}</td>
                <td>{formatInteger(d.fallback)}</td>
                <td>{formatInteger(d.sla_breached)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

function ModelMix({ models }) {
  if (models.length === 0) return <p className="block-empty">No tickets have been triaged in this period.</p>;
  return (
    <ul className="bar-list ops-bar-list" aria-label="Tickets by triage model">
      {models.map((m) => {
        const pct = Math.round((m.share || 0) * 100);
        return (
          <li key={m.model} className="bar-row ops-bar-row">
            <span className={`bar-label${m.model === FALLBACK_MODEL ? '' : ' mono'}`} title={m.model === FALLBACK_MODEL ? 'Keyword rules, used when Claude is unavailable' : m.model}>
              {modelLabel(m.model)}
            </span>
            <span className="bar-track" aria-hidden="true">
              <span className={`bar-fill ${m.model === FALLBACK_MODEL ? 'bar-fallback' : 'bar-model'}`} style={{ width: `${Math.max(pct, 2)}%` }} />
            </span>
            <span className="bar-value">{pct}%</span>
            <span className="ops-bar-note">
              {formatInteger(m.tickets)} tickets, {formatUsd(m.cost_usd)}
              {m.p95_ms !== null ? `, p95 ${formatMs(m.p95_ms)}` : ''}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

function SlaByPriority({ sla }) {
  const rows = PRIORITIES.map((p) => ({ p, value: Number(sla.by_priority[p]) || 0 }));
  const max = Math.max(0, ...rows.map((r) => r.value));
  return (
    <ul className="bar-list" aria-label="SLA breaches by priority">
      {rows.map(({ p, value }) => (
        <li key={p} className="bar-row">
          <span className="bar-label" title={`${p} ${PRIORITY_META[p].label}, SLA ${PRIORITY_META[p].sla}`}>
            <span className="bar-key">{p}</span> {PRIORITY_META[p].label}
          </span>
          <span className="bar-track" aria-hidden="true">
            <span className={`bar-fill bar-${p.toLowerCase()}`} style={{ width: `${max > 0 ? Math.max(value > 0 ? 2 : 0, Math.round((value / max) * 100)) : 0}%` }} />
          </span>
          <span className="bar-value">{formatInteger(value)}</span>
        </li>
      ))}
    </ul>
  );
}

export default function OpsPage() {
  useDocumentTitle('Operations | TriageOps');
  const [days, setDays] = useState(14);
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const controller = useRef(null);

  const load = useCallback(async () => {
    if (controller.current) controller.current.abort();
    const current = new AbortController();
    controller.current = current;
    try {
      const result = await api.opsMetrics(days, { signal: current.signal });
      if (current.signal.aborted) return;
      setData(result);
      setError(null);
    } catch (err) {
      if (current.signal.aborted) return;
      setError(err);
    }
  }, [days]);

  useEffect(() => {
    setData((previous) => (previous && previous.window.days === days ? previous : null));
    load();
    return () => controller.current && controller.current.abort();
  }, [days, load]);
  usePolling(load, REFRESH_MS);

  const pricing = data ? Object.entries(data.pricing) : [];

  return (
    <main id="main" className="container ops-page">
      <div className="page-head">
        <h1 className="page-title">Operations</h1>
        <p className="page-lede">
          Spend, speed and reliability of AI triage, worked out from the ticket table. Costs are estimates at Anthropic list prices in US
          dollars and cover triage calls only, not copilot chat.
        </p>
      </div>

      <div className="ops-toolbar">
        <div className="ops-range" role="group" aria-label="Time range">
          {RANGES.map((r) => (
            <button
              key={r}
              type="button"
              className={`ops-range-button${days === r ? ' is-active' : ''}`}
              aria-pressed={days === r}
              onClick={() => setDays(r)}
            >
              {r} days
            </button>
          ))}
        </div>
        {data ? (
          <p className="ops-window muted">
            {dayLabel(data.window.from, true)} to {dayLabel(data.window.to, true)}, UK time. Refreshes every 30 seconds.
          </p>
        ) : null}
      </div>

      {error ? (
        <div className="panel ops-error" role="alert">
          <p className="state-title">The operations metrics could not be loaded</p>
          <p className="state-text">
            {error.message}. The page tries again every 30 seconds.
            {error.requestId ? (
              <>
                {' '}
                Quote reference <span className="mono">{error.requestId}</span> when reporting this.
              </>
            ) : null}
          </p>
        </div>
      ) : null}

      <section className="overview" aria-label="Headline figures">
        <OpsKpis data={data} />
      </section>

      {data ? (
        <>
          {data.truncated ? (
            <p className="ops-warning" role="status">
              This period has more tickets than one request reads, so the figures cover the earliest tickets only. Choose a shorter range.
            </p>
          ) : null}

          <section className="panel ops-panel" aria-labelledby="cost-per-day">
            <div className="panel-head">
              <h2 id="cost-per-day" className="panel-title">
                Estimated cost per day
              </h2>
              <p className="panel-sub">
                {formatUsd(data.totals.cost_usd)} across {formatInteger(data.totals.ai_attempted)} triaged tickets.
                {data.totals.unpriced_tickets > 0 ? ` ${formatInteger(data.totals.unpriced_tickets)} tickets used a model with no known price and count as zero.` : ''}
              </p>
            </div>
            <div className="ops-panel-body">
              <CostChart key={data.window.days} daily={data.daily} />
              <DailyTable daily={data.daily} />
            </div>
          </section>

          <div className="ops-grid">
            <section className="panel ops-panel" aria-labelledby="model-mix">
              <div className="panel-head">
                <h2 id="model-mix" className="panel-title">
                  Model mix
                </h2>
                <p className="panel-sub">Share of triaged tickets by the model that produced the decision.</p>
              </div>
              <div className="ops-panel-body">
                <ModelMix models={data.model_mix} />
              </div>
            </section>

            <section className="panel ops-panel" aria-labelledby="sla-by-priority">
              <div className="panel-head">
                <h2 id="sla-by-priority" className="panel-title">
                  SLA breaches by priority
                </h2>
                <p className="panel-sub">
                  {formatInteger(data.sla.breached)} of {formatInteger(data.sla.with_sla)} tickets with an SLA in this period.
                </p>
              </div>
              <div className="ops-panel-body">
                <SlaByPriority sla={data.sla} />
              </div>
            </section>
          </div>

          <p className="ops-footnote muted">
            List prices used, per million tokens in and out:{' '}
            {pricing.map(([model, price], i) => (
              <span key={model}>
                <span className="mono">{model}</span> ${price.input_per_million.toFixed(2)} and ${price.output_per_million.toFixed(2)}
                {i < pricing.length - 1 ? ', ' : '.'}
              </span>
            ))}{' '}
            Each API response carries an <span className="mono">X-Request-ID</span> header that matches the server logs.
          </p>
        </>
      ) : null}
    </main>
  );
}
