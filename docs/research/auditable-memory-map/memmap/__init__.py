"""Auditable Memory Map: an audit, governance and explanation layer.

CANDIDATE code. It has been exercised against the synthetic fixtures in `tests/`
and `eval/eval_set.json` in this repository. It has never been run against a live
OpenClaw deployment, and no claim here should be read as a claim that it works in
one.

The package deliberately depends on nothing outside the Python standard library,
holds all state in one SQLite file plus Markdown and CSV reports, needs no
process running between sessions, and returns a degraded status rather than
raising when the map cannot be reached.
"""

from . import rules

__all__ = ["rules"]
__version__ = rules.SCHEMA_VERSION
