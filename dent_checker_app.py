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
# If Streamlit shows "old glued text", it usually means it is using an older srm_index.db
# This app prints SRM DB size + sha256 prefix to confirm.

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

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
rules_engine = None
srm_search = None

try:
    # expected exports in your project:
    # - DentDamage (dataclass)
    # - assess_dent(dent: DentDamage, ...) -> dict or result
    # - build_plain_text_summary(result, ...) -> str (optional)
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
    “B787, fuselage, LH side, STA 1280, S-10L, skin dent 25mm dia, 3mm depth, no visible crack.”

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

    # Stringer formats: S-10L, S10L, Stringer 10L, 10L
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
    # Diameter patterns
    dia_mm = _find_float_mm(raw, [
        r"(\d+(?:\.\d+)?)\s*mm\s*(?:dia|diameter)\b",
        r"\bdia\s*(\d+(?:\.\d+)?)\s*mm\b",
        r"dent\s*(\d+(?:\.\d+)?)\s*mm\s*(?:dia|diameter)\b",
    ])
    depth_mm = _find_float_mm(raw, [
        r"(\d+(?:\.\d+)?)\s*mm\s*depth\b",
        r"\bdepth\s*(\d+(?:\.\d+)?)\s*mm\b",
    ])

    # Inches (convert to mm if mm missing)
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
              created_utc, aircraft_family, structure, structure_zone, side, sta, wl, stringer,
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


def _safe_signature(obj: Any) -> Optional[str]:
    try:
        return str(inspect.signature(obj))
    except Exception:
        return None


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
    default_text = "B787, fuselage, LH side, STA 1280, S-10L, skin dent 25mm dia, 3mm depth, no visible crack."
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

    # SRM DB Debug (kept as-is, expanded)
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
        # -------------------------
        # Dent model (introspected)
        # -------------------------
        dent_result: Dict[str, Any] = {"status": "not_run"}
        dent_debug: Dict[str, Any] = {}

        if HAS_DAMAGE_MODELS and structured.get("damage_type") == "DENT" and DentDamage is not None and assess_dent is not None:
            try:
                # Candidate kwarg pool (we will filter to ONLY what DentDamage accepts)
                candidate = {
                    # aircraft family aliases
                    "aircraft_family": structured.get("aircraft_family") or "UNKNOWN",
                    "aircraft_type": structured.get("aircraft_family") or "UNKNOWN",
                    "aircraft": structured.get("aircraft_family") or "UNKNOWN",

                    # structure/area aliases
                    "structure": structured.get("structure") or "UNKNOWN",
                    "area": structured.get("structure") or "UNKNOWN",
                    "component": structured.get("structure") or "UNKNOWN",

                    # zone aliases
                    "zone": structured.get("structure_zone") or "UNKNOWN",
                    "structure_zone": structured.get("structure_zone") or "UNKNOWN",
                    "subzone": structured.get("structure_zone") or "UNKNOWN",

                    # location/context
                    "side": structured.get("side") or "ANY",
                    "sta": structured.get("sta"),
                    "wl": structured.get("wl"),
                    "stringer": structured.get("stringer"),

                    # dent size aliases
                    "diameter_mm": structured.get("dent_diameter_mm"),
                    "dent_diameter_mm": structured.get("dent_diameter_mm"),
                    "dia_mm": structured.get("dent_diameter_mm"),

                    "depth_mm": structured.get("dent_depth_mm"),
                    "dent_depth_mm": structured.get("dent_depth_mm"),
                    "dep_mm": structured.get("dent_depth_mm"),

                    # crack aliases
                    "has_crack": structured.get("has_crack"),
                    "crack": structured.get("has_crack"),

                    # misc
                    "notes": structured.get("notes"),
                }

                accepted: List[str] = []
                try:
                    sig = inspect.signature(DentDamage)  # type: ignore
                    accepted = list(sig.parameters.keys())
                except Exception:
                    accepted = []

                accepted_set = set(accepted)

                filtered = {k: v for k, v in candidate.items() if k in accepted_set and v is not None}

                dent_debug = {
                    "DentDamage_signature": _safe_signature(DentDamage),
                    "accepted_params": sorted(accepted),
                    "filtered_kwargs_used": filtered,
                    "dropped_candidate_keys": sorted([k for k in candidate.keys() if k not in accepted_set]),
                }

                dent_obj = DentDamage(**filtered)  # type: ignore
                res = assess_dent(dent_obj)  # type: ignore
                dent_result = res if isinstance(res, dict) else {"result": res}

            except Exception as e:
                dent_result = {"status": "error", "error": f"Could not construct/run DentDamage: {e}"}
        else:
            if structured.get("damage_type") != "DENT":
                dent_result = {"status": "skipped", "reason": "damage_type is not DENT"}
            elif not HAS_DAMAGE_MODELS:
                dent_result = {"status": "skipped", "reason": "damage_models module not available"}

        # -------------------------
        # Rules engine (resolver)
        # -------------------------
        rules_rows: Any = []
        rules_debug: Dict[str, Any] = {"selected": None, "signature": None, "module_exports": None}

        if HAS_RULES_ENGINE and RULES_DB.exists() and rules_engine is not None:
            try:
                rules_debug["module_exports"] = [n for n in dir(rules_engine) if not n.startswith("_")]

                # 1) Try common top-level function names
                fn = None
                for name in ["evaluate_rules", "run_rules", "evaluate", "match_rules", "assess", "assess_rules"]:
                    if hasattr(rules_engine, name) and callable(getattr(rules_engine, name)):
                        fn = getattr(rules_engine, name)
                        rules_debug["selected"] = f"function:{name}"
                        rules_debug["signature"] = _safe_signature(fn)
                        break

                if fn is not None:
                    try:
                        rules_rows = fn(str(RULES_DB), structured)  # type: ignore
                    except TypeError:
                        # keyword options
                        try:
                            rules_rows = fn(db_path=str(RULES_DB), structured=structured)  # type: ignore
                        except TypeError:
                            rules_rows = fn(structured, db_path=str(RULES_DB))  # type: ignore

                # 2) Try common class patterns
                if rules_rows == []:
                    for cls_name in ["RulesEngine", "RuleEngine", "Engine"]:
                        if hasattr(rules_engine, cls_name):
                            CLS = getattr(rules_engine, cls_name)
                            if callable(CLS):
                                eng = None
                                try:
                                    eng = CLS(str(RULES_DB))
                                except TypeError:
                                    try:
                                        eng = CLS(db_path=str(RULES_DB))
                                    except TypeError:
                                        eng = CLS()

                                for m in ["evaluate", "run", "run_rules", "evaluate_rules", "match"]:
                                    if hasattr(eng, m) and callable(getattr(eng, m)):
                                        meth = getattr(eng, m)
                                        rules_debug["selected"] = f"class:{cls_name}.{m}"
                                        rules_debug["signature"] = _safe_signature(meth)
                                        try:
                                            rules_rows = meth(structured)
                                        except TypeError:
                                            rules_rows = meth(str(RULES_DB), structured)
                                        break
                            if rules_rows != []:
                                break

                if rules_rows == []:
                    rules_rows = [{"error": "rules_engine has no compatible rules function (evaluate_rules/run_rules/evaluate)"}]

            except Exception as e:
                rules_rows = [{"error": str(e)}]
        else:
            if not HAS_RULES_ENGINE:
                rules_rows = [{"status": "skipped", "reason": "rules_engine not available"}]
            elif not RULES_DB.exists():
                rules_rows = [{"status": "skipped", "reason": "rules.db not found in deployment"}]

        # -------------------------
        # SRM Search (fallback queries)
        # -------------------------
        srm_hits: Any = []
        srm_debug: Dict[str, Any] = {"selected": None, "signature": None, "queries_tried": [], "query_used": None, "module_exports": None}

        if HAS_SRM_SEARCH and SRM_DB.exists() and srm_search is not None:
            try:
                srm_debug["module_exports"] = [n for n in dir(srm_search) if not n.startswith("_")]

                # Resolve a search function name
                search_fn = None
                for name in ["search", "srm_search", "search_srm", "search_srm_db"]:
                    if hasattr(srm_search, name) and callable(getattr(srm_search, name)):
                        search_fn = getattr(srm_search, name)
                        srm_debug["selected"] = name
                        srm_debug["signature"] = _safe_signature(search_fn)
                        break

                if search_fn is None:
                    srm_hits = [{"error": "srm_search module has no search/srm_search-like function"}]
                else:
                    # Your previous structured query + simpler fallbacks
                    q_bits = []
                    if structured.get("aircraft_family"):
                        q_bits.append(str(structured["aircraft_family"]))
                    if structured.get("structure"):
                        q_bits.append(str(structured["structure"]))
                    if structured.get("structure_zone"):
                        q_bits.append(str(structured["structure_zone"]))
                    if structured.get("damage_type"):
                        q_bits.append(str(structured["damage_type"]))

                    # Bias toward what the excerpt actually contains
                    q_bits.append("allowable damage")
                    if structured.get("damage_type") == "DENT":
                        q_bits.append("dent")
                    q_bits.append("table 102")

                    primary_query = " ".join(q_bits).strip()

                    fallback_queries = [
                        primary_query,
                        "allowable damage dent table 102",
                        "fuselage dent allowable damage",
                        "table 102 dent",
                        "dent allowable",
                        "allowable damage",
                    ]
                    srm_debug["queries_tried"] = fallback_queries

                    used = None
                    for q in fallback_queries:
                        try:
                            hits = search_fn(
                                str(SRM_DB),
                                query=q,
                                aircraft_family=structured.get("aircraft_family"),
                                limit=8,
                            )  # type: ignore
                        except TypeError:
                            # positional fallback (db, query, aircraft_family, limit)
                            hits = search_fn(str(SRM_DB), q, structured.get("aircraft_family"), 8)  # type: ignore

                        if hits:
                            srm_hits = hits
                            used = q
                            break

                    srm_debug["query_used"] = used

            except Exception as e:
                srm_hits = [{"error": str(e)}]
        else:
            if not HAS_SRM_SEARCH:
                srm_hits = [{"status": "skipped", "reason": "srm_search module not available"}]
            elif not SRM_DB.exists():
                srm_hits = [{"status": "skipped", "reason": "srm_index.db not found in deployment"}]

        # -------------------------
        # Render results
        # -------------------------
        st.markdown("### Dent model output")
        if build_plain_text_summary is not None and isinstance(dent_result, dict):
            try:
                summary = build_plain_text_summary(dent_result)  # type: ignore
                st.code(summary, language="text")
            except Exception:
                st.json(dent_result)
        else:
            st.json(dent_result)

        with st.expander("Dent model debug", expanded=False):
            st.json(dent_debug)

        st.markdown("### Rules matches")
        st.json(rules_rows)

        with st.expander("Rules engine debug", expanded=False):
            st.json(rules_debug)

        st.markdown("### SRM search hits (prototype)")
        if isinstance(srm_hits, list) and srm_hits and isinstance(srm_hits[0], dict) and "error" not in srm_hits[0]:
            for hit in srm_hits[:8]:
                title = hit.get("title") or hit.get("file_name") or "SRM hit"
                meta = []
                if hit.get("revision"):
                    meta.append(f"Rev: {hit['revision']}")
                if hit.get("aircraft_family"):
                    meta.append(f"Aircraft: {hit['aircraft_family']}")
                if hit.get("file_name"):
                    meta.append(f"File: {hit['file_name']}")
                if hit.get("page_no"):
                    meta.append(f"Page: {hit['page_no']}")
                st.markdown(f"**{title}**" + (f" ({' • '.join(meta)})" if meta else ""))

                snippet = hit.get("snippet") or hit.get("text") or ""
                st.code(str(snippet)[:1200], language="text")
        else:
            st.json(srm_hits)

        with st.expander("SRM search debug", expanded=False):
            st.json(srm_debug)

        # -------------------------
        # Optional logging
        # -------------------------
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
            SELECT id, created_utc, aircraft_family, structure, structure_zone, side, sta, stringer,
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
                        "damage_type": r[8],
                        "dia_mm": r[9],
                        "depth_mm": r[10],
                        "crack": (None if r[11] is None else ("Yes" if r[11] == 1 else "No")),
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
