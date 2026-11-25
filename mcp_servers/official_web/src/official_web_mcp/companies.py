"""Every PSX company's own website, as PSX itself lists it.

The allow-list in config/official_sources.toml names a handful of companies by hand.
Any other listed company is looked up here: its profile on the exchange's own site
(dps.psx.com.pk/company/<SYMBOL>) has a WEBSITE field, and a domain PSX publishes for
a listed company is official by the same standard as the hand-written ones. So the
advisor can read any PSX company's site, and still only sites an official source
vouches for. The model never adds a domain.

    symbol ─► PSX profile page (itself an allow-listed fetch)
           ─► WEBSITE link ─► sanity checks ─► that domain, plus where its home page
                                               redirects (e.g. a rebrand), cached

Checks on what PSX lists: http(s) only, a real hostname (no IP, port or credentials),
not a shared platform (a Facebook page or a free blog host would let anyone publish
there), and not a bare public suffix. Answers are cached for a week; "no website
listed" for a few hours, so a PSX hiccup is retried soon.
"""

import asyncio
import ipaddress
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

log = logging.getLogger(__name__)

PROFILE = "https://dps.psx.com.pk/company/{symbol}"
WEBSITE = re.compile(r"WEBSITE\s*</div>\s*<p>\s*<a\s+href=\"([^\"]{4,300})\"", re.IGNORECASE)
TITLE = re.compile(r"Stock quote for (.{2,120}?) - Pakistan Stock Exchange", re.IGNORECASE)
FOUND_TTL_S = 7 * 24 * 3600
MISSING_TTL_S = 6 * 3600
MAX_HOPS = 3

# Platforms where anyone can publish: a company page there is not the company's site.
SHARED = {"facebook.com", "linkedin.com", "twitter.com", "x.com", "instagram.com",
          "youtube.com", "google.com", "sites.google.com", "blogspot.com", "wordpress.com",
          "wixsite.com", "weebly.com", "github.io", "medium.com", "tumblr.com"}
SUFFIXES = {"com", "pk", "com.pk", "org.pk", "net.pk", "gov.pk", "edu.pk", "biz.pk",
            "web.pk", "co", "net", "org"}

Get = Callable[[str], Awaitable[tuple[int, dict[str, str], bytes]]]


@dataclass(frozen=True)
class Company:
    symbol: str
    name: str
    domains: tuple[str, ...]  # the listed one first, then where it redirects


def clean_domain(url: str) -> str | None:
    """The registrable-looking host of a company link, or None if it is not usable."""
    if "://" not in url:
        url = "http://" + url
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        return None
    if p.port not in (None, 80, 443):
        return None
    host = p.hostname.lower().rstrip(".")
    try:
        ipaddress.ip_address(host)
        return None  # a bare IP address is nobody's official site
    except ValueError:
        pass
    host = host.removeprefix("www.")
    if "." not in host or host in SUFFIXES:
        return None
    if host in SHARED or any(host.endswith("." + s) for s in SHARED):
        return None
    return host


class CompanySites:
    """symbol -> Company, from PSX's company profiles, cached."""

    def __init__(self, get: Get, is_psx: Callable[[str], bool]) -> None:
        self.get, self.is_psx = get, is_psx
        self.cache: dict[str, tuple[float, Company | None]] = {}
        self.locks: dict[str, asyncio.Lock] = {}

    def known(self) -> dict[str, Company]:
        return {s: c for s, (_, c) in self.cache.items() if c is not None}

    async def lookup(self, symbol: str) -> Company | None:
        hit = self.cache.get(symbol)
        if hit and time.time() < hit[0]:
            return hit[1]
        async with self.locks.setdefault(symbol, asyncio.Lock()):
            hit = self.cache.get(symbol)
            if hit and time.time() < hit[0]:
                return hit[1]
            try:
                company = await self._from_psx(symbol)
            except Exception as e:  # noqa: BLE001  unknown, not an error for the caller
                log.warning("company site lookup failed for %s: %s", symbol, e)
                company = None
            ttl = FOUND_TTL_S if company else MISSING_TTL_S
            self.cache[symbol] = (time.time() + ttl, company)
            return company

    async def _from_psx(self, symbol: str) -> Company | None:
        url = PROFILE.format(symbol=symbol)
        assert self.is_psx(url)  # the profile itself comes only from the exchange
        status, _, body = await self.get(url)
        if status != 200:
            return None
        html = body.decode("utf-8", "replace")
        m = WEBSITE.search(html)
        listed = clean_domain(m.group(1)) if m else None
        if listed is None:
            return None
        t = TITLE.search(html)
        name = t.group(1).strip() if t else symbol
        return Company(symbol, name, tuple(dict.fromkeys(
            [listed, *await self._redirects(m.group(1), listed)])))

    async def _redirects(self, url: str, listed: str) -> list[str]:
        """Where the listed site's home page sends visitors (http->https, a rebrand to a
        new domain). The company's own redirect, so its target is the company's too.
        Best effort: a site that is down still has its listed domain."""
        out, current = [], url if "://" in url else "http://" + url
        for _ in range(MAX_HOPS):
            try:
                status, headers, _ = await self.get(current)
            except Exception:  # noqa: BLE001
                break
            loc = headers.get("location")
            if status not in (301, 302, 303, 307, 308) or not loc:
                break
            current = urljoin(current, loc)
            host = clean_domain(current)
            if host is None:
                break
            if host != listed:
                out.append(host)
        return out
