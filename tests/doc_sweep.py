"""Checks the folder documentation against the folders it describes.

Not collected by pytest. It is a script, run by hand:

    python3 -m tests.doc_sweep            # report, exit 1 on any finding
    python3 -m tests.doc_sweep --quiet    # exit code only

It is deliberately not a test. This checkout is edited by many sessions at once
and CI runs the suite on every push to the default branch, so a documentation
rule collected as a test goes red on somebody else's commit that adds a module
-- which is a cost imposed on a person who never asked for it. Run this when you
change a folder, and read what it says. Promoting it is one wrapper away:

    def test_docs_are_current():
        assert not sweep().findings

Three things are checked, and each exists because it is the way this
documentation fails silently:

* **A file with no row.** A `## Layout` table that has stopped listing every
  file in its folder reads as complete. Nothing about it looks wrong -- the
  reader simply never learns the file is there.
* **A row with no file.** The opposite, and worse: a reader goes looking for
  something that was deleted.
* **A link that does not resolve.** The index links thirty-odd documents by
  relative path, and a moved file turns one into a 404 that only shows up when
  somebody clicks it.

What is NOT checked: prose. No script can tell whether a paragraph is true.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Directories whose contents are generated, installed, or captured rather than
# written, and so are documented as a whole rather than file by file.
SKIP_DIRS = {
    ".git", ".github", "node_modules", "__pycache__", ".pytest_cache",
    ".ruff_cache", ".mypy_cache", "dist", "build", "logs", "data", ".venv",
    "venv", "screens", "fonts", "mockups", "mockups-next", "resolver_data",
    "brand", "screenshots",
}

# Files that are not source and do not need a row of their own.
SKIP_FILES = {"README.md", ".gitignore", ".DS_Store", "package-lock.json"}
SKIP_SUFFIXES = {".pyc", ".tsbuildinfo", ".png", ".jpg", ".woff2", ".log"}

# A backticked filename anywhere in a table row. The name has to look like a
# file: a Layout table's first column also carries expressions and type names
# (`frozenset({...})`, `Verdict.WONT_FIT`), and treating those as filenames
# reports a phantom for every one of them.
ROW_NAME = re.compile(r"^\s*\|\s*`([^`]+)`\s*\|")
FILENAME = re.compile(
    r"^[A-Za-z0-9_.@-]+\.(py|ts|tsx|mjs|js|jsx|css|html|json|md|sh|yml|yaml|toml|txt|svg|lock|cfg|ini|Dockerfile)$"
)
# A relative markdown link that is not a URL or an anchor.
LINK = re.compile(r"\[[^\]]*\]\(([^)#][^)]*)\)")


@dataclass
class Finding:
    kind: str
    where: str
    detail: str


@dataclass
class Sweep:
    findings: list[Finding] = field(default_factory=list)
    checked: int = 0

    def report(self) -> str:
        if not self.findings:
            return f"{self.checked} documents checked, nothing stale."
        lines = [f"{self.checked} documents checked, {len(self.findings)} findings.", ""]
        for kind in ("undocumented", "phantom", "broken-link"):
            hits = [f for f in self.findings if f.kind == kind]
            if not hits:
                continue
            lines.append(f"{kind} ({len(hits)}):")
            lines += [f"  {h.where}: {h.detail}" for h in hits]
            lines.append("")
        return "\n".join(lines).rstrip()


def _skipped(path: Path) -> bool:
    return any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts)


def _source_files(folder: Path) -> set[str]:
    """The files in one folder that a README ought to account for."""
    out = set()
    for child in folder.iterdir():
        if child.is_dir() or child.name in SKIP_FILES:
            continue
        if child.suffix in SKIP_SUFFIXES or child.name.startswith("."):
            continue
        out.add(child.name)
    return out


def _documented_names(readme: Path) -> set[str]:
    """Every filename the README names in a Layout row or a `## ` heading."""
    out = set()
    for line in readme.read_text(encoding="utf-8", errors="replace").splitlines():
        row = ROW_NAME.match(line)
        if row:
            name = row.group(1).strip().rstrip("/")
            if FILENAME.match(name) or name.endswith("Dockerfile"):
                out.add(name)
        elif line.startswith("## `") and line.rstrip().endswith("`"):
            name = line[4:].rstrip().rstrip("`")
            if FILENAME.match(name) or name.endswith("Dockerfile"):
                out.add(name)
    return out


def sweep() -> Sweep:
    result = Sweep()
    for readme in sorted(ROOT.rglob("README.md")):
        if _skipped(readme):
            continue
        result.checked += 1
        folder = readme.parent
        rel = readme.relative_to(ROOT)

        present = _source_files(folder)
        named = _documented_names(readme)

        # A README that documents no files at all is an index or an essay, not a
        # folder map. Only hold a document to the inventory rule once it has
        # started keeping one.
        if named & present:
            for missing in sorted(present - named):
                result.findings.append(
                    Finding("undocumented", str(rel), f"`{missing}` is in the folder and not in the document")
                )
            for phantom in sorted(named - present):
                if (folder / phantom).exists():
                    continue
                result.findings.append(
                    Finding("phantom", str(rel), f"`{phantom}` is documented and not in the folder")
                )

    for doc in sorted(list(ROOT.rglob("README.md")) + list((ROOT / "docs").rglob("*.md"))):
        if _skipped(doc):
            continue
        text = doc.read_text(encoding="utf-8", errors="replace")
        for n, line in enumerate(text.splitlines(), 1):
            for target in LINK.findall(line):
                target = target.split("#", 1)[0].strip()
                if not target or "://" in target or target.startswith("mailto:"):
                    continue
                if not (doc.parent / target).exists():
                    result.findings.append(
                        Finding("broken-link", f"{doc.relative_to(ROOT)}:{n}", target)
                    )
    return result


def main(argv: list[str]) -> int:
    result = sweep()
    if "--quiet" not in argv:
        print(result.report())
    return 1 if result.findings else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
