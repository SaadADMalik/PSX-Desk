"""The portfolio agent's sizing (code decides shares, not the model), its fan-out to every
account a brief concerns, and the dashboard's human-side security: accounts, JWT
sessions with rotating refresh tokens, CSRF, and users kept apart."""

import asyncio

import asyncpg
import httpx
import pytest
from contracts import Portfolio, Position, TradeIntent
from portfolio_agent.main import size


def pf(cash=1_000_000.0, held=0, price=100.0) -> Portfolio:
    positions = [Position(symbol="LUCK", quantity=held, avg_cost=price, mark=price,
                          market_value=held * price, weight_pct=0)] if held else []
    return Portfolio(cash=cash, equity=cash + held * price, positions=positions)


def intent(action="buy", weight=10.0) -> TradeIntent:
    return TradeIntent(action=action, target_weight_pct=weight, rationale="test rationale")


def test_target_weight_is_capped_by_confidence_and_order_limit():
    # Asks 20%; confidence 0.5 caps it at 10%; the 10% order limit agrees: 100,000 PKR.
    action, qty, _ = size(intent(weight=20), "bullish", 0.5, pf(), "LUCK", 100.0)
    assert (action, qty) == ("buy", 1_000)


def test_existing_position_counts_toward_the_target():
    # Equity 1.05M, holding 50K (4.8%); target 10% -> buy about 55K more.
    action, qty, _ = size(intent(weight=10), "bullish", 1.0, pf(cash=1_000_000, held=500),
                          "LUCK", 100.0)
    assert action == "buy" and qty == 550


@pytest.mark.parametrize("act, stance, held, why", [
    ("buy", "bearish", 0, "overridden"),     # model contradicts the brief
    ("sell", "bearish", 0, "no short"),      # nothing to sell
    ("buy", "bullish", 2_000, "already met"),  # already at 20% > target
])
def test_code_overrides_intents_it_cannot_carry_out(act, stance, held, why):
    action, qty, reason = size(intent(act, 10), stance, 0.6, pf(cash=800_000, held=held),
                               "LUCK", 100.0)
    assert (action, qty) == ("hold", 0) and why in reason


def test_sell_never_exceeds_the_holding():
    action, qty, _ = size(intent("sell", 0), "bearish", 0.7, pf(held=300), "LUCK", 100.0)
    assert (action, qty) == ("sell", 300)


# ---------------------------------------------------------------- one call, many accounts


async def test_one_model_call_then_a_proposal_sized_for_each_account(monkeypatch):
    import portfolio_agent.main as pa

    chats, proposed = [], []

    class Reply:
        message = type("M", (), {"content": '{"action": "buy", "target_weight_pct": 10, '
                                            '"rationale": "strong brief, test"}'})()
        usage = type("U", (), {"total_tokens": 321})()

    class Gateway:
        def __init__(self, *a, **kw):
            pass

        async def chat(self, messages, **kw):
            chats.append(messages)
            return Reply()

        async def aclose(self):
            pass

    books = {"u_rich": pf(cash=1_000_000), "u_full": pf(cash=1_000_000, held=2_000),
             "u_poor": pf(cash=50.0)}

    class Toolset:
        def __init__(self, url, allow):
            self.allow = allow

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def call(self, tool, **kw):
            assert tool in self.allow
            if tool == "get_valuation":
                return {"values": {"price": 100.0}, "stale": False}
            if tool == "interested_accounts":
                return list(books)
            if tool == "get_portfolio":
                return books[kw["account"]].model_dump()
            if tool == "propose_order":
                proposed.append(kw)
                return {"id": "p", "status": "pending"}
            raise AssertionError(tool)

    monkeypatch.setattr(pa, "GatewayClient", Gateway)
    monkeypatch.setattr(pa, "McpToolset", Toolset)
    brief = {"stance": "bullish", "confidence": 1.0, "thesis": "t", "key_points": [],
             "risks": []}
    out = await pa.propose_trade({"run_id": "r1", "symbol": "LUCK", "brief": brief,
                                  "risk": {"actionable": True, "verdict": "ok", "flags": []}})
    assert len(chats) == 1  # the judgement is made once, however many users
    # sized per account: the full one is at target, the poor one cannot afford a share
    assert [(p["account"], p["quantity"], p["client_order_id"]) for p in proposed] == [
        ("u_rich", 1_000, "run:r1:u_rich")]
    assert out["action"] == "buy" and out["accounts"] == 1 and "u_rich" not in str(out)


# ---------------------------------------------------------------- dashboard

PW = "correct horse battery"
H = {"x-desk-request": "1"}


async def _no_sleep(_s):
    return None


@pytest.fixture
async def dash(monkeypatch, pg_url):
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET", "s" * 40)
    import dashboard.main as d
    from dashboard.auth import UserStore

    admin = await asyncpg.connect(pg_url)
    await admin.execute("TRUNCATE users, sessions CASCADE")
    users = await UserStore.connect(pg_url)
    d.app.state.users = users
    d.limiter.redis, d.limiter.mem = None, {}
    calls = []

    async def fake_broker(tool, **args):
        calls.append((tool, args))
        if tool in ("list_proposals", "get_watchlist"):
            return []
        if tool in ("open_account", "init_watchlist"):
            return True
        if tool == "decide":
            return {"proposal": {"symbol": "LUCK", "status": "rejected"}, "reason": None}
        raise AssertionError(tool)

    monkeypatch.setattr(d, "broker", fake_broker)
    monkeypatch.setattr(d.asyncio, "sleep", _no_sleep)
    yield d, calls, admin
    await users.close()
    await admin.close()


def client(d) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(d.app), base_url="http://dash")


async def signup(c, email="a@example.com", name="Ayesha", password=PW):
    return await c.post("/api/auth/signup", json={"email": email, "name": name,
                                                  "password": password}, headers=H)


def test_access_tokens_are_signed_and_expire():
    from dashboard.auth import ACCESS_S, User, issue_access, read_access

    u = User(id="00000000-0000-4000-8000-000000000001", email="a@x.io", name="A",
             account="u_a")
    tok = issue_access("k" * 40, u)
    assert read_access("k" * 40, tok) == u
    assert read_access("z" * 40, tok) is None  # another key
    head, body, sig = tok.split(".")
    assert read_access("k" * 40, f"{head}.{body}.{sig[::-1]}") is None  # edited
    old = issue_access("k" * 40, u, now=1_000_000)
    assert read_access("k" * 40, old) is None and ACCESS_S == 900  # expired
    assert read_access("k" * 40, "garbage") is None


async def test_sign_up_opens_an_account_and_the_request_header_is_required(dash):
    d, calls, _ = dash
    body = {"approve": False, "note": ""}
    pid = "00000000-0000-0000-0000-000000000001"
    async with client(d) as c:
        assert (await c.post(f"/api/proposals/{pid}/decide", json=body, headers=H)
                ).status_code == 401
        r = await c.post("/api/auth/signup", json={"email": "a@example.com", "name": "A",
                                                   "password": PW})
        assert r.status_code == 403  # a cross-site form cannot set the header
        assert (await signup(c, password="short")).status_code == 422  # 10+ characters
        r = await signup(c)
        assert r.status_code == 200 and {"desk_access", "desk_refresh"} <= set(r.cookies)
        acct = r.json()["account"]
        assert acct.startswith("u_") and acct != "operator"
        assert ("open_account", {"account": acct}) in calls  # their own demo money
        assert (await signup(c)).status_code == 409  # one account per email

        me = (await c.get("/api/me")).json()
        assert (me["name"], me["email"], me["account"]) == ("Ayesha", "a@example.com", acct)
        r = await c.post(f"/api/proposals/{pid}/decide", json=body)  # signed in, no header
        assert r.status_code == 403
        assert not [t for t, _ in calls if t == "decide"]  # nothing reached the broker
        assert (await c.post(f"/api/proposals/{pid}/decide", json=body, headers=H)
                ).status_code == 200
    assert calls[-1] == ("decide", {"account": acct, "proposal_id": pid, "approve": False,
                                    "decided_by": acct, "note": ""})


async def test_login_checks_the_password_and_is_rate_limited(dash):
    d, _, _ = dash
    async with client(d) as c:
        await signup(c)
    async with client(d) as c:
        r = await c.post("/api/auth/login", json={"email": "A@Example.com", "password": PW},
                         headers=H)
        assert r.status_code == 200  # emails are case-insensitive
        for _ in range(8):
            r = await c.post("/api/auth/login", json={"email": "a@example.com",
                                                      "password": "wrong password"},
                             headers=H)
            assert r.status_code == 401 and r.json()["detail"] == "Wrong email or password"
        r = await c.post("/api/auth/login", json={"email": "nobody@example.com",
                                                  "password": PW}, headers=H)
        assert r.status_code == 401  # the same answer for an unknown email
        r = await c.post("/api/auth/login", json={"email": "a@example.com", "password": PW},
                         headers=H)
        assert r.status_code == 200  # the 10th try on this email
        r = await c.post("/api/auth/login", json={"email": "a@example.com", "password": PW},
                         headers=H)
        assert r.status_code == 429  # 10 tries per email per 15 minutes, right or wrong


async def test_refresh_rotates_and_a_reused_token_ends_the_session(dash):
    d, _, admin = dash
    async with client(d) as c:
        first = (await signup(c)).cookies["desk_refresh"]
        r = await c.post("/api/auth/refresh", headers=H)
        assert r.status_code == 200
        second = r.cookies["desk_refresh"]
        assert second != first

    async with client(d) as other:  # a copy of the first token, replayed at once,
        other.cookies.set("desk_refresh", first, path="/api/auth")
        assert (await other.post("/api/auth/refresh", headers=H)).status_code == 409
        # is a race between two tabs (retry). The same replay later is theft:
        await admin.execute("UPDATE sessions SET used_at = now() - interval '1 minute' "
                            "WHERE used_at IS NOT NULL")
        assert (await other.post("/api/auth/refresh", headers=H)).status_code == 401

    async with client(d) as c:  # the whole family is revoked: the real user's token too
        c.cookies.set("desk_refresh", second, path="/api/auth")
        assert (await c.post("/api/auth/refresh", headers=H)).status_code == 401


async def test_logout_revokes_the_refresh_token(dash):
    d, _, _ = dash
    async with client(d) as c:
        token = (await signup(c)).cookies["desk_refresh"]
        assert (await c.post("/api/auth/logout", headers=H)).status_code == 200
    async with client(d) as c:
        c.cookies.set("desk_refresh", token, path="/api/auth")
        assert (await c.post("/api/auth/refresh", headers=H)).status_code == 401


async def test_two_users_reach_only_their_own_account(dash):
    d, calls, _ = dash
    async with client(d) as a, client(d) as b:
        acct_a = (await signup(a, "a@example.com")).json()["account"]
        acct_b = (await signup(b, "b@example.com", "Bilal")).json()["account"]
        calls.clear()
        await a.get("/api/watchlist")
        await b.get("/api/proposals")
    assert calls == [("get_watchlist", {"account": acct_a}),
                     ("list_proposals", {"account": acct_b, "status": None, "limit": 100})]


async def test_the_operator_signs_in_with_the_env_password(dash):
    d, _, _ = dash
    await d.app.state.users.ensure_operator("op@x.io", "first-password", account="operator")
    await d.app.state.users.ensure_operator("op@x.io", "rotated-password", account="operator")
    async with client(d) as c:
        r = await c.post("/api/auth/login", json={"email": "op@x.io",
                                                  "password": "rotated-password"}, headers=H)
        assert r.status_code == 200 and r.json()["account"] == "operator"
        r = await c.post("/api/auth/login", json={"email": "op@x.io",
                                                  "password": "first-password"}, headers=H)
        assert r.status_code == 401  # .env is the source of truth


def test_live_events_go_only_to_the_account_they_concern():
    from dashboard.main import Hub

    hub = Hub()
    qa, qb = asyncio.Queue(), asyncio.Queue()
    hub.clients = {qa: "u_a", qb: "u_b"}
    hub.route([{"account": "u_a", "action": "order.filled", "details": {}},
               {"account": "u_b", "action": "alert.triggered",
                "details": {"symbol": "LUCK", "price": 90.0, "drop_pct": 10.0,
                            "loss_pkr": None, "event_id": 7}},
               {"account": None, "action": "audit.something", "details": {}}])
    a = [qa.get_nowait()["type"] for _ in range(qa.qsize())]
    b = [qb.get_nowait() for _ in range(qb.qsize())]
    assert a == ["portfolio"]
    assert [m["type"] for m in b] == ["alert"] and b[0]["data"]["kind"] == "triggered"


async def test_a_blank_or_spaced_out_name_is_handled_not_a_server_error(dash):
    d, _, _ = dash
    async with client(d) as c:
        r = await signup(c, name="    ")
        assert r.status_code == 422 and r.json()["detail"] == "Enter your name"
        r = await signup(c, email="b@example.com", name="  Ayesha   Khan ")
        assert r.status_code == 200 and r.json()["name"] == "Ayesha Khan"


async def test_a_typo_ticker_is_not_researched_and_has_no_stock_page(dash, monkeypatch):
    d, _, _ = dash
    monkeypatch.setattr(d.hub, "board", {"stocks": [{"symbol": "LUCK", "name": "Lucky"}]})
    started = []

    async def fake_orch(method, path, **kw):
        if path == "/v1/briefs":
            started.append(kw["json"]["symbol"])
            return {"run_id": "r1", "created": True}
        return {"briefs": []}

    async def fake_broker(tool, **args):
        if tool == "get_portfolio":
            return {"positions": held, "cash": 1.0, "equity": 1.0}
        return [] if tool in ("list_alerts", "get_watchlist") else True

    async def no_history(symbol, days):
        return None

    held = []
    monkeypatch.setattr(d, "orch", fake_orch)
    monkeypatch.setattr(d, "broker", fake_broker)
    monkeypatch.setattr(d, "history", no_history)
    async with client(d) as c:
        await signup(c)
        r = await c.post("/api/research", json={"symbol": "ZZZQ"}, headers=H)
        assert r.status_code == 404 and started == []  # no run that can never find data
        r = await c.post("/api/research", json={"symbol": "luck"}, headers=H)
        assert r.status_code == 200 and started == ["LUCK"]
        assert (await c.get("/api/stocks/ZZZQ")).status_code == 404
        assert (await c.get("/api/stocks/LUCK")).status_code == 200
        held.append({"symbol": "OLDCO", "quantity": 5, "avg_cost": 10.0})
        # held but no longer on the board (delisted): the page still opens
        assert (await c.get("/api/stocks/OLDCO")).status_code == 200
