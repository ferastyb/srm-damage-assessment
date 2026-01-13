# srm_search.py
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass
class SRMHit:
    doc_title: str
    page: Optional[int]
    snippet: str
    source: Optional[str] = None
    url: Optional[str] = None
    score: Optional[float] = None


def connect_srm(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _find_fts_table(conn: sqlite3.Connection) -> Optional[str]:
    """
    Find the first FTS5 virtual table in the DB (if any).
    """
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' AND sql LIKE 'CREATE VIRTUAL TABLE%fts5%';"
    ).fetchall()
    for r in rows:
        if r["name"]:
            return r["name"]
    return None


def _fts_safe_query(q: str) -> str:
    """
    Convert raw text into a conservative FTS query:
    - Tokenize
    - Quote tokens that contain punctuation (e.g., S-10L)
    - Join with AND
    """
    q = q.strip()
    if not q:
        return ""

    # Normalize whitespace and remove common “units noise”
    q = q.replace("mm", " ").replace("MM", " ")
    q = re.sub(r"\s+", " ", q)

    tokens = q.split(" ")
    safe_tokens = []
    for t in tokens:
        t = t.strip()
        if not t:
            continue

        # FTS operators / punctuation can break parsing; quote anything non-alnum-ish
        if re.search(r"[^A-Za-z0-9_]", t):
            t = t.replace('"', "")  # remove quotes inside token
            safe_tokens.append(f'"{t}"')
        else:
            safe_tokens.append(t)

    # Join as AND to narrow results
    return " AND ".join(safe_tokens)


def search_srm(
    conn: sqlite3.Connection,
    query: str,
    aircraft_family: Optional[str] = None,
    limit: int = 6,
) -> List[SRMHit]:
    """
    Search SRM index DB.

    Expected DB shapes supported:
      - FTS5 virtual table exists: we use MATCH with parameters.
      - Otherwise, try a generic LIKE search on a table that looks like chunks/pages.

    NOTE: This function is intentionally defensive so it won't explode if the DB schema changes.
    """
    query = (query or "").strip()
    if not query:
        return []

    fts_table = _find_fts_table(conn)

    # ---------- 1) FTS5 path ----------
    if fts_table:
        # Try to infer column names commonly used in our builder(s)
        # We assume the FTS table has a "content" or "text" column, but FTS uses the table name directly in MATCH.
        fts_q = _fts_safe_query(query)

        # Optional family filter if the FTS table has a family column
        has_family = False
        try:
            cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({fts_table})").fetchall()]
            has_family = "aircraft_family" in cols or "family" in cols
        except Exception:
            has_family = False

        family_col = "aircraft_family" if has_family and "aircraft_family" in cols else ("family" if has_family else None)

        # Build SQL with parameters only
        if family_col and aircraft_family:
            sql = f"""
                SELECT
                    COALESCE(doc_title, title, document, '') AS doc_title,
                    COALESCE(page, page_no, NULL) AS page,
                    COALESCE(snippet({fts_table}, 0, '[', ']', '…', 18), '') AS snippet,
                    COALESCE(source, path, NULL) AS source,
                    COALESCE(url, link, NULL) AS url,
                    bm25({fts_table}) AS score
                FROM {fts_table}
                WHERE {fts_table} MATCH ?
                  AND {family_col} = ?
                ORDER BY score
                LIMIT ?
            """
            rows = conn.execute(sql, (fts_q, aircraft_family, limit)).fetchall()
        else:
            sql = f"""
                SELECT
                    COALESCE(doc_title, title, document, '') AS doc_title,
                    COALESCE(page, page_no, NULL) AS page,
                    COALESCE(snippet({fts_table}, 0, '[', ']', '…', 18), '') AS snippet,
                    COALESCE(source, path, NULL) AS source,
                    COALESCE(url, link, NULL) AS url,
                    bm25({fts_table}) AS score
                FROM {fts_table}
                WHERE {fts_table} MATCH ?
                ORDER BY score
                LIMIT ?
            """
            rows = conn.execute(sql, (fts_q, limit)).fetchall()

        hits: List[SRMHit] = []
        for r in rows:
            hits.append(
                SRMHit(
                    doc_title=(r["doc_title"] or "").strip() or "SRM Document",
                    page=r["page"],
                    snippet=(r["snippet"] or "").strip(),
                    source=r["source"],
                    url=r["url"],
                    score=float(r["score"]) if r["score"] is not None else None,
                )
            )
        return hits

    # ---------- 2) Fallback LIKE path ----------
    # Try a few common table names and column patterns
    candidates: List[Tuple[str, str]] = [
        ("srm_chunks", "text"),
        ("chunks", "text"),
        ("srm_pages", "text"),
        ("pages", "text"),
    ]

    for table, text_col in candidates:
        if not _table_exists(conn, table):
            continue

        # check column exists
        cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if text_col not in cols:
            continue

        # Optional family filtering if column exists
        fam_col = None
        for c in ("aircraft_family", "family"):
            if c in cols:
                fam_col = c
                break

        like_q = f"%{query}%"

        if fam_col and aircraft_family:
            sql = f"""
                SELECT
                    COALESCE(doc_title, title, document, '') AS doc_title,
                    COALESCE(page, page_no, NULL) AS page,
                    substr({text_col}, 1, 320) AS snippet,
                    COALESCE(source, path, NULL) AS source,
                    COALESCE(url, link, NULL) AS url
                FROM {table}
                WHERE {text_col} LIKE ?
                  AND {fam_col} = ?
                LIMIT ?
            """
            rows = conn.execute(sql, (like_q, aircraft_family, limit)).fetchall()
        else:
            sql = f"""
                SELECT
                    COALESCE(doc_title, title, document, '') AS doc_title,
                    COALESCE(page, page_no, NULL) AS page,
                    substr({text_col}, 1, 320) AS snippet,
                    COALESCE(source, path, NULL) AS source,
                    COALESCE(url, link, NULL) AS url
                FROM {table}
                WHERE {text_col} LIKE ?
                LIMIT ?
            """
            rows = conn.execute(sql, (like_q, limit)).fetchall()

        hits: List[SRMHit] = []
        for r in rows:
            hits.append(
                SRMHit(
                    doc_title=(r["doc_title"] or "").strip() or "SRM Document",
                    page=r["page"],
                    snippet=(r["snippet"] or "").strip(),
                    source=r["source"],
                    url=r["url"],
                    score=None,
                )
            )
        return hits

    # No recognized tables
    return []
