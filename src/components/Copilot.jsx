import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { api } from '../lib/api.js';
import { renderChatMarkdown } from '../lib/markdown.js';
import { formatSeconds } from '../lib/format.js';
import { SendIcon } from './Icons.jsx';

const SUGGESTED = [
  'Which P1 tickets are open right now?',
  "Summarise today's queue by customer",
  'How do we fix VPN error 809?',
  'Which tickets need human review and why?',
];

const MAX_HISTORY = 12;
const MAX_CHARS = 2000;

function buildHistory(messages) {
  const history = messages
    .filter((m) => !m.error && (m.role === 'user' || m.role === 'assistant'))
    .map((m) => ({ role: m.role, content: String(m.content).slice(0, MAX_CHARS) }))
    .slice(-MAX_HISTORY);
  while (history.length > 0 && history[0].role !== 'user') history.shift();
  return history;
}

function chatError(err) {
  if (err.status === 429) return 'Copilot limit reached, please try again in an hour.';
  if (err.status >= 500) return 'The copilot could not answer just now. Please try again in a moment.';
  if (err.status === 400 || err.status === 422) return `The question was not accepted. ${err.message}.`;
  return `${err.message}. Check your connection and try again.`;
}

function AssistantMessage({ message }) {
  const html = useMemo(() => renderChatMarkdown(message.content), [message.content]);
  // A tool can run more than once in one answer, so show each name once with its call count.
  const tools = useMemo(() => {
    const byName = new Map();
    (Array.isArray(message.tools) ? message.tools : []).forEach((tool) => {
      if (!tool || !tool.name) return;
      const entry = byName.get(tool.name) || { name: tool.name, calls: 0, inputs: [] };
      entry.calls += 1;
      if (tool.input && Object.keys(tool.input).length > 0) entry.inputs.push(JSON.stringify(tool.input));
      byName.set(tool.name, entry);
    });
    return Array.from(byName.values());
  }, [message.tools]);
  return (
    <>
      <div className="chat-markdown" dangerouslySetInnerHTML={{ __html: html }} />
      {tools.length > 0 || message.model ? (
        <div className="chat-meta">
          {tools.length > 0 ? (
            <ul className="tool-chips" aria-label="Tools used">
              {tools.map((tool) => (
                <li key={tool.name} className="tool-chip" title={tool.inputs.length > 0 ? tool.inputs.join('\n') : undefined}>
                  {tool.name}
                  {tool.calls > 1 ? <span className="tool-chip-count"> {tool.calls} calls</span> : null}
                </li>
              ))}
            </ul>
          ) : (
            <span className="chat-meta-note">Answered without tools</span>
          )}
          {message.model ? (
            <span className="chat-model">
              {message.model}
              {Number.isFinite(Number(message.latency)) ? `, ${formatSeconds(message.latency)}` : ''}
            </span>
          ) : null}
        </div>
      ) : null}
    </>
  );
}

export default function Copilot() {
  const uid = useId();
  const navigate = useNavigate();
  const location = useLocation();
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const [pending, setPending] = useState(false);
  const listRef = useRef(null);
  const inputRef = useRef(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, pending]);

  const send = async (text) => {
    const question = String(text || '').trim();
    if (!question || pending) return;
    const userMessage = { role: 'user', content: question.slice(0, MAX_CHARS) };
    const next = [...messages, userMessage];
    setMessages(next);
    setInput('');
    setPending(true);
    try {
      const data = await api.chat(buildHistory(next));
      if (!mounted.current) return;
      setMessages((m) => [
        ...m,
        {
          role: 'assistant',
          content: data.reply || 'The copilot returned an empty answer.',
          tools: data.tools_used || [],
          model: data.model,
          latency: data.latency_ms,
        },
      ]);
    } catch (err) {
      if (!mounted.current) return;
      setMessages((m) => [...m, { role: 'assistant', content: chatError(err), error: true }]);
    } finally {
      if (mounted.current) {
        setPending(false);
        if (inputRef.current) inputRef.current.focus();
      }
    }
  };

  const onSubmit = (event) => {
    event.preventDefault();
    send(input);
  };

  const onKeyDown = (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      send(input);
    }
  };

  // Ticket references inside answers open the ticket drawer without a full page load.
  const onListClick = (event) => {
    const link = event.target.closest('a[data-ticket-ref]');
    if (!link) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
    event.preventDefault();
    navigate({ pathname: `/ticket/${encodeURIComponent(link.dataset.ticketRef)}`, search: location.search });
  };

  const empty = messages.length === 0;

  return (
    <section className="panel copilot-panel" aria-labelledby={`${uid}-title`}>
      <div className="panel-head copilot-head">
        <div>
          <h2 className="panel-title" id={`${uid}-title`}>
            Ask the service desk copilot
          </h2>
          <p className="panel-sub">Claude answers with read-only tools over the live queue and the knowledge base.</p>
        </div>
        {!empty ? (
          <button type="button" className="button button-quiet button-small" onClick={() => setMessages([])} disabled={pending}>
            Clear
          </button>
        ) : null}
      </div>

      <div className="chat-log" ref={listRef} role="log" aria-live="polite" aria-relevant="additions" aria-label="Copilot conversation" onClick={onListClick}>
        {empty ? (
          <div className="chat-empty">
            <p className="chat-empty-text">Try one of these questions.</p>
            <ul className="suggestions">
              {SUGGESTED.map((s) => (
                <li key={s}>
                  <button type="button" className="suggestion" onClick={() => send(s)} disabled={pending}>
                    {s}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        ) : (
          messages.map((m, i) => (
            <div key={i} className={`chat-message chat-${m.role}${m.error ? ' chat-error' : ''}`}>
              <span className="visually-hidden">{m.role === 'user' ? 'You asked' : 'Copilot answered'}</span>
              {m.role === 'assistant' && !m.error ? <AssistantMessage message={m} /> : <p className="chat-text">{m.content}</p>}
            </div>
          ))
        )}
        {pending ? (
          <div className="chat-message chat-assistant chat-pending">
            <span className="typing" aria-hidden="true">
              <span />
              <span />
              <span />
            </span>
            <span className="chat-pending-text">Checking the queue and knowledge base</span>
          </div>
        ) : null}
      </div>

      <form className="chat-form" onSubmit={onSubmit}>
        <label className="visually-hidden" htmlFor={`${uid}-input`}>
          Ask a question about the queue
        </label>
        <textarea
          id={`${uid}-input`}
          ref={inputRef}
          className="textarea chat-input"
          rows={2}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={onKeyDown}
          maxLength={MAX_CHARS}
          placeholder="Ask about tickets, customers or fixes"
          aria-describedby={`${uid}-hint`}
        />
        <button type="submit" className="button button-primary chat-send" disabled={pending || !input.trim()}>
          <SendIcon size={16} />
          <span>Send</span>
        </button>
        <p className="chat-hint" id={`${uid}-hint`}>
          Enter sends, Shift and Enter adds a new line. The last 12 messages are sent as context.
        </p>
      </form>
    </section>
  );
}
