"""Trading state in Postgres, as the `broker` role.

Every state change and its audit record commit in one transaction: there is no
trade without an audit row, and no audit row for a trade that rolled back.
Decisions are serialised with an advisory lock, so two approvals at once cannot
both spend the same cash.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
from contracts import Check, Decision, Fill, OrderSide, Portfolio, Proposal, ProposalStatus

from broker_mcp.policy import (
    BrokerPolicy,
    build_portfolio,
    failed,
    fee,
    fill_price,
    pre_trade_checks,
)

_COLS = ("id, client_order_id, run_id, proposed_by, symbol, side, quantity, ref_price, "
         "rationale, brief, evidence_ids, checks, status, decided_by, decided_at, "
         "decision_note, created_at, expires_at")


def _iso(v: Any) -> str | None:
    return v.isoformat() if isinstance(v, datetime) else v


def _proposal(row: asyncpg.Record) -> Proposal:
    d = dict(row)
    return Proposal(**{**d, "id": str(d["id"]), "ref_price": float(d["ref_price"]),
                       "brief": json.loads(d["brief"]),
                       "checks": [Check(**c) for c in json.loads(d["checks"])],
                       "decided_at": _iso(d["decided_at"]), "created_at": _iso(d["created_at"]),
                       "expires_at": _iso(d["expires_at"])})


async def audit(conn: asyncpg.Connection, actor: str, action: str, subject: str,
                details: dict) -> int:
    return await conn.fetchval("SELECT audit_append($1, $2, $3, $4::jsonb)", actor, action,
                               subject, json.dumps(details, default=str))


class BrokerStore:
    def __init__(self, pool: asyncpg.Pool, policy: BrokerPolicy | None = None) -> None:
        self.pool, self.policy = pool, policy or BrokerPolicy()

    @classmethod
    async def connect(cls, dsn: str, policy: BrokerPolicy | None = None) -> "BrokerStore":
        return cls(await asyncpg.create_pool(dsn, min_size=1, max_size=4), policy)

    async def close(self) -> None:
        await self.pool.close()

    # ------------------------------------------------------------ reads

    async def _book(self, conn) -> tuple[float, list[dict]]:
        cash = float(await conn.fetchval("SELECT coalesce(sum(amount), 0) FROM cash_ledger"))
        fills = [dict(r) for r in await conn.fetch(
            "SELECT symbol, side, quantity, price FROM fills ORDER BY id")]
        return cash, fills

    async def held_symbols(self) -> list[str]:
        rows = await self.pool.fetch(
            "SELECT symbol FROM fills GROUP BY symbol "
            "HAVING sum(CASE side WHEN 'buy' THEN quantity ELSE -quantity END) <> 0")
        return [r["symbol"] for r in rows]

    async def portfolio(self, marks: dict[str, float | None], conn=None) -> Portfolio:
        cash, fills = await self._book(conn or self.pool)
        return build_portfolio(cash, fills, marks)

    async def proposals(self, status: str | None = None, limit: int = 50) -> list[Proposal]:
        rows = await self.pool.fetch(
            f"SELECT {_COLS} FROM proposals WHERE ($1::text IS NULL OR status = $1) "
            "ORDER BY created_at DESC LIMIT $2", status, limit)
        return [_proposal(r) for r in rows]

    async def proposal(self, pid: str) -> Proposal | None:
        row = await self.pool.fetchrow(f"SELECT {_COLS} FROM proposals WHERE id = $1",
                                       uuid.UUID(pid))
        return _proposal(row) if row else None

    async def fills(self, limit: int = 50) -> list[Fill]:
        rows = await self.pool.fetch(
            "SELECT proposal_id, symbol, side, quantity, price, fees, filled_at FROM fills "
            "ORDER BY id DESC LIMIT $1", limit)
        return [Fill(proposal_id=str(r["proposal_id"]), symbol=r["symbol"], side=r["side"],
                     quantity=r["quantity"], price=float(r["price"]), fees=float(r["fees"]),
                     filled_at=r["filled_at"].isoformat()) for r in rows]

    async def audit_log(self, limit: int = 50) -> list[dict]:
        rows = await self.pool.fetch(
            "SELECT id, at, actor, action, subject, details, hash FROM audit_log "
            "ORDER BY id DESC LIMIT $1", limit)
        return [{**dict(r), "at": r["at"].isoformat(), "details": json.loads(r["details"]),
                 "hash": r["hash"][:12]} for r in rows]

    async def verify_audit(self) -> dict:
        r = await self.pool.fetchrow("SELECT * FROM audit_verify()")
        return {"checked": r["checked"], "first_bad_id": r["first_bad_id"],
                "ok": r["first_bad_id"] is None}

    # ------------------------------------------------------------ writes

    async def propose(self, *, client_order_id: str, run_id: str | None, proposed_by: str,
                      symbol: str, side: OrderSide, quantity: int, ref_price: float,
                      rationale: str, brief: dict, evidence_ids: list[str],
                      marks: dict[str, float | None]) -> Proposal:
        """Idempotent on client_order_id: a repeat returns the first proposal unchanged.
        A proposal that fails the broker's checks is still recorded (status failed), so
        the refusal is visible and audited."""
        async with self.pool.acquire() as conn, conn.transaction():
            existing = await conn.fetchrow(
                f"SELECT {_COLS} FROM proposals WHERE client_order_id = $1", client_order_id)
            if existing:
                return _proposal(existing)
            pf = await self.portfolio(marks, conn)
            checks = pre_trade_checks(symbol, side, quantity, ref_price, pf, self.policy)
            status = ProposalStatus.pending if not failed(checks) else ProposalStatus.failed
            expires = datetime.now(UTC) + timedelta(hours=self.policy.expiry_hours)
            row = await conn.fetchrow(
                f"INSERT INTO proposals (client_order_id, run_id, proposed_by, symbol, side, "
                f"quantity, ref_price, rationale, brief, evidence_ids, checks, status, "
                f"expires_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10,$11::jsonb,$12,$13) "
                f"RETURNING {_COLS}",
                client_order_id, run_id, proposed_by, symbol, side.value, quantity, ref_price,
                rationale, json.dumps(brief), evidence_ids,
                json.dumps([c.model_dump() for c in checks]), status.value, expires)
            p = _proposal(row)
            await audit(conn, proposed_by,
                        "proposal.created" if status is ProposalStatus.pending
                        else "proposal.refused",
                        f"proposal:{p.id}",
                        {"symbol": symbol, "side": side.value, "quantity": quantity,
                         "ref_price": ref_price, "run_id": run_id,
                         "failed_checks": failed(checks)})
            return p

    async def decide(self, pid: str, *, approve: bool, actor: str, note: str,
                     quote: float | None, marks: dict[str, float | None]) -> Decision:
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('broker.decide'))")
            row = await conn.fetchrow(f"SELECT {_COLS} FROM proposals WHERE id = $1 FOR UPDATE",
                                      uuid.UUID(pid))
            if row is None:
                raise LookupError(f"no proposal {pid}")
            p = _proposal(row)
            if p.status is not ProposalStatus.pending:
                return Decision(proposal=p, reason=f"already {p.status.value}")

            async def close(status: ProposalStatus, action: str, reason: str | None,
                            details: dict) -> Proposal:
                r = await conn.fetchrow(
                    f"UPDATE proposals SET status = $2, decided_by = $3, decided_at = now(), "
                    f"decision_note = $4 WHERE id = $1 RETURNING {_COLS}",
                    uuid.UUID(pid), status.value, actor, (note or reason or "")[:500])
                await audit(conn, actor, action, f"proposal:{pid}",
                            {"reason": reason, "note": note, **details})
                return _proposal(r)

            if datetime.fromisoformat(p.expires_at) < datetime.now(UTC):
                return Decision(proposal=await close(ProposalStatus.expired, "proposal.expired",
                                                     "expired before a decision", {}),
                                reason="expired before a decision")
            if not approve:
                return Decision(proposal=await close(ProposalStatus.rejected,
                                                     "proposal.rejected", None, {}))

            # Approved by a human. The broker still checks, with today's numbers.
            reasons = []
            if quote is None:
                reasons.append("no current quote")
            else:
                drift = 100 * abs(quote - p.ref_price) / p.ref_price
                if drift > self.policy.max_price_drift_pct:
                    reasons.append(f"price moved {drift:.1f}% since proposal "
                                   f"(limit {self.policy.max_price_drift_pct:g}%)")
                pf = await self.portfolio(marks, conn)
                reasons += failed(pre_trade_checks(p.symbol, p.side, p.quantity, quote, pf,
                                                   self.policy))
            if reasons:
                reason = "; ".join(reasons)
                return Decision(proposal=await close(ProposalStatus.failed, "order.failed",
                                                     reason, {"quote": quote}),
                                reason=reason)

            price = fill_price(quote, p.side, self.policy)
            value = round(p.quantity * price, 2)
            fees = fee(value, self.policy)
            fill_id = await conn.fetchval(
                "INSERT INTO fills (proposal_id, symbol, side, quantity, price, fees) "
                "VALUES ($1, $2, $3, $4, $5, $6) RETURNING id",
                uuid.UUID(pid), p.symbol, p.side.value, p.quantity, price, fees)
            amount = -(value + fees) if p.side is OrderSide.buy else value - fees
            await conn.execute(
                "INSERT INTO cash_ledger (amount, reason, fill_id) VALUES ($1, $2, $3)",
                round(amount, 2), f"{p.side.value} {p.quantity} {p.symbol} @ {price}", fill_id)
            closed = await close(ProposalStatus.filled, "order.filled", None,
                                 {"symbol": p.symbol, "side": p.side.value,
                                  "quantity": p.quantity, "price": price, "fees": fees,
                                  "quote": quote})
            filled_at = await conn.fetchval("SELECT filled_at FROM fills WHERE id = $1", fill_id)
            return Decision(proposal=closed, fill=Fill(
                proposal_id=pid, symbol=p.symbol, side=p.side, quantity=p.quantity,
                price=price, fees=fees, filled_at=filled_at.isoformat()))
