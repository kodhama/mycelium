@AGENTS.md

# CLAUDE.md — Claude Code specifics

Everything above arrives from `AGENTS.md`, the tool-neutral instruction set,
which applies here unchanged; this file adds only what depends on Claude Code.

Do not put `permissions.defaultMode` in `.claude/settings.json`: a project
value shadows the maintainer's user-level one.
