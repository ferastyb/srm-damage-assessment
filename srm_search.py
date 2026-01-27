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
    page: int                 # PDF page number (page_no)
    printed_page: Optional[int]  # SRM printed page number if detected (e.g., 108)
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

    Notes:
    - We bias toward SRM-native anchors.
    - We strip common filler terms and measurement tokens.
    """
    q = _normalize_query(q).lower()

    # Keep alphanum + dash
    raw = re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)?", q)

    stop = {
        "the", "and", "or", "to", "of", "in", "on", "for", "with", "without",
        "mm", "in", "inch", "inches", "dia", "diameter", "depth", "srm",
        "allowable", "damage", "repair", "required", "within", "limit", "limits",
        "no", "visible",
    }

    tokens = [t for t in raw if t not in stop and len(t) >= 3]
    return tokens


def _fts_snippet() -> str:
    # Snippet formatting: [match]
    return "snippet(pages_fts, 0, '[', ']', '…', 16)"


def _has_column(conn: sqlite3.Connection, table: str, col: str) -> bool:
    try:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        return col in cols
    except Exception:
        return False


def _run_fts(
    conn: sqlite3.Connection,
    match_expr: str,
    aircraft_family: Optional[str],
    limit: int,
) -> List[SRMHit]:
    """
    FTS5 query returning SRM hits.
    Supports optional pages.printed_page column if present in schema.
    """
    has_printed = _has_column(conn, "pages", "printed_page")
    printed_select = "p.printed_page AS printed_page" if has_printed else "NULL AS printed_page"

    sql = f"""
    SELECT
      d.title AS doc_title,
      d.revision AS revision,
      d.aircraft_family AS aircraft_family,
      d.file_name AS file_name,
      p.page_no AS page_no,
      {printed_select},
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
        # Row mapping:
        # 0 doc_title
        # 1 revision
        # 2 aircraft_family
        # 3 file_name
        # 4 page_no
        # 5 printed_page (or NULL)
        # 6 snippet
        # 7 rank
        hits.append(
            SRMHit(
                doc_title=r[0] or "",
                revision=r[1],
                aircraft_family=r[2],
                file_name=r[3],
                page=int(r[4]),
                printed_page=(int(r[5]) if r[5] is not None else None),
                snippet=r[6] or "",
                score=float(r[7]) if r[7] is not None else 0.0,
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
    Final fallback if FTS misses tokens (e.g., certain numerics or formatting artifacts).
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

    # printed_page may or may not exist; keep it optional
    has_printed = _has_column(conn, "pages", "printed_page")
    printed_select = "p.printed_page AS printed_page" if has_printed else "NULL AS printed_page"

    sql = f"""
    SELECT
      d.title AS doc_title,
      d.revision AS revision,
      d.aircraft_family AS aircraft_family,
      d.file_name AS file_name,
      p.page_no AS page_no,
      {printed_select},
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
                printed_page=(int(r[5]) if r[5] is not None else None),
                snippet=(r[6] or "").replace("\n", " "),
                score=9999.0,  # arbitrary: fallback ranking
            )
        )
    return hits


def search_srm(
    conn: sqlite3.Connection,
    query: str,
    aircraft_family: Optional[str] = None,
    limit: int = 6,
) -> List[SRMHit]:
    """
    Progressive SRM search:
      1) Phrase search for SRM-native anchors (best precision)
      2) OR-based keyword search (robust)
      3) LIKE fallback over pages.text (last resort)
    """
    q = _normalize_query(query)

    # Stage 1: SRM-native phrase anchors (precision)
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

    # Stage 2: OR-based query with phrases + keywords (recall)
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
    match_expr = " OR ".join(dict.fromkeys(ors))  # dedupe preserving order

    try:
        hits = _run_fts(conn, match_expr, aircraft_family, limit)
        if hits:
            return hits
    except Exception:
        pass

    # Stage 3: LIKE fallback — choose strong substrings likely present
    like_terms = ["ALLOWABLE", "FUSELAGE", "SKIN"]
    if re.search(r"\bdent\b", q, re.I):
        like_terms.append("Dent")
    if re.search(r"\bapplicability\b", q, re.I):
        like_terms.append("Applicability")

    return _run_like_fallback(conn, like_terms=like_terms[:4], aircraft_family=aircraft_family, limit=limit)
