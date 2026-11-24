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
from contracts import (
    AlertEvent,
    AlertRule,
    Check,
    Decision,
    Fill,
    OrderSide,
    Portfolio,
    Proposal,
    ProposalStatus,
)

from broker_mcp.alerts import reading, verdict
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


_ALERT_COLS = ("id, account, symbol, kind, threshold, ref_price, ref_basis, starts_at, ends_at, "
               "state, status, last_price, last_checked_at, times_triggered, created_by, "
               "created_at")
_EVENT_COLS = "id, rule_id, account, symbol, kind, price, drop_pct, loss_pkr, at"
MAX_ACTIVE_ALERTS = 50
MAX_WATCHLIST = 60


def _iso(v: Any) -> str | None:
    return v.isoformat() if isinstance(v, datetime) else v


def _num(v: Any) -> float | None:
    return None if v is None else float(v)


def _alert(row: asyncpg.Record) -> AlertRule:
    d = dict(row)
    return AlertRule(**{**d, "id": str(d["id"]), "threshold": float(d["threshold"]),
                        "ref_price": float(d["ref_price"]), "last_price": _num(d["last_price"]),
                        **{k: _iso(d[k]) for k in ("starts_at", "ends_at", "last_checked_at",
                                                   "created_at")}})


def _event(row: asyncpg.Record) -> AlertEvent:
    d = dict(row)
    return AlertEvent(**{**d, "rule_id": str(d["rule_id"]), "price": float(d["price"]),
                         "drop_pct": float(d["drop_pct"]), "loss_pkr": _num(d["loss_pkr"]),
                         "at": d["at"].isoformat()})


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

    # ------------------------------------------------------------ watchlists

    async def watchlist(self, account: str) -> list[str]:
        rows = await self.pool.fetch(
            "SELECT symbol FROM watchlists WHERE account = $1 ORDER BY seq", account)
        return [r["symbol"] for r in rows]

    async def watched_symbols(self) -> list[str]:
        """Every symbol on anyone's watchlist, once: research is shared per stock."""
        rows = await self.pool.fetch("SELECT DISTINCT symbol FROM watchlists ORDER BY symbol")
        return [r["symbol"] for r in rows]

    async def init_watchlist(self, account: str, symbols: list[str]) -> bool:
        """Give a new account its starting list, once. False if it was set up before, so
        a user who empties their list keeps it empty."""
        async with self.pool.acquire() as conn, conn.transaction():
            new = await conn.fetchval(
                "INSERT INTO watchlist_accounts (account) VALUES ($1) "
                "ON CONFLICT DO NOTHING RETURNING true", account)
            if not new:
                return False
            await conn.executemany(
                "INSERT INTO watchlists (account, symbol) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                [(account, s) for s in symbols[:MAX_WATCHLIST]])
            return True

    async def watch(self, account: str, symbol: str) -> list[str]:
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("INSERT INTO watchlist_accounts (account) VALUES ($1) "
                               "ON CONFLICT DO NOTHING", account)
            # One writer per account at a time, so the size limit holds.
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('watchlist:' || $1))",
                               account)
            n = await conn.fetchval("SELECT count(*) FROM watchlists WHERE account = $1", account)
            if n >= MAX_WATCHLIST:
                raise ValueError(f"a watchlist holds at most {MAX_WATCHLIST} stocks")
            await conn.execute("INSERT INTO watchlists (account, symbol) VALUES ($1, $2) "
                               "ON CONFLICT DO NOTHING", account, symbol)
        return await self.watchlist(account)

    async def unwatch(self, account: str, symbol: str) -> list[str]:
        await self.pool.execute("DELETE FROM watchlists WHERE account = $1 AND symbol = $2",
                                account, symbol)
        return await self.watchlist(account)

    # ------------------------------------------------------------ alerts

    async def alerts(self, account: str, active_only: bool = False) -> list[AlertRule]:
        rows = await self.pool.fetch(
            f"SELECT {_ALERT_COLS} FROM alert_rules WHERE account = $1 "
            "AND (NOT $2 OR status = 'active') ORDER BY created_at DESC LIMIT 200",
            account, active_only)
        return [_alert(r) for r in rows]

    async def alert_events(self, account: str, limit: int = 50) -> list[AlertEvent]:
        rows = await self.pool.fetch(
            f"SELECT {_EVENT_COLS} FROM alert_events WHERE account = $1 "
            "ORDER BY id DESC LIMIT $2", account, limit)
        return [_event(r) for r in rows]

    async def create_alert(self, *, account: str, symbol: str, kind: str, threshold: float,
                           ref_price: float, ref_basis: str, ends_at: datetime | None,
                           created_by: str) -> AlertRule:
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('alerts:' || $1))",
                               account)
            n = await conn.fetchval("SELECT count(*) FROM alert_rules "
                                    "WHERE account = $1 AND status = 'active'", account)
            if n >= MAX_ACTIVE_ALERTS:
                raise ValueError(f"at most {MAX_ACTIVE_ALERTS} active alerts per account")
            row = await conn.fetchrow(
                f"INSERT INTO alert_rules (account, symbol, kind, threshold, ref_price, "
                f"ref_basis, ends_at, created_by) VALUES ($1,$2,$3,$4,$5,$6,$7,$8) "
                f"RETURNING {_ALERT_COLS}",
                account, symbol, kind, threshold, ref_price, ref_basis, ends_at, created_by)
            rule = _alert(row)
            await audit(conn, created_by, "alert.created", f"alert:{rule.id}",
                        {"account": account, "symbol": symbol, "kind": kind,
                         "threshold": threshold, "ref_price": ref_price,
                         "ends_at": rule.ends_at})
            return rule

    async def cancel_alert(self, account: str, alert_id: str, actor: str) -> AlertRule | None:
        """Cancel one of this account's active alerts. None if the account has no such
        alert (another account's id is treated as unknown)."""
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                f"UPDATE alert_rules SET status = 'cancelled' WHERE id = $1 AND account = $2 "
                f"AND status = 'active' RETURNING {_ALERT_COLS}", uuid.UUID(alert_id), account)
            if row is not None:
                await audit(conn, actor, "alert.cancelled", f"alert:{alert_id}", {})
                return _alert(row)
            row = await conn.fetchrow(f"SELECT {_ALERT_COLS} FROM alert_rules "
                                      "WHERE id = $1 AND account = $2",
                                      uuid.UUID(alert_id), account)
            return _alert(row) if row else None

    async def active_alert_symbols(self, now: datetime | None = None) -> list[str]:
        rows = await self.pool.fetch(
            "SELECT DISTINCT symbol FROM alert_rules WHERE status = 'active' "
            "AND starts_at <= $1", now or datetime.now(UTC))
        return [r["symbol"] for r in rows]

    async def check_alerts(self, prices: dict[str, float | None], held: dict[str, int],
                           actor: str, now: datetime | None = None) -> list[AlertEvent]:
        """One pass over every active rule. Each rule is judged and moved in its own
        transaction under a row lock, so two checkers at once cannot both fire the same
        rule. Returns the events this pass created (triggers and re-arms)."""
        now = now or datetime.now(UTC)
        ids = await self.pool.fetch(
            "SELECT id FROM alert_rules WHERE status = 'active' AND starts_at <= $1", now)
        out: list[AlertEvent] = []
        for (rid,) in ids:
            async with self.pool.acquire() as conn, conn.transaction():
                row = await conn.fetchrow(
                    f"SELECT {_ALERT_COLS} FROM alert_rules WHERE id = $1 AND status = 'active' "
                    "FOR UPDATE SKIP LOCKED", rid)
                if row is None:
                    continue  # another checker holds it, or it has just ended
                event = await self._check_one(conn, _alert(row), row["ends_at"], prices, held,
                                              actor, now)
                if event:
                    out.append(event)
        return out

    async def _check_one(self, conn, rule: AlertRule, ends_at: datetime | None,
                         prices: dict[str, float | None], held: dict[str, int], actor: str,
                         now: datetime) -> AlertEvent | None:
        price = prices.get(rule.symbol)
        r = reading(rule.ref_price, price, held.get(rule.symbol, 0)) if price else None
        v = verdict(kind=rule.kind.value, threshold=rule.threshold, state=rule.state,
                    ends_at=ends_at, now=now, reading=r)
        rid = uuid.UUID(rule.id)
        if v in ("expire", "end"):
            status = "expired" if v == "expire" else "ended"
            await conn.execute("UPDATE alert_rules SET status = $2 WHERE id = $1", rid, status)
            await audit(conn, actor, f"alert.{status}", f"alert:{rule.id}",
                        {"reason": "its window passed" if v == "expire" else "position sold"})
            return None
        if price is not None:
            await conn.execute("UPDATE alert_rules SET last_price = $2, last_checked_at = $3 "
                               "WHERE id = $1", rid, price, now)
        if v == "hold" or r is None:
            return None
        fired = v == "trigger"
        await conn.execute(
            "UPDATE alert_rules SET state = $2, times_triggered = times_triggered + $3 "
            "WHERE id = $1", rid, "triggered" if fired else "armed", 1 if fired else 0)
        kind = "triggered" if fired else "rearmed"
        ev = await conn.fetchrow(
            "INSERT INTO alert_events (rule_id, account, symbol, kind, price, drop_pct, "
            f"loss_pkr, at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING {_EVENT_COLS}",
            rid, rule.account, rule.symbol, kind, price, r.drop_pct, r.loss_pkr, now)
        await audit(conn, actor, f"alert.{kind}", f"alert:{rule.id}",
                    {"account": rule.account, "symbol": rule.symbol, "price": price,
                     "drop_pct": r.drop_pct, "loss_pkr": r.loss_pkr})
        return _event(ev)
