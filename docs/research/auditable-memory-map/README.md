# Auditable Memory Map (candidate implementation)

This directory holds the candidate implementation package that accompanies
[the research report](../AUDITABLE_MEMORY_MAP_OPENCLAW_2026-10-01.md). It is
research output, not part of the profile tooling in this repository, and nothing
here runs as part of `npm run lint`, `npm run build` or `npm run check:links`.

**Status: candidate.** The code has been exercised only against the synthetic
fixtures in `tests/` and `eval/eval_set.json` on this machine. It has never run
against a live OpenClaw installation, and no claim here should be read as a claim
that it works in one. Read the report's coverage and risk sections before
deploying any of it.

## What it is

An audit, governance and explanation layer that sits beside OpenClaw's own
memory. It reads OpenClaw's index and memory files read-only, represents each
item as a node with provenance and bitemporal validity, records typed edges,
captures a retrieval trace at query time, enforces lifecycle transitions in code,
detects conflicts, duplicates, gaps and outdated entries, and renders reports an
owner can read without running anything.

## Layout

| Path | Contents |
| --- | --- |
| `schema.sql` | SQLite DDL for nodes, edges, traces, findings, change sets, rule versions |
| `memmap/rules.py` | The versioned rule set: admission, lifecycle, retrieval, detectors |
| `memmap/admission.py` | The admission gate and refusal classes |
| `memmap/lifecycle.py` | The deterministic lifecycle state machine |
| `memmap/retrieval.py` | Retrieval lanes, the trace recorder, the explain path |
| `memmap/detectors.py` | Conflict, duplicate, gap and outdated detectors |
| `memmap/adapter_openclaw.py` | Read-only adapter over OpenClaw's index and files |
| `memmap/reports.py` | Markdown and CSV inspection views |
| `memmap/changesets.py` | The gated improvement loop |
| `memmap/evaluate.py` | The evaluation runner |
| `memmap/cli.py` | Command-line entry points |
| `agents-snippet.txt` | The Markdown fragment to paste into the agent's `AGENTS.md` |
| `eval/eval_set.json` | Fixtures, retrieval cases, admission cases, thresholds |
| `tests/` | Unit tests for every lifecycle rule and detector |

## Running it

Python 3.12 or newer, standard library only.

```bash
cd docs/research/auditable-memory-map
python3 -m unittest discover -s tests          # the test suite
python3 -m memmap.cli --db /tmp/map.sqlite init
python3 -m memmap.cli --db /tmp/map.sqlite ingest --workspace /path/to/workspace
python3 -m memmap.cli --db /tmp/map.sqlite search "how should the gateway be bound"
python3 -m memmap.cli --db /tmp/map.sqlite why tr_...
python3 -m memmap.cli --db /tmp/map.sqlite audit
python3 -m memmap.cli --db /tmp/map.sqlite report --out /tmp/map-reports
```

Add `--openclaw-db ~/.openclaw/agents/<agentId>/agent/openclaw-agent.sqlite` to
`ingest` to read OpenClaw's per-chunk provenance instead of inferring it from
file paths. The adapter opens that database with `mode=ro`.

## Safety properties the tests cover

- Refused content is never stored; only its class and hash are logged.
- Untrusted and unconfirmed content cannot be returned by a default search.
- Graph expansion never seeds from a filtered candidate.
- A superseded value is dropped with a recorded reason rather than returned.
- Explanations come only from stored traces; imported traces declare what was
  never captured.
- A change set cannot be applied without a passing evaluation and owner
  approval, and every applied change set carries a rollback plan.
- A missing or unreadable map returns a degraded status instead of raising.
