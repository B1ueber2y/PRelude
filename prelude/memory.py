"""Reviewer memory: personal review principles plus per-repo notes, written by the reviewer."""

from __future__ import annotations

import re
from pathlib import Path

from .config import Config

_SAFE = re.compile(r"[^A-Za-z0-9._-]")
_SCOPE = re.compile(r"^([\w.-]+)/([\w.-]+)$")

GLOBAL = "global"
STYLE = "style"


def split_scope(scope: str) -> tuple[str, str]:
    m = _SCOPE.match(scope)
    if not m:
        raise ValueError(f"bad memory scope: {scope!r}")
    return m.group(1), m.group(2)


def describe(scope: str) -> str:
    if scope == GLOBAL:
        return "the reviewer's personal review principles (what to flag, what to skip, how strict to be)"
    if scope == STYLE:
        return "the reviewer's comment style guide (how review comments should be worded)"
    return f"review notes for the {scope} repository"


def title(scope: str) -> str:
    return {GLOBAL: "Review principles", STYLE: "Comment style"}.get(scope, f"{scope} notes")


class Memory:
    def __init__(self, cfg: Config):
        self.dir = cfg.data_dir / "memory"

    @property
    def principles_path(self) -> Path:
        return self.dir / "principles.md"

    @property
    def style_path(self) -> Path:
        return self.dir / "style.md"

    def repo_path(self, owner: str, repo: str) -> Path:
        return self.dir / "repos" / f"{_SAFE.sub('_', owner)}__{_SAFE.sub('_', repo)}.md"

    def principles(self) -> str:
        return _read(self.principles_path)

    def repo_notes(self, owner: str, repo: str) -> str:
        return _read(self.repo_path(owner, repo))

    # Scopes: "global" for the principles, "OWNER/REPO" for that repo's notes.

    def read(self, scope: str) -> str:
        return _read(self._path(scope))

    def write(self, scope: str, text: str) -> None:
        _write(self._path(scope), text)

    def _path(self, scope: str) -> Path:
        if scope == GLOBAL:
            return self.principles_path
        if scope == STYLE:
            return self.style_path
        return self.repo_path(*split_scope(scope))

    def remember(self, scope: str, rule: str) -> None:
        """Append one rule as a bullet."""
        rule = " ".join(rule.split())
        if not rule:
            raise ValueError("empty rule")
        current = self.read(scope)
        self.write(scope, (current + "\n" if current else "") + f"- {rule}")

    def prompt_section(self, owner: str, repo: str, style_examples: list[tuple[str, str]] = ()) -> str:
        parts = []
        if p := self.principles():
            parts.append(f"<reviewer_principles>\n{p}\n</reviewer_principles>")
        if n := self.repo_notes(owner, repo):
            parts.append(f"<repo_notes repo=\"{owner}/{repo}\">\n{n}\n</repo_notes>")
        if st := self.read(STYLE):
            parts.append(f"<comment_style>\n{st}\n</comment_style>")
        if style_examples:
            ex = "\n".join(
                f"<example>\n<suggested>{a}</suggested>\n<reviewer_rewrote_as>{b}</reviewer_rewrote_as>\n</example>"
                for a, b in style_examples
            )
            parts.append(f"Recent comments the reviewer rewrote (match their voice):\n{ex}")
        if not parts:
            return ""
        return (
            "The reviewer has written down how they like to review. Follow it; where it conflicts "
            "with the defaults below, it wins.\n\n" + "\n\n".join(parts)
        )


def _read(path: Path) -> str:
    return path.read_text().strip() if path.exists() else ""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text.strip() + "\n" if text.strip() else "")
    tmp.replace(path)
