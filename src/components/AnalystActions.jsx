import { useId, useState } from 'react';
import { adminApi, adminErrorMessage } from '../lib/admin.js';
import { optimisticOverride, optimisticReopen, optimisticReply, optimisticResolve } from '../lib/adminOptimistic.js';
import { formatAbsolute, formatClock } from '../lib/format.js';
import { PRIORITIES, PRIORITY_META, TEAMS } from '../lib/taxonomy.js';
import '../styles/analyst.css';

const LIMITS = { reply: 6000, reason: 500, note: 2000 };

const SUCCESS = {
  reply: 'Reply approved and added to the timeline. Nothing was sent to the customer.',
  override: 'Override saved. It is recorded as a gold label for evaluation.',
  resolve: 'Ticket resolved. The SLA clock has stopped.',
  reopen: 'Ticket reopened. The SLA sweep watches it again.',
};

function lastApproval(events) {
  return [...events].reverse().find((e) => e.type === 'reply_approved' && !e.pending);
}

export default function AnalystActions({ ticket, events, onAction }) {
  const id = useId();
  const resolved = ticket.status === 'resolved';
  const [mode, setMode] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [fieldError, setFieldError] = useState('');
  const [replyText, setReplyText] = useState(ticket.suggested_reply || '');
  const [priority, setPriority] = useState(ticket.priority || '');
  const [team, setTeam] = useState(ticket.team || '');
  const [reason, setReason] = useState('');
  const [note, setNote] = useState('');

  const edit = (setter, value) => {
    setter(value);
    setFieldError('');
  };

  const open = (next) => {
    setError('');
    setFieldError('');
    setNotice('');
    if (next === 'override') {
      setPriority(ticket.priority || '');
      setTeam(ticket.team || '');
    }
    setMode((current) => (current === next ? null : next));
  };

  const validate = () => {
    if (mode === 'reply') {
      if (!replyText.trim()) return 'Write the reply before approving it.';
      if (replyText.trim().length > LIMITS.reply) return 'Keep the reply to 6,000 characters or fewer.';
    }
    if (mode === 'override') {
      if (priority === (ticket.priority || '') && team === (ticket.team || '')) return 'Choose a priority or team that differs from the current one.';
      if (reason.trim().length < 3) return 'Give a reason of at least 3 characters. Overrides are used as gold labels.';
      if (reason.trim().length > LIMITS.reason) return 'Keep the reason to 500 characters or fewer.';
    }
    if (mode === 'resolve') {
      if (note.trim().length < 3) return 'Add a resolution note of at least 3 characters.';
      if (note.trim().length > LIMITS.note) return 'Keep the note to 2,000 characters or fewer.';
    }
    if (mode === 'reopen' && note.trim().length > LIMITS.note) return 'Keep the note to 2,000 characters or fewer.';
    return '';
  };

  const submit = async (event) => {
    event.preventDefault();
    const problem = validate();
    setFieldError(problem);
    if (problem) return;

    const action = mode;
    const ref = ticket.ref;
    let optimistic;
    let request;
    if (action === 'reply') {
      const text = replyText.trim();
      optimistic = (data) => optimisticReply(data, text);
      request = () => adminApi.approveReply(ref, text);
    } else if (action === 'override') {
      const changes = { reason: reason.trim() };
      if (priority && priority !== ticket.priority) changes.priority = priority;
      if (team && team !== ticket.team) changes.team = team;
      optimistic = (data) => optimisticOverride(data, changes);
      request = () => adminApi.override(ref, changes);
    } else if (action === 'resolve') {
      const text = note.trim();
      optimistic = (data) => optimisticResolve(data, text);
      request = () => adminApi.resolve(ref, text);
    } else {
      const text = note.trim();
      optimistic = (data) => optimisticReopen(data, text);
      request = () => adminApi.reopen(ref, text);
    }

    // The drawer shows the change at once. The form closes and returns only if the server refuses it.
    setBusy(true);
    setError('');
    setMode(null);
    setNotice('Saving');
    try {
      await onAction(optimistic, request);
      setNotice(SUCCESS[action]);
      setReason('');
      setNote('');
    } catch (err) {
      setNotice('');
      setError(`${adminErrorMessage(err)} The change was undone.`);
      if (err.status !== 401) setMode(action);
    } finally {
      setBusy(false);
    }
  };

  const approval = lastApproval(events);
  const formId = `${id}-form`;
  const errorId = `${id}-error`;
  const invalid = fieldError ? 'true' : undefined;

  let form = null;
  if (mode === 'reply') {
    form = (
      <div className="field">
        <label className="field-label" htmlFor={`${id}-reply`}>
          Reply to approve
        </label>
        <textarea
          id={`${id}-reply`}
          className="textarea analyst-reply"
          rows={8}
          value={replyText}
          maxLength={LIMITS.reply}
          onChange={(e) => edit(setReplyText, e.target.value)}
          aria-invalid={invalid}
          aria-describedby={`${id}-reply-hint${fieldError ? ` ${errorId}` : ''}`}
        />
        <span className="field-hint" id={`${id}-reply-hint`}>
          Edit the draft if needed. Approving records the reply on the timeline. Nothing is sent to the customer.
        </span>
      </div>
    );
  } else if (mode === 'override') {
    form = (
      <>
        <div className="analyst-grid">
          <div className="field">
            <label className="field-label" htmlFor={`${id}-priority`}>
              Priority
            </label>
            <select id={`${id}-priority`} className="select" value={priority} onChange={(e) => edit(setPriority, e.target.value)}>
              {!ticket.priority ? <option value="">Not set</option> : null}
              {PRIORITIES.map((p) => (
                <option key={p} value={p}>
                  {p} {PRIORITY_META[p].label}, SLA {PRIORITY_META[p].sla}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label className="field-label" htmlFor={`${id}-team`}>
              Team
            </label>
            <select id={`${id}-team`} className="select" value={team} onChange={(e) => edit(setTeam, e.target.value)}>
              {!ticket.team ? <option value="">Unassigned</option> : null}
              {TEAMS.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </div>
        </div>
        <div className="field">
          <label className="field-label" htmlFor={`${id}-reason`}>
            Reason
          </label>
          <textarea
            id={`${id}-reason`}
            className="textarea"
            rows={3}
            value={reason}
            maxLength={LIMITS.reason}
            required
            onChange={(e) => edit(setReason, e.target.value)}
            aria-invalid={invalid}
            aria-describedby={`${id}-reason-hint${fieldError ? ` ${errorId}` : ''}`}
          />
          <span className="field-hint" id={`${id}-reason-hint`}>
            Required. A priority change moves the SLA deadline to match the new priority.
          </span>
        </div>
      </>
    );
  } else if (mode === 'resolve' || mode === 'reopen') {
    form = (
      <div className="field">
        <label className="field-label" htmlFor={`${id}-note`}>
          {mode === 'resolve' ? 'Resolution note' : 'Reason for reopening'}
          {mode === 'reopen' ? <span className="optional"> (optional)</span> : null}
        </label>
        <textarea
          id={`${id}-note`}
          className="textarea"
          rows={3}
          value={note}
          maxLength={LIMITS.note}
          required={mode === 'resolve'}
          onChange={(e) => edit(setNote, e.target.value)}
          aria-invalid={invalid}
          aria-describedby={fieldError ? errorId : undefined}
        />
      </div>
    );
  }

  const submitLabel = { reply: 'Approve reply', override: 'Save override', resolve: 'Resolve ticket', reopen: 'Reopen ticket' }[mode];

  return (
    <section className="drawer-section analyst-panel" aria-labelledby={`${id}-title`}>
      <div className="section-head">
        <h3 className="section-title" id={`${id}-title`}>
          Analyst actions
        </h3>
        <span className="section-note">Signed in as an analyst</span>
      </div>

      {resolved ? <p className="analyst-state">This ticket is resolved. The SLA clock has stopped.</p> : null}
      {approval ? (
        <p className="analyst-state">
          Reply approved at{' '}
          <time dateTime={approval.created_at} title={formatAbsolute(approval.created_at)}>
            {formatClock(approval.created_at)}
          </time>
          {approval.detail && approval.detail.edited ? ', with edits' : ''}.
        </p>
      ) : null}

      <div className="button-row analyst-buttons">
        {resolved ? (
          <button type="button" className="button button-small" aria-expanded={mode === 'reopen'} aria-controls={formId} disabled={busy} onClick={() => open('reopen')}>
            Reopen
          </button>
        ) : (
          <>
            <button type="button" className="button button-small" aria-expanded={mode === 'reply'} aria-controls={formId} disabled={busy} onClick={() => open('reply')}>
              Approve reply
            </button>
            <button type="button" className="button button-small" aria-expanded={mode === 'override'} aria-controls={formId} disabled={busy} onClick={() => open('override')}>
              Change priority or team
            </button>
            <button type="button" className="button button-small" aria-expanded={mode === 'resolve'} aria-controls={formId} disabled={busy} onClick={() => open('resolve')}>
              Resolve
            </button>
          </>
        )}
      </div>

      {form ? (
        <form id={formId} className="analyst-form" onSubmit={submit} noValidate>
          {form}
          {fieldError ? (
            <p className="field-error" id={errorId} role="alert">
              {fieldError}
            </p>
          ) : null}
          <div className="button-row">
            <button type="submit" className="button button-primary button-small" disabled={busy}>
              {submitLabel}
            </button>
            <button type="button" className="button button-quiet button-small" onClick={() => setMode(null)}>
              Cancel
            </button>
          </div>
        </form>
      ) : null}

      <p className="analyst-notice" role="status" aria-live="polite">
        {notice}
      </p>
      {error ? (
        <p className="analyst-error" role="alert">
          {error}
        </p>
      ) : null}
    </section>
  );
}
