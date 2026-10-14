"""Failure reports: a plain-words reason for notifications, and a full report on disk."""

from __future__ import annotations

import sqlite3
import subprocess
import time
import traceback
import urllib.error
from pathlib import Path

from . import claude
from .config import REPO_ROOT, Config
from .workspace import GitError


class PrMoved(Exception):
    """The PR changed since the review this operation builds on."""


def reason(exc: BaseException) -> str:
    """What broke, in words that are safe to send through a public notification relay."""
    if isinstance(exc, PrMoved):
        return "The PR has new commits; use Review again"
    if isinstance(exc, claude.ClaudeNotLoggedIn):
        return "Claude login needed"
    if isinstance(exc, claude.ClaudeError):
        return "Claude timed out" if "timed out" in str(exc) else "Claude run failed"
    if isinstance(exc, GitError):
        return "Couldn't check out the PR"
    if isinstance(exc, urllib.error.HTTPError | urllib.error.URLError):
        return "GitHub request failed"
    if isinstance(exc, sqlite3.Error):
        return "Database error"
    return f"Unexpected error ({type(exc).__name__})"


def _version() -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=10
        )
        return out.stdout.strip() or "unknown"
    except OSError:
        return "unknown"


def save_report(cfg: Config, what: str, context: dict, exc: BaseException) -> Path:
    """Write a full report for diagnosis; returns its path."""
    folder = cfg.data_dir / "errors"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = folder / f"{stamp}-{what.replace(' ', '-')}.txt"
    lines = [
        f"what: {what}",
        f"when: {time.strftime('%Y-%m-%d %H:%M:%S %z')}",
        f"prelude: {_version()}",
        *(f"{k}: {v}" for k, v in context.items()),
        f"reason: {reason(exc)}",
        "",
        "".join(traceback.format_exception(exc)),
    ]
    path.write_text("\n".join(lines))
    return path
