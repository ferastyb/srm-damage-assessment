#!/usr/bin/env python3
# dent_checker_app.py
#
# Streamlit SRM Damage Assessment Tool (Dent checker prototype)
# - Robust against missing/renamed functions in rules_engine.py / srm_search.py
# - Adds safe SQLite auto-migrations for assessments.db (adds missing columns)
# - Includes SRM index debug + direct FTS search (no dependency on srm_search module)
#
# Expected local files in repo root (Streamlit Cloud sees /mount/src/<repo>/):
# - rules.db            (SQLite rules DB)
# - srm_index.db        (SQLite FTS SRM index DB)
# - assessments.db      (created automatically)
#
# Optional:
# - damage_models.py    (if present, used for parsing/assessment; otherwise fallback logic used)

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import streamlit as st


# ----------------------------
# Paths
# ----------------------------

REPO_ROOT = Path(__file__).resolve().parent
RULES_DB_PATH = REPO_ROOT / "rules.db"
SRM_INDEX_DB_PATH = REPO_ROOT / "srm_index.db"
ASSESSMENTS_DB_PATH = REPO_ROOT / "assessments.db"


# ----------------------------
# Helpers: text normalization / "deglue" for display + search
# ----------------------------

_UNIT_RE = r"(mm|cm|m|in\.?|inch|inches|ft|psi|lb|lbs|cycles|deg|°c|°f)"
_DASHES = {
    "\u2010": "-",
    "\u2011": "-",
    "\u2012": "-",
    "\u2013": "-",
    "\u2014": "-",
}


def deglue_text(s: str) -> str:
    """
    Lightweight word-segmentation / deglue for PDF-extracted SRM text:
    - Adds spaces in common glue patterns (CamelCase, letter-digit, digit-letter)
    - Adds spaces around units and common glued function words (than, to, and, within, every, before)
    - Keeps it conservative (avoid over-splitting numbers like "51-40-05")
    """
    if not s:
        return ""

    # normalize dashes
    for k, v in _DASHES.items():
        s = s.replace(k, v)

    s = s.replace("\x00", " ")

    # Ensure whitespace after punctuation when missing
    s = re.sub(r"([.,;:])(?=\w)", r"\1 ", s)

    # CamelCase: AllowableDamage -> Allowable Damage
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)

    # Letters<->digits: Damage1 -> Damage 1, 3.0in -> 3.0 in
    s = re.sub(r"([A-Za-z])(\d)", r"\1 \2", s)
    s = re.sub(r"(\d)([A-Za-z])", r"\1 \2", s)

    # Glue words frequently seen in SRMs
    s = re.sub(r"\b(Greater|Less)than(?=\d|\b)", r"\1 than", s, flags=re.IGNORECASE)
    s = re.sub(r"\bmorethan(?=\d|\b)", "more than", s, flags=re.IGNORECASE)
    s = re.sub(r"(\d)\s*and(\d)", r"\1 and \2", s, flags=re.IGNORECASE)
    s = re.sub(r"(\d)\s*to(\d)", r"\1 to \2", s, flags=re.IGNORECASE)
    s = re.sub(r"\bwithin(?=\d)", "within ", s, flags=re.IGNORECASE)
    s = re.sub(r"\bevery(?=\d)", "every ", s, flags=re.IGNORECASE)
    s = re.sub(r"\bbefore(?=\d)", "before ", s, flags=re.IGNORECASE)
    s = re.sub(r"\bReferto(?=\d)", "Refer to ", s, flags=re.IGNORECASE)

    # Add a space before units (handles "3.175mm", "0.125in", "0.0045inch")
    s = re.sub(rf"(\d)\s*{_UNIT_RE}\b", r"\1 \2", s, flags=re.IGNORECASE)

    # Collapse whitespace
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = "\n".join(" ".join(line.split()) for line in s.splitlines())
    s = re.sub(r"\n{3,}", "\n\n", s).strip()

    return s


def query_tokens(q: str) -> List[str]:
    """
    Tokenize query into safe-ish FTS terms.
    """
    q = deglue_text(q)
    # Keep hyphens inside references like 53-00-01; split other punctuation
    raw = re.findall(r"[A-Za-z]{2,}|\d+(?:\.\d+)?|[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+", q)
    out: List[str] = []
    for t in raw:
        t = t.strip()
        if not t:
            continue
        out.append(t)
    return out


def build_fts_match_query(user_query: str) -> str:
    """
    Build an FTS5 MATCH query that tends to work better than a raw sentence.
    - AND the meaningful tokens
    - Keep it simple to avoid syntax errors
    """
    toks = query_tokens(user_query)
    if not toks:
        return ""

    # Quote tokens that contain hyphens (to keep them together in FTS parsing)
    cooked = []
    for t in toks:
        if "-" in t:
            cooked.append(f'"{t}"')
        else:
            cooked.append(t)

    # AND all terms
    return " AND ".join(cooked)


# ----------------------------
# Damage model fallback (if damage_models import fails)
# ----------------------------

@dataclass
class DentDamageFallback:
    aircraft: str = "B737"
    structure: str = "Fuselage"
    side: str = "LH"
    sta: Optional[float] = None
    stringer: Optional[str] = None
    diameter_mm: Optional[float] = None
    depth_mm: Optional[float] = None
    has_crack: bool = False
    raw_text: str = ""


def parse_simple_damage_text(text: str) -> DentDamageFallback:
    """
    Very lightweight parser: extracts aircraft family, side, STA, stringer, dent dia/depth, crack.
    """
    d = DentDamageFallback(raw_text=text or "")

    t = (text or "").strip()

    # aircraft
    m = re.search(r"\b(B7(?:37|47|57|67|77|87)|A3(?:19|20|21)|E1(?:70|75))\b", t, flags=re.IGNORECASE)
    if m:
        d.aircraft = m.group(1).upper()

    # side
    if re.search(r"\bLH\b|LEFT\s*HAND|LEFT\b", t, flags=re.IGNORECASE):
        d.side = "LH"
    elif re.search(r"\bRH\b|RIGHT\s*HAND|RIGHT\b", t, flags=re.IGNORECASE):
        d.side = "RH"
    else:
        d.side = "ANY"

    # STA
    m = re.search(r"\bSTA(?:TION)?\s*([0-9]+(?:\.[0-9]+)?)\b", t, flags=re.IGNORECASE)
    if m:
        try:
            d.sta = float(m.group(1))
        except Exception:
            d.sta = None

    # stringer like S-10L
    m = re.search(r"\bS[-\s]*([0-9]{1,2})\s*([LR])\b", t, flags=re.IGNORECASE)
    if m:
        d.stringer = f"S-{int(m.group(1))}{m.group(2).upper()}"

    # crack
    if re.search(r"\bno\s+visible\s+crack\b|\bno\s+crack\b", t, flags=re.IGNORECASE):
        d.has_crack = False
    elif re.search(r"\bcrack\b", t, flags=re.IGNORECASE):
        d.has_crack = True

    # dia/depth in mm
    m = re.search(r"\b(\d+(?:\.\d+)?)\s*mm\s*(?:dia|diameter)\b", t, flags=re.IGNORECASE)
    if m:
        d.diameter_mm = float(m.group(1))
    m = re.search(r"\b(\d+(?:\.\d+)?)\s*mm\s*(?:depth|deep)\b", t, flags=re.IGNORECASE)
    if m:
        d.depth_mm = float(m.group(1))

    return d


def assess_dent_fallback(d: DentDamageFallback) -> Dict[str, Any]:
    """
    Placeholder assessment: always advisory.
    """
    checks = []
    if d.has_crack:
        checks.append({"name": "Crack present", "ok": False, "detail": "Crack reported → engineering review."})
    else:
        checks.append({"name": "Crack present", "ok": True, "detail": "No visible crack reported."})

    # Very rough: if depth >= 3.175mm, elevate
    disposition = "ALLOWABLE" if (d.depth_mm is not None and d.depth_mm < 3.175 and not d.has_crack) else "ENGINEERING_REVIEW"
    severity = "LOW" if disposition == "ALLOWABLE" else "HIGH"

    if d.depth_mm is not None and d.depth_mm >= 3.175:
        checks.append({"name": "Depth vs 0.125 in", "ok": False, "detail": f"Depth {d.depth_mm:.3f} mm ≥ 3.175 mm."})
    elif d.depth_mm is not None:
        checks.append({"name": "Depth vs 0.125 in", "ok": True, "detail": f"Depth {d.depth_mm:.3f} mm < 3.175 mm."})
    else:
        checks.append({"name": "Depth provided", "ok": False, "detail": "No depth provided."})

    return {
        "disposition": disposition,
        "severity": severity,
        "srm_ref": "SRM: Repair assessment required (ref placeholder)",
        "rule_id": None,
        "reasoning": ["Prototype assessment. Verify against current SRM and operator procedures."],
        "checks": checks,
    }


# Try to use your real model if available
try:
    from damage_models import DentDamage, assess_dent, build_plain_text_summary  # type: ignore
    HAVE_DAMAGE_MODELS = True
except Exception:
    DentDamage = None  # type: ignore
    assess_dent = None  # type: ignore
    build_plain_text_summary = None  # type: ignore
    HAVE_DAMAGE_MODELS = False


# ----------------------------
# Rules evaluation (robust)
# ----------------------------

def rules_db_connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    return con


def get_latest_ruleset_id(con: sqlite3.Connection, family: str) -> Optional[int]:
    row = con.execute(
        """
        SELECT id
          FROM rule_sets
         WHERE aircraft_family = ?
         ORDER BY id DESC
         LIMIT 1
        """,
        (family,),
    ).fetchone()
    return int(row["id"]) if row else None


def match_rules_simple(con: sqlite3.Connection, family: str, dent: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Minimal matching to prove the plumbing:
    - Select enabled rules for latest ruleset
    - Basic filtering on damage_type/structure/side and STA/stringer range when available
    """
    rs_id = get_latest_ruleset_id(con, family)
    if rs_id is None:
        return []

    side = (dent.get("side") or "ANY").upper()
    sta = dent.get("sta")
    stringer = dent.get("stringer_minmax") or dent.get("stringer")  # tolerate either

    # We'll just pull candidates and filter in python for flexibility
    rows = con.execute(
        """
        SELECT id, priority, damage_type, structure, structure_zone, zone_detail,
               side, sta_min, sta_max, stringer_min, stringer_max,
               conditions_json, limits_json, actions_json,
               srm_ref, severity, notes, source_page
          FROM rules
         WHERE rule_set_id = ?
           AND enabled = 1
         ORDER BY priority DESC, id ASC
        """,
        (rs_id,),
    ).fetchall()

    def side_ok(rule_side: str) -> bool:
        rs = (rule_side or "ANY").upper()
        if rs in ("ANY", "*"):
            return True
        return rs == side

    def sta_ok(sta_min: Any, sta_max: Any) -> bool:
        if sta is None:
            return True
        lo = float(sta_min) if sta_min is not None else None
        hi = float(sta_max) if sta_max is not None else None
        if lo is not None and sta < lo:
            return False
        if hi is not None and sta > hi:
            return False
        return True

    matches: List[Dict[str, Any]] = []
    for r in rows:
        if not side_ok(r["side"]):
            continue
        if not sta_ok(r["sta_min"], r["sta_max"]):
            continue
        # (Stringer matching is complex; skip strict match for now)
        matches.append(dict(r))
        if len(matches) >= 10:
            break
    return matches


def evaluate_rules(dent_obj: Any, family: str) -> List[Dict[str, Any]]:
    """
    1) If rules_engine.py exposes something usable, call it.
    2) Otherwise, do a minimal rules.db match so the UI stays alive.
    """
    # Attempt to call your module if it exists
    try:
        import rules_engine  # type: ignore

        # Common possibilities we try (non-breaking):
        for fn_name in ("evaluate_rules", "run_rules", "evaluate", "evaluate_dent"):
            fn = getattr(rules_engine, fn_name, None)
            if callable(fn):
                try:
                    return fn(dent_obj)  # many engines just take dent object
                except TypeError:
                    # maybe needs db path or family
                    try:
                        return fn(dent_obj, str(RULES_DB_PATH))
                    except TypeError:
                        try:
                            return fn(dent_obj, family=family, db_path=str(RULES_DB_PATH))
                        except Exception:
                            pass
    except Exception:
        pass

    # Fallback: query rules.db directly
    if not RULES_DB_PATH.exists():
        return [{"error": "rules.db not found"}]

    try:
        with rules_db_connect(RULES_DB_PATH) as con:
            dent_dict = dent_obj if isinstance(dent_obj, dict) else (asdict(dent_obj) if hasattr(dent_obj, "__dict__") else {})
            return match_rules_simple(con, family, dent_dict)
    except Exception as e:
        return [{"error": f"rules fallback failed: {e}"}]


# ----------------------------
# SRM FTS search (direct, robust)
# ----------------------------

def srm_connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    return con


def search_srm_direct(
    con: sqlite3.Connection,
    aircraft_family: str,
    user_query: str,
    limit: int = 6,
) -> List[Dict[str, Any]]:
    """
    Direct search against FTS pages_fts (external content table pages).
    Returns: [{title, revision, aircraft_family, file_name, page_no, snippet, text}]
    """
    match_q = build_fts_match_query(user_query)
    if not match_q:
        return []

    # If your build wrote normalized text into pages.text, FTS should match.
    # Join by rowid (FTS rowid maps to pages.id if external content is set up that way).
    sql = """
    SELECT
      d.title AS title,
      d.revision AS revision,
      d.aircraft_family AS aircraft_family,
      d.file_name AS file_name,
      p.page_no AS page_no,
      snippet(pages_fts, 0, '[', ']', '…', 24) AS snip,
      p.text AS text
    FROM pages_fts
    JOIN pages p ON p.id = pages_fts.rowid
    JOIN docs d  ON d.id = p.doc_id
    WHERE pages_fts MATCH ?
      AND d.aircraft_family = ?
    ORDER BY rank
    LIMIT ?
    """
    rows = con.execute(sql, (match_q, aircraft_family, int(limit))).fetchall()

    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "title": r["title"],
                "revision": r["revision"],
                "aircraft_family": r["aircraft_family"],
                "file_name": r["file_name"],
                "page_no": r["page_no"],
                "snippet": r["snip"],
                "text": r["text"],
            }
        )
    return out


# ----------------------------
# Logging (auto-migrate)
# ----------------------------

ASSESSMENT_COLUMNS = [
    ("created_utc", "TEXT"),
    ("aircraft_family", "TEXT"),
    ("structure", "TEXT"),
    ("side", "TEXT"),
    ("sta", "REAL"),
    ("stringer", "TEXT"),
    ("diameter_mm", "REAL"),
    ("depth_mm", "REAL"),
    ("has_crack", "INTEGER"),
    ("disposition", "TEXT"),
    ("severity", "TEXT"),
    ("rule_id", "INTEGER"),
    ("srm_ref", "TEXT"),
    ("summary_text", "TEXT"),
    ("raw_input", "TEXT"),
    ("extra_json", "TEXT"),
]


def ensure_assessments_schema(db_path: Path) -> None:
    con = sqlite3.connect(str(db_path))
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS assessments (
              id INTEGER PRIMARY KEY AUTOINCREMENT
            )
            """
        )
        # existing cols
        cols = {r[1] for r in con.execute("PRAGMA table_info(assessments)").fetchall()}
        for name, typ in ASSESSMENT_COLUMNS:
            if name not in cols:
                con.execute(f"ALTER TABLE assessments ADD COLUMN {name} {typ}")
        con.commit()
    finally:
        con.close()


def log_assessment(
    dent: Dict[str, Any],
    result: Dict[str, Any],
    summary_text: str,
    raw_input: str,
) -> Tuple[bool, Optional[str]]:
    try:
        ensure_assessments_schema(ASSESSMENTS_DB_PATH)
        con = sqlite3.connect(str(ASSESSMENTS_DB_PATH))
        try:
            created_utc = datetime.now(timezone.utc).isoformat()

            payload = {
                "created_utc": created_utc,
                "aircraft_family": dent.get("aircraft_family") or dent.get("aircraft") or "UNKNOWN",
                "structure": dent.get("structure") or "UNKNOWN",
                "side": dent.get("side") or "ANY",
                "sta": dent.get("sta"),
                "stringer": dent.get("stringer"),
                "diameter_mm": dent.get("diameter_mm"),
                "depth_mm": dent.get("depth_mm"),
                "has_crack": 1 if dent.get("has_crack") else 0,
                "disposition": result.get("disposition"),
                "severity": result.get("severity"),
                "rule_id": result.get("rule_id"),
                "srm_ref": result.get("srm_ref"),
                "summary_text": summary_text,
                "raw_input": raw_input,
                "extra_json": json.dumps({"result": result}, ensure_ascii=False),
            }

            cols = ", ".join(payload.keys())
            qs = ", ".join(["?"] * len(payload))
            con.execute(f"INSERT INTO assessments ({cols}) VALUES ({qs})", tuple(payload.values()))
            con.commit()
            return True, None
        finally:
            con.close()
    except Exception as e:
        return False, str(e)


# ----------------------------
# Streamlit UI
# ----------------------------

st.set_page_config(page_title="SRM Damage Assessment Tool", layout="wide")

st.title("SRM Damage Assessment Tool")
st.caption("Advisory tool — verify against the current SRM and operator procedures.")

with st.expander("About (prototype disclaimer)", expanded=False):
    st.write(
        "Outputs are generated from prototype rules and/or search hits. "
        "Always confirm applicability, limits, and repair actions using the latest approved documentation."
    )

# Input
st.subheader("Damage description (quick entry for AOG)")
default_text = "B737, fuselage, LH side, STA 123, S-10L, skin dent 25mm dia, 3.18mm depth, no visible crack."
damage_text = st.text_area("Enter or paste damage description", value=default_text, height=120)

run = st.button("Run assessment", type="primary")

# Optional logging
log_to_db = st.checkbox("Log this assessment to SQLite (assessments.db)", value=True)

# Debug area: SRM db presence/hash
with st.expander("SRM DB Debug", expanded=False):
    st.write("cwd:", str(Path.cwd()))
    st.write("SRM DB path:", str(SRM_INDEX_DB_PATH))
    st.write("srm_index.db exists:", SRM_INDEX_DB_PATH.exists())
    if SRM_INDEX_DB_PATH.exists():
        st.write("srm_index.db size (bytes):", SRM_INDEX_DB_PATH.stat().st_size)
        sha = hashlib.sha256(SRM_INDEX_DB_PATH.read_bytes()).hexdigest()
        st.write("srm_index.db sha256 (prefix):", sha[:16])

    st.write("Rules DB path:", str(RULES_DB_PATH))
    st.write("rules.db exists:", RULES_DB_PATH.exists())
    if RULES_DB_PATH.exists():
        st.write("rules.db size (bytes):", RULES_DB_PATH.stat().st_size)

# Main run
if run:
    # Parse / model
    dent_obj: Any
    result: Dict[str, Any] = {}
    summary_text = ""

    if HAVE_DAMAGE_MODELS:
        try:
            # IMPORTANT: don't pass fields your model doesn't support.
            # We keep it minimal and let damage_models parse from text if it does so.
            # If your DentDamage expects different args, damage_models should own parsing.
            dent_obj = DentDamage.from_text(damage_text) if hasattr(DentDamage, "from_text") else None  # type: ignore
            if dent_obj is None:
                # fallback: attempt common constructor signature (without aircraft_family!)
                dent_obj = DentDamage(raw_text=damage_text)  # type: ignore
            result = assess_dent(dent_obj)  # type: ignore
            summary_text = build_plain_text_summary(dent_obj, result) if callable(build_plain_text_summary) else ""
        except Exception as e:
            dent_obj = parse_simple_damage_text(damage_text)
            result = assess_dent_fallback(dent_obj)
            summary_text = "Model import/exec error; used fallback logic."
            result = {**result, "error": f"damage_models error: {e}"}
    else:
        dent_obj = parse_simple_damage_text(damage_text)
        result = assess_dent_fallback(dent_obj)
        summary_text = "Fallback model (damage_models.py not available or failed to import)."

    # Make a dict for downstream usage
    if isinstance(dent_obj, DentDamageFallback):
        dent_dict = asdict(dent_obj)
        dent_dict["aircraft_family"] = dent_obj.aircraft
    elif isinstance(dent_obj, dict):
        dent_dict = dent_obj
        if "aircraft_family" not in dent_dict and "aircraft" in dent_dict:
            dent_dict["aircraft_family"] = dent_dict["aircraft"]
    else:
        # best-effort
        dent_dict = {}
        for k in ("aircraft_family", "aircraft", "structure", "side", "sta", "stringer", "diameter_mm", "depth_mm", "has_crack", "raw_text"):
            if hasattr(dent_obj, k):
                dent_dict[k] = getattr(dent_obj, k)
        if "aircraft_family" not in dent_dict and "aircraft" in dent_dict:
            dent_dict["aircraft_family"] = dent_dict["aircraft"]

    family = (dent_dict.get("aircraft_family") or "B737").upper()

    # Rules
    rules_matches = evaluate_rules(dent_dict, family)

    # SRM search
    srm_hits: List[Dict[str, Any]] = []
    srm_error: Optional[str] = None
    if not SRM_INDEX_DB_PATH.exists():
        srm_error = "SRM index not available in this deployment (no srm_index.db)."
    else:
        try:
            with srm_connect(SRM_INDEX_DB_PATH) as con:
                srm_query = f"{family} {dent_dict.get('structure','')} {dent_dict.get('side','')} {dent_dict.get('sta','')} {dent_dict.get('stringer','')} dent {dent_dict.get('diameter_mm','')}mm dia {dent_dict.get('depth_mm','')}mm depth {'crack' if dent_dict.get('has_crack') else 'no crack'} table 102 allowable damage repair"
                srm_hits = search_srm_direct(con, family, srm_query, limit=6)
        except Exception as e:
            srm_error = f"SRM search failed: {e}"

    # Output
    st.header("Outputs")

    st.subheader("Rule-based Assessment")
    st.write(f"**Disposition:** {result.get('disposition','ENGINEERING_REVIEW')}")
    st.write(f"**Severity:** {result.get('severity','engineering')}")
    st.write(f"**SRM Ref:** {result.get('srm_ref','(none)')}")
    st.write(f"**Rule ID:** {result.get('rule_id','(n/a)')}")

    st.markdown("### Reasoning")
    reasoning = result.get("reasoning") or []
    if isinstance(reasoning, list) and reasoning:
        for r in reasoning:
            st.write(f"- {r}")
    elif isinstance(reasoning, str) and reasoning.strip():
        st.write(f"- {reasoning}")
    else:
        st.write("- (No reasoning text)")

    st.markdown("### Checks")
    checks = result.get("checks") or []
    if isinstance(checks, list) and checks:
        for c in checks:
            ok = c.get("ok")
            icon = "✅" if ok else "⚠️"
            st.write(f"{icon} **{c.get('name','Check')}** — {c.get('detail','')}")
    else:
        st.write("(No checks listed)")

    # Debug blocks (helpful while plumbing is evolving)
    with st.expander("Dent model output (debug)", expanded=False):
        st.json(result if isinstance(result, dict) else {"result": str(result)})

    with st.expander("Rules matches (debug)", expanded=False):
        st.json(rules_matches)

    st.subheader("SRM search hits (prototype)")
    if srm_error:
        st.warning(srm_error)
    if not srm_hits and not srm_error:
        st.info("No SRM hits found for this query.")

    for h in srm_hits:
        title = f"{h['title']} (Rev: {h['revision']})"
        st.markdown(f"**{title}**")
        st.caption(f"Aircraft: {h['aircraft_family']} • File: {h['file_name']} • Page: {h['page_no']}")
        # Display a cleaned snippet/text so it resembles your local output more closely
        display_txt = deglue_text(h.get("text") or "")
        st.write(display_txt[:1200] + ("…" if len(display_txt) > 1200 else ""))
        st.divider()

    # Logging
    st.subheader("Logging")
    if log_to_db:
        ok, err = log_assessment(dent_dict, result, summary_text, damage_text)
        if ok:
            st.success("Assessment logged.")
        else:
            st.error(f"Failed to log assessment: {err}")
