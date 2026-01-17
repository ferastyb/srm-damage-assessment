# dent_checker_app.py
# Streamlit app: SRM Damage Assessment (Prototype)
#
# Keeps your “best” UI layout, and adds the robust debugging + compatibility
# shims we discussed:
# - DentDamage signature/kwargs filtering + debug
# - rules_engine compatible function detection + ctx debug
# - srm_search compatible function detection + sqlite conn handling + debug
# - SRM DB Debug (cwd + path + exists + size + sha256 prefix)
# - SRM Reference (top hit) panel
# - Assessments logging with self-migrating schema (adds missing columns)

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

damage_models_err: Optional[Exception] = None
rules_engine_err: Optional[Exception] = None
srm_search_err: Optional[Exception] = None

DentDamage = None  # type: ignore
assess_dent = None  # type: ignore
build_plain_text_summary = None  # type: ignore
rules_engine = None  # type: ignore
srm_search = None  # type: ignore

try:
    # expected exports in your project:
    # - DentDamage (dataclass)
    # - assess_dent(dent: DentDamage, ...) -> result
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


def _sig_str(obj: Any) -> Optional[str]:
    try:
        return str(inspect.signature(obj))
    except Exception:
        return None


def _module_exports(mod: Any) -> List[str]:
    try:
        names = [n for n in dir(mod) if not n.startswith("_")]
        return names
    except Exception:
        return []


def _as_dict(obj: Any) -> Any:
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, list):
        return [_as_dict(x) for x in obj]
    if is_dataclass(obj):
        return asdict(obj)
    if hasattr(obj, "__dict__"):
        try:
            return dict(obj.__dict__)
        except Exception:
            pass
    return str(obj)


def parse_damage_description(desc: str) -> Dict[str, Any]:
    """
    Lightweight parser for descriptions like:
    “B737 fuselage LH STA 123 S-10L skin dent 25mm dia 3mm depth no crack”
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
        "damage_type": None,
        "dent_diameter_mm": None,
        "dent_depth_mm": None,
        "has_crack": None,
        "notes": None,
    }

    m = re.search(r"\b(B7\d{2}|A3\d{2}|A32\d{2}|E1\d{2}|E17\d)\b", raw, flags=re.IGNORECASE)
    if m:
        out["aircraft_family"] = _normalize_aircraft_family(m.group(1))

    if re.search(r"\bfuselage\b", raw, flags=re.IGNORECASE):
        out["structure"] = "FUSELAGE"
    elif re.search(r"\bwing\b", raw, flags=re.IGNORECASE):
        out["structure"] = "WING"
    elif re.search(r"\bempennage\b|\btail\b", raw, flags=re.IGNORECASE):
        out["structure"] = "EMPENNAGE"

    if re.search(r"\bskin\b", raw, flags=re.IGNORECASE):
        out["structure_zone"] = "SKIN"
    elif re.search(r"\bstringer\b", raw, flags=re.IGNORECASE):
        out["structure_zone"] = "STRINGER"
    elif re.search(r"\bframe\b", raw, flags=re.IGNORECASE):
        out["structure_zone"] = "FRAME"

    out["side"] = _parse_side(raw)

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

    m = re.search(r"\bS[-\s]?(\d{1,3})([LR])\b", raw, flags=re.IGNORECASE)
    if m:
        out["stringer"] = f"{int(m.group(1))}{m.group(2).upper()}"
    else:
        m2 = re.search(r"\bSTRINGER\s*(\d{1,3})([LR])\b", raw, flags=re.IGNORECASE)
        if m2:
            out["stringer"] = f"{int(m2.group(1))}{m2.group(2).upper()}"

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
    elif re.search(r"\bvisible\s+crack(s)?\b", raw, flags=re.IGNORECASE):
        out["has_crack"] = True
    elif re.search(r"\bcrack(s)?\b", raw, flags=re.IGNORECASE):
        out["has_crack"] = True

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


def build_rules_ctx(structured: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build ctx for rules_engine.assess_damage.

    IMPORTANT: duplicate crack keys for compatibility across different rule styles.
    """
    has_crack = structured.get("has_crack")  # True/False/None

    ctx: Dict[str, Any] = {
        "aircraft_family": structured.get("aircraft_family"),
        "raw": structured.get("raw"),
        "damage": {
            "type": structured.get("damage_type"),
            "structure": structured.get("structure"),
            "has_crack": has_crack,
            "crack_present": has_crack,
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
            "has_crack": has_crack,
            "crack_present": has_crack,
        },
        "_flat": dict(structured),
    }
    return ctx


# -----------------------------
# Assessments DB (self-migrating)
# -----------------------------
def _ensure_table_and_columns(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS assessments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              created_utc TEXT NOT NULL
            );
            """
        )
        con.commit()

        # Current desired columns (append-only, safe ALTER TABLE)
        desired = {
            "aircraft_family": "TEXT",
            "damage_type": "TEXT",
            "dent_diameter_mm": "REAL",
            "dent_depth_mm": "REAL",
            "has_crack": "INTEGER",
            "input_text": "TEXT",
            "structured_json": "TEXT",
            "rules_ctx_json": "TEXT",
            "rules_result_json": "TEXT",
            "dent_result_json": "TEXT",
            "srm_hits_json": "TEXT",
            "srm_top_ref": "TEXT",
            "srm_top_page": "INTEGER",
            "srm_top_file": "TEXT",
        }

        existing = set()
        for row in con.execute("PRAGMA table_info(assessments)").fetchall():
            # row: cid, name, type, notnull, dflt_value, pk
            existing.add(str(row[1]))

        for col, typ in desired.items():
            if col not in existing:
                con.execute(f"ALTER TABLE assessments ADD COLUMN {col} {typ};")
        con.commit()
    finally:
        con.close()


def log_assessment(
    db_path: Path,
    structured: Dict[str, Any],
    rules_ctx: Any,
    rules_result: Any,
    srm_hits: Any,
    dent_result: Any,
    srm_top: Optional[Dict[str, Any]],
) -> None:
    _ensure_table_and_columns(db_path)

    con = sqlite3.connect(str(db_path))
    try:
        con.execute(
            """
            INSERT INTO assessments (
              created_utc,
              aircraft_family,
              damage_type,
              dent_diameter_mm,
              dent_depth_mm,
              has_crack,
              input_text,
              structured_json,
              rules_ctx_json,
              rules_result_json,
              dent_result_json,
              srm_hits_json,
              srm_top_ref,
              srm_top_page,
              srm_top_file
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now_iso(),
                structured.get("aircraft_family"),
                structured.get("damage_type"),
                structured.get("dent_diameter_mm"),
                structured.get("dent_depth_mm"),
                None
                if structured.get("has_crack") is None
                else (1 if structured.get("has_crack") else 0),
                structured.get("raw"),
                safe_json(structured),
                safe_json(rules_ctx),
                safe_json(rules_result),
                safe_json(dent_result),
                safe_json(srm_hits),
                (None if not srm_top else (srm_top.get("doc_title") or srm_top.get("title") or srm_top.get("file_name"))),
                (None if not srm_top else (srm_top.get("page") or srm_top.get("page_no"))),
                (None if not srm_top else (srm_top.get("file_name"))),
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

    structured: Dict[str, Any] = st.session_state.structured

    st.subheader("2) Structured fields")
    f1, f2, f3, f4 = st.columns(4)

    with f1:
        aircraft_family = st.text_input("Aircraft family", value=structured.get("aircraft_family") or "")
        structure = st.text_input("Structure", value=structured.get("structure") or "")
    with f2:
        structure_zone = st.text_input("Zone", value=structured.get("structure_zone") or "")
        side = st.selectbox(
            "Side",
            ["ANY", "LH", "RH"],
            index=max(0, ["ANY", "LH", "RH"].index(structured.get("side") or "ANY")),
        )
    with f3:
        sta_val = float(structured.get("sta") or 0.0)
        wl_val = float(structured.get("wl") or 0.0)
        sta = st.number_input("STA", value=sta_val, step=1.0, format="%.1f")
        wl = st.number_input("WL", value=wl_val, step=1.0, format="%.1f")
    with f4:
        stringer = st.text_input("Stringer", value=structured.get("stringer") or "")

        dmg_options = ["DENT", "GOUGE", "CRACK", "CORROSION", "OTHER"]
        cur_dmg = (structured.get("damage_type") or "DENT").upper()
        dmg_index = dmg_options.index(cur_dmg) if cur_dmg in dmg_options else 0
        damage_type = st.selectbox("Damage type", dmg_options, index=dmg_index)

    d1, d2, d3 = st.columns(3)
    with d1:
        dent_dia = st.number_input(
            "Dent diameter (mm)",
            value=float(structured.get("dent_diameter_mm") or 0.0),
            step=0.1,
            format="%.2f",
        )
    with d2:
        dent_depth = st.number_input(
            "Dent depth (mm)",
            value=float(structured.get("dent_depth_mm") or 0.0),
            step=0.1,
            format="%.2f",
        )
    with d3:
        # Keep same UI behavior you had: Unknown/No/Yes
        crack_opt = st.selectbox("Crack present?", ["Unknown", "No", "Yes"], index=0)

    # Write back into structured dict (source of truth)
    structured["raw"] = (desc or "").strip()
    structured["aircraft_family"] = _normalize_aircraft_family(aircraft_family) if aircraft_family else None
    structured["structure"] = structure.strip().upper() if structure else None
    structured["structure_zone"] = structure_zone.strip().upper() if structure_zone else None
    structured["side"] = side
    structured["sta"] = None if float(sta) == 0.0 else float(sta)
    structured["wl"] = None if float(wl) == 0.0 else float(wl)
    structured["stringer"] = stringer.strip().upper() or None
    structured["damage_type"] = damage_type
    structured["dent_diameter_mm"] = None if float(dent_dia) == 0.0 else float(dent_dia)
    structured["dent_depth_mm"] = None if float(dent_depth) == 0.0 else float(dent_depth)

    if crack_opt == "Unknown":
        structured["has_crack"] = None
    elif crack_opt == "No":
        structured["has_crack"] = False
    else:
        structured["has_crack"] = True

    st.subheader("3) Run assessment")
    run = st.button("Run rules + SRM search + dent model", type="primary")


def _build_srm_queries(structured: Dict[str, Any]) -> List[str]:
    fam = structured.get("aircraft_family") or ""
    structure = structured.get("structure") or ""
    zone = structured.get("structure_zone") or ""
    dtype = structured.get("damage_type") or ""

    q0_bits = [str(x) for x in [fam, structure, zone, dtype] if x]
    q0_bits += ["allowable damage", "dent", "table 102"]
    q0 = " ".join(q0_bits).strip()

    return [
        q0,
        "allowable damage dent table 102",
        "fuselage dent allowable damage",
        "table 102 dent",
        "dent allowable",
        "allowable damage",
    ]


def _normalize_srm_hit(hit: Any) -> Dict[str, Any]:
    if isinstance(hit, dict):
        return hit
    if is_dataclass(hit):
        return asdict(hit)
    # last resort: parse from string-ish repr
    return {"raw": str(hit)}


def _open_sqlite_ro(path: Path) -> sqlite3.Connection:
    # Read-only mode when supported; fallback to normal connect
    try:
        uri = f"file:{path.as_posix()}?mode=ro"
        return sqlite3.connect(uri, uri=True)
    except Exception:
        return sqlite3.connect(str(path))


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

    if not run:
        st.info("Fill the structured fields if needed, then click **Run rules + SRM search + dent model**.")
    else:
        # -----------------------------
        # Run SRM search first (so we can show top ref)
        # -----------------------------
        srm_hits: List[Dict[str, Any]] = []
        srm_debug: Dict[str, Any] = {"selected": None, "signature": None, "query_used": None}

        if HAS_SRM_SEARCH and SRM_DB.exists():
            try:
                queries = _build_srm_queries(structured)

                # Choose function
                fn = None
                fn_name = None
                if hasattr(srm_search, "search_srm"):
                    fn = getattr(srm_search, "search_srm")
                    fn_name = "search_srm"
                elif hasattr(srm_search, "search"):
                    fn = getattr(srm_search, "search")
                    fn_name = "search"
                elif hasattr(srm_search, "srm_search"):
                    fn = getattr(srm_search, "srm_search")
                    fn_name = "srm_search"

                srm_debug["selected"] = fn_name
                srm_debug["signature"] = _sig_str(fn) if fn else None

                if not fn:
                    srm_hits = [{"error": "srm_search module has no search_srm/search/srm_search function"}]
                else:
                    sig = None
                    try:
                        sig = inspect.signature(fn)
                    except Exception:
                        sig = None

                    # If fn expects sqlite connection (like your current srm_search.py)
                    expects_conn = False
                    if sig is not None:
                        params = list(sig.parameters.values())
                        expects_conn = len(params) >= 1 and params[0].name in ("conn", "connection", "db")

                    hits_obj: List[Any] = []
                    used_q: Optional[str] = None

                    if expects_conn:
                        con = _open_sqlite_ro(SRM_DB)
                        try:
                            for q in queries:
                                try:
                                    tmp = fn(con, q, structured.get("aircraft_family"), 8)  # type: ignore
                                except TypeError:
                                    # Support keyword style
                                    tmp = fn(con, query=q, aircraft_family=structured.get("aircraft_family"), limit=8)  # type: ignore
                                if tmp:
                                    hits_obj = tmp
                                    used_q = q
                                    break
                        finally:
                            con.close()
                    else:
                        # fn expects db_path
                        for q in queries:
                            try:
                                tmp = fn(str(SRM_DB), query=q, aircraft_family=structured.get("aircraft_family"), limit=8)  # type: ignore
                            except TypeError:
                                tmp = fn(str(SRM_DB), q, structured.get("aircraft_family"), 8)  # type: ignore
                            if tmp:
                                hits_obj = tmp
                                used_q = q
                                break

                    srm_debug["query_used"] = used_q
                    srm_hits = [_normalize_srm_hit(h) for h in (hits_obj or [])]

            except Exception as e:
                srm_hits = [{"error": str(e)}]
        else:
            if not HAS_SRM_SEARCH:
                srm_hits = [{"status": "skipped", "reason": "srm_search module not available"}]
            elif not SRM_DB.exists():
                srm_hits = [{"status": "skipped", "reason": "srm_index.db not found in deployment"}]

        # Top SRM ref
        top_hit: Optional[Dict[str, Any]] = None
        if srm_hits and isinstance(srm_hits, list) and isinstance(srm_hits[0], dict) and "error" not in srm_hits[0]:
            top_hit = srm_hits[0]

        if top_hit:
            st.markdown("### SRM Reference (top hit)")
            doc_title = top_hit.get("doc_title") or top_hit.get("title") or "SRM"
            page = top_hit.get("page") or top_hit.get("page_no")
            file_name = top_hit.get("file_name") or ""
            rev = top_hit.get("revision") or "UNKNOWN"
            st.write(f"**{doc_title}** • Page {page} • File {file_name} (Rev {rev})")
            st.code(str(top_hit.get("snippet") or top_hit.get("text") or "")[:900], language="text")

        # -----------------------------
        # Dent model
        # -----------------------------
        dent_result: Dict[str, Any] = {"status": "not_run"}
        dent_debug: Dict[str, Any] = {}

        if HAS_DAMAGE_MODELS and structured.get("damage_type") == "DENT":
            try:
                dd_sig = _sig_str(DentDamage)
                dent_debug["DentDamage_signature"] = dd_sig

                sig = inspect.signature(DentDamage)  # type: ignore
                accepted = [p.name for p in sig.parameters.values() if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)]
                dent_debug["accepted_params"] = accepted

                # Crack default policy:
                # - If user explicitly says Yes/No, respect it.
                # - If Unknown, default False (safer than defaulting True, avoids false "crack present").
                crack_present_used: bool
                crack_reason: str
                if structured.get("has_crack") is True:
                    crack_present_used = True
                    crack_reason = "from structured.has_crack=True"
                elif structured.get("has_crack") is False:
                    crack_present_used = False
                    crack_reason = "from structured.has_crack=False"
                else:
                    crack_present_used = False
                    crack_reason = "defaulted False because crack status was Unknown"

                dent_debug["crack_present_used"] = crack_present_used
                dent_debug["crack_present_reason"] = crack_reason

                # Candidate kwargs covering old/new naming
                sta_str = None
                if structured.get("sta") is not None:
                    # DentDamage signature in your app wants Optional[str]
                    sta_str = str(int(structured["sta"])) if float(structured["sta"]).is_integer() else str(structured["sta"])

                candidate_kwargs: Dict[str, Any] = {
                    # common expected by your DentDamage signature:
                    "aircraft_type": structured.get("aircraft_family") or "UNKNOWN",
                    "structure_zone": structured.get("structure_zone") or "UNKNOWN",
                    "side": structured.get("side") or "ANY",
                    "sta": sta_str,
                    "stringer": structured.get("stringer"),
                    "dent_diameter_mm": float(structured.get("dent_diameter_mm") or 0.0),
                    "dent_depth_mm": float(structured.get("dent_depth_mm") or 0.0),
                    "crack_present": crack_present_used,
                    "notes": structured.get("notes"),
                    # extra aliases (won't be used if not in signature):
                    "aircraft_family": structured.get("aircraft_family"),
                    "zone": structured.get("structure_zone"),
                    "has_crack": structured.get("has_crack"),
                }

                filtered = {k: v for k, v in candidate_kwargs.items() if k in accepted}
                dropped = [k for k in candidate_kwargs.keys() if k not in filtered]
                dent_debug["filtered_kwargs_used"] = filtered
                dent_debug["dropped_candidate_keys"] = dropped

                # Ensure required params present (especially crack_present)
                for p in sig.parameters.values():
                    if p.name in accepted and p.default is p.empty and p.name not in filtered:
                        # best effort: supply sensible defaults
                        if p.name == "crack_present":
                            filtered[p.name] = crack_present_used
                        elif p.name in ("dent_diameter_mm", "dent_depth_mm"):
                            filtered[p.name] = 0.0
                        elif p.name in ("aircraft_type", "structure_zone", "side"):
                            filtered[p.name] = "UNKNOWN"
                        else:
                            filtered[p.name] = None

                dent = DentDamage(**filtered)  # type: ignore
                res = assess_dent(dent)  # type: ignore
                dent_result = res if isinstance(res, dict) else {"result": str(res)}
            except Exception as e:
                dent_result = {"status": "error", "error": f"Could not construct/run DentDamage: {e}"}
        else:
            if structured.get("damage_type") != "DENT":
                dent_result = {"status": "skipped", "reason": "damage_type is not DENT"}
            elif not HAS_DAMAGE_MODELS:
                dent_result = {"status": "skipped", "reason": "damage_models module not available"}

        # -----------------------------
        # Rules engine
        # -----------------------------
        rules_ctx = build_rules_ctx(structured)
        rules_result: Any = []
        rules_debug: Dict[str, Any] = {"selected": None, "signature": None, "module_exports": []}

        if HAS_RULES_ENGINE and RULES_DB.exists():
            try:
                rules_debug["module_exports"] = _module_exports(rules_engine)

                fn = None
                fn_name = None
                for name in ("assess_damage", "evaluate_rules", "run_rules", "evaluate"):
                    if hasattr(rules_engine, name):
                        fn = getattr(rules_engine, name)
                        fn_name = name
                        break

                rules_debug["selected"] = fn_name
                rules_debug["signature"] = _sig_str(fn) if fn else None
                rules_debug["ctx_sent"] = rules_ctx

                if not fn:
                    rules_result = [{"error": "rules_engine has no compatible rules function (assess_damage/evaluate_rules/run_rules/evaluate)"}]
                else:
                    # Prefer assess_damage signature: (db_path, aircraft_family, ctx, revision=None)
                    if fn_name == "assess_damage":
                        aircraft = structured.get("aircraft_family") or "UNKNOWN"
                        out = fn(str(RULES_DB), aircraft, rules_ctx, None)  # type: ignore
                        rules_result = _as_dict(out)
                    else:
                        # Fallback: pass structured or ctx based on typical usage
                        try:
                            out = fn(str(RULES_DB), rules_ctx)  # type: ignore
                        except TypeError:
                            out = fn(str(RULES_DB), structured)  # type: ignore
                        rules_result = _as_dict(out)
            except Exception as e:
                rules_result = [{"error": str(e)}]
        else:
            if not HAS_RULES_ENGINE:
                rules_result = [{"status": "skipped", "reason": "rules_engine not available"}]
            elif not RULES_DB.exists():
                rules_result = [{"status": "skipped", "reason": "rules.db not found in deployment"}]

        # -----------------------------
        # Render results
        # -----------------------------
        if structured.get("has_crack") is True:
            st.warning("Crack present → engineering review required regardless of dent dimensional limits.")

        st.markdown("### Dent model output")
        if build_plain_text_summary and isinstance(dent_result, dict) and "result" not in dent_result:
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
        st.json(rules_result)

        with st.expander("Rules engine debug", expanded=False):
            st.json(rules_debug)

        st.markdown("### SRM search hits (prototype)")
        if srm_hits and isinstance(srm_hits[0], dict) and "error" not in srm_hits[0]:
            for hit in srm_hits[:8]:
                title = hit.get("doc_title") or hit.get("title") or hit.get("file_name") or "SRM hit"
                rev = hit.get("revision") or "UNKNOWN"
                fam = hit.get("aircraft_family") or structured.get("aircraft_family") or ""
                fnm = hit.get("file_name") or ""
                page = hit.get("page") or hit.get("page_no") or ""
                meta = f"Rev: {rev} • Aircraft: {fam} • File: {fnm} • Page: {page}"
                st.markdown(f"**{title}** ({meta})")
                snippet = hit.get("snippet") or hit.get("text") or ""
                st.code(str(snippet)[:1200], language="text")
        else:
            st.json(srm_hits)

        with st.expander("SRM search debug", expanded=False):
            st.json(srm_debug)

        # -----------------------------
        # Logging
        # -----------------------------
        st.markdown("### Logging")
        log_it = st.checkbox("Log this assessment to SQLite (assessments.db)", value=True)
        if log_it:
            try:
                log_assessment(
                    ASSESSMENTS_DB,
                    structured=structured,
                    rules_ctx=rules_ctx,
                    rules_result=rules_result,
                    srm_hits=srm_hits,
                    dent_result=dent_result,
                    srm_top=top_hit,
                )
                st.success("Logged to assessments.db")
            except Exception as e:
                st.error(f"Failed to log assessment: {e}")


# -----------------------------
# Assessment history
# -----------------------------
st.divider()
st.subheader("Assessment history (SQLite)")

if ASSESSMENTS_DB.exists():
    try:
        _ensure_table_and_columns(ASSESSMENTS_DB)
        con = sqlite3.connect(str(ASSESSMENTS_DB))
        try:
            rows = con.execute(
                """
                SELECT
                  id,
                  created_utc,
                  aircraft_family,
                  damage_type,
                  dent_diameter_mm,
                  dent_depth_mm,
                  has_crack,
                  srm_top_ref,
                  srm_top_page
                FROM assessments
                ORDER BY id DESC
                LIMIT 25
                """
            ).fetchall()
        finally:
            con.close()

        if rows:
            st.write(f"Showing last {len(rows)} logs from assessments.db")
            st.dataframe(
                [
                    {
                        "id": r[0],
                        "created_utc": r[1],
                        "aircraft": r[2],
                        "damage_type": r[3],
                        "dia_mm": r[4],
                        "depth_mm": r[5],
                        "crack": (None if r[6] is None else ("Yes" if r[6] == 1 else "No")),
                        "top_srm": r[7],
                        "top_page": r[8],
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
