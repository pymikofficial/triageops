# How TriageOps was built

TriageOps is an AI service desk for an IT managed service provider, built by Soumik Chatterjee for the AI Solutions and Automation Engineer role at BeyondStaff. This document follows the path the role describes, one stage per section. Source is at https://github.com/pymikofficial/triageops under the MIT licence.

```text
Requirement → Architecture → AI → API Integration → Backend → Database → Frontend → Automation → Deployment → Security
```

## 1. Requirement

**Problem.** The job description gives a sample brief that has to become something testable.

> "We need an AI-powered system that connects our existing platforms, processes information automatically, stores the data in a database and provides a dashboard for the team."

For an MSP that is a service desk. The platforms are the support mailbox, WhatsApp, PSA and RMM tools that send webhooks, and Slack or Teams. Processing means classifying, prioritising, routing and summarising every ticket. The database holds tickets, an audit trail and the knowledge base. The dashboard is the live queue.

**User stories.**

1. As a client user, I raise a ticket by email, WhatsApp or a web form and get a reference at once.
2. As an analyst, I see each ticket already categorised, prioritised, routed and summarised, with a draft reply grounded in the knowledge base.
3. As the duty engineer, I am alerted when a P1 arrives or a P1 or P2 breaches its SLA.
4. As the service delivery manager, I ask questions about the queue in plain English.
5. As the security lead, I know secrets are masked before any model call, every channel is authenticated, and a ticket cannot choose its own priority.

**Acceptance criteria.**

| ID | Criterion | Checked by |
|---|---|---|
| AC1 | A ticket from any channel is stored, triaged and returned in one request | `timing_ms` in the response |
| AC2 | Output always fits the taxonomy | Forced tool use, Pydantic, `check` constraints |
| AC3 | Ransomware or a confirmed breach is always P1 for Security Ops | Rule tests, `rule_applied` events |
| AC4 | No ticket is lost when Claude, embeddings or alerts fail | Fallback tests, `rules-fallback` |
| AC5 | Unsigned, tampered or replayed webhooks get 401 | HMAC tests |
| AC6 | Passwords, keys, tokens, card and NI numbers are masked before storage | Masking tests, `masked` event |
| AC7 | Provider retries never duplicate a ticket | `"duplicate": true` |
| AC8 | The copilot and MCP server cannot change data | Read-only tools, no `DELETE` grant |

Out of scope for the demo are dashboard login, two way PSA sync and sending replies. They lead the production list at the end.

## 2. Architecture

**Problem.** Four inbound channels, one AI pipeline, a store, a dashboard and alerts, hosted in the UK with near zero idle cost and a path to a client's own server.

| Decision | Alternatives considered | Why |
|---|---|---|
| FastAPI as one Vercel Python function | Next.js API routes, Express, an always-on container | Mature Anthropic SDK and Pydantic, and the same ASGI app runs on Vercel, Docker or systemd |
| Supabase Postgres 17 in London with pgvector | Firebase, Airtable, Pinecone | Relational data, SQL, row level security and vectors in one UK database, with no second vendor holding client data |
| gte-small embeddings in a Supabase Edge Function | OpenAI or Voyage embeddings | No extra key, and 384 dimensions keep the HNSW index cheap |
| Synchronous triage | Queue and worker | Interactive demo. The ticket is saved before the model call, so a timeout cannot lose it |
| Polling every 10 seconds | Supabase Realtime | Realtime needs the anon key in the browser. Polling keeps the database server-only |

```text
┌───────────────────┐ ┌───────────────────┐ ┌───────────────────┐ ┌───────────────────┐
│ Web portal        │ │ Email (Postmark)  │ │ WhatsApp (Meta)   │ │ n8n, Make, PSA    │
│ /api/demo/tickets │ │ /api/webhooks/    │ │ /api/webhooks/    │ │ /api/webhooks/    │
│ honeypot, limits  │ │ email   HMAC      │ │ whatsapp  sha256  │ │ inbound  HMAC     │
└───────────────────┘ └───────────────────┘ └───────────────────┘ └───────────────────┘
          │                     │                     │                     │
          ▼                     ▼                     ▼                     ▼
┌─────────────────────────────────────────────────────────────────────────────────────┐
│ FastAPI on Vercel lhr1   triageops/app.py, triage.py                                │
│                                                                                     │
│ 1 authenticate   2 validate   3 mask secrets   4 INSERT ticket (status new)         │
│ 5 embed + match_kb top 3   6 Claude forced tool use   7 business rules              │
│ 8 SLA + UPDATE ticket   9 alert if P1 or escalated   every step → events            │
└─────────────────────────────────────────────────────────────────────────────────────┘
          │                     │                     │                     │
          ▼                     ▼                     ▼                     ▼
┌───────────────────┐ ┌───────────────────┐ ┌───────────────────┐ ┌───────────────────┐
│ Supabase Postgres │ │ Edge Function     │ │ Claude API        │ │ Slack, Discord    │
│ 17, eu-west-2     │ │ embed             │ │ Haiku 5.5 triage  │ │ or Teams          │
│ tickets, events,  │ │ gte-small         │ │ Sonnet 5.5 agent  │ │ incoming webhook  │
│ kb_articles       │ │ 384 dimensions    │ └───────────────────┘ └───────────────────┘
│ pgvector, RLS     │ └───────────────────┘
└───────────────────┘
          ▲
          │
┌─────────────────────────────────────────────────────────────────────────────────────┐
│ READ PATHS (all through the API, the browser never touches the database)            │
│                                                                                     │
│ Dashboard     GET /api/stats, /api/tickets, /api/tickets/{id}  (polls 10 s)         │
│ Copilot       POST /api/chat → Sonnet agent loop → tools.py (read-only)             │
│ MCP clients   POST /api/mcp → JSON-RPC tools/call → tools.py (read-only)            │
│ Vercel Cron   GET /api/cron/sla (bearer) → breach sweep → events, alerts            │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

**Request flow for a ticket.**

1. The route in `app.py` authenticates the request, `channels.py` maps the provider payload to one `InboundTicket`, and `schemas.py` enforces limits (subject 3 to 200 characters, body 10 to 4,000, request 64 KB).
2. A repeated provider id (Postmark `MessageID`, WhatsApp message id, webhook `external_id`) returns the original ticket.
3. `mask_text` replaces secrets with `[REDACTED:type]` and the requester is stored masked (`+44 7*** ***218`). The ticket is inserted as `new` before any external call.
4. `rag.search` embeds the masked text and `match_kb` returns the three closest articles.
5. Claude triages through forced tool use, `rules.py` applies the business rules and SLA, and the ticket is updated to `triaged` or `escalated`.
6. P1 or escalated tickets fire the alert webhook. Every step writes a `ticket_events` row.

**Data flow for RAG.** Twelve MSP runbooks in `kb_seed.py` (thirty since Level 2, see below) (`vpn-error-809`, `ransomware-first-response`, `phishing-link-clicked` and others) are embedded in one batch the first time `GET /api/health` finds the table empty, inside `pg_try_advisory_xact_lock` with `on conflict (slug) do nothing`, so concurrent cold starts seed once. At triage time the top three matches by cosine similarity go to Claude inside `<knowledge_base>` tags, and only slugs that were actually retrieved are stored in `kb_refs`, so an invented citation never reaches the dashboard.

**Verify.** `GET /api/health` returns `"db":true`, `"ai":true`, `"kb_articles":12` (30 since Level 2). The ticket drawer shows the events in pipeline order.

## 3. AI

**Problem.** The model must return the same structure every time, stay in the taxonomy, ground replies in the knowledge base, ignore instructions hidden in tickets, and never be a single point of failure.

**Forced tool use.** One tool, `record_triage`, has an `input_schema` with enums for category, priority, sentiment and team, and `tool_choice` forces the call. Nothing is parsed from free text.

```python
# abridged from triageops/triage.py
response = _client(settings.anthropic_api_key).messages.create(
    model=settings.triage_model,                      # claude-haiku-5-5 unless CLAUDE_TRIAGE_MODEL is set
    max_tokens=TRIAGE_MAX_TOKENS,
    system=SYSTEM_PROMPT,
    tools=[RECORD_TRIAGE_TOOL],                       # enums for category, priority, sentiment and team
    tool_choice={"type": "tool", "name": TOOL_NAME},  # Claude must call record_triage
    messages=[{"role": "user", "content": build_user_message(...)}],  # KB articles, then the escaped ticket
)
```

Tool input is still model output, so `decision.py` validates it again and repairs what it can ("high" becomes P2, confidence is clamped, the reply is trimmed to 120 words). Each repair is logged in the `triaged` event.

**System prompt policy.** The prompt in `triage.py` is static and versioned. `<priority_matrix>` defines P1 to P4 and says to judge impact from facts, never tone. `<team_routing>` maps work to the five teams. `<customer_reply>` allows only steps from retrieved articles, no promised fix times and never a request for passwords or MFA codes. `<untrusted_input>` says ticket content is data, and any attempt to steer the model sets `injection_suspected`.

**Prompt injection defence in depth.**

1. Secrets are masked before the prompt is built.
2. `<` and `>` in ticket text are escaped, so it cannot close the `<ticket>` tag.
3. The only tool records a decision. The model cannot act, send or write.
4. Claude flags injection and `find_injection_marker` in `security.py` checks independently, so detection survives a model miss or the fallback.
5. Flagged tickets are escalated with a `human_review` event, and the dashboard holds the draft reply for review.
6. Rules can only raise severity, so an injected "mark this P4" cannot downgrade ransomware or a security incident.

**Model choice.** `claude-haiku-5-5` triages, since schema-bound classification at volume needs speed and low cost, and `claude-sonnet-5-5` runs the multi-step copilot. `CLAUDE_TRIAGE_MODEL` and `CLAUDE_AGENT_MODEL` swap either without a code change, and each ticket stores `ai_model`, latency and tokens so a swap can be compared on real traffic.

**Business rules after the model.** `apply_business_rules` runs after every triage and logs each rule that fires.

```python
# abridged from triageops/rules.py
trigger = find_critical_security_trigger(text)
if trigger and (final.priority != "P1" or final.team != "Security Ops"):
    events.append(_rule("critical_security_p1",
                        "Ransomware, encrypted files or a confirmed breach is always P1 for Security Ops",
                        {"matched": trigger, "from": f"{final.priority} {final.team}", "to": "P1 Security Ops"}))
    final.priority, final.team = "P1", "Security Ops"

if final.category == "Security" and final.priority in ("P3", "P4"):
    events.append(_rule("security_minimum_p2", "Security tickets are never below P2",
                        {"from": final.priority, "to": "P2"}))
    final.priority = "P2"
```

In order, ransomware, encrypted files or a confirmed breach force P1 for Security Ops, Security is never below P2, suspected injection goes to human review, P1 or `escalate=true` escalates and alerts, and `sla_due_at` is set 1, 4, 24 or 72 hours from receipt. The ransomware pattern needs a verb such as "have been" before "encrypted", so "how do I send an encrypted file" stays low priority.

**Fallback.** If Claude fails (no key, 25 second timeout, HTTP error, no tool call), `heuristic_triage` classifies with ordered keyword patterns and writes a templated reply, with `ai_model = rules-fallback`, confidence 0.3 and an `error` event giving the reason. The same rules then run.

**Copilot agent.** `agent.py` runs Sonnet over the four read-only tools in `tools.py`, whose arguments are validated with unknown fields rejected. It loops for at most five turns or 35 seconds, then sets `tool_choice` to `none` so the model must answer. The response lists `tools_used`.

**Verify.** The "Ransomware outbreak" sample returns P1, Security Ops, `escalated`. The "Prompt injection attempt" sample returns `injection_suspected` and `human_review` and is not raised to P1. With no API key every ticket shows `rules-fallback`.

## 4. API Integration

**Problem.** Every platform has its own auth scheme and payload, and each must be verified before a byte is trusted.

**Signed webhook.** `POST /api/webhooks/inbound` takes the ticket JSON with `X-TriageOps-Timestamp` (unix seconds) and `X-TriageOps-Signature: sha256=<hex>`, an HMAC SHA-256 of `{timestamp}.{raw_body}` keyed with `WEBHOOK_SECRET`.

```python
# abridged from triageops/security.py
def sign_timestamped(secret: str, timestamp: str, raw_body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + raw_body, hashlib.sha256).hexdigest()

def verify_timestamped_signature(secret, timestamp, signature, raw_body, *, now=None, window=300) -> None:
    ...
    if abs(current - int(timestamp)) > window:
        raise SignatureError("timestamp is outside the replay window")
    if not _matches(sign_timestamped(secret, timestamp, raw_body), signature):  # hmac.compare_digest
        raise SignatureError("signature does not match")
```

Signing raw bytes means field order cannot break it, and the signed timestamp with a 300 second window stops replays. Any client that can compute an HMAC can integrate.

```bash
TS=$(date +%s)
BODY='{"channel":"webhook","customer":"Pennine Logistics","subject":"VPN error 809 from home","body":"The VPN fails with error 809 from home since this morning."}'
SIG=$(printf '%s.%s' "$TS" "$BODY" | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" -hex | sed 's/^.* //')
curl -s https://<host>/api/webhooks/inbound -H "Content-Type: application/json" -H "X-TriageOps-Timestamp: $TS" -H "X-TriageOps-Signature: sha256=$SIG" -d "$BODY"
```

**Email.** `POST /api/webhooks/email` is signed the same way and takes a Postmark style body (`From`, `FromName`, `Subject`, `TextBody`, `MailboxHash`, `MessageID`). `MailboxHash` carries the client from `support+client@`.

**WhatsApp Cloud API.** Meta first calls `GET /api/webhooks/whatsapp` with `hub.mode`, `hub.verify_token` and `hub.challenge`, and the challenge is echoed as `text/plain` only when the token matches, otherwise 403. Deliveries are verified with `X-Hub-Signature-256`, an HMAC of the raw body with `WHATSAPP_APP_SECRET`. Each text message in `entry[].changes[].value.messages[]` becomes one ticket, and the endpoint answers 200 with `{"received": n}` because Meta retries failures.

**Outbound alerts.** `alerts.py` posts `{"text": ..., "content": ...}`, which Slack, Teams and Discord webhooks all accept. It requires https, times out after 5 seconds, never sends the ticket body, and a failure becomes an event, not a failed ticket.

**n8n.** `n8n/gmail-to-triageops.json` polls a Gmail label every minute, signs each email in a Code node with Node `crypto`, posts the raw body with the Gmail id as `external_id`, and sends P1 results to Slack. Make and Zapier can do the same with a code step.

**MCP server.** `POST /api/mcp` speaks MCP over streamable HTTP, stateless with JSON responses, so any serverless instance can answer. It handles `initialize`, `notifications/initialized`, `ping`, `tools/list` and `tools/call` behind `Authorization: Bearer <MCP_TOKEN>`, and exposes the copilot's four tools. Connect from Claude Code and check with `/mcp`.

```bash
claude mcp add --transport http triageops https://<host>/api/mcp --header "Authorization: Bearer <MCP_TOKEN>"
```

**Verify.** The `curl` above returns the ticket. Change one byte after signing, or reuse a timestamp over five minutes old, and it returns 401.

## 5. Backend

**Problem.** One codebase has to run as a short lived function and as a long running process, and degrade safely when a dependency is down.

**Decisions.** FastAPI with Pydantic models gives declared limits and clear 422 errors. psycopg 3 with `prepare_threshold=None` and autocommit works through the Supabase transaction pooler, where prepared statements break. All SQL lives in `store.py`, composed with `psycopg.sql` and bound parameters, rather than an ORM, so every query is reviewable and tests can replace the layer with a fake. There are five runtime dependencies, for fast cold starts.

**How it works.** `api/index.py` exports `triageops.app:app`, and `vercel.json` rewrites `/api/:path*` to it. `DATABASE_URLS` are tried in order, the first that connects is reused by the warm instance, and only the host is ever logged. Database errors return 503 and unexpected errors 500, both with generic messages and the detail kept in the server log.

**Verify.** `GET /api/stats` returns every priority, category, status and channel, zeros included. A 5,000 character body to `/api/demo/tickets` returns 422 naming the field.

## 6. Database

**Problem.** Store tickets with a full audit trail, search the knowledge base by meaning, and ensure a leaked credential cannot wipe the queue.

**Schema.** `001_init.sql` creates `tickets` (with `check` constraints mirroring the taxonomy), `ticket_events` (the timeline) and `kb_articles` (with a `vector(384)` column and an HNSW index). A partial index on open, unbreached tickets keeps the SLA sweep cheap as history grows.

```sql
create function public.match_kb(query_embedding extensions.vector(384), match_count int default 3)
returns table (id bigint, slug text, title text, category text, content text, similarity float)
language sql stable set search_path = public, extensions as $$
  select k.id, k.slug, k.title, k.category, k.content, 1 - (k.embedding <=> query_embedding) as similarity
  from public.kb_articles k
  where k.embedding is not null
  order by k.embedding <=> query_embedding
  limit least(greatest(match_count, 1), 5);
$$;
```

**Least privilege.** `002_role.sql` creates `triage_api`, the only role the backend uses, with `select, insert, update` on tickets and events, `select, insert` on articles, execute on `match_kb`, and a 15 second statement timeout. RLS is on, `anon` and `authenticated` have nothing, and no role can `DELETE`.

**Verify.** Connected as `triage_api`, `delete from tickets` fails with "permission denied".

## 7. Frontend

**Problem.** Give an analyst the queue at a glance, the full story of a ticket in one click, and a way to try the system without code.

**Decisions.** React 18, Vite and react-router with plain CSS and system fonts, since the CSP allows nothing from other origins.

**How it works.** `/` shows overview cards and the queue with filters and search, refreshing every 10 seconds while the tab is visible (`usePolling` in `src/lib/hooks.js`). `/ticket/:ref` opens a drawer with the summary, draft reply, next steps, knowledge base references, SLA countdown and event timeline. "Raise a ticket" offers the sample tickets and a hidden honeypot field. The copilot panel shows which tools answered. `/build` renders this file with `marked`, raw HTML escaped and links restricted (`src/lib/markdown.js`).

**Verify.** `npm run build` succeeds, and the console shows no CSP violations on any route.

## 8. Automation

**Problem.** The desk must keep working when nobody is watching.

1. Every channel runs the same pipeline on arrival, so nothing waits for manual sorting.
2. `GET /api/cron/sla`, behind `Authorization: Bearer <CRON_SECRET>`, marks overdue open tickets as breached, logs `sla_breached` and alerts for P1 and P2. Vercel Cron calls it daily from `vercel.json`, and the VPS kit runs `deploy/sla_cron.sh` every five minutes.
3. The n8n workflow turns a Gmail label into signed tickets, retrying at most three times five seconds apart so a retry stays inside the 300 second window.
4. `deploy/backup.sh` runs nightly on the VPS.

**Verify.** The cron endpoint returns `{"checked": n, "breached": n}` with the token and 401 without it.

## 9. Deployment

**Problem.** A hiring panel needs a working URL today, and a client needs to know it can run on their own infrastructure.

**What is live.** The demo runs on Vercel serverless functions in London (`lhr1`) with Supabase Postgres in London (`eu-west-2`), so data stays in the UK and the function sits next to the database. `vercel.json` sets the region, a 60 second limit, rewrites, cron and security headers. Secrets are Vercel environment variables.

**CI/CD.** `.github/workflows/ci.yml` runs Ruff and pytest on Python 3.12 and `npm ci` plus `npm run build` on Node 22 for every push and pull request. Deploys to `main` go through the Vercel Git integration, so no deploy token sits in GitHub. A commented step deploys to the VPS over SSH with a pinned host key.

**The VPS kit.** `deploy/` runs the same app on any Ubuntu 24.04 VPS (Hetzner, DigitalOcean). It is complete and scripted, but it is not what serves the live demo.

| Piece | What it does |
|---|---|
| DNS | A record to the server IPv4, TTL 300. The script checks it resolves before requesting a certificate |
| `setup_vps.sh <domain> <email>` | Idempotent, `set -euo pipefail`, numbered steps, safe to re-run |
| Users and SSH | Non-root `deploy` user with sudo and the SSH key, then password and root login disabled once a key is confirmed |
| Firewall | UFW allows OpenSSH, 80 and 443. fail2ban guards SSH. unattended-upgrades patches the OS |
| Docker | python:3.12-slim, non-root, read-only app container, compose with app, nginx and certbot, healthcheck on `/api/health`, app port never published |
| nginx | HTTP to HTTPS, ACME challenge, TLS 1.2 and 1.3, HSTS, gzip, security headers, SPA fallback, `/api/` proxy, 1 MB body limit, rate limits for `/api/demo` and `/api/chat` |
| SSL | Let's Encrypt through Certbot webroot, renewal checked twice a day |
| systemd | Docker-free unit with `NoNewPrivileges`, `ProtectSystem=strict` and no capabilities |
| Secrets | `/opt/triageops/.env`, mode 600, kept out of images and command lines |
| Backups | Nightly `pg_dump`, gzip, integrity check, 14 day retention, upload hook |

**Verify.** `curl -sI https://<host>` shows HSTS and CSP. `ssh root@<host>` is refused. The runbook in `deploy/README.md` takes about 15 minutes.

## 10. Security

**Problem.** A service desk holds client details and live incident reports. Every input is hostile until verified, and the AI layer adds its own attack surface.

| Threat | Control | Where in code |
|---|---|---|
| Forged or replayed webhook | HMAC over timestamp and raw body, 300 second window, constant time compare | `security.py`, `/api/webhooks/*` |
| Spoofed WhatsApp delivery | `X-Hub-Signature-256`, verify token handshake | `verify_meta_signature` |
| Unauthorised cron or MCP call | Constant time bearer check, unset secrets reject all | `bearer_token_matches` |
| Prompt injection | Escaped tagged input, record-only tool, model flag plus regex, human review | `triage.py`, `find_injection_marker`, `rules.py` |
| Model misjudges an incident | Rules set the final priority | `apply_business_rules` |
| Malformed model output | Enum schema, repair with audit, retrieved slugs only | `decision.py`, `triage.py` |
| Secrets reaching the model or logs | Masking before storage and the call, hosts but never passwords logged | `mask_text`, `db.py` |
| Tracking users by IP | Only a keyed hash of the IP is stored | `hash_client` |
| Demo abuse and API spend | Honeypot, 6 tickets per IP an hour, 300 a day, 20 chats an hour, nginx `limit_req` | `app.py`, `triageops.conf` |
| Oversized input | Pydantic limits, 64 KB app cap, 1 MB nginx cap | `schemas.py`, `app.py` |
| XSS and clickjacking | React escaping, markdown without raw HTML, CSP with no inline scripts, `X-Frame-Options DENY` | `markdown.js`, `SECURITY_HEADERS`, `vercel.json` |
| SQL injection | Bound parameters and `psycopg.sql` identifiers only | `store.py` |
| Leaked database credential | No `DELETE`, RLS, anon revoked, statement timeout | `002_role.sql` |
| Server takeover | Key only SSH, no root login, UFW, fail2ban, patching, non-root containers | `setup_vps.sh`, `docker-compose.yml` |
| Copilot or MCP misuse | Read-only tools, validated arguments, capped results | `tools.py` |

**Accepted risks for the demo.** Read endpoints are public so the panel needs no account, which is why tickets are synthetic and masked. The chat limit is held in memory per instance, while portal limits are counted in Postgres. Contact details in a ticket body are kept for call-backs. Single sign-on and configurable redaction come first in production.

**UK GDPR.** Data minimisation, UK hosting, a per ticket audit trail, and no deletion by the application role, so erasure goes through an audited admin path.

## Testing and reliability

**Tests.** `tests/` runs offline with an in-memory fake of `store.py` and a fake Claude client, locally and in CI.

1. `test_security.py`. Tampered, wrong-secret, replayed and future-dated signatures, unset secrets, Meta signatures, bearer tokens, masking, IP hashing, injection markers.
2. `test_rules.py`. Every business rule, including the encrypted-file how-to that must stay low priority and injection the model missed.
3. `test_triage.py`. Repair of model output, the forced and fenced Claude call, masking before storage and before Claude, the fallback, embedding failure, idempotent retries.
4. `test_api.py`. Every route, status code and header, the honeypot, rate limits, body caps, signatures over exact raw bytes.
5. `test_mcp.py`, `test_agent.py`, `test_content.py`. JSON-RPC errors and auth, the agent loop, the thirty articles, and the test script signing exactly like the server.
6. Level 2 adds `test_admin_*.py`, `test_evals.py`, `test_hybrid.py`, `test_kb_seed.py`, `test_ops.py`, `test_observability.py`, `test_retrieval_quality.py`, `test_rmm_*.py`, `test_psa_*.py`, `test_alert_formats.py`, `test_priority_hint.py`, `test_n8n_workflows.py` and `test_logging.py`, all offline with a fake store, a fake Claude and a mock PSA server.

**Reliability.** The ticket is written before any external call, and a crash mid-pipeline marks it `failed` rather than losing it. Every external call has a timeout (embeddings 8 seconds, Claude 25 with one retry, alerts 5, database connect 5 per URL) and degrades on its own. `GET /api/health` returns 503 when the database is down, for monitors and the Docker healthcheck.

## Level 2

Version 1.1.0 adds four pieces of work. Each was built on its own branch against the same contract, then merged one at a time into one release branch, with the lint, tests and build run after every merge. Each has a guide in `docs/level2/` that covers the owner actions, the endpoints, the files changed and the tests.

| Addition | What it does | Guide |
|---|---|---|
| Analyst console | A signed in analyst approves the draft reply, overrides the priority or team with a reason, resolves a ticket and reopens it. Every action is an audit event on the public timeline, and every override is exported next to Claude's original decision as a gold label. `/analyst`, `/api/admin/*`, `ADMIN_TOKEN` | [analyst-console.md](https://github.com/pymikofficial/triageops/blob/main/docs/level2/analyst-console.md) |
| Evaluation harness | 60 labelled UK MSP tickets and a runner that scores category, priority, team and injection detection, with latency, tokens and cost per ticket. P1 recall is the release gate. Results render at `/evals`, and a manual GitHub Actions workflow runs the set against Claude | [eval-harness.md](https://github.com/pymikofficial/triageops/blob/main/docs/level2/eval-harness.md) |
| Hybrid knowledge base and operations view | Postgres full text search fused with pgvector by reciprocal rank fusion, 18 new runbooks for 30 in all, an `/ops` page with cost, latency, model mix and SLA breaches, and JSON logs with an `X-Request-ID` on every response | [hybrid-kb-and-ops.md](https://github.com/pymikofficial/triageops/blob/main/docs/level2/hybrid-kb-and-ops.md) |
| MSP integrations | NinjaOne and Datto RMM alerts through a signed n8n relay, with repeat alerts collapsed into one ticket and the RMM severity shown to Claude as a hint, HaloPSA and Autotask ticket creation after triage with retries that never duplicate, and Microsoft Teams Adaptive Cards for alerts | [msp-integrations.md](https://github.com/pymikofficial/triageops/blob/main/docs/level2/msp-integrations.md) |

**Where the branches met.** `app.py` (three routers and the request context middleware), `App.jsx` and `Header.jsx` (three routes and links), `api.js` (extra headers and the request id), `events.js` (timeline labels for analyst, RMM and PSA events), `triage.py` (the retrieval method, the priority hint and the PSA sync step), `.env.example` and the migrations folder. Two branches had both numbered their migration 004, so hybrid search keeps `004_hybrid_kb_search.sql` and the RMM channel became `005_rmm_channel.sql`. The evaluation dataset covers the four channels people write in, and the new `rmm` channel has its own fixtures under `tests/fixtures/rmm`.

**Owner actions.** Apply `004_hybrid_kb_search.sql` and `005_rmm_channel.sql` in the Supabase SQL editor, add `ADMIN_TOKEN` and any integration variables from `.env.example` to the Vercel project, redeploy, open `/api/health` until it reports 30 knowledge base articles, and add the `ANTHROPIC_API_KEY` repository secret before running the Evals workflow. The release pull request lists every step.

**Verify.** `ruff check triageops tests evals --select E,F,I --ignore E501`, `pytest -q` and `npm run build` pass. The suite grew from 189 to 896 tests, all offline.

## What production at an MSP would add

Level 2 delivered the analyst console, one way ticket creation in HaloPSA and Autotask, NinjaOne and Datto RMM alert intake, the evaluation set with P1 recall as the gate, hybrid search and cost per ticket reporting. This is what remains.

1. Entra ID single sign-on, analyst and client roles, and per-client row level security. The analyst console uses one shared token today.
2. Two way sync with the PSA, so status and notes flow back, and a ConnectWise PSA adapter.
3. A queue and worker with retries and a dead letter queue, so intake never waits on the model.
4. Real labelled history in the evaluation set, and a scheduled run so every prompt or model change is scored without a manual step.
5. Analyst-approved replies sent through email and WhatsApp templates. Today an approved reply is recorded but not sent.
6. Knowledge base sync from IT Glue or Hudu.
7. OpenTelemetry tracing, point-in-time recovery and secrets in a vault.
8. Data processing agreements, a retention schedule, and Cyber Essentials Plus and ISO 27001 alignment.

## JD coverage map

| JD item | Where it is used in TriageOps | File or endpoint |
|---|---|---|
| Claude and Anthropic API | Triage and copilot | `triage.py`, `agent.py` |
| Claude Code | The whole build | Build log |
| Prompt engineering | Tagged system prompt, untrusted input policy | `triage.py` |
| Agents | Bounded tool-use loop | `POST /api/chat` |
| Chatbots | Copilot panel | `src/components/Copilot.jsx` |
| Python | Backend, tests, scripts | `triageops/`, `tests/` |
| REST APIs | Queue, tickets, stats, knowledge base | `app.py` |
| Webhooks | Signed inbound, email, WhatsApp, alerts | `/api/webhooks/*`, `alerts.py` |
| JSON | Requests, tool schemas, JSON-RPC, events | `RECORD_TRIAGE_TOOL`, `jsonb` columns |
| API authentication | HMAC, replay window, Meta signature, bearer tokens | `security.py` |
| Make, n8n, Zapier | Importable n8n workflow, signed webhook for any tool | `n8n/gmail-to-triageops.json` |
| React or Next.js | React 18 with Vite, no server rendering needed | `src/` |
| SQL | Constraints, partial and HNSW indexes, functions, grants | `supabase/migrations/`, `store.py` |
| PostgreSQL and Supabase | Postgres 17 in London, RLS, Edge Function | `supabase/` |
| VPS and Linux | Ubuntu 24.04 bootstrap | `deploy/setup_vps.sh` |
| SSH | Key only login, CI deploy with pinned host key | `setup_vps.sh`, `ci.yml` |
| Nginx | Reverse proxy, SPA, rate limits | `deploy/nginx/triageops.conf` |
| SSL | Let's Encrypt, TLS 1.2 and 1.3, HSTS | `docker-compose.yml` |
| DNS | A record and resolution check | `deploy/README.md` |
| Firewalls | UFW, fail2ban | `setup_vps.sh` |
| Environment variables | All configuration and secrets | `config.py`, `.env.example` |
| Server permissions | Non-root users, mode 600, systemd sandbox | `deploy/` |
| Backups | Nightly `pg_dump`, 14 day retention | `deploy/backup.sh` |
| Docker | Multi-stage image, compose | `deploy/Dockerfile` |
| CI/CD | GitHub Actions, Vercel Git deploys | `.github/workflows/ci.yml` |
| Git and GitHub | Public repository, preview deploys | https://github.com/pymikofficial/triageops |
| RAG | Runbooks grounding each triage | `rag.py` |
| Embeddings | gte-small, 384 dimensions | `supabase/functions/embed/` |
| Vector databases | pgvector, HNSW, `match_kb` | `001_init.sql` |
| MCP | Streamable HTTP server, four tools | `POST /api/mcp`, `mcp.py` |
| CRM and helpdesk | PSA webhook, Postmark email | `/api/webhooks/inbound`, `/api/webhooks/email` |
| WhatsApp | Cloud API handshake and signature | `/api/webhooks/whatsapp` |
| IT MSP context | Taxonomy, teams, SLAs, runbooks, UK SME samples | `taxonomy.py`, `kb_seed.py` |
| Security and reliability | Threat model, fallback, idempotency, tests | Section 10 |
| Documentation | This file, README, VPS runbook, four Level 2 guides | `docs/`, `README.md`, `deploy/README.md`, `docs/level2/` |
| Human in the loop | Analyst console, audit events, overrides export | `admin.py`, `/analyst` |
| Evaluation | 60 labelled tickets, P1 recall gate, cost per ticket | `evals/`, `/evals` |
| RMM and PSA | NinjaOne, Datto RMM, HaloPSA, Autotask, Teams cards | `triageops/rmm/`, `triageops/psa/`, `alert_formats.py` |
| Observability | JSON logs, request ids, operations view | `observability.py`, `ops.py`, `/ops` |

## Build log

- Started: 13:01 IST, 4 Nov 2026
- Live: 13:57 IST, 4 Nov 2026 (about 56 minutes from start to the first live deploy)
- 14:05 IST, live fix. The Supabase edge runtime caps CPU time per request, so a 12 article embedding batch failed with WORKER_RESOURCE_LIMIT. Seeding now sends 2 articles per call.
- 14:15 IST, live fix. Requesters sent as "Name <email>" were masked badly. The parser now splits the name from the address, with a new test.
- 14:20 IST, live checks passed. A forged signature, a replayed request, a missing cron token and a wrong MCP token all got 401. The WhatsApp handshake, MCP tools and the copilot answered correctly.

Parallel Claude Code agents built the backend, the frontend and the documentation and DevOps kit against one written contract for endpoints, schema, taxonomy and copy rules.

Built with Claude Code in a cloud session, directed and reviewed by Soumik Chatterjee.
