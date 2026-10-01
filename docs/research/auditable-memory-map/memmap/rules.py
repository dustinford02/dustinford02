"""The rule set: admission gates, lifecycle thresholds and retrieval weights.

Rules live in one versioned dictionary rather than scattered constants. The
version string is written onto every node, edge, trace, finding and history row,
so an audit can tell which gates were in force when a decision was made, and a
proposed rule change is a reviewable diff rather than a code archaeology task.
"""

from __future__ import annotations

from typing import Any, Final

RULE_VERSION: Final[str] = "rules-2026-10-01.1"
DETECTOR_VERSION: Final[str] = "detectors-2026-10-01.1"
SCHEMA_VERSION: Final[str] = "1"

DAY_MS: Final[int] = 86_400_000

NODE_TYPES: Final[tuple[str, ...]] = (
    "fact",
    "preference",
    "decision",
    "procedure",
    "source",
    "episode",
    "lesson",
    "association",
    "finding",
)

EDGE_TYPES: Final[tuple[str, ...]] = (
    "supports",
    "contradicts",
    "supersedes",
    "duplicates",
    "derived_from",
    "about",
    "part_of",
    "retrieved_with",
)

RETENTION_STATUSES: Final[tuple[str, ...]] = (
    "active",
    "reinforced",
    "dormant",
    "archived",
    "superseded",
    "quarantined",
    "deleted",
)

# Trust levels eligible to be returned by default. Mirrors OpenClaw's rule that
# automatic injection is reserved for owner and agent provenance.
TRUSTED_ORIGINS: Final[tuple[str, ...]] = ("owner", "agent")

# Only these types may decay on disuse. Facts, preferences, decisions and
# procedures change by explicit invalidation or supersession, never by timeout.
DECAY_ELIGIBLE_TYPES: Final[tuple[str, ...]] = ("association", "episode", "lesson")

RULES: Final[dict[str, Any]] = {
    "version": RULE_VERSION,
    "schema_version": SCHEMA_VERSION,
    "admission": {
        # Path prefixes whose content never enters the map, matched against the
        # node's source reference. Path matching is the primary case-file gate
        # because it does not depend on recognising sensitive wording.
        "rejected_path_prefixes": [],
        "rejected_classes": ["case", "pay", "health", "credential", "contact"],
        "quarantine_origins": ["untrusted"],
        # Sessions OpenClaw itself refuses to promote from. The map refuses them
        # too so the two systems cannot disagree about what is promotable.
        "non_promotable_session_kinds": ["cron", "heartbeat", "subagent"],
        "owner_fact_requires_confirmation": True,
        "max_content_chars": 2000,
    },
    "lifecycle": {
        "reinforce_min_uses": 3,
        "reinforce_window_days": 30,
        "dormant_after_days": 60,
        "archive_after_days": 180,
        "corroboration_sources_required": 2,
        "decay_eligible_types": list(DECAY_ELIGIBLE_TYPES),
    },
    "retrieval": {
        # Lane weights. Keyword and vector mirror a hybrid merge; graph is an
        # expansion lane over typed edges; recency is a multiplier, not a lane.
        "keyword_weight": 0.5,
        "vector_weight": 0.5,
        "graph_weight": 0.35,
        "graph_max_hops": 1,
        # A lexical-only candidate must cover at least this fraction of the
        # query's content terms. Without it, a disjunctive prefix query returns
        # whatever shares one word with an unanswerable question.
        "min_term_coverage": 0.5,
        "recency_half_life_days": 30,
        "evergreen_types": ["preference", "procedure", "decision"],
        "importance_min_multiplier": 0.9,
        "importance_max_multiplier": 1.3,
        "quarantined_trust_multiplier": 0.0,
        "dormant_trust_multiplier": 0.6,
        "archived_trust_multiplier": 0.0,
        "default_k": 5,
        "default_min_score": 0.05,
        "candidate_cap": 48,
        # LongMemEval measured a small reader degrading sharply past roughly
        # 3k retrieved tokens, so the returned context is capped well under it.
        "max_returned_chars": 6000,
    },
    "detectors": {
        "version": DETECTOR_VERSION,
        "duplicate_jaccard_threshold": 0.9,
        "conflict_overlap_threshold": 0.6,
        "gap_min_queries": 2,
        "outdated_grace_days": 0,
    },
    "reporting": {
        "trace_report_limit": 20,
        "brief_max_chars": 1200,
    },
    "evaluation": {
        "max_harmful_recall_rate": 0.0,
        "max_harmful_forgetting_rate": 0.10,
        "min_recall_at_k": 0.90,
    },
}


def rules_for_version(version: str) -> dict[str, Any]:
    """Return the in-code rule set when the version matches, else raise.

    Historical rule sets are read back from the database, not reconstructed
    here. Refusing to guess is deliberate: an audit of an old decision must read
    the stored rules rather than today's defaults.
    """
    if version != RULE_VERSION:
        raise KeyError(f"rule version {version!r} is not the in-code version")
    return RULES
