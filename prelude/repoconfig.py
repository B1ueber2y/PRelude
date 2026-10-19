"""Editing the repos part of `triggers` in config.yaml, keeping its comments and layout."""

from __future__ import annotations

import io
import os
import re
import tempfile
import threading
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from .config import Config
from .triggers import MODES, TriggerConfig, TriggerConfigError

PATTERN = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._*?-]+$")  # OWNER/REPO, wildcards allowed in REPO
_LOCK = threading.Lock()  # one read-modify-write of config.yaml at a time


class RepoConfigError(ValueError):
    pass


def _list_offset(text: str) -> int:
    """How far the file indents a block list's dashes past its key (the default config uses 2)."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if (m := re.match(r"( *)- ", line)) and i and (k := re.match(r"( *)[^ #-][^:]*:\s*(#.*)?$", lines[i - 1])):
            return max(0, len(m.group(1)) - len(k.group(1)))
    return 2


def _yaml(text: str = "") -> YAML:
    y = YAML()  # round-trip: comments, order and flow style survive
    y.preserve_quotes = True
    y.width = 4096
    offset = _list_offset(text)
    y.indent(mapping=2, sequence=offset + 2, offset=offset)  # block lists as the file already writes them
    y.representer.add_representer(  # keep `null` written as null, not as an empty value
        type(None), lambda r, _: r.represent_scalar("tag:yaml.org,2002:null", "null")
    )
    return y


def _key(m: CommentedMap, slug: str) -> str | None:
    """The existing key for `slug` (patterns are matched case-insensitively, as the triggers do)."""
    return next((k for k in m if str(k).lower() == slug.lower()), None)


def _edit(cfg: Config, change: Callable[[CommentedMap], None]) -> TriggerConfig:
    """Apply `change` to the triggers section; validate, then write atomically and update cfg in place."""
    with _LOCK:
        return _edit_locked(cfg, change)


def _edit_locked(cfg: Config, change: Callable[[CommentedMap], None]) -> TriggerConfig:
    path = cfg.data_dir / "config.yaml"
    original = path.read_text()
    y = _yaml(original)
    data = y.load(original) or CommentedMap()
    triggers = data.get("triggers")
    if not isinstance(triggers, CommentedMap):
        triggers = data["triggers"] = CommentedMap()
    change(triggers)

    buf = io.StringIO()
    y.dump(data, buf)
    text = buf.getvalue()
    try:  # parsed as load_config parses it (PyYAML, YAML 1.1: `no` is False)
        new = TriggerConfig.from_raw((yaml.safe_load(text) or {}).get("triggers"))
    except (TriggerConfigError, ValueError, TypeError) as e:
        raise RepoConfigError(str(e)) from e
    new.enabled = cfg.triggers.enabled  # turning the poller on or off needs a restart

    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config.", suffix=".yaml")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    cfg.triggers = new  # the poller reads cfg.triggers each round
    return new


def _repos(triggers: CommentedMap) -> CommentedMap:
    repos = triggers.get("repos")
    if not isinstance(repos, CommentedMap):
        repos = triggers["repos"] = CommentedMap()
    if repos.fa.flow_style():  # `repos: {}` in a fresh config
        repos.fa.set_block_style()
    return repos


def _entry(mode: str, since: str | None) -> Any:
    if not since:
        return mode
    entry = CommentedMap(mode=mode, since=date.fromisoformat(since))
    entry.fa.set_flow_style()  # {mode: all_prs, since: 2026-10-10}, like the hand-written ones
    return entry


def configured(cfg: Config) -> list[dict[str, Any]]:
    """The repos listed in config.yaml, as written (original case), with their settings."""
    raw = YAML(typ="safe").load((cfg.data_dir / "config.yaml").read_text()) or {}
    t = cfg.triggers
    rows = []
    for slug, value in ((raw.get("triggers") or {}).get("repos") or {}).items():
        since = value.get("since") if isinstance(value, dict) else None
        rows.append(
            {
                "slug": str(slug),
                "mode": t.repos.get(str(slug).lower(), t.default_mode),
                "since": str(since) if since else None,
                "exempt": str(slug).lower() in t.cap_exempt,
                "post": str(slug).lower() in t.auto_post,
                "clean": str(slug).lower() in t.clean_note,
            }
        )
    return rows


def check_pattern(slug: str) -> str:
    slug = slug.strip().removeprefix("https://github.com/").strip("/")
    if not PATTERN.match(slug):
        raise RepoConfigError(f"{slug or 'That'} isn't OWNER/REPO or OWNER/*")
    return slug


def set_repo(cfg: Config, slug: str, mode: str, since: str | None = None) -> TriggerConfig:
    """Add or change a repo's mode; `since` applies to all_prs (today if not given)."""
    slug = check_pattern(slug)
    if mode not in MODES:
        raise RepoConfigError(f"unknown mode {mode!r}")
    if mode == "all_prs":
        since = since or date.today().isoformat()
        try:
            date.fromisoformat(since)
        except ValueError:
            raise RepoConfigError(f"{since!r} is not a date like 2026-10-10") from None
    else:
        since = None

    def change(t: CommentedMap) -> None:
        repos = _repos(t)
        key = _key(repos, slug)
        if key is None:
            repos[slug] = _entry(mode, since)
        else:
            repos[key] = _entry(mode, since)

    return _edit(cfg, change)


def remove_repo(cfg: Config, slug: str) -> TriggerConfig:
    def change(t: CommentedMap) -> None:
        repos = _repos(t)
        if (key := _key(repos, slug)) is not None:
            del repos[key]
        for name in ("cap_exempt", "auto_post", "clean_note"):
            _set_listed(t, name, slug, False)

    return _edit(cfg, change)


def _set_listed(t: CommentedMap, name: str, slug: str, on: bool) -> None:
    """Add `slug` to (or remove it from) the repo list `name` in triggers."""
    items = t.get(name)
    if not isinstance(items, list):
        if not on:
            return
        items = t[name] = CommentedSeq()
        items.fa.set_flow_style()
    present = [x for x in items if str(x).lower() == slug.lower()]
    if on and not present:
        items.append(slug)
    for item in present if not on else []:
        items.remove(item)


def set_cap_exempt(cfg: Config, slug: str, exempt: bool) -> TriggerConfig:
    slug = check_pattern(slug)
    return _edit(cfg, lambda t: _set_listed(t, "cap_exempt", slug, exempt))


def set_auto_post(cfg: Config, slug: str, on: bool) -> TriggerConfig:
    slug = check_pattern(slug)
    return _edit(cfg, lambda t: _set_listed(t, "auto_post", slug, on))


def set_clean_note(cfg: Config, slug: str, on: bool) -> TriggerConfig:
    slug = check_pattern(slug)
    return _edit(cfg, lambda t: _set_listed(t, "clean_note", slug, on))
