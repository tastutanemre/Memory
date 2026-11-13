"""Opsiyonel Gemini Decisions yan adaptoru: sahte transport ile agsiz, uctan uca testler."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from _helpers import IsolatedCase

from mem import cli, config, harness, mcp_server, search, vault
from mem_advisor import gemini_decisions as gd

SECRET = "api_key = sk-abcdefghijklmnopqrstuvwxyz123456"
FAKE_KEY = "AIzaFAKEKEY-0123456789-notreal"


class FakeTransport:
    """transport(url, headers, body, timeout) -> dict; cagri kaydi tutar."""

    def __init__(self, handler=None, error: Exception | None = None):
        self.handler = handler
        self.error = error
        self.calls: list[tuple[str, dict, dict]] = []

    def __call__(self, url, headers, body, timeout):
        req = json.loads(body.decode("utf-8"))
        self.calls.append((url, dict(headers), req))
        if self.error is not None:
            raise self.error
        return self.handler(req)


def _parsed_input(req: dict) -> dict:
    raw = req["input"]
    return json.loads(raw) if isinstance(raw, str) else raw


def rerank_handler(good: str = "redis", good_score: float = 3.0, other_score: float = 0.2, conf: float = 0.9):
    def handler(req):
        out = {}
        for cid, cand in _parsed_input(req)["candidates"].items():
            text = (cand["title"] + " " + cand["snippet"]).lower()
            out[cid] = {"type": "score", "score": good_score if good in text else other_score, "confidence": conf}
        return {"answers": out}
    return handler


def assess_handler(commitment=("asserted", 0.95), relation=("unrelated", 0.95)):
    def handler(req):
        out = {"commitment": {"type": "choice", "choice": commitment[0], "confidence": commitment[1]}}
        for qid in req["questions"]:
            if qid.startswith("rel_p"):
                out[qid] = {"type": "choice", "choice": relation[0], "confidence": relation[1]}
        return {"answers": out}
    return handler


def make_client(transport) -> gd.GeminiDecisionsClient:
    return gd.GeminiDecisionsClient(FAKE_KEY, transport=transport)


class AdvisorBase(IsolatedCase):
    def personal(self, name: str = "vault"):
        root, state = self.make_vault("personal", name)
        return root, state

    def run_cli(self, *argv, stdin: str | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if stdin is not None:
                with mock.patch.object(sys, "stdin", io.StringIO(stdin)):
                    code = cli.main(list(argv))
            else:
                code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()


class GateAndConfigTests(AdvisorBase):
    def test_can_use_advisor_matrix(self):
        self.assertTrue(config.can_use_advisor("personal", "redact"))
        self.assertTrue(config.can_use_advisor("", "redact"))
        self.assertFalse(config.can_use_advisor("work", "redact"))
        self.assertFalse(config.can_use_advisor(" WORK ", "redact"))
        self.assertFalse(config.can_use_advisor("personal", "block"))

    def test_validate_advisor_mode(self):
        self.assertEqual(config.validate_advisor_mode(None, "work", "block"), "off")
        self.assertEqual(config.validate_advisor_mode("", "work", "block"), "off")
        self.assertEqual(config.validate_advisor_mode("gemini", "personal", "redact"), "gemini")
        with self.assertRaises(ValueError):
            config.validate_advisor_mode("gemini", "work", "block")
        with self.assertRaises(ValueError):
            config.validate_advisor_mode("gemini", "personal", "block")
        with self.assertRaises(ValueError):
            config.validate_advisor_mode("openai", "personal", "redact")

    def test_profile_roundtrip_and_legacy_default(self):
        root = self.tmp / "v"
        config.create_profile("personal", root, advisor="gemini")
        self.assertEqual(config.get_profile("personal").advisor, "gemini")
        config.set_profile_advisor("personal", "off")
        self.assertEqual(config.get_profile("personal").advisor, "off")
        raw = json.loads(config.profiles_path().read_text(encoding="utf-8"))
        self.assertNotIn("advisor", raw["profiles"]["personal"])  # eski bicim korunur
        with self.assertRaises(ValueError):
            config.create_profile("work", self.tmp / "w", advisor="gemini")
        config.create_profile("work", self.tmp / "w")
        with self.assertRaises(ValueError):
            config.set_profile_advisor("work", "gemini")
        self.assertEqual(config.get_profile("work").advisor, "off")

    def test_advisor_config_outside_vault(self):
        root, _ = self.personal()
        self.assertFalse(config.is_within(config.advisor_config_path(), root))
        self.assertEqual(config.advisor_config_path().name, "advisor.json")

    def test_effective_mode_env_and_work(self):
        root, _ = self.personal()
        self.assertEqual(config.effective_advisor_mode(root), "off")
        os.environ["MEM_ADVISOR"] = "gemini"
        self.assertEqual(config.effective_advisor_mode(root), "gemini")
        wroot, _ = self.make_vault("work", "wv")
        self.assertEqual(config.effective_advisor_mode(wroot), "off")  # ortam istegi work'te sessiz kapali
        with self.assertRaises(ValueError):
            config.effective_advisor_mode(wroot, "gemini")
        broot, _ = self.make_vault("personal", "bv", dlp_mode="block")
        self.assertEqual(config.effective_advisor_mode(broot), "off")
        with self.assertRaises(ValueError):
            config.effective_advisor_mode(broot, "gemini")

    def test_vault_meta_records_advisor(self):
        root = self.tmp / "iv"
        prof = config.create_profile("personal", root, advisor="gemini")
        vault.init_vault(root, "personal", dlp_mode=prof.dlp_mode, state_dir=prof.state_dir, advisor="gemini")
        self.assertEqual(config.read_vault_meta(root)["advisor"], "gemini")
        with self.assertRaises(ValueError):
            vault.init_vault(self.tmp / "wv", "work", advisor="gemini")


class EndpointAndClientTests(AdvisorBase):
    def test_endpoint_allowlist(self):
        self.assertEqual(gd._validate_endpoint(gd.DECISIONS_ENDPOINT), gd.DECISIONS_ENDPOINT)
        for bad in (
            "http://generativelanguage.googleapis.com/v1beta/decisions",
            "https://evil.example.com/v1beta/decisions",
            "https://generativelanguage.googleapis.com.evil.com/v1beta/decisions",
            "https://generativelanguage.googleapis.com@evil.com/v1beta/decisions",
            "https://generativelanguage.googleapis.com:8443/v1beta/decisions",
            "https://generativelanguage.googleapis.com/v2/decisions",
            "https://generativelanguage.googleapis.com/v1beta",
            "file:///etc/passwd",
            "",
        ):
            with self.assertRaises(ValueError, msg=bad):
                gd._validate_endpoint(bad)
            with self.assertRaises(ValueError, msg=bad):
                gd.GeminiDecisionsClient(FAKE_KEY, endpoint=bad)

    def test_request_schema_headers_and_repr(self):
        t = FakeTransport(lambda req: {"answers": {"q": {"type": "score", "score": 2.0, "confidence": 0.5}}})
        client = make_client(t)
        answers = client.decide({"query": "x"}, {"q": {"type": "score"}})
        self.assertEqual(answers["q"]["score"], 2.0)
        url, headers, body = t.calls[0]
        self.assertEqual(url, gd.DECISIONS_ENDPOINT)
        self.assertEqual(headers["x-goog-api-key"], FAKE_KEY)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(set(body), {"model", "input", "questions"})
        self.assertEqual(body["model"], "gemini-decision-1")
        self.assertNotIn(FAKE_KEY, repr(client))

    def test_bad_responses_raise_advisor_error_without_key(self):
        for resp in ({}, {"answers": []}):
            with self.assertRaises(gd.AdvisorError):
                make_client(FakeTransport(lambda _r, resp=resp: resp)).decide({}, {})
        with self.assertRaises(gd.AdvisorError) as ctx:
            make_client(FakeTransport(error=TimeoutError("boom"))).decide({}, {})
        self.assertNotIn(FAKE_KEY, str(ctx.exception))

    def test_default_transport_never_leaves_allowlist(self):
        with self.assertRaises(ValueError):
            gd._urllib_transport("https://evil.example.com/v1beta/decisions", {}, b"{}", 1.0)

    def test_api_key_resolution_order_and_status(self):
        self.assertIsNone(gd.resolve_api_key())
        gd.save_advisor_settings(gemini_api_key="file-key-1234567890")
        self.assertEqual(gd.resolve_api_key(), "file-key-1234567890")
        os.environ["GOOGLE_API_KEY"] = "google-key-123456"
        self.assertEqual(gd.resolve_api_key(), "google-key-123456")
        os.environ["GEMINI_API_KEY"] = "gemini-key-123456"
        self.assertEqual(gd.resolve_api_key(), "gemini-key-123456")
        os.environ["MEM_GEMINI_API_KEY"] = "mem-key-1234567"
        self.assertEqual(gd.resolve_api_key(), "mem-key-1234567")
        st = gd.advisor_status("personal", "redact", "gemini")
        self.assertTrue(st["key_configured"])
        self.assertEqual(st["key_source"], "env:MEM_GEMINI_API_KEY")
        blob = json.dumps(st)
        for secret in ("mem-key-1234567", "gemini-key-123456", "google-key-123456", "file-key-1234567890"):
            self.assertNotIn(secret, blob)
        with self.assertRaises(ValueError):
            gd.save_advisor_settings(gemini_api_key="bad key with spaces")
        gd.save_advisor_settings(clear_key=True)
        self.assertNotIn("gemini_api_key", gd.load_advisor_settings())

    def test_build_for_profile_gate(self):
        root, state = self.personal()
        with self.assertRaises(ValueError):
            gd.build_for_profile("gemini", root, state, "work", "block")
        with self.assertRaises(ValueError):
            gd.build_for_profile("gemini", root, state, "personal", "block")
        self.assertEqual(gd.build_for_profile("gemini", root, state, "personal", "redact"), (None, None))  # anahtar yok
        os.environ["GEMINI_API_KEY"] = FAKE_KEY
        rr, adv = gd.build_for_profile("gemini", root, state, "personal", "redact", transport=FakeTransport())
        self.assertTrue(rr.is_external and adv.is_external)
        self.assertEqual(gd.build_for_profile("off", root, state, "personal", "redact"), (None, None))


class SearchPoolTests(AdvisorBase):
    def test_pool_wider_than_limit_and_cut_after_rerank(self):
        root, state = self.personal()
        for i in range(10):
            self.write_md(root, f"knowledge/n{i}.md", f"# Not {i}\n\nortak kelime havuz {i}\n")
        seen = {}

        class Reverse:
            def rerank(self, query, results):
                seen["n"] = len(results)
                return list(reversed(results))

        plain = [r["doc_id"] for r in vault.search_memory(root, "havuz", limit=2, state_dir=state)]
        res = vault.search_memory(root, "havuz", limit=2, state_dir=state, reranker=Reverse())
        self.assertEqual(seen["n"], 10)  # min(len, max(limit*3, 24))
        self.assertEqual(len(res), 2)
        self.assertNotEqual([r["doc_id"] for r in res], plain)

    def test_noop_and_no_reranker_keep_zero_hit_empty(self):
        root, state = self.personal()
        self.write_md(root, "knowledge/cache.md", "# Redis\n\nRedis TTL 300 saniye\n")
        q = "önbellek ne zaman bayatlar"
        self.assertEqual(vault.search_memory(root, q, state_dir=state), [])
        self.assertEqual(vault.search_memory(root, q, state_dir=state, reranker=search.NoOpReranker()), [])

    def test_recovery_pool_only_with_active_reranker_and_scope_filters(self):
        root, state = self.personal()
        self.write_md(root, "knowledge/cache.md", "# Redis\n\nRedis TTL 300 saniye\n")
        self.write_md(root, "knowledge/eski.md",
                      "---\ntitle: Eski\nstatus: superseded\n---\n# Eski\n\nRedis eski ayar\n")
        seen = []

        class Spy:
            def rerank(self, query, results):
                seen.extend(dict(r) for r in results)  # kopya: search_vault sonra ic anahtari temizler
                return results

        vault.search_memory(root, "önbellek ne zaman bayatlar", state_dir=state, reranker=Spy())
        rescued = [r for r in seen if r.get("lexical_hit") is False]
        ids = [r["doc_id"] for r in rescued]
        self.assertIn("knowledge/cache.md", ids)
        self.assertNotIn("knowledge/eski.md", ids)  # superseded kapsam disi
        self.assertLessEqual(len(rescued), 24)
        for r in rescued:
            self.assertEqual(r["score"], 0.05)

    def test_bootstrap_uses_reranker(self):
        root, state = self.personal()
        vault.register_project(root, "alpha", [str(self.make_repo())], state_dir=state)
        calls = []

        class Spy:
            def rerank(self, query, results):
                calls.append(query)
                return results

        vault.bootstrap_context(root, state, project="alpha", task_hint="önbellek", reranker=Spy())
        self.assertEqual(calls, ["önbellek"])


class RerankerTests(AdvisorBase):
    def setUp(self):
        super().setUp()
        self.root, self.state = self.personal()
        self.write_md(self.root, "knowledge/cache.md", "# Redis\n\nRedis TTL 300 saniye\n")
        self.write_md(self.root, "knowledge/diger.md", "# Yemek\n\nMenü tarifleri\n")
        self.q = "önbellek ne zaman bayatlar"

    def reranker(self, transport, client=True):
        c = make_client(transport) if client else None
        return gd.GeminiSemanticReranker(c, gd.AdvisorCache(self.state), self.root)

    def test_zero_keyword_semantic_recovery(self):
        t = FakeTransport(rerank_handler())
        res = vault.search_memory(self.root, self.q, state_dir=self.state, reranker=self.reranker(t))
        self.assertEqual([r["doc_id"] for r in res], ["knowledge/cache.md"])  # alakasiz not elendi
        self.assertGreaterEqual(res[0]["semantic_score"], 0.35)
        self.assertNotIn("lexical_hit", res[0])
        self.assertEqual(len(t.calls), 1)  # tek istek
        body = t.calls[0][2]
        self.assertIsInstance(body["input"], str)
        inp = _parsed_input(body)
        self.assertEqual(set(inp), {"query", "candidates"})
        cand = next(iter(inp["candidates"].values()))
        self.assertEqual(set(cand), {"title", "kind", "heading", "snippet"})
        q = next(iter(body["questions"].values()))
        self.assertEqual(q["type"], "score")
        self.assertEqual(len(q["criteria"]), 4)

    def test_no_key_leaks_nothing(self):
        t = FakeTransport(rerank_handler())
        res = vault.search_memory(self.root, self.q, state_dir=self.state, reranker=self.reranker(t, client=False))
        self.assertEqual(res, [])
        self.assertEqual(t.calls, [])

    def test_low_semantic_rescue_dropped(self):
        t = FakeTransport(rerank_handler(good_score=0.5))
        res = vault.search_memory(self.root, self.q, state_dir=self.state, reranker=self.reranker(t))
        self.assertEqual(res, [])

    def test_lexical_hits_survive_error_but_rescue_does_not(self):
        t = FakeTransport(error=TimeoutError())
        res = vault.search_memory(self.root, "Redis TTL", state_dir=self.state, reranker=self.reranker(t))
        self.assertEqual([r["doc_id"] for r in res], ["knowledge/cache.md"])
        res = vault.search_memory(self.root, self.q, state_dir=self.state, reranker=self.reranker(t))
        self.assertEqual(res, [])

    def test_blend_reorders_lexical_hits(self):
        self.write_md(self.root, "knowledge/a.md", "# A\n\nredis redis redis ayar\n")
        self.write_md(self.root, "knowledge/b.md", "# B\n\nredis ayar değil, TTL kuralı burada yazılı\n")

        def handler(req):
            ans = {}
            for cid, cand in _parsed_input(req)["candidates"].items():
                ans[cid] = {"score": 3.0 if cand["title"] == "B" else 0.0, "confidence": 1.0}
            return {"answers": ans}

        res = vault.search_memory(self.root, "redis", state_dir=self.state,
                                  reranker=self.reranker(FakeTransport(handler)))
        self.assertEqual(res[0]["title"], "B")

    def test_dlp_preflight_blocks_query_and_candidates(self):
        t = FakeTransport(rerank_handler())
        rr = self.reranker(t)
        self.assertEqual(rr.rerank(SECRET, [{"doc_id": "d", "title": "t", "kind": "note", "snippet": "s",
                                             "heading_path": "", "score": 1.0}])[0]["doc_id"], "d")
        self.assertEqual(t.calls, [])
        dirty = {"doc_id": "x", "title": "t", "kind": "note", "snippet": SECRET, "heading_path": "", "score": 1.0}
        rescue = {**dirty, "doc_id": "y", "lexical_hit": False, "score": 0.05}
        clean = {"doc_id": "z", "title": "redis", "kind": "note", "snippet": "ttl", "heading_path": "", "score": 1.0}
        out = rr.rerank("redis", [dirty, rescue, clean])
        sent = _parsed_input(t.calls[0][2])["candidates"]
        self.assertEqual(len(sent), 1)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz123456", json.dumps(t.calls[0][2]))
        self.assertEqual({r["doc_id"] for r in out}, {"x", "z"})  # kirli yerel eslesme korunur, kurtarma elenir

    def test_cache_prevents_second_network_call(self):
        t = FakeTransport(rerank_handler())
        rr = self.reranker(t)
        first = vault.search_memory(self.root, self.q, state_dir=self.state, reranker=rr)
        second = vault.search_memory(self.root, self.q, state_dir=self.state, reranker=rr)
        self.assertEqual(len(t.calls), 1)
        self.assertEqual([r["doc_id"] for r in first], [r["doc_id"] for r in second])
        # yeni nesne, ayni state_dir: kalici SQLite onbellegi
        vault.search_memory(self.root, self.q, state_dir=self.state, reranker=self.reranker(t))
        self.assertEqual(len(t.calls), 1)
        self.assertTrue((self.state / "advisor_cache.sqlite3").exists())

    def test_cache_ttl_and_size_bound(self):
        cache = gd.AdvisorCache(self.state, ttl_sec=3600, max_entries=3)
        for i in range(6):
            cache.put(f"k{i}", {"v": i})
        remaining = [i for i in range(6) if cache.get(f"k{i}") is not None]
        self.assertLessEqual(len(remaining), 3)
        expired = gd.AdvisorCache(self.state, ttl_sec=-1)
        expired.put("old", {"v": 1})
        self.assertIsNone(expired.get("old"))

    def test_debug_line_is_secret_free_and_opt_in(self):
        t = FakeTransport(error=TimeoutError(SECRET))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.reranker(t).rerank("redis", [{"doc_id": "z", "title": "redis", "kind": "note", "snippet": "ttl",
                                               "heading_path": "", "score": 1.0}])
        self.assertEqual(err.getvalue(), "")
        os.environ["MEM_ADVISOR_DEBUG"] = "1"
        with contextlib.redirect_stderr(err):
            self.reranker(t).rerank("redis", [{"doc_id": "z", "title": "redis", "kind": "note", "snippet": "ttl",
                                               "heading_path": "", "score": 1.0}])
        self.assertEqual(len(err.getvalue().strip().splitlines()), 1)
        self.assertNotIn("sk-abc", err.getvalue())
        self.assertNotIn(FAKE_KEY, err.getvalue())


class DecisionAdvisorTests(AdvisorBase):
    def setUp(self):
        super().setUp()
        self.root, self.state = self.personal()
        self.prior = vault.record_decision(self.root, None, "Önbellek TTL", "Redis TTL 300 saniye olacak",
                                           rationale="Veri taze kalmalı", state_dir=self.state)["path"]

    def advisor(self, transport):
        return gd.GeminiDecisionAdvisor(make_client(transport), gd.AdvisorCache(self.state), self.root)

    def decide(self, advisor, **kw):
        args = dict(title="Oturum süresi", decision="Oturumlar 30 dakika sonra kapanır", state_dir=self.state)
        args.update(kw)
        return vault.record_decision(self.root, None, args.pop("title"), args.pop("decision"),
                                     advisor=advisor, **args)

    def kararlar(self):
        return sorted(p.name for p in (self.root / "knowledge" / "kararlar").glob("*.md"))

    def test_contradiction_warns_without_writing(self):
        t = FakeTransport(assess_handler(relation=("contradicts_or_supersedes", 0.9)))
        before = self.kararlar()
        res = self.decide(self.advisor(t), title="Anahtar geçerlilik politikası",
                          decision="Anahtarlar bir saat sonra silinir")
        self.assertEqual(res["status"], "advisor_warning")
        self.assertEqual(res["existing"], self.prior)
        self.assertTrue(res["warnings"] and res["message"])
        self.assertEqual(self.kararlar(), before)
        body = t.calls[0][2]
        self.assertIsInstance(body["input"], str)
        self.assertIn("commitment", body["questions"])
        self.assertIsInstance(body["questions"]["commitment"]["criteria"], dict)
        self.assertNotIn("choices", body["questions"]["commitment"])
        self.assertIn("rel_p0", body["questions"])
        self.assertEqual(body["questions"]["rel_p0"]["type"], "choice")
        self.assertIsInstance(body["questions"]["rel_p0"]["criteria"], dict)

    def test_duplicate_and_tentative_warn(self):
        res = self.decide(self.advisor(FakeTransport(assess_handler(relation=("duplicate", 0.85)))),
                          title="TTL kararı", decision="Önbellekte 5 dakika tut")
        self.assertEqual(res["status"], "advisor_warning")
        res = self.decide(self.advisor(FakeTransport(assess_handler(commitment=("tentative", 0.9)))),
                          title="Belki kuyruk", decision="Belki RabbitMQ kullanırız")
        self.assertEqual(res["status"], "advisor_warning")
        self.assertIsNone(res["existing"])

    def test_low_confidence_and_compatible_pass(self):
        res = self.decide(self.advisor(FakeTransport(assess_handler(commitment=("tentative", 0.5),
                                                                   relation=("duplicate", 0.5)))))
        self.assertEqual(res["status"], "created")
        res = self.decide(self.advisor(FakeTransport(assess_handler(relation=("compatible_refinement", 0.99)))),
                          title="Başka karar", decision="Log seviyesi info olacak")
        self.assertEqual(res["status"], "created")

    def test_supersedes_excludes_prior_and_force_skips_advisor(self):
        t = FakeTransport(assess_handler(relation=("contradicts_or_supersedes", 0.99)))
        res = self.decide(self.advisor(t), title="Yeni TTL", decision="Redis TTL 60 saniye", supersedes=[self.prior])
        self.assertEqual(res["status"], "created")
        self.assertNotIn("rel_p0", t.calls[0][2]["questions"])  # acik supersedes verilen prior sorulmaz
        t2 = FakeTransport(assess_handler(commitment=("tentative", 0.99)))
        res = self.decide(self.advisor(t2), title="Zorla", decision="Belki bir şey", force=True)
        self.assertEqual(res["status"], "created")
        self.assertEqual(t2.calls, [])

    def test_fail_open_on_error_and_garbage(self):
        for t in (FakeTransport(error=TimeoutError()), FakeTransport(lambda _r: {"answers": "x"}),
                  FakeTransport(lambda _r: {"answers": {"commitment": "tentative"}})):
            res = self.decide(self.advisor(t), title=f"K {len(t.calls)} {id(t)}", decision=f"Karar {id(t)}")
            self.assertEqual(res["status"], "created")

        class Boom:
            def assess_decision(self, proposal, priors):
                raise RuntimeError("beklenmedik")

        res = self.decide(Boom(), title="Patlayan", decision="Hiç ilgisiz bir şey")
        self.assertEqual(res["status"], "created")

    def test_cache_and_dlp(self):
        t = FakeTransport(assess_handler(relation=("duplicate", 0.9)))
        adv = self.advisor(t)
        proposal = {"project": None, "title": "T", "decision": "Redis TTL 5 dk", "context": "", "alternatives": "",
                    "rationale": ""}
        priors = [{"doc_id": self.prior, "title": "Önbellek TTL", "decision": "Redis TTL 300", "rationale": ""}]
        self.assertIsNotNone(adv.assess_decision(proposal, priors))
        self.assertIsNotNone(adv.assess_decision(proposal, priors))
        self.assertEqual(len(t.calls), 1)
        t2 = FakeTransport(assess_handler(commitment=("tentative", 1.0)))
        dirty = {**proposal, "context": SECRET}
        self.assertIsNone(self.advisor(t2).assess_decision(dirty, priors))
        dirty_prior = [{**priors[0], "decision": SECRET}]
        self.advisor(t2).assess_decision(proposal, dirty_prior)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz123456", json.dumps([c[2] for c in t2.calls]))
        self.assertNotIn("rel_p0", t2.calls[0][2]["questions"])
        self.assertIsNone(gd.GeminiDecisionAdvisor(None).assess_decision(proposal, priors))

    def test_vault_scans_priors_before_sending(self):
        vault.record_decision(self.root, None, "Gizli", "Kimlik bilgisi saklama", state_dir=self.state,
                              force=True)
        path = self.root / "knowledge" / "kararlar"
        note = next(p for p in path.glob("*gizli*.md"))
        note.write_text(note.read_text(encoding="utf-8").replace(
            "Kimlik bilgisi saklama", SECRET), encoding="utf-8", newline="\n")
        seen = []

        class Spy:
            def assess_decision(self, proposal, priors):
                seen.extend(priors)
                return None

        self.decide(Spy())
        self.assertTrue(seen)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz123456", json.dumps(seen))

    def test_work_and_block_never_call_advisor(self):
        class Fail:
            is_external = True

            def assess_decision(self, proposal, priors):
                raise AssertionError("work/block'ta cagrilmamali")

            def rerank(self, query, results):
                raise AssertionError("work/block'ta cagrilmamali")

        for profile, mode in (("work", None), ("personal", "block")):
            root, state = self.make_vault(profile, f"v-{profile}-{mode}", dlp_mode=mode)
            res = vault.record_decision(root, None, "Karar", "Bir karar", state_dir=state, advisor=Fail())
            self.assertEqual(res["status"], "created")
            self.write_md(root, "knowledge/x.md", "# X\n\nredis ayar\n")
            self.assertTrue(vault.search_memory(root, "redis", state_dir=state, reranker=Fail()))
            vault.bootstrap_context(root, state, task_hint="redis", reranker=Fail())


class CliMcpHarnessTests(AdvisorBase):
    def test_cli_advisor_subcommands(self):
        self.run_cli("init", "--profile", "personal", "--vault", str(self.tmp / "pv"))
        code, out, _ = self.run_cli("advisor", "status", "--profile", "personal", "--json")
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(out)["key_configured"])
        self.assertEqual(self.run_cli("advisor", "enable", "--profile", "personal")[0], 0)
        self.assertEqual(config.get_profile("personal").advisor, "gemini")
        code, out, _ = self.run_cli("profiles", "--json")
        self.assertEqual(json.loads(out)[0]["advisor"], "gemini")
        key = "cli-secret-key-0123456789"
        code, out, err = self.run_cli("advisor", "set-key", "--stdin", stdin=key + "\n")
        self.assertEqual(code, 0)
        self.assertNotIn(key, out + err)
        code, out, _ = self.run_cli("advisor", "status", "--profile", "personal", "--json")
        data = json.loads(out)
        self.assertTrue(data["key_configured"])
        self.assertEqual(data["key_source"], "file:advisor.json")
        self.assertNotIn(key, out)
        os.environ["MY_TMP_KEY"] = "env-secret-key-0123456789"
        self.assertEqual(self.run_cli("advisor", "set-key", "--from-env", "MY_TMP_KEY")[0], 0)
        self.assertEqual(gd.load_advisor_settings()["gemini_api_key"], "env-secret-key-0123456789")
        os.environ.pop("MY_TMP_KEY")
        self.assertEqual(self.run_cli("advisor", "set-key", "--from-env", "MY_TMP_KEY")[0], 2)  # tanimsiz
        self.assertEqual(self.run_cli("advisor", "set-key", "--clear")[0], 0)
        self.assertNotIn("gemini_api_key", gd.load_advisor_settings())
        self.assertEqual(self.run_cli("advisor", "disable", "--profile", "personal")[0], 0)
        self.assertEqual(config.get_profile("personal").advisor, "off")

    def test_cli_rejects_work(self):
        self.run_cli("init", "--profile", "work", "--vault", str(self.tmp / "wv"))
        self.assertEqual(self.run_cli("advisor", "enable", "--profile", "work")[0], 2)
        self.assertEqual(self.run_cli("init", "--profile", "work2", "--vault", str(self.tmp / "w2"),
                                      "--dlp", "block", "--advisor", "gemini")[0], 2)
        repo = self.make_repo()
        self.run_cli("project", "add", "alpha", "--root", str(repo), "--profile", "work")
        code, _, err = self.run_cli("search", "x", "--profile", "work", "--cwd", str(repo), "--advisor", "gemini")
        self.assertEqual(code, 2)
        self.assertIn("work", err)
        self.assertEqual(self.run_cli("connect", "devin", "--profile", "work", "--advisor", "gemini",
                                      "--config-root", str(self.tmp / "cfg"), "--dry-run")[0], 2)
        self.assertEqual(self.run_cli("mcp", "--profile", "work", "--advisor", "gemini")[0], 2)

    def test_cli_decide_prints_advisor_warning_and_search_uses_reranker(self):
        self.run_cli("init", "--profile", "personal", "--vault", str(self.tmp / "pv"))
        repo = self.make_repo()
        self.run_cli("project", "add", "alpha", "--root", str(repo), "--profile", "personal")
        self.run_cli("decide", "--profile", "personal", "--cwd", str(repo), "--title", "Önbellek TTL",
                     "--decision", "Redis TTL 300 saniye")
        t = FakeTransport(assess_handler(relation=("contradicts_or_supersedes", 0.9)))
        root = Path(config.get_profile("personal").vault_root)
        state = Path(config.get_profile("personal").state_dir)
        hooks = (gd.GeminiSemanticReranker(make_client(t), None, root),
                 gd.GeminiDecisionAdvisor(make_client(t), gd.AdvisorCache(state), root))
        with mock.patch.object(cli, "_advisor_hooks", return_value=hooks):
            code, out, _ = self.run_cli("decide", "--profile", "personal", "--cwd", str(repo), "--title",
                                        "Geçerlilik politikası", "--decision", "Anahtarlar bir saat sonra silinir")
        self.assertEqual(code, 0)
        self.assertIn("DANIŞMAN UYARISI", out)
        t.handler = rerank_handler()
        with mock.patch.object(cli, "_advisor_hooks", return_value=hooks):
            code, out, _ = self.run_cli("search", "önbellek", "bayatlar", "--profile", "personal", "--cwd", str(repo))
        self.assertEqual(code, 0)
        self.assertIn("Önbellek TTL", out)

    def test_cli_env_gemini_without_key_is_noop(self):
        self.run_cli("init", "--profile", "personal", "--vault", str(self.tmp / "pv"))
        repo = self.make_repo()
        self.run_cli("project", "add", "alpha", "--root", str(repo), "--profile", "personal")
        _, base, _ = self.run_cli("search", "yok", "--profile", "personal", "--cwd", str(repo))
        os.environ["MEM_ADVISOR"] = "gemini"
        code, out, _ = self.run_cli("search", "yok", "--profile", "personal", "--cwd", str(repo))
        self.assertEqual((code, out), (0, base))  # anahtar yok: davranis degismez

    def test_mcp_wiring_and_decide(self):
        root, state = self.personal()
        repo = self.make_repo()
        vault.register_project(root, "alpha", [str(repo)], state_dir=state)
        srv = mcp_server.McpServer(root, state, default_cwd=str(repo))
        self.assertIsNone(srv.reranker)
        self.assertIsNone(srv.advisor)
        os.environ["MEM_ADVISOR"] = "gemini"
        os.environ["GEMINI_API_KEY"] = FAKE_KEY
        srv = mcp_server.McpServer(root, state, default_cwd=str(repo))
        self.assertTrue(srv.reranker.is_external and srv.advisor.is_external)
        t = FakeTransport(assess_handler(relation=("duplicate", 0.9)))
        srv.advisor = gd.GeminiDecisionAdvisor(make_client(t), None, root)
        srv.call_tool("memory_decide", {"title": "Önbellek TTL", "decision": "Redis TTL 300", "cwd": str(repo)})
        res = srv.call_tool("memory_decide", {"title": "Süre", "decision": "TTL beş dakika", "cwd": str(repo)})
        self.assertIn("advisor_warning", res["content"][0]["text"])
        t.handler = rerank_handler()
        srv.reranker = gd.GeminiSemanticReranker(make_client(t), None, root)
        res = srv.call_tool("memory_search", {"query": "önbellek bayatlar", "cwd": str(repo)})
        self.assertIn("Önbellek TTL", res["content"][0]["text"])
        res = srv.call_tool("memory_bootstrap", {"cwd": str(repo), "task_hint": "önbellek bayatlar"})
        self.assertFalse(res.get("isError"))

    def test_mcp_work_always_off(self):
        wroot, wstate = self.make_vault("work", "wv")
        os.environ["MEM_ADVISOR"] = "gemini"
        os.environ["GEMINI_API_KEY"] = FAKE_KEY
        srv = mcp_server.McpServer(wroot, wstate)
        self.assertIsNone(srv.reranker)
        self.assertIsNone(srv.advisor)
        with self.assertRaises(ValueError):
            mcp_server.McpServer(wroot, wstate, advisor_mode="gemini")

    def test_harness_advisor_arg(self):
        default = harness._mcp_entry("personal", Path("mem.py"), "python")
        self.assertNotIn("--advisor", default["args"])
        with_adv = harness._mcp_entry("personal", Path("mem.py"), "python", advisor="gemini")
        self.assertEqual(with_adv["args"][-2:], ["--advisor", "gemini"])
        cfg = self.tmp / "cfg"
        res = harness.connect_harness("devin", "personal", config_root_override=cfg, advisor="gemini")
        self.assertEqual(res["mcp_entry"]["args"][-2:], ["--advisor", "gemini"])
        res = harness.connect_harness("devin", "personal", config_root_override=cfg)
        self.assertNotIn("--advisor", res["mcp_entry"]["args"])
        with self.assertRaises(ValueError):
            harness.connect_harness("devin", "work", config_root_override=cfg, advisor="gemini")
        with self.assertRaises(ValueError):
            harness.connect_harness("devin", "WORK", config_root_override=cfg, advisor="gemini")


if __name__ == "__main__":
    unittest.main()
