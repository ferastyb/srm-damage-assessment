import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
from srm_search import connect_index, search_srm, build_query_from_context


import pandas as pd
import streamlit as st

from rules_engine import assess_damage


APP_TITLE = "Structural Damage Checker (Prototype)"
DB_FILE = "assessments.db"
RULES_DB = "rules.db"

DAMAGE_TYPES = [
    "dent",
    "nick",
    "gouge",
    "crack",
    "corrosion",
    "chafing",
    "scratch"
    "paint scratch",
    "lightning strike",
    "other",
]


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
    return pd.read_sql_query(
        f"""
        SELECT
            id, created_utc,
            aircraft_family, aircraft_variant,
            zone, side, sta, stringer_num,
            damage_type, structure,
            diameter_mm, depth_mm, visible_crack,
            disposition, severity, rule_id, srm_ref
        FROM assessments
        ORDER BY id DESC
        LIMIT {int(limit)}
        """,
        conn,
    )


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


def _has_no_crack_phrase(t: str) -> bool:
    # Covers: "no crack", "no visible crack", "no cracks observed", etc.
    return bool(
        re.search(
            r"\bno\s+(?:visible\s+)?crack(?:s)?\b|\bwithout\s+(?:visible\s+)?crack(?:s)?\b",
            t,
            flags=re.IGNORECASE,
        )
    )


def parse_damage_description(text: str) -> Dict[str, Any]:
    """
    Example:
      “B787, fuselage, LH side, STA 1280, S-10L, skin gouge 25mm dia, 3mm depth, no visible crack.”
    """
    t = (text or "").strip()
    out: Dict[str, Any] = {}

    # Normalize for some detection
    t_norm = t.replace(" ", "").upper()

    # -----------------
    # Aircraft family/variant
    # Boeing: 737/747/757/767/777/787 with optional -xxx
    m = re.search(r"\bB?(7(3[0-9]|4[0-9]|5[0-9]|6[0-9]|7[0-9]|8[0-9]))(?:-?(\d{1,4}))?\b", t_norm)
    if m:
        family_num = m.group(1)  # e.g. "787" or "767"
        variant = m.group(3)     # e.g. "8" or "300" or None
        out["aircraft_family"] = f"B{family_num}"
        out["aircraft_variant"] = f"{family_num}-{variant}" if variant else family_num

    # Airbus (optional)
    m = re.search(r"\bA(3(18|19|20|21|30|40|50|80))(?:-?(\d{1,4}))?\b", t_norm)
    if m:
        family_num = m.group(1)  # e.g. "320"
        variant = m.group(3)
        out["aircraft_family"] = f"A{family_num}"
        out["aircraft_variant"] = f"{family_num}-{variant}" if variant else family_num

    # -----------------
    # Zone / location
    if re.search(r"\bfuselage\b", t, flags=re.IGNORECASE):
        out["zone"] = "fuselage"

    if re.search(r"\bLH\b|\bleft\b", t, flags=re.IGNORECASE):
        out["side"] = "LH"
    elif re.search(r"\bRH\b|\bright\b", t, flags=re.IGNORECASE):
        out["side"] = "RH"

    m = re.search(r"\bSTA\s*(\d+)\b", t, flags=re.IGNORECASE)
    if m:
        out["sta"] = _parse_int(m.group(1))

    m = re.search(r"\bWL\s*(\d+)\b", t, flags=re.IGNORECASE)
    if m:
        out["wl"] = _parse_int(m.group(1))

    # Stringer like S-10L / S-10 / S10
    m = re.search(r"\bS[- ]?(\d+)", t, flags=re.IGNORECASE)
    if m:
        out["stringer_num"] = _parse_int(m.group(1))

    # -----------------
    # Structure
    if re.search(r"\bskin\b", t, flags=re.IGNORECASE):
        out["structure"] = "skin"
    elif re.search(r"\bstringer\b", t, flags=re.IGNORECASE):
        out["structure"] = "stringer"
    elif re.search(r"\bframe\b", t, flags=re.IGNORECASE):
        out["structure"] = "frame"
    elif re.search(r"\bdoubler\b", t, flags=re.IGNORECASE):
        out["structure"] = "doubler"

    # -----------------
    # Diameter / depth
    m = re.search(r"(\d+(?:\.\d+)?)\s*mm\s*(?:dia|diam|diameter)\b", t, flags=re.IGNORECASE)
    if m:
        out["diameter_mm"] = _parse_float(m.group(1))

    m = re.search(r"(\d+(?:\.\d+)?)\s*mm\s*depth\b", t, flags=re.IGNORECASE)
    if m:
        out["depth_mm"] = _parse_float(m.group(1))

    # -----------------
    # Crack presence (separate from damage_type)
    if _has_no_crack_phrase(t):
        out["visible_crack"] = False
    elif re.search(r"\bvisible\s+crack\b|\bcrack\s+present\b|\bcracked\b", t, flags=re.IGNORECASE):
        out["visible_crack"] = True

    # -----------------
    # Damage type detection (IMPORTANT: multiword & higher-priority first)
    # Default: do not overwrite unless we matched something explicitly
    damage_type: Optional[str] = None

    # paint scratch (multiword)
    if re.search(r"\bpaint\b.*\bscratch\b|\bscratch\b.*\bpaint\b|\bpaint[- ]scratch\b", t, flags=re.IGNORECASE):
        damage_type = "paint scratch"
# plain scratch
    elif re.search(r"\bscratch\b", t, flags=re.IGNORECASE):
        damage_type = "scratch"

    elif re.search(r"\bchaf(?:e|ing)\b", t, flags=re.IGNORECASE):
        damage_type = "chafing"
    elif re.search(r"\blightning\b.*\bstrike\b|\blightning\b.*\bstrike\b|\blightning[- ]strike\b", t, flags=re.IGNORECASE):
        damage_type = "lightning strike"
    elif re.search(r"\bcorros(?:ion|ive|ed)\b", t, flags=re.IGNORECASE):
        damage_type = "corrosion"
    elif re.search(r"\bgouge\b", t, flags=re.IGNORECASE):
        damage_type = "gouge"
    elif re.search(r"\bnick\b", t, flags=re.IGNORECASE):
        damage_type = "nick"
    elif re.search(r"\bdent\b", t, flags=re.IGNORECASE):
        damage_type = "dent"
    elif re.search(r"\bcrack\b|\bcracked\b", t, flags=re.IGNORECASE):
        # Only classify as crack if NOT explicitly "no crack"
        if not _has_no_crack_phrase(t):
            damage_type = "crack"

    if damage_type is not None:
        out["damage_type"] = damage_type
        # If the description calls it a crack (and not "no crack"), visible_crack should be True
        if damage_type == "crack" and not _has_no_crack_phrase(t):
            out["visible_crack"] = True

    return out


# ---------------------------
# UI
# ---------------------------
st.set_page_config(page_title="SRM Structural Damage Assessment", layout="wide")
st.title(APP_TITLE)
st.caption("Advisory only — verify against the current SRM and operator procedures.")

rules_db_exists = Path(RULES_DB).exists()
st.sidebar.success(f"{RULES_DB} present: {rules_db_exists}")
st.sidebar.caption("If FALSE: commit/push rules.db into the repo.")


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
        placeholder='e.g. "B787, fuselage, LH side, STA 1280, S-10L, skin gouge 25mm dia, 3mm depth, no visible crack."',
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
    st.selectbox("Damage type", options=DAMAGE_TYPES, key="damage_type")
    st.selectbox("Structure", options=["skin", "stringer", "frame", "doubler", "other"], key="structure")

    c1, c2, c3 = st.columns(3)
    with c1:
        st.number_input("Characteristic diameter (mm)", min_value=0.0, step=0.5, key="diameter_mm")
    with c2:
        st.number_input("Depth (mm)", min_value=0.0, step=0.1, key="depth_mm")
    with c3:
        st.number_input("Thickness (mm) (optional)", min_value=0.0, step=0.1, key="thickness_mm")

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
    # --- SRM Search (FTS) ---
try:
    srm_db_exists = Path("srm_index.db").exists()
    if srm_db_exists:
        srm_conn = connect_index("srm_index.db")
        srm_query = build_query_from_context(ctx)
        hits = search_srm(
            srm_conn,
            query=srm_query,
            aircraft_family=st.session_state.get("aircraft_family", None),
            limit=6,
        )
        srm_conn.close()

        st.subheader("SRM References (Search)")
        st.caption("Search-first SRM connection: results are citations (doc + page), not automated decisions.")

        st.code(srm_query, language="text")

        if hits:
            for h in hits:
                st.markdown(
                    f"**{h.aircraft_family} SRM (Rev {h.revision})** — *{h.title}*  \n"
                    f"Page **{h.page_no}** • Rank `{h.rank:.2f}`"
                )
                st.write(h.snippet)
                # If you later host PDFs, this becomes clickable:
                if h.base_url:
                    st.write(f"PDF: {h.base_url}{h.file_name} (open and go to page {h.page_no})")
                st.divider()
        else:
            st.info("No SRM hits found for this query. Try adding ATA keywords or use a simpler description.")
    else:
        st.info("SRM index not found (srm_index.db). Build it offline with srm_indexer.py and commit it.")
except Exception as e:
    st.warning("SRM search failed (index not ready or DB error).")
    st.exception(e)


    try:
        result = assess_damage(
            db_path=RULES_DB,
            aircraft_family=st.session_state.get("aircraft_family", "B787"),
            ctx=ctx,
            revision=None,
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
