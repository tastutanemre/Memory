import ast
import re
import unittest

from _helpers import ROOT, SRC

FORBIDDEN_IMPORTS = {
    "urllib", "http", "socket", "ssl", "ftplib", "smtplib", "poplib", "imaplib", "telnetlib",
    "xmlrpc", "webbrowser", "socketserver", "requests", "subprocess", "ctypes", "base64", "winreg",
    "pickle", "asyncio",
}
FORBIDDEN_WORDS = [
    "powershell", "pwsh", "encodedcommand", "frombase64string", "schtasks", "registerhotkey",
    "startup", "detached_process",
]


def _py_files():
    files = sorted(SRC.joinpath("mem").glob("*.py")) + [ROOT / "mem.py"]
    assert len(files) >= 13
    return files


class SecurityInvariants(unittest.TestCase):
    def test_no_forbidden_imports(self):
        for f in _py_files():
            tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    mods = [node.module]
                for m in mods:
                    self.assertNotIn(m.split(".")[0], FORBIDDEN_IMPORTS, f"{f.name} yasak modul: {m}")

    def test_no_dynamic_import_or_exec(self):
        for f in _py_files():
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    fn = node.func
                    name = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else "")
                    self.assertNotIn(name, {"eval", "exec", "__import__", "system", "popen", "Popen"},
                                     f"{f.name}: yasak cagri {name}")

    def test_no_forbidden_words(self):
        for f in _py_files():
            text = f.read_text(encoding="utf-8").lower()
            for w in FORBIDDEN_WORDS:
                self.assertNotIn(w, text, f"{f.name} yasak ifade iceriyor: {w}")

    def test_utf8_explicit_in_text_io(self):
        """open()/read_text()/write_text() cagrilari acik encoding vermeli (Windows cp1254 tuzagi)."""
        for f in _py_files():
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                if isinstance(fn, ast.Attribute) and fn.attr in ("read_text", "write_text"):
                    self.assertIn("encoding", {k.arg for k in node.keywords}, f"{f.name}:{node.lineno} {fn.attr}")
                if isinstance(fn, ast.Name) and fn.id == "open":
                    mode = ""
                    if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                        mode = str(node.args[1].value)
                    for k in node.keywords:
                        if k.arg == "mode" and isinstance(k.value, ast.Constant):
                            mode = str(k.value.value)
                    if "b" not in mode:
                        self.assertIn("encoding", {k.arg for k in node.keywords}, f"{f.name}:{node.lineno} open")

    def test_mcp_server_never_prints_to_stdout(self):
        tree = ast.parse((SRC / "mem" / "mcp_server.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
                self.assertIn("file", {k.arg for k in node.keywords}, f"print satiri {node.lineno} stderr'e gitmeli")

    def test_no_third_party_imports(self):
        import sys
        std = set(sys.stdlib_module_names)
        for f in _py_files():
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for a in node.names:
                        self.assertIn(a.name.split(".")[0], std | {"mem", "mem_advisor"}, f"{f.name}: {a.name}")
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    self.assertIn(node.module.split(".")[0], std | {"mem", "mem_advisor"}, f"{f.name}: {node.module}")

    def test_core_modules_never_import_advisor(self):
        """Yalniz cli.py ve mcp_server.py yan paketi (mem_advisor) import edebilir; cekirdek sifir-ag kalir."""
        allowed = {"cli.py", "mcp_server.py"}
        for f in _py_files():
            if f.name in allowed:
                continue
            text = f.read_text(encoding="utf-8")
            self.assertNotIn("mem_advisor", text, f"{f.name} mem_advisor'a basvuramaz")


def _advisor_files():
    files = sorted(SRC.joinpath("mem_advisor").glob("*.py"))
    assert len(files) >= 2
    return files


class AdvisorPackageInvariants(unittest.TestCase):
    """Yan paket: ag yalniz burada; ama ayni sertlik kurallari (utf-8, exec yok, stdout yok, 3. parti yok)."""

    def test_network_client_only_in_advisor_package(self):
        self.assertTrue(any("urllib.request" in f.read_text(encoding="utf-8") for f in _advisor_files()))
        for f in _py_files():
            self.assertNotIn("urllib", f.read_text(encoding="utf-8"), f.name)

    def test_no_dangerous_calls_or_modules(self):
        banned_mods = {"subprocess", "ctypes", "winreg", "pickle", "socket", "ssl", "http", "asyncio", "base64"}
        banned_calls = {"eval", "exec", "__import__", "system", "popen", "Popen"}
        for f in _advisor_files():
            tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for a in node.names:
                        self.assertNotIn(a.name.split(".")[0], banned_mods, f"{f.name}: {a.name}")
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    self.assertNotIn(node.module.split(".")[0], banned_mods, f"{f.name}: {node.module}")
                elif isinstance(node, ast.Call):
                    fn = node.func
                    name = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else "")
                    self.assertNotIn(name, banned_calls, f"{f.name}: yasak cagri {name}")

    def test_utf8_explicit_and_no_stdout_print(self):
        for f in _advisor_files():
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                kws = {k.arg for k in node.keywords}
                if isinstance(fn, ast.Attribute) and fn.attr in ("read_text", "write_text"):
                    self.assertIn("encoding", kws, f"{f.name}:{node.lineno}")
                if isinstance(fn, ast.Name) and fn.id == "print":
                    self.assertIn("file", kws, f"{f.name}:{node.lineno} print stderr'e gitmeli")

    def test_no_third_party_imports(self):
        import sys
        std = set(sys.stdlib_module_names)
        for f in _advisor_files():
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for a in node.names:
                        self.assertIn(a.name.split(".")[0], std | {"mem", "mem_advisor"}, f"{f.name}: {a.name}")
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    self.assertIn(node.module.split(".")[0], std | {"mem", "mem_advisor"}, f"{f.name}: {node.module}")


if __name__ == "__main__":
    unittest.main()
