"""Expandable code around a finding: the PR's version of the file with the diff overlaid."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .diffmap import FileDiff

MAX_LINES = 3000


@dataclass
class Row:
    kind: str  # "+", "-", " "
    text: str
    no: int | None  # head-version line number (None for removed lines)


@dataclass
class CodeView:
    rows: list[Row]
    start: int
    end: int
    total: int

    @property
    def more_above(self) -> bool:
        return self.start > 1

    @property
    def more_below(self) -> bool:
        return self.end < self.total


def read_head_file(worktree: str | Path, path: str) -> list[str] | None:
    """Lines of `path` at the PR head, or None if it's missing or outside the checkout."""
    root = Path(worktree).resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        return None
    try:
        return target.read_text(errors="replace").splitlines()[:MAX_LINES]
    except OSError:
        return None


def build(lines: list[str], fd: FileDiff | None, start: int, end: int) -> CodeView:
    total = len(lines)
    start, end = max(1, start), min(total, end)
    added: set[int] = set()
    removed_before: dict[int, list[str]] = {}
    if fd:
        for h in fd.hunks:
            pending: list[str] = []
            for dl in h.lines:
                if dl.kind == "-":
                    pending.append(dl.text)
                    continue
                if dl.kind == "+":
                    added.add(dl.new_no)
                if pending:
                    removed_before.setdefault(dl.new_no, []).extend(pending)
                    pending = []
            if pending:  # removed lines at the end of a hunk go before the next line
                after = max((dl.new_no for dl in h.lines if dl.new_no), default=0) + 1
                removed_before.setdefault(after, []).extend(pending)
    rows: list[Row] = []
    for n in range(start, end + 1):
        rows.extend(Row("-", text, None) for text in removed_before.get(n, []))
        rows.append(Row("+" if n in added else " ", lines[n - 1], n))
    return CodeView(rows, start, end, total)
