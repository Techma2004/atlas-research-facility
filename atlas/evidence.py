"""Evidence management: source typing/quality, de-duplication, passages, claims, citations.

Nothing here invents data: passages are verbatim slices of stored text, citations
must resolve to a stored source label, and independence is computed from content
similarity so syndicated copies never count as separate confirmation.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from . import security

_WORD = re.compile(r"[a-z0-9]{2,}")
STOP = set("the a an and or of to in on for with is are was were be been by as at from that this it its "
           "what which who how why when where do does did can could should would will may might about into "
           "than then them they their there these those not no yes more most some any such also".split())

PRIMARY_SUFFIX = (".gov", ".mil", ".int", ".gov.uk", ".gov.ng", ".europa.eu", ".un.org")
SCHOLAR_DOMAINS = ("doi.org", "arxiv.org", "openalex.org", "nature.com", "science.org", "sciencedirect.com",
                   "springer.com", "ieee.org", "acm.org", "pubmed.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov",
                   "wiley.com", "jstor.org", "plos.org", "cell.com", "biorxiv.org", "medrxiv.org", "ssrn.com")
NEWS_DOMAINS = ("reuters.com", "apnews.com", "bbc.com", "bbc.co.uk", "nytimes.com", "theguardian.com", "wsj.com",
                "bloomberg.com", "ft.com", "washingtonpost.com", "aljazeera.com", "npr.org", "cnn.com",
                "arstechnica.com", "wired.com", "theverge.com", "techcrunch.com", "vice.com")
DOC_HINTS = ("docs.", "developer.", "readthedocs", "man7.org", "developer.mozilla.org", "rfc-editor.org",
             "ietf.org", "w3.org", "kernel.org", "python.org", "debian.org", "github.com", "gitlab.com",
             "learn.microsoft.com", "cloud.google.com", "docs.aws.amazon.com", "owasp.org", "nist.gov")
OPINION_HINTS = ("medium.com", "substack.com", "blogspot.", "wordpress.com", "quora.com", "reddit.com", "tumblr.com",
                 "linkedin.com/pulse", "/opinion/", "/blog/")
REFERENCE_DOMAINS = ("wikipedia.org", "britannica.com")
QUALITY_BY_TYPE = {"primary": 0.9, "scholarly": 0.9, "documentation": 0.8, "preprint": 0.7, "journalism": 0.65,
                   "reference": 0.55, "opinion": 0.35, "unverified": 0.3}


def classify_source(url: str, type_hint: str | None = None, meta: dict | None = None) -> str:
    d = security.domain_of(url)
    low = url.lower()
    if type_hint in ("scholarly", "preprint", "journalism", "reference"):
        base = type_hint
    else:
        base = None
    if d.endswith(PRIMARY_SUFFIX) or ".gov." in d:
        return "primary"
    if base == "scholarly" or any(d == s or d.endswith("." + s) for s in SCHOLAR_DOMAINS):
        return "preprint" if "arxiv" in d or "rxiv" in d or base == "preprint" else "scholarly"
    if base:
        return base
    if any(d.endswith(x) for x in REFERENCE_DOMAINS):
        return "reference"
    if any(d == n or d.endswith("." + n) for n in NEWS_DOMAINS):
        return "journalism"
    if any(h in d or h in low for h in OPINION_HINTS):
        return "opinion"
    if any(h in d or h in low for h in DOC_HINTS):
        return "documentation"
    if d.endswith(".edu") or ".ac." in d:
        return "primary" if "/~" not in low else "unverified"
    return "unverified"


def quality_score(stype: str, *, has_date: bool, text_len: int, https: bool, relevance: float = 0.0) -> float:
    s = QUALITY_BY_TYPE.get(stype, 0.3)
    s += 0.05 if has_date else 0
    s += 0.05 if text_len > 1500 else (-0.1 if text_len < 200 else 0)
    s += 0.0 if https else -0.1
    s += min(relevance, 1.0) * 0.1
    return round(max(0.0, min(1.0, s)), 3)


# ------------------------------------------------------------------ text utils
def tokens(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in STOP]


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])|\n+", text)
    return [p.strip() for p in parts if 40 <= len(p.strip()) <= 600]


def best_passages(text: str, query: str, n: int = 3, max_len: int = 400) -> list[str]:
    q = set(tokens(query))
    if not q:
        return sentences(text)[:n]
    scored = []
    for i, s in enumerate(sentences(text)[:800]):
        t = set(tokens(s))
        if not t:
            continue
        hit = len(q & t)
        if hit:
            scored.append((hit / (len(q) ** 0.5 * len(t) ** 0.25), -i, s))
    scored.sort(reverse=True)
    out, seen = [], set()
    for _, _, s in scored:
        key = s[:60].lower()
        if key not in seen:
            seen.add(key)
            out.append(s[:max_len])
        if len(out) >= n:
            break
    return out


def norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def quote_in_source(quote: str, text: str) -> bool:
    return bool(quote) and norm_ws(quote) in norm_ws(text)


# ------------------------------------------------------------------ dedup
def shingles(text: str, k: int = 5, cap: int = 400) -> set[int]:
    w = tokens(text)[:3000]
    if len(w) < k:
        return {hash(" ".join(w))} if w else set()
    hs = {int(hashlib.md5(" ".join(w[i:i + k]).encode(), usedforsecurity=False).hexdigest()[:8], 16) for i in range(len(w) - k + 1)}
    return set(sorted(hs)[:cap])      # bottom-k sketch: stable and bounded


def jaccard(a: set[int], b: set[int]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def content_hash(text: str) -> str:
    return hashlib.sha256(" ".join(tokens(text)[:2000]).encode()).hexdigest()[:32]


def norm_title(t: str) -> str:
    return " ".join(tokens(re.sub(r"\s[-|–—]\s[^-|–—]{2,40}$", "", t or "")))


@dataclass
class SourceRec:
    id: int
    url: str
    title: str
    domain: str
    stype: str
    text: str
    quality: float = 0.0
    published: str | None = None
    sketch: set = field(default_factory=set)


class IndependenceIndex:
    """Groups sources that are copies/syndications of one another."""

    def __init__(self, threshold: float = 0.6):
        self.threshold = threshold
        self.parent: dict[int, int] = {}
        self.recs: dict[int, SourceRec] = {}

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def add(self, rec: SourceRec) -> int | None:
        """Add a source; returns the id of an earlier duplicate if one is found."""
        rec.sketch = rec.sketch or shingles(rec.text)
        self.recs[rec.id] = rec
        self.parent[rec.id] = rec.id
        dup = None
        for other in list(self.recs.values()):
            if other.id == rec.id:
                continue
            same_title = norm_title(rec.title) and norm_title(rec.title) == norm_title(other.title) and rec.domain != other.domain
            if jaccard(rec.sketch, other.sketch) >= self.threshold or (same_title and len(rec.text) > 300 and len(other.text) > 300
                                                                       and jaccard(rec.sketch, other.sketch) >= 0.3):
                ra, rb = self.find(rec.id), self.find(other.id)
                if ra != rb:
                    self.parent[ra] = rb
                dup = dup or other.id
        return dup

    def group(self, source_id: int) -> int:
        return self.find(source_id) if source_id in self.parent else source_id

    def independent_count(self, ids) -> int:
        return len({self.group(i) for i in ids})


# ------------------------------------------------------------------ citations / claims
CITE_RE = re.compile(r"\[S(\d{1,3})\]")
MULTI_CITE_RE = re.compile(r"\[(S\d{1,3}(?:\s*[,;]\s*S\d{1,3})+)\]")


def expand_multi_cites(text: str) -> str:
    return MULTI_CITE_RE.sub(lambda m: "".join(f"[{s.strip()}]" for s in re.split(r"[,;]", m.group(1))), text)


def validate_citations(report: str, valid_labels: set[str]) -> tuple[str, list[str]]:
    """Strip citations that do not map to a stored source; return (clean, invalid)."""
    report = expand_multi_cites(report)
    invalid = []

    def sub(m):
        lab = f"S{m.group(1)}"
        if lab in valid_labels:
            return m.group(0)
        invalid.append(lab)
        return "[citation removed: no such stored source]"
    return CITE_RE.sub(sub, report), sorted(set(invalid))


NEG = re.compile(r"\b(not|no|never|none|without|cannot|can't|isn't|aren't|doesn't|don't|fails?|lack|lacks|unproven|refut\w+|debunk\w+|disproved|false)\b", re.I)


def _polarity(s: str) -> int:
    return len(NEG.findall(s)) % 2


def extract_claims(report: str, labels: dict[str, SourceRec], idx: IndependenceIndex,
                   max_claims: int = 40) -> list[dict]:
    """Derive claims from the report: every sentence that carries valid [S#] citations."""
    claims = []
    body = expand_multi_cites(report)
    for raw in re.split(r"(?<=[.!?])\s+|\n+", body):
        cites = [f"S{n}" for n in CITE_RE.findall(raw)]
        cites = [c for c in dict.fromkeys(cites) if c in labels]
        txt = re.sub(r"\s+([.,;:!?])", r"\1", CITE_RE.sub("", raw)).strip(" -*#>\t")
        if not cites or len(txt) < 30:
            continue
        srcs = [labels[c] for c in cites]
        claims.append(_assess(txt, srcs, labels, idx))
        if len(claims) >= max_claims:
            break
    return claims


def _assess(text: str, srcs: list[SourceRec], labels: dict[str, SourceRec], idx: IndependenceIndex) -> dict:
    n_ind = idx.independent_count([s.id for s in srcs])
    full = [s for s in srcs if not getattr(s, "meta", {}).get("snippet_only")]
    n_full = idx.independent_count([s.id for s in full])
    best = max((s.quality for s in full), default=0)
    high = any(s.stype in ("primary", "scholarly", "documentation") for s in full)
    if n_full >= 2 and (high or best >= 0.6):
        strength = "strong"
    elif n_full >= 2 or (best >= 0.7 and high):
        strength = "moderate"
    elif srcs:
        strength = "weak"
    else:
        strength = "unverified"
    ev = []
    for s in srcs:
        quote = (best_passages(s.text, text, 1, 300) or [""])[0]
        ev.append({"source_id": s.id, "stance": "support", "quote": quote})
    # heuristic contradiction scan across other stored sources
    pol = _polarity(text)
    cited = {s.id for s in srcs}
    ctoks = set(tokens(text))
    for s in labels.values():
        if s.id in cited or idx.group(s.id) in {idx.group(c) for c in cited}:
            continue
        for p in best_passages(s.text, text, 1, 300):
            pt = set(tokens(p))
            if ctoks and len(ctoks & pt) / len(ctoks) >= 0.6 and _polarity(p) != pol:
                ev.append({"source_id": s.id, "stance": "contradict", "quote": p})
                break
    unc = []
    if n_ind < 2:
        unc.append("Only one independent source supports this claim.")
    if len(full) < len(srcs):
        unc.append("Part of the support is a search snippet only (the page text was not read).")
    if not high:
        unc.append("No full-text primary, scholarly or documentation source among the citations.")
    if any(e["stance"] == "contradict" for e in ev):
        unc.append("A stored source may disagree (lexical heuristic; verify manually).")
        if strength == "strong":
            strength = "moderate"
    return {"text": text[:600], "strength": strength, "independent_count": n_ind,
            "uncertainty": " ".join(unc) or "No specific uncertainty detected by automated checks.",
            "evidence": ev}


def extractive_report(question: str, recs: list[tuple[str, SourceRec, list[str]]], limitations: list[str],
                      style: str = "brief") -> str:
    """Deterministic, citation-safe report used when no AI model processes the sources.

    Every sentence is a verbatim passage from a stored source with its [S#] label.
    """
    lines = [f"# Research brief: {question}", "",
             "## Executive summary",
             "No AI model synthesised these sources, so this brief is **extractive**: each finding below is a "
             "verbatim passage from a stored source, ranked by relevance to the question. Use a local model or the "
             "browser-assisted workflow for a written synthesis.", "", "## Key findings"]
    n = 0
    for label, rec, passages in recs:
        for p in passages[:2 if style in ("detailed", "comprehensive") else 1]:
            lines.append(f"- {p} [{label}]")
            n += 1
    if n == 0:
        lines.append("- No relevant passages could be extracted from the retrieved sources.")
    lines += ["", "## Limitations and open questions"] + [f"- {x}" for x in limitations]
    return "\n".join(lines)
