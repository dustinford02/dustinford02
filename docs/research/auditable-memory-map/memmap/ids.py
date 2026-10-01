"""Deterministic identity, normalization and similarity helpers.

Standard library only. Every identifier the map mints is a pure function of the
content it names, so re-reading the same OpenClaw entry twice yields one node
rather than two, and a reviewer can recompute any id by hand.
"""

from __future__ import annotations

import math
import re
from hashlib import blake2b
from typing import Iterable

_WHITESPACE = re.compile(r"\s+")
_FUZZY_STRIP = re.compile(r"[^a-z0-9' ]")
_ANNOTATION = re.compile(r"<!--.*?-->", re.DOTALL)
_LIST_MARKER = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")

# Ported from Graphiti's deterministic dedupe pass (graphiti_core/utils/
# maintenance/dedup_helpers.py at commit 3c42764). The constants are theirs; the
# surrounding code is a standard-library reimplementation.
NAME_ENTROPY_THRESHOLD = 1.5
MIN_NAME_LENGTH = 6
MIN_TOKEN_COUNT = 2
FUZZY_JACCARD_THRESHOLD = 0.9
MINHASH_PERMUTATIONS = 32
MINHASH_BAND_SIZE = 4


def digest(*parts: str, size: int = 16) -> str:
    """Stable short hex digest over the given parts."""
    hasher = blake2b(digest_size=size)
    for part in parts:
        hasher.update(part.encode("utf-8"))
        hasher.update(b"\x1f")
    return hasher.hexdigest()


def strip_annotations(text: str) -> str:
    """Remove HTML-comment annotations OpenClaw appends to memory lines."""
    return _ANNOTATION.sub("", text)


def normalize_exact(text: str) -> str:
    """Lowercase, drop annotations and list markers, collapse whitespace."""
    without_annotations = strip_annotations(text)
    without_marker = _LIST_MARKER.sub("", without_annotations)
    return _WHITESPACE.sub(" ", without_marker.lower()).strip()


def normalize_fuzzy(text: str) -> str:
    """Keep alphanumerics and apostrophes so shingles are stable."""
    collapsed = _FUZZY_STRIP.sub(" ", normalize_exact(text))
    return _WHITESPACE.sub(" ", collapsed).strip()


def tokens(text: str) -> list[str]:
    return [token for token in re.findall(r"[a-z0-9']+", normalize_exact(text)) if token]


def content_hash(text: str) -> str:
    return digest("content", normalize_exact(text), size=16)


def node_id(node_type: str, content: str, source_ref: str | None) -> str:
    """Identity is type plus normalized content plus source reference.

    Source reference participates so the same sentence observed in two different
    files stays two nodes with their own provenance; the duplicate detector then
    links them with an explicit edge instead of silently merging provenance.
    """
    return "nd_" + digest("node", node_type, normalize_exact(content), source_ref or "")


def edge_id(src_id: str, dst_id: str, edge_type: str) -> str:
    return "eg_" + digest("edge", src_id, dst_id, edge_type)


def finding_id(kind: str, subject_ids: Iterable[str], detail_key: str = "") -> str:
    ordered = ",".join(sorted(subject_ids))
    return "fd_" + digest("finding", kind, ordered, detail_key)


def trace_id(query: str, created_at_ms: int, salt: str = "") -> str:
    return "tr_" + digest("trace", query, str(created_at_ms), salt)


def change_set_id(title: str, created_at_ms: int) -> str:
    return "cs_" + digest("changeset", title, str(created_at_ms))


def query_hash(query: str) -> str:
    return digest("query", normalize_exact(query), size=12)


SUBJECT_STOPWORDS: frozenset[str] = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "to", "of", "and",
    "or", "not", "no", "never", "always", "prefer", "use", "do", "does",
    "my", "our", "i", "we", "it", "that", "this", "for", "on", "in", "at",
    "with", "from", "by", "as", "but",
})

# Question words and other low-information terms are dropped from a search
# query. Without this, a question like "should I run X" matches almost any
# imperative note through its function words, and a normalized keyword lane then
# presents the best of a bad candidate set as though it were a strong match.
QUERY_STOPWORDS: frozenset[str] = SUBJECT_STOPWORDS | frozenset({
    "what", "which", "when", "where", "who", "whom", "whose", "why", "how",
    "should", "shall", "can", "could", "would", "will", "may", "might", "must",
    "did", "done", "am", "been", "being", "have", "has", "had", "get", "got",
    "me", "mine", "us", "you", "your", "yours", "they", "them", "their",
    "there", "here", "if", "then", "else", "about", "into", "over", "under",
    "than", "so", "such", "any", "some", "all", "just", "now", "again", "very",
    "please", "tell", "show", "give", "need", "want", "like", "ok", "okay",
})


def subject_key(content: str, max_tokens: int = 3) -> str:
    """A coarse topic key used to group candidate conflicts.

    This is deliberately crude: it is a grouping hint for the conflict detector,
    never an assertion that two nodes are about the same thing. The detector
    still has to satisfy its own evidence test before writing a finding.
    """
    content_tokens = [token for token in tokens(content) if token not in SUBJECT_STOPWORDS]
    return "-".join(content_tokens[:max_tokens])


def query_terms(query: str) -> tuple[list[str], bool]:
    """Content-bearing query terms, plus whether a stopword fallback was used."""
    all_terms = tokens(query)
    content_only = [term for term in all_terms if term not in QUERY_STOPWORDS]
    if content_only:
        return content_only, False
    return all_terms, True


def inverse_document_frequency(document_count: int, matching: int) -> float:
    """Smoothed inverse document frequency for one term."""
    if document_count <= 0:
        return 0.0
    return math.log(1.0 + document_count / (1.0 + max(0, matching)))


def name_entropy(normalized: str) -> float:
    """Shannon entropy over characters, as a text specificity proxy."""
    if not normalized:
        return 0.0
    counts: dict[str, int] = {}
    for character in normalized.replace(" ", ""):
        counts[character] = counts.get(character, 0) + 1
    total = sum(counts.values())
    if total == 0:
        return 0.0
    entropy = 0.0
    for count in counts.values():
        probability = count / total
        entropy -= probability * math.log2(probability)
    return entropy


def has_high_entropy(normalized: str) -> bool:
    token_count = len(normalized.split())
    if len(normalized) < MIN_NAME_LENGTH and token_count < MIN_TOKEN_COUNT:
        return False
    return name_entropy(normalized) >= NAME_ENTROPY_THRESHOLD


def shingles(normalized: str) -> set[str]:
    cleaned = normalized.replace(" ", "")
    if len(cleaned) < 2:
        return {cleaned} if cleaned else set()
    return {cleaned[index : index + 3] for index in range(len(cleaned) - 2)}


def _hash_shingle(shingle: str, seed: int) -> int:
    return int.from_bytes(
        blake2b(f"{seed}:{shingle}".encode("utf-8"), digest_size=8).digest(), "big"
    )


def minhash_signature(shingle_set: Iterable[str]) -> tuple[int, ...]:
    materialized = list(shingle_set)
    if not materialized:
        return ()
    return tuple(
        min(_hash_shingle(shingle, seed) for shingle in materialized)
        for seed in range(MINHASH_PERMUTATIONS)
    )


def lsh_bands(signature: Iterable[int]) -> list[tuple[int, ...]]:
    values = list(signature)
    bands: list[tuple[int, ...]] = []
    for start in range(0, len(values), MINHASH_BAND_SIZE):
        band = tuple(values[start : start + MINHASH_BAND_SIZE])
        if len(band) == MINHASH_BAND_SIZE:
            bands.append(band)
    return bands


def jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    union = len(left | right)
    return len(left & right) / union if union else 0.0


def cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)
