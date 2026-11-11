import hashlib
import json
import os
import sys
import unittest
from pathlib import Path

from _helpers import MEM_PY, IsolatedCase

from mem import config, harness, migrate, vault
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
        self.assertEqual(entry["args"], [str(MEM_PY), "mcp", "--profile", "work"])
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
        self.assertEqual(data["mcpServers"]["mem"]["args"][-1], "personal")
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
        self.assertEqual(claude["mcpServers"]["mem"]["args"][1:], ["mcp", "--profile", "work"])
        self.assertTrue((cfg / ".claude" / "CLAUDE.md").exists())
        harness.connect_harness("antigravity", "work", config_root_override=cfg)
        self.assertTrue((cfg / ".gemini" / "config" / "GEMINI.md").exists())
        harness.connect_harness("claude-desktop", "personal", config_root_override=cfg)
        cd = json.loads((cfg / "claude-desktop" / "claude_desktop_config.json").read_text(encoding="utf-8"))
        self.assertEqual(cd["mcpServers"]["mem"]["args"][1:], ["mcp", "--profile", "personal", "--vault-scope"])

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
        v3 = self.tmp / "ikinciBeyin"
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
                              ["projects/orderservice/Genel-Bakis.md", "projects/orderservice/Kurallar.md"])
        self.assertFalse((root / "knowledge/ilke.md").exists())

        res = migrate.import_from_beyin_v3(v3, root, state_dir=state)
        self.assertEqual(tree_hash(v3), before)  # kaynak degismedi
        self.assertCountEqual(res["imported"], [
            "projects/orderservice/kararlar/2026-01-01-redis.md",
            "projects/orderservice/Genel-Bakis.md",
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
        hub = (root / "projects/orderservice/Genel-Bakis.md").read_text(encoding="utf-8")
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
        self.write_md(v3, "🔮 850-Companion/Core.md", "# Düşünme ortağı\n\n- Kısa ve net cevap\n")
        self.write_md(v3, "🔮 850-Companion/Last-Session.md", "# Son oturum\n\nDevir özeti\n")
        self.write_md(v3, "🔮 850-Companion/Threads.md", "# Threads\n\nAçık konu 1\n")
        self.write_md(v3, "knowledge/v3/outcomes.md",
                      '---\n{"generated": true, "kind": "receipt-index"}\n---\n# Index\n')
        self.write_md(v3, "notes/projects/AuthHub/Genel-Bakis.md",
                      "# AuthHub\n\n<!-- beyin-baglantilar -->\nEski bağlantı\n<!-- beyin-baglantilar -->\n")
        self.write_md(v3, "notes/projects/AuthHub/kararlar/2026-09-07-ilk.md",
                      '---\n{"kind": "fact", "decision_date": "2026-09-07", "title": "İlk karar"}\n---\n'
                      "# İlk karar\n\n<!-- beyin-baglantilar -->\n"
                      "Proje: [[notes/projects/AuthHub/Genel-Bakis|AuthHub genel bakış]]\n"
                      "<!-- beyin-baglantilar -->\n")
        self.write_md(v3, "notes/projects/AuthHub/kararlar/2026-09-10-yeni.md",
                      '---\n{"kind": "fact", "title": "Yeni karar", '
                      '"supersedes": ["notes/projects/AuthHub/kararlar/2026-09-07-ilk.md"]}\n---\n'
                      "# Yeni karar\n")
        self.write_md(v3, "receipts/ffe2d0dc0950ec79d6cdc488cf8b62fd04bfe1cfe22a7757234eab083dcfe2cd.md",
                      '---\n{"kind": "receipt", "created_at": "2026-11-08T09:22:10+00:00", '
                      '"refs": ["notes/projects/AuthHub/Genel-Bakis.md"]}\n---\nWebhook testi geçti.\n')

        root, state = self.make_vault("personal")
        repo = self.make_repo("authhub")
        vault.register_project(root, "authhub", [repo], name="AuthHub")
        res = migrate.import_from_beyin_v3(v3, root, state_dir=state)

        self.assertEqual(res["skipped_generated"], ["knowledge/v3/outcomes.md"])
        self.assertFalse((root / "knowledge/v3/outcomes.md").exists())
        self.assertIn("Kısa ve net cevap", (root / "Core.md").read_text(encoding="utf-8"))
        self.assertTrue((root / "knowledge/companion/Son-Oturum-Gecmisi.md").exists())
        self.assertTrue((root / "knowledge/companion/Genel-Acik-Konular.md").exists())
        self.assertTrue((root / "receipts/2026-11/ffe2d0dc0950ec79.md").exists())

        old_meta, old_body = parse_markdown(
            (root / "projects/authhub/kararlar/2026-09-07-ilk.md").read_text(encoding="utf-8"))
        self.assertEqual(old_meta["kind"], "decision")
        self.assertEqual(old_meta["date"], "2026-09-07")
        self.assertEqual(old_meta["status"], "superseded")
        self.assertIn("[[projects/authhub/Genel-Bakis|AuthHub genel bakış]]", old_body)
        self.assertNotIn("beyin-baglantilar", old_body)

        hits = vault.search_memory(root, "karar", project="authhub", kind="decision", state_dir=state)
        self.assertEqual([h["doc_id"] for h in hits], ["projects/authhub/kararlar/2026-09-10-yeni.md"])

    def test_source_target_overlap_rejected(self):
        root, state = self.make_vault("work")
        with self.assertRaises(ValueError):
            migrate.import_from_beyin_v3(root, root, state_dir=state)
        with self.assertRaises(FileNotFoundError):
            migrate.import_from_beyin_v3(self.tmp / "yok", root, state_dir=state)


if __name__ == "__main__":
    unittest.main()
