# srm_search.py
# SQLite FTS5 search helper for SRM index (srm_index.db)
#
# Expected schema tables:
#   - docs(id, aircraft_family, revision, title, file_name, file_hash, created_utc, ...)
#   - pages(id, doc_id, page_no, text)
#   - pages_fts(text) USING fts5(content='pages', content_rowid='id', ...)
#
# NOTE: This version does NOT reference docs.base_url (fixes: "no such column: d.base_url")

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional


def open_srm_index(db_path: str | Path) -> sqlite3.Connection:
    """
    Open SRM index DB in read-only mode where possible.

    On Streamlit Cloud, pass a relative path like "srm_index.db" if it's in repo root.
    """
    p = Path(db_path)
    if not p.exists():
        raise FileNotFoundError(f"SRM index DB not found at: {p.resolve()}")

    # Use SQLite read-only URI for safety (works when file is accessible)
    uri = f"file:{p.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def _normalize_query(q: str) -> str:
    """
    Convert free-text into a reasonable FTS5 query:
      - replace commas with spaces
      - collapse whitespace
      - keep common punctuation out
    """
    q = (q or "").strip()
    q = q.replace(",", " ")
    q = re.sub(r"\s+", " ", q).strip()
    # Remove characters that often break MATCH queries
    q = re.sub(r"[^\w\s\-\./:]", " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    return q


def search_srm(
    conn: sqlite3.Connection,
    query: str,
    aircraft_family: Optional[str] = None,
    limit: int = 6,
) -> List[Dict[str, Any]]:
    """
    Search SRM index using SQLite FTS5.

    Returns list of hits:
      [
        {
          "aircraft_family": "B787",
          "revision": "78",
          "title": "SRM_B787_REV78",
          "file_name": "SRM_B787_REV78.pdf",
          "page_no": 128,
          "snippet": "...highlighted...",
        },
        ...
      ]

    Raises sqlite3.Error if DB is invalid/unreadable.
    """
    q = _normalize_query(query)
    if not q:
        return []

    fam = (aircraft_family or "").upper().strip() or None
    params: Dict[str, Any] = {"fts": q, "limit": int(limit)}
    fam_clause = ""
    if fam:
        fam_clause = "AND d.aircraft_family = :family"
        params["family"] = fam

    # Use snippet() for a short excerpt; bm25() for relevance ordering.
    sql = f"""
    SELECT
      d.aircraft_family,
      d.revision,
      d.title,
      d.file_name,
      p.page_no,
      snippet(pages_fts, 0, '[', ']', '…', 16) AS snippet
    FROM pages_fts
    JOIN pages p ON p.id = pages_fts.rowid
    JOIN docs d ON d.id = p.doc_id
    WHERE pages_fts MATCH :fts
      {fam_clause}
    ORDER BY bm25(pages_fts)
    LIMIT :limit
    """

    rows = conn.execute(sql, params).fetchall()

    hits: List[Dict[str, Any]] = []
    for r in rows:
        hits.append(
            {
                "aircraft_family": r["aircraft_family"],
                "revision": r["revision"],
                "title": r["title"],
                "file_name": r["file_name"],
                "page_no": r["page_no"],
                "snippet": r["snippet"],
            }
        )
    return hits


def safe_search_srm(
    conn: Optional[sqlite3.Connection],
    query: str,
    aircraft_family: Optional[str] = None,
    limit: int = 6,
) -> tuple[List[Dict[str, Any]], Optional[str]]:
    """
    Wrapper that never throws:
      returns (hits, error_message)
    """
    if conn is None:
        return ([], "SRM index connection is not available.")
    try:
        return (search_srm(conn, query, aircraft_family=aircraft_family, limit=limit), None)
    except sqlite3.Error as e:
        return ([], f"SQLite error: {e}")
    except Exception as e:
        return ([], f"Error: {e}")


def guess_srm_db_path() -> Optional[str]:
    """
    Convenience helper:
    - checks env var SRM_INDEX_DB
    - then checks common local filenames in cwd

    Returns path string or None.
    """
    env = os.getenv("SRM_INDEX_DB", "").strip()
    if env and Path(env).exists():
        return env

    for name in ("srm_index.db", "data/srm_index.db"):
        if Path(name).exists():
            return name

    return None
