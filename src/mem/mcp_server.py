"""Saf Python JSON-RPC 2.0 stdio MCP sunucusu (tools + prompts). stdout yalniz protokol mesajlari icindir."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from . import SERVER_NAME, __version__, config, vault
from .dlp import DLPBlockedError
from .lock import ConcurrencyConflictError, PathTraversalError

SUPPORTED_PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18")
DEFAULT_PROTOCOL = "2025-03-26"

_STR = {"type": "string"}


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


_SCOPE_PROPS = {
    "cwd": {"type": "string", "description": "AKTİF çalışma alanı (repo) yolu; her çağrıda gönder. "
                                             "Hafıza beyaz listesi bu yolla denetlenir."},
    "project": {"type": "string", "description": "İsteğe bağlı proje slug doğrulaması (cwd ile birlikte; "
                                                 "tek başına kapsam açmaz)."},
}
_STR_LIST = {"type": "array", "items": _STR}

TOOLS = [
    {
        "name": "memory_bootstrap",
        "description": "Oturum başında çağır: Core, genel/proje kuralları, açık konular, son oturum kartı ve (task_hint ile) ilgili notları döndürür. "
                       "Çalışma alanı beyaz listede değilse status=unregistered_workspace döner; o durumda hafızaya hiçbir şey yazma.",
        "inputSchema": _obj({**_SCOPE_PROPS, "task_hint": _STR,
                             "budget_chars": {"type": "integer", "minimum": 500, "maximum": 50000}}, ["cwd"]),
    },
    {
        "name": "memory_search",
        "description": "Hafızada arar (Türkçe ek/kök duyarlı FTS5 BM25 + WikiLink grafı). Superseded/untrusted/arşiv notlar elenir.",
        "inputSchema": _obj({"query": _STR, **_SCOPE_PROPS, "limit": {"type": "integer", "minimum": 1, "maximum": 30},
                             "include_superseded": {"type": "boolean"}, "include_archived": {"type": "boolean"}},
                            ["query"]),
    },
    {
        "name": "memory_read",
        "description": "Bir notu okur; yanıt memory_patch için gereken sha256'yı içerir.",
        "inputSchema": _obj({"path": _STR, **_SCOPE_PROPS}, ["path"]),
    },
    {
        "name": "memory_decide",
        "description": "Mimari/ürün kararını ADR olarak kaydeder. Mükerrerse duplicate_warning döner (force=true ile zorlanır). "
                       "Eski kararı geçersiz kılmak için supersedes ver.",
        "inputSchema": _obj({"title": _STR, "decision": _STR, "context": _STR, "alternatives": _STR, "rationale": _STR,
                             "supersedes": _STR_LIST, "sources": _STR_LIST, "tags": _STR_LIST,
                             "force": {"type": "boolean"},
                             "shared": {"type": "boolean", "description": "true: karar projeye değil knowledge/kararlar/ altına yazılır"},
                             **_SCOPE_PROPS}, ["title", "decision"]),
    },
    {
        "name": "memory_rule",
        "description": "Kullanıcı düzeltmesinden çıkan kalıcı kuralı ekler (scope=global veya proje).",
        "inputSchema": _obj({"rule": _STR, "reason": _STR, "scope": {"type": "string", "description": "'global' veya proje slug'ı"},
                             **_SCOPE_PROPS}, ["rule"]),
    },
    {
        "name": "memory_patch",
        "description": "Notu sha256 kilidiyle yamalar; her 'old' metni dosyada tam 1 kez geçmeli.",
        "inputSchema": _obj({"path": _STR, "expected_sha256": _STR,
                             "edits": {"type": "array", "items": _obj({"old": _STR, "new": _STR}, ["old", "new"])},
                             **_SCOPE_PROPS}, ["path", "expected_sha256", "edits"]),
    },
    {
        "name": "memory_finish",
        "description": "Anlamlı iş bitince çağır: iş makbuzu yazar, Son-Oturum kartını ve (verilirse) açık konuları günceller.",
        "inputSchema": _obj({"summary": _STR, "open_threads": _STR_LIST, "refs": _STR_LIST, "session_id": _STR,
                             "harness": _STR, **_SCOPE_PROPS}, ["summary"]),
    },
]

PROMPTS = {
    "start": {
        "description": "Oturumu hafıza bağlamıyla başlat (memory_bootstrap).",
        "arguments": [{"name": "task", "description": "Yapılacak iş (opsiyonel)", "required": False}],
        "text": "memory_bootstrap aracını çağır (cwd = aktif çalışma alanı yolu{task}) ve dönen kuralları bu oturum boyunca uygula. "
                "Sonraki her memory_* çağrısında da aynı cwd'yi gönder. "
                "status=unregistered_workspace dönerse hafızaya hiçbir şey yazma.",
    },
    "decide": {
        "description": "Verilen kararı ADR olarak kaydet (memory_decide).",
        "arguments": [{"name": "title", "description": "Karar başlığı", "required": False}],
        "text": "Bu oturumda alınan mimari/ürün kararını memory_decide ile kaydet{title}. Önce memory_search ile mükerrer olup olmadığına bak; "
                "eski bir kararın yerine geçiyorsa supersedes ver. Sır/anahtar/kişisel veri yazma. Aktif çalışma alanı yolunu cwd olarak gönder.",
    },
    "wrapup": {
        "description": "Oturumu kapat: makbuz ve devir kartı (memory_finish).",
        "arguments": [{"name": "summary", "description": "Kısa özet (opsiyonel)", "required": False}],
        "text": "memory_finish ile bu oturumun özetini, açık kalan konuları (open_threads) ve kaynakları (refs) kaydet{summary}. "
                "Önemsiz ayrıntıları yazma. Aktif çalışma alanı yolunu cwd olarak gönder.",
    },
}


def _text(payload, is_error: bool = False) -> dict:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)
    out = {"content": [{"type": "text", "text": text}]}
    if is_error:
        out["isError"] = True
    return out


class McpServer:
    def __init__(self, vault_root, state_dir=None, default_cwd: str | None = None,
                 vault_scope: bool = False):
        self.vault_root = Path(vault_root)
        self.state_dir = state_dir
        self.vault_scope = vault_scope
        self.default_cwd = str(self.vault_root) if vault_scope else (
            default_cwd if default_cwd is not None else os.getcwd()
        )
        # Son BASARILI memory_bootstrap(cwd=...) calisma alani; IDE sunucuyu kayitsiz bir dizinde
        # (ör. %USERPROFILE%) baslattiginda cwd'siz sonraki cagrilar icin guvenli yedek.
        self._bootstrapped_cwd: str | None = None

    # --- kapsam
    def _effective_cwd(self, args: dict) -> str:
        """Acik cwd her zaman denetlenir. Yoksa: kayitli sunucu cwd'si > bootstrap edilmis cwd > acik hata."""
        cwd = args.get("cwd")
        if cwd:
            if not isinstance(cwd, str):
                raise ValueError("cwd string olmalı")
            return cwd
        if self.default_cwd and config.resolve_project(self.vault_root, self.default_cwd) is not None:
            return self.default_cwd
        if self._bootstrapped_cwd:
            return self._bootstrapped_cwd
        raise ValueError(
            "cwd gerekli: sunucu kayıtlı bir proje dizininde başlatılmadı; "
            "çalışma alanı yolunu 'cwd' parametresiyle gönderin."
        )

    def _scope(self, args: dict) -> dict:
        project = args.get("project") or None
        if project is not None and not isinstance(project, str):
            raise ValueError("project string olmalı")
        return vault.resolve_scope(self.vault_root, cwd=self._effective_cwd(args), project=project)

    def _refuse(self, scope: dict) -> dict:
        return _text({"status": scope["status"], "cwd": scope.get("cwd"), "message": scope["message"] +
                      " Hafızaya hiçbir şey yazma/okuma."})

    # --- araçlar
    def call_tool(self, name: str, args: dict) -> dict:
        try:
            return self._call_tool(name, args)
        except DLPBlockedError as exc:
            return _text(f"DLPBlockedError: {exc}", True)
        except ConcurrencyConflictError as exc:
            return _text(f"ConcurrencyConflictError: {exc} (güncel sha256: {exc.current_sha256})", True)
        except PathTraversalError as exc:
            return _text(f"PathTraversalError: {exc}", True)
        except (vault.VaultError, ValueError, TimeoutError, OSError, TypeError, AttributeError, KeyError) as exc:
            return _text(f"{type(exc).__name__}: {exc}", True)

    def _call_tool(self, name: str, a: dict) -> dict:
        sd = self.state_dir
        vr = self.vault_root
        for key in ("context", "alternatives", "rationale", "reason", "task_hint", "session_id", "harness",
                    "scope", "query", "path", "expected_sha256", "title", "decision", "rule", "summary"):
            if a.get(key) is not None and not isinstance(a[key], str):
                raise ValueError(f"{key} string olmalı")
        for key in ("supersedes", "sources", "tags", "open_threads", "refs"):
            _check_str_list(a, key)
        if name == "memory_bootstrap":
            scope = self._scope(a)
            if a.get("cwd"):  # oturum calisma alanini hatirla / kayitsiz repoya gecilince sifirla
                self._bootstrapped_cwd = a["cwd"] if scope["status"] == "ok" else None
            if scope["status"] != "ok":
                return self._refuse(scope)
            res = vault.bootstrap_context(vr, sd, project=scope["project"], task_hint=a.get("task_hint"),
                                          budget_chars=int(a.get("budget_chars") or 8000))
            return _text(res["text"])
        shared = name == "memory_decide" and (a.get("shared") is True or a.get("project") == "shared")
        scope = self._scope({**a, "project": None if shared else a.get("project")})
        if scope["status"] != "ok":
            return self._refuse(scope)
        slug = scope["project"]
        if name == "memory_search":
            q = a.get("query")
            if not isinstance(q, str) or not q.strip():
                raise ValueError("query zorunlu")
            res = vault.search_memory(vr, q, project=slug, limit=int(a.get("limit") or 8),
                                      include_superseded=bool(a.get("include_superseded")),
                                      include_archived=bool(a.get("include_archived")), state_dir=sd)
            return _text({"status": "ok", "count": len(res), "results": res})
        if name == "memory_read":
            return _text(vault.read_note(vr, _req(a, "path"), project=slug, state_dir=sd))
        if name == "memory_decide":
            return _text(vault.record_decision(
                vr, None if shared else slug, _req(a, "title"), _req(a, "decision"), context=a.get("context") or "",
                alternatives=a.get("alternatives") or "", rationale=a.get("rationale") or "",
                supersedes=a.get("supersedes"), sources=a.get("sources"), tags=a.get("tags"),
                force=bool(a.get("force")), state_dir=sd, actor="mcp"))
        if name == "memory_rule":
            rscope = a.get("scope") or slug or "global"
            if scope["scope"] == "project" and rscope not in ("global", slug):
                raise ValueError(f"Bu çalışma alanı '{slug}' projesine ait; '{rscope}' kapsamına kural yazılamaz.")
            return _text(vault.add_rule(vr, rscope, _req(a, "rule"), a.get("reason") or "", state_dir=sd, actor="mcp"))
        if name == "memory_patch":
            edits = a.get("edits")
            if not isinstance(edits, list):
                raise ValueError("edits bir liste olmalı")
            return _text(vault.patch_note(vr, _req(a, "path"), _req(a, "expected_sha256"), edits,
                                          state_dir=sd, project=slug, actor="mcp"))
        if name == "memory_finish":
            target = slug if slug else str(vr)
            return _text(vault.finish_session(
                vr, target, _req(a, "summary"), open_threads=a.get("open_threads"), refs=a.get("refs"),
                session_id=a.get("session_id"), harness=a.get("harness") or "mcp", state_dir=sd))
        raise KeyError(name)

    # --- JSON-RPC
    def handle(self, msg) -> dict | None:
        if isinstance(msg, dict) and "method" not in msg and ("result" in msg or "error" in msg):
            return None  # istemcinin yaniti: yoksay
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            mid = msg.get("id") if isinstance(msg, dict) and isinstance(msg.get("id"), (int, str)) else None
            return _err(mid, -32600, "Invalid Request")
        method = msg["method"]
        mid = msg.get("id")
        is_note = "id" not in msg
        params = msg.get("params")
        if params is None:
            params = {}
        try:
            if not isinstance(params, dict):
                raise _RpcError(-32602, "Invalid params: params bir nesne olmalı")
            result = self._dispatch(method, params)
        except _RpcError as exc:
            return None if is_note else _err(mid, exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001 - sunucu cokmemeli
            print(f"mem mcp ic hata: {type(exc).__name__}: {exc}", file=sys.stderr)
            return None if is_note else _err(mid, -32603, f"Internal error: {type(exc).__name__}")
        if is_note:
            return None
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def _dispatch(self, method: str, params: dict):
        if method == "initialize":
            req = params.get("protocolVersion")
            return {
                "protocolVersion": req if req in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL,
                "capabilities": {"tools": {}, "prompts": {}},
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
            }
        if method in ("notifications/initialized", "notifications/cancelled", "initialized"):
            return None
        if method == "ping":
            return {}
        if method == "tools/list":
            if not self.vault_scope:
                return {"tools": TOOLS}
            tools = []
            for t in TOOLS:
                if t["name"] == "memory_bootstrap":
                    schema = {**t["inputSchema"], "required": []}
                    tools.append({**t, "inputSchema": schema})
                else:
                    tools.append(t)
            return {"tools": tools}
        if method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str) or name not in {t["name"] for t in TOOLS}:
                raise _RpcError(-32602, f"Unknown tool: {name}")
            args = params.get("arguments")
            if args is None:
                args = {}
            if not isinstance(args, dict):
                raise _RpcError(-32602, "Invalid params: arguments bir nesne olmalı")
            return self.call_tool(name, args)
        if method == "prompts/list":
            return {"prompts": [{"name": n, "description": p["description"], "arguments": p["arguments"]}
                                for n, p in PROMPTS.items()]}
        if method == "prompts/get":
            name = params.get("name")
            if not isinstance(name, str) or name not in PROMPTS:
                raise _RpcError(-32602, f"Unknown prompt: {name}")
            args = params.get("arguments")
            if args is None:
                args = {}
            if not isinstance(args, dict):
                raise _RpcError(-32602, "Invalid params: arguments bir nesne olmalı")
            p = PROMPTS[name]
            fill = {"task": "", "title": "", "summary": ""}
            if name == "start" and args.get("task"):
                fill["task"] = f"; task_hint: {args['task']}"
            if name == "decide" and args.get("title"):
                fill["title"] = f" (başlık: {args['title']})"
            if name == "wrapup" and args.get("summary"):
                fill["summary"] = f" (özet taslağı: {args['summary']})"
            text = p["text"].format(**fill)
            return {"description": p["description"],
                    "messages": [{"role": "user", "content": {"type": "text", "text": text}}]}
        raise _RpcError(-32601, f"Method not found: {method}")


class _RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _req(a: dict, key: str) -> str:
    v = a.get(key)
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"{key} zorunlu")
    return v


def _check_str_list(a: dict, key: str) -> None:
    v = a.get(key)
    if v is None:
        return
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ValueError(f"{key} string listesi olmalı")


def _err(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


# ---------------------------------------------------------------- stdio dongusu


def _read_message(inp, state: dict):
    """(mesaj_baytlari | None=EOF). Ilk mesaj Content-Length ile baslarsa LSP moduna gecer."""
    while True:
        line = inp.readline()
        if not line:
            return None
        stripped = line.strip()
        if not stripped:
            continue
        if state["mode"] is None:
            state["mode"] = "lsp" if stripped.lower().startswith(b"content-length:") else "line"
        if state["mode"] == "line":
            return stripped
        length = None
        header = stripped
        while True:
            if header.lower().startswith(b"content-length:"):
                try:
                    length = int(header.split(b":", 1)[1].strip())
                except ValueError:
                    length = None
            header = inp.readline()
            if not header:
                return None
            header = header.strip()
            if not header:
                break
        if length is None:
            continue
        data = inp.read(length)
        if len(data) < length:
            return None
        return data


def _write_message(out, msg: dict, mode: str) -> None:
    data = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if mode == "lsp":
        out.write(b"Content-Length: " + str(len(data)).encode("ascii") + b"\r\n\r\n" + data)
    else:
        out.write(data + b"\n")
    out.flush()


def serve(server: McpServer, inp, out) -> None:
    """Bayt akislariyla (binary) calisir; test icin BytesIO verilebilir."""
    state = {"mode": None}
    while True:
        raw = _read_message(inp, state)
        if raw is None:
            return
        mode = state["mode"] or "line"
        try:
            msg = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            _write_message(out, _err(None, -32700, "Parse error"), mode)
            continue
        if isinstance(msg, list):
            if not msg:
                _write_message(out, _err(None, -32600, "Invalid Request: boş batch"), mode)
                continue
            replies = [r for r in (server.handle(m) for m in msg) if r is not None]
            if replies:
                _write_message(out, replies, mode)  # type: ignore[arg-type]
            continue
        reply = server.handle(msg)
        if reply is not None:
            _write_message(out, reply, mode)


def run_stdio(vault_root, state_dir=None, vault_scope: bool = False) -> int:
    """Gercek stdio: stdout ikili ve yalniz protokol; sapan print'ler stderr'e yonlendirilir."""
    out = sys.stdout.buffer
    inp = sys.stdin.buffer
    sys.stdout = sys.stderr
    server = McpServer(vault_root, state_dir, vault_scope=vault_scope)
    try:
        serve(server, inp, out)
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    return 0


def main_for_profile(profile_name: str | None = None, vault_root=None, state_dir=None,
                     vault_scope: bool = False) -> int:
    if vault_root:
        root = Path(vault_root)
        sdir = config.resolve_state_dir(root, state_dir)
    else:
        prof = config.get_profile(profile_name)
        root, sdir = Path(prof.vault_root), Path(prof.state_dir)
    return run_stdio(root, sdir, vault_scope=vault_scope)
