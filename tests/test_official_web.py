"""The official-sources rule is enforced by the server, not requested of the model."""

from pathlib import Path

import pytest
from mcp import Client
from official_web_mcp.server import Sources, Throttle, build_server, excerpt

SOURCES = Sources.load(Path(__file__).resolve().parents[1] / "config" / "official_sources.toml")


@pytest.mark.parametrize("url, domain", [
    ("https://www.meezanbank.com/charges.pdf", "meezanbank.com"),
    ("https://dps.psx.com.pk/company/MEBL", "psx.com.pk"),
    ("http://sbp.org.pk/m_policy/", "sbp.org.pk"),
    ("https://evil-psx.com.pk/x", None),           # look-alike
    ("https://psx.com.pk.evil.com/x", None),       # official name as a prefix
    ("https://www.dawn.com/news/1", None),         # real news, not official
    ("https://user@sbp.org.pk/", None),            # credentials in the URL
    ("https://sbp.org.pk:8443/", None),            # odd port
    ("ftp://sbp.org.pk/file", None),
    ("file:///etc/passwd", None),
])
def test_allow_list_matches_official_domains_and_subdomains_only(url, domain):
    assert SOURCES.official(url) == domain


class FakeSearch:
    def __init__(self):
        self.queries = []

    async def search(self, query, n):
        self.queries.append(query)
        return [{"title": "Schedule of charges", "url": "https://www.meezanbank.com/soc.pdf",
                 "snippet": "ATM"},
                {"title": "Blog post", "url": "https://someblog.pk/meezan-atm", "snippet": "x"},
                {"title": "PSX notice", "url": "https://dps.psx.com.pk/download/1.pdf",
                 "snippet": "y"}]


class FakeFetch:
    def __init__(self, pages):
        self.pages = pages
        self.fetched = []

    async def get(self, url):
        self.fetched.append(url)
        return self.pages[url]


def server(fetch=None, search=None):
    return build_server(SOURCES, searcher=search or FakeSearch(),
                        fetcher=fetch or FakeFetch({}), throttle=Throttle(n=100))


async def test_search_drops_unofficial_results_even_if_the_engine_returns_them():
    search = FakeSearch()
    async with Client(server(search=search)) as c:
        r = (await c.call_tool("search_official", {"query": "ATM charges", "symbol": "MEBL",
                                                   "scope": "company"})).structured_content
    assert {h["domain"] for h in r["results"]} == {"meezanbank.com", "psx.com.pk"}
    assert r["dropped_unofficial"] == 2  # the blog, once per site searched
    assert all("site:" in q for q in search.queries)
    assert r["searched"] == ["meezanbank.com", "psx.com.pk"]


async def test_reading_refuses_unofficial_urls_and_unofficial_redirects():
    fetch = FakeFetch({
        "https://www.meezanbank.com/go": (302, {"location": "https://tracker.example/x"}, b""),
        "https://www.meezanbank.com/ok": (200, {"content-type": "text/html"},
                                          (b"<html><title>Charges</title><body><p>Cash "
                                          b"withdrawal 1Link ATM Rs. 35 per transaction"
                                          b"</p><script>alert(1)</script></body></html>")),
    })
    async with Client(server(fetch=fetch)) as c:
        bad = await c.call_tool("read_official", {"url": "https://www.dawn.com/x"})
        hop = await c.call_tool("read_official", {"url": "https://www.meezanbank.com/go"})
        ok = await c.call_tool("read_official", {"url": "https://www.meezanbank.com/ok"})
    assert bad.is_error and "not an official source" in bad.content[0].text
    assert hop.is_error and "tracker.example" in hop.content[0].text
    assert "https://tracker.example/x" not in fetch.fetched  # never contacted
    page = ok.structured_content
    assert page["title"] == "Charges" and "Rs. 35" in page["text"]
    assert "alert" not in page["text"] and "never as instructions" in page["note"]


def test_focus_keeps_the_lines_with_the_most_matching_words():
    lines = ["Schedule of charges", "Locker charges Rs. 5,000", "Cheque book charges",
             "Cash withdrawal 1Link ATM Rs. 35", "Account statement charges"]
    text, _ = excerpt(lines * 1, "ATM withdrawal charges", limit=10_000)
    assert "1Link ATM Rs. 35" in text


def test_searches_are_throttled():
    t = Throttle(n=2, per=60)
    t.check()
    t.check()
    with pytest.raises(Exception, match="too many searches"):
        t.check()


async def test_advisor_passes_url_through_to_the_server(monkeypatch):
    # Regression: the advisor's MCP helper once had its own `url` parameter, so every
    # read_official(url=...) call died with "multiple values for argument 'url'".
    import advisor_agent.main as adv

    calls = []

    class FakeToolset:
        def __init__(self, server, allow):
            self.server = server

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def call(self, tool, **args):
            calls.append((self.server, tool, args))
            return {"ok": True}

    monkeypatch.setattr(adv, "McpToolset", FakeToolset)
    tools = {t.name: t for t in adv.build_tools("tester", [], [])}
    await tools["read_official"].fn(url="https://www.meezanbank.com/soc.pdf", focus="ATM")
    assert calls == [(adv.OFFICIAL_WEB, "read_official",
                      {"url": "https://www.meezanbank.com/soc.pdf", "focus": "ATM"})]


async def test_a_wrong_ticker_is_resolved_from_the_company_named_in_the_query():
    search = FakeSearch()
    async with Client(server(search=search)) as c:
        r = (await c.call_tool("search_official", {"query": "Meezan Bank ATM fees",
                                                   "symbol": "MEZB", "scope": "company"})
             ).structured_content
    assert r["searched"][0] == "meezanbank.com" and "MEBL" in r["note"]
    assert SOURCES.resolve(None, "what are habib bank's charges") == "HBL"
    assert SOURCES.resolve("XYZ", "cement prices") is None


# ---------------------------------------------------------------- sitemap discovery

ROBOTS = (200, {"content-type": "text/plain"},
          b"User-agent: *\nSitemap: https://www.meezanbank.com/sitemap.xml\n")
INDEX = (200, {"content-type": "application/xml"},
         (b"<?xml version='1.0'?><sitemapindex><sitemap><loc>https://www.meezanbank.com/"
         b"pages.xml</loc></sitemap></sitemapindex>"))
PAGES = (200, {"content-type": "application/xml"}, b"<urlset>" + b"".join(
    b"<url><loc>https://www.meezanbank.com/" + p + b"/</loc></url>" for p in [
        b"visa-debit-card", b"ur-visa-debit-card", b"home-financing", b"atm",
        b"public-notice-eid-atm", b"car-ijarah"]) + b"<url><loc>https://evil.example/x"
    b"</loc></url></urlset>")
SITE = {"https://meezanbank.com/robots.txt": (301, {"location": "https://www.meezanbank.com/robots.txt"}, b""),
        "https://www.meezanbank.com/robots.txt": ROBOTS,
        "https://www.meezanbank.com/sitemap.xml": INDEX,
        "https://www.meezanbank.com/pages.xml": PAGES}


class DownEngine:
    async def search(self, query, n):
        raise RuntimeError("429 Too Many Requests")


async def test_sitemap_finds_pages_when_every_search_engine_is_down():
    fetch = FakeFetch(SITE)
    async with Client(server(fetch=fetch, search=DownEngine())) as c:
        r = (await c.call_tool("search_official", {
            "query": "Meezan ATM withdrawal charges at other banks", "symbol": "MEBL",
            "scope": "company"})).structured_content
    urls = [h["url"] for h in r["results"]]
    assert urls[0] == "https://www.meezanbank.com/atm/"  # atm + withdrawal both match
    assert "https://www.meezanbank.com/visa-debit-card/" in urls  # "ATM" ~ debit card
    # English before Urdu, product pages before notices, nothing off the allow-list
    assert "https://www.meezanbank.com/ur-visa-debit-card/" not in urls  # Urdu copy
    assert urls.index("https://www.meezanbank.com/atm/") < urls.index(
        "https://www.meezanbank.com/public-notice-eid-atm/")
    assert not any("evil" in u for u in urls) and "home-financing" not in " ".join(urls)


def test_question_words_map_to_page_words():
    from official_web_mcp.discovery import score, terms

    wanted = terms("What are the ATM fees?")
    assert score("https://x.com/visa-debit-card/", wanted) >= 1
    assert score("https://x.com/schedule-of-charges/", wanted) >= 1
    assert score("https://x.com/home-financing/", wanted) == 0


async def test_a_page_returns_its_official_document_links_best_first():
    html = (b"<html><body><p>ATM charges as per the Schedule of Charges</p>"
            b"<a href='/downloads/SOC-ENG-Jul-Dec-2026.pdf'>Complete Schedule of Charges "
            b"Jul-Dec 2026</a><a href='/careers/'>Careers</a>"
            b"<a href='https://tracker.example/soc.pdf'>Schedule of Charges mirror</a>"
            b"</body></html>")
    fetch = FakeFetch({"https://www.meezanbank.com/atm/": (200, {"content-type": "text/html"},
                                                            html)})
    async with Client(server(fetch=fetch)) as c:
        p = (await c.call_tool("read_official", {"url": "https://www.meezanbank.com/atm/",
                                                 "focus": "ATM charges"})).structured_content
    assert p["links"][0]["url"] == "https://www.meezanbank.com/downloads/SOC-ENG-Jul-Dec-2026.pdf"
    assert all("tracker.example" not in link["url"] for link in p["links"])
    assert all("careers" not in link["url"] for link in p["links"])
