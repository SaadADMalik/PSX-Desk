"""Ledger: one row per gateway request, for cost, latency and failure analysis.

Design choice: the ledger is *best effort*. If Postgres is down, model calls still
succeed and the row is logged instead. Losing a few ledger rows is better than
losing the call. (Audit records for trades, in Slice 4, make the opposite
choice: no audit row, no trade.)
"""

import logging
from dataclasses import asdict, dataclass

import asyncpg

log = logging.getLogger(__name__)


@dataclass
class LedgerRow:
    trace_id: str | None
    caller: str
    lane: str
    requested: str  # tier or pinned model
    model: str | None  # model that answered, if any
    outcome: str  # ok | cached | error
    attempts: int
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    cached: bool
    error: str | None = None


INSERT = """
INSERT INTO llm_calls (trace_id, caller, lane, requested, model, outcome, attempts,
                       prompt_tokens, completion_tokens, latency_ms, cached, error)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
"""


class Ledger:
    def __init__(self, pool: asyncpg.Pool | None) -> None:
        self.pool = pool

    async def record(self, row: LedgerRow) -> None:
        if self.pool is None:
            log.info("ledger (no db): %s", asdict(row))
            return
        try:
            await self.pool.execute(INSERT, *asdict(row).values())
        except Exception:
            log.exception("ledger write failed; row=%s", asdict(row))
