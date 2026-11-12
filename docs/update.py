#!/usr/bin/env python3
"""Bump the release version everywhere it must match.

Drop-in replacement for the old ``update.sh``, which relied on GNU ``sed``'s
in-place editing flag (``sed -i`` with no suffix). BSD ``sed`` (macOS) requires a
suffix argument, so that one-liner fails on macOS. This Python version performs
the same edits on any platform.

    ./update.py 0.10.1
"""
from __future__ import annotations

import re
import subprocess
import sys

# (file, sed pattern, replacement) — mirrors the old update.sh exactly.
EDIT_TEMPLATES: list[tuple[str, str, str]] = [
    ("pyproject.toml", r"^version = \".*\"", 'version = "{version}"'),
    ("charts/secchio/Chart.yaml", r"^version: .*", "version: {version}"),
    ("charts/secchio/Chart.yaml", r"^appVersion: .*", 'appVersion: "{version}"'),
    ("charts/secchio/values.yaml", r"^  tag: .*", "  tag: {tag}"),
    ("README.md", r"--version .*", "--version {version}"),
    ("charts/secchio/README.md", r"--version .*", "--version {version}"),
]


def _apply_edit(path: str, pattern: str, replacement: str, *, version: str, tag: str) -> int:
    """Replace every whole-line match of *pattern* with *replacement*.

    Mirrors ``sed -i 's/PATTERN/REPLACEMENT/' file`` where PATTERN is anchored at
    the line start and consumes the rest of the line. *replacement* may contain
    ``{version}`` / ``{tag}`` placeholders, filled in per match. Returns the
    number of lines changed.
    """
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()

    def _fill(_match: re.Match[str]) -> str:
        return replacement.format(version=version, tag=tag)

    new_text, count = re.subn(pattern, _fill, text, flags=re.MULTILINE)
    if count:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(new_text)
    print(f"  {path} ({count} line(s))")
    return count


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        prog = argv[0] if argv else "update.py"
        sys.stderr.write(f"usage: {prog} <version>  (e.g. {prog} 0.7.1)\n")
        return 2
    version = argv[1]
    tag = f"v{version}"

    for path, pattern, replacement in EDIT_TEMPLATES:
        _apply_edit(path, pattern, replacement, version=version, tag=tag)

    # shellcheck=JA05
    subprocess.run(["uv", "lock"], check=True)

    print(f"set version {version} (tag {tag})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
