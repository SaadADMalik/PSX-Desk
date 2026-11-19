# PSX Agent Desk

A multi-agent research desk and demo trading account for the Pakistan Stock
Exchange, built from protocols up to learn how enterprise multi-agent systems work.
It runs entirely on free tiers (Groq, local Docker).

**Demo money, real prices.** Trades go into a demo account that starts with
Rs 1,000,000 of test money and fills at real PSX prices with realistic fees. Nothing
touches a real brokerage. Output is research, not licensed financial advice.

For the full picture, read [`docs/project-overview.md`](docs/project-overview.md).
Decisions are recorded in [`docs/adr/`](docs/adr/).

## Status

| Slice | What | State |
|---|---|---|
| 0 | Infrastructure, LLM gateway, PSX adapter, single-agent brief | **done** |
| 1 | MCP server, A2A agents, orchestrator, one trace across all | **done** |
| 2 | Event bus, durable workflows, idempotency, dead-letter queue | **done** |
| 3 | Debate agents, evidence verifier, risk gate, memory | **done** |
| 4 | Paper broker, approvals, per-service DB roles, signed tokens, audit log | **done** |
| + | Web app (Next.js + shadcn/ui), live updates, advisor chat with official-source search and streaming replies | **done** |

## Run it

Needs Docker, [uv](https://docs.astral.sh/uv/), Node 22 (only to develop the web
app outside Docker), and a free Groq API key.

```bash
cp .env.example .env    # then set GROQ_API_KEY, the passwords and the dashboard secret
uv sync
uv run desk-keygen           # one Ed25519 key pair per service, into .keys/ (untracked)
docker compose up -d --build
```

Then open **http://127.0.0.1:8700** and sign in as `operator` (or `DASHBOARD_USER`)
with `DASHBOARD_PASSWORD` from `.env`.

From the terminal:

```bash
uv run desk-brief LUCK       # multi-agent: specialists -> bull/bear -> judge -> checks
uv run baseline-brief LUCK   # the same question answered by one agent with every tool
```

If `GROQ_API_KEY` is already set in your shell environment, Compose uses that
value when the line in `.env` is empty. To work on the web app with hot reload:
`cd web && npm install && npm run dev` (it talks to the API on 127.0.0.1:8701).

## The app

| Page | What it is for |
|---|---|
| **Today** | Demo account value, today's move, cash, and suggested trades waiting for your approval. Your watchlist with live prices, 30-day sparklines and the desk's latest call, with Research and Trade buttons on each row |
| **Advisor** | A chat that acts as your research advisor. It reads your account, the desk's research, live prices and announcements, and searches the web **only on official sources** (PSX, regulators, the company's own site). Replies stream as they are written, with each step shown live |
| **Demo account** | Account value over time against the same money put into the KSE-100, holdings, every trade with its fees, and every suggestion with its outcome |
| **Stocks** | Any PSX ticker: price chart (1M to 1Y), the desk's view, the case for and against, what each analyst found, your position, a Trade button |
| **Under the hood** | The agents and their versions, today's free AI budget per model, and the tamper-evident audit record |

Prices tick in place (server-sent events). When a brief finishes or a trade fills,
open screens refresh themselves and a toast says what happened.

**Who can do what.** The desk and the advisor can *suggest* a demo trade. Only you,
signed in to the app, can approve one; you can also trade directly. The advisor can
never approve or execute anything.

## How a multi-agent brief flows

```
scheduler / desk-brief --> orchestrator: run in Postgres (lease, checkpoints, outbox)
   |
   |  1. specialists, in parallel (A2A)                     MCP tools they may call
   |     technical-agent    (gpt-oss-20b) --------------->  get_price_summary
   |     fundamentals-agent (gpt-oss-20b) --------------->  get_valuation
   |     news-agent         (Qwen)        --------------->  get_announcements   (untrusted text)
   |        every result is stored in the evidence table by market-data, by id
   |
   |  verify: every number in every point must be in the evidence it cites (code)
   |
   |  2. debate, in parallel and blind to each other (A2A, debate-agent)
   |     bull_case (Qwen)            bear_case (gpt-oss-20b)
   |  verify the arguments (code)
   |
   |  3. judgement (gpt-oss-120b) + prior views recalled from memory (MCP, pgvector)
   |  verify the brief (code); if unusable, send the rejections back to the judge
   |
   |  4. risk gate (code): veto if illiquid; cap confidence on volatility, drawdown,
   |     fresh announcements, stale data, missing analyses, a one-sided debate
   |  5. remember the brief (memory MCP), best effort
   |  6. if actionable: portfolio-agent (A2A) -> intent (model) -> shares (code)
   |     -> broker.propose_order (MCP)  =>  a PENDING paper trade
   v
result + brief.completed (outbox) --> desk.briefs

you --> web app (127.0.0.1:8700) --> app API --broker.decide--> re-price, re-check limits
                                                              --> paper fill + audit row
```

Every arrow above carries a signed token (Ed25519 JWT, 60 s, one audience), and
every receiver checks it against [`config/access.toml`](config/access.toml). The
portfolio agent can propose but never approve; only the app's API, driven by a
signed-in human, holds `broker:decide`.

## How an advisor reply flows

```
browser --POST /api/chat/stream--> app API --A2A SendStreamingMessage--> advisor-agent
   ^                                                                       |
   |   server-sent events: tool steps, then the answer as it is written    |  agent loop
   +-----------------------------------------------------------------------+  (streams)
                                                                           |
      tools, each a scoped MCP or HTTP call:                               v
        desk_view, research ........ orchestrator      LLM gateway --stream--> Groq
        market_snapshot, news, quotes  market-data     (retries and fallback happen
        my_portfolio, suggest_trade .. broker           only before the first word)
        past_views ................... memory
        search_official, read_official  official-web  <- allow-list enforced in code
```

- **Official sources only, by construction.** The official-web MCP server drops any
  search result or redirect outside [`config/official_sources.toml`](config/official_sources.toml)
  before it is fetched. Besides a free metasearch library, it finds pages through
  each site's own sitemap, so it still works when search engines throttle it, and it
  reads PDFs with their table rows intact.
- **Streaming is end to end.** Groq streams to the gateway, the gateway to the agent
  loop, the advisor reports progress as A2A status updates, and the app relays them
  to the browser, which types the text out smoothly.
- See ADRs [0011](docs/adr/0011-service-identity-and-authorisation.md) (identity) and
  [0012](docs/adr/0012-paper-broker-approvals-audit.md) (broker, approvals, audit).

- The orchestrator makes no model calls. It coordinates and checks; agents think.
- Evidence travels by id. The verifier fetches the facts from the service that
  computed them, so no agent can alter a number on its way to the check.
- Bull, bear and judge prefer different model families, so they do not share one
  model's blind spots or one model's token budget.
- Partial failure is normal: a missing analysis or a missing case lowers the
  confidence ceiling; if every specialist fails, the run is parked.
- See ADRs [0009](docs/adr/0009-debate-verifier-risk-gate.md) (debate, verifier,
  risk gate) and [0010](docs/adr/0010-evidence-store-and-memory.md) (evidence store, memory).

## What to look at

| Where | What it shows |
|---|---|
| Terminal output | The brief, the risk verdict, both debate cases, each claim with its evidence ids, and what the verifier rejected |
| `runs/*.json` | Full run: brief, risk flags, verification at every hand-off, reports, cases, prior views, models used, evidence, tokens |
| http://127.0.0.1:16686 (Jaeger) | One trace per brief across orchestrator, both agents, the MCP server and the gateway |
| http://127.0.0.1:8200/v1/agents | What the orchestrator discovered: skill, agent, version, URL |
| http://127.0.0.1:8401/.well-known/agent-card.json | The technical agent's A2A card (8402 fundamentals, 8403 news, 8404 debate: three skills on one card) |
| http://127.0.0.1:8300/mcp | The MCP server; point the MCP Inspector at it (`npx @modelcontextprotocol/inspector`) |
| http://127.0.0.1:8100/v1/budget | Requests and tokens used today per model, circuit state |
| `llm_calls` table | Every model call: caller, model, outcome, tokens, latency, cache hits, errors |
| http://127.0.0.1:8200/v1/runs | Recent workflow runs; `/v1/runs/{id}` shows each step and attempt |
| `docker compose exec orchestrator desk-events` | Latest events on every stream and its dead-letter stream |
| `workflow_runs`, `workflow_steps`, `outbox`, `processed_events` | Durable state, checkpoints, unpublished events, the dedupe inbox |
| `evidence`, `memories` tables | Every computed fact by id; every remembered brief with its embedding |
| http://127.0.0.1:8500/mcp | The memory MCP server (`recall`, `remember`) |
| http://127.0.0.1:8700 | The app (see above). User `operator` (or `DASHBOARD_USER`), password `DASHBOARD_PASSWORD` from `.env` |
| http://127.0.0.1:8701/api/... | The app's JSON API and live event stream (`/api/events`), behind the same sign-in |
| http://127.0.0.1:8406/.well-known/agent-card.json | The advisor's A2A card: `capabilities.streaming` is true |
| http://127.0.0.1:8800/mcp | The official-web MCP server (`search_official`, `read_official`) |
| [`config/official_sources.toml`](config/official_sources.toml) | The only domains the advisor may search or read; add a company's site when you add it to the watchlist |
| [`config/access.toml`](config/access.toml) | Who may call what, for every service, in one file |
| `proposals`, `fills`, `cash_ledger`, `audit_log` tables | Paper trading state (written only by the `broker` role) and the tamper-evident audit trail |

```bash
docker compose exec postgres psql -U psx_admin -d psx -c "select caller, model, outcome, prompt_tokens, completion_tokens, latency_ms from llm_calls order by id desc limit 20"
```

## Layout

```
libs/contracts          typed, versioned messages (chat, evidence, analysis, debate, review, events)
libs/agentkit           agent loop, gateway client, structured output, tracing,
                        MCP toolset (mcp_tools.py), A2A server + directory (a2a.py),
                        shared specialist routine (analyst.py)
libs/marketdata         PSX source, last-good snapshots, indicators and news summaries in code
libs/eventbus           Redis Streams publish/consume, reclaim, dead-letter; desk-events CLI
mcp_servers/market_data MCP server: read-only tools, the evidence store (evidence_store.py)
mcp_servers/memory      MCP server: recall/remember over pgvector, local embeddings
mcp_servers/broker      MCP server: the paper broker (proposals, decisions, fills, audit)
agents/technical        A2A specialist: price history only
agents/fundamentals     A2A specialist: valuation only
agents/news             A2A specialist: announcements (untrusted text), one read-only tool
agents/debate           A2A service with three skills: bull_case, bear_case, judgement
mcp_servers/official_web MCP server: web search and page/PDF reading, official sources only;
                        sitemap discovery (discovery.py)
agents/portfolio        A2A agent: brief -> paper-trade proposal (proposes, never approves)
agents/advisor          A2A agent: the chat advisor; streams progress; suggests, never approves
agents/baseline         one agent with every tool, answering the same question alone
services/orchestrator   desk.py (A2A/MCP clients), workflow.py (the brief, as durable steps),
                        engine.py (leases, parking), store.py, verify.py, risk.py, API, CLI
services/scheduler      fires brief.requested for the watchlist after market close
services/llm_gateway    the only service holding the Groq key; /v1/chat and /v1/chat/stream
services/dashboard      the app's API: sessions, JSON endpoints, live event stream, chat relay;
                        the only holder of broker:decide
web                     the app: Next.js 16 + shadcn/ui (Base UI), Geist, Recharts, SWR
config/access.toml      the permission matrix every receiver enforces
config/official_sources.toml  the domains the advisor may search and read
libs/agentkit/auth.py   signed service tokens, verification, HTTP and MCP guards, desk-keygen
scripts/security_drills.py    live checks that refused calls really are refused
db/migrate.sh           applies db/migrations once each; roles and least-privilege grants
tests                   205 tests; no network or API key needed (Postgres tests use
                        Docker and skip without it)
```

Ports (all bound to 127.0.0.1): 8700 app, 8701 app API, 8100 gateway, 8200
orchestrator, 8300 market-data, 8401-8406 agents, 8500 memory, 8600 broker,
8800 official-web, 16686 Jaeger. 8080 is left alone.

## Things to try (failure drills)

- **Agent down:** `docker compose stop fundamentals-agent`, then `uv run desk-brief ENGRO`.
  The brief is still written from technical analysis, lists the missing analysis in
  `data_gaps`, and carries lower confidence. Then `docker compose start fundamentals-agent`.
- **Cache:** run the same brief twice. The second desk run spends 0 tokens; the
  ledger shows `cached` rows.
- **MCP host guard:** a request to the MCP server with an unexpected `Host` header
  gets HTTP 421 (DNS-rebinding protection).
- **Least privilege:** the gateway's DB role cannot delete its own ledger:
  `docker compose exec postgres sh -c 'PGPASSWORD="$GATEWAY_DB_PASSWORD" psql -h localhost -U gateway -d psx -c "delete from llm_calls"'`
  fails with `permission denied`.
- **Limiter down:** `docker compose stop redis`. The gateway fails closed (HTTP 500)
  rather than calling Groq unmetered, and recovers once `docker compose start redis`
  runs. Returning a clean 503 here is a known gap.
- **Stale data:** if PSX is unreachable after a successful fetch, briefs still run and
  list the stale data in `data_gaps` (covered by unit tests; not yet drilled live).

Slice 2 drills (all run live; results in [ADR 0008](docs/adr/0008-events-durability-idempotency.md)):

- **Scheduler fires twice:** run `docker compose exec scheduler desk-schedule --symbols LUCK,ENGRO`
  twice. Four events arrive and two runs exist, keyed `daily:<date>:<symbol>`.
- **Crash mid-run:** `docker compose exec redis redis-cli SET drill:crash_after_step technical_analysis`,
  then `uv run desk-brief HBL`. The orchestrator exits right after checkpointing the
  technical step; Docker restarts it; the run resumes on attempt 2 without redoing
  technical analysis.
- **Poison event:** `docker compose exec redis redis-cli XADD desk.requests '*' event 'not json'`,
  then `docker compose exec orchestrator desk-events`. It is in `desk.requests.dlq`
  with the reason, and nothing is left pending.
- **Client retry:** POST `/v1/briefs` twice with the same `Idempotency-Key` header.
  Both return the same `run_id` with `"duplicate": "idempotency_key"`.
- **One trace across the bus:** open a scheduled run's trace in Jaeger. Its root is
  `scheduler.fire`, and it continues through the orchestrator, both agents, MCP
  and the gateway.

Slice 3 drills (results in [ADR 0009](docs/adr/0009-debate-verifier-risk-gate.md) and
[ADR 0010](docs/adr/0010-evidence-store-and-memory.md)):

- **Read the verifier:** every brief ends with `Verifier: n/m claims verified` and lists
  each rejected claim with the numbers it could not find. `runs/*.json` keeps the checks
  for every hand-off (`upstream_checks`).
- **Memory:** run `uv run desk-brief HBL` twice. The second brief shows `Prior view …`;
  only the judge makes a model call, because the prior view changed its prompt.
- **Specialist down:** `docker compose stop news-agent`, then a brief. It is written,
  the risk gate says `CAUTION` with `missing_analysis`, and confidence is capped at 0.6.
- **Memory down:** `docker compose stop memory`, then a brief. It is written; the
  `remember` step is recorded as failed with the cause.
- **Crash mid-debate:** `docker compose exec redis redis-cli SET drill:crash_after_step bull_case`,
  then a brief. Attempt 2 resumes at the judge without redoing any analysis or case.
- **Least privilege, again:** each table owner can append but not rewrite history.
  `docker compose exec postgres psql -h localhost -U market_data -d psx -c "delete from evidence"`
  fails with `permission denied`, as do `update evidence`, `select from llm_calls`, and
  the memory role deleting from `memories`.

Slice 4 drills (run live; 13 of 13 pass):

```bash
uv run python scripts/security_drills.py                                       # as operator
docker compose exec -T news-agent python - --as-service < scripts/security_drills.py
docker compose exec -T portfolio-agent python - --as-service < scripts/security_drills.py
```

- **No token, no work:** an agent, the gateway and an MCP server each refuse a call
  without a token (401), before any tool is listed.
- **Wrong caller, no work:** the operator's own token cannot assign work to an agent
  (403); only the orchestrator can. A token minted for one service is refused by another.
- **Prompt injection has no path:** the news agent, which reads untrusted company text,
  cannot read prices and cannot reach the broker at all.
- **Propose is not approve:** the portfolio agent can read the portfolio and propose,
  but `decide` is refused; so is writing memory. The operator CLI cannot approve either.
- **Audit:** the hash chain verifies; editing any row breaks it (unit-tested, and the
  database refuses UPDATE, DELETE and TRUNCATE on `audit_log` outright).
- **Separation of duties:** in the app, approve a suggested trade: the broker re-prices
  it, refuses if the price moved more than 3% or a limit would now break, and records
  who proposed and who approved as different identities.
- **Official sources only:** ask the advisor about a company's fees. Its steps show
  which official pages it searched and read; a non-official URL is refused by the
  server, whatever the model asks for.

## What Slice 0 taught

- **A volatile field kills caching.** Tool results first included `fetched_at`;
  every run had a different prompt, so nothing after step one was a cache hit.
- **Models fail in ways the request doesn't.** gpt-oss tried to submit its answer by
  calling a tool named `JSON`, and Groq rejected the whole turn. That needs its own
  error class (422, correct and retry once), not "bad request".
- **Citations are not grounding.** See [ADR 0005](docs/adr/0005-evidence-only-numbers.md).
- **Tracing finds the real bottleneck.** The first PSX fetch for a symbol took
  about 9 s, longer than all model calls in the run combined.

## What Slice 1 taught

- **Trace context travels differently per protocol.** A2A uses the HTTP `traceparent`
  header; MCP carries it in the JSON-RPC `_meta` field so it also works over stdio.
  The MCP client uses `httpx2`, so header instrumentation alone would have broken the trace.
- **A2A turns integers into floats** (protobuf `Value`), so ids must never be
  recomputed from data that crossed the wire.
- **Parallel agents, serial bottleneck.** Both specialists share one MCP server whose
  PSX limiter allows one fetch at a time; one tool call queued 12.5 s on a cold cache.
- **Narrow context helped quality.** Specialists with one tool and a tight prompt did
  not invent a company name or peer comparisons; the baseline did. It cost 1.7x
  the tokens. See [ADR 0007](docs/adr/0007-orchestration-and-partial-failure.md).

## What Slice 2 taught

- **Fakes hide timeouts.** redis-py 8 defaults `socket_timeout` to 5 s, the same as
  the consumer's blocking read, so every idle poll failed in Docker while every
  fakeredis test passed. The consumer now refuses a block time that isn't below the
  client's socket timeout.
- **Two kinds of duplicate need two defences.** The scheduler firing twice produced
  four *distinct* events; the inbox (`event_id`) let all four through and the business
  key (`daily:<date>:<symbol>`) collapsed them to two runs.
- **Resume time is the lease.** After the drill crash, Docker restarted the
  orchestrator within seconds, but the run waited about 27 s for its 30 s lease to expire.
  A shorter lease resumes faster but risks fencing out a slow, healthy worker.
- **The redone step is the in-flight one.** Fundamentals ran twice (it was running at
  the crash), which is why every step must be safe to repeat; here it cost 0 tokens
  because the gateway cache served the repeat.

## What Slice 3 taught

- **The provider's real limits are in its error bodies.** Qwen's 1,000 output
  tokens/min, charged at the *requested* `max_tokens`, appears in no header or doc,
  only in a 429. Logging 429 bodies found it; an OTPM bucket in the gateway fixed it.
- **Not every 429 means "wait".** "Request too large" can never succeed on that model,
  so retrying it wasted a minute per call. Classify errors before retrying.
- **Your counters and the provider's can disagree.** Groq's daily window rolls;
  ours reset at UTC midnight. Failover on the 429 made it harmless.
- **Most schema repairs were mechanical.** Seven points against a limit of six cost a
  whole extra model call. Fixing that in code (`cap_claims`) removed the repairs.
- **Real data has naming quirks.** PSX renames symbols during ex-dividend periods
  (`HUBCXD`); four fundamentals analyses failed until the adapter learned it.
- **The verifier catches real hallucinations and makes real mistakes.** It caught a
  wrong SMA percentage, and falsely rejected "D-95" quoted from a title until it also
  searched the evidence's text. Rejections are recorded, so the error rate is measurable.
- **More parallelism made it slower.** Two briefs at once queued on the same
  per-minute buckets; one at a time was faster per brief.

## What Slice 4 and the app taught

- **Least privilege needs a test, not a review.** The broker's database role had every
  table grant but no `USAGE` on the schema, so it could not see its own tables. Only a
  test that ran as that role caught it.
- **Typed tool results or nothing.** An MCP tool returning a bare `dict` came back as
  text, and the dashboard crashed on a key that was not there. Pydantic return types
  give structured output; untyped returns do not.
- **Streaming changes where retries can happen.** Before the first word, the gateway
  can still retry or fall back to another model. After it, a retry would show the user
  the same words twice, so a broken stream is an error.
- **Rules in prompts are requests; rules in code are guarantees.** "Use official
  sources only" holds because the server drops everything else, not because the
  prompt asks nicely.
- **Free search engines throttle bots.** The first real question failed on 429s and
  empty pages. Sites' own sitemaps are free, unthrottled and official by construction.
- **Models guess tickers.** Asked about Meezan, the model searched for MEZB and MEZL.
  Resolving the company from names in the question, in code, fixed it.
- **Know what each setting is fixed at.** Next.js bakes rewrite targets in at build
  time, and its proxy cut every request off at 30 s, too short for the advisor.
- **Your ports are not the only ones.** Port 8080 belonged to a local Apache; the app
  moved to 8700 rather than touch it.

## Tests

```bash
uv run pytest
uv run ruff check .
```
