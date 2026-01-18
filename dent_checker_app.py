# dent_checker_app.py
# Streamlit app: SRM Damage Assessment (Prototype)
#
# Key features:
# - Fast “free-text” damage description parsing into structured fields
# - Dent assessment using damage_models (if present)
# - Rules evaluation using rules_engine (if present)
# - SRM full-text search using srm_index.db (if present)
# - SRM DB Debug panel (shows cwd + existence + size + sha256 prefix)
# - SRM Reference (top hit)
# - Final “statement” line with guardrails (WITHIN/OUT/UNKNOWN)
# - Optional logging of assessments to SQLite (assessments.db) with schema migration
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
# Tip: On Streamlit Cloud, only committed files exist at runtime.

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
    if t.startswith("B7"):  # B737, B787, etc.
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


# -----------------------------
# assessments.db logging (with migration)
# -----------------------------
def _table_columns(con: sqlite3.Connection, table: str) -> List[str]:
    rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    return [r[1] for r in rows]


def init_assessments_db(db_path: Path) -> None:
    con = sqlite3.connect(str(db_path))
    try:
        # Create minimal table if missing
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
              rules_ctx_json TEXT,
              srm_hits_json TEXT,
              srm_ref_top_json TEXT,
              result_json TEXT,
              final_statement TEXT
            );
            """
        )
        con.commit()

        # Migrate older schemas by adding any missing columns
        cols = set(_table_columns(con, "assessments"))
        wanted: Dict[str, str] = {
            "rules_ctx_json": "TEXT",
            "srm_ref_top_json": "TEXT",
            "final_statement": "TEXT",
        }
        for col, ctype in wanted.items():
            if col not in cols:
                con.execute(f"ALTER TABLE assessments ADD COLUMN {col} {ctype};")
        con.commit()
    finally:
        con.close()


def log_assessment(
    db_path: Path,
    structured: Dict[str, Any],
    rules_rows: Any,
    rules_ctx: Any,
    srm_hits: Any,
    srm_top_ref: Any,
    result: Any,
    final_statement: str,
) -> None:
    init_assessments_db(db_path)

    con = sqlite3.connect(str(db_path))
    try:
        con.execute(
            """
            INSERT INTO assessments (
              created_utc, aircraft_family, structure, structure_zone, side, sta, wl, stringer,
              damage_type, dent_diameter_mm, dent_depth_mm, has_crack,
              input_text, structured_json, rules_json, rules_ctx_json, srm_hits_json, srm_ref_top_json, result_json, final_statement
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                safe_json(rules_ctx),
                safe_json(srm_hits),
                safe_json(srm_top_ref),
                safe_json(result),
                final_statement,
            ),
        )
        con.commit()
    finally:
        con.close()


# -----------------------------
# Compatibility adapters + debug
# -----------------------------
def _signature_str(obj: Any) -> Optional[str]:
    try:
        return str(inspect.signature(obj))
    except Exception:
        return None


def _callable_exports(module: Any) -> List[str]:
    try:
        return sorted([k for k in dir(module) if not k.startswith("_")])
    except Exception:
        return []


def _dataclass_to_dict(x: Any) -> Any:
    if is_dataclass(x):
        return asdict(x)
    return x


def _build_rules_ctx(structured: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build the ctx shape that rules_engine.assess_damage expects (based on observed signature):
      assess_damage(db_path: str, aircraft_family: str, ctx: Dict[str, Any], revision: Optional[str] = None)
    """
    ctx: Dict[str, Any] = {
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
        "_flat": dict(structured),
    }
    return ctx


def _construct_dent_damage(structured: Dict[str, Any]) -> Tuple[Optional[Any], Dict[str, Any]]:
    """
    Construct DentDamage in a signature-safe way (introspection + filtered kwargs).
    Also provides a bool crack_present for models that require it.
    """
    dbg: Dict[str, Any] = {
        "DentDamage_signature": None,
        "accepted_params": [],
        "filtered_kwargs_used": {},
        "dropped_candidate_keys": [],
        "crack_present_used": None,
        "crack_present_reason": None,
    }

    if not HAS_DAMAGE_MODELS or DentDamage is None:
        return None, dbg

    sig = None
    try:
        sig = inspect.signature(DentDamage)  # type: ignore
        dbg["DentDamage_signature"] = str(sig)
        accepted = [p.name for p in sig.parameters.values() if p.name != "self"]
        dbg["accepted_params"] = accepted
    except Exception:
        accepted = []
        dbg["DentDamage_signature"] = None

    # Candidate mappings (we'll filter by accepted param names)
    # NOTE: your current DentDamage signature (seen in debug) is:
    # (aircraft_type, structure_zone, side, sta, stringer, dent_diameter_mm, dent_depth_mm, crack_present, notes=None)
    candidates: Dict[str, Any] = {
        "aircraft_type": structured.get("aircraft_family") or "UNKNOWN",
        "aircraft_family": structured.get("aircraft_family") or "UNKNOWN",
        "aircraft": structured.get("aircraft_family") or "UNKNOWN",
        "structure_zone": (structured.get("structure_zone") or "UNKNOWN"),
        "zone": (structured.get("structure_zone") or "UNKNOWN"),
        "subzone": structured.get("structure_zone"),
        "side": structured.get("side") or "ANY",
        "sta": None if structured.get("sta") is None else str(int(structured["sta"])) if float(structured["sta"]).is_integer() else str(structured["sta"]),
        "stringer": structured.get("stringer"),
        "dent_diameter_mm": float(structured.get("dent_diameter_mm") or 0.0),
        "dent_depth_mm": float(structured.get("dent_depth_mm") or 0.0),
        "diameter_mm": float(structured.get("dent_diameter_mm") or 0.0),
        "depth_mm": float(structured.get("dent_depth_mm") or 0.0),
        "dia_mm": float(structured.get("dent_diameter_mm") or 0.0),
        "dep_mm": float(structured.get("dent_depth_mm") or 0.0),
        "notes": structured.get("notes"),
        "crack": structured.get("has_crack"),
        "has_crack": structured.get("has_crack"),
        "crack_present": structured.get("has_crack"),
    }

    # crack_present must be bool for your DentDamage
    crack_present: bool
    if structured.get("has_crack") is True:
        crack_present = True
        reason = "from structured.has_crack=True"
    elif structured.get("has_crack") is False:
        crack_present = False
        reason = "from structured.has_crack=False"
    else:
        # Guardrail: unknown crack -> default False for model input, but keep flag Unknown in rules ctx.
        crack_present = False
        reason = "defaulted False because crack status was Unknown"

    candidates["crack_present"] = crack_present
    dbg["crack_present_used"] = crack_present
    dbg["crack_present_reason"] = reason

    # Filter to accepted params
    if accepted:
        filtered = {k: v for k, v in candidates.items() if k in accepted}
        dbg["filtered_kwargs_used"] = filtered
        dropped = [k for k in candidates.keys() if k not in filtered]
        dbg["dropped_candidate_keys"] = dropped
        try:
            dent = DentDamage(**filtered)  # type: ignore
            return dent, dbg
        except Exception as e:
            dbg["error"] = f"constructor failed: {e}"
            return None, dbg

    # No signature -> try a best guess (legacy)
    try:
        dent = DentDamage(  # type: ignore
            aircraft_type=candidates["aircraft_type"],
            structure_zone=candidates["structure_zone"],
            side=candidates["side"],
            sta=candidates["sta"],
            stringer=candidates["stringer"],
            dent_diameter_mm=candidates["dent_diameter_mm"],
            dent_depth_mm=candidates["dent_depth_mm"],
            crack_present=crack_present,
            notes=candidates["notes"],
        )
        dbg["filtered_kwargs_used"] = {
            "aircraft_type": candidates["aircraft_type"],
            "structure_zone": candidates["structure_zone"],
            "side": candidates["side"],
            "sta": candidates["sta"],
            "stringer": candidates["stringer"],
            "dent_diameter_mm": candidates["dent_diameter_mm"],
            "dent_depth_mm": candidates["dent_depth_mm"],
            "crack_present": crack_present,
            "notes": candidates["notes"],
        }
        return dent, dbg
    except Exception as e:
        dbg["error"] = f"constructor failed: {e}"
        return None, dbg


def _run_rules(structured: Dict[str, Any]) -> Tuple[Any, Dict[str, Any], Dict[str, Any]]:
    """
    Returns (rules_result, debug, ctx_sent)
    """
    dbg: Dict[str, Any] = {
        "selected": None,
        "signature": None,
        "module_exports": _callable_exports(rules_engine) if rules_engine else [],
        "ctx_sent": None,
    }
    ctx = _build_rules_ctx(structured)
    dbg["ctx_sent"] = ctx

    if (not HAS_RULES_ENGINE) or (rules_engine is None):
        return [{"status": "skipped", "reason": "rules_engine not available"}], dbg, ctx
    if not RULES_DB.exists():
        return [{"status": "skipped", "reason": "rules.db not found in deployment"}], dbg, ctx

    # Prefer assess_damage(db_path, aircraft_family, ctx, revision=None)
    if hasattr(rules_engine, "assess_damage"):
        fn = getattr(rules_engine, "assess_damage")
        dbg["selected"] = "assess_damage"
        dbg["signature"] = _signature_str(fn)
        try:
            res = fn(str(RULES_DB), structured.get("aircraft_family") or "UNKNOWN", ctx, None)
            res = _dataclass_to_dict(res)
            return res, dbg, ctx
        except Exception as e:
            return {"error": str(e)}, dbg, ctx

    # Backward compatibility: evaluate_rules/run_rules/evaluate patterns
    for name in ("evaluate_rules", "run_rules", "evaluate"):
        if hasattr(rules_engine, name):
            fn = getattr(rules_engine, name)
            dbg["selected"] = name
            dbg["signature"] = _signature_str(fn)
            try:
                # Try to call in a conservative way (some versions may take (db_path, ctx))
                try:
                    res = fn(str(RULES_DB), ctx)  # type: ignore
                except TypeError:
                    res = fn(str(RULES_DB), structured.get("aircraft_family") or "UNKNOWN", ctx)  # type: ignore
                res = _dataclass_to_dict(res)
                return res, dbg, ctx
            except Exception as e:
                return {"error": str(e)}, dbg, ctx

    return {"error": "rules_engine has no compatible rules function (assess_damage/evaluate_rules/run_rules/evaluate)"}, dbg, ctx


def _run_srm_search(structured: Dict[str, Any]) -> Tuple[Any, Dict[str, Any], Optional[Dict[str, Any]]]:
    """
    Returns (hits_as_dicts, debug, top_hit_dict)
    """
    dbg: Dict[str, Any] = {
        "selected": None,
        "signature": None,
        "query_used": None,
    }

    if (not HAS_SRM_SEARCH) or (srm_search is None):
        return [{"status": "skipped", "reason": "srm_search module not available"}], dbg, None
    if not SRM_DB.exists():
        return [{"status": "skipped", "reason": "srm_index.db not found in deployment"}], dbg, None

    # Build a search query from structured fields
    q_bits = []
    if structured.get("aircraft_family"):
        q_bits.append(str(structured["aircraft_family"]))
    if structured.get("structure"):
        q_bits.append(str(structured["structure"]))
    if structured.get("structure_zone"):
        q_bits.append(str(structured["structure_zone"]))
    if structured.get("damage_type"):
        q_bits.append(str(structured["damage_type"]))

    # Bias toward Table 102 dents for your current excerpt
    if structured.get("damage_type") == "DENT":
        q_bits += ["allowable damage", "dent", "table 102"]

    # Extra anchors
    q_bits.append("allowable damage repair limit")

    query = " ".join(q_bits).strip()
    dbg["query_used"] = query

    # Your srm_search.py defines: search_srm(conn, query, aircraft_family=None, limit=6) -> List[SRMHit]
    if hasattr(srm_search, "search_srm"):
        fn = getattr(srm_search, "search_srm")
        dbg["selected"] = "search_srm"
        dbg["signature"] = _signature_str(fn)
        try:
            con = sqlite3.connect(str(SRM_DB))
            try:
                hits = fn(con, query=query, aircraft_family=structured.get("aircraft_family"), limit=8)  # type: ignore
            finally:
                con.close()

            hits_dicts: List[Dict[str, Any]] = []
            for h in hits:
                if is_dataclass(h):
                    hits_dicts.append(asdict(h))
                else:
                    hits_dicts.append({"hit": str(h)})

            top = hits_dicts[0] if hits_dicts else None
            return hits_dicts, dbg, top
        except Exception as e:
            return [{"error": str(e)}], dbg, None

    # Fallback naming
    return [{"error": "srm_search module has no search_srm()"}], dbg, None


# -----------------------------
# SRM metadata extraction + final statement
# -----------------------------
def _extract_srm_meta_from_text(text: str) -> Dict[str, Optional[str]]:
    t = text or ""
    ata = None
    ad_no = None
    table_no = None

    m = re.search(r"\b(\d{2}-\d{2}-\d{2})\b", t)
    if m:
        ata = m.group(1)

    m = re.search(r"\bALLOWABLE\s*DAMAGE\s*(\d+)\b", t, flags=re.IGNORECASE)
    if m:
        ad_no = m.group(1)

    m = re.search(r"\bTable\s*(\d+)\b", t, flags=re.IGNORECASE)
    if m:
        table_no = m.group(1)

    return {"ata": ata, "allowable_damage_no": ad_no, "table_no": table_no}


def _derive_srm_meta(top_hit: Optional[Dict[str, Any]], rules_result: Any) -> Dict[str, Optional[str]]:
    """
    Best-effort extraction of:
      ATA chapter-subchapter (e.g., 53-00-01)
      Allowable Damage # (e.g., 1)
      Table # (e.g., 102)
      Page #
    """
    meta: Dict[str, Optional[str]] = {
        "ata": None,
        "allowable_damage_no": None,
        "table_no": None,
        "page": None,
        "doc_title": None,
        "revision": None,
        "file_name": None,
    }

    # From top hit
    if top_hit:
        meta["doc_title"] = str(top_hit.get("doc_title") or "")
        meta["revision"] = str(top_hit.get("revision") or "")
        meta["file_name"] = str(top_hit.get("file_name") or "")
        if top_hit.get("page") is not None:
            meta["page"] = str(top_hit.get("page"))

        # ATA from filename/doc title patterns like SRM_53-00-01...
        for src in (meta["file_name"], meta["doc_title"]):
            if src:
                m = re.search(r"(\d{2}-\d{2}-\d{2})", src)
                if m:
                    meta["ata"] = m.group(1)
                    break

        # Allowable Damage number from ADL1 / ADL2 patterns
        if meta["doc_title"]:
            m = re.search(r"\bADL\s*([0-9]+)\b", meta["doc_title"], flags=re.IGNORECASE)
            if m:
                meta["allowable_damage_no"] = m.group(1)

        # Try snippet text if present
        snip = str(top_hit.get("snippet") or "")
        from_snip = _extract_srm_meta_from_text(snip)
        for k in ("ata", "allowable_damage_no", "table_no"):
            if meta.get(k) is None and from_snip.get(k) is not None:
                meta[k] = from_snip[k]

    # From rules srm_ref string (usually strongest)
    srm_ref_str = None
    if isinstance(rules_result, dict):
        srm_ref_str = rules_result.get("srm_ref")
    if srm_ref_str:
        from_rules = _extract_srm_meta_from_text(str(srm_ref_str))
        for k in ("ata", "allowable_damage_no", "table_no"):
            if from_rules.get(k) is not None:
                meta[k] = from_rules[k]

    return meta


def _infer_limit_status(structured: Dict[str, Any], rules_result: Any) -> Tuple[str, str]:
    """
    Guardrailed limit status:
      - If crack present True => OUT OF LIMITS (Table entry requirements typically exclude cracks)
      - If crack unknown => UNKNOWN (don’t claim within/out)
      - Else use rules reasons:
          * contains "Within limits" => WITHIN LIMITS
          * passed False or reasons mention missing/exceed => OUT OF LIMITS or UNKNOWN depending on message
    Returns (status_token, rationale)
    """
    has_crack = structured.get("has_crack")

    if has_crack is True:
        return "OUT OF LIMITS", "Crack reported/present (Table entry requirements generally require no crack)."
    if has_crack is None:
        # do not claim within/out
        return "UNKNOWN", "Crack status is unknown; cannot assert within/out of limits."

    # has_crack is False here
    if not isinstance(rules_result, dict):
        return "UNKNOWN", "Rules result not in expected format."

    reasons = rules_result.get("reasons") or []
    reasons_text = " ".join([str(r) for r in reasons]).lower()

    # Some rule engines return "passed": true/false
    passed = rules_result.get("passed")
    if "within limits" in reasons_text:
        return "WITHIN LIMITS", "Rules engine indicates within limits."
    if passed is False:
        # if it's clearly missing info, keep it UNKNOWN
        if "missing" in reasons_text or "provide missing" in reasons_text:
            return "UNKNOWN", "Rules engine needs more classification/details to determine limits."
        return "OUT OF LIMITS", "Rules engine indicates not within limits."
    if "no rule_set found" in reasons_text:
        return "UNKNOWN", "No rule set available for this aircraft in rules.db."

    # Fallback
    return "UNKNOWN", "No definitive limit determination available."


def _compose_final_statement(structured: Dict[str, Any], srm_meta: Dict[str, Optional[str]], limit_status: str) -> str:
    dmg = (structured.get("damage_type") or "DAMAGE").upper()
    ac = (structured.get("aircraft_family") or "UNKNOWN").upper()

    ata = srm_meta.get("ata") or "UNKNOWN"
    ad_no = srm_meta.get("allowable_damage_no") or "?"
    table_no = srm_meta.get("table_no") or "?"
    page = srm_meta.get("page") or "?"

    # Required output format:
    # [damage type] is found [within/out of] [aircraft type] in accordance with SRM [ATA chapter-ATAsubchapter, Allowable Damage #, table #], [page #]
    return f"{dmg} is found {limit_status} {ac} SRM {ata}, Allowable Damage {ad_no}, Table {table_no}, page {page}."


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
        # Dent model
        # --------------
        dent_result: Dict[str, Any] = {"status": "not_run"}
        dent_debug: Dict[str, Any] = {}

        if HAS_DAMAGE_MODELS and structured.get("damage_type") == "DENT":
            try:
                dent_obj, dent_debug = _construct_dent_damage(structured)
                if dent_obj is None:
                    dent_result = {"status": "error", "error": "Could not construct DentDamage (see debug)."}
                else:
                    res = assess_dent(dent_obj)  # type: ignore
                    if isinstance(res, dict):
                        dent_result = res
                    else:
                        dent_result = {"result": str(res)}
            except Exception as e:
                dent_result = {"status": "error", "error": f"Could not construct/run DentDamage: {e}"}
        else:
            if structured.get("damage_type") != "DENT":
                dent_result = {"status": "skipped", "reason": "damage_type is not DENT"}
            elif not HAS_DAMAGE_MODELS:
                dent_result = {"status": "skipped", "reason": "damage_models module not available"}

        # --------------
        # Rules engine
        # --------------
        rules_result, rules_debug, rules_ctx = _run_rules(structured)

        # --------------
        # SRM Search
        # --------------
        srm_hits, srm_debug, top_hit = _run_srm_search(structured)

        # --------------
        # SRM Reference (top hit)
        # --------------
        if top_hit and isinstance(top_hit, dict):
            st.markdown("### SRM Reference (top hit)")
            title = top_hit.get("doc_title") or "SRM"
            page = top_hit.get("page")
            fn = top_hit.get("file_name")
            rev = top_hit.get("revision")
            st.write(f"**{title}** • Page {page} • File {fn} (Rev {rev})")
            st.code(str(top_hit.get("snippet") or "")[:1200], language="text")

        # --------------
        # Final statement (guardrailed)
        # --------------
        srm_meta = _derive_srm_meta(top_hit, rules_result if isinstance(rules_result, dict) else {})
        limit_status, limit_rationale = _infer_limit_status(structured, rules_result if isinstance(rules_result, dict) else {})
        final_statement = _compose_final_statement(structured, srm_meta, limit_status)

        st.markdown("### Final statement")
        st.code(final_statement, language="text")
        st.caption(f"Limit rationale: {limit_rationale}")

        # --------------
        # Render results
        # --------------
        st.markdown("### Dent model output")
        # If model has a plain-text summary helper, use it; otherwise json
        if build_plain_text_summary and isinstance(dent_result, dict):
            try:
                summary = build_plain_text_summary(dent_result)  # type: ignore
                st.code(summary, language="text")
            except Exception:
                st.json(dent_result)
        else:
            st.json(dent_result)

        with st.expander("Dent model debug", expanded=False):
            st.json(dent_debug or {})

        st.markdown("### Rules matches")
        st.json(rules_result)

        with st.expander("Rules engine debug", expanded=False):
            st.json(rules_debug or {})
            st.caption("ctx_sent")
            st.json(rules_ctx or {})

        st.markdown("### SRM search hits (prototype)")
        if isinstance(srm_hits, list) and srm_hits:
            # Show compact card-like output
            for hit in srm_hits[:8]:
                if not isinstance(hit, dict):
                    st.json(hit)
                    continue
                title = hit.get("doc_title") or hit.get("file_name") or "SRM hit"
                meta_line = []
                if hit.get("revision"):
                    meta_line.append(f"Rev: {hit.get('revision')}")
                if hit.get("aircraft_family"):
                    meta_line.append(f"Aircraft: {hit.get('aircraft_family')}")
                if hit.get("file_name"):
                    meta_line.append(f"File: {hit.get('file_name')}")
                if hit.get("page") is not None:
                    meta_line.append(f"Page: {hit.get('page')}")
                st.markdown(f"**{title}**" + (f" ({' • '.join(meta_line)})" if meta_line else ""))
                st.code(str(hit.get("snippet") or "")[:1200], language="text")
        else:
            st.json(srm_hits)

        with st.expander("SRM search debug", expanded=False):
            st.json(srm_debug or {})

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
                    rules_result,
                    rules_ctx,
                    srm_hits,
                    top_hit,
                    dent_result,
                    final_statement,
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
        cols = _table_columns(con, "assessments")
        # Be flexible if table is older
        base_cols = [
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
        ]
        # Only select what exists
        sel = [c for c in base_cols if c in cols]
        rows = con.execute(
            f"""
            SELECT {", ".join(sel)}
              FROM assessments
             ORDER BY id DESC
             LIMIT 25
            """
        ).fetchall()
        con.close()

        if rows:
            st.write(f"Showing last {len(rows)} logs from assessments.db")
            # Map rows dynamically to dicts
            out_rows = []
            for r in rows:
                d = dict(zip(sel, r))
                # Pretty crack
                if "has_crack" in d:
                    if d["has_crack"] is None:
                        d["crack"] = None
                    else:
                        d["crack"] = "Yes" if int(d["has_crack"]) == 1 else "No"
                    d.pop("has_crack", None)
                out_rows.append(d)

            st.dataframe(out_rows, use_container_width=True, hide_index=True)
        else:
            st.info("No logs yet.")
    except Exception as e:
        st.error(f"Could not read assessments.db: {e}")
else:
    st.caption("No assessments.db yet. Run an assessment and enable logging to create it.")
