#!/usr/bin/env python3
"""
srm_indexer.py

Build a fast SRM search index (SQLite + FTS5) from a folder of SRM PDFs.

- Index granularity: page-level (simple, robust, and good enough to start)
- Output: srm_index.db (can be committed if not too large)

Recommended folder structure:
  srm_library/
    B787/
      SRM_B787_REVXX.pdf
    B767/
      SRM_B767_REVYY.pdf

Usage:
  python3 srm_indexer.py --in srm_library --out srm_index.db

Optional:
  --base-url-map base_urls.json
    where base_urls.json can be like:
    {
      "B787": "https://your-private-host/srm/B787/",
      "B767": "https://your-private-host/srm/B767/"
    }
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

# PyPDF2 is lightweight; if you prefer, we can switch to pypdf.
try:
    from PyPDF2 import PdfReader
except Exception as e:
    raise SystemExit(
        "Missing dependency PyPDF2. Install with: pip install PyPDF2"
    ) from e


@dataclass
class DocMeta:
    aircraft_family: str
    revision: str
    title: str
    file_name: str
    file_path: str
    base_url: Optional[str]


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS docs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          aircraft_family TEXT NOT NULL,
          revision TEXT NOT NULL,
          title TEXT NOT NULL,
          file_name TEXT NOT NULL,
          file_hash TEXT NOT NULL,
          base_url TEXT,
          created_utc TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_docs_unique
          ON docs(aircraft_family, revision, file_hash);

        CREATE TABLE IF NOT EXISTS pages (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          doc_id INTEGER NOT NULL,
          page_no INTEGER NOT NULL,            -- 1-based page number
          text TEXT NOT NULL,
          FOREIGN KEY(doc_id) REFERENCES docs(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_pages_doc_page
          ON pages(doc_id, page_no);

        -- FTS5 virtual table for search
        CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts
        USING fts5(
          text,
          content='pages',
          content_rowid='id',
          tokenize='porter'
        );
        """
    )
    conn.commit()


def _rebuild_fts(conn: sqlite3.Connection) -> None:
    # Rebuild FTS index from pages content table
    conn.execute("INSERT INTO pages_fts(pages_fts) VALUES ('rebuild');")
    conn.commit()


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _infer_aircraft_family_from_path(pdf_path: Path) -> str:
    # e.g., srm_library/B787/xxx.pdf -> B787
    parent = pdf_path.parent.name.upper().strip()
    if parent:
        return parent
    return "UNKNOWN"


def _infer_revision_from_filename(name: str) -> str:
    # Attempt to find patterns like REVxx / Rev_78 / R78 / etc.
    n = name.upper()
    m = re.search(r"\bREV[\s_-]*([A-Z0-9]+)\b", n)
    if m:
        return m.group(1)
    # fallback: "UNKNOWN"
    return "UNKNOWN"


def _iter_pdfs(root: Path) -> Iterator[Path]:
    for p in root.rglob("*.pdf"):
        if p.is_file():
            yield p


def _extract_pages_text(pdf_path: Path, max_pages: Optional[int] = None) -> List[str]:
    reader = PdfReader(str(pdf_path))
    texts: List[str] = []
    n_pages = len(reader.pages)
    if max_pages is not None:
        n_pages = min(n_pages, max_pages)

    for i in range(n_pages):
        page = reader.pages[i]
        raw = page.extract_text() or ""
        # light cleanup
        raw = raw.replace("\x00", " ").strip()
        texts.append(raw)
    return texts


def _doc_exists(conn: sqlite3.Connection, family: str, revision: str, file_hash: str) -> Optional[int]:
    row = conn.execute(
        "SELECT id FROM docs WHERE aircraft_family=? AND revision=? AND file_hash=?",
        (family, revision, file_hash),
    ).fetchone()
    return int(row[0]) if row else None


def _insert_doc(conn: sqlite3.Connection, meta: DocMeta, file_hash: str) -> int:
    cur = conn.execute(
        """
        INSERT INTO docs (aircraft_family, revision, title, file_name, file_hash, base_url)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (meta.aircraft_family, meta.revision, meta.title, meta.file_name, file_hash, meta.base_url),
    )
    return int(cur.lastrowid)


def _insert_pages(conn: sqlite3.Connection, doc_id: int, page_texts: List[str]) -> None:
    conn.executemany(
        "INSERT INTO pages (doc_id, page_no, text) VALUES (?, ?, ?)",
        [(doc_id, idx + 1, txt) for idx, txt in enumerate(page_texts)],
    )
    conn.commit()


def main() -> None:
    ap = argparse.ArgumentParser(description="Build SRM search index (SQLite FTS5) from PDFs.")
    ap.add_argument("--in", dest="inp", required=True, help="Input root folder containing SRM PDFs")
    ap.add_argument("--out", dest="out", default="srm_index.db", help="Output SQLite DB path")
    ap.add_argument("--max-pages", type=int, default=None, help="Limit pages per PDF (debug)")
    ap.add_argument("--base-url-map", default=None, help="JSON mapping aircraft_family->base URL for PDFs")
    ap.add_argument("--wipe", action="store_true", help="Wipe existing DB content before indexing")
    args = ap.parse_args()

    root = Path(args.inp).expanduser().resolve()
    out_db = Path(args.out).expanduser().resolve()

    if not root.exists():
        raise SystemExit(f"Input folder not found: {root}")

    base_url_map: Dict[str, str] = {}
    if args.base_url_map:
        import json
        base_url_map = json.loads(Path(args.base_url_map).read_text(encoding="utf-8"))

    conn = _connect(str(out_db))
    try:
        _init_schema(conn)

        if args.wipe:
            conn.executescript(
                """
                DELETE FROM pages;
                DELETE FROM docs;
                DELETE FROM pages_fts;
                """
            )
            conn.commit()

        pdfs = list(_iter_pdfs(root))
        if not pdfs:
            raise SystemExit(f"No PDFs found under: {root}")

        indexed = 0
        skipped = 0

        for pdf in pdfs:
            family = _infer_aircraft_family_from_path(pdf)
            rev = _infer_revision_from_filename(pdf.name)
            title = pdf.stem

            fh = _file_hash(pdf)
            existing_id = _doc_exists(conn, family, rev, fh)
            if existing_id is not None:
                skipped += 1
                continue

            meta = DocMeta(
                aircraft_family=family,
                revision=rev,
                title=title,
                file_name=pdf.name,
                file_path=str(pdf),
                base_url=base_url_map.get(family),
            )

            print(f"Indexing: {pdf}  (family={family}, rev={rev})")
            texts = _extract_pages_text(pdf, max_pages=args.max_pages)

            doc_id = _insert_doc(conn, meta, fh)
            _insert_pages(conn, doc_id, texts)
            indexed += 1

        _rebuild_fts(conn)

        print("✅ Done")
        print(f"DB: {out_db}")
        print(f"Docs indexed: {indexed}")
        print(f"Docs skipped (already present): {skipped}")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
