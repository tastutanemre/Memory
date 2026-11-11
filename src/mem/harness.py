"""Devin Desktop, Antigravity, Claude ve Codex icin seffaf MCP + global kural blogu baglayicisi."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .lock import atomic_write_text

BLOCK_START = "<!-- mem-bridge:start -->"
BLOCK_END = "<!-- mem-bridge:end -->"
TOML_START = "# mem-bridge:start"
TOML_END = "# mem-bridge:end"
HARNESSES = ("devin", "antigravity", "claude", "codex")

RULE_BLOCK = f"""{BLOCK_START}
## Hafıza (mem) köprüsü

`mem` MCP sunucusu bağlıysa şu kurallara uy:

- Oturum başında `memory_bootstrap` çağır (cwd olarak aktif çalışma alanı yolunu ver) ve dönen kuralları uygula.
- Her `memory_*` çağrısında aktif çalışma alanı yolunu `cwd` parametresiyle gönder (sunucu IDE'nin başlattığı dizinde çalışır, repo dizininde değil).
- Mimari/ürün kararı verildiğinde `memory_decide` ile kaydet; eski bir kararın yerine geçiyorsa `supersedes` ver.
- Kullanıcı seni düzelttiğinde kalıcı kuralı `memory_rule` ile kaydet.
- Anlamlı bir iş bitince `memory_finish` ile özet, açık konular ve kaynakları yaz.
- `memory_bootstrap` `status: unregistered_workspace` dönerse bu repo hafıza beyaz listesinde değildir:
  hafızaya HİÇBİR ŞEY yazma ve hafızadan hiçbir bilgi aktarma.
- Parola, token, anahtar, bağlantı dizesi ve kişisel veriyi hafızaya asla yazma.
{BLOCK_END}
"""


# ---------------------------------------------------------------- yollar


def _home(override) -> Path:
    return Path(override) if override else Path.home()


def _devin_dir(override) -> Path:
    if override:
        return Path(override) / "devin"
    if os.name == "nt":
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(appdata) / "devin"
    return Path.home() / ".config" / "devin"


def default_mem_py() -> Path:
    return Path(__file__).resolve().parents[2] / "mem.py"


def _targets(harness: str, override) -> dict:
    home = _home(override)
    if harness == "devin":
        d = _devin_dir(override)
        return {"mcp": d / "mcp_config.json", "mcp_kind": "json", "rules": d / "AGENTS.md"}
    if harness == "antigravity":
        return {"mcp": home / ".gemini" / "config" / "mcp_config.json", "mcp_kind": "json",
                "rules": home / ".gemini" / "config" / "GEMINI.md"}
    if harness == "claude":
        return {"mcp": home / ".claude.json", "mcp_kind": "json", "rules": home / ".claude" / "CLAUDE.md"}
    if harness == "codex":
        return {"mcp": home / ".codex" / "config.toml", "mcp_kind": "toml", "rules": home / ".codex" / "AGENTS.md"}
    raise ValueError(f"Bilinmeyen harness: {harness!r}. Seçenekler: {', '.join(HARNESSES)}")


# ---------------------------------------------------------------- dosya yardimcilari


def _read(path: Path) -> str | None:
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def _write(path: Path, text: str, dry_run: bool, original: str | None) -> str:
    if original is not None and original == text:
        return "unchanged"
    if dry_run:
        return "dry_run"
    if original is not None:
        bak = path.with_name(path.name + ".bak")
        if not bak.exists():
            atomic_write_text(bak, original)
    atomic_write_text(path, text)
    return "created" if original is None else "updated"


def _upsert_block(text: str | None, block: str, start: str, end: str, uninstall: bool) -> str | None:
    text = text or ""
    if start in text and end in text:
        pre, rest = text.split(start, 1)
        _, post = rest.split(end, 1)
        post = post[1:] if post.startswith("\n") else post
        if uninstall:
            out = (pre.rstrip("\n") + ("\n\n" if pre.strip() and post.strip() else "") + post.lstrip("\n")
                   if (pre.strip() or post.strip()) else "")
            return out if out.strip() else ""
        return pre + block.rstrip("\n") + "\n" + post
    if uninstall:
        return text
    if not text.strip():
        return block
    return text.rstrip("\n") + "\n\n" + block


def _json_load(path: Path, text: str | None) -> dict:
    if text is None or not text.strip():
        return {}
    try:
        data = json.loads(text.lstrip("\ufeff"))
    except ValueError as exc:
        raise ValueError(f"{path} geçerli JSON değil; dokunulmadı: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} beklenen biçimde değil (nesne değil); dokunulmadı.")
    return data


def _mcp_entry(profile_name: str, mem_py: Path, python: str) -> dict:
    return {"command": python, "args": [str(mem_py), "mcp", "--profile", profile_name]}


def _toml_block(entry: dict) -> str:
    args = ", ".join(json.dumps(a, ensure_ascii=False) for a in entry["args"])
    return (f"{TOML_START}\n[mcp_servers.mem]\ncommand = {json.dumps(entry['command'], ensure_ascii=False)}\n"
            f"args = [{args}]\n{TOML_END}\n")


# ---------------------------------------------------------------- ana islev


def connect_harness(harness: str, profile_name: str, config_root_override=None, dry_run: bool = False,
                    uninstall: bool = False, mem_py_path=None, python_exe: str | None = None) -> dict:
    """MCP kaydi + global kural blogu yazar (veya uninstall ile yalniz kendi kayitlarini kaldirir)."""
    t = _targets(harness, config_root_override)
    mem_py = Path(mem_py_path) if mem_py_path else default_mem_py()
    entry = _mcp_entry(profile_name, mem_py, python_exe or sys.executable)
    files: list[dict] = []

    # --- MCP kaydi
    mcp_path: Path = t["mcp"]
    original = _read(mcp_path)
    if t["mcp_kind"] == "json":
        data = _json_load(mcp_path, original)
        servers = data.get("mcpServers")
        if servers is not None and not isinstance(servers, dict):
            raise ValueError(f"{mcp_path}: mcpServers nesne olmalı; dokunulmadı.")
        if uninstall:
            if isinstance(servers, dict):
                servers.pop("mem", None)
            if original is None:
                new_text = None
            elif data == {"mcpServers": {}}:
                new_text = ""
            else:
                new_text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        else:
            data.setdefault("mcpServers", {})
            data["mcpServers"]["mem"] = entry
            new_text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    else:
        block = _toml_block(entry)
        new_text = _upsert_block(original, block, TOML_START, TOML_END, uninstall)
        if original is not None and f"[mcp_servers.mem]" in original and TOML_START not in original and not uninstall:
            raise ValueError(f"{mcp_path}: [mcp_servers.mem] zaten elle tanımlı; dokunulmadı.")
    files.append(_apply(mcp_path, original, new_text, dry_run, uninstall))

    # --- kural blogu
    rules_path: Path = t["rules"]
    roriginal = _read(rules_path)
    rnew = _upsert_block(roriginal, RULE_BLOCK, BLOCK_START, BLOCK_END, uninstall)
    files.append(_apply(rules_path, roriginal, rnew, dry_run, uninstall))

    return {"harness": harness, "profile": profile_name, "uninstall": uninstall, "dry_run": dry_run,
            "mcp_entry": entry, "files": files}


def _apply(path: Path, original: str | None, new_text: str | None, dry_run: bool, uninstall: bool) -> dict:
    if uninstall:
        if original is None:
            return {"path": str(path), "action": "unchanged"}
        if new_text is not None and not new_text.strip():
            if dry_run:
                return {"path": str(path), "action": "dry_run"}
            bak = path.with_name(path.name + ".bak")
            if not bak.exists():
                atomic_write_text(bak, original)
            path.unlink()
            return {"path": str(path), "action": "removed"}
        action = _write(path, new_text or "", dry_run, original)
        return {"path": str(path), "action": action}
    return {"path": str(path), "action": _write(path, new_text or "", dry_run, original)}
