"""Long-term memory as an MCP server: what the desk concluded before, searchable by meaning.

Two tools:
  * recall(query, symbol?, k)  read-only; top-k past views ranked by similarity
  * remember(...)              append-only and idempotent (same key -> stored once)

Design choices:
  * Embeddings are computed locally (fastembed, BAAI/bge-small-en-v1.5, 384 dims,
    CPU, ONNX). Groq has no embeddings endpoint, and a local model costs no quota.
  * Postgres + pgvector, not a separate vector database: one less system to run,
    and memories sit next to the runs they came from. At millions of rows a
    dedicated store (or an HNSW index here) would earn its place.
  * Recall is top-k, never "load everything": memory must not crowd today's
    evidence out of an 8K-token window.
  * Memory is a poisoning risk: a wrong conclusion stored today is "context"
    tomorrow. So only the orchestrator writes, only after the verifier and the
    risk gate, and readers are told prior views are context, not evidence: they
    have no evidence id and cannot be cited.
  * Since Slice 4, calls carry signed tokens: `recall` needs memory:read (the debate
    agent), `remember` needs memory:write (the orchestrator alone).
"""

import hashlib
import logging
import math
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol

import asyncpg
import uvicorn
from agentkit.auth import McpGuard
from agentkit.tracing import setup_tracing
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

log = logging.getLogger("memory_mcp")

DIM = 384
MODEL = "BAAI/bge-small-en-v1.5"
SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,12}$")


class MemoryRecord(BaseModel):
    key: str
    symbol: str
    kind: str
    as_of: str
    stance: str
    confidence: float
    text: str
    similarity: float | None = None


class Stored(BaseModel):
    stored: bool = Field(description="false if this key was already remembered")


# ---------------------------------------------------------------- embeddings


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class FastEmbedder:
    def __init__(self, model: str = MODEL) -> None:
        from fastembed import TextEmbedding

        self._model = TextEmbedding(model, cache_dir=os.getenv("FASTEMBED_CACHE_PATH"))

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(map(float, v)) for v in self._model.embed(texts)]


class HashEmbedder:
    """Deterministic bag-of-words vectors: for tests, no model download."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            v = [0.0] * DIM
            for word in re.findall(r"[a-z]+", text.lower()):
                v[int(hashlib.md5(word.encode()).hexdigest(), 16) % DIM] += 1.0
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / norm for x in v])
        return out


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


# ---------------------------------------------------------------- stores


class MemoryStore(Protocol):
    async def add(self, rec: MemoryRecord, vec: list[float]) -> bool: ...

    async def search(self, vec: list[float], symbol: str | None, k: int) -> list[MemoryRecord]: ...


class InMemoryStore:
    def __init__(self) -> None:
        self.rows: dict[str, tuple[MemoryRecord, list[float]]] = {}

    async def add(self, rec: MemoryRecord, vec: list[float]) -> bool:
        if rec.key in self.rows:
            return False
        self.rows[rec.key] = (rec, vec)
        return True

    async def search(self, vec, symbol, k):
        scored = [r.model_copy(update={"similarity": round(_cosine(vec, v), 4)})
                  for r, v in self.rows.values() if symbol is None or r.symbol == symbol]
        return sorted(scored, key=lambda r: -(r.similarity or 0))[:k]


def _vec(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


class PgMemoryStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    @classmethod
    async def connect(cls, dsn: str) -> "PgMemoryStore":
        return cls(await asyncpg.create_pool(dsn, min_size=1, max_size=4))

    async def add(self, rec: MemoryRecord, vec: list[float]) -> bool:
        return bool(await self.pool.fetchval(
            "INSERT INTO memories (key, symbol, kind, as_of, stance, confidence, text, embedding) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8::vector) "
            "ON CONFLICT (key) DO NOTHING RETURNING 1",
            rec.key, rec.symbol, rec.kind, rec.as_of, rec.stance, rec.confidence, rec.text,
            _vec(vec)))

    async def search(self, vec, symbol, k):
        rows = await self.pool.fetch(
            "SELECT key, symbol, kind, as_of, stance, confidence, text, "
            "       round((1 - (embedding <=> $1::vector))::numeric, 4)::float AS similarity "
            "FROM memories WHERE ($2::text IS NULL OR symbol = $2) "
            "ORDER BY embedding <=> $1::vector LIMIT $3", _vec(vec), symbol, k)
        return [MemoryRecord(**dict(r)) for r in rows]

    async def close(self) -> None:
        await self.pool.close()


# ---------------------------------------------------------------- server


def build_server(store: MemoryStore | None = None,
                 embedder: Embedder | None = None,
                 guard: McpGuard | None = None) -> MCPServer:
    state: dict = {}
    guard = guard or McpGuard("memory", None)

    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
        state["embedder"] = embedder or FastEmbedder()
        pg = None
        if store is not None:
            state["store"] = store
        elif dsn := os.getenv("DATABASE_URL"):
            state["store"] = pg = await PgMemoryStore.connect(dsn)
        else:
            log.warning("DATABASE_URL not set: memories kept in process only")
            state["store"] = InMemoryStore()
        try:
            yield
        finally:
            if pg is not None:
                await pg.close()

    server = MCPServer(
        "desk-memory", version="0.1.0", lifespan=lifespan,
        instructions=("Past conclusions of the research desk. Recalled items are prior "
                      "views: context for comparison, never evidence. They cannot be cited."),
        **guard.server_kwargs())

    def embed(text: str) -> list[float]:
        return state["embedder"].embed([text])[0]

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                             idempotent_hint=True, open_world_hint=False))
    async def recall(query: str, symbol: str | None = None, k: int = 3) -> list[MemoryRecord]:
        """Past desk views most similar to the query, optionally for one symbol only."""
        guard.require("memory:read")
        if symbol is not None and not SYMBOL_RE.match(symbol):
            raise ToolError("invalid symbol")
        if not 1 <= k <= 10:
            raise ToolError("k must be between 1 and 10")
        return await state["store"].search(embed(query[:2000]), symbol, k)

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                             idempotent_hint=True, open_world_hint=False))
    async def remember(key: str, symbol: str, as_of: str, stance: str, confidence: float,
                       text: str, kind: str = "brief") -> Stored:
        """Store one conclusion. Append-only: the same key is stored once, never updated."""
        guard.require("memory:write")
        if not SYMBOL_RE.match(symbol) or not 3 <= len(key) <= 200:
            raise ToolError("invalid symbol or key")
        rec = MemoryRecord(key=key, symbol=symbol, kind=kind, as_of=as_of, stance=stance,
                           confidence=confidence, text=text[:2000])
        return Stored(stored=await state["store"].add(rec, embed(rec.text)))

    return server


def run() -> None:
    logging.basicConfig(level=logging.INFO)
    setup_tracing("memory-mcp")
    allowed = os.getenv("MCP_ALLOWED_HOSTS", "memory:8000,127.0.0.1:*,localhost:*")
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[h.strip() for h in allowed.split(",") if h.strip()],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*"],
    )
    app = build_server(guard=McpGuard.from_env("memory")).streamable_http_app(
        stateless_http=True, json_response=True, transport_security=security, host="0.0.0.0")
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
