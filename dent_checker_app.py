import streamlit as st

from engine.damage_models import (
    DamageContext,
    DentDamage,
    assess_dent,
    build_plain_text_summary,
)



st.set_page_config(
    page_title="Fuselage Dent Checker (Prototype)",
    layout="centered",
)


st.title("Fuselage Dent Checker (Prototype)")
st.caption(
    "Semi-automated structural damage assessment for fuselage dents. "
    "Advisory only – always verify against the current SRM and company procedures."
)

# --------- Input form ---------

with st.form("dent_input_form"):

    st.subheader("Context")

    aircraft_type = st.text_input("Aircraft type", value="B787-8")
    structure_zone = st.text_input("Structure zone", value="fuselage")
    area_pressurized = st.checkbox("Pressurized area", value=True)
    srm_reference = st.text_input(
        "SRM reference (optional)",
        value="SRM 53-10-XX Fig. 201 (example)",
    )

    st.markdown("---")
    st.subheader("Location (optional but useful)")

    side = st.selectbox("Side", options=["LH", "RH"], index=0)
    station = st.number_input("Station (STA)", value=1280.0, step=10.0)
    waterline = st.number_input("Waterline (WL)", value=210.0, step=5.0)
    stringer = st.text_input("Stringer", value="S-10L")

    st.markdown("---")
    st.subheader("Dent dimensions")

    depth_mm = st.number_input("Dent depth (mm)", min_value=0.0, value=2.5, step=0.1)
    length_mm = st.number_input("Dent length (mm)", min_value=0.0, value=30.0, step=1.0)
    width_mm = st.number_input("Dent width (mm)", min_value=0.0, value=30.0, step=1.0)
    skin_thickness_mm = st.number_input("Skin thickness at dent (mm)", min_value=0.0, value=2.2, step=0.1)

    st.markdown("---")
    st.subheader("Distances to structure")

    dist_frame_mm = st.number_input(
        "Distance to nearest frame (mm)",
        min_value=0.0,
        value=120.0,
        step=5.0,
        help="Measured along the skin from the dent centre to the closest frame."
    )

    dist_stringer_mm = st.number_input(
        "Distance to nearest stringer (mm)",
        min_value=0.0,
        value=80.0,
        step=5.0,
        help="Measured along the skin from the dent centre to the closest stringer."
    )

    st.markdown("---")
    notes = st.text_area(
        "Notes / observations",
        value="No visible cracking. No wrinkles at fastener heads.",
        height=80,
    )

    submitted = st.form_submit_button("Run assessment")


# --------- Run the rule engine ---------

if submitted:
    ctx = DamageContext(
        aircraft_type=aircraft_type,
        structure_zone=structure_zone,
        area_pressurized=area_pressurized,
        srm_reference=srm_reference.strip() or None,
    )

    dent = DentDamage(
        context=ctx,
        side=side,
        station=station,
        waterline=waterline,
        stringer=stringer.strip() or None,
        depth_mm=depth_mm,
        length_mm=length_mm,
        width_mm=width_mm,
        distance_to_nearest_frame_mm=dist_frame_mm,
        distance_to_nearest_stringer_mm=dist_stringer_mm,
        skin_thickness_mm=skin_thickness_mm,
        notes=notes.strip(),
    )

    result = assess_dent(dent)
    summary_text = build_plain_text_summary(result)

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
