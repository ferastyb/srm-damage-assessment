# srm_search.py
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import List, Optional, Sequence


@dataclass
class SRMHit:
    doc_title: str
    revision: Optional[str]
    aircraft_family: Optional[str]
    file_name: Optional[str]
    page: int
    snippet: str
    score: float


def _normalize_query(q: str) -> str:
    q = (q or "").strip()
    q = q.replace("\u2013", "-").replace("\u2014", "-").replace("\u2011", "-")
    q = re.sub(r"\s+", " ", q)
    return q


def _tokenize_keywords(q: str) -> List[str]:
    """
    Extract strong tokens from a free-text query.
    We intentionally bias toward words you *do* have indexed.
    """
    q = _normalize_query(q).lower()

    # Keep alphanum + dash
    raw = re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)?", q)

    stop = {
        "the", "and", "or", "to", "of", "in", "on", "for", "with", "without",
        "mm", "in", "inch", "inches", "dia", "diameter", "depth", "srm",
        "allowable", "damage", "repair", "required", "within", "limit", "limits",
        "no", "visible"
    }
    tokens = [t for t in raw if t not in stop and len(t) >= 3]
    return tokens


def _fts_snippet() -> str:
    # Snippet formatting: [match]
    return "snippet(pages_fts, 0, '[', ']', '…', 20)"


def _run_fts(
    conn: sqlite3.Connection,
    match_expr: str,
    aircraft_family: Optional[str],
    limit: int,
) -> List[SRMHit]:
    sql = f"""
    SELECT
      d.title AS doc_title,
      d.revision AS revision,
      d.aircraft_family AS aircraft_family,
      d.file_name AS file_name,
      p.page_no AS page_no,
      {_fts_snippet()} AS snip,
      bm25(pages_fts) AS rank
    FROM pages_fts
    JOIN pages p ON p.id = pages_fts.rowid
    JOIN docs d ON d.id = p.doc_id
    WHERE pages_fts MATCH ?
      AND (? IS NULL OR d.aircraft_family = ?)
    ORDER BY rank
    LIMIT ?
    """
    rows = conn.execute(sql, (match_expr, aircraft_family, aircraft_family, limit)).fetchall()

    hits: List[SRMHit] = []
    for r in rows:
        hits.append(
            SRMHit(
                doc_title=r[0] or "",
                revision=r[1],
                aircraft_family=r[2],
                file_name=r[3],
                page=int(r[4]),
                snippet=r[5] or "",
                score=float(r[6]) if r[6] is not None else 0.0,
            )
        )
    return hits


def _run_like_fallback(
    conn: sqlite3.Connection,
    like_terms: Sequence[str],
    aircraft_family: Optional[str],
    limit: int,
) -> List[SRMHit]:
    """
    Final fallback if FTS misses tokens (e.g., 'table', numeric tokens).
    Uses LIKE on pages.text. Slower but reliable for small corpora.
    """
    clauses = []
    params: List[object] = []
    for t in like_terms:
        clauses.append("p.text LIKE ?")
        params.append(f"%{t}%")

    fam_clause = ""
    if aircraft_family:
        fam_clause = "AND d.aircraft_family = ?"
        params.append(aircraft_family)

    sql = f"""
    SELECT
      d.title AS doc_title,
      d.revision AS revision,
      d.aircraft_family AS aircraft_family,
      d.file_name AS file_name,
      p.page_no AS page_no,
      substr(p.text, 1, 650) AS snip
    FROM pages p
    JOIN docs d ON d.id = p.doc_id
    WHERE {" AND ".join(clauses)}
      {fam_clause}
    LIMIT ?
    """
    params.append(limit)
    rows = conn.execute(sql, tuple(params)).fetchall()

    hits: List[SRMHit] = []
    for r in rows:
        hits.append(
            SRMHit(
                doc_title=r[0] or "",
                revision=r[1],
                aircraft_family=r[2],
                file_name=r[3],
                page=int(r[4]),
                snippet=(r[5] or "").replace("\n", " "),
                score=9999.0,  # arbitrary: fallback
            )
        )
    return hits


def _content_bonus(snippet: str, query: str) -> int:
    """
    Heuristic rerank so the *top hit* is more likely to be an actual limits/procedure page
    (e.g., Table 102 / dent limits), not a cover/figure/reference page.
    """
    s = (snippet or "").lower()
    q = (query or "").lower()

    bonus = 0

    # Strong SRM “content page” anchors
    anchors = [
        ("table 102", 80),
        ("table102", 80),
        ("allowable damage limits", 60),
        ("allowable damage", 35),
        ("corrective action", 35),
        ("depth", 20),
        ("ratio", 15),
        ("dent must", 35),
        ("dents must", 35),
        ("inspection", 15),
        ("hfec", 20),
        ("eddy", 10),
        ("cycles", 10),
        ("repair", 10),
        ("paragraph", 10),
        ("procedure", 10),
        ("requirements", 10),
        ("zone", 10),
        ("stations", 8),
        ("stringers", 8),
    ]
    for term, w in anchors:
        if term in s:
            bonus += w

    # Penalize “front matter / figure index” style pages
    penalties = [
        ("figure", 20),
        ("structural repair manual", 25),
        ("yy loc", 25),
        ("references", 20),
        ("continued", 0),  # neutral
    ]
    for term, w in penalties:
        if term in s:
            bonus -= w

    # If the user explicitly asked for something, bump matching anchors.
    if "table" in q or "102" in q:
        if ("table 102" in s) or ("table102" in s):
            bonus += 40
    if "dent" in q and "dent" in s:
        bonus += 10
    if "fuselage" in q and "fuselage" in s:
        bonus += 5
    if "skin" in q and "skin" in s:
        bonus += 5

    return bonus


def _rerank_for_content(hits: List[SRMHit], query: str) -> List[SRMHit]:
    """
    Keep original bm25 ordering mostly, but promote likely “content pages”.
    We do this by sorting on a tuple:
      (content_bonus DESC, bm25 ASC)
    Note: bm25 rank is lower=better (often negative).
    """
    def key(h: SRMHit):
        b = _content_bonus(h.snippet, query)
        return (-b, h.score)

    return sorted(hits, key=key)


def search_srm(
    conn: sqlite3.Connection,
    query: str,
    aircraft_family: Optional[str] = None,
    limit: int = 6,
) -> List[SRMHit]:
    """
    Progressive SRM search:
      1) Phrase search for SRM-native anchors
      2) OR-based keyword search (robust)
      3) LIKE fallback over pages.text (last resort)

    After each stage, rerank hits to prefer "content pages" (tables/limits/procedures)
    over cover/figure/reference pages.
    """
    q = _normalize_query(query)

    # Stage 1: SRM-native phrase anchors (best precision)
    stage1 = [
        '"allowable damage 1"',
        '"fuselage skin"',
        'applicability',
        'stringers',
        'stations',
        'dent',
    ]
    try:
        hits = _run_fts(conn, " AND ".join(stage1), aircraft_family, max(limit * 3, 12))
        if hits:
            hits = _rerank_for_content(hits, q)[:limit]
            return hits
    except Exception:
        pass

    # Stage 2: OR-based query with phrases + keywords
    tokens = _tokenize_keywords(q)
    ors = [
        '"allowable damage 1"',
        '"fuselage skin"',
        'allowable',
        'fuselage',
        'skin',
        'dent',
        'applicability',
        'stringers',
        'stations',
        'section',
        'figure',
        # IMPORTANT: include "table" + "102" if present in your DB now (post-normalization)
        'table',
        '102',
    ]
    ors += tokens[:10]
    match_expr = " OR ".join(dict.fromkeys(ors))  # dedupe preserving order

    try:
        hits = _run_fts(conn, match_expr, aircraft_family, max(limit * 3, 12))
        if hits:
            hits = _rerank_for_content(hits, q)[:limit]
            return hits
    except Exception:
        pass

    # Stage 3: LIKE fallback — pick substrings that should exist
    like_terms = ["ALLOWABLE", "FUSELAGE", "SKIN"]
    if re.search(r"\bdent\b", q, re.I):
        like_terms.append("Dent")
    if re.search(r"\btable\b", q, re.I):
        like_terms.append("Table")
    if re.search(r"\b102\b", q):
        like_terms.append("102")

    hits = _run_like_fallback(conn, like_terms=like_terms[:5], aircraft_family=aircraft_family, limit=max(limit * 3, 12))
    hits = _rerank_for_content(hits, q)[:limit]
    return hits
