# dent_checker_app.py
# Streamlit app: SRM Damage Assessment (Prototype)
#
# Base UI preserved from your last version.
# Patch included:
# 1) ATA gating: do NOT generate an SRM-based assessment unless a matching SRM hit exists
#    for the required ATA chapter inferred from the description (structure).
# 2) Applicability gating: do NOT output "WITHIN/OUT OF LIMITS" if location verification fails
#    vs the SRM excerpt coverage (STA / Stringer / Frame ranges extracted from hit snippet/text).
# 3) Rename "aircraft_family" -> "aircraft_type" in the UI + structured dict.
#
# Notes:
# - This remains a prototype: the dent math bands are still "prototype" unless you move logic into rules.db.
# - Streamlit Cloud: only committed files are available; PDFs are NOT needed at runtime if srm_index.db is committed.

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import sqlite3
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import streamlit as st


# -----------------------------
# Page config
# -----------------------------
st.set_page_config(page_title="SRM Damage Assessment Tool", layout="wide")
st.title("SRM Damage Assessment Tool (Prototype)")
st.caption("Prototype to structure AOG damage descriptions, evaluate rules, and search SRM excerpts.")


# -----------------------------
# Safe imports (optional modules)
# -----------------------------
HAS_DAMAGE_MODELS = False
HAS_RULES_ENGINE = False
HAS_SRM_SEARCH = False

damage_models_err = None
rules_engine_err = None
srm_search_err = None

try:
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


def _normalize_aircraft_type(text: str) -> str:
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
            try:
                return float(m.group(1))
            except Exception:
                continue
    return None


def _find_float_in(text: str, patterns: List[str]) -> Optional[float]:
    for pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            try:
                return float(m.group(1))
            except Exception:
                continue
    return None


def _parse_stringer(text: str) -> Optional[str]:
    # S-10L, S10L, Stringer 10L
    m = re.search(r"\bS[-\s]?(\d{1,3})([LR])\b", text, flags=re.IGNORECASE)
    if m:
        return f"{int(m.group(1))}{m.group(2).upper()}"
    m2 = re.search(r"\bSTRINGER\s*(\d{1,3})([LR])\b", text, flags=re.IGNORECASE)
    if m2:
        return f"{int(m2.group(1))}{m2.group(2).upper()}"
    return None


def _parse_frame(text: str) -> Optional[int]:
    # FR 12, Frame 12
    mf = re.search(r"\b(?:FR|FRAME)\s*[-]?\s*(\d{1,4})\b", text, flags=re.IGNORECASE)
    if mf:
        try:
            return int(mf.group(1))
        except Exception:
            return None
    return None


def parse_damage_description(desc: str) -> Dict[str, Any]:
    """
    Parse single-line AOG description to structured fields.
    """
    raw = desc.strip()

    out: Dict[str, Any] = {
        "raw": raw,
        "aircraft_type": None,
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

    # Aircraft type
    m = re.search(r"\b(B7\d{2}|A3\d{2}|A32\d{2}|E1\d{2}|E17\d)\b", raw, flags=re.IGNORECASE)
    if m:
        out["aircraft_type"] = _normalize_aircraft_type(m.group(1))

    # Structure (last match wins)
    struct_map = [
        ("FUSELAGE", r"\bfuselage\b"),
        ("WING", r"\bwing\b"),
        ("STABILIZER", r"\bstabilizer\b|\bhorizontal\s+stabilizer\b|\bhs\b"),
        ("FIN", r"\bfin\b|\bvertical\s+stabilizer\b|\bvs\b"),
        ("EMPENNAGE", r"\bempennage\b|\btail\b"),
        ("NACELLE", r"\bnacelle\b"),
        ("PYLON", r"\bpylon\b"),
        ("DOOR", r"\bdoor\b"),
        ("WINDOW", r"\bwindow\b|\bwindows\b"),
    ]
    for label, pat in struct_map:
        if re.search(pat, raw, flags=re.IGNORECASE):
            out["structure"] = label

    # Zone / sub-area (last match wins)
    zone_map = [
        ("SKIN", r"\bskin\b"),
        ("STRINGER", r"\bstringer\b|\bstr\b"),
        ("FRAME", r"\bframe\b|\bfr\b"),
    ]
    for label, pat in zone_map:
        if re.search(pat, raw, flags=re.IGNORECASE):
            out["structure_zone"] = label

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

    # Stringer / Frame
    out["stringer"] = _parse_stringer(raw)
    out["frame"] = _parse_frame(raw)

    # Damage type (priority: crack overrides dent)
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
    elif re.search(r"\bvisible\s+crack(s)?\b", raw, flags=re.IGNORECASE):
        out["has_crack"] = True
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
              aircraft_type TEXT,
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
              result_json TEXT
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
) -> None:
    init_assessments_db(db_path)
    con = sqlite3.connect(str(db_path))
    try:
        con.execute(
            """
            INSERT INTO assessments (
              created_utc, aircraft_type, structure, structure_zone, side, sta, wl, stringer, frame,
              damage_type, dent_diameter_mm, dent_depth_mm, has_crack,
              input_text, structured_json, rules_json, srm_hits_json, result_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now_iso(),
                structured.get("aircraft_type"),
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
            ),
        )
        con.commit()
    finally:
        con.close()


def _pick_best_table_hit(hits: List[Dict[str, Any]], table_hint: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if not hits:
        return None
    if table_hint:
        for h in hits:
            sn = str(h.get("snippet") or h.get("text") or "")
            if re.search(rf"\bTable\s*{re.escape(table_hint)}\b", sn, flags=re.IGNORECASE):
                return h
    # Prefer any snippet mentioning "Table"
    for h in hits:
        sn = str(h.get("snippet") or h.get("text") or "")
        if re.search(r"\bTable\s*\d+\b", sn, flags=re.IGNORECASE):
            return h
    return hits[0]


def _compute_wy_ratio(dia_mm: Optional[float], depth_mm: Optional[float]) -> Optional[float]:
    if dia_mm is None or depth_mm is None:
        return None
    if depth_mm <= 0:
        return None
    return dia_mm / depth_mm


# -----------------------------
# ATA gating + coverage extraction
# -----------------------------
ATA_STRUCTURE_MAP: Dict[str, int] = {
    "FUSELAGE": 53,
    "WING": 57,
    "STABILIZER": 55,
    "FIN": 55,
    "EMPENNAGE": 55,
    "NACELLE": 54,
    "PYLON": 54,
    "DOOR": 52,
    "WINDOW": 56,
}


def required_ata_for_struct(structure: Optional[str]) -> Optional[int]:
    if not structure:
        return None
    return ATA_STRUCTURE_MAP.get(structure.upper().strip())


def infer_ata_from_doc(hit: Dict[str, Any]) -> Optional[int]:
    """
    Infer ATA chapter from doc title / file name like SRM_53-00-01_ADL1.pdf.
    """
    txt = " ".join([str(hit.get("doc_title") or ""), str(hit.get("file_name") or ""), str(hit.get("title") or "")])
    m = re.search(r"\b(\d{2})-\d{2}-\d{2}\b", txt)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    # fallback: first 2 digits anywhere after SRM_
    m2 = re.search(r"\bSRM[_\s-]*(\d{2})\b", txt, flags=re.IGNORECASE)
    if m2:
        try:
            return int(m2.group(1))
        except Exception:
            return None
    return None


def _stringer_to_tuple(s: str) -> Optional[Tuple[int, str]]:
    m = re.match(r"^\s*(\d{1,3})\s*([LR])\s*$", (s or "").upper())
    if not m:
        return None
    return int(m.group(1)), m.group(2)


def _stringer_in_range(s: str, a: str, b: str) -> bool:
    """
    Compare stringers like 10L within 10L-10R.
    Same number: L <= R.
    Different numbers: numeric compare; side ignored for bounds unless same number edge.
    """
    ts = _stringer_to_tuple(s)
    ta = _stringer_to_tuple(a)
    tb = _stringer_to_tuple(b)
    if not (ts and ta and tb):
        return False

    sn, ss = ts
    an, asd = ta
    bn, bsd = tb

    lo_n, hi_n = (an, bn) if an <= bn else (bn, an)
    if sn < lo_n or sn > hi_n:
        return False

    # If same numeric as lower/upper edge, handle side edges
    if sn == an == bn:
        # range like 10L-10R
        lo_side = min(asd, bsd)
        hi_side = max(asd, bsd)
        return lo_side <= ss <= hi_side

    # For edge equality with different sides: be permissive
    return True


def extract_coverage_ranges(text: str) -> Dict[str, Any]:
    """
    Extract coverage hints from an SRM excerpt text:
      - Station ranges: "between Stations 360-540"
      - Stringer ranges: "between Stringers 24L-24R"
      - Frame ranges: (rare in your excerpt) "between Frames 10-20"
    Returns:
      {
        "sta_ranges": [(min,max), ...],
        "stringer_ranges": [("10L","10R"), ...],
        "frame_ranges": [(min,max), ...],
      }
    """
    t = text or ""
    out: Dict[str, Any] = {"sta_ranges": [], "stringer_ranges": [], "frame_ranges": []}

    # Stations: "between Stations 360-540" or "Stations 360 to 540"
    for m in re.finditer(r"\bStations?\s*([0-9]{2,5})\s*(?:-|to)\s*([0-9]{2,5})\b", t, flags=re.IGNORECASE):
        try:
            out["sta_ranges"].append((float(m.group(1)), float(m.group(2))))
        except Exception:
            pass

    # Stringers: "between Stringers 24L-24R"
    for m in re.finditer(r"\bStringers?\s*([0-9]{1,3}\s*[LR])\s*(?:-|to)\s*([0-9]{1,3}\s*[LR])\b", t, flags=re.IGNORECASE):
        a = m.group(1).replace(" ", "").upper()
        b = m.group(2).replace(" ", "").upper()
        out["stringer_ranges"].append((a, b))

    # Frames: "between Frames 10-20" or "Frame 10 to 20"
    for m in re.finditer(r"\bFrames?\s*([0-9]{1,4})\s*(?:-|to)\s*([0-9]{1,4})\b", t, flags=re.IGNORECASE):
        try:
            out["frame_ranges"].append((int(m.group(1)), int(m.group(2))))
        except Exception:
            pass

    return out


def verify_location_vs_coverage(
    structured: Dict[str, Any],
    coverage: Dict[str, Any],
) -> Tuple[bool, List[str]]:
    """
    Returns (ok, messages). If no ranges exist for a dimension, it's "cannot verify" not a fail.
    If ranges exist and provided value is outside, fail.
    """
    msgs: List[str] = []
    ok = True

    sta = structured.get("sta")
    stringer = structured.get("stringer")
    frame = structured.get("frame")

    sta_ranges: List[Tuple[float, float]] = coverage.get("sta_ranges") or []
    str_ranges: List[Tuple[str, str]] = coverage.get("stringer_ranges") or []
    fr_ranges: List[Tuple[int, int]] = coverage.get("frame_ranges") or []

    # STA
    if sta_ranges:
        if sta is None:
            msgs.append("• SRM excerpt has station ranges, but STA not provided; cannot verify STA.")
        else:
            in_any = any(min(a, b) <= float(sta) <= max(a, b) for a, b in sta_ranges)
            if in_any:
                msgs.append(f"• STA {sta:g} is within SRM station coverage ranges: {sta_ranges}")
            else:
                ok = False
                msgs.append(f"• STA {sta:g} is NOT within SRM station coverage ranges: {sta_ranges}")
    else:
        msgs.append("• SRM excerpt has no explicit station ranges; cannot verify STA.")

    # Stringer
    if str_ranges:
        if not stringer:
            msgs.append("• SRM excerpt has stringer ranges, but stringer not provided; cannot verify stringer.")
        else:
            in_any = any(_stringer_in_range(stringer, a, b) for a, b in str_ranges)
            if in_any:
                msgs.append(f"• Stringer {stringer} is within SRM stringer coverage ranges: {str_ranges}")
            else:
                ok = False
                msgs.append(f"• Stringer {stringer} is NOT within SRM stringer coverage ranges: {str_ranges}")
    else:
        msgs.append("• SRM excerpt has no explicit stringer ranges; cannot verify stringer.")

    # Frame
    if fr_ranges:
        if frame is None:
            msgs.append("• SRM excerpt has frame ranges, but FR not provided; cannot verify frame.")
        else:
            in_any = any(min(a, b) <= int(frame) <= max(a, b) for a, b in fr_ranges)
            if in_any:
                msgs.append(f"• FR {frame} is within SRM frame coverage ranges: {fr_ranges}")
            else:
                ok = False
                msgs.append(f"• FR {frame} is NOT within SRM frame coverage ranges: {fr_ranges}")
    else:
        msgs.append("• SRM excerpt has no explicit frame ranges; cannot verify frame.")

    return ok, msgs


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
    default_text = "B737, fuselage, LH side, STA 123, S-10L, skin dent 0.25mm dia, 3.18mm depth, no visible crack."
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
        aircraft_type = st.text_input("Aircraft type", value=structured.get("aircraft_type") or "")
        structure = st.text_input("Structure", value=structured.get("structure") or "")
    with f2:
        structure_zone = st.text_input("Zone", value=structured.get("structure_zone") or "")
        side = st.selectbox("Side", ["ANY", "LH", "RH"], index=["ANY", "LH", "RH"].index(structured.get("side") or "ANY"))
    with f3:
        sta = st.number_input("STA", value=float(structured.get("sta") or 0.0), step=1.0, format="%.1f")
        wl = st.number_input("WL", value=float(structured.get("wl") or 0.0), step=1.0, format="%.1f")
    with f4:
        stringer = st.text_input("Stringer", value=structured.get("stringer") or "")
        frame = st.text_input("Frame (FR)", value=str(structured.get("frame") or ""))

    d1, d2, d3 = st.columns(3)
    with d1:
        damage_type = st.selectbox("Damage type", ["DENT", "CRACK", "GOUGE", "CORROSION", "OTHER"], index=0)
    with d2:
        dent_dia = st.number_input("Dent diameter (mm)", value=float(structured.get("dent_diameter_mm") or 0.0), step=0.1, format="%.2f")
    with d3:
        dent_depth = st.number_input("Dent depth (mm)", value=float(structured.get("dent_depth_mm") or 0.0), step=0.1, format="%.2f")

    crack_opt = st.selectbox("Crack present?", ["Unknown", "No", "Yes"], index=0)

    # Write back into structured dict
    structured["raw"] = desc.strip()
    structured["aircraft_type"] = _normalize_aircraft_type(aircraft_type) if aircraft_type else None
    structured["structure"] = structure.strip().upper() if structure else None
    structured["structure_zone"] = structure_zone.strip().upper() if structure_zone else None
    structured["side"] = side
    structured["sta"] = None if sta == 0.0 else float(sta)
    structured["wl"] = None if wl == 0.0 else float(wl)
    structured["stringer"] = stringer.strip().upper() or None
    try:
        structured["frame"] = int(frame) if str(frame).strip() else None
    except Exception:
        structured["frame"] = None

    structured["damage_type"] = damage_type
    structured["dent_diameter_mm"] = None if dent_dia == 0.0 else float(dent_dia)
    structured["dent_depth_mm"] = None if dent_depth == 0.0 else float(dent_depth)

    if crack_opt == "Unknown":
        structured["has_crack"] = None
    elif crack_opt == "No":
        structured["has_crack"] = False
    else:
        structured["has_crack"] = True

    st.subheader("3) Run assessment")
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
            st.info("Commit srm_index.db to the repo if you want SRM hits on Streamlit Cloud.")

    if run:
        # ----------------
        # Determine required ATA for structure (Patch 1)
        # ----------------
        required_ata = required_ata_for_struct(structured.get("structure"))
        required_ata_str = f"{required_ata}" if required_ata is not None else "UNKNOWN"

        # ----------------
        # SRM Search (first)
        # ----------------
        srm_hits: List[Dict[str, Any]] = []
        srm_debug: Dict[str, Any] = {"selected": None, "signature": None, "query_used": None, "required_ata": required_ata}

        query_used = ""
        if HAS_SRM_SEARCH and SRM_DB.exists():
            try:
                q_bits = []
                if structured.get("aircraft_type"):
                    q_bits.append(str(structured["aircraft_type"]))
                if structured.get("structure"):
                    q_bits.append(str(structured["structure"]))
                if structured.get("structure_zone"):
                    q_bits.append(str(structured["structure_zone"]))
                if structured.get("damage_type"):
                    q_bits.append(str(structured["damage_type"]))

                # Keep the anchor phrasing that works well for your excerpt
                q_bits.append("allowable damage dent table 102")
                query_used = " ".join(q_bits).strip()

                con = sqlite3.connect(str(SRM_DB))
                try:
                    fn = getattr(srm_search, "search_srm", None)
                    if fn is None:
                        raise RuntimeError("srm_search.search_srm not found")

                    srm_debug["selected"] = "search_srm"
                    srm_debug["signature"] = str(inspect.signature(fn))
                    srm_debug["query_used"] = query_used

                    hits = fn(con, query=query_used, aircraft_family=structured.get("aircraft_type"), limit=12)  # type: ignore
                finally:
                    con.close()

                tmp: List[Dict[str, Any]] = []
                for h in hits:
                    if is_dataclass(h):
                        d = asdict(h)
                    else:
                        d = {"raw": str(h)}
                    d.setdefault("pdf_page", d.get("page") or d.get("pdf_page"))
                    d.setdefault("printed_page", d.get("printed_page"))
                    tmp.append(d)
                srm_hits = tmp

            except Exception as e:
                srm_hits = [{"error": str(e)}]
        else:
            if not HAS_SRM_SEARCH:
                srm_hits = [{"status": "skipped", "reason": "srm_search module not available"}]
            elif not SRM_DB.exists():
                srm_hits = [{"status": "skipped", "reason": "srm_index.db not found in deployment"}]

        # ----------------
        # ATA filter hits (Patch 1)
        # ----------------
        eligible_hits: List[Dict[str, Any]] = []
        ineligible_hits: List[Dict[str, Any]] = []

        if isinstance(srm_hits, list) and srm_hits and isinstance(srm_hits[0], dict) and "error" not in srm_hits[0]:
            for h in srm_hits:
                hit_ata = infer_ata_from_doc(h)
                h["_inferred_ata"] = hit_ata
                if required_ata is None:
                    eligible_hits.append(h)  # if we can't infer required chapter, don't block
                else:
                    if hit_ata == required_ata:
                        eligible_hits.append(h)
                    else:
                        ineligible_hits.append(h)

        # pick best hit from eligible only
        best_hit = _pick_best_table_hit(eligible_hits, table_hint="102") if eligible_hits else None

        # ----------------
        # Location verification vs SRM excerpt (Patch 2)
        # ----------------
        location_ok = True
        location_msgs: List[str] = []
        coverage_debug: Dict[str, Any] = {}

        if best_hit:
            excerpt_text = str(best_hit.get("snippet") or best_hit.get("text") or "")
            coverage = extract_coverage_ranges(excerpt_text)
            coverage_debug = coverage
            location_ok, location_msgs = verify_location_vs_coverage(structured, coverage)
        else:
            location_ok = False
            location_msgs = ["• No SRM excerpt selected; cannot verify location."]

        # Show SRM Reference (top hit)
        if best_hit:
            st.markdown("### SRM Reference (top hit)")
            doc = best_hit.get("doc_title") or best_hit.get("title") or best_hit.get("file_name") or "SRM"
            rev = best_hit.get("revision") or "UNKNOWN"
            file_name = best_hit.get("file_name") or ""
            printed = best_hit.get("printed_page")
            pdf_page = best_hit.get("pdf_page") or best_hit.get("page")
            page_str = f"Printed page {printed}" if printed else f"PDF page {pdf_page}"

            hit_ata = best_hit.get("_inferred_ata")
            st.write(f"**{doc}** • ATA {hit_ata if hit_ata is not None else 'UNKNOWN'} • {page_str} • File {file_name} (Rev {rev})")
            st.code(str(best_hit.get("snippet") or "")[:600], language="text")
        else:
            st.markdown("### SRM Reference (top hit)")
            if required_ata is not None:
                st.info(
                    f"No SRM PDF/hit available in library for required ATA {required_ata} "
                    f"(based on structure: {structured.get('structure')})."
                )
            else:
                st.info("No SRM PDF/hit available in library (required ATA unknown — structure missing).")

        # ----------------
        # Dent model
        # ----------------
        dent_result: Dict[str, Any] = {"status": "not_run"}
        dent_debug: Dict[str, Any] = {}

        if HAS_DAMAGE_MODELS and structured.get("damage_type") == "DENT":
            try:
                sig = inspect.signature(DentDamage)  # type: ignore
                accepted = set(sig.parameters.keys())

                candidates = {
                    "aircraft_type": structured.get("aircraft_type") or "UNKNOWN",
                    "structure_zone": structured.get("structure_zone") or "UNKNOWN",
                    "side": structured.get("side") or "ANY",
                    "sta": (str(int(structured["sta"])) if structured.get("sta") is not None else None),
                    "stringer": structured.get("stringer"),
                    "dent_diameter_mm": float(structured.get("dent_diameter_mm") or 0.0),
                    "dent_depth_mm": float(structured.get("dent_depth_mm") or 0.0),
                    "crack_present": bool(structured.get("has_crack")) if structured.get("has_crack") is not None else False,
                    "notes": structured.get("notes"),
                }

                filtered = {k: v for k, v in candidates.items() if k in accepted}
                dent_debug = {
                    "DentDamage_signature": str(sig),
                    "accepted_params": list(sig.parameters.keys()),
                    "filtered_kwargs_used": filtered,
                }

                dent = DentDamage(**filtered)  # type: ignore
                res = assess_dent(dent)  # type: ignore
                dent_result = {"result": res} if not isinstance(res, dict) else res
            except Exception as e:
                dent_result = {"status": "error", "error": f"Could not construct/run DentDamage: {e}"}
        else:
            if structured.get("damage_type") != "DENT":
                dent_result = {"status": "skipped", "reason": "damage_type is not DENT"}
            elif not HAS_DAMAGE_MODELS:
                dent_result = {"status": "skipped", "reason": "damage_models module not available"}

        # ----------------
        # Rules engine (optional)
        # ----------------
        rules_rows: Any = []
        rules_debug: Dict[str, Any] = {"selected": None, "signature": None}

        if HAS_RULES_ENGINE and RULES_DB.exists():
            try:
                fn = None
                if hasattr(rules_engine, "assess_damage"):
                    fn = rules_engine.assess_damage  # type: ignore
                elif hasattr(rules_engine, "evaluate_rules"):
                    fn = rules_engine.evaluate_rules  # type: ignore
                elif hasattr(rules_engine, "run_rules"):
                    fn = rules_engine.run_rules  # type: ignore

                if fn is None:
                    raise RuntimeError("rules_engine has no compatible rules function (assess_damage/evaluate_rules/run_rules)")

                rules_debug["selected"] = getattr(fn, "__name__", "unknown")
                rules_debug["signature"] = str(inspect.signature(fn))

                if rules_debug["selected"] == "assess_damage":
                    ctx = {
                        "aircraft_type": structured.get("aircraft_type"),
                        "raw": structured.get("raw"),
                        "damage": {"type": structured.get("damage_type"), "structure": structured.get("structure")},
                        "location": {
                            "zone": structured.get("structure_zone"),
                            "side": structured.get("side"),
                            "sta": structured.get("sta"),
                            "wl": structured.get("wl"),
                            "stringer": structured.get("stringer"),
                            "frame": structured.get("frame"),
                        },
                        "measurements": {
                            "dent": {"diameter_mm": structured.get("dent_diameter_mm"), "depth_mm": structured.get("dent_depth_mm")}
                        },
                        "flags": {"has_crack": structured.get("has_crack")},
                        "_flat": dict(structured),
                    }
                    rules_debug["ctx_sent"] = ctx

                    # rules_engine signature expects aircraft_family; pass aircraft_type into that param
                    rules_rows = fn(str(RULES_DB), structured.get("aircraft_type") or "UNKNOWN", ctx, None)  # type: ignore
                    if is_dataclass(rules_rows):
                        rules_rows = asdict(rules_rows)
                else:
                    rules_rows = fn(str(RULES_DB), structured)  # type: ignore

            except Exception as e:
                rules_rows = [{"error": str(e)}]
        else:
            if not HAS_RULES_ENGINE:
                rules_rows = [{"status": "skipped", "reason": "rules_engine not available"}]
            elif not RULES_DB.exists():
                rules_rows = [{"status": "skipped", "reason": "rules.db not found in deployment"}]

        # ----------------
        # Final statement (SRM-based) with gating
        # ----------------
        st.markdown("### Final statement (SRM-based)")

        dtype = structured.get("damage_type")
        dia = structured.get("dent_diameter_mm")
        dep = structured.get("dent_depth_mm")
        ratio = _compute_wy_ratio(dia, dep)

        # Determine whether we are allowed to assess:
        # - Must have best SRM hit (and it must match required ATA if required ATA is known)
        # - Must pass location verification if ranges exist and input is outside
        can_assess = best_hit is not None and location_ok

        final_lines: List[str] = []

        if best_hit:
            doc = best_hit.get("doc_title") or best_hit.get("file_name") or "SRM"
            rev = best_hit.get("revision") or "UNKNOWN"
            file_name = best_hit.get("file_name") or ""
            printed = best_hit.get("printed_page")
            pdf_page = best_hit.get("pdf_page") or best_hit.get("page")
            page_str = f"Printed page {printed}" if printed else f"PDF page {pdf_page}"

            snippet = str(best_hit.get("snippet") or "")
            mtab = re.search(r"\bTable\s*(\d+)\b", snippet, flags=re.IGNORECASE)
            table_no = mtab.group(1) if mtab else "102"

            hit_ata = best_hit.get("_inferred_ata")
            final_lines.append(
                f"Reference: **{doc}** • ATA {hit_ata if hit_ata is not None else 'UNKNOWN'} • "
                f"**Table {table_no}** • **{page_str}** • File `{file_name}` (Rev {rev})"
            )
        else:
            final_lines.append("Reference: (no SRM hit available in library for required ATA)")

        # If not assessable, do NOT output within/out of limits decision.
        if not can_assess:
            reason_bits: List[str] = []
            if best_hit is None:
                atype = structured.get("aircraft_type") or "UNKNOWN"
                if required_ata is not None:
                    reason_bits.append(f"no SRM reference available for aircraft type {atype} under required ATA {required_ata}.")
                else:
                    reason_bits.append(f"no SRM reference available for aircraft type {atype}.")
            if best_hit is not None and not location_ok:
                reason_bits.append("SRM excerpt appears not applicable to provided location (STA/Stringer/FR).")

            final_lines.append(f"Decision: **NO ASSESSMENT GENERATED** ({' '.join(reason_bits)})")
            final_lines.append("")
            final_lines.append("Location verification vs SRM excerpt:")
            final_lines.append("Location does NOT appear to be within the SRM excerpt coverage ranges (based on extracted ranges).")
            final_lines.extend(location_msgs)

            st.write("\n".join(final_lines))
        else:
            # Assessable: apply dent prototype logic ONLY for dent.
            if dtype != "DENT":
                final_lines.append(f"Decision: **NO ASSESSMENT GENERATED** (prototype SRM math only implemented for DENT; got damage_type={dtype}).")
                st.write("\n".join(final_lines))
            else:
                # Crack override: never within limits if crack present/unknown.
                if structured.get("has_crack") is True:
                    final_lines.append("Decision: **OUT OF LIMITS** (crack present ⇒ not allowable / engineering review).")
                elif structured.get("has_crack") is None:
                    final_lines.append("Decision: **NO ASSESSMENT GENERATED** (crack status unknown; provide crack status for SRM-based decision).")
                else:
                    if dep is None:
                        final_lines.append("Decision: **NO ASSESSMENT GENERATED** (missing dent depth).")
                    else:
                        # Prototype thresholds aligned to your Table 102 discussion:
                        # - Depth > 6.35mm: out
                        # - Depth in (3.175..6.35] requires W/Y >= 30
                        # - Depth <= 3.175mm within (prototype)
                        if dep > 6.35:
                            final_lines.append("Decision: **OUT OF LIMITS** (Depth > 6.35 mm (0.25 in) ⇒ not allowable per Table 102 band).")
                        elif dep > 3.175:
                            if ratio is None:
                                final_lines.append("Decision: **NO ASSESSMENT GENERATED** (cannot compute W/Y ratio).")
                            else:
                                if ratio >= 30.0:
                                    final_lines.append("Decision: **WITHIN LIMITS** (Depth in (3.175..6.35] mm and W/Y ≥ 30).")
                                else:
                                    final_lines.append("Decision: **OUT OF LIMITS** (Depth in (3.175..6.35] mm and W/Y < 30).")
                        else:
                            final_lines.append("Decision: **WITHIN LIMITS** (Depth ≤ 3.175 mm (0.125 in) prototype band).")

                # Proof / calculations
                final_lines.append("")
                final_lines.append("Proof / calculations (W=diameter, Y=depth):")
                if dia is None or dep is None:
                    final_lines.append(f"- W (diameter): {dia} mm")
                    final_lines.append(f"- Y (depth): {dep} mm")
                    final_lines.append("- W/Y: (cannot compute)")
                else:
                    final_lines.append(f"- W (diameter): {dia:.2f} mm")
                    final_lines.append(f"- Y (depth): {dep:.2f} mm")
                    if ratio is not None:
                        final_lines.append(f"- W/Y = {dia:.2f} / {dep:.2f} = **{ratio:.2f}**")
                    else:
                        final_lines.append("- W/Y: (cannot compute)")

                # Location verification (must agree)
                final_lines.append("")
                final_lines.append("Location verification vs SRM excerpt:")
                final_lines.append("Location appears to be within the SRM excerpt coverage ranges (based on extracted ranges).")
                final_lines.extend(location_msgs)

                st.write("\n".join(final_lines))

        # ----------------
        # Dent model output
        # ----------------
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

        # ----------------
        # Rules matches
        # ----------------
        st.markdown("### Rules matches")
        st.json(rules_rows)
        with st.expander("Rules engine debug", expanded=False):
            st.json(rules_debug)

        # ----------------
        # Location verification panel
        # ----------------
        st.markdown("### Location verification vs SRM excerpt")
        if best_hit:
            if location_ok:
                st.success("Location appears to be within the SRM excerpt coverage ranges (based on extracted ranges).")
            else:
                st.error("Location does NOT appear to be within the SRM excerpt coverage ranges (based on extracted ranges).")
            for m in location_msgs:
                st.write(m)
        else:
            st.info("No SRM excerpt selected; location verification not available.")

        with st.expander("Coverage extraction debug", expanded=False):
            st.json(coverage_debug)

        # ----------------
        # SRM hits
        # ----------------
        st.markdown("### SRM search hits (prototype)")
        if eligible_hits:
            st.caption(f"Required ATA (from structure): {required_ata_str}. Showing only hits matching required ATA.")
            for hit in eligible_hits[:8]:
                title = hit.get("doc_title") or hit.get("file_name") or "SRM hit"
                rev = hit.get("revision") or "UNKNOWN"
                atype = hit.get("aircraft_family") or hit.get("aircraft_type") or ""
                file_name = hit.get("file_name") or ""
                printed = hit.get("printed_page")
                pdf_page = hit.get("pdf_page") or hit.get("page")
                hit_ata = hit.get("_inferred_ata")

                page_str = f"Printed page {printed}" if printed else f"PDF page {pdf_page}"
                meta = f"ATA {hit_ata if hit_ata is not None else 'UNKNOWN'} • Rev: {rev} • Aircraft: {atype} • File: {file_name} • {page_str}"
                st.markdown(f"**{title}** ({meta})")
                st.code(str(hit.get("snippet") or hit.get("text") or "")[:1200], language="text")
        elif ineligible_hits:
            st.caption(
                f"Required ATA (from structure): {required_ata_str}. "
                f"Found SRM hits, but they are for other ATA chapters (blocked)."
            )
            for hit in ineligible_hits[:6]:
                title = hit.get("doc_title") or hit.get("file_name") or "SRM hit"
                hit_ata = hit.get("_inferred_ata")
                st.write(f"- Blocked hit: {title} (ATA {hit_ata if hit_ata is not None else 'UNKNOWN'})")
            st.info("Add the correct SRM excerpt PDF for the required ATA into your private library, rebuild srm_index.db, and commit only the DB.")
        else:
            st.json(srm_hits)

        with st.expander("SRM search debug", expanded=False):
            st.json(srm_debug)

        # ----------------
        # Optional logging
        # ----------------
        st.markdown("### Logging")
        log_it = st.checkbox("Log this assessment to SQLite (assessments.db)", value=True)
        if log_it:
            try:
                log_assessment(ASSESSMENTS_DB, structured, rules_rows, srm_hits, dent_result)
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
            SELECT id, created_utc, aircraft_type, structure, structure_zone, side, sta, stringer, frame,
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
                        "aircraft_type": r[2],
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
