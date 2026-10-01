"""The improvement loop: proposed change sets, evaluation, approval, rollback.

A change set is the only way a schema rule, a retention rule or a bulk edit
reaches the map. It must carry a diff, a reason, evidence ids, an expected effect
and an explicit rollback plan, and it cannot be applied until the evaluation set
passes and the owner approves. The agent may propose; it cannot approve.

Supported operations are intentionally few and reversible:
  archive_node        move an eligible node to archived
  supersede_node      retire a node in favour of a named successor
  merge_duplicate     supersede one near-duplicate in favour of the other
  resolve_finding     mark a finding resolved, naming this change set
  set_rule            replace one scalar inside the active rule set
"""

from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from typing import Any

from . import db, ids, lifecycle
from . import rules as rules_module

SUPPORTED_OPERATIONS = (
    "archive_node",
    "supersede_node",
    "merge_duplicate",
    "resolve_finding",
    "set_rule",
)


class ChangeSetError(ValueError):
    """Raised when a proposal is malformed. Rejections are returned, not raised."""


def propose(
    connection: sqlite3.Connection,
    *,
    title: str,
    reason: str,
    operations: list[dict[str, Any]],
    evidence_ids: list[str],
    expected_effect: str,
    author: str = "agent",
    at: int | None = None,
) -> dict[str, Any]:
    """Validate and store a proposal. Nothing is applied here."""
    moment = at if at is not None else db.now_ms()
    rules = db.active_rules(connection)
    if not title.strip():
        raise ChangeSetError("a change set needs a title")
    if not reason.strip():
        raise ChangeSetError("a change set needs a reason")
    if not expected_effect.strip():
        raise ChangeSetError("a change set needs an expected effect on the evaluation set")
    if not operations:
        raise ChangeSetError("a change set needs at least one operation")
    if not evidence_ids:
        raise ChangeSetError(
            "a change set needs evidence ids (finding ids, trace ids or node ids)"
        )

    rollback: list[dict[str, Any]] = []
    for operation in operations:
        kind = str(operation.get("op", ""))
        if kind not in SUPPORTED_OPERATIONS:
            raise ChangeSetError(f"unsupported operation {kind!r}")
        rollback.append(_rollback_for(connection, operation, rules))

    identifier = ids.change_set_id(title, moment)
    connection.execute(
        """
        INSERT INTO change_sets (
          id, created_at, author, title, reason, diff_json, evidence_ids_json,
          expected_effect, rollback_json, status, eval_result_json, applied_at,
          rolled_back_at, rule_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed', NULL, NULL, NULL, ?)
        ON CONFLICT(id) DO NOTHING
        """,
        (
            identifier,
            moment,
            author,
            title,
            reason,
            json.dumps(operations, sort_keys=True),
            json.dumps(sorted(evidence_ids)),
            expected_effect,
            json.dumps(rollback, sort_keys=True),
            str(rules["version"]),
        ),
    )
    return {"status": "proposed", "change_set_id": identifier, "operations": len(operations)}


def _rollback_for(
    connection: sqlite3.Connection, operation: dict[str, Any], rules: dict[str, Any]
) -> dict[str, Any]:
    kind = str(operation["op"])
    if kind in {"archive_node", "supersede_node", "merge_duplicate"}:
        node_id = str(operation.get("node_id", ""))
        node = db.fetch_node(connection, node_id)
        if node is None:
            raise ChangeSetError(f"operation names unknown node {node_id!r}")
        return {
            "op": "restore_node_state",
            "node_id": node_id,
            "retention_status": str(node["retention_status"]),
            "superseded_by": node["superseded_by"],
            "valid_to": node["valid_to"],
        }
    if kind == "resolve_finding":
        finding_id = str(operation.get("finding_id", ""))
        row = connection.execute(
            "SELECT status FROM findings WHERE id = ?", (finding_id,)
        ).fetchone()
        if row is None:
            raise ChangeSetError(f"operation names unknown finding {finding_id!r}")
        return {
            "op": "restore_finding_status",
            "finding_id": finding_id,
            "status": str(row["status"]),
        }
    if kind == "set_rule":
        path = list(operation.get("path", []))
        if not path:
            raise ChangeSetError("set_rule needs a path into the rule set")
        current: Any = rules
        for key in path:
            if not isinstance(current, dict) or key not in current:
                raise ChangeSetError(f"rule path {path} does not exist")
            current = current[key]
        if isinstance(current, (dict, list)):
            raise ChangeSetError("set_rule only replaces scalar rule values")
        return {"op": "set_rule", "path": path, "value": current}
    raise ChangeSetError(f"unsupported operation {kind!r}")


def record_evaluation(
    connection: sqlite3.Connection,
    change_set_id: str,
    result: dict[str, Any],
    *,
    at: int | None = None,
) -> dict[str, Any]:
    moment = at if at is not None else db.now_ms()
    row = connection.execute(
        "SELECT status FROM change_sets WHERE id = ?", (change_set_id,)
    ).fetchone()
    if row is None:
        return {"status": "not-found", "change_set_id": change_set_id}
    status = "evaluated" if result.get("passed") else "rejected"
    connection.execute(
        "UPDATE change_sets SET status = ?, eval_result_json = ? WHERE id = ?",
        (status, json.dumps(result, sort_keys=True), change_set_id),
    )
    db.record_history(
        connection,
        node_id=change_set_id,
        transition="evaluate-change-set",
        from_status=str(row["status"]),
        to_status=status,
        actor="agent",
        accepted=bool(result.get("passed")),
        reason=str(result.get("summary", "evaluation recorded")),
        after=result,
        change_set_id=change_set_id,
        at=moment,
    )
    return {"status": status, "change_set_id": change_set_id}


def apply(
    connection: sqlite3.Connection,
    change_set_id: str,
    *,
    owner_approved: bool,
    at: int | None = None,
) -> dict[str, Any]:
    """Apply a change set only when it is evaluated, passing and owner-approved."""
    moment = at if at is not None else db.now_ms()
    row = connection.execute(
        "SELECT * FROM change_sets WHERE id = ?", (change_set_id,)
    ).fetchone()
    if row is None:
        return {"status": "not-found", "change_set_id": change_set_id}
    if not owner_approved:
        return {
            "status": "refused",
            "reason": "owner approval is required before a change set is applied",
        }
    if str(row["status"]) not in {"evaluated", "approved"}:
        return {
            "status": "refused",
            "reason": (
                "change set must pass the evaluation set first; current status is "
                f"{row['status']}"
            ),
        }
    evaluation = json.loads(str(row["eval_result_json"] or "{}"))
    if not evaluation.get("passed"):
        return {
            "status": "refused",
            "reason": "the recorded evaluation did not pass; refusing to apply",
        }

    operations = json.loads(str(row["diff_json"]))
    applied: list[dict[str, Any]] = []
    for operation in operations:
        applied.append(_apply_one(connection, operation, change_set_id, moment))
    connection.execute(
        "UPDATE change_sets SET status = 'applied', applied_at = ? WHERE id = ?",
        (moment, change_set_id),
    )
    return {"status": "applied", "change_set_id": change_set_id, "operations": applied}


def _apply_one(
    connection: sqlite3.Connection,
    operation: dict[str, Any],
    change_set_id: str,
    at: int,
) -> dict[str, Any]:
    kind = str(operation["op"])
    if kind == "archive_node":
        node_id = str(operation["node_id"])
        result = lifecycle.apply_transition(
            connection,
            node_id=node_id,
            transition="archive",
            actor="owner",
            change_set_id=change_set_id,
            at=at,
        )
        if not result.accepted:
            result = lifecycle.apply_transition(
                connection,
                node_id=node_id,
                transition="decay",
                actor="owner",
                change_set_id=change_set_id,
                at=at,
            )
        return {"op": kind, **result.to_json()}
    if kind in {"supersede_node", "merge_duplicate"}:
        result = lifecycle.supersede(
            connection,
            old_node_id=str(operation["node_id"]),
            new_node_id=str(operation["successor_id"]),
            actor="owner",
            at=at,
        )
        return {"op": kind, **result.to_json()}
    if kind == "resolve_finding":
        connection.execute(
            "UPDATE findings SET status = 'resolved', updated_at = ?, "
            "resolved_by_change_set = ? WHERE id = ?",
            (at, change_set_id, str(operation["finding_id"])),
        )
        return {"op": kind, "finding_id": str(operation["finding_id"]), "accepted": True}
    if kind == "set_rule":
        new_version = _write_rule_version(
            connection, list(operation["path"]), operation["value"], change_set_id, at
        )
        return {"op": kind, "new_rule_version": new_version, "accepted": True}
    return {"op": kind, "accepted": False, "reason": "unsupported operation"}


def _write_rule_version(
    connection: sqlite3.Connection,
    path: list[Any],
    value: Any,
    change_set_id: str,
    at: int,
) -> str:
    current = deepcopy(db.active_rules(connection))
    cursor: Any = current
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = value
    new_version = f"{rules_module.RULE_VERSION}+{change_set_id[-8:]}"
    current["version"] = new_version
    connection.execute(
        """
        INSERT INTO rule_versions (version, created_at, rules_json, notes, active)
        VALUES (?, ?, ?, ?, 1)
        ON CONFLICT(version) DO UPDATE SET rules_json = excluded.rules_json, active = 1
        """,
        (
            new_version,
            at,
            json.dumps(current, sort_keys=True),
            f"applied by change set {change_set_id}: set {'.'.join(map(str, path))}",
        ),
    )
    connection.execute(
        "UPDATE rule_versions SET active = CASE WHEN version = ? THEN 1 ELSE 0 END",
        (new_version,),
    )
    return new_version


def rollback(
    connection: sqlite3.Connection, change_set_id: str, *, at: int | None = None
) -> dict[str, Any]:
    moment = at if at is not None else db.now_ms()
    row = connection.execute(
        "SELECT * FROM change_sets WHERE id = ?", (change_set_id,)
    ).fetchone()
    if row is None:
        return {"status": "not-found", "change_set_id": change_set_id}
    if str(row["status"]) != "applied":
        return {
            "status": "refused",
            "reason": f"only an applied change set can be rolled back (status {row['status']})",
        }
    steps = json.loads(str(row["rollback_json"]))
    undone: list[dict[str, Any]] = []
    for step in steps:
        kind = str(step["op"])
        if kind == "restore_node_state":
            node_id = str(step["node_id"])
            node = db.fetch_node(connection, node_id)
            if node is None:
                undone.append({"op": kind, "node_id": node_id, "accepted": False})
                continue
            connection.execute(
                "UPDATE nodes SET retention_status = ?, superseded_by = ?, valid_to = ?, "
                "updated_at = ? WHERE id = ?",
                (
                    step["retention_status"],
                    step["superseded_by"],
                    step["valid_to"],
                    moment,
                    node_id,
                ),
            )
            db.record_history(
                connection,
                node_id=node_id,
                transition="rollback",
                from_status=str(node["retention_status"]),
                to_status=str(step["retention_status"]),
                actor="owner",
                accepted=True,
                reason=f"rolled back by change set {change_set_id}",
                change_set_id=change_set_id,
                at=moment,
            )
            undone.append({"op": kind, "node_id": node_id, "accepted": True})
        elif kind == "restore_finding_status":
            connection.execute(
                "UPDATE findings SET status = ?, resolved_by_change_set = NULL, "
                "updated_at = ? WHERE id = ?",
                (step["status"], moment, str(step["finding_id"])),
            )
            undone.append({"op": kind, "finding_id": step["finding_id"], "accepted": True})
        elif kind == "set_rule":
            _write_rule_version(
                connection, list(step["path"]), step["value"], change_set_id, moment
            )
            undone.append({"op": kind, "path": step["path"], "accepted": True})
    connection.execute(
        "UPDATE change_sets SET status = 'rolled-back', rolled_back_at = ? WHERE id = ?",
        (moment, change_set_id),
    )
    return {"status": "rolled-back", "change_set_id": change_set_id, "steps": undone}


def listing(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT id, created_at, author, title, status, expected_effect "
        "FROM change_sets ORDER BY created_at DESC, id"
    ).fetchall()
    return db.rows_to_dicts(rows)
