# srm_search.py
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class SRMHit:
    aircraft_family: str
    revision: str
    title: str
    file_name: str
    base_url: Optional[str]
    page_no: int
    snippet: str
    rank: float


def connect_index(db_path: str = "srm_index.db") -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _make_snippet(text: str, q: str, max_len: int = 240) -> str:
    t = (text or "").replace("\n", " ").strip()
    if not t:
        return ""
    # naive snippet: center around first occurrence of a query token
    tokens = [tok for tok in q.lower().split() if len(tok) > 2]
    pos = None
    low = t.lower()
    for tok in tokens:
        i = low.find(tok)
        if i != -1:
            pos = i
            break
    if pos is None:
        return t[:max_len] + ("…" if len(t) > max_len else "")
    start = max(0, pos - max_len // 3)
    end = min(len(t), start + max_len)
    snip = t[start:end]
    if start > 0:
        snip = "…" + snip
    if end < len(t):
        snip = snip + "…"
    return snip


def search_srm(
    conn: sqlite3.Connection,
    query: str,
    aircraft_family: Optional[str] = None,
    limit: int = 8,
) -> List[SRMHit]:
    """
    Uses SQLite FTS5 BM25 ranking.
    If aircraft_family provided, filters docs to that family.
    """
    if not query.strip():
        return []

    params: List[Any] = [query]

    family_filter_sql = ""
    if aircraft_family:
        family_filter_sql = " AND d.aircraft_family = ? "
        params.append(aircraft_family)

    params.append(int(limit))

    rows = conn.execute(
        f"""
        SELECT
          d.aircraft_family,
          d.revision,
          d.title,
          d.file_name,
          d.base_url,
          p.page_no,
          p.text,
          bm25(pages_fts) AS rank
        FROM pages_fts
        JOIN pages p ON p.id = pages_fts.rowid
        JOIN docs d ON d.id = p.doc_id
        WHERE pages_fts MATCH ?
          {family_filter_sql}
        ORDER BY rank
        LIMIT ?
        """,
        params,
    ).fetchall()

    hits: List[SRMHit] = []
    for r in rows:
        hits.append(
            SRMHit(
                aircraft_family=r["aircraft_family"],
                revision=r["revision"],
                title=r["title"],
                file_name=r["file_name"],
                base_url=r["base_url"],
                page_no=int(r["page_no"]),
                snippet=_make_snippet(r["text"], query),
                rank=float(r["rank"]) if r["rank"] is not None else 0.0,
            )
        )
    return hits


def build_query_from_context(ctx: Dict[str, Any]) -> str:
    """
    Turn the structured context into a strong SRM search query.
    Keeps it keyword-based (works great with FTS5).
    """
    parts: List[str] = []

    loc = ctx.get("location", {})
    dmg = ctx.get("damage", {})

    zone = loc.get("zone")
    if zone:
        parts.append(str(zone))

    # Common SRM keywords
    parts += ["SRM", "allowable", "limits", "repair", "procedure"]

    dtype = dmg.get("type")
    if dtype:
        parts.append(str(dtype))

    structure = dmg.get("structure")
    if structure:
        parts.append(str(structure))

    sta = loc.get("sta")
    if sta:
        parts += ["STA", str(sta)]

    stringer = loc.get("stringer_num")
    if stringer:
        parts += ["stringer", f"S{stringer}"]

    # Damage dimensions (helps locate tables sometimes)
    dia = dmg.get("diameter_mm")
    dep = dmg.get("depth_mm")
    if dia:
        parts += [str(int(dia)) + "mm", "diameter"]
    if dep:
        parts += [str(dep) + "mm", "depth"]

    if dmg.get("visible_crack") is True:
        parts += ["crack"]
    else:
        parts += ["no crack", "visual inspection"]

    return " ".join(parts)
