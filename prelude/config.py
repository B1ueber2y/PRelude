"""Configuration; all data lives under PRELUDE_HOME (default: .prelude-data/)."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .notify import NotifyConfig
from .triggers import TriggerConfig

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_CONFIG = """\
# PRelude configuration.

# Development guard: while set, PRelude refuses to read, review or write any
# GitHub repository except this one. Remove (or set to "") to lift it.
only_repo: B1ueber2y/PRelude

# Claude settings. `model: null` uses Claude Code's default model.
claude:
  model: null
  # Independent reviewers per review (more = more thorough, uses more of your plan).
  # With more than one, a meta-reviewer merges their reports. "auto" picks by PR size
  # (changed lines) using reviewers_by_size: first row whose max_lines is not exceeded.
  reviewers: auto
  reviewers_by_size:
    - {max_lines: 99, reviewers: 1}
    - {max_lines: 499, reviewers: 3}
    - {reviewers: 5}
  lenses:
    - correctness and edge cases
    - security and data handling
    - API contracts, error handling and reliability
  review_timeout_seconds: 1500
  discuss_timeout_seconds: 900

# Diffs larger than this many characters are not inlined in the review prompt;
# Claude reads the changed files with its tools instead.
max_inline_diff_chars: 150000

# Adding comments to your pending GitHub review. Off until you turn it on.
github_drafts:
  enabled: false
  prefix: "AI review with PRelude:"

# Automatic reviews while PRelude is running. Repo modes: all_prs | on_request | never;
# patterns like my-org/* work, the most specific match wins. An all_prs repo only covers
# PRs opened after PRelude started watching it, unless you give a date:
#   my-org/web: {mode: all_prs, since: 2026-10-01}
triggers:
  enabled: false
  poll_minutes: 5
  default_mode: on_request        # for repos not listed
  repos: {}
  requesters:
    always: []                    # their requests are reviewed in any repo
    block: []                     # their requests and PRs are ignored
  own_prs: true
  team_requests: true
  skip_draft_prs: true
  max_reviews_per_day: 20
  requests_since: null            # e.g. 2026-10-10: ignore review requests on older PRs

# Phone notifications via the ntfy app. Run `prelude notify-setup` to create a topic.
notify:
  ntfy_topic: null
  details: ref                    # none, ref (owner/repo#123) or full (also the PR title); ntfy.sh is public
  open_url: http://localhost:8787

server:
  host: 127.0.0.1
  port: 8787
  # When serving through `tailscale serve`, only this Tailscale login is let in.
  allowed_tailscale_user: null
"""


AUTO = 0  # pick reviewers by PR size
DEFAULT_BY_SIZE = ((99, 1), (499, 3), (None, 5))

# Kubernetes-style PR size labels by changed lines.
SIZE_LABELS = ((9, "XS"), (29, "S"), (99, "M"), (499, "L"), (999, "XL"), (None, "XXL"))


def size_label(changed_lines: int) -> str:
    return next(label for limit, label in SIZE_LABELS if limit is None or changed_lines <= limit)


DEFAULT_LENSES = (
    "correctness and edge cases",
    "security and data handling",
    "API contracts, error handling and reliability",
)


@dataclass
class ClaudeConfig:
    model: str | None = None
    reviewers: int = AUTO
    reviewers_by_size: list[tuple[int | None, int]] = field(default_factory=lambda: list(DEFAULT_BY_SIZE))
    lenses: list[str] = field(default_factory=lambda: list(DEFAULT_LENSES))
    review_timeout_seconds: int = 1500
    discuss_timeout_seconds: int = 900


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8787
    allowed_tailscale_user: str | None = None


@dataclass
class DraftsConfig:
    enabled: bool = False
    prefix: str = "AI review with PRelude:"


@dataclass
class Config:
    data_dir: Path
    only_repo: str | None = None
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    drafts: DraftsConfig = field(default_factory=DraftsConfig)
    triggers: TriggerConfig = field(default_factory=TriggerConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
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

    def reviewers_for(self, changed_lines: int) -> int:
        for max_lines, n in self.claude.reviewers_by_size:
            if max_lines is None or changed_lines <= max_lines:
                return n
        return 1

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
    drafts_raw = raw.get("github_drafts") or {}
    return Config(
        data_dir=data_dir,
        only_repo=(raw.get("only_repo") or None),
        claude=ClaudeConfig(
            model=claude_raw.get("model"),
            reviewers=_reviewers(claude_raw.get("reviewers", "auto")),
            reviewers_by_size=[
                (row.get("max_lines"), _reviewers(row.get("reviewers", 1)) or 1)
                for row in (claude_raw.get("reviewers_by_size") or [])
            ]
            or list(DEFAULT_BY_SIZE),
            lenses=[str(x) for x in (claude_raw.get("lenses") or DEFAULT_LENSES)],
            review_timeout_seconds=int(claude_raw.get("review_timeout_seconds", 1500)),
            discuss_timeout_seconds=int(claude_raw.get("discuss_timeout_seconds", 900)),
        ),
        server=ServerConfig(
            host=server_raw.get("host", "127.0.0.1"),
            port=int(server_raw.get("port", 8787)),
            allowed_tailscale_user=server_raw.get("allowed_tailscale_user"),
        ),
        drafts=DraftsConfig(
            enabled=bool(drafts_raw.get("enabled", False)),
            prefix=str(drafts_raw.get("prefix", "AI review with PRelude:")),
        ),
        triggers=TriggerConfig.from_raw(raw.get("triggers")),
        notify=NotifyConfig(
            ntfy_topic=(raw.get("notify") or {}).get("ntfy_topic") or None,
            ntfy_server=str((raw.get("notify") or {}).get("ntfy_server", "https://ntfy.sh")),
            details=_notify_details(raw.get("notify") or {}),
            open_url=str((raw.get("notify") or {}).get("open_url", "http://localhost:8787")),
        ),
        max_inline_diff_chars=int(raw.get("max_inline_diff_chars", 150_000)),
        raw=raw,
    )


def _notify_details(raw: dict) -> str:
    if raw.get("include_details") is True:  # older configs
        return "full"
    value = str(raw.get("details", "ref"))
    return value if value in ("none", "ref", "full") else "ref"


def _reviewers(value) -> int:
    if value in (None, "auto", "", 0):
        return AUTO
    return max(1, min(8, int(value)))
