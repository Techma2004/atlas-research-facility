"""FastAPI application: JSON API + static dashboard. Binds to localhost by default."""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__, config, report, security
from .config import MODES, SettingsError, data_dir, get_mode_policy, load_settings, mask, save_settings, secret
from .db import Database, now as db_now
from .engine import ResearchEngine, ValidationError, mem_available_mb, validate_request
from .jobs import JobManager
from .onion import OnionConnector, OnionError, run_onion_research
from .providers import Router
from .search import SearchManager

STATIC = config.ROOT / "static"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}
MAX_BODY = 2_000_000
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def create_app(db_path: Path | None = None, engine_kwargs: dict | None = None, max_concurrent: int = 1) -> FastAPI:
    ddir = data_dir()
    db = Database(db_path or (ddir / "atlas.db"))
    engine_kwargs = engine_kwargs or {}

    def engine_factory() -> ResearchEngine:
        return ResearchEngine(db, load_settings(), **engine_kwargs)

    jobs = JobManager(db, engine_factory, max_concurrent=max_concurrent)
    app = FastAPI(title="ATLAS Research Facility", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    extra_hosts = {h.strip().lower() for h in os.getenv("ATLAS_ALLOWED_HOSTS", "").split(",") if h.strip()}

    # ---------------------------------------------------------------- middleware
    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = (request.headers.get("host") or "").lower()
        hostname = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
        if hostname not in LOCAL_HOSTS and hostname not in extra_hosts:
            return JSONResponse({"detail": "Host not allowed."}, status_code=403)     # DNS-rebinding defence
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("origin")
            if origin:
                o = origin.split("://", 1)[-1].lower()
                if o != host:
                    return JSONResponse({"detail": "Cross-origin request blocked."}, status_code=403)
            ctype = request.headers.get("content-type", "")
            clen = request.headers.get("content-length", "0")
            if clen.isdigit() and int(clen) > MAX_BODY:
                return JSONResponse({"detail": "Request too large."}, status_code=413)
            if clen not in ("0", "") and not ctype.startswith("application/json"):
                return JSONResponse({"detail": "Content-Type must be application/json."}, status_code=415)
        try:
            resp = await call_next(request)
        except Exception as e:                                           # noqa: BLE001
            return JSONResponse({"detail": "Internal error: " + security.safe_error(e)[:120]}, status_code=500)
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api") else "no-cache"
        return resp

    async def body_json(request: Request) -> dict:
        try:
            data = await request.json()
        except ValueError:
            raise HTTPException(400, "Request body must be valid JSON.")
        if not isinstance(data, dict):
            raise HTTPException(400, "Request body must be a JSON object.")
        return data

    def run_or_404(rid: int) -> dict:
        row = db.one("SELECT * FROM runs WHERE id=?", (rid,))
        if not row:
            raise HTTPException(404, "Research run not found.")
        return row

    def bad(e: Exception):
        raise HTTPException(400, str(e))

    # ---------------------------------------------------------------- pages
    @app.get("/", include_in_schema=False)
    def home():
        return FileResponse(STATIC / "index.html", media_type="text/html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # ---------------------------------------------------------------- status / settings
    @app.get("/api/status")
    def status():
        s = load_settings()
        router = Router(s, engine_kwargs.get("llm_client"))
        provs = router.info()
        sm = SearchManager(s, engine_kwargs.get("search_client"))
        return {"version": __version__, "schema_version": db.version(), "data_dir": str(data_dir()),
                "providers": [p.__dict__ for p in provs],
                "local_ready": provs[0].available, "default_provider": s["default_provider"],
                "search_providers": sm.status(), "memory_available_mb": mem_available_mb(),
                "onion": OnionConnector(s).status(),
                "modes": {k: {kk: get_mode_policy(k, s)[kk] for kk in (
                    "label", "min_sources", "target_sources", "max_sources", "queries_per_round", "max_rounds",
                    "fetch_pages", "time_budget_s", "follow_citations", "cross_check", "subquestions", "providers")}
                    for k in MODES}}

    @app.get("/api/settings")
    def get_settings():
        s = load_settings()
        return {"settings": s, "secrets": {k: {"configured": bool(secret(k)), "hint": mask(secret(k))} for k in config.SECRET_ENV},
                "secret_help": "API keys are read from environment variables only (OPENAI_API_KEY, BRAVE_API_KEY). "
                               "ATLAS never stores or displays them. API billing is separate from consumer chat subscriptions."}

    @app.put("/api/settings")
    async def put_settings(request: Request):
        patch = await body_json(request)
        if isinstance(patch.get("onion"), dict) and patch["onion"].get("enabled") is True:
            raise HTTPException(400, "The onion module can only be enabled through its explicit activation control.")
        try:
            s = save_settings(patch)
        except SettingsError as e:
            bad(e)
        return {"settings": s}

    @app.get("/api/models")
    def models():
        s = load_settings()
        loc = Router(s, engine_kwargs.get("llm_client")).local()
        m = loc.list_models()
        return {"base_url": loc.base, "models": m, "configured": s.get("local_model", ""),
                "message": "" if m else f"No model server answered at {loc.base}. Start Ollama or llama.cpp's server, "
                                        "or fix the URL in Settings. ATLAS never downloads models for you."}

    # ---------------------------------------------------------------- research
    @app.post("/api/research")
    async def start_research(request: Request):
        body = await body_json(request)
        try:
            req = validate_request(body)
            rid = engine_factory().create_run(req)
        except ValidationError as e:
            bad(e)
        jobs.start(rid)
        return {"id": rid, "status": "queued"}

    @app.get("/api/research/{rid}/progress")
    def progress(rid: int):
        try:
            p = jobs.progress(rid)
        except KeyError:
            raise HTTPException(404, "Research run not found.")
        return p

    @app.post("/api/research/{rid}/cancel")
    def cancel(rid: int):
        run_or_404(rid)
        return {"cancelled": jobs.cancel(rid)}

    @app.post("/api/runs/{rid}/continue")
    async def continue_run(rid: int, request: Request):
        parent = run_or_404(rid)
        body = await body_json(request) if request.headers.get("content-length", "0") != "0" else {}
        stats = json.loads(parent["stats_json"] or "{}").get("request", {})
        prov = body.get("provider") or parent["provider"]
        prov = prov if prov in ("local", "hybrid", "openai", "browser") else "hybrid"
        req_body = {"question": body.get("question") or parent["question"], "mode": body.get("mode") or parent["mode"],
                    "provider": prov, "sensitive": bool(parent["sensitive"]), "filters": stats.get("filters", {}),
                    "parent_run_id": rid}
        try:
            req = validate_request(req_body)
            new = engine_factory().create_run(req)
        except ValidationError as e:
            bad(e)
        jobs.start(new)
        return {"id": new, "status": "queued", "parent_run_id": rid}

    # ---------------------------------------------------------------- library
    @app.get("/api/runs")
    def list_runs(q: str = "", mode: str = "", status: str = "", date_from: str = "", date_to: str = "",
                  project: str = "", channel: str = "", limit: int = 50, offset: int = 0):
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        where, args = [], []
        if q.strip():
            ids = [r["ref_id"] for r in db.search(q, "run", 200)]
            if not ids:
                return {"runs": [], "total": 0}
            where.append(f"r.id IN ({','.join('?' * len(ids))})")
            args += ids
        if mode in MODES:
            where.append("r.mode=?"); args.append(mode)
        if status in ("completed", "failed", "cancelled", "running", "queued", "interrupted"):
            where.append("r.status=?"); args.append(status)
        if re.match(r"^\d{4}-\d{2}-\d{2}$", date_from):
            where.append("substr(r.created_at,1,10)>=?"); args.append(date_from)
        if re.match(r"^\d{4}-\d{2}-\d{2}$", date_to):
            where.append("substr(r.created_at,1,10)<=?"); args.append(date_to)
        if project:
            where.append("p.name=?"); args.append(project[:80])
        if channel in ("web", "onion"):
            where.append("r.channel=?"); args.append(channel)
        w = (" WHERE " + " AND ".join(where)) if where else ""
        base = f"FROM runs r LEFT JOIN projects p ON p.id=r.project_id{w}"
        total = db.one(f"SELECT COUNT(*) AS n {base}", tuple(args))["n"]  # nosec B608 - fixed clauses, values bound
        rows = db.q(f"SELECT r.id,r.question,r.mode,r.status,r.provider,r.created_at,r.parent_run_id,r.channel,p.name AS project,"
                    f"(SELECT COUNT(*) FROM run_sources rs WHERE rs.run_id=r.id AND rs.label LIKE 'S%') AS sources "
                    f"{base} ORDER BY r.id DESC LIMIT ? OFFSET ?", tuple(args) + (limit, offset))  # nosec B608
        return {"runs": rows, "total": total}

    @app.get("/api/library/search")
    def library_search(q: str = "", kind: str = ""):
        if kind not in ("", "run", "source"):
            raise HTTPException(400, "kind must be run or source.")
        return {"results": db.search(q, kind or None, 30)}

    @app.get("/api/projects")
    def projects():
        return {"projects": db.q("SELECT p.id,p.name,p.created_at,(SELECT COUNT(*) FROM runs r WHERE r.project_id=p.id) AS runs FROM projects p ORDER BY p.name")}

    @app.get("/api/runs/{rid}")
    def run_detail(rid: int):
        run = run_or_404(rid)
        d = report.report_data(db, rid)
        d["progress"] = jobs.progress(rid) if run["status"] in ("queued", "running") else None
        d["browser_prompt"], d["error"], d["channel"] = run["browser_prompt"], run["error"], run["channel"]
        d["parent"] = (db.one("SELECT id,question,created_at FROM runs WHERE id=?", (run["parent_run_id"],))
                       if run["parent_run_id"] else None)
        return d

    @app.get("/api/runs/{rid}/prompt")
    def prompt(rid: int, download: bool = False):
        run = run_or_404(rid)
        if not run["browser_prompt"]:
            raise HTTPException(404, "No browser-assisted prompt is stored for this run.")
        if download:
            return PlainTextResponse(run["browser_prompt"], headers={"Content-Disposition": f'attachment; filename="atlas-prompt-{rid}.txt"'})
        return {"prompt": run["browser_prompt"], "chars": len(run["browser_prompt"]),
                "instructions": ["Copy this prompt.", "Open your preferred AI website and sign in normally.",
                                 "Paste the prompt, submit it, and copy the answer.", "Paste the answer back into ATLAS."]}

    @app.post("/api/runs/{rid}/response")
    async def paste_response(rid: int, request: Request):
        run_or_404(rid)
        body = await body_json(request)
        txt = body.get("text")
        if not isinstance(txt, str):
            raise HTTPException(400, "Provide the pasted response as 'text'.")
        try:
            return engine_factory().attach_ai_response(rid, txt, str(body.get("provider_label") or "browser-assisted"))
        except ValidationError as e:
            bad(e)

    @app.post("/api/runs/{rid}/notes")
    async def add_note(rid: int, request: Request):
        run_or_404(rid)
        body = await body_json(request)
        txt = body.get("text")
        if not isinstance(txt, str) or not txt.strip() or len(txt) > 20000:
            raise HTTPException(400, "Note text is required (max 20000 characters).")
        txt = security.clean_text(txt)
        nid = db.ex("INSERT INTO notes(run_id,kind,provider,body,created_at) VALUES(?,?,?,?,?)", (rid, "note", "user", txt, db_now()))
        db.index("note", rid, "note", txt)
        return {"id": nid}

    @app.get("/api/runs/{a}/compare/{b}")
    def compare(a: int, b: int):
        ra, rb = run_or_404(a), run_or_404(b)
        sa = {r["canonical_url"]: r for r in db.q("SELECT s.canonical_url,s.title,s.url FROM run_sources rs JOIN sources s ON s.id=rs.source_id WHERE rs.run_id=? AND rs.label LIKE 'S%'", (a,))}
        sb = {r["canonical_url"]: r for r in db.q("SELECT s.canonical_url,s.title,s.url FROM run_sources rs JOIN sources s ON s.id=rs.source_id WHERE rs.run_id=? AND rs.label LIKE 'S%'", (b,))}
        ca = {c["text"] for c in db.q("SELECT text FROM claims WHERE run_id=?", (a,))}
        cb = {c["text"] for c in db.q("SELECT text FROM claims WHERE run_id=?", (b,))}
        return {"a": {"id": a, "question": ra["question"], "mode": ra["mode"], "created_at": ra["created_at"], "sources": len(sa), "claims": len(ca)},
                "b": {"id": b, "question": rb["question"], "mode": rb["mode"], "created_at": rb["created_at"], "sources": len(sb), "claims": len(cb)},
                "shared_sources": [sa[k] for k in sa if k in sb], "only_a": [sa[k] for k in sa if k not in sb],
                "only_b": [sb[k] for k in sb if k not in sa], "claims_only_a": sorted(ca - cb)[:30], "claims_only_b": sorted(cb - ca)[:30]}

    @app.delete("/api/runs")
    async def delete_runs(request: Request):
        body = await body_json(request)
        ids = body.get("ids")
        if body.get("confirm") is not True:
            raise HTTPException(400, "Deletion requires confirm: true.")
        if not isinstance(ids, list) or not ids or len(ids) > 500 or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
            raise HTTPException(400, "Provide a list of run ids.")
        for i in ids:
            jobs.cancel(i)
        return {"deleted": db.delete_runs(ids)}

    @app.get("/api/runs/{rid}/export")
    def export(rid: int, format: str = "md"):
        run = run_or_404(rid)
        name = f"atlas-{rid}"
        if format == "md":
            return Response(run["report_md"], media_type="text/markdown; charset=utf-8",
                            headers={"Content-Disposition": f'attachment; filename="{name}.md"'})
        if format == "html":
            return Response(report.export_html(db, rid), media_type="text/html; charset=utf-8",
                            headers={"Content-Disposition": f'attachment; filename="{name}.html"',
                                     "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})
        if format == "json":
            return Response(json.dumps(report.report_data(db, rid), indent=2, ensure_ascii=False), media_type="application/json",
                            headers={"Content-Disposition": f'attachment; filename="{name}.json"'})
        if format in ("csv", "csv-sources"):
            return Response(report.sources_csv(db, rid), media_type="text/csv; charset=utf-8",
                            headers={"Content-Disposition": f'attachment; filename="{name}-sources.csv"'})
        if format == "csv-evidence":
            return Response(report.evidence_csv(db, rid), media_type="text/csv; charset=utf-8",
                            headers={"Content-Disposition": f'attachment; filename="{name}-evidence.csv"'})
        raise HTTPException(400, "format must be md, html, json, csv-sources or csv-evidence.")

    # ---------------------------------------------------------------- backup / restore
    bdir = lambda: (data_dir() / "backups")          # noqa: E731

    @app.get("/api/backups")
    def backups():
        d = bdir()
        files = sorted(d.glob("atlas-*.db"), reverse=True) if d.exists() else []
        return {"backups": [{"name": f.name, "bytes": f.stat().st_size} for f in files]}

    @app.post("/api/backup")
    def make_backup():
        name = "atlas-" + re.sub(r"[^0-9]", "", db_now())[:14] + ".db"
        db.backup(bdir() / name)
        keep = load_settings()["storage"]["backup_keep"]
        for old in sorted(bdir().glob("atlas-*.db"), reverse=True)[max(1, keep):]:
            old.unlink(missing_ok=True)
        return {"name": name}

    @app.post("/api/restore")
    async def restore(request: Request):
        body = await body_json(request)
        name = body.get("name")
        if body.get("confirm") is not True:
            raise HTTPException(400, "Restore requires confirm: true.")
        if not isinstance(name, str) or not re.fullmatch(r"atlas-\d{8,14}\.db", name):
            raise HTTPException(400, "Invalid backup name.")
        try:
            path = security.ensure_within(bdir(), bdir() / name)
        except PermissionError:
            raise HTTPException(400, "Invalid backup path.")
        if not path.exists():
            raise HTTPException(404, "Backup not found.")
        try:
            db.restore(path)
        except ValueError as e:
            bad(e)
        return {"restored": name}

    # ---------------------------------------------------------------- onion (optional)
    @app.get("/api/onion/status")
    def onion_status():
        return OnionConnector(load_settings()).status()

    @app.post("/api/onion/enable")
    async def onion_enable(request: Request):
        body = await body_json(request)
        if body.get("confirm") is not True:
            raise HTTPException(400, "Enabling the onion module requires confirm: true.")
        allow = body.get("allowlist")
        patch = {"onion": {"enabled": True}}
        if allow is not None:
            if not isinstance(allow, list) or len(allow) > 50 or not all(isinstance(a, str) and re.fullmatch(r"[a-z2-7.]{8,80}\.onion", a.strip().lower()) for a in allow):
                raise HTTPException(400, "Allowlist must be a list of valid .onion domains.")
            patch["onion"]["allowlist"] = [a.strip().lower() for a in allow]
        if not (patch["onion"].get("allowlist") or load_settings()["onion"]["allowlist"]):
            raise HTTPException(400, "Add at least one allowlisted .onion domain before enabling.")
        save_settings(patch)
        return OnionConnector(load_settings()).status()

    @app.post("/api/onion/kill")
    def onion_kill():
        OnionConnector(load_settings()).kill()
        return OnionConnector(load_settings()).status()

    @app.post("/api/onion/research")
    async def onion_research(request: Request):
        body = await body_json(request)
        conn = OnionConnector(load_settings(), **({"client": engine_kwargs["onion_client"]} if "onion_client" in engine_kwargs else {}))
        urls = body.get("urls")
        if not isinstance(urls, list) or not all(isinstance(u, str) for u in urls):
            raise HTTPException(400, "urls must be a list of strings.")
        try:
            return run_onion_research(db, conn, str(body.get("question", "")), urls)
        except OnionError as e:
            bad(e)

    app.state.db, app.state.jobs, app.state.engine_factory = db, jobs, engine_factory
    return app
