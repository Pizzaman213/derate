"""Turn a Trivy JSON report into a readable summary and a pass/fail.

    python .github/scripts/trivy_summary.py report.json [--summary out.md]

Prints the summary to stdout and, with --summary, appends it to that file
(CI passes $GITHUB_STEP_SUMMARY, so the result is on the run page rather than
in a 300-line log). Exits 1 when any CRITICAL or HIGH finding has a released
fix, and 0 otherwise.

Findings with no fix are counted, never gated on. Debian carries hundreds of
them at any moment, and a check that fails on those fails every run until
somebody turns it off. What it fails on is what a rebuild or a version bump
can actually clear.

Standard library only: it runs on a bare runner before anything is installed.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter

GATED = ("CRITICAL", "HIGH")
ORDER = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN")


def findings(report: dict) -> list[dict]:
    out = []
    for result in report.get("Results") or []:
        for v in result.get("Vulnerabilities") or []:
            out.append({
                "target": result.get("Target", "?"),
                "type": result.get("Type", "?"),
                "pkg": v.get("PkgName", "?"),
                "installed": v.get("InstalledVersion", ""),
                "fixed": v.get("FixedVersion") or "",
                "id": v.get("VulnerabilityID", "?"),
                "severity": v.get("Severity", "UNKNOWN"),
                "title": (v.get("Title") or "").replace("|", "/")[:90],
            })
    return out


def render(found: list[dict]) -> tuple[str, bool]:
    fixable = [f for f in found if f["fixed"]]
    blocking = [f for f in fixable if f["severity"] in GATED]

    lines = ["## Docker image security scan", ""]
    if blocking:
        lines.append(f"**Failed:** {len(blocking)} CRITICAL/HIGH finding(s) have a fix available.")
    else:
        lines.append("**Passed:** no CRITICAL/HIGH finding has a fix available.")
    lines += ["", "| Severity | Fixable | No fix yet |", "|---|---:|---:|"]
    fix_c = Counter(f["severity"] for f in fixable)
    open_c = Counter(f["severity"] for f in found if not f["fixed"])
    for sev in ORDER:
        if fix_c[sev] or open_c[sev]:
            lines.append(f"| {sev} | {fix_c[sev]} | {open_c[sev]} |")

    if fixable:
        lines += ["", "### Fixable now", "",
                  "| Severity | Package | Installed | Fixed in | ID | Where |",
                  "|---|---|---|---|---|---|"]
        seen = set()
        for f in sorted(fixable, key=lambda f: (ORDER.index(f["severity"]) if f["severity"] in ORDER else 9, f["pkg"], f["id"])):
            key = (f["pkg"], f["installed"], f["id"])
            if key in seen:
                continue
            seen.add(key)
            lines.append(
                f"| {f['severity']} | `{f['pkg']}` | {f['installed']} | {f['fixed']} "
                f"| {f['id']} | {f['type']} |"
            )

    unfixed_gated = [f for f in found if not f["fixed"] and f["severity"] in GATED]
    if unfixed_gated:
        pkgs = Counter(f["pkg"] for f in unfixed_gated)
        top = ", ".join(f"`{p}` ({n})" for p, n in pkgs.most_common(12))
        lines += ["", "<details><summary>CRITICAL/HIGH with no fix released yet "
                  f"({len(unfixed_gated)}). Not gated; they clear on a rebuild once upstream ships.</summary>",
                  "", top, "", "</details>"]
    return "\n".join(lines) + "\n", bool(blocking)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("report")
    ap.add_argument("--summary", help="append the markdown here (e.g. $GITHUB_STEP_SUMMARY)")
    args = ap.parse_args(argv)
    with open(args.report) as fh:
        text, failed = render(findings(json.load(fh)))
    sys.stdout.write(text)
    if args.summary:
        with open(args.summary, "a") as fh:
            fh.write(text)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
