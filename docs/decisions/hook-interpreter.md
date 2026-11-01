---
type: decision
title: "One interpreter and one command for hook launches"
description: Which Python a hook runs mycelium on, and the exact command it uses.
decided: 2026-10-29
ticket: GRO-47
---

# One interpreter and one command for hook launches

**A hook runs mycelium on Python 3.13, with this command:**

```sh
uvx --python 3.13 --from git+https://github.com/kodhama/mycelium@<full commit sha> mycelium <args>
```

**Why pin the interpreter.** Without `--python`, uv picks the newest Python
it can find. On the machine U1 measured, that was 3.14.6, not 3.13 (hub plan
U1, at `areas/agent-comms/plans/2026-10-27-feat-mycelium-plan.md`, revision
`c9b114f5`). A hook whose interpreter changes when someone installs a new
Python is not reproducible. 3.13 is inside the supported range (3.11 or
later), and CI tests 3.11 as the floor.

**Why pin a commit, not a tag.** With a tag ref, uv asks git where the tag
points on every run. With a full commit SHA, a warm run reuses its cached
environment and does no git work. Measured warm on a local repository:
about 0.13 s with a SHA, against about 0.22 s with a tag. The kodhama CLI's
lock records the commit a tag resolved to, so an installer has the SHA to
write. The timing against GitHub is recorded in the pull request that
introduced this decision.

**Cost.** uv adds about 10 ms per run over calling the interpreter directly
(U1). It also keeps a cached environment under its own cache folder.
