"""Moving accepted findings into the reviewer's pending GitHub review, and keeping them in sync."""

from __future__ import annotations

import logging

from .config import Config
from .db import Store
from .discuss import current_draft
from .drafts import DraftError, DraftWriter, NotPending

PREFIX_SETTING = "draft_prefix"

log = logging.getLogger(__name__)


class DraftsDisabled(DraftError):
    pass


class EditedOnGitHub(DraftError):
    pass


def prefix_on(store: Store) -> bool:
    return store.setting(PREFIX_SETTING, "1") == "1"


def body_for(store: Store, cfg: Config, finding) -> str:
    text = current_draft(finding).strip()
    if prefix_on(store) and cfg.drafts.prefix:
        sep = "\n\n" if text.startswith("```") else " "
        return f"{cfg.drafts.prefix}{sep}{text}"
    return text


def _review(store: Store, finding):
    return store.get_review(finding["review_id"])


def add(store: Store, cfg: Config, writer: DraftWriter, finding_id: int) -> None:
    if not cfg.drafts.enabled:
        raise DraftsDisabled("Adding to GitHub drafts is turned off (github_drafts.enabled in config.yaml).")
    f = store.get_finding(finding_id)
    if f["status"] == "drafted":
        return
    r = _review(store, f)
    review_id = writer.pending_review(r["owner"], r["repo"], r["number"], r["head_sha"])
    body = body_for(store, cfg, f)
    comment_id = writer.add_comment(
        r["owner"],
        r["repo"],
        review_id,
        path=f["path"],
        body=body,
        line=f["line"],
        side=f["side"],
        start_line=f["start_line"],
        file_level=not f["in_diff"],
    )
    store.update_finding(
        finding_id,
        status="drafted",
        github_review_id=review_id,
        github_comment_id=comment_id,
        drafted_body=body,
    )


def sync(store: Store, cfg: Config, writer: DraftWriter, finding_id: int) -> None:
    """Push a changed draft to GitHub, unless it was edited there in the meantime."""
    f = store.get_finding(finding_id)
    if f["status"] != "drafted":
        return
    body = body_for(store, cfg, f)
    if body == f["drafted_body"]:
        return
    r = _review(store, f)
    on_github = writer.comment_body(r["owner"], r["repo"], f["github_comment_id"])
    if on_github is not None and on_github.strip() != (f["drafted_body"] or "").strip():
        raise EditedOnGitHub("This comment was edited on GitHub; PRelude won't overwrite it.")
    writer.update_comment(r["owner"], r["repo"], f["github_review_id"], f["github_comment_id"], body)
    store.update_finding(finding_id, drafted_body=body)


def remove(store: Store, cfg: Config, writer: DraftWriter, finding_id: int, new_status: str = "pending") -> None:
    f = store.get_finding(finding_id)
    if f["status"] == "drafted":
        r = _review(store, f)
        writer.delete_comment(r["owner"], r["repo"], f["github_review_id"], f["github_comment_id"])
    store.update_finding(finding_id, status=new_status, github_comment_id=None, drafted_body=None)


def add_all(store: Store, cfg: Config, writer: DraftWriter, review_id: int) -> tuple[int, list[str]]:
    added, failed = 0, []
    for f in store.findings_for(review_id):
        if f["status"] != "pending":
            continue
        try:
            add(store, cfg, writer, f["id"])
            added += 1
        except (DraftsDisabled, NotPending):
            raise
        except DraftError as e:
            failed.append(f"{f['title']}: {e}")
    return added, failed


def auto_add(store: Store, cfg: Config, writer: DraftWriter, finding_ids: list[int]) -> int:
    """Put new findings straight into the pending review (github_drafts.auto). Failures leave them to review."""
    if not (cfg.drafts.enabled and cfg.drafts.auto):
        return 0
    added = 0
    for finding_id in finding_ids:
        try:
            add(store, cfg, writer, finding_id)
            added += 1
        except Exception:  # the rest would likely fail the same way
            log.warning("couldn't add finding %s to the draft", finding_id, exc_info=True)
            break
    return added


def refresh_submitted(store: Store, cfg: Config, writer: DraftWriter, review_id: int) -> int:
    """Notice when the reviewer submitted (or deleted) their pending review on GitHub.

    Submitted: drafted findings become 'posted'. Deleted: they go back to 'pending'. Returns changes.
    """
    drafted = [f for f in store.findings_for(review_id) if f["status"] == "drafted" and f["github_review_id"]]
    if not drafted:
        return 0
    r = store.get_review(review_id)
    changed = 0
    for gh_review in {f["github_review_id"] for f in drafted}:
        state = writer.review_state(r["owner"], r["repo"], gh_review)
        if state == "PENDING":
            continue
        new = "pending" if state is None else "posted"
        for f in drafted:
            if f["github_review_id"] == gh_review:
                fields = {"status": new}
                if new == "pending":
                    fields.update(github_review_id=None, github_comment_id=None, drafted_body=None)
                store.update_finding(f["id"], **fields)
                changed += 1
    return changed
