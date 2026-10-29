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
The switch to WAL comes after the schema. It needs the file to itself, and
SQLite does not apply its busy timeout to it, so an opener that meets another
opener there retries the switch until the same timeout runs out.
`PRAGMA user_version` records the schema version, so a later version can
migrate the file. A file whose version is newer than the running mycelium is
refused, and left as it was. `tests/test_store.py` starts 24 processes on one
fresh path at once, and opens a file caught between the two steps while
another connection holds the write lock.

**What would change it.** If a later unit needs mail to survive power loss,
turn on `fullfsync` and record the measured cost in a new dated entry here.
