"""Running a review: fetch the PR, check it out, ask Claude (one reviewer, or several plus a
meta-reviewer), store findings."""

from __future__ import annotations

import json
import logging
import os
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import claude, drafting, failures
from .config import Config
from .db import Store
from .diffmap import parse_diff
from .drafts import DraftWriter
from .github import GitHubReader, PrRef, PullRequest
from .guard import check_repo
from .memory import Memory
from .notify import review_finished, send
from .prompts import (
    ADD_SCHEMA,
    MERGE_SCHEMA,
    REVIEW_SCHEMA,
    SEVERITIES,
    add_reviewers_prompt,
    comments_section,
    merge_prompt,
    previous_section,
    review_prompt,
)
from .workspace import Checkout, diff_between, prepare_checkout

log = logging.getLogger(__name__)


def start_review(
    store: Store,
    cfg: Config,
    ref: PrRef,
    trigger_reason: str = "manual",
    reviewers: int | None = None,
    previous_review_id: int | None = None,
    mode: str = "full",
) -> int:
    """Record a queued review. Raises RepoNotAllowed before anything is stored.

    With `previous_review_id` it's a re-review: `full` looks at the whole PR again, `changes`
    focuses on commits since that review. Either way, dismissed findings aren't raised again.
    """
    check_repo(cfg, ref.owner, ref.repo)
    if mode not in ("full", "changes"):
        raise ValueError(f"unknown review mode {mode!r}")
    n = max(0, min(8, cfg.claude.reviewers if reviewers is None else reviewers))  # 0 = by PR size
    return store.create_review(
        ref.owner,
        ref.repo,
        ref.number,
        ref.url,
        trigger_reason,
        reviewers=n,
        previous_review_id=previous_review_id,
        mode=mode,
    )


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


def _structured(result: claude.RunResult) -> dict[str, Any]:
    out = result.structured
    if not isinstance(out, dict) or not isinstance(out.get("findings"), list):
        raise claude.ClaudeError("Claude returned no structured findings")
    return out


@dataclass
class _Job:
    store: Store
    cfg: Config
    review_id: int
    ref: PrRef
    pr: PullRequest
    co: Checkout
    memory: str
    previous: str = ""
    comments: str = ""

    @property
    def inline(self) -> bool:
        return len(self.co.diff) <= self.cfg.max_inline_diff_chars

    def progress(self, text: str) -> None:
        self.store.update_review(self.review_id, progress=text)

    def run(self, prompt: str, schema: dict) -> claude.RunResult:
        return claude.run_json(
            prompt,
            self.co.worktree,
            schema=schema,
            model=self.cfg.claude.model,
            timeout=self.cfg.claude.review_timeout_seconds,
            auth=claude.auth_for(self.cfg),
        )

    def review_prompt(self, lens: str | None = None) -> str:
        return review_prompt(
            slug=self.ref.slug,
            title=self.pr.title,
            author=self.pr.author,
            base_ref=self.pr.base_ref,
            head_ref=self.pr.head_ref,
            body=self.pr.body,
            changed_files=self.co.changed_files,
            diff=self.co.diff,
            diff_inlined=self.inline,
            memory=self.memory,
            lens=lens,
            previous=self.previous,
            comments=self.comments,
        )


def _single(job: _Job) -> tuple[dict, claude.RunResult, float]:
    job.progress("Claude is reviewing")
    result = job.run(job.review_prompt(), REVIEW_SCHEMA)
    return _structured(result), result, result.cost_usd or 0.0


def _run_reviewers(job: _Job, n: int, first_lens: int = 0) -> tuple[list[dict], float]:
    """N independent reviewers in parallel, each with its own focus."""
    lenses = job.cfg.claude.lenses or [None]
    reports: list[dict] = []
    cost = 0.0
    job.progress(f"{n} reviewers are reviewing (0/{n} done)")
    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = [
            pool.submit(job.run, job.review_prompt(lenses[(first_lens + i) % len(lenses)]), REVIEW_SCHEMA)
            for i in range(n)
        ]
        for done, fut in enumerate(as_completed(futures), start=1):
            try:
                result = fut.result()
                reports.append(_structured(result))
                cost += result.cost_usd or 0.0
            except Exception:
                log.warning("one reviewer of review %s failed", job.review_id, exc_info=True)
            job.progress(f"{n} reviewers are reviewing ({done}/{n} done)")
    if not reports:
        raise claude.ClaudeError(f"all {n} reviewers failed")
    return reports, cost


def _panel(job: _Job, n: int) -> tuple[dict, claude.RunResult, float, list[dict]]:
    """N independent reviewers, then a meta-reviewer merges their reports."""
    reports, cost = _run_reviewers(job, n)
    job.progress(f"Meta-reviewer is merging {len(reports)} reports")
    result = job.run(
        merge_prompt(
            slug=job.ref.slug,
            title=job.pr.title,
            reports=reports,
            changed_files=job.co.changed_files,
            diff=job.co.diff,
            diff_inlined=job.inline,
            memory=job.memory,
            previous=job.previous,
            comments=job.comments,
        ),
        MERGE_SCHEMA,
    )
    return _structured(result), result, cost + (result.cost_usd or 0.0), reports


def _read_comments(github, ref: PrRef) -> list[dict]:
    try:
        return github.pr_comments(ref)
    except Exception:  # a review without the conversation is still useful
        log.warning("couldn't read comments of %s", ref.slug, exc_info=True)
        return []


def _previous(store: Store, cfg: Config, row, head_sha: str) -> str:
    prev = store.get_review(row["previous_review_id"]) if row["previous_review_id"] else None
    if prev is None or not prev["head_sha"]:
        return ""
    changes = None
    if row["mode"] == "changes":
        changes = diff_between(cfg, row["owner"], row["repo"], prev["head_sha"], head_sha)
    findings = [dict(f) for f in store.findings_for(prev["id"])]
    return previous_section(mode=row["mode"], old_head=prev["head_sha"], findings=findings, changes=changes)


def _record_failure(store: Store, cfg: Config, review_id: int, what: str, exc: Exception, **fields) -> None:
    """Mark the review failed even if writing the report itself fails."""
    detail = "".join(traceback.format_exception(exc))[-8000:]
    try:
        row = store.get_review(review_id)
        report = failures.save_report(
            cfg,
            what,
            {
                "review": review_id,
                "pr": f"{row['owner']}/{row['repo']}#{row['number']}",
                "head": row["head_sha"],
                "reviewers": row["reviewers"],
                "mode": row["mode"],
            },
            exc,
        )
        detail = f"Report: {report}\n\n{detail}"
    except Exception:
        log.warning("couldn't save the failure report for review %s", review_id, exc_info=True)
    store.update_review(
        review_id, progress=None, error=f"{failures.reason(exc)}: {exc}"[:2000], error_detail=detail, **fields
    )


def _repeats(f, drafts) -> bool:
    """Whether a new finding is likely one already in the draft from an earlier review (same spot)."""

    def same_spot(d) -> bool:
        if d["path"] != f["path"] or d["side"] != f["side"] or (d["line"] is None) != (f["line"] is None):
            return False
        return d["line"] is None or abs(d["line"] - f["line"]) <= 5

    return any(same_spot(d) for d in drafts)


def _auto_draft(
    store: Store, cfg: Config, review_id: int, ids: list[int], writer: DraftWriter | None, *, replace: bool
) -> None:
    """Draft new findings. A new review (`replace`) first takes over earlier reviews' untouched auto-drafts,
    even when it found nothing."""
    if not (cfg.drafts.enabled and cfg.drafts.auto) or not (ids or replace):
        return
    writer = writer or DraftWriter(cfg)
    store.update_review(review_id, progress="Updating your GitHub draft")
    if replace:
        drafting.supersede(store, cfg, writer, review_id)
    if not ids:
        return
    r = store.get_review(review_id)
    drafts = store.drafts_on_pr(r["owner"], r["repo"], r["number"], besides=review_id)
    ids = [i for i in ids if not _repeats(store.get_finding(i), drafts)]  # those wait in To review
    drafting.auto_add(store, cfg, writer, ids)


def _in_draft(store: Store, ids: list[int]) -> str:
    drafted = sum(1 for i in ids if store.get_finding(i)["status"] == "drafted")
    return f", {drafted} in your draft" if drafted else ""


def run_review(
    store: Store, cfg: Config, review_id: int, github: GitHubReader | None = None, writer: DraftWriter | None = None
) -> None:
    """Run a queued review to completion. Errors are recorded on the review, not raised."""
    row = store.get_review(review_id)
    if row is None:
        raise KeyError(review_id)
    ref = PrRef(row["owner"], row["repo"], row["number"])
    try:
        check_repo(cfg, ref.owner, ref.repo)
        store.update_review(
            review_id,
            status="running",
            progress="Checking Claude login",
            error=None,
            runner_pid=os.getpid(),
        )
        claude.ensure_logged_in(claude.auth_for(cfg))

        store.update_review(review_id, progress="Reading the PR from GitHub")
        github = github or GitHubReader(cfg)
        pr = github.pull_request(ref)
        if pr.state != "open" and (row["trigger_reason"] or "").startswith("auto:"):
            store.update_review(
                review_id,
                status="skipped",
                progress=None,
                title=pr.title,
                error=f"The PR was {'merged' if pr.merged else 'closed'} before its review started.",
            )
            return
        n = row["reviewers"] or cfg.reviewers_for(pr.changed_lines)
        store.update_review(
            review_id,
            title=pr.title,
            body=pr.body,
            author=pr.author,
            base_ref=pr.base_ref,
            base_sha=pr.base_sha,
            head_sha=pr.head_sha,
            reviewers=n,
            changed_lines=pr.changed_lines,
        )

        pr_comments = _read_comments(github, ref)
        store.update_review(review_id, pr_comments=json.dumps(pr_comments))
        store.update_review(review_id, progress="Checking out the PR")
        co = prepare_checkout(cfg, pr)
        store.update_review(review_id, worktree=str(co.worktree), merge_base=co.merge_base, diff=co.diff)

        job = _Job(
            store,
            cfg,
            review_id,
            ref,
            pr,
            co,
            memory=Memory(cfg).prompt_section(ref.owner, ref.repo, store.recent_edits()),
            previous=_previous(store, cfg, row, pr.head_sha),
            comments=comments_section(pr_comments),
        )
        reports = None
        if n > 1:
            out, result, cost, reports = _panel(job, n)
        else:
            out, result, cost = _single(job)

        files = parse_diff(co.diff)
        ids = store.add_findings(review_id, [_normalize(f, files) for f in out["findings"] if isinstance(f, dict)])
        if pr.state == "open":
            _auto_draft(store, cfg, review_id, ids, writer, replace=True)
        store.update_review(
            review_id,
            status="done",
            finished_at=time.time(),
            progress=None,
            error=None,
            summary=out.get("summary") or "",
            comment_status=json.dumps(out.get("existing_comments") or []),
            claude_session_id=result.session_id,
            cost_usd=cost,
            reports=None if reports is None else json.dumps(reports),
        )
    except Exception as e:  # recorded for the UI; the server keeps running
        log.exception("review %s failed", review_id)
        _record_failure(store, cfg, review_id, "review", e, status="failed")
    final = store.get_review(review_id)
    if final["status"] == "skipped":
        return
    review_finished(cfg.notify, final, store.findings_for(review_id))


def add_reviewers(
    store: Store,
    cfg: Config,
    review_id: int,
    n: int,
    github: GitHubReader | None = None,
    writer: DraftWriter | None = None,
) -> None:
    """Ask `n` more independent reviewers and merge what they add into the existing review.

    The reviewer's decisions (drafts, discards, edits, discussions) are kept. Errors are recorded.
    """
    row = store.get_review(review_id)
    if row is None or row["status"] != "done":
        raise ValueError("only a finished review can get more reviewers")
    ref = PrRef(row["owner"], row["repo"], row["number"])
    try:
        check_repo(cfg, ref.owner, ref.repo)
        if not row["progress"]:  # not claimed by the caller yet
            store.update_review(
                review_id, progress=f"Adding {n} reviewer{'s' if n > 1 else ''}", error=None, runner_pid=os.getpid()
            )
        claude.ensure_logged_in(claude.auth_for(cfg))
        pr = (github or GitHubReader(cfg)).pull_request(ref)
        if pr.head_sha != row["head_sha"]:
            raise failures.PrMoved("the PR has new commits since this review; use Review again instead")
        wt = Path(row["worktree"]) if row["worktree"] else None
        if wt and wt.exists():
            co = Checkout(wt, row["merge_base"], row["diff"] or "", list(parse_diff(row["diff"] or "")))
        else:
            co = prepare_checkout(cfg, pr)
            store.update_review(review_id, worktree=str(co.worktree))
        job = _Job(
            store,
            cfg,
            review_id,
            ref,
            pr,
            co,
            memory=Memory(cfg).prompt_section(ref.owner, ref.repo, store.recent_edits()),
            comments=comments_section(_read_comments(github or GitHubReader(cfg), ref), judge=False),
            previous=_previous(store, cfg, row, pr.head_sha),
        )
        before = row["reviewers"] or 1
        reports, cost = _run_reviewers(job, n, first_lens=before)

        job.progress(f"Meta-reviewer is merging {len(reports)} new reports")
        existing = [dict(f) for f in store.findings_for(review_id)]
        result = job.run(
            add_reviewers_prompt(
                slug=ref.slug,
                title=pr.title,
                existing=existing,
                reports=reports,
                changed_files=co.changed_files,
                diff=co.diff,
                diff_inlined=job.inline,
                memory=job.memory,
                comments=job.comments,
            ),
            ADD_SCHEMA,
        )
        out = result.structured if isinstance(result.structured, dict) else {}
        ids = {f["id"] for f in existing}
        for hit in out.get("also_found") or []:
            if hit.get("id") in ids and int(hit.get("count") or 0) > 0:
                store.bump_found_by(hit["id"], min(int(hit["count"]), len(reports)), before)
        files = parse_diff(co.diff)
        new = [_normalize(f, files) for f in out.get("new_findings") or [] if isinstance(f, dict)]
        ids = store.add_findings(review_id, new, added_later=True)
        if pr.state == "open":
            _auto_draft(store, cfg, review_id, ids, writer, replace=False)
        store.update_review(
            review_id,
            progress=None,
            finished_at=time.time(),
            reviewers=before + len(reports),
            cost_usd=(row["cost_usd"] or 0) + cost + (result.cost_usd or 0),
        )
        send(
            cfg.notify,
            "More reviewers done",
            f"{len(new)} new finding{'' if len(new) == 1 else 's'}" + _in_draft(store, ids),
            path=f"/reviews/{review_id}",
            tags="mag",
            link=f"{row['url']}/files" if _in_draft(store, ids) else None,
        )
    except Exception as e:
        log.exception("adding reviewers to review %s failed", review_id)
        _record_failure(store, cfg, review_id, "adding reviewers", e)
        send(cfg.notify, "Adding reviewers failed", failures.reason(e), path=f"/reviews/{review_id}", tags="warning")
