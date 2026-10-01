"""Quality detectors: conflicts, duplicates, gaps and outdated information.

Each detector writes its result twice: as a row in `findings` and as a node of
type `finding`, so the output of the audit is itself auditable map content with
provenance, confidence and a history. Finding ids are deterministic, so re-running
a detector updates an existing finding instead of accumulating copies.

The duplicate detector is a standard-library port of Graphiti's deterministic
pass: exact normalization bucket, then MinHash/LSH banding, then a Jaccard check
against a 0.9 threshold, with a character-entropy gate that refuses to judge very
short or repetitive text.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable

from . import db, ids, lifecycle
from . import rules as rules_module

NEGATION_MARKERS = (
    "not",
    "never",
    "no",
    "stop",
    "avoid",
    "disable",
    "without",
    "don't",
    "do not",
    "cannot",
)


@dataclass(frozen=True)
class FindingRecord:
    id: str
    kind: str
    subject_ids: tuple[str, ...]
    detail: str
    score: float

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "subject_ids": list(self.subject_ids),
            "detail": self.detail,
            "score": round(self.score, 6),
        }


def _active_nodes(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT * FROM nodes
        WHERE retention_status IN ('active', 'reinforced', 'dormant')
          AND node_type != 'finding'
        ORDER BY id
        """
    ).fetchall()


def _write_finding(
    connection: sqlite3.Connection,
    record: FindingRecord,
    *,
    at: int,
) -> None:
    rules = db.active_rules(connection)
    rule_version = str(rules["version"])
    detector_version = str(rules["detectors"]["version"])
    summary = f"[{record.kind}] {record.detail}"

    node_id, _decision = lifecycle.admit(
        connection,
        content=summary,
        node_type="finding",
        origin_class="agent",
        session_kind="interactive",
        source_ref=f"detector:{record.kind}",
        captured_by="detector",
        actor="detector",
        at=at,
    )

    existing = connection.execute(
        "SELECT status, created_at FROM findings WHERE id = ?", (record.id,)
    ).fetchone()
    created_at = at if existing is None else int(existing["created_at"])
    status = "open" if existing is None else str(existing["status"])
    connection.execute(
        """
        INSERT INTO findings (
          id, node_id, kind, subject_ids_json, detail, score, detector_version,
          status, created_at, updated_at, resolved_by_change_set, rule_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
        ON CONFLICT(id) DO UPDATE SET
          node_id = excluded.node_id,
          detail = excluded.detail,
          score = excluded.score,
          detector_version = excluded.detector_version,
          updated_at = excluded.updated_at,
          rule_version = excluded.rule_version
        """,
        (
            record.id,
            node_id,
            record.kind,
            json.dumps(list(record.subject_ids)),
            record.detail,
            record.score,
            detector_version,
            status,
            created_at,
            at,
            rule_version,
        ),
    )
    if node_id is not None:
        for subject in record.subject_ids:
            lifecycle.add_edge(
                connection,
                src_id=node_id,
                dst_id=subject,
                edge_type="about",
                confidence=0.8,
                confidence_basis="detector-derived",
                captured_by="detector",
                evidence={"finding_id": record.id, "kind": record.kind},
                at=at,
            )


def detect_duplicates(
    connection: sqlite3.Connection, *, at: int | None = None
) -> list[FindingRecord]:
    rules = db.active_rules(connection)
    threshold = float(rules["detectors"]["duplicate_jaccard_threshold"])
    moment = at if at is not None else db.now_ms()
    nodes = _active_nodes(connection)

    by_exact: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    shingles_by_node: dict[str, set[str]] = {}
    buckets: dict[tuple[int, tuple[int, ...]], list[str]] = defaultdict(list)
    rows_by_id: dict[str, sqlite3.Row] = {}

    for node in nodes:
        node_id = str(node["id"])
        rows_by_id[node_id] = node
        normalized = ids.normalize_fuzzy(str(node["content"]))
        by_exact[(str(node["node_type"]), normalized)].append(node)
        if not ids.has_high_entropy(normalized):
            continue
        shingle_set = ids.shingles(normalized)
        shingles_by_node[node_id] = shingle_set
        for index, band in enumerate(ids.lsh_bands(ids.minhash_signature(shingle_set))):
            buckets[(index, band)].append(node_id)

    pairs: dict[tuple[str, str], float] = {}

    for (_node_type, normalized), group in by_exact.items():
        if not normalized or len(group) < 2:
            continue
        ordered = sorted(str(node["id"]) for node in group)
        for index, left in enumerate(ordered):
            for right in ordered[index + 1 :]:
                pairs[(left, right)] = 1.0

    for candidates in buckets.values():
        unique = sorted(set(candidates))
        for index, left in enumerate(unique):
            for right in unique[index + 1 :]:
                key = (left, right)
                if key in pairs:
                    continue
                if rows_by_id[left]["node_type"] != rows_by_id[right]["node_type"]:
                    continue
                score = ids.jaccard(shingles_by_node[left], shingles_by_node[right])
                if score >= threshold:
                    pairs[key] = score

    findings: list[FindingRecord] = []
    for (left, right), score in sorted(pairs.items()):
        lifecycle.add_edge(
            connection,
            src_id=left,
            dst_id=right,
            edge_type="duplicates",
            confidence=min(1.0, score),
            confidence_basis="detector-derived",
            captured_by="detector",
            evidence={"jaccard": round(score, 6), "method": "exact-or-minhash-lsh"},
            at=moment,
        )
        record = FindingRecord(
            id=ids.finding_id("duplicate", (left, right)),
            kind="duplicate",
            subject_ids=(left, right),
            detail=(
                f"near-identical {rows_by_id[left]['node_type']} nodes "
                f"(jaccard {score:.2f} at threshold {threshold:.2f})"
            ),
            score=score,
        )
        _write_finding(connection, record, at=moment)
        findings.append(record)
    return findings


def _has_negation(text: str) -> bool:
    normalized = f" {ids.normalize_exact(text)} "
    return any(f" {marker} " in normalized for marker in NEGATION_MARKERS)


def detect_conflicts(
    connection: sqlite3.Connection, *, at: int | None = None
) -> list[FindingRecord]:
    """Two independent conflict signals, both evidence-bearing.

    Signal one: an explicit `contradicts` edge someone asserted.
    Signal two: two active nodes of the same type share a subject key and have
    high token overlap, but exactly one of them carries a negation marker. That
    pattern catches "always use X" against "never use X" without asking a model.
    """
    rules = db.active_rules(connection)
    overlap_threshold = float(rules["detectors"]["conflict_overlap_threshold"])
    moment = at if at is not None else db.now_ms()
    nodes = _active_nodes(connection)
    rows_by_id = {str(node["id"]): node for node in nodes}

    findings: list[FindingRecord] = []
    seen: set[tuple[str, str]] = set()

    for row in connection.execute(
        """
        SELECT e.src_id, e.dst_id, e.confidence FROM edges AS e
        JOIN nodes AS s ON s.id = e.src_id
        JOIN nodes AS d ON d.id = e.dst_id
        WHERE e.edge_type = 'contradicts'
          AND s.retention_status IN ('active', 'reinforced', 'dormant')
          AND d.retention_status IN ('active', 'reinforced', 'dormant')
        """
    ).fetchall():
        pair = tuple(sorted((str(row["src_id"]), str(row["dst_id"]))))
        if pair in seen:
            continue
        seen.add(pair)  # type: ignore[arg-type]
        record = FindingRecord(
            id=ids.finding_id("conflict", pair),
            kind="conflict",
            subject_ids=pair,  # type: ignore[arg-type]
            detail="both nodes are active and an explicit contradicts edge links them",
            score=float(row["confidence"]),
        )
        _write_finding(connection, record, at=moment)
        findings.append(record)

    by_subject: dict[tuple[str, str], list[str]] = defaultdict(list)
    for node_id, node in rows_by_id.items():
        subject = str(node["subject_key"] or "")
        if subject:
            by_subject[(str(node["node_type"]), subject)].append(node_id)

    for (_node_type, _subject), group in sorted(by_subject.items()):
        ordered = sorted(group)
        for index, left in enumerate(ordered):
            for right in ordered[index + 1 :]:
                pair = (left, right)
                if pair in seen:
                    continue
                left_tokens = set(ids.tokens(str(rows_by_id[left]["content"])))
                right_tokens = set(ids.tokens(str(rows_by_id[right]["content"])))
                if not left_tokens or not right_tokens:
                    continue
                overlap = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
                if overlap < overlap_threshold:
                    continue
                left_negated = _has_negation(str(rows_by_id[left]["content"]))
                right_negated = _has_negation(str(rows_by_id[right]["content"]))
                if left_negated == right_negated:
                    continue
                seen.add(pair)
                lifecycle.add_edge(
                    connection,
                    src_id=left,
                    dst_id=right,
                    edge_type="contradicts",
                    confidence=min(1.0, overlap),
                    confidence_basis="detector-derived",
                    captured_by="detector",
                    evidence={
                        "token_overlap": round(overlap, 6),
                        "method": "shared-subject-with-single-negation",
                    },
                    at=moment,
                )
                record = FindingRecord(
                    id=ids.finding_id("conflict", pair),
                    kind="conflict",
                    subject_ids=pair,
                    detail=(
                        "same subject and high token overlap, but only one side "
                        f"is negated (overlap {overlap:.2f})"
                    ),
                    score=overlap,
                )
                _write_finding(connection, record, at=moment)
                findings.append(record)
    return findings


def detect_gaps(
    connection: sqlite3.Connection, *, at: int | None = None
) -> list[FindingRecord]:
    """Queries asked more than once that no node ever satisfied."""
    rules = db.active_rules(connection)
    min_queries = int(rules["detectors"]["gap_min_queries"])
    moment = at if at is not None else db.now_ms()
    rows = connection.execute(
        """
        SELECT query_hash, COUNT(*) AS asked, MAX(query) AS sample
        FROM traces
        WHERE returned_count = 0
        GROUP BY query_hash
        HAVING COUNT(*) >= ?
        ORDER BY query_hash
        """,
        (min_queries,),
    ).fetchall()
    findings: list[FindingRecord] = []
    for row in rows:
        record = FindingRecord(
            id=ids.finding_id("gap", (), detail_key=str(row["query_hash"])),
            kind="gap",
            subject_ids=(),
            detail=(
                f"query {str(row['sample'])!r} returned nothing on "
                f"{int(row['asked'])} traces"
            ),
            score=float(row["asked"]),
        )
        _write_finding(connection, record, at=moment)
        findings.append(record)
    return findings


def detect_outdated(
    connection: sqlite3.Connection, *, at: int | None = None
) -> list[FindingRecord]:
    """Three independent outdated signals.

    One: an active node whose event-time validity has already ended.
    Two: an active node that names a successor but was never moved to superseded.
    Three: two active nodes share a subject key and the older one has not been
    retired, which is how a stale value outranks a current one.
    """
    rules = db.active_rules(connection)
    grace_ms = int(rules["detectors"]["outdated_grace_days"]) * rules_module.DAY_MS
    moment = at if at is not None else db.now_ms()
    findings: list[FindingRecord] = []

    for row in connection.execute(
        """
        SELECT id, valid_to FROM nodes
        WHERE retention_status IN ('active', 'reinforced', 'dormant')
          AND valid_to IS NOT NULL AND valid_to + ? <= ?
        ORDER BY id
        """,
        (grace_ms, moment),
    ).fetchall():
        record = FindingRecord(
            id=ids.finding_id("outdated", (str(row["id"]),), detail_key="valid-to-passed"),
            kind="outdated",
            subject_ids=(str(row["id"]),),
            detail="node is still active although its valid_to has passed",
            score=1.0,
        )
        _write_finding(connection, record, at=moment)
        findings.append(record)

    for row in connection.execute(
        """
        SELECT id, superseded_by FROM nodes
        WHERE retention_status IN ('active', 'reinforced', 'dormant')
          AND superseded_by IS NOT NULL
        ORDER BY id
        """
    ).fetchall():
        record = FindingRecord(
            id=ids.finding_id("outdated", (str(row["id"]),), detail_key="successor-set"),
            kind="outdated",
            subject_ids=(str(row["id"]),),
            detail=(
                f"node names successor {str(row['superseded_by'])} but is still active"
            ),
            score=1.0,
        )
        _write_finding(connection, record, at=moment)
        findings.append(record)

    rows = connection.execute(
        """
        SELECT id, node_type, subject_key, observed_at, recorded_at FROM nodes
        WHERE retention_status IN ('active', 'reinforced')
          AND node_type IN ('fact', 'preference')
          AND subject_key IS NOT NULL AND subject_key != ''
        ORDER BY id
        """
    ).fetchall()
    grouped: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[(str(row["node_type"]), str(row["subject_key"]))].append(row)
    for (_node_type, _subject), group in sorted(grouped.items()):
        if len(group) < 2:
            continue
        ordered = sorted(
            group, key=lambda item: int(item["observed_at"] or item["recorded_at"])
        )
        newest = ordered[-1]
        for older in ordered[:-1]:
            record = FindingRecord(
                id=ids.finding_id(
                    "outdated",
                    (str(older["id"]), str(newest["id"])),
                    detail_key="newer-observation",
                ),
                kind="outdated",
                subject_ids=(str(older["id"]), str(newest["id"])),
                detail=(
                    "a newer active observation shares this subject; the older "
                    "node was never retired"
                ),
                score=0.8,
            )
            _write_finding(connection, record, at=moment)
            findings.append(record)
    return findings


def run_all(
    connection: sqlite3.Connection, *, at: int | None = None
) -> dict[str, list[dict[str, Any]]]:
    moment = at if at is not None else db.now_ms()
    return {
        "duplicate": [record.to_json() for record in detect_duplicates(connection, at=moment)],
        "conflict": [record.to_json() for record in detect_conflicts(connection, at=moment)],
        "gap": [record.to_json() for record in detect_gaps(connection, at=moment)],
        "outdated": [record.to_json() for record in detect_outdated(connection, at=moment)],
    }


def open_findings(
    connection: sqlite3.Connection, kinds: Iterable[str] | None = None
) -> list[dict[str, Any]]:
    clause = ""
    parameters: list[Any] = []
    if kinds:
        materialized = list(kinds)
        clause = f" AND kind IN ({','.join('?' for _ in materialized)})"
        parameters = materialized
    rows = connection.execute(
        f"SELECT * FROM findings WHERE status = 'open'{clause} "
        "ORDER BY kind, score DESC, id",
        tuple(parameters),
    ).fetchall()
    return db.rows_to_dicts(rows)
