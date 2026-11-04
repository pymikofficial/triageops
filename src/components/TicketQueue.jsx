import { useEffect, useId, useState } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { ChannelTag, PriorityBadge, StatusBadge } from './Badges.jsx';
import { RefreshIcon, SearchIcon } from './Icons.jsx';
import { CHANNELS, CHANNEL_LABEL, PRIORITIES, PRIORITY_META, STATUSES, STATUS_LABEL } from '../lib/taxonomy.js';
import { formatAbsolute, formatAgo, slaState } from '../lib/format.js';
import { useNow } from '../lib/hooks.js';

const INITIAL_ROWS = 15;

function FilterSelect({ id, label, value, onChange, options, allLabel }) {
  return (
    <div className="field field-inline">
      <label htmlFor={id} className="field-label">
        {label}
      </label>
      <select id={id} className="select" value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">{allLabel}</option>
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </div>
  );
}

function SlaCell({ ticket, now }) {
  const sla = slaState(ticket, now);
  return (
    <span className={`sla sla-${sla.tone}`} title={sla.title}>
      {sla.text}
    </span>
  );
}

function TicketRow({ ticket, now, fresh, onOpen, search }) {
  const t = ticket;
  const handleClick = (event) => {
    if (event.target.closest('a, button')) return;
    const selection = window.getSelection ? window.getSelection().toString() : '';
    if (selection) return;
    onOpen(t.ref);
  };

  return (
    <tr className={`queue-row priority-edge-${(t.priority || 'none').toLowerCase()}${fresh ? ' is-fresh' : ''}`} onClick={handleClick}>
      <td className="cell-ref">
        <Link className="ref" to={{ pathname: `/ticket/${encodeURIComponent(t.ref)}`, search }} data-ref-link={t.ref}>
          {t.ref}
        </Link>
        <ChannelTag channel={t.channel} />
      </td>
      <td className="cell-received">
        <time dateTime={t.created_at} title={formatAbsolute(t.created_at)}>
          {formatAgo(t.created_at, now, { compact: true })}
        </time>
      </td>
      <td className="cell-ticket">
        <span className="ticket-subject">{t.subject}</span>
        <span className="ticket-customer">
          {t.customer || 'Unknown customer'}
          {t.injection_suspected ? <span className="inline-flag flag-danger">Possible injection</span> : null}
        </span>
      </td>
      <td className="cell-priority">
        <PriorityBadge priority={t.priority} />
      </td>
      <td className="cell-category">
        <span className="ticket-category">{t.category || 'Awaiting triage'}</span>
        <span className="ticket-team">{t.team || 'Unassigned'}</span>
      </td>
      <td className="cell-status">
        <StatusBadge status={t.status} />
      </td>
      <td className="cell-sla">
        <SlaCell ticket={t} now={now} />
      </td>
    </tr>
  );
}

function SkeletonRows() {
  return Array.from({ length: 6 }).map((_, i) => (
    <tr key={i} className="queue-row is-skeleton" aria-hidden="true">
      <td colSpan={7}>
        <span className="skeleton-line" />
      </td>
    </tr>
  ));
}

export default function TicketQueue({ tickets, error, loading, filters, onFiltersChange, onRefresh, refreshing, lastUpdated, freshIds }) {
  const now = useNow(10000);
  const navigate = useNavigate();
  const location = useLocation();
  const uid = useId();
  const [query, setQuery] = useState(filters.q || '');
  const [expanded, setExpanded] = useState(false);

  // Keep the search box in step with the URL (for example after "Clear filters").
  useEffect(() => {
    const next = filters.q || '';
    setQuery((prev) => (prev.trim() === next ? prev : next));
  }, [filters.q]);

  useEffect(() => {
    if ((filters.q || '') === query.trim()) return undefined;
    const id = setTimeout(() => onFiltersChange({ q: query.trim() }), 300);
    return () => clearTimeout(id);
    // onFiltersChange is stable for the lifetime of the dashboard, so only query matters here.
  }, [query]);

  const hasFilters = Boolean(filters.priority || filters.status || filters.channel || filters.q);
  const list = tickets || [];
  const visible = expanded ? list : list.slice(0, INITIAL_ROWS);
  const hidden = list.length - visible.length;
  const search = location.search;

  const openTicket = (ref) => navigate({ pathname: `/ticket/${encodeURIComponent(ref)}`, search });

  let body;
  if (!tickets && loading) {
    body = <SkeletonRows />;
  } else if (error && !tickets) {
    body = (
      <tr>
        <td colSpan={7} className="queue-state">
          <p className="state-title">The queue could not be loaded</p>
          <p className="state-text">{error}. The page retries every 10 seconds.</p>
          <button type="button" className="button" onClick={onRefresh}>
            Try again now
          </button>
        </td>
      </tr>
    );
  } else if (list.length === 0) {
    body = (
      <tr>
        <td colSpan={7} className="queue-state">
          {hasFilters ? (
            <>
              <p className="state-title">No tickets match these filters</p>
              <p className="state-text">Try a different priority, status or channel, or clear the search.</p>
              <button type="button" className="button" onClick={() => onFiltersChange({ priority: '', status: '', channel: '', q: '' })}>
                Clear filters
              </button>
            </>
          ) : (
            <>
              <p className="state-title">The queue is empty</p>
              <p className="state-text">Raise a test ticket and watch Claude triage it here.</p>
            </>
          )}
        </td>
      </tr>
    );
  } else {
    body = visible.map((t) => (
      <TicketRow key={t.id || t.ref} ticket={t} now={now} fresh={freshIds.has(t.id || t.ref)} onOpen={openTicket} search={search} />
    ));
  }

  const updatedText = lastUpdated ? `Updated ${formatAgo(lastUpdated, now)}` : error ? 'Not connected' : 'Connecting';

  return (
    <section className="panel queue-panel" aria-labelledby={`${uid}-title`}>
      <div className="panel-head queue-head">
        <div>
          <h2 className="panel-title" id={`${uid}-title`}>
            Ticket queue
          </h2>
          <p className="panel-sub">
            Newest first. {tickets && list.length > 0 ? `${list.length} ${list.length === 1 ? 'ticket' : 'tickets'}.` : ''} Refreshes every 10 seconds while this tab is open.
          </p>
        </div>
        <div className="queue-refresh">
          <span className="updated" aria-live="off">
            {error && tickets ? <span className="updated-error">Last refresh failed. </span> : null}
            {updatedText}
          </span>
          <button
            type="button"
            className={`icon-button${refreshing ? ' is-spinning' : ''}`}
            onClick={onRefresh}
            aria-label="Refresh the queue now"
            title="Refresh now"
          >
            <RefreshIcon size={16} />
          </button>
        </div>
      </div>

      <form className="filters" role="search" onSubmit={(e) => e.preventDefault()} aria-label="Filter tickets">
        <div className="field field-search">
          <label htmlFor={`${uid}-q`} className="field-label">
            Search
          </label>
          <span className="search-wrap">
            <SearchIcon size={15} className="search-icon" />
            <input
              id={`${uid}-q`}
              className="input input-search"
              type="search"
              placeholder="Subject, customer or ref"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              maxLength={120}
              autoComplete="off"
            />
          </span>
        </div>
        <FilterSelect
          id={`${uid}-priority`}
          label="Priority"
          value={filters.priority || ''}
          onChange={(v) => onFiltersChange({ priority: v })}
          allLabel="All priorities"
          options={PRIORITIES.map((p) => ({ value: p, label: `${p} ${PRIORITY_META[p].label}` }))}
        />
        <FilterSelect
          id={`${uid}-status`}
          label="Status"
          value={filters.status || ''}
          onChange={(v) => onFiltersChange({ status: v })}
          allLabel="All statuses"
          options={STATUSES.map((s) => ({ value: s, label: STATUS_LABEL[s] }))}
        />
        <FilterSelect
          id={`${uid}-channel`}
          label="Channel"
          value={filters.channel || ''}
          onChange={(v) => onFiltersChange({ channel: v })}
          allLabel="All channels"
          options={CHANNELS.map((c) => ({ value: c, label: CHANNEL_LABEL[c] }))}
        />
        {hasFilters ? (
          <button
            type="button"
            className="button button-quiet filters-clear"
            onClick={() => {
              setQuery('');
              onFiltersChange({ priority: '', status: '', channel: '', q: '' });
            }}
          >
            Clear filters
          </button>
        ) : null}
      </form>

      <div className="table-wrap">
        <table className="queue-table">
          <caption className="visually-hidden">Service desk tickets, newest first. Select a ticket reference to open its details.</caption>
          <thead>
            <tr>
              <th scope="col">Ref and channel</th>
              <th scope="col">Received</th>
              <th scope="col">Subject and customer</th>
              <th scope="col">Priority</th>
              <th scope="col">Category and team</th>
              <th scope="col">Status</th>
              <th scope="col">SLA due</th>
            </tr>
          </thead>
          <tbody>{body}</tbody>
        </table>
      </div>
      {hidden > 0 || (expanded && list.length > INITIAL_ROWS) ? (
        <div className="queue-more">
          <button type="button" className="button button-quiet" onClick={() => setExpanded((v) => !v)} aria-expanded={expanded}>
            {expanded ? `Show the newest ${INITIAL_ROWS} only` : `Show ${hidden} more ${hidden === 1 ? 'ticket' : 'tickets'}`}
          </button>
        </div>
      ) : null}
    </section>
  );
}
