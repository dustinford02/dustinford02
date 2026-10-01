"""Database open, migrate and degrade-safe helpers.

Two properties matter here. First, the map opens OpenClaw's own database
read-only and never writes to it. Second, a map failure must never be able to
block a reply: callers use `safe_open` and get `None` instead of an exception.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from . import rules as rules_module

SCHEMA_FILENAME = "schema.sql"


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class Degraded:
    """Returned instead of raising when the map cannot be reached."""

    reason: str

    def to_json(self) -> dict[str, Any]:
        return {"status": "degraded", "reason": self.reason}


def schema_path() -> Path:
    return Path(__file__).resolve().parent.parent / SCHEMA_FILENAME


def connect(db_path: str | Path, *, read_only: bool = False) -> sqlite3.Connection:
    path = Path(db_path)
    if read_only:
        uri = f"file:{path.as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def has_fts5(connection: sqlite3.Connection) -> bool:
    try:
        connection.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS temp.fts5_probe USING fts5(x)"
        )
        connection.execute("DROP TABLE IF EXISTS temp.fts5_probe")
        return True
    except sqlite3.Error:
        return False


def initialize(db_path: str | Path) -> sqlite3.Connection:
    """Create or upgrade the map database and seed the active rule version."""
    connection = connect(db_path)
    connection.executescript(schema_path().read_text(encoding="utf-8"))
    if has_fts5(connection):
        connection.executescript(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
              content,
              node_id UNINDEXED,
              tokenize = 'unicode61'
            );
            """
        )
    _seed_rule_version(connection)
    _set_meta(connection, "schema_version", rules_module.SCHEMA_VERSION)
    connection.commit()
    return connection


def safe_open(db_path: str | Path) -> sqlite3.Connection | Degraded:
    """Open an existing map without raising. Used by every read command."""
    try:
        path = Path(db_path)
        if not path.exists():
            return Degraded(f"map database not found at {path}")
        connection = connect(path)
        connection.execute("SELECT 1 FROM map_meta LIMIT 1")
        return connection
    except sqlite3.Error as error:
        return Degraded(f"sqlite error: {error}")
    except OSError as error:
        return Degraded(f"filesystem error: {error}")


def _seed_rule_version(connection: sqlite3.Connection) -> None:
    existing = connection.execute(
        "SELECT version FROM rule_versions WHERE version = ?",
        (rules_module.RULE_VERSION,),
    ).fetchone()
    if existing is None:
        connection.execute(
            """
            INSERT INTO rule_versions (version, created_at, rules_json, notes, active)
            VALUES (?, ?, ?, ?, 1)
            """,
            (
                rules_module.RULE_VERSION,
                now_ms(),
                json.dumps(rules_module.RULES, sort_keys=True),
                "seeded by memmap.db.initialize",
            ),
        )
    connection.execute(
        "UPDATE rule_versions SET active = CASE WHEN version = ? THEN 1 ELSE 0 END",
        (rules_module.RULE_VERSION,),
    )


def _set_meta(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        "INSERT INTO map_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def set_meta(connection: sqlite3.Connection, key: str, value: str) -> None:
    _set_meta(connection, key, value)


def get_meta(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("SELECT value FROM map_meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def active_rules(connection: sqlite3.Connection) -> dict[str, Any]:
    """Read the active rule set from the database, not from code."""
    row = connection.execute(
        "SELECT version, rules_json FROM rule_versions WHERE active = 1 LIMIT 1"
    ).fetchone()
    if row is None:
        return rules_module.RULES
    loaded = json.loads(row["rules_json"])
    loaded["version"] = row["version"]
    return loaded


def stored_rules(connection: sqlite3.Connection, version: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT rules_json FROM rule_versions WHERE version = ?", (version,)
    ).fetchone()
    return None if row is None else json.loads(row["rules_json"])


def record_history(
    connection: sqlite3.Connection,
    *,
    node_id: str,
    transition: str,
    from_status: str | None,
    to_status: str | None,
    actor: str,
    accepted: bool,
    reason: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    change_set_id: str | None = None,
    rule_version: str = rules_module.RULE_VERSION,
    at: int | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO node_history (
          node_id, at, transition, from_status, to_status, actor, accepted,
          reason, before_json, after_json, change_set_id, rule_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            node_id,
            at if at is not None else now_ms(),
            transition,
            from_status,
            to_status,
            actor,
            1 if accepted else 0,
            reason,
            json.dumps(before, sort_keys=True) if before is not None else None,
            json.dumps(after, sort_keys=True) if after is not None else None,
            change_set_id,
            rule_version,
        ),
    )


def fetch_node(connection: sqlite3.Connection, node_id: str) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]
