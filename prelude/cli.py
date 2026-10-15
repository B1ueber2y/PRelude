"""Command line: review PRs, triage the findings, and run the web UI."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

from . import claude, triage
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
        previous = store.latest_review_for(ref.owner, ref.repo, ref.number) if args.again else None
        if args.again and previous is None:
            print("No earlier review of this PR; doing a normal review.")
        review_id = start_review(
            store,
            cfg,
            ref,
            trigger_reason="manual (cli)",
            reviewers=args.reviewers,
            previous_review_id=previous["id"] if previous else None,
            mode=args.again or "full",
        )
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
        print(
            f"#{r['id']:<4} {r['status']:<8} {r['owner']}/{r['repo']}#{r['number']}  "
            f"{r['pending']}/{r['total']} pending  {r['title'] or ''}"
        )
    return 0


# ---- triage ----------------------------------------------------------------
# Findings are addressed as REVIEW N, where N is the number `prelude findings` shows.


class UsageError(Exception):
    pass


def _findings(store: Store, review_id: int, numbers: list[int]) -> list:
    by_number = {f["ordinal"]: f for f in store.findings_for(review_id)}
    missing = [n for n in numbers if n not in by_number]
    if missing:
        raise UsageError(f"review #{review_id} has no finding {', '.join(map(str, missing))}")
    return [by_number[n] for n in numbers]


def _learn_later(finding_id: int, action: str) -> None:
    """Update memory in the background, as the web UI does, so the command returns at once."""
    subprocess.Popen(
        [sys.executable, "-m", "prelude.cli", "_learn", str(finding_id), action],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _cmd_learn(args: argparse.Namespace) -> int:
    from .memory_agent import learn_async

    cfg = load_config()
    learn_async(Store(cfg.db_path), cfg, args.finding_id, args.action).join()
    return 0


def _cmd_findings(args: argparse.Namespace) -> int:
    from .discuss import current_draft

    cfg = load_config()
    store = Store(cfg.db_path)
    r = store.get_review(args.review)
    if r is None:
        print(f"no review #{args.review}", file=sys.stderr)
        return 1
    print(f"#{r['id']} {r['owner']}/{r['repo']}#{r['number']} {r['title'] or ''}")
    shown = _findings(store, args.review, args.numbers) if args.numbers else store.findings_for(args.review)
    for f in shown:
        if f["status"] == "discarded" and not (args.numbers or args.all):
            continue
        loc = f"{f['path']}:{f['line']}" if f["line"] else f["path"]
        edited = ", your wording" if f["user_draft"] is not None else ""
        status = "auto-drafted, not checked yet" if f["status"] == "drafted" and f["auto_drafted"] else f["status"]
        print(f"\n{f['ordinal']:>2}. [{f['severity']}] {f['title']}  ({status}{edited})\n    {loc}")
        print(f"\n{textwrap.indent(current_draft(f), '      ')}")
        if args.numbers:
            print(f"\n    Why:\n{textwrap.indent(f['assessment'], '      ')}")
            for m in store.messages_for(f["id"]):
                print(f"\n    {m['role']}:\n{textwrap.indent(m['content'], '      ')}")
    return 0


def _cmd_draft(args: argparse.Namespace) -> int:
    from . import drafting
    from .drafts import DraftError, DraftWriter

    cfg = load_config()
    store = Store(cfg.db_path)
    writer = DraftWriter(cfg)
    chosen = (
        [f for f in store.findings_for(args.review) if f["status"] == "pending"]
        if args.all
        else _findings(store, args.review, args.numbers)
    )
    if not (args.all or args.numbers):
        print("error: give finding numbers, or --all", file=sys.stderr)
        return 2
    failed = 0
    for f in chosen:
        try:
            if why := triage.refusal(f):
                raise DraftError(why)
            if args.cmd == "draft":
                drafting.add(store, cfg, writer, f["id"])
                print(f"{f['ordinal']}: {'kept in' if f['auto_drafted'] else 'added to'} your GitHub draft")
            elif args.cmd == "undraft":
                if f["status"] != "drafted":
                    raise DraftError(f"not in your draft ({f['status']})")
                drafting.remove(store, cfg, writer, f["id"])
                print(f"{f['ordinal']}: back to review")
            elif f["status"] == "discarded":
                print(f"{f['ordinal']}: already discarded")
            else:
                drafting.remove(store, cfg, writer, f["id"], new_status="discarded")
                _learn_later(f["id"], "discard")
                print(f"{f['ordinal']}: discarded")
        except DraftError as e:
            failed += 1
            print(f"{f['ordinal']}: {e}", file=sys.stderr)
    return 1 if failed else 0


def _cmd_restore(args: argparse.Namespace) -> int:
    cfg = load_config()
    store = Store(cfg.db_path)
    for f in _findings(store, args.review, args.numbers):
        if f["status"] == "discarded":
            store.update_finding(f["id"], status="pending")
        print(f"{f['ordinal']}: {store.get_finding(f['id'])['status']}")
    return 0


def _cmd_edit(args: argparse.Namespace) -> int:
    from .discuss import current_draft
    from .drafts import DraftWriter

    cfg = load_config()
    store = Store(cfg.db_path)
    (f,) = _findings(store, args.review, [args.number])
    learn = _learn_later
    if args.reset:
        text = None
    elif args.claude:
        proposals = [m["proposal"] for m in store.messages_for(f["id"]) if m["proposal"]]
        if not proposals:
            print("Claude hasn't proposed new wording for this finding.", file=sys.stderr)
            return 1
        text, learn = proposals[-1], None  # Claude's words, not the reviewer's
    elif args.text is not None:
        text = args.text
    else:
        text = _edit_in_editor(current_draft(f))
        if text is None:
            print("No change.")
            return 0
    if error := triage.set_wording(store, cfg, DraftWriter(cfg), f["id"], text, learn=learn):
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(current_draft(store.get_finding(f["id"])))
    return 0


def _edit_in_editor(text: str) -> str | None:
    with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False) as tmp:
        tmp.write(text)
    path = Path(tmp.name)
    try:
        subprocess.run([*shlex.split(os.environ.get("EDITOR") or "vi"), str(path)], check=True)
        new = path.read_text().strip()
    except (OSError, subprocess.CalledProcessError) as e:
        raise UsageError(f"the editor didn't finish ({e}); nothing changed. Or use --text.") from e
    finally:
        path.unlink(missing_ok=True)
    return None if not new or new == text.strip() else new


def _cmd_discuss(args: argparse.Namespace) -> int:
    from .discuss import Broker, DiscussionBusy, post_message

    cfg = load_config()
    store = Store(cfg.db_path)
    (f,) = _findings(store, args.review, [args.number])
    broker = Broker()
    try:
        replying = post_message(store, cfg, broker, f["id"], args.message)
    except (ValueError, DiscussionBusy) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    events = broker.subscribe(f["id"])
    try:
        while (ev := events.get()).kind not in ("done", "error"):
            if ev.kind == "text":
                print(ev.data, end="", flush=True)
    except KeyboardInterrupt:
        store.release_thread(f["id"])  # the reply dies with this process; don't leave the thread busy
        print("\nStopped. Your message is saved; ask again to get a reply.", file=sys.stderr)
        return 130
    if ev.kind == "error":
        print(f"\nerror: {ev.data}", file=sys.stderr)
        return 1
    reply = store.messages_for(f["id"])[-1]
    print()
    if reply["proposal"]:
        print(f"\nClaude proposes:\n{textwrap.indent(reply['proposal'], '  ')}")
        print(f"Use it: prelude edit {args.review} {args.number} --claude")
    if replying.is_alive():
        print("Updating memory…", file=sys.stderr)
        try:
            replying.join()
        except KeyboardInterrupt:
            print("Skipped the memory update; the reply is saved.", file=sys.stderr)
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


def _cmd_daemon(args: argparse.Namespace) -> int:
    from . import daemon

    cfg = load_config()
    print({"start": daemon.start, "stop": daemon.stop, "status": daemon.status}[args.cmd](cfg))
    return 0


def _cmd_notify_setup(_args: argparse.Namespace) -> int:
    import re

    from .notify import new_topic

    cfg = load_config()
    if cfg.notify.ntfy_topic:
        print(f"Already set up. Subscribe to this topic in the ntfy app: {cfg.notify.ntfy_topic}")
        return 0
    topic = new_topic()
    path = cfg.data_dir / "config.yaml"
    text = path.read_text()
    if re.search(r"^  ntfy_topic:.*$", text, flags=re.M):
        text = re.sub(r"^  ntfy_topic:.*$", f"  ntfy_topic: {topic}", text, count=1, flags=re.M)
    else:
        text += f"\nnotify:\n  ntfy_topic: {topic}\n"
    path.write_text(text)
    print(f"In the ntfy app, tap + and subscribe to: {topic}\nThen run `prelude notify-test`.")
    return 0


def _cmd_notify_test(_args: argparse.Namespace) -> int:
    from .notify import send

    cfg = load_config()
    if not cfg.notify.ntfy_topic:
        print("No topic yet; run `prelude notify-setup` first.")
        return 1
    ok = send(cfg.notify, "PRelude", "Notifications work. Tap to open PRelude.", tags="tada")
    print("Sent." if ok else "Sending failed; see the log above.")
    return 0 if ok else 1


def _cmd_check(_args: argparse.Namespace) -> int:
    """Dry run of the triggers: what would be reviewed now, and why. Starts nothing."""
    from .poller import Poller

    cfg = load_config()
    poller = Poller(Store(cfg.db_path), cfg)
    found = poller.candidates()
    if not found:
        print("No open PRs match your triggers right now.")
    for (owner, repo, number), item in sorted(found.items()):
        _pr, ok, reason = poller.evaluate(owner, repo, number, item)
        print(f"{'REVIEW' if ok else 'skip  '}  {owner}/{repo}#{number}  {reason}")
    return 0


def _cmd_url(_args: argparse.Namespace) -> int:
    cfg = load_config()
    print(f"http://{cfg.server.host}:{cfg.server.port}/?key={cfg.access_key()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="prelude", description="Human-in-the-loop PR review")
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")

    s = sub.add_parser("review", help="review a PR now and print the findings")
    s.add_argument("pr", help="PR link (https://github.com/OWNER/REPO/pull/N) or OWNER/REPO#N")
    s.add_argument(
        "-n",
        "--reviewers",
        type=int,
        default=None,
        help="independent reviewers, merged by a meta-reviewer; 0 = by PR size (default: config)",
    )
    s.add_argument(
        "--again",
        choices=("full", "changes"),
        help="re-review after the last review: the whole PR again, or only the new commits",
    )
    s.set_defaults(fn=_cmd_review)

    s = sub.add_parser("show", help="print a stored review")
    s.add_argument("id", type=int)
    s.set_defaults(fn=_cmd_show)

    sub.add_parser("list", help="list reviews").set_defaults(fn=_cmd_list)

    s = sub.add_parser("findings", help="list a review's findings, or show some in full (with the discussion)")
    s.add_argument("review", type=int)
    s.add_argument("numbers", type=int, nargs="*")
    s.add_argument("--all", action="store_true", help="include discarded findings")
    s.set_defaults(fn=_cmd_findings)
    for name, help_text in (
        ("draft", "add findings to your pending GitHub review"),
        ("undraft", "take findings out of your GitHub draft"),
        ("discard", "discard findings (removes them from the draft if they're in it)"),
    ):
        s = sub.add_parser(name, help=help_text)
        s.add_argument("review", type=int)
        s.add_argument("numbers", type=int, nargs="*" if name == "draft" else "+")
        if name == "draft":
            s.add_argument("--all", action="store_true", help="every finding still to review")
        s.set_defaults(fn=_cmd_draft, all=False)
    s = sub.add_parser("restore", help="bring back discarded findings")
    s.add_argument("review", type=int)
    s.add_argument("numbers", type=int, nargs="+")
    s.set_defaults(fn=_cmd_restore)
    s = sub.add_parser("edit", help="change a finding's wording ($EDITOR, or --text)")
    s.add_argument("review", type=int)
    s.add_argument("number", type=int)
    how = s.add_mutually_exclusive_group()
    how.add_argument("--text", help="the new wording")
    how.add_argument("--claude", action="store_true", help="use Claude's latest proposed wording")
    how.add_argument("--reset", action="store_true", help="back to Claude's original wording")
    s.set_defaults(fn=_cmd_edit)
    s = sub.add_parser("discuss", help="ask Claude about a finding; prints the reply")
    s.add_argument("review", type=int)
    s.add_argument("number", type=int)
    s.add_argument("message")
    s.set_defaults(fn=_cmd_discuss)
    s = sub.add_parser("_learn")  # background memory update for the triage commands
    s.add_argument("finding_id", type=int)
    s.add_argument("action")
    s.set_defaults(fn=_cmd_learn)
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

    sub.add_parser("notify-setup", help="create an ntfy topic for phone notifications").set_defaults(
        fn=_cmd_notify_setup
    )
    sub.add_parser("notify-test", help="send a test notification").set_defaults(fn=_cmd_notify_test)
    sub.add_parser("check", help="dry run: which open PRs the triggers would review, and why").set_defaults(
        fn=_cmd_check
    )

    for name, help_text in (
        ("start", "run the web UI in the background until `prelude stop`"),
        ("stop", "stop a PRelude started with `prelude start`"),
        ("status", "is PRelude running?"),
    ):
        sub.add_parser(name, help=help_text).set_defaults(fn=_cmd_daemon)

    args = p.parse_args(argv)
    if args.cmd is None:  # plain `prelude` starts the web UI
        args = p.parse_args(["serve", *(argv or [])])
    try:
        return int(args.fn(args) or 0)
    except UsageError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
