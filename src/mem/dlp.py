"""Kurumsal DLP ve sir kalkani. Varsayilan ACIK; sir asla hata mesajina/loga acik yazilmaz."""

from __future__ import annotations

import re
from pathlib import Path

DLP_MODE_BLOCK = "block"
DLP_MODE_REDACT = "redact"
DLP_MODE_OFF = "off"


class DLPBlockedError(ValueError):
    """block modunda hassas veri yakalandi. Yalniz kategori ve maskeli onizleme tasir."""

    def __init__(self, findings: list[dict]):
        self.findings = findings
        cats = sorted({f["category"] for f in findings})
        previews = ", ".join(f"{f['category']}:{f['preview']}" for f in findings[:5])
        super().__init__(
            "DLP: hassas veri tespit edildi, kayit reddedildi "
            f"(kategoriler: {', '.join(cats)}; maskeli onizleme: {previews}). "
            "Hassas kismi cikarip yeniden dene."
        )


_I = re.IGNORECASE

_API_TOKEN_PATTERNS = [
    r"\bgh[pousr]_[A-Za-z0-9]{20,}",
    r"\bgithub_pat_[A-Za-z0-9_]{20,}",
    r"\bsk-[A-Za-z0-9_\-]{16,}",
    r"\b(?:rk|sk|pk)_live_[A-Za-z0-9]{12,}",
    r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    r"\bAIza[0-9A-Za-z_\-]{35}",
    r"\bhf_[A-Za-z0-9]{30,}",
    r"\bxox[abeoprs]-[A-Za-z0-9\-]{10,}",
    r"\bnpm_[A-Za-z0-9]{30,}",
    r"\bSG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}",
    r"\b[MN][A-Za-z0-9]{23}\.[A-Za-z0-9_\-]{6}\.[A-Za-z0-9_\-]{27,}",
    r"\bhvs\.[A-Za-z0-9_\-]{20,}",
    r"sig=[A-Za-z0-9%+/=_\-]{10,}&(?:se|st|sp|sv)=",
    r"[?&](?:se|st|sp|sv)=[^&\s]+&sig=[A-Za-z0-9%+/=_\-]{10,}",
]
# Kapanis satiri yoksa (kesik PEM/PGP) basliktan sonraki bitisik satirlar da kapsanir.
_PRIVATE_KEY = (
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"
    r"(?:[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"
    # kesik anahtar: zirh basliklari, (PGP) tek bos satir, ardindan govde satirlari
    r"|(?:\r?\n[^\n\r]+){0,10}(?:\r?\n[ \t]*(?=\r?\n[^\n\r]))?(?:\r?\n[^\n\r]+){0,100})"
)
_JWT_PATTERNS = [
    r"\beyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]*",
    r"\beyJ[A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]*){4}",
    r"(?i)\b(?:Set-)?Cookie[ \t]*:[ \t]*[^=\s]+=\S+[^\n]*",
]
_AUTH_SINGLE_SCHEME_RE = re.compile(
    r"\bAuthorization[ \t]*:[ \t]*"
    r"(?:Bearer|Basic|Token|ApiKey|Api-Key|Negotiate|SharedKey|SSWS)"
    r"[ \t]+[\"']?(?P<val>[^\s\"'<>]{4,})[\"']?",
    _I,
)
_AUTH_PARAM_SCHEME_RE = re.compile(
    r"\bAuthorization[ \t]*:[ \t]*"
    r"(?:Digest|OAuth|AWS4-HMAC-SHA256|HOBA|Mutual)"
    r"[ \t]+(?P<val>[^\r\n]{4,})",
    _I,
)
_AUTH_RAW_RE = re.compile(
    r"\bAuthorization[ \t]*:[ \t]*(?P<val>[A-Za-z0-9._~+/=\-]{16,})(?![ \t]+[A-Za-zğüşıöçĞÜŞİÖÇ])",
    _I,
)
_BEARER_RE = re.compile(r"\bBearer[ \t]+(?P<val>[A-Za-z0-9._~+/=\-]{8,})")
_CONN_URI = re.compile(
    r"\b(?:postgres|postgresql|mysql|mariadb|mongodb|mongodb\+srv|redis|rediss|amqp|amqps|mssql|sqlserver)://[^\s\"']+",
    _I,
)
_HTTP_CRED_URI = re.compile(
    r"\bhttps?://[^/?#\s\"'<>@:]*:[^/?#\s\"'<>@]+@[^/?#\s\"'<>][^\s\"'<>]*",
    _I,
)
_CONN_ADO = re.compile(
    r"\b(?:Server|Data Source|Host)=[^;\n]+;[^\n]*?(?:Password|Pwd)=[^;\n]+",
    _I,
)
# Sira bagimsiz ADO.NET: Password/Pwd, ayni satirda baska baglanti anahtarlariyla birlikte.
_ADO_SECRET = re.compile(r"\b(?:Password|Pwd)\s*=\s*[^;\n]+", _I)
_ADO_CONTEXT = re.compile(
    r"\b(?:Server|Data Source|Host|User Id|Uid|Initial Catalog|Database|Integrated Security)\s*=", _I
)
_CAMEL_LOWER_UPPER_RE = re.compile(r"([a-z0-9])([A-Z])")
_CAMEL_ACRONYM_RE = re.compile(r"([A-Z]+)([A-Z][a-z])")
_NON_SECRET_TOKEN_KEY_RE = re.compile(
    r"(?:(?:^|[_.\-])(?:max|min|total|input|output|prompt|completion|thinking|cached|num|budget|"
    r"cancel|cancellation|stop|csrf|xsrf)"
    r"|^(?:page|next[_.\-]?page|prev[_.\-]?page|continuation))[_.\-]?token$"
)
_NON_SECRET_PASS_WORD_RE = re.compile(r"(?:^|[_.\-])(?:by|com|sur|tres|over|under)pass$")
_CONN_URI_QUERY_SECRET = re.compile(
    r"(?<=[?&#;])(?P<key>[A-Za-z0-9_.\-]*"
    r"(?:password|passwd|pwd|pass|secret|client[_-]?secret|token|api[_-]?key|access[_-]?key))"
    r"=(?P<val>[^&#;\s\"'`<>]+)",
    _I,
)
_URI_QUERY_PARAM = re.compile(
    r"(?<=[?&#;])(?P<key>[A-Za-z0-9_.\-]*"
    r"(?:password|passwd|pwd|pass|secret|client[_-]?secret|token|api[_-]?key|access[_-]?key))"
    r"=(?P<val>[^&#;\s\"'`<>]{8,})",
    _I,
)
# `DB_PASSWORD=`, `dbPassword=`, `accessToken:`, `GITHUB_TOKEN=`, `TOKEN=`, `"token": "..."` dahil.
_GENERIC = re.compile(
    r"(?<![A-Za-z0-9_.\-])(?P<key>[A-Za-z0-9_.\-]*"
    r"(?:api[_-]?key|access[_-]?key|secret[_-]?access[_-]?key|secret[_-]?key|private[_-]?key|"
    r"client[_-]?secret|secret|password|passwd|pwd|token))"
    r"[\"']?\s*[:=]\s*[\"']?(?P<val>(?:\$\{[^{}\s`]+\}|\{\{[^{}\s`]+\}\}|[^\s\"'`,;\}])+)",
    _I,
)
_IBAN = re.compile(r"\bTR\d{2}\s?(?:\d{4}\s?){5}\d{2}\b")
_TCKN = re.compile(r"(?<!\d)[1-9]\d{10}(?!\d)")

_COMPILED_STATIC: list[tuple[str, re.Pattern]] = []
for _p in _API_TOKEN_PATTERNS:
    _COMPILED_STATIC.append(("api_token", re.compile(_p)))
_COMPILED_STATIC.append(("private_key", re.compile(_PRIVATE_KEY)))
for _p in _JWT_PATTERNS:
    _COMPILED_STATIC.append(("jwt_bearer", re.compile(_p)))


_PLACEHOLDER_FULL_RE = re.compile(
    r"^(?:"
    r"<[^<>\r\n`]+>"
    r"|\$\{[^{}\r\n`]+\}"
    r"|\{\{[^{}\r\n`]+\}\}"
    r"|\[REDACTED:[^\[\]\r\n`]+\]"
    r"|\$[A-Z_][A-Z0-9_]*"
    r")+$"
)


def _is_placeholder_value(val: str) -> bool:
    v = val.strip()
    if not v:
        return True
    low = v.lower()
    if low in ("null", "none", "true", "false", "undefined", "redacted"):
        return True
    if _PLACEHOLDER_FULL_RE.match(v):
        return True
    if len(set(v)) == 1 and v[0] in ".*xX-_?":
        return True
    return False


def _is_non_secret_key(key: str) -> bool:
    leaf = key.rsplit(".", 1)[-1].lstrip("-")
    norm = _CAMEL_LOWER_UPPER_RE.sub(r"\1_\2", leaf)
    norm = _CAMEL_ACRONYM_RE.sub(r"\1_\2", norm).lower()
    if _NON_SECRET_TOKEN_KEY_RE.search(norm):
        return True
    if _NON_SECRET_PASS_WORD_RE.search(norm):
        return True
    return False


def _has_param_auth_secret(val: str) -> bool:
    s = val.strip()
    if not s:
        return False
    if "=" in s:
        for part in s.split(","):
            if "=" not in part:
                continue
            rhs = part.split("=", 1)[1].strip().strip("\"'")
            if not _is_placeholder_value(rhs):
                return True
        return False
    if _is_placeholder_value(s):
        return False
    if any(c.isspace() for c in s) or any(c in s for c in "\"'<>"):
        return False
    return len(s) >= 4


def _extract_authority_host(authority: str) -> str:
    hostport = authority.rsplit("@", 1)[-1]
    if hostport.startswith("[") and "]" in hostport:
        return hostport[1 : hostport.index("]")].lower()
    return hostport.split(":", 1)[0].lower()


def _has_query_secret(uri: str) -> bool:
    for m in _CONN_URI_QUERY_SECRET.finditer(uri):
        key, val = m.group("key"), m.group("val")
        if _is_non_secret_key(key) or _is_placeholder_value(val):
            continue
        return True
    return False


def valid_tckn(number: str) -> bool:
    """Resmi TCKN algoritmasi (10. ve 11. hane dogrulamasi)."""
    if len(number) != 11 or not number.isdigit() or number[0] == "0":
        return False
    d = [int(c) for c in number]
    odd = d[0] + d[2] + d[4] + d[6] + d[8]
    even = d[1] + d[3] + d[5] + d[7]
    if (odd * 7 - even) % 10 != d[9]:
        return False
    return sum(d[:10]) % 10 == d[10]


def valid_tr_iban(iban: str) -> bool:
    """Resmi ISO 7064 Mod 97-10 TR IBAN kontrol hanesi dogrulamasi."""
    compact = re.sub(r"\s+", "", iban).upper()
    if len(compact) != 26 or not compact.startswith("TR") or not compact[2:].isdigit():
        return False
    rearranged = compact[4:] + "2927" + compact[2:4]
    return int(rearranged) % 97 == 1


def _mask(raw: str) -> str:
    return f"{'*' * 4}({len(raw)} karakter)"


def load_custom_patterns(vault_root) -> list[tuple[str, re.Pattern]]:
    """Kasa kokundeki dlp-patterns.txt: `regex:` oneki -> sinirli regex, aksi halde literal."""
    if vault_root is None:
        return []
    path = Path(vault_root) / "dlp-patterns.txt"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    out: list[tuple[str, re.Pattern]] = []
    for line in lines:
        line = line.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("regex:"):
            expr = line[6:].strip()
            # sinirlama: uzunluk ve ic ice nicelik (ReDoS) korumasi
            if not expr or len(expr) > 200 or re.search(r"\)[*+{]", expr):
                continue
            try:
                out.append(("custom_pattern", re.compile(expr, _I)))
            except re.error:
                continue
        else:
            out.append(("custom_pattern", re.compile(re.escape(line), _I)))
    return out


def scan(text: str, vault_root=None) -> list[dict]:
    """Bulgular: [{category, start, end, preview}] (yakalanan metin dahil DEGIL)."""
    findings: list[dict] = []

    def add(cat: str, m: re.Match) -> None:
        findings.append(
            {"category": cat, "start": m.start(), "end": m.end(), "preview": _mask(m.group(0))}
        )

    for cat, pat in _COMPILED_STATIC:
        for m in pat.finditer(text):
            add(cat, m)
    for m in _AUTH_SINGLE_SCHEME_RE.finditer(text):
        if not _is_placeholder_value(m.group("val")):
            add("jwt_bearer", m)
    for m in _AUTH_PARAM_SCHEME_RE.finditer(text):
        raw_val = m.group("val").rstrip(" \t")
        if len(raw_val) >= 4 and _has_param_auth_secret(raw_val):
            end = m.start("val") + len(raw_val)
            findings.append(
                {"category": "jwt_bearer", "start": m.start(), "end": end, "preview": _mask(text[m.start() : end])}
            )
    for m in _AUTH_RAW_RE.finditer(text):
        val = m.group("val")
        if _is_placeholder_value(val):
            continue
        if any(not c.isalpha() for c in val) or (val != val.lower() and val != val.upper()):
            add("jwt_bearer", m)
    for m in _BEARER_RE.finditer(text):
        val = m.group("val")
        if _is_placeholder_value(val):
            continue
        if val.isalpha() and val.lower().startswith(("token", "auth", "header", "scheme", "format", "yetki")):
            continue
        add("jwt_bearer", m)
    for m in _CONN_URI.finditer(text):
        uri = m.group(0)
        rest = uri.split("://", 1)[1]
        authority = rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
        has_cred = "@" in authority
        host = _extract_authority_host(authority)
        if not has_cred and host in ("localhost", "127.0.0.1", "::1") and not _has_query_secret(uri):
            continue
        add("connection_string", m)
    for m in _HTTP_CRED_URI.finditer(text):
        uri = m.group(0)
        authority = uri.split("://", 1)[1].split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
        if "@" not in authority:
            continue
        userinfo = authority.rsplit("@", 1)[0]
        pwd = userinfo.split(":", 1)[1] if ":" in userinfo else ""
        if pwd and not _is_placeholder_value(pwd):
            add("connection_string", m)
    for m in _CONN_ADO.finditer(text):
        add("connection_string", m)
    for line_m in re.finditer(r"[^\n]+", text):
        line = line_m.group(0)
        if _ADO_CONTEXT.search(line):
            for m in _ADO_SECRET.finditer(line):
                findings.append({"category": "connection_string", "start": line_m.start() + m.start(),
                                 "end": line_m.start() + m.end(), "preview": _mask(m.group(0))})
    for m in _URI_QUERY_PARAM.finditer(text):
        key, val = m.group("key"), m.group("val")
        if len(val) < 8 or _is_non_secret_key(key) or _is_placeholder_value(val):
            continue
        add("generic_secret_assignment", m)
    for m in _GENERIC.finditer(text):
        key, val = m.group("key"), m.group("val")
        if len(val) < 8 or _is_non_secret_key(key) or _is_placeholder_value(val):
            continue
        add("generic_secret_assignment", m)
    for m in _IBAN.finditer(text):
        if valid_tr_iban(m.group(0)):
            add("pii_tr", m)
    for m in _TCKN.finditer(text):
        if valid_tckn(m.group(0)):
            add("pii_tr", m)
    for cat, pat in load_custom_patterns(vault_root):
        for m in pat.finditer(text):
            if m.end() > m.start():
                add(cat, m)
    findings.sort(key=lambda f: (f["start"], -f["end"]))
    return findings


def _merge(findings: list[dict]) -> list[dict]:
    merged: list[dict] = []
    for f in findings:
        if merged and f["start"] < merged[-1]["end"]:
            if f["end"] > merged[-1]["end"]:
                merged[-1]["end"] = f["end"]
            continue
        merged.append(dict(f))
    return merged


def inspect_and_apply(text: str, mode: str, vault_root=None) -> tuple[str, list[dict]]:
    if mode not in (DLP_MODE_BLOCK, DLP_MODE_REDACT, DLP_MODE_OFF):
        raise ValueError(f"Gecersiz DLP modu: {mode!r}")
    if mode == DLP_MODE_OFF or not text:
        return text, []
    findings = scan(text, vault_root)
    if not findings:
        return text, []
    if mode == DLP_MODE_BLOCK:
        raise DLPBlockedError(findings)
    out: list[str] = []
    pos = 0
    for f in _merge(findings):
        out.append(text[pos : f["start"]])
        out.append(f"[REDACTED:{f['category']}]")
        pos = f["end"]
    out.append(text[pos:])
    return "".join(out), findings
