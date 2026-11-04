import { Link, NavLink, useLocation } from 'react-router-dom';
import { LogoMark, ExternalIcon } from './Icons.jsx';
import StatusPill from './StatusPill.jsx';

export const GITHUB_URL = 'https://github.com/pymikofficial/triageops';

export default function Header() {
  const location = useLocation();
  const onDashboard = location.pathname === '/' || location.pathname.startsWith('/ticket/');

  return (
    <header className="site-header">
      <div className="container header-inner">
        <Link to="/" className="brand" aria-label="TriageOps dashboard">
          <LogoMark />
          <span className="brand-name">TriageOps</span>
        </Link>
        <p className="brand-tagline">AI service desk for IT MSPs, built with Claude</p>
        <nav className="site-nav" aria-label="Main">
          <Link to="/" className={`nav-link${onDashboard ? ' is-active' : ''}`} aria-current={onDashboard ? 'page' : undefined}>
            Dashboard
          </Link>
          <NavLink to="/build" className={({ isActive }) => `nav-link${isActive ? ' is-active' : ''}`}>
            How it was built
          </NavLink>
          <NavLink to="/evals" className={({ isActive }) => `nav-link${isActive ? ' is-active' : ''}`}>
            Evaluation
          </NavLink>
          <a className="nav-link" href={GITHUB_URL} target="_blank" rel="noopener noreferrer">
            GitHub
            <ExternalIcon size={13} className="nav-external" />
            <span className="visually-hidden"> (opens in a new tab)</span>
          </a>
        </nav>
        <StatusPill />
      </div>
    </header>
  );
}
