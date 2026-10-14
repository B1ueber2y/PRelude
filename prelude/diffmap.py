"""Unified-diff parsing: commentable lines and code snippets."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


@dataclass
class DiffLine:
    kind: str  # "+", "-", " "
    text: str
    old_no: int | None
    new_no: int | None


@dataclass
class Hunk:
    header: str
    lines: list[DiffLine] = field(default_factory=list)


@dataclass
class FileDiff:
    path: str
    old_path: str | None
    hunks: list[Hunk] = field(default_factory=list)
    binary: bool = False

    def commentable(self, line: int, side: str = "RIGHT") -> bool:
        for h in self.hunks:
            for dl in h.lines:
                if side == "RIGHT" and dl.new_no == line and dl.kind in "+ ":
                    return True
                if side == "LEFT" and dl.old_no == line and dl.kind in "- ":
                    return True
        return False

    def nearest_commentable(self, line: int) -> int | None:
        candidates = [dl.new_no for h in self.hunks for dl in h.lines if dl.new_no is not None and dl.kind in "+ "]
        if not candidates:
            return None
        return min(candidates, key=lambda n: (abs(n - line), n))

    def snippet(self, line: int, side: str = "RIGHT", context: int = 6) -> list[DiffLine]:
        """Diff lines around `line`, from the hunk that contains it."""
        for h in self.hunks:
            for i, dl in enumerate(h.lines):
                hit = dl.new_no == line if side == "RIGHT" else dl.old_no == line
                if hit:
                    return h.lines[max(0, i - context) : i + context + 1]
        return []


def parse_diff(text: str) -> dict[str, FileDiff]:
    files: dict[str, FileDiff] = {}
    cur: FileDiff | None = None
    hunk: Hunk | None = None
    old_no = new_no = 0
    old_path: str | None = None

    for raw in text.splitlines():
        if raw.startswith("diff --git "):
            cur, hunk, old_path = None, None, None
            m = re.match(r"^diff --git a/(.*) b/(.*)$", raw)
            if m:
                cur = FileDiff(path=m.group(2), old_path=m.group(1))
                files[cur.path] = cur
            continue
        if cur is None:
            continue
        if hunk is None:
            if raw.startswith("--- "):
                p = raw[4:]
                old_path = None if p == "/dev/null" else p.removeprefix("a/")
                cur.old_path = old_path
                continue
            if raw.startswith("+++ "):
                p = raw[4:]
                if p != "/dev/null":
                    new_path = p.removeprefix("b/")
                    if new_path != cur.path:
                        files.pop(cur.path, None)
                        cur.path = new_path
                        files[new_path] = cur
                continue
            if raw.startswith("Binary files"):
                cur.binary = True
                continue
        m = _HUNK.match(raw)
        if m:
            old_no, new_no = int(m.group(1)), int(m.group(3))
            hunk = Hunk(header=raw)
            cur.hunks.append(hunk)
            continue
        if hunk is None:
            continue
        if raw.startswith("+"):
            hunk.lines.append(DiffLine("+", raw[1:], None, new_no))
            new_no += 1
        elif raw.startswith("-"):
            hunk.lines.append(DiffLine("-", raw[1:], old_no, None))
            old_no += 1
        elif raw.startswith(" ") or raw == "":
            hunk.lines.append(DiffLine(" ", raw[1:], old_no, new_no))
            old_no += 1
            new_no += 1
        # "\ No newline at end of file" and other markers are ignored.
    return files
