# PSX Desk

[![ci](https://github.com/SaadADMalik/PSX-Desk/actions/workflows/ci.yml/badge.svg)](https://github.com/SaadADMalik/PSX-Desk/actions/workflows/ci.yml)
[![e2e](https://github.com/SaadADMalik/PSX-Desk/actions/workflows/e2e.yml/badge.svg)](https://github.com/SaadADMalik/PSX-Desk/actions/workflows/e2e.yml)
[![security](https://github.com/SaadADMalik/PSX-Desk/actions/workflows/security.yml/badge.svg)](https://github.com/SaadADMalik/PSX-Desk/actions/workflows/security.yml)

**An AI research desk and demo trading account for the Pakistan Stock Exchange.**

A team of AI agents researches PSX stocks every trading day, argues the case for and
against each one, and proposes sized trades that you approve. An advisor chat answers
questions about stocks, companies and your account, using only official sources, and
writes its answer as you watch.

**Demo money, real prices.** Trades go into a demo account that starts with
Rs 1,000,000 and fills at live PSX prices with realistic fees. Nothing touches a real
brokerage. Output is research, not licensed financial advice.

---

## What it does

| | |
|---|---|
| **Covers the whole market** | All ~750 traded PSX securities, with live prices, day and 1-year change, P/E, dividend yield and traded value. Search any stock by ticker or company name from anywhere (`Ctrl+K`). |
| **Researches your watchlist daily** | Each user keeps their own watchlist: star any stock to follow it. After the market closes, specialist agents analyse every followed stock from three angles: price trend, valuation and company announcements. A bull and a bear argue the case, and a head-of-research agent writes the brief. Any stock can be researched on demand. |
| **Shows its reasoning** | Every brief shows the stance and confidence, key points and risks, both sides of the debate, what each analyst found, and how the desk reached its decision. Every number in it has been checked against the data. |
| **Alerts you when a stock falls** | Set an alert on any stock: a percent fall, or for a stock you hold, rupees lost from your average cost. Give it a number of days, or leave it open until you cancel. It fires once when the price crosses your line, re-arms only after a real recovery, and asks the desk for fresh research on that stock. |
| **Proposes, never acts** | When the desk feels strongly, it proposes a sized demo trade. Nothing executes until you approve it. At approval the broker re-prices the trade and re-checks every limit. |
| **Advises from official sources** | The advisor reads your account, your watchlist and alerts, the desk's research, live prices and announcements, and searches the web **only** on official sites: PSX, Pakistani regulators (SBP, SECP and others) and the companies' own domains. It cites what it read. |
| **Keeps a tamper-evident record** | Every proposal, approval and fill goes into a hash-chained audit log that the app verifies continuously. |

### The app

| Screen | What it is for |
|---|---|
| **Today** | Account value, today's move, cash, trades awaiting approval, your watchlist with live prices, 30-day sparklines and the desk's latest call, and your price alerts |
| **Stocks** | The whole market: search on every keystroke, sector filter, sortable columns, gainers, losers and most active; star a stock to add it to your watchlist |
| **Stock page** | Price chart, the desk's view and how it was decided, the case for and against, each analyst's findings, your position and its alerts, and Watch, Set alert, Research and Trade buttons |
| **Advisor** | Chat that streams its answer, showing each step live (*Searched official sources…*, *Read meezanbank.com*, *Checked live prices*) |
| **Demo account** | Value over time against the same money in the KSE-100, holdings, every fill with fees, every proposal and its outcome |
| **Under the hood** | Every agent and its status, today's AI budget per model, and the audit chain's verification |

Prices update in place over server-sent events. When research finishes or a trade
fills, open screens refresh themselves.

---

## Why you can trust what it says

LLMs write fluently and are sometimes confidently wrong. The desk is built so the
model chooses the words, and code owns the facts and the decisions.

- **Every number is checked.** A claim verifier compares each figure in a brief with
  the evidence it cites, and drops any figure it can't find there. In a live run it
  caught a model writing "10.0% above the 50-day average" when the data said 3.3%.
- **Evidence can't be altered.** Facts are stored by the service that computed them
  and passed between agents by ID. The verifier fetches the originals, so no agent can
  change a number on its way to the check.
- **Decision rules live in code.** A starting stance is computed from the analysts'
  views and the strength of each side's case. The judge may depart from it only with
  an evidence-backed reason, which the stock page shows. Case strength is computed
  from weighted arguments and counter-points, not from a number the model makes up.
- **Trade sizes are arithmetic.** The model picks the intention (buy, how strongly).
  Code calculates the shares from cash, price and limits.
- **The broker trusts nobody's numbers.** It fetches its own price and refuses stale
  data. It checks position limits when a trade is proposed and again when it fills,
  and refuses a fill if the price has moved more than 3%.
- **Official sources only, by construction.** The web-search service drops any result
  or redirect outside the allow-list before fetching it. The rule holds whatever the
  model asks for.

---

## Architecture

```
                                 ┌───────────────── web app (Next.js) ─────────────────┐
                                 │  live prices · search · approvals · advisor chat    │
                                 └──────────────────────────┬──────────────────────────┘
                                                            │ session + CSRF guard
                                                 ┌──────────▼──────────┐
       scheduler ── Redis Streams ──►            │      app API        │  only holder of
       (after close)                             │     (dashboard)     │  broker:decide
                                                 └──┬───────────────┬──┘
                                            A2A     │               │  A2A (streaming)
                                 ┌──────────────────▼──┐        ┌───▼─────────────┐
                                 │    orchestrator     │        │  advisor agent  │
                                 │ durable workflows,  │        │ tools + official│
                                 │ verifier, risk gate │        │ web search      │
                                 └──┬──────────────────┘        └───┬─────────────┘
                     A2A (parallel) │                               │ MCP
     ┌──────────────┬───────────────┼──────────────┬────────────┐  │
     ▼              ▼               ▼              ▼            ▼  ▼
 technical    fundamentals        news          debate       portfolio      MCP servers:
  agent          agent           agent      bull·bear·judge    agent        market-data · broker
     └──────────────┴───────────────┴──────────────┴────────────┘          memory (pgvector)
                    every model call ──► LLM gateway ──► LLM provider       official-web
```

**Services:** 6 agent services carrying 8 agent roles (technical, fundamentals and news
analysts; bull, bear and judge; portfolio; advisor), the orchestrator, a scheduler, an LLM gateway, 4 MCP servers,
the app API and the web app, plus Postgres with pgvector, Redis and Jaeger, all in
Docker Compose. Every service runs as its own container with its own identity.

### Multi-agent communication

| Layer | Protocol | Used for |
|---|---|---|
| Agent ↔ agent | **A2A** | Delegating a task to an agent that thinks for itself: analysis, debate, judgement, trade proposals, advisor chat |
| Agent ↔ tool | **MCP** | Data and actions with typed inputs and outputs: prices, valuations, announcements, evidence, portfolio, orders, memory, official-web search |
| Service ↔ service events | **Redis Streams** | Research requests and "brief ready" events, with consumer groups, reclaim and a dead-letter stream |
| App → browser | **SSE** | Live prices, refresh signals, and the advisor's tool steps and text as they happen |

- **Discovery by skill.** Each agent publishes an A2A agent card listing its skills.
  The orchestrator looks agents up by skill (`technical_analysis`, `bull_case`,
  `judgement`, …) instead of by address, and records any agent it can't reach.
- **Tasks, not calls.** A2A tasks move through states (submitted, working,
  completed, failed) and can stream progress. The advisor's steps reach the browser as
  A2A status updates, relayed over SSE.
- **Parallel and blind.** The three analysts run at the same time. The bull and bear
  argue independently, on different model families, so they don't share one model's
  blind spots or its rate limits.
- **Partial failure is normal.** A missing analysis or a missing case lowers the
  brief's confidence ceiling and is reported, without failing the run. If every
  analyst fails, the run is parked and retried.
- **The orchestrator never calls a model.** It coordinates and checks; agents think.

### How a research brief is produced

```
request (scheduler, app or CLI) ─► orchestrator opens a run (lease, checkpoints, outbox)
  1. technical · fundamentals · news, in parallel (A2A) ── each calls MCP tools;
     every fact is stored in the evidence table, by ID
     └─ verify: every number in every point must exist in its cited evidence
  2. bull case · bear case, in parallel and blind (A2A)  └─ verify the arguments
  3. judge: desk lean (code) + both cases + prior views from memory (pgvector)
     └─ verify the brief; a rejected brief goes back to the judge once with the reasons
  4. risk gate (code): veto if illiquid; cap confidence for volatility, drawdown, fresh
     announcements, stale data, missing analyses or a one-sided debate
  5. remember the brief (memory)
  6. if actionable: portfolio agent ─► intent (model) ─► shares (code) ─► PENDING proposal
  ─► result + brief.completed, written in one transaction (outbox) ─► Redis Streams

you approve in the app ─► broker re-prices, re-checks limits ─► fill + audit entry
```

### How an advisor reply is produced

```
browser ─POST /api/chat/stream─► app API ─A2A streaming─► advisor agent (agent loop)
   ▲                                                          │
   └──── SSE: tool steps, then the answer as it is written ───┤
                                                              ▼
  desk_view, research .......... orchestrator        LLM gateway ─stream─► LLM
  market_snapshot, quotes, news  market-data         retries and fallback happen
  my_portfolio, suggest_trade .. broker              only before the first word
  past_views ................... memory
  search_official, read_official  official-web  ◄── allow-list enforced in code
```

The advisor can *suggest* a demo trade, and it lands in your approval queue. It can
never approve or execute anything.

---

## Reliability

- **One gateway for every model call.** It enforces per-model limits (requests and
  tokens, per minute and per day, including output tokens per minute), retries,
  falls back to another model, and runs a circuit breaker and a response cache.
  Interactive chat gets priority over background research. Any single call is capped
  at 7,000 tokens, and agent loops trim older tool results to stay under it.
- **Crash-safe workflows.** Each research run is leased, every step is checkpointed,
  and the result and its event are committed together (outbox). A crashed worker's
  run resumes from its last checkpoint. A run that keeps failing stops after a fixed
  number of attempts and announces `brief.failed`.
- **Idempotent everywhere.** Requests carry idempotency keys, and scheduled research
  is keyed by date and symbol. Consumers deduplicate events. Firing twice produces one run.
- **Graceful data fallback.** If PSX is unreachable, research runs on the last good
  snapshot and says so in the brief. The broker never fills at a stale price.
- **Fast reads.** The whole market loads in one request and is cached briefly, so
  search runs in the browser on every keystroke. Holdings are priced in one batched
  request, so account pages respond in about 0.1 s.

## Security

- **Service identity.** Every call between services carries an Ed25519-signed JWT that
  lives 60 seconds and is valid only for the receiving service.
- **Permissions decided by the receiver.** One policy file,
  [`config/access.toml`](config/access.toml), states who may call what. A caller can't
  grant itself a scope, and a caller that isn't listed is refused even with a valid token.
- **Separation of duties.** The portfolio agent can propose but not approve. Only the
  app's API, acting for a signed-in human, holds `broker:decide`. Proposer and approver
  are recorded as different identities.
- **Prompt injection has nowhere to go.** The news agent reads untrusted company text,
  but it can't read prices or reach the broker. The worst a hijacked agent can do is
  add a pending proposal for a human to reject.
- **Tamper-evident audit.** Each audit entry carries a hash of the one before. The
  database refuses UPDATE, DELETE and TRUNCATE on the log, and the app verifies the
  chain continuously.
- **Least-privilege data access.** Each service has its own Postgres role and can
  append to the tables it owns but not rewrite history.
- **Hardened web surface.** Signed, HTTP-only, SameSite=strict session cookies; a CSRF
  header on every write; the LLM key held by the gateway alone.
- **13 security drills** ([`scripts/security_drills.py`](scripts/security_drills.py))
  run against the live stack in CI and check that each forbidden call really is refused.

## Observability

- **One trace per request** (OpenTelemetry → Jaeger), across the app, orchestrator,
  agents, MCP servers and gateway, including across the event bus.
- **Every model call recorded** in Postgres: caller, model, outcome, tokens, latency,
  cache hits and errors. This feeds the live per-model budget on *Under the hood*.
- **Durable state you can query:** workflow runs and steps, the outbox, the
  dedupe inbox, the evidence store, memories, and the trading and audit tables.

## Quality

Every pull request runs:

| Workflow | Checks |
|---|---|
| **ci** | ruff, then 225 Python tests (unit, plus Postgres integration via testcontainers); web eslint, type check and production build |
| **e2e** | Playwright against the built app with recorded API responses (sign-in, search, filters, stock page, advisor streaming, demo account); then the **full stack in Docker** with throwaway secrets, running the security drills and browser smoke tests against the real system |
| **security** | CodeQL (Python, TypeScript), gitleaks secret scan, dependency review; Dependabot opens grouped update PRs weekly |

---

## Run it

Requires Docker, [uv](https://docs.astral.sh/uv/), and an API key for an
OpenAI-compatible LLM provider. Node 22 is needed only to work on the web app outside Docker.

```bash
cp .env.example .env         # set the LLM API key, the passwords and the session secret
uv sync
uv run desk-keygen           # one Ed25519 key pair per service, into .keys/ (git-ignored)
docker compose up -d --build
```

Open **http://127.0.0.1:8700** and sign in as `operator` (or `DASHBOARD_USER`) with
`DASHBOARD_PASSWORD` from `.env`.

From the terminal:

```bash
uv run desk-brief LUCK       # full multi-agent brief, with the verifier's report
uv run baseline-brief LUCK   # the same question answered by a single agent with every tool
```

Web app with hot reload: `cd web && npm install && npm run dev` (it uses the API on 127.0.0.1:8701).

Tests:

```bash
uv run pytest && uv run ruff check .
cd web && npm run lint && npm run typecheck && npm run build && npx playwright test
```

### Configuration

| File | Controls |
|---|---|
| `.env` | LLM API key, database passwords, app password and session secret (never committed) |
| [`config/access.toml`](config/access.toml) | Who may call what, for every service |
| [`config/official_sources.toml`](config/official_sources.toml) | The only domains the advisor may search or read (regulators, plus each company's site) |
| `WATCHLIST` in `docker-compose.yml` | The starting watchlist for a new account; after that, each user edits their own in the app |
| `ALERT_CHECK_S` in `docker-compose.yml` | How often price alerts are checked during market hours (default 15 minutes) |

---

## Operating it

| Where | What you see |
|---|---|
| http://127.0.0.1:8700 | The app |
| http://127.0.0.1:16686 | Jaeger: one trace per brief or chat, across every service |
| http://127.0.0.1:8200/v1/agents | What the orchestrator discovered: skill, agent, version, URL |
| http://127.0.0.1:8200/v1/runs | Recent workflow runs; `/v1/runs/{id}` shows each step and attempt |
| http://127.0.0.1:8100/v1/budget | Requests and tokens used today per model, circuit state |
| `http://127.0.0.1:84xx/.well-known/agent-card.json` | Each agent's A2A card (8401 technical, 8402 fundamentals, 8403 news, 8404 debate, 8405 portfolio, 8406 advisor) |
| http://127.0.0.1:8300/mcp | Market-data MCP server; inspect with `npx @modelcontextprotocol/inspector` (also 8500 memory, 8600 broker, 8800 official-web) |
| `docker compose exec orchestrator desk-events` | Latest events on every stream and its dead-letter stream |
| `llm_calls` table | Every model call with tokens, latency, outcome |

```bash
docker compose exec postgres psql -U psx_admin -d psx -c \
  "select caller, model, outcome, prompt_tokens, completion_tokens, latency_ms from llm_calls order by id desc limit 20"
```

Ports are all bound to 127.0.0.1: 8700 app, 8701 app API, 8100 gateway, 8200
orchestrator, 8300 market-data, 8401–8406 agents, 8500 memory, 8600 broker,
8800 official-web, 16686 Jaeger.

### Failure drills

| Drill | Command | Expected |
|---|---|---|
| Agent down | `docker compose stop fundamentals-agent`, then `uv run desk-brief ENGRO` | Brief is written, lists the missing analysis, confidence capped |
| Crash mid-run | `docker compose exec redis redis-cli SET drill:crash_after_step technical_analysis`, then a brief | Orchestrator exits after the checkpoint, restarts, resumes without redoing the step |
| Scheduler fires twice | `docker compose exec scheduler desk-schedule --symbols LUCK,ENGRO` twice | Two runs, not four |
| Poison event | `docker compose exec redis redis-cli XADD desk.requests '*' event 'not json'` | Lands in `desk.requests.dlq` with the reason |
| Client retry | POST `/v1/briefs` twice with one `Idempotency-Key` | Same `run_id`, marked duplicate |
| Limiter down | `docker compose stop redis` | Gateway fails closed; no unmetered model calls |
| Least privilege | Delete from `llm_calls` as the `gateway` role | `permission denied` |
| Security | `uv run python scripts/security_drills.py` | 13 of 13 refused as expected |

---

## Engineering decisions

Each decision has a full record in [`docs/adr/`](docs/adr/); the system is described
end to end in [`docs/project-overview.md`](docs/project-overview.md).

- **A hand-written agent loop and workflow engine.** Every step, retry and trim is
  visible, testable and traceable. Guardrails live in the services they protect, not in
  the orchestration layer, so the orchestration could be replaced without weakening them.
- **Rules in code, not prompts.** "Official sources only" holds because the server
  drops everything else. "Don't exceed limits" holds because the broker refuses.
- **Classify provider errors before retrying.** A rate limit means wait. "Request too
  large" will never succeed on that model, and a malformed tool call needs one
  correction, not a retry. Each has its own error class.
- **Streaming changes where retries can happen.** Before the first word, the gateway
  may retry or switch models. After it, a retry would repeat text, so a broken stream
  is reported as an error.
- **Stable inputs make caching work.** Volatile fields (fetch times) are kept out of
  model prompts, so repeated research costs no tokens.
- **Real market data has quirks.** PSX renames symbols during ex-dividend periods
  (`HUBCXD`) and lists non-voting, rights and non-compliant securities under suffixes.
  The data layer maps them back to the company.
- **Narrow agents beat one big agent.** Specialists with one tool and a tight prompt
  invent less than a single agent holding every tool (kept as `baseline-brief` for
  comparison), and they fail independently.

## Layout

```
agents/          technical, fundamentals, news, debate (bull · bear · judge), portfolio,
                 advisor, baseline
mcp_servers/     market_data (+ evidence store), broker, memory (pgvector), official_web
services/        orchestrator, scheduler, llm_gateway, dashboard (the app API)
libs/            contracts (typed messages), agentkit (agent loop, A2A, MCP, auth, tracing),
                 marketdata (PSX source, snapshots, indicators), eventbus (Redis Streams)
web/             Next.js app (shadcn/ui, SWR, Recharts) and Playwright tests
config/          access.toml (permissions), official_sources.toml (advisor allow-list)
db/              migrations and least-privilege roles
scripts/         security drills
tests/           Python test suite
docs/            project overview and architecture decision records
```
