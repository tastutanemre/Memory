"""Pasaj duzeyinde FTS5 BM25 + [[WikiLink]] graf yakinligi + opsiyonel reranker kancasi."""

from __future__ import annotations

import re
from typing import Protocol, runtime_checkable

from .turkish import fold_tr, query_fts_expression, tokenize

PROJECT_BOOST = 1.25
KIND_BOOST = 1.15
GRAPH_BONUS = 0.10
GRAPH_TOP_N = 3
CANDIDATE_LIMIT = 300
RERANK_POOL_MIN = 24
RECOVERY_POOL = 24
RECOVERY_SCORE = 0.05


@runtime_checkable
class SemanticReranker(Protocol):
    """Yerel (Ollama/ONNX vb.) anlamsal yeniden siralama icin kanca; varsayilan NoOp."""

    def rerank(self, query: str, results: list[dict]) -> list[dict]: ...


class NoOpReranker:
    def rerank(self, query: str, results: list[dict]) -> list[dict]:
        return results


def _is_active(reranker) -> bool:
    return reranker is not None and not isinstance(reranker, NoOpReranker)


def _excerpt(body: str, query: str, width: int = 320) -> str:
    flat = re.sub(r"\s+", " ", body).strip()
    if len(flat) <= width:
        return flat
    low = flat.lower()
    pos = -1
    for tok in tokenize(query):
        i = low.find(tok.lower())
        if i != -1 and (pos == -1 or i < pos):
            pos = i
    if pos == -1:
        folded = fold_tr(flat)
        for tok in tokenize(query):
            i = folded.find(fold_tr(tok))
            if i != -1 and (pos == -1 or i < pos):
                pos = i
    start = max(0, (pos if pos != -1 else 0) - width // 4)
    snippet = flat[start : start + width]
    return ("…" if start > 0 else "") + snippet + ("…" if start + width < len(flat) else "")


def search_vault(
    conn,
    query: str,
    project: str | None = None,
    limit: int = 8,
    include_superseded: bool = False,
    include_archived: bool = False,
    include_untrusted: bool = False,
    kinds: list[str] | None = None,
    reranker: SemanticReranker | None = None,
) -> list[dict]:
    expr = query_fts_expression(query)
    if not expr:
        return []
    where = ["passages_fts MATCH ?"]
    params: list = [expr]
    if not include_untrusted:
        where.append("d.trust = 'trusted'")
    if not include_superseded:
        where.append("d.status != 'superseded'")
        where.append("d.doc_id NOT IN (SELECT older_doc_id FROM supersedes)")
    if not include_archived or project:
        # proje kapsaminda arsiv (tum projelerin kartlarini tasir) asla getirilmez
        where.append("d.status != 'archived'")
        where.append("d.doc_id NOT LIKE 'archive/%'")
    if project:
        where.append("(d.project = ? OR d.project IS NULL)")
        params.append(project)
    if kinds:
        where.append("d.kind IN (%s)" % ",".join("?" for _ in kinds))
        params.extend(kinds)
    sql = (
        "SELECT p.doc_id, p.heading_path, p.body, bm25(passages_fts, 8.0, 4.0, 2.0, 1.5) AS rank, "
        "d.title, d.kind, d.project, d.status, d.trust, d.updated_at "
        "FROM passages_fts JOIN passages p ON p.passage_id = passages_fts.rowid "
        "JOIN documents d ON d.doc_id = p.doc_id "
        "WHERE " + " AND ".join(where) + " ORDER BY rank LIMIT ?"
    )
    params.append(CANDIDATE_LIMIT)
    rows = conn.execute(sql, params).fetchall()
    scope_where, scope_params = where[1:], params[1:-1]

    best: dict[str, dict] = {}
    for r in rows:
        if r["doc_id"] in best:
            continue
        score = -float(r["rank"])
        if project and r["project"] == project:
            score *= PROJECT_BOOST
        if r["kind"] in ("decision", "rule"):
            score *= KIND_BOOST
        elif r["kind"] == "receipt":
            score *= 0.65
        best[r["doc_id"]] = {
            "doc_id": r["doc_id"],
            "title": r["title"],
            "kind": r["kind"],
            "project": r["project"],
            "status": r["status"],
            "trust": r["trust"],
            "heading_path": r["heading_path"],
            "snippet": _excerpt(r["body"], query),
            "score": score,
            "updated_at": r["updated_at"] or "",
        }
    results = sorted(best.values(), key=lambda x: x["updated_at"], reverse=True)
    results.sort(key=lambda x: -x["score"])

    # 1-hop WikiLink graf yakinligi: en iyi sonuclara dogrudan bagli komsulara baglam bonusu
    if results:
        top = results[:GRAPH_TOP_N]
        top_score = top[0]["score"]
        near: dict[str, set[str]] = {}
        for t in top:
            for row in conn.execute(
                "SELECT dst_doc_id AS n FROM links WHERE src_doc_id=? "
                "UNION SELECT src_doc_id FROM links WHERE dst_doc_id=?",
                (t["doc_id"], t["doc_id"]),
            ):
                near.setdefault(row[0], set()).add(t["doc_id"])
        for r in results:
            anchors = near.get(r["doc_id"], set()) - {r["doc_id"]}
            if anchors:
                r["score"] += GRAPH_BONUS * top_score
                r["graph_boost"] = True
        results.sort(key=lambda x: x["updated_at"], reverse=True)
        results.sort(key=lambda x: -x["score"])

    cap = max(1, int(limit))
    if not _is_active(reranker):
        results = results[:cap]
        results = (reranker or NoOpReranker()).rerank(query, results)
        for r in results:
            r["score"] = round(r["score"], 8)
        return results

    # Aktif anlamsal reranker: kesim reranker'dan SONRA yapilir; sifir/az kelime ortusmesinde
    # ayni kapsamdaki en guncel belgeler "lexical_hit": False aday olarak havuza eklenir.
    if len(results) < cap:
        results.extend(_recovery_candidates(conn, scope_where, scope_params, best, query))
    pool = results[: min(len(results), max(cap * 3, RERANK_POOL_MIN))]
    results = list(reranker.rerank(query, pool))[:cap]
    for r in results:
        r.pop("lexical_hit", None)
        r["score"] = round(r["score"], 8)
    return results


def _recovery_candidates(conn, scope_where: list[str], scope_params: list, best: dict[str, dict],
                         query: str) -> list[dict]:
    """FTS'te eslesmeyen ama kapsam filtrelerinden gecen en guncel belgeleri (ilk pasajla) dondurur."""
    sql = (
        "SELECT d.doc_id, d.title, d.kind, d.project, d.status, d.trust, d.updated_at, "
        "p.heading_path, p.body FROM documents d "
        "JOIN passages p ON p.doc_id = d.doc_id "
        "AND p.ord = (SELECT MIN(p2.ord) FROM passages p2 WHERE p2.doc_id = d.doc_id) "
        "WHERE " + (" AND ".join(scope_where) or "1=1") + " "
        "ORDER BY (d.kind = 'receipt'), d.updated_at DESC, d.doc_id LIMIT ?"
    )
    rows = conn.execute(sql, [*scope_params, RECOVERY_POOL + len(best)]).fetchall()
    out: list[dict] = []
    for r in rows:
        if r["doc_id"] in best:
            continue
        out.append({
            "doc_id": r["doc_id"],
            "title": r["title"],
            "kind": r["kind"],
            "project": r["project"],
            "status": r["status"],
            "trust": r["trust"],
            "heading_path": r["heading_path"],
            "snippet": _excerpt(r["body"], query),
            "score": RECOVERY_SCORE,
            "updated_at": r["updated_at"] or "",
            "lexical_hit": False,
        })
        if len(out) >= RECOVERY_POOL:
            break
    return out
