from dataclasses import dataclass, asdict
from typing import List, Optional, Literal


# --------- Core data models ---------

@dataclass
class DamageContext:
    """
    General context for the damage.
    """
    aircraft_type: str              # e.g. "B787-8"
    structure_zone: str             # e.g. "fuselage", "wing"
    area_pressurized: bool          # True if in pressurized fuselage
    srm_reference: Optional[str] = None  # e.g. "SRM 53-10-XX Fig. 201"


@dataclass
class DentDamage:
    """
    Normalized input for a single dent.
    """
    context: DamageContext

    side: Literal["LH", "RH"]
    station: Optional[float] = None        # STA (if used)
    waterline: Optional[float] = None      # WL (if used)
    stringer: Optional[str] = None         # e.g. "S-10L"

    depth_mm: float = 0.0
    length_mm: float = 0.0                 # major dimension of dent
    width_mm: float = 0.0                  # minor dimension of dent

    distance_to_nearest_frame_mm: Optional[float] = None
    distance_to_nearest_stringer_mm: Optional[float] = None

    skin_thickness_mm: float = 0.0

    notes: str = ""                        # free text notes / observations


@dataclass
class DentLimits:
    """
    Numeric limits for a given dent scenario.
    All units are mm-based except depth ratio.
    """
    name: str                               # e.g. "pressurized_fuselage_dent"
    max_depth_ratio: float                  # depth / thickness limit (e.g. 0.05 = 5% t)
    max_diameter_mm: float                  # max dent diameter
    min_dist_frame_mm: float                # min distance to frame
    min_dist_stringer_mm: float             # min distance to stringer


@dataclass
class CheckResult:
    """
    Result for an individual check (depth, diameter, distances, etc).
    """
    name: str
    passed: bool
    message: str


@dataclass
class AssessmentResult:
    """
    Overall result of the dent assessment.
    """
    within_limits: bool
    checks: List[CheckResult]
    context: DamageContext
    raw_input: dict                         # original dent data as dict for traceability


# --------- Example limits table (you will tune these to your SRM) ---------

# NOTE: These values are EXAMPLES / PLACEHOLDERS.
# Replace them with actual SRM limits for your aircraft & zone.
PRESSURIZED_FUSELAGE_DENT_LIMITS = DentLimits(
    name="pressurized_fuselage_dent",
    max_depth_ratio=0.05,          # 5% of skin thickness
    max_diameter_mm=50.0,          # e.g. 50 mm max dent diameter
    min_dist_frame_mm=50.0,        # e.g. ≥ 50 mm from frame
    min_dist_stringer_mm=50.0,     # e.g. ≥ 50 mm from stringer
)

UNPRESSURIZED_FUSELAGE_DENT_LIMITS = DentLimits(
    name="unpressurized_fuselage_dent",
    max_depth_ratio=0.10,          # allow deeper dents in unpressurized area
    max_diameter_mm=75.0,
    min_dist_frame_mm=30.0,
    min_dist_stringer_mm=30.0,
)


def get_dent_limits_for_context(context: DamageContext) -> DentLimits:
    """
    Select a DentLimits set based on the context.
    For v1 we only differentiate pressurized vs unpressurized fuselage.
    Expand this as you add more structures and scenarios.
    """
    zone = context.structure_zone.lower()

    if zone == "fuselage":
        if context.area_pressurized:
            return PRESSURIZED_FUSELAGE_DENT_LIMITS
        else:
            return UNPRESSURIZED_FUSELAGE_DENT_LIMITS

    # Fallback – in a real system you’d handle wing, tail, doors, etc.
    # For now default to pressurized fuselage limits as a conservative choice.
    return PRESSURIZED_FUSELAGE_DENT_LIMITS


# --------- Rule engine for dent assessment ---------

def assess_dent(dent: DentDamage, limits: Optional[DentLimits] = None) -> AssessmentResult:
    """
    Apply SRM-style numeric rules to a dent and return a structured result.
    `limits` can be passed explicitly or auto-selected from context.
    """
    if limits is None:
        limits = get_dent_limits_for_context(dent.context)

    checks: List[CheckResult] = []
    within_limits = True

    # ---- 1. Depth vs thickness ----
    if dent.skin_thickness_mm <= 0:
        checks.append(CheckResult(
            name="depth_vs_thickness",
            passed=False,
            message="Skin thickness is not specified or zero – cannot apply depth ratio check."
        ))
        within_limits = False
    else:
        depth_ratio = dent.depth_mm / dent.skin_thickness_mm
        if depth_ratio <= limits.max_depth_ratio:
            checks.append(CheckResult(
                name="depth_vs_thickness",
                passed=True,
                message=(
                    f"Dent depth ratio {depth_ratio:.3f} "
                    f"≤ limit {limits.max_depth_ratio:.3f} (depth {dent.depth_mm:.1f} mm, "
                    f"skin thickness {dent.skin_thickness_mm:.1f} mm)."
                )
            ))
        else:
            checks.append(CheckResult(
                name="depth_vs_thickness",
                passed=False,
                message=(
                    f"Dent depth ratio {depth_ratio:.3f} "
                    f"> limit {limits.max_depth_ratio:.3f} (depth {dent.depth_mm:.1f} mm, "
                    f"skin thickness {dent.skin_thickness_mm:.1f} mm)."
                )
            ))
            within_limits = False

    # ---- 2. Diameter / size check ----
    # For now we treat the dent "diameter" as max(length, width).
    diameter = max(dent.length_mm, dent.width_mm)
    if diameter <= limits.max_diameter_mm:
        checks.append(CheckResult(
            name="dent_diameter",
            passed=True,
            message=(
                f"Dent diameter {diameter:.1f} mm ≤ limit {limits.max_diameter_mm:.1f} mm "
                f"(based on max of length {dent.length_mm:.1f} mm and width {dent.width_mm:.1f} mm)."
            )
        ))
    else:
        checks.append(CheckResult(
            name="dent_diameter",
            passed=False,
            message=(
                f"Dent diameter {diameter:.1f} mm > limit {limits.max_diameter_mm:.1f} mm "
                f"(based on max of length {dent.length_mm:.1f} mm and width {dent.width_mm:.1f} mm)."
            )
        ))
        within_limits = False

    # ---- 3. Distance to nearest frame ----
    if dent.distance_to_nearest_frame_mm is None:
        checks.append(CheckResult(
            name="distance_to_frame",
            passed=False,
            message="Distance to nearest frame not provided – cannot verify against limit."
        ))
        within_limits = False
    else:
        if dent.distance_to_nearest_frame_mm >= limits.min_dist_frame_mm:
            checks.append(CheckResult(
                name="distance_to_frame",
                passed=True,
                message=(
                    f"Distance to nearest frame {dent.distance_to_nearest_frame_mm:.1f} mm "
                    f"≥ limit {limits.min_dist_frame_mm:.1f} mm."
                )
            ))
        else:
            checks.append(CheckResult(
                name="distance_to_frame",
                passed=False,
                message=(
                    f"Distance to nearest frame {dent.distance_to_nearest_frame_mm:.1f} mm "
                    f"< limit {limits.min_dist_frame_mm:.1f} mm."
                )
            ))
            within_limits = False

    # ---- 4. Distance to nearest stringer ----
    if dent.distance_to_nearest_stringer_mm is None:
        checks.append(CheckResult(
            name="distance_to_stringer",
            passed=False,
            message="Distance to nearest stringer not provided – cannot verify against limit."
        ))
        within_limits = False
    else:
        if dent.distance_to_nearest_stringer_mm >= limits.min_dist_stringer_mm:
            checks.append(CheckResult(
                name="distance_to_stringer",
                passed=True,
                message=(
                    f"Distance to nearest stringer {dent.distance_to_nearest_stringer_mm:.1f} mm "
                    f"≥ limit {limits.min_dist_stringer_mm:.1f} mm."
                )
            ))
        else:
            checks.append(CheckResult(
                name="distance_to_stringer",
                passed=False,
                message=(
                    f"Distance to nearest stringer {dent.distance_to_nearest_stringer_mm:.1f} mm "
                    f"< limit {limits.min_dist_stringer_mm:.1f} mm."
                )
            ))
            within_limits = False

    return AssessmentResult(
        within_limits=within_limits,
        checks=checks,
        context=dent.context,
        raw_input=asdict(dent),
    )


# --------- Simple deterministic explanation (placeholder for LLM) ---------

def build_plain_text_summary(result: AssessmentResult) -> str:
    """
    Build a simple text summary from an AssessmentResult.
    This is a deterministic placeholder; you can later replace or augment
    this with an LLM-generated explanation.
    """
    ctx = result.context
    status = "WITHIN" if result.within_limits else "OUTSIDE"

    lines = []
    lines.append(
        f"Assessment: Dent is {status} the configured limits "
        f"for scenario '{ctx.structure_zone}' "
        f"({'pressurized' if ctx.area_pressurized else 'unpressurized'} area)."
    )

    if ctx.srm_reference:
        lines.append(f"Applicable reference: {ctx.srm_reference}")

    lines.append("")
    lines.append("Check details:")
    for check in result.checks:
        prefix = "✔" if check.passed else "✘"
        lines.append(f" - {prefix} {check.message}")

    if result.within_limits:
        lines.append("")
        lines.append("Suggested action (non-authoritative):")
        lines.append(
            " - No repair required per these limits. Record damage and continue "
            "operation in accordance with the current SRM revision."
        )
    else:
        lines.append("")
        lines.append("Suggested action (non-authoritative):")
        lines.append(
            " - Outside configured limits or insufficient data. Refer to "
            "Structures Engineering / Design Office and verify against the "
            "current SRM revision."
        )

    lines.append("")
    lines.append(
        "NOTE: This assessment is advisory only and must be verified "
        "against the latest applicable SRM and company procedures."
    )

    return "\n".join(lines)
