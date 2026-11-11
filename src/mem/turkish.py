"""Cift katmanli Turkce kok/ek + teknik terim koruyucu tokenizasyon."""

from __future__ import annotations

import re

_FOLD_TABLE = str.maketrans(
    {
        "ı": "i", "İ": "i", "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g",
        "ü": "u", "Ü": "u", "ö": "o", "Ö": "o", "ç": "c", "Ç": "c",
    }
)
_WORD_RE = re.compile(r"[^\W_]+(?:[._\-][^\W_]+)*", re.UNICODE)
_TECH_FRAGMENTS = (
    "q", "w", "x", "th", "sh", "tion",
    "claude", "docker", "token", "server", "config", "schema", "query",
)
_STOPWORDS = {
    "ve", "ile", "bir", "bu", "su", "de", "da", "mi", "ne", "icin", "gibi", "ama", "veya",
    "neden", "nasil", "niye", "hangi", "nedir", "neye",
    "the", "an", "of", "to", "in", "and", "or", "for", "is", "on", "at", "it",
}
# Katlanmis (ASCII) sonek listesi; uzundan kisaya denenir.
_SUFFIXES = sorted(
    {
        "lar", "ler", "dan", "den", "tan", "ten", "nda", "nde", "ndan", "nden",
        "da", "de", "ta", "te", "in", "un", "ki", "yi", "ya", "ye",
        "la", "le", "sin", "sun",
        "a", "e", "i", "u",
        "si", "su", "imiz", "iniz", "lari", "leri", "umuz", "unuz",
        "lik", "luk", "siz", "ci", "cu",
    },
    key=len,
    reverse=True,
)
_UNSOFTEN = {"kayd": "kayit"}
_SOFT_BACK = {"b": "p", "d": "t", "g": "k"}
_MAX_ITER = 7


def fold_tr(text: str) -> str:
    """Turkce karakterleri ASCII'ye katlar ve kucuk harfe cevirir."""
    return text.translate(_FOLD_TABLE).lower()


def tokenize(text: str) -> list[str]:
    return _WORD_RE.findall(text)


def is_technical_token(token: str) -> bool:
    """CamelCase, rakam/altcizgi/nokta/tire iceren veya Ingilizce teknik harf dizili kelimeler."""
    if any(ch.isdigit() for ch in token) or any(ch in token for ch in "_.-"):
        return True
    if re.search(r"[a-z][A-Z]", token) or re.fullmatch(r"[A-Z]{2,}s?", token):
        return True
    low = token.replace("İ", "i").lower()
    return any(frag in low for frag in _TECH_FRAGMENTS)


def _soften_back(w: str) -> str:
    """Son unsuz yumusamasini geri al: kitab->kitap, onbelleg->onbellek, kanad->kanat."""
    if w in _UNSOFTEN:
        return _UNSOFTEN[w]
    if len(w) >= 4 and w[-1] in _SOFT_BACK:
        return w[:-1] + _SOFT_BACK[w[-1]]
    return w


def stem_tr(word: str) -> str:
    """Guvenli sonek soyucu (katlanmis uzayda); min kok 3 (tek harfli ek icin 4).

    Yumusama geri alma her adimda uygulanir; boylece `guvenligi` ve `guvenlikte` ayni koke iner.
    """
    w = fold_tr(word)
    if not w.isalpha():
        return w
    for _ in range(_MAX_ITER):
        w = _soften_back(w)
        for suf in _SUFFIXES:
            minimum = 3 if len(suf) > 1 else 4
            if w.endswith(suf) and len(w) - len(suf) >= minimum:
                w = w[: -len(suf)]
                break
        else:
            break
    return _soften_back(w)


def _terms(token: str) -> tuple[str, str | None]:
    exact = fold_tr(token)
    if is_technical_token(token):
        return exact, None
    stem = stem_tr(token)
    return exact, (stem if stem != exact else None)


def index_tokens(text: str) -> str:
    """FTS5 shadow_terms icin: her kelime icin exact_folded (+ stem_folded)."""
    out: list[str] = []
    for tok in tokenize(text):
        if len(tok) < 2:
            continue
        exact, stem = _terms(tok)
        out.append(exact)
        if stem:
            out.append(stem)
    return " ".join(out)


def query_fts_expression(query: str) -> str:
    """Kullanici sorgusunu guvenli FTS5 ifadesine cevirir (OR'lu; bos ise '')."""
    toks = [t for t in tokenize(query) if len(t) >= 2]
    meaningful = [t for t in toks if fold_tr(t) not in _STOPWORDS] or toks
    groups: list[str] = []
    seen: set[str] = set()
    for tok in meaningful:
        exact, stem = _terms(tok)
        key = exact + "|" + (stem or "")
        if key in seen:
            continue
        seen.add(key)
        if stem:
            groups.append(f'("{exact}" OR "{stem}")')
        else:
            groups.append(f'"{exact}"')
    return " OR ".join(groups)
