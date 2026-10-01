"""Lifecycle tests: one per transition, accepted and refused."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memmap import db, lifecycle  # noqa: E402
from memmap import rules as rules_module  # noqa: E402

DAY = rules_module.DAY_MS


class LifecycleTests(unittest.TestCase):
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

    def status(self, node_id: str) -> str:
        return str(db.fetch_node(self.connection, node_id)["retention_status"])

    def test_t020_use_increments_and_journals(self) -> None:
        node_id = self.add("The broker listens on port 11434.")
        result = lifecycle.apply_transition(
            self.connection, node_id=node_id, transition="use", at=self.now
        )
        self.assertTrue(result.accepted)
        row = db.fetch_node(self.connection, node_id)
        self.assertEqual(int(row["use_count"]), 1)
        self.assertEqual(int(row["last_used_at"]), self.now)
        history = self.connection.execute(
            "SELECT transition, accepted FROM node_history WHERE node_id = ? ORDER BY seq",
            (node_id,),
        ).fetchall()
        self.assertEqual([str(r["transition"]) for r in history], ["admit", "use"])

    def test_t021_reinforce_requires_minimum_uses(self) -> None:
        node_id = self.add("The broker listens on port 11434.")
        refused = lifecycle.apply_transition(
            self.connection, node_id=node_id, transition="reinforce", at=self.now
        )
        self.assertFalse(refused.accepted)
        self.assertIn("needs 3 uses", refused.reason)
        for _ in range(3):
            lifecycle.apply_transition(
                self.connection, node_id=node_id, transition="use", at=self.now
            )
        accepted = lifecycle.apply_transition(
            self.connection, node_id=node_id, transition="reinforce", at=self.now
        )
        self.assertTrue(accepted.accepted)
        self.assertEqual(self.status(node_id), "reinforced")

    def test_t022_reinforcement_window_expires(self) -> None:
        node_id = self.add("The broker listens on port 11434.")
        for _ in range(3):
            lifecycle.apply_transition(
                self.connection, node_id=node_id, transition="use", at=self.now
            )
        later = self.now + 31 * DAY
        refused = lifecycle.apply_transition(
            self.connection, node_id=node_id, transition="reinforce", at=later
        )
        self.assertFalse(refused.accepted)

    def test_t023_facts_never_decay(self) -> None:
        node_id = self.add("The broker listens on port 11434.")
        refused = lifecycle.apply_transition(
            self.connection,
            node_id=node_id,
            transition="decay",
            at=self.now + 400 * DAY,
        )
        self.assertFalse(refused.accepted)
        self.assertIn("do not decay", refused.reason)
        self.assertEqual(self.status(node_id), "active")

    def test_t024_preferences_never_decay(self) -> None:
        node_id = self.add("Prefer short summaries in replies.", node_type="preference")
        refused = lifecycle.apply_transition(
            self.connection,
            node_id=node_id,
            transition="decay",
            at=self.now + 400 * DAY,
        )
        self.assertFalse(refused.accepted)

    def test_t025_associations_decay_then_archive(self) -> None:
        node_id = self.add("Release helper mentioned near package validation.", "association")
        refused = lifecycle.apply_transition(
            self.connection, node_id=node_id, transition="decay", at=self.now + 10 * DAY
        )
        self.assertFalse(refused.accepted)
        decayed = lifecycle.apply_transition(
            self.connection, node_id=node_id, transition="decay", at=self.now + 61 * DAY
        )
        self.assertTrue(decayed.accepted)
        self.assertEqual(self.status(node_id), "dormant")
        too_soon = lifecycle.apply_transition(
            self.connection, node_id=node_id, transition="archive", at=self.now + 62 * DAY
        )
        self.assertFalse(too_soon.accepted)
        archived = lifecycle.apply_transition(
            self.connection, node_id=node_id, transition="archive", at=self.now + 181 * DAY
        )
        self.assertTrue(archived.accepted)
        self.assertEqual(self.status(node_id), "archived")

    def test_t026_use_revives_a_dormant_node(self) -> None:
        node_id = self.add("Release helper mentioned near package validation.", "association")
        lifecycle.apply_transition(
            self.connection, node_id=node_id, transition="decay", at=self.now + 61 * DAY
        )
        revived = lifecycle.apply_transition(
            self.connection, node_id=node_id, transition="use", at=self.now + 62 * DAY
        )
        self.assertTrue(revived.accepted)
        self.assertEqual(self.status(node_id), "active")

    def test_t027_quarantined_nodes_are_not_usable(self) -> None:
        node_id = self.add("A page claims the port is 9999.", origin_class="untrusted")
        self.assertEqual(self.status(node_id), "quarantined")
        refused = lifecycle.apply_transition(
            self.connection, node_id=node_id, transition="use", at=self.now
        )
        self.assertFalse(refused.accepted)

    def test_t028_corroboration_releases_quarantine(self) -> None:
        node_id = self.add("A page claims the port is 9999.", origin_class="untrusted")
        refused = lifecycle.apply_transition(
            self.connection,
            node_id=node_id,
            transition="corroborate",
            corroborating_sources=1,
            at=self.now,
        )
        self.assertFalse(refused.accepted)
        accepted = lifecycle.apply_transition(
            self.connection,
            node_id=node_id,
            transition="corroborate",
            corroborating_sources=2,
            at=self.now,
        )
        self.assertTrue(accepted.accepted)
        self.assertEqual(self.status(node_id), "active")

    def test_t029_owner_confirmation_requires_the_owner(self) -> None:
        node_id = self.add("The owner prefers short summaries.", node_type="preference")
        self.assertEqual(self.status(node_id), "quarantined")
        refused = lifecycle.apply_transition(
            self.connection,
            node_id=node_id,
            transition="owner-confirm",
            actor="agent",
            at=self.now,
        )
        self.assertFalse(refused.accepted)
        self.assertIn("requires the owner", refused.reason)
        accepted = lifecycle.apply_transition(
            self.connection,
            node_id=node_id,
            transition="owner-confirm",
            actor="owner",
            at=self.now,
        )
        self.assertTrue(accepted.accepted)
        row = db.fetch_node(self.connection, node_id)
        self.assertEqual(str(row["owner_confirmation"]), "confirmed")
        self.assertEqual(str(row["confidence_basis"]), "owner-confirmed")

    def test_t030_owner_rejection_writes_a_tombstone(self) -> None:
        node_id = self.add("The owner prefers loud notifications.", node_type="preference")
        result = lifecycle.apply_transition(
            self.connection,
            node_id=node_id,
            transition="owner-reject",
            actor="owner",
            at=self.now,
        )
        self.assertTrue(result.accepted)
        row = db.fetch_node(self.connection, node_id)
        self.assertEqual(str(row["retention_status"]), "deleted")
        self.assertEqual(str(row["content"]), "")
        self.assertTrue(str(row["content_hash"]))

    def test_t031_facts_change_only_by_explicit_invalidation(self) -> None:
        node_id = self.add("The broker listens on port 11434.")
        result = lifecycle.apply_transition(
            self.connection,
            node_id=node_id,
            transition="invalidate",
            at=self.now + DAY,
            valid_to=self.now + DAY,
        )
        self.assertTrue(result.accepted)
        row = db.fetch_node(self.connection, node_id)
        self.assertEqual(str(row["retention_status"]), "superseded")
        self.assertEqual(int(row["valid_to"]), self.now + DAY)

    def test_t032_preferences_are_superseded_in_place(self) -> None:
        old_id = self.add("Bind the gateway to the LAN address.", node_type="preference")
        new_id = self.add("Keep the gateway on loopback only.", node_type="preference")
        result = lifecycle.supersede(
            self.connection, old_node_id=old_id, new_node_id=new_id, at=self.now + DAY
        )
        self.assertTrue(result.accepted)
        old_row = db.fetch_node(self.connection, old_id)
        self.assertEqual(str(old_row["retention_status"]), "superseded")
        self.assertEqual(str(old_row["superseded_by"]), new_id)
        edge = self.connection.execute(
            "SELECT * FROM edges WHERE edge_type = 'supersedes'"
        ).fetchone()
        self.assertEqual(str(edge["src_id"]), new_id)
        self.assertEqual(str(edge["dst_id"]), old_id)

    def test_t033_supersede_requires_a_successor(self) -> None:
        node_id = self.add("Bind the gateway to the LAN address.", node_type="preference")
        refused = lifecycle.apply_transition(
            self.connection, node_id=node_id, transition="supersede", at=self.now
        )
        self.assertFalse(refused.accepted)
        self.assertIn("successor", refused.reason)

    def test_t034_deletion_requires_the_owner_and_a_retired_state(self) -> None:
        node_id = self.add("The broker listens on port 11434.")
        refused_active = lifecycle.apply_transition(
            self.connection,
            node_id=node_id,
            transition="delete",
            actor="owner",
            at=self.now,
        )
        self.assertFalse(refused_active.accepted)
        lifecycle.apply_transition(
            self.connection, node_id=node_id, transition="invalidate", at=self.now
        )
        refused_agent = lifecycle.apply_transition(
            self.connection,
            node_id=node_id,
            transition="delete",
            actor="agent",
            at=self.now,
        )
        self.assertFalse(refused_agent.accepted)
        accepted = lifecycle.apply_transition(
            self.connection,
            node_id=node_id,
            transition="delete",
            actor="owner",
            at=self.now,
        )
        self.assertTrue(accepted.accepted)
        self.assertEqual(self.status(node_id), "deleted")

    def test_t035_tombstones_cannot_transition(self) -> None:
        node_id = self.add("A page claims the port is 9999.", origin_class="untrusted")
        lifecycle.apply_transition(
            self.connection,
            node_id=node_id,
            transition="owner-reject",
            actor="owner",
            at=self.now,
        )
        for transition in ("use", "corroborate", "revive", "invalidate"):
            with self.subTest(transition=transition):
                refused = lifecycle.apply_transition(
                    self.connection,
                    node_id=node_id,
                    transition=transition,
                    actor="owner",
                    at=self.now,
                )
                self.assertFalse(refused.accepted)

    def test_t036_unknown_transition_is_refused_and_journalled(self) -> None:
        node_id = self.add("The broker listens on port 11434.")
        refused = lifecycle.apply_transition(
            self.connection, node_id=node_id, transition="promote", at=self.now
        )
        self.assertFalse(refused.accepted)
        self.assertIn("unknown transition", refused.reason)
        row = self.connection.execute(
            "SELECT transition, accepted FROM node_history WHERE node_id = ? "
            "ORDER BY seq DESC LIMIT 1",
            (node_id,),
        ).fetchone()
        self.assertEqual(str(row["transition"]), "promote")
        self.assertEqual(int(row["accepted"]), 0)

    def test_t037_refused_transition_on_missing_node_is_journalled(self) -> None:
        refused = lifecycle.apply_transition(
            self.connection, node_id="nd_missing", transition="use", at=self.now
        )
        self.assertFalse(refused.accepted)
        row = self.connection.execute(
            "SELECT reason FROM node_history WHERE node_id = 'nd_missing'"
        ).fetchone()
        self.assertEqual(str(row["reason"]), "node not found")

    def test_t038_sweep_is_idempotent(self) -> None:
        self.add("Release helper mentioned near package validation.", "association")
        later = self.now + 200 * DAY
        first = lifecycle.sweep(self.connection, at=later)
        second = lifecycle.sweep(self.connection, at=later)
        self.assertEqual(first["dormant"], 1)
        self.assertEqual(second["dormant"], 0)

    def test_t039_sweep_retires_expired_validity(self) -> None:
        node_id = self.add(
            "Study block runs weekday mornings this term.",
            valid_to=self.now + 5 * DAY,
        )
        counts = lifecycle.sweep(self.connection, at=self.now + 6 * DAY)
        self.assertEqual(counts["outdated"], 1)
        self.assertEqual(self.status(node_id), "superseded")

    def test_t040_edges_carry_provenance_and_confidence(self) -> None:
        left = self.add("The broker listens on port 11434.")
        right = self.add("Embeddings come from nomic-embed-text.")
        edge_id = lifecycle.add_edge(
            self.connection,
            src_id=left,
            dst_id=right,
            edge_type="supports",
            confidence=0.7,
            confidence_basis="agent-inference",
            at=self.now,
        )
        row = self.connection.execute(
            "SELECT * FROM edges WHERE id = ?", (edge_id,)
        ).fetchone()
        self.assertEqual(float(row["confidence"]), 0.7)
        self.assertEqual(str(row["confidence_basis"]), "agent-inference")
        self.assertEqual(str(row["captured_by"]), "agent")
        self.assertEqual(str(row["rule_version"]), rules_module.RULE_VERSION)

    def test_t041_unknown_edge_type_is_a_programmer_error(self) -> None:
        left = self.add("The broker listens on port 11434.")
        right = self.add("Embeddings come from nomic-embed-text.")
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.add_edge(
                self.connection,
                src_id=left,
                dst_id=right,
                edge_type="causes",
                confidence=0.5,
                confidence_basis="agent-inference",
            )


if __name__ == "__main__":
    unittest.main()
