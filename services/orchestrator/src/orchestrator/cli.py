"""`uv run desk-brief LUCK` -> asks the orchestrator for a multi-agent brief."""

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from agentkit.auth import bearer_for
from dotenv import load_dotenv


def _claims(title: str, claims: list[dict]) -> list[str]:
    return [title, *[f"  - {c['text']}  [{', '.join(c['evidence_ids'])}]" for c in claims]]


def render(r: dict) -> str:
    b, risk, v = r["brief"], r["risk"], r["verification"]
    lines = [f"\n{b['symbol']}: {b['stance'].upper()} (confidence {b['confidence']:.2f}"
             + (f", capped from {risk['confidence_before']:.2f}"
                if risk["confidence_after"] < risk["confidence_before"] else "") + ")",
             f"risk gate: {risk['verdict'].upper()}"
             + ("" if risk["actionable"] else " (not actionable)"),
             f"\n{b['thesis']}\n", "Specialists:"]
    for rep in r["reports"]:
        lines.append(f"  - {rep['agent']}: {rep['stance']} ({rep['confidence']:.2f}) "
                     f"{rep['summary']}")
    for skill, why in r["failures"].items():
        lines.append(f"  - {skill}: FAILED ({why})")
    lines.append("Debate:")
    for c in r["cases"]:
        model = r.get("models", {}).get(f"{c['side']}-agent") or "?"
        lines.append(f"  - {c['side']} ({c['confidence']:.2f}, {model}): {c['thesis']}")
        lines.append(f"      concedes: {c['concession']}")
    for side in r["cases_missing"]:
        lines.append(f"  - {side}: no case")
    for p in r.get("prior_views", []):
        lines.append(f"Prior view {p['as_of']}: {p['stance']} ({p['confidence']:.2f})")
    lines += _claims("Key points:", b["key_points"]) + _claims("Risks:", b["risks"])
    if b["data_gaps"]:
        lines += ["Data gaps:", *[f"  - {g}" for g in b["data_gaps"]]]
    if risk["flags"]:
        lines += ["Risk flags:", *[f"  - [{f['severity']}] {f['rule']}: {f['detail']}"
                                   for f in risk["flags"]]]
    upstream = r.get("upstream_checks", {})
    rejected = sum(len(u["rejected"]) for u in upstream.values()) + len(v["rejected"])
    checked = sum(u["checked"] for u in upstream.values()) + v["checked"]
    lines.append(f"Verifier: {checked - rejected}/{checked} claims verified")
    for c in [*v["rejected"], *[c for u in upstream.values() for c in u["rejected"]]]:
        lines.append(f"  - rejected ({c['section']}): {c['text']}  unmatched={c['unmatched']}"
                     + (f" unknown={c['unknown_ids']}" if c["unknown_ids"] else ""))
    lines += ["", f"tokens={r['tokens']}", "Research output, not investment advice."]
    return "\n".join(lines)


def wait_for(base: str, run_id: str, timeout: float) -> dict:
    """Poll the run, printing step changes. Survives orchestrator restarts: the run is
    in Postgres, so we simply keep asking until someone answers."""
    deadline = time.monotonic() + timeout
    last, down_since = None, None
    while time.monotonic() < deadline:
        try:
            run = httpx.get(f"{base}/v1/runs/{run_id}", timeout=10, auth=bearer_for()).json()
        except httpx.HTTPError:
            if down_since is None:
                down_since = time.monotonic()
                print("  orchestrator unreachable; the run is safe in Postgres, waiting...")
            time.sleep(2)
            continue
        if down_since is not None:
            print(f"  orchestrator back after {time.monotonic() - down_since:.0f}s")
            down_since = None
        state = (run["status"], run["attempts"],
                 tuple((k, v["status"], v["attempts"]) for k, v in sorted(run["steps"].items())))
        if state != last:
            steps = ", ".join(f"{k}={v['status']}" + (f"(x{v['attempts']})" if v["attempts"] > 1
                                                       else "")
                              for k, v in sorted(run["steps"].items()))
            print(f"  [{run['status']} attempt {run['attempts']}] {steps}")
            last = state
        if run["terminal"]:
            return run
        time.sleep(1.5)
    sys.exit(f"timed out waiting for run {run_id}")


def main() -> None:
    load_dotenv()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="Multi-agent PSX research brief")
    p.add_argument("symbol")
    p.add_argument("--orchestrator", default=os.getenv("ORCHESTRATOR_URL",
                                                       "http://127.0.0.1:8200"))
    p.add_argument("--lane", default="interactive")
    p.add_argument("--key", help="Idempotency-Key: the same key always returns the same run")
    p.add_argument("--timeout", type=float, default=900)
    p.add_argument("--out", default="runs")
    args = p.parse_args()

    headers = {"Idempotency-Key": args.key} if args.key else {}
    r = httpx.post(f"{args.orchestrator}/v1/briefs", headers=headers,
                   json={"symbol": args.symbol, "lane": args.lane}, timeout=30,
                   auth=bearer_for())
    if r.status_code not in (200, 202):
        print(f"orchestrator HTTP {r.status_code}: {r.text}", file=sys.stderr)
        sys.exit(1)
    created = r.json()
    print(f"run {created['run_id']} "
          f"({'created' if created['created'] else 'existing run, ' + created['duplicate']})")

    run = wait_for(args.orchestrator, created["run_id"], args.timeout)
    if run["status"] != "succeeded":
        print(f"run {run['status']}: {run.get('error')}", file=sys.stderr)
        sys.exit(1)
    result = run["result"]
    print(render(result))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = out / f"desk-{result['brief']['symbol']}-{stamp}.json"
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"saved {path}")
