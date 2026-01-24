# dent_checker_app.py
# Streamlit app: SRM Damage Assessment (Prototype)
#
# Updated:
# - Renamed aircraft "family" -> aircraft "type" (UI + structured dict + ctx)
# - SRM search now uses staged fallbacks:
#     1) full query + aircraft filter
#     2) full query + no aircraft filter (handles family mismatch in DB)
#     3) core query + aircraft filter
#     4) core query + no aircraft filter
# - Final statement now refuses to generate an assessment if no SRM reference exists for the selected aircraft type.
# - Logging + assessments schema supports aircraft_type and frame.
# - Keeps your existing UI layout.

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
from typing import Any, Dict, List, Optional

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
    # Common patterns you used
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
    ]
    for label, pat in struct_map:
        if re.search(pat, raw, flags=re.IGNORECASE):
            out["structure"] = label

    # Zone / sub-area
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

    # Stringer formats: S-10L, S10L, Stringer 10L
    m = re.search(r"\bS[-\s]?(\d{1,3})([LR])\b", raw, flags=re.IGNORECASE)
    if m:
        out["stringer"] = f"{int(m.group(1))}{m.group(2).upper()}"
    else:
        m2 = re.search(r"\bSTRINGER\s*(\d{1,3})([LR])\b", raw, flags=re.IGNORECASE)
        if m2:
            out["stringer"] = f"{int(m2.group(1))}{m2.group(2).upper()}"

    # Frame formats: FR 12, Frame 12
    mf = re.search(r"\b(?:FR|FRAME)\s*[-]?\s*(\d{1,4})\b", raw, flags=re.IGNORECASE)
    if mf:
        try:
            out["frame"] = int(mf.group(1))
        except Exception:
            pass

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


def init_assessments_db(db_path: Path) -> None:
    """
    Create assessments table if it doesn't exist.
    NOTE: If you already have an older table (without 'frame' or aircraft_type),
          this function won't alter it. In that case, delete assessments.db or migrate.
    """
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


def _pick_best_table102_hit(hits: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not hits:
        return None
    for h in hits:
        sn = str(h.get("snippet") or h.get("text") or "")
        if re.search(r"\bTable\s*102\b", sn, flags=re.IGNORECASE):
            return h
    return hits[0]


def _compute_wy_ratio(dia_mm: Optional[float], depth_mm: Optional[float]) -> Optional[float]:
    if dia_mm is None or depth_mm is None:
        return None
    if depth_mm <= 0:
        return None
    return dia_mm / depth_mm


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
        # SRM Search (first) with staged fallbacks
        # ----------------
        srm_hits: List[Dict[str, Any]] = []
        srm_debug: Dict[str, Any] = {
            "selected": None,
            "signature": None,
            "aircraft_type_filter": None,
            "queries_tried": [],
            "query_used": None,
            "stage": None,
        }

        def _mk_query(structured_: Dict[str, Any], mode: str) -> str:
            bits: List[str] = []
            if structured_.get("aircraft_type"):
                bits.append(str(structured_["aircraft_type"]))

            if mode == "full":
                if structured_.get("structure"):
                    bits.append(str(structured_["structure"]))
                if structured_.get("structure_zone"):
                    bits.append(str(structured_["structure_zone"]))
                if structured_.get("damage_type"):
                    bits.append(str(structured_["damage_type"]))

            bits.append("allowable damage 1")
            bits.append("fuselage skin")
            bits.append("dent")
            bits.append("table 102")
            return " ".join(bits).strip()

        def _run_search(conn: sqlite3.Connection, query: str, aircraft_filter: Optional[str]) -> List[Any]:
            fn = getattr(srm_search, "search_srm", None)
            if fn is None:
                raise RuntimeError("srm_search.search_srm not found")
            return fn(conn, query=query, aircraft_family=aircraft_filter, limit=8)  # type: ignore

        hits_any: List[Any] = []
        if HAS_SRM_SEARCH and SRM_DB.exists():
            try:
                con = sqlite3.connect(str(SRM_DB))
                try:
                    fn = getattr(srm_search, "search_srm", None)
                    if fn is None:
                        raise RuntimeError("srm_search.search_srm not found")

                    srm_debug["selected"] = "search_srm"
                    srm_debug["signature"] = str(inspect.signature(fn))

                    a_type = structured.get("aircraft_type")
                    a_type = _normalize_aircraft_type(a_type) if a_type else None
                    srm_debug["aircraft_type_filter"] = a_type

                    q1 = _mk_query(structured, mode="full")
                    srm_debug["queries_tried"].append({"stage": "1_full_with_type", "query": q1, "aircraft": a_type})
                    hits_any = _run_search(con, q1, aircraft_filter=a_type)
                    if hits_any:
                        srm_debug["stage"] = "1_full_with_type"
                        srm_debug["query_used"] = q1
                    else:
                        srm_debug["queries_tried"].append({"stage": "2_full_no_type", "query": q1, "aircraft": None})
                        hits_any = _run_search(con, q1, aircraft_filter=None)
                        if hits_any:
                            srm_debug["stage"] = "2_full_no_type"
                            srm_debug["query_used"] = q1
                        else:
                            q2 = _mk_query(structured, mode="core")
                            srm_debug["queries_tried"].append({"stage": "3_core_with_type", "query": q2, "aircraft": a_type})
                            hits_any = _run_search(con, q2, aircraft_filter=a_type)
                            if hits_any:
                                srm_debug["stage"] = "3_core_with_type"
                                srm_debug["query_used"] = q2
                            else:
                                srm_debug["queries_tried"].append({"stage": "4_core_no_type", "query": q2, "aircraft": None})
                                hits_any = _run_search(con, q2, aircraft_filter=None)
                                srm_debug["stage"] = "4_core_no_type"
                                srm_debug["query_used"] = q2
                finally:
                    con.close()

                tmp: List[Dict[str, Any]] = []
                for h in (hits_any or []):
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

        # Determine best SRM hit
        best_hit = None
        if isinstance(srm_hits, list) and srm_hits and isinstance(srm_hits[0], dict) and "error" not in srm_hits[0]:
            best_hit = _pick_best_table102_hit(srm_hits)

        if best_hit:
            st.markdown("### SRM Reference (top hit)")
            doc = best_hit.get("doc_title") or best_hit.get("title") or best_hit.get("file_name") or "SRM"
            rev = best_hit.get("revision") or "UNKNOWN"
            file_name = best_hit.get("file_name") or ""
            printed = best_hit.get("printed_page")
            pdf_page = best_hit.get("pdf_page") or best_hit.get("page")
            page_str = f"Printed page {printed}" if printed else f"PDF page {pdf_page}"
            st.write(f"**{doc}** • {page_str} • File {file_name} (Rev {rev})")
            st.code(str(best_hit.get("snippet") or "")[:600], language="text")

        # ----------------
        # Dent model
        # ----------------
        dent_result: Dict[str, Any] = {"status": "not_run"}
        dent_debug: Dict[str, Any] = {}

        crack_present = structured.get("has_crack")
        crack_reason = "used parsed crack flag"
        crack_used = bool(crack_present) if crack_present is not None else False
        if crack_present is None:
            crack_used = False
            crack_reason = "defaulted False because crack status was Unknown"

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
                    "crack_present_used": filtered.get("crack_present"),
                    "crack_present_reason": crack_reason,
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
                        "measurements": {"dent": {"diameter_mm": structured.get("dent_diameter_mm"), "depth_mm": structured.get("dent_depth_mm")}},
                        "flags": {"has_crack": structured.get("has_crack")},
                        "_flat": dict(structured),
                    }
                    rules_debug["ctx_sent"] = ctx
                    # rules_engine expects aircraft_family argument name, but value is aircraft type (B737 etc)
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
        # SRM-based "Final statement" (must have SRM hit, and must match aircraft type)
        # ----------------
        st.markdown("### Final statement (SRM-based)")
        final_lines: List[str] = []

        selected_type = structured.get("aircraft_type") or "UNKNOWN"

        if not best_hit:
            final_lines.append("Reference: **(no SRM hit available in library)**")
            final_lines.append(
                f"Decision: **NO ASSESSMENT GENERATED** (no SRM reference available for aircraft type **{selected_type}** in the current library/index)."
            )
            st.write("\n".join(final_lines))
        else:
            # If hit aircraft differs from selected, do not assess
            hit_type = best_hit.get("aircraft_family") or best_hit.get("aircraft_type") or None
            hit_type_norm = _normalize_aircraft_type(hit_type) if hit_type else None
            selected_type_norm = _normalize_aircraft_type(selected_type) if selected_type else None

            if hit_type_norm and selected_type_norm and hit_type_norm != selected_type_norm:
                doc = best_hit.get("doc_title") or best_hit.get("file_name") or "SRM"
                final_lines.append(f"Reference: **{doc}** (aircraft={hit_type_norm})")
                final_lines.append(
                    f"Decision: **NO ASSESSMENT GENERATED** (top SRM reference aircraft `{hit_type_norm}` does not match selected aircraft type `{selected_type_norm}`)."
                )
                st.write("\n".join(final_lines))
            else:
                # Build reference line
                doc = best_hit.get("doc_title") or best_hit.get("file_name") or "SRM"
                rev = best_hit.get("revision") or "UNKNOWN"
                file_name = best_hit.get("file_name") or ""
                printed = best_hit.get("printed_page")
                pdf_page = best_hit.get("pdf_page") or best_hit.get("page")
                page_str = f"Printed page {printed}" if printed else f"PDF page {pdf_page}"

                snippet = str(best_hit.get("snippet") or "")
                mtab = re.search(r"\bTable\s*(\d+)\b", snippet, flags=re.IGNORECASE)
                table_no = mtab.group(1) if mtab else "102"

                final_lines.append(
                    f"Reference: **{doc}** (Rev {rev}) • **Table {table_no}** • **{page_str}** • File `{file_name}`"
                )

                # Dent “why” block
                dtype = structured.get("damage_type")
                dia = structured.get("dent_diameter_mm")
                dep = structured.get("dent_depth_mm")
                ratio = _compute_wy_ratio(dia, dep)

                if dtype == "DENT":
                    if structured.get("has_crack") is True:
                        final_lines.append("Decision: **OUT OF LIMITS** (crack present ⇒ not allowable / engineering review).")
                    else:
                        if dep is None:
                            final_lines.append("Decision: **ENGINEERING REVIEW** (missing dent depth).")
                        else:
                            if dep > 6.35:
                                final_lines.append("Decision: **OUT OF LIMITS** (Depth > 6.35 mm (0.25 in) ⇒ not allowable).")
                            elif dep > 3.175:
                                if ratio is None:
                                    final_lines.append("Decision: **ENGINEERING REVIEW** (cannot compute W/Y ratio).")
                                else:
                                    if ratio >= 30.0:
                                        final_lines.append("Decision: **WITHIN LIMITS** (Depth in (3.175..6.35] mm and W/Y ≥ 30).")
                                    else:
                                        final_lines.append("Decision: **OUT OF LIMITS** (Depth in (3.175..6.35] mm and W/Y < 30).")
                            else:
                                final_lines.append("Decision: **WITHIN LIMITS** (Depth ≤ 3.175 mm (0.125 in) prototype band).")

                    final_lines.append("")
                    final_lines.append("Proof / calculations:")
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
                    if structured.get("has_crack") is True:
                        final_lines.append("- Crack: **present** (override to OUT OF LIMITS)")
                    elif structured.get("has_crack") is False:
                        final_lines.append("- Crack: not reported")
                    else:
                        final_lines.append("- Crack: unknown (treated as not present for prototype dent math)")
                else:
                    final_lines.append(f"Decision: **NO ASSESSMENT GENERATED** (damage_type={dtype}; SRM dent logic applies to DENT only).")

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
        # SRM hits
        # ----------------
        st.markdown("### SRM search hits (prototype)")
        if isinstance(srm_hits, list) and srm_hits and isinstance(srm_hits[0], dict) and "error" not in srm_hits[0]:
            for hit in srm_hits[:8]:
                title = hit.get("doc_title") or hit.get("file_name") or "SRM hit"
                rev = hit.get("revision") or "UNKNOWN"
                fam = hit.get("aircraft_family") or ""
                file_name = hit.get("file_name") or ""
                printed = hit.get("printed_page")
                pdf_page = hit.get("pdf_page") or hit.get("page")

                page_str = f"Printed page {printed}" if printed else f"PDF page {pdf_page}"
                meta = f"Rev: {rev} • Aircraft: {fam} • File: {file_name} • {page_str}"
                st.markdown(f"**{title}** ({meta})")
                st.code(str(hit.get("snippet") or hit.get("text") or "")[:1200], language="text")
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
                st.error(
                    f"Failed to log assessment: {e}\n\n"
                    "If you recently added new columns (e.g., frame/aircraft_type), delete assessments.db to regenerate."
                )

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
        st.error(
            f"Could not read assessments.db: {e}\n\n"
            "If you changed the schema (e.g., added aircraft_type/frame), delete assessments.db to regenerate."
        )
else:
    st.caption("No assessments.db yet. Run an assessment and enable logging to create it.")
