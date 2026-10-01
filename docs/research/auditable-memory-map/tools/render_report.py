#!/usr/bin/env python3
"""Render the research report from its template plus the real source files.

The report has to contain the implementation package as full code blocks, and the
repository has to contain the same code as runnable files. Writing it twice would
guarantee drift, so the document is generated: every code block comes from the
file it names.

    python3 tools/render_report.py            # write the report
    python3 tools/render_report.py --check    # fail if the report is stale

Template directive, one per line:

    @@INCLUDE <path-relative-to-package> <fence-language>@@
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent.parent
TEMPLATE = PACKAGE_DIR / "report.template.md.tmpl"
OUTPUT = PACKAGE_DIR.parent / "AUDITABLE_MEMORY_MAP_OPENCLAW_2026-10-01.md"
DIRECTIVE = re.compile(r"^@@INCLUDE\s+(\S+)\s+(\S+)@@$")


def render() -> str:
    lines: list[str] = []
    for raw in TEMPLATE.read_text(encoding="utf-8").splitlines():
        match = DIRECTIVE.match(raw.strip())
        if match is None:
            lines.append(raw)
            continue
        relative, language = match.group(1), match.group(2)
        source = PACKAGE_DIR / relative
        if not source.is_file():
            raise SystemExit(f"template includes a missing file: {relative}")
        body = source.read_text(encoding="utf-8").rstrip("\n")
        lines.append(f"File `docs/research/auditable-memory-map/{relative}`:")
        lines.append("")
        lines.append(f"```{language}")
        lines.extend(body.split("\n"))
        lines.append("```")
    return "\n".join(lines).rstrip("\n") + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rendered = render()
    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.is_file() else ""
        if current != rendered:
            print(f"{OUTPUT.name} is out of date; rerun tools/render_report.py")
            return 1
        print(f"{OUTPUT.name} is up to date")
        return 0
    OUTPUT.write_text(rendered, encoding="utf-8")
    print(f"wrote {OUTPUT} ({len(rendered.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
