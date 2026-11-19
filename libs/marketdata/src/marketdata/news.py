"""Company announcements: parsing, categorising and summarising, all in code.

Announcement titles are written by issuers and scraped from the web, so they are
**untrusted input**. They are cleaned (control and invisible formatting characters
stripped, length capped) and passed on as data. The agent that reads them has no
tool that writes anything, so text that tries to give instructions has nothing to
act with.

Categories and counts are computed here, so a model never has to count, and the
verifier can check any count a model repeats.
"""

import re
import unicodedata
from datetime import date, datetime

from bs4 import BeautifulSoup

WINDOW_DAYS = 30
MAX_TITLE = 160
MAX_LATEST = 8

# First match wins: "Board Meeting to consider financial results" is a board meeting.
CATEGORIES: list[tuple[str, tuple[str, ...]]] = [
    ("board_meeting", ("board meeting", "meeting of the board", "board of directors")),
    ("financial_results", ("financial result", "quarterly report", "annual report",
                           "half yearly", "half-yearly", "accounts")),
    ("dividend_or_payout", ("dividend", "bonus", "right shares", "payout", "book closure",
                            "entitlement")),
    ("material_information", ("material information",)),
    ("corporate_briefing", ("corporate briefing",)),
    ("general_meeting", ("general meeting", "agm", "egm")),
]


def clean_title(raw: str) -> str:
    # Drop control characters (Cc) and invisible formatting ones (Cf: zero-width,
    # bidi overrides), which can hide text from a human reviewer but not from a model.
    text = "".join(" " if unicodedata.category(ch) in {"Cc", "Cf"} else ch for ch in raw)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_TITLE]


def categorise(title: str) -> str:
    t = title.lower()
    for name, words in CATEGORIES:
        if any(w in t for w in words):
            return name
    return "other"


def parse_announcements(html: str) -> list[dict]:
    """PSX's announcements table -> [{date, title}], newest first."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for tr in soup.select("#announcementsTable tbody tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if len(cells) < 5:
            continue
        try:
            day = datetime.strptime(cells[0], "%b %d, %Y").date()
        except ValueError:
            continue
        out.append({"date": day.isoformat(), "title": clean_title(cells[4])})
    return out


def news_summary(items: list[dict], today: date) -> dict:
    """Compact, countable view of recent announcements. Small on purpose: prompt input."""
    dated = sorted(((date.fromisoformat(i["date"]), i["title"]) for i in items), reverse=True)
    recent = [(d, t) for d, t in dated if (today - d).days <= WINDOW_DAYS]
    counts: dict[str, int] = {}
    for _, title in recent:
        c = categorise(title)
        counts[c] = counts.get(c, 0) + 1
    results = [d for d, t in dated if categorise(t) == "financial_results"]
    return {
        "window_days": WINDOW_DAYS,
        "count_in_window": len(recent),
        "by_category_in_window": dict(sorted(counts.items())),
        "days_since_last": (today - dated[0][0]).days if dated else None,
        "days_since_financial_results": (today - results[0]).days if results else None,
        "latest": [{"date": d.isoformat(), "category": categorise(t), "title": t}
                   for d, t in dated[:MAX_LATEST]],
    }
