import { Link } from 'react-router-dom';
import { useDocumentTitle } from '../lib/hooks.js';

export default function NotFound() {
  useDocumentTitle('Page not found | TriageOps');
  return (
    <main id="main" className="container not-found">
      <h1>This page does not exist</h1>
      <p>The link may be out of date. The live queue is on the dashboard.</p>
      <Link className="button button-primary" to="/">
        Go to the dashboard
      </Link>
    </main>
  );
}
