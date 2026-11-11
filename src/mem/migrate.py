"""Eski beyin_v3 kasasindan tek yonlu, salt-okunur `import-v3` tasiyici."""

from __future__ import annotations

import os
import re
from pathlib import Path

from . import config, dlp, store
from .config import is_within
from .frontmatter import dump_markdown, parse_markdown
from .lock import resolve_in_vault
from .turkish import fold_tr
from .vault import (
    LINKS_END,
    LINKS_START,
    _CORE_BODY,
    _ctx,
    _iso,
    _now,
    _project_files,
    _refresh_hub,
    _Session,
    slugify,
)

_SRC_DIRS = ("notes", "knowledge", "projects")
_KEEP_FROM_EXISTING = ("title", "kind", "project", "status", "created_at")
_LEGACY_HUB_RE = re.compile(r"<!--\s*beyin-baglantilar\s*-->([\s\S]*?)<!--\s*beyin-baglantilar\s*-->")
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)((?:#[^\]|]*)?(?:\|[^\]]*)?)\]\]")
_YM_RE = re.compile(r"^(\d{4}-\d{2})")


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", fold_tr(text))


def _slug_for(folder: str, projects: list[dict]) -> str:
    """v3 klasor adini kayitli projeye esler (`OrderService` == `order-service`); yoksa slugify."""
    n = _norm(folder)
    for p in projects:
        if _norm(p["slug"]) == n or _norm(p.get("name", "")) == n:
            return p["slug"]
    return slugify(folder)


def _map_target(rel: str, projects: list[dict], receipt_map: dict[str, str] | None = None) -> str | None:
    """v3 goreli yolu -> yeni kasa yolu."""
    clean = rel.strip().replace("\\", "/").lstrip("/")
    if receipt_map and clean in receipt_map:
        return receipt_map[clean]
    if receipt_map and f"{clean}.md" in receipt_map:
        return receipt_map[f"{clean}.md"]
    parts = clean.split("/")
    if parts[0] == "notes":
        if len(parts) >= 4 and parts[1] == "projects":
            return "/".join(["projects", _slug_for(parts[2], projects)] + parts[3:])
        return "/".join(["knowledge", "notes"] + parts[1:])
    if parts[0] == "projects" and len(parts) >= 3:
        return "/".join(["projects", _slug_for(parts[1], projects)] + parts[2:])
    if parts[0] == "knowledge":
        return clean
    return None


def _rewrite_wikilinks(body: str, projects: list[dict], receipt_map: dict[str, str]) -> str:
    def _sub(match: re.Match) -> str:
        raw_target = match.group(1).strip()
        suffix = match.group(2) or ""
        has_md = raw_target.endswith(".md")
        probe = raw_target if has_md else f"{raw_target}.md"
        mapped = _map_target(probe, projects, receipt_map)
        if not mapped:
            return match.group(0)
        new_target = mapped if (has_md and not raw_target.startswith("receipts/")) else mapped[:-3] if mapped.endswith(".md") else mapped
        return f"[[{new_target}{suffix}]]"

    return _WIKILINK_RE.sub(_sub, body)


def _upgrade_legacy_hub(body: str, is_overview: bool) -> str:
    if is_overview:
        if _LEGACY_HUB_RE.search(body):
            return _LEGACY_HUB_RE.sub(f"{LINKS_START}\n{LINKS_END}", body, count=1)
        return body
    return _LEGACY_HUB_RE.sub(lambda m: m.group(1).strip(), body)


def _infer_kind(target: str, current_kind: str | None) -> str:
    if target.endswith("/Genel-Bakis.md"):
        return "overview"
    if target.endswith("/Kurallar.md") or target == "Kurallar.md":
        return "rule"
    if target.endswith("/Acik-Konular.md"):
        return "threads"
    if target.endswith("/Son-Oturum.md"):
        return "session"
    if "/kararlar/" in f"/{target}":
        return "decision"
    if target.startswith("receipts/"):
        return "receipt"
    if current_kind and current_kind not in ("fact", "note"):
        return current_kind
    if target.startswith("knowledge/"):
        return "knowledge"
    return current_kind or "note"


def _strip_hub(body: str) -> str:
    return re.sub(re.escape(LINKS_START) + r"[\s\S]*?" + re.escape(LINKS_END), LINKS_START + LINKS_END, body)


def _is_pristine_scaffold(existing_text: str, target: str, projects: list[dict]) -> bool:
    """Hedef, `init` veya `project add` iskeletinden hic degismemisse (hub blogu haric) True."""
    if target == "Core.md":
        return parse_markdown(existing_text)[1].strip() == _CORE_BODY.strip()
    parts = target.split("/")
    if len(parts) != 3 or parts[0] != "projects":
        return False
    proj = next((p for p in projects if p["slug"] == parts[1]), None)
    if proj is None:
        return False
    tmpl = _project_files(proj["slug"], proj.get("name", proj["slug"]), proj.get("description", ""), "")
    if target not in tmpl:
        return False
    return _strip_hub(parse_markdown(existing_text)[1]).strip() == _strip_hub(parse_markdown(tmpl[target])[1]).strip()


def _collect_receipts(src: Path, now_ym: str) -> tuple[list[tuple[str, Path, str]], dict[str, str]]:
    """v3 receipts/*.md dosyalarini tarar ve eski->yeni yol haritasini cikarir."""
    rdir = src / "receipts"
    items: list[tuple[str, Path, str]] = []
    rmap: dict[str, str] = {}
    if not rdir.is_dir() or not is_within(rdir, src):
        return items, rmap
    used: set[str] = set()
    for rel, full, _st in store.iter_markdown(rdir):
        if not is_within(full, src):
            continue
        text = full.read_text(encoding="utf-8", errors="replace")
        meta, _ = parse_markdown(text)
        if meta.get("generated") is True:
            continue
        m = _YM_RE.match(str(meta.get("created_at") or ""))
        ym = m.group(1) if m else now_ym
        stem = re.sub(r"[^a-zA-Z0-9_-]", "", Path(rel).stem)[:16] or "receipt"
        target = f"receipts/{ym}/{stem}.md"
        idx = 2
        while target in used:
            target = f"receipts/{ym}/{stem}-{idx}.md"
            idx += 1
        used.add(target)
        src_rel = f"receipts/{rel}"
        items.append((src_rel, full, target))
        rmap[src_rel] = target
    return items, rmap


def _collect_companion(src: Path) -> list[tuple[str, Path, str, str]]:
    """`🔮 850-Companion` gibi klasorlerden Core, Last-Session ve Threads dosyalarini toplar."""
    out: list[tuple[str, Path, str, str]] = []
    try:
        entries = sorted(src.iterdir(), key=lambda p: p.name)
    except OSError:
        return out
    for entry in entries:
        if entry.is_symlink() or not entry.is_dir() or not is_within(entry, src):
            continue
        if "companion" not in entry.name.lower():
            continue
        mapping = (
            ("Core.md", "Core.md", "overview"),
            ("Last-Session.md", "knowledge/companion/Son-Oturum-Gecmisi.md", "session"),
            ("Threads.md", "knowledge/companion/Genel-Acik-Konular.md", "threads"),
        )
        for fname, target, kind in mapping:
            f = entry / fname
            if f.is_file() and not f.is_symlink() and is_within(f, src):
                out.append((f"{entry.name}/{fname}", f, target, kind))
        break
    return out


def import_from_beyin_v3(source_vault, target_vault, dry_run: bool = False, state_dir=None) -> dict:
    src = Path(os.path.abspath(str(source_vault)))
    dst = Path(os.path.abspath(str(target_vault)))
    if not src.is_dir():
        raise FileNotFoundError(f"Kaynak kasa bulunamadı: {src}")
    if is_within(dst, src) or is_within(src, dst):
        raise ValueError("Kaynak ve hedef kasa iç içe/aynı olamaz.")
    ctx = _ctx(dst, state_dir)
    projects = config.load_projects(ctx.vault_root)
    result = {
        "imported": [],
        "replaced_scaffold": [],
        "skipped_existing": [],
        "skipped_generated": [],
        "blocked": [],
        "unmapped": 0,
        "unregistered_projects": [],
        "dry_run": dry_run,
    }
    now_dt = _now()
    now = _iso(now_dt)
    receipt_items, receipt_map = _collect_receipts(src, now_dt.strftime("%Y-%m"))
    candidates: list[tuple[str, Path, str | None, str | None]] = []
    for top in _SRC_DIRS:
        base = src / top
        if not base.is_dir() or not is_within(base, src):
            continue
        for rel, full, _st in store.iter_markdown(base):
            if is_within(full, src):
                candidates.append((f"{top}/{rel}", full, None, None))
    for src_rel, full, target, kind in _collect_companion(src):
        candidates.append((src_rel, full, target, kind))
    for src_rel, full, target in receipt_items:
        candidates.append((src_rel, full, target, "receipt"))

    projects_seen: set[str] = set()
    superseded_targets: set[str] = set()
    with _Session(ctx, "import-v3") as sess:
        for rel, full, preset_target, preset_kind in candidates:
            target = preset_target or _map_target(rel, projects, receipt_map)
            if target is None:
                result["unmapped"] += 1
                continue
            try:
                resolve_in_vault(dst, target)
            except ValueError:
                result["unmapped"] += 1
                continue
            text = full.read_text(encoding="utf-8", errors="replace")  # salt-okunur
            meta, body = parse_markdown(text)
            if meta.get("generated") is True:
                result["skipped_generated"].append(rel)
                continue
            meta = dict(meta)
            meta["kind"] = preset_kind or _infer_kind(target, meta.get("kind"))
            if target.startswith("projects/"):
                slug = target.split("/")[1]
                projects_seen.add(slug)
                meta["project"] = slug
            if meta["kind"] == "decision":
                if "decision_date" in meta and "date" not in meta:
                    meta["date"] = meta.pop("decision_date")
                meta.setdefault("date", Path(target).name[:10])
                meta.setdefault("status", "active")
            if isinstance(meta.get("supersedes"), list):
                mapped_sup = []
                for item in meta["supersedes"]:
                    s = str(item).strip()
                    ms = _map_target(s if s.endswith(".md") else f"{s}.md", projects, receipt_map) or s
                    mapped_sup.append(ms)
                    superseded_targets.add(ms)
                meta["supersedes"] = mapped_sup
            if isinstance(meta.get("refs"), list):
                mapped_refs = []
                ref_projects: set[str] = set()
                for item in meta["refs"]:
                    s = str(item).strip()
                    mr = _map_target(s if s.endswith(".md") else f"{s}.md", projects, receipt_map) or s
                    mapped_refs.append(mr)
                    if mr.startswith("projects/") and len(mr.split("/")) >= 2:
                        ref_projects.add(mr.split("/")[1])
                meta["refs"] = mapped_refs
                if meta["kind"] == "receipt" and len(ref_projects) == 1 and "project" not in meta:
                    meta["project"] = next(iter(ref_projects))
            meta.setdefault("title", Path(target).stem)
            meta["imported_from"] = "beyin_v3"
            meta.setdefault("imported_at", now)

            is_overview = target.endswith("/Genel-Bakis.md")
            body = _upgrade_legacy_hub(body, is_overview)
            body = _rewrite_wikilinks(body, projects, receipt_map)

            replace_scaffold = False
            if sess.exists(target):
                existing = sess.read(target)
                if _is_pristine_scaffold(existing, target, projects):
                    replace_scaffold = True
                    emeta = parse_markdown(existing)[0]
                    for k in _KEEP_FROM_EXISTING:
                        if k in emeta:
                            meta[k] = emeta[k]
                else:
                    result["skipped_existing"].append(target)
                    continue
            if is_overview and LINKS_START not in body:
                body = body.rstrip("\n") + f"\n\n## Bağlantılar\n\n{LINKS_START}\n{LINKS_END}\n"

            out_text = dump_markdown(meta, body)
            try:
                out_text, _findings = dlp.inspect_and_apply(out_text, ctx.dlp_mode, ctx.vault_root)
            except dlp.DLPBlockedError as exc:
                result["blocked"].append(
                    {"source": rel, "categories": sorted({f["category"] for f in exc.findings})}
                )
                continue
            if not dry_run:
                sess.write(target, out_text, "import_v3", {"source": rel})
            result["imported"].append(target)
            if replace_scaffold:
                result["replaced_scaffold"].append(target)

        if not dry_run and superseded_targets:
            for sup_doc in sorted(superseded_targets):
                try:
                    resolve_in_vault(dst, sup_doc)
                except ValueError:
                    continue
                if sess.exists(sup_doc):
                    smeta, sbody = parse_markdown(sess.read(sup_doc))
                    if smeta.get("status") != "superseded":
                        smeta = dict(smeta)
                        smeta["status"] = "superseded"
                        sess.write(sup_doc, dump_markdown(smeta, sbody), "import_v3_supersede")

        if not dry_run and result["imported"]:
            sess.sync()
            registered = {p["slug"] for p in projects}
            for slug in sorted(projects_seen & registered):
                _refresh_hub(sess, slug)
            sess.sync()
    result["unregistered_projects"] = sorted(p for p in projects_seen if p not in {q["slug"] for q in projects})
    return result
