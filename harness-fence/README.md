# Harness Fence Kit

> **DESIGN-ONLY — NOT INSTALL AUTHORIZATION.** This kit lints synthetic
> harness descriptions. It does not authorize runtime startup, package or
> container installation, credentials, host access, or external writes.

The review loop is: **AGT briefs → Cursor codes → pull request → AGT reports.**
AGT owns coordination and reporting; Cursor implements the scoped,
reviewable code. A human decides whether any later operational work is
authorized.

## What the fence checks

`fence/rules.yaml` defines twelve fail-closed checks covering host
protection, Anthropic Console API-key boundaries, consumer-auth marker
rejection, the `openclaw@2026.9.2` design pin, injection hygiene, secret
exclusion, side-effect denial, and dual-gated run authorization.

The examples are synthetic:

- `fence/examples/good.harness.yaml` satisfies every rule.
- `fence/examples/bad.harness.yaml` is intentionally blocked for enabling
  host installation, naming a forbidden auth marker, and omitting
  `__RUNAUTH__`.

The intentionally small YAML reader accepts nested mappings and scalar
values. It rejects unsupported YAML constructs instead of guessing.

## Run locally

Requires Node.js 20 or newer. There are no package dependencies.

```bash
cd harness-fence
npm test
node src/cli.mjs check fence/examples/good.harness.yaml
node src/cli.mjs check fence/examples/bad.harness.yaml
```

The packaged command signature is:

```text
harness-fence check <file>
```

A conforming file exits `0`. A blocked or unreadable file exits `1` and
prints rule IDs with human-readable reasons. Invalid command usage exits
`2`.

## Boundaries

This repository contains policy, synthetic examples, and a linter only.
It contains no runtime launcher, installation workflow, live credential,
secret file, external-system path, or write integration.

See `docs/COLLAB.md` for the handoff and review responsibilities.
