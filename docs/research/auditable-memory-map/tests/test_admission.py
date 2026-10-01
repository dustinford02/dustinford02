"""Admission gate tests: one per refusal class and one per admitted state."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memmap import admission, db, lifecycle  # noqa: E402
from memmap import rules as rules_module  # noqa: E402


class AdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.connection = db.initialize(Path(self.directory.name) / "map.sqlite")
        self.rules = db.active_rules(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.directory.cleanup()

    def decide(self, content: str, **overrides):
        parameters = {
            "content": content,
            "node_type": "fact",
            "origin_class": "owner",
            "session_kind": "interactive",
            "source_ref": None,
            "rules": self.rules,
        }
        parameters.update(overrides)
        return admission.evaluate(**parameters)

    def test_t001_rejects_credentials(self) -> None:
        decision = self.decide("api_key = sk-abcdefghijklmnopqrstuvwx0123")
        self.assertEqual(decision.decision, "rejected")
        self.assertEqual(decision.redaction_class, "credential")

    def test_t002_rejects_pay(self) -> None:
        decision = self.decide("Owner salary is confidential but recorded here.")
        self.assertEqual(decision.decision, "rejected")
        self.assertEqual(decision.redaction_class, "pay")

    def test_t003_rejects_health(self) -> None:
        decision = self.decide("Prescribed 20 mg daily after the diagnosis.")
        self.assertEqual(decision.decision, "rejected")
        self.assertEqual(decision.redaction_class, "health")

    def test_t004_rejects_case_details(self) -> None:
        decision = self.decide("Case no. 24-cv-00912 deposition is scheduled.")
        self.assertEqual(decision.decision, "rejected")
        self.assertEqual(decision.redaction_class, "case")

    def test_t005_rejects_contact_details(self) -> None:
        decision = self.decide("Reach the owner at owner@example.com")
        self.assertEqual(decision.decision, "rejected")
        self.assertEqual(decision.redaction_class, "contact")

    def test_t006_rejects_refused_source_path(self) -> None:
        local_rules = dict(self.rules)
        local_rules["admission"] = dict(self.rules["admission"])
        local_rules["admission"]["rejected_path_prefixes"] = ["shares/legal"]
        decision = admission.evaluate(
            content="A neutral looking sentence from a refused root.",
            node_type="fact",
            origin_class="owner",
            session_kind="interactive",
            source_ref="shares/legal/matter/notes.md#L3-L3",
            rules=local_rules,
        )
        self.assertEqual(decision.decision, "rejected")
        self.assertEqual(decision.reason_code, "refused-source-path")

    def test_t007_rejects_non_promotable_session_kinds(self) -> None:
        for kind in ("cron", "heartbeat", "subagent"):
            with self.subTest(kind=kind):
                decision = self.decide("A heartbeat restatement.", session_kind=kind)
                self.assertEqual(decision.decision, "rejected")
                self.assertEqual(decision.reason_code, "non-promotable-session-kind")

    def test_t008_rejects_system_origin(self) -> None:
        decision = self.decide("Cron preamble scaffolding.", origin_class="system")
        self.assertEqual(decision.decision, "rejected")

    def test_t009_quarantines_untrusted_origin(self) -> None:
        decision = self.decide("A web page claims the port is 9999.", origin_class="untrusted")
        self.assertEqual(decision.decision, "quarantined")
        self.assertEqual(decision.quarantine_reason, "untrusted-origin")
        self.assertEqual(decision.confidence_basis, "untrusted-uncorroborated")

    def test_t010_admits_corroborated_untrusted_origin(self) -> None:
        decision = self.decide(
            "Two independent pages agree the port is 11434.",
            origin_class="untrusted",
            corroborating_sources=2,
        )
        self.assertEqual(decision.decision, "admitted")
        self.assertEqual(decision.confidence_basis, "multi-source-corroborated")

    def test_t011_quarantines_unconfirmed_owner_fact(self) -> None:
        decision = self.decide("The owner prefers short written summaries.", node_type="preference")
        self.assertEqual(decision.decision, "quarantined")
        self.assertEqual(decision.quarantine_reason, "awaiting-owner-confirmation")
        self.assertEqual(decision.owner_confirmation, "pending")

    def test_t012_admits_confirmed_owner_fact(self) -> None:
        decision = self.decide(
            "The owner prefers short written summaries.",
            node_type="preference",
            owner_confirmed=True,
        )
        self.assertEqual(decision.decision, "admitted")
        self.assertEqual(decision.confidence_basis, "owner-confirmed")

    def test_t013_rejects_empty_and_oversized(self) -> None:
        self.assertEqual(self.decide("   ").reason_code, "empty-content")
        limit = int(self.rules["admission"]["max_content_chars"])
        self.assertEqual(self.decide("x" * (limit + 1)).reason_code, "content-too-long")

    def test_t014_refused_content_is_never_stored(self) -> None:
        node_id, decision = lifecycle.admit(
            self.connection,
            content="api_key = sk-abcdefghijklmnopqrstuvwx0123",
            node_type="fact",
            origin_class="owner",
        )
        self.assertIsNone(node_id)
        self.assertEqual(decision.decision, "rejected")
        rows = self.connection.execute("SELECT COUNT(*) AS n FROM nodes").fetchone()
        self.assertEqual(int(rows["n"]), 0)
        logged = self.connection.execute(
            "SELECT decision, redaction_class, content_hash FROM admission_log"
        ).fetchone()
        self.assertEqual(str(logged["decision"]), "rejected")
        self.assertEqual(str(logged["redaction_class"]), "credential")
        self.assertTrue(str(logged["content_hash"]))

    def test_t015_admission_is_idempotent(self) -> None:
        first, _ = lifecycle.admit(
            self.connection,
            content="The broker listens on port 11434.",
            node_type="fact",
            origin_class="owner",
            source_ref="MEMORY.md#L4-L4",
        )
        second, _ = lifecycle.admit(
            self.connection,
            content="The broker listens on port 11434.",
            node_type="fact",
            origin_class="owner",
            source_ref="MEMORY.md#L4-L4",
        )
        self.assertEqual(first, second)
        count = self.connection.execute("SELECT COUNT(*) AS n FROM nodes").fetchone()
        self.assertEqual(int(count["n"]), 1)
        refusal = self.connection.execute(
            "SELECT reason FROM node_history WHERE accepted = 0 ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        self.assertIn("already-present", str(refusal["reason"]))

    def test_t016_every_node_names_a_rule_version(self) -> None:
        node_id, _ = lifecycle.admit(
            self.connection,
            content="Session transcripts live under the sessions directory.",
            node_type="fact",
            origin_class="agent",
        )
        row = db.fetch_node(self.connection, str(node_id))
        self.assertEqual(str(row["rule_version"]), rules_module.RULE_VERSION)


if __name__ == "__main__":
    unittest.main()
