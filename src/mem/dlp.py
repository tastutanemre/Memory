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
    r"\bAuthorization\s*:\s*\S+(?:[ \t]+\S+)?",
    r"\bBearer[ \t]+[A-Za-z0-9._~+/=\-]{8,}",
    r"(?i)\b(?:Set-)?Cookie[ \t]*:[ \t]*[^=\s]+=\S+[^\n]*",
]
_CONN_URI = re.compile(
    r"\b(?:postgres|postgresql|mysql|mariadb|mongodb|mongodb\+srv|redis|rediss|amqp|amqps|mssql|sqlserver)://[^\s\"']+",
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
_URI_QUERY_SECRET = re.compile(r"[?&;](?:password|passwd|pwd|pass|secret|token|api[_-]?key)=[^&\s]+", _I)
# `DB_PASSWORD=`, `JWT_SECRET=`, `AWS_SECRET_ACCESS_KEY=`, `"password": "..."` gibi onekli/tirnakli atamalar dahil.
_GENERIC = re.compile(
    r"(?:api[_-]?key|access[_-]?key|secret[_-]?access[_-]?key|secret[_-]?key|private[_-]?key|client[_-]?secret|"
    r"secret|password|passwd|pwd|auth[_-]?token|access[_-]?token|refresh[_-]?token)"
    r"[\"']?\s*[:=]\s*[\"']?[^\s\"',;]{8,}",
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
    for m in _CONN_URI.finditer(text):
        uri = m.group(0)
        rest = uri.split("://", 1)[1]
        authority = rest.split("/", 1)[0]
        has_cred = "@" in authority
        host = authority.rsplit("@", 1)[-1].split(":")[0].lower()
        if not has_cred and host in ("localhost", "127.0.0.1", "::1") and not _URI_QUERY_SECRET.search(uri):
            continue
        add("connection_string", m)
    for m in _CONN_ADO.finditer(text):
        add("connection_string", m)
    for line_m in re.finditer(r"[^\n]+", text):
        line = line_m.group(0)
        if _ADO_CONTEXT.search(line):
            for m in _ADO_SECRET.finditer(line):
                findings.append({"category": "connection_string", "start": line_m.start() + m.start(),
                                 "end": line_m.start() + m.end(), "preview": _mask(m.group(0))})
    for m in _GENERIC.finditer(text):
        add("generic_secret_assignment", m)
    for m in _IBAN.finditer(text):
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
