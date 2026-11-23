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
- **The returning paths, corrected:** a session kept out after a reboot
  returns only as a fresh session, or by `adopt` on a lease grove offers it.
  `adopt` refuses a live claim that names another session, so "only through
  `adopt` or a fresh session" above means that. The lead's acceptance stands.
- **Known limit (KTD6, the lead agreed):** a kept-out session that grove's
  live claim still names keeps the lease in force through the claim's
  session. Other sessions go provisional, and only that session can adopt
  the nonce. The lease is that session's.

**Known limits of the lease rules, left to a follow-up story** (re-check of
`71dad14` to `c041060`, GRO-50; the lead ruled to record them, not fix them
here). A third fix in that round, which let a superseded session's running
process keep the lease in force, was reverted: it let a stale nonce lock a
name with no possible holder.

- **F3:** a lease holder's `/clear` that has no launch name yet (lessons C2)
  supersedes the holder, and a superseded session keeps no lease in force.
  So an outside session can bind current until the successor's name appears.
- **G1:** a kept-out session can take the name back by moving to another
  herdr name and back. Moving resets its `seen_holds`.
- **G2:** a kept-out session seen in a new pid with source `startup` is
  decided again and can take the name. That is unlikely for Claude, and U11
  maps Codex's sources.
- **G3:** a lease holder's successor that has no nonce, resumed in a new pid
  after the holder's process ends, no longer keeps the lease in force.
- **G4:** a G2-unbound session's stale nonce keeps a lease in force after
  another session adopted that nonce and died, while the unbound session
  hasn't re-run its hook.

## 2026-11-14 acceptance-review fixes (GRO-50)

The lead's acceptance review of `7dfd265` found three small faults, fixed here.

- **A path that can't be looked at is not absent.** `os.path.lexists` reads
  a path under a folder that can't be searched as absent. So `whoami` and
  `adopt` exited 1 ("no binding for this session yet") where they could not
  tell, and `directory` could call an undeclared name gone while its mail
  waited. mycelium now looks with `lstat` (`files.exists`): only a missing
  path, or one under a file, is absent, and any other error is exit 3.
- **`adopt` reads the time once it holds the write lock.** The command line
  read the time before the store's busy wait of up to 30 seconds, so a
  pending claim that expired during the wait could still be adopted. Its
  expiry is now checked against the time read after the lock is taken
  (`Context.clock`).
- **When `directory` exits 3 for the message store.** The entries above say
  `directory` exits 3 when the message store can't be read. It opens that
  store only for an undeclared name with no hold and no live binding whose
  7-day clock has run out, to see whether mail waits. Only then does a store
  that can't be read give exit 3: a symlink, a newer schema, a corrupt file,
  or a folder on its way that can't be searched. Otherwise the listing exits
  0 without reading it.

## 2026-11-14 limit labels, a date and a correction (U4c U1, GRO-74)

U4c rebuilds the binding rules on one record per name, in four slices (hub
plan `areas/agent-comms/plans/2026-11-15-u4c-name-records.md` at `69a9549`,
unit U1). This first slice changes no rule. It pins today's behaviour for
the 27 rows the plan's scenario matrix marks right but untested (for A50
that verdict is right only in part, below), and six rows of the plan's
review (X1-X3, X7, X8, X10), so the later slices can't regress them, and it
fixes this record where GRO-72's doc nits, and the reviews of this slice,
found it off. The plan words the nits as edits to
earlier entries; they land here instead, since this record is append-only
(the lead's ruling, GRO-74).

- **The known limits get names.** Their letters clashed with rule G2 and
  lessons G3 above. From here on they are: **nameless clear** [F3],
  **rename back** [G1], **startup escape** [G2], **nameless resume** [G3]
  and **stale nonce** [G4]. The list is otherwise unchanged, but the pins
  showed two of its limits reach differently from their text. The startup
  escape reaches only a kept-out session that never held the name nor went
  provisional for it, such as a fork: a kept-out former holder seen in a new
  pid with source `startup` stays out, and so does one that once went
  provisional, since its `seen_holds` is set. A superseded holder seen that
  way is decided again and can bind current, but that is matrix row A43,
  which the plan's U4 fixes, not this limit.
- **Nameless clear is wider than its text.** The lease is not kept "until the
  successor's name appears": the holder is superseded, so once named the
  successor binds current with no nonce, and an outside session can still
  bind current after it. A `/resume` with no launch name in the holder's
  process does the same, unless it lands on the session the claim names or
  on one that carries the lease's nonce (`runs()` in `_for_name` matches
  either). The nonce keeps the lease only until that session's next start
  with a launch name, which keeps it out and clears its nonce; an outside
  session can then bind current without the nonce.
- **A launch name keeps the lease less often than the text says.** A
  `/clear` with a launch name keeps it, and so does a `/resume` with one
  onto a session decided afresh. A `/resume` onto a session whose binding
  continues (kept out, a fork's, a nested one's, or current or superseded
  under another name) re-confirms that binding, which supersedes the holder
  in its process, and an outside session can then bind current without the
  nonce.
- **A fork restored after a reboot becomes current.** With no holder in the
  new boot, a fork that `resume` restores is decided as a fresh start: it
  binds current and supersedes the dead holder. Matrix row A50 expected the
  fork rules to keep it unbound, and the rule above that "a fork or a nested
  session stays unbound until it adopts the nonce" does not hold for it. Its
  pin records today's behaviour, reported on GRO-74 for a later slice to
  rule on.
- **The known-limits list dates from 2026-11-12.** "Known limits of the lease
  rules, left to a follow-up story" is part of that day's entry: the
  `71dad14` to `c041060` re-check it cites was reported on GRO-50 that day.
- **"`adopt` refuses a live claim that names another session"**, in "The
  returning paths, corrected", means a session of another process. A live
  claim that names another session of the caller's own process can be
  adopted, as a `/clear` or `/resume` successor does; the 2026-11-10 adopt
  rule above says the same ("a live one held by this session or another
  session of its process"). The 2026-11-10 exit-code entry's "a live claim
  another session holds" reads the same way. A claim naming a session of
  another process is refused whether that session still runs or not.

## 2026-11-15 one record per name for the clock, and a schema reset (U4c U2, GRO-75)

The second U4c slice (hub plan
`areas/agent-comms/plans/2026-11-15-u4c-name-records.md` at `69a9549`, unit
U2) moves the facts R37's clock needs off the session rows into one record
per name, so writing a session row can't erase them. The holder, the nonce
and the lease stay on the rows until U3, and keep-out until U4.

- **The name record.** The `names` table replaces `generations`. It holds
  `declared`, `generation` (the last one issued), `epoch` (the hold count,
  until now `holds`), `held_boot`, `bound_seen` and `first_bound_at`. The
  first SessionStart decided under a name makes its record, a fork's or a
  provisional one's included. One that re-confirms a session still bound to
  another name makes none, since no row then carries the name it brought.
  Nothing deletes a record, so generations still never repeat. Session rows drop `declared` and `bound_seen`. Each
  row keeps `seen_holds`, now the name's `epoch` when the row last held the
  name or went provisional for it, until U4 moves it to sightings. This
  replaces "the pieces" entry's `declared` on each row and its second
  table, and the `generations` table in the 2026-11-12 entry's "How"
  bullet.
- **The clock reads the record.** The rule of the 2026-11-12 entry is
  unchanged: an
  undeclared name with no hold and no live current binding is gone once 7
  days have passed since a binding last held it, or, if none ever did,
  since it was first seen, and nothing unhandled waits in its mailbox. Those
  two times are now the record's `bound_seen` and `first_bound_at`.
  `bound_seen` moves at every write of a current row, `touch` included, and
  at no other write. `first_bound_at` never moves. This replaces the
  2026-11-12 "How" bullet that read the clock from the latest `bound_seen`
  among the name's rows, or their earliest `bound_at`. A rename now leaves
  the clock as it was. Pinned: the only holder renames away and the name
  goes 7 days after it last held it (A116); an old fork stays behind and the
  clock still runs from the holder (A117); the earliest row renames away and
  a name nobody held stays gone (N1).
- **The directory lists every name with a record**, so a name whose
  sessions all moved to other names stays listed. This
  replaces "every name a binding carries" in the directory entry.
- **`declared` lives on the record.** A SessionStart rewrites it from the
  index when the session ends up under its launch name: decided under that
  name, or re-confirmed under it. One with no launch name, one that
  re-confirms a session still bound to another name, and `adopt` leave
  every record's `declared` alone. So a name a cluster declares later reads
  declared at its session's next SessionStart under that name (AT7). Until now only a new
  or renamed row recomputed it. The directory still reads declared-ness from
  the index, not from this field.
- **`bind` and `touch` read the time once they hold the write lock**, as
  `adopt` does since the 2026-11-14 acceptance-review fixes. Both used the time read before
  the busy wait of up to 30 seconds.
- **Schema 2, and an older bindings file is reset on open.** The 2026-11-10
  note on "held since" kept version 1 because the file was new and
  unreleased. That is still true, so an older file is emptied rather than
  migrated: in the transaction that makes the schema, every bindings table
  any version made, or U4 will make, is dropped (`bindings` and
  `generations` from version 1, `names`, and `sightings`, which U4 adds). `files.open_database` takes that reset step, which runs only
  for a file of an older version, never a new one. The message store passes
  none. A newer file is still refused before any write.
  - **Why reset:** no user can have binding rows. The only release,
    `v0.1.0` at `c55c9d2`, predates bindings. The CLI opens the bindings
    file only if it exists, nothing in `src/` calls `bind`, and the hooks
    that will (hub U8, U11) aren't built (the lead's check, recorded in the
    plan's KC7 with the PM's approval on 2026-11-14). And the old rows hold
    exactly the facts a rewrite lost, so rebuilding records from them would
    carry the faults over.
  - **Why not keep version 1:** an old-shape version-1 file would skip
    schema creation and then fail on the missing tables, with exit 3.
  - **What was promised:** `user_version` exists so a later version can
    tell an older file and migrate it ([store durability](store-durability.md)).
    The bump keeps that: the version says which shape the file has.
  - **Sunset:** the reset is safe only while no released mycelium writes
    rows. The first release that ships the hooks (hub U8) ends it, and the
    reset step is removed then; later schema changes migrate or refuse. U3 and U4 each bump the version again,
    each with a reset test from the version before.
- **Known limit until U3 merges: continuing resume** [F4]. **F4 is open: a
  non-heir's `/resume` in the holder's process supersedes the holder and
  lets the name's lease lapse.** That is a `/resume` with a launch name onto
  a session whose binding continues (kept out, a fork's, a nested one's, or
  current or superseded under another name): it re-confirms that binding,
  which supersedes the holder, and an outside session can then bind current
  without the nonce. The 2026-11-14 limit-labels entry described this
  behaviour ("A launch name keeps the lease less often than the text
  says"). The PM ruled it not an accepted limit but the lease bypass the
  plan's attack flagged as P1 (GRO-73, 2026-11-14 and 2026-11-15): U3
  closes it, at a plan revision still to be set, and the own-process
  supersede (A30) stands. This slice doesn't change it.
- **No other rule changes.** Every pin U1 added passes unchanged, and
  existing tests changed only where they read a moved field or call the
  directory with the name records.

## 2026-11-21 holder, nonce and lease on the name record (U4c U3, GRO-76)

The third U4c slice (hub plan
`areas/agent-comms/plans/2026-11-15-u4c-name-records.md` at `6e4953b`, unit
U3) moves the holder and its lease nonce onto the name record, decides the
lease from it, and closes F4. Keep-out stays on the rows' `seen_holds` until
U4.

- **The record holds the holder.** `names` gains `holder` (harness and
  session id), `nonce` and `lease_pid`; session rows drop `nonce`. Every
  becoming-current (a fresh bind, a successor, the holder decided again, an
  adoption) writes the holder, raises `epoch` by one and records the boot. A
  re-confirm writes no holder, `epoch` or nonce, and `touch` writes only
  `bound_seen` and `last_seen`. A binding's `nonce`, as `whoami --json`
  prints it, is the record's when the session is the holder of the name its
  row points at, and null otherwise, so its JSON keeps its keys. A nameless
  successor holding a name therefore shows a null nonce until it binds
  under that name. The nonce is set by
  adoption, kept by an heir, and cleared when a session becomes current with
  no lease in force; a holder that goes provisional keeps it.
- **The lease gate.** A pending claim holds while unexpired, as before. A live
  claim of this boot holds only (a) through a running row of the name, not
  superseded, whose session the claim names, or (b) through the name's
  holder, still pointing at the name or at none, while the claim's nonce is
  the name's: a holder that runs and is not superseded (b1), or a superseded
  holder whose `lease_pid` runs (b2). No other row's nonce counts: this
  replaces the 2026-11-12 entry's rule that any running session of the name
  carrying the nonce keeps the lease, and its rule that "a superseded
  session never keeps a lease in force", which (b2) now limits to every
  superseded session but such a holder. The caller's own row is judged with its
  pid and the name being decided. Every bind path that would make a session
  current passes the gate: under a lease in force it becomes current only as
  the holder or the holder's successor, with the claim's nonce, and is
  provisional otherwise.
- **Process succession replaces the nonce on the row.** A session succeeds
  the name's holder when the holder runs in its process (same harness, pid
  and boot), is not superseded and still points at the name or at none. A
  fork or nested session never succeeds, nor does a kept-out session to the
  name it is kept out of. With a launch name, the gate decides: heir, current
  at the next generation with the nonce; under another nonce's lease,
  provisional; with no lease, current with the nonce cleared. With no launch
  name, under a lease in force with the name's nonce, the successor becomes
  the holder, unbound and nameless, so the lease stays in force through it;
  with no such lease nothing passes. A session that starts with no launch
  name while still bound to another name, or kept out of this one, is no
  successor: it supersedes the holder as a non-heir (below). A holder that
  renamed away passes nothing of its old name on.
- **F4 is closed** (the PM's rulings, GRO-73, 2026-11-15 and 2026-11-18).
  A session that is not the holder's heir and supersedes it in its own
  process (A30, which stands) leaves it the holder with the nonce. While a
  live claim carries that nonce, the record stores the holder's pid as
  `lease_pid`, and the lease stays in force while that pid runs or until the
  claim ends, whichever comes first; outsiders go provisional. In that pid,
  the `/resume` back to the holder, named for the role, is decided again and
  is current at the next generation with the nonce, as the 2026-11-10
  `/resume` back is; a `/clear` named for the role there is heir the same
  way. A nameless successor holding the name counts as the holder here too.
  Seen at another pid, or with no launch name, a resume or compaction
  leaves the holder superseded. A `startup` at a new pid that is decided
  again leaves it the holder but no longer superseded: provisional under
  the lease if named, unbound if nameless. From there A118's case stays
  open until U4: the lease holds through the holder's own row, even after
  `lease_pid`'s process ends, and the holder's next start that is decided
  again and named for the role, at any pid, is current with the old nonce.
  `lease_pid` never takes the caller's pid. `lease_pid` is
  cleared whenever a session becomes current for the name or the holder
  moves, and once the claim has ended: replaced by another claim, removed or
  unreadable. The claim file changes outside `bind`, so that clear happens at
  the next decision for the name. With no live claim nothing is stored.
  This closes the U2 entry's "Known limit until U3 merges", which stays as
  written above.
- **`adopt` refuses a live claim that names no session** (A77, ruled): it is
  held neither by this session nor by another of its process, and grove
  never writes one. **The current holder adopting its nonce again is a
  no-op success** (A78, ruled): same generation, same `epoch`, nothing
  superseded; only `last_seen` and `bound_seen` move. A holder that is not
  current, such as one G2 unbound, still takes the next generation.
- **Fixed limits:** nameless clear [F3] (A27), nameless resume [G3] (A28)
  and stale nonce [G4] (A80), each pinned. **Changed outcome:** a holder
  renamed away and back under its own live claim is current with its nonce
  (X9); until now it ended provisional, since the rename dropped the nonce.
- **Still open until U4:** a superseded session's return at a new pid with
  source `startup` (A118, N2), and the keep-out of a nameless successor that
  lost the lease between processes, once an outsider took the name and it
  resumes named (X5's second half): today it takes the name back. Startup
  escape and rename back stay as listed.
- **Schema 3.** A version-2 file is reset on open, as the U2 entry says,
  with its own reset test, and two openers racing over it reset it once. A
  newer file is still refused.
- **A missing name record now raises.** Every decision under a name makes
  its record first, so a write that finds none is a bug, not a silent no-op.

## 2026-11-23 keep-out on the name record, and sticky states (U4c U4, GRO-81)

The last U4c slice (hub plan
`areas/agent-comms/plans/2026-11-15-u4c-name-records.md` at `980a81c`, unit
U4, with its U3 follow-ups and the A50 ruling) decides keep-out from the
name's own store, makes sticky states hold for the boot at any pid or
source, and keeps a fork or nested origin across a reboot.

- **Sightings replace `seen_holds`.** A `sightings` row, keyed by (name,
  harness, session id), holds a boot and an epoch. It is made at the
  session's first decision that carries the name, with this boot and an
  empty epoch. A later decision that carries the name and isn't kept out
  moves its boot to this one. Its epoch is set to the name's `epoch` when the
  session goes provisional, becomes current, adopts, or succeeds the holder
  with no launch name under its lease, and it never falls. A decision with
  no launch name never touches it, and a fork or nested decision only makes
  it if absent. Sightings belong to the name: no rename, `/clear` or
  `startup` rewrites or drops them (the PM's condition on the recorded
  deviation, pinned by `test_a_session_rename_leaves_the_old_names_sightings_intact`).
  Like name records and origins, sightings are never pruned: hub U5 may
  prune rows only. Rows drop `seen_holds`; every `seen_holds` in the entries
  above now means the sighting's epoch.
- **The keep-out rule.** A session is kept out of a name when it is not the
  name's holder, the name was last held in this boot, it has a sighting of
  the name, and that sighting is from an earlier boot (after a reboot the
  first session to hold the name keeps the others out, a never-holder
  included) or has an epoch below the name's (someone held the name since
  this session last held it or went provisional for it). A session with no
  sighting, or with an empty epoch, such as one the unknown-nesting barrier
  left unbound, is never kept out in its boot, as before.
- **Succession comes first** (KC4; the lead's ruling, Option A of the U4 gap
  list). A named decision checks process succession, computed without the
  lease, then keep-out, then the holder decided again, then a fresh bind,
  all through the lease gate. So the `/resume` back of the 2026-11-10 entry
  still works: the superseded session is not the holder and its sighting is
  behind, but it succeeds the session that holds the name in its own
  process. A row kept out of this name in this boot, or unbound as a fork or
  nested session, never succeeds.
- **Sticky states hold for the boot.** A row of this boot that is
  superseded, or unbound as a fork, a nested session or kept out, keeps its
  state at every SessionStart, whatever its pid or source; a new pid with
  source `startup` no longer decides it again. The exceptions are the ones
  above: a kept-out row under a new herdr name binds fresh there; a
  superseded row whose name a session of its own process now holds current
  is decided again (the `/resume` back); and a superseded holder back named
  for the role in its name's `lease_pid`, while that keeps the lease in
  force, is decided again (F4). Current, provisional and other unbound rows
  keep their continuation rule.
- **Origins.** An `origins` row, keyed by session, records `fork` or
  `nested` at the session's first decision ever, its first SessionStart as
  this session id, and only when that decision is a fork or a nested one,
  even with no launch name. It is never pruned, survives a reboot, and only
  `adopt` clears it. Every decision reads it, a superseded row's included,
  so a fork superseded in its own process and resumed back stays unbound and
  never becomes the name's holder. A fork or nested decision is now unbound
  with the fork or nested reason even with no launch name, so a fork first
  seen with no name stays unbound once named. A session first decided as
  neither gets no row: one restored nested later is unbound while nested,
  and leaves no lasting mark.
- **A50 and F2 ruled** (PM ruling, GRO-73 d62c86fe). A session's origin is a
  fact about how that session id started, so a fork restored after a reboot,
  and a nested session restored by a caller that is no longer nested, stay
  unbound until they adopt. This rules the 2026-11-14 entry's "A fork
  restored after a reboot becomes current", whose pin is flipped. It is the
  one exception to "an earlier boot's row is a fresh start"; the row's other
  sticky states still reset at a reboot.
- **Fixed:** startup escape [G2] (A41, A42) and rename back [G1] (A46), each
  pinned. Also fixed and pinned: a superseded session seen at a new pid with
  `startup` stays superseded (A43), and so does a superseded lease holder,
  whose lease follows the gate only (A118, N2); a nested session there stays
  nested (A44); an earlier-boot session restored with no name stays out once
  named (A121); a fork or nested session first seen with no name stays
  unbound once named (A122); the nameless successor that lost the lease
  between processes is kept out when it resumes named (X5's second half);
  and a kept-out session whose row was deleted, as hub U5's pruning would,
  stays out under the same session id (X11). This closes the U3 entry's
  "Still open until U4".
- **A118's wider case is closed.** The U3 entry says a `startup` at a new
  pid decides F4's superseded holder again, after which the lease holds
  through its own row past `lease_pid`'s process and its next named start is
  current with the old nonce. Now it stays superseded at any pid, so the
  lease lapses when `lease_pid`'s process ends. The pin assumes that pid is
  not reused within the boot.
- **Three corrections to the U3 entry.** "`lease_pid` is cleared whenever …
  the holder moves": moves means the holding passes to another session.
  "Provisional under the lease if named" held only while the lease was in
  force, and that case is gone with A118's. And the lease through the
  holder's own row lasts only while that row runs.
- **A nameless successor is never kept out or a fork.** It succeeds the
  holder only if its sighting doesn't keep it out of the name, whatever its
  row points at, and never as a fork or nested session.
- **One read of the claim per decision.** A locked `bind` or `adopt` reads
  each name's claim file once and decides on that read.
- **Schema 4.** A version-3 file is reset on open, as the U2 entry says,
  with every bindings table (`bindings`, `generations`, `names`,
  `sightings`, `origins`) dropped first. A newer file is still refused.
- **Known limits, restated.**
  - **Pid-less predecessor:** under a live claim, a pid-less predecessor
    reads live, and its successor can't inherit or adopt the lease (the
    2026-11-12 entry; plan U11).
  - **A rename between SessionStarts goes unseen** until the next one (the
    2026-11-10 G2 ruling).
  - **Sticky states hold under a new herdr name too.** A superseded, fork or
    nested row of this boot seen under another name keeps its state and its
    old name; before, a new pid with `startup` bound it fresh there. So a
    fork can no longer move to another name within its boot, except by
    `adopt`. Only a kept-out row binds fresh under a new name.
  - **Succession before keep-out reaches non-sticky rows.** A row that is
    not sticky in this boot but whose sighting keeps it out, such as an
    earlier-boot row, or a session renamed away and back or decided with no
    launch name, resumed into the process of this boot's holder succeeds that
    holder and takes the name at its next generation. Before this slice an
    earlier-boot row there was kept out. The same holds in a superseded
    holder's `lease_pid` while that keeps the lease in force: such a session,
    named for the role, is heir. The U3 pin
    `test_a_kept_out_session_never_succeeds_through_a_nameless_start` expects
    that session kept out and fails at this head; the lead rules on it
    (GRO-81).
  - **First decision ever, for a pruned session.** An origin is written
    only when a session has no row. Once hub U5 prunes rows, a pruned
    session that returns can't be told from a new one, so a later fork or
    nested decision of it would write an origin it should not have; one that
    was a fork or nested session keeps its existing row.
