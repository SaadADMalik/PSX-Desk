"""Turning model text into a validated object, with exactly one repair attempt.

Why one: if a model cannot fix its output when shown the exact validation error,
a second or third try usually burns quota for the same failure. Fail the step
and let the workflow decide.
"""

import json
import logging
import re

from contracts import ChatMessage, Tier
from pydantic import BaseModel, ValidationError

from agentkit.client import GatewayClient

log = logging.getLogger(__name__)

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


class StructuredOutputError(Exception):
    pass


def cap_claims(value: object, limit: int) -> object:
    """Mechanical fixes before validation, so they never cost a repair call.

    Measured live: most repairs were for a 7th point when 6 is the limit, or a risk
    with no evidence id. Both have a fix that needs no model: keep the first
    `limit`, and drop uncited claims (grounding would drop them anyway). Use as a
    `mode="before"` validator on claim lists."""
    if not isinstance(value, list):
        return value
    cited = [c for c in value if not isinstance(c, dict) or c.get("evidence_ids")]
    return cited[:limit]


def extract_json(text: str) -> dict:
    """Accept bare JSON, fenced JSON, or JSON surrounded by prose."""
    text = _THINK.sub("", text or "").strip()
    if m := _FENCE.search(text):
        text = m.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found")
    return json.loads(text[start : end + 1])


def parse_as[T: BaseModel](text: str, model: type[T]) -> T:
    try:
        return model.model_validate(extract_json(text))
    except (ValueError, ValidationError) as e:
        raise StructuredOutputError(str(e)) from e


async def parse_with_repair[T: BaseModel](
    client: GatewayClient,
    text: str,
    model: type[T],
    *,
    tier: Tier = Tier.small,
    max_tokens: int = 1500,
) -> tuple[T, bool]:
    """Returns (object, repaired?)."""
    try:
        return parse_as(text, model), False
    except StructuredOutputError as first:
        # Each repair is a paid model call; log why, so a recurring cause gets fixed.
        log.warning("%s output needs repair (%s): %s", model.__name__,
                    getattr(client, "caller", "?"), str(first)[:400])
        schema = json.dumps(model.model_json_schema(), separators=(",", ":"))
        repair = [
            ChatMessage(role="system", content=(
                "You fix JSON so it validates against a schema. Do not change meaning or "
                "invent facts or numbers. Reply with the JSON object only.")),
            ChatMessage(role="user", content=(
                f"Schema:\n{schema}\n\nValidation error:\n{first}\n\nOriginal output:\n{text}")),
        ]
        resp = await client.chat(repair, tier=tier, max_tokens=max_tokens)
        try:
            return parse_as(resp.message.content or "", model), True
        except StructuredOutputError as second:
            raise StructuredOutputError(f"repair failed: {second}") from second
