"""Command line: `prelude review <PR>`, `prelude serve`, `prelude list`, `prelude show <id>`."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from . import claude
from .config import load_config
from .db import Store
from .github import parse_pr
from .guard import RepoNotAllowed, check_repo
from .review import run_review, start_review


def _cmd_review(args: argparse.Namespace) -> int:
    cfg = load_config()
    store = Store(cfg.db_path)
    try:
        ref = parse_pr(args.pr)
        review_id = start_review(store, cfg, ref, trigger_reason="manual (cli)", reviewers=args.reviewers)
    except (ValueError, RepoNotAllowed) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"review #{review_id}: {ref.slug} — running (this can take several minutes)…")
    run_review(store, cfg, review_id)
    return _print_review(store, review_id)


def _print_review(store: Store, review_id: int) -> int:
    r = store.get_review(review_id)
    if r is None:
        print(f"no review #{review_id}", file=sys.stderr)
        return 1
    print(f"\n#{r['id']} {r['owner']}/{r['repo']}#{r['number']} {r['title'] or ''}")
    usage = f"  usage: ≈ ${r['cost_usd']:.2f} API-equivalent (not billed on a subscription)" if r["cost_usd"] else ""
    print(f"status: {r['status']}{usage}")
    if r["error"]:
        print(f"error: {r['error']}")
        return 1
    if r["summary"]:
        print(f"\n{r['summary']}\n")
    for f in store.findings_for(review_id):
        loc = f"{f['path']}:{f['line']}" if f["line"] else f["path"]
        flag = "" if f["in_diff"] else "  (not on a diff line)"
        agree = f"  (found by {f['found_by']}/{r['reviewers']})" if f["found_by"] else ""
        print(f"[{f['severity']:>6}] {f['title']}{agree}\n         {loc}{flag}  — {f['status']}")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    cfg = load_config()
    return _print_review(Store(cfg.db_path), args.id)


def _cmd_list(_args: argparse.Namespace) -> int:
    cfg = load_config()
    for r in Store(cfg.db_path).list_reviews():
        print(f"#{r['id']:<4} {r['status']:<8} {r['owner']}/{r['repo']}#{r['number']}  "
              f"{r['pending']}/{r['total']} pending  {r['title'] or ''}")
    return 0


def _cmd_auth(_args: argparse.Namespace) -> int:
    status = claude.auth_status()
    safe = {k: status.get(k) for k in ("loggedIn", "authMethod", "subscriptionType")}
    print(json.dumps(safe, indent=2))
    try:
        claude.ensure_logged_in()
        print("OK: PRelude can run reviews with this login.")
        return 0
    except claude.ClaudeNotLoggedIn as e:
        print(f"blocked: {e}")
        return 1


def _cmd_style_init(args: argparse.Namespace) -> int:
    from .github import GitHubReader
    from .memory_agent import init_style

    cfg = load_config()
    owner, _, repo = args.repo.partition("/")
    if not owner or not repo:
        print("error: expected OWNER/REPO", file=sys.stderr)
        return 2
    try:
        check_repo(cfg, owner, repo)
        gh = GitHubReader(cfg)
        login = args.author or gh.viewer_login()
        comments = gh.comments_by(owner, repo, login, limit=args.limit)
    except RepoNotAllowed as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if len(comments) < 5:
        print(f"Only {len(comments)} comments by {login} on {args.repo}; not enough to infer a style.")
        return 1
    print(f"Learning comment style from {len(comments)} comments by {login} on {args.repo}…")
    summary = init_style(Store(cfg.db_path), cfg, args.repo, comments)
    print(summary or "No change.")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    from .web import serve

    cfg = load_config()
    serve(cfg, host=args.host, port=args.port)
    return 0


def _cmd_url(_args: argparse.Namespace) -> int:
    cfg = load_config()
    print(f"http://{cfg.server.host}:{cfg.server.port}/?key={cfg.access_key()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="prelude", description="Human-in-the-loop PR review")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("review", help="review a PR now and print the findings")
    s.add_argument("pr", help="PR link (https://github.com/OWNER/REPO/pull/N) or OWNER/REPO#N")
    s.add_argument("-n", "--reviewers", type=int, default=None,
                   help="independent reviewers, merged by a meta-reviewer; 0 = by PR size (default: config)")
    s.set_defaults(fn=_cmd_review)

    s = sub.add_parser("show", help="print a stored review")
    s.add_argument("id", type=int)
    s.set_defaults(fn=_cmd_show)

    sub.add_parser("list", help="list reviews").set_defaults(fn=_cmd_list)
    sub.add_parser("auth", help="check the Claude login PRelude will use").set_defaults(fn=_cmd_auth)
    sub.add_parser("url", help="print the web UI link (with access key)").set_defaults(fn=_cmd_url)

    s = sub.add_parser("style-init", help="write your comment style guide from past comments on a repo")
    s.add_argument("repo", help="OWNER/REPO")
    s.add_argument("--author", help="GitHub login (default: you)")
    s.add_argument("--limit", type=int, default=150)
    s.set_defaults(fn=_cmd_style_init)

    s = sub.add_parser("serve", help="run the web UI")
    s.add_argument("--host", default=None)
    s.add_argument("--port", type=int, default=None)
    s.set_defaults(fn=_cmd_serve)

    args = p.parse_args(argv)
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
