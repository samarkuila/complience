import hashlib, re, os, json, time, pathlib, datetime as dt, requests
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from dateutil import parser as dparse
import db, screening, paragraphs
HDR = {"User-Agent": "Mozilla/5.0 (Compliance-Regulatory-Monitor)"}
DATA = pathlib.Path("data")
BACKDATE_TOLERANCE_DAYS = 2   # an item dated this many days (or more) before the newest known item counts as backdated
FIRST_CRAWL_CAPTURE = 10    # on a source's first crawl, also read the content of this many items so there is something to see straight away
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

def sniff_ext(data):
    """Extension for a file served from a link with no clear ending."""
    if data[:5] == b"%PDF-": return ".pdf"
    if data[:2] == b"PK":
        try:
            import zipfile, io
            names = zipfile.ZipFile(io.BytesIO(data)).namelist()
            if "word/document.xml" in names: return ".docx"
            if "xl/workbook.xml" in names: return ".xlsx"
        except Exception: pass
    return ""

def store(data, name, srcname):
    """Save bytes under data/docs/<source>/<date>/, named by content hash. Returns (path, sha256)."""
    if len(data) > 50_000_000: raise ValueError("file larger than 50 MB")
    sha = hashlib.sha256(data).hexdigest()
    d = DATA / "docs" / re.sub(r"\W+", "_", srcname) / dt.date.today().isoformat(); d.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^\w.-]", "_", name)[:80] or "document"
    if paragraphs.kind_of(name) == "file": name += sniff_ext(data)      # a real PDF/Word/Excel file served from a link with no ending
    p = d / f"{sha[:8]}_{name}"; p.write_bytes(data); return str(p), sha

def download(url, srcname):
    r = requests.get(url, headers=HDR, timeout=90); r.raise_for_status()
    return store(r.content, os.path.basename(urlparse(url).path) or "document", srcname)

FILE_TYPES = ("pdf", "octet-stream", "msword", "ms-excel", "officedocument")
def page_and_docs(url, render_js=False):
    """For an item link that is not itself a file: the page's HTML (to be read) and the documents linked from it.
    If the link turns out to serve a file directly (even without a .pdf ending), returns (None, [url])."""
    try:
        if render_js: html = fetch(url, True)
        else:
            r = requests.get(url, headers=HDR, timeout=45, stream=True); r.raise_for_status()
            ct = (r.headers.get("Content-Type") or "").lower(); cd = (r.headers.get("Content-Disposition") or "").lower()
            if any(t in ct for t in FILE_TYPES) or ".pdf" in cd: r.close(); return None, [url]
            html = r.text
        soup = BeautifulSoup(html, "html.parser"); paragraphs.strip_chrome(soup); links = []      # menu and footer files (accessibility notes etc.) are not part of the item
        for a in soup.find_all("a", href=True):
            u = urljoin(url, a["href"].strip())
            if is_doc(u) and u not in links: links.append(u)
        return html, links
    except Exception: return None, []

WHY = {"blocked": "the site showed a bot-check page instead of the content - download it manually and upload it",
       "no_text": "scanned image with no readable text - needs OCR or manual entry",
       "unsupported": "old Word/Excel or archive file: saved, but not read - open it manually",
       "unreadable": "file could not be read"}

def process_item(c, iid, src, url, title):
    """Capture everything behind an item: the web page text, and every PDF / Word / Excel file it links to."""
    docs, now = [], db.now()
    page_html, targets = (None, [url]) if is_doc(url) else page_and_docs(url, src["render_js"])
    def record(u, path, sha):
        did, status, _ = paragraphs.ingest(c, iid, u, path, sha, now)
        docs.append({"url": u, "path": path, "sha256": sha, "kind": paragraphs.kind_of(path), "document_id": did, "status": status})
    if page_html:      # keep only the page's text (in the database), never the HTML itself
        try:
            did, status, _ = paragraphs.ingest(c, iid, url, None, None, now, html=page_html)
            docs.append({"url": url, "path": None, "sha256": None, "kind": "html", "document_id": did, "status": status})
        except Exception as e: db.log_exc(c, src["id"], "page", f"{url}: {e}")
    for u in targets[:5]:
        try: record(u, *download(u, src["name"]))
        except Exception as e: db.log_exc(c, src["id"], "download", f"{u}: {e}")
    # A blocked PDF is only a spare copy when the page text itself was captured: note it quietly instead of raising an exception.
    solid = any(d["kind"] == "html" and d["status"] == "ok" and d["document_id"] and
                (c.execute("SELECT COALESCE(SUM(LENGTH(text)),0) FROM paragraphs WHERE document_id=?", (d["document_id"],)).fetchone()[0] >= 600) for d in docs)
    for d in docs:
        if d["status"] == "blocked" and d["kind"] == "pdf" and solid:
            c.execute("UPDATE documents SET status='copy_blocked' WHERE id=?", (d["document_id"],)); d["status"] = "copy_blocked"
        elif d["status"] in WHY: db.log_exc(c, src["id"], "document", f"{title[:60]} ({d['url']}): {WHY[d['status']]}")
    text = " ".join(r["text"] for r in c.execute("SELECT p.text FROM paragraphs p JOIN documents d ON d.id=p.document_id WHERE d.item_id=? AND d.status='ok' "
                                                  "ORDER BY d.id,p.seq LIMIT 60", (iid,)))
    s = screening.screen(title, text, [k.strip() for k in (src["keywords"] or "").split(",") if k.strip()])
    pdfs = [d for d in docs if d["kind"] == "pdf"]
    c.execute("UPDATE items SET pdf_path=?,meta_json=?,nbfc_flag=?,nbfc_score=?,nbfc_reason=?,content_checked=? WHERE id=?",
              (pdfs[0]["path"] if pdfs else None, json.dumps({"docs": docs}), s["flag"], s["score"], s["reason"], now, iid))

def run_source(src, cycle):
    src = dict(src); c = db.conn(); t0 = db.now()
    cid = c.execute("INSERT INTO crawls(source_id,cycle,started,status) VALUES(?,?,?,'running')", (src["id"], cycle, t0)).lastrowid
    try:
        html = fetch(src["url"], src["render_js"])
        items = parse_items(html, src["url"], src["selector"])
        if not items: raise ValueError("No links found - page may be blocked, empty or changed")
        first_run = src["last_success"] is None
        known = {r["url"]: r for r in c.execute("SELECT * FROM items WHERE source_id=?", (src["id"],))}
        n_new = 0; n_cap = 0
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
            elif n_cap < FIRST_CRAWL_CAPTURE: n_cap += 1; process_item(c, iid, src, url, title)      # first crawl: read the top of the list now
        c.execute("UPDATE crawls SET status='ok',finished=?,n_items=?,n_new=?,snapshot_path=? WHERE id=?", (db.now(), len(items), n_new, None, cid))      # the page's HTML is not kept: only its links (items) and text are stored
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

def capture_existing(source_id=None, limit=10):
    """Read the content (page text, PDF, Word, Excel) of items that were already on the site at the first crawl. Newest-first, politely paced."""
    c = db.conn(); done = 0
    q = "SELECT * FROM sources WHERE active=1" + (" AND id=?" if source_id else "")
    for src in [dict(r) for r in c.execute(q, (source_id,) if source_id else ())]:
        rows = c.execute("SELECT id,url,title FROM items WHERE source_id=? AND change_type='baseline' AND content_checked IS NULL ORDER BY id LIMIT ?", (src["id"], limit)).fetchall()
        for r in rows:
            try: process_item(c, r["id"], src, r["url"], r["title"]); c.commit(); done += 1
            except Exception as e: db.log_exc(c, src["id"], "capture", f"{r['url']}: {e}"); c.commit()
            time.sleep(1)
    db.audit(c, "system", "content_captured", f"{done} existing item(s) read (limit {limit} per source)"); c.commit(); c.close(); return done
