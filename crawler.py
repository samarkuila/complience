import hashlib, re, os, json, time, pathlib, datetime as dt, requests
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from dateutil import parser as dparse
import db, screening, paragraphs
HDR = {"User-Agent": "Mozilla/5.0 (Compliance-Regulatory-Monitor)"}
DATA = pathlib.Path("data")
BACKDATE_TOLERANCE_DAYS = 2   # an item dated this many days (or more) before the newest known item counts as backdated
DOC_EXT = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip")
DATE_RE = re.compile(r"\b(\d{1,2}[ /-][A-Za-z]{3,9}[ /-]\d{4}|\d{1,2}[/-]\d{1,2}[/-]\d{4}|[A-Za-z]{3,9} \d{1,2}, \d{4})\b")
SIZE_SUFFIX = re.compile(r"\s*\(\s*[\d.,]+\s*(?:KB|MB|GB|bytes)\s*\)\s*$", re.I)
norm_title = lambda t: SIZE_SUFFIX.sub("", t or "").strip()   # '(652 KB)' on a link is not a content change
is_doc = lambda u: u.lower().split("?")[0].endswith(DOC_EXT)

def fetch(url, render_js=False):
    if render_js:  # needed for JavaScript-built pages
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(); pg = b.new_page()
            pg.goto(url, timeout=60000, wait_until="networkidle"); h = pg.content(); b.close(); return h
    r = requests.get(url, headers=HDR, timeout=45); r.raise_for_status(); return r.text

def parse_items(html, base, selector):
    soup = BeautifulSoup(html, "html.parser")
    scope = soup.select(selector) if selector else [soup]
    if not scope: raise ValueError(f"CSS selector '{selector}' matched nothing - page layout may have changed")
    out = {}
    for node in scope:
        for a in node.find_all("a", href=True):
            href = a["href"].strip()
            if href.startswith(("javascript:", "#", "mailto:")): continue
            title = " ".join(a.get_text(" ").split())
            if len(title) < 4 and not is_doc(href): continue
            url = urljoin(base, href)
            row = a.find_parent(["tr", "li", "div"]); txt = row.get_text(" ") if row else title
            m, shown = DATE_RE.search(txt), None
            if m:
                try: shown = dparse.parse(m.group(1), dayfirst=True).date().isoformat()
                except Exception: pass
            out[url] = (norm_title(title) or os.path.basename(urlparse(url).path), shown)
    return out

def download(url, srcname):
    r = requests.get(url, headers=HDR, timeout=90); r.raise_for_status()
    data = r.content
    if len(data) > 50_000_000: raise ValueError("file larger than 50 MB")
    sha = hashlib.sha256(data).hexdigest()
    d = DATA / "docs" / re.sub(r"\W+", "_", srcname) / dt.date.today().isoformat(); d.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^\w.-]", "_", os.path.basename(urlparse(url).path) or "document")[:80]
    p = d / f"{sha[:8]}_{name}"; p.write_bytes(data); return str(p), sha

def pdf_meta(path):
    import fitz
    d = fitz.open(path); m = d.metadata or {}
    text = "".join(d[i].get_text() for i in range(min(3, len(d))))
    return {"pages": len(d), "title": m.get("title"), "author": m.get("author"),
            "created": m.get("creationDate"), "modified": m.get("modDate")}, text

def detail_docs(url):  # many sites link to a detail page that holds the PDF
    try:
        soup = BeautifulSoup(fetch(url), "html.parser")
        return [urljoin(url, a["href"]) for a in soup.find_all("a", href=True) if is_doc(a["href"])]
    except Exception: return []

def process_item(c, iid, src, url, title):
    docs, text = [], ""
    for u in ([url] if is_doc(url) else detail_docs(url))[:5]:
        try:
            p, sha = download(u, src["name"]); d = {"url": u, "path": p, "sha256": sha}
            if p.lower().endswith(".pdf"):
                did, status, newver = paragraphs.ingest(c, iid, u, p, sha, db.now()); d["document_id"], d["status"] = did, status
                if status == "ok":
                    text += " ".join(r["text"] for r in c.execute("SELECT text FROM paragraphs WHERE document_id=? ORDER BY seq LIMIT 40", (did,)))
                else:   # blocked / scanned / unreadable: never screen or summarise the wrong text; send to a person
                    why = {"blocked": "site served a bot-check page instead of the PDF - download manually and upload",
                           "no_text": "scanned image PDF with no readable text - needs OCR or manual entry"}.get(status, "file could not be read")
                    db.log_exc(c, src["id"], "document", f"{title[:60]} ({u}): {why}")
                    c.execute("UPDATE items SET review_status='pending' WHERE id=?", (iid,))
            docs.append(d)
        except Exception as e: db.log_exc(c, src["id"], "download", f"{u}: {e}")
    s = screening.screen(title, text, [k.strip() for k in (src["keywords"] or "").split(",") if k.strip()])
    c.execute("UPDATE items SET pdf_path=?,meta_json=?,nbfc_flag=?,nbfc_score=?,nbfc_reason=? WHERE id=?",
              (docs[0]["path"] if docs else None, json.dumps({"docs": docs}), s["flag"], s["score"], s["reason"], iid))

def run_source(src, cycle):
    src = dict(src); c = db.conn(); t0 = db.now()
    cid = c.execute("INSERT INTO crawls(source_id,cycle,started,status) VALUES(?,?,?,'running')", (src["id"], cycle, t0)).lastrowid
    try:
        html = fetch(src["url"], src["render_js"])
        items = parse_items(html, src["url"], src["selector"])
        if not items: raise ValueError("No links found - page may be blocked, empty or changed")
        sd = DATA / "snapshots" / re.sub(r"\W+", "_", src["name"]); sd.mkdir(parents=True, exist_ok=True)
        sp = sd / (t0.replace(":", "") + ".html"); sp.write_text(html, encoding="utf-8")
        first_run = src["last_success"] is None
        known = {r["url"]: r for r in c.execute("SELECT * FROM items WHERE source_id=?", (src["id"],))}
        n_new = 0
        dates = [r["shown_date"] for r in known.values() if r["shown_date"]]
        newest = max(dates) if dates else None       # newest date already seen on this source's page
        cutoff = (dt.date.fromisoformat(newest) - dt.timedelta(days=BACKDATE_TOLERANCE_DAYS)).isoformat() if newest else None
        for url, (title, shown) in items.items():
            k = known.get(url)
            if k:
                if norm_title(k["title"]) != title:   # same link, changed wording -> modified
                    c.execute("UPDATE items SET title=?,change_type='modified',detected_at=?,review_status='pending',last_seen=? WHERE id=?",
                              (title, t0, t0, k["id"])); n_new += 1
                    process_item(c, k["id"], src, url, title)
                else: c.execute("UPDATE items SET last_seen=? WHERE id=?", (t0, k["id"]))
                continue
            if first_run: ctype, rs = "baseline", "n/a"       # first crawl only records the starting state
            elif shown and cutoff and shown < cutoff: ctype, rs = "backdated/inserted", "pending"  # new link with an old date
            else: ctype, rs = "new", "pending"
            iid = c.execute("INSERT INTO items(source_id,url,title,shown_date,first_seen,last_seen,detected_at,change_type,review_status,crawl_id) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?)", (src["id"], url, title, shown, t0, t0, t0, ctype, rs, cid)).lastrowid
            if ctype != "baseline": n_new += 1; process_item(c, iid, src, url, title)
        c.execute("UPDATE crawls SET status='ok',finished=?,n_items=?,n_new=?,snapshot_path=? WHERE id=?", (db.now(), len(items), n_new, str(sp), cid))
        c.execute("UPDATE sources SET last_success=?,status='ok' WHERE id=?", (t0, src["id"]))
    except Exception as e:
        c.execute("UPDATE crawls SET status='failed',finished=?,error=? WHERE id=?", (db.now(), str(e)[:500], cid))
        c.execute("UPDATE sources SET status='exception' WHERE id=?", (src["id"],))
        db.log_exc(c, src["id"], "crawl", e)   # routed to manual review - never silently skipped
    c.commit(); c.close()

def run_cycle(cycle, source_id=None):
    c = db.conn()
    q = "SELECT * FROM sources WHERE active=1" + (" AND id=?" if source_id else "")
    rows = c.execute(q, (source_id,) if source_id else ()).fetchall(); c.close()
    for s in rows: run_source(s, cycle); time.sleep(2)   # polite gap between sites
