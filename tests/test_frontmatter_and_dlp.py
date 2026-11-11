import json
import unittest

from _helpers import IsolatedCase

from mem import config, dlp, vault
from mem.frontmatter import dump_markdown, parse_markdown

AWS = "AKIAIOSFODNN7EXAMPLE"
PG = "postgres://appuser:S3cretPass@db.internal:5432/orders"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop"
TCKN = "10000000146"


class FrontmatterTests(unittest.TestCase):
    def test_round_trip(self):
        meta = {
            "title": "Redis: neden? #1",
            "kind": "decision",
            "status": "active",
            "date": "2026-11-11",
            "count": 3,
            "ratio": 0.5,
            "flag": True,
            "off": False,
            "nothing": None,
            "numeric_string": "123",
            "bool_string": "true",
            "tags": ["mimari", "db, cache", "ğüşiöç"],
            "supersedes": [],
            "refs": ["projects/foo/kararlar/2026-01-01-eski.md"],
            "multi": "satır1\nsatır2",
            "padded": "  boşluklu  ",
            "nested": {"a": 1, "b": [1, 2]},
            "link": "[[projects/x/Kurallar|X]]",
        }
        body = "# Başlık\n\nGövde ğüşıöç\n"
        text = dump_markdown(meta, body)
        self.assertTrue(text.startswith("---\n"))
        self.assertIn("status: active\n", text)
        self.assertIn("date: 2026-11-11\n", text)
        meta2, body2 = parse_markdown(text)
        self.assertEqual(meta, meta2)
        self.assertEqual(body, body2)
        self.assertEqual(dump_markdown(meta2, body2), text)  # deterministik

    def test_plain_yaml_variants(self):
        text = (
            "---\r\ntitle: 'Tek ''tırnak'''\r\ntags: [mimari, db]\r\nsupersedes:\r\n"
            '  - "projects/foo/kararlar/2026-01-01-eski.md"\r\n  - b.md\r\nempty: []\r\n'
            "n: ~\r\nflag: yes_not_bool\r\n# yorum\r\nx: 5 # sonek yorum\r\n---\r\n\r\nGövde\r\n"
        )
        meta, body = parse_markdown(text)
        self.assertEqual(meta["title"], "Tek 'tırnak'")
        self.assertEqual(meta["tags"], ["mimari", "db"])
        self.assertEqual(meta["supersedes"], ["projects/foo/kararlar/2026-01-01-eski.md", "b.md"])
        self.assertEqual(meta["empty"], [])
        self.assertIsNone(meta["n"])
        self.assertEqual(meta["flag"], "yes_not_bool")
        self.assertEqual(meta["x"], 5)
        self.assertEqual(body, "Gövde\n")

    def test_json_frontmatter_backward_compat(self):
        text = '---\n{"kind": "fact", "project": "OrderService", "tags": ["a"]}\n---\n# Not\n'
        meta, body = parse_markdown(text)
        self.assertEqual(meta, {"kind": "fact", "project": "OrderService", "tags": ["a"]})
        self.assertEqual(body, "# Not\n")

    def test_no_frontmatter_and_bom(self):
        self.assertEqual(parse_markdown("# Merhaba\n"), ({}, "# Merhaba\n"))
        meta, body = parse_markdown("\ufeff---\na: 1\n---\n\nX")
        self.assertEqual(meta, {"a": 1})
        self.assertEqual(body, "X")
        self.assertEqual(parse_markdown("---\nunclosed: 1\n"), ({}, "---\nunclosed: 1\n"))


class DlpUnitTests(IsolatedCase):
    def test_tckn_checksum(self):
        self.assertTrue(dlp.valid_tckn(TCKN))
        self.assertFalse(dlp.valid_tckn("12345678901"))
        self.assertFalse(dlp.valid_tckn("00000000000"))
        self.assertEqual(dlp.scan("zaman damgasi 12345678901 id", None), [])

    def test_categories_detected(self):
        samples = {
            "api_token": [AWS, "ghp_" + "a" * 36, "sk-" + "b" * 30, "AIza" + "c" * 35, "xoxb-1234567890-abcdef"],
            "private_key": ["-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----"],
            "jwt_bearer": [JWT, "Authorization: Bearer abcdefghijkl", "Set-Cookie: sid=abc123def456; Path=/"],
            "connection_string": [PG, "Server=db1;Database=x;User Id=sa;Password=Xyz12345;"],
            "generic_secret_assignment": ['password = "hunter2hunter2"', "api_key: abcdef123456"],
            "pii_tr": ["TR33 0006 1005 1978 6457 8413 26", TCKN],
        }
        for cat, items in samples.items():
            for s in items:
                cats = {f["category"] for f in dlp.scan(f"not: {s} son", None)}
                self.assertIn(cat, cats, f"{cat} yakalanmadi: {s[:20]}")

    def test_localhost_without_password_not_flagged(self):
        self.assertEqual(dlp.scan("postgres://localhost:5432/dev", None), [])
        self.assertTrue(dlp.scan("redis://:pw123@localhost:6379/0", None))

    def test_work_blocks_and_does_not_leak(self):
        root, _ = self.make_vault("work")
        self.assertEqual(config.read_vault_meta(root)["dlp_mode"], "block")
        (root / "dlp-patterns.txt").write_text("# yorum\n.corp.sirket.com\nregex:PRJ-\\d{4}\n", encoding="utf-8")
        for secret in (AWS, PG, JWT, TCKN, "api.corp.sirket.com", "PRJ-1234"):
            with self.assertRaises(dlp.DLPBlockedError) as cm:
                dlp.inspect_and_apply(f"metin {secret} metin", "block", root)
            self.assertNotIn(secret, str(cm.exception))
            self.assertTrue(all("start" in f and secret not in json.dumps(f) for f in cm.exception.findings))

    def test_work_vault_refuses_to_write_secret(self):
        root, state = self.make_vault("work")
        repo = self.make_repo()
        vault.register_project(root, "svc", [repo], name="Svc")
        with self.assertRaises(dlp.DLPBlockedError):
            vault.record_decision(root, "svc", "Bağlantı", f"Kullan {PG}", state_dir=state)
        with self.assertRaises(dlp.DLPBlockedError):
            vault.add_rule(root, "svc", f"anahtar {AWS}", state_dir=state)
        with self.assertRaises(dlp.DLPBlockedError):
            vault.finish_session(root, "svc", f"token {JWT}", state_dir=state)
        self.assertEqual(list((root / "projects" / "svc" / "kararlar").glob("*.md")), [])
        self.assertEqual(list((root / "receipts").rglob("*.md")), [])
        self.assertNotIn(AWS, (root / "projects" / "svc" / "Kurallar.md").read_text(encoding="utf-8"))

    def test_personal_redacts(self):
        root, state = self.make_vault("personal")
        self.assertEqual(config.read_vault_meta(root)["dlp_mode"], "redact")
        text, findings = dlp.inspect_and_apply(f"a {AWS} b {PG} c", "redact", root)
        self.assertIn("[REDACTED:api_token]", text)
        self.assertIn("[REDACTED:connection_string]", text)
        self.assertNotIn(AWS, text)
        self.assertNotIn("S3cretPass", text)
        self.assertEqual(len(findings), 2)
        res = vault.record_decision(root, None, "Anahtar", f"AWS {AWS} kullanıldı", state_dir=state)
        content = (root / res["path"]).read_text(encoding="utf-8")
        self.assertIn("[REDACTED:api_token]", content)
        self.assertNotIn(AWS, content)

    def test_off_and_invalid_mode(self):
        self.assertEqual(dlp.inspect_and_apply(AWS, "off", None), (AWS, []))
        with self.assertRaises(ValueError):
            dlp.inspect_and_apply("x", "bogus", None)

    def test_unsafe_custom_regex_ignored(self):
        root, _ = self.make_vault("work")
        (root / "dlp-patterns.txt").write_text("regex:(a+)+$\nregex:[unclosed\n", encoding="utf-8")
        self.assertEqual(dlp.load_custom_patterns(root), [])


if __name__ == "__main__":
    unittest.main()
