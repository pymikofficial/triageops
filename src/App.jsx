import { Suspense, lazy, useEffect } from 'react';
import { Route, Routes, useLocation } from 'react-router-dom';
import Header from './components/Header.jsx';
import Footer from './components/Footer.jsx';
import Dashboard from './pages/Dashboard.jsx';
import TicketDrawer from './components/TicketDrawer.jsx';
import NotFound from './pages/NotFound.jsx';

const BuildPage = lazy(() => import('./pages/BuildPage.jsx'));
const OpsPage = lazy(() => import('./pages/OpsPage.jsx'));

function ScrollManager() {
  const { pathname, hash } = useLocation();
  useEffect(() => {
    // Opening or closing the ticket drawer keeps the dashboard scroll position.
    if (pathname === '/' || pathname.startsWith('/ticket/')) return;
    if (hash) return;
    window.scrollTo(0, 0);
  }, [pathname, hash]);
  return null;
}

export default function App() {
  return (
    <div className="app">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <ScrollManager />
      <Header />
      <Routes>
        <Route path="/" element={<Dashboard />}>
          <Route path="ticket/:ref" element={<TicketDrawer />} />
        </Route>
        <Route
          path="/build"
          element={
            <Suspense fallback={<main id="main" className="container page-loading" aria-busy="true">Loading the build document</main>}>
              <BuildPage />
            </Suspense>
          }
        />
        <Route
          path="/ops"
          element={
            <Suspense fallback={<main id="main" className="container page-loading" aria-busy="true">Loading the operations view</main>}>
              <OpsPage />
            </Suspense>
          }
        />
        <Route path="*" element={<NotFound />} />
      </Routes>
      <Footer />
    </div>
  );
}
