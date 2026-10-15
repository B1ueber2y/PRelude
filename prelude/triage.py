"""Acting on a finding's wording and status; shared by the web UI and the command line."""

from __future__ import annotations

from collections.abc import Callable

from . import drafting
from .config import Config
from .db import Store
from .drafts import DraftError, DraftWriter

READ_ONLY = {
    "posted": "This comment is already posted on GitHub; change it there.",
    "dropped": "This PR was closed before the comment was posted.",
    "superseded": "A later review of this PR replaced this comment.",
}


def refusal(finding) -> str | None:
    """Why the finding can't be changed any more, if it can't."""
    return READ_ONLY.get(finding["status"])


def set_wording(
    store: Store,
    cfg: Config,
    writer: DraftWriter,
    finding_id: int,
    text: str | None,
    learn: Callable[[int, str], object] | None = None,
) -> str | None:
    """Set the reviewer's wording (None = Claude's), learn from it, and sync any GitHub draft.

    Returns an error message if the wording couldn't be set or the GitHub draft couldn't be updated.
    """
    f = store.get_finding(finding_id)
    if why := refusal(f):
        return why
    text = text.strip() if text is not None else None
    user_draft = None if text is None or text == f["claude_comment"] else text
    # auto_drafted=0: the reviewer has looked. Conditional, so a supersede that just claimed it wins.
    unchanged = {} if user_draft != f["user_draft"] else {"acted_at": f["acted_at"], "action": f["action"]}
    if not store.update_finding_if(
        finding_id, {"status": f["status"]}, user_draft=user_draft, auto_drafted=0, **unchanged
    ):
        return refusal(store.get_finding(finding_id)) or "This comment just changed; reload."
    if learn and user_draft is not None and user_draft != f["user_draft"]:
        learn(finding_id, "edit")
    try:
        drafting.sync(store, cfg, writer, finding_id)
    except DraftError as e:
        return str(e)
    return None
