"""Kasa operasyonlari: init, proje kaydi, bootstrap, decide (ADR), rule, patch, finish, okuma/arama."""

from __future__ import annotations

import difflib
import json
import os
import posixpath
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol, runtime_checkable

from . import config, dlp, store
from .config import VAULT_SCOPE
from .frontmatter import dump_markdown, parse_markdown
from .lock import (
    ConcurrencyConflictError,
    PathTraversalError,
    VaultLock,
    atomic_write_text,
    resolve_in_vault,
    sha256_bytes,
)
from .search import search_vault
from .turkish import fold_tr

__all__ = [
    "DecisionAdvisor",
    "VaultError",
    "ConcurrencyConflictError",
    "PathTraversalError",
    "init_vault",
    "register_project",
    "refresh_project_links",
    "resolve_scope",
    "bootstrap_context",
    "record_decision",
    "add_rule",
    "patch_note",
    "finish_session",
    "compact_receipts",
    "read_note",
    "search_memory",
    "sync_index",
    "slugify",
]

UNREGISTERED_MESSAGE = "Bu çalışma alanı hafıza beyaz listesinde değil."
SESSION_LIMIT = 6000
SESSION_MAX_CARDS = 5
SESSION_MIN_CARDS = 3
MIN_HANDOFF_CHARS = 1200
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
LINKS_START = "<!-- mem-links -->"
LINKS_END = "<!-- /mem-links -->"


class VaultError(ValueError):
    """Kasa islemi gecersiz."""


@runtime_checkable
class DecisionAdvisor(Protocol):
    """Opsiyonel harici karar danismani kancasi (yalniz personal/non-block profillerde baglanir).

    `warnings` dolu bir sozluk donerse karar yazilmaz (advisor_warning); `None` her zaman guvenlidir.
    """

    def assess_decision(self, proposal: dict, priors: list[dict]) -> dict | None: ...


ADVISOR_PRIOR_LIMIT = 8


def _gate_external(ctx: "_Ctx", reranker):
    """Savunma derinligi: work/block kasada harici (is_external) reranker asla kullanilmaz."""
    if reranker is not None and getattr(reranker, "is_external", False) \
            and not config.can_use_advisor(ctx.profile, ctx.dlp_mode):
        return None
    return reranker


def _now() -> datetime:
    """Testlerde monkeypatch ile sabitlenebilir."""
    return datetime.now()


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def slugify(text: str, maxlen: int = 60) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", fold_tr(text)).strip("-")
    return s[:maxlen].strip("-") or "kayit"


# ---------------------------------------------------------------- baglam / oturum


@dataclass
class _Ctx:
    vault_root: Path
    state_dir: Path
    dlp_mode: str
    profile: str


def _ctx(vault_root, state_dir=None) -> _Ctx:
    root = Path(os.path.abspath(str(vault_root)))
    meta = config.read_vault_meta(root)
    sdir = config.resolve_state_dir(root, state_dir)
    mode = meta.get("dlp_mode") or config.default_dlp_mode(meta.get("profile", ""))
    if mode not in config.DLP_MODES:
        raise VaultError(f"Gecersiz dlp_mode: {mode!r}")
    return _Ctx(root, sdir, mode, meta.get("profile") or "default")


class _Session:
    """Kilit + veritabani baglantisi; yazma yardimcilari. Ic ice kullanilmaz."""

    def __init__(self, ctx: _Ctx, actor: str = "mem"):
        self.ctx = ctx
        self.actor = actor
        self._lock = VaultLock(ctx.state_dir / "writer.lock")
        self.conn = None

    def __enter__(self):
        self._lock.acquire()
        try:
            self.conn = store.connect(self.ctx.state_dir)
        except BaseException:
            self._lock.release()
            raise
        return self

    def __exit__(self, *exc):
        try:
            if self.conn is not None:
                self.conn.close()
        finally:
            self._lock.release()
        return False

    def path(self, doc_id: str) -> Path:
        return resolve_in_vault(self.ctx.vault_root, doc_id)

    def exists(self, doc_id: str) -> bool:
        return self.path(doc_id).exists()

    def read(self, doc_id: str) -> str:
        return self.path(doc_id).read_text(encoding="utf-8").replace("\r\n", "\n")

    def read_bytes(self, doc_id: str) -> bytes:
        return self.path(doc_id).read_bytes()

    def write(self, doc_id: str, text: str, action: str, detail: dict | None = None) -> str:
        backup = self.ctx.state_dir / "backups" / doc_id.replace("/", "__")
        sha = atomic_write_text(self.path(doc_id), text, backup_dir=backup)
        store.log_event(self.conn, action, doc_id, sha, self.actor, detail)
        return sha

    def sync(self) -> dict:
        return store.sync_vault(self.conn, self.ctx.vault_root)

    def shield(self, text: str) -> tuple[str, list[str]]:
        cleaned, findings = dlp.inspect_and_apply(text, self.ctx.dlp_mode, self.ctx.vault_root)
        return cleaned, sorted({f["category"] for f in findings})


def _shield_all(sess: _Session, values: list[str]) -> tuple[list[str], list[str]]:
    cats: set[str] = set()
    out = []
    for v in values:
        cleaned, c = sess.shield(v)
        cats.update(c)
        out.append(cleaned)
    return out, sorted(cats)


def _meta_doc(meta: dict, body: str) -> str:
    return dump_markdown(meta, body)


def _touch(meta: dict, now: datetime) -> dict:
    meta["updated_at"] = _iso(now)
    return meta


# ---------------------------------------------------------------- init


_CORE_BODY = """# Core — Mühendislik Hafızası Çekirdeği

Bu kasa `mem` motoru tarafından yönetilen ikinci beyindir. Oturum başında ajanlar
`memory_bootstrap` ile bu dosyayı ve kuralları okur.

## Çalışma ilkeleri

- Kararlar `projects/<proje>/kararlar/` altında ADR olarak tutulur.
- Hassas veri (sır, anahtar, kişisel veri) hafızaya yazılmaz; DLP kalkanı bunu zorlar.
- Hafıza beyaz listesinde olmayan çalışma alanlarına hiçbir şey yazılmaz/okunmaz.

Bağlantılar: [[Kurallar|Genel kurallar]]
"""

_OBSIDIAN_GRAPH = {
    "collapse-filter": False,
    "search": "-path:receipts -path:archive",
    "showTags": False,
    "showAttachments": False,
    "hideUnresolved": True,
    "showOrphans": True,
    "collapse-color-groups": False,
    "colorGroups": [
        {"query": "file:Core OR path:Kurallar.md", "color": {"a": 1, "rgb": 15381256}},
        {"query": "path:projects -path:kararlar -file:Kurallar -file:Acik-Konular -file:Son-Oturum -file:Karar-Dizini",
         "color": {"a": 1, "rgb": 3899638}},
        {"query": "path:kararlar", "color": {"a": 1, "rgb": 1096065}},
        {"query": "file:Kurallar path:projects", "color": {"a": 1, "rgb": 16347926}},
        {"query": "path:knowledge", "color": {"a": 1, "rgb": 11032055}},
    ],
}

_DLP_PATTERNS = """# Şirkete özel DLP desenleri (her satır bir desen; # yorum satırıdır)
# Düz metin: büyük/küçük harf duyarsız eşleşir. Örnekler:
#   .corp.sirket.com
#   10.42.
# Regex için `regex:` öneki kullanın (en çok 200 karakter, iç içe nicelik yok):
#   regex:PRJ-\\d{4,}
"""


def init_vault(vault_root, profile_name: str, dlp_mode: str | None = None, state_dir=None,
               advisor: str | None = None) -> dict:
    if not config.PROFILE_NAME_RE.match(profile_name or ""):
        raise VaultError(f"Gecersiz profil adi: {profile_name!r}")
    root = Path(os.path.abspath(str(vault_root)))
    mode = dlp_mode or config.default_dlp_mode(profile_name)
    if mode not in config.DLP_MODES:
        raise VaultError(f"Gecersiz dlp_mode: {mode!r}")
    adv = config.validate_advisor_mode(advisor, profile_name, mode)
    created: list[str] = []
    root.mkdir(parents=True, exist_ok=True)
    meta_path = config.vault_meta_path(root)
    if not meta_path.exists():
        sdir_check = Path(state_dir) if state_dir else config.default_state_dir(profile_name)
        config.ensure_state_outside(root, sdir_check)
        atomic_write_text(meta_path, json.dumps(config.new_vault_meta(profile_name, mode, adv), indent=2) + "\n")
        created.append(".mem-vault.json")
    ctx = _ctx(root, state_dir)
    if not config.projects_file(root).exists():
        config.save_projects(root, [])
        created.append("projects.json")
    now = _iso(_now())
    pats = root / "dlp-patterns.txt"
    if not pats.exists():
        atomic_write_text(pats, _DLP_PATTERNS)
        created.append("dlp-patterns.txt")
    for d in ("projects", "knowledge", "receipts", "archive"):
        (root / d).mkdir(exist_ok=True)
    obs_graph = root / ".obsidian" / "graph.json"
    if not obs_graph.exists():
        obs_graph.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(obs_graph, json.dumps(_OBSIDIAN_GRAPH, ensure_ascii=False, indent=2) + "\n")
    templates = {
        "Core.md": ({"title": "Core", "kind": "knowledge", "status": "active", "created_at": now}, _CORE_BODY),
        "Kurallar.md": (
            {"title": "Genel Kurallar", "kind": "rule", "status": "active", "created_at": now},
            "# Genel Kurallar\n\n",
        ),
    }
    with _Session(ctx) as sess:
        for name, (meta, body) in templates.items():
            if not sess.exists(name):
                sess.write(name, _meta_doc(meta, body), "init")
                created.append(name)
        sess.sync()
        store.log_event(sess.conn, "init_vault", ".mem-vault.json", None, sess.actor, {"profile": profile_name})
    return {
        "vault_root": str(root),
        "state_dir": str(ctx.state_dir),
        "profile": profile_name,
        "dlp_mode": ctx.dlp_mode,
        "created": created,
    }


# ---------------------------------------------------------------- proje kaydi


def hub_filename(name: str, slug: str = "") -> str:
    """Obsidian grafik dugumunde proje adinin gorunmesi icin merkez dosyasi `<ProjeAdi>.md` olarak adlandirilir."""
    raw = (name or slug or "Genel-Bakis").strip()
    cleaned = re.sub(r'[<>:"/\\|?*#\^\[\]\x00-\x1f]+', "-", raw).strip(". ")
    if not cleaned or cleaned.lower() in ("kurallar", "acik-konular", "son-oturum", "kararlar"):
        cleaned = f"{slug or 'proje'}-genel"
    return f"{cleaned}.md"


def project_hub_doc_id(vault_root, slug: str, name: str | None = None) -> str:
    if name is None:
        proj = config.get_project(vault_root, slug) or {}
        name = proj.get("name") or slug
    preferred = f"projects/{slug}/{hub_filename(name, slug)}"
    legacy = f"projects/{slug}/Genel-Bakis.md"
    vroot = Path(vault_root)
    if not (vroot / preferred).exists() and (vroot / legacy).exists():
        return legacy
    return preferred


def _project_files(slug: str, name: str, description: str, now: str) -> dict[str, str]:
    base = f"projects/{slug}"
    hub_file = hub_filename(name, slug)
    desc = description.strip() or "(açıklama yok)"
    return {
        f"{base}/{hub_file}": _meta_doc(
            {"title": f"{name} genel bakış", "kind": "overview", "project": slug, "status": "active",
             "created_at": now},
            f"# {name} — Genel Bakış\n\n{desc}\n\n## Bağlantılar\n\n{LINKS_START}\n{LINKS_END}\n",
        ),
        f"{base}/Kurallar.md": _meta_doc(
            {"title": f"{name} kuralları", "kind": "rule", "project": slug, "status": "active",
             "created_at": now},
            f"# {name} — Kurallar\n\nProje: [[{base}/{hub_file[:-3]}|{name} genel bakış]]\n",
        ),
        f"{base}/Acik-Konular.md": _meta_doc(
            {"title": f"{name} açık konular", "kind": "threads", "project": slug, "status": "active",
             "created_at": now},
            f"# {name} — Açık Konular\n\n(açık konu yok)\n",
        ),
        f"{base}/Son-Oturum.md": _meta_doc(
            {"title": f"{name} son oturum", "kind": "session", "project": slug, "status": "active",
             "created_at": now},
            f"# {name} — Son Oturum\n\n",
        ),
    }


def _norm_root(root) -> str:
    p = os.path.abspath(str(root)).replace("\\", "/")
    if re.match(r"^([A-Za-z]:)?/?$", p):
        raise VaultError(f"Disk kökü proje kökü olamaz: {root}")
    return p.rstrip("/")


def _normalize_tags(tags: list[str] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for t in tags or []:
        s = (t or "").strip()
        if not s:
            continue
        norm = slugify(s, 24)
        if norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def register_project(vault_root, slug: str, repo_roots, name: str | None = None,
                     description: str = "", state_dir=None, tags: list[str] | None = None) -> dict:
    if not SLUG_RE.match(slug or ""):
        raise VaultError("Proje slug'i yalnız küçük harf, rakam ve tire içerebilir (örn. order-service).")
    if isinstance(repo_roots, (str, Path)):
        repo_roots = [repo_roots]
    roots = [_norm_root(r) for r in repo_roots]
    if not roots:
        raise VaultError("En az bir repo kökü gerekli.")
    norm_tags = _normalize_tags(tags) if tags is not None else None
    ctx = _ctx(vault_root, state_dir)
    now = _now()
    with _Session(ctx) as sess:
        desc, _ = _shield_all(sess, [description])
        desc = desc[0]
        projects = config.load_projects(ctx.vault_root)
        existing = next((p for p in projects if p["slug"] == slug), None)
        if existing is None:
            entry = {"slug": slug, "name": name or slug, "roots": roots, "description": desc}
            if norm_tags is not None:
                entry["tags"] = norm_tags
            projects.append(entry)
            action = "created"
        else:
            entry = existing
            for r in roots:
                if r.lower() not in [x.lower() for x in entry.get("roots", [])]:
                    entry.setdefault("roots", []).append(r)
            if name:
                entry["name"] = name
            if desc:
                entry["description"] = desc
            if norm_tags is not None:
                entry["tags"] = norm_tags
            action = "updated"
        config.save_projects(ctx.vault_root, projects)
        store.log_event(sess.conn, "project_add", "projects.json", None, sess.actor,
                        {"slug": slug, "roots": entry["roots"]})
        pname = entry["name"]
        for doc_id, text in _project_files(slug, pname, entry.get("description", ""), _iso(now)).items():
            if not sess.exists(doc_id):
                sess.write(doc_id, text, "project_init")
        (ctx.vault_root / "projects" / slug / "kararlar").mkdir(parents=True, exist_ok=True)
        pointer = f"- [kapsam: {slug}] [[projects/{slug}/Kurallar|{pname} kuralları]]"
        rules = sess.read("Kurallar.md")
        if f"[[projects/{slug}/Kurallar|" not in rules:
            sess.write("Kurallar.md", rules.rstrip("\n") + "\n" + pointer + "\n", "rule_pointer")
        sess.sync()
        _refresh_hub(sess, slug)
        sess.sync()
    return {"status": action, "slug": slug, "name": pname, "roots": entry["roots"], "tags": entry.get("tags", [])}


# ---------------------------------------------------------------- hub


def _decision_entries(sess: _Session, slug: str) -> list[dict]:
    d = sess.path(f"projects/{slug}/kararlar")
    out = []
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.md"), reverse=True):
        meta, _ = parse_markdown(f.read_text(encoding="utf-8", errors="replace"))
        out.append(
            {
                "doc_id": f"projects/{slug}/kararlar/{f.name}",
                "title": str(meta.get("title") or f.stem),
                "status": str(meta.get("status") or "active"),
                "date": str(meta.get("date") or f.name[:10]),
            }
        )
    return out


def _hub_block(sess: _Session, slug: str) -> str:
    lines = [
        LINKS_START,
        f"- [[projects/{slug}/Kurallar|Kurallar]]",
        f"- [[projects/{slug}/Acik-Konular|Açık konular]]",
        f"- [[projects/{slug}/Son-Oturum|Son oturum]]",
    ]
    entries = _decision_entries(sess, slug)
    if entries:
        lines.append("- Kararlar:")
        for e in entries:
            mark = " · yerine geçildi" if e["status"] == "superseded" else ""
            title = e["title"].replace("|", "/").replace("]", ")").replace("[", "(")
            lines.append(f"  - [[{e['doc_id'][:-3]}|{title}]] — {e['date']}{mark}")
    lines.append(LINKS_END)
    return "\n".join(lines)


def _refresh_hub(sess: _Session, slug: str) -> bool:
    doc_id = project_hub_doc_id(sess.ctx.vault_root, slug)
    if not sess.exists(doc_id):
        return False
    text = sess.read(doc_id)
    block = _hub_block(sess, slug)
    if LINKS_START in text and LINKS_END in text:
        pre, rest = text.split(LINKS_START, 1)
        _, post = rest.split(LINKS_END, 1)
        new = pre + block + post
    else:
        new = text.rstrip("\n") + "\n\n## Bağlantılar\n\n" + block + "\n"
    if new == text:
        return False
    sess.write(doc_id, new, "hub_refresh", {"project": slug})
    return True


def refresh_project_links(vault_root, slug: str, state_dir=None) -> bool:
    ctx = _ctx(vault_root, state_dir)
    if config.get_project(ctx.vault_root, slug) is None:
        raise VaultError(f"Kayıtsız proje: {slug}")
    with _Session(ctx) as sess:
        changed = _refresh_hub(sess, slug)
        sess.sync()
    return changed


# ---------------------------------------------------------------- kapsam


def resolve_scope(vault_root, cwd=None, project: str | None = None) -> dict:
    """Kapsam cozumu.

    * `cwd` verilmisse kasa icinde ya da kayitli bir projenin `roots` icinde olmak ZORUNDA; aksi halde
      `project` ne olursa olsun `unregistered_workspace` doner (atlanamaz).
    * `project` yalniz kayitli slug olabilir; `global`/`_vault` ile kapsam genisletilemez.
    * cwd bir projeye aitse baska projenin slug'i istenemez (capraz proje reddedilir).
    * cwd yok + project yok -> kasa kapsami (yalniz yerel CLI/kutuphane kullanimi).
    """
    root = Path(os.path.abspath(str(vault_root)))
    if project in ("global", VAULT_SCOPE, "shared"):
        raise VaultError(
            f"'{project}' proje olarak verilemez; kapsam genişletilemez. "
            "Genel kural için scope='global', paylaşılan karar için shared=true kullan."
        )
    cwd_proj = None
    if cwd:
        cwd_proj = config.resolve_project(root, cwd)
        if cwd_proj is None:
            return {"status": "unregistered_workspace", "cwd": str(cwd), "message": UNREGISTERED_MESSAGE}
    if project:
        proj = config.get_project(root, project)
        if proj is None:
            raise VaultError(f"Bilinmeyen proje: {project!r}. Önce `mem project add` ile kaydet.")
        if cwd_proj is not None and cwd_proj["slug"] not in (project, VAULT_SCOPE):
            raise VaultError(
                f"cwd '{cwd_proj['slug']}' projesine ait; istenen proje '{project}'. Çapraz proje erişimi reddedildi."
            )
        return {"status": "ok", "project": project, "name": proj.get("name", project), "scope": "project"}
    if cwd_proj is None or cwd_proj["slug"] == VAULT_SCOPE:
        return {"status": "ok", "project": None, "name": None, "scope": "vault"}
    return {"status": "ok", "project": cwd_proj["slug"], "name": cwd_proj.get("name", cwd_proj["slug"]),
            "scope": "project"}


# ---------------------------------------------------------------- okuma / arama


def sync_index(vault_root, state_dir=None) -> dict:
    ctx = _ctx(vault_root, state_dir)
    conn = store.connect(ctx.state_dir)
    try:
        return store.sync_vault(conn, ctx.vault_root)
    finally:
        conn.close()


def search_memory(vault_root, query: str, project: str | None = None, limit: int = 8,
                  include_superseded: bool = False, include_archived: bool = False,
                  kind: str | None = None, kinds: list[str] | None = None,
                  state_dir=None, reranker=None) -> list[dict]:
    ctx = _ctx(vault_root, state_dir)
    klist = list(kinds) if kinds else ([kind] if kind else None)
    conn = store.connect(ctx.state_dir)
    try:
        store.sync_vault(conn, ctx.vault_root)
        return search_vault(conn, query, project=project, limit=limit,
                            include_superseded=include_superseded,
                            include_archived=include_archived, kinds=klist,
                            reranker=_gate_external(ctx, reranker))
    finally:
        conn.close()


def _check_note_path(path: str) -> str:
    p = str(path).replace("\\", "/").lstrip("/")
    if not p.lower().endswith(".md"):
        p += ".md"
    parts = p.split("/")
    if any(x.startswith(".") for x in parts):
        raise PathTraversalError("Gizli/dahili yollara erişim yok.")
    return p


def _doc_project(doc_id: str, meta: dict) -> str | None:
    parts = doc_id.split("/")
    if len(parts) >= 3 and parts[0] == "projects":
        return parts[1]
    p = meta.get("project")
    return p if isinstance(p, str) and p else None


def _assert_access(doc_id: str, meta: dict, project: str | None) -> None:
    """Proje kapsaminda baska projenin notuna ve (tum projelerin kartlarini tasiyan) arsive erisim yok."""
    if not project:
        return
    if doc_id.startswith("archive/"):
        raise VaultError("Arşiv notları proje kapsamında okunamaz/değiştirilemez (kasa kapsamı gerekir).")
    owner = _doc_project(doc_id, meta)
    if owner and owner != project:
        raise VaultError(f"Bu not başka bir projeye ait ({owner}); erişim reddedildi.")


def read_note(vault_root, path: str, project: str | None = None, state_dir=None) -> dict:
    """Not okur; `project` verilmisse baska projenin notunu reddeder."""
    root = Path(os.path.abspath(str(vault_root)))
    doc_id = _check_note_path(path)
    full = resolve_in_vault(root, doc_id)
    if not full.is_file():
        raise VaultError(f"Not bulunamadı: {doc_id}")
    raw = full.read_bytes()
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
    meta, body = parse_markdown(text)
    _assert_access(doc_id, meta, project)
    return {"path": doc_id, "sha256": sha256_bytes(raw), "frontmatter": meta, "body": body, "content": text}


# ---------------------------------------------------------------- bootstrap

_POINTER_RE = re.compile(r"^\s*- \[kapsam: ([^\]]+)\]")
_RULE_LINE_RE = re.compile(
    r"^\s*-\s+\*\*\[[^\]]*\]\*\*\s+(?:\{tags:\s*([^}]*)\}\s+)?(.*?)(?:\s+\*\(Gerekçe:.*\)\*)?\s*$"
)


def _filter_global_rules(grules: str, root: Path, slug: str | None) -> str:
    if slug is None:
        return grules
    proj_info = config.get_project(root, slug) or {}
    proj_tags = {slugify(t, 24) for t in (proj_info.get("tags") or []) if t} | {slugify(slug, 24)}
    kept: list[str] = []
    hidden_rules = 0
    for ln in grules.split("\n"):
        pm = _POINTER_RE.match(ln)
        if pm:
            if pm.group(1).strip() == slug:
                kept.append(ln)
            continue
        rm = _RULE_LINE_RE.match(ln)
        if rm and rm.group(1):
            rtags = {slugify(x.strip(), 24) for x in rm.group(1).split(",") if x.strip()}
            if rtags and not (rtags & proj_tags):
                hidden_rules += 1
                continue
        kept.append(ln)
    out = "\n".join(kept).strip()
    if hidden_rules > 0:
        notice = (
            f'- _({hidden_rules} etiketli genel kural bu projeyle eşleşmediği için gizlendi; '
            f'tamamı için `memory_read("Kurallar.md")`)_'
        )
        out = (out + "\n" + notice).strip() if out else notice
    return out


def _read_if_exists(root: Path, doc_id: str) -> str | None:
    try:
        full = resolve_in_vault(root, doc_id)
        if full.is_file():
            return parse_markdown(full.read_text(encoding="utf-8", errors="replace"))[1].strip()
    except (OSError, PathTraversalError):
        pass
    return None


def _trim_tail(text: str, limit: int, min_keep: int | None = None) -> str:
    if len(text) <= limit:
        return text
    cap = max(0, limit)
    cut = text[:cap]
    floor = min(len(text), min_keep) if min_keep is not None else (cap // 2)
    nl = cut.rfind("\n")
    if nl >= floor and nl > 0:
        cut = cut[:nl]
    trimmed = cut.rstrip()
    if min_keep is not None and len(trimmed) < floor:
        # rstrip veya erken kesme tabanin altina indirdiyse tam min_keep/cap koru
        later_nl = text.find("\n", floor, cap)
        cut = text[:later_nl] if later_nl != -1 else text[: max(cap, floor)]
        trimmed = cut.rstrip("\n")
        if len(trimmed) < floor:
            trimmed = text[:floor]
    return trimmed + "\n…(kısaltıldı; tamamı için memory_read kullan)"


def bootstrap_context(vault_root, state_dir=None, cwd=None, project: str | None = None,
                      task_hint: str | None = None, budget_chars: int = 8000, reranker=None) -> dict:
    root = Path(os.path.abspath(str(vault_root)))
    scope = resolve_scope(root, cwd=cwd, project=project)
    if scope["status"] != "ok":
        return scope
    slug = scope["project"]
    ctx = _ctx(root, state_dir)
    conn = store.connect(ctx.state_dir)
    try:
        store.sync_vault(conn, ctx.vault_root)
        core = _read_if_exists(root, "Core.md") or ""
        grules = _filter_global_rules(_read_if_exists(root, "Kurallar.md") or "", root, slug)
        prules = _read_if_exists(root, f"projects/{slug}/Kurallar.md") if slug else None
        threads = _read_if_exists(root, f"projects/{slug}/Acik-Konular.md") if slug else None
        last = _read_if_exists(root, f"projects/{slug}/Son-Oturum.md") if slug else None
        sources = ["Core.md", "Kurallar.md"]
        relevant: list[dict] = []
        if task_hint and task_hint.strip():
            skip = {"Core.md", "Kurallar.md"}
            if slug:
                skip |= {f"projects/{slug}/{n}" for n in ("Kurallar.md", "Acik-Konular.md", "Son-Oturum.md")}
            for r in search_vault(conn, task_hint, project=slug, limit=10,
                                  kinds=["decision", "rule", "knowledge", "note", "overview"],
                                  reranker=_gate_external(ctx, reranker)):
                if r["doc_id"] in skip:
                    continue
                relevant.append({k: r[k] for k in ("doc_id", "title", "kind", "heading_path", "snippet", "score")})
                if len(relevant) >= 4:
                    break
    finally:
        conn.close()

    mandatory = ["# Hafıza bağlamı" + (f" — {scope['name']} ({slug})" if slug else " — genel")]
    if core:
        mandatory.append("## Core\n\n" + core)
    if grules:
        mandatory.append("## Genel kurallar\n\n" + grules)
    if prules is not None:
        mandatory.append(f"## Proje kuralları ({slug})\n\n" + (prules or "(kural yok)"))
        sources.append(f"projects/{slug}/Kurallar.md")
    text = "\n\n".join(mandatory)
    if len(text) > budget_chars:
        text += (
            f"\n\n> Not: Zorunlu kurallar ({len(text)} karakter) {budget_chars} karakterlik bütçeyi aştı; "
            "açık konular ve son oturum taban payla eklendi."
        )
    handoff: list[tuple[str, str]] = []
    if threads is not None:
        handoff.append((f"## Açık konular ({slug})", threads))
        sources.append(f"projects/{slug}/Acik-Konular.md")
    if last is not None and last.strip():
        handoff.append((f"## Son oturum ({slug})", last))
        sources.append(f"projects/{slug}/Son-Oturum.md")
    for idx, (heading, content) in enumerate(handoff):
        remaining = budget_chars - len(text) - len(heading) - 4
        handoff_left = len(handoff) - idx
        share = max(MIN_HANDOFF_CHARS, remaining // handoff_left)
        limit = max(MIN_HANDOFF_CHARS, min(remaining, share))
        text += f"\n\n{heading}\n\n" + _trim_tail(content, limit, min_keep=MIN_HANDOFF_CHARS)
    if relevant:
        rel = "\n\n".join(f"- **{r['title']}** (`{r['doc_id']}`)\n  {r['snippet']}" for r in relevant)
        heading = "## İlgili notlar (görev ipucu)"
        sources.extend(r["doc_id"] for r in relevant)
        remaining = budget_chars - len(text) - len(heading) - 4
        if remaining < 120:
            text += f"\n\n{heading}\n\n(bütçe nedeniyle atlandı; memory_search ile ara)"
        else:
            text += f"\n\n{heading}\n\n" + _trim_tail(rel, remaining)
    return {
        "status": "ok",
        "project": slug,
        "project_name": scope["name"],
        "core": core,
        "global_rules": grules,
        "project_rules": prules,
        "open_threads": threads,
        "last_session": last,
        "relevant": relevant,
        "sources": sources,
        "text": text,
    }


# ---------------------------------------------------------------- karar (ADR)


def _field(body: str, label: str) -> str:
    m = re.search(rf"^\*\*{re.escape(label)}:\*\*[ \t]*(.*)$", body, re.M)
    return m.group(1).strip() if m else ""


def _resolve_ref(sess: _Session, ref: str, folder: str) -> str:
    """supersedes hedefi YALNIZ aktif kapsamin karar klasorundeki bir ADR olabilir."""
    r = ref.strip()
    m = re.match(r"^\[\[([^\]|#]+)", r)
    if m:
        r = m.group(1)
    r = r.replace("\\", "/").strip()
    if not r.lower().endswith(".md"):
        r += ".md"
    if "/" not in r:
        r = f"{folder}/{r}"
    r = posixpath.normpath(r)
    if not r.startswith(folder + "/") or r.count("/") != folder.count("/") + 1:
        raise VaultError(f"supersedes hedefi '{folder}/' altındaki bir karar notu olmalı: {ref}")
    if not sess.exists(r):
        raise VaultError(f"supersedes hedefi bulunamadı: {ref}")
    return r


def _find_duplicate_decision(sess: _Session, slug: str | None, title: str, decision: str) -> dict | None:
    rows = sess.conn.execute(
        "SELECT doc_id, title FROM documents WHERE kind='decision' AND status='active' AND project IS ? "
        "AND doc_id NOT IN (SELECT older_doc_id FROM supersedes)",
        (slug,),
    ).fetchall()
    ft = fold_tr(title)
    fd = fold_tr(decision)[:300]
    for r in rows:
        same_title = difflib.SequenceMatcher(None, ft, fold_tr(r["title"])).ratio() >= 0.9
        other = ""
        try:
            other = _field(sess.read(r["doc_id"]), "Karar")
        except OSError:
            pass
        same_dec = bool(other) and difflib.SequenceMatcher(None, fd, fold_tr(other)[:300]).ratio() >= 0.9
        if same_title or same_dec:
            return {
                "status": "duplicate_warning",
                "existing": r["doc_id"],
                "message": "Benzer aktif karar zaten var. Yerine geçiyorsa supersedes ver; "
                           "yine de yazmak için force=true kullan.",
            }
    return None


def _collect_advisor_inputs(sess: _Session, slug: str | None, title: str, decision: str,
                            context: str, alternatives: str, rationale: str,
                            skip_ids: set[str]) -> tuple[dict, list[dict]] | None:
    vault_root = sess.ctx.vault_root
    proposal = {"project": slug, "title": title, "decision": decision, "context": context,
                "alternatives": alternatives, "rationale": rationale}
    if any(dlp.scan(v, vault_root) for v in proposal.values() if isinstance(v, str) and v):
        return None
    rows = sess.conn.execute(
        "SELECT doc_id, title FROM documents WHERE kind='decision' AND status='active' AND project IS ? "
        "AND doc_id NOT IN (SELECT older_doc_id FROM supersedes) ORDER BY updated_at DESC LIMIT ?",
        (slug, ADVISOR_PRIOR_LIMIT),
    ).fetchall()
    priors: list[dict] = []
    for r in rows:
        if r["doc_id"] in skip_ids:
            continue
        try:
            body = sess.read(r["doc_id"])
        except OSError:
            continue
        prior = {"doc_id": r["doc_id"], "title": r["title"], "decision": _field(body, "Karar"),
                 "rationale": _field(body, "Gerekçe")}
        if any(dlp.scan(v, vault_root) for v in (prior["title"], prior["decision"], prior["rationale"]) if v):
            continue
        priors.append(prior)
    return proposal, priors


def _run_advisor(advisor: DecisionAdvisor, proposal: dict, priors: list[dict]) -> dict | None:
    try:
        adv = advisor.assess_decision(proposal, priors)
    except Exception:  # noqa: BLE001 - harici danisman hatasi karar kaydini asla engellememeli
        return None
    if isinstance(adv, dict) and isinstance(adv.get("warnings"), list) and adv["warnings"]:
        return {
            "status": "advisor_warning",
            "warnings": adv["warnings"],
            "existing": adv.get("existing"),
            "message": adv.get("message") or (
                "Danışman uyarısı: Karar taslağı mevcut bir kararla çelişiyor/örtüşüyor veya henüz "
                "kesinleşmemiş görünüyor. Eski kararın yerine geçiyorsa supersedes ver; "
                "yine de yazmak için force=true kullan."
            ),
        }
    return None


def _write_decision(sess: _Session, ctx: _Ctx, slug: str | None, folder: str, now: datetime,
                    title: str, decision: str, context: str, alternatives: str, rationale: str,
                    old_ids: list[str], sources_c: list[str], tags_c: list[str], cats: list[str]) -> dict:
    date = now.strftime("%Y-%m-%d")
    base = f"{folder}/{date}-{slugify(title)}"
    doc_id = base + ".md"
    n = 2
    while sess.exists(doc_id):
        doc_id = f"{base}-{n}.md"
        n += 1
    meta = {
        "title": title,
        "kind": "decision",
        "project": slug,
        "status": "active",
        "date": date,
        "created_at": _iso(now),
        "updated_at": _iso(now),
        "tags": tags_c,
        "supersedes": old_ids,
        "sources": sources_c,
    }
    lines = [f"# {title}", "", f"**Karar:** {decision}"]
    if context:
        lines.append(f"**Bağlam:** {context}")
    if alternatives:
        lines.append(f"**Alternatifler:** {alternatives}")
    if rationale:
        lines.append(f"**Gerekçe:** {rationale}")
    lines.append("**Durum:** geçerli")
    if sources_c:
        lines.append("**Kaynak:** " + ", ".join(sources_c))
    if slug:
        pname = (config.get_project(ctx.vault_root, slug) or {}).get("name", slug)
        hub_link = project_hub_doc_id(ctx.vault_root, slug, pname)[:-3]
        lines += ["", f"Proje: [[{hub_link}|{pname} genel bakış]]"]
    sha = sess.write(doc_id, _meta_doc(meta, "\n".join(lines) + "\n"), "decide", {"supersedes": old_ids})

    for old in old_ids:
        otext = sess.read(old)
        ometa, obody = parse_markdown(otext)
        ometa["status"] = "superseded"
        ometa["superseded_by"] = doc_id
        _touch(ometa, now)
        status_line = f"**Durum:** Yerine geçen: [[{doc_id[:-3]}|{title.replace('|', '/')}]]"
        if re.search(r"^\*\*Durum:\*\*.*$", obody, re.M):
            obody = re.sub(r"^\*\*Durum:\*\*.*$", lambda _m: status_line, obody, count=1, flags=re.M)
        else:
            obody = obody.rstrip("\n") + "\n\n" + status_line + "\n"
        sess.write(old, _meta_doc(ometa, obody), "supersede", {"by": doc_id})
    sess.sync()
    if slug:
        _refresh_hub(sess, slug)
        sess.sync()
    return {"status": "created", "path": doc_id, "sha256": sha, "superseded": old_ids, "dlp_categories": cats}


def _check_not_already_superseded(sess: _Session, old_ids: list[str]) -> None:
    for old in old_ids:
        ometa, _ = parse_markdown(sess.read(old))
        if ometa.get("status") == "superseded":
            by = ometa.get("superseded_by") or "başka bir karar"
            raise VaultError(
                f"{old} zaten geçersiz kılınmış (yerine geçen: {by}). "
                "Güncel kararı supersedes olarak verin veya force=true kullanın."
            )


def record_decision(vault_root, project: str | None, title: str, decision: str, context: str = "",
                    alternatives: str = "", rationale: str = "", supersedes: list[str] | None = None,
                    sources: list[str] | None = None, tags: list[str] | None = None,
                    force: bool = False, state_dir=None, actor: str = "mem",
                    advisor: DecisionAdvisor | None = None) -> dict:
    if not (title or "").strip() or not (decision or "").strip():
        raise VaultError("title ve decision zorunlu.")
    raw_inputs = [title, decision, context, alternatives, rationale] + list(sources or []) + list(tags or [])
    n_src = len(sources or [])
    ctx = _ctx(vault_root, state_dir)
    slug = None if project in (None, "", "shared") else project
    if slug is not None and config.get_project(ctx.vault_root, slug) is None:
        raise VaultError(f"Bilinmeyen proje: {slug!r}")
    folder = f"projects/{slug}/kararlar" if slug else "knowledge/kararlar"
    now = _now()
    use_advisor = not force and advisor is not None and config.can_use_advisor(ctx.profile, ctx.dlp_mode)
    advisor_payload: tuple[dict, list[dict]] | None = None
    with _Session(ctx, actor) as sess:
        values, cats = _shield_all(sess, raw_inputs)
        title_c, decision_c, context_c, alternatives_c, rationale_c = [v.strip() for v in values[:5]]
        sources_c = values[5 : 5 + n_src]
        tags_c = values[5 + n_src :]
        sess.sync()
        old_ids = [_resolve_ref(sess, s, folder) for s in (supersedes or [])]
        if not force:
            _check_not_already_superseded(sess, old_ids)
        if not force and not old_ids:
            dup = _find_duplicate_decision(sess, slug, title_c, decision_c)
            if dup is not None:
                return dup
        if use_advisor:
            advisor_payload = _collect_advisor_inputs(
                sess, slug, title_c, decision_c, context_c, alternatives_c, rationale_c, set(old_ids)
            )
        if not use_advisor or advisor_payload is None:
            return _write_decision(
                sess, ctx, slug, folder, now, title_c, decision_c, context_c, alternatives_c, rationale_c,
                old_ids, sources_c, tags_c, cats,
            )

    # Harici danisman cagrisi VaultLock disinda yapilir; ag gecikmesi diger ajanlari bloklamaz.
    warned = _run_advisor(advisor, advisor_payload[0], advisor_payload[1])  # type: ignore[arg-type]
    if warned is not None:
        return warned

    ctx = _ctx(vault_root, state_dir)
    with _Session(ctx, actor) as sess:
        values, cats = _shield_all(sess, raw_inputs)
        title_c, decision_c, context_c, alternatives_c, rationale_c = [v.strip() for v in values[:5]]
        sources_c = values[5 : 5 + n_src]
        tags_c = values[5 + n_src :]
        sess.sync()
        old_ids = [_resolve_ref(sess, s, folder) for s in (supersedes or [])]
        if not force:
            _check_not_already_superseded(sess, old_ids)
        if not force and not old_ids:
            dup = _find_duplicate_decision(sess, slug, title_c, decision_c)
            if dup is not None:
                return dup
        return _write_decision(
            sess, ctx, slug, folder, now, title_c, decision_c, context_c, alternatives_c, rationale_c,
            old_ids, sources_c, tags_c, cats,
        )


# ---------------------------------------------------------------- kural


def _rule_text(line: str) -> str:
    m = _RULE_LINE_RE.match(line)
    return fold_tr(m.group(2)) if m else ""


_ARCHIVED_REMOVED_RE = re.compile(r"^- ~~(.+)~~ \*\(kaldırıldı:.*\)\*$")


def _find_rule_line_opt(lines: list[str], query_text: str) -> int | None:
    q = fold_tr(" ".join((query_text or "").split()))
    if not q:
        raise VaultError("Eşleştirilecek kural metni boş olamaz.")
    exact = [i for i, ln in enumerate(lines) if _rule_text(ln) == q]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise VaultError(f"Birden fazla kural eşleşti ({len(exact)}); daha belirgin metin verin.")
    if len(q) >= 10:
        sub = [i for i, ln in enumerate(lines) if _rule_text(ln) and q in _rule_text(ln)]
        if len(sub) == 1:
            return sub[0]
        if len(sub) > 1:
            raise VaultError(f"Birden fazla kural eşleşti ({len(sub)}); daha belirgin metin verin.")
    return None


def _find_rule_line(lines: list[str], query_text: str) -> int:
    idx = _find_rule_line_opt(lines, query_text)
    if idx is None:
        raise VaultError(f"Eşleşen kural bulunamadı: {query_text!r}")
    return idx


def _find_archived_removed_rule(sess: _Session, doc_id: str, query_text: str, now: datetime) -> tuple[str, str] | None:
    month = now.strftime("%Y-%m")
    arch_id = f"archive/{month}.md"
    if not sess.exists(arch_id):
        return None
    q = fold_tr(" ".join((query_text or "").split()))
    if not q:
        return None
    marker = f"<!-- kurallar: {doc_id} -->"
    alines = sess.read(arch_id).split("\n")
    matches: list[str] = []
    for i, ln in enumerate(alines):
        if ln.strip() == marker and i + 1 < len(alines):
            m = _ARCHIVED_REMOVED_RE.match(alines[i + 1].strip())
            if not m:
                continue
            rule_line = m.group(1)
            rt = _rule_text(rule_line)
            if rt == q or (len(q) >= 10 and rt and q in rt):
                matches.append(rule_line)
    if matches:
        return arch_id, matches[-1]
    return None


def _archive_rule(sess: _Session, doc_id: str, old_line: str, note: str, now: datetime) -> str:
    month = now.strftime("%Y-%m")
    arch_id = f"archive/{month}.md"
    if sess.exists(arch_id):
        existing = sess.read(arch_id)
    else:
        existing = _meta_doc(
            {"title": f"Arşiv {month}", "kind": "note", "status": "archived", "created_at": _iso(now)},
            f"# Arşiv {month}\n",
        )
    entry_block = f"<!-- kurallar: {doc_id} -->\n- ~~{old_line.strip()}~~ *({note})*"
    if entry_block in existing:
        return arch_id
    sess.write(arch_id, existing.rstrip("\n") + "\n\n" + entry_block + "\n", "archive_rule")
    return arch_id


def add_rule(vault_root, scope: str, rule_text: str = "", reason: str = "", state_dir=None,
             actor: str = "mem", tags: list[str] | None = None,
             supersedes: str | None = None, remove: bool | str = False) -> dict:
    is_remove = bool(remove.strip()) if isinstance(remove, str) else bool(remove)
    if is_remove:
        if isinstance(remove, str) and (rule_text or "").strip():
            raise VaultError("remove ile yeni kural metni birlikte verilemez; yerine koymak için supersedes kullanın.")
        remove_query = remove.strip() if isinstance(remove, str) else (rule_text or "").strip()
        if not remove_query:
            raise VaultError("Kaldırılacak kural metni boş olamaz.")
    elif not (rule_text or "").strip():
        raise VaultError("Kural metni boş olamaz.")
    ctx = _ctx(vault_root, state_dir)
    if scope in (None, "", "global", VAULT_SCOPE):
        doc_id = "Kurallar.md"
    else:
        if config.get_project(ctx.vault_root, scope) is None:
            raise VaultError(f"Bilinmeyen proje: {scope!r}")
        doc_id = f"projects/{scope}/Kurallar.md"
    now = _now()
    with _Session(ctx, actor) as sess:
        if is_remove:
            (rq, rsn), cats = _shield_all(sess, [remove_query, reason])
            rsn = " ".join(rsn.split())
            current = sess.read(doc_id)
            lines = current.split("\n")
            idx = _find_rule_line_opt(lines, rq)
            if idx is None:
                archived_hit = _find_archived_removed_rule(sess, doc_id, rq, now)
                if archived_hit is not None:
                    arch_id, old_line = archived_hit
                    cur_sha = sha256_bytes(sess.path(doc_id).read_bytes())
                    return {"status": "removed", "path": doc_id, "removed_line": old_line,
                            "archived": [arch_id], "sha256": cur_sha, "dlp_categories": cats}
                raise VaultError(f"Eşleşen kural bulunamadı: {rq!r}")
            old_line = lines[idx]
            new_lines = lines[:idx] + lines[idx + 1 :]
            new_text = "\n".join(new_lines).rstrip("\n") + "\n"
            arch_id = _archive_rule(sess, doc_id, old_line, f"kaldırıldı: {rsn or 'remove'}", now)
            sha = sess.write(doc_id, new_text, "rule_remove")
            sess.sync()
            return {"status": "removed", "path": doc_id, "removed_line": old_line, "archived": [arch_id],
                    "sha256": sha, "dlp_categories": cats}

        raw_tags = list(tags or [])
        vals, cats = _shield_all(sess, [rule_text, reason] + raw_tags)
        text = " ".join(vals[0].split())
        rsn = " ".join(vals[1].split())
        norm_tags = _normalize_tags(vals[2:])
        tag_part = f"{{tags: {', '.join(norm_tags)}}} " if norm_tags else ""
        line = f"- **[{now.strftime('%Y-%m-%d')}]** {tag_part}{text}" + (f" *(Gerekçe: {rsn})*" if rsn else "")
        current = sess.read(doc_id)
        lines = current.split("\n")
        superseded_line = None
        arch_ids: list[str] = []
        if supersedes and supersedes.strip():
            sup_q, _ = _shield_all(sess, [supersedes])
            idx = _find_rule_line(lines, sup_q[0])
            superseded_line = lines[idx]
            rem_lines = lines[:idx] + lines[idx + 1 :]
        else:
            rem_lines = lines
        key = fold_tr(text)
        if any(_rule_text(ln) == key for ln in rem_lines):
            return {"status": "duplicate", "path": doc_id, "line": line}
        if superseded_line is not None:
            arch_ids.append(_archive_rule(sess, doc_id, superseded_line, f"yerine: {text}", now))
        updated = "\n".join(rem_lines).rstrip("\n") + "\n" + line + "\n"
        sha = sess.write(doc_id, updated, "rule")
        sess.sync()
    out = {"status": "added", "path": doc_id, "line": line, "sha256": sha, "dlp_categories": cats}
    if superseded_line is not None:
        out["superseded_line"] = superseded_line
        out["archived"] = arch_ids
    return out


# ---------------------------------------------------------------- yama


def patch_note(vault_root, path: str, expected_sha256: str, edits: list[dict], state_dir=None,
               project: str | None = None, actor: str = "mem") -> dict:
    if not edits:
        raise VaultError("edits boş olamaz.")
    ctx = _ctx(vault_root, state_dir)
    doc_id = _check_note_path(path)
    with _Session(ctx, actor) as sess:
        full = sess.path(doc_id)
        if not full.is_file():
            raise VaultError(f"Not bulunamadı: {doc_id}")
        raw = full.read_bytes()
        current = sha256_bytes(raw)
        if (expected_sha256 or "").lower() != current:
            raise ConcurrencyConflictError(
                f"{doc_id} beklenen sha256 ile eşleşmiyor (başka bir ajan değiştirmiş olabilir). "
                "memory_read ile güncel içeriği ve sha256'yı alıp yeniden dene.",
                current_sha256=current,
            )
        text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
        if project:
            _assert_access(doc_id, parse_markdown(text)[0], project)
        cats: set[str] = set()
        for i, e in enumerate(edits):
            old, new = e.get("old"), e.get("new", "")
            if not isinstance(old, str) or old == "" or not isinstance(new, str):
                raise VaultError(f"edits[{i}]: 'old' (boş olmayan) ve 'new' string olmalı.")
            new, c = sess.shield(new)
            cats.update(c)
            count = text.count(old)
            if count != 1:
                raise VaultError(f"edits[{i}]: 'old' metni dosyada tam 1 kez geçmeli (bulunan: {count}).")
            text = text.replace(old, new, 1)
        sha = sess.write(doc_id, text, "patch", {"edits": len(edits), "previous_sha256": current})
        sess.sync()
        slug = _doc_project(doc_id, {})
        if slug and "/kararlar/" in doc_id and config.get_project(ctx.vault_root, slug):
            if _refresh_hub(sess, slug):
                sess.sync()
    return {"status": "patched", "path": doc_id, "sha256": sha, "previous_sha256": current,
            "dlp_categories": sorted(cats)}


# ---------------------------------------------------------------- oturum bitirme


def _split_cards(body: str) -> tuple[str, list[str]]:
    header: list[str] = []
    cards: list[str] = []
    cur: list[str] | None = None
    for ln in body.split("\n"):
        if ln.startswith("## "):
            if cur is not None:
                cards.append("\n".join(cur).rstrip("\n"))
            cur = [ln]
        elif cur is None:
            header.append(ln)
        else:
            cur.append(ln)
    if cur is not None:
        cards.append("\n".join(cur).rstrip("\n"))
    return "\n".join(header).rstrip("\n"), cards


def _escape_heads(text: str) -> str:
    return "\n".join(("\\" + ln) if ln.lstrip().startswith("#") else ln for ln in text.split("\n"))


def _compose_session(meta: dict, header: str, cards: list[str]) -> str:
    body = header + "\n\n" + "\n\n".join(cards) + "\n" if cards else header + "\n"
    return _meta_doc(meta, body)


def _archive_card(sess: _Session, slug: str, card: str, now: datetime) -> str:
    m = re.match(r"^## (\d{4}-\d{2})-\d{2}", card)
    month = m.group(1) if m else now.strftime("%Y-%m")
    doc_id = f"archive/{month}.md"
    if sess.exists(doc_id):
        existing = sess.read(doc_id)
    else:
        existing = _meta_doc(
            {"title": f"Arşiv {month}", "kind": "note", "status": "archived", "created_at": _iso(now)},
            f"# Arşiv {month}\n",
        )
    if card in existing:
        return doc_id
    entry = f"\n<!-- proje: {slug} -->\n{card}\n"
    sess.write(doc_id, existing.rstrip("\n") + "\n" + entry, "archive")
    return doc_id


@dataclass
class _ThreadItem:
    id: str
    owner: str
    text: str


_THREAD_LINE_RE = re.compile(r"^\s*-\s+\[\s*\]\s+(.*)$")
_THREAD_TAG_RE = re.compile(r"\s*<!--\s*t:([a-z0-9_-]+)\s+h:([a-z0-9_-]+)\s*-->\s*$")
MANAGED_THREADS_HEADING = "## Ajan Açık Konuları"


def _make_thread_id(owner: str, text: str, used_ids: set[str]) -> str:
    base = sha256_bytes(f"{owner}|{fold_tr(text)}".encode("utf-8"))
    cand = base[:6]
    if cand not in used_ids:
        return cand
    for length in range(7, len(base) + 1):
        c = base[:length]
        if c not in used_ids:
            return c
    n = 2
    while f"{cand}-{n}" in used_ids:
        n += 1
    return f"{cand}-{n}"


def _split_threads_body(tb: str, default_title: str) -> tuple[str, str, list[_ThreadItem]]:
    items: list[_ThreadItem] = []
    used_ids: set[str] = set()
    other_lines: list[str] = []
    for ln in tb.split("\n"):
        s = ln.strip()
        m = _THREAD_LINE_RE.match(ln)
        if m:
            raw = m.group(1).strip()
            tm = _THREAD_TAG_RE.search(raw)
            if tm:
                text = raw[: tm.start()].strip()
                tid = tm.group(1).lower()
                owner = tm.group(2).lower()
                if tid in used_ids:
                    tid = _make_thread_id(owner, text, used_ids)
            else:
                text = raw
                owner = "legacy"
                tid = _make_thread_id(owner, text, used_ids)
            if text:
                used_ids.add(tid)
                items.append(_ThreadItem(id=tid, owner=owner, text=text))
            continue
        if s == "(açık konu yok)" or s == MANAGED_THREADS_HEADING:
            continue
        other_lines.append(ln)

    while other_lines and not other_lines[0].strip():
        other_lines.pop(0)
    if other_lines and other_lines[0].startswith("# ") and not other_lines[0].startswith("## "):
        h1 = other_lines[0].rstrip()
        rest = "\n".join(other_lines[1:]).strip()
    else:
        h1 = f"# {default_title} — Açık Konular"
        rest = "\n".join(other_lines).strip()
    return h1, rest, items


def _compose_threads_body(h1: str, freeform_rest: str, final_items: list[_ThreadItem]) -> str:
    items_str = (
        "\n".join(f"- [ ] {it.text} <!-- t:{it.id} h:{it.owner} -->" for it in final_items)
        if final_items
        else "(açık konu yok)"
    )
    if freeform_rest:
        return f"{h1}\n\n{freeform_rest}\n\n{MANAGED_THREADS_HEADING}\n\n{items_str}\n"
    return f"{h1}\n\n{items_str}\n"


def _parse_threads(body: str) -> list[_ThreadItem]:
    return _split_threads_body(body, "")[2]


def _archive_closed_threads(sess: _Session, slug: str, items: list[_ThreadItem],
                            tag: str, harness: str, now: datetime) -> str:
    month = now.strftime("%Y-%m")
    doc_id = f"archive/{month}.md"
    if sess.exists(doc_id):
        existing = sess.read(doc_id)
    else:
        existing = _meta_doc(
            {"title": f"Arşiv {month}", "kind": "note", "status": "archived", "created_at": _iso(now)},
            f"# Arşiv {month}\n",
        )
    lines = [
        f"<!-- proje: {slug} · kapanan-konular -->",
        f"## {now.strftime('%Y-%m-%d %H:%M')} · Kapanan Konular · {tag}",
        "",
    ]
    for it in items:
        lines.append(f"- [x] {it.text} *(açan: {it.owner}, kapatan: {harness}, t:{it.id})*")
    block = "\n".join(lines)
    sess.write(doc_id, existing.rstrip("\n") + "\n\n" + block + "\n", "archive_threads",
               {"project": slug, "count": len(items)})
    return doc_id


def _match_close_selector(items: list[_ThreadItem], sel: str) -> list[_ThreadItem]:
    s = sel.strip()
    if s.lower().startswith("t:"):
        id_probe = s[2:].strip().lower()
        if not id_probe:
            return []
        return [it for it in items if it.id.lower() == id_probe]
    txt_probe = fold_tr(" ".join(s.split()))
    if not txt_probe:
        return []
    return [it for it in items if fold_tr(it.text) == txt_probe]


def _plan_project_threads(sess: _Session, slug: str, proj_name: str, harness: str,
                          now: datetime, open_threads: list[str] | None, threads_c: list[str],
                          close_c: list[str], mode: str) -> tuple[str, str, list[_ThreadItem], list[str]]:
    tid = f"projects/{slug}/Acik-Konular.md"
    old_ttext = sess.read(tid)
    tmeta, tb = parse_markdown(old_ttext)
    h1, freeform_rest, current_items = _split_threads_body(tb, proj_name)

    dedup_threads_c: list[str] = []
    seen_new: set[str] = set()
    for t in threads_c:
        if not t:
            continue
        ft = fold_tr(t)
        if ft not in seen_new:
            seen_new.add(ft)
            dedup_threads_c.append(t)

    closed_items: list[_ThreadItem] = []
    unmatched_close: list[str] = []

    if mode == "replace":
        explicit_closed: list[_ThreadItem] = []
        for sel in close_c:
            if not sel:
                continue
            matches = _match_close_selector(current_items, sel)
            if len(matches) > 1:
                raise VaultError(
                    f"close_threads belirsiz ({len(matches)} eşleşme): {sel!r}; tam madde kimliği (t:...) kullanın."
                )
            if len(matches) == 1:
                if matches[0] not in explicit_closed:
                    explicit_closed.append(matches[0])
            else:
                unmatched_close.append(sel)
        if open_threads is None:
            final_items = [it for it in current_items if it not in explicit_closed]
            closed_items = list(explicit_closed)
        else:
            new_keys = {fold_tr(t) for t in dedup_threads_c}
            final_items = []
            new_used: set[str] = set()
            for t in dedup_threads_c:
                nid = _make_thread_id(harness, t, new_used)
                new_used.add(nid.lower())
                final_items.append(_ThreadItem(id=nid, owner=harness, text=t))
            for it in current_items:
                if fold_tr(it.text) not in new_keys or it in explicit_closed:
                    closed_items.append(it)
    else:
        remaining = list(current_items)
        for sel in close_c:
            if not sel:
                continue
            matches = _match_close_selector(remaining, sel)
            if len(matches) > 1:
                raise VaultError(
                    f"close_threads belirsiz ({len(matches)} eşleşme): {sel!r}; tam madde kimliği (t:...) kullanın."
                )
            if len(matches) == 1:
                m = matches[0]
                remaining = [it for it in remaining if it is not m]
                closed_items.append(m)
            else:
                unmatched_close.append(sel)
        if open_threads is not None:
            others = [it for it in remaining if it.owner != harness]
            mine = [it for it in remaining if it.owner == harness]
            new_keys = {fold_tr(t) for t in dedup_threads_c}
            mine_by_key: dict[str, _ThreadItem] = {}
            for it in mine:
                fk = fold_tr(it.text)
                if fk not in new_keys:
                    closed_items.append(it)
                elif fk not in mine_by_key:
                    mine_by_key[fk] = it
            others_keys = {fold_tr(it.text) for it in others}
            used_ids = {it.id.lower() for it in others} | {it.id.lower() for it in mine_by_key.values()}
            updated_mine: list[_ThreadItem] = []
            for t in dedup_threads_c:
                ft = fold_tr(t)
                if ft in others_keys:
                    continue
                if ft in mine_by_key:
                    old_it = mine_by_key[ft]
                    updated_mine.append(_ThreadItem(id=old_it.id, owner=harness, text=t))
                else:
                    nid = _make_thread_id(harness, t, used_ids)
                    used_ids.add(nid.lower())
                    updated_mine.append(_ThreadItem(id=nid, owner=harness, text=t))
            final_items = others + updated_mine
        else:
            final_items = remaining

    _touch(tmeta, now)
    new_ttext = _meta_doc(tmeta, _compose_threads_body(h1, freeform_rest, final_items))
    return old_ttext, new_ttext, closed_items, unmatched_close


def finish_session(vault_root, cwd_or_project, summary: str, open_threads: list[str] | None = None,
                   refs: list[str] | None = None, session_id: str | None = None,
                   harness: str = "mcp", state_dir=None,
                   close_threads: list[str] | None = None,
                   threads_mode: str = "merge") -> dict:
    if not (summary or "").strip():
        raise VaultError("summary boş olamaz.")
    mode = "merge" if threads_mode is None else threads_mode
    if mode not in ("merge", "replace"):
        raise VaultError(f"Geçersiz threads_mode: {threads_mode!r}")
    root = Path(os.path.abspath(str(vault_root)))
    if cwd_or_project and config.get_project(root, str(cwd_or_project)) is not None:
        scope = resolve_scope(root, project=str(cwd_or_project))
    else:
        scope = resolve_scope(root, cwd=cwd_or_project)
    if scope["status"] != "ok":
        return scope
    slug = scope["project"]
    ctx = _ctx(root, state_dir)
    now = _now()
    harness = slugify(harness or "mcp", 20)
    with _Session(ctx, f"{harness}") as sess:
        raw_open = list(open_threads or [])
        raw_refs = list(refs or [])
        raw_close = list(close_threads or [])
        vals, cats = _shield_all(sess, [summary] + raw_open + raw_refs + raw_close)
        summary_c = vals[0].strip()
        n_thr = len(raw_open)
        n_ref = len(raw_refs)
        threads_c = [" ".join(t.split()) for t in vals[1 : 1 + n_thr] if " ".join(t.split())]
        refs_c = [" ".join(r.split()) for r in vals[1 + n_thr : 1 + n_thr + n_ref]]
        close_c = [" ".join(c.split()) for c in vals[1 + n_thr + n_ref :]]
        if session_id and str(session_id).strip():
            sid_full = slugify(session_id, len(str(session_id)) + 16)
            while sid_full.startswith(f"{harness}-"):
                sid_full = sid_full[len(harness) + 1 :]
            if not sid_full or sid_full == harness:
                tag = harness
                norm_sid = harness
            else:
                norm_sid = sid_full[:24].rstrip("-")
                tag = f"{harness}-{norm_sid}"
            key = f"{slug}|{harness}|{norm_sid}"
        else:
            key = f"{slug}|{harness}|{summary_c}"
            tag = None
        event_hash = sha256_bytes(key.encode("utf-8"))
        tag = tag or f"{harness}-{event_hash[:8]}"

        # Tum yazmalardan ONCE acik konu guncellemesini ve close_threads secicilerini dogrula.
        thread_plan = None
        if slug and (open_threads is not None or close_threads is not None):
            thread_plan = _plan_project_threads(
                sess, slug, scope["name"] or slug, harness, now,
                open_threads, threads_c, close_c, mode,
            )

        month_dir = f"receipts/{now.strftime('%Y-%m')}"
        receipt_id = f"{month_dir}/{event_hash[:16]}.md"
        rdir = ctx.vault_root / "receipts"
        if rdir.is_dir():
            for found in rdir.glob(f"*/{event_hash[:16]}.md"):
                receipt_id = found.relative_to(ctx.vault_root).as_posix()
        created = _iso(now)
        if sess.exists(receipt_id):
            created = str(parse_markdown(sess.read(receipt_id))[0].get("created_at") or created)
        if slug:
            hub_link = project_hub_doc_id(ctx.vault_root, slug)[:-3]
            proj_ref = f"[[{hub_link}|{slug}]]"
        else:
            proj_ref = "[[Core|genel]]"
        rbody = [f"# Makbuz — {tag}", "",
                 f"**Proje:** {proj_ref} · **Harness:** {harness} · **Oturum:** {tag}", "", summary_c]
        if threads_c:
            rbody += ["", "**Açık konular:**"] + [f"- {t}" for t in threads_c]
        if refs_c:
            rbody += ["", "**Kaynaklar:** " + ", ".join(refs_c)]
        rmeta = {"title": f"Makbuz {tag}", "kind": "receipt", "project": slug, "status": "active",
                 "harness": harness, "session": tag, "event_hash": event_hash,
                 "created_at": created, "updated_at": _iso(now), "refs": refs_c}
        new_receipt = _meta_doc(rmeta, "\n".join(rbody) + "\n")
        if not (sess.exists(receipt_id) and sess.read(receipt_id) == new_receipt):
            sess.write(receipt_id, new_receipt, "finish", {"session": tag})
        result = {"status": "recorded", "project": slug, "receipt": receipt_id, "session_tag": tag,
                  "archived": [], "closed_threads": [], "dlp_categories": cats}

        if slug:
            son_id = f"projects/{slug}/Son-Oturum.md"
            smeta, sbody = parse_markdown(sess.read(son_id))
            header, cards = _split_cards(sbody)
            card_lines = [f"## {now.strftime('%Y-%m-%d %H:%M')} · {tag}", "", _escape_heads(summary_c)]
            if threads_c:
                card_lines += ["", "**Açık konular:**"] + [f"- {_escape_heads(t)}" for t in threads_c]
            if refs_c:
                card_lines += ["", "**Kaynaklar:** " + ", ".join(refs_c)]
            card = "\n".join(card_lines)
            idx = next((i for i, c in enumerate(cards) if c.split("\n", 1)[0].endswith(f" · {tag}")), None)
            if idx is None:
                cards.insert(0, card)
            else:
                old_body = cards[idx].split("\n", 1)[1] if "\n" in cards[idx] else ""
                if old_body != card.split("\n", 1)[1]:
                    cards[idx] = card
            _touch(smeta, now)
            text = _compose_session(smeta, header, cards)
            archived: list[str] = []
            while (
                len(cards) > SESSION_MAX_CARDS
                or (len(text) > SESSION_LIMIT and len(cards) > SESSION_MIN_CARDS)
                or (len(text) > 12000 and len(cards) > 1)
            ):
                oldest = cards.pop()
                archived.append(_archive_card(sess, slug, oldest, now))
                text = _compose_session(smeta, header, cards)
            old_text = sess.read(son_id)
            if text != old_text:
                sess.write(son_id, text, "session_card", {"session": tag, "archived": len(archived)})

            if thread_plan is not None:
                old_ttext, new_ttext, closed_items, unmatched_close = thread_plan
                if closed_items:
                    archived.append(_archive_closed_threads(sess, slug, closed_items, tag, harness, now))
                if new_ttext != old_ttext:
                    sess.write(f"projects/{slug}/Acik-Konular.md", new_ttext, "threads")
                result["closed_threads"] = [it.id for it in closed_items]
                if unmatched_close:
                    result["unmatched_close"] = unmatched_close
            result["archived"] = sorted(set(archived))
        sess.sync()
    return result


def _parse_receipt_dt(meta: dict, st_mtime: float) -> datetime:
    for k in ("created_at", "updated_at"):
        raw = meta.get(k)
        if raw:
            try:
                dt = datetime.fromisoformat(str(raw))
                if dt.tzinfo is not None:
                    dt = dt.astimezone().replace(tzinfo=None)
                return dt
            except ValueError:
                pass
    return datetime.fromtimestamp(st_mtime)


def _receipt_short_summary(body: str) -> str:
    lines: list[str] = []
    in_threads = False
    for ln in body.split("\n"):
        s = ln.strip()
        if not s or s.startswith("#") or s.startswith("**Proje:**"):
            continue
        if s.startswith("**Açık konular:**"):
            in_threads = True
            continue
        if s.startswith("**Kaynaklar:**"):
            in_threads = False
            continue
        if in_threads and s.startswith("- "):
            continue
        if s.startswith("- "):
            s = s[2:].strip()
        if s:
            lines.append(s)
            break
    flat = " ".join(" ".join(lines).split())
    if len(flat) > 140:
        return flat[:137].rstrip() + "..."
    return flat or "(özet yok)"


def compact_receipts(vault_root, older_than_days: int = 14, project: str | None = None,
                     dry_run: bool = False, state_dir=None) -> dict:
    days = int(older_than_days)
    if days < 0:
        raise VaultError("older_than_days negatif olamaz.")
    ctx = _ctx(vault_root, state_dir)
    if project is not None and config.get_project(ctx.vault_root, project) is None:
        raise VaultError(f"Bilinmeyen proje: {project!r}")
    now = _now()
    cutoff = now - timedelta(days=days)
    rdir = ctx.vault_root / "receipts"
    if not rdir.is_dir():
        return {"status": "ok", "dry_run": dry_run, "compacted": 0, "summaries": []}

    groups: dict[tuple[str, str | None], list[dict]] = {}
    with _Session(ctx, "compact") as sess:
        for rel, full, st in store.iter_markdown(rdir):
            doc_id = f"receipts/{rel}"
            if doc_id.endswith("-ozet.md"):
                continue
            text = full.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
            meta, body = parse_markdown(text)
            if meta.get("compacted_summary"):
                continue
            trust = str(meta.get("trust") or "trusted")
            if trust != "trusted":
                continue
            kind = str(meta.get("kind") or "receipt")
            status = str(meta.get("status") or "active")
            if kind != "receipt" or status != "active":
                continue
            p = meta.get("project")
            proj_slug = p.strip() if isinstance(p, str) and p.strip() else None
            if project is not None and proj_slug != project:
                continue
            dt = _parse_receipt_dt(meta, st.st_mtime)
            if dt >= cutoff:
                continue
            parts = doc_id.split("/")
            ym = parts[1] if len(parts) >= 3 and re.match(r"^\d{4}-\d{2}$", parts[1]) else dt.strftime("%Y-%m")
            groups.setdefault((ym, proj_slug), []).append({
                "doc_id": doc_id,
                "meta": dict(meta),
                "body": body,
                "dt": dt,
                "proj_slug": proj_slug,
            })

        compacted = 0
        summary_ids: set[str] = set()
        for (ym, proj_slug), items in sorted(groups.items(), key=lambda kv: (kv[0][0], "" if kv[0][1] is None else kv[0][1])):
            if proj_slug is None:
                summary_id = f"receipts/{ym}/_vault-ozet.md"
                label = "genel (kasa)"
            else:
                summary_id = f"receipts/{ym}/{proj_slug}-ozet.md"
                label = proj_slug
            summary_exists = sess.exists(summary_id)
            if len(items) < 2 and not summary_exists:
                continue
            items.sort(key=lambda x: (x["dt"], x["doc_id"]))
            if summary_exists:
                smeta, sbody = parse_markdown(sess.read(summary_id))
                existing_proj = smeta.get("project")
                existing_proj = existing_proj.strip() if isinstance(existing_proj, str) and existing_proj.strip() else None
                if existing_proj != proj_slug:
                    raise VaultError(
                        f"Özet dosyası proje sahibi uyuşmuyor ({summary_id}): beklenen={proj_slug!r}, mevcut={existing_proj!r}"
                    )
                if str(smeta.get("trust") or "trusted") != "trusted":
                    raise VaultError(f"Güvenilmeyen özet dosyası güncellenemez: {summary_id}")
                smeta = dict(smeta)
                _touch(smeta, now)
            else:
                smeta = {
                    "title": f"Makbuz Özeti {ym} ({label})",
                    "kind": "receipt",
                    "project": proj_slug,
                    "status": "active",
                    "trust": "trusted",
                    "compacted_summary": True,
                    "created_at": _iso(now),
                    "updated_at": _iso(now),
                }
                sbody = f"# Makbuz Özeti {ym} ({label})\n"

            lines = sbody.split("\n")
            body_changed = False
            new_bullets: list[str] = []
            for it in items:
                doc_id = it["doc_id"]
                link_ref = f"[[{doc_id[:-3]}]]"
                created_date = str(it["meta"].get("created_at") or "")[:10] or it["dt"].strftime("%Y-%m-%d")
                session = str(it["meta"].get("session") or Path(doc_id).stem)
                short_sum = _receipt_short_summary(it["body"])
                bullet = f"- **{created_date} · {session}** ({link_ref}): {short_sum}"
                marker = f"({link_ref}):"
                matched_idx = next((i for i, ln in enumerate(lines) if marker in ln), None)
                if matched_idx is not None:
                    if lines[matched_idx] != bullet:
                        lines[matched_idx] = bullet
                        body_changed = True
                else:
                    new_bullets.append(bullet)

            if not dry_run:
                if body_changed or new_bullets:
                    merged_body = "\n".join(lines).rstrip("\n")
                    if new_bullets:
                        merged_body = merged_body + "\n\n" + "\n".join(new_bullets)
                    merged_body += "\n"
                    sess.write(summary_id, _meta_doc(smeta, merged_body), "compact_summary",
                               {"count": len(new_bullets), "updated": body_changed})
                for it in items:
                    imeta = it["meta"]
                    imeta["status"] = "archived"
                    imeta["compacted_into"] = summary_id
                    _touch(imeta, now)
                    sess.write(it["doc_id"], _meta_doc(imeta, it["body"]), "compact_receipt",
                               {"summary": summary_id})
            compacted += len(items)
            summary_ids.add(summary_id)
        if not dry_run and compacted > 0:
            sess.sync()
    return {"status": "ok", "dry_run": dry_run, "compacted": compacted, "summaries": sorted(summary_ids)}
