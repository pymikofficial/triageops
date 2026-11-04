import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useLocation, useNavigate, useParams } from 'react-router-dom';
import { api } from '../lib/api.js';
import { describeEvent, eventLabel, EVENT_TONE } from '../lib/events.js';
import { formatAbsolute, formatAgo, formatClock, formatInteger, formatOffset, formatPercent, formatSeconds, slaState, toDate, toFraction } from '../lib/format.js';
import { useDocumentTitle, useNow, usePolling } from '../lib/hooks.js';
import { PRIORITY_META } from '../lib/taxonomy.js';
import { ChannelTag, PriorityBadge, SentimentText, StatusBadge } from './Badges.jsx';
import CopyButton from './CopyButton.jsx';
import { AlertIcon, CloseIcon, EyeIcon, ShieldIcon } from './Icons.jsx';

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

// Masked values look like [REDACTED:email] or pri***@nort***. The split keeps them as separate parts.
const MASK_PATTERN = /(\[REDACTED(?::[A-Za-z0-9_ -]{1,30})?\]|\[[A-Z][A-Z0-9 _-]{1,30}\]|\S*\*{3,}\S*)/g;

function MaskedBody({ text }) {
  const parts = String(text || '').split(MASK_PATTERN);
  return (
    <pre className="ticket-body">
      {parts.map((part, i) =>
        i % 2 === 1 ? (
          <mark key={i} className="mask" title="Masked before it reached Claude or the database">
            {part}
          </mark>
        ) : (
          part
        ),
      )}
    </pre>
  );
}

function normaliseSteps(steps) {
  if (!Array.isArray(steps)) return [];
  return steps
    .map((s) => (typeof s === 'string' ? s : s && (s.step || s.text || s.title || s.action)) || '')
    .map((s) => String(s).trim())
    .filter(Boolean);
}

function NextSteps({ steps }) {
  const [done, setDone] = useState(() => new Set());
  if (steps.length === 0) return <p className="muted">No next steps were suggested.</p>;
  const toggle = (i) =>
    setDone((prev) => {
      const next = new Set(prev);
      if (next.has(i)) next.delete(i);
      else next.add(i);
      return next;
    });
  return (
    <>
      <ol className="checklist">
        {steps.map((step, i) => (
          <li key={i} className={done.has(i) ? 'is-done' : undefined}>
            <label>
              <input type="checkbox" checked={done.has(i)} onChange={() => toggle(i)} />
              <span>{step}</span>
            </label>
          </li>
        ))}
      </ol>
      <p className="checklist-progress" aria-live="polite">
        {done.size} of {steps.length} done. Ticks are kept in this browser tab only.
      </p>
    </>
  );
}

function Flags({ ticket, events }) {
  const humanReview =
    ticket.injection_suspected ||
    events.some((e) => e.type === 'human_review') ||
    (ticket.escalation_reason && /review/i.test(ticket.escalation_reason));
  const flags = [];
  if (ticket.injection_suspected) {
    flags.push({
      key: 'injection',
      tone: 'danger',
      icon: <ShieldIcon size={16} />,
      title: 'Possible prompt injection',
      text: 'The ticket contains instructions aimed at the AI. Claude treated them as ticket text and did not follow them.',
    });
  }
  if (humanReview) {
    flags.push({
      key: 'review',
      tone: 'warn',
      icon: <EyeIcon size={16} />,
      title: 'Human review',
      text: ticket.escalation_reason || 'An engineer should check this ticket before any reply is sent.',
    });
  } else if (ticket.status === 'escalated' && ticket.escalation_reason) {
    flags.push({
      key: 'escalated',
      tone: 'danger',
      icon: <AlertIcon size={16} />,
      title: 'Escalated',
      text: ticket.escalation_reason,
    });
  }
  if (ticket.pii_masked) {
    flags.push({
      key: 'pii',
      tone: 'neutral',
      icon: <ShieldIcon size={16} />,
      title: 'PII masked',
      text: 'Personal data such as email addresses and phone numbers was masked before triage and storage.',
    });
  }
  if (flags.length === 0) {
    return <p className="flags-none">No flags raised. No personal data was found to mask.</p>;
  }
  return (
    <ul className="flag-list">
      {flags.map((f) => (
        <li key={f.key} className={`flag flag-${f.tone}`}>
          <span className="flag-icon">{f.icon}</span>
          <span>
            <strong>{f.title}</strong>
            <span className="flag-text">{f.text}</span>
          </span>
        </li>
      ))}
    </ul>
  );
}

function Timeline({ events, ticket }) {
  if (events.length === 0) return <p className="muted">No events were recorded for this ticket.</p>;
  const sorted = [...events].sort((a, b) => (toDate(a.created_at)?.getTime() || 0) - (toDate(b.created_at)?.getTime() || 0));
  const start = toDate(sorted[0].created_at)?.getTime() || 0;
  return (
    <ol className="timeline">
      {sorted.map((event, i) => {
        const info = describeEvent(event);
        const at = toDate(event.created_at)?.getTime() || start;
        return (
          <li key={event.id ?? i} className={`timeline-item tone-${EVENT_TONE[event.type] || 'neutral'}`}>
            <span className="timeline-node" aria-hidden="true" />
            <div className="timeline-main">
              <div className="timeline-row">
                <span className="timeline-label">{eventLabel(event, ticket)}</span>
                <span className="timeline-time">
                  <time dateTime={event.created_at} title={formatAbsolute(event.created_at)}>
                    {formatClock(event.created_at)}
                  </time>
                  <span className="timeline-offset">{i === 0 ? 'start' : formatOffset(at - start)}</span>
                </span>
              </div>
              {info.text ? <p className="timeline-text">{info.text}</p> : null}
              {info.items.length > 0 ? (
                <ul className="timeline-items">
                  {info.items.map((item, j) => (
                    <li key={j}>
                      {item.title}
                      {item.meta ? <span className="timeline-item-meta">{item.meta}</span> : null}
                    </li>
                  ))}
                </ul>
              ) : null}
              {info.facts.length > 0 ? (
                <dl className="timeline-facts">
                  {info.facts.map(([k, v], j) => (
                    <div key={j}>
                      <dt>{k}</dt>
                      <dd>{v}</dd>
                    </div>
                  ))}
                </dl>
              ) : null}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

function KbRefs({ refs }) {
  const list = Array.isArray(refs) ? refs : [];
  if (list.length === 0) return <p className="muted">No knowledge base articles were close enough to use.</p>;
  return (
    <ul className="kb-list">
      {list.map((kb, i) => {
        const frac = toFraction(kb.similarity);
        return (
          <li key={kb.slug || i} className="kb-item">
            <div className="kb-text">
              <span className="kb-title">{kb.title || kb.slug}</span>
              {kb.slug ? <span className="kb-slug">{kb.slug}</span> : null}
            </div>
            <div className="kb-score" title="Cosine similarity between the ticket and the article">
              <span className="kb-meter" aria-hidden="true">
                <span style={{ width: `${Math.round(frac * 100)}%` }} />
              </span>
              <span className="kb-pct">{formatPercent(kb.similarity)}</span>
              <span className="visually-hidden"> similarity</span>
            </div>
          </li>
        );
      })}
    </ul>
  );
}

function DrawerSkeleton() {
  return (
    <div className="drawer-body" aria-hidden="true">
      {[70, 40, 90, 60, 80].map((w, i) => (
        <span key={i} className="skeleton-line" style={{ width: `${w}%` }} />
      ))}
    </div>
  );
}

export default function TicketDrawer() {
  const { ref } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const now = useNow(15000);
  const [state, setState] = useState({ phase: 'loading', data: null, error: null, status: 0 });
  const panelRef = useRef(null);
  const returnFocus = useRef(null);

  useDocumentTitle(`${ref} | TriageOps`);

  const close = useCallback(() => {
    navigate({ pathname: '/', search: location.search });
  }, [navigate, location.search]);

  const load = useCallback(
    async (silent = false) => {
      if (!silent) setState((s) => ({ ...s, phase: s.data && s.data.ticket && s.data.ticket.ref === ref ? 'ready' : 'loading', error: null }));
      try {
        const data = await api.ticket(ref);
        setState({ phase: 'ready', data, error: null, status: 200 });
      } catch (err) {
        if (silent) return;
        setState({ phase: 'error', data: null, error: err.message, status: err.status || 0 });
      }
    },
    [ref],
  );

  useEffect(() => {
    load();
  }, [load]);
  usePolling(() => load(true), 15000, state.phase === 'ready');

  // Focus management: remember what had focus, move focus into the dialog, restore on close.
  useEffect(() => {
    returnFocus.current = document.activeElement;
    document.body.classList.add('has-drawer');
    // Focus the dialog itself so screen readers announce it and Tab reaches the close button first.
    const id = requestAnimationFrame(() => panelRef.current && panelRef.current.focus({ preventScroll: true }));
    return () => {
      cancelAnimationFrame(id);
      document.body.classList.remove('has-drawer');
      const previous = returnFocus.current;
      const target =
        previous && previous !== document.body && document.contains(previous)
          ? previous
          : document.querySelector(`[data-ref-link="${CSS.escape(ref)}"]`);
      if (target && typeof target.focus === 'function') target.focus({ preventScroll: true });
    };
  }, [ref]);

  const onKeyDown = (event) => {
    if (event.key === 'Escape') {
      event.stopPropagation();
      close();
      return;
    }
    if (event.key !== 'Tab' || !panelRef.current) return;
    const nodes = Array.from(panelRef.current.querySelectorAll(FOCUSABLE)).filter((n) => n.offsetParent !== null);
    if (nodes.length === 0) return;
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  const ticket = state.data && state.data.ticket;
  const events = useMemo(() => (state.data && Array.isArray(state.data.events) ? state.data.events : []), [state.data]);
  const steps = useMemo(() => normaliseSteps(ticket && ticket.next_steps), [ticket]);

  let content;
  if (state.phase === 'loading') {
    content = <DrawerSkeleton />;
  } else if (state.phase === 'error') {
    content = (
      <div className="drawer-body drawer-state">
        <p className="state-title">{state.status === 404 ? `Ticket ${ref} was not found` : 'This ticket could not be loaded'}</p>
        <p className="state-text">
          {state.status === 404 ? 'The reference may be mistyped, or the link may be from another environment.' : `${state.error}.`}
        </p>
        <div className="button-row">
          {state.status !== 404 ? (
            <button type="button" className="button" onClick={() => load()}>
              Try again
            </button>
          ) : null}
          <button type="button" className="button button-quiet" onClick={close}>
            Back to the queue
          </button>
        </div>
      </div>
    );
  } else if (ticket) {
    const sla = slaState(ticket, now);
    const confidence = ticket.confidence !== null && ticket.confidence !== undefined ? toFraction(ticket.confidence) : null;
    const priorityMeta = PRIORITY_META[ticket.priority];
    const fallback = ticket.ai_model === 'rules-fallback';
    content = (
      <div className="drawer-body">
        <section className="drawer-section" aria-label="Flags">
          <Flags ticket={ticket} events={events} />
        </section>

        <section className="drawer-section" aria-labelledby="d-triage">
          <h3 className="section-title" id="d-triage">
            Triage
          </h3>
          <dl className="fact-grid">
            <div className="fact">
              <dt>Priority</dt>
              <dd>
                <PriorityBadge priority={ticket.priority} showLabel size="lg" />
                {priorityMeta ? <span className="fact-note">SLA {priorityMeta.sla}</span> : null}
              </dd>
            </div>
            <div className="fact">
              <dt>Status</dt>
              <dd>
                <StatusBadge status={ticket.status} />
              </dd>
            </div>
            <div className="fact">
              <dt>SLA due</dt>
              <dd>
                <span className={`sla sla-${sla.tone}`}>{sla.text}</span>
                {ticket.sla_due_at ? <span className="fact-note">{formatAbsolute(ticket.sla_due_at)}</span> : null}
              </dd>
            </div>
            <div className="fact">
              <dt>Category</dt>
              <dd>{ticket.category || 'Awaiting triage'}</dd>
            </div>
            <div className="fact">
              <dt>Team</dt>
              <dd>{ticket.team || 'Unassigned'}</dd>
            </div>
            <div className="fact">
              <dt>Sentiment</dt>
              <dd>
                <SentimentText sentiment={ticket.sentiment} />
              </dd>
            </div>
            <div className="fact">
              <dt>Confidence</dt>
              <dd>
                {confidence === null ? (
                  'Not recorded'
                ) : (
                  <span className="confidence">
                    <span className="confidence-value">{Math.round(confidence * 100)}%</span>
                    <span className="confidence-meter" aria-hidden="true">
                      <span style={{ width: `${Math.round(confidence * 100)}%` }} />
                    </span>
                  </span>
                )}
              </dd>
            </div>
          </dl>
        </section>

        <section className="drawer-section" aria-labelledby="d-summary">
          <h3 className="section-title" id="d-summary">
            Summary
          </h3>
          <p className="summary-text">{ticket.summary || 'No summary was produced for this ticket.'}</p>
        </section>

        <section className="drawer-section" aria-labelledby="d-reply">
          <div className="section-head">
            <h3 className="section-title" id="d-reply">
              Suggested customer reply
            </h3>
            {ticket.suggested_reply ? <CopyButton text={ticket.suggested_reply} label="Copy reply" /> : null}
          </div>
          {ticket.injection_suspected ? (
            <p className="reply-hold">Held for human review. Check the reply before it is sent.</p>
          ) : null}
          {ticket.suggested_reply ? (
            <div className="reply-box" tabIndex={0} aria-label="Suggested customer reply">
              {ticket.suggested_reply}
            </div>
          ) : (
            <p className="muted">No reply was drafted.</p>
          )}
        </section>

        <section className="drawer-section" aria-labelledby="d-steps">
          <h3 className="section-title" id="d-steps">
            Engineer next steps
          </h3>
          <NextSteps key={ticket.ref} steps={steps} />
        </section>

        <section className="drawer-section" aria-labelledby="d-kb">
          <h3 className="section-title" id="d-kb">
            Knowledge base articles used
          </h3>
          <KbRefs refs={ticket.kb_refs} />
        </section>

        <section className="drawer-section" aria-labelledby="d-body">
          <div className="section-head">
            <h3 className="section-title" id="d-body">
              Original ticket
            </h3>
            <span className="section-note">{ticket.pii_masked ? 'Shown after PII masking' : 'As received'}</span>
          </div>
          <MaskedBody text={ticket.body} />
        </section>

        <section className="drawer-section" aria-labelledby="d-timeline">
          <h3 className="section-title" id="d-timeline">
            Event timeline
          </h3>
          <Timeline events={events} ticket={ticket} />
        </section>

        <section className="drawer-section" aria-labelledby="d-ai">
          <h3 className="section-title" id="d-ai">
            AI metadata
          </h3>
          <dl className="meta-grid">
            <div>
              <dt>Model</dt>
              <dd className="mono">{ticket.ai_model || 'Not recorded'}</dd>
            </div>
            <div>
              <dt>Latency</dt>
              <dd>{ticket.ai_latency_ms !== null && ticket.ai_latency_ms !== undefined ? formatSeconds(ticket.ai_latency_ms, 2) : 'Not recorded'}</dd>
            </div>
            <div>
              <dt>Input tokens</dt>
              <dd>{ticket.input_tokens !== null && ticket.input_tokens !== undefined ? formatInteger(ticket.input_tokens) : 'Not recorded'}</dd>
            </div>
            <div>
              <dt>Output tokens</dt>
              <dd>{ticket.output_tokens !== null && ticket.output_tokens !== undefined ? formatInteger(ticket.output_tokens) : 'Not recorded'}</dd>
            </div>
          </dl>
          {fallback ? (
            <p className="section-foot">Claude was unavailable for this ticket, so keyword rules triaged it. The reason is in the timeline.</p>
          ) : null}
        </section>
      </div>
    );
  }

  return (
    <div className="drawer-layer" onKeyDown={onKeyDown}>
      <div className="drawer-backdrop" onClick={close} aria-hidden="true" />
      <aside
        className="drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="drawer-title"
        ref={panelRef}
        tabIndex={-1}
      >
        <header className="drawer-head">
          <div className="drawer-topline">
            <span className="ref ref-plain">{ref}</span>
            {ticket ? <ChannelTag channel={ticket.channel} /> : null}
            {ticket ? (
              <time className="drawer-received" dateTime={ticket.created_at} title={formatAbsolute(ticket.created_at)}>
                Received {formatAgo(ticket.created_at, now)}
              </time>
            ) : null}
            <button type="button" className="icon-button drawer-close" onClick={close} aria-label="Close ticket">
              <CloseIcon size={18} />
            </button>
          </div>
          <h2 className="drawer-title" id="drawer-title">
            {ticket ? ticket.subject : state.phase === 'error' ? 'Ticket unavailable' : 'Loading ticket'}
          </h2>
          {ticket ? (
            <p className="drawer-sub">
              <span>{ticket.customer || 'Unknown customer'}</span>
              {ticket.requester ? <span className="drawer-requester">{ticket.requester}</span> : null}
              {ticket.is_demo ? <span className="inline-flag flag-neutral">Raised from the public demo</span> : null}
            </p>
          ) : null}
        </header>
        {content}
      </aside>
    </div>
  );
}
