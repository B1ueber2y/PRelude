"""While `only_repo` is set, every GitHub access must target that repository."""

from __future__ import annotations

from .config import Config


class RepoNotAllowed(Exception):
    pass


def check_repo(cfg: Config, owner: str, repo: str) -> None:
    if not cfg.only_repo:
        return
    if f"{owner}/{repo}".lower() != cfg.only_repo.lower():
        raise RepoNotAllowed(
            f"{owner}/{repo} is not allowed: PRelude is restricted to {cfg.only_repo} "
            "(only_repo in config.yaml)"
        )
