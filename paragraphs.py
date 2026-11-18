"""PDF -> numbered paragraphs, with page + the circular's own paragraph reference (e.g. "5.2(a)").
The extracted text is the single source for every downstream module, so nothing here summarises or rewrites."""
import re, collections, hashlib, pathlib
try: import pymupdf as fitz
except ImportError: import fitz

# Signs that a "PDF" is really a bot-check / access-denied page, not the circular.
BLOCKED = re.compile(r"please enable javascript|your support id is|what code is in the image|access denied|are you a robot|captcha|request unsuccessful", re.I)
NUM = re.compile(r"^(?:(\d+(?:\.\d+)+)[.)]?|(\d+)[.)])\s+(?=[A-Z(\"'“‘])")   # 5.2  /  5.  /  5)
SUB = re.compile(r"^\(([a-z]{1,4}|\d{1,2})\)\s+")                              # (a) (iv) (1)
HEAD = re.compile(r"^(chapter|part|section|annex(?:ure)?|appendix|schedule)\b", re.I)
PAGENO = re.compile(r"^(page\s*)?\d+(\s*(of|/)\s*\d+)?$", re.I)
ROMAN = set("i ii iii iv v vi vii viii ix x xi xii xiii xiv xv".split())

def _is_page_number(t): return bool(PAGENO.match(t.strip()))

def _norm(t): return re.sub(r"\d+", "#", re.sub(r"\s+", " ", t.lower())).strip()

def _blocks(doc):
    """Text blocks per page in reading order, with running headers/footers removed."""
    pages = []
    for pno, page in enumerate(doc, 1):
        h = page.rect.height; items = []
        for b in page.get_text("blocks"):
            if b[6] != 0 or not b[4].strip(): continue      # skip images
            items.append((pno, b[1], b[3], b[4], h))
        pages.append(sorted(items, key=lambda x: (round(x[1]), 0)))
    n = len(pages); seen = collections.Counter()
    for items in pages:
        for (_, y0, y1, t, h) in items:
            if y1 < h * 0.09 or y0 > h * 0.91: seen[_norm(t)] += 1
    out = []
    for items in pages:
        for (pno, y0, y1, t, h) in items:
            edge = y1 < h * 0.09 or y0 > h * 0.91
            if edge and (_is_page_number(t) or (n >= 3 and seen[_norm(t)] >= 2)): continue
            out.append((pno, t))
    return out

def _succ(tok): return chr(ord(tok) + 1) if len(tok) == 1 else None

def extract(path):
    """Returns {"status": ok|blocked|no_text|unreadable, "pages": n, "meta": {...}, "paragraphs": [...]}"""
    try:
        doc = fitz.open(path)
    except Exception as e:
        return {"status": "unreadable", "pages": 0, "meta": {}, "paragraphs": [], "error": str(e)}
    m = doc.metadata or {}
    meta = {"title": m.get("title"), "author": m.get("author"), "created": m.get("creationDate"), "modified": m.get("modDate")}
    raw = "".join(p.get_text() for p in doc)
    if len(doc) <= 3 and BLOCKED.search(raw) and len(raw) < 1500:
        return {"status": "blocked", "pages": len(doc), "meta": meta, "paragraphs": []}
    if len(raw.strip()) < 50:
        return {"status": "no_text", "pages": len(doc), "meta": meta, "paragraphs": []}   # scanned image; needs OCR / manual

    return {"status": "ok", "pages": len(doc), "meta": meta, "paragraphs": build(_blocks(doc))}

def build(blocks, hard_blocks=False):
    """blocks = [(page, text) or (page, text, "h")]. Keeps the circular's own numbering (5.2(a)); hard_blocks=True makes every block its own paragraph (web pages, Word, Excel)."""
    paras, cur, section = [], None, ""
    top, stack = "", []                                         # top-level number and sub-level markers
    def ref():
        return (top + "".join(f"({t})" for _, t in stack)) if (top or stack) else ""
    def close():
        nonlocal cur
        if cur:
            cur["text"] = re.sub(r"\s+", " ", cur["text"]).strip()
            if cur["text"]: paras.append(cur)
        cur = None
    def start(page, text, r, kind):
        nonlocal cur
        close(); cur = {"page": page, "page_end": page, "ref": r, "kind": kind, "section": section, "text": text}

    for blk in blocks:
        pno, btxt, is_head = blk[0], blk[1], len(blk) > 2 and blk[2] == "h"
        btxt = re.sub(r"(\w)-\n(\w)", r"\1\2", btxt)               # re-join hyphenated line breaks
        first_line = True
        for line in btxt.split("\n"):
            line = line.strip()
            if not line: continue
            mn, ms = NUM.match(line), SUB.match(line)
            if mn:
                top, stack = (mn.group(1) or mn.group(2)), []
                start(pno, line, ref(), "para")
            elif ms:
                tok = ms.group(1)
                last = stack[-1] if stack else None
                if tok.isdigit(): stack = [("d", tok)]
                elif tok in ROMAN and last and last[0] in "ar" and (last[0] == "r" or tok != _succ(last[1])):
                    while stack and stack[-1][0] == "r": stack.pop()
                    stack.append(("r", tok))
                else:
                    while stack and stack[-1][0] in "ar": stack.pop()
                    stack.append(("a", tok))
                start(pno, line, ref(), "para")
            elif first_line and (is_head or HEAD.match(line) or (line.isupper() and len(line) < 100)):
                section = line; start(pno, line, "", "heading"); close()
            elif first_line and (hard_blocks or cur is None or re.search(r"[.:;]$", cur["text"])):
                start(pno, line, "", "para")                        # unnumbered paragraph (preamble, letter text)
            elif cur is not None:
                cur["text"] += " " + line; cur["page_end"] = pno    # wrapped line or continuation across a page break
            else:
                start(pno, line, "", "para")
            first_line = False
    close()
    # Short numbered lines with no end punctuation are headings ("5. Eligibility criteria")
    for p in paras:
        if p["ref"] and len(p["text"]) < 90 and not re.search(r"[.:;,]$", p["text"]) and not SUB.match(p["text"]):
            p["kind"] = "heading"
    sec = ""
    for p in paras:                                             # recompute section for numbered headings
        if p["kind"] == "heading": sec = p["text"]
        p["section"] = sec
    for i, p in enumerate(paras, 1): p["seq"] = i
    return paras

NOISE = re.compile(r"^(\(\s*[\d.,]+\s*(kb|mb)\s*\)$|change language|skip to|menu|search|print|share|font size|back to top|home$)", re.I)
NAV_SET = {"menu", "nav", "navbar", "navigation", "footer", "header", "sidebar", "breadcrumb", "breadcrumbs", "cookie", "social", "search", "skip", "toolbar", "banner", "popup", "modal"}
NAV_END = ("menu", "navbar", "sidebar", "footer", "breadcrumb")
BLOCK_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "tr", "pre", "blockquote", "dd", "dt", "td", "th", "div"]
def _ws(t): return re.sub(r"\s+", " ", t or "").strip()

def strip_chrome(soup):
    """Remove scripts, menus, headers, footers and similar page furniture, in place."""
    for t in soup(["script", "style", "noscript", "nav", "header", "footer", "aside", "iframe", "svg", "button", "select", "option", "input", "textarea"]): t.decompose()
    for t in soup.find_all(True):
        if t.attrs is None: continue
        ident = " ".join(filter(None, [" ".join(t.get("class") or []), t.get("id") or "", t.get("role") or ""]))
        toks = re.split(r"[\s_\-]+", ident.lower())                       # whole words only: "tableheader" is content, "header" is navigation
        if ident and t.name not in ("html", "body", "main", "article") and any(k in NAV_SET or k.endswith(NAV_END) for k in toks): t.decompose()

def extract_html(path):
    return extract_html_str(open(path, "rb").read().decode("utf-8", "ignore"))

def extract_html_str(html):
    """Readable text of a web page (given as HTML text): headings and paragraphs, with menus, footers and scripts removed."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    title = _ws(soup.title.get_text()) if soup.title else ""
    strip_chrome(soup)
    root = soup.find("main") or soup.find("article") or soup.body or soup
    STRUCT = ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "tr", "pre", "blockquote", "dd", "dt", "div"]
    has_inner = lambda e: e.find(STRUCT) is not None                   # a block that holds other blocks is skipped; its parts are read instead
    row_ok = {id(t) for t in root.find_all("tr") if not has_inner(t)}  # plain table rows are read as one line: "a | b | c"
    blocks, seen = [], set()
    for el in root.find_all(STRUCT + ["td", "th"]):
        if el.name in ("td", "th"):
            if has_inner(el) or (el.find_parent("tr") is not None and id(el.find_parent("tr")) in row_ok): continue
        elif has_inner(el): continue
        text = _ws(" | ".join(x for x in (_ws(c.get_text(" ")) for c in el.find_all(["td", "th"])) if x) if (el.name == "tr") else el.get_text(" "))
        if not text or text in seen or NOISE.match(text): continue
        links = sum(len(_ws(a.get_text(" "))) for a in el.find_all("a"))
        if links and links / max(len(text), 1) > 0.8 and len(text) < 120: continue         # link lists are navigation, not content
        seen.add(text); blocks.append((1, text, "h") if el.name in ("h1", "h2", "h3", "h4", "h5", "h6") and len(text) < 200 else (1, text))
    raw = " ".join(b[1] for b in blocks)
    if len(blocks) < 3 and not raw:
        blocks = [(1, _ws(l)) for l in root.get_text("\n").split("\n") if _ws(l)]; raw = " ".join(b[1] for b in blocks)
    meta = {"title": title, "author": None, "created": None, "modified": None}
    if BLOCKED.search(raw) and len(raw) < 1500: return {"status": "blocked", "pages": 1, "meta": meta, "paragraphs": []}
    if len(raw) < 80: return {"status": "no_text", "pages": 1, "meta": meta, "paragraphs": []}
    return {"status": "ok", "pages": 1, "meta": meta, "paragraphs": build(blocks, hard_blocks=True)}

def extract_docx(path):
    try:
        from docx import Document
        from docx.table import Table
        from docx.text.paragraph import Paragraph
        d = Document(path)
    except Exception as e:
        return {"status": "unreadable", "pages": 0, "meta": {}, "paragraphs": [], "error": str(e)}
    blocks = []
    for ch in d.element.body.iterchildren():
        if ch.tag.endswith("}p"):
            p = Paragraph(ch, d); t = _ws(p.text)
            if t: blocks.append((1, t, "h") if (p.style is not None and (p.style.name or "").lower().startswith(("heading", "title"))) else (1, t))
        elif ch.tag.endswith("}tbl"):
            for row in Table(ch, d).rows:
                cells = []
                for c in row.cells:
                    x = _ws(c.text)
                    if x and (not cells or cells[-1] != x): cells.append(x)
                if cells: blocks.append((1, " | ".join(cells)))
    cp = d.core_properties; meta = {"title": cp.title, "author": cp.author, "created": str(cp.created or ""), "modified": str(cp.modified or "")}
    if sum(len(b[1]) for b in blocks) < 50: return {"status": "no_text", "pages": 1, "meta": meta, "paragraphs": []}
    return {"status": "ok", "pages": 1, "meta": meta, "paragraphs": build(blocks, hard_blocks=True)}

def extract_xlsx(path, max_rows=5000):
    """Each non-empty row becomes one paragraph ('Row 5: a | b'); each sheet is a section."""
    try:
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception as e:
        return {"status": "unreadable", "pages": 0, "meta": {}, "paragraphs": [], "error": str(e)}
    blocks = []
    for si, ws in enumerate(wb.worksheets, 1):
        blocks.append((si, f"Sheet: {ws.title}", "h"))
        for ri, row in enumerate(ws.iter_rows(values_only=True), 1):
            if ri > max_rows: blocks.append((si, f"(stopped after {max_rows} rows)")); break
            cells = [_ws(str(v)) for v in row if v is not None and _ws(str(v))]
            if cells: blocks.append((si, f"Row {ri}: " + " | ".join(cells)))
    if sum(len(b[1]) for b in blocks) < 40: return {"status": "no_text", "pages": len(wb.worksheets), "meta": {}, "paragraphs": []}
    return {"status": "ok", "pages": len(wb.worksheets), "meta": {}, "paragraphs": build(blocks, hard_blocks=True)}

KIND_BY_EXT = {".pdf": "pdf", ".html": "html", ".htm": "html", ".docx": "word", ".xlsx": "excel", ".doc": "word (old)", ".xls": "excel (old)", ".zip": "archive"}
def kind_of(path):
    ext = pathlib.Path(path).suffix.lower(); return KIND_BY_EXT.get(ext, "file")

def extract_any(path):
    k = kind_of(path)
    if k == "pdf": return extract(path)
    if k == "html": return extract_html(path)
    if k == "word": return extract_docx(path)
    if k == "excel": return extract_xlsx(path)
    return {"status": "unsupported", "pages": 0, "meta": {}, "paragraphs": []}      # old .doc/.xls and .zip are saved but not read

def trace_label(p):
    """Human-readable pointer back to the source, used in every downstream output."""
    r = p["ref"] or f"unnumbered #{p['seq']}"
    kind = p.get("kind") or "pdf"
    if kind == "html": return f"Para {r} (web page)"
    if kind == "word": return f"Para {r} (Word document)"
    if kind == "excel": return f"{r} (sheet {p['page']})"
    pg = f"p.{p['page']}" + (f"-{p['page_end']}" if p["page_end"] != p["page"] else "")
    return f"Para {r} ({pg})"

def ingest(c, item_id, url, path, sha256, now, html=None):
    """Store a document's TEXT as a version plus its paragraphs. A different hash at the same URL = new version.
    Web pages are passed as html= and only their text is kept (no HTML file is saved); their version hash is the hash of the text.
    Returns (document_id, status, is_new_version); document_id is None for a web page with no real text (not worth storing)."""
    if html is not None:
        kind, r, path = "html", extract_html_str(html), None
        if r["status"] == "no_text": return None, "skipped", False
        sha256 = hashlib.sha256(("\n".join(p["text"] for p in r["paragraphs"]) or html).encode("utf-8")).hexdigest()
    else: kind, r = kind_of(path), None
    row = c.execute("SELECT id,status FROM documents WHERE item_id=? AND url=? AND sha256=?", (item_id, url, sha256)).fetchone()
    if row: return row["id"], row["status"], False
    if r is None: r = extract_any(path)
    prev = c.execute("SELECT MAX(version) v FROM documents WHERE item_id=? AND url=?", (item_id, url)).fetchone()["v"] or 0; m = r["meta"]
    did = c.execute("INSERT INTO documents(item_id,url,sha256,path,version,pages,status,pdf_title,pdf_created,pdf_modified,fetched_at,kind) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (item_id, url, sha256, path, prev + 1, r["pages"], r["status"], m.get("title"), m.get("created"), m.get("modified"), now, kind)).lastrowid
    for p in r["paragraphs"]:
        c.execute("INSERT INTO paragraphs(document_id,seq,page,page_end,para_ref,kind,section,text) VALUES(?,?,?,?,?,?,?,?)",
                  (did, p["seq"], p["page"], p["page_end"], p["ref"], p["kind"], p["section"], p["text"]))
    return did, r["status"], prev > 0
