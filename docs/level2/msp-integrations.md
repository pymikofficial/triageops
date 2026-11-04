# MSP integrations: RMM alerts, PSA sync and Teams cards

This level connects TriageOps to the tools an MSP runs every day. NinjaOne and Datto RMM alerts become tickets, repeat alerts collapse into one ticket, triaged tickets are created in HaloPSA or Autotask, and P1 alerts reach Microsoft Teams as an Adaptive Card.

```text
NinjaOne ─┐                                   ┌─► HaloPSA or Autotask (PSA_PROVIDER)
          ├─► n8n relay ─► /api/webhooks/rmm ─┤
Datto RMM ┘   header auth   HMAC, de-dup,     └─► Slack, Discord or Teams card (ALERT_WEBHOOK_URL)
              then signs    priority hint
```

## 1. RMM alert intake

**Endpoint.** `POST /api/webhooks/rmm`, optional query `source=ninjaone` or `source=datto`. Without `source` the vendor is detected from the shape of the body.

**Authentication.** The same timestamped HMAC as `/api/webhooks/inbound`. Send `X-TriageOps-Timestamp` (unix seconds) and `X-TriageOps-Signature: sha256=<hex>`, the HMAC SHA-256 of `{timestamp}.{raw_body}`. The key is `RMM_WEBHOOK_SECRET`, or `WEBHOOK_SECRET` when that is empty. Requests older than 300 seconds are rejected. The signature is checked over the raw bytes before anything is parsed.

Neither NinjaOne nor Datto RMM can sign a webhook. NinjaOne API webhooks and Datto Global Webhooks can send a fixed custom header, so they post to the n8n relay in `n8n/rmm-to-triageops.json`, which checks that header and then signs the alert for TriageOps. TriageOps deliberately has no static token mode, because a fixed token gives no body integrity and no replay protection.

**Accepted payloads.**

| Vendor | Shape | Raised | Cleared |
|---|---|---|---|
| NinjaOne | Activity webhook (`PUT /v2/webhook`, ideally with `expand: ["device", "organization"]`) | `activityType` `CONDITION` and `statusCode` `TRIGGERED` | `RESET` or `RESET_BY_PSA_TICKET_CALLBACK` |
| Datto RMM | Webhook template with `[variable]` placeholders (template below) | default, or `event` `triggered` | `event` `resolved` or `alert_resolved` true |
| Datto RMM | API v2 alert object (`alertUid`, `priority`, `alertSourceInfo`, `alertContext`) | `resolved` false | `resolved` true |

Other NinjaOne activities (acknowledged, disabled, non condition activities) are answered with action `ignored` and write nothing. JSON and `application/x-www-form-urlencoded` bodies are accepted. One alert per request, the relay splits a Datto `alerts` list.

**Response.** Always 200 for a handled alert, because RMMs and the relay retry any other status.

```json
{"action": "created", "ticket": {"ref": "TKT-1104-1001", "channel": "rmm", "priority": "P2", "...": "..."}, "occurrences": 1, "priority_hint": "P2", "vendor": "ninjaone"}
```

| `action` | Meaning |
|---|---|
| `created` | A new ticket was stored and triaged |
| `collapsed` | A repeat for the same device and condition joined the open ticket, `occurrences` counts it |
| `duplicate` | A retried delivery of an alert already seen. Nothing is counted twice |
| `reset` | The RMM reported the condition cleared, an `rmm_reset` event was added. The ticket stays open for a person to close |
| `ignored` | Not an alert TriageOps acts on, `reason` says why |

Errors are 401 for a bad signature, 400 for a body that is not valid JSON, 422 for a payload that is not a recognisable alert, 413 above 64 KB and 503 when the database is down.

**De-duplication.** The key is vendor, device and condition. For NinjaOne that is `deviceId` and `sourceConfigUid` (falling back to `sourceName` or `subject`). For Datto it is the device UID and `alert_type` plus `alert_category` (falling back to the `alertContext` class or the message with digits removed). A new alert with the same key within `RMM_DEDUP_MINUTES` (default 30) of the last occurrence joins the open ticket and adds an `rmm_repeat` event with the occurrence count. The window rolls from the last occurrence, so a flapping alert stays on one ticket. Resolved and failed tickets are never reused. Repeats do not run triage again, do not create another PSA ticket and do not alert again. The stored key is a SHA-256 hash, and the device and condition labels in event detail are masked like the ticket, because the timeline is shown in the dashboard.

Retried deliveries are matched on the vendor alert id. NinjaOne uses `id` plus `statusCode`, Datto uses the alert UID plus raised or resolved, because Datto sends the same UID for both.

**Priority hint.** The RMM severity becomes a hint that Claude sees in a `<priority_hint>` block outside the untrusted `<ticket>` block. The hint text is built from our own vocabulary, never from payload text, so a payload cannot use it to inject instructions.

| NinjaOne `severity` | Datto `priority` | Hint |
|---|---|---|
| CRITICAL | Critical | P1 |
| MAJOR | High | P2 |
| MODERATE | Moderate | P3 |
| MINOR, NONE | Low, Information | P4 |
| missing | missing or unexpanded | no hint |

When NinjaOne `severity` is NONE or missing, its `priority` is used instead (HIGH P2, MEDIUM P3, LOW P4). The hint is a starting point only. Claude still judges impact from the facts, and the business rules still have the final word. If Claude is unavailable, the keyword fallback uses the hint when it is more urgent than its own answer and logs a `priority_hint_fallback` rule.

**Datto RMM template.** Use this as the raised payload of a Global Webhook, with Content Type `text/plain` so the n8n relay receives the text even when a value breaks the JSON. For the resolved payload use the same body with `"event":"resolved"`, or send resolved alerts to a separate URL ending in `&event=resolved`, which the relay applies over any event value in the body.

```json
{"source":"datto_rmm","event":"triggered","alert_uid":"[alert_uid]","priority":"[alert_priority]","alert_type":"[alert_type]","alert_category":"[alert_category]","alert_message":"[alert_message]","device_uid":"[device_uid]","device_hostname":"[device_hostname]","site_uid":"[site_uid]","site_name":"[site_name]"}
```

**Try it locally.**

```bash
TS=$(date +%s)
BODY='{"id":48213377,"activityTime":1793805137.4,"deviceId":1042,"severity":"MAJOR","priority":"HIGH","activityType":"CONDITION","statusCode":"TRIGGERED","sourceConfigUid":"b7e4d1c0-2f9a-4e6b-8d35-91a0c7f2e614","sourceName":"Disk Free Space < 10%","message":"Disk C: free space is 6.2% on ACME-FS01","device":{"id":1042,"displayName":"ACME-FS01","references":{"organization":{"name":"Acme Ltd"}}}}'
SIG=$(printf '%s.%s' "$TS" "$BODY" | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" -hex | sed 's/^.* //')
curl -s "http://localhost:8000/api/webhooks/rmm?source=ninjaone" -H "Content-Type: application/json" -H "X-TriageOps-Timestamp: $TS" -H "X-TriageOps-Signature: sha256=$SIG" -d "$BODY"
```

Send it twice with a different `id` and the second answer is `collapsed` with `occurrences` 2.

## 2. PSA sync

After a ticket is triaged, `triageops/psa/sync.py` creates it in the PSA and records the external id in a `psa_synced` event. It is off by default. Set `PSA_PROVIDER` to `halopsa` or `autotask` and complete that provider's settings (see `.env.example`). If the provider is set but a setting is missing or invalid, each ticket gets a `psa_skipped` event that names what is missing.

| | HaloPSA | Autotask |
|---|---|---|
| Auth | OAuth2 client credentials at `{HALOPSA_AUTH_URL}/token`, token cached until a minute before expiry and refreshed once on a 401 | `ApiIntegrationCode`, `UserName` and `Secret` headers, zone found from `zoneInformation` unless `AUTOTASK_BASE_URL` is set |
| Create | `POST /api/Tickets` with a one item array: `summary`, `details`, `tickettype_id`, `priority_id`, `team` or `team_id`, `category_1`, `client_id`, `site_id`, `dateoccurred`, `third_party_id_string` | `POST /V1.0/Tickets`: `companyID`, `title`, `description`, `status`, `priority`, `dueDateTime` (the SLA due time), `queueID`, `ticketType`, `source`, `externalID` |
| External id | Halo ticket `id`, with a link to `/tickets?id=` | Autotask `itemId` |
| Required settings | `HALOPSA_BASE_URL`, `HALOPSA_CLIENT_ID`, `HALOPSA_CLIENT_SECRET`, `HALOPSA_TICKET_TYPE_ID` | `AUTOTASK_USERNAME`, `AUTOTASK_SECRET`, `AUTOTASK_INTEGRATION_CODE`, `AUTOTASK_COMPANY_ID` |

Priority ids differ between PSA tenants, so map them with `HALOPSA_PRIORITY_IDS` and `AUTOTASK_PRIORITY_IDS`. The description sent to the PSA holds the reference, the summary, the masked ticket body, the next steps, the SLA due time and a warning when prompt injection was suspected. The draft reply is never sent, because it waits for a person to review it.

**Retries without duplicates.** Connection failures, 429 and 503 are retried with exponential backoff from `PSA_BACKOFF_SECONDS`, honouring `Retry-After`, for at most `PSA_MAX_ATTEMPTS` attempts and within `PSA_DEADLINE_SECONDS` in total, so the serverless function stays inside its 60 second limit. A create that may have reached the PSA (a read timeout, 500, 502 or 504) is never sent again blindly. The adapter first looks the ticket up by the TriageOps reference (`third_party_id_string` in HaloPSA, `externalID` in Autotask). A match counts as success. Only when nothing is found is the create sent again. If the lookup itself fails, the `psa_failed` event says the ticket may already exist, so a person checks before retrying. Validation errors and bad credentials are not retried.

The PSA can never fail a ticket. Every outcome is an event (`psa_synced`, `psa_skipped` or `psa_failed`), error summaries are capped at 200 characters with secrets removed, and demo portal tickets are never sent to the PSA.

## 3. Microsoft Teams Adaptive Cards

`ALERT_WEBHOOK_URL` now gets a body in the format of its host.

| Host | Body |
|---|---|
| `*.webhook.office.com`, `*.logic.azure.com`, `*.powerplatform.com` (Teams connectors and Workflows) | A message with one Adaptive Card 1.4 attachment |
| `hooks.slack.com` | `{"text": ...}` with Slack escaping |
| `discord.com/api/webhooks/` | `{"content": ...}` with mentions switched off |
| anything else | `{"text": ..., "content": ...}` as before |

The card shows the priority, reference and customer as a coloured title (red for P1, amber for P2), the summary, and facts for priority, team, status, SLA due, channel and reason. Set `PUBLIC_APP_URL` to add an Open ticket button. Every ticket field is escaped, so a subject such as `[click](https://example.com)` shows as text and cannot become a link. The ticket body and the draft reply are never included. Set `ALERT_FORMAT` to `teams`, `slack`, `discord` or `generic` to override the detection, for example for a government cloud Teams host.

## 4. n8n relay for RMM alerts

`n8n/rmm-to-triageops.json` has six nodes.

1. **RMM Webhook.** POST on `/webhook/triageops-rmm` with header auth, answering at once so the RMM never times out.
2. **Sign RMM alerts.** Detects the vendor with the same rules as the server, splits Datto `alerts` lists, merges `event` and `priority` query parameters into a Datto body, rescues a Datto body whose values broke the JSON, then signs each alert.
3. **POST to TriageOps.** Raw body, both signature headers, three tries five seconds apart, so a retry stays inside the 300 second window.
4. **Build fallback message** and **Post fallback alert.** Any failure goes to `TRIAGEOPS_FALLBACK_WEBHOOK_URL` (Teams, Slack or Discord), so an alert is never silently lost.
5. **Setup notes.** Every step below.

**Setup.**

1. Set the n8n environment variables `TRIAGEOPS_RMM_WEBHOOK_SECRET` (the server's `RMM_WEBHOOK_SECRET`, or set `TRIAGEOPS_WEBHOOK_SECRET` to the server's `WEBHOOK_SECRET`), `TRIAGEOPS_FALLBACK_WEBHOOK_URL`, `NODE_FUNCTION_ALLOW_BUILTIN=crypto` and `N8N_BLOCK_ENV_ACCESS_IN_NODE=false`, then restart n8n.
2. Create a Header Auth credential for the webhook node, for example `X-RMM-Secret` with a random value of at least 32 characters. It must not be the TriageOps secret.
3. Import the workflow, change the TriageOps host if it is not `triageops.vercel.app`, activate it and copy the production URL.
4. NinjaOne. Call `PUT /v2/webhook` with that URL plus `?source=ninjaone`, `activities` `{"CONDITION": ["TRIGGERED", "RESET"]}`, `expand` `["device", "organization"]` and the header in `headers`. Console notification channels cannot send headers, so use the API webhook.
5. Datto RMM. Add a Global Webhook with that URL plus `?source=datto`, the same custom header, the priorities to send and the template above. Monitor level webhooks cannot send headers.
6. Send a test alert from each RMM. A Datto test send creates a real alert that must be closed by hand.

## 5. Database

`supabase/migrations/004_rmm_channel.sql` adds the `rmm` channel to the `tickets` check constraint and two partial expression indexes on `ticket_events` for the de-duplication and retry lookups. **It has not been applied.** Apply it before deploying this code, otherwise every RMM alert fails the channel check and the endpoint answers 503. No grants change, because every new record is a `ticket_events` row.

## 6. Tests

All tests run offline. HTTP is faked with `httpx.MockTransport` and the database with the in-memory store.

| File | Covers |
|---|---|
| `test_rmm_parsers.py` | Every NinjaOne and Datto shape in `tests/fixtures/rmm/`, severity mapping, keys, time parsing, limits, bad input |
| `test_rmm_intake.py` | The 30 minute rolling window, occurrence counts, retried deliveries, resets, resolved and failed tickets |
| `test_rmm_api.py` | Every signature failure, secret precedence, body formats, every action end to end |
| `test_priority_hint.py` | The hint block outside `<ticket>`, escaping, and the fallback rule |
| `test_psa_*.py` | Settings, the retry policy, both adapters against the stateful mock PSA server in `tests/mock_psa.py`, and sync through the pipeline |
| `test_alert_formats.py` | Host detection, every body format, card structure, escaping and size |
| `test_n8n_workflows.py` | Both workflows' structure, and the relay's JavaScript run in Node, signing exactly like the server |

## 7. Known limits

1. Two first deliveries of the same condition that arrive at the same instant can both create a ticket, because the pooler runs in autocommit mode and a transaction lock would not hold. The window for this is a few milliseconds, since the de-duplication key is written before triage starts.
2. A repeat that is more severe than the first alert is recorded in its `rmm_repeat` event but does not raise the ticket priority.
3. Several vendor details could not be confirmed from the vendor sites, which were not reachable while this was built. They are the Datto `[alert_priority]` variable, whether HaloPSA returns `third_party_id_string` in ticket searches, whether Autotask allows queries on `externalID`, and both PSAs' picklist ids. Each one fails safely. Check them on a test tenant before going live.
