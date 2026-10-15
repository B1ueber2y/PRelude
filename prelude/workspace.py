"""Cached bare clones and read-only per-review worktrees at the PR head."""

from __future__ import annotations

import fcntl
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .github import PullRequest
from .guard import check_repo
from .retry import retry

# Use the `gh` login for HTTPS git access, ignoring any other credential helpers.
_GIT_AUTH = ["-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential"]


class GitError(RuntimeError):
    def __init__(self, message: str, transient: bool = False):
        super().__init__(message)
        self.transient = transient


_GIT_NETWORK = re.compile(
    r"could not resolve host|connection reset|connection timed out|operation timed out|early eof"
    r"|rpc failed|the remote end hung up"
    r"|(?:http|returned error:)\s*50[234]\b"  # git's own HTTP wording, not bare numbers in refs or SHAs
)


def _git(*args: str, cwd: Path | None = None, timeout: int = 600) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
    if out.returncode != 0:
        err = out.stderr.strip()
        network = "fetch" in args and bool(_GIT_NETWORK.search(err.lower()))
        raise GitError(f"git {' '.join(args[:3])}… failed: {err[-2000:]}", transient=network)
    return out.stdout


@dataclass
class Checkout:
    worktree: Path
    merge_base: str
    diff: str
    changed_files: list[str]


class RepoBusy(Exception):
    pass


@contextmanager
def _repo_lock(cfg: Config, owner: str, repo: str, wait: bool = True) -> Iterator[None]:
    """One writer at a time per cached clone, across threads and processes (server, CLI).

    With wait=False, raises RepoBusy instead of waiting.
    """
    lock = cfg.repos_dir / owner / f"{repo}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            raise RepoBusy(f"{owner}/{repo} is being checked out") from None
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _bare_repo(cfg: Config, owner: str, repo: str) -> Path:
    path = cfg.repos_dir / owner / f"{repo}.git"
    if not (path / "HEAD").exists():
        path.mkdir(parents=True, exist_ok=True)
        _git("init", "--bare", "--quiet", str(path))
        _git("--git-dir", str(path), "config", "remote.origin.url", f"https://github.com/{owner}/{repo}.git")
        _git("--git-dir", str(path), "config", "remote.origin.promisor", "true")
        _git("--git-dir", str(path), "config", "remote.origin.partialclonefilter", "blob:none")
    return path


def _make_read_only(root: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(root):
        if ".git" in dirnames:
            dirnames.remove(".git")
        for name in filenames:
            p = Path(dirpath) / name
            try:
                mode = p.lstat().st_mode
                if not stat.S_ISLNK(mode):
                    os.chmod(p, mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
            except OSError:
                pass


def _make_writable(root: Path) -> None:
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            p = Path(dirpath) / name
            try:
                mode = p.lstat().st_mode
                if not stat.S_ISLNK(mode):
                    os.chmod(p, mode | stat.S_IWUSR)
            except OSError:
                pass


def _is_checkout_of(wt: Path, sha: str) -> bool:
    if not (wt / ".git").exists():
        return False
    try:
        return _git("-C", str(wt), "rev-parse", "HEAD").strip() == sha
    except GitError:
        return False


def _fetch(gd: list[str], *refs: str) -> None:
    """Fetch from GitHub, retrying network hiccups."""
    retry(
        lambda: _git(*_GIT_AUTH, *gd, "fetch", "--quiet", "--no-tags", "--filter=blob:none", "origin", *refs),
        what="git fetch",
    )


def prepare_checkout(cfg: Config, pr: PullRequest) -> Checkout:
    owner, repo, number = pr.ref.owner, pr.ref.repo, pr.ref.number
    check_repo(cfg, owner, repo)
    with _repo_lock(cfg, owner, repo):
        bare = _bare_repo(cfg, owner, repo)
        gd = ["--git-dir", str(bare)]

        _fetch(
            gd,
            f"+refs/pull/{number}/head:refs/prelude/pr/{number}",
            f"+refs/heads/{pr.base_ref}:refs/prelude/base/{pr.base_ref}",
        )
        # The PR may have moved on since we read its metadata; insist on the exact SHAs.
        for sha in (pr.head_sha, pr.base_sha):
            try:
                _git(*gd, "cat-file", "-e", f"{sha}^{{commit}}")
            except GitError:
                _fetch(gd, sha)

        merge_base = _git(*gd, "merge-base", pr.base_sha, pr.head_sha).strip()

        wt = cfg.worktrees_dir / f"{owner}__{repo}__{number}__{pr.head_sha[:12]}"
        if not _is_checkout_of(wt, pr.head_sha):
            if wt.exists():
                _remove_checkout(cfg, owner, repo, wt)
            wt.parent.mkdir(parents=True, exist_ok=True)
            _git(*_GIT_AUTH, *gd, "worktree", "add", "--quiet", "--detach", str(wt), pr.head_sha, timeout=1800)
            _make_read_only(wt)
        # else: reuse it; other reviews and discussions of this commit may be using it right now

    diff = _git(*gd, "diff", "--no-color", "--no-ext-diff", "-M", f"{merge_base}..{pr.head_sha}")
    names = _git(*gd, "diff", "--name-only", "-M", f"{merge_base}..{pr.head_sha}").split()
    return Checkout(worktree=wt, merge_base=merge_base, diff=diff, changed_files=names)


def remove_checkout(cfg: Config, owner: str, repo: str, wt: Path) -> bool:
    """Remove a review's checkout. Skipped (False) while a checkout of the repo is in progress,
    since that one may be about to reuse this worktree."""
    if not wt.resolve().is_relative_to(cfg.worktrees_dir.resolve()):
        raise ValueError(f"refusing to remove {wt}: not under {cfg.worktrees_dir}")
    try:
        with _repo_lock(cfg, owner, repo, wait=False):
            _remove_checkout(cfg, owner, repo, wt)
    except RepoBusy:
        return False
    return True


def _remove_checkout(cfg: Config, owner: str, repo: str, wt: Path) -> None:
    bare = cfg.repos_dir / owner / f"{repo}.git"
    _make_writable(wt)
    try:
        _git("--git-dir", str(bare), "worktree", "remove", "--force", str(wt))
    except GitError:
        shutil.rmtree(wt, ignore_errors=True)
        _git("--git-dir", str(bare), "worktree", "prune")


def diff_between(cfg: Config, owner: str, repo: str, old_sha: str, new_sha: str) -> str | None:
    """Changes from an earlier head to the current one, or None if the old commit is gone."""
    gd = ["--git-dir", str(cfg.repos_dir / owner / f"{repo}.git")]
    try:
        return _git(*gd, "diff", "--no-color", "--no-ext-diff", "-M", f"{old_sha}..{new_sha}")
    except GitError:
        return None
