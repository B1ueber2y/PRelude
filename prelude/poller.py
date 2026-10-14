"""While PRelude runs with triggers enabled: check GitHub every few minutes, decide, and queue reviews."""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

from . import notify
from .config import Config
from .db import Store
from .github import GitHubReader, PrRef
from .guard import RepoNotAllowed
from .review import run_review, start_review
from .triggers import Candidate, decide

log = logging.getLogger(__name__)

OWN = "@own"  # watch-start key for your own PRs
LAST_POLL = "poll_last"
LAST_ERROR = "poll_error"


class Poller:
    def __init__(self, store: Store, cfg: Config, reader: GitHubReader | None = None,
                 run: Callable[[int], None] | None = None, sender: notify.Sender | None = None):
        self.store, self.cfg = store, cfg
        self.reader = reader or GitHubReader(cfg)
        self.run = run or (lambda review_id: run_review(store, cfg, review_id))
        self.sender = sender
        self.queue: queue.Queue[int] = queue.Queue()
        self._me: str | None = None
        self._stop = threading.Event()

    # ---- one round -----------------------------------------------------------

    def since(self, pattern: str) -> str:
        """Start date for an all_prs pattern: configured, else when PRelude first watched it."""
        if configured := self.cfg.triggers.since.get(pattern):
            return configured
        key = f"watch_since:{pattern}"
        started = self.store.setting(key, "")
        if not started:
            started = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.store.set_setting(key, started)
        return started

    def candidates(self) -> dict[tuple[str, str, int], dict]:
        t = self.cfg.triggers
        queries = ["review-requested:@me"]
        if t.own_prs:
            queries.append(f"author:@me created:>={self.since(OWN)[:10]}")
        for pattern, mode in t.repos.items():
            if mode == "all_prs":
                owner, _, repo = pattern.partition("/")
                scope = f"repo:{pattern}" if repo != "*" else f"user:{owner}"
                queries.append(f"{scope} created:>={self.since(pattern)[:10]}")
        found: dict[tuple[str, str, int], dict] = {}
        for q in queries:
            for item in self.reader.search_prs(q):
                found[(item["owner"], item["repo"], item["number"])] = item
        return found

    def evaluate(self, owner: str, repo: str, number: int, item: dict):
        """(pull request, review?, reason) for one candidate."""
        self._me = self._me or self.reader.viewer_login()
        pr = self.reader.pull_request(PrRef(owner, repo, number))
        direct, team = self.reader.review_requests(owner, repo, number, self._me)
        t = self.cfg.triggers
        pattern = t.repo_pattern(f"{owner}/{repo}")
        since = self.since(pattern) if pattern and t.repos[pattern] == "all_prs" else None
        ok, reason = decide(t, self._me, Candidate(
            slug=f"{owner}/{repo}", number=number, author=item["author"], draft=pr.draft,
            created_at=item.get("created_at", ""), direct_requesters=direct, team_requesters=team,
        ), since=since, own_since=self.since(OWN))
        return pr, ok, reason

    def poll_once(self) -> list[int]:
        """Check GitHub once; returns the ids of reviews queued."""
        t = self.cfg.triggers
        queued = []
        for (owner, repo, number), item in self.candidates().items():
            ref = PrRef(owner, repo, number)
            try:
                seen = self.store.watched(owner, repo, number)
                if seen and item.get("updated_at") and seen["updated_at"] == item["updated_at"]:
                    continue  # unchanged since last check: no further API calls
                pr, ok, reason = self.evaluate(owner, repo, number, item)
                if seen and seen["head_sha"] == pr.head_sha:
                    self.store.set_watched(owner, repo, number, updated_at=item.get("updated_at"))
                    continue  # nothing new since last time
                self.store.set_watched(owner, repo, number, head_sha=pr.head_sha, updated_at=item.get("updated_at"),
                                       decision="review" if ok else "skip", reason=reason)
                if not ok:
                    continue
                if seen and seen["reviewed_sha"]:
                    continue  # new commits on a reviewed PR: shown in the app, re-review is your call
                if self.store.auto_reviews_since(time.time() - 86400) >= t.max_reviews_per_day:
                    log.info("daily cap reached; not reviewing %s", ref.slug)
                    continue
                review_id = start_review(self.store, self.cfg, ref, trigger_reason=f"auto: {reason}")
                self.store.set_watched(owner, repo, number, reviewed_sha=pr.head_sha)
                self.queue.put(review_id)
                queued.append(review_id)
            except RepoNotAllowed:
                continue
        return queued

    # ---- background threads ---------------------------------------------------

    def start(self) -> None:
        threading.Thread(target=self._poll_loop, daemon=True, name="poller").start()
        threading.Thread(target=self._review_loop, daemon=True, name="auto-reviews").start()

    def stop(self) -> None:
        self._stop.set()

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
                self.store.set_setting(LAST_ERROR, "")
            except Exception as e:
                log.warning("polling GitHub failed", exc_info=True)
                self.store.set_setting(LAST_ERROR, f"{type(e).__name__}: {e}"[:300])
            self.store.set_setting(LAST_POLL, str(time.time()))
            self._stop.wait(self.cfg.triggers.poll_minutes * 60)

    def _review_loop(self) -> None:
        """Automatic reviews run one at a time."""
        while not self._stop.is_set():
            try:
                review_id = self.queue.get(timeout=5)
            except queue.Empty:
                continue
            self.run(review_id)
