"""Kasa operasyonlari: init, proje kaydi, bootstrap, decide (ADR), rule, patch, finish, okuma/arama."""

from __future__ import annotations

import difflib
import json
import os
import posixpath
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

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
    "read_note",
    "search_memory",
    "sync_index",
    "slugify",
]

UNREGISTERED_MESSAGE = "Bu çalışma alanı hafıza beyaz listesinde değil."
SESSION_LIMIT = 3000
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
LINKS_START = "<!-- mem-links -->"
LINKS_END = "<!-- /mem-links -->"


class VaultError(ValueError):
    """Kasa islemi gecersiz."""


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
    "hideUnresolved": False,
    "showOrphans": True,
    "collapse-color-groups": False,
    "colorGroups": [
        {"query": "file:Core OR path:Kurallar.md", "color": {"a": 1, "rgb": 15381256}},
        {"query": "file:Genel-Bakis", "color": {"a": 1, "rgb": 3899638}},
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


def init_vault(vault_root, profile_name: str, dlp_mode: str | None = None, state_dir=None) -> dict:
    if not config.PROFILE_NAME_RE.match(profile_name or ""):
        raise VaultError(f"Gecersiz profil adi: {profile_name!r}")
    root = Path(os.path.abspath(str(vault_root)))
    mode = dlp_mode or config.default_dlp_mode(profile_name)
    if mode not in config.DLP_MODES:
        raise VaultError(f"Gecersiz dlp_mode: {mode!r}")
    created: list[str] = []
    root.mkdir(parents=True, exist_ok=True)
    meta_path = config.vault_meta_path(root)
    if not meta_path.exists():
        sdir_check = Path(state_dir) if state_dir else config.default_state_dir(profile_name)
        config.ensure_state_outside(root, sdir_check)
        atomic_write_text(meta_path, json.dumps(config.new_vault_meta(profile_name, mode), indent=2) + "\n")
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


def _project_files(slug: str, name: str, description: str, now: str) -> dict[str, str]:
    base = f"projects/{slug}"
    desc = description.strip() or "(açıklama yok)"
    return {
        f"{base}/Genel-Bakis.md": _meta_doc(
            {"title": f"{name} genel bakış", "kind": "overview", "project": slug, "status": "active",
             "created_at": now},
            f"# {name} — Genel Bakış\n\n{desc}\n\n## Bağlantılar\n\n{LINKS_START}\n{LINKS_END}\n",
        ),
        f"{base}/Kurallar.md": _meta_doc(
            {"title": f"{name} kuralları", "kind": "rule", "project": slug, "status": "active",
             "created_at": now},
            f"# {name} — Kurallar\n\n",
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


def register_project(vault_root, slug: str, repo_roots, name: str | None = None,
                     description: str = "", state_dir=None) -> dict:
    if not SLUG_RE.match(slug or ""):
        raise VaultError("Proje slug'i yalnız küçük harf, rakam ve tire içerebilir (örn. order-service).")
    if isinstance(repo_roots, (str, Path)):
        repo_roots = [repo_roots]
    roots = [_norm_root(r) for r in repo_roots]
    if not roots:
        raise VaultError("En az bir repo kökü gerekli.")
    ctx = _ctx(vault_root, state_dir)
    now = _now()
    with _Session(ctx) as sess:
        desc, _ = _shield_all(sess, [description])
        desc = desc[0]
        projects = config.load_projects(ctx.vault_root)
        existing = next((p for p in projects if p["slug"] == slug), None)
        if existing is None:
            entry = {"slug": slug, "name": name or slug, "roots": roots, "description": desc}
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
    return {"status": action, "slug": slug, "name": pname, "roots": entry["roots"]}


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
    doc_id = f"projects/{slug}/Genel-Bakis.md"
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
                            include_archived=include_archived, kinds=klist, reranker=reranker)
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


def _read_if_exists(root: Path, doc_id: str) -> str | None:
    try:
        full = resolve_in_vault(root, doc_id)
        if full.is_file():
            return parse_markdown(full.read_text(encoding="utf-8", errors="replace"))[1].strip()
    except (OSError, PathTraversalError):
        pass
    return None


def _trim_tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: max(0, limit)]
    nl = cut.rfind("\n")
    if nl > limit // 2:
        cut = cut[:nl]
    return cut.rstrip() + "\n…(kısaltıldı; tamamı için memory_read kullan)"


def bootstrap_context(vault_root, state_dir=None, cwd=None, project: str | None = None,
                      task_hint: str | None = None, budget_chars: int = 8000) -> dict:
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
        grules = _read_if_exists(root, "Kurallar.md") or ""
        grules = "\n".join(
            ln for ln in grules.split("\n")
            if not (_POINTER_RE.match(ln) and _POINTER_RE.match(ln).group(1).strip() != slug)
        ).strip()
        prules = _read_if_exists(root, f"projects/{slug}/Kurallar.md") if slug else None
        threads = _read_if_exists(root, f"projects/{slug}/Acik-Konular.md") if slug else None
        last = _read_if_exists(root, f"projects/{slug}/Son-Oturum.md") if slug else None
        sources = ["Core.md", "Kurallar.md"]
        relevant: list[dict] = []
        if task_hint and task_hint.strip():
            skip = {"Core.md", "Kurallar.md"}
            if slug:
                skip |= {f"projects/{slug}/{n}" for n in ("Kurallar.md", "Acik-Konular.md", "Son-Oturum.md")}
            for r in search_vault(conn, task_hint, project=slug, limit=10):
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
    optional: list[tuple[str, str]] = []
    if relevant:
        rel = "\n\n".join(f"- **{r['title']}** (`{r['doc_id']}`)\n  {r['snippet']}" for r in relevant)
        optional.append(("## İlgili notlar (görev ipucu)", rel))
        sources.extend(r["doc_id"] for r in relevant)
    if last is not None and last.strip():
        optional.append((f"## Son oturum ({slug})", last))
        sources.append(f"projects/{slug}/Son-Oturum.md")
    if threads is not None:
        optional.append((f"## Açık konular ({slug})", threads))
        sources.append(f"projects/{slug}/Acik-Konular.md")
    for idx, (heading, content) in enumerate(optional):
        remaining = budget_chars - len(text) - len(heading) - 4
        if remaining < 80:
            text += f"\n\n{heading}\n\n(bütçe nedeniyle atlandı; memory_read ile oku)"
            continue
        left = len(optional) - idx
        share = remaining if left <= 1 else max(1200, remaining // left)
        text += f"\n\n{heading}\n\n" + _trim_tail(content, min(remaining, share))
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


def record_decision(vault_root, project: str | None, title: str, decision: str, context: str = "",
                    alternatives: str = "", rationale: str = "", supersedes: list[str] | None = None,
                    sources: list[str] | None = None, tags: list[str] | None = None,
                    force: bool = False, state_dir=None, actor: str = "mem") -> dict:
    if not (title or "").strip() or not (decision or "").strip():
        raise VaultError("title ve decision zorunlu.")
    ctx = _ctx(vault_root, state_dir)
    slug = None if project in (None, "", "shared") else project
    if slug is not None and config.get_project(ctx.vault_root, slug) is None:
        raise VaultError(f"Bilinmeyen proje: {slug!r}")
    folder = f"projects/{slug}/kararlar" if slug else "knowledge/kararlar"
    now = _now()
    with _Session(ctx, actor) as sess:
        values, cats = _shield_all(
            sess, [title, decision, context, alternatives, rationale] + list(sources or []) + list(tags or [])
        )
        title, decision, context, alternatives, rationale = [v.strip() for v in values[:5]]
        n_src = len(sources or [])
        sources_c = values[5 : 5 + n_src]
        tags_c = values[5 + n_src :]
        sess.sync()
        old_ids = [_resolve_ref(sess, s, folder) for s in (supersedes or [])]

        if not force and not old_ids:
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

        folder = f"projects/{slug}/kararlar" if slug else "knowledge/kararlar"
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
            lines += ["", f"Proje: [[projects/{slug}/Genel-Bakis|{pname} genel bakış]]"]
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


# ---------------------------------------------------------------- kural


def _rule_text(line: str) -> str:
    m = re.match(r"^\s*-\s+\*\*\[[^\]]*\]\*\*\s+(.*?)(?:\s+\*\(Gerekçe:.*\)\*)?\s*$", line)
    return fold_tr(m.group(1)) if m else ""


def add_rule(vault_root, scope: str, rule_text: str, reason: str = "", state_dir=None,
             actor: str = "mem") -> dict:
    if not (rule_text or "").strip():
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
        (text, rsn), cats = _shield_all(sess, [rule_text, reason])
        text = " ".join(text.split())
        rsn = " ".join(rsn.split())
        line = f"- **[{now.strftime('%Y-%m-%d')}]** {text}" + (f" *(Gerekçe: {rsn})*" if rsn else "")
        current = sess.read(doc_id)
        key = fold_tr(text)
        if any(_rule_text(ln) == key for ln in current.split("\n")):
            return {"status": "duplicate", "path": doc_id, "line": line}
        sha = sess.write(doc_id, current.rstrip("\n") + "\n" + line + "\n", "rule")
        sess.sync()
    return {"status": "added", "path": doc_id, "line": line, "sha256": sha, "dlp_categories": cats}


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


def finish_session(vault_root, cwd_or_project, summary: str, open_threads: list[str] | None = None,
                   refs: list[str] | None = None, session_id: str | None = None,
                   harness: str = "mcp", state_dir=None) -> dict:
    if not (summary or "").strip():
        raise VaultError("summary boş olamaz.")
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
        vals, cats = _shield_all(sess, [summary] + list(open_threads or []) + list(refs or []))
        summary_c = vals[0].strip()
        n_thr = len(open_threads or [])
        threads_c = [" ".join(t.split()) for t in vals[1 : 1 + n_thr]]
        refs_c = [" ".join(r.split()) for r in vals[1 + n_thr :]]
        if session_id:
            key = f"{slug}|{harness}|{session_id}"
            tag = f"{harness}-{slugify(session_id, 24)}"
        else:
            key = f"{slug}|{harness}|{summary_c}"
            tag = None
        event_hash = sha256_bytes(key.encode("utf-8"))
        tag = tag or f"{harness}-{event_hash[:8]}"
        month_dir = f"receipts/{now.strftime('%Y-%m')}"
        receipt_id = f"{month_dir}/{event_hash[:16]}.md"
        rdir = ctx.vault_root / "receipts"
        if rdir.is_dir():
            for found in rdir.glob(f"*/{event_hash[:16]}.md"):
                receipt_id = found.relative_to(ctx.vault_root).as_posix()
        created = _iso(now)
        if sess.exists(receipt_id):
            created = str(parse_markdown(sess.read(receipt_id))[0].get("created_at") or created)
        proj_ref = f"[[projects/{slug}/Genel-Bakis|{slug}]]" if slug else "[[Core|genel]]"
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
                  "archived": [], "dlp_categories": cats}

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
            while len(text) > SESSION_LIMIT and len(cards) > 1:
                oldest = cards.pop()
                archived.append(_archive_card(sess, slug, oldest, now))
                text = _compose_session(smeta, header, cards)
            old_text = sess.read(son_id)
            if text != old_text:
                sess.write(son_id, text, "session_card", {"session": tag, "archived": len(archived)})
            result["archived"] = sorted(set(archived))

            if open_threads is not None:
                tid = f"projects/{slug}/Acik-Konular.md"
                tmeta, _tb = parse_markdown(sess.read(tid))
                _touch(tmeta, now)
                name = scope["name"] or slug
                items = "\n".join(f"- [ ] {t}" for t in threads_c) if threads_c else "(açık konu yok)"
                sess.write(tid, _meta_doc(tmeta, f"# {name} — Açık Konular\n\n{items}\n"), "threads")
        sess.sync()
    return result
