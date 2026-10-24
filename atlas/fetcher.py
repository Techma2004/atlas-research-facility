"""Safe page fetching and text extraction.

* every hop (including redirects) is validated against SSRF rules
* size, redirect, timeout and concurrency limits
* robots.txt respected; no CAPTCHA/paywall bypass
* HTML is parsed as data only - scripts are never executed
* results cached in SQLite (page_cache)
"""
from __future__ import annotations

import datetime as dt
import io
import json
import re
import threading
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from . import security
from .db import Database, now

TEXT_TYPES = ("text/html", "application/xhtml+xml", "text/plain", "application/pdf", "text/markdown")
BLOCK_MARKERS = ("captcha", "are you a robot", "access denied", "verify you are human",
                 "enable javascript and cookies", "cf-chl", "attention required")


@dataclass
class Page:
    url: str
    final_url: str = ""
    status: int = 0
    content_type: str = ""
    title: str = ""
    text: str = ""
    published: str | None = None
    meta: dict = field(default_factory=dict)
    error: str = ""
    from_cache: bool = False
    bytes: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.text) and not self.error


def extract_html(html: str, base_url: str = "") -> tuple[str, str, str | None, dict]:
    soup = BeautifulSoup(html, "html.parser")
    meta: dict = {}
    title = (soup.title.get_text(" ", strip=True) if soup.title else "")
    for sel, key in (("meta[property='og:title']", "og_title"), ("meta[name='author']", "author"),
                     ("meta[property='og:site_name']", "site_name"),
                     ("meta[name='description']", "description")):
        el = soup.select_one(sel)
        if el and el.get("content"):
            meta[key] = el["content"].strip()[:300]
    link = soup.select_one("link[rel='canonical']")
    if link and link.get("href"):
        meta["canonical"] = urljoin(base_url, link["href"])
    published = None
    for sel in ("meta[property='article:published_time']", "meta[name='date']", "meta[name='pubdate']",
                "meta[itemprop='datePublished']", "meta[name='citation_publication_date']",
                "meta[name='DC.date']", "meta[property='og:published_time']"):
        el = soup.select_one(sel)
        if el and el.get("content"):
            published = el["content"].strip()[:40]
            break
    if not published:
        for s in soup.find_all("script", type="application/ld+json")[:5]:
            try:
                data = json.loads(s.string or "")
            except ValueError:
                continue
            for d in (data if isinstance(data, list) else [data]):
                if isinstance(d, dict) and d.get("datePublished"):
                    published = str(d["datePublished"])[:40]
                    break
            if published:
                break
    if not published:
        t = soup.find("time", attrs={"datetime": True})
        if t:
            published = t["datetime"][:40]
    # outbound links (for citation following) - data only
    links = []
    for a in soup.select("a[href]")[:400]:
        href = urljoin(base_url, a["href"])
        if href.startswith(("http://", "https://")):
            links.append(href)
    meta["links"] = links[:200]
    for el in soup(["script", "style", "noscript", "iframe", "object", "embed", "svg", "form",
                    "nav", "footer", "header", "aside", "template"]):
        el.decompose()
    main = soup.find("article") or soup.find("main") or soup.body or soup
    paras = [p.get_text(" ", strip=True) for p in main.find_all(["p", "li", "h1", "h2", "h3", "h4", "pre", "td"])]
    paras = [p for p in paras if len(p) > 25]
    text = "\n".join(paras) if paras else main.get_text(" ", strip=True)
    return title[:300], security.clean_text(text), published, meta


def extract_pdf(data: bytes, max_pages: int = 25) -> tuple[str, str]:
    try:
        from pypdf import PdfReader
    except ImportError:
        return "", ""
    try:
        r = PdfReader(io.BytesIO(data))
        title = ""
        try:
            title = (r.metadata.title or "") if r.metadata else ""
        except Exception:
            pass
        parts = []
        for pg in r.pages[:max_pages]:
            parts.append(pg.extract_text() or "")
        return str(title)[:300], security.clean_text("\n".join(parts))
    except Exception:
        return "", ""


class Fetcher:
    def __init__(self, db: Database | None, settings: dict, *, client: httpx.Client | None = None,
                 resolver=security.default_resolver, cancel: threading.Event | None = None):
        self.db = db
        self.cfg = settings["fetch"]
        self.resolver = resolver
        self.cancel = cancel or threading.Event()
        self.client = client or httpx.Client(
            follow_redirects=False, timeout=httpx.Timeout(self.cfg["timeout_s"], connect=6),
            headers={"User-Agent": self.cfg["user_agent"], "Accept": "text/html,application/pdf,text/plain;q=0.9"},
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4))
        self._robots: dict[str, RobotFileParser | None] = {}
        self._lock = threading.Lock()
        self._sem = threading.BoundedSemaphore(4)
        self.stats = {"fetched": 0, "cached": 0, "errors": 0, "blocked": 0}

    def close(self):
        self.client.close()

    # ---- robots ------------------------------------------------------
    def _allowed(self, url: str) -> bool:
        if not self.cfg.get("respect_robots", True):
            return True
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        with self._lock:
            if origin not in self._robots:
                rp = RobotFileParser()
                try:
                    security.validate_url(origin + "/robots.txt", resolver=self.resolver)
                    r = self.client.get(origin + "/robots.txt", timeout=6)
                    if r.status_code == 200 and len(r.content) < 500_000:
                        rp.parse(r.text.splitlines())
                    elif r.status_code in (401, 403):
                        rp.parse(["User-agent: *", "Disallow: /"])
                    else:
                        rp = None
                except Exception:
                    rp = None
                self._robots[origin] = rp
            rp = self._robots[origin]
        return True if rp is None else rp.can_fetch(self.cfg["user_agent"], url)

    # ---- cache -------------------------------------------------------
    def _cache_get(self, url: str) -> Page | None:
        if not self.db:
            return None
        row = self.db.one("SELECT * FROM page_cache WHERE url=?", (url,))
        if not row:
            return None
        try:
            age = dt.datetime.now().astimezone() - dt.datetime.fromisoformat(row["fetched_at"])
        except ValueError:
            return None
        if age > dt.timedelta(hours=self.cfg["cache_ttl_hours"]) or row["status"] != 200:
            return None
        return Page(url=url, final_url=row["final_url"], status=200, content_type=row["content_type"],
                    title=row["title"], text=row["text"], published=row["published"],
                    meta=json.loads(row["meta_json"] or "{}"), from_cache=True, bytes=len(row["text"]))

    def _cache_put(self, pg: Page):
        if not self.db or not pg.ok:
            return
        self.db.ex("INSERT OR REPLACE INTO page_cache(url,fetched_at,status,content_type,title,published,text,final_url,meta_json)"
                   " VALUES(?,?,?,?,?,?,?,?,?)",
                   (pg.url, now(), pg.status, pg.content_type, pg.title, pg.published, pg.text, pg.final_url,
                    json.dumps(pg.meta)))

    # ---- main entry --------------------------------------------------
    def fetch(self, url: str, *, use_cache: bool = True, max_text: int = 60000) -> Page:
        pg = Page(url=url)
        if self.cancel.is_set():
            pg.error = "cancelled"
            return pg
        try:
            url = security.validate_url(url, resolver=self.resolver)
        except security.UnsafeURL as e:
            pg.error = f"blocked: {e}"
            self.stats["blocked"] += 1
            return pg
        pg.url = url
        if use_cache:
            hit = self._cache_get(url)
            if hit:
                self.stats["cached"] += 1
                hit.text = hit.text[:max_text]
                return hit
        if not self._allowed(url):
            pg.error = "blocked: disallowed by robots.txt"
            self.stats["blocked"] += 1
            return pg
        with self._sem:
            try:
                self._download(pg, url, max_text)
            except httpx.TimeoutException:
                pg.error = "timeout"
            except httpx.HTTPError as e:
                pg.error = f"network error: {type(e).__name__}"
            except security.UnsafeURL as e:
                pg.error = f"blocked: {e}"
                self.stats["blocked"] += 1
        if pg.error:
            self.stats["errors"] += 1
        else:
            self.stats["fetched"] += 1
            self._cache_put(pg)
        return pg

    def _download(self, pg: Page, url: str, max_text: int):
        max_bytes = int(self.cfg["max_bytes"])
        hops = 0
        while True:
            security.validate_url(url, resolver=self.resolver)   # re-validate every hop
            with self.client.stream("GET", url) as r:
                if r.status_code in (301, 302, 303, 307, 308):
                    loc = r.headers.get("location")
                    hops += 1
                    if not loc or hops > int(self.cfg["max_redirects"]):
                        pg.error = "too many redirects"
                        return
                    url = urljoin(url, loc)
                    continue
                pg.status = r.status_code
                pg.final_url = url
                if r.status_code in (401, 402, 403, 429, 451):
                    pg.error = f"access restricted (HTTP {r.status_code}); not bypassed"
                    return
                if r.status_code >= 400:
                    pg.error = f"HTTP {r.status_code}"
                    return
                ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                pg.content_type = ctype
                if ctype and not any(ctype.startswith(t) for t in TEXT_TYPES):
                    pg.error = f"unsupported content type: {ctype}"
                    return
                clen = r.headers.get("content-length")
                if clen and clen.isdigit() and int(clen) > max_bytes:
                    pg.error = "response too large"
                    return
                buf = bytearray()
                for chunk in r.iter_bytes(16384):
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        pg.error = "response too large"
                        return
                    if self.cancel.is_set():
                        pg.error = "cancelled"
                        return
                pg.bytes = len(buf)
                if ctype == "application/pdf" or url.lower().endswith(".pdf"):
                    pg.title, pg.text = extract_pdf(bytes(buf))
                    pg.content_type = "application/pdf"
                else:
                    html = bytes(buf).decode(r.encoding or "utf-8", errors="replace")
                    pg.title, pg.text, pg.published, pg.meta = extract_html(html, url)
                low = pg.text[:1500].lower()
                if len(pg.text) < 800 and any(m in low for m in BLOCK_MARKERS):
                    pg.error = "page blocked by anti-bot or access check; not bypassed"
                    pg.text = ""
                    return
                pg.text = pg.text[:max_text]
                if not pg.text:
                    pg.error = "no readable text"
                return
