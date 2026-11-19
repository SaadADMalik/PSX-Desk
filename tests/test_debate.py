"""The judge's starting point (desk_lean) and the rule that departing from it needs a reason."""

import json

import pytest
from contracts import AnalysisReport, Case, ChatMessage, ChatResponse, Claim, Usage
from debate_agent import main as debate
from debate_agent.main import desk_lean

CLAIM = [Claim(text="price is up", evidence_ids=["e1"])]


def report(agent, stance):
    return AnalysisReport(request_id="r", agent=agent, symbol="HBL", stance=stance,
                          confidence=0.6, summary="a summary here", points=CLAIM,
                          evidence_ids=["e1"])


def case(side, confidence):
    return Case(request_id="r", symbol="HBL", side=side, thesis="a thesis here",
                arguments=CLAIM, concession="a concession", confidence=confidence)


def reps(*stances):
    return [report(a, s) for a, s in zip(("technical", "fundamentals", "news"), stances,
                                          strict=False)]


# Rows are real briefs from Nov 2026 that the judge called neutral (except SYS / FFC).
@pytest.mark.parametrize(("name", "stances", "bull", "bear", "want"), [
    ("MCB: two bullish, bull stronger", ("bullish", "bullish", "neutral"), 0.8, 0.6, "bullish"),
    ("ENGRO: one bullish, bear weak", ("bullish", "neutral", "neutral"), 0.75, 0.3, "bullish"),
    ("HBL: one bullish, gap exactly 0.15", ("neutral", "bullish", "neutral"), 0.75, 0.6,
     "bullish"),
    ("HUBC: one bullish, cases close", ("neutral", "bullish", "neutral"), 0.65, 0.6, "neutral"),
    ("OGDC: nobody has a view", ("neutral", "neutral", "neutral"), 0.7, 0.6, "neutral"),
    ("SYS: one bearish, bear stronger", ("bearish", "neutral", "neutral"), 0.45, 0.7, "bearish"),
    ("split specialists", ("bullish", "bearish", "neutral"), 0.9, 0.2, "neutral"),
    ("two bullish but debate says bear", ("bullish", "bullish", "neutral"), 0.4, 0.8, "neutral"),
    ("two bearish, debate silent", ("bearish", "bearish", "neutral"), 0.6, 0.6, "bearish"),
])
def test_desk_lean(name, stances, bull, bear, want):
    lean, why = desk_lean(reps(*stances), [case("bull", bull), case("bear", bear)])
    assert lean == want, name
    assert "bull case" in why


def test_one_missing_case_gives_no_debate_signal():
    lean, _ = desk_lean(reps("neutral", "bullish"), [case("bull", 0.9)])
    assert lean == "neutral"  # one specialist alone, and no debate to back it


# ---------------------------------------------------------------- the judge


def brief_json(stance, override=""):
    return json.dumps({"symbol": "HBL", "stance": stance, "confidence": 0.6,
                       "thesis": "a thesis long enough", "override_reason": override,
                       "key_points": [{"text": "price is up", "evidence_ids": ["e1"]}],
                       "risks": [{"text": "could fall", "evidence_ids": ["e1"]}]})


def reply(content):
    return ChatResponse(message=ChatMessage(role="assistant", content=content), model="fake",
                        usage=Usage(prompt_tokens=10, completion_tokens=5))


class Client:
    def __init__(self, replies):
        self.replies, self.seen = list(replies), []

    async def chat(self, messages, **kw):
        self.seen.append(messages)
        return self.replies.pop(0)

    async def aclose(self):
        pass


@pytest.fixture
def judge_with(monkeypatch):
    def setup(first, *later):
        client = Client([reply(x) for x in later])
        seen = {}

        async def ask(system, user, **kw):
            seen["user"] = user
            return reply(first), client

        class Ev:
            def prompt_view(self):
                return {"id": "e1"}

        async def no_evidence(ids):
            return {"e1": Ev()}

        async def no_memory(symbol, query):
            return []

        monkeypatch.setattr(debate, "_ask", ask)
        monkeypatch.setattr(debate, "fetch_evidence", no_evidence)
        monkeypatch.setattr(debate, "recall", no_memory)
        payload = {"symbol": "HBL", "request_id": "r",
                   "reports": [r.model_dump(mode="json")
                               for r in reps("bullish", "bullish", "neutral")],
                   "cases": [case("bull", 0.8).model_dump(mode="json"),
                             case("bear", 0.6).model_dump(mode="json")]}
        return payload, client, seen
    return setup


async def test_judge_sees_the_lean_and_agreeing_needs_no_reason(judge_with):
    payload, client, seen = judge_with(brief_json("bullish"))
    out = await debate.judge(payload)
    assert seen["user"]["desk_lean"]["stance"] == "bullish"
    assert out["lean"] == "bullish" and out["override_reason"] == ""
    assert client.seen == []  # no second call


async def test_unexplained_neutral_is_sent_back_once(judge_with):
    payload, client, _ = judge_with(brief_json("neutral"),
                                    brief_json("neutral", "3-month return is -5%, falling"))
    out = await debate.judge(payload)
    assert len(client.seen) == 1
    assert "override_reason is empty" in client.seen[0][-1].content
    assert out["brief"]["stance"] == "neutral"
    assert out["override_reason"].startswith("3-month")
    assert out["tokens_used"] == 30  # both calls are counted


async def test_still_unexplained_after_retry_is_recorded_not_hidden(judge_with):
    payload, _, _ = judge_with(brief_json("neutral"), brief_json("neutral"))
    out = await debate.judge(payload)
    assert out["brief"]["stance"] == "neutral"  # the judge's call stands...
    assert any("no reason" in d for d in out["dropped"])  # ...but the gap is on record


# ---------------------------------------------------------------- case strength


def test_case_strength_is_computed_from_weighted_points():
    from debate_agent.main import _Against, _Weighted, case_strength
    w = lambda n: _Weighted(text="a point here", evidence_ids=["e1"], weight=n)
    a = lambda n: _Against(text="a counter point", weight=n)
    assert case_strength([w(3), w(3), w(2)], [a(1)]) == 0.89
    assert case_strength([w(2), w(2)], [a(2), a(2)]) == 0.5
    assert case_strength([w(1)], [a(3), a(3)]) == 0.14
    assert case_strength([w(3)], []) == 0.75  # nothing named against counts as one weak point
    assert case_strength([w(3)] * 5, []) == 0.94
    # different material, different numbers: the point of the change
    assert len({case_strength([w(x), w(y)], [a(z)])
                for x in (1, 2, 3) for y in (1, 2, 3) for z in (1, 2, 3)}) >= 8


async def test_argue_ignores_dropped_points_when_scoring(monkeypatch):
    draft = json.dumps({"thesis": "a thesis long enough", "concession": "it is pricey",
                        "arguments": [{"text": "cheap on P/E", "evidence_ids": ["e1"], "weight": 3},
                                      {"text": "invented fact", "evidence_ids": ["nope"],
                                       "weight": 3}],
                        "against": [{"text": "falling price", "weight": 3}]})

    class Ev:
        def prompt_view(self):
            return {"id": "e1"}

    async def ask(system, user, **kw):
        assert '"confidence"' not in system  # the model is no longer asked for a number
        return reply(draft), Client([])

    async def evidence(ids):
        return {"e1": Ev()}

    monkeypatch.setattr(debate, "_ask", ask)
    monkeypatch.setattr(debate, "fetch_evidence", evidence)
    out = await debate.bull({"symbol": "HBL", "request_id": "r",
                             "reports": [report("technical", "bullish").model_dump(mode="json")]})
    assert out["confidence"] == 0.5  # 3 for vs 3 against; the uncited point is not counted
    assert len(out["arguments"]) == 1 and "weight" not in out["arguments"][0]
    assert out["dropped"]
