import { useId, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { adminApi, adminErrorMessage, downloadOverridesCsv, signIn, signOut, useAnalystSession } from '../lib/admin.js';
import { useDocumentTitle } from '../lib/hooks.js';
import '../styles/analyst.css';

function SignInForm() {
  const id = useId();
  const navigate = useNavigate();
  const [token, setToken] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const submit = async (event) => {
    event.preventDefault();
    const value = token.trim();
    if (!value) {
      setError('Enter the analyst token.');
      return;
    }
    setBusy(true);
    setError('');
    try {
      await adminApi.login(value);
      signIn(value);
      setToken('');
      navigate('/');
    } catch (err) {
      setError(err.status === 401 ? 'That token was not accepted. Check it and try again.' : adminErrorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="panel analyst-card" onSubmit={submit} noValidate>
      <h1 className="panel-title">Analyst sign in</h1>
      <p className="panel-sub">
        Analysts approve, override and resolve what Claude triaged. The public dashboard stays read only.
      </p>
      <div className="field">
        <label className="field-label" htmlFor={`${id}-token`}>
          Analyst token
        </label>
        <input
          id={`${id}-token`}
          className="input"
          type="password"
          autoComplete="off"
          spellCheck={false}
          value={token}
          onChange={(e) => setToken(e.target.value)}
          aria-invalid={error ? 'true' : undefined}
          aria-describedby={`${id}-hint${error ? ` ${id}-error` : ''}`}
        />
        <span className="field-hint" id={`${id}-hint`}>
          The token is kept in this browser tab only and is cleared when the tab closes. Repeated wrong tokens lock this
          connection out for 15 minutes.
        </span>
      </div>
      {error ? (
        <p className="analyst-error" id={`${id}-error`} role="alert">
          {error}
        </p>
      ) : null}
      <div className="button-row">
        <button type="submit" className="button button-primary" disabled={busy}>
          {busy ? 'Checking' : 'Sign in'}
        </button>
        <Link to="/" className="button button-quiet">
          Back to the dashboard
        </Link>
      </div>
    </form>
  );
}

function SignedIn() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const exportCsv = async () => {
    setBusy(true);
    setError('');
    setNotice('');
    try {
      await downloadOverridesCsv();
      setNotice('The overrides file has been downloaded.');
    } catch (err) {
      setError(adminErrorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="panel analyst-card" aria-labelledby="analyst-title">
      <h1 className="panel-title" id="analyst-title">
        Analyst console
      </h1>
      <p className="panel-sub">You are signed in. Open any ticket on the dashboard to act on it.</p>
      <ul className="analyst-list">
        <li>Approve the draft reply, with edits if needed. Nothing is sent to the customer.</li>
        <li>Change the priority or team. A reason is required.</li>
        <li>Resolve a ticket with a note, which stops its SLA clock, or reopen it.</li>
      </ul>
      <p className="panel-sub">
        Every override is exported next to Claude&apos;s original decision, ready to use as gold labels for evaluation.
      </p>
      <div className="button-row">
        <Link to="/" className="button button-primary">
          Go to the queue
        </Link>
        <button type="button" className="button" onClick={exportCsv} disabled={busy}>
          {busy ? 'Preparing the file' : 'Export overrides CSV'}
        </button>
        <button type="button" className="button button-quiet" onClick={signOut}>
          Sign out
        </button>
      </div>
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

export default function AnalystPage() {
  useDocumentTitle('Analyst console | TriageOps');
  const { signedIn } = useAnalystSession();
  return (
    <main id="main" className="container analyst-page">
      {signedIn ? <SignedIn /> : <SignInForm />}
    </main>
  );
}
