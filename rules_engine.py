# rules_engine.py
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class AssessmentResult:
    rule_id: Optional[int]
    passed: bool
    disposition: str
    severity: str
    srm_ref: Optional[str]
    reasons: List[str]
    actions: Dict[str, Any]


def _within_range(val, minv, maxv) -> bool:
    if val is None:
        return True
    if minv is not None and val < minv:
        return False
    if maxv is not None and val > maxv:
        return False
    return True


def _get(ctx: Dict[str, Any], path: str) -> Any:
    cur: Any = ctx
    for p in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def _eval_conditions(conditions: Dict[str, Any], ctx: Dict[str, Any], reasons: List[str]) -> bool:
    # Demo support
    if conditions.get("requires_no_visible_crack") is True:
        if bool(_get(ctx, "damage.visible_crack")):
            reasons.append("Condition failed: visible crack present.")
            return False

    def check_clause(cl: Dict[str, Any]) -> bool:
        field = cl.get("field")
        op = cl.get("op")
        value = cl.get("value")
        actual = _get(ctx, field) if isinstance(field, str) else None

        if op == "==": return actual == value
        if op == "!=": return actual != value
        if op == "<=": return actual is not None and actual <= value
        if op == "<":  return actual is not None and actual < value
        if op == ">=": return actual is not None and actual >= value
        if op == ">":  return actual is not None and actual > value
        return False

    for clause in (conditions.get("deny_if") or []):
        if check_clause(clause):
            reasons.append(f"Denied by condition: {clause}")
            return False

    for clause in (conditions.get("allow_if") or []):
        if not check_clause(clause):
            reasons.append(f"Allow-if condition not met: {clause}")
            return False

    return True


def _eval_limits(limits: Dict[str, Any], ctx: Dict[str, Any], reasons: List[str]) -> bool:
    dmg = ctx.get("damage", {})
    ok = True

    if "max_diameter_mm" in limits and dmg.get("diameter_mm") is not None:
        if float(dmg["diameter_mm"]) > float(limits["max_diameter_mm"]):
            ok = False
            reasons.append(f"Diameter {dmg['diameter_mm']}mm > limit {limits['max_diameter_mm']}mm")

    if "max_depth_mm" in limits and dmg.get("depth_mm") is not None:
        if float(dmg["depth_mm"]) > float(limits["max_depth_mm"]):
            ok = False
            reasons.append(f"Depth {dmg['depth_mm']}mm > limit {limits['max_depth_mm']}mm")

    if "max_depth_to_thickness_ratio" in limits:
        ratio = dmg.get("depth_to_thickness_ratio")
        if ratio is None:
            ok = False
            reasons.append("Depth/thickness ratio not provided (needed for this rule).")
        elif float(ratio) > float(limits["max_depth_to_thickness_ratio"]):
            ok = False
            reasons.append(f"Ratio {ratio:.2f} > limit {limits['max_depth_to_thickness_ratio']}")

    return ok


def assess_damage(db_path: str, aircraft_family: str, ctx: Dict[str, Any], revision: Optional[str] = None) -> AssessmentResult:
    dmg_type = _get(ctx, "damage.type")
    structure = _get(ctx, "damage.structure")
    zone = _get(ctx, "location.zone")
    side = _get(ctx, "location.side") or "ANY"
    press = _get(ctx, "location.pressurized")
    sta = _get(ctx, "location.sta")
    wl = _get(ctx, "location.wl")
    stringer = _get(ctx, "location.stringer_num")

    press_i = None if press is None else (1 if press else 0)

    if not (dmg_type and structure and zone):
        return AssessmentResult(
            None, False, "ENGINEERING_REVIEW", "engineering", None,
            ["Missing required classification (damage.type / damage.structure / location.zone)."],
            {"disposition": "ENGINEERING_REVIEW", "next_steps": ["Provide missing details."]}
        )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if revision:
            rs = conn.execute(
                "SELECT id FROM rule_sets WHERE aircraft_family=? AND revision=? ORDER BY id DESC LIMIT 1",
                (aircraft_family, revision)
            ).fetchone()
        else:
            rs = conn.execute(
                "SELECT id FROM rule_sets WHERE aircraft_family=? ORDER BY id DESC LIMIT 1",
                (aircraft_family,)
            ).fetchone()

        if not rs:
            return AssessmentResult(
                None, False, "ENGINEERING_REVIEW", "engineering", None,
                [f"No rule_set found for aircraft_family={aircraft_family}."],
                {"disposition": "ENGINEERING_REVIEW", "next_steps": ["Seed rules.db into repo."]}
            )

        rule_set_id = int(rs["id"])

        rows = conn.execute(
            """
            SELECT * FROM rules
             WHERE rule_set_id=?
               AND enabled=1
               AND damage_type=?
               AND structure_zone=?
               AND structure=?
               AND (side='ANY' OR side=?)
               AND (pressurized IS NULL OR pressurized=? OR ? IS NULL)
             ORDER BY priority DESC, id ASC
            """,
            (rule_set_id, dmg_type, zone, structure, side, press_i, press_i)
        ).fetchall()

        if not rows:
            return AssessmentResult(
                None, False, "ENGINEERING_REVIEW", "engineering", None,
                ["No matching rules found; escalate to engineering."],
                {"disposition": "ENGINEERING_REVIEW", "next_steps": ["Add rules for this case."]}
            )

        best_fail: Optional[AssessmentResult] = None

        for row in rows:
            if not _within_range(sta, row["sta_min"], row["sta_max"]):
                continue
            if not _within_range(wl, row["wl_min"], row["wl_max"]):
                continue
            if stringer is not None:
                if row["stringer_min"] is not None and stringer < row["stringer_min"]:
                    continue
                if row["stringer_max"] is not None and stringer > row["stringer_max"]:
                    continue

            reasons: List[str] = []
            conditions = json.loads(row["conditions_json"] or "{}")
            limits = json.loads(row["limits_json"] or "{}")
            actions = json.loads(row["actions_json"] or "{}")

            if not _eval_conditions(conditions, ctx, reasons):
                continue

            passed = _eval_limits(limits, ctx, reasons)

            if passed:
                return AssessmentResult(
                    int(row["id"]), True,
                    actions.get("disposition", "ALLOW_AS_IS"),
                    row["severity"] or "allow",
                    row["srm_ref"],
                    reasons or ["Within limits."],
                    actions
                )

            if best_fail is None:
                best_fail = AssessmentResult(
                    int(row["id"]), False,
                    actions.get("disposition", "ENGINEERING_REVIEW"),
                    row["severity"] or "engineering",
                    row["srm_ref"],
                    reasons or ["Out of limits."],
                    actions
                )

        return best_fail or AssessmentResult(
            None, False, "ENGINEERING_REVIEW", "engineering", None,
            ["No applicable rule after filtering; escalate to engineering."],
            {"disposition": "ENGINEERING_REVIEW", "next_steps": ["Capture more location detail."]}
        )
    finally:
        conn.close()
