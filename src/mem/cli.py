"""mem CLI: tum alt komutlar (okunabilir metin veya --json)."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

from . import __version__, config, harness, migrate, store, vault
from .dlp import DLPBlockedError
from .frontmatter import parse_markdown
from .lock import ConcurrencyConflictError, PathTraversalError, VaultLock

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_DLP, EXIT_CONFLICT, EXIT_UNREGISTERED = 0, 1, 2, 3, 4, 5


def _out(args, data, text: str | None = None) -> None:
    if getattr(args, "json", False):
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(text if text is not None else json.dumps(data, ensure_ascii=False, indent=2))


def _vault_ctx(args) -> tuple[Path, Path | None]:
    if getattr(args, "vault", None):
        root = Path(args.vault)
        return root, None
    prof = config.get_profile(getattr(args, "profile", None))
    return Path(prof.vault_root), Path(prof.state_dir)


def _advisor_hooks(args, root: Path, sd: Path | None):
    """(reranker, advisor): yalniz acik istek/MEM_ADVISOR/profil ayari + personal/non-block ise baglanir."""
    explicit = getattr(args, "advisor", None)
    if explicit == "off":
        return None, None
    mode = config.effective_advisor_mode(root, explicit)  # work + --advisor gemini -> ValueError
    if mode != "gemini":
        return None, None
    from mem_advisor.gemini_decisions import build_for_profile

    name, dlp_mode = config.advisor_identity(root)
    return build_for_profile(mode, root, config.resolve_state_dir(root, sd), name, dlp_mode)


def _scope_or_exit(args, root: Path):
    project = getattr(args, "project", None)
    explicit_cwd = getattr(args, "cwd", None)
    cwd = explicit_cwd or (None if project else os.getcwd())
    return vault.resolve_scope(root, cwd=cwd, project=project)


def _unregistered(args, scope: dict) -> int:
    _out(args, scope, f"{scope['message']} (cwd: {scope.get('cwd')}) — hiçbir şey yazılmadı.")
    return EXIT_UNREGISTERED


# ---------------------------------------------------------------- komutlar


def cmd_init(args) -> int:
    name = args.profile or "work"
    root = Path(args.vault) if args.vault else config.default_vault_dir(name)
    mode = args.dlp
    prof = config.create_profile(name, root, dlp_mode=mode, advisor=args.advisor)
    res = vault.init_vault(root, name, dlp_mode=prof.dlp_mode, state_dir=prof.state_dir, advisor=prof.advisor)
    _out(args, res, f"Kasa hazır: {res['vault_root']}\nProfil: {name} (dlp: {res['dlp_mode']}"
                    f"{', advisor: ' + prof.advisor if prof.advisor != 'off' else ''})\n"
                    f"State: {res['state_dir']}\nOluşturulan: {', '.join(res['created']) or '-'}")
    return EXIT_OK


def cmd_project_add(args) -> int:
    root, sd = _vault_ctx(args)
    roots = args.root or [os.getcwd()]
    res = vault.register_project(root, args.slug, roots, name=args.name, description=args.description or "",
                                 state_dir=sd, tags=args.tag)
    _out(args, res, f"Proje {res['status']}: {res['slug']} ({res['name']})\nKökler: {', '.join(res['roots'])}")
    return EXIT_OK


def cmd_project_list(args) -> int:
    root, _ = _vault_ctx(args)
    projects = config.load_projects(root)
    _out(args, projects, "\n".join(f"{p['slug']}\t{p.get('name', '')}\t{'; '.join(p.get('roots', []))}"
                                   for p in projects) or "(kayıtlı proje yok)")
    return EXIT_OK


def cmd_decide(args) -> int:
    root, sd = _vault_ctx(args)
    scope = _scope_or_exit(args, root)
    if scope["status"] != "ok":
        return _unregistered(args, scope)
    _reranker, advisor = _advisor_hooks(args, root, sd)
    res = vault.record_decision(
        root, None if args.shared else scope["project"], args.title, args.decision, context=args.context or "",
        alternatives=args.alternatives or "", rationale=args.rationale or "", supersedes=args.supersedes,
        sources=args.source, tags=args.tag, force=args.force, state_dir=sd, actor="cli",
        advisor=advisor)
    if res["status"] == "advisor_warning":
        warn_lines = "\n".join(f"  - {w}" for w in res["warnings"])
        _out(args, res, f"DANIŞMAN UYARISI: {res['message']}\n{warn_lines}\nMevcut: {res.get('existing') or '-'}")
        return EXIT_OK
    if res["status"] == "duplicate_warning":
        _out(args, res, f"UYARI: {res['message']}\nMevcut: {res['existing']}")
        return EXIT_OK
    _out(args, res, f"Karar kaydedildi: {res['path']}" + (f"\nYerine geçti: {', '.join(res['superseded'])}"
                                                          if res["superseded"] else ""))
    return EXIT_OK


def cmd_rule(args) -> int:
    root, sd = _vault_ctx(args)
    scope = _scope_or_exit(args, root)
    if scope["status"] != "ok":
        return _unregistered(args, scope)
    rscope = args.scope or scope["project"] or "global"
    if scope["scope"] == "project" and rscope not in ("global", scope["project"]):
        raise vault.VaultError(f"Bu çalışma alanı '{scope['project']}' projesine ait; '{rscope}' kapsamına yazılamaz.")
    res = vault.add_rule(root, rscope, args.rule, args.reason or "", state_dir=sd, actor="cli",
                         tags=args.tag, supersedes=args.supersedes, remove=args.remove)
    if res["status"] == "removed":
        _out(args, res, f"Kural kaldırıldı: {res['path']}\n{res['removed_line']}")
    else:
        _out(args, res, f"Kural {res['status']}: {res['path']}\n{res['line']}")
    return EXIT_OK


def cmd_search(args) -> int:
    root, sd = _vault_ctx(args)
    scope = _scope_or_exit(args, root)
    if scope["status"] != "ok":
        return _unregistered(args, scope)
    reranker, _advisor = _advisor_hooks(args, root, sd)
    res = vault.search_memory(root, " ".join(args.query), project=scope["project"], limit=args.limit,
                              include_superseded=args.include_superseded,
                              include_archived=args.include_archived, state_dir=sd, reranker=reranker)
    lines = []
    for i, r in enumerate(res, 1):
        lines.append(f"{i}. [{r['kind']}] {r['title']}  ({r['doc_id']})  skor={r['score']:.4g}\n   {r['snippet']}")
    _out(args, res, "\n".join(lines) or "(sonuç yok)")
    return EXIT_OK


def cmd_read(args) -> int:
    root, _ = _vault_ctx(args)
    scope = _scope_or_exit(args, root)
    if scope["status"] != "ok":
        return _unregistered(args, scope)
    res = vault.read_note(root, args.path, project=scope["project"])
    _out(args, res, f"sha256: {res['sha256']}\n\n{res['content']}")
    return EXIT_OK


def cmd_bootstrap(args) -> int:
    root, sd = _vault_ctx(args)
    scope = _scope_or_exit(args, root)
    if scope["status"] != "ok":
        return _unregistered(args, scope)
    reranker, _advisor = _advisor_hooks(args, root, sd)
    res = vault.bootstrap_context(root, sd, project=scope["project"], task_hint=args.task,
                                  budget_chars=args.budget, reranker=reranker)
    _out(args, res, res["text"])
    return EXIT_OK


def cmd_finish(args) -> int:
    root, sd = _vault_ctx(args)
    scope = _scope_or_exit(args, root)
    if scope["status"] != "ok":
        return _unregistered(args, scope)
    target = scope["project"] or str(root)
    res = vault.finish_session(root, target, args.summary, open_threads=args.thread, refs=args.ref,
                               session_id=args.session, harness=args.harness, state_dir=sd,
                               close_threads=args.close_thread, threads_mode=args.threads_mode)
    _out(args, res, f"Makbuz: {res['receipt']}\nOturum: {res['session_tag']}"
                    + (f"\nArşive taşındı: {', '.join(res['archived'])}" if res["archived"] else "")
                    + (f"\nKapatılamayan (eşleşmedi): {', '.join(res['unmatched_close'])}"
                       if res.get("unmatched_close") else ""))
    return EXIT_OK


def cmd_compact(args) -> int:
    root, sd = _vault_ctx(args)
    res = vault.compact_receipts(root, older_than_days=args.days, project=args.project,
                                 dry_run=args.dry_run, state_dir=sd)
    _out(args, res, f"Sıkıştırılan makbuz: {res['compacted']}"
                    + (f"\nÖzetler: {', '.join(res['summaries'])}" if res["summaries"] else ""))
    return EXIT_OK


def cmd_patch(args) -> int:
    root, sd = _vault_ctx(args)
    scope = _scope_or_exit(args, root)
    if scope["status"] != "ok":
        return _unregistered(args, scope)
    res = vault.patch_note(root, args.path, args.sha256, [{"old": args.old, "new": args.new}], state_dir=sd,
                           project=scope["project"], actor="cli")
    _out(args, res, f"Yamalandı: {res['path']}\nYeni sha256: {res['sha256']}")
    return EXIT_OK


def cmd_sync(args) -> int:
    root, sd = _vault_ctx(args)
    res = vault.sync_index(root, sd)
    _out(args, res, ", ".join(f"{k}={v}" for k, v in res.items()))
    return EXIT_OK


def cmd_connect(args) -> int:
    res = harness.connect_harness(args.harness, args.profile or "work", config_root_override=args.config_root,
                                  dry_run=args.dry_run, uninstall=args.uninstall,
                                  advisor=args.advisor, install_skill=args.with_skill)
    lines = [f"{f['action']:10} {f['path']}" for f in res["files"]]
    _out(args, res, "\n".join(lines))
    return EXIT_OK


def _parse_project_spec(spec: str) -> tuple[str, str]:
    if "=" in spec:
        left, right = spec.split("=", 1)
        if left.strip() and "/" not in left and "\\" not in left and ":" not in left:
            if not right.strip():
                raise vault.VaultError(f"Geçersiz --project değeri (boş yol): {spec!r}")
            return vault.slugify(left.strip(), 63), right
    raw_path = spec
    slug = vault.slugify(Path(raw_path).resolve().name, 63)
    return slug, raw_path


def _resolve_profileless_owner() -> str:
    try:
        return config.get_profile(None).name
    except config.ProfileError:
        return "<belirsiz>"


def cmd_setup(args) -> int:
    existing_profiles = {p.name: p for p in config.list_profiles()}
    if args.profile:
        profile_name = args.profile
    else:
        try:
            profile_name = config.get_profile(None).name
        except config.ProfileError:
            if existing_profiles:
                raise
            profile_name = "work"

    if not config.PROFILE_NAME_RE.match(profile_name or ""):
        raise config.ProfileError(f"Gecersiz profil adi: {profile_name!r}")

    if profile_name in existing_profiles:
        mode = "upgraded"
        prof = existing_profiles[profile_name]
        root = Path(args.vault) if args.vault else Path(prof.vault_root)
        mpath = config.vault_meta_path(root)
        vmeta = config.read_vault_meta(root) if mpath.exists() else None
        if vmeta is not None:
            vprof = (vmeta.get("profile") or "").strip()
            if vprof and vprof != profile_name:
                raise ValueError(f"Hedef dizinde '{vprof}' profiline ait kasa zaten var: {root}")
        stored_dlp = (vmeta.get("dlp_mode") if vmeta else None) or prof.dlp_mode
        dlp_mode = args.dlp if args.dlp else stored_dlp
        stored_adv = (vmeta.get("advisor") if vmeta and "advisor" in vmeta else None) or prof.advisor
        adv_source = args.advisor if args.advisor is not None else (
            "off" if not config.can_use_advisor(profile_name, dlp_mode) else stored_adv
        )
        adv = config.validate_advisor_mode(adv_source, profile_name, dlp_mode)
        state_dir = Path(prof.state_dir)
        config.ensure_state_outside(root, state_dir)
        if not args.dry_run:
            if args.vault or args.dlp or args.advisor:
                prof = config.create_profile(profile_name, root, state_dir=prof.state_dir,
                                             dlp_mode=dlp_mode, advisor=adv)
                if mpath.exists():
                    meta = config.read_vault_meta(root)
                    if args.dlp:
                        meta["dlp_mode"] = dlp_mode
                    if adv == "off":
                        meta.pop("advisor", None)
                    else:
                        meta["advisor"] = adv
                    from .lock import atomic_write_text
                    atomic_write_text(mpath, json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
            vault.init_vault(root, profile_name, dlp_mode=dlp_mode, state_dir=prof.state_dir,
                             advisor=adv)
            state_dir = Path(prof.state_dir)
    else:
        mode = "installed"
        root = Path(args.vault) if args.vault else config.default_vault_dir(profile_name)
        mpath = config.vault_meta_path(root)
        if mpath.exists():
            vmeta = config.read_vault_meta(root)
            vprof = (vmeta.get("profile") or "").strip()
            vdlp = vmeta.get("dlp_mode") or config.default_dlp_mode(vprof or profile_name)
            if vprof and vprof != profile_name:
                raise ValueError(f"Hedef dizinde '{vprof}' profiline ait kasa zaten var: {root}")
            if args.dlp and args.dlp != vdlp:
                raise ValueError(f"Hedef kasadaki dlp_mode ({vdlp!r}) istenen --dlp ({args.dlp!r}) ile çelişiyor: {root}")
            dlp_mode = vdlp
            adv_req = args.advisor if args.advisor is not None else vmeta.get("advisor")
        else:
            dlp_mode = args.dlp or config.default_dlp_mode(profile_name)
            adv_req = args.advisor
        adv = config.validate_advisor_mode(adv_req, profile_name, dlp_mode)
        state_dir = config.default_state_dir(profile_name)
        config.ensure_state_outside(root, state_dir)
        if not args.dry_run:
            prof = config.create_profile(profile_name, root, dlp_mode=dlp_mode, advisor=adv)
            vault.init_vault(root, profile_name, dlp_mode=prof.dlp_mode, state_dir=prof.state_dir,
                             advisor=prof.advisor)
            state_dir = Path(prof.state_dir)
            dlp_mode = prof.dlp_mode

    projects_added: list[dict] = []
    for spec in args.project or []:
        slug, raw_path = _parse_project_spec(spec)
        norm_root = vault._norm_root(raw_path)
        if args.dry_run:
            projects_added.append({"status": "dry_run", "slug": slug, "roots": [norm_root]})
        else:
            projects_added.append(vault.register_project(root, slug, [norm_root], state_dir=state_dir))

    unconnected_detected: list[str] = []
    skipped_other_profile: list[dict] = []
    skipped_unmanaged: list[dict] = []
    if args.harness:
        target_harnesses = list(dict.fromkeys(args.harness))
    else:
        target_harnesses = []
        for h in harness.detect_installed_harnesses(args.config_root):
            cprof = harness.connected_harness_profile(h, args.config_root)
            if cprof is None:
                if args.all_harnesses:
                    target_harnesses.append(h)
                else:
                    unconnected_detected.append(h)
            else:
                owner = cprof if cprof else _resolve_profileless_owner()
                if owner == profile_name:
                    target_harnesses.append(h)
                else:
                    skipped_other_profile.append({"harness": h, "profile": owner})

    connected: list[dict] = []
    for h in target_harnesses:
        try:
            connected.append(
                harness.connect_harness(
                    h,
                    profile_name,
                    config_root_override=args.config_root,
                    dry_run=args.dry_run,
                    advisor=args.advisor,
                    install_skill=not args.no_skill,
                    proposed_dlp=dlp_mode,
                )
            )
        except ValueError as exc:
            if not args.harness and "[mcp_servers.mem]" in str(exc):
                skipped_unmanaged.append({"harness": h, "reason": str(exc)})
            else:
                raise

    sync_res = None if args.dry_run else vault.sync_index(root, state_dir)
    out_mode = "dry_run" if args.dry_run else mode
    out = {
        "mode": out_mode,
        "profile": profile_name,
        "vault_root": str(root),
        "state_dir": str(state_dir),
        "dlp_mode": dlp_mode,
        "connected": connected,
        "unconnected_detected": unconnected_detected,
        "skipped_other_profile": skipped_other_profile,
        "skipped_unmanaged": skipped_unmanaged,
        "projects_added": projects_added,
        "sync": sync_res,
    }
    lines = [
        f"Mod: {out_mode} | Profil: {profile_name} (dlp: {dlp_mode})",
        f"Kasa: {root}",
        f"State: {state_dir}",
    ]
    if projects_added:
        lines.append("Projeler: " + ", ".join(f"{p['slug']} ({p['status']})" for p in projects_added))
    if connected:
        lines.append("Bağlanan/güncellenen istemciler:")
        for c in connected:
            fsum = ", ".join(f"{Path(f['path']).name}:{f['action']}" for f in c["files"])
            lines.append(f"  - {c['harness']}: {fsum}")
    else:
        lines.append("Bağlanan istemci: (yok)")
    if unconnected_detected:
        lines.append(
            "Algılanan ancak henüz bağlı olmayan istemciler (bağlamak için --harness <ad> veya --all-harnesses): "
            + ", ".join(unconnected_detected)
        )
    if skipped_other_profile:
        lines.append(
            "Başka profile bağlı olduğu için atlananlar: "
            + ", ".join(f"{s['harness']} ({s['profile']})" for s in skipped_other_profile)
        )
    if skipped_unmanaged:
        lines.append(
            "Elle yönetildiği için atlananlar: "
            + ", ".join(f"{s['harness']}" for s in skipped_unmanaged)
        )
    if sync_res is not None:
        lines.append("İndeks: " + ", ".join(f"{k}={v}" for k, v in sync_res.items()))
    if connected and not args.dry_run:
        lines.append("Not: Açık IDE/masaüstü istemcilerini değişikliklerin geçerli olması için yeniden başlatın.")
    _out(args, out, "\n".join(lines))
    return EXIT_OK


def cmd_import_v3(args) -> int:
    root, sd = _vault_ctx(args)
    res = migrate.import_from_beyin_v3(Path(args.source), root, dry_run=args.dry_run, state_dir=sd)
    _out(args, res, f"Aktarılan: {len(res['imported'])}  Atlanan(var): {len(res['skipped_existing'])}  "
                    f"DLP engelli: {len(res['blocked'])}  Eşlenemeyen: {res['unmapped']}"
                    + (f"\nKayıtsız projeler (mem project add ile kaydet): {', '.join(res['unregistered_projects'])}"
                       if res["unregistered_projects"] else ""))
    return EXIT_OK


def cmd_mcp(args) -> int:
    from .mcp_server import main_for_profile
    return main_for_profile(args.profile, vault_root=args.vault, vault_scope=args.vault_scope,
                            advisor_mode=args.advisor, default_harness=args.harness)


def cmd_profiles(args) -> int:
    ps = [{"name": p.name, "vault_root": p.vault_root, "state_dir": p.state_dir, "dlp_mode": p.dlp_mode,
           "advisor": p.advisor} for p in config.list_profiles()]
    _out(args, ps, "\n".join(f"{p['name']}\t{p['dlp_mode']}\tadvisor={p['advisor']}\t{p['vault_root']}"
                             for p in ps) or "(profil yok)")
    return EXIT_OK


def cmd_advisor(args) -> int:
    """Opsiyonel Gemini Decisions danismani: durum / ac / kapat / API anahtari (gizli, argv'siz)."""
    from mem_advisor.gemini_decisions import advisor_status, save_advisor_settings

    action = args.advisor_cmd
    if action == "set-key":
        if args.clear:
            save_advisor_settings(clear_key=True)
            _out(args, {"status": "cleared"}, "API anahtarı advisor.json'dan silindi.")
            return EXIT_OK
        if args.stdin:
            key = sys.stdin.readline()
        else:
            var = args.from_env
            key = os.environ.get(var) or ""
            if not key.strip():
                raise ValueError(f"Ortam değişkeni boş veya tanımsız: {var}")
        save_advisor_settings(gemini_api_key=key)
        _out(args, {"status": "saved"}, "API anahtarı kaydedildi (değeri hiçbir yerde gösterilmez).")
        return EXIT_OK
    prof = config.get_profile(getattr(args, "profile", None))
    if action == "enable":
        prof = config.set_profile_advisor(prof.name, "gemini")
    elif action == "disable":
        prof = config.set_profile_advisor(prof.name, "off")
    st = {"profile": prof.name, **advisor_status(prof.name, prof.dlp_mode, prof.advisor)}
    _out(args, st, f"profil={st['profile']} advisor={st['mode']} izinli={st['allowed']} "
                   f"anahtar={'var (' + str(st['key_source']) + ')' if st['key_configured'] else 'yok'} "
                   f"model={st['model']}")
    return EXIT_OK


def cmd_doctor(args) -> int:
    checks: list[tuple[str, str, str]] = []

    def add(level: str, name: str, detail: str = "") -> None:
        checks.append((level, name, detail))

    add("PASS" if sys.version_info >= (3, 11) else "FAIL", "Python >= 3.11", sys.version.split()[0])
    add("PASS" if store.fts5_available() else "FAIL", "SQLite FTS5", f"sqlite {sqlite3.sqlite_version}")
    try:
        root, sd = _vault_ctx(args)
    except ValueError as exc:
        add("FAIL", "Profil/kasa çözümlemesi", str(exc))
        return _doctor_report(args, checks)
    add("PASS" if root.is_dir() else "FAIL", "Kasa dizini", str(root))
    try:
        meta = config.read_vault_meta(root)
        mode = meta.get("dlp_mode")
        add("PASS", ".mem-vault.json", f"profil={meta.get('profile')} dlp={mode}")
        add("FAIL" if mode == "off" else "PASS", "DLP kalkanı açık", f"mod={mode}")
        adv_mode = config.effective_advisor_mode(root)
        add("PASS", "Harici danışman (advisor)",
            f"mod={adv_mode}" + ("" if config.can_use_advisor(meta.get("profile") or "", mode)
                                  else " (work/block: hava boşluklu)"))
        sdir = config.resolve_state_dir(root, sd)
        add("PASS", "state_dir kasa dışında", str(sdir))
    except ValueError as exc:
        add("FAIL", "Kasa meta/state", str(exc))
        return _doctor_report(args, checks)
    try:
        sdir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=sdir, prefix=".doctor-", delete=True):
            pass
        add("PASS", "state_dir yazılabilir")
        with VaultLock(sdir / "writer.lock", timeout=2.0):
            pass
        add("PASS", "Yazar kilidi alınabiliyor")
    except (OSError, TimeoutError) as exc:
        add("FAIL", "state_dir/kilit", str(exc))
    try:
        res = vault.sync_index(root, sdir)
        conn = store.connect(sdir)
        try:
            n = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            p = conn.execute("SELECT COUNT(*) FROM passages").fetchone()[0]
            try:
                conn.execute("INSERT INTO passages_fts(passages_fts) VALUES('integrity-check')")
                add("PASS", "FTS5 bütünlük denetimi")
            except sqlite3.Error as exc:
                add("FAIL", "FTS5 bütünlük denetimi", str(exc))
        finally:
            conn.close()
        add("PASS", "İndeks senkronu", f"{n} belge, {p} pasaj, okunan={res['read_count']}")
    except (sqlite3.Error, OSError, ValueError) as exc:
        add("FAIL", "İndeks senkronu", str(exc))
    projects = config.load_projects(root)
    add("PASS" if projects else "WARN", "Kayıtlı projeler", f"{len(projects)} proje")
    for proj in projects:
        for r in proj.get("roots", []):
            if not Path(r).exists():
                add("WARN", f"Proje kökü bulunamadı ({proj['slug']})", r)
    def _read_doctor_rule(rel: str) -> str | None:
        p = root / rel
        if not p.exists() and not p.is_symlink():
            return None
        try:
            return parse_markdown(p.read_text(encoding="utf-8"))[1].strip()
        except (OSError, UnicodeDecodeError) as exc:
            add("FAIL", f"Kural dosyası okunamadı ({rel})", str(exc))
            return None

    core_body = _read_doctor_rule("Core.md") or ""
    grules_body = _read_doctor_rule("Kurallar.md") or ""
    v_mandatory = ["# Hafıza bağlamı — genel"]
    if core_body:
        v_mandatory.append("## Core\n\n" + core_body)
    if grules_body:
        v_mandatory.append("## Genel kurallar\n\n" + grules_body)
    vault_wide_len = len("\n\n".join(v_mandatory))
    max_len = vault_wide_len
    over: list[str] = []
    if vault_wide_len >= 6000:
        over.append(f"genel ({vault_wide_len} kr{', >8000 bütçe aşımı' if vault_wide_len > 8000 else ''})")
    for proj in projects:
        slug = proj["slug"]
        pname = proj.get("name", slug)
        filtered_global = vault._filter_global_rules(grules_body, root, slug)
        prules = _read_doctor_rule(f"projects/{slug}/Kurallar.md")
        mandatory = [f"# Hafıza bağlamı — {pname} ({slug})"]
        if core_body:
            mandatory.append("## Core\n\n" + core_body)
        if filtered_global:
            mandatory.append("## Genel kurallar\n\n" + filtered_global)
        if prules is not None:
            mandatory.append(f"## Proje kuralları ({slug})\n\n" + (prules or "(kural yok)"))
        mlen = len("\n\n".join(mandatory))
        if mlen > max_len:
            max_len = mlen
        if mlen >= 6000:
            over.append(f"{slug} ({mlen} kr{', >8000 bütçe aşımı' if mlen > 8000 else ''})")
    pct = round(max_len * 100 / 8000)
    if over:
        add("WARN", "Bootstrap kural bütçesi",
            f"en yüksek={max_len} kr (%{pct} / 8000); aşan kapsamlar: {', '.join(over)}")
    else:
        add("PASS", "Bootstrap kural bütçesi", f"en yüksek={max_len} kr (%{pct} / 8000)")
    return _doctor_report(args, checks)


def _doctor_report(args, checks) -> int:
    failed = any(c[0] == "FAIL" for c in checks)
    data = {"ok": not failed, "checks": [{"level": a, "name": b, "detail": c} for a, b, c in checks]}
    _out(args, data, "\n".join(f"[{a}] {b}" + (f" — {c}" if c else "") for a, b, c in checks)
         + f"\n\nSonuç: {'HATA VAR' if failed else 'TAMAM'}")
    return EXIT_ERROR if failed else EXIT_OK


# ---------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--profile", help="profil adı (varsayılan: MEM_PROFILE veya kayıtlı varsayılan)")
    common.add_argument("--vault", help="kasa yolunu doğrudan ver (profil yerine)")
    common.add_argument("--json", action="store_true", help="JSON çıktı")

    advisor_opt = argparse.ArgumentParser(add_help=False)
    advisor_opt.add_argument("--advisor", choices=config.ADVISOR_MODES,
                             help="harici danışman (varsayılan: profil ayarı / MEM_ADVISOR; work'te reddedilir)")

    scoped = argparse.ArgumentParser(add_help=False)
    scoped.add_argument("--project", help="proje slug'ı (cwd yerine)")
    scoped.add_argument("--cwd", help="çalışma alanı yolu (varsayılan: geçerli dizin)")

    p = argparse.ArgumentParser(prog="mem", description="İkinci Beyin (mem) motoru")
    p.add_argument("--version", action="version", version=f"mem {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", parents=[common], help="kasa ve profil oluştur")
    s.add_argument("--dlp", choices=("block", "redact"), help="DLP modu (work: block, diğer: redact)")
    s.add_argument("--advisor", choices=config.ADVISOR_MODES,
                   help="opsiyonel Gemini Decisions danışmanı (yalnız work olmayan/block olmayan profiller)")
    s.set_defaults(func=cmd_init)

    pr = sub.add_parser("project", help="proje beyaz listesi")
    psub = pr.add_subparsers(dest="project_cmd", required=True)
    s = psub.add_parser("add", parents=[common], help="projeyi kaydet")
    s.add_argument("slug")
    s.add_argument("--root", action="append", help="repo kökü (tekrarlanabilir; varsayılan: geçerli dizin)")
    s.add_argument("--name")
    s.add_argument("--description")
    s.add_argument("--tag", action="append", help="proje etiketi (kural filtreleme için, ör. python, android)")
    s.set_defaults(func=cmd_project_add)
    s = psub.add_parser("list", parents=[common], help="kayıtlı projeler")
    s.set_defaults(func=cmd_project_list)

    s = sub.add_parser("decide", parents=[common, scoped, advisor_opt], help="karar (ADR) kaydet")
    s.add_argument("--title", required=True)
    s.add_argument("--decision", required=True)
    s.add_argument("--context")
    s.add_argument("--alternatives")
    s.add_argument("--rationale")
    s.add_argument("--supersedes", action="append")
    s.add_argument("--source", action="append")
    s.add_argument("--tag", action="append")
    s.add_argument("--force", action="store_true")
    s.add_argument("--shared", action="store_true", help="karar projeye değil knowledge/kararlar/ altına yazılır")
    s.set_defaults(func=cmd_decide)

    s = sub.add_parser("rule", parents=[common, scoped], help="kural ekle, güncelle veya kaldır")
    s.add_argument("rule")
    s.add_argument("--reason")
    s.add_argument("--scope", help="'global' veya proje slug'ı")
    s.add_argument("--tag", action="append", help="kural etiketi (tekrarlanabilir; genel kuralları projeye göre filtreler)")
    s.add_argument("--supersedes", help="yerine geçilecek mevcut kural metni (arşive taşınır)")
    s.add_argument("--remove", action="store_true", help="verilen rule metniyle eşleşen mevcut kuralı kaldırıp arşive taşır")
    s.set_defaults(func=cmd_rule)

    s = sub.add_parser("search", parents=[common, scoped, advisor_opt], help="hafızada ara")
    s.add_argument("query", nargs="+")
    s.add_argument("--limit", type=int, default=8)
    s.add_argument("--include-superseded", action="store_true")
    s.add_argument("--include-archived", action="store_true")
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("read", parents=[common, scoped], help="not oku")
    s.add_argument("path")
    s.set_defaults(func=cmd_read)

    s = sub.add_parser("bootstrap", parents=[common, scoped, advisor_opt], help="oturum bağlamı")
    s.add_argument("--task", help="görev ipucu")
    s.add_argument("--budget", type=int, default=8000)
    s.set_defaults(func=cmd_bootstrap)

    s = sub.add_parser("finish", parents=[common, scoped], help="oturumu bitir (makbuz + devir kartı)")
    s.add_argument("--summary", required=True)
    s.add_argument("--thread", action="append", help="açık konu (tekrarlanabilir)")
    s.add_argument("--close-thread", action="append", help="kapatılacak açık konu kimliği veya tam metni")
    s.add_argument("--threads-mode", choices=("merge", "replace"), default="merge",
                   help="açık konular güncelleme modu (varsayılan: merge)")
    s.add_argument("--ref", action="append")
    s.add_argument("--session")
    s.add_argument("--harness", default="cli")
    s.set_defaults(func=cmd_finish)

    s = sub.add_parser("patch", parents=[common, scoped], help="sha256 kilitli tek düzenleme")
    s.add_argument("path")
    s.add_argument("--sha256", required=True)
    s.add_argument("--old", required=True)
    s.add_argument("--new", required=True)
    s.set_defaults(func=cmd_patch)

    s = sub.add_parser("sync", parents=[common], help="indeksi eşitle")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("compact", parents=[common], help="eski oturum makbuzlarını aylık özet notunda sıkıştır")
    s.add_argument("--days", type=int, default=14, help="bu günden eski makbuzları sıkıştır (varsayılan: 14)")
    s.add_argument("--project", help="yalnız belirtilen proje slug'ı için sıkıştır")
    s.add_argument("--dry-run", action="store_true", help="değişiklik yazmadan önizle")
    s.set_defaults(func=cmd_compact)

    s = sub.add_parser("doctor", parents=[common], help="kurulum sağlık denetimi")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("setup", parents=[common, advisor_opt],
                       help="tek komutta kurulum/güncelleme (kasa + proje + istemci + skill)")
    s.add_argument("--dlp", choices=("block", "redact"), help="DLP modu (work: block, diğer: redact)")
    s.add_argument("--harness", action="append", choices=harness.HARNESSES,
                   help="bağlanacak istemci (verilmezse yalnız zaten bu profile bağlı olanlar güncellenir)")
    s.add_argument("--all-harnesses", action="store_true",
                   help="algılanan ve başka profile bağlı olmayan tüm kurulu istemcileri bağla")
    s.add_argument("--no-skill", action="store_true", help="global SKILL.md kopyalamasını atla")
    s.add_argument("--project", action="append", metavar="[SLUG=]PATH",
                   help="beyaz listeye eklenecek proje kökü (tekrarlanabilir)")
    s.add_argument("--dry-run", action="store_true", help="değişiklik yazmadan önizle")
    s.add_argument("--config-root", help="gerçek ev dizini yerine bu kökü kullan (test/izolasyon)")
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser("connect", parents=[common, advisor_opt], help="Devin/Antigravity/Claude/Codex bağla")
    s.add_argument("harness", choices=harness.HARNESSES)
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--uninstall", action="store_true")
    s.add_argument("--with-skill", action="store_true", help="global skills/memory/SKILL.md dosyasını da kur/güncelle")
    s.add_argument("--config-root", help="gerçek ev dizini yerine bu kökü kullan (test/izolasyon)")
    s.set_defaults(func=cmd_connect)

    s = sub.add_parser("import-v3", parents=[common], help="eski Markdown kasasını içe aktar (salt-okunur)")
    s.add_argument("source")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_import_v3)

    s = sub.add_parser("profiles", parents=[common], help="kayıtlı profiller")
    s.set_defaults(func=cmd_profiles)

    s = sub.add_parser("advisor", help="opsiyonel Gemini Decisions danışmanı (work ve block olmayan profiller)")
    asub = s.add_subparsers(dest="advisor_cmd", required=True)
    for nm, hlp in (("status", "durum"), ("enable", "profilde aç"), ("disable", "profilde kapat")):
        a = asub.add_parser(nm, parents=[common], help=hlp)
        a.set_defaults(func=cmd_advisor)
    a = asub.add_parser("set-key", parents=[common], help="API anahtarını advisor.json'a kaydet (argv'de düz metin yok)")
    grp = a.add_mutually_exclusive_group(required=True)
    grp.add_argument("--stdin", action="store_true", help="anahtarı stdin'in ilk satırından oku")
    grp.add_argument("--from-env", nargs="?", const="GEMINI_API_KEY", metavar="VAR",
                     help="anahtarı bu ortam değişkeninden oku (varsayılan: GEMINI_API_KEY)")
    grp.add_argument("--clear", action="store_true", help="kayıtlı anahtarı sil")
    a.set_defaults(func=cmd_advisor)

    s = sub.add_parser("mcp", parents=[common, advisor_opt], help="MCP stdio sunucusu")
    s.add_argument("--harness", help="bağlı istemci kimliği (connect tarafından atanır)")
    s.add_argument("--vault-scope", action="store_true",
                   help="masaüstü sohbet istemcileri (ör. Claude Desktop) için varsayılan kapsamı kasa kökü yap")
    s.set_defaults(func=cmd_mcp)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not (argv and argv[0] == "mcp"):
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                try:
                    stream.reconfigure(encoding="utf-8")
                except (ValueError, OSError):
                    pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except DLPBlockedError as exc:
        print(f"Hata: {exc}", file=sys.stderr)
        return EXIT_DLP
    except ConcurrencyConflictError as exc:
        print(f"Hata: {exc} (güncel sha256: {exc.current_sha256})", file=sys.stderr)
        return EXIT_CONFLICT
    except (ValueError, PathTraversalError, FileNotFoundError, TimeoutError, OSError) as exc:
        print(f"Hata: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
