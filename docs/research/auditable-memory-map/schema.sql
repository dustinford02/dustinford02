-- Auditable Memory Map (AMM) schema, version 1.
-- CANDIDATE. Validated only against the synthetic fixtures in tests/; never run
-- against a live OpenClaw deployment. See the research report for coverage.
--
-- Design constraints this schema is built to:
--   * One SQLite file in the agent workspace. No service, no extensions.
--   * The map never writes to OpenClaw's own database or memory files.
--   * Every row that asserts something carries provenance and a rule version.
--   * Nothing is hard-deleted. Removal writes a tombstone.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS map_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
) STRICT;

-- Versioned rule sets. Every node, edge, trace and history row names the rule
-- version in force when it was written, so an audit can replay the gates that
-- actually applied rather than the gates that apply now.
CREATE TABLE IF NOT EXISTS rule_versions (
  version    TEXT PRIMARY KEY,
  created_at INTEGER NOT NULL,
  rules_json TEXT NOT NULL,
  notes      TEXT NOT NULL DEFAULT '',
  active     INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1))
) STRICT;

CREATE TABLE IF NOT EXISTS nodes (
  id            TEXT PRIMARY KEY,
  node_type     TEXT NOT NULL CHECK (node_type IN (
                  'fact', 'preference', 'decision', 'procedure', 'source',
                  'episode', 'lesson', 'association', 'finding')),
  content       TEXT NOT NULL,
  content_norm  TEXT NOT NULL,
  content_hash  TEXT NOT NULL,

  -- Provenance. origin_class and session_kind use OpenClaw's closed sets so the
  -- map cannot invent a trust level OpenClaw does not have.
  origin_class  TEXT NOT NULL CHECK (origin_class IN (
                  'owner', 'agent', 'untrusted', 'system')),
  session_kind  TEXT NOT NULL CHECK (session_kind IN (
                  'interactive', 'cron', 'heartbeat', 'subagent', 'unknown')),
  source_ref    TEXT,
  source_session_id TEXT,
  captured_by   TEXT NOT NULL CHECK (captured_by IN (
                  'adapter', 'agent', 'owner', 'detector')),

  -- Bitemporal fields. valid_* is event time (when the claim held). recorded_at
  -- and observed_at are transaction/observation time.
  valid_from    INTEGER,
  valid_to      INTEGER,
  recorded_at   INTEGER NOT NULL,
  observed_at   INTEGER,
  superseded_by TEXT REFERENCES nodes(id) ON DELETE SET NULL,

  confidence       REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
  confidence_basis TEXT NOT NULL CHECK (confidence_basis IN (
                     'owner-confirmed', 'multi-source-corroborated',
                     'single-source', 'agent-inference',
                     'untrusted-uncorroborated', 'detector-derived')),

  -- Relevance signals. importance and trigger_phrases mirror the optional
  -- annotations OpenClaw already stores, so an imported entry keeps its signal.
  importance      INTEGER CHECK (importance IS NULL OR importance BETWEEN 1 AND 10),
  trigger_phrases TEXT,
  project_key     TEXT,
  subject_key     TEXT,

  use_count          INTEGER NOT NULL DEFAULT 0 CHECK (use_count >= 0),
  last_used_at       INTEGER,
  last_reinforced_at INTEGER,

  retention_status  TEXT NOT NULL CHECK (retention_status IN (
                      'active', 'reinforced', 'dormant', 'archived',
                      'superseded', 'quarantined', 'deleted')),
  quarantine_reason TEXT CHECK (quarantine_reason IS NULL OR quarantine_reason IN (
                      'untrusted-origin', 'awaiting-owner-confirmation',
                      'detector-flagged')),
  owner_confirmation TEXT NOT NULL DEFAULT 'not-required'
                      CHECK (owner_confirmation IN (
                        'not-required', 'pending', 'confirmed', 'rejected')),
  about_owner        INTEGER NOT NULL DEFAULT 0 CHECK (about_owner IN (0, 1)),

  created_at   INTEGER NOT NULL,
  updated_at   INTEGER NOT NULL,
  rule_version TEXT NOT NULL REFERENCES rule_versions(version)
) STRICT;

CREATE INDEX IF NOT EXISTS nodes_status_idx     ON nodes(retention_status);
CREATE INDEX IF NOT EXISTS nodes_type_idx       ON nodes(node_type);
CREATE INDEX IF NOT EXISTS nodes_norm_idx       ON nodes(content_norm);
CREATE INDEX IF NOT EXISTS nodes_origin_idx     ON nodes(origin_class);
CREATE INDEX IF NOT EXISTS nodes_subject_idx    ON nodes(subject_key);
CREATE INDEX IF NOT EXISTS nodes_valid_to_idx   ON nodes(valid_to);
CREATE INDEX IF NOT EXISTS nodes_superseded_idx ON nodes(superseded_by);
CREATE INDEX IF NOT EXISTS nodes_hash_idx       ON nodes(content_hash);

CREATE TABLE IF NOT EXISTS edges (
  id        TEXT PRIMARY KEY,
  src_id    TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
  dst_id    TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
  edge_type TEXT NOT NULL CHECK (edge_type IN (
              'supports', 'contradicts', 'supersedes', 'duplicates',
              'derived_from', 'about', 'part_of', 'retrieved_with')),

  confidence       REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
  confidence_basis TEXT NOT NULL,
  origin_class     TEXT NOT NULL CHECK (origin_class IN (
                     'owner', 'agent', 'untrusted', 'system')),
  captured_by      TEXT NOT NULL CHECK (captured_by IN (
                     'adapter', 'agent', 'owner', 'detector')),
  source_ref       TEXT,
  evidence_json    TEXT NOT NULL DEFAULT '{}',

  recorded_at  INTEGER NOT NULL,
  valid_from   INTEGER,
  valid_to     INTEGER,
  rule_version TEXT NOT NULL REFERENCES rule_versions(version),
  UNIQUE (src_id, dst_id, edge_type)
) STRICT;

CREATE INDEX IF NOT EXISTS edges_src_idx  ON edges(src_id);
CREATE INDEX IF NOT EXISTS edges_dst_idx  ON edges(dst_id);
CREATE INDEX IF NOT EXISTS edges_type_idx ON edges(edge_type);

-- One row per retrieval. Written by the retrieval path at query time. The
-- explain command reads only these rows; it never re-runs a search to explain
-- an old one.
CREATE TABLE IF NOT EXISTS traces (
  id             TEXT PRIMARY KEY,
  created_at     INTEGER NOT NULL,
  query          TEXT NOT NULL,
  query_hash     TEXT NOT NULL,
  agent_session  TEXT,
  k_requested    INTEGER NOT NULL,
  min_score      REAL NOT NULL,
  lanes_json     TEXT NOT NULL,
  filters_json   TEXT NOT NULL,
  candidate_count INTEGER NOT NULL,
  returned_count  INTEGER NOT NULL,
  completeness   TEXT NOT NULL CHECK (completeness IN ('full', 'partial-import')),
  trace_source   TEXT NOT NULL CHECK (trace_source IN (
                   'map-search', 'openclaw-recall-import')),
  rule_version   TEXT NOT NULL REFERENCES rule_versions(version)
) STRICT;

CREATE INDEX IF NOT EXISTS traces_created_idx ON traces(created_at);
CREATE INDEX IF NOT EXISTS traces_query_idx   ON traces(query_hash);

CREATE TABLE IF NOT EXISTS trace_candidates (
  trace_id        TEXT NOT NULL REFERENCES traces(id) ON DELETE CASCADE,
  node_id         TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
  rank            INTEGER NOT NULL,
  returned        INTEGER NOT NULL CHECK (returned IN (0, 1)),
  drop_reason     TEXT,
  lane_hits       TEXT NOT NULL DEFAULT '',
  keyword_score   REAL NOT NULL DEFAULT 0.0,
  vector_score    REAL NOT NULL DEFAULT 0.0,
  graph_score     REAL NOT NULL DEFAULT 0.0,
  recency_score   REAL NOT NULL DEFAULT 1.0,
  importance_mult REAL NOT NULL DEFAULT 1.0,
  trust_mult      REAL NOT NULL DEFAULT 1.0,
  final_score     REAL NOT NULL DEFAULT 0.0,
  graph_path      TEXT,
  PRIMARY KEY (trace_id, node_id)
) STRICT;

CREATE INDEX IF NOT EXISTS trace_candidates_node_idx ON trace_candidates(node_id);

-- Detector output. Every finding also gets a node of type 'finding' so findings
-- are themselves inspectable, linkable and auditable map content.
CREATE TABLE IF NOT EXISTS findings (
  id               TEXT PRIMARY KEY,
  node_id          TEXT REFERENCES nodes(id) ON DELETE SET NULL,
  kind             TEXT NOT NULL CHECK (kind IN (
                     'conflict', 'duplicate', 'gap', 'outdated')),
  subject_ids_json TEXT NOT NULL,
  detail           TEXT NOT NULL,
  score            REAL NOT NULL DEFAULT 0.0,
  detector_version TEXT NOT NULL,
  status           TEXT NOT NULL CHECK (status IN (
                     'open', 'acknowledged', 'resolved', 'dismissed')),
  created_at       INTEGER NOT NULL,
  updated_at       INTEGER NOT NULL,
  resolved_by_change_set TEXT,
  rule_version     TEXT NOT NULL REFERENCES rule_versions(version)
) STRICT;

CREATE INDEX IF NOT EXISTS findings_kind_status_idx ON findings(kind, status);

CREATE TABLE IF NOT EXISTS change_sets (
  id               TEXT PRIMARY KEY,
  created_at       INTEGER NOT NULL,
  author           TEXT NOT NULL CHECK (author IN ('agent', 'owner', 'detector')),
  title            TEXT NOT NULL,
  reason           TEXT NOT NULL,
  diff_json        TEXT NOT NULL,
  evidence_ids_json TEXT NOT NULL,
  expected_effect  TEXT NOT NULL,
  rollback_json    TEXT NOT NULL,
  status           TEXT NOT NULL CHECK (status IN (
                     'proposed', 'evaluated', 'approved', 'applied',
                     'rejected', 'rolled-back')),
  eval_result_json TEXT,
  applied_at       INTEGER,
  rolled_back_at   INTEGER,
  rule_version     TEXT NOT NULL REFERENCES rule_versions(version)
) STRICT;

-- Append-only lifecycle journal. One row per attempted transition, accepted or
-- refused, so "why is this node archived" is answerable without inference.
CREATE TABLE IF NOT EXISTS node_history (
  seq           INTEGER PRIMARY KEY AUTOINCREMENT,
  node_id       TEXT NOT NULL,
  at            INTEGER NOT NULL,
  transition    TEXT NOT NULL,
  from_status   TEXT,
  to_status     TEXT,
  actor         TEXT NOT NULL,
  accepted      INTEGER NOT NULL CHECK (accepted IN (0, 1)),
  reason        TEXT NOT NULL,
  before_json   TEXT,
  after_json    TEXT,
  change_set_id TEXT,
  rule_version  TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS node_history_node_idx ON node_history(node_id, at);

-- Admission decisions. Rejected sensitive content is never stored: the row
-- keeps the reason class and a hash so a reviewer can confirm the gate fired
-- without the repository holding the content it refused.
CREATE TABLE IF NOT EXISTS admission_log (
  seq             INTEGER PRIMARY KEY AUTOINCREMENT,
  at              INTEGER NOT NULL,
  decision        TEXT NOT NULL CHECK (decision IN (
                    'admitted', 'quarantined', 'rejected')),
  reason_code     TEXT NOT NULL,
  redaction_class TEXT,
  origin_class    TEXT NOT NULL,
  source_ref      TEXT,
  content_hash    TEXT NOT NULL,
  node_id         TEXT,
  rule_version    TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS admission_log_at_idx ON admission_log(at);

-- Adapter bookkeeping so a re-read of an unchanged OpenClaw source is a no-op.
CREATE TABLE IF NOT EXISTS ingest_state (
  source_key                TEXT PRIMARY KEY,
  last_seen_hash            TEXT NOT NULL,
  last_ingested_at          INTEGER NOT NULL,
  openclaw_index_revision   INTEGER,
  note                      TEXT NOT NULL DEFAULT ''
) STRICT;
