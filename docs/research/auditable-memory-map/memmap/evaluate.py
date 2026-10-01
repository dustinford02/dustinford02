"""The evaluation set runner.

Two rates are scored separately because they fail in opposite directions:

  harmful recall      a stale, superseded, quarantined, archived or refused item
                      was returned. The threshold is zero. One is a failure.
  harmful forgetting  an item the map holds, and that the case says should
                      surface, did not. Some tolerance is allowed.

The runner builds its own fixture map in a temporary database so a run never
touches the live map, and it never calls a model: every case is decided by
comparing returned node ids against the case expectations.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from . import admission, db, detectors, lifecycle, retrieval
from . import rules as rules_module

DEFAULT_EVAL_SET = Path(__file__).resolve().parent.parent / "eval" / "eval_set.json"


def load_eval_set(path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path) if path else DEFAULT_EVAL_SET
    return json.loads(target.read_text(encoding="utf-8"))


def build_fixture_map(
    db_path: str | Path, eval_set: dict[str, Any], *, now: int
) -> tuple[sqlite3.Connection, dict[str, str]]:
    """Create a map populated with the evaluation fixtures."""
    connection = db.initialize(db_path)
    day = rules_module.DAY_MS
    identifiers: dict[str, str] = {}

    for fixture in eval_set["fixtures"]:
        created_offset = int(fixture.get("created_days_ago", 0)) * day
        valid_from = (
            now - int(fixture["valid_from_days_ago"]) * day
            if "valid_from_days_ago" in fixture
            else None
        )
        valid_to = (
            now - int(fixture["valid_to_days_ago"]) * day
            if "valid_to_days_ago" in fixture
            else None
        )
        node_id, _decision = lifecycle.admit(
            connection,
            content=str(fixture["content"]),
            node_type=str(fixture["node_type"]),
            origin_class=str(fixture["origin_class"]),
            session_kind="interactive",
            source_ref=fixture.get("source_ref"),
            captured_by="adapter",
            importance=fixture.get("importance"),
            valid_from=valid_from,
            valid_to=valid_to,
            actor="adapter",
            at=now - created_offset,
        )
        if node_id is not None:
            identifiers[str(fixture["id"])] = node_id

    for fixture in eval_set["fixtures"]:
        successor_key = fixture.get("supersede_with")
        if not successor_key:
            continue
        old_id = identifiers.get(str(fixture["id"]))
        new_id = identifiers.get(str(successor_key))
        if old_id and new_id:
            lifecycle.supersede(
                connection, old_node_id=old_id, new_node_id=new_id, actor="owner", at=now
            )

    # Age the weak association past the decay and archive thresholds, then sweep,
    # so the retention case exercises the real state machine rather than a flag.
    lifecycle.sweep(connection, actor="agent", at=now)
    connection.commit()
    return connection, identifiers


def run(
    db_path: str | Path,
    *,
    eval_set_path: str | Path | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    eval_set = load_eval_set(eval_set_path)
    moment = now if now is not None else db.now_ms()
    target = Path(db_path)
    if target.exists():
        target.unlink()
    connection, identifiers = build_fixture_map(target, eval_set, now=moment)
    thresholds = eval_set["thresholds"]

    retrieval_cases: list[dict[str, Any]] = []
    harmful_recall = 0
    harmful_forgetting = 0
    recall_hits = 0
    recall_total = 0
    returned_chars_max = 0

    try:
        for case in eval_set["cases"]:
            outcome = retrieval.search(
                connection,
                query=str(case["query"]),
                agent_session="eval",
                at=moment,
            )
            returned_ids = {candidate.node_id for candidate in outcome.returned}
            returned_chars = sum(len(candidate.content) for candidate in outcome.returned)
            returned_chars_max = max(returned_chars_max, returned_chars)

            expected = {
                identifiers[key]
                for key in case.get("expect_returned", [])
                if key in identifiers
            }
            forbidden = {
                identifiers[key]
                for key in case.get("expect_absent", [])
                if key in identifiers
            }

            leaked = sorted(returned_ids & forbidden)
            # An abstention case has to come back empty. Returning something
            # unrelated to a question the map cannot answer is a recall failure,
            # not a neutral outcome.
            if case.get("expect_empty") and returned_ids:
                leaked = sorted(returned_ids | set(leaked))
            missed = sorted(expected - returned_ids)
            if leaked:
                harmful_recall += 1
            if missed:
                harmful_forgetting += 1
            if expected:
                recall_total += 1
                if not missed:
                    recall_hits += 1

            retrieval_cases.append(
                {
                    "id": str(case["id"]),
                    "ability": str(case["ability"]),
                    "query": str(case["query"]),
                    "trace_id": outcome.trace_id,
                    "returned": sorted(returned_ids),
                    "harmful_recall": leaked,
                    "harmful_forgetting": missed,
                    "returned_chars": returned_chars,
                    "passed": not leaked and not missed,
                }
            )

        admission_cases: list[dict[str, Any]] = []
        rules = db.active_rules(connection)
        for case in eval_set["admission_cases"]:
            decision = admission.evaluate(
                content=str(case["content"]),
                node_type=str(case["node_type"]),
                origin_class=str(case["origin_class"]),
                session_kind="interactive",
                source_ref=None,
                rules=rules,
            )
            decision_ok = decision.decision == str(case["expect_decision"])
            class_ok = decision.redaction_class == case.get("expect_class")
            if not decision_ok and str(case["expect_decision"]) == "rejected":
                harmful_recall += 1
            admission_cases.append(
                {
                    "id": str(case["id"]),
                    "ability": str(case["ability"]),
                    "expected": str(case["expect_decision"]),
                    "actual": decision.decision,
                    "expected_class": case.get("expect_class"),
                    "actual_class": decision.redaction_class,
                    "reason_code": decision.reason_code,
                    "passed": decision_ok and class_ok,
                }
            )

        detector_result = detectors.run_all(connection, at=moment)
        connection.commit()
    finally:
        connection.close()

    total_scored = len(retrieval_cases) + len(eval_set["admission_cases"])
    harmful_recall_rate = harmful_recall / total_scored if total_scored else 0.0
    harmful_forgetting_rate = (
        harmful_forgetting / len(retrieval_cases) if retrieval_cases else 0.0
    )
    recall_at_k = recall_hits / recall_total if recall_total else 1.0

    failures: list[str] = []
    if harmful_recall_rate > float(thresholds["max_harmful_recall_rate"]):
        failures.append(
            f"harmful recall rate {harmful_recall_rate:.3f} exceeds "
            f"{float(thresholds['max_harmful_recall_rate']):.3f}"
        )
    if harmful_forgetting_rate > float(thresholds["max_harmful_forgetting_rate"]):
        failures.append(
            f"harmful forgetting rate {harmful_forgetting_rate:.3f} exceeds "
            f"{float(thresholds['max_harmful_forgetting_rate']):.3f}"
        )
    if recall_at_k < float(thresholds["min_recall_at_k"]):
        failures.append(
            f"recall@k {recall_at_k:.3f} is below {float(thresholds['min_recall_at_k']):.3f}"
        )
    if returned_chars_max > int(thresholds["max_returned_chars"]):
        failures.append(
            f"returned context {returned_chars_max} chars exceeds "
            f"{int(thresholds['max_returned_chars'])}"
        )
    failed_cases = [case["id"] for case in retrieval_cases if not case["passed"]]
    failed_admissions = [case["id"] for case in admission_cases if not case["passed"]]
    if failed_admissions:
        failures.append(f"admission cases failed: {', '.join(failed_admissions)}")

    return {
        "eval_set": str(eval_set["name"]),
        "passed": not failures,
        "summary": "; ".join(failures) if failures else "all thresholds met",
        "metrics": {
            "harmful_recall_rate": round(harmful_recall_rate, 6),
            "harmful_forgetting_rate": round(harmful_forgetting_rate, 6),
            "recall_at_k": round(recall_at_k, 6),
            "max_returned_chars": returned_chars_max,
            "cases_scored": total_scored,
        },
        "failed_retrieval_cases": failed_cases,
        "failed_admission_cases": failed_admissions,
        "retrieval_cases": retrieval_cases,
        "admission_cases": admission_cases,
        "detector_counts": {
            kind: len(records) for kind, records in detector_result.items()
        },
    }
