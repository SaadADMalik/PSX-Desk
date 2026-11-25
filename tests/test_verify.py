"""The claim verifier and the risk gate: deterministic checks, so exact tests."""

import pytest
from contracts import Brief, Claim, Evidence, Stance, Verification
from orchestrator.risk import RiskPolicy, assess
from orchestrator.verify import Unverifiable, check_claim, numbers_in, verify_brief

# Real values from the HBL run of 2026-11-16.
PRICE = Evidence(id="ev_d4b883ec4818", symbol="HBL", kind="price_summary", source="t",
                 as_of="2026-11-14", fetched_at="2026-11-16T00:00:00Z", values={
                     "rsi_14": 59.49, "sma_50": 298.24, "sma_200": 299.77, "last_close": 308.22,
                     "pct_vs_sma_50": 3.35, "return_1m_pct": 6.49, "return_3m_pct": -5.87,
                     "avg_volume_20d": 1907726.0, "max_drawdown_1y_pct": -33.2,
                     "volatility_60d_annualised_pct": 21.08, "above_sma_200": True})
VALUATION = Evidence(id="ev_51a13c739a6d", symbol="HBL", kind="valuation", source="t",
                     as_of="2026-11-16", fetched_at="2026-11-16T00:00:00Z",
                     values={"pe_ratio": 6.77, "dividend_yield_pct": 7.84})
NEWS = Evidence(id="ev_aaaaaaaaaaaa", symbol="HBL", kind="news", source="t",
                as_of="2026-11-16", fetched_at="2026-11-16T00:00:00Z", values={
                    "window_days": 30, "count_in_window": 11, "days_since_last": 0,
                    "latest": [{"date": "2026-11-16", "category": "material_information",
                                "title": "Financial Results for the Quarter Ended 2026-09-30"}]})
EV = {e.id: e for e in (PRICE, VALUATION, NEWS)}
# announcements were read, and nothing new in the last fortnight
QUIET = NEWS.model_copy(update={"id": "ev_bbbbbbbbbbbb", "values": {
    **NEWS.values, "days_since_last": 14, "latest": []}})


def claim(text, *ids):
    return Claim(text=text, evidence_ids=list(ids or [PRICE.id]))


def status(text, *ids):
    return check_claim(claim(text, *ids), "key_points", EV)


def test_the_real_hallucination_is_caught():
    # A citation is valid, the number is not: 308.22 / 298.24 is 3.35% above, not 10.0%.
    c = status("Last close of 308.22 is 10.0% above the 50-day SMA, a bullish trend.")
    assert c.status == "unverified" and c.unmatched == ["10.0%"]
    assert status("Last close of 308.22 is 3.35% above the 50-day SMA.").status == "verified"


@pytest.mark.parametrize("text", [
    "60-day volatility of 21.1% is moderate",          # rounded to the precision written
    "Average volume of 1.9 million shares",            # scale words
    "Average volume of 1,907,726 shares",              # thousands separators
    "3-month return of -5.87%",                        # signs
    "a 3-month decline of 5.87%",                      # sign carried by the words
    "1-year max drawdown of 33.2%",
    "RSI(14) at 59.49 is below the overbought level of 70",  # label + convention
    "Trades above its 50-day and 200-day averages over 52 weeks",  # periods only
])
def test_written_forms_of_true_numbers_verify(text):
    assert status(text).status == "verified", numbers_in(text)


def test_dates_years_and_ids_are_not_treated_as_facts():
    text = ("Results for the quarter ended 2026-09-30 were posted on Nov 16, 2026 "
            "(see ev_aaaaaaaaaaaa) in Q3 FY26")
    assert numbers_in(text) == []


def test_number_must_be_in_the_cited_evidence_not_just_anywhere():
    # 6.77 is HBL's P/E, but this claim cites only the price evidence.
    assert status("P/E of 6.77 looks cheap", PRICE.id).status == "unverified"
    assert status("P/E of 6.77 looks cheap", VALUATION.id).status == "verified"


def test_counts_from_news_are_checked():
    assert status("11 announcements in the last 30 days", NEWS.id).status == "verified"
    assert status("14 announcements in the last 30 days", NEWS.id).unmatched == ["14"]


def test_unknown_evidence_is_uncited():
    c = status("RSI at 59.49", "ev_ffffffffffff")
    assert c.status == "uncited" and c.unknown_ids == ["ev_ffffffffffff"]


def brief(points, risks, confidence=0.7, stance=Stance.bullish):
    return Brief(symbol="HBL", stance=stance, confidence=confidence, thesis="Balanced view.",
                 key_points=points, risks=risks)


def test_brief_keeps_verified_claims_and_reports_the_rest():
    b, v = verify_brief(brief([claim("RSI at 59.49"), claim("RSI at 75")],
                              [claim("Drawdown of 33.2%")]), EV)
    assert [c.text for c in b.key_points] == ["RSI at 59.49"]
    assert v.checked == 3 and v.verified == 2 and v.rejected[0].unmatched == ["75"]


def test_brief_without_a_verified_risk_is_unusable_and_says_why():
    with pytest.raises(Unverifiable) as e:
        verify_brief(brief([claim("RSI at 59.49")], [claim("Drawdown of 50%")]), EV)
    (fb,) = e.value.checkpoint["feedback"]
    assert "Drawdown of 50%" in fb and "50%" in fb


# ---------------------------------------------------------------- risk gate

GOOD = Verification(checked=4, verified=4)


def gate(b=None, evidence=None, **kw):
    kw = {"failures": {}, "cases_missing": [], "verification": GOOD, **kw}
    return assess(b or brief([claim("RSI at 59.49")], [claim("Drawdown of 33.2%")]), evidence or EV, **kw)


def test_clean_brief_passes_and_is_actionable():
    b, risk = gate(evidence={PRICE.id: PRICE, VALUATION.id: VALUATION, QUIET.id: QUIET})
    assert risk.verdict == "pass" and risk.actionable and b.confidence == 0.7


def test_illiquid_stock_is_vetoed():
    thin = PRICE.model_copy(update={"values": {**PRICE.values, "avg_volume_20d": 12_000}})
    _, risk = gate(evidence={thin.id: thin, QUIET.id: QUIET})
    assert risk.verdict == "veto" and not risk.actionable
    assert [f.rule for f in risk.flags if f.severity == "veto"] == ["illiquid"]


def test_cautions_cap_confidence():
    wild = PRICE.model_copy(update={"values": {**PRICE.values,
                                               "volatility_60d_annualised_pct": 72.0}})
    b, risk = gate(evidence={wild.id: wild, QUIET.id: QUIET}, failures={"news_analysis": "down"})
    assert risk.verdict == "caution" and b.confidence == 0.6 and risk.confidence_before == 0.7
    assert {f.rule for f in risk.flags} >= {"high_volatility", "missing_analysis"}


def test_stale_data_and_weak_grounding_cap_harder():
    stale = PRICE.model_copy(update={"stale": True})
    b, risk = gate(evidence={stale.id: stale}, verification=Verification(checked=4, verified=2))
    assert b.confidence == 0.5 and {"stale_data", "weak_grounding"} <= {f.rule for f in risk.flags}


def test_fresh_material_announcement_is_a_caution():
    b, risk = gate()  # NEWS: material information announced today
    assert "fresh_announcement" in {f.rule for f in risk.flags} and b.confidence == 0.6
    _, risk = gate(policy=RiskPolicy(recent_announcement_days=-1))
    assert "fresh_announcement" not in {f.rule for f in risk.flags}


def test_announcements_that_could_not_be_read_veto_the_trade():
    _, risk = gate(evidence={PRICE.id: PRICE, VALUATION.id: VALUATION})
    assert risk.verdict == "veto" and not risk.actionable
    assert [f.rule for f in risk.flags if f.severity == "veto"] == ["no_announcement_data"]


def test_neutral_or_unconfident_briefs_are_never_actionable():
    _, neutral = gate(brief([claim("RSI at 59.49")], [claim("Drawdown of 33.2%")], stance=Stance.neutral))
    _, weak = gate(brief([claim("RSI at 59.49")], [claim("Drawdown of 33.2%")], confidence=0.4))
    assert not neutral.actionable and not weak.actionable
    assert neutral.verdict == weak.verdict  # informational, not a veto


def test_numbers_quoted_from_evidence_text_verify():
    news = NEWS.model_copy(update={"values": {**NEWS.values, "latest": [
        {"date": "2026-11-13", "title": "Credit of Third Interim Cash Dividend (D-95) 2026"}]}})
    c = check_claim(claim("Third interim dividend (D-95) was credited", news.id), "points",
                    {news.id: news})
    assert c.status == "verified"
