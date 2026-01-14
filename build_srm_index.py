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
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
from pathlib import Path
from typing import List, Optional

try:
    from PyPDF2 import PdfReader
except Exception as e:
    raise SystemExit("PyPDF2 not installed. Run: python3 -m pip install PyPDF2") from e


# ----------------------------
# Utilities
# ----------------------------

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


import re

def normalize_pdf_text(s: str) -> str:
    """
    Make PDF-extracted SRM text searchable and more readable:
    - normalize dash variants
    - fix hyphenated line breaks
    - add spaces between glued tokens (CamelCase, ALLCAPS->lowercase, digit/alpha)
    - split common SRM glued patterns (Greaterthan, Lessthan, Referto, etc.)
    - split long ALLCAPS mega-words using a small SRM vocabulary
    - NEW: segment long all-lowercase glued runs using lightweight DP
    """
    if not s:
        return ""

    # ----------------------------
    # Helpers: lightweight segmentation
    # ----------------------------
    try:
        from wordfreq import zipf_frequency  # type: ignore
    except Exception:
        zipf_frequency = None  # fallback

    SRM_TERMS = [
        # structure / aircraft
        "fuselage","skin","crown","pressurized","stringer","stringers","frame","frames",
        "station","stations","bulkhead","door","cutout","fastener","fasteners","hole","holes",
        "lap","splice","bonded","unbonded","tear","strap",

        # damage & actions
        "allowable","damage","limits","limit","dent","dents","crack","cracks","scratch","scratches",
        "nick","nicks","gouge","gouges","corrosion","wrinkle","wrinkles","buckle","buckles",
        "repair","repaired","permanent","temporarily","inspection","visual","detailed","hfec",
        "cycles","within","every","initially","replace","install","apply","remove","refer",
        "approved","torque","interference","transition","shifted",

        # common SRM glue words
        "greater","less","than","more","from","between","and","or","not","permitted","must",
        "use","you","do","if","the","a","an","to","of","in","on","for","as","is","are","be",
        "shown","given","figure","table","note","continued","general","requirements",
    ]
    SRM_VOCAB = set(SRM_TERMS)

    def _word_score(w: str) -> float:
        """
        Higher is better.
        If wordfreq is installed, use Zipf frequency.
        Otherwise, use a small SRM vocab + simple heuristics.
        """
        if not w:
            return -999.0
        if w in SRM_VOCAB:
            return 6.0  # strong
        if zipf_frequency is not None:
            # zipf_frequency returns ~0-7+; unknown words near 0
            return float(zipf_frequency(w, "en"))
        # fallback heuristic: prefer shorter common-ish segments
        if len(w) <= 2:
            return 1.0
        if len(w) <= 5:
            return 2.0
        if len(w) <= 9:
            return 1.5
        return 0.5

    def segment_lowercase_run(token: str, max_word_len: int = 22) -> str:
        """
        Segment a long all-lowercase glued string using DP.
        Only called for tokens that are very likely to be glued prose.
        """
        n = len(token)
        if n < 18:
            return token

        # DP arrays
        best_cost = [float("inf")] * (n + 1)
        best_cost[0] = 0.0
        back = [-1] * (n + 1)

        # penalty for "unknown" chunks
        UNKNOWN_PENALTY = 4.0

        for i in range(1, n + 1):
            j0 = max(0, i - max_word_len)
            for j in range(j0, i):
                w = token[j:i]
                # skip ridiculous splits
                if len(w) == 1:
                    continue

                score = _word_score(w)
                # Convert to cost (lower is better)
                # - encourage known/frequent words
                # - penalize unknown long blobs
                cost = best_cost[j]
                if score <= 0.1 and w not in SRM_VOCAB:
                    cost += UNKNOWN_PENALTY + (len(w) / 8.0)
                else:
                    cost += (7.0 - score)  # high score -> low cost

                if cost < best_cost[i]:
                    best_cost[i] = cost
                    back[i] = j

        # if no path found, return as-is
        if back[n] == -1:
            return token

        # reconstruct
        out = []
        i = n
        while i > 0:
            j = back[i]
            if j < 0:
                return token
            out.append(token[j:i])
            i = j
        out.reverse()

        # light post-filter: avoid splitting into too many tiny bits
        if len(out) > n / 3:
            return token

        return " ".join(out)

    # ----------------------------
    # Normalize dash types
    # ----------------------------
    s = (
        s.replace("\u2010", "-")
         .replace("\u2011", "-")
         .replace("\u2012", "-")
         .replace("\u2013", "-")
         .replace("\u2014", "-")
    )

    # Remove NULs
    s = s.replace("\x00", " ")

    # Fix hyphenated line breaks: "allow-\nable" -> "allowable"
    s = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", s)

    # Normalize newlines
    s = re.sub(r"\r\n?", "\n", s)

    # --- SRM-specific deglue (high value) ---
    s = re.sub(r"\b(Greater|Less)than\b", r"\1 than", s, flags=re.IGNORECASE)
    s = re.sub(r"\bReferto\b", "Refer to", s, flags=re.IGNORECASE)
    s = re.sub(r"\bNOTE:\s*", "NOTE: ", s)
    s = re.sub(r"\brepairif\b", "repair if", s, flags=re.IGNORECASE)

    # --- Generic spacing rules ---
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)           # AllowableDamage -> Allowable Damage
    s = re.sub(r"([A-Z]{2,})([a-z])", r"\1 \2", s)       # SRMapproved -> SRM approved
    s = re.sub(r"([A-Za-z])(\d)", r"\1 \2", s)           # Table102 -> Table 102
    s = re.sub(r"(\d)([A-Za-z])", r"\1 \2", s)           # 102Table -> 102 Table
    s = re.sub(r"([:;,\.\)\]])(\w)", r"\1 \2", s)        # mm)Lessthan -> mm) Lessthan

    # Split long ALLCAPS mega-words using a small SRM vocabulary
    CAPS_TERMS = [
        "FUSELAGE","ALLOWABLE","DAMAGE","LIMITS","LIMIT","SKIN","DENT",
        "REPAIR","GENERAL","INSPECTION","CRACK","STRINGER","STRINGERS",
        "STATION","STATIONS","FASTENER","FASTENERS","CORRECTIVE","ACTION",
        "PRESSURIZED","CROWN","AREA","NOTE","CONTINUED"
    ]
    CAPS_TERMS = sorted(set(CAPS_TERMS), key=len, reverse=True)

    def split_caps_token(tok: str) -> str:
        if not tok.isupper() or len(tok) < 18:
            return tok
        t = tok
        for term in CAPS_TERMS:
            t = t.replace(term, term + " ")
        return " ".join(t.split())

    parts = []
    for tok in re.split(r"(\s+)", s):
        if tok and not tok.isspace():
            parts.append(split_caps_token(tok))
        else:
            parts.append(tok)
    s = "".join(parts)

    # ----------------------------
    # NEW: segment long all-lowercase runs token-by-token
    # Only apply to tokens that are:
    #  - all lowercase letters
    #  - very long
    #  - not already a known short word
    # ----------------------------
    def maybe_segment_token(tok: str) -> str:
        if re.fullmatch(r"[a-z]{18,}", tok) and tok not in SRM_VOCAB:
            return segment_lowercase_run(tok)
        return tok

    parts = []
    for tok in re.split(r"(\s+)", s):
        if tok and not tok.isspace():
            parts.append(maybe_segment_token(tok))
        else:
            parts.append(tok)
    s = "".join(parts)

    # Collapse horizontal whitespace line-by-line (keep newlines)
    s = "\n".join(" ".join(line.split()) for line in s.splitlines())

    return s.strip()



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
        INSERT INTO docs (aircraft_family, revision, title, file_name, file_hash)
        VALUES (?, ?, ?, ?, ?)
        """,
        (family, revision, title, file_name, file_hash),
    )
    return int(cur.lastrowid)


def extract_pages_text(pdf_path: Path, max_pages: Optional[int] = None) -> List[str]:
    reader = PdfReader(str(pdf_path))
    total = len(reader.pages)
    if max_pages is not None:
        total = min(total, max_pages)

    out: List[str] = []
    for i in range(total):
        page = reader.pages[i]
        raw = page.extract_text() or ""
        txt = normalize_pdf_text(raw)
        out.append(txt)
    return out


def insert_pages(conn: sqlite3.Connection, doc_id: int, page_texts: List[str]) -> None:
    # Store normalized text
    conn.executemany(
        "INSERT INTO pages (doc_id, page_no, text) VALUES (?, ?, ?)",
        [(doc_id, i + 1, t) for i, t in enumerate(page_texts)],
    )


def rebuild_fts(conn: sqlite3.Connection) -> None:
    # Rebuild from pages content table (works with external-content FTS)
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

            texts = extract_pages_text(pdf, max_pages=args.max_pages)

            # Transaction per doc for speed & safety
            conn.execute("BEGIN;")
            doc_id = insert_doc(conn, family, revision, title, pdf.name, fhash)
            insert_pages(conn, doc_id, texts)
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
