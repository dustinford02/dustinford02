"""Read-only adapter over OpenClaw's memory index and memory files.

The adapter opens OpenClaw's per-agent SQLite database with `mode=ro` and never
issues a write to it, never touches `MEMORY.md`, `USER.md` or `memory/*.md`, and
never asks OpenClaw to reindex. If the database is missing or its shape differs
from the version this adapter was written against, the adapter reports that and
falls back to reading the Markdown files directly.

Tables read, as declared in OpenClaw 2026.9.2:

  memory_index_chunks(id, path, source, start_line, end_line, hash, model, text,
                      embedding, updated_at)
  memory_index_chunk_provenance(chunk_id, origin_class, session_kind,
                               observed_at, supersedes_key)
  memory_index_chunk_recall_metadata(chunk_id, importance, triggers, project_key)
  memory_index_sources(id, path, source, hash, mtime, size)
  memory_index_state(id, revision)
  memory_entry_origins(entry_key, agent_id, session_id, session_key,
                       origin_class, observed_at)

Entry type inference is a heuristic and is labelled as such on every node: a
`USER.md` line becomes a preference, a `MEMORY.md` line becomes a fact, and a
dated daily note line becomes an episode. The adapter does not ask a model to
classify anything.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from . import db, ids, lifecycle

PROMOTION_MARKER = re.compile(r"<!--\s*openclaw-memory-promotion:([^\n]*?)\s*-->")
TRIGGER_ANNOTATION = re.compile(r"<!--\s*trigger:\s*([^\n]*?)\s*-->")
IMPORTANCE_ANNOTATION = re.compile(r"<!--\s*importance:\s*(\d{1,2})\s*-->")
PROJECT_ANNOTATION = re.compile(r"<!--\s*project:\s*([^\n]*?)\s*-->")
DATED_NOTE = re.compile(r"(?:^|/)(\d{4}-\d{2}-\d{2})(?:-[^/]*)?\.md$")
BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*\S)\s*$")

EXPECTED_CHUNK_COLUMNS = {
    "id",
    "path",
    "source",
    "start_line",
    "end_line",
    "text",
    "updated_at",
}


@dataclass
class IngestReport:
    source: str
    read: int = 0
    admitted: int = 0
    quarantined: int = 0
    rejected: int = 0
    skipped_unchanged: int = 0
    notes: list[str] | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "read": self.read,
            "admitted": self.admitted,
            "quarantined": self.quarantined,
            "rejected": self.rejected,
            "skipped_unchanged": self.skipped_unchanged,
            "notes": self.notes or [],
        }


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return set()
    return {str(row["name"]) for row in rows}


CURATED_FILENAMES = {"memory.md", "user.md"}


def infer_node_type(path: str) -> str:
    lowered = path.replace("\\", "/").lower()
    basename = lowered.rsplit("/", 1)[-1]
    if basename == "user.md":
        return "preference"
    if basename == "memory.md":
        return "fact"
    if DATED_NOTE.search(lowered):
        return "episode"
    if basename in {"dreams.md", "dream.md"}:
        return "source"
    return "lesson"


def infer_origin_class(path: str) -> str:
    """Conservative provenance for a Markdown read with no index to consult.

    OpenClaw propagates a network taint through a turn, but only tools that
    declare their results as network-sourced participate; a local file read does
    not, so assistant text derived from a local file keeps `agent` provenance.
    Daily notes are exactly where that gap shows up, because they are the landing
    place for summaries of web pages and forum threads.

    When the map reads Markdown directly it therefore refuses to infer trust for
    anything outside the curated core. Curated files are `agent`, because nothing
    reaches them without passing OpenClaw's own promotion gates; everything else
    is `untrusted` and enters quarantine until corroborated or confirmed. When the
    index is available its per-chunk origin class wins, because that value was
    written by OpenClaw's classification code rather than guessed here.
    """
    basename = path.replace("\\", "/").lower().rsplit("/", 1)[-1]
    return "agent" if basename in CURATED_FILENAMES else "untrusted"


def parse_annotations(line: str) -> dict[str, Any]:
    triggers = TRIGGER_ANNOTATION.search(line)
    importance = IMPORTANCE_ANNOTATION.search(line)
    project = PROJECT_ANNOTATION.search(line)
    promotion = PROMOTION_MARKER.search(line)
    value = None
    if importance is not None:
        parsed = int(importance.group(1))
        value = parsed if 1 <= parsed <= 10 else None
    return {
        "trigger_phrases": triggers.group(1).strip() if triggers else None,
        "importance": value,
        "project_key": project.group(1).strip() if project else None,
        "promotion_key": promotion.group(1).strip() if promotion else None,
    }


def iter_markdown_entries(root: Path) -> Iterator[dict[str, Any]]:
    """Yield one candidate per Markdown list item in the workspace memory files."""
    targets: list[Path] = []
    for name in ("MEMORY.md", "USER.md"):
        candidate = root / name
        if candidate.is_file():
            targets.append(candidate)
    memory_dir = root / "memory"
    if memory_dir.is_dir():
        targets.extend(sorted(path for path in memory_dir.rglob("*.md") if path.is_file()))

    for path in targets:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        relative = path.relative_to(root).as_posix()
        for index, raw in enumerate(lines, start=1):
            match = BULLET.match(raw)
            if match is None:
                continue
            body = match.group(1)
            if not ids.strip_annotations(body).strip():
                continue
            annotations = parse_annotations(raw)
            yield {
                # Annotations are parsed into their own fields, so the stored
                # content is the claim itself rather than the claim plus its
                # metadata comments. Node ids are unaffected: identity already
                # normalizes annotations away.
                "content": ids.strip_annotations(body).strip(),
                "path": relative,
                "start_line": index,
                "end_line": index,
                "node_type": infer_node_type(relative),
                **annotations,
            }


def ingest_markdown(
    connection: sqlite3.Connection,
    workspace_dir: str | Path,
    *,
    origin_class_override: str | None = None,
    at: int | None = None,
) -> IngestReport:
    """Ingest workspace memory files. Used when the index is unavailable."""
    root = Path(workspace_dir)
    report = IngestReport(source=f"markdown:{root.as_posix()}", notes=[])
    if not root.is_dir():
        report.notes = [f"workspace directory not found: {root}"]
        return report
    moment = at if at is not None else db.now_ms()

    for entry in iter_markdown_entries(root):
        report.read += 1
        source_ref = f"{entry['path']}#L{entry['start_line']}-L{entry['end_line']}"
        key = f"markdown:{source_ref}"
        content_hash = ids.content_hash(str(entry["content"]))
        if _unchanged(connection, key, content_hash):
            report.skipped_unchanged += 1
            continue
        node_id, decision = lifecycle.admit(
            connection,
            content=str(entry["content"]),
            node_type=str(entry["node_type"]),
            origin_class=origin_class_override or infer_origin_class(str(entry["path"])),
            session_kind="interactive",
            source_ref=source_ref,
            captured_by="adapter",
            importance=entry["importance"],
            trigger_phrases=entry["trigger_phrases"],
            project_key=entry["project_key"],
            actor="adapter",
            at=moment,
        )
        _count(report, decision.decision)
        _remember(connection, key, content_hash, moment, note="markdown entry")
        if node_id is not None and entry["path"] not in {"MEMORY.md", "USER.md"}:
            _link_to_source_node(connection, node_id, str(entry["path"]), moment)
    return report


def ingest_index(
    connection: sqlite3.Connection,
    openclaw_db_path: str | Path,
    *,
    at: int | None = None,
) -> IngestReport:
    """Ingest from OpenClaw's index, which carries authoritative provenance."""
    path = Path(openclaw_db_path)
    report = IngestReport(source=f"openclaw-index:{path.as_posix()}", notes=[])
    if not path.is_file():
        report.notes = [f"OpenClaw database not found: {path}"]
        return report
    moment = at if at is not None else db.now_ms()

    try:
        source = db.connect(path, read_only=True)
    except sqlite3.Error as error:
        report.notes = [f"could not open OpenClaw database read-only: {error}"]
        return report

    try:
        columns = _table_columns(source, "memory_index_chunks")
        if not EXPECTED_CHUNK_COLUMNS.issubset(columns):
            report.notes = [
                "memory_index_chunks is missing expected columns "
                f"{sorted(EXPECTED_CHUNK_COLUMNS - columns)}; refusing to guess",
            ]
            return report

        has_provenance = bool(_table_columns(source, "memory_index_chunk_provenance"))
        has_recall = bool(_table_columns(source, "memory_index_chunk_recall_metadata"))
        revision_row = None
        if _table_columns(source, "memory_index_state"):
            revision_row = source.execute(
                "SELECT revision FROM memory_index_state WHERE id = 1"
            ).fetchone()
        revision = None if revision_row is None else int(revision_row["revision"])
        if not has_provenance:
            report.notes.append(
                "no memory_index_chunk_provenance table; every chunk is treated as "
                "untrusted because origin class could not be read"
            )

        select = [
            "c.id AS chunk_id",
            "c.path AS path",
            "c.source AS source",
            "c.start_line AS start_line",
            "c.end_line AS end_line",
            "c.text AS text",
            "c.updated_at AS updated_at",
        ]
        joins = ""
        if has_provenance:
            select += [
                "p.origin_class AS origin_class",
                "p.session_kind AS session_kind",
                "p.observed_at AS observed_at",
                "p.supersedes_key AS supersedes_key",
            ]
            joins += (
                " LEFT JOIN memory_index_chunk_provenance AS p ON p.chunk_id = c.id"
            )
        if has_recall:
            select += [
                "r.importance AS importance",
                "r.triggers AS triggers",
                "r.project_key AS project_key",
            ]
            joins += (
                " LEFT JOIN memory_index_chunk_recall_metadata AS r ON r.chunk_id = c.id"
            )

        rows = source.execute(
            f"SELECT {', '.join(select)} FROM memory_index_chunks AS c{joins} "
            "ORDER BY c.path, c.start_line"
        ).fetchall()

        for row in rows:
            keys = row.keys()
            report.read += 1
            text = ids.strip_annotations(str(row["text"])).strip()
            path_value = str(row["path"])
            source_ref = (
                f"{path_value}#L{int(row['start_line'])}-L{int(row['end_line'])}"
            )
            key = f"chunk:{row['chunk_id']}"
            content_hash = ids.content_hash(text)
            if _unchanged(connection, key, content_hash):
                report.skipped_unchanged += 1
                continue

            origin_class = (
                str(row["origin_class"])
                if "origin_class" in keys and row["origin_class"] is not None
                else "untrusted"
            )
            session_kind = (
                str(row["session_kind"])
                if "session_kind" in keys and row["session_kind"] is not None
                else "unknown"
            )
            observed_at = (
                int(row["observed_at"])
                if "observed_at" in keys and row["observed_at"] is not None
                else int(row["updated_at"])
            )
            importance = (
                int(row["importance"])
                if "importance" in keys and row["importance"] is not None
                else None
            )
            triggers = (
                str(row["triggers"])
                if "triggers" in keys and row["triggers"] is not None
                else None
            )
            project_key = (
                str(row["project_key"])
                if "project_key" in keys and row["project_key"] is not None
                else None
            )
            node_type = (
                "episode" if str(row["source"]) == "sessions" else infer_node_type(path_value)
            )

            node_id, decision = lifecycle.admit(
                connection,
                content=text,
                node_type=node_type,
                origin_class=origin_class,
                session_kind=session_kind,
                source_ref=source_ref,
                captured_by="adapter",
                importance=importance,
                trigger_phrases=triggers,
                project_key=project_key,
                observed_at=observed_at,
                actor="adapter",
                at=moment,
            )
            _count(report, decision.decision)
            _remember(
                connection,
                key,
                content_hash,
                moment,
                revision=revision,
                note=f"chunk from {path_value}",
            )
            if node_id is not None:
                _link_to_source_node(connection, node_id, path_value, moment)
        return report
    finally:
        source.close()


def import_recall_events(
    connection: sqlite3.Connection,
    events: list[dict[str, Any]],
    *,
    at: int | None = None,
) -> dict[str, Any]:
    """Import OpenClaw `memory.recall.recorded` events as partial traces.

    These are explicitly marked `partial-import`: OpenClaw records the query, the
    returned path and line range, and one combined score, but not the per-lane
    components, the filters, or the candidates it dropped. The explain command
    reports those as not captured rather than inferring them.
    """
    from . import retrieval

    moment = at if at is not None else db.now_ms()
    imported = 0
    unmatched = 0
    for event in events:
        if event.get("type") != "memory.recall.recorded":
            continue
        query = str(event.get("query", ""))
        if not query:
            continue
        candidates: list[retrieval.Candidate] = []
        for result in event.get("results", []) or []:
            source_ref = (
                f"{result.get('path')}#L{result.get('startLine')}-L{result.get('endLine')}"
            )
            row = connection.execute(
                "SELECT * FROM nodes WHERE source_ref = ? LIMIT 1", (source_ref,)
            ).fetchone()
            if row is None:
                unmatched += 1
                continue
            candidate = retrieval.Candidate(
                node_id=str(row["id"]),
                content=str(row["content"]),
                node_type=str(row["node_type"]),
                origin_class=str(row["origin_class"]),
                retention_status=str(row["retention_status"]),
                importance=None if row["importance"] is None else int(row["importance"]),
                valid_to=None if row["valid_to"] is None else int(row["valid_to"]),
                recorded_at=int(row["recorded_at"]),
                last_used_at=None
                if row["last_used_at"] is None
                else int(row["last_used_at"]),
                final_score=float(result.get("score", 0.0)),
            )
            candidate.lane_hits.add("openclaw-combined")
            candidates.append(candidate)
        retrieval.record_trace(
            connection,
            query=query,
            k_requested=len(candidates),
            min_score=0.0,
            lanes={
                "openclaw-combined": {
                    "available": True,
                    "note": "imported combined score; per-lane components not recorded "
                    "by the source event",
                }
            },
            filters={"imported": True},
            candidates=candidates,
            agent_session=None,
            completeness="partial-import",
            trace_source="openclaw-recall-import",
            at=moment,
        )
        imported += 1
    return {"imported_traces": imported, "unmatched_results": unmatched}


def _link_to_source_node(
    connection: sqlite3.Connection, node_id: str, path_value: str, at: int
) -> None:
    source_id, _decision = lifecycle.admit(
        connection,
        content=f"OpenClaw memory file {path_value}",
        node_type="source",
        origin_class="agent",
        session_kind="interactive",
        source_ref=f"file:{path_value}",
        captured_by="adapter",
        actor="adapter",
        at=at,
    )
    if source_id is not None:
        lifecycle.add_edge(
            connection,
            src_id=node_id,
            dst_id=source_id,
            edge_type="derived_from",
            confidence=1.0,
            confidence_basis="adapter-observed-path",
            captured_by="adapter",
            source_ref=f"file:{path_value}",
            at=at,
        )


def _count(report: IngestReport, decision: str) -> None:
    if decision == "admitted":
        report.admitted += 1
    elif decision == "quarantined":
        report.quarantined += 1
    else:
        report.rejected += 1


def _unchanged(connection: sqlite3.Connection, key: str, content_hash: str) -> bool:
    row = connection.execute(
        "SELECT last_seen_hash FROM ingest_state WHERE source_key = ?", (key,)
    ).fetchone()
    return row is not None and str(row["last_seen_hash"]) == content_hash


def _remember(
    connection: sqlite3.Connection,
    key: str,
    content_hash: str,
    at: int,
    *,
    revision: int | None = None,
    note: str = "",
) -> None:
    connection.execute(
        """
        INSERT INTO ingest_state (
          source_key, last_seen_hash, last_ingested_at, openclaw_index_revision, note
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(source_key) DO UPDATE SET
          last_seen_hash = excluded.last_seen_hash,
          last_ingested_at = excluded.last_ingested_at,
          openclaw_index_revision = excluded.openclaw_index_revision,
          note = excluded.note
        """,
        (key, content_hash, at, revision, note),
    )


def store_node_embedding(
    connection: sqlite3.Connection, node_id: str, embedding: list[float], at: int
) -> None:
    """Park an imported embedding beside the node so the vector lane can run.

    Embeddings are kept in `ingest_state` rather than on `nodes` so the node table
    stays small enough to read by hand, and so dropping every embedding is one
    delete that cannot damage the map itself.
    """
    _remember(
        connection,
        f"embedding:{node_id}",
        ids.digest("embedding", json.dumps(embedding)),
        at,
        note=json.dumps(embedding),
    )
