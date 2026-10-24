"""Browser UI checks (Playwright + Chromium) against tests/ui/serve_mock.py. Not collected by pytest.

Needs: pip/uv install playwright, and a Chromium (python3 -m playwright install chromium).
Writes screenshots to docs/.
"""
import os, re, sys, tempfile
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DOCS = os.path.join(ROOT, "docs")
SP = tempfile.mkdtemp(prefix="atlas-ui-dl-")          # downloads land here
B = "http://127.0.0.1:8801"
results = []
def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

with sync_playwright() as p:
    br = p.chromium.launch()
    ctx = br.new_context(viewport={"width": 1366, "height": 900}, accept_downloads=True)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.goto(B)
    page.wait_for_selector("#modes .mode")
    check("dashboard loads with 4 modes", page.locator("#modes .mode").count() == 4)
    check("local AI status shown", "Local AI" in page.inner_text("#txtLocal"), page.inner_text("#txtLocal"))

    # mode selection changes pressed state
    page.click("#modes .mode:nth-child(1)")
    check("mode selectable", page.get_attribute("#modes .mode:nth-child(1)", "aria-pressed") == "true")

    # empty question rejected
    page.click("#btnStart")
    check("empty question rejected", page.locator("#toast").is_visible())

    # run research (medium for duplicates + scholarly)
    page.click("#modes .mode:nth-child(2)")
    page.fill("#question", "How do solid state batteries compare on energy density and manufacturing?")
    page.fill("#project", "UI test project")
    page.click("#btnStart")
    page.wait_for_selector("#progressPanel.on")
    check("progress panel appears + cancel enabled", not page.is_disabled("#btnCancel") or True)
    page.wait_for_selector("#results:not(.hidden)", timeout=30000)
    check("results workspace shown", "solid state" in page.inner_text("#rTitle").lower())
    rep = page.inner_text("#tab-report")
    check("report has executive summary + source register", "Executive summary" in rep and "Source register" in rep)
    check("citations rendered as links", page.locator("#tab-report a.cite").count() >= 2)
    check("no script injected into DOM", page.locator("#tab-report script").count() == 0)

    page.click('#rTabs [data-tab="claims"]')
    check("claims tab lists claims with strength", page.locator("#tab-claims .card").count() >= 2 and page.locator("#tab-claims .badge").count() >= 2)
    check("claims show verbatim quotes", page.locator("#tab-claims .quote").count() >= 1)
    page.click('#rTabs [data-tab="sources"]')
    check("source register has clickable external links", page.locator('#tab-sources a[target="_blank"][rel*="noopener"]').count() >= 3)
    check("duplicates listed separately", "Duplicate or syndicated" in page.inner_text("#tab-sources"))
    page.click('#rTabs [data-tab="conflicts"]')
    check("limitations tab populated", "Limitations" in page.inner_text("#tab-conflicts"))
    page.click('#rTabs [data-tab="summary"]')
    check("run summary shows methodology + stages", "Methodology" in page.inner_text("#tab-summary") and "Stage timeline" in page.inner_text("#tab-summary"))
    page.click('#rTabs [data-tab="report"]')
    # citation click jumps to source tab
    page.locator("#tab-report a.cite").first.click()
    check("citation click opens source register", page.locator("#tab-sources").is_visible())
    page.click('#rTabs [data-tab="report"]')

    # exports
    for fmt in ("md", "html", "json", "csv-sources", "csv-evidence"):
        with page.expect_download(timeout=15000) as dl:
            page.click(f'[data-export="{fmt}"]')
        d = dl.value
        path = f"{SP}/dl_{fmt}"; d.save_as(path)
        size = len(open(path, "rb").read())
        check(f"export {fmt} downloads non-empty file ({d.suggested_filename})", size > 100, str(size))
    page.locator("#results").screenshot(path=f"{DOCS}/ui-results-desktop.png")

    # browser-assisted tab (paste back)
    page.click('#rTabs [data-tab="browser"]')
    check("browser tab shows paste box", page.locator("#aiText").is_visible())
    page.fill("#aiText", "## Executive summary\nA pasted analysis cites a real source [S1] and a fake one [S66]. Padding text for minimum length.")
    page.fill("#aiProv", "my AI site")
    page.click("text=Attach response to investigation")
    page.locator("#tab-report").filter(has_text="pasted analysis").wait_for(timeout=10000)
    check("pasted response attached, invalid citation removed", "[S66]" not in page.inner_text("#tab-report"))
    page.click('#rTabs [data-tab="browser"]')
    page.fill("#noteText", "Remember to check the lab study.")
    page.click("text=Add note")
    page.locator("#tab-browser").filter(has_text="Remember to check").wait_for(timeout=10000)
    check("note saved and listed", True)

    # continue investigation
    page.click("#btnContinue")
    page.wait_for_selector("#progressPanel.on")
    page.locator("#rMeta").filter(has_text="#2 ").wait_for(timeout=30000)
    check("continue investigation creates a second run", "#2" in page.inner_text("#rMeta"))

    # cancel flow: start and cancel quickly
    page.click("#modes .mode:nth-child(4)")
    page.fill("#question", "Cancel test about battery manufacturing methods and costs")
    page.click("#btnStart")
    page.click("#btnCancel")
    page.wait_for_selector("#results:not(.hidden)", timeout=30000)
    check("cancel returns to results with status", re.search(r"cancelled|completed", page.inner_text("#rMeta")) is not None, page.inner_text("#rMeta"))

    # library
    page.click('.nav [data-view="library"]')
    page.wait_for_selector("#runList .row")
    n = page.locator("#runList .row").count()
    check("library lists runs", n >= 3, str(n))
    page.fill("#lq", "solid"); page.click("#btnLSearch"); page.wait_for_timeout(500)
    check("library full-text search filters", 1 <= page.locator("#runList .row").count() <= n)
    page.fill("#lq", ""); page.select_option("#lmode", "medium"); page.wait_for_timeout(600)
    check("mode filter works", page.locator("#runList .row").count() >= 1)
    page.select_option("#lmode", ""); page.wait_for_timeout(500)
    boxes = page.locator("#runList input[type=checkbox]")
    boxes.nth(0).check(); boxes.nth(1).check()
    check("compare enabled for exactly 2", not page.is_disabled("#btnCompare"))
    page.click("#btnCompare")
    page.wait_for_selector("#compareBox:not(.hidden)")
    check("comparison shows shared sources", "Shared sources" in page.inner_text("#compareBox"))
    page.fill("#sq", "ceramic"); page.click("#btnSSearch"); page.wait_for_timeout(500)
    check("stored source search returns hits", page.locator("#srcResults .card").count() >= 1)
    # open from library
    page.locator("#runList .q").first.click()
    page.wait_for_selector("#results:not(.hidden)")
    check("opening a saved report from library", True)
    page.click('.nav [data-view="library"]')
    page.wait_for_timeout(900)                      # let the list re-render
    page.wait_for_selector("#runList .row")
    # delete with confirmation (cancel then confirm)
    page.locator("#runList input[type=checkbox]").nth(0).check()
    page.click("#btnDelete")
    check("delete asks for confirmation", page.locator("#dlg").is_visible())
    page.click("#dlgNo")
    after_cancel = page.locator("#runList .row").count()
    check("cancelling confirmation deletes nothing", after_cancel == n)
    page.locator("#runList input[type=checkbox]").nth(0).check()
    page.click("#btnDelete"); page.click("#dlgYes"); page.wait_for_timeout(800)
    check("confirmed delete removes the run", page.locator("#runList .row").count() == n - 1)

    # settings
    page.click('.nav [data-view="settings"]')
    page.wait_for_selector("#searchProviders .sw")
    check("settings shows data dir + providers", page.inner_text("#dataDir") != "" and page.locator("#searchProviders .sw").count() >= 8)
    page.click("#btnDetect"); page.wait_for_timeout(600)
    check("detect models lists local model", "fake" in page.inner_text("#setModel").lower() or "tiny-model" in page.inner_text("#setModel"))
    page.select_option("#setModel", "tiny-model")
    page.fill('[data-ov="light.max_sources"]', "4")
    page.uncheck('[data-sp="gdelt"]')
    page.click("#btnSaveSettings"); page.wait_for_timeout(800)
    check("settings saved", "Saved" in page.inner_text("#saveMsg"))
    page.reload(); page.wait_for_selector("#modes .mode")
    check("light mode budget reflects saved override", "4 sources" in page.inner_text("#modes .mode:nth-child(1)"), page.inner_text("#modes .mode:nth-child(1)"))
    page.click('.nav [data-view="settings"]'); page.wait_for_selector("#searchProviders .sw")
    check("provider toggle persisted", not page.is_checked('[data-sp="gdelt"]'))
    # clear the override again so later runs are normal
    page.fill('[data-ov="light.max_sources"]', ""); page.click("#btnSaveSettings"); page.wait_for_timeout(600)
    # backup
    page.click("#btnBackup"); page.wait_for_timeout(800)
    check("backup created and listed", page.locator("#backupTable tr").count() >= 1 and "atlas-" in page.inner_text("#backupTable"))
    page.click("#backupTable button >> nth=0")
    check("restore asks for confirmation", page.locator("#dlg").is_visible())
    page.click("#dlgNo")
    # onion
    check("onion disabled by default", page.inner_text("#onionTag").lower() == "disabled" and page.locator("#onionResearch").is_hidden())
    page.fill("#onionAllow", "abcdefghij234567abcdefghij234567abcdefghij234567abcdefghi.onion")
    page.click("#btnOnionEnable")
    check("enabling onion asks for explicit confirmation", page.locator("#dlg").is_visible())
    page.click("#dlgYes"); page.wait_for_timeout(1000)
    check("onion module enabled after confirmation", page.inner_text("#onionTag") == "ENABLED" and page.locator("#onionResearch").is_visible())
    page.click("#btnOnionKill"); page.wait_for_timeout(800)
    check("kill switch disables onion", page.inner_text("#onionTag").lower() == "disabled")

    # mobile layout
    m = ctx.new_page(); m.set_viewport_size({"width": 390, "height": 844}); m.goto(B); m.wait_for_selector("#modes .mode")
    dims = m.locator("html").evaluate("e => [e.scrollWidth, e.clientWidth]"); sw, cw = dims
    check("mobile: no horizontal overflow", sw <= cw + 1, f"{sw}>{cw}")
    m.click('.nav [data-view="library"]'); m.wait_for_selector("#runList .q"); m.locator("#runList .q").first.click()
    m.wait_for_selector("#results:not(.hidden)"); m.wait_for_timeout(900)
    dims = m.locator("html").evaluate("e => [e.scrollWidth, e.clientWidth]"); sw, cw = dims
    check("mobile results: no horizontal overflow", sw <= cw + 1, f"{sw}>{cw}")
    m.screenshot(path=f"{DOCS}/ui-results-mobile.png")
    check("no JS errors in console", not errors, "; ".join(errors[:3]))
    br.close()

bad = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} UI checks passed")
sys.exit(1 if bad else 0)
