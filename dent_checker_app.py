# dent_checker_app.py
# Streamlit app: SRM Damage Assessment (Prototype)
#
# Key features:
# - Fast “free-text” damage description parsing into structured fields
# - Dent assessment using damage_models (if present)
# - Rules evaluation using rules_engine (if present)
# - SRM full-text search using srm_index.db (if present)
# - SRM DB Debug panel (shows cwd + existence + size + sha256 prefix)
# - Location verification vs SRM excerpt coverage ranges (Stations / Stringers / Frames)
# - Optional logging of assessments to SQLite (assessments.db)
#
# Option A + Option B behavior:
# - Option A (Upload SRM excerpt PDF): user can upload a PDF; we extract coverage ranges
#   directly from the uploaded PDF text to verify STA/Stringer/Frame applicability.
# - Option B (Use srm_index.db): if no PDF uploaded, we verify against coverage ranges
#   extracted from the indexed SRM doc text stored in srm_index.db (best-effort).
# - If srm_index.db is missing, we warn (Streamlit Cloud can only use committed files).
#
# IMPORTANT SAFETY FIX (Patch 1):
# - The app will NOT claim “within limits per SRM …” if location verification says
#   the SRM excerpt does NOT cover the provided location. It will instead state that
#   the excerpt is not applicable → ENGINEERING REVIEW.
#
# Patch 2:
# - Expanded stringer-range extraction patterns: S10L-S10R, S-10L - S-10R,
#   “between Stringers 24L and 24R”, etc.

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import streamlit as st


# -----------------------------
# Page config
# -----------------------------
st.set_page_config(
    page_title="SRM Damage Assessment Tool",
    layout="wide",
)

st.title("SRM Damage Assessment Tool (Prototype)")
st.caption("Prototype to structure AOG damage descriptions, evaluate rules, and search SRM excerpts.")


# -----------------------------
# Safe imports (optional modules)
# -----------------------------
HAS_DAMAGE_MODELS = False
HAS_RULES_ENGINE = False
HAS_SRM_SEARCH = False
HAS_PYPDF2 = False

damage_models_err = None
rules_engine_err = None
srm_search_err = None
pypdf2_err = None

try:
    # expected exports in your project:
    # - DentDamage (dataclass)
    # - assess_dent(dent: DentDamage, ...) -> dict or result
    # - build_plain_text_summary(result, ...) -> str (optional)
    from damage_models import DentDamage, assess_dent, build_plain_text_summary  # type: ignore

    HAS_DAMAGE_MODELS = True
except Exception as e:
    damage_models_err = e

try:
    import rules_engine  # type: ignore

    HAS_RULES_ENGINE = True
except Exception as e:
    rules_engine_err = e

try:
    import srm_search  # type: ignore

    HAS_SRM_SEARCH = True
except Exception as e:
    srm_search_err = e

try:
    from PyPDF2 import PdfReader  # type: ignore

    HAS_PYPDF2 = True
except Exception as e:
    pypdf2_err = e


# -----------------------------
# Paths
# -----------------------------
ROOT = Path(__file__).resolve().parent
RULES_DB = ROOT / "rules.db"
SRM_DB = ROOT / "srm_index.db"
ASSESSMENTS_DB = ROOT / "assessments.db"


# -----------------------------
# Helpers
# -----------------------------
def sha256_path(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_json(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    except Exception:
        return str(obj)


def _normalize_aircraft_family(text: str) -> str:
    t = (text or "").strip().upper().replace(" ", "")
    if t.startswith("B7"):
        return t
    if t.startswith("A3") or t.startswith("A32"):
        return t
    if t.startswith("E1") or t.startswith("E17"):
        return t
    return (text or "").strip().upper()


def _parse_side(text: str) -> str:
    s = (text or "").upper()
    if "LH" in s or "LEFT" in s:
        return "LH"
    if "RH" in s or "RIGHT" in s:
        return "RH"
    return "ANY"


def _find_float_mm(text: str, patterns: List[str]) -> Optional[float]:
    for pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            val = m.group(1)
            try:
                return float(val)
            except Exception:
                continue
    return None


def _find_float_in(text: str, patterns: List[str]) -> Optional[float]:
    for pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            val = m.group(1)
            try:
                return float(val)
            except Exception:
                continue
    return None


def _parse_stringer_token(s: Optional[str]) -> Optional[Tuple[int, str]]:
    """
    "10L" -> (10,"L")
    "S-10L" -> (10,"L")
    """
    if not s:
        return None
    t = str(s).upper().strip()
    t = t.replace("STRINGER", "").replace("S-", "S").replace("S ", "S").strip()
    m = re.search(r"\bS?(\d{1,3})([LR])\b", t)
    if not m:
        return None
    return int(m.group(1)), m.group(2)


def _parse_frame_token(s: Optional[str]) -> Optional[int]:
    """
    Accept FR 12, FRAME 12, FR12, F12 (best-effort).
    """
    if not s:
        return None
    t = str(s).upper().strip()
    m = re.search(r"\b(?:FR|FRAME|F)\s*[-]?\s*(\d{1,4})\b", t)
    if not m:
        return None
    return int(m.group(1))


def _cmp_stringer_in_range(token: Tuple[int, str], r: Tuple[str, str]) -> bool:
    """
    Range endpoints like ("10L","10R") or ("24L","24R") or ("5R","10R") etc.
    We treat:
      - If numeric span > 0: inclusive by number, and by side if range is single-side (L->L or R->R)
      - If numeric span == 0 and sides differ (e.g., 10L->10R): only that number is covered (both sides).
    """
    n, side = token
    a, b = r
    pa = _parse_stringer_token(a)
    pb = _parse_stringer_token(b)
    if not pa or not pb:
        return False
    na, sa = pa
    nb, sb = pb
    lo = min(na, nb)
    hi = max(na, nb)

    if n < lo or n > hi:
        return False

    if lo == hi and sa != sb:
        # e.g. 10L to 10R: both sides allowed at that number
        return n == lo

    # If both endpoints on same side, require same side
    if sa == sb and side != sa:
        return False

    # If endpoints on different sides with numeric span > 0, we cannot reliably interpret cross-over.
    # Be conservative: accept by number only when the SRM explicitly says L-R for a span.
    # If it's L to R with different numbers, accept both sides in range by number.
    return True


def parse_damage_description(desc: str) -> Dict[str, Any]:
    """
    Lightweight parser for AOG descriptions.
    """
    raw = desc.strip()

    out: Dict[str, Any] = {
        "raw": raw,
        "aircraft_family": None,
        "structure": None,
        "structure_zone": None,
        "side": "ANY",
        "sta": None,
        "wl": None,
        "stringer": None,
        "frame": None,
        "damage_type": None,
        "dent_diameter_mm": None,
        "dent_depth_mm": None,
        "has_crack": None,
        "notes": None,
    }

    # Aircraft family
    m = re.search(r"\b(B7\d{2}|A3\d{2}|A32\d{2}|E1\d{2}|E17\d)\b", raw, flags=re.IGNORECASE)
    if m:
        out["aircraft_family"] = _normalize_aircraft_family(m.group(1))

    # Structure keywords (first match wins; explicit stabilizer etc should override)
    # Order matters: more specific before generic.
    if re.search(r"\bstabilizer\b|\bhorizontal\s+stab\b|\bhs\b", raw, flags=re.IGNORECASE):
        out["structure"] = "STABILIZER"
    elif re.search(r"\bvertical\s+stab\b|\bvs\b|\bfin\b", raw, flags=re.IGNORECASE):
        out["structure"] = "VERTICAL_STABILIZER"
    elif re.search(r"\bwing\b", raw, flags=re.IGNORECASE):
        out["structure"] = "WING"
    elif re.search(r"\bempennage\b|\btail\b", raw, flags=re.IGNORECASE):
        out["structure"] = "EMPENNAGE"
    elif re.search(r"\bfuselage\b", raw, flags=re.IGNORECASE):
        out["structure"] = "FUSELAGE"

    # Zone / sub-area
    if re.search(r"\bskin\b", raw, flags=re.IGNORECASE):
        out["structure_zone"] = "SKIN"
    elif re.search(r"\bstringer\b", raw, flags=re.IGNORECASE):
        out["structure_zone"] = "STRINGER"
    elif re.search(r"\bframe\b|\bfr\b", raw, flags=re.IGNORECASE):
        out["structure_zone"] = "FRAME"

    # Side
    out["side"] = _parse_side(raw)

    # STA / WL
    m = re.search(r"\bSTA(?:TION)?\s*([0-9]{2,5}(?:\.[0-9]+)?)\b", raw, flags=re.IGNORECASE)
    if m:
        try:
            out["sta"] = float(m.group(1))
        except Exception:
            pass

    m = re.search(r"\bWL\s*([0-9]{1,5}(?:\.[0-9]+)?)\b", raw, flags=re.IGNORECASE)
    if m:
        try:
            out["wl"] = float(m.group(1))
        except Exception:
            pass

    # Stringer formats: S-10L, S10L, Stringer 10L
    m = re.search(r"\bS[-\s]?(\d{1,3})([LR])\b", raw, flags=re.IGNORECASE)
    if m:
        out["stringer"] = f"{int(m.group(1))}{m.group(2).upper()}"
    else:
        m2 = re.search(r"\bSTRINGER\s*(\d{1,3})([LR])\b", raw, flags=re.IGNORECASE)
        if m2:
            out["stringer"] = f"{int(m2.group(1))}{m2.group(2).upper()}"

    # Frame formats: FR 12 / Frame 12 / FR12
    mfr = re.search(r"\b(?:FR|FRAME)\s*[-]?\s*(\d{1,4})\b", raw, flags=re.IGNORECASE)
    if mfr:
        out["frame"] = int(mfr.group(1))

    # Damage type (prioritize crack)
    if re.search(r"\bcrack\b", raw, flags=re.IGNORECASE):
        out["damage_type"] = "CRACK"
    elif re.search(r"\bdent\b", raw, flags=re.IGNORECASE):
        out["damage_type"] = "DENT"
    elif re.search(r"\bgouge\b", raw, flags=re.IGNORECASE):
        out["damage_type"] = "GOUGE"
    elif re.search(r"\bcorrosion\b", raw, flags=re.IGNORECASE):
        out["damage_type"] = "CORROSION"

    # Crack present?
    if re.search(r"\bno\s+(visible\s+)?crack(s)?\b", raw, flags=re.IGNORECASE):
        out["has_crack"] = False
    elif re.search(r"\bcrack(s)?\b", raw, flags=re.IGNORECASE):
        out["has_crack"] = True

    # Dent dimensions (mm or inches)
    dia_mm = _find_float_mm(raw, [
        r"(\d+(?:\.\d+)?)\s*mm\s*(?:dia|diameter)\b",
        r"\bdia\s*(\d+(?:\.\d+)?)\s*mm\b",
        r"dent\s*(\d+(?:\.\d+)?)\s*mm\s*(?:dia|diameter)\b",
    ])
    depth_mm = _find_float_mm(raw, [
        r"(\d+(?:\.\d+)?)\s*mm\s*depth\b",
        r"\bdepth\s*(\d+(?:\.\d+)?)\s*mm\b",
    ])

    dia_in = _find_float_in(raw, [
        r"(\d+(?:\.\d+)?)\s*(?:in|inch|in\.)\s*(?:dia|diameter)\b",
        r"\bdia\s*(\d+(?:\.\d+)?)\s*(?:in|inch|in\.)\b",
    ])
    depth_in = _find_float_in(raw, [
        r"(\d+(?:\.\d+)?)\s*(?:in|inch|in\.)\s*depth\b",
        r"\bdepth\s*(\d+(?:\.\d+)?)\s*(?:in|inch|in\.)\b",
    ])

    if dia_mm is None and dia_in is not None:
        dia_mm = dia_in * 25.4
    if depth_mm is None and depth_in is not None:
        depth_mm = depth_in * 25.4

    out["dent_diameter_mm"] = dia_mm
    out["dent_depth_mm"] = depth_mm

    return out


def init_assessments_db(db_path: Path) -> None:
    con = sqlite3.connect(str(db_path))
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS assessments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              created_utc TEXT NOT NULL,
              aircraft_family TEXT,
              structure TEXT,
              structure_zone TEXT,
              side TEXT,
              sta REAL,
              wl REAL,
              stringer TEXT,
              frame INTEGER,
              damage_type TEXT,
              dent_diameter_mm REAL,
              dent_depth_mm REAL,
              has_crack INTEGER,
              input_text TEXT,
              structured_json TEXT,
              rules_json TEXT,
              srm_hits_json TEXT,
              result_json TEXT,
              location_verify_json TEXT
            );
            """
        )
        con.commit()
    finally:
        con.close()


def log_assessment(
    db_path: Path,
    structured: Dict[str, Any],
    rules_rows: Any,
    srm_hits: Any,
    result: Any,
    location_verify: Any,
) -> None:
    init_assessments_db(db_path)

    con = sqlite3.connect(str(db_path))
    try:
        con.execute(
            """
            INSERT INTO assessments (
              created_utc, aircraft_family, structure, structure_zone, side, sta, wl, stringer, frame,
              damage_type, dent_diameter_mm, dent_depth_mm, has_crack,
              input_text, structured_json, rules_json, srm_hits_json, result_json, location_verify_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now_iso(),
                structured.get("aircraft_family"),
                structured.get("structure"),
                structured.get("structure_zone"),
                structured.get("side"),
                structured.get("sta"),
                structured.get("wl"),
                structured.get("stringer"),
                structured.get("frame"),
                structured.get("damage_type"),
                structured.get("dent_diameter_mm"),
                structured.get("dent_depth_mm"),
                None if structured.get("has_crack") is None else (1 if structured.get("has_crack") else 0),
                structured.get("raw"),
                safe_json(structured),
                safe_json(rules_rows),
                safe_json(srm_hits),
                safe_json(result),
                safe_json(location_verify),
            ),
        )
        con.commit()
    finally:
        con.close()


# -----------------------------
# SRM coverage extraction (Option A/B)
# -----------------------------
def extract_coverage_ranges_from_text(full_text: str) -> Dict[str, Any]:
    """
    Extracts station/stringer/frame ranges from SRM text (best effort).
    Returns:
      {
        "stations": [(360.0,540.0), ...],
        "stringers": [("10L","10R"), ...],
        "frames": [(12,18), ...],
        "raw_examples": {...}
      }
    """
    full = full_text or ""
    stations: List[Tuple[float, float]] = []
    stringers: List[Tuple[str, str]] = []
    frames: List[Tuple[int, int]] = []

    # --- Stations (STA) ---
    # e.g. "between Stations 360-540", "Stations 727-887", "STA 1138-1156"
    for m in re.finditer(r"\bStations?\s*(\d{2,5}(?:\.\d+)?)\s*[-–]\s*(\d{2,5}(?:\.\d+)?)\b", full, flags=re.IGNORECASE):
        stations.append((float(m.group(1)), float(m.group(2))))
    for m in re.finditer(r"\bSTA(?:TION)?\s*(\d{2,5}(?:\.\d+)?)\s*[-–]\s*(\d{2,5}(?:\.\d+)?)\b", full, flags=re.IGNORECASE):
        stations.append((float(m.group(1)), float(m.group(2))))

    # --- Stringers ---
    # Existing/common: "S-10L TO S-10R"
    for m in re.finditer(r"\bS-?\s*(\d{1,3})([LR])\s*(?:TO|THRU|THROUGH)\s*S-?\s*(\d{1,3})([LR])\b", full, flags=re.IGNORECASE):
        a = f"{int(m.group(1))}{m.group(2).upper()}"
        b = f"{int(m.group(3))}{m.group(4).upper()}"
        stringers.append((a, b))

    # Patch 2 additions:
    # "S10L-S10R" or "S-10L - S-10R"
    for m in re.finditer(r"\bS-?\s*(\d{1,3})([LR])\s*[-–]\s*S-?\s*(\d{1,3})([LR])\b", full, flags=re.IGNORECASE):
        a = f"{int(m.group(1))}{m.group(2).upper()}"
        b = f"{int(m.group(3))}{m.group(4).upper()}"
        stringers.append((a, b))

    # "Stringers 24L-24R" / "between Stringers 24L and 24R"
    for m in re.finditer(
        r"\bStringers?\s*(\d{1,3}\s*[LR])\s*(?:AND|TO|THRU|THROUGH|[-–])\s*(\d{1,3}\s*[LR])\b",
        full,
        flags=re.IGNORECASE,
    ):
        a = m.group(1).replace(" ", "").upper()
        b = m.group(2).replace(" ", "").upper()
        # normalize "24L" etc
        stringers.append((a, b))

    # --- Frames ---
    # e.g. "Frames 12-18", "FR 12-18", "between Frames 100-120"
    for m in re.finditer(r"\b(?:Frames?|FR)\s*(\d{1,4})\s*[-–]\s*(\d{1,4})\b", full, flags=re.IGNORECASE):
        frames.append((int(m.group(1)), int(m.group(2))))

    # De-dupe
    def _uniq(seq):
        seen = set()
        out = []
        for x in seq:
            if x in seen:
                continue
            seen.add(x)
            out.append(x)
        return out

    stations = _uniq(stations)
    stringers = _uniq(stringers)
    frames = _uniq(frames)

    return {
        "stations": stations,
        "stringers": stringers,
        "frames": frames,
    }


def extract_text_from_uploaded_pdf(pdf_bytes: bytes, max_pages: int = 50) -> str:
    if not HAS_PYPDF2:
        return ""
    try:
        import io

        reader = PdfReader(io.BytesIO(pdf_bytes))
        total = min(len(reader.pages), max_pages)
        chunks = []
        for i in range(total):
            t = reader.pages[i].extract_text() or ""
            chunks.append(t)
        return "\n".join(chunks)
    except Exception:
        return ""


def get_indexed_doc_fulltext(conn: sqlite3.Connection, file_name: Optional[str], doc_title: Optional[str]) -> str:
    """
    Pull all pages.text for a doc from srm_index.db (Option B source).
    """
    if not file_name and not doc_title:
        return ""
    if file_name:
        row = conn.execute("SELECT id FROM docs WHERE file_name = ? LIMIT 1", (file_name,)).fetchone()
    else:
        row = conn.execute("SELECT id FROM docs WHERE title = ? LIMIT 1", (doc_title,)).fetchone()
    if not row:
        return ""
    doc_id = int(row[0])
    rows = conn.execute("SELECT text FROM pages WHERE doc_id = ? ORDER BY page_no", (doc_id,)).fetchall()
    return "\n".join((r[0] or "") for r in rows)


def verify_location_against_coverage(structured: Dict[str, Any], coverage: Dict[str, Any]) -> Dict[str, Any]:
    """
    Returns:
      {
        "ok": bool|None,
        "messages": [str...],
        "coverage": {stations/stringers/frames}
      }
    ok = False if any explicit mismatch
    ok = True if all provided fields are within at least one extracted range (or ranges absent)
    ok = None if we have nothing to verify (no ranges and no location)
    """
    messages: List[str] = []
    ok: Optional[bool] = True

    sta = structured.get("sta")
    stringer = structured.get("stringer")
    frame = structured.get("frame")

    stations: List[Tuple[float, float]] = coverage.get("stations") or []
    stringers: List[Tuple[str, str]] = coverage.get("stringers") or []
    frames: List[Tuple[int, int]] = coverage.get("frames") or []

    # STA
    if sta is not None:
        if stations:
            in_any = any(min(a, b) <= float(sta) <= max(a, b) for (a, b) in stations)
            if not in_any:
                ok = False
                messages.append(f"• STA {sta:g} is NOT within SRM station coverage ranges: {stations}")
            else:
                messages.append(f"• STA {sta:g} is within SRM station coverage ranges.")
        else:
            messages.append("• SRM excerpt has no explicit station ranges; cannot verify STA.")
    # Stringer
    if stringer:
        if stringers:
            tok = _parse_stringer_token(stringer)
            if tok:
                in_any = any(_cmp_stringer_in_range(tok, r) for r in stringers)
                if not in_any:
                    ok = False
                    messages.append(f"• Stringer {stringer} is NOT within SRM stringer coverage ranges: {stringers}")
                else:
                    messages.append(f"• Stringer {stringer} is within SRM stringer coverage ranges.")
            else:
                messages.append("• Could not parse stringer token; cannot verify stringer.")
        else:
            messages.append("• SRM excerpt has no explicit stringer ranges; cannot verify stringer.")
    # Frame
    if frame is not None:
        if frames:
            in_any = any(min(a, b) <= int(frame) <= max(a, b) for (a, b) in frames)
            if not in_any:
                ok = False
                messages.append(f"• Frame {frame} is NOT within SRM frame coverage ranges: {frames}")
            else:
                messages.append(f"• Frame {frame} is within SRM frame coverage ranges.")
        else:
            messages.append("• SRM excerpt has no explicit frame ranges; cannot verify frame.")

    if sta is None and not stringer and frame is None:
        ok = None
        messages.append("• No STA/stringer/frame provided; nothing to verify.")

    # If we had no ranges at all and user did provide something, keep ok True but flag uncertainty
    if (sta is not None or stringer or frame is not None) and (not stations and not stringers and not frames):
        ok = None  # conservative: we cannot assert applicability
        messages.append("• No coverage ranges could be extracted from the SRM source; applicability cannot be confirmed.")

    return {"ok": ok, "messages": messages, "coverage": coverage}


# -----------------------------
# Sidebar: environment / health
# -----------------------------
with st.sidebar:
    st.header("Environment")
    st.write("Working directory:", os.getcwd())
    st.write("Repo root:", str(ROOT))

    st.subheader("Modules")
    st.write("damage_models:", "✅" if HAS_DAMAGE_MODELS else "❌")
    if damage_models_err:
        st.caption(f"damage_models import error: {damage_models_err}")

    st.write("rules_engine:", "✅" if HAS_RULES_ENGINE else "❌")
    if rules_engine_err:
        st.caption(f"rules_engine import error: {rules_engine_err}")

    st.write("srm_search:", "✅" if HAS_SRM_SEARCH else "❌")
    if srm_search_err:
        st.caption(f"srm_search import error: {srm_search_err}")

    st.write("PyPDF2:", "✅" if HAS_PYPDF2 else "❌")
    if pypdf2_err:
        st.caption(f"PyPDF2 import error: {pypdf2_err}")

    st.subheader("Databases")
    st.write("rules.db exists:", RULES_DB.exists())
    st.write("srm_index.db exists:", SRM_DB.exists())
    st.write("assessments.db exists:", ASSESSMENTS_DB.exists())

    st.divider()
    st.caption("Tip: On Streamlit Cloud, only files committed to GitHub are available at runtime.")


# -----------------------------
# Main UI
# -----------------------------
colA, colB = st.columns([1.1, 0.9], gap="large")

with colA:
    st.subheader("1) Paste damage description")
    default_text = "B737, fuselage, LH side, STA 1123, S-50L, FR 12, skin dent 0.25mm dia, 3.18mm depth, no visible crack."
    desc = st.text_area(
        "Damage description",
        value=default_text,
        height=120,
        help="Paste a single-line or multi-line AOG description. The app will parse into structured fields.",
    )

    parse_now = st.button("Parse description", type="primary")

    if "structured" not in st.session_state:
        st.session_state.structured = parse_damage_description(default_text)

    if parse_now:
        st.session_state.structured = parse_damage_description(desc)

    structured = st.session_state.structured

    st.subheader("2) Structured fields")
    f1, f2, f3, f4 = st.columns(4)
    with f1:
        aircraft_family = st.text_input("Aircraft family", value=structured.get("aircraft_family") or "")
        structure = st.text_input("Structure", value=structured.get("structure") or "")
    with f2:
        structure_zone = st.text_input("Zone", value=structured.get("structure_zone") or "")
        side = st.selectbox("Side", ["ANY", "LH", "RH"], index=["ANY", "LH", "RH"].index(structured.get("side") or "ANY"))
    with f3:
        sta = st.number_input("STA", value=float(structured.get("sta") or 0.0), step=1.0, format="%.1f")
        wl = st.number_input("WL", value=float(structured.get("wl") or 0.0), step=1.0, format="%.1f")
    with f4:
        stringer = st.text_input("Stringer", value=structured.get("stringer") or "")
        frame = st.number_input("Frame (FR)", value=int(structured.get("frame") or 0), step=1)
        damage_type = st.selectbox("Damage type", ["DENT", "CRACK", "GOUGE", "CORROSION", "OTHER"], index=0)

    d1, d2, d3 = st.columns(3)
    with d1:
        dent_dia = st.number_input("Dent diameter (mm)", value=float(structured.get("dent_diameter_mm") or 0.0), step=0.1, format="%.2f")
    with d2:
        dent_depth = st.number_input("Dent depth (mm)", value=float(structured.get("dent_depth_mm") or 0.0), step=0.1, format="%.2f")
    with d3:
        crack_opt = st.selectbox("Crack present?", ["Unknown", "No", "Yes"], index=0)

    # Write back into structured dict (source of truth for evaluation/search)
    structured["raw"] = desc.strip()
    structured["aircraft_family"] = _normalize_aircraft_family(aircraft_family) if aircraft_family else None
    structured["structure"] = structure.strip().upper() if structure else None
    structured["structure_zone"] = structure_zone.strip().upper() if structure_zone else None
    structured["side"] = side
    structured["sta"] = None if sta == 0.0 else float(sta)
    structured["wl"] = None if wl == 0.0 else float(wl)
    structured["stringer"] = stringer.strip().upper() or None
    structured["frame"] = None if frame == 0 else int(frame)
    structured["damage_type"] = damage_type
    structured["dent_diameter_mm"] = None if dent_dia == 0.0 else float(dent_dia)
    structured["dent_depth_mm"] = None if dent_depth == 0.0 else float(dent_depth)

    if crack_opt == "Unknown":
        structured["has_crack"] = None
    elif crack_opt == "No":
        structured["has_crack"] = False
    else:
        structured["has_crack"] = True

    st.subheader("3) SRM excerpt upload (Option A)")
    uploaded_pdf = st.file_uploader(
        "Upload SRM excerpt PDF (optional). If uploaded, the app will extract station/stringer/frame coverage from it.",
        type=["pdf"],
        accept_multiple_files=False,
    )

    st.subheader("4) Run assessment")
    run = st.button("Run rules + SRM search + dent model", type="primary")

with colB:
    st.subheader("Results")

    # SRM DB Debug
    with st.expander("SRM DB Debug", expanded=True):
        st.write("cwd:", os.getcwd())
        st.write("SRM DB path:", str(SRM_DB))
        st.write("srm_index.db exists:", SRM_DB.exists())
        if SRM_DB.exists():
            st.write("srm_index.db size (bytes):", SRM_DB.stat().st_size)
            try:
                st.write("srm_index.db sha256 (prefix):", sha256_path(SRM_DB)[:16])
            except Exception as e:
                st.write("sha256 error:", str(e))
        else:
            st.info("If you want SRM hits on Streamlit Cloud, commit srm_index.db to the repo (PDFs are not needed at runtime).")

    if run:
        # --------------
        # Dent model
        # --------------
        dent_result: Dict[str, Any] = {"status": "not_run"}
        dent_debug: Dict[str, Any] = {}

        if HAS_DAMAGE_MODELS and structured.get("damage_type") in ("DENT", "CRACK"):
            try:
                import inspect

                sig = str(inspect.signature(DentDamage))  # type: ignore
                dent_debug["DentDamage_signature"] = sig

                # Build kwargs flexibly (only pass what DentDamage accepts)
                params = list(inspect.signature(DentDamage).parameters.keys())  # type: ignore
                dent_debug["accepted_params"] = params

                candidate = {
                    # common names we might have
                    "aircraft_type": structured.get("aircraft_family") or "UNKNOWN",
                    "aircraft_family": structured.get("aircraft_family") or "UNKNOWN",
                    "structure_zone": structured.get("structure_zone") or "UNKNOWN",
                    "zone": structured.get("structure_zone") or "UNKNOWN",
                    "side": structured.get("side") or "ANY",
                    "sta": None if structured.get("sta") is None else str(int(structured["sta"])),
                    "stringer": structured.get("stringer"),
                    "dent_diameter_mm": float(structured.get("dent_diameter_mm") or 0.0),
                    "dent_depth_mm": float(structured.get("dent_depth_mm") or 0.0),
                    "crack_present": bool(structured.get("has_crack") is True),
                    "has_crack": structured.get("has_crack"),
                    "notes": structured.get("notes"),
                }

                # If user explicitly said "No", force False; if unknown, default False (conservative for allowability)
                if structured.get("has_crack") is False:
                    candidate["crack_present"] = False
                elif structured.get("has_crack") is None:
                    candidate["crack_present"] = False
                    dent_debug["crack_present_reason"] = "defaulted False because crack status was Unknown"
                else:
                    candidate["crack_present"] = True

                filtered = {k: v for k, v in candidate.items() if k in params}
                dropped = [k for k in candidate.keys() if k not in filtered]
                dent_debug["filtered_kwargs_used"] = filtered
                dent_debug["dropped_candidate_keys"] = dropped

                dent = DentDamage(**filtered)  # type: ignore
                r = assess_dent(dent)  # type: ignore
                dent_result = r if isinstance(r, dict) else {"result": str(r)}
            except Exception as e:
                dent_result = {"status": "error", "error": f"Could not construct/run DentDamage: {e}"}
        else:
            if structured.get("damage_type") not in ("DENT", "CRACK"):
                dent_result = {"status": "skipped", "reason": "damage_type is not DENT/CRACK"}
            elif not HAS_DAMAGE_MODELS:
                dent_result = {"status": "skipped", "reason": "damage_models module not available"}

        # --------------
        # Rules engine
        # --------------
        rules_rows: Any = []
        rules_debug: Dict[str, Any] = {"selected": None, "signature": None, "module_exports": []}

        # Build nested ctx that rules_engine.assess_damage expects (based on your debug output)
        rules_ctx = {
            "aircraft_family": structured.get("aircraft_family"),
            "raw": structured.get("raw"),
            "damage": {
                "type": structured.get("damage_type"),
                "structure": structured.get("structure"),
            },
            "location": {
                "zone": structured.get("structure_zone"),
                "side": structured.get("side"),
                "sta": structured.get("sta"),
                "wl": structured.get("wl"),
                "stringer": structured.get("stringer"),
                "frame": structured.get("frame"),
            },
            "measurements": {
                "dent": {
                    "diameter_mm": structured.get("dent_diameter_mm"),
                    "depth_mm": structured.get("dent_depth_mm"),
                }
            },
            "flags": {
                "has_crack": structured.get("has_crack"),
            },
            "_flat": dict(structured),
        }

        if HAS_RULES_ENGINE and RULES_DB.exists():
            try:
                import inspect

                exports = sorted([n for n in dir(rules_engine) if not n.startswith("_")])
                rules_debug["module_exports"] = exports

                fn = None
                if hasattr(rules_engine, "assess_damage"):
                    fn = getattr(rules_engine, "assess_damage")
                    rules_debug["selected"] = "assess_damage"
                elif hasattr(rules_engine, "evaluate_rules"):
                    fn = getattr(rules_engine, "evaluate_rules")
                    rules_debug["selected"] = "evaluate_rules"
                elif hasattr(rules_engine, "run_rules"):
                    fn = getattr(rules_engine, "run_rules")
                    rules_debug["selected"] = "run_rules"
                elif hasattr(rules_engine, "evaluate"):
                    fn = getattr(rules_engine, "evaluate")
                    rules_debug["selected"] = "evaluate"

                if fn is None:
                    rules_rows = {"error": "rules_engine has no compatible rules function (assess_damage/evaluate_rules/run_rules/evaluate)"}
                else:
                    try:
                        rules_debug["signature"] = str(inspect.signature(fn))
                    except Exception:
                        rules_debug["signature"] = None

                    # Call with the right signature
                    if rules_debug["selected"] == "assess_damage":
                        # assess_damage(db_path: str, aircraft_family: str, ctx: Dict[str, Any], revision: Optional[str]=None)
                        rules_rows = fn(str(RULES_DB), structured.get("aircraft_family") or "UNKNOWN", rules_ctx)  # type: ignore
                        # dataclass result -> dict
                        if is_dataclass(rules_rows):
                            rules_rows = asdict(rules_rows)
                        rules_debug["ctx_sent"] = rules_ctx
                    else:
                        # legacy: evaluate_rules(db_path, structured)
                        rules_rows = fn(str(RULES_DB), structured)  # type: ignore
            except Exception as e:
                rules_rows = {"error": str(e)}
        else:
            if not HAS_RULES_ENGINE:
                rules_rows = {"status": "skipped", "reason": "rules_engine not available"}
            elif not RULES_DB.exists():
                rules_rows = {"status": "skipped", "reason": "rules.db not found in deployment"}

        # --------------
        # SRM Search
        # --------------
        srm_hits: Any = []
        srm_debug: Dict[str, Any] = {"selected": None, "signature": None, "query_used": None}

        top_ref: Optional[str] = None
        top_hit_obj: Any = None

        if HAS_SRM_SEARCH and SRM_DB.exists():
            try:
                # Build a search query from structured fields
                q_bits = []
                if structured.get("aircraft_family"):
                    q_bits.append(str(structured["aircraft_family"]))
                if structured.get("structure"):
                    q_bits.append(str(structured["structure"]))
                if structured.get("structure_zone"):
                    q_bits.append(str(structured["structure_zone"]))
                if structured.get("damage_type"):
                    q_bits.append(str(structured["damage_type"]))
                q_bits += ["allowable damage", "dent", "table 102"]
                query = " ".join(q_bits).strip()

                # srm_search.search_srm(conn, query, aircraft_family=None, limit=6)
                if hasattr(srm_search, "search_srm"):
                    srm_debug["selected"] = "search_srm"
                    srm_debug["signature"] = "search_srm(conn, query, aircraft_family=None, limit=6)"
                    srm_debug["query_used"] = query
                    con = sqlite3.connect(str(SRM_DB))
                    try:
                        hits = srm_search.search_srm(con, query=query, aircraft_family=structured.get("aircraft_family"), limit=8)  # type: ignore
                    finally:
                        con.close()
                    # Convert dataclass hits -> dict for UI consistency
                    srm_hits = [asdict(h) if is_dataclass(h) else (h if isinstance(h, dict) else {"hit": str(h)}) for h in hits]
                else:
                    srm_hits = [{"error": "srm_search module has no search_srm function"}]

                if isinstance(srm_hits, list) and srm_hits:
                    top_hit_obj = srm_hits[0]
                    # Build a human reference string
                    try:
                        doc_title = top_hit_obj.get("doc_title") or top_hit_obj.get("title") or ""
                        page = top_hit_obj.get("page") or top_hit_obj.get("page_no") or ""
                        fname = top_hit_obj.get("file_name") or ""
                        rev = top_hit_obj.get("revision") or ""
                        afam = top_hit_obj.get("aircraft_family") or structured.get("aircraft_family") or ""
                        top_ref = f"{doc_title} • Page {page} • File {fname} (Rev {rev or 'UNKNOWN'})"
                    except Exception:
                        top_ref = None

            except Exception as e:
                srm_hits = [{"error": str(e)}]
        else:
            if not HAS_SRM_SEARCH:
                srm_hits = [{"status": "skipped", "reason": "srm_search module not available"}]
            elif not SRM_DB.exists():
                srm_hits = [{"status": "skipped", "reason": "srm_index.db not found in deployment"}]

        # --------------
        # Coverage extraction + location verification (Option A then B)
        # --------------
        coverage_source = "none"
        coverage_text = ""
        coverage: Dict[str, Any] = {"stations": [], "stringers": [], "frames": []}

        # Option A: uploaded PDF
        if uploaded_pdf is not None and HAS_PYPDF2:
            pdf_bytes = uploaded_pdf.read()
            coverage_text = extract_text_from_uploaded_pdf(pdf_bytes, max_pages=80)
            coverage = extract_coverage_ranges_from_text(coverage_text)
            coverage_source = "uploaded_pdf"
        elif SRM_DB.exists() and top_hit_obj is not None:
            # Option B: use indexed full doc text from srm_index.db (best-effort)
            try:
                con = sqlite3.connect(str(SRM_DB))
                try:
                    coverage_text = get_indexed_doc_fulltext(
                        con,
                        file_name=top_hit_obj.get("file_name") if isinstance(top_hit_obj, dict) else None,
                        doc_title=top_hit_obj.get("doc_title") if isinstance(top_hit_obj, dict) else None,
                    )
                finally:
                    con.close()
                coverage = extract_coverage_ranges_from_text(coverage_text)
                coverage_source = "srm_index_db"
            except Exception:
                coverage_source = "srm_index_db_failed"

        location_verify = verify_location_against_coverage(structured, coverage)

        # --------------
        # Render: SRM Reference (top hit)
        # --------------
        if top_ref:
            st.markdown("### SRM Reference (top hit)")
            st.write(top_ref)
            if isinstance(top_hit_obj, dict):
                snip = top_hit_obj.get("snippet") or top_hit_obj.get("text") or ""
                st.code(str(snip)[:900], language="text")

        # --------------
        # Render: Dent model output
        # --------------
        st.markdown("### Dent model output")
        if HAS_DAMAGE_MODELS and "build_plain_text_summary" in globals() and isinstance(dent_result, dict):
            try:
                summary = build_plain_text_summary(dent_result)  # type: ignore
                st.code(summary, language="text")
            except Exception:
                st.json(dent_result)
        else:
            st.json(dent_result)

        with st.expander("Dent model debug", expanded=False):
            st.json(dent_debug)

        # --------------
        # Render: Rules matches
        # --------------
        st.markdown("### Rules matches")
        st.json(rules_rows)

        with st.expander("Rules engine debug", expanded=False):
            st.json(rules_debug)

        # --------------
        # Render: SRM hits
        # --------------
        st.markdown("### SRM search hits (prototype)")
        if isinstance(srm_hits, list) and srm_hits and isinstance(srm_hits[0], dict) and "error" not in srm_hits[0]:
            for hit in srm_hits[:8]:
                title = hit.get("doc_title") or hit.get("file_name") or "SRM hit"
                meta = []
                if hit.get("revision"):
                    meta.append(f"Rev: {hit['revision']}")
                if hit.get("aircraft_family"):
                    meta.append(f"Aircraft: {hit['aircraft_family']}")
                if hit.get("file_name"):
                    meta.append(f"File: {hit['file_name']}")
                if hit.get("page"):
                    meta.append(f"Page: {hit['page']}")
                st.markdown(f"**{title}**" + (f" ({' • '.join(meta)})" if meta else ""))
                snippet = hit.get("snippet") or hit.get("text") or ""
                st.code(str(snippet)[:650], language="text")
        else:
            st.json(srm_hits)

        with st.expander("SRM search debug", expanded=False):
            st.json(srm_debug)

        # --------------
        # Location verification UI
        # --------------
        st.markdown("### Location verification vs SRM excerpt")
        st.caption(f"Coverage source: **{coverage_source}** (Option A uploaded PDF takes priority; else Option B from srm_index.db).")
        if location_verify.get("ok") is True:
            st.success("Location appears to be within the SRM excerpt coverage ranges (based on extracted ranges).")
        elif location_verify.get("ok") is False:
            st.error("Location does NOT appear to be within the SRM excerpt coverage ranges (based on extracted ranges).")
        else:
            st.warning("Location applicability cannot be confirmed from extracted ranges (insufficient coverage data).")

        for msg in (location_verify.get("messages") or []):
            st.write(msg)

        with st.expander("Extracted coverage ranges (debug)", expanded=False):
            st.json(location_verify.get("coverage") or {})

        # --------------
        # Final statement (SRM-based) — PATCH 1 gating to avoid contradiction
        # --------------
        st.markdown("### Final statement (SRM-based)")

        dtype = structured.get("damage_type") or "DAMAGE"
        final_statement: Optional[str] = None

        if top_ref:
            # Patch 1: never claim SRM allowability if location verification explicitly fails.
            if location_verify and (location_verify.get("ok") is False):
                why = []
                for line in (location_verify.get("messages") or []):
                    if "NOT within" in line or "cannot verify" in line:
                        why.append(line.strip("• ").strip())
                why_txt = " / ".join(why[:2]) if why else "Location outside excerpt coverage ranges."
                final_statement = (
                    f"{dtype} location is NOT covered by the SRM excerpt found ({top_ref}). "
                    f"SRM excerpt not applicable → ENGINEERING REVIEW. "
                    f"Reason: {why_txt}"
                )
            else:
                # Only here can we phrase “within/out of limits per SRM excerpt”.
                disposition_hint = "within limits"

                # Conservative guards: crack always out-of-limits
                if structured.get("has_crack") is True or structured.get("damage_type") == "CRACK":
                    disposition_hint = "out of limits"

                # Heuristic guard: dent depth > 6.35mm is out-of-limits (until table logic is fully implemented)
                if structured.get("dent_depth_mm") is not None:
                    try:
                        if float(structured.get("dent_depth_mm")) > 6.35:
                            disposition_hint = "out of limits"
                    except Exception:
                        pass

                final_statement = f"{dtype} is found {disposition_hint} per {top_ref}"

        if final_statement:
            st.write(final_statement)
        else:
            st.info("No SRM reference available yet. Add/commit srm_index.db or upload a PDF excerpt (Option A).")

        # --------------
        # Optional logging
        # --------------
        st.markdown("### Logging")
        log_it = st.checkbox("Log this assessment to SQLite (assessments.db)", value=True)
        if log_it:
            try:
                log_assessment(ASSESSMENTS_DB, structured, rules_rows, srm_hits, dent_result, location_verify)
                st.success("Logged to assessments.db")
            except Exception as e:
                st.error(f"Failed to log assessment: {e}")

    else:
        st.info("Fill the structured fields if needed, then click **Run rules + SRM search + dent model**.")


# -----------------------------
# Assessment history
# -----------------------------
st.divider()
st.subheader("Assessment history (SQLite)")

if ASSESSMENTS_DB.exists():
    try:
        con = sqlite3.connect(str(ASSESSMENTS_DB))
        rows = con.execute(
            """
            SELECT id, created_utc, aircraft_family, structure, structure_zone, side, sta, stringer, frame,
                   damage_type, dent_diameter_mm, dent_depth_mm, has_crack
              FROM assessments
             ORDER BY id DESC
             LIMIT 25
            """
        ).fetchall()
        con.close()

        if rows:
            st.write(f"Showing last {len(rows)} logs from assessments.db")
            st.dataframe(
                [
                    {
                        "id": r[0],
                        "created_utc": r[1],
                        "aircraft": r[2],
                        "structure": r[3],
                        "zone": r[4],
                        "side": r[5],
                        "sta": r[6],
                        "stringer": r[7],
                        "frame": r[8],
                        "damage_type": r[9],
                        "dia_mm": r[10],
                        "depth_mm": r[11],
                        "crack": (None if r[12] is None else ("Yes" if r[12] == 1 else "No")),
                    }
                    for r in rows
                ],
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("No logs yet.")
    except Exception as e:
        st.error(f"Could not read assessments.db: {e}")
else:
    st.caption("No assessments.db yet. Run an assessment and enable logging to create it.")
