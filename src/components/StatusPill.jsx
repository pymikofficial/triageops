import { useCallback, useEffect, useState } from 'react';
import { api } from '../lib/api.js';
import { usePolling } from '../lib/hooks.js';

function describe(state) {
  if (state.phase === 'loading') return { tone: 'checking', text: 'Checking status', title: 'Checking the service' };
  if (state.phase === 'error' || !state.data || state.data.ok === false) {
    return { tone: 'offline', text: 'Offline', title: 'The service did not respond. The page will keep trying.' };
  }
  const d = state.data;
  if (d.db === false) {
    return { tone: 'offline', text: 'Offline', title: 'The database is unavailable, so new tickets cannot be stored.' };
  }
  const parts = ['Database connected'];
  if (d.kb_articles !== undefined && Number.isFinite(Number(d.kb_articles))) {
    parts.push(`${d.kb_articles} knowledge base articles`);
  }
  if (d.region) parts.push(`Region ${d.region}`);
  if (d.version) parts.push(`Version ${d.version}`);
  if (d.ai) {
    return { tone: 'live', text: 'Live, Claude connected', title: `${parts.join('. ')}.` };
  }
  return {
    tone: 'fallback',
    text: 'Fallback mode',
    title: `Claude is not connected, so tickets are triaged by keyword rules. ${parts.join('. ')}.`,
  };
}

export default function StatusPill() {
  const [state, setState] = useState({ phase: 'loading', data: null });

  const check = useCallback(async () => {
    try {
      const data = await api.health();
      setState({ phase: 'ready', data });
    } catch {
      setState({ phase: 'error', data: null });
    }
  }, []);

  useEffect(() => {
    check();
  }, [check]);
  usePolling(check, 60000);

  const info = describe(state);
  return (
    <span className={`status-pill status-pill-${info.tone}`} title={info.title} role="status">
      <span className="status-pill-dot" aria-hidden="true" />
      {info.text}
    </span>
  );
}
