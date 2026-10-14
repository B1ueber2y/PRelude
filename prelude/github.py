"""Read-only GitHub access (GET requests only)."""

from __future__ import annotations

import json
import re
import subprocess
import urllib.request
from dataclasses import dataclass

from .config import Config
from .guard import check_repo

API = "https://api.github.com"

_PR_URL = re.compile(r"^https?://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)(?:[/?#].*)?$")
_PR_SHORT = re.compile(r"^([\w.-]+)/([\w.-]+)#(\d+)$")


@dataclass(frozen=True)
class PrRef:
    owner: str
    repo: str
    number: int

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}#{self.number}"

    @property
    def url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}/pull/{self.number}"


def parse_pr(text: str) -> PrRef:
    text = text.strip()
    m = _PR_URL.match(text) or _PR_SHORT.match(text)
    if not m:
        raise ValueError(f"not a GitHub PR link: {text!r} (expected https://github.com/OWNER/REPO/pull/N)")
    return PrRef(m.group(1), m.group(2), int(m.group(3)))


def gh_token() -> str:
    """The token of the `gh` CLI login on this machine."""
    out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=30)
    if out.returncode != 0 or not out.stdout.strip():
        raise RuntimeError("GitHub CLI is not logged in (run `gh auth login`)")
    return out.stdout.strip()


@dataclass
class PullRequest:
    ref: PrRef
    title: str
    body: str
    author: str
    state: str
    draft: bool
    base_ref: str
    base_sha: str
    head_ref: str
    head_sha: str
    html_url: str
    additions: int = 0
    deletions: int = 0

    @property
    def changed_lines(self) -> int:
        return self.additions + self.deletions


class GitHubReader:
    def __init__(self, cfg: Config, token: str | None = None):
        self.cfg = cfg
        self._token = token

    @property
    def token(self) -> str:
        if self._token is None:
            self._token = gh_token()
        return self._token

    def _get(self, owner: str, repo: str, path: str) -> object:
        check_repo(self.cfg, owner, repo)
        return self._request(f"/repos/{owner}/{repo}{path}")

    def _request(self, path: str) -> object:
        req = urllib.request.Request(
            f"{API}{path}",
            method="GET",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "prelude",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)

    def pull_request(self, ref: PrRef) -> PullRequest:
        data = self._get(ref.owner, ref.repo, f"/pulls/{ref.number}")
        assert isinstance(data, dict)
        return PullRequest(
            ref=ref,
            title=data["title"],
            body=data.get("body") or "",
            author=data["user"]["login"],
            state=data["state"],
            draft=bool(data.get("draft")),
            base_ref=data["base"]["ref"],
            base_sha=data["base"]["sha"],
            head_ref=data["head"]["ref"],
            head_sha=data["head"]["sha"],
            html_url=data["html_url"],
            additions=int(data.get("additions") or 0),
            deletions=int(data.get("deletions") or 0),
        )

    def viewer_login(self) -> str:
        data = self._request("/user")
        assert isinstance(data, dict)
        return data["login"]

    def comments_by(self, owner: str, repo: str, author: str, limit: int = 150) -> list[str]:
        """Recent PR comments (inline and conversation) written by `author`, newest first."""
        found: list[tuple[str, str]] = []
        for kind in ("pulls/comments", "issues/comments"):
            for page in range(1, 6):
                batch = self._get(owner, repo, f"/{kind}?sort=created&direction=desc&per_page=100&page={page}")
                assert isinstance(batch, list)
                for c in batch:
                    if (c.get("user") or {}).get("login", "").lower() != author.lower():
                        continue
                    if kind == "issues/comments" and "/pull/" not in c.get("html_url", ""):
                        continue
                    found.append((c.get("created_at", ""), c.get("body") or ""))
                if len(batch) < 100 or len(found) >= limit:
                    break
        found.sort(reverse=True)
        return [body for _, body in found[:limit] if body.strip()]
