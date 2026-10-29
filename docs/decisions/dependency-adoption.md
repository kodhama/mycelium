---
type: decision
title: "Dependency adoption"
description: Why each dependency mycelium carries was adopted, its scope, and what was turned down for it.
decided: 2026-10-29
ticket: GRO-47
---

# Dependency adoption

**mycelium has no runtime dependency.** It runs on Python 3.11 or later with
the standard library only (hub plan KTD11, at
`areas/agent-comms/plans/2026-10-27-feat-mycelium-plan.md`, revision
`c9b114f5`). Everything below is used only to build, test or lint mycelium.
None of it is imported at runtime.

Add a dependency only with a dated decision here (`AGENTS.md`).

**2026-10-29 decision: hatchling and hatch-vcs** (build only). `uvx --from
git+…` and the kodhama CLI build mycelium from a git checkout, so the package
needs a build backend. With hatch-vcs, **the `vX.Y.Z` tag is the only place
the version lives**. A tagged checkout builds as `X.Y.Z`. Any other commit
builds as a `.devN+g<sha>` version, so an untagged build is never mistaken
for a release. Turned down: `uv_build` with a static version in
`pyproject.toml`. It would be a second copy of the version, bumped by hand
before each tag. Cost: hatch-vcs reads git history, so CI checks out with
`fetch-depth: 0`, and a source archive without `.git` cannot be built.

**2026-10-29 decision: pytest** (development only, `>=8`). `uv run pytest`
is the plan's test gate (Verification Contract). The store tests start real
processes with `multiprocessing`, which pytest runs without extra plugins.

**2026-10-29 decision: ruff** (development only, `>=0.6`). `uv run ruff
check` is the plan's lint gate. One tool covers lint and import order.
