from dotenv import load_dotenv
load_dotenv()   # reads .env (Azure OpenAI settings) before anything else starts
import datetime as dt, pathlib, html as H, hashlib, re
from fastapi import FastAPI, Header, HTTPException, BackgroundTasks, UploadFile, File
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from apscheduler.schedulers.background import BackgroundScheduler
import db, crawler, paragraphs
app = FastAPI(title="Regulatory Crawler")
DECISIONS = {"information_only", "other_function", "potential_future", "implementation_required", "not_nbfc_relevant"}
REPORTS = pathlib.Path("data/reports")

def plain(m):
    """Plain-English version of a technical error, for people reading the daily email."""
    m = m or ""
    for pat, txt in [("Executable doesn't exist", "browser component not installed on the server"), ("404", "page address not found (the URL may have changed)"),
                     ("No links found", "no links found - the site may be blocking automated access or has changed layout"), ("Timeout", "the site took too long to respond"),
                     ("RESET", "the site closed the connection - it may be blocking automated access"), ("bot-check", "document download blocked by the site - manual download needed"),
                     ("scanned image", "scanned document with no readable text - manual review needed")]:
        if pat in m: return txt
    return m.splitlines()[0][:140] if m else ""

# ---------------------------------------------------------------- daily update report (email body, inline styles so it renders in Outlook)
def build_report(cycle, day=None):
    day = day or dt.date.today().isoformat(); c = db.conn()
    name = f"{day}_{cycle}.html"
    cur = c.execute("SELECT status FROM reports WHERE name=?", (name,)).fetchone()
    if cur and cur["status"] == "approved":      # an approved report is a record; never silently overwrite it
        c.close(); return str(REPORTS / name)
    lo, hi = ("00:00:00", "12:59:59") if cycle == "morning" else ("13:00:00", "23:59:59")
    rows = c.execute("SELECT i.*,s.regulator FROM items i JOIN sources s ON s.id=i.source_id WHERE i.change_type!='baseline' "
                     "AND substr(i.detected_at,1,10)=? AND substr(i.detected_at,12,8) BETWEEN ? AND ? "
                     "ORDER BY (i.nbfc_flag='potentially relevant') DESC, s.regulator", (day, lo, hi)).fetchall()
    bad = c.execute("SELECT e.*,s.name sname FROM exceptions e LEFT JOIN sources s ON s.id=e.source_id WHERE e.resolved=0").fetchall()
    total = c.execute("SELECT COUNT(*) FROM sources WHERE active=1").fetchone()[0]
    ok = c.execute("SELECT COUNT(*) FROM sources WHERE active=1 AND status='ok'").fetchone()[0]
    TD = "padding:9px 12px;border-bottom:1px solid #E3E8F0;font:13px/1.45 Segoe UI,Arial,sans-serif;color:#1B2A41;vertical-align:top"
    TH = "padding:8px 12px;background:#F2F5FA;text-align:left;font:600 11px Segoe UI,Arial,sans-serif;color:#5B6B84;text-transform:uppercase;letter-spacing:.04em"
    b = [f"<div style='font:14px/1.6 Segoe UI,Arial,sans-serif;color:#1B2A41'>",
         f"<p style='margin:0 0 14px'>Dear Team,</p><p style='margin:0 0 16px'>Please find the regulatory update for the <b>{cycle}</b> cycle of <b>{day}</b>.</p>"]
    if rows:
        n_rel = sum(1 for r in rows if r["nbfc_flag"] == "potentially relevant")
        b.append(f"<p style='margin:0 0 10px'><b>{len(rows)}</b> new or changed item(s) identified, of which <b>{n_rel}</b> are flagged as potentially NBFC-relevant. "
                 "Flags are suggestions only; applicability is for Compliance to decide.</p>")
        b.append(f"<table style='border-collapse:collapse;width:100%;border:1px solid #E3E8F0'><tr><th style='{TH}'>Regulator</th><th style='{TH}'>Circular / notification</th>"
                 f"<th style='{TH}'>Type</th><th style='{TH}'>Date shown</th><th style='{TH}'>Screening</th></tr>")
        for r in rows:
            b.append(f"<tr><td style='{TD}'><b>{H.escape(r['regulator'])}</b></td><td style='{TD}'><a href='{H.escape(r['url'])}' style='color:#0B3C8C'>{H.escape(r['title'])}</a></td>"
                     f"<td style='{TD}'>{H.escape(r['change_type'])}</td><td style='{TD}'>{r['shown_date'] or '-'}</td><td style='{TD}'>{H.escape(r['nbfc_flag'] or '-')}</td></tr>")
        b.append("</table>")
    else:
        b.append(f"<div style='padding:14px 16px;background:#EAF6EF;border-left:4px solid #1E8E5A;margin:0 0 12px'><b>No new or changed regulatory items were identified</b> "
                 f"in this cycle across the {ok} source(s) that were crawled successfully.</div>")
    if bad:
        b.append("<p style='margin:18px 0 6px'><b>Not covered by this update (manual check needed):</b></p><ul style='margin:0;padding-left:20px'>" +
                 "".join(f"<li>{H.escape(str(e['sname'] or 'System'))}: {H.escape(plain(e['message']))}</li>" for e in bad[:12]) + "</ul>")
    b.append(f"<p style='margin:18px 0 0;color:#5B6B84;font-size:12px'>Sources crawled successfully: {ok} of {total}. Prepared with automated assistance; "
             "reviewed and approved by Compliance before release.</p></div>")
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / name).write_text("\n".join(b), encoding="utf-8")
    c.execute("INSERT INTO reports(name,day,cycle,status,generated_at,n_items) VALUES(?,?,?,'draft',?,?) "
              "ON CONFLICT(name) DO UPDATE SET generated_at=excluded.generated_at,n_items=excluded.n_items,status='draft'",
              (name, day, cycle, db.now(), len(rows)))
    db.audit(c, "system", "report_generated", name); c.commit(); c.close(); return str(REPORTS / name)

def job(cycle): crawler.run_cycle(cycle); build_report(cycle)

@app.on_event("startup")
def start():
    db.init(); s = BackgroundScheduler(timezone="Asia/Kolkata")
    s.add_job(job, "cron", hour=9, minute=0, args=["morning"]); s.add_job(job, "cron", hour=18, minute=0, args=["evening"]); s.start()

class SourceIn(BaseModel):
    regulator: str; name: str; url: str; selector: str = ""; render_js: int = 0; keywords: str = ""; active: int = 1
class TestIn(BaseModel):
    url: str; selector: str = ""; render_js: int = 0
class ReviewIn(BaseModel):
    decision: str; remarks: str = ""
class ApproveIn(BaseModel):
    remarks: str = ""
def q(sql, args=()):
    c = db.conn(); r = [dict(x) for x in c.execute(sql, args)]; c.close(); return r

# ---------------------------------------------------------------- summary
@app.get("/api/summary")
def summary():
    t = dt.date.today(); wk = (t - dt.timedelta(days=6)).isoformat(); t = t.isoformat()
    n = lambda sql, a=(): q(sql, a)[0]["n"]
    return {"sources": n("SELECT COUNT(*) n FROM sources WHERE active=1"),
            "sources_ok": n("SELECT COUNT(*) n FROM sources WHERE active=1 AND status='ok'"),
            "new_today": n("SELECT COUNT(*) n FROM items WHERE change_type!='baseline' AND substr(detected_at,1,10)=?", (t,)),
            "new_week": n("SELECT COUNT(*) n FROM items WHERE change_type!='baseline' AND substr(detected_at,1,10)>=?", (wk,)),
            "last_crawl": (q("SELECT MAX(started) m FROM crawls WHERE status='ok'")[0]["m"] or "Never"),
            "pending": n("SELECT COUNT(*) n FROM items WHERE review_status='pending'"),
            "pending_relevant": n("SELECT COUNT(*) n FROM items WHERE review_status='pending' AND nbfc_flag='potentially relevant'"),
            "open_exceptions": n("SELECT COUNT(*) n FROM exceptions WHERE resolved=0"),
            "reports_awaiting": n("SELECT COUNT(*) n FROM reports WHERE status='draft'")}

# ---------------------------------------------------------------- source register
@app.get("/api/sources")
def sources():
    return q("SELECT s.*,(SELECT COUNT(*) FROM items i WHERE i.source_id=s.id AND i.change_type!='baseline') n_updates,"
             "(SELECT COUNT(*) FROM exceptions e WHERE e.source_id=s.id AND e.resolved=0) n_open_exc,"
             "(SELECT error FROM crawls c WHERE c.source_id=s.id ORDER BY c.id DESC LIMIT 1) last_error FROM sources s ORDER BY s.id")
@app.post("/api/sources")
def add_source(s: SourceIn, x_user: str = Header("compliance_user")):
    c = db.conn()
    try: c.execute("INSERT INTO sources(regulator,name,url,selector,render_js,keywords,active) VALUES(?,?,?,?,?,?,?)",
        (s.regulator, s.name, s.url, s.selector, s.render_js, s.keywords, s.active))
    except Exception: raise HTTPException(400, "A source with this name already exists")
    db.audit(c, x_user, "source_added", s.json()); c.commit(); return {"ok": True}
@app.put("/api/sources/{sid}")
def edit_source(sid: int, s: SourceIn, x_user: str = Header("compliance_user")):
    c = db.conn(); old = c.execute("SELECT regulator,name,url,selector,render_js,keywords,active FROM sources WHERE id=?", (sid,)).fetchone()
    c.execute("UPDATE sources SET regulator=?,name=?,url=?,selector=?,render_js=?,keywords=?,active=? WHERE id=?",
        (s.regulator, s.name, s.url, s.selector, s.render_js, s.keywords, s.active, sid))
    db.audit(c, x_user, "source_edited", f"{sid}: before={dict(old) if old else None} after={s.dict()}"); c.commit(); return {"ok": True}
@app.delete("/api/sources/{sid}")
def delete_source(sid: int, x_user: str = Header("compliance_user")):
    """Only a source with no history can be deleted; otherwise deactivate it, so past updates stay traceable."""
    c = db.conn()
    if c.execute("SELECT 1 FROM crawls WHERE source_id=? UNION SELECT 1 FROM items WHERE source_id=? LIMIT 1", (sid, sid)).fetchone():
        raise HTTPException(409, "This source has crawl history and cannot be deleted. Deactivate it instead - its past updates stay on record.")
    c.execute("DELETE FROM sources WHERE id=?", (sid,)); db.audit(c, x_user, "source_deleted", str(sid)); c.commit(); return {"ok": True}
@app.post("/api/sources/test")
def test_source(t: TestIn):
    """Dry run for the add/edit form: shows what the crawler would see on this page, saves nothing."""
    try:
        items = crawler.parse_items(crawler.fetch(t.url, bool(t.render_js)), t.url, t.selector)
    except Exception as e:
        m = str(e); raise HTTPException(400, "Could not reach this address. Check the URL, or the site may be blocking automated access." if "Max retries" in m or "Connection" in m or "Timeout" in m else plain(m) or m[:200])
    if not items: raise HTTPException(400, "No links found - the page may be blocked, empty, or needs Browser mode / a different selector")
    return {"count": len(items), "sample": [{"title": v[0], "date": v[1], "url": k} for k, v in list(items.items())[:8]]}

# ---------------------------------------------------------------- crawling
@app.post("/api/crawl/run")
def run_now(bg: BackgroundTasks, source_id: int = None):
    bg.add_task(crawler.run_cycle, "manual", source_id); return {"started": True}
@app.get("/api/crawls")
def crawls(): return q("SELECT c.*,s.name sname,s.regulator FROM crawls c JOIN sources s ON s.id=c.source_id ORDER BY c.id DESC LIMIT 200")

# ---------------------------------------------------------------- updates + Compliance decision
@app.get("/api/items")
def items(status: str = "", all: int = 0):
    w = "i.change_type!='baseline'" if not all else "1=1"
    if status: w += " AND i.review_status=?"
    return q(f"SELECT i.*,s.regulator,s.name sname,(SELECT status FROM documents d WHERE d.item_id=i.id ORDER BY d.id DESC LIMIT 1) doc_status,"
             f"(SELECT COUNT(*) FROM paragraphs p JOIN documents d ON d.id=p.document_id WHERE d.item_id=i.id AND d.id=(SELECT MAX(id) FROM documents WHERE item_id=i.id)) n_paras "
             f"FROM items i JOIN sources s ON s.id=i.source_id WHERE {w} ORDER BY i.detected_at DESC LIMIT 300", (status,) if status else ())
@app.post("/api/items/{iid}/review")
def review(iid: int, r: ReviewIn, x_user: str = Header("compliance_user")):
    if r.decision not in DECISIONS: raise HTTPException(400, "Unknown decision")
    if r.decision == "not_nbfc_relevant" and not r.remarks.strip(): raise HTTPException(400, "Please add a remark explaining why this is not NBFC relevant")
    c = db.conn(); it = c.execute("SELECT nbfc_flag,review_status FROM items WHERE id=?", (iid,)).fetchone()
    if not it: raise HTTPException(404, "Unknown item")
    override = (it["nbfc_flag"] == "potentially relevant" and r.decision == "not_nbfc_relevant") or (it["nbfc_flag"] == "not flagged" and r.decision == "implementation_required")
    c.execute("UPDATE items SET review_status=?,reviewer=?,remarks=?,reviewed_at=? WHERE id=?", (r.decision, x_user, r.remarks, db.now(), iid))
    db.audit(c, x_user, "item_reviewed", f"item {iid}: {it['review_status']} -> {r.decision}; AI flag was '{it['nbfc_flag']}'"
             f"{' (OVERRIDE of AI suggestion)' if override else ''}; remarks: {r.remarks}"); c.commit(); return {"ok": True}
@app.get("/api/items/{iid}/paragraphs")
def item_paragraphs(iid: int):
    d = q("SELECT * FROM documents WHERE item_id=? ORDER BY id DESC LIMIT 1", (iid,))
    if not d: raise HTTPException(404, "No document stored for this item")
    rows = q("SELECT * FROM paragraphs WHERE document_id=? ORDER BY seq", (d[0]["id"],))
    for r in rows: r["trace"] = paragraphs.trace_label({"ref": r["para_ref"], "seq": r["seq"], "page": r["page"], "page_end": r["page_end"]})
    return {"document": d[0], "paragraphs": rows}
@app.post("/api/items/{iid}/upload")
async def upload_doc(iid: int, f: UploadFile = File(...), x_user: str = Header("compliance_user")):
    """Manual fallback when the site blocks automated download: a person downloads the PDF and uploads it here."""
    data = await f.read()
    if not data.startswith(b"%PDF"): raise HTTPException(400, "Not a PDF file")
    if len(data) > 50_000_000: raise HTTPException(400, "File larger than 50 MB")
    sha = hashlib.sha256(data).hexdigest(); d = pathlib.Path("data/docs/manual") / dt.date.today().isoformat(); d.mkdir(parents=True, exist_ok=True)
    p = d / f"{sha[:8]}_{re.sub(r'[^A-Za-z0-9_.-]', '_', f.filename or 'upload.pdf')[:80]}"; p.write_bytes(data)
    c = db.conn(); it = c.execute("SELECT url FROM items WHERE id=?", (iid,)).fetchone()
    if not it: raise HTTPException(404, "Unknown item")
    did, status, newver = paragraphs.ingest(c, iid, it["url"], str(p), sha, db.now())
    if status == "ok": c.execute("UPDATE items SET pdf_path=? WHERE id=?", (str(p), iid))
    db.audit(c, x_user, "document_uploaded", f"item {iid}: {f.filename} sha256={sha[:12]} status={status}"); c.commit(); c.close()
    return {"document_id": did, "status": status, "new_version": newver}
@app.get("/api/doc/{iid}")
def doc(iid: int):
    p = q("SELECT pdf_path FROM items WHERE id=?", (iid,))
    if not p or not p[0]["pdf_path"]: raise HTTPException(404, "No downloaded document for this item")
    return FileResponse(p[0]["pdf_path"], media_type="application/pdf")

# ---------------------------------------------------------------- exceptions, audit
@app.get("/api/exceptions")
def excs(): return q("SELECT e.*,s.name sname FROM exceptions e LEFT JOIN sources s ON s.id=e.source_id ORDER BY e.resolved, e.id DESC LIMIT 200")
@app.post("/api/exceptions/{eid}/resolve")
def resolve(eid: int, x_user: str = Header("compliance_user")):
    c = db.conn(); c.execute("UPDATE exceptions SET resolved=1 WHERE id=?", (eid,)); db.audit(c, x_user, "exception_resolved", str(eid)); c.commit(); return {"ok": True}
@app.get("/api/audit")
def audit(): return q("SELECT * FROM audit ORDER BY id DESC LIMIT 200")

# ---------------------------------------------------------------- daily update: preview + approval
@app.post("/api/report/generate")
def gen(cycle: str = "morning"):
    if cycle not in ("morning", "evening"): raise HTTPException(400, "cycle must be morning or evening")
    return {"file": build_report(cycle)}
@app.get("/api/reports")
def reports():
    rows = {r["name"]: r for r in q("SELECT * FROM reports")}
    if REPORTS.exists():       # reports created before approval tracking existed count as drafts
        for f in REPORTS.glob("*.html"):
            if f.name not in rows:
                d, cyc = f.stem.split("_", 1); rows[f.name] = {"name": f.name, "day": d, "cycle": cyc, "status": "draft", "generated_at": None, "n_items": None}
    return sorted(rows.values(), key=lambda r: (r["day"], r["cycle"]), reverse=True)
@app.get("/api/report/{name}", response_class=HTMLResponse)
def report(name: str):
    f = REPORTS / pathlib.Path(name).name
    if not f.exists(): raise HTTPException(404, "Report not found")
    return f.read_text(encoding="utf-8")
@app.post("/api/report/{name}/approve")
def approve_report(name: str, a: ApproveIn, x_user: str = Header("compliance_user")):
    name = pathlib.Path(name).name; c = db.conn()
    if not (REPORTS / name).exists(): raise HTTPException(404, "Report not found")
    d, cyc = name.replace(".html", "").split("_", 1)
    c.execute("INSERT INTO reports(name,day,cycle,status) VALUES(?,?,?, 'draft') ON CONFLICT(name) DO NOTHING", (name, d, cyc))
    c.execute("UPDATE reports SET status='approved',approved_by=?,approved_at=?,remarks=? WHERE name=?", (x_user, db.now(), a.remarks, name))
    db.audit(c, x_user, "report_approved", f"{name}; remarks: {a.remarks}"); c.commit(); return {"ok": True}
@app.post("/api/report/{name}/reopen")
def reopen_report(name: str, x_user: str = Header("compliance_user")):
    name = pathlib.Path(name).name; c = db.conn()
    c.execute("UPDATE reports SET status='draft',approved_by=NULL,approved_at=NULL WHERE name=?", (name,))
    db.audit(c, x_user, "report_reopened", name); c.commit(); return {"ok": True}

app.mount("/", StaticFiles(directory="static", html=True), name="static")
