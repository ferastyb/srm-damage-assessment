-- srm_index_schema.sql
-- SQLite schema for SRM search index (page-level) using FTS5.
-- Public-safe artifact: stores extracted text + metadata. No PDFs required at runtime.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS docs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  aircraft_family TEXT,
  revision TEXT,
  title TEXT,
  file_name TEXT,
  file_hash TEXT,
  created_utc TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS pages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_id INTEGER NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
  page_no INTEGER NOT NULL,              -- PDF page number (1..N)
  printed_page INTEGER,                  -- SRM printed page number (e.g., 108) if detected
  text TEXT NOT NULL
);

-- External-content FTS5 table referencing pages.text
CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
  text,
  content='pages',
  content_rowid='id',
  tokenize='unicode61'
);

-- Keep FTS synced with pages table
CREATE TRIGGER IF NOT EXISTS pages_ai AFTER INSERT ON pages BEGIN
  INSERT INTO pages_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TRIGGER IF NOT EXISTS pages_ad AFTER DELETE ON pages BEGIN
  INSERT INTO pages_fts(pages_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;

CREATE TRIGGER IF NOT EXISTS pages_au AFTER UPDATE ON pages BEGIN
  INSERT INTO pages_fts(pages_fts, rowid, text) VALUES ('delete', old.id, old.text);
  INSERT INTO pages_fts(rowid, text) VALUES (new.id, new.text);
END;

-- Helpful indexes
CREATE INDEX IF NOT EXISTS idx_pages_doc_page ON pages(doc_id, page_no);
CREATE INDEX IF NOT EXISTS idx_pages_printed_page ON pages(printed_page);
