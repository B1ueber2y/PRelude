# PRelude

**Review the AI review comments before they reach the author.**

AI reviewers are good at finding problems and bad at knowing which ones matter to *you*.
Posting their output straight onto a pull request is noisy; pasting it into one long chat
is unmanageable. PRelude treats an AI review the way we treat code: as a proposal that
gets reviewed.

Claude reads the pull request and proposes review comments. You are the meta-reviewer:
like an area chair weighing a referee report, you go through each comment on your phone,
push back where it's wrong, and decide what the author actually sees. It works the way
you'd review a change:

| Code review              | PRelude                                              |
| ------------------------ | ---------------------------------------------------- |
| A contributor opens a PR | Claude proposes a set of review comments             |
| Approve                  | **Add** the comment to your GitHub draft review      |
| Suggest changes          | **Edit** the wording                                 |
| Comment thread           | **Discuss** it with Claude, one thread per comment   |
| Reject                   | **Discard** it                                       |
| Release                  | You submit the review on GitHub                      |
| The contributor improves | PRelude **remembers** your principles and style      |

Nothing is published by PRelude. Accepted comments land in your *pending* GitHub review;
you read them in place and submit it yourself (Comment / Approve / Request changes).
Optionally, each comment is prefixed with `AI review with PRelude:` so authors know where it came from.

## Built on Claude Code

PRelude is a harness around [Claude Code](https://claude.com/claude-code), not a model or
reviewer of its own. Every review, discussion and memory update is a headless run of the
official `claude` CLI, using your own Claude Code login (a Claude subscription); PRelude
supplies the prompts, the read-only sandbox and the workflow around the results:

- **Claude Code does the thinking:** reading the PR and the surrounding code, finding
  problems, drafting comments, and answering when you push back.
- **PRelude does everything else:** deciding what to review, checking out the PR read-only,
  running independent reviewers and merging their reports, keeping one conversation per
  finding (forked Claude Code sessions), learning your preferences, the phone UI, and
  writing drafts to GitHub only when you say so.

So review quality follows Claude Code's, and usage counts against your Claude plan.

## What it does

- **Investigates, not just diffs.** Claude explores the repository around the change
  (callers, tests, config) in a read-only checkout and anchors each finding to a line.
- **A panel, not one opinion.** Optionally run several independent reviewers, each with
  a different focus, and a meta-reviewer that merges their reports, checks disputed
  claims against the code, and records how many reviewers raised each finding.
- **One thread per finding.** Challenging one comment never muddles another. Each
  discussion continues from the review's context, so Claude doesn't start over.
- **Your words stay yours.** Claude's suggestion and your edited wording are stored
  separately; Claude can propose new wording but never overwrites yours.
- **Learns how you review.** Review principles (what to flag, what to skip), a comment
  style guide (how you phrase things) and per-repo notes. They update as you discard,
  edit and discuss — like a colleague who learns from your feedback — and every change
  can be undone. `prelude style-init OWNER/REPO` seeds the style from your past comments.
- **Built for the phone.** A small installable web app, served privately from your own
  machine, with notifications when a review is ready.
- **Reviews on its own, while you let it.** Watch repos for new PRs or review requests
  (`prelude start` … `prelude stop`); nothing runs unless you started it.

Status: early, but in daily use. Reviews (single or panels), editing, discarding,
discussing, memory, GitHub drafts (off until enabled in `config.yaml`), automatic review
triggers and phone notifications (via ntfy) all work.

## Requirements

- Python 3.12+
- [Claude Code](https://claude.com/claude-code) logged in with a Claude subscription (`claude auth status`)
- GitHub CLI logged in (`gh auth status`)

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/prelude auth        # checks the Claude login PRelude will use
```

Data (database, checkouts, memory, config) lives in `.prelude-data/` unless `PRELUDE_HOME`
is set. Edit `.prelude-data/config.yaml` to change settings; `only_repo` restricts PRelude
to a single repository during development.

## Usage

```bash
.venv/bin/prelude review https://github.com/OWNER/REPO/pull/123   # review from the terminal
.venv/bin/prelude review -n 3 OWNER/REPO#123                     # 3 reviewers + meta-reviewer
.venv/bin/prelude                                                # run the web UI (127.0.0.1:8787); Ctrl+C stops it
.venv/bin/prelude start | stop | status                          # the same, in the background
.venv/bin/prelude check                                          # dry run: which PRs the triggers would review
.venv/bin/prelude notify-setup                                   # phone notifications via the ntfy app
.venv/bin/prelude url                                            # link that unlocks the UI on a device
.venv/bin/prelude style-init OWNER/REPO                          # learn your comment style
```

Open the `prelude url` link once on each device; it sets a cookie. PRelude only listens on
localhost; to use it from a phone, forward the port over SSH (for example a local port
forward in an SSH app such as Termius) or another private tunnel such as `tailscale serve`.

## Safety

- Claude runs headlessly with read-only tools (`Read`, `Grep`, `Glob`) in a read-only
  checkout, with settings, hooks, plugins and MCP servers disabled, so code in the
  reviewed repository is never executed. API-key variables are removed from its
  environment, so runs always use the subscription login.
- GitHub writes are limited to the reviewer's own pending review (add, edit, delete a
  draft comment); submitting a review is not possible from PRelude, and tests enforce it.

## Development

```bash
.venv/bin/pytest
scripts/format.sh        # ruff format + lint fixes; CI checks both
```
