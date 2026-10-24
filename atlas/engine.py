"""The research pipeline: plan -> search -> fetch -> evaluate -> (iterate) -> synthesise -> verify -> save.

All loops are bounded by the mode policy (rounds, sources, pages, time budget) and can be
cancelled at any point. Retrieved text is untrusted data and is only ever fenced and analysed.
"""
from __future__ import annotations

import datetime as dt
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import httpx

from . import evidence as ev
from . import security
from .config import MODES, get_mode_policy, load_settings
from .db import Database, j, now
from .fetcher import Fetcher
from .providers import (LLM, ProviderError, Router, build_analysis_prompt, parse_json_list)
from .search import Filters, Hit, SearchManager
import json

PROVIDER_CHOICES = ("local", "hybrid", "openai", "browser")


class ValidationError(ValueError):
    pass


def validate_request(body: dict, settings: dict | None = None) -> dict:
    settings = settings or load_settings()
    if not isinstance(body, dict):
        raise ValidationError("Request must be a JSON object.")
    q = body.get("question")
    if not isinstance(q, str):
        raise ValidationError("A research question is required.")
    q = security.clean_text(q)
    if len(q) < 5:
        raise ValidationError("Enter a research question of at least 5 characters.")
    if len(q) > 2000:
        raise ValidationError("The research question is too long (max 2000 characters).")
    mode = body.get("mode", "medium")
    if mode not in MODES:
        raise ValidationError(f"Unknown mode '{mode}'. Choose light, medium, high or max.")
    provider = body.get("provider", settings.get("default_provider", "hybrid"))
    if provider not in PROVIDER_CHOICES:
        raise ValidationError("Unknown AI provider choice.")
    sensitive = body.get("sensitive", False)
    if not isinstance(sensitive, bool):
        raise ValidationError("'sensitive' must be true or false.")
    flt = body.get("filters") or {}
    if not isinstance(flt, dict):
        raise ValidationError("filters must be an object.")
    dom_re = re.compile(r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]*[a-z0-9])?)+$")

    def domains(key):
        vals = flt.get(key) or []
        if isinstance(vals, str):
            vals = [v for v in re.split(r"[,\s]+", vals) if v]
        if not isinstance(vals, list) or len(vals) > 20:
            raise ValidationError(f"{key} must be a short list of domains.")
        out = []
        for v in vals:
            v = str(v).strip().lower().removeprefix("www.")
            if not dom_re.match(v):
                raise ValidationError(f"Invalid domain: {v[:60]}")
            out.append(v)
        return out

    def date(key):
        v = flt.get(key)
        if not v:
            return None
        if not isinstance(v, str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", v):
            raise ValidationError(f"{key} must be YYYY-MM-DD.")
        try:
            dt.date.fromisoformat(v)
        except ValueError:
            raise ValidationError(f"{key} is not a valid date.")
        return v

    filters = {"include_domains": domains("include_domains"), "exclude_domains": domains("exclude_domains"),
               "date_from": date("date_from"), "date_to": date("date_to")}
    if filters["date_from"] and filters["date_to"] and filters["date_from"] > filters["date_to"]:
        raise ValidationError("date_from must not be after date_to.")
    project = body.get("project")
    if project is not None:
        if not isinstance(project, str) or len(project.strip()) > 80:
            raise ValidationError("Project name must be at most 80 characters.")
        project = security.clean_text(project) or None
    parent = body.get("parent_run_id")
    if parent is not None and (not isinstance(parent, int) or isinstance(parent, bool)):
        raise ValidationError("parent_run_id must be an integer.")
    overrides = body.get("budget") or {}
    if not isinstance(overrides, dict):
        raise ValidationError("budget must be an object.")
    budget = {}
    for k in ("max_sources", "time_budget_s"):
        if k in overrides:
            v = overrides[k]
            if not isinstance(v, int) or isinstance(v, bool) or v < 1:
                raise ValidationError(f"budget.{k} must be a positive integer.")
            budget[k] = v
    return {"question": q, "mode": mode, "provider": provider, "sensitive": sensitive, "filters": filters,
            "project": project, "parent_run_id": parent, "budget": budget}


# --------------------------------------------------------------- progress / control
class Progress:
    def __init__(self):
        self._lock = threading.Lock()
        self.stage, self.message, self.percent = "queued", "Waiting to start", 0
        self.counts = {"queries": 0, "found": 0, "fetched": 0, "failed": 0, "duplicates": 0, "sources": 0}
        self.events: list[dict] = []
        self.started = time.time()
        self.finished: float | None = None
        self.provider = ""
        self.status = "queued"

    def set(self, stage: str, message: str, percent: int | None = None):
        with self._lock:
            self.stage, self.message = stage, message
            if percent is not None:
                self.percent = max(self.percent, min(100, percent))
            self.events.append({"t": round(time.time() - self.started, 1), "stage": stage, "message": message})
            self.events = self.events[-150:]

    def bump(self, **kw):
        with self._lock:
            for k, v in kw.items():
                self.counts[k] = self.counts.get(k, 0) + v

    def snapshot(self) -> dict:
        with self._lock:
            end = self.finished or time.time()
            return {"stage": self.stage, "message": self.message, "percent": self.percent,
                    "counts": dict(self.counts), "events": list(self.events[-40:]),
                    "elapsed_s": round(end - self.started, 1), "provider": self.provider, "status": self.status}


def mem_available_mb() -> int | None:
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except OSError:
        pass
    return None


@dataclass
class Cand:
    url: str
    canonical: str
    title: str = ""
    snippet: str = ""
    published: str | None = None
    providers: set = field(default_factory=set)
    categories: set = field(default_factory=set)
    score: float = 0.0
    type_hint: str | None = None
    doi: str | None = None
    references: list = field(default_factory=list)
    queries: list = field(default_factory=list)
    text: str = ""
    fetched: bool = False
    error: str = ""
    meta: dict = field(default_factory=dict)
    stype: str = "unverified"
    quality: float = 0.0
    duplicate_of: int | None = None     # index into candidate list
    inj: list = field(default_factory=list)
    carried_id: int | None = None       # DB source id when carried over from a parent run


# --------------------------------------------------------------- query planning (heuristic fallbacks)
def keywords(question: str, n: int = 7) -> list[str]:
    seen, out = set(), []
    for t in ev.tokens(question):
        if t not in seen and len(t) > 2:
            seen.add(t)
            out.append(t)
    return out[:n]


FACETS = ["overview", "evidence study research", "limitations criticism problems", "official documentation",
          "comparison alternatives", "latest developments", "case study", "best practices", "benchmark results",
          "survey review", "history background", "risks security"]


def heuristic_queries(question: str, n: int, used: set[str] | None = None, round_no: int = 0) -> list[str]:
    used = used or set()
    kw = " ".join(keywords(question))
    cands = [question.strip()[:200]] if round_no == 0 else []
    cands += [f"{kw} {f}".strip() for f in (FACETS[round_no * 2:] + FACETS[:round_no * 2])]
    out = []
    for c in cands:
        k = c.lower()
        if k and k not in used and k not in {o.lower() for o in out}:
            out.append(c)
        if len(out) >= n:
            break
    return out


def heuristic_subquestions(question: str) -> list[str]:
    kw = " ".join(keywords(question, 6)) or question[:80]
    return [f"What is {kw} and how does it work?", f"What does the evidence say about {kw}?",
            f"What are the criticisms, risks or limitations of {kw}?",
            f"What alternatives or competing approaches exist for {kw}?",
            f"What are the most recent developments in {kw}?"]


# --------------------------------------------------------------- engine
class ResearchEngine:
    def __init__(self, db: Database, settings: dict | None = None, *, search_client: httpx.Client | None = None,
                 fetch_client: httpx.Client | None = None, llm_client: httpx.Client | None = None,
                 resolver=security.default_resolver):
        self.db = db
        self.settings = settings or load_settings()
        self.search_client, self.fetch_client, self.llm_client = search_client, fetch_client, llm_client
        self.resolver = resolver

    # ---- run creation -------------------------------------------------
    def create_run(self, req: dict) -> int:
        project_id = None
        if req.get("project"):
            row = self.db.one("SELECT id FROM projects WHERE name=?", (req["project"],))
            project_id = row["id"] if row else self.db.ex("INSERT INTO projects(name,created_at) VALUES(?,?)",
                                                          (req["project"], now()))
        if req.get("parent_run_id") and not self.db.one("SELECT id FROM runs WHERE id=?", (req["parent_run_id"],)):
            raise ValidationError("The run to continue does not exist.")
        rid = self.db.ex(
            "INSERT INTO runs(project_id,parent_run_id,question,mode,status,provider,sensitive,created_at,stats_json)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (project_id, req.get("parent_run_id"), req["question"], req["mode"], "queued", req["provider"],
             int(req["sensitive"]), now(), j({"request": req})))
        return rid

    # ---- main pipeline --------------------------------------------------
    def execute(self, run_id: int, progress: Progress | None = None, cancel: threading.Event | None = None) -> dict:
        progress = progress or Progress()
        cancel = cancel or threading.Event()
        run = self.db.one("SELECT * FROM runs WHERE id=?", (run_id,))
        req = json.loads(run["stats_json"]).get("request", {})
        mode, question = run["mode"], run["question"]
        settings = self.settings
        pol = get_mode_policy(mode, settings)
        for k, v in (req.get("budget") or {}).items():           # per-run budgets can only tighten
            if k in pol:
                pol[k] = min(pol[k], v)
        started = time.monotonic()
        gather_deadline = started + pol["time_budget_s"] * 0.8
        filters = Filters(**req.get("filters", {}))
        stats: dict = {"request": req, "policy": {k: pol[k] for k in (
            "target_sources", "max_sources", "max_rounds", "fetch_pages", "time_budget_s", "queries_per_round",
            "providers", "follow_citations", "cross_check", "subquestions")},
            "queries": [], "provider_errors": {}, "stage_providers": [], "stop_reason": "", "rounds": 0}
        limitations: list[str] = []
        stages: list[dict] = []
        self.db.ex("UPDATE runs SET status='running' WHERE id=?", (run_id,))
        progress.status = "running"

        def stage(name, msg, pct, provider="-"):
            progress.set(name, msg, pct)
            stages.append({"stage": name, "t": round(time.monotonic() - started, 1), "provider": provider, "note": msg})

        search = SearchManager(settings, self.search_client, cancel)
        fetcher = Fetcher(self.db, settings, client=self.fetch_client, resolver=self.resolver, cancel=cancel)
        router = Router(settings, self.llm_client)
        plan = router.plan(run["provider"], bool(run["sensitive"]))
        if run["sensitive"] and run["provider"] in ("openai", "hybrid"):
            limitations.append("Run marked sensitive: no external AI provider was used. Search queries are still sent to "
                               "the enabled search services.")
        active: list[LLM] = []
        status, err = "completed", ""
        try:
            # --- local model availability --------------------------------
            stage("check", "Checking AI providers", 2)
            for llm in plan:
                try:
                    if llm.id == "local":
                        llm.resolve_model()
                    active.append(llm)
                except ProviderError as e:
                    stats["provider_errors"][llm.id] = str(e)
            if plan and not active:
                limitations.append("No AI provider was available (" + "; ".join(stats["provider_errors"].values()) +
                                   "). Falling back to browser-assisted analysis.")
            planner = active[0] if active and mode != "light" else None
            progress.provider = planner.tag if planner else ("browser-assisted" if not active else active[0].tag)

            pool: dict[str, Cand] = {}
            order: list[Cand] = []
            # --- carry over from a parent run ----------------------------
            if run["parent_run_id"]:
                self._seed_from_parent(run["parent_run_id"], pool, order)
                if order:
                    progress.set("seed", f"Continuing investigation: {len(order)} stored sources carried over", 4)
                    progress.bump(found=len(order))

            used_q: set[str] = set()
            subqs: list[str] = []
            n_rounds = 0
            for rnd in range(pol["max_rounds"]):
                if cancel.is_set() or time.monotonic() > gather_deadline:
                    stats["stop_reason"] = stats["stop_reason"] or ("cancelled" if cancel.is_set() else "time budget")
                    break
                if self._low_memory():
                    stats["stop_reason"] = "low memory"
                    limitations.append("Stopped gathering early because available memory was low.")
                    break
                n_rounds += 1
                # ---- plan ----
                stage("plan", f"Round {rnd + 1}/{pol['max_rounds']}: planning queries", 6 + rnd * 4,
                      planner.tag if planner else "heuristic")
                if rnd == 0 and pol["subquestions"]:
                    subqs = self._subquestions(question, planner, stats) or heuristic_subquestions(question)
                    stats["subquestions"] = subqs
                queries = self._queries(question, subqs, pol["queries_per_round"], used_q, rnd, planner, order)
                if not queries:
                    stats["stop_reason"] = "no further queries"
                    break
                used_q.update(q.lower() for q in queries)
                stats["queries"] += [{"round": rnd + 1, "q": q} for q in queries]
                progress.bump(queries=len(queries))
                # ---- search ----
                stage("search", f"Searching {len(queries)} queries across providers", 10 + rnd * 8)
                before = len(order)
                self._search(search, pol, queries, filters, pool, order, gather_deadline, progress, cancel)
                new_found = len(order) - before
                if rnd == 0 and not order:
                    limitations.append("No search results were returned. " + "; ".join(search.errors.values()))
                # ---- follow citations ----
                if pol["follow_citations"] and rnd >= 0 and not cancel.is_set() and time.monotonic() < gather_deadline:
                    stage("citations", "Following references from top sources", 30 + rnd * 8)
                    added = self._follow_references(search, fetcher, order, pool, pol, filters, rnd, progress, cancel)
                    new_found += added
                # ---- fetch ----
                stage("fetch", "Reading pages", 35 + rnd * 10)
                self._fetch_all(fetcher, order, pol, gather_deadline, progress, cancel)
                usable = sum(1 for c in order if c.text)
                if cancel.is_set():
                    stats["stop_reason"] = "cancelled"
                    break
                if usable >= pol["target_sources"]:
                    stats["stop_reason"] = "target source count reached"
                    break
                if new_found == 0 and rnd > 0:
                    stats["stop_reason"] = "diminishing returns (no new sources)"
                    break
            stats["rounds"] = n_rounds
            stats["stop_reason"] = stats["stop_reason"] or "round limit reached"
            stats["provider_errors"].update(search.errors)
            stats["search_counts"] = dict(search.counts)
            stats["fetch"] = dict(fetcher.stats)

            # --- evaluate ------------------------------------------------
            stage("evaluate", "Scoring, classifying and de-duplicating sources", 58)
            analysis = self._evaluate(order, pol, question, progress, limitations)
            progress.bump(sources=len(analysis))
            if len(analysis) < pol["min_sources"]:
                limitations.append(f"Only {len(analysis)} usable source(s) were found; the {mode} target is "
                                   f"{pol['min_sources']}-{pol['max_sources']}. Sources were not padded.")
            # --- persist sources -------------------------------------------
            stage("save", "Saving sources and evidence", 64)
            self._persist(run_id, order, analysis, question, pol)
            register = self._register(run_id)

            # --- synthesise ------------------------------------------------
            body, used_tag, browser_prompt = "", "", ""
            if cancel.is_set():
                status = "cancelled"
            if register and status != "cancelled":
                prompt_srcs = self._prompt_sources(register, pol)
                notes = [r["body"] for r in self.db.q("SELECT body FROM notes WHERE run_id=? AND kind='note'", (run_id,))]
                body, used_tag = self._synthesise(run, pol, question, prompt_srcs, active, stats, stage, notes, limitations, cancel)
                browser_prompt = self._browser_prompt(run, pol, question, register, notes)
                if used_tag.startswith("browser"):
                    pass
            elif not register:
                limitations.append("No sources could be retrieved, so no synthesis was produced.")
            # --- cross-check (max) -------------------------------------------
            if pol["cross_check"] and body and not cancel.is_set() and time.monotonic() < started + pol["time_budget_s"]:
                stage("cross-check", "Cross-checking weakly supported claims", 88)
                self._cross_check(run_id, body, search, fetcher, pol, filters, question, stats, progress, cancel)

            stage("finalize", "Verifying citations and assembling the report", 95)
            self.db.ex("UPDATE runs SET provider=?, browser_prompt=? WHERE id=?",
                       (used_tag or "none", browser_prompt, run_id))
            if body:
                self.db.ex("INSERT INTO notes(run_id,kind,provider,body,created_at) VALUES(?,?,?,?,?)",
                           (run_id, "synthesis", used_tag, body, now()))
        except Exception as e:                                           # noqa: BLE001 - surfaced safely
            status, err = "failed", security.safe_error(e)
            limitations.append(f"The run stopped on an error: {err}")
        finally:
            fetcher.close()
        if cancel.is_set():
            status = "cancelled" if status != "failed" else status
        stats["duration_s"] = round(time.monotonic() - started, 1)
        stats["status_note"] = status
        stats["counts"] = progress.snapshot()["counts"]
        self.db.ex("UPDATE runs SET status=?, finished_at=?, stats_json=?, stages_json=?, limitations_json=?, error=? WHERE id=?",
                   (status, now(), j(stats), j(stages), j(limitations), err, run_id))
        from . import report
        report.rebuild(self.db, run_id)
        progress.status = status
        progress.finished = time.time()
        progress.set("done", {"completed": "Research complete", "cancelled": "Cancelled - partial results saved",
                              "failed": "Run failed - see limitations"}.get(status, status), 100)
        return {"run_id": run_id, "status": status}

    # ---- helpers ----------------------------------------------------------
    def _low_memory(self) -> bool:
        m = mem_available_mb()
        return m is not None and m < 200

    def _subquestions(self, question, planner, stats) -> list[str]:
        if not planner:
            return []
        try:
            txt = planner.complete(
                f"Break this research question into 4-6 specific, non-overlapping sub-questions. Return ONLY a JSON "
                f"array of strings.\nQuestion: {question}", max_tokens=400, timeout=60)
            out = parse_json_list(txt)[:6]
            stats["stage_providers"].append({"stage": "subquestions", "provider": planner.tag})
            return out
        except ProviderError as e:
            stats["provider_errors"]["planner"] = str(e)
            return []

    def _queries(self, question, subqs, n, used, rnd, planner, order) -> list[str]:
        out: list[str] = []
        if planner and rnd >= 0:
            have = "; ".join(c.title for c in order[:8] if c.title)[:600]
            try:
                txt = planner.complete(
                    f"Generate {n} distinct web search queries (3-10 words each) to research the question below. "
                    f"{'Cover these sub-questions: ' + ' | '.join(subqs) if subqs else ''} "
                    f"{'Already found sources titled: ' + have + '. Target gaps and different angles.' if have else ''} "
                    f"Return ONLY a JSON array of strings.\nQuestion: {question}", max_tokens=400, timeout=60)
                out = [q for q in parse_json_list(txt) if q.lower() not in used][:n]
            except ProviderError:
                out = []
        if len(out) < n:
            sq = [re.sub(r"[?]", "", s) for s in subqs if s.lower() not in used]
            for q in (sq if rnd == 0 else []) + heuristic_queries(question, n, used | {o.lower() for o in out}, rnd):
                if q.lower() not in used and q.lower() not in {o.lower() for o in out}:
                    out.append(q)
                if len(out) >= n:
                    break
        return out[:n]

    def _search(self, search, pol, queries, flt, pool, order, deadline, progress, cancel):
        adapters = search.enabled_for(pol["providers"])
        if not adapters:
            search.errors["config"] = "no search providers are enabled/configured"
            return
        jobs = [(a, q) for q in queries for a in adapters]
        scores: dict[str, float] = {}
        with ThreadPoolExecutor(max_workers=min(pol["concurrency"], 1 if self._low_memory() else 8)) as ex:
            futs = {ex.submit(search.run, a, q, pol["results_per_query"], flt, pol["max_retries"], deadline): (a, q)
                    for a, q in jobs}
            for f in as_completed(futs):
                a, q = futs[f]
                try:
                    hits = f.result()
                except Exception:                          # noqa: BLE001
                    continue
                for rank, h in enumerate(hits):
                    try:
                        canon = security.canonical_url(h.url)
                    except Exception:                      # noqa: BLE001
                        continue
                    c = pool.get(canon)
                    if not c:
                        c = pool[canon] = Cand(url=h.url, canonical=canon, title=h.title, snippet=h.snippet,
                                               published=h.published, type_hint=h.type_hint, doi=h.doi,
                                               references=h.references)
                        order.append(c)
                        progress.bump(found=1)
                    c.providers.add(h.provider)
                    c.categories.add(h.category)
                    c.queries.append(q)
                    c.score += 1.0 / (30 + rank)           # reciprocal rank fusion
                    if len(h.snippet) > len(c.snippet):
                        c.snippet = h.snippet
                    c.published = c.published or h.published
                    c.references = c.references or h.references
        order.sort(key=lambda c: -c.score)

    def _follow_references(self, search, fetcher, order, pool, pol, flt, rnd, progress, cancel) -> int:
        """Follow scholarly references (OpenAlex ids) and high-value outbound links, bounded."""
        limit = 8 if pol["max_sources"] <= 50 else 20
        added = 0
        ids: list[str] = []
        for c in order[:15]:
            for r in c.references[:5]:
                if r.startswith("https://openalex.org/W"):
                    ids.append(r.rsplit("/", 1)[1])
        ids = list(dict.fromkeys(ids))[:limit]
        if ids and search.settings["search_providers"].get("openalex"):
            try:
                r = search.client.get("https://api.openalex.org/works", params={
                    "filter": "openalex:" + "|".join(ids), "per-page": len(ids),
                    "select": "id,doi,title,publication_date,abstract_inverted_index,primary_location"})
                if r.status_code == 200:
                    from .search import _inverted_abstract
                    for w in r.json().get("results", []):
                        url = w.get("doi") or (w.get("primary_location") or {}).get("landing_page_url") or w.get("id")
                        h = Hit(url=url, title=w.get("title") or "", snippet=_inverted_abstract(w.get("abstract_inverted_index")),
                                published=w.get("publication_date"), provider="openalex-refs", category="scholar",
                                type_hint="scholarly")
                        added += self._add_hit(h, pool, order, flt, 0.01, progress)
            except (httpx.HTTPError, ValueError):
                search.errors["openalex-refs"] = "openalex-refs: failed"
        # outbound links from already-fetched pages to primary / scholarly / documentation domains
        for c in order[:25]:
            if added >= limit * 2 or cancel.is_set():
                break
            for link in (c.meta.get("links") or [])[:200]:
                if ev.classify_source(link) in ("primary", "scholarly", "documentation"):
                    if self._add_hit(Hit(url=link, provider="citation-follow", category="web"), pool, order, flt, 0.005, progress):
                        added += 1
                        if added >= limit * 2:
                            break
        return added

    def _add_hit(self, h, pool, order, flt, score, progress) -> int:
        if not h.url or not flt.accepts(h):
            return 0
        canon = security.canonical_url(h.url)
        if canon in pool:
            return 0
        c = pool[canon] = Cand(url=h.url, canonical=canon, title=h.title, snippet=h.snippet, published=h.published,
                               type_hint=h.type_hint, providers={h.provider}, categories={h.category}, score=score)
        order.append(c)
        progress.bump(found=1)
        return 1

    def _fetch_all(self, fetcher, order, pol, deadline, progress, cancel):
        todo = []
        per_domain: dict[str, int] = {}
        for c in order:
            if c.fetched or c.text or c.error:
                continue
            d = security.domain_of(c.url)
            if per_domain.get(d, 0) >= 4:
                continue
            per_domain[d] = per_domain.get(d, 0) + 1
            if "scholar" in c.categories and c.snippet and len(c.snippet) > 150:
                c.text, c.fetched = c.snippet, True          # abstract from the metadata API
                c.meta["abstract_only"] = True
                continue
            todo.append(c)
        # read the most promising pages first: fused rank plus a small prior for source type
        todo.sort(key=lambda c: -(c.score + 0.02 * ev.QUALITY_BY_TYPE.get(ev.classify_source(c.url, c.type_hint), 0.3)))
        budget = max(0, pol["fetch_pages"] - sum(1 for c in order if c.fetched and not c.meta.get("abstract_only")))
        todo = todo[:budget]
        workers = 1 if self._low_memory() else pol["concurrency"]
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {}
            for c in todo:
                if cancel.is_set() or time.monotonic() > deadline:
                    break
                futs[ex.submit(fetcher.fetch, c.url, max_text=self.settings["storage"]["max_text_per_source"])] = c
            for f in as_completed(futs):
                c = futs[f]
                try:
                    pg = f.result()
                except Exception as e:                       # noqa: BLE001
                    c.error = security.safe_error(e)
                    progress.bump(failed=1)
                    continue
                if pg.ok:
                    c.text, c.fetched = pg.text, True
                    c.title = c.title or pg.title
                    c.published = c.published or pg.published
                    c.meta.update({k: v for k, v in pg.meta.items() if k != "description"})
                    c.url = pg.final_url or c.url
                    progress.bump(fetched=1)
                else:
                    c.error = pg.error
                    progress.bump(failed=1)

    def _evaluate(self, order, pol, question, progress, limitations) -> list[Cand]:
        idx = ev.IndependenceIndex()
        qtok = set(ev.tokens(question))
        for i, c in enumerate(order):
            text = c.text or c.snippet
            c.stype = ev.classify_source(c.url, c.type_hint, c.meta)
            rel = len(qtok & set(ev.tokens(c.title + " " + text[:3000]))) / max(1, len(qtok))
            c.quality = ev.quality_score(c.stype, has_date=bool(c.published), text_len=len(c.text),
                                         https=c.url.startswith("https"), relevance=rel)
            if not c.text:
                c.quality = round(c.quality * 0.6, 3)        # snippet-only sources are weaker evidence
            c.inj = security.injection_hits(text)
            if c.inj:
                c.quality = round(c.quality * 0.7, 3)
            if c.text and len(c.text) >= 300 and rel > 0:     # only full text is compared; snippets are too short to judge
                dup = idx.add(ev.SourceRec(id=i, url=c.url, title=c.title, domain=security.domain_of(c.url),
                                           stype=c.stype, text=text, quality=c.quality))
                if dup is not None:
                    c.duplicate_of = dup
        keep = [c for c in order if c.duplicate_of is None and (c.text or c.snippet)]
        dups = sum(1 for c in order if c.duplicate_of is not None)
        progress.bump(duplicates=dups)
        flagged = sum(1 for c in keep if c.inj)
        if flagged:
            limitations.append(f"{flagged} source(s) contained instruction-like text (possible prompt injection); it was "
                               "neutralised and the sources down-weighted.")
        if dups:
            limitations.append(f"{dups} duplicate/syndicated copy(ies) were detected and are not counted as independent confirmation.")
        # prefer relevance*quality with per-domain diversity
        keep.sort(key=lambda c: (-(c.quality + min(c.score * 8, 0.3))))
        out, per_dom = [], {}
        for c in keep:
            d = security.domain_of(c.url)
            if per_dom.get(d, 0) >= 3:
                continue
            per_dom[d] = per_dom.get(d, 0) + 1
            out.append(c)
            if len(out) >= pol["max_sources"]:
                break
        snippet_only = sum(1 for c in out if not c.text)
        if snippet_only:
            limitations.append(f"{snippet_only} source(s) are search-snippet only (page could not be read); treat as weak evidence.")
        failed = [c for c in order if c.error and c.duplicate_of is None]
        if failed:
            limitations.append(f"{len(failed)} page(s) could not be retrieved (e.g. {failed[0].error}).")
        return out

    def _persist(self, run_id, order, analysis, question, pol):
        chosen = {id(c) for c in analysis}
        idmap: dict[int, int] = {}
        for i, c in enumerate(order):
            if id(c) not in chosen and c.duplicate_of is None:
                continue
            text = (c.text or "")[: self.settings["storage"]["max_text_per_source"]]
            row = self.db.one("SELECT id FROM sources WHERE canonical_url=?", (c.canonical,))
            vals = (c.url, c.title or c.url, security.domain_of(c.url), c.stype, c.published, now(),
                    ev.content_hash(text or c.snippet), text or "", (text or c.snippet)[:600],
                    c.quality, j({"doi": c.doi, "providers": sorted(c.providers), "abstract_only": c.meta.get("abstract_only", False),
                                  "injection_flags": c.inj, "snippet_only": not bool(c.text)}))
            if row:
                sid = row["id"]
                self.db.ex("UPDATE sources SET url=?,title=?,domain=?,source_type=?,published=COALESCE(?,published),retrieved_at=?,"
                           "content_hash=?,text=CASE WHEN length(?)>0 THEN ? ELSE text END,excerpt=?,quality=?,meta_json=? WHERE id=?",
                           (vals[0], vals[1], vals[2], vals[3], vals[4], vals[5], vals[6], vals[7], vals[7], vals[8], vals[9], vals[10], sid))
            else:
                sid = self.db.ex("INSERT INTO sources(canonical_url,url,title,domain,source_type,published,retrieved_at,"
                                 "content_hash,text,excerpt,quality,meta_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (c.canonical,) + vals)
            idmap[i] = sid
            self.db.index("source", sid, c.title or c.url, (text or c.snippet))
        ranked = [c for c in analysis]
        label_n = 0
        for c in ranked:
            sid = idmap.get(order.index(c))
            if sid is None:
                continue
            label_n += 1
            self.db.ex("INSERT OR REPLACE INTO run_sources(run_id,source_id,label,rank,query,snippet,provider,fetched) VALUES(?,?,?,?,?,?,?,?)",
                       (run_id, sid, f"S{label_n}", label_n, (c.queries[0] if c.queries else ""), c.snippet[:500],
                        ",".join(sorted(c.providers)), int(bool(c.text))))
            for p in ev.best_passages(c.text or c.snippet, question, 3, 400):
                self.db.ex("INSERT INTO passages(run_id,source_id,text) VALUES(?,?,?)", (run_id, sid, p))
        # duplicates: link to their original and list them in the register (label D#, never cited as evidence)
        dn = 0
        for i, c in enumerate(order):
            if c.duplicate_of is not None and i in idmap and c.duplicate_of in idmap:
                self.db.ex("UPDATE sources SET duplicate_of=? WHERE id=?", (idmap[c.duplicate_of], idmap[i]))
                dn += 1
                self.db.ex("INSERT OR IGNORE INTO run_sources(run_id,source_id,label,rank,query,snippet,provider,fetched) VALUES(?,?,?,?,?,?,?,?)",
                           (run_id, idmap[i], f"D{dn}", 1000 + dn, "", c.snippet[:300], ",".join(sorted(c.providers)), int(bool(c.text))))

    def _register(self, run_id) -> list[ev.SourceRec]:
        from . import report
        return report.load_register(self.db, run_id)

    def _prompt_sources(self, register, pol) -> list[dict]:
        out = []
        for r in register:
            out.append({"label": r.label, "title": r.title, "url": r.url, "domain": r.domain, "type": r.stype,
                        "published": r.published, "text": r.text})
        return out

    def _browser_prompt(self, run, pol, question, register, notes) -> str:
        srcs = self._prompt_sources(register, pol)
        return build_analysis_prompt(question, run["mode"], srcs, style=pol["report_style"],
                                     budget_chars=min(pol["llm_context_chars"], 60000), notes=notes)

    def _synthesise(self, run, pol, question, srcs, active, stats, stage, notes, limitations, cancel) -> tuple[str, str]:
        budget = pol["llm_context_chars"]
        if self._low_memory():
            budget //= 2
        for llm in active:
            if cancel.is_set():
                break
            for attempt in range(2):                          # second attempt halves the context (low-RAM degradation)
                prompt = build_analysis_prompt(question, run["mode"], srcs, style=pol["report_style"],
                                               budget_chars=budget, notes=notes)
                stage("synthesise", f"Analysing {len(srcs)} sources with {llm.tag}", 72 + attempt * 5, llm.tag)
                try:
                    timeout = min(600, max(60, pol["time_budget_s"] // 2))
                    txt = llm.complete(prompt, max_tokens=3500 if pol["report_style"] in ("detailed", "comprehensive") else 2000,
                                       timeout=timeout)
                    stats["stage_providers"].append({"stage": "synthesis", "provider": llm.tag})
                    return txt, llm.tag
                except ProviderError as e:
                    stats["provider_errors"][llm.id] = str(e)
                    limitations.append(f"{llm.tag} failed during synthesis: {e}")
                    if attempt == 0 and ("timed out" in str(e) or "memory" in str(e)):
                        budget //= 2
                        limitations.append("Retried with a halved context to fit a constrained model.")
                        continue
                    break
        stage("synthesise", "No AI model available: building extractive brief; browser-assisted prompt ready", 80, "browser-assisted")
        return "", "browser-assisted"

    def _cross_check(self, run_id, body, search, fetcher, pol, flt, question, stats, progress, cancel):
        from . import report
        reg = report.load_register(self.db, run_id)
        labels = {r.label: r for r in reg}
        idx = report.build_index(reg)
        claims = ev.extract_claims(body, labels, idx, 40)
        weak = [c for c in claims if c["independent_count"] < 2][:6]
        if not weak:
            stats["cross_check"] = {"claims_checked": 0, "note": "no single-source claims"}
            return
        have = {r.url for r in reg}
        found: list[Cand] = []
        adapters = search.enabled_for(["web", "scholar"])
        deadline = time.monotonic() + 90
        for wc in weak:
            if cancel.is_set() or time.monotonic() > deadline:
                break
            q = " ".join(keywords(wc["text"], 8))
            for a in adapters[:3]:
                for h in search.run(a, q, 4, flt, 1, deadline):
                    if h.url not in have and security.canonical_url(h.url) not in {security.canonical_url(x) for x in have}:
                        found.append(Cand(url=h.url, canonical=security.canonical_url(h.url), title=h.title, snippet=h.snippet,
                                          published=h.published, providers={h.provider}, categories={h.category},
                                          type_hint=h.type_hint, doi=h.doi))
                        have.add(h.url)
        found = found[:10]
        self._fetch_all(fetcher, found, {**pol, "fetch_pages": 10}, deadline, progress, cancel)
        usable = [c for c in found if c.text]
        added = 0
        for c in usable:
            c.stype = ev.classify_source(c.url, c.type_hint)
            c.quality = ev.quality_score(c.stype, has_date=bool(c.published), text_len=len(c.text), https=True)
        # attach new sources to the run only when a passage genuinely supports a weak claim
        n_labels = len(reg)
        for wc_i, wc in enumerate(weak):
            for c in usable:
                ps = ev.best_passages(c.text, wc["text"], 1, 300)
                ctoks = set(ev.tokens(wc["text"]))
                if ps and ctoks and len(ctoks & set(ev.tokens(ps[0]))) / len(ctoks) >= 0.5:
                    row = self.db.one("SELECT id FROM sources WHERE canonical_url=?", (c.canonical,))
                    if not row:
                        self._persist_one(c)
                        row = self.db.one("SELECT id FROM sources WHERE canonical_url=?", (c.canonical,))
                    if not self.db.one("SELECT 1 FROM run_sources WHERE run_id=? AND source_id=?", (run_id, row["id"])):
                        n_labels += 1
                        added += 1
                        self.db.ex("INSERT INTO run_sources(run_id,source_id,label,rank,query,snippet,provider,fetched) VALUES(?,?,?,?,?,?,?,1)",
                                   (run_id, row["id"], f"S{n_labels}", n_labels, "cross-check", c.snippet[:300], "cross-check"))
        stats["cross_check"] = {"claims_checked": len(weak), "new_sources": added,
                                "note": "Additional sources are attached only where a stored passage overlaps the claim."}

    def _persist_one(self, c: Cand):
        text = c.text[: self.settings["storage"]["max_text_per_source"]]
        sid = self.db.ex("INSERT OR IGNORE INTO sources(canonical_url,url,title,domain,source_type,published,retrieved_at,content_hash,text,excerpt,quality,meta_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                         (c.canonical, c.url, c.title or c.url, security.domain_of(c.url), c.stype, c.published, now(),
                          ev.content_hash(text), text, text[:600], c.quality, j({"providers": sorted(c.providers)})))
        row = self.db.one("SELECT id FROM sources WHERE canonical_url=?", (c.canonical,))
        if row:
            self.db.index("source", row["id"], c.title or c.url, text)

    def _seed_from_parent(self, parent_id, pool, order):
        rows = self.db.q("SELECT s.* FROM run_sources rs JOIN sources s ON s.id=rs.source_id WHERE rs.run_id=? ORDER BY rs.rank", (parent_id,))
        for r in rows:
            c = Cand(url=r["url"], canonical=r["canonical_url"], title=r["title"], snippet=r["excerpt"], published=r["published"],
                     providers={"carried-over"}, text=r["text"], fetched=bool(r["text"]), score=0.05, carried_id=r["id"])
            pool[c.canonical] = c
            order.append(c)

    # ---- browser-assisted answer ------------------------------------------
    def attach_ai_response(self, run_id: int, text: str, provider_label: str = "browser-assisted") -> dict:
        from . import report
        run = self.db.one("SELECT * FROM runs WHERE id=?", (run_id,))
        if not run:
            raise ValidationError("Research run not found.")
        text = security.clean_text(text)
        if len(text) < 40:
            raise ValidationError("The pasted response is too short.")
        if len(text) > 200_000:
            raise ValidationError("The pasted response is too long.")
        provider_label = (provider_label or "browser-assisted")[:80]
        reg = report.load_register(self.db, run_id)
        clean, invalid = ev.validate_citations(text, {r.label for r in reg})
        self.db.ex("INSERT INTO notes(run_id,kind,provider,body,created_at) VALUES(?,?,?,?,?)",
                   (run_id, "ai_response", provider_label, clean, now()))
        self.db.ex("UPDATE runs SET provider=? WHERE id=?", (f"browser-assisted:{provider_label}"[:100], run_id))
        report.rebuild(self.db, run_id)
        return {"run_id": run_id, "invalid_citations": invalid, "citations_valid": len(ev.CITE_RE.findall(clean))}
