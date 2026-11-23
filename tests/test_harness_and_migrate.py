import hashlib
import json
import os
import sys
import unittest
from pathlib import Path

from _helpers import MEM_PY, ROOT, IsolatedCase

from mem import cli, config, harness, migrate, vault
from mem.frontmatter import parse_markdown

FORBIDDEN = ["powershell", "pwsh", "encodedcommand", "frombase64string", "schtasks", "startup"]
BAD_SECRET = "AKIAIOSFODNN7EXAMPLE"


def tree_hash(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


class HarnessTests(IsolatedCase):
    def test_devin_install_idempotent_uninstall(self):
        cfg = self.tmp / "cfgroot"
        devin = cfg / "devin"
        devin.mkdir(parents=True)
        (devin / "mcp_config.json").write_text(
            json.dumps({"mcpServers": {"other": {"command": "x", "args": []}}, "keep": 1}), encoding="utf-8")
        (devin / "AGENTS.md").write_text("# Benim kurallarım\n\nKendi metnim.\n", encoding="utf-8")
        original_agents = (devin / "AGENTS.md").read_text(encoding="utf-8")

        dry = harness.connect_harness("devin", "work", config_root_override=cfg, dry_run=True)
        self.assertEqual({f["action"] for f in dry["files"]}, {"dry_run"})
        self.assertNotIn("mem", json.loads((devin / "mcp_config.json").read_text(encoding="utf-8"))["mcpServers"])

        res = harness.connect_harness("devin", "work", config_root_override=cfg)
        self.assertEqual([f["action"] for f in res["files"]], ["updated", "updated"])
        data = json.loads((devin / "mcp_config.json").read_text(encoding="utf-8"))
        entry = data["mcpServers"]["mem"]
        self.assertEqual(entry["command"], sys.executable)
        self.assertEqual(entry["args"], [str(MEM_PY), "mcp", "--profile", "work", "--harness", "devin"])
        self.assertTrue(Path(entry["args"][0]).is_file())
        self.assertIn("other", data["mcpServers"])
        self.assertEqual(data["keep"], 1)
        agents = (devin / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn(harness.BLOCK_START, agents)
        self.assertIn(harness.BLOCK_END, agents)
        for tool in ("memory_bootstrap", "memory_decide", "memory_rule", "memory_finish", "unregistered_workspace"):
            self.assertIn(tool, agents)
        self.assertIn("Kendi metnim.", agents)
        self.assertTrue((devin / "mcp_config.json.bak").exists())
        self.assertTrue((devin / "AGENTS.md.bak").exists())

        # idempotent
        again = harness.connect_harness("devin", "work", config_root_override=cfg)
        self.assertEqual({f["action"] for f in again["files"]}, {"unchanged"})
        self.assertEqual((devin / "AGENTS.md").read_text(encoding="utf-8").count(harness.BLOCK_START), 1)
        # profil degisince ayni anahtar guncellenir
        harness.connect_harness("devin", "personal", config_root_override=cfg)
        data = json.loads((devin / "mcp_config.json").read_text(encoding="utf-8"))
        self.assertEqual(data["mcpServers"]["mem"]["args"][1:], ["mcp", "--profile", "personal", "--harness", "devin"])
        self.assertEqual(len(data["mcpServers"]), 2)

        # uninstall: yalniz kendi kayitlarini kaldirir
        un = harness.connect_harness("devin", "work", config_root_override=cfg, uninstall=True)
        self.assertEqual([f["action"] for f in un["files"]], ["updated", "updated"])
        data = json.loads((devin / "mcp_config.json").read_text(encoding="utf-8"))
        self.assertEqual(data, {"mcpServers": {"other": {"command": "x", "args": []}}, "keep": 1})
        self.assertEqual((devin / "AGENTS.md").read_text(encoding="utf-8").strip(), original_agents.strip())
        un2 = harness.connect_harness("devin", "work", config_root_override=cfg, uninstall=True)
        self.assertEqual({f["action"] for f in un2["files"]}, {"unchanged"})

    def test_fresh_install_and_uninstall_leaves_nothing(self):
        for h in harness.HARNESSES:
            cfg = self.tmp / f"root-{h}"
            res = harness.connect_harness(h, "work", config_root_override=cfg)
            self.assertTrue(all(f["action"] == "created" for f in res["files"]), h)
            for f in res["files"]:
                p = Path(f["path"])
                self.assertTrue(p.is_relative_to(cfg), f"{h}: {p} izole kok disinda")
                text = p.read_text(encoding="utf-8").lower()
                for w in FORBIDDEN:
                    self.assertNotIn(w, text, f"{h}/{p.name}: {w}")
                self.assertNotIn("base64", text)
            un = harness.connect_harness(h, "work", config_root_override=cfg, uninstall=True)
            self.assertTrue(all(f["action"] == "removed" for f in un["files"]), (h, un))
            for f in un["files"]:
                self.assertFalse(Path(f["path"]).exists())

    def test_codex_toml_block_and_claude_json(self):
        cfg = self.tmp / "r"
        harness.connect_harness("codex", "work", config_root_override=cfg)
        toml = (cfg / ".codex" / "config.toml").read_text(encoding="utf-8")
        self.assertIn("[mcp_servers.mem]", toml)
        self.assertIn(json.dumps(sys.executable), toml)
        self.assertIn("# mem-bridge:start", toml)
        harness.connect_harness("claude", "work", config_root_override=cfg)
        claude = json.loads((cfg / ".claude.json").read_text(encoding="utf-8"))
        self.assertEqual(claude["mcpServers"]["mem"]["args"][1:], ["mcp", "--profile", "work", "--harness", "claude"])
        self.assertTrue((cfg / ".claude" / "CLAUDE.md").exists())
        harness.connect_harness("antigravity", "work", config_root_override=cfg)
        self.assertTrue((cfg / ".gemini" / "config" / "GEMINI.md").exists())
        harness.connect_harness("claude-desktop", "personal", config_root_override=cfg)
        cd = json.loads((cfg / "claude-desktop" / "claude_desktop_config.json").read_text(encoding="utf-8"))
        self.assertEqual(cd["mcpServers"]["mem"]["args"][1:],
                         ["mcp", "--profile", "personal", "--harness", "claude-desktop", "--vault-scope"])

    def test_invalid_json_left_untouched_and_unknown_harness(self):
        cfg = self.tmp / "r2"
        (cfg / "devin").mkdir(parents=True)
        bad = cfg / "devin" / "mcp_config.json"
        bad.write_text("{ bozuk", encoding="utf-8")
        with self.assertRaises(ValueError):
            harness.connect_harness("devin", "work", config_root_override=cfg)
        self.assertEqual(bad.read_text(encoding="utf-8"), "{ bozuk")
        with self.assertRaises(ValueError):
            harness.connect_harness("emacs", "work", config_root_override=cfg)

    def test_cli_connect_uses_config_root(self):
        import subprocess
        cfg = self.tmp / "cli-root"
        p = subprocess.run([sys.executable, str(MEM_PY), "connect", "devin", "--profile", "work",
                            "--config-root", str(cfg), "--json"], capture_output=True, env=os.environ.copy(),
                           timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8"))
        self.assertTrue((cfg / "devin" / "mcp_config.json").exists())


class MigrateTests(IsolatedCase):
    def make_v3(self) -> Path:
        v3 = self.tmp / "legacy-vault"
        self.write_md(v3, "notes/projects/OrderService/kararlar/2026-01-01-redis.md",
                      '---\n{"kind": "fact", "project": "OrderService", "title": "Redis kararı", '
                      '"tags": ["db"]}\n---\n# Redis kararı\n\n**Karar:** Redis kullan\n')
        self.write_md(v3, "notes/projects/OrderService/Genel-Bakis.md", "# Genel\n\nSipariş servisi\n")
        self.write_md(v3, "projects/OrderService/Kurallar.md", "# Kurallar\n\n- test yaz\n")
        self.write_md(v3, "knowledge/ilke.md", '---\n{"title": "İlke"}\n---\nKod incelemesi şart\n')
        self.write_md(v3, "notes/genel/not.md", "# Not\n\nserbest not\n")
        self.write_md(v3, "notes/projects/OrderService/kararlar/sizinti.md", f"# S\n\nanahtar {BAD_SECRET}\n")
        self.write_md(v3, "other/ignored.md", "# yok sayilir\n")
        (v3 / ".obsidian").mkdir()
        (v3 / ".obsidian" / "x.md").write_text("gizli", encoding="utf-8")
        return v3

    def test_import_v3_readonly_conversion_and_dlp(self):
        v3 = self.make_v3()
        before = tree_hash(v3)
        root, state = self.make_vault("work")
        repo = self.make_repo("orders")
        vault.register_project(root, "orderservice", [repo], name="OrderService")
        dry = migrate.import_from_beyin_v3(v3, root, dry_run=True, state_dir=state)
        # kayitli projenin DEGISMEMIS iskelet dosyalari (Genel-Bakis, Kurallar) v3 icerigiyle degistirilir
        self.assertEqual(len(dry["imported"]), 5)
        self.assertCountEqual(dry["replaced_scaffold"],
                              ["projects/orderservice/OrderService.md", "projects/orderservice/Kurallar.md"])
        self.assertFalse((root / "knowledge/ilke.md").exists())

        res = migrate.import_from_beyin_v3(v3, root, state_dir=state)
        self.assertEqual(tree_hash(v3), before)  # kaynak degismedi
        self.assertCountEqual(res["imported"], [
            "projects/orderservice/kararlar/2026-01-01-redis.md",
            "projects/orderservice/OrderService.md",
            "projects/orderservice/Kurallar.md",
            "knowledge/ilke.md",
            "knowledge/notes/genel/not.md",
        ])
        self.assertIn("- test yaz", (root / "projects/orderservice/Kurallar.md").read_text(encoding="utf-8"))
        self.assertEqual([b["source"] for b in res["blocked"]],
                         ["notes/projects/OrderService/kararlar/sizinti.md"])
        self.assertNotIn(BAD_SECRET, json.dumps(res))
        self.assertEqual(res["unmapped"], 0)
        self.assertEqual(res["unregistered_projects"], [])

        text = (root / "projects/orderservice/kararlar/2026-01-01-redis.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        self.assertIn("title: Redis kararı\n", text)
        self.assertNotIn("{", text.split("---")[1])  # JSON degil, YAML
        meta, body = parse_markdown(text)
        self.assertEqual(meta["title"], "Redis kararı")
        self.assertEqual(meta["project"], "orderservice")
        self.assertEqual(meta["tags"], ["db"])
        self.assertEqual(meta["imported_from"], "beyin_v3")
        self.assertIn("**Karar:** Redis kullan", body)
        hits = vault.search_memory(root, "redis", project="orderservice", state_dir=state)
        self.assertIn("projects/orderservice/kararlar/2026-01-01-redis.md", [h["doc_id"] for h in hits])
        hub = (root / "projects/orderservice/OrderService.md").read_text(encoding="utf-8")
        self.assertIn("2026-01-01-redis", hub)

        again = migrate.import_from_beyin_v3(v3, root, state_dir=state)
        self.assertEqual(again["imported"], [])
        self.assertEqual(len(again["skipped_existing"]), 5)
        self.assertEqual(tree_hash(v3), before)

    def test_personal_redacts_instead_of_blocking(self):
        v3 = self.make_v3()
        root, state = self.make_vault("personal")
        res = migrate.import_from_beyin_v3(v3, root, state_dir=state)
        self.assertEqual(res["blocked"], [])
        self.assertEqual(res["unregistered_projects"], ["orderservice"])
        txt = (root / "projects/orderservice/kararlar/sizinti.md").read_text(encoding="utf-8")
        self.assertIn("[REDACTED:api_token]", txt)
        self.assertNotIn(BAD_SECRET, txt)

    def test_companion_receipts_generated_and_wikilinks(self):
        v3 = self.tmp / "v3full"
        self.write_md(v3, "00-Companion/Core.md", "# Düşünme ortağı\n\n- Kısa ve net cevap\n")
        self.write_md(v3, "00-Companion/Last-Session.md", "# Son oturum\n\nDevir özeti\n")
        self.write_md(v3, "00-Companion/Threads.md", "# Threads\n\nAçık konu 1\n")
        self.write_md(v3, "knowledge/v3/outcomes.md",
                      '---\n{"generated": true, "kind": "receipt-index"}\n---\n# Index\n')
        self.write_md(v3, "notes/projects/AuthService/Genel-Bakis.md",
                      "# AuthService\n\n<!-- beyin-baglantilar -->\nEski bağlantı\n<!-- beyin-baglantilar -->\n")
        self.write_md(v3, "notes/projects/AuthService/kararlar/2026-09-07-ilk.md",
                      '---\n{"kind": "fact", "decision_date": "2026-09-07", "title": "İlk karar"}\n---\n'
                      "# İlk karar\n\n<!-- beyin-baglantilar -->\n"
                      "Proje: [[notes/projects/AuthService/Genel-Bakis|AuthService genel bakış]]\n"
                      "<!-- beyin-baglantilar -->\n")
        self.write_md(v3, "notes/projects/AuthService/kararlar/2026-09-10-yeni.md",
                      '---\n{"kind": "fact", "title": "Yeni karar", '
                      '"supersedes": ["notes/projects/AuthService/kararlar/2026-09-07-ilk.md"]}\n---\n'
                      "# Yeni karar\n")
        self.write_md(v3, "receipts/a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90.md",
                      '---\n{"kind": "receipt", "created_at": "2026-11-08T09:22:10+00:00", '
                      '"refs": ["notes/projects/AuthService/Genel-Bakis.md"]}\n---\nTest oturumu tamamlandı.\n')

        root, state = self.make_vault("personal")
        repo = self.make_repo("authservice")
        vault.register_project(root, "authservice", [repo], name="AuthService")
        res = migrate.import_from_beyin_v3(v3, root, state_dir=state)

        self.assertEqual(res["skipped_generated"], ["knowledge/v3/outcomes.md"])
        self.assertFalse((root / "knowledge/v3/outcomes.md").exists())
        self.assertIn("Kısa ve net cevap", (root / "Core.md").read_text(encoding="utf-8"))
        self.assertTrue((root / "knowledge/companion/Son-Oturum-Gecmisi.md").exists())
        self.assertTrue((root / "knowledge/companion/Genel-Acik-Konular.md").exists())
        self.assertTrue((root / "receipts/2026-11/a1b2c3d4e5f60718.md").exists())

        old_meta, old_body = parse_markdown(
            (root / "projects/authservice/kararlar/2026-09-07-ilk.md").read_text(encoding="utf-8"))
        self.assertEqual(old_meta["kind"], "decision")
        self.assertEqual(old_meta["date"], "2026-09-07")
        self.assertEqual(old_meta["status"], "superseded")
        self.assertIn("[[projects/authservice/AuthService|AuthService genel bakış]]", old_body)
        self.assertNotIn("beyin-baglantilar", old_body)

        hits = vault.search_memory(root, "karar", project="authservice", kind="decision", state_dir=state)
        self.assertEqual([h["doc_id"] for h in hits], ["projects/authservice/kararlar/2026-09-10-yeni.md"])

    def test_source_target_overlap_rejected(self):
        root, state = self.make_vault("work")
        with self.assertRaises(ValueError):
            migrate.import_from_beyin_v3(root, root, state_dir=state)
        with self.assertRaises(FileNotFoundError):
            migrate.import_from_beyin_v3(self.tmp / "yok", root, state_dir=state)

    def test_receipt_with_null_project_infers_from_refs_and_body(self):
        v3 = self.tmp / "v3receipts"
        self.write_md(
            v3,
            "receipts/r1.md",
            '---\n{"kind": "receipt", "project": null, "created_at": "2026-11-08T09:00:00+00:00", '
            '"refs": ["notes/projects/OrderService/Genel-Bakis.md"]}\n---\n'
            "Sipariş kuyruğu benzersizkelime1 tamamlandı.\n",
        )
        self.write_md(
            v3,
            "receipts/r2.md",
            '---\n{"kind": "receipt", "project": "", "created_at": "2026-11-08T10:00:00+00:00", '
            '"refs": ["notes/projects/OrderService/a.md", "notes/projects/OrderService/b.md", '
            '"notes/projects/AuthService/c.md"]}\n---\n'
            "Çoğunluk benzersizkelime2 tamamlandı.\n",
        )
        self.write_md(
            v3,
            "receipts/r3.md",
            '---\n{"kind": "receipt", "project": null, "created_at": "2026-11-08T11:00:00+00:00", '
            '"refs": ["notes/projects/OrderService/a.md", "notes/projects/AuthService/b.md"]}\n---\n'
            "AuthService: token rotasyonu benzersizkelime3 tamamlandı.\n",
        )
        root, state = self.make_vault("personal")
        vault.register_project(root, "order-service", [self.make_repo("ord")], name="OrderService")
        vault.register_project(root, "authservice", [self.make_repo("auth")], name="AuthService")
        res = migrate.import_from_beyin_v3(v3, root, state_dir=state)
        self.assertEqual(len(res["imported"]), 3)

        m1, _ = parse_markdown((root / "receipts/2026-11/r1.md").read_text(encoding="utf-8"))
        m2, _ = parse_markdown((root / "receipts/2026-11/r2.md").read_text(encoding="utf-8"))
        m3, _ = parse_markdown((root / "receipts/2026-11/r3.md").read_text(encoding="utf-8"))
        self.assertEqual(m1["project"], "order-service")
        self.assertEqual(m2["project"], "order-service")
        self.assertEqual(m3["project"], "authservice")

        # authservice aramasinda order-service makbuzu sizmaz
        auth_hits = [h["doc_id"] for h in vault.search_memory(root, "benzersizkelime1",
                                                               project="authservice", state_dir=state)]
        self.assertEqual(auth_hits, [])

    def test_slug_for_exact_match_precedence_over_normalized(self):
        v3 = self.tmp / "v3slugs"
        self.write_md(
            v3,
            "projects/foobar/Kurallar.md",
            '---\n{"project": "foobar"}\n---\n# Kurallar\n\n- foobar kuralı\n',
        )
        self.write_md(
            v3,
            "notes/projects/foo-bar/kararlar/2026-01-01-k.md",
            '---\n{"title": "K", "project": "foo-bar"}\n---\n# K\n\n**Karar:** X\n',
        )
        self.write_md(
            v3,
            "knowledge/foobar-notu.md",
            '---\n{"title": "Foobar Notu", "project": "foobar"}\n---\n# Foobar Notu\n\nİçerik.\n',
        )
        root, state = self.make_vault("personal")
        vault.register_project(root, "foo-bar", [self.make_repo("fb1")], name="Foo Bar")
        vault.register_project(root, "foobar", [self.make_repo("fb2")], name="FooBar")

        res = migrate.import_from_beyin_v3(v3, root, state_dir=state)
        self.assertIn("projects/foobar/Kurallar.md", res["replaced_scaffold"])
        self.assertIn("projects/foo-bar/kararlar/2026-01-01-k.md", res["imported"])
        self.assertIn("knowledge/foobar-notu.md", res["imported"])
        fb_meta, _ = parse_markdown((root / "knowledge/foobar-notu.md").read_text(encoding="utf-8"))
        self.assertEqual(fb_meta["project"], "foobar")
        self.assertEqual(migrate._slug_for("foobar", config.load_projects(root)), "foobar")
        self.assertEqual(migrate._slug_for("foo-bar", config.load_projects(root)), "foo-bar")


class SetupAndSkillTests(IsolatedCase):
    def _run_cli(self, argv: list[str]) -> tuple[int, dict]:
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(argv)
        return code, json.loads(buf.getvalue())

    def test_detect_and_connected_profile_and_skill_uninstall(self):
        cfg = self.tmp / "cfg"
        (cfg / "devin").mkdir(parents=True)
        (cfg / ".claude.json").write_text("{}", encoding="utf-8")
        (cfg / ".codex").mkdir(parents=True)

        self.assertEqual(harness.detect_installed_harnesses(cfg), ["devin", "claude", "codex"])
        self.assertIsNone(harness.connected_harness_profile("devin", cfg))
        self.assertIsNone(harness.connected_harness_profile("claude", cfg))

        # --profile olmadan eski mem kaydi -> "" donmeli
        (cfg / "devin" / "mcp_config.json").write_text(
            json.dumps({"mcpServers": {"mem": {"command": sys.executable, "args": [str(MEM_PY), "mcp"]}}}),
            encoding="utf-8",
        )
        self.assertEqual(harness.connected_harness_profile("devin", cfg), "")

        # connect_harness(..., install_skill=True) ve uninstall=True
        res = harness.connect_harness("claude", "personal", config_root_override=cfg, install_skill=True)
        skill_dst = cfg / ".claude" / "skills" / "memory" / "SKILL.md"
        self.assertTrue(skill_dst.is_file())
        self.assertEqual(
            skill_dst.read_text(encoding="utf-8").replace("\r\n", "\n"),
            harness.default_skill_source().read_text(encoding="utf-8").replace("\r\n", "\n"),
        )
        self.assertEqual(harness.connected_harness_profile("claude", cfg), "personal")
        self.assertTrue(any(f["path"] == str(skill_dst) and f["action"] == "created" for f in res["files"]))

        un = harness.connect_harness("claude", "personal", config_root_override=cfg, uninstall=True)
        self.assertFalse(skill_dst.exists())
        self.assertTrue((skill_dst.with_name("SKILL.md.bak")).exists())
        self.assertTrue(any(f["path"] == str(skill_dst) and f["action"] == "removed" for f in un["files"]))

        # claude-desktop icin skill yolu None
        res_cd = harness.connect_harness("claude-desktop", "personal", config_root_override=cfg, install_skill=True)
        self.assertEqual(len(res_cd["files"]), 1)

    def test_setup_fresh_install_auto_detects_harnesses_and_installs_skill(self):
        cfg = self.tmp / "cfg-fresh"
        (cfg / "devin").mkdir(parents=True)
        (cfg / ".claude").mkdir(parents=True)
        vault_dir = self.tmp / "work-vault"
        repo = self.make_repo("order-svc")

        code, out = self._run_cli([
            "setup", "--profile", "work", "--vault", str(vault_dir),
            "--project", f"order-svc={repo}", "--config-root", str(cfg), "--json",
        ])
        self.assertEqual(code, 0)
        self.assertEqual(out["mode"], "installed")
        self.assertEqual(out["profile"], "work")
        self.assertEqual(out["dlp_mode"], "block")
        self.assertEqual(config.read_vault_meta(vault_dir)["dlp_mode"], "block")
        self.assertEqual(config.get_profile("work").dlp_mode, "block")

        projects = config.load_projects(vault_dir)
        self.assertEqual([p["slug"] for p in projects], ["order-svc"])
        self.assertEqual(projects[0]["roots"], [str(repo).replace("\\", "/")])

        self.assertEqual([c["harness"] for c in out["connected"]], ["devin", "claude"])
        self.assertEqual(out["skipped_other_profile"], [])

        devin_mcp = json.loads((cfg / "devin" / "mcp_config.json").read_text(encoding="utf-8"))
        self.assertEqual(devin_mcp["mcpServers"]["mem"]["args"],
                         [str(MEM_PY), "mcp", "--profile", "work", "--harness", "devin"])
        claude_mcp = json.loads((cfg / ".claude.json").read_text(encoding="utf-8"))
        self.assertEqual(claude_mcp["mcpServers"]["mem"]["args"],
                         [str(MEM_PY), "mcp", "--profile", "work", "--harness", "claude"])

        expected_skill = (ROOT / "skills" / "memory" / "SKILL.md").read_text(encoding="utf-8").replace("\r\n", "\n")
        self.assertEqual((cfg / "devin" / "skills" / "memory" / "SKILL.md").read_text(encoding="utf-8").replace("\r\n", "\n"),
                         expected_skill)
        self.assertEqual((cfg / ".claude" / "skills" / "memory" / "SKILL.md").read_text(encoding="utf-8").replace("\r\n", "\n"),
                         expected_skill)
        self.assertFalse((cfg / ".codex").exists())
        self.assertFalse((cfg / ".gemini").exists())
        self.assertFalse((cfg / "claude-desktop").exists())

    def test_setup_idempotent_upgrade_preserves_vault_and_skips_other_profile_harness(self):
        cfg = self.tmp / "cfg-upg"
        (cfg / ".claude").mkdir(parents=True)
        (cfg / ".codex").mkdir(parents=True)

        vroot, _state = self.make_vault("work")
        custom_core = "# Core\n\nÖzel kullanıcı tercihi: Kısa yanıt ver.\n"
        (vroot / "Core.md").write_text(custom_core, encoding="utf-8")

        # Eski tarz --harness icermeyen claude kaydi ve baska profile bagli codex kaydi
        (cfg / ".claude.json").write_text(
            json.dumps({"mcpServers": {"mem": {"command": sys.executable, "args": [str(MEM_PY), "mcp", "--profile", "work"]}}}),
            encoding="utf-8",
        )
        harness.connect_harness("codex", "personal", config_root_override=cfg)
        codex_before = (cfg / ".codex" / "config.toml").read_text(encoding="utf-8")

        code, out = self._run_cli(["setup", "--config-root", str(cfg), "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(out["mode"], "upgraded")
        self.assertEqual(out["profile"], "work")
        self.assertEqual((vroot / "Core.md").read_text(encoding="utf-8"), custom_core)

        self.assertEqual([c["harness"] for c in out["connected"]], ["claude"])
        self.assertEqual(out["skipped_other_profile"], [{"harness": "codex", "profile": "personal"}])

        claude_mcp = json.loads((cfg / ".claude.json").read_text(encoding="utf-8"))
        self.assertIn("--harness", claude_mcp["mcpServers"]["mem"]["args"])
        self.assertIn("claude", claude_mcp["mcpServers"]["mem"]["args"])
        self.assertTrue((cfg / ".claude" / "skills" / "memory" / "SKILL.md").is_file())
        self.assertEqual((cfg / ".codex" / "config.toml").read_text(encoding="utf-8"), codex_before)

        # Idempotent ikinci calistirma
        code2, out2 = self._run_cli(["setup", "--config-root", str(cfg), "--json"])
        self.assertEqual(code2, 0)
        self.assertEqual(out2["mode"], "upgraded")
        self.assertEqual({f["action"] for f in out2["connected"][0]["files"]}, {"unchanged"})
        self.assertEqual(out2["skipped_other_profile"], [{"harness": "codex", "profile": "personal"}])

    def test_setup_dry_run_no_skill_auto_slug_and_explicit_harness_override(self):
        cfg = self.tmp / "cfg-opts"
        (cfg / "devin").mkdir(parents=True)
        (cfg / ".codex").mkdir(parents=True)
        harness.connect_harness("codex", "personal", config_root_override=cfg)
        repo_path = self.make_repo("My Service Repo")
        vault_dir = self.tmp / "dry-vault"

        # dry-run: hicbir sey yazmaz
        code_dry, out_dry = self._run_cli([
            "setup", "--profile", "work", "--vault", str(vault_dir),
            "--project", str(repo_path), "--config-root", str(cfg), "--dry-run", "--json",
        ])
        self.assertEqual(code_dry, 0)
        self.assertEqual(out_dry["mode"], "dry_run")
        self.assertEqual(out_dry["projects_added"][0]["slug"], "my-service-repo")
        self.assertFalse(vault_dir.exists())
        self.assertFalse(config.profiles_path().exists())
        self.assertFalse((cfg / "devin" / "mcp_config.json").exists())
        self.assertEqual(out_dry["skipped_other_profile"], [{"harness": "codex", "profile": "personal"}])

        # --no-skill + acik --harness codex ile zorla baglama + Windows-benzeri '=' icermeyen yol
        code_real, out_real = self._run_cli([
            "setup", "--profile", "work", "--vault", str(vault_dir),
            "--project", str(repo_path), "--harness", "devin", "--harness", "codex",
            "--no-skill", "--config-root", str(cfg), "--json",
        ])
        self.assertEqual(code_real, 0)
        self.assertEqual(out_real["mode"], "installed")
        self.assertEqual([c["harness"] for c in out_real["connected"]], ["devin", "codex"])
        self.assertEqual(out_real["skipped_other_profile"], [])
        self.assertFalse((cfg / "devin" / "skills" / "memory" / "SKILL.md").exists())
        self.assertFalse((cfg / ".codex" / "skills" / "memory" / "SKILL.md").exists())
        self.assertEqual(harness.connected_harness_profile("codex", cfg), "work")

        # connect --with-skill alt komutu
        code_conn, out_conn = self._run_cli([
            "connect", "devin", "--profile", "work", "--with-skill", "--config-root", str(cfg), "--json",
        ])
        self.assertEqual(code_conn, 0)
        self.assertTrue((cfg / "devin" / "skills" / "memory" / "SKILL.md").is_file())
        self.assertTrue(any(f["action"] == "created" and f["path"].endswith("SKILL.md") for f in out_conn["files"]))

    def test_readme_setup_and_connect_with_skill_examples_parse(self):
        import re
        import shlex

        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        lines = re.findall(r"^\s*python(?:3)?\s+mem\.py\s+(?:setup|connect)\b.*$", readme, flags=re.MULTILINE)
        setup_cmds = [ln for ln in lines if " mem.py setup" in ln]
        connect_skill_cmds = [ln for ln in lines if " mem.py connect" in ln and "--with-skill" in ln]
        self.assertTrue(setup_cmds, "README.md icinde `python mem.py setup` ornegi bulunamadi")
        self.assertTrue(connect_skill_cmds, "README.md icinde `connect ... --with-skill` ornegi bulunamadi")

        parser = cli.build_parser()
        for raw in setup_cmds + connect_skill_cmds:
            # Dokumantasyondaki opsiyonel `[...]` koseli parantez gosterimlerini ve `<...>` yer tutucularini somutlastir
            expanded = re.sub(r"\[(--[^\]]+)\]", r"\1", raw)
            expanded = expanded.replace("<claude|claude-desktop|codex|antigravity|devin>", "claude")
            expanded = expanded.replace("<work|personal>", "personal")
            expanded = expanded.replace("<yol>", "/tmp/vault")
            expanded = expanded.replace("<slug>", "order-service")
            expanded = re.sub(r"\bredact\|block\b", "redact", expanded)
            expanded = re.sub(r"\boff\|gemini\b", "off", expanded)
            tokens = shlex.split(expanded)
            idx = tokens.index("mem.py")
            ns = parser.parse_args(tokens[idx + 1:])
            self.assertIn(ns.command, ("setup", "connect"))


if __name__ == "__main__":
    unittest.main()
