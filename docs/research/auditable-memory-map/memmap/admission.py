"""The admission gate.

Every candidate passes through `evaluate` before anything is written. The gate is
deterministic code: a model may propose a node, but only this function decides
whether it is admitted, quarantined or refused, and the decision is logged with
a reason code either way.

Refusal classes come from the owner's standing rule that case details, pay,
health information and credentials never enter memory. Two independent gates
implement it: a path gate (the source file lives under a refused root) and a
content gate (refused wording). The path gate is the stronger of the two because
it does not depend on recognising sensitive phrasing; the content gate is defence
in depth with known false-negative risk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from . import ids

# Content patterns, ordered most specific first. These are a keyword gate, not a
# classifier: they will miss paraphrases. The path gate and the owner's own
# write discipline remain the primary controls.
CREDENTIAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\b(?:api[_-]?key|secret|passwd|password|token)\s*[:=]\s*\S{6,}", re.I),
    re.compile(r"\bbearer\s+[A-Za-z0-9._-]{20,}", re.I),
)

PAY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:salary|wage|wages|pay rate|hourly rate|annual pay|take[- ]home|"
        r"compensation|bonus|base pay|gross pay|net pay)\b",
        re.I,
    ),
    re.compile(r"\b(?:paid|earns?|earning)\b[^.]{0,40}[$£€]\s?\d", re.I),
)

HEALTH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:diagnos(?:is|ed|es)|prescrib(?:ed|ption)|dosage|mg\b|symptom|"
        r"therapy session|therapist|psychiatr\w*|medical record|blood pressure|"
        r"test results?|lab results?|icd-?10)\b",
        re.I,
    ),
)

CASE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:case\s*(?:no\.?|number|#)|docket(?:\s*no\.?|\s*#)?|"
        r"civil action|plaintiff|defendant|deposition|subpoena|"
        r"settlement (?:amount|offer)|attorney[- ]client)\b",
        re.I,
    ),
    re.compile(r"\b\d{1,2}:\d{2}-cv-\d{3,6}\b", re.I),
)

CONTACT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b"),
    re.compile(r"(?<!\d)(?:\+?\d{1,2}[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?!\d)"),
)

CLASS_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "credential": CREDENTIAL_PATTERNS,
    "case": CASE_PATTERNS,
    "health": HEALTH_PATTERNS,
    "pay": PAY_PATTERNS,
    "contact": CONTACT_PATTERNS,
}

# First-person markers that make a claim a statement about the owner, which the
# owner's rule says may only persist after explicit confirmation.
OWNER_MARKERS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:the owner|my|mine|i am|i'm|i prefer|i always|i never|we always)\b", re.I),
    re.compile(r"\bowner(?:'s)?\b", re.I),
)


@dataclass(frozen=True)
class AdmissionDecision:
    decision: str  # admitted | quarantined | rejected
    reason_code: str
    redaction_class: str | None = None
    retention_status: str = "active"
    quarantine_reason: str | None = None
    owner_confirmation: str = "not-required"
    about_owner: bool = False
    confidence: float = 0.5
    confidence_basis: str = "single-source"
    notes: tuple[str, ...] = field(default=())

    @property
    def admitted(self) -> bool:
        return self.decision in {"admitted", "quarantined"}


def classify_sensitive(content: str) -> str | None:
    """Return the first refused class the content matches, else None."""
    for class_name, patterns in CLASS_PATTERNS.items():
        for pattern in patterns:
            if pattern.search(content):
                return class_name
    return None


def is_about_owner(content: str) -> bool:
    return any(pattern.search(content) for pattern in OWNER_MARKERS)


def _path_refused(source_ref: str | None, prefixes: list[str]) -> str | None:
    if not source_ref:
        return None
    normalized = source_ref.replace("\\", "/").lower()
    for prefix in prefixes:
        candidate = prefix.replace("\\", "/").lower().rstrip("/")
        if candidate and (normalized.startswith(candidate) or f"/{candidate}" in normalized):
            return prefix
    return None


def evaluate(
    *,
    content: str,
    node_type: str,
    origin_class: str,
    session_kind: str,
    source_ref: str | None,
    rules: dict[str, Any],
    corroborating_sources: int = 1,
    owner_confirmed: bool = False,
) -> AdmissionDecision:
    """Decide whether a candidate may enter the map, and in what state."""
    admission = rules["admission"]
    notes: list[str] = []

    if not content.strip():
        return AdmissionDecision("rejected", "empty-content")

    if len(content) > int(admission["max_content_chars"]):
        return AdmissionDecision("rejected", "content-too-long")

    refused_prefix = _path_refused(source_ref, list(admission["rejected_path_prefixes"]))
    if refused_prefix is not None:
        return AdmissionDecision(
            "rejected", "refused-source-path", redaction_class="case"
        )

    sensitive = classify_sensitive(content)
    if sensitive is not None and sensitive in set(admission["rejected_classes"]):
        return AdmissionDecision("rejected", "refused-class", redaction_class=sensitive)

    if session_kind in set(admission["non_promotable_session_kinds"]):
        return AdmissionDecision("rejected", "non-promotable-session-kind")

    if origin_class == "system":
        return AdmissionDecision("rejected", "system-origin-not-durable")

    about_owner = is_about_owner(content) and node_type in {"fact", "preference"}

    if origin_class in set(admission["quarantine_origins"]):
        corroborated = corroborating_sources >= int(
            rules["lifecycle"]["corroboration_sources_required"]
        )
        if not corroborated:
            notes.append("untrusted origin held for corroboration or owner confirmation")
            return AdmissionDecision(
                "quarantined",
                "untrusted-origin",
                retention_status="quarantined",
                quarantine_reason="untrusted-origin",
                owner_confirmation="pending" if about_owner else "not-required",
                about_owner=about_owner,
                confidence=0.2,
                confidence_basis="untrusted-uncorroborated",
                notes=tuple(notes),
            )
        notes.append("untrusted origin corroborated by independent sources")
        return AdmissionDecision(
            "admitted",
            "untrusted-corroborated",
            retention_status="active",
            confidence=0.5,
            confidence_basis="multi-source-corroborated",
            about_owner=about_owner,
            notes=tuple(notes),
        )

    if about_owner and admission["owner_fact_requires_confirmation"] and not owner_confirmed:
        return AdmissionDecision(
            "quarantined",
            "owner-fact-unconfirmed",
            retention_status="quarantined",
            quarantine_reason="awaiting-owner-confirmation",
            owner_confirmation="pending",
            about_owner=True,
            confidence=0.4,
            confidence_basis="single-source",
            notes=("statement about the owner held until the owner confirms it",),
        )

    if owner_confirmed:
        return AdmissionDecision(
            "admitted",
            "owner-confirmed",
            confidence=0.95,
            confidence_basis="owner-confirmed",
            owner_confirmation="confirmed",
            about_owner=about_owner,
        )

    basis = "single-source" if origin_class == "owner" else "agent-inference"
    confidence = 0.7 if origin_class == "owner" else 0.5
    if corroborating_sources >= int(rules["lifecycle"]["corroboration_sources_required"]):
        basis = "multi-source-corroborated"
        confidence = min(0.9, confidence + 0.2)
    return AdmissionDecision(
        "admitted",
        "trusted-origin",
        confidence=confidence,
        confidence_basis=basis,
        about_owner=about_owner,
    )


def log_decision(
    connection,
    decision: AdmissionDecision,
    *,
    content: str,
    origin_class: str,
    source_ref: str | None,
    node_id: str | None,
    rule_version: str,
    at: int,
) -> None:
    """Record the decision. Refused content is represented only by its hash."""
    connection.execute(
        """
        INSERT INTO admission_log (
          at, decision, reason_code, redaction_class, origin_class, source_ref,
          content_hash, node_id, rule_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            at,
            decision.decision,
            decision.reason_code,
            decision.redaction_class,
            origin_class,
            source_ref,
            ids.content_hash(content),
            node_id,
            rule_version,
        ),
    )
