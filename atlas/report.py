"""Report assembly (from stored evidence) and exports: Markdown, HTML, JSON, CSV."""
from __future__ import annotations

import csv
import html
import io
import json
import re

from . import evidence as ev
from . import security
from .db import Database, j, now


def load_register(db: Database, run_id: int, include_dups: bool = False) -> list[ev.SourceRec]:
    rows = db.q("SELECT s.*, rs.label, rs.rank FROM run_sources rs JOIN sources s ON s.id=rs.source_id "
                "WHERE rs.run_id=? ORDER BY rs.rank", (run_id,))
    out = []
    for r in rows:
        if not include_dups and not r["label"].startswith("S"):
            continue
        rec = ev.SourceRec(id=r["id"], url=r["url"], title=r["title"], domain=r["domain"], stype=r["source_type"],
                           text=r["text"] or r["excerpt"], quality=r["quality"], published=r["published"])
        rec.label = r["label"]                      # type: ignore[attr-defined]
        rec.retrieved = r["retrieved_at"]           # type: ignore[attr-defined]
        rec.duplicate_of = r["duplicate_of"]        # type: ignore[attr-defined]
        rec.meta = json.loads(r["meta_json"] or "{}")  # type: ignore[attr-defined]
        out.append(rec)
    return out


def build_index(register: list[ev.SourceRec], dup_rows: list[dict] | None = None) -> ev.IndependenceIndex:
    idx = ev.IndependenceIndex()
    for r in register:
        idx.add(r)
    for d in dup_rows or []:                       # known duplicates share a group with their original
        if d["duplicate_of"] in idx.parent:
            idx.parent[d["id"]] = idx.find(d["duplicate_of"])
            idx.recs[d["id"]] = ev.SourceRec(id=d["id"], url=d["url"], title=d["title"], domain=d["domain"],
                                              stype=d["source_type"], text="")
    return idx


def _latest_body(db: Database, run_id: int) -> tuple[str, str, str]:
    row = db.one("SELECT kind, provider, body FROM notes WHERE run_id=? AND kind IN ('synthesis','ai_response') "
                 "ORDER BY id DESC LIMIT 1", (run_id,))
    return (row["body"], row["provider"], row["kind"]) if row else ("", "", "")


def rebuild(db: Database, run_id: int) -> str:
    run = db.one("SELECT * FROM runs WHERE id=?", (run_id,))
    reg = load_register(db, run_id)
    labels = {r.label: r for r in reg}
    dups = db.q("SELECT s.id, s.url, s.title, s.domain, s.source_type, s.duplicate_of FROM run_sources rs "
                "JOIN sources s ON s.id=rs.source_id WHERE rs.run_id=? AND rs.label LIKE 'D%'", (run_id,))
    idx = build_index(reg, dups)
    limitations = json.loads(run["limitations_json"] or "[]")
    stats = json.loads(run["stats_json"] or "{}")
    body, prov, kind = _latest_body(db, run_id)
    origin = "model"
    if body:
        body, invalid = ev.validate_citations(body, set(labels))
        if invalid:
            limitations = [x for x in limitations if "citation(s) removed" not in x]
            limitations.append(f"{len(invalid)} citation label(s) in the AI output ({', '.join(invalid)}) did not match any "
                               "stored source and were removed.")
        if not ev.CITE_RE.search(body):
            limitations = [x for x in limitations if "contained no valid" not in x]
            limitations.append("The AI response contained no valid [S#] citations; its statements are UNVERIFIED. "
                               "Extractive findings from the stored sources are appended for traceability.")
    if not body or not ev.CITE_RE.search(body):
        extra = _extractive(db, run, reg, limitations)
        body = (body + "\n\n" + extra).strip() if body else extra
        origin = "extractive" if not kind else "model"
    claims = ev.extract_claims(body, labels, idx)
    with db.conn() as c:
        c.execute("DELETE FROM claims WHERE run_id=?", (run_id,))
        for cl in claims:
            cid = c.execute("INSERT INTO claims(run_id,text,strength,independent_count,uncertainty,origin) VALUES(?,?,?,?,?,?)",
                            (run_id, cl["text"], cl["strength"], cl["independent_count"], cl["uncertainty"], origin)).lastrowid
            for e in cl["evidence"]:
                c.execute("INSERT OR IGNORE INTO claim_evidence(claim_id,source_id,stance,quote) VALUES(?,?,?,?)",
                          (cid, e["source_id"], e["stance"], e["quote"]))
    md = assemble_markdown(db, run_id, body, prov, limitations)
    db.ex("UPDATE runs SET report_md=?, limitations_json=? WHERE id=?", (md, j(limitations), run_id))
    db.index("run", run_id, run["question"], md)
    return md


def _extractive(db, run, reg, limitations) -> str:
    """Deterministic, citation-safe findings: verbatim stored passages, each with its [S#] label."""
    per = 2 if run["mode"] in ("high", "max") else 1
    lines = ["## Extractive findings",
             "No AI model synthesised these sources, so the findings below are **verbatim passages** from stored sources, "
             "ranked by relevance. Use a local model or the browser-assisted workflow for a written synthesis.", ""]
    n = 0
    for r in reg:
        for p in db.q("SELECT text FROM passages WHERE run_id=? AND source_id=? ORDER BY id LIMIT ?", (run["id"], r.id, per)):
            lines.append(f"- {p['text']} [{r.label}]")
            n += 1
    if not n:
        lines.append("- No relevant passages could be extracted from the retrieved sources.")
    return "\n".join(lines)


def assemble_markdown(db: Database, run_id: int, body: str, provider: str, limitations: list[str]) -> str:
    run = db.one("SELECT * FROM runs WHERE id=?", (run_id,))
    stats = json.loads(run["stats_json"] or "{}")
    reg = load_register(db, run_id)
    dups = load_register(db, run_id, True)
    dups = [d for d in dups if d.label.startswith("D")]
    claims = db.q("SELECT * FROM claims WHERE run_id=? ORDER BY id", (run_id,))
    lines = [f"# {run['question']}", "",
             f"- **Mode:** {run['mode']}  ·  **Status:** {run['status']}  ·  **Created:** {run['created_at']}",
             f"- **Analysis provider:** {run['provider'] or 'none'}  ·  **Sources in register:** {len(reg)}"
             + (f"  ·  **Duplicates excluded:** {len(dups)}" if dups else ""),
             f"- **Duration:** {stats.get('duration_s', '?')} s  ·  **Rounds:** {stats.get('rounds', '?')}  ·  "
             f"**Stop reason:** {stats.get('stop_reason', '-')}", ""]
    if run["status"] == "running" or run["status"] == "queued":
        lines += ["_Research is still running._", ""]
    lines += [body.strip(), ""]
    if claims:
        lines += ["## Evidence-backed claims", "", "| # | Claim | Strength | Independent sources | Evidence |", "|---|---|---|---|---|"]
        for i, c in enumerate(claims, 1):
            evs = db.q("SELECT rs.label, ce.stance FROM claim_evidence ce JOIN run_sources rs ON rs.source_id=ce.source_id AND rs.run_id=? "
                       "WHERE ce.claim_id=? ORDER BY rs.rank", (run_id, c["id"]))
            sup = ", ".join(f"[{e['label']}]" for e in evs if e["stance"] == "support")
            con = ", ".join(f"[{e['label']}]" for e in evs if e["stance"] == "contradict")
            cell = sup + (f" · possible conflict: {con}" if con else "")
            lines.append(f"| {i} | {c['text'].replace('|', '/')} | {c['strength']} | {c['independent_count']} | {cell} |")
        lines += ["", "_Strength reflects independent sources and source type; it is not a guarantee of truth. "
                      "Claims marked weak or unverified could not be independently confirmed._", ""]
        conflicts = db.q("SELECT c.text, ce.quote, rs.label FROM claim_evidence ce JOIN claims c ON c.id=ce.claim_id "
                         "JOIN run_sources rs ON rs.source_id=ce.source_id AND rs.run_id=? WHERE c.run_id=? AND ce.stance='contradict'",
                         (run_id, run_id))
        lines += ["## Conflicting findings", ""]
        if conflicts:
            for x in conflicts:
                lines.append(f"- Claim: {x['text']}\n  - Possibly contradicted by [{x['label']}]: \"{x['quote']}\" _(lexical heuristic - verify)_")
        else:
            lines.append("- No contradictions were detected by the automated check. This does not prove that none exist.")
        lines.append("")
    lines += ["## Limitations and unanswered questions", ""] + ([f"- {x}" for x in limitations] or ["- None recorded."]) + [""]
    lines += ["## Methodology", "", _methodology(run, stats), ""]
    lines += ["## Source register", ""]
    for r in reg:
        flags = []
        if r.meta.get("snippet_only"):
            flags.append("snippet only")
        if r.meta.get("abstract_only"):
            flags.append("abstract only")
        if r.meta.get("injection_flags"):
            flags.append("contained instruction-like text")
        lines.append(f"- **{r.label}** [{r.title.replace('[', '(').replace(']', ')')[:160]}]({r.url}) - {r.domain} · {r.stype} · "
                     f"published: {r.published or 'unknown'} · retrieved: {r.retrieved} · quality {r.quality:.2f}"
                     + (f" · _{', '.join(flags)}_" if flags else ""))
    if dups:
        lines += ["", "### Duplicate or syndicated copies (not counted as independent)"]
        for d in dups:
            lines.append(f"- **{d.label}** [{d.title[:120]}]({d.url}) - {d.domain}")
    return "\n".join(lines).strip() + "\n"


def _methodology(run, stats) -> str:
    p = stats.get("policy", {})
    counts = stats.get("counts", {})
    qs = stats.get("queries", [])
    prov = stats.get("search_counts", {})
    parts = [f"Mode **{run['mode']}** policy: up to {p.get('max_sources', '?')} sources, {p.get('max_rounds', '?')} round(s), "
             f"{p.get('fetch_pages', '?')} page fetches, {p.get('time_budget_s', '?')} s budget; citation following "
             f"{'on' if p.get('follow_citations') else 'off'}; cross-checking {'on' if p.get('cross_check') else 'off'}.",
             f"{len(qs)} search queries were issued; results per provider: " + (", ".join(f"{k} {v}" for k, v in prov.items()) or "none") + ".",
             f"Found {counts.get('found', 0)} candidate URLs, read {counts.get('fetched', 0)} pages "
             f"({counts.get('failed', 0)} failed), removed {counts.get('duplicates', 0)} duplicates.",
             "Sources were typed (primary, scholarly, documentation, journalism, reference, opinion, unverified), scored for quality, "
             "and de-duplicated by content similarity. Citations were validated against stored sources; passages are verbatim."]
    if stats.get("subquestions"):
        parts.append("Sub-questions: " + " | ".join(stats["subquestions"]))
    sp = stats.get("stage_providers", [])
    if sp:
        parts.append("AI stages: " + ", ".join(f"{x['stage']} by {x['provider']}" for x in sp) + ".")
    else:
        parts.append("No AI model processed this run (search, extraction and scoring are deterministic).")
    return "\n\n".join(parts)


# ------------------------------------------------------------------ exports
def report_data(db: Database, run_id: int) -> dict:
    run = db.one("SELECT * FROM runs WHERE id=?", (run_id,))
    if not run:
        raise KeyError(run_id)
    srcs = db.q("SELECT rs.label, rs.rank, rs.query, rs.snippet, rs.provider AS found_by, s.id AS source_id, s.url, "
                "s.canonical_url, s.title, s.domain, s.source_type, s.published, s.retrieved_at, s.quality, s.excerpt, "
                "s.duplicate_of FROM run_sources rs JOIN sources s ON s.id=rs.source_id WHERE rs.run_id=? ORDER BY rs.rank", (run_id,))
    claims = db.q("SELECT * FROM claims WHERE run_id=? ORDER BY id", (run_id,))
    for c in claims:
        c["evidence"] = db.q("SELECT ce.stance, ce.quote, rs.label, s.url FROM claim_evidence ce JOIN sources s ON s.id=ce.source_id "
                             "LEFT JOIN run_sources rs ON rs.source_id=ce.source_id AND rs.run_id=? WHERE ce.claim_id=?", (run_id, c["id"]))
    passages = db.q("SELECT p.text, rs.label FROM passages p LEFT JOIN run_sources rs ON rs.source_id=p.source_id AND rs.run_id=p.run_id WHERE p.run_id=?", (run_id,))
    notes = db.q("SELECT id, kind, provider, body, created_at FROM notes WHERE run_id=? ORDER BY id", (run_id,))
    stats = json.loads(run["stats_json"] or "{}")
    stats.pop("request", None) if False else None
    return {"atlas_version": "1.0.0", "id": run["id"], "question": run["question"], "mode": run["mode"],
            "status": run["status"], "provider": run["provider"], "sensitive": bool(run["sensitive"]),
            "created_at": run["created_at"], "finished_at": run["finished_at"], "parent_run_id": run["parent_run_id"],
            "methodology": _methodology(run, stats), "stats": stats, "stages": json.loads(run["stages_json"] or "[]"),
            "limitations": json.loads(run["limitations_json"] or "[]"), "report_markdown": run["report_md"],
            "sources": srcs, "claims": claims, "passages": passages, "notes": notes}


def _csv_safe(v) -> str:
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def sources_csv(db: Database, run_id: int) -> str:
    d = report_data(db, run_id)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["label", "title", "url", "domain", "type", "published", "retrieved_at", "quality", "found_by", "duplicate_of_source_id"])
    for s in d["sources"]:
        w.writerow([_csv_safe(x) for x in (s["label"], s["title"], s["url"], s["domain"], s["source_type"], s["published"],
                                           s["retrieved_at"], s["quality"], s["found_by"], s["duplicate_of"])])
    return out.getvalue()


def evidence_csv(db: Database, run_id: int) -> str:
    d = report_data(db, run_id)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["claim_id", "claim", "strength", "independent_sources", "stance", "source_label", "source_url", "quote", "uncertainty"])
    for c in d["claims"]:
        for e in c["evidence"] or [{"stance": "", "label": "", "url": "", "quote": ""}]:
            w.writerow([_csv_safe(x) for x in (c["id"], c["text"], c["strength"], c["independent_count"], e["stance"],
                                               e["label"], e["url"], e["quote"], c["uncertainty"])])
    return out.getvalue()


def _inline(s: str, labels: set[str]) -> str:
    s = html.escape(s, quote=True)
    s = re.sub(r"\[(.+?)\]\((https?://[^\s)]+)\)", lambda m: f'<a href="{m.group(2)}" rel="noopener noreferrer" target="_blank">{m.group(1)}</a>', s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![\w*])_(.+?)_(?![\w*])", r"<em>\1</em>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\[(S\d{1,3})\]", lambda m: f'<a class="cite" href="#{m.group(1)}">[{m.group(1)}]</a>' if m.group(1) in labels else m.group(0), s)
    return s


def markdown_to_html(md: str, labels: set[str]) -> str:
    """Small, safe Markdown subset renderer: all text is escaped before any tag is emitted."""
    out, para, in_ul, in_tbl = [], [], False, False

    def flush():
        nonlocal para
        if para:
            out.append("<p>" + _inline(" ".join(para), labels) + "</p>")
            para = []

    def close():
        nonlocal in_ul, in_tbl
        if in_ul:
            out.append("</ul>")
            in_ul = False
        if in_tbl:
            out.append("</tbody></table>")
            in_tbl = False

    for line in md.splitlines():
        m = re.match(r"^(#{1,4})\s+(.*)", line)
        if m:
            flush(); close()
            n = len(m.group(1))
            out.append(f"<h{n}>{_inline(m.group(2), labels)}</h{n}>")
        elif re.match(r"^\s*[-*]\s+", line):
            flush()
            if in_tbl:
                close()
            if not in_ul:
                out.append("<ul>"); in_ul = True
            item = re.sub(r"^\s*[-*]\s+", "", line)
            anchor = re.match(r"\*\*([SD]\d{1,3})\*\*", item)
            out.append(f'<li id="{anchor.group(1)}">' if anchor else "<li>")
            out[-1] += _inline(item, labels) + "</li>"
        elif line.startswith("|"):
            flush()
            if re.match(r"^\|[\s\-|:]+\|?$", line):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if not in_tbl:
                close()
                out.append("<table><thead><tr>" + "".join(f"<th>{_inline(c, labels)}</th>" for c in cells) + "</tr></thead><tbody>")
                in_tbl = True
            else:
                out.append("<tr>" + "".join(f"<td>{_inline(c, labels)}</td>" for c in cells) + "</tr>")
        elif re.match(r"^\s*(\d+)\.\s+", line) and re.match(r"^S\d+\.", line.strip()):
            flush(); close()
            lab = line.split(".", 1)[0]
            out.append(f'<p class="src" id="{html.escape(lab)}">{_inline(line, labels)}</p>')
        elif not line.strip():
            flush(); close()
        else:
            if in_ul or in_tbl:
                close()
            para.append(line.strip())
    flush(); close()
    return "\n".join(out)


def export_html(db: Database, run_id: int) -> str:
    run = db.one("SELECT report_md, question FROM runs WHERE id=?", (run_id,))
    labels = {r["label"] for r in db.q("SELECT label FROM run_sources WHERE run_id=?", (run_id,))}
    md = re.sub(r"^(S\d+)\. ", r"\1. ", run["report_md"], flags=re.M)
    body = markdown_to_html(md, labels)
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">"
            f"<title>{html.escape(run['question'][:120])} - ATLAS report</title><style>"
            "body{font:16px/1.6 system-ui,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;color:#16202e;background:#fff}"
            "h1{font-size:1.7rem}h2{margin-top:2rem;border-bottom:1px solid #d6dde8;padding-bottom:.2rem}a{color:#0b6aa8}"
            "table{border-collapse:collapse;width:100%;font-size:.9rem}td,th{border:1px solid #d6dde8;padding:.4rem;text-align:left;vertical-align:top}"
            ".src{font-size:.9rem;margin:.3rem 0}code{background:#eef2f7;padding:0 .25rem}</style></head><body>"
            f"{body}<hr><p><small>Generated by ATLAS. Verify high-impact claims against the original sources.</small></p></body></html>")
