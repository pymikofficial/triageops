// Markdown rendering with raw HTML disabled. Any HTML in the source is escaped and
// shown as text, links are limited to http, https, mailto and relative paths, and
// images are reduced to their alt text (the CSP would block remote images anyway).
import { Marked } from 'marked';

const HTML_ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

export function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => HTML_ESCAPES[ch]);
}

function safeHref(href) {
  if (!href) return null;
  const h = String(href).trim();
  if (h.startsWith('//')) return null;
  if (/^[a-z][a-z0-9+.-]*:/i.test(h)) return /^(https?|mailto):/i.test(h) ? h : null;
  return h;
}

const safeRenderer = {
  html({ text }) {
    return escapeHtml(text);
  },
  link({ href, title, tokens }) {
    const inner = this.parser.parseInline(tokens);
    const safe = safeHref(href);
    if (!safe) return inner;
    const external = /^https?:/i.test(safe);
    const titleAttr = title ? ` title="${escapeHtml(title)}"` : '';
    const rel = external ? ' target="_blank" rel="noopener noreferrer"' : '';
    return `<a href="${escapeHtml(safe)}"${titleAttr}${rel}>${inner}</a>`;
  },
  image({ text }) {
    return text ? `<span class="md-image-alt">${escapeHtml(text)}</span>` : '';
  },
};

// Ticket references such as TKT-1104-4821 become links that open the ticket drawer.
const TICKET_REF = /^TKT-\d{3,8}(?:-[A-Z0-9]{2,10})?(?![A-Za-z0-9])/;

const ticketRefExtension = {
  name: 'ticketRef',
  level: 'inline',
  start(src) {
    const i = src.search(/TKT-\d/);
    return i < 0 ? undefined : i;
  },
  tokenizer(src) {
    const match = TICKET_REF.exec(src);
    if (match) return { type: 'ticketRef', raw: match[0], ref: match[0] };
    return undefined;
  },
  renderer(token) {
    const ref = escapeHtml(token.ref);
    return `<a href="/ticket/${encodeURIComponent(token.ref)}" class="ref-link" data-ticket-ref="${ref}">${ref}</a>`;
  },
};

const chatMarked = new Marked({
  gfm: true,
  breaks: true,
  renderer: safeRenderer,
  extensions: [ticketRefExtension],
});

export function renderChatMarkdown(src) {
  return chatMarked.parse(String(src ?? ''), { async: false });
}

export function plainHeadingText(text) {
  return String(text ?? '')
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/[`*_~]/g, '')
    .replace(/<[^>]*>/g, '')
    .replace(/&[a-z]+;/gi, ' ')
    .trim();
}

export function createSlugger() {
  const seen = new Map();
  return (text) => {
    let base = plainHeadingText(text)
      .toLowerCase()
      .replace(/&/g, ' and ')
      .replace(/[^a-z0-9\s-]/g, '')
      .trim()
      .replace(/\s+/g, '-')
      .replace(/-+/g, '-');
    if (!base) base = 'section';
    const count = seen.get(base) || 0;
    seen.set(base, count + 1);
    return count === 0 ? base : `${base}-${count}`;
  };
}

// Renders a long document and returns its HTML and heading outline. Each heading
// gets a stable id from its text so the table of contents can link to it.
export function renderDocument(src) {
  const slug = createSlugger();
  const headings = [];
  const md = new Marked({
    gfm: true,
    renderer: {
      ...safeRenderer,
      heading(token) {
        const inner = this.parser.parseInline(token.tokens);
        const id = token.anchorId || slug(token.text);
        return `<h${token.depth} id="${escapeHtml(id)}">${inner}</h${token.depth}>\n`;
      },
    },
    walkTokens(token) {
      if (token.type === 'heading' && !token.anchorId) {
        token.anchorId = slug(token.text);
        headings.push({ depth: token.depth, text: plainHeadingText(token.text), id: token.anchorId });
      }
    },
  });
  const html = md.parse(String(src ?? ''), { async: false });
  return { html, headings };
}
