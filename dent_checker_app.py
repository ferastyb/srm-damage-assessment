# dent_checker_app.py
# Streamlit app: SRM Damage Assessment (Prototype)
#
# Key features:
# - Fast “free-text” damage description parsing into structured fields
# - Dent assessment using damage_models (if present)
# - Rules evaluation using rules_engine (if present)
# - SRM full-text search using srm_index.db (if present)
# - SRM DB Debug panel (shows cwd + existence + size + sha256 prefix)
# - ATA gating: prevents using SRM hits from the wrong ATA chapter
# - Location verification: verifies STA / Stringer / Frame against ranges extracted from SRM excerpt
# - Optional logging of assessments to SQLite (assessments.db) with schema-tolerant insertion
#
# Repo layout assumptions (root):
# - dent_checker_app.py  (this file)
# - damage_models.py     (your dent model + assess_dent, etc.)
# - rules_engine.py      (rules evaluation)
# - srm_search.py        (search SRM index)
# - rules.db             (rules DB)
# - srm_index.db         (SRM search DB)  <-- must be committed if you want SRM hits on Streamlit
#
# IMPORTANT:
# - Streamlit Cloud cannot read uncommitted files. Only files in GitHub exist at runtime.
# - If SRM hits are "wrong ATA", this file filters them out and reports "PDF not available for required ATA".

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import inspect
from dataclasses import asdict, is_dataclass
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
st.caption("Prototype to structure AOG damage descriptions, evaluate rules, and search SRM excerpts.")


# -----------------------------
# ATA 100 (focused set for structures)
# -----------------------------
ATA_CHAPTERS: Dict[int, str] = {
    51: "Standard Practices and Structures - General",
    52: "Doors",
    53: "Fuselage",
    54: "Nacelles/Pylons",
    55: "Stabilizers",
    56: "Windows",
    57: "Wings",
}

# Component/structure → ATA mapping (extend as you add SRMs)
STRUCTURE_TO_ATA: Dict[str, int] = {
    "FUSELAGE": 53,
    "WING": 57,
    "STABILIZER": 55,
    "STABILIZERS": 55,
    "EMPENNAGE": 55,
    "TAIL": 55,
    "HORIZONTAL STABILIZER": 55,
    "VERTICAL STABILIZER": 55,
    "FIN": 55,
    "RUDDER": 55,
    "ELEVATOR": 55,
}

# Keywords → normalized structure label (for parsing)
STRUCTURE_KEYWORDS: List[Tuple[str, str]] = [
    (r"\bfuselage\b", "FUSELAGE"),
    (r"\bwing\b", "WING"),
    (r"\bstabilizer\b", "STABILIZERS"),
    (r"\bhorizontal\s+stabilizer\b", "STABILIZERS"),
    (r"\bvertical\s+stabilizer\b", "STABILIZERS"),
    (r"\bempennage\b", "STABILIZERS"),
    (r"\btail\b", "STABILIZERS"),
    (r"\brudder\b", "STABILIZERS"),
    (r"\belevator\b", "STABILIZERS"),
    (r"\bfin\b", "STABILIZERS"),
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
    rules_engine = None  # type: ignore

try:
    import srm_search  # type: ignore
    HAS_SRM_SEARCH = True
except Exception as e:
    srm_search_err = e
    srm_search = None  # type: ignore


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
    return t


def _parse_side(text: str) -> str:
    s = (text or "").upper()
    if "LH" in s or "LEFT" in s or "LHS" in s:
        return "LH"
    if "RH" in s or "RIGHT" in s or "RHS" in s:
        return "RH"
    return "ANY"


def _find_float(text: str, patterns: List[str]) -> Optional[float]:
    for pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            try:
                return float(m.group(1))
            except Exception:
                continue
    return None


def _infer_structure(raw: str) -> Optional[str]:
    for pat, label in STRUCTURE_KEYWORDS:
        if re.search(pat, raw, flags=re.IGNORECASE):
            return label
    return None


def _infer_zone(raw: str) -> Optional[str]:
    if re.search(r"\bskin\b", raw, flags=re.IGNORECASE):
        return "SKIN"
    if re.search(r"\bstringer\b", raw, flags=re.IGNORECASE):
        return "STRINGER"
    if re.search(r"\bframe\b|\bfr\b", raw, flags=re.IGNORECASE):
        return "FRAME"
    return None


def _infer_damage_type(raw: str) -> Optional[str]:
    if re.search(r"\bdent(s)?\b", raw, flags=re.IGNORECASE):
        return "DENT"
    if re.search(r"\bcrack(s)?\b", raw, flags=re.IGNORECASE):
        return "CRACK"
    if re.search(r"\bgouge(s)?\b", raw, flags=re.IGNORECASE):
        return "GOUGE"
    if re.search(r"\bcorrosion\b", raw, flags=re.IGNORECASE):
        return "CORROSION"
    return None


def _infer_required_ata(structure: Optional[str]) -> Optional[int]:
    if not structure:
        return None
    s = structure.strip().upper()
    # direct match
    if s in STRUCTURE_TO_ATA:
        return STRUCTURE_TO_ATA[s]
    # try contains match
    for k, v in STRUCTURE_TO_ATA.items():
        if k in s:
            return v
    return None


def parse_damage_description(desc: str) -> Dict[str, Any]:
    """
    Parses free text like:
    "B737, stabilizer, LH side, STA 123, S-10L, FR 12, skin dent 0.25mm dia, 3.18mm depth, no visible crack."
    """
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

    # Structure / zone / side / damage
    out["structure"] = _infer_structure(raw)
    out["structure_zone"] = _infer_zone(raw)
    out["side"] = _parse_side(raw)
    out["damage_type"] = _infer_damage_type(raw) or "OTHER"

    # STA / WL
    m = re.search(r"\bSTA(?:TION)?\s*([0-9]{1,5}(?:\.[0-9]+)?)\b", raw, flags=re.IGNORECASE)
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

    # Frame formats: FR 12, FR-12, FRAME 12, FRAME12
    m = re.search(r"\b(?:FR|FRAME)\s*[-#:]?\s*(\d{1,4})\b", raw, flags=re.IGNORECASE)
    if m:
        out["frame"] = int(m.group(1))

    # Crack present?
    if re.search(r"\bno\s+(visible\s+)?crack(s)?\b", raw, flags=re.IGNORECASE):
        out["has_crack"] = False
    elif re.search(r"\bvisible\s+crack(s)?\b|\bcrack(s)?\s+present\b", raw, flags=re.IGNORECASE):
        out["has_crack"] = True
    elif re.search(r"\bcrack(s)?\b", raw, flags=re.IGNORECASE):
        out["has_crack"] = True

    # Dent measurements (mm; also accept inches and convert)
    dia_mm = _find_float(raw, [
        r"(\d+(?:\.\d+)?)\s*mm\s*(?:dia|diameter)\b",
        r"\bdia\s*(\d+(?:\.\d+)?)\s*mm\b",
    ])
    depth_mm = _find_float(raw, [
        r"(\d+(?:\.\d+)?)\s*mm\s*depth\b",
        r"\bdepth\s*(\d+(?:\.\d+)?)\s*mm\b",
    ])

    dia_in = _find_float(raw, [
        r"(\d+(?:\.\d+)?)\s*(?:in|inch|in\.)\s*(?:dia|diameter)\b",
        r"\bdia\s*(\d+(?:\.\d+)?)\s*(?:in|inch|in\.)\b",
    ])
    depth_in = _find_float(raw, [
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
# Assessments DB (schema-tolerant)
# -----------------------------
def _ensure_assessments_table(con: sqlite3.Connection) -> None:
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
          final_statement TEXT,
          required_ata INTEGER
        );
        """
    )


def _table_columns(con: sqlite3.Connection, table: str) -> List[str]:
    rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    return [r[1] for r in rows]  # name


def log_assessment(db_path: Path, payload: Dict[str, Any]) -> None:
    con = sqlite3.connect(str(db_path))
    try:
        _ensure_assessments_table(con)
        con.commit()

        cols = set(_table_columns(con, "assessments"))

        # Only insert fields that exist
        row: Dict[str, Any] = {}
        for k, v in payload.items():
            if k in cols:
                row[k] = v

        if "created_utc" in cols and "created_utc" not in row:
            row["created_utc"] = utc_now_iso()

        keys = list(row.keys())
        if not keys:
            return

        sql = f"INSERT INTO assessments ({', '.join(keys)}) VALUES ({', '.join(['?']*len(keys))})"
        con.execute(sql, tuple(row[k] for k in keys))
        con.commit()
    finally:
        con.close()


# -----------------------------
# SRM helpers
# -----------------------------
def infer_ata_from_doc_id(doc_title: str, file_name: Optional[str]) -> Optional[int]:
    """
    Extract ATA chapter from doc title / file:
      SRM_53-00-01_ADL1 -> 53
      SRM_57-xx-xx -> 57
    """
    hay = f"{doc_title} {file_name or ''}"
    m = re.search(r"\bSRM[_-]?(\d{2})\b", hay, flags=re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"\b(\d{2})-\d{2}-\d{2}\b", hay)
    if m:
        return int(m.group(1))
    return None


def open_sqlite(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(path))


def available_ata_chapters_in_srm(conn: sqlite3.Connection, aircraft_family: Optional[str]) -> List[int]:
    sql = "SELECT title, file_name FROM docs WHERE (? IS NULL OR aircraft_family=?)"
    rows = conn.execute(sql, (aircraft_family, aircraft_family)).fetchall()
    atas: List[int] = []
    for title, file_name in rows:
        ata = infer_ata_from_doc_id(title or "", file_name)
        if ata is not None:
            atas.append(ata)
    return sorted(set(atas))


def _stringer_to_axis(s: str) -> Optional[int]:
    """
    Convert "10L" -> -10, "24R" -> +24 for easy range compare.
    """
    if not s:
        return None
    m = re.match(r"^\s*(\d{1,4})\s*([LR])\s*$", s.strip().upper())
    if not m:
        return None
    n = int(m.group(1))
    side = m.group(2)
    return -n if side == "L" else n


def extract_coverage_ranges_from_doc(conn: sqlite3.Connection, doc_title: str, file_name: Optional[str]) -> Dict[str, Any]:
    """
    Extract coverage ranges from ALL pages of a doc:
    - station ranges: "between Stations 360-540"
    - stringer ranges: "between Stringers 24L-24R", "S-10L TO S-10R"
    - frame ranges: "between Frames 12-34" (if present)
    """
    row = conn.execute(
        "SELECT id FROM docs WHERE title=? OR file_name=? LIMIT 1",
        (doc_title, file_name or doc_title),
    ).fetchone()

    if not row:
        return {"found": False, "stations": [], "stringers": [], "frames": []}

    doc_id = int(row[0])

    pages = conn.execute("SELECT page_no, text FROM pages WHERE doc_id=? ORDER BY page_no", (doc_id,)).fetchall()
    full = "\n".join((t or "") for _, t in pages)

    stations: List[Tuple[float, float]] = []
    stringers: List[Tuple[str, str]] = []
    frames: List[Tuple[int, int]] = []

    # Stations: "Stations 360-540" or "between Stations 1138-1156"
    for m in re.finditer(r"\bStations?\s*(\d{2,5}(?:\.\d+)?)\s*[-–]\s*(\d{2,5}(?:\.\d+)?)", full, flags=re.IGNORECASE):
        try:
            stations.append((float(m.group(1)), float(m.group(2))))
        except Exception:
            pass

    # Stringers: "Stringers 24L-24R" / "between Stringers 4R-5R"
    for m in re.finditer(r"\bStringers?\s*(\d{1,3}\s*[LR])\s*[-–]\s*(\d{1,3}\s*[LR])", full, flags=re.IGNORECASE):
        a = m.group(1).replace(" ", "").upper()
        b = m.group(2).replace(" ", "").upper()
        stringers.append((a, b))

    # Stringers: "S-10L TO S-10R"
    for m in re.finditer(r"\bS[-\s]?(\d{1,3})([LR])\s*(?:TO|THRU|THROUGH)\s*S[-\s]?(\d{1,3})([LR])", full, flags=re.IGNORECASE):
        a = f"{int(m.group(1))}{m.group(2).upper()}"
        b = f"{int(m.group(3))}{m.group(4).upper()}"
        stringers.append((a, b))

    # Frames: "Frames 12-34" (generic)
    for m in re.finditer(r"\bFrames?\s*(\d{1,4})\s*[-–]\s*(\d{1,4})", full, flags=re.IGNORECASE):
        try:
            frames.append((int(m.group(1)), int(m.group(2))))
        except Exception:
            pass

    return {"found": True, "stations": stations, "stringers": stringers, "frames": frames}


def verify_location_against_ranges(
    structured: Dict[str, Any],
    coverage: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Compare STA/stringer/frame in the structured input to extracted coverage ranges.
    """
    result = {
        "ok": True,
        "messages": [],
    }

    if not coverage.get("found"):
        return {"ok": False, "messages": ["Could not load SRM excerpt ranges for verification."]}

    sta = structured.get("sta")
    stringer = structured.get("stringer")
    frame = structured.get("frame")

    stations = coverage.get("stations") or []
    stringers = coverage.get("stringers") or []
    frames = coverage.get("frames") or []

    # STA check
    if sta is None:
        result["messages"].append("• No STA provided; cannot verify STA.")
    elif not stations:
        result["messages"].append("• SRM excerpt has no explicit station ranges; cannot verify STA.")
    else:
        inside = any(lo <= float(sta) <= hi or hi <= float(sta) <= lo for lo, hi in stations)
        if inside:
            result["messages"].append(f"• STA {sta:g} appears within SRM station coverage.")
        else:
            result["ok"] = False
            result["messages"].append(f"• STA {sta:g} is NOT within SRM station coverage ranges: {stations}")

    # Stringer check
    if not stringer:
        result["messages"].append("• No stringer provided; cannot verify stringer.")
    elif not stringers:
        result["messages"].append("• SRM excerpt has no explicit stringer ranges; cannot verify stringer.")
    else:
        x = _stringer_to_axis(stringer)
        if x is None:
            result["messages"].append(f"• Stringer '{stringer}' not recognized; cannot verify.")
        else:
            ok = False
            for a, b in stringers:
                xa = _stringer_to_axis(a)
                xb = _stringer_to_axis(b)
                if xa is None or xb is None:
                    continue
                lo, hi = (xa, xb) if xa <= xb else (xb, xa)
                if lo <= x <= hi:
                    ok = True
                    break
            if ok:
                result["messages"].append(f"• Stringer {stringer} appears within SRM stringer coverage.")
            else:
                result["ok"] = False
                result["messages"].append(f"• Stringer {stringer} is NOT within SRM stringer coverage ranges: {stringers}")

    # Frame check
    if frame is None:
        result["messages"].append("• No frame (FR) provided; cannot verify frame.")
    elif not frames:
        result["messages"].append("• SRM excerpt has no explicit frame ranges; cannot verify frame.")
    else:
        inside = any(lo <= int(frame) <= hi or hi <= int(frame) <= lo for lo, hi in frames)
        if inside:
            result["messages"].append(f"• Frame {frame} appears within SRM frame coverage.")
        else:
            result["ok"] = False
            result["messages"].append(f"• Frame {frame} is NOT within SRM frame coverage ranges: {frames}")

    return result


# -----------------------------
# Rules engine adapter
# -----------------------------
def run_rules_engine(structured: Dict[str, Any]) -> Tuple[Any, Dict[str, Any]]:
    """
    Calls your rules_engine.assess_damage(db_path, aircraft_family, ctx, revision=None)
    """
    debug: Dict[str, Any] = {"selected": None, "signature": None, "ctx_sent": None}
    if not HAS_RULES_ENGINE or rules_engine is None:
        return [{"status": "skipped", "reason": "rules_engine not available"}], debug
    if not RULES_DB.exists():
        return [{"status": "skipped", "reason": "rules.db not found in deployment"}], debug

    fn = getattr(rules_engine, "assess_damage", None)
    if fn is None:
        exports = sorted([x for x in dir(rules_engine) if not x.startswith("_")])
        debug["module_exports"] = exports
        return [{"error": "rules_engine has no assess_damage()"}], debug

    debug["selected"] = "assess_damage"
    try:
        debug["signature"] = str(inspect.signature(fn))
    except Exception:
        debug["signature"] = None

    # ctx structure expected by your current rules_engine.py
    ctx = {
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

    debug["ctx_sent"] = ctx

    try:
        # IMPORTANT: call with positional args to avoid "multiple values" bug
        res = fn(str(RULES_DB), str(structured.get("aircraft_family") or "UNKNOWN"), ctx, None)
        if is_dataclass(res):
            return asdict(res), debug
        return res, debug
    except Exception as e:
        return {"error": str(e)}, debug


# -----------------------------
# Dent model adapter
# -----------------------------
def run_dent_model(structured: Dict[str, Any]) -> Tuple[Any, Dict[str, Any]]:
    debug: Dict[str, Any] = {}
    if not HAS_DAMAGE_MODELS or DentDamage is None or assess_dent is None:
        return {"status": "skipped", "reason": "damage_models not available"}, debug

    if structured.get("damage_type") != "DENT":
        return {"status": "skipped", "reason": "damage_type is not DENT"}, debug

    try:
        sig = inspect.signature(DentDamage)  # type: ignore
        debug["DentDamage_signature"] = str(sig)
        accepted = list(sig.parameters.keys())
        debug["accepted_params"] = accepted

        crack_present = structured.get("has_crack")
        # Default crack_present safely:
        # if unknown -> False (do NOT assume crack)
        crack_present_bool = bool(crack_present) if crack_present is not None else False
        debug["crack_present_used"] = crack_present_bool
        debug["crack_present_reason"] = "defaulted False because crack status was Unknown" if crack_present is None else "from input"

        candidate = {
            "aircraft_type": structured.get("aircraft_family") or "UNKNOWN",
            "structure_zone": structured.get("structure_zone") or "UNKNOWN",
            "side": structured.get("side") or "ANY",
            "sta": None if structured.get("sta") is None else str(int(structured.get("sta"))),
            "stringer": structured.get("stringer"),
            "dent_diameter_mm": float(structured.get("dent_diameter_mm") or 0.0),
            "dent_depth_mm": float(structured.get("dent_depth_mm") or 0.0),
            "crack_present": crack_present_bool,
            "notes": structured.get("notes"),
        }

        kwargs = {k: v for k, v in candidate.items() if k in accepted}
        debug["filtered_kwargs_used"] = kwargs

        dent = DentDamage(**kwargs)  # type: ignore
        res = assess_dent(dent)  # type: ignore

        if is_dataclass(res):
            return {"result": str(res)}, debug
        if isinstance(res, dict):
            return res, debug
        return {"result": str(res)}, debug

    except Exception as e:
        return {"status": "error", "error": f"Could not construct/run DentDamage: {e}"}, debug


# -----------------------------
# SRM Search adapter (ATA-gated)
# -----------------------------
def run_srm_search(structured: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any], Optional[int]]:
    debug: Dict[str, Any] = {"selected": None, "signature": None, "query_used": None}
    if not HAS_SRM_SEARCH or srm_search is None:
        return [{"status": "skipped", "reason": "srm_search module not available"}], debug, None
    if not SRM_DB.exists():
        return [{"status": "skipped", "reason": "srm_index.db not found in deployment"}], debug, None

    required_ata = _infer_required_ata(structured.get("structure"))
    debug["required_ata"] = required_ata

    conn = open_sqlite(SRM_DB)
    try:
        # what function do we have?
        fn = getattr(srm_search, "search_srm", None)
        if fn is None:
            return [{"error": "srm_search has no search_srm(conn, query, aircraft_family, limit)"}], debug, required_ata

        debug["selected"] = "search_srm"
        try:
            debug["signature"] = str(inspect.signature(fn))
        except Exception:
            debug["signature"] = None

        # Build SRM query (keywords)
        q_bits = []
        if structured.get("aircraft_family"):
            q_bits.append(str(structured["aircraft_family"]))
        if structured.get("structure"):
            q_bits.append(str(structured["structure"]))
        if structured.get("structure_zone"):
            q_bits.append(str(structured["structure_zone"]))
        if structured.get("damage_type"):
            q_bits.append(str(structured["damage_type"]))
        # Add "allowable damage" anchors because excerpt is AllowableDamage1
        q_bits.append("allowable damage")
        q_bits.append("table 102")
        query = " ".join(q_bits).strip()
        debug["query_used"] = query

        hits = fn(conn, query=query, aircraft_family=structured.get("aircraft_family"), limit=8)

        # Convert SRMHit dataclasses to dict
        out: List[Dict[str, Any]] = []
        for h in hits or []:
            if is_dataclass(h):
                d = asdict(h)
            elif isinstance(h, dict):
                d = h
            else:
                d = {"raw": str(h)}

            # Compute ATA from returned hit
            doc_title = d.get("doc_title") or d.get("title") or ""
            file_name = d.get("file_name")
            ata = infer_ata_from_doc_id(str(doc_title), str(file_name) if file_name else None)
            d["ata"] = ata
            out.append(d)

        # ATA gate: if required_ata exists, only keep those hits
        if required_ata is not None:
            gated = [d for d in out if d.get("ata") == required_ata]
            debug["gated_out_count"] = len(out) - len(gated)
            out = gated

            # If none remain, report "pdf not available"
            if not out:
                # show what ATAs are available for this family
                av = available_ata_chapters_in_srm(conn, structured.get("aircraft_family"))
                return (
                    [{
                        "status": "no_pdf_for_required_ata",
                        "required_ata": required_ata,
                        "required_ata_name": ATA_CHAPTERS.get(required_ata),
                        "available_atas": av,
                        "message": f"No SRM PDF/hit available in library for required ATA {required_ata} ({ATA_CHAPTERS.get(required_ata,'Unknown')})."
                    }],
                    debug,
                    required_ata
                )

        return out, debug, required_ata
    finally:
        conn.close()


def format_srm_reference(hit: Dict[str, Any]) -> str:
    doc = hit.get("doc_title") or "SRM"
    page = hit.get("page") or hit.get("page_no") or "?"
    file_name = hit.get("file_name") or "?"
    rev = hit.get("revision") or "UNKNOWN"
    ata = hit.get("ata")
    ata_txt = f"ATA {ata} ({ATA_CHAPTERS.get(int(ata), 'Unknown')})" if isinstance(ata, int) else "ATA ?"
    return f"{doc} • {ata_txt} • Page {page} • File {file_name} (Rev {rev})"


# -----------------------------
# UI state management (fixes “sticky fuselage” bug)
# -----------------------------
def _set_widget_defaults_from_structured(s: Dict[str, Any]) -> None:
    st.session_state["w_aircraft_family"] = s.get("aircraft_family") or ""
    st.session_state["w_structure"] = s.get("structure") or ""
    st.session_state["w_structure_zone"] = s.get("structure_zone") or ""
    st.session_state["w_side"] = s.get("side") or "ANY"
    st.session_state["w_sta"] = float(s.get("sta") or 0.0)
    st.session_state["w_wl"] = float(s.get("wl") or 0.0)
    st.session_state["w_stringer"] = s.get("stringer") or ""
    st.session_state["w_frame"] = int(s.get("frame") or 0)
    st.session_state["w_damage_type"] = s.get("damage_type") or "OTHER"
    st.session_state["w_dent_dia"] = float(s.get("dent_diameter_mm") or 0.0)
    st.session_state["w_dent_depth"] = float(s.get("dent_depth_mm") or 0.0)
    # crack select
    if s.get("has_crack") is None:
        st.session_state["w_crack"] = "Unknown"
    elif s.get("has_crack") is True:
        st.session_state["w_crack"] = "Yes"
    else:
        st.session_state["w_crack"] = "No"


def build_structured_from_widgets(raw_text: str) -> Dict[str, Any]:
    structured: Dict[str, Any] = {
        "raw": (raw_text or "").strip(),
        "aircraft_family": _normalize_aircraft_family(st.session_state.get("w_aircraft_family", "")) or None,
        "structure": (st.session_state.get("w_structure", "") or "").strip().upper() or None,
        "structure_zone": (st.session_state.get("w_structure_zone", "") or "").strip().upper() or None,
        "side": st.session_state.get("w_side", "ANY"),
        "sta": None if float(st.session_state.get("w_sta", 0.0)) == 0.0 else float(st.session_state.get("w_sta", 0.0)),
        "wl": None if float(st.session_state.get("w_wl", 0.0)) == 0.0 else float(st.session_state.get("w_wl", 0.0)),
        "stringer": (st.session_state.get("w_stringer", "") or "").strip().upper() or None,
        "frame": None if int(st.session_state.get("w_frame", 0) or 0) == 0 else int(st.session_state.get("w_frame")),
        "damage_type": st.session_state.get("w_damage_type", "OTHER"),
        "dent_diameter_mm": None if float(st.session_state.get("w_dent_dia", 0.0)) == 0.0 else float(st.session_state.get("w_dent_dia")),
        "dent_depth_mm": None if float(st.session_state.get("w_dent_depth", 0.0)) == 0.0 else float(st.session_state.get("w_dent_depth")),
        "has_crack": None,
        "notes": None,
    }

    crack_opt = st.session_state.get("w_crack", "Unknown")
    if crack_opt == "Unknown":
        structured["has_crack"] = None
    elif crack_opt == "Yes":
        structured["has_crack"] = True
    else:
        structured["has_crack"] = False

    return structured


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

    st.subheader("ATA mapping")
    st.caption("Structure→ATA: Fuselage(53), Stabilizers(55), Wings(57).")
    st.divider()
    st.caption("Tip: On Streamlit Cloud, only files committed to GitHub are available at runtime.")


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
        help="Paste a single-line or multi-line AOG description. The app will parse into structured fields.",
        key="desc_text",
    )

    c1, c2 = st.columns([0.25, 0.75])
    with c1:
        parse_now = st.button("Parse description", type="primary")
    with c2:
        st.caption("Parsing updates the structured widgets (fixes previous issue where structure stayed as fuselage).")

    if "structured" not in st.session_state:
        st.session_state.structured = parse_damage_description(default_text)
        _set_widget_defaults_from_structured(st.session_state.structured)

    if parse_now:
        st.session_state.structured = parse_damage_description(desc)
        _set_widget_defaults_from_structured(st.session_state.structured)

    st.subheader("2) Structured fields")

    f1, f2, f3, f4 = st.columns(4)
    with f1:
        st.text_input("Aircraft family", key="w_aircraft_family")
        st.text_input("Structure (e.g., FUSELAGE / WING / STABILIZERS)", key="w_structure")
    with f2:
        st.text_input("Zone (e.g., SKIN)", key="w_structure_zone")
        st.selectbox("Side", ["ANY", "LH", "RH"], key="w_side")
    with f3:
        st.number_input("STA", step=1.0, format="%.1f", key="w_sta")
        st.number_input("WL", step=1.0, format="%.1f", key="w_wl")
    with f4:
        st.text_input("Stringer (e.g., 10L)", key="w_stringer")
        st.number_input("Frame / FR (e.g., 12)", step=1, key="w_frame")

    d1, d2, d3 = st.columns(3)
    with d1:
        st.selectbox("Damage type", ["DENT", "CRACK", "GOUGE", "CORROSION", "OTHER"], key="w_damage_type")
    with d2:
        st.number_input("Dent diameter (mm)", step=0.1, format="%.2f", key="w_dent_dia")
    with d3:
        st.number_input("Dent depth (mm)", step=0.1, format="%.2f", key="w_dent_depth")

    st.selectbox("Crack present?", ["Unknown", "No", "Yes"], key="w_crack")

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
        structured = build_structured_from_widgets(desc)

        # Required ATA from structure
        required_ata = _infer_required_ata(structured.get("structure"))

        # Dent model
        dent_result, dent_debug = run_dent_model(structured)

        # Rules engine
        rules_res, rules_debug = run_rules_engine(structured)

        # SRM Search (ATA-gated)
        srm_hits, srm_debug, required_ata_from_srm = run_srm_search(structured)
        required_ata = required_ata_from_srm if required_ata_from_srm is not None else required_ata

        # Top SRM ref
        top_ref = None
        top_hit = None
        if srm_hits and isinstance(srm_hits, list) and isinstance(srm_hits[0], dict):
            if srm_hits[0].get("status") == "no_pdf_for_required_ata":
                top_ref = None
            else:
                top_hit = srm_hits[0]
                top_ref = format_srm_reference(top_hit)

        # Location verification using top hit's doc
        location_verify = None
        location_msgs = []
        if top_hit and SRM_DB.exists():
            try:
                conn = open_sqlite(SRM_DB)
                cov = extract_coverage_ranges_from_doc(conn, str(top_hit.get("doc_title") or ""), str(top_hit.get("file_name") or ""))
                location_verify = verify_location_against_ranges(structured, cov)
                location_msgs = location_verify.get("messages", [])
                conn.close()
            except Exception as e:
                location_verify = {"ok": False, "messages": [f"Location verification error: {e}"]}

        # Final statement (SRM-based only if ATA gated hits exist)
        final_statement = None
        if top_ref:
            dtype = structured.get("damage_type") or "DAMAGE"
            # If dent depth present and obviously above 6.35mm we can say out-of-limits (prototype)
            disposition_hint = "within limits"
            if structured.get("dent_depth_mm") is not None and float(structured.get("dent_depth_mm")) > 6.35:
                disposition_hint = "out of limits"
            # If crack present, never claim within limits
            if structured.get("has_crack") is True:
                disposition_hint = "out of limits"

            final_statement = f"{dtype} is found {disposition_hint} per {top_ref}"

        st.markdown("### Final statement (SRM-based)")
        if final_statement:
            st.success(final_statement)
        else:
            # if no SRM due to ATA gating
            if srm_hits and isinstance(srm_hits[0], dict) and srm_hits[0].get("status") == "no_pdf_for_required_ata":
                st.warning(srm_hits[0].get("message"))
            else:
                st.info("No SRM-based statement produced (no ATA-matching SRM excerpt found).")

        # Show SRM reference
        if top_ref:
            st.markdown("### SRM Reference (top hit)")
            st.code(top_ref, language="text")
            snippet = str(top_hit.get("snippet") or "")[:1200]
            st.code(snippet, language="text")

        # Location verification panel
        st.markdown("### Location verification vs SRM excerpt")
        if location_verify:
            if location_verify.get("ok"):
                st.success("Location appears to be within the SRM excerpt coverage ranges (based on extracted ranges).")
            else:
                st.error("Location does NOT appear to be within the SRM excerpt coverage ranges (based on extracted ranges).")
            for m in location_msgs:
                st.write(m)
        else:
            st.info("No SRM top hit available to verify location ranges.")

        # Dent model output
        st.markdown("### Dent model output")
        st.json(dent_result)
        with st.expander("Dent model debug", expanded=False):
            st.json(dent_debug)

        # Rules output
        st.markdown("### Rules matches")
        st.json(rules_res)
        with st.expander("Rules engine debug", expanded=False):
            st.json(rules_debug)

        # SRM hits output
        st.markdown("### SRM search hits (prototype)")
        if srm_hits and isinstance(srm_hits, list) and isinstance(srm_hits[0], dict) and srm_hits[0].get("status") == "no_pdf_for_required_ata":
            st.warning(srm_hits[0].get("message"))
            st.caption(f"Available ATAs in library for this aircraft: {srm_hits[0].get('available_atas')}")
        else:
            for h in srm_hits[:8]:
                st.json(h)

        with st.expander("SRM search debug", expanded=False):
            st.json(srm_debug)

        # Logging
        st.markdown("### Logging")
        log_it = st.checkbox("Log this assessment to SQLite (assessments.db)", value=True)
        if log_it:
            try:
                payload = {
                    "created_utc": utc_now_iso(),
                    "aircraft_family": structured.get("aircraft_family"),
                    "structure": structured.get("structure"),
                    "structure_zone": structured.get("structure_zone"),
                    "side": structured.get("side"),
                    "sta": structured.get("sta"),
                    "wl": structured.get("wl"),
                    "stringer": structured.get("stringer"),
                    "frame": structured.get("frame"),
                    "damage_type": structured.get("damage_type"),
                    "dent_diameter_mm": structured.get("dent_diameter_mm"),
                    "dent_depth_mm": structured.get("dent_depth_mm"),
                    "has_crack": None if structured.get("has_crack") is None else (1 if structured.get("has_crack") else 0),
                    "input_text": structured.get("raw"),
                    "structured_json": safe_json(structured),
                    "rules_json": safe_json(rules_res),
                    "srm_hits_json": safe_json(srm_hits),
                    "result_json": safe_json(dent_result),
                    "final_statement": final_statement,
                    "required_ata": required_ata,
                }
                log_assessment(ASSESSMENTS_DB, payload)
                st.success("Logged to assessments.db")
            except Exception as e:
                st.error(f"Failed to log assessment: {e}")

    else:
        st.info("Paste a description, optionally Parse, then click **Run rules + SRM search + dent model**.")


# -----------------------------
# Assessment history
# -----------------------------
st.divider()
st.subheader("Assessment history (SQLite)")

if ASSESSMENTS_DB.exists():
    try:
        con = sqlite3.connect(str(ASSESSMENTS_DB))
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT * FROM assessments ORDER BY id DESC LIMIT 25").fetchall()
        con.close()
        if rows:
            st.dataframe([dict(r) for r in rows], use_container_width=True, hide_index=True)
        else:
            st.info("No logs yet.")
    except Exception as e:
        st.error(f"Could not read assessments.db: {e}")
else:
    st.caption("No assessments.db yet. Run an assessment and enable logging to create it.")
