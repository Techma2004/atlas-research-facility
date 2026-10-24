"""Interchangeable search-provider adapters + a manager with retries and dedup.

Adapters only talk to fixed, documented public endpoints (or a user-configured
SearXNG / Brave API). Result URLs are untrusted and are validated again by the
fetcher before any download. No CAPTCHA or access-control bypass is attempted.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, quote, urlparse

import httpx
from bs4 import BeautifulSoup

from . import security


class SearchError(Exception):
    pass


@dataclass
class Hit:
    url: str
    title: str = ""
    snippet: str = ""
    published: str | None = None
    provider: str = ""
    category: str = "web"
    doi: str | None = None
    references: list[str] = field(default_factory=list)   # URLs/DOIs this work cites (for citation following)
    type_hint: str | None = None


@dataclass
class Filters:
    include_domains: list[str] = field(default_factory=list)
    exclude_domains: list[str] = field(default_factory=list)
    date_from: str | None = None   # YYYY-MM-DD
    date_to: str | None = None

    def accepts(self, h: Hit) -> bool:
        d = security.domain_of(h.url)
        if self.include_domains and not any(d == x or d.endswith("." + x) for x in self.include_domains):
            return False
        if any(d == x or d.endswith("." + x) for x in self.exclude_domains):
            return False
        if h.published:
            pub = h.published[:10]
            if self.date_from and re.match(r"\d{4}-\d{2}-\d{2}", pub) and pub < self.date_from:
                return False
            if self.date_to and re.match(r"\d{4}-\d{2}-\d{2}", pub) and pub > self.date_to:
                return False
        return True


class Adapter:
    name = "base"
    category = "web"
    needs_key = False

    def __init__(self, client: httpx.Client, settings: dict):
        self.client = client
        self.settings = settings

    def available(self) -> tuple[bool, str]:
        return True, ""

    def search(self, query: str, limit: int, flt: Filters) -> list[Hit]:
        raise NotImplementedError

    def _get(self, url: str, **kw) -> httpx.Response:
        r = self.client.get(url, **kw)
        if r.status_code == 429:
            raise SearchError("rate limited (HTTP 429)")
        if r.status_code >= 400:
            raise SearchError(f"HTTP {r.status_code}")
        return r


# ---------------------------------------------------------------- web
class DuckDuckGo(Adapter):
    name = "duckduckgo"
    category = "web"
    suffix = ""

    def search(self, query, limit, flt):
        q = query + self.suffix
        if flt.include_domains:
            q += " (" + " OR ".join(f"site:{d}" for d in flt.include_domains[:4]) + ")"
        for d in flt.exclude_domains[:4]:
            q += f" -site:{d}"
        data = {"q": q}
        if flt.date_from:
            data["df"] = f"{flt.date_from}..{flt.date_to or time.strftime('%Y-%m-%d')}"
        r = self.client.post("https://html.duckduckgo.com/html/", data=data)
        if r.status_code in (202, 403, 429):
            raise SearchError(f"search engine refused the request (HTTP {r.status_code}); not bypassed")
        if r.status_code >= 400:
            raise SearchError(f"HTTP {r.status_code}")
        soup = BeautifulSoup(r.text, "html.parser")
        if soup.select_one("form[action*='anomaly']") or "anomaly" in r.text[:3000].lower() and not soup.select(".result"):
            raise SearchError("search engine presented a bot check; not bypassed")
        out = []
        for item in soup.select(".result"):
            a = item.select_one(".result__a")
            if not a:
                continue
            href = a.get("href", "")
            if "uddg=" in href:
                href = parse_qs(urlparse(href).query).get("uddg", [""])[0]
            elif href.startswith("//"):
                href = "https:" + href
            if not href.startswith(("http://", "https://")) or "duckduckgo.com" in href:
                continue
            sn = item.select_one(".result__snippet")
            out.append(Hit(url=href, title=a.get_text(" ", strip=True),
                           snippet=sn.get_text(" ", strip=True) if sn else "", provider=self.name,
                           category=self.category))
            if len(out) >= limit:
                break
        return out


class DocsSearch(DuckDuckGo):
    """Technical-documentation discovery: DuckDuckGo biased towards docs/specs."""
    name = "duckduckgo-docs"
    category = "docs"
    suffix = " documentation OR specification OR reference OR RFC"


class Wikipedia(Adapter):
    name = "wikipedia"
    category = "web"

    def search(self, query, limit, flt):
        r = self._get("https://en.wikipedia.org/w/api.php", params={
            "action": "query", "list": "search", "srsearch": query, "format": "json", "srlimit": limit,
            "srprop": "snippet|timestamp"})
        out = []
        for it in r.json().get("query", {}).get("search", []):
            title = it.get("title", "")
            snip = BeautifulSoup(it.get("snippet", ""), "html.parser").get_text(" ")
            out.append(Hit(url="https://en.wikipedia.org/wiki/" + quote(title.replace(" ", "_")),
                           title=title + " - Wikipedia", snippet=snip, published=(it.get("timestamp") or "")[:10] or None,
                           provider=self.name, category="web", type_hint="reference"))
        return out


class SearXNG(Adapter):
    name = "searxng"
    category = "web"

    def available(self):
        return (bool(self.settings.get("searxng_url")), "Set a SearXNG instance URL in Settings.")

    def search(self, query, limit, flt):
        base = self.settings["searxng_url"].rstrip("/")
        r = self._get(base + "/search", params={"q": query, "format": "json"})
        out = []
        for it in r.json().get("results", [])[:limit]:
            if it.get("url"):
                out.append(Hit(url=it["url"], title=it.get("title", ""), snippet=it.get("content", ""),
                               published=(it.get("publishedDate") or "")[:10] or None,
                               provider=self.name, category="web"))
        return out


class Brave(Adapter):
    name = "brave"
    category = "web"
    needs_key = True

    def available(self):
        from .config import secret
        return (bool(secret("brave")), "Set BRAVE_API_KEY in the environment.")

    def search(self, query, limit, flt):
        from .config import secret
        r = self._get("https://api.search.brave.com/res/v1/web/search", params={"q": query, "count": min(limit, 20)},
                      headers={"X-Subscription-Token": secret("brave"), "Accept": "application/json"})
        return [Hit(url=i["url"], title=i.get("title", ""), snippet=BeautifulSoup(i.get("description", ""), "html.parser").get_text(" "),
                    published=(i.get("page_age") or "")[:10] or None, provider=self.name)
                for i in r.json().get("web", {}).get("results", [])[:limit] if i.get("url")]


# ---------------------------------------------------------------- scholarly
def _inverted_abstract(inv: dict | None) -> str:
    if not inv:
        return ""
    pos = {}
    for w, idxs in inv.items():
        for i in idxs:
            pos[i] = w
    return " ".join(pos[i] for i in sorted(pos))[:1200]


class OpenAlex(Adapter):
    name = "openalex"
    category = "scholar"

    def search(self, query, limit, flt):
        params = {"search": query, "per-page": min(limit, 25),
                  "select": "id,doi,title,publication_date,abstract_inverted_index,primary_location,referenced_works,type"}
        filt = []
        if flt.date_from:
            filt.append(f"from_publication_date:{flt.date_from}")
        if flt.date_to:
            filt.append(f"to_publication_date:{flt.date_to}")
        if filt:
            params["filter"] = ",".join(filt)
        r = self._get("https://api.openalex.org/works", params=params)
        out = []
        for w in r.json().get("results", []):
            loc = w.get("primary_location") or {}
            url = w.get("doi") or loc.get("landing_page_url") or w.get("id")
            if not url:
                continue
            out.append(Hit(url=url, title=w.get("title") or "", snippet=_inverted_abstract(w.get("abstract_inverted_index")),
                           published=w.get("publication_date"), provider=self.name, category="scholar",
                           doi=(w.get("doi") or "").replace("https://doi.org/", "") or None,
                           references=(w.get("referenced_works") or [])[:25], type_hint="scholarly"))
        return out


class Crossref(Adapter):
    name = "crossref"
    category = "scholar"

    def search(self, query, limit, flt):
        params = {"query": query, "rows": min(limit, 25),
                  "select": "DOI,title,URL,issued,abstract,container-title,type"}
        filt = []
        if flt.date_from:
            filt.append(f"from-pub-date:{flt.date_from}")
        if flt.date_to:
            filt.append(f"until-pub-date:{flt.date_to}")
        if filt:
            params["filter"] = ",".join(filt)
        r = self._get("https://api.crossref.org/works", params=params)
        out = []
        for it in r.json().get("message", {}).get("items", []):
            doi = it.get("DOI")
            if not doi:
                continue
            parts = ((it.get("issued") or {}).get("date-parts") or [[None]])[0]
            pub = "-".join(f"{int(p):02d}" if i else str(p) for i, p in enumerate(parts) if p) or None
            title = (it.get("title") or [""])[0]
            abstract = BeautifulSoup(it.get("abstract", ""), "html.parser").get_text(" ")[:1200]
            venue = (it.get("container-title") or [""])[0]
            out.append(Hit(url=it.get("URL") or f"https://doi.org/{doi}", title=title,
                           snippet=(abstract or venue), published=pub, provider=self.name,
                           category="scholar", doi=doi, type_hint="scholarly"))
        return out


class Arxiv(Adapter):
    name = "arxiv"
    category = "scholar"

    def search(self, query, limit, flt):
        r = self._get("https://export.arxiv.org/api/query", params={
            "search_query": "all:" + query, "max_results": min(limit, 20), "sortBy": "relevance"})
        txt = r.text
        if re.search(r"<!DOCTYPE|<!ENTITY", txt, re.I):
            raise SearchError("refusing XML with DTD/entities")
        # html.parser never expands entities or fetches resources, so it is safe for untrusted feeds
        soup = BeautifulSoup(txt, "html.parser")
        out = []
        for e in soup.find_all("entry"):
            link = (e.find("id").get_text(strip=True) if e.find("id") else "")
            if not link.startswith(("http://", "https://")):
                continue
            g = lambda t: " ".join((e.find(t).get_text(" ") if e.find(t) else "").split())   # noqa: E731
            out.append(Hit(url=link, title=g("title"), snippet=g("summary")[:1200],
                           published=g("published")[:10] or None, provider=self.name, category="scholar", type_hint="preprint"))
        if not out and "<entry" in txt:
            raise SearchError("unparseable arXiv response")
        return out


class Gdelt(Adapter):
    name = "gdelt"
    category = "news"

    def search(self, query, limit, flt):
        r = self._get("https://api.gdeltproject.org/api/v2/doc/doc", params={
            "query": query, "mode": "artlist", "format": "json", "maxrecords": min(limit, 50), "sort": "hybridrel"})
        try:
            arts = r.json().get("articles", [])
        except ValueError:
            raise SearchError("GDELT returned a non-JSON response")
        out = []
        for a in arts:
            sd = a.get("seendate", "")
            pub = f"{sd[:4]}-{sd[4:6]}-{sd[6:8]}" if len(sd) >= 8 else None
            if a.get("url"):
                out.append(Hit(url=a["url"], title=a.get("title", ""), snippet=a.get("domain", ""),
                               published=pub, provider=self.name, category="news", type_hint="journalism"))
        return out


ADAPTERS: dict[str, type[Adapter]] = {
    "duckduckgo": DuckDuckGo, "duckduckgo-docs": DocsSearch, "wikipedia": Wikipedia, "searxng": SearXNG,
    "brave": Brave, "openalex": OpenAlex, "crossref": Crossref, "arxiv": Arxiv, "gdelt": Gdelt,
}
CATEGORY_ADAPTERS = {
    "web": ["searxng", "brave", "duckduckgo", "wikipedia"],
    "docs": ["duckduckgo-docs"],
    "scholar": ["openalex", "crossref", "arxiv"],
    "news": ["gdelt"],
}


class SearchManager:
    def __init__(self, settings: dict, client: httpx.Client | None = None, cancel: threading.Event | None = None):
        self.settings = settings
        self.cancel = cancel or threading.Event()
        self.client = client or httpx.Client(timeout=httpx.Timeout(15, connect=6), follow_redirects=True,
                                             headers={"User-Agent": settings["fetch"]["user_agent"]},
                                             max_redirects=3)
        self.errors: dict[str, str] = {}
        self.counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def enabled_for(self, categories: list[str]) -> list[Adapter]:
        on = self.settings["search_providers"]
        out = []
        for cat in categories:
            for name in CATEGORY_ADAPTERS.get(cat, []):
                key = "duckduckgo" if name == "duckduckgo-docs" else name
                if not on.get(key, False):
                    continue
                a = ADAPTERS[name](self.client, self.settings)
                ok, why = a.available()
                if not ok:
                    continue
                out.append(a)
        return out

    def status(self) -> list[dict]:
        rows = []
        on = self.settings["search_providers"]
        for name, cls in ADAPTERS.items():
            if name == "duckduckgo-docs":
                continue
            a = cls(self.client, self.settings)
            ok, why = a.available()
            rows.append({"name": name, "category": a.category, "enabled": bool(on.get(name)),
                         "configured": ok, "note": "" if ok else why, "needs_key": a.needs_key})
        return rows

    def run(self, adapter: Adapter, query: str, limit: int, flt: Filters, retries: int,
            deadline: float) -> list[Hit]:
        last = ""
        for attempt in range(retries + 1):
            if self.cancel.is_set() or time.monotonic() > deadline:
                return []
            try:
                hits = adapter.search(query, limit, flt)
                hits = [h for h in hits if h.url and flt.accepts(h)]
                with self._lock:
                    self.counts[adapter.name] = self.counts.get(adapter.name, 0) + len(hits)
                return hits
            except SearchError as e:
                last = str(e)
                if "refused" in last or "bot check" in last:
                    break
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
                last = f"{type(e).__name__}"
            time.sleep(min(0.4 * (2 ** attempt), 3) if not self.cancel.is_set() else 0)
        with self._lock:
            self.errors[adapter.name] = f"{adapter.name}: {last or 'failed'}"
        return []
