# Evaluation harness

**Goal.** Prove triage quality with numbers. Every prompt or model change can now be scored against the same 60 labelled tickets, and P1 recall is the release gate. Missing a ransomware ticket costs far more than over-triaging a printer, so the target is 100%.

```text
evals/dataset.jsonl ─► harness.run_cases ─► triage.process_ticket (unchanged) ─► scoring.score ─► latest.json, latest.md ─► /evals page
                        fake store, offline retriever      Claude call + business rules
```

## What was built

| Piece | Where |
|---|---|
| 60 labelled UK MSP tickets | `evals/dataset.jsonl` |
| Runner (CLI) | `evals/run_eval.py` |
| Offline pipeline, fake database, retriever, dataset loader | `evals/harness.py` |
| Metrics, pure functions | `evals/scoring.py` |
| Markdown report | `evals/report.py` |
| Price table for cost per ticket | `evals/pricing.py` |
| Committed results | `evals/results/latest.json`, `evals/results/latest.md` |
| Manual GitHub Actions run | `.github/workflows/evals.yml` |
| Results page with nav link | `src/pages/EvalsPage.jsx`, `src/pages/EvalsPage.css`, route `/evals` |
| Tests | `tests/test_evals.py` (34 tests, no network) |

No schema change was needed, so there is no new migration.

## The dataset

Each line is one ticket with `id`, `channel`, `customer`, `requester`, `subject`, `body`, `expected_category`, `expected_priority`, `expected_team`, `injection`, `tags` and a one line `rationale` for the label.

| Coverage | Count |
|---|---|
| Tickets | 60 |
| P1, P2, P3, P4 | 14, 16, 17, 13 |
| P1 kinds | ransomware (4, one from an RMM webhook), confirmed breach or exposure (3), site or tenant wide outage (4), payment fraud (2), data loss (1) |
| Prompt injection attempts | 9 in 9 styles: direct override, fake system header, tag break-out, embedded JSON, claimed authority, role play, prompt extraction, hidden HTML comment, and an instruction to downgrade real ransomware |
| Tricky cases | calm tone but critical (2), angry tone but low impact (3), mixed issues (2), and a legitimate "developer mode" request that must not be flagged |
| Categories, teams, channels | all nine, all five, all four |

Labels follow the system prompt's priority matrix and team routing, and tests check they never contradict the business rules (for example, a Security label is never below P2). Ticket text is fictional, uses the Ofcom drama phone range and follows the house style.

## How a run works

`triage.process_ticket` runs exactly as in production: masking, the forced tool use Claude call, output repair, the business rules and the SLA. Three dependencies are swapped so the only network call is to Claude.

1. **Database.** `EvalStore` replaces the `store` functions in memory behind a lock, so four tickets can run at once.
2. **Retrieval.** `LexicalRetriever` ranks the twelve seeded runbooks by TF-IDF cosine similarity in place of gte-small embeddings in Supabase. Claude still sees three relevant articles, but retrieval quality differs from production, so the eval measures classification, not retrieval.
3. **Alerts.** `ALERT_WEBHOOK_URL` is cleared for the run.

Every stand-in is restored afterwards, and a test fails if the embeddings client is called.

## Running it

```bash
python evals/run_eval.py                      # Claude on every ticket, needs ANTHROPIC_API_KEY
python evals/run_eval.py --limit 10           # first ten tickets
python evals/run_eval.py --model claude-haiku-5-5 --concurrency 4
python evals/run_eval.py --offline            # keyword fallback only, no key and no network
```

Options are `--limit`, `--model` (default `CLAUDE_TRIAGE_MODEL`, then `claude-haiku-5-5`), `--concurrency` (default 4), `--dataset`, `--out-dir`, `--name` and `--no-gate`. The exit code is 0 when the gate passes, 1 when it fails and 2 when the run cannot start, for example with no API key.

In GitHub, open Actions, choose **Evals** and press **Run workflow**. It reads the `ANTHROPIC_API_KEY` repository secret, writes the Markdown report to the job summary and uploads both result files as an artifact, even when the gate fails.

## Metrics

| Metric | Definition |
|---|---|
| P1 recall (gate) | Expected P1 tickets whose final priority is P1. A crashed ticket counts as a miss. Target 100% |
| P1 precision | Final P1s that were labelled P1 |
| Priority accuracy | Final priority equals the label, with a 4 by 5 confusion matrix (expected rows, final columns plus "no result"), per class recall and precision, and under and over triage counts |
| Category and team accuracy | Final value equals the label, with per label counts |
| Injection detection rate | Labelled attempts where the final ticket is flagged |
| Injection false positives | Clean tickets that were flagged, as a count and a rate |
| Latency | Mean, p50, p95 and max of end to end pipeline time per ticket. p95 uses the nearest rank method. Model call latency is reported separately for tickets Claude triaged |
| Tokens and cost | Mean input and output tokens per billed ticket, and cost per ticket, per 1,000 tickets and per run from `evals/pricing.py` |
| Fallbacks | Tickets where Claude failed and keyword rules answered, with the reason |

"Final" always means after the business rules, because that is what reaches the queue. Each result also stores what the model proposed and which rules changed it, so a failure shows whether the model or the rules were wrong.

## Results so far

No Anthropic API key was available in the build environment, so **Claude has not been evaluated yet**. The committed `latest.json` is an `--offline` run of the keyword fallback, and the `/evals` page says so in a banner. It is still a useful baseline, because it is the floor the service drops to when Claude is down.

| Measure | Keyword fallback |
|---|---|
| P1 recall (gate) | 50.0%, 7 of 14, gate failed |
| Priority accuracy | 50.0% |
| Category accuracy | 65.0% |
| Team accuracy | 75.0% |
| Injection detection | 22.2%, 2 of 9, with 1 false positive |

The fallback misses calm or indirect P1s (odd file extensions, a forwarding rule, fake bank details) and most injection styles that avoid the regex wording. Those are exactly the cases Claude is there to catch, and the first real run will show whether it does.

The one injection false positive is the "developer mode" ticket (`EV-060`). The deterministic marker check in `security.py` flags it whatever the model says, so even a perfect model scores one false positive. A test pins this down, and it is worth fixing in the rules rather than in the dataset.

## Things to know

1. **Forced tool use and newer models.** The pipeline forces `tool_choice` to `record_triage`. Claude Haiku 5.5 accepts this, but Claude Sonnet 5.5, Claude Opus 5.5 and Claude Fable 5.1 return HTTP 400 for a forced tool choice. Running the eval with `--model claude-sonnet-5-5` today would send every ticket to the keyword fallback. The report and the page call this out as fallbacks with the reason, and the CLI prints a warning. Supporting those models means changing `triage.py` to `tool_choice` auto with a strict tool, which is out of scope here.
2. **Prices.** `evals/pricing.py` holds list prices checked on 4 November 2026 (Haiku 5.5 is $0.10 input and $0.50 output per million tokens). An unlisted model is reported without a cost.
3. **Prompt version.** Each run records a hash of the system prompt and tool schema, so two runs can be compared only when they used the same prompt.
4. **Stale results.** A test fails if `latest.json` was not produced from the current `dataset.jsonl`, so the page never shows results for an older dataset.

## Owner actions

1. Add the `ANTHROPIC_API_KEY` repository secret (Settings, Secrets and variables, Actions).
2. Run the **Evals** workflow from the Actions tab.
3. Download the artifact, copy `latest.json` and `latest.md` into `evals/results/` and commit them. The `/evals` page then shows Claude's numbers in place of the fallback baseline.
