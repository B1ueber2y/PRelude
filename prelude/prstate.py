"""Keep reviews in step with their PR: merged or closed PRs, and comments left unposted."""

from __future__ import annotations

import logging

from . import drafting
from .config import Config
from .db import Store
from .drafts import DraftWriter
from .github import GitHubReader, PrRef
from .guard import RepoNotAllowed
from .retry import is_transient

log = logging.getLogger(__name__)


class StillOpen(Exception):
    pass


def state_of(pr) -> str:
    return "merged" if pr.merged else ("open" if pr.state == "open" else "closed")


def refresh(
    store: Store, cfg: Config, reader: GitHubReader, prs: list[tuple[str, str, int]] | None = None, limit: int = 20
) -> int:
    """Record each PR's current state (closed PRs can reopen; only merged is final). Returns changes.

    Checks at most `limit` PRs, least recently checked first, and stops at the first temporary
    GitHub failure so an outage doesn't hold up the rest of the round.
    """
    changed = 0
    for owner, repo, number in prs if prs is not None else store.prs_to_recheck(limit):
        try:
            state = state_of(reader.pull_request(PrRef(owner, repo, number)))
        except RepoNotAllowed:
            store.mark_pr_checked(owner, repo, number)
            continue
        except Exception as e:
            log.warning("couldn't read state of %s/%s#%s: %s", owner, repo, number, e)
            store.mark_pr_checked(owner, repo, number)  # so one bad PR can't stay first forever
            if is_transient(e):
                break
            continue
        changed += store.set_pr_state(owner, repo, number, state)
    return changed


def leftovers(findings) -> dict[str, int]:
    return {
        "pending": sum(1 for f in findings if f["status"] == "pending"),
        "drafted": sum(1 for f in findings if f["status"] == "drafted"),
    }


def clear_leftovers(store: Store, cfg: Config, writer: DraftWriter, reader: GitHubReader, review_id: int) -> int:
    """For a closed PR: remove unsubmitted draft comments and mark unhandled findings 'dropped'.

    Re-checks the PR first (it may have been reopened) and picks up drafts submitted meanwhile.
    Not a judgement on the comments, so nothing is learned from it.
    """
    r = store.get_review(review_id)
    state = state_of(reader.pull_request(PrRef(r["owner"], r["repo"], r["number"])))
    store.set_pr_state(r["owner"], r["repo"], r["number"], state)
    if state == "open":
        raise StillOpen("This PR is open again; nothing was cleared.")
    drafting.refresh_submitted(store, cfg, writer, review_id)
    n = 0
    for f in store.findings_for(review_id):
        if f["status"] == "drafted":
            drafting.remove(store, cfg, writer, f["id"], new_status="dropped")
            n += 1
        elif f["status"] == "pending":
            store.update_finding(f["id"], status="dropped")
            n += 1
    return n
