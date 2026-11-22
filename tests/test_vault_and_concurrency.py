import json
import os
import subprocess
import sys
import threading
import unittest
from datetime import datetime, timedelta
from unittest import mock

from _helpers import MEM_PY, IsolatedCase

from mem import config, store, vault
from mem.frontmatter import parse_markdown
from mem.lock import (ConcurrencyConflictError, PathTraversalError, VaultLock, atomic_write_text,
                      resolve_in_vault, sha256_bytes)


class VaultBase(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.root, self.state = self.make_vault("work")
        self.repo = self.make_repo("orders")
        vault.register_project(self.root, "order-service", [self.repo], name="OrderService",
                               description="Sipariş servisi")


class ProjectAndScopeTests(VaultBase):
    def test_register_project_creates_structure(self):
        base = self.root / "projects" / "order-service"
        for n in ("OrderService.md", "Kurallar.md", "Acik-Konular.md", "Son-Oturum.md"):
            self.assertTrue((base / n).is_file(), n)
        self.assertTrue((base / "kararlar").is_dir())
        projects = json.loads((self.root / "projects.json").read_text(encoding="utf-8"))["projects"]
        self.assertEqual(projects[0]["slug"], "order-service")
        rules = (self.root / "Kurallar.md").read_text(encoding="utf-8")
        self.assertIn("- [kapsam: order-service] [[projects/order-service/Kurallar|OrderService kuralları]]", rules)
        before = (base / "OrderService.md").read_text(encoding="utf-8")
        vault.register_project(self.root, "order-service", [self.repo])  # idempotent
        self.assertEqual(before, (base / "OrderService.md").read_text(encoding="utf-8"))
        self.assertEqual(rules, (self.root / "Kurallar.md").read_text(encoding="utf-8"))
        with self.assertRaises(vault.VaultError):
            vault.register_project(self.root, "Bad Slug", [self.repo])

    def test_resolve_project_normalization(self):
        sub = self.repo / "src" / "deep"
        sub.mkdir(parents=True)
        for cwd in (sub, str(sub).replace("\\", "/"), str(sub).upper(), self.repo):
            p = config.resolve_project(self.root, cwd)
            self.assertIsNotNone(p, cwd)
            self.assertEqual(p["slug"], "order-service")
        self.assertIsNone(config.resolve_project(self.root, self.make_repo("foreign")))
        self.assertIsNone(config.resolve_project(self.root, str(self.repo) + "-evil"))
        self.assertEqual(config.resolve_project(self.root, self.root / "projects")["slug"], "_vault")

    def test_unregistered_workspace_zero_leak(self):
        foreign = self.make_repo("foreign")
        res = vault.bootstrap_context(self.root, self.state, cwd=foreign)
        self.assertEqual(res["status"], "unregistered_workspace")
        self.assertEqual(res["message"], "Bu çalışma alanı hafıza beyaz listesinde değil.")
        self.assertNotIn("text", res)
        self.assertNotIn("core", res)
        self.assertEqual(vault.finish_session(self.root, str(foreign), "x")["status"], "unregistered_workspace")
        self.assertEqual(list((self.root / "receipts").rglob("*.md")), [])

    def test_bootstrap_content_and_rules_never_cut(self):
        big_rule = "- **[2026-01-01]** " + ("kural " * 400)
        p = self.root / "projects" / "order-service" / "Kurallar.md"
        p.write_text(p.read_text(encoding="utf-8") + big_rule + "\n", encoding="utf-8")
        vault.add_rule(self.root, "global", "Her zaman test yaz", "kalite", state_dir=self.state)
        res = vault.bootstrap_context(self.root, self.state, cwd=self.repo / ".", budget_chars=500)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["project"], "order-service")
        self.assertIn(big_rule.strip(), res["text"])  # proje kurallari ortadan kirpilmaz
        self.assertIn("Her zaman test yaz", res["text"])
        self.assertIn("Core", res["text"])
        other = self.make_repo("other")
        vault.register_project(self.root, "other-svc", [other], name="Other")
        res2 = vault.bootstrap_context(self.root, self.state, cwd=other)
        self.assertNotIn("kapsam: order-service", res2["text"])
        self.assertIn("kapsam: other-svc", res2["text"])
        self.assertNotIn("kural kural", res2["text"])

    def test_bootstrap_priority_order_and_handoff_floor(self):
        big_rule = "- **[2026-01-01]** " + ("kural " * 500)
        p = self.root / "projects" / "order-service" / "Kurallar.md"
        p.write_text(p.read_text(encoding="utf-8") + big_rule + "\n", encoding="utf-8")
        # >1500 chars in Acik-Konular and Son-Oturum (with a newline around char 950 followed by >1300 chars), plus a relevant note
        threads = [f"Açık görev {i:02d}: " + ("detay " * 18) for i in range(15)]
        summary_with_mid_nl = ("a" * 915) + "\n" + ("b" * 500)
        vault.finish_session(self.root, "order-service", summary_with_mid_nl,
                             open_threads=threads, session_id="s1", harness="claude", state_dir=self.state)
        self.write_md(self.root, "projects/order-service/extra.md", "# Redis\n\nRedis cache detayları.\n")

        res = vault.bootstrap_context(self.root, self.state, cwd=self.repo, task_hint="Redis", budget_chars=2000)
        txt = res["text"]
        self.assertIn("> Not: Zorunlu kurallar (", txt)
        self.assertIn("2000 karakterlik bütçeyi aştı; açık konular ve son oturum taban payla eklendi.", txt)
        idx_threads = txt.index("## Açık konular (order-service)")
        idx_last = txt.index("## Son oturum (order-service)")
        idx_rel = txt.index("## İlgili notlar (görev ipucu)")
        self.assertLess(idx_threads, idx_last)
        self.assertLess(idx_last, idx_rel)
        threads_section = txt[idx_threads:idx_last]
        last_section = txt[idx_last:idx_rel]
        rel_section = txt[idx_rel:]
        self.assertNotIn("bütçe nedeniyle atlandı", threads_section)
        self.assertNotIn("bütçe nedeniyle atlandı", last_section)

        suffix = "\n…(kısaltıldı; tamamı için memory_read kullan)"
        threads_src = threads_section.split("\n\n", 1)[1].rstrip()
        if threads_src.endswith(suffix):
            threads_src = threads_src[: -len(suffix)]
        last_src = last_section.split("\n\n", 1)[1].rstrip()
        if last_src.endswith(suffix):
            last_src = last_src[: -len(suffix)]
        self.assertGreaterEqual(len(threads_src), 1200)
        self.assertGreaterEqual(len(last_src), 1200)
        self.assertIn("(bütçe nedeniyle atlandı; memory_search ile ara)", rel_section)

    def test_bootstrap_long_project_slug_tag_normalized(self):
        long_slug = "very-long-service-name-with-suffix"
        long_repo = self.make_repo("longsvc")
        vault.register_project(self.root, long_slug, [long_repo], name="LongSvc", state_dir=self.state)
        vault.add_rule(self.root, "global", "Uzun slug kuralı yüklenmeli", tags=[long_slug], state_dir=self.state)
        # Eski 32 karakter sinirinda kaydedilmis tekil (belirsiz olmayan) etiket de geriye donuk eslesmeli
        kpath = self.root / "Kurallar.md"
        kpath.write_text(
            kpath.read_text(encoding="utf-8").rstrip("\n")
            + "\n- **[2026-11-22]** {tags: very-long-service-name-with-suff} Eski 32kr kuralı yüklenmeli\n",
            encoding="utf-8",
        )
        res = vault.bootstrap_context(self.root, self.state, cwd=long_repo)
        self.assertIn("Uzun slug kuralı yüklenmeli", res["text"])
        self.assertIn("Eski 32kr kuralı yüklenmeli", res["text"])

        # 32 karakterlik ayni oneki paylasan iki farkli 36-kr proje birbirine sizdirmamali
        s_api = "very-long-shared-prefix-service-api1"
        s_web = "very-long-shared-prefix-service-web2"
        r_api = self.make_repo("shared32-api")
        r_web = self.make_repo("shared32-web")
        vault.register_project(self.root, s_api, [r_api], state_dir=self.state)
        vault.register_project(self.root, s_web, [r_web], state_dir=self.state)
        vault.add_rule(self.root, "global", "Yalnız 36kr API1 kuralı", tags=[s_api], state_dir=self.state)
        kpath.write_text(
            kpath.read_text(encoding="utf-8").rstrip("\n")
            + "\n- **[2026-11-22]** {tags: very-long-shared-prefix-service} Belirsiz 31kr kural sızmamalı\n",
            encoding="utf-8",
        )
        self.assertIn("Yalnız 36kr API1 kuralı", vault.bootstrap_context(self.root, self.state, cwd=r_api)["text"])
        web_txt = vault.bootstrap_context(self.root, self.state, cwd=r_web)["text"]
        self.assertNotIn("Yalnız 36kr API1 kuralı", web_txt)
        self.assertNotIn("Belirsiz 31kr kural sızmamalı", web_txt)

    def test_project_tags_and_global_rule_tag_filtering(self):
        vault.register_project(self.root, "order-service", [self.repo], tags=["Python", "FastAPI"],
                               state_dir=self.state)
        proj = config.get_project(self.root, "order-service")
        self.assertEqual(proj["tags"], ["python", "fastapi"])
        # idempotent re-register without tags preserves existing tags
        vault.register_project(self.root, "order-service", [self.repo], state_dir=self.state)
        self.assertEqual(config.get_project(self.root, "order-service")["tags"], ["python", "fastapi"])

        vault.add_rule(self.root, "global", "Her zaman untagged kural uygula", state_dir=self.state)
        vault.add_rule(self.root, "global", "Python tip ipucu kullan", tags=["python"], state_dir=self.state)
        vault.add_rule(self.root, "global", "Order özel kural", tags=["order-service"], state_dir=self.state)
        vault.add_rule(self.root, "global", "Android gradle kuralı 1", tags=["android", "gradle"],
                       state_dir=self.state)
        vault.add_rule(self.root, "global", "Rust cargo kuralı 2", tags=["rust"], state_dir=self.state)

        res = vault.bootstrap_context(self.root, self.state, cwd=self.repo)
        txt = res["text"]
        self.assertIn("Her zaman untagged kural uygula", txt)
        self.assertIn("Python tip ipucu kullan", txt)
        self.assertIn("Order özel kural", txt)
        self.assertNotIn("Android gradle kuralı 1", txt)
        self.assertNotIn("Rust cargo kuralı 2", txt)
        self.assertIn("2 etiketli genel kural bu projeyle eşleşmediği için gizlendi", txt)
        self.assertIn('memory_read("Kurallar.md")', txt)

    def test_bootstrap_excludes_receipts_from_relevant_notes(self):
        vault.finish_session(self.root, "order-service", "kuantum önbellek oturum özeti",
                             session_id="r1", harness="claude", state_dir=self.state)
        self.write_md(self.root, "projects/order-service/kararlar/2026-01-01-kuantum.md",
                      "---\ntitle: Kuantum Kararı\nkind: decision\nproject: order-service\nstatus: active\n---\n"
                      "# Kuantum Kararı\n\nkuantum önbellek kararı.\n")
        res = vault.bootstrap_context(self.root, self.state, cwd=self.repo, task_hint="kuantum önbellek")
        rel_ids = [r["doc_id"] for r in res["relevant"]]
        self.assertIn("projects/order-service/kararlar/2026-01-01-kuantum.md", rel_ids)
        self.assertFalse([d for d in rel_ids if d.startswith("receipts/")])

    def test_cross_project_explicit_conflict(self):
        other = self.make_repo("other")
        vault.register_project(self.root, "other-svc", [other])
        with self.assertRaises(vault.VaultError):
            vault.resolve_scope(self.root, cwd=other, project="order-service")
        with self.assertRaises(vault.VaultError):
            vault.resolve_scope(self.root, project="nope")

    def test_state_dir_must_be_outside_vault(self):
        with self.assertRaises(ValueError):
            config.create_profile("x", self.tmp / "v2", state_dir=self.tmp / "v2" / "state")
        with self.assertRaises(ValueError):
            config.ensure_state_outside(self.root, self.root / ".state")

    def test_path_traversal_blocked(self):
        for bad in ("../outside.md", "projects/../../x.md", "/etc/passwd", "C:/Windows/x.md", ""):
            with self.assertRaises(PathTraversalError, msg=bad):
                resolve_in_vault(self.root, bad)
        with self.assertRaises(PathTraversalError):
            vault.read_note(self.root, "../secret.md")
        with self.assertRaises(PathTraversalError):
            vault.read_note(self.root, ".mem-vault.json")


class DecisionTests(VaultBase):
    def decide(self, title, decision, **kw):
        return vault.record_decision(self.root, "order-service", title, decision, state_dir=self.state, **kw)

    def test_duplicate_warning_and_force(self):
        first = self.decide("Redis cache kullan", "Sipariş okumaları için Redis cache", context="Yük", rationale="Hız")
        self.assertEqual(first["status"], "created")
        self.assertTrue((self.root / first["path"]).is_file())
        dup = self.decide("Redis cache kullan", "Başka metin")
        self.assertEqual(dup["status"], "duplicate_warning")
        self.assertEqual(dup["existing"], first["path"])
        forced = self.decide("Redis cache kullan", "Başka metin", force=True)
        self.assertEqual(forced["status"], "created")
        self.assertNotEqual(forced["path"], first["path"])

    def test_supersedes_hides_from_search_but_stays_on_disk(self):
        old = self.decide("Kuyruk olarak RabbitMQ", "Mesajlaşma için RabbitMQ seç", rationale="olgun")
        new = self.decide("Kuyruk olarak Kafka", "Mesajlaşma için Kafka seç", supersedes=[old["path"]])
        self.assertEqual(new["superseded"], [old["path"]])
        meta, body = parse_markdown((self.root / old["path"]).read_text(encoding="utf-8"))
        self.assertEqual(meta["status"], "superseded")
        self.assertIn("Yerine geçen: [[" + new["path"][:-3], body)
        ids = [r["doc_id"] for r in vault.search_memory(self.root, "mesajlaşma RabbitMQ Kafka",
                                                        project="order-service", state_dir=self.state)]
        self.assertIn(new["path"], ids)
        self.assertNotIn(old["path"], ids)
        ids_all = [r["doc_id"] for r in vault.search_memory(self.root, "RabbitMQ", project="order-service",
                                                            include_superseded=True, state_dir=self.state)]
        self.assertIn(old["path"], ids_all)
        # basename ile referans da calisir
        third = self.decide("Kuyruk olarak NATS", "NATS seç", supersedes=[new["path"].split("/")[-1][:-3]])
        self.assertEqual(third["superseded"], [new["path"]])

    def test_hub_wikilinks_updated(self):
        res = self.decide("Veritabanı PostgreSQL", "PostgreSQL kullan")
        hub = (self.root / "projects/order-service/OrderService.md").read_text(encoding="utf-8")
        self.assertIn(f"[[{res['path'][:-3]}|Veritabanı PostgreSQL]]", hub)
        self.assertIn("<!-- mem-links -->", hub)
        self.assertIn("<!-- /mem-links -->", hub)
        conn = store.connect(self.state)
        try:
            store.sync_vault(conn, self.root)
            rows = conn.execute("SELECT 1 FROM links WHERE src_doc_id=? AND dst_doc_id=?",
                                ("projects/order-service/OrderService.md", res["path"])).fetchall()
            self.assertEqual(len(rows), 1)
            back = conn.execute("SELECT 1 FROM links WHERE src_doc_id=? AND dst_doc_id=?",
                                (res["path"], "projects/order-service/OrderService.md")).fetchall()
            self.assertEqual(len(back), 1)
        finally:
            conn.close()

    def test_global_decision_goes_to_knowledge(self):
        res = vault.record_decision(self.root, None, "Genel ilke", "Kod incelemesi şart", state_dir=self.state)
        self.assertTrue(res["path"].startswith("knowledge/kararlar/"))


class RuleTests(VaultBase):
    def test_rule_dedup_and_scopes(self):
        r1 = vault.add_rule(self.root, "order-service", "Her PR test içermeli", "kalite", state_dir=self.state)
        self.assertEqual(r1["status"], "added")
        self.assertRegex(r1["line"], r"^- \*\*\[\d{4}-\d{2}-\d{2}\]\*\* Her PR test içermeli \*\(Gerekçe: kalite\)\*$")
        r2 = vault.add_rule(self.root, "order-service", "her pr TEST içermeli", state_dir=self.state)
        self.assertEqual(r2["status"], "duplicate")
        text = (self.root / "projects/order-service/Kurallar.md").read_text(encoding="utf-8")
        self.assertEqual(text.count("Her PR test"), 1)
        g = vault.add_rule(self.root, "global", "Sırları yazma", state_dir=self.state)
        self.assertEqual(g["path"], "Kurallar.md")
        with self.assertRaises(vault.VaultError):
            vault.add_rule(self.root, "yok", "x", state_dir=self.state)

    def test_rule_tags_supersede_and_remove(self):
        r1 = vault.add_rule(self.root, "global", "API yanıtları JSON olmalı", reason="standart",
                            tags=["Python", "REST API"], state_dir=self.state)
        self.assertIn("{tags: python, rest-api}", r1["line"])
        # ayni metin tekrar eklenirse (etiketsiz bile olsa) duplicate sayilir
        dup = vault.add_rule(self.root, "global", "api yanıtları json olmalı", state_dir=self.state)
        self.assertEqual(dup["status"], "duplicate")

        # supersedes: eski kurali arsivler ve yenisini ekler
        r2 = vault.add_rule(self.root, "global", "API yanıtları JSON Schema ile doğrulanmalı",
                            reason="tip güvenliği", tags=["python"],
                            supersedes="API yanıtları JSON olmalı", state_dir=self.state)
        self.assertEqual(r2["status"], "added")
        self.assertIn("API yanıtları JSON olmalı", r2["superseded_line"])
        self.assertTrue(r2["archived"])
        kurallar = (self.root / "Kurallar.md").read_text(encoding="utf-8")
        self.assertNotIn("API yanıtları JSON olmalı", kurallar)
        self.assertIn("API yanıtları JSON Schema ile doğrulanmalı", kurallar)
        arch = "\n".join(p.read_text(encoding="utf-8") for p in (self.root / "archive").glob("*.md"))
        self.assertIn("~~", arch)
        self.assertIn("yerine: API yanıtları JSON Schema ile doğrulanmalı", arch)

        # remove via string and via bool
        vault.add_rule(self.root, "global", "Geçici kural bir iki üç", state_dir=self.state)
        rem1 = vault.add_rule(self.root, "global", remove="Geçici kural bir iki üç", reason="eskidi",
                              state_dir=self.state)
        self.assertEqual(rem1["status"], "removed")
        self.assertTrue(rem1["archived"])
        self.assertNotIn("Geçici kural bir iki üç", (self.root / "Kurallar.md").read_text(encoding="utf-8"))

        vault.add_rule(self.root, "global", "Başka kaldırılacak kural", state_dir=self.state)
        rem2 = vault.add_rule(self.root, "global", "Başka kaldırılacak kural", remove=True, state_dir=self.state)
        self.assertEqual(rem2["status"], "removed")
        self.assertNotIn("Başka kaldırılacak kural", (self.root / "Kurallar.md").read_text(encoding="utf-8"))

        # belirsiz/yok eslesme -> VaultError
        vault.add_rule(self.root, "global", "Ortak önekli kural A", state_dir=self.state)
        vault.add_rule(self.root, "global", "Ortak önekli kural B", state_dir=self.state)
        with self.assertRaises(vault.VaultError):
            vault.add_rule(self.root, "global", "Yeni", supersedes="Ortak önekli", state_dir=self.state)
        with self.assertRaises(vault.VaultError):
            vault.add_rule(self.root, "global", remove="hiç olmayan kural", state_dir=self.state)

    def test_rule_archive_failure_preserves_active_and_retry_is_idempotent(self):
        vault.add_rule(self.root, "global", "Kalıcı kural silinmemeli", reason="güvenlik", state_dir=self.state)
        orig_write = vault._Session.write

        def fail_archive_write(sess_self, doc_id, content, action, detail=None):
            if doc_id.startswith("archive/"):
                raise OSError("disk dolu")
            return orig_write(sess_self, doc_id, content, action, detail)

        # remove sirasinda arsiv yazimi basarisiz olursa aktif kural korunur
        with mock.patch.object(vault._Session, "write", fail_archive_write):
            with self.assertRaises(OSError):
                vault.add_rule(self.root, "global", remove="Kalıcı kural silinmemeli",
                               reason="deneme", state_dir=self.state)
        self.assertIn("Kalıcı kural silinmemeli", (self.root / "Kurallar.md").read_text(encoding="utf-8"))

        # supersedes sirasinda arsiv yazimi basarisiz olursa aktif kural korunur
        with mock.patch.object(vault._Session, "write", fail_archive_write):
            with self.assertRaises(OSError):
                vault.add_rule(self.root, "global", "Yeni kural", supersedes="Kalıcı kural silinmemeli",
                               state_dir=self.state)
        kurallar = (self.root / "Kurallar.md").read_text(encoding="utf-8")
        self.assertIn("Kalıcı kural silinmemeli", kurallar)
        self.assertNotIn("Yeni kural", kurallar)

        # arsiv yazildiktan sonra Kurallar.md yazimi kesintiye ugrarsa yeniden deneme duplike arsiv yazmaz
        def fail_kurallar_once(state_dict):
            def _w(sess_self, doc_id, content, action, detail=None):
                if doc_id == "Kurallar.md" and not state_dict["failed"]:
                    state_dict["failed"] = True
                    raise OSError("kesinti")
                return orig_write(sess_self, doc_id, content, action, detail)
            return _w

        with mock.patch.object(vault._Session, "write", fail_kurallar_once({"failed": False})):
            with self.assertRaises(OSError):
                vault.add_rule(self.root, "global", remove="Kalıcı kural silinmemeli",
                               reason="temizle", state_dir=self.state)
        self.assertIn("Kalıcı kural silinmemeli", (self.root / "Kurallar.md").read_text(encoding="utf-8"))

        rem_ok = vault.add_rule(self.root, "global", remove="Kalıcı kural silinmemeli",
                                reason="temizle", state_dir=self.state)
        self.assertEqual(rem_ok["status"], "removed")
        self.assertNotIn("Kalıcı kural silinmemeli", (self.root / "Kurallar.md").read_text(encoding="utf-8"))

        # tamamen tamamlanmis remove cagrisi tekrar edilirse idempotent status='removed' doner, arsiv cogalmaz
        rem_retry = vault.add_rule(self.root, "global", remove="Kalıcı kural silinmemeli",
                                   reason="temizle", state_dir=self.state)
        self.assertEqual(rem_retry["status"], "removed")
        arch_text = "\n".join(p.read_text(encoding="utf-8") for p in (self.root / "archive").glob("*.md"))
        self.assertEqual(arch_text.count("Kalıcı kural silinmemeli"), 1)


class PatchAndConcurrencyTests(VaultBase):
    def test_patch_sha_conflict_and_success(self):
        note = "projects/order-service/OrderService.md"
        info = vault.read_note(self.root, note)
        with self.assertRaises(ConcurrencyConflictError) as cm:
            vault.patch_note(self.root, note, "0" * 64, [{"old": "Sipariş servisi", "new": "X"}], state_dir=self.state)
        self.assertEqual(cm.exception.current_sha256, info["sha256"])
        res = vault.patch_note(self.root, note, info["sha256"],
                               [{"old": "Sipariş servisi", "new": "Sipariş ve ödeme servisi"}], state_dir=self.state)
        self.assertEqual(res["previous_sha256"], info["sha256"])
        self.assertNotEqual(res["sha256"], info["sha256"])
        self.assertIn("ödeme servisi", (self.root / note).read_text(encoding="utf-8"))
        # eski sha ile ikinci deneme artik catisir
        with self.assertRaises(ConcurrencyConflictError):
            vault.patch_note(self.root, note, info["sha256"], [{"old": "ödeme", "new": "x"}], state_dir=self.state)
        # yedek alindi
        backups = list((self.state / "backups").rglob("*.bak"))
        self.assertTrue(backups)

    def test_patch_old_must_match_exactly_once(self):
        self.write_md(self.root, "knowledge/n.md", "# N\n\nabc abc\n\nxyz\n")
        sha = sha256_bytes((self.root / "knowledge/n.md").read_bytes())
        with self.assertRaises(vault.VaultError):
            vault.patch_note(self.root, "knowledge/n.md", sha, [{"old": "abc", "new": "d"}], state_dir=self.state)
        with self.assertRaises(vault.VaultError):
            vault.patch_note(self.root, "knowledge/n.md", sha, [{"old": "yok", "new": "d"}], state_dir=self.state)
        self.assertEqual((self.root / "knowledge/n.md").read_text(encoding="utf-8"), "# N\n\nabc abc\n\nxyz\n")
        res = vault.patch_note(self.root, "knowledge/n.md", sha,
                               [{"old": "xyz", "new": "uvw"}, {"old": "uvw", "new": "rst"}], state_dir=self.state)
        self.assertEqual(res["status"], "patched")
        self.assertIn("rst", (self.root / "knowledge/n.md").read_text(encoding="utf-8"))

    def test_patch_cross_project_denied(self):
        other = self.make_repo("other")
        vault.register_project(self.root, "other-svc", [other])
        note = "projects/other-svc/Kurallar.md"
        sha = vault.read_note(self.root, note)["sha256"]
        with self.assertRaises(vault.VaultError):
            vault.patch_note(self.root, note, sha, [{"old": "Kurallar", "new": "K"}], state_dir=self.state,
                             project="order-service")
        with self.assertRaises(vault.VaultError):
            vault.read_note(self.root, note, project="order-service")

    def test_concurrent_rule_writers_lose_nothing(self):
        errors = []

        def worker(i):
            try:
                vault.add_rule(self.root, "order-service", f"Eşzamanlı kural numara {i:02d}", state_dir=self.state)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(errors, [])
        text = (self.root / "projects/order-service/Kurallar.md").read_text(encoding="utf-8")
        for i in range(10):
            self.assertEqual(text.count(f"kural numara {i:02d}"), 1)
        self.assertEqual(list((self.root / "projects/order-service").glob(".mem-tmp-*")), [])

    def test_lock_timeout_and_atomic_write(self):
        lock_path = self.state / "x.lock"
        with VaultLock(lock_path, timeout=1.0):
            with self.assertRaises(TimeoutError):
                VaultLock(lock_path, timeout=0.2, poll=0.02).acquire()
        with VaultLock(lock_path, timeout=0.5):
            pass
        target = self.tmp / "a" / "b.md"
        sha = atomic_write_text(target, "satır1\r\nsatır2\n", backup_dir=self.tmp / "bk")
        self.assertEqual(target.read_bytes(), "satır1\nsatır2\n".encode("utf-8"))
        self.assertEqual(sha, sha256_bytes(target.read_bytes()))
        for i in range(25):
            atomic_write_text(target, f"v{i}", backup_dir=self.tmp / "bk")
        self.assertLessEqual(len(list((self.tmp / "bk").glob("b.md.*.bak"))), 20)


class FinishSessionTests(VaultBase):
    def test_finish_creates_receipt_card_and_threads_idempotently(self):
        kw = dict(open_threads=["TTL belirle", "Metrik ekle"], refs=["commit abc123"], session_id="oturum-1",
                  harness="devin", state_dir=self.state)
        r1 = vault.finish_session(self.root, "order-service", "Cache eklendi", **kw)
        self.assertEqual(r1["status"], "recorded")
        self.assertRegex(r1["receipt"], r"^receipts/\d{4}-\d{2}/[0-9a-f]{16}\.md$")
        r2 = vault.finish_session(self.root, str(self.repo), "Cache eklendi", **kw)
        self.assertEqual(r1["receipt"], r2["receipt"])
        self.assertEqual(len(list((self.root / "receipts").rglob("*.md"))), 1)
        son = (self.root / "projects/order-service/Son-Oturum.md").read_text(encoding="utf-8")
        self.assertEqual(son.count("· devin-oturum-1"), 1)
        # ayni session_id: kart yerinde guncellenir
        vault.finish_session(self.root, "order-service", "Cache güncellendi", **kw)
        son = (self.root / "projects/order-service/Son-Oturum.md").read_text(encoding="utf-8")
        self.assertEqual(son.count("· devin-oturum-1"), 1)
        self.assertIn("Cache güncellendi", son)
        self.assertNotIn("Cache eklendi", son)
        threads = (self.root / "projects/order-service/Acik-Konular.md").read_text(encoding="utf-8")
        self.assertIn("- [ ] TTL belirle", threads)
        vault.finish_session(self.root, "order-service", "Konular kapandı", open_threads=[], harness="devin",
                             state_dir=self.state, session_id="oturum-2")
        threads = (self.root / "projects/order-service/Acik-Konular.md").read_text(encoding="utf-8")
        self.assertNotIn("TTL belirle", threads)

    def test_session_tag_deduplicates_harness_prefix(self):
        r1 = vault.finish_session(self.root, "order-service", "İş 1", session_id="s1",
                                  harness="devin", state_dir=self.state)
        r2 = vault.finish_session(self.root, "order-service", "İş 2", session_id="devin-s1",
                                  harness="devin", state_dir=self.state)
        self.assertEqual(r1["session_tag"], "devin-s1")
        self.assertEqual(r2["session_tag"], "devin-s1")
        self.assertEqual(r1["receipt"], r2["receipt"])
        r3 = vault.finish_session(self.root, "order-service", "İş 3", session_id="devin-devin-s1",
                                  harness="devin", state_dir=self.state)
        self.assertEqual(r3["session_tag"], "devin-s1")
        self.assertEqual(r1["receipt"], r3["receipt"])
        r4 = vault.finish_session(self.root, "order-service", "İş 4", session_id="devin",
                                  harness="devin", state_dir=self.state)
        self.assertEqual(r4["session_tag"], "devin")

    def test_session_id_strips_repeated_prefix_before_24_char_truncation(self):
        long_suffix = "123456789012345678901234567890"
        r1 = vault.finish_session(
            self.root, "order-service", "Uzun önekli oturum",
            session_id=f"antigravity-antigravity-antigravity-{long_suffix}",
            harness="antigravity", state_dir=self.state,
        )
        r2 = vault.finish_session(
            self.root, "order-service", "Aynı oturum düz",
            session_id=long_suffix,
            harness="antigravity", state_dir=self.state,
        )
        self.assertEqual(r1["session_tag"], f"antigravity-{long_suffix[:24]}")
        self.assertEqual(r1["session_tag"], r2["session_tag"])
        self.assertEqual(r1["receipt"], r2["receipt"])

    def test_close_selector_separation_and_replace_none_and_atomic_prevalidation(self):
        import re as _re
        vault.finish_session(self.root, "order-service", "Başlangıç",
                             open_threads=["İlk iş"], session_id="s1", harness="claude", state_dir=self.state)
        tpath = self.root / "projects/order-service/Acik-Konular.md"
        tid_m = _re.search(r"İlk iş <!-- t:([a-z0-9_-]+) h:claude -->", tpath.read_text(encoding="utf-8"))
        self.assertIsNotNone(tid_m)
        tid = tid_m.group(1)

        # ciplak id (t: olmadan) ID eslestirmez
        r_bare = vault.finish_session(self.root, "order-service", "Çıplak id",
                                      close_threads=[tid], session_id="s2", harness="codex", state_dir=self.state)
        self.assertEqual(r_bare["closed_threads"], [])
        self.assertIn("İlk iş", tpath.read_text(encoding="utf-8"))

        # t:<metin> metin eslestirmez
        r_t_txt = vault.finish_session(self.root, "order-service", "T metin",
                                       close_threads=["t:İlk iş"], session_id="s3", harness="codex", state_dir=self.state)
        self.assertEqual(r_t_txt["closed_threads"], [])
        self.assertIn("İlk iş", tpath.read_text(encoding="utf-8"))

        # replace + open_threads=None yalniz close_threads ile isteneni kapatir, digerlerini korur
        vault.finish_session(self.root, "order-service", "İkinci iş",
                             open_threads=["İkinci iş"], session_id="s4", harness="devin", state_dir=self.state)
        r_rep_none = vault.finish_session(self.root, "order-service", "Sadece birini kapat",
                                          open_threads=None, close_threads=[f"t:{tid}"], threads_mode="replace",
                                          session_id="s5", harness="antigravity", state_dir=self.state)
        self.assertEqual(r_rep_none["closed_threads"], [tid])
        ttext = tpath.read_text(encoding="utf-8")
        self.assertNotIn("İlk iş", ttext)
        self.assertIn("İkinci iş", ttext)

        # belirsiz close_threads -> hicbir dosya yazilmaz (receipt, Son-Oturum, Acik-Konular, archive)
        tpath.write_text(
            "# OrderService — Açık Konular\n\n"
            "- [ ] Çift madde <!-- t:aaaaaa h:claude -->\n"
            "- [ ] Çift madde <!-- t:bbbbbb h:codex -->\n",
            encoding="utf-8",
        )
        receipts_before = sorted(p.relative_to(self.root).as_posix() for p in (self.root / "receipts").rglob("*.md"))
        son_before = (self.root / "projects/order-service/Son-Oturum.md").read_text(encoding="utf-8")
        thr_before = tpath.read_text(encoding="utf-8")
        arch_before = {p.name: p.read_text(encoding="utf-8") for p in (self.root / "archive").glob("*.md")}

        with self.assertRaises(vault.VaultError):
            vault.finish_session(self.root, "order-service", "Hatalı kapanış",
                                 close_threads=["Çift madde"], session_id="fail-1",
                                 harness="antigravity", state_dir=self.state)

        self.assertEqual(
            sorted(p.relative_to(self.root).as_posix() for p in (self.root / "receipts").rglob("*.md")),
            receipts_before,
        )
        self.assertEqual((self.root / "projects/order-service/Son-Oturum.md").read_text(encoding="utf-8"), son_before)
        self.assertEqual(tpath.read_text(encoding="utf-8"), thr_before)
        self.assertEqual(
            {p.name: p.read_text(encoding="utf-8") for p in (self.root / "archive").glob("*.md")},
            arch_before,
        )

    def test_acik_konular_preserves_freeform_markdown_across_all_modes(self):
        tpath = self.root / "projects/order-service/Acik-Konular.md"
        freeform_doc = (
            "---\n"
            "title: OrderService — Açık Konular\n"
            "kind: threads\n"
            "project: order-service\n"
            "imported_from: beyin_v3\n"
            "visibility: internal\n"
            "---\n"
            "# OrderService — Açık konular (2026-10-31 itibarıyla)\n\n"
            "Repo dokümanlarından derlendi; docs/PLAN.md kontrol et.\n\n"
            "## Karar bekleyenler\n\n"
            "- **K13 (ADR-0015):** Ödeme sağlayıcısı zaman aşımı politikası belirlenecek.\n"
            "- **K4 ödeme sağlayıcısı:** İkinci sağlayıcı yedekleme senaryosu netleşecek.\n"
            "- **K6:** İade kuyruğu yeniden deneme sayısı seçilecek.\n\n"
            "## Altyapı ve engeller\n\n"
            "- Redis TLS sertifikası test ortamına yüklenecek.\n"
            "- Kafka bölüm sayısı üretim yüküne göre gözden geçirilecek.\n"
            "- Log saklama süresi 90 güne sabitlenecek.\n"
            "- Dağıtık izleme örnekleme oranı %10 olarak doğrulanacak.\n"
            "- Veritabanı bağlantı havuzu üst sınırı ölçülecek.\n"
            "- Sağlık ucu zaman aşımı alarm eşiği ayarlanacak.\n"
            "- Yedekleme geri yükleme tatbikatı planlanacak.\n"
            "- Dış API oran sınırlayıcı başlıkları belgelenecek.\n"
            "- [x] Tamamlanmış eski madde\n"
            "- [ ] Eski açık madde\n"
        )
        tpath.write_text(freeform_doc, encoding="utf-8")
        preserved_lines = [
            "# OrderService — Açık konular (2026-10-31 itibarıyla)",
            "Repo dokümanlarından derlendi; docs/PLAN.md kontrol et.",
            "## Karar bekleyenler",
            "- **K13 (ADR-0015):** Ödeme sağlayıcısı zaman aşımı politikası belirlenecek.",
            "- **K4 ödeme sağlayıcısı:** İkinci sağlayıcı yedekleme senaryosu netleşecek.",
            "- **K6:** İade kuyruğu yeniden deneme sayısı seçilecek.",
            "## Altyapı ve engeller",
            "- Redis TLS sertifikası test ortamına yüklenecek.",
            "- Dış API oran sınırlayıcı başlıkları belgelenecek.",
            "- [x] Tamamlanmış eski madde",
        ]

        # 1. merge
        vault.finish_session(self.root, "order-service", "Merge adımı",
                             open_threads=["Yeni ajan maddesi"], session_id="m1",
                             harness="claude", state_dir=self.state)
        txt1 = tpath.read_text(encoding="utf-8")
        meta1, _ = parse_markdown(txt1)
        self.assertEqual(meta1.get("imported_from"), "beyin_v3")
        self.assertEqual(meta1.get("visibility"), "internal")
        for ln in preserved_lines:
            self.assertIn(ln, txt1)
        self.assertIn("Eski açık madde", txt1)
        self.assertIn("h:legacy", txt1)
        self.assertIn("Yeni ajan maddesi", txt1)
        self.assertIn("h:claude", txt1)
        self.assertEqual(txt1.count(vault.MANAGED_THREADS_HEADING), 1)
        self.assertLess(txt1.index(vault.MANAGED_THREADS_HEADING), txt1.index("Yeni ajan maddesi"))

        # 2. replace
        vault.finish_session(self.root, "order-service", "Replace adımı",
                             open_threads=["Devin maddesi"], threads_mode="replace",
                             session_id="m2", harness="devin", state_dir=self.state)
        txt2 = tpath.read_text(encoding="utf-8")
        for ln in preserved_lines:
            self.assertIn(ln, txt2)
        self.assertNotIn("Eski açık madde", txt2)
        self.assertNotIn("Yeni ajan maddesi", txt2)
        self.assertIn("Devin maddesi", txt2)
        self.assertEqual(txt2.count(vault.MANAGED_THREADS_HEADING), 1)
        arch_text = "\n".join(p.read_text(encoding="utf-8") for p in (self.root / "archive").glob("*.md"))
        self.assertIn("Eski açık madde", arch_text)
        self.assertIn("Yeni ajan maddesi", arch_text)

        # 3. close_threads
        vault.finish_session(self.root, "order-service", "Close adımı",
                             close_threads=["Devin maddesi"],
                             session_id="m3", harness="antigravity", state_dir=self.state)
        txt3 = tpath.read_text(encoding="utf-8")
        for ln in preserved_lines:
            self.assertIn(ln, txt3)
        self.assertNotIn("Devin maddesi", txt3)
        self.assertEqual(txt3.count(vault.MANAGED_THREADS_HEADING), 1)
        self.assertIn("(açık konu yok)", txt3.split(vault.MANAGED_THREADS_HEADING, 1)[1])

    def test_multi_agent_open_threads_ownership_and_close_threads(self):
        vault.finish_session(self.root, "order-service", "Claude işi",
                             open_threads=["API şemasını güncelle"],
                             session_id="c1", harness="claude", state_dir=self.state)
        vault.finish_session(self.root, "order-service", "Antigravity işi",
                             open_threads=["UI testini ekle"],
                             session_id="a1", harness="antigravity", state_dir=self.state)
        tpath = self.root / "projects/order-service/Acik-Konular.md"
        ttext = tpath.read_text(encoding="utf-8")
        self.assertIn("API şemasını güncelle", ttext)
        self.assertIn("h:claude", ttext)
        self.assertIn("UI testini ekle", ttext)
        self.assertIn("h:antigravity", ttext)

        # antigravity open_threads=[] yalniz kendi maddesini kapatir, claude'unki acik kalir
        r_close_own = vault.finish_session(self.root, "order-service", "UI bitti",
                                           open_threads=[], session_id="a2",
                                           harness="antigravity", state_dir=self.state)
        ttext = tpath.read_text(encoding="utf-8")
        self.assertIn("API şemasını güncelle", ttext)
        self.assertNotIn("UI testini ekle", ttext)
        self.assertTrue(r_close_own["archived"])
        self.assertEqual(len(r_close_own["closed_threads"]), 1)

        # substring ile kapatma CALISMAZ
        r_sub = vault.finish_session(self.root, "order-service", "Kısmi deneme",
                                     close_threads=["API şeması"], session_id="a3",
                                     harness="antigravity", state_dir=self.state)
        self.assertEqual(r_sub["closed_threads"], [])
        self.assertIn("API şemasını güncelle", tpath.read_text(encoding="utf-8"))

        # baska ajan (antigravity) claude'un maddesini tam metin ile kapatabilir
        r_cross = vault.finish_session(self.root, "order-service", "API de bitti",
                                       close_threads=["API şemasını güncelle"], session_id="a4",
                                       harness="antigravity", state_dir=self.state)
        self.assertEqual(len(r_cross["closed_threads"]), 1)
        ttext = tpath.read_text(encoding="utf-8")
        self.assertNotIn("API şemasını güncelle", ttext)
        self.assertIn("(açık konu yok)", ttext)
        arch_text = "\n".join(p.read_text(encoding="utf-8") for p in (self.root / "archive").glob("*.md"))
        self.assertIn("kapanan-konular", arch_text)
        self.assertIn("açan: claude, kapatan: antigravity", arch_text)

    def test_legacy_untagged_threads_and_ambiguous_close_and_replace_mode(self):
        tpath = self.root / "projects/order-service/Acik-Konular.md"
        tpath.write_text("# OrderService — Açık Konular\n\n- [ ] Eski miras madde\n", encoding="utf-8")
        vault.finish_session(self.root, "order-service", "Yeni iş",
                             open_threads=["Claude maddesi"], session_id="c1",
                             harness="claude", state_dir=self.state)
        ttext = tpath.read_text(encoding="utf-8")
        self.assertIn("Eski miras madde", ttext)
        self.assertIn("h:legacy", ttext)
        self.assertIn("Claude maddesi", ttext)
        self.assertIn("h:claude", ttext)

        # id (t:...) ile legacy maddeyi kapatma
        import re as _re
        m = _re.search(r"Eski miras madde <!-- t:([a-z0-9_-]+) h:legacy -->", ttext)
        self.assertIsNotNone(m)
        leg_id = m.group(1)
        r_leg = vault.finish_session(self.root, "order-service", "Legacy bitti",
                                     close_threads=[f"t:{leg_id}"], session_id="c2",
                                     harness="codex", state_dir=self.state)
        self.assertEqual(r_leg["closed_threads"], [leg_id])
        self.assertNotIn("Eski miras madde", tpath.read_text(encoding="utf-8"))

        # belirsiz cift metin -> VaultError
        tpath.write_text(
            "# OrderService — Açık Konular\n\n"
            "- [ ] Tekrarlı madde <!-- t:111111 h:claude -->\n"
            "- [ ] Tekrarlı madde <!-- t:222222 h:codex -->\n",
            encoding="utf-8",
        )
        with self.assertRaises(vault.VaultError):
            vault.finish_session(self.root, "order-service", "Çakışma",
                                 close_threads=["Tekrarlı madde"], session_id="x1",
                                 harness="antigravity", state_dir=self.state)

        # threads_mode="replace" tum listeyi degistirir
        r_rep = vault.finish_session(self.root, "order-service", "Sıfırla",
                                     open_threads=["Sadece bu"], threads_mode="replace",
                                     session_id="x2", harness="devin", state_dir=self.state)
        ttext = tpath.read_text(encoding="utf-8")
        self.assertIn("Sadece bu", ttext)
        self.assertNotIn("Tekrarlı madde", ttext)
        self.assertEqual(sorted(r_rep["closed_threads"]), ["111111", "222222"])

    def test_concurrent_multi_agent_open_threads_never_clobber(self):
        errors = []

        def worker(i: int):
            try:
                vault.finish_session(self.root, "order-service", f"Özet {i}",
                                     open_threads=[f"Ajan görevi {i}"],
                                     session_id=f"s-{i}", harness=f"agent-{i}",
                                     state_dir=self.state)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(60)
        self.assertEqual(errors, [])
        ttext = (self.root / "projects/order-service/Acik-Konular.md").read_text(encoding="utf-8")
        for i in range(8):
            self.assertIn(f"Ajan görevi {i}", ttext)
            self.assertIn(f"h:agent-{i}", ttext)

    def test_son_oturum_compaction_is_lossless(self):
        clock = {"t": datetime(2026, 1, 20, 10, 0)}

        def fake_now():
            return clock["t"]

        summaries = []
        with mock.patch.object(vault, "_now", fake_now):
            for i in range(14):
                s = f"Özet {i:02d}: " + ("ğüşıöç içerik " * 25)
                summaries.append(s.strip())
                vault.finish_session(self.root, "order-service", s, session_id=f"s{i}", harness="t",
                                     state_dir=self.state)
                clock["t"] += timedelta(days=4)
        son_path = self.root / "projects/order-service/Son-Oturum.md"
        son = son_path.read_text(encoding="utf-8")
        self.assertLessEqual(len(son), vault.SESSION_LIMIT)
        _, cards = vault._split_cards(parse_markdown(son)[1])
        self.assertLessEqual(len(cards), vault.SESSION_MAX_CARDS)
        archives = sorted((self.root / "archive").glob("*.md"))
        self.assertTrue(archives)
        archived_text = "\n".join(a.read_text(encoding="utf-8") for a in archives)
        self.assertRegex(archived_text, r"<!-- proje: order-service -->")
        for s in summaries:
            self.assertTrue(s in son or s in archived_text, f"kayip: {s[:12]}")
        self.assertTrue(all(a.name[:4] == "2026" for a in archives))
        self.assertGreaterEqual(len(archives), 2)  # farkli aylara dagilir
        # en yeni kart ustte
        self.assertLess(son.index("Özet 13"), son.index("Özet 12"))
        # indeks arsivi varsayilan aramadan eler
        res = vault.search_memory(self.root, "Özet 00", project="order-service", state_dir=self.state)
        self.assertTrue(all(not r["doc_id"].startswith("archive/") for r in res))
        res = vault.search_memory(self.root, "Özet 00", include_archived=True, state_dir=self.state)
        self.assertTrue(any(r["doc_id"].startswith("archive/") for r in res))

    def test_son_oturum_keeps_at_least_three_cards_of_1300_chars(self):
        for i in range(3):
            vault.finish_session(self.root, "order-service", f"Kart {i}: " + ("detay " * 215),
                                 session_id=f"c{i}", harness="claude", state_dir=self.state)
        son = (self.root / "projects/order-service/Son-Oturum.md").read_text(encoding="utf-8")
        _, cards = vault._split_cards(parse_markdown(son)[1])
        self.assertEqual(len(cards), 3)
        self.assertFalse(list((self.root / "archive").glob("*.md")))

    def test_vault_scope_finish_writes_receipt_only(self):
        res = vault.finish_session(self.root, str(self.root), "Kasa bakımı yapıldı", state_dir=self.state)
        self.assertEqual(res["status"], "recorded")
        self.assertIsNone(res["project"])

    def test_compact_receipts_and_cli(self):
        other = self.make_repo("other")
        vault.register_project(self.root, "other-svc", [other], name="Other")
        base_now = datetime(2026, 5, 25, 12, 0)
        old_dt = base_now - timedelta(days=20)
        recent_dt = base_now - timedelta(days=2)

        old_receipts = []
        with mock.patch.object(vault, "_now", return_value=old_dt):
            for i in range(3):
                r = vault.finish_session(self.root, "order-service", f"Eski oturum özeti {i} özelkelime",
                                         session_id=f"old-{i}", harness="claude", state_dir=self.state)
                old_receipts.append(r["receipt"])
            single_other = vault.finish_session(self.root, "other-svc", "Tekli eski oturum",
                                                session_id="other-1", harness="claude", state_dir=self.state)["receipt"]
        with mock.patch.object(vault, "_now", return_value=recent_dt):
            recent_r = vault.finish_session(self.root, "order-service", "Yeni oturum özeti",
                                            session_id="new-1", harness="claude", state_dir=self.state)["receipt"]

        with mock.patch.object(vault, "_now", return_value=base_now):
            dry = vault.compact_receipts(self.root, older_than_days=14, dry_run=True, state_dir=self.state)
            self.assertEqual(dry["compacted"], 3)
            self.assertEqual(dry["summaries"], ["receipts/2026-05/order-service-ozet.md"])
            self.assertFalse((self.root / "receipts/2026-05/order-service-ozet.md").exists())

            res = vault.compact_receipts(self.root, older_than_days=14, state_dir=self.state)
            self.assertEqual(res["compacted"], 3)
            sum_path = self.root / "receipts/2026-05/order-service-ozet.md"
            self.assertTrue(sum_path.is_file())
            sum_meta, sum_body = parse_markdown(sum_path.read_text(encoding="utf-8"))
            self.assertTrue(sum_meta.get("compacted_summary"))
            self.assertEqual(sum_meta.get("project"), "order-service")
            for rp in old_receipts:
                self.assertIn(f"[[{rp[:-3]}]]", sum_body)
                rmeta, _ = parse_markdown((self.root / rp).read_text(encoding="utf-8"))
                self.assertEqual(rmeta["status"], "archived")
                self.assertEqual(rmeta["compacted_into"], "receipts/2026-05/order-service-ozet.md")

            # tekli grup ve yeni makbuz aktif kalir
            self.assertEqual(parse_markdown((self.root / single_other).read_text(encoding="utf-8"))[0]["status"], "active")
            self.assertEqual(parse_markdown((self.root / recent_r).read_text(encoding="utf-8"))[0]["status"], "active")

            # idempotent
            again = vault.compact_receipts(self.root, older_than_days=14, state_dir=self.state)
            self.assertEqual(again["compacted"], 0)

        # arsivlenen tekil makbuzlar varsayilan aramadan gizlenir, ozet bulunur
        hits = [h["doc_id"] for h in vault.search_memory(self.root, "özelkelime", project="order-service",
                                                          state_dir=self.state)]
        self.assertIn("receipts/2026-05/order-service-ozet.md", hits)
        for rp in old_receipts:
            self.assertNotIn(rp, hits)

        # CLI mem compact
        p = subprocess.run([sys.executable, str(MEM_PY), "compact", "--vault", str(self.root), "--days", "14",
                            "--dry-run", "--json"], capture_output=True, env=os.environ.copy(), timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8"))
        self.assertEqual(json.loads(p.stdout.decode("utf-8"))["status"], "ok")

    def test_compact_security_namespace_resume_and_refresh(self):
        # 1. negative older_than_days rejected upfront
        with self.assertRaises(vault.VaultError):
            vault.compact_receipts(self.root, older_than_days=-1, state_dir=self.state)

        genel_repo = self.make_repo("genel")
        vault.register_project(self.root, "genel", [genel_repo], name="GenelProje", state_dir=self.state)

        base_now = datetime(2026, 6, 25, 12, 0)
        old_dt = base_now - timedelta(days=20)
        with mock.patch.object(vault, "_now", return_value=old_dt):
            # 2 projectless receipts
            r_v1 = vault.finish_session(self.root, str(self.root), "Kasa bakımı 1", session_id="v1",
                                        harness="claude", state_dir=self.state)["receipt"]
            r_v2 = vault.finish_session(self.root, str(self.root), "Kasa bakımı 2", session_id="v2",
                                        harness="claude", state_dir=self.state)["receipt"]
            # 2 receipts for project literally named "genel"
            r_g1 = vault.finish_session(self.root, "genel", "Genel proje işi 1", session_id="g1",
                                        harness="claude", state_dir=self.state)["receipt"]
            r_g2 = vault.finish_session(self.root, "genel", "Genel proje işi 2", session_id="g2",
                                        harness="claude", state_dir=self.state)["receipt"]
            # 2 untrusted receipts for order-service (must be skipped!)
            for i in range(2):
                self.write_md(
                    self.root,
                    f"receipts/2026-06/untrusted-{i}.md",
                    f"---\ntitle: Untrusted {i}\nkind: receipt\nproject: order-service\n"
                    f"status: active\ntrust: untrusted\ncreated_at: {old_dt.isoformat(timespec='seconds')}\n---\n"
                    f"# Makbuz\n\n**Proje:** [[projects/order-service/OrderService|order-service]]\n\n"
                    f"Güvenilmeyen özet {i}\n",
                )

        with mock.patch.object(vault, "_now", return_value=base_now):
            res = vault.compact_receipts(self.root, older_than_days=14, state_dir=self.state)
            self.assertEqual(res["compacted"], 4)
            self.assertEqual(sorted(res["summaries"]), [
                "receipts/2026-06/_vault-ozet.md",
                "receipts/2026-06/genel-ozet.md",
            ])

            # 2. untrusted receipts skipped and never promoted
            self.assertFalse((self.root / "receipts/2026-06/order-service-ozet.md").exists())
            for i in range(2):
                um, _ = parse_markdown((self.root / f"receipts/2026-06/untrusted-{i}.md").read_text(encoding="utf-8"))
                self.assertEqual(um["status"], "active")

            # 3. projectless (_vault-ozet) and literal "genel" (genel-ozet) stay strictly separated
            vm, vb = parse_markdown((self.root / "receipts/2026-06/_vault-ozet.md").read_text(encoding="utf-8"))
            gm, gb = parse_markdown((self.root / "receipts/2026-06/genel-ozet.md").read_text(encoding="utf-8"))
            self.assertIsNone(vm.get("project"))
            self.assertEqual(vm.get("trust"), "trusted")
            self.assertIn("Kasa bakımı 1", vb)
            self.assertNotIn("Genel proje işi", vb)
            self.assertEqual(gm.get("project"), "genel")
            self.assertEqual(gm.get("trust"), "trusted")
            self.assertIn("Genel proje işi 1", gb)
            self.assertNotIn("Kasa bakımı", gb)

        # 4. When summary already exists, a single new old receipt (len==1) is compacted into it
        with mock.patch.object(vault, "_now", return_value=old_dt + timedelta(days=1)):
            r_g3 = vault.finish_session(self.root, "genel", "Tekli ek eski iş", session_id="g3",
                                        harness="claude", state_dir=self.state)["receipt"]
        with mock.patch.object(vault, "_now", return_value=base_now):
            res_single = vault.compact_receipts(self.root, older_than_days=14, state_dir=self.state)
            self.assertEqual(res_single["compacted"], 1)
            gb2 = (self.root / "receipts/2026-06/genel-ozet.md").read_text(encoding="utf-8")
            self.assertIn("Tekli ek eski iş", gb2)
            self.assertEqual(parse_markdown((self.root / r_g3).read_text(encoding="utf-8"))[0]["status"], "archived")

        # 5. Re-updating a previously compacted receipt via finish_session refreshes its bullet in-place
        with mock.patch.object(vault, "_now", return_value=old_dt + timedelta(days=2)):
            r_g1_upd = vault.finish_session(self.root, "genel", "Güncellenmiş genel özet YENİ", session_id="g1",
                                            harness="claude", state_dir=self.state)["receipt"]
            self.assertEqual(r_g1_upd, r_g1)
            self.assertEqual(parse_markdown((self.root / r_g1).read_text(encoding="utf-8"))[0]["status"], "active")
        with mock.patch.object(vault, "_now", return_value=base_now):
            res_upd = vault.compact_receipts(self.root, older_than_days=14, state_dir=self.state)
            self.assertEqual(res_upd["compacted"], 1)
            gb3 = (self.root / "receipts/2026-06/genel-ozet.md").read_text(encoding="utf-8")
            self.assertIn("Güncellenmiş genel özet YENİ", gb3)
            self.assertNotIn("Genel proje işi 1", gb3)
            self.assertEqual(gb3.count(f"[[{r_g1[:-3]}]]"), 1)

        # Owner mismatch check on tampered summary file
        with mock.patch.object(vault, "_now", return_value=old_dt):
            vault.finish_session(self.root, str(self.root), "Tamper test", session_id="v3",
                                 harness="claude", state_dir=self.state)
        v_sum = self.root / "receipts/2026-06/_vault-ozet.md"
        v_sum.write_text(v_sum.read_text(encoding="utf-8").replace("project: null", "project: genel"), encoding="utf-8")
        with mock.patch.object(vault, "_now", return_value=base_now):
            with self.assertRaises(vault.VaultError):
                vault.compact_receipts(self.root, older_than_days=14, state_dir=self.state)


class CliEndToEndTests(IsolatedCase):
    def run_cli(self, *args, cwd=None, expect=0):
        env = os.environ.copy()
        p = subprocess.run([sys.executable, str(MEM_PY), *args], capture_output=True, cwd=str(cwd or self.tmp),
                           env=env, timeout=60)
        out, err = p.stdout.decode("utf-8"), p.stderr.decode("utf-8")
        self.assertEqual(p.returncode, expect, f"{args}\nSTDOUT:{out}\nSTDERR:{err}")
        return out, err

    def test_full_cli_flow(self):
        vault_dir = self.tmp / "v"
        repo = self.make_repo("proj")
        out, _ = self.run_cli("init", "--profile", "work", "--vault", str(vault_dir), "--json")
        self.assertEqual(json.loads(out)["dlp_mode"], "block")
        self.run_cli("project", "add", "demo", "--root", str(repo), "--name", "Demo")
        out, _ = self.run_cli("decide", "--title", "Önbellek stratejisi", "--decision", "Redis kullan",
                              "--rationale", "hız", "--json", cwd=repo)
        decided = json.loads(out)
        self.assertEqual(decided["status"], "created")
        self.run_cli("rule", "Commit mesajı Türkçe olsun", "--reason", "ekip", cwd=repo)
        out, _ = self.run_cli("search", "önbellek", "--json", cwd=repo)
        self.assertIn(decided["path"], [r["doc_id"] for r in json.loads(out)])
        out, _ = self.run_cli("bootstrap", "--task", "redis", cwd=repo)
        self.assertIn("Commit mesajı Türkçe olsun", out)
        out, _ = self.run_cli("finish", "--summary", "Redis eklendi", "--thread", "TTL", "--session", "a", cwd=repo)
        self.assertIn("Makbuz:", out)
        out, _ = self.run_cli("doctor", "--json")
        doc_json = json.loads(out)
        self.assertTrue(doc_json["ok"])
        budget_chk = [c for c in doc_json["checks"] if c["name"] == "Bootstrap kural bütçesi"]
        self.assertEqual(len(budget_chk), 1)
        self.assertEqual(budget_chk[0]["level"], "PASS")
        # Kurallar >= 6000 kr olunca doctor WARN verir
        (vault_dir / "Kurallar.md").write_text(
            (vault_dir / "Kurallar.md").read_text(encoding="utf-8") + ("x" * 6000) + "\n", encoding="utf-8"
        )
        out_w, _ = self.run_cli("doctor", "--json")
        budget_warn = [c for c in json.loads(out_w)["checks"] if c["name"] == "Bootstrap kural bütçesi"][0]
        self.assertEqual(budget_warn["level"], "WARN")
        # yabanci repo: hicbir sey yazilmaz, cikis kodu 5
        foreign = self.make_repo("foreign")
        out, _ = self.run_cli("decide", "--title", "Sızıntı", "--decision", "x", cwd=foreign, expect=5)
        self.assertIn("beyaz listesinde değil", out)
        self.assertEqual(len(list((vault_dir / "projects" / "demo" / "kararlar").glob("*.md"))), 1)
        # DLP: work profilinde sir -> cikis kodu 3, sir stderr'e sizmaz
        secret = "AKIAIOSFODNN7EXAMPLE"
        _, err = self.run_cli("rule", f"anahtar {secret}", cwd=repo, expect=3)
        self.assertNotIn(secret, err)

    def test_doctor_per_project_budget_and_corrupt_utf8_report(self):
        vault_dir = self.tmp / "vdoc"
        repo_a = self.make_repo("alpha")
        repo_b = self.make_repo("beta")
        self.run_cli("init", "--profile", "work", "--vault", str(vault_dir), "--json")
        self.run_cli("project", "add", "alpha", "--root", str(repo_a), "--name", "Alpha", "--tag", "python")
        self.run_cli("project", "add", "beta", "--root", str(repo_b), "--name", "Beta", "--tag", "rust")

        # Tag-matched global rule pushes alpha over 8000 chars while beta stays small
        vault.add_rule(vault_dir, "global", "Alpha için çok uzun kural " + ("a" * 8100),
                       tags=["python"], state_dir=None)
        out, _ = self.run_cli("doctor", "--json")
        doc = json.loads(out)
        self.assertTrue(doc["ok"])
        chk = [c for c in doc["checks"] if c["name"] == "Bootstrap kural bütçesi"][0]
        self.assertEqual(chk["level"], "WARN")
        self.assertIn("alpha", chk["detail"])
        self.assertIn(">8000", chk["detail"])
        self.assertNotIn("beta", chk["detail"])

        # Missing projects/beta/Kurallar.md produces 0 chars and no error
        (vault_dir / "projects" / "beta" / "Kurallar.md").unlink()
        out_missing, _ = self.run_cli("doctor", "--json")
        self.assertTrue(json.loads(out_missing)["ok"])

        # Corrupt UTF-8 in projects/alpha/Kurallar.md does NOT crash; emits FAIL in doctor --json
        (vault_dir / "projects" / "alpha" / "Kurallar.md").write_bytes(b"\xff\xfe\x80\x81")
        out_bad, _ = self.run_cli("doctor", "--json", expect=1)
        doc_bad = json.loads(out_bad)
        self.assertFalse(doc_bad["ok"])
        fail_checks = [c for c in doc_bad["checks"] if c["level"] == "FAIL"]
        self.assertTrue(any("Kural dosyası okunamadı (projects/alpha/Kurallar.md)" in c["name"] for c in fail_checks))


class Round2HardeningTests(VaultBase):
    def test_fenced_checkboxes_untouched_and_noop_finish_skips_thread_rewrite(self):
        tpath = self.root / "projects/order-service/Acik-Konular.md"
        tpath.write_text(
            "# OrderService — Açık Konular\n\n"
            "Örnek şablon:\n\n"
            "```markdown\n"
            "- [ ] Şablon içindeki örnek madde\n"
            "```\n\n"
            "- [ ] Retry  failed jobs\n",
            encoding="utf-8",
        )
        r1 = vault.finish_session(self.root, "order-service", "İlk tur", open_threads=["Yeni görev"],
                                  session_id="s1", harness="claude", state_dir=self.state)
        self.assertEqual(r1["status"], "recorded")
        t1 = tpath.read_text(encoding="utf-8")
        self.assertIn("```markdown\n- [ ] Şablon içindeki örnek madde\n```", t1)
        self.assertNotIn("Şablon içindeki örnek madde <!--", t1)
        self.assertIn("Retry  failed jobs <!--", t1)

        # Degisiklik yokken ikinci finish Acik-Konular.md dosyasini yeniden yazmamali (updated_at degismez)
        bak_dir = self.state / "backups"
        bak_before = len(list(bak_dir.rglob("*Acik-Konular.md*.bak"))) if bak_dir.exists() else 0
        vault.finish_session(self.root, "order-service", "İkinci tur özet", open_threads=["Yeni görev"],
                             session_id="s2", harness="claude", state_dir=self.state)
        self.assertEqual(tpath.read_text(encoding="utf-8"), t1)
        bak_after = len(list(bak_dir.rglob("*Acik-Konular.md*.bak"))) if bak_dir.exists() else 0
        self.assertEqual(bak_after, bak_before)

        # Cift bosluklu legacy madde tek bosluklu tam metin secicisiyle kapanir
        rc = vault.finish_session(self.root, "order-service", "Kapatma", close_threads=["Retry failed jobs"],
                                  session_id="s3", harness="codex", state_dir=self.state)
        self.assertEqual(len(rc["closed_threads"]), 1)
        self.assertNotIn("Retry  failed jobs", tpath.read_text(encoding="utf-8"))

    def test_long_project_slugs_distinct_and_compact_summary_guards(self):
        r_api = self.make_repo("long-api")
        r_web = self.make_repo("long-web")
        slug_api = "abcdefghijklmnopqrstuvwx-api"
        slug_web = "abcdefghijklmnopqrstuvwx-web"
        vault.register_project(self.root, slug_api, [r_api], state_dir=self.state)
        vault.register_project(self.root, slug_web, [r_web], state_dir=self.state)
        vault.add_rule(self.root, "global", "Yalnız API uzun slug kuralı", tags=[slug_api], state_dir=self.state)
        b_api = vault.bootstrap_context(self.root, self.state, cwd=r_api)
        b_web = vault.bootstrap_context(self.root, self.state, cwd=r_web)
        self.assertIn("Yalnız API uzun slug kuralı", b_api["text"])
        self.assertNotIn("Yalnız API uzun slug kuralı", b_web["text"])

        # compact_receipts: arsivlenmis ozet dosyasi kaynaklari yutmamali + baslik disi link eslesmemeli
        old_dt = datetime(2025, 5, 10, 12, 0)
        with mock.patch.object(vault, "_now", return_value=old_dt):
            ra = vault.finish_session(self.root, "order-service", "Özet A ilk", session_id="sa",
                                      harness="claude", state_dir=self.state)
            rb = vault.finish_session(self.root, "order-service",
                                      f"Özet B referans ([[{ra['receipt'][:-3]}]]): detay",
                                      session_id="sb", harness="claude", state_dir=self.state)
        vault.compact_receipts(self.root, older_than_days=14, project="order-service", state_dir=self.state)
        sum_path = self.root / "receipts/2025-05/order-service-ozet.md"
        # A makbuzunu guncelleyip yeniden sikistirinca B'nin satiri ezilmemeli
        with mock.patch.object(vault, "_now", return_value=old_dt):
            vault.finish_session(self.root, "order-service", "Özet A güncel", session_id="sa",
                                 harness="claude", state_dir=self.state)
        vault.compact_receipts(self.root, older_than_days=14, project="order-service", state_dir=self.state)
        stext = sum_path.read_text(encoding="utf-8")
        self.assertIn("Özet A güncel", stext)
        self.assertIn("Özet B referans", stext)

        # Ozet dosyasi status: archived yapilirsa compact_receipts hata vermeli
        smeta, sbody = parse_markdown(stext)
        smeta["status"] = "archived"
        from mem.frontmatter import dump_markdown
        sum_path.write_text(dump_markdown(smeta, sbody), encoding="utf-8")
        with mock.patch.object(vault, "_now", return_value=old_dt):
            vault.finish_session(self.root, "order-service", "Özet C", session_id="sc",
                                 harness="claude", state_dir=self.state)
        with self.assertRaises(vault.VaultError):
            vault.compact_receipts(self.root, older_than_days=14, project="order-service", state_dir=self.state)


if __name__ == "__main__":
    unittest.main()
