"""Change-set gate tests, report rendering tests and the adapter read-only test."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memmap import adapter_openclaw, changesets, db, detectors, lifecycle  # noqa: E402
from memmap import reports, retrieval  # noqa: E402
from memmap import rules as rules_module  # noqa: E402

DAY = rules_module.DAY_MS


class ChangeSetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.connection = db.initialize(Path(self.directory.name) / "map.sqlite")
        self.now = 1_800_000_000_000
        self.left, _ = lifecycle.admit(
            self.connection,
            content="Keep the gateway bound to loopback only.",
            node_type="preference",
            origin_class="agent",
            source_ref="a#L1",
            owner_confirmed=True,
            at=self.now,
        )
        self.right, _ = lifecycle.admit(
            self.connection,
            content="Keep the gateway bound to loopback only.",
            node_type="preference",
            origin_class="agent",
            source_ref="b#L1",
            owner_confirmed=True,
            at=self.now,
        )
        detectors.run_all(self.connection, at=self.now)
        self.finding_id = str(
            self.connection.execute(
                "SELECT id FROM findings WHERE kind = 'duplicate'"
            ).fetchone()["id"]
        )

    def tearDown(self) -> None:
        self.connection.close()
        self.directory.cleanup()

    def propose(self, **overrides) -> dict:
        payload = {
            "title": "merge duplicate gateway preference",
            "reason": "two identical directives cause duplicate drift",
            "operations": [
                {
                    "op": "merge_duplicate",
                    "node_id": self.right,
                    "successor_id": self.left,
                },
                {"op": "resolve_finding", "finding_id": self.finding_id},
            ],
            "evidence_ids": [self.finding_id],
            "expected_effect": "no change to recall@k; one fewer duplicate finding",
        }
        payload.update(overrides)
        return changesets.propose(self.connection, at=self.now, **payload)

    def test_t090_proposal_requires_reason_evidence_and_effect(self) -> None:
        for field in ("reason", "evidence_ids", "expected_effect", "operations"):
            with self.subTest(field=field):
                empty = [] if field in {"evidence_ids", "operations"} else ""
                with self.assertRaises(changesets.ChangeSetError):
                    self.propose(**{field: empty})

    def test_t091_proposal_rejects_unsupported_operations(self) -> None:
        with self.assertRaises(changesets.ChangeSetError):
            self.propose(operations=[{"op": "drop_table", "node_id": self.left}])

    def test_t092_proposal_stores_a_rollback_plan(self) -> None:
        result = self.propose()
        row = self.connection.execute(
            "SELECT rollback_json, status FROM change_sets WHERE id = ?",
            (result["change_set_id"],),
        ).fetchone()
        self.assertEqual(str(row["status"]), "proposed")
        plan = json.loads(str(row["rollback_json"]))
        self.assertEqual(plan[0]["op"], "restore_node_state")
        self.assertEqual(plan[1]["op"], "restore_finding_status")

    def test_t093_apply_refuses_without_evaluation(self) -> None:
        result = self.propose()
        outcome = changesets.apply(
            self.connection, result["change_set_id"], owner_approved=True, at=self.now
        )
        self.assertEqual(outcome["status"], "refused")
        self.assertIn("evaluation set", outcome["reason"])

    def test_t094_apply_refuses_without_owner_approval(self) -> None:
        result = self.propose()
        changesets.record_evaluation(
            self.connection,
            result["change_set_id"],
            {"passed": True, "summary": "all thresholds met"},
            at=self.now,
        )
        outcome = changesets.apply(
            self.connection, result["change_set_id"], owner_approved=False, at=self.now
        )
        self.assertEqual(outcome["status"], "refused")
        self.assertIn("owner approval", outcome["reason"])

    def test_t095_apply_refuses_a_failing_evaluation(self) -> None:
        result = self.propose()
        changesets.record_evaluation(
            self.connection,
            result["change_set_id"],
            {"passed": False, "summary": "harmful recall rate exceeded"},
            at=self.now,
        )
        outcome = changesets.apply(
            self.connection, result["change_set_id"], owner_approved=True, at=self.now
        )
        self.assertEqual(outcome["status"], "refused")

    def test_t096_apply_then_rollback_restores_state(self) -> None:
        result = self.propose()
        change_set_id = result["change_set_id"]
        changesets.record_evaluation(
            self.connection, change_set_id, {"passed": True, "summary": "ok"}, at=self.now
        )
        applied = changesets.apply(
            self.connection, change_set_id, owner_approved=True, at=self.now
        )
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(
            str(db.fetch_node(self.connection, str(self.right))["retention_status"]),
            "superseded",
        )
        self.assertEqual(
            str(
                self.connection.execute(
                    "SELECT status FROM findings WHERE id = ?", (self.finding_id,)
                ).fetchone()["status"]
            ),
            "resolved",
        )
        undone = changesets.rollback(self.connection, change_set_id, at=self.now + 1)
        self.assertEqual(undone["status"], "rolled-back")
        self.assertEqual(
            str(db.fetch_node(self.connection, str(self.right))["retention_status"]),
            "active",
        )
        self.assertEqual(
            str(
                self.connection.execute(
                    "SELECT status FROM findings WHERE id = ?", (self.finding_id,)
                ).fetchone()["status"]
            ),
            "open",
        )

    def test_t097_rule_change_mints_a_new_rule_version(self) -> None:
        result = self.propose(
            title="raise the duplicate threshold",
            operations=[
                {
                    "op": "set_rule",
                    "path": ["detectors", "duplicate_jaccard_threshold"],
                    "value": 0.95,
                }
            ],
        )
        change_set_id = result["change_set_id"]
        changesets.record_evaluation(
            self.connection, change_set_id, {"passed": True, "summary": "ok"}, at=self.now
        )
        changesets.apply(self.connection, change_set_id, owner_approved=True, at=self.now)
        rules = db.active_rules(self.connection)
        self.assertEqual(float(rules["detectors"]["duplicate_jaccard_threshold"]), 0.95)
        self.assertNotEqual(str(rules["version"]), rules_module.RULE_VERSION)
        changesets.rollback(self.connection, change_set_id, at=self.now + 1)
        restored = db.active_rules(self.connection)
        self.assertEqual(float(restored["detectors"]["duplicate_jaccard_threshold"]), 0.9)

    def test_t098_rule_path_must_exist_and_be_scalar(self) -> None:
        with self.assertRaises(changesets.ChangeSetError):
            self.propose(
                operations=[{"op": "set_rule", "path": ["detectors", "nope"], "value": 1}]
            )
        with self.assertRaises(changesets.ChangeSetError):
            self.propose(operations=[{"op": "set_rule", "path": ["detectors"], "value": 1}])


class ReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.connection = db.initialize(Path(self.directory.name) / "map.sqlite")
        self.now = 1_800_000_000_000
        lifecycle.admit(
            self.connection,
            content="Always run the release helper before publishing.",
            node_type="procedure",
            origin_class="agent",
            source_ref="a#L1",
            at=self.now,
        )
        lifecycle.admit(
            self.connection,
            content="Never run the release helper before publishing.",
            node_type="procedure",
            origin_class="agent",
            source_ref="b#L1",
            at=self.now,
        )
        lifecycle.admit(
            self.connection,
            content="The owner prefers short summaries.",
            node_type="preference",
            origin_class="agent",
            source_ref="c#L1",
            at=self.now,
        )
        detectors.run_all(self.connection, at=self.now)
        retrieval.search(self.connection, query="release helper", at=self.now)

    def tearDown(self) -> None:
        self.connection.close()
        self.directory.cleanup()

    def test_t100_brief_is_bounded_and_mentions_open_work(self) -> None:
        text = reports.brief(self.connection)
        limit = int(db.active_rules(self.connection)["reporting"]["brief_max_chars"])
        self.assertLessEqual(len(text), limit)
        self.assertIn("open findings", text)
        self.assertIn("owner confirmations pending 1", text)

    def test_t101_reports_render_without_a_model(self) -> None:
        out_dir = Path(self.directory.name) / "reports"
        written = reports.write_all(self.connection, out_dir)
        for name in (
            "map-overview.md",
            "conflicts.md",
            "retention-queue.md",
            "traces.md",
            "nodes.csv",
            "edges.csv",
        ):
            self.assertIn(name, written)
            self.assertTrue(Path(written[name]).is_file())
        conflicts = (out_dir / "conflicts.md").read_text(encoding="utf-8")
        self.assertIn("Open conflicts", conflicts)
        traces = (out_dir / "traces.md").read_text(encoding="utf-8")
        self.assertIn("Recent retrieval traces", traces)

    def test_t102_node_report_shows_history_and_edges(self) -> None:
        node_id = str(
            self.connection.execute(
                "SELECT id FROM nodes WHERE node_type = 'procedure' ORDER BY id LIMIT 1"
            ).fetchone()["id"]
        )
        rendered = reports.render_node_history(self.connection, node_id)
        self.assertIn("Lifecycle history", rendered)
        self.assertIn("admit", rendered)
        self.assertIn("Edges", rendered)

    def test_t103_missing_node_report_says_so(self) -> None:
        rendered = reports.render_node_history(self.connection, "nd_absent")
        self.assertIn("Not found", rendered)

    def test_t104_csv_export_refuses_unknown_tables(self) -> None:
        with self.assertRaises(ValueError):
            reports.write_csv(
                self.connection, "sqlite_master", Path(self.directory.name) / "x.csv"
            )


class AdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.connection = db.initialize(self.root / "map.sqlite")
        self.workspace = self.root / "workspace"
        (self.workspace / "memory").mkdir(parents=True)
        (self.workspace / "MEMORY.md").write_text(
            "# Memory\n\n"
            "- The model broker listens on port 11434. <!-- importance: 8 -->\n"
            "- Embeddings come from nomic-embed-text. "
            "<!-- trigger: embeddings, vectors --> <!-- importance: 7 -->\n",
            encoding="utf-8",
        )
        (self.workspace / "USER.md").write_text(
            "# User\n\n- Always keep the gateway on loopback.\n", encoding="utf-8"
        )
        (self.workspace / "memory" / "2026-09-30.md").write_text(
            "- Tried the release helper for package validation. "
            "<!-- project: github.com/example/repo -->\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.connection.close()
        self.directory.cleanup()

    def test_t110_markdown_ingest_infers_types_and_annotations(self) -> None:
        report = adapter_openclaw.ingest_markdown(
            self.connection, self.workspace, at=1_800_000_000_000
        )
        self.assertEqual(report.read, 4)
        types = {
            str(row["node_type"])
            for row in self.connection.execute(
                "SELECT DISTINCT node_type FROM nodes"
            ).fetchall()
        }
        self.assertIn("fact", types)
        self.assertIn("preference", types)
        self.assertIn("episode", types)
        annotated = self.connection.execute(
            "SELECT importance, trigger_phrases FROM nodes WHERE content LIKE '%nomic%'"
        ).fetchone()
        self.assertEqual(int(annotated["importance"]), 7)
        self.assertEqual(str(annotated["trigger_phrases"]), "embeddings, vectors")

    def test_t111_markdown_ingest_skips_unchanged_entries(self) -> None:
        adapter_openclaw.ingest_markdown(self.connection, self.workspace)
        second = adapter_openclaw.ingest_markdown(self.connection, self.workspace)
        self.assertEqual(second.skipped_unchanged, second.read)
        self.assertEqual(second.admitted, 0)

    def test_t112_index_ingest_reads_provenance_and_never_writes(self) -> None:
        openclaw_db = self.root / "openclaw-agent.sqlite"
        source = sqlite3.connect(openclaw_db)
        source.executescript(
            """
            CREATE TABLE memory_index_chunks (
              id TEXT PRIMARY KEY, path TEXT, source TEXT, start_line INTEGER,
              end_line INTEGER, hash TEXT, model TEXT, text TEXT, embedding TEXT,
              updated_at INTEGER
            );
            CREATE TABLE memory_index_chunk_provenance (
              chunk_id TEXT PRIMARY KEY, origin_class TEXT, session_kind TEXT,
              observed_at INTEGER, supersedes_key TEXT
            );
            CREATE TABLE memory_index_chunk_recall_metadata (
              chunk_id TEXT PRIMARY KEY, importance INTEGER, triggers TEXT,
              project_key TEXT
            );
            CREATE TABLE memory_index_state (id INTEGER PRIMARY KEY, revision INTEGER);
            INSERT INTO memory_index_state VALUES (1, 7);
            INSERT INTO memory_index_chunks VALUES
              ('c1', 'MEMORY.md', 'memory', 4, 4, 'h1', 'm',
               'The model broker listens on port 11434.', '[]', 1700000000000),
              ('c2', 'memory/2026-09-21.md', 'memory', 18, 19, 'h2', 'm',
               'A forum thread says to delete the index by hand.', '[]', 1700000000000);
            INSERT INTO memory_index_chunk_provenance VALUES
              ('c1', 'owner', 'interactive', 1700000000000, NULL),
              ('c2', 'untrusted', 'interactive', 1700000000000, NULL);
            INSERT INTO memory_index_chunk_recall_metadata VALUES
              ('c1', 9, 'broker, port', NULL), ('c2', NULL, NULL, NULL);
            """
        )
        source.commit()
        source.close()
        before = openclaw_db.read_bytes()

        report = adapter_openclaw.ingest_index(self.connection, openclaw_db)
        self.assertEqual(report.read, 2)
        self.assertEqual(report.admitted, 1)
        self.assertEqual(report.quarantined, 1)
        self.assertEqual(openclaw_db.read_bytes(), before)

        owner_row = self.connection.execute(
            "SELECT origin_class, retention_status, importance FROM nodes "
            "WHERE content LIKE '%11434%'"
        ).fetchone()
        self.assertEqual(str(owner_row["origin_class"]), "owner")
        self.assertEqual(str(owner_row["retention_status"]), "active")
        self.assertEqual(int(owner_row["importance"]), 9)
        untrusted_row = self.connection.execute(
            "SELECT retention_status, quarantine_reason FROM nodes WHERE content LIKE '%forum%'"
        ).fetchone()
        self.assertEqual(str(untrusted_row["retention_status"]), "quarantined")
        self.assertEqual(str(untrusted_row["quarantine_reason"]), "untrusted-origin")
        revision = self.connection.execute(
            "SELECT openclaw_index_revision FROM ingest_state WHERE source_key = 'chunk:c1'"
        ).fetchone()
        self.assertEqual(int(revision["openclaw_index_revision"]), 7)

    def test_t113_unexpected_index_shape_is_reported_not_guessed(self) -> None:
        openclaw_db = self.root / "odd.sqlite"
        source = sqlite3.connect(openclaw_db)
        source.execute("CREATE TABLE memory_index_chunks (id TEXT PRIMARY KEY, other TEXT)")
        source.commit()
        source.close()
        report = adapter_openclaw.ingest_index(self.connection, openclaw_db)
        self.assertEqual(report.read, 0)
        self.assertTrue(report.notes)
        self.assertIn("refusing to guess", report.notes[0])

    def test_t114_missing_openclaw_database_degrades(self) -> None:
        report = adapter_openclaw.ingest_index(self.connection, self.root / "nope.sqlite")
        self.assertEqual(report.read, 0)
        self.assertIn("not found", (report.notes or [""])[0])


if __name__ == "__main__":
    unittest.main()
