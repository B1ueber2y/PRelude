"""SQLite storage. `claude_comment` is Claude's text; `user_draft` is only written by the reviewer."""

from __future__ import annotations

import json
import os
import sqlite3
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
  author TEXT,
  base_ref TEXT,
  base_sha TEXT,
  head_sha TEXT,
  merge_base TEXT,
  status TEXT NOT NULL DEFAULT 'queued',   -- queued | running | done | failed
  progress TEXT,
  error TEXT,
  trigger_reason TEXT,
  summary TEXT,
  claude_session_id TEXT,
  worktree TEXT,
  diff TEXT,
  cost_usd REAL,
  reviewers INTEGER NOT NULL DEFAULT 1,     -- 0 until resolved from PR size
  changed_lines INTEGER,
  runner_pid INTEGER,                      -- process running the review
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
  status TEXT NOT NULL DEFAULT 'pending',  -- pending | drafted | discarded
  verdict TEXT,                            -- latest Claude verdict from discussion
  github_review_id TEXT,                   -- pending review the comment was added to
  github_comment_id TEXT,
  drafted_body TEXT,                       -- body as last written to GitHub
  found_by INTEGER,                        -- reviewers who raised it (multi-reviewer runs)
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

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
  reviewed_sha TEXT,                       -- head of the last automatic review
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
"""


class Store:
    def __init__(self, path: Path):
        self.path = path
        with self.connect() as c:
            c.executescript(SCHEMA)
            _add_missing_columns(c, "findings", {
                "github_review_id": "TEXT", "github_comment_id": "TEXT", "drafted_body": "TEXT",
                "found_by": "INTEGER",
            })
            _add_missing_columns(c, "reviews", {
                "reviewers": "INTEGER NOT NULL DEFAULT 1", "reports": "TEXT", "changed_lines": "INTEGER",
                "runner_pid": "INTEGER",
            })

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- settings ----------------------------------------------------------

    def setting(self, key: str, default: str) -> str:
        with self.connect() as c:
            row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as c:
            c.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?",
                      (key, value, value))

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
            c.execute(f"UPDATE watched SET {cols} WHERE owner = ? AND repo = ? AND number = ?",
                      (*fields.values(), owner, repo, number))

    def auto_reviews_since(self, since: float) -> int:
        with self.connect() as c:
            return c.execute(
                "SELECT COUNT(*) FROM reviews WHERE trigger_reason LIKE 'auto:%' AND created_at >= ?", (since,)
            ).fetchone()[0]

    # ---- reviews -----------------------------------------------------------

    def create_review(
        self, owner: str, repo: str, number: int, url: str, trigger_reason: str, reviewers: int = 1,
    ) -> int:
        now = time.time()
        with self.connect() as c:
            cur = c.execute(
                "INSERT INTO reviews (owner, repo, number, url, trigger_reason, reviewers, status, created_at,"
                " updated_at) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
                (owner, repo, number, url, trigger_reason, reviewers, now, now),
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

    def list_reviews(self, limit: int = 50) -> list[sqlite3.Row]:
        with self.connect() as c:
            return c.execute(
                "SELECT r.*,"
                " (SELECT COUNT(*) FROM findings f WHERE f.review_id = r.id AND f.status = 'pending') AS pending,"
                " (SELECT COUNT(*) FROM findings f WHERE f.review_id = r.id AND f.status = 'drafted') AS drafted,"
                " (SELECT COUNT(*) FROM findings f WHERE f.review_id = r.id) AS total"
                " FROM reviews r ORDER BY r.id DESC LIMIT ?",
                (limit,),
            ).fetchall()

    def fail_stale_reviews(self) -> int:
        """Fail reviews whose process is gone; another process may still be running some."""
        with self.connect() as c:
            rows = c.execute(
                "SELECT id, runner_pid FROM reviews WHERE status IN ('queued', 'running')"
            ).fetchall()
            stale = [r["id"] for r in rows if not _alive(r["runner_pid"])]
            for rid in stale:
                c.execute(
                    "UPDATE reviews SET status = 'failed', error = 'interrupted (PRelude restarted)', updated_at = ?"
                    " WHERE id = ?",
                    (time.time(), rid),
                )
            c.execute("UPDATE threads SET busy = 0")
            return len(stale)

    # ---- findings ----------------------------------------------------------

    def add_findings(self, review_id: int, findings: list[dict[str, Any]]) -> None:
        now = time.time()
        with self.connect() as c:
            for i, f in enumerate(findings, start=1):
                c.execute(
                    "INSERT INTO findings (review_id, ordinal, path, line, start_line, side, in_diff, severity,"
                    " category, title, assessment, evidence, confidence, claude_comment, found_by, created_at,"
                    " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        review_id, i, f["path"], f.get("line"), f.get("start_line"), f.get("side", "RIGHT"),
                        int(bool(f.get("in_diff"))), f["severity"], f.get("category"), f["title"],
                        f["assessment"], json.dumps(f.get("evidence") or []), f.get("confidence"),
                        f["suggested_comment"], f.get("found_by"), now, now,
                    ),
                )

    def findings_for(self, review_id: int) -> list[sqlite3.Row]:
        with self.connect() as c:
            return c.execute(
                "SELECT * FROM findings WHERE review_id = ? ORDER BY"
                " CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 WHEN 'low' THEN 2 ELSE 3 END, ordinal",
                (review_id,),
            ).fetchall()

    def get_finding(self, finding_id: int) -> sqlite3.Row | None:
        with self.connect() as c:
            return c.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()

    def update_finding(self, finding_id: int, **fields: Any) -> None:
        fields["updated_at"] = time.time()
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
            return c.execute(
                "SELECT * FROM messages WHERE finding_id = ? ORDER BY id", (finding_id,)
            ).fetchall()

    def get_message(self, message_id: int) -> sqlite3.Row | None:
        with self.connect() as c:
            return c.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()

    # ---- memory chat -------------------------------------------------------

    def add_memory_message(
        self, scope: str, role: str, content: str, *,
        new_document: str | None = None, prev_document: str | None = None, finding_id: int | None = None,
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
