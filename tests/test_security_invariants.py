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
                        self.assertIn(a.name.split(".")[0], std | {"mem"}, f"{f.name}: {a.name}")
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    self.assertIn(node.module.split(".")[0], std | {"mem"}, f"{f.name}: {node.module}")


if __name__ == "__main__":
    unittest.main()
