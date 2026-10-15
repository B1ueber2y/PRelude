"""Editing the repos part of `triggers` in config.yaml, keeping its comments and layout."""

from __future__ import annotations

import io
import os
import re
import tempfile
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from .config import Config
from .triggers import MODES, TriggerConfig, TriggerConfigError

PATTERN = re.compile(r"^[A-Za-z0-9-]+/(\*|[A-Za-z0-9._-]+)$")


class RepoConfigError(ValueError):
    pass


def _yaml() -> YAML:
    y = YAML()  # round-trip: comments, order and flow style survive
    y.preserve_quotes = True
    y.width = 4096
    y.representer.add_representer(  # keep `null` written as null, not as an empty value
        type(None), lambda r, _: r.represent_scalar("tag:yaml.org,2002:null", "null")
    )
    return y


def _key(m: CommentedMap, slug: str) -> str | None:
    """The existing key for `slug` (patterns are matched case-insensitively, as the triggers do)."""
    return next((k for k in m if str(k).lower() == slug.lower()), None)


def _edit(cfg: Config, change: Callable[[CommentedMap], None]) -> TriggerConfig:
    """Apply `change` to the triggers section; validate, then write atomically and update cfg in place."""
    path = cfg.data_dir / "config.yaml"
    y = _yaml()
    data = y.load(path.read_text()) or CommentedMap()
    triggers = data.get("triggers")
    if not isinstance(triggers, CommentedMap):
        triggers = data["triggers"] = CommentedMap()
    change(triggers)

    buf = io.StringIO()
    y.dump(data, buf)
    text = buf.getvalue()
    try:
        new = TriggerConfig.from_raw(YAML(typ="safe").load(text).get("triggers"))
    except (TriggerConfigError, ValueError, TypeError) as e:
        raise RepoConfigError(str(e)) from e

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
        exempt = t.get("cap_exempt")
        if isinstance(exempt, list):
            for item in [x for x in exempt if str(x).lower() == slug.lower()]:
                exempt.remove(item)

    return _edit(cfg, change)


def set_cap_exempt(cfg: Config, slug: str, exempt: bool) -> TriggerConfig:
    slug = check_pattern(slug)

    def change(t: CommentedMap) -> None:
        items = t.get("cap_exempt")
        if not isinstance(items, list):
            items = t["cap_exempt"] = CommentedSeq()
            items.fa.set_flow_style()
        present = [x for x in items if str(x).lower() == slug.lower()]
        if exempt and not present:
            items.append(slug)
        for item in present if not exempt else []:
            items.remove(item)

    return _edit(cfg, change)
