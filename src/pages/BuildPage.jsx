import { useEffect, useMemo, useState } from 'react';
import { Link, useLocation } from 'react-router-dom';
import buildDoc from '../../docs/BUILD.md?raw';
import { renderDocument } from '../lib/markdown.js';
import { useDocumentTitle } from '../lib/hooks.js';
import { BackIcon } from '../components/Icons.jsx';

// The ten stages of the build, in order. Each one links to the matching h2 in BUILD.md.
const STAGES = [
  { name: 'Requirement', match: /requirement/i },
  { name: 'Architecture', match: /architecture/i },
  { name: 'AI', match: /(^|[^a-z])(ai|claude|llm|model)([^a-z]|$)/i },
  { name: 'API Integration', match: /api\s+integration|integration/i },
  { name: 'Backend', match: /back\s*-?end/i },
  { name: 'Database', match: /database|postgres|supabase/i },
  { name: 'Frontend', match: /front\s*-?end|react|dashboard/i },
  { name: 'Automation', match: /automation|n8n|workflow/i },
  { name: 'Deployment', match: /deploy/i },
  { name: 'Security', match: /security/i },
];

function stripNumbering(text) {
  return text.replace(/^\s*(stage\s*)?\d{1,2}\s*[.):-]?\s*/i, '');
}

function mapStages(headings) {
  const h2 = headings.filter((h) => h.depth === 2);
  const used = new Set();
  return STAGES.map((stage, index) => {
    const number = index + 1;
    // Prefer a heading that carries the stage number and name, then the name alone.
    const numbered = new RegExp(`^\\s*(stage\\s*)?0?${number}(?!\\d)`, 'i');
    let found = h2.find((h) => !used.has(h.id) && numbered.test(h.text) && stage.match.test(stripNumbering(h.text)));
    if (!found) found = h2.find((h) => !used.has(h.id) && stage.match.test(stripNumbering(h.text)));
    if (found) used.add(found.id);
    return { ...stage, number, id: found ? found.id : null };
  });
}

function useActiveHeading(ids) {
  const [active, setActive] = useState(null);
  useEffect(() => {
    if (ids.length === 0 || typeof IntersectionObserver === 'undefined') return undefined;
    const elements = ids.map((id) => document.getElementById(id)).filter(Boolean);
    const visible = new Map();
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => visible.set(entry.target.id, entry.isIntersecting));
        const firstVisible = elements.find((el) => visible.get(el.id));
        if (firstVisible) {
          setActive(firstVisible.id);
          return;
        }
        // Between headings: pick the last heading above the viewport top.
        const above = elements.filter((el) => el.getBoundingClientRect().top < 120);
        if (above.length > 0) setActive(above[above.length - 1].id);
      },
      { rootMargin: '-80px 0px -65% 0px', threshold: 0 },
    );
    elements.forEach((el) => observer.observe(el));
    return () => observer.disconnect();
  }, [ids]);
  return active;
}

export default function BuildPage() {
  useDocumentTitle('How it was built | TriageOps');
  const { hash } = useLocation();
  const { html, headings } = useMemo(() => renderDocument(buildDoc), []);
  const toc = useMemo(() => headings.filter((h) => h.depth === 2), [headings]);
  const stages = useMemo(() => mapStages(headings), [headings]);
  const tocIds = useMemo(() => toc.map((h) => h.id), [toc]);
  const active = useActiveHeading(tocIds);
  const activeStage = stages.findIndex((s) => s.id && s.id === active);

  // Honour a deep link such as /build#security once the document is in the page.
  useEffect(() => {
    if (!hash) return;
    const el = document.getElementById(decodeURIComponent(hash.slice(1)));
    if (el) el.scrollIntoView();
  }, [hash]);

  return (
    <main id="main" className="build-page">
      <div className="container">
        <Link to="/" className="back-link">
          <BackIcon size={15} />
          Back to the dashboard
        </Link>

        <nav className="stage-rail" aria-label="Build stages">
          <ol className="stage-list">
            {stages.map((stage, i) => {
              const state = activeStage === -1 ? '' : i < activeStage ? ' is-past' : i === activeStage ? ' is-current' : '';
              const inner = (
                <>
                  <span className="stage-number">{stage.number}</span>
                  <span className="stage-name">{stage.name}</span>
                </>
              );
              return (
                <li key={stage.name} className={`stage${state}`}>
                  {stage.id ? (
                    <a href={`#${stage.id}`} className="stage-link" aria-current={i === activeStage ? 'step' : undefined}>
                      {inner}
                    </a>
                  ) : (
                    <span className="stage-link is-disabled" title="This stage is not in the document yet">
                      {inner}
                    </span>
                  )}
                </li>
              );
            })}
          </ol>
        </nav>

        <div className={`doc-layout${toc.length > 0 ? '' : ' no-toc'}`}>
          {toc.length > 0 ? (
            <nav className="doc-toc" aria-label="On this page">
              <p className="doc-toc-title">On this page</p>
              <ol>
                {toc.map((h) => (
                  <li key={h.id}>
                    <a href={`#${h.id}`} className={h.id === active ? 'is-active' : undefined} aria-current={h.id === active ? 'location' : undefined}>
                      {h.text}
                    </a>
                  </li>
                ))}
              </ol>
            </nav>
          ) : null}
          <article className="doc" dangerouslySetInnerHTML={{ __html: html }} />
        </div>
      </div>
    </main>
  );
}
