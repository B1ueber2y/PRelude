"""Writes to GitHub, limited to the reviewer's own pending (draft) review.

Only the four GraphQL mutations below can be sent. A review is never submitted: no
`event` is ever passed, and after each write the review is checked to still be PENDING.
"""

from __future__ import annotations

import json
import re
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from .config import Config
from .github import gh_token
from .guard import check_repo

GRAPHQL = "https://api.github.com/graphql"

ALLOWED_MUTATIONS = frozenset(
    {
        "addPullRequestReview",
        "addPullRequestReviewThread",
        "updatePullRequestReviewComment",
        "deletePullRequestReviewComment",
    }
)

_MUTATION_CALL = re.compile(r"\b([a-zA-Z]+)\s*\(\s*input\s*:")


class DraftError(RuntimeError):
    pass


class NotPending(DraftError):
    """The review is no longer a draft; PRelude stops writing to it."""


def check_query(query: str) -> None:
    """Refuse anything but the allowed mutations, and any review `event`."""
    if query.lstrip().startswith("mutation"):
        called = set(_MUTATION_CALL.findall(query))
        if not called or not called <= ALLOWED_MUTATIONS:
            raise DraftError(f"mutation not allowed: {sorted(called - ALLOWED_MUTATIONS) or 'unknown'}")
    if re.search(r"\bevent\s*:", query):
        raise DraftError("reviews are never submitted by PRelude")


Transport = Callable[[str, dict], dict]


def _http_transport(token: str) -> Transport:
    def send(query: str, variables: dict) -> dict:
        req = urllib.request.Request(
            GRAPHQL,
            data=json.dumps({"query": query, "variables": variables}).encode(),
            method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": "prelude"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)

    return send


@dataclass
class Draft:
    review_id: str
    comment_id: str


class DraftWriter:
    def __init__(self, cfg: Config, transport: Transport | None = None):
        self.cfg = cfg
        self._transport = transport

    def _call(self, owner: str, repo: str, query: str, variables: dict) -> dict:
        check_repo(self.cfg, owner, repo)
        check_query(query)
        if self._transport is None:
            self._transport = _http_transport(gh_token())
        out = self._transport(query, variables)
        if out.get("errors"):
            raise DraftError("; ".join(e.get("message", "?") for e in out["errors"])[:500])
        return out["data"]

    def pending_review(self, owner: str, repo: str, number: int, commit: str) -> str:
        """The viewer's pending review on the PR, created if needed."""
        data = self._call(
            owner,
            repo,
            """
            query($owner: String!, $repo: String!, $number: Int!) {
              repository(owner: $owner, name: $repo) {
                pullRequest(number: $number) {
                  id
                  reviews(states: PENDING, first: 20) { nodes { id viewerDidAuthor } }
                }
              }
            }""",
            {"owner": owner, "repo": repo, "number": number},
        )
        pr = data["repository"]["pullRequest"]
        for r in pr["reviews"]["nodes"]:
            if r["viewerDidAuthor"]:
                return r["id"]
        data = self._call(
            owner,
            repo,
            """
            mutation($pr: ID!, $commit: GitObjectID!) {
              addPullRequestReview(input: {pullRequestId: $pr, commitOID: $commit}) {
                pullRequestReview { id state }
              }
            }""",
            {"pr": pr["id"], "commit": commit},
        )
        review = data["addPullRequestReview"]["pullRequestReview"]
        if review["state"] != "PENDING":
            raise NotPending(f"new review is {review['state']}, expected PENDING")
        return review["id"]

    def add_comment(
        self,
        owner: str,
        repo: str,
        review_id: str,
        *,
        path: str,
        body: str,
        line: int | None,
        side: str = "RIGHT",
        start_line: int | None = None,
        file_level: bool = False,
    ) -> str:
        variables: dict = {"review": review_id, "path": path, "body": body}
        if file_level or line is None:
            fields = "subjectType: FILE"
        else:
            fields = "line: $line, side: $side"
            variables.update(line=line, side=side)
            if start_line:
                fields += ", startLine: $startLine, startSide: $side"
                variables["startLine"] = start_line
        decls = (
            "$review: ID!, $path: String!, $body: String!"
            + (", $line: Int!, $side: DiffSide!" if "line" in variables else "")
            + (", $startLine: Int!" if "startLine" in variables else "")
        )
        data = self._call(
            owner,
            repo,
            f"""
            mutation({decls}) {{
              addPullRequestReviewThread(input: {{pullRequestReviewId: $review, path: $path, body: $body, {fields}}}) {{
                thread {{ comments(first: 1) {{ nodes {{ id }} }} }}
              }}
            }}""",
            variables,
        )
        thread = data["addPullRequestReviewThread"]["thread"]
        if thread is None:
            raise DraftError("GitHub rejected the comment location")
        self.assert_pending(owner, repo, review_id)
        return thread["comments"]["nodes"][0]["id"]

    def comment_body(self, owner: str, repo: str, comment_id: str) -> str | None:
        data = self._call(
            owner,
            repo,
            """
            query($id: ID!) { node(id: $id) { ... on PullRequestReviewComment { body } } }""",
            {"id": comment_id},
        )
        node = data.get("node")
        return node["body"] if node else None

    def update_comment(self, owner: str, repo: str, review_id: str, comment_id: str, body: str) -> None:
        self.assert_pending(owner, repo, review_id)
        self._call(
            owner,
            repo,
            """
            mutation($id: ID!, $body: String!) {
              updatePullRequestReviewComment(input: {pullRequestReviewCommentId: $id, body: $body}) {
                pullRequestReviewComment { id }
              }
            }""",
            {"id": comment_id, "body": body},
        )

    def delete_comment(self, owner: str, repo: str, review_id: str, comment_id: str) -> None:
        self.assert_pending(owner, repo, review_id)
        self._call(
            owner,
            repo,
            """
            mutation($id: ID!) {
              deletePullRequestReviewComment(input: {id: $id}) { pullRequestReview { id } }
            }""",
            {"id": comment_id},
        )

    def review_state(self, owner: str, repo: str, review_id: str) -> str | None:
        """PENDING, COMMENTED, APPROVED, CHANGES_REQUESTED, DISMISSED, or None if it was deleted."""
        data = self._call(
            owner,
            repo,
            """
            query($id: ID!) { node(id: $id) { ... on PullRequestReview { state } } }""",
            {"id": review_id},
        )
        return (data.get("node") or {}).get("state")

    def assert_pending(self, owner: str, repo: str, review_id: str) -> None:
        data = self._call(
            owner,
            repo,
            """
            query($id: ID!) { node(id: $id) { ... on PullRequestReview { state } } }""",
            {"id": review_id},
        )
        state = (data.get("node") or {}).get("state")
        if state != "PENDING":
            raise NotPending(f"the GitHub review is {state or 'gone'}; it was submitted or deleted")
