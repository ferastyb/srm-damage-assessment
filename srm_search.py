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
    revision: Optional[str]
    file_name: Optional[str]
    page: int
    snippet: str
    score: Optional[float] = None


def connect_srm(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _fts_safe_query(raw: str) -> str:
    """
    Convert free text into a conservative FTS5 MATCH query:
    - split into tokens
    - quote tokens containing punctuation (e.g. S-10L)
    - join with AND
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

        # Quote punctuation tokens so '-' etc. doesn't break MATCH parsing
        if re.search(r"[^A-Za-z0-9_]", t):
            t = t.replace('"', "")
            safe.append(f'"{t}"')
        else:
            safe.append(t)

    return " AND ".join(safe)


def build_query_from_context(ctx: dict) -> str:
    """
    Build a search string from parsed damage context.
    Safe to pass into search_srm(); it will be token-quoted for FTS.
    """
    parts = []
    fam = (ctx.get("aircraft_family") or "").strip()
    if fam:
        parts.append(fam)

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
        parts += [f"{dia}mm", "dia"]
    if dep:
        parts += [f"{dep}mm", "depth"]

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
    Search SRM index DB using FTS table pages_fts.
    Assumes:
      - pages_fts is built over pages.text
      - pages_fts.rowid == pages.id
      - pages.doc_id links to docs.id
    """
    query = (query or "").strip()
    if not query:
        return []

    fts_q = _fts_safe_query(query)

    sql = """
    SELECT
        d.aircraft_family AS aircraft_family,
        d.id              AS doc_id,
        d.title           AS doc_title,
        d.revision        AS revision,
        d.file_name       AS file_name,
        p.page_no         AS page_no,
        snippet(pages_fts, 0, '[', ']', '…', 18) AS snippet,
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
                revision=r["revision"],
                file_name=r["file_name"],
                page=int(r["page_no"] or 0),
                snippet=(r["snippet"] or "").strip(),
                score=float(r["score"]) if r["score"] is not None else None,
            )
        )
    return hits
