"""SQLite storage. `claude_comment` is Claude's text; `user_draft` is only written by the reviewer."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY,
  owner TEXT NOT NULL,
  repo TEXT NOT NULL,
  number INTEGER NOT NULL,
  url TEXT NOT NULL,
  title TEXT,
  body TEXT,                               -- the author's PR description
  author TEXT,
  base_ref TEXT,
  base_sha TEXT,
  head_sha TEXT,
  merge_base TEXT,
  status TEXT NOT NULL DEFAULT 'queued',   -- queued | running | done | failed | skipped
  progress TEXT,
  error TEXT,
  error_detail TEXT,                       -- full trace for the "Technical details" view
  pr_state TEXT,                           -- open | merged | closed (last seen on GitHub)
  pr_checked_at REAL,
  pr_comments TEXT,                        -- JSON: the PR conversation the review saw
  comment_status TEXT,                     -- JSON: per existing comment, addressed / open / unclear
  trigger_reason TEXT,
  summary TEXT,
  claude_session_id TEXT,
  worktree TEXT,
  diff TEXT,
  cost_usd REAL,
  reviewers INTEGER NOT NULL DEFAULT 1,     -- 0 until resolved from PR size
  changed_lines INTEGER,
  runner_pid INTEGER,                      -- process running the review
  previous_review_id INTEGER,              -- set on re-reviews
  mode TEXT NOT NULL DEFAULT 'full',       -- full | changes (re-review of new commits only)
  reports TEXT,                            -- independent reviewers' raw reports (JSON)
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS findings (
  id INTEGER PRIMARY KEY,
  review_id INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  path TEXT NOT NULL,
  line INTEGER,
  start_line INTEGER,
  side TEXT NOT NULL DEFAULT 'RIGHT',
  in_diff INTEGER NOT NULL DEFAULT 0,
  severity TEXT NOT NULL,
  category TEXT,
  title TEXT NOT NULL,
  assessment TEXT NOT NULL,
  evidence TEXT NOT NULL DEFAULT '[]',
  confidence TEXT,
  claude_comment TEXT NOT NULL,
  user_draft TEXT,
  status TEXT NOT NULL DEFAULT 'pending',  -- pending | drafted | posted | discarded | dropped (PR closed first) | superseded (by a later review)
  verdict TEXT,                            -- latest Claude verdict from discussion
  github_review_id TEXT,                   -- pending review the comment was added to
  github_comment_id TEXT,
  drafted_body TEXT,                       -- body as last written to GitHub
  found_by INTEGER,                        -- reviewers who raised it (multi-reviewer runs)
  added_later INTEGER NOT NULL DEFAULT 0,  -- found by reviewers added after the first run
  acted_at REAL,                           -- last change to its status or wording
  action TEXT,                             -- what that change was: drafted, discarded, edited, ...
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS findings_by_review ON findings(review_id);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,
  finding_id INTEGER NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
  role TEXT NOT NULL,                      -- user | assistant | error
  content TEXT NOT NULL,
  proposal TEXT,                           -- suggested comment wording, if any
  verdict TEXT,                            -- confirmed | revised | withdrawn
  session_id TEXT,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_messages (
  id INTEGER PRIMARY KEY,
  scope TEXT NOT NULL,                     -- "global" or "OWNER/REPO"
  role TEXT NOT NULL,                      -- user | assistant | learned | error
  content TEXT NOT NULL,
  new_document TEXT,                       -- document after this change, if it changed it
  prev_document TEXT,                      -- document before, for undo
  finding_id INTEGER,                      -- source of a learned change
  undone INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS watched (
  owner TEXT NOT NULL,
  repo TEXT NOT NULL,
  number INTEGER NOT NULL,
  head_sha TEXT,                           -- last head seen
  updated_at TEXT,                         -- PR's updated_at when last evaluated
  reviewed_sha TEXT,                       -- head of the last automatic review
  request_used TEXT,                       -- latest re-request already answered (ISO time)
  decision TEXT,                           -- review | skip
  reason TEXT,
  checked_at REAL,
  PRIMARY KEY (owner, repo, number)
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS threads (
  finding_id INTEGER PRIMARY KEY REFERENCES findings(id) ON DELETE CASCADE,
  session_id TEXT,
  busy INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS chats (         -- threads in a PR's discussion space
  id INTEGER PRIMARY KEY,
  owner TEXT NOT NULL,
  repo TEXT NOT NULL,
  number INTEGER NOT NULL,
  review_id INTEGER,                       -- the review it started from (kept if that review is deleted)
  head_sha TEXT,                           -- the commit Claude sees
  worktree TEXT,
  base_session_id TEXT,                    -- the review's session, forked on the first reply
  session_id TEXT,                         -- the thread's own session after that
  busy INTEGER NOT NULL DEFAULT 0,
  seen_at REAL NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS chats_by_pr ON chats(owner, repo, number);
CREATE INDEX IF NOT EXISTS messages_by_finding ON messages(finding_id);

CREATE TABLE IF NOT EXISTS chat_messages (
  id INTEGER PRIMARY KEY,
  chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
  role TEXT NOT NULL,                      -- user | assistant | error
  content TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS chat_messages_by_chat ON chat_messages(chat_id);
"""


_NEWEST_PER_PR = "SELECT MAX(id) FROM reviews GROUP BY owner, repo, number"

# A reply in a finding's discussion that came after you last looked at it.
_UNREAD_FINDING_REPLY = (
    "SELECT 1 FROM messages WHERE finding_id = {finding} AND role != 'user'"
    " AND created_at > COALESCE((SELECT seen_at FROM threads WHERE finding_id = {finding}), 0)"
)


class Store:
    def __init__(self, path: Path):
        self.path = path
        self._local = threading.local()
        with sqlite3.connect(path, timeout=30) as c:
            c.execute("PRAGMA journal_mode=WAL")  # persistent; set once, not on every connection
        with self.connect() as c:
            c.executescript(SCHEMA)
            _add_missing_columns(
                c,
                "findings",
                {
                    "github_review_id": "TEXT",
                    "github_comment_id": "TEXT",
                    "drafted_body": "TEXT",
                    "found_by": "INTEGER",
                    "added_later": "INTEGER NOT NULL DEFAULT 0",
                    "auto_drafted": "INTEGER NOT NULL DEFAULT 0",
                    "acted_at": "REAL",
                    "action": "TEXT",
                },
            )
            _add_missing_columns(c, "watched", {"updated_at": "TEXT", "request_used": "TEXT"})
            if "seen_at" not in {r[1] for r in c.execute("PRAGMA table_info(threads)")}:
                _add_missing_columns(c, "threads", {"seen_at": "REAL NOT NULL DEFAULT 0"})
                c.execute("UPDATE threads SET seen_at = ?", (time.time(),))  # older replies count as seen
            _add_missing_columns(
                c,
                "reviews",
                {
                    "reviewers": "INTEGER NOT NULL DEFAULT 1",
                    "reports": "TEXT",
                    "changed_lines": "INTEGER",
                    "runner_pid": "INTEGER",
                    "previous_review_id": "INTEGER",
                    "finished_at": "REAL",  # last time it finished: the review, or reviewers added later
                    "body": "TEXT",
                    "mode": "TEXT NOT NULL DEFAULT 'full'",
                    "error_detail": "TEXT",
                    "pr_state": "TEXT",
                    "pr_checked_at": "REAL",
                    "pr_comments": "TEXT",
                    "comment_status": "TEXT",
                    "effort": "TEXT",  # chosen for this review; NULL: claude.effort
                },
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """This thread's connection; commits on success, rolls back on error.

        Connections are kept open per thread: opening and closing one per operation from several
        threads made SQLite's WAL handling block threads for up to the busy timeout.
        """
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    # ---- settings ----------------------------------------------------------

    def setting(self, key: str, default: str) -> str:
        with self.connect() as c:
            row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as c:
            c.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?",
                (key, value, value),
            )

    # ---- watched PRs (automatic triggers) ----------------------------------

    def watched(self, owner: str, repo: str, number: int) -> sqlite3.Row | None:
        with self.connect() as c:
            return c.execute(
                "SELECT * FROM watched WHERE owner = ? AND repo = ? AND number = ?", (owner, repo, number)
            ).fetchone()

    def set_watched(self, owner: str, repo: str, number: int, **fields: Any) -> None:
        fields["checked_at"] = time.time()
        with self.connect() as c:
            c.execute("INSERT OR IGNORE INTO watched (owner, repo, number) VALUES (?, ?, ?)", (owner, repo, number))
            cols = ", ".join(f"{k} = ?" for k in fields)
            c.execute(
                f"UPDATE watched SET {cols} WHERE owner = ? AND repo = ? AND number = ?",
                (*fields.values(), owner, repo, number),
            )

    def auto_reviews_since(self, since: float) -> list[tuple[str, str, float]]:
        """(OWNER/REPO, reason, started) of each automatic review started since then."""
        with self.connect() as c:
            return [
                (f"{o}/{r}", reason.removeprefix("auto: "), started)
                for o, r, reason, started in c.execute(
                    "SELECT owner, repo, trigger_reason, created_at FROM reviews WHERE trigger_reason LIKE 'auto:%'"
                    " AND status != 'skipped' AND created_at >= ?",
                    (since,),
                )
            ]

    # ---- reviews -----------------------------------------------------------

    def create_review(
        self,
        owner: str,
        repo: str,
        number: int,
        url: str,
        trigger_reason: str,
        reviewers: int = 1,
        previous_review_id: int | None = None,
        mode: str = "full",
        effort: str | None = None,
    ) -> int:
        now = time.time()
        with self.connect() as c:
            cur = c.execute(
                "INSERT INTO reviews (owner, repo, number, url, trigger_reason, reviewers, previous_review_id, mode,"
                " effort, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
                (owner, repo, number, url, trigger_reason, reviewers, previous_review_id, mode, effort, now, now),
            )
            return int(cur.lastrowid)

    def update_review(self, review_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.connect() as c:
            c.execute(f"UPDATE reviews SET {cols} WHERE id = ?", (*fields.values(), review_id))

    def get_review(self, review_id: int) -> sqlite3.Row | None:
        with self.connect() as c:
            return c.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()

    def try_mark_busy(self, review_id: int, progress: str) -> bool:
        """Claim a finished review for extra work; False if it's already busy (atomic)."""
        with self.connect() as c:
            cur = c.execute(
                "UPDATE reviews SET progress = ?, error = NULL, runner_pid = ?, updated_at = ?"
                " WHERE id = ? AND status = 'done' AND progress IS NULL",
                (progress, os.getpid(), time.time(), review_id),
            )
            return cur.rowcount == 1

    def delete_review(self, review_id: int) -> None:
        """Remove a review and its findings and discussions, and the PR's threads with its last review.

        GitHub is not touched."""
        with self.connect() as c:
            c.execute("UPDATE reviews SET previous_review_id = NULL WHERE previous_review_id = ?", (review_id,))
            pr = c.execute("SELECT owner, repo, number FROM reviews WHERE id = ?", (review_id,)).fetchone()
            c.execute("DELETE FROM reviews WHERE id = ?", (review_id,))
            if (
                pr
                and not c.execute(
                    "SELECT 1 FROM reviews WHERE owner = ? AND repo = ? AND number = ?", tuple(pr)
                ).fetchone()
            ):  # the PR's last review: nothing links to its discussion space any more
                c.execute("DELETE FROM chats WHERE owner = ? AND repo = ? AND number = ?", tuple(pr))

    def reviews_of_pr(self, owner: str, repo: str, number: int) -> int:
        with self.connect() as c:
            q = "SELECT COUNT(*) FROM reviews WHERE owner = ? AND repo = ? AND number = ?"
            return c.execute(q, (owner, repo, number)).fetchone()[0]

    def worktrees_in_use(self) -> set[str]:
        with self.connect() as c:
            return {
                r[0]
                for r in c.execute(
                    "SELECT worktree FROM reviews WHERE worktree IS NOT NULL"
                    " UNION SELECT worktree FROM chats WHERE worktree IS NOT NULL"
                )
            }

    def latest_review_for(self, owner: str, repo: str, number: int, done_only: bool = True) -> sqlite3.Row | None:
        with self.connect() as c:
            return c.execute(
                "SELECT * FROM reviews WHERE owner = ? AND repo = ? AND number = ?"
                + (" AND status = 'done'" if done_only else "")
                + " ORDER BY id DESC LIMIT 1",
                (owner, repo, number),
            ).fetchone()

    def list_reviews(self, limit: int = 50, *, latest_only: bool = False) -> list[dict[str, Any]]:
        """Open PRs before closed ones, then latest activity first: a review starting or finishing,
        a comment changing, or a discussion message. Each row gets `activity_at` and `activity`
        (what happened). Each review has its own activity; with `latest_only`, which keeps each PR's
        newest review, that one has the whole PR's.
        """
        where = f"WHERE r.id IN ({_NEWEST_PER_PR})" if latest_only else ""
        with self.connect() as c:
            rows = [
                dict(r)
                for r in c.execute(
                    f"SELECT {_CARD_COLUMNS},"  # not the large diff and report columns: this runs every 5 s
                    " (SELECT COUNT(*) FROM findings f WHERE f.review_id = r.id AND f.status = 'pending') AS pending,"
                    " (SELECT COUNT(*) FROM findings f WHERE f.review_id = r.id AND f.status = 'drafted') AS drafted,"
                    " (SELECT COUNT(*) FROM findings f WHERE f.review_id = r.id AND f.status = 'posted') AS posted,"
                    " (SELECT COUNT(*) FROM findings f WHERE f.review_id = r.id) AS total,"
                    " r.id = (SELECT MAX(x.id) FROM reviews x WHERE x.owner = r.owner AND x.repo = r.repo"
                    f" AND x.number = r.number) AS newest FROM reviews r {where}"
                )
            ]
            events = self._events(c)
        for r in rows:
            pr = (r["owner"], r["repo"], r["number"])
            mine = [
                e
                for e in events.get(pr, [])
                if e[2] == r["id"] or (r["newest"] and (latest_only or e[2] is None))  # PR threads: its newest
            ]
            r["activity_at"], r["activity"], _ = max(mine, default=(r["created_at"], "started", r["id"]))
        rows.sort(key=lambda r: ((r["pr_state"] or "open") != "open", -r["activity_at"], -r["id"]))
        return rows[:limit]

    @staticmethod
    def _events(c: sqlite3.Connection) -> dict[tuple[str, str, int], list[tuple[float, str, int | None]]]:
        """PR -> (time, what happened, review or None) for everything that happened on it."""
        events: dict[tuple[str, str, int], list[tuple[float, str, int | None]]] = {}
        queries = (
            f"SELECT owner, repo, number, created_at, {_REVIEW_EVENT}, id FROM reviews",
            f"SELECT owner, repo, number, finished_at, {_REVIEW_EVENT}, id FROM reviews WHERE finished_at IS NOT NULL",
            "SELECT r.owner, r.repo, r.number, MAX(f.acted_at), f.action, r.id FROM findings f"
            " JOIN reviews r ON r.id = f.review_id WHERE f.acted_at IS NOT NULL GROUP BY r.id",
            "SELECT r.owner, r.repo, r.number, MAX(m.created_at), 'discussed', r.id FROM messages m"
            " JOIN findings f ON f.id = m.finding_id JOIN reviews r ON r.id = f.review_id GROUP BY r.id",
            "SELECT owner, repo, number, MAX(m.created_at), 'discussed', NULL FROM chat_messages m"
            " JOIN chats c ON c.id = m.chat_id GROUP BY owner, repo, number",
        )
        for q in queries:
            for owner, repo, number, at, what, review_id in c.execute(q):
                events.setdefault((owner, repo, number), []).append((at, what, review_id))
        return events

    def earlier_reviews(self) -> dict[tuple[str, str, int], tuple[int, int]]:
        """Per PR: how many reviews are older than its newest, and their findings still open."""
        with self.connect() as c:
            rows = c.execute(
                "SELECT r.owner, r.repo, r.number, COUNT(*),"
                " SUM((SELECT COUNT(*) FROM findings f WHERE f.review_id = r.id AND f.status IN ('pending', 'drafted')))"
                f" FROM reviews r WHERE r.id NOT IN ({_NEWEST_PER_PR}) GROUP BY r.owner, r.repo, r.number"
            ).fetchall()
        return {(o, r, n): (hidden, to_go) for o, r, n, hidden, to_go in rows}

    def fail_stale_reviews(self) -> int:
        """Fail reviews whose process is gone; another process may still be running some."""
        with self.connect() as c:
            rows = c.execute("SELECT id, runner_pid FROM reviews WHERE status IN ('queued', 'running')").fetchall()
            stale = [r["id"] for r in rows if not _alive(r["runner_pid"])]
            adding = c.execute(
                "SELECT id, runner_pid FROM reviews WHERE status = 'done' AND progress IS NOT NULL"
            ).fetchall()
            for r in adding:
                if not _alive(r["runner_pid"]):
                    c.execute(
                        "UPDATE reviews SET progress = NULL, error = ? WHERE id = ?",
                        ("Adding reviewers was interrupted (PRelude restarted)", r["id"]),
                    )
            for rid in stale:
                c.execute(
                    "UPDATE reviews SET status = 'failed', error = 'interrupted (PRelude restarted)', updated_at = ?"
                    " WHERE id = ?",
                    (time.time(), rid),
                )
            c.execute("UPDATE threads SET busy = 0")
            c.execute("UPDATE chats SET busy = 0")
            return len(stale)

    # ---- findings ----------------------------------------------------------

    def add_findings(self, review_id: int, findings: list[dict[str, Any]], added_later: bool = False) -> list[int]:
        now = time.time()
        ids = []
        with self.connect() as c:
            first = c.execute("SELECT COALESCE(MAX(ordinal), 0) FROM findings WHERE review_id = ?", (review_id,))
            start = first.fetchone()[0] + 1
            for i, f in enumerate(findings, start=start):
                cur = c.execute(
                    "INSERT INTO findings (review_id, ordinal, path, line, start_line, side, in_diff, severity,"
                    " category, title, assessment, evidence, confidence, claude_comment, found_by, added_later,"
                    " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        review_id,
                        i,
                        f["path"],
                        f.get("line"),
                        f.get("start_line"),
                        f.get("side", "RIGHT"),
                        int(bool(f.get("in_diff"))),
                        f["severity"],
                        f.get("category"),
                        f["title"],
                        f["assessment"],
                        json.dumps(f.get("evidence") or []),
                        f.get("confidence"),
                        f["suggested_comment"],
                        f.get("found_by"),
                        int(added_later),
                        now,
                        now,
                    ),
                )
                ids.append(cur.lastrowid)
        return ids

    def findings_for(self, review_id: int) -> list[sqlite3.Row]:
        with self.connect() as c:
            return c.execute(
                "SELECT * FROM findings WHERE review_id = ? ORDER BY"
                " CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 WHEN 'low' THEN 2 ELSE 3 END, ordinal",
                (review_id,),
            ).fetchall()

    def bump_found_by(self, finding_id: int, extra: int, reviewers_before: int) -> None:
        """Another `extra` reviewers raised this finding too."""
        with self.connect() as c:
            c.execute(
                "UPDATE findings SET found_by = MIN(COALESCE(found_by, ?) + ?, ?) WHERE id = ?",
                (reviewers_before, extra, reviewers_before + extra, finding_id),
            )

    def get_finding(self, finding_id: int) -> sqlite3.Row | None:
        with self.connect() as c:
            return c.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()

    def update_finding_if(self, finding_id: int, expected: dict[str, Any], **fields: Any) -> bool:
        """Update only if the finding still has the `expected` values; False if someone changed it first."""
        fields.update(_action(fields))
        cols = ", ".join(f"{k} = ?" for k in fields)
        cond = "".join(f" AND {k} IS ?" for k in expected)  # IS: also matches NULL
        with self.connect() as c:
            cur = c.execute(
                f"UPDATE findings SET {cols} WHERE id = ?{cond}", (*fields.values(), finding_id, *expected.values())
            )
            return cur.rowcount == 1

    def update_finding(self, finding_id: int, **fields: Any) -> None:
        fields.update(_action(fields))
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.connect() as c:
            c.execute(f"UPDATE findings SET {cols} WHERE id = ?", (*fields.values(), finding_id))

    # ---- discussion --------------------------------------------------------

    def add_message(
        self,
        finding_id: int,
        role: str,
        content: str,
        *,
        proposal: str | None = None,
        verdict: str | None = None,
        session_id: str | None = None,
    ) -> int:
        with self.connect() as c:
            cur = c.execute(
                "INSERT INTO messages (finding_id, role, content, proposal, verdict, session_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (finding_id, role, content, proposal, verdict, session_id, time.time()),
            )
            return int(cur.lastrowid)

    def messages_for(self, finding_id: int) -> list[sqlite3.Row]:
        with self.connect() as c:
            return c.execute("SELECT * FROM messages WHERE finding_id = ? ORDER BY id", (finding_id,)).fetchall()

    def get_message(self, message_id: int) -> sqlite3.Row | None:
        with self.connect() as c:
            return c.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()

    # ---- memory chat -------------------------------------------------------

    def add_memory_message(
        self,
        scope: str,
        role: str,
        content: str,
        *,
        new_document: str | None = None,
        prev_document: str | None = None,
        finding_id: int | None = None,
    ) -> int:
        with self.connect() as c:
            cur = c.execute(
                "INSERT INTO memory_messages (scope, role, content, new_document, prev_document, finding_id,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (scope, role, content, new_document, prev_document, finding_id, time.time()),
            )
            return int(cur.lastrowid)

    def memory_messages(self, scope: str, limit: int = 40) -> list[sqlite3.Row]:
        with self.connect() as c:
            rows = c.execute(
                "SELECT * FROM memory_messages WHERE scope = ? AND role != 'learned' ORDER BY id DESC LIMIT ?",
                (scope, limit),
            ).fetchall()
            return rows[::-1]

    def memory_changes(self, limit: int = 30) -> list[sqlite3.Row]:
        with self.connect() as c:
            return c.execute(
                "SELECT * FROM memory_messages WHERE new_document IS NOT NULL ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()

    def get_memory_message(self, message_id: int) -> sqlite3.Row | None:
        with self.connect() as c:
            return c.execute("SELECT * FROM memory_messages WHERE id = ?", (message_id,)).fetchone()

    def mark_memory_undone(self, message_id: int) -> None:
        with self.connect() as c:
            c.execute("UPDATE memory_messages SET undone = 1 WHERE id = ?", (message_id,))

    def recent_edits(self, limit: int = 4) -> list[tuple[str, str]]:
        """(Claude's comment, reviewer's rewrite) pairs, newest first."""
        with self.connect() as c:
            rows = c.execute(
                "SELECT claude_comment, user_draft FROM findings WHERE user_draft IS NOT NULL"
                " AND user_draft != claude_comment ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [(r[0], r[1]) for r in rows]

    def prs_to_recheck(self, limit: int = 20) -> list[tuple[str, str, int]]:
        """Reviewed PRs that aren't merged (closed ones can reopen), least recently checked first."""
        with self.connect() as c:
            rows = c.execute(
                "SELECT owner, repo, number FROM reviews WHERE status = 'done'"
                " AND COALESCE(pr_state, 'open') != 'merged' GROUP BY owner, repo, number"
                " ORDER BY MIN(COALESCE(pr_checked_at, 0)) LIMIT ?",
                (limit,),
            ).fetchall()
            return [(r[0], r[1], r[2]) for r in rows]

    def mark_pr_checked(self, owner: str, repo: str, number: int) -> None:
        """Move a PR to the back of the re-check queue, even if reading it failed."""
        with self.connect() as c:
            c.execute(
                "UPDATE reviews SET pr_checked_at = ? WHERE owner = ? AND repo = ? AND number = ?",
                (time.time(), owner, repo, number),
            )

    def set_pr_state(self, owner: str, repo: str, number: int, state: str) -> int:
        """Store the PR's state on all its reviews; returns 1 if it changed."""
        with self.connect() as c:
            cur = c.execute(
                "UPDATE reviews SET pr_state = ?, pr_checked_at = ? WHERE owner = ? AND repo = ? AND number = ?"
                " AND COALESCE(pr_state, 'open') != ?",
                (state, time.time(), owner, repo, number, state),
            )
            c.execute(
                "UPDATE reviews SET pr_checked_at = ? WHERE owner = ? AND repo = ? AND number = ?",
                (time.time(), owner, repo, number),
            )
            return 1 if cur.rowcount else 0

    def drafts_on_pr(self, owner: str, repo: str, number: int, *, besides: int) -> list[sqlite3.Row]:
        """Findings of other reviews of this PR that are still in the pending GitHub review."""
        with self.connect() as c:
            return c.execute(
                "SELECT f.* FROM findings f JOIN reviews r ON r.id = f.review_id WHERE f.status = 'drafted'"
                " AND r.owner = ? AND r.repo = ? AND r.number = ? AND r.id != ?",
                (owner, repo, number, besides),
            ).fetchall()

    def supersede_untouched(self, review_id: int) -> int:
        """The previous review's findings still waiting in To review, never edited or discussed: this
        re-review was shown them and reports again the ones still present, so it replaces them."""
        with self.connect() as c:
            return c.execute(
                "UPDATE findings SET status = 'superseded', updated_at = ?1, acted_at = ?1, action = 'replaced'"
                " WHERE status = 'pending'"
                " AND user_draft IS NULL AND verdict IS NULL"
                " AND NOT EXISTS (SELECT 1 FROM messages WHERE finding_id = findings.id)"
                " AND review_id = (SELECT previous_review_id FROM reviews WHERE id = ?2)",
                (time.time(), review_id),
            ).rowcount

    def reviews_with_drafts(self) -> list[int]:
        with self.connect() as c:
            return [r[0] for r in c.execute("SELECT DISTINCT review_id FROM findings WHERE status = 'drafted'")]

    def repo_activity(self) -> dict[str, tuple[int, float]]:
        """OWNER/REPO -> (number of reviews, last time one started or finished)."""
        with self.connect() as c:
            rows = c.execute(
                "SELECT owner, repo, COUNT(*), MAX(MAX(created_at, COALESCE(finished_at, 0))) FROM reviews GROUP BY owner, repo"
            )
            return {f"{o}/{r}": (n, last) for o, r, n, last in rows}

    def reviewed_repos(self) -> list[tuple[str, str]]:
        with self.connect() as c:
            return [(r[0], r[1]) for r in c.execute("SELECT DISTINCT owner, repo FROM reviews ORDER BY owner, repo")]

    def thread(self, finding_id: int) -> sqlite3.Row | None:
        with self.connect() as c:
            return c.execute("SELECT * FROM threads WHERE finding_id = ?", (finding_id,)).fetchone()

    def try_lock_thread(self, finding_id: int) -> bool:
        """Mark the finding's discussion busy; False if a reply is already running."""
        with self.connect() as c:
            c.execute("INSERT OR IGNORE INTO threads (finding_id, busy) VALUES (?, 0)", (finding_id,))
            cur = c.execute("UPDATE threads SET busy = 1 WHERE finding_id = ? AND busy = 0", (finding_id,))
            return cur.rowcount == 1

    def release_thread(self, finding_id: int, session_id: str | None = None) -> None:
        with self.connect() as c:
            if session_id:
                c.execute("UPDATE threads SET busy = 0, session_id = ? WHERE finding_id = ?", (session_id, finding_id))
            else:
                c.execute("UPDATE threads SET busy = 0 WHERE finding_id = ?", (finding_id,))

    # ---- PR discussion space -----------------------------------------------

    def create_chat(self, review: sqlite3.Row) -> int:
        now = time.time()
        with self.connect() as c:
            cur = c.execute(
                "INSERT INTO chats (owner, repo, number, review_id, head_sha, worktree, base_session_id,"
                " busy, seen_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
                (
                    review["owner"],
                    review["repo"],
                    review["number"],
                    review["id"],
                    review["head_sha"],
                    review["worktree"],
                    review["claude_session_id"],
                    now,
                    now,
                    now,
                ),
            )
            return int(cur.lastrowid)

    def get_chat(self, chat_id: int) -> sqlite3.Row | None:
        with self.connect() as c:
            return c.execute("SELECT * FROM chats WHERE id = ?", (chat_id,)).fetchone()

    def chats_for(self, owner: str, repo: str, number: int) -> list[sqlite3.Row]:
        """Oldest first, with reply counts and whether there's a reply you haven't seen."""
        with self.connect() as c:
            return c.execute(
                "SELECT c.*,"
                " (SELECT content FROM chat_messages WHERE chat_id = c.id ORDER BY id LIMIT 1) AS first_message,"
                " (SELECT COUNT(*) FROM chat_messages WHERE chat_id = c.id) - 1 AS replies,"
                " (SELECT MAX(created_at) FROM chat_messages WHERE chat_id = c.id) AS last_at,"
                " EXISTS (SELECT 1 FROM chat_messages WHERE chat_id = c.id AND role != 'user'"
                " AND created_at > c.seen_at) AS unread"
                " FROM chats c WHERE owner = ? AND repo = ? AND number = ? ORDER BY id",
                (owner, repo, number),
            ).fetchall()

    def chat_unread(self) -> dict[tuple[str, str, int], int]:
        """PR -> threads (its own, and findings' discussions) with a reply you haven't seen."""
        with self.connect() as c:
            rows = c.execute(
                "SELECT owner, repo, number, COUNT(*) FROM ("
                " SELECT owner, repo, number FROM chats c WHERE EXISTS (SELECT 1 FROM chat_messages"
                " WHERE chat_id = c.id AND role != 'user' AND created_at > c.seen_at)"
                " UNION ALL SELECT r.owner, r.repo, r.number FROM findings f JOIN reviews r ON r.id = f.review_id"
                f" WHERE EXISTS ({_UNREAD_FINDING_REPLY.format(finding='f.id')})"
                ") GROUP BY owner, repo, number"
            )
            return {(o, r, n): k for o, r, n, k in rows}

    def update_chat(self, chat_id: int, **fields: Any) -> None:
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.connect() as c:
            c.execute(f"UPDATE chats SET {cols} WHERE id = ?", (*fields.values(), chat_id))

    def try_lock_chat(self, chat_id: int) -> bool:
        with self.connect() as c:
            return c.execute("UPDATE chats SET busy = 1 WHERE id = ? AND busy = 0", (chat_id,)).rowcount == 1

    def release_chat(self, chat_id: int, session_id: str | None = None) -> None:
        with self.connect() as c:
            c.execute(
                "UPDATE chats SET busy = 0, session_id = COALESCE(?, session_id), updated_at = ? WHERE id = ?",
                (session_id, time.time(), chat_id),
            )

    def delete_chat(self, chat_id: int) -> None:
        with self.connect() as c:
            c.execute("DELETE FROM chats WHERE id = ?", (chat_id,))

    def add_chat_message(self, chat_id: int, role: str, content: str) -> int:
        with self.connect() as c:
            cur = c.execute(
                "INSERT INTO chat_messages (chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (chat_id, role, content, time.time()),
            )
            return int(cur.lastrowid)

    def chat_messages(self, chat_id: int) -> list[sqlite3.Row]:
        with self.connect() as c:
            return c.execute("SELECT * FROM chat_messages WHERE chat_id = ? ORDER BY id", (chat_id,)).fetchall()

    _COMMENT_THREAD = (
        "SELECT f.*, r.id AS review_id,"
        " (SELECT COUNT(*) FROM messages WHERE finding_id = f.id) AS messages,"
        " (SELECT MAX(created_at) FROM messages WHERE finding_id = f.id) AS last_at,"
        " COALESCE((SELECT busy FROM threads WHERE finding_id = f.id), 0) AS busy,"
        f" EXISTS ({_UNREAD_FINDING_REPLY.format(finding='f.id')}) AS unread"
        " FROM findings f JOIN reviews r ON r.id = f.review_id"
    )

    def comment_threads(self, owner: str, repo: str, number: int) -> list[sqlite3.Row]:
        """The newest finished review's live comments (to review or in your draft) and every comment
        you discussed, with their discussion's state: first the ones that need a look, then by latest message."""
        with self.connect() as c:
            return c.execute(
                self._COMMENT_THREAD + " WHERE r.owner = ? AND r.repo = ? AND r.number = ?"
                " AND ((f.status IN ('pending', 'drafted') AND r.id = (SELECT MAX(id) FROM reviews"
                "  WHERE owner = r.owner AND repo = r.repo AND number = r.number AND status = 'done'))"
                "  OR EXISTS (SELECT 1 FROM messages WHERE finding_id = f.id))"
                " ORDER BY busy OR unread DESC, last_at IS NULL, last_at DESC, r.id, f.ordinal",
                (owner, repo, number),
            ).fetchall()

    def comment_thread(self, finding_id: int) -> sqlite3.Row | None:
        """One finding with its discussion's state, live or not."""
        with self.connect() as c:
            return c.execute(self._COMMENT_THREAD + " WHERE f.id = ?", (finding_id,)).fetchone()

    def discussion_state(self, review_id: int) -> dict[int, str]:
        """Finding -> "replying" or "new reply", for findings whose discussion needs a look."""
        with self.connect() as c:
            rows = c.execute(
                "SELECT f.id, COALESCE((SELECT busy FROM threads WHERE finding_id = f.id), 0),"
                f" EXISTS ({_UNREAD_FINDING_REPLY.format(finding='f.id')}) FROM findings f WHERE f.review_id = ?",
                (review_id,),
            )
            return {fid: "replying" if busy else "new reply" for fid, busy, unread in rows if busy or unread}

    def mark_thread_seen(self, finding_id: int) -> None:
        with self.connect() as c:
            c.execute("INSERT OR IGNORE INTO threads (finding_id, busy) VALUES (?, 0)", (finding_id,))
            c.execute("UPDATE threads SET seen_at = ? WHERE finding_id = ?", (time.time(), finding_id))


_CARD_COLUMNS = ", ".join(
    f"r.{c}"
    for c in [
        "id",
        "owner",
        "repo",
        "number",
        "url",
        "title",
        "author",
        "status",
        "progress",
        "error",
        "pr_state",
        "trigger_reason",
        "reviewers",
        "previous_review_id",
        "mode",
        "created_at",
        "finished_at",
    ]
)

_REVIEW_EVENT = (
    "CASE status WHEN 'done' THEN 'reviewed' WHEN 'failed' THEN 'review failed'"
    " WHEN 'skipped' THEN 'skipped' ELSE 'started' END"
)

ACTIONS = {
    "drafted": "drafted",
    "discarded": "discarded",
    "posted": "posted",
    "pending": "back to review",
    "superseded": "replaced",
    "dropped": "dropped",
}


def _action(fields: dict[str, Any]) -> dict[str, Any]:
    """Timestamps for an update, and which action it was if it changes the status or wording."""
    now = time.time()
    out: dict[str, Any] = {"updated_at": now}
    if "acted_at" in fields:  # the caller says what it was (e.g. undoing a change, or nothing changed)
        return out
    if "status" in fields:
        out.update(acted_at=now, action=ACTIONS.get(fields["status"], fields["status"]))
    elif "user_draft" in fields:
        out.update(acted_at=now, action="edited")
    return out


def _alive(pid: int | None) -> bool:
    if not pid or pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _add_missing_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    for name, decl in columns.items():
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
