import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Outlet, useSearchParams } from 'react-router-dom';
import { api } from '../lib/api.js';
import { useDocumentTitle, usePolling } from '../lib/hooks.js';
import { OPEN_STATUSES } from '../lib/taxonomy.js';
import { toDate } from '../lib/format.js';
import { Breakdown, KpiStrip } from '../components/Overview.jsx';
import TicketQueue from '../components/TicketQueue.jsx';
import RaiseTicket from '../components/RaiseTicket.jsx';
import Copilot from '../components/Copilot.jsx';

const FILTER_KEYS = ['priority', 'status', 'channel', 'q'];
const REFRESH_MS = 10000;

function openP1From(stats) {
  if (!stats) return undefined;
  if (stats.open_p1 !== undefined) return Number(stats.open_p1);
  if (stats.open_by_priority && stats.open_by_priority.P1 !== undefined) return Number(stats.open_by_priority.P1);
  return undefined;
}

export default function Dashboard() {
  useDocumentTitle('TriageOps, AI service desk for IT MSPs');
  const [searchParams, setSearchParams] = useSearchParams();
  const filters = useMemo(() => {
    const f = {};
    FILTER_KEYS.forEach((k) => {
      f[k] = searchParams.get(k) || '';
    });
    return f;
  }, [searchParams]);
  const filterKey = FILTER_KEYS.map((k) => filters[k]).join('|');

  const [stats, setStats] = useState(null);
  const [openP1, setOpenP1] = useState(undefined);
  const [tickets, setTickets] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [lastUpdated, setLastUpdated] = useState(null);
  const [freshIds, setFreshIds] = useState(() => new Set());

  const requestSeq = useRef(0);
  const seenIds = useRef(null);
  const lastView = useRef(null);
  const filtersRef = useRef(filters);
  filtersRef.current = filters;

  const load = useCallback(async () => {
    const seq = ++requestSeq.current;
    const f = filtersRef.current;
    setRefreshing(true);
    const params = { priority: f.priority, status: f.status, channel: f.channel, q: f.q.trim(), limit: 50 };
    const [statsRes, ticketsRes] = await Promise.allSettled([api.stats(), api.tickets(params)]);
    if (seq !== requestSeq.current) return;

    let p1 = undefined;
    if (statsRes.status === 'fulfilled') {
      setStats(statsRes.value);
      p1 = openP1From(statsRes.value);
      if (p1 === undefined) {
        // The stats endpoint counts P1 across all statuses, so count open P1 tickets directly.
        try {
          const p1Res = await api.tickets({ priority: 'P1', limit: 50 });
          p1 = (p1Res.tickets || []).filter((t) => OPEN_STATUSES.has(t.status)).length;
        } catch {
          p1 = undefined;
        }
        if (seq !== requestSeq.current) return;
      }
      setOpenP1(p1);
    }

    if (ticketsRes.status === 'fulfilled') {
      const list = Array.isArray(ticketsRes.value.tickets) ? ticketsRes.value.tickets : [];
      setTickets(list);
      const now = Date.now();
      const view = FILTER_KEYS.map((k) => f[k]).join('|');
      const sameView = lastView.current === view;
      lastView.current = view;
      if (seenIds.current && sameView) {
        const fresh = new Set();
        list.forEach((t) => {
          const key = t.id || t.ref;
          const created = toDate(t.created_at);
          if (!seenIds.current.has(key) && created && now - created.getTime() < 120000) fresh.add(key);
        });
        setFreshIds(fresh);
      } else {
        if (!seenIds.current) seenIds.current = new Set();
        setFreshIds(new Set());
      }
      list.forEach((t) => seenIds.current.add(t.id || t.ref));
    }

    const failed = [statsRes, ticketsRes].find((r) => r.status === 'rejected');
    setError(failed ? failed.reason.message || 'The service did not respond' : '');
    if (!failed) setLastUpdated(Date.now());
    setLoading(false);
    setRefreshing(false);
  }, []);

  useEffect(() => {
    load();
  }, [filterKey, load]);

  usePolling(load, REFRESH_MS);

  const onFiltersChange = useCallback(
    (changes) => {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          Object.entries(changes).forEach(([k, v]) => {
            if (v) next.set(k, v);
            else next.delete(k);
          });
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  return (
    <main id="main" className="dashboard">
      <div className="container">
        <div className="page-head">
          <h1 className="page-title">Service desk overview</h1>
          <p className="page-lede">
            Tickets arrive by email, WhatsApp, the portal and signed webhooks. Claude sets the category, priority, team and
            SLA for each one, grounded in the knowledge base, and drafts the first reply.
          </p>
        </div>

        <section aria-label="Queue statistics" className="overview">
          <KpiStrip stats={stats} openP1={openP1} />
          <Breakdown stats={stats} loading={loading} />
        </section>

        <div className="workspace">
          <div className="workspace-main">
            <TicketQueue
              tickets={tickets}
              error={error}
              loading={loading}
              filters={filters}
              onFiltersChange={onFiltersChange}
              onRefresh={load}
              refreshing={refreshing}
              lastUpdated={lastUpdated}
              freshIds={freshIds}
            />
          </div>
          <aside className="workspace-rail" aria-label="Try TriageOps">
            <RaiseTicket onCreated={load} />
            <Copilot />
          </aside>
        </div>
      </div>
      <Outlet />
    </main>
  );
}
