"""Optional, isolated onion (Tor) research connector.

Disabled by default. Scope is deliberately narrow:
* only hosts on an explicit allowlist of .onion domains are reachable
* no crawling, no search, no link following - the user supplies exact URLs
* traffic goes only through a user-configured *local* Tor SOCKS proxy (socks5h, so
  hostnames are resolved by Tor; nothing is resolved locally)
* strict size/time limits, text-only content, redirects revalidated, never executed
* a kill switch disables the module immediately and aborts in-flight requests
Onion content is stored with origin='onion' and is never mixed into clearnet runs.
"""
from __future__ import annotations

import ipaddress
import socket
import threading
from urllib.parse import urljoin, urlparse

import httpx

from . import evidence as ev
from . import security
from .config import load_settings, save_settings
from .db import Database, j, now
from .fetcher import Page, extract_html

ONION_PORTS = {80, 443, 8080, 8443}
MAX_URLS = 10


class OnionError(Exception):
    pass


def _socks_available() -> bool:
    try:
        import socksio  # noqa: F401
        return True
    except ImportError:
        return False


def _loopback_proxy(socks_url: str) -> tuple[str, int]:
    p = urlparse(socks_url)
    if p.scheme not in ("socks5h", "socks5") or not p.hostname:
        raise OnionError("socks_url must look like socks5h://127.0.0.1:9050")
    host = p.hostname
    try:
        if not ipaddress.ip_address(host).is_loopback:
            raise OnionError("The Tor SOCKS proxy must be on this machine (loopback address).")
    except ValueError:
        if host != "localhost":
            raise OnionError("The Tor SOCKS proxy must be on this machine (localhost).")
    return host, p.port or 9050


class OnionConnector:
    def __init__(self, settings: dict | None = None, client: httpx.Client | None = None):
        self.settings = settings or load_settings()
        self.cfg = self.settings["onion"]
        self._client = client            # injectable for tests
        self.killed = threading.Event()

    # ---- state -----------------------------------------------------------
    def allowlist(self) -> list[str]:
        return [a.strip().lower().rstrip(".") for a in self.cfg.get("allowlist", []) if a.strip()]

    def tor_reachable(self) -> bool:
        try:
            host, port = _loopback_proxy(self.cfg["socks_url"])
            with socket.create_connection((host, port), timeout=2):
                return True
        except (OSError, OnionError):
            return False

    def status(self) -> dict:
        reach = self.tor_reachable() if self.cfg["enabled"] else None
        return {"enabled": bool(self.cfg["enabled"]), "allowlist": self.allowlist(), "socks_url": self.cfg["socks_url"],
                "socks_support_installed": _socks_available(), "tor_reachable": reach,
                "max_bytes": self.cfg["max_bytes"], "timeout_s": self.cfg["timeout_s"],
                "note": ("Disabled (default). Enable it explicitly in Settings after starting Tor locally."
                         if not self.cfg["enabled"] else
                         ("Ready." if reach and _socks_available() else
                          "Enabled but not operational: " + ("install 'httpx[socks]'. " if not _socks_available() else "") +
                          ("Tor is not answering on the configured SOCKS port." if not reach else "")))}

    def kill(self) -> None:
        """Kill switch: disable the module persistently and abort in-flight work."""
        self.killed.set()
        save_settings({"onion": {"enabled": False}})
        self.cfg["enabled"] = False

    # ---- validation ----------------------------------------------------------
    def validate(self, url: str) -> str:
        if not isinstance(url, str) or len(url) > 1024:
            raise OnionError("Invalid URL.")
        p = urlparse(url)
        if p.scheme not in ("http", "https") or p.username or p.password:
            raise OnionError("Only plain http(s) onion URLs without credentials are allowed.")
        host = (p.hostname or "").lower().rstrip(".")
        if not host.endswith(".onion"):
            raise OnionError("Only .onion addresses are handled by this module.")
        label = host.rsplit(".", 1)[0].split(".")[-1]
        if len(label) not in (16, 56) and not self._test_mode():
            raise OnionError("Not a valid v2/v3 onion address length.")
        al = self.allowlist()
        if not any(host == a or host.endswith("." + a) for a in al):
            raise OnionError("This onion domain is not on the allowlist.")
        try:
            port = p.port or (443 if p.scheme == "https" else 80)
        except ValueError:
            raise OnionError("Invalid port.")
        if port not in ONION_PORTS:
            raise OnionError(f"Port {port} is not permitted.")
        return url.split("#")[0]

    def _test_mode(self) -> bool:
        return self._client is not None

    # ---- fetch -----------------------------------------------------------------
    def _make_client(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        if not _socks_available():
            raise OnionError("SOCKS support is not installed. Run: pip install 'httpx[socks]'")
        _loopback_proxy(self.cfg["socks_url"])
        url = self.cfg["socks_url"].replace("socks5://", "socks5h://", 1)     # force remote DNS through Tor
        return httpx.Client(proxy=url, follow_redirects=False, timeout=httpx.Timeout(self.cfg["timeout_s"], connect=20),
                            headers={"User-Agent": "Mozilla/5.0 (ATLAS research)"})

    def fetch(self, url: str) -> Page:
        if not self.cfg["enabled"] or self.killed.is_set():
            raise OnionError("The onion module is disabled.")
        url = self.validate(url)
        pg = Page(url=url)
        client = self._make_client()
        try:
            hops = 0
            while True:
                if self.killed.is_set():
                    raise OnionError("Aborted by kill switch.")
                url = self.validate(url)                       # re-validate every redirect hop
                with client.stream("GET", url) as r:
                    if r.status_code in (301, 302, 303, 307, 308):
                        hops += 1
                        loc = r.headers.get("location")
                        if not loc or hops > 3:
                            raise OnionError("Too many redirects.")
                        url = urljoin(url, loc)
                        continue
                    pg.status, pg.final_url = r.status_code, url
                    if r.status_code >= 400:
                        raise OnionError(f"HTTP {r.status_code}")
                    ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                    if ctype not in ("text/html", "text/plain", "application/xhtml+xml", ""):
                        raise OnionError(f"Unsupported content type: {ctype}")
                    buf = bytearray()
                    for chunk in r.iter_bytes(8192):
                        buf.extend(chunk)
                        if len(buf) > int(self.cfg["max_bytes"]):
                            raise OnionError("Response too large.")
                        if self.killed.is_set():
                            raise OnionError("Aborted by kill switch.")
                    html = bytes(buf).decode(r.encoding or "utf-8", errors="replace")
                    pg.title, pg.text, pg.published, pg.meta = extract_html(html, url)
                    pg.meta.pop("links", None)                  # never follow links
                    pg.bytes = len(buf)
                    pg.content_type = ctype
                    if not pg.text:
                        raise OnionError("No readable text.")
                    return pg
        except httpx.TimeoutException:
            raise OnionError("Timed out.")
        except httpx.HTTPError as e:
            raise OnionError(f"Network error: {type(e).__name__}")
        finally:
            if self._client is None:
                client.close()


def run_onion_research(db: Database, connector: OnionConnector, question: str, urls: list[str]) -> dict:
    """Create a separate 'onion' run from user-supplied allowlisted URLs. No crawling or search."""
    question = security.clean_text(question)
    if len(question) < 5:
        raise OnionError("Enter a question of at least 5 characters.")
    if not urls or len(urls) > MAX_URLS:
        raise OnionError(f"Provide 1-{MAX_URLS} onion URLs.")
    if not connector.cfg["enabled"]:
        raise OnionError("The onion module is disabled. Enable it explicitly first.")
    rid = db.ex("INSERT INTO runs(question,mode,status,provider,created_at,channel,stats_json) VALUES(?,?,?,?,?,?,?)",
                (question, "light", "running", "none", now(), "onion", j({"request": {"urls": urls}})))
    errors, n = [], 0
    for u in urls:
        try:
            pg = connector.fetch(u)
        except OnionError as e:
            errors.append(f"{u[:80]}: {e}")
            continue
        n += 1
        canon = "onion:" + security.canonical_url(pg.final_url or u)
        text = pg.text[:60000]
        meta = j({"origin": "onion", "snippet_only": False})
        existing = db.one("SELECT id FROM sources WHERE canonical_url=?", (canon,))
        if existing:
            sid = existing["id"]
            db.ex("UPDATE sources SET text=?, excerpt=?, retrieved_at=?, title=? WHERE id=?", (text, text[:600], now(), pg.title or u, sid))
        else:
            sid = db.ex("INSERT INTO sources(canonical_url,url,title,domain,source_type,published,retrieved_at,content_hash,text,excerpt,origin,quality,meta_json)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (canon, pg.final_url or u, pg.title or u, security.domain_of(u), "unverified", pg.published, now(),
                         ev.content_hash(text), text, text[:600], "onion", 0.3, meta))
        db.index("source", sid, pg.title or u, text)
        db.ex("INSERT OR REPLACE INTO run_sources(run_id,source_id,label,rank,query,snippet,provider,fetched) VALUES(?,?,?,?,?,?,?,1)",
              (rid, sid, f"S{n}", n, "", text[:300], "onion", ))
        for p in ev.best_passages(text, question, 3, 400):
            db.ex("INSERT INTO passages(run_id,source_id,text) VALUES(?,?,?)", (rid, sid, p))
    lim = ["Onion content is unverified by default and was retrieved only from user-supplied, allowlisted addresses.",
           "No crawling, searching or link-following is performed by the onion module."] + errors
    status = "completed" if n else "failed"
    db.ex("UPDATE runs SET status=?, finished_at=?, limitations_json=? WHERE id=?", (status, now(), j(lim), rid))
    from . import report
    report.rebuild(db, rid)
    return {"run_id": rid, "status": status, "retrieved": n, "errors": errors}
