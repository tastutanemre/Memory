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
                                 state_dir=sd)
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
    res = vault.add_rule(root, rscope, args.rule, args.reason or "", state_dir=sd, actor="cli")
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
                               session_id=args.session, harness=args.harness, state_dir=sd)
    _out(args, res, f"Makbuz: {res['receipt']}\nOturum: {res['session_tag']}"
                    + (f"\nArşive taşındı: {', '.join(res['archived'])}" if res["archived"] else ""))
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
                                  advisor=args.advisor)
    lines = [f"{f['action']:10} {f['path']}" for f in res["files"]]
    _out(args, res, "\n".join(lines))
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
                            advisor_mode=args.advisor)


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

    s = sub.add_parser("rule", parents=[common, scoped], help="kural ekle")
    s.add_argument("rule")
    s.add_argument("--reason")
    s.add_argument("--scope", help="'global' veya proje slug'ı")
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
    s.add_argument("--budget", type=int, default=6000)
    s.set_defaults(func=cmd_bootstrap)

    s = sub.add_parser("finish", parents=[common, scoped], help="oturumu bitir (makbuz + devir kartı)")
    s.add_argument("--summary", required=True)
    s.add_argument("--thread", action="append", help="açık konu (tekrarlanabilir)")
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

    s = sub.add_parser("doctor", parents=[common], help="kurulum sağlık denetimi")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("connect", parents=[common, advisor_opt], help="Devin/Antigravity/Claude/Codex bağla")
    s.add_argument("harness", choices=harness.HARNESSES)
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--uninstall", action="store_true")
    s.add_argument("--config-root", help="gerçek ev dizini yerine bu kökü kullan (test/izolasyon)")
    s.set_defaults(func=cmd_connect)

    s = sub.add_parser("import-v3", parents=[common], help="eski beyin_v3 kasasını içe aktar (salt-okunur)")
    s.add_argument("source")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_import_v3)

    s = sub.add_parser("profiles", parents=[common], help="kayıtlı profiller")
    s.set_defaults(func=cmd_profiles)

    s = sub.add_parser("advisor", help="opsiyonel Gemini Decisions danışmanı (yalnız personal)")
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
