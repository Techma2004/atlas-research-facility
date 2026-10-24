"""End-to-end pipeline behaviour against the mocked internet."""
import json
import threading
import time

import httpx

from atlas.config import get_mode_policy, save_settings
from atlas.engine import Progress, ResearchEngine
from conftest import run_req


def stats(db, rid):
    return json.loads(db.one("SELECT stats_json FROM runs WHERE id=?", (rid,))["stats_json"])


def run(engine, **kw):
    rid = run_req(engine, **kw)
    res = engine.execute(rid)
    return rid, res


def test_light_with_local_ai_end_to_end(engine, db):
    rid, res = run(engine, mode="light", provider="local")
    r = db.one("SELECT * FROM runs WHERE id=?", (rid,))
    assert res["status"] == "completed" and r["provider"] == "local:tiny-model"
    assert "Source register" in r["report_md"] and "Executive summary" in r["report_md"]
    srcs = db.q("SELECT s.* FROM run_sources rs JOIN sources s ON s.id=rs.source_id WHERE rs.run_id=? AND rs.label LIKE 'S%'", (rid,))
    assert 3 <= len(srcs) <= 5                                  # light: ~3-5 sources
    assert all(s["retrieved_at"] and s["url"].startswith("https://") and s["domain"] for s in srcs)


def test_modes_produce_different_effective_work(engine, db, world):
    results = {}
    for mode in ("light", "medium", "high"):
        rid, _ = run(engine, mode=mode, provider="local")
        st = stats(db, rid)
        results[mode] = (len(st["queries"]), st["rounds"], len(db.q("SELECT 1 FROM run_sources WHERE run_id=? AND label LIKE 'S%'", (rid,))))
    assert results["light"][0] < results["medium"][0]
    assert results["high"][1] >= results["medium"][1]
    assert results["light"][2] <= 5 < results["medium"][2] + 6
    assert results["medium"][2] >= results["light"][2]


def test_high_mode_uses_subquestions_and_scholarly_sources(engine, db):
    rid, _ = run(engine, mode="high", provider="local")
    st = stats(db, rid)
    assert st["policy"]["subquestions"] and st["subquestions"]
    types = {r["source_type"] for r in db.q("SELECT s.source_type FROM run_sources rs JOIN sources s ON s.id=rs.source_id WHERE rs.run_id=?", (rid,))}
    assert types & {"scholarly", "preprint"}
    assert any("openalex" in q["provider"] or "openalex" in q["provider"] for q in db.q("SELECT provider FROM run_sources WHERE run_id=?", (rid,)))


def test_high_mode_follows_references(engine, db, world):
    rid, _ = run(engine, mode="high", provider="local")
    assert any("filter=openalex%3AW99" in r for r in world.requests)           # reference lookup was issued
    titles = [r["title"] for r in db.q("SELECT s.title FROM run_sources rs JOIN sources s ON s.id=rs.source_id WHERE rs.run_id=?", (rid,))]
    assert "Referenced work on ceramic electrolytes" in titles                  # and the cited work entered the register


def test_max_mode_cross_check_runs(engine, db):
    rid, res = run(engine, mode="max", provider="local")
    st = stats(db, rid)
    assert res["status"] == "completed" and st["policy"]["cross_check"] and "cross_check" in st


def test_syndicated_duplicates_detected_in_register(engine, db):
    rid, _ = run(engine, mode="medium", provider="local")
    dups = db.q("SELECT * FROM run_sources WHERE run_id=? AND label LIKE 'D%'", (rid,))
    md = db.one("SELECT report_md FROM runs WHERE id=?", (rid,))["report_md"]
    assert dups and "not counted as independent" in md


def test_prompt_injection_in_source_is_flagged_not_obeyed(engine, db, world):
    seen = {}
    world.llm_reply = lambda p: seen.setdefault("prompt", p) and "## Summary\nBatteries matter [S1]."
    rid, _ = run(engine, mode="medium", provider="local")
    assert "Ignore all previous instructions" not in seen["prompt"] or "flagged-instruction-like-text" in seen["prompt"]
    assert "<<<UNTRUSTED_SOURCE" in seen["prompt"]
    lim = json.loads(db.one("SELECT limitations_json FROM runs WHERE id=?", (rid,))["limitations_json"])
    assert any("instruction-like" in x for x in lim)


def test_every_citation_and_claim_maps_to_stored_source(engine, db):
    rid, _ = run(engine, mode="medium", provider="local")
    md = db.one("SELECT report_md FROM runs WHERE id=?", (rid,))["report_md"]
    import re
    body = md.split("## Source register")[0]
    cited = set(re.findall(r"\[(S\d+)\]", body))
    labels = {r["label"] for r in db.q("SELECT label FROM run_sources WHERE run_id=?", (rid,))}
    assert cited and cited <= labels and "S77" not in cited
    for ce in db.q("SELECT ce.source_id FROM claim_evidence ce JOIN claims c ON c.id=ce.claim_id WHERE c.run_id=?", (rid,)):
        assert db.one("SELECT 1 FROM sources WHERE id=?", (ce["source_id"],))


def test_no_local_model_falls_back_to_browser_assisted(engine, db, world):
    world.fail_hosts = {"127.0.0.1"}
    rid, res = run(engine, mode="light", provider="hybrid")
    r = db.one("SELECT * FROM runs WHERE id=?", (rid,))
    assert res["status"] == "completed" and r["provider"] == "browser-assisted"
    assert "Extractive findings" in r["report_md"] and r["browser_prompt"]
    assert "UNTRUSTED_SOURCE" in r["browser_prompt"]
    lim = json.loads(r["limitations_json"])
    assert any("No AI provider was available" in x for x in lim)


def test_browser_assisted_round_trip(engine, db, world):
    world.fail_hosts = {"127.0.0.1"}
    rid, _ = run(engine, mode="light", provider="browser")
    labels = [r["label"] for r in db.q("SELECT label FROM run_sources WHERE run_id=? AND label LIKE 'S%'", (rid,))]
    pasted = f"## Executive summary\nSolid state cells improve density [{labels[0]}]. Invented claim [S88].\n## Findings\nMore text here for length."
    out = engine.attach_ai_response(rid, pasted, "my chat site")
    assert out["invalid_citations"] == ["S88"]
    r = db.one("SELECT * FROM runs WHERE id=?", (rid,))
    assert "Solid state cells improve density" in r["report_md"] and "[S88]" not in r["report_md"]
    assert "my chat site" in r["provider"]
    assert db.q("SELECT 1 FROM notes WHERE run_id=? AND kind='ai_response'", (rid,))
    # uncited response: flagged unverified, extractive findings appended
    out2 = engine.attach_ai_response(rid, "Completely uncited prose that claims many things without sources at all here.")
    assert "UNVERIFIED" in db.one("SELECT report_md FROM runs WHERE id=?", (rid,))["report_md"]


def test_sensitive_run_never_uses_external_provider(engine, db, world, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key-123456")
    s = save_settings({"allow_external_providers": True})
    eng = ResearchEngine(db, s, search_client=engine.search_client, fetch_client=engine.fetch_client, llm_client=engine.llm_client, resolver=engine.resolver)
    world.fail_hosts = {"127.0.0.1"}                      # local down
    rid = run_req(eng, mode="light", provider="hybrid", sensitive=True)
    eng.execute(rid)
    assert not any("api.openai.com" in r for r in world.requests)
    assert db.one("SELECT provider FROM runs WHERE id=?", (rid,))["provider"] == "browser-assisted"


def test_openai_provider_used_only_when_explicitly_allowed(engine, db, world, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key-123456")
    orig = world.handler

    def h(req):
        if req.url.host == "api.openai.com":
            world.requests.append("OPENAI " + str(req.url))
            return httpx.Response(200, json={"choices": [{"message": {"content": "## Summary\nOnline analysis [S1]."}}]})
        return orig(req)
    world.handler = h
    clients = {k: httpx.Client(transport=httpx.MockTransport(world.handler), follow_redirects=(k != "fetch_client")) for k in ("search_client", "fetch_client", "llm_client")}
    s = save_settings({"allow_external_providers": True})
    eng = ResearchEngine(db, s, resolver=engine.resolver, **clients)
    rid = run_req(eng, mode="light", provider="openai")
    eng.execute(rid)
    assert db.one("SELECT provider FROM runs WHERE id=?", (rid,))["provider"].startswith("openai")
    # and without the explicit permission: nothing goes out
    world.requests.clear()
    s = save_settings({"allow_external_providers": False})
    eng2 = ResearchEngine(db, s, resolver=engine.resolver, **clients)
    rid2 = run_req(eng2, mode="light", provider="openai")
    eng2.execute(rid2)
    assert not any(r.startswith("OPENAI") for r in world.requests)


def test_search_total_failure_reported_clearly(engine, db, world):
    world.ddg_status = 403
    world.fail_hosts = {"en.wikipedia.org"}
    rid, res = run(engine, mode="light", provider="local")
    r = db.one("SELECT * FROM runs WHERE id=?", (rid,))
    lim = json.loads(r["limitations_json"])
    assert any("No search results" in x and "duckduckgo" in x for x in lim)
    assert "No sources could be retrieved" in " ".join(lim) and res["status"] == "completed"


def test_local_model_timeout_degrades_gracefully(engine, db, world):
    world.llm_status = 500
    rid, res = run(engine, mode="light", provider="local")
    r = db.one("SELECT * FROM runs WHERE id=?", (rid,))
    assert res["status"] == "completed" and r["provider"] == "browser-assisted"
    assert any("failed during synthesis" in x for x in json.loads(r["limitations_json"]))


def test_cancellation_stops_and_saves_partial_results(engine, db, world):
    cancel = threading.Event()
    orig = world.handler

    def h(req):
        if req.url.host == "html.duckduckgo.com":
            cancel.set()                                  # user cancels while searching
        return orig(req)
    world.handler = h
    for k in ("search_client", "fetch_client", "llm_client"):
        setattr(engine, k, httpx.Client(transport=httpx.MockTransport(world.handler), follow_redirects=True))
    rid = run_req(engine, mode="medium", provider="local")
    prog = Progress()
    res = engine.execute(rid, prog, cancel)
    r = db.one("SELECT * FROM runs WHERE id=?", (rid,))
    assert res["status"] == "cancelled" and prog.status == "cancelled" and r["report_md"]


def test_time_budget_is_enforced(engine, db, world):
    orig = world.handler

    def slow(req):
        if req.url.host == "html.duckduckgo.com":
            time.sleep(0.4)
        return orig(req)
    world.handler = slow
    for k in ("search_client", "fetch_client", "llm_client"):
        setattr(engine, k, httpx.Client(transport=httpx.MockTransport(world.handler), follow_redirects=True))
    rid = run_req(engine, mode="max", provider="local", budget={"time_budget_s": 1})
    t0 = time.time()
    engine.execute(rid)
    assert time.time() - t0 < 20
    st = stats(db, rid)
    assert st["policy"]["time_budget_s"] == 1 and st["stop_reason"] in ("time budget", "round limit reached", "target source count reached")


def test_per_run_budget_can_only_tighten(engine, db):
    rid = run_req(engine, mode="light", provider="local", budget={"max_sources": 99, "time_budget_s": 9999})
    engine.execute(rid)
    p = stats(db, rid)["policy"]
    assert p["max_sources"] == get_mode_policy("light")["max_sources"] and p["time_budget_s"] == 90


def test_continue_investigation_carries_over_sources(engine, db):
    rid, _ = run(engine, mode="light", provider="local")
    n1 = {r["source_id"] for r in db.q("SELECT source_id FROM run_sources WHERE run_id=? AND label LIKE 'S%'", (rid,))}
    from atlas.engine import validate_request
    child = engine.create_run(validate_request({"question": "How do solid state batteries compare on energy density and manufacturing?", "mode": "medium", "provider": "local", "parent_run_id": rid}))
    engine.execute(child)
    n2 = {r["source_id"] for r in db.q("SELECT source_id FROM run_sources WHERE run_id=? AND label LIKE 'S%'", (child,))}
    assert len(n2) >= len(n1) and n1 & n2
    assert db.one("SELECT parent_run_id FROM runs WHERE id=?", (child,))["parent_run_id"] == rid


def test_page_cache_avoids_repeat_downloads(engine, db, world):
    run(engine, mode="light", provider="local")
    before = sum("batteries.example.com/solid-state" in r for r in world.requests)
    run(engine, mode="light", provider="local")
    after = sum("batteries.example.com/solid-state" in r for r in world.requests)
    assert before >= 1 and after == before


def test_progress_reports_stages_and_counts(engine, db):
    rid = run_req(engine, mode="light", provider="local")
    prog = Progress()
    engine.execute(rid, prog)
    snap = prog.snapshot()
    stages = {e["stage"] for e in snap["events"]}
    assert snap["percent"] == 100 and snap["status"] == "completed" and {"search", "fetch", "synthesise"} <= stages
    assert snap["counts"]["queries"] >= 1 and snap["counts"]["found"] >= 3
