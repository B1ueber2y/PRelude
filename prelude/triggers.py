"""Which pull requests to review automatically (only while PRelude is running).

Precedence: your own PR -> blocked people -> always-list requesters -> repo mode -> default mode.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any

MODES = ("all_prs", "on_request", "never")


class TriggerConfigError(ValueError):
    pass


@dataclass
class TriggerConfig:
    enabled: bool = False
    poll_minutes: int = 5
    default_mode: str = "on_request"
    repos: dict[str, str] = field(default_factory=dict)
    since: dict[str, str] = field(default_factory=dict)  # pattern -> ISO date for all_prs repos
    always: set[str] = field(default_factory=set)
    block: set[str] = field(default_factory=set)
    own_prs: bool = True
    team_requests: bool = True
    skip_draft_prs: bool = True
    max_reviews_per_day: int = 20

    @classmethod
    def from_raw(cls, raw: dict[str, Any] | None) -> TriggerConfig:
        raw = raw or {}
        people = raw.get("requesters") or {}
        repos, since = {}, {}
        for pattern, value in (raw.get("repos") or {}).items():
            key = str(pattern).lower()
            if isinstance(value, dict):
                repos[key] = str(value.get("mode", "on_request"))
                if value.get("since"):
                    since[key] = str(value["since"])
            else:
                repos[key] = str(value)
        t = cls(
            enabled=bool(raw.get("enabled", False)),
            poll_minutes=max(2, int(raw.get("poll_minutes", 5))),
            default_mode=str(raw.get("default_mode", "on_request")),
            repos=repos,
            since=since,
            always={str(x).lower() for x in people.get("always") or []},
            block={str(x).lower() for x in people.get("block") or []},
            own_prs=bool(raw.get("own_prs", True)),
            team_requests=bool(raw.get("team_requests", True)),
            skip_draft_prs=bool(raw.get("skip_draft_prs", True)),
            max_reviews_per_day=int(raw.get("max_reviews_per_day", 20)),
        )
        t.validate()
        return t

    def validate(self) -> None:
        for mode in (self.default_mode, *self.repos.values()):
            if mode not in MODES:
                raise TriggerConfigError(f"unknown repo mode {mode!r} (use one of {', '.join(MODES)})")
        if both := self.always & self.block:
            raise TriggerConfigError(f"on both the always and block lists: {', '.join(sorted(both))}")

    def repo_pattern(self, slug: str) -> str | None:
        """The configured pattern for a repo: exact match, else the most specific wildcard."""
        slug = slug.lower()
        if slug in self.repos:
            return slug
        matches = [p for p in self.repos if fnmatchcase(slug, p)]
        return max(matches, key=lambda p: len(p.replace("*", ""))) if matches else None

    def repo_mode(self, slug: str) -> str:
        pattern = self.repo_pattern(slug)
        return self.repos[pattern] if pattern else self.default_mode


@dataclass
class Candidate:
    slug: str  # OWNER/REPO
    number: int
    author: str
    draft: bool = False
    created_at: str = ""  # ISO timestamp
    direct_requesters: set[str] = field(default_factory=set)  # requested you personally
    team_requesters: set[str] = field(default_factory=set)  # requested a team you're on


def decide(
    t: TriggerConfig, me: str, c: Candidate, since: str | None = None, own_since: str | None = None
) -> tuple[bool, str]:
    """Whether to review `c` automatically, and why (shown on the review).

    `since` / `own_since` (ISO dates) limit all_prs repos and your own PRs to PRs opened from
    then on; review requests always count.
    """
    me, author = me.lower(), c.author.lower()
    if t.skip_draft_prs and c.draft:
        return False, "draft PR"
    if author == me:
        if not t.own_prs:
            return False, "your PR (own_prs off)"
        if own_since and c.created_at and c.created_at < own_since:
            return False, f"your PR, opened before {own_since[:10]}, when watching started"
        return True, "your PR"
    if author in t.block:
        return False, f"author {c.author} is blocked"

    direct = {r.lower() for r in c.direct_requesters} - t.block
    team = ({r.lower() for r in c.team_requesters} - t.block) if t.team_requests else set()
    requesters = direct | team

    if vip := sorted(requesters & t.always):
        return True, f"requested by {vip[0]} (always list)"

    mode = t.repo_mode(c.slug)
    if mode == "never":
        return False, f"{c.slug} is set to never"
    if mode == "all_prs" and not (since and c.created_at and c.created_at < since):
        return True, f"new PR in {c.slug} (all_prs)"
    if requesters:
        who = sorted(direct)[0] if direct else sorted(team)[0]
        via = "" if direct else " (team request)"
        return True, f"requested by {who}{via}"
    if (c.direct_requesters or c.team_requesters) and not requesters:
        return False, "only blocked people requested you"
    if mode == "all_prs":
        return False, f"opened before {since[:10]}, when watching {c.slug} started"
    return False, "no review request"
