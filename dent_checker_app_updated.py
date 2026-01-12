# dent_checker_app.py
"""
Fuselage Dent Checker (Prototype)
- Fast AOG free-text parsing into structured fields
- Deterministic dent assessment (prototype rules)
- Optional SRM search against a local SQLite FTS index (srm_index.db)

This file is designed to be deployable on Streamlit Cloud.
If SRM index DB is not present, SRM search is skipped gracefully.

Expected optional files:
- rules.db          (SQLite rules DB for future expansion)
- srm_index.db      (SQLite FTS5 index built from SRM PDFs)
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import streamlit as st

# Your existing deterministic models (already in your repo)
from damage_models import (
    DentDamage,
    assess_dent,
    build_plain_text_summary,
)

# SRM search helper (we just updated this file in your repo)
try:
    from srm_search import guess_srm_db_path, open_srm_index, safe_search_srm
except Exception:  # pragma: no cover
    guess_srm_db_path = None  # type: ignore
    open_srm_index = None  # type: ignore
    safe_search_srm = None  # type: ignore


# ---------------------------
# Helpers
# ---------------------------

def _float_or_none(x: str) -> Optional[float]:
    x = (x or "").strip()
    if not x:
        return None
    try:
        return float(x)
    except Exception:
        return None


def parse_damage_description(text: str) -> Dict[str, Any]:
    """
    Very lightweight parser for AOG-style descriptions.

    Example:
      "B787, fuselage, LH side, STA 1280, S-10L, skin dent 25mm dia, 3mm depth, no visible crack."
    """
    t = (text or "").strip()

    out: Dict[str, Any] = {}

    # Aircraft family/type: B787, B737, A320, E175 (accept variants like B787-8, 737-800)
    m = re.search(r"\b(B\s?7\d{2}|A\s?3\d{2}|E\s?17[05])(?:[-\s]?\d+)?\b", t, re.I)
    if m:
        out["aircraft_type"] = m.group(0).upper().replace(" ", "")

    # Side
    if re.search(r"\bLH\b|\bleft\b", t, re.I):
        out["side"] = "LH"
    elif re.search(r"\bRH\b|\bright\b", t, re.I):
        out["side"] = "RH"

    # STA
    m = re.search(r"\bSTA\s*([0-9]{2,5})\b", t, re.I)
    if m:
        out["sta"] = m.group(1)

    # Stringer / station line like S-10L, S10L, STR 10L
    m = re.search(r"\bS[-\s]?(\d{1,2}[LR]?)\b", t, re.I)
    if m:
        out["stringer"] = f"S-{m.group(1).upper()}"

    # Diameter (mm)
    m = re.search(r"(\d+(?:\.\d+)?)\s*mm\s*(?:dia|diam|diameter)\b", t, re.I)
    if m:
        out["diameter_mm"] = float(m.group(1))

    # Depth (mm)
    m = re.search(r"(\d+(?:\.\d+)?)\s*mm\s*depth\b", t, re.I)
    if m:
        out["depth_mm"] = float(m.group(1))

    # Crack present?
    if re.search(r"\bno\s+visible\s+crack\b|\bno\s+crack\b|\bwithout\s+crack\b", t, re.I):
        out["crack_present"] = False
    elif re.search(r"\bcrack\b|\bcracked\b", t, re.I):
        out["crack_present"] = True

    # Damage type hint (kept for SRM query building)
    if re.search(r"\bdent\b", t, re.I):
        out["damage_type"] = "dent"

    return out


def build_query_from_context(ctx: Dict[str, Any]) -> str:
    """
    Build a compact SRM search query from the parsed/entered context.
    This is intentionally simple and robust for SQLite FTS.
    """
    parts = []

    fam = ctx.get("aircraft_family") or ctx.get("aircraft_type")
    if fam:
        parts.append(str(fam))

    for key in ("zone", "sta", "stringer", "damage_type"):
        v = ctx.get(key)
        if v:
            parts.append(str(v))

    d = ctx.get("diameter_mm")
    if d is not None:
        parts.append(f"{d:g}mm dia")

    dep = ctx.get("depth_mm")
    if dep is not None:
        parts.append(f"{dep:g}mm depth")

    crack = ctx.get("crack_present")
    if crack is True:
        parts.append("crack")
    elif crack is False:
        parts.append("no crack")

    # A couple of high-value SRM keywords
    parts.append("SRM")
    parts.append("allowable damage")
    parts.append("repair")

    return " ".join(parts)


def get_srm_connection() -> Optional[sqlite3.Connection]:
    """
    Open SRM index DB if present. Returns None if missing.
    """
    # Prefer an explicit env var (useful on Streamlit Cloud)
    env_path = os.getenv("SRM_INDEX_DB", "").strip()
    if env_path:
        p = Path(env_path)
        if p.exists():
            try:
                if open_srm_index:
                    return open_srm_index(str(p))
                conn = sqlite3.connect(str(p), check_same_thread=False)
                conn.row_factory = sqlite3.Row
                return conn
            except Exception:
                return None

    # Otherwise try to guess
    if guess_srm_db_path:
        guessed = guess_srm_db_path()
        if guessed:
            try:
                if open_srm_index:
                    return open_srm_index(guessed)
                conn = sqlite3.connect(guessed, check_same_thread=False)
                conn.row_factory = sqlite3.Row
                return conn
            except Exception:
                return None

    return None


# ---------------------------
# Streamlit UI
# ---------------------------

st.set_page_config(page_title="Fuselage Dent Checker (Prototype)", layout="centered")

st.title("Fuselage Dent Checker (Prototype)")
st.caption("Advisory only — always verify against the current SRM and operator procedures.")

with st.expander("Damage description (quick entry for AOG)", expanded=True):
    raw = st.text_area(
        "Enter or paste damage description",
        key="raw_description",
        placeholder='e.g. "B787, fuselage, LH side, STA 1280, S-10L, skin dent 25mm dia, 3mm depth, no visible crack."',
        height=110,
    )

    c1, c2 = st.columns([1, 1])
    with c1:
        if st.button("Parse description into fields"):
            parsed = parse_damage_description(raw)
            # Map parsed fields into session state defaults
            if "aircraft_type" in parsed:
                st.session_state["aircraft_type"] = parsed["aircraft_type"]
            if "side" in parsed:
                st.session_state["side"] = parsed["side"]
            if "sta" in parsed:
                st.session_state["sta"] = parsed["sta"]
            if "stringer" in parsed:
                st.session_state["stringer"] = parsed["stringer"]
            if "diameter_mm" in parsed:
                st.session_state["dent_diameter_mm"] = parsed["diameter_mm"]
            if "depth_mm" in parsed:
                st.session_state["dent_depth_mm"] = parsed["depth_mm"]
            if "crack_present" in parsed:
                st.session_state["crack_present"] = parsed["crack_present"]
            st.success("Parsed. Review/adjust fields below.")
    with c2:
        st.write("")


st.markdown("---")
st.subheader("Context")

aircraft_type = st.text_input("Aircraft type / family", key="aircraft_type", placeholder="B787 / B737 / A320 / E175")
structure_zone = st.text_input("Structure zone (optional)", key="structure_zone", placeholder="Fuselage / Wing / Empennage ...")
side = st.selectbox("Side", ["", "LH", "RH"], key="side")
sta = st.text_input("STA (optional)", key="sta", placeholder="1280")
stringer = st.text_input("Stringer (optional)", key="stringer", placeholder="S-10L")

st.subheader("Dent details")
c1, c2, c3 = st.columns(3)
with c1:
    dent_diameter_mm = st.number_input("Dent diameter (mm)", min_value=0.0, value=float(st.session_state.get("dent_diameter_mm") or 0.0))
with c2:
    dent_depth_mm = st.number_input("Dent depth (mm)", min_value=0.0, value=float(st.session_state.get("dent_depth_mm") or 0.0))
with c3:
    crack_present = st.checkbox("Crack present", value=bool(st.session_state.get("crack_present") or False))

notes = st.text_area("Notes (optional)", key="notes", placeholder="Any extra info (impact source, location detail, inspection results, etc.)", height=80)

st.markdown("---")
run = st.button("Run assessment", type="primary")

if run:
    # Build DentDamage for deterministic assessment
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

    st.header("Rule-based Assessment")
    st.write(f"**Disposition:** {result.disposition}")
    st.write(f"**Severity:** {result.severity}")
    if result.srm_reference:
        st.write(f"**SRM Ref:** {result.srm_reference}")
    if result.rule_id is not None:
        st.write(f"**Rule ID:** {result.rule_id}")

    st.subheader("Reasoning")
    if result.reasoning:
        for r in result.reasoning:
            st.write(f"- {r}")
    else:
        st.write("- (No reasoning provided)")

    st.subheader("Checks")
    for check in result.checks:
        if check.passed:
            st.write(f"✅ **{check.name}** — {check.message}")
        else:
            st.write(f"⚠️ **{check.name}** — {check.message}")

    st.subheader("Summary (copy/paste)")
    st.code(summary_text, language="markdown")

    # ---------------------------
    # SRM Search (optional)
    # ---------------------------
    st.markdown("---")
    st.subheader("SRM search (prototype)")

    # ✅ IMPORTANT FIX: define ctx BEFORE using it (prevents NameError: ctx not defined)
    ctx: Dict[str, Any] = {
        "aircraft_family": (aircraft_type or "").strip() or None,
        "aircraft_type": (aircraft_type or "").strip() or None,
        "zone": (structure_zone or "").strip() or None,
        "side": (side or "").strip() or None,
        "sta": (sta or "").strip() or None,
        "stringer": (stringer or "").strip() or None,
        "damage_type": "dent",
        "diameter_mm": float(dent_diameter_mm),
        "depth_mm": float(dent_depth_mm),
        "crack_present": bool(crack_present),
        "notes": (notes or "").strip() or None,
        "raw_description": (raw or "").strip() or None,
    }

    srm_query = build_query_from_context(ctx)
    st.caption(f"Query: `{srm_query}`")

    srm_conn = get_srm_connection()
    if not srm_conn or not safe_search_srm:
        st.info("SRM index not available in this deployment (no srm_index.db).")
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
                st.markdown(
                    f"**{h['aircraft_family']}** rev **{h.get('revision','')}** — `{h['file_name']}` — page **{h['page_no']}**"
                )
                st.write(h["snippet"])

    st.markdown(
        "> **Disclaimer:** This tool is a prototype and provides advisory output only. "
        "You must verify all assessments against the latest SRM revision and "
        "your organization's approved procedures."
    )
