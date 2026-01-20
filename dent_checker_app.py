# dent_checker_app.py
# Streamlit app: SRM Damage Assessment (Prototype)
#
# Key features:
# - Fast “free-text” damage description parsing into structured fields
# - Dent assessment using damage_models (if present)
# - Rules evaluation using rules_engine (if present)
# - SRM full-text search using srm_index.db (if present)
# - SRM DB Debug panel (shows cwd + existence + size + sha256 prefix)
# - Optional logging of assessments to SQLite (assessments.db)
#
# Important behavior update:
# - ATA chapter gating:
#   The app infers an ATA chapter from the description (e.g., WING -> ATA 57),
#   checks if SRM library/index contains docs for that ATA,
#   and *only* searches/assesses against SRM content for that ATA.
#   If no SRM doc exists for the inferred ATA, it reports "PDF isn't available".
#
# ATA chapter names are based on ATA 100 chapter list. (Wikipedia) :contentReference[oaicite:2]{index=2}

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
# ATA 100 chapter list (major chapters)
# Source: ATA 100 page (Wikipedia). :contentReference[oaicite:3]{index=3}
# -----------------------------
ATA_CHAPTERS: Dict[int, str] = {
    0: "GENERAL",
    1: "MAINTENANCE POLICY",
    2: "OPERATIONS",
    3: "SUPPORT",
    4: "AIRWORTHINESS LIMITATIONS",
    5: "TIME LIMITS/MAINTENANCE CHECKS",
    6: "DIMENSIONS AND AREAS",
    7: "LIFTING AND SHORING",
    8: "LEVELING AND WEIGHING",
    9: "TOWING AND TAXIING",
    10: "PARKING, MOORING, STORAGE AND RETURN TO SERVICE",
    11: "PLACARDS AND MARKINGS",
    12: "SERVICING",
    13: "HARDWARE AND GENERAL TOOLS",
    15: "AIRCREW INFORMATION",
    16: "CHANGE OF ROLE",
    18: "VIBRATION AND NOISE ANALYSIS (HELICOPTER ONLY)",
    20: "STANDARD PRACTICES- AIRFRAME",
    21: "AIR CONDITIONING AND PRESSURIZATION",
    22: "AUTO FLIGHT",
    23: "COMMUNICATIONS",
    24: "ELECTRICAL POWER",
    25: "EQUIPMENT / FURNISHINGS",
    26: "FIRE PROTECTION",
    27: "FLIGHT CONTROLS",
    28: "FUEL",
    29: "HYDRAULIC POWER",
    30: "ICE AND RAIN PROTECTION",
    31: "INDICATING / RECORDING SYSTEM",
    32: "LANDING GEAR",
    33: "LIGHTS",
    34: "NAVIGATION",
    35: "OXYGEN",
    36: "PNEUMATIC",
    37: "VACUUM",
    38: "WATER / WASTE",
    39: "ELECTRICAL - ELECTRONIC PANELS AND MULTIPURPOSE COMPONENTS",
    40: "MULTISYSTEM",
    41: "WATER BALLAST",
    42: "INTEGRATED MODULAR AVIONICS (IMA)",
    43: "EMERGENCY SOLAR PANEL SYSTEM (ESPS)",
    44: "CABIN SYSTEMS",
    45: "CENTRAL MAINTENANCE SYSTEM (CMS)",
    46: "INFORMATION SYSTEMS",
    47: "NITROGEN GENERATION SYSTEM",
    48: "IN FLIGHT FUEL DISPENSING",
    49: "AIRBORNE AUXILIARY POWER",
    50: "CARGO AND ACCESSORY COMPARTMENTS",
    51: "STANDARD PRACTICES AND STRUCTURES - GENERAL",
    52: "DOORS",
    53: "FUSELAGE",
    54: "NACELLES / PYLONS",
    55: "STABILIZERS",
    56: "WINDOWS",
    57: "WINGS",
    60: "STANDARD PRACTICES - PROP./ROTOR",
    61: "PROPELLERS / PROPULSION",
    62: "MAIN ROTOR(S)",
    63: "MAIN ROTOR DRIVE(S)",
    64: "TAIL ROTOR",
    65: "TAIL ROTOR DRIVE",
    66: "FOLDING BLADES/PYLON",
    67: "ROTORS FLIGHT CONTROL",
    70: "STANDARD PRACTICES - ENGINE",
    71: "POWER PLANT",
    72: "ENGINE",
    73: "ENGINE - FUEL AND CONTROL",
    74: "IGNITION",
    75: "BLEED AIR",
    76: "ENGINE CONTROLS",
    77: "ENGINE INDICATING",
    78: "EXHAUST",
    79: "OIL",
    80: "STARTING",
    81: "TURBINES (RECIPROCATING ENGINES)",
    82: "WATER INJECTION",
    83: "ACCESSORY GEAR BOX (ENGINE DRIVEN)",
    84: "PROPULSION AUGMENTATION",
    85: "RECIPROCATING ENGINE",
    91: "CHARTS",
    97: "IMAGE RECORDING",
    99: "ELECTRONIC WARFARE SYSTEM",
}


# -----------------------------
# ATA inference rules (lightweight)
# - This is the guardrail that prevents "wing" being assessed as ATA 53.
# -----------------------------
ATA_KEYWORDS: List[Tuple[int, List[str]]] = [
    (57, ["wing", "wings", "wing skin", "aileron", "flap", "slat", "spoiler", "winglet", "wingtip"]),
    (55, ["stabilizer", "stabilizers", "horizontal stabilizer", "vertical stabilizer", "fin", "tailplane"]),
    (53, ["fuselage", "cabin skin", "belly", "crown", "bulkhead", "pressure bulkhead", "door surround"]),
    (52, ["door", "doors", "entry door", "cargo door", "service door"]),
    (56, ["window", "windows", "windshield"]),
    (32, ["landing gear", "gear", "strut", "wheel", "brake"]),
    (54, ["nacelle", "pylon", "engine pylon"]),
    (27, ["flight control", "elevator", "rudder", "aileron", "trim"]),
    (28, ["fuel", "tank"]),
    (29, ["hydraulic"]),
    (30, ["ice", "rain", "anti-ice", "deice"]),
    (24, ["electrical", "power"]),
]


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

rules_engine = None
srm_search = None

try:
    # Expected in your repo:
    # - DentDamage dataclass
    # - assess_dent(dent: DentDamage) -> DentAssessmentResult or dict
    # - build_plain_text_summary(optional)
    from damage_models import DentDamage as _DentDamage, assess_dent as _assess_dent  # type: ignore
    DentDamage = _DentDamage
    assess_dent = _assess_dent
    try:
        from damage_models import build_plain_text_summary as _bps  # type: ignore
        build_plain_text_summary = _bps
    except Exception:
        build_plain_text_summary = None
    HAS_DAMAGE_MODELS = True
except Exception as e:
    damage_models_err = e

try:
    import rules_engine as _rules_engine  # type: ignore
    rules_engine = _rules_engine
    HAS_RULES_ENGINE = True
except Exception as e:
    rules_engine_err = e

try:
    import srm_search as _srm_search  # type: ignore
    srm_search = _srm_search
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
SRM_LIBRARY_DIR = ROOT / "srm_library"  # may or may not exist in your repo


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
    if t.startswith("B7") or t.startswith("A3") or t.startswith("A32") or t.startswith("E1") or t.startswith("E17"):
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


def infer_ata_from_text(desc: str) -> Optional[int]:
    """
    Infer ATA chapter from description keywords.
    This is the guardrail for your "wing should be ATA 57" requirement.
    """
    s = (desc or "").lower()
    for ata, kws in ATA_KEYWORDS:
        for kw in kws:
            if kw in s:
                return ata
    return None


def ata_label(ata: Optional[int]) -> str:
    if ata is None:
        return "UNKNOWN"
    name = ATA_CHAPTERS.get(int(ata))
    if name:
        return f"ATA {int(ata):02d} — {name}"
    return f"ATA {int(ata):02d}"


def parse_damage_description(desc: str) -> Dict[str, Any]:
    """
    Lightweight parser.
    NOTE: 'no visible crack' overrides crack presence.
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
        "damage_type": None,
        "dent_diameter_mm": None,
        "dent_depth_mm": None,
        "has_crack": None,
        "notes": None,
        "ata_chapter": None,
    }

    # Aircraft family
    m = re.search(r"\b(B7\d{2}|A3\d{2}|A32\d{2}|E1\d{2}|E17\d)\b", raw, flags=re.IGNORECASE)
    if m:
        out["aircraft_family"] = _normalize_aircraft_family(m.group(1))

    # Structure keywords (used for ATA inference + context)
    if re.search(r"\bfuselage\b", raw, flags=re.IGNORECASE):
        out["structure"] = "FUSELAGE"
    elif re.search(r"\bwing\b", raw, flags=re.IGNORECASE):
        out["structure"] = "WING"
    elif re.search(r"\bempennage\b|\btail\b", raw, flags=re.IGNORECASE):
        out["structure"] = "EMPENNAGE"
    elif re.search(r"\bdoor\b", raw, flags=re.IGNORECASE):
        out["structure"] = "DOOR"

    # Zone / sub-area
    if re.search(r"\bskin\b", raw, flags=re.IGNORECASE):
        out["structure_zone"] = "SKIN"
    elif re.search(r"\bstringer\b", raw, flags=re.IGNORECASE):
        out["structure_zone"] = "STRINGER"
    elif re.search(r"\bframe\b", raw, flags=re.IGNORECASE):
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

    # Stringer: S-10L / S10L / Stringer 10L
    m = re.search(r"\bS[-\s]?(\d{1,3})([LR])\b", raw, flags=re.IGNORECASE)
    if m:
        out["stringer"] = f"{int(m.group(1))}{m.group(2).upper()}"
    else:
        m2 = re.search(r"\bSTRINGER\s*(\d{1,3})([LR])\b", raw, flags=re.IGNORECASE)
        if m2:
            out["stringer"] = f"{int(m2.group(1))}{m2.group(2).upper()}"

    # Damage type
    if re.search(r"\bdent\b", raw, flags=re.IGNORECASE):
        out["damage_type"] = "DENT"
    elif re.search(r"\bgouge\b", raw, flags=re.IGNORECASE):
        out["damage_type"] = "GOUGE"
    elif re.search(r"\bcrack\b", raw, flags=re.IGNORECASE):
        out["damage_type"] = "CRACK"
    elif re.search(r"\bcorrosion\b", raw, flags=re.IGNORECASE):
        out["damage_type"] = "CORROSION"

    # Crack present? (override logic)
    if re.search(r"\bno\s+(visible\s+)?crack(s)?\b", raw, flags=re.IGNORECASE):
        out["has_crack"] = False
    elif re.search(r"\bcrack(s)?\b", raw, flags=re.IGNORECASE):
        out["has_crack"] = True

    # Dimensions (support mm and inches)
    dia_mm = _find_float_mm(raw, [
        r"(\d+(?:\.\d+)?)\s*mm\s*(?:dia|diameter)\b",
        r"\bdia\s*(\d+(?:\.\d+)?)\s*mm\b",
        r"(?:dent|crack)\s*(\d+(?:\.\d+)?)\s*mm\s*(?:dia|diameter)\b",
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

    # Infer ATA chapter from text
    out["ata_chapter"] = infer_ata_from_text(raw)

    return out


# -----------------------------
# Assessments DB (migrating schema)
# -----------------------------
def _col_exists(con: sqlite3.Connection, table: str, col: str) -> bool:
    rows = con.execute(f"PRAGMA table_info({table});").fetchall()
    return any(r[1] == col for r in rows)


def init_assessments_db(db_path: Path) -> None:
    con = sqlite3.connect(str(db_path))
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS assessments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              created_utc TEXT NOT NULL,
              aircraft_family TEXT,
              ata_chapter INTEGER,
              structure TEXT,
              structure_zone TEXT,
              side TEXT,
              sta REAL,
              wl REAL,
              stringer TEXT,
              damage_type TEXT,
              dent_diameter_mm REAL,
              dent_depth_mm REAL,
              has_crack INTEGER,
              input_text TEXT,
              structured_json TEXT,
              rules_ctx_json TEXT,
              rules_json TEXT,
              srm_hits_json TEXT,
              result_json TEXT,
              final_statement TEXT
            );
            """
        )
        con.commit()

        # Lightweight "migrations" if table already existed:
        for col, ddl in [
            ("ata_chapter", "ALTER TABLE assessments ADD COLUMN ata_chapter INTEGER;"),
            ("rules_ctx_json", "ALTER TABLE assessments ADD COLUMN rules_ctx_json TEXT;"),
            ("final_statement", "ALTER TABLE assessments ADD COLUMN final_statement TEXT;"),
        ]:
            if not _col_exists(con, "assessments", col):
                con.execute(ddl)
                con.commit()
    finally:
        con.close()


def log_assessment(
    db_path: Path,
    structured: Dict[str, Any],
    rules_ctx: Any,
    rules_rows: Any,
    srm_hits: Any,
    result: Any,
    final_statement: str,
) -> None:
    init_assessments_db(db_path)

    con = sqlite3.connect(str(db_path))
    try:
        con.execute(
            """
            INSERT INTO assessments (
              created_utc, aircraft_family, ata_chapter, structure, structure_zone, side, sta, wl, stringer,
              damage_type, dent_diameter_mm, dent_depth_mm, has_crack,
              input_text, structured_json, rules_ctx_json, rules_json, srm_hits_json, result_json, final_statement
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now_iso(),
                structured.get("aircraft_family"),
                structured.get("ata_chapter"),
                structured.get("structure"),
                structured.get("structure_zone"),
                structured.get("side"),
                structured.get("sta"),
                structured.get("wl"),
                structured.get("stringer"),
                structured.get("damage_type"),
                structured.get("dent_diameter_mm"),
                structured.get("dent_depth_mm"),
                None if structured.get("has_crack") is None else (1 if structured.get("has_crack") else 0),
                structured.get("raw"),
                safe_json(structured),
                safe_json(rules_ctx),
                safe_json(rules_rows),
                safe_json(srm_hits),
                safe_json(result),
                final_statement,
            ),
        )
        con.commit()
    finally:
        con.close()


# -----------------------------
# Module adapters (dent / rules / SRM)
# -----------------------------
def _sig_and_params(fn: Any) -> Tuple[str, List[str]]:
    try:
        sig = str(inspect.signature(fn))
        params = list(inspect.signature(fn).parameters.keys())
        return sig, params
    except Exception:
        return "UNKNOWN", []


def run_dent_model(structured: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Adapts to your current DentDamage signature, e.g.:
      (aircraft_type, structure_zone, side, sta, stringer, dent_diameter_mm, dent_depth_mm, crack_present, notes=None)
    """
    debug: Dict[str, Any] = {}
    if not (HAS_DAMAGE_MODELS and DentDamage and assess_dent):
        return {"status": "skipped", "reason": "damage_models module not available"}, debug

    if structured.get("damage_type") != "DENT":
        return {"status": "skipped", "reason": "damage_type is not DENT"}, debug

    sig, accepted = _sig_and_params(DentDamage)
    debug["DentDamage_signature"] = sig
    debug["accepted_params"] = accepted

    # Candidate kwargs (multiple naming styles)
    candidates: Dict[str, Any] = {
        "aircraft_type": structured.get("aircraft_family") or "UNKNOWN",
        "aircraft_family": structured.get("aircraft_family") or "UNKNOWN",
        "structure_zone": structured.get("structure_zone") or "UNKNOWN",
        "zone": structured.get("structure_zone") or "UNKNOWN",
        "side": structured.get("side") or "ANY",
        "sta": None if structured.get("sta") is None else str(int(structured["sta"])),
        "stringer": structured.get("stringer"),
        "dent_diameter_mm": float(structured.get("dent_diameter_mm") or 0.0),
        "dent_depth_mm": float(structured.get("dent_depth_mm") or 0.0),
        "diameter_mm": float(structured.get("dent_diameter_mm") or 0.0),
        "depth_mm": float(structured.get("dent_depth_mm") or 0.0),
        "notes": structured.get("notes"),
        "crack_present": structured.get("has_crack"),
        "has_crack": structured.get("has_crack"),
    }

    # crack_present defaulting (important)
    crack_present_used = candidates.get("crack_present")
    crack_reason = ""
    if crack_present_used is None:
        # default to False if unknown (you can change this policy)
        crack_present_used = False
        crack_reason = "defaulted False because crack status was Unknown"
    else:
        crack_reason = "from structured.has_crack"
    candidates["crack_present"] = bool(crack_present_used)

    debug["crack_present_used"] = bool(crack_present_used)
    debug["crack_present_reason"] = crack_reason

    # Filter to DentDamage accepted parameters
    filtered = {}
    dropped = []
    for k, v in candidates.items():
        if k in accepted:
            filtered[k] = v
        else:
            dropped.append(k)

    debug["filtered_kwargs_used"] = filtered
    debug["dropped_candidate_keys"] = dropped

    try:
        dent_obj = DentDamage(**filtered)  # type: ignore
        res = assess_dent(dent_obj)  # type: ignore

        # Normalize result for display
        if is_dataclass(res):
            return {"result": str(res)}, debug
        if isinstance(res, dict):
            return res, debug
        return {"result": str(res)}, debug
    except Exception as e:
        return {"status": "error", "error": f"Could not construct/run DentDamage: {e}"}, debug


def build_rules_ctx(structured: Dict[str, Any]) -> Dict[str, Any]:
    ctx = {
        "aircraft_family": structured.get("aircraft_family"),
        "raw": structured.get("raw"),
        "ata_chapter": structured.get("ata_chapter"),
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
    return ctx


def run_rules_engine(structured: Dict[str, Any]) -> Tuple[Any, Dict[str, Any], Dict[str, Any]]:
    """
    Supports rules_engine.assess_damage(db_path, aircraft_family, ctx, revision=None)
    """
    debug: Dict[str, Any] = {
        "selected": None,
        "signature": None,
        "ctx_sent": None,
        "module_exports": [],
    }

    if not HAS_RULES_ENGINE or rules_engine is None:
        return [{"status": "skipped", "reason": "rules_engine not available"}], debug, {}

    debug["module_exports"] = sorted([k for k in dir(rules_engine) if not k.startswith("_")])

    if not RULES_DB.exists():
        return {
            "rule_id": None,
            "passed": False,
            "disposition": "ENGINEERING_REVIEW",
            "severity": "engineering",
            "srm_ref": None,
            "reasons": ["rules.db not found in deployment."],
            "actions": {"disposition": "ENGINEERING_REVIEW", "next_steps": ["Seed rules.db into repo."]},
        }, debug, {}

    ctx = build_rules_ctx(structured)
    debug["ctx_sent"] = ctx

    # find compatible function
    fn = None
    fn_name = None
    for name in ["assess_damage", "evaluate_rules", "run_rules", "evaluate"]:
        if hasattr(rules_engine, name):
            fn = getattr(rules_engine, name)
            fn_name = name
            break

    if fn is None:
        return [{"error": "rules_engine has no compatible rules function (assess_damage/evaluate_rules/run_rules/evaluate)"}], debug, ctx

    debug["selected"] = fn_name
    sig, _ = _sig_and_params(fn)
    debug["signature"] = sig

    try:
        if fn_name == "assess_damage":
            # signature: (db_path: str, aircraft_family: str, ctx: Dict[str, Any], revision: Optional[str]=None)
            res = fn(str(RULES_DB), structured.get("aircraft_family") or "UNKNOWN", ctx)  # type: ignore
        else:
            # For older styles, pass db_path then ctx or structured.
            # We'll try (db_path, ctx) then (db_path, structured)
            try:
                res = fn(str(RULES_DB), ctx)  # type: ignore
            except Exception:
                res = fn(str(RULES_DB), structured)  # type: ignore

        if is_dataclass(res):
            return asdict(res), debug, ctx
        if isinstance(res, dict):
            return res, debug, ctx
        return {"result": str(res)}, debug, ctx
    except Exception as e:
        return [{"error": str(e)}], debug, ctx


def _list_docs_for_ata_from_db(db_path: Path, ata: int) -> List[Tuple[str, str]]:
    """
    Returns list of (title, file_name) for docs that appear to belong to ATA CC.
    Heuristic: title or file_name contains 'CC-' or 'SRM_CC-'.
    """
    if not db_path.exists():
        return []
    cc = f"{ata:02d}-"
    out: List[Tuple[str, str]] = []
    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute("SELECT title, file_name FROM docs;").fetchall()
        for title, file_name in rows:
            t = (title or "")
            f = (file_name or "")
            if cc in t or cc in f or f"SRM_{cc}" in t or f"SRM_{cc}" in f:
                out.append((t, f))
    except Exception:
        pass
    finally:
        con.close()
    return out


def _library_has_pdf_for_ata(ata: int) -> Tuple[bool, str]:
    """
    Checks for SRM availability for this ATA:
    1) Prefer srm_index.db docs listing
    2) If srm_library exists, scan for PDFs whose names contain 'CC-'
    """
    cc = f"{ata:02d}-"

    docs = _list_docs_for_ata_from_db(SRM_DB, ata)
    if docs:
        return True, f"srm_index.db contains {len(docs)} doc(s) matching '{cc}'"

    if SRM_LIBRARY_DIR.exists():
        pdfs = list(SRM_LIBRARY_DIR.rglob("*.pdf"))
        matches = [p for p in pdfs if cc in p.name]
        if matches:
            return True, f"srm_library contains {len(matches)} pdf(s) matching '{cc}'"
        return False, f"srm_library exists but no pdf names include '{cc}'"

    return False, f"No docs in srm_index.db and no srm_library/ folder in repo"


def run_srm_search_with_ata_gate(
    structured: Dict[str, Any],
    limit: int = 8,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any], Optional[int]]:
    """
    Uses srm_search.search_srm(conn, query, aircraft_family, limit).
    Adds ATA gating by rejecting hits that are not in the inferred ATA chapter.
    """
    debug: Dict[str, Any] = {
        "selected": None,
        "signature": None,
        "query_used": None,
        "ata_required": structured.get("ata_chapter"),
        "ata_required_label": ata_label(structured.get("ata_chapter")),
        "ata_library_available": None,
        "ata_library_reason": None,
    }

    if not HAS_SRM_SEARCH or srm_search is None:
        return [{"status": "skipped", "reason": "srm_search module not available"}], debug, structured.get("ata_chapter")

    if not SRM_DB.exists():
        return [{"status": "skipped", "reason": "srm_index.db not found in deployment"}], debug, structured.get("ata_chapter")

    ata = structured.get("ata_chapter")
    if ata is None:
        # No ATA inference => do not guess; return guidance.
        return [{"status": "no_ata", "reason": "Could not infer ATA chapter from description. Add component keywords (e.g., wing/fuselage/door)."}], debug, None

    ok, reason = _library_has_pdf_for_ata(int(ata))
    debug["ata_library_available"] = ok
    debug["ata_library_reason"] = reason
    if not ok:
        return [{"status": "no_pdf", "reason": f"No SRM PDF/index found for {ata_label(int(ata))}. ({reason})"}], debug, int(ata)

    # Build search query
    q_bits = []
    if structured.get("aircraft_family"):
        q_bits.append(str(structured["aircraft_family"]))
    if structured.get("structure"):
        q_bits.append(str(structured["structure"]))
    if structured.get("structure_zone"):
        q_bits.append(str(structured["structure_zone"]))
    if structured.get("damage_type"):
        q_bits.append(str(structured["damage_type"]))
    if structured.get("sta"):
        q_bits.append(f"STA {int(structured['sta'])}")
    if structured.get("stringer"):
        q_bits.append(f"S{structured['stringer']}")

    # Add SRM anchors
    q_bits.append("allowable damage")
    if structured.get("damage_type") == "DENT":
        q_bits.append("dent")
        q_bits.append("table 102")
    query = " ".join(q_bits).strip()
    debug["query_used"] = query

    # Find compatible search function
    fn = None
    fn_name = None
    for name in ["search_srm", "search", "srm_search"]:
        if hasattr(srm_search, name):
            fn = getattr(srm_search, name)
            fn_name = name
            break
    if fn is None:
        return [{"error": "srm_search module has no search function (search_srm/search/srm_search)"}], debug, int(ata)

    debug["selected"] = fn_name
    sig, _ = _sig_and_params(fn)
    debug["signature"] = sig

    con = sqlite3.connect(str(SRM_DB))
    try:
        hits = fn(con, query=query, aircraft_family=structured.get("aircraft_family"), limit=limit)  # type: ignore
    finally:
        con.close()

    # Convert hits to dicts
    hit_dicts: List[Dict[str, Any]] = []
    for h in hits or []:
        if is_dataclass(h):
            d = asdict(h)
        elif isinstance(h, dict):
            d = h
        else:
            d = {"result": str(h)}

        # Normalize key "page"
        if "page_no" in d and "page" not in d:
            d["page"] = d.get("page_no")

        hit_dicts.append(d)

    # ATA gating: keep only hits whose doc title/file includes CC-
    cc = f"{int(ata):02d}-"
    gated = []
    for d in hit_dicts:
        title = str(d.get("doc_title") or d.get("title") or "")
        file_name = str(d.get("file_name") or "")
        if cc in title or cc in file_name or f"SRM_{cc}" in title or f"SRM_{cc}" in file_name:
            gated.append(d)

    # If gating removes everything, treat as "no pdf in that ATA" (even if db exists for other ATA)
    if not gated:
        return [{"status": "no_hits_in_ata", "reason": f"SRM index has hits, but none are in {ata_label(int(ata))} (filtered by '{cc}')."}], debug, int(ata)

    return gated, debug, int(ata)


def extract_srm_ref_from_rules(rules_res: Any) -> Optional[str]:
    if isinstance(rules_res, dict):
        for k in ["srm_ref", "srm_reference", "reference"]:
            if rules_res.get(k):
                return str(rules_res.get(k))
    return None


def parse_ata_from_srm_ref(s: str) -> Optional[int]:
    """
    Try to read 'CC-SS-SS' from an SRM ref string and return CC.
    """
    if not s:
        return None
    m = re.search(r"\b(\d{2})-\d{2}-\d{2}\b", s)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    return None


def build_final_statement(
    structured: Dict[str, Any],
    srm_hits: List[Dict[str, Any]],
    rules_res: Any,
) -> str:
    """
    Produces a conservative SRM statement:
    - Only if SRM hit is in inferred ATA chapter.
    - If not available, returns a "PDF not available" statement.
    """
    ata = structured.get("ata_chapter")
    dmg = structured.get("damage_type") or "DAMAGE"
    aircraft = structured.get("aircraft_family") or "UNKNOWN"

    if ata is None:
        return "Unable to infer ATA chapter from description; add component keywords (e.g., wing/fuselage/door) to enable SRM chapter selection."

    # If SRM hits is an error/status list, report that directly
    if not srm_hits:
        return f"No SRM hits found for {ata_label(int(ata))}. Ensure a relevant SRM PDF is indexed/available."

    if isinstance(srm_hits[0], dict) and srm_hits[0].get("status") in {"no_pdf", "no_ata", "no_hits_in_ata", "skipped"}:
        return str(srm_hits[0].get("reason") or srm_hits[0])

    # Use top hit
    top = srm_hits[0]
    doc_title = top.get("doc_title") or top.get("title") or "SRM"
    page = top.get("page") or top.get("page_no")
    file_name = top.get("file_name") or ""

    # ATA hard check: top hit must match inferred ATA
    inferred_ata = int(ata)
    cc = f"{inferred_ata:02d}-"
    title_s = f"{doc_title} {file_name}"
    if cc not in title_s and f"SRM_{cc}" not in title_s:
        return (
            f"SRM content found, but it does not match inferred {ata_label(inferred_ata)} "
            f"(top hit appears outside '{cc}'). Add/Index the correct ATA SRM PDF for this component."
        )

    # Get SRM ref from rules if present AND matches inferred ATA
    srm_ref = extract_srm_ref_from_rules(rules_res) or ""
    ref_ata = parse_ata_from_srm_ref(srm_ref) if srm_ref else None
    if ref_ata is not None and ref_ata != inferred_ata:
        # do not use mismatched ref
        srm_ref = ""

    # Determine within/out-of limits from rules if it provides "passed"
    within_txt = "within limits"
    if isinstance(rules_res, dict) and "passed" in rules_res:
        within_txt = "within limits" if bool(rules_res.get("passed")) else "out of limits"

    # Try to mention ATA subchapter/allowable/table if rules ref exists; else use doc title only
    if srm_ref:
        # Example: "SRM 53-00-01 ALLOWABLE DAMAGE 1 — Table 102 ..."
        ref_part = srm_ref
    else:
        ref_part = f"{ata_label(inferred_ata)}"

    pg_part = f"Page {page}" if page else "Page ?"
    return f"{dmg} is found {within_txt} per {aircraft} SRM ({ref_part}), {pg_part}."


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
        damage_type = st.selectbox("Damage type", ["DENT", "GOUGE", "CRACK", "CORROSION", "OTHER"], index=0)

    d1, d2, d3 = st.columns(3)
    with d1:
        dent_dia = st.number_input("Dent diameter (mm)", value=float(structured.get("dent_diameter_mm") or 0.0), step=0.1, format="%.2f")
    with d2:
        dent_depth = st.number_input("Dent depth (mm)", value=float(structured.get("dent_depth_mm") or 0.0), step=0.1, format="%.2f")
    with d3:
        crack_opt = st.selectbox("Crack present?", ["Unknown", "No", "Yes"], index=0)

    # Write back into structured dict
    structured["raw"] = desc.strip()
    structured["aircraft_family"] = _normalize_aircraft_family(aircraft_family) if aircraft_family else None
    structured["structure"] = structure.strip().upper() if structure else None
    structured["structure_zone"] = structure_zone.strip().upper() if structure_zone else None
    structured["side"] = side
    structured["sta"] = None if sta == 0.0 else float(sta)
    structured["wl"] = None if wl == 0.0 else float(wl)
    structured["stringer"] = stringer.strip().upper() or None
    structured["damage_type"] = damage_type
    structured["dent_diameter_mm"] = None if dent_dia == 0.0 else float(dent_dia)
    structured["dent_depth_mm"] = None if dent_depth == 0.0 else float(dent_depth)

    if crack_opt == "Unknown":
        structured["has_crack"] = None
    elif crack_opt == "No":
        structured["has_crack"] = False
    else:
        structured["has_crack"] = True

    # Update ATA inference after edits
    structured["ata_chapter"] = infer_ata_from_text(structured["raw"])

    st.info(f"Inferred ATA: **{ata_label(structured.get('ata_chapter'))}**")

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
            st.info("If you want SRM hits on Streamlit Cloud, commit srm_index.db to the repo (PDFs are not needed at runtime).")

    if run:
        # Dent model
        dent_result, dent_debug = run_dent_model(structured)

        # Rules engine
        rules_res, rules_debug, rules_ctx = run_rules_engine(structured)

        # SRM search with ATA gating
        srm_hits, srm_debug, ata_required = run_srm_search_with_ata_gate(structured, limit=8)

        # Final SRM-based statement (ATA-safe)
        final_statement = build_final_statement(structured, srm_hits if isinstance(srm_hits, list) else [], rules_res)

        st.markdown("### Final statement (SRM-based)")
        st.success(final_statement)

        # Optional: show SRM reference top hit
        if isinstance(srm_hits, list) and srm_hits and isinstance(srm_hits[0], dict) and "status" not in srm_hits[0]:
            top = srm_hits[0]
            st.markdown("**SRM Reference (top hit)**")
            st.write(
                f"{top.get('doc_title','SRM')} • Page {top.get('page','?')} • "
                f"File {top.get('file_name','?')} (Rev {top.get('revision','?')})"
            )
            snip = str(top.get("snippet") or "")[:600]
            if snip:
                st.code(snip, language="text")

        # Render dent model output
        st.markdown("### Dent model output")
        if build_plain_text_summary and isinstance(dent_result, dict) and "error" not in dent_result:
            try:
                st.code(build_plain_text_summary(dent_result), language="text")  # type: ignore
            except Exception:
                st.json(dent_result)
        else:
            st.json(dent_result)

        with st.expander("Dent model debug", expanded=False):
            st.json(dent_debug)

        # Render rules
        st.markdown("### Rules matches")
        st.json(rules_res)

        with st.expander("Rules engine debug", expanded=False):
            st.json(rules_debug)

        # Render SRM hits
        st.markdown("### SRM search hits (prototype)")
        if isinstance(srm_hits, list) and srm_hits and isinstance(srm_hits[0], dict) and "status" not in srm_hits[0]:
            for hit in srm_hits[:8]:
                title = hit.get("doc_title") or hit.get("title") or hit.get("file_name") or "SRM hit"
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
                st.code(str(snippet)[:1200], language="text")
        else:
            st.json(srm_hits)

        with st.expander("SRM search debug", expanded=False):
            st.json(srm_debug)

        # Logging
        st.markdown("### Logging")
        log_it = st.checkbox("Log this assessment to SQLite (assessments.db)", value=True)
        if log_it:
            try:
                log_assessment(
                    ASSESSMENTS_DB,
                    structured=structured,
                    rules_ctx=rules_ctx,
                    rules_rows=rules_res,
                    srm_hits=srm_hits,
                    result=dent_result,
                    final_statement=final_statement,
                )
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
            SELECT id, created_utc, aircraft_family, ata_chapter, structure, structure_zone, side, sta, stringer,
                   damage_type, dent_diameter_mm, dent_depth_mm, has_crack, final_statement
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
                        "ata": (None if r[3] is None else f"{int(r[3]):02d}"),
                        "structure": r[4],
                        "zone": r[5],
                        "side": r[6],
                        "sta": r[7],
                        "stringer": r[8],
                        "damage_type": r[9],
                        "dia_mm": r[10],
                        "depth_mm": r[11],
                        "crack": (None if r[12] is None else ("Yes" if r[12] == 1 else "No")),
                        "statement": r[13],
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
