# dent_checker_app.py
# Streamlit app: SRM Damage Assessment (Prototype)

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


def parse_damage_description(desc: str) -> Dict[str, Any]:
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
    m = re.search(r"\bSTA(?:TION)?\s*([0-9]{2,6}(?:\.[0-9]+)?)\b", raw, flags=re.IGNORECASE)
    if m:
        try:
            out["sta"] = float(m.group(1))
        except Exception:
            pass

    m = re.search(r"\bWL\s*([0-9]{1,6}(?:\.[0-9]+)?)\b", raw, flags=re.IGNORECASE)
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


def _compute_wy_ratio(dia_mm: Optional[float], depth_mm: Optional[float]) -> Optional[float]:
    if dia_mm is None or depth_mm is None:
        return None
    if depth_mm <= 0:
        return None
    return dia_mm / depth_mm


def _pick_best_table102_hit(hits: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not hits:
        return None
    for h in hits:
        sn = str(h.get("snippet") or h.get("text") or "")
        if re.search(r"\bTable\s*102\b", sn, flags=re.IGNORECASE):
            return h
    return hits[0]


def _build_query_variants(structured: Dict[str, Any]) -> List[str]:
    a = structured.get("aircraft_type") or ""
    s = structured.get("structure") or ""
    z = structured.get("structure_zone") or ""
    d = structured.get("damage_type") or ""

    variants: List[str] = []
    variants.append(f"{a} {s} {z} {d} allowable damage dent table 102".strip())
    variants.append(f"{a} {s} {z} allowable damage dent table 102".strip())
    variants.append("allowable damage dent table 102")
    variants.append("table 102 dent")
    variants.append("fuselage skin dent allowable damage")
    variants.append("dent allowable")
    variants.append("allowable damage")

    out: List[str] = []
    seen = set()
    for q in variants:
        q2 = re.sub(r"\s+", " ", (q or "").strip())
        if not q2:
            continue
        k = q2.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(q2)
    return out


def _fallback_fts_search(
    con: sqlite3.Connection,
    query: str,
    aircraft_type: Optional[str],
    limit: int = 8,
) -> List[Dict[str, Any]]:
    """
    Direct FTS5 fallback if srm_search module returns nothing.
    """
    # Very forgiving OR query
    q = (query or "").strip()
    words = re.findall(r"[A-Za-z0-9]+", q)
    keep = []
    for w in words:
        wl = w.lower()
        if wl in {"the", "and", "or", "to", "of", "in", "on", "for", "with"}:
            continue
        if len(w) < 3:
            continue
        keep.append(wl)
    # Always add anchors
    anchors = ["allowable", "damage", "fuselage", "skin", "dent", "table", "102"]
    for a in anchors:
        if a not in keep:
            keep.append(a)
    match_expr = " OR ".join(keep[:18])

    sql = f"""
    SELECT
      d.title AS doc_title,
      d.revision AS revision,
      d.aircraft_family AS aircraft_family,
      d.file_name AS file_name,
      p.page_no AS page,
      snippet(pages_fts, 0, '[', ']', '…', 16) AS snippet,
      bm25(pages_fts) AS score
    FROM pages_fts
    JOIN pages p ON p.id = pages_fts.rowid
    JOIN docs d ON d.id = p.doc_id
    WHERE pages_fts MATCH ?
      AND (? IS NULL OR d.aircraft_family = ?)
    ORDER BY score
    LIMIT ?
    """
    rows = con.execute(sql, (match_expr, aircraft_type, aircraft_type, limit)).fetchall()
    return [
        {
            "doc_title": r[0],
            "revision": r[1],
            "aircraft_family": r[2],
            "file_name": r[3],
            "page": r[4],
            "pdf_page": r[4],
            "snippet": r[5],
            "score": r[6],
            "_fallback_match_expr": match_expr,
        }
        for r in rows
    ]


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
    desc = st.text_area("Damage description", value=default_text, height=120)

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

    # write-back
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

    # SRM DB Debug + integrity checks
    with st.expander("SRM DB Debug (Integrity + Search sanity)", expanded=True):
        st.write("cwd:", os.getcwd())
        st.write("SRM DB path:", str(SRM_DB))
        st.write("srm_index.db exists:", SRM_DB.exists())
        if SRM_DB.exists():
            st.write("srm_index.db size (bytes):", SRM_DB.stat().st_size)
            st.write("srm_index.db sha256 (prefix):", sha256_path(SRM_DB)[:16])

            try:
                con = sqlite3.connect(str(SRM_DB))
                docs_n = con.execute("select count(*) from docs").fetchone()[0]
                pages_n = con.execute("select count(*) from pages").fetchone()[0]
                fts_n = con.execute("select count(*) from pages_fts").fetchone()[0]
                st.write("docs:", int(docs_n), "pages:", int(pages_n), "pages_fts:", int(fts_n))

                fams = con.execute("select distinct aircraft_family from docs order by aircraft_family").fetchall()
                fam_list = [str(x[0]) for x in fams]
                st.write("docs.aircraft_family values:", fam_list)

                # FTS sanity
                test_allowable = con.execute("select count(*) from pages_fts where pages_fts match ?", ("allowable",)).fetchone()[0]
                st.write("FTS sanity (MATCH 'allowable') hits:", int(test_allowable))

                # LIKE sanity for table/102
                test_table = con.execute("select count(*) from pages where text like ?", ("%Table 102%",)).fetchone()[0]
                test_102 = con.execute("select count(*) from pages where text like ?", ("%102%",)).fetchone()[0]
                st.write("LIKE sanity (text contains 'Table 102'):", int(test_table))
                st.write("LIKE sanity (text contains '102'):", int(test_102))
                con.close()
            except Exception as e:
                st.error(f"DB sanity checks failed: {e}")
        else:
            st.info("Commit srm_index.db to the repo if you want SRM hits on Streamlit Cloud.")

    if run:
        selected_type = _normalize_aircraft_type(structured.get("aircraft_type") or "UNKNOWN")

        # ----------------
        # SRM Search (module + fallback)
        # ----------------
        srm_hits: List[Dict[str, Any]] = []
        srm_debug: Dict[str, Any] = {"queries_tried": [], "used_fallback": False}

        queries = _build_query_variants(structured)

        if SRM_DB.exists():
            con = sqlite3.connect(str(SRM_DB))
            try:
                # 1) Try module search if available
                if HAS_SRM_SEARCH and hasattr(srm_search, "search_srm"):
                    fn = getattr(srm_search, "search_srm")
                    srm_debug["signature"] = str(inspect.signature(fn))
                    for q in queries:
                        srm_debug["queries_tried"].append({"query": q, "filter": selected_type})
                        try:
                            hits = fn(con, query=q, aircraft_family=selected_type, limit=8)  # type: ignore
                            for h in hits or []:
                                d = asdict(h) if is_dataclass(h) else {"raw": str(h)}
                                d.setdefault("pdf_page", d.get("page") or d.get("pdf_page"))
                                srm_hits.append(d)
                        except Exception as e:
                            srm_debug.setdefault("module_errors", []).append(str(e))

                    # If module returns nothing, try without filter
                    if not srm_hits:
                        for q in queries:
                            srm_debug["queries_tried"].append({"query": q, "filter": None})
                            try:
                                hits = fn(con, query=q, aircraft_family=None, limit=8)  # type: ignore
                                for h in hits or []:
                                    d = asdict(h) if is_dataclass(h) else {"raw": str(h)}
                                    d.setdefault("pdf_page", d.get("page") or d.get("pdf_page"))
                                    srm_hits.append(d)
                            except Exception as e:
                                srm_debug.setdefault("module_errors", []).append(str(e))

                # 2) Fallback direct FTS if still empty
                if not srm_hits:
                    srm_debug["used_fallback"] = True
                    for q in queries[:3]:
                        srm_debug["queries_tried"].append({"fallback_query": q, "filter": selected_type})
                        srm_hits.extend(_fallback_fts_search(con, query=q, aircraft_type=selected_type, limit=8))
                    if not srm_hits:
                        for q in queries[:3]:
                            srm_debug["queries_tried"].append({"fallback_query": q, "filter": None})
                            srm_hits.extend(_fallback_fts_search(con, query=q, aircraft_type=None, limit=8))

            finally:
                con.close()
        else:
            srm_hits = [{"error": "srm_index.db not found in deployment"}]

        # de-dupe + sort
        def _score(h: Dict[str, Any]) -> float:
            try:
                return float(h.get("score"))
            except Exception:
                return 9999.0

        uniq: List[Dict[str, Any]] = []
        seen = set()
        for h in srm_hits:
            if "error" in h:
                uniq.append(h)
                continue
            key = (h.get("doc_title"), h.get("file_name"), int(h.get("page") or 0), str(h.get("snippet") or "")[:60])
            if key in seen:
                continue
            seen.add(key)
            uniq.append(h)
        srm_hits = sorted([h for h in uniq if isinstance(h, dict) and "error" not in h], key=_score) + [h for h in uniq if "error" in h]

        # Pick best hit for assessment: must match aircraft_type in docs if present
        hits_match_type = []
        for h in srm_hits:
            fam = h.get("aircraft_family")
            if fam is None:
                continue
            if _normalize_aircraft_type(str(fam)) == selected_type:
                hits_match_type.append(h)

        best_hit = _pick_best_table102_hit(hits_match_type) if hits_match_type else None

        # ----------------
        # Final statement gating
        # ----------------
        st.markdown("### Final statement (SRM-based)")
        dia = structured.get("dent_diameter_mm")
        dep = structured.get("dent_depth_mm")
        ratio = _compute_wy_ratio(dia, dep)

        if not best_hit:
            st.write(
                f"Reference: **(no SRM hit available for selected aircraft type in library)**\n"
                f"Decision: **NO ASSESSMENT GENERATED** (no SRM reference available for aircraft type **{selected_type}** "
                f"in the current library/index).\n\n"
                f"Tip: expand **SRM DB Debug (Integrity + Search sanity)** above — it will show whether FTS is populated."
            )
        else:
            doc = best_hit.get("doc_title") or best_hit.get("file_name") or "SRM"
            rev = best_hit.get("revision") or "UNKNOWN"
            file_name = best_hit.get("file_name") or ""
            pdf_page = best_hit.get("pdf_page") or best_hit.get("page")
            snippet = str(best_hit.get("snippet") or "")

            mtab = re.search(r"\bTable\s*(\d+)\b", snippet, flags=re.IGNORECASE)
            table_no = mtab.group(1) if mtab else "102"

            decision_lines = []
            decision_lines.append(f"Reference: **{doc}** (Rev {rev}) • **Table {table_no}** • **PDF page {pdf_page}** • File `{file_name}`")

            # Only apply prototype dent math if damage_type is DENT
            if structured.get("damage_type") != "DENT":
                decision_lines.append(f"Decision: **NO ASSESSMENT GENERATED** (damage_type={structured.get('damage_type')}; SRM dent logic applies to DENT only).")
            else:
                if structured.get("has_crack") is True:
                    decision_lines.append("Decision: **OUT OF LIMITS** (crack present ⇒ not allowable / engineering review).")
                elif dep is None:
                    decision_lines.append("Decision: **ENGINEERING REVIEW** (missing dent depth).")
                else:
                    if dep > 6.35:
                        decision_lines.append("Decision: **OUT OF LIMITS** (Depth > 6.35 mm (0.25 in) ⇒ not allowable).")
                    elif dep > 3.175:
                        if ratio is None:
                            decision_lines.append("Decision: **ENGINEERING REVIEW** (cannot compute W/Y ratio).")
                        else:
                            if ratio >= 30.0:
                                decision_lines.append("Decision: **WITHIN LIMITS** (Depth in (3.175..6.35] mm and W/Y ≥ 30).")
                            else:
                                decision_lines.append("Decision: **OUT OF LIMITS** (Depth in (3.175..6.35] mm and W/Y < 30).")
                    else:
                        decision_lines.append("Decision: **WITHIN LIMITS** (Depth ≤ 3.175 mm (0.125 in) prototype band).")

                decision_lines.append("")
                decision_lines.append("Proof / calculations:")
                if dia is None or dep is None:
                    decision_lines.append(f"- W (diameter): {dia} mm")
                    decision_lines.append(f"- Y (depth): {dep} mm")
                    decision_lines.append("- W/Y: (cannot compute)")
                else:
                    decision_lines.append(f"- W (diameter): {dia:.2f} mm")
                    decision_lines.append(f"- Y (depth): {dep:.2f} mm")
                    decision_lines.append(f"- W/Y = {dia:.2f} / {dep:.2f} = **{ratio:.2f}**" if ratio is not None else "- W/Y: (cannot compute)")

                if structured.get("has_crack") is True:
                    decision_lines.append("- Crack: **present** (override to OUT OF LIMITS)")
                elif structured.get("has_crack") is False:
                    decision_lines.append("- Crack: not reported")
                else:
                    decision_lines.append("- Crack: unknown (treated as not present for prototype dent math)")

            st.write("\n".join(decision_lines))

        # ----------------
        # Dent model output
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
                dent_debug = {"DentDamage_signature": str(sig), "filtered_kwargs_used": filtered}
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

        st.markdown("### Dent model output")
        if HAS_DAMAGE_MODELS and "build_plain_text_summary" in globals() and isinstance(dent_result, dict):
            try:
                st.code(build_plain_text_summary(dent_result), language="text")  # type: ignore
            except Exception:
                st.json(dent_result)
        else:
            st.json(dent_result)

        with st.expander("Dent model debug", expanded=False):
            st.json(dent_debug)

        # Rules (optional)
        st.markdown("### Rules matches")
        rules_rows: Any = [{"status": "skipped", "reason": "rules engine not evaluated in this patch"}]
        st.json(rules_rows)

        # SRM hits
        st.markdown("### SRM search hits (prototype)")
        if srm_hits:
            for hit in srm_hits[:8]:
                if "error" in hit:
                    st.error(hit["error"])
                    continue
                title = hit.get("doc_title") or hit.get("file_name") or "SRM hit"
                rev = hit.get("revision") or "UNKNOWN"
                fam = hit.get("aircraft_family") or ""
                file_name = hit.get("file_name") or ""
                pdf_page = hit.get("pdf_page") or hit.get("page")
                st.markdown(f"**{title}** (Rev: {rev} • Aircraft: {fam} • File: {file_name} • PDF page {pdf_page})")
                st.code(str(hit.get("snippet") or "")[:1200], language="text")
        else:
            st.write("No SRM hits returned.")

        with st.expander("SRM search debug", expanded=False):
            st.json(srm_debug)

        # Logging
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
