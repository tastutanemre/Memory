"""Sifir bagimlilikli, deterministik YAML frontmatter okuyucu/yazici (Obsidian uyumlu alt kume)."""

from __future__ import annotations

import json
import re

_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.\-]*)[ \t]*:(?:[ \t]+(.*))?$")
_INT_RE = re.compile(r"^[+-]?(0|[1-9][0-9]*)$")
_FLOAT_RE = re.compile(r"^[+-]?[0-9]+\.[0-9]+([eE][+-]?[0-9]+)?$")
_RESERVED = {"true", "false", "null", "~", "yes", "no", "on", "off", "y", "n"}
_SPECIAL = set(":#[]{},&*!|>'\"%@`\\\n\r\t")


# ---------------------------------------------------------------- okuma


def _find_quote_end(s: str) -> int:
    """s[0] cift tirnak; kapanis indeksi (kacislari atlar) veya -1."""
    i = 1
    while i < len(s):
        if s[i] == "\\":
            i += 2
            continue
        if s[i] == '"':
            return i
        i += 1
    return -1


def _flow_end(s: str) -> int:
    """s[0] '[' veya '{'; eslesen kapanis indeksi (tirnaklari atlar) veya -1."""
    depth = 0
    quote = None
    i = 0
    while i < len(s):
        ch = s[i]
        if quote:
            if quote == '"' and ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _split_flow(inner: str) -> list[str]:
    items: list[str] = []
    buf: list[str] = []
    depth = 0
    quote = None
    i = 0
    while i < len(inner):
        ch = inner[i]
        if quote:
            buf.append(ch)
            if quote == '"' and ch == "\\" and i + 1 < len(inner):
                buf.append(inner[i + 1])
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch in "[{":
            depth += 1
            buf.append(ch)
        elif ch in "]}":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            items.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf)
    if tail.strip() or items:
        items.append(tail)
    return [x for x in items if x.strip() != ""]


def _parse_scalar(raw: str):
    s = raw.strip()
    if s == "":
        return None
    if s[0] == '"':
        end = _find_quote_end(s)
        if end > 0:
            try:
                return json.loads(s[: end + 1])
            except ValueError:
                return s[1:end]
        return s
    if s[0] == "'":
        out = []
        i = 1
        while i < len(s):
            if s[i] == "'":
                if i + 1 < len(s) and s[i + 1] == "'":
                    out.append("'")
                    i += 2
                    continue
                return "".join(out)
            out.append(s[i])
            i += 1
        return s
    if s[0] in "[{":
        end = _flow_end(s)
        if end > 0:
            s = s[: end + 1]  # kapanistan sonraki ` # yorum` yok sayilir; ic metindeki # korunur
            if s[0] == "[":
                try:
                    return json.loads(s)
                except ValueError:
                    return [_parse_scalar(x) for x in _split_flow(s[1:-1])]
            try:
                return json.loads(s)
            except ValueError:
                return s
    s = re.sub(r"\s+#.*$", "", s).strip()
    low = s.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "~"):
        return None
    if _INT_RE.match(s):
        return int(s)
    if _FLOAT_RE.match(s):
        return float(s)
    if s[0] == "[" and s[-1] == "]":
        try:
            return json.loads(s)
        except ValueError:
            return [_parse_scalar(x) for x in _split_flow(s[1:-1])]
    if s[0] == "{" and s[-1] == "}":
        try:
            return json.loads(s)
        except ValueError:
            return s
    return s


def _parse_block(lines: list[str]) -> dict:
    meta: dict = {}
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#") or line[0] in " \t":
            i += 1
            continue
        m = _KEY_RE.match(line.rstrip())
        if not m:
            i += 1
            continue
        key, rest = m.group(1), m.group(2)
        i += 1
        rest = rest if rest is not None else ""
        if rest.strip() in ("|", ">", "|-", ">-"):
            chunk = []
            while i < n and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
                chunk.append(lines[i].strip())
                i += 1
            joiner = "\n" if rest.strip().startswith("|") else " "
            meta[key] = joiner.join(chunk).strip()
            continue
        if rest.strip() != "" and not rest.strip().startswith("#"):
            meta[key] = _parse_scalar(rest)
            continue
        # deger sonraki satirlarda: liste veya ic ice sozluk
        chunk = []
        while i < n and (lines[i].startswith((" ", "\t")) or lines[i].startswith("- ") or lines[i].strip() == "-"):
            chunk.append(lines[i])
            i += 1
        stripped = [c.strip() for c in chunk if c.strip()]
        if not stripped:
            meta[key] = None
        elif all(c.startswith("-") for c in stripped):
            meta[key] = [_parse_scalar(c[1:]) for c in stripped]
        else:
            indent = min(len(c) - len(c.lstrip()) for c in chunk if c.strip())
            meta[key] = _parse_block([c[indent:] for c in chunk])
    return meta


def parse_markdown(text: str) -> tuple[dict, str]:
    """(frontmatter_dict, govde). Frontmatter yoksa ({}, text)."""
    if text.startswith("\ufeff"):
        text = text[1:]
    text = text.replace("\r\n", "\n")
    if not (text.startswith("---\n") or text == "---"):
        return {}, text
    lines = text.split("\n")
    end = None
    for idx in range(1, len(lines)):
        if lines[idx].rstrip() in ("---", "..."):
            end = idx
            break
    if end is None:
        return {}, text
    block_lines = lines[1:end]
    block = "\n".join(block_lines)
    body = "\n".join(lines[end + 1 :])
    if body.startswith("\n"):
        body = body[1:]
    stripped = block.strip()
    if stripped.startswith("{"):
        try:
            meta = json.loads(stripped)
            if isinstance(meta, dict):
                return meta, body
        except ValueError:
            pass
    return _parse_block(block_lines), body


# ---------------------------------------------------------------- yazma


def _is_plain(v: str) -> bool:
    if v == "" or v != v.strip():
        return False
    if any(ch in _SPECIAL for ch in v):
        return False
    if v[0] in "-?":
        return False
    if v.lower() in _RESERVED:
        return False
    if _INT_RE.match(v) or _FLOAT_RE.match(v):
        return False
    return True


def _dump_scalar(v) -> str:
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return v if _is_plain(v) else json.dumps(v, ensure_ascii=False)
    return json.dumps(v, ensure_ascii=False, default=str)


def _safe_key(k) -> str:
    k = str(k)
    if _KEY_RE.match(k + ":"):
        return k
    return re.sub(r"[^A-Za-z0-9_.\-]", "_", k) or "_"


def dump_markdown(meta: dict, body: str) -> str:
    out = ["---"]
    for key, value in meta.items():
        k = _safe_key(key)
        if isinstance(value, (list, tuple)):
            if not value:
                out.append(f"{k}: []")
            else:
                out.append(f"{k}:")
                for item in value:
                    out.append(f"  - {_dump_scalar(item)}")
        elif isinstance(value, dict):
            out.append(f"{k}: {json.dumps(value, ensure_ascii=False, default=str)}")
        else:
            out.append(f"{k}: {_dump_scalar(value)}")
    out.append("---")
    return "\n".join(out) + "\n\n" + body
