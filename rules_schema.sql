-- rules_schema.sql
-- SQLite schema for SRM-style damage assessment rules (data-driven)
-- Safe for iterative growth: core columns + JSON for flexible predicates/limits/actions.

PRAGMA foreign_keys = ON;

-- ---------- Rule sets (SRM revision containers) ----------
CREATE TABLE IF NOT EXISTS rule_sets (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  name            TEXT NOT NULL,                 -- e.g., "B787 SRM Rev 01 (Demo)"
  aircraft_family TEXT NOT NULL,                 -- e.g., "B787"
  revision        TEXT NOT NULL,                 -- e.g., "01"
  effective_date  TEXT,                          -- ISO date "YYYY-MM-DD"
  source          TEXT,                          -- e.g., "OEM SRM PDF" / "Demo"
  created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_rule_sets_family_rev
  ON rule_sets (aircraft_family, revision);

-- ---------- Rules ----------
CREATE TABLE IF NOT EXISTS rules (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  rule_set_id     INTEGER NOT NULL,
  enabled         INTEGER NOT NULL DEFAULT 1,     -- 1=true, 0=false
  priority        INTEGER NOT NULL DEFAULT 0,     -- higher wins if multiple match

  -- Scope / matching
  damage_type     TEXT NOT NULL,                 -- dent/scratch/gouge/crack/etc
  structure       TEXT NOT NULL,                 -- skin/stringer/frame/etc
  structure_zone  TEXT NOT NULL,                 -- fuselage/wing/tail/etc
  zone_detail     TEXT,                          -- crown/keel/lower_lobe/etc

  side            TEXT DEFAULT 'ANY',             -- LH/RH/ANY

  -- Location ranges (nullable means "any")
  sta_min         REAL,
  sta_max         REAL,
  wl_min          REAL,
  wl_max          REAL,

  -- Stringer range (nullable means "any")
  stringer_min    INTEGER,
  stringer_max    INTEGER,

  -- Optional scope
  pressurized     INTEGER,                        -- 1/0/NULL
  material        TEXT,                           -- Al/Ti/CFRP/etc

  -- Flexible rule logic payloads
  conditions_json TEXT,                           -- JSON object (predicates)
  limits_json     TEXT,                           -- JSON object (thresholds)
  actions_json    TEXT,                           -- JSON object (disposition/steps)

  -- Traceability
  srm_ref         TEXT,                           -- reference string (no proprietary excerpts)
  severity        TEXT NOT NULL DEFAULT 'engineering', -- allow/repair/engineering/grounding
  notes           TEXT,
  source_page     TEXT,                           -- optional, "p. 123" / anchor
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at      TEXT NOT NULL DEFAULT (datetime('now')),

  FOREIGN KEY(rule_set_id) REFERENCES rule_sets(id) ON DELETE CASCADE
);

-- Helpful indexes for candidate selection
CREATE INDEX IF NOT EXISTS idx_rules_core_match
  ON rules (rule_set_id, enabled, damage_type, structure_zone, structure);

CREATE INDEX IF NOT EXISTS idx_rules_priority
  ON rules (rule_set_id, enabled, priority DESC);

CREATE INDEX IF NOT EXISTS idx_rules_side
  ON rules (side);

CREATE INDEX IF NOT EXISTS idx_rules_sta_range
  ON rules (sta_min, sta_max);

CREATE INDEX IF NOT EXISTS idx_rules_wl_range
  ON rules (wl_min, wl_max);

CREATE INDEX IF NOT EXISTS idx_rules_stringer_range
  ON rules (stringer_min, stringer_max);

-- ---------- Rule tags (optional) ----------
CREATE TABLE IF NOT EXISTS rule_tags (
  rule_id INTEGER NOT NULL,
  tag     TEXT NOT NULL,
  PRIMARY KEY (rule_id, tag),
  FOREIGN KEY(rule_id) REFERENCES rules(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_rule_tags_tag
  ON rule_tags(tag);

-- ---------- Trigger to keep updated_at current ----------
CREATE TRIGGER IF NOT EXISTS trg_rules_updated_at
AFTER UPDATE ON rules
FOR EACH ROW
BEGIN
  UPDATE rules SET updated_at = datetime('now') WHERE id = NEW.id;
END;
