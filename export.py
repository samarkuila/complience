"""Crawl details report: Excel and PDF, for a date range. Read-only; never changes crawl data."""
import io, os, pathlib, datetime as dt
import db

def plain(m):
    """Plain-English version of a technical error, for people reading reports and emails."""
    m = m or ""
    for pat, txt in [("Executable doesn't exist", "browser component not installed on the server"), ("404", "page address not found (the URL may have changed)"),
                     ("No links found", "no links found - the site may be blocking automated access or has changed layout"), ("Timeout", "the site took too long to respond"),
                     ("RESET", "the site closed the connection - it may be blocking automated access"), ("bot-check", "document download blocked by the site - manual download needed"),
                     ("scanned image", "scanned document with no readable text - manual review needed")]:
        if pat in m: return txt
    return m.splitlines()[0][:140] if m else ""

RESULT = {"ok": "Completed", "failed": "Failed", "running": "Running"}
DECISION = {"pending": "Pending review", "n/a": "", "implementation_required": "Implementation required", "potential_future": "Future relevance",
            "other_function": "Other department", "information_only": "Information only", "not_nbfc_relevant": "Not NBFC relevant"}
KINDS = {"pdf": "PDF", "html": "Web page", "word": "Word", "excel": "Excel"}
STATUS = {"ok": "Read", "copy_blocked": "PDF copy blocked (page text captured)", "blocked": "Blocked by site", "no_text": "Scanned (no text)", "unsupported": "Saved, not read", "unreadable": "Unreadable"}
CHANGE = {"new": "New", "modified": "Modified", "backdated/inserted": "Backdated / inserted"}

def gather(d_from, d_to):
    """All data for the report between two dates (inclusive, ISO yyyy-mm-dd)."""
    c = db.conn(); rng = (d_from, d_to)
    crawls = [dict(r) for r in c.execute(
        "SELECT c.*,s.regulator,s.name sname,s.url FROM crawls c JOIN sources s ON s.id=c.source_id WHERE substr(c.started,1,10) BETWEEN ? AND ? ORDER BY c.started DESC", rng)]
    items = [dict(r) for r in c.execute(
        "SELECT i.*,s.regulator,s.name sname FROM items i JOIN sources s ON s.id=i.source_id WHERE i.change_type!='baseline' "
        "AND substr(i.detected_at,1,10) BETWEEN ? AND ? ORDER BY i.detected_at DESC", rng)]
    excs = [dict(r) for r in c.execute(
        "SELECT e.*,s.regulator,s.name sname FROM exceptions e LEFT JOIN sources s ON s.id=e.source_id WHERE substr(e.ts,1,10) BETWEEN ? AND ? ORDER BY e.ts DESC", rng)]
    docs = [dict(r) for r in c.execute(
        "SELECT d.*,i.title item_title,s.regulator,(SELECT COUNT(*) FROM paragraphs p WHERE p.document_id=d.id) n_paras FROM documents d JOIN items i ON i.id=d.item_id "
        "JOIN sources s ON s.id=i.source_id WHERE substr(d.fetched_at,1,10) BETWEEN ? AND ? ORDER BY d.id DESC", rng)]
    content = [dict(r) for r in c.execute(
        "SELECT p.*,d.kind dkind,d.url durl,i.title item_title,s.regulator FROM paragraphs p JOIN documents d ON d.id=p.document_id JOIN items i ON i.id=d.item_id "
        "JOIN sources s ON s.id=i.source_id WHERE substr(d.fetched_at,1,10) BETWEEN ? AND ? ORDER BY d.id DESC, p.seq LIMIT 20000", rng)]
    per = {}
    for r in crawls:
        p = per.setdefault(r["source_id"], {"regulator": r["regulator"], "name": r["sname"], "runs": 0, "ok": 0, "failed": 0, "new": 0, "last_ok": None, "last_error": None})
        p["runs"] += 1; p["new"] += r["n_new"] or 0
        if r["status"] == "ok":
            p["ok"] += 1; p["last_ok"] = p["last_ok"] or r["started"]          # crawls are newest first
        elif r["status"] == "failed":
            p["failed"] += 1; p["last_error"] = p["last_error"] or plain(r["error"])
    c.close()
    sources = sorted(per.values(), key=lambda p: (p["regulator"] or "").lower())
    tot = {"runs": len(crawls), "ok": sum(1 for r in crawls if r["status"] == "ok"), "failed": sum(1 for r in crawls if r["status"] == "failed"),
           "updates": len(items), "relevant": sum(1 for i in items if i["nbfc_flag"] == "potentially relevant"),
           "pending": sum(1 for i in items if i["review_status"] == "pending"), "open_exc": sum(1 for e in excs if not e["resolved"])}
    return {"from": d_from, "to": d_to, "generated": dt.datetime.now().strftime("%d %b %Y, %H:%M"), "totals": tot,
            "sources": sources, "crawls": crawls, "items": items, "exceptions": excs, "docs": docs, "content": content}

def _t(s): return (s or "").replace("T", " ")[:16]

# rows as plain lists, shared by both formats
def tables(d):
    return {
     "Source summary": (["Regulator", "Page", "Crawl runs", "Completed", "Failed", "New / changed found", "Last successful crawl", "Latest problem"],
        [[p["regulator"], p["name"], p["runs"], p["ok"], p["failed"], p["new"], _t(p["last_ok"]) or "-", p["last_error"] or ""] for p in d["sources"]]),
     "Crawl runs": (["Started", "Regulator", "Page", "Run type", "Result", "Links seen", "New / changed", "Problem"],
        [[_t(r["started"]), r["regulator"], r["sname"], r["cycle"], RESULT.get(r["status"], r["status"]), r["n_items"], r["n_new"], plain(r["error"])] for r in d["crawls"]]),
     "Updates found": (["Found", "Regulator", "Title", "Type", "Date shown", "NBFC screening", "Compliance decision", "Remarks", "Link"],
        [[_t(i["detected_at"]), i["regulator"], i["title"], CHANGE.get(i["change_type"], i["change_type"]), i["shown_date"] or "", (i["nbfc_flag"] or "").capitalize(),
          DECISION.get(i["review_status"], i["review_status"]), i["remarks"] or "", i["url"]] for i in d["items"]]),
     "Documents captured": (["Fetched", "Regulator", "Item", "Type", "Status", "Paragraphs", "Pages / sheets", "Version", "File address", "SHA-256 (first 12)"],
        [[_t(x["fetched_at"]), x["regulator"], x["item_title"], KINDS.get(x.get("kind"), x.get("kind") or "PDF"), STATUS.get(x["status"], x["status"]), x["n_paras"], x["pages"], x["version"], x["url"], (x["sha256"] or "")[:12]] for x in d["docs"]]),
     "Extracted content": (["Regulator", "Item", "Document type", "Section", "Reference", "Page / sheet", "Text"],
        [[x["regulator"], x["item_title"], KINDS.get(x["dkind"], x["dkind"] or "PDF"), x["section"] or "", x["para_ref"] or f"unnumbered #{x['seq']}", x["page"], (x["text"] or "")[:32000]] for x in d["content"]]),
     "Exceptions": (["When", "Regulator", "Page", "Where", "Problem", "Status"],
        [[_t(e["ts"]), e["regulator"] or "System", e["sname"] or "", e["stage"], plain(e["message"]) or e["message"], "Resolved" if e["resolved"] else "Open"] for e in d["exceptions"]])}

def to_xlsx(d):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    wb = Workbook(); ws = wb.active; ws.title = "Overview"
    hdr_fill = PatternFill("solid", fgColor="0B3C8C"); hdr_font = Font(bold=True, color="FFFFFF"); thin = Side(style="thin", color="E3E8F0")
    def put(sheet, r, c_, v, **kw):
        cell = sheet.cell(row=r, column=c_, value=v)
        if isinstance(v, str): cell.data_type = "s"          # text stays text: a title starting with "=" must never run as a formula
        for k, x in kw.items(): setattr(cell, k, x)
        return cell
    t = d["totals"]
    put(ws, 1, 1, "Regulatory crawl report", font=Font(bold=True, size=16, color="0B3C8C"))
    put(ws, 2, 1, f"Period: {d['from']} to {d['to']}   |   Generated: {d['generated']}", font=Font(color="5B6B84"))
    for i, (k, v) in enumerate([("Crawl runs", t["runs"]), ("Completed", t["ok"]), ("Failed", t["failed"]), ("New or changed items found", t["updates"]),
                                ("Potentially NBFC-relevant", t["relevant"]), ("Still pending Compliance review", t["pending"]), ("Open exceptions", t["open_exc"])], 4):
        put(ws, i, 1, k, font=Font(bold=True)); put(ws, i, 2, v, alignment=Alignment(horizontal="left"))
    put(ws, 12, 1, "AI and keyword flags are suggestions only. Applicability and implementation decisions rest with Compliance.", font=Font(italic=True, color="5B6B84"))
    ws.column_dimensions["A"].width = 38; ws.column_dimensions["B"].width = 14
    for name, (head, rows) in tables(d).items():
        sh = wb.create_sheet(name[:31])
        for j, h in enumerate(head, 1): put(sh, 1, j, h, font=hdr_font, fill=hdr_fill, alignment=Alignment(vertical="center", wrap_text=True))
        for i, row in enumerate(rows, 2):
            for j, v in enumerate(row, 1): put(sh, i, j, v, alignment=Alignment(vertical="top", wrap_text=True), border=Border(bottom=thin))
        for j, h in enumerate(head, 1):
            w = max([len(str(h))] + [len(str(r[j - 1])) for r in rows[:200]]); sh.column_dimensions[get_column_letter(j)].width = min(max(w + 2, 11), 60)
        sh.freeze_panes = "A2"
        if rows: sh.auto_filter.ref = f"A1:{get_column_letter(len(head))}{len(rows) + 1}"
        else: put(sh, 3, 1, "Nothing in this period.", font=Font(italic=True, color="7A889E"))
    buf = io.BytesIO(); wb.save(buf); return buf.getvalue()

def to_pdf(d):
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer, PageBreak
    from xml.sax.saxutils import escape
    BRAND, INK, MUTE = colors.HexColor("#0B3C8C"), colors.HexColor("#1B2A41"), colors.HexColor("#5B6B84")
    st = lambda **k: ParagraphStyle("x", fontName=k.pop("fontName", "Helvetica"), fontSize=k.pop("fontSize", 8), leading=k.pop("leading", 10), textColor=k.pop("textColor", INK), **k)
    cell, head = st(), st(fontName="Helvetica-Bold", textColor=colors.white)
    buf = io.BytesIO(); W = landscape(A4)[0] - 24 * mm
    def footer(canv, doc):
        canv.saveState(); canv.setFont("Helvetica", 7.5); canv.setFillColor(MUTE)
        canv.drawString(12 * mm, 8 * mm, f"Regulatory crawl report  |  {d['from']} to {d['to']}  |  Flags are suggestions; Compliance decides.")
        canv.drawRightString(landscape(A4)[0] - 12 * mm, 8 * mm, f"Page {doc.page}"); canv.restoreState()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm, bottomMargin=14 * mm, title="Regulatory crawl report")
    t = d["totals"]; S = []
    S += [Paragraph("Regulatory crawl report", st(fontName="Helvetica-Bold", fontSize=18, leading=22, textColor=BRAND)),
          Paragraph(f"Period: {d['from']} to {d['to']} &nbsp;|&nbsp; Generated: {d['generated']}", st(fontSize=9, textColor=MUTE)), Spacer(1, 6 * mm)]
    kp = [("Crawl runs", t["runs"]), ("Completed", t["ok"]), ("Failed", t["failed"]), ("New / changed found", t["updates"]),
          ("Potentially NBFC-relevant", t["relevant"]), ("Pending review", t["pending"]), ("Open exceptions", t["open_exc"])]
    k = Table([[Paragraph(f"<b><font size=15>{v}</font></b>", st(leading=17)) for _, v in kp], [Paragraph(n, st(fontSize=7.5, textColor=MUTE)) for n, _ in kp]], colWidths=[W / 7] * 7)
    k.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), .5, colors.HexColor("#E3E8F0")), ("INNERGRID", (0, 0), (-1, -1), .5, colors.HexColor("#E3E8F0")),
                           ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F5F7FB")), ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    S += [k, Spacer(1, 6 * mm)]
    widths = {"Source summary": [.10, .20, .07, .08, .07, .10, .14, .24], "Crawl runs": [.11, .08, .20, .07, .09, .07, .08, .30],
              "Updates found": [.09, .07, .30, .09, .08, .10, .12, .15], "Documents captured": [.09, .07, .30, .07, .09, .07, .08, .06, .17, .0], "Exceptions": [.11, .09, .17, .08, .45, .10]}
    for n, (title, (hd, rows)) in enumerate(tables(d).items()):
        if title == "Extracted content": continue                                        # full text is in the Excel version
        if title == "Documents captured": hd, rows = hd[:-1], [r[:-1] for r in rows]; widths[title] = widths[title][:-1]
        if title == "Updates found": hd, rows = hd[:-1], [r[:-1] for r in rows]          # the link column is in the Excel version
        if n: S.append(PageBreak()) if title in ("Crawl runs",) else S.append(Spacer(1, 6 * mm))
        S.append(Paragraph(f"{title} ({len(rows)})", st(fontName="Helvetica-Bold", fontSize=11, leading=14, textColor=BRAND))); S.append(Spacer(1, 2 * mm))
        if not rows: S.append(Paragraph("Nothing in this period.", st(textColor=MUTE))); continue
        data = [[Paragraph(escape(h), head) for h in hd]] + [[Paragraph(escape(str(v if v is not None else "")), cell) for v in r] for r in rows]
        tb = Table(data, colWidths=[W * w for w in widths[title]], repeatRows=1)
        tb.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), BRAND), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFD")]),
                                ("LINEBELOW", (0, 0), (-1, -1), .25, colors.HexColor("#E3E8F0")), ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
        S.append(tb)
    doc.build(S, onFirstPage=footer, onLaterPages=footer); return buf.getvalue()

def report_dir():
    """Where saved Excel reports live. Set REPORT_FOLDER in .env to use another folder, e.g. a OneDrive-synced one."""
    d = pathlib.Path(os.getenv("REPORT_FOLDER") or "data/crawl_reports"); d.mkdir(parents=True, exist_ok=True); return d

def save(d_from, d_to, label="manual", by="system"):
    """Build the Excel crawl report for the dates and keep it as a file plus a record in the database. Never overwrites an earlier file."""
    data = gather(d_from, d_to); body = to_xlsx(data)
    stem = f"crawl_report_{d_from}" + (f"_to_{d_to}" if d_to != d_from else "") + f"_{label}"
    c = db.conn(); n, name = 1, f"{stem}.xlsx"
    while (report_dir() / name).exists() or c.execute("SELECT 1 FROM saved_reports WHERE name=?", (name,)).fetchone():
        n += 1; name = f"{stem}_{n}.xlsx"
    path = report_dir() / name; path.write_bytes(body)
    t = data["totals"]
    c.execute("INSERT INTO saved_reports(name,path,period_from,period_to,label,created_at,created_by,size,n_runs,n_updates) VALUES(?,?,?,?,?,?,?,?,?,?)",
              (name, str(path.resolve()), d_from, d_to, label, db.now(), by, len(body), t["runs"], t["updates"]))
    db.audit(c, by, "report_saved", f"{name} ({d_from}..{d_to}, {t['runs']} runs, {t['updates']} updates)"); c.commit(); c.close()
    return name
