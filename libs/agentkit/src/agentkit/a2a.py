"""A2A: how agents talk to agents.

Server side (`build_a2a_app`): wraps a plain async handler as an A2A service.
  * publishes an Agent Card at /.well-known/agent-card.json: name, version,
    endpoint, and the *skills* the agent offers;
  * speaks JSON-RPC 2.0 (`SendMessage`, `GetTask`, ...) at /;
  * runs each request as a Task with a lifecycle:
        submitted -> working -> completed
                             -> failed    (the agent broke)
                  -> rejected             (the request was invalid)

Client side (`AgentDirectory`): discovery and routing.
  * reads each known agent's card and indexes agents by skill id;
  * callers ask for a *skill* ("technical_analysis"), never a hostname, so an
    agent can be replaced, moved or versioned without touching callers;
  * turns non-completed task states into typed exceptions.

Why A2A rather than plain REST between our services: the card makes agents
self-describing and discoverable, the task model gives a standard way to report
long-running work and partial failure, and any A2A-speaking agent (another team's,
another vendor's) can join without a bespoke integration.

Streaming (A2A `SendStreamingMessage`): a handler that takes an `emit` argument can
report progress while it works. Each emit becomes a TaskStatusUpdateEvent (state still
`working`) carrying a small JSON data part, sent to the caller over server-sent events
as it happens; the result arrives as usual, as the task's artifact. The card advertises
`capabilities.streaming` so clients know they may ask for it. Callers that do not
stream get the same final result.

Trace context rides in the HTTP `traceparent` header, which our httpx and FastAPI
instrumentation inject and extract (unlike MCP, which carries it in `_meta`).
"""

import inspect
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.helpers import (
    get_data_parts,
    get_text_parts,
    new_data_part,
    new_task_from_user_message,
    new_text_part,
)
from a2a.server.agent_execution import AgentExecutor
from a2a.server.request_handlers import DefaultRequestHandlerV2
from a2a.server.routes import (
    add_a2a_routes_to_fastapi,
    create_agent_card_routes,
    create_jsonrpc_routes,
)
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Message,
    Role,
    SendMessageRequest,
    TaskState,
)
from fastapi import FastAPI
from pydantic import ValidationError

from agentkit.auth import bearer_for, guard_http, load_verifier

log = logging.getLogger(__name__)

Handler = Callable[..., Awaitable[dict[str, Any]]]  # (payload) or (payload, emit)
Emit = Callable[[dict[str, Any]], Awaitable[None]]


def _streams(handler: Handler) -> bool:
    return "emit" in inspect.signature(handler).parameters


class InvalidRequest(Exception):
    """Raise from a handler when the input is the caller's fault: the task is rejected."""


# ---------------------------------------------------------------- server


@dataclass
class Skill:
    id: str
    name: str
    description: str
    tags: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)


@dataclass
class A2AService:
    name: str
    description: str
    version: str
    url: str  # the URL other services use to reach this agent
    skills: list[Skill]
    # One handler for a single-skill agent, or {skill id: handler}. A2A messages do
    # not name a skill, so the directory puts the requested skill id in the
    # message's `metadata` and the executor routes on it.
    handler: Handler | dict[str, Handler]

    def card(self) -> AgentCard:
        return AgentCard(
            name=self.name,
            description=self.description,
            version=self.version,
            supported_interfaces=[AgentInterface(url=self.url, protocol_binding="JSONRPC",
                                                 protocol_version="1.0")],
            capabilities=AgentCapabilities(streaming=any(_streams(h) for h in (
                self.handler.values() if isinstance(self.handler, dict) else [self.handler]))),
            default_input_modes=["application/json"],
            default_output_modes=["application/json"],
            skills=[AgentSkill(id=s.id, name=s.name, description=s.description,
                               tags=s.tags, examples=s.examples) for s in self.skills],
        )


class _HandlerExecutor(AgentExecutor):
    def __init__(self, handler: Handler | dict[str, Handler]) -> None:
        self.handler = handler

    def _route(self, message: Message) -> Handler | None:
        if not isinstance(self.handler, dict):
            return self.handler
        # protobuf Struct has no .get()
        skill = message.metadata["skill"] if "skill" in message.metadata else None  # noqa: SIM401
        if skill is None and len(self.handler) == 1:
            return next(iter(self.handler.values()))
        return self.handler.get(skill)

    async def execute(self, context, event_queue) -> None:
        task = context.current_task
        if task is None:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)  # state: submitted
        updater = TaskUpdater(event_queue, task.id, task.context_id)

        def say(text: str) -> Message:
            return updater.new_agent_message([new_text_part(text[:2000])])

        data = get_data_parts(context.message.parts)
        if not data or not isinstance(data[0], dict):
            await updater.reject(say("expected one JSON object as a data part"))
            return

        handler = self._route(context.message)
        if handler is None:
            await updater.reject(say("unknown or missing skill in message metadata"))
            return

        await updater.start_work()  # state: working

        async def emit(event: dict[str, Any]) -> None:
            await updater.update_status(
                TaskState.TASK_STATE_WORKING,
                message=updater.new_agent_message([new_data_part(event)]))

        try:
            result = await (handler(data[0], emit=emit) if _streams(handler)
                            else handler(data[0]))
        except (InvalidRequest, ValidationError) as e:
            await updater.reject(say(f"invalid request: {e}"))
            return
        except Exception as e:
            log.exception("handler failed")
            await updater.failed(say(f"{type(e).__name__}: {e}"))
            return
        await updater.add_artifact([new_data_part(result)], name="result")
        await updater.complete()

    async def cancel(self, context, event_queue) -> None:
        if context.current_task is not None:
            await TaskUpdater(event_queue, context.current_task.id,
                              context.current_task.context_id).cancel()


def build_a2a_app(service: A2AService) -> FastAPI:
    card = service.card()
    handler = DefaultRequestHandlerV2(
        agent_executor=_HandlerExecutor(service.handler),
        # In memory: a task is lost if the agent restarts. Slice 2 makes the
        # *orchestrator's* workflow durable instead, which is where it matters.
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    app = FastAPI(title=service.name, version=service.version)
    # The card stays public (discovery); sending work needs `agent:call`, which
    # config/access.toml grants to the orchestrator alone.
    guard_http(app, load_verifier(service.name), {"POST /": "agent:call"})
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(card),
        jsonrpc_routes=create_jsonrpc_routes(handler, "/"),
    )

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    return app


# ---------------------------------------------------------------- client


class SkillNotFound(Exception):
    pass


class AgentTaskFailed(Exception):
    def __init__(self, agent: str, state: str, message: str) -> None:
        super().__init__(f"{agent}: task {state}: {message}")
        self.agent, self.state, self.message = agent, state, message


@dataclass
class _Entry:
    url: str
    card: AgentCard


class AgentDirectory:
    """Static list of agent base URLs in; a skill -> agent index out."""

    def __init__(self, urls: list[str], http: httpx.AsyncClient | None = None,
                 timeout_s: float = 600.0) -> None:
        self.urls = urls
        self.http = http or httpx.AsyncClient(timeout=timeout_s, auth=bearer_for())
        self._by_skill: dict[str, _Entry] = {}
        self.unreachable: dict[str, str] = {}

    async def refresh(self) -> None:
        by_skill: dict[str, _Entry] = {}
        self.unreachable = {}
        for url in self.urls:
            try:
                card = await A2ACardResolver(self.http, url).get_agent_card()
            except Exception as e:  # noqa: BLE001  one bad agent must not hide the others
                self.unreachable[url] = f"{type(e).__name__}: {e}"
                continue
            for skill in card.skills:
                if skill.id in by_skill:
                    log.warning("skill %s offered by more than one agent; keeping first",
                                skill.id)
                    continue
                by_skill[skill.id] = _Entry(url, card)
        self._by_skill = by_skill

    def listing(self) -> list[dict[str, Any]]:
        return [{"skill": s, "agent": e.card.name, "version": e.card.version, "url": e.url}
                for s, e in sorted(self._by_skill.items())]

    async def call(self, skill: str, payload: dict[str, Any]) -> dict[str, Any]:
        entry = self._by_skill.get(skill)
        if entry is None:
            await self.refresh()  # the agent may have come up since we last looked
            entry = self._by_skill.get(skill)
        if entry is None:
            raise SkillNotFound(f"no reachable agent offers skill {skill!r}")

        client = await create_client(entry.card,
                                     ClientConfig(httpx_client=self.http, streaming=False))
        message = Message(message_id=str(uuid.uuid4()), role=Role.ROLE_USER,
                          parts=[new_data_part(payload)])
        message.metadata.update({"skill": skill})
        request = SendMessageRequest(message=message)
        task = None
        async for event in client.send_message(request):
            if event.HasField("task"):
                task = event.task
        if task is None:
            raise AgentTaskFailed(entry.card.name, "no_task", "agent returned no task")

        state = TaskState.Name(task.status.state)
        if task.status.state != TaskState.TASK_STATE_COMPLETED:
            text = " ".join(get_text_parts(task.status.message.parts))
            raise AgentTaskFailed(entry.card.name, state, text or "no detail")
        for artifact in task.artifacts:
            for data in get_data_parts(artifact.parts):
                if isinstance(data, dict):
                    return data
        raise AgentTaskFailed(entry.card.name, state, "completed without a data artifact")

    async def stream(self, skill: str, payload: dict[str, Any]
                     ) -> AsyncIterator[dict[str, Any]]:
        """Call a skill and yield its progress events as they happen, then
        {"type": "result", "data": <the result>}. Same errors as `call`."""
        entry = await self._entry(skill)
        client = await create_client(entry.card,
                                     ClientConfig(httpx_client=self.http, streaming=True))
        message = Message(message_id=str(uuid.uuid4()), role=Role.ROLE_USER,
                          parts=[new_data_part(payload)])
        message.metadata.update({"skill": skill})
        result: dict[str, Any] | None = None
        failed = {TaskState.TASK_STATE_FAILED, TaskState.TASK_STATE_REJECTED,
                  TaskState.TASK_STATE_CANCELED}
        async for event in client.send_message(SendMessageRequest(message=message)):
            status = None
            if event.HasField("status_update"):
                status = event.status_update.status
            elif event.HasField("task"):
                status = event.task.status
                for artifact in event.task.artifacts:
                    result = next((d for d in get_data_parts(artifact.parts)
                                   if isinstance(d, dict)), result)
            elif event.HasField("artifact_update"):
                result = next((d for d in get_data_parts(event.artifact_update.artifact.parts)
                               if isinstance(d, dict)), result)
            if status is None:
                continue
            if status.state in failed:
                text = " ".join(get_text_parts(status.message.parts))
                raise AgentTaskFailed(entry.card.name, TaskState.Name(status.state),
                                      text or "no detail")
            if status.state == TaskState.TASK_STATE_WORKING and status.HasField("message"):
                for d in get_data_parts(status.message.parts):
                    if isinstance(d, dict):
                        yield d
        if result is None:
            raise AgentTaskFailed(entry.card.name, "no_result", "stream ended without a result")
        yield {"type": "result", "data": result}

    async def _entry(self, skill: str) -> _Entry:
        entry = self._by_skill.get(skill)
        if entry is None:
            await self.refresh()
            entry = self._by_skill.get(skill)
        if entry is None:
            raise SkillNotFound(f"no reachable agent offers skill {skill!r}")
        return entry

    async def aclose(self) -> None:
        await self.http.aclose()
