"""Service identity: signed tokens, audience binding, receiver-side policy, and the
same checks through the real MCP SDK over HTTP."""

import time
from contextlib import asynccontextmanager

import httpx
import httpx2
import jwt
import pytest
from agentkit.auth import (
    AuthError,
    BearerAuth,
    Identity,
    McpGuard,
    Verifier,
    audience_for,
    generate,
    guard_http,
    load_private_key,
    load_public_keys,
)
from agentkit.mcp_tools import McpToolError, McpToolset
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, Request
from helpers import FlakySource
from market_data_mcp.server import build_server
from marketdata import MarketData
from mcp.client.streamable_http import streamable_http_client
from mcp.server.transport_security import TransportSecuritySettings

TECH, NEWS, ROGUE = (Identity(n, Ed25519PrivateKey.generate())
                     for n in ("technical-agent", "news-agent", "rogue"))


def verifier(service="market-data", grants=None) -> Verifier:
    return Verifier(service, {i.name: i.key.public_key() for i in (TECH, NEWS)},
                    grants or {"technical-agent": frozenset({"market:read"}),
                               "news-agent": frozenset({"news:read"})})


def test_valid_token_yields_the_callers_name_and_granted_scopes():
    p = verifier().check(f"Bearer {TECH.mint('market-data')}", "market:read")
    assert p.name == "technical-agent" and p.scopes == {"market:read"}


@pytest.mark.parametrize("token, status, why", [
    (lambda: TECH.mint("broker"), 401, "(?i)audience"),           # minted for someone else
    (lambda: ROGUE.mint("market-data"), 401, "unknown signer"),  # key we don't know
    (lambda: TECH.mint("market-data", ttl_s=-60), 401, "expired"),
    (lambda: TECH.mint("market-data", ttl_s=3600), 401, "lifetime"),
    (lambda: TECH.mint("market-data")[:-4] + "AAAA", 401, "Signature"),
])
def test_bad_tokens_are_refused(token, status, why):
    with pytest.raises(AuthError, match=why) as e:
        verifier().verify(token())
    assert e.value.status == status


def test_a_token_signed_by_one_service_cannot_claim_to_be_another():
    now = int(time.time())
    forged = jwt.encode({"iss": "technical-agent", "sub": "technical-agent",
                         "aud": "market-data", "iat": now, "exp": now + 60},
                        NEWS.key, algorithm="EdDSA", headers={"kid": "technical-agent"})
    with pytest.raises(AuthError, match="Signature"):
        verifier().verify(forged)


def test_authenticated_but_not_permitted_is_403_not_401():
    v = verifier(grants={"technical-agent": frozenset({"market:read"})})
    with pytest.raises(AuthError) as e:
        v.verify(NEWS.mint("market-data"))  # valid signature, no grants here
    assert e.value.status == 403
    with pytest.raises(AuthError, match="lacks scope") as e:
        v.check(f"Bearer {TECH.mint('market-data')}", "evidence:read")
    assert e.value.status == 403


def test_audience_is_the_service_name_even_through_a_loopback_port():
    assert audience_for("http://market-data:8000/mcp") == "market-data"
    assert audience_for("http://127.0.0.1:8600/mcp") == "broker"


def test_keygen_round_trip(tmp_path):
    assert generate(["a", "b"], tmp_path) == ["a", "b"]
    assert generate(["a", "b"], tmp_path) == []  # never overwrites without --rotate
    ident = Identity("a", load_private_key(tmp_path / "private" / "a.key"))
    v = Verifier("x", load_public_keys(tmp_path / "public"), {"a": frozenset({"s"})})
    assert v.check(f"Bearer {ident.mint('x')}", "s").name == "a"


async def test_http_guard_leaves_open_paths_open_and_protects_the_rest():
    app = FastAPI()
    guard_http(app, verifier("technical-agent", {"technical-agent": frozenset({"agent:call"})}),
               {"POST /": "agent:call"})

    @app.get("/.well-known/agent-card.json")
    async def card() -> dict:
        return {"name": "technical-agent"}

    @app.post("/")
    async def rpc(request: Request) -> dict:
        return {"caller": request.state.principal.name}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                 base_url="http://technical-agent") as c:
        assert (await c.get("/.well-known/agent-card.json")).status_code == 200
        r = await c.post("/", json={})
        assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
        r = await c.post("/", json={}, auth=BearerAuth(TECH))
        assert r.status_code == 200 and r.json() == {"caller": "technical-agent"}
        r = await c.post("/", json={}, auth=BearerAuth(NEWS))
        assert r.status_code == 403  # a known signer, but granted nothing on this agent


# ---------------------------------------------------------------- MCP, over real HTTP


@asynccontextmanager
async def mcp_app(tmp_path):
    """Real streamable-HTTP app with the SDK's bearer middleware, in process. A context
    manager, not a fixture: the app's task group must open and close in one task."""
    server = build_server(MarketData(FlakySource(), tmp_path),
                          guard=McpGuard("market-data", verifier()))
    security = TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                         allowed_hosts=["market-data"])
    app = server.streamable_http_app(stateless_http=True, json_response=True,
                                     transport_security=security)
    async with app.router.lifespan_context(app):
        yield app


def toolset(app, identity: Identity, allow: set[str]) -> McpToolset:
    http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app), auth=BearerAuth(identity))
    return McpToolset(streamable_http_client("http://market-data/mcp", http_client=http),
                      allow=allow)


async def test_mcp_tool_runs_for_a_caller_holding_its_scope(tmp_path):
    async with mcp_app(tmp_path) as app, toolset(app, TECH, {"get_price_summary"}) as ts:
        ev = await ts.call("get_price_summary", symbol="TEST")
    assert ev["symbol"] == "TEST"


async def test_mcp_tool_refuses_a_valid_caller_without_its_scope(tmp_path):
    # The news agent is authenticated, but holds news:read only: no prices.
    async with mcp_app(tmp_path) as app,             toolset(app, NEWS, {"get_price_summary", "get_announcements"}) as ts:
        with pytest.raises(McpToolError, match="lacks scope 'market:read'"):
            await ts.call("get_price_summary", symbol="TEST")


async def test_mcp_without_a_valid_token_is_401_before_any_tool_runs(tmp_path):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    headers = {"accept": "application/json, text/event-stream"}
    async with mcp_app(tmp_path) as app, httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app), base_url="http://market-data") as c:
        assert (await c.post("/mcp", json=body, headers=headers)).status_code == 401
        rogue = {**headers, "authorization": f"Bearer {ROGUE.mint('market-data')}"}
        assert (await c.post("/mcp", json=body, headers=rogue)).status_code == 401
