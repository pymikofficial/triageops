# Analyst console

Claude proposes, a human decides. The analyst console lets a signed in analyst approve the draft reply, override the priority or team, resolve a ticket and reopen it. Every action is written to the ticket timeline as an audit event, and every override is exported next to Claude's original decision as a gold label for evaluation. The public dashboard stays read only.

## What the owner must do

1. Add `ADMIN_TOKEN` to the Vercel project (Production, and Preview if wanted). Use a long random value, for example the output of `openssl rand -hex 32`. Until it is set, every `/api/admin` route answers 503 and the console says it is not configured.
2. Redeploy so the function picks up the new variable.
3. Share the token only with analysts. Rotating it means changing the variable and redeploying. Every open analyst session then ends at its next action.

No database migration is needed. The new events use the existing `ticket_events` table, whose `type` column is free text, and `resolved` is already an allowed ticket status.

## Authentication

| Control | Detail | Where |
|---|---|---|
| One shared token | `ADMIN_TOKEN` environment variable, sent as `Authorization: Bearer <token>` | `triageops/admin_auth.py` |
| Constant time comparison | Reuses `bearer_token_matches`, which calls `hmac.compare_digest` and never accepts an unset secret | `triageops/security.py` |
| Failed attempt lockout | 5 failures from one hashed client IP within 15 minutes lock that client out for the rest of the window, even with the right token, with `Retry-After` | `FailedAttemptLimiter` |
| Successes are free | Only failures are counted, so a busy analyst is never locked out | `FailedAttemptLimiter` |
| Auth before parsing | The token is checked before the body is validated, so an anonymous caller learns nothing about the request shape | router dependency |
| Browser storage | The token is kept in `sessionStorage` after the sign in screen, so it is cleared when the tab closes and is never a cookie, which also rules out CSRF | `src/lib/admin.js` |
| Session end | Any 401 clears the stored token. The drawer undoes the optimistic change and asks the analyst to sign in again | `src/lib/admin.js`, `TicketDrawer.jsx` |

The lockout count is held in memory per serverless instance, the same accepted risk as the copilot limit. A determined attacker spread across many cold instances gets more guesses, which is why the token must be long and random. A 64 character hex token cannot be found by guessing at any realistic rate.

## Endpoints

Every route is under `/api/admin` and needs the bearer token. Ticket keys accept a reference such as `TKT-1104-1001` or a UUID. Each action returns the updated ticket and its full timeline, the same shape as `GET /api/tickets/{key}`.

| Method and path | Body | Effect | Event |
|---|---|---|---|
| `POST /api/admin/login` | none | Checks the token for the sign in screen | none |
| `POST /api/admin/tickets/{key}/reply` | `{"text"}` up to 6,000 characters | Records the approved reply. The draft is kept unchanged. Nothing is sent | `reply_approved` with `text`, `edited`, `delivery`, `by` |
| `POST /api/admin/tickets/{key}/override` | `{"priority"?, "team"?, "reason"}`, reason 3 to 500 characters and required | Changes priority, team or both. A priority change moves the SLA deadline to the new priority's hours from arrival | one `human_override` per changed field, with `field`, `from`, `to`, `reason`, `by`, and `sla_due_at` for priority |
| `POST /api/admin/tickets/{key}/resolve` | `{"note"}` 3 to 2,000 characters | Status becomes `resolved`. The SLA clock stops | `resolved` with `note`, `from`, `to`, `sla` (met, breached or not set), `by` |
| `POST /api/admin/tickets/{key}/reopen` | `{"note"?}` optional | Status returns to what it was before resolution, or `triaged` if that is unknown | `reopened` with `from`, `to`, `by` and the note if given |
| `GET /api/admin/overrides.csv` | none | Downloads every override as CSV | none |

Rules that keep the record clean.

1. A resolved ticket refuses approve, override and resolve with 409 until it is reopened. Only a resolved ticket can be reopened.
2. An override that changes nothing returns 422, so every exported row is a real disagreement with the previous value.
3. Analyst text goes through the same masking as ticket text before storage, because the timeline is public. A password typed into a reply or a card number in a reason is stored as `[REDACTED:...]`.
4. When a priority change moves the deadline into the future, an earlier breach flag is cleared so the sweep can judge the ticket again. If the new deadline is also past, the breach stands. The original `sla_breached` event stays on the timeline either way.

## Overrides export

`GET /api/admin/overrides.csv` returns one row per `human_override` event, oldest first, capped at 10,000 rows. The SQL is in `triageops/admin_store.py` and joins each override to the first `triaged` event of its ticket, which holds Claude's own proposal before the business rules ran.

| Column | Meaning |
|---|---|
| `ticket_ref`, `channel`, `ticket_created_at` | The ticket |
| `overridden_at`, `field`, `reason` | The human decision |
| `claude_value` | What Claude proposed for the overridden field |
| `from_value`, `to_value` | The value before and after the override. `from_value` can differ from `claude_value` when a business rule or an earlier override changed it first |
| `claude_model`, `claude_category`, `claude_priority`, `claude_team`, `claude_confidence` | Claude's full original decision. `rules-fallback` as the model means Claude was unavailable |
| `subject`, `body` | The masked ticket text, so the file is a ready made evaluation set |

Cells that start with `=`, `+`, `-`, `@`, a tab or a carriage return are prefixed with an apostrophe, because ticket text is attacker controlled and a spreadsheet would otherwise run it as a formula.

## Stats and the SLA sweep

`OPEN_STATUSES` is `new`, `triaged` and `escalated`. `GET /api/stats` counts only those as `open` and `open_p1`, and `mark_sla_breaches` only checks and flags those. A resolved ticket therefore leaves the open counts and is skipped by the sweep with no code change. New tests pin both queries to `OPEN_STATUSES` so a later edit cannot quietly bring resolved tickets back. A reopened ticket returns to an open status and the sweep watches it again from its existing deadline.

## Frontend

1. `/analyst` is the sign in screen. After a successful check it stores the token and returns to the dashboard. When signed in it shows the console summary, the overrides export and sign out. The header link reads "Analyst sign in" or "Analyst console".
2. The ticket drawer shows an "Analyst actions" section only when signed in. Approve reply opens the draft in an editable text area. Change priority or team needs a reason. Resolve needs a note. A resolved ticket offers Reopen instead.
3. Updates are optimistic. The drawer applies the change and adds the timeline entry at once, marked "(saving)", then swaps in the server's ticket and timeline. If the server refuses, the drawer goes back to its previous state, the form reopens with the analyst's text kept, and the reason is shown. A poll that started before an action finished is ignored, so the timeline never jumps backwards.
4. After each action the dashboard queue and stats refresh straight away through the router outlet context, rather than waiting for the next 10 second poll.
5. The CSV is fetched with the bearer header and handed to the browser as a download, because a plain link cannot carry the header.

## Files

| File | Change |
|---|---|
| `triageops/admin.py` | New. Router, request models, actions, CSV writer |
| `triageops/admin_auth.py` | New. Token check and failed attempt lockout |
| `triageops/admin_store.py` | New. Overrides export query |
| `triageops/app.py` | Two lines. Imports and includes the router |
| `triageops/config.py` | Two lines. `ADMIN_TOKEN` setting |
| `.env.example` | `ADMIN_TOKEN` entry |
| `src/lib/admin.js`, `src/lib/adminOptimistic.js` | New. Session, API calls, optimistic updates |
| `src/components/AnalystActions.jsx`, `src/pages/AnalystPage.jsx`, `src/styles/analyst.css` | New. Drawer actions, sign in page, styles |
| `src/components/TicketDrawer.jsx` | Renders the actions, runs optimistic updates, marks pending events |
| `src/App.jsx`, `src/components/Header.jsx`, `src/pages/Dashboard.jsx` | Route, header link, outlet context |
| `src/lib/api.js` | `apiFetch` accepts extra headers |
| `src/lib/events.js` | Labels for the new events, readable booleans, dates and "Not set" |
| `tests/admin_fakes.py`, `tests/test_admin_*.py` | New. 57 tests |

## Testing

```bash
ruff check triageops tests evals --select E,F,I --ignore E501
pytest -q
npm run build
```

The new tests run offline like the rest. `tests/admin_fakes.py` builds on `FakeStore`, runs tickets through the real pipeline with a fake Claude so genuine `triaged` events exist, and adds a recording connection that checks the SQL of the real store functions without a database.

1. `test_admin_auth.py`. Right and wrong tokens, wrong schemes, an unset token, the constant time call, every admin route refusing anonymous callers, auth before body validation, the lockout, per client lockouts, the window releasing, successes not counting, and public ticket routes refusing writes.
2. `test_admin_actions.py`. Each action and its exact audit event, validation of every field, masking of analyst text, the SLA move on a priority change and the breach rule, 404 and 409 cases, reopen restoring the previous status, the public timeline telling the whole story, and the stats and sweep queries using only open statuses.
3. `test_admin_export.py`. Auth, the empty file, rows next to Claude's decision including a ticket the ransomware rule changed before the analyst stepped in, formula defusing, and the export query.

The console was also checked in a headless browser against the app running on the in-memory fake store. Sign in with a wrong and a right token, an override with and without a reason, approving an edited reply, resolving, reopening, the CSV download, and an expired token rolling back an optimistic change all behaved as described.
