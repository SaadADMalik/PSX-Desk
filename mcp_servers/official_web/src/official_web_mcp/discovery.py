"""Finding pages on official sites without depending on a search engine.

Free search engines throttle automated clients (429s, empty pages), and `site:` queries
on small sites often return nothing at all. But almost every official site publishes
its own page list, a sitemap (named in robots.txt). Matching a question against that
list is free, needs no third party, and is official by construction: every URL in it
came from the site itself.

The weak spot is wording: people say "ATM fees", the page is called /visa-debit-card/.
A small, explicit synonym table bridges the usual finance vocabulary. It is a table,
not a model, so what it does is easy to read and to test.
"""

import asyncio
import logging
import re
import time
from urllib.parse import urljoin, urlsplit

from mcp.server.mcpserver.exceptions import ToolError

log = logging.getLogger(__name__)

MAX_REDIRECTS = 4
REDIRECTS = (301, 302, 303, 307, 308)

# question word -> words that may appear in a page address instead
SYNONYMS: dict[str, set[str]] = {
    "atm": {"atm", "debit", "card"},
    "withdrawal": {"withdrawal", "atm", "cash"},
    "cash": {"cash", "atm", "withdrawal"},
    "charge": {"charge", "fee", "tariff", "soc", "schedule", "pricing"},
    "fee": {"fee", "charge", "tariff", "soc", "schedule", "pricing"},
    "cost": {"fee", "charge", "tariff", "pricing"},
    "tariff": {"tariff", "fee", "charge", "soc", "schedule"},
    "card": {"card", "debit", "credit"},
    "debit": {"debit", "card"},
    "dividend": {"dividend", "payout", "entitlement", "shareholder"},
    "result": {"result", "financial", "report", "statement", "quarterly", "annual",
               "investor"},
    "earning": {"financial", "result", "report", "statement", "quarterly", "annual"},
    "financial": {"financial", "result", "report", "statement"},
    "report": {"report", "annual", "financial", "statement"},
    "profit": {"profit", "rate", "financial", "result"},
    "rate": {"rate", "profit", "return"},
    "loan": {"financing", "finance", "loan"},
    "financing": {"financing", "finance"},
    "mortgage": {"home", "financing", "housing"},
    "account": {"account", "saving", "current", "deposit"},
    "saving": {"saving", "account", "deposit"},
    "deposit": {"deposit", "saving", "account", "term"},
    "investor": {"investor", "shareholder", "relation"},
    "shareholder": {"shareholder", "investor", "dividend"},
    "board": {"board", "director", "governance"},
    "director": {"director", "board", "governance"},
    "policy": {"policy"},
    "remittance": {"remittance", "home", "transfer"},
    "transfer": {"transfer", "ibft", "remittance", "funds"},
}

STOP = {"the", "and", "for", "what", "does", "how", "much", "with", "from", "are", "its",
        "their", "this", "that", "about", "now", "current", "latest", "new", "other",
        "bank", "banks", "company", "limited", "ltd", "pakistan", "official",
        "use", "using", "when", "which", "who", "why", "per", "any", "all", "has", "have",
        "been", "did", "can", "will", "there"}


def _norm(word: str) -> str:
    """Crude singular: charges -> charge, fees -> fee. Good enough for page slugs."""
    w = word.lower()
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def terms(query: str, drop: set[str] = frozenset()) -> list[set[str]]:
    """One synonym set per meaningful question word."""
    out, seen = [], set()
    for raw in re.findall(r"[a-zA-Z]{3,}", query):
        w = _norm(raw)
        if raw.lower() in STOP or w in STOP or w in drop or w in seen:
            continue
        seen.add(w)
        out.append({_norm(s) for s in SYNONYMS.get(w, {w})} | {w})
    return out


def url_words(url: str) -> set[str]:
    path = urlsplit(url).path.lower()
    return {_norm(w) for w in re.findall(r"[a-z]{2,}", path)}


# Announcements and press pages mention products in passing; product pages describe them.
NEWSY = {"notice", "news", "press", "holiday", "eid", "ramadan", "expand", "establishe",
         "launche", "celebrate", "award", "event", "campaign"}


def score(url: str, wanted: list[set[str]]) -> float:
    words = url_words(url)
    s = float(sum(1 for group in wanted if group & words))
    if s and words & NEWSY:
        s -= 0.75
    if len(re.findall(r"[a-z]+", urlsplit(url).path.lower())) > 7:
        s -= 0.5  # headline-length addresses are news items, not product pages
    path = urlsplit(url).path.lower()
    if re.match(r"^/(ur|urdu)([-/]|$)", path):
        s -= 0.5  # same page in Urdu: prefer the English one
    return s


async def get_official(src, fetcher, url: str) -> tuple[str, str, int, dict, bytes]:
    """GET an allow-listed URL, following redirects only to allow-listed URLs. Every hop
    is checked BEFORE it is fetched. Returns (final url, domain, status, headers, body)."""
    current = url.strip()
    for _ in range(MAX_REDIRECTS + 1):
        domain = src.official(current)
        if domain is None:
            host = urlsplit(current).hostname or current
            raise ToolError(f"not an official source: {host!r}. Only PSX, regulators and "
                            "listed companies' own sites.")
        try:
            status, headers, body = await fetcher.get(current)
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"could not fetch the page: {type(e).__name__}") from e
        if status in REDIRECTS and headers.get("location"):
            current = urljoin(current, headers["location"])
            continue
        return current, domain, status, headers, body
    raise ToolError("too many redirects")


LOC = re.compile(rb"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)


class SiteIndex:
    """Each official site's page list, from its sitemap(s), cached for hours.

    XML is read with a plain <loc> pattern rather than an XML parser: we only need the
    addresses, and a regex cannot be made to expand entities (no XML-bomb risk)."""

    def __init__(self, src, fetcher, ttl_s: float = 12 * 3600, max_urls: int = 10_000,
                 max_maps: int = 25, budget_s: float = 25.0) -> None:
        self.src, self.fetcher = src, fetcher
        self.ttl_s, self.max_urls, self.max_maps, self.budget_s = (ttl_s, max_urls,
                                                                   max_maps, budget_s)
        self.cache: dict[str, tuple[float, list[str]]] = {}
        self.locks: dict[str, asyncio.Lock] = {}

    async def urls(self, domain: str) -> list[str]:
        hit = self.cache.get(domain)
        if hit and time.monotonic() - hit[0] < self.ttl_s:
            return hit[1]
        async with self.locks.setdefault(domain, asyncio.Lock()):
            hit = self.cache.get(domain)
            if hit and time.monotonic() - hit[0] < self.ttl_s:
                return hit[1]
            try:
                pages = await asyncio.wait_for(self._crawl(domain), self.budget_s)
            except Exception as e:  # noqa: BLE001  no sitemap is normal, not an error
                log.info("no sitemap for %s: %s", domain, e)
                pages = []
            self.cache[domain] = (time.monotonic(), pages)
            return pages

    async def _get(self, url: str) -> bytes | None:
        try:
            _, _, status, _, body = await get_official(self.src, self.fetcher, url)
        except ToolError:
            return None
        return body if status == 200 else None

    async def _crawl(self, domain: str) -> list[str]:
        maps: list[str] = []
        robots = await self._get(f"https://{domain}/robots.txt")
        if robots:
            maps = [m.decode().strip() for m in
                    re.findall(rb"(?im)^\s*sitemap:\s*(\S+)", robots)]
        maps = maps or [f"https://{domain}/sitemap.xml", f"https://{domain}/sitemap_index.xml",
                        f"https://{domain}/wp-sitemap.xml"]
        pages: list[str] = []
        seen_maps: set[str] = set()
        while maps and len(seen_maps) < self.max_maps and len(pages) < self.max_urls:
            m = maps.pop(0)
            if m in seen_maps or m.endswith(".gz"):
                continue
            seen_maps.add(m)
            body = await self._get(m)
            if not body:
                continue
            locs = [x.decode("utf-8", "replace") for x in LOC.findall(body)]
            if b"<sitemapindex" in body[:2000].lower():
                maps.extend(locs)
            else:
                pages.extend(u for u in locs if self.src.official(u))
        return list(dict.fromkeys(pages))[: self.max_urls]

    async def find(self, domain: str, query: str, n: int,
                   drop: set[str] = frozenset()) -> list[str]:
        wanted = terms(query, drop)
        if not wanted:
            return []
        ranked = sorted(((score(u, wanted), -len(u), u) for u in await self.urls(domain)),
                        reverse=True)
        # best matches first; anything with at least one meaningful word can follow
        return [u for s, _, u in ranked if s >= 1.0][:n]


def slug_title(url: str) -> str:
    path = urlsplit(url).path.strip("/").split("/")
    last = (path[-1] if path and path[-1] else urlsplit(url).hostname) or url
    last = re.sub(r"\.(pdf|html?|aspx?|php)$", "", last, flags=re.IGNORECASE)
    return re.sub(r"[-_]+", " ", last).strip().title()[:120]
