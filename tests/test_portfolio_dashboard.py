"""The portfolio agent's sizing (code decides shares, not the model) and the dashboard's
human-side security: signed sessions, CSRF, no decision without a login."""

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


# ---------------------------------------------------------------- dashboard


@pytest.fixture
def dash(monkeypatch):
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET", "s" * 32)
    monkeypatch.setenv("DASHBOARD_PASSWORD", "test-password")
    import dashboard.main as d

    monkeypatch.setattr(d, "USER", "tester")
    calls = []

    async def fake_broker(tool, **args):
        calls.append((tool, args))
        if tool in ("list_proposals", "get_watchlist"):
            return []
        if tool == "decide":
            return {"proposal": {"symbol": "LUCK", "status": "rejected"}, "reason": None}
        raise AssertionError(tool)

    monkeypatch.setattr(d, "broker", fake_broker)
    return d, calls


def test_session_cookie_is_signed_and_expires(dash):
    d, _ = dash
    cookie = d.make_session("tester", now=1_000)
    assert d.read_session(cookie, now=2_000) == "tester"
    assert d.read_session(cookie, now=1_000 + d.SESSION_S + 1) is None  # expired
    _, exp, sig = cookie.split("|")
    assert d.read_session(f"admin|{exp}|{sig}", now=2_000) is None  # edited user
    assert d.read_session("garbage", now=2_000) is None


async def test_no_login_no_decision_and_the_request_header_is_required(dash):
    d, calls = dash
    h = {"x-desk-request": "1"}
    body = {"approve": False, "note": ""}
    pid = "00000000-0000-0000-0000-000000000001"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(d.app),
                                 base_url="http://dash") as c:
        r = await c.post(f"/api/proposals/{pid}/decide", json=body, headers=h)
        assert r.status_code == 401
        r = await c.post("/api/login", json={"user": "tester", "password": "x"})
        assert r.status_code == 403  # a cross-site form cannot set the header
        r = await c.post("/api/login", json={"user": "tester", "password": "wrong"}, headers=h)
        assert r.status_code == 401
        r = await c.post("/api/login", json={"user": "tester", "password": "test-password"},
                         headers=h)
        assert r.status_code == 200 and "desk_session" in r.cookies
        assert (await c.get("/api/me")).json()["user"] == "tester"
        r = await c.post(f"/api/proposals/{pid}/decide", json=body)  # signed in, no header
        assert r.status_code == 403
        assert not [t for t, _ in calls if t == "decide"]  # nothing reached the broker

        r = await c.post(f"/api/proposals/{pid}/decide", json=body, headers=h)
        assert r.status_code == 200
    assert calls[-1] == ("decide", {"proposal_id": pid, "approve": False,
                                    "decided_by": "tester", "note": ""})
