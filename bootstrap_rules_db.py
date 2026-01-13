# bootstrap_rules_db.py
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from seed_rules import load_seed, run_schema, ensure_foreign_keys, upsert_ruleset, insert_rules

REPO = Path(__file__).resolve().parent
DB_PATH = REPO / "rules.db"
SCHEMA_PATH = REPO / "rules_schema.sql"
SEED_PATHS = [
    REPO / "rules_seed.json",
    REPO / "rules_seed_b737_allowable_damage_1.json",
]
STAMP_PATH = REPO / ".rules_seed.sha256"


def sha256_of_files(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for p in paths:
        if p.exists():
            h.update(p.read_bytes())
    return h.hexdigest()


def bootstrap_rules_db(force: bool = False) -> None:
    seeds = [p for p in SEED_PATHS if p.exists()]
    if not seeds:
        return

    current = sha256_of_files([SCHEMA_PATH, *seeds])
    previous = STAMP_PATH.read_text().strip() if STAMP_PATH.exists() else ""

    if (not force) and DB_PATH.exists() and previous == current:
        return

    conn = sqlite3.connect(str(DB_PATH))
    try:
        ensure_foreign_keys(conn)
        run_schema(conn, SCHEMA_PATH)

        # Build/update each seed file into DB
        conn.execute("BEGIN;")
        for seed_path in seeds:
            rs, rules = load_seed(seed_path)
            rule_set_id = upsert_ruleset(conn, rs, upsert=True)
            # wipe rules for this ruleset each time to keep it deterministic
            conn.execute("DELETE FROM rules WHERE rule_set_id = ?", (rule_set_id,))
            insert_rules(conn, rule_set_id, rules)

        conn.commit()
        STAMP_PATH.write_text(current)
    finally:
        conn.close()
