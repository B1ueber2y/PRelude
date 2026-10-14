"""Moving accepted findings into the reviewer's pending GitHub review, and keeping them in sync."""

from __future__ import annotations

from .config import Config
from .db import Store
from .discuss import current_draft
from .drafts import DraftError, DraftWriter, NotPending

PREFIX_SETTING = "draft_prefix"


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
