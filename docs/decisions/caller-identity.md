---
type: decision
title: "Caller identity: the process walk, Codex's thread id, and a read-only pane module"
description: How mycelium tells which session called it, when that stays unknown, and how it reads herdr panes.
decided: 2026-11-09
ticket: GRO-49
---

# Caller identity

**mycelium tells its caller from the process tree, never from an argument
(KTD5).** `mycelium.identity` walks up from the calling process to the
nearest harness. `mycelium.pane` is the one module that calls herdr (KTD19).
This follows hub plan unit U4, its U4a half, and KTD4, KTD5 and KTD19
(`areas/agent-comms/plans/2026-10-27-feat-mycelium-plan.md` at `ef44555`),
plus the U2 probes (`areas/agent-comms/research/probes-u2.md`: S3, and
consequences 1, 2 and 8). Bindings, the directory and `whoami` build on
these two modules in U4b.

## 2026-11-09 decision: how a caller resolves

The walk reads one `ps -axww -o pid=,ppid=,comm=` snapshot. It then climbs
from the caller's parent to the nearest process whose name is `claude`,
`codex`, `droid` or `opencode`. Names are basenames, matched case-sensitively,
so the desktop app `Claude` is not a harness.

- **Claude:** the harness is that `claude` pid. The session id is
  `CLAUDE_CODE_SESSION_ID`, taken only when `CLAUDE_PID` names that same pid.
  A nested Claude sets both variables afresh. Any other value was inherited,
  so the session stays unknown (probes S3 Claude).
- **Codex under the shared app-server daemon** (its `comm` path holds
  `app-server-daemon`): this is a Codex TUI's tool call. The session is
  `CODEX_THREAD_ID`, which the 2026-10-29 ruling counts as KTD5's fallback.
  The pid is unknown, because the daemon is shared and proves no one
  session's liveness. Nesting is unknown too: the daemon's ancestors belong
  to whichever TUI started it (consequences 1 and 2).
- **Any other Codex** (`codex exec`): its own pid, and `CODEX_THREAD_ID`.
- **Droid or opencode:** unbound. U4 binds Claude and Codex only.
- **Nested:** another harness sits above the caller's own harness. The daemon
  exemption applies only when the caller's own nearest harness is the daemon.
  A `codex exec` started from a Codex TUI's tool has another session's daemon
  above it, so it is nested (lead ruling, GRO-49).
- **No process table** (`ps` denied in Codex's sandbox, missing, or timed
  out): `CODEX_THREAD_ID` resolves the caller as Codex, with pid and nesting
  unknown. Without it, the caller is unbound.
- **No harness ancestor:** unbound.

**Claude's hook-written token** (KTD5's other fallback, through
`CLAUDE_ENV_FILE`) is not read here. It lands in U8 with the hook that writes
it (lead ruling, GRO-49, recorded in the plan's U8). Without a walk, an own
token can't be told from one a nested child inherited.

**Known limits:**

- **An inherited thread id.** A Claude started inside a Codex session
  inherits `CODEX_THREAD_ID`. Where `ps` is denied, that Claude reads as
  Codex.
- **The daemon is recognised by its path.** The path comes from the U2
  probe on macOS. Linux `ps -o comm=` prints a short name, so on Linux a
  Codex TUI's tools would read as a `codex exec`.
- **pid reuse.** One snapshot can't hold a reused pid. Across time, a stored
  Claude pid that now belongs to another session is told apart by the
  session id the walk returns. U4b's binding compares the two.

## 2026-11-09 decision: herdr 0.9.3, read only

Probed live against herdr 0.9.3:

- **Pane records:** `pane get` and `pane current` answer with the pane id,
  `agent`, `agent_session.value` (the harness's session id),
  `terminal_title_stripped`, `cwd` and `terminal_id`. A pane record carries
  no name.
- **Agent records:** `agent get` adds `name`, the live agent name. herdr
  holds no record for a pane without an agent, and lessons C2 says the
  record may not exist yet at SessionStart.
- **Process info:** `pane process-info --pane <id>` lists the foreground
  processes, each with `pid`, `name`, `argv` and `cwd`, plus `shell_pid`.
  This is the T9 check's source. A pane herdr could not restore (it carries
  `restore_error`) answers as not found.
- **Replies:** a success is JSON on stdout with exit 0. An error is JSON on
  **stderr** with exit 1, such as `pane_not_found` or `agent_not_found`.

`mycelium.pane` exposes the title and the agent name side by side. Which one
counts as the herdr name is U4b's launch-name rule. It accepts an
`HERDR_PANE_ID` hint only when that pane's foreground holds the pid, and
otherwise scans the panes. It reports `terminal_id` but never keys on it,
since herdr issues new ones at reboot (lessons C1). Typing into a pane is
U12's.
