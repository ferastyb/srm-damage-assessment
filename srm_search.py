# srm_search.py
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class SRMHit:
    aircraft_family: Optional[str]
    doc_id: int
    doc_title: str
    page: int
    snippet: str
    file_path: Optional[str] = None
    score: Optional[float] = None


def connect_srm(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _fts_safe_query(raw: str) -> str:
    """
    Turn a free-text query into a safe FTS5 MATCH expression.
    - splits into tokens
    - quotes tokens containing punctuation (e.g. S-10L)
    - joins with AND for precision
    """
    raw = (raw or "").strip()
    if not raw:
        return ""

    raw = re.sub(r"\s+", " ", raw)
    tokens = raw.split(" ")

    safe = []
    for t in tokens:
        t = t.strip()
        if not t:
            continue

        # quote anything with punctuation so MATCH parser doesn't treat '-' specially
        if re.search(r"[^A-Za-z0-9_]", t):
            t = t.replace('"', "")
            safe.append(f'"{t}"')
        else:
            safe.append(t)

    return " AND ".join(safe)


def build_query_from_context(ctx: dict) -> str:
    """
    Optional helper: build a search string from parsed context.
    ctx keys can be: aircraft_family, ata, structure_zone, side, sta, stringer, damage_type,
                     dent_diameter_mm, dent_depth_mm, crack_present
    """
    parts = []
    fam = (ctx.get("aircraft_family") or "").strip()
    if fam:
        parts.append(fam)

    # prioritize structure / location tokens
    for k in ("structure_zone", "side", "sta", "stringer", "ata"):
        v = ctx.get(k)
        if v:
            parts.append(str(v))

    damage_type = (ctx.get("damage_type") or "").strip()
    if damage_type:
        parts.append(damage_type)

    dia = ctx.get("dent_diameter_mm")
    dep = ctx.get("dent_depth_mm")
    if dia:
        parts.append(f"{dia}mm")
        parts.append("dia")
    if dep:
        parts.append(f"{dep}mm")
        parts.append("depth")

    crack = ctx.get("crack_present")
    if crack is True:
        parts.append("crack")
    elif crack is False:
        parts.append("no crack")

    parts += ["SRM", "allowable", "damage", "repair"]
    return " ".join(parts)


def search_srm(
    conn: sqlite3.Connection,
    query: str,
    aircraft_family: Optional[str] = None,
    limit: int = 6,
) -> List[SRMHit]:
    """
    Searches using pages_fts MATCH, joins to pages and docs.
    Requires tables: pages_fts, pages, docs (as in your DB).
    """
    query = (query or "").strip()
    if not query:
        return []

    fts_q = _fts_safe_query(query)

    # We assume pages_fts is an FTS5 table built over pages (external content).
    # In that case, the FTS table rowid corresponds to pages.id.
    # We'll join pages.id = pages_fts.rowid
    sql = """
    SELECT
        d.aircraft_family AS aircraft_family,
        d.id              AS doc_id,
        d.title           AS doc_title,
        p.page_no         AS page_no,
        snippet(pages_fts, 0, '[', ']', '…', 18) AS snippet,
        d.file_path       AS file_path,
        bm25(pages_fts)   AS score
    FROM pages_fts
    JOIN pages p ON p.id = pages_fts.rowid
    JOIN docs  d ON d.id = p.doc_id
    WHERE pages_fts MATCH ?
      AND (? IS NULL OR d.aircraft_family = ?)
    ORDER BY score ASC
    LIMIT ?
    """

    rows = conn.execute(sql, (fts_q, aircraft_family, aircraft_family, limit)).fetchall()

    hits: List[SRMHit] = []
    for r in rows:
        hits.append(
            SRMHit(
                aircraft_family=r["aircraft_family"],
                doc_id=int(r["doc_id"]),
                doc_title=(r["doc_title"] or "SRM Document").strip(),
                page=int(r["page_no"] or 0),
                snippet=(r["snippet"] or "").strip(),
                file_path=r["file_path"],
                score=float(r["score"]) if r["score"] is not None else None,
            )
        )
    return hits
