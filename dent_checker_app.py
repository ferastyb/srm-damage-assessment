import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pandas as pd
import streamlit as st

# Uses your rules engine module
from rules_engine import assess_damage


APP_TITLE = "Fuselage Dent Checker (Prototype)"
DB_FILE = "assessments.db"
RULES_DB = "rules.db"


# ---------------------------
# SQLite logging
# ---------------------------
def get_conn(db_path: str = DB_FILE) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def ensure_assessments_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS assessments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_utc TEXT NOT NULL,

            aircraft_family TEXT,
            aircraft_variant TEXT,

            zone TEXT,
            side TEXT,
            sta INTEGER,
            wl INTEGER,
            stringer_num INTEGER,
            pressurized INTEGER,

            damage_type TEXT,
            structure TEXT,

            diameter_mm REAL,
            depth_mm REAL,
            thickness_mm REAL,
            depth_to_thickness_ratio REAL,
            visible_crack INTEGER,
            near_fastener_row INTEGER,

            disposition TEXT,
            severity TEXT,
            rule_id INTEGER,
            srm_ref TEXT,
            reasons TEXT,

            raw_description TEXT,
            ctx_json TEXT
        )
        """
    )
    conn.commit()


def log_assessment(conn: sqlite3.Connection, payload: Dict[str, Any]) -> None:
    ensure_assessments_table(conn)

    conn.execute(
        """
        INSERT INTO assessments (
            created_utc,
            aircraft_family, aircraft_variant,
            zone, side, sta, wl, stringer_num, pressurized,
            damage_type, structure,
            diameter_mm, depth_mm, thickness_mm, depth_to_thickness_ratio,
            visible_crack, near_fastener_row,
            disposition, severity, rule_id, srm_ref, reasons,
            raw_description, ctx_json
        ) VALUES (
            :created_utc,
            :aircraft_family, :aircraft_variant,
            :zone, :side, :sta, :wl, :stringer_num, :pressurized,
            :damage_type, :structure,
            :diameter_mm, :depth_mm, :thickness_mm, :depth_to_thickness_ratio,
            :visible_crack, :near_fastener_row,
            :disposition, :severity, :rule_id, :srm_ref, :reasons,
            :raw_description, :ctx_json
        )
        """,
        payload,
    )
    conn.commit()


def fetch_recent_logs(conn: sqlite3.Connection, limit: int = 50) -> pd.DataFrame:
    ensure_assessments_table(conn)
    df = pd.read_sql_query(
        f"""
        SELECT
            id, created_utc,
            aircraft_family, aircraft_variant,
            zone, side, sta, stringer_num,
            diameter_mm, depth_mm, visible_crack,
            disposition, severity, rule_id, srm_ref
        FROM assessments
        ORDER BY id DESC
        LIMIT {int(limit)}
        """,
        conn,
    )
    return df


# ---------------------------
# Parsing helpers (AOG text -> fields)
# ---------------------------
def _parse_int(s: str) -> Optional[int]:
    try:
        return int(s)
    except Exception:
        return None


def _parse_float(s: str) -> Optional[float]:
    try:
        return float(s)
    except Exception:
        return None


def parse_damage_description(text: str) -> Dict[str, Any]:
    """
    Example:
      “B787, fuselage, LH side, STA 1280, S-10L, skin dent 25mm dia, 3mm depth, no visible crack.”
    """
    t = (text or "").strip()
    out: Dict[str, Any] = {}

    # Aircraft family/variant
    # B787 or 787-8 etc
    m = re.search(r"\b(B?\s*7\s*8\s*7(?:-\s*\d+)?)\b", t, flags=re.IGNORECASE)
    if m:
        fam = m.group(1).replace(" ", "").upper()
        out["aircraft_family"] = "B787"
        out["aircraft_variant"] = fam.replace("B", "")

    # Zone
    if re.search(r"\bfuselage\b", t, flags=re.IGNORECASE):
        out["zone"] = "fuselage"

    # Side (LH/RH)
    if re.search(r"\bLH\b|\bleft\b", t, flags=re.IGNORECASE):
        out["side"] = "LH"
    elif re.search(r"\bRH\b|\bright\b", t, flags=re.IGNORECASE):
        out["side"] = "RH"

    # STA
    m = re.search(r"\bSTA\s*(\d+)\b", t, flags=re.IGNORECASE)
    if m:
        out["sta"] = _parse_int(m.group(1))

    # WL
    m = re.search(r"\bWL\s*(\d+)\b", t, flags=re.IGNORECASE)
    if m:
        out["wl"] = _parse_int(m.group(1))

    # Stringer like S-10L / S-10 / S10
    m = re.search(r"\bS[- ]?(\d+)", t, flags=re.IGNORECASE)
    if m:
        out["stringer_num"] = _parse_int(m.group(1))

    # Diameter mm (dia)
    m = re.search(r"(\d+(?:\.\d+)?)\s*mm\s*(?:dia|diam|diameter)\b", t, flags=re.IGNORECASE)
    if m:
        out["diameter_mm"] = _parse_float(m.group(1))

    # Depth mm
    m = re.search(r"(\d+(?:\.\d+)?)\s*mm\s*depth\b", t, flags=re.IGNORECASE)
    if m:
        out["depth_mm"] = _parse_float(m.group(1))

    # Visible crack presence
    if re.search(r"\bno\s+visible\s+crack\b|\bno\s+crack\b", t, flags=re.IGNORECASE):
        out["visible_crack"] = False
    elif re.search(r"\bvisible\s+crack\b|\bcrack\s+present\b", t, flags=re.IGNORECASE):
        out["visible_crack"] = True

    # Damage type / structure
    if re.search(r"\bdent\b", t, flags=re.IGNORECASE):
        out["damage_type"] = "dent"
    if re.search(r"\bskin\b", t, flags=re.IGNORECASE):
        out["structure"] = "skin"

    return out


# ---------------------------
# UI
# ---------------------------
st.set_page_config(page_title="SRM Damage Assessment", layout="wide")
st.title(APP_TITLE)
st.caption("Advisory only — verify against the current SRM and operator procedures.")

# File presence checks
rules_db_exists = Path(RULES_DB).exists()
st.sidebar.success(f"{RULES_DB} present: {rules_db_exists}")
st.sidebar.caption("If FALSE: commit/push rules.db into the repo.")

# Defaults
def ss_setdefault(k: str, v: Any) -> None:
    if k not in st.session_state:
        st.session_state[k] = v


ss_setdefault("aircraft_family", "B787")
ss_setdefault("aircraft_variant", "787-8")
ss_setdefault("zone", "fuselage")
ss_setdefault("side", "LH")
ss_setdefault("pressurized", True)
ss_setdefault("sta", 1280)
ss_setdefault("wl", None)
ss_setdefault("stringer_num", 10)
ss_setdefault("damage_type", "dent")
ss_setdefault("structure", "skin")
ss_setdefault("diameter_mm", 25.0)
ss_setdefault("depth_mm", 3.0)
ss_setdefault("thickness_mm", None)
ss_setdefault("visible_crack", False)
ss_setdefault("near_fastener_row", False)
ss_setdefault("damage_description", "")

st.subheader("Damage description (quick entry for AOG)")
st.write("Enter or paste a free-text description; click **Parse** to auto-fill the fields below.")

colA, colB = st.columns([3, 1], gap="large")
with colA:
    st.text_area(
        "Enter or paste damage description",
        key="damage_description",
        height=110,
        placeholder='e.g. "B787, fuselage, LH side, STA 1280, S-10L, skin dent 25mm dia, 3mm depth, no visible crack."',
    )
with colB:
    if st.button("Parse description into fields", use_container_width=True):
        parsed = parse_damage_description(st.session_state["damage_description"])
        for k, v in parsed.items():
            st.session_state[k] = v
        st.success("Parsed and filled available fields.")

st.divider()

left, right = st.columns([1, 1], gap="large")

with left:
    st.markdown("### Context")
    st.text_input("Aircraft family", key="aircraft_family")
    st.text_input("Aircraft variant", key="aircraft_variant")

    st.selectbox("Structure zone", options=["fuselage", "wing", "empennage", "other"], key="zone")
    st.selectbox("Side", options=["LH", "RH", "ANY"], key="side")
    st.checkbox("Pressurized", key="pressurized")

    c1, c2, c3 = st.columns(3)
    with c1:
        st.number_input("STA", min_value=0, max_value=99999, step=1, key="sta")
    with c2:
        st.number_input("WL (optional)", min_value=-99999, max_value=99999, step=1, key="wl")
    with c3:
        st.number_input("Stringer # (optional)", min_value=0, max_value=999, step=1, key="stringer_num")

with right:
    st.markdown("### Damage")
    st.selectbox("Damage type", options=["dent", "scratch", "gouge", "crack", "corrosion", "other"], key="damage_type")
    st.selectbox("Structure", options=["skin", "stringer", "frame", "doubler", "other"], key="structure")

    c1, c2, c3 = st.columns(3)
    with c1:
        st.number_input("Dent diameter (mm)", min_value=0.0, step=0.5, key="diameter_mm")
    with c2:
        st.number_input("Dent depth (mm)", min_value=0.0, step=0.1, key="depth_mm")
    with c3:
        st.number_input("Skin thickness (mm) (optional)", min_value=0.0, step=0.1, key="thickness_mm")

    st.checkbox("Visible crack", key="visible_crack")
    st.checkbox("Near fastener row", key="near_fastener_row")

st.divider()

st.subheader("Rule-based Assessment")

with st.form("assessment_form", clear_on_submit=False):
    submitted = st.form_submit_button("Run assessment", use_container_width=True)

if submitted:
    thickness = st.session_state.get("thickness_mm")
    depth = st.session_state.get("depth_mm")
    ratio = None
    if thickness and thickness > 0 and depth is not None:
        ratio = float(depth) / float(thickness)

    ctx = {
        "location": {
            "zone": st.session_state.get("zone"),
            "side": st.session_state.get("side"),
            "sta": st.session_state.get("sta"),
            "wl": st.session_state.get("wl"),
            "stringer_num": st.session_state.get("stringer_num"),
            "pressurized": bool(st.session_state.get("pressurized", True)),
        },
        "damage": {
            "type": st.session_state.get("damage_type"),
            "structure": st.session_state.get("structure"),
            "diameter_mm": st.session_state.get("diameter_mm"),
            "depth_mm": st.session_state.get("depth_mm"),
            "thickness_mm": thickness,
            "depth_to_thickness_ratio": ratio,
            "visible_crack": bool(st.session_state.get("visible_crack", False)),
            "near_fastener_row": bool(st.session_state.get("near_fastener_row", False)),
        },
    }

    try:
        result = assess_damage(
            db_path=RULES_DB,
            aircraft_family=st.session_state.get("aircraft_family", "B787"),
            ctx=ctx,
            revision=None,  # or set "DEMO-01" if you want to pin it
        )

        st.write(f"**Disposition:** {result.disposition}")
        st.write(f"**Severity:** {result.severity}")
        if result.srm_ref:
            st.write(f"**SRM Ref:** {result.srm_ref}")
        if result.rule_id is not None:
            st.write(f"**Rule ID:** {result.rule_id}")

        st.markdown("### Reasoning")
        if result.reasons:
            for r in result.reasons:
                st.write(f"- {r}")
        else:
            st.write("- (no reasons returned)")

        # Log into SQLite (NEW, fixed, no old variables)
        conn = get_conn(DB_FILE)
        payload = {
            "created_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",

            "aircraft_family": st.session_state.get("aircraft_family"),
            "aircraft_variant": st.session_state.get("aircraft_variant"),

            "zone": st.session_state.get("zone"),
            "side": st.session_state.get("side"),
            "sta": st.session_state.get("sta"),
            "wl": st.session_state.get("wl"),
            "stringer_num": st.session_state.get("stringer_num"),
            "pressurized": 1 if st.session_state.get("pressurized", True) else 0,

            "damage_type": st.session_state.get("damage_type"),
            "structure": st.session_state.get("structure"),

            "diameter_mm": st.session_state.get("diameter_mm"),
            "depth_mm": st.session_state.get("depth_mm"),
            "thickness_mm": thickness,
            "depth_to_thickness_ratio": ratio,
            "visible_crack": 1 if st.session_state.get("visible_crack", False) else 0,
            "near_fastener_row": 1 if st.session_state.get("near_fastener_row", False) else 0,

            "disposition": result.disposition,
            "severity": result.severity,
            "rule_id": result.rule_id,
            "srm_ref": result.srm_ref,
            "reasons": "\n".join(result.reasons or []),

            "raw_description": st.session_state.get("damage_description", ""),
            "ctx_json": json.dumps(ctx, ensure_ascii=False),
        }
        log_assessment(conn, payload)
        conn.close()

        st.success("Assessment logged to SQLite.")

        with st.expander("Debug: Context sent to rules engine"):
            st.code(json.dumps(ctx, indent=2), language="json")

    except Exception as e:
        st.error("Assessment failed. Check logs for details.")
        st.exception(e)

st.divider()

st.subheader("Assessment log (SQLite)")
conn = get_conn(DB_FILE)
df = fetch_recent_logs(conn, limit=50)
conn.close()

if df.empty:
    st.info("No assessments logged yet.")
else:
    st.dataframe(df, use_container_width=True)

    csv = df.to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download recent log CSV",
        data=csv,
        file_name="assessments_recent.csv",
        mime="text/csv",
        use_container_width=True,
    )

st.caption("Note: On Streamlit Cloud, local SQLite files may not persist across rebuilds/redeploys unless you add external storage.")
