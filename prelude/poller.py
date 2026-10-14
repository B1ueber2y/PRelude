"""While PRelude runs with triggers enabled: check GitHub every few minutes, decide, and queue reviews."""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable

from . import notify
from .config import Config
from .db import Store
from .github import GitHubReader, PrRef
from .guard import RepoNotAllowed
from .review import run_review, start_review
from .triggers import Candidate, decide

log = logging.getLogger(__name__)

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

    def candidates(self) -> dict[tuple[str, str, int], dict]:
        t = self.cfg.triggers
        queries = ["review-requested:@me"]
        if t.own_prs:
            queries.append("author:@me")
        for pattern, mode in t.repos.items():
            if mode == "all_prs":
                owner, _, repo = pattern.partition("/")
                queries.append(f"repo:{pattern}" if repo != "*" else f"user:{owner}")
        found: dict[tuple[str, str, int], dict] = {}
        for q in queries:
            for item in self.reader.search_prs(q):
                found[(item["owner"], item["repo"], item["number"])] = item
        return found

    def poll_once(self) -> list[int]:
        """Check GitHub once; returns the ids of reviews queued."""
        t = self.cfg.triggers
        self._me = self._me or self.reader.viewer_login()
        queued = []
        for (owner, repo, number), item in self.candidates().items():
            ref = PrRef(owner, repo, number)
            try:
                pr = self.reader.pull_request(ref)
                seen = self.store.watched(owner, repo, number)
                if seen and seen["head_sha"] == pr.head_sha:
                    continue  # nothing new since last time
                direct, team = self.reader.review_requests(owner, repo, number, self._me)
                ok, reason = decide(t, self._me, Candidate(
                    slug=f"{owner}/{repo}", number=number, author=item["author"], draft=pr.draft,
                    direct_requesters=direct, team_requesters=team,
                ))
                self.store.set_watched(owner, repo, number, head_sha=pr.head_sha,
                                       decision="review" if ok else "skip", reason=reason)
                if not ok:
                    continue
                if seen and seen["reviewed_sha"]:  # reviewed before; these are new commits
                    if t.on_new_commits == "notify":
                        notify.send(self.cfg.notify, "New commits", "A PR you reviewed has new commits",
                                    path="/", tags="arrows_counterclockwise", sender=self.sender)
                        self.store.set_watched(owner, repo, number, reviewed_sha=pr.head_sha)
                        continue
                    if t.on_new_commits == "ignore":
                        continue
                    reason = f"{reason}; new commits"
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
