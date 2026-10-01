"""Retrieval with a trace recorded at query time.

The trace is the product, not a by-product. Every candidate the lanes produced is
written with its per-lane score components, the filters that applied, and, when it
was dropped, the reason it was dropped. The explain path reads only these rows, so
an explanation cannot be invented after the fact.

Lanes:
  keyword  FTS5 BM25 when available, otherwise a normalized LIKE scan. The trace
           records which of the two ran, so a degraded lane is never mistaken for
           a strong keyword lane.
  vector   Cosine over an embedding supplied by the caller against embeddings
           carried on nodes. When no query embedding is supplied the lane is
           recorded as unavailable with a reason; it is never silently skipped.
  graph    One or more hops over typed edges from the nodes the other lanes found.
           The path that reached each node is stored on the candidate row.
  recency  An exponential decay multiplier, not a lane that can find anything.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Sequence

from . import db, ids
from . import rules as rules_module

GRAPH_EXPANSION_EDGE_TYPES = (
    "supports",
    "about",
    "part_of",
    "derived_from",
    "duplicates",
    "contradicts",
)


@dataclass
class Candidate:
    node_id: str
    content: str
    node_type: str
    origin_class: str
    retention_status: str
    importance: int | None
    valid_to: int | None
    recorded_at: int
    last_used_at: int | None
    keyword_score: float = 0.0
    vector_score: float = 0.0
    graph_score: float = 0.0
    recency_score: float = 1.0
    importance_mult: float = 1.0
    trust_mult: float = 1.0
    final_score: float = 0.0
    lane_hits: set[str] = field(default_factory=set)
    graph_path: str | None = None
    drop_reason: str | None = None

    @property
    def returned(self) -> bool:
        return self.drop_reason is None


@dataclass
class SearchOutcome:
    trace_id: str
    returned: list[Candidate]
    dropped: list[Candidate]
    lanes: dict[str, Any]
    filters: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "lanes": self.lanes,
            "filters": self.filters,
            "returned": [
                {
                    "node_id": candidate.node_id,
                    "score": round(candidate.final_score, 6),
                    "content": candidate.content,
                    "node_type": candidate.node_type,
                    "origin_class": candidate.origin_class,
                }
                for candidate in self.returned
            ],
            "dropped_count": len(self.dropped),
        }


def _fts_available(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'nodes_fts'"
    ).fetchone()
    return row is not None


def _bm25_to_score(rank: float) -> float:
    """Map SQLite bm25() output (lower is better, usually negative) to 0..1."""
    if not math.isfinite(rank):
        return 0.0
    if rank < 0:
        relevance = -rank
        return relevance / (1.0 + relevance)
    return 1.0 / (1.0 + rank)


def _build_fts_query(terms: list[str]) -> str | None:
    """Disjunction of prefix terms.

    Prefix matching is deliberate: the index has no stemmer, so a query for
    "embedding" would otherwise miss an entry that says "embeddings". A
    disjunction rather than a conjunction keeps partial matches as candidates,
    which matters because a dropped candidate is still recorded in the trace and
    a term-coverage gate decides afterwards whether a partial match is enough.
    """
    if not terms:
        return None
    return " OR ".join(f'"{term}"*' for term in terms)


def _row_to_candidate(row: sqlite3.Row) -> Candidate:
    return Candidate(
        node_id=str(row["id"]),
        content=str(row["content"]),
        node_type=str(row["node_type"]),
        origin_class=str(row["origin_class"]),
        retention_status=str(row["retention_status"]),
        importance=None if row["importance"] is None else int(row["importance"]),
        valid_to=None if row["valid_to"] is None else int(row["valid_to"]),
        recorded_at=int(row["recorded_at"]),
        last_used_at=None if row["last_used_at"] is None else int(row["last_used_at"]),
    )


def _keyword_lane(
    connection: sqlite3.Connection, terms: list[str], cap: int
) -> tuple[dict[str, float], dict[str, Any]]:
    scores: dict[str, float] = {}
    if _fts_available(connection):
        match = _build_fts_query(terms)
        if match is None:
            return {}, {"available": False, "mode": "fts5", "reason": "query had no terms"}
        try:
            # The result column must not be called `rank`: FTS5 reserves that
            # name for its own hidden column, and an alias collision silently
            # returns a near-zero value instead of the bm25 score.
            rows = connection.execute(
                """
                SELECT node_id, bm25(nodes_fts) AS bm25_score
                FROM nodes_fts
                WHERE nodes_fts MATCH ?
                ORDER BY bm25_score
                LIMIT ?
                """,
                (match, cap),
            ).fetchall()
            raw: dict[str, float] = {}
            for row in rows:
                raw[str(row["node_id"])] = _bm25_to_score(float(row["bm25_score"]))
            # BM25 is corpus-relative: when a term appears in nearly every row of
            # a small map its inverse document frequency collapses and the raw
            # score approaches zero. Normalizing against the best hit in this
            # query keeps the lane usable on a map with a handful of nodes. The
            # lane is therefore a ranking signal, not a calibrated relevance
            # estimate, and the trace records the raw best so the scale is not
            # lost.
            best = max(raw.values()) if raw else 0.0
            if best > 0.0:
                scores = {node_id: value / best for node_id, value in raw.items()}
            return scores, {
                "available": True,
                "mode": "fts5-bm25",
                "hits": len(scores),
                "normalization": "divided by the best raw score in this query",
                "raw_best": best,
            }
        except sqlite3.Error as error:
            return {}, {"available": False, "mode": "fts5", "reason": str(error)}

    if not terms:
        return {}, {"available": False, "mode": "like", "reason": "query had no terms"}
    rows = connection.execute("SELECT id, content_norm FROM nodes").fetchall()
    for row in rows:
        haystack = str(row["content_norm"])
        hits = sum(1 for term in terms if term in haystack)
        if hits:
            scores[str(row["id"])] = hits / len(terms)
    ranked = dict(sorted(scores.items(), key=lambda item: -item[1])[:cap])
    return ranked, {
        "available": True,
        "mode": "like-degraded",
        "hits": len(ranked),
        "note": "FTS5 unavailable; keyword lane ran as a normalized substring scan",
    }


def _vector_lane(
    connection: sqlite3.Connection,
    query_embedding: Sequence[float] | None,
    cap: int,
) -> tuple[dict[str, float], dict[str, Any]]:
    if not query_embedding:
        return {}, {
            "available": False,
            "reason": "no query embedding supplied; vector lane did not run",
        }
    rows = connection.execute(
        "SELECT source_key, note FROM ingest_state WHERE source_key LIKE 'embedding:%'"
    ).fetchall()
    if not rows:
        return {}, {
            "available": False,
            "reason": "no node embeddings imported; vector lane did not run",
        }
    vector = list(query_embedding)
    scores: dict[str, float] = {}
    for row in rows:
        node_id = str(row["source_key"]).split(":", 1)[1]
        try:
            stored = json.loads(str(row["note"]))
        except (TypeError, ValueError):
            continue
        if not isinstance(stored, list):
            continue
        similarity = ids.cosine(vector, [float(value) for value in stored])
        if similarity > 0.0:
            scores[node_id] = similarity
    ranked = dict(sorted(scores.items(), key=lambda item: -item[1])[:cap])
    return ranked, {"available": True, "hits": len(ranked), "dims": len(vector)}


def _graph_lane(
    connection: sqlite3.Connection,
    seeds: dict[str, float],
    max_hops: int,
) -> tuple[dict[str, tuple[float, str]], dict[str, Any]]:
    if not seeds or max_hops < 1:
        return {}, {"available": False, "reason": "no seed nodes for graph expansion"}
    reached: dict[str, tuple[float, str]] = {}
    frontier = {node_id: (score, node_id) for node_id, score in seeds.items()}
    placeholders = ",".join("?" for _ in GRAPH_EXPANSION_EDGE_TYPES)
    for hop in range(1, max_hops + 1):
        next_frontier: dict[str, tuple[float, str]] = {}
        for node_id, (score, path) in frontier.items():
            rows = connection.execute(
                f"""
                SELECT dst_id AS other, edge_type, confidence FROM edges
                WHERE src_id = ? AND edge_type IN ({placeholders})
                UNION ALL
                SELECT src_id AS other, edge_type, confidence FROM edges
                WHERE dst_id = ? AND edge_type IN ({placeholders})
                """,
                (node_id, *GRAPH_EXPANSION_EDGE_TYPES, node_id, *GRAPH_EXPANSION_EDGE_TYPES),
            ).fetchall()
            for row in rows:
                other = str(row["other"])
                if other in seeds or other in reached:
                    continue
                propagated = score * float(row["confidence"]) / (hop + 1)
                trail = f"{path} -{row['edge_type']}-> {other}"
                current = next_frontier.get(other)
                if current is None or propagated > current[0]:
                    next_frontier[other] = (propagated, trail)
        reached.update(next_frontier)
        frontier = next_frontier
        if not frontier:
            break
    return reached, {
        "available": True,
        "hops": max_hops,
        "hits": len(reached),
        "edge_types": list(GRAPH_EXPANSION_EDGE_TYPES),
    }


def _term_weights(
    connection: sqlite3.Connection, terms: list[str]
) -> tuple[dict[str, float], dict[str, Any]]:
    """Weight each query term by how discriminative it is in this map.

    A term that matches almost every node ("run") says little about relevance; a
    term that matches one node ("nomic") says a lot. Weighting the coverage test
    by inverse document frequency is what lets the gate reject a candidate that
    shares only a common word while accepting one that shares the rare word the
    question was really about.

    A term absent from the whole map is dropped from the denominator. No node can
    cover it, so keeping it would make every candidate look uncovered and the map
    would answer nothing at all whenever a question used an unfamiliar word.
    """
    total = int(
        connection.execute("SELECT COUNT(*) AS n FROM nodes").fetchone()["n"] or 0
    )
    frequencies: dict[str, int] = {}
    for term in terms:
        row = connection.execute(
            "SELECT COUNT(*) AS n FROM nodes WHERE content_norm LIKE ?",
            (f"%{term}%",),
        ).fetchone()
        frequencies[term] = int(row["n"] or 0)
    weights = {
        term: ids.inverse_document_frequency(total, count)
        for term, count in frequencies.items()
        if count > 0
    }
    detail = {
        "document_count": total,
        "document_frequency": frequencies,
        "discriminative_terms": sorted(weights),
        "absent_terms": sorted(term for term, count in frequencies.items() if count == 0),
    }
    return weights, detail


def _weighted_coverage(
    weights: dict[str, float], content_norm: str
) -> float:
    denominator = sum(weights.values())
    if denominator <= 0.0:
        return 0.0
    matched = sum(weight for term, weight in weights.items() if term in content_norm)
    return matched / denominator


def _recency_multiplier(
    candidate: Candidate, rules: dict[str, Any], now: int
) -> float:
    retrieval = rules["retrieval"]
    if candidate.node_type in set(retrieval["evergreen_types"]):
        return 1.0
    half_life_ms = float(retrieval["recency_half_life_days"]) * rules_module.DAY_MS
    if half_life_ms <= 0:
        return 1.0
    reference = candidate.last_used_at or candidate.recorded_at
    age_ms = max(0, now - int(reference))
    return float(2.0 ** (-age_ms / half_life_ms))


def _importance_multiplier(candidate: Candidate, rules: dict[str, Any]) -> float:
    retrieval = rules["retrieval"]
    low = float(retrieval["importance_min_multiplier"])
    high = float(retrieval["importance_max_multiplier"])
    if candidate.importance is None:
        return 1.0
    fraction = (candidate.importance - 1) / 9.0
    return low + (high - low) * fraction


def _trust_multiplier(candidate: Candidate, rules: dict[str, Any]) -> float:
    retrieval = rules["retrieval"]
    if candidate.retention_status == "quarantined":
        return float(retrieval["quarantined_trust_multiplier"])
    if candidate.retention_status == "archived":
        return float(retrieval["archived_trust_multiplier"])
    if candidate.retention_status == "dormant":
        return float(retrieval["dormant_trust_multiplier"])
    return 1.0


def _filter_reason(
    candidate: Candidate,
    *,
    include_untrusted: bool,
    include_superseded: bool,
    allowed_types: set[str] | None,
    now: int,
) -> str | None:
    """Why this candidate may not be returned, independent of its score.

    Separating eligibility from scoring matters for one security reason: graph
    expansion seeds from eligible candidates only. Without that split, a
    quarantined node that matched the query could pull a trusted neighbour into
    the result set, which is influence by association and exactly the behaviour
    quarantine is meant to prevent.
    """
    if candidate.retention_status == "deleted":
        return "filter:deleted-tombstone"
    if candidate.node_type == "finding":
        return "filter:finding-node-not-recallable"
    if candidate.retention_status == "quarantined" and not include_untrusted:
        return "filter:quarantined-requires-include-untrusted"
    if candidate.origin_class not in rules_module.TRUSTED_ORIGINS and not include_untrusted:
        return f"filter:origin-{candidate.origin_class}-not-in-trusted-origins"
    if candidate.retention_status == "superseded" and not include_superseded:
        return "filter:superseded-requires-include-superseded"
    if candidate.retention_status == "archived":
        return "filter:archived"
    if candidate.valid_to is not None and candidate.valid_to <= now and not include_superseded:
        return "filter:valid-to-passed"
    if allowed_types is not None and candidate.node_type not in allowed_types:
        return f"filter:node-type-{candidate.node_type}-excluded"
    return None


def search(
    connection: sqlite3.Connection,
    *,
    query: str,
    k: int | None = None,
    min_score: float | None = None,
    query_embedding: Sequence[float] | None = None,
    include_untrusted: bool = False,
    include_superseded: bool = False,
    node_types: Sequence[str] | None = None,
    project_key: str | None = None,
    agent_session: str | None = None,
    at: int | None = None,
) -> SearchOutcome:
    """Run the lanes, score, record the trace, return the surviving candidates."""
    rules = db.active_rules(connection)
    retrieval = rules["retrieval"]
    now = at if at is not None else db.now_ms()
    limit = int(k if k is not None else retrieval["default_k"])
    threshold = float(min_score if min_score is not None else retrieval["default_min_score"])
    cap = int(retrieval["candidate_cap"])

    filters = {
        "include_untrusted": include_untrusted,
        "include_superseded": include_superseded,
        "node_types": list(node_types) if node_types else None,
        "project_key": project_key,
        "min_score": threshold,
        "k": limit,
        "trusted_origins": list(rules_module.TRUSTED_ORIGINS),
    }

    allowed_types = set(node_types) if node_types else None
    terms, stopword_fallback = ids.query_terms(query)
    min_coverage = float(retrieval["min_term_coverage"])
    term_weights, term_detail = _term_weights(connection, terms)
    keyword_scores, keyword_lane = _keyword_lane(connection, terms, cap)
    keyword_lane["terms"] = terms
    keyword_lane["stopwords_removed"] = not stopword_fallback
    keyword_lane["min_term_coverage"] = min_coverage
    keyword_lane["term_weighting"] = term_detail
    vector_scores, vector_lane = _vector_lane(connection, query_embedding, cap)

    seed_scores: dict[str, float] = {}
    for lane_scores in (keyword_scores, vector_scores):
        for node_id, score in lane_scores.items():
            seed_scores[node_id] = max(seed_scores.get(node_id, 0.0), score)

    candidates: dict[str, Candidate] = {}
    low_coverage: dict[str, float] = {}
    for row in _fetch_rows(connection, set(seed_scores)):
        candidate = _row_to_candidate(row)
        candidate.keyword_score = keyword_scores.get(candidate.node_id, 0.0)
        candidate.vector_score = vector_scores.get(candidate.node_id, 0.0)
        if candidate.keyword_score > 0.0:
            candidate.lane_hits.add("keyword")
        if candidate.vector_score > 0.0:
            candidate.lane_hits.add("vector")
        # A disjunctive prefix query matches on a single shared term, which on a
        # question the map cannot answer returns whatever happens to share a
        # word. A lexical candidate therefore has to cover a minimum fraction of
        # the query's content terms. A candidate the vector lane found is exempt,
        # because semantic similarity does not imply shared wording.
        coverage = _weighted_coverage(term_weights, str(row["content_norm"]))
        if candidate.vector_score == 0.0 and coverage < min_coverage:
            low_coverage[candidate.node_id] = coverage
        candidates[candidate.node_id] = candidate

    eligible_seeds = {
        node_id: seed_scores[node_id]
        for node_id, candidate in candidates.items()
        if node_id not in low_coverage
        and _filter_reason(
            candidate,
            include_untrusted=include_untrusted,
            include_superseded=include_superseded,
            allowed_types=allowed_types,
            now=now,
        )
        is None
    }
    graph_reached, graph_lane = _graph_lane(
        connection, eligible_seeds, int(retrieval["graph_max_hops"])
    )
    graph_lane["seeds_considered"] = len(seed_scores)
    graph_lane["seeds_eligible"] = len(eligible_seeds)
    graph_lane["note"] = (
        "expansion seeds exclude filtered candidates so quarantined or retired "
        "content cannot pull a neighbour into the result set"
    )

    for row in _fetch_rows(connection, set(graph_reached) - set(candidates)):
        candidates[str(row["id"])] = _row_to_candidate(row)
    for node_id, (score, path) in graph_reached.items():
        candidate = candidates.get(node_id)
        if candidate is None:
            continue
        candidate.graph_score = score
        candidate.graph_path = path
        candidate.lane_hits.add("graph")

    for candidate in candidates.values():
        candidate.recency_score = _recency_multiplier(candidate, rules, now)
        candidate.importance_mult = _importance_multiplier(candidate, rules)
        candidate.trust_mult = _trust_multiplier(candidate, rules)
        base = (
            float(retrieval["keyword_weight"]) * candidate.keyword_score
            + float(retrieval["vector_weight"]) * candidate.vector_score
            + float(retrieval["graph_weight"]) * candidate.graph_score
        )
        candidate.final_score = (
            base * candidate.recency_score * candidate.importance_mult * candidate.trust_mult
        )
        candidate.drop_reason = _filter_reason(
            candidate,
            include_untrusted=include_untrusted,
            include_superseded=include_superseded,
            allowed_types=allowed_types,
            now=now,
        )
        if (
            candidate.drop_reason is None
            and candidate.node_id in low_coverage
            and candidate.graph_score == 0.0
        ):
            candidate.drop_reason = (
                f"low-term-coverage:{low_coverage[candidate.node_id]:.2f}<{min_coverage:.2f}"
            )
        if candidate.drop_reason is None and candidate.final_score < threshold:
            candidate.drop_reason = (
                f"below-min-score:{candidate.final_score:.6f}<{threshold:.6f}"
            )

    survivors = sorted(
        (candidate for candidate in candidates.values() if candidate.returned),
        key=lambda item: (-item.final_score, item.node_id),
    )
    returned: list[Candidate] = []
    budget = int(retrieval["max_returned_chars"])
    used = 0
    for position, candidate in enumerate(survivors):
        if position >= limit:
            candidate.drop_reason = f"beyond-k:{limit}"
            continue
        length = len(candidate.content)
        if used + length > budget:
            candidate.drop_reason = f"context-budget:{budget}-chars-exhausted"
            continue
        used += length
        returned.append(candidate)

    dropped = [candidate for candidate in candidates.values() if not candidate.returned]
    lanes = {
        "keyword": keyword_lane,
        "vector": vector_lane,
        "graph": graph_lane,
        "recency": {
            "available": True,
            "half_life_days": retrieval["recency_half_life_days"],
            "evergreen_types": list(retrieval["evergreen_types"]),
        },
    }

    trace = record_trace(
        connection,
        query=query,
        k_requested=limit,
        min_score=threshold,
        lanes=lanes,
        filters=filters,
        candidates=list(candidates.values()),
        agent_session=agent_session,
        completeness="full",
        trace_source="map-search",
        at=now,
    )
    return SearchOutcome(trace, returned, dropped, lanes, filters)


def _fetch_rows(connection: sqlite3.Connection, node_ids: set[str]) -> list[sqlite3.Row]:
    if not node_ids:
        return []
    placeholders = ",".join("?" for _ in node_ids)
    return connection.execute(
        f"SELECT * FROM nodes WHERE id IN ({placeholders})", tuple(node_ids)
    ).fetchall()


def record_trace(
    connection: sqlite3.Connection,
    *,
    query: str,
    k_requested: int,
    min_score: float,
    lanes: dict[str, Any],
    filters: dict[str, Any],
    candidates: Sequence[Candidate],
    agent_session: str | None,
    completeness: str,
    trace_source: str,
    at: int,
) -> str:
    rules = db.active_rules(connection)
    identifier = ids.trace_id(query, at, salt=str(len(candidates)))
    ordered = sorted(candidates, key=lambda item: (-item.final_score, item.node_id))
    returned_count = sum(1 for candidate in ordered if candidate.returned)
    connection.execute(
        """
        INSERT INTO traces (
          id, created_at, query, query_hash, agent_session, k_requested,
          min_score, lanes_json, filters_json, candidate_count, returned_count,
          completeness, trace_source, rule_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO NOTHING
        """,
        (
            identifier,
            at,
            query,
            ids.query_hash(query),
            agent_session,
            k_requested,
            min_score,
            json.dumps(lanes, sort_keys=True),
            json.dumps(filters, sort_keys=True),
            len(ordered),
            returned_count,
            completeness,
            trace_source,
            str(rules["version"]),
        ),
    )
    for rank, candidate in enumerate(ordered, start=1):
        connection.execute(
            """
            INSERT INTO trace_candidates (
              trace_id, node_id, rank, returned, drop_reason, lane_hits,
              keyword_score, vector_score, graph_score, recency_score,
              importance_mult, trust_mult, final_score, graph_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trace_id, node_id) DO NOTHING
            """,
            (
                identifier,
                candidate.node_id,
                rank,
                1 if candidate.returned else 0,
                candidate.drop_reason,
                ",".join(sorted(candidate.lane_hits)),
                candidate.keyword_score,
                candidate.vector_score,
                candidate.graph_score,
                candidate.recency_score,
                candidate.importance_mult,
                candidate.trust_mult,
                candidate.final_score,
                candidate.graph_path,
            ),
        )
    _record_retrieved_with(connection, [c for c in ordered if c.returned], at)
    return identifier


def _record_retrieved_with(
    connection: sqlite3.Connection, returned: Sequence[Candidate], at: int
) -> None:
    """Co-retrieval is itself a relationship, recorded as a typed edge."""
    from . import lifecycle  # local import avoids an import cycle

    ordered = [candidate.node_id for candidate in returned]
    for index, left in enumerate(ordered):
        for right in ordered[index + 1 :]:
            first, second = sorted((left, right))
            lifecycle.add_edge(
                connection,
                src_id=first,
                dst_id=second,
                edge_type="retrieved_with",
                confidence=0.3,
                confidence_basis="co-retrieval-observation",
                captured_by="adapter",
                evidence={"observed_at": at},
                at=at,
            )


def explain(connection: sqlite3.Connection, trace_id: str) -> dict[str, Any]:
    """Reconstruct an explanation strictly from the stored trace.

    When a trace is an imported partial record, the explanation says so and lists
    the components that were never captured, rather than filling them in.
    """
    trace = connection.execute("SELECT * FROM traces WHERE id = ?", (trace_id,)).fetchone()
    if trace is None:
        return {"status": "not-found", "trace_id": trace_id}
    rows = connection.execute(
        """
        SELECT tc.*, n.content, n.node_type, n.origin_class, n.retention_status
        FROM trace_candidates AS tc
        LEFT JOIN nodes AS n ON n.id = tc.node_id
        WHERE tc.trace_id = ?
        ORDER BY tc.rank
        """,
        (trace_id,),
    ).fetchall()
    lanes = json.loads(str(trace["lanes_json"]))
    missing: list[str] = []
    if str(trace["completeness"]) != "full":
        for lane in ("keyword", "vector", "graph"):
            if lane not in lanes:
                missing.append(f"{lane} lane components were not captured")
        missing.append("dropped candidates were not captured by the source system")
    return {
        "status": "ok",
        "trace_id": trace_id,
        "created_at": int(trace["created_at"]),
        "query": str(trace["query"]),
        "completeness": str(trace["completeness"]),
        "trace_source": str(trace["trace_source"]),
        "rule_version": str(trace["rule_version"]),
        "lanes": lanes,
        "filters": json.loads(str(trace["filters_json"])),
        "not_captured": missing,
        "candidates": [
            {
                "rank": int(row["rank"]),
                "node_id": str(row["node_id"]),
                "returned": bool(row["returned"]),
                "drop_reason": row["drop_reason"],
                "lane_hits": str(row["lane_hits"]),
                "components": {
                    "keyword": float(row["keyword_score"]),
                    "vector": float(row["vector_score"]),
                    "graph": float(row["graph_score"]),
                    "recency": float(row["recency_score"]),
                    "importance": float(row["importance_mult"]),
                    "trust": float(row["trust_mult"]),
                    "final": float(row["final_score"]),
                },
                "graph_path": row["graph_path"],
                "node_type": row["node_type"],
                "origin_class": row["origin_class"],
                "retention_status": row["retention_status"],
                "content": row["content"],
            }
            for row in rows
        ],
    }


def record_use(
    connection: sqlite3.Connection, node_ids: Sequence[str], *, at: int | None = None
) -> None:
    """Count a retrieval as a use, which is what drives reinforcement."""
    from . import lifecycle

    moment = at if at is not None else db.now_ms()
    for node_id in node_ids:
        lifecycle.apply_transition(
            connection, node_id=node_id, transition="use", actor="agent", at=moment
        )
