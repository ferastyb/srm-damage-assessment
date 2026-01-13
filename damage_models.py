# damage_models.py
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass
class CheckResult:
    name: str
    passed: bool
    message: str


@dataclass
class DentDamage:
    aircraft_type: str
    structure_zone: str
    side: str
    sta: Optional[str]
    stringer: Optional[str]
    dent_diameter_mm: float
    dent_depth_mm: float
    crack_present: bool
    notes: Optional[str] = None


@dataclass
class DentAssessmentResult:
    disposition: str                 # e.g., "WITHIN_LIMITS", "ENGINEERING_REVIEW"
    severity: str                    # e.g., "LOW", "MEDIUM", "HIGH"
    within_limits: bool
    srm_reference: Optional[str]
    rule_id: Optional[int]
    reasoning: List[str]
    checks: List[CheckResult]


def assess_dent(dent: DentDamage) -> DentAssessmentResult:
    """
    Prototype deterministic logic.
    Replace thresholds with SRM-derived rules later.
    """
    checks: List[CheckResult] = []
    reasoning: List[str] = []

    # Gate: crack present => escalate
    if dent.crack_present:
        checks.append(CheckResult("Crack present", False, "Crack reported/present: requires engineering/SRM crack procedure."))
        reasoning.append("Crack present → engineering review.")
        return DentAssessmentResult(
            disposition="ENGINEERING_REVIEW",
            severity="HIGH",
            within_limits=False,
            srm_reference="SRM: Crack procedures (ref placeholder)",
            rule_id=1,
            reasoning=reasoning,
            checks=checks,
        )
    else:
        checks.append(CheckResult("Crack present", True, "No visible crack reported."))

    # Simple dent allowables (demo)
    # You can tune these quickly; later replace with SRM-coded limits.
    dia = float(dent.dent_diameter_mm or 0.0)
    dep = float(dent.dent_depth_mm or 0.0)

    if dep <= 0 or dia <= 0:
        checks.append(CheckResult("Valid dimensions", False, "Dent diameter/depth must be > 0."))
        reasoning.append("Missing/invalid dent dimensions.")
        return DentAssessmentResult(
            disposition="INSUFFICIENT_DATA",
            severity="MEDIUM",
            within_limits=False,
            srm_reference=None,
            rule_id=2,
            reasoning=reasoning,
            checks=checks,
        )

    # Demo thresholds (adjust)
    # within if depth <= 2.0mm AND diameter <= 30mm
    if dep <= 2.0 and dia <= 30.0:
        checks.append(CheckResult("Dent limits", True, f"Dent depth {dep:g}mm and diameter {dia:g}mm within prototype limits."))
        reasoning.append("Dent within prototype allowables.")
        return DentAssessmentResult(
            disposition="WITHIN_LIMITS",
            severity="LOW",
            within_limits=True,
            srm_reference="SRM: Dent allowables (ref placeholder)",
            rule_id=3,
            reasoning=reasoning,
            checks=checks,
        )

    # borderline: depth <= 3mm and diameter <= 50mm => inspect/monitor
    if dep <= 3.0 and dia <= 50.0:
        checks.append(CheckResult("Dent limits", True, f"Dent in borderline band (dep={dep:g}mm, dia={dia:g}mm)."))
        reasoning.append("Borderline dent → enhanced inspection / engineering confirmation.")
        return DentAssessmentResult(
            disposition="INSPECT_MONITOR",
            severity="MEDIUM",
            within_limits=True,
            srm_reference="SRM: Dent allowables / inspection (ref placeholder)",
            rule_id=4,
            reasoning=reasoning,
            checks=checks,
        )

    checks.append(CheckResult("Dent limits", False, f"Dent exceeds prototype limits (dep={dep:g}mm, dia={dia:g}mm)."))
    reasoning.append("Dent exceeds prototype allowables → engineering review.")
    return DentAssessmentResult(
        disposition="ENGINEERING_REVIEW",
        severity="HIGH",
        within_limits=False,
        srm_reference="SRM: Repair assessment required (ref placeholder)",
        rule_id=5,
        reasoning=reasoning,
        checks=checks,
    )


def build_plain_text_summary(dent: DentDamage, result: DentAssessmentResult) -> str:
    lines = []
    lines.append(f"Aircraft: {dent.aircraft_type}")
    lines.append(f"Zone: {dent.structure_zone} | Side: {dent.side} | STA: {dent.sta or 'N/A'} | Stringer: {dent.stringer or 'N/A'}")
    lines.append(f"Damage: Dent | Diameter: {dent.dent_diameter_mm:g} mm | Depth: {dent.dent_depth_mm:g} mm | Crack: {'YES' if dent.crack_present else 'NO'}")
    if dent.notes:
        lines.append(f"Notes: {dent.notes}")

    lines.append("")
    lines.append(f"Disposition: {result.disposition} | Severity: {result.severity}")
    if result.srm_reference:
        lines.append(f"SRM Reference: {result.srm_reference}")
    if result.rule_id is not None:
        lines.append(f"Rule ID: {result.rule_id}")

    if result.reasoning:
        lines.append("")
        lines.append("Reasoning:")
        for r in result.reasoning:
            lines.append(f"- {r}")

    if result.checks:
        lines.append("")
        lines.append("Checks:")
        for c in result.checks:
            lines.append(f"- [{'PASS' if c.passed else 'FAIL'}] {c.name}: {c.message}")

    return "\n".join(lines)
