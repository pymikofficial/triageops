import { useEffect, useId, useRef, useState } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { api } from '../lib/api.js';
import { SAMPLE_TICKETS } from '../lib/samples.js';
import { formatAbsolute, slaState } from '../lib/format.js';
import { PriorityBadge, StatusBadge } from './Badges.jsx';
import { CheckIcon } from './Icons.jsx';

const STEPS = [
  { key: 'received', label: 'Received', startsAt: 0 },
  { key: 'masking', label: 'Masking PII', startsAt: 350 },
  { key: 'kb', label: 'Searching knowledge base', startsAt: 950 },
  { key: 'claude', label: 'Claude triage', startsAt: 1800 },
  { key: 'rules', label: 'Applying rules', startsAt: null },
];

const EMPTY_FORM = { channel: 'portal', customer: '', requester: '', subject: '', body: '', website: '' };

const CHANNEL_OPTIONS = [
  { value: 'portal', label: 'Portal' },
  { value: 'email', label: 'Email' },
  { value: 'whatsapp', label: 'WhatsApp' },
];

function validate(form) {
  const errors = {};
  const subject = form.subject.trim();
  const body = form.body.trim();
  if (subject.length < 3) errors.subject = 'Enter a subject of at least 3 characters.';
  else if (subject.length > 200) errors.subject = 'Keep the subject to 200 characters or fewer.';
  if (body.length < 10) errors.body = 'Describe the issue in at least 10 characters.';
  else if (body.length > 4000) errors.body = 'Keep the description to 4,000 characters or fewer.';
  return errors;
}

function errorMessage(err) {
  if (err.status === 429) return 'Demo limit reached, please try again in an hour.';
  if (err.status === 400 || err.status === 422) return `The ticket was not accepted. ${err.message}.`;
  if (err.status >= 500) return 'The service could not triage this ticket. Please try again in a moment.';
  return `${err.message}. Check your connection and try again.`;
}

function Progress({ phase, active, elapsed, fallback }) {
  return (
    <ol className="triage-steps" aria-hidden="true">
      {STEPS.map((step, i) => {
        const done = phase === 'done' || i < active;
        const current = phase !== 'done' && i === active;
        return (
          <li key={step.key} className={`triage-step${done ? ' is-done' : ''}${current ? ' is-current' : ''}`}>
            <span className="triage-step-node">{done ? <CheckIcon size={12} strokeWidth={2.5} /> : null}</span>
            <span className="triage-step-label">
              {step.label}
              {step.key === 'claude' && fallback ? <span className="triage-step-note">Keyword rules used instead</span> : null}
            </span>
            {current ? <span className="triage-step-time">{(elapsed / 1000).toFixed(1)} s</span> : null}
          </li>
        );
      })}
    </ol>
  );
}

function ResultCard({ result, onReset, search, headingRef }) {
  const t = result.ticket;
  const seconds = ((result.timing_ms ?? result.elapsed) / 1000).toFixed(1);
  const sla = slaState(t);
  return (
    <div className="result-card">
      <p className="result-time" ref={headingRef} tabIndex={-1}>
        Triaged in {seconds} s
      </p>
      <div className="result-line">
        <span className="ref ref-plain">{t.ref}</span>
        <PriorityBadge priority={t.priority} showLabel />
        <StatusBadge status={t.status} />
      </div>
      <p className="result-subject">{t.subject}</p>
      <dl className="result-facts">
        <div>
          <dt>Category</dt>
          <dd>{t.category || 'Not set'}</dd>
        </div>
        <div>
          <dt>Team</dt>
          <dd>{t.team || 'Unassigned'}</dd>
        </div>
        <div>
          <dt>SLA due</dt>
          <dd title={t.sla_due_at ? formatAbsolute(t.sla_due_at) : undefined}>{sla.text}</dd>
        </div>
      </dl>
      {t.summary ? <p className="result-summary">{t.summary}</p> : null}
      {t.injection_suspected ? (
        <p className="result-flag flag-danger">Possible prompt injection detected. The ticket is held for human review.</p>
      ) : null}
      {t.ai_model === 'rules-fallback' ? (
        <p className="result-flag flag-warn">Claude was unavailable, so keyword rules triaged this ticket.</p>
      ) : null}
      <div className="button-row">
        <Link className="button button-primary" to={{ pathname: `/ticket/${encodeURIComponent(t.ref)}`, search }}>
          Open ticket {t.ref}
        </Link>
        <button type="button" className="button button-quiet" onClick={onReset}>
          Raise another
        </button>
      </div>
    </div>
  );
}

export default function RaiseTicket({ onCreated }) {
  const uid = useId();
  const location = useLocation();
  const [form, setForm] = useState(EMPTY_FORM);
  const [errors, setErrors] = useState({});
  const [phase, setPhase] = useState('idle'); // idle, running, done, error
  const [active, setActive] = useState(0);
  const [elapsed, setElapsed] = useState(0);
  const [result, setResult] = useState(null);
  const [submitError, setSubmitError] = useState('');
  const [sampleId, setSampleId] = useState(null);
  const timers = useRef([]);
  const resultHeading = useRef(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      timers.current.forEach(clearTimeout);
      timers.current.forEach(clearInterval);
    };
  }, []);

  useEffect(() => {
    if (phase === 'done' && resultHeading.current) resultHeading.current.focus();
  }, [phase]);

  const update = (field) => (event) => {
    const value = event.target.value;
    setForm((f) => ({ ...f, [field]: value }));
    if (errors[field]) setErrors((e) => ({ ...e, [field]: undefined }));
    if (field !== 'website') setSampleId(null);
  };

  const fillSample = (sample) => {
    setForm({ ...EMPTY_FORM, channel: sample.channel, customer: sample.customer, requester: sample.requester, subject: sample.subject, body: sample.body });
    setErrors({});
    setSubmitError('');
    setSampleId(sample.id);
    if (phase === 'done' || phase === 'error') setPhase('idle');
  };

  const clearTimers = () => {
    timers.current.forEach((t) => {
      clearTimeout(t);
      clearInterval(t);
    });
    timers.current = [];
  };

  const reset = () => {
    clearTimers();
    setForm(EMPTY_FORM);
    setErrors({});
    setSubmitError('');
    setResult(null);
    setSampleId(null);
    setPhase('idle');
    setActive(0);
    setElapsed(0);
  };

  const onSubmit = async (event) => {
    event.preventDefault();
    if (phase === 'running') return;
    const found = validate(form);
    setErrors(found);
    if (Object.keys(found).length > 0) {
      const firstField = document.getElementById(`${uid}-${Object.keys(found)[0]}`);
      if (firstField) firstField.focus();
      return;
    }

    clearTimers();
    setSubmitError('');
    setResult(null);
    setPhase('running');
    setActive(0);
    setElapsed(0);

    const started = performance.now();
    STEPS.forEach((step, i) => {
      if (i > 0 && step.startsAt !== null) {
        timers.current.push(setTimeout(() => mounted.current && setActive((a) => Math.max(a, i)), step.startsAt));
      }
    });
    timers.current.push(setInterval(() => mounted.current && setElapsed(performance.now() - started), 100));

    const payload = {
      channel: form.channel,
      customer: form.customer.trim() || undefined,
      requester: form.requester.trim() || undefined,
      subject: form.subject.trim(),
      body: form.body.trim(),
      website: form.website,
    };

    try {
      const data = await api.createDemoTicket(payload);
      if (!mounted.current) return;
      const total = performance.now() - started;
      clearTimers();
      setElapsed(total);
      setActive(STEPS.length - 1);
      // Hold the final step briefly so the rules stage is visible, then show the result.
      timers.current.push(
        setTimeout(() => {
          if (!mounted.current) return;
          setResult({ ...data, elapsed: total });
          setPhase('done');
          if (onCreated) onCreated(data.ticket);
        }, 450),
      );
    } catch (err) {
      if (!mounted.current) return;
      clearTimers();
      setSubmitError(errorMessage(err));
      setPhase('error');
    }
  };

  const running = phase === 'running';
  const fallback = result && result.ticket && result.ticket.ai_model === 'rules-fallback';
  const liveText =
    phase === 'running'
      ? `Step ${active + 1} of ${STEPS.length}, ${STEPS[active].label}`
      : phase === 'done' && result
        ? `Triaged in ${(((result.timing_ms ?? result.elapsed) || 0) / 1000).toFixed(1)} seconds. ${result.ticket.ref} is ${result.ticket.priority || 'awaiting priority'}.`
        : '';

  return (
    <section className="panel raise-panel" aria-labelledby={`${uid}-title`}>
      <div className="panel-head">
        <h2 className="panel-title" id={`${uid}-title`}>
          Raise a test ticket
        </h2>
        <p className="panel-sub">It goes through the same pipeline as email and WhatsApp tickets, then appears in the queue.</p>
      </div>

      <div className="samples" role="group" aria-labelledby={`${uid}-samples`}>
        <p className="field-label" id={`${uid}-samples`}>
          Start from a sample
        </p>
        <div className="sample-list">
          {SAMPLE_TICKETS.map((s) => (
            <button
              key={s.id}
              type="button"
              className={`sample-button${sampleId === s.id ? ' is-selected' : ''}${s.id === 'injection' ? ' sample-injection' : ''}`}
              onClick={() => fillSample(s)}
              disabled={running}
              aria-pressed={sampleId === s.id}
            >
              {s.label}
            </button>
          ))}
        </div>
      </div>

      <form className="raise-form" onSubmit={onSubmit} noValidate>
        <div className="form-grid">
          <div className="field">
            <label className="field-label" htmlFor={`${uid}-channel`}>
              Channel
            </label>
            <select id={`${uid}-channel`} className="select" value={form.channel} onChange={update('channel')} disabled={running}>
              {CHANNEL_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label className="field-label" htmlFor={`${uid}-customer`}>
              Customer <span className="optional">optional</span>
            </label>
            <input
              id={`${uid}-customer`}
              className="input"
              value={form.customer}
              onChange={update('customer')}
              maxLength={120}
              placeholder="Harbour Dental Group"
              disabled={running}
              autoComplete="organization"
            />
          </div>
          <div className="field field-full">
            <label className="field-label" htmlFor={`${uid}-requester`}>
              Requester <span className="optional">optional, masked on arrival</span>
            </label>
            <input
              id={`${uid}-requester`}
              className="input"
              value={form.requester}
              onChange={update('requester')}
              maxLength={120}
              placeholder="Name, email or phone number"
              disabled={running}
              autoComplete="off"
            />
          </div>
          <div className="field field-full">
            <label className="field-label" htmlFor={`${uid}-subject`}>
              Subject
            </label>
            <input
              id={`${uid}-subject`}
              className="input"
              value={form.subject}
              onChange={update('subject')}
              maxLength={200}
              required
              aria-invalid={errors.subject ? 'true' : undefined}
              aria-describedby={errors.subject ? `${uid}-subject-error` : undefined}
              disabled={running}
              autoComplete="off"
            />
            {errors.subject ? (
              <p className="field-error" id={`${uid}-subject-error`}>
                {errors.subject}
              </p>
            ) : null}
          </div>
          <div className="field field-full">
            <label className="field-label" htmlFor={`${uid}-body`}>
              Description
            </label>
            <textarea
              id={`${uid}-body`}
              className="textarea"
              rows={5}
              value={form.body}
              onChange={update('body')}
              maxLength={4000}
              required
              aria-invalid={errors.body ? 'true' : undefined}
              aria-describedby={`${uid}-body-hint${errors.body ? ` ${uid}-body-error` : ''}`}
              disabled={running}
            />
            <p className="field-hint" id={`${uid}-body-hint`}>
              {form.body.length.toLocaleString('en-GB')} of 4,000 characters. Please do not enter real personal data.
            </p>
            {errors.body ? (
              <p className="field-error" id={`${uid}-body-error`}>
                {errors.body}
              </p>
            ) : null}
          </div>
          <div className="honeypot" aria-hidden="true">
            <label htmlFor={`${uid}-website`}>Website</label>
            <input id={`${uid}-website`} name="website" type="text" tabIndex={-1} autoComplete="off" value={form.website} onChange={update('website')} />
          </div>
        </div>

        <div className="form-actions">
          <button type="submit" className="button button-primary" disabled={running}>
            {running ? 'Triaging' : 'Raise ticket'}
          </button>
          {phase === 'idle' || phase === 'error' ? (
            <span className="form-actions-note">Triage usually takes a few seconds.</span>
          ) : null}
        </div>
      </form>

      <p className="visually-hidden" aria-live="polite" role="status">
        {liveText}
      </p>

      {submitError ? (
        <p className="form-error" role="alert">
          {submitError}
        </p>
      ) : null}

      {running || phase === 'done' ? (
        <div className="triage-progress">
          {running ? <Progress phase={phase} active={active} elapsed={elapsed} fallback={false} /> : null}
          {phase === 'done' && result ? (
            <>
              <Progress phase="done" active={STEPS.length} elapsed={elapsed} fallback={fallback} />
              <ResultCard result={result} onReset={reset} search={location.search} headingRef={resultHeading} />
            </>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
