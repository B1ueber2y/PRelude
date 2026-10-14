"""Claude keeps the reviewer memory up to date: by chat, and by learning from review actions.

Changes apply immediately and are logged with the previous version, so each can be undone.
"""

from __future__ import annotations

import logging
import threading

from . import claude
from .config import Config
from .db import Store
from .memory import GLOBAL, STYLE, Memory, describe
from .prompts import (
    LEARN_SCHEMA,
    MEMORY_CHAT_SCHEMA,
    STYLE_SCHEMA,
    learn_prompt,
    memory_chat_prompt,
    style_from_edit_prompt,
    style_from_history_prompt,
)

log = logging.getLogger(__name__)
_write_lock = threading.Lock()


class MemoryChanged(Exception):
    pass


def chat(store: Store, cfg: Config, scope: str, text: str) -> None:
    text = text.strip()
    if not text:
        raise ValueError("empty message")
    memory = Memory(cfg)
    history = [m for m in store.memory_messages(scope, limit=12) if m["role"] in ("user", "assistant")]
    store.add_memory_message(scope, "user", text)
    try:
        claude.ensure_logged_in()
        transcript = "".join(f"\n{'Reviewer' if m['role'] == 'user' else 'You'}: {m['content']}" for m in history)
        prompt = memory_chat_prompt(
            what=describe(scope), document=memory.read(scope),
            history=f"\nEarlier in this conversation:{transcript}\n" if transcript else "",
            user_message=text,
        )
        out = _run(cfg, memory, prompt, MEMORY_CHAT_SCHEMA)
        new = out.get("updated_document")
        with _write_lock:
            prev = memory.read(scope)
            changed = new is not None and new.strip() != prev
            if changed:
                memory.write(scope, new)
        store.add_memory_message(
            scope, "assistant", out.get("reply") or "Done.",
            new_document=memory.read(scope) if changed else None, prev_document=prev if changed else None,
        )
    except Exception as e:
        store.add_memory_message(scope, "error", f"{type(e).__name__}: {e}"[:2000])


def undo(store: Store, cfg: Config, message_id: int) -> str:
    m = store.get_memory_message(message_id)
    if m is None or m["new_document"] is None or m["undone"]:
        raise KeyError(message_id)
    memory = Memory(cfg)
    with _write_lock:
        if memory.read(m["scope"]) != m["new_document"].strip():
            raise MemoryChanged("The memory has changed since; edit it by hand instead.")
        memory.write(m["scope"], m["prev_document"] or "")
    store.mark_memory_undone(message_id)
    return m["scope"]


def learn_async(store: Store, cfg: Config, finding_id: int, action: str) -> threading.Thread:
    """Edits teach comment style; discards and discussions teach review principles."""
    target = learn_style_from_edit if action == "edit" else learn_from_finding
    args = (store, cfg, finding_id) if action == "edit" else (store, cfg, finding_id, action)
    t = threading.Thread(target=target, args=args, daemon=True, name=f"learn-{finding_id}")
    t.start()
    return t


def learn_style_from_edit(store: Store, cfg: Config, finding_id: int) -> str | None:
    try:
        f = store.get_finding(finding_id)
        if f is None or not f["user_draft"] or f["user_draft"] == f["claude_comment"]:
            return None
        memory = Memory(cfg)
        claude.ensure_logged_in()
        out = _run(cfg, memory, style_from_edit_prompt(
            style=memory.read(STYLE), suggested=f["claude_comment"], edited=f["user_draft"],
        ), STYLE_SCHEMA, timeout=300)
        return _save(store, memory, STYLE, out.get("updated_style"), out.get("summary") or "", finding_id)
    except Exception:
        log.warning("learning style from finding %s failed", finding_id, exc_info=True)
        return None


def init_style(store: Store, cfg: Config, repo: str, comments: list[str]) -> str | None:
    """Write the style guide from the reviewer's past comments. Returns the summary."""
    memory = Memory(cfg)
    claude.ensure_logged_in()
    text = "\n\n".join(f"<comment>{c.strip()}</comment>" for c in comments if c.strip())
    out = _run(cfg, memory, style_from_history_prompt(repo=repo, comments=text), STYLE_SCHEMA)
    return _save(store, memory, STYLE, out.get("updated_style"), out.get("summary") or "", None,
                 note=f"Initialized from {len(comments)} past comments on {repo}")


def _save(store: Store, memory: Memory, scope: str, new: str | None, summary: str, finding_id: int | None,
          note: str | None = None) -> str | None:
    if not new or not new.strip():
        return None
    with _write_lock:
        prev = memory.read(scope)
        if new.strip() == prev:
            return None
        memory.write(scope, new)
        label = note or summary or "Updated"
        store.add_memory_message(
            scope, "learned", label, new_document=memory.read(scope), prev_document=prev, finding_id=finding_id,
        )
    return summary or label


def learn_from_finding(store: Store, cfg: Config, finding_id: int, action: str) -> str | None:
    """Record a lasting preference shown by how the reviewer handled a finding, if there is one."""
    try:
        f = store.get_finding(finding_id)
        r = store.get_review(f["review_id"]) if f else None
        if f is None or r is None:
            return None
        repo = f"{r['owner']}/{r['repo']}"
        msgs = [m for m in store.messages_for(finding_id) if m["role"] != "error"][-6:]
        discussion = (
            "Discussion:\n" + "\n".join(f"{'Reviewer' if m['role'] == 'user' else 'PRelude'}: {m['content']}" for m in msgs)
            if msgs else ""
        )
        memory = Memory(cfg)
        existing = "\n".join(filter(None, [memory.read(GLOBAL), memory.read(repo)]))
        claude.ensure_logged_in()
        out = _run(cfg, memory, learn_prompt(
            repo=repo, title=f["title"], path=f["path"], claude_comment=f["claude_comment"],
            outcome=_outcome(f, action), discussion=discussion, existing=existing,
        ), LEARN_SCHEMA, timeout=300)
        lesson = (out.get("lesson") or "").strip()
        if not lesson:
            return None
        scope = repo if out.get("scope") == "repo" else GLOBAL
        with _write_lock:
            prev = memory.read(scope)
            memory.remember(scope, lesson)
            store.add_memory_message(
                scope, "learned", lesson, new_document=memory.read(scope), prev_document=prev, finding_id=finding_id,
            )
        return lesson
    except Exception:
        log.warning("learning from finding %s failed", finding_id, exc_info=True)
        return None


def _outcome(f, action: str) -> str:
    if action == "discard":
        return "discarded the finding (decided not to raise it)"
    if action == "edit":
        return f"rewrote the comment as:\n<edited>\n{f['user_draft']}\n</edited>"
    return "discussed the finding (see below)"


def _run(cfg: Config, memory: Memory, prompt: str, schema: dict, timeout: int | None = None) -> dict:
    memory.dir.mkdir(parents=True, exist_ok=True)
    result = claude.run_json(
        prompt, memory.dir, schema=schema, model=cfg.claude.model,
        timeout=timeout or cfg.claude.discuss_timeout_seconds,
    )
    return result.structured if isinstance(result.structured, dict) else {}
