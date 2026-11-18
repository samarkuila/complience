import sqlite3, pathlib, datetime as dt
DB = pathlib.Path("data/crawler.db")
SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(id INTEGER PRIMARY KEY, regulator TEXT, name TEXT UNIQUE, url TEXT, selector TEXT DEFAULT '',
  render_js INTEGER DEFAULT 0, keywords TEXT DEFAULT '', active INTEGER DEFAULT 1, last_success TEXT, status TEXT DEFAULT 'not crawled');
CREATE TABLE IF NOT EXISTS crawls(id INTEGER PRIMARY KEY, source_id INTEGER, cycle TEXT, started TEXT, finished TEXT,
  status TEXT, n_items INTEGER DEFAULT 0, n_new INTEGER DEFAULT 0, error TEXT, snapshot_path TEXT);
CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY, source_id INTEGER, url TEXT, title TEXT, shown_date TEXT,
  first_seen TEXT, last_seen TEXT, detected_at TEXT, change_type TEXT, nbfc_flag TEXT, nbfc_score INTEGER DEFAULT 0, nbfc_reason TEXT,
  pdf_path TEXT, meta_json TEXT, review_status TEXT DEFAULT 'pending', reviewer TEXT, remarks TEXT, reviewed_at TEXT, crawl_id INTEGER,
  UNIQUE(source_id,url));
CREATE TABLE IF NOT EXISTS exceptions(id INTEGER PRIMARY KEY, ts TEXT, source_id INTEGER, stage TEXT, message TEXT, resolved INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS documents(id INTEGER PRIMARY KEY, item_id INTEGER, url TEXT, sha256 TEXT, path TEXT, version INTEGER DEFAULT 1,
  pages INTEGER, status TEXT, pdf_title TEXT, pdf_created TEXT, pdf_modified TEXT, fetched_at TEXT, UNIQUE(item_id,url,sha256));
CREATE TABLE IF NOT EXISTS paragraphs(id INTEGER PRIMARY KEY, document_id INTEGER, seq INTEGER, page INTEGER, page_end INTEGER,
  para_ref TEXT, kind TEXT, section TEXT, text TEXT, UNIQUE(document_id,seq));
CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY, name TEXT UNIQUE, day TEXT, cycle TEXT, status TEXT DEFAULT 'draft',
  generated_at TEXT, n_items INTEGER, approved_by TEXT, approved_at TEXT, remarks TEXT);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, ts TEXT, actor TEXT, action TEXT, detail TEXT);
"""
def now(): return dt.datetime.now().isoformat(timespec="seconds")
def conn():
    DB.parent.mkdir(exist_ok=True)
    c = sqlite3.connect(DB, timeout=30); c.row_factory = sqlite3.Row; return c
def audit(c, actor, action, detail=""):
    c.execute("INSERT INTO audit(ts,actor,action,detail) VALUES(?,?,?,?)", (now(), actor, action, detail))
def log_exc(c, sid, stage, msg):
    c.execute("INSERT INTO exceptions(ts,source_id,stage,message) VALUES(?,?,?,?)", (now(), sid, stage, str(msg)[:500]))
def init():
    c = conn(); c.executescript(SCHEMA)
    c.commit(); c.close()
