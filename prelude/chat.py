"""A PR's discussion space: threads with Claude, each forking the latest review's session, then
continuing its own. Threads reply in parallel; each one takes one message at a time."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from . import claude, notify
from .config import Config
from .db import Store
from .discuss import Broker, DiscussionBusy, Event
from .memory import Memory
from .prompts import chat_first_prompt, chat_followup_prompt

log = logging.getLogger(__name__)


def source_review(store: Store, owner: str, repo: str, number: int):
    """The newest finished review whose session and checkout a new thread can start from."""
    r = store.latest_review_for(owner, repo, number)
    return r if r and r["claude_session_id"] and r["worktree"] else None


def start_thread(store: Store, cfg: Config, broker: Broker, owner: str, repo: str, number: int, text: str) -> int:
    if not text.strip():
        raise ValueError("empty message")
    review = source_review(store, owner, repo, number)
    if review is None:
        raise ValueError("This PR has no finished review to talk about yet.")
    chat_id = store.create_chat(review)
    post_message(store, cfg, broker, chat_id, text)
    return chat_id


def post_message(store: Store, cfg: Config, broker: Broker, chat_id: int, text: str) -> threading.Thread:
    """Store the reviewer's message and start Claude's reply in the background."""
    text = text.strip()
    if not text:
        raise ValueError("empty message")
    chat = store.get_chat(chat_id)
    if chat is None:
        raise KeyError(chat_id)
    if not store.try_lock_chat(chat_id):
        raise DiscussionBusy("Claude is still replying to the previous message")
    try:
        earlier = [m["content"] for m in store.chat_messages(chat_id) if m["role"] == "user"]
        store.add_chat_message(chat_id, "user", text)
        store.update_chat(chat_id, seen_at=time.time())
        broker.reset(chat_id)
        if chat["session_id"]:
            prompt, resume, fork = chat_followup_prompt(user_message=text), chat["session_id"], False
        else:  # the first message, or no reply has come through yet: start from the review again
            latest = store.latest_review_for(chat["owner"], chat["repo"], chat["number"], done_only=False)
            prompt = chat_first_prompt(
                user_message=text,
                earlier=earlier,
                newer_commits=bool(latest and latest["head_sha"] and latest["head_sha"] != chat["head_sha"]),
                memory=Memory(cfg).prompt_section(chat["owner"], chat["repo"], store.recent_edits()),
            )
            resume, fork = chat["base_session_id"], True
        t = threading.Thread(
            target=_reply,
            args=(store, cfg, broker, chat_id, prompt, Path(chat["worktree"]), resume, fork),
            daemon=True,
            name=f"chat-{chat_id}",
        )
        t.start()
    except BaseException:
        store.release_chat(chat_id)
        raise
    return t


def _reply(
    store: Store, cfg: Config, broker: Broker, chat_id: int, prompt: str, worktree: Path, resume: str, fork: bool
) -> None:
    final, ok, session_id = Event("done"), False, None
    try:
        auth = claude.auth_for(cfg)
        claude.ensure_logged_in(auth)
        result = claude.run_stream(
            prompt,
            worktree,
            on_text=lambda s: broker.publish(chat_id, Event("text", s)),
            on_activity=lambda name: broker.publish(chat_id, Event("activity", name)),
            model=cfg.claude.model,
            resume=resume,
            fork=fork,
            timeout=cfg.claude.discuss_timeout_seconds,
            auth=auth,
        )
        store.add_chat_message(chat_id, "assistant", result.text.strip())
        session_id, ok = result.session_id, True
    except Exception as e:
        log.exception("reply in discussion thread %s failed", chat_id)
        final = Event("error", str(e)[:500])
        try:
            store.add_chat_message(chat_id, "error", f"{type(e).__name__}: {e}"[:2000])
        except Exception:
            log.exception("couldn't record the failure")
    finally:
        store.release_chat(chat_id, session_id)
        broker.publish(chat_id, final)
    chat = store.get_chat(chat_id)
    if chat:  # you may have left: tell you it's ready
        review = store.get_review(chat["review_id"]) if chat["review_id"] else None
        pr = (chat["owner"], chat["repo"], chat["number"])
        notify.reply_ready(cfg.notify, pr, review["title"] if review else None, ok, f"/chats/{chat_id}")
