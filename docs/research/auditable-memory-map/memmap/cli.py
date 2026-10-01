"""Command-line entry points the agent calls through its exec tool.

Every command prints JSON on stdout and exits 0 for an ordinary refusal, so a
memory problem is data the agent can read rather than an error that breaks a
turn. Exit code 2 is reserved for a usage error in the command itself.

    map init                              create or upgrade the map
    map ingest --workspace DIR [--openclaw-db PATH]
    map search "query" [--k N] [--include-untrusted]
    map why TRACE_ID                      explain a retrieval from its trace
    map inspect NODE_ID                   one node with full history
    map audit                             run every detector, list open findings
    map sweep                             run the time-driven transitions
    map confirm NODE_ID --accept|--reject owner decision on a pending node
    map propose --file CHANGESET.json     record a proposed change set
    map evaluate [--change-set ID]        run the evaluation set
    map apply CHANGE_SET_ID --owner-approved
    map rollback CHANGE_SET_ID
    map report [--out DIR]                write the owner-readable views
    map brief                             the compact per-turn status block
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Sequence

from . import adapter_openclaw, changesets, db, detectors, evaluate, lifecycle, reports
from . import retrieval

DEFAULT_DB_ENV = "AMM_DB"
DEFAULT_DB_NAME = "memory-map.sqlite"


def default_db_path() -> Path:
    override = os.environ.get(DEFAULT_DB_ENV)
    if override:
        return Path(override)
    return Path.cwd() / "memory-map" / DEFAULT_DB_NAME


def emit(payload: dict[str, Any]) -> int:
    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def _open(path: Path) -> sqlite3.Connection | db.Degraded:
    return db.safe_open(path)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="map", description="Auditable memory map")
    parser.add_argument("--db", default=None, help="path to the map database")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create or upgrade the map database")

    ingest = sub.add_parser("ingest", help="read OpenClaw memory into the map")
    ingest.add_argument("--workspace", required=True)
    ingest.add_argument("--openclaw-db", default=None)
    ingest.add_argument("--recall-events", default=None, help="JSON file of recall events")

    search = sub.add_parser("search", help="search the map and record a trace")
    search.add_argument("query")
    search.add_argument("--k", type=int, default=None)
    search.add_argument("--min-score", type=float, default=None)
    search.add_argument("--include-untrusted", action="store_true")
    search.add_argument("--include-superseded", action="store_true")
    search.add_argument("--type", action="append", dest="node_types", default=None)
    search.add_argument("--session", default=None)
    search.add_argument("--count-use", action="store_true", help="count hits as uses")

    why = sub.add_parser("why", help="explain a retrieval from its recorded trace")
    why.add_argument("trace_id")

    inspect = sub.add_parser("inspect", help="show one node with its full history")
    inspect.add_argument("node_id")
    inspect.add_argument("--markdown", action="store_true")

    sub.add_parser("audit", help="run every detector and list open findings")
    sub.add_parser("sweep", help="run the time-driven lifecycle transitions")
    sub.add_parser("brief", help="print the compact per-turn status block")

    confirm = sub.add_parser("confirm", help="record an owner decision")
    confirm.add_argument("node_id")
    group = confirm.add_mutually_exclusive_group(required=True)
    group.add_argument("--accept", action="store_true")
    group.add_argument("--reject", action="store_true")

    add = sub.add_parser("add", help="propose one node through the admission gate")
    add.add_argument("content")
    add.add_argument("--type", dest="node_type", default="fact")
    add.add_argument("--origin", dest="origin_class", default="agent")
    add.add_argument("--source-ref", default=None)
    add.add_argument("--importance", type=int, default=None)

    propose = sub.add_parser("propose", help="record a proposed change set")
    propose.add_argument("--file", required=True)

    evaluate_parser = sub.add_parser("evaluate", help="run the evaluation set")
    evaluate_parser.add_argument("--change-set", default=None)
    evaluate_parser.add_argument("--eval-set", default=None)
    evaluate_parser.add_argument("--scratch-db", default=None)

    apply_parser = sub.add_parser("apply", help="apply an approved change set")
    apply_parser.add_argument("change_set_id")
    apply_parser.add_argument("--owner-approved", action="store_true")

    rollback_parser = sub.add_parser("rollback", help="roll back an applied change set")
    rollback_parser.add_argument("change_set_id")

    sub.add_parser("change-sets", help="list recorded change sets")

    report = sub.add_parser("report", help="write the owner-readable views")
    report.add_argument("--out", default=None)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    db_path = Path(args.db) if args.db else default_db_path()

    if args.command == "init":
        connection = db.initialize(db_path)
        try:
            return emit(
                {
                    "status": "ok",
                    "db": db_path.as_posix(),
                    "rule_version": str(db.active_rules(connection)["version"]),
                    "fts5": db.has_fts5(connection),
                }
            )
        finally:
            connection.commit()
            connection.close()

    opened = _open(db_path)
    if isinstance(opened, db.Degraded):
        return emit(opened.to_json())
    connection = opened

    try:
        if args.command == "ingest":
            result: dict[str, Any] = {"status": "ok", "reports": []}
            if args.openclaw_db:
                result["reports"].append(
                    adapter_openclaw.ingest_index(connection, args.openclaw_db).to_json()
                )
            result["reports"].append(
                adapter_openclaw.ingest_markdown(connection, args.workspace).to_json()
            )
            if args.recall_events:
                events = json.loads(Path(args.recall_events).read_text(encoding="utf-8"))
                result["recall_import"] = adapter_openclaw.import_recall_events(
                    connection, events
                )
            connection.commit()
            return emit(result)

        if args.command == "search":
            outcome = retrieval.search(
                connection,
                query=args.query,
                k=args.k,
                min_score=args.min_score,
                include_untrusted=args.include_untrusted,
                include_superseded=args.include_superseded,
                node_types=args.node_types,
                agent_session=args.session,
            )
            if args.count_use:
                retrieval.record_use(
                    connection, [candidate.node_id for candidate in outcome.returned]
                )
            connection.commit()
            return emit({"status": "ok", **outcome.to_json()})

        if args.command == "why":
            return emit(retrieval.explain(connection, args.trace_id))

        if args.command == "inspect":
            if args.markdown:
                sys.stdout.write(reports.render_node_history(connection, args.node_id))
                return 0
            return emit(reports.node_history(connection, args.node_id))

        if args.command == "audit":
            found = detectors.run_all(connection)
            connection.commit()
            return emit(
                {
                    "status": "ok",
                    "detected": {kind: len(items) for kind, items in found.items()},
                    "open_findings": detectors.open_findings(connection),
                    "overview": reports.overview(connection),
                }
            )

        if args.command == "sweep":
            counts = lifecycle.sweep(connection)
            connection.commit()
            return emit({"status": "ok", "transitions": counts})

        if args.command == "brief":
            sys.stdout.write(reports.brief(connection) + "\n")
            return 0

        if args.command == "confirm":
            transition = "owner-confirm" if args.accept else "owner-reject"
            result = lifecycle.apply_transition(
                connection, node_id=args.node_id, transition=transition, actor="owner"
            )
            connection.commit()
            return emit({"status": "ok", **result.to_json()})

        if args.command == "add":
            node_id, decision = lifecycle.admit(
                connection,
                content=args.content,
                node_type=args.node_type,
                origin_class=args.origin_class,
                source_ref=args.source_ref,
                importance=args.importance,
                captured_by="agent",
                actor="agent",
            )
            connection.commit()
            return emit(
                {
                    "status": "ok",
                    "node_id": node_id,
                    "decision": decision.decision,
                    "reason_code": decision.reason_code,
                    "redaction_class": decision.redaction_class,
                    "retention_status": decision.retention_status,
                    "notes": list(decision.notes),
                }
            )

        if args.command == "propose":
            payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
            try:
                result = changesets.propose(
                    connection,
                    title=str(payload["title"]),
                    reason=str(payload["reason"]),
                    operations=list(payload["operations"]),
                    evidence_ids=list(payload["evidence_ids"]),
                    expected_effect=str(payload["expected_effect"]),
                    author=str(payload.get("author", "agent")),
                )
            except (changesets.ChangeSetError, KeyError) as error:
                return emit({"status": "refused", "reason": str(error)})
            connection.commit()
            return emit(result)

        if args.command == "evaluate":
            scratch = Path(args.scratch_db) if args.scratch_db else db_path.with_name(
                "eval-scratch.sqlite"
            )
            result = evaluate.run(scratch, eval_set_path=args.eval_set)
            if args.change_set:
                changesets.record_evaluation(connection, args.change_set, result)
                connection.commit()
            return emit({"status": "ok", **result})

        if args.command == "apply":
            result = changesets.apply(
                connection, args.change_set_id, owner_approved=args.owner_approved
            )
            connection.commit()
            return emit(result)

        if args.command == "rollback":
            result = changesets.rollback(connection, args.change_set_id)
            connection.commit()
            return emit(result)

        if args.command == "change-sets":
            return emit({"status": "ok", "change_sets": changesets.listing(connection)})

        if args.command == "report":
            out_dir = Path(args.out) if args.out else db_path.parent / "reports"
            written = reports.write_all(connection, out_dir)
            return emit({"status": "ok", "written": written})

        parser.error(f"unhandled command {args.command}")
        return 2
    except sqlite3.Error as error:
        return emit({"status": "degraded", "reason": f"sqlite error: {error}"})
    finally:
        connection.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
