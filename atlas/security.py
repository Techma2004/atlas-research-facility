"""Security helpers: SSRF-safe URL validation, untrusted-text handling, secrets."""
from __future__ import annotations

import ipaddress
import re
import socket
from pathlib import Path
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

ALLOWED_PORTS = {80, 443, 8080, 8443}
BLOCKED_SUFFIXES = (".local", ".localhost", ".internal", ".localdomain", ".lan", ".home",
                    ".corp", ".intranet", ".home.arpa")
TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
                   "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src", "igshid", "spm"}


class UnsafeURL(ValueError):
    """Raised when a URL fails SSRF / scheme / host validation."""


def ip_is_public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    if isinstance(addr, ipaddress.IPv6Address) and addr.sixtofour:
        addr = addr.sixtofour
    return addr.is_global and not (addr.is_multicast or addr.is_reserved or addr.is_loopback
                                   or addr.is_link_local or addr.is_private or addr.is_unspecified)


def default_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({i[4][0] for i in infos})


def validate_url(url: str, *, resolver=default_resolver, allow_onion: bool = False,
                 allowed_ports=ALLOWED_PORTS) -> str:
    """Return a normalised URL or raise UnsafeURL.

    Checks scheme, credentials, port, hostname patterns, and that EVERY resolved
    address is a public one. Call again for each redirect hop.
    """
    if not isinstance(url, str) or not url or len(url) > 2048:
        raise UnsafeURL("URL is empty or too long.")
    if any(ord(c) < 32 or c in " \\" for c in url):
        raise UnsafeURL("URL contains illegal characters.")
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise UnsafeURL("Only http and https URLs are allowed.")
    if p.username or p.password:
        raise UnsafeURL("URLs with embedded credentials are not allowed.")
    host = (p.hostname or "").lower().rstrip(".")
    if not host:
        raise UnsafeURL("URL has no hostname.")
    try:
        port = p.port or (443 if p.scheme == "https" else 80)
    except ValueError:
        raise UnsafeURL("Invalid port.")
    if port not in allowed_ports:
        raise UnsafeURL(f"Port {port} is not permitted.")
    is_onion = host.endswith(".onion")
    if is_onion and not allow_onion:
        raise UnsafeURL("Onion addresses are only reachable through the isolated onion module.")
    if not is_onion:
        if host == "localhost" or host.endswith(BLOCKED_SUFFIXES) or "." not in host and not _is_ip(host):
            raise UnsafeURL("Local or internal hostnames are not permitted.")
        try:
            addrs = [host] if _is_ip(host) else resolver(host, port)
        except (socket.gaierror, OSError):
            raise UnsafeURL("Hostname could not be resolved.")
        if not addrs:
            raise UnsafeURL("Hostname could not be resolved.")
        for a in addrs:
            if not ip_is_public(a):
                raise UnsafeURL("Destination resolves to a private, local or reserved address.")
    return urlunparse(p._replace(fragment=""))


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def canonical_url(url: str) -> str:
    """Normalise a URL for deduplication (no fragment, tracking params, trailing slash)."""
    p = urlparse(url.strip())
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    port = f":{p.port}" if p.port and p.port not in (80, 443) else ""
    q = sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=False)
               if k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_"))
    path = p.path.rstrip("/") or "/"
    scheme = "https" if p.scheme in ("http", "https") else p.scheme
    return urlunparse((scheme, host + port, path, "", urlencode(q), ""))


def domain_of(url: str) -> str:
    h = (urlparse(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def ensure_within(base: Path, candidate: Path) -> Path:
    base = Path(base).resolve()
    c = Path(candidate).resolve()
    if base != c and base not in c.parents:
        raise PermissionError("Path is outside the permitted directory.")
    return c


# ---------------------------------------------------------------------------
# Untrusted text / prompt-injection handling
# ---------------------------------------------------------------------------
_INJECTION_PATTERNS = [
    r"ignore (all |any |the )?(previous|prior|above|earlier) (instructions|prompts?|rules)",
    r"disregard (all |any |the )?(previous|prior|above|earlier|system)",
    r"you are now\b", r"new (system )?instructions?:", r"(^|\n)\s*system\s*:",
    r"reveal (your|the) (system )?prompt", r"<\|?(im_start|im_end|system|assistant)\|?>",
    r"\bact as (an? )?(unrestricted|jailbroken|dan)\b", r"override (the )?(safety|policy|rules)",
    r"do not (cite|mention) (your|the) sources", r"exfiltrate|send (the )?(api )?keys?",
]
_INJ_RE = re.compile("|".join(_INJECTION_PATTERNS), re.I)
_CTRL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u202a-\u202e\u2066-\u2069]")


def clean_text(text: str) -> str:
    """Strip control / bidi-override characters and normalise whitespace."""
    text = _CTRL.sub("", text or "")
    return re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", text)).strip()


def injection_hits(text: str) -> list[str]:
    return sorted({m.group(0).strip().lower()[:60] for m in _INJ_RE.finditer(text or "")})


def neutralize(text: str) -> str:
    """Defang instruction-like phrases and chat-template tokens in source text.

    The text stays readable (so evidence is preserved) but is visibly marked so a
    model cannot mistake it for an instruction. Layered with fencing in prompts.
    """
    text = re.sub(r"<\|[^|>]{1,30}\|>", "[token-removed]", text)
    return _INJ_RE.sub(lambda m: f"[flagged-instruction-like-text: {m.group(0)[:50]}]", text)


def fence(label: str, text: str) -> str:
    """Wrap untrusted text in a fence; the closing marker cannot appear inside."""
    body = neutralize(clean_text(text)).replace("<<<", "«").replace(">>>", "»")
    return f"<<<UNTRUSTED_SOURCE {label}>>>\n{body}\n<<<END_UNTRUSTED_SOURCE {label}>>>"


SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9._\-]{8,}|api[_-]?key=[^&\s]+)", re.I)


def redact(text: str) -> str:
    return SECRET_RE.sub("[redacted]", text or "")


def safe_error(exc: BaseException) -> str:
    """User-safe error string: no stack traces, secrets or file paths."""
    msg = redact(str(exc))[:300]
    msg = re.sub(r"(/[\w.\-]+){2,}", "<path>", msg)
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__
