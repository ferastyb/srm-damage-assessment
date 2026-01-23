#!/usr/bin/env python3
"""
build_srm_index.py

Builds srm_index.db from SRM PDFs in srm_library/<AIRCRAFT>/ folders.
Index granularity: page-level
Search engine: SQLite FTS5 (pages_fts)

Usage:
  python3 -m pip install PyPDF2
  python3 build_srm_index.py --in srm_library --out srm_index.db --wipe
  python3 build_srm_index.py --in srm_library --out srm_index.db

Notes:
- This does NOT upload SRMs anywhere; it just creates a local searchable DB.
- PDFs should not be committed publicly if they are proprietary.
- This version extracts BOTH:
    * PDF page index (page_no)
    * Printed SRM page number (printed_page), e.g. 108
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

try:
    from PyPDF2 import PdfReader
except Exception as e:
    raise SystemExit("PyPDF2 not installed. Run: python3 -m pip install PyPDF2") from e


# ----------------------------
# Utilities
# ----------------------------

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def infer_family(pdf_path: Path) -> str:
    # srm_library/B737/foo.pdf -> B737
    return pdf_path.parent.name.upper().strip() or "UNKNOWN"


def infer_revision(filename: str) -> str:
    """
    Tries to extract revision from file name:
      - REV78, Rev_79, REV-01, etc.
    Otherwise returns UNKNOWN
    """
    name = filename.upper()
    m = re.search(r"\bREV[\s_-]*([A-Z0-9]+)\b", name)
    if m:
        return m.group(1)
    return "UNKNOWN"


def normalize_pdf_text(s: str) -> str:
    """
    Make PDF text searchable (fix common extraction artifacts).

    Key fix for SRM excerpts:
      "AllowableDamage1givestheallowabledamage..." -> becomes tokenizable.
    """
    if not s:
        return ""

    # Remove NULs
    s = s.replace("\x00", " ")

    # Normalize dash types
    s = (
        s.replace("\u2010", "-")
         .replace("\u2011", "-")
         .replace("\u2012", "-")
         .replace("\u2013", "-")
         .replace("\u2014", "-")
    )

    # Fix hyphenated line breaks: "allow-\nable" -> "allowable"
    s = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", s)

    # Ensure whitespace after punctuation when missing: "mm)from" -> "mm) from"
    s = re.sub(r"([.,;:])(?=\w)", r"\1 ", s)

    # Insert spaces between lower->upper (CamelCase): "AllowableDamage" -> "Allowable Damage"
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)

    # Insert spaces between letters and digits: "Damage1" -> "Damage 1", "3.0in" -> "3.0 in"
    s = re.sub(r"([A-Za-z])(\d)", r"\1 \2", s)
    s = re.sub(r"(\d)([A-Za-z])", r"\1 \2", s)

    # 0.0005and0.0045 -> 0.0005 and 0.0045
    s = re.sub(r"(\d)and(\d)", r"\1 and \2", s, flags=re.IGNORECASE)

    # 0.0005to0.0045 -> 0.0005 to 0.0045
    s = re.sub(r"(\d)to(\d)", r"\1 to \2", s, flags=re.IGNORECASE)

    # within500cycles -> within 500 cycles
    s = re.sub(r"\bwithin(?=\d)", "within ", s, flags=re.IGNORECASE)

    # every500cycles -> every 500 cycles
    s = re.sub(r"\bevery(?=\d)", "every ", s, flags=re.IGNORECASE)

    # before5000cycles -> before 5000 cycles
    s = re.sub(r"\bbefore(?=\d)", "before ", s, flags=re.IGNORECASE)

    # Add space before unit: 3.175mm / 0.125in / 0.0045inch
    s = re.sub(
        r"(\d)\s*(mm|cm|m|in\.?|inch|inches|ft|psi|lb|lbs|cycles)\b",
        r"\1 \2",
        s,
        flags=re.IGNORECASE,
    )

    # Space around slash if stuck: "Table102/ALLOWABLEDAMAGE1" -> "Table 102 / ALLOWABLE DAMAGE 1"
    s = re.sub(r"([A-Za-z])(/)([A-Za-z])", r"\1 \2 \3", s)

    # Split LONG ALLCAPS runs using a lightweight dictionary-based splitter
    caps_terms = [
        "FUSELAGE", "ALLOWABLE", "DAMAGE", "LIMITS", "LIMIT", "SKIN", "DENT",
        "REPAIR", "GENERAL", "INSPECTION", "CRACK", "STRINGER", "STRINGERS",
        "STATION", "STATIONS", "FASTENER", "FASTENERS", "CORRECTIVE", "ACTION",
        "PRESSURIZED", "CROWN", "AREA", "NOTE", "CONTINUED", "TABLE", "FIGURE"
    ]
    caps_terms = sorted(set(caps_terms), key=len, reverse=True)

    def split_caps_run(m: re.Match) -> str:
        tok = m.group(0)
        t = tok
        for term in caps_terms:
            t = t.replace(term, term + " ")
        return " ".join(t.split())

    s = re.sub(r"[A-Z]{18,}", split_caps_run, s)

    # Greaterthan0.125 -> Greater than 0.125
    s = re.sub(r"\b(Greater|Less)than(?=\d|\b)", r"\1 than", s, flags=re.IGNORECASE)

    # morethan3.0 -> more than 3.0
    s = re.sub(r"\bmorethan(?=\d|\b)", "more than", s, flags=re.IGNORECASE)

    # Referto51-40-05 -> Refer to 51-40-05
    s = re.sub(r"\bReferto(?=\d)", "Refer to ", s, flags=re.IGNORECASE)

    # NOTE:Installa -> NOTE: Install a
    s = re.sub(r"\bNOTE:\s*", "NOTE: ", s)

    # Normalize line endings and collapse excessive whitespace (keep paragraph breaks)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = "\n".join(" ".join(line.split()) for line in s.splitlines())
    s = re.sub(r"\n{3,}", "\n\n", s).strip()

    return s


def extract_printed_page(raw_text: str) -> Optional[int]:
    """
    Attempt to detect the SRM *printed* page number (e.g., 108).
    Heuristic:
      - Look at the last ~12 non-empty lines of the raw extracted page text.
      - Try patterns like "PAGE 108" / "Page 108"
      - Then try a line that is only digits (2-4 digits), often in footer.
    """
    if not raw_text:
        return None

    # Keep original-ish line breaks for footer scan
    raw = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.strip() for ln in raw.split("\n") if ln.strip()]
    if not lines:
        return None

    tail = lines[-12:]

    # Pattern 1: explicit PAGE label
    for ln in tail[::-1]:
        m = re.search(r"\bPAGE\s+(\d{1,4})\b", ln, flags=re.IGNORECASE)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                pass
        m = re.search(r"\bPage\s+(\d{1,4})\b", ln, flags=re.IGNORECASE)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                pass

    # Pattern 2: standalone digits line (footer)
    for ln in tail[::-1]:
        if re.fullmatch(r"\d{2,4}", ln):
            try:
                return int(ln)
            except Exception:
                pass

    # Pattern 3: sometimes footers have "… 108" at end
    for ln in tail[::-1]:
        m = re.search(r"(\d{2,4})\s*$", ln)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                pass

    return None


# ----------------------------
# DB helpers
# ----------------------------

def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_schema(conn: sqlite3.Connection, schema_path: Path) -> None:
    schema_sql = schema_path.read_text(encoding="utf-8")
    conn.executescript(schema_sql)
    conn.commit()


def wipe_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DELETE FROM pages;
        DELETE FROM docs;
        DELETE FROM pages_fts;
        """
    )
    conn.commit()


def doc_exists(conn: sqlite3.Connection, family: str, revision: str, file_hash: str) -> Optional[int]:
    row = conn.execute(
        "SELECT id FROM docs WHERE aircraft_family=? AND revision=? AND file_hash=?",
        (family, revision, file_hash),
    ).fetchone()
    return int(row[0]) if row else None


def insert_doc(conn: sqlite3.Connection, family: str, revision: str, title: str, file_name: str, file_hash: str) -> int:
    cur = conn.execute(
        """
        INSERT INTO docs (aircraft_family, revision, title, file_name, file_hash, created_utc)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (family, revision, title, file_name, file_hash, utc_now_iso()),
    )
    return int(cur.lastrowid)


def extract_pages(pdf_path: Path, max_pages: Optional[int] = None) -> List[Tuple[int, Optional[int], str]]:
    """
    Returns list of (pdf_page_no, printed_page_no, normalized_text)
    """
    reader = PdfReader(str(pdf_path))
    total = len(reader.pages)
    if max_pages is not None:
        total = min(total, max_pages)

    out: List[Tuple[int, Optional[int], str]] = []
    for i in range(total):
        page = reader.pages[i]
        raw = page.extract_text() or ""
        printed = extract_printed_page(raw)
        norm = normalize_pdf_text(raw)
        out.append((i + 1, printed, norm))
    return out


def insert_pages(conn: sqlite3.Connection, doc_id: int, page_rows: List[Tuple[int, Optional[int], str]]) -> None:
    conn.executemany(
        "INSERT INTO pages (doc_id, page_no, printed_page, text) VALUES (?, ?, ?, ?)",
        [(doc_id, pdf_page, printed_page, text) for (pdf_page, printed_page, text) in page_rows],
    )


def rebuild_fts(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO pages_fts(pages_fts) VALUES ('rebuild');")


def iter_pdfs(root: Path) -> List[Path]:
    return sorted([p for p in root.rglob("*.pdf") if p.is_file()])


# ----------------------------
# Main
# ----------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Build SRM SQLite FTS5 index from PDFs.")
    ap.add_argument("--in", dest="inp", default="srm_library", help="Input folder (default: srm_library)")
    ap.add_argument("--out", dest="out", default="srm_index.db", help="Output SQLite DB (default: srm_index.db)")
    ap.add_argument("--schema", dest="schema", default="srm_index_schema.sql", help="Schema SQL file")
    ap.add_argument("--wipe", action="store_true", help="Wipe DB content before indexing")
    ap.add_argument("--max-pages", type=int, default=None, help="Limit pages per PDF (debug)")
    args = ap.parse_args()

    root = Path(args.inp).expanduser().resolve()
    out_db = Path(args.out).expanduser().resolve()
    schema = Path(args.schema).expanduser().resolve()

    if not root.exists():
        raise SystemExit(f"Input folder not found: {root}")
    if not schema.exists():
        raise SystemExit(f"Schema file not found: {schema}")

    pdfs = iter_pdfs(root)
    if not pdfs:
        raise SystemExit(f"No PDFs found under: {root}")

    conn = connect(out_db)
    try:
        init_schema(conn, schema)
        if args.wipe:
            wipe_db(conn)

        indexed = 0
        skipped = 0

        for pdf in pdfs:
            family = infer_family(pdf)
            revision = infer_revision(pdf.name)
            title = pdf.stem
            fhash = sha256_file(pdf)

            if doc_exists(conn, family, revision, fhash) is not None:
                skipped += 1
                continue

            print(f"Indexing {family} rev={revision}: {pdf.name}")

            page_rows = extract_pages(pdf, max_pages=args.max_pages)

            conn.execute("BEGIN;")
            doc_id = insert_doc(conn, family, revision, title, pdf.name, fhash)
            insert_pages(conn, doc_id, page_rows)
            conn.commit()

            indexed += 1

        rebuild_fts(conn)
        conn.commit()

        print("\n✅ SRM index build complete")
        print(f"DB: {out_db}")
        print(f"Docs indexed: {indexed}")
        print(f"Docs skipped: {skipped}")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
