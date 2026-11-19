# PSX Agent Desk: project overview and technical implementation

## Part 1. Project overview

### What it is

PSX Agent Desk is a research desk and demo trading account for the Pakistan Stock
Exchange (PSX), run by a team of AI agents. You give it a watchlist. It researches each
stock, argues both sides, checks every number against real data, and, when the case is
strong enough, suggests a trade. You approve or reject the suggestion, or trade yourself,
and a demo account shows where the money would have gone.

**Demo money, real prices.** The account starts with Rs 1,000,000 of test money.
Trades fill at real PSX prices with realistic fees and slippage. Nothing is connected to
a brokerage, so no real money moves. Everything it produces is research, not licensed
financial advice.

### Why it exists

The project was built to learn how agents communicate and cooperate in enterprise
multi-agent systems, by building every mechanism explicitly instead of hiding them in
an agent framework. That means:

- the protocols: MCP for tools, A2A for agent-to-agent work;
- orchestration and durable workflows;
- reliability under hard rate limits;
- grounding: stopping models from inventing numbers;
- security: service identity, least privilege, human approval and an audit trail.

The constraint was **free only**: the Groq free tier for models, open-source software,
and local Docker.

### What you can do with it

| In the app | What happens |
|---|---|
| Look at **Today** | Your demo account value, today's move, cash, and trades waiting for your approval; your watchlist with live prices and the desk's latest call |
| Press **Research** on a stock | Five agents research it, argue both sides and produce a checked brief in one to three minutes |
| Open a **stock** | Price chart, the desk's view, the case for and against, what each analyst found, your position |
| **Approve or reject** a suggested trade | The broker re-checks the price and limits, then fills it in your demo account |
| **Trade** yourself | Buy or sell any PSX stock with demo money |
| Ask the **Advisor** | A chat that reads your account, the research, live prices and official sources, and answers as it types |
| Check **Demo account** | Your account value against simply holding the KSE-100, holdings, trades, fees |
| Check **Under the hood** | The agents, today's AI budget per model, and the tamper-evident audit record |

### Principles

1. **Code decides what code can decide.** Indicators, number checks, risk rules, trade
   sizing and permissions are deterministic code. Models are used where judgement is
   needed: reading, arguing, weighing, explaining.
2. **No number comes from a model.** Prices and ratios are computed in code and stored
   as evidence. Every claim must cite evidence, and a verifier checks each figure.
3. **Agents can suggest; only a person can approve.** No agent holds the permission to
   approve a trade.
4. **Rules in code, not in prompts.** "Official sources only" and "the news agent cannot
   trade" are enforced by servers, so they hold even if a model is tricked.
5. **Partial failure is normal.** A missing analysis lowers confidence; it does not stop
   the desk.

---

## Part 2. Technical implementation

### Architecture

```
                 you: web app (Next.js) on 127.0.0.1:8700
                          |
                    app API (FastAPI): sessions, JSON, live events, chat relay
                          |
  scheduler --> event bus (Redis Streams) --> orchestrator (durable workflows, Postgres)
                          |                         |  A2A (JSON-RPC over HTTP, signed tokens)
                          |      +-----------+------+------+-----------+-----------+
                          |   technical  fundamentals    news       debate      portfolio
                          |     agent       agent        agent    (bull, bear,    agent
                          |                                         judge)
                          |                                                     advisor agent
                          |         MCP (streamable HTTP, signed tokens)        (chat, A2A
                          |   market-data   memory (pgvector)   broker (paper)   streaming)
                          |                                     official-web (search, read)
                          |
   every model call --> LLM gateway --> Groq (gpt-oss-120b, gpt-oss-20b, Qwen)
   everything --> OpenTelemetry --> Jaeger;   Postgres: state, evidence, ledger, audit
```

Each box is its own container. Only 127.0.0.1 ports are published.

### How the parts talk, and why

| Interaction | Mechanism | Why this one |
|---|---|---|
| Agent to tool or data | **MCP** over streamable HTTP | A standard tool contract; the server enforces permissions on every call |
| Orchestrator or app to agent | **A2A** 1.0 (`a2a-sdk` 1.3) | Agents are discoverable by their Agent Card, work is a Task with a lifecycle, and progress can stream |
| "Something happened" | **Redis Streams** | The publisher neither knows nor waits for consumers; consumer groups, acks, dead letters |
| Anything to a model | **LLM gateway** | One place for rate limits, fallback, caching, cost and the call ledger; agents never hold the API key |

Control is **orchestration**: one coordinator owns each workflow, which keeps the flow
traceable. Events are used only at the edges (scheduling, completion notices).

### Services

| Service | Kind | Role |
|---|---|---|
| `llm-gateway` | HTTP | The only holder of the Groq key. Limits, fallback, cache, ledger, streaming |
| `orchestrator` | HTTP + worker | Runs each brief as a durable workflow. Makes no model calls itself |
| `scheduler` | worker | Requests briefs for the watchlist after market close |
| `market-data` | MCP | PSX prices, indicators, valuation, announcements, live quotes, history; the evidence store |
| `memory` | MCP | Past briefs, searchable by meaning (pgvector, local embeddings) |
| `broker` | MCP | The paper broker: proposals, decisions, fills, cash ledger, audit log |
| `official-web` | MCP | Web search and page/PDF reading, restricted to official sources |
| `technical-agent` | A2A | Reads price history only |
| `fundamentals-agent` | A2A | Reads valuation only |
| `news-agent` | A2A | Reads company announcements (untrusted text), one read-only tool |
| `debate-agent` | A2A | Three skills: `bull_case`, `bear_case`, `judgement` |
| `portfolio-agent` | A2A | Turns an actionable brief into a suggested trade |
| `advisor-agent` | A2A (streaming) | The chat advisor |
| `dashboard` | HTTP | The app's API; the only holder of `broker:decide` |
| `web` | Node | The app UI |
| `postgres`, `redis`, `jaeger` | infra | State, events and limits, traces |

### The agent loop

There is no agent framework. `libs/agentkit/loop.py` is about 200 lines:

```
send messages + tool specs to the model
if the model asks for tools: run them, append results, go again
otherwise: the text is the answer
```

The rest is guard rails:
- a step limit and a token budget per run;
- tool errors, unknown tools and bad arguments go back to the model as tool output, so
  it can recover instead of crashing;
- an invalid turn (e.g. calling a tool that does not exist, which Groq rejects with
  `tool_use_failed`) gets one corrective message.

With `on_event` set, every call streams. The loop reports text deltas, each tool it is
about to run, and `discard` when streamed text turned out to be a lead-in to tool
calls rather than the answer.

### The research workflow (one brief)

The orchestrator runs these steps, checkpointing each in Postgres:

1. **Specialists, in parallel (A2A).** Technical (gpt-oss-20b), fundamentals
   (gpt-oss-20b) and news (Qwen) each call their own MCP tools. Every tool result is
   stored as evidence with an id.
2. **Verify.** Every number in every point must appear in the evidence it cites.
3. **Debate, in parallel and blind.** The bull case (Qwen) and the bear case
   (gpt-oss-20b) are written without seeing each other. Then they are verified.
4. **Judgement** (gpt-oss-120b), with prior views recalled from memory. Verified; if the
   brief is unusable, the rejections go back to the judge once.
5. **Risk gate (code).**
   - Veto if there is no price data, or if 20-day average volume is below 50,000 shares.
   - Cap confidence at 0.6 for:
     - volatility above 50%, or a drawdown above 40%;
     - an announcement in the last 2 days;
     - stale data, missing analyses, or a one-sided debate.
   - Cap at 0.5 if fewer than 70% of claims verified.
   - Actionable only if not vetoed, not neutral, and confidence is at least 0.5.
6. **Remember** the brief in memory (best effort).
7. **Propose** (only if actionable). The portfolio agent's model writes an intent: buy,
   sell or hold, a target weight and a rationale. Code works out the shares:
   - the target weight is capped at 20% × confidence;
   - one order is capped at 10% of equity, and at the cash available.

   The broker records it as a **pending** trade.

Bull, bear and judge prefer different model families, so they don't share one model's
blind spots or one model's token budget.

### The advisor (chat)

The advisor is an A2A agent with ten tools:
- `desk_view` and `research` (orchestrator)
- `market_snapshot`, `recent_news` and `live_quotes` (market-data)
- `my_portfolio` and `suggest_trade` (broker)
- `past_views` (memory)
- `search_official` and `read_official` (official-web)

It chooses its own tools, because a conversation can go anywhere. What it can *do* is
fixed by its permissions. It can read, start research and suggest a trade. It cannot
approve or execute one.

**Official sources only.** The official-web MCP server holds the allow-list in
`config/official_sources.toml`:
- PSX;
- the regulators: SBP, SECP, the Finance Ministry, PBS, NEPRA, OGRA, PTA, CDC;
- each watchlist company's own website.

How it's enforced:
- Every search result is filtered against the list after the search engine answers.
- Every fetch, including each redirect hop, is checked *before* anything is downloaded.
- Look-alike domains, credentials in URLs, odd ports and non-HTTP schemes are refused.

How it finds pages:
- **Search engine:** the free `ddgs` metasearch library, no API key.
- **Sitemap:** each site's own page list, read from robots.txt and its sitemaps and
  cached for 12 hours. Free search engines throttle automated clients; sitemaps don't,
  and every URL in them comes from the site itself.
- **Wording gap:** a small synonym table connects question words to page words
  ("ATM" also matches "debit card"; "fees" also matches "schedule of charges").
- **Wrong tickers:** company names in the question decide which company site to
  search, because models often guess tickers wrong.

How it reads:
- HTML is stripped of scripts and navigation.
- PDFs are read in layout mode, so each table row stays on one line.
- A `focus` parameter returns only the passages that match, about 2,500 characters at most.
- A page's relevant official links (for example a fee schedule PDF) are returned too,
  so the advisor can follow them.
- All page text is marked untrusted: it's reported as data, never followed as
  instructions.

**Streaming, end to end.**

```
Groq --SSE--> gateway /v1/chat/stream --SSE--> agent loop (on_event)
  --> advisor: batches text every ~60 ms --> A2A status updates (SendStreamingMessage)
  --> app API /api/chat/stream --SSE--> browser: steps appear live, text types out
```

- **Retries only before the first word.** The gateway still retries and falls back
  between models until the first word arrives. After that it commits to that model; a
  broken stream becomes an error, because a retry would repeat text.
- **Smooth typing.** Groq writes faster than people read, so the browser reveals
  buffered text a few characters per frame, faster when there is a backlog.

### The LLM gateway

**Free-tier limits.** Each model has its own budget:
- 30 requests and 8,000 tokens per minute;
- 1,000 requests and 200,000 tokens per day;
- Qwen also has a 1,000 output-tokens-per-minute limit, found only in a 429 error body.

**What it does:**
- **Token buckets in Redis** per model, for requests and tokens per minute plus daily
  counters, reconciled with Groq's `x-ratelimit-*` headers after every call.
- **Priority lanes:**
  - interactive: may use the whole budget, and waits up to 45 s for quota;
  - pipeline: 85% of the daily budget, waits up to 90 s;
  - backtest: 50%.
- **Fallback chains:**
  - large: 120b, then Qwen, then 20b;
  - small: 20b, then Qwen, then 120b.

  Falling back also spreads load over separate budgets.
- **Error classes:**
  - 429: wait out `retry-after`, or move to the next model; "request too large" moves
    on at once.
  - Timeout or 5xx: exponential backoff with jitter, then the circuit breaker.
  - `tool_use_failed`: a 422 back to the caller, who can correct the model.
  - Other 4xx: stop.
- **Response cache** keyed on the full request, so repeats cost nothing.
- **The `llm_calls` ledger:** caller (the *proven* identity), model, outcome, tokens,
  latency, cache hits.
- **Per-call cap** of about 7,000 tokens, so no request can be too large for the window.

### Reliability and durability

- **Durable workflows.** Runs and steps live in Postgres:
  - A worker holds a 30-second lease on a run.
  - After a crash, another worker resumes from the last checkpoint.
  - Finished steps are never redone, and each step is safe to repeat.
- **At-least-once events, handled idempotently:**
  - a transactional outbox, so a state change and its event can't disagree;
  - a `processed_events` inbox keyed by event id;
  - business keys such as `daily:<date>:<symbol>`, so two scheduler firings make one run;
  - `Idempotency-Key` on the HTTP API.
- **Dead-letter streams** for poison events, with the reason.
- **Graceful degradation.** A specialist down means a brief written without it, with
  lower confidence. Memory down means the brief is still written. PSX unreachable means
  last-good data, marked stale.

### Grounding: keeping numbers honest

- Indicators (returns, moving averages, RSI, volatility, drawdown, 52-week range) and
  valuation are computed in code from PSX data and stored as **evidence** records with ids.
- Agents pass evidence ids, not numbers. The verifier fetches the evidence from the
  service that computed it, so nothing can alter a figure on its way to the check.
- Every claim is checked number by number; rejections are recorded with the figure that
  wasn't found.
- Memory is a poisoning risk, so only the orchestrator writes it, only after the
  verifier and the risk gate. Recalled views are context, never citable evidence.

### Security

- **Service identity.** Every service has an Ed25519 key pair (`uv run desk-keygen`,
  stored in `.keys/`, untracked). Each call carries a self-signed JWT:
  - 60-second lifetime, and an audience set to the receiver, so it can't be replayed
    against another service;
  - the caller's identity, checked against its public key.
- **Authorisation.** Receivers check the caller against one permission matrix,
  `config/access.toml`, so a caller cannot grant itself a scope. Examples:
  - the news agent has only `news:read`;
  - the portfolio agent has `broker:propose` but not `broker:decide`;
  - only the app's API holds `broker:decide`;
  - only the advisor holds `web:read`.

  Services fail closed when `AUTH_REQUIRED=1`.
- **Least-privilege database roles.**
  - Each service has its own Postgres role with only the grants it needs.
  - Agents never run SQL; they call MCP tools with fixed, parameterised queries.
  - Only the `broker` role can write trading tables.
- **Separation of duties.**
  - The proposer and the approver must be different identities.
  - At approval the broker re-prices the trade and refuses if the price moved more than
    3%, re-runs the position and order limits, and expires unanswered proposals after
    24 hours.
- **Tamper-evident audit log.**
  - Each row is hash-chained to the previous one.
  - Rows are written only through a `SECURITY DEFINER` function.
  - A trigger forbids UPDATE, DELETE and TRUNCATE.
  - `audit_verify()` recomputes the whole chain.
- **Prompt injection.** Company announcements and web pages are untrusted text:
  - The agent that reads announcements has no write tools and no route to the broker.
  - The advisor can only *suggest*, and you approve.
- **The app.**
  - Signed, HTTP-only, SameSite=Strict session cookies with an 8-hour lifetime.
  - Every write needs an `X-Desk-Request` header that a cross-site form can't set.
  - Failed logins are slowed down.

Live security drills (`scripts/security_drills.py`) run as three different identities
and confirm each refusal; all 13 pass.

### Paper broker

- **Fees and slippage:** commission is 0.15% of value (minimum Rs 25), and fills are
  0.1% worse than the quote.
- **Limits:**
  - one stock at most 20% of equity after the trade;
  - one order at most 10% of equity;
  - never more than the cash available;
  - no short selling.
- **Each trade passes through:**
  - a proposal (with its checks) and a decision (who, when, why);
  - a fill and its cash ledger entry;
  - an audit row.
- **Idempotency:** a unique client order id per trade, so a retried request never
  trades twice.

### PSX data

PSX has no official API.
- `psxdata` reads the data portal for OHLCV history, quotes, valuation and announcements.
- One screener request returns the price and day change for every listed symbol, about
  755 of them, in about 7 seconds.
- An adapter adds:
  - caching, retries and its own rate limiter;
  - last-good snapshots when PSX is down, marked stale;
  - handling for PSX's ex-dividend renames (`HUBCXD` is HUBC).

### The web app

- **Stack.** Next.js 16 with:
  - shadcn/ui on Base UI;
  - the Geist font;
  - a custom dark theme with green and red for gains and losses;
  - Recharts for charts, SWR for data, react-markdown for chat.

  It's built as a standalone Node server.
- **Same-origin API.** Next rewrites `/api/*` to the app API, so the session cookie
  stays first-party.
- **Real time.** One server-sent-events stream per tab (`/api/events`) carries:
  - live quotes: every 30 s while the market is open, 5 minutes when closed;
  - brief-completed events from Redis;
  - broker changes.

  Prices update and flash in place, other screens refresh through SWR, and toasts
  announce finished research.
- **Pages:** Today, Advisor, Demo account, Stocks (list and detail), Under the hood.

### Observability

- **One trace per brief** in Jaeger, across scheduler, orchestrator, agents, MCP servers
  and gateway. Trace context rides in `traceparent` for HTTP and A2A, and in the
  JSON-RPC `_meta` field for MCP.
- **The `llm_calls` ledger** for every model call, and `/v1/budget` for today's usage per
  model and the circuit state.
- **Run records** in Postgres and `runs/*.json`: brief, risk flags, every verification,
  reports, debate cases, prior views, models, tokens.

### Technology

| Area | Choice |
|---|---|
| Language | Python 3.13 (uv workspace monorepo); TypeScript for the app |
| Models | Groq free tier: gpt-oss-120b, gpt-oss-20b, Qwen 3.8 27B |
| Agent-to-agent | `a2a-sdk` 1.3 (A2A spec 1.0), including streaming |
| Tools | `mcp` 2.3: servers over streamable HTTP, scoped by signed tokens |
| HTTP | FastAPI, httpx, uvicorn |
| State | Postgres 17 with pgvector |
| Events, limits, cache | Redis 7 (Streams, token buckets) |
| Embeddings | fastembed, BAAI/bge-small-en-v1.5, on CPU, downloaded at build time |
| Identity | PyJWT with Ed25519 |
| Web search | `ddgs` (free metasearch) plus site sitemaps; BeautifulSoup, pypdf |
| Tracing | OpenTelemetry to Jaeger |
| UI | Next.js 16, shadcn/ui, Base UI, Recharts, SWR |
| Runtime | Docker Compose; services run as a non-root user |

### Repository layout

```
libs/contracts            typed, versioned message schemas
libs/agentkit             agent loop, gateway client, A2A server and directory, MCP toolset,
                          auth (signed tokens, guards, keygen), tracing
libs/marketdata           PSX source, snapshots, indicators
libs/eventbus             Redis Streams publish/consume, dead letters
mcp_servers/              market_data, memory, broker, official_web
agents/                   technical, fundamentals, news, debate, portfolio, advisor, baseline
services/                 llm_gateway, orchestrator, scheduler, dashboard (the app's API)
web/                      the Next.js app
config/                   access.toml (permissions), official_sources.toml (allow-list)
db/migrations             schema, roles and grants, audit triggers
docs/adr                  twelve decision records
scripts/                  security drills
tests/                    205 tests; no network or API key needed
```

### Testing

There are 205 automated tests. They run without network access or an API key, and the
Postgres tests use Docker. They cover:
- the rate limiter, router and circuit breaker;
- the agent loop, A2A (including streaming) and MCP over real HTTP;
- signed tokens and refusals;
- broker policy, idempotency, expiry and database role permissions, run as each role;
- audit-chain tamper detection;
- workflows and crash resume;
- the verifier and risk gate;
- streaming at every hop;
- the official-sources allow-list, redirects, sitemap discovery and page links;
- the app's sessions and CSRF guard.

Live drills cover crash-resume, duplicate events, poison events, agents down and
security refusals.

### Known limits

- **Free-tier speed.** At 8,000 tokens per minute, a multi-step advisor answer that reads
  official pages can take one to two minutes, because each step re-sends the
  conversation and waits for quota. Short questions are much faster.
- **PSX data** is scraped from undocumented endpoints and can fail; the app then shows
  cached, stale-marked data.
- **Free web search** can be throttled; the sitemap fallback covers company sites, but
  regulator pages without sitemaps depend on the search engine.
- **Official sources list** must be updated when a stock is added to the watchlist.
- **Single user.** The app has one operator account; it's designed for local use, with
  every port bound to 127.0.0.1.
