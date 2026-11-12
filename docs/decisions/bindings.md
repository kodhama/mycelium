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
which records the U4b rulings; the 2026-11-12 entry moves it to `23d0776`),
the lead's rulings on GRO-50, and grove's lease
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
reason, the adopted nonce, the bound and last-seen times, and
`seen_generation`: the name's generation when the row last held the name or
went provisional for it (below). A second table keeps each name's last
generation, so generations never repeat.

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
    has ended, the hook binding follows newest-wins, unless another session
    has held the name since (the lead's ruling, below). While the lease
    holds, it stays provisional and supersedes nothing;
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
  a new process, as when herdr restores a pane with `claude --resume`. A
  session decided again follows the lead's ruling on a re-decided session
  (below).
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
  - A session decided again **keeps its generation while it is under the
    same name and nobody has taken a newer one**, in any state (the lead's
    ruling, below). A generation belongs to its name: a session decided under
    another name never carries the old one. A SessionStart that gives no
    launch name leaves the row's own name, generation and `seen_generation`
    in place, so re-running the hook while the names disagree loses neither.
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
session again, so it binds once its names agree, unless another session has
held the name since (the next ruling). **Known limit: a rename
between SessionStarts goes unseen.** Nothing checks until the next
SessionStart.

**A re-decided session's generation** (GRO-50 lead note, 2026-11-10). It
applies to a session `bind` decides again because it is unbound (other than a
fork or a nested session) or provisional: G2's "launch names now disagree"
once the names agree again, or a provisional binding whose lease ended.

1. **The same session under the same name, which nobody else has held since,
   keeps its generation**, as KTD4 has it: "Resume and compaction re-confirm
   the existing generation". A provisional session that never held the name
   has none, so when it turns current it takes the name's next one, its
   first.
2. **A session whose name another binding has held since stays out**, as
   KTD4 has it: "a superseded session can't take the role back by re-running
   its hook". It is unbound with the reason "another session held `<name>`
   since; adopt the lease or start fresh to take it back", and supersedes
   nothing. It returns only through `adopt`, which gives it a new generation,
   or a fresh start, a new session id. A provisional session that another
   session's adoption superseded already stays superseded; this rule covers
   one that left the name's holders first, as a nonce mismatch unbinds it.
3. **A session whose herdr name changed binds under the new name as a fresh
   binding**, at that name's next generation.

**How "held since" is decided:** each row records `seen_generation`, the
name's generation counter when the row last held the name (its own
generation) or went provisional for it. Every binding that becomes current
raises the counter, so nobody else has held the name since while the counter
still equals it. A generation alone can't tell this for a provisional row,
which has none, and timestamps would compare clocks across rows. The bindings
file is new on this branch and unreleased, so the column needs no migration
and `SCHEMA_VERSION` stays 1, the first schema. The rule doesn't look at the
boot: after a reboot, a session whose name another session held in the
earlier boot stays out too, and that holder keeps its generation when it
binds again.

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

## 2026-11-12 lead rulings and review fixes (GRO-50)

**A reboot ends the stay-out rule** (GRO-50 lead note, 2026-11-10). This
replaces the last sentence of "How held since is decided" above. **Only a
holder from the current boot keeps a session out.** After a reboot, a session
that returns, such as a pane herdr restores, is a new process and a fresh
start under KTD4. When another session held the name after it, it takes the
name's next generation. When nobody did, it keeps its own, as U4's restored
pane does (a resume re-confirms its generation). Otherwise a name whose earlier holder never came back
would have no possible holder, and the role would sit absent until someone
started it by hand. Within one boot the rule stays as it was.

- **How:** the `generations` table records `held_boot`, the boot in which a
  binding last became current for the name, and `holds`, a count every
  binding that becomes current raises, even one that keeps its generation.
  Each row's `seen_holds` replaces `seen_generation`: the name's count when
  the row last held the name or went provisional for it. A session stays out
  only when the count moved past its `seen_holds` and `held_boot` is the
  current boot. The generation alone missed a holder that keeps its
  generation, such as a G2-unbound holder whose names agree again (floor
  review, GRO-50).
- **Pinned:**
  - X held the name, Y superseded X, then a reboot. Only X's pane comes back,
    and X becomes current at the next generation.
  - The same holds for a session that G2 left unbound while Y held the name.
  - A holder that binds in the new boot, fresh or restored, still keeps such
    a session out, and so does one that holds again at its old generation.

**A SessionStart with no launch name keeps the row's old name, generation
and `seen_generation`** (lead ruling (i), same note). The directory row now
carries `state`, the shown binding's state, so that row reads `unbound` when
it is the one shown. A current binding under the name, even a dead one from
an earlier boot, is shown first. It
never makes the name live or knockable, since only a live current binding
does. **It never refreshes an undeclared name's 7-day clock either** (lead
ruling, GRO-50, 2026-11-12). R37 counts the time the name is unbound, and a
session with no launch name never holds the name. This replaces the
undeclared bullet's "its latest binding was last seen 7 days ago" above.

- **How:** each row records `bound_seen`, when it was last seen current, set
  whenever a current row is written, `touch` included. The clock runs from
  the latest `bound_seen` among the name's bindings. A binding never current,
  such as a fork, never moves it. Only a name nobody has held runs its clock
  from when it was first seen, the earliest `bound_at`.
- **For U5:** the "later of that and `last_seen`" in the known limit above
  now reads "the later of the death and `bound_seen`", as the plan has it at
  `23d0776`. With `last_seen`, a session that never held the name would
  refresh the clock again.
- **Mail isn't lost:** a name with unhandled mail stays queue-only.
- **`last_seen` in the directory row** is still the latest sighting of any
  binding under the name.

**A session decided again keeps the lease it adopted.** An unbound row keeps
its nonce, a SessionStart with no launch name included. When `bind` decides
a session again, the row's own nonce counts: both for whether its live claim
runs and for the heir that inherits the nonce. So a session that G2 unbound
and whose names agree again returns current with its nonce, and a later
`/clear` still inherits the lease.

**`adopt` refuses an earlier-boot or expired claim before it checks the
nonce.** Such a claim can't be adopted, so a mismatched nonce on it no longer
unbinds a provisional session. A session `adopt` makes current also
supersedes any other session of its own process, as `bind` does.

**`bound_at` restarts when a session is decided under a new name.** It says
since when the session has been bound to that name.

**A session that `bind` decides supersedes the other sessions of its own
process, whatever state it gets itself.** This is by design. One process runs
one session, and a `/clear` or `/resume` ends the session the process ran
before. So even an unbound or provisional result marks the process's earlier
session superseded.

**What the entries above replace, sentence by sentence** (ce-doc-review,
GRO-50):

- **The plan revision** is now `23d0776`. Since `c7b1b80` the plan places
  the R37 clock in U5 and U8 (`06c1feb`), has U5's sweep prune binding rows
  (`b7e90b2`), and times U5's gone clock from `bound_seen` (`23d0776`). U4b
  keeps every row: nothing here deletes one.
- **The reboot rule** replaces, besides the sentence named above, rule 2's
  "returns only through `adopt` ... or a fresh start, a new session id" for
  a session from an earlier boot. Here "fresh start" means what KTD4 means by
  it for a new process, even under the same session id. **Every row from an
  earlier boot is decided again, in any state**, superseded and stale
  current rows included. It stays out once a session of this boot has held
  the name, so after a reboot the first session to bind a name holds it (the
  lead confirmed this, GRO-50 lead note, 2026-11-12).
- **"Supersedes nothing"** in the binding decision (a provisional session
  "supersedes nothing" while the lease holds; a superseded or held-out
  session "supersedes nothing") means it supersedes no other session of the
  name. The own-process entry above still applies to each of them.
- **`touch` "changes nothing else"** now has one exception: it also sets
  `bound_seen`, as every write of a current row does.
- **"A lease exists"** counts any running session of the name whose session
  or nonce the claim names, an unbound one included. A lease holder that G2
  unbound still holds the lease, so a hand-started session stays
  provisional.
- **"So a Codex TUI can restart in place"** holds only with no lease in
  force. **Known limit:** under a live claim, a pid-less predecessor reads
  live (`unverified`), and its successor can't inherit or adopt the lease.
  So the successor stays provisional until the predecessor is superseded.
  Plan U11 settles pid-less liveness.

**After the last review at `e153df4`** (GRO-50):

- **A superseded session never keeps a lease in force.** "Any running session
  of the name" above leaves out a superseded one. It gave the role up, and
  with it the lease, though it keeps its nonce on the row.
- **A session kept out gives up its nonce.** Another session holds the name
  now, and with it the lease if it adopted one. So a `/clear` in the kept-out
  process inherits nothing and goes provisional.
- **An earlier-boot row that never held the name**, a fork for instance,
  stays out like any other once a session of this boot holds the name.
- **Known consequence** of the reboot rule: when this boot's first holder
  then dies, a session it kept out can come back only through `adopt` or a
  fresh session. For an undeclared name, which has no lease, that means a
  fresh session. The same dead end already held within one boot for a
  session kept out. The lead accepted it as KTD4's rule, "only adoption or a
  fresh start" (GRO-50 lead note, 2026-11-12).
- **Every `seen_generation` above**, in "the pieces", the re-decide rule and
  "How held since is decided", now means `seen_holds`, as the replacement
  list says.

**The last code round of the 2026-11-12 resume** (re-check of `e153df4`
to `71dad14`, GRO-50):

- **Kept out stays out for the boot.** A session unbound with "another
  session held `<name>` since" is re-confirmed, still unbound, at each later
  SessionStart of the boot, as a fork is. Before, a row from an earlier boot
  that never held the name was rewritten with this boot and could take the
  name at its next start. A new herdr name still binds it fresh under that
  name (rule 3).
- **An heir must have seen every hold of the name.** A `/clear` or
  `/resume` inherits a nonce only from a session of its process whose
  `seen_holds` equals the name's count. A session another one took the name
  from, and its process, inherit nothing.
- **A superseded session whose process runs on** in another session, such as
  a lease holder's `/clear` that has no launch name yet (lessons C2), still
  keeps the lease in force. One process runs one session, so the process
  still holds the lease. This qualifies "a superseded session never keeps a
  lease in force" above.
- **The returning paths, corrected:** a session kept out after a reboot
  returns only as a fresh session, or by `adopt` on a lease grove offers it.
  `adopt` refuses a live claim that names another session, so "only through
  `adopt` or a fresh session" above means that. The lead's acceptance stands.
- **Known limit (KTD6, the lead agreed):** a kept-out session that grove's
  live claim still names keeps the lease in force through the claim's
  session. Other sessions go provisional, and only that session can adopt
  the nonce. The lease is that session's.
