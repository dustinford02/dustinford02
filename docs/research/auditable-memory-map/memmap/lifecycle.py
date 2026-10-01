"""The lifecycle state machine.

Every write to `nodes` goes through this module. A model may propose a
transition; `apply_transition` decides whether the rules allow it, and either way
appends a row to `node_history`. A refused proposal is recorded as refused, so a
reviewer can see what the agent tried to do as well as what happened.

Transition table (actor-independent unless stated):

    (none)        --admit-->                active | quarantined
    quarantined   --corroborate-->          active          (>= N independent sources)
    quarantined   --owner-confirm-->        active          (owner only)
    quarantined   --owner-reject-->         deleted         (owner only, tombstone)
    active        --use-->                  active
    active        --reinforce-->            reinforced      (>= N uses in window)
    reinforced    --use-->                  reinforced
    active        --decay-->                dormant         (decay-eligible types only)
    reinforced    --decay-->                dormant         (decay-eligible types only)
    dormant       --use-->                  active
    dormant       --archive-->              archived
    archived      --revive-->               active          (owner only)
    any(active-ish)--invalidate-->          superseded      (explicit valid_to)
    any(active-ish)--supersede-->           superseded      (successor node required)
    archived      --delete-->               deleted         (owner only, tombstone)
    quarantined   --delete-->               deleted         (owner only, tombstone)

Facts never move to dormant or archived on a timer: the only way a fact leaves
active is explicit invalidation or supersession. Preferences are superseded in
place by a successor node. Weak associations may decay.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from . import admission, db, ids
from . import rules as rules_module

ACTIVE_LIKE = ("active", "reinforced", "dormant")


@dataclass(frozen=True)
class TransitionResult:
    accepted: bool
    node_id: str
    from_status: str | None
    to_status: str | None
    reason: str

    def to_json(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "node_id": self.node_id,
            "from_status": self.from_status,
            "to_status": self.to_status,
            "reason": self.reason,
        }


class LifecycleError(RuntimeError):
    """Raised only for programmer errors, never for a refused transition."""


def admit(
    connection: sqlite3.Connection,
    *,
    content: str,
    node_type: str,
    origin_class: str,
    session_kind: str = "interactive",
    source_ref: str | None = None,
    source_session_id: str | None = None,
    captured_by: str = "agent",
    importance: int | None = None,
    trigger_phrases: str | None = None,
    project_key: str | None = None,
    valid_from: int | None = None,
    valid_to: int | None = None,
    observed_at: int | None = None,
    corroborating_sources: int = 1,
    owner_confirmed: bool = False,
    actor: str = "agent",
    at: int | None = None,
) -> tuple[str | None, admission.AdmissionDecision]:
    """Run the admission gate and insert the node when it passes.

    Returns the node id (None when refused) and the decision, so the caller can
    report the reason code without re-deriving it.
    """
    if node_type not in rules_module.NODE_TYPES:
        raise LifecycleError(f"unknown node type {node_type!r}")

    rules = db.active_rules(connection)
    rule_version = str(rules["version"])
    moment = at if at is not None else db.now_ms()

    decision = admission.evaluate(
        content=content,
        node_type=node_type,
        origin_class=origin_class,
        session_kind=session_kind,
        source_ref=source_ref,
        rules=rules,
        corroborating_sources=corroborating_sources,
        owner_confirmed=owner_confirmed,
    )

    if not decision.admitted:
        admission.log_decision(
            connection,
            decision,
            content=content,
            origin_class=origin_class,
            source_ref=source_ref,
            node_id=None,
            rule_version=rule_version,
            at=moment,
        )
        return None, decision

    new_id = ids.node_id(node_type, content, source_ref)
    existing = db.fetch_node(connection, new_id)
    if existing is not None:
        admission.log_decision(
            connection,
            decision,
            content=content,
            origin_class=origin_class,
            source_ref=source_ref,
            node_id=new_id,
            rule_version=rule_version,
            at=moment,
        )
        db.record_history(
            connection,
            node_id=new_id,
            transition="admit",
            from_status=str(existing["retention_status"]),
            to_status=str(existing["retention_status"]),
            actor=actor,
            accepted=False,
            reason="already-present: identical type, content and source",
            rule_version=rule_version,
            at=moment,
        )
        return new_id, decision

    connection.execute(
        """
        INSERT INTO nodes (
          id, node_type, content, content_norm, content_hash,
          origin_class, session_kind, source_ref, source_session_id, captured_by,
          valid_from, valid_to, recorded_at, observed_at, superseded_by,
          confidence, confidence_basis,
          importance, trigger_phrases, project_key, subject_key,
          use_count, last_used_at, last_reinforced_at,
          retention_status, quarantine_reason, owner_confirmation, about_owner,
          created_at, updated_at, rule_version
        ) VALUES (
          ?, ?, ?, ?, ?,
          ?, ?, ?, ?, ?,
          ?, ?, ?, ?, NULL,
          ?, ?,
          ?, ?, ?, ?,
          0, NULL, NULL,
          ?, ?, ?, ?,
          ?, ?, ?
        )
        """,
        (
            new_id,
            node_type,
            content,
            ids.normalize_exact(content),
            ids.content_hash(content),
            origin_class,
            session_kind,
            source_ref,
            source_session_id,
            captured_by,
            valid_from,
            valid_to,
            moment,
            observed_at if observed_at is not None else moment,
            decision.confidence,
            decision.confidence_basis,
            importance,
            trigger_phrases,
            project_key,
            ids.subject_key(content),
            decision.retention_status,
            decision.quarantine_reason,
            decision.owner_confirmation,
            1 if decision.about_owner else 0,
            moment,
            moment,
            rule_version,
        ),
    )
    _index_node(connection, new_id, content)
    admission.log_decision(
        connection,
        decision,
        content=content,
        origin_class=origin_class,
        source_ref=source_ref,
        node_id=new_id,
        rule_version=rule_version,
        at=moment,
    )
    db.record_history(
        connection,
        node_id=new_id,
        transition="admit",
        from_status=None,
        to_status=decision.retention_status,
        actor=actor,
        accepted=True,
        reason=decision.reason_code,
        after={"retention_status": decision.retention_status},
        rule_version=rule_version,
        at=moment,
    )
    return new_id, decision


def _index_node(connection: sqlite3.Connection, node_id: str, content: str) -> None:
    if not _fts_available(connection):
        return
    connection.execute("DELETE FROM nodes_fts WHERE node_id = ?", (node_id,))
    connection.execute(
        "INSERT INTO nodes_fts (content, node_id) VALUES (?, ?)",
        (ids.strip_annotations(content), node_id),
    )


def _fts_available(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'nodes_fts'"
    ).fetchone()
    return row is not None


def _allowed(
    transition: str,
    node: sqlite3.Row,
    rules: dict[str, Any],
    actor: str,
    successor_id: str | None,
    corroborating_sources: int,
    now: int,
) -> tuple[bool, str | None, str]:
    """Return (allowed, target_status, reason)."""
    status = str(node["retention_status"])
    node_type = str(node["node_type"])
    lifecycle = rules["lifecycle"]
    decay_types = set(lifecycle["decay_eligible_types"])

    if status == "deleted":
        return False, None, "node is a tombstone and cannot transition"

    if transition == "use":
        if status in {"active", "reinforced"}:
            return True, status, "use recorded"
        if status == "dormant":
            return True, "active", "use revives a dormant node"
        if status == "quarantined":
            return False, None, "quarantined nodes are not usable"
        if status in {"archived", "superseded"}:
            return False, None, f"{status} nodes are not usable"
        return False, None, f"unexpected status {status}"

    if transition == "reinforce":
        if status not in {"active", "reinforced"}:
            return False, None, f"cannot reinforce from {status}"
        uses = int(node["use_count"])
        window_ms = int(lifecycle["reinforce_window_days"]) * rules_module.DAY_MS
        last_used = node["last_used_at"]
        in_window = last_used is not None and (now - int(last_used)) <= window_ms
        if uses >= int(lifecycle["reinforce_min_uses"]) and in_window:
            return True, "reinforced", f"{uses} uses within the reinforcement window"
        return False, None, (
            f"needs {lifecycle['reinforce_min_uses']} uses inside "
            f"{lifecycle['reinforce_window_days']} days, has {uses}"
        )

    if transition == "corroborate":
        if status != "quarantined":
            return False, None, "only quarantined nodes are corroborated"
        required = int(lifecycle["corroboration_sources_required"])
        if corroborating_sources >= required:
            return True, "active", f"{corroborating_sources} independent sources"
        return False, None, (
            f"needs {required} independent sources, has {corroborating_sources}"
        )

    if transition == "owner-confirm":
        if actor != "owner":
            return False, None, "owner confirmation requires the owner as actor"
        if status != "quarantined":
            return False, None, "only quarantined nodes await confirmation"
        return True, "active", "owner confirmed"

    if transition == "owner-reject":
        if actor != "owner":
            return False, None, "owner rejection requires the owner as actor"
        if status != "quarantined":
            return False, None, "only quarantined nodes can be rejected"
        return True, "deleted", "owner rejected"

    if transition == "decay":
        if node_type not in decay_types:
            return False, None, (
                f"{node_type} nodes do not decay; they change by invalidation "
                "or supersession"
            )
        if status not in {"active", "reinforced"}:
            return False, None, f"cannot decay from {status}"
        idle_ms = int(lifecycle["dormant_after_days"]) * rules_module.DAY_MS
        reference = node["last_used_at"] or node["created_at"]
        if (now - int(reference)) >= idle_ms:
            return True, "dormant", f"idle for {lifecycle['dormant_after_days']} days"
        return False, None, "not idle long enough to decay"

    if transition == "archive":
        if node_type not in decay_types:
            return False, None, f"{node_type} nodes are not archived on a timer"
        if status != "dormant":
            return False, None, "only dormant nodes are archived"
        idle_ms = int(lifecycle["archive_after_days"]) * rules_module.DAY_MS
        reference = node["last_used_at"] or node["created_at"]
        if (now - int(reference)) >= idle_ms:
            return True, "archived", f"idle for {lifecycle['archive_after_days']} days"
        return False, None, "not idle long enough to archive"

    if transition == "revive":
        if actor != "owner":
            return False, None, "reviving an archived node requires the owner"
        if status != "archived":
            return False, None, "only archived nodes are revived"
        return True, "active", "owner revived"

    if transition == "invalidate":
        if status not in ACTIVE_LIKE:
            return False, None, f"cannot invalidate from {status}"
        return True, "superseded", "explicitly invalidated"

    if transition == "supersede":
        if status not in ACTIVE_LIKE:
            return False, None, f"cannot supersede from {status}"
        if not successor_id:
            return False, None, "supersession requires a successor node id"
        return True, "superseded", f"superseded by {successor_id}"

    if transition == "delete":
        if actor != "owner":
            return False, None, "deletion requires the owner as actor"
        if status not in {"archived", "quarantined", "superseded"}:
            return False, None, (
                "only archived, quarantined or superseded nodes may be deleted"
            )
        return True, "deleted", "owner deleted"

    if transition == "quarantine":
        if status not in ACTIVE_LIKE:
            return False, None, f"cannot quarantine from {status}"
        return True, "quarantined", "flagged for review"

    return False, None, f"unknown transition {transition!r}"


def apply_transition(
    connection: sqlite3.Connection,
    *,
    node_id: str,
    transition: str,
    actor: str = "agent",
    successor_id: str | None = None,
    corroborating_sources: int = 0,
    valid_to: int | None = None,
    change_set_id: str | None = None,
    at: int | None = None,
) -> TransitionResult:
    """Attempt one transition. Always journals the attempt."""
    rules = db.active_rules(connection)
    rule_version = str(rules["version"])
    now = at if at is not None else db.now_ms()
    node = db.fetch_node(connection, node_id)
    if node is None:
        db.record_history(
            connection,
            node_id=node_id,
            transition=transition,
            from_status=None,
            to_status=None,
            actor=actor,
            accepted=False,
            reason="node not found",
            rule_version=rule_version,
            at=now,
        )
        return TransitionResult(False, node_id, None, None, "node not found")

    from_status = str(node["retention_status"])
    allowed, target, reason = _allowed(
        transition, node, rules, actor, successor_id, corroborating_sources, now
    )
    if not allowed or target is None:
        db.record_history(
            connection,
            node_id=node_id,
            transition=transition,
            from_status=from_status,
            to_status=None,
            actor=actor,
            accepted=False,
            reason=reason,
            rule_version=rule_version,
            at=now,
        )
        return TransitionResult(False, node_id, from_status, None, reason)

    before = {
        "retention_status": from_status,
        "use_count": int(node["use_count"]),
        "valid_to": node["valid_to"],
        "superseded_by": node["superseded_by"],
        "owner_confirmation": str(node["owner_confirmation"]),
    }
    updates: dict[str, Any] = {"retention_status": target, "updated_at": now}

    if transition == "use":
        updates["use_count"] = int(node["use_count"]) + 1
        updates["last_used_at"] = now
    elif transition == "reinforce":
        updates["last_reinforced_at"] = now
    elif transition == "owner-confirm":
        updates["owner_confirmation"] = "confirmed"
        updates["quarantine_reason"] = None
        updates["confidence"] = 0.95
        updates["confidence_basis"] = "owner-confirmed"
    elif transition == "owner-reject":
        updates["owner_confirmation"] = "rejected"
        updates["content"] = ""
        updates["quarantine_reason"] = None
    elif transition == "corroborate":
        updates["quarantine_reason"] = None
        updates["confidence_basis"] = "multi-source-corroborated"
        updates["confidence"] = max(0.5, float(node["confidence"]))
    elif transition == "invalidate":
        updates["valid_to"] = valid_to if valid_to is not None else now
    elif transition == "supersede":
        updates["superseded_by"] = successor_id
        updates["valid_to"] = valid_to if valid_to is not None else now
    elif transition == "delete":
        updates["content"] = ""
        updates["quarantine_reason"] = None
    elif transition == "quarantine":
        updates["quarantine_reason"] = "detector-flagged"

    assignments = ", ".join(f"{column} = ?" for column in updates)
    connection.execute(
        f"UPDATE nodes SET {assignments} WHERE id = ?",
        (*updates.values(), node_id),
    )
    if "content" in updates:
        if _fts_available(connection):
            connection.execute("DELETE FROM nodes_fts WHERE node_id = ?", (node_id,))

    db.record_history(
        connection,
        node_id=node_id,
        transition=transition,
        from_status=from_status,
        to_status=target,
        actor=actor,
        accepted=True,
        reason=reason,
        before=before,
        after={key: value for key, value in updates.items() if key != "updated_at"},
        change_set_id=change_set_id,
        rule_version=rule_version,
        at=now,
    )
    return TransitionResult(True, node_id, from_status, target, reason)


def add_edge(
    connection: sqlite3.Connection,
    *,
    src_id: str,
    dst_id: str,
    edge_type: str,
    confidence: float,
    confidence_basis: str,
    origin_class: str = "agent",
    captured_by: str = "agent",
    source_ref: str | None = None,
    evidence: dict[str, Any] | None = None,
    valid_from: int | None = None,
    valid_to: int | None = None,
    at: int | None = None,
) -> str | None:
    """Insert a typed edge with its own provenance and confidence."""
    if edge_type not in rules_module.EDGE_TYPES:
        raise LifecycleError(f"unknown edge type {edge_type!r}")
    if src_id == dst_id:
        return None
    for endpoint in (src_id, dst_id):
        if db.fetch_node(connection, endpoint) is None:
            return None
    rules = db.active_rules(connection)
    moment = at if at is not None else db.now_ms()
    identifier = ids.edge_id(src_id, dst_id, edge_type)
    connection.execute(
        """
        INSERT INTO edges (
          id, src_id, dst_id, edge_type, confidence, confidence_basis,
          origin_class, captured_by, source_ref, evidence_json,
          recorded_at, valid_from, valid_to, rule_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(src_id, dst_id, edge_type) DO UPDATE SET
          confidence = excluded.confidence,
          confidence_basis = excluded.confidence_basis,
          evidence_json = excluded.evidence_json,
          valid_to = excluded.valid_to
        """,
        (
            identifier,
            src_id,
            dst_id,
            edge_type,
            max(0.0, min(1.0, confidence)),
            confidence_basis,
            origin_class,
            captured_by,
            source_ref,
            json.dumps(evidence or {}, sort_keys=True),
            moment,
            valid_from,
            valid_to,
            str(rules["version"]),
        ),
    )
    return identifier


def supersede(
    connection: sqlite3.Connection,
    *,
    old_node_id: str,
    new_node_id: str,
    actor: str = "agent",
    valid_to: int | None = None,
    at: int | None = None,
) -> TransitionResult:
    """Supersede in place: mark the old node and record the directed edge.

    Preferences use this path. The old directive is retired rather than left
    beside the new one, because an append-only preference history reliably leaves
    a stale directive available to answer from.
    """
    result = apply_transition(
        connection,
        node_id=old_node_id,
        transition="supersede",
        actor=actor,
        successor_id=new_node_id,
        valid_to=valid_to,
        at=at,
    )
    if result.accepted:
        add_edge(
            connection,
            src_id=new_node_id,
            dst_id=old_node_id,
            edge_type="supersedes",
            confidence=0.9,
            confidence_basis="explicit-supersession",
            captured_by="owner" if actor == "owner" else "agent",
            at=at,
        )
    return result


def sweep(
    connection: sqlite3.Connection,
    *,
    actor: str = "agent",
    at: int | None = None,
) -> dict[str, int]:
    """Run the time-driven transitions. Idempotent; safe to call every session."""
    now = at if at is not None else db.now_ms()
    counts = {"reinforced": 0, "dormant": 0, "archived": 0, "outdated": 0}

    for row in connection.execute(
        "SELECT id FROM nodes WHERE retention_status IN ('active')"
    ).fetchall():
        if apply_transition(
            connection, node_id=str(row["id"]), transition="reinforce", actor=actor, at=now
        ).accepted:
            counts["reinforced"] += 1

    for row in connection.execute(
        "SELECT id FROM nodes WHERE retention_status IN ('active', 'reinforced')"
    ).fetchall():
        if apply_transition(
            connection, node_id=str(row["id"]), transition="decay", actor=actor, at=now
        ).accepted:
            counts["dormant"] += 1

    for row in connection.execute(
        "SELECT id FROM nodes WHERE retention_status = 'dormant'"
    ).fetchall():
        if apply_transition(
            connection, node_id=str(row["id"]), transition="archive", actor=actor, at=now
        ).accepted:
            counts["archived"] += 1

    for row in connection.execute(
        "SELECT id FROM nodes WHERE retention_status IN ('active', 'reinforced', 'dormant') "
        "AND valid_to IS NOT NULL AND valid_to <= ?",
        (now,),
    ).fetchall():
        if apply_transition(
            connection,
            node_id=str(row["id"]),
            transition="invalidate",
            actor=actor,
            at=now,
        ).accepted:
            counts["outdated"] += 1

    return counts
