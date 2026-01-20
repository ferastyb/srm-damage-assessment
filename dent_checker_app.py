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
# Resilient on Streamlit Cloud:
# - If a module/DB is missing, the app continues with warnings.
#
# IMPORTANT FIXES (ATA-gating + damage-type correctness):
# - Infer expected ATA chapter from structure (WING->57, FUSELAGE->53, etc.)
# - Prefer / filter SRM hits that match expected ATA; refuse definitive statement on ATA mismatch
# - If text indicates CRACK (and not DENT), do not run dent W/Y logic or dent SRM table logic
# - "no visible crack" forces has_crack=False
#
# Repo layout assumptions (root):
# - dent_checker_app.py  (this file)
# - damage_models.py
# - rules_engine.py
# - srm_search.py        (expects search_srm(conn, query, aircraft_family=None, limit=6))
# - rules.db
# - srm_index.db

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
    # expected exports in your project:
    # - DentDamage (dataclass)
    # - assess_dent(dent: DentDamage, ...) -> dict or result
    # - build_plain_text_summary(result, ...) -> str (optional)
    from damage_models import DentDamage as _DentDamage, assess_dent as _assess_dent  # type: ignore
    from damage_models import build_plain_text_summary as _build_plain_text_summary  # type: ignore

    DentDamage = _DentDamage
    assess_dent = _assess_dent
    build_plain_text_summary = _build_plain_text_summary
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


def _infer_ata_from_structure(structure: Optional[str]) -> Optional[str]:
    """
    Lightweight mapping for *expected* SRM ATA chapter.
    Adjust as you expand beyond prototype.
    """
    if not structure:
        return None
    s = structure.strip().upper()
    if s == "FUSELAGE":
        return "53"
    if s == "WING":
        return "57"
    if s == "EMPENNAGE" or s == "TAIL":
        return "55"
    if s == "NOSE" or s == "NACELLE":
        return None
    return None


def _extract_ata_from_text(blob: str) -> Optional[str]:
    """
    Tries to infer ATA chapter from SRM text/title/file.
    Looks for patterns like 53-00-01 or ATA 53 or ' 53-'.
    """
    if not blob:
        return None
    # 53-00-01 style
    m = re.search(r"\b(\d{2})-\d{2}-\d{2}\b", blob)
    if m:
        return m.group(1)
    # ATA 53 style
    m = re.search(r"\bATA\s*(\d{2})\b", blob, flags=re.IGNORECASE)
    if m:
        return m.group(1)
    # fallback: isolated "53-" token
    m = re.search(r"\b(\d{2})\s*-\s*\d{2}\b", blob)
    if m:
        return m.group(1)
    return None


def _hit_to_dict(hit: Any) -> Dict[str, Any]:
    if is_dataclass(hit):
        return asdict(hit)
    if isinstance(hit, dict):
        return hit
    # string repr fallback
    return {"repr": str(hit)}


def _coerce_hits_to_dicts(hits: Any) -> List[Dict[str, Any]]:
    if hits is None:
        return []
    if isinstance(hits, list):
        return [_hit_to_dict(h) for h in hits]
    return [_hit_to_dict(hits)]


def parse_damage_description(desc: str) -> Dict[str, Any]:
    """
    Lightweight parser.

    IMPORTANT FIX:
    - damage_type precedence:
        If "crack" appears AND "dent" does NOT appear, choose CRACK.
        If "dent" appears, choose DENT.
    - "no visible crack" forces has_crack=False even if the word crack appears elsewhere.
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

    # Stringer formats
    m = re.search(r"\bS[-\s]?(\d{1,3})([LR])\b", raw, flags=re.IGNORECASE)
    if m:
        out["stringer"] = f"{int(m.group(1))}{m.group(2).upper()}"
    else:
        m2 = re.search(r"\bSTRINGER\s*(\d{1,3})([LR])\b", raw, flags=re.IGNORECASE)
        if m2:
            out["stringer"] = f"{int(m2.group(1))}{m2.group(2).upper()}"

    # Crack present? (precedence: "no visible crack" wins)
    if re.search(r"\bno\s+(visible\s+)?crack(s)?\b", raw, flags=re.IGNORECASE):
        out["has_crack"] = False
    elif re.search(r"\bcrack(s)?\b", raw, flags=re.IGNORECASE):
        out["has_crack"] = True

    # Damage type precedence
    has_dent_word = bool(re.search(r"\bdent(s)?\b", raw, flags=re.IGNORECASE))
    has_crack_word = bool(re.search(r"\bcrack(s)?\b", raw, flags=re.IGNORECASE))

    if has_dent_word:
        out["damage_type"] = "DENT"
    elif has_crack_word:
        out["damage_type"] = "CRACK"
    elif re.search(r"\bgouge(s)?\b", raw, flags=re.IGNORECASE):
        out["damage_type"] = "GOUGE"
    elif re.search(r"\bcorrosion\b", raw, flags=re.IGNORECASE):
        out["damage_type"] = "CORROSION"

    # Dimensions (we still parse them, but ONLY dent logic uses W/Y etc.)
    dia_mm = _find_float_mm(raw, [
        r"(\d+(?:\.\d+)?)\s*mm\s*(?:dia|diameter)\b",
        r"\bdia\s*(\d+(?:\.\d+)?)\s*mm\b",
        r"(\d+(?:\.\d+)?)\s*mm\s*dia\b",
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


def _ensure_column(con: sqlite3.Connection, table: str, col: str, decl: str) -> None:
    """
    Backward-compatible schema evolution: add missing column if needed.
    """
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({table});").fetchall()]
    if col not in cols:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl};")


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
        # If you later decide to store extra debug blobs, add them safely:
        # _ensure_column(con, "assessments", "rules_ctx_json", "TEXT")

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


def _build_rules_ctx(structured: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build the ctx expected by rules_engine.assess_damage(db_path, aircraft_family, ctx, revision=None)
    """
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


def _best_effort_dataclass_to_dict(obj: Any) -> Any:
    if is_dataclass(obj):
        return asdict(obj)
    if hasattr(obj, "__dict__"):
        try:
            return dict(obj.__dict__)
        except Exception:
            return str(obj)
    return obj


def _summarize_result_for_ui(obj: Any) -> Any:
    # If assess_dent returns a dataclass-like result, show readable dict
    try:
        if is_dataclass(obj):
            return asdict(obj)
        return obj
    except Exception:
        return str(obj)


def _pick_srm_queries(structured: Dict[str, Any]) -> List[str]:
    """
    Build a small set of queries. We will ALSO enforce ATA-matching later.
    """
    af = structured.get("aircraft_family")
    structure = structured.get("structure")
    zone = structured.get("structure_zone")
    dmg = structured.get("damage_type")

    expected_ata = _infer_ata_from_structure(structure)
    ata_bits: List[str] = []
    if expected_ata:
        # include both "ATA 57" and "57-"
        ata_bits = [f"ATA {expected_ata}", f"{expected_ata}-"]

    # Keep these fairly general; ATA filtering does the safety.
    q1_bits = [b for b in [af, structure, zone, dmg] if b]
    q1_bits += ["allowable damage"] if dmg == "DENT" else []
    q1_bits += ata_bits
    q1_bits += ["table"] if dmg == "DENT" else []
    q1 = " ".join(q1_bits).strip()

    q2 = " ".join([b for b in ["allowable damage", "dent", "table"] + ata_bits if b]).strip()
    q3 = " ".join([b for b in [structure, dmg, "allowable damage"] + ata_bits if b]).strip()
    q4 = " ".join([b for b in ["damage limits"] + ata_bits if b]).strip()

    # Dedup, keep non-empty
    out: List[str] = []
    for q in [q1, q2, q3, q4]:
        q = re.sub(r"\s+", " ", q).strip()
        if q and q not in out:
            out.append(q)
    return out[:6]


def _score_hit_for_expected_ata(hit_dict: Dict[str, Any], expected_ata: Optional[str]) -> Tuple[int, Optional[str]]:
    """
    Returns (bonus, inferred_ata).
    bonus higher means better match to expected ATA.
    """
    if not expected_ata:
        return (0, None)

    blob = " ".join(
        [
            str(hit_dict.get("doc_title") or ""),
            str(hit_dict.get("file_name") or ""),
            str(hit_dict.get("snippet") or ""),
        ]
    )
    inferred = _extract_ata_from_text(blob)
    if inferred == expected_ata:
        return (100, inferred)

    # weaker matches
    if re.search(rf"\bATA\s*{re.escape(expected_ata)}\b", blob, flags=re.IGNORECASE):
        return (60, inferred)
    if re.search(rf"\b{re.escape(expected_ata)}-", blob):
        return (40, inferred)

    return (-50, inferred)


def _filter_and_sort_hits_by_ata(hits: List[Dict[str, Any]], expected_ata: Optional[str]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Prefer ATA-matching hits. If none match, return original but mark mismatch in debug.
    """
    debug: Dict[str, Any] = {"expected_ata": expected_ata, "hits_in": len(hits)}
    if not hits:
        debug["hits_out"] = 0
        return hits, debug

    scored: List[Tuple[int, Dict[str, Any], Optional[str]]] = []
    for h in hits:
        bonus, inferred = _score_hit_for_expected_ata(h, expected_ata)
        scored.append((bonus, h, inferred))

    scored.sort(key=lambda t: t[0], reverse=True)
    debug["top_scores"] = [{"bonus": s[0], "inferred_ata": s[2], "page": s[1].get("page") or s[1].get("page_no")} for s in scored[:5]]

    # keep only strong matches if we have at least 1 exact
    exact = [t for t in scored if t[0] >= 100]
    if exact:
        out = [t[1] for t in exact]
        debug["mode"] = "filtered_to_exact_ata"
        debug["hits_out"] = len(out)
        return out, debug

    # otherwise keep all but sorted (best first)
    out = [t[1] for t in scored]
    debug["mode"] = "sorted_no_exact_ata"
    debug["hits_out"] = len(out)
    return out, debug


def _build_final_statement(
    structured: Dict[str, Any],
    top_hit: Optional[Dict[str, Any]],
) -> Tuple[Optional[str], Dict[str, Any]]:
    """
    Produce the end-goal statement only when we are confident we are in the right ATA.
    If ATA mismatch, return None and explain why in debug.
    """
    debug: Dict[str, Any] = {}
    if not top_hit:
        return None, {"reason": "no_srm_hit"}

    damage_type = structured.get("damage_type") or "DAMAGE"
    aircraft = structured.get("aircraft_family") or "UNKNOWN"
    structure = structured.get("structure")
    expected_ata = _infer_ata_from_structure(structure)
    debug["expected_ata"] = expected_ata

    blob = " ".join([str(top_hit.get("doc_title") or ""), str(top_hit.get("file_name") or ""), str(top_hit.get("snippet") or "")])
    inferred_ata = _extract_ata_from_text(blob)
    debug["inferred_ata_from_hit"] = inferred_ata

    # Hard gate: if we expected an ATA and the hit looks like a different ATA, do NOT claim compliance.
    if expected_ata and inferred_ata and inferred_ata != expected_ata:
        return None, {
            **debug,
            "reason": "ata_mismatch",
            "message": f"Expected ATA {expected_ata} from structure={structure}, but top SRM hit looks like ATA {inferred_ata}.",
        }

    # Extract best-effort SRM identifiers
    # doc_title often looks like SRM_53-00-01_ADL1
    doc_title = top_hit.get("doc_title") or top_hit.get("title") or "SRM"
    revision = top_hit.get("revision") or "UNKNOWN"
    file_name = top_hit.get("file_name") or "UNKNOWN"
    page = top_hit.get("page") or top_hit.get("page_no") or "?"

    # Attempt to pull "53-00-01" from doc title/file/snippet
    ata_triplet = None
    m = re.search(r"\b(\d{2}-\d{2}-\d{2})\b", blob)
    if m:
        ata_triplet = m.group(1)

    # If we only have chapter
    if not ata_triplet and expected_ata:
        ata_triplet = f"{expected_ata}-??-??"

    # Attempt to find "Table 102" in snippet
    table_no = None
    m = re.search(r"\bTable\s*([0-9]{1,4})\b", str(top_hit.get("snippet") or ""), flags=re.IGNORECASE)
    if m:
        table_no = m.group(1)

    # Attempt to infer Allowable Damage #
    adl = None
    m = re.search(r"\bALLOWABLE\s*DAMAGE\s*([0-9]+)\b", blob, flags=re.IGNORECASE)
    if m:
        adl = m.group(1)

    # Determine within/out from rules result if it provides passed/within_limits,
    # otherwise stay neutral.
    status_phrase = "found (SRM reference identified)"
    # Prefer rules engine result if present in session state
    rules_obj = st.session_state.get("_last_rules_obj")
    dent_obj = st.session_state.get("_last_dent_obj")

    if isinstance(rules_obj, dict):
        if rules_obj.get("passed") is True:
            status_phrase = "is found within limits"
        elif rules_obj.get("passed") is False:
            status_phrase = "is found out of limits"

    # If dent model result exists (and is dent), it can also set within/out
    if structured.get("damage_type") == "DENT":
        # Dent model might be dict or string; try best effort
        if isinstance(dent_obj, dict):
            within = dent_obj.get("within_limits")
            if within is True:
                status_phrase = "is found within limits"
            elif within is False:
                status_phrase = "is found out of limits"

    # Compose statement
    bits = [f"{damage_type} {status_phrase} per {aircraft} SRM"]
    if ata_triplet:
        bits.append(f"{ata_triplet}")
    if adl:
        bits.append(f"Allowable Damage {adl}")
    if table_no:
        bits.append(f"Table {table_no}")
    bits.append(f"Page {page}")

    statement = ", ".join(bits) + "."
    debug.update(
        {
            "doc_title": doc_title,
            "revision": revision,
            "file_name": file_name,
            "page": page,
            "ata_triplet": ata_triplet,
            "allowable_damage_no": adl,
            "table_no": table_no,
        }
    )
    return statement, debug


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
        damage_type = st.selectbox("Damage type", ["DENT", "CRACK", "GOUGE", "CORROSION", "OTHER"], index=0)

    d1, d2, d3 = st.columns(3)
    with d1:
        dent_dia = st.number_input("Diameter (mm)", value=float(structured.get("dent_diameter_mm") or 0.0), step=0.1, format="%.2f")
    with d2:
        dent_depth = st.number_input("Depth (mm)", value=float(structured.get("dent_depth_mm") or 0.0), step=0.1, format="%.2f")
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
        # reset last-run caches used by final statement builder
        st.session_state["_last_rules_obj"] = None
        st.session_state["_last_dent_obj"] = None

        # --------------
        # Dent model (ONLY for DENT)
        # --------------
        dent_result: Dict[str, Any] = {"status": "not_run"}
        dent_debug: Dict[str, Any] = {}

        if structured.get("damage_type") == "DENT" and HAS_DAMAGE_MODELS and DentDamage and assess_dent:
            try:
                sig = None
                try:
                    sig = str(inspect.signature(DentDamage))  # type: ignore[arg-type]
                except Exception:
                    sig = None

                dent_debug["DentDamage_signature"] = sig

                # Construct DentDamage by filtering kwargs to accepted params (your model differs over time)
                accepted = set()
                try:
                    accepted = set(inspect.signature(DentDamage).parameters.keys())  # type: ignore[arg-type]
                except Exception:
                    accepted = set()

                # Candidate mapping (many-to-one)
                candidates: Dict[str, Any] = {
                    "aircraft_type": structured.get("aircraft_family") or "UNKNOWN",
                    "aircraft_family": structured.get("aircraft_family") or "UNKNOWN",
                    "structure_zone": structured.get("structure_zone") or "UNKNOWN",
                    "zone": structured.get("structure_zone") or "UNKNOWN",
                    "side": structured.get("side") or "ANY",
                    "sta": None if structured.get("sta") is None else str(int(structured.get("sta"))),
                    "stringer": structured.get("stringer"),
                    "dent_diameter_mm": float(structured.get("dent_diameter_mm") or 0.0),
                    "dent_depth_mm": float(structured.get("dent_depth_mm") or 0.0),
                    "diameter_mm": float(structured.get("dent_diameter_mm") or 0.0),
                    "depth_mm": float(structured.get("dent_depth_mm") or 0.0),
                    "notes": structured.get("notes"),
                }

                # crack_present / has_crack mapping
                crack_present_val: Optional[bool]
                if structured.get("has_crack") is None:
                    crack_present_val = False
                    dent_debug["crack_present_reason"] = "defaulted False because crack status was Unknown"
                else:
                    crack_present_val = bool(structured.get("has_crack"))
                    dent_debug["crack_present_reason"] = "from structured.has_crack"

                candidates["crack_present"] = crack_present_val
                candidates["has_crack"] = crack_present_val
                dent_debug["crack_present_used"] = crack_present_val

                filtered = {k: v for k, v in candidates.items() if (not accepted) or (k in accepted)}
                dent_debug["accepted_params"] = sorted(list(accepted)) if accepted else None
                dent_debug["filtered_kwargs_used"] = filtered

                dent_obj = DentDamage(**filtered)  # type: ignore[misc]
                out = assess_dent(dent_obj)  # type: ignore[misc]
                if isinstance(out, dict):
                    dent_result = out
                else:
                    dent_result = {"result": str(out)}

                st.session_state["_last_dent_obj"] = out if isinstance(out, dict) else {"result": str(out)}

            except Exception as e:
                dent_result = {"status": "error", "error": f"Could not construct/run DentDamage: {e}"}
        else:
            if structured.get("damage_type") != "DENT":
                dent_result = {"status": "skipped", "reason": "damage_type is not DENT (dent model not applicable)"}
            elif not HAS_DAMAGE_MODELS:
                dent_result = {"status": "skipped", "reason": "damage_models module not available"}

        # --------------
        # Rules engine
        # --------------
        rules_obj: Any = {}
        rules_debug: Dict[str, Any] = {}

        if HAS_RULES_ENGINE and RULES_DB.exists():
            try:
                fn = None
                if hasattr(rules_engine, "assess_damage"):
                    fn = getattr(rules_engine, "assess_damage")
                    rules_debug["selected"] = "assess_damage"
                elif hasattr(rules_engine, "evaluate_rules"):
                    fn = getattr(rules_engine, "evaluate_rules")
                    rules_debug["selected"] = "evaluate_rules"
                elif hasattr(rules_engine, "run_rules"):
                    fn = getattr(rules_engine, "run_rules")
                    rules_debug["selected"] = "run_rules"

                if fn is None:
                    rules_obj = {"error": "rules_engine has no compatible rules function (assess_damage/evaluate_rules/run_rules)"}
                else:
                    rules_debug["signature"] = str(inspect.signature(fn))
                    ctx = _build_rules_ctx(structured)
                    rules_debug["ctx_sent"] = ctx

                    # Call shape:
                    # assess_damage(db_path, aircraft_family, ctx, revision=None)
                    # Some older versions might accept (db_path, ctx) only; we try best effort.
                    try:
                        rules_obj = fn(str(RULES_DB), structured.get("aircraft_family") or "UNKNOWN", ctx)  # type: ignore[misc]
                    except TypeError:
                        # fallback
                        rules_obj = fn(str(RULES_DB), ctx)  # type: ignore[misc]

                if is_dataclass(rules_obj):
                    rules_obj = asdict(rules_obj)
                elif hasattr(rules_obj, "__dict__") and not isinstance(rules_obj, dict):
                    rules_obj = _best_effort_dataclass_to_dict(rules_obj)

                st.session_state["_last_rules_obj"] = rules_obj

            except Exception as e:
                rules_obj = {"error": str(e)}
        else:
            if not HAS_RULES_ENGINE:
                rules_obj = {"status": "skipped", "reason": "rules_engine not available"}
            elif not RULES_DB.exists():
                rules_obj = {"status": "skipped", "reason": "rules.db not found in deployment"}

        # --------------
        # SRM Search (ATA-gated)
        # --------------
        srm_hits: List[Dict[str, Any]] = []
        srm_debug: Dict[str, Any] = {}

        expected_ata = _infer_ata_from_structure(structured.get("structure"))
        srm_debug["expected_ata"] = expected_ata

        if HAS_SRM_SEARCH and SRM_DB.exists():
            try:
                # srm_search.search_srm expects a sqlite3.Connection
                con = sqlite3.connect(str(SRM_DB))

                try:
                    queries = _pick_srm_queries(structured)
                    srm_debug["queries_tried"] = queries

                    raw_hits: List[Any] = []
                    # Try each query until we get something
                    for q in queries:
                        h = srm_search.search_srm(con, q, aircraft_family=structured.get("aircraft_family"), limit=10)  # type: ignore[attr-defined]
                        if h:
                            raw_hits = h
                            srm_debug["query_used"] = q
                            break

                    # coerce to dicts
                    srm_hits = _coerce_hits_to_dicts(raw_hits)

                    # ATA filter/sort
                    srm_hits, ata_debug = _filter_and_sort_hits_by_ata(srm_hits, expected_ata)
                    srm_debug["ata_filter"] = ata_debug

                finally:
                    con.close()

            except Exception as e:
                srm_hits = [{"error": str(e)}]
        else:
            if not HAS_SRM_SEARCH:
                srm_hits = [{"status": "skipped", "reason": "srm_search module not available"}]
            elif not SRM_DB.exists():
                srm_hits = [{"status": "skipped", "reason": "srm_index.db not found in deployment"}]

        # --------------
        # SRM Reference (top hit) + Final statement (ATA-gated)
        # --------------
        top_hit = None
        if srm_hits and isinstance(srm_hits[0], dict) and "error" not in srm_hits[0]:
            top_hit = srm_hits[0]

        final_stmt, final_debug = _build_final_statement(structured, top_hit)
        if final_stmt:
            st.markdown("### Final statement (SRM-based)")
            st.success(final_stmt)
        else:
            # If we had hits but ATA mismatch, be explicit
            if final_debug.get("reason") == "ata_mismatch":
                st.markdown("### Final statement (SRM-based)")
                st.warning(
                    "ATA mismatch: refusing to issue a definitive SRM chapter/table statement. "
                    + str(final_debug.get("message") or "")
                )

        if top_hit:
            st.markdown("### SRM Reference (top hit)")
            doc = top_hit.get("doc_title") or "SRM"
            page = top_hit.get("page") or top_hit.get("page_no") or "?"
            file_name = top_hit.get("file_name") or "?"
            rev = top_hit.get("revision") or "UNKNOWN"
            st.write(f"**{doc}** • Page **{page}** • File **{file_name}** (Rev **{rev}**)")
            st.code(str(top_hit.get("snippet") or "")[:1200], language="text")

        # --------------
        # Render results
        # --------------
        st.markdown("### Dent model output")
        st.json(_summarize_result_for_ui(dent_result))

        if dent_debug:
            with st.expander("Dent model debug", expanded=False):
                st.json(dent_debug)

        st.markdown("### Rules matches")
        st.json(rules_obj)

        if rules_debug:
            with st.expander("Rules engine debug", expanded=False):
                st.json(rules_debug)

        st.markdown("### SRM search hits (prototype)")
        if isinstance(srm_hits, list) and srm_hits and isinstance(srm_hits[0], dict) and "error" not in srm_hits[0]:
            # show up to 8
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
            st.json(srm_hits)

        with st.expander("SRM search debug", expanded=False):
            st.json(srm_debug)
            st.json(final_debug)

        # --------------
        # Optional logging
        # --------------
        st.markdown("### Logging")
        log_it = st.checkbox("Log this assessment to SQLite (assessments.db)", value=True)
        if log_it:
            try:
                log_assessment(ASSESSMENTS_DB, structured, rules_obj, srm_hits, dent_result)
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
