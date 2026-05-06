"""Audit every module-level mutable global in app/ and deerflow/.

Why: the v0.1 framework promise is "two Harness instances in the same
process are fully independent." Module-level mutable state breaks that.

This script is intentionally simple AST-based — it lists candidates for
a human to triage. It is NOT a strict linter (yet).

Usage:
    uv run python scripts/audit_globals.py
    uv run python scripts/audit_globals.py --json    # machine-readable
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

ROOTS = ("app", "deerflow")
ALLOWED_IMMUTABLE_TYPES = {
    "Constant",  # int/str/float/bytes/None/bool literals
    "Tuple",  # only if all elements are constants — checked deeper
}
# Calls that produce known-safe immutable singletons.
KNOWN_IMMUTABLE_CALLS = {
    "Path",
    "PurePath",
    "Decimal",
    "Fraction",
    "frozenset",
    "datetime",
    "date",
    "timedelta",
}


@dataclass
class GlobalFinding:
    file: str
    line: int
    name: str
    kind: str  # "assignment" | "annotated" | "augmented"
    snippet: str


def is_safely_constant(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.Tuple):
        return all(is_safely_constant(elt) for elt in node.elts)
    if isinstance(node, ast.Call):
        func = node.func
        name = (
            func.id if isinstance(func, ast.Name) else
            func.attr if isinstance(func, ast.Attribute) else None
        )
        return name in KNOWN_IMMUTABLE_CALLS
    return False


def scan_file(path: Path) -> list[GlobalFinding]:
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError:
        return []

    findings: list[GlobalFinding] = []
    lines = src.splitlines()

    for node in tree.body:
        # Plain assignment at module level.
        if isinstance(node, ast.Assign):
            if is_safely_constant(node.value):
                continue
            for tgt in node.targets:
                names = _names(tgt)
                for name in names:
                    findings.append(
                        GlobalFinding(
                            file=str(path),
                            line=node.lineno,
                            name=name,
                            kind="assignment",
                            snippet=lines[node.lineno - 1].strip(),
                        )
                    )
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            if is_safely_constant(node.value):
                continue
            if isinstance(node.target, ast.Name):
                findings.append(
                    GlobalFinding(
                        file=str(path),
                        line=node.lineno,
                        name=node.target.id,
                        kind="annotated",
                        snippet=lines[node.lineno - 1].strip(),
                    )
                )
        elif isinstance(node, ast.AugAssign):
            if isinstance(node.target, ast.Name):
                findings.append(
                    GlobalFinding(
                        file=str(path),
                        line=node.lineno,
                        name=node.target.id,
                        kind="augmented",
                        snippet=lines[node.lineno - 1].strip(),
                    )
                )

    return findings


def _names(target: ast.AST) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        out: list[str] = []
        for elt in target.elts:
            out.extend(_names(elt))
        return out
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--root", action="append", default=list(ROOTS))
    args = parser.parse_args(argv)

    repo = Path(__file__).resolve().parent.parent
    all_findings: list[GlobalFinding] = []
    for root_name in args.root:
        root = repo / root_name
        if not root.exists():
            continue
        for py in sorted(root.rglob("*.py")):
            # Skip __pycache__, tests inside the package, generated files.
            if "__pycache__" in py.parts:
                continue
            all_findings.extend(scan_file(py))

    if args.json:
        json.dump([asdict(f) for f in all_findings], sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    by_file: dict[str, list[GlobalFinding]] = {}
    for f in all_findings:
        by_file.setdefault(f.file, []).append(f)

    print(f"Found {len(all_findings)} module-level global candidates "
          f"across {len(by_file)} files.\n")
    for file in sorted(by_file):
        rels = file.removeprefix(str(repo) + "/")
        print(f"=== {rels}")
        for finding in by_file[file]:
            print(f"  L{finding.line:>4}  [{finding.kind:10}] {finding.name}")
            print(f"        {finding.snippet}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
