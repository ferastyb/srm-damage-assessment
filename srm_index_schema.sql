-- srm_index_schema.sql
-- SRM full-text index database (SQLite + FTS5)

PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS docs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  aircraft_family TEXT NOT NULL,     -- e.g., B787, B737, A320, E175
  revision TEXT NOT NULL,            -- inferred from filename or set UNKNOWN
  title TEXT NOT NULL,               -- pdf stem
  file_name TEXT NOT NULL,           -- original file name
  file_hash TEXT NOT NULL,           -- sha256 to prevent re-indexing same file
  created_utc TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_docs_unique
  ON docs(aircraft_family, revision, file_hash);

CREATE TABLE IF NOT EXISTS pages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_id INTEGER NOT NULL,
  page_no INTEGER NOT NULL,          -- 1-based page number
  text TEXT NOT NULL,
  FOREIGN KEY(doc_id) REFERENCES docs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_pages_doc_page
  ON pages(doc_id, page_no);

-- Full text search virtual table
CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts
USING fts5(
  text,
  content='pages',
  content_rowid='id',
  tokenize='porter'
);
