# dent_checker_app.py
# Streamlit app: SRM Damage Assessment (Prototype)
#
# UI: KEEP EXACTLY this layout/flow (your “best by far” version)
# Changes implemented (debug + resiliency, from the last update):
# - ✅ Robust DentDamage construction (no hard dependency on aircraft_family kwarg)
# - ✅ Robust rules_engine calling (tries multiple function names + signatures)
# - ✅ Robust SRM search: tries srm_search module if present, else falls back to DIRECT SQLite FTS
# - ✅ SRM DB Debug panel: cwd + path + exists + size + sha256 prefix (unchanged)
# - ✅ Optional logging: AUTO-MIGRATES assessments table (adds missing columns instead of failing)
# - ✅ “Deglue” display for SRM hits (so Streamlit output looks like your local)
#
# Repo layout assumptions (root):
# - dent_checker_app.py  (this file)
# - damage_models.py     (optional)
# - rules_engine.py      (optional)
# - srm_search.py        (optional)
# - rules.db             (optional)
# - srm_index.db         (optional; required for SRM hits on Streamlit)
#
# Tip:
# Streamlit Cloud can only see files committed to GitHub. If srm_index.db is present in debug,
# SRM search can work even if PDFs are NOT in the repo.

from __future__ import annotations

import hashlib
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
    # expected exports in your project:
    # - DentDamage (dataclass)
    # - assess_dent(dent: DentDamage, ...) -> dict or result
    # - build_plain_text_summary(result, ...) -> str (optional)
    from damage_models import DentDamage as _DentDamage, assess_dent as _assess_dent, build_plain_text_summary as _build_plain_text_summary  # type: ignore

    DentDamage = _DentDamage
    assess_dent = _assess_dent
    build_plain_text_summary = _build_plain_text_summary
    HAS_DAMAGE_MODELS = True
except Exception as e:
    damage_models_err = e

try:
    # expected exports (typical):
    # - evaluate_rules(db_path, structured_damage_dict, ...) -> list[dict]
    # or other function names (run_rules, evaluate, etc)
    import rules_engine  # type: ignore

    HAS_RULES_ENGINE = True
except Exception as e:
    rules_engine_err = e

try:
    # expected exports:
    # - search(db_path, query, aircraft_family=None, limit=5) -> list[dict]
    # or srm_search(...)
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
_DASHES = {
    "\u2010": "-",
    "\u2011": "-",
    "\u2012": "-",
    "\u2013": "-",
    "\u2014": "-",
}
_UNIT_RE = r"(mm|cm|m|in\.?|inch|inches|ft|psi|lb|lbs|cycles|deg|°c|°f)"


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


def deglue_text(s: str) -> str:
    """
    Lightweight “word segmentation” for SRM extracted text so Streamlit display matches local:
    - inserts spaces in CamelCase + letter/digit glue
    - fixes common glued words: Greaterthan -> Greater than, Referto -> Refer to
    - inserts space before units: 3.175mm -> 3.175 mm
    Conservative by design.
    """
    if not s:
        return ""
    for k, v in _DASHES.items():
        s = s.replace(k, v)
    s = s.replace("\x00", " ")

    # punctuation spacing
    s = re.sub(r"([.,;:])(?=\w)", r"\1 ", s)

    # camelcase
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)

    # letters<->digits
    s = re.sub(r"([A-Za-z])(\d)", r"\1 \2", s)
    s = re.sub(r"(\d)([A-Za-z])", r"\1 \2", s)

    # common glued words
    s = re.sub(r"\b(Greater|Less)than(?=\d|\b)", r"\1 than", s, flags=re.IGNORECASE)
    s = re.sub(r"\bmorethan(?=\d|\b)", "more than", s, flags=re.IGNORECASE)
    s = re.sub(r"\bReferto(?=\d)", "Refer to ", s, flags=re.IGNORECASE)
    s = re.sub(r"\bwithin(?=\d)", "within ", s, flags=re.IGNORECASE)
    s = re.sub(r"\bevery(?=\d)", "every ", s, flags=re.IGNORECASE)
    s = re.sub(r"\bbefore(?=\d)", "before ", s, flags=re.IGNORECASE)
    s = re.sub(r"(\d)\s*and(\d)", r"\1 and \2", s, flags=re.IGNORECASE)
    s = re.sub(r"(\d)\s*to(\d)", r"\1 to \2", s, flags=re.IGNORECASE)

    # units
    s = re.sub(rf"(\d)\s*{_UNIT_RE}\b", r"\1 \2", s, flags=re.IGNORECASE)

    # collapse whitespace
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = "\n".join(" ".join(line.split()) for line in s.splitlines())
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return s


def _normalize_aircraft_family(text: str) -> str:
    t = (text or "").strip().upper().replace(" ", "")
    # Accept common variants
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


def parse_damage_description(desc: str) -> Dict[str, Any]:
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

    if re.search(r"\bno\s+(visible\s+)?crack(s)?\b", raw, flags=re.IGNORECASE):
        out["has_crack"] = False
    elif re.search(r"\bcrack(s)?\b", raw, flags=re.IGNORECASE):
        out["has_crack"] = True

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
# Logging: auto-migrate schema
# -----------------------------
ASSESSMENT_COLS: List[tuple[str, str]] = [
    ("created_utc", "TEXT"),
    ("aircraft_family", "TEXT"),
    ("structure", "TEXT"),
    ("structure_zone", "TEXT"),
    ("side", "TEXT"),
    ("sta", "REAL"),
    ("wl", "REAL"),
    ("stringer", "TEXT"),
    ("damage_type", "TEXT"),
    ("dent_diameter_mm", "REAL"),
    ("dent_depth_mm", "REAL"),
    ("has_crack", "INTEGER"),
    ("input_text", "TEXT"),
    ("structured_json", "TEXT"),
    ("rules_json", "TEXT"),
    ("srm_hits_json", "TEXT"),
    ("result_json", "TEXT"),
]


def ensure_assessments_db(db_path: Path) -> None:
    """
    Creates table if missing, and adds missing columns if schema evolved.
    Prevents errors like:
      - "table assessments has no column named has_crack"
      - "no column named aircraft_type"
    """
    con = sqlite3.connect(str(db_path))
    try:
        con.execute("CREATE TABLE IF NOT EXISTS assessments (id INTEGER PRIMARY KEY AUTOINCREMENT)")
        existing = {r[1] for r in con.execute("PRAGMA table_info(assessments)").fetchall()}
        for name, typ in ASSESSMENT_COLS:
            if name not in existing:
                con.execute(f"ALTER TABLE assessments ADD COLUMN {name} {typ}")
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
    ensure_assessments_db(db_path)

    con = sqlite3.connect(str(db_path))
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
            "damage_type": structured.get("damage_type"),
            "dent_diameter_mm": structured.get("dent_diameter_mm"),
            "dent_depth_mm": structured.get("dent_depth_mm"),
            "has_crack": None if structured.get("has_crack") is None else (1 if structured.get("has_crack") else 0),
            "input_text": structured.get("raw"),
            "structured_json": safe_json(structured),
            "rules_json": safe_json(rules_rows),
            "srm_hits_json": safe_json(srm_hits),
            "result_json": safe_json(result),
        }

        cols = ", ".join(payload.keys())
        qs = ", ".join(["?"] * len(payload))
        con.execute(f"INSERT INTO assessments ({cols}) VALUES ({qs})", tuple(payload.values()))
        con.commit()
    finally:
        con.close()


        # --------------
        # Rules engine
        # --------------
        rules_rows: Any = []
        rules_debug: Dict[str, Any] = {"found_callables": []}

        if HAS_RULES_ENGINE and RULES_DB.exists() and rules_engine is not None:
            try:
                rules_debug["module_dir"] = [n for n in dir(rules_engine) if not n.startswith("_")]

                # 1) Try common function names
                for name in ["evaluate_rules", "run_rules", "evaluate", "match_rules", "assess", "assess_rules"]:
                    if hasattr(rules_engine, name) and callable(getattr(rules_engine, name)):
                        fn = getattr(rules_engine, name)
                        rules_debug["selected"] = f"function:{name}"
                        rules_debug["signature"] = str(inspect.signature(fn))
                        try:
                            rules_rows = fn(str(RULES_DB), structured)  # type: ignore
                        except TypeError:
                            # try keyword forms
                            try:
                                rules_rows = fn(db_path=str(RULES_DB), structured=structured)  # type: ignore
                            except TypeError:
                                rules_rows = fn(structured, db_path=str(RULES_DB))  # type: ignore
                        break

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
                                # try methods
                                for m in ["evaluate", "run", "run_rules", "evaluate_rules", "match"]:
                                    if hasattr(eng, m) and callable(getattr(eng, m)):
                                        meth = getattr(eng, m)
                                        rules_debug["selected"] = f"class:{cls_name}.{m}"
                                        rules_debug["signature"] = str(inspect.signature(meth))
                                        try:
                                            rules_rows = meth(structured)
                                        except TypeError:
                                            rules_rows = meth(str(RULES_DB), structured)
                                        break
                            if rules_rows != []:
                                break

                if rules_rows == []:
                    rules_rows = [{"error": "Could not find a callable rules function/class method in rules_engine"}]

            except Exception as e:
                rules_rows = [{"error": str(e)}]
        else:
            if not HAS_RULES_ENGINE:
                rules_rows = [{"status": "skipped", "reason": "rules_engine not available"}]
            elif not RULES_DB.exists():
                rules_rows = [{"status": "skipped", "reason": "rules.db not found in deployment"}]

        with st.expander("Rules engine debug", expanded=False):
            st.json(rules_debug)



# -----------------------------
# SRM Search: module if present, else direct FTS fallback
# -----------------------------
def _fts_match_query(query: str) -> str:
    """
    Build a safer FTS MATCH string: token AND token AND ...
    """
    q = deglue_text(query or "")
    toks = re.findall(r"[A-Za-z]{2,}|\d+(?:\.\d+)?|[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+", q)
    toks = [t.strip() for t in toks if t.strip()]
    if not toks:
        return ""
    cooked = []
    for t in toks:
        cooked.append(f'"{t}"' if "-" in t else t)
    return " AND ".join(cooked)


def srm_search_direct(db_path: Path, query: str, aircraft_family: Optional[str], limit: int = 8) -> List[Dict[str, Any]]:
    """
    Direct search in SQLite FTS5:
    pages_fts JOIN pages JOIN docs
    """
    if not db_path.exists():
        return [{"status": "skipped", "reason": "srm_index.db not found in deployment"}]

    match_q = _fts_match_query(query)
    if not match_q:
        return []

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        sql = """
        SELECT
          d.title AS title,
          d.revision AS revision,
          d.aircraft_family AS aircraft_family,
          d.file_name AS file_name,
          p.page_no AS page_no,
          snippet(pages_fts, 0, '[', ']', '…', 24) AS snippet,
          p.text AS text
        FROM pages_fts
        JOIN pages p ON p.id = pages_fts.rowid
        JOIN docs d  ON d.id = p.doc_id
        WHERE pages_fts MATCH ?
        """
        params: List[Any] = [match_q]

        if aircraft_family:
            sql += " AND d.aircraft_family = ?"
            params.append(str(aircraft_family).upper())

        sql += " ORDER BY rank LIMIT ?"
        params.append(int(limit))

        rows = con.execute(sql, tuple(params)).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "title": r["title"],
                    "revision": r["revision"],
                    "aircraft_family": r["aircraft_family"],
                    "file_name": r["file_name"],
                    "page_no": r["page_no"],
                    "snippet": r["snippet"],
                    "text": r["text"],
                }
            )
        return out
    finally:
        con.close()


def call_srm_search(structured: Dict[str, Any]) -> Any:
    if not SRM_DB.exists():
        return [{"status": "skipped", "reason": "srm_index.db not found in deployment"}]

    # Build a search query from structured fields (keep your original approach)
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
    if structured.get("dent_depth_mm"):
        q_bits.append(f"{structured['dent_depth_mm']} mm depth")
    if structured.get("dent_diameter_mm"):
        q_bits.append(f"{structured['dent_diameter_mm']} mm dia")
    q_bits.append("allowable damage repair limit")
    query = " ".join(q_bits).strip()

    # Prefer srm_search module if available + has expected functions
    if HAS_SRM_SEARCH:
        try:
            if hasattr(srm_search, "search") and callable(getattr(srm_search, "search")):  # type: ignore
                return srm_search.search(str(SRM_DB), query=query, aircraft_family=structured.get("aircraft_family"), limit=8)  # type: ignore
            if hasattr(srm_search, "srm_search") and callable(getattr(srm_search, "srm_search")):  # type: ignore
                return srm_search.srm_search(str(SRM_DB), query=query, aircraft_family=structured.get("aircraft_family"), limit=8)  # type: ignore
            # If module exists but missing expected fn, fall back to direct
        except Exception as e:
            # fall through to direct with context
            direct = srm_search_direct(SRM_DB, query=query, aircraft_family=structured.get("aircraft_family"), limit=8)
            return [{"error": f"srm_search module error: {e}", "fallback": direct}]

    # Direct fallback (works even if srm_search.py is broken/missing)
    return srm_search_direct(SRM_DB, query=query, aircraft_family=structured.get("aircraft_family"), limit=8)


# -----------------------------
# Dent model: robust construction
# -----------------------------
def make_dent_model(structured: Dict[str, Any]) -> Any:
    """
    Build DentDamage without assuming its constructor accepts aircraft_family.
    Tries a few signatures safely.
    """
    if not (HAS_DAMAGE_MODELS and structured.get("damage_type") == "DENT"):
        if structured.get("damage_type") != "DENT":
            return {"status": "skipped", "reason": "damage_type is not DENT"}
        return {"status": "skipped", "reason": "damage_models module not available"}

    assert DentDamage is not None
    assert assess_dent is not None

    # candidate kwargs (we’ll try subsets)
    kwargs_full = dict(
        aircraft_family=structured.get("aircraft_family") or "UNKNOWN",
        structure=(structured.get("structure") or "UNKNOWN"),
        zone=(structured.get("structure_zone") or "UNKNOWN"),
        side=(structured.get("side") or "ANY"),
        sta=float(structured.get("sta") or 0.0) if structured.get("sta") is not None else None,
        stringer=structured.get("stringer"),
        diameter_mm=float(structured.get("dent_diameter_mm") or 0.0) if structured.get("dent_diameter_mm") is not None else None,
        depth_mm=float(structured.get("dent_depth_mm") or 0.0) if structured.get("dent_depth_mm") is not None else None,
        has_crack=structured.get("has_crack"),
        notes=structured.get("notes"),
        raw_text=structured.get("raw"),
    )

    # ordered attempts
    attempts: List[Dict[str, Any]] = []

    # 1) full (may fail on aircraft_family)
    attempts.append(kwargs_full)

    # 2) drop aircraft_family
    k2 = dict(kwargs_full)
    k2.pop("aircraft_family", None)
    attempts.append(k2)

    # 3) minimal required-ish
    k3 = {
        "structure": kwargs_full["structure"],
        "zone": kwargs_full["zone"],
        "side": kwargs_full["side"],
        "sta": kwargs_full["sta"],
        "stringer": kwargs_full["stringer"],
        "diameter_mm": kwargs_full["diameter_mm"],
        "depth_mm": kwargs_full["depth_mm"],
        "has_crack": kwargs_full["has_crack"],
    }
    attempts.append(k3)

    last_err = None
    dent_obj = None
    for kw in attempts:
        try:
            dent_obj = DentDamage(**kw)  # type: ignore
            break
        except Exception as e:
            last_err = e

    if dent_obj is None:
        return {"status": "error", "error": f"Could not construct DentDamage: {last_err}"}

    try:
        res = assess_dent(dent_obj)  # type: ignore
        return res if isinstance(res, dict) else {"result": res}
    except Exception as e:
        return {"status": "error", "error": str(e)}


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

    # SRM DB Debug (keep)
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

           # --------------
        # Dent model
        # --------------
        dent_result: Dict[str, Any] = {"status": "not_run"}
        dent_debug: Dict[str, Any] = {}

        if HAS_DAMAGE_MODELS and structured.get("damage_type") == "DENT" and DentDamage is not None and assess_dent is not None:
            try:
                # Build a rich "candidate" dict (we will FILTER before constructing)
                candidate = {
                    "aircraft_family": structured.get("aircraft_family") or "UNKNOWN",
                    "aircraft_type": structured.get("aircraft_family") or "UNKNOWN",
                    "aircraft": structured.get("aircraft_family") or "UNKNOWN",

                    "structure": structured.get("structure") or "UNKNOWN",
                    "area": structured.get("structure") or "UNKNOWN",
                    "component": structured.get("structure") or "UNKNOWN",

                    "zone": structured.get("structure_zone") or "UNKNOWN",
                    "structure_zone": structured.get("structure_zone") or "UNKNOWN",
                    "subzone": structured.get("structure_zone") or "UNKNOWN",

                    "side": structured.get("side") or "ANY",
                    "sta": structured.get("sta"),
                    "wl": structured.get("wl"),
                    "stringer": structured.get("stringer"),

                    "diameter_mm": structured.get("dent_diameter_mm"),
                    "dent_diameter_mm": structured.get("dent_diameter_mm"),
                    "dia_mm": structured.get("dent_diameter_mm"),

                    "depth_mm": structured.get("dent_depth_mm"),
                    "dent_depth_mm": structured.get("dent_depth_mm"),
                    "dep_mm": structured.get("dent_depth_mm"),

                    "has_crack": structured.get("has_crack"),
                    "crack": structured.get("has_crack"),

                    "notes": structured.get("notes"),
                }

                # Introspect DentDamage accepted params
                try:
                    sig = inspect.signature(DentDamage)
                    accepted = set(sig.parameters.keys())
                except Exception:
                    accepted = set()

                # Filter candidate keys to ONLY what DentDamage accepts
                filtered = {k: v for k, v in candidate.items() if k in accepted and v is not None}

                dent_debug = {
                    "DentDamage_signature": str(inspect.signature(DentDamage)) if DentDamage else None,
                    "accepted_params": sorted(list(accepted)),
                    "filtered_kwargs_used": filtered,
                    "dropped_candidate_keys": sorted([k for k in candidate.keys() if k not in accepted]),
                }

                dent_obj = DentDamage(**filtered)  # <-- cannot send unsupported kwargs now
                res = assess_dent(dent_obj)  # type: ignore
                dent_result = res if isinstance(res, dict) else {"result": res}

            except Exception as e:
                dent_result = {"status": "error", "error": f"{e}", "debug": dent_debug}
        else:
            if structured.get("damage_type") != "DENT":
                dent_result = {"status": "skipped", "reason": "damage_type is not DENT"}
            elif not HAS_DAMAGE_MODELS:
                dent_result = {"status": "skipped", "reason": "damage_models module not available"}

        with st.expander("Dent model debug", expanded=False):
        st.json(dent_debug)

        # --------------
        # Optional logging
        # --------------
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
        ensure_assessments_db(ASSESSMENTS_DB)
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
