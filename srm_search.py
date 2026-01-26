# srm_search.py
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple


@dataclass
class SRMHit:
    doc_title: str
    revision: Optional[str]
    aircraft_family: Optional[str]
    file_name: Optional[str]
    page: int                   # PDF page index in your DB (page_no)
    snippet: str
    score: float
    printed_page: Optional[int] = None  # schema-safe: not stored in current DB


def _normalize_query(q: str) -> str:
    q = (q or "").strip()
    q = q.replace("\u2013", "-").replace("\u2014", "-").replace("\u2011", "-")
    q = re.sub(r"\s+", " ", q)
    return q


def _tokenize_keywords(q: str) -> List[str]:
    q = _normalize_query(q).lower()
    raw = re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)?", q)

    stop = {
        "the", "and", "or", "to", "of", "in", "on", "for", "with", "without",
        "mm", "in", "inch", "inches", "dia", "diameter", "depth", "srm",
        "allowable", "damage", "repair", "required", "within", "limit",
        "limits", "no", "visible",
    }
    return [t for t in raw if t not in stop and len(t) >= 3]


def _fts_snippet() -> str:
    return "snippet(pages_fts, 0, '[', ']', '…', 16)"


def _run_fts(
    conn: sqlite3.Connection,
    match_expr: str,
    aircraft_family: Optional[str],
    limit: int,
) -> List[SRMHit]:
    """
    Schema-safe FTS query:
    - pages table contains: id, doc_id, page_no, text
    - NO printed_page column in your current DB
    """
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
                printed_page=None,
            )
        )
    return hits


def _run_like_fallback(
    conn: sqlite3.Connection,
    like_terms: Sequence[str],
    aircraft_family: Optional[str],
    limit: int,
) -> List[SRMHit]:
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
      substr(p.text, 1, 400) AS snip
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
                score=9999.0,
                printed_page=None,
            )
        )
    return hits


def search_srm(
    conn: sqlite3.Connection,
    query: str,
    aircraft_family: Optional[str] = None,
    limit: int = 6,
) -> List[SRMHit]:
    q = _normalize_query(query)

    stage1 = [
        '"allowable damage 1"',
        '"fuselage skin"',
        "applicability",
        "stringers",
        "stations",
        "dent",
    ]
    try:
        hits = _run_fts(conn, " AND ".join(stage1), aircraft_family, limit)
        if hits:
            return hits
    except Exception:
        pass

    tokens = _tokenize_keywords(q)
    ors = [
        '"allowable damage 1"',
        '"fuselage skin"',
        "allowable",
        "fuselage",
        "skin",
        "dent",
        "applicability",
        "stringers",
        "stations",
        "section",
        "figure",
    ]
    ors += tokens[:8]
    match_expr = " OR ".join(dict.fromkeys(ors))

    try:
        hits = _run_fts(conn, match_expr, aircraft_family, limit)
        if hits:
            return hits
    except Exception:
        pass

    like_terms = ["ALLOWABLE", "FUSELAGE", "SKIN"]
    if re.search(r"\bdent\b", q, re.I):
        like_terms.append("Dent")
    if re.search(r"\bapplicability\b", q, re.I):
        like_terms.append("Applicability")

    return _run_like_fallback(conn, like_terms=like_terms[:4], aircraft_family=aircraft_family, limit=limit)
