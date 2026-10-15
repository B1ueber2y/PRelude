# PRelude

**Meta-review a panel of independent AI code reviewers before their comments reach the author** (a harness around [Claude Code](#built-on-claude-code)).

<p align="center"><strong>Laptop</strong><br>
  <img src="docs/images/laptop.png" width="780" alt="A review on a laptop: the findings listed on the left, the selected one in full on the right">
</p>
<p align="center"><strong>Phone</strong><br>
  <img src="docs/images/home.png" width="32%" alt="The review list, one card per PR">
  <img src="docs/images/review.png" width="32%" alt="A review: summary, actions and the first finding with its code">
  <img src="docs/images/finding.png" width="32%" alt="One finding: the code, why Claude flagged it, and your wording">
</p>

AI reviewers are good at finding problems and bad at knowing which ones matter to *you*.
Posting their output straight onto a pull request is noisy; pasting it into one long chat
is unmanageable. PRelude treats an AI review the way we treat code: as a proposal that
gets reviewed.

Claude reads the pull request and proposes review comments. You are the meta-reviewer: you
go through each comment, push back where it's wrong, and decide what the author actually
sees. It works the way you'd review a change:

| Code review              | PRelude                                              |
| ------------------------ | ---------------------------------------------------- |
| A contributor opens a PR | Claude proposes a set of review comments             |
| Approve                  | **Add** the comment to your GitHub draft review      |
| Suggest changes          | **Edit** the wording                                 |
| Comment thread           | **Discuss** it with Claude, one thread per comment   |
| Reject                   | **Discard** it                                       |
| Release                  | You add your own comments and submit on GitHub       |
| The contributor improves | PRelude **remembers** your principles and style      |

Nothing is published by PRelude. Comments land in your *pending* GitHub review, which only
you can see. New comments start there and you discard the ones you don't want (with
`github_drafts.auto: false` they wait for you to add them). Then, on GitHub, you add any
comments of your own and submit the review yourself (Comment / Approve / Request changes).
Optionally, each comment is prefixed with `AI review with PRelude:` so authors know where it came from.

## Ways to use it

PRelude fits wherever you'd want a careful second reviewer:

- **Review your own PRs before anyone else does.** Open a PR and have PRelude review it
  (paste the link, or turn on `triggers` and your own non-draft PRs are reviewed
  automatically). Fix what's right, push, tap **Review again → Changes only**, and repeat
  until the re-review comes back clean. This pairs well with a coding
  agent: one Claude writes the change, another reviews it, and you judge both. PRelude is
  developed this way: every PR in this repo went through several such rounds.
- **Get a second opinion on a risky change.** Ask for a panel (3 or 5 reviewers, or add
  more later with **+N**) and see which problems several reviewers found independently.
- **Answer review requests faster.** With `triggers` on, when a colleague requests your
  review, a draft is waiting by the time you look; you keep, reword or drop each comment
  and submit.
- **First pass on contributions to a project you maintain.** Turn on `triggers`, add your
  repos on the **Repos** page, and PRelude prepares a review of each new PR; contributors
  only see what you approved.

All of it works from the phone, a laptop browser, or the terminal (`prelude findings`,
`prelude draft`, `prelude discard`, … see [From the terminal](#from-the-terminal)).

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
  writing comments only into your private pending GitHub review.

So review quality follows Claude Code's, and usage counts against your Claude plan.

### Use within Anthropic's terms

PRelude is an independent project, not made or endorsed by Anthropic, and this section is
not legal advice. Read the current [Consumer Terms](https://www.anthropic.com/legal/consumer-terms),
the [Claude Code legal and compliance](https://code.claude.com/docs/en/legal-and-compliance)
page and your plan's help pages yourself; they changed several times in 2026. As of
October 2026:

- **Scripted use of your own subscription is documented.** The Consumer Terms forbid
  automated access "except when you are accessing our Services via an Anthropic API Key or
  where we otherwise explicitly permit it". Anthropic's own docs show subscription-backed
  scripted use: `claude setup-token` is "for CI pipelines and scripts", the GitHub Actions
  guide reviews every new PR on a subscription token, and the help center says `claude -p`
  can be used "with your subscription limits". PRelude does the same on your machine: it
  runs the unmodified `claude` CLI under your own login and never reads or stores your
  subscription login (in API-key mode it keeps only the key you give it).
- **Don't run it for other people.** Subscriptions may not be shared or used "on behalf of"
  other users, and products built on Claude should use an API key. Keep PRelude personal:
  your login, your machine, your reviews. For heavier or team use, switch to an
  [API key](#subscription-or-api-key) (billed per token) and check with Anthropic.
- **Keep it ordinary, individual usage.** Pro and Max limits "assume ordinary, individual
  usage", and Anthropic can cap heavy or always-on background use. Automatic reviews are
  off by default; when you turn them on, keep `max_reviews_per_day` and reviewer panels
  modest. PRelude doesn't retry when Claude reports a usage limit.

## Install and run

### 1. Install

You need Python 3.12+, [Claude Code](https://claude.com/claude-code) logged in with your
Claude subscription (`claude auth status`; or an Anthropic API key, see below) and the
GitHub CLI logged in (`gh auth status`). Then:

```bash
git clone https://github.com/B1ueber2y/PRelude.git && cd PRelude
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/prelude auth        # checks the Claude login PRelude will use
```

Data (database, checkouts, memory, config) lives in `.prelude-data/` unless `PRELUDE_HOME`
is set; settings are in `.prelude-data/config.yaml`. To try PRelude on a single repo
first, set `only_repo: OWNER/REPO` there; it then refuses every other repository.

### 2. Start it

```bash
.venv/bin/prelude start       # in the background; `prelude stop` / `prelude status`
.venv/bin/prelude             # or in the foreground; Ctrl+C stops it
```

PRelude serves its web UI on `127.0.0.1:8787` only, and runs nothing until you start it.

### 3. Open it on a computer

Run `.venv/bin/prelude url` and open the link it prints
(`http://127.0.0.1:8787/?key=…`). It sets a cookie for that address, so afterwards
`http://127.0.0.1:8787` is enough in that browser (`localhost` counts as a different
address). On a laptop the review page is a split view: the findings on the left, the
selected one on the right; `j`/`k` move between them and `a`/`e`/`d` add, edit or discard.

If PRelude runs on another machine (say a desktop at work), forward the port from your
laptop and use the same link:

```bash
ssh -L 8787:127.0.0.1:8787 you@that-machine
```

### 4. (Optional) Set up your phone

1. **Reach it privately.** PRelude never listens on the network. Forward the port from the
   phone: in an SSH app such as Termius, add the machine as a host and a *local port
   forward* from 8787 to `127.0.0.1:8787` (keep-alive on helps).
2. **Unlock it once.** Open the `prelude url` link in the phone's browser.
3. **Add it to the home screen.** iPhone: Safari → Share → *Add to Home Screen*. Android:
   Chrome → ⋮ → *Add to Home screen*. It then opens like an app.
4. **Notifications (optional).** Run `.venv/bin/prelude notify-setup`, subscribe to the
   topic it prints in the [ntfy](https://ntfy.sh) app, and check with
   `.venv/bin/prelude notify-test`. You get a notification when a review is ready;
   `notify.details` in `config.yaml` sets how much it says (ntfy.sh is a public server).

### 5. Drafts and automatic reviews

- **GitHub drafts** are on out of the box: new findings start in your pending GitHub review,
  which only you see until you submit it. Set `github_drafts.auto: false` to add them
  yourself, or `github_drafts.enabled: false` to keep PRelude from writing to GitHub at all.
- **Limited automatic reviews:** set `triggers.enabled: true` and restart; then choose repos
  on the **Repos** page. They're capped at `triggers.max_reviews_per_day` (20 by default,
  over a rolling 24 hours); the home and Repos pages show how much of the cap is used and
  when the next slot frees up. Your own PRs in repos listed under `triggers.cap_exempt`
  don't count. `prelude check` shows what would be reviewed, without starting anything.

### Subscription or API key

By default PRelude runs on your Claude Code login (`claude.auth: subscription`): reviews
use up part of your plan's limits and cost nothing extra. To use an Anthropic API key
instead, which is billed per token in the Claude Console and recommended for heavy use:

```bash
.venv/bin/prelude api-key     # paste the key (hidden); stored in .prelude-data/ readable only by you
```

then set `auth: api_key` under `claude:` in `config.yaml` and restart PRelude
(`prelude auth` shows which one is in use). An `ANTHROPIC_API_KEY` in PRelude's own
environment takes precedence over the stored key. Use a key made just for PRelude, with a
spending limit set in the Console. Some plans include monthly API credits that cover
`claude -p` with a key from your linked Console organization; check your plan.

## From the terminal

Everything also works without the web UI; `N` is the number `prelude findings` shows:

```bash
.venv/bin/prelude review https://github.com/OWNER/REPO/pull/123   # review now and print the findings
.venv/bin/prelude review -n 3 OWNER/REPO#123                     # 3 reviewers + meta-reviewer
.venv/bin/prelude review --again changes OWNER/REPO#123          # re-review only the new commits
.venv/bin/prelude findings 32            # list a review's findings (add numbers for the full text)
.venv/bin/prelude draft 32 1 3           # add to your GitHub draft (--all: every one still to review)
.venv/bin/prelude discard 32 2           # discard (also takes it out of the draft)
.venv/bin/prelude edit 32 1              # reword in $EDITOR (--text "...", --claude, --reset)
.venv/bin/prelude discuss 32 1 "Is this really reachable?"
.venv/bin/prelude style-init OWNER/REPO  # learn your comment style from your past comments
```

## Safety

- Claude runs headlessly with read-only tools (`Read`, `Grep`, `Glob`) in a read-only
  checkout, with settings, hooks, plugins and MCP servers disabled, so code in the
  reviewed repository is never executed. Only the credential you chose reaches it: on the
  subscription, API-key variables are removed; with `claude.auth: api_key`, only that key
  is passed (in its environment, never on the command line), and any verbatim copy of it
  is removed from errors and from everything a run returns. Claude can still read its own
  environment and files, as it can your subscription login, so a PR written to trick it
  could try to get the key out in some disguised form; use a dedicated key with a spending
  limit, and you still read every comment before you submit it.
- GitHub writes are limited to the reviewer's own pending review (add, edit, delete a
  draft comment); submitting a review is not possible from PRelude, and tests enforce it.

## Development

```bash
.venv/bin/pytest
scripts/format.sh        # ruff format + lint fixes; CI checks both
```
