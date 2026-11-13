"""Google Gemini Decisions API (`gemini-decision-1`) yan adaptoru.

Guvenlik sinirlari:
- Yalniz sabit izinli uc nokta (https + generativelanguage.googleapis.com + /v1beta/...).
- Giden her metin once `mem.dlp.scan` ile taranir; bulgu varsa o metin ASLA gonderilmez.
- API anahtari loglanmaz, hata mesajina ve `repr`'e yazilmaz.
- Hata/zaman asimi = fail-open: yerel sonuclar aynen korunur.
- Ag erisimi (`urllib`) yalniz bu pakettedir; `src/mem` cekirdegi sifir-ag kalir.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

from mem import config, dlp
from mem.lock import atomic_write_text, read_text_retry

DECISIONS_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/decisions"
DEFAULT_MODEL = "gemini-decision-1"
ALLOWED_HOST = "generativelanguage.googleapis.com"
ALLOWED_PATH_PREFIX = "/v1beta/"
KEY_ENV_VARS = ("MEM_GEMINI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")
DEFAULT_TIMEOUT_SEC = 8.0
MAX_RESPONSE_BYTES = 1_000_000
CACHE_TTL_SEC = 7 * 24 * 3600
CACHE_MAX_ENTRIES = 2000
RERANK_MAX_CANDIDATES = 24
RESCUE_MIN_SEMANTIC = 0.35
ADVISOR_CONFIDENCE = 0.78
MAX_PRIORS = 8
FIELD_LIMIT = 1200

Transport = Callable[[str, dict, bytes, float], dict]

RERANK_CRITERIA = [
    "Unrelated to the query",
    "Loosely related domain only",
    "Partially relevant context or background",
    "Directly answers or states the governing rule/decision for the query",
]
COMMITMENT_CRITERIA = {
    "asserted": "A concrete architectural, product, or engineering decision has been chosen and committed.",
    "tentative": "Still a hypothesis, open question, brainstorm, or undecided exploration.",
}
RELATION_CRITERIA = {
    "unrelated": "Covers a different topic or orthogonal concern.",
    "compatible_refinement": "Builds on or refines the prior decision without contradicting or duplicating it.",
    "duplicate": "Restates substantially the same decision already recorded in the prior.",
    "contradicts_or_supersedes": "Replaces, reverses, or conflicts with the prior decision.",
}


class AdvisorError(RuntimeError):
    """Danisman istegi basarisiz (mesaj gizli veri icermez)."""


def _debug(message: str) -> None:
    """Yalniz MEM_ADVISOR_DEBUG=1 iken, gizli veri icermeyen tek satir (stderr)."""
    if os.environ.get("MEM_ADVISOR_DEBUG") == "1":
        print(f"mem-advisor: {message}", file=sys.stderr)


# ---------------------------------------------------------------- uc nokta / anahtar


def _validate_endpoint(url: str) -> str:
    """Yalniz https://generativelanguage.googleapis.com/v1beta/... adresine izin verir."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise ValueError("Gecersiz danisman uc noktasi") from exc
    if (
        parts.scheme != "https"
        or host != ALLOWED_HOST
        or port not in (None, 443)
        or parts.username is not None
        or parts.password is not None
        or not parts.path.startswith(ALLOWED_PATH_PREFIX)
        or parts.fragment
    ):
        raise ValueError(f"Danisman uc noktasi izinli degil: yalniz https://{ALLOWED_HOST}{ALLOWED_PATH_PREFIX}*")
    return url


def load_advisor_settings() -> dict:
    """advisor.json (kasa DISINDA, MEM_HOME altinda); yok/bozuksa bos sozluk."""
    path = config.advisor_config_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(read_text_retry(path))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_advisor_settings(*, gemini_api_key: str | None = None, clear_key: bool = False,
                          model: str | None = None) -> Path:
    """Ayarlari birlestirerek yazar. Anahtar yalniz bu dosyaya yazilir (POSIX'te 0600)."""
    data = load_advisor_settings()
    if clear_key:
        data.pop("gemini_api_key", None)
    if gemini_api_key is not None:
        key = gemini_api_key.strip()
        if not (8 <= len(key) <= 512) or any(ch.isspace() for ch in key):
            raise ValueError("Gecersiz API anahtari bicimi (bosluksuz, 8-512 karakter olmali).")
        data["gemini_api_key"] = key
    if model is not None:
        data["model"] = model.strip() or DEFAULT_MODEL
    path = config.advisor_config_path()
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def _key_with_source() -> tuple[str | None, str | None]:
    for name in KEY_ENV_VARS:
        val = (os.environ.get(name) or "").strip()
        if val:
            return val, f"env:{name}"
    val = load_advisor_settings().get("gemini_api_key")
    if isinstance(val, str) and val.strip():
        return val.strip(), "file:advisor.json"
    return None, None


def resolve_api_key() -> str | None:
    """MEM_GEMINI_API_KEY > GEMINI_API_KEY > GOOGLE_API_KEY > advisor.json; yoksa None."""
    return _key_with_source()[0]


def advisor_status(profile_name: str, dlp_mode: str, mode: str = "off") -> dict:
    """Durum ozeti; anahtarin KENDISINI asla icermez (yalniz key_configured/key_source)."""
    _key, source = _key_with_source()
    settings = load_advisor_settings()
    return {
        "mode": mode,
        "allowed": config.can_use_advisor(profile_name, dlp_mode),
        "key_configured": source is not None,
        "key_source": source,
        "model": str(settings.get("model") or DEFAULT_MODEL),
        "endpoint": DECISIONS_ENDPOINT,
    }


# ---------------------------------------------------------------- tasima / istemci


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401, ARG002
        return None  # yonlendirme izlenmez: izinli uc nokta disina cikis yok


def _urllib_transport(url: str, headers: dict, body: bytes, timeout: float) -> dict:
    _validate_endpoint(url)
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise AdvisorError(f"HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AdvisorError(f"ag hatasi: {type(exc).__name__}") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise AdvisorError("yanit cok buyuk")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise AdvisorError("gecersiz JSON yaniti") from None
    if not isinstance(data, dict):
        raise AdvisorError("beklenmeyen yanit bicimi")
    return data


class GeminiDecisionsClient:
    """`/v1beta/decisions` istemcisi; `transport` ile ag olmadan test edilebilir."""

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, endpoint: str = DECISIONS_ENDPOINT,
                 transport: Transport | None = None, timeout_sec: float = DEFAULT_TIMEOUT_SEC):
        if not api_key or not api_key.strip():
            raise ValueError("API anahtari bos")
        self._api_key = api_key.strip()
        self.model = model or DEFAULT_MODEL
        self.endpoint = _validate_endpoint(endpoint)
        self.timeout_sec = float(timeout_sec)
        self._transport: Transport = transport or _urllib_transport

    def __repr__(self) -> str:  # anahtar asla yazilmaz
        return f"GeminiDecisionsClient(model={self.model!r}, endpoint={self.endpoint!r}, api_key=<redacted>)"

    def decide(self, input_data: str | list | dict, questions: dict) -> dict:
        """Tek istek; `answers` sozlugunu dondurur. Yapilandirilmis sozluk girdisi JSON metin olarak serilestirilir."""
        wire_input = (
            json.dumps(input_data, ensure_ascii=False, indent=2)
            if isinstance(input_data, dict)
            else input_data
        )
        body = json.dumps({"model": self.model, "input": wire_input, "questions": questions},
                          ensure_ascii=False).encode("utf-8")
        headers = {"x-goog-api-key": self._api_key, "Content-Type": "application/json"}
        try:
            resp = self._transport(self.endpoint, headers, body, self.timeout_sec)
        except AdvisorError:
            raise
        except (OSError, ValueError, TimeoutError) as exc:
            raise AdvisorError(f"tasima hatasi: {type(exc).__name__}") from None
        answers = resp.get("answers") if isinstance(resp, dict) else None
        if not isinstance(answers, dict):
            raise AdvisorError("yanitta 'answers' yok")
        return answers


# ---------------------------------------------------------------- onbellek


def make_cache_key(model: str, operation: str, payload: dict) -> str:
    canon = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{model}\n{operation}\n{canon}".encode("utf-8")).hexdigest()


class AdvisorCache:
    """Yerel SQLite onbellegi (state_dir/advisor_cache.sqlite3): yalniz ozet anahtar + cevap JSON'u."""

    def __init__(self, state_dir, ttl_sec: int = CACHE_TTL_SEC, max_entries: int = CACHE_MAX_ENTRIES):
        self.path = Path(state_dir) / "advisor_cache.sqlite3"
        self.ttl_sec = ttl_sec
        self.max_entries = max_entries

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=5.0)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS advisor_cache "
            "(cache_key TEXT PRIMARY KEY, value TEXT NOT NULL, created REAL NOT NULL)"
        )
        return conn

    def get(self, key: str) -> dict | None:
        try:
            conn = self._connect()
            try:
                row = conn.execute("SELECT value, created FROM advisor_cache WHERE cache_key=?", (key,)).fetchone()
                if row is None:
                    return None
                if time.time() - float(row[1]) > self.ttl_sec:
                    conn.execute("DELETE FROM advisor_cache WHERE cache_key=?", (key,))
                    conn.commit()
                    return None
                value = json.loads(row[0])
                return value if isinstance(value, dict) else None
            finally:
                conn.close()
        except (sqlite3.Error, OSError, ValueError):
            return None

    def put(self, key: str, value: dict) -> None:
        try:
            conn = self._connect()
            try:
                now = time.time()
                conn.execute(
                    "INSERT OR REPLACE INTO advisor_cache(cache_key, value, created) VALUES (?,?,?)",
                    (key, json.dumps(value, ensure_ascii=False), now),
                )
                conn.execute("DELETE FROM advisor_cache WHERE created < ?", (now - self.ttl_sec,))
                conn.execute(
                    "DELETE FROM advisor_cache WHERE cache_key IN (SELECT cache_key FROM advisor_cache "
                    "ORDER BY created DESC LIMIT -1 OFFSET ?)",
                    (self.max_entries,),
                )
                conn.commit()
            finally:
                conn.close()
        except (sqlite3.Error, OSError, TypeError, ValueError):
            pass


# ---------------------------------------------------------------- DLP on-ucus


def _is_dlp_clean(text: str, vault_root=None) -> bool:
    """Bulgu yoksa True. Tarama hatasi = fail-closed (False): metin gonderilmez."""
    if not text:
        return True
    try:
        return not dlp.scan(text, vault_root)
    except Exception:  # noqa: BLE001 - emin olamazsak GONDERME
        return False


def _clip(text) -> str:
    return str(text or "")[:FIELD_LIMIT]


def _num(value, lo: float, hi: float, default: float | None = None) -> float | None:
    try:
        if isinstance(value, bool):
            return default
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 1. yetenek: anlamsal yeniden siralama


class GeminiSemanticReranker:
    """FTS adaylarini (ve sifir-kelime kurtarma havuzunu) Decisions API ile anlamsal puanlar."""

    is_external = True

    def __init__(self, client: GeminiDecisionsClient | None, cache: AdvisorCache | None = None,
                 vault_root=None):
        self.client = client
        self.cache = cache
        self.vault_root = vault_root

    @staticmethod
    def _local_only(results: list[dict]) -> list[dict]:
        """Anahtarsiz/hatali durumda yalniz gercek kelime eslesmeleri kalir (kurtarma adaylari elenir)."""
        return [r for r in results if r.get("lexical_hit", True)]

    def _candidate_clean(self, c: dict) -> bool:
        return all(_is_dlp_clean(str(c.get(k) or ""), self.vault_root)
                   for k in ("title", "heading_path", "snippet"))

    def rerank(self, query: str, results: list[dict]) -> list[dict]:
        if not results:
            return results
        if self.client is None or not _is_dlp_clean(query, self.vault_root):
            return self._local_only(results)
        head, tail = results[:RERANK_MAX_CANDIDATES], results[RERANK_MAX_CANDIDATES:]
        sendable = [c for c in head if self._candidate_clean(c)]
        if not sendable:
            return self._local_only(results)
        try:
            sem = self._semantic_scores(self.client, query, sendable)
        except (AdvisorError, OSError, ValueError, TypeError) as exc:
            _debug(f"rerank basarisiz ({type(exc).__name__}); yerel sonuclar kullaniliyor")
            return self._local_only(results)
        return self._blend(head, tail, sem)

    def _semantic_scores(self, client: GeminiDecisionsClient, query: str,
                         cands: list[dict]) -> dict[int, float]:
        """id(aday) -> 0..1 anlamsal puan; onbellekte olmayanlar icin TEK istek."""
        model = client.model
        scores: dict[int, float] = {}
        pending: dict[str, tuple[dict, str]] = {}
        for i, c in enumerate(cands):
            body = {"title": _clip(c.get("title")), "kind": _clip(c.get("kind")),
                    "heading": _clip(c.get("heading_path")), "snippet": _clip(c.get("snippet"))}
            key = make_cache_key(model, "rerank", {"query": query, "candidate": body})
            hit = self.cache.get(key) if self.cache else None
            if hit is not None and _num(hit.get("sem"), 0.0, 1.0) is not None:
                scores[id(c)] = float(hit["sem"])
                continue
            pending[f"c{i}"] = (c, key)
        if pending:
            input_obj = {"query": query, "candidates": {
                cid: {"title": _clip(c.get("title")), "kind": _clip(c.get("kind")),
                      "heading": _clip(c.get("heading_path")), "snippet": _clip(c.get("snippet"))}
                for cid, (c, _k) in pending.items()}}
            instructions = ("Rate how well this candidate note answers or governs the query. "
                            "Judge meaning, not shared words; the note may use different vocabulary.")
            questions = {cid: {"type": "score", "instructions": instructions, "criteria": RERANK_CRITERIA}
                         for cid in pending}
            answers = client.decide(input_obj, questions)
            for cid, (c, key) in pending.items():
                ans = answers.get(cid)
                if not isinstance(ans, dict):
                    continue
                raw = _num(ans.get("score"), 0.0, 3.0)
                if raw is None:
                    continue
                conf = _num(ans.get("confidence"), 0.0, 1.0, 1.0)
                sem = (raw / 3.0) * max(0.5, min(1.0, conf if conf is not None else 1.0))
                scores[id(c)] = sem
                if self.cache:
                    self.cache.put(key, {"sem": sem})
        return scores

    @staticmethod
    def _blend(head: list[dict], tail: list[dict], sem: dict[int, float]) -> list[dict]:
        top_local = max((float(c.get("score") or 0.0) for c in head if c.get("lexical_hit", True)), default=0.0)
        scored: list[tuple[float, int, dict]] = []
        for idx, c in enumerate(head):
            s = sem.get(id(c))
            lexical = c.get("lexical_hit", True)
            local = (float(c.get("score") or 0.0) / top_local) if top_local > 0 else 0.0
            if not lexical:
                if s is None or s < RESCUE_MIN_SEMANTIC:
                    continue  # alakasiz kurtarma adayi sizdirilmaz
                final = s
            elif s is None:
                final = 0.5 * local
            else:
                final = 0.4 * local + 0.6 * s
            c["local_score"] = float(c.get("score") or 0.0)
            if s is not None:
                c["semantic_score"] = round(s, 4)
            c["score"] = final
            scored.append((final, idx, c))
        scored.sort(key=lambda t: (-t[0], t[1]))
        return [c for _f, _i, c in scored] + [c for c in tail if c.get("lexical_hit", True)]


# ---------------------------------------------------------------- 2. yetenek: karar danismani


class GeminiDecisionAdvisor:
    """Karar taslagini onceki kararlara karsi (celiski/mukerrer) ve kesinlik acisindan degerlendirir."""

    is_external = True

    def __init__(self, client: GeminiDecisionsClient | None, cache: AdvisorCache | None = None,
                 vault_root=None):
        self.client = client
        self.cache = cache
        self.vault_root = vault_root

    def assess_decision(self, proposal: dict, priors: list[dict]) -> dict | None:
        if self.client is None:
            return None
        fields = ("title", "decision", "context", "alternatives", "rationale")
        prop = {k: _clip(proposal.get(k)) for k in fields}
        if not all(_is_dlp_clean(v, self.vault_root) for v in prop.values()):
            return None
        usable: list[dict] = []
        for p in list(priors or [])[:MAX_PRIORS]:
            item = {"doc_id": str(p.get("doc_id") or ""), "title": _clip(p.get("title")),
                    "decision": _clip(p.get("decision")), "rationale": _clip(p.get("rationale"))}
            if item["doc_id"] and all(_is_dlp_clean(item[k], self.vault_root)
                                      for k in ("title", "decision", "rationale")):
                usable.append(item)
        input_obj = {"proposal": prop, "existing_decisions": {
            f"p{i}": {k: it[k] for k in ("title", "decision", "rationale")} for i, it in enumerate(usable)}}
        questions: dict[str, dict] = {"commitment": {
            "type": "choice",
            "instructions": "Is the proposed decision firmly asserted, or only tentative/undecided "
                            "(e.g. 'maybe', 'we might', 'to be decided')?",
            "criteria": COMMITMENT_CRITERIA}}
        for i in range(len(usable)):
            questions[f"rel_p{i}"] = {
                "type": "choice",
                "instructions": f"How does the proposal relate to existing decision p{i}?",
                "criteria": RELATION_CRITERIA}
        key = make_cache_key(self.client.model, "assess", {"input": input_obj, "questions": questions})
        answers = self.cache.get(key) if self.cache else None
        if answers is None:
            try:
                answers = self.client.decide(input_obj, questions)
            except (AdvisorError, OSError, ValueError, TypeError) as exc:
                _debug(f"karar danismani basarisiz ({type(exc).__name__}); fail-open")
                return None
            if self.cache:
                self.cache.put(key, answers)
        return self._interpret(answers, usable)

    @staticmethod
    def _choice(answers: dict, qid: str) -> tuple[str | None, float]:
        ans = answers.get(qid)
        if not isinstance(ans, dict) or not isinstance(ans.get("choice"), str):
            return None, 0.0
        return ans["choice"], _num(ans.get("confidence"), 0.0, 1.0, 0.0) or 0.0

    def _interpret(self, answers: dict, usable: list[dict]) -> dict | None:
        warnings: list[str] = []
        existing: str | None = None
        choice, conf = self._choice(answers, "commitment")
        if choice == "tentative" and conf >= ADVISOR_CONFIDENCE:
            warnings.append(f"Karar henüz kesinleşmemiş görünüyor (güven {conf:.2f}).")
        for i, it in enumerate(usable):
            rel, rconf = self._choice(answers, f"rel_p{i}")
            if rconf < ADVISOR_CONFIDENCE:
                continue
            if rel == "duplicate":
                warnings.append(f"Mevcut karar ile mükerrer: {it['doc_id']} (güven {rconf:.2f}).")
            elif rel == "contradicts_or_supersedes":
                warnings.append(f"Mevcut kararla çelişiyor veya onun yerine geçiyor: {it['doc_id']} "
                                f"(güven {rconf:.2f}).")
            else:
                continue
            existing = existing or it["doc_id"]
        if not warnings:
            return None
        return {"warnings": warnings, "existing": existing,
                "message": "Danışman uyarısı: " + " ".join(warnings)
                           + " Eski kararın yerine geçiyorsa supersedes ver; yine de yazmak için force=true kullan."}


# ---------------------------------------------------------------- baglama


def build_for_profile(mode: str | None, vault_root, state_dir, profile_name: str, dlp_mode: str,
                      transport: Transport | None = None
                      ) -> tuple[GeminiSemanticReranker | None, GeminiDecisionAdvisor | None]:
    """(reranker, advisor) cifti. "off" veya anahtar yoksa (None, None); work/block -> ValueError."""
    adv = config.validate_advisor_mode(mode, profile_name, dlp_mode)
    if adv != "gemini":
        return None, None
    key = resolve_api_key()
    if key is None:
        _debug("API anahtari yok; danisman devre disi")
        return None, None
    model = str(load_advisor_settings().get("model") or DEFAULT_MODEL)
    client = GeminiDecisionsClient(key, model=model, transport=transport)
    cache = AdvisorCache(state_dir) if state_dir else None
    return (GeminiSemanticReranker(client, cache, vault_root),
            GeminiDecisionAdvisor(client, cache, vault_root))
