import re
import json
import sqlite3
from pathlib import Path
from datetime import datetime
from rules_engine import assess_damage

import streamlit as st

from engine.damage_models import (
    DamageContext,
    DentDamage,
    assess_dent,
    build_plain_text_summary,
)

# --------- DB setup ---------

DB_PATH = Path("dent_assessments.db")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dent_assessments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            aircraft_type TEXT,
            structure_zone TEXT,
            area_pressurized INTEGER,
            srm_reference TEXT,
            side TEXT,
            station REAL,
            waterline REAL,
            stringer TEXT,
            depth_mm REAL,
            length_mm REAL,
            width_mm REAL,
            distance_to_frame_mm REAL,
            distance_to_stringer_mm REAL,
            skin_thickness_mm REAL,
            within_limits INTEGER,
            summary TEXT,
            raw_input_json TEXT
        )
        """
    )
    conn.commit()


def log_assessment(dent: DentDamage, within_limits: bool, summary: str, raw_input: dict):
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO dent_assessments (
            created_at,
            aircraft_type,
            structure_zone,
            area_pressurized,
            srm_reference,
            side,
            station,
            waterline,
            stringer,
            depth_mm,
            length_mm,
            width_mm,
            distance_to_frame_mm,
            distance_to_stringer_mm,
            skin_thickness_mm,
            within_limits,
            summary,
            raw_input_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            datetime.utcnow().isoformat(timespec="seconds") + "Z",
            dent.context.aircraft_type,
            dent.context.structure_zone,
            1 if dent.context.area_pressurized else 0,
            dent.context.srm_reference,
            dent.side,
            dent.station,
            dent.waterline,
            dent.stringer,
            dent.depth_mm,
            dent.length_mm,
            dent.width_mm,
            dent.distance_to_nearest_frame_mm,
            dent.distance_to_nearest_stringer_mm,
            dent.skin_thickness_mm,
            1 if within_limits else 0,
            summary,
            json.dumps(raw_input),
        ),
    )
    conn.commit()


def load_recent_assessments(limit: int = 20):
    conn = get_connection()
    cur = conn.execute(
        """
        SELECT
            created_at,
            aircraft_type,
            structure_zone,
            side,
            station,
            depth_mm,
            length_mm,
            width_mm,
            distance_to_frame_mm,
            distance_to_stringer_mm,
            skin_thickness_mm,
            within_limits
        FROM dent_assessments
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    )
    return cur.fetchall()


# --------- Simple parser for free-text damage description ---------

def parse_damage_description(text: str) -> dict:
    """
    Very simple, deterministic parser for descriptions like:
    "B787, fuselage, LH side, STA 1280, S-10L, skin dent 25mm dia, 3mm depth, no visible crack."
    Returns a dict of fields you can drop into session_state/defaults.
    """
    parsed = {
        "aircraft_type": st.session_state.get("aircraft_type", "B787-8"),
        "structure_zone": st.session_state.get("structure_zone", "fuselage"),
        "area_pressurized": st.session_state.get("area_pressurized", True),
        "srm_reference": st.session_state.get(
            "srm_reference", "SRM 53-10-XX Fig. 201 (example)"
        ),
        "side": st.session_state.get("side", "LH"),
        "station": st.session_state.get("station", 1280.0),
        "waterline": st.session_state.get("waterline", 210.0),
        "stringer": st.session_state.get("stringer", "S-10L"),
        "depth_mm": st.session_state.get("depth_mm", 2.5),
        "length_mm": st.session_state.get("length_mm", 30.0),
        "width_mm": st.session_state.get("width_mm", 30.0),
        "skin_thickness_mm": st.session_state.get("skin_thickness_mm", 2.2),
        "dist_frame_mm": st.session_state.get("dist_frame_mm", 120.0),
        "dist_stringer_mm": st.session_state.get("dist_stringer_mm", 80.0),
        "notes": st.session_state.get(
            "notes", "No visible cracking. No wrinkles at fastener heads."
        ),
    }

    if not text:
        return parsed

    lower = text.lower()

    # Aircraft type (e.g. B787, B787-8, B767-300)
    m = re.search(r"\b(b[0-9]{3,4}(?:-[0-9a-z]+)?)\b", text, re.IGNORECASE)
    if m:
        parsed["aircraft_type"] = m.group(1).upper()

    # Structure zone
    if "fuselage" in lower:
        parsed["structure_zone"] = "fuselage"
    elif "wing" in lower:
        parsed["structure_zone"] = "wing"
    elif "tail" in lower or "empennage" in lower:
        parsed["structure_zone"] = "tail"

    # Side (LH / RH)
    m = re.search(r"\b(LH|RH)\b", text, re.IGNORECASE)
    if m:
        parsed["side"] = m.group(1).upper()

    # Station (STA 1280 etc.)
    m = re.search(r"\bSTA\s*([0-9]+(?:\.[0-9]+)?)", text, re.IGNORECASE)
    if m:
        try:
            parsed["station"] = float(m.group(1))
        except ValueError:
            pass

    # Stringer (S-10L, S10L...)
    m = re.search(r"\bS[-\s]?([0-9]+[LR]?)\b", text, re.IGNORECASE)
    if m:
        parsed["stringer"] = f"S-{m.group(1).upper()}"

    # Dent diameter (25mm dia, 25 mm diameter)
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*mm\s*(dia|diameter)", lower)
    if m:
        try:
            val = float(m.group(1))
            parsed["length_mm"] = val
            parsed["width_mm"] = val
        except ValueError:
            pass

    # Dent depth (3mm depth, 3 mm deep)
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*mm\s*(depth|deep)", lower)
    if m:
        try:
            parsed["depth_mm"] = float(m.group(1))
        except ValueError:
            pass

    # No visible crack / cracking
    if "no visible crack" in lower or "no cracks" in lower:
        if parsed["notes"]:
            parsed["notes"] += " "
        parsed["notes"] += "No visible cracking."

    return parsed


# --------- Streamlit app setup ---------

init_db()

st.set_page_config(
    page_title="Fuselage Dent Checker (Prototype)",
    layout="centered",
)

st.title("Fuselage Dent Checker (Prototype)")
st.caption(
    "Semi-automated structural damage assessment for fuselage dents. "
    "Advisory only – always verify against the current SRM and company procedures."
)

# --------- Initialize session_state defaults ---------

defaults = {
    "aircraft_type": "B787-8",
    "structure_zone": "fuselage",
    "area_pressurized": True,
    "srm_reference": "SRM 53-10-XX Fig. 201 (example)",
    "side": "LH",
    "station": 1280.0,
    "waterline": 210.0,
    "stringer": "S-10L",
    "depth_mm": 2.5,
    "length_mm": 30.0,
    "width_mm": 30.0,
    "skin_thickness_mm": 2.2,
    "dist_frame_mm": 120.0,
    "dist_stringer_mm": 80.0,
    "notes": "No visible cracking. No wrinkles at fastener heads.",
    "damage_desc": "",
}

for k, v in defaults.items():
    st.session_state.setdefault(k, v)

# --------- Free-text damage description ---------

st.subheader("Damage description (quick entry for AOG)")

damage_desc = st.text_area(
    "Enter or paste damage description",
    value=st.session_state["damage_desc"],
    placeholder='e.g. "B787, fuselage, LH side, STA 1280, S-10L, skin dent 25mm dia, 3mm depth, no visible crack."',
)

if st.button("Parse description into fields"):
    st.session_state["damage_desc"] = damage_desc
    parsed = parse_damage_description(damage_desc)
    for key, value in parsed.items():
        st.session_state[key] = value
    st.success("Description parsed into fields below. Please review before running assessment.")

st.markdown("---")

# --------- Structured input form ---------

with st.form("dent_input_form"):

    st.subheader("Context")

    aircraft_type = st.text_input(
        "Aircraft type", value=st.session_state["aircraft_type"]
    )

    structure_zone = st.text_input(
        "Structure zone", value=st.session_state["structure_zone"]
    )

    area_pressurized = st.checkbox(
        "Pressurized area", value=st.session_state["area_pressurized"]
    )

    srm_reference = st.text_input(
        "SRM reference (optional)", value=st.session_state["srm_reference"]
    )

    st.markdown("---")
    st.subheader("Location (optional but useful)")

    side = st.selectbox("Side", options=["LH", "RH"], index=0 if st.session_state["side"] == "LH" else 1)

    station = st.number_input(
        "Station (STA)", value=st.session_state["station"], step=10.0
    )

    waterline = st.number_input(
        "Waterline (WL)", value=st.session_state["waterline"], step=5.0
    )

    stringer = st.text_input("Stringer", value=st.session_state["stringer"])

    st.markdown("---")
    st.subheader("Dent dimensions")

    depth_mm = st.number_input(
        "Dent depth (mm)",
        min_value=0.0,
        value=st.session_state["depth_mm"],
        step=0.1,
    )

    length_mm = st.number_input(
        "Dent length (mm)",
        min_value=0.0,
        value=st.session_state["length_mm"],
        step=1.0,
    )

    width_mm = st.number_input(
        "Dent width (mm)",
        min_value=0.0,
        value=st.session_state["width_mm"],
        step=1.0,
    )

    skin_thickness_mm = st.number_input(
        "Skin thickness at dent (mm)",
        min_value=0.0,
        value=st.session_state["skin_thickness_mm"],
        step=0.1,
    )

    st.markdown("---")
    st.subheader("Distances to structure")

    dist_frame_mm = st.number_input(
        "Distance to nearest frame (mm)",
        min_value=0.0,
        value=st.session_state["dist_frame_mm"],
        step=5.0,
        help="Measured along the skin from the dent centre to the closest frame.",
    )

    dist_stringer_mm = st.number_input(
        "Distance to nearest stringer (mm)",
        min_value=0.0,
        value=st.session_state["dist_stringer_mm"],
        step=5.0,
        help="Measured along the skin from the dent centre to the closest stringer.",
    )

    st.markdown("---")
    notes = st.text_area(
        "Notes / observations",
        value=st.session_state["notes"],
        height=80,
    )

    submitted = st.form_submit_button("Run assessment")

# --------- Run the rule engine ---------

if submitted:
    # Persist latest values back to session_state
    st.session_state.update(
        dict(
            aircraft_type=aircraft_type,
            structure_zone=structure_zone,
            area_pressurized=area_pressurized,
            srm_reference=srm_reference,
            side=side,
            station=station,
            waterline=waterline,
            stringer=stringer,
            depth_mm=depth_mm,
            length_mm=length_mm,
            width_mm=width_mm,
            skin_thickness_mm=skin_thickness_mm,
            dist_frame_mm=dist_frame_mm,
            dist_stringer_mm=dist_stringer_mm,
            notes=notes,
        )
    )

    ctx = {
    "location": {
        "zone": "fuselage",
        "side": st.session_state.get("side", "ANY"),
        "sta": st.session_state.get("sta"),
        "wl": st.session_state.get("wl"),
        "stringer_num": st.session_state.get("stringer_num"),
        "pressurized": True,
    },
    "damage": {
        "type": "dent",
        "structure": "skin",
        "diameter_mm": st.session_state.get("dent_diameter_mm"),
        "depth_mm": st.session_state.get("dent_depth_mm"),
        "visible_crack": st.session_state.get("visible_crack", False),
        "near_fastener_row": st.session_state.get("near_fastener_row", False),
        "depth_to_thickness_ratio": st.session_state.get("depth_to_thickness_ratio"),
    }
}

result = assess_damage("rules.db", "B787", ctx)

st.subheader("Rule-based Assessment")
st.write(f"**Disposition:** {result.disposition}")
st.write(f"**Severity:** {result.severity}")
if result.srm_ref:
    st.write(f"**SRM Ref:** {result.srm_ref}")
if result.rule_id is not None:
    st.write(f"**Rule ID:** {result.rule_id}")

st.markdown("### Reasoning")
for r in result.reasons:
    st.write(f"- {r}")


    # Log to SQLite
    log_assessment(dent, result.within_limits, summary_text, result.raw_input)

    # --------- Display results ---------

    st.markdown("---")
    st.subheader("Assessment result")

    if result.within_limits:
        st.success("Dent is WITHIN configured limits (prototype).")
    else:
        st.error("Dent is OUTSIDE configured limits or data is incomplete (prototype).")

    st.markdown("### Detailed checks")

    for check in result.checks:
        if check.passed:
            st.write(f"✅ **{check.name}** – {check.message}")
        else:
            st.write(f"⚠️ **{check.name}** – {check.message}")

    st.markdown("### Summary (for copy/paste into damage report)")
    st.code(summary_text, language="markdown")

    st.markdown(
        "> **Disclaimer:** This tool is a prototype and provides advisory output only. "
        "You must verify all assessments against the latest SRM revision and "
        "your organization's approved procedures."
    )

# --------- History section ---------

st.markdown("---")
st.subheader("Previous assessments (this environment)")

rows = load_recent_assessments(limit=20)
if rows:
    records = []
    for r in rows:
        d = dict(r)
        d["within_limits"] = "YES" if d["within_limits"] == 1 else "NO"
        records.append(d)
    st.table(records)
else:
    st.info("No assessments logged yet in this environment.")
