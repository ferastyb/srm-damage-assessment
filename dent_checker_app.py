# dent_checker_app.py
# Streamlit app: SRM Damage Assessment (Prototype)
#
# Fixes in this version:
# - Auto re-parse when the description changes (prevents stale structured fields like 'FUSELAGE')
# - Recognize stabilizer terms and map them to EMPENNAGE
# - ATA gating for SRM search: filters hits to the correct ATA chapter based on structure
# - Location verification no longer claims "within coverage" when no ranges are present

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import sqlite3
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import streamlit as st


# -----------------------------
# Page config
# -----------------------------
st.set_page_config(
    page_title="SRM Damage Assessment Tool",
    layout="wide",
)

st.title("SRM Damage Assessment Tool (Prototype)")
st.caption("Prototype to structure AOG damage descriptions, evaluate rules, search SRM excerpts, and verify location coverage.")


# -----------------------------
# Safe imports (optional modules)
# -----------------------------
HAS_DAMAGE_MODELS = False
HAS_RULES_ENGINE = False
HAS_SRM_SEARCH = False

damage_models_err = None
rules_engine_err = None
srm_search_err = None

DentDamage = None
assess_dent = None
build_plain_text_summary = None

try:
    from damage_models import DentDamage as _DentDamage, assess_dent as _assess_dent  # type: ignore

    DentDamage = _DentDamage
    assess_dent = _assess_dent

    try:
        from damage_models import build_plain_text_summary as _build_plain_text_summary  # type: ignore
        build_plain_text_summary = _build_plain_text_summary
    except Exception:
        build_plain_text_summary = None

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
    if "LH" in s or "LHS" in s or "LEFT" in s:
        return "LH"
    if "RH" in s or "RHS" in s or "RIGHT" in s:
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


def infer_ata_chapter(structure: Optional[str], zone: Optional[str] = None) -> Optional[str]:
    """
    Minimal SRM/ATA mapping for gating SRM search.
    - FUSELAGE -> ATA 53
    - WING -> ATA 57
    - EMPENNAGE / STABILIZER -> ATA 55
    """
    s = (structure or "").upper().strip()
    z = (zone or "").upper().strip()

    if s == "FUSELAGE":
        return "53"
    if s == "WING":
        return "57"
    if s == "EMPENNAGE":
        return "55"

    # If structure not explicit, try zone hints
    if "STABILIZER" in z:
        return "55"

    return None


def ata_in_title_or_filename(ata: str, title: str, filename: str) -> bool:
    """
    Heuristic: SRM PDFs typically include patterns like:
      53-00-01, 57-xx-xx, 55-xx-xx
    We allow several separators.
    """
    t = (title or "").upper()
    f = (filename or "").upper()
    patterns = [
        rf"\b{ata}-\d{{2}}-\d{{2}}\b",
        rf"\b{ata}_\d{{2}}_\d{{2}}\b",
        rf"\b{ata}\s*-\s*\d{{2}}\s*-\s*\d{{2}}\b",
    ]
    for p in patterns:
        if re.search(p, t) or re.search(p, f):
            return True
    return False


# -----------------------------
# Parse damage description
# -----------------------------
def parse_damage_description(desc: str) -> Dict[str, Any]:
    raw = (desc or "").strip()

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

    # Structure keywords (expanded)
    # NOTE: Stabilizer -> EMPENNAGE (ATA 55)
    if re.search(r"\bfuselage\b", raw, flags=re.IGNORECASE):
        out["structure"] = "FUSELAGE"
    elif re.search(r"\bwing\b", raw, flags=re.IGNORECASE):
        out["structure"] = "WING"
    elif re.search(r"\bempennage\b|\btail\b", raw, flags=re.IGNORECASE):
        out["structure"] = "EMPENNAGE"
    elif re.search(r"\bstabilizer\b|\bh\s*-\s*stab\b|\bv\s*-\s*stab\b|\bhorizontal\s+stabilizer\b|\bvertical\s+stabilizer\b", raw, flags=re.IGNORECASE):
        out["structure"] = "EMPENNAGE"
        # optional: set a more specific zone if not already
        if re.search(r"\bvertical\s+stabilizer\b|\bv\s*-\s*stab\b", raw, flags=re.IGNORECASE):
            out["structure_zone"] = "VERTICAL_STABILIZER"
        elif re.search(r"\bhorizontal\s+stabilizer\b|\bh\s*-\s*stab\b", raw, flags=re.IGNORECASE):
            out["structure_zone"] = "HORIZONTAL_STABILIZER"
    elif re.search(r"\bdoor\b", raw, flags=re.IGNORECASE):
        out["structure"] = "DOOR"

    # Zone / sub-area (only set if not already set by stabilizer specialization)
    if out.get("structure_zone") is None:
        if re.search(r"\bskin\b", raw, flags=re.IGNORECASE):
            out["structure_zone"] = "SKIN"
        elif re.search(r"\bstringer\b", raw, flags=re.IGNORECASE):
            out["structure_zone"] = "STRINGER"
        elif re.search(r"\bframe\b", raw, flags=re.IGNORECASE):
            out["structure_zone"] = "FRAME"
        elif re.search(r"\bpanel\b", raw, flags=re.IGNORECASE):
            out["structure_zone"] = "PANEL"

    # Side
    out["side"] = _parse_side(raw)

    # STA variants
    m = re.search(r"\bSTA(?:TION)?\s*[:=]?\s*([0-9]{2,5}(?:\.[0-9]+)?)\b", raw, flags=re.IGNORECASE)
    if m:
        try:
            out["sta"] = float(m.group(1))
        except Exception:
            pass

    # WL variants
    m = re.search(r"\bWL\s*[:=]?\s*([0-9]{1,5}(?:\.[0-9]+)?)\b", raw, flags=re.IGNORECASE)
    if m:
        try:
            out["wl"] = float(m.group(1))
        except Exception:
            pass

    # Stringer formats
    m = re.search(r"\bS[-\s]?(\d{1,3})([LR])\b", raw, flags=re.IGNORECASE)
    if m:
        out["stringer"] = f"{int(m.group(1))}{m.group(2).upper()}"
    else:
        m2 = re.search(r"\b(?:STRINGER|STR)\s*[-:]?\s*(\d{1,3})([LR])\b", raw, flags=re.IGNORECASE)
        if m2:
            out["stringer"] = f"{int(m2.group(1))}{m2.group(2).upper()}"

    # Frame formats (FR/FRAME)
    m = re.search(r"\b(?:FR|FRAME)\s*[-:]?\s*([0-9]{1,4})\b", raw, flags=re.IGNORECASE)
    if m:
        try:
            out["frame"] = int(m.group(1))
        except Exception:
            pass
    else:
        m2 = re.search(r"\bFR([0-9]{1,4})\b", raw, flags=re.IGNORECASE)
        if m2:
            try:
                out["frame"] = int(m2.group(1))
            except Exception:
                pass

    # Damage type
    if re.search(r"\bdent\b", raw, flags=re.IGNORECASE):
        out["damage_type"] = "DENT"
    elif re.search(r"\bgouge\b", raw, flags=re.IGNORECASE):
        out["damage_type"] = "GOUGE"
    elif re.search(r"\bcrack\b", raw, flags=re.IGNORECASE):
        out["damage_type"] = "CRACK"
    elif re.search(r"\bcorrosion\b", raw, flags=re.IGNORECASE):
        out["damage_type"] = "CORROSION"
    else:
        out["damage_type"] = "OTHER"

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


# -----------------------------
# SRM location coverage extraction
# -----------------------------
@dataclass
class LocationCoverage:
    sta_ranges: List[Tuple[float, float]]
    stringer_ranges: List[Tuple[int, int]]  # signed: L negative, R positive
    frame_ranges: List[Tuple[int, int]]


def _lr_to_signed(n: int, side: str) -> int:
    side = (side or "").upper()
    if side == "L":
        return -abs(int(n))
    if side == "R":
        return abs(int(n))
    return int(n)


def _parse_stringer_token(tok: str) -> Optional[int]:
    if not tok:
        return None
    t = tok.strip().upper()
    t = t.replace("STRINGER", "").replace("STR", "").strip()
    t = t.replace("S-", "S").replace("S ", "S")
    m = re.search(r"\bS?(\d{1,3})\s*([LR])\b", t)
    if not m:
        return None
    return _lr_to_signed(int(m.group(1)), m.group(2))


def extract_location_coverage_from_doc(con: sqlite3.Connection, doc_title: str) -> LocationCoverage:
    row = con.execute("SELECT id FROM docs WHERE title = ? LIMIT 1", (doc_title,)).fetchone()
    if not row:
        return LocationCoverage([], [], [])
    doc_id = int(row[0])

    # Scan first 12 pages (Applicability / General / Definitions)
    pages = con.execute(
        "SELECT page_no, text FROM pages WHERE doc_id = ? AND page_no <= 12 ORDER BY page_no",
        (doc_id,),
    ).fetchall()
    text = "\n".join([(p[1] or "") for p in pages]).strip()
    if not text:
        return LocationCoverage([], [], [])

    sta_ranges: List[Tuple[float, float]] = []
    str_ranges: List[Tuple[int, int]] = []
    fr_ranges: List[Tuple[int, int]] = []

    # Stations
    for m in re.finditer(r"\bStations?\s*([0-9]{2,5}(?:\.[0-9]+)?)\s*[-–]\s*([0-9]{2,5}(?:\.[0-9]+)?)\b", text, re.I):
        try:
            a = float(m.group(1)); b = float(m.group(2))
            sta_ranges.append((min(a, b), max(a, b)))
        except Exception:
            pass

    # Stringers
    for m in re.finditer(r"\bStringers?\s*([0-9]{1,3}\s*[LR])\s*[-–]\s*([0-9]{1,3}\s*[LR])\b", text, re.I):
        a = _parse_stringer_token(m.group(1))
        b = _parse_stringer_token(m.group(2))
        if a is not None and b is not None:
            str_ranges.append((min(a, b), max(a, b)))

    for m in re.finditer(r"\bS[-\s]?([0-9]{1,3}\s*[LR])\s*(?:to|TO)\s*S[-\s]?([0-9]{1,3}\s*[LR])\b", text):
        a = _parse_stringer_token(m.group(1))
        b = _parse_stringer_token(m.group(2))
        if a is not None and b is not None:
            str_ranges.append((min(a, b), max(a, b)))

    # Frames
    for m in re.finditer(r"\bFrames?\s*([0-9]{1,4})\s*[-–]\s*([0-9]{1,4})\b", text, re.I):
        try:
            a = int(m.group(1)); b = int(m.group(2))
            fr_ranges.append((min(a, b), max(a, b)))
        except Exception:
            pass

    return LocationCoverage(sta_ranges=sta_ranges, stringer_ranges=str_ranges, frame_ranges=fr_ranges)


def _in_any_range(v: float, ranges: List[Tuple[float, float]]) -> bool:
    return any(lo <= v <= hi for lo, hi in ranges)


def validate_location_against_coverage(structured: Dict[str, Any], cov: LocationCoverage) -> Tuple[str, List[str]]:
    """
    Returns verdict in:
      - "WITHIN"       : at least one relevant range existed and all checkable fields are within
      - "OUTSIDE"      : at least one relevant range existed and at least one field was outside
      - "NOT_VERIFIABLE": no relevant ranges existed for the provided fields
    """
    msgs: List[str] = []

    sta = structured.get("sta")
    s_tok = structured.get("stringer")
    fr = structured.get("frame")

    have_sta_check = sta is not None and bool(cov.sta_ranges)
    have_str_check = bool(s_tok) and bool(cov.stringer_ranges)
    have_fr_check = fr is not None and bool(cov.frame_ranges)

    if sta is not None and not cov.sta_ranges:
        msgs.append("SRM excerpt has no explicit station ranges; cannot verify STA.")
    if s_tok and not cov.stringer_ranges:
        msgs.append("SRM excerpt has no explicit stringer ranges; cannot verify stringer.")
    if fr is not None and not cov.frame_ranges:
        msgs.append("SRM excerpt has no explicit frame ranges; cannot verify frame.")

    if not (have_sta_check or have_str_check or have_fr_check):
        return "NOT_VERIFIABLE", msgs

    outside = False

    if have_sta_check:
        if not _in_any_range(float(sta), cov.sta_ranges):
            outside = True
            msgs.append(f"STA {sta} is outside SRM station ranges found in this stored excerpt.")
        else:
            msgs.append(f"STA {sta} is within SRM station ranges found in this stored excerpt.")

    if have_str_check:
        sv = _parse_stringer_token(str(s_tok))
        if sv is None:
            outside = True
            msgs.append(f"Could not parse stringer token: {s_tok}")
        else:
            if not any(lo <= sv <= hi for lo, hi in cov.stringer_ranges):
                outside = True
                msgs.append(f"Stringer {s_tok} is outside SRM stringer ranges found in this stored excerpt.")
            else:
                msgs.append(f"Stringer {s_tok} is within SRM stringer ranges found in this stored excerpt.")

    if have_fr_check:
        try:
            fv = int(fr)
            if not any(lo <= fv <= hi for lo, hi in cov.frame_ranges):
                outside = True
                msgs.append(f"Frame {fv} is outside SRM frame ranges found in this stored excerpt.")
            else:
                msgs.append(f"Frame {fv} is within SRM frame ranges found in this stored excerpt.")
        except Exception:
            outside = True
            msgs.append("Frame value could not be parsed as an integer.")

    return ("OUTSIDE" if outside else "WITHIN"), msgs


# -----------------------------
# Assessments DB logging
# -----------------------------
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
              created_utc, aircraft_family, structure, structure_zone, side, sta, wl, stringer, frame,
              damage_type, dent_diameter_mm, dent_depth_mm, has_crack,
              input_text, structured_json, rules_json, srm_hits_json, result_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )
        con.commit()
    finally:
        con.close()


# -----------------------------
# Dent model adapter
# -----------------------------
def run_dent_model(structured: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    debug: Dict[str, Any] = {"DentDamage_signature": None, "accepted_params": [], "filtered_kwargs_used": {}, "dropped_candidate_keys": []}

    if not (HAS_DAMAGE_MODELS and DentDamage and assess_dent):
        return {"status": "skipped", "reason": "damage_models not available"}, debug

    if structured.get("damage_type") != "DENT":
        return {"status": "skipped", "reason": "damage_type is not DENT"}, debug

    sig = inspect.signature(DentDamage.__init__)  # type: ignore
    params = [p for p in sig.parameters.keys() if p != "self"]
    debug["DentDamage_signature"] = str(sig)
    debug["accepted_params"] = params

    candidates: Dict[str, Any] = {
        "aircraft_type": structured.get("aircraft_family"),
        "aircraft_family": structured.get("aircraft_family"),
        "structure_zone": structured.get("structure_zone"),
        "side": structured.get("side"),
        "sta": None if structured.get("sta") is None else str(int(structured["sta"])) if float(structured["sta"]).is_integer() else str(structured["sta"]),
        "stringer": structured.get("stringer"),
        "dent_diameter_mm": structured.get("dent_diameter_mm") or 0.0,
        "dent_depth_mm": structured.get("dent_depth_mm") or 0.0,
        "crack_present": bool(structured.get("has_crack")) if structured.get("has_crack") is not None else False,
        "notes": structured.get("notes"),
    }

    kwargs: Dict[str, Any] = {}
    dropped = []
    for k, v in candidates.items():
        if k in params:
            kwargs[k] = v
        else:
            dropped.append(k)

    debug["filtered_kwargs_used"] = kwargs
    debug["dropped_candidate_keys"] = dropped

    try:
        dent_obj = DentDamage(**kwargs)  # type: ignore
        res = assess_dent(dent_obj)  # type: ignore
        if is_dataclass(res):
            res = asdict(res)
        if isinstance(res, dict):
            return res, debug
        return {"result": str(res)}, debug
    except Exception as e:
        return {"status": "error", "error": f"Could not construct/run DentDamage: {e}"}, debug


# -----------------------------
# Rules engine adapter
# -----------------------------
def build_rules_ctx(structured: Dict[str, Any]) -> Dict[str, Any]:
    return {
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
        "_flat": structured,
    }


def run_rules(structured: Dict[str, Any]) -> Tuple[Any, Dict[str, Any]]:
    debug: Dict[str, Any] = {"selected": None, "signature": None, "module_exports": []}
    if not HAS_RULES_ENGINE:
        return {"status": "skipped", "reason": "rules_engine not available"}, debug
    if not RULES_DB.exists():
        return {"status": "skipped", "reason": "rules.db not found in deployment"}, debug

    exports = sorted([n for n in dir(rules_engine) if not n.startswith("_")])
    debug["module_exports"] = exports

    fn = None
    if hasattr(rules_engine, "assess_damage"):
        fn = getattr(rules_engine, "assess_damage")
        debug["selected"] = "assess_damage"
    elif hasattr(rules_engine, "evaluate_rules"):
        fn = getattr(rules_engine, "evaluate_rules")
        debug["selected"] = "evaluate_rules"
    elif hasattr(rules_engine, "run_rules"):
        fn = getattr(rules_engine, "run_rules")
        debug["selected"] = "run_rules"

    if fn is None:
        return {"error": "rules_engine has no compatible rules function (assess_damage/evaluate_rules/run_rules)"}, debug

    try:
        debug["signature"] = str(inspect.signature(fn))
    except Exception:
        debug["signature"] = None

    ctx = build_rules_ctx(structured)
    debug["ctx_sent"] = ctx

    try:
        if debug["selected"] == "assess_damage":
            res = fn(str(RULES_DB), structured.get("aircraft_family") or "UNKNOWN", ctx)  # type: ignore
        else:
            res = fn(str(RULES_DB), ctx)  # type: ignore

        if is_dataclass(res):
            return asdict(res), debug
        return res, debug
    except Exception as e:
        return {"error": str(e)}, debug


# -----------------------------
# SRM search (ATA gated)
# -----------------------------
def run_srm_search(structured: Dict[str, Any], limit: int = 8) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    debug: Dict[str, Any] = {"selected": None, "signature": None, "query_used": None, "ata_required": None, "filtered_out_by_ata": 0}
    if not HAS_SRM_SEARCH:
        return [], {"error": "srm_search module not available"}
    if not SRM_DB.exists():
        return [], {"error": "srm_index.db not found in deployment"}

    ata = infer_ata_chapter(structured.get("structure"), structured.get("structure_zone"))
    debug["ata_required"] = ata

    q_bits = []
    if structured.get("aircraft_family"):
        q_bits.append(str(structured["aircraft_family"]))
    if structured.get("structure"):
        q_bits.append(str(structured["structure"]))
    if structured.get("structure_zone"):
        q_bits.append(str(structured["structure_zone"]))
    if structured.get("damage_type"):
        q_bits.append(str(structured["damage_type"]))

    # SRM-ish anchors
    q_bits += ["allowable damage", "dent"]

    query = " ".join(q_bits).strip()
    debug["query_used"] = query

    try:
        if hasattr(srm_search, "search_srm"):
            fn = getattr(srm_search, "search_srm")
            debug["selected"] = "search_srm"
        elif hasattr(srm_search, "search"):
            fn = getattr(srm_search, "search")
            debug["selected"] = "search"
        else:
            return [], {"error": "srm_search has no search_srm/search function"}

        try:
            debug["signature"] = str(inspect.signature(fn))
        except Exception:
            debug["signature"] = None

        con = sqlite3.connect(str(SRM_DB))
        try:
            hits = fn(con, query=query, aircraft_family=structured.get("aircraft_family"), limit=limit * 3)  # type: ignore
        finally:
            con.close()

        # Normalize to list[dict]
        out: List[Dict[str, Any]] = []
        for h in hits or []:
            if is_dataclass(h):
                out.append(asdict(h))
            elif isinstance(h, dict):
                out.append(h)
            else:
                out.append({"hit": str(h)})

        # ATA filter
        if ata:
            kept: List[Dict[str, Any]] = []
            filtered = 0
            for hit in out:
                title = (hit.get("doc_title") or hit.get("title") or "") or ""
                fname = (hit.get("file_name") or "") or ""
                if ata_in_title_or_filename(ata, title, fname):
                    kept.append(hit)
                else:
                    filtered += 1
            debug["filtered_out_by_ata"] = filtered
            out = kept

        # Trim to limit
        out = out[:limit]

        # If we required ATA and got nothing, be explicit
        if ata and not out:
            return [], {"error": f"No SRM PDF/hit available in library for required ATA {ata} (based on structure).", **debug}

        return out, debug

    except Exception as e:
        return [], {"error": str(e), **debug}


def srm_top_ref(hits: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    return hits[0] if hits else None


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
# Auto-parse state handling
# -----------------------------
def _ensure_structured_from_desc(desc: str) -> None:
    """
    Ensures session_state.structured matches the current description text.
    This prevents stale structure fields (like 'FUSELAGE') from persisting.
    """
    desc = (desc or "").strip()
    prev = (st.session_state.get("_last_desc") or "").strip()
    if desc != prev or "structured" not in st.session_state:
        st.session_state.structured = parse_damage_description(desc)
        st.session_state._last_desc = desc


# -----------------------------
# Main UI
# -----------------------------
colA, colB = st.columns([1.1, 0.9], gap="large")

with colA:
    st.subheader("1) Paste damage description")

    default_text = "B737, fuselage, LH side, STA 123, S-10L, FR 12, skin dent 0.25mm dia, 3.18mm depth, no visible crack."
    desc = st.text_area(
        "Damage description",
        value=st.session_state.get("desc_text", default_text),
        height=120,
        key="desc_text",
        help="Paste a single-line or multi-line AOG description. The app will parse into structured fields.",
    )

    # Auto-parse on change
    _ensure_structured_from_desc(desc)

    # Optional manual parse button (kept)
    if st.button("Parse description", type="primary"):
        _ensure_structured_from_desc(desc)

    structured = st.session_state.structured

    st.subheader("2) Structured fields")
    f1, f2, f3, f4, f5 = st.columns(5)
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
        frame = st.text_input("Frame (FR)", value=str(structured.get("frame") or ""))
    with f5:
        damage_type = st.selectbox("Damage type", ["DENT", "GOUGE", "CRACK", "CORROSION", "OTHER"], index=["DENT","GOUGE","CRACK","CORROSION","OTHER"].index(structured.get("damage_type") or "DENT"))

    d1, d2, d3 = st.columns(3)
    with d1:
        dent_dia = st.number_input("Dent diameter (mm)", value=float(structured.get("dent_diameter_mm") or 0.0), step=0.1, format="%.2f")
    with d2:
        dent_depth = st.number_input("Dent depth (mm)", value=float(structured.get("dent_depth_mm") or 0.0), step=0.1, format="%.2f")
    with d3:
        crack_opt = st.selectbox("Crack present?", ["Unknown", "No", "Yes"], index=0)

    # Write back into structured dict (source of truth)
    structured["raw"] = desc.strip()
    structured["aircraft_family"] = _normalize_aircraft_family(aircraft_family) if aircraft_family else None
    structured["structure"] = structure.strip().upper() if structure else None
    structured["structure_zone"] = structure_zone.strip().upper() if structure_zone else None
    structured["side"] = side
    structured["sta"] = None if sta == 0.0 else float(sta)
    structured["wl"] = None if wl == 0.0 else float(wl)
    structured["stringer"] = stringer.strip().upper() or None

    fr_val = (frame or "").strip()
    if fr_val == "":
        structured["frame"] = None
    else:
        mfr = re.search(r"([0-9]{1,4})", fr_val)
        structured["frame"] = int(mfr.group(1)) if mfr else None

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

    if run:
        # Ensure we parse the latest text before running (extra safety)
        _ensure_structured_from_desc(desc)
        structured = st.session_state.structured

        dent_result, dent_debug = run_dent_model(structured)
        rules_res, rules_debug = run_rules(structured)
        srm_hits, srm_debug = run_srm_search(structured, limit=8)

        top = srm_top_ref(srm_hits)
        if top:
            st.markdown("### SRM Reference (top hit)")
            doc_title = top.get("doc_title") or top.get("title") or "SRM hit"
            page = top.get("page") or top.get("page_no")
            file_name = top.get("file_name")
            rev = top.get("revision")
            st.info(f"{doc_title} • Page {page} • File {file_name} (Rev {rev})")
            snippet = top.get("snippet") or top.get("text") or ""
            st.code(str(snippet)[:1200], language="text")

            st.markdown("### Location verification vs SRM excerpt")
            try:
                con = sqlite3.connect(str(SRM_DB))
                cov = extract_location_coverage_from_doc(con, doc_title=str(doc_title))
                con.close()

                verdict, messages = validate_location_against_coverage(structured, cov)

                if verdict == "WITHIN":
                    st.success("Location is within SRM excerpt coverage ranges (based on extracted ranges).")
                elif verdict == "OUTSIDE":
                    st.error("This stringer and/or this frame and/or this STA is/are not within the SRM stored in the library.")
                else:
                    st.warning("Cannot verify location against SRM excerpt ranges (no explicit ranges found for provided fields).")

                for m in messages:
                    st.write("•", m)

                with st.expander("Coverage ranges extracted from SRM excerpt"):
                    st.json(
                        {
                            "sta_ranges": cov.sta_ranges,
                            "stringer_ranges_signed": cov.stringer_ranges,
                            "frame_ranges": cov.frame_ranges,
                            "note_stringers": "Stringers are stored as signed ints (L negative, R positive). Example: 10L=-10, 10R=+10",
                        }
                    )
            except Exception as e:
                st.warning(f"Location verification could not be performed: {e}")

        st.markdown("### Dent model output")
        if build_plain_text_summary and isinstance(dent_result, dict):
            try:
                st.code(build_plain_text_summary(dent_result), language="text")  # type: ignore
            except Exception:
                st.json(dent_result)
        else:
            st.json(dent_result)

        with st.expander("Dent model debug"):
            st.json(dent_debug)

        st.markdown("### Rules matches")
        st.json(rules_res)

        with st.expander("Rules engine debug"):
            st.json(rules_debug)

        st.markdown("### SRM search hits (prototype)")
        if srm_hits:
            for hit in srm_hits[:8]:
                title = hit.get("doc_title") or hit.get("title") or hit.get("file_name") or "SRM hit"
                meta = []
                if hit.get("revision"):
                    meta.append(f"Rev: {hit['revision']}")
                if hit.get("aircraft_family"):
                    meta.append(f"Aircraft: {hit['aircraft_family']}")
                if hit.get("file_name"):
                    meta.append(f"File: {hit['file_name']}")
                if hit.get("page") or hit.get("page_no"):
                    meta.append(f"Page: {hit.get('page') or hit.get('page_no')}")
                st.markdown(f"**{title}**" + (f" ({' • '.join(meta)})" if meta else ""))
                snippet = hit.get("snippet") or hit.get("text") or ""
                st.code(str(snippet)[:1200], language="text")
        else:
            # show error from debug if present
            if isinstance(srm_debug, dict) and srm_debug.get("error"):
                st.warning(str(srm_debug["error"]))
            else:
                st.info("No SRM hits returned.")

        with st.expander("SRM search debug"):
            st.json(srm_debug)

        st.markdown("### Logging")
        log_it = st.checkbox("Log this assessment to SQLite (assessments.db)", value=True)
        if log_it:
            try:
                log_assessment(ASSESSMENTS_DB, structured, rules_res, srm_hits, dent_result)
                st.success("Logged to assessments.db")
            except Exception as e:
                st.error(f"Failed to log assessment: {e}")

    else:
        st.info("Fill the structured fields if needed, then click **Run rules + SRM search + dent model**.")


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
