"""Detector tests: one per detector, plus the idempotence and node-writing rules."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memmap import db, detectors, lifecycle, retrieval  # noqa: E402
from memmap import rules as rules_module  # noqa: E402

DAY = rules_module.DAY_MS


class DetectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.connection = db.initialize(Path(self.directory.name) / "map.sqlite")
        self.now = 1_800_000_000_000

    def tearDown(self) -> None:
        self.connection.close()
        self.directory.cleanup()

    def add(self, content: str, node_type: str = "fact", **overrides) -> str:
        parameters = {
            "content": content,
            "node_type": node_type,
            "origin_class": "agent",
            "session_kind": "interactive",
            "at": self.now,
        }
        parameters.update(overrides)
        node_id, decision = lifecycle.admit(self.connection, **parameters)
        self.assertIsNotNone(node_id, f"admission refused: {decision.reason_code}")
        return str(node_id)

    def test_t050_detects_exact_duplicates(self) -> None:
        self.add("Keep the gateway bound to loopback only.", source_ref="MEMORY.md#L3-L3")
        self.add("Keep the gateway bound to loopback only.", source_ref="MEMORY.md#L41-L41")
        found = detectors.detect_duplicates(self.connection, at=self.now)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].score, 1.0)
        edge = self.connection.execute(
            "SELECT * FROM edges WHERE edge_type = 'duplicates'"
        ).fetchone()
        self.assertIsNotNone(edge)
        evidence = json.loads(str(edge["evidence_json"]))
        self.assertIn("jaccard", evidence)

    def test_t051_detects_near_duplicates_above_threshold(self) -> None:
        self.add(
            "To repair stale recall run the memory index force rebuild for the agent.",
            source_ref="MEMORY.md#L10-L10",
        )
        self.add(
            "To repair stale recall, run the memory index force rebuild for the agent.",
            source_ref="MEMORY.md#L55-L55",
        )
        found = detectors.detect_duplicates(self.connection, at=self.now)
        self.assertEqual(len(found), 1)
        self.assertGreaterEqual(found[0].score, 0.9)

    def test_t052_ignores_unrelated_pairs(self) -> None:
        self.add("The broker listens on port 11434.", source_ref="MEMORY.md#L1-L1")
        self.add("Embeddings come from nomic-embed-text.", source_ref="MEMORY.md#L2-L2")
        self.assertEqual(detectors.detect_duplicates(self.connection, at=self.now), [])

    def test_t053_does_not_pair_across_node_types(self) -> None:
        self.add("Keep the gateway bound to loopback only.", "fact", source_ref="a#L1-L1")
        self.add("Keep the gateway bound to loopback only.", "preference", source_ref="b#L1-L1")
        self.assertEqual(detectors.detect_duplicates(self.connection, at=self.now), [])

    def test_t054_detects_negation_conflicts(self) -> None:
        self.add("Always run the release helper before publishing.", "procedure", source_ref="a#L1")
        self.add("Never run the release helper before publishing.", "procedure", source_ref="b#L1")
        found = detectors.detect_conflicts(self.connection, at=self.now)
        self.assertEqual(len(found), 1)
        edge = self.connection.execute(
            "SELECT * FROM edges WHERE edge_type = 'contradicts'"
        ).fetchone()
        self.assertIsNotNone(edge)
        self.assertEqual(str(edge["captured_by"]), "detector")

    def test_t055_reports_explicit_contradicts_edges(self) -> None:
        left = self.add("The broker listens on port 11434.", source_ref="a#L1")
        right = self.add("The broker answers on port 9999 instead.", source_ref="b#L1")
        lifecycle.add_edge(
            self.connection,
            src_id=left,
            dst_id=right,
            edge_type="contradicts",
            confidence=0.8,
            confidence_basis="agent-inference",
            at=self.now,
        )
        found = detectors.detect_conflicts(self.connection, at=self.now)
        self.assertTrue(any(record.subject_ids == tuple(sorted((left, right))) for record in found))

    def test_t056_superseded_nodes_are_not_conflicts(self) -> None:
        old_id = self.add("Always bind the gateway to the LAN.", "preference", source_ref="a#L1")
        new_id = self.add("Never bind the gateway to the LAN.", "preference", source_ref="b#L1")
        lifecycle.supersede(
            self.connection, old_node_id=old_id, new_node_id=new_id, at=self.now + DAY
        )
        self.assertEqual(detectors.detect_conflicts(self.connection, at=self.now), [])

    def test_t057_detects_gaps_after_repeated_empty_queries(self) -> None:
        for offset in range(2):
            retrieval.search(
                self.connection,
                query="what is the owner's bank routing number",
                at=self.now + offset,
            )
        found = detectors.detect_gaps(self.connection, at=self.now)
        self.assertEqual(len(found), 1)
        self.assertIn("returned nothing", found[0].detail)

    def test_t058_single_empty_query_is_not_yet_a_gap(self) -> None:
        retrieval.search(self.connection, query="an unknown topic", at=self.now)
        self.assertEqual(detectors.detect_gaps(self.connection, at=self.now), [])

    def test_t059_detects_passed_validity(self) -> None:
        self.add("Study block runs weekday mornings.", valid_to=self.now - DAY)
        found = detectors.detect_outdated(self.connection, at=self.now)
        self.assertTrue(any("valid_to has passed" in record.detail for record in found))

    def test_t060_detects_newer_observation_on_the_same_subject(self) -> None:
        self.add(
            "The deploy target is host alpha.",
            source_ref="a#L1",
            observed_at=self.now - 10 * DAY,
        )
        self.add(
            "The deploy target is host beta.",
            source_ref="b#L1",
            observed_at=self.now,
        )
        found = detectors.detect_outdated(self.connection, at=self.now)
        self.assertTrue(any("newer active observation" in record.detail for record in found))

    def test_t061_detects_inconsistent_successor(self) -> None:
        old_id = self.add("The deploy target is host alpha.", source_ref="a#L1")
        new_id = self.add("The deploy target is host beta.", source_ref="b#L1")
        self.connection.execute(
            "UPDATE nodes SET superseded_by = ? WHERE id = ?", (new_id, old_id)
        )
        found = detectors.detect_outdated(self.connection, at=self.now)
        self.assertTrue(any("names successor" in record.detail for record in found))

    def test_t062_findings_are_written_as_nodes_with_edges(self) -> None:
        self.add("Keep the gateway bound to loopback only.", source_ref="a#L1")
        self.add("Keep the gateway bound to loopback only.", source_ref="b#L1")
        detectors.detect_duplicates(self.connection, at=self.now)
        finding = self.connection.execute("SELECT * FROM findings").fetchone()
        self.assertIsNotNone(finding["node_id"])
        node = db.fetch_node(self.connection, str(finding["node_id"]))
        self.assertEqual(str(node["node_type"]), "finding")
        self.assertEqual(str(node["captured_by"]), "detector")
        about = self.connection.execute(
            "SELECT COUNT(*) AS n FROM edges WHERE edge_type = 'about' AND src_id = ?",
            (str(finding["node_id"]),),
        ).fetchone()
        self.assertEqual(int(about["n"]), 2)

    def test_t063_detectors_are_idempotent(self) -> None:
        self.add("Keep the gateway bound to loopback only.", source_ref="a#L1")
        self.add("Keep the gateway bound to loopback only.", source_ref="b#L1")
        detectors.run_all(self.connection, at=self.now)
        before = self.connection.execute("SELECT COUNT(*) AS n FROM findings").fetchone()
        detectors.run_all(self.connection, at=self.now + 1)
        after = self.connection.execute("SELECT COUNT(*) AS n FROM findings").fetchone()
        self.assertEqual(int(before["n"]), int(after["n"]))

    def test_t064_resolved_findings_stay_resolved_on_rerun(self) -> None:
        self.add("Keep the gateway bound to loopback only.", source_ref="a#L1")
        self.add("Keep the gateway bound to loopback only.", source_ref="b#L1")
        detectors.run_all(self.connection, at=self.now)
        finding_id = str(self.connection.execute("SELECT id FROM findings").fetchone()["id"])
        self.connection.execute(
            "UPDATE findings SET status = 'resolved' WHERE id = ?", (finding_id,)
        )
        detectors.run_all(self.connection, at=self.now + 1)
        row = self.connection.execute(
            "SELECT status FROM findings WHERE id = ?", (finding_id,)
        ).fetchone()
        self.assertEqual(str(row["status"]), "resolved")

    def test_t065_finding_nodes_are_never_recallable(self) -> None:
        self.add("Keep the gateway bound to loopback only.", source_ref="a#L1")
        self.add("Keep the gateway bound to loopback only.", source_ref="b#L1")
        detectors.run_all(self.connection, at=self.now)
        outcome = retrieval.search(
            self.connection, query="gateway loopback duplicate", at=self.now
        )
        returned_types = {candidate.node_type for candidate in outcome.returned}
        self.assertNotIn("finding", returned_types)
        dropped = {
            candidate.node_id: candidate.drop_reason for candidate in outcome.dropped
        }
        self.assertTrue(
            any(
                reason == "filter:finding-node-not-recallable"
                for reason in dropped.values()
            )
        )


if __name__ == "__main__":
    unittest.main()
