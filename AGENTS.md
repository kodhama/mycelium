# AGENTS.md — Agent Operating Instructions

**This file is the tool-neutral instruction set for mycelium.** Claude Code
reads it through the `@AGENTS.md` import atop `CLAUDE.md`; every other agent
reads it directly.

mycelium is kodhama's agent communication layer: every agent knows its
manager, peers and reports, and gets its mail. It is a Python command line
with a SQLite store. Its plans live in the kodhama hub, not here: this repo
holds code, tests, schemas, release docs and decisions about its own code.

## Workflow

**Compound Engineering (CE) is the default route for planned work**: reach
for the CE skill whose description fits before hand-building what it already
does (`ce-work`, `ce-debug`, `ce-code-review`). **Plans are written in the
hub, never in this repo**: a pull request cites the hub plan's path and
revision. If a skill is absent, do not hand-roll a stand-in: say in your
hand-back that it was unavailable and that step did not run.

**Trellis** rules arrive at session start via the trellis plugin
(hook-driven); `.trellis/rules.toml` selects which are active, and its rows
govern. The rules live in the plugin, not here.

**Write for a reader who skims.** Applies to anything the maintainer reads:
chat, reports, PRs, issues and comments.

- First line = the point, and what the maintainer needs to do (if anything).
- Short paragraphs, one idea each. Bullets for several items. Bold the key phrase.
- One question at a time, recommendation first.

## Review loop

**Review runs before the PR, always, sized by scope**: every change gets an
in-session review on the branch before a PR opens. The native single-pass
reviewer (`/code-review` in Claude Code) is the floor; bigger scope escalates
to `ce-code-review` (report-only). **The author never rules its own work
correct**: an independent reviewer (a fresh session, or another model)
checks it and returns findings, which the author triages against the source.
Automated GitHub reviewers, where the repo has them, are the second round.
**Open a PR as a draft while fixes are still landing, and mark it ready when
the branch is final.**

## Checks that must stay green

`.github/workflows/test.yml` runs two jobs on every push to `main` and every
PR: `test` on Python 3.13, and `floor` on Python 3.11, the oldest that
`requires-python` allows.

| step    | command                         |
| ------- | ------------------------------- |
| lint    | `uv run ruff check`             |
| format  | `uv run ruff format --check`    |
| tests   | `uv run pytest`                 |
| floor   | `uv run --python 3.11 pytest`   |
| secrets | CI only (gitleaks)              |

Run them locally before pushing. A red is a real failure: fix it, never merge
past it. **mycelium calls no paid API**: nothing here needs a key, and no
check may add one.

## Where work lives

**Linear tracks the work (`GRO-*` in the kodhama workspace); GitHub hosts the
code**, its pull requests and CI.

**A comment an agent writes ends by saying so**: last line, own paragraph, as
literal plain text: `— Posted by <your tool name> (agent session) on behalf
of @<maintainer-handle>.` Comments only: a PR body is covered by its branch and
`Fixes GRO-<n>` line, commits by `Co-authored-by`.

## Decisions

Decision records live in `docs/decisions/<slug>.md` with frontmatter
(`type: decision`, `title`, `description`, `decided`, `ticket`). They cover
this repo's own code only, and cite the hub plan they follow. Records are
append-only: a change is a dated entry inside the record, never an edit to
an earlier one.

## Branch, commit, and PR rules

- **Branch `feature/gro-<n>-<slug>`**, the shape Linear generates.
- **Put `Fixes GRO-<n>` in the PR body** (or `Part of GRO-<n>` when the PR is
  one of several for the issue), and cite the hub plan's path and revision.
- **A story's PR body carries a `Capture:` line and a `Docs:` line**, each at
  the start of its own line, as `story-worker` gives them. To hand the body
  to the receipt check, save it with
  `gh pr view <n> --json body --jq .body > <file>` and pass `--pr-body <file>`.
- Conventional Commits; linear history (rebase-merge or squash with a
  hand-edited message).
- **Every change reaches `main` through a PR. The maintainer approves every
  merge**; an agent never merges.
- **The version is the `vX.Y.Z` tag**, read by hatch-vcs; nothing in the tree
  carries it. The maintainer cuts the tag after merge.
- **`README.md` is the doc a change keeps current**, and `docs/decisions/`
  where the change makes or supersedes a decision about this repo's code.
- Code changes are test-first: failing test, then implementation, then green.
- Keep diffs reviewable from a phone: small, focused, well-described.

## What you must NOT do

- Do not invent requirements or broaden focused work: requirements are
  measured against the hub plan and the issue you were given.
- Do not add a dependency without recording the decision under
  `docs/decisions/`. The runtime stays standard-library only.
- Do not commit a secret, a token, a real home path or personal data. This
  repo is public. Tests and fixtures use synthetic names such as `role-01`
  and placeholder paths such as `/home/maintainer`.
- mycelium itself writes its state only in the XDG folders `mycelium.paths`
  resolves, never in `/tmp`. Tests and CI scratch space are not state.
