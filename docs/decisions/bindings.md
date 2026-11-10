---
type: decision
title: "Bindings, the directory and whoami"
description: How a session binds to a role, when it is current, what whoami prints and exits, and how the directory computes deliverability.
decided: 2026-11-10
ticket: GRO-50
---

# Bindings, the directory and whoami

**Each agent session is bound to its role, or told plainly that it isn't,
and the directory says who is reachable.** This follows hub plan unit U4, its
U4b half, with R5, R36, R37 and KTD3-KTD6
(`areas/agent-comms/plans/2026-10-27-feat-mycelium-plan.md` at `c7b1b80`,
which records the U4b rulings), the lead's rulings on GRO-50, and grove's lease
(`skills/supervision-setup/scripts/lease.mjs` at `7d054e2`). It builds on the
caller walk and the pane module ([caller identity](caller-identity.md)).

## 2026-11-10 decision: the pieces

- **`boot`** reads the boot id from the source grove's claim uses: macOS
  `sysctl -n kern.bootsessionuuid` (run as `/usr/sbin/sysctl`), Linux
  `/proc/sys/kernel/random/boot_id`. Without one, nothing binds, as grove
  acquires nothing without one.
- **`lease`** reads grove's claim and never writes it. `lease.mjs` writes
  `{state, nonce, resumer, boot, expires, pane, session}`. `expires` is a
  `toISOString` timestamp (`2026-11-10T12:10:00.000Z`) while `pending`, and
  null once `live`; `pane` and `session` are null until adoption. A claim
  missing its state, nonce or boot id, or a pending one whose expiry is not
  such a timestamp, is `unreadable`, as grove's `readClaim` judges it.
- **`launch`** decides the launch name (R2, R3): the harness session name
  (passed in by the hook), `KODHAMA_AGENT`, and herdr's live agent name.
- **`bindings`** is the store and the rules: `bind` (the hook's, in U8 and
  U11), `adopt`, `resolve_caller`, `authorize`, `touch` (U8's
  UserPromptSubmit hook's) and `live_bindings`.
- **`directory`** computes each name's row.
- **The CLI** adds `whoami`, `adopt <nonce>` and `directory`.

**The bindings file** is `state/bindings.sqlite3`. It opens under the
message store's rules, now shared in `mycelium.store.files`: owner-only
folders and files, a symlink refused, a newer schema refused before any
write, and WAL with `synchronous=NORMAL`. One row per (harness, session id)
records the name, `declared`, the pid, the evidence the identity rested on
(`walk` or `codex-thread-id`), the boot id, the generation, the state and its
reason, the adopted nonce, and the bound and last-seen times. A second table
keeps each name's last generation, so generations never repeat.

**Where the claim is filed.** `lease.mjs` takes `home` and `name` from its
caller. The kodhama hub's cluster spec (`specs/suite/clusters.md` at
`f309590`, hub #66) fixes both:

- **§1:** the claim is `<home>/.kodhama/grove/local/<name>/claim`. `<home>`
  is the cluster's home, the folder that holds `.kodhama/`, and `<name>` is
  the agent's declared name, never its effective name.
- **§3:** an effective name is never split to find the declared one. The
  declared name comes from `cluster.toml`, and the suffix from
  `org/local/instance.toml`.

grove's owner confirmed this on GRO-50. So mycelium reads
`<slice.home>/.kodhama/grove/local/<declared_name>/claim`
(`bindings.claim_reader`). The index's slice already carries the declared
name, and a test pins the suffixed case.

## 2026-11-10 decision: the launch name

- **The herdr name counts only when it is the session's own.** The pane's
  foreground must hold the walk's harness pid (lessons T9), and the pane's
  agent session must be the walk's session (lessons C2). The pane title never
  counts.
- **Every source present must agree.** None gives no name, and a
  disagreement gives no name (R3). `@maintainer` from any source is refused
  (R35).
- **A herdr that can't be read is unknown, never a match.** The other sources
  then decide. A caller with no pid, such as a Codex TUI, isn't looked up.
- **One deadline** covers the pane scan and both identity reads.
  `pane.read_identity` now shares its timeout across its two calls.

## 2026-11-10 decision: binding

- **A SessionStart re-confirms a session that continues** (KTD4). A session
  continues when its binding is from this boot and the SessionStart is its
  own hook re-run in the same process (hooks installed twice), or its
  `resume` or `compact` in any process, as when herdr restores a pane with
  `claude --resume <id>` under a new pid. A re-confirm:
  - keeps the state, the generation and the nonce;
  - takes the caller's pid and evidence, and sets `last_seen`;
  - applies G2 (below), which can unbind it;
  - supersedes the other sessions of the caller's own process, since one
    process runs one session.
- **These are decided again instead**, as a new session would be:
  - a binding from an earlier boot;
  - an unbound session, unless it is unbound as a fork or as nested. So a
    session unbound for no launch name, for names that disagree or now
    disagree (G2), for a nonce mismatch, or for a walk-verified holder is
    decided again at each SessionStart. herdr's record may not exist yet at
    the first one (lessons C2);
  - a provisional session. Provisional means a lease exists (KTD6); once it
    has ended, the hook binding follows newest-wins. While the lease holds,
    it stays provisional and supersedes nothing;
  - a superseded session whose name a session of the caller's own process
    (same pid and boot) now holds: a `/resume` back within one process
    (lessons G3). It becomes current with a new generation, and the session
    it left is superseded.
- **So a resume in a new process** leaves a current session current with
  its generation and nonce: a lease holder keeps its lease, and a later
  `/clear` in the new process inherits it. A superseded session stays
  superseded and supersedes nothing, so it can't take its role back (R4). A
  session superseded by another process stays superseded even in its own
  process. A fork or a nested session stays unbound until it adopts the
  nonce.
- **Generations, per name.** A binding that becomes current takes the name's
  next generation after a fresh start, a `/clear`, or a `/resume` onto
  another session in the same pid (lead ruling, G3). Adoption also takes the
  next one. A resume or compaction of the same session keeps its own, even in
  a new process, as when herdr restores a pane with `claude --resume`.
- **States:** `current`, `provisional`, `superseded`, and `unbound` with a
  reason: no launch name, names disagree, nested, fork, nonce mismatch,
  `@maintainer`, or a walk-verified holder (below).
- **Supersede.** A binding that becomes current supersedes the name's other
  current or provisional bindings. It also supersedes any other session of
  its own process, since one process runs one session. `bind` and `adopt`
  return each superseded binding once, at the moment it changes, so the
  notice can go out once. Sending it is U5's, as mail. **Until U5, nothing
  tells the superseded session**: the CLI's `adopt` reports none, and `bind`
  has no caller before the hooks (U8, U11).
- **Under a lease the nonce wins** (KTD6). A hook binding is provisional, and
  `adopt <nonce>` with the claim's nonce makes it current. "A lease exists"
  means a claim from this boot that is pending and unexpired, or live with a
  live binding of the name for the claim's session or nonce. mycelium judges
  that from its own bindings, not herdr. An expired, earlier-boot or
  dead-session claim gives newest-wins.
- **Choices the plan left open:**
  - An **unreadable claim** gives newest-wins: a claim no session can adopt
    must not block the name.
  - A **`/clear` or `/resume` in the process that holds the lease** keeps
    the lease: the new session inherits its nonce and is current.
  - **`adopt`** accepts a pending, unexpired claim, or a live one held by
    this session or another session of its process. It refuses a wrong nonce,
    which unbinds a provisional caller ("nonce mismatch"), an earlier-boot
    claim, an expired one, and a live one another session holds.
  - A session decided again **keeps its generation only if it is still
    current under the same name** and nobody has taken a newer one. One that
    turns current from unbound or provisional takes the name's next
    generation. A generation belongs to its name: a session decided under
    another name never carries the old one.
- **Forks and nested sessions** bind unbound unless they adopt the nonce
  (KTD4).

## 2026-11-10 decision: liveness (KTD3)

Liveness is computed, never stored.

- A binding from another boot is dead.
- With a pid, it is live while that pid runs under the harness's name, read
  from one `ps` call.
- A pid-less binding, or any binding when `ps` can't run, is live as
  `unverified` for its boot until it is superseded.

## 2026-11-10 lead rulings (GRO-50)

**G2: a later SessionStart whose launch names now differ.** At a re-confirm
(a `resume` or `compact` of the same session, or its hook re-run), if any
launch source now names someone other than the binding's name, the binding
becomes unbound, with the reason "launch names now disagree" (R3). The role
turns absent, and R13 alerts. A `/clear` is not a re-confirm: it starts a
new session id, which is decided. The next SessionStart decides a G2-unbound
session again, so it binds once its names agree. **Known limit: a rename
between SessionStarts goes unseen.** Nothing checks until the next
SessionStart.

**A caller with unknown nesting** (`nested` None: a Codex caller with no walk
evidence, or a Codex TUI). It may bind, but **it never supersedes a live
binding the walk verified**, one with a known pid. It may supersede pid-less
or dead bindings, so a Codex TUI can restart in place. Otherwise it binds
unbound. Each row records its evidence. `bindings.may_supersede` holds this
decision alone. **Known limit:** a ps-denied `codex exec` under a bound Codex
TUI can't be told apart from that TUI. Plan U11 probes it.

**`whoami`.**

- **Line 1 is the name exactly**, with no padding or decoration, and line 2
  is `generation <n>`. `--json` prints the full record instead.
- **Consumers compare line 1 only**, stripping one trailing newline. Spec §9e's
  helper compares it with `KODHAMA_AGENT`, and grove's test stub prints the
  name alone.
- **Exit codes:**
  - **0**: the caller is current.
  - **1**: refused: provisional, superseded or unbound. The reason goes to
    stderr, and nothing to stdout.
  - **2**: a usage error, such as an unknown command or bad arguments.
  - **3**: mycelium could not tell: the bindings store or the state folder
    could not be read, or failed once open (a locked database, an I/O
    error), or the boot id is unknown. `whoami` reads neither the index nor
    the cluster helper.

**`adopt <nonce>`** prints what `whoami` prints, line 1 the name and line 2
the generation, and uses the same codes. It also exits 3 when the index or
the cluster helper can't be read, since it reads the index to find the
claim. It exits 1 for a caller with no binding yet, no lease to adopt, a
wrong nonce, a claim from an earlier boot, an expired pending claim, or a
live claim another session holds.

**`directory`** exits 0, 2 or 3. It exits 3 when the index, the cluster
helper, the bindings store or the message store can't be read, or the boot
id is unknown: without the boot id every binding would read dead, and every
running role absent.

**`whoami` across an in-place `/clear`** (the lead's pin). The pid, pane and
name stay the same, but the session id is new. Until the clear's
SessionStart hook binds that id, `whoami` exits 1 with
`unbound: no binding for this session yet`. **It reads `unbound`, not
`provisional`.** `provisional` is a state of a binding that exists, waiting
for its lease nonce. A session id with no row has no binding at all. Once the
hook binds it with source `clear`, `whoami` exits 0 with the same name and
the next generation (KTD4), and stays at 0. The exit 1 lasts only until the
hook has run.

**Resolution (KTD5).** The walk finds the caller. A Claude caller matches
its binding by pid and session id, so a reused pid never resolves to another
process's binding. A Codex caller matches by thread id. A caller with no
harness ancestor is unbound.

**Not in U4b** (placed in U9 by the lead): a named script identity for
`whoami` (R9), and the named-binding read grove's §5.4 step 3 needs (KOD-43).

## 2026-11-10 decision: the directory (R5, R36, R37)

The directory lists every name the index declares, every held name, and
every name a binding carries. Each row holds `name`, `declared`,
`deliverability`, `harness`, `generation`, `last_seen` and
`knock {can, how}`. `mycelium directory` prints them as `{"names": [...]}`.

- **Declared:** `done` is gone and `parked` is queue-only. `running` is live
  with a live current binding, and absent otherwise. A reboot makes a role
  absent, never gone. A provisional binding gives no delivery, so it doesn't
  make a name live.
- **Held:** held until its 7-day hold ends, then gone.
- **Undeclared (R37):** live only with a live current binding (a
  provisional one doesn't count), and queue-only otherwise. It is gone once
  its latest binding was last seen 7 days ago and its mailbox holds nothing
  unhandled. The mailbox is read through the store's own interface
  (`has_unhandled`): the store opens once per directory, through
  `open_store` and its rules, and only when its file exists, so a missing
  store reads as empty and nothing is created. A symlinked store, or one
  from a newer mycelium, is refused.
- **`last_seen`** is set at each SessionStart and by `Bindings.touch`, which
  changes nothing else. U8's UserPromptSubmit hook will call `touch`.
  **Known limit:** a session that runs more than 7 days without a
  SessionStart can read gone when it exits, until U8 (its UserPromptSubmit
  hook refreshes `last_seen` through `touch`) and U5 (its sweep records when
  it first sees a binding dead, and the directory then times gone from the
  later of that and `last_seen`) land. U4b builds neither.
- **Knock:** set only when live, `sendmessage-hint` for Claude and
  `codex-queue` for Codex (KTD9). Codex's own corroboration check comes in
  U11.
- **The harness and generation** shown are the live binding's, else the
  latest current one's, else the latest binding's.
- **Null fields:** `harness`, `generation` and `last_seen` are null for a
  name with no binding, and `generation` also when the binding shown was
  never current. `knock.how` is null unless `knock.can`.
