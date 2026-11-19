"""The risk gate: hard rules with a veto, applied after the judge and the verifier.

Plain code on purpose. "Does this stock trade enough to buy?" has a right answer
in the evidence; a model would sometimes get it wrong and could be talked out
of it by persuasive text. A rule cannot be persuaded.

Each rule reads computed evidence and may:
  * cap the brief's confidence (caution), or
  * veto it, which makes the brief not actionable. Slice 4 only lets actionable
    briefs become paper-trade proposals.

Thresholds are data (RiskPolicy), so they can be reviewed and tuned without
touching the rules.
"""

from dataclasses import dataclass

from contracts import Brief, Evidence, RiskAssessment, RiskFlag, Verification


@dataclass(frozen=True)
class RiskPolicy:
    min_avg_volume_20d: float = 50_000  # shares/day; below this, a position cannot be exited
    max_volatility_pct: float = 50.0  # 60-day annualised
    max_drawdown_pct: float = 40.0  # 1-year, magnitude
    recent_announcement_days: int = 2  # material news this fresh: read it first
    min_verification_rate: float = 0.7
    min_actionable_confidence: float = 0.5
    cap_caution: float = 0.6
    cap_weak_grounding: float = 0.5


def assess(brief: Brief, evidence: dict[str, Evidence], *, failures: dict[str, str],
           cases_missing: list[str], verification: Verification,
           policy: RiskPolicy = RiskPolicy()) -> tuple[Brief, RiskAssessment]:
    flags: list[RiskFlag] = []
    caps: list[float] = []

    def flag(rule: str, severity: str, detail: str, cap: float | None = None) -> None:
        flags.append(RiskFlag(rule=rule, severity=severity, detail=detail))
        if cap is not None:
            caps.append(cap)

    by_kind = {ev.kind: ev for ev in evidence.values()}
    price = by_kind.get("price_summary")
    if price is None:
        flag("no_price_data", "veto", "no price evidence: liquidity and volatility unknown")
    else:
        v = price.values
        volume = v.get("avg_volume_20d")
        if volume is not None and volume < policy.min_avg_volume_20d:
            flag("illiquid", "veto", f"20-day average volume {volume:,.0f} is below "
                 f"{policy.min_avg_volume_20d:,.0f} shares")
        vol = v.get("volatility_60d_annualised_pct")
        if vol is not None and vol > policy.max_volatility_pct:
            flag("high_volatility", "caution", f"60-day volatility {vol}% exceeds "
                 f"{policy.max_volatility_pct}%", policy.cap_caution)
        dd = v.get("max_drawdown_1y_pct")
        if dd is not None and abs(dd) > policy.max_drawdown_pct:
            flag("deep_drawdown", "caution", f"1-year max drawdown {dd}% exceeds "
                 f"{policy.max_drawdown_pct}%", policy.cap_caution)

    news = by_kind.get("news")
    if news is not None:
        fresh = [i for i in news.values.get("latest", [])
                 if i.get("category") in {"material_information", "financial_results"}
                 and news.values.get("days_since_last") is not None
                 and news.values["days_since_last"] <= policy.recent_announcement_days]
        if fresh:
            flag("fresh_announcement", "caution", f"{fresh[0]['category']} announced on "
                 f"{fresh[0]['date']}; its content is not analysed here (titles only)",
                 policy.cap_caution)

    stale = sorted(ev.kind for ev in evidence.values() if ev.stale)
    if stale:
        flag("stale_data", "caution", f"served from last-good snapshot: {stale}",
             policy.cap_weak_grounding)
    if failures:
        flag("missing_analysis", "caution", f"analyses missing: {sorted(failures)}",
             policy.cap_caution)
    if cases_missing:
        flag("one_sided_debate", "caution", f"no case from: {cases_missing}",
             policy.cap_caution)
    if verification.pass_rate < policy.min_verification_rate:
        flag("weak_grounding", "caution", f"only {verification.verified}/"
             f"{verification.checked} claims verified", policy.cap_weak_grounding)

    before = brief.confidence
    after = min([before, *caps])
    if any(f.severity == "veto" for f in flags):
        verdict = "veto"
    elif any(f.severity == "caution" for f in flags):
        verdict = "caution"
    else:
        verdict = "pass"
    if brief.stance == "neutral":
        flag("neutral_stance", "info", "nothing to act on")
    if after < policy.min_actionable_confidence:
        flag("low_confidence", "info", f"confidence {after:.2f} is below "
             f"{policy.min_actionable_confidence}")
    actionable = (verdict != "veto" and brief.stance != "neutral"
                  and after >= policy.min_actionable_confidence)
    return (brief.model_copy(update={"confidence": after}),
            RiskAssessment(verdict=verdict, actionable=actionable, flags=flags,
                           confidence_before=before, confidence_after=after))
