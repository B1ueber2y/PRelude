"""Cached bare clones and read-only per-review worktrees at the PR head."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .github import PullRequest
from .guard import check_repo

# Use the `gh` login for HTTPS git access, ignoring any other credential helpers.
_GIT_AUTH = ["-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential"]


class GitError(RuntimeError):
    pass


def _git(*args: str, cwd: Path | None = None, timeout: int = 600) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
    if out.returncode != 0:
        raise GitError(f"git {' '.join(args[:3])}… failed: {out.stderr.strip()[-2000:]}")
    return out.stdout


@dataclass
class Checkout:
    worktree: Path
    merge_base: str
    diff: str
    changed_files: list[str]


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


def prepare_checkout(cfg: Config, pr: PullRequest) -> Checkout:
    owner, repo, number = pr.ref.owner, pr.ref.repo, pr.ref.number
    check_repo(cfg, owner, repo)
    bare = _bare_repo(cfg, owner, repo)
    gd = ["--git-dir", str(bare)]

    _git(
        *_GIT_AUTH, *gd, "fetch", "--quiet", "--no-tags", "--filter=blob:none", "origin",
        f"+refs/pull/{number}/head:refs/prelude/pr/{number}",
        f"+refs/heads/{pr.base_ref}:refs/prelude/base/{pr.base_ref}",
    )
    # The PR may have moved on since we read its metadata; insist on the exact SHAs.
    for sha in (pr.head_sha, pr.base_sha):
        try:
            _git(*gd, "cat-file", "-e", f"{sha}^{{commit}}")
        except GitError:
            _git(*_GIT_AUTH, *gd, "fetch", "--quiet", "--no-tags", "--filter=blob:none", "origin", sha)

    merge_base = _git(*gd, "merge-base", pr.base_sha, pr.head_sha).strip()

    wt = cfg.worktrees_dir / f"{owner}__{repo}__{number}__{pr.head_sha[:12]}"
    if wt.exists():
        remove_checkout(cfg, owner, repo, wt)
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(*_GIT_AUTH, *gd, "worktree", "add", "--quiet", "--detach", str(wt), pr.head_sha, timeout=1800)
    _make_read_only(wt)

    diff = _git(*gd, "diff", "--no-color", "--no-ext-diff", "-M", f"{merge_base}..{pr.head_sha}")
    names = _git(*gd, "diff", "--name-only", "-M", f"{merge_base}..{pr.head_sha}").split()
    return Checkout(worktree=wt, merge_base=merge_base, diff=diff, changed_files=names)


def remove_checkout(cfg: Config, owner: str, repo: str, wt: Path) -> None:
    bare = cfg.repos_dir / owner / f"{repo}.git"
    if not wt.resolve().is_relative_to(cfg.worktrees_dir.resolve()):
        raise ValueError(f"refusing to remove {wt}: not under {cfg.worktrees_dir}")
    _make_writable(wt)
    try:
        _git("--git-dir", str(bare), "worktree", "remove", "--force", str(wt))
    except GitError:
        shutil.rmtree(wt, ignore_errors=True)
        _git("--git-dir", str(bare), "worktree", "prune")
