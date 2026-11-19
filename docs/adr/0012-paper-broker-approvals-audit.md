# ADR 0012: A paper broker with separated duties, human approval, and a hash-chained audit log

## Context
The desk produces a verified, risk-gated brief. The broker lets it act on one: a
paper trade. That is the first action with consequences, so three questions have to be
answered in code rather than by trust: who may propose a trade, who may approve one,
and how do we know afterwards what happened?

## Decisions

### 1. Separation of duties across three identities
| Who | Scopes on the broker | Can | Cannot |
|---|---|---|---|
| portfolio agent | broker:read, broker:propose | read the portfolio, propose | approve, execute |
| dashboard (a logged-in human) | broker:read, broker:decide, audit:read | approve or reject | propose |
| everyone else | none | nothing | anything |

A fully compromised agent can at worst add a *pending* proposal for a human to reject.
The agent is also missing `decide` from its tool allowlist. That's two independent locks,
in two places.

### 2. The model decides intent; code decides the numbers
The portfolio agent's model writes `buy | sell | hold`, a target weight and a rationale,
with no tools. Code turns that into shares. The weight is capped at 20% x confidence,
and the order at 10% of equity and the available cash. Code also overrides intents it
can't carry out: buy on a bearish brief, or sell with nothing held. The broker never
uses the proposer's price; it fetches its own quote.

### 3. The broker re-checks at fill time
Approval isn't execution. At the moment of approval the broker:
- re-prices at the current quote and refuses if it moved more than 3% since the proposal;
- recomputes the portfolio and re-runs its limits: cash, max position 20%, max order
  10%, no short selling.

Proposals expire after 24 h. Fills take 10 bps of slippage and a 0.15% fee (min PKR 25).
Decisions are serialised by an advisory lock, so two approvals at once can't both spend
the same cash.

### 4. Idempotent end to end
The workflow's `proposal` step sends `client_order_id = run:<run id>`, and the broker's
unique key makes a repeat return the first proposal. A crashed and redone step never
makes a second trade. A second approval of a filled proposal returns "already filled".

### 5. Audit log: append-only, and tamper-evident when that fails
- Each state change and its audit row commit **in one transaction**.
- No role has UPDATE or DELETE on `audit_log`, and a trigger refuses both, and TRUNCATE,
  even for the owner.
- Writers can't INSERT either. They get EXECUTE on `audit_append()`, which holds a
  lock, chains the new row's SHA-256 to the previous row's, and inserts.
- `audit_verify()` recomputes the chain. If an administrator disables the trigger and
  edits a row, the chain breaks at that row (tested).
- Actors are proven identities, e.g. `portfolio-agent`. A human acting through the
  dashboard is recorded as `dashboard:<user>`: the service proved itself to the broker,
  and the human proved themself to the service.

### 6. Least privilege in the database, per service role
The `broker` role:
- may INSERT proposals, fills and cash ledger rows;
- may UPDATE only a proposal's status and decision columns, never its quantity or price;
- may not DELETE anything.

No other role has any grant on trading tables. Tests attempt each forbidden write as each
role.

## Consequences
- The hash chain proves *whether* the log was altered, not *who* altered it, and an
  attacker with owner rights could rewrite the whole chain consistently. Anchoring the
  latest hash outside the database (printing it daily, or writing it to a second
  system) closes that. Not done here.
- Human authentication is a single local user and password, behind 127.0.0.1. It's
  enough to show the boundary; a real desk would use SSO and per-person accounts.
- Paper fills ignore order books, partial fills, taxes and circuit breakers. They are
  good enough to compare decisions, not to estimate real execution costs.
- Every proposal costs one small-model call, but only for actionable briefs. Most briefs
  aren't actionable, so the token impact is small.
