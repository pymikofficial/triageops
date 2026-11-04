# TriageOps

An AI service desk for an IT managed service provider. Tickets arrive by email, WhatsApp, a web portal and signed webhooks, Claude triages each one against the MSP knowledge base, and the team works the queue from a live dashboard.

**Live demo** https://triageops.vercel.app  
**How it was built** https://triageops.vercel.app/build (the ten stage build document, also in [docs/BUILD.md](docs/BUILD.md))

```text
Requirement → Architecture → AI → API Integration → Backend → Database → Frontend → Automation → Deployment → Security
```

## What it does

1. **Ingests** tickets from four channels. A public portal, a Postmark style inbound email webhook, the Meta WhatsApp Cloud API, and a generic HMAC signed webhook that n8n, Make, Zapier or a PSA can call.
2. **Protects** the data before any model sees it. Passwords, API keys, tokens, card numbers and National Insurance numbers are redacted, the requester is stored in masked form, and the client IP is replaced by a keyed hash.
3. **Retrieves** the three most relevant knowledge base articles with pgvector (gte-small embeddings, 384 dimensions).
4. **Triages** with Claude through forced tool use, so the output always matches a JSON schema. Category, priority, sentiment, team, summary, draft reply, next steps, confidence and an injection flag.
5. **Applies business rules** in Python after the model. Ransomware is always P1 for Security Ops, security tickets are never below P2, suspected prompt injection goes to human review, and SLA deadlines are computed in code.
6. **Alerts** a Slack, Discord or Teams channel for P1 and escalated tickets, and sweeps for SLA breaches on a schedule.
7. **Answers questions** about the queue through a copilot chat agent, and exposes the same read-only tools to Claude Desktop, Claude Code or any MCP client through an MCP server.
8. **Falls back** to deterministic keyword triage if the model is unavailable, so a ticket is never lost.

## Stack

| Layer | Choice |
|---|---|
| AI | Claude API. `claude-haiku-5-5` for triage, `claude-sonnet-5-5` for the copilot agent, both set by env var |
| Backend | Python 3.12, FastAPI, psycopg 3, httpx, Pydantic |
| Database | Supabase Postgres 17 in London (eu-west-2), pgvector, row level security, least-privilege role |
| Embeddings | Supabase Edge Function running gte-small, no third party embedding key |
| Frontend | React 18 and Vite, react-router, marked |
| Hosting | Vercel (London, lhr1) for the live demo. Docker, nginx, Certbot and systemd kit for any Ubuntu VPS in `deploy/` |
| Automation | n8n workflow (Gmail to signed webhook to Slack), Vercel Cron, outbound alert webhook |
| CI | GitHub Actions. Ruff, pytest, Vite build, then Vercel Git integration deploys `main` |

## Quick start (local)

Requires Python 3.12 and Node 22. The backend and the frontend run side by side, and Vite proxies `/api` to port 8000.

```bash
git clone https://github.com/pymikofficial/triageops.git
cd triageops
cp .env.example .env
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt "uvicorn[standard]"
set -a
source .env
set +a
uvicorn triageops.app:app --reload --port 8000
```

In a second terminal.

```bash
npm ci
npm run dev
```

Open http://localhost:5173. Without `ANTHROPIC_API_KEY` the pipeline still runs and every ticket is triaged by the rules fallback, which is a quick way to see the failure path.

Send a signed ticket to the webhook to see HMAC authentication end to end (it reads `WEBHOOK_SECRET` from the environment).

```bash
python scripts/send_test_ticket.py --list
python scripts/send_test_ticket.py --sample 1 --url http://localhost:8000
```

Connect the MCP server to Claude Code.

```bash
claude mcp add --transport http triageops https://<host>/api/mcp --header "Authorization: Bearer <MCP_TOKEN>"
```

### Environment variables

All variables are server side only and are never sent to the browser. The full list with comments is in [.env.example](.env.example).

| Name | Required | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | yes | Claude API key |
| `CLAUDE_TRIAGE_MODEL` | no | Triage model, default `claude-haiku-5-5` |
| `CLAUDE_AGENT_MODEL` | no | Copilot model, default `claude-sonnet-5-5` |
| `DATABASE_URLS` | yes | Comma separated Postgres URLs tried in order (Supabase pooler, role `triage_api`) |
| `EMBED_URL` | yes | Supabase Edge Function that returns gte-small embeddings |
| `EMBED_SECRET` | yes | Shared secret sent as `x-embed-secret` |
| `WEBHOOK_SECRET` | yes | HMAC key for `/api/webhooks/inbound` and `/api/webhooks/email` |
| `WHATSAPP_VERIFY_TOKEN` | for WhatsApp | Meta webhook verification handshake |
| `WHATSAPP_APP_SECRET` | for WhatsApp | Key for Meta `X-Hub-Signature-256` |
| `CRON_SECRET` | yes | Bearer token that Vercel Cron sends to `/api/cron/sla` |
| `MCP_TOKEN` | for MCP | Bearer token required by `/api/mcp` |
| `ALERT_WEBHOOK_URL` | no | Slack, Discord or Teams incoming webhook for P1 alerts |
| `IP_HASH_SALT` | yes | Salt for hashing client IPs (raw IPs are never stored) |
| `DEMO_HOURLY_LIMIT` | no | Public portal tickets per IP per hour, default 6 |
| `DEMO_DAILY_LIMIT` | no | Public portal tickets per day across all users, default 300 |
| `CHAT_HOURLY_LIMIT` | no | Copilot messages per IP per hour, default 20 |

## API reference

All endpoints are under `/api` and speak JSON.

| Method and path | Auth | Purpose |
|---|---|---|
| `GET /api/health` | none | Liveness plus database, AI and knowledge base checks |
| `GET /api/stats` | none | Queue counts by priority, category, status and channel, latency, SLA breaches |
| `GET /api/tickets` | none | Queue, newest first. Filters `status`, `priority`, `channel`, `q`, `limit` |
| `GET /api/tickets/{id}` | none | One ticket by UUID or reference, with its event timeline |
| `GET /api/kb` | none | Knowledge base article list |
| `POST /api/demo/tickets` | rate limited | Public portal submission, triaged synchronously |
| `POST /api/webhooks/inbound` | HMAC | Generic signed webhook for n8n, Make, Zapier or a PSA |
| `POST /api/webhooks/email` | HMAC | Postmark style inbound email |
| `GET /api/webhooks/whatsapp` | verify token | Meta subscription handshake |
| `POST /api/webhooks/whatsapp` | `X-Hub-Signature-256` | WhatsApp Cloud API messages, one ticket per message |
| `POST /api/chat` | rate limited | Copilot agent with read-only tools |
| `GET /api/cron/sla` | bearer `CRON_SECRET` | Marks breached SLAs and alerts on P1 and P2 |
| `POST /api/mcp` | bearer `MCP_TOKEN` | MCP server (streamable HTTP, stateless) |

Signed webhooks send `X-TriageOps-Timestamp` (unix seconds) and `X-TriageOps-Signature: sha256=<hex>`, the HMAC SHA-256 of `{timestamp}.{raw_body}`. Requests older than 300 seconds are rejected.

## Project structure

```text
api/index.py               Vercel entry point, exports the FastAPI app
triageops/
  app.py                   routes, middleware, security headers, error handling
  schemas.py, channels.py  request models and per channel adapters
  triage.py                pipeline, forced tool use Claude call, SLA sweep
  decision.py, rules.py    model output repair, business rules, keyword fallback
  rag.py, kb_seed.py       embeddings client, match_kb retrieval, idempotent seeding
  agent.py, tools.py       copilot agent loop and the four read-only tools
  mcp.py                   MCP JSON-RPC handler
  security.py              HMAC, replay window, masking, IP hashing, rate limiter
  alerts.py                outbound Slack, Discord or Teams webhook
  store.py, db.py          every SQL statement, connection helper
  config.py, taxonomy.py   settings from env vars, shared vocabulary
  samples.py               eight sample tickets from fictional UK SMEs
tests/                     pytest with a fake store and a fake Claude, no network
scripts/send_test_ticket.py
supabase/migrations/       schema, RLS, least-privilege role
supabase/functions/embed/  gte-small embedding Edge Function
src/                       React dashboard, ticket drawer, copilot, /build page
docs/BUILD.md              the ten stage build document
deploy/                    Docker, nginx, Certbot, systemd, VPS bootstrap, backups
n8n/                       importable Gmail to TriageOps workflow
.github/workflows/ci.yml   lint, tests, frontend build
vercel.json                regions, rewrites, cron, headers
```

## Deployment

**Vercel (the live demo).** One project serves the Vite build as static files and `api/index.py` as a Python serverless function in `lhr1`, next to the Supabase database in `eu-west-2`. Pushing to `main` deploys through the Vercel Git integration once CI passes. Vercel Cron calls `/api/cron/sla` with the `CRON_SECRET` bearer token.

**Any Ubuntu 24.04 VPS.** `deploy/setup_vps.sh <domain> <email>` hardens the server (deploy user, key only SSH, UFW, fail2ban, unattended upgrades), installs Docker, issues a Let's Encrypt certificate and starts nginx, the app and certbot with Docker Compose. Nightly `pg_dump` backups keep 14 days. The runbook is in [deploy/README.md](deploy/README.md).

## Testing

```bash
ruff check triageops tests --select E,F,I --ignore E501
pytest -q
npm run build
```

The tests use a fake database and a fake Claude client, so they run offline in CI. They cover HMAC verification and the replay window, PII masking, each business rule, the fallback path, the WhatsApp handshake and signature, MCP methods and authentication, and rate limits.

## Security summary

1. Every inbound channel is authenticated. HMAC with a 300 second replay window, Meta `X-Hub-Signature-256`, bearer tokens for cron and MCP, constant time comparisons throughout.
2. Secrets, card numbers and NI numbers are masked before storage and before the model call, requester contact details are stored masked, and raw IPs are never stored.
3. Prompt injection is defended in depth. Ticket text is wrapped as untrusted data, the model can only answer through a fixed schema, and a flag routes the ticket to human review.
4. Model output never decides the outcome alone. Deterministic rules in Python have the final word on priority, team and escalation.
5. The database role `triage_api` can select, insert and update, but cannot delete anything. Row level security is on and the public `anon` role has no access.
6. Strict CSP with no inline scripts, HSTS, `X-Frame-Options DENY`, nosniff and a locked down Permissions-Policy on every response.
7. Rate limits per hashed IP and a global daily cap protect the public demo and the API budget.

The full threat model is in the Security section of [docs/BUILD.md](docs/BUILD.md).

## Credits

Built by Soumik Chatterjee with Claude Code.  
https://cosmik.work  
https://www.linkedin.com/in/soumikaispecialist/

Released under the [MIT License](LICENSE).
