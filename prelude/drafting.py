"""Moving accepted findings into the reviewer's pending GitHub review, and keeping them in sync."""

from __future__ import annotations

import logging

from . import notify
from .config import Config
from .db import Store
from .discuss import current_draft
from .drafts import DraftError, DraftWriter, NotPending, PostUncertain

PREFIX_SETTING = "draft_prefix"

log = logging.getLogger(__name__)


class DraftsDisabled(DraftError):
    pass


class EditedOnGitHub(DraftError):
    pass


def prefix_on(store: Store) -> bool:
    return store.setting(PREFIX_SETTING, "1") == "1"


def _prefixed(prefix: str, text: str) -> str:
    text = text.strip()
    if not prefix:
        return text
    return f"{prefix}{chr(10) * 2 if text.startswith('```') else ' '}{text}"


def body_for(store: Store, cfg: Config, finding) -> str:
    return _prefixed(cfg.drafts.prefix if prefix_on(store) else "", current_draft(finding))


def _review(store: Store, finding):
    return store.get_review(finding["review_id"])


def add(store: Store, cfg: Config, writer: DraftWriter, finding_id: int, *, auto: bool = False) -> None:
    if not cfg.drafts.enabled:
        raise DraftsDisabled("Adding to GitHub drafts is turned off (github_drafts.enabled in config.yaml).")
    f = store.get_finding(finding_id)
    if f["status"] == "drafted":
        if f["auto_drafted"] and not auto:  # the reviewer chose to keep an auto-draft
            store.update_finding_if(finding_id, {"status": "drafted", "auto_drafted": 1}, auto_drafted=0)
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
    claimed = store.update_finding_if(
        finding_id,
        {"status": f["status"], "user_draft": f["user_draft"]},
        status="drafted",
        github_review_id=review_id,
        github_comment_id=comment_id,
        drafted_body=body,
        auto_drafted=int(auto),
    )
    if not claimed:  # discarded, reworded or added elsewhere while GitHub was busy: undo this copy
        writer.delete_comment(r["owner"], r["repo"], review_id, comment_id)


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
    store.update_finding(finding_id, status=new_status, github_comment_id=None, drafted_body=None, auto_drafted=0)


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


def auto_post(store: Store, cfg: Config, writer: DraftWriter, review_id: int, finding_ids: list[int]) -> list[int]:
    """Post an automatic review's findings on the PR right away (triggers.auto_post). Returns those handled.

    Only line comments of the chosen severities; the rest, or all of them if GitHub refuses, go on as usual.
    """
    r = store.get_review(review_id)
    t = cfg.triggers
    if not (
        cfg.drafts.enabled and t.posts(f"{r['owner']}/{r['repo']}") and (r["trigger_reason"] or "").startswith("auto:")
    ):
        return []
    picked = [
        f
        for f in map(store.get_finding, finding_ids)
        if f["status"] == "pending" and f["in_diff"] and f["severity"] in t.auto_post_severities
    ]
    if not picked:
        return []
    prefix = t.auto_post_prefix
    threads = []
    for f in picked:
        thread = {
            "path": f["path"],
            "body": _prefixed(prefix, current_draft(f)),
            "line": f["line"],
            "side": f["side"],
        }
        if f["start_line"]:
            thread.update(startLine=f["start_line"], startSide=f["side"])
        threads.append(thread)
    try:
        gh_review, comment_ids = writer.post_review(
            r["owner"], r["repo"], r["number"], r["head_sha"], prefix.rstrip(": ") + ".", threads
        )
    except PostUncertain:  # may be on the PR already: leave them to review, not in a draft too
        log.warning("posting review %s automatically may have failed", review_id, exc_info=True)
        notify.send(
            cfg.notify,
            "Check the PR",
            notify.pr_prefix(cfg.notify, f"{r['owner']}/{r['repo']}#{r['number']}", r["title"])
            + "posting the automatic review may have failed; its comments wait in PRelude",
            path=f"/reviews/{review_id}",
            tags="warning",
            link=r["url"],
        )
        return [f["id"] for f in picked]
    except Exception:
        log.warning("couldn't post review %s automatically; its findings go on as usual", review_id, exc_info=True)
        return []
    for f, thread, comment_id in zip(picked, threads, comment_ids, strict=True):
        store.update_finding(
            f["id"],
            status="posted",
            github_review_id=gh_review,
            github_comment_id=comment_id,
            drafted_body=thread["body"],
            auto_drafted=1,  # posted without the reviewer's judgement
        )
    return [f["id"] for f in picked]


def auto_add(store: Store, cfg: Config, writer: DraftWriter, finding_ids: list[int]) -> int:
    """Put new findings straight into the pending review (github_drafts.auto). Failures leave them to review.

    They count as not handled until the reviewer edits them or submits the review.
    """
    if not (cfg.drafts.enabled and cfg.drafts.auto):
        return 0
    added = 0
    for finding_id in finding_ids:
        if store.get_finding(finding_id)["status"] != "pending":  # handled in the UI meanwhile
            continue
        try:
            add(store, cfg, writer, finding_id, auto=True)
            added += 1
        except (DraftsDisabled, NotPending):
            break
        except DraftError:  # e.g. a line GitHub won't take; the others may still work
            log.warning("couldn't add finding %s to the draft", finding_id, exc_info=True)
        except Exception:  # GitHub unreachable: the rest would fail the same way
            log.warning("couldn't add finding %s to the draft", finding_id, exc_info=True)
            break
    return added


def supersede(store: Store, cfg: Config, writer: DraftWriter, review_id: int) -> int:
    """Take earlier reviews' untouched auto-drafts of this PR out of the draft; this review replaces them."""
    r = store.get_review(review_id)
    done = 0
    for f in store.drafts_on_pr(r["owner"], r["repo"], r["number"], besides=review_id):
        # Claim it first, so an edit or discard made meanwhile wins (the UI refuses superseded findings).
        if not store.update_finding_if(f["id"], {"status": "drafted", "auto_drafted": 1}, status="superseded"):
            continue
        try:
            writer.delete_comment(r["owner"], r["repo"], f["github_review_id"], f["github_comment_id"])
        except Exception as e:
            # Undone: it's the same draft as before, not a new one.
            store.update_finding_if(
                f["id"], {"status": "superseded"}, status="drafted", acted_at=f["acted_at"], action=f["action"]
            )
            if not isinstance(e, NotPending):  # NotPending: submitted on GitHub; refresh_submitted will notice
                log.warning("couldn't take finding %s out of the draft", f["id"], exc_info=True)
            continue  # other drafts may sit in another, still pending review
        store.update_finding(f["id"], github_comment_id=None, drafted_body=None, auto_drafted=0)
        done += 1
    return done


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
                fields = {"status": new, "auto_drafted": 0}
                if new == "pending":
                    fields.update(github_review_id=None, github_comment_id=None, drafted_body=None)
                store.update_finding(f["id"], **fields)
                changed += 1
    return changed
