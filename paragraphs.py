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

    for pno, btxt in _blocks(doc):
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
            elif first_line and (HEAD.match(line) or (line.isupper() and len(line) < 100)):
                section = line; start(pno, line, "", "heading"); close()
            elif first_line and (cur is None or re.search(r"[.:;]$", cur["text"])):
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
    return {"status": "ok", "pages": len(doc), "meta": meta, "paragraphs": paras}

def trace_label(p):
    """Human-readable pointer back to the source, used in every downstream output."""
    r = p["ref"] or f"unnumbered #{p['seq']}"
    pg = f"p.{p['page']}" + (f"-{p['page_end']}" if p["page_end"] != p["page"] else "")
    return f"Para {r} ({pg})"

def ingest(c, item_id, url, path, sha256, now):
    """Store a downloaded file as a document VERSION plus its paragraphs. A different hash at the same URL = new version.
    Returns (document_id, status, is_new_version)."""
    row = c.execute("SELECT id,status FROM documents WHERE item_id=? AND url=? AND sha256=?", (item_id, url, sha256)).fetchone()
    if row: return row["id"], row["status"], False
    prev = c.execute("SELECT MAX(version) v FROM documents WHERE item_id=? AND url=?", (item_id, url)).fetchone()["v"] or 0
    r = extract(path); m = r["meta"]
    did = c.execute("INSERT INTO documents(item_id,url,sha256,path,version,pages,status,pdf_title,pdf_created,pdf_modified,fetched_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (item_id, url, sha256, path, prev + 1, r["pages"], r["status"], m.get("title"), m.get("created"), m.get("modified"), now)).lastrowid
    for p in r["paragraphs"]:
        c.execute("INSERT INTO paragraphs(document_id,seq,page,page_end,para_ref,kind,section,text) VALUES(?,?,?,?,?,?,?,?)",
                  (did, p["seq"], p["page"], p["page_end"], p["ref"], p["kind"], p["section"], p["text"]))
    return did, r["status"], prev > 0
