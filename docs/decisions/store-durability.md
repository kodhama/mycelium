---
type: decision
title: "Store durability: survive a process crash"
description: What a stored message survives, and the SQLite settings that hold it there.
decided: 2026-10-29
ticket: GRO-47
---

# Store durability: survive a process crash

**A message the store has accepted survives the posting process crashing,
being killed, or exiting midway. It is not promised to survive power loss or
a kernel panic.** Power loss is not a v0 goal.

**Settings.** The store is SQLite (hub plan U1, at
`areas/agent-comms/plans/2026-10-27-feat-mycelium-plan.md`, revision
`c9b114f5`). It runs with:

- `journal_mode=WAL`, which is persistent in the file;
- `synchronous=NORMAL`, so a commit is written to the WAL file before
  `append` returns, without a sync on every commit;
- `fullfsync=OFF`, so on macOS it never asks the drive to flush its cache.

A commit that has returned is in the operating system's file cache. A killed
process cannot lose it. A power cut can lose the last commits, and WAL
recovery then rolls the database back to an earlier consistent state, never
a torn one.

**Why not full sync.** U1 measured both on this hardware:

- `PRAGMA fullfsync=ON` costs about 13 ms more per post;
- under 20 concurrent posters, full sync stretched a run from about 0.4 s to
  about 5 s.

A hook or a post waits on that time, so it is not worth paying for a promise
v0 does not make. Turned down: `fullfsync=ON` and `synchronous=FULL`.

**Two first runs at once.** U1 left one case unmeasured: two processes
creating one new database at the same moment. Each opener takes the write
lock with `BEGIN IMMEDIATE` before it creates anything, and it creates the
schema with `IF NOT EXISTS`. The openers then create it one at a time, and
every one after the first finds the schema already in place.
The switch to WAL comes after the schema, and it needs the file to itself.
Measured on SQLite 3.53.1: while another connection holds a SHARED lock, the
switch waits out the busy timeout; while one holds RESERVED, it fails at
once. Both raise `SQLITE_BUSY`, so an opener retries the switch until a
deadline of the same length runs out. An open can wait up to
`BUSY_TIMEOUT_SECONDS` for the write lock that creates the schema and as long
again for the switch, so about twice that at worst. The read-only version
check before them (below) adds a wait only behind a writer committing to a
file still in rollback mode.
`PRAGMA user_version` records the schema version, so a later version can
migrate the file. A file whose version is newer than the running mycelium is
refused before anything writes to it. The version is read over a read-only
connection, which can neither checkpoint nor delete a WAL a crash left, and
before any file's mode is changed. That connection may still rebuild `-shm`,
the index SQLite derives from the WAL, or add an empty `-wal` beside a clean
file. `tests/test_store.py` starts 24 processes on one fresh path at once,
and opens a file caught between the two steps while another connection holds
the write lock.

**No descriptor on a store file that may be in use.** Closing any descriptor
on a database file drops every POSIX lock the process holds on it, a
connection's included
([How To Corrupt An SQLite Database File](https://sqlite.org/howtocorrupt.html),
section 2.2). Opening the store therefore checks for symlinks with `lstat`,
tightens modes with `chmod` by path, and opens only a store file it has just
created with `O_EXCL`. A test holds the write lock in one process, opens the
store there, and checks that another process is still kept out.

**What would change it.** If a later unit needs mail to survive power loss,
turn on `fullfsync` and record the measured cost in a new dated entry here.
