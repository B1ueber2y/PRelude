"""Keep reviews in step with their PR: merged or closed PRs, and comments left unposted."""

from __future__ import annotations

import logging

from . import drafting
from .config import Config
from .db import Store
from .drafts import DraftWriter
from .github import GitHubReader, PrRef

log = logging.getLogger(__name__)


def state_of(pr) -> str:
    return "merged" if pr.merged else ("open" if pr.state == "open" else "closed")


def refresh(store: Store, cfg: Config, reader: GitHubReader, prs: list[tuple[str, str, int]] | None = None) -> int:
    """Record merged/closed PRs. Returns how many changed."""
    changed = 0
    for owner, repo, number in prs if prs is not None else store.open_reviewed_prs():
        try:
            state = state_of(reader.pull_request(PrRef(owner, repo, number)))
        except Exception:
            log.warning("couldn't read state of %s/%s#%s", owner, repo, number, exc_info=True)
            continue
        if state != "open":
            store.set_pr_state(owner, repo, number, state)
            changed += 1
    return changed


def leftovers(findings) -> dict[str, int]:
    return {
        "pending": sum(1 for f in findings if f["status"] == "pending"),
        "drafted": sum(1 for f in findings if f["status"] == "drafted"),
    }


def clear_leftovers(store: Store, cfg: Config, writer: DraftWriter, review_id: int) -> int:
    """For a closed PR: remove unsubmitted draft comments and mark unhandled findings 'dropped'.

    Not a judgement on the comments, so nothing is learned from it.
    """
    n = 0
    for f in store.findings_for(review_id):
        if f["status"] == "drafted":
            drafting.remove(store, cfg, writer, f["id"], new_status="dropped")
            n += 1
        elif f["status"] == "pending":
            store.update_finding(f["id"], status="dropped")
            n += 1
    return n
