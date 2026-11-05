import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import results from '../../evals/results/latest.json';
import { useDocumentTitle } from '../lib/hooks.js';
import { BackIcon } from '../components/Icons.jsx';
import { PriorityBadge } from '../components/Badges.jsx';
import './EvalsPage.css';

const GITHUB_WORKFLOW = 'https://github.com/pymikofficial/triageops/actions/workflows/evals.yml';

const FIELD_LABEL = { priority: 'Priority', category: 'Category', team: 'Team', injection: 'Injection' };

function pct(value) {
  return value === null || value === undefined ? 'n/a' : `${(value * 100).toFixed(1)}%`;
}

function seconds(ms) {
  if (ms === null || ms === undefined) return 'n/a';
  return ms >= 1000 ? `${(ms / 1000).toFixed(2)} s` : `${Math.round(ms)} ms`;
}

function usd(value, places = 5) {
  return value === null || value === undefined ? 'n/a' : `$${value.toFixed(places)}`;
}

function plural(count, word) {
  return `${count} ${word}${count === 1 ? '' : 's'}`;
}

function valueText(field, values) {
  if (!values) return 'no result';
  if (field === 'injection') return values.injection ? 'flagged' : 'not flagged';
  return values[field];
}

function Tile({ label, value, note, tone }) {
  return (
    <div className={`eval-tile${tone ? ` eval-tile-${tone}` : ''}`}>
      <dt className="eval-tile-label">{label}</dt>
      <dd className="eval-tile-value">{value}</dd>
      {note ? <dd className="eval-tile-note">{note}</dd> : null}
    </div>
  );
}

function GateCard({ gate, p1 }) {
  const passed = gate.passed;
  return (
    <section className={`eval-gate ${passed ? 'is-pass' : 'is-fail'}`} aria-labelledby="gate-title">
      <span className="eval-gate-icon" aria-hidden="true">
        {passed ? '✓' : '✕'}
      </span>
      <div>
        <h2 id="gate-title" className="eval-gate-title">
          Release gate {passed ? 'passed' : 'failed'}
        </h2>
        <p className="eval-gate-text">
          P1 recall is {pct(gate.value)} against a target of {pct(gate.target)}. {p1.caught} of {p1.expected} critical
          tickets were triaged as P1.
          {p1.missed.length > 0 ? ` Missed: ${p1.missed.join(', ')}.` : ' None were missed.'}
        </p>
      </div>
    </section>
  );
}

function ConfusionMatrix({ priority }) {
  const { labels, columns, confusion, per_class: perClass } = priority;
  const max = Math.max(1, ...confusion.flat());
  return (
    <div className="eval-matrix-wrap">
      <table className="eval-matrix">
        <caption className="eval-matrix-caption">
          Rows are the expected priority, columns are the final priority after the business rules. The outlined
          diagonal is correct.
        </caption>
        <thead>
          <tr>
            <th scope="col" className="eval-matrix-corner">
              Expected
            </th>
            {columns.map((col) => (
              <th scope="col" key={col}>
                {col === 'none' ? 'No result' : col}
              </th>
            ))}
            <th scope="col">Recall</th>
          </tr>
        </thead>
        <tbody>
          {labels.map((label, row) => (
            <tr key={label}>
              <th scope="row">
                <PriorityBadge priority={label} />
              </th>
              {confusion[row].map((count, col) => {
                const share = count / max;
                const diagonal = col === row;
                const style = count
                  ? { '--cell-alpha': (0.1 + 0.75 * share).toFixed(3) }
                  : undefined;
                const predicted = columns[col] === 'none' ? 'no result' : columns[col];
                return (
                  <td
                    key={columns[col]}
                    className={`eval-cell${count ? ' has-value' : ''}${share > 0.55 ? ' is-strong' : ''}${diagonal ? ' is-diagonal' : ''}`}
                    style={style}
                    title={`Expected ${label}, triaged as ${predicted}: ${plural(count, 'ticket')}`}
                  >
                    {count}
                  </td>
                );
              })}
              <td className="eval-recall">{pct(perClass[label].recall)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="eval-matrix-note">
        {plural(priority.under_triaged, 'ticket')} under triaged (above the diagonal), {plural(priority.over_triaged, 'ticket')}{' '}
        over triaged (below it).
      </p>
    </div>
  );
}

function Mismatch({ field, item }) {
  const expected = valueText(field, item.expected);
  const actual = valueText(field, item.actual);
  return (
    <li className="eval-mismatch">
      <span className="eval-mismatch-field">{FIELD_LABEL[field]}</span>
      {field === 'priority' ? (
        <>
          <span className="eval-mismatch-label">expected</span> <PriorityBadge priority={item.expected.priority} />
          <span className="eval-mismatch-label">got</span>{' '}
          {item.actual ? <PriorityBadge priority={item.actual.priority} /> : <span>no result</span>}
        </>
      ) : (
        <span>
          <span className="eval-mismatch-label">expected</span> {expected}{' '}
          <span className="eval-mismatch-label">got</span> {actual}
        </span>
      )}
    </li>
  );
}

function FailureItem({ item, fallbackRun }) {
  const proposed = item.model_proposed;
  const ruleChanged = proposed && item.actual && proposed.priority !== item.actual.priority;
  return (
    <li className="eval-failure">
      <div className="eval-failure-head">
        <span className="ref ref-plain">{item.id}</span>
        <span className="eval-failure-subject">{item.subject}</span>
        {item.expected.priority === 'P1' && item.mismatches.includes('priority') ? (
          <span className="inline-flag flag-danger">Missed P1</span>
        ) : null}
        {item.fallback && !fallbackRun ? <span className="inline-flag flag-neutral">Keyword fallback</span> : null}
        {item.error ? <span className="inline-flag flag-danger">Pipeline error</span> : null}
      </div>
      <ul className="eval-mismatches">
        {item.mismatches.map((field) => (
          <Mismatch key={field} field={field} item={item} />
        ))}
      </ul>
      <p className="eval-failure-meta">
        {item.rationale}
        {ruleChanged ? ` The model proposed ${proposed.priority} and the business rules changed it.` : ''}
        {item.tags.length > 0 ? (
          <span className="eval-tags">
            {item.tags.map((tag) => (
              <span key={tag} className="eval-tag">
                {tag}
              </span>
            ))}
          </span>
        ) : null}
      </p>
    </li>
  );
}

const FILTERS = [
  { key: 'all', label: 'All', test: () => true },
  { key: 'p1', label: 'Missed P1', test: (f) => f.expected.priority === 'P1' && f.mismatches.includes('priority') },
  { key: 'priority', label: 'Priority', test: (f) => f.mismatches.includes('priority') },
  { key: 'category', label: 'Category', test: (f) => f.mismatches.includes('category') },
  { key: 'team', label: 'Team', test: (f) => f.mismatches.includes('team') },
  { key: 'injection', label: 'Injection', test: (f) => f.mismatches.includes('injection') },
];

function Failures({ failures, fallbackRun }) {
  const [filter, setFilter] = useState('all');
  const active = FILTERS.find((f) => f.key === filter) || FILTERS[0];
  const shown = useMemo(() => failures.filter(active.test), [failures, active]);
  return (
    <section className="eval-section" aria-labelledby="failures-title">
      <h2 id="failures-title" className="eval-section-title">
        Failures <span className="muted">({failures.length})</span>
      </h2>
      <div className="eval-filters" role="group" aria-label="Filter failures">
        {FILTERS.map((f) => (
          <button
            key={f.key}
            type="button"
            className={`eval-filter${filter === f.key ? ' is-active' : ''}`}
            aria-pressed={filter === f.key}
            onClick={() => setFilter(f.key)}
          >
            {f.label} <span className="eval-filter-count">{failures.filter(f.test).length}</span>
          </button>
        ))}
      </div>
      {shown.length === 0 ? (
        <p className="block-empty">No failures of this kind.</p>
      ) : (
        <ol className="eval-failures">
          {shown.map((item) => (
            <FailureItem key={item.id} item={item} fallbackRun={fallbackRun} />
          ))}
        </ol>
      )}
    </section>
  );
}

export default function EvalsPage() {
  useDocumentTitle('Evaluation | TriageOps');
  const { run, gate, metrics: m, failures } = results;
  const fallbackRun = run.mode === 'rules-fallback';
  const generated = new Date(run.generated_at);

  return (
    <main id="main" className="container eval-page">
      <Link to="/" className="back-link">
        <BackIcon size={15} />
        Back to the dashboard
      </Link>

      <div className="page-head">
        <h1 className="page-title">Triage evaluation</h1>
        <p className="page-lede">
          {m.tickets} labelled UK MSP tickets run through the real triage pipeline, the Claude call and the business
          rules, against a fake database. P1 recall is the release gate.
        </p>
      </div>

      {fallbackRun ? (
        <p className="eval-notice" role="note">
          These numbers come from the keyword fallback, not from Claude, because no API key was used for this run. They
          show the floor the service falls back to. Run the{' '}
          <a href={GITHUB_WORKFLOW} target="_blank" rel="noopener noreferrer">
            Evals workflow
          </a>{' '}
          to measure Claude.
        </p>
      ) : null}
      {!fallbackRun && m.fallbacks.count > 0 ? (
        <p className="eval-notice" role="note">
          {plural(m.fallbacks.count, 'ticket')} fell back to keyword rules because the Claude call failed (
          {m.fallbacks.ids.join(', ')}).
        </p>
      ) : null}

      <GateCard gate={gate} p1={m.p1} />

      <dl className="eval-tiles">
        <Tile
          label="P1 recall"
          value={pct(m.p1.recall)}
          note={`${m.p1.caught} of ${m.p1.expected}, precision ${pct(m.p1.precision)}`}
          tone={gate.passed ? null : 'critical'}
        />
        <Tile label="Priority accuracy" value={pct(m.priority.accuracy)} note={`${m.priority.correct} of ${m.priority.total}`} />
        <Tile label="Category accuracy" value={pct(m.category.accuracy)} note={`${m.category.correct} of ${m.category.total}`} />
        <Tile label="Team accuracy" value={pct(m.team.accuracy)} note={`${m.team.correct} of ${m.team.total}`} />
        <Tile
          label="Injection detection"
          value={pct(m.injection.detection_rate)}
          note={`${m.injection.detected} of ${m.injection.expected} caught, ${plural(m.injection.false_positives, 'false positive')}`}
        />
        <Tile label="Mean latency" value={seconds(m.latency_ms.mean)} note={`p95 ${seconds(m.latency_ms.p95)} per ticket`} />
        <Tile
          label="Cost per ticket"
          value={usd(m.cost_usd.per_ticket)}
          note={
            m.tokens.billed_tickets > 0
              ? `${Math.round(m.tokens.input_mean)} in, ${Math.round(m.tokens.output_mean)} out tokens`
              : 'No model calls were billed'
          }
        />
        <Tile label="Fully correct" value={`${m.fully_correct} of ${m.tickets}`} note="All four labels right" />
      </dl>

      <section className="eval-section" aria-labelledby="matrix-title">
        <h2 id="matrix-title" className="eval-section-title">
          Priority confusion matrix
        </h2>
        <ConfusionMatrix priority={m.priority} />
      </section>

      <Failures failures={failures} fallbackRun={fallbackRun} />

      <section className="eval-section" aria-labelledby="run-title">
        <h2 id="run-title" className="eval-section-title">
          About this run
        </h2>
        <dl className="eval-run">
          <div>
            <dt>Model</dt>
            <dd className="mono">{run.model}</dd>
          </div>
          <div>
            <dt>Generated</dt>
            <dd>{Number.isNaN(generated.getTime()) ? run.generated_at : generated.toUTCString()}</dd>
          </div>
          <div>
            <dt>Dataset</dt>
            <dd>
              <span className="mono">{run.dataset.path}</span>, {run.dataset.tickets} tickets
            </dd>
          </div>
          <div>
            <dt>Prompt version</dt>
            <dd className="mono">{run.prompt_sha256}</dd>
          </div>
          <div>
            <dt>Rules that changed a decision</dt>
            <dd>
              {Object.keys(m.rules_fired).length === 0
                ? 'None'
                : Object.entries(m.rules_fired)
                    .map(([rule, count]) => `${rule} (${count})`)
                    .join(', ')}
            </dd>
          </div>
        </dl>
      </section>
    </main>
  );
}
