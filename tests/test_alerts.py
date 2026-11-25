"""Watchlists and price alerts: the firing rule (pure), the state machine in Postgres,
who may do what over MCP, and the scheduler turning a fired alert into research."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import asyncpg
import httpx2
import pytest
from agentkit.auth import BearerAuth, Identity, McpGuard, Verifier
from agentkit.mcp_tools import McpToolError, McpToolset
from broker_mcp.alerts import Reading, reading, verdict
from broker_mcp.policy import BrokerPolicy
from broker_mcp.server import build_server
from broker_mcp.store import BrokerStore
from contracts import OrderSide
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from mcp.client.streamable_http import streamable_http_client
from mcp.server.transport_security import TransportSecuritySettings

NOW = datetime.now(UTC) + timedelta(minutes=1)  # just after any rule a test creates

# ---------------------------------------------------------------- the rule (pure)


def judge(state="armed", drop=0.0, loss=None, kind="pct", threshold=5.0, ends_at=None):
    return verdict(kind=kind, threshold=threshold, state=state, ends_at=ends_at, now=NOW,
                   reading=Reading(drop, loss))


def test_reading_measures_the_fall_from_the_reference_and_the_rupees_lost():
    assert reading(100.0, 94.8, held=200) == Reading(5.2, 1040.0)
    assert reading(100.0, 110.0, held=0) == Reading(-10.0, None)  # above it; nothing held


def test_fires_on_the_crossing_and_not_again_while_still_below():
    assert judge("armed", drop=5.2) == "trigger"       # Monday: -5.2% crosses -5%
    assert judge("triggered", drop=6.0) == "hold"      # Tuesday: still down, no repeat


def test_rearms_only_after_recovering_past_the_buffer():
    assert judge("triggered", drop=4.5) == "hold"      # back above -5% but not past -4%
    assert judge("triggered", drop=4.0) == "rearm"     # recovered past 80% of the line
    assert judge("armed", drop=4.9) == "hold"


def test_rupee_alerts_compare_the_loss_and_end_when_the_position_is_sold():
    assert judge(kind="pkr", threshold=1000, drop=5.2, loss=1040.0) == "trigger"
    assert judge(kind="pkr", threshold=1000, drop=9.0, loss=None) == "end"


def test_a_window_expires_and_no_price_means_no_judgement():
    assert judge(drop=50, ends_at=NOW) == "expire"
    assert verdict(kind="pct", threshold=5, state="armed", ends_at=None, now=NOW,
                   reading=None) == "hold"


# ---------------------------------------------------------------- state in Postgres


@pytest.fixture
async def store(pg_url, broker_url):
    s = await BrokerStore.connect(broker_url, BrokerPolicy())
    admin = await asyncpg.connect(pg_url)
    await admin.execute("""
        ALTER TABLE audit_log DISABLE TRIGGER USER;
        TRUNCATE fills, cash_ledger, proposals, audit_log, alert_events, alert_rules, accounts,
                 watchlists, watchlist_accounts RESTART IDENTITY CASCADE;
        ALTER TABLE audit_log ENABLE TRIGGER USER;
        INSERT INTO accounts (account, initial_capital)
            VALUES ('saad', 1000000), ('other', 1000000);
        INSERT INTO cash_ledger (account, amount, reason)
            VALUES ('saad', 1000000, 'initial'), ('other', 1000000, 'initial');""")
    yield s, admin
    await admin.close()
    await s.close()


async def test_each_account_has_its_own_watchlist_and_research_covers_the_union(store):
    s, _ = store
    assert await s.init_watchlist("saad", ["LUCK", "HBL"]) is True
    await s.watch("ali", "HBL")
    await s.watch("ali", "OGDC")
    assert await s.watchlist("saad") == ["LUCK", "HBL"]
    assert await s.watchlist("ali") == ["HBL", "OGDC"]
    assert await s.watched_symbols() == ["HBL", "LUCK", "OGDC"]  # HBL once, not twice


async def test_the_starting_list_is_given_once_so_an_emptied_list_stays_empty(store):
    s, _ = store
    await s.init_watchlist("saad", ["LUCK"])
    await s.unwatch("saad", "LUCK")
    assert await s.init_watchlist("saad", ["LUCK"]) is False
    assert await s.watchlist("saad") == []


async def rule(s, **kw):
    args = {"account": "saad", "symbol": "LUCK", "kind": "pct", "threshold": 5.0,
            "ref_price": 100.0, "ref_basis": "avg_cost", "ends_at": None,
            "created_by": "dashboard:saad"} | kw
    return await s.create_alert(**args)


async def test_one_fall_alerts_once_then_rearms_after_recovery(store):
    s, admin = store
    r = await rule(s)
    held = {("saad", "LUCK"): 200}

    async def check(price, minutes):
        return await s.check_alerts({"LUCK": price}, held, actor="scheduler",
                                    now=NOW + timedelta(minutes=minutes))

    assert [e.kind for e in await check(94.8, 1)] == ["triggered"]
    assert await check(94.0, 2) == []                      # still below: no repeat
    assert await check(95.5, 3) == []                      # inside the buffer
    assert [e.kind for e in await check(96.5, 4)] == ["rearmed"]
    (again,) = await check(94.0, 5)
    assert again.kind == "triggered" and again.loss_pkr == 1200.0
    (now,) = await s.alerts("saad")
    assert now.id == r.id and now.state == "triggered" and now.times_triggered == 2
    assert await admin.fetchval("SELECT first_bad_id FROM audit_verify()") is None
    assert await admin.fetchval(
        "SELECT count(*) FROM audit_log WHERE action LIKE 'alert.%'") == 4  # created + 3 moves


async def test_a_window_ends_the_rule_and_a_stale_price_is_never_judged(store):
    s, _ = store
    await rule(s, ends_at=NOW + timedelta(days=7))
    assert await s.check_alerts({"LUCK": None}, {}, "scheduler", now=NOW) == []
    (r,) = await s.alerts("saad")
    assert r.status == "active" and r.last_price is None
    await s.check_alerts({"LUCK": 50.0}, {}, "scheduler", now=NOW + timedelta(days=8))
    (r,) = await s.alerts("saad")
    assert r.status == "expired" and r.times_triggered == 0  # past its window: no alert


async def test_another_account_cannot_cancel_or_see_your_alert(store):
    s, _ = store
    r = await rule(s)
    assert await s.cancel_alert("ali", r.id, "dashboard:ali") is None
    assert await s.alerts("ali") == []
    assert (await s.cancel_alert("saad", r.id, "dashboard:saad")).status == "cancelled"
    assert await s.check_alerts({"LUCK": 1.0}, {}, "scheduler", now=NOW) == []


async def test_the_broker_role_cannot_rewrite_an_alert(store):
    _, admin = store
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE broker")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await admin.execute("UPDATE alert_rules SET threshold = 1")
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE broker")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await admin.execute("DELETE FROM alert_events")


# ---------------------------------------------------------------- who may do what (HTTP)


class Quotes:
    def __init__(self):
        self.px = {"LUCK": 100.0}

    async def price(self, symbol):
        return self.px.get(symbol)

    async def prices(self, symbols):
        return {s: self.px.get(s) for s in symbols}


IDS = {n: Identity(n, Ed25519PrivateKey.generate())
       for n in ("dashboard", "advisor-agent", "scheduler")}
GRANTS = {"dashboard": frozenset({"broker:read", "broker:decide", "watchlist:read",
                                  "watchlist:write", "alerts:read", "alerts:write",
                                  "grants:issue"}),
          "advisor-agent": frozenset({"broker:read", "watchlist:read", "alerts:read"}),
          "scheduler": frozenset({"watchlist:all", "alerts:check"})}


@asynccontextmanager
async def http(store, quotes):
    v = Verifier("broker", {i.name: i.key.public_key() for i in IDS.values()}, GRANTS)
    app = build_server(store, quotes, guard=McpGuard("broker", v)).streamable_http_app(
        stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                                     allowed_hosts=["broker"]))
    async with app.router.lifespan_context(app):
        yield app


def as_(app, name, allow):
    client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app),
                                auth=BearerAuth(IDS[name]))
    return McpToolset(streamable_http_client("http://broker/mcp", http_client=client),
                      allow=allow)


ALERT_TOOLS = {"create_alert", "list_alerts", "check_alerts", "watch", "get_watchlist"}


def g(account: str, party: str = "dashboard") -> str:
    """The user's grant, as the dashboard signs it when they are signed in."""
    return IDS["dashboard"].mint_grant(account, "broker", party)


async def test_the_user_sets_alerts_the_advisor_only_reads_and_the_scheduler_only_checks(store):
    s, _ = store
    q = Quotes()
    async with http(s, q) as app:
        async with as_(app, "dashboard", ALERT_TOOLS) as ts:
            with pytest.raises(McpToolError, match="needs a position"):
                await ts.call("create_alert", account="saad", grant=g("saad"), symbol="LUCK", kind="pkr",
                              threshold=1000, created_by="saad")
            a = await ts.call("create_alert", account="saad", grant=g("saad"), symbol="LUCK", kind="pct",
                              threshold=5, created_by="saad", days=7)
            assert (a["ref_basis"], a["ref_price"]) == ("price_when_set", 100.0)
            assert a["created_by"] == "dashboard:saad" and a["ends_at"]
            await ts.call("watch", account="saad", grant=g("saad"), symbol="luck")
            with pytest.raises(McpToolError, match="lacks scope 'alerts:check'"):
                await ts.call("check_alerts")
        async with as_(app, "advisor-agent", ALERT_TOOLS) as ts:
            assert len(await ts.call("list_alerts", account="saad", grant=g("saad", "advisor-agent"))) == 1
            assert await ts.call("get_watchlist", account="saad", grant=g("saad", "advisor-agent")) == ["LUCK"]
            with pytest.raises(McpToolError, match="lacks scope 'alerts:write'"):
                await ts.call("create_alert", account="saad", grant=g("saad", "advisor-agent"), symbol="LUCK", kind="pct",
                              threshold=1, created_by="saad")
        q.px["LUCK"] = 94.0
        async with as_(app, "scheduler", ALERT_TOOLS) as ts:
            (ev,) = await ts.call("check_alerts")
            assert (ev["kind"], ev["account"], ev["drop_pct"]) == ("triggered", "saad", 6.0)
            assert await ts.call("check_alerts") == []  # fired once
            with pytest.raises(McpToolError, match="lacks scope 'watchlist:write'"):
                await ts.call("watch", account="saad", symbol="HBL")


async def test_a_held_stock_measures_from_its_average_cost(store):
    s, _ = store
    p = await s.propose(account="saad", client_order_id="manual:1", run_id=None,
                        proposed_by="dashboard:saad",
                        symbol="LUCK", side=OrderSide.buy, quantity=100, ref_price=80.0,
                        rationale="my own demo trade", brief={}, evidence_ids=[], marks={})
    await s.decide("saad", p.id, approve=True, actor="dashboard:saad", note="", quote=80.0,
                   marks={})
    async with http(s, Quotes()) as app, as_(app, "dashboard", ALERT_TOOLS) as ts:
        a = await ts.call("create_alert", account="saad", grant=g("saad"), symbol="LUCK", kind="pkr",
                          threshold=500, created_by="saad")
        # saad's shares are saad's: another user holds none, so no rupee alert for them
        with pytest.raises(McpToolError, match="you don't hold LUCK"):
            await ts.call("create_alert", account="other", grant=g("other"), symbol="LUCK", kind="pkr",
                          threshold=500, created_by="other")
        b = await ts.call("create_alert", account="other", grant=g("other"), symbol="LUCK", kind="pct",
                          threshold=5, created_by="other")
    assert a["ref_basis"] == "avg_cost" and a["ref_price"] == pytest.approx(80.08)  # slippage
    assert b["ref_basis"] == "price_when_set"  # measured from today's price, not saad's cost


# ---------------------------------------------------------------- the scheduler


async def test_a_fired_alert_requests_research_once_keyed_by_the_alert(monkeypatch):
    import scheduler.main as sch

    events = [{"id": 7, "kind": "triggered", "symbol": "LUCK"},
              {"id": 8, "kind": "rearmed", "symbol": "HBL"}]

    class FakeToolset:
        def __init__(self, url, allow):
            assert allow == {"check_alerts"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def call(self, tool):
            return events

    class Bus:
        def __init__(self):
            self.published = []

        async def publish(self, stream, event):
            self.published.append((stream, event))

    monkeypatch.setattr(sch, "McpToolset", FakeToolset)
    bus = Bus()
    assert await sch.check_alerts(bus) == 1
    ((stream, ev),) = bus.published
    assert stream == sch.REQUESTS and ev.type == "brief.requested"
    assert ev.payload["symbol"] == "LUCK" and ev.payload["idempotency_key"] == "alert:7"


def test_alerts_are_checked_in_market_hours_and_just_after_the_close():
    import scheduler.main as sch

    pkt = sch.PKT
    assert sch.market_hours(datetime(2026, 11, 23, 9, 45, tzinfo=pkt))       # Monday open
    assert sch.market_hours(datetime(2026, 11, 23, 15, 50, tzinfo=pkt))      # after close
    assert not sch.market_hours(datetime(2026, 11, 23, 16, 30, tzinfo=pkt))
    assert not sch.market_hours(datetime(2026, 11, 22, 11, 0, tzinfo=pkt))   # Sunday


async def test_research_covers_everyones_watchlist_and_falls_back_if_the_broker_is_down(
        monkeypatch):
    import scheduler.main as sch

    class Down:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            raise ConnectionError("broker down")

        async def __aexit__(self, *exc):
            return None

    monkeypatch.setattr(sch, "McpToolset", Down)
    monkeypatch.setenv("WATCHLIST", "LUCK,HBL")
    assert await sch.watchlist() == ["LUCK", "HBL"]
