"""Configuration; all data lives under PRELUDE_HOME (default: .prelude-data/)."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_CONFIG = """\
# PRelude configuration.

# Development guard: while set, PRelude refuses to read, review or write any
# GitHub repository except this one. Remove (or set to "") to lift it.
only_repo: B1ueber2y/PRelude

# Claude settings. `model: null` uses Claude Code's default model.
claude:
  model: null
  review_timeout_seconds: 1500
  discuss_timeout_seconds: 900

# Diffs larger than this many characters are not inlined in the review prompt;
# Claude reads the changed files with its tools instead.
max_inline_diff_chars: 150000

server:
  host: 127.0.0.1
  port: 8787
  # When serving through `tailscale serve`, only this Tailscale login is let in.
  allowed_tailscale_user: null
"""


@dataclass
class ClaudeConfig:
    model: str | None = None
    review_timeout_seconds: int = 1500
    discuss_timeout_seconds: int = 900


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8787
    allowed_tailscale_user: str | None = None


@dataclass
class Config:
    data_dir: Path
    only_repo: str | None = None
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    max_inline_diff_chars: int = 150_000
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "prelude.sqlite"

    @property
    def repos_dir(self) -> Path:
        return self.data_dir / "repos"

    @property
    def worktrees_dir(self) -> Path:
        return self.data_dir / "worktrees"

    @property
    def access_key_path(self) -> Path:
        return self.data_dir / "access_key"

    def access_key(self) -> str:
        """Secret the phone presents (once, via link) to use the web UI."""
        path = self.access_key_path
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_urlsafe(32))
        return path.read_text().strip()


def default_data_dir() -> Path:
    return Path(os.environ.get("PRELUDE_HOME", REPO_ROOT / ".prelude-data")).expanduser()


def load_config(data_dir: Path | None = None) -> Config:
    data_dir = data_dir or default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(data_dir, 0o700)
    path = data_dir / "config.yaml"
    if not path.exists():
        path.write_text(DEFAULT_CONFIG)
    raw = yaml.safe_load(path.read_text()) or {}

    claude_raw = raw.get("claude") or {}
    server_raw = raw.get("server") or {}
    return Config(
        data_dir=data_dir,
        only_repo=(raw.get("only_repo") or None),
        claude=ClaudeConfig(
            model=claude_raw.get("model"),
            review_timeout_seconds=int(claude_raw.get("review_timeout_seconds", 1500)),
            discuss_timeout_seconds=int(claude_raw.get("discuss_timeout_seconds", 900)),
        ),
        server=ServerConfig(
            host=server_raw.get("host", "127.0.0.1"),
            port=int(server_raw.get("port", 8787)),
            allowed_tailscale_user=server_raw.get("allowed_tailscale_user"),
        ),
        max_inline_diff_chars=int(raw.get("max_inline_diff_chars", 150_000)),
        raw=raw,
    )
