# mycelium

**kodhama's agent communication layer**: every agent knows its manager, peers
and reports, and gets its mail.

**Status: early.** This release holds the command line's skeleton, the folder
resolver and the message store. Mailboxes, the directory and the harness
hooks come in later releases.

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
| state | `$XDG_STATE_HOME/kodhama/mycelium` | bindings, hold records, cursors over the clusters' audit logs |
| data | `$XDG_DATA_HOME/kodhama/mycelium` | the message store (`store.sqlite3`): mailboxes and questions |
| cache | `$XDG_CACHE_HOME/kodhama/mycelium` | the index built from the clusters; safe to delete |
| clusters | `$XDG_STATE_HOME/kodhama/clusters` | the registry of clusters, shared with the other kodhama tools |

When an `XDG_*` variable is unset, empty or relative, mycelium uses
`~/.local/state`, `~/.local/share` or `~/.cache` in its place, on macOS too.

- **The paths are canonical.** A symlinked home or XDG folder resolves to its
  target, so on macOS a folder under `/var` and the same one under
  `/private/var` are one path.
- **Its files are owner-only.** Folders mycelium creates are `0700`. The
  store and its `-wal` and `-shm` files are `0600`, and any of them with a
  looser mode is tightened when the store is opened. A folder that already
  exists keeps its mode and owner: mycelium neither tightens nor checks it.
- **It refuses a symlink it finds when the store opens.** The store's path
  must be canonical: a symlink anywhere in it, such as a linked `kodhama/`
  folder, or in place of the store, `-wal` or `-shm` file, is refused with an
  error before anything is created through it.
- **These checks catch a misconfiguration, not an attacker.** They run as the
  store opens. A process running as the same user that swaps a link into the
  `0700` folder while the store opens can get past them.

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
