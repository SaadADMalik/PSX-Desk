"""The evidence store: every fact this server computes, kept by id.

Why the *source* stores evidence, not the agents or the orchestrator: a value
that passed through an agent may have been altered (by a bug, a model, or a
compromised service), and A2A even changes its type (int -> float). The verifier
must compare a brief's numbers against the record as it was computed, so it
fetches from the one service that computed it.

This server owns the `evidence` table (its DB role can only SELECT and INSERT
there). Everyone else reads through the `get_evidence` MCP tool. One owner per
table is how services stay independently changeable.

Ids are content hashes, so storing is idempotent: the same facts arriving twice
are one row.
"""

import json
import re
from typing import Protocol

import asyncpg
from contracts import Evidence

ID_RE = re.compile(r"^ev_[0-9a-f]{12}$")


class EvidenceStore(Protocol):
    async def put(self, ev: Evidence) -> None: ...

    async def get(self, ids: list[str]) -> list[Evidence]: ...


class MemoryEvidenceStore:
    """For tests and for running without a database."""

    def __init__(self) -> None:
        self.rows: dict[str, Evidence] = {}

    async def put(self, ev: Evidence) -> None:
        self.rows.setdefault(ev.id, ev)

    async def get(self, ids: list[str]) -> list[Evidence]:
        return [self.rows[i] for i in ids if i in self.rows]


class PgEvidenceStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    @classmethod
    async def connect(cls, dsn: str) -> "PgEvidenceStore":
        return cls(await asyncpg.create_pool(dsn, min_size=1, max_size=4))

    async def put(self, ev: Evidence) -> None:
        await self.pool.execute(
            "INSERT INTO evidence (id, symbol, kind, as_of, facts, source, stale, fetched_at) "
            "VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8) ON CONFLICT (id) DO NOTHING",
            ev.id, ev.symbol, ev.kind, ev.as_of, json.dumps(ev.values, default=str),
            ev.source, ev.stale, ev.fetched_at)

    async def get(self, ids: list[str]) -> list[Evidence]:
        rows = await self.pool.fetch(
            "SELECT id, symbol, kind, as_of, facts::text AS facts, source, stale, fetched_at "
            "FROM evidence WHERE id = ANY($1)", ids)
        return [Evidence(id=r["id"], symbol=r["symbol"], kind=r["kind"], as_of=r["as_of"],
                         values=json.loads(r["facts"]), source=r["source"], stale=r["stale"],
                         fetched_at=r["fetched_at"]) for r in rows]

    async def close(self) -> None:
        await self.pool.close()
