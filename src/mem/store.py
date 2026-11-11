"""SQLite WAL + FTS5 indeksi, (mtime_ns, size) hizli artimli senkronizasyon, olay kutugu."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .frontmatter import parse_markdown
from .turkish import index_tokens

SCHEMA_USER_VERSION = 1
PASSAGE_TARGET = 900
_SKIP_DIRS = {".obsidian", ".git", "node_modules"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    kind TEXT NOT NULL,
    project TEXT,
    status TEXT NOT NULL,
    trust TEXT NOT NULL,
    mtime_ns INTEGER NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    created_at TEXT,
    updated_at TEXT,
    frontmatter_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS passages (
    passage_id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    ord INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    heading_path TEXT NOT NULL,
    body TEXT NOT NULL,
    shadow_terms TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_passages_doc ON passages(doc_id);
CREATE VIRTUAL TABLE IF NOT EXISTS passages_fts USING fts5(
    title, heading_path, body, shadow_terms,
    content='passages', content_rowid='passage_id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS passages_ai AFTER INSERT ON passages BEGIN
    INSERT INTO passages_fts(rowid, title, heading_path, body, shadow_terms)
    VALUES (new.passage_id, new.title, new.heading_path, new.body, new.shadow_terms);
END;
CREATE TRIGGER IF NOT EXISTS passages_ad AFTER DELETE ON passages BEGIN
    INSERT INTO passages_fts(passages_fts, rowid, title, heading_path, body, shadow_terms)
    VALUES ('delete', old.passage_id, old.title, old.heading_path, old.body, old.shadow_terms);
END;
CREATE TABLE IF NOT EXISTS links (
    src_doc_id TEXT NOT NULL,
    dst_doc_id TEXT NOT NULL,
    PRIMARY KEY (src_doc_id, dst_doc_id)
);
CREATE INDEX IF NOT EXISTS idx_links_dst ON links(dst_doc_id);
CREATE TABLE IF NOT EXISTS supersedes (
    newer_doc_id TEXT NOT NULL,
    older_doc_id TEXT NOT NULL,
    PRIMARY KEY (newer_doc_id, older_doc_id)
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    action TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    sha256 TEXT,
    actor TEXT,
    detail_json TEXT
);
"""


def fts5_available() -> bool:
    try:
        con = sqlite3.connect(":memory:")
        try:
            con.execute("CREATE VIRTUAL TABLE t USING fts5(a)")
            return True
        finally:
            con.close()
    except sqlite3.Error:
        return False


def connect(state_dir) -> sqlite3.Connection:
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(state_dir / "index.sqlite3"), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_USER_VERSION:
        conn.executescript(_SCHEMA)
        conn.execute(f"PRAGMA user_version={SCHEMA_USER_VERSION}")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def log_event(conn, action: str, doc_id: str, sha256: str | None = None, actor: str | None = None,
              detail: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO events(ts, action, doc_id, sha256, actor, detail_json) VALUES (?,?,?,?,?,?)",
        (_now(), action, doc_id, sha256, actor, json.dumps(detail, ensure_ascii=False) if detail else None),
    )


# ---------------------------------------------------------------- siniflandirma


def classify(doc_id: str, meta: dict) -> tuple[str, str | None, str, str]:
    """(kind, project, status, trust)."""
    parts = doc_id.split("/")
    name = parts[-1]
    kind = None
    if "kararlar" in parts[:-1]:
        kind = "decision"
    elif parts[0] == "receipts":
        kind = "receipt"
    elif parts[0] == "knowledge":
        kind = "knowledge"
    elif name == "Genel-Bakis.md":
        kind = "overview"
    elif name == "Kurallar.md":
        kind = "rule"
    elif name == "Acik-Konular.md":
        kind = "threads"
    elif name == "Son-Oturum.md":
        kind = "session"
    elif name == "Core.md":
        kind = "knowledge"
    declared = meta.get("kind")
    if isinstance(declared, str) and declared in (
        "overview", "rule", "decision", "threads", "session", "knowledge", "receipt", "note",
    ):
        kind = declared if kind is None else kind
    kind = kind or "note"

    project = None
    if len(parts) >= 3 and parts[0] == "projects":
        project = parts[1]
    elif isinstance(meta.get("project"), str) and meta.get("project"):
        project = meta["project"]

    status = meta.get("status")
    if status not in ("active", "superseded", "archived", "draft"):
        status = "active"
    if parts[0] == "archive":
        status = "archived"

    trust = meta.get("trust")
    if trust not in ("trusted", "untrusted"):
        trust = "trusted"
    return kind, project, status, trust


# ---------------------------------------------------------------- parcalama

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE_RE = re.compile(r"^[ \t]*(```|~~~)")


def _blocks(lines: list[str]) -> list[str]:
    """Bos satirlarla paragraflara boler; kod citleri tek blok kalir."""
    blocks: list[str] = []
    cur: list[str] = []
    in_fence = False
    for ln in lines:
        if _FENCE_RE.match(ln):
            if not in_fence and cur and not cur[-1].strip() == "":
                pass
            in_fence = not in_fence
            cur.append(ln)
            continue
        if not in_fence and not ln.strip():
            if cur:
                blocks.append("\n".join(cur))
                cur = []
            continue
        cur.append(ln)
    if cur:
        blocks.append("\n".join(cur))
    return blocks


def split_passages(body: str, title: str = "") -> list[tuple[str, str]]:
    """Baslik hiyerarsisini koruyan ~900 karakterlik bloklar: [(heading_path, text)]."""
    sections: list[tuple[str, list[str]]] = []
    stack: list[tuple[int, str]] = []
    cur_lines: list[str] = []
    cur_path = title
    in_fence = False
    for ln in body.split("\n"):
        if _FENCE_RE.match(ln):
            in_fence = not in_fence
            cur_lines.append(ln)
            continue
        m = None if in_fence else _HEADING_RE.match(ln)
        if m:
            sections.append((cur_path, cur_lines))
            level = len(m.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, m.group(2).strip()))
            cur_path = " > ".join(h for _, h in stack)
            cur_lines = []
        else:
            cur_lines.append(ln)
    sections.append((cur_path, cur_lines))

    passages: list[tuple[str, str]] = []
    for path, lines in sections:
        buf = ""
        for block in _blocks(lines):
            if buf and len(buf) + len(block) + 2 > PASSAGE_TARGET:
                passages.append((path, buf))
                buf = ""
            buf = block if not buf else buf + "\n\n" + block
        if buf.strip():
            passages.append((path, buf))
    if not passages:
        passages.append((title, title or ""))
    return passages


# ---------------------------------------------------------------- baglantilar

_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")


def norm_link_target(target: str) -> str:
    t = target.strip().replace("\\", "/")
    while t.startswith("./"):
        t = t[2:]
    t = t.lstrip("/")
    if t.lower().endswith(".md"):
        t = t[:-3]
    return t + ".md"


def extract_links(text: str) -> list[str]:
    seen: list[str] = []
    for m in _WIKILINK_RE.finditer(text):
        t = norm_link_target(m.group(1))
        if t not in seen:
            seen.append(t)
    return seen


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v).strip()]
    return []


# ---------------------------------------------------------------- senkronizasyon


def _inside(root_real: str, path: str) -> bool:
    real = os.path.normcase(os.path.realpath(path))
    base = os.path.normcase(root_real).rstrip("\\/")
    return real == base or real.startswith(base + os.sep)


def iter_markdown(root: Path):
    """Kasa icindeki .md dosyalari; symlink dosyalari ve kasa disina cikan klasorler (junction dahil) atlanir."""
    root_real = os.path.realpath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in _SKIP_DIRS and not d.startswith(".mem-") and _inside(root_real, os.path.join(dirpath, d))
        ]
        for fn in filenames:
            if not fn.lower().endswith(".md") or fn.startswith(".mem-"):
                continue
            full = Path(dirpath) / fn
            if os.path.islink(full):
                continue
            try:
                st = full.stat()
            except OSError:
                continue
            rel = full.relative_to(root).as_posix()
            yield rel, full, st


def _delete_doc(conn, doc_id: str) -> None:
    conn.execute("DELETE FROM passages WHERE doc_id=?", (doc_id,))
    conn.execute("DELETE FROM links WHERE src_doc_id=?", (doc_id,))
    conn.execute("DELETE FROM supersedes WHERE newer_doc_id=?", (doc_id,))
    conn.execute("DELETE FROM documents WHERE doc_id=?", (doc_id,))


def _first_heading(body: str) -> str:
    for ln in body.split("\n"):
        m = _HEADING_RE.match(ln)
        if m:
            return m.group(2).strip()
    return ""


def _index_doc(conn, doc_id: str, data: bytes, st) -> tuple[list[str], list[str]]:
    text = data.decode("utf-8", errors="replace")
    sha = hashlib.sha256(data).hexdigest()
    meta, body = parse_markdown(text)
    title = str(meta.get("title") or _first_heading(body) or Path(doc_id).stem)
    kind, project, status, trust = classify(doc_id, meta)
    created = str(meta.get("created_at") or meta.get("created") or meta.get("date") or "")
    updated = str(meta.get("updated_at") or "") or datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")
    _delete_doc(conn, doc_id)
    conn.execute(
        "INSERT INTO documents(doc_id,title,kind,project,status,trust,mtime_ns,size_bytes,sha256,"
        "created_at,updated_at,frontmatter_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (doc_id, title, kind, project, status, trust, st.st_mtime_ns, st.st_size, sha,
         created, updated, json.dumps(meta, ensure_ascii=False, default=str)),
    )
    for ord_, (heading, ptext) in enumerate(split_passages(body, title)):
        shadow = index_tokens(f"{title} {heading} {ptext}")
        conn.execute(
            "INSERT INTO passages(doc_id, ord, title, heading_path, body, shadow_terms) VALUES (?,?,?,?,?,?)",
            (doc_id, ord_, title, heading, ptext, shadow),
        )
    return extract_links(body), [norm_link_target(s) for s in _as_list(meta.get("supersedes"))]


def _resolver(conn):
    ids = {r[0] for r in conn.execute("SELECT doc_id FROM documents")}
    by_stem: dict[str, list[str]] = {}
    for i in ids:
        by_stem.setdefault(Path(i).stem.lower(), []).append(i)

    def resolve(target: str) -> str:
        if target in ids:
            return target
        low = {i.lower(): i for i in ids}
        if target.lower() in low:
            return low[target.lower()]
        if "/" not in target:
            cands = by_stem.get(Path(target).stem.lower(), [])
            if len(cands) == 1:
                return cands[0]
        return target

    return resolve


def sync_vault(conn, vault_root) -> dict:
    """Degisen/yeni dosyalari indeksler. (mtime_ns,size) ayniysa dosya diskten OKUNMAZ."""
    root = Path(vault_root)
    stats = {"added": 0, "updated": 0, "removed": 0, "unchanged": 0, "read_count": 0}
    existing = {
        r["doc_id"]: (r["mtime_ns"], r["size_bytes"])
        for r in conn.execute("SELECT doc_id, mtime_ns, size_bytes FROM documents")
    }
    seen: set[str] = set()
    changed: list[tuple[str, Path, os.stat_result]] = []
    root_real = os.path.realpath(root)
    for rel, full, st in iter_markdown(root):
        prev = existing.get(rel)
        if prev == (st.st_mtime_ns, st.st_size):
            seen.add(rel)
            stats["unchanged"] += 1
        elif _inside(root_real, str(full)):  # degisen dosyalar icin realpath dogrulamasi
            seen.add(rel)
            changed.append((rel, full, st))
    removed = [d for d in existing if d not in seen]
    if not changed and not removed:
        return stats
    with transaction(conn):
        for d in removed:
            _delete_doc(conn, d)
            log_event(conn, "remove", d, actor="sync")
            stats["removed"] += 1
        pending: list[tuple[str, list[str], list[str]]] = []
        for rel, full, st in changed:
            try:
                data = full.read_bytes()
            except OSError:
                continue
            stats["read_count"] += 1
            links, sups = _index_doc(conn, rel, data, st)
            pending.append((rel, links, sups))
            stats["updated" if rel in existing else "added"] += 1
            log_event(conn, "index", rel, hashlib.sha256(data).hexdigest(), "sync")
        resolve = _resolver(conn)
        for rel, links, sups in pending:
            for dst in links:
                d = resolve(dst)
                if d != rel:
                    conn.execute("INSERT OR IGNORE INTO links(src_doc_id,dst_doc_id) VALUES (?,?)", (rel, d))
            for old in sups:
                o = resolve(old)
                if o != rel:
                    conn.execute(
                        "INSERT OR IGNORE INTO supersedes(newer_doc_id, older_doc_id) VALUES (?,?)", (rel, o)
                    )
        if removed:
            conn.execute("DELETE FROM links WHERE src_doc_id NOT IN (SELECT doc_id FROM documents)")
            conn.execute("DELETE FROM supersedes WHERE newer_doc_id NOT IN (SELECT doc_id FROM documents)")
    return stats


def get_doc(conn, doc_id: str):
    return conn.execute("SELECT * FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
