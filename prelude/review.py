"""Running a review: fetch the PR, check it out, ask Claude, store findings."""

from __future__ import annotations

import logging
from typing import Any

from . import claude
from .config import Config
from .db import Store
from .diffmap import parse_diff
from .github import GitHubReader, PrRef
from .guard import check_repo
from .memory import Memory
from .prompts import REVIEW_SCHEMA, SEVERITIES, review_prompt
from .workspace import prepare_checkout

log = logging.getLogger(__name__)


def start_review(store: Store, cfg: Config, ref: PrRef, trigger_reason: str = "manual") -> int:
    """Record a queued review. Raises RepoNotAllowed before anything is stored."""
    check_repo(cfg, ref.owner, ref.repo)
    return store.create_review(ref.owner, ref.repo, ref.number, ref.url, trigger_reason)


def _normalize(raw: dict[str, Any], files: dict) -> dict[str, Any]:
    f = dict(raw)
    f["severity"] = f.get("severity") if f.get("severity") in SEVERITIES else "low"
    f["side"] = "LEFT" if f.get("side") == "LEFT" else "RIGHT"
    path = str(f.get("path", ""))
    f["path"] = path[2:] if path.startswith("./") else path
    fd = files.get(f["path"])
    line = f.get("line")
    f["in_diff"] = bool(fd and isinstance(line, int) and fd.commentable(line, f["side"]))
    start = f.get("start_line")
    if not (isinstance(start, int) and isinstance(line, int) and start < line):
        f["start_line"] = None
    return f


def run_review(store: Store, cfg: Config, review_id: int, github: GitHubReader | None = None) -> None:
    """Run a queued review to completion. Errors are recorded on the review, not raised."""
    row = store.get_review(review_id)
    if row is None:
        raise KeyError(review_id)
    ref = PrRef(row["owner"], row["repo"], row["number"])
    try:
        check_repo(cfg, ref.owner, ref.repo)
        store.update_review(review_id, status="running", progress="Checking Claude login", error=None)
        claude.ensure_logged_in()

        store.update_review(review_id, progress="Reading the PR from GitHub")
        pr = (github or GitHubReader(cfg)).pull_request(ref)
        store.update_review(
            review_id, title=pr.title, author=pr.author, base_ref=pr.base_ref,
            base_sha=pr.base_sha, head_sha=pr.head_sha,
        )

        store.update_review(review_id, progress="Checking out the PR")
        co = prepare_checkout(cfg, pr)
        store.update_review(review_id, worktree=str(co.worktree), merge_base=co.merge_base, diff=co.diff)

        inline = len(co.diff) <= cfg.max_inline_diff_chars
        prompt = review_prompt(
            slug=ref.slug, title=pr.title, author=pr.author, base_ref=pr.base_ref,
            head_ref=pr.head_ref, body=pr.body, changed_files=co.changed_files,
            diff=co.diff, diff_inlined=inline, memory=Memory(cfg).prompt_section(ref.owner, ref.repo, store.recent_edits()),
        )

        store.update_review(review_id, progress="Claude is reviewing")
        result = claude.run_json(
            prompt, co.worktree, schema=REVIEW_SCHEMA, model=cfg.claude.model,
            timeout=cfg.claude.review_timeout_seconds,
        )
        out = result.structured
        if not isinstance(out, dict) or not isinstance(out.get("findings"), list):
            raise claude.ClaudeError("Claude returned no structured findings")

        files = parse_diff(co.diff)
        findings = [_normalize(f, files) for f in out["findings"] if isinstance(f, dict)]
        store.add_findings(review_id, findings)
        store.update_review(
            review_id, status="done", progress=None, summary=out.get("summary") or "",
            claude_session_id=result.session_id, cost_usd=result.cost_usd,
        )
    except Exception as e:  # recorded for the UI; the server keeps running
        log.exception("review %s failed", review_id)
        store.update_review(
            review_id, status="failed", progress=None,
            error=f"{type(e).__name__}: {e}"[:4000],
        )
