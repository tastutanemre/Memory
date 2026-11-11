import io
import json
import os
import queue
import subprocess
import sys
import threading
import unittest

from _helpers import MEM_PY, IsolatedCase

from mem import vault
from mem.mcp_server import McpServer, serve

TIMEOUT = 30


class StdioClient:
    def __init__(self, cwd, env):
        self.proc = subprocess.Popen(
            [sys.executable, str(MEM_PY), "mcp", "--profile", "work"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(cwd), env=env,
        )
        self.q: queue.Queue = queue.Queue()
        self.raw_lines: list[bytes] = []
        threading.Thread(target=self._pump, daemon=True).start()
        self._id = 0

    def _pump(self):
        for line in self.proc.stdout:
            self.raw_lines.append(line)
            self.q.put(line)
        self.q.put(None)

    def send(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self._id += 1
            msg["id"] = self._id
        self.proc.stdin.write(json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n")
        self.proc.stdin.flush()
        if notify:
            return None
        line = self.q.get(timeout=TIMEOUT)
        assert line is not None, "sunucu beklenmedik sekilde kapandi"
        reply = json.loads(line.decode("utf-8"))
        assert reply["id"] == self._id
        return reply

    def call(self, name, **args):
        r = self.send("tools/call", {"name": name, "arguments": args})
        return r["result"]

    def close(self):
        try:
            self.proc.stdin.close()
            code = self.proc.wait(timeout=TIMEOUT)
        finally:
            if self.proc.poll() is None:
                self.proc.kill()
            for s in (self.proc.stdout, self.proc.stderr):
                s.close()
        return code


def text_of(result):
    return result["content"][0]["text"]


class McpStdioLifecycleTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.root, self.state = self.make_vault("work")
        self.repo = self.make_repo("svc")
        vault.register_project(self.root, "svc", [self.repo], name="Svc")

    def test_full_lifecycle_over_real_subprocess(self):
        c = StdioClient(self.repo, os.environ.copy())
        try:
            r = c.send("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                      "clientInfo": {"name": "test", "version": "1"}})
            res = r["result"]
            self.assertEqual(res["protocolVersion"], "2025-03-26")
            self.assertEqual(res["serverInfo"], {"name": "mem", "version": "0.1.0"})
            self.assertIn("tools", res["capabilities"])
            self.assertIn("prompts", res["capabilities"])
            c.send("notifications/initialized", notify=True)
            self.assertEqual(c.send("ping")["result"], {})

            tools = c.send("tools/list")["result"]["tools"]
            self.assertEqual({t["name"] for t in tools}, {
                "memory_bootstrap", "memory_search", "memory_read", "memory_decide", "memory_rule",
                "memory_patch", "memory_finish"})
            for t in tools:
                self.assertEqual(t["inputSchema"]["type"], "object")
            prompts = c.send("prompts/list")["result"]["prompts"]
            self.assertEqual({p["name"] for p in prompts}, {"start", "decide", "wrapup"})
            got = c.send("prompts/get", {"name": "start", "arguments": {"task": "cache"}})["result"]
            self.assertIn("memory_bootstrap", got["messages"][0]["content"]["text"])

            boot = c.call("memory_bootstrap", cwd=str(self.repo), task_hint="cache")
            self.assertNotIn("isError", boot)
            self.assertIn("Hafıza bağlamı", text_of(boot))
            # cwd verilmezse sunucunun cwd'si (kayitli repo) kullanilir
            self.assertIn("svc", text_of(c.call("memory_bootstrap")))

            dec = c.call("memory_decide", cwd=str(self.repo), title="Önbellek stratejisi",
                         decision="Redis kullan", rationale="hız")
            self.assertNotIn("isError", dec)
            decided = json.loads(text_of(dec))
            self.assertEqual(decided["status"], "created")
            dup = json.loads(text_of(c.call("memory_decide", cwd=str(self.repo), title="Önbellek stratejisi",
                                            decision="x")))
            self.assertEqual(dup["status"], "duplicate_warning")

            found = json.loads(text_of(c.call("memory_search", query="önbelleği", cwd=str(self.repo))))
            self.assertIn(decided["path"], [x["doc_id"] for x in found["results"]])

            read = json.loads(text_of(c.call("memory_read", path=decided["path"], cwd=str(self.repo))))
            patched = c.call("memory_patch", path=decided["path"], expected_sha256=read["sha256"],
                             edits=[{"old": "Redis kullan", "new": "Valkey kullan"}], cwd=str(self.repo))
            self.assertNotIn("isError", patched)
            stale = c.call("memory_patch", path=decided["path"], expected_sha256=read["sha256"],
                           edits=[{"old": "Valkey", "new": "X"}], cwd=str(self.repo))
            self.assertTrue(stale["isError"])
            self.assertIn("ConcurrencyConflictError", text_of(stale))

            rule = json.loads(text_of(c.call("memory_rule", rule="Testsiz merge yok", cwd=str(self.repo))))
            self.assertEqual(rule["status"], "added")

            fin = json.loads(text_of(c.call("memory_finish", summary="Valkey geçişi tamam", open_threads=["TTL"],
                                            session_id="m1", harness="devin", cwd=str(self.repo))))
            self.assertEqual(fin["status"], "recorded")
            self.assertTrue((self.root / fin["receipt"]).is_file())

            # DLP: work profilinde sir -> isError, sir yanitta yok, dosya yazilmadi
            secret = "AKIAIOSFODNN7EXAMPLE"
            blocked = c.call("memory_rule", rule=f"anahtar {secret}", cwd=str(self.repo))
            self.assertTrue(blocked["isError"])
            self.assertIn("DLPBlockedError", text_of(blocked))
            self.assertNotIn(secret, text_of(blocked))

            # yabanci cwd: sifir sizinti
            foreign = self.make_repo("foreign")
            for tool, args in (("memory_bootstrap", {}), ("memory_search", {"query": "valkey"}),
                               ("memory_decide", {"title": "t", "decision": "d"}),
                               ("memory_finish", {"summary": "s"}), ("memory_rule", {"rule": "r"})):
                out = json.loads(text_of(c.call(tool, cwd=str(foreign), **args)))
                self.assertEqual(out["status"], "unregistered_workspace", tool)
            self.assertEqual(len(list((self.root / "projects/svc/kararlar").glob("*.md"))), 1)

            # hatali istekler sunucuyu cokertmez
            self.assertEqual(c.send("nope/method")["error"]["code"], -32601)
            self.assertEqual(c.send("tools/call", {"name": "yok", "arguments": {}})["error"]["code"], -32602)
            bad = c.call("memory_read", path="../../etc/passwd", cwd=str(self.repo))
            self.assertTrue(bad["isError"])
            self.assertEqual(c.send("ping")["result"], {})
        finally:
            code = c.close()
        self.assertEqual(code, 0)
        # stdout'ta protokol disi tek karakter yok: her satir gecerli JSON-RPC 2.0
        self.assertTrue(c.raw_lines)
        for line in c.raw_lines:
            msg = json.loads(line.decode("utf-8"))
            self.assertEqual(msg["jsonrpc"], "2.0")
            self.assertTrue(line.endswith(b"\n") and not line.endswith(b"\r\n"))


class McpInProcessTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.root, self.state = self.make_vault("personal")
        self.repo = self.make_repo("svc")
        vault.register_project(self.root, "svc", [self.repo])
        self.server = McpServer(self.root, self.state, default_cwd=str(self.repo))

    def run_serve(self, data: bytes) -> bytes:
        out = io.BytesIO()
        serve(self.server, io.BytesIO(data), out)
        return out.getvalue()

    def test_content_length_framing_autodetected(self):
        def frame(msg):
            b = json.dumps(msg).encode("utf-8")
            return b"Content-Length: " + str(len(b)).encode() + b"\r\n\r\n" + b

        data = (frame({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2024-11-05"}})
                + frame({"jsonrpc": "2.0", "method": "notifications/initialized"})
                + frame({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
        out = self.run_serve(data)
        self.assertTrue(out.startswith(b"Content-Length: "))
        parts = out.split(b"Content-Length: ")[1:]
        self.assertEqual(len(parts), 2)  # bildirime yanit yok
        first = json.loads(parts[0].split(b"\r\n\r\n", 1)[1])
        self.assertEqual(first["result"]["protocolVersion"], "2024-11-05")

    def test_parse_error_batch_and_unknown_version(self):
        out = self.run_serve(b"{bozuk json\n")
        self.assertEqual(json.loads(out)["error"]["code"], -32700)
        batch = json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "ping"},
                            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}},
                            {"jsonrpc": "2.0", "id": 2, "method": "initialize",
                             "params": {"protocolVersion": "1999-01-01"}}]).encode() + b"\n"
        replies = json.loads(self.run_serve(batch))
        self.assertEqual(len(replies), 2)
        self.assertEqual(replies[1]["result"]["protocolVersion"], "2025-03-26")

    def test_personal_profile_redacts_via_tools(self):
        res = self.server.call_tool("memory_decide", {"cwd": str(self.repo), "title": "Anahtar",
                                                      "decision": "AKIAIOSFODNN7EXAMPLE kullanıldı"})
        self.assertNotIn("isError", res)
        self.assertEqual(json.loads(res["content"][0]["text"])["dlp_categories"], ["api_token"])

    def test_cross_project_tools_denied(self):
        other = self.make_repo("other")
        vault.register_project(self.root, "other", [other])
        res = self.server.call_tool("memory_rule", {"rule": "x", "scope": "other", "cwd": str(self.repo)})
        self.assertTrue(res["isError"])
        res = self.server.call_tool("memory_read", {"path": "projects/other/Kurallar.md", "cwd": str(self.repo)})
        self.assertTrue(res["isError"])


if __name__ == "__main__":
    unittest.main()
