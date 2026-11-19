"""`uv run baseline-brief LUCK` -> prints a brief and saves the full run to runs/."""

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from agentkit import GatewayClient
from agentkit.tracing import setup_tracing, shutdown_tracing
from contracts import Lane
from dotenv import load_dotenv
from marketdata import MarketData

from baseline_agent.agent import BriefRun, write_brief


def render(run: BriefRun) -> str:
    b = run.brief
    lines = [
        f"\n{b.symbol}: {b.stance.value.upper()} (confidence {b.confidence:.2f})",
        f"\n{b.thesis}\n",
        "Key points:",
        *[f"  - {c.text}  [{', '.join(c.evidence_ids)}]" for c in b.key_points],
        "Risks:",
        *[f"  - {c.text}  [{', '.join(c.evidence_ids)}]" for c in b.risks],
    ]
    if b.data_gaps:
        lines += ["Data gaps:", *[f"  - {g}" for g in b.data_gaps]]
    lines += [
        "",
        (f"steps={run.steps} tool_calls={run.tool_calls} models={sorted(set(run.models))} "
        f"tokens={run.usage.total_tokens} cached_calls={run.cached_calls} "
         f"repaired={run.repaired}"),
    ]
    if run.unknown_citations:
        lines.append(f"WARNING: cited evidence that no tool returned: {run.unknown_citations}")
    for n in run.notes:
        lines.append(f"WARNING: {n}")
    lines.append("Research output, not investment advice.")
    return "\n".join(lines)


async def amain(args: argparse.Namespace) -> int:
    client = GatewayClient(args.gateway, caller="baseline-agent", lane=Lane(args.lane))
    try:
        run = await write_brief(client, MarketData(), args.symbol)
    finally:
        await client.aclose()

    print(render(run))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = out / f"{run.brief.symbol}-{stamp}.json"
    path.write_text(json.dumps({
        "brief": run.brief.model_dump(mode="json"),
        "evidence": {k: v.model_dump(mode="json") for k, v in run.evidence.items()},
        "unknown_citations": run.unknown_citations,
        "usage": run.usage.model_dump(),
        "steps": run.steps, "tool_calls": run.tool_calls,
        "models": run.models, "repaired": run.repaired, "notes": run.notes,
    }, indent=2))
    print(f"saved {path}")
    return 1 if run.unknown_citations else 0


def main() -> None:
    load_dotenv()  # host-side: picks up LLM_GATEWAY_URL and the OTLP endpoint
    # Windows consoles default to cp1252; model text often has Unicode punctuation.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="Single-agent PSX research brief (baseline)")
    p.add_argument("symbol")
    p.add_argument("--gateway", default=os.getenv("LLM_GATEWAY_URL", "http://127.0.0.1:8100"))
    p.add_argument("--lane", default="interactive", choices=[l.value for l in Lane])
    p.add_argument("--out", default="runs")
    args = p.parse_args()
    logging.basicConfig(level=logging.WARNING)
    os.environ.setdefault("OTEL_SERVICE_NAME", "baseline-agent")
    setup_tracing("baseline-agent")
    try:
        sys.exit(asyncio.run(amain(args)))
    finally:
        shutdown_tracing()
