"""A2A services and discovery through the real SDK, over in-process ASGI transports."""

import httpx
import pytest
from agentkit.a2a import (
    A2AService,
    AgentDirectory,
    AgentTaskFailed,
    InvalidRequest,
    Skill,
    SkillNotFound,
    build_a2a_app,
)


async def echo(payload):
    return {"echo": payload, "count": 3}


async def broken(payload):
    raise RuntimeError("model gateway unreachable")


async def picky(payload):
    raise InvalidRequest("symbol is required")


def app(name, skill, handler):
    return build_a2a_app(A2AService(name=name, description="test agent", version="1.2.0",
                                    url=f"http://{name}/", skills=[Skill(skill, skill, "d")],
                                    handler=handler))


@pytest.fixture
async def directory():
    http = httpx.AsyncClient(mounts={
        "http://echo": httpx.ASGITransport(app("echo", "echo_skill", echo)),
        "http://broken": httpx.ASGITransport(app("broken", "broken_skill", broken)),
        "http://picky": httpx.ASGITransport(app("picky", "picky_skill", picky)),
    })
    d = AgentDirectory(["http://echo", "http://broken", "http://picky", "http://down.invalid"],
                       http=http)
    await d.refresh()
    yield d
    await d.aclose()


async def test_agent_card_is_published_at_the_well_known_path():
    transport = httpx.ASGITransport(app("echo", "echo_skill", echo))
    async with httpx.AsyncClient(transport=transport, base_url="http://echo") as c:
        card = (await c.get("/.well-known/agent-card.json")).json()
    assert card["name"] == "echo" and card["version"] == "1.2.0"
    assert card["skills"][0]["id"] == "echo_skill"
    assert card["supportedInterfaces"][0]["protocolBinding"] == "JSONRPC"


async def test_discovery_indexes_skills_and_records_unreachable_agents(directory):
    assert {s["skill"] for s in directory.listing()} == {"echo_skill", "broken_skill",
                                                         "picky_skill"}
    assert "http://down.invalid" in directory.unreachable


async def test_completed_task_returns_artifact_data_with_ints_as_floats(directory):
    out = await directory.call("echo_skill", {"symbol": "LUCK"})
    assert out["echo"] == {"symbol": "LUCK"}
    assert out["count"] == 3.0 and isinstance(out["count"], float)  # protobuf Value


async def test_handler_crash_becomes_a_failed_task(directory):
    with pytest.raises(AgentTaskFailed) as e:
        await directory.call("broken_skill", {"symbol": "LUCK"})
    assert e.value.state == "TASK_STATE_FAILED" and "unreachable" in e.value.message


async def test_invalid_input_becomes_a_rejected_task(directory):
    with pytest.raises(AgentTaskFailed) as e:
        await directory.call("picky_skill", {})
    assert e.value.state == "TASK_STATE_REJECTED"


async def test_unknown_skill_is_an_explicit_error(directory):
    with pytest.raises(SkillNotFound):
        await directory.call("astrology", {})


async def test_one_service_can_host_several_skills_routed_by_metadata():
    async def bull(p):
        return {"side": "bull", **p}

    async def bear(p):
        return {"side": "bear", **p}

    service = build_a2a_app(A2AService(
        name="debate", description="d", version="1", url="http://debate/",
        skills=[Skill("bull_case", "Bull", "d"), Skill("bear_case", "Bear", "d")],
        handler={"bull_case": bull, "bear_case": bear}))
    http = httpx.AsyncClient(mounts={"http://debate": httpx.ASGITransport(service)})
    d = AgentDirectory(["http://debate"], http=http)
    await d.refresh()
    assert {s["skill"] for s in d.listing()} == {"bull_case", "bear_case"}
    assert (await d.call("bear_case", {"x": 1}))["side"] == "bear"
    assert (await d.call("bull_case", {"x": 1}))["side"] == "bull"
    await d.aclose()
