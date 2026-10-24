"""HTTP endpoints, exports, onion module."""
import csv
import io
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from atlas import report
from atlas.config import load_settings, save_settings
from atlas.onion import OnionConnector, OnionError, run_onion_research
from atlas.server import create_app
from conftest import fake_resolver, run_req

H = {"host": "127.0.0.1:8765"}


@pytest.fixture
def client(data_dir, clients):
    app = create_app(engine_kwargs=clients)
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        c.app_state = app.state
        yield c


def wait_done(c, rid, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        p = c.get(f"/api/research/{rid}/progress").json()
        if p["status"] in ("completed", "failed", "cancelled"):
            return p
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def start(c, **kw):
    body = {"question": "How do solid state batteries compare on energy density and manufacturing?", "mode": "light", "provider": "local"}
    body.update(kw)
    r = c.post("/api/research", json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


# ------------------------------------------------------------ pages, status, security headers
def test_dashboard_loads_and_assets_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "ATLAS" in r.text and "/static/app.js" in r.text
    assert client.get("/static/app.js").status_code == 200 and client.get("/static/style.css").status_code == 200
    assert "script-src 'self'" in r.headers["content-security-policy"] and r.headers["x-frame-options"] == "DENY"
    assert "<script>" not in r.text.replace('<script src=', '')          # no inline scripts (CSP)


def test_status_lists_modes_providers_and_onion_off(client):
    s = client.get("/api/status").json()
    assert set(s["modes"]) == {"light", "medium", "high", "max"} and s["onion"]["enabled"] is False
    assert s["modes"]["max"]["max_sources"] == 100 and s["local_ready"] is True
    assert {p["id"] for p in s["providers"]} == {"local", "openai", "browser"}


def test_host_header_and_origin_protection(client):
    assert client.get("/api/status", headers={"host": "evil.example.com"}).status_code == 403       # DNS rebinding
    r = client.post("/api/research", json={"question": "valid question here?"}, headers={"origin": "http://evil.example.com"})
    assert r.status_code == 403
    r = client.post("/api/research", content="question=x", headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 415
    assert client.post("/api/research", content="{not json", headers={"content-type": "application/json"}).status_code == 400


def test_validation_errors_are_friendly(client):
    r = client.post("/api/research", json={"question": "x"})
    assert r.status_code == 400 and "at least 5" in r.json()["detail"]
    assert client.post("/api/research", json={"question": "valid question?", "mode": "warp"}).status_code == 400
    assert client.get("/api/runs/9999").status_code == 404


# ------------------------------------------------------------ research flow + library
def test_research_flow_history_and_export(client):
    rid = start(client, project="Battery study")
    p = wait_done(client, rid)
    assert p["status"] == "completed" and p["counts"]["found"] >= 3
    d = client.get(f"/api/runs/{rid}").json()
    assert d["status"] == "completed" and d["claims"] and d["sources"] and d["methodology"]
    lst = client.get("/api/runs").json()
    assert lst["total"] == 1 and lst["runs"][0]["project"] == "Battery study" and lst["runs"][0]["sources"] >= 3
    assert client.get("/api/runs?q=solid").json()["total"] == 1 and client.get("/api/runs?q=zebra").json()["total"] == 0
    assert client.get("/api/runs?mode=max").json()["total"] == 0 and client.get("/api/runs?status=completed").json()["total"] == 1
    assert client.get("/api/library/search?kind=source&q=ceramic").json()["results"]
    assert client.get("/api/projects").json()["projects"][0]["runs"] == 1

    md = client.get(f"/api/runs/{rid}/export?format=md")
    assert md.status_code == 200 and "attachment" in md.headers["content-disposition"] and "## Source register" in md.text
    j = client.get(f"/api/runs/{rid}/export?format=json").json()
    assert j["question"] and j["sources"] and j["claims"] and j["methodology"] and j["mode"] == "light"
    src_csv = list(csv.reader(io.StringIO(client.get(f"/api/runs/{rid}/export?format=csv-sources").text)))
    assert src_csv[0][:3] == ["label", "title", "url"] and len(src_csv) > 3
    ev_csv = list(csv.reader(io.StringIO(client.get(f"/api/runs/{rid}/export?format=csv-evidence").text)))
    assert ev_csv[0][0] == "claim_id" and len(ev_csv) > 1
    html = client.get(f"/api/runs/{rid}/export?format=html")
    assert html.status_code == 200 and "<h1>" in html.text and "content-security-policy" in html.headers
    assert client.get(f"/api/runs/{rid}/export?format=pdf").status_code == 400


def test_survives_restart(data_dir, clients):
    app1 = create_app(engine_kwargs=clients)
    with TestClient(app1, base_url="http://127.0.0.1:8765") as c:
        rid = start(c); wait_done(c, rid)
    app2 = create_app(engine_kwargs=clients)                        # "restart"
    with TestClient(app2, base_url="http://127.0.0.1:8765") as c2:
        d = c2.get(f"/api/runs/{rid}").json()
        assert d["status"] == "completed" and d["report_markdown"]


def test_interrupted_runs_marked_after_restart(data_dir, clients):
    app1 = create_app(engine_kwargs=clients)
    app1.state.db.ex("INSERT INTO runs(question,mode,status,created_at) VALUES('x y z a b','light','running','t')")
    app2 = create_app(engine_kwargs=clients)
    assert app2.state.db.one("SELECT status FROM runs WHERE id=1")["status"] == "interrupted"


def test_cancel_endpoint(client, world):
    def slow(req):
        if req.url.host == "html.duckduckgo.com":
            time.sleep(0.8)
    world.hook = slow
    rid = start(client, mode="medium")
    time.sleep(0.2)
    assert client.post(f"/api/research/{rid}/cancel").json()["cancelled"] is True
    assert wait_done(client, rid)["status"] == "cancelled"


def test_browser_prompt_and_paste_back_via_api(client, world):
    world.fail_hosts = {"127.0.0.1"}
    rid = start(client, provider="browser")
    wait_done(client, rid)
    p = client.get(f"/api/runs/{rid}/prompt").json()
    assert "RESEARCH QUESTION" in p["prompt"] and len(p["instructions"]) == 4
    dl = client.get(f"/api/runs/{rid}/prompt?download=true")
    assert "attachment" in dl.headers["content-disposition"]
    r = client.post(f"/api/runs/{rid}/response", json={"text": "## Summary\nA finding [S1]. Fake [S50].\nMore words to pass the minimum length.", "provider_label": "site"})
    assert r.json()["invalid_citations"] == ["S50"]
    assert "A finding" in client.get(f"/api/runs/{rid}").json()["report_markdown"]
    assert client.post(f"/api/runs/{rid}/response", json={"text": "short"}).status_code == 400
    assert client.post(f"/api/runs/{rid}/notes", json={"text": "my note"}).status_code == 200


def test_continue_and_compare_endpoints(client):
    a = start(client); wait_done(client, a)
    r = client.post(f"/api/runs/{a}/continue", json={"mode": "medium"})
    b = r.json()["id"]; wait_done(client, b)
    c = client.get(f"/api/runs/{a}/compare/{b}").json()
    assert c["a"]["id"] == a and c["shared_sources"]


def test_delete_requires_confirmation(client):
    rid = start(client); wait_done(client, rid)
    assert client.request("DELETE", "/api/runs", json={"ids": [rid]}).status_code == 400
    assert client.request("DELETE", "/api/runs", json={"ids": [rid], "confirm": True}).json()["deleted"] == 1
    assert client.get(f"/api/runs/{rid}").status_code == 404
    assert client.request("DELETE", "/api/runs", json={"ids": ["x"], "confirm": True}).status_code == 400


def test_settings_api_validation_and_no_secret_leak(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-verysecretvalue1234")
    r = client.get("/api/settings").json()
    assert "verysecret" not in json.dumps(r) and r["secrets"]["openai"]["configured"] is True
    assert client.put("/api/settings", json={"local_model": "tiny-model", "mode_overrides": {"light": {"max_sources": 3}}}).status_code == 200
    assert client.get("/api/status").json()["modes"]["light"]["max_sources"] == 3
    assert client.put("/api/settings", json={"bogus": 1}).status_code == 400
    assert client.put("/api/settings", json={"onion": {"enabled": True}}).status_code == 400      # needs explicit activation
    assert client.get("/api/models").json()["models"] == ["tiny-model"]


def test_backup_restore_api(client):
    rid = start(client); wait_done(client, rid)
    name = client.post("/api/backup").json()["name"]
    assert name in [b["name"] for b in client.get("/api/backups").json()["backups"]]
    client.request("DELETE", "/api/runs", json={"ids": [rid], "confirm": True})
    assert client.post("/api/restore", json={"name": name}).status_code == 400                    # needs confirm
    assert client.post("/api/restore", json={"name": "../../etc/passwd", "confirm": True}).status_code == 400
    assert client.post("/api/restore", json={"name": name, "confirm": True}).status_code == 200
    assert client.get(f"/api/runs/{rid}").status_code == 200


def test_server_errors_do_not_leak_internals(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret path /home/x/y/z.py sk-abcdef0123456789")
    monkeypatch.setattr("atlas.server.report.report_data", boom)
    rid = start(client); wait_done(client, rid)
    r = client.get(f"/api/runs/{rid}")
    assert r.status_code == 500 and "sk-abcdef" not in r.text and "/home/x" not in r.text and "Traceback" not in r.text


# ------------------------------------------------------------ exports
def test_html_export_escapes_untrusted_content(engine, db):
    rid = run_req(engine, mode="light", provider="local")
    engine.execute(rid)
    sid = db.q("SELECT source_id FROM run_sources WHERE run_id=? LIMIT 1", (rid,))[0]["source_id"]
    db.ex("UPDATE sources SET title=? WHERE id=?", ("<script>alert(1)</script><img src=x onerror=alert(2)>", sid))
    report.rebuild(db, rid)
    html = report.export_html(db, rid)
    assert "<script>" not in html and "<img" not in html and "&lt;script&gt;" in html


def test_csv_formula_injection_neutralised(engine, db):
    rid = run_req(engine, mode="light", provider="local"); engine.execute(rid)
    sid = db.q("SELECT source_id FROM run_sources WHERE run_id=? LIMIT 1", (rid,))[0]["source_id"]
    db.ex("UPDATE sources SET title='=HYPERLINK(\"http://evil\",\"x\")' WHERE id=?", (sid,))
    rows = list(csv.reader(io.StringIO(report.sources_csv(db, rid))))
    assert all(not r[1].startswith("=") for r in rows[1:])


def test_export_contains_metadata_and_real_references(engine, db):
    rid = run_req(engine, mode="medium", provider="local"); engine.execute(rid)
    d = report.report_data(db, rid)
    md = d["report_markdown"]
    assert "**Mode:** medium" in md and "Methodology" in md and "retrieved:" in md
    for s in d["sources"]:
        if s["label"].startswith("S"):
            assert s["url"] in md and s["url"].startswith(("https://", "http://"))


# ------------------------------------------------------------ onion
ONION = "abcdefghij234567abcdefghij234567abcdefghij234567abcdefghi.onion"


def onion_conn(handler, **cfg):
    save_settings({"onion": {"enabled": True, "allowlist": [ONION], **cfg}})
    c = httpx.Client(transport=httpx.MockTransport(handler))
    return OnionConnector(load_settings(), client=c)


def test_onion_disabled_by_default(data_dir):
    s = load_settings()
    assert s["onion"]["enabled"] is False and s["onion"]["allowlist"] == []
    with pytest.raises(OnionError):
        OnionConnector(s).fetch(f"http://{ONION}/")


def test_clearnet_fetcher_refuses_onion(world):
    from atlas.security import validate_url, UnsafeURL
    with pytest.raises(UnsafeURL):
        validate_url(f"http://{ONION}/", resolver=fake_resolver)


def test_onion_allowlist_enforced_and_text_only(data_dir):
    ok_html = "<html><title>Hidden</title><article><p>This is a public onion service describing censorship circumvention tools in detail.</p></article></html>"
    conn = onion_conn(lambda r: httpx.Response(200, text=ok_html, headers={"content-type": "text/html"}))
    pg = conn.fetch(f"http://{ONION}/page")
    assert "censorship" in pg.text and "links" not in pg.meta
    with pytest.raises(OnionError):
        conn.fetch("http://zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz.onion/")        # not allowlisted
    for bad in ("http://example.com/", "http://127.0.0.1/", f"http://u:p@{ONION}/", f"http://{ONION}:22/", "file:///etc/passwd"):
        with pytest.raises(OnionError):
            conn.fetch(bad)


def test_onion_redirect_revalidated(data_dir):
    def h(req):
        return httpx.Response(302, headers={"location": "http://127.0.0.1:8765/api/settings"})
    conn = onion_conn(h)
    with pytest.raises(OnionError):
        conn.fetch(f"http://{ONION}/")
    conn2 = onion_conn(lambda r: httpx.Response(302, headers={"location": "https://example.com/"}))
    with pytest.raises(OnionError):
        conn2.fetch(f"http://{ONION}/")


def test_onion_limits(data_dir):
    conn = onion_conn(lambda r: httpx.Response(200, text="<p>" + "x" * 5000 + "</p>", headers={"content-type": "text/html"}), max_bytes=1000)
    with pytest.raises(OnionError, match="too large"):
        conn.fetch(f"http://{ONION}/")
    conn = onion_conn(lambda r: httpx.Response(200, content=b"MZ", headers={"content-type": "application/x-msdownload"}))
    with pytest.raises(OnionError, match="content type"):
        conn.fetch(f"http://{ONION}/")


def test_onion_kill_switch(data_dir):
    conn = onion_conn(lambda r: httpx.Response(200, text="<article><p>content that is long enough to extract properly here.</p></article>", headers={"content-type": "text/html"}))
    conn.kill()
    assert load_settings()["onion"]["enabled"] is False
    with pytest.raises(OnionError):
        conn.fetch(f"http://{ONION}/")


def test_onion_proxy_must_be_local(data_dir):
    from atlas.onion import _loopback_proxy
    assert _loopback_proxy("socks5h://127.0.0.1:9050") == ("127.0.0.1", 9050)
    for bad in ("socks5h://8.8.8.8:9050", "http://127.0.0.1:9050", "socks5h://evil.com:1"):
        with pytest.raises(OnionError):
            _loopback_proxy(bad)


def test_onion_research_isolated_from_web_runs(data_dir, db):
    html = "<html><title>Hidden svc</title><article><p>The service documents censorship circumvention research for journalists in public.</p></article></html>"
    conn = onion_conn(lambda r: httpx.Response(200, text=html, headers={"content-type": "text/html"}))
    out = run_onion_research(db, conn, "censorship circumvention research", [f"http://{ONION}/a"])
    assert out["status"] == "completed" and out["retrieved"] == 1
    run = db.one("SELECT * FROM runs WHERE id=?", (out["run_id"],))
    assert run["channel"] == "onion" and "unverified" in run["report_md"].lower()
    assert db.one("SELECT origin FROM sources")["origin"] == "onion"


def test_onion_api_needs_confirmation_and_allowlist(client):
    assert client.post("/api/onion/enable", json={}).status_code == 400
    assert client.post("/api/onion/enable", json={"confirm": True}).status_code == 400            # no allowlist
    assert client.post("/api/onion/enable", json={"confirm": True, "allowlist": ["not-onion.com"]}).status_code == 400
    s = client.post("/api/onion/enable", json={"confirm": True, "allowlist": [ONION]}).json()
    assert s["enabled"] is True and "tor" in json.dumps(s).lower()
    assert client.post("/api/onion/kill").json()["enabled"] is False
    assert client.get("/api/onion/status").json()["enabled"] is False
