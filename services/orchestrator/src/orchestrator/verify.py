"""The claim verifier: every number a model writes must exist in the evidence it cites.

Slice 1 checked only that cited evidence ids exist. That is not enough. A real
brief cited valid evidence and still said "10.0% above the 50-day SMA" when the
evidence put it 3.3% above: the model did the arithmetic itself, and got it
wrong. So the check is now mechanical, per claim:

  1. every cited id must be known                    else  "uncited"
  2. every number in the text must match a value
     in the cited evidence (to the precision written) else  "unverified"

Rejected claims are dropped and reported. If a brief is left without a key
point or a risk, it is unusable; the engine sends the reasons back to the judge
for one rewrite.

What is *not* a fact to check: periods and labels ("50-day", "RSI(14)", "1y"),
dates, years, quarters, evidence ids, and a few conventional constants (RSI's
30/70 bands). Everything else is checked. A false rejection costs one claim; a
false acceptance puts a wrong number in front of an investor, so the rules lean
strict and the drop rate is measured.
"""

import math
import re
from collections.abc import Iterable
from typing import Any

from contracts import AnalysisReport, Brief, Case, Claim, ClaimCheck, Evidence, Verification

_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
IGNORE = [
    re.compile(r"\bev_[0-9a-f]{12}\b"),
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(rf"\b{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}\b", re.IGNORECASE),
    re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH}\s+\d{{4}}\b", re.IGNORECASE),
    # Periods: 50-day, 52 week, 3-month, 1-year, 20d, 1y, 3m (integer + unit, no space)
    re.compile(r"\b\d+\s*[-‐‑–]\s*(?:day|week|month|year|session)s?\b", re.IGNORECASE),
    re.compile(r"\b\d+\s+(?:day|week|month|year|trading day|session)s?\b", re.IGNORECASE),
    re.compile(r"\b\d+[dwmy]\b", re.IGNORECASE),
    # Indicator names: RSI(14), RSI 14, SMA-200, SMA50, EMA 20
    re.compile(r"\b(?:rsi|sma|ema|ma)\s*[-(]?\s*\d+\s*\)?", re.IGNORECASE),
    re.compile(r"\b(?:q[1-4]|h[12]|fy\s?\d{2,4})\b", re.IGNORECASE),
]
CONVENTIONS: list[tuple[re.Pattern, set[float]]] = [
    (re.compile(r"\b(?:rsi|overbought|oversold)\b", re.IGNORECASE), {30.0, 70.0}),
]
NUMBER = re.compile(
    r"(?<![\w.])([-+−]?\d[\d,]*(?:\.\d+)?)\s*"
    r"(%|percent\b|million\b|mn\b|m\b|billion\b|bn\b|thousand\b|k\b|x\b)?",
    re.IGNORECASE)
SCALE = {"million": 1e6, "mn": 1e6, "m": 1e6, "billion": 1e9, "bn": 1e9,
         "thousand": 1e3, "k": 1e3}


def numbers_in(text: str) -> list[tuple[str, float, float]]:
    """-> [(as written, value, tolerance)]. Tolerance is half a unit in the last
    written digit, so "21.1%" matches 21.08 and "1.9 million" matches 1,907,726."""
    for pattern in IGNORE:
        text = pattern.sub(" ", text)
    out = []
    for m in NUMBER.finditer(text):
        raw, unit = m.group(1), (m.group(2) or "").lower()
        digits = raw.replace(",", "").replace("−", "-")
        try:
            value = float(digits)
        except ValueError:
            continue
        decimals = len(digits.split(".")[1]) if "." in digits else 0
        scale = SCALE.get(unit, 1.0)
        if not unit and decimals == 0 and 1990 <= value <= 2100:
            continue  # a year
        out.append((m.group(0).strip(), value * scale, 0.5 * 10 ** -decimals * scale))
    return out


def _leaves(values: Any) -> Iterable[float]:
    if isinstance(values, bool) or values is None:
        return
    if isinstance(values, int | float):
        if not math.isnan(values):
            yield float(values)
    elif isinstance(values, dict):
        for v in values.values():
            yield from _leaves(v)
    elif isinstance(values, list):
        for v in values:
            yield from _leaves(v)
    elif isinstance(values, str):
        # Numbers quoted from source text (an announcement title's "D-95") are
        # faithful quotes. Found live: the first version rejected them.
        for m in re.finditer(r"\d[\d,]*(?:\.\d+)?", values):
            yield float(m.group(0).replace(",", ""))


def check_claim(claim: Claim, section: str, evidence: dict[str, Evidence]) -> ClaimCheck:
    unknown = sorted(set(claim.evidence_ids) - evidence.keys())
    found = numbers_in(claim.text)
    if unknown:
        return ClaimCheck(section=section, text=claim.text, status="uncited",
                          evidence_ids=claim.evidence_ids, numbers=[f[0] for f in found],
                          unknown_ids=unknown)
    pool = [v for eid in claim.evidence_ids for v in _leaves(evidence[eid].values)]
    conventional = {c for pat, consts in CONVENTIONS if pat.search(claim.text) for c in consts}
    unmatched = [
        written for written, value, tol in found
        if value not in conventional
        and not any(abs(abs(value) - abs(v)) <= tol + 1e-9 for v in pool)
    ]
    return ClaimCheck(section=section, text=claim.text,
                      status="unverified" if unmatched else "verified",
                      evidence_ids=claim.evidence_ids, numbers=[f[0] for f in found],
                      unmatched=unmatched)


class Unverifiable(Exception):
    """Too little survived verification to make a usable brief."""

    def __init__(self, verification: Verification) -> None:
        self.verification = verification
        # Saved with the failed step, so the judge's next attempt (even on another
        # worker, after a crash) can be told exactly what was rejected.
        self.checkpoint = {"feedback": feedback(verification),
                           "verification": verification.model_dump(mode="json")}
        super().__init__("; ".join(feedback(verification)))


def feedback(v: Verification) -> list[str]:
    return [f"REJECTED ({c.section}): \"{c.text}\": {c.reason()}" for c in v.rejected]


def _filter(claims: list[Claim], section: str, evidence: dict[str, Evidence],
            checks: list[ClaimCheck]) -> list[Claim]:
    kept = []
    for c in claims:
        check = check_claim(c, section, evidence)
        checks.append(check)
        if check.status == "verified":
            kept.append(c)
    return kept


def _summary(checks: list[ClaimCheck]) -> Verification:
    return Verification(checked=len(checks),
                        verified=sum(c.status == "verified" for c in checks),
                        rejected=[c for c in checks if c.status != "verified"])


def verify_brief(brief: Brief, evidence: dict[str, Evidence]) -> tuple[Brief, Verification]:
    checks: list[ClaimCheck] = []
    key_points = _filter(brief.key_points, "key_points", evidence, checks)
    risks = _filter(brief.risks, "risks", evidence, checks)
    v = _summary(checks)
    if not key_points or not risks:
        raise Unverifiable(v)
    return brief.model_copy(update={"key_points": key_points, "risks": risks}), v


def verify_report(report: AnalysisReport,
                  evidence: dict[str, Evidence]) -> tuple[AnalysisReport, Verification]:
    """Specialists' unverifiable points are removed before the debaters read them."""
    checks: list[ClaimCheck] = []
    points = _filter(report.points, "points", evidence, checks)
    v = _summary(checks)
    gaps = report.data_gaps + [f"verifier removed: {c.text} ({c.reason()})" for c in v.rejected]
    return report.model_copy(update={"points": points, "data_gaps": gaps}), v


def verify_case(case: Case, evidence: dict[str, Evidence]) -> tuple[Case, Verification]:
    """Debaters' unverifiable arguments are removed before the judge reads them."""
    checks: list[ClaimCheck] = []
    arguments = _filter(case.arguments, "arguments", evidence, checks)
    v = _summary(checks)
    dropped = case.dropped + [f"{c.text} ({c.reason()})" for c in v.rejected]
    return case.model_copy(update={"arguments": arguments, "dropped": dropped}), v
