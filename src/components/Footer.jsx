import { GITHUB_URL } from './Header.jsx';

export default function Footer() {
  return (
    <footer className="site-footer">
      <div className="container footer-inner">
        <p className="footer-credit">
          Built by{' '}
          <a href="https://cosmik.work" target="_blank" rel="noopener noreferrer">
            Soumik Chatterjee
          </a>
        </p>
        <ul className="footer-links" aria-label="Author links">
          <li>
            <a href="https://cosmik.work" target="_blank" rel="noopener noreferrer">
              cosmik.work
            </a>
          </li>
          <li>
            <a href="https://www.linkedin.com/in/soumikaispecialist/" target="_blank" rel="noopener noreferrer">
              LinkedIn
            </a>
          </li>
          <li>
            <a href={GITHUB_URL} target="_blank" rel="noopener noreferrer">
              GitHub
            </a>
          </li>
        </ul>
        <p className="footer-note">
          Live demo. Tickets you raise are visible to other visitors, so please do not enter real personal data.
        </p>
      </div>
    </footer>
  );
}
