"""Bagimsiz denetim (#4) bulgulari icin regresyon testleri: BLOCKER-1..5, MAJOR-1..3, MINOR-1..3."""

import json
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path

from _helpers import MEM_PY, ROOT, SRC, IsolatedCase

from mem import config, dlp, harness, migrate, store, vault
from mem.frontmatter import dump_markdown, parse_markdown
from mem.mcp_server import McpServer
from mem.turkish import stem_tr

AWS = "AKIAIOSFODNN7EXAMPLE"


class TwoProjectBase(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.root, self.state = self.make_vault("personal")
        self.alpha = self.make_repo("alpha")
        self.beta = self.make_repo("beta")
        self.foreign = self.make_repo("foreign")
        vault.register_project(self.root, "alpha", [self.alpha], name="Alpha")
        vault.register_project(self.root, "beta", [self.beta], name="Beta")
        self.beta_dec = vault.record_decision(self.root, "beta", "Beta gizli karar", "Beta kuantum sırrı",
                                              state_dir=self.state)["path"]
        self.server = McpServer(self.root, self.state, default_cwd=str(self.alpha))

    def call(self, tool, **args):
        res = self.server.call_tool(tool, args)
        text = res["content"][0]["text"]
        return res, text


class Blocker1ScopeTests(TwoProjectBase):
    def test_project_param_cannot_bypass_unregistered_cwd(self):
        for project in ("alpha", "beta"):
            scope = vault.resolve_scope(self.root, cwd=self.foreign, project=project)
            self.assertEqual(scope["status"], "unregistered_workspace")
        for project in ("global", "_vault", "shared"):
            with self.assertRaises(vault.VaultError):
                vault.resolve_scope(self.root, cwd=self.foreign, project=project)
            with self.assertRaises(vault.VaultError):
                vault.resolve_scope(self.root, cwd=self.alpha, project=project)
        for tool, args in (("memory_search", {"query": "kuantum"}),
                           ("memory_read", {"path": self.beta_dec}),
                           ("memory_patch", {"path": self.beta_dec, "expected_sha256": "0" * 64,
                                             "edits": [{"old": "a", "new": "b"}]}),
                           ("memory_bootstrap", {}), ("memory_rule", {"rule": "x"})):
            res, text = self.call(tool, cwd=str(self.foreign), project="beta", **args)
            self.assertIn("unregistered_workspace", text, tool)
            self.assertNotIn("kuantum", text.replace("kuantum\"", ""))
        res, text = self.call("memory_search", cwd=str(self.foreign), project="global", query="kuantum")
        self.assertTrue(res["isError"])
        self.assertNotIn("kuantum", text)

    def test_alpha_cannot_reach_beta_or_widen_scope(self):
        a = str(self.alpha)
        res, text = self.call("memory_search", cwd=a, query="kuantum sırrı")
        self.assertNotIn(self.beta_dec, text)
        for kw in ({"project": "beta"}, {"project": "global"}, {"project": "_vault"}):
            res, text = self.call("memory_search", cwd=a, query="kuantum sırrı", **kw)
            self.assertTrue(res.get("isError"), kw)
            self.assertNotIn("Beta gizli", text)
        res, text = self.call("memory_read", cwd=a, path=self.beta_dec)
        self.assertTrue(res["isError"])
        self.assertNotIn("kuantum", text)
        sha = vault.read_note(self.root, self.beta_dec)["sha256"]
        res, _ = self.call("memory_patch", cwd=a, path=self.beta_dec, expected_sha256=sha,
                           edits=[{"old": "Beta kuantum", "new": "ele geçirildi"}])
        self.assertTrue(res["isError"])
        self.assertIn("Beta kuantum", (self.root / self.beta_dec).read_text(encoding="utf-8"))
        res, _ = self.call("memory_rule", cwd=a, rule="kural", scope="beta")
        self.assertTrue(res["isError"])
        res, _ = self.call("memory_decide", cwd=a, project="beta", title="t", decision="d")
        self.assertTrue(res["isError"])

    def test_shared_decision_only_under_knowledge(self):
        before = sorted(p.relative_to(self.root).as_posix() for p in (self.root / "projects/beta").rglob("*"))
        for i, kw in enumerate(({"shared": True}, {"project": "shared"})):
            res, text = self.call("memory_decide", cwd=str(self.alpha),
                                  title=["Kod inceleme ilkesi", "Şema değişikliği kuralı"][i],
                                  decision=["Her PR için kod incelemesi zorunlu", "Şema yalnızca migration ile değişir"][i],
                                  **kw)
            self.assertFalse(res.get("isError"), text)
            path = json.loads(text)["path"]
            self.assertTrue(path.startswith("knowledge/kararlar/"), path)
        self.assertEqual(before, sorted(p.relative_to(self.root).as_posix()
                                        for p in (self.root / "projects/beta").rglob("*")))
        # yabanci cwd'den shared da yazilamaz
        res, text = self.call("memory_decide", cwd=str(self.foreign), shared=True, title="x", decision="y")
        self.assertIn("unregistered_workspace", text)
        self.assertEqual(len(list((self.root / "knowledge/kararlar").glob("*.md"))), 2)

    def test_archive_hidden_in_project_scope_but_visible_in_vault_scope(self):
        self.write_md(self.root, "archive/2026-01.md", "# Arşiv\n\n<!-- proje: beta -->\nkuantum arşiv kartı\n")
        with self.assertRaises(vault.VaultError):
            vault.read_note(self.root, "archive/2026-01.md", project="alpha")
        sha = vault.read_note(self.root, "archive/2026-01.md")["sha256"]  # kasa kapsami okuyabilir
        with self.assertRaises(vault.VaultError):
            vault.patch_note(self.root, "archive/2026-01.md", sha, [{"old": "kart", "new": "x"}],
                             state_dir=self.state, project="alpha")
        ids = [r["doc_id"] for r in vault.search_memory(self.root, "kuantum arşiv", project="alpha",
                                                        include_archived=True, state_dir=self.state)]
        self.assertNotIn("archive/2026-01.md", ids)
        ids = [r["doc_id"] for r in vault.search_memory(self.root, "kuantum arşiv", include_archived=True,
                                                        state_dir=self.state)]
        self.assertIn("archive/2026-01.md", ids)

    def test_cli_project_with_foreign_cwd(self):
        p = subprocess.run([sys.executable, str(MEM_PY), "search", "kuantum", "--vault", str(self.root),
                            "--project", "beta", "--cwd", str(self.foreign), "--json"],
                           capture_output=True, env=os.environ.copy(), timeout=60)
        self.assertEqual(p.returncode, 5, p.stderr.decode("utf-8"))
        self.assertNotIn("kuantum", p.stdout.decode("utf-8").replace("kuantum\"", ""))


class Blocker2SupersedesTests(TwoProjectBase):
    def test_cannot_supersede_foreign_or_non_decision_targets(self):
        before_core = (self.root / "Core.md").read_bytes()
        before_beta = (self.root / self.beta_dec).read_bytes()
        for target in (self.beta_dec, "Core.md", "projects/beta/Kurallar.md",
                       "projects/alpha/kararlar/../../beta/kararlar/" + Path(self.beta_dec).name,
                       "../outside.md", "knowledge/kararlar/yok.md"):
            with self.assertRaises(vault.VaultError, msg=target):
                vault.record_decision(self.root, "alpha", "Yeni", "x", supersedes=[target], state_dir=self.state)
        self.assertEqual(before_core, (self.root / "Core.md").read_bytes())
        self.assertEqual(before_beta, (self.root / self.beta_dec).read_bytes())
        self.assertEqual(list((self.root / "projects/alpha/kararlar").glob("*.md")), [])

    def test_same_scope_chain_still_works(self):
        a1 = vault.record_decision(self.root, "alpha", "Birinci", "A", state_dir=self.state)["path"]
        a2 = vault.record_decision(self.root, "alpha", "İkinci", "B", supersedes=[a1], state_dir=self.state)["path"]
        a3 = vault.record_decision(self.root, "alpha", "Üçüncü", "C", supersedes=[a2], state_dir=self.state)
        self.assertEqual(a3["superseded"], [a2])
        ids = [r["doc_id"] for r in vault.search_memory(self.root, "birinci ikinci üçüncü", project="alpha",
                                                        state_dir=self.state)]
        self.assertEqual([i for i in ids if "kararlar" in i], [a3["path"]])


class Blocker3SymlinkTests(IsolatedCase):
    def make_link(self, link: Path, target: Path) -> bool:
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            if os.name == "nt":
                import _winapi
                _winapi.CreateJunction(str(target), str(link))
            else:
                os.symlink(target, link, target_is_directory=True)
            return True
        except (OSError, AttributeError):
            return False

    def test_junction_or_symlink_dir_pointing_outside_is_not_indexed(self):
        root, state = self.make_vault("personal")
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "gizli.md").write_text("# Gizli\n\ndisaridakisirkelimesi\n", encoding="utf-8")
        if not self.make_link(root / "knowledge" / "ext", outside):
            self.skipTest("junction/symlink olusturulamadi")
        self.assertEqual(vault.search_memory(root, "disaridakisirkelimesi", state_dir=state), [])
        conn = store.connect(state)
        try:
            store.sync_vault(conn, root)
            self.assertIsNone(store.get_doc(conn, "knowledge/ext/gizli.md"))
        finally:
            conn.close()
        with self.assertRaises(ValueError):
            vault.read_note(root, "knowledge/ext/gizli.md")

    def test_file_symlink_not_indexed(self):
        root, state = self.make_vault("personal")
        outside = self.tmp / "dis.md"
        outside.write_text("# Dis\n\ndisdosyakelimesi\n", encoding="utf-8")
        try:
            os.symlink(outside, root / "knowledge" / "link.md")
        except (OSError, NotImplementedError):
            self.skipTest("dosya symlink'i olusturulamadi")
        self.assertEqual(vault.search_memory(root, "disdosyakelimesi", state_dir=state), [])

    def test_import_v3_ignores_outside_junction(self):
        v3 = self.tmp / "v3"
        self.write_md(v3, "knowledge/ok.md", "# Tamam\n\nicerde\n")
        outside = self.tmp / "outside2"
        outside.mkdir()
        (outside / "gizli.md").write_text("# G\n\nsizinti\n", encoding="utf-8")
        if not self.make_link(v3 / "knowledge" / "ext", outside):
            self.skipTest("junction/symlink olusturulamadi")
        root, state = self.make_vault("personal")
        res = migrate.import_from_beyin_v3(v3, root, state_dir=state)
        self.assertEqual(res["imported"], ["knowledge/ok.md"])


class Blocker4HarnessPathTests(IsolatedCase):
    def test_antigravity_global_mcp_path(self):
        cfg = self.tmp / "home"
        res = harness.connect_harness("antigravity", "work", config_root_override=cfg)
        paths = {Path(f["path"]).relative_to(cfg).as_posix() for f in res["files"]}
        self.assertEqual(paths, {".gemini/config/mcp_config.json", ".gemini/config/GEMINI.md"})
        data = json.loads((cfg / ".gemini/config/mcp_config.json").read_text(encoding="utf-8"))
        self.assertEqual(data["mcpServers"]["mem"]["args"][1:], ["mcp", "--profile", "work"])
        self.assertFalse((cfg / ".gemini" / "antigravity").exists())


class Blocker5DlpTests(IsolatedCase):
    def cats(self, text):
        return {f["category"] for f in dlp.scan(text, None)}

    def test_env_style_secrets(self):
        for line in ("DB_PASSWORD=hunter2hunter2", "POSTGRES_PASSWORD=Sup3rS3cret!", "JWT_SECRET=abcdef0123456789",
                     "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "OAUTH_CLIENT_SECRET=zzzz1111yyyy",
                     "secret_key = 'abcdefgh1234'", "access_key: abcdefgh1234", "private_key=abcdefgh1234",
                     "REDIS_PWD=abcdefgh1234", '{"password": "abcdefgh1234"}', "export STRIPE_API_KEY=abcdefgh1234"):
            self.assertIn("generic_secret_assignment", self.cats(line), line)

    def test_ado_any_order(self):
        for s in ("User Id=sa;Password=S3cret!;Server=sql.corp.local;",
                  "User Id=sa;Pwd=S3cret123!;Server=sql.corp.local;",
                  "Server=sql;Database=x;User Id=sa;Password=abc;",
                  "Data Source=db;Initial Catalog=x;Pwd=ab;"):
            self.assertIn("connection_string", self.cats(s), s)
        self.assertEqual(self.cats("Password policy is documented in the wiki"), set())

    def test_localhost_uri_with_query_secret(self):
        for s in ("postgres://localhost:5432/mydb?user=postgres&pwd=SuperSecret123",
                  "postgres://localhost/db?password=secret1", "redis://127.0.0.1:6379/0?token=abc"):
            self.assertIn("connection_string", self.cats(s), s)
        self.assertEqual(self.cats("postgres://localhost:5432/mydb"), set())

    def test_truncated_pem_and_pgp_redacted_fully(self):
        body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7\nabcdEFGH1234+/abcdEFGH1234+/abcdEFGH1234\n"
        for header in ("-----BEGIN PRIVATE KEY-----", "-----BEGIN RSA PRIVATE KEY-----",
                       "-----BEGIN OPENSSH PRIVATE KEY-----", "-----BEGIN PGP PRIVATE KEY BLOCK-----"):
            text = f"önce\n{header}\n{body}"
            cleaned, _ = dlp.inspect_and_apply(text, "redact", None)
            self.assertIn("[REDACTED:private_key]", cleaned, header)
            self.assertNotIn("MIIEvQ", cleaned)
            self.assertNotIn("abcdEFGH", cleaned)
            self.assertTrue(cleaned.startswith("önce\n"))
        full = f"-----BEGIN PRIVATE KEY-----\n{body}-----END PRIVATE KEY-----\nsonra"
        cleaned, _ = dlp.inspect_and_apply(full, "redact", None)
        self.assertEqual(cleaned, "[REDACTED:private_key]\nsonra")
        with self.assertRaises(dlp.DLPBlockedError):
            dlp.inspect_and_apply(f"-----BEGIN PGP PRIVATE KEY BLOCK-----\n{body}", "block", None)


class Major1ImportTests(IsolatedCase):
    def make_v3(self):
        v3 = self.tmp / "v3"
        self.write_md(v3, "projects/OrderService/Kurallar.md", "# Kurallar\n\n- v3 kuralı bir\n- v3 kuralı iki\n")
        self.write_md(v3, "notes/projects/OrderService/Genel-Bakis.md", "# Genel\n\nv3 mimari özeti\n")
        self.write_md(v3, "notes/projects/OrderService/kararlar/2026-01-01-redis.md",
                      '---\n{"title": "Redis kararı", "kind": "decision"}\n---\n# Redis\n\n**Karar:** Redis\n')
        return v3

    def test_dashed_slug_matched_and_scaffold_replaced(self):
        root, state = self.make_vault("personal")
        repo = self.make_repo("orders")
        vault.register_project(root, "order-service", [repo], name="OrderService")
        res = migrate.import_from_beyin_v3(self.make_v3(), root, state_dir=state)
        self.assertEqual(res["unregistered_projects"], [])
        self.assertFalse((root / "projects/orderservice").exists())
        self.assertCountEqual(res["replaced_scaffold"], ["projects/order-service/Kurallar.md",
                                                         "projects/order-service/OrderService.md"])
        rules = (root / "projects/order-service/Kurallar.md").read_text(encoding="utf-8")
        self.assertIn("v3 kuralı iki", rules)
        meta, _ = parse_markdown(rules)
        self.assertEqual(meta["project"], "order-service")
        self.assertEqual(meta["kind"], "rule")
        hub = (root / "projects/order-service/OrderService.md").read_text(encoding="utf-8")
        self.assertIn("v3 mimari özeti", hub)
        self.assertIn("<!-- mem-links -->", hub)
        self.assertIn("2026-01-01-redis", hub)  # hub blogu yenilendi
        hits = vault.search_memory(root, "redis", project="order-service", state_dir=state)
        self.assertIn("projects/order-service/kararlar/2026-01-01-redis.md", [h["doc_id"] for h in hits])
        bootstrap = vault.bootstrap_context(root, state, project="order-service")
        self.assertIn("v3 kuralı bir", bootstrap["text"])

    def test_modified_scaffold_is_not_overwritten(self):
        root, state = self.make_vault("personal")
        vault.register_project(root, "order-service", [self.make_repo("o")], name="OrderService")
        vault.add_rule(root, "order-service", "Elle eklenmiş kural", state_dir=state)
        res = migrate.import_from_beyin_v3(self.make_v3(), root, state_dir=state)
        self.assertIn("projects/order-service/Kurallar.md", res["skipped_existing"])
        self.assertIn("Elle eklenmiş kural", (root / "projects/order-service/Kurallar.md").read_text(encoding="utf-8"))
        self.assertNotIn("v3 kuralı", (root / "projects/order-service/Kurallar.md").read_text(encoding="utf-8"))


class PgpArmorBlankLineTests(unittest.TestCase):
    def test_truncated_pgp_with_blank_armor_line_redacted_in_full(self):
        text = ("önce\n-----BEGIN PGP PRIVATE KEY BLOCK-----\nVersion: GnuPG v2\nComment: x\n\n"
                "lQOYBGeABCDEFGH1234567890abcdefghij\n=ab12\nsonra satır")
        cleaned, _ = dlp.inspect_and_apply(text, "redact", None)
        self.assertNotIn("lQOYBGe", cleaned)
        self.assertNotIn("GnuPG", cleaned)
        self.assertTrue(cleaned.startswith("önce\n[REDACTED:private_key]"))
        with self.assertRaises(dlp.DLPBlockedError):
            dlp.inspect_and_apply(text, "block", None)


class GlobalMcpCwdTests(IsolatedCase):
    """IDE sunucuyu kayitsiz bir dizinde (ör. %USERPROFILE%) baslatir; ajan cwd'yi bazen atlar."""

    def setUp(self):
        super().setUp()
        self.root, self.state = self.make_vault("work")
        self.repo = self.make_repo("OrderService")
        self.other = self.make_repo("Other")
        self.fake_home = self.make_repo("fake_home")
        self.foreign = self.make_repo("foreign")
        vault.register_project(self.root, "order-service", [self.repo], name="OrderService")
        vault.register_project(self.root, "other", [self.other], name="Other")

    def test_schema_requires_cwd_for_bootstrap(self):
        from mem.mcp_server import TOOLS
        boot = next(t for t in TOOLS if t["name"] == "memory_bootstrap")
        self.assertEqual(boot["inputSchema"]["required"], ["cwd"])
        props = boot["inputSchema"]["properties"]
        self.assertIn("tek başına kapsam açmaz", props["project"]["description"])
        self.assertIn("AKTİF", props["cwd"]["description"])

    def test_real_subprocess_unregistered_server_cwd(self):
        from test_mcp_stdio import StdioClient, text_of
        c = StdioClient(self.fake_home, os.environ.copy())
        try:
            c.send("initialize", {"protocolVersion": "2025-03-26"})
            # bootstrap oncesi cwd'siz cagri: yaniltici unregistered_workspace degil, acik hata
            r = c.call("memory_search", query="x")
            self.assertTrue(r["isError"])
            self.assertIn("cwd gerekli", text_of(r))
            boot = c.call("memory_bootstrap", cwd=str(self.repo))
            self.assertNotIn("isError", boot)
            # cwd ATLANARAK tum araclar calisir
            dec = c.call("memory_decide", title="Önbellek", decision="Redis kullan")
            self.assertNotIn("isError", dec, text_of(dec))
            path = json.loads(text_of(dec))["path"]
            self.assertTrue(path.startswith("projects/order-service/kararlar/"))
            found = json.loads(text_of(c.call("memory_search", query="önbellek")))
            self.assertIn(path, [x["doc_id"] for x in found["results"]])
            read = json.loads(text_of(c.call("memory_read", path=path)))
            patched = c.call("memory_patch", path=path, expected_sha256=read["sha256"],
                             edits=[{"old": "Redis kullan", "new": "Valkey kullan"}])
            self.assertNotIn("isError", patched)
            self.assertNotIn("isError", c.call("memory_rule", rule="Testsiz merge yok"))
            fin = json.loads(text_of(c.call("memory_finish", summary="bitti", session_id="g1")))
            self.assertEqual(fin["project"], "order-service")
            # cwd verilerek de calisir; baska kayitli projeye acikca gecis mumkun
            self.assertNotIn("isError", c.call("memory_search", query="valkey", cwd=str(self.repo)))
            own = json.loads(text_of(c.call("memory_search", query="valkey", cwd=str(self.other))))
            self.assertNotIn(path, [x["doc_id"] for x in own["results"]])
            # kayitsiz repoya gecis: bootstrap reddedilir ve yedek cwd SIFIRLANIR
            gone = json.loads(text_of(c.call("memory_bootstrap", cwd=str(self.foreign))))
            self.assertEqual(gone["status"], "unregistered_workspace")
            after = c.call("memory_decide", title="Sızıntı", decision="olmamalı")
            self.assertTrue(after["isError"])
            self.assertIn("cwd gerekli", text_of(after))
            # acik kayitsiz cwd'li cagri de reddedilir
            fo = json.loads(text_of(c.call("memory_search", query="valkey", cwd=str(self.foreign))))
            self.assertEqual(fo["status"], "unregistered_workspace")
        finally:
            c.close()
        self.assertEqual(len(list((self.root / "projects/order-service/kararlar").glob("*.md"))), 1)
        self.assertEqual(list((self.root / "projects/other/kararlar").glob("*.md")), [])

    def test_registered_server_cwd_wins_without_bootstrap(self):
        server = McpServer(self.root, self.state, default_cwd=str(self.repo))
        res = server.call_tool("memory_search", {"query": "genel"})
        self.assertNotIn("isError", res)
        server2 = McpServer(self.root, self.state, default_cwd=str(self.fake_home))
        res = server2.call_tool("memory_search", {"query": "genel"})
        self.assertTrue(res["isError"])
        server2.call_tool("memory_bootstrap", {"cwd": str(self.other)})
        self.assertEqual(server2._bootstrapped_cwd, str(self.other))
        server2.call_tool("memory_bootstrap", {"cwd": str(self.foreign)})
        self.assertIsNone(server2._bootstrapped_cwd)


class Major2ShadowingTests(unittest.TestCase):
    def run_py(self, args, cwd, pythonpath=None):
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        if pythonpath:
            env["PYTHONPATH"] = pythonpath
        return subprocess.run([sys.executable, *args], capture_output=True, cwd=str(cwd), env=env, timeout=60)

    def test_import_mem_from_repo_root_loads_real_package(self):
        p = self.run_py(["-c", "import mem, mem.cli; print(mem.__file__); print(mem.__version__)"], ROOT)
        out = p.stdout.decode("utf-8").splitlines()
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8"))
        self.assertTrue(Path(out[0]).resolve().is_relative_to(SRC.resolve()), out[0])
        self.assertEqual(out[1], "0.1.0")

    def test_python_dash_m_mem_variants(self):
        for cwd, pp in ((ROOT, None), (ROOT, str(SRC)), (SRC, None), (Path(os.environ.get("TEMP", ROOT)), str(SRC))):
            p = self.run_py(["-m", "mem", "--version"], cwd, pythonpath=pp)
            self.assertEqual(p.returncode, 0, f"{cwd} {pp}: {p.stderr.decode('utf-8')}")
            self.assertIn("mem 0.1.0", p.stdout.decode("utf-8"))
        p = self.run_py([str(MEM_PY), "--version"], Path(os.environ.get("TEMP", ROOT)))
        self.assertEqual(p.returncode, 0)


class Major3McpValidationTests(TwoProjectBase):
    def rpc(self, msg):
        return self.server.handle(msg)

    def test_invalid_requests(self):
        r = self.rpc({"jsonrpc": "2.0", "id": 10})
        self.assertEqual((r["id"], r["error"]["code"]), (10, -32600))
        self.assertIsNone(self.rpc({"jsonrpc": "2.0", "id": 11, "result": {}}))
        self.assertIsNone(self.rpc({"jsonrpc": "2.0", "id": 12, "error": {"code": 1, "message": "x"}}))
        self.assertEqual(self.rpc({"jsonrpc": "2.0", "id": 13, "method": 5})["error"]["code"], -32600)
        self.assertEqual(self.rpc([1])["error"]["code"], -32600)
        self.assertEqual(self.rpc({"jsonrpc": "2.0", "id": 14, "method": "ping", "params": [1, 2]})["error"]["code"],
                         -32602)
        self.assertEqual(self.rpc({"jsonrpc": "2.0", "id": 15, "method": "prompts/get",
                                   "params": {"name": "start", "arguments": "str"}})["error"]["code"], -32602)
        self.assertEqual(self.rpc({"jsonrpc": "2.0", "id": 16, "method": "tools/call",
                                   "params": {"name": ["x"], "arguments": {}}})["error"]["code"], -32602)
        self.assertEqual(self.rpc({"jsonrpc": "2.0", "id": 17, "method": "tools/call",
                                   "params": {"name": "memory_search", "arguments": [1]}})["error"]["code"], -32602)

    def test_empty_batch_over_stdio_framing(self):
        import io
        from mem.mcp_server import serve
        out = io.BytesIO()
        serve(self.server, io.BytesIO(b"[]\n"), out)
        self.assertEqual(json.loads(out.getvalue())["error"]["code"], -32600)

    def test_bad_tool_argument_types_are_iserror_not_internal(self):
        cwd = str(self.alpha)
        bad_calls = [
            ("memory_decide", {"title": "t", "decision": "d", "supersedes": "notalist"}),
            ("memory_decide", {"title": "t", "decision": "d", "sources": [123]}),
            ("memory_decide", {"title": "t", "decision": "d", "tags": "a"}),
            ("memory_decide", {"title": 5, "decision": "d"}),
            ("memory_finish", {"summary": "s", "open_threads": "x"}),
            ("memory_finish", {"summary": "s", "refs": [None, 1]}),
            ("memory_search", {"query": ["a"]}),
            ("memory_search", {"query": "a", "limit": "çok"}),
            ("memory_patch", {"path": "x.md", "expected_sha256": "0", "edits": [{"old": 1, "new": 2}]}),
            ("memory_read", {"path": 5}),
            ("memory_rule", {"rule": "r", "reason": 3}),
        ]
        for tool, args in bad_calls:
            r = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": tool, "arguments": {"cwd": cwd, **args}}})
            self.assertIn("result", r, (tool, args))
            self.assertTrue(r["result"].get("isError"), (tool, args, r))


class Minor1FrontmatterTests(unittest.TestCase):
    def test_hash_inside_flow_values(self):
        meta, _ = parse_markdown('---\ntags: ["not #1", "db"]\nnested: {"title": "Redis #1"}\n'
                                 'plain: [a, b] # yorum\nx: "q #2" # yorum\nz: ab # yorum\n---\n\nG')
        self.assertEqual(meta["tags"], ["not #1", "db"])
        self.assertEqual(meta["nested"], {"title": "Redis #1"})
        self.assertEqual(meta["plain"], ["a", "b"])
        self.assertEqual(meta["x"], "q #2")
        self.assertEqual(meta["z"], "ab")
        again, _ = parse_markdown(dump_markdown({"tags": ["not #1"], "nested": {"t": "R #1"}}, "g"))
        self.assertEqual(again, {"tags": ["not #1"], "nested": {"t": "R #1"}})


class Minor2StemTests(unittest.TestCase):
    def test_veritabani_and_guvenlik_families(self):
        for base, forms in (("veritabanı", ["veritabanında", "veritabanları", "veritabanını", "veritabanlarında"]),
                            ("güvenlik", ["güvenliği", "güvenlikte", "güvenliğin", "güvenlikler"])):
            for f in forms:
                self.assertEqual(stem_tr(f), stem_tr(base), f"{f} != {base}")


class Minor3AtomicRegistryTests(IsolatedCase):
    def test_concurrent_registry_and_project_saves(self):
        root, _ = self.make_vault("personal")
        errors = []

        def worker(i):
            try:
                for _ in range(10):
                    config.save_projects(root, [{"slug": f"p{i}", "name": "x", "roots": []}])
                    config.create_profile(f"prof{i}", self.tmp / f"v{i}")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(60)
        self.assertEqual(errors, [])
        self.assertEqual(list(root.glob(".mem-tmp-*")), [])
        json.loads((root / "projects.json").read_text(encoding="utf-8"))
        json.loads(config.profiles_path().read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
