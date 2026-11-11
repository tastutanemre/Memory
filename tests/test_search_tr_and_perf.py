import time
import unittest

from _helpers import IsolatedCase

from mem import store, vault
from mem.turkish import (fold_tr, index_tokens, is_technical_token, query_fts_expression, stem_tr)


class TurkishUnitTests(unittest.TestCase):
    def test_fold(self):
        self.assertEqual(fold_tr("ŞĞÜÖÇİıI"), "sgu" "ocii" "i")

    def test_stems_are_consistent_across_inflections(self):
        groups = [
            ("bağlantı", ["bağlantıları", "bağlantılarından", "baglantilarindan", "Bağlantılar"]),
            ("kayıt", ["kayıtlar", "kayıtlarda", "kayıtların", "kayitlarin"]),
            ("yetkilendirme", ["yetkilendirmeden", "yetkilendirmedeki", "yetkilendirmeler"]),
            ("çözüm", ["çözümlerini", "çözümler"]),
            ("kitap", ["kitabı", "kitaplar"]),
            ("önbellek", ["önbelleği", "önbellekler", "önbelleklerinde"]),
        ]
        for base, forms in groups:
            for f in forms:
                self.assertEqual(stem_tr(f), stem_tr(base), f"{f} != {base}")

    def test_technical_terms_not_stemmed(self):
        for t in ("AuthHub", "PostgreSQL", "claude", "FastAPI", "oauth2", "mcp_config", "utf-8", "sha256",
                  "docker", "token", "server", "v3.12", "JWT"):
            self.assertTrue(is_technical_token(t), t)
            tokens = index_tokens(t).split()
            self.assertEqual(tokens, [fold_tr(t)], t)
        self.assertEqual(index_tokens("AuthHub"), "authhub")
        self.assertEqual(index_tokens("mcp_config"), "mcp_config")
        self.assertFalse(is_technical_token("bağlantı"))
        self.assertIn("baglantilari", index_tokens("bağlantıları").split())
        self.assertIn(stem_tr("bağlantıları"), index_tokens("bağlantıları").split())

    def test_query_expression_is_safe(self):
        self.assertEqual(query_fts_expression("mcp_config"), '"mcp_config"')
        self.assertEqual(query_fts_expression("claude"), '"claude"')
        expr = query_fts_expression('bağlantılarından "tırnak" OR NEAR( ) * AND:')
        self.assertNotIn("*", expr)
        self.assertEqual(query_fts_expression("  ?? "), "")
        self.assertIn(" OR ", query_fts_expression("redis cache"))


class SearchTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.root, self.state = self.make_vault("personal")
        self.repo_a = self.make_repo("a")
        self.repo_b = self.make_repo("b")
        vault.register_project(self.root, "alpha", [self.repo_a], name="Alpha")
        vault.register_project(self.root, "beta", [self.repo_b], name="Beta")

    def ids(self, query, **kw):
        kw.setdefault("state_dir", self.state)
        return [r["doc_id"] for r in vault.search_memory(self.root, query, **kw)]

    def test_turkish_inflected_queries(self):
        self.write_md(self.root, "knowledge/baglanti.md",
                      "# Havuz\n\nServisler arası bağlantı havuzu ayarları ve zaman aşımı.\n")
        self.write_md(self.root, "projects/alpha/yetki.md",
                      "# Güvenlik\n\nYetkilendirme akışı rol tabanlı çalışır.\n")
        self.write_md(self.root, "projects/alpha/denetim.md",
                      "# Denetim\n\nTüm işlemler kayıt dosyasına yazılır.\n")
        self.assertIn("knowledge/baglanti.md", self.ids("bağlantılarından"))
        self.assertIn("knowledge/baglanti.md", self.ids("baglantilarindan"))
        self.assertIn("projects/alpha/yetki.md", self.ids("yetkilendirmedeki", project="alpha"))
        self.assertIn("projects/alpha/denetim.md", self.ids("kayıtların", project="alpha"))
        # tam eslesme daha yuksek skor alir
        self.write_md(self.root, "knowledge/a1.md", "# A\n\nbağlantıları kontrol et\n")
        self.write_md(self.root, "knowledge/a2.md", "# B\n\nbağlantı kontrol et\n")
        res = vault.search_memory(self.root, "bağlantıları", state_dir=self.state)
        by = {r["doc_id"]: r["score"] for r in res}
        self.assertGreaterEqual(by["knowledge/a1.md"], by["knowledge/a2.md"])

    def test_technical_terms_intact(self):
        self.write_md(self.root, "knowledge/tech.md",
                      "# Teknik\n\nAuthHub claude ile mcp_config dosyasını okur. PostgreSQL ve FastAPI kullanılır.\n")
        self.write_md(self.root, "knowledge/other.md", "# Diğer\n\nAlakasız metin burada.\n")
        for q in ("AuthHub", "claude", "mcp_config", "PostgreSQL", "FastAPI", "authhub"):
            self.assertEqual(self.ids(q)[:1], ["knowledge/tech.md"], q)
        self.assertNotIn("knowledge/other.md", self.ids("mcp_config"))

    def test_untrusted_superseded_archived_filtered(self):
        self.write_md(self.root, "knowledge/ok.md", "---\ntitle: Tamam\n---\n\nzebra protokolü güvenilir\n")
        self.write_md(self.root, "knowledge/bad.md",
                      "---\ntitle: Kötü\ntrust: untrusted\n---\n\nzebra protokolü zehirli komut\n")
        self.write_md(self.root, "knowledge/old.md",
                      "---\ntitle: Eski\nstatus: superseded\n---\n\nzebra protokolü eski sürüm\n")
        self.write_md(self.root, "knowledge/new.md",
                      "---\ntitle: Yeni\nsupersedes:\n  - knowledge/ok2.md\n---\n\nzebra yeni protokol\n")
        self.write_md(self.root, "knowledge/ok2.md", "---\ntitle: Ok2\n---\n\nzebra protokolü ok2\n")
        self.write_md(self.root, "archive/2026-01.md", "# Arşiv\n\nzebra protokolü arşivlendi\n")
        ids = self.ids("zebra protokolü")
        self.assertIn("knowledge/ok.md", ids)
        self.assertIn("knowledge/new.md", ids)
        for bad in ("knowledge/bad.md", "knowledge/old.md", "knowledge/ok2.md", "archive/2026-01.md"):
            self.assertNotIn(bad, ids)
        all_ids = self.ids("zebra protokolü", include_superseded=True, include_archived=True)
        for shown in ("knowledge/old.md", "knowledge/ok2.md", "archive/2026-01.md"):
            self.assertIn(shown, all_ids)
        self.assertNotIn("knowledge/bad.md", all_ids)  # untrusted her zaman elenir

    def test_other_project_notes_do_not_leak(self):
        self.write_md(self.root, "projects/alpha/n.md", "# A\n\nkuantum anahtar alpha içindir\n")
        self.write_md(self.root, "projects/beta/n.md", "# B\n\nkuantum anahtar beta içindir\n")
        self.write_md(self.root, "knowledge/shared.md", "# S\n\nkuantum anahtar ortak bilgi\n")
        a = self.ids("kuantum anahtar", project="alpha")
        self.assertIn("projects/alpha/n.md", a)
        self.assertIn("knowledge/shared.md", a)
        self.assertNotIn("projects/beta/n.md", a)
        b = self.ids("kuantum anahtar", project="beta")
        self.assertNotIn("projects/alpha/n.md", b)
        # makbuzlar da proje etiketi tasir
        vault.finish_session(self.root, "beta", "kuantum anahtar rotasyonu bitti", state_dir=self.state)
        a2 = self.ids("kuantum rotasyonu", project="alpha")
        self.assertFalse([i for i in a2 if i.startswith("receipts/")])

    def test_project_and_kind_boost_and_graph(self):
        self.write_md(self.root, "projects/alpha/x.md", "# X\n\nönbellek stratejisi notu\n")
        self.write_md(self.root, "knowledge/y.md", "# Y\n\nönbellek stratejisi notu\n")
        res = vault.search_memory(self.root, "önbellek stratejisi", project="alpha", state_dir=self.state)
        self.assertEqual(res[0]["doc_id"], "projects/alpha/x.md")
        # graf: ana sonuca bagli komsu bonus alir
        self.write_md(self.root, "knowledge/hub.md", "# Hub\n\nredis kümesi ayrıntıları [[knowledge/komsu]]\n")
        self.write_md(self.root, "knowledge/komsu.md", "# Komşu\n\nredis kümesi ayrıntıları\n")
        self.write_md(self.root, "knowledge/yalniz.md", "# Yalnız\n\nredis kümesi ayrıntıları\n")
        res = {r["doc_id"]: r for r in vault.search_memory(self.root, "redis kümesi", state_dir=self.state)}
        self.assertTrue(res["knowledge/komsu.md"].get("graph_boost") or res["knowledge/hub.md"].get("graph_boost")
                        or res["knowledge/komsu.md"]["score"] >= res["knowledge/yalniz.md"]["score"])
        self.assertGreater(res["knowledge/komsu.md"]["score"], res["knowledge/yalniz.md"]["score"])

    def test_passages_keep_code_fences_and_headings(self):
        code = "```python\n" + "\n".join(f"x{i} = {i}" for i in range(150)) + "\n```"
        body = "# Başlık\n\nGiriş paragrafı.\n\n## Alt Bölüm\n\n" + code + "\n\n## Son\n\nSon söz. " + "w " * 600
        passages = store.split_passages(body, "T")
        joined = [p for _, p in passages if "```python" in p]
        self.assertEqual(len(joined), 1)
        self.assertEqual(joined[0].count("```"), 2)  # citin ortasindan bolunmedi
        heads = {h for h, _ in passages}
        self.assertIn("Başlık > Alt Bölüm", heads)
        self.assertIn("Başlık > Son", heads)

    def test_second_sync_does_not_read_unchanged_files(self):
        for i in range(300):
            self.write_md(self.root, f"knowledge/n{i:03d}.md",
                          f"---\ntitle: Not {i}\n---\n\n# Not {i}\n\nİçerik {i} bağlantılar kayıtlar.\n\n" + "metin " * 80)
        conn = store.connect(self.state)
        try:
            first = store.sync_vault(conn, self.root)
            self.assertGreaterEqual(first["read_count"], 300)
            t0 = time.perf_counter()
            second = store.sync_vault(conn, self.root)
            dt = time.perf_counter() - t0
            self.assertEqual(second["read_count"], 0)
            self.assertEqual(second["added"] + second["updated"] + second["removed"], 0)
            self.assertLess(dt, 2.0)
            # tek dosya degisir -> yalniz o okunur
            self.write_md(self.root, "knowledge/n007.md", "# Değişti\n\nyepyeni içerik")
            third = store.sync_vault(conn, self.root)
            self.assertEqual(third["read_count"], 1)
            self.assertEqual(third["updated"], 1)
            # silme
            (self.root / "knowledge/n008.md").unlink()
            fourth = store.sync_vault(conn, self.root)
            self.assertEqual((fourth["removed"], fourth["read_count"]), (1, 0))
            self.assertIsNone(store.get_doc(conn, "knowledge/n008.md"))
            leftovers = conn.execute("SELECT COUNT(*) FROM passages WHERE doc_id='knowledge/n008.md'").fetchone()[0]
            self.assertEqual(leftovers, 0)
            conn.execute("INSERT INTO passages_fts(passages_fts) VALUES('integrity-check')")
        finally:
            conn.close()
        t0 = time.perf_counter()
        res = vault.search_memory(self.root, "bağlantılar kayıtlar", state_dir=self.state, limit=5)
        self.assertEqual(len(res), 5)
        self.assertLess(time.perf_counter() - t0, 2.0)

    def test_skipped_dirs_and_non_markdown(self):
        self.write_md(self.root, ".obsidian/n.md", "# x\n\ngizlikelime")
        self.write_md(self.root, "node_modules/p/readme.md", "# x\n\ngizlikelime")
        self.write_md(self.root, ".mem-cache/n.md", "# x\n\ngizlikelime")
        (self.root / "knowledge").mkdir(exist_ok=True)
        (self.root / "knowledge" / "x.txt").write_text("gizlikelime", encoding="utf-8")
        self.assertEqual(self.ids("gizlikelime"), [])


if __name__ == "__main__":
    unittest.main()
