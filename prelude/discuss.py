"""Per-finding discussions: each forks the review session, then continues its own."""

from __future__ import annotations

import logging
import queue
import threading
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from . import claude, memory_agent, notify
from .config import Config
from .db import Store
from .drafts import DraftError, DraftWriter
from .memory import Memory
from .prompts import discuss_first_prompt, discuss_followup_prompt, parse_reply

log = logging.getLogger(__name__)


@dataclass
class Event:
    kind: str  # "text" | "activity" | "done" | "error"
    data: str = ""


class Broker:
    """Fan-out of live discussion events to any open SSE streams.

    Events of the reply in progress are buffered, so a stream that connects a
    moment after the reply started still receives all of it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: dict[int, list[queue.Queue[Event]]] = defaultdict(list)
        self._buffer: dict[int, list[Event]] = defaultdict(list)

    def reset(self, finding_id: int) -> None:
        with self._lock:
            self._buffer[finding_id] = []

    def subscribe(self, finding_id: int) -> queue.Queue[Event]:
        q: queue.Queue[Event] = queue.Queue()
        with self._lock:
            for ev in self._buffer.get(finding_id, []):
                q.put(ev)
            self._subs[finding_id].append(q)
        return q

    def unsubscribe(self, finding_id: int, q: queue.Queue[Event]) -> None:
        with self._lock:
            if q in self._subs.get(finding_id, []):
                self._subs[finding_id].remove(q)

    def publish(self, finding_id: int, event: Event) -> None:
        with self._lock:
            self._buffer[finding_id].append(event)
            subs = list(self._subs.get(finding_id, []))
        for q in subs:
            q.put(event)


class DiscussionBusy(Exception):
    pass


def current_draft(finding) -> str:
    return finding["user_draft"] if finding["user_draft"] is not None else finding["claude_comment"]


def post_message(
    store: Store,
    cfg: Config,
    broker: Broker,
    finding_id: int,
    text: str,
    *,
    ping: bool = True,
    writer: DraftWriter | None = None,
) -> threading.Thread:
    """Store the reviewer's message and start Claude's reply in the background (`ping`: notify when ready)."""
    text = text.strip()
    if not text:
        raise ValueError("empty message")
    finding = store.get_finding(finding_id)
    if finding is None:
        raise KeyError(finding_id)
    review = store.get_review(finding["review_id"])
    if review is None or not review["claude_session_id"] or not review["worktree"]:
        raise ValueError("this review has no Claude session to continue")
    if not store.try_lock_thread(finding_id):
        raise DiscussionBusy("Claude is still replying to the previous message")

    store.add_message(finding_id, "user", text)
    broker.reset(finding_id)
    thread = store.thread(finding_id)
    own_session = thread["session_id"] if thread else None

    if own_session:
        prompt = discuss_followup_prompt(current_draft=current_draft(finding), user_message=text)
        resume, fork = own_session, False
    else:
        prompt = discuss_first_prompt(
            ordinal=finding["ordinal"],
            title=finding["title"],
            path=finding["path"],
            line=finding["line"],
            assessment=finding["assessment"],
            claude_comment=finding["claude_comment"],
            current_draft=current_draft(finding),
            user_message=text,
            memory=Memory(cfg).prompt_section(review["owner"], review["repo"], store.recent_edits()),
        )
        resume, fork = review["claude_session_id"], True

    t = threading.Thread(
        target=_reply,
        args=(store, cfg, broker, finding_id, prompt, Path(review["worktree"]), resume, fork, ping, writer),
        daemon=True,
        name=f"discuss-{finding_id}",
    )
    t.start()
    return t


def _withdraw(store: Store, cfg: Config, writer: DraftWriter, finding_id: int) -> None:
    """Claude agrees the comment shouldn't be made: discard it, and take it out of the GitHub draft."""
    from . import drafting  # drafting imports this module

    if store.get_finding(finding_id)["status"] not in ("pending", "drafted"):
        return
    try:
        drafting.remove(store, cfg, writer, finding_id, new_status="discarded")
    except DraftError as e:
        store.add_message(finding_id, "error", f"Couldn't remove the GitHub draft comment: {e}"[:2000])


def _reply(
    store: Store,
    cfg: Config,
    broker: Broker,
    finding_id: int,
    prompt: str,
    worktree: Path,
    resume: str,
    fork: bool,
    ping: bool = True,
    writer: DraftWriter | None = None,
) -> None:
    session_id = None
    changed_mind = False
    final = Event("done")
    try:
        claude.ensure_logged_in(claude.auth_for(cfg))
        result = claude.run_stream(
            prompt,
            worktree,
            on_text=lambda s: broker.publish(finding_id, Event("text", s)),
            on_activity=lambda name: broker.publish(finding_id, Event("activity", name)),
            model=cfg.claude.model,
            resume=resume,
            fork=fork,
            timeout=cfg.claude.discuss_timeout_seconds,
            auth=claude.auth_for(cfg),
        )
        session_id = result.session_id
        text, proposal, verdict = parse_reply(result.text)
        store.add_message(finding_id, "assistant", text, proposal=proposal, verdict=verdict, session_id=session_id)
        if verdict:
            store.update_finding(finding_id, verdict=verdict)
        if verdict == "withdrawn":
            _withdraw(store, cfg, writer or DraftWriter(cfg), finding_id)
        changed_mind = verdict in ("revised", "withdrawn")
    except Exception as e:
        log.exception("discussion reply for finding %s failed", finding_id)
        store.add_message(finding_id, "error", f"{type(e).__name__}: {e}"[:2000])
        final = Event("error", str(e)[:500])
    finally:
        store.release_thread(finding_id, session_id)
        broker.publish(finding_id, final)
    finding = store.get_finding(finding_id)
    review = store.get_review(finding["review_id"]) if finding else None
    if ping and review:  # you may have left: tell you it's ready
        pr = (review["owner"], review["repo"], review["number"])
        notify.reply_ready(cfg.notify, pr, review["title"], final.kind == "done", f"/findings/{finding_id}")
    if changed_mind:
        # The reviewer changed Claude's mind: maybe a lasting preference.
        memory_agent.learn_from_finding(store, cfg, finding_id, "discuss")
