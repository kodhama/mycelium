# mycelium

**kodhama's agent communication layer**: every agent knows its manager, peers
and reports, and gets its mail.

**Status: early.** v0.1.0 holds the folder resolver and the message store.
`main` adds, unreleased, the org index, the bindings and the directory. The
command line offers only `--version`, `paths`, `whoami`, `adopt` and
`directory` so far. Mailboxes and the harness hooks come later.

## Run it

mycelium is a Python package with no runtime dependencies. Run it with
[uv](https://docs.astral.sh/uv/):

```sh
uvx --python 3.13 --from git+https://github.com/kodhama/mycelium@<tag> mycelium --version
```

`<tag>` is a release tag such as `v0.1.0`; the repo's tags list them.

- **Python 3.11 or later** is supported; CI tests 3.13 and 3.11.
- **Hooks pin one interpreter and one commit**:
  `uvx --python 3.13 --from git+https://github.com/kodhama/mycelium@<full commit sha> mycelium …`.
  Without `--python`, uv picks the newest Python it finds. A tag ref makes uv
  ask git where the tag points on every run, and a commit SHA does not
  ([decision](docs/decisions/hook-interpreter.md)).
- **The version is the git tag.** A `vX.Y.Z` tag builds as `X.Y.Z`, and any
  other commit as a `.dev` version.
- **`kodhama install mycelium` is not supported yet.** It arrives with
  mycelium's first skill: the kodhama CLI installs a tool's skills, and this
  release has none.

## Where it keeps its files

`mycelium paths` prints them. Resolving them creates nothing. **Opening
the store creates it, even to read**: its folders, its file and its schema.

| folder | default | holds |
| --- | --- | --- |
| state | `$XDG_STATE_HOME/kodhama/mycelium` | bindings, hold records, the index's last-good snapshots and published names, cursors over the clusters' audit logs |
| data | `$XDG_DATA_HOME/kodhama/mycelium` | the message store (`store.sqlite3`): mailboxes and questions |
| cache | `$XDG_CACHE_HOME/kodhama/mycelium` | the index built from the clusters; safe to delete |
| clusters | `$XDG_STATE_HOME/kodhama/clusters` | the registry of clusters, shared with the other kodhama tools |

When an `XDG_*` variable is unset, empty or relative, mycelium uses
`~/.local/state`, `~/.local/share` or `~/.cache` in its place, on macOS too.

- **The paths are canonical.** A symlinked home or XDG folder resolves to its
  target, so on macOS a folder under `/var` and the same one under
  `/private/var` are one path.
- **Its files are owner-only.** Folders mycelium creates are `0700`. The
  store, the bindings (`bindings.sqlite3` in state) and their `-wal` and
  `-shm` files are `0600`, and any of them with a
  looser mode is tightened when the store is opened. A folder that already
  exists keeps its mode and owner: mycelium neither tightens nor checks it.
- **It refuses a symlink it finds when the store opens.** The store's path
  must be canonical: a symlink anywhere in it, such as a linked `kodhama/`
  folder, or in place of the store, `-wal` or `-shm` file, is refused with an
  error before anything is created through it.
- **These checks catch a misconfiguration, not an attacker.** They run as the
  store opens. A process running as the same user that swaps a link into the
  `0700` folder while the store opens can get past them.

## The org index

**Each agent's org slice comes from an index of the `.kodhama/` clusters**:
its manager, reports, peers with their topics, interested parties and
escalation routes. mycelium never parses a cluster file. It reads them
through the cluster helper's `kodhama-cluster read --json`, once per
decision, and rebuilds the index when any cluster's revision changes.

- **The helper isn't released yet.** Until it is, the index reports the
  helper as unavailable, and the tests use a stand-in. The JSON shape it
  reads is set out in [the decision](docs/decisions/org-index.md).
- **A cluster that fails validation keeps its last good entry, marked stale
  with the reason.** That covers a hand edit, a cycle or two managers for one
  checkout. Its agents stay routable. Without a last good entry, the slice
  says the manager is unknown, never that there is none. A failing cluster
  keeps every name it had, with or without one. The clusters below a failing
  one keep their place, marked stale too.
- **A checkout whose folder is gone puts its names on hold** for 7 days, with
  their last-known manager, whether or not that manager was ever known.
- **The index in the cache can be deleted at any time.** The last-good
  snapshots, published names and hold records it can't rebuild live in state.
  Damaged hold or published-name records, or a state or cache folder that
  can't be read or written, stop the index with an error, never an empty list.

## Who is calling

**mycelium tells which session called it from the process tree, never from
an argument.** It climbs from its own process to the nearest harness.

- **Claude Code** is identified by its `claude` process.
- **Codex:** the walk finds a `codex exec` caller by its `codex` process.
  The session id is always the thread id Codex sets, `CODEX_THREAD_ID`.
  Inside Codex's sandbox `ps` is denied, so there the thread id alone
  identifies the caller. A Codex TUI runs its tools under one shared daemon,
  so its pid is unknown.
- **A session started inside another one** is reported as nested when the
  walk shows it. Where `ps` is denied, `CODEX_THREAD_ID` beside a Claude
  variable means one runs inside the other, so that caller is nested and
  unbound. A variable inherited from wherever the shell started reads the
  same.
- **Nesting can be unknown.** A Codex TUI caller, even one under a Claude
  session, and a caller found by thread id alone with no Claude variable
  beside it come back with nesting unknown, never "not nested". Such a
  caller can bind, but never supersedes a live binding the walk verified.
- **A Codex TUI is recognised by its shared daemon's path, on macOS.** That
  Codex starts the daemon by its path is not yet confirmed on a live Codex
  (plan U11 checks it). Linux `ps` shows no path, so there it is never
  recognised.
- **Droid and opencode** callers resolve as unbound for now.
- **Identity guards against mislabelling, not against an attacker.** A
  process of the same user can pose as a session by its process name and
  variables.

herdr panes are read through one module, which only reads: a pane's identity,
its foreground processes, and the pane that holds a given process. It was
built against herdr 0.9.3. The rules and their limits are in
[the decision](docs/decisions/caller-identity.md).

## Bindings and the directory

**Each session is bound to the role it was launched as, or told plainly that
it isn't.** The harness hooks bind it at every SessionStart. The rules are in
[the decision](docs/decisions/bindings.md).

- **Until the hooks ship (plan U8 for Claude, U11 for Codex), nothing
  binds.** `whoami` and `adopt` exit 1 for every session, with "no binding
  for this session yet".
- **`directory` exits 3 until the cluster helper, `kodhama-cluster`, is
  installed**, since it reads the index through it.

- **The launch name** comes from the harness's session name, `KODHAMA_AGENT`
  and herdr's name for the session's own pane. Every source present must
  agree; if none names it, or they disagree, the session is unbound. The pane
  title never counts, and `@maintainer` is never bound.
- **The newest session for a name is current**, and the one it replaces is
  superseded. The supersede is recorded once, so its notice can go out once;
  sending it is a later release's (mail, U5), and until then nothing tells
  the superseded session. Under grove's lease only the lease nonce makes a
  session current: `mycelium adopt <nonce>`. A fork, or a session started
  inside another one, stays unbound unless it adopts the nonce.
- **A resume or compaction keeps the session's binding**, even in a new
  process, as when herdr restores a pane: a current session stays current
  with its generation and lease, and a superseded one stays superseded. Two
  exceptions: a current session whose launch names now disagree is unbound,
  and a `/resume` back to a session its own process left makes it current
  again at a new generation.
- **A session unbound or provisional is decided again at its next
  SessionStart** (the lead's ruling on GRO-50, after KTD4). Under the same
  name, it keeps its generation if nobody else has held the name since, and
  a provisional one that never held it takes the name's next. If another
  session has held the name since, it stays unbound: re-running its hook
  can't take the role back, and only `mycelium adopt <nonce>` or a fresh
  session can. Under a new herdr name it binds fresh, at that name's next
  generation.
- **After a reboot, the first session to bind a name holds it.** Every
  binding from the earlier boot is decided again, whatever its state, by the
  usual rules: a lease or a caller of unknown nesting can still hold it back.
  Otherwise a session restored after a reboot is current, at the name's next
  generation if another session held the name after it, or its own if not.
  Once a session of the new boot holds the name, the others restored after
  it stay out, as above, a fork included, for the rest of the boot (the
  record lists the known gaps). They stay out even if that holder then
  dies: they return only as a fresh session, or by `mycelium adopt <nonce>`
  on a lease they can take: a pending claim, or a live one naming them or
  another session of their own process.
- **A session decided again with no launch name keeps its old name**, as
  unbound. The directory shows it as `"state": "unbound"` unless a current
  binding under the name is shown first, and it never makes the name live or
  keeps the name from going.
- **After a reboot every binding is dead**, so the role reads as absent until
  its session binds again.
- **A name declared in no cluster still binds**, as undeclared.

**`mycelium whoami` says who is calling.** Line 1 is the name exactly, and
line 2 is `generation <n>`; `--json` prints the full record instead. Readers
compare line 1 only, stripping one trailing newline: the cluster helper
compares it with `KODHAMA_AGENT`.

| exit | meaning |
| --- | --- |
| 0 | the caller is current |
| 1 | refused: provisional, superseded or unbound; the reason is on stderr and stdout is empty |
| 2 | a usage error: an unknown command or bad arguments |
| 3 | mycelium could not tell: the bindings store or state folder can't be read (a folder on the way that can't be searched included) or fails once open, or the boot id is unknown |

`whoami` reads neither the index nor the cluster helper. `adopt` and
`directory` read both, and exit 3 when either can't be read.

**`mycelium adopt <nonce>` adopts grove's lease.** On success it prints what
`whoami` prints and exits 0. It uses the same exit codes: 1 when it refuses
(no binding yet, no lease, a wrong nonce, a claim from an earlier boot, an
expired one, or a live one another session holds), 2 for a usage error, and
3 as above, or when the index or the cluster helper can't be read.

A caller the walk can't resolve, or any caller before the bindings file
exists, gets exit 1 from `whoami` and `adopt` before anything else is read.

Right after an in-place `/clear`, `whoami` exits 1 until the clear's
SessionStart hook has bound the new session id, then 0 with the next
generation.

**`mycelium directory` prints who is reachable**, as JSON:
`{"names": [{"name", "declared", "deliverability", "harness", "generation",
"state", "last_seen", "knock": {"can", "how"}}]}`. `state` is the shown
binding's stored state, `current`, `provisional`, `superseded` or `unbound`,
not its liveness: a dead current binding reads `current` beside `absent`.
Deliverability is `live`, `absent`, `queue-only`, `held` or `gone`, computed
from the clusters and the bindings each time. A name can be knocked only while live.

- **A name declared in no cluster is live only with a live current
  binding**; a provisional one doesn't count.
- **Null fields:** `harness`, `generation`, `state` and `last_seen` are null
  for a name with no binding, and `generation` also when its binding was
  never current. `knock.how` is null unless `knock.can` is true.
- **Exit codes:** 0 when printed, 2 for a usage error, and 3 when the index,
  the cluster helper or the bindings store can't be read, or the boot id is
  unknown.
- **The message store is read only when needed.** `directory` opens it only
  for an undeclared name with no hold and no live binding whose 7-day clock
  has run out, to see whether mail waits. Only then does a store that can't
  be read give exit 3: a symlink, a newer schema, a corrupt file, or a folder
  on its way that can't be searched. A missing store means no mail waits.

## What the store survives

**A stored message survives the posting process crashing or being killed.
It is not promised to survive power loss.** The store is SQLite in WAL mode
with `synchronous=NORMAL` and `fullfsync` off. Full sync would cost about
13 ms more per post, and much more under many concurrent posters
([decision](docs/decisions/store-durability.md)).

**A store written by a newer mycelium is refused, not misread.** The file
records its schema version, and an older mycelium that finds a newer one
stops with an error saying to update. It reads the version before it
writes anything, so the store's data, a WAL left by a crash included, stays
as it was.

## Develop

```sh
uv run ruff check
uv run ruff format --check
uv run pytest
uv run --python 3.11 pytest
```

Contributor and agent instructions are in [AGENTS.md](AGENTS.md).

## License

[MIT](LICENSE)
