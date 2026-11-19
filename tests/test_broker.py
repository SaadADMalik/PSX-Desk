"""The paper broker: its limits (pure), its ledger and audit chain (real Postgres), and
separation of duties through the real MCP SDK over HTTP."""

from contextlib import asynccontextmanager

import asyncpg
import httpx2
import pytest
from agentkit.auth import BearerAuth, Identity, McpGuard, Verifier
from agentkit.mcp_tools import McpToolError, McpToolset
from broker_mcp.policy import BrokerPolicy, build_portfolio, failed, pre_trade_checks
from broker_mcp.server import build_server
from broker_mcp.store import BrokerStore
from contracts import OrderSide, ProposalStatus
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from mcp.client.streamable_http import streamable_http_client
from mcp.server.transport_security import TransportSecuritySettings

BRIEF = {"stance": "bullish", "confidence": 0.62, "thesis": "test thesis", "risk": "pass"}

# ---------------------------------------------------------------- policy (pure)


def test_average_cost_and_weights_from_fills():
    fills = [{"symbol": "LUCK", "side": "buy", "quantity": 100, "price": 100},
             {"symbol": "LUCK", "side": "buy", "quantity": 100, "price": 200},
             {"symbol": "LUCK", "side": "sell", "quantity": 50, "price": 300}]
    pf = build_portfolio(10_000, fills, {"LUCK": 250.0})
    (p,) = pf.positions
    assert (p.quantity, p.avg_cost, p.market_value) == (150, 150.0, 37_500)
    assert pf.equity == 47_500 and p.weight_pct == pytest.approx(78.95)


def test_unquoted_positions_are_valued_at_cost_and_flagged():
    pf = build_portfolio(0, [{"symbol": "X", "side": "buy", "quantity": 10, "price": 5}],
                         {"X": None})
    assert pf.positions[0].market_value == 50 and pf.marks_stale == ["X"]


@pytest.mark.parametrize("side, qty, policy, rule", [
    ("buy", 2_000, BrokerPolicy(max_position_pct=1000, max_order_pct=1000), "cash"),
    ("buy", 150, BrokerPolicy(max_position_pct=10, max_order_pct=100), "max_position"),
    ("sell", 1, BrokerPolicy(), "no_short"),
])
def test_each_limit_refuses_on_its_own(side, qty, policy, rule):
    pf = build_portfolio(100_000, [], {})
    bad = failed(pre_trade_checks("LUCK", OrderSide(side), qty, 100.0, pf, policy))
    assert [b.split(":")[0] for b in bad] == [rule]


def test_order_size_limit_is_separate_from_position_limit():
    pf = build_portfolio(100_000, [], {})
    bad = failed(pre_trade_checks("LUCK", OrderSide.buy, 150, 100.0, pf,
                                  BrokerPolicy(max_position_pct=50, max_order_pct=10)))
    assert [b.split(":")[0] for b in bad] == ["max_order"]


# ---------------------------------------------------------------- ledger + audit (Postgres)


@pytest.fixture
async def broker(pg_url):
    s = await BrokerStore.connect(pg_url, BrokerPolicy(max_position_pct=20, max_order_pct=10))
    admin = await asyncpg.connect(pg_url)
    # audit_log refuses TRUNCATE by design; tests start from a fresh table instead.
    await admin.execute("""
        ALTER TABLE audit_log DISABLE TRIGGER USER;
        TRUNCATE fills, cash_ledger, proposals, audit_log RESTART IDENTITY CASCADE;
        ALTER TABLE audit_log ENABLE TRIGGER USER;
        INSERT INTO cash_ledger (amount, reason) VALUES (1000000, 'initial');""")
    yield s, admin
    await admin.close()
    await s.close()


async def propose(s: BrokerStore, key="run:1", qty=100, side=OrderSide.buy, price=500.0):
    return await s.propose(client_order_id=key, run_id="1", proposed_by="portfolio-agent",
                           symbol="LUCK", side=side, quantity=qty, ref_price=price,
                           rationale="bullish brief, confidence 0.62", brief=BRIEF,
                           evidence_ids=["ev_000000000001"], marks={})


async def test_proposal_is_idempotent_on_client_order_id(broker):
    s, _ = broker
    a = await propose(s)
    b = await propose(s, qty=999)  # a retry with different numbers changes nothing
    assert a.id == b.id and b.quantity == 100 and a.status is ProposalStatus.pending
    assert len(await s.proposals()) == 1


async def test_approval_fills_at_the_current_quote_with_slippage_and_fees(broker):
    s, _ = broker
    p = await propose(s, qty=100, price=500.0)
    d = await s.decide(p.id, approve=True, actor="dashboard:saad", note="ok",
                       quote=505.0, marks={"LUCK": 505.0})
    assert d.proposal.status is ProposalStatus.filled and d.fill is not None
    assert d.fill.price == pytest.approx(505.505)  # +10 bps
    pf = await s.portfolio({"LUCK": 505.0})
    assert pf.cash == pytest.approx(1_000_000 - 50_550.5 - 75.83, abs=0.01)
    assert pf.positions[0].quantity == 100
    again = await s.decide(p.id, approve=True, actor="dashboard:saad", note="",
                           quote=505.0, marks={})
    assert again.fill is None and again.reason == "already filled"  # no double fill


async def test_broker_rechecks_at_fill_time_and_refuses_a_moved_price(broker):
    s, _ = broker
    p = await propose(s, qty=100, price=500.0)
    d = await s.decide(p.id, approve=True, actor="dashboard:saad", note="",
                       quote=530.0, marks={"LUCK": 530.0})  # +6%
    assert d.proposal.status is ProposalStatus.failed and "price moved 6.0%" in d.reason
    assert (await s.portfolio({})).cash == 1_000_000


async def test_proposal_failing_limits_is_recorded_as_refused(broker):
    s, _ = broker
    p = await propose(s, qty=1_000, price=500.0)  # 500K: 50% of equity in one order
    assert p.status is ProposalStatus.failed
    assert {c.rule for c in p.checks if not c.ok} == {"max_position", "max_order"}
    assert (await s.audit_log())[0]["action"] == "proposal.refused"


async def test_rejection_and_expiry_are_terminal(broker):
    s, admin = broker
    p = await propose(s, key="run:a")
    d = await s.decide(p.id, approve=False, actor="dashboard:saad", note="not convinced",
                       quote=None, marks={})
    assert d.proposal.status is ProposalStatus.rejected and d.proposal.decided_by == \
        "dashboard:saad"
    q = await propose(s, key="run:b")
    await admin.execute("UPDATE proposals SET expires_at = now() - interval '1 hour' "
                        "WHERE id = $1", __import__("uuid").UUID(q.id))
    d = await s.decide(q.id, approve=True, actor="dashboard:saad", note="", quote=500.0,
                       marks={})
    assert d.proposal.status is ProposalStatus.expired and d.fill is None


async def test_audit_chain_verifies_and_detects_tampering(broker):
    s, admin = broker
    p = await propose(s)
    await s.decide(p.id, approve=True, actor="dashboard:saad", note="", quote=500.0,
                   marks={"LUCK": 500.0})
    assert [r["action"] for r in await s.audit_log()] == ["order.filled", "proposal.created"]
    assert await s.verify_audit() == {"checked": 2, "first_bad_id": None, "ok": True}

    with pytest.raises(asyncpg.RaiseError, match="append-only"):
        await admin.execute("UPDATE audit_log SET actor = 'someone-else' WHERE id = 1")
    with pytest.raises(asyncpg.RaiseError, match="append-only"):
        await admin.execute("DELETE FROM audit_log")

    # Even the owner, disabling the trigger, cannot hide an edit: the chain breaks.
    await admin.execute("ALTER TABLE audit_log DISABLE TRIGGER USER; "
                        "UPDATE audit_log SET details = '{\"quantity\": 1}' WHERE id = 1; "
                        "ALTER TABLE audit_log ENABLE TRIGGER USER;")
    assert await s.verify_audit() == {"checked": 1, "first_bad_id": 1, "ok": False}


@pytest.mark.parametrize("role, sql", [
    ("broker", "UPDATE audit_log SET actor = 'x'"),
    ("broker", ("INSERT INTO audit_log (at, actor, action, subject, prev_hash, hash) "
                "VALUES (now(), 'x', 'x', 'x', 'x', 'x')")),  # only via audit_append
    ("broker", "UPDATE proposals SET quantity = 1"),  # column grants: status fields only
    ("broker", "DELETE FROM proposals"),
    ("market_data", ("INSERT INTO fills (proposal_id, symbol, side, quantity, price, fees) "
                     "VALUES (gen_random_uuid(), 'X', 'buy', 1, 1, 0)")),
    ("orchestrator", "SELECT audit_append('x', 'x', 'x', '{}')"),
    ("memory", "SELECT * FROM proposals"),
])
async def test_least_privilege_per_role(broker, role, sql):
    _, admin = broker
    async with admin.transaction():
        await admin.execute(f"SET LOCAL ROLE {role}")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await admin.execute(sql)


# ---------------------------------------------------------------- separation of duties (HTTP)


class FakeQuotes:
    async def price(self, symbol: str) -> float | None:
        return 500.0


AGENT, DASH = (Identity(n, Ed25519PrivateKey.generate()) for n in ("portfolio-agent", "dashboard"))
GRANTS = {"portfolio-agent": frozenset({"broker:read", "broker:propose"}),
          "dashboard": frozenset({"broker:read", "broker:decide", "audit:read"})}


@asynccontextmanager
async def broker_http(store):
    v = Verifier("broker", {i.name: i.key.public_key() for i in (AGENT, DASH)}, GRANTS)
    server = build_server(store, FakeQuotes(), guard=McpGuard("broker", v))
    app = server.streamable_http_app(
        stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                                     allowed_hosts=["broker"]))
    async with app.router.lifespan_context(app):
        yield app


def as_(app, ident, allow):
    http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app), auth=BearerAuth(ident))
    return McpToolset(streamable_http_client("http://broker/mcp", http_client=http), allow=allow)


async def test_the_proposer_cannot_approve_and_the_approver_cannot_propose(broker):
    s, _ = broker
    async with broker_http(s) as app:
        async with as_(app, AGENT, {"propose_order", "decide"}) as ts:
            p = await ts.call("propose_order", client_order_id="run:x", symbol="LUCK",
                              side="buy", quantity=10, rationale="bullish, conf 0.6",
                              brief=BRIEF, run_id="x")
            assert p["status"] == "pending" and p["proposed_by"] == "portfolio-agent"
            assert p["ref_price"] == 500.0  # the broker priced it, not the caller
            with pytest.raises(McpToolError, match="lacks scope 'broker:decide'"):
                await ts.call("decide", proposal_id=p["id"], approve=True, decided_by="agent")
        async with as_(app, DASH, {"propose_order", "decide"}) as ts:
            with pytest.raises(McpToolError, match="lacks scope 'broker:propose'"):
                await ts.call("propose_order", client_order_id="run:y", symbol="LUCK",
                              side="buy", quantity=10, rationale="x" * 20, brief={})
            d = await ts.call("decide", proposal_id=p["id"], approve=True, decided_by="saad")
    assert d["proposal"]["status"] == "filled"
    assert d["proposal"]["decided_by"] == "dashboard:saad"  # service + the human it vouches for


async def test_audit_tools_return_structured_results_over_mcp(broker):
    s, _ = broker
    await propose(s)
    async with broker_http(s) as app, as_(app, DASH, {"audit_log", "verify_audit"}) as ts:
        assert (await ts.call("verify_audit"))["ok"] is True
        (entry,) = await ts.call("audit_log")
    assert entry["action"] == "proposal.created" and entry["actor"] == "portfolio-agent"
