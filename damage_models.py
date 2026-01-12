# dent_checker_app.py
"""
SRM Damage Assessment Tool (Prototype) — Fuselage Dent Checker

Restored features:
- AOG quick-entry: paste a free-text damage description and parse into fields
- Full structured entry UI (context + dent geometry + crack + notes)
- Rule-based assessment (uses damage_models.py)
- SQLite assessment logging (assessments.db) with history table + CSV export
- Optional SRM search (prototype) against srm_index.db (FTS), fails gracefully if missing
- Robust imports for different repo layouts (root/, app/, src/)

Notes:
- Advisory only. Always verify against the latest SRM and operator procedures.
"""

from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import streamlit as st

# -----------------------------
# Robust imports (repo layout)
# -----------------------------
# damage_models is required for assessment; support multiple layouts.
try:
    from damage_models import DentDamage, assess_dent, build_plain_text_summary
except ModuleNotFoundError:
    try:
        from app.damage_models import DentDamage, assess_dent, build_plain_text_summary
    except ModuleNotFoundError:
        from src.damage_models import DentDamage, assess_dent, build_plain_text_summary

# Optional SRM search helpers
try:
    from srm_search import guess_srm_db_path, open_srm_index, safe_search_srm
except Exception:
    guess_srm_db_path = None  # type: ignore
    open_srm_index = None  # type: ignore
    safe_search_srm = None  # type: ignore

# -----------------------------
# Constants / Paths
# -----------------------------
APP_DIR = Path(__file__).resolve().parent
DEFAULT_SRM_INDEX_DB = APP_DIR / "srm_index.db"
DEFAULT_ASSESSMENTS_DB = APP_DIR / "assessments.db"


# -----------------------------
# Utilities
# -----------------------------
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def coalesce(*vals: Any) -> Any:
    for v in vals:
        if v is not None and v != "":
            return v
    return None


def parse_damage_description(text: str) -> Dict[str, Any]:
    """
    Lightweight parser for AOG-style descriptions.

    Example:
      "B787, fuselage, LH side, STA 1280, S-10L, skin dent 25mm dia, 3mm depth, no visible crack."
    """
    t = (text or "").strip()
    out: Dict[str, Any] = {}

    # Aircraft (B787/B737/A320/E175)
    m = re.search(r"\b(B\s?7(?:87|37)|A\s?320|E\s?175)\b(?:[-\s]?\d+)?", t, re.I)
    if m:
        out["aircraft_type"] = m.group(0).upper().replace(" ", "")

    # Zone
    if re.search(r"\bfuselage\b", t, re.I):
        out["structure_zone"] = "Fuselage"
    elif re.search(r"\bwing\b", t, re.I):
        out["structure_zone"] = "Wing"
    elif re.search(r"\bempennage\b|\btail\b", t, re.I):
        out["structure_zone"] = "Empennage"

    # Side
    if re.search(r"\bLH\b|\bleft\b", t, re.I):
        out["side"] = "LH"
    elif re.search(r"\bRH\b|\bright\b", t, re.I):
        out["side"] = "RH"

    # STA
    m = re.search(r"\bSTA\s*([0-9]{2,5})\b", t, re.I)
    if m:
        out["sta"] = m.group(1)

    # Stringer like S-10L / S10L
    m = re.search(r"\bS[-\s]?(\d{1,2}[LR])\b", t, re.I)
    if m:
        out["stringer"] = f"S-{m.group(1).upper()}"

    # Diameter mm
    m = re.search(r"(\d+(?:\.\d+)?)\s*mm\s*(?:dia|diam|diameter)\b", t, re.I)
    if m:
        out["dent_diameter_mm"] = float(m.group(1))

    # Depth mm
    m = re.search(r"(\d+(?:\.\d+)?)\s*mm\s*depth\b", t, re.I)
    if m:
        out["dent_depth_mm"] = float(m.group(1))

    # Crack present
    if re.search(r"\bno\s+visible\s+crack\b|\bno\s+crack\b|\bwithout\s+crack\b", t, re.I):
        out["crack_present"] = False
    elif re.search(r"\bcrack\b|\bcracked\b", t, re.I):
        out["crack_present"] = True

    return out


def build_query_from_context(ctx: Dict[str, Any]) -> str:
    parts: List[str] = []
    fam = coalesce(ctx.get("aircraft_family"), ctx.get("aircraft_type"))
    if fam:
        parts.append(str(fam))

    for k in ("zone", "structure_zone", "side", "sta", "stringer"):
        v = ctx.get(k)
        if v:
            parts.append(str(v))

    parts.append("dent")

    d = ctx.get("dent_diameter_mm")
    dep = ctx.get("dent_depth_mm")
    if d is not None:
        parts.append(f"{float(d):g}mm dia")
    if dep is not None:
        parts.append(f"{float(dep):g}mm depth")

    crack = ctx.get("crack_present")
    if crack is True:
        parts.append("crack")
    elif crack is False:
        parts.append("no crack")

    parts += ["allowable", "damage", "limits", "repair"]
    return " ".join(parts)


def open_sqlite(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


# -----------------------------
# Assessment logging (SQLite)
# -----------------------------
def ensure_assessments_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS assessments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_utc TEXT NOT NULL,
            aircraft_type TEXT,
            structure_zone TEXT,
            side TEXT,
            sta TEXT,
            stringer TEXT,
            dent_diameter_mm REAL,
            dent_depth_mm REAL,
            crack_present INTEGER,
            notes TEXT,
            raw_description TEXT,

            disposition TEXT,
            severity TEXT,
            srm_reference TEXT,
            rule_id INTEGER,
            within_limits INTEGER,

            summary_text TEXT,
            reasoning_json TEXT
        );
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_assessments_created ON assessments(created_utc);")
    conn.commit()


def log_assessment(conn: sqlite3.Connection, dent: DentDamage, result: Any, summary_text: str, raw_description: Optional[str]) -> None:
    ensure_assessments_schema(conn)

    disposition = getattr(result, "disposition", None)
    severity = getattr(result, "severity", None)
    srm_reference = getattr(result, "srm_reference", None)
    rule_id = getattr(result, "rule_id", None)
    within_limits = getattr(result, "within_limits", None)

    reasoning = getattr(result, "reasoning", None)
    checks = getattr(result, "checks", None)
    payload = {"reasoning": reasoning, "checks": []}
    if checks:
        for c in checks:
            payload["checks"].append(
                {
                    "name": getattr(c, "name", None),
                    "passed": getattr(c, "passed", None),
                    "message": getattr(c, "message", None),
                }
            )

    conn.execute(
        """
        INSERT INTO assessments (
            created_utc,
            aircraft_type, structure_zone, side, sta, stringer,
            dent_diameter_mm, dent_depth_mm, crack_present,
            notes, raw_description,
            disposition, severity, srm_reference, rule_id, within_limits,
            summary_text, reasoning_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            utc_now_iso(),
            dent.aircraft_type,
            dent.structure_zone,
            dent.side,
            dent.sta,
            dent.stringer,
            dent.dent_diameter_mm,
            dent.dent_depth_mm,
            1 if dent.crack_present else 0,
            dent.notes,
            raw_description,
            disposition,
            severity,
            srm_reference,
            rule_id,
            1 if within_limits else 0 if within_limits is not None else None,
            summary_text,
            json.dumps(payload, default=str),
        ),
    )
    conn.commit()


def fetch_recent_assessments(conn: sqlite3.Connection, limit: int) -> List[sqlite3.Row]:
    ensure_assessments_schema(conn)
    return conn.execute(
        """
        SELECT *
        FROM assessments
        ORDER BY datetime(created_utc) DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def export_rows_to_csv(rows: List[sqlite3.Row]) -> str:
    if not rows:
        return ""
    buf = StringIO()
    writer = csv.writer(buf)
    cols = list(rows[0].keys())
    writer.writerow(cols)
    for r in rows:
        writer.writerow([r[c] for c in cols])
    return buf.getvalue()


# -----------------------------
# SRM DB connection
# -----------------------------
def get_srm_connection() -> Optional[sqlite3.Connection]:
    env_path = os.getenv("SRM_INDEX_DB", "").strip()
    if env_path:
        p = Path(env_path)
        if p.exists():
            try:
                if open_srm_index:
                    return open_srm_index(str(p))
                return open_sqlite(p)
            except Exception:
                return None

    if DEFAULT_SRM_INDEX_DB.exists():
        try:
            if open_srm_index:
                return open_srm_index(str(DEFAULT_SRM_INDEX_DB))
            return open_sqlite(DEFAULT_SRM_INDEX_DB)
        except Exception:
            return None

    if guess_srm_db_path:
        guessed = guess_srm_db_path()
        if guessed and Path(guessed).exists():
            try:
                if open_srm_index:
                    return open_srm_index(guessed)
                return open_sqlite(Path(guessed))
            except Exception:
                return None

    return None


# -----------------------------
# UI
# -----------------------------
st.set_page_config(page_title="SRM Damage Assessment (Prototype)", layout="wide")

st.title("SRM Damage Assessment Tool")
st.caption("Prototype • Advisory use only — verify against the current SRM and operator procedures.")

with st.expander("Advisory tool — verify against the current SRM and operator procedures.", expanded=True):
    st.write(
        "Outputs are generated from prototype rules and user-provided inputs. "
        "Always confirm applicability, limits, and repair actions using the latest approved documentation."
    )

with st.sidebar:
    st.header("Settings")
    enable_logging = st.checkbox("Log assessments to SQLite", value=True)
    show_history = st.checkbox("Show recent assessment history", value=True)
    enable_srm_search = st.checkbox("Enable SRM search (prototype)", value=True)
    history_limit = st.slider("History rows", min_value=5, max_value=100, value=25, step=5)

left, right = st.columns([0.48, 0.52], gap="large")

with left:
    st.subheader("Damage description (quick entry for AOG)")
    raw_description = st.text_area(
        "Enter or paste damage description",
        key="raw_description",
        placeholder='e.g. "B787, fuselage, LH side, STA 1280, S-10L, skin dent 25mm dia, 3mm depth, no visible crack."',
        height=110,
    )

    c1, c2 = st.columns([1, 1])
    with c1:
        if st.button("Parse description into fields", use_container_width=True):
            parsed = parse_damage_description(raw_description)
            for k, v in parsed.items():
                st.session_state[k] = v
            st.success("Parsed. Review/adjust fields below.")
    with c2:
        if st.button("Clear fields", use_container_width=True):
            for k in [
                "aircraft_type",
                "structure_zone",
                "side",
                "sta",
                "stringer",
                "dent_diameter_mm",
                "dent_depth_mm",
                "crack_present",
                "notes",
            ]:
                st.session_state.pop(k, None)
            st.success("Cleared.")

    st.divider()
    st.subheader("Context")

    aircraft_type = st.text_input("Aircraft type / family", key="aircraft_type", placeholder="B787 / B737 / A320 / E175")
    structure_zone = st.text_input("Structure zone", key="structure_zone", placeholder="Fuselage")
    side = st.selectbox("Side", ["", "LH", "RH"], index=["", "LH", "RH"].index(st.session_state.get("side", "") if st.session_state.get("side", "") in ["", "LH", "RH"] else ""), key="side")
    sta = st.text_input("STA (optional)", key="sta", placeholder="1280")
    stringer = st.text_input("Stringer (optional)", key="stringer", placeholder="S-10L")

    st.subheader("Dent details")
    cc1, cc2, cc3 = st.columns(3)
    with cc1:
        dent_diameter_mm = st.number_input("Dent diameter (mm)", min_value=0.0, value=float(st.session_state.get("dent_diameter_mm") or 0.0), key="dent_diameter_mm")
    with cc2:
        dent_depth_mm = st.number_input("Dent depth (mm)", min_value=0.0, value=float(st.session_state.get("dent_depth_mm") or 0.0), key="dent_depth_mm")
    with cc3:
        crack_present = st.checkbox("Crack present", value=bool(st.session_state.get("crack_present") or False), key="crack_present")

    notes = st.text_area("Notes (optional)", key="notes", height=90)

    st.divider()
    run = st.button("Run assessment", type="primary", use_container_width=True)

with right:
    st.subheader("Outputs")

    if run:
        dent = DentDamage(
            aircraft_type=(aircraft_type or "").strip() or "UNKNOWN",
            structure_zone=(structure_zone or "").strip() or "UNKNOWN",
            side=(side or "").strip() or "UNKNOWN",
            sta=(sta or "").strip() or None,
            stringer=(stringer or "").strip() or None,
            dent_diameter_mm=float(dent_diameter_mm),
            dent_depth_mm=float(dent_depth_mm),
            crack_present=bool(crack_present),
            notes=(notes or "").strip() or None,
        )

        result = assess_dent(dent)
        summary_text = build_plain_text_summary(dent, result)

        st.markdown("### Rule-based Assessment")
        st.write(f"**Disposition:** {getattr(result, 'disposition', '')}")
        st.write(f"**Severity:** {getattr(result, 'severity', '')}")
        if getattr(result, "srm_reference", None):
            st.write(f"**SRM Ref:** {result.srm_reference}")
        if getattr(result, "rule_id", None) is not None:
            st.write(f"**Rule ID:** {result.rule_id}")

        st.markdown("### Reasoning")
        reasoning = getattr(result, "reasoning", None) or []
        if reasoning:
            for r in reasoning:
                st.write(f"- {r}")
        else:
            st.write("- (No reasoning provided)")

        st.markdown("### Checks")
        checks = getattr(result, "checks", None) or []
        if checks:
            for c in checks:
                passed = getattr(c, "passed", None)
                name = getattr(c, "name", "check")
                msg = getattr(c, "message", "")
                if passed is True:
                    st.write(f"✅ **{name}** — {msg}")
                elif passed is False:
                    st.write(f"⚠️ **{name}** — {msg}")
                else:
                    st.write(f"• **{name}** — {msg}")
        else:
            st.write("- (No checks)")

        st.markdown("### Summary (copy/paste)")
        st.code(summary_text, language="markdown")

        if enable_logging:
            try:
                aconn = open_sqlite(DEFAULT_ASSESSMENTS_DB)
                log_assessment(
                    aconn,
                    dent=dent,
                    result=result,
                    summary_text=summary_text,
                    raw_description=(raw_description or "").strip() or None,
                )
                st.success("✅ Assessment logged to SQLite.")
            except Exception as e:
                st.warning("Assessment logging failed.")
                st.caption(str(e))

        if enable_srm_search:
            st.divider()
            st.markdown("### SRM search (prototype)")

            # ✅ ctx is defined here (fixes your NameError)
            ctx: Dict[str, Any] = {
                "aircraft_family": (aircraft_type or "").strip() or None,
                "aircraft_type": (aircraft_type or "").strip() or None,
                "structure_zone": (structure_zone or "").strip() or None,
                "zone": (structure_zone or "").strip() or None,
                "side": (side or "").strip() or None,
                "sta": (sta or "").strip() or None,
                "stringer": (stringer or "").strip() or None,
                "dent_diameter_mm": float(dent_diameter_mm),
                "dent_depth_mm": float(dent_depth_mm),
                "crack_present": bool(crack_present),
                "notes": (notes or "").strip() or None,
                "raw_description": (raw_description or "").strip() or None,
            }

            srm_query = build_query_from_context(ctx)
            st.caption(f"Query: `{srm_query}`")

            srm_conn = get_srm_connection()
            if not srm_conn or not safe_search_srm:
                st.info("SRM index not available in this deployment (missing srm_index.db or srm_search.py).")
            else:
                hits, err = safe_search_srm(
                    srm_conn,
                    srm_query,
                    aircraft_family=ctx.get("aircraft_family"),
                    limit=6,
                )
                if err:
                    st.warning("SRM search failed (index not ready or DB error).")
                    st.caption(err)
                elif not hits:
                    st.write("No SRM hits found for this query.")
                else:
                    for h in hits:
                        title = f"{h.get('aircraft_family','')} rev {h.get('revision','')} — {h.get('file_name','')} — page {h.get('page_no','')}"
                        st.markdown(f"**{title}**")
                        st.write(h.get("snippet", ""))

    else:
        st.info("Enter details on the left, then click **Run assessment**.")


if show_history:
    st.divider()
    st.subheader("Recent assessments (SQLite)")
    try:
        conn = open_sqlite(DEFAULT_ASSESSMENTS_DB)
        rows = fetch_recent_assessments(conn, limit=history_limit)
        if not rows:
            st.caption("No logged assessments yet.")
        else:
            table_rows = []
            for r in rows:
                table_rows.append(
                    {
                        "UTC": r["created_utc"],
                        "Aircraft": r["aircraft_type"],
                        "Zone": r["structure_zone"],
                        "Side": r["side"],
                        "STA": r["sta"],
                        "Dia(mm)": r["dent_diameter_mm"],
                        "Depth(mm)": r["dent_depth_mm"],
                        "Crack": bool(r["crack_present"]) if r["crack_present"] is not None else None,
                        "Disposition": r["disposition"],
                        "SRM Ref": r["srm_reference"],
                    }
                )
            st.dataframe(table_rows, use_container_width=True, hide_index=True)

            csv_text = export_rows_to_csv(rows)
            st.download_button(
                "Download history CSV",
                data=csv_text.encode("utf-8"),
                file_name="assessments_history.csv",
                mime="text/csv",
            )
    except Exception as e:
        st.warning("History unavailable (DB error).")
        st.caption(str(e))

st.divider()
st.caption(
    "Disclaimer: Prototype tool. Outputs are advisory only. "
    "Always verify against the latest SRM and approved operator procedures."
)
