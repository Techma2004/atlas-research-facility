"use strict";
/* ATLAS dashboard. All untrusted text is inserted with textContent (never innerHTML). */
const $ = (id) => document.getElementById(id);
const state = { status: null, mode: "medium", runId: null, poll: null, settings: null, lib: [], tab: "report", detail: null, running: false };

/* ---------- DOM helpers ---------- */
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return el;
}
const clear = (el) => { while (el.firstChild) el.removeChild(el.firstChild); return el; };
const safeUrl = (u) => (/^https?:\/\//i.test(u || "") ? u : null);
function link(url, text) {
  const ok = safeUrl(url);
  return ok ? h("a", { href: ok, target: "_blank", rel: "noopener noreferrer", text }) : h("span", { text });
}

let toastTimer;
function toast(msg, err) {
  const t = $("toast");
  t.textContent = msg; t.className = "toast" + (err ? " err" : "");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.add("hidden"), err ? 7000 : 3500);
}
async function api(method, path, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  let r;
  try { r = await fetch(path, opt); } catch (e) { throw new Error("Cannot reach the ATLAS server. Is it still running?"); }
  let data = null;
  try { data = await r.json(); } catch (e) { /* non-JSON */ }
  if (!r.ok) throw new Error((data && (data.detail || data.message)) || `Request failed (${r.status})`);
  return data;
}
const guard = (fn) => async (...a) => { try { return await fn(...a); } catch (e) { toast(e.message, true); } };

function confirmDialog(title, body, yes = "Confirm") {
  return new Promise((resolve) => {
    $("dlgTitle").textContent = title; $("dlgBody").textContent = body; $("dlgYes").textContent = yes;
    const d = $("dlg"); const done = (v) => { d.close(); $("dlgYes").onclick = $("dlgNo").onclick = null; resolve(v); };
    $("dlgYes").onclick = () => done(true); $("dlgNo").onclick = () => done(false); d.oncancel = () => done(false);
    d.showModal();
  });
}
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch (e) {
    const ta = h("textarea", { class: "offscreen" }); ta.value = text; document.body.append(ta); ta.select();
    let ok = false; try { ok = document.execCommand("copy"); } catch (_) { /* ignore */ } ta.remove(); return ok;
  }
}

/* ---------- safe Markdown -> DOM ---------- */
function inline(text, labels) {
  const frag = document.createDocumentFragment();
  const re = /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)|\*\*([^*]+)\*\*|`([^`]+)`|\[(S\d{1,3})\]|(?<![\w*])_([^_]+)_(?![\w*])/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) frag.append(text.slice(last, m.index));
    if (m[1] !== undefined) frag.append(link(m[2], m[1]));
    else if (m[3] !== undefined) frag.append(h("strong", { text: m[3] }));
    else if (m[4] !== undefined) frag.append(h("code", { text: m[4] }));
    else if (m[5] !== undefined) {
      if (labels.has(m[5])) frag.append(h("a", { class: "cite", href: "#", "data-cite": m[5], text: `[${m[5]}]` }));
      else frag.append(`[${m[5]}]`);
    } else if (m[6] !== undefined) frag.append(h("em", { text: m[6] }));
    last = re.lastIndex;
  }
  if (last < text.length) frag.append(text.slice(last));
  return frag;
}
function renderMd(md, labels) {
  const root = document.createDocumentFragment();
  let para = [], ul = null, tbl = null;
  const flush = () => { if (para.length) { root.append(h("p", {}, inline(para.join(" "), labels))); para = []; } };
  const close = () => { ul = null; tbl = null; };
  for (const line of md.split("\n")) {
    let m;
    if ((m = line.match(/^(#{1,4})\s+(.*)/))) { flush(); close(); root.append(h("h" + m[1].length, {}, inline(m[2], labels))); }
    else if (/^\s*[-*]\s+/.test(line)) { flush(); tbl = null; if (!ul) { ul = h("ul"); root.append(ul); } ul.append(h("li", {}, inline(line.replace(/^\s*[-*]\s+/, ""), labels))); }
    else if (line.startsWith("|")) {
      flush(); ul = null;
      if (/^\|[\s\-|:]+\|?$/.test(line)) continue;
      const cells = line.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      if (!tbl) { tbl = h("table", {}, h("thead", {}, h("tr", {}, cells.map((c) => h("th", {}, inline(c, labels))))), h("tbody")); root.append(tbl); }
      else tbl.querySelector("tbody").append(h("tr", {}, cells.map((c) => h("td", {}, inline(c, labels)))));
    }
    else if (!line.trim()) { flush(); close(); }
    else { close(); para.push(line.trim()); }
  }
  flush();
  return root;
}

/* ---------- navigation ---------- */
function showView(v) {
  for (const s of ["desk", "library", "settings"]) $("view-" + s).classList.toggle("hidden", s !== v);
  document.querySelectorAll(".nav button").forEach((b) => b.classList.toggle("on", b.dataset.view === v));
  if (v === "library") loadLibrary();
  if (v === "settings") loadSettings();
}
document.querySelectorAll(".nav button").forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));

/* ---------- status ---------- */
async function refreshStatus() {
  try {
    const s = await api("GET", "/api/status");
    state.status = s;
    const loc = s.providers.find((p) => p.id === "local");
    $("dotLocal").className = "dot " + (loc.available ? "ok" : "bad");
    $("txtLocal").textContent = loc.available ? `Local AI: ${loc.model || "ready"}` : "Local AI offline";
    const sp = s.search_providers.filter((p) => p.enabled && p.configured).length;
    $("dotSearch").className = "dot " + (sp ? "ok" : "bad"); $("txtSearch").textContent = `${sp} search provider(s) on`;
    $("txtMem").textContent = s.memory_available_mb != null ? `Memory free: ${s.memory_available_mb} MB` : "Memory: n/a";
    $("dotOnion").className = "dot " + (s.onion.enabled ? "ok" : ""); $("txtOnion").textContent = "Onion module: " + (s.onion.enabled ? "ON" : "off");
    $("txtVer").textContent = `ATLAS ${s.version} · schema v${s.schema_version}`;
    renderModes();
    const prov = $("provider").value;
    $("providerHint").textContent = prov === "local" || prov === "hybrid"
      ? (loc.available ? `Local model ready: ${loc.model}` : "No local model detected - hybrid falls back to browser-assisted.") : "";
  } catch (e) { $("txtLocal").textContent = "Server unreachable"; }
}
function renderModes() {
  const box = clear($("modes"));
  for (const [key, m] of Object.entries(state.status.modes)) {
    const b = h("button", { class: "mode", type: "button", "aria-pressed": String(state.mode === key), "data-mode": key },
      h("b", { text: m.label }),
      h("small", { text: `${m.min_sources === m.max_sources ? "" : m.min_sources + "–"}${m.max_sources} sources · ${m.max_rounds} round${m.max_rounds > 1 ? "s" : ""}` }),
      h("small", { text: `${m.queries_per_round} ${m.queries_per_round === 1 ? "query" : "queries"}/round · ${Math.round(m.time_budget_s / 60 * 10) / 10 || "<1"} min${m.follow_citations ? " · follows refs" : ""}${m.cross_check ? " · cross-check" : ""}` }));
    b.addEventListener("click", () => { state.mode = key; renderModes(); });
    box.append(b);
  }
}
$("provider").addEventListener("change", refreshStatus);

/* ---------- start / progress ---------- */
const splitList = (s) => s.split(/[,\s]+/).map((x) => x.trim()).filter(Boolean);
async function startRun(extra) {
  const q = $("question").value.trim();
  if (q.length < 5) { $("question").focus(); toast("Enter a research question of at least 5 characters.", true); return; }
  const body = { question: q, mode: state.mode, provider: $("provider").value, sensitive: $("sensitive").checked,
    project: $("project").value.trim() || null,
    filters: { include_domains: splitList($("incDom").value), exclude_domains: splitList($("excDom").value),
      date_from: $("dFrom").value || null, date_to: $("dTo").value || null }, budget: {} };
  if ($("bSrc").value) body.budget.max_sources = parseInt($("bSrc").value, 10);
  if ($("bTime").value) body.budget.time_budget_s = parseInt($("bTime").value, 10);
  const r = await api("POST", "/api/research", { ...body, ...(extra || {}) });
  beginPolling(r.id);
}
function beginPolling(id) {
  state.runId = id; state.running = true;
  $("btnStart").disabled = true; $("btnCancel").disabled = false;
  $("progressPanel").classList.add("on"); $("results").classList.add("hidden");
  clearInterval(state.poll); state.poll = setInterval(guard(() => tick(id)), 1000); tick(id);
}
async function tick(id) {
  const p = await api("GET", `/api/research/${id}/progress`);
  $("pStage").textContent = p.message || p.stage;
  $("pProv").textContent = p.provider ? "provider: " + p.provider : "";
  $("pBar").style.width = p.percent + "%";
  $("sSources").textContent = p.counts.sources || 0; $("sFound").textContent = p.counts.found || 0;
  $("sFetched").textContent = p.counts.fetched || 0; $("sQueries").textContent = p.counts.queries || 0;
  $("sTime").textContent = Math.round(p.elapsed_s) + "s";
  const log = clear($("pLog")); for (const e of p.events.slice(-25)) log.append(h("div", { text: `[${e.t}s] ${e.stage}: ${e.message}` })); log.scrollTop = log.scrollHeight;
  if (["completed", "failed", "cancelled", "interrupted"].includes(p.status)) {
    clearInterval(state.poll); state.running = false;
    $("btnStart").disabled = false; $("btnCancel").disabled = true;
    await loadRun(id); toast(`Run ${p.status}.`, p.status === "failed");
  }
}
$("btnStart").addEventListener("click", guard(() => startRun()));
$("btnCancel").addEventListener("click", guard(async () => { await api("POST", `/api/research/${state.runId}/cancel`); toast("Cancellation requested…"); }));

/* ---------- results ---------- */
const TABS = ["report", "claims", "sources", "conflicts", "browser", "summary"];
function setTab(t) {
  state.tab = t;
  document.querySelectorAll("#rTabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === t));
  for (const x of TABS) $("tab-" + x).classList.toggle("hidden", x !== t);
}
document.querySelectorAll("#rTabs button").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));

async function loadRun(id) {
  showView("desk");
  const d = await api("GET", `/api/runs/${id}`);
  state.detail = d; state.runId = id;
  $("results").classList.remove("hidden");
  $("rTitle").textContent = d.question;
  const srcN = d.sources.filter((s) => s.label.startsWith("S")).length;
  clear($("rMeta")).append(`#${d.id} · ${d.mode} · ${d.status} · provider ${d.provider || "none"} · ${srcN} sources · ${d.created_at}`);
  const labels = new Set(d.sources.map((s) => s.label));
  const rep = clear($("tab-report")); rep.append(renderMd(d.report_markdown || "", labels));
  rep.querySelectorAll("a.cite").forEach((a) => a.addEventListener("click", (ev) => { ev.preventDefault(); setTab("sources"); const t = document.getElementById("src-" + a.dataset.cite); if (t) t.scrollIntoView({ block: "center" }); }));
  renderClaims(d); renderSources(d); renderConflicts(d); renderBrowser(d); renderSummary(d);
  setTab(state.tab);
  $("results").scrollIntoView({ behavior: "smooth", block: "start" });
}
function badge(t) { return h("span", { class: "badge " + t, text: t }); }
function renderClaims(d) {
  const el = clear($("tab-claims"));
  if (!d.claims.length) { el.append(h("div", { class: "empty", text: "No cited claims yet. Claims are derived from sentences that carry valid [S#] citations." })); return; }
  el.append(h("p", { class: "hint", text: "Strength counts independent sources (syndicated copies count once) and source type. Quotes are verbatim passages from stored sources." }));
  d.claims.forEach((c, i) => {
    const card = h("div", { class: "card" }, h("div", { class: "t" }, h("b", { text: `${i + 1}.` }), badge(c.strength), h("span", { class: "meta", text: `${c.independent_count} independent source(s)` })),
      h("p", { text: c.text }), h("div", { class: "hint", text: c.uncertainty }));
    for (const e of c.evidence) {
      card.append(h("div", { class: "quote" }, h("b", { text: `${e.stance === "support" ? "Supports" : "Possible conflict"} · ${e.label || "source"}: ` }),
        e.quote ? `“${e.quote}”` : "(no matching passage)", " ", link(e.url, "↗")));
    }
    el.append(card);
  });
}
function renderSources(d) {
  const el = clear($("tab-sources"));
  const main = d.sources.filter((s) => s.label.startsWith("S")), dups = d.sources.filter((s) => s.label.startsWith("D"));
  if (!main.length) el.append(h("div", { class: "empty", text: "No sources were retrieved. See Conflicts & limitations for the reason." }));
  for (const s of main) {
    el.append(h("div", { class: "card", id: "src-" + s.label }, h("div", { class: "t" }, h("b", { text: s.label }), link(s.url, s.title || s.url), badge(s.source_type)),
      h("div", { class: "meta", text: `${s.domain} · published ${s.published || "unknown"} · retrieved ${s.retrieved_at} · quality ${Number(s.quality).toFixed(2)} · found by ${s.found_by}` }),
      s.excerpt ? h("div", { class: "quote", text: s.excerpt.slice(0, 320) }) : null));
  }
  if (dups.length) { el.append(h("h3", { text: "Duplicate or syndicated copies (not independent confirmation)" })); for (const s of dups) el.append(h("div", { class: "meta" }, `${s.label} · `, link(s.url, s.title || s.url), ` · ${s.domain}`)); }
}
function renderConflicts(d) {
  const el = clear($("tab-conflicts"));
  el.append(h("h3", { text: "Conflicting findings" }));
  const conf = d.claims.flatMap((c) => c.evidence.filter((e) => e.stance === "contradict").map((e) => ({ c, e })));
  if (!conf.length) el.append(h("p", { class: "hint", text: "No contradictions detected by the automated check. This does not prove none exist." }));
  for (const { c, e } of conf) el.append(h("div", { class: "card" }, h("p", { text: c.text }), h("div", { class: "quote", text: `${e.label || "source"}: “${e.quote}”` }), h("div", { class: "hint warn", text: "Lexical heuristic - verify manually." })));
  el.append(h("h3", { text: "Limitations and unanswered questions" }));
  const ul = h("ul"); (d.limitations.length ? d.limitations : ["None recorded."]).forEach((x) => ul.append(h("li", { text: x }))); el.append(ul);
  if (d.error) el.append(h("p", { class: "hint warn", text: "Error: " + d.error }));
}
function renderSummary(d) {
  const el = clear($("tab-summary")); const s = d.stats || {}; const c = s.counts || {};
  el.append(h("div", { class: "stats" },
    ...[["queries", (s.queries || []).length], ["candidates", c.found || 0], ["pages read", c.fetched || 0], ["failed", c.failed || 0], ["duplicates", c.duplicates || 0], ["sources", c.sources || 0], ["rounds", s.rounds || 0], ["seconds", s.duration_s || 0]]
      .map(([k, v]) => h("div", { class: "stat" }, h("b", { text: String(v) }), h("span", { text: k })))));
  el.append(h("p", { text: `Stop reason: ${s.stop_reason || "-"}` }));
  el.append(h("h3", { text: "Methodology" })); d.methodology.split("\n\n").forEach((p) => el.append(h("p", { text: p })));
  el.append(h("h3", { text: "Stage timeline (provider per stage)" }));
  const t = h("table", { class: "t" }, h("tr", {}, h("th", { text: "t (s)" }), h("th", { text: "Stage" }), h("th", { text: "Provider" }), h("th", { text: "Note" })));
  (d.stages || []).forEach((x) => t.append(h("tr", {}, h("td", { text: x.t }), h("td", { text: x.stage }), h("td", { text: x.provider }), h("td", { text: x.note }))));
  el.append(t);
  const errs = Object.entries(s.provider_errors || {});
  if (errs.length) { el.append(h("h3", { text: "Provider problems" })); errs.forEach(([k, v]) => el.append(h("p", { class: "hint warn", text: `${k}: ${v}` }))); }
  if (s.cross_check) el.append(h("p", { class: "hint", text: `Cross-check: ${JSON.stringify(s.cross_check)}` }));
}
function renderBrowser(d) {
  const el = clear($("tab-browser"));
  el.append(h("h3", { text: "Browser-assisted analysis (no API key)" }),
    h("ol", { class: "hint" }, h("li", { text: "Copy the prompt below (or download it)." }), h("li", { text: "Open your preferred AI website and sign in normally." }),
      h("li", { text: "Submit the prompt through the site's ordinary interface." }), h("li", { text: "Paste the AI's answer here. ATLAS validates every [S#] citation against the stored sources." })),
    h("p", { class: "hint warn", text: "ATLAS never reads or automates your AI account. Consumer chat subscriptions and API billing are separate. Don't paste prompts from sensitive runs into services you don't trust." }));
  if (d.browser_prompt) {
    el.append(h("div", { class: "prompt", text: d.browser_prompt.slice(0, 4000) + (d.browser_prompt.length > 4000 ? "\n… (truncated in preview; copy gets the full prompt)" : "") }),
      h("div", { class: "bar" }, h("button", { class: "b pri", onclick: guard(async () => { toast((await copyText(d.browser_prompt)) ? "Prompt copied." : "Copy failed - use Download.", false); }), text: "Copy prompt" }),
        h("a", { class: "b btnlink", href: `/api/runs/${d.id}/prompt?download=true`, text: "Download prompt (.txt)" })));
  } else el.append(h("p", { class: "hint", text: "No prompt is stored for this run (no sources were retrieved)." }));
  el.append(h("label", { class: "f", for: "aiProv", text: "Which AI site did you use? (recorded with the answer)" }), h("input", { type: "text", id: "aiProv", maxlength: "60", placeholder: "e.g. my chat assistant" }),
    h("label", { class: "f", for: "aiText", text: "Paste the AI's response" }), h("textarea", { id: "aiText", class: "mh140" }),
    h("div", { class: "bar" }, h("button", { class: "b pri", onclick: guard(async () => {
      const text = $("aiText").value.trim(); if (!text) { toast("Paste the AI's response first.", true); return; }
      const r = await api("POST", `/api/runs/${d.id}/response`, { text, provider_label: $("aiProv").value.trim() || "browser-assisted" });
      toast(`Attached. ${r.citations_valid} valid citation(s)` + (r.invalid_citations.length ? `; removed invalid: ${r.invalid_citations.join(", ")}` : "."));
      state.tab = "report";                       // show the merged, citation-checked report
      await loadRun(d.id);
    }), text: "Attach response to investigation" })));
  el.append(h("h3", { text: "Research notes" }), h("textarea", { id: "noteText", placeholder: "Add a note. Notes are trusted context for future analysis of this run.", class: "mh70" }),
    h("div", { class: "bar" }, h("button", { class: "b", text: "Add note", onclick: guard(async () => { const t = $("noteText").value.trim(); if (!t) return; await api("POST", `/api/runs/${d.id}/notes`, { text: t }); toast("Note saved."); await loadRun(d.id); }) })));
  for (const n of d.notes) el.append(h("div", { class: "card" }, h("div", { class: "t" }, badge(n.kind === "ai_response" ? "completed" : "reference"), h("span", { class: "meta", text: `${n.kind} · ${n.provider || "-"} · ${n.created_at}` })), h("p", { text: n.body.slice(0, 1500) })));
}
document.querySelectorAll("[data-export]").forEach((b) => b.addEventListener("click", () => { if (state.runId) window.location.href = `/api/runs/${state.runId}/export?format=${b.dataset.export}`; }));
$("btnCopyMd").addEventListener("click", guard(async () => { toast((await copyText(state.detail?.report_markdown || "")) ? "Markdown copied." : "Copy failed.", false); }));
$("btnContinue").addEventListener("click", guard(async () => {
  const r = await api("POST", `/api/runs/${state.runId}/continue`, { mode: state.mode, provider: $("provider").value });
  toast("Continuing: stored sources carried over; gathering new evidence."); beginPolling(r.id);
}));

/* ---------- library ---------- */
async function loadLibrary() {
  const p = new URLSearchParams();
  for (const [id, k] of [["lq", "q"], ["lmode", "mode"], ["lstatus", "status"], ["lfrom", "date_from"], ["lto", "date_to"], ["lproject", "project"], ["lchan", "channel"]]) if ($(id).value) p.set(k, $(id).value);
  const projs = await api("GET", "/api/projects"); const cur = $("lproject").value; const sel = clear($("lproject")); sel.append(h("option", { value: "", text: "All" }));
  projs.projects.forEach((x) => sel.append(h("option", { value: x.name, text: `${x.name} (${x.runs})` }))); sel.value = cur;
  const r = await api("GET", "/api/runs?" + p);
  state.lib = r.runs; $("lcount").textContent = `${r.total} investigation(s)`;
  const list = clear($("runList"));
  if (!r.runs.length) list.append(h("div", { class: "empty", text: "No investigations match. Run one from the Research desk." }));
  for (const x of r.runs) {
    list.append(h("div", { class: "row" }, h("input", { type: "checkbox", "data-id": x.id, "aria-label": "Select run " + x.id, onchange: syncSel }),
      h("div", {}, h("button", { class: "q", text: x.question, onclick: guard(() => loadRun(x.id)) }),
        h("div", { class: "meta", text: `#${x.id} · ${x.mode} · ${x.sources} sources · ${x.provider || "no AI"} · ${x.created_at}${x.project ? " · project " + x.project : ""}${x.channel === "onion" ? " · onion" : ""}${x.parent_run_id ? " · continues #" + x.parent_run_id : ""}` })),
      badge(x.status)));
  }
  syncSel();
}
const selected = () => [...document.querySelectorAll("#runList input:checked")].map((i) => parseInt(i.dataset.id, 10));
function syncSel() { const n = selected().length; $("btnCompare").disabled = n !== 2; $("btnDelete").disabled = n < 1; }
$("btnLSearch").addEventListener("click", guard(loadLibrary));
["lmode", "lstatus", "lfrom", "lto", "lproject", "lchan"].forEach((i) => $(i).addEventListener("change", guard(loadLibrary)));
$("lq").addEventListener("keydown", (e) => { if (e.key === "Enter") guard(loadLibrary)(); });
$("btnDelete").addEventListener("click", guard(async () => {
  const ids = selected();
  if (!(await confirmDialog(`Delete ${ids.length} investigation(s)?`, "This permanently removes the selected investigations, their claims and any sources no other investigation uses. Export or back up first if unsure.", "Delete permanently"))) return;
  const r = await api("DELETE", "/api/runs", { ids, confirm: true }); toast(`Deleted ${r.deleted}.`); await loadLibrary();
}));
$("btnCompare").addEventListener("click", guard(async () => {
  const [a, b] = selected(); const c = await api("GET", `/api/runs/${a}/compare/${b}`); const box = clear($("compareBox")); box.classList.remove("hidden");
  const col = (r) => h("div", { class: "card" }, h("b", { text: `#${r.id} · ${r.mode}` }), h("p", { text: r.question }), h("div", { class: "meta", text: `${r.sources} sources · ${r.claims} claims · ${r.created_at}` }));
  const lst = (title, arr, fn) => [h("h3", { text: `${title} (${arr.length})` }), ...arr.slice(0, 25).map(fn)];
  box.append(h("div", { class: "ph" }, h("h2", { text: "Comparison" }), h("button", { class: "b sm", text: "Close", onclick: () => box.classList.add("hidden") })), h("div", { class: "grid2" }, col(c.a), col(c.b)),
    ...lst("Shared sources", c.shared_sources, (s) => h("div", { class: "meta" }, link(s.url, s.title || s.url))),
    ...lst(`Only in #${c.a.id}`, c.only_a, (s) => h("div", { class: "meta" }, link(s.url, s.title || s.url))),
    ...lst(`Only in #${c.b.id}`, c.only_b, (s) => h("div", { class: "meta" }, link(s.url, s.title || s.url))),
    ...lst(`Claims only in #${c.a.id}`, c.claims_only_a, (t) => h("div", { class: "hint", text: "• " + t })),
    ...lst(`Claims only in #${c.b.id}`, c.claims_only_b, (t) => h("div", { class: "hint", text: "• " + t })));
  box.scrollIntoView({ behavior: "smooth" });
}));
$("btnSSearch").addEventListener("click", guard(async () => {
  const q = $("sq").value.trim(); const el = clear($("srcResults")); if (!q) return;
  const r = await api("GET", "/api/library/search?kind=source&q=" + encodeURIComponent(q));
  if (!r.results.length) el.append(h("div", { class: "empty", text: "No stored sources match." }));
  r.results.forEach((x) => el.append(h("div", { class: "card" }, h("b", { text: x.title }), h("div", { class: "quote", text: x.snippet }))));
}));

/* ---------- settings ---------- */
const MODE_KEYS = [["max_sources", "Max sources"], ["time_budget_s", "Time budget (s)"], ["fetch_pages", "Page fetches"], ["max_rounds", "Rounds"], ["concurrency", "Concurrent requests"]];
async function loadSettings() {
  const [r, st] = await Promise.all([api("GET", "/api/settings"), api("GET", "/api/status")]);
  state.settings = r.settings; const s = r.settings;
  $("dataDir").textContent = st.data_dir; $("setBase").value = s.local_base_url; $("setDefault").value = s.default_provider;
  $("setExternal").checked = s.allow_external_providers; $("setOaiBase").value = s.openai_base_url; $("setOaiModel").value = s.openai_model; $("setSearx").value = s.searxng_url;
  $("keyStatus").textContent = Object.entries(r.secrets).map(([k, v]) => `${k}: ${v.configured ? "key configured " + v.hint : "no key in environment"}`).join("  ·  ");
  await detectModels(s.local_model, true);
  const sp = clear($("searchProviders"));
  for (const p of st.search_providers) sp.append(h("label", { class: "sw" }, h("span", {}, h("b", { text: p.name }), ` · ${p.category}`, p.note ? h("span", { class: "hint warn", text: "  " + p.note }) : null),
    h("input", { type: "checkbox", "data-sp": p.name, checked: !!s.search_providers[p.name] })));
  const lt = clear($("limitTable")); lt.append(h("tr", {}, h("th", { text: "Mode" }), ...MODE_KEYS.map(([, l]) => h("th", { text: l }))));
  for (const m of Object.keys(st.modes)) lt.append(h("tr", {}, h("td", { text: st.modes[m].label }), ...MODE_KEYS.map(([k]) => h("td", {}, h("input", { type: "number", min: "1", "data-ov": `${m}.${k}`, value: (s.mode_overrides[m] || {})[k] ?? "", placeholder: st.modes[m][k] ?? "default" })))));
  $("fMaxBytes").value = s.fetch.max_bytes; $("fTimeout").value = s.fetch.timeout_s; $("fRedirects").value = s.fetch.max_redirects; $("fRobots").checked = s.fetch.respect_robots;
  $("storageInfo").textContent = `Database and settings live in ${st.data_dir}. Schema v${st.schema_version}. Backups are consistent SQLite copies; restoring replaces current data (a .pre-restore copy is kept).`;
  await loadBackups(); renderOnion(st.onion, s);
}
async function detectModels(current, quiet) {
  const r = await api("GET", "/api/models"); const sel = clear($("setModel")); sel.append(h("option", { value: "", text: "Auto (first available)" }));
  r.models.forEach((m) => sel.append(h("option", { value: m, text: m }))); if (current && !r.models.includes(current)) sel.append(h("option", { value: current, text: current + " (not installed)" }));
  sel.value = current || ""; $("modelMsg").textContent = r.models.length ? `${r.models.length} model(s) found at ${r.base_url}.` : r.message;
  if (!quiet) toast(r.models.length ? "Models detected." : "No local model server answered.", !r.models.length);
}
$("btnDetect").addEventListener("click", guard(async () => {
  // test the URL currently typed in the box by saving it first
  await api("PUT", "/api/settings", { local_base_url: $("setBase").value.trim() }); await detectModels($("setModel").value, false); refreshStatus();
}));
$("btnSaveSettings").addEventListener("click", guard(async () => {
  const sp = {}; document.querySelectorAll("[data-sp]").forEach((c) => { sp[c.dataset.sp] = c.checked; });
  const ov = {}; document.querySelectorAll("[data-ov]").forEach((i) => { if (i.value) { const [m, k] = i.dataset.ov.split("."); (ov[m] = ov[m] || {})[k] = parseInt(i.value, 10); } });
  const prev = state.settings.mode_overrides || {}; // cleared fields revert to defaults
  const patch = { local_base_url: $("setBase").value.trim(), local_model: $("setModel").value, default_provider: $("setDefault").value,
    allow_external_providers: $("setExternal").checked, openai_base_url: $("setOaiBase").value.trim(), openai_model: $("setOaiModel").value.trim(),
    searxng_url: $("setSearx").value.trim(), search_providers: sp, mode_overrides: ov,
    fetch: { max_bytes: parseInt($("fMaxBytes").value, 10), timeout_s: parseInt($("fTimeout").value, 10), max_redirects: parseInt($("fRedirects").value, 10), respect_robots: $("fRobots").checked } };
  void prev; await api("PUT", "/api/settings", patch); $("saveMsg").textContent = "Saved."; toast("Settings saved."); await loadSettings(); refreshStatus();
}));
async function loadBackups() {
  const r = await api("GET", "/api/backups"); const t = clear($("backupTable"));
  if (!r.backups.length) { t.append(h("tr", {}, h("td", { class: "hint", text: "No backups yet." }))); return; }
  for (const b of r.backups) t.append(h("tr", {}, h("td", { text: b.name }), h("td", { text: (b.bytes / 1024).toFixed(0) + " KB" }),
    h("td", {}, h("button", { class: "b sm danger", text: "Restore…", onclick: guard(async () => {
      if (!(await confirmDialog("Restore this backup?", `Current data will be replaced by ${b.name}. A .pre-restore copy of the current database is kept.`, "Restore"))) return;
      await api("POST", "/api/restore", { name: b.name, confirm: true }); toast("Backup restored."); refreshStatus(); }) }))));
}
$("btnBackup").addEventListener("click", guard(async () => { const r = await api("POST", "/api/backup"); toast("Backup created: " + r.name); await loadBackups(); }));

/* ---------- onion ---------- */
function renderOnion(o, s) {
  $("onionTag").textContent = o.enabled ? "ENABLED" : "disabled"; $("onionNote").textContent = o.note;
  if (document.activeElement !== $("onionAllow")) $("onionAllow").value = (s.onion.allowlist || []).join("\n");
  $("onionResearch").classList.toggle("hidden", !o.enabled); $("btnOnionKill").disabled = !o.enabled;
}
const allowLines = () => $("onionAllow").value.split(/\s+/).map((x) => x.trim().toLowerCase()).filter(Boolean);
$("btnOnionEnable").addEventListener("click", guard(async () => {
  if (!(await confirmDialog("Enable onion research?", "This activates the isolated onion module for allowlisted .onion domains you list, through your local Tor SOCKS proxy. Use only for legitimate public research. You can switch it off at any time with the kill switch.", "Enable"))) return;
  await api("POST", "/api/onion/enable", { confirm: true, allowlist: allowLines() }); toast("Onion module enabled."); await loadSettings(); refreshStatus();
}));
$("btnOnionKill").addEventListener("click", guard(async () => { await api("POST", "/api/onion/kill"); toast("Onion module disabled."); await loadSettings(); refreshStatus(); }));
$("btnOnionRun").addEventListener("click", guard(async () => {
  const urls = $("ourls").value.split(/\s+/).map((x) => x.trim()).filter(Boolean);
  const r = await api("POST", "/api/onion/research", { question: $("oq").value, urls });
  toast(`Retrieved ${r.retrieved} page(s).` + (r.errors.length ? " Some failed - see report." : ""), !r.retrieved); await loadRun(r.run_id);
}));

/* ---------- boot ---------- */
refreshStatus(); setInterval(() => { if (!state.running) refreshStatus(); }, 30000);
(async () => { const r = await api("GET", "/api/runs?limit=1").catch(() => null); if (r) { const s = await api("GET", "/api/settings").catch(() => null); if (s) $("provider").value = s.settings.default_provider; } })();
