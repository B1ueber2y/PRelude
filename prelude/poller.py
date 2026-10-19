"""While PRelude runs with triggers enabled: check GitHub every few minutes, decide, and queue reviews."""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime

from . import notify, prstate
from .config import Config
from .db import Store
from .github import GitHubReader, PrRef
from .guard import RepoNotAllowed
from .review import FOUND, run_review, start_review
from .triggers import Candidate, TriggerConfig, decide

log = logging.getLogger(__name__)

OWN = "@own"  # watch-start key for your own PRs
LAST_POLL = "poll_last"
LAST_ERROR = "poll_error"


CAP_WINDOW = 86400  # the cap counts automatic reviews started in the last 24 hours


def _counted(store: Store, t: TriggerConfig) -> list[tuple[str, float]]:
    reviews = store.auto_reviews_since(time.time() - CAP_WINDOW)
    return [(slug, started) for slug, reason, started in reviews if t.counts_toward_cap(slug, reason)]


def cap_usage(store: Store, t: TriggerConfig) -> Counter[str]:
    """OWNER/REPO -> automatic reviews in the last 24 hours that count toward max_reviews_per_day."""
    return Counter(slug for slug, _ in _counted(store, t))


def reviews_toward_cap(store: Store, t: TriggerConfig) -> int:
    return len(_counted(store, t))


def next_cap_slot(store: Store, t: TriggerConfig) -> float | None:
    """When the cap has room again, if it's full now (None if it has room, or a cap of 0 never will)."""
    starts = sorted(started for _, started in _counted(store, t))
    if len(starts) < t.max_reviews_per_day or t.max_reviews_per_day <= 0:
        return None
    return starts[len(starts) - t.max_reviews_per_day] + CAP_WINDOW  # enough have aged out


class Poller:
    def __init__(
        self,
        store: Store,
        cfg: Config,
        reader: GitHubReader | None = None,
        run: Callable[[int], None] | None = None,
        sender: notify.Sender | None = None,
    ):
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
        """(pull request, review?, reason, direct requests to you) for one candidate."""
        self._me = self._me or self.reader.viewer_login()
        pr = self.reader.pull_request(PrRef(owner, repo, number))
        direct, team, mine = self.reader.review_requests(owner, repo, number, self._me)
        t = self.cfg.triggers
        pattern = t.repo_pattern(f"{owner}/{repo}")
        since = self.since(pattern) if pattern and t.repos[pattern] == "all_prs" else None
        ok, reason = decide(
            t,
            self._me,
            Candidate(
                slug=f"{owner}/{repo}",
                number=number,
                author=item["author"],
                draft=pr.draft,
                created_at=item.get("created_at", ""),
                direct_requesters=direct,
                team_requesters=team,
            ),
            since=since,
            own_since=self.since(OWN),
        )
        return pr, ok, reason, mine

    def poll_once(self) -> list[int]:
        """Check GitHub once; returns the ids of reviews queued, or left waiting for you."""
        t = self.cfg.triggers
        queued = []
        for (owner, repo, number), item in self.candidates().items():
            ref = PrRef(owner, repo, number)
            try:
                seen = self.store.watched(owner, repo, number)
                if seen and item.get("updated_at") and seen["updated_at"] == item["updated_at"]:
                    continue  # unchanged since last check: no further API calls
                pr, ok, reason, mine = self.evaluate(owner, repo, number, item)
                again = self._rerequested(ref, pr, seen, mine) if ok and seen and seen["reviewed_sha"] else None
                if again:
                    reason = f"re-requested by {again[1]}"
                became_eligible = ok and seen and seen["decision"] == "skip"  # e.g. a draft marked ready
                if seen and seen["head_sha"] == pr.head_sha and not again and not became_eligible:
                    self.store.set_watched(owner, repo, number, updated_at=item.get("updated_at"))
                    continue  # nothing new since last time
                self.store.set_watched(
                    owner,
                    repo,
                    number,
                    head_sha=pr.head_sha,
                    updated_at=item.get("updated_at"),
                    decision="review" if ok else "skip",
                    reason=reason,
                )
                if not ok:
                    continue
                if seen and seen["reviewed_sha"] and not again:
                    continue  # new commits on a reviewed PR: shown in the app, re-review is your call
                latest = self.store.latest_review_for(owner, repo, number, done_only=False)
                if latest and latest["status"] == "waiting":
                    continue  # still waiting for you; you'll review the PR as it is then
                if latest and latest["status"] in ("queued", "running"):
                    # Already being reviewed (e.g. by hand); it marks the PR reviewed when done. Look
                    # again next round in case it fails.
                    self.store.set_watched(owner, repo, number, updated_at=None, head_sha=None)
                    continue
                ask = not t.auto_start  # you start it: the cap is for reviews no one started
                if not ask and t.counts_toward_cap(ref.slug, reason) and self._capped_today() >= t.max_reviews_per_day:
                    log.info("daily cap reached; not reviewing %s", ref.slug)
                    continue
                review_id = start_review(
                    self.store,
                    self.cfg,
                    ref,
                    trigger_reason=f"{FOUND} {reason}" if ask else f"auto: {reason}",
                    previous_review_id=again[0]["id"] if again else None,
                    mode="changes" if again else "full",
                )
                if again:
                    self.store.set_watched(owner, repo, number, request_used=again[2])
                self.store.update_review(review_id, title=pr.title, author=pr.author)  # shown while queued
                self.store.set_watched(owner, repo, number, reviewed_sha=pr.head_sha)
                if ask:
                    self.store.update_review(review_id, status="waiting")
                    notify.send(
                        self.cfg.notify,
                        "Ready to review",
                        notify.pr_prefix(self.cfg.notify, f"{ref.slug}#{number}", pr.title)
                        + (reason if self.cfg.notify.details != "none" else "a PR waits for you"),
                        path=f"/reviews/{review_id}",
                        tags="eyes",
                        sender=self.sender,
                    )
                else:
                    self.queue.put(review_id)
                queued.append(review_id)
            except RepoNotAllowed:
                continue
        return queued

    def _rerequested(self, ref: PrRef, pr, seen, mine: list[tuple[str, str]]):
        """(last finished review, who asked, when) if your review was requested again after the last
        review and the PR has new commits; otherwise None. A request with nothing new is used up."""
        asks = [(at, who) for at, who in mine if who.lower() not in self.cfg.triggers.block]
        last = max(asks, default=None)
        if not last or not last[0] or last[0] <= (seen["request_used"] or ""):
            return None
        asked = datetime.fromisoformat(last[0].replace("Z", "+00:00")).timestamp()
        latest = self.store.latest_review_for(ref.owner, ref.repo, ref.number, done_only=False)
        if latest is None or latest["created_at"] >= asked:  # answered already (even if that review failed)
            return None
        prev = self.store.latest_review_for(ref.owner, ref.repo, ref.number)
        if prev is None:
            return None
        if prev["head_sha"] == pr.head_sha:  # nothing new to look at: this request doesn't wait for a push
            self.store.set_watched(ref.owner, ref.repo, ref.number, request_used=last[0])
            return None
        return prev, last[1], last[0]

    # ---- background threads ---------------------------------------------------

    def start(self) -> None:
        threading.Thread(target=self._poll_loop, daemon=True, name="poller").start()
        threading.Thread(target=self._review_loop, daemon=True, name="auto-reviews").start()

    def _capped_today(self) -> int:
        return reviews_toward_cap(self.store, self.cfg.triggers)

    def stop(self) -> None:
        self._stop.set()

    def refresh_drafts(self) -> None:
        """Pick up reviews you submitted (or deleted) on GitHub."""
        from . import drafting
        from .drafts import DraftWriter

        writer = DraftWriter(self.cfg)
        for review_id in self.store.reviews_with_drafts():
            try:
                drafting.refresh_submitted(self.store, self.cfg, writer, review_id)
            except Exception:
                log.warning("couldn't refresh drafts of review %s", review_id, exc_info=True)

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            self.round()
            self._stop.wait(self.cfg.triggers.poll_minutes * 60)

    def round(self) -> None:
        """One polling round. Each step has its own error handling, so one failing step can't skip the rest."""
        try:
            self.poll_once()  # new PRs first; housekeeping can't delay them
            self.store.set_setting(LAST_ERROR, "")
        except Exception as e:
            log.warning("polling GitHub failed", exc_info=True)
            self.store.set_setting(LAST_ERROR, f"{type(e).__name__}: {e}"[:300])
        for step in (self.refresh_drafts, lambda: prstate.refresh(self.store, self.cfg, self.reader)):
            try:
                step()
            except Exception:
                log.warning("housekeeping step failed", exc_info=True)
        self.store.set_setting(LAST_POLL, str(time.time()))

    def _review_loop(self) -> None:
        """Automatic reviews run one at a time."""
        while not self._stop.is_set():
            try:
                review_id = self.queue.get(timeout=5)
            except queue.Empty:
                continue
            self.run(review_id)
