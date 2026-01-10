#!/usr/bin/env python3
"""
seed_rules.py

Creates/updates the SRM rules SQLite DB from:
- rules_schema.sql
- rules_seed.json

Usage:
  python seed_rules.py --db rules.db --schema rules_schema.sql --seed rules_seed.json

Notes:
- This is a "demo-safe" seeder: it INSERTs a new rule_set each run unless you use --upsert-ruleset.
- Rules are inserted under the selected rule_set_id.
- JSON is stored as TEXT; validation here ensures the seed JSON is well-formed and minimally shaped.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ----------------------------
# Validation helpers
# ----------------------------

class SeedError(Exception):
    pass


def _require(obj: Dict[str, Any], key: str, typ: Any, ctx: str) -> Any:
    if key not in obj:
        raise SeedError(f"Missing required key '{key}' in {ctx}")
    val = obj[key]
    if typ is not None and val is not None and not isinstance(val, typ):
        raise SeedError(f"Key '{key}' in {ctx} must be {typ}, got {type(val)}")
    return val


def _optional(obj: Dict[str, Any], key: str, typ: Any, ctx: str, default=None) -> Any:
    if key not in obj:
        return default
    val = obj[key]
    if typ is not None and val is not None and not isinstance(val, typ):
        raise SeedError(f"Key '{key}' in {ctx} must be {typ} or null, got {type(val)}")
    return val


def _as_int_or_none(v: Any, ctx: str) -> Optional[int]:
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    raise SeedError(f"Expected int/bool/null for {ctx}, got {type(v)}")


def _as_float_or_none(v: Any, ctx: str) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    raise SeedError(f"Expected number/null for {ctx}, got {type(v)}")


# ----------------------------
# Data shapes
# ----------------------------

@dataclass
class RuleSetSeed:
    name: str
    aircraft_family: str
    revision: str
    effective_date: Optional[str]
    source: Optional[str]


@dataclass
class RuleSeed:
    enabled: int
    priority: int
    damage_type: str
    structure: str
    structure_zone: str
    zone_detail: Optional[str]
    side: str
    sta_min: Optional[float]
    sta_max: Optional[float]
    wl_min: Optional[float]
    wl_max: Optional[float]
    stringer_min: Optional[int]
    stringer_max: Optional[int]
    pressurized: Optional[int]
    material: Optional[str]
    conditions_json: str
    limits_json: str
    actions_json: str
    srm_ref: Optional[str]
    severity: str
    notes: Optional[str]
    source_page: Optional[str]


# ----------------------------
# DB helpers
# ----------------------------

def run_schema(conn: sqlite3.Connection, schema_path: Path) -> None:
    sql = schema_path.read_text(encoding="utf-8")
    conn.executescript(sql)


def ensure_foreign_keys(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON;")


def upsert_ruleset(
    conn: sqlite3.Connection,
    rs: RuleSetSeed,
    *,
    upsert: bool,
) -> int:
    """
    If upsert=False: always create a new rule_set row and return its id.
    If upsert=True: find existing by (aircraft_family, revision); update metadata; return id.
    """
    if upsert:
        row = conn.execute(
            "SELECT id FROM rule_sets WHERE aircraft_family = ? AND revision = ?",
            (rs.aircraft_family, rs.revision),
        ).fetchone()
        if row:
            rule_set_id = int(row[0])
            conn.execute(
                """
                UPDATE rule_sets
                   SET name = ?, effective_date = ?, source = ?
                 WHERE id = ?
                """,
                (rs.name, rs.effective_date, rs.source, rule_set_id),
            )
            return rule_set_id

    cur = conn.execute(
        """
        INSERT INTO rule_sets (name, aircraft_family, revision, effective_date, source)
        VALUES (?, ?, ?, ?, ?)
        """,
        (rs.name, rs.aircraft_family, rs.revision, rs.effective_date, rs.source),
    )
    return int(cur.lastrowid)


def insert_rules(conn: sqlite3.Connection, rule_set_id: int, rules: List[RuleSeed]) -> Tuple[int, int]:
    inserted = 0
    for r in rules:
        conn.execute(
            """
            INSERT INTO rules (
              rule_set_id, enabled, priority,
              damage_type, structure, structure_zone, zone_detail,
              side, sta_min, sta_max, wl_min, wl_max,
              stringer_min, stringer_max, pressurized, material,
              conditions_json, limits_json, actions_json,
              srm_ref, severity, notes, source_page
            )
            VALUES (
              ?, ?, ?,
              ?, ?, ?, ?,
              ?, ?, ?, ?, ?,
              ?, ?, ?, ?,
              ?, ?, ?,
              ?, ?, ?, ?
            )
            """,
            (
                rule_set_id, r.enabled, r.priority,
                r.damage_type, r.structure, r.structure_zone, r.zone_detail,
                r.side, r.sta_min, r.sta_max, r.wl_min, r.wl_max,
                r.stringer_min, r.stringer_max, r.pressurized, r.material,
                r.conditions_json, r.limits_json, r.actions_json,
                r.srm_ref, r.severity, r.notes, r.source_page
            ),
        )
        inserted += 1

    total = conn.execute("SELECT COUNT(*) FROM rules WHERE rule_set_id = ?", (rule_set_id,)).fetchone()[0]
    return inserted, int(total)


# ----------------------------
# Seed loading & validation
# ----------------------------

def load_seed(seed_path: Path) -> Tuple[RuleSetSeed, List[RuleSeed]]:
    data = json.loads(seed_path.read_text(encoding="utf-8"))

    rs_obj = _require(data, "rule_set", dict, "seed root")
    rs = RuleSetSeed(
        name=str(_require(rs_obj, "name", str, "rule_set")),
        aircraft_family=str(_require(rs_obj, "aircraft_family", str, "rule_set")),
        revision=str(_require(rs_obj, "revision", str, "rule_set")),
        effective_date=_optional(rs_obj, "effective_date", str, "rule_set", default=None),
        source=_optional(rs_obj, "source", str, "rule_set", default=None),
    )

    rules_arr = _require(data, "rules", list, "seed root")
    rules: List[RuleSeed] = []

    for i, robj in enumerate(rules_arr):
        if not isinstance(robj, dict):
            raise SeedError(f"Rule at index {i} must be an object/dict")

        ctx = f"rules[{i}]"

        enabled = _as_int_or_none(_optional(robj, "enabled", (int, bool), ctx, default=1), f"{ctx}.enabled")
        if enabled is None:
            enabled = 1

        priority = _optional(robj, "priority", int, ctx, default=0)
        if priority is None:
            priority = 0

        damage_type = str(_require(robj, "damage_type", str, ctx))
        structure = str(_require(robj, "structure", str, ctx))
        structure_zone = str(_require(robj, "structure_zone", str, ctx))

        zone_detail = _optional(robj, "zone_detail", str, ctx, default=None)
        side = str(_optional(robj, "side", str, ctx, default="ANY") or "ANY")

        sta_min = _as_float_or_none(_optional(robj, "sta_min", (int, float), ctx, default=None), f"{ctx}.sta_min")
        sta_max = _as_float_or_none(_optional(robj, "sta_max", (int, float), ctx, default=None), f"{ctx}.sta_max")
        wl_min = _as_float_or_none(_optional(robj, "wl_min", (int, float), ctx, default=None), f"{ctx}.wl_min")
        wl_max = _as_float_or_none(_optional(robj, "wl_max", (int, float), ctx, default=None), f"{ctx}.wl_max")

        stringer_min = _optional(robj, "stringer_min", int, ctx, default=None)
        stringer_max = _optional(robj, "stringer_max", int, ctx, default=None)

        pressurized = _as_int_or_none(_optional(robj, "pressurized", (int, bool), ctx, default=None), f"{ctx}.pressurized")
        material = _optional(robj, "material", str, ctx, default=None)

        # These are in seed as objects; store as compact JSON text.
        conditions = _optional(robj, "conditions", dict, ctx, default={})
        limits = _optional(robj, "limits", dict, ctx, default={})
        actions = _optional(robj, "actions", dict, ctx, default={})

        # Minimal sanity checks (keep flexible)
        if not isinstance(conditions, dict):
            raise SeedError(f"{ctx}.conditions must be an object")
        if not isinstance(limits, dict):
            raise SeedError(f"{ctx}.limits must be an object")
        if not isinstance(actions, dict):
            raise SeedError(f"{ctx}.actions must be an object")
        if "disposition" not in actions:
            # Not required by schema, but useful to enforce in demo.
            actions = {**actions, "disposition": "ENGINEERING_REVIEW"}

        srm_ref = _optional(robj, "srm_ref", str, ctx, default=None)
        severity = str(_optional(robj, "severity", str, ctx, default="engineering") or "engineering")
        notes = _optional(robj, "notes", str, ctx, default=None)
        source_page = _optional(robj, "source_page", str, ctx, default=None)

        rules.append(
            RuleSeed(
                enabled=int(enabled),
                priority=int(priority),
                damage_type=damage_type,
                structure=structure,
                structure_zone=structure_zone,
                zone_detail=zone_detail,
                side=side,
                sta_min=sta_min,
                sta_max=sta_max,
                wl_min=wl_min,
                wl_max=wl_max,
                stringer_min=stringer_min,
                stringer_max=stringer_max,
                pressurized=pressurized,
                material=material,
                conditions_json=json.dumps(conditions, separators=(",", ":"), ensure_ascii=False),
                limits_json=json.dumps(limits, separators=(",", ":"), ensure_ascii=False),
                actions_json=json.dumps(actions, separators=(",", ":"), ensure_ascii=False),
                srm_ref=srm_ref,
                severity=severity,
                notes=notes,
                source_page=source_page,
            )
        )

    return rs, rules


# ----------------------------
# CLI
# ----------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Seed SRM rules into SQLite from schema + seed JSON.")
    ap.add_argument("--db", default="rules.db", help="SQLite DB path (default: rules.db)")
    ap.add_argument("--schema", default="rules_schema.sql", help="Path to rules_schema.sql")
    ap.add_argument("--seed", default="rules_seed.json", help="Path to rules_seed.json")
    ap.add_argument(
        "--upsert-ruleset",
        action="store_true",
        help="Upsert rule_set by (aircraft_family, revision) instead of always inserting a new one.",
    )
    ap.add_argument(
        "--wipe-ruleset-rules",
        action="store_true",
        help="If used with --upsert-ruleset: delete existing rules for the matched rule_set before inserting seed rules.",
    )
    args = ap.parse_args()

    db_path = Path(args.db).expanduser().resolve()
    schema_path = Path(args.schema).expanduser().resolve()
    seed_path = Path(args.seed).expanduser().resolve()

    if not schema_path.exists():
        raise SystemExit(f"Schema file not found: {schema_path}")
    if not seed_path.exists():
        raise SystemExit(f"Seed file not found: {seed_path}")

    rs, rules = load_seed(seed_path)

    conn = sqlite3.connect(str(db_path))
    try:
        ensure_foreign_keys(conn)
        run_schema(conn, schema_path)

        conn.execute("BEGIN;")
        rule_set_id = upsert_ruleset(conn, rs, upsert=args.upsert_ruleset)

        if args.upsert_ruleset and args.wipe_ruleset_rules:
            conn.execute("DELETE FROM rules WHERE rule_set_id = ?", (rule_set_id,))

        inserted, total = insert_rules(conn, rule_set_id, rules)
        conn.commit()

        print("✅ Seed complete")
        print(f"DB: {db_path}")
        print(f"Rule set: {rs.name} (id={rule_set_id}, family={rs.aircraft_family}, rev={rs.revision})")
        print(f"Inserted rules this run: {inserted}")
        print(f"Total rules in this rule_set: {total}")

    except SeedError as e:
        conn.rollback()
        raise SystemExit(f"❌ Seed validation error: {e}") from e
    except Exception as e:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
