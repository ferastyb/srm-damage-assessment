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
# Designed to be resilient on Streamlit Cloud:
# - If a module/DB is missing, the app continues with warnings.
#
# Repo layout assumptions (root):
# - dent_checker_app.py  (this file)
# - damage_models.py     (your dent model + assess_dent, etc.)
# - rules_engine.py      (rules evaluation)
# - srm_search.py        (search SRM index)
# - rules.db             (rules DB)
# - srm_index.db         (SRM search DB)  <-- must be committed if you want SRM hits on Streamlit
#
# Tip:
# - If Streamlit shows "old glued text", it often means it is using an older srm_index.db.
#   This app prints SRM DB size + sha256 prefix to confirm which DB is deployed.

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

damage_models_err = None
rules_engine_err = None
srm_search_err = None

DentDamage = None
assess_dent = None
build_plain_text_summary = None

try:
    # expected exports in your project (best-effort):
    # - DentDamage (dataclass)
    # - assess_dent(dent: DentDamage, ...) -> dict or result
    # - build_plain_text_summary(result, ...) -> str (optional)
    from damage_models import DentDamage as _DentDamage  # type: ignore
    from damage_models import assess_dent as _assess_dent  # type: ignore

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
    # expected exports:
    # - assess_damage(db_path, aircraft_family, ctx, revision=None) -> AssessmentResult
    import rules_engine  # type: ignore

    HAS_RULES_ENGINE = True
except Exception as e:
    rules_engine_err = e
    rules_engine = None  # type: ignore

try:
    # expected exports:
    # - search_srm(conn, query, aircraft_family=None, limit=6) -> list[SRMHit]
    # or module includes SRMHit dataclass
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
    # Accept common variants
    if t.startswith("B7"):
        # B737, B787, etc.
        return t
    if t.startswith("A3") or t.startswith("A32"):
        return t
    if t.startswith("E1") or t.startswith("E17"):
        return t
    return (text or "").strip().upper()


def _parse_side(text: str) -> str:
    s = (text or "").upper()
    # Common: LH/RH, LHS/RHS, LEFT/RIGHT
    if "LH" in s or "LEFT" in s:
        return "LH"
    if "RH" in s or "RIGHT" in s:
        return "RH"
    return "ANY"


def _find_float_mm(text: str, patterns: List[str]) -> Optional[float]:
    """
    Finds numeric values in mm based on regex patterns that capture number.
    Returns first match as float.
    """
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
    """
    Finds numeric values in inches based on regex patterns that capture number.
    """
    for pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            val = m.group(1)
            try:
                return float(val)
            except Exception:
                continue
    return None


def parse_damage_description(desc: str) -> Dict[str, Any]:
    """
    Lightweight parser for descriptions like:
    “B737 fuselage LH STA 123 S-10L skin dent 100mm dia 13.75mm depth no visible crack”

    Returns a structured dict used by rules_engine + dent assessment + SRM search.
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
    }

    # Aircraft family
    m = re.search(r"\b(B7\d{2}|A3\d{2}|A32\d{2}|E1\d{2}|E17\d)\b", raw, flags=re.IGNORECASE)
    if m:
        out["aircraft_family"] = _normalize_aircraft_family(m.group(1))

    # Structure keywords
    if re.search(r"\bfuselage\b", raw, flags=re.IGNORECASE):
        out["structure"] = "FUSELAGE"
    elif re.search(r"\bwing\b", raw, flags=re.IGNORECASE):
        out["structure"] = "WING"
    elif re.search(r"\bempennage\b|\btail\b", raw, flags=re.IGNORECASE):
        out["structure"] = "EMPENNAGE"

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

    # Stringer formats: S-10L, S10L, Stringer 10L
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

    # Crack present?
    if re.search(r"\bno\s+(visible\s+)?crack(s)?\b", raw, flags=re.IGNORECASE):
        out["has_crack"] = False
    elif re.search(r"\bcrack(s)?\b", raw, flags=re.IGNORECASE):
        out["has_crack"] = True

    # Dent dimensions (mm or inches)
    dia_mm = _find_float_mm(
        raw,
        [
            r"(\d+(?:\.\d+)?)\s*mm\s*(?:dia|diameter)\b",
            r"\bdia\s*(\d+(?:\.\d+)?)\s*mm\b",
            r"dent\s*(\d+(?:\.\d+)?)\s*mm\s*(?:dia|diameter)\b",
        ],
    )
    depth_mm = _find_float_mm(
        raw,
        [
            r"(\d+(?:\.\d+)?)\s*mm\s*depth\b",
            r"\bdepth\s*(\d+(?:\.\d+)?)\s*mm\b",
        ],
    )

    dia_in = _find_float_in(
        raw,
        [
            r"(\d+(?:\.\d+)?)\s*(?:in|inch|in\.)\s*(?:dia|diameter)\b",
            r"\bdia\s*(\d+(?:\.\d+)?)\s*(?:in|inch|in\.)\b",
        ],
    )
    depth_in = _find_float_in(
        raw,
        [
            r"(\d+(?:\.\d+)?)\s*(?:in|inch|in\.)\s*depth\b",
            r"\bdepth\s*(\d+(?:\.\d+)?)\s*(?:in|inch|in\.)\b",
        ],
    )

    if dia_mm is None and dia_in is not None:
        dia_mm = dia_in * 25.4
    if depth_mm is None and depth_in is not None:
        depth_mm = depth_in * 25.4

    out["dent_diameter_mm"] = dia_mm
    out["dent_depth_mm"] = depth_mm

    return out


def _flatten_ctx(structured: Dict[str, Any]) -> Dict[str, Any]:
    """Build the ctx shape expected by rules_engine.assess_damage (nested + _flat)."""
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
        "_flat": {
            "raw": structured.get("raw"),
            "aircraft_family": structured.get("aircraft_family"),
            "structure": structured.get("structure"),
            "structure_zone": structured.get("structure_zone"),
            "side": structured.get("side"),
            "sta": structured.get("sta"),
            "wl": structured.get("wl"),
            "stringer": structured.get("stringer"),
            "damage_type": structured.get("damage_type"),
            "dent_diameter_mm": structured.get("dent_diameter_mm"),
            "dent_depth_mm": structured.get("dent_depth_mm"),
            "has_crack": structured.get("has_crack"),
            "notes": structured.get("notes"),
        },
    }
    return ctx


def _srm_wy_and_within_limits(structured: Dict[str, Any]) -> Dict[str, Any]:
    """
    Lightweight SRM logic for B737 SRM 53-00-01 ADL1 Table 102:
    - W/Y must be >= 30 to be "allowable/no repair" ONLY when depth < 3.175mm AND crack is absent.
    - Anything else => not "within limits" (may still be deferrable/repair category).
    Here we treat W as dent diameter (proxy for dent width).
    """
    dia = structured.get("dent_diameter_mm")
    dep = structured.get("dent_depth_mm")
    has_crack = structured.get("has_crack")

    out: Dict[str, Any] = {
        "w_mm": None,
        "y_mm": None,
        "wy_ratio": None,
        "within_limits": None,
        "classification": None,
        "notes": [],
    }

    if structured.get("damage_type") != "DENT":
        out["within_limits"] = None
        out["classification"] = "not_a_dent"
        return out

    if dia is None or dep is None or dep == 0:
        out["within_limits"] = None
        out["classification"] = "missing_dimensions"
        out["notes"].append("Need dent diameter (W) and depth (Y) to compute W/Y.")
        return out

    out["w_mm"] = float(dia)
    out["y_mm"] = float(dep)
    ratio = float(dia) / float(dep) if float(dep) != 0 else None
    out["wy_ratio"] = ratio

    # Crack gate: Table 102 requirements include "no cracks"
    if has_crack is True:
        out["within_limits"] = False
        out["classification"] = "crack_present_not_allowable"
        out["notes"].append("Table 102 requires no cracks/gouges/creases; crack present => not allowable.")
        return out

    # Depth gate: > 6.35mm not allowable
    if float(dep) > 6.35:
        out["within_limits"] = False
        out["classification"] = "depth_gt_6_35mm_not_allowable"
        out["notes"].append("Depth > 6.35mm (0.25in) => not allowable per Table 102.")
        return out

    # If crack unknown, we do NOT claim within limits.
    if has_crack is None:
        out["within_limits"] = False
        out["classification"] = "crack_unknown"
        out["notes"].append("Crack status unknown; cannot claim 'within limits'.")
        return out

    # "Allowable / no repair necessary" only when depth < 3.175mm AND W/Y >= 30
    if float(dep) < 3.175 and ratio is not None and ratio >= 30.0:
        out["within_limits"] = True
        out["classification"] = "allowable_no_repair"
        return out

    # Otherwise, not "within limits" (repair/deferral categories)
    out["within_limits"] = False
    if ratio is not None and ratio < 15.0:
        out["classification"] = "repair_required_ratio_lt_15"
    elif ratio is not None and ratio < 30.0:
        out["classification"] = "repair_required_ratio_15_to_30"
    else:
        out["classification"] = "repair_required_ratio_ge_30_depth_ge_3_175"
    return out


def _final_statement(
    structured: Dict[str, Any],
    top_hit: Optional[Dict[str, Any]],
    srm_eval: Dict[str, Any],
) -> str:
    """
    Desired format:
      [damage type] is found [within/out of] [aircraft type] SRM [ATA chapter-ATAsubchapter, Allowable Damage #, table #], [page #]
    We infer SRM identifiers from the deployed excerpt (B737 SRM_53-00-01_ADL1, Table 102).
    """
    dmg = (structured.get("damage_type") or "DAMAGE").upper()
    ac = structured.get("aircraft_family") or "UNKNOWN"
    within = srm_eval.get("within_limits")
    within_txt = "within limits" if within else "out of limits"

    # For now we hard-wire the SRM reference block for this excerpt
    ata = "53-00-01"
    adl = "Allowable Damage 1"
    table = "Table 102"

    page = None
    if top_hit:
        page = top_hit.get("page") or top_hit.get("page_no") or top_hit.get("page_no".upper())

    page_txt = f"Page {page}" if page else "Page (unknown)"
    return f"{dmg} is found {within_txt} per {ac} SRM {ata}, {adl}, {table}, {page_txt}."


def _to_dict(obj: Any) -> Any:
    if is_dataclass(obj):
        return asdict(obj)
    if hasattr(obj, "__dict__") and not isinstance(obj, (str, int, float, bool, list, dict, tuple)):
        try:
            return dict(obj.__dict__)
        except Exception:
            return str(obj)
    return obj


def _call_rules_engine(
    structured: Dict[str, Any],
) -> Tuple[Any, Dict[str, Any]]:
    """
    Calls rules_engine with best-effort compatibility.
    Returns (rules_result, debug_dict).
    """
    dbg: Dict[str, Any] = {
        "selected": None,
        "signature": None,
        "ctx_sent": None,
        "module_exports": [],
    }

    if not HAS_RULES_ENGINE or rules_engine is None:
        return {"error": "rules_engine not available"}, dbg

    try:
        dbg["module_exports"] = sorted([k for k in dir(rules_engine) if not k.startswith("_")])[:200]
    except Exception:
        pass

    if not RULES_DB.exists():
        return {"error": "rules.db not found in deployment"}, dbg

    ctx = _flatten_ctx(structured)
    dbg["ctx_sent"] = ctx

    # Prefer assess_damage(db_path, aircraft_family, ctx, revision=None)
    fn = None
    for name in ("assess_damage", "evaluate_rules", "run_rules", "evaluate"):
        if hasattr(rules_engine, name):
            fn = getattr(rules_engine, name)
            dbg["selected"] = name
            break

    if fn is None or not callable(fn):
        return {"error": "rules_engine has no compatible rules function (assess_damage/evaluate_rules/run_rules/evaluate)"}, dbg

    try:
        dbg["signature"] = str(inspect.signature(fn))
    except Exception:
        dbg["signature"] = None

    try:
        # Call style for assess_damage:
        #   assess_damage(db_path: str, aircraft_family: str, ctx: Dict[str,Any], revision: Optional[str]=None)
        if dbg["selected"] == "assess_damage":
            res = fn(str(RULES_DB), structured.get("aircraft_family") or "UNKNOWN", ctx)  # type: ignore
        else:
            # For other names, try (db_path, structured) then (db_path, aircraft_family, ctx)
            try:
                res = fn(str(RULES_DB), structured)  # type: ignore
            except Exception:
                res = fn(str(RULES_DB), structured.get("aircraft_family") or "UNKNOWN", ctx)  # type: ignore
        return _to_dict(res), dbg
    except Exception as e:
        return {"error": str(e)}, dbg


def _construct_and_run_dent_model(structured: Dict[str, Any]) -> Tuple[Any, Dict[str, Any]]:
    """
    Constructs DentDamage using signature inspection, then calls assess_dent().
    Returns (dent_result, debug_dict).
    """
    dbg: Dict[str, Any] = {
        "DentDamage_signature": None,
        "accepted_params": [],
        "filtered_kwargs_used": {},
        "dropped_candidate_keys": [],
        "crack_present_used": None,
        "crack_present_reason": None,
    }

    if not HAS_DAMAGE_MODELS or DentDamage is None or assess_dent is None:
        return {"status": "skipped", "reason": "damage_models module not available"}, dbg

    if structured.get("damage_type") != "DENT":
        return {"status": "skipped", "reason": "damage_type is not DENT"}, dbg

    try:
        sig = inspect.signature(DentDamage)  # type: ignore[arg-type]
        dbg["DentDamage_signature"] = str(sig)
        accepted = list(sig.parameters.keys())
        dbg["accepted_params"] = accepted
    except Exception as e:
        return {"status": "error", "error": f"Could not inspect DentDamage signature: {e}"}, dbg

    # Candidate kwargs from structured (we offer many names, then filter to accepted)
    candidates: Dict[str, Any] = {
        # primary names in your current damage_models.py
        "aircraft_type": structured.get("aircraft_family") or "UNKNOWN",
        "structure_zone": structured.get("structure_zone") or "UNKNOWN",
        "side": structured.get("side") or "ANY",
        "sta": (str(int(structured["sta"])) if structured.get("sta") is not None else None),
        "stringer": structured.get("stringer"),
        "dent_diameter_mm": float(structured["dent_diameter_mm"]) if structured.get("dent_diameter_mm") is not None else 0.0,
        "dent_depth_mm": float(structured["dent_depth_mm"]) if structured.get("dent_depth_mm") is not None else 0.0,
        "crack_present": None,  # set below
        "notes": structured.get("notes"),
        # alternates (in case model signature changes again)
        "aircraft_family": structured.get("aircraft_family"),
        "zone": structured.get("structure_zone"),
        "diameter_mm": structured.get("dent_diameter_mm"),
        "depth_mm": structured.get("dent_depth_mm"),
        "has_crack": structured.get("has_crack"),
        "crack": structured.get("has_crack"),
        "structure": structured.get("structure"),
    }

    # crack_present logic:
    # - Use structured.has_crack if set
    # - If unknown (None), default False (do NOT assume crack present)
    if structured.get("has_crack") is True:
        crack_present = True
        dbg["crack_present_reason"] = "from structured.has_crack=True"
    elif structured.get("has_crack") is False:
        crack_present = False
        dbg["crack_present_reason"] = "from structured.has_crack=False"
    else:
        crack_present = False
        dbg["crack_present_reason"] = "defaulted False because crack status was Unknown"

    dbg["crack_present_used"] = crack_present
    candidates["crack_present"] = crack_present

    # Filter kwargs to match DentDamage signature
    accepted = set(dbg["accepted_params"])
    filtered = {k: v for k, v in candidates.items() if k in accepted}

    dbg["filtered_kwargs_used"] = filtered
    dbg["dropped_candidate_keys"] = [k for k in candidates.keys() if k not in accepted]

    try:
        dent_obj = DentDamage(**filtered)  # type: ignore[misc]
        res = assess_dent(dent_obj)  # type: ignore[operator]
        return (_to_dict(res), dbg)
    except Exception as e:
        return {"status": "error", "error": f"Could not construct/run DentDamage: {e}"}, dbg


def _open_sqlite_readonly(path: Path) -> sqlite3.Connection:
    # Use URI mode to allow read-only open when supported
    try:
        return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except Exception:
        return sqlite3.connect(str(path))


def _call_srm_search(structured: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Calls srm_search with best-effort compatibility.
    Returns (hits_as_dicts, debug_dict).
    """
    dbg: Dict[str, Any] = {
        "selected": None,
        "signature": None,
        "queries_tried": [],
        "query_used": None,
        "module_exports": [],
    }

    if not HAS_SRM_SEARCH or srm_search is None:
        return ([{"error": "srm_search module not available"}], dbg)

    if not SRM_DB.exists():
        return ([{"error": "srm_index.db not found in deployment"}], dbg)

    try:
        dbg["module_exports"] = sorted([k for k in dir(srm_search) if not k.startswith("_")])[:200]
    except Exception:
        pass

    # Pick search function
    fn = None
    for name in ("search_srm", "search", "srm_search"):
        if hasattr(srm_search, name):
            fn = getattr(srm_search, name)
            dbg["selected"] = name
            break

    if fn is None or not callable(fn):
        return ([{"error": "srm_search has no compatible function (search_srm/search/srm_search)"}], dbg)

    try:
        dbg["signature"] = str(inspect.signature(fn))
    except Exception:
        dbg["signature"] = None

    # Build a few queries (progressive)
    ac = structured.get("aircraft_family") or ""
    struct = structured.get("structure") or ""
    zone = structured.get("structure_zone") or ""
    dmg = structured.get("damage_type") or ""
    q_candidates = [
        f"{ac} {struct} {zone} {dmg} allowable damage dent table 102",
        "allowable damage dent table 102",
        "fuselage dent allowable damage",
        "table 102 dent",
        "dent allowable",
        "allowable damage",
    ]
    dbg["queries_tried"] = q_candidates

    hits_out: List[Dict[str, Any]] = []
    last_err: Optional[str] = None

    # IMPORTANT: your srm_search.search_srm expects a sqlite3.Connection (per your file),
    # so we open a connection and pass it in.
    con = _open_sqlite_readonly(SRM_DB)
    try:
        for q in q_candidates:
            try:
                res = fn(con, q, aircraft_family=structured.get("aircraft_family"), limit=8)  # type: ignore[misc]
                dbg["query_used"] = q
                # Convert SRMHit dataclasses to dicts
                if isinstance(res, list):
                    for item in res:
                        if is_dataclass(item):
                            hits_out.append(asdict(item))
                        elif hasattr(item, "__dict__") and not isinstance(item, dict):
                            hits_out.append(dict(item.__dict__))
                        elif isinstance(item, dict):
                            hits_out.append(item)
                        else:
                            hits_out.append({"value": str(item)})
                else:
                    hits_out = [{"value": str(res)}]
                if hits_out:
                    return hits_out, dbg
            except Exception as e:
                last_err = str(e)
                continue
    finally:
        try:
            con.close()
        except Exception:
            pass

    if last_err:
        return ([{"error": last_err}], dbg)
    return ([], dbg)


# -----------------------------
# Assessments DB (resilient schema)
# -----------------------------
def _ensure_table_and_columns(con: sqlite3.Connection, table: str, columns: Dict[str, str]) -> None:
    con.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {table} (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          created_utc TEXT NOT NULL
        );
        """
    )
    # Discover existing columns
    existing = set()
    for row in con.execute(f"PRAGMA table_info({table});").fetchall():
        existing.add(row[1])

    for col, coltype in columns.items():
        if col not in existing:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype};")


def init_assessments_db(db_path: Path) -> None:
    con = sqlite3.connect(str(db_path))
    try:
        desired_cols = {
            "aircraft_family": "TEXT",
            "structure": "TEXT",
            "structure_zone": "TEXT",
            "side": "TEXT",
            "sta": "REAL",
            "wl": "REAL",
            "stringer": "TEXT",
            "damage_type": "TEXT",
            "dent_diameter_mm": "REAL",
            "dent_depth_mm": "REAL",
            "has_crack": "INTEGER",
            "wy_ratio": "REAL",
            "within_limits": "INTEGER",
            "final_statement": "TEXT",
            "srm_ref_title": "TEXT",
            "srm_ref_page": "INTEGER",
            "srm_ref_file": "TEXT",
            "srm_ref_rev": "TEXT",
            "input_text": "TEXT",
            "structured_json": "TEXT",
            "rules_ctx_json": "TEXT",
            "rules_json": "TEXT",
            "srm_hits_json": "TEXT",
            "result_json": "TEXT",
        }
        _ensure_table_and_columns(con, "assessments", desired_cols)
        con.commit()
    finally:
        con.close()


def log_assessment(
    db_path: Path,
    structured: Dict[str, Any],
    rules_ctx: Dict[str, Any],
    rules_result: Any,
    srm_hits: Any,
    dent_result: Any,
    final_statement: str,
    wy_ratio: Optional[float],
    within_limits: Optional[bool],
    top_hit: Optional[Dict[str, Any]],
) -> None:
    init_assessments_db(db_path)

    con = sqlite3.connect(str(db_path))
    try:
        con.execute(
            """
            INSERT INTO assessments (
              created_utc, aircraft_family, structure, structure_zone, side, sta, wl, stringer,
              damage_type, dent_diameter_mm, dent_depth_mm, has_crack,
              wy_ratio, within_limits, final_statement,
              srm_ref_title, srm_ref_page, srm_ref_file, srm_ref_rev,
              input_text, structured_json, rules_ctx_json, rules_json, srm_hits_json, result_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                structured.get("damage_type"),
                structured.get("dent_diameter_mm"),
                structured.get("dent_depth_mm"),
                None if structured.get("has_crack") is None else (1 if structured.get("has_crack") else 0),
                wy_ratio,
                None if within_limits is None else (1 if within_limits else 0),
                final_statement,
                (top_hit or {}).get("doc_title") if top_hit else None,
                (top_hit or {}).get("page") if top_hit else None,
                (top_hit or {}).get("file_name") if top_hit else None,
                (top_hit or {}).get("revision") if top_hit else None,
                structured.get("raw"),
                safe_json(structured),
                safe_json(rules_ctx),
                safe_json(rules_result),
                safe_json(srm_hits),
                safe_json(dent_result),
            ),
        )
        con.commit()
    finally:
        con.close()


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
    default_text = "B737, fuselage, LH side, STA 123, S-10L, skin dent 100mm dia, 13.75mm depth, no visible crack."
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
        dent_dia = st.number_input("Dent diameter / width W (mm)", value=float(structured.get("dent_diameter_mm") or 0.0), step=0.1, format="%.2f")
    with d2:
        dent_depth = st.number_input("Dent depth Y (mm)", value=float(structured.get("dent_depth_mm") or 0.0), step=0.1, format="%.2f")
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
            st.info("If you want SRM hits on Streamlit Cloud, commit srm_index.db to the repo (PDFs are not needed at runtime).")

    if run:
        # --------------
        # SRM Search (run early so we can cite top hit)
        # --------------
        srm_hits, srm_dbg = _call_srm_search(structured)
        top_hit = None
        if isinstance(srm_hits, list) and srm_hits and isinstance(srm_hits[0], dict) and "error" not in srm_hits[0]:
            top_hit = srm_hits[0]

        if top_hit:
            st.markdown("### SRM Reference (top hit)")
            st.write(
                f"{top_hit.get('doc_title','SRM')} • Page {top_hit.get('page','?')} • "
                f"File {top_hit.get('file_name','?')} (Rev {top_hit.get('revision','?')})"
            )
            st.code(str(top_hit.get("snippet", ""))[:900], language="text")

        # --------------
        # SRM-based W/Y classification (Table 102-lite)
        # --------------
        srm_eval = _srm_wy_and_within_limits(structured)
        final_stmt = _final_statement(structured, top_hit, srm_eval)

        st.markdown("### Final statement (SRM-based)")
        st.write(final_stmt)

        if srm_eval.get("wy_ratio") is not None:
            st.caption(f"W/Y ratio (W=diameter, Y=depth): {srm_eval['wy_ratio']:.2f}")
        if srm_eval.get("notes"):
            for n in srm_eval["notes"]:
                st.caption(f"• {n}")

        # --------------
        # Dent model
        # --------------
        dent_result, dent_dbg = _construct_and_run_dent_model(structured)

        st.markdown("### Dent model output")
        if build_plain_text_summary and isinstance(dent_result, dict) and "error" not in dent_result:
            try:
                summary = build_plain_text_summary(dent_result)  # type: ignore[misc]
                st.code(summary, language="text")
            except Exception:
                st.json(dent_result)
        else:
            st.json(dent_result)

        st.markdown("### Dent model debug")
        st.json(dent_dbg)

        # --------------
        # Rules engine
        # --------------
        rules_ctx = _flatten_ctx(structured)
        rules_result, rules_dbg = _call_rules_engine(structured)

        st.markdown("### Rules matches")
        st.json(rules_result)

        st.markdown("### Rules engine debug")
        st.json(rules_dbg)

        # --------------
        # SRM hits (prototype)
        # --------------
        st.markdown("### SRM search hits (prototype)")
        if isinstance(srm_hits, list) and srm_hits and isinstance(srm_hits[0], dict) and "error" not in srm_hits[0]:
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

        st.markdown("### SRM search debug")
        st.json(srm_dbg)

        # --------------
        # Optional logging
        # --------------
        st.markdown("### Logging")
        log_it = st.checkbox("Log this assessment to SQLite (assessments.db)", value=True)
        if log_it:
            try:
                log_assessment(
                    ASSESSMENTS_DB,
                    structured,
                    rules_ctx=rules_ctx,
                    rules_result=rules_result,
                    srm_hits=srm_hits,
                    dent_result=dent_result,
                    final_statement=final_stmt,
                    wy_ratio=srm_eval.get("wy_ratio"),
                    within_limits=srm_eval.get("within_limits"),
                    top_hit=top_hit,
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
        # Be resilient: select only columns that exist
        cols = [r[1] for r in con.execute("PRAGMA table_info(assessments);").fetchall()]
        want = [
            "id",
            "created_utc",
            "aircraft_family",
            "structure",
            "structure_zone",
            "side",
            "sta",
            "stringer",
            "damage_type",
            "dent_diameter_mm",
            "dent_depth_mm",
            "has_crack",
            "wy_ratio",
            "within_limits",
            "final_statement",
        ]
        have = [c for c in want if c in cols]
        if not have:
            st.caption("assessments.db exists, but schema is missing expected columns.")
        else:
            sql = f"SELECT {', '.join(have)} FROM assessments ORDER BY id DESC LIMIT 25"
            rows = con.execute(sql).fetchall()
            if rows:
                st.write(f"Showing last {len(rows)} logs from assessments.db")
                st.dataframe(
                    [dict(zip(have, r)) for r in rows],
                    use_container_width=True,
                    hide_index=True,
                )
            else:
                st.info("No logs yet.")
        con.close()
    except Exception as e:
        st.error(f"Could not read assessments.db: {e}")
else:
    st.caption("No assessments.db yet. Run an assessment and enable logging to create it.")
